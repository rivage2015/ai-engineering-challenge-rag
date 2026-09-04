from __future__ import annotations

import copy
import hashlib
import importlib.util
import sys
import unittest
from pathlib import Path
from typing import Any
from unittest import mock


REPOSITORY = Path(__file__).resolve().parents[3]
MODULE_PATH = (
    REPOSITORY
    / "distribution"
    / "macos-local-memory"
    / "app"
    / "semantic_graph_answer_promotion.py"
)


def load_module() -> Any:
    specification = importlib.util.spec_from_file_location(
        "semantic_graph_answer_promotion_test_target", MODULE_PATH
    )
    assert specification is not None and specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = module
    specification.loader.exec_module(module)
    return module


promotion = load_module()


class SemanticGraphAnswerPromotionTests(unittest.TestCase):
    maxDiff = None

    def setUp(self) -> None:
        self.question = (
            "Project Orionの「移行リハーサル統括」の"
            "2026年9月4日時点の主担当者は誰ですか？"
        )
        self.reference_date = "2026-09-04"
        self.snapshot = "xkgs_" + "a" * 32
        self.logical_snapshot = "e" * 64
        self.projection = "f" * 64
        self.build_id = "1" * 32
        self.registration = {
            "schema_version": "0.1",
            "status": "validated_storage_only",
            "generation": "generation-" + "b" * 32,
            "database_sha256": "c" * 64,
            "graph_snapshot_id": self.snapshot,
            "logical_snapshot_sha256": self.logical_snapshot,
        }
        quote = "2026年9月4日時点の主担当者はPerson Aです。"
        self.candidate = {
            "schema_version": "0.1",
            "record_type": "cross_document_semantic_graph_query_candidate",
            "adapter": "cross-document-semantic-graph-runtime",
            "adapter_version": "0.1.0",
            "status": "accepted",
            "decision": "ACCEPTED",
            "reason_code": None,
            "diagnostic_code": None,
            "operation": "owner",
            "answer_text": "2026年9月4日時点の主担当者はPerson Aです。",
            "asserted_facts": [{
                "field": "assignee_name",
                "value": "Person A",
                "proof_edge_ids": ["edge_1"],
            }],
            "asserted_relations": [],
            "trace": {
                "graph_snapshot_id": self.snapshot,
                "question_hash": promotion.question_sha256(self.question),
                "question_reference_date": self.reference_date,
                "used_semantic_edge_ids": ["edge_1"],
                "used_edge_statuses": ["verified"],
                "database_opened": True,
                "outbound_network_attempt_count": 0,
                "resolved_source_references": [{
                    "edge_id": "edge_1",
                    "evidence_id": "evidence_1",
                    "document_id": "document_1",
                    "path": "source.docx",
                    "source_sha256": "d" * 64,
                    "locator": {"paragraph": 2},
                    "observed_text_sha256": hashlib.sha256(
                        quote.encode("utf-8")
                    ).hexdigest(),
                    "quote": quote,
                }],
            },
            "runtime_attestation": {
                "read_only": True,
                "generation": self.registration["generation"],
                "build_id": self.build_id,
                "graph_snapshot_id": self.snapshot,
                "logical_snapshot_sha256": self.logical_snapshot,
                "projection_sha256": self.projection,
                "outbound_network_attempt_count": 0,
            },
            "used_for_answers": False,
            "independent_edge_audit_status": "not_implemented_step4",
        }
        self.audit = self.make_audit(self.candidate)
        self.trust_binding = {
            "generation": self.registration["generation"],
            "build_id": self.build_id,
            "manifest_sha256": "2" * 64,
            "keychain_service": promotion.KEYCHAIN_SERVICE,
            "keychain_account": self.registration["generation"],
            "activation_policy_version": promotion.ACTIVATION_POLICY_VERSION,
            "storage_registration_sha256": promotion.canonical_sha256(
                self.registration
            ),
            "graph_snapshot_id": self.snapshot,
            "logical_snapshot_sha256": self.logical_snapshot,
            "projection_sha256": self.projection,
        }
        self.config = {
            "generation": self.registration["generation"],
            promotion.TRUST_CONFIG_KEY: {
                "schema_version": "0.1",
                "status": "trusted",
                "manifest_path": "/tmp/test-trust-manifest.json",
                **self.trust_binding,
            },
        }
        self.legacy = {
            "answer_status": "insufficient",
            "answer_mode": "insufficient",
            "answer": "わかりません",
            "evidence_ids": [],
            "basis_summary": "従来検索では足りません。",
            "uncertainties": ["直接根拠不足"],
            "non_answer_reason": {
                "code": "missing_evidence",
                "explanation": "直接根拠がありません。",
            },
            "diagnostic_evidence_ids": [],
            "needed_information": ["担当者の記録"],
            "follow_up_question": "資料を追加しますか？",
            "reconsideration_condition": "資料追加後。",
            "verification_reminder": "",
        }

    def make_audit(self, candidate: dict[str, Any]) -> dict[str, Any]:
        return {
            "schema_version": "0.1",
            "record_type": "cross_document_semantic_graph_independent_edge_audit",
            "status": "passed",
            "verdict": "PASS",
            "reason_code": None,
            "diagnostic_code": None,
            "operation": candidate["operation"],
            "candidate_sha256": promotion.canonical_sha256(candidate),
            "registration_sha256": promotion.canonical_sha256(
                self.registration
            ),
            "question_sha256": promotion.question_sha256(self.question),
            "question_reference_date": self.reference_date,
            "graph_snapshot_id": self.snapshot,
            "reconstructed_semantics_sha256": promotion.canonical_sha256(
                promotion.deterministic_candidate_semantics(candidate)
            ),
            "checks": {
                "candidate_contract": "PASS",
                "question_classification": "PASS",
                "registered_storage_integrity": "PASS",
                "independent_graph_reconstruction": "PASS",
                "candidate_semantics": "PASS",
            },
            "audit_attestation": {
                "read_only": True,
                "database_opened": True,
                "generation": self.registration["generation"],
                "graph_snapshot_id": self.snapshot,
                "logical_snapshot_sha256": self.logical_snapshot,
                "projection_sha256": self.projection,
                "outbound_network_attempt_count": 0,
            },
            "used_for_answers": False,
            "allows_answer_activation": False,
        }

    @staticmethod
    def accept_candidate(*_args: object) -> bool:
        return True

    @staticmethod
    def accept_audit(*_args: object) -> bool:
        return True

    def accept_trust(self, *_args: object) -> dict[str, Any]:
        return copy.deepcopy(self.trust_binding)

    @staticmethod
    def accept_latest_config(*_args: object) -> bool:
        return True

    @staticmethod
    def accept_answer(*_args: object) -> None:
        return None

    def run_gate(self, **changes: Any) -> tuple[dict[str, Any], dict[str, Any]]:
        inputs = {
            "legacy_answer": self.legacy,
            "question": self.question,
            "reference_date": self.reference_date,
            "candidate": self.candidate,
            "audit": self.audit,
            "registration": self.registration,
            "feature_enabled": True,
            "activation_available": True,
            "initial_config": copy.deepcopy(self.config),
            "latest_config": copy.deepcopy(self.config),
            "candidate_validator": self.accept_candidate,
            "audit_validator": self.accept_audit,
            "trust_validator": self.accept_trust,
            "latest_config_validator": self.accept_latest_config,
            "final_config_loader": lambda: copy.deepcopy(self.config),
            "answer_validator": self.accept_answer,
        }
        inputs.update(changes)
        return promotion.promote_answer(**inputs)

    def assert_fallback(
        self,
        selected: dict[str, Any],
        record: dict[str, Any],
        reason: str,
    ) -> None:
        self.assertEqual(self.legacy, selected)
        self.assertIsNot(self.legacy, selected)
        self.assertEqual(promotion.PROMOTION_FIELDS, set(record))
        self.assertEqual("fallback", record["status"])
        self.assertEqual("FALLBACK", record["decision"])
        self.assertEqual("legacy", record["source_answer"])
        self.assertEqual(reason, record["reason_code"])
        self.assertFalse(record["used_for_answers"])
        self.assertEqual({}, record["projected_answer"])
        self.assertEqual([], record["evidence_ids"])
        self.assertEqual([], record["source_references"])
        self.assertEqual({}, record["trust_binding"])
        self.assertIsNone(record["initial_config_sha256"])
        self.assertIsNone(record["latest_config_sha256"])
        self.assertIsNone(record["final_config_sha256"])
        selected["needed_information"].append("mutated fallback")
        self.assertNotEqual(self.legacy, selected)
        self.assertNotIn("mutated fallback", self.legacy["needed_information"])

    def test_promotes_all_three_supported_operations(self) -> None:
        for operation in sorted(promotion.SUPPORTED_OPERATIONS):
            with self.subTest(operation=operation):
                candidate = copy.deepcopy(self.candidate)
                candidate["operation"] = operation
                audit = self.make_audit(candidate)
                selected, record = self.run_gate(
                    candidate=candidate,
                    audit=audit,
                )
                self.assertEqual("promoted", record["status"])
                self.assertEqual("PROMOTE", record["decision"])
                self.assertEqual("semantic_graph", record["source_answer"])
                self.assertEqual(operation, record["operation"])
                self.assertTrue(record["used_for_answers"])
                self.assertEqual("grounded", selected["answer_mode"])
                self.assertEqual(candidate["answer_text"], selected["answer"])
                self.assertEqual(["evidence_1"], selected["evidence_ids"])
                self.assertEqual(selected, record["projected_answer"])
                self.assertEqual(self.trust_binding, record["trust_binding"])
                expected_config_hash = promotion.canonical_sha256(self.config)
                self.assertEqual(
                    expected_config_hash,
                    record["initial_config_sha256"],
                )
                self.assertEqual(
                    expected_config_hash,
                    record["latest_config_sha256"],
                )
                self.assertEqual(
                    expected_config_hash,
                    record["final_config_sha256"],
                )
                self.assertTrue(
                    all(value == "PASS" for value in record["checks"].values())
                )
                self.assertFalse(candidate["used_for_answers"])
                self.assertFalse(audit["used_for_answers"])
                self.assertFalse(audit["allows_answer_activation"])

    def test_promoted_answer_and_record_are_deep_copies(self) -> None:
        candidate_before = copy.deepcopy(self.candidate)
        audit_before = copy.deepcopy(self.audit)
        legacy_before = copy.deepcopy(self.legacy)
        selected, record = self.run_gate()
        selected["evidence_ids"].append("mutated")
        record["source_references"][0]["locator"]["paragraph"] = 999
        self.assertEqual(candidate_before, self.candidate)
        self.assertEqual(audit_before, self.audit)
        self.assertEqual(legacy_before, self.legacy)

    def test_feature_disabled_or_non_boolean_falls_back(self) -> None:
        for value in (False, None, 1, "true"):
            with self.subTest(value=value):
                selected, record = self.run_gate(feature_enabled=value)
                self.assert_fallback(selected, record, "feature_disabled")
                self.assertEqual("FAIL", record["checks"]["feature_enabled"])

    def test_held_and_not_applicable_candidates_never_promote(self) -> None:
        for status, decision in (("held", "HOLD"), ("not_applicable", "NOT_APPLICABLE")):
            with self.subTest(status=status):
                candidate = copy.deepcopy(self.candidate)
                candidate["status"] = status
                candidate["decision"] = decision
                candidate["operation"] = None if status == "not_applicable" else "owner"
                selected, record = self.run_gate(candidate=candidate)
                self.assert_fallback(
                    selected, record, "candidate_not_accepted"
                )

    def test_candidate_validator_false_or_exception_falls_back(self) -> None:
        for validator, diagnostic in (
            (lambda *_args: False, None),
            (lambda *_args: (_ for _ in ()).throw(RuntimeError("boom")), "RuntimeError"),
        ):
            with self.subTest(diagnostic=diagnostic):
                selected, record = self.run_gate(candidate_validator=validator)
                self.assert_fallback(
                    selected, record, "candidate_contract_invalid"
                )
                self.assertEqual(diagnostic, record["diagnostic_code"])

    def test_rejected_audit_and_validator_failure_fall_back(self) -> None:
        rejected = copy.deepcopy(self.audit)
        rejected["status"] = "rejected"
        rejected["verdict"] = "REJECT"
        selected, record = self.run_gate(audit=rejected)
        self.assert_fallback(
            selected, record, "independent_edge_audit_rejected"
        )

        selected, record = self.run_gate(audit_validator=lambda *_args: False)
        self.assert_fallback(
            selected, record, "independent_edge_audit_contract_invalid"
        )

    def test_missing_audit_or_registration_falls_back(self) -> None:
        selected, record = self.run_gate(audit=None)
        self.assert_fallback(
            selected, record, "independent_edge_audit_absent"
        )
        selected, record = self.run_gate(registration=None)
        self.assert_fallback(selected, record, "registered_storage_invalid")

    def test_trust_validator_false_or_exception_falls_back(self) -> None:
        class StableTrustError(ValueError):
            reason_code = "trust_root_mismatch"

        for validator, diagnostic in (
            (lambda *_args: False, None),
            (lambda *_args: (_ for _ in ()).throw(OSError("boom")), "OSError"),
            (
                lambda *_args: (_ for _ in ()).throw(StableTrustError()),
                "trust_root_mismatch",
            ),
        ):
            with self.subTest(diagnostic=diagnostic):
                selected, record = self.run_gate(trust_validator=validator)
                self.assert_fallback(
                    selected, record, "trust_root_binding_invalid"
                )
                self.assertEqual(diagnostic, record["diagnostic_code"])
                self.assertEqual(
                    "FAIL",
                    record["checks"]["registered_storage_integrity"],
                )

    def test_tampered_trust_receipt_never_promotes(self) -> None:
        for field, value in (
            ("manifest_sha256", "0" * 64),
            ("keychain_account", "generation-" + "0" * 32),
            ("build_id", "0" * 32),
            ("projection_sha256", "0" * 64),
        ):
            with self.subTest(field=field):
                receipt = copy.deepcopy(self.trust_binding)
                receipt[field] = value
                selected, record = self.run_gate(
                    trust_validator=lambda *_args, receipt=receipt: receipt,
                )
                self.assert_fallback(
                    selected,
                    record,
                    "trust_root_binding_invalid",
                )
                self.assertEqual(
                    "FAIL",
                    record["checks"]["trust_root_binding"],
                )

    def test_latest_config_binding_is_an_independent_gate(self) -> None:
        observed = {}

        def validator(
            initial: object,
            latest: object,
            registration: dict[str, Any],
        ) -> bool:
            observed.update({
                "initial": initial,
                "latest": latest,
                "registration": registration,
            })
            return False

        selected, record = self.run_gate(
            initial_config={"version": "start"},
            latest_config={"version": "changed"},
            latest_config_validator=validator,
        )
        self.assert_fallback(
            selected, record, "latest_config_binding_invalid"
        )
        self.assertEqual("FAIL", record["checks"]["latest_config_binding"])
        self.assertEqual("start", observed["initial"]["version"])
        self.assertEqual("changed", observed["latest"]["version"])
        self.assertEqual(self.registration, observed["registration"])

    def test_busy_activation_lease_falls_back_before_trust(self) -> None:
        trust_called = False

        def trust_validator(*_args: object) -> bool:
            nonlocal trust_called
            trust_called = True
            return True

        selected, record = self.run_gate(
            activation_available=False,
            trust_validator=trust_validator,
        )
        self.assert_fallback(selected, record, "promotion_activation_busy")
        self.assertEqual("FAIL", record["checks"]["activation_lease"])
        self.assertFalse(trust_called)

    def test_final_config_change_after_trust_falls_back(self) -> None:
        selected, record = self.run_gate(
            final_config_loader=lambda: {"generation": "changed"},
        )
        self.assert_fallback(
            selected,
            record,
            "final_config_binding_invalid",
        )
        self.assertEqual("PASS", record["checks"]["trust_root_binding"])
        self.assertEqual("FAIL", record["checks"]["final_config_binding"])

    def test_answer_validator_receives_existing_answer_contract_shape(self) -> None:
        observed = {}

        def validator(
            answer: dict[str, Any],
            allowed_ids: set[str],
            expected_mode: str,
            reminder_required: bool,
        ) -> None:
            observed.update({
                "answer": answer,
                "allowed_ids": allowed_ids,
                "expected_mode": expected_mode,
                "reminder_required": reminder_required,
            })

        selected, record = self.run_gate(answer_validator=validator)
        self.assertEqual("promoted", record["status"])
        self.assertEqual(selected, observed["answer"])
        self.assertEqual({"evidence_1"}, observed["allowed_ids"])
        self.assertEqual("grounded", observed["expected_mode"])
        self.assertIs(False, observed["reminder_required"])

        for rejecting in (
            lambda *_args: False,
            lambda *_args: (_ for _ in ()).throw(ValueError("invalid")),
        ):
            with self.subTest(rejecting=rejecting):
                selected, record = self.run_gate(answer_validator=rejecting)
                self.assert_fallback(
                    selected, record, "answer_projection_invalid"
                )
                self.assertEqual("FAIL", record["checks"]["answer_projection"])

    def test_candidate_audit_hash_and_question_bindings_are_enforced(self) -> None:
        mutations = []
        wrong_candidate = copy.deepcopy(self.audit)
        wrong_candidate["candidate_sha256"] = "0" * 64
        mutations.append(wrong_candidate)
        wrong_registration = copy.deepcopy(self.audit)
        wrong_registration["registration_sha256"] = "0" * 64
        mutations.append(wrong_registration)
        wrong_question = copy.deepcopy(self.audit)
        wrong_question["question_sha256"] = "0" * 64
        mutations.append(wrong_question)
        wrong_semantics = copy.deepcopy(self.audit)
        wrong_semantics["reconstructed_semantics_sha256"] = "0" * 64
        mutations.append(wrong_semantics)
        for audit in mutations:
            with self.subTest(field=next(
                key for key in audit if audit[key] != self.audit.get(key)
            )):
                selected, record = self.run_gate(audit=audit)
                self.assert_fallback(
                    selected, record, "candidate_audit_binding_invalid"
                )

    def test_auditor_cannot_self_grant_activation_authority(self) -> None:
        audit = copy.deepcopy(self.audit)
        audit["allows_answer_activation"] = True
        selected, record = self.run_gate(audit=audit)
        self.assert_fallback(
            selected, record, "candidate_audit_binding_invalid"
        )

    def test_reference_date_mismatch_and_invalid_date_fall_back(self) -> None:
        selected, record = self.run_gate(reference_date="2026-09-05")
        self.assert_fallback(
            selected, record, "reference_date_binding_invalid"
        )
        self.assertEqual("FAIL", record["checks"]["reference_date_binding"])
        selected, record = self.run_gate(reference_date="2026-9-4")
        self.assert_fallback(
            selected, record, "reference_date_binding_invalid"
        )

    def test_missing_or_tampered_evidence_falls_back(self) -> None:
        missing = copy.deepcopy(self.candidate)
        missing["trace"]["resolved_source_references"] = []
        missing_audit = self.make_audit(missing)
        selected, record = self.run_gate(candidate=missing, audit=missing_audit)
        self.assert_fallback(
            selected, record, "candidate_audit_binding_invalid"
        )

    def test_more_than_ten_disjoint_evidence_ids_never_promotes(self) -> None:
        candidate = copy.deepcopy(self.candidate)
        template = candidate["trace"]["resolved_source_references"][0]
        references = []
        edge_ids = []
        for index in range(11):
            edge_id = f"edge_{index}"
            evidence_id = f"evidence_{index}"
            edge_ids.append(edge_id)
            reference = copy.deepcopy(template)
            reference["edge_id"] = edge_id
            reference["evidence_id"] = evidence_id
            references.append(reference)
        candidate["trace"]["used_semantic_edge_ids"] = edge_ids
        candidate["trace"]["resolved_source_references"] = references
        audit = self.make_audit(candidate)
        selected, record = self.run_gate(candidate=candidate, audit=audit)
        self.assert_fallback(
            selected,
            record,
            "answer_evidence_budget_exceeded",
        )
        self.assertEqual("FAIL", record["checks"]["evidence_projection"])

    def test_shared_evidence_can_cover_more_than_ten_used_edges(self) -> None:
        candidate = copy.deepcopy(self.candidate)
        template = candidate["trace"]["resolved_source_references"][0]
        references = []
        edge_ids = []
        for index in range(11):
            edge_id = f"edge_{index:02d}"
            edge_ids.append(edge_id)
            unique = copy.deepcopy(template)
            unique["edge_id"] = edge_id
            unique["evidence_id"] = f"a_unique_{index:02d}"
            common = copy.deepcopy(template)
            common["edge_id"] = edge_id
            common["evidence_id"] = "z_common"
            references.extend((unique, common))
        candidate["trace"]["used_semantic_edge_ids"] = edge_ids
        candidate["trace"]["resolved_source_references"] = references
        audit = self.make_audit(candidate)

        selected, record = self.run_gate(candidate=candidate, audit=audit)

        self.assertEqual("PROMOTE", record["decision"])
        self.assertEqual(["z_common"], selected["evidence_ids"])
        self.assertEqual(references, record["source_references"])

    def test_exact_cover_rescues_a_valid_solution_after_greedy_fails(
        self,
    ) -> None:
        edge_ids = [f"edge_{index}" for index in range(1, 7)]
        coverage = {
            "evidence_a": edge_ids[:4],
            "evidence_b": ["edge_1", "edge_2", "edge_5"],
            "evidence_c": ["edge_3", "edge_4", "edge_6"],
        }
        references = [
            {"edge_id": edge_id, "evidence_id": evidence_id}
            for evidence_id, covered_edges in coverage.items()
            for edge_id in covered_edges
        ]
        with mock.patch.object(
            promotion,
            "MAX_ANSWER_EVIDENCE_IDS",
            2,
        ):
            selected = promotion._bounded_evidence_cover(
                edge_ids,
                references,
            )
        self.assertEqual(["evidence_b", "evidence_c"], selected)

    def test_exact_cover_search_exhaustion_fails_closed(self) -> None:
        edge_ids = [f"edge_{index}" for index in range(1, 7)]
        coverage = {
            "evidence_a": edge_ids[:4],
            "evidence_b": ["edge_1", "edge_2", "edge_5"],
            "evidence_c": ["edge_3", "edge_4", "edge_6"],
        }
        references = [
            {"edge_id": edge_id, "evidence_id": evidence_id}
            for evidence_id, covered_edges in coverage.items()
            for edge_id in covered_edges
        ]
        with (
            mock.patch.object(
                promotion,
                "MAX_ANSWER_EVIDENCE_IDS",
                2,
            ),
            mock.patch.object(
                promotion,
                "MAX_EVIDENCE_COVER_SEARCH_STATES",
                1,
            ),
        ):
            with self.assertRaisesRegex(
                ValueError,
                "promotion_evidence_cover_search_exhausted",
            ):
                promotion._bounded_evidence_cover(edge_ids, references)

    def _default_budget_adversarial_cover(
        self,
    ) -> tuple[list[str], list[dict[str, Any]]]:
        core_edges = [f"edge_core_{index}" for index in range(1, 7)]
        extra_edges = [f"edge_extra_{index}" for index in range(1, 9)]
        coverage = {
            "evidence_trap": core_edges[:4],
            "evidence_opt_b": [
                "edge_core_1",
                "edge_core_2",
                "edge_core_5",
            ],
            "evidence_opt_c": [
                "edge_core_3",
                "edge_core_4",
                "edge_core_6",
            ],
            **{
                f"evidence_extra_{index}": [edge_id]
                for index, edge_id in enumerate(extra_edges, start=1)
            },
        }
        template = self.candidate["trace"][
            "resolved_source_references"
        ][0]
        references = []
        for evidence_id, covered_edges in coverage.items():
            for edge_id in covered_edges:
                reference = copy.deepcopy(template)
                reference["edge_id"] = edge_id
                reference["evidence_id"] = evidence_id
                references.append(reference)
        return core_edges + extra_edges, references

    def test_exact_cover_rescues_real_ten_item_budget(self) -> None:
        edge_ids, references = self._default_budget_adversarial_cover()
        selected = promotion._bounded_evidence_cover(edge_ids, references)
        self.assertEqual(10, len(selected))
        self.assertIn("evidence_opt_b", selected)
        self.assertIn("evidence_opt_c", selected)
        self.assertNotIn("evidence_trap", selected)

    def test_public_gate_maps_cover_search_exhaustion_to_fallback(
        self,
    ) -> None:
        candidate = copy.deepcopy(self.candidate)
        edge_ids, references = self._default_budget_adversarial_cover()
        candidate["trace"]["used_semantic_edge_ids"] = edge_ids
        candidate["trace"]["resolved_source_references"] = references
        audit = self.make_audit(candidate)
        with mock.patch.object(
            promotion,
            "MAX_EVIDENCE_COVER_SEARCH_STATES",
            1,
        ):
            selected, record = self.run_gate(
                candidate=candidate,
                audit=audit,
            )
        self.assert_fallback(
            selected,
            record,
            "answer_evidence_cover_search_exhausted",
        )
        self.assertEqual("FAIL", record["checks"]["evidence_projection"])

    def test_shared_evidence_id_with_conflicting_content_never_promotes(
        self,
    ) -> None:
        candidate = copy.deepcopy(self.candidate)
        first = candidate["trace"]["resolved_source_references"][0]
        second = copy.deepcopy(first)
        second["edge_id"] = "edge_2"
        second["quote"] = "同じEvidence IDを別内容へ差し替えました。"
        second["observed_text_sha256"] = hashlib.sha256(
            second["quote"].encode("utf-8")
        ).hexdigest()
        candidate["trace"]["used_semantic_edge_ids"] = ["edge_1", "edge_2"]
        candidate["trace"]["resolved_source_references"].append(second)
        audit = self.make_audit(candidate)

        selected, record = self.run_gate(candidate=candidate, audit=audit)

        self.assert_fallback(
            selected,
            record,
            "candidate_audit_binding_invalid",
        )
        self.assertEqual(
            "promotion_evidence_identity_conflict",
            record["diagnostic_code"],
        )

    def test_all_references_are_retained_with_one_answer_id_per_edge(self) -> None:
        candidate = copy.deepcopy(self.candidate)
        template = candidate["trace"]["resolved_source_references"][0]
        references = []
        for index in range(24):
            edge_id = "edge_1" if index < 12 else "edge_2"
            quote = f"validated source statement {index}"
            reference = copy.deepcopy(template)
            reference.update({
                "edge_id": edge_id,
                "evidence_id": f"evidence_{index:02d}",
                "quote": quote,
                "observed_text_sha256": hashlib.sha256(
                    quote.encode("utf-8")
                ).hexdigest(),
            })
            references.append(reference)
        candidate["trace"]["used_semantic_edge_ids"] = ["edge_1", "edge_2"]
        candidate["trace"]["resolved_source_references"] = references
        audit = self.make_audit(candidate)

        selected, record = self.run_gate(candidate=candidate, audit=audit)

        self.assertEqual("PROMOTE", record["decision"])
        self.assertEqual(24, len(record["source_references"]))
        self.assertEqual(
            ["evidence_00", "evidence_12"],
            record["evidence_ids"],
        )
        self.assertEqual(record["evidence_ids"], selected["evidence_ids"])

    def test_tampered_evidence_falls_back(self) -> None:

        tampered = copy.deepcopy(self.candidate)
        tampered["trace"]["resolved_source_references"][0]["quote"] += "X"
        tampered_audit = self.make_audit(tampered)
        selected, record = self.run_gate(candidate=tampered, audit=tampered_audit)
        self.assert_fallback(
            selected, record, "candidate_audit_binding_invalid"
        )

    def test_inputs_are_frozen_before_untrusted_callbacks_run(self) -> None:
        original = copy.deepcopy(self.candidate)

        def mutating_validator(candidate: dict[str, Any], *_args: object) -> bool:
            candidate["answer_text"] = "mutated in callback"
            return True

        selected, record = self.run_gate(candidate_validator=mutating_validator)
        self.assertEqual("promoted", record["status"])
        self.assertEqual(original["answer_text"], selected["answer"])
        self.assertEqual(original, self.candidate)

        def mutating_audit_validator(
            audit: dict[str, Any],
            candidate: dict[str, Any],
            registration: dict[str, Any],
            *_args: object,
        ) -> bool:
            candidate["answer_text"] = "coordinated mutation"
            registration["graph_snapshot_id"] = "xkgs_" + "0" * 32
            audit["graph_snapshot_id"] = registration["graph_snapshot_id"]
            return True

        selected, record = self.run_gate(
            audit_validator=mutating_audit_validator
        )
        self.assertEqual("promoted", record["status"])
        self.assertEqual(original["answer_text"], selected["answer"])

        def mutating_trust_validator(
            _registration: dict[str, Any],
            candidate: dict[str, Any],
            _audit: dict[str, Any],
        ) -> dict[str, Any]:
            candidate["answer_text"] = "post-binding mutation"
            return copy.deepcopy(self.trust_binding)

        selected, record = self.run_gate(
            trust_validator=mutating_trust_validator
        )
        self.assertEqual("promoted", record["status"])
        self.assertEqual(original["answer_text"], selected["answer"])


if __name__ == "__main__":
    unittest.main()
