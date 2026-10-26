"""Conservative, reproducible comparison of completed attempt reports."""

from __future__ import annotations

import json
import math
from pathlib import Path


MATCH_FIELDS = (
    "schema_version", "task_id", "task_sha256", "benchmark_commit", "model",
    "reasoning_effort", "runtime_version", "environment_label", "environment",
    "mode", "timeout_seconds",
    "toolkit_profile_runtime_sha256",
)


def load_reports(paths: list[Path]) -> list[dict]:
    reports = []
    seen = set()
    for path in paths:
        if path.is_dir():
            path = path / "report.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("schema_version") != 1:
            raise ValueError(f"Unsupported report: {path}")
        statuses = {"completed", "task_failed", "timeout", "agent_failed", "verification_error", "infrastructure_error"}
        if data.get("status") not in statuses or not isinstance(data.get("accepted"), bool):
            raise ValueError(f"Incomplete report: {path}")
        acceptance = data.get("acceptance")
        if not isinstance(acceptance, dict) or not isinstance(acceptance.get("passed"), bool):
            raise ValueError(f"Missing acceptance result: {path}")
        criteria = acceptance.get("criteria")
        if not isinstance(criteria, list) or any(
            not isinstance(item, dict) or not isinstance(item.get("id"), str)
            or not isinstance(item.get("passed"), bool) for item in criteria
        ):
            raise ValueError(f"Invalid acceptance criteria: {path}")
        if len({item["id"] for item in criteria}) != len(criteria):
            raise ValueError(f"Duplicate acceptance criterion: {path}")
        if data["accepted"] and (data["status"] != "completed" or not acceptance["passed"] or not criteria or not all(item["passed"] for item in criteria) or data.get("scope_violations") or acceptance.get("critical_failures")):
            raise ValueError(f"Contradictory acceptance status: {path}")
        identifier = data.get("run_id")
        if not identifier or identifier in seen:
            raise ValueError(f"Missing or duplicate run ID: {path}")
        seen.add(identifier)
        duration = data.get("wall_seconds")
        if duration is not None and (not isinstance(duration, (int, float)) or isinstance(duration, bool) or not math.isfinite(duration) or duration < 0):
            raise ValueError(f"Invalid wall time: {path}")
        reports.append(data)
    if not reports:
        raise ValueError("Both variants need at least one report")
    return reports


def summarize(reports: list[dict]) -> dict:
    accepted = sum(report["accepted"] for report in reports)
    measured = all(report.get("wall_seconds") is not None for report in reports)
    seconds = sum(report["wall_seconds"] for report in reports) if measured else None
    interventions = [r.get("human_interventions") for r in reports]
    criterion_ids = {item["id"] for r in reports for item in r["acceptance"].get("criteria", [])}
    return {
        "attempts": len(reports), "accepted": accepted,
        "acceptance_rate": accepted / len(reports),
        "total_wall_seconds": seconds,
        "seconds_per_accepted_task": seconds / accepted if seconds is not None and accepted else None,
        "critical_failures": sum(bool(r.get("scope_violations") or r.get("acceptance", {}).get("critical_failures")) for r in reports),
        "human_interventions": sum(interventions) if all(isinstance(i, int) for i in interventions) else None,
        "total_tree_tokens": None,
        "criterion_pass_counts": {
            identifier: sum(any(item["id"] == identifier and item["passed"] for item in r["acceptance"].get("criteria", [])) for r in reports)
            for identifier in sorted(criterion_ids)
        },
        "run_ids": [r["run_id"] for r in reports],
        "failures": [r["run_id"] for r in reports if not r["accepted"]],
    }


def compare(baseline: list[dict], candidate: list[dict], minimum_repeats: int = 5) -> dict:
    reasons = []
    all_reports = baseline + candidate
    identifiers = [r["run_id"] for r in all_reports]
    if len(set(identifiers)) != len(identifiers):
        raise ValueError("The same attempt cannot appear in both variants")
    reference = baseline[0]
    for field in MATCH_FIELDS:
        if any(r.get(field) != reference.get(field) for r in all_reports):
            reasons.append(f"Условия различаются: {field}")
    for group, name in ((baseline, "baseline"), (candidate, "candidate")):
        revisions = {r.get("toolkit_commit") for r in group}
        if len(revisions) != 1 or None in revisions:
            reasons.append(f"В {name} нужна одна известная ревизия Toolkit")
    if baseline[0].get("toolkit_commit") == candidate[0].get("toolkit_commit"):
        reasons.append("Обе группы используют одну ревизию Toolkit")
    for field in ("model", "reasoning_effort", "runtime_version", "environment_label", "toolkit_profile_runtime_sha256"):
        if any(not r.get(field) for r in all_reports):
            reasons.append(f"Не указано условие: {field}")
    if len(baseline) != len(candidate):
        reasons.append("Число попыток в группах различается")
    if min(len(baseline), len(candidate)) < minimum_repeats:
        reasons.append(f"Нужно минимум {minimum_repeats} повторов на вариант")
    if any(r.get("mode") != "external" for r in all_reports):
        reasons.append("Ручное измерение включает паузы; результат только предварительный")
    sets = [
        frozenset(item["id"] for item in r["acceptance"].get("criteria", []))
        for r in all_reports if not r["acceptance"].get("error")
    ]
    if not sets or not sets[0] or any(ids != sets[0] for ids in sets):
        reasons.append("Набор критериев приёмки различается или отсутствует")
    a, b = summarize(baseline), summarize(candidate)
    ratio = None
    if a["seconds_per_accepted_task"] and b["seconds_per_accepted_task"] is not None:
        ratio = b["seconds_per_accepted_task"] / a["seconds_per_accepted_task"]
    else:
        reasons.append("Нет измеримого времени на принятую задачу для обеих групп")
    eligible = not reasons
    if b["critical_failures"]:
        reasons.append("В candidate есть критические ошибки или нарушение границ изменений")
    if b["acceptance_rate"] < a["acceptance_rate"]:
        reasons.append("Доля принятых задач уменьшилась")
    criterion_changes = {}
    for identifier in sorted(set(a["criterion_pass_counts"]) | set(b["criterion_pass_counts"])):
        before = a["criterion_pass_counts"].get(identifier, 0)
        after = b["criterion_pass_counts"].get(identifier, 0)
        criterion_changes[identifier] = {"baseline_passes": before, "candidate_passes": after}
        if after / len(candidate) < before / len(baseline):
            reasons.append(f"Критерий {identifier} ухудшился: {before}/{len(baseline)} → {after}/{len(candidate)}")
    if b["human_interventions"] is None or a["human_interventions"] is None:
        reasons.append("Вмешательства пользователя не измерены")
    elif b["human_interventions"] > a["human_interventions"]:
        reasons.append("Потребовалось больше вмешательств пользователя")
    if ratio is not None and ratio > 0.85:
        reasons.append("Экономия времени на принятую задачу меньше 15%")
    result = {
        "schema_version": 1, "metric": "seconds_per_accepted_task",
        "baseline": a, "candidate": b, "candidate_to_baseline_ratio": ratio,
        "criterion_changes": criterion_changes,
        "comparable": eligible, "gate_passed": not reasons,
        "conclusion": "Критерии улучшения выполнены на этой задаче" if not reasons else "Эффект улучшения не подтверждён",
        "reasons": reasons,
        "limits": [
            "15% and five repeats are engineering thresholds, not statistical significance.",
            "All submitted attempts, including failures and timeouts, count toward cost.",
            "Public test cases can be learned; use a fresh task for independent confirmation after tuning.",
            "One task does not establish performance across all development work.",
            "Whole-tree token cost is unknown and is not compared.",
        ],
    }
    return result


def render_comparison(result: dict) -> str:
    def seconds(value):
        return "нет данных" if value is None else f"{value:.1f} с"

    a, b = result["baseline"], result["candidate"]
    text = [
        "# Сравнение Toolkit", "", f"**{result['conclusion']}.**", "",
        "| Метрика | Baseline | Candidate |", "|---|---:|---:|",
        f"| Принято / попыток | {a['accepted']}/{a['attempts']} | {b['accepted']}/{b['attempts']} |",
        f"| Время на принятую задачу, включая неудачи | {seconds(a['seconds_per_accepted_task'])} | {seconds(b['seconds_per_accepted_task'])} |",
        f"| Попытки с критическими ошибками | {a['critical_failures']} | {b['critical_failures']} |",
    ]
    if result["reasons"]:
        text += ["", *[f"- {reason}." for reason in result["reasons"]]]
    changes = {key: counts for key, counts in result["criterion_changes"].items() if counts["baseline_passes"] != counts["candidate_passes"]}
    if changes:
        text += ["", "| Изменившийся критерий | Baseline | Candidate |", "|---|---:|---:|"]
        text += [f"| {key} | {counts['baseline_passes']}/{a['attempts']} | {counts['candidate_passes']}/{b['attempts']} |" for key, counts in changes.items()]
    text += ["", "Порог 15% — правило принятия изменения, не доказательство статистической значимости. "
             "Расход токенов всего дерева не измерен. Вывод относится только к этой задаче."]
    return "\n".join(text) + "\n"
