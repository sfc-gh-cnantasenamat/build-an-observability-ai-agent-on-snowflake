"""Execute graph runs and persist their canonical evidence to DEVREL.

This is connector-based ingestion, not Snowpipe Streaming or a vendor trace.
"""

from __future__ import annotations

import json
from uuid import uuid4

from refund_agent import config
from refund_agent.events import canonical_events
from refund_agent.graph import CASES, run_case


SCHEMA = "REFUND_AGENT_LAB.DEMO"


def verify_connection(cursor) -> None:
    cursor.execute("SELECT CURRENT_ORGANIZATION_NAME(), CURRENT_ACCOUNT()")
    if tuple(cursor.fetchone()) != config.expected_identity():
        raise RuntimeError(f"Refusing to write outside {config.ACCOUNT} (REFUND_LAB_ACCOUNT)")


def persist_run(connection, result: dict) -> int:
    """Idempotently write one run and its three allowlisted events."""
    events = canonical_events(result)
    cursor = connection.cursor()
    try:
        verify_connection(cursor)
        cursor.execute(
            f"INSERT INTO {SCHEMA}.RUNS "
            "(COHORT_ID, TRIAL, RUN_ID, VARIANT, CASE_ID, ORDER_ID, "
            "DECISION_AT, AMOUNT_CENTS, CLAIM, ATTEMPTED, TOOL_STATUS, "
            "LEDGER_EFFECT_CENTS, MODEL_COST_USD, AGENT_LATENCY_MS) "
            "SELECT %s,%s,%s,%s,%s,%s,TO_TIMESTAMP_TZ(%s),%s,%s,%s,%s,%s,%s,%s "
            f"WHERE NOT EXISTS (SELECT 1 FROM {SCHEMA}.RUNS WHERE RUN_ID=%s)",
            (result["cohort_id"], result["trial"], result["run_id"],
             result["variant"], result["case_id"], result["order_id"],
             result["decision_at"], result["amount_cents"], result["claim"],
             result["attempted"], result["tool_status"],
             result["ledger_effect_cents"], result["model_cost_usd"],
             result["agent_latency_ms"], result["run_id"]),
        )
        inserted = cursor.rowcount
        for event in events:
            cursor.execute(
                f"INSERT INTO {SCHEMA}.TRACE_EVENTS "
                "(EVENT_ID,RUN_ID,EVENT_AT,KIND,NODE,DETAIL,EVENT_ORDINAL) "
                "SELECT %s,%s,TO_TIMESTAMP_TZ(%s),%s,%s,PARSE_JSON(%s),%s "
                f"WHERE NOT EXISTS (SELECT 1 FROM {SCHEMA}.TRACE_EVENTS WHERE EVENT_ID=%s)",
                (event["event_id"], event["run_id"], event["event_at"],
                 event["kind"], event["node"], json.dumps(event["detail"]),
                 event["event_ordinal"], event["event_id"]),
            )
        connection.commit()
        return inserted
    except Exception:
        connection.rollback()
        raise
    finally:
        cursor.close()


def run_cohort(connection, cohort: str | None = None) -> list[dict]:
    """Run the four deterministic cases and persist only successful executions."""
    cohort_id = cohort or f"GRAPH_{uuid4().hex[:12].upper()}"
    completed = []
    for variant in ("baseline", "fixed"):
        for case in CASES:
            result = run_case(case, variant, cohort=cohort_id)
            persist_run(connection, result)
            completed.append(result)
    return completed


def main() -> None:
    import snowflake.connector

    connection = snowflake.connector.connect(connection_name=config.CONNECTION, autocommit=False)
    try:
        runs = run_cohort(connection)
        print(json.dumps({"cohort": runs[0]["cohort_id"], "run_ids":
                          [item["run_id"] for item in runs]}, indent=2))
    finally:
        connection.close()


if __name__ == "__main__":
    main()