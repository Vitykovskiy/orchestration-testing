"""Compare frozen Toolkit or requested-capacity variants without hiding case regressions."""

from __future__ import annotations

from collections import defaultdict
import json
import math
from pathlib import Path


MATCH_FIELDS = (
    "schema_version", "benchmark_commit", "model", "reasoning_effort",
    "runtime_version", "environment_label", "environment", "mode",
    "toolkit_profile_runtime_sha256",
)
KNOWN_FIELDS = (
    "model", "reasoning_effort", "runtime_version", "environment_label",
    "benchmark_commit", "toolkit_profile_runtime_sha256",
)
STATUSES = {
    "completed", "task_failed", "timeout", "agent_failed", "verification_error",
    "infrastructure_error",
}


def validate_report(data: dict, source: object = "report") -> None:
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        raise ValueError(f"Unsupported report: {source}")
    if data.get("status") not in STATUSES or not isinstance(data.get("accepted"), bool):
        raise ValueError(f"Incomplete report: {source}")
    acceptance = data.get("acceptance")
    if not isinstance(acceptance, dict) or not isinstance(acceptance.get("passed"), bool):
        raise ValueError(f"Missing acceptance result: {source}")
    criteria = acceptance.get("criteria")
    if not isinstance(criteria, list) or any(
        not isinstance(item, dict) or not isinstance(item.get("id"), str)
        or not item["id"] or not isinstance(item.get("passed"), bool) for item in criteria
    ):
        raise ValueError(f"Invalid acceptance criteria: {source}")
    if len({item["id"] for item in criteria}) != len(criteria):
        raise ValueError(f"Duplicate acceptance criterion: {source}")
    if data["accepted"] and (
        data["status"] != "completed" or not acceptance["passed"] or not criteria
        or not all(item["passed"] for item in criteria)
        or data.get("scope_violations") or acceptance.get("critical_failures")
    ):
        raise ValueError(f"Contradictory acceptance status: {source}")
    if not isinstance(data.get("run_id"), str) or not data["run_id"]:
        raise ValueError(f"Missing run ID: {source}")
    if not isinstance(data.get("task_id"), str) or not data["task_id"]:
        raise ValueError(f"Missing task ID: {source}")
    duration = data.get("wall_seconds")
    if duration is not None and (
        not isinstance(duration, (int, float)) or isinstance(duration, bool)
        or not math.isfinite(duration) or duration < 0
    ):
        raise ValueError(f"Invalid wall time: {source}")
    interventions = data.get("human_interventions")
    if interventions is not None and (type(interventions) is not int or interventions < 0):
        raise ValueError(f"Invalid human intervention count: {source}")


def load_reports(paths: list[Path]) -> list[dict]:
    reports = []
    seen = set()
    for path in paths:
        if path.is_dir():
            path = path / "report.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        validate_report(data, path)
        if data["run_id"] in seen:
            raise ValueError(f"Duplicate run ID: {path}")
        seen.add(data["run_id"])
        reports.append(data)
    if not reports:
        raise ValueError("Both variants need at least one report")
    return reports


def summarize(reports: list[dict]) -> dict:
    accepted = sum(report["accepted"] for report in reports)
    measured = bool(reports) and all(report.get("wall_seconds") is not None for report in reports)
    seconds = sum(report["wall_seconds"] for report in reports) if measured else None
    interventions = [r.get("human_interventions") for r in reports]
    same_case = len({r["task_id"] for r in reports}) <= 1
    criterion_ids = {item["id"] for r in reports for item in r["acceptance"].get("criteria", [])}
    return {
        "attempts": len(reports), "accepted": accepted,
        "acceptance_rate": accepted / len(reports) if reports else None,
        "total_wall_seconds": seconds,
        "seconds_per_accepted_task": seconds / accepted if seconds is not None and accepted else None,
        "critical_failures": sum(bool(r.get("scope_violations") or r["acceptance"].get("critical_failures")) for r in reports),
        "human_interventions": sum(interventions) if interventions and all(type(i) is int for i in interventions) else None,
        "total_tree_tokens": None,
        # The same criterion name in different tasks is not the same outcome.
        "criterion_pass_counts": {
            identifier: sum(any(item["id"] == identifier and item["passed"] for item in r["acceptance"].get("criteria", [])) for r in reports)
            for identifier in sorted(criterion_ids)
        } if same_case else {},
        "run_ids": [r["run_id"] for r in reports],
        "failures": [r["run_id"] for r in reports if not r["accepted"]],
    }


def _ratio(before: dict, after: dict) -> float | None:
    a, b = before["seconds_per_accepted_task"], after["seconds_per_accepted_task"]
    return b / a if a is not None and a > 0 and b is not None and b > 0 else None


def _known(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip()) and value.lower() not in {"unknown", "not_set"}


def _fixed_value(reports: list[dict], field: str) -> object:
    values = [r.get(field) for r in reports]
    return values[0] if values and all(value == values[0] for value in values) else None


def _case_comparison(baseline: list[dict], candidate: list[dict], minimum_repeats: int) -> dict:
    conditions = []
    quality = []
    a, b = summarize(baseline), summarize(candidate)
    if not baseline or not candidate:
        conditions.append("Сценарий отсутствует в одной группе")
    elif len(baseline) != len(candidate):
        conditions.append("Число попыток сценария в группах различается")
    if min(len(baseline), len(candidate)) < minimum_repeats:
        conditions.append(f"Нужно минимум {minimum_repeats} повторов сценария на вариант")
    all_reports = baseline + candidate
    for field in ("task_sha256", "timeout_seconds"):
        if _fixed_value(all_reports, field) is None:
            conditions.append(f"Условия сценария различаются или отсутствуют: {field}")
    if any(not _known(r.get("task_sha256")) for r in all_reports):
        conditions.append("Неизвестна ревизия задания")
    sets = [
        frozenset(item["id"] for item in r["acceptance"].get("criteria", []))
        for r in all_reports if not r["acceptance"].get("error")
    ]
    if not sets or not sets[0] or any(ids != sets[0] for ids in sets):
        conditions.append("Набор критериев приёмки различается или отсутствует")
    ratio = _ratio(a, b)
    if ratio is None:
        conditions.append("Нет измеримого времени на принятую задачу для обеих групп")
    if b["critical_failures"]:
        quality.append("В candidate есть критические ошибки или нарушение границ изменений")
    if baseline and candidate and b["acceptance_rate"] < a["acceptance_rate"]:
        quality.append("Доля принятых задач сценария уменьшилась")
    criterion_changes = {}
    for identifier in sorted(set(a["criterion_pass_counts"]) | set(b["criterion_pass_counts"])):
        before = a["criterion_pass_counts"].get(identifier, 0)
        after = b["criterion_pass_counts"].get(identifier, 0)
        criterion_changes[identifier] = {"baseline_passes": before, "candidate_passes": after}
        if baseline and candidate and after / len(candidate) < before / len(baseline):
            quality.append(f"Критерий {identifier} ухудшился: {before}/{len(baseline)} → {after}/{len(candidate)}")
    if b["human_interventions"] is None or a["human_interventions"] is None:
        conditions.append("Вмешательства пользователя не измерены")
    elif b["human_interventions"] > a["human_interventions"]:
        quality.append("Потребовалось больше вмешательств пользователя")
    efficiency = []
    if ratio is not None and ratio > 0.85:
        efficiency.append("Экономия времени на принятую задачу меньше 15%")
    saved = None
    if ratio is not None:
        saved = a["seconds_per_accepted_task"] - b["seconds_per_accepted_task"]
    return {
        "task_sha256": _fixed_value(all_reports, "task_sha256"),
        "baseline": a, "candidate": b, "candidate_to_baseline_ratio": ratio,
        "speedup": 1 / ratio if ratio is not None and ratio > 0 else None,
        "saved_seconds_per_accepted_task": saved,
        "criterion_changes": criterion_changes,
        "comparable": not conditions, "quality_floor_passed": not quality,
        "gate_passed": not (conditions + quality + efficiency),
        "condition_reasons": conditions, "quality_reasons": quality,
        "reasons": conditions + quality + efficiency,
    }


def compare(
    baseline: list[dict], candidate: list[dict], minimum_repeats: int = 5,
    study: str = "toolkit",
) -> dict:
    if study not in {"toolkit", "capacity"}:
        raise ValueError("study must be toolkit or capacity")
    if type(minimum_repeats) is not int or minimum_repeats < 1:
        raise ValueError("minimum_repeats must be a positive integer")
    if not baseline or not candidate:
        raise ValueError("Both variants need at least one report")
    all_reports = baseline + candidate
    identifiers = [r.get("run_id") for r in all_reports]
    if len(set(identifiers)) != len(identifiers) or any(not item for item in identifiers):
        raise ValueError("Run IDs must be present and unique across both variants")
    conditions = []
    reference = baseline[0]
    for field in MATCH_FIELDS:
        if any(r.get(field) != reference.get(field) for r in all_reports):
            conditions.append(f"Условия различаются: {field}")
    for field in KNOWN_FIELDS:
        if any(not _known(r.get(field)) for r in all_reports):
            conditions.append(f"Не указано условие: {field}")
    revisions = [_fixed_value(group, "toolkit_commit") for group in (baseline, candidate)]
    capacities = [_fixed_value(group, "subagent_limit") for group in (baseline, candidate)]
    for number, name in enumerate(("baseline", "candidate")):
        if not _known(revisions[number]):
            conditions.append(f"В {name} нужна одна известная ревизия Toolkit")
        if type(capacities[number]) is not int or capacities[number] < 0:
            conditions.append(f"В {name} нужен один известный запрошенный лимит субагентов")
    if study == "toolkit":
        if revisions[0] == revisions[1]:
            conditions.append("Для сравнения Toolkit нужны разные ревизии")
        if capacities[0] != capacities[1]:
            conditions.append("При сравнении Toolkit лимит субагентов должен совпадать")
    else:
        if revisions[0] != revisions[1]:
            conditions.append("При сравнении параллелизма нужна одна ревизия Toolkit")
        if capacities[0] == capacities[1]:
            conditions.append("Для сравнения параллелизма нужны разные запрошенные лимиты субагентов")
        if any(r.get("capacity_source") != "codex-config" for r in all_reports):
            conditions.append("Нет подтверждения передачи лимита в конфигурацию запуска Codex")
    if any(r.get("mode") != "external" for r in all_reports):
        conditions.append("Ручное измерение включает паузы; результат только предварительный")
    shared_conditions = list(conditions)
    grouped = []
    for reports in (baseline, candidate):
        cases = defaultdict(list)
        for report in reports:
            cases[report["task_id"]].append(report)
        grouped.append(cases)
    task_ids = sorted(set(grouped[0]) | set(grouped[1]))
    balanced = set(grouped[0]) == set(grouped[1]) and all(
        len(grouped[0][task_id]) == len(grouped[1][task_id]) for task_id in task_ids
    )
    if not balanced:
        conditions.append("Набор сценариев и число попыток каждого сценария должны совпадать")
    by_case = {}
    quality = []
    for task_id in task_ids:
        result = _case_comparison(grouped[0][task_id], grouped[1][task_id], minimum_repeats)
        result["shared_condition_reasons"] = shared_conditions
        result["comparable"] = result["comparable"] and not shared_conditions
        result["gate_passed"] = result["gate_passed"] and not shared_conditions
        result["reasons"] = shared_conditions + result["reasons"]
        by_case[task_id] = result
        conditions.extend(f"{task_id}: {reason}" for reason in result["condition_reasons"])
        quality.extend(f"{task_id}: {reason}" for reason in result["quality_reasons"])
    a, b = summarize(baseline), summarize(candidate)
    ratio = _ratio(a, b) if balanced else None
    if ratio is None:
        conditions.append("Нет сопоставимого общего времени на принятую задачу")
    efficiency = []
    if ratio is not None and ratio > 0.85:
        efficiency.append("Общая экономия времени на принятую задачу меньше 15%")
    reasons = list(dict.fromkeys(conditions + quality + efficiency))
    limit_delta = None
    if study == "capacity" and all(type(n) is int and n >= 0 for n in capacities):
        limit_delta = capacities[1] - capacities[0]
    for case in by_case.values():
        saved = case["saved_seconds_per_accepted_task"]
        case["saved_seconds_per_additional_requested_subagent"] = (
            saved / limit_delta if saved is not None and limit_delta is not None and limit_delta > 0 else None
        )
    if len(by_case) == 1:
        criterion_changes = next(iter(by_case.values()))["criterion_changes"]
    else:
        criterion_changes = {
            f"{task_id}/{criterion}": counts
            for task_id, case in by_case.items() for criterion, counts in case["criterion_changes"].items()
        }
    return {
        "schema_version": 1, "study": study, "metric": "seconds_per_accepted_task",
        "baseline": a, "candidate": b, "aggregate_balanced": balanced,
        "candidate_to_baseline_ratio": ratio,
        "speedup": 1 / ratio if ratio is not None and ratio > 0 else None,
        "by_case": by_case, "criterion_changes": criterion_changes,
        "treatment": {
            "baseline_toolkit_commit": revisions[0], "candidate_toolkit_commit": revisions[1],
            "baseline_subagent_limit": capacities[0], "candidate_subagent_limit": capacities[1],
            "requested_subagent_limit_delta": limit_delta,
            "semantics": "requested spawned threads, excluding primary",
            "observed_peak_subagents": None,
        },
        "comparable": not conditions, "gate_passed": not reasons,
        "conclusion": "Критерии улучшения выполнены на этом наборе задач" if not reasons else "Эффект улучшения не подтверждён",
        "reasons": reasons,
        "limits": [
            "15% and five repeats per case are engineering thresholds, not statistical significance.",
            "All submitted attempts, including failures and timeouts, count toward cost.",
            "Cases are compared separately; overall timing is meaningful only for a balanced case mix.",
            "A per-case quality regression cannot be offset by another case's improvement.",
            "Requested subagent capacity is not observed peak concurrency or utilization.",
            "A capacity experiment requires a pinned runtime and no conflicting global capacity overrides.",
            "Public cases can be learned; use new cases for confirmation after tuning.",
            "Whole-tree token cost is unknown and is not compared.",
        ],
    }


def render_comparison(result: dict) -> str:
    def seconds(value):
        return "нет данных" if value is None else f"{value:.1f} с"

    def speedup(value):
        return "—" if value is None else f"{value:.2f}×"

    a, b = result["baseline"], result["candidate"]
    title = "Сравнение лимитов субагентов" if result["study"] == "capacity" else "Сравнение Toolkit"
    treatment = result["treatment"]
    text = [
        f"# {title}", "", f"**{result['conclusion']}.**", "",
        f"Запрошенный лимит субагентов (без основного потока): **{treatment['baseline_subagent_limit']} → {treatment['candidate_subagent_limit']}**.",
        "Фактический пик одновременно работающих субагентов не измерен.", "",
        "| Сценарий | Принято A | Принято B | Время / принятую A | Время / принятую B | Ускорение A/B |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for task_id, case in result["by_case"].items():
        before, after = case["baseline"], case["candidate"]
        text.append(
            f"| {task_id} | {before['accepted']}/{before['attempts']} | {after['accepted']}/{after['attempts']} | "
            f"{seconds(before['seconds_per_accepted_task'])} | {seconds(after['seconds_per_accepted_task'])} | {speedup(case['speedup'])} |"
        )
    if result["aggregate_balanced"]:
        text.append(
            f"| Весь одинаковый набор | {a['accepted']}/{a['attempts']} | {b['accepted']}/{b['attempts']} | "
            f"{seconds(a['seconds_per_accepted_task'])} | {seconds(b['seconds_per_accepted_task'])} | {speedup(result['speedup'])} |"
        )
    else:
        text += ["", "Общий вывод по времени не вычислен: состав сценариев или число попыток различается."]
    if result["reasons"]:
        text += ["", *[f"- {reason}." for reason in result["reasons"]]]
    changes = [
        (task_id, key, counts, case)
        for task_id, case in result["by_case"].items()
        for key, counts in case["criterion_changes"].items()
        if counts["baseline_passes"] != counts["candidate_passes"]
    ]
    if changes:
        text += ["", "| Изменившийся критерий | Baseline | Candidate |", "|---|---:|---:|"]
        text += [
            f"| {task_id}/{key} | {counts['baseline_passes']}/{case['baseline']['attempts']} | {counts['candidate_passes']}/{case['candidate']['attempts']} |"
            for task_id, key, counts, case in changes
        ]
    text += ["", "Порог 15% — правило принятия изменения, не доказательство статистической значимости. "
             "Расход токенов всего дерева не измерен. Вывод относится только к указанным задачам и настройкам лимита."]
    return "\n".join(text) + "\n"
