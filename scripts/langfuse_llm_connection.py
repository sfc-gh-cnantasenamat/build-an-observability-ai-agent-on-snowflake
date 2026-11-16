"""Create Langfuse's LLM connection to Cortex (OpenAI-compatible) for native judges.
The PAT is read from ~/.langfuse_spcs.env and never printed."""
import base64
from pathlib import Path
import requests
import snowflake.connector
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from refund_agent import config
from refund_agent.observers import JUDGE_MODEL

HOST = config.LANGFUSE_HOST
CORTEX = f"{config.account_host()}/api/v2/cortex/v1"
env = dict(l.split("=", 1) for l in (Path.home() / ".langfuse_spcs.env").read_text().splitlines() if l)
conn = snowflake.connector.connect(connection_name=config.CONNECTION,
                                   session_parameters={"PYTHON_CONNECTOR_QUERY_RESULT_FORMAT": "json"})
tok = conn._rest._token_request("ISSUE")["data"]["sessionToken"]
basic = base64.b64encode(f"{env['LF_INIT_PUBLIC_KEY']}:{env['LF_INIT_SECRET_KEY']}".encode()).decode()
h = {"X-SF-SPCS-Authorization": f'Snowflake Token="{tok}"', "Authorization": f"Basic {basic}"}
r = requests.put(f"{HOST}/api/public/llm-connections", headers=h, timeout=30, json={
    "provider": "snowflake-cortex", "adapter": "openai", "secretKey": env["CORTEX_JUDGE_PAT"],
    "baseURL": CORTEX, "customModels": [JUDGE_MODEL], "withDefaultModels": False})
print("llm-connection:", r.status_code, r.text[:300].replace(env["CORTEX_JUDGE_PAT"], "***"))
for path in ("/api/public/llm-connections", "/api/public/evaluators", "/api/public/evals/configs",
             "/api/public/v2/evaluators"):
    g = requests.get(HOST + path, headers=h, timeout=30)
    print("GET", path, g.status_code, g.text[:200].replace(env["CORTEX_JUDGE_PAT"], "***"))
