import asyncio
import json
import traceback
from typing import Optional, Any
from uuid import UUID
import asyncpg
from datetime import datetime, timedelta

from config import (
    logger, DATABASE_URL, PG_POOL_MIN, PG_POOL_MAX, EMBED_DIM, MAX_CONCURRENT_API_CALLS,
    DUP_THRESHOLD, CONFLICT_THRESHOLD, RELATES_TO_THRESHOLD, MAX_TAXONOMY_PATHS
)
from utils import _now, generate_deterministic_id, _vector_literal
from llm import embed, extract_semantic_sections, evaluate_conflict

pool: asyncpg.Pool | None = None
primer_last_updated: datetime | None = None

CHUNK_BATCH_SIZE = 10

# volatility_class -> verify_after: static=NULL, high=+7d, medium=+30d, low=+365d
_VOLATILITY_DELTAS: dict[str, Optional[timedelta]] = {
    "high":   timedelta(weeks=1),
    "medium": timedelta(days=30),
    "low":    timedelta(days=365),
    "static": None,
}


def _compute_verify_after(volatility_class: str, from_dt: datetime) -> Optional[datetime]:
    delta = _VOLATILITY_DELTAS.get(volatility_class, timedelta(days=365))
    return from_dt + delta if delta else None


def get_pool() -> asyncpg.Pool:
    global pool
    if pool is None:
        logger.error("Attempted to access Database pool before initialization.")
        raise RuntimeError("DB pool not initialized")
    return pool

async def init_db(conn: asyncpg.Connection) -> None:
    logger.info("Initializing database schema")

    logger.debug("Creating extensions (vector, ltree)...")
    await conn.execute("CREATE EXTENSION IF NOT EXISTS vector;")
    await conn.execute("CREATE EXTENSION IF NOT EXISTS ltree;")

    dim_check = await conn.fetchrow(
        """
        SELECT atttypmod
        FROM pg_attribute
        JOIN pg_class ON pg_class.oid = pg_attribute.attrelid
        WHERE pg_class.relname = 'memories' AND pg_attribute.attname = 'embedding';
        """
    )
    if dim_check:
        db_dim = dim_check["atttypmod"]
        if db_dim != -1 and db_dim != EMBED_DIM:
            raise RuntimeError(f"Database vector dimension mismatch. DB requires {db_dim}, config specifies {EMBED_DIM}. Migration required.")

    logger.debug("Creating base 'memories' table...")
    await conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS memories (
            id UUID PRIMARY KEY,
            content TEXT NOT NULL,
            embedding vector({EMBED_DIM}) NOT NULL,
            category_path ltree NOT NULL DEFAULT 'reference.unknown'::ltree,
            supersedes_id UUID,
            archived_at TIMESTAMPTZ,
            metadata JSONB DEFAULT '{{}}'::jsonb,
            lexical_search tsvector,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            last_accessed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        """
    )

    logger.debug("Running migrations for any missing columns...")
    await conn.execute("ALTER TABLE memories ADD COLUMN IF NOT EXISTS lexical_search tsvector;")
    await conn.execute("UPDATE memories SET lexical_search = to_tsvector('english', content) WHERE lexical_search IS NULL;")
    await conn.execute("ALTER TABLE memories ADD COLUMN IF NOT EXISTS archived_at TIMESTAMPTZ;")
    await conn.execute("ALTER TABLE memories ADD COLUMN IF NOT EXISTS verify_after TIMESTAMPTZ;")

    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS memory_edges (
            source_id UUID NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
            target_id UUID NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
            relation_type VARCHAR(50) CHECK (relation_type IN ('supersedes', 'relates_to', 'depends_on', 'sequence_next')),
            PRIMARY KEY (source_id, target_id, relation_type)
        );
        """
    )

    logger.debug("Creating ingestion staging table...")
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ingestion_staging (
            job_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            raw_text TEXT NOT NULL,
            ttl_days INT,
            status VARCHAR(20) NOT NULL DEFAULT 'pending',
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            error TEXT
        );
        """
    )

    logger.debug("Establishing GIST & HNSW Indexes...")
    await conn.execute("CREATE INDEX IF NOT EXISTS memories_category_path_gist ON memories USING gist (category_path);")
    await conn.execute("CREATE INDEX IF NOT EXISTS memories_lexical_search_gin ON memories USING GIN (lexical_search);")
    await conn.execute(
        """
        CREATE INDEX IF NOT EXISTS memories_embedding_hnsw_idx
        ON memories USING hnsw (embedding vector_cosine_ops)
        WITH (m = 24, ef_construction = 100);
        """
    )
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS memories_verify_after_idx "
        "ON memories (verify_after) WHERE verify_after IS NOT NULL;"
    )

    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS context_store (
            key VARCHAR(200) PRIMARY KEY,
            value TEXT NOT NULL,
            scope VARCHAR(50) NOT NULL DEFAULT 'session',
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            expires_at TIMESTAMPTZ NOT NULL
        );
        """
    )
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS context_store_expires_idx "
        "ON context_store (expires_at);"
    )
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS context_store_scope_idx "
        "ON context_store (scope);"
    )

    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS primer_cache (
            key VARCHAR(100) PRIMARY KEY,
            content TEXT NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        """
    )
    logger.info("Database schema initialization complete.")

async def get_profile_chunks(conn) -> list[str]:
    """Fetch all active profile.* memory contents for primer summarization."""
    rows = await conn.fetch(
        """
        SELECT content FROM memories
        WHERE category_path <@ 'profile'::ltree
          AND supersedes_id IS NULL
          AND archived_at IS NULL
        ORDER BY category_path, created_at
        """
    )
    return [r["content"] for r in rows]

async def get_cached_user_context(conn) -> str | None:
    row = await conn.fetchrow(
        "SELECT content FROM primer_cache WHERE key = 'user_context'"
    )
    return row["content"] if row else None

async def set_cached_user_context(conn, content: str):
    await conn.execute(
        """
        INSERT INTO primer_cache (key, content, updated_at)
        VALUES ('user_context', $1, NOW())
        ON CONFLICT (key) DO UPDATE
            SET content = EXCLUDED.content,
                updated_at = EXCLUDED.updated_at
        """,
        content,
    )

def is_profile_path(category_path: str) -> bool:
    return category_path.startswith("profile")

async def process_and_insert_memory(
    text: str,
    conn: asyncpg.Connection,
    ttl_days: Optional[int] = None,
    dup_threshold: float = 0.95,
    conflict_threshold: float = 0.85,
) -> UUID:
    """
    Insert memory sections into the DB in batches of CHUNK_BATCH_SIZE per transaction.
    Sections are produced by LLM semantic extraction; each section gets embed, duplicate
    detection, and conflict evaluation scoped by category_path.
    """
    logger.info("Processing memory insertion (text length: %d, ttl_days: %s)", len(text), ttl_days)

    cat_rows = await conn.fetch(
        "SELECT category_path::text, COUNT(*) as cnt FROM memories "
        "WHERE supersedes_id IS NULL AND archived_at IS NULL "
        f"GROUP BY category_path ORDER BY cnt DESC LIMIT {MAX_TAXONOMY_PATHS}"
    )
    active_taxonomy = "\n".join(r["category_path"] for r in cat_rows)

    if not active_taxonomy.strip():
        active_taxonomy = "profile\nprojects\norganizations\nconcepts\nreference\nhealth\nlifestyle\npsychology"

    sections = await extract_semantic_sections(text, active_taxonomy)
    now = _now()

    base_metadata: dict[str, Any] = {}
    if ttl_days is not None:
        base_metadata["ttl_days"] = ttl_days

    api_semaphore = asyncio.Semaphore(MAX_CONCURRENT_API_CALLS)
    db_lock = asyncio.Lock()

    async def process_section(i: int, section: dict[str, Any]):
        chunk_path = section.get("category_path", "reference.unknown")
        chunk_tags = section.get("tags", [])
        chunk_threshold = float(DUP_THRESHOLD)
        chunk_conflict_threshold = CONFLICT_THRESHOLD
        chunk_volatility = section.get("volatility_class", "low")
        chunk_verify_after = _compute_verify_after(chunk_volatility, now)
        chunk_content = section.get("content", "")

        # Content-based IDs: isolate from LLM non-determinism (index shifts across re-ingestions).
        chunk_id = generate_deterministic_id(chunk_content)
        async with db_lock:
            exists = await conn.fetchval("SELECT 1 FROM memories WHERE id = $1", chunk_id)
        if exists:
            return {"id": chunk_id, "exists": True, "duplicate_of": chunk_id}

        async with api_semaphore:
            vec = await embed(chunk_content)

        vec_lit = _vector_literal(vec)
        async with db_lock:
            similar_mem = await conn.fetchrow(
                """
                SELECT id, content, 1 - (embedding <=> $1::vector) AS similarity
                FROM memories
                WHERE supersedes_id IS NULL
                AND archived_at IS NULL
                AND category_path <@ $2::ltree
                ORDER BY embedding <=> $1::vector
                LIMIT 1
                """,
                vec_lit, chunk_path
            )

        similarity = float(similar_mem["similarity"]) if similar_mem else 0.0

        if similarity > chunk_threshold:
            logger.debug("Section %s duplicate of %s (sim: %.3f)", chunk_id, similar_mem["id"], similarity)
            return {"id": chunk_id, "exists": True, "duplicate_of": similar_mem["id"]}
        elif chunk_conflict_threshold <= similarity <= chunk_threshold:
            logger.debug("Section %s conflicts with %s (sim: %.3f), evaluating...", chunk_id, similar_mem["id"], similarity)
            async with api_semaphore:
                resolution_result = await evaluate_conflict(similar_mem["content"], chunk_content)
                final_text = resolution_result["updated_text"]
                final_vec = await embed(final_text)

            chunk_metadata = dict(base_metadata)
            if chunk_tags:
                chunk_metadata["tags"] = chunk_tags
            chunk_metadata["volatility_class"] = chunk_volatility

            # Content-based ID for inserted content (isolated new state for supersedes, unified for merges).
            insert_id = generate_deterministic_id(final_text)
            return {
                "id": insert_id, "chunk": final_text, "vec": final_vec,
                "cat_path": chunk_path, "metadata": chunk_metadata,
                "exists": False,
                "supersedes": similar_mem["id"],
                "resolution": resolution_result["resolution"],
                "verify_after": chunk_verify_after,
            }
        else:
            chunk_metadata = dict(base_metadata)
            if chunk_tags:
                chunk_metadata["tags"] = chunk_tags
            chunk_metadata["volatility_class"] = chunk_volatility
            return {
                "id": chunk_id, "chunk": chunk_content, "vec": vec,
                "cat_path": chunk_path, "metadata": chunk_metadata,
                "exists": False, "supersedes": None, "resolution": None,
                "verify_after": chunk_verify_after,
            }

    section_data = await asyncio.gather(*(process_section(i, s) for i, s in enumerate(sections)))

    first_id: Optional[UUID] = None
    prev_id: Optional[UUID] = None

    # Process sections in isolated batches to avoid one giant transaction.
    for batch_start in range(0, len(section_data), CHUNK_BATCH_SIZE):
        batch = section_data[batch_start:batch_start + CHUNK_BATCH_SIZE]
        async with conn.transaction():
            for item in batch:
                chunk_id = item["id"]

                # When a section is a near-duplicate of a *different* existing memory,
                # use the DB-resident duplicate_of ID for edge chaining.
                # When inserting, use item["id"] (fresh UUID for supersedes, else deterministic).
                effective_id: UUID = item.get("duplicate_of", item["id"]) if item["exists"] else item["id"]

                if first_id is None:
                    first_id = effective_id

                if not item["exists"]:
                    insert_id = item["id"]  # Fresh UUID when supersedes, else deterministic
                    vec_lit = _vector_literal(item["vec"])
                    await conn.execute(
                        """
                        INSERT INTO memories (id, content, embedding, category_path, metadata, lexical_search, created_at, updated_at, last_accessed_at, verify_after)
                        VALUES ($1, $2, $3::vector, $4::ltree, $5::jsonb, to_tsvector('english', $2), $6, $6, $6, $7)
                        ON CONFLICT (id) DO UPDATE SET updated_at = EXCLUDED.updated_at
                        """,
                        insert_id, item["chunk"], vec_lit, item["cat_path"], json.dumps(item["metadata"]), now, item.get("verify_after"),
                    )

                    # Set supersedes_id on the OLD node pointing to the new replacement.
                    # Supersedes: new record has fresh ID; merges: new record has deterministic ID.
                    if item["supersedes"]:
                        old_id = item["supersedes"]
                        new_id = insert_id
                        await conn.execute(
                            "UPDATE memories SET supersedes_id = $1, updated_at = $2 WHERE id = $3",
                            new_id, now, old_id
                        )
                        # Redirect memory_edges: INSERT new edges, then DELETE old (avoids UniqueViolation)
                        await conn.execute(
                            """
                            INSERT INTO memory_edges (source_id, target_id, relation_type)
                            SELECT $1, target_id, relation_type FROM memory_edges WHERE source_id = $2
                            ON CONFLICT (source_id, target_id, relation_type) DO NOTHING
                            """,
                            new_id, old_id
                        )
                        await conn.execute(
                            """
                            INSERT INTO memory_edges (source_id, target_id, relation_type)
                            SELECT source_id, $1, relation_type FROM memory_edges WHERE target_id = $2
                            ON CONFLICT (source_id, target_id, relation_type) DO NOTHING
                            """,
                            new_id, old_id
                        )
                        await conn.execute(
                            "DELETE FROM memory_edges WHERE source_id = $1 OR target_id = $1",
                            old_id
                        )

                    await conn.execute(
                        """
                        INSERT INTO memory_edges (source_id, target_id, relation_type)
                        SELECT $1::uuid, id, 'relates_to'
                        FROM memories
                        WHERE id != $1::uuid
                          AND supersedes_id IS NULL
                          AND archived_at IS NULL
                          AND (category_path::text = $3::text OR 1 - (embedding <=> $2::vector) > $4)
                        ORDER BY (1 - (embedding <=> $2::vector)) DESC LIMIT 6
                        ON CONFLICT DO NOTHING
                        """,
                        insert_id, vec_lit, item["cat_path"], RELATES_TO_THRESHOLD
                    )

                    if prev_id and prev_id != effective_id:
                        await conn.execute(
                            "INSERT INTO memory_edges (source_id, target_id, relation_type) VALUES ($1, $2, 'sequence_next') ON CONFLICT DO NOTHING",
                            prev_id, effective_id
                        )
                    prev_id = effective_id
                else:
                    await conn.execute("UPDATE memories SET last_accessed_at = $1 WHERE id = $2", now, effective_id)

    if first_id is None:
        raise ValueError("No sections produced from input text")

    profile_changed = any(
        is_profile_path(section.get("category_path", ""))
        for section in sections
        if not section.get("exists", False)
    )
    from tools.context import synthesize_system_primer
    async with get_pool().acquire() as primer_conn:
        await synthesize_system_primer(primer_conn, profile_changed=profile_changed)

    return first_id