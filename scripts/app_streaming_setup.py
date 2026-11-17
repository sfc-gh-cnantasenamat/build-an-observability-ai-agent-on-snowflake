"""Let the hosted Streamlit app write with Snowpipe Streaming.

Creates, idempotently:
- the secret REFUND_AGENT_LAB.DEMO.STREAMER_PRIVATE_KEY, holding the REFUND_LAB_STREAMER
  private key (read from ~/.snowflake/keys/refund_lab_streamer.p8, never printed);
- the network rule REFUND_AGENT_LAB.DEMO.STREAMING_EGRESS_RULE for the hosts the streaming
  SDK calls: the account endpoint, the streaming ingest endpoint and the account's stage
  storage, all discovered from SYSTEM$ALLOWLIST() rather than hard-coded;
- the external access integration REFUND_LAB_STREAM_EAI, which the app's snowflake.yml
  attaches.

The key goes into SQL as an escaped literal (CREATE SECRET takes no bind variables), not
through the shell, so it never appears in a command line or history.
Cleanup: DROP INTEGRATION REFUND_LAB_STREAM_EAI; DROP SECRET
REFUND_AGENT_LAB.DEMO.STREAMER_PRIVATE_KEY; DROP NETWORK RULE
REFUND_AGENT_LAB.DEMO.STREAMING_EGRESS_RULE.
"""
import json
import sys
from pathlib import Path

import snowflake.connector

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from refund_agent import config

KEY_FILE = Path.home() / ".snowflake/keys/refund_lab_streamer.p8"
# Hosts the SDK needs: account endpoint, ingest endpoint, stage storage (uploads).
NEEDED = {"SNOWFLAKE_DEPLOYMENT", "SNOWFLAKE_DEPLOYMENT_REGIONLESS", "STREAMING_ROWSET", "STAGE"}


def egress_hosts(allowlist: list[dict], account: str) -> list[str]:
    """host:port entries for the network rule. The SDK replaces underscores with hyphens
    in hostnames, so both spellings are allowed for the account and ingest hosts."""
    hosts = {f"{account.lower().replace('_', '-')}.snowflakecomputing.com:443"}
    for entry in allowlist:
        if entry["type"] in NEEDED:
            host = entry["host"].lower()
            hosts.update({f"{host}:{entry['port']}", f"{host.replace('_', '-')}:{entry['port']}"})
    return sorted(hosts)


def sql_literal(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


if __name__ == "__main__":
    if KEY_FILE.stat().st_mode & 0o077:
        sys.exit(f"{KEY_FILE} must not be group/world readable (chmod 600)")
    key = KEY_FILE.read_text()
    if "PRIVATE KEY" not in key:
        sys.exit(f"{KEY_FILE} is not a PEM private key")
    conn = snowflake.connector.connect(connection_name=config.CONNECTION)
    cur = conn.cursor()
    cur.execute("SELECT CURRENT_ORGANIZATION_NAME(), CURRENT_ACCOUNT()")
    if tuple(cur.fetchone()) != config.expected_identity():
        sys.exit(f"Refusing to write outside {config.ACCOUNT} (REFUND_LAB_ACCOUNT)")
    cur.execute("SELECT SYSTEM$ALLOWLIST()")
    hosts = egress_hosts(json.loads(cur.fetchone()[0]), config.ACCOUNT)
    if not any(".ingest." in h for h in hosts):
        sys.exit("SYSTEM$ALLOWLIST() lists no streaming ingest host for this account")
    cur.execute("CREATE OR REPLACE SECRET REFUND_AGENT_LAB.DEMO.STREAMER_PRIVATE_KEY "
                f"TYPE = GENERIC_STRING SECRET_STRING = {sql_literal(key)} "
                "COMMENT = 'Refund lab: REFUND_LAB_STREAMER private key for the app'")
    cur.execute("CREATE OR REPLACE NETWORK RULE REFUND_AGENT_LAB.DEMO.STREAMING_EGRESS_RULE "
                "TYPE = HOST_PORT MODE = EGRESS VALUE_LIST = ("
                + ", ".join(sql_literal(h) for h in hosts) + ")")
    cur.execute("CREATE OR REPLACE EXTERNAL ACCESS INTEGRATION REFUND_LAB_STREAM_EAI "
                "ALLOWED_NETWORK_RULES = (REFUND_AGENT_LAB.DEMO.STREAMING_EGRESS_RULE) "
                "ALLOWED_AUTHENTICATION_SECRETS = (REFUND_AGENT_LAB.DEMO.STREAMER_PRIVATE_KEY) "
                "ENABLED = TRUE COMMENT = 'Refund lab app: Snowpipe Streaming'")
    print(f"Secret, network rule ({len(hosts)} hosts) and REFUND_LAB_STREAM_EAI are ready.")
