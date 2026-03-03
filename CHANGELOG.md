# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
