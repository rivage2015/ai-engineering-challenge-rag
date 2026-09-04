from __future__ import annotations

import copy
import importlib.util
import json
import os
import shutil
import sqlite3
import sys
import unittest
from contextlib import closing
from datetime import date, timedelta
from pathlib import Path
from typing import Any


REPOSITORY = Path(__file__).resolve().parents[3]
APP = REPOSITORY / "distribution" / "macos-local-memory" / "app"
ENGINE = REPOSITORY / "distribution" / "macos-local-memory" / "engine"
PROJECTION_TEST = (
    REPOSITORY / "tests" / "test_project_cross_document_graph_to_answer_index.py"
)
RUNTIME_TEST = (
    REPOSITORY
    / "distribution"
    / "macos-local-memory"
    / "tests"
    / "test_cross_document_semantic_graph_runtime.py"
)


def load_module(name: str, path: Path) -> Any:
    specification = importlib.util.spec_from_file_location(name, path)
    assert specification is not None and specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    sys.modules[name] = module
    specification.loader.exec_module(module)
    return module


projection_fixture = load_module(
    "step5_e2e_projection_fixture",
    PROJECTION_TEST,
)
runtime_fixture = load_module(
    "step5_e2e_runtime_fixture",
    RUNTIME_TEST,
)
runtime = load_module(
    "step5_e2e_runtime",
    ENGINE / "cross_document_semantic_graph_runtime.py",
)
auditor = load_module(
    "step5_e2e_edge_auditor",
    APP / "cross_document_semantic_graph_edge_audit.py",
)
trust = load_module(
    "step5_e2e_trust",
    APP / "semantic_graph_trust.py",
)
promotion = load_module(
    "step5_e2e_promotion",
    APP / "semantic_graph_answer_promotion.py",
)


class MemoryTrustStore:
    """Create/read-only test double; it never calls the macOS Keychain."""

    def __init__(self, roots: dict[str, str] | None = None) -> None:
        self.roots = dict(roots or {})
        self.create_calls: list[tuple[str, str]] = []
        self.read_calls: list[str] = []

    def create_root(self, generation: str, root_sha256: str) -> None:
        self.create_calls.append((generation, root_sha256))
        if generation in self.roots:
            raise trust.SemanticGraphTrustError("trust_store_create_failed")
        self.roots[generation] = root_sha256

    def read_root(self, generation: str) -> str:
        self.read_calls.append(generation)
        try:
            return self.roots[generation]
        except KeyError as exc:
            raise trust.SemanticGraphTrustError(
                "trust_store_read_failed"
            ) from exc


class Step5AnswerPromotionEndToEndTests(unittest.TestCase):
    """Exercise the real candidate, independent audit, trust, and gate chain."""

    maxDiff = None

    @classmethod
    def setUpClass(cls) -> None:
        fixture_type = (
            projection_fixture.CrossDocumentGraphAnswerIndexProjectionTests
        )
        cls.fixture = fixture_type(
            methodName="test_success_copies_validated_graph_without_changing_base"
        )
        cls.fixture.setUp()
        try:
            cls._build_minimal_synthetic_graph()
        except BaseException:
            cls.fixture.tearDown()
            raise

    @classmethod
    def tearDownClass(cls) -> None:
        cls.fixture.tearDown()

    @classmethod
    def _build_minimal_synthetic_graph(cls) -> None:
        fixture = cls.fixture
        for path in (fixture.semantic, fixture.security, fixture.shadow):
            shutil.rmtree(path)
        fixture.base_index.unlink()
        fixture.semantic.mkdir()
        fixture.security.mkdir()

        sources = {
            "doc_context": {
                "relative_path": "synthetic/context.docx",
                "sha256": "1" * 64,
                "extension": "docx",
            },
            "doc_assignment": {
                "relative_path": "synthetic/assignment.xlsx",
                "sha256": "2" * 64,
                "extension": "xlsx",
            },
            "doc_identity": {
                "relative_path": "synthetic/identity.pdf",
                "sha256": "3" * 64,
                "extension": "pdf",
            },
        }
        records: list[dict[str, Any]] = []

        def add(
            evidence_id: str,
            document_id: str,
            observed_text: str,
            source_record_type: str,
            locator: dict[str, Any],
        ) -> None:
            records.append({
                "evidence_id": evidence_id,
                "document_id": document_id,
                "source": sources[document_id],
                "locator": locator,
                "observed_text": observed_text,
                "ordinal": len(records) + 1,
                "adapter": {
                    "execution_policy": "never_execute",
                    "source_record_type": source_record_type,
                },
                "status": "observed",
            })

        # These values intentionally differ from the frozen evaluation corpus.
        # One explicit statement supplies the three subject-identity edges.
        add(
            "ev_context",
            "doc_context",
            (
                "案件別表記: Project ZephyrはProject ID: PRJ-Z9の別表記\n"
                "WORK-Z9はPRJ-Z9に属する業務「耐障害演習統括」の正式ID"
            ),
            "paragraph",
            {"paragraph_index": 1},
        )

        headers = (
            "Project ID",
            "Work ID",
            "Role",
            "Assignee ID",
            "Valid From",
            "Status",
        )
        values = (
            "PRJ-Z9",
            "WORK-Z9",
            "主担当",
            "EMP-Z9",
            "2024-01-01",
            "FINAL",
        )
        for row_number, row in enumerate((headers, values), start=1):
            for column_number, value in enumerate(row, start=1):
                add(
                    f"ev_assignment_{row_number}_{column_number}",
                    "doc_assignment",
                    value,
                    "table_cell",
                    {
                        "sheet_name": "Duty",
                        "cell": f"{chr(64 + column_number)}{row_number}",
                    },
                )

        # A single coordinate-free PDF page exercises the ordered-row fallback
        # while keeping the promoted Evidence set within the ten-item contract.
        add(
            "ev_identity",
            "doc_identity",
            (
                "Document Status: APPROVED\n"
                "Employee ID\nPerson Name\nStatus\n"
                "EMP-Z9\n架空花子\nActive"
            ),
            "page",
            {"page_number": 1},
        )
        documents = [
            {
                "document_id": document_id,
                "source": source,
                "evidence_ids": [
                    record["evidence_id"]
                    for record in records
                    if record["document_id"] == document_id
                ],
                "status": "extracted",
            }
            for document_id, source in sources.items()
        ]
        projection_fixture.write_jsonl(fixture.documents, documents)
        projection_fixture.write_jsonl(fixture.source_evidence, records)
        projection_fixture.security_builder.build(
            fixture.source_evidence,
            fixture.documents,
            fixture.security,
            created_at="2026-09-03T00:00:00+09:00",
        )
        fixture.records = projection_fixture.read_jsonl(fixture.evidence)
        cls.build_id = "5" * 32
        fixture._publish_current_shadow(build_id=cls.build_id)
        fixture._build_ready_answer_index()

        state = projection_fixture.projector.project(**fixture._arguments())
        runtime_fixture.add_source_hashes_to_fixture_index(
            fixture.output,
            fixture.records,
        )
        runtime_support = (
            runtime_fixture.CrossDocumentSemanticGraphRuntimeTests
        )
        runtime_support.bind_state_to_current_index(
            fixture.output,
            fixture.state,
        )
        final_directory = fixture.generation / runtime.INDEX_DIRECTORY
        os.replace(fixture.output_dir, final_directory)
        # TemporaryDirectory may be exposed through /var while resolve() uses
        # /private/var.  Use one canonical path spelling for the trust binding.
        cls.generation = fixture.generation.resolve(strict=True)
        cls.index = (
            cls.generation / runtime.INDEX_DIRECTORY / runtime.INDEX_FILENAME
        )
        cls.state_path = (
            cls.generation / runtime.INDEX_DIRECTORY / runtime.STATE_FILENAME
        )
        cls.registration = runtime_support.registration_for(cls.index)
        cls.storage_state = json.loads(
            cls.state_path.read_text(encoding="utf-8")
        )
        cls.assert_state_matches_projection = (
            state["shadow"]["graph_snapshot_id"]
            == cls.registration["graph_snapshot_id"]
        )

        publication_store = MemoryTrustStore()
        cls.trust_locator = trust.publish_trust_root(
            cls.generation,
            cls.build_id,
            cls.registration,
            cls.storage_state,
            publication_store,
        )
        cls.trust_root = publication_store.roots[cls.generation.name]

        # Build the question from the projected graph, not from any frozen QA
        # question constant or production answer literal.
        with closing(sqlite3.connect(cls.index)) as connection:
            project_alias = connection.execute(
                "SELECT canonical_key FROM semantic_graph_nodes "
                "WHERE node_type = 'ProjectAlias' ORDER BY canonical_key"
            ).fetchone()[0]
            work_name = connection.execute(
                "SELECT canonical_key FROM semantic_graph_nodes "
                "WHERE node_type = 'WorkName' ORDER BY canonical_key"
            ).fetchone()[0]
            properties = json.loads(connection.execute(
                "SELECT properties_json FROM semantic_graph_edges "
                "WHERE relation_type = 'ASSIGNED_TO' ORDER BY edge_id"
            ).fetchone()[0])
        active_date = date.fromisoformat(properties["valid_from"]) + timedelta(
            days=365
        )
        cls.question = (
            f"{project_alias}の「{work_name}」は、"
            f"{active_date.year}年{active_date.month}月{active_date.day}日時点で"
            "誰が主担当でしたか。"
        )
        cls.reference_date = None
        cls.candidate = runtime.evaluate_candidate(
            cls.index,
            cls.question,
            cls.generation.name,
            expected_build_id=cls.build_id,
            expected_registration=cls.registration,
            reference_date=cls.reference_date,
        )
        cls.edge_audit = auditor.audit_candidate(
            cls.index,
            cls.question,
            cls.registration,
            cls.candidate,
            reference_date=cls.reference_date,
        )

    def setUp(self) -> None:
        self.store = MemoryTrustStore({
            self.generation.name: self.trust_root,
        })
        self.legacy_answer = {
            "answer_status": "insufficient",
            "answer_mode": "insufficient",
            "answer": "わかりません",
            "evidence_ids": [],
            "basis_summary": "従来経路の根拠が足りません。",
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
        self.initial_config = {
            "active_generation": self.generation.name,
            "cross_document_semantic_graph_storage": copy.deepcopy(
                self.registration
            ),
            promotion.TRUST_CONFIG_KEY: copy.deepcopy(self.trust_locator),
            "cross_document_semantic_graph_answer_promotion_enabled": True,
            "configuration_epoch": "stable",
        }
        self.latest_config = copy.deepcopy(self.initial_config)

    @staticmethod
    def validate_candidate(
        candidate: object,
        registration: dict[str, Any],
        question: str,
        reference_date: str | None,
    ) -> bool:
        if not isinstance(candidate, dict):
            return False
        auditor._validate_candidate_contract(candidate)
        return (
            candidate["trace"]["question_hash"]
            == auditor.sha256_text(auditor._normalize_surface(question))
            and candidate["trace"]["question_reference_date"]
            == reference_date
            and candidate["runtime_attestation"]["generation"]
            == registration["generation"]
        )

    @staticmethod
    def validate_audit(
        audit: object,
        candidate: dict[str, Any],
        registration: dict[str, Any],
        question: str,
        reference_date: str | None,
    ) -> bool:
        if not isinstance(audit, dict) or set(audit) != auditor.AUDIT_FIELDS:
            return False
        return (
            audit["candidate_sha256"] == auditor.sha256_value(candidate)
            and audit["registration_sha256"]
            == auditor.sha256_value(registration)
            and audit["question_sha256"]
            == auditor.sha256_text(auditor._normalize_surface(question))
            and audit["question_reference_date"] == reference_date
        )

    @staticmethod
    def validate_config(
        initial: object,
        latest: object,
        registration: dict[str, Any],
    ) -> bool:
        return (
            isinstance(initial, dict)
            and isinstance(latest, dict)
            and initial == latest
            and latest.get("cross_document_semantic_graph_storage")
            == registration
            and latest.get(
                "cross_document_semantic_graph_answer_promotion_enabled"
            )
            is True
        )

    def validate_trust(
        self,
        registration: dict[str, Any],
        candidate: dict[str, Any],
        edge_audit: dict[str, Any],
    ) -> dict[str, Any] | bool:
        verified = trust.validate_trust_root(
            self.generation,
            registration,
            self.store,
        )
        trust.validate_trust_registration(
            self.trust_locator,
            self.generation,
            registration,
            verified_root=verified,
        )
        expected = {
            "generation": verified["generation"],
            "graph_snapshot_id": verified["graph_snapshot_id"],
            "logical_snapshot_sha256": verified["logical_snapshot_sha256"],
            "projection_sha256": verified["projection_sha256"],
        }
        candidate_attestation = candidate.get("runtime_attestation", {})
        audit_attestation = edge_audit.get("audit_attestation", {})
        if not (
            candidate_attestation.get("build_id") == verified["build_id"]
            and all(
                candidate_attestation.get(key) == value
                and audit_attestation.get(key) == value
                for key, value in expected.items()
            )
        ):
            return False
        return {
            key: verified[key] for key in promotion.TRUST_BINDING_FIELDS
        }

    @staticmethod
    def validate_answer(
        answer: dict[str, Any],
        allowed_ids: set[str],
        expected_mode: str,
        reminder_required: bool,
    ) -> None:
        projection_fixture.answer_validator.validate_answer(
            answer,
            allowed_ids,
            expected_mode,
            reminder_required,
        )

    def run_gate(
        self,
        *,
        candidate: object | None = None,
        edge_audit: object | None = None,
        final_config: object | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        selected_candidate = (
            copy.deepcopy(self.candidate)
            if candidate is None
            else copy.deepcopy(candidate)
        )
        selected_audit = (
            copy.deepcopy(self.edge_audit)
            if edge_audit is None
            else copy.deepcopy(edge_audit)
        )
        final_value = (
            copy.deepcopy(self.latest_config)
            if final_config is None
            else copy.deepcopy(final_config)
        )
        return promotion.promote_answer(
            legacy_answer=copy.deepcopy(self.legacy_answer),
            question=self.question,
            reference_date=self.reference_date,
            candidate=selected_candidate,
            audit=selected_audit,
            registration=copy.deepcopy(self.registration),
            feature_enabled=True,
            activation_available=True,
            initial_config=copy.deepcopy(self.initial_config),
            latest_config=copy.deepcopy(self.latest_config),
            final_config_loader=lambda: copy.deepcopy(final_value),
            candidate_validator=self.validate_candidate,
            audit_validator=self.validate_audit,
            trust_validator=self.validate_trust,
            latest_config_validator=self.validate_config,
            answer_validator=self.validate_answer,
        )

    def test_real_candidate_audit_trust_chain_promotes(self) -> None:
        self.assertTrue(self.assert_state_matches_projection)
        self.assertEqual("ACCEPTED", self.candidate["decision"])
        self.assertEqual("PASS", self.edge_audit["verdict"])
        evidence_ids = {
            reference["evidence_id"]
            for reference in self.candidate["trace"][
                "resolved_source_references"
            ]
        }
        self.assertLessEqual(len(evidence_ids), 10)

        selected, record = self.run_gate()

        self.assertEqual("PROMOTE", record["decision"])
        self.assertEqual("semantic_graph", record["source_answer"])
        self.assertTrue(record["used_for_answers"])
        self.assertEqual(self.candidate["answer_text"], selected["answer"])
        self.assertEqual("grounded", selected["answer_mode"])
        selected_evidence = set(selected["evidence_ids"])
        self.assertLessEqual(selected_evidence, evidence_ids)
        self.assertLessEqual(len(selected_evidence), 10)
        self.assertEqual(
            self.candidate["trace"]["resolved_source_references"],
            record["source_references"],
        )
        for edge_id in self.candidate["trace"]["used_semantic_edge_ids"]:
            covering_evidence = {
                reference["evidence_id"]
                for reference in record["source_references"]
                if reference["edge_id"] == edge_id
            }
            self.assertTrue(selected_evidence & covering_evidence)
        self.assertTrue(
            all(value == "PASS" for value in record["checks"].values())
        )
        self.assertFalse(self.candidate["used_for_answers"])
        self.assertFalse(self.edge_audit["used_for_answers"])
        self.assertFalse(self.edge_audit["allows_answer_activation"])
        self.assertGreaterEqual(len(self.store.read_calls), 1)

    def test_tampered_independent_root_falls_back_to_legacy_answer(self) -> None:
        self.store.roots[self.generation.name] = "f" * 64

        selected, record = self.run_gate()

        self.assertEqual(self.legacy_answer, selected)
        self.assertIsNot(self.legacy_answer, selected)
        self.assertEqual("FALLBACK", record["decision"])
        self.assertEqual("legacy", record["source_answer"])
        self.assertFalse(record["used_for_answers"])
        self.assertEqual("trust_root_binding_invalid", record["reason_code"])
        self.assertEqual("trust_root_mismatch", record["diagnostic_code"])
        self.assertEqual("FAIL", record["checks"]["trust_root_binding"])

    def test_final_config_change_after_trust_falls_back(self) -> None:
        changed = copy.deepcopy(self.latest_config)
        changed["configuration_epoch"] = "changed-after-trust"

        selected, record = self.run_gate(final_config=changed)

        self.assertEqual(self.legacy_answer, selected)
        self.assertEqual("FALLBACK", record["decision"])
        self.assertEqual("final_config_binding_invalid", record["reason_code"])
        self.assertEqual("FAIL", record["checks"]["final_config_binding"])
        self.assertGreaterEqual(len(self.store.read_calls), 1)


class Step5ExistingFiveDocumentEvidenceProjectionTests(unittest.TestCase):
    """Prove that a real large trace can still fit the answer schema."""

    maxDiff = None

    @classmethod
    def setUpClass(cls) -> None:
        cls.fixture_type = (
            runtime_fixture.CrossDocumentSemanticGraphRuntimeTests
        )
        cls.fixture_type.setUpClass()
        cls.fixture = cls.fixture_type()
        cls.fixture.setUp()
        try:
            cls.index = cls.fixture_type.index
            cls.generation = cls.fixture_type.generation
            # The older five-document fixture predates the production build-id
            # contract and uses a prose label.  Keep its graph content intact,
            # but give this Step 5 boundary test a production-shaped identity.
            cls.build_id = "5" * 32
            state_path = cls.fixture_type.state
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["shadow"]["build_id"] = cls.build_id
            state_path.write_text(
                json.dumps(
                    state,
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            cls.registration = cls.fixture.registration_for(cls.index)
            cls.generation = cls.generation.resolve(strict=True)
            storage_state = json.loads(
                cls.fixture_type.state.read_text(encoding="utf-8")
            )
            publication_store = MemoryTrustStore()
            cls.trust_locator = trust.publish_trust_root(
                cls.generation,
                cls.build_id,
                cls.registration,
                storage_state,
                publication_store,
            )
            cls.trust_root = publication_store.roots[cls.generation.name]
            with closing(sqlite3.connect(cls.index)) as connection:
                project_alias = connection.execute(
                    "SELECT canonical_key FROM semantic_graph_nodes "
                    "WHERE node_type = 'ProjectAlias' "
                    "AND canonical_key LIKE 'Project %' "
                    "ORDER BY canonical_key"
                ).fetchone()[0]
                work_name = connection.execute(
                    "SELECT canonical_key FROM semantic_graph_nodes "
                    "WHERE node_type = 'WorkName' ORDER BY canonical_key"
                ).fetchone()[0]
                assignment = json.loads(connection.execute(
                    "SELECT properties_json FROM semantic_graph_edges "
                    "WHERE relation_type = 'ASSIGNED_TO' "
                    "ORDER BY properties_json"
                ).fetchone()[0])
            reference_time = date.fromisoformat(
                assignment["valid_from"]
            ) + timedelta(days=180)
            cls.question = (
                f"{project_alias}内の「{work_name}」について、"
                f"{reference_time.isoformat()}時点では"
                "誰が担当していましたか。"
            )
            cls.candidate = runtime.evaluate_candidate(
                cls.index,
                cls.question,
                cls.generation.name,
                expected_build_id=cls.build_id,
                expected_registration=cls.registration,
            )
            cls.edge_audit = auditor.audit_candidate(
                cls.index,
                cls.question,
                cls.registration,
                cls.candidate,
            )
        except BaseException:
            cls.fixture.tearDown()
            cls.fixture_type.tearDownClass()
            raise

    @classmethod
    def tearDownClass(cls) -> None:
        cls.fixture.tearDown()
        cls.fixture_type.tearDownClass()

    def setUp(self) -> None:
        self.store = MemoryTrustStore({
            self.generation.name: self.trust_root,
        })

    def evaluate_chain(
        self,
        question: str,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        candidate = runtime.evaluate_candidate(
            self.index,
            question,
            self.generation.name,
            expected_build_id=self.build_id,
            expected_registration=self.registration,
        )
        edge_audit = auditor.audit_candidate(
            self.index,
            question,
            self.registration,
            candidate,
        )
        return candidate, edge_audit

    def validate_trust(
        self,
        registration: dict[str, Any],
        candidate: dict[str, Any],
        edge_audit: dict[str, Any],
    ) -> dict[str, Any] | bool:
        verified = trust.validate_trust_root(
            self.generation,
            registration,
            self.store,
        )
        trust.validate_trust_registration(
            self.trust_locator,
            self.generation,
            registration,
            verified_root=verified,
        )
        candidate_attestation = candidate.get("runtime_attestation", {})
        audit_attestation = edge_audit.get("audit_attestation", {})
        expected = {
            "generation": verified["generation"],
            "graph_snapshot_id": verified["graph_snapshot_id"],
            "logical_snapshot_sha256": verified[
                "logical_snapshot_sha256"
            ],
            "projection_sha256": verified["projection_sha256"],
        }
        if not (
            candidate_attestation.get("build_id") == verified["build_id"]
            and all(
                candidate_attestation.get(key) == value
                and audit_attestation.get(key) == value
                for key, value in expected.items()
            )
        ):
            return False
        return {
            key: verified[key] for key in promotion.TRUST_BINDING_FIELDS
        }

    def run_gate(
        self,
        *,
        question: str | None = None,
        candidate: dict[str, Any] | None = None,
        edge_audit: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        selected_question = self.question if question is None else question
        selected_candidate = (
            self.candidate if candidate is None else candidate
        )
        selected_audit = (
            self.edge_audit if edge_audit is None else edge_audit
        )
        legacy = {
            "answer_status": "insufficient",
            "answer_mode": "insufficient",
            "answer": "わかりません",
            "evidence_ids": [],
            "basis_summary": "従来経路の根拠が足りません。",
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
        config = {
            "active_generation": self.generation.name,
            "cross_document_semantic_graph_storage": copy.deepcopy(
                self.registration
            ),
            "cross_document_semantic_graph_answer_promotion_enabled": True,
            promotion.TRUST_CONFIG_KEY: copy.deepcopy(self.trust_locator),
        }
        return promotion.promote_answer(
            legacy_answer=legacy,
            question=selected_question,
            reference_date=None,
            candidate=copy.deepcopy(selected_candidate),
            audit=copy.deepcopy(selected_audit),
            registration=copy.deepcopy(self.registration),
            feature_enabled=True,
            activation_available=True,
            initial_config=copy.deepcopy(config),
            latest_config=copy.deepcopy(config),
            final_config_loader=lambda: copy.deepcopy(config),
            candidate_validator=(
                Step5AnswerPromotionEndToEndTests.validate_candidate
            ),
            audit_validator=Step5AnswerPromotionEndToEndTests.validate_audit,
            trust_validator=self.validate_trust,
            latest_config_validator=(
                Step5AnswerPromotionEndToEndTests.validate_config
            ),
            answer_validator=(
                Step5AnswerPromotionEndToEndTests.validate_answer
            ),
        )

    def assert_operation_contract(
        self,
        candidate: dict[str, Any],
        operation: str,
    ) -> None:
        self.assertEqual("ACCEPTED", candidate["decision"])
        self.assertEqual(operation, candidate["operation"])
        self.assertEqual(
            runtime.OPERATION_FACT_FIELDS[operation],
            frozenset(item["field"] for item in candidate["asserted_facts"]),
        )
        self.assertEqual(
            runtime.OPERATION_RELATION_TYPES[operation],
            frozenset(
                item["relation"]
                for item in candidate["asserted_relations"]
            ),
        )

    def assert_bounded_edge_cover(
        self,
        candidate: dict[str, Any],
        selected: dict[str, Any],
        record: dict[str, Any],
    ) -> None:
        references = candidate["trace"]["resolved_source_references"]
        all_evidence_ids = {
            reference["evidence_id"] for reference in references
        }
        selected_evidence = set(selected["evidence_ids"])
        self.assertGreater(len(all_evidence_ids), 10)
        self.assertLessEqual(len(selected_evidence), 10)
        self.assertEqual(len(selected_evidence), len(selected["evidence_ids"]))
        self.assertLessEqual(selected_evidence, all_evidence_ids)
        self.assertEqual(references, record["source_references"])
        for edge_id in candidate["trace"]["used_semantic_edge_ids"]:
            covering_evidence = {
                reference["evidence_id"]
                for reference in references
                if reference["edge_id"] == edge_id
            }
            self.assertTrue(
                selected_evidence & covering_evidence,
                msg=f"selected Evidence does not cover {edge_id}",
            )

    def test_owner_promotes_with_full_trace_and_bounded_edge_cover(self) -> None:
        self.assertEqual("ACCEPTED", self.candidate["decision"])
        self.assertEqual("PASS", self.edge_audit["verdict"])
        references = self.candidate["trace"]["resolved_source_references"]
        all_evidence_ids = {
            reference["evidence_id"] for reference in references
        }
        self.assertGreater(len(all_evidence_ids), 10)

        selected, record = self.run_gate()

        self.assertEqual("PROMOTE", record["decision"])
        self.assertEqual(references, record["source_references"])
        self.assertGreater(len(record["source_references"]), 10)
        self.assertEqual(record["evidence_ids"], selected["evidence_ids"])
        self.assertLessEqual(len(selected["evidence_ids"]), 10)
        self.assertEqual(
            len(selected["evidence_ids"]),
            len(set(selected["evidence_ids"])),
        )
        selected_evidence = set(selected["evidence_ids"])
        self.assertLessEqual(selected_evidence, all_evidence_ids)
        for edge_id in self.candidate["trace"]["used_semantic_edge_ids"]:
            covering_evidence = {
                reference["evidence_id"]
                for reference in references
                if reference["edge_id"] == edge_id
            }
            self.assertTrue(
                selected_evidence & covering_evidence,
                msg=f"selected Evidence does not cover {edge_id}",
            )

        repeated_selected, repeated_record = self.run_gate()
        self.assertEqual(
            selected["evidence_ids"],
            repeated_selected["evidence_ids"],
        )
        self.assertEqual(
            record["source_references"],
            repeated_record["source_references"],
        )

    def test_assignment_change_question_contract_promotes(self) -> None:
        question = runtime_fixture.ASSIGNMENT_CHANGE_QUESTION
        candidate, edge_audit = self.evaluate_chain(question)

        self.assert_operation_contract(candidate, "assignment_change")
        self.assertEqual("PASS", edge_audit["verdict"])
        selected, record = self.run_gate(
            question=question,
            candidate=candidate,
            edge_audit=edge_audit,
        )

        self.assertEqual("PROMOTE", record["decision"])
        self.assertEqual("semantic_graph", record["source_answer"])
        self.assertEqual(candidate["answer_text"], selected["answer"])
        self.assertEqual(
            promotion.TRUST_BINDING_FIELDS,
            frozenset(record["trust_binding"]),
        )
        self.assert_bounded_edge_cover(candidate, selected, record)

    def test_version_change_question_contract_promotes_with_bounded_cover(
        self,
    ) -> None:
        question = runtime_fixture.VERSION_CHANGE_QUESTION
        candidate, edge_audit = self.evaluate_chain(question)

        self.assert_operation_contract(candidate, "version_change")
        self.assertEqual("PASS", edge_audit["verdict"])
        selected, record = self.run_gate(
            question=question,
            candidate=candidate,
            edge_audit=edge_audit,
        )

        self.assertEqual("PROMOTE", record["decision"])
        self.assertEqual("semantic_graph", record["source_answer"])
        self.assertEqual(candidate["answer_text"], selected["answer"])
        self.assert_bounded_edge_cover(candidate, selected, record)


if __name__ == "__main__":
    unittest.main()
