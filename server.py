import asyncio
import traceback
import asyncpg

from config import logger, DATABASE_URL, PG_POOL_MIN, PG_POOL_MAX, STAGING_RETENTION_DAYS, PRODUCTION_PORT, ADMIN_PORT, API_KEY
from utils import _now
import db
from db import init_db
from tools.production import mcp as production_mcp
from tools.admin import mcp as admin_mcp
from tools.context import synthesize_system_primer

from contextlib import asynccontextmanager
from starlette.middleware import Middleware
from auth import BearerTokenMiddleware


async def _ingestion_worker() -> None:
    """
    Fire-and-forget background processor for the ingestion_staging queue.
    Polls every 2 s, claims one pending job atomically, runs full ingestion,
    then marks the job complete or failed.
    """
    logger.info("Ingestion worker started.")

    # Reset any jobs orphaned by a previous crash.
    try:
        p = db.get_pool()
        async with p.acquire() as conn:
            reset = await conn.execute(
                "UPDATE ingestion_staging SET status = 'pending' WHERE status = 'processing'"
            )
            if reset != "UPDATE 0":
                logger.warning("Ingestion worker reset stale processing jobs: %s", reset)
    except Exception as e:
        logger.warning("Could not reset stale ingestion jobs on startup: %s", e)

    # Main polling loop — must be outside the startup try/except above.
    while True:
        try:
            p = db.get_pool()

            # Atomically claim one pending job.
            async with p.acquire() as claim_conn:
                job = await claim_conn.fetchrow(
                    """
                    UPDATE ingestion_staging SET status = 'processing'
                    WHERE job_id = (
                        SELECT job_id FROM ingestion_staging
                        WHERE status = 'pending'
                        ORDER BY created_at ASC
                        LIMIT 1
                        FOR UPDATE SKIP LOCKED
                    )
                    RETURNING job_id, raw_text, ttl_days
                    """
                )

            if not job:
                await asyncio.sleep(2)
                continue

            job_id = job["job_id"]
            logger.info("Ingestion worker processing job %s (text_len=%d)", job_id, len(job["raw_text"]))

            try:
                async with p.acquire() as proc_conn:
                    await db.process_and_insert_memory(
                        job["raw_text"], proc_conn, job["ttl_days"]
                    )

                async with p.acquire() as status_conn:
                    await status_conn.execute(
                        "UPDATE ingestion_staging SET status = 'complete' WHERE job_id = $1",
                        job_id,
                    )

                logger.info("Ingestion worker completed job %s", job_id)

            except Exception as job_exc:
                err_msg = str(job_exc)[:1000]
                logger.error("Ingestion job %s failed: %s\n%s", job_id, job_exc, traceback.format_exc())
                try:
                    async with p.acquire() as err_conn:
                        await err_conn.execute(
                            "UPDATE ingestion_staging SET status = 'failed', error = $1 WHERE job_id = $2",
                            err_msg, job_id,
                        )
                except Exception as update_exc:
                    logger.error("Could not mark job %s as failed: %s", job_id, update_exc)

        except Exception as outer_exc:
            logger.error("Ingestion worker outer error: %s\n%s", outer_exc, traceback.format_exc())
            await asyncio.sleep(5)


async def _ttl_daemon() -> None:
    """
    Hourly autonomous maintenance daemon.
    Soft-deletes (archives) memories whose TTL has elapsed, then hard-deletes
    records that have been archived for more than 30 days.
    """
    logger.info("TTL daemon started.")
    while True:
        await asyncio.sleep(3600)
        try:
            p = db.get_pool()
            async with p.acquire() as conn:
                soft_result = await conn.execute(
                    """
                    UPDATE memories
                    SET archived_at = NOW()
                    WHERE archived_at IS NULL
                      AND metadata->>'ttl_days' IS NOT NULL
                      AND NOW() > updated_at + (metadata->>'ttl_days')::int * INTERVAL '1 day'
                    """
                )
                soft_count = int(soft_result.split()[-1]) if soft_result.startswith("UPDATE") else 0

                hard_result = await conn.execute(
                    "DELETE FROM memories WHERE archived_at IS NOT NULL AND archived_at < NOW() - INTERVAL '30 days'"
                )
                hard_count = int(hard_result.split()[-1]) if hard_result.startswith("DELETE") else 0

                staging_result = await conn.execute(
                    "DELETE FROM ingestion_staging WHERE status IN ('complete','failed') AND created_at < NOW() - INTERVAL '1 day' * $1",
                    STAGING_RETENTION_DAYS
                )
                staging_count = int(staging_result.split()[-1]) if staging_result.startswith("DELETE") else 0

                context_result = await conn.execute(
                    "DELETE FROM context_store WHERE expires_at < NOW()"
                )
                context_count = int(context_result.split()[-1]) if context_result.startswith("DELETE") else 0

                if context_count:
                    logger.info("TTL daemon: deleted %d expired context entries.", context_count)

                if soft_count or hard_count or staging_count:
                    logger.info("TTL daemon: soft-archived %d, hard-deleted %d, cleared %d staging records.", soft_count, hard_count, staging_count)
                    await synthesize_system_primer(conn, profile_changed=True)
                elif not context_count:
                    logger.debug("TTL daemon: no expired records found.")

        except Exception as e:
            logger.error("TTL daemon error: %s\n%s", e, traceback.format_exc())


@asynccontextmanager
async def server_lifespan(server):
    logger.info("Starting up FastMCP application...")
    try:
        db.pool = await asyncpg.create_pool(
            DATABASE_URL,
            min_size=PG_POOL_MIN,
            max_size=PG_POOL_MAX,
            command_timeout=30,
        )
        logger.info("Successfully established connection pool to Database.")
        async with db.pool.acquire() as conn:
            await init_db(conn)
            row = await conn.fetchrow(
                "SELECT updated_at FROM memories "
                "WHERE category_path = 'reference.system.primer' "
                "AND supersedes_id IS NULL AND archived_at IS NULL"
            )
            db.primer_last_updated = row["updated_at"] if row else _now()
        
        async def _bg_primer():
            async with db.pool.acquire() as bg_conn:
                await synthesize_system_primer(bg_conn, profile_changed=False)
        asyncio.create_task(_bg_primer())
    except Exception as e:
        logger.error("Failed during startup: %s\n%s", e, traceback.format_exc())
        raise

    ingestion_task = asyncio.create_task(_ingestion_worker())
    ttl_task = asyncio.create_task(_ttl_daemon())

    try:
        yield {}
    finally:
        logger.info("Shutting down... cancelling background workers.")
        ingestion_task.cancel()
        ttl_task.cancel()
        try:
            await asyncio.gather(ingestion_task, ttl_task, return_exceptions=True)
        except Exception:
            pass
        logger.info("Closing connection pool.")
        if db.pool:
            await db.pool.close()
            db.pool = None
            logger.info("Connection pool closed.")


if __name__ == "__main__":
    logger.info("Starting memory-mcp dual FastMCP servers")
    
    @asynccontextmanager
    async def dummy_lifespan(server):
        yield {}
        
    async def run_servers():
        production_mcp._lifespan = server_lifespan
        admin_mcp._lifespan = dummy_lifespan
        
        prod_middleware = None
        if API_KEY:
            prod_middleware = [Middleware(BearerTokenMiddleware, api_key=API_KEY)]
            logger.info("Bearer token auth enabled on production server.")
        else:
            logger.info("No API_KEY set — production server running without auth (WireGuard-trusted mode).")

        logger.info(f"Production server listening on 0.0.0.0:{PRODUCTION_PORT}")
        logger.info(f"Admin server listening on 0.0.0.0:{ADMIN_PORT}")

        await asyncio.gather(
            production_mcp.run_http_async(host="0.0.0.0", port=PRODUCTION_PORT, middleware=prod_middleware),
            admin_mcp.run_http_async(host="0.0.0.0", port=ADMIN_PORT),
        )

    try:
        asyncio.run(run_servers())
    except Exception as e:
        logger.critical("Fatal error starting FastMCP servers: %s\n%s", e, traceback.format_exc())
