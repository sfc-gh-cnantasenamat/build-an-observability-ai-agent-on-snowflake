"""Optional high-performance Snowpipe Streaming path for redacted application events.

Requires an existing, separately authorized key-pair service identity and pipe.
This module never stores private keys, creates users, or substitutes connector inserts.
"""

from __future__ import annotations

from pathlib import Path
from tempfile import NamedTemporaryFile
import json
import os

from refund_agent import config
from refund_agent.events import canonical_events


DATABASE = "REFUND_AGENT_LAB"
SCHEMA = "DEMO"
PIPE = "TRACE_EVENTS-STREAMING"
# The streaming identity must be least-privilege; these roles are refused.
ADMIN_ROLES = {"ACCOUNTADMIN", "ORGADMIN", "SECURITYADMIN", "SYSADMIN", "USERADMIN"}


def event_row(event: dict) -> dict:
    """Keep only the columns in the isolated TRACE_EVENTS table."""
    return {
        "EVENT_ID": event["event_id"],
        "RUN_ID": event["run_id"],
        "EVENT_AT": event["event_at"],
        "KIND": event["kind"],
        "NODE": event["node"],
        "DETAIL": event["detail"],
        "EVENT_ORDINAL": event["event_ordinal"],
    }


def stream_run(channel, run: dict) -> str:
    """Append one run to its dedicated channel; return final commit-check offset."""
    events = canonical_events(run)
    for event in events:
        offset = f"{run['run_id']}:{event['event_ordinal']:03d}"
        channel.append_row(event_row(event), offset_token=offset)
    return offset


def open_streaming_client(*, account: str, user: str, role: str,
                          private_key_path: str, client_name: str):
    """Create a channel client using a pre-provisioned key-pair identity only."""
    if (account.lower() != config.streaming_account() or not all((user, role, client_name))
            or role.upper() in ADMIN_ROLES):
        raise ValueError("Only a named key-pair service identity in REFUND_LAB_ACCOUNT is permitted")
    key_file = Path(private_key_path).expanduser()
    if not key_file.is_file():
        raise FileNotFoundError("Streaming private key path does not exist")
    if key_file.stat().st_mode & 0o077:
        raise PermissionError("Streaming private key must not be group/world accessible")

    from snowflake.ingest.streaming import StreamingIngestClient

    profile = {
        "account": account,
        "user": user,
        "role": role,
        "url": f"https://{account}.snowflakecomputing.com:443",
        "private_key": key_file.read_text(),
    }
    with NamedTemporaryFile(mode="w", prefix="refund-streaming-", suffix=".json",
                            delete=False) as temp:
        profile_path = temp.name
        os.chmod(profile_path, 0o600)
        json.dump(profile, temp)
    try:
        return StreamingIngestClient(
            client_name=client_name,
            db_name=DATABASE,
            schema_name=SCHEMA,
            pipe_name=PIPE,
            profile_json=profile_path,
        )
    finally:
        Path(profile_path).unlink(missing_ok=True)