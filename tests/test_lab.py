import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from refund_agent.lab import (EVAL_KINDS, EVAL_REPEATS, EVAL_REQUESTS, SCENARIOS, eval_set,
                              generate_orders, generate_requests, scenario_orders,
                              scenario_requests, valid_cohort)

COHORT = "APP_0123456789ABCDEF"


class GeneratorTests(unittest.TestCase):
    def test_eval_set_is_reproducible_and_balanced_by_kind(self):
        orders, requests = eval_set(COHORT)
        self.assertEqual((orders, requests), eval_set(COHORT))
        self.assertNotEqual(orders, eval_set("APP_FEDCBA9876543210")[0])
        self.assertEqual(len(requests), EVAL_REQUESTS)
        self.assertEqual(EVAL_REQUESTS, 100)
        self.assertEqual([r["trial"] for r in requests], list(range(1, 101)))
        counts = {}
        for r in requests:
            counts[r["scenario"]] = counts.get(r["scenario"], 0) + 1
        expected = {name: count for name, count, *_ in EVAL_KINDS}
        expected["repeat_request"] = EVAL_REPEATS
        self.assertEqual(counts, expected)
        by_id = {o["order_id"]: o for o in orders}
        ranges = {name: ages for name, _, ages, *_ in EVAL_KINDS}
        seen = set()
        for r in requests:
            order = by_id[r["order_id"]]
            if r["scenario"] == "repeat_request":
                self.assertIn(r["order_id"], seen)  # the original came first
                continue
            seen.add(r["order_id"])
            low, high = ranges[r["scenario"]]
            self.assertTrue(low <= order["age_days"] <= high, r)
            self.assertEqual(r["partial"], r["amount_cents"] < order["amount_cents"])
        self.assertTrue(all(o["payment_status"] == "pending"
                            for o in orders if o["settled_cents"] == 0))

    def test_orders_are_reproducible_and_realistic(self):
        orders = generate_orders(COHORT, 50)
        self.assertEqual(orders, generate_orders(COHORT, 50))
        self.assertEqual(orders[0]["order_id"], f"{COHORT}_O01")
        recent = sum(o["age_days"] <= 30 for o in orders) / len(orders)
        self.assertGreater(recent, 0.6)
        for o in orders:
            settled = o["payment_status"] == "settled"
            self.assertEqual(o["settled_cents"], o["amount_cents"] if settled else 0)
            self.assertIn(o["processor_status"], ("ok", "declined"))
        # Across many simulations the messy cases all appear.
        many = [o for i in range(20) for o in generate_orders(f"APP_{i:016X}", 50)]
        share = lambda pred: sum(map(pred, many)) / len(many)
        self.assertAlmostEqual(share(lambda o: o["payment_status"] == "pending"), 0.10, delta=0.03)
        self.assertAlmostEqual(share(lambda o: o["processor_status"] == "declined"), 0.08, delta=0.03)
        self.assertAlmostEqual(share(lambda o: o["age_days"] <= 30), 0.80, delta=0.04)

    def test_requests_are_fewer_than_orders_with_some_partial(self):
        orders = generate_orders(COHORT, 20)
        requests = generate_requests(COHORT, orders, 15)
        self.assertEqual([r["trial"] for r in requests], list(range(1, 16)))
        totals = {o["order_id"]: o["amount_cents"] for o in orders}
        for r in requests:
            self.assertLessEqual(r["amount_cents"], totals[r["order_id"]])
            self.assertEqual(r["partial"], r["amount_cents"] < totals[r["order_id"]])
        with self.assertRaises(ValueError):
            generate_requests(COHORT, orders, 20)
        many = [r for i in range(30) for r in generate_requests(
            f"APP_{i:016X}", generate_orders(f"APP_{i:016X}", 20), 15)]
        self.assertAlmostEqual(sum(r["partial"] for r in many) / len(many), 0.20, delta=0.05)

    def test_scenario_set_covers_every_case_and_repeats_the_first_order(self):
        cohort = "APP_0123456789ABCDEF"
        orders, requests = scenario_orders(cohort), scenario_requests(cohort)
        self.assertEqual(len(orders), len(SCENARIOS))
        self.assertEqual(len(requests), len(SCENARIOS) + 1)
        names = {r["scenario"] for r in requests}
        self.assertEqual(names, {"eligible", "eligible_partial", "window_edge", "old_window_only",
                                 "outside_both", "payment_pending", "processor_declined",
                                 "repeat_request"})
        by_id = {o["order_id"]: o for o in orders}
        self.assertTrue(all(r["order_id"] in by_id for r in requests))
        self.assertEqual(requests[-1]["order_id"], requests[0]["order_id"])
        partial = next(r for r in requests if r["scenario"] == "eligible_partial")
        self.assertLess(partial["amount_cents"], by_id[partial["order_id"]]["amount_cents"])
        stale = [by_id[r["order_id"]]["age_days"] for r in requests
                 if r["scenario"] == "old_window_only"]
        self.assertTrue(all(14 < age <= 30 for age in stale))
        pending = next(o for o in orders if o["payment_status"] == "pending")
        self.assertEqual(pending["settled_cents"], 0)

    def test_cohort_ids_are_validated(self):
        self.assertTrue(valid_cohort(COHORT))
        self.assertFalse(valid_cohort("APP_123'; DROP"))


if __name__ == "__main__":
    unittest.main()


class SimulateTests(unittest.TestCase):
    def test_agents_batch_with_own_ledgers_and_observer_hooks(self):
        from unittest import mock

        from refund_agent import lab
        from refund_agent.agents import build_agent
        from tests.test_agents import FakeChat

        orders = generate_orders(COHORT, 4)
        facts = {o["order_id"]: o for o in orders}
        # Force a repeat request on one eligible order so the ledger matters.
        requests = [{"trial": 1, "order_id": orders[0]["order_id"],
                     "amount_cents": orders[0]["amount_cents"], "partial": False},
                    {"trial": 2, "order_id": orders[0]["order_id"],
                     "amount_cents": orders[0]["amount_cents"], "partial": False}]
        for o in orders:
            o.update(age_days=3, payment_status="settled", processor_status="ok")

        class Sink:
            rows = []
            def write(self, run): self.rows.append(run)
            def close(self): self.closed = True

        class Observer:
            batches, routed, callback_calls = [], [], []
            def batch(self, cohort, agent, graph, loop):
                self.batches.append(agent); loop()
            def invoke(self, agent, request, run_id, do_run):
                self.routed.append((agent, run_id)); return do_run()
            def callbacks(self, agent, request, run_id):
                self.callback_calls.append(agent); return []

        graphs = {a: build_agent(a, FakeChat) for a in ("lean", "fixed")}
        sink, observer = Sink(), Observer()
        with mock.patch.object(lab, "create_orders", return_value=orders), \
             mock.patch.object(lab, "load_facts", return_value=facts), \
             mock.patch.object(lab, "live_policy",
                               return_value={"policy_id": "P_2026_14D", "max_age_days": 14}), \
             mock.patch.object(lab, "generate_requests", return_value=requests):
            runs = lab.simulate(None, COHORT, 4, 2, graphs, sink, agents=("lean", "fixed"),
                                observers=[observer])
        self.assertEqual(observer.batches, ["lean", "fixed"])
        self.assertEqual(len(observer.routed), 4)
        self.assertEqual(observer.callback_calls, ["lean", "lean", "fixed", "fixed"])
        fixed = [r for r in runs if r["variant"] == "fixed"]
        self.assertEqual([r["tool_status"] for r in fixed], ["posted", "duplicate"])
        self.assertEqual(len(sink.rows), 4)
        self.assertEqual(len({r["run_id"] for r in runs}), 4)


class StreamingSinkTests(unittest.TestCase):
    def test_streams_ordered_rows_per_table_and_waits_for_commit(self):
        import os
        import tempfile
        from refund_agent.lab import StreamingSink

        class Channel:
            def __init__(self): self.rows, self.waited, self.closed = [], None, False
            def append_row(self, row, offset_token): self.rows.append((offset_token, row))
            def wait_for_commit(self, token_checker, timeout_seconds):
                self.waited = token_checker(self.rows[-1][0]) if self.rows else None
            def close(self): self.closed = True

        class Client:
            made = {}
            def __init__(self, client_name, db_name, schema_name, pipe_name, profile_json):
                self.pipe = pipe_name
                self.profile_mode = oct(os.stat(profile_json).st_mode & 0o777)
                Client.made[pipe_name] = self
            def open_channel(self, channel_name):
                self.channel = Channel(); return self.channel, "SUCCESS"
            def close(self): pass

        fd, key = tempfile.mkstemp(); os.write(fd, b"KEY"); os.close(fd); os.chmod(key, 0o600)
        run = {"cohort_id": COHORT, "trial": 1, "run_id": "r1", "variant": "fixed",
               "case_id": "C01_O01", "order_id": f"{COHORT}_O01",
               "decision_at": "2026-11-08T12:00:00+00:00", "amount_cents": 100,
               "order_total_cents": 100, "claim": "refunded", "attempted": True,
               "tool_status": "posted", "ledger_effect_cents": 100, "agent_policy_id": "P",
               "reply": "ok", "model_cost_credits": 0.0, "agent_latency_ms": 1.0,
               "eligible": True, "observed_at": "2026-11-08T12:00:01+00:00",
               "evidence": [{"policy_id": "P", "read_at": "x"}],
               "llm_calls": [{"node": "decide", "model": "m", "input_tokens": 1,
                              "output_tokens": 1, "latency_ms": 1, "est_credits": 0}]}
        sink = StreamingSink(COHORT, key, client_factory=Client)
        self.assertEqual(sorted(Client.made), ["LLM_CALLS-STREAMING", "RUNS-STREAMING",
                                               "TRACE_EVENTS-STREAMING"])
        self.assertTrue(all(c.profile_mode == "0o600" for c in Client.made.values()))
        sink.write(run)
        sink.write({**run, "run_id": "r2"})
        counts = sink.close()
        os.remove(key)
        self.assertEqual(counts, {"RUNS": 2, "TRACE_EVENTS": 6, "LLM_CALLS": 2})
        events = Client.made["TRACE_EVENTS-STREAMING"].channel
        self.assertEqual([t for t, _ in events.rows], [str(i) for i in range(1, 7)])
        self.assertTrue(events.waited and events.closed)
        self.assertIsNone(sink._profile)

    def test_refuses_admin_role(self):
        from refund_agent.lab import StreamingSink
        with self.assertRaises(ValueError):
            StreamingSink(COHORT, "/nonexistent", role="accountadmin")


class RestStreamingSinkTests(unittest.TestCase):
    RUN = {"cohort_id": COHORT, "trial": 1, "run_id": "r1", "variant": "fixed",
           "case_id": "C01_O01", "order_id": f"{COHORT}_O01",
           "decision_at": "2026-11-08T12:00:00+00:00", "amount_cents": 100,
           "order_total_cents": 100, "claim": "refunded", "attempted": True,
           "tool_status": "posted", "ledger_effect_cents": 100, "agent_policy_id": "P",
           "reply": "ok", "model_cost_credits": 0.0, "agent_latency_ms": 1.0,
           "eligible": True, "observed_at": "2026-11-08T12:00:01+00:00",
           "evidence": [{"policy_id": "P", "read_at": "x"}],
           "llm_calls": [{"node": "decide", "model": "m", "input_tokens": 1,
                          "output_tokens": 1, "latency_ms": 1, "est_credits": 0}]}

    def fake(self, fail_append=False):
        import json as _json

        class Response:
            def __init__(self, status=200, body=""):
                self.status_code, self.text = status, body
            def json(self):
                return _json.loads(self.text)

        class Http:
            def __init__(self):
                self.calls, self.committed = [], {}
            def request(self, method, url, headers, timeout, params=None, data=None):
                self.calls.append((method, url, params, data, headers))
                assert timeout and headers["Authorization"] == "Bearer TOKEN"
                if url.endswith("/v2/streaming/hostname"):
                    return Response(body="ACCT_X.ingest.example")
                if method == "PUT":
                    return Response(body='{"next_continuation_token": "c0"}')
                if url.endswith("/rows"):
                    if fail_append:
                        return Response(401, '{"code": "390422"}')
                    self.committed[url.split("/pipes/")[1].split("-")[0]] = params["endOffsetToken"]
                    assert "offsetToken" not in params
                    return Response(body='{"next_continuation_token": "c1"}')
                if url.endswith(":bulk-channel-status"):
                    table = url.split("/pipes/")[1].split("-")[0]
                    name = _json.loads(data)["channel_names"][0]
                    return Response(body=_json.dumps({"channel_statuses": {name: {
                        "channel_status_code": "SUCCESS",
                        "last_committed_offset_token": self.committed.get(table)}}}))
                return Response()
        return Http()

    def sink(self, http):
        import os
        import tempfile
        from refund_agent.lab import RestStreamingSink
        fd, path = tempfile.mkstemp(); os.write(fd, b"TOKEN\n"); os.close(fd)
        self.addCleanup(os.remove, path)
        return RestStreamingSink(COHORT, host="acct.snowflakecomputing.com", token_file=path, http=http)

    def test_appends_each_run_per_table_and_waits_for_commit(self):
        http = self.fake()
        sink = self.sink(http)
        self.assertEqual(sink.ingest, "acct-x.ingest.example")
        sink.write(self.RUN)
        sink.write({**self.RUN, "run_id": "r2"})
        self.assertEqual(sink.close(), {"RUNS": 2, "TRACE_EVENTS": 6, "LLM_CALLS": 2})
        appends = [c for c in http.calls if c[1].endswith("/rows")]
        events = [c for c in appends if "/TRACE_EVENTS-STREAMING/" in c[1]]
        self.assertTrue(all("/v2/streaming/data/databases/" in c[1] for c in appends))
        self.assertEqual([(c[2]["startOffsetToken"], c[2]["endOffsetToken"]) for c in events],
                         [("1", "3"), ("4", "6")])
        self.assertEqual(events[0][4]["Content-Type"], "application/x-ndjson")
        self.assertEqual(len(events[0][3].decode().splitlines()), 3)
        self.assertEqual(sum(c[0] == "DELETE" for c in http.calls), 3)

    def test_failed_request_raises_instead_of_retrying(self):
        sink = self.sink(self.fake(fail_append=True))
        with self.assertRaises(RuntimeError):
            sink.write(self.RUN)
