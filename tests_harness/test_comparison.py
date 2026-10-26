"""Protect the experimental conclusion from optimistic bookkeeping."""

import copy
import json
from pathlib import Path
import tempfile
import unittest

from benchmark.compare import compare, load_reports, render_comparison, summarize


def reports(variant, seconds=100, count=5, task_id="stockctl-v1", limit=2, toolkit=None):
    return [{
        "schema_version": 1, "run_id": f"{variant}-{task_id}-{number}", "task_id": task_id,
        "task_sha256": f"same-task-{task_id}", "benchmark_commit": "same-benchmark",
        "toolkit_commit": toolkit or variant, "model": "test-model", "reasoning_effort": "medium",
        "subagent_limit": limit, "capacity_source": "codex-config",
        "runtime_version": "test-cli", "environment_label": "clean-1",
        "toolkit_profile_runtime_sha256": "same-profile-runtime",
        "environment": {"system": "test"}, "mode": "external", "timeout_seconds": 1800,
        "status": "completed", "accepted": True, "wall_seconds": seconds,
        "acceptance": {"passed": True, "critical_failures": [], "criteria": [
            {"id": "warehouse", "passed": True}, {"id": "export", "passed": True}
        ]}, "scope_violations": [],
        "human_interventions": 0,
    } for number in range(count)]


class ComparisonTests(unittest.TestCase):
    def test_all_failures_count_toward_time_per_accepted_task(self):
        data = reports("a", seconds=100)
        data[0].update(accepted=False, status="timeout", wall_seconds=1800)
        result = summarize(data)
        self.assertEqual(result["accepted"], 4)
        self.assertEqual(result["total_wall_seconds"], 2200)
        self.assertEqual(result["seconds_per_accepted_task"], 550)

    def test_zero_success_is_unknown_not_free(self):
        data = reports("a")
        for report in data:
            report["accepted"] = False
        self.assertIsNone(summarize(data)["seconds_per_accepted_task"])
        self.assertFalse(compare(data, reports("b", seconds=50))["gate_passed"])

    def test_speedup_requires_equal_conditions_and_no_quality_regression(self):
        a, b = reports("a"), reports("b", seconds=80)
        self.assertTrue(compare(a, b)["gate_passed"])
        changed = copy.deepcopy(b)
        changed[0]["model"] = "different-model"
        self.assertFalse(compare(a, changed)["comparable"])
        changed = copy.deepcopy(b)
        changed[0]["toolkit_profile_runtime_sha256"] = "different-child-model"
        self.assertFalse(compare(a, changed)["comparable"])
        changed = copy.deepcopy(b)
        changed[0].update(accepted=False, status="task_failed")
        self.assertFalse(compare(a, changed)["gate_passed"])
        changed = copy.deepcopy(b)
        changed[0]["acceptance"]["critical_failures"] = ["corrupted_data"]
        self.assertFalse(compare(a, changed)["gate_passed"])

    def test_missing_metadata_manual_time_small_sample_and_mixed_candidate_block_claim(self):
        a, b = reports("a"), reports("b", seconds=50)
        for field, value in (("mode", "manual"), ("model", None), ("toolkit_commit", "third-version")):
            changed = copy.deepcopy(b)
            changed[0][field] = value
            self.assertFalse(compare(a, changed)["gate_passed"])
        self.assertFalse(compare(a[:1], b[:1])["gate_passed"])

    def test_one_criterion_regression_cannot_be_offset_by_another_improvement(self):
        a, b = reports("a"), reports("b", seconds=50)
        for group in (a, b):
            group[0].update(accepted=False, status="task_failed")
            group[0]["acceptance"]["passed"] = False
        a[0]["acceptance"]["criteria"][1]["passed"] = False
        b[0]["acceptance"]["criteria"][0]["passed"] = False
        result = compare(a, b)
        self.assertEqual(result["baseline"]["accepted"], result["candidate"]["accepted"])
        self.assertFalse(result["gate_passed"])
        self.assertEqual(result["criterion_changes"]["warehouse"], {"baseline_passes": 5, "candidate_passes": 4})

    def test_duplicate_and_contradictory_reports_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "report.json"
            path.write_text(json.dumps(reports("a")[0]))
            with self.assertRaises(ValueError):
                load_reports([path, path])
            bad = reports("a")[0]
            bad["status"] = "timeout"
            path.write_text(json.dumps(bad))
            with self.assertRaises(ValueError):
                load_reports([path])

    def test_capacity_is_a_distinct_experiment_with_the_same_toolkit(self):
        a = reports("a", limit=1, toolkit="same-toolkit")
        b = reports("b", seconds=60, limit=4, toolkit="same-toolkit")
        result = compare(a, b, study="capacity")
        self.assertTrue(result["gate_passed"])
        self.assertEqual(result["treatment"]["requested_subagent_limit_delta"], 3)
        self.assertIsNone(result["treatment"]["observed_peak_subagents"])
        case = result["by_case"]["stockctl-v1"]
        self.assertAlmostEqual(case["speedup"], 100 / 60)
        self.assertAlmostEqual(case["saved_seconds_per_additional_requested_subagent"], 40 / 3)
        self.assertIn("Фактический пик", render_comparison(result))
        self.assertFalse(compare(a, b, study="toolkit")["gate_passed"])

    def test_experimental_dimensions_cannot_be_changed_together(self):
        a, b = reports("a", limit=1), reports("b", seconds=50, limit=4)
        self.assertFalse(compare(a, b, study="capacity")["comparable"])
        self.assertFalse(compare(a, b, study="toolkit")["comparable"])
        b = reports("b", seconds=50, limit=1, toolkit="a")
        self.assertFalse(compare(a, b, study="capacity")["comparable"])

    def test_capacity_requires_one_known_limit_and_a_configuration_source(self):
        a = reports("a", limit=1, toolkit="same-toolkit")
        b = reports("b", seconds=60, limit=4, toolkit="same-toolkit")
        for field, value in (("subagent_limit", None), ("subagent_limit", 8), ("subagent_limit", True), ("capacity_source", None)):
            changed = copy.deepcopy(b)
            changed[0][field] = value
            self.assertFalse(compare(a, changed, study="capacity")["gate_passed"])

    def test_multiple_cases_keep_their_identity_and_separate_metrics(self):
        a = reports("a", seconds=20, task_id="small-v1") + reports("a", seconds=200, task_id="parallel-v1")
        b = reports("b", seconds=19, task_id="small-v1") + reports("b", seconds=100, task_id="parallel-v1")
        result = compare(a, b)
        self.assertTrue(result["gate_passed"])
        self.assertEqual(set(result["by_case"]), {"small-v1", "parallel-v1"})
        self.assertAlmostEqual(result["by_case"]["small-v1"]["candidate_to_baseline_ratio"], 0.95)
        self.assertFalse(result["by_case"]["small-v1"]["gate_passed"])
        self.assertTrue(result["by_case"]["parallel-v1"]["gate_passed"])
        self.assertEqual(result["baseline"]["criterion_pass_counts"], {})
        self.assertIn("small-v1/warehouse", result["criterion_changes"])
        self.assertIn("parallel-v1/warehouse", result["criterion_changes"])

    def test_case_regression_cannot_be_offset_by_other_case_improvement(self):
        a = reports("a", task_id="small-v1") + reports("a", task_id="parallel-v1")
        b = reports("b", seconds=40, task_id="small-v1") + reports("b", seconds=40, task_id="parallel-v1")
        a[5].update(accepted=False, status="task_failed")
        a[5]["acceptance"]["passed"] = False
        a[5]["acceptance"]["criteria"][0]["passed"] = False
        b[0].update(accepted=False, status="task_failed")
        b[0]["acceptance"]["passed"] = False
        b[0]["acceptance"]["criteria"][0]["passed"] = False
        result = compare(a, b)
        self.assertEqual(result["baseline"]["accepted"], result["candidate"]["accepted"])
        self.assertFalse(result["gate_passed"])
        self.assertFalse(result["by_case"]["small-v1"]["quality_floor_passed"])

    def test_missing_case_or_unequal_case_counts_disable_aggregate_timing(self):
        a = reports("a", task_id="small-v1") + reports("a", task_id="parallel-v1")
        b = reports("b", seconds=20, task_id="small-v1")
        result = compare(a, b)
        self.assertFalse(result["aggregate_balanced"])
        self.assertIsNone(result["candidate_to_baseline_ratio"])
        self.assertFalse(result["comparable"])
        self.assertIn("Общий вывод по времени не вычислен", render_comparison(result))
        b += reports("b", seconds=20, count=6, task_id="parallel-v1")
        self.assertFalse(compare(a, b)["aggregate_balanced"])

    def test_repetitions_are_required_for_every_case_and_hashes_match_per_case(self):
        a = reports("a", count=9, task_id="small-v1") + reports("a", count=1, task_id="parallel-v1")
        b = reports("b", count=9, seconds=40, task_id="small-v1") + reports("b", count=1, seconds=40, task_id="parallel-v1")
        self.assertFalse(compare(a, b)["comparable"])
        a, b = reports("a"), reports("b", seconds=50)
        b[0]["task_sha256"] = "different-prompt"
        self.assertFalse(compare(a, b)["comparable"])

    def test_global_model_mismatch_also_invalidates_each_case_gate(self):
        a, b = reports("a"), reports("b", seconds=50)
        for report in b:
            report["model"] = "other-model"
        result = compare(a, b)
        self.assertFalse(result["gate_passed"])
        self.assertFalse(result["by_case"]["stockctl-v1"]["gate_passed"])

    def test_zero_measured_time_is_not_an_infinite_speedup(self):
        result = compare(reports("a"), reports("b", seconds=0))
        self.assertFalse(result["gate_passed"])
        self.assertIsNone(result["candidate_to_baseline_ratio"])


if __name__ == "__main__":
    unittest.main()
