"""Tool: axiom_request_attestation.

Submits a fresh on-chain ``request_property_data`` TX + polls for the
Master Broker's resulting ``submit_attestation_v2`` write. Dispatches
on ``asset_class``:

    asset_class == 1 (real estate)  → request_real_estate_attestation
    asset_class == 2 (equity)       → request_equity_attestation

If ``fee_lamports`` is omitted, the tool calls ``quote_fee()`` first to
obtain the current threshold (so callers can submit a one-line tool call
without pre-quoting; convenience for AI agents).

**Authenticated-tier required.** SOL-spending operation; the operator's
wallet pays the fee.
"""

from __future__ import annotations

from typing import Any

from integrations.oracle_compliance.client import (
    OracleError,
    OracleFeeError,
    OracleQuoteUnavailable,
    OracleSignatureError,
    OracleTimeoutError,
)
from integrations.oracle_compliance.constants import (
    ASSET_CLASS_EQUITY,
    ASSET_CLASS_REAL_ESTATE,
)
from mcp_server.tools.fetch_attestation import _normalize_variant


def request_attestation_impl(
    client,
    asset_id: str,
    asset_class: int,
    fee_lamports: int | None = None,
    data_flags: int = 0xFF,
    timeout_seconds: int = 90,
    poll_interval_seconds: float = 2.0,
) -> dict[str, Any]:
    """Submit a fresh attestation request via the supplied OracleClient.

    Dispatches on asset_class. Auto-fetches fee_lamports via quote_fee()
    if not supplied. Returns the decoded Attestation + the submitted
    transaction signature isn't available without modification of the SDK
    — instead we return the freshly-polled Attestation which carries the
    Master Broker's signature trail via provider_pubkey.

    Raises:
        ValueError on invalid asset_class.
        OracleQuoteUnavailable if listener /quote is unreachable when
            auto-quoting.
        OracleFeeError / OracleTimeoutError / OracleSignatureError from
            the underlying SDK call.
    """
    if asset_class not in (ASSET_CLASS_REAL_ESTATE, ASSET_CLASS_EQUITY):
        raise ValueError(
            f"asset_class must be {ASSET_CLASS_REAL_ESTATE} (real estate) or "
            f"{ASSET_CLASS_EQUITY} (equity) in V1; got {asset_class}"
        )

    # Auto-quote if not supplied. quote_fee may raise OracleQuoteUnavailable;
    # propagated.
    if fee_lamports is None:
        quote = client.quote_fee(
            asset_class=asset_class,
            data_flags=data_flags,
        )
        fee_lamports = quote["fee_lamports"]

    if asset_class == ASSET_CLASS_EQUITY:
        att = client.request_equity_attestation(
            symbol=asset_id,
            fee_lamports=fee_lamports,
            data_flags=data_flags,
            timeout_seconds=timeout_seconds,
            poll_interval_seconds=poll_interval_seconds,
        )
    else:
        att = client.request_real_estate_attestation(
            address=asset_id,
            fee_lamports=fee_lamports,
            data_flags=data_flags,
            timeout_seconds=timeout_seconds,
            poll_interval_seconds=poll_interval_seconds,
        )

    # Normalize for JSON (same shape as fetch_attestation_impl).
    return {
        "asset_class": att.asset_class,
        "asset_id": att.asset_id,
        "asset_data": _normalize_variant(att.asset_data),
        "attestations": [
            {
                "provider_pubkey": str(r.provider_pubkey),
                "valuation_score": r.valuation_score,
                "confidence_score": r.confidence_score,
                "timestamp": r.timestamp,
                "raw_snapshot_hash": r.raw_snapshot_hash.hex(),
            }
            for r in att.attestations
        ],
        "latest_snapshot_hash": att.latest_snapshot_hash.hex(),
        "manual_audit_required": att.manual_audit_required,
        "bump": att.bump,
        "fee_lamports_paid": fee_lamports,
    }


__all__ = [
    "request_attestation_impl",
    "OracleError",
    "OracleFeeError",
    "OracleQuoteUnavailable",
    "OracleSignatureError",
    "OracleTimeoutError",
]
