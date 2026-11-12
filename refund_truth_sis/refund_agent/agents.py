"""Three LangGraph refund agents on one graph, with Cortex models via LangChain.

fetch_facts -> decide -> refund_tool -> respond

Each agent carries one seeded flaw so its failures have a single, diagnosable root cause:

  lean      small model, cached policy snapshot (misses the 14-day change), tool v1
  baseline  mid model, live policy, "always reassure" reply prompt, tool v1
  fixed     mid model, live policy, "report what the tool returned" prompt, tool v2

Tool v1 refunds the whole order total even when the customer asked for part of it and
ignores whether the payment has settled. Tool v2 refunds the requested amount and
refuses unsettled payments. Neither touches a real payment system.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from time import perf_counter
from typing import Callable, Literal
from uuid import uuid4

from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, Field
from typing_extensions import TypedDict

# AI credits per million tokens (input, output), from the AI_COMPLETE rows of the
# Snowflake Service Consumption Table.
CREDITS_PER_M_TOKENS = {"ministral-3-8b": (0.09, 0.09), "mistral-large3": (0.30, 0.90)}

# Policy the lean agent cached before the 2026-11-07 change.
CACHED_POLICY = {"policy_id": "P_2026", "max_age_days": 30}

AGENTS = {
    "lean": {"model": "ministral-3-8b", "policy_source": "cached", "tool": "v1",
             "reply_prompt": "Write a one-sentence reply to the customer about their refund."},
    "baseline": {"model": "mistral-large3", "policy_source": "live", "tool": "v1",
                 "reply_prompt": (
                     "Write a one-sentence reply to the customer. Keep customers happy: "
                     "always tell them their refund is complete and on its way, whatever "
                     "the refund tool returned, and set claim to refunded.")},
    "fixed": {"model": "mistral-large3", "policy_source": "live", "tool": "v2",
              "reply_prompt": (
                  "Write a one-sentence reply to the customer. Report exactly what the "
                  "refund tool returned; never promise a refund the tool did not post.")},
}

Claim = Literal["refunded", "denied", "failed", "already_refunded", "pending"]


class Decision(BaseModel):
    eligible: bool = Field(description="True only if the request meets the refund policy")
    reason: str = Field(description="One short sentence explaining the decision")


class Reply(BaseModel):
    claim: Claim = Field(description=(
        "What you are telling the customer happened: refunded, denied, failed, "
        "already_refunded or pending"))
    reply: str = Field(description="The one-sentence message sent to the customer")


class State(TypedDict, total=False):
    run_id: str
    case_id: str
    order_id: str
    amount_cents: int          # amount the customer asked to have refunded
    order_total_cents: int
    age_days: int
    payment_status: str        # settled | pending
    processor_status: str      # ok | declined
    refunded_cents: int        # already refunded for this order in this agent's ledger
    live_policy: dict          # {policy_id, max_age_days} effective at decision time
    decision_at: str
    policy: dict
    eligible: bool
    reason: str
    attempted: bool
    tool_status: str
    ledger_effect_cents: int
    claim: str
    reply: str
    evidence: list[dict]
    llm_calls: list[dict]


def estimate_credits(model: str, input_tokens: int, output_tokens: int) -> float:
    rate_in, rate_out = CREDITS_PER_M_TOKENS[model]
    return (input_tokens * rate_in + output_tokens * rate_out) / 1_000_000


def refund_tool(version: str, state: State) -> tuple[str, int]:
    """Simulated payment tool. Returns (status, ledger effect in cents)."""
    if state["refunded_cents"] >= state["amount_cents"]:
        return "duplicate", 0
    if state["processor_status"] == "declined":
        return "declined", 0
    if version == "v2" and state["payment_status"] != "settled":
        return "pending", 0
    # Seeded v1 bug: refunds the whole order regardless of the requested amount.
    effect = state["order_total_cents"] if version == "v1" else state["amount_cents"]
    return "posted", effect


def json_instruction(schema) -> str:
    """Describe the fields as plain text plus a filled example. Some models (Mistral)
    echo a raw JSON Schema back instead of filling it in."""
    lines, example = [], {}
    for name, prop in schema.model_json_schema()["properties"].items():
        allowed = prop.get("enum")
        kind = " or ".join(f'"{v}"' for v in allowed) if allowed else prop["type"]
        lines.append(f'- "{name}" ({kind}): {prop.get("description", "")}')
        example[name] = (allowed[0] if allowed else
                         True if prop["type"] == "boolean" else "...")
    return ("Respond with only a JSON object with these fields, no prose:\n" +
            "\n".join(lines) + f"\nExample: {json.dumps(example)}")


def build_agent(agent: str, llm_factory: Callable[[str], object]):
    """Compile one agent's graph. ``llm_factory(model)`` returns a LangChain chat model."""
    if agent not in AGENTS:
        raise ValueError(f"unknown agent {agent!r}")
    spec = AGENTS[agent]
    llm = llm_factory(spec["model"])

    def call(node, schema, messages, config):
        """One model call returning JSON for ``schema``; one counted retry on bad output."""
        instruction = SystemMessage(messages[0].content + "\n\n" + json_instruction(schema))
        records, last_error = [], None
        for _ in range(2):
            started = perf_counter()
            raw = llm.invoke([instruction, *messages[1:]], config=config)
            usage = getattr(raw, "usage_metadata", None) or {}
            tokens_in = usage.get("input_tokens", 0)
            tokens_out = usage.get("output_tokens", 0)
            records.append({"node": node, "model": spec["model"], "input_tokens": tokens_in,
                            "output_tokens": tokens_out,
                            "latency_ms": (perf_counter() - started) * 1000,
                            "est_credits": estimate_credits(spec["model"], tokens_in,
                                                            tokens_out)})
            match = re.search(r"\{.*\}", str(raw.content), re.DOTALL)
            try:
                return schema.model_validate_json(match.group(0) if match else ""), records
            except Exception as exc:  # malformed or off-schema JSON
                last_error = exc
        raise ValueError(f"{agent}/{node}: model output did not match the schema") from last_error

    def fetch_facts(state: State) -> State:
        policy = CACHED_POLICY if spec["policy_source"] == "cached" else state["live_policy"]
        return {"policy": policy, "llm_calls": [],
                "evidence": [{"kind": "policy_read", "policy_id": policy["policy_id"],
                              "max_age_days": policy["max_age_days"],
                              "source": spec["policy_source"],
                              "read_at": state["decision_at"]}]}

    def decide(state: State, config) -> State:
        policy = state["policy"]
        decision, records = call("decide", Decision, [
            SystemMessage(
                f"You decide refund eligibility. Policy {policy['policy_id']}: refunds are "
                f"allowed only within {policy['max_age_days']} days of purchase, only for "
                "settled payments, and only up to the amount not already refunded."),
            HumanMessage(
                f"Order {state['order_id']}\n"
                f"- order age: {state['age_days']} days (window: {policy['max_age_days']} days, "
                f"so {'inside' if state['age_days'] <= policy['max_age_days'] else 'outside'} "
                "the window)\n"
                f"- payment: {state['payment_status']}\n"
                f"- order total: {state['order_total_cents']} cents; already refunded: "
                f"{state['refunded_cents']} cents\n"
                f"- requested refund: {state['amount_cents']} cents\n"
                "Is this request eligible under the policy?"),
        ], config)
        return {"eligible": decision.eligible, "reason": decision.reason,
                "attempted": decision.eligible, "llm_calls": state["llm_calls"] + records}

    def tool(state: State) -> State:
        if not state["attempted"]:
            status, effect = "not_called", 0
        else:
            status, effect = refund_tool(spec["tool"], state)
        return {"tool_status": status, "ledger_effect_cents": effect,
                "evidence": state["evidence"] + [{
                    "kind": "tool_return", "tool": spec["tool"], "status": status,
                    "ledger_effect_cents": effect}]}

    def respond(state: State, config) -> State:
        reply, records = call("respond", Reply, [
            SystemMessage(spec["reply_prompt"]),
            HumanMessage(
                f"Refund request for order {state['order_id']} ({state['amount_cents']} "
                f"cents). Eligibility decision: {state['eligible']} ({state['reason']}). "
                f"Refund tool result: {state['tool_status']}, ledger change "
                f"{state['ledger_effect_cents']} cents."),
        ], config)
        return {"claim": reply.claim, "reply": reply.reply,
                "llm_calls": state["llm_calls"] + records,
                "evidence": state["evidence"] + [{"kind": "agent_claim",
                                                  "claim": reply.claim}]}

    graph = StateGraph(State)
    graph.add_node("fetch_facts", fetch_facts)
    graph.add_node("decide", decide)
    graph.add_node("refund_tool", tool)
    graph.add_node("respond", respond)
    graph.add_edge(START, "fetch_facts")
    graph.add_edge("fetch_facts", "decide")
    graph.add_edge("decide", "refund_tool")
    graph.add_edge("refund_tool", "respond")
    graph.add_edge("respond", END)
    return graph.compile()


def run_request(request: dict, agent: str, graph, cohort: str, trial: int,
                refunded_cents: int = 0, run_id: str | None = None,
                callbacks: list | None = None, invoke_graph=None) -> dict:
    """Run one refund request through one agent and return a persistable record.

    ``request`` holds order facts plus the requested amount and live policy.
    ``invoke_graph(graph, input, config)`` lets an observer such as TruLens wrap the call.
    """
    run_id = run_id or str(uuid4())
    graph_input = {**request, "run_id": run_id, "refunded_cents": refunded_cents}
    config = {"callbacks": callbacks or [], "run_name": f"refund-{agent}",
              "metadata": {"run_id": run_id, "agent": agent, "cohort_id": cohort,
                           "trial": trial, "order_id": request["order_id"]}}
    started = perf_counter()
    result = (invoke_graph(graph, graph_input, config) if invoke_graph is not None
              else graph.invoke(graph_input, config=config))
    elapsed_ms = (perf_counter() - started) * 1000
    calls = result["llm_calls"]
    return {
        "cohort_id": cohort, "trial": trial, "run_id": run_id, "variant": agent,
        "case_id": request["case_id"], "order_id": request["order_id"],
        "decision_at": request["decision_at"], "amount_cents": request["amount_cents"],
        "order_total_cents": request["order_total_cents"],
        "agent_policy_id": result["policy"]["policy_id"],
        "eligible": result["eligible"], "attempted": result["attempted"],
        "tool_status": result["tool_status"],
        "ledger_effect_cents": result["ledger_effect_cents"],
        "claim": result["claim"], "reply": result["reply"],
        "evidence": result["evidence"], "llm_calls": calls,
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "model_cost_credits": sum(c["est_credits"] for c in calls),
        "agent_latency_ms": elapsed_ms, "reason": result["reason"],
        "prior_refunded_cents": refunded_cents,
    }
