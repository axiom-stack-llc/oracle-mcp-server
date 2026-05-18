"""Tool: axiom_fetch_audit_reference.

Returns the listener's current operational-integrity anchor (CXO-locked
schema per ``PHASE_B_AUDIT_CHAIN_DIRECTIVE.md §A``). Fail-open: never
raises; ``oracle_anchor_status: "unanchored"`` on any failure mode.

Anonymous-tier allowed (read-only).
"""

from __future__ import annotations

from typing import Any


def fetch_audit_reference_impl(client, timeout_seconds: float = 3.0) -> dict[str, Any]:
    """Run fetch_oracle_audit_reference via the supplied OracleClient.

    Never raises (per the SDK helper's fail-open contract). FastMCP returns
    the dict directly as the tool result.
    """
    return client.fetch_oracle_audit_reference(timeout_seconds=timeout_seconds)


__all__ = ["fetch_audit_reference_impl"]
