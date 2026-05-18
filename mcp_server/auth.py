"""Bearer-token auth + tier policy for the Axiom Oracle MCP server.

V1 auth model (locked per Phase G1.1 directive Q4):

  - Anonymous tier allowed for read-only tools when ``AXIOM_MCP_ALLOW_ANONYMOUS=true``:
      axiom_quote_fee
      axiom_fetch_audit_reference
      axiom_fetch_attestation

  - Authenticated tier (required regardless of anonymous flag) for write /
    SOL-spending tools:
      axiom_request_attestation

  - Bearer tokens stored in ``AXIOM_MCP_API_KEYS`` env var (comma-separated
    allowlist). In production this env var is sourced from AWS Secrets
    Manager via the ECS task definition's ``secrets`` block.

  - Rate limiting for the anonymous tier is enforced at the API Gateway
    layer (~1000 req/day per source IP); the MCP server itself does NOT
    rate-limit. Auth at the MCP server is binary (allow / deny).

  - V1.5 migration path: OAuth 2.1 resource server via the official MCP
    SDK's ``TokenVerifier`` protocol, paired with an authorization server
    in the Developer Portal. See README §"Auth: V1 → V1.5 migration".
"""

from __future__ import annotations

import json
import logging
import os
from typing import Awaitable, Callable

logger = logging.getLogger(__name__)


ANONYMOUS_TOOLS: frozenset[str] = frozenset({
    "axiom_quote_fee",
    "axiom_fetch_audit_reference",
    "axiom_fetch_attestation",
})

AUTH_REQUIRED_TOOLS: frozenset[str] = frozenset({
    "axiom_request_attestation",
})


def _allow_anonymous_enabled() -> bool:
    """Return True if AXIOM_MCP_ALLOW_ANONYMOUS env var is truthy."""
    v = os.environ.get("AXIOM_MCP_ALLOW_ANONYMOUS", "").strip().lower()
    return v in {"1", "true", "yes", "on"}


def _api_keys() -> set[str]:
    """Parse the comma-separated AXIOM_MCP_API_KEYS env var into a set.

    Empty / whitespace tokens are ignored. Returns an empty set if the
    env var is unset — meaning NO authenticated requests are permitted
    until the env var is populated.
    """
    raw = os.environ.get("AXIOM_MCP_API_KEYS", "")
    return {t for t in (s.strip() for s in raw.split(",")) if t}


def _extract_bearer_token(auth_header: str) -> str | None:
    """Parse ``Authorization: Bearer <token>``. Returns None on bad form."""
    if not auth_header:
        return None
    auth_header = auth_header.strip()
    parts = auth_header.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    return parts[1].strip() or None


def is_authorized(auth_header: str, tool_name: str) -> tuple[bool, str | None]:
    """Decide whether to allow a tool call.

    Returns (allowed, reason). ``reason`` is None on allow, a short
    explanation string on deny (suitable for the 401 response body).
    """
    if tool_name in ANONYMOUS_TOOLS and _allow_anonymous_enabled():
        return True, None

    # Either:
    #  - tool requires auth (AUTH_REQUIRED_TOOLS), regardless of anonymous flag
    #  - tool is in ANONYMOUS_TOOLS but anonymous tier is disabled
    token = _extract_bearer_token(auth_header)
    if token is None:
        return False, (
            f"tool {tool_name} requires Authorization: Bearer <token>"
            if tool_name in AUTH_REQUIRED_TOOLS
            else f"anonymous tier disabled; tool {tool_name} requires bearer token"
        )

    valid = _api_keys()
    if not valid:
        return False, "no API keys configured on server (AXIOM_MCP_API_KEYS is unset)"

    if token not in valid:
        return False, "bearer token not in allowlist"

    return True, None


def _extract_tool_name(body_bytes: bytes) -> str | None:
    """Extract the tool name from a JSON-RPC ``tools/call`` request body.

    Returns None if the body is not a JSON-RPC tools/call (i.e., the
    request is initialize / tools/list / something else that doesn't
    require tool-level auth).
    """
    try:
        msg = json.loads(body_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(msg, dict):
        return None
    if msg.get("method") != "tools/call":
        return None
    params = msg.get("params") or {}
    if not isinstance(params, dict):
        return None
    name = params.get("name")
    return name if isinstance(name, str) else None


class BearerAuthMiddleware:
    """ASGI middleware that gates MCP tool calls by bearer token.

    Pass-through for non-HTTP scopes + non-POST methods + non-tool-call
    JSON-RPC methods (initialize, tools/list, etc. don't require auth).
    Buffers the request body once, inspects the JSON-RPC method, and
    either passes through (replaying the buffered body) or short-circuits
    with a 401 / 403.
    """

    def __init__(self, app: Callable) -> None:
        self.app = app

    async def __call__(
        self,
        scope: dict,
        receive: Callable[[], Awaitable[dict]],
        send: Callable[[dict], Awaitable[None]],
    ) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        if scope.get("method") != "POST":
            await self.app(scope, receive, send)
            return

        # Buffer body. Use list of chunks so we can replay them downstream.
        chunks: list[bytes] = []
        more_body = True
        while more_body:
            msg = await receive()
            if msg.get("type") != "http.request":
                # Disconnect or other; let downstream handle.
                async def replay_disconnect():
                    return msg
                await self.app(scope, replay_disconnect, send)
                return
            chunks.append(msg.get("body", b"") or b"")
            more_body = bool(msg.get("more_body", False))
        body = b"".join(chunks)

        tool_name = _extract_tool_name(body)

        # If this is not a tools/call (e.g., initialize / tools/list), pass
        # through unconditionally — tool discovery + protocol handshake are
        # unauthenticated by design.
        if tool_name is None:
            await self.app(scope, _replay_factory(body), send)
            return

        # Inspect Authorization header.
        headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
        auth_header = headers.get("authorization", "")
        allowed, reason = is_authorized(auth_header, tool_name)
        if not allowed:
            await _send_401(send, tool_name, reason or "unauthorized")
            return

        await self.app(scope, _replay_factory(body), send)


def _replay_factory(body: bytes) -> Callable[[], Awaitable[dict]]:
    """Return a `receive` coroutine that emits the buffered body once."""
    sent = False

    async def replay() -> dict:
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.disconnect"}

    return replay


async def _send_401(send: Callable[[dict], Awaitable[None]], tool_name: str, reason: str) -> None:
    """Emit a JSON-RPC-shaped 401 response.

    JSON-RPC error code -32001 = "Server-defined: Unauthorized". MCP clients
    that understand this map it back to a tool-call failure with the
    reason string.
    """
    payload = json.dumps({
        "jsonrpc": "2.0",
        "error": {
            "code": -32001,
            "message": f"Unauthorized: {reason}",
            "data": {"tool": tool_name},
        },
        "id": None,
    }).encode("utf-8")
    await send({
        "type": "http.response.start",
        "status": 401,
        "headers": [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(payload)).encode()),
            (b"www-authenticate", b'Bearer realm="axiom-oracle-mcp"'),
        ],
    })
    await send({
        "type": "http.response.body",
        "body": payload,
    })
