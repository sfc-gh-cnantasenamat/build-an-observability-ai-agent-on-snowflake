-- How well each observer's judge agrees with business truth, one row per cohort, tool and
-- metric. Each judge is treated as a classifier, as in standard LLM-judge benchmarks: a
-- pass (score >= 0.5) predicts "this run was business-correct", and BUSINESS_CORRECT from
-- RUN_EVIDENCE (the oracle, never a judge) is the label.
--   Confusion counts: PASS_CORRECT (TP), PASS_WRONG (FP), FAIL_CORRECT (FN), FAIL_WRONG (TN).
--   PRECISION: of the runs the judge passed, the share that were correct. A judge that
--     passes wrong runs (low precision) is the dangerous kind.
--   RECALL: of the correct runs, the share the judge passed.
--   F1, overall AGREEMENT and Cohen's KAPPA (agreement beyond chance).
--   FALSE_PASS_RATE: of the wrong runs, the share the judge passed.
--   *_LO / *_HI: 95% intervals (Wilson for proportions; large-sample for kappa), so a
--     reader can see whether a difference between judges is bigger than the noise.
--   PEARSON, SPEARMAN (average ranks for ties), MAE and RMSE compare the raw score with
--     truth as 0/1; these match Langfuse Score Analytics.
-- The business_correct rows the lab copies into each tool are excluded here.
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
), rates AS (
  SELECT *,
         (TP + TN) / N AS PO,
         ((TP + FP) * (TP + FN) + (FN + TN) * (FP + TN)) / SQUARE(N) AS PE,
         TP / NULLIF(TP + FP, 0) AS PREC, TP + FP AS PREC_N,
         TP / NULLIF(TP + FN, 0) AS REC, TP + FN AS REC_N,
         FP / NULLIF(FP + TN, 0) AS FPR, FP + TN AS FPR_N
  FROM counts
)
SELECT COHORT_ID, TOOL, METRIC, JUDGE, N,
       TP AS PASS_CORRECT, FP AS PASS_WRONG, FN AS FAIL_CORRECT, TN AS FAIL_WRONG,
       ROUND(PREC, 3) AS PRECISION,
       ROUND((PREC + 1.9208 / PREC_N - 1.96 * SQRT(PREC * (1 - PREC) / PREC_N + 0.9604 / SQUARE(PREC_N)))
             / (1 + 3.8416 / PREC_N), 3) AS PRECISION_LO,
       ROUND((PREC + 1.9208 / PREC_N + 1.96 * SQRT(PREC * (1 - PREC) / PREC_N + 0.9604 / SQUARE(PREC_N)))
             / (1 + 3.8416 / PREC_N), 3) AS PRECISION_HI,
       ROUND(REC, 3) AS RECALL,
       ROUND((REC + 1.9208 / REC_N - 1.96 * SQRT(REC * (1 - REC) / REC_N + 0.9604 / SQUARE(REC_N)))
             / (1 + 3.8416 / REC_N), 3) AS RECALL_LO,
       ROUND((REC + 1.9208 / REC_N + 1.96 * SQRT(REC * (1 - REC) / REC_N + 0.9604 / SQUARE(REC_N)))
             / (1 + 3.8416 / REC_N), 3) AS RECALL_HI,
       ROUND(2 * TP / NULLIF(2 * TP + FP + FN, 0), 3) AS F1,
       ROUND(PO, 3) AS AGREEMENT,
       ROUND((PO - PE) / NULLIF(1 - PE, 0), 3) AS KAPPA,
       ROUND((PO - PE) / NULLIF(1 - PE, 0) - 1.96 * SQRT(PO * (1 - PO) / N) / NULLIF(1 - PE, 0), 3)
         AS KAPPA_LO,
       ROUND((PO - PE) / NULLIF(1 - PE, 0) + 1.96 * SQRT(PO * (1 - PO) / N) / NULLIF(1 - PE, 0), 3)
         AS KAPPA_HI,
       ROUND(FPR, 3) AS FALSE_PASS_RATE,
       ROUND(PEARSON, 3) AS PEARSON, ROUND(SPEARMAN, 3) AS SPEARMAN,
       ROUND(MAE, 3) AS MAE, ROUND(RMSE, 3) AS RMSE
FROM rates;
