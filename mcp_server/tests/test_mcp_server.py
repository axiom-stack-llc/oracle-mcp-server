"""Tests for the Axiom Oracle MCP server.

Coverage:
  - Auth middleware: anonymous-allowed reads, auth-required writes,
    token allowlist, bearer-format edge cases, initialize / tools/list
    pass-through.
  - Tool implementations: quote_fee / fetch_audit_reference /
    fetch_attestation / request_attestation — happy paths + error paths
    via mocked SDK client.
  - Token generation CLI: shape + length.

All tests use mocked SDK clients (MagicMock); no live AWS / RPC calls.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import struct
from dataclasses import dataclass
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from integrations.oracle_compliance.client import (
    OracleError,
    OracleFeeError,
    OraclePdaNotFound,
    OracleQuoteUnavailable,
    OracleTimeoutError,
)

from mcp_server.auth import (
    ANONYMOUS_TOOLS,
    AUTH_REQUIRED_TOOLS,
    BearerAuthMiddleware,
    _extract_bearer_token,
    _extract_tool_name,
    is_authorized,
)
from mcp_server.scripts.generate_token import generate_token
from mcp_server.tools.fetch_attestation import fetch_attestation_impl
from mcp_server.tools.fetch_audit_reference import fetch_audit_reference_impl
from mcp_server.tools.quote_fee import quote_fee_impl
from mcp_server.tools.request_attestation import request_attestation_impl


# ---------------------------------------------------------------------------
# Test environment guard — clear any inherited env vars per test
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Each test starts with no MCP env vars set, so tests are independent."""
    for k in ("AXIOM_MCP_ALLOW_ANONYMOUS", "AXIOM_MCP_API_KEYS",
              "AXIOM_ORACLE_API_URL", "AWS_PROFILE"):
        monkeypatch.delenv(k, raising=False)


# ===========================================================================
# Token generation CLI
# ===========================================================================

def test_generate_token_returns_url_safe_base64_43_chars():
    t = generate_token()
    assert len(t) == 43
    raw = base64.urlsafe_b64decode(t + "=" * (4 - len(t) % 4))
    assert len(raw) == 32


def test_generate_token_unique():
    seen = {generate_token() for _ in range(50)}
    assert len(seen) == 50, "50 token generations produced duplicates"


# ===========================================================================
# Auth: _extract_bearer_token
# ===========================================================================

@pytest.mark.parametrize("header,expected", [
    ("Bearer abc123", "abc123"),
    ("bearer abc123", "abc123"),
    ("BEARER abc123", "abc123"),
    ("Bearer   abc123  ", "abc123"),
    ("", None),
    ("abc123", None),
    ("Basic abc123", None),
    ("Bearer ", None),
])
def test_extract_bearer_token(header, expected):
    assert _extract_bearer_token(header) == expected


# ===========================================================================
# Auth: is_authorized
# ===========================================================================

def test_is_authorized_anonymous_allowed_when_flag_set(monkeypatch):
    monkeypatch.setenv("AXIOM_MCP_ALLOW_ANONYMOUS", "true")
    for tool in ANONYMOUS_TOOLS:
        ok, reason = is_authorized("", tool)
        assert ok, f"{tool} should be allowed anonymously, got reason {reason!r}"


def test_is_authorized_anonymous_denied_when_flag_unset(monkeypatch):
    monkeypatch.delenv("AXIOM_MCP_ALLOW_ANONYMOUS", raising=False)
    monkeypatch.setenv("AXIOM_MCP_API_KEYS", "valid-key-1")
    ok, reason = is_authorized("", "axiom_quote_fee")
    assert not ok
    assert "anonymous" in reason


def test_is_authorized_auth_required_tool_denied_anonymous_even_with_flag(monkeypatch):
    monkeypatch.setenv("AXIOM_MCP_ALLOW_ANONYMOUS", "true")
    for tool in AUTH_REQUIRED_TOOLS:
        ok, reason = is_authorized("", tool)
        assert not ok, f"{tool} must require auth regardless of anonymous flag"
        assert "Bearer" in reason


def test_is_authorized_valid_token(monkeypatch):
    monkeypatch.setenv("AXIOM_MCP_API_KEYS", "key-a,key-b,key-c")
    ok, _ = is_authorized("Bearer key-b", "axiom_request_attestation")
    assert ok


def test_is_authorized_invalid_token(monkeypatch):
    monkeypatch.setenv("AXIOM_MCP_API_KEYS", "key-a,key-b")
    ok, reason = is_authorized("Bearer wrong-token", "axiom_request_attestation")
    assert not ok
    assert "allowlist" in reason


def test_is_authorized_no_keys_configured(monkeypatch):
    monkeypatch.delenv("AXIOM_MCP_API_KEYS", raising=False)
    ok, reason = is_authorized("Bearer some-token", "axiom_request_attestation")
    assert not ok
    assert "no API keys configured" in reason


def test_is_authorized_keys_set_whitespace_tolerant(monkeypatch):
    monkeypatch.setenv("AXIOM_MCP_API_KEYS", " ,key-a,  ,key-b , ")
    ok, _ = is_authorized("Bearer key-a", "axiom_request_attestation")
    assert ok
    ok2, _ = is_authorized("Bearer ", "axiom_request_attestation")
    assert not ok2


# ===========================================================================
# Auth: _extract_tool_name
# ===========================================================================

def test_extract_tool_name_from_tools_call():
    body = json.dumps({
        "jsonrpc": "2.0", "method": "tools/call", "id": 1,
        "params": {"name": "axiom_quote_fee", "arguments": {"asset_class": 2}},
    }).encode()
    assert _extract_tool_name(body) == "axiom_quote_fee"


def test_extract_tool_name_returns_none_for_non_tools_call():
    body = json.dumps({"jsonrpc": "2.0", "method": "tools/list", "id": 1}).encode()
    assert _extract_tool_name(body) is None


def test_extract_tool_name_returns_none_for_initialize():
    body = json.dumps({"jsonrpc": "2.0", "method": "initialize", "id": 1,
                       "params": {"protocolVersion": "2025-11-25"}}).encode()
    assert _extract_tool_name(body) is None


def test_extract_tool_name_returns_none_for_malformed_body():
    assert _extract_tool_name(b"not json") is None
    assert _extract_tool_name(b"[1,2,3]") is None
    assert _extract_tool_name(b"") is None


# ===========================================================================
# Auth middleware: ASGI-level integration
# ===========================================================================

def _build_asgi_scope(body: bytes, auth_header: str | None = None, method: str = "POST") -> dict:
    headers = []
    if auth_header is not None:
        headers.append((b"authorization", auth_header.encode()))
    return {
        "type": "http", "method": method, "path": "/", "headers": headers,
        "query_string": b"", "server": ("test", 80), "client": ("client", 0),
    }


async def _run_middleware(
    body: bytes, auth_header: str | None,
    downstream_app, method: str = "POST",
) -> tuple[int, dict[str, str], bytes]:
    mw = BearerAuthMiddleware(downstream_app)
    scope = _build_asgi_scope(body, auth_header, method)

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    captured_status = [-1]
    captured_headers: dict[str, str] = {}
    captured_body = bytearray()
    async def send(msg):
        if msg["type"] == "http.response.start":
            captured_status[0] = msg["status"]
            for k, v in msg.get("headers", []):
                captured_headers[k.decode().lower()] = v.decode()
        elif msg["type"] == "http.response.body":
            captured_body.extend(msg.get("body", b""))

    await mw(scope, receive, send)
    return captured_status[0], captured_headers, bytes(captured_body)


def _make_passthrough_app(captured: list):
    async def app(scope, receive, send):
        msg = await receive()
        captured.append({"scope": scope, "body": msg.get("body", b"")})
        await send({"type": "http.response.start", "status": 200,
                    "headers": [(b"content-type", b"application/json")]})
        await send({"type": "http.response.body", "body": b'{"ok": true}'})
    return app


def test_middleware_passes_initialize_through_unauthenticated():
    body = json.dumps({"jsonrpc": "2.0", "method": "initialize", "id": 1}).encode()
    captured = []
    status, _, _ = asyncio.run(
        _run_middleware(body, auth_header=None, downstream_app=_make_passthrough_app(captured))
    )
    assert status == 200
    assert captured
    assert captured[0]["body"] == body


def test_middleware_passes_tools_list_through_unauthenticated():
    body = json.dumps({"jsonrpc": "2.0", "method": "tools/list", "id": 1}).encode()
    captured = []
    status, *_ = asyncio.run(
        _run_middleware(body, auth_header=None, downstream_app=_make_passthrough_app(captured))
    )
    assert status == 200


def test_middleware_anonymous_tool_blocks_when_flag_off(monkeypatch):
    monkeypatch.delenv("AXIOM_MCP_ALLOW_ANONYMOUS", raising=False)
    body = json.dumps({
        "jsonrpc": "2.0", "method": "tools/call", "id": 1,
        "params": {"name": "axiom_quote_fee", "arguments": {}},
    }).encode()
    captured = []
    status, headers, _ = asyncio.run(
        _run_middleware(body, auth_header=None, downstream_app=_make_passthrough_app(captured))
    )
    assert status == 401
    assert not captured
    assert headers.get("www-authenticate", "").startswith("Bearer")


def test_middleware_anonymous_tool_allowed_when_flag_on(monkeypatch):
    monkeypatch.setenv("AXIOM_MCP_ALLOW_ANONYMOUS", "true")
    body = json.dumps({
        "jsonrpc": "2.0", "method": "tools/call", "id": 1,
        "params": {"name": "axiom_quote_fee", "arguments": {"asset_class": 2}},
    }).encode()
    captured = []
    status, *_ = asyncio.run(
        _run_middleware(body, auth_header=None, downstream_app=_make_passthrough_app(captured))
    )
    assert status == 200
    assert captured


def test_middleware_auth_required_tool_denied_anonymous_even_with_flag(monkeypatch):
    monkeypatch.setenv("AXIOM_MCP_ALLOW_ANONYMOUS", "true")
    body = json.dumps({
        "jsonrpc": "2.0", "method": "tools/call", "id": 1,
        "params": {"name": "axiom_request_attestation", "arguments": {"asset_id": "NVDA", "asset_class": 2}},
    }).encode()
    captured = []
    status, *_ = asyncio.run(
        _run_middleware(body, auth_header=None, downstream_app=_make_passthrough_app(captured))
    )
    assert status == 401
    assert not captured


def test_middleware_valid_token_allows_auth_required_tool(monkeypatch):
    monkeypatch.setenv("AXIOM_MCP_API_KEYS", "key-a,key-b")
    body = json.dumps({
        "jsonrpc": "2.0", "method": "tools/call", "id": 1,
        "params": {"name": "axiom_request_attestation", "arguments": {"asset_id": "NVDA", "asset_class": 2}},
    }).encode()
    captured = []
    status, *_ = asyncio.run(
        _run_middleware(body, auth_header="Bearer key-a", downstream_app=_make_passthrough_app(captured))
    )
    assert status == 200
    assert captured


def test_middleware_invalid_token_denied(monkeypatch):
    monkeypatch.setenv("AXIOM_MCP_API_KEYS", "key-a")
    body = json.dumps({
        "jsonrpc": "2.0", "method": "tools/call", "id": 1,
        "params": {"name": "axiom_request_attestation", "arguments": {"asset_id": "NVDA", "asset_class": 2}},
    }).encode()
    captured = []
    status, _, response_body = asyncio.run(
        _run_middleware(body, auth_header="Bearer wrong-key", downstream_app=_make_passthrough_app(captured))
    )
    assert status == 401
    assert not captured
    err = json.loads(response_body)
    assert err["error"]["code"] == -32001
    assert "allowlist" in err["error"]["message"]


def test_middleware_get_request_passes_through(monkeypatch):
    captured = []
    status, *_ = asyncio.run(
        _run_middleware(b"", auth_header=None,
                        downstream_app=_make_passthrough_app(captured),
                        method="GET")
    )
    assert status == 200
    assert captured


# ===========================================================================
# Tool: quote_fee_impl
# ===========================================================================

def test_quote_fee_impl_calls_client():
    client = MagicMock()
    client.quote_fee.return_value = {
        "fee_lamports": 8_000_000, "fiat_cost_usd": 0.43, "margin_pct": 60,
        "sol_price_usd": 84.9, "computed_at": "x", "cached": False,
    }
    result = quote_fee_impl(client, asset_class=2, data_flags=255, margin_pct=60)
    client.quote_fee.assert_called_once_with(
        asset_class=2, data_flags=255, margin_pct=60, timeout_seconds=5.0,
    )
    assert result["fee_lamports"] == 8_000_000


def test_quote_fee_impl_propagates_unavailable():
    client = MagicMock()
    client.quote_fee.side_effect = OracleQuoteUnavailable("listener down")
    with pytest.raises(OracleQuoteUnavailable):
        quote_fee_impl(client, asset_class=2)


# ===========================================================================
# Tool: fetch_audit_reference_impl
# ===========================================================================

def test_fetch_audit_reference_impl_calls_client():
    client = MagicMock()
    client.fetch_oracle_audit_reference.return_value = {
        "oracle_anchor_status": "anchored",
        "oracle_anchor_attempted_at": "2026-05-18T...",
        "oracle_attestation_tx_sig": "tx-abc",
        "oracle_attestation_pda": "pda-xyz",
        "oracle_attestation_timestamp": "2026-05-18T...",
        "oracle_attestation_asset_class": 2,
    }
    result = fetch_audit_reference_impl(client, timeout_seconds=3.0)
    client.fetch_oracle_audit_reference.assert_called_once_with(timeout_seconds=3.0)
    assert result["oracle_anchor_status"] == "anchored"


def test_fetch_audit_reference_impl_returns_unanchored_dict():
    client = MagicMock()
    client.fetch_oracle_audit_reference.return_value = {
        "oracle_anchor_status": "unanchored",
        "oracle_anchor_attempted_at": "2026-05-18T...",
        "oracle_anchor_failure_reason": "timeout",
    }
    result = fetch_audit_reference_impl(client)
    assert result["oracle_anchor_status"] == "unanchored"
    assert result["oracle_anchor_failure_reason"] == "timeout"


# ===========================================================================
# Tool: fetch_attestation_impl
# ===========================================================================

def _build_fake_attestation(asset_id: str = "NVDA", asset_class: int = 2):
    from integrations.oracle_compliance.borsh_decoder import (
        Attestation, AttestationV2, EquityData,
    )
    from solders.pubkey import Pubkey
    pk = Pubkey.from_string("9BHC6c5Gv9tUL3DCzRSGkdApdU2QMwh29pxH4Q6zV9xR")
    eq = EquityData(
        closing_price_micros=195_500_000, volume_24h=50_000_000,
        market_cap_usd_millions=3_000_000, exchange_code=1,
    )
    return Attestation(
        asset_class=asset_class,
        asset_id=asset_id,
        asset_data=eq,
        attestations=(
            AttestationV2(
                provider_pubkey=pk, valuation_score=50.0,
                confidence_score=0.85, timestamp=1_800_000_000,
                raw_snapshot_hash=b"\x11" * 32,
            ),
        ),
        latest_snapshot_hash=b"\x22" * 32,
        manual_audit_required=False, bump=254,
    )


def test_fetch_attestation_impl_happy_path_normalizes_to_dict():
    client = MagicMock()
    client.fetch_attestation_by_pda.return_value = _build_fake_attestation()
    result = fetch_attestation_impl(client, pda_address="5wSVE58F...")
    assert result["asset_class"] == 2
    assert result["asset_id"] == "NVDA"
    assert result["manual_audit_required"] is False
    assert result["bump"] == 254
    assert result["latest_snapshot_hash"] == "22" * 32
    assert result["asset_data"]["variant_type"] == "EquityData"
    assert result["asset_data"]["closing_price_micros"] == 195_500_000
    rec = result["attestations"][0]
    assert isinstance(rec["provider_pubkey"], str)
    assert rec["raw_snapshot_hash"] == "11" * 32
    assert rec["timestamp"] == 1_800_000_000


def test_fetch_attestation_impl_propagates_not_found():
    client = MagicMock()
    client.fetch_attestation_by_pda.side_effect = OraclePdaNotFound("no account")
    with pytest.raises(OraclePdaNotFound):
        fetch_attestation_impl(client, pda_address="x")


# ===========================================================================
# Tool: request_attestation_impl
# ===========================================================================

def test_request_attestation_impl_invalid_asset_class_raises():
    client = MagicMock()
    with pytest.raises(ValueError, match="asset_class must be"):
        request_attestation_impl(client, asset_id="X", asset_class=3, fee_lamports=8_000_000)


def test_request_attestation_impl_equity_dispatch():
    client = MagicMock()
    client.request_equity_attestation.return_value = _build_fake_attestation("NVDA", 2)
    result = request_attestation_impl(
        client, asset_id="NVDA", asset_class=2,
        fee_lamports=8_000_000, data_flags=255,
    )
    client.request_equity_attestation.assert_called_once_with(
        symbol="NVDA", fee_lamports=8_000_000, data_flags=255,
        timeout_seconds=90, poll_interval_seconds=2.0,
    )
    client.request_real_estate_attestation.assert_not_called()
    assert result["asset_class"] == 2
    assert result["fee_lamports_paid"] == 8_000_000


def test_request_attestation_impl_real_estate_dispatch():
    client = MagicMock()
    from integrations.oracle_compliance.borsh_decoder import RealEstateData, Attestation, AttestationV2
    from solders.pubkey import Pubkey
    pk = Pubkey.from_string("9BHC6c5Gv9tUL3DCzRSGkdApdU2QMwh29pxH4Q6zV9xR")
    att = Attestation(
        asset_class=1, asset_id="100 MAIN ST",
        asset_data=RealEstateData(monthly_noi_usd=12_500, property_pda_v0=None),
        attestations=(AttestationV2(
            provider_pubkey=pk, valuation_score=50.0, confidence_score=0.85,
            timestamp=1_800_000_000, raw_snapshot_hash=b"\x11" * 32,
        ),),
        latest_snapshot_hash=b"\x22" * 32,
        manual_audit_required=False, bump=254,
    )
    client.request_real_estate_attestation.return_value = att
    result = request_attestation_impl(
        client, asset_id="100 MAIN ST", asset_class=1,
        fee_lamports=5_000_000, data_flags=31,
    )
    client.request_real_estate_attestation.assert_called_once()
    assert result["asset_class"] == 1
    assert result["asset_data"]["variant_type"] == "RealEstateData"


def test_request_attestation_impl_auto_quotes_when_fee_omitted():
    client = MagicMock()
    client.quote_fee.return_value = {"fee_lamports": 8_000_000}
    client.request_equity_attestation.return_value = _build_fake_attestation()
    result = request_attestation_impl(client, asset_id="NVDA", asset_class=2)
    client.quote_fee.assert_called_once()
    assert result["fee_lamports_paid"] == 8_000_000


def test_request_attestation_impl_propagates_timeout():
    client = MagicMock()
    client.request_equity_attestation.side_effect = OracleTimeoutError("90s elapsed")
    with pytest.raises(OracleTimeoutError):
        request_attestation_impl(
            client, asset_id="NVDA", asset_class=2, fee_lamports=8_000_000,
        )


# ===========================================================================
# FastMCP integration
# ===========================================================================

def test_mcp_server_exposes_all_four_tools():
    from mcp_server.server import mcp
    tools = mcp._tool_manager.list_tools()
    names = {t.name for t in tools}
    assert names == {
        "axiom_quote_fee",
        "axiom_fetch_audit_reference",
        "axiom_fetch_attestation",
        "axiom_request_attestation",
    }


def test_mcp_server_tools_have_docstrings():
    from mcp_server.server import mcp
    for tool in mcp._tool_manager.list_tools():
        assert tool.description, f"tool {tool.name} has empty description"
        assert len(tool.description) > 50, f"tool {tool.name} description too short"
