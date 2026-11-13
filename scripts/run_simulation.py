"""Live end-to-end simulation: orders, all agents, both observers, Snowpipe Streaming.

Usage: python scripts/run_simulation.py [orders] [requests] [--scenarios] [--sql]
                                        [--no-trulens] [--no-langfuse]
``--scenarios`` uses the fixed scenario set (lab.SCENARIOS) instead of random orders.
Run events are written with Snowpipe Streaming as REFUND_LAB_STREAMER (key at
~/.snowflake/keys/refund_lab_streamer.p8, see sql/streaming_setup.sql); ``--sql`` uses
ordinary inserts instead. The leaderboard (sql/leaderboard.sql) then runs on the
interactive warehouse REFUND_LAB_IWH, which is suspended afterwards.
Prints the simulation ID, observer status, the leaderboard and which warehouse ran it.
"""
import logging
import os
import sys
import time
import uuid
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")
logging.disable(logging.WARNING)
# The Snowpipe Streaming SDK logs every flush at INFO; keep the run output readable.
os.environ.setdefault("SS_LOG_LEVEL", "warn")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from refund_agent import config

import snowflake.connector
from langchain_snowflake import ChatSnowflake
from snowflake.snowpark import Session

from refund_agent.agents import build_agent
from refund_agent.lab import AGENT_NAMES, SCENARIOS, SqlSink, StreamingSink, simulate
from refund_agent.observers import TRULENS_METRICS, LangfuseObserver, TruLensObserver

HOST = config.LANGFUSE_HOST
args = [a for a in sys.argv[1:] if not a.startswith("--")]
orders = int(args[0]) if args else 6
requests = int(args[1]) if len(args) > 1 else 5
scenarios = "--scenarios" in sys.argv
if scenarios:
    orders, requests = len(SCENARIOS), len(SCENARIOS) + 1
cohort = "APP_" + uuid.uuid4().hex[:16].upper()

conn = snowflake.connector.connect(
    connection_name=config.CONNECTION, paramstyle="qmark",
    session_parameters={"PYTHON_CONNECTOR_QUERY_RESULT_FORMAT": "json"})
session = Session.builder.config("connection", conn).create()
chat = lambda m: ChatSnowflake(model=m, session=session, temperature=0)

observers = []
trulens = None if "--no-trulens" in sys.argv else TruLensObserver(session)
if trulens:
    observers.append(trulens)
# With TruLens on, agent model calls become TruLens cost spans.
agent_chat = TruLensObserver.chat_factory(chat) if trulens else chat
graphs = {a: build_agent(a, agent_chat) for a in AGENT_NAMES}
langfuse = None
if "--no-langfuse" not in sys.argv:
    env = dict(l.split("=", 1) for l in
               (Path.home() / ".langfuse_spcs.env").read_text().splitlines() if l)
    token = conn._rest._token_request("ISSUE")["data"]["sessionToken"]
    langfuse = LangfuseObserver(HOST, env["LF_INIT_PUBLIC_KEY"], env["LF_INIT_SECRET_KEY"],
                                token)  # native Langfuse judges
    observers.append(langfuse)

sink = (SqlSink(conn) if "--sql" in sys.argv
        else StreamingSink(cohort, str(Path.home() / ".snowflake/keys/refund_lab_streamer.p8")))
print("simulation", cohort, "scenario set:" if scenarios else "random:",
      f"{orders} orders, {requests} requests,", "sink:", sink.name,
      "observers:", [o.name for o in observers], flush=True)
started = time.perf_counter()
runs = simulate(conn, cohort, orders, requests, graphs, sink, observers=observers,
                scenarios=scenarios,
                progress=lambda a, i, n: print(f"  {a} request {i}/{n}", flush=True))
print(f"runs written: {len(runs)} in {time.perf_counter() - started:.0f}s", flush=True)
if sink.name == "streaming":
    print("streamed and committed rows:", sink.offsets, flush=True)

if langfuse:
    langfuse.flush()
    print("langfuse export:", langfuse.export(conn, runs), flush=True)
if trulens:
    trulens.flush()
    truth_metric = TruLensObserver.business_correct_metric(conn, cohort)
    for agent in AGENT_NAMES:
        _, app = trulens.apps[agent]
        run = app.get_run(trulens.run_name(cohort, agent))
        for _ in range(30):
            status = str(run.get_status())
            if "INVOCATION_COMPLETED" in status or "PARTIALLY" in status:
                break
            time.sleep(10)
        print(f"trulens {agent}: {status} ->",
              run.compute_metrics(list(TRULENS_METRICS) + [truth_metric]),
              flush=True)

cur = conn.cursor()
leaderboard = (Path(__file__).resolve().parents[1] / "sql" / "leaderboard.sql").read_text()
try:
    cur.execute("USE WAREHOUSE REFUND_LAB_IWH")
    started = time.perf_counter()
    cur.execute(leaderboard.rstrip().rstrip(";"), (cohort,))
    elapsed, qid = time.perf_counter() - started, cur.sfqid
    print("leaderboard", [d[0] for d in cur.description])
    for row in cur.fetchall():
        print(row)
    cur.execute(f"USE WAREHOUSE {config.WAREHOUSE}")
    cur.execute("SELECT WAREHOUSE_NAME FROM TABLE(INFORMATION_SCHEMA.QUERY_HISTORY_BY_SESSION()) "
                "WHERE QUERY_ID = ?", (qid,))
    ran_on = cur.fetchone()
    print(f"leaderboard ran on {ran_on[0] if ran_on else 'unknown'} in {elapsed:.1f}s")
finally:
    # An interactive warehouse can't auto-suspend in under 24 hours.
    cur.execute("ALTER WAREHOUSE REFUND_LAB_IWH SUSPEND")
print("cohort", cohort)
