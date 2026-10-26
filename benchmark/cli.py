"""Small public command interface for agents and benchmark maintainers."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from .compare import compare, load_reports, render_comparison
from .runs import finish, grade, run_external, start, write_json
from .workspace import commit_id


def positive_integer(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return number


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Repeatable stockctl agent benchmark")
    commands = result.add_subparsers(dest="command", required=True)
    begin = commands.add_parser("start", help="Start one clean manual attempt before editing")
    begin.add_argument("--label", default="current")
    begin.add_argument("--model")
    begin.add_argument("--effort")
    begin.add_argument("--toolkit-ref")
    begin.add_argument("--environment-label")
    commands.add_parser("finish", help="Verify and write REPORT.md, report.json and changes.patch")
    execute = commands.add_parser("run", help="Run Codex in a new workspace with a pinned Toolkit")
    execute.add_argument("--toolkit", type=Path, required=True)
    execute.add_argument("--toolkit-ref", required=True)
    execute.add_argument("--label", required=True)
    execute.add_argument("--model", required=True)
    execute.add_argument("--effort", choices=["minimal", "low", "medium", "high", "xhigh", "max", "ultra"], default="medium")
    execute.add_argument("--output", type=Path, required=True)
    execute.add_argument("--timeout", type=positive_integer, default=1800)
    execute.add_argument("--environment-label", required=True, help="Same declared clean machine/configuration label for both variants")
    check = commands.add_parser("verify", help="Recheck a solution using this controller's committed verifier")
    check.add_argument("--workspace", type=Path, required=True)
    check.add_argument("--output", type=Path, required=True)
    comparison = commands.add_parser("compare", help="Compare all reports from two frozen variants")
    comparison.add_argument("--baseline", nargs="+", type=Path, required=True)
    comparison.add_argument("--candidate", nargs="+", type=Path, required=True)
    comparison.add_argument("--output", type=Path, default=Path(".benchmark/comparison"))
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    repository = Path(__file__).resolve().parents[1]
    try:
        if args.command == "start":
            result = start(repository, label=args.label, model=args.model, effort=args.effort,
                           toolkit_ref=args.toolkit_ref, environment_label=args.environment_label)
            print(f"Started {result['run_id']}. Implement TASK.md, then run python3 bench.py finish.")
            return 0
        if args.command == "finish":
            result = finish(repository)
            print(Path(result["directory"], "REPORT.md").read_text(encoding="utf-8"))
            print("Artifacts:", result["directory"])
            return 0 if result["report"]["accepted"] else 1
        if args.command == "run":
            result = run_external(
                repository, toolkit=args.toolkit, toolkit_ref=args.toolkit_ref,
                label=args.label, model=args.model, effort=args.effort,
                output=args.output.resolve(), timeout=args.timeout,
                environment_label=args.environment_label,
            )
            print(Path(result["directory"], "REPORT.md").read_text(encoding="utf-8"))
            print("Artifacts:", result["directory"])
            return 0 if result["report"]["accepted"] else 1
        if args.command == "verify":
            result = grade(args.workspace.resolve(), repository, commit_id(repository))
            if args.output.exists():
                raise ValueError("Refusing to overwrite an existing verification result")
            write_json(args.output, result)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result["passed"] else 1
        if args.command == "compare":
            result = compare(load_reports(args.baseline), load_reports(args.candidate))
            if args.output.exists():
                raise ValueError("Comparison output must be a new directory")
            args.output.mkdir(parents=True)
            write_json(args.output / "comparison.json", result)
            (args.output / "COMPARISON.md").write_text(render_comparison(result), encoding="utf-8")
            print(render_comparison(result))
            return 0 if result["gate_passed"] else 1
    except (OSError, ValueError, KeyError) as error:
        print(f"Benchmark error: {error}", file=sys.stderr)
        return 2
    return 2
