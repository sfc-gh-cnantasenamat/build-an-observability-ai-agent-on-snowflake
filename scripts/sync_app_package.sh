#!/usr/bin/env bash
# Copy the shared lab package into the Streamlit bundle. src/refund_agent is the source
# of truth; tests/test_sis_bundle.py fails if the copies drift.
set -euo pipefail
cd "$(dirname "$0")/.."
for f in __init__.py config.py events.py agents.py lab.py observers.py; do
  cp "src/refund_agent/$f" "refund_truth_sis/refund_agent/$f"
done
echo "synced refund_agent into refund_truth_sis"
