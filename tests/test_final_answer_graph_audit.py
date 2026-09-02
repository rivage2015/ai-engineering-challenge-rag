from __future__ import annotations

import copy
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "distribution" / "macos-local-memory" / "app"


def load_module(name: str, path: Path):
    specification = importlib.util.spec_from_file_location(name, path)
    if specification is None or specification.loader is None:
        raise ImportError(f"cannot load {path}")
    module = importlib.util.module_from_spec(specification)
    sys.modules[name] = module
    specification.loader.exec_module(module)
    return module


final_audit = load_module(
    "final_answer_graph_audit_test",
    APP / "final_answer_audit.py",
)


BINDING_FIELDS = (
    "evidence_sha256",
    "graph_sha256",
    "graph_security_partition_sha256",
    "graph_retrievable_evidence_set_sha256",
    "graph_embeddings_sha256",
)


def record_lookup_artifact() -> dict:
    return {
        "artifact_id": "qeg_record_lookup",
        "status": "ready",
        "intent": {"operation": "record_lookup"},
        "selected_evidence_ids": ["ROW", "E1", "E2"],
        "branches": [
            {
                "item_id": "F1",
                "branch_id": "branch_f1",
                "value": "value-F1",
                "selected_evidence_ids": ["ROW", "E1"],
                "validation_evidence_ids": ["E3"],
                "primary_path": ["question", "E1"],
                "stored_graph_binding": {
                    "structured_record_lookup_lineage": {
                        "field": {"value_evidence_id": "E1"},
                    },
                },
            },
            {
                "item_id": "F2",
                "branch_id": "branch_f2",
                "value": (
                    "=E3*F3 "
                    "[保存値・ファイル保存時・未再計算: 10000]"
                ),
                "selected_evidence_ids": ["ROW", "E2"],
                "validation_evidence_ids": ["E4"],
                "primary_path": ["question", "E2"],
                "stored_graph_binding": {
                    "structured_record_lookup_lineage": {
                        "field": {"value_evidence_id": "E2"},
                    },
                },
            },
        ],
    }


def field_run(
    item_id: str,
    branch_id: str,
    selected_evidence_ids: list[str],
    *,
    supported_value: str | None = None,
    supporting_packet_ids: list[str] | None = None,
) -> dict:
    supported_value = supported_value or f"value-{item_id}"
    supporting_packet_ids = supporting_packet_ids or [selected_evidence_ids[-1]]
    return {
        "item": {"item_id": item_id, "label": item_id, "required": True},
        "retrieved_evidence_ids": [*selected_evidence_ids, "DECOY"],
        "question_graph_branch_id": branch_id,
        "graph_primary_evidence_ids": selected_evidence_ids,
        "graph_augmented_evidence_ids": selected_evidence_ids,
        "audit": {
            "item_id": item_id,
            "verdict": "supported",
            "supported_value": supported_value,
            "supporting_packet_ids": supporting_packet_ids,
            "competing_packet_ids": [],
            "reason_code": "none",
            "defect": "",
            "missing_information": [],
        },
    }


def record_lookup_record() -> dict:
    question_plan = {
        "operation": "record_lookup",
        "items": [
            {"item_id": "F1", "label": "F1", "required": True},
            {"item_id": "F2", "label": "F2", "required": True},
        ],
        "answer_shape": "record",
    }
    metadata = {field: f"hash-{field}" for field in BINDING_FIELDS}
    return {
        "query": "人物の所在地と同居人を教えてください。",
        "question_plan": question_plan,
        "question_evidence_graph": record_lookup_artifact(),
        "graph_route": {
            "operation": "record_lookup",
            "required": True,
            "used": True,
        },
        "field_runs": [
            field_run("F1", "branch_f1", ["ROW", "E1"]),
            field_run(
                "F2",
                "branch_f2",
                ["ROW", "E2"],
                supported_value="= e3 * f3",
            ),
        ],
        "answer": {
            "answer_status": "answered",
            "answer_mode": "grounded",
            "answer": "F1とF2を確認しました。",
            "evidence_ids": ["E1", "E2"],
            "diagnostic_evidence_ids": [],
        },
        "index": metadata,
        "models": {},
        "performance": {},
    }


class FinalAnswerGraphAuditTests(unittest.TestCase):
    def test_collects_top_level_and_every_branch_evidence(self) -> None:
        artifact = record_lookup_artifact()
        artifact["selection"] = {
            "selected_evidence_ids": ["E0"],
            "validation_evidence_ids": ["EV0"],
        }
        selected, validation = final_audit.question_graph_evidence_ids(artifact)
        self.assertEqual(selected, ["ROW", "E1", "E2", "E0"])
        self.assertEqual(validation, ["EV0", "E3", "E4"])

    def test_supported_operations_require_pass_but_generic_may_be_not_applicable(
        self,
    ) -> None:
        not_applicable = {"status": "not_applicable"}
        self.assertFalse(final_audit.question_graph_validation_is_acceptable(
            not_applicable, frozenset({"aggregate_count"})
        ))
        self.assertFalse(final_audit.question_graph_validation_is_acceptable(
            not_applicable, frozenset({"record_lookup"})
        ))
        self.assertTrue(final_audit.question_graph_validation_is_acceptable(
            not_applicable, frozenset({"unknown"})
        ))
        self.assertTrue(final_audit.question_graph_validation_is_acceptable(
            {"status": "pass"}, frozenset({"aggregate_count"})
        ))

    def run_main(self, record: dict) -> tuple[dict, mock.Mock, mock.Mock, mock.Mock]:
        metadata = {field: record["index"][field] for field in BINDING_FIELDS}
        evidence_records = [
            {
                "evidence_id": evidence_id,
                "document_id": "doc_1",
                "relative_path": "fixture.xlsx",
                "locator": {"cell": f"A{ordinal}"},
                "text": f"evidence-{evidence_id}",
            }
            for ordinal, evidence_id in enumerate(
                ("E1", "E2", "E3", "E4", "ROW", "DECOY"), start=1
            )
        ]
        policy = {
            "eligible_evidence_ids": frozenset(
                item["evidence_id"] for item in evidence_records
            ),
            "metadata": metadata,
            "graph_sha256": metadata["graph_sha256"],
            "partition_sha256": metadata[
                "graph_security_partition_sha256"
            ],
            "eligible_evidence_set_sha256": metadata[
                "graph_retrievable_evidence_set_sha256"
            ],
            "source_graph": {"nodes": [], "edges": []},
        }
        question_validation = {
            "status": "pass",
            "failures": [],
            "warnings": [],
        }
        claim_validation = {
            "status": "pass",
            "failures": [],
            "warnings": [],
        }
        independent_result = (
            {"verdict": "verified", "reason": "ok", "unsupported_claims": []},
            {"wall_seconds": 0.0},
        )
        with tempfile.TemporaryDirectory() as temporary:
            record_path = Path(temporary) / "record.json"
            record_path.write_text(
                json.dumps(record, ensure_ascii=False), encoding="utf-8"
            )
            validate_graph = mock.Mock(return_value=question_validation)
            build_claims = mock.Mock(return_value=(
                {"items": []}, {"claims": []}, claim_validation,
            ))
            independent_audit = mock.Mock(return_value=independent_result)
            argv = [
                str(APP / "final_answer_audit.py"),
                "--record", str(record_path),
                "--index", str(Path(temporary) / "index.sqlite3"),
            ]
            with mock.patch.object(sys, "argv", argv), mock.patch.object(
                final_audit.answer_engine,
                "load_answer_evidence_records",
                return_value=(evidence_records, policy),
            ), mock.patch.object(
                final_audit.answer_engine, "validate_answer"
            ), mock.patch.object(
                final_audit.question_graph,
                "validate_question_evidence_graph",
                validate_graph,
            ), mock.patch.object(
                final_audit.claim_validator,
                "build_and_validate",
                build_claims,
            ), mock.patch.object(
                final_audit, "audit", independent_audit,
            ), redirect_stdout(io.StringIO()) as output:
                self.assertEqual(final_audit.main(), 0)
            return (
                json.loads(output.getvalue()),
                validate_graph,
                build_claims,
                independent_audit,
            )

    def test_record_lookup_passes_plan_and_audits_all_branch_packets(self) -> None:
        record = record_lookup_record()
        audited, validate_graph, build_claims, independent_audit = self.run_main(
            record
        )

        self.assertEqual(audited["graph_retrieval_trace"]["status"], "pass")
        self.assertTrue(
            audited["orchestration_decision"]["checks"]["graph_retrieval_trace"]
        )
        self.assertEqual(audited["orchestration_decision"]["status"], "accepted")
        independent_audit.assert_called_once()
        self.assertEqual(
            validate_graph.call_args.kwargs["question_plan"],
            record["question_plan"],
        )
        packet_ids = [
            packet["evidence_id"]
            for packet in build_claims.call_args.args[1]
        ]
        self.assertEqual(packet_ids, ["E1", "E2", "E3", "E4", "ROW"])

    def test_out_of_branch_support_fails_before_independent_audit(self) -> None:
        record = record_lookup_record()
        record["field_runs"][0]["audit"]["supporting_packet_ids"] = ["E2"]

        audited, _validate_graph, _build_claims, independent_audit = self.run_main(
            record
        )

        independent_audit.assert_not_called()
        self.assertEqual(audited["graph_retrieval_trace"]["status"], "blocked")
        self.assertIn(
            "record_lookup_support_outside_branch",
            {
                failure["code"]
                for failure in audited["graph_retrieval_trace"]["failures"]
            },
        )
        self.assertEqual(
            audited["performance"]["independent_final_audit"]["skip_reason"],
            "graph_retrieval_trace_blocked",
        )
        self.assertEqual(audited["orchestration_decision"]["status"], "rejected")
        self.assertFalse(
            audited["orchestration_decision"]["checks"]["graph_retrieval_trace"]
        )
        self.assertEqual(audited["answer"]["answer_status"], "insufficient")

    def test_unbound_field_run_is_not_accepted_as_graph_use(self) -> None:
        record = record_lookup_record()
        record["field_runs"].append({
            **field_run("F3", "branch_f3", ["ROW", "E1"]),
            "question_graph_branch_id": None,
        })

        trace = final_audit.validate_graph_retrieval_trace(
            record,
            record["question_evidence_graph"],
            frozenset({"record_lookup"}),
        )

        self.assertEqual(trace["status"], "blocked")
        self.assertIn(
            "record_lookup_field_branch_id_invalid",
            {failure["code"] for failure in trace["failures"]},
        )

    def test_shared_row_alone_cannot_support_a_branch_value(self) -> None:
        record = record_lookup_record()
        record["field_runs"][0]["audit"]["supporting_packet_ids"] = ["ROW"]

        audited, _validate_graph, _build_claims, independent_audit = self.run_main(
            record
        )
        trace = audited["graph_retrieval_trace"]

        independent_audit.assert_not_called()
        self.assertEqual(trace["status"], "blocked")
        self.assertIn(
            "record_lookup_value_support_missing",
            {failure["code"] for failure in trace["failures"]},
        )
        self.assertEqual(
            audited["performance"]["independent_final_audit"]["skip_reason"],
            "graph_retrieval_trace_blocked",
        )

    def test_normalized_item_ids_and_formula_only_value_are_accepted(self) -> None:
        record = record_lookup_record()
        record["field_runs"][0]["item"]["item_id"] = " Ｆ１ "
        record["field_runs"][0]["audit"]["item_id"] = "ｆ１"

        trace = final_audit.validate_graph_retrieval_trace(
            record,
            record["question_evidence_graph"],
            frozenset({"record_lookup"}),
        )

        self.assertEqual(trace["status"], "pass", trace)

    def test_audit_identity_verdict_and_value_must_match_the_branch(self) -> None:
        base = record_lookup_record()
        cases = (
            (
                "item_id",
                lambda record: record["field_runs"][0]["audit"].__setitem__(
                    "item_id", "F2"
                ),
                "record_lookup_audit_item_mismatch",
            ),
            (
                "verdict",
                lambda record: record["field_runs"][0]["audit"].__setitem__(
                    "verdict", "insufficient"
                ),
                "record_lookup_audit_verdict_invalid",
            ),
            (
                "supported_value",
                lambda record: record["field_runs"][0]["audit"].__setitem__(
                    "supported_value", "value-from-another-field"
                ),
                "record_lookup_supported_value_mismatch",
            ),
        )
        for name, mutate, expected_code in cases:
            with self.subTest(name=name):
                record = copy.deepcopy(base)
                mutate(record)
                trace = final_audit.validate_graph_retrieval_trace(
                    record,
                    record["question_evidence_graph"],
                    frozenset({"record_lookup"}),
                )
                self.assertEqual(trace["status"], "blocked")
                self.assertIn(
                    expected_code,
                    {failure["code"] for failure in trace["failures"]},
                )

    def test_decimal_point_cannot_be_erased_by_text_normalization(self) -> None:
        record = record_lookup_record()
        record["question_evidence_graph"]["branches"][0]["value"] = "1.00"
        record["field_runs"][0]["audit"]["supported_value"] = "100"

        trace = final_audit.validate_graph_retrieval_trace(
            record,
            record["question_evidence_graph"],
            frozenset({"record_lookup"}),
        )

        self.assertEqual(trace["status"], "blocked")
        self.assertIn(
            "record_lookup_supported_value_mismatch",
            {failure["code"] for failure in trace["failures"]},
        )
        self.assertTrue(final_audit._branch_value_matches("1", "1.00"))

    def test_item_id_punctuation_cannot_collapse_across_branches(self) -> None:
        record = record_lookup_record()
        record["question_evidence_graph"]["branches"][0]["item_id"] = "F-1"

        trace = final_audit.validate_graph_retrieval_trace(
            record,
            record["question_evidence_graph"],
            frozenset({"record_lookup"}),
        )

        self.assertEqual(trace["status"], "blocked")
        self.assertTrue({
            "record_lookup_field_item_mismatch",
            "record_lookup_audit_item_mismatch",
        } <= {failure["code"] for failure in trace["failures"]})

    def test_text_value_punctuation_is_not_erased(self) -> None:
        record = record_lookup_record()
        record["question_evidence_graph"]["branches"][0]["value"] = "2030-01-15"
        record["field_runs"][0]["audit"]["supported_value"] = "20250812"

        trace = final_audit.validate_graph_retrieval_trace(
            record,
            record["question_evidence_graph"],
            frozenset({"record_lookup"}),
        )

        self.assertEqual(trace["status"], "blocked")
        self.assertIn(
            "record_lookup_supported_value_mismatch",
            {failure["code"] for failure in trace["failures"]},
        )
        self.assertTrue(final_audit._branch_value_matches(
            "  Example Operations   / Aoki ", "Example Operations / Aoki"
        ))
        self.assertFalse(final_audit._branch_value_matches("AB-CD", "ABCD"))

    def test_formula_projection_preserves_semantic_operand_spaces(self) -> None:
        self.assertTrue(final_audit._branch_value_matches(
            "= d3 * e3", "=D3*E3",
        ))
        self.assertFalse(final_audit._branch_value_matches(
            '="East Division"', '="EastDivision"',
        ))
        self.assertFalse(final_audit._branch_value_matches(
            "='East Division'!A1", "='EastDivision'!A1",
        ))
        self.assertFalse(final_audit._branch_value_matches(
            "=Table1[East Division]", "=Table1[EastDivision]",
        ))
        self.assertFalse(final_audit._branch_value_matches(
            "=A1 B1", "=A1B1",
        ))
        self.assertFalse(final_audit._branch_value_matches(
            '="North ""Region"" Team"', '="North ""Region""Team"',
        ))
        self.assertFalse(final_audit._branch_value_matches(
            "='North ''Region'' Team'!A1", "='North ''Region''Team'!A1",
        ))
        self.assertTrue(final_audit._branch_value_matches(
            "= SUM( A1 , B1 )", "=sum(a1,b1)",
        ))
        self.assertTrue(final_audit._branch_value_matches(
            "=A1   B1", "=a1 B1",
        ))
        self.assertFalse(final_audit._branch_value_matches(
            '="North"', '="north"',
        ))
        self.assertFalse(final_audit._branch_value_matches(
            "=Table1[North]", "=table1[north]",
        ))
        self.assertFalse(final_audit._branch_value_matches(
            "=Table1[保存値:列]", "=Table1",
        ))
        self.assertFalse(final_audit._branch_value_matches(
            "=Table1[保存値:10000]", "=Table1",
        ))


if __name__ == "__main__":
    unittest.main()
