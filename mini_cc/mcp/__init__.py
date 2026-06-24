"""MCP client pool — per-project MCP server connections.

Ports s20 lines 1531-1646 from s20_comprehensive/code.py.
"""
from .client import MCPClient, MCPPool, normalize_mcp_name
from .stdio import StdioMCPClient, StdioMCPError

__all__ = ["MCPClient", "MCPPool", "normalize_mcp_name",
           "StdioMCPClient", "StdioMCPError"]
