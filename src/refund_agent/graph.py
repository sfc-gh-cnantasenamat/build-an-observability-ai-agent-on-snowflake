"""Deterministic LangGraph refund workflow; no real payment or model calls."""

from __future__ import annotations

from datetime import datetime, timezone
from time import perf_counter
from typing_extensions import TypedDict
from uuid import uuid4

from langgraph.graph import END, START, StateGraph


CASES = (
    {"case_id": "C_ELIGIBLE", "order_id": "O_1", "amount_cents": 4000,
     "decision_at": "2026-11-06T12:00:00+00:00", "expected": "refund"},
    {"case_id": "C_INELIGIBLE", "order_id": "O_2", "amount_cents": 2500,
     "decision_at": "2026-11-06T12:00:00+00:00", "expected": "deny"},
)

ORDERS = {"O_1": {"age_days": 10}, "O_2": {"age_days": 40}}
SETTLED = {"O_1": 4000, "O_2": 2500}
POLICY = {"policy_id": "P_2026", "effective_from": "2026-11-01T00:00:00+00:00",
          "max_age_days": 30}


class State(TypedDict, total=False):
    case_id: str
    order_id: str
    amount_cents: int
    age_days: int
    decision_at: str
    variant: str
    refunded_cents: int
    eligible: bool
    attempted: bool
    tool_status: str
    ledger_effect_cents: int
    claim: str
    evidence: list[dict]


def build_graph(variant: str):
    if variant not in ("baseline", "fixed"):
        raise ValueError("variant must be baseline or fixed")

    def decide(state: State) -> State:
        # Generated orders carry their own age; the two reference orders use fixtures.
        age_days = state.get("age_days", ORDERS.get(state["order_id"], {}).get("age_days"))
        eligible = age_days is not None and age_days <= POLICY["max_age_days"]
        return {"eligible": eligible, "attempted": eligible,
                "evidence": [{"kind": "policy_read", "policy_id": POLICY["policy_id"],
                              "effective_from": POLICY["effective_from"],
                              "read_at": state["decision_at"], "eligible": eligible}]}

    def refund_tool(state: State) -> State:
        if not state["attempted"]:
            return {"tool_status": "not_called", "ledger_effect_cents": 0}
        # A refund already posted for this order in this simulated ledger blocks a repeat.
        if state.get("refunded_cents", 0) >= state["amount_cents"]:
            return {"tool_status": "duplicate", "ledger_effect_cents": 0}
        # Seeded bug: the baseline sends a malformed refund call that the payment
        # tool rejects. The corrected tool handles the valid request. Neither
        # version writes to a payment provider or shared ledger.
        status = "rejected" if variant == "baseline" else "posted"
        effect = state["amount_cents"] if status == "posted" else 0
        return {"tool_status": status, "ledger_effect_cents": effect}

    def respond(state: State) -> State:
        if not state["attempted"]:
            claim = "denied"
        elif variant == "baseline":
            claim = "refunded"  # Seeded bug: trusts intent, not the tool result.
        else:
            claim = {"posted": "refunded", "duplicate": "already_refunded"}.get(
                state["tool_status"], "failed")
        return {"claim": claim}

    graph = StateGraph(State)
    graph.add_node("decide", decide)
    graph.add_node("refund_tool", refund_tool)
    graph.add_node("respond", respond)
    graph.add_edge(START, "decide")
    graph.add_edge("decide", "refund_tool")
    graph.add_edge("refund_tool", "respond")
    graph.add_edge("respond", END)
    return graph.compile()


def run_case(case: dict, variant: str, cohort: str = "LAB_20261106",
             trial: int = 1, run_id: str | None = None,
             invoke_graph=None, refunded_cents: int = 0) -> dict:
    """Return a complete, labeled fixture run for external event emission.

    ``refunded_cents`` is what this variant's simulated ledger already refunded
    for the order earlier in the same simulation.
    """
    invocation_id = run_id or str(uuid4())
    started = perf_counter()
    graph = build_graph(variant)
    graph_input = {**case, "variant": variant, "cohort_id": cohort,
                   "run_id": invocation_id, "refunded_cents": refunded_cents}
    result = (invoke_graph(graph, graph_input) if invoke_graph is not None
              else graph.invoke(graph_input))
    elapsed_ms = (perf_counter() - started) * 1000
    return {"cohort_id": cohort, "trial": trial, "run_id": invocation_id,
            "variant": variant, "case_id": case["case_id"],
            "order_id": case["order_id"], "decision_at": case["decision_at"],
            "amount_cents": case["amount_cents"], "expected": case["expected"],
            "eligible": result["eligible"], "attempted": result["attempted"],
            "tool_status": result["tool_status"],
            "ledger_effect_cents": result["ledger_effect_cents"],
            "claim": result["claim"], "evidence": result["evidence"],
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "model_cost_usd": None, "agent_latency_ms": elapsed_ms}