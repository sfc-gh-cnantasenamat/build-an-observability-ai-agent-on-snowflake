-- Let the hosted Streamlit app send traces to Langfuse on SPCS.
-- Langfuse API keys are attached as Streamlit secrets through an EAI; the network rule
-- allows the public ingress host only: SPCS internal DNS names (*.svc.spcs.internal)
-- are rejected in network rules because Snowflake cannot resolve them.
-- Run with: snow sql -f sql/app_access.sql -D langfuse_host=<ingress host without https://>
CREATE NETWORK RULE IF NOT EXISTS REFUND_AGENT_LAB.DEMO.LANGFUSE_EGRESS_RULE
  TYPE = HOST_PORT MODE = EGRESS
  VALUE_LIST = ('<% langfuse_host %>:443');

-- The app reads the Langfuse ingress URL from this secret (no account-specific code).
CREATE OR REPLACE SECRET REFUND_AGENT_LAB.LANGFUSE.LANGFUSE_HOST
  TYPE = GENERIC_STRING SECRET_STRING = '<% langfuse_host %>'
  COMMENT = 'Langfuse ingress host for the refund lab app';

CREATE OR REPLACE EXTERNAL ACCESS INTEGRATION REFUND_LAB_APP_EAI
  ALLOWED_NETWORK_RULES = (REFUND_AGENT_LAB.DEMO.LANGFUSE_EGRESS_RULE)
  ALLOWED_AUTHENTICATION_SECRETS = (REFUND_AGENT_LAB.LANGFUSE.LF_INIT_PUBLIC_KEY,
                                    REFUND_AGENT_LAB.LANGFUSE.LF_INIT_SECRET_KEY,
                                    REFUND_AGENT_LAB.LANGFUSE.LANGFUSE_HOST)
  ENABLED = TRUE
  COMMENT = 'Refund lab app: Langfuse on SPCS (PyPI comes from PYPI_ACCESS_INTEGRATION)';

-- The app installs its Python packages from PyPI. IF NOT EXISTS leaves an existing
-- integration of this name alone; drop it in cleanup only if this script created it.
CREATE EXTERNAL ACCESS INTEGRATION IF NOT EXISTS PYPI_ACCESS_INTEGRATION
  ALLOWED_NETWORK_RULES = (SNOWFLAKE.EXTERNAL_ACCESS.PYPI_RULE)
  ENABLED = TRUE
  COMMENT = 'PyPI for Streamlit container runtime apps';
