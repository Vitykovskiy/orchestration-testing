"""Exercise experiment planning, failure accounting and capacity comparisons offline."""

from collections import Counter, defaultdict
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from benchmark.catalog import CASES
from benchmark.matrix import planned_order, run_matrix
from benchmark.runs import write_json
from benchmark.workspace import initialize_git


SOURCE = Path(__file__).resolve().parents[1]


class MatrixTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="matrix-test-", dir=SOURCE.parent)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.controller = self.root / "controller"
        self.toolkit = self.root / "toolkit"
        for repository in (self.controller, self.toolkit):
            repository.mkdir()
            (repository / "README.md").write_text("Test snapshot\n", encoding="utf-8")
        (self.controller / ".gitignore").write_text(".benchmark/\n", encoding="utf-8")
        for case_id, settings in CASES.items():
            task_path = "TASK.md" if case_id == "workflow" else f"cases/{case_id}/TASK.md"
            task = self.controller / task_path
            task.parent.mkdir(parents=True, exist_ok=True)
            task.write_text(f"Task {case_id}\n", encoding="utf-8")
            verifier = self.controller / settings["verifier"]
            verifier.parent.mkdir(parents=True, exist_ok=True)
            verifier.write_text("# Fixture checker; mocked agent runs never execute it.\n", encoding="utf-8")
        self.benchmark_ref = initialize_git(self.controller)
        self.toolkit_ref = initialize_git(self.toolkit)
        self.calls = []

    def arguments(self, **overrides):
        values = dict(
            toolkit=self.toolkit, toolkit_ref=self.toolkit_ref, model="test-model", effort="medium",
            environment_label="clean-test-machine", output=self.root / "matrix", cases=["small", "parallel"],
            limits=[1, 2, 4], repeats=5, timeout=1800, seed=123,
        )
        values.update(overrides)
        return values

    def fake_run(self, repository, **kwargs):
        self.calls.append(kwargs)
        output = kwargs["output"]
        self.assertFalse(output.exists(), "Every attempt needs a new output directory")
        output.mkdir(parents=True)
        case_id = kwargs["case_id"]
        report = {
            "schema_version": 1, "run_id": output.name, "case_id": case_id,
            "task_id": CASES[case_id]["task_id"], "task_sha256": "task-" + case_id,
            "benchmark_commit": self.benchmark_ref, "toolkit_commit": self.toolkit_ref,
            "toolkit_profile_runtime_sha256": "same-profiles", "model": kwargs["model"],
            "reasoning_effort": kwargs["effort"], "runtime_version": "fake-cli",
            "environment_label": kwargs["environment_label"], "environment": {"system": "test"},
            "mode": "external", "timeout_seconds": kwargs["timeout"],
            "subagent_limit": kwargs["subagent_limit"], "capacity_source": "codex-config",
            "status": "completed", "accepted": True,
            "wall_seconds": 100 / kwargs["subagent_limit"], "human_interventions": 0,
            "scope_violations": [],
            "acceptance": {"passed": True, "criteria": [{"id": "functional", "passed": True}], "critical_failures": []},
        }
        write_json(output / "report.json", report)
        return {"report": report, "directory": str(output)}

    def test_plan_is_deterministic_balanced_and_keeps_variants_adjacent(self):
        cases, limits = ["small", "workflow", "parallel", "handoff"], [1, 2, 4]
        first = planned_order(cases, limits, 6, 101)
        self.assertEqual(first, planned_order(cases, limits, 6, 101))
        self.assertNotEqual(first, planned_order(cases, limits, 6, 102))
        self.assertEqual(len(first), 4 * 3 * 6)
        counts = Counter((item["case_id"], item["subagent_limit"]) for item in first)
        self.assertEqual(set(counts.values()), {6})
        positions = defaultdict(Counter)
        for offset in range(0, len(first), len(limits)):
            block = first[offset:offset + len(limits)]
            self.assertEqual(len({(item["case_id"], item["repeat"]) for item in block}), 1)
            for position, item in enumerate(block):
                positions[(item["case_id"], item["subagent_limit"])][position] += 1
        self.assertTrue(all(set(values.values()) == {2} for values in positions.values()))

    def test_plan_only_writes_budget_without_codex_or_run_calls(self):
        with patch("benchmark.matrix.shutil.which", side_effect=AssertionError("No CLI probe for a plan")), patch("benchmark.matrix.run_external") as run:
            result = run_matrix(self.controller, **self.arguments(plan_only=True))
        run.assert_not_called()
        summary = result["summary"]
        self.assertEqual(summary["status"], "planned")
        self.assertEqual(summary["planned_runs"], 30)
        self.assertEqual(summary["maximum_agent_wall_seconds"], 30 * 1800)
        self.assertFalse(summary["gate_passed"])
        self.assertTrue(Path(result["plan"]).exists())
        self.assertTrue(Path(result["markdown"]).exists())
        with self.assertRaisesRegex(ValueError, "new directory"):
            run_matrix(self.controller, **self.arguments(plan_only=True))

    def test_matrix_uses_all_cases_caps_repeats_and_one_frozen_plan(self):
        plan_bytes = []

        def run(repository, **kwargs):
            plan_bytes.append((self.root / "matrix/matrix-plan.json").read_bytes())
            return self.fake_run(repository, **kwargs)

        with patch("benchmark.matrix.shutil.which", return_value="/fake/codex"), patch("benchmark.matrix.run_external", side_effect=run):
            result = run_matrix(self.controller, **self.arguments())
        summary = result["summary"]
        self.assertEqual(len(self.calls), 30)
        self.assertEqual(len(set(plan_bytes)), 1)
        self.assertEqual(len({call["output"] for call in self.calls}), 30)
        self.assertTrue(summary["complete"])
        self.assertEqual(summary["supported_improvement_limits"], [2, 4])
        self.assertEqual(summary["by_case"]["parallel"]["limits"]["4"]["speedup_vs_minimum_limit"], 4)
        self.assertIsNone(summary["observed_peak_subagents"])

    def test_failed_task_reports_are_counted_in_quality_and_cost(self):
        failed_once = False

        def run(repository, **kwargs):
            nonlocal failed_once
            result = self.fake_run(repository, **kwargs)
            if kwargs["case_id"] == "parallel" and kwargs["subagent_limit"] == 2 and not failed_once:
                failed_once = True
                report = result["report"]
                report.update(status="timeout", accepted=False, wall_seconds=1800)
                report["acceptance"]["passed"] = False
                report["acceptance"]["criteria"][0]["passed"] = False
                write_json(kwargs["output"] / "report.json", report)
            return result

        with patch("benchmark.matrix.shutil.which", return_value="/fake/codex"), patch("benchmark.matrix.run_external", side_effect=run):
            summary = run_matrix(self.controller, **self.arguments())["summary"]
        cell = summary["by_case"]["parallel"]["limits"]["2"]
        self.assertTrue(summary["complete"])
        self.assertEqual(cell["accepted"], 4)
        self.assertEqual(cell["reported_attempts"], 5)
        self.assertEqual(cell["total_wall_seconds"], 2000)
        self.assertEqual(cell["seconds_per_accepted_task"], 500)
        self.assertFalse(summary["comparisons"]["2"]["gate_passed"])

    def test_unknown_cost_exception_is_retained_and_blocks_positive_conclusions(self):
        called = 0

        def run(repository, **kwargs):
            nonlocal called
            called += 1
            if called == 2:
                raise OSError("Simulated launch failure")
            return self.fake_run(repository, **kwargs)

        with patch("benchmark.matrix.shutil.which", return_value="/fake/codex"), patch("benchmark.matrix.run_external", side_effect=run):
            summary = run_matrix(self.controller, **self.arguments())["summary"]
        self.assertEqual(called, 30)
        self.assertEqual(summary["status"], "incomplete")
        self.assertEqual(summary["incomplete_runs"], 1)
        self.assertEqual(summary["reported_runs"], 29)
        self.assertFalse(summary["gate_passed"])
        self.assertTrue(all(not value["gate_passed"] for value in summary["comparisons"].values()))
        failure = next(a for a in summary["attempts"] if a["status"] == "incomplete")
        self.assertTrue((self.root / "matrix" / failure["error_file"]).exists())
        cell = summary["by_case"][failure["case_id"]]["limits"][str(failure["subagent_limit"])]
        self.assertIsNone(cell["seconds_per_accepted_task"])
        self.assertIsNone(cell["total_wall_seconds"])

    def test_one_repeat_pilot_cannot_claim_improvement(self):
        with patch("benchmark.matrix.shutil.which", return_value="/fake/codex"), patch("benchmark.matrix.run_external", side_effect=self.fake_run):
            summary = run_matrix(self.controller, **self.arguments(repeats=1))["summary"]
        self.assertTrue(summary["complete"])
        self.assertFalse(summary["gate_passed"])
        self.assertEqual(summary["supported_improvement_limits"], [])

    def test_dirty_source_and_missing_codex_fail_before_launch(self):
        with patch("benchmark.matrix.shutil.which", return_value=None), patch("benchmark.matrix.run_external") as run:
            with self.assertRaisesRegex(ValueError, "Codex CLI"):
                run_matrix(self.controller, **self.arguments())
        run.assert_not_called()
        self.assertFalse((self.root / "matrix").exists())
        (self.controller / "new.txt").write_text("dirty", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "clean"):
            run_matrix(self.controller, **self.arguments(plan_only=True))


if __name__ == "__main__":
    unittest.main()
