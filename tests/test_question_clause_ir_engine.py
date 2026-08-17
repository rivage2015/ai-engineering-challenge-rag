"""Semantic tests for the deterministic QuestionClauseIR engine.

The fixtures contain only synthetic questions and compiler contracts.  They do
not read competition questions, answers, source files, or retrieval results.
"""

from __future__ import annotations

import copy
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from build_question_clause_ir import build_question_clause_ir
from build_question_understanding import build_question_understanding
from validate_question_clause_ir import (
    load_strict_json,
    validate_question_clause_ir,
)
from tests.test_question_understanding_engine import (
    compile_fixture,
    generic_compound_fixture,
    generic_list_fixture,
)


STAMP = "2026-08-17T00:00:00Z"


class QuestionClauseIREngineTest(unittest.TestCase):
    def _list(self):
        question, draft = generic_list_fixture("qcir_engine")
        run = compile_fixture(question, draft)
        return question, run["question_intent_contract"]

    def _compound(self):
        question, draft = generic_compound_fixture("qcir_engine")
        run = compile_fixture(question, draft)
        return question, run["question_intent_contract"]

    def test_three_certified_profiles_are_complete_and_qic_bound(self):
        list_question, list_qic = self._list()
        compound_question, compound_qic = self._compound()
        suffix_question = {
            "question_id": "q_suffix_qcir_engine",
            "original_question": (
                "組織UVのledger_v7.xlsxにおいて、確認済みフェーズに一致する"
                "RowIDをすべて挙げてください。"
            ),
        }
        suffix_run = build_question_understanding(suffix_question)
        self.assertEqual("ready_for_retrieval", suffix_run["final_status"])
        cases = (
            (list_question, list_qic, "list_eq_id_all_v0_1"),
            (
                suffix_question,
                suffix_run["question_intent_contract"],
                "list_suffix_eq_id_all_v0_1",
            ),
            (
                compound_question,
                compound_qic,
                "compound_eq_gt_mean_nearest_id_all_v0_1",
            ),
        )
        for question, qic, profile in cases:
            with self.subTest(profile=profile):
                record = build_question_clause_ir(
                    question, generated_at=STAMP, qic=qic
                )
                self.assertEqual(profile, record["grammar_profile"])
                self.assertEqual("complete", record["coverage"]["status"])
                self.assertEqual(
                    len(question["original_question"]),
                    record["coverage"]["covered_codepoints"],
                )
                self.assertEqual([], validate_question_clause_ir(record, qic))

    def test_unsupported_question_is_preserved_as_incomplete(self):
        question = {
            "question_id": "q_unsupported_qcir",
            "original_question": "重要度をよしなに判断してください。",
        }
        record = build_question_clause_ir(question, generated_at=STAMP)
        self.assertEqual("unsupported_v0_1", record["grammar_profile"])
        self.assertEqual("incomplete", record["coverage"]["status"])
        self.assertEqual(0, record["coverage"]["covered_codepoints"])
        self.assertEqual("unresolved", record["clauses"][0]["role"])
        self.assertEqual([], validate_question_clause_ir(record))

    def test_opaque_literals_and_ids_are_stable(self):
        question, qic = self._list()
        first = build_question_clause_ir(
            question, generated_at="2026-08-17T00:00:00Z", qic=qic
        )
        second = build_question_clause_ir(
            question, generated_at="2026-08-17T01:00:00Z", qic=qic
        )
        self.assertEqual(
            first["question_clause_ir_id"], second["question_clause_ir_id"]
        )
        self.assertEqual(
            [item["clause_id"] for item in first["clauses"]],
            [item["clause_id"] for item in second["clauses"]],
        )
        self.assertNotEqual(
            first["provenance"]["generated_at"],
            second["provenance"]["generated_at"],
        )

    def test_span_coverage_reference_id_and_registry_tampering_is_rejected(self):
        question, qic = self._list()
        baseline = build_question_clause_ir(question, generated_at=STAMP, qic=qic)
        mutations = []
        value = copy.deepcopy(baseline)
        value["clauses"][0]["span"]["text"] = "tampered"
        mutations.append(value)
        value = copy.deepcopy(baseline)
        value["coverage"]["covered_codepoints"] -= 1
        mutations.append(value)
        value = copy.deepcopy(baseline)
        value["clauses"][0]["clause_id"] = "qcl_" + "f" * 20
        mutations.append(value)
        value = copy.deepcopy(baseline)
        value["provenance"]["registry_sha256"] = "f" * 64
        mutations.append(value)
        value = copy.deepcopy(baseline)
        value["question_clause_ir_id"] = "qcir_" + "e" * 20
        mutations.append(value)
        for index, mutation in enumerate(mutations):
            with self.subTest(index=index):
                self.assertTrue(validate_question_clause_ir(mutation, qic))

    def test_qic_value_and_conjunction_topology_tampering_is_rejected(self):
        list_question, list_qic = self._list()
        list_ir = build_question_clause_ir(
            list_question, generated_at=STAMP, qic=list_qic
        )
        bad_list_qic = copy.deepcopy(list_qic)
        bad_list_qic["requested"]["scope"]["filters"][0]["value"] = "other"
        self.assertTrue(validate_question_clause_ir(list_ir, bad_list_qic))

        compound_question, compound_qic = self._compound()
        compound_ir = build_question_clause_ir(
            compound_question, generated_at=STAMP, qic=compound_qic
        )
        bad_compound_qic = copy.deepcopy(compound_qic)
        bad_compound_qic["requested"]["operation_graph"]["edges"] = [
            edge
            for edge in bad_compound_qic["requested"]["operation_graph"]["edges"]
            if not (
                edge["from"] == "op_000_filter"
                and edge["to"] == "op_001_filter"
            )
        ]
        errors = validate_question_clause_ir(compound_ir, bad_compound_qic)
        self.assertTrue(
            any("conjunction topology" in error for error in errors), errors
        )

    def test_strict_json_rejects_duplicate_nonfinite_and_deep_inputs(self):
        with self.assertRaises(ValueError):
            load_strict_json('{"a":1,"a":2}')
        with self.assertRaises(ValueError):
            load_strict_json('{"a":NaN}')
        with self.assertRaises(ValueError):
            load_strict_json("[" * 80 + "0" + "]" * 80, max_depth=64)

    def test_engine_has_no_representative_source_literals(self):
        text = "\n".join(
            (ROOT / "scripts" / name).read_text(encoding="utf-8")
            for name in (
                "build_question_clause_ir.py",
                "validate_question_clause_ir.py",
            )
        )
        for forbidden in (
            "青葉与信",
            "青葉バイオメディカル",
            "EducationField",
            "Marketing",
            "MonthlyIncome",
            "train.csv",
        ):
            self.assertNotIn(forbidden, text)


if __name__ == "__main__":
    unittest.main()
