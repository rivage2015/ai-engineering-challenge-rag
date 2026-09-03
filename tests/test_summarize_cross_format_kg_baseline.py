from __future__ import annotations

import sys
import unittest
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY / "scripts"))

import summarize_cross_format_kg_baseline as baseline


class CrossFormatKgBaselineSummaryTest(unittest.TestCase):
    def test_summary_marks_phase_one_as_not_graph_proof(self) -> None:
        methods = []
        for method in baseline.METHODS:
            methods.append({
                "method": method,
                "metrics": {"all_relevant_at_5": 1.0, "case_count": 1},
                "cases": [{
                    "eval_case_id": "case-1",
                    "all_relevant_at_5": 1,
                    "source_recall_at_5": 1.0,
                    "relevant_sources": ["source.docx"],
                    "retrieved": [{"relative_path": "source.docx"}],
                }],
            })
        report = {
            "coverage": {
                "case_count": 1,
                "dataset_files": 5,
                "formats": ["docx", "pdf", "pptx", "xlsx"],
                "distribution": {"document_count": 5},
                "layer1": {"input_files": 5, "statuses": {"success": 5}},
            },
            "modes": {
                "external_network_used": False,
                "llm_answer_generation": "not_evaluated",
            },
            "expected_phrase_coverage": {
                "distribution": {
                    "all_pass": False,
                    "cases": [{
                        "eval_case_id": "case-1",
                        "missing_phrases": ["2023-04-01"],
                    }],
                },
                "layer1_adapter": {
                    "all_pass": True,
                    "cases": [{
                        "eval_case_id": "case-1",
                        "missing_phrases": [],
                    }],
                },
            },
            "retrieval_comparison": methods,
            "relationship_context_audit": {"all_pass": False, "cases": []},
        }

        result = baseline.summarize(report)

        self.assertEqual("BASELINE_ONLY_NOT_GRAPH_PROOF", result["decision"])
        self.assertEqual(
            "UNDECLARED_NOT_EVALUATED",
            result["graph_and_answer"]["semantic_graph_traversal"],
        )
        self.assertEqual(
            ["2023-04-01"],
            result["expected_phrase_coverage"]["distribution"]
            ["failed_cases"][0]["missing_phrases"],
        )

    def test_summary_rejects_missing_retrieval_method(self) -> None:
        with self.assertRaisesRegex(ValueError, "missing retrieval methods"):
            baseline.summarize({
                "coverage": {},
                "expected_phrase_coverage": {},
                "retrieval_comparison": [],
            })


if __name__ == "__main__":
    unittest.main()
