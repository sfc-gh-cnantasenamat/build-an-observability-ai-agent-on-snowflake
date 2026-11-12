"""Live end-to-end simulation: orders, all agents, both observers, SqlSink.

Usage: python scripts/run_simulation.py [orders] [requests] [--scenarios] [--no-trulens] [--no-langfuse]
``--scenarios`` uses the fixed scenario set (lab.SCENARIOS) instead of random orders.
Prints the simulation ID, observer status and a per-agent summary from RUN_EVIDENCE.
"""
import logging
import sys
import time
import uuid
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")
logging.disable(logging.WARNING)
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

sink = (StreamingSink(cohort, str(Path.home() / ".snowflake/keys/refund_lab_streamer.p8"))
        if "--stream" in sys.argv else SqlSink(conn))
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
cur.execute("""
SELECT VARIANT, COUNT(*) runs, COUNT_IF(BUSINESS_CORRECT) correct,
       COUNT_IF(FALSE_SUCCESS) false_success, COUNT_IF(CUSTOMER_MISINFORMED) misinformed,
       SUM(MONEY_AT_RISK_CENTS) risk_cents, ROUND(SUM(MODEL_COST_CREDITS) * 1e6) microcredits,
       ROUND(MEDIAN(AGENT_LATENCY_MS)) p50_ms, LISTAGG(DISTINCT ROOT_CAUSE, ',') causes
FROM REFUND_AGENT_LAB.DEMO.RUN_EVIDENCE WHERE COHORT_ID = ? GROUP BY 1 ORDER BY 1""", (cohort,))
print([d[0] for d in cur.description])
for row in cur.fetchall():
    print(row)
print("cohort", cohort)
