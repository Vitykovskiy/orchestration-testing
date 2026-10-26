"""Start, finish and externally time a reproducible benchmark attempt."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import platform
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid

from .workspace import (
    changed_paths, commit_id, export_snapshot, git, initialize_git,
    install_toolkit, scope_violations, sha256, solution_patch, tracked_files,
)


TASK_ID = "stockctl-v1"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def environment() -> dict:
    return {
        "python": platform.python_version(),
        "system": platform.system(),
        "machine": platform.machine(),
    }


def start(
    workspace: Path, *, label: str = "current", model: str | None = None,
    toolkit_ref: str | None = None, effort: str | None = None,
    mode: str = "manual", benchmark_ref: str | None = None,
    timeout: int | None = None, runtime: str | None = None,
    environment_label: str | None = None,
) -> dict:
    workspace = workspace.resolve()
    root = Path(git(workspace, "rev-parse", "--show-toplevel").decode().strip()).resolve()
    if root != workspace:
        raise ValueError("Run from the repository root")
    current = workspace / ".benchmark/current.json"
    if current.exists():
        raise ValueError("An attempt already exists. Use a fresh clone for the next attempt; do not reset this one")
    base = commit_id(workspace)
    dirty = changed_paths(workspace, base)
    if dirty:
        raise ValueError("The starting checkout must be clean: " + ", ".join(dirty[:8]))
    task = git(workspace, "show", f"{base}:TASK.md")
    record = {
        "schema_version": 1,
        "run_id": datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:8],
        "task_id": TASK_ID, "task_sha256": sha256(task), "label": label,
        "benchmark_commit": benchmark_ref or base, "workspace_base_commit": base,
        "toolkit_commit": toolkit_ref, "model": model, "reasoning_effort": effort,
        "runtime_version": runtime, "environment_label": environment_label,
        "environment": environment(), "mode": mode, "timeout_seconds": timeout,
        "started_at": utc_now(), "started_monotonic": time.monotonic(),
        "boot_offset": time.time() - time.monotonic(), "status": "running",
    }
    write_json(current, record)
    return record


def timed_process(command: list[str], cwd: Path, stdout: Path, stderr: Path, timeout: int) -> dict:
    """Use a process group so a timeout also terminates local child processes."""
    started = time.monotonic()
    stdout.parent.mkdir(parents=True, exist_ok=True)
    with stdout.open("wb") as out, stderr.open("wb") as err:
        process = subprocess.Popen(command, cwd=cwd, stdout=out, stderr=err, start_new_session=os.name == "posix")
        timed_out = False
        try:
            code = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
            code = process.wait()
    return {"exit_code": code, "timed_out": timed_out, "wall_seconds": time.monotonic() - started}


def grade(workspace: Path, verifier_repo: Path, verifier_commit: str) -> dict:
    """Read verifier bytes from a frozen Git commit, not candidate worktree files."""
    with tempfile.TemporaryDirectory(prefix="stockctl-verifier-") as temporary:
        controller = Path(temporary)
        for path, _ in tracked_files(verifier_repo, verifier_commit):
            if not path.startswith("verification/"):
                continue
            target = controller / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(git(verifier_repo, "show", f"{verifier_commit}:{path}"))
        checker = controller / "verification/check.py"
        if not checker.exists():
            raise ValueError("Frozen commit does not contain verification/check.py")
        output = controller / "grade.json"
        execution = timed_process(
            [sys.executable, "-I", str(checker), "--workspace", str(workspace), "--output", str(output)],
            controller, controller / "stdout.log", controller / "stderr.log", 120,
        )
        if execution["timed_out"] or execution["exit_code"] not in (0, 1) or not output.exists():
            return {
                "passed": False, "criteria": [], "critical_failures": [],
                "error": "Verifier did not complete; inspect the candidate before rerunning",
                "execution": execution,
                "stderr": (controller / "stderr.log").read_text(encoding="utf-8", errors="replace")[-2000:],
            }
        try:
            result = json.loads(output.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            return {"passed": False, "criteria": [], "critical_failures": [], "error": str(error)}
        if not isinstance(result, dict) or not isinstance(result.get("passed"), bool) or not isinstance(result.get("criteria"), list):
            return {"passed": False, "criteria": [], "critical_failures": [], "error": "Invalid verifier result"}
        result["verifier_commit"] = verifier_commit
        result["execution"] = execution
        return result


def usage_from_events(path: Path | None) -> dict:
    """Codex exec's root usage does not establish whole-tree usage coverage."""
    usage = {
        "coverage": "unknown", "total_tree_tokens": None,
        "root_input_tokens": None, "root_cached_input_tokens": None,
        "root_output_tokens": None, "subagent_count": None,
        "source": None,
    }
    if path is None or not path.exists():
        return usage
    totals = {"input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0}
    turns = 0
    with path.open(encoding="utf-8", errors="replace") as stream:
        for line in stream:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict) or event.get("type") != "turn.completed":
                continue
            values = event.get("usage")
            if not isinstance(values, dict):
                continue
            if any(not isinstance(values.get(key), int) or isinstance(values.get(key), bool) or values[key] < 0 for key in totals):
                continue
            for key in totals:
                totals[key] += values[key]
            turns += 1
    if turns:
        usage.update({"coverage": "partial", "source": "codex-exec-jsonl-root"})
        for key, value in totals.items():
            usage["root_" + key] = value
    return usage


def render_report(report: dict) -> str:
    acceptance = report["acceptance"]
    criteria = acceptance.get("criteria", [])
    passed = sum(item.get("passed") is True for item in criteria)
    elapsed = report["wall_seconds"]
    duration = "не измерено" if elapsed is None else f"{elapsed:.1f} с"
    result = "PASS" if report["accepted"] else "FAIL"
    short = lambda value: str(value)[:12] if value else "не указано"
    failures = [item.get("id", "unknown") for item in criteria if item.get("passed") is not True]
    lines = [
        "# Отчёт проверки агентской разработки", "",
        f"- Результат: **{result}**; критерии: **{passed}/{len(criteria)}**; статус: `{report['status']}`.",
        f"- Задача: `{report['task_id']}`; стенд: `{short(report['benchmark_commit'])}`; запуск: `{report['run_id']}`.",
        f"- Вариант: `{report['label']}`; Toolkit: `{short(report['toolkit_commit'])}`.",
        f"- Модель: `{report['model'] or 'не указано'}`; reasoning: `{report['reasoning_effort'] or 'не указано'}`.",
        f"- Время: **{duration}**; способ измерения: `{report['mode']}`.",
        "- Токены всего дерева агентов: **нет подтверждённых данных**.",
        f"- Границы изменений: {'соблюдены' if not report['scope_violations'] else 'НАРУШЕНЫ'}.",
        f"- Вмешательства пользователя: {report['human_interventions'] if report['human_interventions'] is not None else 'не измерены'}.",
    ]
    if report["usage"]["root_input_tokens"] is not None:
        lines.append(f"- Только корневой поток: input {report['usage']['root_input_tokens']}, output {report['usage']['root_output_tokens']}; не общая стоимость.")
    if failures:
        lines.append("- Не прошли: " + ", ".join(failures) + ".")
    if report["scope_violations"]:
        lines.append("- За пределами задачи: " + ", ".join(report["scope_violations"][:6]) + ".")
    if acceptance.get("error"):
        lines.append("- Проверка: " + acceptance["error"] + ".")
    lines.extend([
        "", "Рядом: `report.json`, `changes.patch`. Отчёт создан проверяющей программой; "
        "публичные проверки доступны агенту. Один запуск не доказывает улучшение Toolkit.",
    ])
    return "\n".join(lines) + "\n"


def finish(
    workspace: Path, *, record: dict | None = None, output: Path | None = None,
    execution: dict | None = None, events: Path | None = None,
) -> dict:
    workspace = workspace.resolve()
    state_file = workspace / ".benchmark/current.json"
    external = record is not None
    if record is None:
        if not state_file.exists():
            raise ValueError("No attempt: run python3 bench.py start before editing")
        record = json.loads(state_file.read_text(encoding="utf-8"))
        if record.get("mode") == "external":
            raise ValueError("This attempt is controlled externally; return your result and let the controller finish")
    if record.get("status") != "running":
        raise ValueError("This attempt is already finished; preserve its report and use a fresh clone")
    elapsed = time.monotonic() - record["started_monotonic"]
    same_boot = abs((time.time() - time.monotonic()) - record["boot_offset"]) < 10
    if elapsed < 0 or not same_boot:
        elapsed = None
    if execution is not None:
        elapsed = execution["wall_seconds"]
    violations = scope_violations(workspace, record["workspace_base_commit"])
    patch = solution_patch(workspace, record["workspace_base_commit"])
    acceptance = grade(workspace, workspace, record["workspace_base_commit"])
    status = "completed"
    if execution and execution.get("launch_error"):
        status = "infrastructure_error"
    elif execution and execution["timed_out"]:
        status = "timeout"
    elif execution and execution["exit_code"] != 0:
        status = "agent_failed"
    elif acceptance.get("error"):
        status = "verification_error"
    elif not acceptance["passed"] or violations:
        status = "task_failed"
    accepted = status == "completed" and acceptance["passed"] and not violations
    report = {key: value for key, value in record.items() if key not in {"started_monotonic", "boot_offset"}}
    report.update({
        "status": status, "finished_at": utc_now(), "accepted": bool(accepted),
        "wall_seconds": elapsed, "acceptance": acceptance, "scope_violations": violations,
        "usage": usage_from_events(events), "human_interventions": 0 if external else None,
        "changed_files": changed_paths(workspace, record["workspace_base_commit"]),
        "patch_sha256": sha256(patch), "agent_execution": execution,
        "measurement_notes": [
            "Wall time excludes clone/setup and independent grading.",
            "Manual wall time includes pauses between start and finish.",
            "Token coverage is not complete; root-only tokens are never total cost.",
            "Public acceptance is a functional check, not a blind evaluation or security boundary.",
            "Model/Toolkit metadata in manual mode is declared, not runtime-attested.",
        ],
    })
    output = output or workspace / ".benchmark/results" / record["run_id"]
    output.mkdir(parents=True, exist_ok=True)
    if (output / "report.json").exists():
        raise ValueError(f"Refusing to overwrite an existing report: {output}")
    (output / "changes.patch").write_bytes(patch)
    write_json(output / "report.json", report)
    (output / "REPORT.md").write_text(render_report(report), encoding="utf-8")
    record = dict(record, status=status, report_directory=str(output))
    write_json(state_file, record)
    return {"report": report, "directory": str(output)}


def run_external(
    repository: Path, *, toolkit: Path, toolkit_ref: str, label: str, model: str,
    effort: str, output: Path, timeout: int, environment_label: str,
) -> dict:
    if os.name != "posix":
        raise ValueError("The external runner requires Linux/macOS for process-group timeouts")
    codex = shutil.which("codex")
    if not codex:
        raise ValueError("Codex CLI is unavailable. Install/authenticate it in your test environment, or use start/finish with your current agent")
    if output.exists():
        raise ValueError("--output must name a new directory so earlier attempts cannot be overwritten")
    base = commit_id(repository)
    if changed_paths(repository, base):
        raise ValueError("Run the controller from a clean benchmark checkout")
    output.mkdir(parents=True)
    workspace = output / "workspace"
    export_snapshot(repository, base, workspace)
    imported = install_toolkit(toolkit.resolve(), toolkit_ref, workspace)
    initialize_git(workspace)
    runtime = subprocess.run([codex, "--version"], capture_output=True, text=True, check=True).stdout.strip()
    record = start(
        workspace, label=label, model=model, toolkit_ref=imported["commit"], effort=effort,
        mode="external", benchmark_ref=base, timeout=timeout, runtime=runtime,
        environment_label=environment_label,
    )
    record["toolkit_manifest_sha256"] = imported["manifest_sha256"]
    record["toolkit_profile_runtime_sha256"] = imported["profile_runtime_sha256"]
    write_json(workspace / ".benchmark/current.json", record)
    prompt = (
        "Выполни тестовую задачу из TASK.md в этом репозитории, соблюдая AGENTS.md. "
        "Это уже запущенная внешним контроллером попытка. Не запускай bench.py start/finish. "
        "Внеси изменения, выполни необходимые проверки и верни краткий результат. "
        "Не создавай commit/PR и не меняй измерения, условия задачи или проверяющие программы."
    )
    (output / "prompt.txt").write_text(prompt + "\n", encoding="utf-8")
    command = [
        codex, "exec", "--cd", str(workspace), "--sandbox", "workspace-write",
        "--model", model, "-c", 'approval_policy="never"',
        "-c", f'model_reasoning_effort="{effort}"', "--json",
        "--output-last-message", str(output / "agent-final.txt"), prompt,
    ]
    write_json(output / "invocation.json", {"argv": command, "runtime_version": runtime})
    launch_started = time.monotonic()
    try:
        execution = timed_process(command, workspace, output / "events.jsonl", output / "stderr.log", timeout)
    except OSError as error:
        execution = {
            "exit_code": None, "timed_out": False,
            "wall_seconds": time.monotonic() - launch_started,
            "launch_error": str(error),
        }
    return finish(workspace, record=record, output=output, execution=execution, events=output / "events.jsonl")
