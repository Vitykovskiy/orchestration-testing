"""Public acceptance checks for the deliberately narrow status-label task."""

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def command(workspace, *args):
    environment = {"PATH": os.defpath, "PYTHONIOENCODING": "utf-8", "PYTHONDONTWRITEBYTECODE": "1"}
    result = subprocess.run([sys.executable, "-s", "-m", "statusctl", *args], cwd=workspace,
                            env=environment, capture_output=True, timeout=10, check=False)
    return subprocess.CompletedProcess(result.args, result.returncode,
                                       result.stdout.decode("utf-8"), result.stderr.decode("utf-8"))


def verify(workspace):
    workspace = Path(workspace).resolve(strict=True)
    results = []

    def check(identifier, operation):
        try:
            operation()
            results.append({"id": identifier, "passed": True, "detail": "passed"})
        except (AssertionError, OSError, subprocess.TimeoutExpired) as error:
            results.append({"id": identifier, "passed": False, "detail": str(error)[:500] or "output mismatch"})

    def label():
        for available, reserved in ((7, 3), (0, 0), (123456, 9)):
            result = command(workspace, "--available", str(available), "--reserved", str(reserved))
            require(result.returncode == 0 and result.stderr == "", "text command failed")
            require(result.stdout == f"Доступно: {available}\nРезерв: {reserved}\n", "text label or values differ")

    def machine_output():
        for available, reserved in ((7, 3), (0, 0), (123456, 9)):
            result = command(workspace, "--available", str(available), "--reserved", str(reserved), "--format", "json")
            expected = f'{{"available":{available},"reserved":{reserved}}}\n'
            require(result.returncode == 0 and result.stderr == "", "JSON command failed")
            require(result.stdout == expected, "JSON byte contract changed")

    def errors():
        for name in ("--available", "--reserved"):
            result = command(workspace, name, "-1")
            require(result.returncode == 2 and not result.stdout and result.stderr, "negative-value rejection changed")

    check("text_label", label)
    check("json_unchanged", machine_output)
    check("errors_unchanged", errors)
    count = sum(result["passed"] for result in results)
    return {"passed": count == len(results), "criteria": results, "critical_failures": [],
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
