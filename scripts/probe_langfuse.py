"""Probe: one agent run traced to Langfuse on SPCS via the LangChain callback, plus judge
scores, then read back by business run ID. TruLens runs in the same process to check the
two OpenTelemetry pipelines don't interfere."""
import base64
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

import requests
import snowflake.connector
from langchain_snowflake import ChatSnowflake
from snowflake.snowpark import Session

from refund_agent.agents import build_agent, run_request
from refund_agent.observers import JUDGE_MODEL, LangfuseObserver

HOST = config.LANGFUSE_HOST
ENV = dict(l.split("=", 1) for l in (Path.home() / ".langfuse_spcs.env").read_text().splitlines() if l)

conn = snowflake.connector.connect(connection_name=config.CONNECTION, paramstyle="qmark",
                                   session_parameters={"PYTHON_CONNECTOR_QUERY_RESULT_FORMAT": "json"})
token = conn._rest._token_request("ISSUE")["data"]["sessionToken"]
session = Session.builder.config("connection", conn).create()
chat = lambda m: ChatSnowflake(model=m, session=session, temperature=0)
lf = LangfuseObserver(HOST, ENV["LF_INIT_PUBLIC_KEY"], ENV["LF_INIT_SECRET_KEY"], token,
                      judge_llm=chat(JUDGE_MODEL))

agent = "baseline"
graph = build_agent(agent, chat)
req = {"case_id": "C1", "order_id": "LFPROBE_O1", "amount_cents": 4000, "order_total_cents": 4000,
       "age_days": 5, "payment_status": "settled", "processor_status": "declined",
       "decision_at": "2026-11-08T12:00:00+00:00",
       "live_policy": {"policy_id": "P_2026_14D", "max_age_days": 14}}
run_id = str(uuid.uuid4())
run = run_request(req, agent, graph, cohort="LFPROBE", trial=1, run_id=run_id,
                  callbacks=lf.callbacks(agent, req))
print("run", run_id, run["tool_status"], run["claim"], flush=True)
print("judge", lf.judge(run, req), flush=True)
lf.flush()

basic = base64.b64encode(f"{ENV['LF_INIT_PUBLIC_KEY']}:{ENV['LF_INIT_SECRET_KEY']}".encode()).decode()
headers = {"X-SF-SPCS-Authorization": f'Snowflake Token="{token}"', "Authorization": f"Basic {basic}"}
for attempt in range(12):
    time.sleep(5)
    r = requests.get(f"{HOST}/api/public/v2/observations", headers=headers, timeout=30,
                     params={"limit": 50, "fromStartTime": "2026-11-08T00:00:00Z"})
    obs = [o for o in r.json().get("data", []) if run_id in str(o.get("metadata", "")) or run_id in str(o)]
    if obs:
        print("observations for run:", len(obs), sorted({o.get("type") for o in obs}),
              sorted({o.get("name") for o in obs})[:8], flush=True)
        print("trace id:", obs[0]["traceId"], flush=True)
        print("sample keys:", sorted(obs[0].keys()), flush=True)
        break
else:
    print("not found; status", r.status_code, r.text[:300])
