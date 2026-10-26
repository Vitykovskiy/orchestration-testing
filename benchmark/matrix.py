"""Plan and run sequential capacity experiments over independent fresh cases."""

from __future__ import annotations

import json
from pathlib import Path
import random
import re
import shutil

from .catalog import case_settings
from .compare import compare, summarize, validate_report
from .runs import run_external, utc_now, write_json
from .workspace import changed_paths, commit_id, git, sha256


def _validate_inputs(cases: list[str], limits: list[int], repeats: int, timeout: int, seed: int) -> None:
    if not cases or len(set(cases)) != len(cases):
        raise ValueError("Select at least one case, without duplicates")
    for case_id in cases:
        case_settings(case_id)
    if len(limits) < 2 or len(set(limits)) != len(limits) or any(type(n) is not int or n < 1 for n in limits):
        raise ValueError("Select at least two distinct positive subagent limits")
    if type(repeats) is not int or repeats < 1 or type(timeout) is not int or timeout < 1:
        raise ValueError("repeats and timeout must be positive integers")
    if type(seed) is not int:
        raise ValueError("seed must be an integer")


def planned_order(cases: list[str], limits: list[int], repeats: int, seed: int) -> list[dict]:
    """Rotate each capacity through each order position; keep variants adjacent."""
    randomizer = random.Random(seed)
    ordered_cases = list(cases)
    randomizer.shuffle(ordered_cases)
    ordered_limits = sorted(limits)
    randomizer.shuffle(ordered_limits)
    offsets = {case_id: randomizer.randrange(len(ordered_limits)) for case_id in cases}
    plan = []
    for repeat in range(repeats):
        case_offset = repeat % len(ordered_cases)
        case_order = ordered_cases[case_offset:] + ordered_cases[:case_offset]
        for case_id in case_order:
            limit_offset = (repeat + offsets[case_id]) % len(ordered_limits)
            capacity_order = ordered_limits[limit_offset:] + ordered_limits[:limit_offset]
            for limit in capacity_order:
                sequence = len(plan) + 1
                key = f"{sequence:04d}-{case_id}-n{limit}-r{repeat + 1:02d}"
                plan.append({
                    "sequence": sequence, "key": key, "case_id": case_id,
                    "task_id": case_settings(case_id)["task_id"],
                    "subagent_limit": limit, "repeat": repeat + 1,
                    "directory": f"runs/{key}",
                })
    return plan


def _summary(plan: dict, attempts: list[dict], reports: list[dict], plan_hash: str, plan_only: bool) -> dict:
    planned_count = len(plan["runs"])
    incomplete = [a for a in attempts if a["status"] == "incomplete"]
    complete = not plan_only and len(attempts) == planned_count and not incomplete
    status = "planned" if plan_only else "completed" if complete else "incomplete"
    by_case = {}
    for case_id in plan["cases"]:
        cells = {}
        for limit in plan["limits"]:
            cell_reports = [r for r in reports if r.get("case_id") == case_id and r.get("subagent_limit") == limit]
            cell_attempts = [a for a in attempts if a["case_id"] == case_id and a["subagent_limit"] == limit]
            totals = summarize(cell_reports)
            cell_complete = len(cell_reports) == plan["repeats"] and len(cell_attempts) == plan["repeats"]
            totals.update({
                "planned_attempts": plan["repeats"], "reported_attempts": len(cell_reports),
                "incomplete_attempts": sum(a["status"] == "incomplete" for a in cell_attempts),
                "not_started": plan["repeats"] - len(cell_attempts),
                "complete": cell_complete,
                "observed_partial_wall_seconds": totals["total_wall_seconds"],
            })
            if not cell_complete:
                # An exception without a complete report has unknown comparable cost.
                totals["total_wall_seconds"] = None
                totals["seconds_per_accepted_task"] = None
                totals["acceptance_rate"] = None
            cells[str(limit)] = totals
        baseline = cells[str(plan["baseline_subagent_limit"])]
        for cell in cells.values():
            a, b = baseline["seconds_per_accepted_task"], cell["seconds_per_accepted_task"]
            cell["speedup_vs_minimum_limit"] = a / b if a and b else None
        by_case[case_id] = {"task_id": case_settings(case_id)["task_id"], "limits": cells}
    comparisons = {}
    if not plan_only:
        baseline_reports = [r for r in reports if r["subagent_limit"] == plan["baseline_subagent_limit"]]
        for limit in plan["limits"]:
            if limit == plan["baseline_subagent_limit"]:
                continue
            candidate_reports = [r for r in reports if r["subagent_limit"] == limit]
            if baseline_reports and candidate_reports:
                comparison = compare(baseline_reports, candidate_reports, study="capacity")
            else:
                comparison = {
                    "study": "capacity", "comparable": False, "gate_passed": False,
                    "conclusion": "Эффект улучшения не подтверждён",
                    "reasons": ["Нет завершённых отчётов одной из групп"], "by_case": {},
                }
            if not complete:
                comparison["comparable"] = False
                comparison["gate_passed"] = False
                comparison["conclusion"] = "Эффект улучшения не подтверждён"
                comparison["reasons"].append("Матрица неполна: есть попытки без полного отчёта")
                for case in comparison["by_case"].values():
                    case["comparable"] = False
                    case["gate_passed"] = False
            comparisons[str(limit)] = comparison
    improvements = [int(limit) for limit, comparison in comparisons.items() if comparison["gate_passed"]]
    return {
        "schema_version": 1, "study": "capacity", "status": status,
        "complete": complete, "planned_runs": planned_count,
        "reported_runs": len(reports), "incomplete_runs": len(incomplete),
        "not_started_runs": planned_count - len(attempts),
        "benchmark_commit": plan["benchmark_commit"], "toolkit_commit": plan["toolkit_commit"],
        "cases": plan["cases"], "limits": plan["limits"], "repeats": plan["repeats"],
        "baseline_subagent_limit": plan["baseline_subagent_limit"],
        "maximum_agent_wall_seconds": plan["maximum_agent_wall_seconds"],
        "plan_sha256": plan_hash, "by_case": by_case, "comparisons": comparisons,
        "supported_improvement_limits": improvements,
        "gate_passed": complete and bool(improvements),
        "attempts": attempts,
        "observed_peak_subagents": None,
        "notes": [
            "Attempts run sequentially; only agents within an attempt may run concurrently.",
            "Five repeats per case and capacity are required for the engineering improvement gate.",
            "Incomplete attempts are retained and prevent a positive matrix conclusion.",
            "The wall budget covers agent processes only, excluding setup and external grading.",
            "Configured capacity is not a measurement of actual concurrency or token cost.",
        ],
    }


def render_matrix(result: dict) -> str:
    def seconds(value):
        return "—" if value is None else f"{value:.1f} с"

    def speedup(value):
        return "—" if value is None else f"{value:.2f}×"

    status = {"planned": "План подготовлен; агенты не запускались", "completed": "Матрица завершена", "incomplete": "Матрица неполна; вывод об улучшении запрещён"}[result["status"]]
    hours = result["maximum_agent_wall_seconds"] / 3600
    lines = [
        "# Проверка доступного параллелизма", "", f"**{status}.**", "",
        f"Сценариев: {len(result['cases'])}; лимиты субагентов: {', '.join(map(str, result['limits']))}; повторов: {result['repeats']}.",
        f"Всего попыток по плану: {result['planned_runs']}. Полных отчётов: {result['reported_runs']}; неполных попыток: {result['incomplete_runs']}.",
        f"Верхний лимит времени процессов агента: {hours:.1f} ч; подготовка и внешняя приёмка дополнительно.", "",
        "| Сценарий | Лимит | Принято / полных отчётов | Попыток по плану | Время / принятую | Ускорение к минимальному лимиту |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for case_id, case in result["by_case"].items():
        for limit, cell in case["limits"].items():
            lines.append(
                f"| {case_id} | {limit} | {cell['accepted']}/{cell['reported_attempts']} | {cell['planned_attempts']} | "
                f"{seconds(cell['seconds_per_accepted_task'])} | {speedup(cell['speedup_vs_minimum_limit'])} |"
            )
    if result["comparisons"]:
        lines += ["", "## Сравнение с минимальным лимитом", ""]
        for limit, comparison in result["comparisons"].items():
            lines.append(f"- {result['baseline_subagent_limit']} → {limit}: {comparison['conclusion']}.")
    lines += [
        "", "Подробности каждого сценария, критерии и причины отказа от вывода — в `matrix.json`; "
        "порядок запусков — в `matrix-plan.json`; отдельные отчёты — в `runs/`.",
        "Лимит не включает основной поток. Фактический пик субагентов и токены всего дерева не измерены. "
        "Один повтор — техническая проба; порог 15% и пять повторов не являются тестом статистической значимости.",
    ]
    return "\n".join(lines) + "\n"


def run_matrix(
    repository: Path, *, toolkit: Path, toolkit_ref: str, model: str, effort: str,
    environment_label: str, output: Path, cases: list[str], limits: list[int],
    repeats: int = 5, timeout: int = 1800, seed: int = 20261026,
    plan_only: bool = False,
) -> dict:
    _validate_inputs(cases, limits, repeats, timeout, seed)
    repository, toolkit, output = repository.resolve(), toolkit.resolve(), output.resolve()
    if output.exists():
        raise ValueError("Matrix output must be a new directory")
    if output.is_relative_to(repository) and output.relative_to(repository).parts[0] != ".benchmark":
        raise ValueError("Put matrix output outside the controller checkout or inside its ignored .benchmark directory")
    if not re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", toolkit_ref):
        raise ValueError("--toolkit-ref must be a full immutable commit SHA")
    if any(not value.strip() for value in (model, effort, environment_label)):
        raise ValueError("model, effort and environment_label must be specified")
    base = commit_id(repository)
    if changed_paths(repository, base):
        raise ValueError("Plan and run a matrix from a clean benchmark checkout")
    toolkit_commit = commit_id(toolkit, toolkit_ref)
    for case_id in cases:
        settings = case_settings(case_id)
        task_path = "TASK.md" if case_id == "workflow" else f"cases/{case_id}/TASK.md"
        git(repository, "cat-file", "-e", f"{base}:{task_path}")
        git(repository, "cat-file", "-e", f"{base}:{settings['verifier']}")
    if not plan_only and not shutil.which("codex"):
        raise ValueError("Codex CLI is unavailable; use --plan-only to prepare a plan without launching agents")
    plan = {
        "schema_version": 1, "study": "capacity", "created_at": utc_now(),
        "benchmark_commit": base, "toolkit_commit": toolkit_commit,
        "model": model, "reasoning_effort": effort, "environment_label": environment_label,
        "cases": list(cases), "limits": sorted(limits), "repeats": repeats,
        "timeout_seconds": timeout, "seed": seed, "baseline_subagent_limit": min(limits),
        "maximum_agent_wall_seconds": len(cases) * len(limits) * repeats * timeout,
        "runs": planned_order(cases, limits, repeats, seed),
    }
    output.mkdir(parents=True)
    plan_file = output / "matrix-plan.json"
    write_json(plan_file, plan)
    plan_hash = sha256(plan_file.read_bytes())
    reports, attempts = [], []

    def save_summary() -> dict:
        result = _summary(plan, attempts, reports, plan_hash, plan_only)
        write_json(output / "matrix.json", result)
        (output / "MATRIX.md").write_text(render_matrix(result), encoding="utf-8")
        return result

    if not plan_only:
        for entry in plan["runs"]:
            attempt = dict(entry, started_at=utc_now())
            destination = output / entry["directory"]
            try:
                if commit_id(repository) != base or changed_paths(repository, base):
                    raise ValueError("The controller checkout changed after the matrix was planned")
                if sha256(plan_file.read_bytes()) != plan_hash:
                    raise ValueError("The immutable matrix plan changed during execution")
                result = run_external(
                    repository, toolkit=toolkit, toolkit_ref=toolkit_commit,
                    label=f"{entry['case_id']}-n{entry['subagent_limit']}-r{entry['repeat']}",
                    model=model, effort=effort, output=destination, timeout=timeout,
                    environment_label=environment_label, case_id=entry["case_id"],
                    subagent_limit=entry["subagent_limit"],
                )
                report = result["report"]
                validate_report(report, destination)
                expected = {
                    "benchmark_commit": base, "toolkit_commit": toolkit_commit,
                    "case_id": entry["case_id"], "task_id": entry["task_id"],
                    "subagent_limit": entry["subagent_limit"], "capacity_source": "codex-config",
                    "model": model, "reasoning_effort": effort, "environment_label": environment_label,
                }
                if any(report.get(key) != value for key, value in expected.items()):
                    raise ValueError("Attempt report does not match its planned conditions")
                reports.append(report)
                attempt.update(status="reported", task_status=report["status"], accepted=report["accepted"], report=f"{entry['directory']}/report.json")
            except Exception as error:
                # Preserve an unknown-cost attempt. Do not fabricate a zero-cost failure report.
                destination.mkdir(parents=True, exist_ok=True)
                failure = dict(entry, error_type=type(error).__name__, error=str(error), failed_at=utc_now())
                write_json(destination / "attempt.error.json", failure)
                attempt.update(status="incomplete", accepted=None, error_file=f"{entry['directory']}/attempt.error.json")
            attempt["finished_at"] = utc_now()
            attempts.append(attempt)
            save_summary()
    summary = save_summary()
    return {
        "directory": str(output), "plan": str(plan_file),
        "summary_path": str(output / "matrix.json"), "markdown": str(output / "MATRIX.md"),
        "summary": summary,
    }
