"""MCP tool implementations.

Each tool is a thin shim over the corresponding ``OracleClient`` SDK
method. Tools live in separate modules for clarity + per-tool unit
test scoping. Registered as MCP tools in ``mcp_server.server``.
"""
