# Axiom Oracle — MCP Server

**Make Axiom Oracle callable from any [Model Context Protocol](https://modelcontextprotocol.io)–compatible AI agent without writing custom SDK integration code.**

| Component | Status |
|---|---|
| Production endpoint | `https://mcp.axiomstack.dev` (live) |
| MCP protocol version | 2025-11-25 |
| Transport | Streamable HTTP (stateless, JSON response) |
| Auth (V1) | Bearer token; optional anonymous read tier |
| Roadmap (V1.5) | OAuth 2.1 resource server |

> **Important:** Axiom Oracle currently operates on **Solana devnet only**. Attestations have audit-trail value but are NOT mainnet-grade financial records. See [Production status](#production-status).

---

## What this lets your AI agent do

The Axiom Oracle attests real-world-asset (RWA) data on Solana. Once you add this MCP server to your agent's config, the agent can — during reasoning — call:

| Tool | Tier | What it does |
|---|---|---|
| `axiom_quote_fee` | Anonymous (read) | Return the current Oracle attestation fee threshold (USD-anchored; floats with SOL price). |
| `axiom_fetch_audit_reference` | Anonymous (read) | Capture the operator's operational-integrity anchor at this moment. For audit-chain records. |
| `axiom_fetch_attestation` | Anonymous (read) | Read an existing on-chain attestation by PDA. No fee. |
| `axiom_request_attestation` | **Authenticated** (write — devnet SOL-spending) | Submit a fresh attestation request; await the Master Broker's on-chain write; return the decoded result. |

---

## Quick start — add to your agent

### Claude Desktop

Edit `~/Library/Application Support/Claude/claude_desktop_config.json` (macOS) or `%APPDATA%\Claude\claude_desktop_config.json` (Windows):

```json
{
  "mcpServers": {
    "axiom-oracle": {
      "url": "https://mcp.axiomstack.dev",
      "headers": {
        "Authorization": "Bearer YOUR_BEARER_TOKEN_HERE"
      }
    }
  }
}
```

Restart Claude Desktop. The four Axiom Oracle tools appear in the tools list. **Omit the `headers` block** to use the anonymous read tier (no token required, but `axiom_request_attestation` will return 401).

> Some Claude Desktop versions only support stdio MCP servers. If yours rejects the `url` field, use the [`mcp-remote` stdio bridge](https://www.npmjs.com/package/mcp-remote) instead.

### Cursor

In Cursor settings → MCP Servers, add the same `{url, headers}` JSON object.

### Cline / Continue / Goose

These clients accept the same MCP server config shape. Consult each client's MCP docs for the exact config file location; the entry's content is the same `{url, headers}` JSON object.

### Programmatic (Python — for testing or custom agent integration)

```python
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamablehttp_client

async with streamablehttp_client(
    "https://mcp.axiomstack.dev",
    headers={"Authorization": "Bearer YOUR_BEARER_TOKEN_HERE"},
) as (read, write, _):
    async with ClientSession(read, write) as session:
        await session.initialize()
        tools = await session.list_tools()
        # ... invoke tools as needed
```

### Quick smoke test — `curl`

```bash
# Anonymous tier — current fee for equity attestation request
curl -sS -X POST https://mcp.axiomstack.dev/mcp \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call",
       "params":{"name":"axiom_quote_fee","arguments":{"asset_class":2}}}' | jq

# Authenticated tier — request a fresh NVDA attestation (spends devnet SOL)
curl -sS -X POST https://mcp.axiomstack.dev/mcp \
  -H 'Authorization: Bearer YOUR_BEARER_TOKEN_HERE' \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/call",
       "params":{"name":"axiom_request_attestation",
                 "arguments":{"asset_id":"NVDA","asset_class":2}}}' | jq
```

---

## Tool reference

### `axiom_quote_fee`

Returns the current fee floor the listener requires for a fresh attestation.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `asset_class` | `int` | required | `1`=real estate, `2`=equity, `3`=fixed income, `4`=macro, `5`=commodity. V1 supports `1` and `2` on the request path. |
| `data_flags` | `int` | `255` (all 8 layers) | u8 bitfield. The listener normalizes `0` to `31`. |
| `margin_pct` | `int` | `60` | Target margin on vendor USD cost. |

**Returns:**
```json
{
  "fee_lamports": 8000000,
  "fiat_cost_usd": 0.43,
  "margin_pct": 60,
  "sol_price_usd": 84.9,
  "computed_at": "2026-05-18T05:25:07Z",
  "cached": false
}
```

### `axiom_fetch_audit_reference`

Captures the current operational-integrity anchor for audit-chain records. **Fail-open**: never raises.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `timeout_seconds` | `float` | `3.0` | HTTP timeout for the underlying call. |

**Returns (anchored variant):**
```json
{
  "oracle_anchor_status": "anchored",
  "oracle_anchor_attempted_at": "2026-05-18T19:24:33.281Z",
  "oracle_attestation_tx_sig": "<88-char base58>",
  "oracle_attestation_pda": "<base58 PDA>",
  "oracle_attestation_timestamp": "<ISO 8601>",
  "oracle_attestation_asset_class": 2
}
```

**Returns (unanchored variant):**
```json
{
  "oracle_anchor_status": "unanchored",
  "oracle_anchor_attempted_at": "<ISO 8601>",
  "oracle_anchor_failure_reason": "<non-empty diagnostic>"
}
```

### `axiom_fetch_attestation`

Reads an existing on-chain attestation by its PDA address. Single Solana `getAccountInfo` RPC; no on-chain TX; no fee.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `pda_address` | `str` | required | base58-encoded Solana PDA address. |

**Returns** a decoded `AssetStateV2` dict with variant-typed `asset_data` (EquityData / RealEstateData / FixedIncomeData / MacroData / CommodityData), an `attestations` list, `latest_snapshot_hash`, `manual_audit_required`, `bump`.

> **Note on equity payloads in V1:** the current listener writes equity attestations using AlphaVantage's GLOBAL_QUOTE endpoint, which does not include market-cap or full exchange data. `market_cap_usd_millions` is therefore `0` in V1 equity payloads, and `exchange_code` is from a static lookup table. V1.5 will upgrade the listener to AlphaVantage's TIME_SERIES_DAILY_ADJUSTED + OVERVIEW bundle for full population.

**Errors:** raises `OraclePdaNotFound` if the PDA has no account or the data is not a valid `AssetStateV2`.

### `axiom_request_attestation`

**Authenticated tier.** Submits a fresh attestation request + awaits the Master Broker's resulting write.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `asset_id` | `str` | required | Ticker (equity) or address (real estate). |
| `asset_class` | `int` | required | `1` (real estate) or `2` (equity). |
| `fee_lamports` | `int \| null` | auto-quote via `quote_fee()` | Lamports to pay. Omit to let the server auto-quote. |
| `data_flags` | `int` | `255` | u8 bitfield. |
| `timeout_seconds` | `int` | `90` | Total polling timeout. |
| `poll_interval_seconds` | `float` | `2.0` | PDA poll cadence. |

**Returns** the same shape as `axiom_fetch_attestation` plus `fee_lamports_paid`. **Latency**: typically 12–20 seconds (real on-chain round-trip).

---

## Authentication

### V1 — Bearer token (current)

The server enforces a two-tier model:

- **Anonymous tier** (when the server runs with `AXIOM_MCP_ALLOW_ANONYMOUS=true`, which is the production default for read tools): the three read tools (`axiom_quote_fee`, `axiom_fetch_audit_reference`, `axiom_fetch_attestation`) work without authentication.
- **Authenticated tier** (always required for `axiom_request_attestation`): caller sends `Authorization: Bearer <token>` with a token issued by the operator. Tokens are 32-byte URL-safe base64 (~43 ASCII chars).

Request a token by contacting `axiomstack.dev@gmail.com` with your intended use case. V1 token issuance is manual; V1.5 will replace this with self-service via the Axiom Stack Developer Portal.

### V1 → V1.5 migration

V1.5 migrates to **OAuth 2.1 resource server** semantics (per the MCP spec's recommended auth path) via the Python SDK's `TokenVerifier` protocol. V1 bearer tokens will be honored throughout a transition window.

> **Known limitation (V1):** Claude Desktop's "custom connector" UI supports OAuth flows only — it cannot supply a static bearer token. V1 Claude Desktop users can call the three anonymous-tier tools through the canonical UI; `axiom_request_attestation` requires either Claude Desktop's `config.json` `headers` block (supported by recent versions) or a programmatic MCP client. V1.5 OAuth removes this limitation.

---

## Failure modes & error mapping

| Underlying SDK exception | Returned to caller as |
|---|---|
| `OracleQuoteUnavailable` (listener `/quote` unreachable) | MCP error from `axiom_quote_fee` / `axiom_request_attestation` |
| `OracleFeeError` (caller-supplied fee invalid, e.g. ≤ 0) | MCP error from `axiom_request_attestation` |
| `OracleTimeoutError` (no fresh attestation within timeout) | MCP error from `axiom_request_attestation` |
| `OracleSignatureError` (Master Broker absent from attestation) | MCP error from `axiom_request_attestation` |
| `OraclePdaNotFound` (PDA has no account or undecodable data) | MCP error from `axiom_fetch_attestation` |
| `OracleError` (other SDK base failures) | MCP error with the exception message |

`axiom_fetch_audit_reference` is fail-open: it does not raise. The dict returned has `oracle_anchor_status: "unanchored"` and a `oracle_anchor_failure_reason` on any internal failure.

---

## Production status

- **Network:** Solana devnet only. The on-chain program is deployed to devnet. **Mainnet deployment requires a professional security audit + treasury hardening and is not authorized as of this writing.**
- **Equity-payload stub disclosure** (V1 known limitation): the listener uses AlphaVantage GLOBAL_QUOTE, which does not return market-cap or full exchange data. `market_cap_usd_millions` is hardcoded to `0`; `exchange_code` defaults to NASDAQ (`1`) for tickers not in a 20-entry NYSE allowlist. Closing price and 24h volume ARE live values. V1.5 will upgrade to the richer AlphaVantage bundle (TIME_SERIES_DAILY_ADJUSTED + OVERVIEW) — same on-chain Borsh layout.
- **Trust model:** the on-chain Master Broker key signs every attestation; the bearer token grants permission to *request* a fresh attestation, not to *attest data itself*. Consumers should verify the on-chain provider signature against the canonical Master Broker pubkey, not trust the MCP server's bearer-token grant for data validity.

---

## Repository structure

```
oracle-mcp-server/
├── README.md                   This document
├── LICENSE                     MIT
├── server.json                 MCP Registry submission (mcp-publisher reads from here)
├── docs/
│   └── ARCHITECTURE.md         Developer-facing design memo: how the server is built, why, how to extend
├── mcp_server/                 Python package
│   ├── __init__.py             version metadata
│   ├── server.py               FastMCP app + 4 @mcp.tool() decorators + ASGI wrapping
│   ├── auth.py                 ASGI BearerAuthMiddleware (tier policy)
│   ├── requirements.txt        Production deps
│   ├── tools/                  Four tool impls (thin shims over the SDK)
│   │   ├── quote_fee.py
│   │   ├── fetch_audit_reference.py
│   │   ├── fetch_attestation.py
│   │   └── request_attestation.py
│   ├── scripts/
│   │   └── generate_token.py   CLI: opaque 32-byte URL-safe base64 token generator
│   └── tests/                  Pytest coverage for auth, tools, FastMCP integration
```

The Axiom Oracle Compliance SDK (`integrations/oracle_compliance/`) is a private dependency of `server.py`. This public repo references the SDK by package name; the implementation lives in the maintainer's private workspace and is published via the live `mcp.axiomstack.dev` deployment. Self-hosters wishing to deploy their own instance against a different listener can fork and substitute their own SDK.

---

## Contributing

Bug reports and small fixes welcome via GitHub issues / PRs. For larger contributions — new tools, transport changes, or non-trivial refactors — please open an issue first describing the proposed change; the V1 → V1.5 roadmap may already cover what you're after.

The MCP server is a thin wrapper over Axiom Oracle's Compliance SDK; new tools generally start as new SDK methods (not as new MCP-server-only logic). See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) §6 ("How to add a new tool").

---

## Cross-references

- MCP spec: https://modelcontextprotocol.io/specification
- MCP Registry: https://registry.modelcontextprotocol.io/
- Python MCP SDK: https://github.com/modelcontextprotocol/python-sdk
- Maintainer contact: `axiomstack.dev@gmail.com`
