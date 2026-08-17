"""Acceptance tests for the question-only generic advisory compiler.

Competition CSVs are projected to the ``question`` column at read time.  Gold
values are neither loaded nor referenced by these tests.
"""

from __future__ import annotations

import copy
import inspect
import sys
import time
import unittest
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
RAG = ROOT / "rag"
if str(RAG) not in sys.path:
    sys.path.insert(0, str(RAG))

from generic_question_graph import compile_advisory_intent  # noqa: E402


QUESTIONS = ROOT / "share" / "質問回答"


def _questions_only(file_name: str) -> tuple[str, ...]:
    """Load exactly one permitted column from a competition question file."""

    frame = pd.read_csv(
        QUESTIONS / file_name,
        encoding="utf-8-sig",
        usecols=["question"],
        dtype={"question": "string"},
        keep_default_na=False,
    )
    values = tuple(str(value) for value in frame["question"].tolist())
    if not all(value.strip() for value in values):
        raise AssertionError(f"empty question in {file_name}")
    return values


def _nodes(record: dict[str, object]) -> list[dict[str, object]]:
    return record["intent"]["operation_graph"]["nodes"]  # type: ignore[index,return-value]


def _outputs(record: dict[str, object]) -> list[dict[str, object]]:
    return record["intent"]["requested_outputs"]  # type: ignore[index,return-value]


def _operators(record: dict[str, object]) -> tuple[str, ...]:
    return tuple(str(node["operator"]) for node in _nodes(record))


class GenericQuestionGraphAcceptanceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.valid_questions = _questions_only("questions_valid.csv")
        cls.test_questions = _questions_only("questions_test.csv")

    def test_all_130_questions_compile_in_one_second_class(self) -> None:
        questions = (*self.valid_questions, *self.test_questions)
        self.assertEqual(130, len(questions))

        started = time.perf_counter()
        records = [
            compile_advisory_intent(f"corpus-{index:03d}", question)
            for index, question in enumerate(questions)
        ]
        elapsed = time.perf_counter() - started

        self.assertEqual(130, len(records))
        self.assertTrue(all(record["advisory_intent_id"] for record in records))
        self.assertLess(
            elapsed,
            1.0,
            f"130 question-only compilations took {elapsed:.3f}s",
        )

    def test_compilation_is_deterministic_and_question_id_is_audit_only(self) -> None:
        question = "担当タスクIDをすべて挙げてください。"
        first = compile_advisory_intent("audit-a", question)
        repeated = compile_advisory_intent("audit-a", question)
        renamed = compile_advisory_intent("audit-b", question)

        self.assertEqual(first, repeated)
        self.assertEqual(first["advisory_intent_id"], renamed["advisory_intent_id"])
        self.assertEqual(
            first["intent"]["operation_graph"]["operation_graph_id"],
            renamed["intent"]["operation_graph"]["operation_graph_id"],
        )
        without_audit_id = copy.deepcopy(first)
        renamed_without_audit_id = copy.deepcopy(renamed)
        without_audit_id.pop("question_id")
        renamed_without_audit_id.pop("question_id")
        self.assertEqual(without_audit_id, renamed_without_audit_id)

    def test_public_input_and_provenance_exclude_sources_answers_and_predictions(self) -> None:
        self.assertEqual(
            ("question_id", "question"),
            tuple(inspect.signature(compile_advisory_intent).parameters),
        )
        record = compile_advisory_intent("provenance", "タスクIDを答えてください。")
        provenance = record["provenance"]

        self.assertIs(provenance["deterministic"], True)
        self.assertIs(provenance["question_only"], True)
        for field in (
            "source_data_used",
            "answer_data_used",
            "past_answers_used",
            "prediction_data_used",
        ):
            self.assertIn(field, provenance)
            self.assertIs(provenance[field], False)
        self.assertIs(provenance["question_id_affects_semantics"], False)

    def test_explicit_all_and_list_produce_a_list_contract(self) -> None:
        record = compile_advisory_intent(
            "all-list", "該当するタスクIDをすべて挙げてください。"
        )
        output = _outputs(record)[-1]

        self.assertIn("list", _operators(record))
        self.assertEqual("all", output["cardinality"]["mode"])
        self.assertEqual("list", output["answer_shape"]["container"])
        self.assertEqual("identifier", output["answer_shape"]["value_type"])
        self.assertIs(output["inference_basis"]["enforceable"]["cardinality"], True)

    def test_explicit_people_count_produces_integer_count_contract(self) -> None:
        record = compile_advisory_intent("people-count", "担当者は何人ですか。")
        output = _outputs(record)[-1]

        self.assertIn("count", _operators(record))
        self.assertEqual("count", output["return_field"])
        self.assertEqual("single", output["cardinality"]["mode"])
        self.assertEqual(1, output["cardinality"]["expected_count"])
        self.assertEqual("scalar", output["answer_shape"]["container"])
        self.assertEqual("integer", output["answer_shape"]["value_type"])

    def test_decimal_places_are_preserved_as_an_explicit_display_constraint(self) -> None:
        record = compile_advisory_intent(
            "decimal", "改善幅を小数第6位まで答えてください。"
        )
        output = _outputs(record)[-1]

        self.assertEqual(
            {"mode": "decimal_places", "digits": 6},
            output["display_precision"],
        )
        self.assertIs(
            output["inference_basis"]["enforceable"]["display_precision"],
            True,
        )

    def test_nearby_wh_unit_is_attached_to_numeric_output(self) -> None:
        record = compile_advisory_intent(
            "near-unit", "作業工数の合計は何時間ですか。"
        )
        output = _outputs(record)[-1]

        self.assertIn("sum", _operators(record))
        self.assertEqual("時間", output["answer_shape"]["unit"])
        self.assertEqual("number", output["answer_shape"]["value_type"])
        self.assertIs(output["inference_basis"]["enforceable"]["unit"], True)

    def test_q4_confirmation_is_supporting_inspection_not_boolean_output(self) -> None:
        record = compile_advisory_intent("test-q4", self.test_questions[4])
        output = _outputs(record)[-1]
        verify_atoms = [
            atom
            for atom in record["lexical_atoms"]
            if atom["kind"] == "operation" and atom["canonical"] == "verify"
        ]

        self.assertTrue(verify_atoms)
        self.assertTrue(
            all(
                atom.get("details", {}).get("role") == "supporting_inspection"
                for atom in verify_atoms
            )
        )
        self.assertNotIn("verify", _operators(record))
        self.assertNotIn("boolean_test", _operators(record))
        self.assertNotEqual("yes_no", output["answer_shape"]["container"])
        self.assertNotEqual("boolean", output["answer_shape"]["value_type"])

    def test_q8_mean_in_salary_label_is_not_the_requested_terminal(self) -> None:
        record = compile_advisory_intent("test-q8", self.test_questions[8])
        nodes_by_id = {node["operation_id"]: node for node in _nodes(record)}
        mean_atoms = [
            atom
            for atom in record["lexical_atoms"]
            if atom["kind"] == "operation" and atom["canonical"] == "mean"
        ]
        terminal_operators = {
            nodes_by_id[output["source_operation_ref"]]["operator"]
            for output in _outputs(record)
        }

        self.assertTrue(mean_atoms)
        self.assertNotIn("mean", terminal_operators)

    def test_q10_argmax_selects_a_bin_but_is_not_the_requested_count_value(self) -> None:
        record = compile_advisory_intent("test-q10", self.test_questions[10])
        nodes_by_id = {node["operation_id"]: node for node in _nodes(record)}
        output = _outputs(record)[-1]
        terminal_operators = {
            nodes_by_id[candidate["source_operation_ref"]]["operator"]
            for candidate in _outputs(record)
        }

        self.assertNotIn("argmax_all", terminal_operators)
        self.assertEqual({"max"}, terminal_operators)
        self.assertEqual("value", output["return_field"])
        self.assertEqual("scalar", output["answer_shape"]["container"])
        self.assertEqual("number", output["answer_shape"]["value_type"])

    def test_q57_argmax_is_threshold_selection_not_the_requested_f1_value(self) -> None:
        record = compile_advisory_intent("test-q57", self.test_questions[57])
        nodes_by_id = {node["operation_id"]: node for node in _nodes(record)}
        argmax_atoms = [
            atom
            for atom in record["lexical_atoms"]
            if atom["kind"] == "operation" and atom["canonical"] == "argmax_all"
        ]
        terminal_operators = {
            nodes_by_id[output["source_operation_ref"]]["operator"]
            for output in _outputs(record)
        }

        self.assertTrue(argmax_atoms)
        self.assertNotIn("argmax_all", terminal_operators)

    def test_q99_argmax_is_ratio_operand_not_the_requested_ratio(self) -> None:
        record = compile_advisory_intent("test-q99", self.test_questions[99])
        nodes_by_id = {node["operation_id"]: node for node in _nodes(record)}
        argmax_atoms = [
            atom
            for atom in record["lexical_atoms"]
            if atom["kind"] == "operation" and atom["canonical"] == "argmax_all"
        ]
        terminal_operators = {
            nodes_by_id[output["source_operation_ref"]]["operator"]
            for output in _outputs(record)
        }

        self.assertTrue(argmax_atoms)
        self.assertNotIn("argmax_all", terminal_operators)

    def test_q83_value_how_many_plus_decimal_does_not_force_shape(self) -> None:
        record = compile_advisory_intent("test-q83", self.test_questions[83])
        output = _outputs(record)[-1]

        self.assertEqual(
            {"mode": "decimal_places", "digits": 5},
            output["display_precision"],
        )
        self.assertEqual("unknown", output["cardinality"]["mode"])
        self.assertIsNone(output["cardinality"]["expected_count"])
        self.assertEqual("unknown", output["answer_shape"]["container"])
        self.assertIs(
            output["inference_basis"]["enforceable"]["cardinality"],
            False,
        )

    def test_q51_description_word_is_not_explain_and_week_is_single_output(self) -> None:
        record = compile_advisory_intent("test-q51", self.test_questions[51])
        output = _outputs(record)[-1]
        explain_atoms = [
            atom
            for atom in record["lexical_atoms"]
            if atom["kind"] == "operation" and atom["canonical"] == "explain"
        ]

        self.assertTrue(explain_atoms)
        self.assertTrue(
            all(
                atom.get("details", {}).get("role") == "descriptor_mention"
                for atom in explain_atoms
            )
        )
        self.assertNotIn("explain", _operators(record))
        self.assertEqual("single", output["cardinality"]["mode"])
        self.assertEqual(1, output["cardinality"]["expected_count"])
        self.assertEqual("scalar", output["answer_shape"]["container"])
        self.assertEqual("週", output["answer_shape"]["unit"])

    def test_q62_setting_difference_does_not_force_numeric_scalar_shape(self) -> None:
        record = compile_advisory_intent("test-q62", self.test_questions[62])
        output = _outputs(record)[-1]
        absolute_distance_atoms = [
            atom
            for atom in record["lexical_atoms"]
            if atom["kind"] == "operation"
            and atom["canonical"] == "absolute_distance"
        ]

        self.assertTrue(absolute_distance_atoms)
        self.assertTrue(
            all(
                atom.get("details", {}).get("role") == "descriptor_mention"
                for atom in absolute_distance_atoms
            )
        )
        self.assertNotIn("absolute_distance", _operators(record))
        self.assertIn("compare", _operators(record))
        self.assertNotEqual("number", output["answer_shape"]["value_type"])
        self.assertNotEqual("scalar", output["answer_shape"]["container"])

    def test_q66_count_is_metric_mention_and_terminal_is_date_argmax(self) -> None:
        record = compile_advisory_intent("test-q66", self.test_questions[66])
        output = _outputs(record)[-1]
        nodes_by_id = {node["operation_id"]: node for node in _nodes(record)}
        count_atoms = [
            atom
            for atom in record["lexical_atoms"]
            if atom["kind"] == "operation" and atom["canonical"] == "count"
        ]

        self.assertTrue(count_atoms)
        self.assertTrue(
            all(
                atom.get("details", {}).get("role") == "metric_mention"
                for atom in count_atoms
            )
        )
        self.assertNotIn("count", _operators(record))
        self.assertIn("argmax_all", _operators(record))
        self.assertEqual(
            "argmax_all",
            nodes_by_id[output["source_operation_ref"]]["operator"],
        )
        self.assertEqual("single", output["cardinality"]["mode"])
        self.assertEqual("scalar", output["answer_shape"]["container"])
        self.assertEqual("日", output["answer_shape"]["unit"])
        self.assertNotEqual("count", output["return_field"])

    def test_valid_q1_count_trend_is_not_output_and_terminal_is_argmin_day(self) -> None:
        record = compile_advisory_intent("valid-q1", self.valid_questions[1])
        outputs = _outputs(record)
        nodes_by_id = {node["operation_id"]: node for node in _nodes(record)}
        count_atoms = [
            atom
            for atom in record["lexical_atoms"]
            if atom["kind"] == "operation" and atom["canonical"] == "count"
        ]
        terminal_operators = {
            nodes_by_id[output["source_operation_ref"]]["operator"]
            for output in outputs
        }

        self.assertTrue(count_atoms)
        self.assertTrue(
            all(
                atom.get("details", {}).get("role") == "metric_mention"
                for atom in count_atoms
            )
        )
        self.assertNotIn("count", _operators(record))
        self.assertEqual(1, len(outputs))
        self.assertEqual({"argmin_all"}, terminal_operators)
        output = outputs[0]
        self.assertNotEqual("count", output["return_field"])
        self.assertEqual("single", output["cardinality"]["mode"])
        self.assertEqual(1, output["cardinality"]["expected_count"])
        self.assertEqual("scalar", output["answer_shape"]["container"])
        self.assertEqual("日", output["answer_shape"]["unit"])

    def test_q72_how_many_tasks_is_scalar_integer_count(self) -> None:
        record = compile_advisory_intent("test-q72", self.test_questions[72])
        output = _outputs(record)[-1]

        self.assertIn("count", _operators(record))
        self.assertEqual("count", output["return_field"])
        self.assertEqual("single", output["cardinality"]["mode"])
        self.assertEqual(1, output["cardinality"]["expected_count"])
        self.assertEqual("scalar", output["answer_shape"]["container"])
        self.assertEqual("integer", output["answer_shape"]["value_type"])

    def test_q86_all_people_how_many_has_one_count_terminal(self) -> None:
        record = compile_advisory_intent("test-q86", self.test_questions[86])
        outputs = _outputs(record)
        nodes_by_id = {node["operation_id"]: node for node in _nodes(record)}

        self.assertEqual(1, len(outputs))
        self.assertEqual(1, _operators(record).count("count"))
        self.assertNotIn("list", _operators(record))
        output = outputs[0]
        self.assertEqual(
            "count",
            nodes_by_id[output["source_operation_ref"]]["operator"],
        )
        self.assertEqual("count", output["return_field"])
        self.assertEqual("scalar", output["answer_shape"]["container"])
        self.assertEqual("integer", output["answer_shape"]["value_type"])

    def test_page_number_question_never_compiles_a_record_count(self) -> None:
        record = compile_advisory_intent("page-number", self.test_questions[12])
        output = _outputs(record)[-1]

        self.assertNotIn("count", _operators(record))
        self.assertNotEqual("count", output["return_field"])
        self.assertNotEqual("integer", output["answer_shape"]["value_type"])

    def test_contextual_date_week_and_page_words_do_not_leak_output_units(self) -> None:
        questions = (
            "2025-07-01の日付に登録された案件をすべて挙げてください。",
            "第5週目に実施する項目は何ですか。",
            "資料のページで強調された語をすべて抜き出してください。",
        )

        for index, question in enumerate(questions):
            with self.subTest(question=question):
                output = _outputs(
                    compile_advisory_intent(f"context-unit-{index}", question)
                )[-1]
                self.assertIsNone(output["answer_shape"]["unit"])
                self.assertIs(
                    output["inference_basis"]["enforceable"]["unit"],
                    False,
                )

    def test_two_connected_file_names_remain_distinct_scope_candidates(self) -> None:
        record = compile_advisory_intent(
            "two-files",
            "会議録_2025-10-29.pdfと会議録_2025-11-11.pdfにおいて、"
            "完了したアクションIDをすべて挙げてください。",
        )
        scope = record["intent"]["scope"]
        containers = [
            candidate["container"] for candidate in scope["literal_candidates"]
        ]

        self.assertCountEqual(
            ("会議録_2025-10-29.pdf", "会議録_2025-11-11.pdf"),
            containers,
        )
        self.assertEqual(2, len(containers))
        self.assertIsNone(scope["container"])
        self.assertEqual("unknown", scope["source"])

    def test_list_plus_sum_has_two_terminals_or_no_forced_single_shape(self) -> None:
        record = compile_advisory_intent(
            "list-sum",
            "該当案件をすべて挙げ、それらの契約金額の合計を答えてください。",
        )
        nodes_by_id = {node["operation_id"]: node for node in _nodes(record)}
        outputs = _outputs(record)

        self.assertIn("list", _operators(record))
        self.assertIn("sum", _operators(record))
        terminal_operators = {
            nodes_by_id[output["source_operation_ref"]]["operator"]
            for output in outputs
        }
        if len(outputs) >= 2:
            self.assertIn("list", terminal_operators)
            self.assertIn("sum", terminal_operators)
        else:
            output = outputs[0]
            self.assertEqual("unknown", output["answer_shape"]["container"])
            self.assertIs(
                output["inference_basis"]["enforceable"]["container"],
                False,
            )
            self.assertIs(
                output["inference_basis"]["enforceable"]["cardinality"],
                False,
            )

    def test_single_character_target_lexemes_do_not_match_inside_words(self) -> None:
        record = compile_advisory_intent(
            "one-character-targets",
            "銀行の実行時ログと表現、価値判断、列車の情報を確認してください。",
        )
        target_atoms = [
            atom for atom in record["lexical_atoms"] if atom["kind"] == "target"
        ]

        self.assertEqual([], target_atoms)
        self.assertEqual("unknown", record["intent"]["target"]["status"])
        self.assertEqual([], record["intent"]["target"]["candidates"])


if __name__ == "__main__":
    unittest.main()
