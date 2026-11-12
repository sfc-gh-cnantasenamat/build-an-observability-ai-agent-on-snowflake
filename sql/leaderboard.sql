-- Leaderboard for one simulation (bind: COHORT_ID). The signals a team would normally
-- pick an agent on come first; business truth (from RUN_EVIDENCE) is revealed after.
-- Langfuse scores come from its native evaluators (answer_relevance, groundedness).
-- TruLens scores come from REFUND_AGENT_LAB.DEMO.TRULENS_SCORES; that view reads the
-- AI Observability event table, so it is joined separately on a standard warehouse.
SELECT e.VARIANT AS AGENT,
       COUNT(*) AS REQUESTS,
       -- The signals before the reveal
       ROUND(AVG(e.MODEL_COST_CREDITS) * 1e6, 0) AS MICROCREDITS_PER_REQUEST,
       ROUND(MEDIAN(e.AGENT_LATENCY_MS), 0) AS P50_LATENCY_MS,
       ROUND(100 * AVG(IFF(e.CLAIM IN ('refunded', 'already_refunded'), 1, 0)), 0)
         AS SELF_REPORTED_SUCCESS_PCT,
       ROUND(AVG(lf.RELEVANCE), 2) AS LANGFUSE_RELEVANCE,
       ROUND(AVG(lf.GROUNDEDNESS), 2) AS LANGFUSE_GROUNDEDNESS,
       -- Business truth
       ROUND(100 * AVG(IFF(e.BUSINESS_CORRECT, 1, 0)), 0) AS BUSINESS_CORRECT_PCT,
       COUNT_IF(e.FALSE_SUCCESS) AS FALSE_SUCCESSES,
       COUNT_IF(e.CUSTOMER_MISINFORMED) AS CUSTOMERS_MISINFORMED,
       SUM(e.MONEY_AT_RISK_CENTS) / 100 AS MONEY_AT_RISK_DOLLARS,
       LISTAGG(DISTINCT e.ROOT_CAUSE, ', ') WITHIN GROUP (ORDER BY e.ROOT_CAUSE) AS ROOT_CAUSES
FROM REFUND_AGENT_LAB.DEMO.RUN_EVIDENCE e
LEFT JOIN (
  SELECT RUN_ID, MAX(IFF(METRIC = 'answer_relevance', SCORE, NULL)) AS RELEVANCE,
         MAX(IFF(METRIC = 'groundedness', SCORE, NULL)) AS GROUNDEDNESS
  FROM REFUND_AGENT_LAB.DEMO.LANGFUSE_SCORES GROUP BY RUN_ID
) lf ON lf.RUN_ID = e.RUN_ID
WHERE e.COHORT_ID = ?
GROUP BY e.VARIANT
ORDER BY MICROCREDITS_PER_REQUEST;
