from fastmcp import FastMCP
from .ingestion import memorize_context, check_ingestion_status
from .search import search_memory, list_categories, explore_taxonomy, fetch_document
from .context import initialize_context, trace_history, confirm_memory_validity
from .crud import update_memory, delete_memory, recategorize_memory, bulk_move_category, update_memory_metadata
from .admin_tools import prune_history, export_memories, run_diagnostics, get_ingestion_stats, flush_staging
from tools_context import register_context_tools

mcp = FastMCP("memory-mcp-admin")

mcp.tool()(initialize_context)
mcp.tool()(memorize_context)
mcp.tool()(check_ingestion_status)
mcp.tool()(search_memory)
mcp.tool()(list_categories)
mcp.tool()(explore_taxonomy)
mcp.tool()(fetch_document)
mcp.tool()(trace_history)
mcp.tool()(confirm_memory_validity)
mcp.tool()(update_memory)

# Admin only
mcp.tool()(delete_memory)
mcp.tool()(prune_history)
mcp.tool()(export_memories)
mcp.tool()(recategorize_memory)
mcp.tool()(bulk_move_category)
mcp.tool()(update_memory_metadata)

mcp.tool()(run_diagnostics)
mcp.tool()(get_ingestion_stats)
mcp.tool()(flush_staging)

register_context_tools(mcp)
