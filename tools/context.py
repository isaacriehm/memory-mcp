"""Context initialization, verification, and system primer synthesis."""

import json
import traceback
import asyncio
import re
from datetime import timedelta
from typing import Optional, Any
from uuid import UUID

from fastmcp import Context

from config import logger
from utils import _now, _vector_literal, _add_ttl_warning, generate_deterministic_id, sanitize_ltree_path
from llm import embed
from db import get_pool, _compute_verify_after
import db

from .search import _build_taxonomy_tree

_HANDOFF_LABEL_RE = re.compile(r"[^a-z0-9._-]+")


async def initialize_context(ctx: Context) -> dict[str, Any]:
    """REQUIRED — Call this FIRST at the start of every session, before any other memory tool or reasoning.
    Returns the System Primer: user identity, full taxonomy map, and retrieval guide.
    Must be called exactly once per session to orient the agent before interacting with the knowledge base.
    If `verification_block` is non-empty, inject it into the System Primer under ## Verification Required.
    Query the user regarding the accuracy of those specific records BEFORE executing any other commands.
    If `pending_handoffs` is non-empty, surface them to the user — another AI session left work to be picked up."""
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

            handoff_rows = await conn.fetch(
                """
                SELECT key, value, created_at, updated_at, expires_at
                FROM context_store
                WHERE scope = 'handoff' AND expires_at > NOW()
                ORDER BY updated_at DESC
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

        pending_handoffs = [
            {
                "key": r["key"],
                "preview": r["value"][:200] + ("..." if len(r["value"]) > 200 else ""),
                "created_at": r["created_at"].isoformat(),
                "updated_at": r["updated_at"].isoformat(),
                "expires_at": r["expires_at"].isoformat(),
                "resume_prompt": f"Execute pending handoff: {r['key'].removeprefix('handoff.')}",
            }
            for r in handoff_rows
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
            "initialize_context retrieved %d system records, %d requiring verification, %d pending handoffs.",
            len(results), len(verification_required), len(pending_handoffs),
        )
        return {
            "ok": True,
            "results": results,
            "verification_required": verification_required,
            "verification_block": verification_block,
            "pending_handoffs": pending_handoffs,
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


def _timeline_excerpt(text: Optional[str], max_len: int = 140) -> Optional[str]:
    if not text:
        return None
    compact = " ".join(text.split())
    if not compact:
        return None

    split_idx = len(compact)
    for token in (". ", "! ", "? "):
        pos = compact.find(token)
        if pos != -1:
            split_idx = min(split_idx, pos + 1)
    sentence = compact[:split_idx] if split_idx < len(compact) else compact

    if len(sentence) <= max_len:
        return sentence
    return sentence[: max_len - 3] + "..."


def _sanitize_handoff_label(label: str) -> str:
    compact = str(label or "").strip().lower()
    compact = _HANDOFF_LABEL_RE.sub("-", compact)
    compact = re.sub(r"-{2,}", "-", compact)
    compact = compact.strip(".-_")
    return compact or "handoff"


def _clamp_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except Exception:
        parsed = default
    return max(minimum, min(parsed, maximum))


def _shorten(text: Optional[str], max_len: int = 220) -> str:
    if not text:
        return ""
    compact = " ".join(text.split())
    if len(compact) <= max_len:
        return compact
    return compact[: max_len - 3] + "..."


async def _load_contradiction_snapshot(
    ctx: Context,
    category_path: Optional[str],
    since_days: int,
    limit: int,
) -> dict[str, Any]:
    try:
        from .admin_tools import contradiction_audit
    except Exception:
        return {"ok": False, "error": "unavailable"}

    try:
        return await contradiction_audit(
            ctx,
            category_path=category_path,
            since_days=since_days,
            limit=limit,
        )
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


async def decision_timeline(
    ctx: Context,
    category_path: Optional[str] = None,
    since_days: int = 90,
    limit: int = 50,
    include_superseded: bool = True,
) -> dict[str, Any]:
    """
    Return a deterministic timeline of memory decision events.

    Events are merged from memories (create/update/supersede) and, when present,
    conflict_audit_events. Output is ordered oldest → newest.
    """
    logger.info(
        "Tool invoked: decision_timeline (category_path=%s, since_days=%s, limit=%s, include_superseded=%s)",
        category_path, since_days, limit, include_superseded,
    )

    try:
        safe_limit = max(1, min(int(limit if limit is not None else 50), 200))
    except Exception:
        return {"ok": False, "error": "limit must be an integer"}

    try:
        safe_since_days = max(1, min(int(since_days if since_days is not None else 90), 3650))
    except Exception:
        return {"ok": False, "error": "since_days must be an integer"}

    safe_category_path = None
    if category_path and str(category_path).strip():
        safe_category_path = sanitize_ltree_path(str(category_path).strip())

    events: list[dict[str, Any]] = []

    try:
        db_pool = get_pool()
        async with db_pool.acquire() as conn:
            audit_table_exists = bool(
                await conn.fetchval("SELECT to_regclass('public.conflict_audit_events') IS NOT NULL")
            )

            created_params: list[Any] = [safe_since_days]
            created_where = [
                "m.archived_at IS NULL",
                "m.created_at >= NOW() - ($1::int * INTERVAL '1 day')",
            ]
            if safe_category_path:
                created_params.append(safe_category_path)
                created_where.append(f"m.category_path <@ ${len(created_params)}::ltree")
            if not include_superseded:
                created_where.append("m.supersedes_id IS NULL")
            created_params.append(safe_limit)
            created_rows = await conn.fetch(
                f"""
                SELECT
                    m.id,
                    m.content,
                    m.category_path::text AS category_path,
                    m.supersedes_id,
                    m.created_at
                FROM memories m
                WHERE {' AND '.join(created_where)}
                ORDER BY m.created_at DESC
                LIMIT ${len(created_params)}
                """,
                *created_params,
            )

            updated_params: list[Any] = [safe_since_days]
            updated_where = [
                "m.archived_at IS NULL",
                "m.supersedes_id IS NULL",
                "m.updated_at > m.created_at",
                "m.updated_at >= NOW() - ($1::int * INTERVAL '1 day')",
            ]
            if safe_category_path:
                updated_params.append(safe_category_path)
                updated_where.append(f"m.category_path <@ ${len(updated_params)}::ltree")
            updated_params.append(safe_limit)
            updated_rows = await conn.fetch(
                f"""
                SELECT
                    m.id,
                    m.content,
                    m.category_path::text AS category_path,
                    m.updated_at
                FROM memories m
                WHERE {' AND '.join(updated_where)}
                ORDER BY m.updated_at DESC
                LIMIT ${len(updated_params)}
                """,
                *updated_params,
            )

            superseded_rows = []
            if include_superseded:
                superseded_params: list[Any] = [safe_since_days]
                superseded_where = [
                    "m.archived_at IS NULL",
                    "m.supersedes_id IS NOT NULL",
                    "m.updated_at >= NOW() - ($1::int * INTERVAL '1 day')",
                ]
                if safe_category_path:
                    superseded_params.append(safe_category_path)
                    superseded_where.append(f"m.category_path <@ ${len(superseded_params)}::ltree")
                superseded_params.append(safe_limit)
                superseded_rows = await conn.fetch(
                    f"""
                    SELECT
                        m.id,
                        m.content,
                        m.category_path::text AS category_path,
                        m.supersedes_id,
                        m.updated_at
                    FROM memories m
                    WHERE {' AND '.join(superseded_where)}
                    ORDER BY m.updated_at DESC
                    LIMIT ${len(superseded_params)}
                    """,
                    *superseded_params,
                )

            audit_rows = []
            if audit_table_exists:
                try:
                    audit_params: list[Any] = [safe_since_days]
                    audit_where = ["e.created_at >= NOW() - ($1::int * INTERVAL '1 day')"]
                    if safe_category_path:
                        audit_params.append(safe_category_path)
                        audit_where.append(f"e.category_path <@ ${len(audit_params)}::ltree")
                    audit_params.append(safe_limit)
                    audit_rows = await conn.fetch(
                        f"""
                        SELECT
                            e.id,
                            e.created_at,
                            e.new_memory_id,
                            e.old_memory_id,
                            e.resolution,
                            e.category_path::text AS category_path,
                            e.details::text AS details,
                            COALESCE(new_m.content, old_m.content) AS summary_content
                        FROM conflict_audit_events e
                        LEFT JOIN memories new_m ON new_m.id = e.new_memory_id
                        LEFT JOIN memories old_m ON old_m.id = e.old_memory_id
                        WHERE {' AND '.join(audit_where)}
                        ORDER BY e.created_at DESC
                        LIMIT ${len(audit_params)}
                        """,
                        *audit_params,
                    )
                except Exception as audit_exc:
                    if getattr(audit_exc, "sqlstate", None) == "42P01":
                        logger.warning("decision_timeline degraded mode: conflict_audit_events unavailable")
                        audit_rows = []
                        audit_table_exists = False
                    else:
                        raise

        for r in created_rows:
            summary = _timeline_excerpt(r["content"])
            item = {
                "timestamp": r["created_at"].isoformat(),
                "event_type": "memory_created",
                "memory_id": str(r["id"]),
                "category_path": r["category_path"],
                "summary": f"Created memory: {summary}" if summary else "Created memory.",
            }
            if r["supersedes_id"] is not None:
                item["superseded_by"] = str(r["supersedes_id"])
            item["_sort_ts"] = r["created_at"]
            events.append(item)

        for r in updated_rows:
            summary = _timeline_excerpt(r["content"])
            item = {
                "timestamp": r["updated_at"].isoformat(),
                "event_type": "memory_updated",
                "memory_id": str(r["id"]),
                "category_path": r["category_path"],
                "summary": f"Updated memory: {summary}" if summary else "Updated memory.",
                "_sort_ts": r["updated_at"],
            }
            events.append(item)

        for r in superseded_rows:
            summary = _timeline_excerpt(r["content"])
            item = {
                "timestamp": r["updated_at"].isoformat(),
                "event_type": "memory_superseded",
                "memory_id": str(r["id"]),
                "category_path": r["category_path"],
                "summary": f"Memory superseded: {summary}" if summary else "Memory superseded.",
                "_sort_ts": r["updated_at"],
            }
            if r["supersedes_id"] is not None:
                item["superseded_by"] = str(r["supersedes_id"])
            events.append(item)

        for r in audit_rows:
            details = json.loads(r["details"]) if r["details"] else {}
            reason = details.get("reason_summary") if isinstance(details, dict) else None
            summary = f"Conflict resolved via {r['resolution']}."
            if reason:
                summary += f" {_timeline_excerpt(str(reason), max_len=100) or ''}".rstrip()
            else:
                excerpt = _timeline_excerpt(r["summary_content"])
                if excerpt:
                    summary += f" {excerpt}"

            item = {
                "timestamp": r["created_at"].isoformat(),
                "event_type": "conflict_resolved",
                "memory_id": str(r["new_memory_id"] or r["old_memory_id"]) if (r["new_memory_id"] or r["old_memory_id"]) else None,
                "category_path": r["category_path"] or "reference.unknown",
                "summary": summary,
                "audit_event_id": str(r["id"]),
                "_sort_ts": r["created_at"],
            }
            if r["old_memory_id"] is not None:
                item["supersedes"] = str(r["old_memory_id"])
            events.append(item)

        events.sort(key=lambda e: (e["_sort_ts"], e["event_type"], e["memory_id"]))
        if len(events) > safe_limit:
            events = events[-safe_limit:]
        for e in events:
            e.pop("_sort_ts", None)

        return {
            "ok": True,
            "count": len(events),
            "events": events,
            "filters": {
                "category_path": safe_category_path,
                "since_days": safe_since_days,
                "limit": safe_limit,
                "include_superseded": bool(include_superseded),
            },
            "audit_source_available": audit_table_exists,
        }
    except Exception as e:
        logger.error("Error in decision_timeline: %s\n%s", e, traceback.format_exc())
        return {"ok": False, "error": str(e)}


async def create_handoff_pack(
    ctx: Context,
    label: str,
    goal: str,
    ttl_hours: int = 72,
    include_recent_hours: int = 48,
    category_path: Optional[str] = None,
    max_context_items: int = 12,
) -> dict[str, Any]:
    """
    Create a deterministic, execution-ready handoff pack and store it under
    `handoff.<label>` in the context store.
    """
    logger.info(
        "Tool invoked: create_handoff_pack (label=%s, ttl_hours=%s, include_recent_hours=%s, category_path=%s, max_context_items=%s)",
        label, ttl_hours, include_recent_hours, category_path, max_context_items,
    )

    if not label or not isinstance(label, str) or not label.strip():
        return {"ok": False, "error": "label must be a non-empty string"}
    if not goal or not isinstance(goal, str) or not goal.strip():
        return {"ok": False, "error": "goal must be a non-empty string"}

    safe_label = _sanitize_handoff_label(label)
    key = f"handoff.{safe_label}"
    resume_prompt = f"Execute pending handoff: {safe_label}"
    safe_ttl = _clamp_int(ttl_hours, default=72, minimum=1, maximum=720)
    safe_recent_hours = _clamp_int(include_recent_hours, default=48, minimum=1, maximum=720)
    safe_max_items = _clamp_int(max_context_items, default=12, minimum=1, maximum=25)
    safe_category_path = None
    if category_path and str(category_path).strip():
        safe_category_path = sanitize_ltree_path(str(category_path).strip())

    days_window = max(1, (safe_recent_hours + 23) // 24)
    now = _now()

    try:
        db_pool = get_pool()
        async with db_pool.acquire() as conn:
            mem_where = [
                "supersedes_id IS NULL",
                "archived_at IS NULL",
                "(lexical_search @@ websearch_to_tsquery('english', $1) OR content ILIKE $2)",
            ]
            mem_params: list[Any] = [goal.strip(), f"%{goal.strip()}%"]
            if safe_category_path:
                mem_params.append(safe_category_path)
                mem_where.append(f"category_path <@ ${len(mem_params)}::ltree")
            mem_params.append(safe_max_items)
            memory_rows = await conn.fetch(
                f"""
                SELECT
                    id,
                    category_path::text AS category_path,
                    content,
                    updated_at,
                    ts_rank_cd(lexical_search, websearch_to_tsquery('english', $1)) AS lexical_rank
                FROM memories
                WHERE {' AND '.join(mem_where)}
                ORDER BY lexical_rank DESC, updated_at DESC
                LIMIT ${len(mem_params)}
                """,
                *mem_params,
            )

            context_rows = await conn.fetch(
                """
                SELECT key, scope, value, updated_at, expires_at
                FROM context_store
                WHERE expires_at > NOW()
                  AND scope != 'handoff'
                  AND updated_at >= NOW() - ($1 * INTERVAL '1 hour')
                ORDER BY updated_at DESC
                LIMIT $2
                """,
                safe_recent_hours,
                safe_max_items,
            )

            timeline = await decision_timeline(
                ctx,
                category_path=safe_category_path,
                since_days=days_window,
                limit=min(safe_max_items, 10),
                include_superseded=True,
            )
            contradiction = await _load_contradiction_snapshot(
                ctx,
                category_path=safe_category_path,
                since_days=days_window,
                limit=min(safe_max_items, 10),
            )

            touched_entities: list[tuple[str, str]] = []
            for row in memory_rows:
                touched_entities.append((str(row["id"]), row["category_path"]))

            constraints = [
                f"- Persist output to `{key}` with scope `handoff`",
                f"- TTL: {safe_ttl} hours",
                f"- Context window: last {safe_recent_hours} hours",
                f"- Max context items per source: {safe_max_items}",
            ]
            if safe_category_path:
                constraints.append(f"- Category scope: `{safe_category_path}`")
            else:
                constraints.append("- Category scope: global")

            decision_lines: list[str] = []
            if memory_rows:
                decision_lines.append("### Relevant memories")
                for row in memory_rows:
                    decision_lines.append(
                        f"- `{row['id']}` ({row['category_path']}): {_shorten(row['content'], 180)}"
                    )
            if context_rows:
                decision_lines.append("### Recent context keys")
                for row in context_rows:
                    decision_lines.append(
                        f"- `{row['key']}` [{row['scope']}] updated {row['updated_at'].isoformat()}: "
                        f"{_shorten(row['value'], 140)}"
                    )

            timeline_events = timeline.get("events", []) if isinstance(timeline, dict) and timeline.get("ok") else []
            if timeline_events:
                decision_lines.append("### Timeline signals")
                for event in timeline_events[:min(len(timeline_events), safe_max_items)]:
                    decision_lines.append(
                        f"- {event.get('timestamp', '')} {event.get('event_type', 'event')}: {event.get('summary', '')}"
                    )

            contradiction_events = (
                contradiction.get("events", [])
                if isinstance(contradiction, dict) and contradiction.get("ok")
                else []
            )
            if contradiction_events:
                decision_lines.append("### Conflict audit signals")
                for event in contradiction_events[:min(len(contradiction_events), safe_max_items)]:
                    decision_lines.append(
                        f"- {event.get('created_at', '')} {event.get('resolution', 'unknown')}: "
                        f"{_shorten(event.get('summary'), 150)}"
                    )

            risks: list[str] = []
            if not memory_rows:
                risks.append("- No matching active memories were found for the goal query.")
            if not context_rows:
                risks.append("- No recent non-handoff context entries were available in the selected time window.")
            if not timeline_events:
                risks.append("- Decision timeline data is unavailable or empty for the selected scope.")
            if isinstance(contradiction, dict) and not contradiction.get("ok"):
                risks.append("- Contradiction audit source unavailable; pack excludes conflict-resolution detail.")

            if not risks:
                risks.append("- No immediate blockers detected from retrieved context sources.")

            touched_lines = [
                f"- memory `{memory_id}` in `{category}`" for memory_id, category in touched_entities
            ]
            if not touched_lines:
                touched_lines = ["- none"]

            next_steps = [
                "- Load this handoff key and execute against the specified goal.",
                "- Validate behavior with focused checks for modified code paths.",
                "- Report what changed, what remains, and any newly introduced risks.",
            ]
            success_checks = [
                "- Goal outcome is implemented and verifiable.",
                "- No unresolved blockers remain in open risks.",
                "- Handoff key can be deleted after execution completes.",
            ]

            pack = "\n".join([
                f"# Handoff Pack: {safe_label}",
                "",
                "## Goal",
                goal.strip(),
                "",
                "## Constraints",
                *constraints,
                "",
                "## Decisions/Context",
                *(decision_lines or ["- No context signals were found."]),
                "",
                "## Touched entities (memory IDs/categories)",
                *touched_lines,
                "",
                "## Open risks",
                *risks,
                "",
                "## Next steps",
                *next_steps,
                "",
                "## Success checks",
                *success_checks,
            ])

            expires_at = now + timedelta(hours=safe_ttl)
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
                key,
                pack,
                "handoff",
                now,
                expires_at,
            )

        preview = pack[:280] + ("..." if len(pack) > 280 else "")
        return {
            "ok": True,
            "key": key,
            "resume_prompt": resume_prompt,
            "expires_at": expires_at.isoformat(),
            "pack_preview": preview,
        }
    except Exception as e:
        logger.error("Error in create_handoff_pack: %s\n%s", e, traceback.format_exc())
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
            f"## Handoff Protocol\n"
            f"Use the context store to pass plans and task state between AI sessions or between different AI tools.\n\n"
            f"**Exporting a handoff (AI A — the planner):**\n"
            f"When the user says 'export plan', 'hand this off', or similar:\n"
            f"1. Write the plan to context store: `set_context('handoff.<label>', <plan text>, ttl_hours=72, scope='handoff')`\n"
            f"   - `<label>` is a short slug describing the work, e.g. `handoff.auth-refactor`\n"
            f"   - Include enough context for a fresh AI to execute without clarification: goal, steps, relevant files, decisions made\n"
            f"2. Tell the user the resume prompt to paste into the other AI:\n"
            f"   > **Resume prompt:** \"Execute pending handoff: <label>\"\n"
            f"3. STOP. Do NOT offer to execute the handoff plan yourself. The purpose of a handoff is to pass work to another AI session.\n\n"
            f"**Importing a handoff (AI B — the executor):**\n"
            f"When the user says 'execute pending handoff', 'resume handoff: <label>', or similar:\n"
            f"1. If a label is given: `get_context('handoff.<label>')` and execute the plan\n"
            f"2. If no label: check `initialize_context` response for `pending_handoffs` — it lists all active handoffs\n"
            f"   then `get_context` the relevant one and confirm with the user before starting\n"
            f"3. After completion: `delete_context('handoff.<label>')` to clean up\n\n"
            f"**Note:** `initialize_context` automatically surfaces any active handoffs in `pending_handoffs` "
            f"so you never need to poll manually at session start.\n\n"
            f"## Retrieval Guide\n"
            f"- `search_memory(query)` — hybrid semantic + keyword search, returns top 10\n"
            f"- `search_memory(query, category_path='projects.myapp.planning')` — scoped to subtree\n"
            f"- `list_categories()` — all paths with counts\n"
            f"- `fetch_document(memory_id)` — reconstruct full document from chunk chain\n"
            f"- `trace_history(memory_id)` — inspect supersession chain for a fact\n"
            f"- `decision_timeline(...)` — view chronological decision changes across memories and audit events\n"
            f"- `contradiction_audit(...)` — inspect conflict-resolution audit events and reason payloads\n"
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
