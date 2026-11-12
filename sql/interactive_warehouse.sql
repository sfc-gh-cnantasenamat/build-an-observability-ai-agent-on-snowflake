-- Interactive warehouse for the lab's leaderboard and request queries.
-- Interactive warehouses query standard tables directly (zero-copy). Statements are
-- capped at 5 seconds; slower ones are retried on the fallback warehouse.
-- AUTO_SUSPEND can't be below 24 hours, so suspend it by hand after a session and
-- drop it in cleanup: DROP WAREHOUSE REFUND_LAB_IWH;
-- Run with: snow sql -f sql/interactive_warehouse.sql -D warehouse=<standard warehouse>
CREATE INTERACTIVE WAREHOUSE IF NOT EXISTS REFUND_LAB_IWH
  TABLES (REFUND_AGENT_LAB.DEMO.RUNS, REFUND_AGENT_LAB.DEMO.LLM_CALLS,
          REFUND_AGENT_LAB.DEMO.ORDERS, REFUND_AGENT_LAB.DEMO.PAYMENTS,
          REFUND_AGENT_LAB.DEMO.LANGFUSE_SCORES)
  WAREHOUSE_SIZE = 'XSMALL'
  COMMENT = 'Refund agent lab leaderboard';
ALTER WAREHOUSE REFUND_LAB_IWH SET FALLBACK_WAREHOUSE = <% warehouse %>;
