-- TruLens judge scores (Snowflake AI Observability) keyed by the lab's business run ID.
-- Live runs are named <simulation>_<agent>; the instrumented root span carries
-- refund_lab.run_id, and each eval_root record points back to it via target_record_id.
CREATE OR REPLACE VIEW REFUND_AGENT_LAB.DEMO.TRULENS_SCORES AS
WITH ev AS (
  SELECT RECORD_ATTRIBUTES AS a
  FROM TABLE(SNOWFLAKE.LOCAL.GET_AI_OBSERVABILITY_EVENTS(
    'REFUND_AGENT_LAB', 'DEMO', 'REFUND_AGENT_LAB', 'EXTERNAL AGENT'))
), roots AS (
  SELECT a:"ai.observability.record_id"::VARCHAR AS RECORD_ID,
         a:"refund_lab.run_id"::VARCHAR AS RUN_ID,
         a:"snow.ai.observability.run.name"::VARCHAR AS TRULENS_RUN
  FROM ev WHERE a:"refund_lab.run_id" IS NOT NULL
), scores AS (
  SELECT a:"ai.observability.eval.target_record_id"::VARCHAR AS RECORD_ID,
         a:"ai.observability.eval.metric_name"::VARCHAR AS METRIC,
         a:"ai.observability.eval.llm_judge_name"::VARCHAR AS JUDGE,
         a:"ai.observability.eval_root.score"::FLOAT AS SCORE
  FROM ev WHERE a:"ai.observability.eval_root.score" IS NOT NULL
)
SELECT r.RUN_ID, r.TRULENS_RUN, r.RECORD_ID, s.METRIC, s.JUDGE, s.SCORE
FROM roots r JOIN scores s ON s.RECORD_ID = r.RECORD_ID;

-- Langfuse export (self-hosted on SPCS). One row per Langfuse observation read back via
-- /api/public/v2/observations, keyed by the business run ID (trace ID is derived from it).
CREATE TABLE IF NOT EXISTS REFUND_AGENT_LAB.DEMO.LANGFUSE_TRACES (
  RUN_ID VARCHAR, TRACE_ID VARCHAR, OBSERVATION_ID VARCHAR, PARENT_OBSERVATION_ID VARCHAR,
  TYPE VARCHAR, NAME VARCHAR, MODEL VARCHAR, INPUT_TOKENS NUMBER, OUTPUT_TOKENS NUMBER,
  START_TIME TIMESTAMP_TZ, END_TIME TIMESTAMP_TZ, EXPORTED_AT TIMESTAMP_TZ
);
-- Langfuse judge scores. Langfuse v4 (events-only) accepts scores but has no read API,
-- so these rows are the same values posted to Langfuse, recorded at write time.
CREATE TABLE IF NOT EXISTS REFUND_AGENT_LAB.DEMO.LANGFUSE_SCORES (
  RUN_ID VARCHAR, TRACE_ID VARCHAR, METRIC VARCHAR, JUDGE VARCHAR, SCORE FLOAT,
  SCORED_AT TIMESTAMP_TZ
);
-- Langfuse's own cost per observation, from the custom model prices registered by
-- scripts/langfuse_models.py (AI credits per token, not USD).
ALTER TABLE REFUND_AGENT_LAB.DEMO.LANGFUSE_TRACES ADD COLUMN IF NOT EXISTS COST_CREDITS FLOAT;
