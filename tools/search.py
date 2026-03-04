"""Search and taxonomy retrieval tools."""

import hashlib
import json
import re
import traceback
from typing import Optional, Any
from uuid import UUID

from fastmcp import Context

from config import (
    logger,
    DEFAULT_SEARCH_LIMIT,
    FEEDBACK_RERANK_ENABLED,
    FEEDBACK_MAX_DELTA,
    FEEDBACK_HALF_LIFE_DAYS,
    CANONICAL_MIN_IN_TOPK,
    HISTORICAL_MIN_IN_TOPK,
    FEEDBACK_EXPLORATION_SLOTS,
    HISTORICAL_BASE_SCORE_MULTIPLIER,
)
from utils import (
    _now,
    _vector_literal,
    _add_ttl_warning,
    sanitize_ltree_path,
    sanitize_ltree_label,
    infer_memory_tier,
    normalize_memory_tier,
)
from llm import embed, semantic_diff
from db import get_pool


def _hash_query(query: str, category_path: str, task_type: Optional[str] = None) -> str:
    normalized = " ".join(query.lower().split())
    category_scope = sanitize_ltree_path(category_path.strip()).lower()

    task_scope = ""
    if task_type and str(task_type).strip():
        task_scope = str(task_type).strip().lower()[:120]

    scoped = f"q={normalized}|cat={category_scope}|task={task_scope or '*'}"
    return hashlib.sha256(scoped.encode("utf-8")).hexdigest()


def _normalize_outcome(outcome: Any) -> tuple[bool, int | str]:
    if isinstance(outcome, bool):
        return False, "outcome must be +1 or -1"
    if isinstance(outcome, int):
        if outcome in (-1, 1):
            return True, outcome
        return False, "outcome must be +1 or -1"
    if isinstance(outcome, str):
        norm = outcome.strip().lower()
        if norm in {"1", "+1", "helpful", "positive", "up"}:
            return True, 1
        if norm in {"-1", "not_helpful", "not-helpful", "negative", "down"}:
            return True, -1
    return False, "outcome must be +1 or -1"


def _infer_memory_tier(category_path: str, metadata: dict[str, Any]) -> str:
    return infer_memory_tier(category_path, metadata)


def _resolve_result_tier(category_path: str, metadata: dict[str, Any], tier_column: Any) -> str:
    resolved = normalize_memory_tier(tier_column) or _infer_memory_tier(category_path, metadata)
    if resolved == "ephemeral":
        return "historical"
    return resolved


def _effective_tier_requirements(candidates: list[dict[str, Any]], limit: int) -> dict[str, int]:
    available_by_tier = {
        "canonical": sum(1 for item in candidates if item.get("tier") == "canonical"),
        "historical": sum(1 for item in candidates if item.get("tier") == "historical"),
    }
    required_raw = {
        "canonical": CANONICAL_MIN_IN_TOPK,
        "historical": HISTORICAL_MIN_IN_TOPK,
    }
    required: dict[str, int] = {}
    remaining_slots = limit
    for tier, floor in required_raw.items():
        if floor <= 0 or remaining_slots <= 0:
            continue
        assigned = min(int(floor), int(available_by_tier.get(tier, 0)), remaining_slots)
        if assigned > 0:
            required[tier] = assigned
            remaining_slots -= assigned
    return required


def _select_removal_index(selected: list[dict[str, Any]], required_by_tier: dict[str, int]) -> Optional[int]:
    selected_sorted = sorted(
        enumerate(selected),
        key=lambda pair: (float(pair[1]["adjusted_score"]), float(pair[1]["base_score"])),
    )
    for idx, _ in selected_sorted:
        tier = selected[idx].get("tier", "other")
        if tier in required_by_tier:
            current_count = sum(1 for item in selected if item.get("tier") == tier)
            if current_count <= required_by_tier[tier]:
                continue
        return idx
    return None


def _enforce_tier_floors(
    ranked_candidates: list[dict[str, Any]],
    limit: int,
    required_by_tier: dict[str, int],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    selected = list(ranked_candidates[:limit])
    unselected = list(ranked_candidates[limit:])

    for tier, required in required_by_tier.items():
        while sum(1 for item in selected if item.get("tier") == tier) < required:
            candidates_for_tier = [item for item in unselected if item.get("tier") == tier]
            if not candidates_for_tier:
                break
            add_item = max(
                candidates_for_tier,
                key=lambda item: (float(item["adjusted_score"]), float(item["base_score"])),
            )
            remove_idx = _select_removal_index(selected, required_by_tier)
            if remove_idx is None:
                break

            removed = selected.pop(remove_idx)
            unselected.remove(add_item)
            selected.append(add_item)
            unselected.append(removed)

            selected.sort(key=lambda item: float(item["adjusted_score"]), reverse=True)
            unselected.sort(key=lambda item: float(item["adjusted_score"]), reverse=True)

    return selected, unselected


def _apply_exploration_slot(
    selected: list[dict[str, Any]],
    unselected: list[dict[str, Any]],
    required_by_tier: dict[str, int],
    slots: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if slots <= 0:
        return selected, unselected

    for _ in range(slots):
        unexplored = [item for item in unselected if int(item.get("feedback_count", 0)) == 0]
        if not unexplored:
            break
        promote_item = max(
            unexplored,
            key=lambda item: (float(item["base_score"]), float(item["adjusted_score"])),
        )
        remove_idx = _select_removal_index(selected, required_by_tier)
        if remove_idx is None:
            break

        removed = selected.pop(remove_idx)
        unselected.remove(promote_item)
        selected.append(promote_item)
        unselected.append(removed)

        selected.sort(key=lambda item: float(item["adjusted_score"]), reverse=True)
        unselected.sort(key=lambda item: float(item["adjusted_score"]), reverse=True)

    return selected, unselected


async def report_retrieval_outcome(
    ctx: Context,
    query: str,
    memory_id: str,
    outcome: int,
    task_type: Optional[str] = None,
    category_path: Optional[str] = None,
) -> dict[str, Any]:
    """
    Persist retrieval outcome feedback for future reranking.

    outcome: +1 for helpful, -1 for not_helpful.
    """
    logger.info("Tool invoked: report_retrieval_outcome (memory_id: %s, task_type: %s)", memory_id, task_type)

    if not query or not isinstance(query, str) or not query.strip():
        return {"ok": False, "error": "query must be a non-empty string"}

    ok, parsed_outcome = _normalize_outcome(outcome)
    if not ok:
        return {"ok": False, "error": str(parsed_outcome)}

    try:
        UUID(memory_id)
    except Exception:
        return {"ok": False, "error": "memory_id must be a valid UUID"}

    task_type_clean: Optional[str] = None
    if task_type is not None:
        task_type_clean = str(task_type).strip()[:120] or None

    scoped_category_path: Optional[str] = None
    if category_path and category_path.strip():
        try:
            scoped_category_path = sanitize_ltree_path(category_path.strip())
        except Exception:
            return {"ok": False, "error": "category_path must be a valid ltree-style path"}

    try:
        ok_latest, resolved_memory_id = await _resolve_latest_active_memory_id(memory_id)
        if not ok_latest:
            return {"ok": False, "error": str(resolved_memory_id)}
        target_memory_id = UUID(resolved_memory_id)

        db_pool = get_pool()
        async with db_pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT category_path::text AS category_path
                FROM memories
                WHERE id = $1
                  AND supersedes_id IS NULL
                  AND archived_at IS NULL
                """,
                target_memory_id,
            )
            if not row:
                return {"ok": False, "error": "memory_id not found or archived"}

            memory_category_path = row["category_path"]
            hash_paths = {memory_category_path}
            if scoped_category_path:
                hash_paths.add(scoped_category_path)
            query_hashes = sorted(_hash_query(query, p, task_type_clean) for p in hash_paths)

            for query_hash in query_hashes:
                await conn.execute(
                    """
                    INSERT INTO retrieval_feedback (query_hash, memory_id, outcome, task_type, created_at)
                    VALUES ($1, $2, $3, $4, NOW())
                    """,
                    query_hash,
                    target_memory_id,
                    int(parsed_outcome),
                    task_type_clean,
                )

        return {
            "ok": True,
            "memory_id": str(target_memory_id),
            "query_hashes": query_hashes,
            "outcome": int(parsed_outcome),
            "task_type": task_type_clean,
            "category_path": memory_category_path,
        }
    except Exception as e:
        logger.error("Error in report_retrieval_outcome: %s\n%s", e, traceback.format_exc())
        return {"ok": False, "error": str(e)}


async def search_memory(
    ctx: Context,
    query: str,
    category_path: Optional[str] = None,
    limit: int = DEFAULT_SEARCH_LIMIT,
    task_type: Optional[str] = None,
) -> dict[str, Any]:
    """Semantically search the knowledge base.
    Hybrid Retrieval: Use 'category_path' to filter by domains (e.g., 'projects.myapp' or 'user').
    If unsure of the exact path, leave category_path null to search globally, or use list_categories first.
    Results include 'tags' in the metadata for exact contextual matches."""
    logger.info("Tool invoked: search_memory (query: '%s', category_path: '%s', limit: %d)", query, category_path, limit)

    if not query or not isinstance(query, str) or not query.strip():
        return {"ok": False, "error": "query must be a non-empty string"}

    limit = max(1, min(int(limit or DEFAULT_SEARCH_LIMIT), 100))
    candidate_limit = min(100, limit + 25) if FEEDBACK_RERANK_ENABLED else limit
    try:
        vec = await embed(query)
        vec_lit = _vector_literal(vec)
        task_type_clean: Optional[str] = None
        if task_type is not None:
            task_type_clean = str(task_type).strip()[:120] or None

        db_pool = get_pool()
        async with db_pool.acquire() as conn:
            where_clause = "m.supersedes_id IS NULL AND m.archived_at IS NULL"
            params = [vec_lit, candidate_limit, query]
            safe_path: Optional[str] = None
            if category_path and category_path.strip():
                safe_path = sanitize_ltree_path(category_path.strip())
                where_clause += f" AND m.category_path <@ ${len(params) + 1}::ltree"
                params.append(safe_path)

            rows = await conn.fetch(
                f"""
                WITH semantic_search AS (
                    SELECT id, 1 - (embedding <=> $1::vector) AS semantic_score,
                           row_number() OVER (ORDER BY embedding <=> $1::vector) AS semantic_rank
                    FROM memories m WHERE {where_clause}
                    ORDER BY embedding <=> $1::vector LIMIT $2
                ),
                keyword_search AS (
                    SELECT id, ts_rank_cd(lexical_search, websearch_to_tsquery('english', $3)) AS keyword_score,
                           row_number() OVER (ORDER BY ts_rank_cd(lexical_search, websearch_to_tsquery('english', $3)) DESC) AS keyword_rank
                    FROM memories m WHERE {where_clause} AND lexical_search @@ websearch_to_tsquery('english', $3)
                    ORDER BY keyword_score DESC LIMIT $2
                ),
                combined AS (
                    SELECT m.id, m.content, m.category_path::text, m.supersedes_id, m.created_at, m.updated_at, m.metadata::text, m.tier,
                    COALESCE(s.semantic_score, 0.0) AS semantic_score,
                    COALESCE(k.keyword_score, 0.0) AS keyword_score,
                    COALESCE(1.0 / (60 + s.semantic_rank), 0.0) + COALESCE(1.0 / (60 + k.keyword_rank), 0.0) AS rrf_score
                    FROM memories m
                    LEFT JOIN semantic_search s ON m.id = s.id
                    LEFT JOIN keyword_search k ON m.id = k.id
                    WHERE s.id IS NOT NULL OR k.id IS NOT NULL
                    ORDER BY rrf_score DESC LIMIT $2
                )
                SELECT c.*,
                       prev_lat.prev_content,
                       nxt_lat.next_content
                FROM combined c
                LEFT JOIN LATERAL (
                    SELECT prev_inner.content AS prev_content
                    FROM memory_edges ep_inner
                    JOIN memories prev_inner ON prev_inner.id = ep_inner.source_id
                      AND prev_inner.supersedes_id IS NULL AND prev_inner.archived_at IS NULL
                    WHERE ep_inner.target_id = c.id AND ep_inner.relation_type = 'sequence_next'
                    LIMIT 1
                ) prev_lat ON true
                LEFT JOIN LATERAL (
                    SELECT nxt_inner.content AS next_content
                    FROM memory_edges en_inner
                    JOIN memories nxt_inner ON nxt_inner.id = en_inner.target_id
                      AND nxt_inner.supersedes_id IS NULL AND nxt_inner.archived_at IS NULL
                    WHERE en_inner.source_id = c.id AND en_inner.relation_type = 'sequence_next'
                    LIMIT 1
                ) nxt_lat ON true
                ORDER BY c.rrf_score DESC
                """,
                *params,
            )

            if not rows:
                logger.info("search_memory completed. Found 0 results.")
                return {"ok": True, "results": []}

            candidates: list[dict[str, Any]] = []
            for r in rows:
                metadata = json.loads(r["metadata"]) if r["metadata"] else {}
                tier = _resolve_result_tier(r["category_path"], metadata, r["tier"])
                raw_base_score = float(r["rrf_score"])
                base_score = (
                    raw_base_score * HISTORICAL_BASE_SCORE_MULTIPLIER
                    if tier == "historical"
                    else raw_base_score
                )
                candidate = {
                    "id": r["id"],
                    "content": r["content"],
                    "prev_content": r["prev_content"],
                    "next_content": r["next_content"],
                    "category_path": r["category_path"],
                    "created_at": r["created_at"],
                    "updated_at": r["updated_at"],
                    "metadata": metadata,
                    "tier": tier,
                    "semantic_score": float(r["semantic_score"]),
                    "keyword_score": float(r["keyword_score"]),
                    "raw_base_score": raw_base_score,
                    "base_score": base_score,
                    "adjusted_score": base_score,
                    "feedback_signal": 0.0,
                    "feedback_delta": 0.0,
                    "feedback_count": 0,
                }
                candidates.append(candidate)

            if FEEDBACK_RERANK_ENABLED and candidates:
                candidate_ids = [item["id"] for item in candidates]
                hash_set: set[str] = set()
                if safe_path:
                    hash_set.add(_hash_query(query, safe_path, task_type_clean))
                for item in candidates:
                    hash_set.add(_hash_query(query, item["category_path"], task_type_clean))
                query_hashes = list(hash_set)

                feedback_rows = await conn.fetch(
                    """
                    SELECT
                        memory_id,
                        COALESCE(
                            SUM(
                                outcome * POWER(
                                    0.5,
                                    GREATEST(EXTRACT(EPOCH FROM (NOW() - created_at)) / 86400.0, 0.0) / $2::double precision
                                )
                            ) / NULLIF(
                                SUM(
                                    POWER(
                                        0.5,
                                        GREATEST(EXTRACT(EPOCH FROM (NOW() - created_at)) / 86400.0, 0.0) / $2::double precision
                                    )
                                ),
                                0
                            ),
                            0.0
                        ) AS signal,
                        COUNT(*)::int AS feedback_count
                    FROM retrieval_feedback
                    WHERE query_hash = ANY($1::text[])
                      AND memory_id = ANY($3::uuid[])
                    GROUP BY memory_id
                    """,
                    query_hashes,
                    FEEDBACK_HALF_LIFE_DAYS,
                    candidate_ids,
                )
                feedback_by_id = {
                    row["memory_id"]: {
                        "signal": float(row["signal"]),
                        "count": int(row["feedback_count"]),
                    }
                    for row in feedback_rows
                }

                for item in candidates:
                    feedback = feedback_by_id.get(item["id"])
                    if feedback:
                        signal = max(-1.0, min(1.0, float(feedback["signal"])))
                        delta = max(-FEEDBACK_MAX_DELTA, min(FEEDBACK_MAX_DELTA, signal * FEEDBACK_MAX_DELTA))
                        bounded_score = max(
                            item["base_score"] - FEEDBACK_MAX_DELTA,
                            min(item["base_score"] + FEEDBACK_MAX_DELTA, item["base_score"] + delta),
                        )
                        item["feedback_signal"] = signal
                        item["feedback_delta"] = bounded_score - item["base_score"]
                        item["adjusted_score"] = bounded_score
                        item["feedback_count"] = int(feedback["count"])

            candidates.sort(
                key=lambda item: (
                    float(item["adjusted_score"]),
                    float(item["base_score"]),
                    float(item["semantic_score"]),
                ),
                reverse=True,
            )

            if FEEDBACK_RERANK_ENABLED:
                required_by_tier = _effective_tier_requirements(candidates, limit)
                selected, unselected = _enforce_tier_floors(candidates, limit, required_by_tier)
                selected, _ = _apply_exploration_slot(
                    selected,
                    unselected,
                    required_by_tier,
                    min(FEEDBACK_EXPLORATION_SLOTS, max(0, limit - sum(required_by_tier.values()))),
                )
                final_candidates = selected[:limit]
            else:
                final_candidates = candidates[:limit]

            ids = [item["id"] for item in final_candidates]
            if ids:
                await conn.execute("UPDATE memories SET last_accessed_at = $1 WHERE id = ANY($2)", _now(), ids)

            results = []
            for item in final_candidates:
                full_content = item["content"]
                if item["prev_content"]:
                    full_content = f"...{item['prev_content']}\n\n{full_content}"
                if item["next_content"]:
                    full_content = f"{full_content}\n\n{item['next_content']}..."

                result_item = {
                    "id": str(item["id"]),
                    "content": full_content,
                    "category_path": item["category_path"],
                    "score": round(float(item["adjusted_score"]), 6),
                    "semantic_score": round(float(item["semantic_score"]), 6),
                    "keyword_score": round(float(item["keyword_score"]), 6),
                    "created_at": item["created_at"].isoformat(),
                    "updated_at": item["updated_at"].isoformat(),
                    "metadata": item["metadata"],
                }
                if FEEDBACK_RERANK_ENABLED:
                    result_item["base_score"] = round(float(item["base_score"]), 6)
                    result_item["raw_base_score"] = round(float(item["raw_base_score"]), 6)
                    result_item["feedback_delta"] = round(float(item["feedback_delta"]), 6)
                    result_item["feedback_signal"] = round(float(item["feedback_signal"]), 6)
                    result_item["tier"] = item["tier"]
                _add_ttl_warning(result_item, item["updated_at"])
                results.append(result_item)

        results.sort(key=lambda r: r.get("is_expired", False))
        logger.info("search_memory completed. Found %d results.", len(results))
        return {"ok": True, "results": results}
    except Exception as e:
        logger.error("Error in search_memory: %s\n%s", e, traceback.format_exc())
        return {"ok": False, "error": str(e)}


async def list_categories(ctx: Context) -> dict[str, Any]:
    """List all active taxonomy paths with memory counts."""
    try:
        db_pool = get_pool()
        async with db_pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT category_path::text AS category, COUNT(*) AS count "
                "FROM memories WHERE supersedes_id IS NULL AND archived_at IS NULL "
                "GROUP BY category_path ORDER BY count DESC"
            )
        cats = [{"category": r["category"], "count": int(r["count"])} for r in rows]
        return {"ok": True, "categories": cats}
    except Exception as e:
        return {"ok": False, "error": str(e)}


async def fetch_document(ctx: Context, memory_id: str) -> dict[str, Any]:
    """
    Reconstruct the full document for a given memory chunk by following all
    sequence_next edges via a recursive CTE. Returns the concatenated text of
    all chunks in sequence order, eliminating the need for manual traversal.
    """
    logger.info("Tool invoked: fetch_document (memory_id: %s)", memory_id)
    try:
        target_id = UUID(memory_id)
    except Exception:
        return {"ok": False, "error": "memory_id must be a valid UUID"}

    try:
        db_pool = get_pool()
        async with db_pool.acquire() as conn:
            rows = await conn.fetch(
                """
                WITH RECURSIVE backward AS (
                    SELECT m.id, m.content, m.category_path::text, m.created_at, 0 AS depth
                    FROM memories m
                    WHERE m.id = $1 AND m.supersedes_id IS NULL AND m.archived_at IS NULL
                  UNION ALL
                    SELECT m.id, m.content, m.category_path::text, m.created_at, b.depth + 1
                    FROM backward b
                    JOIN memory_edges e ON e.target_id = b.id AND e.relation_type = 'sequence_next'
                    JOIN memories m ON m.id = e.source_id
                    WHERE m.supersedes_id IS NULL
                      AND m.archived_at IS NULL
                      AND b.depth < 200
                ),
                forward AS (
                    SELECT m.id, m.content, m.category_path::text, m.created_at, 0 AS depth
                    FROM memories m
                    WHERE m.id = $1 AND m.supersedes_id IS NULL AND m.archived_at IS NULL
                  UNION ALL
                    SELECT m.id, m.content, m.category_path::text, m.created_at, f.depth + 1
                    FROM forward f
                    JOIN memory_edges e ON e.source_id = f.id AND e.relation_type = 'sequence_next'
                    JOIN memories m ON m.id = e.target_id
                    WHERE m.supersedes_id IS NULL
                      AND m.archived_at IS NULL
                      AND f.depth < 200
                ),
                combined AS (
                    SELECT id, content, category_path, created_at, -depth AS sort_key FROM backward
                    UNION ALL
                    SELECT id, content, category_path, created_at, depth AS sort_key FROM forward WHERE depth > 0
                ),
                deduped AS (
                    SELECT DISTINCT ON (id) id, content, category_path, created_at, sort_key
                    FROM combined
                    ORDER BY id, sort_key
                )
                SELECT id, content, category_path, created_at, sort_key
                FROM deduped
                ORDER BY sort_key
                """,
                target_id,
            )

        if not rows:
            return {"ok": False, "error": f"Memory {memory_id} not found or is archived."}

        unified_text = "\n\n".join(r["content"] for r in rows)
        logger.info("fetch_document assembled %d chunks for memory %s", len(rows), memory_id)
        return {
            "ok": True,
            "memory_id": memory_id,
            "chunk_count": len(rows),
            "category_path": rows[0]["category_path"],
            "content": unified_text,
        }
    except Exception as e:
        logger.error("Error in fetch_document: %s\n%s", e, traceback.format_exc())
        return {"ok": False, "error": str(e)}


def _fallback_snippet(text: str, max_chars: int = 120) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    source = lines[0] if lines else text.strip()
    compact = " ".join(source.split())
    return compact[:max_chars] if compact else "(empty)"


def _fallback_token_set(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9_]+", text.lower()))


def _build_semantic_diff_fallback(
    left_doc: dict[str, Any], right_doc: dict[str, Any], err: Exception, max_bullets: int
) -> dict[str, Any]:
    left_content = str(left_doc.get("content", ""))
    right_content = str(right_doc.get("content", ""))
    left_words = len(left_content.split())
    right_words = len(right_content.split())
    left_tokens = _fallback_token_set(left_content)
    right_tokens = _fallback_token_set(right_content)
    overlap = len(left_tokens & right_tokens) / max(1, len(left_tokens | right_tokens))

    added_points: list[str] = []
    removed_points: list[str] = []
    changed_points: list[str] = []
    risk_notes: list[str] = []

    if right_words > left_words:
        added_points.append(f"Right document is longer by {right_words - left_words} words.")
    if left_words > right_words:
        removed_points.append(f"Right document is shorter by {left_words - right_words} words.")
    if left_doc.get("chunk_count") != right_doc.get("chunk_count"):
        changed_points.append(
            f"Chunk count changed from {left_doc.get('chunk_count')} to {right_doc.get('chunk_count')}."
        )
    if left_doc.get("category_path") != right_doc.get("category_path"):
        changed_points.append(
            f"Category moved from {left_doc.get('category_path')} to {right_doc.get('category_path')}."
        )
    if _fallback_snippet(left_content) != _fallback_snippet(right_content):
        changed_points.append(
            f"Opening snippet changed: '{_fallback_snippet(left_content)}' -> '{_fallback_snippet(right_content)}'."
        )

    risk_notes.append(
        "LLM semantic diff unavailable; this fallback reports structural deltas only."
    )
    risk_notes.append(f"Token overlap ratio: {overlap:.2f}")
    risk_notes.append(f"LLM error: {str(err)[:300]}")

    return {
        "overview": "Deterministic fallback: semantic comparison degraded to structural signals.",
        "added_points": added_points[:max_bullets],
        "removed_points": removed_points[:max_bullets],
        "changed_points": changed_points[:max_bullets],
        "risk_notes": risk_notes[:max_bullets],
        "fallback_error": str(err)[:500],
        "degraded": True,
    }


async def _resolve_latest_active_memory_id(memory_id: str) -> tuple[bool, str]:
    """
    Resolve any memory ID (active or superseded) to the latest active successor.
    Returns (ok, resolved_id_or_error_message).
    """
    try:
        target_id = UUID(memory_id)
    except Exception:
        return False, "memory_id must be a valid UUID"

    try:
        db_pool = get_pool()
        async with db_pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                WITH RECURSIVE chain AS (
                    SELECT id, supersedes_id, archived_at, 0 AS depth
                    FROM memories
                    WHERE id = $1
                  UNION ALL
                    SELECT m.id, m.supersedes_id, m.archived_at, c.depth + 1
                    FROM memories m
                    JOIN chain c ON m.id = c.supersedes_id
                    WHERE c.supersedes_id IS NOT NULL
                      AND c.depth < 100
                )
                SELECT id, supersedes_id, archived_at, depth
                FROM chain
                ORDER BY depth DESC
                LIMIT 1
                """,
                target_id,
            )
        if not row:
            return False, f"Memory {memory_id} not found."
        if row["archived_at"] is not None:
            return False, f"Memory {memory_id} resolves to an archived record."
        if row["supersedes_id"] is not None:
            # Defensive guard for malformed chains.
            return False, f"Memory {memory_id} could not resolve to an active record."
        return True, str(row["id"])
    except Exception as e:
        logger.error("Error resolving latest active memory id for %s: %s\n%s", memory_id, e, traceback.format_exc())
        return False, str(e)


async def semantic_diff_memory(
    ctx: Context,
    left_memory_id: str,
    right_memory_id: str,
    max_bullets: int = 12,
) -> dict[str, Any]:
    """
    Compare two memory documents semantically and return concise added/removed/changed meaning deltas.
    """
    logger.info(
        "Tool invoked: semantic_diff_memory (left_memory_id: %s, right_memory_id: %s, max_bullets: %s)",
        left_memory_id,
        right_memory_id,
        max_bullets,
    )

    try:
        UUID(left_memory_id)
    except Exception:
        return {"ok": False, "error": "left_memory_id must be a valid UUID"}
    try:
        UUID(right_memory_id)
    except Exception:
        return {"ok": False, "error": "right_memory_id must be a valid UUID"}

    try:
        parsed_max_bullets = int(max_bullets)
    except (TypeError, ValueError):
        return {"ok": False, "error": "max_bullets must be an integer"}
    bullet_limit = max(1, min(parsed_max_bullets, 20))

    left_ok, resolved_left_id = await _resolve_latest_active_memory_id(left_memory_id)
    if not left_ok:
        return {"ok": False, "error": f"Left memory lookup failed: {resolved_left_id}"}

    right_ok, resolved_right_id = await _resolve_latest_active_memory_id(right_memory_id)
    if not right_ok:
        return {"ok": False, "error": f"Right memory lookup failed: {resolved_right_id}"}

    left_doc = await fetch_document(ctx, resolved_left_id)
    if not left_doc.get("ok"):
        return {"ok": False, "error": f"Left memory lookup failed: {left_doc.get('error', 'unknown error')}"}

    right_doc = await fetch_document(ctx, resolved_right_id)
    if not right_doc.get("ok"):
        return {"ok": False, "error": f"Right memory lookup failed: {right_doc.get('error', 'unknown error')}"}

    try:
        diff = await semantic_diff(left_doc["content"], right_doc["content"], bullet_limit)
        return {
            "ok": True,
            "left_memory_id": left_memory_id,
            "right_memory_id": right_memory_id,
            "resolved_left_memory_id": resolved_left_id,
            "resolved_right_memory_id": resolved_right_id,
            "overview": diff.get("overview", ""),
            "added_points": diff.get("added_points", []),
            "removed_points": diff.get("removed_points", []),
            "changed_points": diff.get("changed_points", []),
            "risk_notes": diff.get("risk_notes", []),
            "degraded": False,
        }
    except Exception as e:
        logger.warning("semantic_diff_memory using deterministic fallback: %s", e)
        fallback = _build_semantic_diff_fallback(left_doc, right_doc, e, bullet_limit)
        return {
            "ok": True,
            "left_memory_id": left_memory_id,
            "right_memory_id": right_memory_id,
            "resolved_left_memory_id": resolved_left_id,
            "resolved_right_memory_id": resolved_right_id,
            **fallback,
        }


def _count_subtree_nodes(node: dict) -> int:
    """Count total descendant nodes (not leaf values) in a taxonomy subtree."""
    total = len(node)
    for key in node:
        total += _count_subtree_nodes(node[key]["_children"])
    return total


def _build_taxonomy_tree(
    cat_rows: list[dict],
    max_depth: Optional[int] = None,
    max_branch_nodes: Optional[int] = None,
) -> str:
    """
    Build an indented tree string from flat category_path rows.

    When max_depth or max_branch_nodes is set, deep/wide branches are collapsed
    with a '[+N more → explore_taxonomy(...)]' hint so the primer stays compact.
    """
    tree: dict = {}
    for r in cat_rows:
        path = r["category"]
        count = int(r["count"])
        parts = path.split(".")
        node = tree
        for part in parts:
            if part not in node:
                node[part] = {"_count": 0, "_children": {}}
            node = node[part]
            node["_count"] += count
            node = node["_children"]

    lines: list[str] = []

    def _render(node: dict, depth: int = 0, path_prefix: str = "") -> None:
        for key in sorted(node.keys()):
            info = node[key]
            children = info["_children"]
            count = info["_count"]
            current_path = f"{path_prefix}.{key}" if path_prefix else key
            indent = "│   " * depth + "├── " if depth > 0 else ""

            subtree_nodes = _count_subtree_nodes(children)
            should_collapse = bool(children) and (
                (max_depth is not None and depth >= max_depth)
                or (max_branch_nodes is not None and subtree_nodes > max_branch_nodes)
            )

            if should_collapse:
                lines.append(
                    f"{indent}{key}/ ({count}) [+{subtree_nodes} more → explore_taxonomy('{current_path}')]"
                )
            elif children:
                child_keys = sorted(children.keys())
                leaf_children = [k for k in child_keys if not children[k]["_children"]]
                branch_children = [k for k in child_keys if children[k]["_children"]]
                if leaf_children and not branch_children:
                    lines.append(f"{indent}{key}/ ({count}) — {', '.join(leaf_children)}")
                else:
                    lines.append(f"{indent}{key}/ ({count})")
                    _render(children, depth + 1, current_path)
            else:
                lines.append(f"{indent}{key} [{count}]")

    _render(tree)
    return "\n".join(lines)


async def explore_taxonomy(ctx: Context, path: str) -> dict[str, Any]:
    """
    Drill down into a specific taxonomy branch to see the full uncollapsed subtree.
    Use this when synthesize_system_primer shows a collapsed '[+N more]' branch.
    """
    logger.info("Tool invoked: explore_taxonomy (path: %s)", path)
    safe_segments = [sanitize_ltree_label(s) for s in path.split(".") if s.strip()]
    safe_path = ".".join(safe_segments) if safe_segments else "reference"

    try:
        db_pool = get_pool()
        async with db_pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT category_path::text AS category, COUNT(*) AS count
                FROM memories
                WHERE category_path <@ $1::ltree
                  AND supersedes_id IS NULL
                  AND archived_at IS NULL
                GROUP BY category_path
                ORDER BY category_path ASC
                """,
                safe_path,
            )

        if not rows:
            return {"ok": True, "path": safe_path, "tree": "(empty)", "total": 0}

        cats = [{"category": r["category"], "count": int(r["count"])} for r in rows]
        tree = _build_taxonomy_tree(cats)
        total = sum(c["count"] for c in cats)
        return {"ok": True, "path": safe_path, "tree": tree, "total": total, "categories": cats}
    except Exception as e:
        logger.error("Error in explore_taxonomy: %s\n%s", e, traceback.format_exc())
        return {"ok": False, "error": str(e)}
