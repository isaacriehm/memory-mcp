import json
import re
import traceback
from datetime import timedelta
from typing import Optional

from fastmcp import FastMCP, Context
from config import (
    logger,
    CONTEXT_DEFAULT_TTL_HOURS,
    CONTEXT_MAX_VALUE_LENGTH,
    CONTEXT_MAX_KEY_LENGTH,
)
from utils import _now
from db import get_pool

# Key validation: alphanumeric, underscores, hyphens, dots only. No spaces, slashes, quotes.
_VALID_KEY_RE = re.compile(r'^[a-zA-Z0-9_\-\.]{1,200}$')


def _validate_key(key: str) -> Optional[str]:
    """Returns error string if invalid, None if valid."""
    if not key or not isinstance(key, str):
        return "key must be a non-empty string"
    if len(key) > CONTEXT_MAX_KEY_LENGTH:
        return f"key must be {CONTEXT_MAX_KEY_LENGTH} characters or fewer"
    if not _VALID_KEY_RE.match(key):
        return "key may only contain letters, numbers, underscores, hyphens, and dots"
    return None


def register_context_tools(mcp: FastMCP) -> None:
    """
    Register all context store tools onto the provided FastMCP instance.
    Call this from production.py and admin.py after the split.
    """

    @mcp.tool()
    async def set_context(
        ctx: Context,
        key: str,
        value: str,
        ttl_hours: int = CONTEXT_DEFAULT_TTL_HOURS,
        scope: str = "session",
    ) -> dict:
        """
        Write a value to the ephemeral context store under a given key.

        This is NOT long-term memory. Use this for active plans, current task state,
        session summaries, or any data that will be stale within hours or days.
        Data is automatically deleted when the TTL expires.

        key: unique identifier for this context entry (alphanumeric, underscores, hyphens, dots only)
        value: the content to store (max 50,000 characters)
        ttl_hours: how many hours until this entry auto-expires (default: 24, min: 1, max: 720)
        scope: logical grouping for bulk retrieval (e.g. 'session', 'plan', 'task', 'codebase')

        If a key already exists, it is overwritten and the TTL is reset.
        """
        logger.info("Tool invoked: set_context (key: %s, scope: %s, ttl_hours: %d)", key, scope, ttl_hours)

        key_error = _validate_key(key)
        if key_error:
            return {"ok": False, "error": key_error}

        if not value or not isinstance(value, str) or not value.strip():
            return {"ok": False, "error": "value must be a non-empty string"}

        if len(value) > CONTEXT_MAX_VALUE_LENGTH:
            return {"ok": False, "error": f"value exceeds maximum length of {CONTEXT_MAX_VALUE_LENGTH} characters"}

        if not isinstance(ttl_hours, int) or ttl_hours < 1:
            return {"ok": False, "error": "ttl_hours must be a positive integer"}

        if ttl_hours > 720:
            return {"ok": False, "error": "ttl_hours cannot exceed 720 (30 days). For permanent storage use memorize_context instead."}

        if not scope or not isinstance(scope, str):
            scope = "session"
        scope = scope.strip().lower()[:50]

        try:
            now = _now()
            expires_at = now + timedelta(hours=ttl_hours)
            db_pool = get_pool()
            async with db_pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO context_store (key, value, scope, created_at, updated_at, expires_at)
                    VALUES ($1, $2, $3, $4, $4, $5)
                    ON CONFLICT (key) DO UPDATE
                        SET value = EXCLUDED.value,
                            scope = EXCLUDED.scope,
                            updated_at = EXCLUDED.updated_at,
                            expires_at = EXCLUDED.expires_at
                    """,
                    key, value, scope, now, expires_at,
                )
            logger.info("set_context: wrote key '%s' (scope: %s, expires: %s)", key, scope, expires_at.isoformat())
            return {
                "ok": True,
                "key": key,
                "scope": scope,
                "expires_at": expires_at.isoformat(),
                "ttl_hours": ttl_hours,
            }
        except Exception as e:
            logger.error("Error in set_context: %s\n%s", e, traceback.format_exc())
            return {"ok": False, "error": str(e)}


    @mcp.tool()
    async def get_context(ctx: Context, key: str) -> dict:
        """
        Retrieve a value from the ephemeral context store by its exact key.

        Returns the value if it exists and has not expired.
        Returns ok: false with error 'not_found' if the key does not exist or has expired.

        Use list_context_keys to discover what keys are currently active.
        """
        logger.info("Tool invoked: get_context (key: %s)", key)

        key_error = _validate_key(key)
        if key_error:
            return {"ok": False, "error": key_error}

        try:
            db_pool = get_pool()
            async with db_pool.acquire() as conn:
                row = await conn.fetchrow(
                    """
                    SELECT key, value, scope, created_at, updated_at, expires_at
                    FROM context_store
                    WHERE key = $1 AND expires_at > NOW()
                    """,
                    key,
                )
            if not row:
                return {"ok": False, "error": "not_found", "key": key}

            return {
                "ok": True,
                "key": row["key"],
                "value": row["value"],
                "scope": row["scope"],
                "created_at": row["created_at"].isoformat(),
                "updated_at": row["updated_at"].isoformat(),
                "expires_at": row["expires_at"].isoformat(),
            }
        except Exception as e:
            logger.error("Error in get_context: %s\n%s", e, traceback.format_exc())
            return {"ok": False, "error": str(e)}


    @mcp.tool()
    async def delete_context(ctx: Context, key: str) -> dict:
        """
        Explicitly delete a context entry before its TTL expires.

        Use this when a plan is completed, a task is done, or context is no longer relevant.
        Expired entries are deleted automatically by the TTL daemon — manual deletion is optional.
        """
        logger.info("Tool invoked: delete_context (key: %s)", key)

        key_error = _validate_key(key)
        if key_error:
            return {"ok": False, "error": key_error}

        try:
            db_pool = get_pool()
            async with db_pool.acquire() as conn:
                result = await conn.execute(
                    "DELETE FROM context_store WHERE key = $1",
                    key,
                )
            deleted = result == "DELETE 1"
            return {"ok": True, "key": key, "deleted": deleted}
        except Exception as e:
            logger.error("Error in delete_context: %s\n%s", e, traceback.format_exc())
            return {"ok": False, "error": str(e)}


    @mcp.tool()
    async def list_context_keys(ctx: Context, scope: Optional[str] = None) -> dict:
        """
        List all active (non-expired) context store keys, optionally filtered by scope.

        Returns key names, scopes, and expiry times. Does not return values.
        Use get_context(key) to retrieve a specific value.

        scope: optional filter (e.g. 'session', 'plan', 'task'). If omitted, returns all active keys.
        """
        logger.info("Tool invoked: list_context_keys (scope: %s)", scope)

        try:
            db_pool = get_pool()
            async with db_pool.acquire() as conn:
                if scope:
                    scope_clean = scope.strip().lower()[:50]
                    rows = await conn.fetch(
                        """
                        SELECT key, scope, created_at, updated_at, expires_at
                        FROM context_store
                        WHERE expires_at > NOW() AND scope = $1
                        ORDER BY updated_at DESC
                        """,
                        scope_clean,
                    )
                else:
                    rows = await conn.fetch(
                        """
                        SELECT key, scope, created_at, updated_at, expires_at
                        FROM context_store
                        WHERE expires_at > NOW()
                        ORDER BY updated_at DESC
                        """
                    )

            entries = [
                {
                    "key": r["key"],
                    "scope": r["scope"],
                    "created_at": r["created_at"].isoformat(),
                    "updated_at": r["updated_at"].isoformat(),
                    "expires_at": r["expires_at"].isoformat(),
                }
                for r in rows
            ]
            return {"ok": True, "count": len(entries), "entries": entries}
        except Exception as e:
            logger.error("Error in list_context_keys: %s\n%s", e, traceback.format_exc())
            return {"ok": False, "error": str(e)}


    @mcp.tool()
    async def extend_context_ttl(ctx: Context, key: str, additional_hours: int) -> dict:
        """
        Extend the TTL of an existing context entry by adding more hours to its current expiry.

        Use this when a plan or task takes longer than expected.
        Cannot extend beyond 720 total hours (30 days) from now.

        key: the context key to extend
        additional_hours: number of hours to add to the current expires_at (min: 1, max: 720)
        """
        logger.info("Tool invoked: extend_context_ttl (key: %s, additional_hours: %d)", key, additional_hours)

        key_error = _validate_key(key)
        if key_error:
            return {"ok": False, "error": key_error}

        if not isinstance(additional_hours, int) or additional_hours < 1:
            return {"ok": False, "error": "additional_hours must be a positive integer"}

        try:
            db_pool = get_pool()
            async with db_pool.acquire() as conn:
                row = await conn.fetchrow(
                    """
                    UPDATE context_store
                    SET expires_at = LEAST(
                        expires_at + ($1 * INTERVAL '1 hour'),
                        NOW() + INTERVAL '720 hours'
                    ),
                    updated_at = NOW()
                    WHERE key = $2 AND expires_at > NOW()
                    RETURNING key, expires_at
                    """,
                    additional_hours, key,
                )
            if not row:
                return {"ok": False, "error": "not_found", "key": key}

            return {
                "ok": True,
                "key": row["key"],
                "new_expires_at": row["expires_at"].isoformat(),
            }
        except Exception as e:
            logger.error("Error in extend_context_ttl: %s\n%s", e, traceback.format_exc())
            return {"ok": False, "error": str(e)}
