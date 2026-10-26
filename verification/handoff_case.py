"""Public final-stage acceptance for the handoff benchmark.

The external controller separately checks phase one before starting phase two:
a nonempty .codex/tmp/plan.md must exist and product files must be unchanged.
This program checks only the final product, never imports candidate modules,
and is not an operating-system security sandbox.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile


CSV_FIELDS = ("warehouse", "sku", "on_hand", "reserved", "available")
COMMAND_TIMEOUT_SECONDS = 10


class CheckFailure(Exception):
    def __init__(self, message: str, *, critical: bool = False):
        super().__init__(message)
        self.critical = critical


def require(condition: bool, message: str, *, critical: bool = False) -> None:
    if not condition:
        raise CheckFailure(message, critical=critical)


def equal(actual, expected, label: str) -> None:
    require(actual == expected, f"{label}: expected {str(expected)[:350]!r}, got {str(actual)[:350]!r}")


class Scenario:
    def __init__(self, workspace: Path, directory: Path):
        self.workspace = workspace
        self.path = directory / "snapshot.json"

    def command(self, source, format_: str, warehouse: str | None = None) -> subprocess.CompletedProcess[str]:
        self.path.write_text(json.dumps(source, ensure_ascii=False), encoding="utf-8")
        original = self.path.read_bytes()
        command = [sys.executable, "-s", "-m", "shelfctl", "--input", str(self.path), "report", "--format", format_]
        if warehouse is not None:
            command.extend(("--warehouse", warehouse))
        environment = {"PATH": os.defpath, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONIOENCODING": "utf-8", "PYTHONHASHSEED": "0"}
        for name in ("SYSTEMROOT", "WINDIR", "TMP", "TEMP"):
            if name in os.environ:
                environment[name] = os.environ[name]
        try:
            result = subprocess.run(command, cwd=self.workspace, env=environment, capture_output=True,
                                    timeout=COMMAND_TIMEOUT_SECONDS, check=False)
            # Preserve line endings: text=True would normalize CRLF before checks.
            result = subprocess.CompletedProcess(
                result.args, result.returncode,
                result.stdout.decode("utf-8"), result.stderr.decode("utf-8"),
            )
        except subprocess.TimeoutExpired as error:
            raise CheckFailure(f"report command exceeded {COMMAND_TIMEOUT_SECONDS}s") from error
        except (OSError, UnicodeError) as error:
            raise CheckFailure(f"command execution failed: {type(error).__name__}") from error
        try:
            unchanged = self.path.read_bytes() == original
        except OSError:
            unchanged = False
        require(unchanged, "report command changed or removed the input snapshot", critical=True)
        return result

    def output(self, source, format_: str, warehouse: str | None = None) -> str:
        result = self.command(source, format_, warehouse)
        equal(result.returncode, 0, f"{format_} exit code; stderr={result.stderr[:150]}")
        equal(result.stderr, "", f"{format_} stderr")
        return result.stdout

    def csv_rows(self, source, warehouse: str | None = None, *, require_lf_records: bool = False) -> list[list[str]]:
        output = self.output(source, "csv", warehouse)
        require(output.endswith("\n"), "CSV must end with LF")
        if require_lf_records:
            # Only enable this for a probe with no CR characters in its data.
            require("\r" not in output, "CSV record separators must use LF")
        try:
            rows = list(csv.reader(io.StringIO(output, newline=""), strict=True))
        except csv.Error as error:
            raise CheckFailure("invalid CSV escaping") from error
        require(bool(rows), "CSV header is missing")
        equal(rows[0], list(CSV_FIELDS), "updated CSV columns")
        require(all(len(row) == len(CSV_FIELDS) for row in rows), "CSV row has the wrong number of columns")
        return rows[1:]


def item(warehouse: str, sku: str, on_hand: int, reserved: int) -> dict:
    return {"warehouse": warehouse, "sku": sku, "on_hand": on_hand, "reserved": reserved}


def snapshot(*items: dict) -> dict:
    return {"snapshot": "morning-Отчёт", "items": list(items)}


def csv_columns_and_order(scenario: Scenario) -> None:
    data = snapshot(item("W2", "A", 7, 2), item("W1", "Z", 10, 4), item("W1", "A", 3, 0))
    equal(scenario.csv_rows(data, require_lf_records=True), [["W1", "A", "3", "0", "3"], ["W1", "Z", "10", "4", "6"], ["W2", "A", "7", "2", "5"]],
          "updated warehouse-then-SKU order and values")


def csv_filter(scenario: Scenario) -> None:
    data = snapshot(item("W2", "A", 7, 2), item("W1", "A", 3, 0), item("W2", "B", 10, 1))
    equal(scenario.csv_rows(data, "W2"), [["W2", "A", "7", "2", "5"], ["W2", "B", "10", "1", "9"]], "warehouse filter")
    equal(scenario.csv_rows(data, "missing"), [], "unknown warehouse")


def csv_aggregation(scenario: Scenario) -> None:
    data = snapshot(item("W2", "A", 7, 2), item("W1", "A", 3, 0), item("W2", "A", 5, 4))
    equal(scenario.csv_rows(data), [["W1", "A", "3", "0", "3"], ["W2", "A", "12", "6", "6"]], "duplicate position aggregation")


def csv_escaping(scenario: Scenario) -> None:
    warehouse = 'Склад,"Юг"\nзона 2'
    sku = 'Чай,"чёрный"\nупаковка'
    data = snapshot(item(warehouse, sku, 8, 3), item("W1", "simple", 1, 0))
    equal(scenario.csv_rows(data), [["W1", "simple", "1", "0", "1"], [warehouse, sku, "8", "3", "5"]], "CSV special-character roundtrip")


def csv_empty(scenario: Scenario) -> None:
    output = scenario.output(snapshot(), "csv")
    equal(output, "warehouse,sku,on_hand,reserved,available\n", "empty CSV is exactly the revised header")


def expected_report(data: dict, warehouse: str | None = None) -> dict:
    # This data function expresses the public contract without reading source.
    positions = {}
    for row in data["items"]:
        key = row["warehouse"], row["sku"]
        if key not in positions:
            positions[key] = item(key[0], key[1], 0, 0)
        positions[key]["on_hand"] += row["on_hand"]
        positions[key]["reserved"] += row["reserved"]
    rows = [dict(row, available=row["on_hand"] - row["reserved"]) for row in positions.values()
            if warehouse is None or row["warehouse"] == warehouse]
    return {"snapshot": data["snapshot"], "rows": rows,
            "totals": {key: sum(row[key] for row in rows) for key in ("on_hand", "reserved", "available")}}


def json_compatibility(scenario: Scenario) -> None:
    data = snapshot(item("W2", "Б", 7, 2), item("W1", "А", 3, 0), item("W2", "Б", 5, 1))
    for warehouse in (None, "W2", "missing"):
        expected = json.dumps(expected_report(data, warehouse), ensure_ascii=False, separators=(",", ":")) + "\n"
        equal(scenario.output(data, "json", warehouse), expected, "unchanged JSON bytes and original row order")


def text_compatibility(scenario: Scenario) -> None:
    data = snapshot(item("W2", "B", 7, 2), item("W1", "A", 3, 0), item("W2", "B", 5, 1))
    equal(scenario.output(data, "text"), "Склад\tSKU\tВ наличии\tРезерв\tДоступно\nW2\tB\t12\t3\t9\nW1\tA\t3\t0\t3\n", "unchanged text output")
    equal(scenario.output(data, "text", "missing"), "Склад\tSKU\tВ наличии\tРезерв\tДоступно\n", "empty text output")


def input_validation(scenario: Scenario) -> None:
    invalid = [snapshot(item("W1", "A", True, 0)), snapshot(item("W1", "A", 2, 3)), snapshot(item("", "A", 2, 0)), {"snapshot": "x", "items": "wrong"}]
    for source in invalid:
        for format_ in ("json", "csv"):
            result = scenario.command(source, format_)
            equal(result.returncode, 2, "invalid-input exit code")
            equal(result.stdout, "", "invalid-input stdout")
            try:
                error = json.loads(result.stderr)
            except json.JSONDecodeError as cause:
                raise CheckFailure("invalid-input stderr must be one JSON object") from cause
            require(isinstance(error, dict) and isinstance(error.get("error"), str) and bool(error["error"]), "missing domain error message")


CRITERIA = (
    ("updated_csv_columns_order", "Revised columns and warehouse/SKU order replace the earlier plan", csv_columns_and_order),
    ("csv_warehouse_filter", "Existing warehouse filtering also applies to CSV", csv_filter),
    ("csv_duplicate_aggregation", "Repeated positions aggregate before CSV rendering", csv_aggregation),
    ("csv_escaping", "CSV roundtrips Unicode, commas, quotes and embedded newlines", csv_escaping),
    ("csv_empty", "Empty CSV contains exactly the revised header", csv_empty),
    ("json_compatibility", "Existing JSON bytes, ordering and filtering are preserved", json_compatibility),
    ("text_compatibility", "Existing text output and ordering are preserved", text_compatibility),
    ("input_validation", "Input validation and read-only behavior are preserved", input_validation),
)


def verify(workspace: Path) -> dict:
    workspace = workspace.resolve(strict=True)
    if not workspace.is_dir():
        raise ValueError("workspace must be a directory")
    criteria = []
    critical = []
    for identifier, description, check in CRITERIA:
        result = {"id": identifier, "passed": False, "detail": description}
        with tempfile.TemporaryDirectory(prefix="handoff-check-") as directory:
            try:
                check(Scenario(workspace, Path(directory)))
                result["passed"] = True
            except CheckFailure as error:
                result["detail"] = str(error)
                if error.critical:
                    critical.append(identifier)
        criteria.append(result)
    passed = sum(item["passed"] for item in criteria)
    return {"passed": passed == len(criteria), "criteria": criteria, "critical_failures": critical,
            "aggregate": {"passed": passed, "total": len(criteria)}}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = verify(args.workspace)
    output = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(output, encoding="utf-8")
    print(output, end="")
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
