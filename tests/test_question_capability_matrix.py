from __future__ import annotations

import copy
import csv
import json
import sys
import tempfile
import unittest
import unicodedata
from pathlib import Path

import jsonschema


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import build_question_capability_matrix as matrix


class FakeResult:
    answer = "T01、T02"
    source_paths = ("project/schedule.xlsx",)
    source_sha256 = "a" * 64
    operation_count = 2


class FakeDecision:
    def __init__(self, status: str, reason: str, result: object | None = None) -> None:
        self.status = status
        self.reason = reason
        self.result = result


class FakePlan:
    strict_status = "pass"
    strict_reasons = ("extended_graph_certified",)
    advisory_usable = True
    fallback_used = False
    retrieval_queries = (object(),)


class FakeEngine:
    def decide_from_graph(
        self, question_id: str, question: str, plan: object
    ) -> FakeDecision:
        del question_id, question, plan
        return FakeDecision("resolved", "certified_extended", FakeResult())


class FakeHoldEngine:
    def decide_from_graph(
        self, question_id: str, question: str, plan: object
    ) -> FakeDecision:
        del question_id, question, plan
        return FakeDecision("hold", "graph_not_structured")


def current_record(state: str) -> dict[str, object]:
    base: dict[str, object] = {
        "state": state,
        "graph_plan_version": "0.4",
        "graph_strict_status": "pass" if state == "certified" else "hold",
        "graph_strict_reasons": (
            ["extended_graph_certified"]
            if state == "certified"
            else ["question_equivalence_unproven", "generic_advisory_graph"]
        ),
        "advisory_usable": state != "error",
        "fallback_used": False,
        "branch_count": 1 if state != "error" else 0,
        "candidate_version": "0.1",
        "decision_status": "resolved" if state == "certified" else "hold",
        "decision_reason": (
            "certified_extended" if state == "certified" else "graph_not_structured"
        ),
        "graph_rule_version": "1.2" if state == "certified" else None,
        "rule_id": "fixture_rule" if state == "certified" else None,
        "graph_contract_id": "xgraph_fixture" if state == "certified" else None,
        "contract_rebuild_valid": True if state == "certified" else None,
        "output_validation_status": "pass" if state == "certified" else "not_run",
        "violations": [],
        "answer_sha256": "b" * 64 if state == "certified" else None,
        "source_paths": ["project/schedule.xlsx"] if state == "certified" else [],
        "source_sha256": "a" * 64 if state == "certified" else None,
        "operation_count": 2 if state == "certified" else None,
        "error": None,
    }
    if state == "error":
        base.update(
            {
                "graph_strict_status": "error",
                "graph_strict_reasons": ["graph_execution_error"],
                "decision_status": "not_run",
                "decision_reason": "graph_execution_error",
                "error": "fixture failure",
            }
        )
    return base


class QuestionCapabilityMatrixTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="aiec-qcm-")
        self.root = Path(self.temporary.name)
        self.questions = self.root / "questions_test.csv"
        with self.questions.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["index", "question"])
            writer.writerow(["0", "計画.xlsxの黄色ハイライト行をすべて答えてください。"])
            writer.writerow(["1", "報告.pdfのグラフで最大値を計算してください。"])
            writer.writerow(["2", "旧版と最新版を比較して変更内容を挙げてください。"])
        self.question_rows = matrix.read_questions(
            self.questions, expected_question_count=3
        )
        self.run_log = self.root / "fourth-run.json"
        self.audit = self.root / "test_delta_audit_20260817_v16.json"
        self.source_root = self.root / "source"
        self.source_root.mkdir()
        (self.source_root / "source.xlsx").write_bytes(b"opaque source fixture")
        answers = [
            {
                "index": "0",
                "質問": self.question_rows[0]["question"],
                "回答": "T01、T02",
                "回答経路": "structured-candidate",
                "question_graph": {
                    "output_validation": {"validation_status": "pass"}
                },
                "structured_candidate_decision": {
                    "status": "resolved",
                    "reason": "certified_extended",
                },
                "参照資料": ["project/schedule.xlsx / Sheet1"],
            },
            {
                "index": "1",
                "質問": self.question_rows[1]["question"],
                "回答": "BASELINE_UNVERIFIED_SECRET",
                "回答経路": "question-graph-layer1-hybrid",
                "question_graph": {
                    "output_validation": {"validation_status": "pass"}
                },
                "structured_candidate_decision": {
                    "status": "hold",
                    "reason": "graph_not_structured",
                },
                "参照資料": ["project/report.pdf / page=1"],
            },
            {
                "index": "2",
                "質問": self.question_rows[2]["question"],
                "回答": "わかりません",
                "回答経路": "question-graph-layer1-hybrid",
                "question_graph": {
                    "output_validation": {"validation_status": "pass"}
                },
                "structured_candidate_decision": {
                    "status": "hold",
                    "reason": "graph_not_structured",
                },
                "参照資料": ["project/old/report.pptx / slide=1"],
            },
        ]
        self.run_log.write_text(
            json.dumps(
                {
                    "モード": "test",
                    "モデル": "fixture-model",
                    "実行日時": "2026-08-17T00:00:00",
                    "質問数": 3,
                    "パラメータ": {
                        "answer_path": "question-graph",
                        "structured_candidate": True,
                    },
                    "回答": answers,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        delta = {
            "fresh_answer": "V16_FRESH_SECRET",
            "index": "1",
            "old_answer": "V16_OLD_SECRET",
            "question": self.question_rows[1]["question"],
            "rationale": "source fixture recomputed",
            "selected_answer": "V16_SELECTED_SECRET",
            "selection": "source_recomputed",
        }
        self.audit.write_text(
            json.dumps(
                {
                    "schema_version": "audited-test-hybrid-0.4",
                    "question_count": 3,
                    "questions_sha256": matrix.sha256_file(self.questions),
                    "selection_policy": (
                        "question and source artifacts only; no valid answers, gold, "
                        "or prior score labels used"
                    ),
                    "audited_deltas": [delta],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_forbidden_inputs_and_answer_columns_are_rejected(self) -> None:
        forbidden = self.root / "questions_valid.csv"
        forbidden.write_text("index,question\n0,x\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "forbidden"):
            matrix.read_questions(forbidden, expected_question_count=1)
        prediction = self.root / "predictions.csv"
        prediction.write_text("0,answer\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "forbidden"):
            matrix.reject_forbidden_input(prediction, "fourth run log")
        gold = self.root / "gold.json"
        gold.write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "forbidden"):
            matrix.reject_forbidden_input(gold, "v16 source audit")
        bad_header = self.root / "questions_test_bad.csv"
        bad_header.write_text("index,question,answer\n0,x,y\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "named exactly"):
            matrix.read_questions(bad_header, expected_question_count=1)

    def test_baseline_and_audit_states_are_separate(self) -> None:
        fourth, _ = matrix.load_fourth_run(self.run_log, self.question_rows)
        audit, _ = matrix.load_v16_source_audit(
            self.audit,
            self.question_rows,
            matrix.sha256_file(self.questions),
        )
        self.assertEqual("machine_certified", matrix.classify_baseline(fourth["0"])["state"])
        self.assertEqual("answered_unverified", matrix.classify_baseline(fourth["1"])["state"])
        self.assertEqual("abstained", matrix.classify_baseline(fourth["2"])["state"])
        self.assertEqual("source_recomputed", matrix.classify_source_audit(audit["1"])["state"])
        self.assertEqual(
            "not_manually_audited", matrix.classify_source_audit(None)["state"]
        )

    def test_execute_current_requires_output_contract_validation(self) -> None:
        contract = {
            "graph_rule_version": "1.2",
            "rule_id": "fixture_rule",
            "graph_contract_id": "xgraph_fixture",
        }
        result = matrix.execute_current(
            "0",
            "fixture",
            FakeEngine(),
            graph_builder=lambda *args, **kwargs: FakePlan(),
            answer_validator=lambda answer, plan: (),
            contract_builder=lambda question: contract,
            contract_validator=lambda question, value: True,
        )
        self.assertEqual("certified", result["state"])
        self.assertEqual("pass", result["output_validation_status"])
        rejected = matrix.execute_current(
            "0",
            "fixture",
            FakeEngine(),
            graph_builder=lambda *args, **kwargs: FakePlan(),
            answer_validator=lambda answer, plan: ("shape_mismatch",),
            contract_builder=lambda question: contract,
            contract_validator=lambda question, value: True,
        )
        self.assertEqual("error", rejected["state"])
        self.assertEqual("failed", rejected["output_validation_status"])
        unproven = matrix.execute_current(
            "0",
            "fixture",
            FakeHoldEngine(),
            graph_builder=lambda *args, **kwargs: FakePlan(),
            answer_validator=lambda answer, plan: (),
            contract_builder=lambda question: None,
            contract_validator=lambda question, value: False,
        )
        self.assertEqual("unproven", unproven["state"])
        self.assertEqual("not_run", unproven["output_validation_status"])

    def test_generic_capabilities_do_not_depend_on_question_id(self) -> None:
        question = "報告.pdfのグラフでx軸の値を計算してください。"
        first = matrix.derive_capabilities(
            question, ("project/report.pdf / page=1",), "unproven"
        )
        second = matrix.derive_capabilities(
            question, ("project/report.pdf / page=1",), "unproven"
        )
        self.assertEqual(first, second)
        self.assertIn("graph_value_recovery", first["tags"])
        self.assertEqual("chart_to_table", first["primary_gap"])
        components = matrix.component_statuses(first, "unproven")
        self.assertEqual("applicable_not_e2e_tested", components["apple_vision"])
        self.assertNotEqual("certified_current", components["docling"])

    def test_reported_paths_are_nfc_and_unicode_spelling_stable(self) -> None:
        nfc = self.root / "共有ドライブ" / "source.json"
        nfd = self.root / unicodedata.normalize("NFD", "共有ドライブ") / "source.json"
        self.assertEqual(matrix.reported_path(nfc), matrix.reported_path(nfd))
        self.assertEqual(
            unicodedata.normalize("NFC", matrix.reported_path(nfd)),
            matrix.reported_path(nfd),
        )

    def test_builds_closed_four_artifact_matrix(self) -> None:
        states = {"0": "certified", "1": "unproven", "2": "error"}

        def fake_current(question_id: str, question: str, engine: object) -> dict[str, object]:
            del question, engine
            return copy.deepcopy(current_record(states[question_id]))

        output = self.root / "output"
        original_glossary = matrix.build_glossary
        try:
            matrix.build_glossary = lambda source_root: object()
            summary = matrix.build_matrix(
                self.questions,
                self.run_log,
                self.audit,
                self.source_root,
                output,
                expected_question_count=3,
                engine_factory=lambda source_root, glossary: object(),
                current_executor=fake_current,
            )
        finally:
            matrix.build_glossary = original_glossary

        self.assertEqual(3, summary["question_count"])
        self.assertEqual(
            {"certified": 1, "error": 1, "unproven": 1},
            summary["counts"]["current"],
        )
        expected = {
            "question-capability-matrix.jsonl",
            "question-capability-matrix.csv",
            "coverage-summary.json",
            "question-capability-matrix.md",
        }
        self.assertEqual(expected, {path.name for path in output.iterdir()})
        schema = json.loads(
            (ROOT / "schemas" / "question-capability-matrix.schema.json").read_text(
                encoding="utf-8"
            )
        )
        validator = jsonschema.Draft202012Validator(schema)
        records = [
            json.loads(line)
            for line in (output / "question-capability-matrix.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        self.assertEqual(3, len(records))
        for record in records:
            validator.validate(record)
            self.assertFalse(record["provenance"]["gold_used"])
            self.assertFalse(record["provenance"]["prediction_files_used"])
        invalid = copy.deepcopy(records[0])
        invalid["unexpected"] = True
        with self.assertRaises(jsonschema.ValidationError):
            validator.validate(invalid)
        emitted = "\n".join(
            (output / name).read_text(encoding="utf-8")
            for name in (
                "question-capability-matrix.jsonl",
                "question-capability-matrix.csv",
                "question-capability-matrix.md",
            )
        )
        self.assertNotIn("BASELINE_UNVERIFIED_SECRET", emitted)
        self.assertNotIn("V16_SELECTED_SECRET", emitted)
        with self.assertRaisesRegex(ValueError, "overwrite"):
            matrix.build_matrix(
                self.questions,
                self.run_log,
                self.audit,
                self.source_root,
                output,
                expected_question_count=3,
                engine_factory=lambda source_root, glossary: object(),
                current_executor=fake_current,
            )


if __name__ == "__main__":
    unittest.main()
