"""Probe: stream one synthetic TRACE_EVENTS row via Snowpipe Streaming and confirm commit."""
import json
import os
import stat
import sys
import tempfile
import time
import uuid
from pathlib import Path

from snowflake.ingest.streaming import StreamingIngestClient
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from refund_agent import config

key = (Path.home() / ".snowflake/keys/refund_lab_streamer.p8").read_text()
profile = {"account": config.ACCOUNT, "user": "REFUND_LAB_STREAMER",
           "url": f"{config.account_host()}:443",
           "private_key": key, "role": "REFUND_LAB_STREAM_ROLE"}
fd, path = tempfile.mkstemp(suffix=".json")
os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
with os.fdopen(fd, "w") as f:
    json.dump(profile, f)
try:
    client = StreamingIngestClient(client_name="refund_lab_probe", db_name="REFUND_AGENT_LAB",
                                   schema_name="DEMO", pipe_name="TRACE_EVENTS-STREAMING",
                                   profile_json=path)
    channel, status = client.open_channel(channel_name="probe")
    print("opened", status, flush=True)
    event_id = str(uuid.uuid4())
    channel.append_row({"EVENT_ID": event_id, "RUN_ID": "stream-probe", "EVENT_AT":
                        "2026-11-08T12:00:00+00:00", "KIND": "probe", "NODE": "probe",
                        "DETAIL": {"ok": True}, "EVENT_ORDINAL": 0}, offset_token="1")
    channel.wait_for_commit(token_checker=lambda t: t == "1", timeout_seconds=60)
    print("committed offset:", channel.get_latest_committed_offset_token(), "event", event_id, flush=True)
    channel.close()
    client.close()
finally:
    os.remove(path)
