"""CRUD and taxonomy management tools."""

import json
import traceback
import asyncio
from typing import Any
from uuid import UUID

from fastmcp import Context

from config import logger
from utils import _now, _vector_literal, sanitize_ltree_path
from llm import embed
from db import get_pool, _compute_verify_after

from .context import synthesize_system_primer


async def delete_memory(ctx: Context, id: str) -> dict[str, Any]:
    """Delete a memory by its ID. Linked edges will cascade delete if properly constrained."""
    logger.info("Tool invoked: delete_memory (id: %s)", id)
    try:
        memory_id = UUID(id)
    except Exception:
        return {"ok": False, "error": "id must be a valid UUID"}

    try:
        db_pool = get_pool()
        async with db_pool.acquire() as conn:
            from db import is_profile_path
            row = await conn.fetchrow("SELECT category_path::text FROM memories WHERE id = $1", memory_id)
            profile_changed = is_profile_path(row["category_path"]) if row else False

            result = await conn.execute(
                """
                WITH RECURSIVE backward AS (
                    SELECT id FROM memories WHERE id = $1
                    UNION
                    SELECT e.source_id FROM memory_edges e
                    INNER JOIN backward b ON b.id = e.target_id
                    WHERE e.relation_type = 'sequence_next'
                ),
                forward AS (
                    SELECT id FROM memories WHERE id = $1
                    UNION
                    SELECT e.target_id FROM memory_edges e
                    INNER JOIN forward f ON f.id = e.source_id
                    WHERE e.relation_type = 'sequence_next'
                ),
                chunk_chain AS (
                    SELECT id FROM backward
                    UNION
                    SELECT id FROM forward
                )
                DELETE FROM memories m USING chunk_chain c WHERE m.id = c.id
                """,
                memory_id,
            )
        deleted_count = int(result.split()[-1]) if result.startswith("DELETE") else 0
        deleted = deleted_count > 0

        if deleted:
            logger.info("Successfully deleted memory ID: %s (%d related chunks)", memory_id, deleted_count)
            async with db_pool.acquire() as primer_conn:
                await synthesize_system_primer(primer_conn, profile_changed=profile_changed)
        else:
            logger.warning("delete_memory yielded no results. Memory %s was not found.", memory_id)

        return {"ok": True, "deleted": deleted, "id": str(memory_id)}
    except Exception as e:
        logger.error("Error in delete_memory: %s\n%s", e, traceback.format_exc())
        return {"ok": False, "error": str(e)}


async def update_memory(ctx: Context, id: str, new_content: str) -> dict[str, Any]:
    """Update the content of an existing memory in-place by ID.
    Re-embeds the new text but preserves the record's identity, category_path, edges, and created_at.
    Use this instead of delete + re-ingest when correcting or refreshing a known record."""
    logger.info("Tool invoked: update_memory (id: %s, content length: %d)", id, len(new_content) if new_content else 0)
    try:
        memory_id = UUID(id)
    except Exception:
        return {"ok": False, "error": "id must be a valid UUID"}

    if not new_content or not isinstance(new_content, str) or not new_content.strip():
        return {"ok": False, "error": "new_content must be a non-empty string"}

    try:
        db_pool = get_pool()
        async with db_pool.acquire() as conn:
            existing = await conn.fetchrow(
                "SELECT id, category_path::text, metadata::text FROM memories WHERE id = $1", memory_id
            )
            if not existing:
                return {"ok": False, "error": f"Memory {id} not found."}

            metadata = json.loads(existing["metadata"]) if existing["metadata"] else {}
            volatility_class = metadata.get("volatility_class", "low")

            vec = await embed(new_content)
            vec_lit = _vector_literal(vec)
            now = _now()
            new_verify_after = _compute_verify_after(volatility_class, now)

            await conn.execute(
                """
                UPDATE memories
                SET content = $1,
                    embedding = $2::vector,
                    lexical_search = to_tsvector('english', $1),
                    updated_at = $3,
                    verify_after = $5
                WHERE id = $4
                """,
                new_content, vec_lit, now, memory_id, new_verify_after,
            )

        logger.info("Successfully updated memory %s in-place.", memory_id)
        from db import is_profile_path
        profile_changed = is_profile_path(existing["category_path"])
        async with db_pool.acquire() as primer_conn:
            await synthesize_system_primer(primer_conn, profile_changed=profile_changed)
        return {
            "ok": True,
            "id": str(memory_id),
            "category_path": existing["category_path"],
            "message": "Memory updated in-place. Edges, category, and history preserved.",
        }
    except Exception as e:
        logger.error("Error in update_memory: %s\n%s", e, traceback.format_exc())
        return {"ok": False, "error": str(e)}


async def update_memory_metadata(ctx: Context, id: str, metadata: dict[str, Any]) -> dict[str, Any]:
    """Merge new key/value pairs into a memory's metadata without changing its content or category.
    Use this to set ttl_days, add tags, or annotate existing memories.
    Existing metadata keys not present in the update are preserved."""
    logger.info("Tool invoked: update_memory_metadata (id: %s)", id)
    try:
        memory_id = UUID(id)
    except Exception:
        return {"ok": False, "error": "id must be a valid UUID"}

    ttl_days = metadata.get("ttl_days")
    if ttl_days is not None and (not isinstance(ttl_days, int) or ttl_days < 1):
        return {"ok": False, "error": "ttl_days must be a positive integer"}

    try:
        db_pool = get_pool()
        async with db_pool.acquire() as conn:
            row = await conn.fetchrow(
                "UPDATE memories SET metadata = metadata || $1::jsonb, updated_at = $2 "
                "WHERE id = $3 AND supersedes_id IS NULL AND archived_at IS NULL "
                "RETURNING id, metadata::text",
                json.dumps(metadata), _now(), memory_id,
            )
        if not row:
            return {"ok": False, "error": f"Memory {id} not found, is superseded, or is archived."}
        logger.info("Successfully updated metadata for memory %s.", memory_id)
        return {"ok": True, "id": str(memory_id), "metadata": json.loads(row["metadata"])}
    except Exception as e:
        logger.error("Error in update_memory_metadata: %s\n%s", e, traceback.format_exc())
        return {"ok": False, "error": str(e)}


async def recategorize_memory(ctx: Context, id: str, new_category_path: str) -> dict[str, Any]:
    """Fix or update the category path of a specific memory by ID. Use this when a memory is miscategorized."""
    logger.info("Tool invoked: recategorize_memory (id: %s, new_path: %s)", id, new_category_path)
    try:
        memory_id = UUID(id)
        safe_path = sanitize_ltree_path(new_category_path)
    except Exception:
        return {"ok": False, "error": "Invalid UUID or category path format."}

    try:
        db_pool = get_pool()
        async with db_pool.acquire() as conn:
            existing = await conn.fetchrow(
                "SELECT category_path::text FROM memories WHERE id = $1", memory_id
            )
            if not existing:
                return {"ok": False, "error": "Memory not found."}
            if existing["category_path"] == "reference.system.primer":
                return {"ok": False, "error": "Cannot recategorize the System Primer; it must stay at reference.system.primer."}
            row = await conn.fetchrow(
                "UPDATE memories SET category_path = $1::ltree, updated_at = $2 WHERE id = $3 RETURNING id",
                safe_path, _now(), memory_id,
            )
        if not row:
            return {"ok": False, "error": "Memory not found."}
        async with db_pool.acquire() as primer_conn:
            await synthesize_system_primer(primer_conn, profile_changed=True)
        return {"ok": True, "id": str(memory_id), "new_category_path": safe_path}
    except Exception as e:
        logger.error("Error in recategorize_memory: %s\n%s", e, traceback.format_exc())
        return {"ok": False, "error": str(e)}


async def bulk_move_category(ctx: Context, old_path_prefix: str, new_path_prefix: str) -> dict[str, Any]:
    """Move all memories from an old taxonomy branch to a new one. Example: old_path='software.web', new_path='projects.myapp.backend'"""
    logger.info("Tool invoked: bulk_move_category (%s -> %s)", old_path_prefix, new_path_prefix)
    safe_old = sanitize_ltree_path(old_path_prefix)
    safe_new = sanitize_ltree_path(new_path_prefix)
    try:
        db_pool = get_pool()
        async with db_pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, category_path::text FROM memories "
                "WHERE category_path <@ $1::ltree AND supersedes_id IS NULL AND archived_at IS NULL "
                "AND category_path != 'reference.system.primer'::ltree",
                safe_old,
            )
            updated_count = 0
            for r in rows:
                old_full = r["category_path"]
                suffix = old_full[len(safe_old):]
                new_full = sanitize_ltree_path(safe_new + suffix)
                await conn.execute(
                    "UPDATE memories SET category_path = $1::ltree, updated_at = $2 WHERE id = $3",
                    new_full, _now(), r["id"],
                )
                updated_count += 1

        if updated_count > 0:
            async with db_pool.acquire() as primer_conn:
                await synthesize_system_primer(primer_conn, profile_changed=True)

        return {
            "ok": True,
            "updated_count": updated_count,
            "message": f"Moved {updated_count} active records to {safe_new}.*",
        }
    except Exception as e:
        logger.error("Error in bulk_move_category: %s\n%s", e, traceback.format_exc())
        return {"ok": False, "error": str(e)}
