import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from refund_agent.graph import CASES, run_case
from refund_agent.streaming import event_row, open_streaming_client, stream_run


class StreamingTests(unittest.TestCase):
    def test_redacted_ordered_events_and_offsets(self):
        channel = MagicMock()
        run = run_case(CASES[0], "baseline", run_id="test-streaming-run")
        self.assertEqual(stream_run(channel, run), "test-streaming-run:003")
        rows = [call.args[0] for call in channel.append_row.call_args_list]
        tokens = [call.kwargs["offset_token"] for call in channel.append_row.call_args_list]
        self.assertEqual(tokens, [f"test-streaming-run:{ordinal:03d}" for ordinal in (1, 2, 3)])
        self.assertEqual([row["EVENT_ORDINAL"] for row in rows], [1, 2, 3])
        self.assertEqual([row["KIND"] for row in rows], ["policy_read", "tool_return", "agent_claim"])
        self.assertEqual(set(rows[0]), set(event_row({
            "event_id": "id", "run_id": "run", "event_at": "time", "kind": "kind",
            "node": "node", "detail": {}, "event_ordinal": 1,
        })))

    def test_missing_key_refuses_to_open_client(self):
        with self.assertRaises(FileNotFoundError):
            open_streaming_client(account="myorg-my_account", user="service",
                                  role="streaming_role", private_key_path="/not/a/key.p8",
                                  client_name="refund_test")

    def test_wrong_account_refuses_to_open_client(self):
        with self.assertRaises(ValueError):
            open_streaming_client(account="sf-cogsops-snowhouse", user="service",
                                  role="streaming_role", private_key_path="/not/a/key.p8",
                                  client_name="refund_test")

    def test_admin_role_refuses_to_open_client(self):
        with self.assertRaises(ValueError):
            open_streaming_client(account="myorg-my_account", user="service",
                                  role="ACCOUNTADMIN", private_key_path="/not/a/key.p8",
                                  client_name="refund_test")


if __name__ == "__main__":
    unittest.main()