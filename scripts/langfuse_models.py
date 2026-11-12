"""Register Cortex model prices in Langfuse so it computes cost natively.

Prices are Snowflake AI credits per token (Langfuse calls the unit "price"; read it as
credits, not USD). Idempotent: skips models already defined."""
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
JUDGE_RATE = {"llama4-maverick": (0.144, 0.582), "openai-gpt-5-mini": (0.15, 1.20)}  # credits per 1M tokens

env = dict(l.split("=", 1) for l in (Path.home() / ".langfuse_spcs.env").read_text().splitlines() if l)
conn = snowflake.connector.connect(connection_name=config.CONNECTION,
                                   session_parameters={"PYTHON_CONNECTOR_QUERY_RESULT_FORMAT": "json"})
token = conn._rest._token_request("ISSUE")["data"]["sessionToken"]
basic = base64.b64encode(f"{env['LF_INIT_PUBLIC_KEY']}:{env['LF_INIT_SECRET_KEY']}".encode()).decode()
headers = {"X-SF-SPCS-Authorization": f'Snowflake Token="{token}"', "Authorization": f"Basic {basic}"}

existing = requests.get(f"{HOST}/api/public/models", headers=headers, params={"limit": 100}, timeout=30)
print("GET models:", existing.status_code, existing.text[:200])
names = {m["modelName"] for m in existing.json().get("data", [])} if existing.ok else set()
for model, (rate_in, rate_out) in {**CREDITS_PER_M_TOKENS, **JUDGE_RATE}.items():
    if model in names:
        print("exists:", model); continue
    r = requests.post(f"{HOST}/api/public/models", headers=headers, timeout=30, json={
        "modelName": model, "matchPattern": f"(?i)^{model.replace('.', '[.]')}$", "unit": "TOKENS",
        "inputPrice": rate_in / 1e6, "outputPrice": rate_out / 1e6})
    print("exists:" if "already exists" in r.text else f"POST {r.status_code}", model)
