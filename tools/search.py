"""Search and taxonomy retrieval tools."""

import json
import traceback
from typing import Optional, Any
from uuid import UUID

from fastmcp import Context

from config import logger, DEFAULT_SEARCH_LIMIT
from utils import _now, _vector_literal, _add_ttl_warning, sanitize_ltree_path, sanitize_ltree_label
from llm import embed
from db import get_pool


async def search_memory(
    ctx: Context, query: str, category_path: Optional[str] = None, limit: int = DEFAULT_SEARCH_LIMIT
) -> dict[str, Any]:
    """Semantically search the knowledge base.
    Hybrid Retrieval: Use 'category_path' to filter by domains (e.g., 'projects.myapp' or 'user').
    If unsure of the exact path, leave category_path null to search globally, or use list_categories first.
    Results include 'tags' in the metadata for exact contextual matches."""
    logger.info("Tool invoked: search_memory (query: '%s', category_path: '%s', limit: %d)", query, category_path, limit)

    if not query or not isinstance(query, str) or not query.strip():
        return {"ok": False, "error": "query must be a non-empty string"}

    limit = max(1, min(int(limit or DEFAULT_SEARCH_LIMIT), 100))
    try:
        vec = await embed(query)
        vec_lit = _vector_literal(vec)

        db_pool = get_pool()
        async with db_pool.acquire() as conn:
            where_clause = "m.supersedes_id IS NULL AND m.archived_at IS NULL"
            params = [vec_lit, limit, query]
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
                    SELECT m.id, m.content, m.category_path::text, m.supersedes_id, m.created_at, m.updated_at, m.metadata::text,
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

            if rows:
                ids = [r["id"] for r in rows]
                await conn.execute("UPDATE memories SET last_accessed_at = $1 WHERE id = ANY($2)", _now(), ids)

                results = []
                for r in rows:
                    full_content = r["content"]
                    if r["prev_content"]:
                        full_content = f"...{r['prev_content']}\n\n{full_content}"
                    if r["next_content"]:
                        full_content = f"{full_content}\n\n{r['next_content']}..."

                    item = {
                        "id": str(r["id"]),
                        "content": full_content,
                        "category_path": r["category_path"],
                        "score": round(float(r["rrf_score"]), 6),
                        "semantic_score": round(float(r["semantic_score"]), 6),
                        "keyword_score": round(float(r["keyword_score"]), 6),
                        "created_at": r["created_at"].isoformat(),
                        "updated_at": r["updated_at"].isoformat(),
                        "metadata": json.loads(r["metadata"]) if r["metadata"] else {},
                    }
                    _add_ttl_warning(item, r["updated_at"])
                    results.append(item)
            else:
                results = []

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
