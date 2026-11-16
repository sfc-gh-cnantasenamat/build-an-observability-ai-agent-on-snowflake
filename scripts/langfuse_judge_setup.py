"""Create the 7-day PAT for REFUND_LAB_JUDGE, store it, and test a Cortex REST call.

The token is never printed. It's saved to the Snowflake secret
REFUND_AGENT_LAB.LANGFUSE.CORTEX_JUDGE_PAT and to ~/.langfuse_spcs.env (mode 600) as
CORTEX_JUDGE_PAT, for configuring Langfuse's LLM connection.

A PAT's role restriction must match the creating session's role, so this connects as the
judge user. Before running, as ACCOUNTADMIN: set a temporary RSA key on REFUND_LAB_JUDGE
(private key at ~/.snowflake/keys/refund_lab_judge_tmp.p8) and
GRANT MODIFY PROGRAMMATIC AUTHENTICATION METHODS ON USER REFUND_LAB_JUDGE TO ROLE
REFUND_LAB_JUDGE_ROLE. Afterwards revoke the grant, UNSET RSA_PUBLIC_KEY, delete the key.
"""
import os
import stat
import sys
from pathlib import Path

import requests
import snowflake.connector
from cryptography.hazmat.primitives import serialization

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from refund_agent import config
from refund_agent.observers import JUDGE_MODEL

ACCOUNT_HOST = config.account_host()
ENV_FILE = Path.home() / ".langfuse_spcs.env"

# Connect as the judge user itself (temporary key pair): a PAT's role restriction must
# match the creating session's role, and the admin connection is ACCOUNTADMIN.
key = serialization.load_pem_private_key(
    (Path.home() / ".snowflake/keys/refund_lab_judge_tmp.p8").read_bytes(), password=None)
conn = snowflake.connector.connect(
    account=config.ACCOUNT, user="REFUND_LAB_JUDGE",
    role="REFUND_LAB_JUDGE_ROLE", private_key=key)
cur = conn.cursor()
cur.execute("SELECT CURRENT_ORGANIZATION_NAME(), CURRENT_ACCOUNT(), CURRENT_ROLE()")
assert tuple(cur.fetchone()) == (*config.expected_identity(), "REFUND_LAB_JUDGE_ROLE")

# Rotating: a token keeps its name after it expires, so remove the old one first.
cur.execute("SHOW USER PROGRAMMATIC ACCESS TOKENS")
names = [dict(zip([d[0].lower() for d in cur.description], r)).get("name") for r in cur.fetchall()]
if "LANGFUSE_JUDGE" in names:
    cur.execute("ALTER USER REMOVE PROGRAMMATIC ACCESS TOKEN LANGFUSE_JUDGE")

cur.execute("ALTER USER ADD PROGRAMMATIC ACCESS TOKEN LANGFUSE_JUDGE "
            "ROLE_RESTRICTION = 'REFUND_LAB_JUDGE_ROLE' DAYS_TO_EXPIRY = 7 "
            "COMMENT = 'Langfuse native evaluators (refund lab)'")
cols = [d[0].lower() for d in cur.description]
row = dict(zip(cols, cur.fetchone()))
token = row.get("token_secret")
assert token, f"no token returned (columns: {cols})"

# Snowflake secret (bind-free DDL: escape single quotes; a PAT has none, but be safe).
literal = token.replace("\\", "\\\\").replace("'", "\\'")
admin = snowflake.connector.connect(connection_name=config.CONNECTION)
admin.cursor().execute("CREATE OR REPLACE SECRET REFUND_AGENT_LAB.LANGFUSE.CORTEX_JUDGE_PAT "
            f"TYPE = GENERIC_STRING SECRET_STRING = '{literal}' "
            "COMMENT = 'PAT for REFUND_LAB_JUDGE (7-day)'")

lines = [l for l in ENV_FILE.read_text().splitlines() if l and not l.startswith("CORTEX_JUDGE_PAT=")]
ENV_FILE.write_text("\n".join(lines + [f"CORTEX_JUDGE_PAT={token}"]) + "\n")
os.chmod(ENV_FILE, stat.S_IRUSR | stat.S_IWUSR)

r = requests.post(f"{ACCOUNT_HOST}/api/v2/cortex/v1/chat/completions", timeout=60,
                  headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                  json={"model": JUDGE_MODEL, "messages": [{"role": "user", "content": "Reply with OK"}]})
print("Cortex REST test:", r.status_code,
      (r.json().get("choices") or [{}])[0].get("message", {}).get("content") if r.ok else r.text[:200])
print("PAT stored in secret and env file; expires", row.get("expires_at", "in 7 days"))
