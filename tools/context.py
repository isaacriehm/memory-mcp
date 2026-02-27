"""Context initialization, verification, and system primer synthesis."""

import json
import traceback
import asyncio
from typing import Optional, Any
from uuid import UUID

from fastmcp import Context

from config import logger
from utils import _now, _vector_literal, _add_ttl_warning, generate_deterministic_id
from llm import embed
from db import get_pool, _compute_verify_after
import db

from .search import _build_taxonomy_tree


async def initialize_context(ctx: Context) -> dict[str, Any]:
    """REQUIRED — Call this FIRST at the start of every session, before any other memory tool or reasoning.
    Returns the System Primer: user identity, full taxonomy map, and retrieval guide.
    Must be called exactly once per session to orient the agent before interacting with the knowledge base.
    If `verification_block` is non-empty, inject it into the System Primer under ## Verification Required.
    Query the user regarding the accuracy of those specific records BEFORE executing any other commands."""
    logger.info("Tool invoked: initialize_context")
    try:
        db_pool = get_pool()
        async with db_pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, content, category_path::text, created_at, updated_at, metadata::text
                FROM memories
                WHERE category_path ~ 'reference.system.*'::lquery
                  AND supersedes_id IS NULL
                  AND archived_at IS NULL
                ORDER BY created_at ASC
                """
            )

            expired_rows = await conn.fetch(
                """
                SELECT id, content, category_path::text, verify_after, metadata::text
                FROM memories
                WHERE supersedes_id IS NULL
                  AND archived_at IS NULL
                  AND verify_after < NOW()
                ORDER BY verify_after ASC
                LIMIT 3
                """
            )

        results = []
        for r in rows:
            item = {
                "id": str(r["id"]), "content": r["content"], "category_path": r["category_path"],
                "created_at": r["created_at"].isoformat(), "updated_at": r["updated_at"].isoformat(),
                "metadata": json.loads(r["metadata"]) if r["metadata"] else {},
            }
            _add_ttl_warning(item, r["updated_at"])
            results.append(item)

        verification_required = [
            {
                "memory_id": str(r["id"]),
                "content": r["content"],
                "category_path": r["category_path"],
                "verify_after": r["verify_after"].isoformat(),
                "volatility_class": (json.loads(r["metadata"]) if r["metadata"] else {}).get("volatility_class", "low"),
            }
            for r in expired_rows
        ]

        verification_block = ""
        if verification_required:
            lines = ["## Verification Required", ""]
            lines.append(
                "The following records have passed their verification deadline. "
                "Query the user regarding the accuracy of each BEFORE executing any other commands."
            )
            for v in verification_required:
                lines.append(f"\n- **Memory ID**: `{v['memory_id']}`")
                lines.append(f"  **Category**: {v['category_path']}")
                lines.append(f"  **Content**: {v['content'][:300]}{'...' if len(v['content']) > 300 else ''}")
                lines.append(f"  **Verify after**: {v['verify_after']}")
            lines.append("")
            lines.append("If the user confirms unchanged → call `confirm_memory_validity(memory_id)`.")
            lines.append("If the user provides updated info → call `memorize_context(new_text)`.")
            verification_block = "\n".join(lines)

        logger.info(
            "initialize_context retrieved %d system records, %d requiring verification.",
            len(results), len(verification_required),
        )
        return {
            "ok": True,
            "results": results,
            "verification_required": verification_required,
            "verification_block": verification_block,
        }
    except Exception as e:
        logger.error("Error in initialize_context: %s\n%s", e, traceback.format_exc())
        return {"ok": False, "error": str(e)}


async def trace_history(ctx: Context, memory_id: str) -> dict[str, Any]:
    """
    Trace the full supersession chain for a memory node using a backward-facing
    recursive CTE. Returns all predecessor versions ordered oldest → newest,
    revealing the temporal evolution of a fact.
    """
    logger.info("Tool invoked: trace_history (memory_id: %s)", memory_id)
    try:
        target_id = UUID(memory_id)
    except Exception:
        return {"ok": False, "error": "memory_id must be a valid UUID"}

    try:
        db_pool = get_pool()
        async with db_pool.acquire() as conn:
            rows = await conn.fetch(
                """
                WITH RECURSIVE history AS (
                    SELECT id, content, supersedes_id, created_at, updated_at, 0 AS generation
                    FROM memories
                    WHERE id = $1
                  UNION ALL
                    SELECT m.id, m.content, m.supersedes_id, m.created_at, m.updated_at, h.generation + 1
                    FROM memories m
                    JOIN history h ON m.supersedes_id = h.id
                    WHERE h.generation < 100
                )
                SELECT id, content, supersedes_id, created_at, updated_at, generation
                FROM history
                ORDER BY created_at ASC
                """,
                target_id,
            )

        if not rows:
            return {"ok": False, "error": f"Memory {memory_id} not found."}

        chain = [
            {
                "id": str(r["id"]),
                "content": r["content"],
                "superseded_by": str(r["supersedes_id"]) if r["supersedes_id"] else None,
                "created_at": r["created_at"].isoformat(),
                "updated_at": r["updated_at"].isoformat(),
                "generation": r["generation"],
            }
            for r in rows
        ]
        logger.info("trace_history found %d versions for memory %s", len(chain), memory_id)
        return {"ok": True, "memory_id": memory_id, "version_count": len(chain), "chain": chain}
    except Exception as e:
        logger.error("Error in trace_history: %s\n%s", e, traceback.format_exc())
        return {"ok": False, "error": str(e)}


async def confirm_memory_validity(ctx: Context, memory_id: str) -> dict[str, Any]:
    """
    Confirm that an expired memory is still accurate. Recalculates and advances
    verify_after based on the record's volatility_class without altering its
    content, category, or history. Call this when the user confirms existing
    information is still correct after a verification prompt.
    """
    logger.info("Tool invoked: confirm_memory_validity (memory_id: %s)", memory_id)
    try:
        target_id = UUID(memory_id)
    except Exception:
        return {"ok": False, "error": "memory_id must be a valid UUID"}

    try:
        db_pool = get_pool()
        async with db_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id, metadata::text FROM memories "
                "WHERE id = $1 AND supersedes_id IS NULL AND archived_at IS NULL",
                target_id,
            )
            if not row:
                return {"ok": False, "error": f"Memory {memory_id} not found, is superseded, or is archived."}

            metadata = json.loads(row["metadata"]) if row["metadata"] else {}
            volatility_class = metadata.get("volatility_class", "low")
            now = _now()
            new_verify_after = _compute_verify_after(volatility_class, now)

            await conn.execute(
                "UPDATE memories SET verify_after = $1, updated_at = $2 WHERE id = $3",
                new_verify_after, now, target_id,
            )

        logger.info(
            "confirm_memory_validity: memory %s confirmed, next verify_after=%s (%s)",
            memory_id, new_verify_after, volatility_class,
        )
        return {
            "ok": True,
            "memory_id": memory_id,
            "volatility_class": volatility_class,
            "next_verify_after": new_verify_after.isoformat() if new_verify_after else None,
        }
    except Exception as e:
        logger.error("Error in confirm_memory_validity: %s\n%s", e, traceback.format_exc())
        return {"ok": False, "error": str(e)}


async def synthesize_system_primer(conn, profile_changed: bool = False) -> None:
    """Deterministically build a System Primer from SQL aggregation. No LLM calls."""
    from llm import summarize_user_profile
    logger.info("Synthesizing system primer...")
    try:
        if profile_changed:
            chunks = await db.get_profile_chunks(conn)
            user_context = await summarize_user_profile(chunks)
            await db.set_cached_user_context(conn, user_context)
        else:
            user_context = await db.get_cached_user_context(conn)
            if user_context is None:
                # Cache miss — generate unconditionally
                chunks = await db.get_profile_chunks(conn)
                user_context = await summarize_user_profile(chunks)
                await db.set_cached_user_context(conn, user_context)

        cat_rows = await conn.fetch(
            """
            SELECT category_path::text AS category, COUNT(*) AS count
            FROM memories
            WHERE supersedes_id IS NULL
              AND archived_at IS NULL
              AND category_path != 'reference.system.primer'::ltree
            GROUP BY category_path ORDER BY category_path ASC
            """
        )
        total_memories = sum(int(r["count"]) for r in cat_rows)
        taxonomy_tree = _build_taxonomy_tree(cat_rows, max_depth=2, max_branch_nodes=50)

        primer_content = (
            f"# System Primer\n\n"
            f"Knowledge base contains {total_memories} active memories "
            f"across {len(cat_rows)} categories.\n\n"
            f"## User Context\n{user_context}\n\n"
            f"## Taxonomy\n"
            f"```\n{taxonomy_tree}\n```\n\n"
            f"## Verification Protocol\n"
            f"When `initialize_context` returns a non-empty `verification_block`, inject it under "
            f"## Verification Required and address EACH item BEFORE any other task:\n"
            f"1. Quote the memory content to the user and ask if it is still accurate.\n"
            f"2. User confirms unchanged → call `confirm_memory_validity(memory_id)`.\n"
            f"3. User provides updated info → call `memorize_context(new_text)` to run "
            f"the standard contradiction engine and supersede the stale record.\n\n"
            f"## Context Store Guide\n"
            f"Separate from long-term memory. Use for ephemeral, session-scoped working data.\n"
            f"- `set_context(key, value, ttl_hours, scope)` — write active state (plans, task context, summaries)\n"
            f"- `get_context(key)` — retrieve by exact key\n"
            f"- `list_context_keys(scope?)` — see what's currently active\n"
            f"- `delete_context(key)` — explicitly clear when done\n"
            f"- `extend_context_ttl(key, hours)` — push expiry forward if needed\n\n"
            f"**When to use context store vs memorize_context:**\n"
            f"- Use context store for: active plans, current task state, session summaries, anything that will be stale in < 7 days\n"
            f"- Use memorize_context for: facts about you, project decisions, architecture notes, anything that should persist long-term\n"
            f"- Default TTL: 24 hours. Plans/tasks: 72 hours. Never exceed 168 hours (1 week) for working context.\n\n"
            f"## Retrieval Guide\n"
            f"- `search_memory(query)` — hybrid semantic + keyword search, returns top 10\n"
            f"- `search_memory(query, category_path='projects.myapp.planning')` — scoped to subtree\n"
            f"- `list_categories()` — all paths with counts\n"
            f"- `fetch_document(memory_id)` — reconstruct full document from chunk chain\n"
            f"- `trace_history(memory_id)` — inspect supersession chain for a fact\n"
            f"- `explore_taxonomy(path)` — expand a collapsed '[+N more]' branch\n"
            f"- `check_ingestion_status(job_id)` — poll async ingestion progress\n"
            f"- `confirm_memory_validity(memory_id)` — confirm an expired record is still accurate; advances verify_after\n"
            f"- `initialize_context()` — returns this primer\n"
        )

        primer_id = generate_deterministic_id(primer_content)
        vec = await embed(primer_content)
        vec_lit = _vector_literal(vec)
        now = _now()

        async with conn.transaction():
            old_primers = await conn.fetch(
                """
                SELECT id FROM memories
                WHERE category_path = 'reference.system.primer'
                  AND supersedes_id IS NULL
                  AND archived_at IS NULL
                """
            )
            for old_primer in old_primers:
                if old_primer["id"] == primer_id:
                    continue
                await conn.execute(
                    "UPDATE memories SET supersedes_id = $1, updated_at = $2 WHERE id = $3",
                    primer_id, now, old_primer["id"],
                )

            await conn.execute(
                """
                INSERT INTO memories (id, content, embedding, category_path, metadata, created_at, updated_at, last_accessed_at)
                VALUES ($1, $2, $3::vector, 'reference.system.primer'::ltree, '{}'::jsonb, $4, $4, $4)
                ON CONFLICT (id) DO UPDATE SET content = EXCLUDED.content, updated_at = EXCLUDED.updated_at
                """,
                primer_id, primer_content, vec_lit, now,
            )

        db.primer_last_updated = now
        logger.info("Primer synthesized (%d chars, %d categories).", len(primer_content), len(cat_rows))
    except Exception as e:
        logger.error("Error in synthesize_system_primer: %s\n%s", e, traceback.format_exc())
