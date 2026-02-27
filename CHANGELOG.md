# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
