from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
DATASET = REPO / "evaluation" / "general-memory-v0.1"
SCRIPT = REPO / "scripts" / "evaluate_general_memory_shadow.py"


class GeneralMemoryShadowEvaluationTest(unittest.TestCase):
    def test_offline_shadow_evaluation_is_traceable_and_safe(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "shadow"
            process = subprocess.run(
                [sys.executable, str(SCRIPT), "--dataset", str(DATASET), "--out", str(output)],
                cwd=REPO,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(process.returncode, 0, process.stderr)
            report = json.loads((output / "shadow-evaluation-report.json").read_text(encoding="utf-8"))
            self.assertEqual(report["coverage"]["case_count"], 6)
            self.assertEqual(report["coverage"]["dataset_files"], 10)
            self.assertFalse(report["modes"]["external_network_used"])
            self.assertTrue(report["safety_audit"]["all_pass"])
            safety = report["safety_audit"]["distribution_gate"][0]
            self.assertEqual(safety["distribution_actual"], "quarantine")
            self.assertEqual(safety["adapter_actual"], "quarantine")
            self.assertIn("priority_override", safety["distribution_risk_reasons"])
            self.assertIn("priority_override", safety["adapter_risk_reasons"])
            self.assertFalse(safety["distribution_safe_stream_exposed_source"])
            self.assertFalse(safety["adapter_safe_stream_exposed_source"])
            self.assertTrue(safety["layer1_raw_retrieval_exposed_source"])
            self.assertTrue(report["expected_phrase_coverage"]["distribution"]["all_pass"])
            self.assertTrue(report["expected_phrase_coverage"]["layer1_adapter"]["all_pass"])
            methods = {item["method"]: item for item in report["retrieval_comparison"]}
            self.assertEqual(set(methods), {
                "distribution-lexical-token-proxy",
                "layer1-real-bm25",
                "layer1-adapter-through-distribution-safe-stream-proxy",
            })
            for method in methods.values():
                self.assertEqual(method["metrics"]["case_count"], 5)
                self.assertIn("source_recall_at_5", method["metrics"])
                for case in method["cases"]:
                    self.assertTrue(case["relevant_sources"])
                    for item in case["retrieved"]:
                        self.assertIsInstance(item["relative_path"], str)
                    paths = [item["relative_path"] for item in case["retrieved"]]
                    self.assertEqual(len(paths), len(set(paths)))

    def test_adapter_rejects_source_changed_after_layer1_extraction(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            temporary_path = Path(temporary)
            corpus = temporary_path / "corpus"
            shutil.copytree(DATASET / "corpus", corpus)
            intermediate = temporary_path / "intermediate"
            build = subprocess.run(
                [
                    sys.executable, str(REPO / "scripts" / "build_intermediate_records.py"),
                    "--root", str(corpus), "--out", str(intermediate),
                    "--run-at", "2026-08-27T00:00:00+00:00",
                ],
                cwd=REPO, text=True, capture_output=True, check=False,
            )
            self.assertEqual(build.returncode, 0, build.stderr)
            changed = corpus / "storage" / "blue-box.md"
            changed.write_text(changed.read_text(encoding="utf-8") + "\n変更後\n", encoding="utf-8")
            adapt = subprocess.run(
                [
                    sys.executable, str(REPO / "scripts" / "adapt_layer1_to_local_memory.py"),
                    "--intermediate", str(intermediate), "--source-root", str(corpus),
                    "--out", str(temporary_path / "adapter"),
                ],
                cwd=REPO, text=True, capture_output=True, check=False,
            )
            self.assertNotEqual(adapt.returncode, 0)
            self.assertIn("source hash mismatch: storage/blue-box.md", adapt.stderr)


if __name__ == "__main__":
    unittest.main()
