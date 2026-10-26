"""Exercise attempt preservation and snapshot boundaries using real Git repositories."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from benchmark.runs import finish, start, timed_process, usage_from_events
from benchmark.workspace import export_snapshot, git, initialize_git, solution_patch


SOURCE = Path(__file__).resolve().parents[1]


class AttemptTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="runner-test-", dir=SOURCE.parent)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        # Copy only the authored fixture and harness. Concurrent local checks
        # may put temporary directories beside these files.
        for name in ("stockctl", "verification", "tests_product", "docs", "benchmark", "bench.py", "TASK.md", "AGENTS.md", ".gitignore"):
            source = SOURCE / name
            target = self.workspace / name
            if source.is_dir():
                shutil.copytree(source, target, ignore=shutil.ignore_patterns(".git", ".benchmark", "__pycache__", "*.pyc"))
            elif source.is_file():
                shutil.copy2(source, target)
        # A temporary checkout has the same generated-output exclusions even
        # when these tests are run before the repository is first published.
        ignored = self.workspace / ".gitignore"
        with ignored.open("a", encoding="utf-8") as stream:
            stream.write("\n.benchmark/\n__pycache__/\n*.pyc\n")
        self.base = initialize_git(self.workspace)

    def test_start_rejects_changed_or_untracked_input(self):
        task = self.workspace / "TASK.md"
        original = task.read_bytes()
        task.write_bytes(original + b"\nChanged task\n")
        with self.assertRaisesRegex(ValueError, "clean"):
            start(self.workspace)
        task.write_bytes(original)
        extra = self.workspace / "untracked.txt"
        extra.write_text("untracked input", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "clean"):
            start(self.workspace)
        self.assertFalse((self.workspace / ".benchmark/current.json").exists())
        extra.unlink()
        record = start(self.workspace)
        self.assertEqual(record["workspace_base_commit"], self.base)
        with self.assertRaisesRegex(ValueError, "already exists"):
            start(self.workspace)

    def test_frozen_verifier_and_failed_report_survive_worktree_changes(self):
        begun = subprocess.run(
            [sys.executable, "bench.py", "start"], cwd=self.workspace,
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(begun.returncode, 0, begun.stderr)
        original = git(self.workspace, "show", f"{self.base}:verification/check.py")
        snapshot = self.root / "frozen"
        export_snapshot(self.workspace, self.base, snapshot)
        verifier = self.workspace / "verification/check.py"
        verifier.write_text("raise RuntimeError('Do not execute the edited verifier')\n", encoding="utf-8")

        finished = subprocess.run(
            [sys.executable, "bench.py", "finish"], cwd=self.workspace,
            capture_output=True, text=True, timeout=150,
        )
        self.assertEqual(finished.returncode, 1, finished.stderr)
        self.assertIn("FAIL", finished.stdout)
        outputs = list((self.workspace / ".benchmark/results").glob("*/report.json"))
        self.assertEqual(len(outputs), 1)
        report = json.loads(outputs[0].read_text(encoding="utf-8"))
        self.assertEqual((snapshot / "verification/check.py").read_bytes(), original)
        self.assertFalse(report["accepted"])
        self.assertEqual(report["status"], "task_failed")
        self.assertIn("verification/check.py", report["scope_violations"])
        self.assertNotIn("error", report["acceptance"])
        self.assertEqual(report["acceptance"]["verifier_commit"], self.base)
        self.assertTrue(report["acceptance"]["criteria"])
        self.assertTrue(any(not item["passed"] for item in report["acceptance"]["criteria"]))

        output = outputs[0].parent
        files = {name: (output / name).read_bytes() for name in ("report.json", "REPORT.md", "changes.patch")}
        with self.assertRaisesRegex(ValueError, "already finished"):
            finish(self.workspace)
        with self.assertRaisesRegex(ValueError, "already exists"):
            start(self.workspace)
        for name, content in files.items():
            self.assertEqual((output / name).read_bytes(), content)

    def test_patch_includes_new_files_and_worktree_without_changing_real_index(self):
        service = self.workspace / "stockctl/service.py"
        service.write_text(service.read_text(encoding="utf-8") + "\n# staged change\n", encoding="utf-8")
        git(self.workspace, "add", "stockctl/service.py")
        service.write_text(service.read_text(encoding="utf-8") + "# final worktree change\n", encoding="utf-8")
        new_file = self.workspace / "tests_solution/test_new.py"
        new_file.parent.mkdir()
        new_file.write_text("# A new solution check.\n", encoding="utf-8")
        index = self.workspace / ".git/index"
        original_index = index.read_bytes()

        change = solution_patch(self.workspace, self.base)

        self.assertEqual(index.read_bytes(), original_index)
        self.assertIn(b"tests_solution/test_new.py", change)
        restored = self.root / "restored"
        export_snapshot(self.workspace, self.base, restored)
        initialize_git(restored)
        subprocess.run(["git", "-C", str(restored), "apply", "--binary", "-"], input=change, check=True, capture_output=True)
        self.assertEqual((restored / "stockctl/service.py").read_bytes(), service.read_bytes())
        self.assertEqual((restored / "tests_solution/test_new.py").read_bytes(), new_file.read_bytes())

    def test_patch_preserves_staged_deletion_of_allowed_tree(self):
        git(self.workspace, "rm", "-r", "stockctl")
        index = self.workspace / ".git/index"
        original_index = index.read_bytes()
        change = solution_patch(self.workspace, self.base)
        self.assertEqual(index.read_bytes(), original_index)
        self.assertIn(b"deleted file mode", change)
        restored = self.root / "restored"
        export_snapshot(self.workspace, self.base, restored)
        initialize_git(restored)
        subprocess.run(["git", "-C", str(restored), "apply", "--binary", "-"], input=change, check=True, capture_output=True)
        self.assertFalse((restored / "stockctl").exists())

    def test_timeout_cannot_be_accepted_even_when_functional_checks_pass(self):
        record = start(self.workspace, mode="external")
        execution = {"timed_out": True, "exit_code": -9, "wall_seconds": 3.5}
        passing_grade = {"passed": True, "criteria": [{"id": "functional", "passed": True}], "critical_failures": []}
        with patch("benchmark.runs.grade", return_value=passing_grade):
            result = finish(self.workspace, record=record, execution=execution)
        self.assertEqual(result["report"]["status"], "timeout")
        self.assertFalse(result["report"]["accepted"])
        self.assertEqual(result["report"]["wall_seconds"], 3.5)
        self.assertTrue((Path(result["directory"]) / "report.json").exists())


class ProcessAndUsageTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "posix", "Process-group cleanup requires POSIX")
    def test_timeout_terminates_a_local_child_process(self):
        with tempfile.TemporaryDirectory(prefix="runner-timeout-") as temporary:
            root = Path(temporary)
            marker = root / "child-finished"
            pid_file = root / "child.pid"
            child = "import pathlib,time;time.sleep(1);pathlib.Path('child-finished').write_text('survived');time.sleep(30)"
            parent = (
                "import pathlib,subprocess,sys,time\n"
                f"child = subprocess.Popen([sys.executable, '-c', {child!r}])\n"
                "pathlib.Path('child.pid').write_text(str(child.pid))\n"
                "time.sleep(30)\n"
            )
            execution = timed_process(
                [sys.executable, "-c", parent], root,
                root / "stdout.log", root / "stderr.log", timeout=0.4,
            )
            self.assertTrue(execution["timed_out"])
            self.assertNotEqual(execution["exit_code"], 0)
            self.assertTrue(pid_file.exists(), "The child must start before the timeout")
            time.sleep(0.8)
            self.assertFalse(marker.exists(), "The timed-out child continued running")
            process_stat = Path("/proc") / pid_file.read_text() / "stat"
            if process_stat.exists():
                # A killed child may briefly remain as a zombie until the host
                # reaps it, but it must not remain runnable or sleeping.
                state = process_stat.read_text().rsplit(") ", 1)[1].split()[0]
                self.assertEqual(state, "Z")

    def test_root_usage_remains_partial_and_missing_usage_is_unknown(self):
        with tempfile.TemporaryDirectory(prefix="runner-usage-") as temporary:
            events = Path(temporary) / "events.jsonl"
            events.write_text(
                "not json\n"
                + json.dumps({"type": "turn.completed", "usage": {"input_tokens": 20, "cached_input_tokens": 10, "output_tokens": 2, "reasoning_output_tokens": 900}}) + "\n"
                + json.dumps({"type": "turn.completed", "usage": {"input_tokens": 10, "cached_input_tokens": 1, "output_tokens": 1}}) + "\n"
                + json.dumps({"type": "turn.completed", "usage": {"input_tokens": True, "cached_input_tokens": 0, "output_tokens": 1}}) + "\n",
                encoding="utf-8",
            )
            usage = usage_from_events(events)
            self.assertEqual(usage["coverage"], "partial")
            self.assertIsNone(usage["total_tree_tokens"])
            self.assertEqual(usage["root_input_tokens"], 30)
            self.assertEqual(usage["root_cached_input_tokens"], 11)
            self.assertEqual(usage["root_output_tokens"], 3)
            self.assertIsNone(usage["subagent_count"])
            missing = usage_from_events(Path(temporary) / "missing.jsonl")
            self.assertEqual(missing["coverage"], "unknown")
            self.assertIsNone(missing["root_input_tokens"])


if __name__ == "__main__":
    unittest.main()
