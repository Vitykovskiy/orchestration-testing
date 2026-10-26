import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class ExistingReportTests(unittest.TestCase):
    def test_json_groups_repeated_positions_and_preserves_first_appearance(self):
        source = {"snapshot": "morning", "items": [
            {"warehouse": "W2", "sku": "B", "on_hand": 4, "reserved": 1},
            {"warehouse": "W1", "sku": "A", "on_hand": 10, "reserved": 2},
            {"warehouse": "W2", "sku": "B", "on_hand": 2, "reserved": 0},
        ]}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "snapshot.json"
            path.write_text(json.dumps(source), encoding="utf-8")
            result = subprocess.run([sys.executable, "-m", "shelfctl", "--input", str(path), "report"], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(result.stdout)
            self.assertEqual([row["warehouse"] for row in report["rows"]], ["W2", "W1"])
            self.assertEqual(report["totals"], {"on_hand": 16, "reserved": 3, "available": 13})

    def test_invalid_quantity_is_rejected(self):
        source = {"snapshot": "morning", "items": [
            {"warehouse": "W1", "sku": "A", "on_hand": True, "reserved": 0},
        ]}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "snapshot.json"
            path.write_text(json.dumps(source), encoding="utf-8")
            result = subprocess.run([sys.executable, "-m", "shelfctl", "--input", str(path), "report"], capture_output=True, text=True)
            self.assertEqual(result.returncode, 2)
            self.assertEqual(result.stdout, "")
            self.assertTrue(json.loads(result.stderr)["error"])


if __name__ == "__main__":
    unittest.main()
