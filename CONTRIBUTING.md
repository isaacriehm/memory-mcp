# Contributing to memory-mcp

Thanks for your interest in contributing. This is a focused project — please open an issue before starting significant work so we can align on approach.

## Development Setup

**Prerequisites:** Python 3.11+, Docker + Docker Compose, a PostgreSQL instance with `pgvector`.

```bash
# Clone
git clone https://github.com/isaacriehm/memory-mcp.git
cd memory-mcp

# Create virtual environment
python3.11 -m venv .venv
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Configure environment
cp .env.example .env
# Edit .env — at minimum set DATABASE_URL and OPENAI_API_KEY

# Start PostgreSQL (easiest via Docker)
docker compose up -d memory-db

# Run the server
python -m server
```

The production MCP server is available at `http://localhost:8766/mcp`.
The admin MCP server is available at `http://localhost:8767/mcp`.

## Project Structure

```
server.py           Entry point — starts both MCP servers, background workers
config.py           All environment variables and OpenAI client
db.py               asyncpg pool, schema init, memory processing + dedup
llm.py              Embedding, extraction, conflict resolution via OpenAI
utils.py            Chunking, ID generation, helpers

tools/
  production.py     Production MCP server registration
  admin.py          Admin MCP server registration (superset of production)
  ingestion.py      memorize_context, check_ingestion_status
  search.py         search_memory, list_categories, explore_taxonomy, fetch_document
  context.py        initialize_context, trace_history, confirm_memory_validity, system primer
  crud.py           update_memory, delete_memory, recategorize_memory, bulk_move_category
  admin_tools.py    prune_history, export_memories, run_diagnostics, flush_staging

tools_context.py    Ephemeral context store tools (set/get/list/delete/extend)

scripts/
  export_memories.py      Export all memories to JSON
  visualize_memories.py   Generate interactive memory graph (memory_map.html)

backup/
  Dockerfile        Alpine image for PostgreSQL backup job
  backup.sh         pg_dump → git push to private repo
  run.sh            Loop scheduler
```

## Making Changes

### Adding a new MCP tool

1. Implement the async function in the appropriate `tools/` module
2. Register it in `tools/production.py` and/or `tools/admin.py`
3. Update the tool table in `README.md`
4. Add an entry to `CHANGELOG.md`

### Adding environment variables

1. Add the variable to `config.py` with a sensible default
2. Add it to `.env.example` with a comment
3. Document it in the env vars table in `README.md`

### Database schema changes

Schema is managed inline in `db.py`'s `init_db()` function using `CREATE TABLE IF NOT EXISTS` and `ALTER TABLE ADD COLUMN IF NOT EXISTS`. Migrations run automatically on startup. Follow this pattern — no migration framework is used.

## Code Style

- Follow the existing async/await patterns throughout
- Use the module-level `logger` from `config.py` for all logging
- Acquire connections from the pool via `async with get_pool().acquire() as conn`
- Keep tools focused — one clear responsibility per function

## Pull Requests

- Open an issue first for non-trivial changes
- Keep PRs small and focused
- Update `CHANGELOG.md` under `[Unreleased]`
- Test against a real PostgreSQL + pgvector instance before submitting

## Reporting Issues

Use the GitHub issue templates. Include logs, the version/commit, and your deployment method.
