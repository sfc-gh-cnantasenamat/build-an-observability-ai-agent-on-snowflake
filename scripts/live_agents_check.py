"""Live check: the three agents against Cortex on six representative requests."""
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
graphs = {a: build_agent(a, lambda m: ChatSnowflake(model=m, session=session, temperature=0))
          for a in ("lean", "baseline", "fixed")}
live = {"policy_id": "P_2026_14D", "max_age_days": 14}
cases = {
    "eligible": dict(age_days=5, amount_cents=4000, order_total_cents=4000,
                     payment_status="settled", processor_status="ok"),
    "stale_20d": dict(age_days=20, amount_cents=4000, order_total_cents=4000,
                      payment_status="settled", processor_status="ok"),
    "declined": dict(age_days=5, amount_cents=4000, order_total_cents=4000,
                     payment_status="settled", processor_status="declined"),
    "partial": dict(age_days=5, amount_cents=1500, order_total_cents=6000,
                    payment_status="settled", processor_status="ok"),
    "pending": dict(age_days=5, amount_cents=3000, order_total_cents=3000,
                    payment_status="pending", processor_status="ok"),
    "old_45d": dict(age_days=45, amount_cents=2000, order_total_cents=2000,
                    payment_status="settled", processor_status="ok"),
}
for name, facts in cases.items():
    req = {"case_id": name, "order_id": f"T_{name}",
           "decision_at": "2026-11-08T12:00:00+00:00", "live_policy": live, **facts}
    print(name, flush=True)
    for agent, graph in graphs.items():
        try:
            r = run_request(req, agent, graph, cohort="LIVE_TEST", trial=1)
            print(f"    {agent:8} elig={r['eligible']!s:5} tool={r['tool_status']:10} "
                  f"eff={r['ledger_effect_cents']:5} claim={r['claim']:16} "
                  f"{r['agent_latency_ms']:6.0f}ms {r['model_cost_credits'] * 1e6:5.0f} microcredits"
                  f" | {r['reply'][:70]}", flush=True)
        except Exception as exc:
            print(f"    {agent:8} ERROR {str(exc)[:120]}", flush=True)
