"""Admin and maintenance tools."""

import json
import traceback
from typing import Optional, Any

from fastmcp import Context

from config import logger
from utils import sanitize_ltree_path
from db import get_pool
import db


def _excerpt(text: Optional[str], max_len: int = 220) -> Optional[str]:
    if not text:
        return None
    compact = " ".join(text.split())
    if len(compact) <= max_len:
        return compact
    return compact[: max_len - 3] + "..."


async def prune_history(ctx: Context, days_old: int) -> dict[str, Any]:
    """Execute a batch DELETE for records where supersedes_id IS NOT NULL and updated_at is older than the given days."""
    logger.info("Tool invoked: prune_history (days_old: %s)", days_old)
    try:
        days = int(days_old)
    except ValueError:
        return {"ok": False, "error": "days_old must be an integer"}

    try:
        db_pool = get_pool()
        async with db_pool.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM memories WHERE supersedes_id IS NOT NULL AND updated_at < NOW() - INTERVAL '1 day' * $1",
                days,
            )
        deleted_count = int(result.split()[-1]) if result.startswith("DELETE") else 0
        return {"ok": True, "deleted_count": deleted_count}
    except Exception as e:
        return {"ok": False, "error": str(e)}


async def export_memories(ctx: Context, category_path: Optional[str] = None) -> dict[str, Any]:
    """Export all active (non-superseded/non-archived) memories to a portable list.
    Optional 'category_path' filters the export to a specific branch."""
    logger.info("Tool invoked: export_memories (category_path: %s)", category_path)
    try:
        db_pool = get_pool()
        async with db_pool.acquire() as conn:
            query = (
                "SELECT id, content, category_path::text, metadata::text, created_at "
                "FROM memories WHERE supersedes_id IS NULL AND archived_at IS NULL"
            )
            params: list[Any] = []
            if category_path and category_path.strip():
                query += " AND category_path <@ $1::ltree"
                params.append(sanitize_ltree_path(category_path.strip()))

            rows = await conn.fetch(query + " ORDER BY category_path ASC", *params)

        memories = [
            {
                "id": str(r["id"]),
                "content": r["content"],
                "category_path": r["category_path"],
                "metadata": json.loads(r["metadata"]) if r["metadata"] else {},
                "created_at": r["created_at"].isoformat(),
            }
            for r in rows
        ]
        logger.info("export_memories exported %d memories", len(memories))
        return {"ok": True, "count": len(memories), "memories": memories}
    except Exception as e:
        logger.error("Error in export_memories: %s\n%s", e, traceback.format_exc())
        return {"ok": False, "error": str(e)}


async def run_diagnostics(ctx: Optional[Context] = None) -> dict[str, Any]:
    """Run system diagnostics: DB pool stats, ingestion counts, memory stats, etc."""
    logger.info("Tool invoked: run_diagnostics")
    try:
        db_pool = get_pool()

        pool_stats = {
            "size": db_pool.get_size(),
            "idle": db_pool.get_idle_size(),
        }

        async with db_pool.acquire() as conn:
            ingestion_stats = await conn.fetchrow(
                "SELECT COUNT(*) FILTER (WHERE status='pending') as pending, "
                "COUNT(*) FILTER (WHERE status='processing') as processing, "
                "COUNT(*) FILTER (WHERE status='failed') as failed "
                "FROM ingestion_staging"
            )

            expired_count = await conn.fetchval(
                "SELECT COUNT(*) FROM memories WHERE verify_after < NOW() AND supersedes_id IS NULL AND archived_at IS NULL"
            )

            archived_count = await conn.fetchval(
                "SELECT COUNT(*) FROM memories WHERE archived_at IS NOT NULL"
            )

            primer_ts = db.primer_last_updated.isoformat() if db.primer_last_updated else None

            violations = await conn.fetchval(
                "SELECT COUNT(*) FROM memories WHERE NOT (category_path <@ 'profile'::ltree OR category_path <@ 'projects'::ltree OR category_path <@ 'organizations'::ltree OR category_path <@ 'concepts'::ltree OR category_path <@ 'reference'::ltree) AND supersedes_id IS NULL AND archived_at IS NULL"
            )

        return {
            "ok": True,
            "pool_stats": pool_stats,
            "ingestion": dict(ingestion_stats),
            "expired_memories": expired_count,
            "archived_memories": archived_count,
            "primer_last_updated": primer_ts,
            "l1_root_violations": violations,
        }
    except Exception as e:
        logger.error("Error in run_diagnostics: %s\n%s", e, traceback.format_exc())
        return {"ok": False, "error": str(e)}


async def get_ingestion_stats(ctx: Optional[Context] = None) -> dict[str, Any]:
    """Get counts by status, oldest pending age, last 5 failed jobs."""
    logger.info("Tool invoked: get_ingestion_stats")
    try:
        db_pool = get_pool()
        async with db_pool.acquire() as conn:
            counts = await conn.fetch(
                "SELECT status, COUNT(*) FROM ingestion_staging GROUP BY status"
            )
            counts_dict = {r["status"]: r["count"] for r in counts}

            oldest = await conn.fetchval(
                "SELECT extract(epoch from (NOW() - created_at)) FROM ingestion_staging WHERE status = 'pending' ORDER BY created_at ASC LIMIT 1"
            )

            failed_jobs = await conn.fetch(
                "SELECT job_id, error, created_at FROM ingestion_staging WHERE status = 'failed' ORDER BY created_at DESC LIMIT 5"
            )

        return {
            "ok": True,
            "counts": counts_dict,
            "oldest_pending_age_seconds": oldest,
            "last_failed": [dict(r) for r in failed_jobs]
        }
    except Exception as e:
        logger.error("Error in get_ingestion_stats: %s\n%s", e, traceback.format_exc())
        return {"ok": False, "error": str(e)}


async def flush_staging(ctx: Optional[Context] = None, days_old: int = 7) -> dict[str, Any]:
    """DELETE complete/failed staging older than N days (default 7)."""
    logger.info("Tool invoked: flush_staging (days_old: %s)", days_old)
    try:
        db_pool = get_pool()
        async with db_pool.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM ingestion_staging WHERE status IN ('complete', 'failed') AND created_at < NOW() - INTERVAL '1 day' * $1",
                days_old
            )
        deleted_count = int(result.split()[-1]) if result.startswith("DELETE") else 0
        return {"ok": True, "deleted_count": deleted_count}
    except Exception as e:
        logger.error("Error in flush_staging: %s\n%s", e, traceback.format_exc())
        return {"ok": False, "error": str(e)}


async def contradiction_audit(
    ctx: Context,
    limit: int = 25,
    category_path: Optional[str] = None,
    resolution: Optional[str] = None,
    since_days: Optional[int] = None,
) -> dict[str, Any]:
    """
    Query recent contradiction-resolution audit events (newest first).

    Filters:
    - category_path subtree (ltree)
    - resolution in {'supersedes', 'merges'}
    - created_at within the last N days
    """
    logger.info(
        "Tool invoked: contradiction_audit (limit=%s, category_path=%s, resolution=%s, since_days=%s)",
        limit, category_path, resolution, since_days,
    )

    try:
        raw_limit = 25 if limit is None else int(limit)
        safe_limit = max(1, min(raw_limit, 100))
    except Exception:
        return {"ok": False, "error": "limit must be an integer"}

    safe_resolution = None
    if resolution is not None:
        safe_resolution = str(resolution).strip().lower()
        if safe_resolution not in ("supersedes", "merges"):
            return {"ok": False, "error": "resolution must be either 'supersedes' or 'merges'"}

    safe_since_days: Optional[int] = None
    if since_days is not None:
        try:
            safe_since_days = int(since_days)
        except Exception:
            return {"ok": False, "error": "since_days must be an integer"}
        if safe_since_days < 1:
            return {"ok": False, "error": "since_days must be >= 1"}

    try:
        where = ["TRUE"]
        params: list[Any] = []

        if category_path and category_path.strip():
            params.append(sanitize_ltree_path(category_path.strip()))
            where.append(f"e.category_path <@ ${len(params)}::ltree")

        if safe_resolution:
            params.append(safe_resolution)
            where.append(f"e.resolution = ${len(params)}")

        if safe_since_days is not None:
            params.append(safe_since_days)
            where.append(f"e.created_at >= NOW() - (${len(params)}::int * INTERVAL '1 day')")

        params.append(safe_limit)
        limit_param = len(params)

        query = f"""
            SELECT
                e.id,
                e.created_at,
                e.new_memory_id,
                e.old_memory_id,
                e.resolution,
                e.similarity,
                e.category_path::text AS category_path,
                e.details::text AS details,
                new_m.content AS new_content,
                old_m.content AS old_content
            FROM conflict_audit_events e
            LEFT JOIN memories new_m ON new_m.id = e.new_memory_id
            LEFT JOIN memories old_m ON old_m.id = e.old_memory_id
            WHERE {' AND '.join(where)}
            ORDER BY e.created_at DESC
            LIMIT ${limit_param}
        """

        db_pool = get_pool()
        async with db_pool.acquire() as conn:
            rows = await conn.fetch(query, *params)

        events = [
            {
                "id": str(r["id"]),
                "created_at": r["created_at"].isoformat(),
                "resolution": r["resolution"],
                "similarity": round(float(r["similarity"]), 6) if r["similarity"] is not None else None,
                "category_path": r["category_path"],
                "new_memory": {
                    "id": str(r["new_memory_id"]) if r["new_memory_id"] else None,
                    "excerpt": _excerpt(r["new_content"]),
                },
                "old_memory": {
                    "id": str(r["old_memory_id"]) if r["old_memory_id"] else None,
                    "excerpt": _excerpt(r["old_content"]),
                },
                "details": json.loads(r["details"]) if r["details"] else {},
            }
            for r in rows
        ]
        return {"ok": True, "count": len(events), "events": events}
    except Exception as e:
        logger.error("Error in contradiction_audit: %s\n%s", e, traceback.format_exc())
        return {"ok": False, "error": str(e)}
