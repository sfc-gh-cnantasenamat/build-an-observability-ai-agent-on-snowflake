import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from langchain_core.messages import AIMessage

from refund_agent.agents import (AGENTS, CANNED_SUCCESS, Decision, Reply, build_agent,
                                 estimate_credits, refund_tool, run_request)


class FakeChat:
    """Stands in for ChatSnowflake: decides from the prompt's numbers and always reports
    the tool result honestly, so any false reply comes from the agent's own code."""

    def __init__(self, model):
        self.model = model

    def invoke(self, messages, config=None):
        system, human = messages[0].content, messages[1].content
        if '"eligible"' in system:
            days = int(system.split("within ")[1].split(" days")[0])
            age = int(human.split(": ")[1].split(" days")[0])
            body = Decision(eligible=age <= days and "payment: settled" in human,
                            reason="checked window").model_dump_json()
        else:
            status = human.split("Refund tool result: ")[1].split(",")[0]
            claim = {"posted": "refunded", "duplicate": "already_refunded",
                     "pending": "pending", "not_called": "denied"}.get(status, "failed")
            body = Reply(claim=claim, reply=f"Your refund is {claim}.").model_dump_json()
        return AIMessage(content=f"Sure. {body}", usage_metadata={
            "input_tokens": 100, "output_tokens": 20, "total_tokens": 120})


class FlakyChat(FakeChat):
    """Returns prose first, then valid JSON: the retry must be counted."""

    def __init__(self, model):
        super().__init__(model)
        self.calls = 0

    def invoke(self, messages, config=None):
        self.calls += 1
        if self.calls == 1:
            return AIMessage(content="I think yes.", usage_metadata={
                "input_tokens": 100, "output_tokens": 5, "total_tokens": 105})
        return super().invoke(messages, config)


def request(**overrides):
    base = {"case_id": "C_O01", "order_id": "APP_X_O01", "amount_cents": 4000,
            "order_total_cents": 4000, "age_days": 20, "payment_status": "settled",
            "processor_status": "ok", "decision_at": "2026-11-08T12:00:00+00:00",
            "live_policy": {"policy_id": "P_2026_14D", "max_age_days": 14}}
    return {**base, **overrides}


def run(agent, **overrides):
    graph = build_agent(agent, FakeChat)
    return run_request(request(**overrides), agent, graph, cohort="APP_X", trial=1)


class AgentTests(unittest.TestCase):
    def test_lean_uses_stale_policy(self):
        # 20 days old: inside the cached 30-day window, outside the live 14-day one.
        lean, fixed = run("lean"), run("fixed")
        self.assertEqual(lean["agent_policy_id"], "P_2026")
        self.assertTrue(lean["eligible"])
        self.assertEqual(lean["tool_status"], "posted")
        self.assertEqual(fixed["agent_policy_id"], "P_2026_14D")
        self.assertFalse(fixed["eligible"])
        self.assertEqual(fixed["claim"], "denied")

    def test_baseline_reply_template_claims_success_after_decline(self):
        # The model reports the decline honestly; reply template v1 overrides it.
        base = run("baseline", age_days=5, processor_status="declined")
        fixed = run("fixed", age_days=5, processor_status="declined")
        self.assertEqual(base["tool_status"], "declined")
        self.assertEqual(base["ledger_effect_cents"], 0)
        self.assertEqual(base["claim"], "refunded")
        self.assertEqual(base["reply"], CANNED_SUCCESS)
        self.assertEqual(base["evidence"][-1], {"kind": "agent_claim", "claim": "refunded"})
        # A refund the agent didn't attempt still gets the model's honest reply.
        self.assertEqual(run("baseline")["claim"], "denied")
        self.assertEqual(fixed["claim"], "failed")

    def test_tool_v1_refunds_whole_order_on_partial_request(self):
        base = run("baseline", age_days=5, amount_cents=1500, order_total_cents=6000)
        fixed = run("fixed", age_days=5, amount_cents=1500, order_total_cents=6000)
        self.assertEqual(base["ledger_effect_cents"], 6000)
        self.assertEqual(fixed["ledger_effect_cents"], 1500)

    def test_records_model_calls_and_cost(self):
        result = run("fixed", age_days=5)
        self.assertEqual([c["node"] for c in result["llm_calls"]], ["decide", "respond"])
        self.assertTrue(all(c["model"] == AGENTS["fixed"]["model"] for c in result["llm_calls"]))
        self.assertAlmostEqual(result["model_cost_credits"],
                               2 * estimate_credits(AGENTS["fixed"]["model"], 100, 20))
        lean = run("lean", age_days=5)
        self.assertLess(lean["model_cost_credits"], result["model_cost_credits"])

    def test_tool_rules(self):
        state = {"amount_cents": 1000, "order_total_cents": 3000, "refunded_cents": 0,
                 "payment_status": "pending", "processor_status": "ok"}
        self.assertEqual(refund_tool("v1", state), ("posted", 3000))
        self.assertEqual(refund_tool("v2", state), ("pending", 0))
        self.assertEqual(refund_tool("v2", {**state, "refunded_cents": 1000}), ("duplicate", 0))
        self.assertEqual(refund_tool("v1", {**state, "processor_status": "declined"}),
                         ("declined", 0))

    def test_run_metadata_reaches_callbacks(self):
        graph = build_agent("fixed", FakeChat)
        seen = {}

        def invoke(graph, graph_input, config):
            seen.update(config["metadata"])
            return graph.invoke(graph_input, config=config)

        result = run_request(request(age_days=5), "fixed", graph, cohort="APP_X", trial=3,
                             run_id="run-1", invoke_graph=invoke)
        self.assertEqual(seen, {"run_id": "run-1", "agent": "fixed", "cohort_id": "APP_X",
                                "trial": 3, "order_id": "APP_X_O01"})
        self.assertEqual(result["run_id"], "run-1")

    def test_malformed_output_is_retried_and_counted(self):
        graph = build_agent("fixed", FlakyChat)
        result = run_request(request(age_days=5), "fixed", graph, cohort="APP_X", trial=1)
        self.assertEqual([c["node"] for c in result["llm_calls"]],
                         ["decide", "decide", "respond"])
        self.assertEqual(result["claim"], "refunded")


if __name__ == "__main__":
    unittest.main()
