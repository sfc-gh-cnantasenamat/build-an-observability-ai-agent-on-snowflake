import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from langchain_core.messages import AIMessage

from refund_agent.observers import (NATIVE_JUDGE_STEP, LangfuseObserver, TruLensObserver,
                                    TruLensTracedChat, insert_values)

REQUEST = {"case_id": "C01_O01", "order_id": "O1", "amount_cents": 4000,
           "order_total_cents": 4000, "age_days": 20, "payment_status": "settled",
           "processor_status": "ok", "decision_at": "2026-11-08T12:00:00+00:00",
           "live_policy": {"policy_id": "P_2026_14D", "max_age_days": 14}}
RUN = {"run_id": "r1", "reply": "Your request is outside our 14-day window.", "eligible": False,
       "attempted": False, "tool_status": "not_called", "ledger_effect_cents": 0,
       "agent_policy_id": "P_2026_14D", "prior_refunded_cents": 0, "claim": "declined"}


class FakeStep:
    def __init__(self, kwargs):
        self.kwargs, self.ended, self.id = kwargs, False, "obs-1"

    def end(self):
        self.ended = True


class FakeLangfuse:
    def __init__(self):
        self.steps, self.scores = [], []

    def create_score(self, **kwargs):
        self.scores.append(kwargs)

    def flush(self):
        pass

    def create_trace_id(self, seed):
        return f"trace-{seed}"

    def start_observation(self, **kwargs):
        self.steps.append(FakeStep(kwargs))
        return self.steps[-1]


def native_observer():
    observer = LangfuseObserver.__new__(LangfuseObserver)
    observer.client, observer.judge_llm = FakeLangfuse(), None
    return observer


class NativeLangfuseJudgeTests(unittest.TestCase):
    def test_after_run_adds_tagged_judge_step_to_the_runs_trace(self):
        observer = native_observer()
        observer.after_run("lean", dict(RUN), REQUEST)
        (step,) = observer.client.steps
        kwargs = step.kwargs
        self.assertTrue(step.ended)
        self.assertEqual(kwargs["trace_context"], {"trace_id": "trace-r1"})
        self.assertEqual(kwargs["name"], NATIVE_JUDGE_STEP)
        self.assertEqual(kwargs["metadata"]["lab_step"], NATIVE_JUDGE_STEP)
        self.assertEqual(kwargs["output"], RUN["reply"])
        self.assertIn("$40.00", kwargs["input"]["request"])
        self.assertIn("14", kwargs["input"]["facts"])

    def test_export_keeps_only_native_numeric_scores(self):
        observer = native_observer()
        observer.fetch_observations = lambda run_id: []
        observer.fetch_scores = lambda run_id: [
            {"name": "groundedness", "source": "EVAL", "dataType": "NUMERIC", "value": 1},
            {"name": "answer_relevance", "source": "EVAL", "dataType": "NUMERIC", "value": 0.5},
            {"name": "relevance", "source": "API", "dataType": "NUMERIC", "value": 0.1}]

        class Cursor:
            rows = []
            def execute(self, sql, params=None):
                if params and "LANGFUSE_SCORES" in sql:
                    self.rows.extend(zip(*[iter(params)] * 5))
            def close(self):
                pass

        class Conn:
            def cursor(self):
                return Cursor()
            def commit(self):
                pass

        run = dict(RUN)
        result = observer.export(Conn(), [run], wait_s=0, score_wait_s=1)
        self.assertEqual(run["langfuse_scores"], {"groundedness": 1, "answer_relevance": 0.5})
        self.assertEqual(result["scores"], 2)
        self.assertEqual(sorted(row[2] for row in Cursor.rows), ["answer_relevance", "groundedness"])

    def test_insert_values_batches_rows_into_multi_row_statements(self):
        calls = []

        class Cursor:
            def execute(self, sql, params=None):
                calls.append((sql, params))

        rows = [(i, f"r{i}") for i in range(450)]
        insert_values(Cursor(), rows, "INSERT INTO T SELECT column1, column2 FROM VALUES {rows}")
        self.assertEqual(len(calls), 3)  # chunks of 200, 200, 50
        self.assertEqual(calls[0][0].count("(?, ?)"), 200)
        self.assertEqual(calls[2][0].count("(?, ?)"), 50)
        self.assertEqual([v for _, params in calls for v in params],
                         [v for row in rows for v in row])
        insert_values(Cursor(), [], "unused {rows}")
        self.assertEqual(len(calls), 3)


class TruthConn:
    """Answers the RUN_EVIDENCE lookups with fixed verdicts."""
    def __init__(self, rows):
        self.rows = rows

    def cursor(self):
        rows = self.rows

        class Cursor:
            def execute(self, sql, params=None):
                self.params = params
            def fetchall(self):
                return rows
            def close(self):
                pass
        return Cursor()


class BusinessTruthTests(unittest.TestCase):
    def test_langfuse_gets_truth_and_pass_scores_on_the_judge_step(self):
        observer = native_observer()
        run = dict(RUN, judge_step_id="obs-1",
                   langfuse_scores={"groundedness": 0.5, "answer_relevance": 0.4})
        posted = observer.post_truth(TruthConn([("r1", False)]), [run])
        scores = {s["name"]: s for s in observer.client.scores}
        self.assertEqual(posted, 4)
        self.assertEqual(scores["business_correct"]["value"], 0)
        self.assertEqual(scores["business_correct"]["data_type"], "BOOLEAN")
        self.assertEqual(scores["business_correct_value"]["data_type"], "NUMERIC")
        self.assertEqual(scores["groundedness_pass"]["value"], 1)
        self.assertEqual(scores["answer_relevance_pass"]["value"], 0)
        self.assertTrue(all(s["observation_id"] == "obs-1" and s["trace_id"] == "trace-r1"
                            for s in observer.client.scores))

    def test_trulens_metric_reads_the_oracle_verdict(self):
        metric = TruLensObserver.business_correct_metric(
            TruthConn([("r1", True), ("r2", False)]), "COHORT")
        self.assertEqual(metric.name, "business_correct")
        self.assertEqual(metric.imp("r1"), 1.0)
        self.assertEqual(metric.imp("r2"), 0.0)


class TruLensCostSpanTests(unittest.TestCase):
    def test_cost_attributes_use_token_usage_and_lab_rates(self):
        reply = AIMessage("{}", usage_metadata={"input_tokens": 1000, "output_tokens": 100,
                                                "total_tokens": 1100})
        chat = TruLensTracedChat(llm=None, model="claude-sonnet-5-5")
        attributes = TruLensTracedChat._cost_attributes(reply, None, chat)
        values = {key.rsplit(".", 1)[-1]: value for key, value in attributes.items()}
        self.assertEqual(values["num_tokens"], 1100)
        self.assertAlmostEqual(values["cost"], (1000 * 1.20 + 100 * 6.00) / 1e6)
        self.assertEqual(values["cost_currency"], "Snowflake AI credits")


if __name__ == "__main__":
    unittest.main()
