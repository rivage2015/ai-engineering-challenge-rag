from __future__ import annotations

import hashlib
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
DOCX_BUILDER = REPO / "scripts" / "build_general_memory_docx_fixtures.py"


class GeneralMemoryShadowEvaluationTest(unittest.TestCase):
    def test_docx_fixture_builder_is_byte_stable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "office"
            command = [sys.executable, str(DOCX_BUILDER), "--out", str(output)]
            first = subprocess.run(command, cwd=REPO, text=True, capture_output=True, check=False)
            self.assertEqual(first.returncode, 0, first.stderr)
            first_hashes = {
                path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                for path in sorted(output.glob("*.docx"))
            }
            second = subprocess.run(command, cwd=REPO, text=True, capture_output=True, check=False)
            self.assertEqual(second.returncode, 0, second.stderr)
            second_hashes = {
                path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                for path in sorted(output.glob("*.docx"))
            }
            self.assertEqual(len(first_hashes), 3)
            self.assertEqual(first_hashes, second_hashes)

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
            self.assertEqual(report["coverage"]["case_count"], 12)
            self.assertEqual(report["coverage"]["dataset_files"], 19)
            self.assertIn("docx", report["coverage"]["formats"])
            self.assertIn("xlsx", report["coverage"]["formats"])
            self.assertIn("pptx", report["coverage"]["formats"])
            self.assertNotIn("docx", report["coverage"]["not_yet_covered"])
            self.assertNotIn("xlsx", report["coverage"]["not_yet_covered"])
            self.assertNotIn("pptx", report["coverage"]["not_yet_covered"])
            self.assertFalse(report["modes"]["external_network_used"])
            self.assertTrue(report["safety_audit"]["all_pass"])
            safety_cases = report["safety_audit"]["distribution_gate"]
            self.assertEqual(len(safety_cases), 4)
            for safety in safety_cases:
                self.assertEqual(safety["distribution_actual"], "quarantine")
                self.assertEqual(safety["adapter_actual"], "quarantine")
                self.assertIn("priority_override", safety["distribution_risk_reasons"])
                self.assertIn("priority_override", safety["adapter_risk_reasons"])
                self.assertFalse(safety["distribution_safe_stream_exposed_source"])
                self.assertFalse(safety["adapter_safe_stream_exposed_source"])
                self.assertTrue(safety["layer1_raw_retrieval_exposed_source"])
            self.assertTrue(report["expected_phrase_coverage"]["distribution"]["all_pass"])
            self.assertTrue(report["expected_phrase_coverage"]["layer1_adapter"]["all_pass"])
            self.assertTrue(report["relationship_context_audit"]["all_pass"])
            self.assertEqual(
                report["coverage"]["layer1_adapter"]["search_unit_projection"]["included_unit_types"],
                ["table_row"],
            )
            self.assertGreater(
                report["coverage"]["layer1_adapter"]["search_unit_projection"]["count"], 0,
            )
            methods = {item["method"]: item for item in report["retrieval_comparison"]}
            self.assertEqual(set(methods), {
                "distribution-lexical-token-proxy",
                "layer1-real-bm25",
                "layer1-adapter-document-support-through-distribution-safe-stream-proxy",
            })
            for method in methods.values():
                self.assertEqual(method["metrics"]["case_count"], 8)
                self.assertIn("source_recall_at_5", method["metrics"])
                for case in method["cases"]:
                    self.assertTrue(case["relevant_sources"])
                    for item in case["retrieved"]:
                        self.assertIsInstance(item["relative_path"], str)
                    paths = [item["relative_path"] for item in case["retrieved"]]
                    self.assertEqual(len(paths), len(set(paths)))
            adapter_docx = next(
                case for case in methods[
                    "layer1-adapter-document-support-through-distribution-safe-stream-proxy"
                ]["cases"]
                if case["eval_case_id"] == "gm_docx_final_lecture_plan"
            )
            self.assertEqual(adapter_docx["first_relevant_rank"], 1)
            self.assertEqual(
                [item["relative_path"] for item in adapter_docx["retrieved"][:2]],
                [
                    "office/regional-ai-lecture-final.docx",
                    "office/regional-ai-lecture-old.docx",
                ],
            )
            xlsx_relationship = next(
                case for case in report["relationship_context_audit"]["cases"]
                if case["eval_case_id"] == "gm_xlsx_final_onboarding_row"
            )
            self.assertTrue(xlsx_relationship["pass"])
            xlsx_case = next(
                case for case in methods["layer1-real-bm25"]["cases"]
                if case["eval_case_id"] == "gm_xlsx_final_onboarding_row"
            )
            self.assertEqual(xlsx_case["first_relevant_rank"], 1)
            pptx_relationship = next(
                case for case in report["relationship_context_audit"]["cases"]
                if case["eval_case_id"] == "gm_pptx_final_onboarding_decision"
            )
            self.assertTrue(pptx_relationship["pass"])
            self.assertTrue(pptx_relationship["slide_groups"][0]["matches"])
            self.assertEqual(pptx_relationship["slide_groups"][0]["matches"][0]["slide_number"], 2)
            self.assertTrue(pptx_relationship["spatial_relations"][0]["matches"])
            spatial_match = pptx_relationship["spatial_relations"][0]["matches"][0]
            self.assertEqual(spatial_match["slide_number"], 2)
            self.assertLessEqual(
                spatial_match["from_geometry"]["y"] + spatial_match["from_geometry"]["height"],
                spatial_match["to_geometry"]["y"],
            )
            pptx_case = next(
                case for case in methods["layer1-real-bm25"]["cases"]
                if case["eval_case_id"] == "gm_pptx_final_onboarding_decision"
            )
            self.assertEqual(pptx_case["first_relevant_rank"], 1)

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

    def test_adapter_projects_only_verified_table_row_relationships(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            temporary_path = Path(temporary)
            corpus = temporary_path / "corpus"
            shutil.copytree(DATASET / "corpus", corpus)
            intermediate = temporary_path / "intermediate"
            search = temporary_path / "search"
            for command in (
                [
                    sys.executable, str(REPO / "scripts" / "build_intermediate_records.py"),
                    "--root", str(corpus), "--out", str(intermediate),
                    "--run-at", "2026-08-27T00:00:00+00:00",
                ],
                [
                    sys.executable, str(REPO / "scripts" / "build_search_units.py"),
                    "--intermediate", str(intermediate), "--out", str(search),
                ],
            ):
                process = subprocess.run(command, cwd=REPO, text=True, capture_output=True, check=False)
                self.assertEqual(process.returncode, 0, process.stderr)
            output = temporary_path / "adapter"
            adapt = subprocess.run(
                [
                    sys.executable, str(REPO / "scripts" / "adapt_layer1_to_local_memory.py"),
                    "--intermediate", str(intermediate), "--search-output", str(search),
                    "--source-root", str(corpus), "--out", str(output),
                ],
                cwd=REPO, text=True, capture_output=True, check=False,
            )
            self.assertEqual(adapt.returncode, 0, adapt.stderr)
            records = [
                json.loads(line)
                for line in (output / "semantic-evidence.jsonl").read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            row_records = [
                item for item in records
                if item.get("adapter", {}).get("source_record_type") == "search_unit"
            ]
            self.assertTrue(row_records)
            self.assertEqual({item["adapter"]["unit_type"] for item in row_records}, {"table_row"})
            for item in row_records:
                self.assertTrue(item["adapter"]["source_evidence_ids"])
                self.assertEqual(item["adapter"]["execution_policy"], "never_execute")

    def test_adapter_rejects_search_unit_with_dangling_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            temporary_path = Path(temporary)
            corpus = temporary_path / "corpus"
            shutil.copytree(DATASET / "corpus", corpus)
            intermediate = temporary_path / "intermediate"
            search = temporary_path / "search"
            for command in (
                [
                    sys.executable, str(REPO / "scripts" / "build_intermediate_records.py"),
                    "--root", str(corpus), "--out", str(intermediate),
                    "--run-at", "2026-08-27T00:00:00+00:00",
                ],
                [
                    sys.executable, str(REPO / "scripts" / "build_search_units.py"),
                    "--intermediate", str(intermediate), "--out", str(search),
                ],
            ):
                process = subprocess.run(command, cwd=REPO, text=True, capture_output=True, check=False)
                self.assertEqual(process.returncode, 0, process.stderr)
            units_path = search / "search_units.jsonl"
            units = [json.loads(line) for line in units_path.read_text(encoding="utf-8").splitlines() if line.strip()]
            units[0]["source_evidence_ids"] = ["ev_00000000000000000000000000000000"]
            units_path.write_text(
                "".join(json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n" for item in units),
                encoding="utf-8",
            )
            adapt = subprocess.run(
                [
                    sys.executable, str(REPO / "scripts" / "adapt_layer1_to_local_memory.py"),
                    "--intermediate", str(intermediate), "--search-output", str(search),
                    "--source-root", str(corpus), "--out", str(temporary_path / "adapter"),
                ],
                cwd=REPO, text=True, capture_output=True, check=False,
            )
            self.assertNotEqual(adapt.returncode, 0)
            self.assertIn("dangling or cross-document Evidence", adapt.stderr)


if __name__ == "__main__":
    unittest.main()
