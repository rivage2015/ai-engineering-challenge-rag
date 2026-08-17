"""Schema-only contracts for QuestionClauseIR.

These tests intentionally do not claim that arbitrary question syntax has been
parsed correctly.  Span/text correspondence, cross-field coverage arithmetic,
clause extraction, semantic equivalence, deterministic ID/hash recomputation,
QIC binding, and completeness decisions require a future deterministic semantic
validator and are outside this schema-only phase.
"""

from __future__ import annotations

import json
import re
import unittest
from datetime import datetime
from pathlib import Path
from typing import Any

import jsonschema


REPOSITORY = Path(__file__).resolve().parents[1]
SCHEMA_PATH = REPOSITORY / "schemas" / "question-clause-ir.schema.json"


RFC3339_DATETIME = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}[Tt][0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]+)?(?:[Zz]|[+-][0-9]{2}:[0-9]{2})$"
)


def is_rfc3339_datetime(value: object) -> bool:
    """Supply the optional date-time checker absent from the lean test venv."""

    if not isinstance(value, str) or RFC3339_DATETIME.fullmatch(value) is None:
        return False
    datetime.fromisoformat(value.replace("Z", "+00:00").replace("z", "+00:00"))
    return True


def complete_clause_ir() -> dict[str, Any]:
    question = "組織A"
    return {
        "schema_version": "0.1",
        "record_type": "question_clause_ir",
        "question_clause_ir_id": "qcir_aaaaaaaaaaaaaaaa",
        "question_id": "q_clause_complete",
        "original_question": question,
        "grammar_profile": "list_eq_id_all_v0_1",
        "clauses": [
            {
                "clause_id": "qcl_bbbbbbbbbbbbbbbb",
                "span": {"start": 0, "end": len(question), "text": question},
                "role": "scope_location",
                "normalized_value": question,
                "polarity": "positive",
                "qic_paths": ["/requested/scope/location"],
                "disposition": "mapped",
            }
        ],
        "coverage": {
            "status": "complete",
            "total_codepoints": len(question),
            "covered_codepoints": len(question),
            "unresolved_clause_refs": [],
            "conflict_clause_refs": [],
            "unbound_qic_paths": [],
        },
        "provenance": {
            "parser": "question-clause-parser",
            "parser_version": "0.1",
            "registry_name": "question-language-registry",
            "registry_version": "0.1",
            "registry_sha256": "c" * 64,
            "rule_version": "v0.1",
            "generated_at": "2026-08-16T00:00:00+00:00",
            "input_question_sha256": "d" * 64,
            "deterministic": True,
            "question_only": True,
            "catalog_used": False,
            "answer_data_used": False,
            "past_answers_used": False,
        },
    }


def unsupported_incomplete_clause_ir() -> dict[str, Any]:
    record = complete_clause_ir()
    question = "自由な質問です。"
    clause_id = "qcl_cccccccccccccccc"
    record.update(
        {
            "question_clause_ir_id": "qcir_dddddddddddddddd",
            "question_id": "q_clause_unsupported",
            "original_question": question,
            "grammar_profile": "unsupported_v0_1",
            "clauses": [
                {
                    "clause_id": clause_id,
                    "span": {
                        "start": 0,
                        "end": len(question),
                        "text": question,
                    },
                    "role": "unresolved",
                    "normalized_value": None,
                    "polarity": "not_applicable",
                    "qic_paths": [],
                    "disposition": "unresolved",
                }
            ],
            "coverage": {
                "status": "incomplete",
                "total_codepoints": len(question),
                "covered_codepoints": 0,
                "unresolved_clause_refs": [clause_id],
                "conflict_clause_refs": [],
                "unbound_qic_paths": [],
            },
        }
    )
    return record


class QuestionClauseIRSchemaTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        jsonschema.Draft202012Validator.check_schema(cls.schema)
        format_checker = jsonschema.FormatChecker()
        if "date-time" not in format_checker.checkers:
            format_checker.checks("date-time", raises=ValueError)(
                is_rfc3339_datetime
            )
        cls.validator = jsonschema.Draft202012Validator(
            cls.schema,
            format_checker=format_checker,
        )

    def errors(self, value: object) -> list[str]:
        return sorted(
            error.message
            for error in self.validator.iter_errors(value)
        )

    def assert_invalid(self, value: object) -> None:
        self.assertTrue(self.errors(value), "schema-invalid fixture was accepted")

    def test_complete_and_unsupported_incomplete_records_are_valid(self) -> None:
        self.assertEqual(
            self.schema["$id"],
            "https://local.ai-engineering-challenge/schemas/question-clause-ir.schema.json",
        )
        self.assertEqual(self.errors(complete_clause_ir()), [])
        self.assertEqual(self.errors(unsupported_incomplete_clause_ir()), [])

    def test_unknown_fields_are_rejected_at_every_closed_level(self) -> None:
        mutations = (
            ("root", lambda value: value.__setitem__("unknown", True)),
            (
                "clause",
                lambda value: value["clauses"][0].__setitem__("unknown", True),
            ),
            (
                "span",
                lambda value: value["clauses"][0]["span"].__setitem__(
                    "unknown", True
                ),
            ),
            (
                "coverage",
                lambda value: value["coverage"].__setitem__("unknown", True),
            ),
            (
                "provenance",
                lambda value: value["provenance"].__setitem__("unknown", True),
            ),
        )
        for label, mutate in mutations:
            with self.subTest(container=label):
                record = complete_clause_ir()
                mutate(record)
                self.assert_invalid(record)

    def test_bad_ids_spans_enums_and_qic_paths_are_rejected(self) -> None:
        mutations = {
            "question_clause_ir_id": lambda value: value.__setitem__(
                "question_clause_ir_id", "qcir_short"
            ),
            "clause_id": lambda value: value["clauses"][0].__setitem__(
                "clause_id", "qcl_not-hex"
            ),
            "span_start": lambda value: value["clauses"][0]["span"].__setitem__(
                "start", -1
            ),
            "span_end": lambda value: value["clauses"][0]["span"].__setitem__(
                "end", 0
            ),
            "span_text": lambda value: value["clauses"][0]["span"].__setitem__(
                "text", ""
            ),
            "grammar_profile": lambda value: value.__setitem__(
                "grammar_profile", "arbitrary_syntax"
            ),
            "role": lambda value: value["clauses"][0].__setitem__(
                "role", "answer"
            ),
            "polarity": lambda value: value["clauses"][0].__setitem__(
                "polarity", "maybe"
            ),
            "disposition": lambda value: value["clauses"][0].__setitem__(
                "disposition", "accepted"
            ),
            "coverage_status": lambda value: value["coverage"].__setitem__(
                "status", "ready"
            ),
            "qic_path": lambda value: value["clauses"][0].__setitem__(
                "qic_paths", ["/catalog/rows"]
            ),
            "mapped_without_path": lambda value: value["clauses"][0].__setitem__(
                "qic_paths", []
            ),
            "complete_with_unresolved": lambda value: value["coverage"].__setitem__(
                "unresolved_clause_refs", [value["clauses"][0]["clause_id"]]
            ),
        }
        for label, mutate in mutations.items():
            with self.subTest(invalid_field=label):
                record = complete_clause_ir()
                mutate(record)
                self.assert_invalid(record)

    def test_grammar_coverage_and_clause_disposition_are_fail_closed(self) -> None:
        unsupported_complete = complete_clause_ir()
        unsupported_complete["grammar_profile"] = "unsupported_v0_1"
        self.assert_invalid(unsupported_complete)

        incomplete_claiming_complete = unsupported_incomplete_clause_ir()
        incomplete_claiming_complete["coverage"].update(
            {"status": "complete", "unresolved_clause_refs": []}
        )
        self.assert_invalid(incomplete_claiming_complete)

        valid_syntax = complete_clause_ir()
        valid_syntax["clauses"][0].update(
            {
                "role": "syntax",
                "normalized_value": None,
                "polarity": "not_applicable",
                "qic_paths": [],
                "disposition": "syntax",
            }
        )
        self.assertEqual(self.errors(valid_syntax), [])

        mutations = {
            "syntax_role_as_mapped": {
                "role": "syntax",
                "polarity": "not_applicable",
                "disposition": "mapped",
            },
            "syntax_disposition_with_semantic_role": {
                "role": "scope_location",
                "polarity": "not_applicable",
                "qic_paths": [],
                "disposition": "syntax",
            },
            "syntax_with_semantic_polarity": {
                "role": "syntax",
                "polarity": "positive",
                "qic_paths": [],
                "disposition": "syntax",
            },
            "syntax_with_qic_binding": {
                "role": "syntax",
                "polarity": "not_applicable",
                "disposition": "syntax",
            },
            "mapped_unresolved_role": {
                "role": "unresolved",
                "polarity": "not_applicable",
                "disposition": "mapped",
            },
            "complete_conflict_clause": {
                "disposition": "conflict",
            },
        }
        for label, fields in mutations.items():
            with self.subTest(role_disposition=label):
                record = complete_clause_ir()
                record["clauses"][0].update(fields)
                self.assert_invalid(record)

        unresolved_mutations = {
            "unresolved_with_semantic_role": {"role": "scope_location"},
            "unresolved_with_semantic_polarity": {"polarity": "negative"},
            "unresolved_with_qic_binding": {
                "qic_paths": ["/requested/scope/location"]
            },
        }
        for label, fields in unresolved_mutations.items():
            with self.subTest(role_disposition=label):
                record = unsupported_incomplete_clause_ir()
                record["clauses"][0].update(fields)
                self.assert_invalid(record)

    def test_qic_paths_are_limited_to_intent_bearing_domains(self) -> None:
        valid_paths = (
            "/requested/target",
            "/requested/scope/location",
            "/requested/operation_graph/nodes/0",
            "/requested/requested_outputs/0",
            "/not_requested/0",
            "/ambiguity/0/candidates/1",
        )
        for path in valid_paths:
            with self.subTest(valid_path=path):
                record = complete_clause_ir()
                record["clauses"][0]["qic_paths"] = [path]
                self.assertEqual(self.errors(record), [])

        invalid_paths = (
            "/requested",
            "/requested/filter",
            "/requested/scope//location",
            "/not_requested",
            "/not_requested/item",
            "/not_requested/00",
            "/ambiguity/-1",
            "/ambiguity/0/",
            "/answer/value",
        )
        for path in invalid_paths:
            with self.subTest(invalid_path=path):
                record = complete_clause_ir()
                record["clauses"][0]["qic_paths"] = [path]
                self.assert_invalid(record)

    def test_role_specific_normalized_vocabularies_are_closed(self) -> None:
        vocabularies = {
            "filter_operator": "eq",
            "cardinality": "all",
            "boolean_connector": "and",
            "operation": "mean",
            "return_field": "identifier",
            "answer_container": "list",
            "answer_value_type": "string",
            "precision": "exact",
        }
        for role, valid_value in vocabularies.items():
            with self.subTest(role=role, value="declared"):
                record = complete_clause_ir()
                record["clauses"][0].update(
                    {"role": role, "normalized_value": valid_value}
                )
                self.assertEqual(self.errors(record), [])

            with self.subTest(role=role, value="undeclared"):
                record = complete_clause_ir()
                record["clauses"][0].update(
                    {"role": role, "normalized_value": "invented_value"}
                )
                self.assert_invalid(record)

    def test_provenance_formats_and_question_only_constants_are_enforced(self) -> None:
        mutations = {
            "registry_name": ("registry_name", "fixture-registry"),
            "parser_version": ("parser_version", "v0.1"),
            "registry_version": ("registry_version", "current"),
            "registry_sha256": ("registry_sha256", "A" * 64),
            "rule_version": ("rule_version", "0.1"),
            "generated_at": ("generated_at", "not-a-timestamp"),
            "input_question_sha256": ("input_question_sha256", "d" * 63),
            "deterministic": ("deterministic", False),
            "question_only": ("question_only", False),
            "catalog_used": ("catalog_used", True),
            "answer_data_used": ("answer_data_used", True),
            "past_answers_used": ("past_answers_used", True),
        }
        for label, (field, replacement) in mutations.items():
            with self.subTest(provenance_field=label):
                record = complete_clause_ir()
                record["provenance"][field] = replacement
                self.assert_invalid(record)

    def test_catalog_evidence_and_answer_key_smuggling_is_rejected(self) -> None:
        forbidden_keys = (
            "catalog",
            "data_catalog",
            "evidence",
            "retrieval_hits",
            "answer",
            "answer_plan",
            "final_answer",
            "ground_truth",
        )
        containers = {
            "root": lambda value: value,
            "clause": lambda value: value["clauses"][0],
            "span": lambda value: value["clauses"][0]["span"],
            "coverage": lambda value: value["coverage"],
            "provenance": lambda value: value["provenance"],
        }
        for key in forbidden_keys:
            for label, select in containers.items():
                with self.subTest(forbidden_key=key, container=label):
                    record = complete_clause_ir()
                    select(record)[key] = "smuggled"
                    self.assert_invalid(record)

        nested_value = complete_clause_ir()
        nested_value["clauses"][0]["normalized_value"] = {
            "answer": "smuggled"
        }
        self.assert_invalid(nested_value)

        raw_words = unsupported_incomplete_clause_ir()
        raw_words["clauses"][0]["normalized_value"] = (
            "catalog evidence answer are literal source words"
        )
        self.assertEqual(self.errors(raw_words), [])


if __name__ == "__main__":
    unittest.main()
