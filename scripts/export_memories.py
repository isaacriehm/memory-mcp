#!/usr/bin/env python3
"""
Standalone CLI to export memories to JSON.
Run without the MCP server: python scripts/export_memories.py
Requires DATABASE_URL in environment.
"""
import asyncio
import json
import os
import sys
import asyncpg
from datetime import datetime


async def export():
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        print("ERROR: DATABASE_URL environment variable not set.", file=sys.stderr)
        sys.exit(1)

    conn = await asyncpg.connect(database_url)
    try:
        rows = await conn.fetch(
            """
            SELECT id, content, category_path::text, metadata::text, created_at
            FROM memories
            WHERE supersedes_id IS NULL AND archived_at IS NULL
            ORDER BY category_path ASC
            """
        )

        data = []
        for r in rows:
            data.append(
                {
                    "id": str(r["id"]),
                    "category": r["category_path"],
                    "content": r["content"],
                    "metadata": json.loads(r["metadata"]) if r["metadata"] else {},
                    "created_at": r["created_at"].isoformat(),
                }
            )

        filename = f"memory_export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        with open(filename, "w") as f:
            json.dump(data, f, indent=2)

        print(f"Exported {len(data)} memories to {filename}")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(export())
