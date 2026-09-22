"""MCP client pool — per-project MCP server connections.

Ports s20 lines 1531-1646 from reference/learn-claude-code/s20_comprehensive/code.py.
"""
from .client import MCPClient, MCPPool, normalize_mcp_name
from .http import HttpMCPClient, HttpMCPError, SseMCPClient
from .stdio import StdioMCPClient, StdioMCPError

__all__ = ["MCPClient", "MCPPool", "normalize_mcp_name",
           "StdioMCPClient", "StdioMCPError",
           "HttpMCPClient", "HttpMCPError", "SseMCPClient"]
