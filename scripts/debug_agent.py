"""Debug: show the lean agent's decide/respond reasoning on one eligible request."""
import logging
import sys
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")
logging.disable(logging.CRITICAL)
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from refund_agent import config

from langchain_snowflake import ChatSnowflake
from snowflake.snowpark import Session

from refund_agent.agents import build_agent, run_request

session = Session.builder.config("connection_name", config.CONNECTION).create()
agent = sys.argv[1] if len(sys.argv) > 1 else "lean"
graph = build_agent(agent, lambda m: ChatSnowflake(model=m, session=session, temperature=0))
facts = {"age_days": 5, "amount_cents": 4000, "order_total_cents": 4000,
         "payment_status": "settled", "processor_status": sys.argv[2] if len(sys.argv) > 2 else "ok"}
for i in range(3):
    r = run_request({"case_id": "dbg", "order_id": "T_dbg", "decision_at": "2026-11-08T12:00:00+00:00",
                     "live_policy": {"policy_id": "P_2026_14D", "max_age_days": 14}, **facts},
                    agent, graph, cohort="DBG", trial=1)
    print(i, r["eligible"], r.get("reason", ""), r["llm_calls"][0]["output_tokens"], r["tool_status"], r["claim"], "|", r["reply"][:110], flush=True)
