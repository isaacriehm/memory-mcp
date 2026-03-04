# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.7.0] - 2026-03-03

### Added

- Minimal OAuth bridge routes for connector compatibility on production server: `/authorize`, `/token`, `/.well-known/oauth-authorization-server`, and protected-resource metadata endpoints.
- New `oauth.py` module implementing auth code + PKCE validation and token exchange mapped to existing `API_KEY`.
- Optional OAuth bridge configuration knobs: `OAUTH_ALLOWED_REDIRECT_URIS` and `OAUTH_ISSUER`.

### Changed

- Production Bearer auth middleware now exempts OAuth bridge/discovery routes while continuing to enforce static `API_KEY` for MCP traffic.
- Documentation and `.env.example` now describe API-key-first auth with minimal OAuth bridge semantics (`client_id=api-key`, `client_secret=API_KEY`).

## [1.6.0] - 2026-03-03

### Added

- New persisted `memories.tier` column (`canonical`/`historical`/`ephemeral`) with partial index `memories_tier_idx`.
- New migration script `scripts/migrate_add_tier_column.py` to add/backfill tier values for existing memories and report tier distribution.
- New ingestion-time config `TIER_LLM_INFERENCE_ENABLED` (default `true`) to gate LLM tier suggestions without disabling ingestion.
- New retrieval-time config `HISTORICAL_BASE_SCORE_MULTIPLIER` (default `0.85`) to apply a tunable historical base-score penalty.
- `extract_semantic_sections` now returns `suggested_tier` and includes decision-record classification guidance.

### Changed

- Ingestion now resolves and writes tier on insert with precedence: explicit metadata tier override, LLM `suggested_tier`, then heuristic fallback.
- `memorize_context` ingestion queue now preserves optional caller metadata so explicit `metadata.tier` overrides reach insert-time tier resolution.
- `search_memory` now applies historical-tier penalty before feedback rerank and surfaces `raw_base_score` diagnostics when rerank diagnostics are enabled.
- `update_memory_metadata` now validates and writes `tier` directly to the authoritative column, enabling explicit promote/demote workflows.
- Decision-record extraction now enforces canonical classification and normalizes category to `projects.<project>.decisions`.
- System primer synthesis now surfaces active `*.decisions` records and includes a mandatory planner decision-memorization protocol.

## [1.5.0] - 2026-03-03

### Added

- New `retrieval_feedback` table for persistent query-memory outcome events (`+1` helpful, `-1` not helpful) with indexes for rerank lookups.
- New MCP tool `report_retrieval_outcome` (production and admin) to record retrieval outcomes without mutating memory content.
- New guarded feedback rerank pipeline in `search_memory` behind `FEEDBACK_RERANK_ENABLED` (default `false`), using time-decayed feedback and bounded score delta (`FEEDBACK_MAX_DELTA`).
- Tier safeguards for top-K (`CANONICAL_MIN_IN_TOPK`, `HISTORICAL_MIN_IN_TOPK`) and optional exploration slots (`FEEDBACK_EXPLORATION_SLOTS`) to reduce lock-in risk.

### Changed

- `search_memory` now returns optional diagnostics (`base_score`, `feedback_delta`, `feedback_signal`, `tier`) only when feedback rerank is enabled.
- Configuration/docs updated with explicit rollout and rollback controls for feedback reranking.
- Tier floor requirements now normalize against `top_k` to prevent over-subscribed floor constraints.
- Retrieval feedback now resolves superseded IDs to the latest active memory and applies scoped hashing by category/task to avoid cross-scope bleed.

## [1.4.0] - 2026-03-03

### Added

- New MCP tool `create_handoff_pack` (production and admin) that builds deterministic, execution-ready handoff packs and stores them under `handoff.<label>` in context store.
- Handoff pack generation now includes scoped memory search, recent context key capture, timeline signal snapshot, and optional contradiction audit snapshot.
- Input hygiene for handoff creation: label slug sanitization, bounded TTL/hour/item clamps, and deterministic resume prompt format.
- Async unit tests for handoff pack creation success path, resume prompt formatting, and label sanitization with overwrite (`ON CONFLICT`) behavior.

## [1.3.0] - 2026-03-03

### Added

- New read-only MCP tool `semantic_diff_memory` (production and admin) for semantic comparison between two memory IDs, returning concise `overview`, `added_points`, `removed_points`, `changed_points`, and `risk_notes`.
- Deterministic fallback mode for `semantic_diff_memory` when LLM calls fail, including structural change signals and a surfaced fallback error.
- Input validation for `max_bullets` to return a clean error on invalid values instead of raising.
- Tests for `semantic_diff_memory` invalid `max_bullets` input and left/right lookup failure branches.

## [1.2.0] - 2026-03-03

### Added

- New read-only MCP tool `decision_timeline` (production and admin) that merges memory lifecycle events with conflict audit history into a deterministic chronological timeline.

## [1.1.0] - 2026-03-03

### Added

- Persisted `conflict_audit_events` table for ingestion-time contradiction decisions (`supersedes`/`merges`) with similarity, taxonomy path, and compact JSON details.
- New read-only MCP tool `contradiction_audit` (available on production and admin servers) for querying contradiction history with limit, category, resolution, and time filters.
- Conflict evaluation contract now supports optional `reason_summary` and `changed_claims` fields for transparent audit payloads.

## [1.0.0] - 2026-02-27

### Added

- Initial public release
- Semantic memory storage with pgvector hybrid search (vector + BM25 with RRF)
- Autonomous ingestion pipeline: chunking, embedding, categorization, deduplication
- Hierarchical taxonomy via PostgreSQL `ltree` with auto-assignment
- Dual MCP server architecture: production (port 8766) and admin (port 8767)
- System primer with automatic regeneration on memory threshold
- Ephemeral context store with TTL-based expiry
- Memory graph edges (`sequence_next`, `relates_to`, `supersedes`)
- `fetch_document` for full document reconstruction via edge traversal
- `trace_history` for inspecting supersession chains
- `confirm_memory_validity` for periodic verification prompts
- `explore_taxonomy` for drilling into collapsed taxonomy branches
- TTL daemon for soft-archiving and hard-deletion of expired memories
- Async ingestion worker with staging queue and crash recovery
- PostgreSQL backup service with configurable GitHub push interval
- Memory export script and interactive graph visualization
- Docker Compose stack with multi-platform images (amd64/arm64)
- GitHub Actions workflow for automated GHCR image builds
