"""Create Langfuse's native LLM-as-a-judge evaluators for the refund lab.

Two evaluators mirror the TruLens metrics (groundedness, answer_relevance) and run inside
Langfuse on the Cortex JUDGE_MODEL through the "snowflake-cortex" LLM connection. An
evaluation rule runs them on every observation whose metadata has
`lab_step = judge_input`: the step LangfuseObserver adds to each run's trace with the
same request and facts TruLens sees. Idempotent by name; an existing evaluator whose
model differs from JUDGE_MODEL is switched to it.
"""
import base64
import json
import sys
from pathlib import Path

import requests
import snowflake.connector

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from refund_agent import config
from refund_agent.observers import JUDGE_MODEL, LANGFUSE_JUDGE_PROVIDER, NATIVE_JUDGE_STEP

HOST = config.LANGFUSE_HOST
EVALUATORS = {
    "groundedness": (
        "You check whether a customer-support reply is supported by the facts.\n\n"
        "Facts:\n{{facts}}\n\nReply:\n{{reply}}\n\n"
        "Score from 0 to 1: the fraction of the reply's factual claims (what happened to "
        "the refund, amounts, reasons) that the facts directly support. A claim the facts "
        "contradict or don't contain counts as unsupported."),
    "answer_relevance": (
        "You check whether a customer-support reply addresses the customer's request.\n\n"
        "Request:\n{{request}}\n\nReply:\n{{reply}}\n\n"
        "Score from 0 to 1: how directly and completely the reply answers the request."),
}
MAPPING = [{"variable": "request", "source": "input", "jsonPath": "$.request"},
           {"variable": "facts", "source": "input", "jsonPath": "$.facts"},
           {"variable": "reply", "source": "output"}]

env = dict(l.split("=", 1) for l in (Path.home() / ".langfuse_spcs.env").read_text().splitlines() if l)
conn = snowflake.connector.connect(connection_name=config.CONNECTION,
                                   session_parameters={"PYTHON_CONNECTOR_QUERY_RESULT_FORMAT": "json"})
tok = conn._rest._token_request("ISSUE")["data"]["sessionToken"]
basic = base64.b64encode(f"{env['LF_INIT_PUBLIC_KEY']}:{env['LF_INIT_SECRET_KEY']}".encode()).decode()
h = {"X-SF-SPCS-Authorization": f'Snowflake Token="{tok}"', "Authorization": f"Basic {basic}"}

existing = {e["name"]: e for e in requests.get(f"{HOST}/api/public/v2/evaluators", headers=h,
                                               timeout=30).json().get("data", [])}
ids = {}
model_config = {"provider": LANGFUSE_JUDGE_PROVIDER, "model": JUDGE_MODEL}
OUTPUT = {"dataType": "NUMERIC", "minValue": 0, "maxValue": 1,
          "scoreValueInstructions": "A number from 0 to 1.",
          "scoreReasoningInstructions": "One or two sentences."}
for name, prompt in EVALUATORS.items():
    variables = [m for m in MAPPING if "{{" + m["variable"] + "}}" in prompt]
    definition = {"type": "llm_as_judge", "prompt": prompt, "modelConfig": model_config,
                  "variableMapping": variables, "outputDefinition": OUTPUT}
    if name in existing:
        ids[name] = existing[name]["id"]
        if (existing[name].get("modelConfig") or {}).get("model") == JUDGE_MODEL:
            print("exists:", name); continue
        # A model change is a new evaluator version: send the complete definition.
        r = requests.patch(f"{HOST}/api/public/v2/evaluators/{ids[name]}", headers=h,
                           timeout=30, json=definition)
        print("PATCH evaluator", name, r.status_code, r.text[:300])
        r.raise_for_status()
        continue
    r = requests.post(f"{HOST}/api/public/v2/evaluators", headers=h, timeout=30, json={
        **definition, "name": name, "description": f"Refund lab: mirrors TruLens {name}"})
    print("POST evaluator", name, r.status_code, r.text[:300])
    r.raise_for_status()
    ids[name] = r.json()["id"]

rules = requests.get(f"{HOST}/api/public/v2/evaluation-rules", headers=h, timeout=30).json().get("data", [])
if any(rule["name"] == "refund-lab-judges" for rule in rules):
    print("rule exists")
else:
    r = requests.post(f"{HOST}/api/public/v2/evaluation-rules", headers=h, timeout=30, json={
        "name": "refund-lab-judges", "enabled": True,
        "filter": [{"type": "stringObject", "column": "metadata", "key": "lab_step",
                    "operator": "=", "value": NATIVE_JUDGE_STEP}],
        "evaluatorAssignments": [{"evaluatorId": i} for i in ids.values()]})
    print("POST rule", r.status_code, r.text[:400])
    r.raise_for_status()
print(json.dumps(ids))
