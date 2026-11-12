import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from refund_agent.graph import CASES, run_case
from refund_agent.events import canonical_events


class GraphTests(unittest.TestCase):
    def test_false_success_and_targeted_fix(self):
        baseline = run_case(CASES[0], "baseline", run_id="base-1")
        fixed = run_case(CASES[0], "fixed", run_id="fixed-1")
        self.assertTrue(baseline["eligible"])
        self.assertEqual(baseline["tool_status"], "rejected")
        self.assertEqual(baseline["ledger_effect_cents"], 0)
        self.assertEqual(baseline["claim"], "refunded")
        self.assertEqual(fixed["claim"], "refunded")
        self.assertEqual(fixed["ledger_effect_cents"], 4000)
        self.assertEqual(baseline["evidence"], fixed["evidence"])

    def test_ineligible_stays_denied(self):
        for variant in ("baseline", "fixed"):
            with self.subTest(variant=variant):
                result = run_case(CASES[1], variant)
                self.assertFalse(result["eligible"])
                self.assertFalse(result["attempted"])
                self.assertEqual(result["tool_status"], "not_called")
                self.assertEqual(result["claim"], "denied")

    def test_repeat_request_on_refunded_order_is_a_duplicate(self):
        fixed = run_case(CASES[0], "fixed", refunded_cents=4000)
        self.assertEqual(fixed["tool_status"], "duplicate")
        self.assertEqual(fixed["ledger_effect_cents"], 0)
        self.assertEqual(fixed["claim"], "already_refunded")
        # The baseline never posted O_1, so its repeat is still a tool failure it ignores.
        baseline = run_case(CASES[0], "baseline", refunded_cents=0)
        self.assertEqual(baseline["tool_status"], "rejected")
        self.assertEqual(baseline["claim"], "refunded")

    def test_invalid_variant_rejected(self):
        with self.assertRaises(ValueError):
            run_case(CASES[0], "unknown")

    def test_events_have_stable_ids_and_no_customer_data(self):
        result = run_case(CASES[0], "baseline", run_id="case-123")
        result["customer_email"] = "secret@example.com"
        events = canonical_events(result)
        self.assertEqual([event["kind"] for event in events],
                         ["policy_read", "tool_return", "agent_claim"])
        self.assertEqual([event["event_ordinal"] for event in events], [1, 2, 3])
        self.assertEqual(events, canonical_events(result))
        self.assertNotIn("secret@example.com", str(events))
        self.assertGreater(result["agent_latency_ms"], 0)


if __name__ == "__main__":
    unittest.main()