"""Axiom Oracle MCP server — main entry point.

Constructs a FastMCP server with 4 tools, wraps it in
``BearerAuthMiddleware``, and exposes the ASGI app as ``app`` for
uvicorn / gunicorn / hypercorn to serve. The deployed container's
``CMD`` runs ``uvicorn mcp_server.server:app --host 0.0.0.0 --port 8080``.

Configuration via env vars:

    AXIOM_ORACLE_API_URL          override the listener endpoint
                                  (default: https://oracle-api.axiomstack.dev)
    AXIOM_MCP_API_KEYS            comma-separated bearer-token allowlist
                                  for authenticated-tier tool calls
    AXIOM_MCP_ALLOW_ANONYMOUS     truthy → anonymous-tier reads allowed
                                  (default: false)
    LISTENER_HTTP_PORT            unused here; documented for symmetry
                                  with the listener service env shape

Module-load semantics:

    The OracleClient is constructed lazily on first tool invocation
    (not at import) so that the server's HTTP listen socket binds
    BEFORE any network-dependent init runs. This ordering matters for
    container deployments behind a load balancer whose health probe
    must succeed within the container's start grace window.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from mcp_server.auth import BearerAuthMiddleware
from mcp_server.tools.fetch_attestation import fetch_attestation_impl
from mcp_server.tools.fetch_audit_reference import fetch_audit_reference_impl
from mcp_server.tools.quote_fee import quote_fee_impl
from mcp_server.tools.request_attestation import request_attestation_impl

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# OracleClient singleton — lazy-constructed on first tool call.
# ---------------------------------------------------------------------------

_oracle_client: Any | None = None


def _get_client() -> Any:
    """Lazy-construct the OracleClient on first tool call.

    The SDK constructor auto-loads the operator wallet from a secret
    store (AWS SSM SecureString in the canonical deployment) using the
    runtime task identity. If the secret store is unreachable, the
    wallet attribute is None and tools that require a wallet
    (``axiom_request_attestation``) raise OracleError when invoked.
    Read-only tools work without a wallet.
    """
    global _oracle_client
    if _oracle_client is None:
        # Lazy import avoids pulling solana / solders at server import time;
        # SDK module load takes ~0.5s due to those deps.
        from integrations.oracle_compliance.client import OracleClient
        _oracle_client = OracleClient()
    return _oracle_client


# ---------------------------------------------------------------------------
# FastMCP server + tool registration.
# ---------------------------------------------------------------------------

# stateless_http=True + json_response=True per Phase 1 recon (Q1).
# Production-recommended transport per MCP spec 2025-11-25.
#
# transport_security explicit override: FastMCP auto-enables DNS-rebinding
# protection when host defaults to 127.0.0.1, with allowed_hosts limited to
# localhost variants. Behind API Gateway the Host header is the public domain
# (or the raw execute-api URL), so we configure the allow-list ourselves.
# Allowed hosts come from $AXIOM_MCP_ALLOWED_HOSTS (comma-separated) plus
# localhost wildcards for in-container probes.
_extra_hosts = [h.strip() for h in os.environ.get("AXIOM_MCP_ALLOWED_HOSTS", "").split(",") if h.strip()]
mcp = FastMCP(
    name="axiom-oracle",
    instructions=(
        "Axiom Oracle: on-chain attestation infrastructure for Solana. "
        "Use these tools to query attestation data, get current fee floors, "
        "verify operational integrity anchors, and (with auth) request "
        "fresh attestations on devnet."
    ),
    stateless_http=True,
    json_response=True,
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[
            "mcp.axiomstack.dev",
            "localhost:*",
            "127.0.0.1:*",
            *_extra_hosts,
        ],
        # allowed_origins=[] → Origin header is optional; only validated when
        # present. CLI MCP clients (Claude Desktop, Cursor, Cline) omit Origin,
        # so the V1 deploy stays compatible. Lock down origins in V1.5 when
        # browser-based clients arrive.
        allowed_origins=[],
    ),
)


@mcp.tool()
def axiom_quote_fee(
    asset_class: int,
    data_flags: int = 0xFF,
    margin_pct: int = 60,
) -> dict[str, Any]:
    """Return the listener's current fee threshold for a fresh attestation
    request.

    The Axiom Oracle listener applies a USD-anchored dynamic fee floor —
    call this BEFORE axiom_request_attestation to learn the current
    fee_lamports value to submit. The threshold floats with SOL price.

    Args:
        asset_class: Oracle asset class. 1=real_estate, 2=equity,
                     3=fixed_income, 4=macro, 5=commodity. V1 supports
                     1 and 2 on the request path.
        data_flags: u8 bitfield of which data layers to fetch. Default
                    0xFF (all 8 layers). 0 is normalized to 31 by the
                    listener.
        margin_pct: Target margin on vendor USD cost. Default 60.

    Returns:
        dict with fee_lamports (int), fiat_cost_usd (float), margin_pct
        (int), sol_price_usd (float), computed_at (ISO 8601), cached
        (bool).

    Anonymous-tier allowed.
    """
    return quote_fee_impl(
        _get_client(),
        asset_class=asset_class,
        data_flags=data_flags,
        margin_pct=margin_pct,
    )


@mcp.tool()
def axiom_fetch_audit_reference(timeout_seconds: float = 3.0) -> dict[str, Any]:
    """Capture the listener's current operational-integrity anchor for
    audit-chain records.

    Returns the locked Phase B schema: either ``oracle_anchor_status:
    "anchored"`` with a freshly-observed Master-Broker-signed Solana
    TX + PDA, or ``oracle_anchor_status: "unanchored"`` with an explicit
    failure reason. Never raises — fail-open by design.

    Args:
        timeout_seconds: HTTP timeout for the underlying /health call.

    Returns:
        dict matching the CXO-locked audit-record schema (see
        docs/integration/PHASE_B_AUDIT_CHAIN_DIRECTIVE.md §A).

    Anonymous-tier allowed.
    """
    return fetch_audit_reference_impl(_get_client(), timeout_seconds=timeout_seconds)


@mcp.tool()
def axiom_fetch_attestation(pda_address: str) -> dict[str, Any]:
    """Read an existing on-chain attestation by its PDA address.

    Single ``getAccountInfo`` RPC call; read-only; no on-chain TX; no
    fee. Use this when you already know the PDA of an asset (e.g., from
    a prior request or from off-chain knowledge of the asset_class +
    asset_id mapping).

    Args:
        pda_address: base58-encoded Solana PDA address.

    Returns:
        dict with asset_class, asset_id, asset_data (variant-typed),
        attestations (list of provider records with timestamps + scores),
        latest_snapshot_hash, manual_audit_required, bump.

    Raises:
        OraclePdaNotFound if the PDA has no account OR the data is not
        a valid AssetStateV2.

    Anonymous-tier allowed.
    """
    return fetch_attestation_impl(_get_client(), pda_address=pda_address)


@mcp.tool()
def axiom_request_attestation(
    asset_id: str,
    asset_class: int,
    fee_lamports: int | None = None,
    data_flags: int = 0xFF,
    timeout_seconds: int = 90,
    poll_interval_seconds: float = 2.0,
) -> dict[str, Any]:
    """Submit a fresh ``request_property_data`` TX + await the Master
    Broker's resulting attestation write.

    Dispatches on asset_class:
        asset_class=1 → request_real_estate_attestation(asset_id, ...)
        asset_class=2 → request_equity_attestation(asset_id, ...)

    If fee_lamports is omitted, the tool calls quote_fee() first to
    obtain the current threshold (convenience for one-shot AI-agent
    invocations).

    This is a SOL-spending operation. The operator-controlled wallet
    pays the fee (typically ~0.005-0.010 SOL = ~$0.50-$1.00 USD-equivalent
    on devnet). Roundtrip latency is typically 12-20 seconds (TX
    confirm + listener observation + provider fetch + submit_attestation_v2
    confirm + SDK poll).

    Args:
        asset_id: Asset identifier. For equity: ticker (e.g., "NVDA").
                  For real estate: address string.
        asset_class: 1=real_estate, 2=equity. Other classes are out of
                     scope for V1.
        fee_lamports: optional; if absent, quote_fee() is called first.
        data_flags: u8 bitfield. Default 0xFF.
        timeout_seconds: total polling timeout. Default 90.
        poll_interval_seconds: PDA poll cadence. Default 2.0.

    Returns:
        dict with asset_class, asset_id, asset_data (variant-typed),
        attestations (provider records — typically just the Master Broker),
        latest_snapshot_hash, manual_audit_required, bump,
        fee_lamports_paid.

    Raises:
        ValueError if asset_class is unsupported.
        OracleQuoteUnavailable if listener /quote is unreachable when
            auto-quoting fee.
        OracleFeeError / OracleTimeoutError / OracleSignatureError from
            the SDK call.

    Authenticated-tier required.
    """
    return request_attestation_impl(
        _get_client(),
        asset_id=asset_id,
        asset_class=asset_class,
        fee_lamports=fee_lamports,
        data_flags=data_flags,
        timeout_seconds=timeout_seconds,
        poll_interval_seconds=poll_interval_seconds,
    )


# ---------------------------------------------------------------------------
# ASGI app: path-dispatch wrapper around FastMCP's streamable_http_app.
#
# Why not a plain Starlette wrapper: FastMCP's StreamableHTTP session manager
# is initialized inside the app's lifespan event. Mounting the FastMCP app
# under a parent Starlette breaks lifespan propagation (the session manager
# raises "Task group is not initialized" at first request). The wrapper
# below explicitly forwards lifespan + HTTP scopes to the inner app, and
# intercepts only `GET /health` for NLB target-group probes (which support
# GET only; MCP traffic is POST /mcp).
#
# BearerAuthMiddleware sits in front of the whole thing and only intercepts
# POST requests carrying a JSON-RPC `tools/call`; `GET /health` and any
# `tools/list` / `initialize` POST passes through.
# ---------------------------------------------------------------------------

_inner_mcp_app = mcp.streamable_http_app()


class _HealthOrMcpApp:
    """Path-dispatch wrapper: GET /health → static 200; everything else
    (including lifespan events) → inner FastMCP app.
    """

    def __init__(self, inner):
        self.inner = inner

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "http":
            if scope.get("method") == "GET" and scope.get("path") == "/health":
                # Drain the (empty) request body before responding, so the
                # ASGI contract is honored.
                while True:
                    msg = await receive()
                    if msg.get("type") != "http.request":
                        break
                    if not msg.get("more_body", False):
                        break
                payload = b'{"status":"ok","service":"axiom-oracle-mcp"}'
                await send({
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"content-length", str(len(payload)).encode()),
                        (b"cache-control", b"no-store"),
                    ],
                })
                await send({"type": "http.response.body", "body": payload})
                return
        # All other scopes (lifespan, websocket, other HTTP paths) → inner.
        await self.inner(scope, receive, send)


app = BearerAuthMiddleware(_HealthOrMcpApp(_inner_mcp_app))


def main() -> None:
    """Local dev runner. In production, the Dockerfile runs uvicorn
    against ``mcp_server.server:app`` instead of calling this directly.
    """
    import uvicorn
    port = int(os.environ.get("LISTENER_HTTP_PORT", "8080"))
    host = os.environ.get("LISTENER_HTTP_HOST", "0.0.0.0")
    logger.info(f"axiom-oracle-mcp starting on {host}:{port}")
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
