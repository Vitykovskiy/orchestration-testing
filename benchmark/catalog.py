"""The fixed public case catalog; no candidate-controlled executable paths."""

CASES = {
    "small": {
        "title": "Накладные расходы на простую правку",
        "task_id": "small-v1", "package": "statusctl",
        "verifier": "verification/small_case.py", "phases": 1,
    },
    "workflow": {
        "title": "Связанные изменения складского приложения",
        "task_id": "stockctl-v1", "package": "stockctl",
        "verifier": "verification/check.py", "phases": 1,
    },
    "parallel": {
        "title": "Шесть независимых аналитических модулей",
        "task_id": "parallel-v1", "package": "opsreport",
        "verifier": "verification/parallel_case.py", "phases": 1,
    },
    "handoff": {
        "title": "Новая сессия и уточнение требований",
        "task_id": "handoff-v1", "package": "shelfctl",
        "verifier": "verification/handoff_case.py", "phases": 2,
    },
}


def case_settings(identifier: str) -> dict:
    if identifier not in CASES:
        raise ValueError(f"Unknown case: {identifier}; choose one of {', '.join(CASES)}")
    return CASES[identifier]
