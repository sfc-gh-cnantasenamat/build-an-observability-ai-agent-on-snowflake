import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from refund_agent.graph import CASES, run_case
from refund_agent.producer import persist_run, verify_connection


class ProducerTests(unittest.TestCase):
    def test_wrong_account_prevents_all_writes(self):
        cursor = MagicMock()
        cursor.fetchone.return_value = ("SFCOGSOPS", "SNOWHOUSE")
        with self.assertRaises(RuntimeError):
            verify_connection(cursor)
        self.assertEqual(cursor.execute.call_count, 1)

    def test_parameterized_run_and_event_writes(self):
        connection = MagicMock()
        cursor = connection.cursor.return_value
        cursor.fetchone.return_value = ("MYORG", "MY_ACCOUNT")
        cursor.rowcount = 1
        result = run_case(CASES[0], "baseline", run_id="real-case")
        self.assertEqual(persist_run(connection, result), 1)
        self.assertEqual(cursor.execute.call_count, 5)
        for call in cursor.execute.call_args_list[1:]:
            self.assertIsInstance(call.args[1], tuple)
        self.assertEqual([call.args[1][-2] for call in cursor.execute.call_args_list[2:]], [1, 2, 3])
        connection.commit.assert_called_once()


if __name__ == "__main__":
    unittest.main()