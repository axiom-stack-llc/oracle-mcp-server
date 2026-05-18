"""Tool: axiom_quote_fee.

Returns the current Oracle attestation fee threshold at the off-chain
listener's gate. Backed by ``OracleClient.quote_fee()``.

Anonymous-tier allowed (read-only).
"""

from __future__ import annotations

from typing import Any

from integrations.oracle_compliance.client import OracleQuoteUnavailable


def quote_fee_impl(
    client,
    asset_class: int,
    data_flags: int = 0xFF,
    margin_pct: int = 60,
    timeout_seconds: float = 5.0,
) -> dict[str, Any]:
    """Run quote_fee via the supplied OracleClient.

    Raises OracleQuoteUnavailable on listener unreachable / non-200 / malformed
    response. FastMCP wraps the raised exception into an MCP error reply.
    """
    return client.quote_fee(
        asset_class=asset_class,
        data_flags=data_flags,
        margin_pct=margin_pct,
        timeout_seconds=timeout_seconds,
    )


__all__ = ["quote_fee_impl", "OracleQuoteUnavailable"]
