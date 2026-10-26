"""Exercise prepared cases and two fresh Codex processes with an offline fake CLI."""

import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

from benchmark.runs import checkpoint, finish, run_external, start
from benchmark.workspace import initialize_git, prepare_case, scope_violations, solution_patch


SOURCE = Path(__file__).resolve().parents[1]


FAKE_CODEX = r'''
import json, os, pathlib, sys, time
if sys.argv[1:] == ["--version"]:
    print("fake-codex-for-tests 1")
    raise SystemExit(0)
assert sys.argv[1] == "exec" and "resume" not in sys.argv
workspace = pathlib.Path(sys.argv[sys.argv.index("--cd") + 1])
scratch = workspace / ".codex/tmp"
scratch.mkdir(parents=True, exist_ok=True)
log = scratch / "fake-invocations.jsonl"
number = len(log.read_text().splitlines()) + 1 if log.exists() else 1
state = json.loads((workspace / ".benchmark/current.json").read_text())
entry = {"pid": os.getpid(), "phase": number, "argv": sys.argv[1:], "prompt": sys.argv[-1],
         "checkpoint_before_process": state.get("phase1_checkpoint")}
with log.open("a") as stream:
    stream.write(json.dumps(entry, ensure_ascii=False) + "\n")
if number == 1:
    (scratch / "plan.md").write_text("Plan the requested export and its checks; leave product code unchanged until the next instruction.\n")
    if os.environ.get("FAKE_EARLY_IMPLEMENTATION"):
        source = workspace / "shelfctl/writers.py"
        source.write_text(source.read_text() + "\n# Premature product change.\n")
else:
    source = workspace / "shelfctl/writers.py"
    source.write_text(source.read_text() + "\n# Work performed in the second session.\n")
time.sleep(float(os.environ.get("FAKE_DELAY_SECONDS", "0.02")))
last = pathlib.Path(sys.argv[sys.argv.index("--output-last-message") + 1])
last.write_text("Fake process finished; this is not a model evaluation.\n")
print(json.dumps({"type": "thread.started", "thread_id": f"test-{os.getpid()}"}))
print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 10 * number, "cached_input_tokens": number, "output_tokens": number}}))
'''


@unittest.skipUnless(os.name == "posix", "External process-group runner requires POSIX")
class CaseRunTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="case-run-test-", dir=SOURCE.parent)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.controller = self.root / "controller"
        self.controller.mkdir()
        for name in ("benchmark", "verification", "stockctl", "tests_product", "docs", "AGENTS.md", "TASK.md", "README.md", "bench.py", ".gitignore"):
            source = SOURCE / name
            target = self.controller / name
            if source.is_dir():
                shutil.copytree(source, target, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            else:
                shutil.copy2(source, target)
        shutil.copytree(SOURCE / "cases/handoff", self.controller / "cases/handoff",
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        self.controller_commit = initialize_git(self.controller)

        self.toolkit = self.root / "toolkit"
        skill = self.toolkit / "skills/codex-orchestration-pact"
        (skill / "references/templates").mkdir(parents=True)
        (skill / "SKILL.md").write_text("# Test procedure\n", encoding="utf-8")
        (skill / "references/templates/AGENTS.fragment.md").write_text("```markdown\n## Procedures\nUse existing test procedures.\n```\n", encoding="utf-8")
        agents = self.toolkit / "agents"
        agents.mkdir()
        (agents / "developer.toml").write_text(
            'name="developer"\ndescription="Test profile"\nmodel="test-model"\n'
            'model_reasoning_effort="medium"\ndeveloper_instructions="Follow the task"\n', encoding="utf-8"
        )
        self.toolkit_commit = initialize_git(self.toolkit)
        self.codex = self.root / "fake-codex"
        self.codex.write_text(f"#!{sys.executable}\n" + FAKE_CODEX, encoding="utf-8")
        self.codex.chmod(0o755)

    def run_case(self, name="attempt", timeout=10):
        with patch("benchmark.runs.shutil.which", return_value=str(self.codex)):
            return run_external(
                self.controller, toolkit=self.toolkit, toolkit_ref=self.toolkit_commit,
                label="test", model="test-model", effort="medium", output=self.root / name,
                timeout=timeout, environment_label="offline-test", case_id="handoff", subagent_limit=2,
            )

    def prepare(self, name="prepared"):
        workspace = self.root / name
        metadata = prepare_case(self.controller, self.controller_commit, workspace, "handoff")
        base = initialize_git(workspace)
        return workspace, base, metadata

    def test_preparation_and_solution_patch_use_case_specific_boundaries(self):
        workspace, base, metadata = self.prepare()
        self.assertEqual(metadata["id"], "handoff")
        self.assertTrue((workspace / "shelfctl/cli.py").is_file())
        for excluded in ("stockctl", "opsreport", "statusctl", "cases", "tests_harness"):
            self.assertFalse((workspace / excluded).exists(), excluded)
        first_task = (workspace / "TASK.md").read_text(encoding="utf-8")
        self.assertIn("sku,on_hand,available", first_task)
        self.assertNotIn("warehouse,sku,on_hand,reserved,available", first_task)
        writer = workspace / "shelfctl/writers.py"
        writer.write_text(writer.read_text() + "\n# Final worktree change.\n")
        additions = workspace / "tests_solution"
        additions.mkdir()
        (additions / "test_case.py").write_text("# New solution check.\n")
        wrong_package = workspace / "stockctl"
        wrong_package.mkdir()
        (wrong_package / "unrelated.py").write_text("# Outside this case.\n")
        self.assertEqual(scope_violations(workspace, base), ["stockctl/unrelated.py"])
        change = solution_patch(workspace, base)
        self.assertIn(b"shelfctl/writers.py", change)
        self.assertIn(b"tests_solution/test_case.py", change)
        self.assertNotIn(b"stockctl/unrelated.py", change)

    def test_handoff_uses_two_fresh_processes_with_an_intermediate_checkpoint(self):
        result = self.run_case()
        report = result["report"]
        directory = Path(result["directory"])
        invocations = [json.loads(line) for line in (directory / "workspace/.codex/tmp/fake-invocations.jsonl").read_text().splitlines()]
        self.assertEqual(len(invocations), 2)
        self.assertNotEqual(invocations[0]["pid"], invocations[1]["pid"])
        self.assertNotIn("resume", invocations[1]["argv"])
        self.assertIsNone(invocations[0]["checkpoint_before_process"])
        self.assertTrue(invocations[1]["checkpoint_before_process"]["passed"])
        self.assertEqual(invocations[1]["checkpoint_before_process"]["premature_changes"], [])
        self.assertIn("warehouse,sku,on_hand,reserved,available", invocations[1]["prompt"])
        self.assertIn("sku,on_hand,available", invocations[1]["prompt"])
        self.assertIn("agents.max_concurrent_threads_per_session=2", invocations[0]["argv"])
        self.assertEqual(report["phase_count"], 2)
        self.assertEqual(len(report["agent_execution"]["phases"]), 2)
        self.assertAlmostEqual(report["wall_seconds"], sum(p["wall_seconds"] for p in report["agent_execution"]["phases"]))
        self.assertEqual(report["usage"]["root_input_tokens"], 30)
        self.assertIsNone(report["usage"]["total_tree_tokens"])
        self.assertFalse(report["accepted"], "The fake did not implement the product task")
        criteria = report["acceptance"]["criteria"]
        self.assertTrue(next(item for item in criteria if item["id"] == "plan_before_implementation")["passed"])
        self.assertEqual(report["acceptance"]["aggregate"]["total"], len(criteria))
        self.assertEqual(report["acceptance"]["aggregate"]["passed"], sum(item["passed"] for item in criteria))

    def test_premature_implementation_stops_phase_two_and_cannot_pass(self):
        successful_product = {"passed": True, "criteria": [{"id": "product", "passed": True}],
                              "critical_failures": [], "aggregate": {"passed": 1, "total": 1}}
        with patch.dict(os.environ, {"FAKE_EARLY_IMPLEMENTATION": "1"}), patch("benchmark.runs.grade", return_value=successful_product):
            result = self.run_case()
        report = result["report"]
        self.assertFalse(report["accepted"])
        self.assertEqual(len(report["agent_execution"]["phases"]), 1)
        self.assertFalse((Path(result["directory"]) / "phase-2").exists())
        self.assertFalse(report["phase1_checkpoint"]["passed"])
        self.assertIn("shelfctl/writers.py", report["phase1_checkpoint"]["premature_changes"])

    def test_both_phases_share_one_process_time_budget(self):
        successful_product = {"passed": True, "criteria": [{"id": "product", "passed": True}],
                              "critical_failures": [], "aggregate": {"passed": 1, "total": 1}}
        with patch.dict(os.environ, {"FAKE_DELAY_SECONDS": "0.65"}), patch("benchmark.runs.grade", return_value=successful_product):
            result = self.run_case(timeout=1)
        report = result["report"]
        phases = report["agent_execution"]["phases"]
        self.assertEqual(len(phases), 2)
        self.assertFalse(phases[0]["timed_out"])
        self.assertTrue(phases[1]["timed_out"])
        self.assertEqual(report["status"], "timeout")
        self.assertFalse(report["accepted"])
        self.assertLess(phases[1]["wall_seconds"], phases[0]["wall_seconds"])
        self.assertAlmostEqual(report["wall_seconds"], 1, delta=0.2)

    def test_case_verifier_and_routing_are_taken_from_frozen_input(self):
        workspace, base, _ = self.prepare()
        record = start(workspace, mode="external")
        plan = workspace / ".codex/tmp/plan.md"
        plan.parent.mkdir(parents=True)
        plan.write_text("Plan before implementation.\n", encoding="utf-8")
        self.assertTrue(checkpoint(workspace, record)["passed"])
        (workspace / "verification/handoff_case.py").write_text("raise RuntimeError('edited checker')\n")
        (workspace / "CASE.json").write_text('{"id":"workflow"}\n')
        result = finish(workspace, record=record)
        report = result["report"]
        self.assertEqual(report["case_id"], "handoff")
        self.assertEqual(report["acceptance"]["verifier_commit"], base)
        self.assertNotIn("error", report["acceptance"])
        self.assertIn("updated_csv_columns_order", {item["id"] for item in report["acceptance"]["criteria"]})
        self.assertIn("CASE.json", report["scope_violations"])
        self.assertIn("verification/handoff_case.py", report["scope_violations"])


if __name__ == "__main__":
    unittest.main()
