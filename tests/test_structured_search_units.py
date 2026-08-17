"""Question-independent row certification and operation execution tests."""

from __future__ import annotations

import copy
import sys
import unittest
from decimal import Decimal
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from structured_search_units import (  # noqa: E402
    StructuredProfileAccumulator,
    StructuredRowError,
    capabilities_for_profile,
    classify_scalar,
    decode_table_row,
    execute_operation_graph,
)
from tests.test_question_understanding_engine import (  # noqa: E402
    compile_fixture,
    generic_compound_fixture,
    generic_list_fixture,
)


def _unit(index: int, headers: list[str], values: list[str]) -> dict[str, object]:
    if len(headers) != len(values):
        raise ValueError("test row header/value arity differs")
    return {
        "search_unit_id": f"su_{index:032x}",
        "document_id": "doc_" + "1" * 32,
        "unit_type": "table_row",
        "context": {
            "header_labels": headers,
            "header_method": "source_header_row",
            "is_header_candidate": False,
        },
        "text": {
            "search_text": "\n".join(
                f"{header}: {value}"
                for header, value in zip(headers, values)
            )
        },
    }


class StructuredSearchUnitTest(unittest.TestCase):
    def test_exact_row_decode_profile_and_types(self):
        headers = ["RowID", "Category", "Amount", "Metric"]
        first = _unit(1, headers, ["r1", "A", "10", "1.5"])
        second = _unit(2, headers, ["r2", "B", "20", "2"])
        decoded = decode_table_row(first)
        self.assertEqual("10", decoded.mapping()["Amount"])
        accumulator = StructuredProfileAccumulator()
        accumulator.observe(first)
        accumulator.observe(second)
        profile = accumulator.finish()
        self.assertIsNotNone(profile)
        self.assertEqual(
            ("string", "string", "integer", "number"), profile.data_types
        )
        self.assertEqual(
            {
                "retrieval_channels": ["lexical", "structured"],
                "predicate_operators": ["eq", "ne", "gt", "gte", "lt", "lte"],
                "graph_operators": ["filter", "list", "project", "argmin_all", "mean"],
            },
            capabilities_for_profile(profile, lexical=True),
        )

    def test_ambiguous_missing_multiline_and_schema_drift_fail_closed(self):
        valid = _unit(1, ["A", "B"], ["x", "y"])
        mutations = []
        duplicate = copy.deepcopy(valid)
        duplicate["context"]["header_labels"] = ["A", "A"]
        mutations.append(duplicate)
        missing = copy.deepcopy(valid)
        missing["text"]["search_text"] = "A: x"
        mutations.append(missing)
        multiline = copy.deepcopy(valid)
        multiline["text"]["search_text"] = "A: x\ncontinuation\nB: y"
        mutations.append(multiline)
        for mutation in mutations:
            with self.subTest(text=mutation["text"]["search_text"]):
                with self.assertRaises(StructuredRowError):
                    decode_table_row(mutation)

        accumulator = StructuredProfileAccumulator()
        accumulator.observe(valid)
        accumulator.observe(_unit(2, ["A", "C"], ["x", "z"]))
        self.assertIsNone(accumulator.finish())

    def test_scalar_classification_does_not_turn_zero_padded_ids_numeric(self):
        self.assertEqual("integer", classify_scalar("42"))
        self.assertEqual("number", classify_scalar("42.5"))
        self.assertEqual("string", classify_scalar("0042"))
        self.assertEqual("string", classify_scalar("train_0042"))

    def test_list_filter_project_execution(self):
        question, draft = generic_list_fixture("structured")
        requested = compile_fixture(question, draft)["question_intent_contract"]["requested"]
        field = requested["scope"]["filters"][0]["field"]
        target = requested["target"]["surface"]
        expected = requested["scope"]["filters"][0]["value"]
        rows = [
            decode_table_row(_unit(1, [target, field], ["id_1", expected])),
            decode_table_row(_unit(2, [target, field], ["id_2", "other"])),
            decode_table_row(_unit(3, [target, field], ["id_3", expected])),
        ]
        result = execute_operation_graph(requested, rows)
        self.assertEqual(("id_1", "id_3"), result.requested_outputs[0]["value"])

    def test_compound_mean_and_all_nearest_ties_use_unrounded_decimal(self):
        question, draft = generic_compound_fixture("structured")
        requested = compile_fixture(question, draft)["question_intent_contract"]["requested"]
        equality = requested["scope"]["filters"][0]
        threshold = requested["scope"]["filters"][1]
        metric = requested["operation_graph"]["nodes"][2]["fields"][0]
        identifier = requested["operation_graph"]["nodes"][5]["fields"][0]
        headers = [identifier, equality["field"], threshold["field"], metric]
        rows = [
            decode_table_row(_unit(1, headers, ["r1", str(equality["value"]), "40000", "10"])),
            decode_table_row(_unit(2, headers, ["r2", str(equality["value"]), "40001", "20"])),
            decode_table_row(_unit(3, headers, ["r3", str(equality["value"]), "40002", "30"])),
            decode_table_row(_unit(4, headers, ["skip", "other", "50000", "999"])),
        ]
        result = execute_operation_graph(requested, rows)
        self.assertEqual(Decimal("20"), result.requested_outputs[0]["value"])
        self.assertEqual(("r2",), result.requested_outputs[1]["value"])

        tied = [
            decode_table_row(_unit(1, headers, ["r1", str(equality["value"]), "40000", "10"])),
            decode_table_row(_unit(2, headers, ["r2", str(equality["value"]), "40001", "20"])),
        ]
        tie_result = execute_operation_graph(requested, tied)
        self.assertEqual(Decimal("15"), tie_result.requested_outputs[0]["value"])
        self.assertEqual(("r1", "r2"), tie_result.requested_outputs[1]["value"])


if __name__ == "__main__":
    unittest.main()
