"""Admin and maintenance tools."""

import json
import traceback
from typing import Optional, Any

from fastmcp import Context

from config import logger
from utils import sanitize_ltree_path
from db import get_pool
import db


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
