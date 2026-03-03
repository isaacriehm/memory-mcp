from fastmcp import FastMCP
from .ingestion import memorize_context, check_ingestion_status
from .search import search_memory, list_categories, explore_taxonomy, fetch_document
from .context import initialize_context, trace_history, confirm_memory_validity, decision_timeline
from .crud import update_memory
from .admin_tools import contradiction_audit
from tools_context import register_context_tools

mcp = FastMCP("memory-mcp-production")

mcp.tool()(initialize_context)
mcp.tool()(memorize_context)
mcp.tool()(check_ingestion_status)
mcp.tool()(search_memory)
mcp.tool()(list_categories)
mcp.tool()(explore_taxonomy)
mcp.tool()(fetch_document)
mcp.tool()(trace_history)
mcp.tool()(decision_timeline)
mcp.tool()(confirm_memory_validity)
mcp.tool()(update_memory)
mcp.tool()(contradiction_audit)

register_context_tools(mcp)
