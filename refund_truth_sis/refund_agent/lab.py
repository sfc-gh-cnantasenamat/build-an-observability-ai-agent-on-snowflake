"""Shared lab plumbing: realistic synthetic orders, refund requests, sinks, simulation.

Used by both the notebook and the Streamlit app. Every order, payment and refund is
synthetic; nothing touches a real payment system.
"""

from __future__ import annotations

import json
import random
from typing import Callable, Iterable, Protocol

from refund_agent import config
from refund_agent.events import canonical_events

SCHEMA = "REFUND_AGENT_LAB.DEMO"
MAX_ORDERS = 50
# New simulations are decided after the 2026-11-07 policy change (30 -> 14 days).
DECISION_AT = "2026-11-08T12:00:00+00:00"
AGENT_NAMES = ("lean", "baseline", "fixed")
# The streaming identity must be least-privilege; these roles are refused.
ADMIN_ROLES = {"ACCOUNTADMIN", "ORGADMIN", "SECURITYADMIN", "SYSADMIN", "USERADMIN"}

LIVE_POLICY_SQL = f"""
SELECT POLICY_ID, MAX_AGE_DAYS FROM {SCHEMA}.POLICIES
WHERE EFFECTIVE_FROM <= TO_TIMESTAMP_TZ(?)
  AND (EFFECTIVE_TO IS NULL OR TO_TIMESTAMP_TZ(?) < EFFECTIVE_TO)
ORDER BY EFFECTIVE_FROM DESC LIMIT 1
"""

COHORT_FACTS_SQL = f"""
SELECT o.ORDER_ID, o.AGE_DAYS, o.AMOUNT_CENTS, p.SETTLED_CENTS,
       p.PAYMENT_STATUS, p.PROCESSOR_STATUS
FROM {SCHEMA}.ORDERS o JOIN {SCHEMA}.PAYMENTS p ON p.ORDER_ID = o.ORDER_ID
WHERE STARTSWITH(o.ORDER_ID, ?)
ORDER BY o.ORDER_ID
"""


def valid_cohort(cohort: str) -> bool:
    return (cohort.startswith("APP_") and len(cohort) == 20
            and all(c in "0123456789ABCDEF" for c in cohort[4:]))


def generate_orders(cohort: str, count: int) -> list[dict]:
    """Reproducible synthetic orders with realistic messiness.

    About 80% are under 30 days old, 10% have a pending (unsettled) payment and 8%
    are on a card the processor will decline.
    """
    if not isinstance(count, int) or not 2 <= count <= MAX_ORDERS:
        raise ValueError(f"Choose between 2 and {MAX_ORDERS} orders")
    rng = random.Random(cohort)
    orders = []
    for number in range(1, count + 1):
        age = rng.randint(1, 30) if rng.random() < 0.8 else rng.randint(31, 90)
        amount = rng.randrange(1000, 10001, 50)
        pending = rng.random() < 0.10
        orders.append({
            "order_id": f"{cohort}_O{number:02d}", "age_days": age, "amount_cents": amount,
            "payment_status": "pending" if pending else "settled",
            "settled_cents": 0 if pending else amount,
            "processor_status": "declined" if rng.random() < 0.08 else "ok",
        })
    return orders


# A fixed scenario set: each seeded flaw is triggered a known number of times, so
# scores mean the same thing from run to run. (name, age, amount, payment, processor,
# requested amount or None for the full order). The last request repeats the first.
SCENARIOS = (
    ("eligible", 5, 4200, "settled", "ok", None),
    ("eligible", 9, 7550, "settled", "ok", None),
    ("eligible_partial", 6, 8000, "settled", "ok", 3200),
    ("window_edge", 14, 2600, "settled", "ok", None),
    ("old_window_only", 20, 5900, "settled", "ok", None),
    ("old_window_only", 27, 3350, "settled", "ok", None),
    ("outside_both", 45, 6100, "settled", "ok", None),
    ("payment_pending", 8, 4800, "pending", "ok", None),
    ("processor_declined", 4, 2950, "settled", "declined", None),
)
REPEAT_SCENARIO = "repeat_request"


def scenario_orders(cohort: str) -> list[dict]:
    return [{"order_id": f"{cohort}_O{number:02d}", "age_days": age, "amount_cents": amount,
             "payment_status": payment, "settled_cents": amount if payment == "settled" else 0,
             "processor_status": processor}
            for number, (_, age, amount, payment, processor, _) in enumerate(SCENARIOS, 1)]


def scenario_requests(cohort: str) -> list[dict]:
    requests = []
    for number, (name, _, amount, _, _, asked) in enumerate(SCENARIOS, 1):
        requests.append({"trial": number, "order_id": f"{cohort}_O{number:02d}",
                         "amount_cents": asked or amount, "partial": asked is not None,
                         "scenario": name})
    first = requests[0]
    requests.append(dict(first, trial=len(requests) + 1, scenario=REPEAT_SCENARIO))
    return requests


# The judge-alignment eval set: the same scenario kinds as SCENARIOS, many variants each,
# reproducible from the simulation ID. Per kind: (count, age range, payment, processor,
# partial). Ages sit inside each kind's window (the live policy allows 14 days, the lean
# agent's cached one 30). Repeat requests re-ask for an eligible order already requested.
EVAL_KINDS = (
    ("eligible", 20, (1, 13), "settled", "ok", False),
    ("eligible_partial", 12, (1, 13), "settled", "ok", True),
    ("window_edge", 6, (14, 14), "settled", "ok", False),
    ("old_window_only", 18, (15, 30), "settled", "ok", False),
    ("outside_both", 10, (31, 90), "settled", "ok", False),
    ("payment_pending", 10, (1, 13), "pending", "ok", False),
    ("processor_declined", 12, (1, 13), "settled", "declined", False),
)
EVAL_REPEATS = 12
EVAL_REQUESTS = sum(kind[1] for kind in EVAL_KINDS) + EVAL_REPEATS  # 100 per agent


def eval_set(cohort: str) -> tuple[list[dict], list[dict]]:
    """(orders, requests) for the eval set, in a shuffled but reproducible order."""
    rng = random.Random(f"{cohort}/eval")
    specs = [(name, ages, payment, processor, partial)
             for name, count, ages, payment, processor, partial in EVAL_KINDS
             for _ in range(count)]
    rng.shuffle(specs)
    orders, requests = [], []
    for number, (name, ages, payment, processor, partial) in enumerate(specs, 1):
        amount = rng.randrange(1000, 10001, 50)
        order = {"order_id": f"{cohort}_O{number:03d}", "age_days": rng.randint(*ages),
                 "amount_cents": amount, "payment_status": payment,
                 "settled_cents": amount if payment == "settled" else 0,
                 "processor_status": processor}
        asked = rng.randrange(amount // 5, amount * 4 // 5, 50) if partial else amount
        orders.append(order)
        requests.append({"order_id": order["order_id"], "amount_cents": asked,
                         "partial": partial, "scenario": name})
    eligible = [r for r in requests if r["scenario"] == "eligible"]
    for first in rng.sample(eligible, EVAL_REPEATS):
        requests.append(dict(first, scenario=REPEAT_SCENARIO))
    # Repeats go after their originals; everything else stays shuffled.
    return orders, [dict(r, trial=trial) for trial, r in enumerate(requests, 1)]


def generate_requests(cohort: str, orders: list[dict], count: int) -> list[dict]:
    """Refund requests: fewer than orders, each for a random order; ~20% partial."""
    if not isinstance(count, int) or not 1 <= count < len(orders):
        raise ValueError("Refund requests must be at least 1 and fewer than orders")
    rng = random.Random(f"{cohort}/requests")
    requests = []
    for trial in range(1, count + 1):
        order = orders[rng.randrange(len(orders))]
        partial = rng.random() < 0.20
        amount = (rng.randrange(500, order["amount_cents"], 50)
                  if partial and order["amount_cents"] > 550 else order["amount_cents"])
        requests.append({"trial": trial, "order_id": order["order_id"],
                         "amount_cents": amount, "partial": amount < order["amount_cents"]})
    return requests


def _guard_account(cursor):
    cursor.execute("SELECT CURRENT_ORGANIZATION_NAME(), CURRENT_ACCOUNT()")
    if tuple(cursor.fetchone()) != config.expected_identity():
        raise RuntimeError(f"Refusing to write outside {config.ACCOUNT} (REFUND_LAB_ACCOUNT)")


def create_orders(conn, cohort: str, count: int, orders: list[dict] | None = None) -> list[dict]:
    """Insert one simulation's synthetic orders and payments in a transaction."""
    if not valid_cohort(cohort):
        raise ValueError("Invalid simulation ID")
    orders = orders if orders is not None else generate_orders(cohort, count)
    cursor = conn.cursor()
    try:
        _guard_account(cursor)
        cursor.execute("BEGIN")
        cursor.executemany(
            f"INSERT INTO {SCHEMA}.ORDERS (ORDER_ID, AGE_DAYS, AMOUNT_CENTS) VALUES (?, ?, ?)",
            [(o["order_id"], o["age_days"], o["amount_cents"]) for o in orders])
        cursor.executemany(
            f"INSERT INTO {SCHEMA}.PAYMENTS (ORDER_ID, SETTLED_CENTS, PAYMENT_STATUS, "
            "PROCESSOR_STATUS) VALUES (?, ?, ?, ?)",
            [(o["order_id"], o["settled_cents"], o["payment_status"], o["processor_status"])
             for o in orders])
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cursor.close()
    return orders


def fetch_rows(conn, sql: str, params: Iterable) -> list[dict]:
    cursor = conn.cursor()
    try:
        cursor.execute(sql, tuple(params))
        names = [d[0].lower() for d in cursor.description]
        return [dict(zip(names, row)) for row in cursor.fetchall()]
    finally:
        cursor.close()


def load_facts(conn, cohort: str) -> dict[str, dict]:
    """Order facts as stored in Snowflake (requests use these, not the generator copy)."""
    return {row["order_id"]: row for row in fetch_rows(conn, COHORT_FACTS_SQL, [f"{cohort}_"])}


def live_policy(conn, decision_at: str = DECISION_AT) -> dict:
    rows = fetch_rows(conn, LIVE_POLICY_SQL, [decision_at, decision_at])
    if not rows:
        raise ValueError("No policy is effective at the decision time")
    return {"policy_id": rows[0]["policy_id"], "max_age_days": int(rows[0]["max_age_days"])}


def agent_request(cohort: str, request: dict, facts: dict, policy: dict) -> dict:
    label = request["order_id"].rsplit("_", 1)[-1]
    return {"case_id": f"C{request['trial']:02d}_{label}", "order_id": request["order_id"],
            "amount_cents": request["amount_cents"],
            "order_total_cents": int(facts["amount_cents"]),
            "age_days": int(facts["age_days"]), "payment_status": facts["payment_status"],
            "processor_status": facts["processor_status"],
            "decision_at": DECISION_AT, "live_policy": policy}


class EventSink(Protocol):
    """Where a finished run goes: RUNS, TRACE_EVENTS and LLM_CALLS."""

    def write(self, run: dict) -> None: ...

    def close(self) -> None: ...


class SqlSink:
    """Writes each run with ordinary INSERTs in one transaction (qmark binds)."""

    name = "sql"

    def __init__(self, conn):
        self.conn = conn
        cursor = conn.cursor()
        try:
            _guard_account(cursor)
        finally:
            cursor.close()

    def write(self, run: dict) -> None:
        cursor = self.conn.cursor()
        try:
            cursor.execute("BEGIN")
            cursor.execute(
                f"INSERT INTO {SCHEMA}.RUNS (COHORT_ID, TRIAL, RUN_ID, VARIANT, CASE_ID, "
                "ORDER_ID, DECISION_AT, AMOUNT_CENTS, ORDER_TOTAL_CENTS, CLAIM, ATTEMPTED, "
                "TOOL_STATUS, LEDGER_EFFECT_CENTS, AGENT_POLICY_ID, REPLY, "
                "MODEL_COST_CREDITS, AGENT_LATENCY_MS) "
                "SELECT ?,?,?,?,?,?,TO_TIMESTAMP_TZ(?),?,?,?,?,?,?,?,?,?,? "
                f"WHERE NOT EXISTS (SELECT 1 FROM {SCHEMA}.RUNS WHERE RUN_ID = ?)",
                (run["cohort_id"], run["trial"], run["run_id"], run["variant"],
                 run["case_id"], run["order_id"], run["decision_at"], run["amount_cents"],
                 run["order_total_cents"], run["claim"], run["attempted"],
                 run["tool_status"], run["ledger_effect_cents"], run["agent_policy_id"],
                 run["reply"], run["model_cost_credits"], run["agent_latency_ms"],
                 run["run_id"]))
            for event in canonical_events(run):
                cursor.execute(
                    f"INSERT INTO {SCHEMA}.TRACE_EVENTS "
                    "(EVENT_ID, RUN_ID, EVENT_AT, KIND, NODE, DETAIL, EVENT_ORDINAL) "
                    "SELECT ?,?,TO_TIMESTAMP_TZ(?),?,?,PARSE_JSON(?),? "
                    f"WHERE NOT EXISTS (SELECT 1 FROM {SCHEMA}.TRACE_EVENTS WHERE EVENT_ID = ?)",
                    (event["event_id"], event["run_id"], event["event_at"], event["kind"],
                     event["node"], json.dumps(event["detail"]), event["event_ordinal"],
                     event["event_id"]))
            # Few rows per run; executemany can't bind into TO_TIMESTAMP_TZ(?) here.
            for c in run["llm_calls"]:
                cursor.execute(
                    f"INSERT INTO {SCHEMA}.LLM_CALLS (RUN_ID, NODE, MODEL, INPUT_TOKENS, "
                    "OUTPUT_TOKENS, LATENCY_MS, EST_CREDITS, CALLED_AT) "
                    "SELECT ?,?,?,?,?,?,?,TO_TIMESTAMP_TZ(?)",
                    (run["run_id"], c["node"], c["model"], c["input_tokens"],
                     c["output_tokens"], c["latency_ms"], c["est_credits"],
                     run["observed_at"]))
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        finally:
            cursor.close()

    def close(self) -> None:
        pass


def run_rows(run: dict) -> dict[str, list[dict]]:
    """A finished run as rows for RUNS, TRACE_EVENTS and LLM_CALLS (column names upper)."""
    return {
        "RUNS": [{
            "COHORT_ID": run["cohort_id"], "TRIAL": run["trial"], "RUN_ID": run["run_id"],
            "VARIANT": run["variant"], "CASE_ID": run["case_id"], "ORDER_ID": run["order_id"],
            "DECISION_AT": run["decision_at"], "AMOUNT_CENTS": run["amount_cents"],
            "ORDER_TOTAL_CENTS": run["order_total_cents"], "CLAIM": run["claim"],
            "ATTEMPTED": run["attempted"], "TOOL_STATUS": run["tool_status"],
            "LEDGER_EFFECT_CENTS": run["ledger_effect_cents"],
            "AGENT_POLICY_ID": run["agent_policy_id"], "REPLY": run["reply"],
            "MODEL_COST_CREDITS": run["model_cost_credits"],
            "AGENT_LATENCY_MS": run["agent_latency_ms"]}],
        "TRACE_EVENTS": [{
            "EVENT_ID": e["event_id"], "RUN_ID": e["run_id"], "EVENT_AT": e["event_at"],
            "KIND": e["kind"], "NODE": e["node"], "DETAIL": e["detail"],
            "EVENT_ORDINAL": e["event_ordinal"]} for e in canonical_events(run)],
        "LLM_CALLS": [{
            "RUN_ID": run["run_id"], "NODE": c["node"], "MODEL": c["model"],
            "INPUT_TOKENS": c["input_tokens"], "OUTPUT_TOKENS": c["output_tokens"],
            "LATENCY_MS": c["latency_ms"], "EST_CREDITS": c["est_credits"],
            "CALLED_AT": run["observed_at"]} for c in run["llm_calls"]],
    }


class StreamingSink:
    """Streams each run's rows through Snowpipe Streaming (high-performance architecture).

    One client and channel per table on its default ``<TABLE>-STREAMING`` pipe. Every
    row gets an increasing offset token; ``close()`` waits until the last token commits.
    Credentials: a key-pair service identity (see sql/streaming_setup.sql); the private
    key is read from disk and written only to a temporary 0600 profile file.
    """

    name = "streaming"
    TABLES = ("RUNS", "TRACE_EVENTS", "LLM_CALLS")

    def __init__(self, cohort: str, private_key_path: str,
                 user: str = "REFUND_LAB_STREAMER", role: str = "REFUND_LAB_STREAM_ROLE",
                 account: str | None = None, client_factory=None):
        import os
        import stat
        import tempfile
        from pathlib import Path

        if not valid_cohort(cohort):
            raise ValueError("Invalid simulation ID")
        account = account or config.ACCOUNT
        if role.upper() in ADMIN_ROLES:
            raise ValueError("Streaming must use the lab's least-privilege service role, not an admin role")
        key_file = Path(private_key_path).expanduser()
        if key_file.stat().st_mode & 0o077:
            raise PermissionError("Streaming private key must not be group/world readable")
        if client_factory is None:
            from snowflake.ingest.streaming import StreamingIngestClient
            client_factory = StreamingIngestClient
        profile = {"account": account, "user": user, "role": role,
                   "url": f"https://{account.lower().replace('_', '-')}.snowflakecomputing.com:443",
                   "private_key": key_file.read_text()}
        fd, self._profile = tempfile.mkstemp(suffix=".json")
        os.chmod(self._profile, stat.S_IRUSR | stat.S_IWUSR)
        with os.fdopen(fd, "w") as handle:
            json.dump(profile, handle)
        self.clients, self.channels, self.offsets = {}, {}, {}
        try:
            for table in self.TABLES:
                client = client_factory(client_name=f"refund_lab_{table.lower()}",
                                        db_name="REFUND_AGENT_LAB", schema_name="DEMO",
                                        pipe_name=f"{table}-STREAMING", profile_json=self._profile)
                channel, _ = client.open_channel(channel_name=f"{cohort}_{table}")
                self.clients[table], self.channels[table], self.offsets[table] = client, channel, 0
        except Exception:
            self._remove_profile()
            raise

    def _remove_profile(self):
        import os
        if self._profile and os.path.exists(self._profile):
            os.remove(self._profile)
        self._profile = None

    def write(self, run: dict) -> None:
        for table, rows in run_rows(run).items():
            for row in rows:
                self.offsets[table] += 1
                self.channels[table].append_row(row, offset_token=str(self.offsets[table]))

    def close(self, timeout_seconds: int = 120) -> dict:
        """Wait for every channel to commit its last offset, then close. Returns rows per table."""
        try:
            for table, channel in self.channels.items():
                last = self.offsets[table]
                if last:
                    channel.wait_for_commit(
                        token_checker=lambda t, last=last: t is not None and int(t) >= last,
                        timeout_seconds=timeout_seconds)
                channel.close()
                self.clients[table].close()
        finally:
            self._remove_profile()
        return dict(self.offsets)


class RestStreamingSink:
    """Streams each run's rows through the Snowpipe Streaming REST API (high-performance).

    For a Streamlit app on container runtime. A container can't sign in with a customer
    key pair, so this uses the container's own OAuth session token
    (``/snowflake/session/token``) and streams as the app's owner role, over Snowflake's
    internal network: no secret, network rule or external access integration. One named
    channel per table on its default ``<TABLE>-STREAMING`` pipe; each run is one append
    per table; ``close()`` waits until the last offset commits. Every request has a
    timeout, so a failure raises instead of retrying forever.
    """

    name = "streaming"
    TABLES = StreamingSink.TABLES
    BASE = "/v2/streaming/databases/REFUND_AGENT_LAB/schemas/DEMO/pipes/{table}-STREAMING"

    def __init__(self, cohort: str, host: str | None = None, token_file: str = "/snowflake/session/token",
                 http=None, timeout_s: int = 30):
        import os

        if not valid_cohort(cohort):
            raise ValueError("Invalid simulation ID")
        if http is None:
            import requests as http
        self.http, self.timeout_s, self.token_file = http, timeout_s, token_file
        self.host = host or os.environ["SNOWFLAKE_HOST"]
        r = self._call("GET", f"https://{self.host}/v2/streaming/hostname")
        self.ingest = r.text.strip().replace("_", "-").lower()
        self.channels, self.tokens, self.offsets = {}, {}, {}
        for table in self.TABLES:
            channel = f"{cohort}_{table}"
            r = self._call("PUT", self._url(table, f"/channels/{channel}"), data="{}")
            self.channels[table], self.offsets[table] = channel, 0
            self.tokens[table] = r.json()["next_continuation_token"]

    def _headers(self) -> dict:
        # Snowflake refreshes the container's token file every few minutes.
        with open(self.token_file) as handle:
            token = handle.read().strip()
        return {"Authorization": f"Bearer {token}", "X-Snowflake-Authorization-Token-Type": "OAUTH",
                "Content-Type": "application/json"}

    def _url(self, table: str, path: str, data: bool = False) -> str:
        # Appends go to /v2/streaming/data/...; channel and status calls don't have /data.
        base = self.BASE.replace("/v2/streaming/", "/v2/streaming/data/") if data else self.BASE
        return f"https://{self.ingest}" + base.format(table=table) + path

    def _call(self, method: str, url: str, **kwargs):
        headers = {**self._headers(), **kwargs.pop("headers", {})}
        r = self.http.request(method, url, headers=headers, timeout=self.timeout_s, **kwargs)
        if r.status_code >= 400:
            raise RuntimeError(f"Snowpipe Streaming {method} {r.status_code}: {r.text[:300]}")
        return r

    def write(self, run: dict) -> None:
        for table, rows in run_rows(run).items():
            if not rows:
                continue
            start = self.offsets[table] + 1
            self.offsets[table] += len(rows)
            body = "".join(json.dumps(row, default=str) + "\n" for row in rows)
            r = self._call("POST", self._url(table, f"/channels/{self.channels[table]}/rows", data=True),
                           params={"continuationToken": self.tokens[table],
                                   "startOffsetToken": str(start),
                                   "endOffsetToken": str(self.offsets[table])},
                           data=body.encode(), headers={"Content-Type": "application/x-ndjson"})
            self.tokens[table] = r.json()["next_continuation_token"]

    def close(self, timeout_seconds: int = 120) -> dict:
        """Wait for every channel to commit its last offset, then drop it. Returns rows per table."""
        import time

        deadline = time.monotonic() + timeout_seconds
        pending = {t for t, last in self.offsets.items() if last}
        while pending:
            for table in sorted(pending):
                r = self._call("POST", self._url(table, ":bulk-channel-status"),
                               data=json.dumps({"channel_names": [self.channels[table]]}))
                status = r.json()["channel_statuses"].get(self.channels[table], {})
                committed = status.get("last_committed_offset_token")
                if status.get("channel_status_code") not in (None, "SUCCESS"):
                    raise RuntimeError(f"{table} channel failed: {status.get('last_error_message')}")
                if committed is not None and int(committed) >= self.offsets[table]:
                    pending.discard(table)
            if pending and time.monotonic() > deadline:
                raise TimeoutError(f"Snowpipe Streaming did not commit {sorted(pending)} in time")
            if pending:
                time.sleep(1)
        for table, channel in self.channels.items():
            self._call("DELETE", self._url(table, f"/channels/{channel}"),
                       data=json.dumps({"fail_on_uncommitted_rows": True}))
        return dict(self.offsets)


def simulate(conn, cohort: str, order_count: int, request_count: int,
             graphs: dict, sink: EventSink, agents: Iterable[str] = AGENT_NAMES,
             observers: Iterable = (),
             progress: Callable[[str, int, int], None] | None = None,
             scenarios: bool = False, evalset: bool = False) -> list[dict]:
    """Create orders, then send every refund request to every selected agent.

    With ``scenarios`` the fixed SCENARIOS set replaces random orders and requests; with
    ``evalset`` the larger judge-alignment eval set (``eval_set``) does
    (``order_count`` and ``request_count`` are ignored).

    Agents run one after another over the same requests. Each keeps its own simulated
    ledger, so a repeat request for an order that agent already refunded comes back as
    a duplicate. Observers may implement:
      ``batch(cohort, agent, graph, loop)``  run ``loop()`` inside a tracing run (TruLens)
      ``invoke(agent, request, run_id, do_run)``  route one run through the observer
      ``callbacks(agent, request, run_id)``  LangChain callbacks for one run (Langfuse)
      ``after_run(agent, run, request)``  post-run hook, e.g. a judge (Langfuse)
    Returns the run records.
    """
    from uuid import uuid4

    from refund_agent.agents import run_request

    agents = [a for a in agents if a in graphs]
    if not agents:
        raise ValueError("Choose at least one agent")
    observers = list(observers)
    if evalset:
        eval_orders, planned = eval_set(cohort)
        orders = create_orders(conn, cohort, len(eval_orders), eval_orders)
    elif scenarios:
        orders = create_orders(conn, cohort, len(SCENARIOS), scenario_orders(cohort))
        planned = scenario_requests(cohort)
    else:
        orders = create_orders(conn, cohort, order_count)
        planned = generate_requests(cohort, orders, request_count)
    facts = load_facts(conn, cohort)
    if len(facts) != len(orders):
        raise ValueError("The generated orders were not all saved")
    policy = live_policy(conn)
    requests = [(r, agent_request(cohort, r, facts[r["order_id"]], policy)) for r in planned]
    runs = []
    judged: list[tuple] = []

    for agent in agents:
        ledger: dict[str, int] = {}

        def loop(agent=agent, ledger=ledger):
            for request, base in requests:
                refunded = ledger.get(request["order_id"], 0)
                run_id = str(uuid4())
                callbacks = [cb for o in observers if hasattr(o, "callbacks")
                             for cb in o.callbacks(agent, base, run_id)]

                def do_run(base=base, request=request, refunded=refunded, run_id=run_id,
                           callbacks=callbacks):
                    def call():
                        return run_request(base, agent, graphs[agent], cohort=cohort,
                                           trial=request["trial"], refunded_cents=refunded,
                                           run_id=run_id, callbacks=callbacks)
                    guard = next((o.guard for o in observers if hasattr(o, "guard")), None)
                    return guard(call) if guard else call()

                router = next((o for o in observers if hasattr(o, "invoke")), None)
                run = router.invoke(agent, base, run_id, do_run) if router else do_run()
                ledger[request["order_id"]] = refunded + run["ledger_effect_cents"]
                sink.write(run)
                judged.append((agent, run, base))
                runs.append(run)
                if progress is not None:
                    progress(agent, request["trial"], len(requests))

        batcher = next((o for o in observers if hasattr(o, "batch")), None)
        if batcher:
            batcher.batch(cohort, agent, graphs[agent], loop)
        else:
            loop()
    # Post-run hooks (e.g. an LLM judge) run after every tracing batch has closed: an
    # untraced model call inside a TruLens live run becomes a span with no record ID.
    for agent, run, base in judged:
        for o in observers:
            if hasattr(o, "after_run"):
                o.after_run(agent, run, base)
    sink.close()
    return runs
