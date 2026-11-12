-- How well each observer's judge scores agree with business truth, one row per cohort,
-- tool and metric. Uses the same statistics as Langfuse Score Analytics so both tools
-- are compared on identical definitions:
--   numeric judge score vs BUSINESS_CORRECT as 0/1: Pearson, Spearman (average ranks for
--   ties), MAE and RMSE;
--   judge pass (score >= 0.5) vs BUSINESS_CORRECT: 2x2 confusion counts, overall
--   agreement, Cohen's kappa and F1 with "correct" as the positive class.
-- BUSINESS_CORRECT comes from RUN_EVIDENCE (the oracle), never from a judge. The
-- business_correct rows the lab copies into each tool are excluded here.
CREATE OR REPLACE VIEW REFUND_AGENT_LAB.DEMO.JUDGE_AGREEMENT AS
WITH judged AS (
  SELECT 'TruLens' AS TOOL, RUN_ID, METRIC, JUDGE, SCORE
  FROM REFUND_AGENT_LAB.DEMO.TRULENS_SCORES WHERE METRIC <> 'business_correct'
  UNION ALL
  SELECT 'Langfuse', RUN_ID, METRIC, JUDGE, SCORE
  FROM REFUND_AGENT_LAB.DEMO.LANGFUSE_SCORES WHERE METRIC <> 'business_correct'
), paired AS (
  SELECT e.COHORT_ID, j.TOOL, j.METRIC, j.JUDGE, j.SCORE,
         IFF(e.BUSINESS_CORRECT, 1, 0) AS TRUTH,
         j.SCORE >= 0.5 AS PASS, e.BUSINESS_CORRECT AS CORRECT
  FROM judged j JOIN REFUND_AGENT_LAB.DEMO.RUN_EVIDENCE e ON e.RUN_ID = j.RUN_ID
  QUALIFY ROW_NUMBER() OVER (PARTITION BY j.TOOL, j.METRIC, j.RUN_ID ORDER BY j.SCORE) = 1
), ranked AS (
  SELECT *,
         RANK() OVER (PARTITION BY COHORT_ID, TOOL, METRIC ORDER BY SCORE)
           + (COUNT(*) OVER (PARTITION BY COHORT_ID, TOOL, METRIC, SCORE) - 1) / 2 AS SCORE_RANK,
         RANK() OVER (PARTITION BY COHORT_ID, TOOL, METRIC ORDER BY TRUTH)
           + (COUNT(*) OVER (PARTITION BY COHORT_ID, TOOL, METRIC, TRUTH) - 1) / 2 AS TRUTH_RANK
  FROM paired
), counts AS (
  SELECT COHORT_ID, TOOL, METRIC, ANY_VALUE(JUDGE) AS JUDGE, COUNT(*) AS N,
         CORR(SCORE, TRUTH) AS PEARSON, CORR(SCORE_RANK, TRUTH_RANK) AS SPEARMAN,
         AVG(ABS(SCORE - TRUTH)) AS MAE, SQRT(AVG(SQUARE(SCORE - TRUTH))) AS RMSE,
         COUNT_IF(PASS AND CORRECT) AS TP, COUNT_IF(PASS AND NOT CORRECT) AS FP,
         COUNT_IF(NOT PASS AND CORRECT) AS FN, COUNT_IF(NOT PASS AND NOT CORRECT) AS TN
  FROM ranked GROUP BY COHORT_ID, TOOL, METRIC
)
SELECT COHORT_ID, TOOL, METRIC, JUDGE, N,
       ROUND(PEARSON, 3) AS PEARSON, ROUND(SPEARMAN, 3) AS SPEARMAN,
       ROUND(MAE, 3) AS MAE, ROUND(RMSE, 3) AS RMSE,
       TP AS PASS_CORRECT, FP AS PASS_WRONG, FN AS FAIL_CORRECT, TN AS FAIL_WRONG,
       ROUND((TP + TN) / N, 3) AS AGREEMENT,
       ROUND(((TP + TN) / N - ((TP + FP) * (TP + FN) + (FN + TN) * (FP + TN)) / SQUARE(N))
             / NULLIF(1 - ((TP + FP) * (TP + FN) + (FN + TN) * (FP + TN)) / SQUARE(N), 0), 3)
         AS KAPPA,
       ROUND(2 * TP / NULLIF(2 * TP + FP + FN, 0), 3) AS F1
FROM counts;
