"""Register Cortex model prices in Langfuse so it computes cost natively.

Prices are Snowflake AI credits per token (Langfuse calls the unit "price"; read it as
credits, not USD). Langfuse also ships its own USD prices for many provider model names
(for example claude-sonnet-5-5); a project-defined model takes precedence over those, so
the lab defines every model it uses. Idempotent: skips models the lab already defined."""
import base64
import sys
from pathlib import Path

import requests
import snowflake.connector

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from refund_agent import config
from refund_agent.agents import CREDITS_PER_M_TOKENS
from refund_agent.observers import JUDGE_MODEL

HOST = config.LANGFUSE_HOST
# The judge's rate (AI_COMPLETE, Table 6(a), Service Consumption Table effective 2026-11-12).
JUDGE_RATE = {"openai-gpt-5.4-mini": (0.45, 2.70)}  # credits per 1M tokens
assert JUDGE_MODEL in JUDGE_RATE, f"add {JUDGE_MODEL}'s rate to JUDGE_RATE"

env = dict(l.split("=", 1) for l in (Path.home() / ".langfuse_spcs.env").read_text().splitlines() if l)
conn = snowflake.connector.connect(connection_name=config.CONNECTION,
                                   session_parameters={"PYTHON_CONNECTOR_QUERY_RESULT_FORMAT": "json"})
token = conn._rest._token_request("ISSUE")["data"]["sessionToken"]
basic = base64.b64encode(f"{env['LF_INIT_PUBLIC_KEY']}:{env['LF_INIT_SECRET_KEY']}".encode()).decode()
headers = {"X-SF-SPCS-Authorization": f'Snowflake Token="{token}"', "Authorization": f"Basic {basic}"}

models, page = [], 1
while True:
    response = requests.get(f"{HOST}/api/public/models", headers=headers,
                            params={"limit": 100, "page": page}, timeout=30)
    response.raise_for_status()
    models += response.json()["data"]
    if page >= response.json()["meta"]["totalPages"]:
        break
    page += 1
# Only the lab's own (project) definitions count; Langfuse-managed ones are priced in USD.
names = {m["modelName"] for m in models if not m.get("isLangfuseManaged")}
for model, (rate_in, rate_out) in {**CREDITS_PER_M_TOKENS, **JUDGE_RATE}.items():
    if model in names:
        print("exists:", model); continue
    r = requests.post(f"{HOST}/api/public/models", headers=headers, timeout=30, json={
        "modelName": model, "matchPattern": f"(?i)^{model.replace('.', '[.]')}$", "unit": "TOKENS",
        "inputPrice": rate_in / 1e6, "outputPrice": rate_out / 1e6})
    print(f"POST {r.status_code}", model, "" if r.ok else r.text[:200])
