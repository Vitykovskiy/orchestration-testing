"""Small public command interface for agents and benchmark maintainers."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from .compare import compare, load_reports, render_comparison
from .runs import checkpoint, finish, followup_prompt, grade, run_external, start, write_json
from .workspace import changed_paths, commit_id, initialize_git, install_toolkit, prepare_case
from .catalog import CASES


def positive_integer(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return number


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Repeatable agent-development benchmark suite")
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser("cases", help="List benchmark cases and their purpose")
    prepare = commands.add_parser("prepare", help="Create a clean manual workspace for one case")
    prepare.add_argument("--case", choices=CASES, required=True)
    prepare.add_argument("--output", type=Path, required=True)
    prepare.add_argument("--toolkit", type=Path)
    prepare.add_argument("--toolkit-ref")
    begin = commands.add_parser("start", help="Start one clean manual attempt before editing")
    begin.add_argument("--label", default="current")
    begin.add_argument("--model")
    begin.add_argument("--effort")
    begin.add_argument("--toolkit-ref")
    begin.add_argument("--environment-label")
    begin.add_argument("--subagents", type=positive_integer, help="Declared current limit; start does not reconfigure an existing agent")
    commands.add_parser("finish", help="Verify and write REPORT.md, report.json and changes.patch")
    commands.add_parser("checkpoint", help="Capture handoff phase 1 and print the next-session instruction")
    execute = commands.add_parser("run", help="Run Codex in a new workspace with a pinned Toolkit")
    execute.add_argument("--toolkit", type=Path, required=True)
    execute.add_argument("--toolkit-ref", required=True)
    execute.add_argument("--label", required=True)
    execute.add_argument("--case", choices=CASES, default="workflow")
    execute.add_argument("--subagents", type=positive_integer, required=True, help="Maximum open subagent threads, excluding the primary")
    execute.add_argument("--model", required=True)
    execute.add_argument("--effort", choices=["minimal", "low", "medium", "high", "xhigh", "max", "ultra"], default="medium")
    execute.add_argument("--output", type=Path, required=True)
    execute.add_argument("--timeout", type=positive_integer, default=1800)
    execute.add_argument("--environment-label", required=True, help="Same declared clean machine/configuration label for both variants")
    check = commands.add_parser("verify", help="Recheck a solution using this controller's committed verifier")
    check.add_argument("--workspace", type=Path, required=True)
    check.add_argument("--output", type=Path, required=True)
    check.add_argument("--case", choices=CASES)
    comparison = commands.add_parser("compare", help="Compare all reports from two frozen variants")
    comparison.add_argument("--baseline", nargs="+", type=Path, required=True)
    comparison.add_argument("--candidate", nargs="+", type=Path, required=True)
    comparison.add_argument("--output", type=Path, default=Path(".benchmark/comparison"))
    comparison.add_argument("--study", choices=["toolkit", "capacity"], default="toolkit")
    matrix = commands.add_parser("matrix", help="Run a reproducible case × subagent-limit matrix")
    matrix.add_argument("--toolkit", type=Path, required=True)
    matrix.add_argument("--toolkit-ref", required=True)
    matrix.add_argument("--model", required=True)
    matrix.add_argument("--effort", choices=["minimal", "low", "medium", "high", "xhigh", "max", "ultra"], default="medium")
    matrix.add_argument("--environment-label", required=True)
    matrix.add_argument("--output", type=Path, required=True)
    matrix.add_argument("--cases", nargs="+", choices=CASES, default=list(CASES))
    matrix.add_argument("--subagents", nargs="+", type=positive_integer, default=[1, 2, 4])
    matrix.add_argument("--repeats", type=positive_integer, default=5)
    matrix.add_argument("--timeout", type=positive_integer, default=1800)
    matrix.add_argument("--seed", type=int, default=20261026)
    matrix.add_argument("--plan-only", action="store_true", help="Save order and budget without calling Codex")
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    repository = Path(__file__).resolve().parents[1]
    try:
        if args.command == "cases":
            for identifier, case in CASES.items():
                print(f"{identifier}: {case['title']} ({case['phases']} phase(s))")
            return 0
        if args.command == "prepare":
            if bool(args.toolkit) != bool(args.toolkit_ref):
                raise ValueError("Provide both --toolkit and --toolkit-ref, or neither")
            base = commit_id(repository)
            if changed_paths(repository, base):
                raise ValueError("Prepare cases from a clean committed controller checkout")
            workspace = args.output.resolve()
            prepare_case(repository, base, workspace, args.case)
            if args.toolkit:
                install_toolkit(args.toolkit.resolve(), args.toolkit_ref, workspace)
            initialize_git(workspace)
            print(f"Prepared {args.case}: {workspace}. Start a new agent session there and follow AGENTS.md.")
            return 0
        if args.command == "start":
            result = start(repository, label=args.label, model=args.model, effort=args.effort,
                           toolkit_ref=args.toolkit_ref, environment_label=args.environment_label,
                           subagent_limit=args.subagents)
            print(f"Started {result['run_id']}. Implement TASK.md, then run python3 bench.py finish.")
            return 0
        if args.command == "checkpoint":
            result = checkpoint(repository)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            if not result["passed"]:
                print("Phase 1 failed. Preserve the attempt with python3 bench.py finish.")
                return 1
            record = json.loads((repository / ".benchmark/current.json").read_text(encoding="utf-8"))
            print("\nStop this session. Give the following instruction to a NEW session in the same workspace:\n")
            print(followup_prompt(repository, record))
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
                case_id=args.case, subagent_limit=args.subagents,
            )
            print(Path(result["directory"], "REPORT.md").read_text(encoding="utf-8"))
            print("Artifacts:", result["directory"])
            return 0 if result["report"]["accepted"] else 1
        if args.command == "verify":
            result = grade(args.workspace.resolve(), repository, commit_id(repository), args.case)
            if args.output.exists():
                raise ValueError("Refusing to overwrite an existing verification result")
            write_json(args.output, result)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result["passed"] else 1
        if args.command == "compare":
            result = compare(load_reports(args.baseline), load_reports(args.candidate), study=args.study)
            if args.output.exists():
                raise ValueError("Comparison output must be a new directory")
            args.output.mkdir(parents=True)
            write_json(args.output / "comparison.json", result)
            (args.output / "COMPARISON.md").write_text(render_comparison(result), encoding="utf-8")
            print(render_comparison(result))
            return 0 if result["gate_passed"] else 1
        if args.command == "matrix":
            from .matrix import run_matrix
            result = run_matrix(
                repository, toolkit=args.toolkit, toolkit_ref=args.toolkit_ref,
                model=args.model, effort=args.effort, environment_label=args.environment_label,
                output=args.output.resolve(), cases=args.cases, limits=args.subagents,
                repeats=args.repeats, timeout=args.timeout, seed=args.seed,
                plan_only=args.plan_only,
            )
            print(Path(result["markdown"]).read_text(encoding="utf-8"))
            print("Artifacts:", result["directory"])
            return 2 if result["summary"]["status"] == "incomplete" else 0
    except (OSError, ValueError, KeyError) as error:
        print(f"Benchmark error: {error}", file=sys.stderr)
        return 2
    return 2
