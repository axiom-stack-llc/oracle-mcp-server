# Axiom Oracle MCP Server — Architecture

**Audience:** developers extending the server with new tools, reviewing the security posture, or porting the pattern to other MCP services.
**Companion docs:** [`README.md`](../README.md) at the repo root (user-facing — how to add this server to your AI agent).

This memo answers "how is this thing built, why, and how do I extend it." For "how do I add this server to my AI agent," see the user-facing README.

---

## 1. What the server is

A thin HTTP-served MCP (Model Context Protocol) façade over the Axiom Oracle Compliance SDK. The server exposes four tools that AI agents can invoke during reasoning, without anyone writing a custom SDK integration:

| Tool | Backing SDK method | Tier |
|---|---|---|
| `axiom_quote_fee` | `OracleClient.quote_fee()` | Anonymous (read) |
| `axiom_fetch_audit_reference` | `OracleClient.fetch_oracle_audit_reference()` | Anonymous (read) |
| `axiom_fetch_attestation` | `OracleClient.fetch_attestation_by_pda()` | Anonymous (read) |
| `axiom_request_attestation` | `OracleClient.request_*_attestation()` dispatch | Authenticated (write — devnet SOL-spending) |

The server itself is **not** the source of truth for any of these operations — it is a transport-shifting wrapper. Authoritative behavior lives in the Compliance SDK; the MCP server is a 4-file Python module that turns four SDK methods into four MCP tools.

---

## 2. Layered architecture

```
                  ┌─────────────────────────────────┐
                  │   AI agent (Claude Desktop /    │
                  │   Cursor / Cline / Continue /   │
                  │   Goose / direct API consumer)  │
                  └────────────────┬────────────────┘
                                   │ MCP JSON-RPC 2.0 over
                                   │ HTTPS (Streamable HTTP transport,
                                   │ stateless, JSON response)
                                   ▼
                  ┌─────────────────────────────────┐
                  │ Public endpoint:                │
                  │   https://mcp.axiomstack.dev    │
                  │ TLS terminated at AWS ACM cert  │
                  │ (single-SAN: mcp.axiomstack.dev)│
                  └────────────────┬────────────────┘
                                   │
                                   ▼
                  ┌─────────────────────────────────┐
                  │ AWS API Gateway HTTP API        │
                  │   GET  /health                  │
                  │   POST /mcp                     │
                  │   POST /                        │
                  │ Routes use HTTP_PROXY via VPC   │
                  │ link to the MCP NLB.            │
                  └────────────────┬────────────────┘
                                   │ Private TCP/8080
                                   ▼
                  ┌─────────────────────────────────┐
                  │ Internal NLB → ECS Fargate      │
                  │ Container: uvicorn → ASGI app:  │
                  │   BearerAuthMiddleware          │
                  │     → _HealthOrMcpApp           │
                  │       → GET /health (inline)    │
                  │       → everything else → FastMCP│
                  │         StreamableHTTP session  │
                  │           → @mcp.tool() funcs    │
                  │             → OracleClient SDK   │
                  └────────────────┬────────────────┘
                                   │
                                   ▼
                  Downstream (SDK calls):
                    - HTTPS to the Oracle listener (/quote, /health)
                    - Solana devnet RPC (getAccountInfo, sendTransaction)
                    - AWS SSM SecureString (operator wallet key)
```

---

## 3. Module map

```
oracle-mcp-server/
├── server.json                Registry submission entry
├── README.md                  User-facing
├── docs/ARCHITECTURE.md       This file
└── mcp_server/
    ├── __init__.py            version metadata (__version__ = "0.1.0-g1.1")
    ├── server.py              FastMCP("axiom-oracle"), 4 @mcp.tool() decorators,
    │                          lazy OracleClient singleton, _HealthOrMcpApp
    │                          path-dispatch wrapper, BearerAuthMiddleware at top.
    │                          Exposes `app` for uvicorn.
    ├── auth.py                ASGI BearerAuthMiddleware + tier policy.
    │                          Buffers request body once, extracts JSON-RPC
    │                          `tools/call`.params.name, allows or denies based
    │                          on (ANONYMOUS_TOOLS ∩ AXIOM_MCP_ALLOW_ANONYMOUS) ∪
    │                          (token in AXIOM_MCP_API_KEYS).
    ├── tools/
    │   ├── quote_fee.py
    │   ├── fetch_audit_reference.py
    │   ├── fetch_attestation.py
    │   └── request_attestation.py     (dispatches on asset_class, auto-quotes
    │                                   if fee_lamports omitted)
    ├── scripts/
    │   └── generate_token.py          CLI: opaque 32-byte URL-safe base64
    └── tests/
        └── test_mcp_server.py         Pytest coverage for auth, tools, FastMCP
                                       integration.
```

---

## 4. Why this design

### 4.1 Transport choice: Streamable HTTP (stateless), not SSE

The MCP spec at version 2025-11-25 recommends **Streamable HTTP** for production tool servers — `stateless_http=True, json_response=True` — which is strictly better than SSE for this use case:

- Each tool call is a self-contained HTTP request/response (no long-lived session)
- Works cleanly through any HTTP-aware load balancer / API Gateway
- Bearer auth becomes a straightforward per-request check
- Sessionless makes the auth-tier policy decidable purely from headers + body

### 4.2 Auth at the ASGI middleware layer, not inside tools

The auth model has two tiers:

- Anonymous-allowed reads (when `AXIOM_MCP_ALLOW_ANONYMOUS=true`)
- Authenticated-required writes (always, regardless of anonymous flag)

The natural FastMCP pattern would be `Context`-based auth inside each tool function. Two problems:

1. `Context` exposes `request_context.request`, but accessing HTTP headers from there couples each tool to ASGI internals.
2. Auth is a cross-cutting concern; inlining it in 4 tool functions invites drift.

Instead, an **ASGI middleware** sits at the very top of the stack (`BearerAuthMiddleware` in `mcp_server/auth.py`):

- Inspects the request body once, extracts the JSON-RPC `params.name` if `method == "tools/call"`
- Decides allow/deny based on tier policy + bearer-token presence
- 401s with a JSON-RPC-shaped error (`-32001`) BEFORE the FastMCP app ever sees the request
- For `initialize` / `tools/list` / other non-tool-call methods, passes through unauthenticated (protocol handshake is free)

Trade-off: the middleware buffers the request body to introspect it, then replays the buffered body to downstream. The buffer is bounded (MCP JSON-RPC requests are <10 KB) and the implementation is ~30 lines.

### 4.3 `_HealthOrMcpApp` path-dispatch wrapper, not `Starlette(routes=[...])`

NLB target group HTTP health checks require GET; MCP traffic is POST. We need a `GET /health` endpoint. First attempt wrapped FastMCP's `streamable_http_app()` inside a parent Starlette with explicit routes — broke FastMCP's StreamableHTTP session manager (`RuntimeError: Task group is not initialized`) because mounting a Starlette app under another parent skips the inner app's lifespan event.

Second attempt (current): a thin ASGI wrapper that:

- For `GET /health` → emits a static 200 inline
- For everything else (including lifespan, websocket) → passes scope/receive/send unchanged to the FastMCP app

Preserves FastMCP's lifespan plumbing while adding a single GET route the load balancer can probe.

### 4.4 Lazy OracleClient construction

The OracleClient is **lazy-constructed on first tool call** rather than at module import:

```python
_oracle_client: Any | None = None
def _get_client():
    global _oracle_client
    if _oracle_client is None:
        from integrations.oracle_compliance.client import OracleClient
        _oracle_client = OracleClient()  # auto-loads wallet from SSM
    return _oracle_client
```

Two reasons:

1. **Startup ordering**: the HTTP listen socket must bind FAST so health probes succeed within the container's start grace window. SSM keypair load is a network call that can take 200-500 ms; we don't want that on the boot path.
2. **Anonymous-only operation**: if SSM is unreachable (or the task role lacks grants), the OracleClient lazy-load fails on first `axiom_request_attestation` only — anonymous read tools work without SSM access.

### 4.5 Explicit `transport_security` for production hosts

`mcp_server/server.py` constructs `FastMCP(...)` with an explicit `transport_security=TransportSecuritySettings(...)` block:

```python
transport_security=TransportSecuritySettings(
    enable_dns_rebinding_protection=True,
    allowed_hosts=[
        "mcp.axiomstack.dev",
        "localhost:*",
        "127.0.0.1:*",
        *_extra_hosts,           # from $AXIOM_MCP_ALLOWED_HOSTS env
    ],
    allowed_origins=[],
)
```

Why this is necessary: FastMCP's `__init__` *auto-enables* DNS-rebinding protection when `host` defaults to `127.0.0.1` (the inner SDK assumes a localhost-only deployment), populating `allowed_hosts` with `["127.0.0.1:*", "localhost:*", "[::1]:*"]`. Behind a reverse proxy or API Gateway the `Host` header is the public custom domain, which doesn't match the auto-generated localhost-only list — the inner SDK returns HTTP 421 *"Invalid Host header"* before the request reaches the tool layer.

Local-container smoke tests with `curl http://localhost:8080/mcp` do not surface this defect because the `Host` header is `localhost:8080`, which matches the auto list. The bug only appears under a reverse proxy that forwards a non-localhost `Host`.

The `$AXIOM_MCP_ALLOWED_HOSTS` env var (comma-separated) allows operational additions without code change — e.g., a raw API Gateway URL during pre-DNS verification of a fresh deploy.

Defense-in-depth: API Gateway itself rejects unknown `Host` values with HTTP 403 at its custom-domain mapping layer (only routes traffic when `Host` matches the bound custom domain). So an attacker who somehow reached the container directly would still hit the FastMCP middleware's allow-list check.

### 4.6 Project separation: new everything

The MCP server runs in its own ECS service / NLB / target group / security groups / log group, isolated from the upstream listener service. Failure-domain isolation between the two services; either can be redeployed without affecting the other.

---

## 5. Security boundaries (summary)

- **TLS**: ACM-issued cert on the API Gateway custom domain, single-SAN.
- **API Gateway**: rejects unknown `Host` headers (403) before traffic reaches the container.
- **DNS rebinding**: FastMCP's `TransportSecurityMiddleware` rejects unknown `Host` headers (421) at the container layer (defense-in-depth).
- **Bearer auth**: `BearerAuthMiddleware` rejects unauthorized tool calls (401 with JSON-RPC `-32001`) before the FastMCP layer sees them.
- **Wallet exposure**: the operator wallet key lives in AWS SSM SecureString with KMS encryption; the ECS task role grants narrow `ssm:GetParameter` + `kms:Decrypt` only on the specific parameter path. The wallet is loaded lazily on first authenticated tool call (see §4.4).
- **Trust separation**: the bearer token grants permission to *request* a fresh attestation, not to *attest data itself*. The on-chain Master Broker key (held by the listener) is the only key whose signature an attestation consumer should trust for data validity.

---

## 6. How to add a new tool

Five steps, in order. Estimated time: 20-30 min including tests.

### Step 1: Add the SDK method (if backing one doesn't exist)

Each MCP tool is a thin wrapper over an `OracleClient` method. If your tool needs functionality not in the SDK today, add the SDK method first (separate commit) with full SDK-level test coverage. Don't put "real" logic in the MCP tool layer — the SDK is the source of truth.

### Step 2: Create `mcp_server/tools/<new_tool>.py`

```python
"""Tool: axiom_<verb>_<object>.

<one-line purpose>

Anonymous-tier or Authenticated-tier? (Pick one explicitly; declare in §1
of this module's docstring.)
"""

from __future__ import annotations
from typing import Any

def <verb>_<object>_impl(client, *args, **kwargs) -> dict[str, Any]:
    """Run the SDK call. Raises propagate; FastMCP wraps into MCP errors."""
    return client.sdk_method(*args, **kwargs)
```

### Step 3: Register the tool in `mcp_server/server.py`

```python
from mcp_server.tools.<new_tool> import <verb>_<object>_impl

@mcp.tool()
def axiom_<verb>_<object>(...args..., type_annotated: bool = True) -> dict[str, Any]:
    """User-visible docstring (becomes the tool's description in tools/list).

    Document parameters with Args / Returns / Raises blocks. The AI agent
    reads this docstring during reasoning to decide whether to invoke
    this tool.

    Anonymous-tier or Authenticated-tier? Match the impl module's choice.
    """
    return <verb>_<object>_impl(_get_client(), ...args...)
```

### Step 4: Update the tier policy in `mcp_server/auth.py`

```python
ANONYMOUS_TOOLS = frozenset({
    "axiom_quote_fee",
    "axiom_fetch_audit_reference",
    "axiom_fetch_attestation",
    "axiom_<verb>_<object>",                    # ← add for anonymous read tools
})

AUTH_REQUIRED_TOOLS = frozenset({
    "axiom_request_attestation",
    "axiom_<verb>_<object>",                    # ← add for write/SOL-spending tools
})
```

### Step 5: Add tests in `mcp_server/tests/test_mcp_server.py`

At least three per tool:

```python
def test_<verb>_<object>_anonymous_allowed():
    """If anonymous-tier: 200 with no auth header."""
    ...

def test_<verb>_<object>_requires_auth_when_no_anonymous():
    """If auth-required: 401 with no auth header."""
    ...

def test_<verb>_<object>_returns_expected_shape():
    """Tool's structuredContent matches the documented Returns shape."""
    ...
```

Run `pytest mcp_server/tests/`. All should pass.

After all five steps: rebuild + redeploy the container image. The tool appears in `tools/list` discovery on first restart.

---

## 7. What V1 doesn't do (intentional deferrals)

| Item | V1 status | V1.5+ plan |
|---|---|---|
| OAuth 2.1 resource server | Not implemented; bearer tokens only | Migrate to OAuth 2.1 via Python SDK's `TokenVerifier` protocol; V1 tokens honored during transition |
| Self-service token issuance | Manual (email request) | Developer Portal at axiomstack.dev/portal (TBD) |
| Per-token rate limiting | Coarse (~1000 req/day per source IP at API GW) | Per-token quotas |
| Multi-region deployment | Single us-east-1 | Regional endpoints with URL templates in `server.json` `remotes[].variables` |
| Equity payload completeness | `market_cap_usd_millions=0`, `exchange_code` from lookup | Upgrade listener to AlphaVantage TIME_SERIES_DAILY_ADJUSTED + OVERVIEW bundle (same Borsh layout) |
| `data_source` indicator in tool responses | Not surfaced | Add `data_source: "stub" \| "live"` field to disambiguate V1 stub payloads |

---

## 8. Cross-references

- MCP spec: https://modelcontextprotocol.io/specification
- MCP Registry: https://registry.modelcontextprotocol.io/
- Python MCP SDK: https://github.com/modelcontextprotocol/python-sdk
- Anchor framework (on-chain program toolchain): https://www.anchor-lang.com/
- Solana docs (devnet): https://solana.com/docs
