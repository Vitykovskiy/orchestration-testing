"""Regression checks for the product behavior that already exists."""

import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class ExistingBehavior(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.db = Path(self.directory.name) / "stock.sqlite"
        self.command("init")

    def command(self, *args, expected=0):
        result = subprocess.run(
            [sys.executable, "-m", "stockctl", "--db", str(self.db), *args],
            cwd=ROOT,
            text=True,
            capture_output=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, expected, result.stderr)
        if expected:
            self.assertEqual(result.stdout, "")
            self.assertTrue(json.loads(result.stderr)["error"])
            return None
        return json.loads(result.stdout)

    def add(self, qty=10):
        return self.command("stock", "add", "--warehouse", "W1", "--sku", "A", "--qty", str(qty))

    def reserve(self, qty=2):
        return self.command("reserve", "--warehouse", "W1", "--sku", "A", "--qty", str(qty))

    def listing(self):
        return self.command("stock", "list", "--warehouse", "W1", "--format", "json")

    def test_empty_and_repeated_initialization(self):
        self.assertEqual(self.listing(), [])
        self.add()
        self.command("init")
        self.assertEqual(self.listing()[0]["on_hand"], 10)

    def test_addition_accumulates_stock(self):
        self.add(10)
        item = self.add(3)
        self.assertEqual(item, {"warehouse": "W1", "sku": "A", "on_hand": 13, "reserved": 0, "available": 13})

    def test_reserve_and_cancel_once(self):
        self.add()
        reservation = self.reserve()
        self.assertEqual(reservation["status"], "active")
        self.assertEqual(self.listing()[0]["available"], 8)
        cancelled = self.command("cancel", str(reservation["id"]))
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertEqual(self.listing()[0]["available"], 10)

    def test_ship_is_idempotent(self):
        self.add()
        reservation = self.reserve()
        first = self.command("ship", str(reservation["id"]))
        second = self.command("ship", str(reservation["id"]))
        self.assertEqual(first, second)
        self.assertEqual(first["status"], "shipped")
        self.assertEqual(self.listing()[0]["on_hand"], 8)
        self.assertEqual(self.listing()[0]["reserved"], 0)
        with sqlite3.connect(self.db) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM movements WHERE kind = 'ship'").fetchone()[0], 1)

    def test_failed_reservation_does_not_change_state(self):
        self.add(1)
        self.command("reserve", "--warehouse", "W1", "--sku", "A", "--qty", "2", expected=2)
        self.assertEqual(self.listing()[0]["available"], 1)
        with sqlite3.connect(self.db) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM reservations").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM movements").fetchone()[0], 1)

    def test_keyless_repeated_requests_create_distinct_reservations(self):
        self.add()
        first, second = self.reserve(), self.reserve()
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(self.listing()[0]["reserved"], 4)

    def test_unknown_and_incompatible_terminal_operations_fail(self):
        self.command("cancel", "999", expected=2)
        self.add()
        reservation = self.reserve()
        self.command("ship", str(reservation["id"]))
        self.command("cancel", str(reservation["id"]), expected=2)
        another = self.reserve()
        self.command("cancel", str(another["id"]))
        self.command("ship", str(another["id"]), expected=2)

    def test_non_positive_quantities_fail(self):
        self.command("stock", "add", "--warehouse", "W1", "--sku", "A", "--qty", "0", expected=2)
        self.add()
        self.command("reserve", "--warehouse", "W1", "--sku", "A", "--qty", "-1", expected=2)
        self.assertEqual(self.listing()[0]["available"], 10)


if __name__ == "__main__":
    unittest.main()
