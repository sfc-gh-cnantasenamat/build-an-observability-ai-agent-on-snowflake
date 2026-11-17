#!/usr/bin/env bash
# Create (or replace) the 7-day Cortex token Langfuse's judges use, as REFUND_LAB_JUDGE.
# A token's role restriction must match the creating session's role, so the judge user
# creates its own token through a temporary key pair, which this script removes again,
# along with the temporary permission, even if a step fails.
# Langfuse keeps its own copy in its Cortex connection, so the script updates that too;
# Langfuse must be running (MANAGE_LANGFUSE('resume', ...)).
# Run from the project folder after `source .refund_lab.env`, every 7 days.
set -euo pipefail
: "${REFUND_LAB_CONNECTION:?source .refund_lab.env first}"
KEY="$HOME/.snowflake/keys/refund_lab_judge_tmp.p8"
sf() { snow sql --connection "$REFUND_LAB_CONNECTION" -q "$1" >/dev/null; }

cleanup() {
  sf "REVOKE MODIFY PROGRAMMATIC AUTHENTICATION METHODS ON USER REFUND_LAB_JUDGE FROM ROLE REFUND_LAB_JUDGE_ROLE" || true
  sf "ALTER USER REFUND_LAB_JUDGE UNSET RSA_PUBLIC_KEY" || true
  rm -f "$KEY"
}
trap cleanup EXIT

mkdir -p "$(dirname "$KEY")"
( umask 077; openssl genrsa 2048 2>/dev/null | openssl pkcs8 -topk8 -nocrypt -out "$KEY" )
PUB=$(openssl rsa -in "$KEY" -pubout 2>/dev/null | grep -v -- ----- | tr -d '\n')
sf "ALTER USER REFUND_LAB_JUDGE SET RSA_PUBLIC_KEY='$PUB'"
sf "GRANT MODIFY PROGRAMMATIC AUTHENTICATION METHODS ON USER REFUND_LAB_JUDGE TO ROLE REFUND_LAB_JUDGE_ROLE"
.venv/bin/python scripts/langfuse_judge_setup.py
.venv/bin/python scripts/langfuse_llm_connection.py
