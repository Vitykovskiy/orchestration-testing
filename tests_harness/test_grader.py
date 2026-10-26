"""Exercise verifier boundaries with small real subprocesses."""

import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from verification.check import CheckFailure, Scenario


class VerifierBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="verifier-test-")
        self.addCleanup(self.directory.cleanup)
        self.workspace = Path(self.directory.name)
        package = self.workspace / "stockctl"
        package.mkdir()
        (package / "__init__.py").write_text("", encoding="utf-8")
        self.program = package / "__main__.py"
        self.database = self.workspace / "inventory.sqlite3"
        with sqlite3.connect(self.database) as connection:
            connection.execute("CREATE TABLE stock (id INTEGER PRIMARY KEY, quantity INTEGER)")
            connection.execute("INSERT INTO stock VALUES (1, 10)")
        self.scenario = Scenario(self.workspace, self.database)

    def write_program(self, source):
        self.program.write_text(source, encoding="utf-8")

    def test_application_runs_in_a_separate_process(self):
        self.write_program("import os, json\nprint(json.dumps({'pid': os.getpid()}))\n")
        result = self.scenario.json("init")
        self.assertNotEqual(result["pid"], os.getpid())

    def test_rejected_command_that_writes_is_critical(self):
        self.write_program(
            "import sqlite3, sys\n"
            "with sqlite3.connect(sys.argv[2]) as db:\n"
            "    db.execute('UPDATE stock SET quantity = 9')\n"
            "print('{\"error\": \"rejected\"}', file=sys.stderr)\n"
            "raise SystemExit(2)\n"
        )
        with self.assertRaises(CheckFailure) as failure:
            self.scenario.reject_without_change("reserve")
        self.assertTrue(failure.exception.critical)

    def test_rejected_command_without_writes_is_accepted(self):
        self.write_program(
            "import sys\n"
            "print('{\"error\": \"rejected\"}', file=sys.stderr)\n"
            "raise SystemExit(2)\n"
        )
        self.scenario.reject_without_change("reserve")

    def test_read_command_that_writes_is_critical(self):
        self.write_program(
            "import sqlite3, sys\n"
            "with sqlite3.connect(sys.argv[2]) as db:\n"
            "    db.execute('UPDATE stock SET quantity = 9')\n"
            "print('[]')\n"
        )
        with self.assertRaises(CheckFailure) as failure:
            self.scenario.read_without_change("export", "stock")
        self.assertTrue(failure.exception.critical)

    def test_command_timeout_becomes_failed_criterion(self):
        self.write_program("import time\ntime.sleep(30)\n")
        with patch("verification.check.COMMAND_TIMEOUT_SECONDS", 0.05):
            with self.assertRaisesRegex(CheckFailure, "timed out"):
                self.scenario.command("init")

    def test_invalid_json_does_not_pass(self):
        self.write_program("print('not json')\n")
        with self.assertRaisesRegex(CheckFailure, "not one JSON"):
            self.scenario.json("init")

    def test_command_preserves_output_newlines_before_contract_checks(self):
        self.write_program("import sys\nsys.stdout.buffer.write(b'[]\\r\\n')\n")
        self.assertEqual(self.scenario.command("init").stdout, "[]\r\n")

    def test_wrong_reservation_shape_is_a_check_failure(self):
        self.write_program("print('[]')\n")
        with self.assertRaisesRegex(CheckFailure, "must be an object"):
            self.scenario.reservation_response("cancel", 1)


if __name__ == "__main__":
    unittest.main()
