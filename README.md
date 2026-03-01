# memory-mcp

**Persistent, self-organizing semantic memory for AI agents — served as an MCP server.**

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Docker](https://img.shields.io/badge/docker-ghcr.io-blue?logo=docker)](https://github.com/isaacriehm/memory-mcp/pkgs/container/memory-mcp)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/)

---

## What is this?

memory-mcp is a [Model Context Protocol](https://modelcontextprotocol.io) server that gives AI agents durable, searchable memory backed by PostgreSQL and `pgvector`. Drop it into any MCP-compatible client (Claude Code, Cursor, Windsurf, etc.) and your agent gains the ability to remember, retrieve, and reason over information across sessions — without you managing any schema or storage logic.

**What it does autonomously:**
- Chunks and embeds incoming text
- Categorizes memories into a hierarchical taxonomy (`ltree` dot-paths)
- Deduplicates against existing memories and resolves conflicts
- Synthesizes a **System Primer** — a compressed, always-current summary of everything it knows — and surfaces it at session start
- Expires stale memories via TTL and prompts for verification of aging facts

---

## Why memory-mcp?

| | memory-mcp | Simple vector DB | LangChain / LlamaIndex memory |
|---|---|---|---|
| Schema management | Automatic | Manual | Manual |
| Deduplication | Semantic + LLM | None | None |
| Taxonomy | Auto-assigned ltree | None | None |
| Session bootstrap | System Primer | Manual RAG | Manual |
| Conflict resolution | LLM-evaluated | None | None |
| Ephemeral context | Built-in (TTL store) | No | No |
| Self-hostable | Yes (Docker) | Varies | No |
| MCP-native | Yes | No | No |

---

## Architecture

```
AI Agent (Claude Code / Cursor / Windsurf)
        │  HTTP (MCP — Streamable HTTP)
        ▼
┌──────────────────────────────────────────┐
│              server.py                    │
│  ┌─────────────────┐ ┌─────────────────┐ │
│  │ Production MCP  │ │   Admin MCP     │ │
│  │   :8766/mcp     │ │   :8767/mcp     │ │
│  └────────┬────────┘ └────────┬────────┘ │
│           │  tools/           │           │
│  ┌────────▼──────────────────▼────────┐  │
│  │  ingestion · search · context      │  │
│  │  crud · admin_tools · context_store│  │
│  └────────────────┬───────────────────┘  │
│                   │                       │
│  ┌────────────────▼───────────────────┐  │
│  │         Background Workers          │  │
│  │  Ingestion Queue · TTL Daemon       │  │
│  │  System Primer Auto-Regeneration    │  │
│  └────────────────┬───────────────────┘  │
└───────────────────┼──────────────────────┘
                    │  asyncpg
                    ▼
         PostgreSQL + pgvector
         ┌─────────────────┐
         │ memories        │  chunks, embeddings, ltree paths
         │ memory_edges    │  sequence_next, relates_to, supersedes
         │ ingestion_staging│ async job queue
         │ context_store   │  ephemeral TTL store
         └─────────────────┘
                    │
         ┌──────────▼──────────┐
         │  Backup Service     │  pg_dump → private GitHub repo
         └─────────────────────┘
```

**Two servers, one process:**
- **Production** (`:8766`) — tools safe for the agent to call freely
- **Admin** (`:8767`) — superset including destructive tools (delete, prune, bulk-move). Point your agent at production; use admin for maintenance.

---

## Quickstart (Docker)

**Prerequisites:** Docker + Docker Compose, an OpenAI API key.

```bash
# 1. Clone
git clone https://github.com/isaacriehm/memory-mcp.git
cd memory-mcp

# 2. Configure
cp .env.example .env
$EDITOR .env   # set OPENAI_API_KEY and DB_PASSWORD at minimum

# 3. Start
docker compose up -d

# Production MCP endpoint: http://localhost:8766/mcp
# Admin MCP endpoint:      http://localhost:8767/mcp
```

To rebuild after code changes:

```bash
docker compose up -d --build memory-api
```

---

## Connecting to an MCP Client

### Claude Code

Add to your project's `.claude/settings.json` or `~/.claude/settings.json`:

```json
{
  "mcpServers": {
    "memory": {
      "type": "http",
      "url": "http://localhost:8766/mcp"
    }
  }
}
```

Or via the CLI:

```bash
claude mcp add memory --transport http http://localhost:8766/mcp
```

Then add this instruction to your `CLAUDE.md` so the agent always bootstraps memory at session start:

```markdown
## Memory
At the start of every session, call `initialize_context` before anything else.
This returns your System Primer — your identity, current knowledge taxonomy, and retrieval guide.
Always consult it before answering questions about prior context.
```

### Cursor / Windsurf

Add to your MCP settings (`.cursor/mcp.json` or equivalent):

```json
{
  "mcpServers": {
    "memory": {
      "url": "http://localhost:8766/mcp"
    }
  }
}
```

---

## MCP Tools

### Production Tools (`:8766`)

| Tool | Description |
|---|---|
| `initialize_context` | **Call first every session.** Returns the System Primer + verification prompts for aging memories. |
| `memorize_context` | Ingest raw text. Automatically chunks, embeds, categorizes, and deduplicates. Supports `ttl_days`. |
| `check_ingestion_status` | Poll async ingestion job by `job_id`. Returns `pending`, `processing`, `complete`, or `failed`. |
| `search_memory` | Hybrid vector + BM25 search with Reciprocal Rank Fusion. Filter by `category_path`. |
| `list_categories` | Return all occupied taxonomy paths with memory counts. |
| `explore_taxonomy` | Drill into a collapsed `[+N more]` branch from `list_categories`. |
| `fetch_document` | Reconstruct a full document by following `sequence_next` edges from a memory ID. |
| `trace_history` | Inspect the full supersession chain (oldest → newest) for a memory. |
| `confirm_memory_validity` | Confirm an aging memory is still accurate. Advances its `verify_after` date. |
| `update_memory` | Rewrite a memory's content in-place (preserves identity, edges, history). |
| `set_context` | Write a key/value pair to the ephemeral context store with a TTL. |
| `get_context` | Retrieve an ephemeral context entry by key. |
| `list_context_keys` | List active (non-expired) context keys, optionally filtered by scope. |
| `delete_context` | Explicitly delete a context entry before its TTL expires. |
| `extend_context_ttl` | Push a context entry's expiry forward by N hours. |

### Admin-Only Tools (`:8767`)

| Tool | Description |
|---|---|
| `delete_memory` | Hard-delete a memory by ID (cascades edges). |
| `prune_history` | Batch-delete superseded memories older than N days. |
| `export_memories` | Export all active memories to JSON. |
| `recategorize_memory` | Move a single memory to a new taxonomy path. |
| `bulk_move_category` | Move an entire taxonomy branch (e.g. `old.prefix` → `new.prefix`). |
| `update_memory_metadata` | Patch a memory's metadata JSONB in-place. |
| `run_diagnostics` | Report on pool health, memory counts, ingestion queue depth. |
| `get_ingestion_stats` | Breakdown of ingestion job statuses. |
| `flush_staging` | Clear all completed/failed staging jobs immediately. |

---

## Taxonomy

Memories are organized into a dot-path hierarchy using PostgreSQL `ltree`. The system assigns paths automatically during ingestion. You can override with `recategorize_memory` or `bulk_move_category`.

**Example paths:**

```
user.profile.personal
user.health.medical
projects.myapp.architecture
projects.myapp.decisions
organizations.acme.business
concepts.ai.behavior
reference.system.primer     ← auto-generated System Primer lives here
```

Search is subtree-aware — passing `category_path: "projects.myapp"` returns everything under that branch.

---

## System Primer

`initialize_context` returns a synthesized summary stored at `reference.system.primer`. It includes:

- A compressed user/agent profile
- The full taxonomy tree with memory counts
- Retrieval guidance

The primer auto-regenerates in the background when ≥10 new memories are ingested or when the previous primer is older than 1 hour. You can force regeneration via the admin tool `synthesize_system_primer`.

---

## Environment Variables

Copy `.env.example` to `.env` and fill in your values.

### Required

| Variable | Description |
|---|---|
| `DATABASE_URL` | PostgreSQL connection string (e.g. `postgresql://user:pass@localhost:5432/memory`) |
| `OPENAI_API_KEY` | OpenAI API key for embeddings and LLM calls |
| `DB_PASSWORD` | PostgreSQL password (used by Docker Compose) |

### Optional — Models & Embeddings

| Variable | Default | Description |
|---|---|---|
| `EMBEDDING_MODEL` | `text-embedding-3-small` | OpenAI embedding model |
| `EXTRACT_MODEL` | `gpt-5-mini` | LLM for semantic section extraction and categorization |
| `CONFLICT_MODEL` | `gpt-5-nano` | LLM for conflict/dedup evaluation |
| `EMBED_DIM` | `1536` | Embedding vector dimension (must match model) |

### Optional — Search & Limits

| Variable | Default | Description |
|---|---|---|
| `DEFAULT_SEARCH_LIMIT` | `10` | Default result count for `search_memory` |
| `DEFAULT_LIST_LIMIT` | `50` | Default result count for `list_categories` |
| `DUP_THRESHOLD` | `0.95` | Cosine similarity threshold for deduplication |
| `CONFLICT_THRESHOLD` | `0.55` | Similarity threshold for conflict detection |
| `RELATES_TO_THRESHOLD` | `0.65` | Similarity threshold for `relates_to` edge creation |
| `MIN_SECTION_LENGTH` | `100` | Minimum character length for a chunk to be stored |
| `MAX_TAXONOMY_PATHS` | `40` | Max taxonomy paths assigned per ingestion |

### Optional — OpenAI & Concurrency

| Variable | Default | Description |
|---|---|---|
| `OPENAI_TIMEOUT_S` | `60` | Per-request OpenAI timeout in seconds |
| `OPENAI_MAX_RETRIES` | `5` | Exponential-backoff retry limit |
| `MAX_CONCURRENT_API_CALLS` | `5` | Semaphore for parallel OpenAI requests |
| `EXTRACT_REASONING` | `low` | Reasoning effort for extraction LLM |
| `CONFLICT_REASONING` | `minimal` | Reasoning effort for conflict LLM |

### Optional — Database

| Variable | Default | Description |
|---|---|---|
| `PG_POOL_MIN` | `1` | asyncpg minimum pool connections |
| `PG_POOL_MAX` | `10` | asyncpg maximum pool connections |
| `STAGING_RETENTION_DAYS` | `7` | Days to retain completed/failed staging jobs |

### Optional — Authentication

| Variable | Default | Description |
|---|---|---|
| `API_KEY` | _(unset)_ | Static Bearer token for the production server. Unset = no auth (trusted network). See [External Provider Auth](#external-provider-auth). |

### Optional — Server

| Variable | Default | Description |
|---|---|---|
| `PRODUCTION_PORT` | `8766` | Production MCP server port |
| `ADMIN_PORT` | `8767` | Admin MCP server port |
| `MCP_TRANSPORT` | `streamable-http` | FastMCP transport mode |
| `FASTMCP_JSON_RESPONSE` | — | Set to `1` to force JSON responses |
| `LOG_LEVEL` | `INFO` | `DEBUG` / `INFO` / `WARNING` |

### Optional — System Primer

| Variable | Default | Description |
|---|---|---|
| `PRIMER_UPDATE_MAX_AGE_S` | `3600` | Max seconds before auto primer regeneration |

### Optional — Context Store

| Variable | Default | Description |
|---|---|---|
| `CONTEXT_DEFAULT_TTL_HOURS` | `24` | Default TTL for context store entries |
| `CONTEXT_MAX_VALUE_LENGTH` | `50000` | Max character length for context values |
| `CONTEXT_MAX_KEY_LENGTH` | `200` | Max character length for context keys |

### Optional — Backup Service

| Variable | Description |
|---|---|
| `GITHUB_PAT` | GitHub Personal Access Token with `repo` scope |
| `GITHUB_BACKUP_REPO` | Target repo in `owner/repo` format |
| `BACKUP_INTERVAL_SECONDS` | Seconds between backups (default: `21600` = 6 hours) |

---

## External Provider Auth

By default the production server runs without authentication — suitable when access is restricted to a trusted network (WireGuard, Tailscale, etc.).

To allow external AI providers (ChatGPT Actions, any public-internet client) to connect securely, set `API_KEY` in your `.env`:

```bash
# Generate a secure random token
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

```env
API_KEY=your-generated-token
```

Every request to the production server will then require `Authorization: Bearer <token>`. The admin server (`:8767`) is never exposed publicly and has no auth requirement.

**MCP client config (external, with auth):**

```json
{
  "mcpServers": {
    "memory": {
      "type": "http",
      "url": "https://your-public-url/mcp",
      "headers": {
        "Authorization": "Bearer your-generated-token"
      }
    }
  }
}
```

**WireGuard / trusted network (no auth):**

```json
{
  "mcpServers": {
    "memory": {
      "type": "http",
      "url": "http://10.x.x.x:8766/mcp"
    }
  }
}
```

The same server handles both — `API_KEY` is the only switch.

---

## Running Locally (Development)

Requirements: Python 3.11+, PostgreSQL with `pgvector`.

```bash
# Create and activate virtual environment
python3.11 -m venv .venv
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Configure
cp .env.example .env
$EDITOR .env

# Start the server
python -m server
# Production: http://0.0.0.0:8766
# Admin:      http://0.0.0.0:8767
```

---

## Backup Service

The `backup/` directory contains a containerized PostgreSQL backup job that:

1. Runs `pg_dump` on the configured interval (default: every 6 hours)
2. Commits the dump to a private GitHub repository

The backup service starts automatically with `docker compose up`. Set `GITHUB_PAT` and `GITHUB_BACKUP_REPO` in your `.env` to enable it. If those variables are unset, the service will error on startup — remove the `memory-backup` service from `docker-compose.yml` if you don't need backups.

---

## CLI Scripts

Standalone scripts in `scripts/` (require `DATABASE_URL` in environment):

```bash
# Export all memories to a timestamped JSON file
python scripts/export_memories.py

# Generate an interactive graph visualization
python scripts/visualize_memories.py
open memory_map.html
```

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

[MIT](LICENSE)
