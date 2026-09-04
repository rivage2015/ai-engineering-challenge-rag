from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from typing import Any
from unittest import mock


REPOSITORY = Path(__file__).resolve().parents[3]
ENGINE = REPOSITORY / "distribution" / "macos-local-memory" / "engine"
APP = REPOSITORY / "distribution" / "macos-local-memory" / "app"
PROJECTION_TEST = (
    REPOSITORY / "tests"
    / "test_project_cross_document_graph_to_answer_index.py"
)
GENERATION_NAME = "generation-" + "a" * 32
OWNER_QUESTION = (
    "Project Orionの「移行リハーサル統括」は、2022年8月1日時点で"
    "誰が主担当でしたか。"
)
OWNER_2023_QUESTION = (
    "Project Orionの「移行リハーサル統括」は、2023年5月1日時点で"
    "誰が主担当でしたか。"
)
OWNER_WITHOUT_DATE_QUESTION = (
    "Project Orionの「移行リハーサル統括」の主担当は誰ですか。"
)
RELATIVE_OWNER_QUESTION = (
    "Project Orionの「移行リハーサル統括」は、5年前に"
    "誰が主担当でしたか。"
)
ASSIGNMENT_CHANGE_QUESTION = (
    "Project Orionの「移行リハーサル統括」で、主担当が切り替わった日と、"
    "変更前・変更後の担当者を答えてください。"
)
VERSION_CHANGE_QUESTION = (
    "Project Orionの「移行リハーサル統括」について、承認済みの担当変更理由と、"
    "旧案から何が変わったかを答えてください。"
)


def load_module(name: str, path: Path) -> Any:
    specification = importlib.util.spec_from_file_location(name, path)
    assert specification is not None and specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    sys.modules[name] = module
    specification.loader.exec_module(module)
    return module


def load_server_module() -> Any:
    previous_bootstrap = sys.modules.pop("bootstrap", None)
    sys.path.insert(0, str(APP))
    try:
        return load_module(
            "cross_document_semantic_graph_server_test_target",
            APP / "local_memory_server.py",
        )
    finally:
        sys.path.remove(str(APP))
        sys.modules.pop("bootstrap", None)
        if previous_bootstrap is not None:
            sys.modules["bootstrap"] = previous_bootstrap


runtime = load_module(
    "cross_document_semantic_graph_runtime_test_target",
    ENGINE / "cross_document_semantic_graph_runtime.py",
)
projection_fixture = load_module(
    "cross_document_semantic_runtime_projection_fixture",
    PROJECTION_TEST,
)


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def update_metadata(
    connection: sqlite3.Connection,
    key: str,
    value: Any,
) -> None:
    connection.execute(
        "UPDATE metadata SET value = ? WHERE key = ?",
        (
            projection_fixture.answer_validator.canonical_json(value),
            key,
        ),
    )


def add_source_hashes_to_fixture_index(
    index: Path,
    records: list[dict[str, Any]],
) -> None:
    """Make the test base match the full production graph-node projection."""
    source_hashes = {
        record["evidence_id"]: record["source"]["sha256"]
        for record in records
    }
    validator = projection_fixture.answer_validator
    with closing(sqlite3.connect(index)) as connection:
        rows = list(connection.execute(
            "SELECT node_id, node_type, payload_json, status "
            "FROM graph_nodes ORDER BY "
            "CASE node_type WHEN 'document' THEN 0 ELSE 1 END, node_id"
        ))
        for node_id, node_type, payload_json, status in rows:
            if node_type != "evidence":
                continue
            payload = json.loads(payload_json)
            payload["source_record"]["source"]["sha256"] = source_hashes[node_id]
            node = {
                "node_id": node_id,
                "node_type": node_type,
                "payload": payload,
                "status": status,
            }
            connection.execute(
                "UPDATE graph_nodes SET payload_json = ?, record_sha256 = ? "
                "WHERE node_id = ?",
                (
                    validator.canonical_json(payload),
                    validator.record_sha256(node),
                    node_id,
                ),
            )

        nodes = []
        for (
            node_id, node_type, payload_json, status, record_sha256,
        ) in connection.execute(
            "SELECT node_id, node_type, payload_json, status, record_sha256 "
            "FROM graph_nodes ORDER BY "
            "CASE node_type WHEN 'document' THEN 0 ELSE 1 END, node_id"
        ):
            nodes.append({
                "node_id": node_id,
                "node_type": node_type,
                "payload": json.loads(payload_json),
                "status": status,
                "record_sha256": record_sha256,
            })
        edges = []
        for row in connection.execute(
            "SELECT relation_id, from_node_id, relation_type, to_node_id, "
            "relation_class, basis_kind, basis_rule, basis_json, "
            "properties_json, status, record_sha256 "
            "FROM graph_edges ORDER BY relation_id"
        ):
            edges.append({
                "relation_id": row[0],
                "from_node_id": row[1],
                "relation_type": row[2],
                "to_node_id": row[3],
                "relation_class": row[4],
                "basis_kind": row[5],
                "basis_rule": row[6],
                "basis": json.loads(row[7]),
                "properties": json.loads(row[8]),
                "status": row[9],
                "record_sha256": row[10],
            })
        graph = {
            "graph_schema_version": validator.GRAPH_SCHEMA_VERSION,
            "nodes": nodes,
            "edges": edges,
        }
        eligible_rows = [
            {
                "evidence_id": node["node_id"],
                "status": node["status"],
                "record_sha256": node["record_sha256"],
            }
            for node in nodes
            if node["node_type"] == "evidence"
            and node["status"] in validator.GRAPH_RETRIEVABLE_EVIDENCE_STATUSES
        ]
        update_metadata(
            connection,
            "graph_sha256",
            validator.record_sha256(graph),
        )
        update_metadata(
            connection,
            "graph_retrievable_evidence_set_sha256",
            validator.record_sha256(eligible_rows),
        )
        connection.commit()
        validator.validate_answer_graph_contract(connection)


class CrossDocumentSemanticGraphRuntimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        fixture_class = (
            projection_fixture.CrossDocumentGraphAnswerIndexProjectionTests
        )
        cls.fixture = fixture_class(
            methodName="test_success_copies_validated_graph_without_changing_base"
        )
        cls.fixture.setUp()
        try:
            cls.fixture._replace_with_five_document_fixture()
            add_source_hashes_to_fixture_index(
                cls.fixture.base_index,
                cls.fixture.records,
            )
            state = projection_fixture.projector.project(
                **cls.fixture._arguments()
            )
            cls.build_id = state["shadow"]["build_id"]
            cls.final_dir = (
                cls.fixture.generation / runtime.INDEX_DIRECTORY
            )
            os.replace(cls.fixture.output_dir, cls.final_dir)
            cls.index = cls.final_dir / runtime.INDEX_FILENAME
            cls.state = cls.final_dir / runtime.STATE_FILENAME
            cls.generation = cls.fixture.generation
        except BaseException:
            cls.fixture.tearDown()
            raise

    @classmethod
    def tearDownClass(cls) -> None:
        cls.fixture.tearDown()

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def copy_generation(self) -> tuple[Path, Path]:
        copied = self.root / self.generation.name
        shutil.copytree(self.generation, copied)
        index = (
            copied / runtime.INDEX_DIRECTORY / runtime.INDEX_FILENAME
        )
        state = copied / runtime.INDEX_DIRECTORY / runtime.STATE_FILENAME
        return index, state

    @staticmethod
    def bind_state_to_current_index(index: Path, state_path: Path) -> None:
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["output"]["sqlite_sha256"] = file_sha256(index)
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

    @staticmethod
    def registration_for(index: Path) -> dict[str, Any]:
        index = index.resolve(strict=True)
        state_path = index.parent / runtime.STATE_FILENAME
        state = json.loads(state_path.read_text(encoding="utf-8"))
        base_index = index.parent.parent / runtime.INDEX_FILENAME
        return {
            "schema_version": "0.1",
            "status": "validated_storage_only",
            "generation": index.parent.parent.name,
            "database_path": str(index),
            "database_sha256": file_sha256(index),
            "state_path": str(state_path),
            "state_sha256": file_sha256(state_path),
            "base_index_path": str(base_index),
            "base_index_sha256": file_sha256(base_index),
            "graph_snapshot_id": state["shadow"]["graph_snapshot_id"],
            "logical_snapshot_sha256": state["shadow"][
                "logical_snapshot_sha256"
            ],
            "counts": state["counts"],
            "retrieval_enabled": False,
            "used_for_answers": False,
        }

    def evaluate_candidate(
        self,
        index: Path,
        question: str,
        expected_generation: str | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        kwargs.setdefault(
            "expected_registration",
            self.registration_for(index),
        )
        return runtime.evaluate_candidate(
            index,
            question,
            expected_generation,
            **kwargs,
        )

    def test_three_bounded_operations_reuse_graph_answer_logic(self) -> None:
        cases = (
            (OWNER_QUESTION, "owner", "2022-08-01"),
            (
                ASSIGNMENT_CHANGE_QUESTION,
                "assignment_change",
                "2023-04-01",
            ),
            (VERSION_CHANGE_QUESTION, "version_change", "APPROVED"),
        )
        before_index = file_sha256(self.index)
        before_state = self.state.read_bytes()
        before_modified = self.index.stat().st_mtime_ns
        for question, operation, expected_value in cases:
            with self.subTest(operation=operation):
                result = self.evaluate_candidate(
                    self.index,
                    question,
                    self.generation.name,
                    expected_build_id=self.build_id,
                )
                self.assertEqual("accepted", result["status"])
                self.assertEqual("ACCEPTED", result["decision"])
                self.assertEqual(operation, result["operation"])
                self.assertIn(
                    expected_value,
                    {item["value"] for item in result["asserted_facts"]},
                )
                self.assertGreater(
                    result["trace"]["used_semantic_edge_count"], 0
                )
                self.assertEqual(
                    ["verified"], result["trace"]["used_edge_statuses"]
                )
                self.assertTrue(
                    result["trace"]["resolved_source_references"]
                )
                self.assertTrue(all(
                    len(item["source_sha256"]) == 64
                    for item in result["trace"][
                        "resolved_source_references"
                    ]
                ))
                self.assertFalse(result["used_for_answers"])
                self.assertEqual(
                    "not_implemented_step4",
                    result["independent_edge_audit_status"],
                )
                self.assertEqual(
                    "single_sqlite_transaction",
                    result["runtime_attestation"]["read_snapshot"],
                )
                self.assertEqual(
                    13, result["runtime_attestation"]["node_count"]
                )
                self.assertEqual(
                    16, result["runtime_attestation"]["edge_count"]
                )
        self.assertEqual(before_index, file_sha256(self.index))
        self.assertEqual(before_state, self.state.read_bytes())
        self.assertEqual(before_modified, self.index.stat().st_mtime_ns)

    def test_relative_year_reuses_the_answer_run_reference_date(self) -> None:
        accepted = self.evaluate_candidate(
            self.index,
            RELATIVE_OWNER_QUESTION,
            self.generation.name,
            expected_build_id=self.build_id,
            reference_date="2027-08-01",
        )
        self.assertEqual("accepted", accepted["status"])
        self.assertEqual("ACCEPTED", accepted["decision"])
        self.assertIn(
            "2022-08-01",
            {item["value"] for item in accepted["asserted_facts"]},
        )
        self.assertEqual(
            "2027-08-01",
            accepted["trace"]["question_reference_date"],
        )

        missing_anchor = self.evaluate_candidate(
            self.index,
            RELATIVE_OWNER_QUESTION,
            self.generation.name,
            expected_build_id=self.build_id,
        )
        self.assertEqual("held", missing_anchor["status"])
        self.assertEqual(
            "reference_time_required", missing_anchor["reason_code"]
        )
        self.assertIsNone(
            missing_anchor["trace"]["question_reference_date"]
        )

        approximate = self.evaluate_candidate(
            self.index,
            RELATIVE_OWNER_QUESTION.replace("5年前", "約5年前"),
            self.generation.name,
            expected_build_id=self.build_id,
            reference_date="2027-08-01",
        )
        self.assertEqual("held", approximate["status"])
        self.assertEqual(
            "reference_time_ambiguous", approximate["reason_code"]
        )
        self.assertEqual([], approximate["asserted_facts"])

    def test_five_question_gate_is_four_accepted_and_one_hold(self) -> None:
        cases = (
            (OWNER_QUESTION, "ACCEPTED", None),
            (OWNER_2023_QUESTION, "ACCEPTED", None),
            (ASSIGNMENT_CHANGE_QUESTION, "ACCEPTED", None),
            (VERSION_CHANGE_QUESTION, "ACCEPTED", None),
            (
                OWNER_WITHOUT_DATE_QUESTION,
                "HOLD",
                "reference_time_required",
            ),
        )
        results = []
        for question, decision, reason_code in cases:
            with self.subTest(question=question):
                result = self.evaluate_candidate(
                    self.index,
                    question,
                    self.generation.name,
                    expected_build_id=self.build_id,
                )
                self.assertEqual(decision, result["decision"])
                self.assertEqual(reason_code, result["reason_code"])
                self.assertFalse(result["used_for_answers"])
                results.append(result)
        self.assertEqual(
            4,
            sum(result["status"] == "accepted" for result in results),
        )
        self.assertEqual(
            1,
            sum(result["status"] == "held" for result in results),
        )

    def test_non_applicable_question_does_not_open_or_stat_index(self) -> None:
        missing = self.root / "does-not-exist.sqlite3"
        questions = (
            "この資料の概要を短く説明してください。",
            "2026年8月、分身ロボットカフェDAWNでは何回稼働していましたか？",
        )
        for question in questions:
            with self.subTest(question=question):
                with (
                    mock.patch.object(
                        runtime.sqlite3,
                        "connect",
                        side_effect=AssertionError("SQLite must not be opened"),
                    ),
                    mock.patch.object(
                        runtime,
                        "_validate_runtime_paths",
                        side_effect=AssertionError(
                            "index path must not be inspected"
                        ),
                    ),
                ):
                    result = runtime.evaluate_candidate(
                        missing,
                        question,
                        expected_registration={},
                    )
                self.assertEqual("not_applicable", result["status"])
                self.assertEqual("NOT_APPLICABLE", result["decision"])
                self.assertEqual(
                    0, result["trace"]["used_semantic_edge_count"]
                )
                self.assertFalse(result["trace"]["database_opened"])

    def test_registration_anchor_rejects_self_consistent_artifact_swap(self) -> None:
        copied_index, _copied_state = self.copy_generation()
        original_registration = self.registration_for(self.index)
        result = runtime.evaluate_candidate(
            copied_index,
            OWNER_QUESTION,
            expected_registration=original_registration,
        )
        self.assertEqual("held", result["status"])
        self.assertEqual("HOLD", result["decision"])
        self.assertEqual(
            "semantic_runtime_registration_binding_invalid",
            result["diagnostic_code"],
        )
        self.assertEqual([], result["asserted_facts"])

    def test_registration_anchor_rejects_database_hash_mismatch(self) -> None:
        registration = {
            **self.registration_for(self.index),
            "database_sha256": "0" * 64,
        }
        result = runtime.evaluate_candidate(
            self.index,
            OWNER_QUESTION,
            expected_registration=registration,
        )
        self.assertEqual("held", result["status"])
        self.assertEqual(
            "semantic_runtime_registration_binding_invalid",
            result["diagnostic_code"],
        )

    def test_registration_anchor_rejects_state_and_base_changes(self) -> None:
        state_index, state_path = self.copy_generation()
        state_registration = self.registration_for(state_index)
        state_path.write_bytes(state_path.read_bytes() + b"\n")
        state_result = runtime.evaluate_candidate(
            state_index,
            OWNER_QUESTION,
            expected_registration=state_registration,
        )
        self.assertEqual("held", state_result["status"])
        self.assertEqual(
            "semantic_runtime_registration_binding_invalid",
            state_result["diagnostic_code"],
        )

        base_root = self.root / "base-case"
        base_root.mkdir()
        copied = base_root / self.generation.name
        shutil.copytree(self.generation, copied)
        base_index = (
            copied / runtime.INDEX_DIRECTORY / runtime.INDEX_FILENAME
        )
        base_registration = self.registration_for(base_index)
        source_index = copied / runtime.INDEX_FILENAME
        source_index.write_bytes(source_index.read_bytes() + b"changed")
        base_result = runtime.evaluate_candidate(
            base_index,
            OWNER_QUESTION,
            expected_registration=base_registration,
        )
        self.assertEqual("held", base_result["status"])
        self.assertEqual(
            "semantic_runtime_registered_base_index_mismatch",
            base_result["diagnostic_code"],
        )

    def test_registration_anchor_rejects_snapshot_and_shape_mismatch(self) -> None:
        registration = self.registration_for(self.index)
        cases = (
            (
                {**registration, "graph_snapshot_id": "xkgs_" + "0" * 32},
                "semantic_runtime_registration_contract_invalid",
            ),
            (
                {
                    key: value
                    for key, value in registration.items()
                    if key != "state_sha256"
                },
                "semantic_runtime_registration_fields_invalid",
            ),
        )
        for candidate, expected in cases:
            with self.subTest(expected=expected):
                result = runtime.evaluate_candidate(
                    self.index,
                    OWNER_QUESTION,
                    expected_registration=candidate,
                )
                self.assertEqual("held", result["status"])
                self.assertEqual(expected, result["diagnostic_code"])

    def test_runtime_refuses_symlink_hardlink_and_sqlite_sidecar(self) -> None:
        def make_case(label: str) -> Path:
            copied = self.root / label / self.generation.name
            copied.parent.mkdir()
            shutil.copytree(self.generation, copied)
            return (
                copied / runtime.INDEX_DIRECTORY / runtime.INDEX_FILENAME
            )

        symlink_index = make_case("symlink")
        symlink_registration = self.registration_for(symlink_index)
        symlink_index.unlink()
        symlink_index.symlink_to(self.index)

        hardlink_index = make_case("hardlink")
        hardlink_registration = self.registration_for(hardlink_index)
        os.link(hardlink_index, hardlink_index.with_suffix(".linked"))

        sidecar_index = make_case("sidecar")
        sidecar_registration = self.registration_for(sidecar_index)
        sidecar_index.with_name(sidecar_index.name + "-wal").write_bytes(
            b"unexpected-sidecar"
        )

        cases = (
            (
                symlink_index,
                symlink_registration,
                "semantic_runtime_index_path_invalid",
            ),
            (
                hardlink_index,
                hardlink_registration,
                "semantic_runtime_index_not_single_regular_file",
            ),
            (
                sidecar_index,
                sidecar_registration,
                "semantic_runtime_index_sidecar_present",
            ),
        )
        for index, registration, expected in cases:
            with self.subTest(expected=expected):
                result = runtime.evaluate_candidate(
                    index,
                    OWNER_QUESTION,
                    expected_registration=registration,
                )
                self.assertEqual("held", result["status"])
                self.assertEqual(expected, result["diagnostic_code"])

    def test_cli_requires_and_uses_full_registration_anchor(self) -> None:
        completed = subprocess.run(
            [
                sys.executable,
                str(ENGINE / "cross_document_semantic_graph_runtime.py"),
                OWNER_QUESTION,
                "--index",
                str(self.index),
                "--registration-json",
                json.dumps(
                    self.registration_for(self.index),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
        result = json.loads(completed.stdout)
        self.assertEqual("accepted", result["status"])
        self.assertEqual("ACCEPTED", result["decision"])
        self.assertFalse(result["used_for_answers"])

    def test_server_gate_dispatches_real_candidate_cli_operations(self) -> None:
        server = load_server_module()
        workspace = (self.root / "data").resolve()
        generation = workspace / "generations" / self.generation.name
        generation.parent.mkdir(parents=True)
        shutil.copytree(self.generation, generation)
        index = (
            generation / runtime.INDEX_DIRECTORY / runtime.INDEX_FILENAME
        )
        registration = self.registration_for(index)
        config = {
            "workspace": str(workspace),
            "active_generation": generation.name,
            server.bootstrap.CROSS_DOCUMENT_STORAGE_FLAG: True,
            server.bootstrap.CROSS_DOCUMENT_QUERY_CANDIDATE_FLAG: True,
            server.bootstrap.CROSS_DOCUMENT_STORAGE_CONFIG_KEY: registration,
        }
        server.ENGINE = ENGINE
        cases = (
            (OWNER_QUESTION, "accepted", "ACCEPTED", "owner"),
            (
                ASSIGNMENT_CHANGE_QUESTION,
                "accepted",
                "ACCEPTED",
                "assignment_change",
            ),
            (
                VERSION_CHANGE_QUESTION,
                "accepted",
                "ACCEPTED",
                "version_change",
            ),
            (
                OWNER_WITHOUT_DATE_QUESTION,
                "held",
                "HOLD",
                "owner",
            ),
        )
        for question, status, decision, operation in cases:
            with self.subTest(operation=operation, status=status):
                candidate, performance = (
                    server.run_semantic_graph_candidate(
                        question,
                        config,
                        index,
                    )
                )
                self.assertEqual(status, candidate["status"])
                self.assertEqual(decision, candidate["decision"])
                self.assertEqual(operation, candidate["operation"])
                self.assertFalse(candidate["used_for_answers"])
                self.assertEqual(
                    "not_implemented_step4",
                    candidate["independent_edge_audit_status"],
                )
                self.assertTrue(performance["enabled"])
                self.assertFalse(performance["timed_out"])
        relative, performance = server.run_semantic_graph_candidate(
            RELATIVE_OWNER_QUESTION,
            config,
            index,
            reference_date="2027-08-01",
        )
        self.assertEqual("accepted", relative["status"])
        self.assertEqual(
            "2027-08-01", relative["trace"]["question_reference_date"]
        )
        self.assertTrue(performance["enabled"])
        self.assertTrue(
            server._candidate_result_is_safe(
                candidate,
                registration,
                OWNER_WITHOUT_DATE_QUESTION,
            )
        )
        tampered_output = json.loads(json.dumps(candidate))
        tampered_output["runtime_attestation"]["index_sha256"] = "0" * 64
        self.assertFalse(
            server._candidate_result_is_safe(
                tampered_output,
                registration,
                OWNER_WITHOUT_DATE_QUESTION,
            )
        )
        for field in ("question_hash", "run_id"):
            with self.subTest(tampered_trace_field=field):
                tampered_output = json.loads(json.dumps(candidate))
                tampered_output["trace"][field] = "0" * 64
                self.assertFalse(
                    server._candidate_result_is_safe(
                        tampered_output,
                        registration,
                        OWNER_WITHOUT_DATE_QUESTION,
                    )
                )
                del tampered_output["trace"][field]
                self.assertFalse(
                    server._candidate_result_is_safe(
                        tampered_output,
                        registration,
                        OWNER_WITHOUT_DATE_QUESTION,
                    )
                )

    def test_server_non_applicable_candidate_never_needs_graph_files(self) -> None:
        server = load_server_module()
        workspace = (self.root / "missing-data").resolve()
        generation_name = "generation-" + "b" * 32
        generation = workspace / "generations" / generation_name
        index = (
            generation / runtime.INDEX_DIRECTORY / runtime.INDEX_FILENAME
        )
        state_path = index.parent / runtime.STATE_FILENAME
        base_index = generation / runtime.INDEX_FILENAME
        logical_sha256 = "1" * 64
        registration = {
            "schema_version": "0.1",
            "status": "validated_storage_only",
            "generation": generation_name,
            "database_path": str(index),
            "database_sha256": "2" * 64,
            "state_path": str(state_path),
            "state_sha256": "3" * 64,
            "base_index_path": str(base_index),
            "base_index_sha256": "4" * 64,
            "graph_snapshot_id": "xkgs_" + logical_sha256[:32],
            "logical_snapshot_sha256": logical_sha256,
            "counts": {"nodes": 1, "edges": 1, "edge_evidence": 1},
            "retrieval_enabled": False,
            "used_for_answers": False,
        }
        config = {
            "workspace": str(workspace),
            "active_generation": generation_name,
            server.bootstrap.CROSS_DOCUMENT_STORAGE_FLAG: True,
            server.bootstrap.CROSS_DOCUMENT_QUERY_CANDIDATE_FLAG: True,
            server.bootstrap.CROSS_DOCUMENT_STORAGE_CONFIG_KEY: registration,
        }
        server.ENGINE = ENGINE
        candidate, performance = server.run_semantic_graph_candidate(
            "この資料の概要を短く説明してください。",
            config,
            index,
        )
        self.assertEqual("not_applicable", candidate["status"])
        self.assertEqual("NOT_APPLICABLE", candidate["decision"])
        self.assertFalse(candidate["trace"]["database_opened"])
        self.assertFalse(index.exists())
        self.assertEqual("not_applicable", performance["status"])

    def test_applicable_question_uses_one_sqlite_connection(self) -> None:
        original = sqlite3.connect
        calls: list[tuple[Any, ...]] = []

        def tracked(*args: Any, **kwargs: Any) -> sqlite3.Connection:
            calls.append(args)
            return original(*args, **kwargs)

        with mock.patch.object(runtime.sqlite3, "connect", side_effect=tracked):
            result = self.evaluate_candidate(
                self.index,
                OWNER_QUESTION,
                self.generation.name,
            )
        self.assertEqual("accepted", result["status"])
        self.assertEqual(1, len(calls))

    def test_large_artifact_hashing_does_not_buffer_via_byte_reader(self) -> None:
        artifact = self.root / "large-artifact.bin"
        payload = b"streaming-hash-block" * 150_000
        artifact.write_bytes(payload)
        with mock.patch.object(
            runtime,
            "_regular_file_bytes",
            side_effect=AssertionError("hashing must stream"),
        ):
            digest, identity = runtime._sha256_regular_file(
                artifact,
                "streaming_test",
            )
        self.assertEqual(hashlib.sha256(payload).hexdigest(), digest)
        self.assertEqual(len(payload), identity.size)

    def test_source_and_packaged_module_layouts_both_resolve_contracts(self) -> None:
        self.assertEqual(
            "owner",
            runtime.classify_question(OWNER_QUESTION)["operation"],
        )
        packaged_engine = self.root / "Resources" / "engine"
        packaged_scripts = packaged_engine / "layer1" / "scripts"
        packaged_scripts.mkdir(parents=True)
        shutil.copy2(
            ENGINE / "cross_document_semantic_graph_runtime.py",
            packaged_engine,
        )
        shutil.copy2(ENGINE / "answer_local_memory.py", packaged_engine)
        shutil.copy2(
            REPOSITORY / "scripts"
            / "query_cross_document_semantic_graph.py",
            packaged_scripts,
        )
        packaged = load_module(
            "packaged_cross_document_semantic_graph_runtime_test_target",
            packaged_engine / "cross_document_semantic_graph_runtime.py",
        )
        self.assertEqual(
            "owner",
            packaged.classify_question(OWNER_QUESTION)["operation"],
        )
        self.assertEqual(
            "0.1",
            packaged._answer_contract().GRAPH_SCHEMA_VERSION,
        )

    def test_disabled_required_edge_returns_held_without_assertions(self) -> None:
        loaded = runtime.load_runtime_graph(
            self.index,
            expected_generation=self.generation.name,
        )
        edge_id = next(
            edge.edge_id
            for edge in loaded.snapshot.edges.values()
            if edge.relation_type == "HAS_CURRENT_CLAIM"
        )
        result = self.evaluate_candidate(
            self.index,
            VERSION_CHANGE_QUESTION,
            self.generation.name,
            disabled_edge_ids=[edge_id],
        )
        self.assertEqual("held", result["status"])
        self.assertEqual("HOLD", result["decision"])
        self.assertEqual("current_claim_missing", result["reason_code"])
        self.assertEqual([], result["asserted_facts"])
        self.assertEqual([], result["asserted_relations"])
        self.assertIsNotNone(result["runtime_attestation"])

    def test_incomplete_used_edge_status_trace_is_held(self) -> None:
        query = runtime._query_contract()
        real_answer = query.answer_question

        def incomplete_trace(*args: Any, **kwargs: Any) -> dict[str, Any]:
            answer = real_answer(*args, **kwargs)
            answer["trace"]["used_edge_statuses"] = []
            return answer

        with mock.patch.object(
            query, "answer_question", side_effect=incomplete_trace
        ):
            result = self.evaluate_candidate(
                self.index,
                OWNER_QUESTION,
                self.generation.name,
            )
        self.assertEqual("held", result["status"])
        self.assertEqual(
            "semantic_runtime_answer_trace_invalid",
            result["diagnostic_code"],
        )
        self.assertEqual([], result["asserted_facts"])

    def test_asserted_fact_value_must_be_proved_by_its_used_edge(self) -> None:
        query = runtime._query_contract()
        real_answer = query.answer_question

        def tampered_fact(*args: Any, **kwargs: Any) -> dict[str, Any]:
            answer = real_answer(*args, **kwargs)
            answer["asserted_facts"][0]["value"] = "ARBITRARY-NOT-IN-EDGE"
            return answer

        with mock.patch.object(
            query, "answer_question", side_effect=tampered_fact
        ):
            result = self.evaluate_candidate(
                self.index,
                OWNER_QUESTION,
                self.generation.name,
            )
        self.assertEqual("held", result["status"])
        self.assertEqual(
            "semantic_runtime_answer_fact_invalid",
            result["diagnostic_code"],
        )
        self.assertEqual([], result["asserted_facts"])

    def test_unknown_fact_field_cannot_borrow_an_edge_endpoint_value(self) -> None:
        query = runtime._query_contract()
        real_answer = query.answer_question

        def fabricated_field(*args: Any, **kwargs: Any) -> dict[str, Any]:
            answer = real_answer(*args, **kwargs)
            snapshot = args[0]
            proof_edge_id = answer["asserted_facts"][0]["proof_edge_ids"][0]
            edge = snapshot.edges[proof_edge_id]
            answer["asserted_facts"][0] = {
                "field": "totally_fabricated_field",
                "value": snapshot.nodes[edge.to_node_id].canonical_key,
                "proof_edge_ids": [proof_edge_id],
            }
            return answer

        with mock.patch.object(
            query, "answer_question", side_effect=fabricated_field
        ):
            result = self.evaluate_candidate(
                self.index,
                OWNER_QUESTION,
                self.generation.name,
            )
        self.assertEqual("held", result["status"])
        self.assertEqual(
            "semantic_runtime_answer_fact_invalid",
            result["diagnostic_code"],
        )

    def test_disconnected_extra_edge_cannot_enter_used_proof_graph(self) -> None:
        query = runtime._query_contract()
        real_answer = query.answer_question

        def disconnected_edge(*args: Any, **kwargs: Any) -> dict[str, Any]:
            answer = real_answer(*args, **kwargs)
            snapshot = args[0]
            trace = answer["trace"]
            visited = set(trace["visited_node_ids"])
            edge = next(
                item
                for item in snapshot.edges.values()
                if item.from_node_id not in visited
                and item.to_node_id not in visited
            )
            trace["used_semantic_edge_ids"].append(edge.edge_id)
            trace["visited_edge_ids"].append(edge.edge_id)
            trace["visited_edge_hashes"].append(edge.record_sha256)
            trace["used_semantic_edge_count"] += 1
            node_ids = sorted(
                visited | {edge.from_node_id, edge.to_node_id}
            )
            trace["visited_node_ids"] = node_ids
            trace["visited_node_hashes"] = sorted(
                snapshot.nodes[node_id].record_sha256
                for node_id in node_ids
            )
            for evidence_id in edge.supporting_evidence_ids:
                evidence = snapshot.evidence[evidence_id]
                trace["resolved_source_references"].append({
                    "edge_id": edge.edge_id,
                    "evidence_id": evidence_id,
                    "document_id": evidence.document_id,
                    "path": evidence.relative_path,
                    "source_sha256": evidence.source_sha256,
                    "locator": evidence.locator,
                    "observed_text_sha256": evidence.observed_sha256,
                    "quote": evidence.observed_text,
                })
            trace["resolved_source_references"].sort(
                key=lambda item: (item["edge_id"], item["evidence_id"])
            )
            trace["visited_document_paths"] = sorted({
                item["path"]
                for item in trace["resolved_source_references"]
            })
            return answer

        with mock.patch.object(
            query, "answer_question", side_effect=disconnected_edge
        ):
            result = self.evaluate_candidate(
                self.index,
                OWNER_QUESTION,
                self.generation.name,
            )
        self.assertEqual("held", result["status"])
        self.assertEqual(
            "semantic_runtime_answer_graph_disconnected",
            result["diagnostic_code"],
        )

    def test_asserted_relation_tuple_must_match_its_used_edge(self) -> None:
        query = runtime._query_contract()
        real_answer = query.answer_question

        def tampered_relation(*args: Any, **kwargs: Any) -> dict[str, Any]:
            answer = real_answer(*args, **kwargs)
            answer["asserted_relations"][0]["to"] = "ARBITRARY-NODE"
            return answer

        with mock.patch.object(
            query, "answer_question", side_effect=tampered_relation
        ):
            result = self.evaluate_candidate(
                self.index,
                VERSION_CHANGE_QUESTION,
                self.generation.name,
            )
        self.assertEqual("held", result["status"])
        self.assertEqual(
            "semantic_runtime_answer_relation_invalid",
            result["diagnostic_code"],
        )
        self.assertEqual([], result["asserted_relations"])

    def test_all_twenty_nine_required_query_edge_uses_ablate_to_hold(self) -> None:
        questions = (
            OWNER_QUESTION,
            OWNER_2023_QUESTION,
            ASSIGNMENT_CHANGE_QUESTION,
            VERSION_CHANGE_QUESTION,
        )
        cases: list[tuple[str, str]] = []
        for question in questions:
            baseline = self.evaluate_candidate(
                self.index,
                question,
                self.generation.name,
                expected_build_id=self.build_id,
            )
            self.assertEqual("accepted", baseline["status"])
            cases.extend(
                (question, edge_id)
                for edge_id in baseline["trace"]["used_semantic_edge_ids"]
            )
        self.assertEqual(29, len(cases))

        for number, (question, edge_id) in enumerate(cases, start=1):
            with self.subTest(ablation=number, edge_id=edge_id):
                result = self.evaluate_candidate(
                    self.index,
                    question,
                    self.generation.name,
                    expected_build_id=self.build_id,
                    disabled_edge_ids=[edge_id],
                )
                self.assertEqual("held", result["status"])
                self.assertEqual("HOLD", result["decision"])
                self.assertEqual([], result["asserted_facts"])
                self.assertEqual([], result["asserted_relations"])
                self.assertFalse(result["used_for_answers"])

    def test_corrupt_edge_hash_returns_fail_closed_record(self) -> None:
        index, state = self.copy_generation()
        with closing(sqlite3.connect(index)) as connection:
            connection.execute(
                "UPDATE semantic_graph_edges SET record_sha256 = ? "
                "WHERE edge_id = (SELECT edge_id FROM semantic_graph_edges "
                "ORDER BY edge_id LIMIT 1)",
                ("0" * 64,),
            )
            connection.commit()
        self.bind_state_to_current_index(index, state)

        result = self.evaluate_candidate(
            index, OWNER_QUESTION, self.generation.name
        )
        self.assertEqual("held", result["status"])
        self.assertEqual(
            "semantic_graph_runtime_contract_invalid",
            result["reason_code"],
        )
        self.assertTrue(
            result["diagnostic_code"].startswith(
                "semantic_runtime_edge_contract_invalid:"
            )
        )
        self.assertEqual([], result["asserted_facts"])
        self.assertEqual(0, result["trace"]["used_semantic_edge_count"])

    def test_unverified_support_returns_fail_closed_record(self) -> None:
        real_contract = runtime._answer_contract()

        class WithheldEvidenceContract:
            @staticmethod
            def validate_answer_graph_contract(
                connection: sqlite3.Connection,
                metadata: dict[str, Any],
            ) -> dict[str, Any]:
                policy = dict(
                    real_contract.validate_answer_graph_contract(
                        connection, metadata
                    )
                )
                evidence_id = connection.execute(
                    "SELECT evidence_id "
                    "FROM semantic_graph_edge_evidence "
                    "ORDER BY edge_id, evidence_id LIMIT 1"
                ).fetchone()[0]
                policy["eligible_evidence_ids"] = frozenset(
                    set(policy["eligible_evidence_ids"]) - {evidence_id}
                )
                return policy

        with mock.patch.object(
            runtime,
            "_answer_contract",
            return_value=WithheldEvidenceContract(),
        ):
            result = self.evaluate_candidate(
                self.index,
                OWNER_QUESTION,
                self.generation.name,
            )
        self.assertEqual("held", result["status"])
        self.assertTrue(
            result["diagnostic_code"].startswith(
                "semantic_runtime_support_not_verified:"
            )
        )
        self.assertEqual([], result["asserted_facts"])

    def test_competing_assignment_periods_hold_without_legacy_fallback(self) -> None:
        loaded = runtime.load_runtime_graph(
            self.index,
            expected_generation=self.generation.name,
        )
        query = runtime._query_contract()
        old_assignment = min(
            (
                edge
                for edge in loaded.snapshot.edges.values()
                if edge.relation_type == "ASSIGNED_TO"
            ),
            key=lambda edge: edge.properties["valid_from"],
        )
        properties = {
            **old_assignment.properties,
            "valid_to": "2023-12-31",
            "valid_to_inclusive": True,
        }
        payload = {
            "edge_id": old_assignment.edge_id,
            "from_node_id": old_assignment.from_node_id,
            "relation_type": old_assignment.relation_type,
            "to_node_id": old_assignment.to_node_id,
            "relation_class": old_assignment.relation_class,
            "status": old_assignment.status,
            "basis_kind": old_assignment.basis_kind,
            "basis_rule": old_assignment.basis_rule,
            "properties": properties,
            "supporting_evidence_ids": list(
                old_assignment.supporting_evidence_ids
            ),
        }
        competing = query.Edge(
            edge_id=old_assignment.edge_id,
            from_node_id=old_assignment.from_node_id,
            relation_type=old_assignment.relation_type,
            to_node_id=old_assignment.to_node_id,
            relation_class=old_assignment.relation_class,
            status=old_assignment.status,
            basis_kind=old_assignment.basis_kind,
            basis_rule=old_assignment.basis_rule,
            properties=properties,
            supporting_evidence_ids=old_assignment.supporting_evidence_ids,
            record_sha256=query.sha256_value(payload),
        )
        snapshot = query.GraphSnapshot(
            loaded.snapshot.graph_snapshot_id,
            loaded.snapshot.nodes,
            {
                **loaded.snapshot.edges,
                competing.edge_id: competing,
            },
            loaded.snapshot.evidence,
        )
        conflicting = runtime.LoadedRuntimeGraph(
            snapshot=snapshot,
            attestation=loaded.attestation,
            eligible_evidence_ids=loaded.eligible_evidence_ids,
        )
        with mock.patch.object(
            runtime,
            "load_runtime_graph",
            return_value=conflicting,
        ):
            result = self.evaluate_candidate(
                self.index,
                OWNER_2023_QUESTION,
                self.generation.name,
            )
        self.assertEqual("held", result["status"])
        self.assertEqual("HOLD", result["decision"])
        self.assertEqual(
            "assignment_at_time_not_unique",
            result["reason_code"],
        )
        self.assertEqual([], result["asserted_facts"])
        self.assertEqual([], result["asserted_relations"])
        self.assertFalse(result["used_for_answers"])

    def test_step2_storage_only_metadata_is_required(self) -> None:
        index, state = self.copy_generation()
        with closing(sqlite3.connect(index)) as connection:
            update_metadata(
                connection,
                "cross_document_semantic_graph_retrieval_enabled",
                True,
            )
            connection.commit()
        self.bind_state_to_current_index(index, state)

        result = self.evaluate_candidate(
            index, OWNER_QUESTION, self.generation.name
        )
        self.assertEqual("held", result["status"])
        self.assertEqual(
            "semantic_runtime_metadata_status_invalid",
            result["diagnostic_code"],
        )

    def test_index_change_without_projection_state_update_is_held(self) -> None:
        index, _state = self.copy_generation()
        with closing(sqlite3.connect(index)) as connection:
            connection.execute(
                "UPDATE semantic_graph_nodes SET record_sha256 = ? "
                "WHERE node_id = (SELECT node_id FROM semantic_graph_nodes "
                "ORDER BY node_id LIMIT 1)",
                ("0" * 64,),
            )
            connection.commit()
        result = self.evaluate_candidate(
            index, OWNER_QUESTION, self.generation.name
        )
        self.assertEqual("held", result["status"])
        self.assertEqual(
            "semantic_runtime_projection_output_binding_invalid",
            result["diagnostic_code"],
        )

    def test_generation_and_build_id_bindings_fail_closed(self) -> None:
        cases = (
            (
                "generation-" + "b" * 32,
                self.build_id,
                "semantic_runtime_registration_generation_mismatch",
            ),
            (
                self.generation.name,
                "different-build",
                "semantic_runtime_build_id_mismatch",
            ),
        )
        for generation, build_id, expected in cases:
            with self.subTest(expected=expected):
                result = self.evaluate_candidate(
                    self.index,
                    OWNER_QUESTION,
                    generation,
                    expected_build_id=build_id,
                )
                self.assertEqual("held", result["status"])
                self.assertEqual(expected, result["diagnostic_code"])

    def test_programmer_misuse_raises(self) -> None:
        with self.assertRaises(ValueError):
            runtime.evaluate_candidate(
                self.index,
                " ",
                expected_registration=self.registration_for(self.index),
            )
        with self.assertRaises(ValueError):
            runtime.evaluate_candidate(
                self.index,
                OWNER_QUESTION,
                "not-a-generation",
                expected_registration=self.registration_for(self.index),
            )
        with self.assertRaises(ValueError):
            runtime.evaluate_candidate(
                self.index,
                OWNER_QUESTION,
                expected_registration=self.registration_for(self.index),
                disabled_edge_ids=[None],
            )
        with self.assertRaisesRegex(ValueError, "strict ISO"):
            runtime.evaluate_candidate(
                self.index,
                RELATIVE_OWNER_QUESTION,
                expected_registration=self.registration_for(self.index),
                reference_date="2027-8-1",
            )


if __name__ == "__main__":
    unittest.main()
