-- Idle safeguard: suspend Langfuse after 30 minutes with no ingress (UI or API).
-- Every 10 minutes a serverless task calls MANAGE_LANGFUSE('idle_check'), which suspends
-- the service only if it is RUNNING, was started/upgraded over 30 minutes ago, and its
-- network.ingress.* platform metrics show no connections in the last 30 minutes.
-- Run as ACCOUNTADMIN after deploy.sql. Resume Langfuse with MANAGE_LANGFUSE('resume', ...).

GRANT CREATE TASK ON SCHEMA REFUND_AGENT_LAB.LANGFUSE TO ROLE ACCOUNTADMIN;

CREATE TASK IF NOT EXISTS REFUND_AGENT_LAB.LANGFUSE.LANGFUSE_IDLE_SUSPEND
  SCHEDULE = '10 MINUTE'
  SERVERLESS_TASK_MAX_STATEMENT_SIZE = 'XSMALL'
  COMMENT = 'Suspends the Langfuse SPCS service after 30 idle minutes'
AS
  CALL REFUND_AGENT_LAB.LANGFUSE.MANAGE_LANGFUSE('idle_check', 'http://localhost:3000');

ALTER TASK REFUND_AGENT_LAB.LANGFUSE.LANGFUSE_IDLE_SUSPEND RESUME;

-- Turn the safeguard off:  ALTER TASK REFUND_AGENT_LAB.LANGFUSE.LANGFUSE_IDLE_SUSPEND SUSPEND;
