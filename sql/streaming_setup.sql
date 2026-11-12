-- Snowpipe Streaming (high-performance) identity for the refund lab.
-- A key-pair-only SERVICE user whose role can only insert into the lab's event tables.
-- The private key lives at ~/.snowflake/keys/refund_lab_streamer.p8 (mode 600). Generate
-- your own pair, then pass the public key body (no BEGIN/END lines, no newlines):
--   snow sql -f sql/streaming_setup.sql -D streamer_public_key=<public key body>
-- Cleanup: DROP USER REFUND_LAB_STREAMER; DROP ROLE REFUND_LAB_STREAM_ROLE;
CREATE ROLE IF NOT EXISTS REFUND_LAB_STREAM_ROLE COMMENT = 'Refund lab: stream run events only';
GRANT USAGE ON DATABASE REFUND_AGENT_LAB TO ROLE REFUND_LAB_STREAM_ROLE;
GRANT USAGE ON SCHEMA REFUND_AGENT_LAB.DEMO TO ROLE REFUND_LAB_STREAM_ROLE;
GRANT INSERT ON TABLE REFUND_AGENT_LAB.DEMO.RUNS TO ROLE REFUND_LAB_STREAM_ROLE;
GRANT INSERT ON TABLE REFUND_AGENT_LAB.DEMO.TRACE_EVENTS TO ROLE REFUND_LAB_STREAM_ROLE;
GRANT INSERT ON TABLE REFUND_AGENT_LAB.DEMO.LLM_CALLS TO ROLE REFUND_LAB_STREAM_ROLE;

CREATE USER IF NOT EXISTS REFUND_LAB_STREAMER
  TYPE = SERVICE
  DEFAULT_ROLE = REFUND_LAB_STREAM_ROLE
  RSA_PUBLIC_KEY = '<% streamer_public_key %>'
  COMMENT = 'Refund lab Snowpipe Streaming client (key-pair only)';
GRANT ROLE REFUND_LAB_STREAM_ROLE TO USER REFUND_LAB_STREAMER;
