"""Git snapshots and the deliberately narrow solution boundary."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import tempfile
import tomllib

from .catalog import case_settings


def git(repo: Path, *args: str, env: dict | None = None) -> bytes:
    result = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, env=env, check=False
    )
    if result.returncode:
        raise ValueError(result.stderr.decode("utf-8", errors="replace").strip())
    return result.stdout


def commit_id(repo: Path, ref: str = "HEAD") -> str:
    return git(repo, "rev-parse", "--verify", f"{ref}^{{commit}}").decode().strip()


def tracked_files(repo: Path, commit: str) -> list[tuple[str, str]]:
    entries = []
    for item in git(repo, "ls-tree", "-r", "-z", commit).split(b"\0"):
        if not item:
            continue
        header, raw_path = item.split(b"\t", 1)
        mode, kind, _ = header.decode().split()
        path = raw_path.decode("utf-8")
        parts = PurePosixPath(path)
        if kind != "blob" or mode not in {"100644", "100755"}:
            raise ValueError(f"Unsupported snapshot entry: {path} ({mode}, {kind})")
        if parts.is_absolute() or ".." in parts.parts or ".git" in parts.parts:
            raise ValueError(f"Unsafe snapshot path: {path}")
        entries.append((path, mode))
    return entries


def export_snapshot(repo: Path, commit: str, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=False)
    for path, mode in tracked_files(repo, commit):
        target = destination / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(git(repo, "show", f"{commit}:{path}"))
        target.chmod(0o755 if mode == "100755" else 0o644)


def initialize_git(workspace: Path) -> str:
    git(workspace, "init", "-b", "main")
    git(workspace, "add", ".")
    git(
        workspace, "-c", "user.name=Benchmark", "-c",
        "user.email=benchmark@localhost", "-c", "core.hooksPath=" + os.devnull,
        "-c", "commit.gpgsign=false", "commit", "-m", "Frozen benchmark input"
    )
    return commit_id(workspace)


def changed_paths(workspace: Path, base: str) -> list[str]:
    changed = git(workspace, "diff", "--name-only", "--no-renames", "-z", base, "--")
    untracked = git(workspace, "ls-files", "--others", "--exclude-standard", "-z")
    return sorted({p.decode("utf-8") for p in (changed + untracked).split(b"\0") if p})


def snapshot_case(workspace: Path, base: str) -> dict:
    if not git(workspace, "ls-tree", "--name-only", base, "--", "CASE.json").strip():
        return {"id": "workflow"}
    metadata = json.loads(git(workspace, "show", f"{base}:CASE.json"))
    case_settings(metadata.get("id"))
    return metadata


def prepare_case(repository: Path, base: str, destination: Path, case_id: str) -> dict:
    """Build a case-specific copy without other product fixtures or solutions."""
    settings = case_settings(case_id)
    export_snapshot(repository, base, destination)
    metadata = {"id": case_id, "source_commit": base}
    if case_id != "workflow":
        case_root = destination / "cases" / case_id
        overlay = case_root / "workspace"
        if not overlay.is_dir() or not (case_root / "TASK.md").is_file():
            raise ValueError(f"The selected commit does not contain case {case_id}")
        for name in ("stockctl", "tests_product"):
            target = destination / name
            if target.exists():
                shutil.rmtree(target)
        (destination / "docs/contracts.md").unlink(missing_ok=True)
        shutil.copytree(overlay, destination, dirs_exist_ok=True)
        shutil.copyfile(case_root / "TASK.md", destination / "TASK.md")
        if settings["phases"] == 2:
            followup = (case_root / "phase2.md").read_bytes()
            metadata["followup_sha256"] = sha256(followup)
            runtime = destination / ".benchmark"
            runtime.mkdir(exist_ok=True)
            (runtime / "next-phase.md").write_bytes(followup)
    cases = destination / "cases"
    if cases.exists():
        shutil.rmtree(cases)
    # Controller self-tests refer to the complete catalog, not one prepared case.
    self_tests = destination / "tests_harness"
    if self_tests.exists():
        shutil.rmtree(self_tests)
    (destination / "README.md").write_text(
        f"# Рабочая копия: {case_id}\n\n{settings['title']}.\n\n"
        "Следуйте AGENTS.md, выполните текущий этап TASK.md и верните сформированные отчёт и patch.\n"
        "Здесь только исходники выбранного сценария. Другие сценарии и тесты контроллера исключены.\n\n"
        "Полный стенд: https://github.com/Vitykovskiy/orchestration-testing\n",
        encoding="utf-8",
    )
    (destination / "CASE.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return metadata


def allowed_solution_path(path: str, case_id: str = "workflow") -> bool:
    package = case_settings(case_id)["package"]
    return path.startswith((package + "/", "tests_solution/")) or path == "docs/solution.md"


def scope_violations(workspace: Path, base: str) -> list[str]:
    case_id = snapshot_case(workspace, base)["id"]
    violations = [p for p in changed_paths(workspace, base) if not allowed_solution_path(p, case_id)]
    for dirname in (case_settings(case_id)["package"], "tests_solution"):
        directory = workspace / dirname
        if directory.is_symlink():
            violations.append(f"symlink:{dirname}")
            continue
        if directory.exists():
            violations.extend(f"symlink:{p.relative_to(workspace)}" for p in directory.rglob("*") if p.is_symlink())
    if (workspace / "docs" / "solution.md").is_symlink():
        violations.append("symlink:docs/solution.md")
    return sorted(set(violations))


def solution_patch(workspace: Path, base: str) -> bytes:
    """Include new solution files without changing the user's real index."""
    with tempfile.TemporaryDirectory(prefix="benchmark-index-") as tmp:
        env = dict(os.environ, GIT_INDEX_FILE=str(Path(tmp) / "index"))
        git(workspace, "read-tree", base, env=env)
        case_id = snapshot_case(workspace, base)["id"]
        paths = [case_settings(case_id)["package"], "tests_solution", "docs/solution.md"]
        for path in paths:
            tracked = git(workspace, "ls-files", "--", path, env=env)
            if (workspace / path).exists() or tracked:
                git(workspace, "add", "-A", "--", path, env=env)
        return git(workspace, "diff", "--cached", "--binary", "--no-ext-diff", base, "--", env=env)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def install_toolkit(source: Path, ref: str, workspace: Path) -> dict:
    """Import only into a newly created benchmark workspace, never globally."""
    if not re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", ref):
        raise ValueError("--toolkit-ref must be a full immutable commit SHA")
    commit = commit_id(source, ref)
    if (workspace / ".agents").exists() or (workspace / ".codex").exists():
        raise ValueError("Fresh workspace already contains .agents or .codex")
    manifest = []
    adaptations = []
    profile_runtime = {}
    entries = tracked_files(source, commit)
    for path, _ in entries:
        if path.startswith("skills/"):
            target = workspace / ".agents" / path
        elif path.startswith("agents/") and path.endswith(".toml"):
            target = workspace / ".codex" / path
        else:
            continue
        original = git(source, "show", f"{commit}:{path}")
        content = original
        if path.startswith("agents/"):
            profile = tomllib.loads(original.decode("utf-8"))
            profile_runtime[path] = {
                key: value for key, value in profile.items()
                if key not in {"description", "developer_instructions"}
            }
            content = original.replace(b"../skills/", b"../../.agents/skills/")
            adaptations.append(f"- `{target.relative_to(workspace)}`: ../skills/ → ../../.agents/skills/; SHA-256 {sha256(content)}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        manifest.append(f"{sha256(original)}  {path}")
    if not manifest:
        raise ValueError("The selected repository contains no Toolkit skills or profiles")
    fragment_path = "skills/codex-orchestration-pact/references/templates/AGENTS.fragment.md"
    fragment = git(source, "show", f"{commit}:{fragment_path}").decode("utf-8")
    blocks = re.findall(r"```markdown\n(.*?)```", fragment, flags=re.S)
    if len(blocks) != 1:
        raise ValueError("Expected one project-local AGENTS fragment")
    with (workspace / "AGENTS.md").open("a", encoding="utf-8") as stream:
        stream.write("\n\n" + blocks[0].rstrip() + "\n")
    metadata = workspace / ".agents"
    (metadata / "upstream.sha256").write_text("\n".join(manifest) + "\n", encoding="utf-8")
    (metadata / "SOURCE.md").write_text(
        f"# Toolkit source\n\nRepository: https://github.com/Vitykovskiy/codex-toolkit\n"
        f"Commit: {commit}\nSnapshot: complete skills/ and agents/*.toml from Git objects.\n"
        "Authorization: explicit benchmark run with --toolkit and --toolkit-ref.\n"
        "Skills are byte-identical; model settings are preserved.\n"
        "The runner does not prove that a runtime loaded every profile.\n",
        encoding="utf-8",
    )
    (metadata / "LOCAL-ADAPTATIONS.md").write_text(
        "# Local adaptations\n\nProfile Markdown paths and appended project AGENTS fragment only.\n\n"
        + "\n".join(adaptations) + "\n", encoding="utf-8"
    )
    return {
        "commit": commit,
        "manifest_sha256": sha256("\n".join(manifest).encode()),
        "profile_runtime_sha256": sha256(json.dumps(profile_runtime, sort_keys=True).encode()),
    }
