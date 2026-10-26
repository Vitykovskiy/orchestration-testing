"""Public subprocess acceptance for six independently implementable reports."""

import argparse
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile


REPORTS = ["latency", "reliability", "throughput", "incidents", "traffic", "audit"]
HEADER = "id,timestamp_utc,service,status,duration_ms,bytes,route\n"


class CheckFailure(Exception):
    def __init__(self, message, critical=False):
        super().__init__(message)
        self.critical = critical


def require(condition, message, critical=False):
    if not condition:
        raise CheckFailure(message, critical)


def equal(actual, expected):
    require(actual == expected, f"expected {str(expected)[:300]!r}, got {str(actual)[:300]!r}")


def event(identifier, service="api", timestamp="2026-01-01T00:00:00Z", status="ok", duration=10, size=10, route="/a"):
    return {"id": identifier, "service": service, "timestamp": timestamp, "status": status,
            "duration_ms": duration, "bytes": size, "route": route}


MAIN = [
    event("e01", duration=10, size=10, route="/a?x=1"),
    event("e02", timestamp="2026-01-01T00:00:01Z", status="error", duration=0, size=20, route="/a#part"),
    event("e03", timestamp="2026-01-01T00:00:10Z", status="error", duration=50, size=30, route='/quoted,"x"\n?x=1'),
    event("e04", timestamp="2026-01-01T00:01:00Z", duration=1000, size=40),
    event("e05", timestamp="2026-01-01T00:01:30Z", status="error", duration=2000, size=50, route="/b"),
    event("e06", timestamp="2026-01-01T00:01:30Z", status="error", duration=300, size=60, route="/a?x=2"),
    event("e07", "db", "2026-01-01T03:00:00+03:00", "error", 1, 5, "/db"),
    event("e08", "db", "2026-01-01T00:00:01Z", "ok", 20, 5, "/a"),
    event("e09", "db", "2026-01-01T00:00:02Z", "ok", 30, 5, "/b"),
    event("e10", "db", "2026-01-01T00:02:00Z", "ok", 40, 5, "/a"),
    event("e11", "z", "2026-01-01T00:59:00+01:00", "error", 5, 0, "/down"),
    event("e12", timestamp="2026-01-01T00:02:00Z", duration=2000, size=70, route="/unicode/ёж"),
]

EXPECTED = {
    "latency": [
        {"service": "api", "count": 3, "min_ms": 10, "max_ms": 2000, "p50_ms": 1000, "p95_ms": 2000, "slow_requests": 2},
        {"service": "db", "count": 3, "min_ms": 20, "max_ms": 40, "p50_ms": 30, "p95_ms": 40, "slow_requests": 0},
        {"service": "z", "count": 0, "min_ms": None, "max_ms": None, "p50_ms": None, "p95_ms": None, "slow_requests": 0},
    ],
    "reliability": [
        {"service": "api", "total": 7, "ok": 3, "error": 4, "availability_bp": 4285, "longest_error_streak": 2},
        {"service": "db", "total": 4, "ok": 3, "error": 1, "availability_bp": 7500, "longest_error_streak": 1},
        {"service": "z", "total": 1, "ok": 0, "error": 1, "availability_bp": 0, "longest_error_streak": 1},
    ],
    "throughput": [
        {"minute": "2025-12-31T23:59:00Z", "service": "z", "requests": 1, "bytes": 0},
        {"minute": "2026-01-01T00:00:00Z", "service": "api", "requests": 3, "bytes": 60},
        {"minute": "2026-01-01T00:00:00Z", "service": "db", "requests": 3, "bytes": 15},
        {"minute": "2026-01-01T00:01:00Z", "service": "api", "requests": 3, "bytes": 150},
        {"minute": "2026-01-01T00:02:00Z", "service": "api", "requests": 1, "bytes": 70},
        {"minute": "2026-01-01T00:02:00Z", "service": "db", "requests": 1, "bytes": 5},
    ],
    "incidents": [
        {"service": "api", "start": "2026-01-01T00:00:01Z", "end": "2026-01-01T00:01:00Z", "error_count": 2, "duration_seconds": 59},
        {"service": "api", "start": "2026-01-01T00:01:30Z", "end": "2026-01-01T00:02:00Z", "error_count": 2, "duration_seconds": 30},
        {"service": "db", "start": "2026-01-01T00:00:00Z", "end": "2026-01-01T00:00:01Z", "error_count": 1, "duration_seconds": 1},
        {"service": "z", "start": "2025-12-31T23:59:00Z", "end": None, "error_count": 1, "duration_seconds": None},
    ],
    "traffic": [
        {"route": "/a", "requests": 6, "bytes": 140, "services": ["api", "db"], "max_duration_ms": 1000},
        {"route": "/unicode/ёж", "requests": 1, "bytes": 70, "services": ["api"], "max_duration_ms": 2000},
        {"route": "/b", "requests": 2, "bytes": 55, "services": ["api", "db"], "max_duration_ms": 2000},
        {"route": '/quoted,"x"\n', "requests": 1, "bytes": 30, "services": ["api"], "max_duration_ms": 50},
        {"route": "/db", "requests": 1, "bytes": 5, "services": ["db"], "max_duration_ms": 1},
        {"route": "/down", "requests": 1, "bytes": 0, "services": ["z"], "max_duration_ms": 5},
    ],
}


def expected_audit():
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, lineterminator="\n")
    writer.writerow(HEADER.strip().split(","))
    normalized = {row["id"]: row["timestamp"] for row in MAIN}
    normalized.update(e07="2026-01-01T00:00:00Z", e11="2025-12-31T23:59:00Z")
    for row in sorted(MAIN, key=lambda item: (normalized[item["id"]], item["id"])):
        writer.writerow([row["id"], normalized[row["id"]], row["service"], row["status"], row["duration_ms"], row["bytes"], row["route"]])
    return stream.getvalue()


EXPECTED["audit"] = expected_audit()


def invoke(workspace, payload, *arguments):
    with tempfile.TemporaryDirectory(prefix="opsreport-input-") as directory:
        path = Path(directory) / "events.json"
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        original = hashlib.sha256(path.read_bytes()).digest()
        environment = {"PATH": os.defpath, "PYTHONIOENCODING": "utf-8", "PYTHONDONTWRITEBYTECODE": "1"}
        for name in ("SYSTEMROOT", "WINDIR", "TMP", "TEMP"):
            if name in os.environ:
                environment[name] = os.environ[name]
        try:
            result = subprocess.run([sys.executable, "-s", "-m", "opsreport", "--input", str(path), *arguments],
                                    cwd=workspace, env=environment, capture_output=True, timeout=10, check=False)
            result = subprocess.CompletedProcess(result.args, result.returncode,
                                                 result.stdout.decode("utf-8"), result.stderr.decode("utf-8"))
        except subprocess.TimeoutExpired as error:
            raise CheckFailure("candidate command timed out") from error
        require(path.is_file() and hashlib.sha256(path.read_bytes()).digest() == original,
                "candidate changed the input file", critical=True)
        return result


def output(workspace, rows, report):
    result = invoke(workspace, {"events": rows}, "report", report)
    require(result.returncode == 0 and result.stderr == "", f"{report}: exit {result.returncode}; {result.stderr[:300]}")
    if report == "audit":
        return result.stdout
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise CheckFailure(f"{report}: invalid JSON") from error


def registry(workspace):
    result = invoke(workspace, {"events": MAIN}, "reports")
    equal(result.returncode, 0)
    equal(json.loads(result.stdout), REPORTS)
    result = invoke(workspace, {"events": MAIN}, "check")
    equal(result.returncode, 0)
    equal(json.loads(result.stdout), {"events": 12, "services": 3})


def validation(workspace):
    invalid = [{}, {"events": [False]}, {"events": [event("same"), event("same")]}]
    for key, value in (("bytes", True), ("duration_ms", -1), ("status", "other"),
                       ("timestamp", "2026-01-01T00:00:00"), ("route", "relative")):
        row = event("bad")
        row[key] = value
        invalid.append({"events": [row]})
    for payload in invalid:
        result = invoke(workspace, payload, "check")
        equal(result.returncode, 2)
        equal(result.stdout, "")
        error = json.loads(result.stderr)
        require(isinstance(error, dict) and isinstance(error.get("error"), str) and bool(error["error"]), "missing JSON error")


def latency_rank(workspace):
    rows = [event(f"a{n:02d}", service="a", duration=n) for n in range(1, 21)]
    rows += [event("b1", service="b", duration=0), event("b2", service="b", duration=1000)]
    rows += [event("c1", service="c", status="error", duration=99999)]
    equal(output(workspace, rows[::-1], "latency"), [
        {"service": "a", "count": 20, "min_ms": 1, "max_ms": 20, "p50_ms": 10, "p95_ms": 19, "slow_requests": 0},
        {"service": "b", "count": 2, "min_ms": 0, "max_ms": 1000, "p50_ms": 0, "p95_ms": 1000, "slow_requests": 1},
        {"service": "c", "count": 0, "min_ms": None, "max_ms": None, "p50_ms": None, "p95_ms": None, "slow_requests": 0},
    ])


def reliability_ties(workspace):
    rows = [event("4", status="error"), event("2", status="ok"), event("1", status="error"),
            event("3", status="error"), event("other", service="b")]
    equal(output(workspace, rows, "reliability"), [
        {"service": "api", "total": 4, "ok": 1, "error": 3, "availability_bp": 2500, "longest_error_streak": 2},
        {"service": "b", "total": 1, "ok": 1, "error": 0, "availability_bp": 10000, "longest_error_streak": 0},
    ])


def throughput_boundaries(workspace):
    rows = [event("1", timestamp="2026-01-01T00:00:59Z", size=0),
            event("2", timestamp="2026-01-01T00:01:00Z", size=2),
            event("3", timestamp="2026-01-01T05:31:59+05:30", size=3)]
    equal(output(workspace, rows[::-1], "throughput"), [
        {"minute": "2026-01-01T00:00:00Z", "service": "api", "requests": 1, "bytes": 0},
        {"minute": "2026-01-01T00:01:00Z", "service": "api", "requests": 2, "bytes": 5},
    ])


def incident_ties(workspace):
    rows = [event("3", status="error"), event("1", status="error"), event("2"), event("other", service="b")]
    equal(output(workspace, rows, "incidents"), [
        {"service": "api", "start": "2026-01-01T00:00:00Z", "end": "2026-01-01T00:00:00Z", "error_count": 1, "duration_seconds": 0},
        {"service": "api", "start": "2026-01-01T00:00:00Z", "end": None, "error_count": 1, "duration_seconds": None},
    ])


def traffic_order(workspace):
    rows = [event("1", route="/c", size=10), event("2", route="/a?query#fragment", size=4),
            event("3", route="/b", size=10), event("4", service="b", route="/a#fragment?query", size=6)]
    equal(output(workspace, rows, "traffic"), [
        {"route": "/a", "requests": 2, "bytes": 10, "services": ["api", "b"], "max_duration_ms": 10},
        {"route": "/b", "requests": 1, "bytes": 10, "services": ["api"], "max_duration_ms": 10},
        {"route": "/c", "requests": 1, "bytes": 10, "services": ["api"], "max_duration_ms": 10},
    ])


def empty_reports(workspace):
    for report in REPORTS:
        equal(output(workspace, [], report), HEADER if report == "audit" else [])


def order_invariance(workspace):
    forward = output(workspace, MAIN, "all")
    reverse = output(workspace, MAIN[::-1], "all")
    equal(forward, EXPECTED)
    equal(reverse, forward)
    equal(output(workspace, MAIN, "all"), forward)


CRITERIA = [
    ("registry_and_check", registry), ("input_validation", validation),
    ("latency_values", lambda w: equal(output(w, MAIN[::-1], "latency"), EXPECTED["latency"])),
    ("latency_nearest_rank", latency_rank),
    ("reliability_values", lambda w: equal(output(w, MAIN[::-1], "reliability"), EXPECTED["reliability"])),
    ("reliability_event_order", reliability_ties),
    ("throughput_values", lambda w: equal(output(w, MAIN[::-1], "throughput"), EXPECTED["throughput"])),
    ("throughput_window_boundaries", throughput_boundaries),
    ("incident_intervals", lambda w: equal(output(w, MAIN[::-1], "incidents"), EXPECTED["incidents"])),
    ("incident_timestamp_ties", incident_ties),
    ("traffic_values", lambda w: equal(output(w, MAIN[::-1], "traffic"), EXPECTED["traffic"])),
    ("traffic_grouping_and_order", traffic_order),
    ("audit_csv", lambda w: equal(output(w, MAIN[::-1], "audit"), EXPECTED["audit"])),
    ("audit_empty", lambda w: equal(output(w, [], "audit"), HEADER)),
    ("all_reports_integration", lambda w: equal(output(w, MAIN[::-1], "all"), EXPECTED)),
    ("empty_reports", empty_reports), ("input_order_and_repeatability", order_invariance),
]


def verify(workspace):
    workspace = Path(workspace).resolve(strict=True)
    results, critical = [], []
    for identifier, check in CRITERIA:
        item = {"id": identifier, "passed": False, "detail": "passed"}
        try:
            check(workspace)
            item["passed"] = True
        except CheckFailure as error:
            item["detail"] = str(error)[:600]
            if error.critical:
                critical.append(identifier)
        except (OSError, ValueError, UnicodeError) as error:
            item["detail"] = f"{type(error).__name__}: {str(error)[:400]}"
        results.append(item)
    count = sum(item["passed"] for item in results)
    return {"passed": count == len(results), "criteria": results, "critical_failures": critical,
            "aggregate": {"passed": count, "total": len(results)}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = verify(args.workspace)
    text = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
    print(text, end="")
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
