-- Self-hosted Langfuse (open source, v4) on Snowpark Container Services.
-- Run as ACCOUNTADMIN once; the service itself is owned by LANGFUSE_ROLE
-- because services can't be created with ACCOUNTADMIN.
-- Run with: snow sql -f langfuse_spcs/setup.sql -D admin_user=<your user> -D warehouse=<warehouse>

CREATE ROLE IF NOT EXISTS LANGFUSE_ROLE COMMENT = 'Owns the self-hosted Langfuse SPCS service';
GRANT ROLE LANGFUSE_ROLE TO USER <% admin_user %>;

CREATE SCHEMA IF NOT EXISTS REFUND_AGENT_LAB.LANGFUSE;
GRANT USAGE ON DATABASE REFUND_AGENT_LAB TO ROLE LANGFUSE_ROLE;
GRANT OWNERSHIP ON SCHEMA REFUND_AGENT_LAB.LANGFUSE TO ROLE LANGFUSE_ROLE COPY CURRENT GRANTS;

-- 6 vCPU / ~28 GiB node: Langfuse web + worker, ClickHouse, Postgres, Redis, MinIO.
CREATE COMPUTE POOL IF NOT EXISTS LANGFUSE_POOL
  MIN_NODES = 1 MAX_NODES = 1
  INSTANCE_FAMILY = CPU_X64_M
  AUTO_SUSPEND_SECS = 3600 AUTO_RESUME = TRUE
  COMMENT = 'Self-hosted Langfuse for the refund agent lab';
GRANT USAGE, MONITOR, OPERATE ON COMPUTE POOL LANGFUSE_POOL TO ROLE LANGFUSE_ROLE;

-- Public endpoint for the Langfuse UI and API (Snowflake sign-in required).
GRANT BIND SERVICE ENDPOINT ON ACCOUNT TO ROLE LANGFUSE_ROLE;

GRANT USAGE ON WAREHOUSE <% warehouse %> TO ROLE LANGFUSE_ROLE;

USE ROLE LANGFUSE_ROLE;
CREATE IMAGE REPOSITORY IF NOT EXISTS REFUND_AGENT_LAB.LANGFUSE.IMAGES;
