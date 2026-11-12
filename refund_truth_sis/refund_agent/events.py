"""Redacted, application-owned run events; not vendor observability records."""

from __future__ import annotations

from uuid import uuid5, NAMESPACE_URL


def canonical_events(run: dict) -> list[dict]:
    """Create replay-stable event IDs and a strict, non-sensitive payload."""
    run_id = run["run_id"]
    allowed = (
        ("policy_read", "decide", {
            "policy_id": run["evidence"][0]["policy_id"],
            "eligible": run["eligible"],
            "read_at": run["evidence"][0]["read_at"],
        }),
        ("tool_return", "refund_tool", {
            "status": run["tool_status"],
            "ledger_effect_cents": run["ledger_effect_cents"],
        }),
        ("agent_claim", "respond", {"claim": run["claim"]}),
    )
    return [
        {
            "event_id": str(uuid5(NAMESPACE_URL, f"refund-lab/{run_id}/{kind}")),
            "run_id": run_id,
            "event_at": run["observed_at"],
            "event_ordinal": position,
            "kind": kind,
            "node": node,
            "detail": detail,
        }
        for position, (kind, node, detail) in enumerate(allowed, start=1)
    ]