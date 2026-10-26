"""Protect the experimental conclusion from optimistic bookkeeping."""

import copy
import json
from pathlib import Path
import tempfile
import unittest

from benchmark.compare import compare, load_reports, summarize


def reports(variant, seconds=100, count=5):
    return [{
        "schema_version": 1, "run_id": f"{variant}-{number}", "task_id": "stockctl-v1",
        "task_sha256": "same-task", "benchmark_commit": "same-benchmark",
        "toolkit_commit": variant, "model": "test-model", "reasoning_effort": "medium",
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


if __name__ == "__main__":
    unittest.main()
