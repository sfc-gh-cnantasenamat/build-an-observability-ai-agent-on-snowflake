import importlib
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import refund_agent.config as config


class ConfigTests(unittest.TestCase):
    def reload(self, **env):
        with mock.patch.dict(os.environ, {k: v for k, v in env.items() if v is not None}):
            for name, value in env.items():
                if value is None:
                    os.environ.pop(name, None)
            return importlib.reload(config)

    def tearDown(self):
        importlib.reload(config)

    def test_account_is_required(self):
        cfg = self.reload(REFUND_LAB_ACCOUNT=None, REFUND_LAB_CONNECTION=None)
        self.assertEqual(cfg.CONNECTION, "default")
        with self.assertRaises(ValueError):
            cfg.expected_identity()
        with self.assertRaises(ValueError):
            cfg.account_host()

    def test_another_account_is_derived_consistently(self):
        cfg = self.reload(REFUND_LAB_ACCOUNT="myorg-my_account", REFUND_LAB_CONNECTION="mine",
                          REFUND_LAB_LANGFUSE_HOST="https://abc-myorg-my-account.snowflakecomputing.app/")
        self.assertEqual(cfg.expected_identity(), ("MYORG", "MY_ACCOUNT"))
        self.assertEqual(cfg.account_host(), "https://myorg-my-account.snowflakecomputing.com")
        self.assertEqual(cfg.streaming_account(), "myorg-my_account")
        self.assertEqual(cfg.LANGFUSE_HOST, "https://abc-myorg-my-account.snowflakecomputing.app")
        self.assertEqual(cfg.CONNECTION, "mine")

    def test_account_without_org_is_rejected(self):
        cfg = self.reload(REFUND_LAB_ACCOUNT="justanaccount")
        with self.assertRaises(ValueError):
            cfg.expected_identity()


if __name__ == "__main__":
    unittest.main()
