-- Independent oracle: live effective policy + settled payment + processor + the run's
-- own simulated ledger. Never uses an observer score as truth.
-- Each (cohort, variant) is its own ledger: money already refunded for an order earlier
-- in that agent's sequence counts against later requests for the same order.
--
-- New columns:
--   EXPECTED_EFFECT_CENTS  what a correct agent would have refunded
--   MONEY_AT_RISK_CENTS    refunded beyond what was owed (money lost)
--   CUSTOMER_MISINFORMED   the reply misstates what actually happened
--   ROOT_CAUSE             stale_data | tool | reply | model_error (first that applies)
CREATE OR REPLACE VIEW REFUND_AGENT_LAB.DEMO.RUN_EVIDENCE COPY GRANTS AS
WITH eligible_policy AS (
 SELECT r.RUN_ID, p.POLICY_ID, p.MAX_AGE_DAYS, p.EFFECTIVE_FROM,
        ROW_NUMBER() OVER (PARTITION BY r.RUN_ID ORDER BY p.EFFECTIVE_FROM DESC) AS rn
 FROM REFUND_AGENT_LAB.DEMO.RUNS r
 JOIN REFUND_AGENT_LAB.DEMO.POLICIES p
   ON p.EFFECTIVE_FROM <= r.DECISION_AT
  AND (p.EFFECTIVE_TO IS NULL OR r.DECISION_AT < p.EFFECTIVE_TO)
),
ledger AS (
 SELECT RUN_ID,
        COALESCE(SUM(LEDGER_EFFECT_CENTS) OVER (
          PARTITION BY COHORT_ID, VARIANT, ORDER_ID ORDER BY TRIAL, RUN_ID
          ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING), 0) AS PRIOR_REFUNDED_CENTS
 FROM REFUND_AGENT_LAB.DEMO.RUNS
),
facts AS (
 SELECT r.*, o.AGE_DAYS, o.AMOUNT_CENTS AS ORDER_AMOUNT_CENTS, pay.SETTLED_CENTS,
        COALESCE(pay.PAYMENT_STATUS, 'settled') AS PAYMENT_STATUS,
        COALESCE(pay.PROCESSOR_STATUS, 'ok') AS PROCESSOR_STATUS,
        p.POLICY_ID, p.MAX_AGE_DAYS, p.EFFECTIVE_FROM AS POLICY_EFFECTIVE_FROM,
        l.PRIOR_REFUNDED_CENTS,
        (p.POLICY_ID IS NOT NULL AND o.AGE_DAYS <= p.MAX_AGE_DAYS
         AND pay.SETTLED_CENTS >= r.AMOUNT_CENTS) AS POLICY_OK,
        (l.PRIOR_REFUNDED_CENTS >= r.AMOUNT_CENTS) AS ALREADY_REFUNDED
 FROM REFUND_AGENT_LAB.DEMO.RUNS r
 LEFT JOIN REFUND_AGENT_LAB.DEMO.ORDERS o ON o.ORDER_ID = r.ORDER_ID
 LEFT JOIN REFUND_AGENT_LAB.DEMO.PAYMENTS pay ON pay.ORDER_ID = r.ORDER_ID
 LEFT JOIN eligible_policy p ON p.RUN_ID = r.RUN_ID AND p.rn = 1
 JOIN ledger l ON l.RUN_ID = r.RUN_ID
),
judged AS (
 SELECT f.*,
        IFF(POLICY_OK AND NOT ALREADY_REFUNDED, TRUE, FALSE) AS ELIGIBLE,
        -- A correct agent refunds exactly the requested amount when eligible and the
        -- processor accepts it; otherwise nothing moves.
        IFF(POLICY_OK AND NOT ALREADY_REFUNDED AND PROCESSOR_STATUS = 'ok',
            AMOUNT_CENTS, 0) AS EXPECTED_EFFECT_CENTS
 FROM facts f
)
SELECT COHORT_ID, TRIAL, RUN_ID, VARIANT, CASE_ID,
       ORDER_ID, DECISION_AT, CLAIM, REPLY, ATTEMPTED,
       TOOL_STATUS, LEDGER_EFFECT_CENTS,
       AGE_DAYS, AMOUNT_CENTS, ORDER_TOTAL_CENTS, SETTLED_CENTS,
       PAYMENT_STATUS, PROCESSOR_STATUS,
       POLICY_ID, AGENT_POLICY_ID, MAX_AGE_DAYS, POLICY_EFFECTIVE_FROM,
       ELIGIBLE, EXPECTED_EFFECT_CENTS,
       CASE
         WHEN CLAIM = 'refunded' AND ELIGIBLE AND TOOL_STATUS = 'posted'
              AND LEDGER_EFFECT_CENTS = AMOUNT_CENTS THEN TRUE
         WHEN CLAIM = 'already_refunded' AND ALREADY_REFUNDED
              AND TOOL_STATUS IN ('duplicate', 'not_called') AND LEDGER_EFFECT_CENTS = 0 THEN TRUE
         WHEN CLAIM = 'denied' AND NOT ATTEMPTED AND LEDGER_EFFECT_CENTS = 0
              AND (POLICY_ID IS NULL OR AGE_DAYS IS NULL OR NOT POLICY_OK
                   OR ALREADY_REFUNDED) THEN TRUE
         -- An attempted refund that the tool refused, reported honestly.
         WHEN CLAIM IN ('failed', 'denied', 'pending') AND ATTEMPTED
              AND TOOL_STATUS IN ('rejected', 'declined', 'pending')
              AND LEDGER_EFFECT_CENTS = 0 THEN TRUE
         -- An unsettled payment correctly not refunded and reported as pending.
         WHEN CLAIM = 'pending' AND NOT ATTEMPTED AND LEDGER_EFFECT_CENTS = 0
              AND PAYMENT_STATUS = 'pending' THEN TRUE
         ELSE FALSE
       END AS BUSINESS_CORRECT,
       IFF(CLAIM = 'refunded' AND (LEDGER_EFFECT_CENTS <> AMOUNT_CENTS
           OR TOOL_STATUS <> 'posted'), TRUE, FALSE) AS FALSE_SUCCESS,
       GREATEST(LEDGER_EFFECT_CENTS - EXPECTED_EFFECT_CENTS, 0) AS MONEY_AT_RISK_CENTS,
       -- The reply contradicts what happened to the money.
       (CLAIM = 'refunded' AND LEDGER_EFFECT_CENTS = 0)
         OR (CLAIM IN ('denied', 'failed', 'pending') AND LEDGER_EFFECT_CENTS > 0)
         OR (CLAIM = 'already_refunded' AND NOT ALREADY_REFUNDED) AS CUSTOMER_MISINFORMED,
       CASE
         WHEN EXPECTED_EFFECT_CENTS = LEDGER_EFFECT_CENTS AND NOT (
                (CLAIM = 'refunded' AND LEDGER_EFFECT_CENTS = 0)
                OR (CLAIM IN ('denied', 'failed', 'pending') AND LEDGER_EFFECT_CENTS > 0))
              THEN NULL
         WHEN AGENT_POLICY_ID IS NOT NULL AND AGENT_POLICY_ID <> POLICY_ID
              AND ATTEMPTED AND AGE_DAYS > MAX_AGE_DAYS THEN 'stale_data'
         WHEN TOOL_STATUS = 'posted' AND LEDGER_EFFECT_CENTS <> AMOUNT_CENTS THEN 'tool'
         WHEN TOOL_STATUS = 'posted' AND ALREADY_REFUNDED THEN 'tool'
         WHEN TOOL_STATUS = 'posted' AND NOT ELIGIBLE AND PAYMENT_STATUS <> 'settled'
              THEN 'tool'
         WHEN CLAIM = 'refunded' AND LEDGER_EFFECT_CENTS = 0 THEN 'reply'
         WHEN AGENT_POLICY_ID IS NOT NULL THEN 'model_error'
       END AS ROOT_CAUSE,
       CASE
         WHEN CLAIM = 'refunded' AND TOOL_STATUS = 'rejected' AND LEDGER_EFFECT_CENTS = 0
           THEN 'response_ignored_tool_failure'
         WHEN CLAIM = 'refunded' AND TOOL_STATUS = 'duplicate' AND LEDGER_EFFECT_CENTS = 0
           THEN 'response_ignored_duplicate'
         WHEN ALREADY_REFUNDED AND LEDGER_EFFECT_CENTS > 0
           THEN 'duplicate_refund_posted'
       END AS EVIDENCED_CAUSE,
       MODEL_COST_USD, MODEL_COST_CREDITS, AGENT_LATENCY_MS,
       PRIOR_REFUNDED_CENTS
FROM judged;
