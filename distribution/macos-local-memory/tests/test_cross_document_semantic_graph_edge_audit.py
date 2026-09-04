from __future__ import annotations

import copy
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock


REPOSITORY = Path(__file__).resolve().parents[3]
APP = REPOSITORY / "distribution" / "macos-local-memory" / "app"
RUNTIME_TEST = (
    REPOSITORY
    / "distribution"
    / "macos-local-memory"
    / "tests"
    / "test_cross_document_semantic_graph_runtime.py"
)
AUDITOR_PATH = APP / "cross_document_semantic_graph_edge_audit.py"


def load_module(name: str, path: Path) -> Any:
    specification = importlib.util.spec_from_file_location(name, path)
    assert specification is not None and specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    sys.modules[name] = module
    specification.loader.exec_module(module)
    return module


auditor = load_module("cross_document_edge_audit_test_target", AUDITOR_PATH)
runtime_fixture = load_module(
    "cross_document_edge_audit_runtime_fixture", RUNTIME_TEST
)


class CrossDocumentSemanticGraphEdgeAuditTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.fixture_class = runtime_fixture.CrossDocumentSemanticGraphRuntimeTests
        cls.fixture_class.setUpClass()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.fixture_class.tearDownClass()

    def setUp(self) -> None:
        self.fixture = self.fixture_class()
        self.fixture.setUp()

    def tearDown(self) -> None:
        self.fixture.tearDown()

    def candidate(
        self,
        question: str,
        *,
        reference_date: str | None = None,
    ) -> dict[str, Any]:
        kwargs: dict[str, Any] = {}
        if reference_date is not None:
            kwargs["reference_date"] = reference_date
        return self.fixture.evaluate_candidate(
            self.fixture.index,
            question,
            self.fixture.generation.name,
            expected_build_id=self.fixture.build_id,
            **kwargs,
        )

    def audit(
        self,
        question: str,
        candidate: dict[str, Any],
        *,
        reference_date: str | None = None,
        index: Path | None = None,
        registration: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        target = index if index is not None else self.fixture.index
        anchor = (
            registration
            if registration is not None
            else self.fixture.registration_for(target)
        )
        return auditor.audit_candidate(
            target,
            question,
            anchor,
            candidate,
            reference_date=reference_date,
        )

    def request(
        self,
        question: str,
        *,
        reference_date: str | None = None,
        index: Path | None = None,
        registration: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        target = index if index is not None else self.fixture.index
        anchor = (
            registration
            if registration is not None
            else self.fixture.registration_for(target)
        )
        return {
            "schema_version": "0.1",
            "question": question,
            "index_path": str(target),
            "registration": anchor,
            "question_reference_date": reference_date,
        }

    def write_private_json(
        self,
        name: str,
        value: dict[str, Any],
        *,
        canonical: bool = True,
        mode: int = 0o600,
    ) -> Path:
        path = self.fixture.root / name
        text = (
            auditor.canonical_json(value)
            if canonical
            else json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)
        )
        path.write_text(text, encoding="utf-8")
        os.chmod(path, mode)
        return path

    @staticmethod
    def run_cli(
        request_file: Path,
        candidate_file: Path,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                str(AUDITOR_PATH),
                "--request-file",
                str(request_file),
                "--candidate-file",
                str(candidate_file),
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )

    def assert_rejected_semantics(self, result: dict[str, Any]) -> None:
        self.assertEqual("rejected", result["status"])
        self.assertEqual("REJECT", result["verdict"])
        self.assertEqual("candidate_semantics_mismatch", result["reason_code"])
        self.assertEqual("FAIL", result["checks"]["candidate_semantics"])
        self.assertFalse(result["used_for_answers"])
        self.assertFalse(result["allows_answer_activation"])

    def test_three_happy_operations_are_independently_reconstructed(self) -> None:
        cases = (
            (runtime_fixture.OWNER_QUESTION, "owner"),
            (runtime_fixture.ASSIGNMENT_CHANGE_QUESTION, "assignment_change"),
            (runtime_fixture.VERSION_CHANGE_QUESTION, "version_change"),
        )
        for question, operation in cases:
            with self.subTest(operation=operation):
                candidate = self.candidate(question)
                result = self.audit(question, candidate)
                self.assertEqual("passed", result["status"])
                self.assertEqual("PASS", result["verdict"])
                self.assertEqual(operation, result["operation"])
                self.assertEqual(
                    auditor.sha256_value(
                        auditor.deterministic_candidate_semantics(candidate)
                    ),
                    result["reconstructed_semantics_sha256"],
                )
                self.assertEqual(
                    {
                        "candidate_contract": "PASS",
                        "question_classification": "PASS",
                        "registered_storage_integrity": "PASS",
                        "independent_graph_reconstruction": "PASS",
                        "candidate_semantics": "PASS",
                    },
                    result["checks"],
                )
                self.assertTrue(result["audit_attestation"]["database_opened"])
                self.assertFalse(result["used_for_answers"])
                self.assertFalse(result["allows_answer_activation"])

    def test_relative_date_is_bound_to_the_explicit_run_anchor(self) -> None:
        question = runtime_fixture.RELATIVE_OWNER_QUESTION
        candidate = self.candidate(question, reference_date="2027-08-01")
        result = self.audit(
            question, candidate, reference_date="2027-08-01"
        )
        self.assertEqual("PASS", result["verdict"])
        self.assertEqual("2027-08-01", result["question_reference_date"])

        tampered = copy.deepcopy(candidate)
        tampered["trace"]["question_reference_date"] = "2028-08-01"
        rejected = self.audit(
            question, tampered, reference_date="2027-08-01"
        )
        self.assert_rejected_semantics(rejected)
        self.assertEqual(
            "candidate_reference_time_binding_mismatch",
            rejected["diagnostic_code"],
        )

        anchored_question = question.replace("5年前", "今から遡って5年前")
        anchored_candidate = self.candidate(
            anchored_question, reference_date="2027-08-01"
        )
        anchored_result = self.audit(
            anchored_question,
            anchored_candidate,
            reference_date="2027-08-01",
        )
        self.assertEqual("PASS", anchored_result["verdict"])

    def test_correct_reference_time_hold_passes_independent_audit(self) -> None:
        cases = (
            runtime_fixture.OWNER_WITHOUT_DATE_QUESTION,
            runtime_fixture.RELATIVE_OWNER_QUESTION,
        )
        for question in cases:
            with self.subTest(question=question):
                candidate = self.candidate(question)
                self.assertEqual("HOLD", candidate["decision"])
                result = self.audit(question, candidate)
                self.assertEqual("passed", result["status"])
                self.assertEqual("PASS", result["verdict"])
                self.assertEqual("owner", result["operation"])
                self.assertEqual(
                    "PASS", result["checks"]["candidate_semantics"]
                )

    def test_non_owner_temporal_scope_holds_match_step3_and_fake_accepts_reject(
        self,
    ) -> None:
        for signal in ("前月", "年度末", "Q1"):
            with self.subTest(signal=signal):
                question = (
                    "Project Orionの「移行リハーサル統括」で、"
                    f"{signal}に主担当が切り替わった日と、"
                    "変更前・変更後の担当者を答えてください。"
                )
                candidate = self.candidate(
                    question, reference_date="2027-08-01"
                )
                self.assertEqual("held", candidate["status"])
                self.assertEqual("HOLD", candidate["decision"])
                self.assertEqual(
                    "temporal_context_unsupported", candidate["reason_code"]
                )
                result = self.audit(
                    question,
                    candidate,
                    reference_date="2027-08-01",
                )
                self.assertEqual("PASS", result["verdict"])
                self.assertEqual("assignment_change", result["operation"])

                fake_accepted = self.candidate(
                    runtime_fixture.ASSIGNMENT_CHANGE_QUESTION,
                    reference_date="2027-08-01",
                )
                fake_accepted["trace"]["question_hash"] = candidate["trace"][
                    "question_hash"
                ]
                fake_accepted["trace"]["run_id"] = candidate["trace"][
                    "run_id"
                ]
                rejected = self.audit(
                    question,
                    fake_accepted,
                    reference_date="2027-08-01",
                )
                self.assert_rejected_semantics(rejected)

    def test_true_nonapplicable_does_not_stat_or_open_index(self) -> None:
        question = "この資料の概要を短く説明してください。"
        missing = self.fixture.root / "must-not-be-inspected.sqlite3"
        candidate = runtime_fixture.runtime.evaluate_candidate(
            missing,
            question,
            expected_registration={},
        )
        with (
            mock.patch.object(
                auditor.os,
                "stat",
                side_effect=AssertionError("index must not be stated"),
            ),
            mock.patch.object(
                auditor.sqlite3,
                "connect",
                side_effect=AssertionError("SQLite must not be opened"),
            ),
        ):
            result = auditor.audit_candidate(
                missing, question, {}, candidate
            )
        self.assertEqual("PASS", result["verdict"])
        self.assertEqual("NOT_APPLICABLE", candidate["decision"])
        self.assertEqual(
            "NOT_APPLICABLE",
            result["checks"]["registered_storage_integrity"],
        )
        self.assertFalse(result["audit_attestation"]["database_opened"])
        self.assertIsNone(result["graph_snapshot_id"])

    def test_failure_after_begin_reports_partial_open_transaction_attestation(
        self,
    ) -> None:
        question = runtime_fixture.OWNER_QUESTION
        candidate = self.candidate(question)
        registration = self.fixture.registration_for(self.fixture.index)
        with mock.patch.object(
            auditor,
            "_read_metadata",
            side_effect=auditor.AuditContractError("forced_after_begin"),
        ):
            result = auditor.audit_candidate(
                self.fixture.index,
                question,
                registration,
                candidate,
            )
        self.assertEqual("REJECT", result["verdict"])
        self.assertEqual("forced_after_begin", result["diagnostic_code"])
        self.assertEqual(
            {
                "read_only": True,
                "read_snapshot": "single_sqlite_transaction",
                "database_opened": True,
                "generation": registration["generation"],
                "index_sha256": registration["database_sha256"],
                "graph_snapshot_id": None,
                "logical_snapshot_sha256": None,
                "projection_sha256": None,
                "node_count": None,
                "edge_count": None,
                "edge_evidence_count": None,
                "eligible_evidence_count": None,
                "outbound_network_attempt_count": 0,
            },
            result["audit_attestation"],
        )

    def test_connect_failure_reports_no_database_facts_as_observed(self) -> None:
        question = runtime_fixture.OWNER_QUESTION
        candidate = self.candidate(question)
        registration = self.fixture.registration_for(self.fixture.index)
        with mock.patch.object(
            auditor.sqlite3,
            "connect",
            side_effect=auditor.sqlite3.OperationalError("forced connect failure"),
        ):
            result = auditor.audit_candidate(
                self.fixture.index,
                question,
                registration,
                candidate,
            )
        self.assertEqual("REJECT", result["verdict"])
        self.assertEqual(
            "edge_audit_sqlite_read_failed", result["diagnostic_code"]
        )
        self.assertEqual(
            auditor._empty_audit_attestation(),
            result["audit_attestation"],
        )

    def test_pre_begin_failure_reports_open_connection_without_transaction(
        self,
    ) -> None:
        question = runtime_fixture.OWNER_QUESTION
        candidate = self.candidate(question)
        registration = self.fixture.registration_for(self.fixture.index)
        real_connect = auditor.sqlite3.connect

        class FailingConnection:
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                self._connection = real_connect(*args, **kwargs)
                self.row_factory: Any = None

            def execute(self, _statement: str) -> Any:
                raise auditor.sqlite3.OperationalError("forced before begin")

            def close(self) -> None:
                self._connection.close()

        with mock.patch.object(
            auditor.sqlite3, "connect", side_effect=FailingConnection
        ):
            result = auditor.audit_candidate(
                self.fixture.index,
                question,
                registration,
                candidate,
            )
        self.assertEqual("REJECT", result["verdict"])
        self.assertEqual(
            "edge_audit_sqlite_read_failed", result["diagnostic_code"]
        )
        expected = auditor._empty_audit_attestation()
        expected.update({
            "read_snapshot": "connection_opened_no_transaction",
            "database_opened": True,
            "generation": registration["generation"],
            "index_sha256": registration["database_sha256"],
        })
        self.assertEqual(expected, result["audit_attestation"])

    def test_answer_text_only_tamper_is_rejected(self) -> None:
        question = runtime_fixture.OWNER_QUESTION
        candidate = self.candidate(question)
        candidate["answer_text"] = "別の担当者です。"
        result = self.audit(question, candidate)
        self.assert_rejected_semantics(result)
        self.assertEqual("candidate_answer_text_mismatch", result["diagnostic_code"])

    def test_in_period_reference_time_tamper_is_rejected(self) -> None:
        question = runtime_fixture.OWNER_QUESTION
        candidate = self.candidate(question)
        fact = next(
            item for item in candidate["asserted_facts"]
            if item["field"] == "reference_time"
        )
        fact["value"] = "2022-09-01"
        candidate["answer_text"] = candidate["answer_text"].replace(
            "2022-08-01", "2022-09-01"
        )
        result = self.audit(question, candidate)
        self.assert_rejected_semantics(result)
        self.assertIn(
            result["diagnostic_code"],
            {"candidate_answer_text_mismatch", "candidate_asserted_facts_mismatch"},
        )

    def test_coordinated_alternate_valid_path_facts_and_proofs_are_rejected(
        self,
    ) -> None:
        expected_question = runtime_fixture.OWNER_QUESTION
        alternate = self.candidate(runtime_fixture.OWNER_2023_QUESTION)
        expected = self.candidate(expected_question)
        alternate["trace"]["question_hash"] = expected["trace"]["question_hash"]
        alternate["trace"]["run_id"] = expected["trace"]["run_id"]
        result = self.audit(expected_question, alternate)
        self.assert_rejected_semantics(result)
        self.assertTrue(result["audit_attestation"]["database_opened"])
        self.assertIsNotNone(result["reconstructed_semantics_sha256"])

    def test_version_swap_is_rejected(self) -> None:
        question = runtime_fixture.VERSION_CHANGE_QUESTION
        candidate = self.candidate(question)
        by_field = {item["field"]: item for item in candidate["asserted_facts"]}
        pairs = (
            ("old_plan_status", "current_plan_status"),
            ("old_plan_assignee_id", "current_plan_assignee_id"),
            ("old_plan_assignee_name", "current_plan_assignee_name"),
        )
        for left, right in pairs:
            by_field[left]["value"], by_field[right]["value"] = (
                by_field[right]["value"], by_field[left]["value"]
            )
            by_field[left]["proof_edge_ids"], by_field[right]["proof_edge_ids"] = (
                by_field[right]["proof_edge_ids"],
                by_field[left]["proof_edge_ids"],
            )
        result = self.audit(question, candidate)
        self.assert_rejected_semantics(result)
        self.assertEqual(
            "candidate_asserted_facts_mismatch", result["diagnostic_code"]
        )

    def test_self_consistent_quote_and_hash_tamper_is_rejected(self) -> None:
        question = runtime_fixture.OWNER_QUESTION
        candidate = self.candidate(question)
        reference = candidate["trace"]["resolved_source_references"][0]
        reference["quote"] = "改ざん済みの引用"
        reference["observed_text_sha256"] = auditor.sha256_text(
            reference["quote"]
        )
        result = self.audit(question, candidate)
        self.assert_rejected_semantics(result)
        self.assertEqual(
            "candidate_source_reference_mismatch", result["diagnostic_code"]
        )

    def test_fake_not_applicable_for_supported_question_is_rejected(self) -> None:
        nonapp_question = "この資料の概要を説明してください。"
        fake = runtime_fixture.runtime.evaluate_candidate(
            self.fixture.root / "unused.sqlite3",
            nonapp_question,
            expected_registration={},
        )
        result = self.audit(runtime_fixture.OWNER_QUESTION, fake)
        self.assert_rejected_semantics(result)
        self.assertEqual("candidate_status_mismatch", result["diagnostic_code"])
        self.assertTrue(result["audit_attestation"]["database_opened"])

    def test_missing_required_edge_or_support_evidence_is_rejected(self) -> None:
        question = runtime_fixture.OWNER_QUESTION
        original = self.candidate(question)

        missing_edge = copy.deepcopy(original)
        identity_edge = next(
            edge_id
            for edge_id in missing_edge["trace"]["used_semantic_edge_ids"]
            if edge_id in next(
                item["proof_edge_ids"]
                for item in missing_edge["asserted_facts"]
                if item["field"] == "assignee_name"
            )
            and edge_id not in next(
                item["proof_edge_ids"]
                for item in missing_edge["asserted_facts"]
                if item["field"] == "assignee_id"
            )
        )
        edge_index = missing_edge["trace"]["visited_edge_ids"].index(identity_edge)
        for field in ("visited_edge_ids", "used_semantic_edge_ids"):
            missing_edge["trace"][field].remove(identity_edge)
        missing_edge["trace"]["visited_edge_hashes"].pop(edge_index)
        missing_edge["trace"]["used_semantic_edge_count"] -= 1
        missing_edge["trace"]["resolved_source_references"] = [
            item for item in missing_edge["trace"]["resolved_source_references"]
            if item["edge_id"] != identity_edge
        ]
        name_fact = next(
            item for item in missing_edge["asserted_facts"]
            if item["field"] == "assignee_name"
        )
        name_fact["proof_edge_ids"].remove(identity_edge)
        result = self.audit(question, missing_edge)
        self.assert_rejected_semantics(result)

        missing_evidence = copy.deepcopy(original)
        missing_evidence["trace"]["resolved_source_references"].pop()
        result = self.audit(question, missing_evidence)
        self.assert_rejected_semantics(result)
        self.assertEqual(
            "candidate_source_reference_mismatch", result["diagnostic_code"]
        )

    def test_candidate_pre_audit_marker_is_immutable(self) -> None:
        candidate = self.candidate(runtime_fixture.OWNER_QUESTION)
        candidate["independent_edge_audit_status"] = "passed"
        result = self.audit(runtime_fixture.OWNER_QUESTION, candidate)
        self.assertEqual("REJECT", result["verdict"])
        self.assertEqual("candidate_contract_invalid", result["reason_code"])
        self.assertEqual(
            "edge_audit_candidate_identity_invalid", result["diagnostic_code"]
        )

    def test_cli_reads_only_private_canonical_request_and_candidate_files(
        self,
    ) -> None:
        question = runtime_fixture.OWNER_QUESTION
        candidate = self.candidate(question)
        registration = self.fixture.registration_for(self.fixture.index)
        reference_date = "2027-08-01"
        request = self.request(
            question,
            reference_date=reference_date,
            registration=registration,
        )
        candidate = self.candidate(question, reference_date=reference_date)
        request_file = self.write_private_json("request.json", request)
        candidate_file = self.write_private_json("candidate.json", candidate)
        command = [
            sys.executable,
            str(AUDITOR_PATH),
            "--request-file",
            str(request_file),
            "--candidate-file",
            str(candidate_file),
        ]
        for secret in (
            question,
            str(self.fixture.index),
            auditor.canonical_json(registration),
            reference_date,
        ):
            self.assertNotIn(secret, command)
        completed = self.run_cli(request_file, candidate_file)
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertEqual("", completed.stderr)
        self.assertEqual(1, len(completed.stdout.splitlines()))
        result = json.loads(completed.stdout)
        self.assertEqual("PASS", result["verdict"])
        self.assertEqual(0, result["audit_attestation"][
            "outbound_network_attempt_count"
        ])
        self.assertEqual(
            {
                "schema_version", "record_type", "auditor", "auditor_version",
                "status", "verdict", "reason_code", "diagnostic_code",
                "operation", "candidate_sha256", "registration_sha256",
                "question_sha256", "question_reference_date",
                "graph_snapshot_id", "reconstructed_semantics_sha256", "checks",
                "audit_attestation", "used_for_answers",
                "allows_answer_activation",
            },
            set(result),
        )
        self.assertEqual(
            auditor.sha256_value(candidate), result["candidate_sha256"]
        )
        self.assertEqual(
            auditor.sha256_value(registration), result["registration_sha256"]
        )

    def test_request_file_rejects_noncanonical_symlink_hardlink_and_open_mode(
        self,
    ) -> None:
        question = runtime_fixture.OWNER_QUESTION
        candidate = self.candidate(question)
        request = self.request(question)
        candidate_file = self.write_private_json("candidate.json", candidate)

        noncanonical = self.write_private_json(
            "request-pretty.json", request, canonical=False
        )
        open_mode = self.write_private_json(
            "request-open-mode.json", request, mode=0o644
        )
        symlink_target = self.write_private_json(
            "request-symlink-target.json", request
        )
        symlink = self.fixture.root / "request-symlink.json"
        symlink.symlink_to(symlink_target)
        hardlink_target = self.write_private_json(
            "request-hardlink-target.json", request
        )
        hardlink = self.fixture.root / "request-hardlink.json"
        os.link(hardlink_target, hardlink)
        extra_field = self.write_private_json(
            "request-extra-field.json", {**request, "extra": True}
        )
        missing_field_value = dict(request)
        missing_field_value.pop("question_reference_date")
        missing_field = self.write_private_json(
            "request-missing-field.json", missing_field_value
        )

        for label, request_file in (
            ("noncanonical", noncanonical),
            ("mode", open_mode),
            ("symlink", symlink),
            ("hardlink", hardlink),
            ("extra-field", extra_field),
            ("missing-field", missing_field),
        ):
            with self.subTest(label=label):
                completed = self.run_cli(request_file, candidate_file)
                self.assertNotEqual(0, completed.returncode)

    def test_process_audit_hook_denies_and_counts_socket_and_dns_attempts(
        self,
    ) -> None:
        script = "\n".join((
            "import importlib.util, json, socket, sys",
            f"path = {str(AUDITOR_PATH)!r}",
            "spec = importlib.util.spec_from_file_location('edge_audit_probe', path)",
            "module = importlib.util.module_from_spec(spec)",
            "sys.modules[spec.name] = module",
            "spec.loader.exec_module(module)",
            "boundary = module.install_deny_network_boundary()",
            "denied = 0",
            "for attempt in (lambda: socket.socket(), lambda: socket.getaddrinfo('example.invalid', 443)):",
            "    try:",
            "        attempt()",
            "    except module.OutboundNetworkDenied:",
            "        denied += 1",
            "candidate = module._candidate_result(status='not_applicable', decision='NOT_APPLICABLE', operation=None, reason_code='question_operation_unsupported', answer_text='', asserted_facts=[], asserted_relations=[], trace=module._empty_trace('NOT_APPLICABLE', None), runtime_attestation=None)",
            "result = module.audit_candidate(module.Path('/must-not-open.sqlite3'), 'この資料の概要を教えてください。', {}, candidate, network_boundary=boundary)",
            "print(module.canonical_json({'denied': denied, 'count': boundary.attempt_count, 'result': result}))",
        ))
        completed = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
        observed = json.loads(completed.stdout)
        self.assertEqual(2, observed["denied"])
        self.assertEqual(2, observed["count"])
        self.assertEqual("REJECT", observed["result"]["verdict"])
        self.assertEqual(
            "edge_audit_outbound_network_attempted",
            observed["result"]["diagnostic_code"],
        )
        self.assertEqual(
            2,
            observed["result"]["audit_attestation"][
                "outbound_network_attempt_count"
            ],
        )

    def test_main_wires_a_denied_dns_attempt_into_the_audit_record(self) -> None:
        question = runtime_fixture.OWNER_QUESTION
        request_file = self.write_private_json(
            "request.json", self.request(question)
        )
        candidate_file = self.write_private_json(
            "candidate.json", self.candidate(question)
        )
        script = "\n".join((
            "import importlib.util, socket, sys",
            f"path = {str(AUDITOR_PATH)!r}",
            "spec = importlib.util.spec_from_file_location('edge_audit_main_probe', path)",
            "module = importlib.util.module_from_spec(spec)",
            "sys.modules[spec.name] = module",
            "spec.loader.exec_module(module)",
            "original_loader = module._private_canonical_object_file",
            "attempted = False",
            "def probing_loader(path, label):",
            "    global attempted",
            "    if not attempted:",
            "        attempted = True",
            "        try:",
            "            socket.getaddrinfo('example.invalid', 443)",
            "        except module.OutboundNetworkDenied:",
            "            pass",
            "    return original_loader(path, label)",
            "module._private_canonical_object_file = probing_loader",
            "raise SystemExit(module.main(['--request-file', sys.argv[1], '--candidate-file', sys.argv[2]]))",
        ))
        completed = subprocess.run(
            [
                sys.executable,
                "-c",
                script,
                str(request_file),
                str(candidate_file),
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
        result = json.loads(completed.stdout)
        self.assertEqual("REJECT", result["verdict"])
        self.assertEqual(
            "edge_audit_outbound_network_attempted",
            result["diagnostic_code"],
        )
        self.assertEqual(
            1,
            result["audit_attestation"]["outbound_network_attempt_count"],
        )

    def test_auditor_does_not_import_or_call_the_producer_implementations(
        self,
    ) -> None:
        source = AUDITOR_PATH.read_text(encoding="utf-8")
        self.assertNotIn("cross_document_semantic_graph_runtime.py", source)
        self.assertNotIn("query_cross_document_semantic_graph.py", source)


if __name__ == "__main__":
    unittest.main()
