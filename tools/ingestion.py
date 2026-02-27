"""Fire-and-forget ingestion tools."""

import traceback
from typing import Optional, Any
from uuid import UUID

from fastmcp import Context

from config import logger, MAX_MEMORIZE_TEXT_LENGTH
from db import get_pool


async def memorize_context(ctx: Context, text: str, ttl_days: Optional[int] = None) -> dict[str, Any]:
    """
    Enqueue text for autonomous ingestion. Returns a job_id immediately.
    The system will chunk, categorize, deduplicate, and merge the content
    in the background. Use check_ingestion_status(job_id) to poll progress.
    """
    logger.info("Tool invoked: memorize_context (text length: %d)", len(text) if text else 0)
    if not text or not isinstance(text, str) or not text.strip():
        return {"ok": False, "error": "text must be a non-empty string"}

    if len(text) > MAX_MEMORIZE_TEXT_LENGTH:
        return {"ok": False, "error": f"text exceeds maximum allowed length of {MAX_MEMORIZE_TEXT_LENGTH} characters"}

    if ttl_days is not None and (not isinstance(ttl_days, int) or ttl_days < 1):
        return {"ok": False, "error": "ttl_days must be a positive integer"}

    try:
        db_pool = get_pool()
        async with db_pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO ingestion_staging (raw_text, ttl_days)
                VALUES ($1, $2)
                RETURNING job_id
                """,
                text, ttl_days,
            )
        job_id = str(row["job_id"])
        logger.info("memorize_context enqueued job %s", job_id)
        return {
            "ok": True,
            "job_id": job_id,
            "message": "Ingestion enqueued. Poll check_ingestion_status(job_id) for progress.",
        }
    except Exception as e:
        logger.error("Error in memorize_context: %s\n%s", e, traceback.format_exc())
        return {"ok": False, "error": str(e)}


async def check_ingestion_status(ctx: Context, job_id: str) -> dict[str, Any]:
    """Check the processing status of a memorize_context job. Returns pending, processing, complete, or failed."""
    logger.info("Tool invoked: check_ingestion_status (job_id: %s)", job_id)
    try:
        parsed_id = UUID(job_id)
    except Exception:
        return {"ok": False, "error": "job_id must be a valid UUID"}

    try:
        db_pool = get_pool()
        async with db_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT job_id, status, error, created_at FROM ingestion_staging WHERE job_id = $1",
                parsed_id,
            )
        if not row:
            return {"ok": False, "error": f"Job {job_id} not found."}
        return {
            "ok": True,
            "job_id": str(row["job_id"]),
            "status": row["status"],
            "error": row["error"],
            "created_at": row["created_at"].isoformat(),
        }
    except Exception as e:
        logger.error("Error in check_ingestion_status: %s\n%s", e, traceback.format_exc())
        return {"ok": False, "error": str(e)}
