"""Check self-hosted Langfuse on SPCS end to end, from outside Snowflake.

SPCS ingress requires Snowflake authentication. The proxy strips `X-SF-SPCS-Authorization`
and only strips `Authorization` when it holds a Snowflake token, so:
  - the Snowflake session token goes in `X-SF-SPCS-Authorization`
  - Langfuse's own Basic auth (public:secret key) stays in `Authorization`
Steps: health check, project lookup with Langfuse keys, then send a trace with the
Langfuse Python SDK and read it back. Secrets come from ~/.langfuse_spcs.env, never printed.
"""

import base64
import sys
import time
import uuid
from pathlib import Path

import requests
import snowflake.connector
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from refund_agent import config

HOST = config.LANGFUSE_HOST
ENV = dict(line.split("=", 1) for line in
           (Path.home() / ".langfuse_spcs.env").read_text().splitlines() if line)


def main():
    con = snowflake.connector.connect(
        connection_name=config.CONNECTION,
        session_parameters={"PYTHON_CONNECTOR_QUERY_RESULT_FORMAT": "json"},
    )
    try:
        # Tutorial 8: ingress needs a freshly issued session token, not the connector's own.
        token = con._rest._token_request("ISSUE")["data"]["sessionToken"]
        spcs = {"X-SF-SPCS-Authorization": f'Snowflake Token="{token}"'}
        basic = base64.b64encode(
            f"{ENV['LF_INIT_PUBLIC_KEY']}:{ENV['LF_INIT_SECRET_KEY']}".encode()).decode()
        both = {**spcs, "Authorization": f"Basic {basic}"}

        r = requests.get(f"{HOST}/api/public/health", headers=spcs, timeout=30,
                         allow_redirects=False)
        print("1 health:", r.status_code, r.text[:120])
        if r.status_code != 200:
            sys.exit(1)

        r = requests.get(f"{HOST}/api/public/projects", headers=both, timeout=30)
        print("2 projects with Langfuse keys:", r.status_code,
              [p.get("name") for p in r.json().get("data", [])] if r.ok else r.text[:200])
        if not r.ok:
            sys.exit(1)

        # 3. SDK trace: point the SDK at SPCS and add the Snowflake header to its exporter.
        from langfuse import Langfuse
        trace_name = f"spcs-smoke-{uuid.uuid4().hex[:8]}"
        lf = Langfuse(public_key=ENV["LF_INIT_PUBLIC_KEY"], secret_key=ENV["LF_INIT_SECRET_KEY"],
                      base_url=HOST, additional_headers=spcs)
        with lf.start_as_current_observation(as_type="span", name=trace_name) as span:
            span.update(input={"order": "O1"}, output={"claim": "refunded"})
            trace_id = lf.get_current_trace_id()
        lf.flush()
        print("3 sent trace:", trace_name, trace_id)

        # v4 runs in events-only mode: the legacy /traces/{id} read API is disabled,
        # so read the trace back through the v2 observations API.
        for attempt in range(12):
            time.sleep(5)
            r = requests.get(f"{HOST}/api/public/v2/observations", headers=both,
                             params={"traceId": trace_id}, timeout=30)
            names = [o.get("name") for o in r.json().get("data", [])] if r.ok else []
            if trace_name in names:
                print("4 read back:", names, f"after {5 * (attempt + 1)}s")
                return
        print("4 trace not readable yet:", r.status_code, r.text[:200])
        sys.exit(1)
    finally:
        con.close()


if __name__ == "__main__":
    main()
