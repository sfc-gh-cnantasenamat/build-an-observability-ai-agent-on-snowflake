"""AppTest for the Streamlit lab: leaderboard reveal, request verdicts, investigate."""

import sys
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd

APP = Path(__file__).resolve().parents[1] / "refund_truth_sis"
sys.path.insert(0, str(APP))

from streamlit.testing.v1 import AppTest  # noqa: E402

COHORT = "APP_0123456789ABCDEF"


def run_rows():
    base = {"cohort_id": COHORT, "decision_at": "2026-11-08", "order_total_cents": 4000,
            "amount_cents": 4000, "age_days": 20, "payment_status": "settled",
            "processor_status": "ok", "policy_id": "P_2026_14D", "max_age_days": 14,
            "reply": "Your refund is done.", "order_id": f"{COHORT}_O01"}
    rows = []
    for trial in (1, 2):
        rows += [
            {**base, "trial": trial, "run_id": f"lean{trial}", "variant": "lean",
             "agent_policy_id": "P_2026", "attempted": True, "tool_status": "posted",
             "ledger_effect_cents": 4000, "expected_effect_cents": 0, "claim": "refunded",
             "business_correct": False, "false_success": False, "customer_misinformed": False,
             "money_at_risk_cents": 4000, "root_cause": "stale_data",
             "model_cost_credits": 50e-6, "agent_latency_ms": 2000},
            {**base, "trial": trial, "run_id": f"base{trial}", "variant": "baseline",
             "agent_policy_id": "P_2026_14D", "attempted": False, "tool_status": "not_called",
             "ledger_effect_cents": 0, "expected_effect_cents": 0, "claim": "refunded",
             "business_correct": False, "false_success": True, "customer_misinformed": True,
             "money_at_risk_cents": 0, "root_cause": "prompt",
             "model_cost_credits": 160e-6, "agent_latency_ms": 3000},
            {**base, "trial": trial, "run_id": f"fix{trial}", "variant": "fixed",
             "agent_policy_id": "P_2026_14D", "attempted": False, "tool_status": "not_called",
             "ledger_effect_cents": 0, "expected_effect_cents": 0, "claim": "denied",
             "business_correct": True, "false_success": False, "customer_misinformed": False,
             "money_at_risk_cents": 0, "root_cause": None,
             "model_cost_credits": 158e-6, "agent_latency_ms": 2800},
        ]
    return pd.DataFrame(rows)


class FakeConnection:
    raw_connection = object()

    def query(self, sql, params=None, ttl=None):
        if "CURRENT_ORGANIZATION_NAME" in sql:
            return pd.DataFrame({"O": ["MYORG"], "A": ["MY_ACCOUNT"]})
        if "GROUP BY r.COHORT_ID" in sql:
            return pd.DataFrame({"COHORT_ID": [COHORT], "LAST_EVENT": ["2026-11-08"]})
        if "RUN_EVIDENCE" in sql:
            return run_rows().rename(columns=str.upper)
        if "LANGFUSE_SCORES" in sql:
            return pd.DataFrame({"RUN_ID": ["base1", "base1"], "METRIC": ["answer_relevance", "groundedness"],
                                 "SCORE": [1.0, 1.0], "TRACE_ID": ["t1", "t1"]})
        if "TRULENS_SCORES" in sql:
            return pd.DataFrame(columns=["RUN_ID", "METRIC", "SCORE"])
        if "JUDGE_AGREEMENT" in sql:
            return pd.DataFrame(columns=["COHORT_ID", "TOOL", "METRIC", "N", "KAPPA"])
        return pd.DataFrame()


class AppTests(unittest.TestCase):
    def app(self):
        with mock.patch("streamlit.connection", return_value=FakeConnection()):
            at = AppTest.from_file(str(APP / "streamlit_app.py"), default_timeout=60)
            at.run()
        return at

    def test_truth_hidden_until_reveal(self):
        with mock.patch("streamlit.connection", return_value=FakeConnection()):
            at = AppTest.from_file(str(APP / "streamlit_app.py"), default_timeout=60)
            at.run()
            self.assertFalse(at.exception, at.exception)
            board = at.dataframe[0].value
            self.assertEqual(list(board.Agent), ["Lean", "Fixed", "Baseline"])
            self.assertNotIn("Business-correct %", board.columns)
            self.assertEqual(board.set_index("Agent").loc["Baseline", "Langfuse relevance"], 1.0)
            self.assertTrue(any("Scores pending" in c.value for c in at.caption))

            at.segmented_control[0].set_value("Lean").run()
            at.button(key="reveal").click().run()
            board = at.dataframe[0].value.set_index("Agent")
            self.assertEqual(board.loc["Fixed", "Business-correct %"], 100)
            self.assertEqual(board.loc["Lean", "Money at risk ($)"], 80.0)
            self.assertEqual(board.loc["Baseline", "False successes"], 2)
            self.assertTrue(any("most business-correct agent is Fixed" in e.value for e in at.error))

    def test_requests_and_investigate_show_every_agent(self):
        with mock.patch("streamlit.connection", return_value=FakeConnection()):
            at = AppTest.from_file(str(APP / "streamlit_app.py"), default_timeout=60)
            at.run()
            requests = at.dataframe[1].value
            self.assertEqual(list(requests.Request), [1, 2])
            self.assertEqual(requests.Lean.iloc[0], "Wrong: stale policy")
            self.assertEqual(requests.Baseline.iloc[0], "Wrong: false reply")
            self.assertEqual(requests.Fixed.iloc[0], "Correct")
            at.toggle(key="wrong_only").set_value(True).run()
            self.assertEqual(len(at.dataframe[1].value), 2)
            self.assertTrue(any("Matches business truth" in s.value for s in at.success))
            self.assertEqual(sum("Wrong" in e.value for e in at.error), 2)


if __name__ == "__main__":
    unittest.main()
