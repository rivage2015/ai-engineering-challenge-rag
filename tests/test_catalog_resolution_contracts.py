"""Schema-only contracts for CatalogResolutionRun.

These tests enforce the published closed shape, controlled vocabularies, and
question/source/answer separation boundary.  They intentionally do not claim
semantic recomputation of IDs or hashes, reference existence, catalog
membership, complete branch coverage, actual QIC-path binding, or derived status
recomputation.  Those checks require a future deterministic semantic validator.
"""

from __future__ import annotations

import copy
import json
import re
import unittest
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import jsonschema


REPOSITORY = Path(__file__).resolve().parents[1]
SCHEMA_PATH = REPOSITORY / "schemas" / "catalog-resolution-run.schema.json"

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


def resolved_run() -> dict[str, Any]:
    return {
        "schema_version": "0.1",
        "record_type": "catalog_resolution_run",
        "catalog_resolution_run_id": "crr_aaaaaaaaaaaaaaaa",
        "question_understanding_run_id": "qur_bbbbbbbbbbbbbbbb",
        "question_intent_contract_id": "qic_cccccccccccccccc",
        "question_clause_ir_id": "qcir_dddddddddddddddd",
        "data_catalog_snapshot_id": "dcs_eeeeeeeeeeeeeeee",
        "branch_resolutions": [
            {
                "branch_id": "branch_1111111111111111",
                "candidate_bindings": [
                    {
                        "binding_id": "crb_2222222222222222",
                        "catalog_entry_ref": "dce_3333333333333333",
                        "target_bindings": [
                            {
                                "qic_path": "/requested/target",
                                "target_kind": "catalog_entry",
                                "target_ref": "dce_3333333333333333",
                                "match_mode": "exact",
                                "status": "matched",
                            }
                        ],
                        "scope_bindings": [
                            {
                                "qic_path": "/requested/scope/location",
                                "label_ref": "dcl_4444444444444444",
                                "match_mode": "exact",
                                "status": "matched",
                            }
                        ],
                        "field_bindings": [
                            {
                                "qic_path": "/requested/scope/predicates/0/field",
                                "field_ref": "dcf_5555555555555555",
                                "match_mode": "exact_normalized",
                                "status": "matched",
                            }
                        ],
                        "capability_checks": [
                            {
                                "operation_ref": None,
                                "capability_kind": "retrieval_channel",
                                "required_capability": "structured",
                                "status": "pass",
                                "reason_code": "declared_supported",
                            },
                            {
                                "operation_ref": "op_6666666666666666",
                                "capability_kind": "predicate_operator",
                                "required_capability": "eq",
                                "status": "pass",
                                "reason_code": None,
                            },
                            {
                                "operation_ref": "op_7777777777777777",
                                "capability_kind": "graph_operator",
                                "required_capability": "filter",
                                "status": "pass",
                                "reason_code": "declared_supported",
                            },
                        ],
                        "basis_refs": [
                            "qcl_8888888888888888",
                            "dce_3333333333333333",
                            "dcl_4444444444444444",
                            "dcf_5555555555555555",
                        ],
                        "status": "resolved",
                    }
                ],
                "status": "resolved",
            }
        ],
        "final_status": "resolved",
        "reason_codes": [],
        "errors": [],
        "provenance": {
            "resolver": "catalog-resolver",
            "resolver_version": "0.1",
            "generated_at": "2026-08-16T00:00:00Z",
            "deterministic": True,
            "model_used": False,
            "question_independent": False,
            "question_data_used": True,
            "source_data_used": True,
            "answer_data_used": False,
            "past_answers_used": False,
            "input_qur_sha256": "9" * 64,
            "input_catalog_sha256": "a" * 64,
        },
    }


def clarification_run() -> dict[str, Any]:
    run = resolved_run()
    run["catalog_resolution_run_id"] = "crr_bbbbbbbbbbbbbbbb"
    binding = run["branch_resolutions"][0]["candidate_bindings"][0]
    binding["scope_bindings"][0]["status"] = "conflict"
    binding["capability_checks"][0].update(
        {"status": "unknown", "reason_code": "catalog_unavailable"}
    )
    binding["status"] = "partial"
    run["branch_resolutions"][0]["status"] = "ambiguous"
    run["final_status"] = "clarification_required"
    run["reason_codes"] = ["scope_ambiguous"]
    return run


def failed_run() -> dict[str, Any]:
    run = resolved_run()
    run_id = "crr_cccccccccccccccc"
    run.update(
        {
            "catalog_resolution_run_id": run_id,
            "branch_resolutions": [],
            "final_status": "failed",
            "reason_codes": ["runtime_failure"],
            "errors": [
                {
                    "stage": "runtime",
                    "code": "internal_error",
                    "subject_refs": [run_id],
                }
            ],
        }
    )
    return run


class CatalogResolutionRunSchemaTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        jsonschema.Draft202012Validator.check_schema(cls.schema)
        checker = jsonschema.FormatChecker()
        if "date-time" not in checker.checkers:
            checker.checks("date-time", raises=ValueError)(
                is_rfc3339_datetime
            )
        cls.validator = jsonschema.Draft202012Validator(
            cls.schema, format_checker=checker
        )

    def errors(self, value: object) -> list[str]:
        return sorted(error.message for error in self.validator.iter_errors(value))

    def assert_invalid(self, value: object) -> None:
        self.assertTrue(self.errors(value), "schema-invalid resolution was accepted")

    def test_resolved_clarification_and_failed_examples_are_valid(self) -> None:
        self.assertEqual(
            self.schema["$id"],
            "https://local.ai-engineering-challenge/schemas/catalog-resolution-run.schema.json",
        )
        self.assertEqual(self.errors(resolved_run()), [])
        self.assertEqual(self.errors(clarification_run()), [])
        self.assertEqual(self.errors(failed_run()), [])

    def test_every_record_container_is_closed(self) -> None:
        resolved_containers: dict[
            str, Callable[[dict[str, Any]], dict[str, Any]]
        ] = {
            "root": lambda value: value,
            "branch": lambda value: value["branch_resolutions"][0],
            "candidate": lambda value: value["branch_resolutions"][0][
                "candidate_bindings"
            ][0],
            "target_binding": lambda value: value["branch_resolutions"][0][
                "candidate_bindings"
            ][0]["target_bindings"][0],
            "scope_binding": lambda value: value["branch_resolutions"][0][
                "candidate_bindings"
            ][0]["scope_bindings"][0],
            "field_binding": lambda value: value["branch_resolutions"][0][
                "candidate_bindings"
            ][0]["field_bindings"][0],
            "capability_check": lambda value: value["branch_resolutions"][0][
                "candidate_bindings"
            ][0]["capability_checks"][0],
            "provenance": lambda value: value["provenance"],
        }
        for label, select in resolved_containers.items():
            with self.subTest(container=label):
                run = resolved_run()
                select(run)["unknown_metadata"] = True
                self.assert_invalid(run)

        failed = failed_run()
        failed["errors"][0]["unknown_metadata"] = True
        self.assert_invalid(failed)

    def test_question_values_answers_scores_and_primary_cannot_be_smuggled(self) -> None:
        forbidden_keys = (
            "question",
            "original_question",
            "filter_value",
            "answer",
            "score",
            "primary",
            "primary_result",
        )
        containers: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
            "root": lambda value: value,
            "branch": lambda value: value["branch_resolutions"][0],
            "candidate": lambda value: value["branch_resolutions"][0][
                "candidate_bindings"
            ][0],
            "target_binding": lambda value: value["branch_resolutions"][0][
                "candidate_bindings"
            ][0]["target_bindings"][0],
            "scope_binding": lambda value: value["branch_resolutions"][0][
                "candidate_bindings"
            ][0]["scope_bindings"][0],
            "field_binding": lambda value: value["branch_resolutions"][0][
                "candidate_bindings"
            ][0]["field_bindings"][0],
            "capability_check": lambda value: value["branch_resolutions"][0][
                "candidate_bindings"
            ][0]["capability_checks"][0],
            "provenance": lambda value: value["provenance"],
        }
        for key in forbidden_keys:
            for label, select in containers.items():
                with self.subTest(forbidden_key=key, container=label):
                    run = resolved_run()
                    select(run)[key] = "smuggled"
                    self.assert_invalid(run)

        for key in forbidden_keys:
            with self.subTest(forbidden_key=key, container="error"):
                run = failed_run()
                run["errors"][0][key] = "smuggled"
                self.assert_invalid(run)

    def test_ids_references_paths_statuses_and_match_modes_are_closed(self) -> None:
        qic_style_operation_id = resolved_run()
        qic_style_operation_id["branch_resolutions"][0]["candidate_bindings"][0][
            "capability_checks"
        ][1]["operation_ref"] = "op_000_filter"
        self.assertEqual(self.errors(qic_style_operation_id), [])

        mutations: dict[str, Callable[[dict[str, Any]], None]] = {
            "run_id": lambda value: value.__setitem__(
                "catalog_resolution_run_id", "crr_short"
            ),
            "question_run_id": lambda value: value.__setitem__(
                "question_understanding_run_id", "qur_not-hex"
            ),
            "intent_contract_id": lambda value: value.__setitem__(
                "question_intent_contract_id", "qic_short"
            ),
            "clause_ir_id": lambda value: value.__setitem__(
                "question_clause_ir_id", "qcir_short"
            ),
            "snapshot_id": lambda value: value.__setitem__(
                "data_catalog_snapshot_id", "dcs_short"
            ),
            "branch_id": lambda value: value["branch_resolutions"][0].__setitem__(
                "branch_id", "branch_short"
            ),
            "binding_id": lambda value: value["branch_resolutions"][0][
                "candidate_bindings"
            ][0].__setitem__("binding_id", "crb_short"),
            "catalog_entry_ref": lambda value: value["branch_resolutions"][0][
                "candidate_bindings"
            ][0].__setitem__("catalog_entry_ref", "dce_short"),
            "target_ref": lambda value: value["branch_resolutions"][0][
                "candidate_bindings"
            ][0]["target_bindings"][0].__setitem__("target_ref", "dce_short"),
            "label_ref": lambda value: value["branch_resolutions"][0][
                "candidate_bindings"
            ][0]["scope_bindings"][0].__setitem__("label_ref", "dcl_short"),
            "field_ref": lambda value: value["branch_resolutions"][0][
                "candidate_bindings"
            ][0]["field_bindings"][0].__setitem__("field_ref", "dcf_short"),
            "operation_ref": lambda value: value["branch_resolutions"][0][
                "candidate_bindings"
            ][0]["capability_checks"][1].__setitem__(
                "operation_ref", "Op_000_filter"
            ),
            "operation_ref_too_long": lambda value: value["branch_resolutions"][0][
                "candidate_bindings"
            ][0]["capability_checks"][1].__setitem__("operation_ref", "a" * 129),
            "basis_ref": lambda value: value["branch_resolutions"][0][
                "candidate_bindings"
            ][0].__setitem__("basis_refs", ["answer_1111111111111111"]),
            "qic_path": lambda value: value["branch_resolutions"][0][
                "candidate_bindings"
            ][0]["scope_bindings"][0].__setitem__(
                "qic_path", "/answer/value"
            ),
            "final_status": lambda value: value.__setitem__(
                "final_status", "ready"
            ),
            "branch_status": lambda value: value["branch_resolutions"][0].__setitem__(
                "status", "ready"
            ),
            "candidate_status": lambda value: value["branch_resolutions"][0][
                "candidate_bindings"
            ][0].__setitem__("status", "ready"),
            "target_status": lambda value: value["branch_resolutions"][0][
                "candidate_bindings"
            ][0]["target_bindings"][0].__setitem__("status", "partial"),
            "scope_status": lambda value: value["branch_resolutions"][0][
                "candidate_bindings"
            ][0]["scope_bindings"][0].__setitem__("status", "partial"),
            "field_status": lambda value: value["branch_resolutions"][0][
                "candidate_bindings"
            ][0]["field_bindings"][0].__setitem__("status", "partial"),
            "scope_match_mode": lambda value: value["branch_resolutions"][0][
                "candidate_bindings"
            ][0]["scope_bindings"][0].__setitem__("match_mode", "fuzzy"),
            "target_match_mode": lambda value: value["branch_resolutions"][0][
                "candidate_bindings"
            ][0]["target_bindings"][0].__setitem__("match_mode", "fuzzy"),
            "field_match_mode": lambda value: value["branch_resolutions"][0][
                "candidate_bindings"
            ][0]["field_bindings"][0].__setitem__("match_mode", "semantic"),
        }
        for label, mutate in mutations.items():
            with self.subTest(invalid_field=label):
                run = resolved_run()
                mutate(run)
                self.assert_invalid(run)

        failed = failed_run()
        failed["errors"][0]["subject_refs"] = ["Answer_1111111111111111"]
        self.assert_invalid(failed)

    def test_capability_kind_value_status_and_reason_are_consistent(self) -> None:
        check_path = lambda value: value["branch_resolutions"][0][
            "candidate_bindings"
        ][0]["capability_checks"][0]
        mutations: dict[str, Callable[[dict[str, Any]], None]] = {
            "kind": lambda value: check_path(value).__setitem__(
                "capability_kind", "answer_capability"
            ),
            "unknown_value": lambda value: check_path(value).__setitem__(
                "required_capability", "invented_operator"
            ),
            "kind_value_mismatch": lambda value: check_path(value).__setitem__(
                "required_capability", "eq"
            ),
            "retrieval_with_operation_ref": lambda value: check_path(value).__setitem__(
                "operation_ref", "op_000_retrieve"
            ),
            "predicate_without_operation_ref": lambda value: value[
                "branch_resolutions"
            ][0]["candidate_bindings"][0]["capability_checks"][1].__setitem__(
                "operation_ref", None
            ),
            "graph_without_operation_ref": lambda value: value[
                "branch_resolutions"
            ][0]["candidate_bindings"][0]["capability_checks"][2].__setitem__(
                "operation_ref", None
            ),
            "status": lambda value: check_path(value).__setitem__("status", "ready"),
            "pass_with_failure_reason": lambda value: check_path(value).__setitem__(
                "reason_code", "not_declared"
            ),
            "fail_without_reason": lambda value: check_path(value).update(
                {"status": "fail", "reason_code": None}
            ),
            "unknown_with_success_reason": lambda value: check_path(value).update(
                {"status": "unknown", "reason_code": "declared_supported"}
            ),
        }
        for label, mutate in mutations.items():
            with self.subTest(capability_check=label):
                run = resolved_run()
                mutate(run)
                self.assert_invalid(run)

        invalid_reason = clarification_run()
        invalid_reason["reason_codes"] = ["question_relevant"]
        self.assert_invalid(invalid_reason)

        invalid_error_stage = failed_run()
        invalid_error_stage["errors"][0]["stage"] = "answer"
        self.assert_invalid(invalid_error_stage)

        invalid_error_code = failed_run()
        invalid_error_code["errors"][0]["code"] = "answer_failed"
        self.assert_invalid(invalid_error_code)

    def test_resolved_and_clarification_status_invariants_are_enforced(self) -> None:
        two_resolved_branches = resolved_run()
        second_branch = copy.deepcopy(two_resolved_branches["branch_resolutions"][0])
        second_branch["branch_id"] = "branch_9999999999999999"
        two_resolved_branches["branch_resolutions"].append(second_branch)
        self.assert_invalid(two_resolved_branches)

        two_resolved_candidates = resolved_run()
        second_candidate = copy.deepcopy(
            two_resolved_candidates["branch_resolutions"][0]["candidate_bindings"][0]
        )
        second_candidate["binding_id"] = "crb_9999999999999999"
        two_resolved_candidates["branch_resolutions"][0][
            "candidate_bindings"
        ].append(second_candidate)
        self.assert_invalid(two_resolved_candidates)

        mutations: dict[str, Callable[[dict[str, Any]], None]] = {
            "resolved_without_branch": lambda value: value.__setitem__(
                "branch_resolutions", []
            ),
            "resolved_with_ambiguous_branch": lambda value: value[
                "branch_resolutions"
            ][0].__setitem__("status", "ambiguous"),
            "resolved_with_partial_candidate": lambda value: value[
                "branch_resolutions"
            ][0]["candidate_bindings"][0].__setitem__("status", "partial"),
            "resolved_with_reason": lambda value: value.__setitem__(
                "reason_codes", ["scope_ambiguous"]
            ),
            "resolved_with_error": lambda value: value.__setitem__(
                "errors", failed_run()["errors"]
            ),
            "branch_resolved_without_candidate": lambda value: value[
                "branch_resolutions"
            ][0].__setitem__("candidate_bindings", []),
            "binding_resolved_with_scope_conflict": lambda value: value[
                "branch_resolutions"
            ][0]["candidate_bindings"][0]["scope_bindings"][0].__setitem__(
                "status", "conflict"
            ),
            "binding_resolved_without_target": lambda value: value[
                "branch_resolutions"
            ][0]["candidate_bindings"][0].__setitem__("target_bindings", []),
            "binding_resolved_with_target_conflict": lambda value: value[
                "branch_resolutions"
            ][0]["candidate_bindings"][0]["target_bindings"][0].__setitem__(
                "status", "conflict"
            ),
            "target_kind_ref_mismatch": lambda value: value[
                "branch_resolutions"
            ][0]["candidate_bindings"][0]["target_bindings"][0].update(
                {"target_kind": "field", "target_ref": "dce_3333333333333333"}
            ),
            "target_with_non_target_qic_path": lambda value: value[
                "branch_resolutions"
            ][0]["candidate_bindings"][0]["target_bindings"][0].__setitem__(
                "qic_path", "/requested/scope/location"
            ),
            "binding_resolved_with_field_conflict": lambda value: value[
                "branch_resolutions"
            ][0]["candidate_bindings"][0]["field_bindings"][0].__setitem__(
                "status", "conflict"
            ),
            "binding_resolved_with_capability_failure": lambda value: value[
                "branch_resolutions"
            ][0]["candidate_bindings"][0]["capability_checks"][0].update(
                {"status": "fail", "reason_code": "not_declared"}
            ),
            "binding_resolved_without_capability_check": lambda value: value[
                "branch_resolutions"
            ][0]["candidate_bindings"][0].__setitem__("capability_checks", []),
        }
        for label, mutate in mutations.items():
            with self.subTest(resolved_invariant=label):
                run = resolved_run()
                mutate(run)
                self.assert_invalid(run)

        clarification_without_reason = clarification_run()
        clarification_without_reason["reason_codes"] = []
        self.assert_invalid(clarification_without_reason)

        clarification_with_error = clarification_run()
        clarification_with_error["errors"] = failed_run()["errors"]
        self.assert_invalid(clarification_with_error)

    def test_failed_status_requires_a_controlled_error(self) -> None:
        without_error = failed_run()
        without_error["errors"] = []
        self.assert_invalid(without_error)

        missing_subject = failed_run()
        missing_subject["errors"][0]["subject_refs"] = []
        self.assert_invalid(missing_subject)

    def test_provenance_constants_hashes_and_timestamp_are_enforced(self) -> None:
        mutations = {
            "resolver": "model-resolver",
            "resolver_version": "v0.1",
            "generated_at": "not-a-timestamp",
            "deterministic": False,
            "model_used": True,
            "question_independent": True,
            "question_data_used": False,
            "source_data_used": False,
            "answer_data_used": True,
            "past_answers_used": True,
            "input_qur_sha256": "9" * 63,
            "input_catalog_sha256": "A" * 64,
        }
        for field, replacement in mutations.items():
            with self.subTest(provenance_field=field):
                run = resolved_run()
                run["provenance"][field] = replacement
                self.assert_invalid(run)


if __name__ == "__main__":
    unittest.main()
