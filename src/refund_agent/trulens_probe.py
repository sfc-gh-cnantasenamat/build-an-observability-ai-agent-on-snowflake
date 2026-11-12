"""Run the four-case TruGraph/SnowflakeConnector correlation probe."""

from __future__ import annotations

from datetime import datetime, timezone
from time import perf_counter
from uuid import uuid4

from refund_agent import config
from refund_agent.graph import CASES, build_graph
from refund_agent.producer import persist_run


def main() -> None:
    import snowflake.connector
    from snowflake.snowpark import Session
    from trulens.apps.langgraph import TruGraph
    from trulens.connectors.snowflake import SnowflakeConnector
    from trulens.core import TruSession

    session = Session.builder.config("connection_name", config.CONNECTION).create()
    writer = snowflake.connector.connect(connection_name=config.CONNECTION, autocommit=False)
    try:
        identity = session.sql(
            "SELECT CURRENT_ORGANIZATION_NAME(), CURRENT_ACCOUNT()"
        ).collect()[0]
        if tuple(identity) != config.expected_identity():
            raise RuntimeError("Wrong account; refusing TruLens initialization")
        session.use_database("REFUND_AGENT_LAB")
        session.use_schema("DEMO")
        connector = SnowflakeConnector(snowpark_session=session)
        tru_session = TruSession(connector=connector)
        cohort = f"TRU_{uuid4().hex[:12].upper()}"
        for variant in ("baseline", "fixed"):
            graph = build_graph(variant)
            recorder = TruGraph(
                graph, app_name="refund_truth_probe", app_version=variant,
                session=tru_session, start_evaluator=False,
            )
            for case in CASES:
                run_id = str(uuid4())
                started = perf_counter()
                with recorder:
                    result = graph.invoke({**case, "cohort_id": cohort,
                                           "run_id": run_id})
                elapsed_ms = (perf_counter() - started) * 1000
                run = {
                    "cohort_id": cohort, "trial": 1, "run_id": run_id,
                    "variant": variant, "case_id": case["case_id"],
                    "order_id": case["order_id"], "decision_at": case["decision_at"],
                    "amount_cents": case["amount_cents"], "expected": case["expected"],
                    "eligible": result["eligible"], "attempted": result["attempted"],
                    "tool_status": result["tool_status"],
                    "ledger_effect_cents": result["ledger_effect_cents"],
                    "claim": result["claim"], "evidence": result["evidence"],
                    "observed_at": datetime.now(timezone.utc).isoformat(),
                    "model_cost_usd": None, "agent_latency_ms": elapsed_ms,
                }
                persist_run(writer, run)
                print({"cohort": cohort, "run_id": run_id, "variant": variant,
                       "case_id": case["case_id"], "claim": result["claim"],
                       "tool_status": result["tool_status"]})
        if not tru_session.force_flush(timeout_millis=60000):
            raise RuntimeError("TruLens exporter did not flush")
        print("Exporter flushed; verify twenty trace spans and four record IDs in Snowflake")
    finally:
        writer.close()
        session.close()


if __name__ == "__main__":
    main()