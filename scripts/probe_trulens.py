"""Probe: TruLens live run for one agent on two requests, then server-side metrics."""
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

from refund_agent.agents import build_agent, run_request
from refund_agent.observers import TruLensObserver

conn = snowflake.connector.connect(connection_name=config.CONNECTION, paramstyle="qmark")
session = Session.builder.config("connection", conn).create()
cohort = "PROBE_" + uuid.uuid4().hex[:8].upper()
agent = "baseline"
graph = build_agent(agent, lambda m: ChatSnowflake(model=m, session=session, temperature=0))
observer = TruLensObserver(session)
live = {"policy_id": "P_2026_14D", "max_age_days": 14}
requests = [
    {"case_id": "C1", "order_id": f"{cohort}_O1", "amount_cents": 4000, "order_total_cents": 4000,
     "age_days": 5, "payment_status": "settled", "processor_status": "declined",
     "decision_at": "2026-11-08T12:00:00+00:00", "live_policy": live},
    {"case_id": "C2", "order_id": f"{cohort}_O2", "amount_cents": 2000, "order_total_cents": 2000,
     "age_days": 40, "payment_status": "settled", "processor_status": "ok",
     "decision_at": "2026-11-08T12:00:00+00:00", "live_policy": live},
]
for i, req in enumerate(requests, 1):
    call = observer.wrap(cohort, agent, graph, req,
                         lambda run_id, req=req, i=i: run_request(req, agent, graph, cohort, i, run_id=run_id))
    run = call(str(uuid.uuid4()))
    print("run", run["run_id"], run["tool_status"], run["claim"], flush=True)
observer.flush()
_, tru_app = observer.apps[agent]
run_obj = tru_app.get_run(observer.run_name(cohort, agent))
for _ in range(30):
    status = run_obj.get_status()
    print("status", status, flush=True)
    if "INVOCATION_COMPLETED" in str(status) or "PARTIALLY" in str(status):
        break
    time.sleep(10)
print("compute:", observer.compute(cohort, [agent]), flush=True)
print("cohort", cohort, flush=True)
