import filecmp
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SHARED = ("__init__.py", "config.py", "events.py", "agents.py", "lab.py", "observers.py")


class BundleTests(unittest.TestCase):
    def test_app_bundle_matches_shared_package(self):
        for name in SHARED:
            with self.subTest(file=name):
                self.assertTrue(filecmp.cmp(ROOT / "src/refund_agent" / name,
                                            ROOT / "refund_truth_sis/refund_agent" / name,
                                            shallow=False),
                                f"{name} differs; run scripts/sync_app_package.sh")

    def test_manifest_ships_every_shared_module(self):
        manifest = (ROOT / "refund_truth_sis/snowflake.yml").read_text()
        for name in SHARED:
            self.assertIn(f"refund_agent/{name}", manifest)


if __name__ == "__main__":
    unittest.main()
