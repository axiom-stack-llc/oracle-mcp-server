"""Tool: axiom_fetch_attestation.

Reads a single on-chain ``AssetStateV2`` account by PDA + returns the
decoded Attestation. Read-only — no on-chain TX, no fee. Backed by
``OracleClient.fetch_attestation_by_pda()``.

Anonymous-tier allowed (read-only).
"""

from __future__ import annotations

from typing import Any

from integrations.oracle_compliance.client import OraclePdaNotFound


def fetch_attestation_impl(client, pda_address: str) -> dict[str, Any]:
    """Run fetch_attestation_by_pda + normalize the Attestation dataclass
    for JSON serialization.

    Raises OraclePdaNotFound on missing / undecodable PDA. FastMCP wraps
    the exception into an MCP error reply.
    """
    att = client.fetch_attestation_by_pda(pda_address)
    # Normalize for JSON: dataclass → dict; nested Attestation records →
    # list of dicts; bytes hash → hex string for human readability.
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
    }


def _normalize_variant(data: Any) -> dict[str, Any]:
    """Normalize an asset-class variant dataclass for JSON output.

    Each variant (RealEstateData, EquityData, etc.) is a frozen dataclass
    with simple int / Pubkey | None fields. Pubkey → str; nothing else
    special needed.
    """
    fields: dict[str, Any] = {}
    for slot in getattr(data, "__dataclass_fields__", {}):
        v = getattr(data, slot)
        if v is None:
            fields[slot] = None
        elif hasattr(v, "__str__") and not isinstance(v, (int, float, bool, str, bytes)):
            # Pubkey or similar object — stringify.
            fields[slot] = str(v)
        elif isinstance(v, bytes):
            fields[slot] = v.hex()
        else:
            fields[slot] = v
    return {"variant_type": type(data).__name__, **fields}


__all__ = ["fetch_attestation_impl", "OraclePdaNotFound"]
