-- Identity for Langfuse's native LLM-as-a-judge evaluators to call Cortex.
-- Scope: the role holds only SNOWFLAKE.CORTEX_REST_API_USER (no data access). PATs need
-- a network policy; SPCS egress IPs aren't fixed, so this user's policy allows any IP.
-- The PAT is created by scripts/langfuse_judge_setup.py (7-day expiry, never printed).
-- Cleanup: DROP USER REFUND_LAB_JUDGE; DROP ROLE REFUND_LAB_JUDGE_ROLE;
--          DROP NETWORK POLICY REFUND_LAB_JUDGE_POLICY;
--          DROP NETWORK RULE REFUND_AGENT_LAB.LANGFUSE.REFUND_LAB_JUDGE_ANY_IP;
CREATE ROLE IF NOT EXISTS REFUND_LAB_JUDGE_ROLE COMMENT = 'Refund lab: Cortex REST API only';
GRANT DATABASE ROLE SNOWFLAKE.CORTEX_REST_API_USER TO ROLE REFUND_LAB_JUDGE_ROLE;

CREATE NETWORK RULE IF NOT EXISTS REFUND_AGENT_LAB.LANGFUSE.REFUND_LAB_JUDGE_ANY_IP
  TYPE = IPV4 MODE = INGRESS VALUE_LIST = ('0.0.0.0/0')
  COMMENT = 'Langfuse on SPCS has no fixed egress IP';
CREATE NETWORK POLICY IF NOT EXISTS REFUND_LAB_JUDGE_POLICY
  ALLOWED_NETWORK_RULE_LIST = ('REFUND_AGENT_LAB.LANGFUSE.REFUND_LAB_JUDGE_ANY_IP')
  COMMENT = 'Refund lab judge PAT user only';

CREATE USER IF NOT EXISTS REFUND_LAB_JUDGE
  TYPE = SERVICE DEFAULT_ROLE = REFUND_LAB_JUDGE_ROLE
  NETWORK_POLICY = REFUND_LAB_JUDGE_POLICY
  COMMENT = 'Langfuse native evaluators -> Cortex REST API (refund lab)';
GRANT ROLE REFUND_LAB_JUDGE_ROLE TO USER REFUND_LAB_JUDGE;

-- Egress from the Langfuse service to the Cortex REST API on this account.
-- Run with: snow sql -f langfuse_spcs/judge_access.sql -D account_host=<org-account>.snowflakecomputing.com
-- (use hyphens, not underscores, in the host: the TLS certificate covers the hyphenated form).
CREATE NETWORK RULE IF NOT EXISTS REFUND_AGENT_LAB.LANGFUSE.CORTEX_REST_EGRESS
  TYPE = HOST_PORT MODE = EGRESS
  VALUE_LIST = ('<% account_host %>:443');
CREATE EXTERNAL ACCESS INTEGRATION IF NOT EXISTS LANGFUSE_CORTEX_EAI
  ALLOWED_NETWORK_RULES = (REFUND_AGENT_LAB.LANGFUSE.CORTEX_REST_EGRESS)
  ENABLED = TRUE COMMENT = 'Langfuse on SPCS -> Cortex REST API (native judges)';
GRANT USAGE ON INTEGRATION LANGFUSE_CORTEX_EAI TO ROLE LANGFUSE_ROLE;
