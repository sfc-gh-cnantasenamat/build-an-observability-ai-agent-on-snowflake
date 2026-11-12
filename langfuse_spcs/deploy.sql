-- Deploy self-hosted Langfuse on SPCS. Run as ACCOUNTADMIN after setup.sql.
-- The service must be owned by LANGFUSE_ROLE (ACCOUNTADMIN can't create services),
-- so an owner's-rights procedure owned by LANGFUSE_ROLE performs the service DDL.

CREATE STAGE IF NOT EXISTS REFUND_AGENT_LAB.LANGFUSE.SPECS
  DIRECTORY = (ENABLE = TRUE) COMMENT = 'Langfuse service specification';
GRANT READ ON STAGE REFUND_AGENT_LAB.LANGFUSE.SPECS TO ROLE LANGFUSE_ROLE;
-- PUT file://langfuse_spcs/langfuse.yaml @REFUND_AGENT_LAB.LANGFUSE.SPECS AUTO_COMPRESS=FALSE OVERWRITE=TRUE;

CREATE OR REPLACE PROCEDURE REFUND_AGENT_LAB.LANGFUSE.MANAGE_LANGFUSE(ACTION VARCHAR, NEXTAUTH_URL VARCHAR)
RETURNS VARCHAR
LANGUAGE SQL
EXECUTE AS OWNER
AS
$$
DECLARE
  spec_clause VARCHAR;
BEGIN
  -- Only a bare https origin (or the local default) may reach the spec template.
  IF (NOT (NEXTAUTH_URL = 'http://localhost:3000'
           OR REGEXP_LIKE(NEXTAUTH_URL, '^https://[a-z0-9-]+(\\.[a-z0-9-]+)+$'))) THEN
    RETURN 'rejected: NEXTAUTH_URL must be a bare https origin';
  END IF;
  spec_clause := 'FROM @REFUND_AGENT_LAB.LANGFUSE.SPECS SPECIFICATION_TEMPLATE_FILE = ''langfuse.yaml'' '
              || 'USING (nextauth_url => ''"' || NEXTAUTH_URL || '"'')';
  CASE (ACTION)
    WHEN 'create' THEN
      EXECUTE IMMEDIATE 'CREATE SERVICE IF NOT EXISTS REFUND_AGENT_LAB.LANGFUSE.LANGFUSE '
        || 'IN COMPUTE POOL LANGFUSE_POOL ' || spec_clause
        || ' MIN_INSTANCES = 1 MAX_INSTANCES = 1 '
        || 'COMMENT = ''Self-hosted Langfuse v4 for the refund agent lab''';
      EXECUTE IMMEDIATE 'GRANT MONITOR, OPERATE ON SERVICE REFUND_AGENT_LAB.LANGFUSE.LANGFUSE TO ROLE ACCOUNTADMIN';
      RETURN 'created';
    WHEN 'upgrade' THEN
      EXECUTE IMMEDIATE 'ALTER SERVICE REFUND_AGENT_LAB.LANGFUSE.LANGFUSE ' || spec_clause;
      RETURN 'upgraded';
    WHEN 'suspend' THEN
      EXECUTE IMMEDIATE 'ALTER SERVICE REFUND_AGENT_LAB.LANGFUSE.LANGFUSE SUSPEND';
      RETURN 'suspended';
    WHEN 'resume' THEN
      EXECUTE IMMEDIATE 'ALTER SERVICE REFUND_AGENT_LAB.LANGFUSE.LANGFUSE RESUME';
      RETURN 'resumed';
    WHEN 'grant_admin' THEN
      -- Lets the deploying admin reach the public endpoint (UI and API).
      EXECUTE IMMEDIATE 'GRANT SERVICE ROLE REFUND_AGENT_LAB.LANGFUSE.LANGFUSE!ALL_ENDPOINTS_USAGE TO ROLE ACCOUNTADMIN';
      RETURN 'granted endpoint usage to ACCOUNTADMIN';
    WHEN 'set_eai' THEN
      -- Egress to the Cortex REST API, for Langfuse's native LLM-as-a-judge evaluators
      -- (see judge_access.sql). Takes effect on restart.
      EXECUTE IMMEDIATE 'ALTER SERVICE REFUND_AGENT_LAB.LANGFUSE.LANGFUSE '
        || 'SET EXTERNAL_ACCESS_INTEGRATIONS = (LANGFUSE_CORTEX_EAI)';
      RETURN 'external access set: LANGFUSE_CORTEX_EAI';
    WHEN 'idle_check' THEN
      -- Suspend when RUNNING, started/upgraded over 30 min ago, and no ingress
      -- (UI or API) in the last 30 min. Uses the spec's network platform metrics.
      SHOW SERVICES LIKE 'LANGFUSE' IN SCHEMA REFUND_AGENT_LAB.LANGFUSE;
      LET show_id VARCHAR := SQLID;
      LET svc_status VARCHAR := (SELECT MAX("status") FROM TABLE(RESULT_SCAN(:show_id)));
      LET started TIMESTAMP_LTZ := (SELECT MAX(GREATEST("resumed_on", "updated_on")) FROM TABLE(RESULT_SCAN(:show_id)));
      IF (svc_status <> 'RUNNING') THEN
        RETURN 'skip: service is ' || svc_status;
      END IF;
      IF (started > DATEADD('minute', -30, CURRENT_TIMESTAMP())) THEN
        RETURN 'skip: started or upgraded under 30 minutes ago';
      END IF;
      LET active_points NUMBER := (
        SELECT COUNT(*) FROM TABLE(REFUND_AGENT_LAB.LANGFUSE.LANGFUSE!SPCS_GET_METRICS(
          START_TIME => DATEADD('minute', -30, CURRENT_TIMESTAMP())))
        WHERE METRIC_NAME IN ('network.ingress.connections.active', 'network.ingress.cps')
          AND VALUE > 0);
      IF (active_points > 0) THEN
        RETURN 'in use: ' || active_points || ' active ingress data points in the last 30 minutes';
      END IF;
      EXECUTE IMMEDIATE 'ALTER SERVICE REFUND_AGENT_LAB.LANGFUSE.LANGFUSE SUSPEND';
      RETURN 'suspended: no ingress for 30 minutes';
    WHEN 'endpoint' THEN
      SHOW ENDPOINTS IN SERVICE REFUND_AGENT_LAB.LANGFUSE.LANGFUSE;
      LET url VARCHAR := (SELECT MAX("ingress_url") FROM TABLE(RESULT_SCAN(LAST_QUERY_ID())));
      RETURN url;
    ELSE
      RETURN 'rejected: unknown action';
  END CASE;
END;
$$;

GRANT OWNERSHIP ON PROCEDURE REFUND_AGENT_LAB.LANGFUSE.MANAGE_LANGFUSE(VARCHAR, VARCHAR)
  TO ROLE LANGFUSE_ROLE COPY CURRENT GRANTS;
GRANT USAGE ON PROCEDURE REFUND_AGENT_LAB.LANGFUSE.MANAGE_LANGFUSE(VARCHAR, VARCHAR) TO ROLE ACCOUNTADMIN;
