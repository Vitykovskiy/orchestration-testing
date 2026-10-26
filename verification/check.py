"""Run public acceptance checks without importing the candidate application.

The controller should execute its pristine copy of this file. This checker is
not a security sandbox: candidate commands execute Python with the caller's
operating-system permissions. Every criterion gets a fresh temporary database.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
from typing import Any, Callable


COMMAND_TIMEOUT_SECONDS = 10
STOCK_FIELDS = ("warehouse", "sku", "on_hand", "reserved", "available")
RESERVATION_FIELDS = ("id", "warehouse", "sku", "qty", "status")
MOVEMENT_FIELDS = ("id", "reservation_id", "warehouse", "sku", "delta", "kind")


class CheckFailure(Exception):
    def __init__(self, message: str, *, critical: bool = False):
        super().__init__(message)
        self.critical = critical


def require(condition: bool, message: str, *, critical: bool = False) -> None:
    if not condition:
        raise CheckFailure(message, critical=critical)


def equal(actual: Any, expected: Any, label: str, *, critical: bool = False) -> None:
    require(
        actual == expected,
        f"{label}: expected {str(expected)[:250]!r}, got {str(actual)[:250]!r}",
        critical=critical,
    )


class Scenario:
    def __init__(self, workspace: Path, database: Path):
        self.workspace = workspace
        self.database = database

    def command(self, *arguments: object) -> subprocess.CompletedProcess[str]:
        environment = {
            "PATH": os.defpath,
            "PYTHONIOENCODING": "utf-8",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONHASHSEED": "0",
        }
        for name in ("SYSTEMROOT", "WINDIR", "TMP", "TEMP"):
            if name in os.environ:
                environment[name] = os.environ[name]
        command = [
            sys.executable, "-s", "-m", "stockctl", "--db", str(self.database),
            *(str(argument) for argument in arguments),
        ]
        try:
            return subprocess.run(
                command, cwd=self.workspace, env=environment,
                capture_output=True, text=True, encoding="utf-8",
                timeout=COMMAND_TIMEOUT_SECONDS, check=False,
            )
        except subprocess.TimeoutExpired as error:
            raise CheckFailure(f"command timed out after {COMMAND_TIMEOUT_SECONDS}s: {arguments}") from error
        except (OSError, UnicodeError) as error:
            raise CheckFailure(f"command could not be executed or decoded: {type(error).__name__}") from error

    def successful(self, *arguments: object) -> str:
        result = self.command(*arguments)
        self.require_success(result, arguments)
        return result.stdout

    @staticmethod
    def require_success(result: subprocess.CompletedProcess[str], arguments: object) -> None:
        require(
            result.returncode == 0,
            f"{arguments}: exit {result.returncode}; stderr: {result.stderr.strip()[:350]}",
        )

    def json(self, *arguments: object) -> Any:
        output = self.successful(*arguments)
        try:
            return json.loads(output)
        except json.JSONDecodeError as error:
            raise CheckFailure(f"{arguments}: stdout is not one JSON value") from error

    def add(self, warehouse: str, sku: str, quantity: int) -> None:
        self.json("stock", "add", "--warehouse", warehouse, "--sku", sku, "--qty", quantity)

    def reserve(self, warehouse: str, sku: str, quantity: int, key: str | None = None) -> dict:
        arguments: list[object] = ["reserve", "--warehouse", warehouse, "--sku", sku, "--qty", quantity]
        if key is not None:
            arguments.extend(("--request-id", key))
        value = self.reservation_response(*arguments)
        equal(
            (value["warehouse"], value["sku"], value["qty"]),
            (warehouse, sku, quantity), "reservation response parameters",
        )
        return value

    def reservation_response(self, *arguments: object) -> dict:
        value = self.json(*arguments)
        require(isinstance(value, dict), "reservation response must be an object")
        require(set(RESERVATION_FIELDS).issubset(value), "reservation response is missing existing fields")
        require(type(value["id"]) is int and value["id"] > 0, "reservation id must be a positive integer")
        return value

    def rows(self, query: str, parameters: tuple = ()) -> list[tuple]:
        require(self.database.is_file(), "database file was not created")
        with sqlite3.connect(self.database.as_uri() + "?mode=ro", uri=True) as connection:
            return connection.execute(query, parameters).fetchall()

    def snapshot(self) -> tuple[str, ...]:
        require(self.database.is_file(), "database file was not created")
        with sqlite3.connect(self.database.as_uri() + "?mode=ro", uri=True) as connection:
            return tuple(connection.iterdump())

    def stock(self, warehouse: str, sku: str) -> tuple[int, int]:
        rows = self.rows(
            "SELECT on_hand, reserved FROM stock WHERE warehouse = ? AND sku = ?",
            (warehouse, sku),
        )
        require(len(rows) == 1, f"expected one stock row for {warehouse!r}/{sku!r}")
        return rows[0]

    def reject_without_change(self, *arguments: object) -> None:
        before = self.snapshot()
        result = self.command(*arguments)
        require(self.snapshot() == before, "rejected command changed the database", critical=True)
        equal(result.returncode, 2, "domain error exit code")
        equal(result.stdout.strip(), "", "domain error stdout")
        try:
            error = json.loads(result.stderr)
        except json.JSONDecodeError as cause:
            raise CheckFailure("domain error stderr must be one JSON object") from cause
        require(isinstance(error, dict) and isinstance(error.get("error"), str) and bool(error["error"]),
                "domain error must contain a nonempty error string")

    def read_without_change(self, *arguments: object) -> str:
        before = self.snapshot()
        result = self.command(*arguments)
        require(self.snapshot() == before, "read command changed the database", critical=True)
        self.require_success(result, arguments)
        return result.stdout


def stock_record(warehouse: str, sku: str, on_hand: int, reserved: int) -> dict:
    return dict(zip(STOCK_FIELDS, (warehouse, sku, on_hand, reserved, on_hand - reserved)))


def warehouse_isolation(s: Scenario) -> None:
    s.add("W1", "SKU-A", 10)
    s.add("W2", "SKU-A", 40)
    s.add("W2", "SKU-Z", 7)
    s.reserve("W1", "SKU-A", 3)
    s.reserve("W2", "SKU-A", 5)
    output = s.json("stock", "list", "--warehouse", "W2", "--format", "json")
    equal(output, [stock_record("W2", "SKU-A", 40, 5), stock_record("W2", "SKU-Z", 7, 0)],
          "warehouse W2 stock")
    equal(s.json("stock", "list", "--warehouse", "missing", "--format", "json"), [],
          "unknown warehouse stock")
    equal(s.json("stock", "list", "--warehouse", "W1", "--format", "json"),
          [stock_record("W1", "SKU-A", 10, 3)], "warehouse W1 stock")
    header = "Склад\tSKU\tНа складе\tЗарезервировано\tДоступно"
    equal(s.successful("stock", "list", "--warehouse", "W2", "--format", "text").splitlines(),
          [header, "W2\tSKU-A\t40\t5\t35", "W2\tSKU-Z\t7\t0\t7"], "text stock filtering")
    equal(s.successful("stock", "list", "--warehouse", "missing", "--format", "text").splitlines(),
          [header], "unknown warehouse text output")


def cancellation_replay(s: Scenario) -> None:
    s.add("W1", "SKU-A", 10)
    s.add("W2", "SKU-A", 20)
    first = s.reserve("W1", "SKU-A", 2)
    other = s.reserve("W1", "SKU-A", 3)
    s.reserve("W2", "SKU-A", 4)
    response = s.reservation_response("cancel", first["id"])
    equal(response, {**first, "status": "cancelled"}, "first cancellation response")
    equal(s.stock("W1", "SKU-A"), (10, 3), "first cancellation balance")
    before = s.snapshot()
    equal(s.reservation_response("cancel", first["id"]), response, "replayed cancellation response")
    require(s.snapshot() == before, "repeated cancellation changed the database", critical=True)
    equal(s.stock("W2", "SKU-A"), (20, 4), "other warehouse balance", critical=True)
    equal(s.rows("SELECT status FROM reservations WHERE id = ?", (other["id"],)),
          [("active",)], "other reservation status", critical=True)
    equal(s.rows("SELECT COUNT(*) FROM movements WHERE reservation_id = ? AND kind = 'cancel'",
                 (first["id"],)), [(1,)], "cancellation movement count")


def rejected_cancellations(s: Scenario) -> None:
    s.add("W1", "SKU-A", 10)
    reservation = s.reserve("W1", "SKU-A", 2)
    shipped = s.reservation_response("ship", reservation["id"])
    equal(shipped, {**reservation, "status": "shipped"}, "shipment response")
    equal(s.stock("W1", "SKU-A"), (8, 0), "shipment stock balance")
    s.reject_without_change("cancel", reservation["id"])
    s.reject_without_change("cancel", 999999)


def stock_export(s: Scenario) -> None:
    equal(json.loads(s.read_without_change("export", "stock", "--format", "json")), [], "empty stock export")
    s.add("W2", "SKU-Z", 9)
    s.add("W1", "SKU-B", 12)
    s.add("W1", "SKU-A", 7)
    s.add("Склад", 'SKU-"Я"\nsecond', 6)
    s.reserve("W1", "SKU-B", 4)
    output = s.read_without_change("export", "stock", "--format", "json")
    try:
        value = json.loads(output)
    except json.JSONDecodeError as error:
        raise CheckFailure("stock exporter must produce JSON") from error
    equal(value, [stock_record("W1", "SKU-A", 7, 0), stock_record("W1", "SKU-B", 12, 4),
                  stock_record("W2", "SKU-Z", 9, 0), stock_record("Склад", 'SKU-"Я"\nsecond', 6, 0)],
          "stock export fields, values and ordering")
    equal(s.read_without_change("export", "stock", "--format", "json"), output,
          "stock export stability")


def movement_export(s: Scenario) -> None:
    empty = s.read_without_change("export", "movements", "--format", "csv")
    equal(list(csv.reader(io.StringIO(empty, newline=""))), [list(MOVEMENT_FIELDS)], "empty CSV header")
    warehouse, sku = 'Склад,"А"', 'SKU, "quoted"\nnext line'
    s.add(warehouse, sku, 20)
    first = s.reserve(warehouse, sku, 3)
    s.reservation_response("cancel", first["id"])
    second = s.reserve(warehouse, sku, 2)
    s.reservation_response("ship", second["id"])
    s.add("W2", "SKU-A", 1)
    output = s.read_without_change("export", "movements", "--format", "csv")
    require(output.endswith("\n"), "CSV output must end with a newline")
    actual = list(csv.reader(io.StringIO(output, newline="")))
    rows = s.rows("SELECT id, reservation_id, warehouse, sku, delta, kind FROM movements ORDER BY id")
    expected = [list(MOVEMENT_FIELDS), *[["" if value is None else str(value) for value in row] for row in rows]]
    equal(actual, expected, "CSV fields, escaping, values and ordering")
    equal(s.read_without_change("export", "movements", "--format", "csv"), output,
          "CSV export stability")


def persistent_request_replay(s: Scenario) -> None:
    s.add("W1", "SKU-A", 10)
    first = s.reserve("W1", "SKU-A", 4, "order-1")
    equal(first["status"], "active", "new reservation status")
    before = s.snapshot()
    equal(s.reserve("W1", "SKU-A", 4, "order-1"), first, "request replay after process restart")
    require(s.snapshot() == before, "request replay changed the database", critical=True)
    equal(s.stock("W1", "SKU-A"), (10, 4), "reservation charged only once", critical=True)
    equal(s.rows("SELECT COUNT(*) FROM reservations"), [(1,)], "reservation count")
    equal(s.rows("SELECT COUNT(*) FROM movements WHERE kind = 'reserve'"), [(1,)], "reserve movement count")


def conflicting_request_replay(s: Scenario) -> None:
    s.add("W1", "SKU-A", 10)
    s.add("W1", "SKU-B", 10)
    s.add("W2", "SKU-A", 10)
    s.reserve("W1", "SKU-A", 2, "same-key")
    for warehouse, sku, quantity in (("W1", "SKU-A", 3), ("W1", "SKU-B", 2), ("W2", "SKU-A", 2)):
        s.reject_without_change("reserve", "--warehouse", warehouse, "--sku", sku,
                                "--qty", quantity, "--request-id", "same-key")


def terminal_request_replay(s: Scenario) -> None:
    s.add("W1", "SKU-A", 12)
    for key, terminal_command, status in (("cancel-key", "cancel", "cancelled"),
                                          ("ship-key", "ship", "shipped")):
        original = s.reserve("W1", "SKU-A", 2, key)
        terminal = s.reservation_response(terminal_command, original["id"])
        equal(terminal["status"], status, "terminal reservation status")
        before = s.snapshot()
        equal(s.reserve("W1", "SKU-A", 2, key), terminal, "terminal request replay response")
        require(s.snapshot() == before, "terminal request replay changed the database", critical=True)


def keyless_compatibility(s: Scenario) -> None:
    s.add("W1", "SKU-A", 10)
    first = s.reserve("W1", "SKU-A", 2)
    second = s.reserve("W1", "SKU-A", 2)
    require(first["id"] != second["id"], "keyless reservations must have different ids")
    equal(s.stock("W1", "SKU-A"), (10, 4), "keyless reservations charge separately")
    equal(s.rows("SELECT COUNT(*) FROM movements WHERE kind = 'reserve'"), [(2,)],
          "keyless reserve movements")


def request_key_validation(s: Scenario) -> None:
    s.add("W1", "SKU-A", 3)
    s.reserve("W1", "SKU-A", 1, "warmup")
    s.reject_without_change("reserve", "--warehouse", "W1", "--sku", "SKU-A",
                            "--qty", 1, "--request-id", "")
    s.reject_without_change("reserve", "--warehouse", "W1", "--sku", "SKU-A",
                            "--qty", 5, "--request-id", "retry-after-add")
    s.add("W1", "SKU-A", 4)
    result = s.reserve("W1", "SKU-A", 5, "retry-after-add")
    equal(result["status"], "active", "failed request key remains reusable")
    equal(s.stock("W1", "SKU-A"), (7, 6), "retry after adding stock")
    equal(s.rows("SELECT COUNT(*) FROM reservations"), [(2,)], "failed request created no reservation")


def shipment_compatibility(s: Scenario) -> None:
    s.add("W1", "SKU-A", 10)
    first = s.reserve("W1", "SKU-A", 2)
    shipped = s.reservation_response("ship", first["id"])
    before = s.snapshot()
    equal(s.reservation_response("ship", first["id"]), shipped, "repeat shipment response")
    require(s.snapshot() == before, "repeat shipment changed the database", critical=True)
    second = s.reserve("W1", "SKU-A", 3)
    s.reservation_response("cancel", second["id"])
    s.reject_without_change("ship", second["id"])
    s.reject_without_change("ship", 999999)
    equal(s.stock("W1", "SKU-A"), (8, 0), "shipment compatibility stock balance", critical=True)


LEGACY_SCHEMA = """
CREATE TABLE stock (warehouse TEXT NOT NULL, sku TEXT NOT NULL, on_hand INTEGER NOT NULL,
                    reserved INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (warehouse, sku));
CREATE TABLE reservations (id INTEGER PRIMARY KEY AUTOINCREMENT, warehouse TEXT NOT NULL,
                           sku TEXT NOT NULL, qty INTEGER NOT NULL, status TEXT NOT NULL,
                           FOREIGN KEY (warehouse, sku) REFERENCES stock (warehouse, sku));
CREATE TABLE movements (id INTEGER PRIMARY KEY AUTOINCREMENT, reservation_id INTEGER,
                       warehouse TEXT NOT NULL, sku TEXT NOT NULL, delta INTEGER NOT NULL,
                       kind TEXT NOT NULL,
                       FOREIGN KEY (reservation_id) REFERENCES reservations (id));
INSERT INTO stock VALUES ('W1', 'legacy-sku', 15, 3);
INSERT INTO reservations VALUES (41, 'W1', 'legacy-sku', 3, 'active');
INSERT INTO movements VALUES (91, NULL, 'W1', 'legacy-sku', 15, 'add');
INSERT INTO movements VALUES (92, 41, 'W1', 'legacy-sku', 0, 'reserve');
"""


def legacy_migration(s: Scenario) -> None:
    with sqlite3.connect(s.database) as connection:
        connection.executescript(LEGACY_SCHEMA)
    first = s.reserve("W1", "legacy-sku", 2, "migrated-key")
    require(first["id"] > 41, "migration must preserve the reservation sequence")
    equal(s.rows("SELECT id, warehouse, sku, qty, status FROM reservations WHERE id = 41"),
          [(41, "W1", "legacy-sku", 3, "active")], "legacy reservation preserved", critical=True)
    equal(s.rows("SELECT id, reservation_id, warehouse, sku, delta, kind FROM movements WHERE id <= 92 ORDER BY id"),
          [(91, None, "W1", "legacy-sku", 15, "add"), (92, 41, "W1", "legacy-sku", 0, "reserve")],
          "legacy movements preserved", critical=True)
    equal(s.stock("W1", "legacy-sku"), (15, 5), "legacy stock preserved", critical=True)
    before = s.snapshot()
    equal(s.reserve("W1", "legacy-sku", 2, "migrated-key"), first, "migrated request replay")
    require(s.snapshot() == before, "migrated request replay changed the database", critical=True)
    s.json("init")
    require(s.snapshot() == before, "repeated init changed existing data", critical=True)
    cancelled = s.reservation_response("cancel", 41)
    equal(cancelled["status"], "cancelled", "legacy reservation remains cancellable")
    equal(s.stock("W1", "legacy-sku"), (15, 2), "legacy cancellation stock balance", critical=True)


def invalid_reservations(s: Scenario) -> None:
    s.add("W1", "SKU-A", 10)
    s.reserve("W1", "SKU-A", 3)
    for sku, quantity in (("SKU-A", 0), ("SKU-A", -1), ("SKU-A", 8), ("missing-sku", 1)):
        s.reject_without_change("reserve", "--warehouse", "W1", "--sku", sku, "--qty", quantity)


CRITERIA: tuple[tuple[str, str, Callable[[Scenario], None], bool], ...] = (
    ("warehouse_isolation", "Stock queries isolate warehouses and preserve sorting", warehouse_isolation, True),
    ("cancellation_replay", "Cancellation replay does not change stock or movements", cancellation_replay, True),
    ("rejected_cancellations", "Shipped and unknown reservations cannot be cancelled", rejected_cancellations, True),
    ("stock_export", "JSON stock export is complete, ordered, stable and read-only", stock_export, True),
    ("movement_export", "CSV movement export escapes data, orders rows and is read-only", movement_export, True),
    ("persistent_request_replay", "Request keys survive process restarts without duplicate writes", persistent_request_replay, True),
    ("conflicting_request_replay", "Request key conflicts reject every changed parameter without writes", conflicting_request_replay, True),
    ("terminal_request_replay", "Request replay returns current terminal status without writes", terminal_request_replay, True),
    ("keyless_compatibility", "Calls without a request key still create independent reservations", keyless_compatibility, True),
    ("request_key_validation", "Empty keys fail and failed reservations do not consume keys", request_key_validation, True),
    ("shipment_compatibility", "Existing shipment replay and rejection rules are preserved", shipment_compatibility, True),
    ("legacy_migration", "Old databases migrate in place without losing data", legacy_migration, False),
    ("invalid_reservations", "Invalid reservations fail without changing data", invalid_reservations, True),
)


def verify(workspace: Path) -> dict[str, Any]:
    """Return deterministic named acceptance results for a candidate workspace."""
    workspace = workspace.resolve(strict=True)
    if not workspace.is_dir():
        raise ValueError("workspace must be a directory")
    results = []
    critical_failures = []
    for criterion_id, description, check, initialize in CRITERIA:
        result = {"id": criterion_id, "passed": False, "detail": description}
        with tempfile.TemporaryDirectory(prefix="stockctl-check-") as directory:
            scenario = Scenario(workspace, Path(directory) / "inventory.sqlite3")
            try:
                if initialize:
                    scenario.json("init")
                check(scenario)
                result["passed"] = True
            except CheckFailure as error:
                result["detail"] = str(error)
                if error.critical:
                    critical_failures.append(criterion_id)
            except (sqlite3.DatabaseError, json.JSONDecodeError) as error:
                result["detail"] = f"invalid database or output: {type(error).__name__}: {str(error)[:300]}"
        results.append(result)
    passed_count = sum(result["passed"] for result in results)
    return {
        "passed": passed_count == len(results),
        "criteria": results,
        "critical_failures": critical_failures,
        "aggregate": {"passed": passed_count, "total": len(results)},
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    arguments = parser.parse_args()
    result = verify(arguments.workspace)
    output = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if arguments.output:
        arguments.output.parent.mkdir(parents=True, exist_ok=True)
        arguments.output.write_text(output, encoding="utf-8")
    print(output, end="")
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
