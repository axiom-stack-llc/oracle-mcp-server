"""Phase G1.1 — MCP server for Axiom Oracle.

Exposes Oracle SDK functionality as MCP tools, callable from any
MCP-compatible AI agent (Claude Desktop, Cursor, Cline, Continue, Goose,
direct API consumers, etc.) without custom SDK integration.

Top-level: ``mcp_server.server.app`` is the ASGI app to deploy. Tools live
in ``mcp_server.tools.*``. Bearer-token auth lives in ``mcp_server.auth``.
Token generation CLI: ``python -m mcp_server.scripts.generate_token``.
"""

__version__ = "0.1.0-g1.1"
