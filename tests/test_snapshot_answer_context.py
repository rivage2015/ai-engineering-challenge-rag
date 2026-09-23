"""Offline snapshot coverage contracts, including documents with no safe text.

Uses synthetic source files and the actual reader/importer/security/index path.
Only embedding transport and audit transport are mocked; no user data or models.
"""
from __future__ import annotations

import ast
import contextlib
import copy
import hashlib
import html
import importlib.util
import io
import json
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

from test_snapshot_reader_connection import builder, freezing, bridge, lineage, PNG_BYTES
from test_answerability_integration import fixture as answer_fixture, FIELDS

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / "distribution/macos-local-memory/engine"
APP = ROOT / "distribution/macos-local-memory/app"


def load(name, path):
    spec = importlib.util.spec_from_file_location("snapshot_context_test_" + name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


scope = load("scope", ENGINE / "reading_snapshot_context.py")
indexer = load("indexer", ENGINE / "build_local_semantic_index.py")
answer_engine = load("answer", ENGINE / "answer_local_memory_v2.py")
gate = load("gate", ENGINE / "content_security_gate.py")
audit = load("audit", APP / "final_answer_audit.py")


def ui_functions():
    """Run the actual pure renderer functions without importing app CONFIG."""
    tree = ast.parse((APP / "local_memory_server.py").read_text())
    names = {"reading_snapshot_notice", "answerability_notice"}
    nodes = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
    if len(nodes) != len(names):
        raise AssertionError("snapshot UI functions missing")
    namespace = {"html": html, "importlib": importlib, "ENGINE": ENGINE}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(APP / "local_memory_server.py"), "exec"), namespace)
    return namespace


class SnapshotAnswerContextTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        temp = tempfile.TemporaryDirectory(prefix="snapshot-answer-context-")
        cls.addClassCleanup(temp.cleanup)
        cls.directory = Path(temp.name)
        cls.source = cls.directory / "source"
        cls.source.mkdir()
        (cls.source / "schedule.txt").write_text("架空展示の公開日は火曜日です。祝日は休みです。", encoding="utf-8")
        (cls.source / "unread.png").write_bytes(PNG_BYTES)
        (cls.source / "broken.xlsx").write_bytes(b"not an office zip archive")
        payload = {"reading_policy": "text_first_v1", "fixture": "snapshot-answer-context"}
        fingerprint = {"version": "1", "payload": payload,
                       "sha256": hashlib.sha256(builder.canonical_json(payload).encode()).hexdigest()}
        original = cls.directory / "original"
        with mock.patch.object(sys, "argv", ["builder", "--root", str(cls.source), "--out", str(original),
                                             "--reading-policy", "text_first_v1", "--run-at", "2031-05-01T00:00:00+00:00"]), \
             mock.patch.object(builder, "processing_fingerprint", return_value=fingerprint), \
             contextlib.redirect_stdout(io.StringIO()):
            builder.main()
        cls.snapshot = cls.directory / "reading.json"
        cls.envelope = freezing.freeze(original, cls.snapshot)
        cls.semantic = cls.directory / "semantic"
        cls.inventory = cls.directory / "inventory.jsonl"
        entries = [{"kind": "file", "relative_path": path.name, "read_status": "observed",
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "size_bytes": path.stat().st_size}
                   for path in sorted(cls.source.iterdir())]
        cls.inventory.write_text("".join(json.dumps(entry) + "\n" for entry in entries))
        bridge.build(cls.source, cls.inventory, cls.semantic, ROOT / "scripts", reading_snapshot_path=cls.snapshot)
        report = lineage.validate(cls.semantic, cls.source, cls.inventory, initialize_lineage=True)
        if report["status"] != "PASS":
            raise AssertionError(report)
        cls.documents = [json.loads(line) for line in (cls.semantic / "semantic-documents.jsonl").read_text().splitlines()]
        cls.context = scope.build_context(cls.semantic, cls.documents)
        security = cls.directory / "security"
        security.mkdir()
        gate.build(cls.semantic / "semantic-evidence.jsonl", cls.semantic / "semantic-documents.jsonl", security)
        cls.index = cls.directory / "index.sqlite"
        command = ["index", "--evidence", str(security / "safe-answer-evidence.jsonl"),
                   "--documents", str(cls.semantic / "semantic-documents.jsonl"),
                   "--security-state", str(security / "content-security-state.json"),
                   "--source-root", str(cls.source), "--source-inventory", str(cls.inventory),
                   "--output", str(cls.index), "--index-purpose", "safe_answer"]
        with mock.patch.object(sys, "argv", command), \
             mock.patch.object(indexer, "embed", side_effect=lambda model, texts, timeout: [[1.0, 0.0] for _ in texts]), \
             contextlib.redirect_stdout(io.StringIO()):
            indexer.main()
        cls.ui = ui_functions()

    @property
    def packets(self):
        return answer_engine.base.load_answer_evidence_records(self.index)[0]

    @property
    def policy(self):
        return answer_engine.base.load_answer_evidence_records(self.index)[1]

    def test_whole_selection_survives_security_and_index_projection(self):
        indexed = self.policy["metadata"]["reading_snapshot"]
        self.assertEqual(indexed, self.context)
        docs = {doc["source"]["relative_path"]: doc for doc in indexed["documents"]}
        self.assertEqual(set(docs), {"schedule.txt", "unread.png", "broken.xlsx"})
        self.assertEqual(docs["broken.xlsx"]["extraction"]["status"], "failed")
        self.assertTrue(docs["broken.xlsx"]["extraction"]["errors"])
        self.assertTrue(docs["unread.png"]["extraction"]["visual_coverage"]["pending"])
        self.assertEqual({packet["relative_path"] for packet in self.packets}, {"schedule.txt"})
        self.assertEqual(scope.validate_context({"reading_snapshot": indexed}, [
            doc for doc in self.documents if doc["source"]["relative_path"] == "schedule.txt"
        ]), indexed)

    def test_metadata_change_is_rejected_even_after_rehash(self):
        changed = copy.deepcopy(self.context)
        changed["documents"][0]["extraction"]["warnings"].append("added after freeze")
        with self.assertRaisesRegex(ValueError, "context_hash_mismatch"):
            scope.validate_context({"reading_snapshot": changed}, self.documents)
        changed["contract_sha256"] = scope.digest({k: v for k, v in changed.items() if k != "contract_sha256"})
        with self.assertRaisesRegex(ValueError, "context_content_mismatch"):
            scope.validate_context({"reading_snapshot": changed}, self.documents)

    def test_unsearchable_document_cannot_be_deleted_from_coverage(self):
        changed = copy.deepcopy(self.context)
        changed["documents"] = [doc for doc in changed["documents"] if doc["source"]["relative_path"] != "broken.xlsx"]
        changed["contract_sha256"] = scope.digest({k: v for k, v in changed.items() if k != "contract_sha256"})
        with self.assertRaisesRegex(ValueError, "context_content_mismatch"):
            scope.validate_context({"reading_snapshot": changed}, self.documents)

    def test_missing_snapshot_metadata_and_document_bindings_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "coverage_missing"):
            scope.validate_context({}, self.documents)
        changed = copy.deepcopy(self.documents)
        changed[0]["extraction_metadata"].pop("reading_snapshot")
        with self.assertRaisesRegex(ValueError, "binding_missing"):
            scope.validate_context({"reading_snapshot": self.context}, changed)

    def test_source_extraction_and_other_snapshot_binding_are_rejected(self):
        for kind in ("source", "extraction", "snapshot"):
            changed = copy.deepcopy(self.documents)
            with self.subTest(kind=kind):
                if kind == "source":
                    changed[0]["source"]["sha256"] = "a" * 64
                elif kind == "extraction":
                    changed[0]["extraction_metadata"]["source_extraction"]["status"] = "success"
                else:
                    changed[0]["extraction_metadata"]["reading_snapshot"]["snapshot_id"] = "reading_" + "b" * 64
                with self.assertRaises(ValueError):
                    scope.validate_context({"reading_snapshot": self.context}, changed)

    def test_build_requires_all_selected_documents(self):
        with self.assertRaisesRegex(ValueError, "coverage_missing"):
            scope.build_context(self.semantic, self.documents[:1])

    def test_index_metadata_tamper_or_delete_is_rejected_on_real_load(self):
        for value in (None, {**self.context, "complete_source_coverage": True}):
            with self.subTest(deleted=value is None), tempfile.TemporaryDirectory() as directory:
                altered = Path(directory) / "index.sqlite"
                shutil.copyfile(self.index, altered)
                with sqlite3.connect(altered) as connection:
                    if value is None:
                        connection.execute("DELETE FROM metadata WHERE key = 'reading_snapshot'")
                    else:
                        connection.execute("UPDATE metadata SET value = ? WHERE key = 'reading_snapshot'", (json.dumps(value),))
                with self.assertRaisesRegex(ValueError, "snapshot_index_"):
                    answer_engine.base.load_answer_evidence_records(altered)

    def test_model_scope_preserves_unread_and_failed_documents_without_evidence_ids(self):
        model = scope.model_scope(self.context)
        self.assertFalse(model["complete_source_coverage"])
        self.assertEqual(model["source_freshness"], "extraction_time_only")
        docs = {doc["path"]: doc for doc in model["documents"]}
        self.assertGreater(docs["broken.xlsx"]["error_count"], 0)
        self.assertEqual(len(docs["unread.png"]["unread_locations"]), 1)
        self.assertNotIn("evidence_id", json.dumps(model))

    def test_compact_context_adds_scope_not_fake_evidence(self):
        with mock.patch.object(answer_engine, "ACTIVE_READING_SNAPSHOT", self.context):
            text, mapping = answer_engine.compact_context(self.packets)
            empty_text, empty_mapping = answer_engine.compact_context([])
        self.assertIn("READING SCOPE: metadata, not Evidence", text)
        self.assertIn("broken.xlsx", text)
        self.assertIn("unread.png", text)
        self.assertEqual(set(mapping.values()), {packet["evidence_id"] for packet in self.packets})
        self.assertEqual(empty_mapping, {})
        self.assertNotIn("[EVIDENCE", empty_text)
        self.assertIn("READING SCOPE", empty_text)

    def test_scope_limit_fails_closed_instead_of_silently_dropping_coverage(self):
        too_large = copy.deepcopy(self.context)
        too_large["documents"] *= 100
        with self.assertRaisesRegex(ValueError, "scope_exceeds_context_budget"):
            scope.model_scope(too_large)

    def test_ordinary_path_has_no_snapshot_metadata(self):
        self.assertIsNone(scope.validate_context({}, [{"document_id": "ordinary", "extraction_metadata": {}}]))
        self.assertIsNone(scope.model_scope(None))
        self.assertEqual(scope.scope_text(None), "")

    def capture_field_request(self, *, batched, snapshot, reading, workflow_claim, legacy_capacity=False):
        class RequestCaptured(Exception):
            pass
        packets = copy.deepcopy(self.packets)
        if reading:
            for packet in packets:
                packet.update(retrieval_source="workflow_reading_section", workflow_reading_roles=["main"])
        claim = "展示準備の手順を教えてください。" if workflow_claim else "展示の公開日はいつですか？"
        item = {"item_id": "F1", "label": "確認項目", "required_claim": claim}
        field = {"item": item, "retrieved": packets}
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(answer_engine, "ACTIVE_READING_SNAPSHOT", self.context if snapshot else None))
            if legacy_capacity:
                # Emulate only the former capacity expression. All actual
                # prompt/schema construction still runs, unchanged.
                stack.enter_context(mock.patch.object(answer_engine, "field_audit_context_options",
                                                     side_effect=lambda selected: {"num_ctx": 16384} if selected else {}))
            transport = stack.enter_context(mock.patch.object(answer_engine.base, "post_json", side_effect=RequestCaptured))
            with self.assertRaises(RequestCaptured):
                if batched:
                    answer_engine.audit_fields_batched("synthetic-test-model", [field], 37)
                else:
                    context, mapping = answer_engine.compact_context(packets)
                    answer_engine.audit_field("synthetic-test-model", item, context, mapping, 37)
        self.assertEqual(transport.call_count, 1)
        self.assertEqual(transport.call_args.args[0], answer_engine.base.OLLAMA_CHAT_URL)
        self.assertEqual(transport.call_args.args[2], 37)
        return transport.call_args.args[1]

    def check_field_request_capacity(self, *, batched):
        original_schemas = copy.deepcopy((answer_engine.FIELD_AUDIT_SCHEMA, answer_engine.BATCH_AUDIT_SCHEMA))
        for snapshot in (False, True):
            for reading in (False, True):
                for workflow_claim in (False, True):
                    with self.subTest(snapshot=snapshot, reading=reading, workflow_claim=workflow_claim):
                        kwargs = {"batched": batched, "snapshot": snapshot, "reading": reading,
                                  "workflow_claim": workflow_claim}
                        payload = self.capture_field_request(**kwargs)
                        previous = self.capture_field_request(**kwargs, legacy_capacity=True)
                        tokens = 16384 if reading else 8192 if snapshot else None
                        expected = {"temperature": 0,
                                    "num_predict": (3200 if workflow_claim else 900) if batched else (1600 if workflow_claim else 450)}
                        if tokens is not None:
                            expected["num_ctx"] = tokens
                        self.assertEqual(payload["options"], expected)
                        if snapshot and not reading:
                            previous["options"]["num_ctx"] = 8192
                        self.assertEqual(payload, previous, "Only the approved context capacity may differ")
                        self.assertEqual(payload["model"], "synthetic-test-model")
                        self.assertFalse(payload["stream"])
                        self.assertFalse(payload["think"])
                        self.assertIn("手順" if workflow_claim else "公開日", payload["messages"][1]["content"])
        self.assertEqual((answer_engine.FIELD_AUDIT_SCHEMA, answer_engine.BATCH_AUDIT_SCHEMA), original_schemas)

    def test_individual_field_transport_reserves_8192_only_for_normal_snapshot(self):
        self.check_field_request_capacity(batched=False)

    def test_batched_field_transport_keeps_workflow_16384_and_legacy_unspecified(self):
        self.check_field_request_capacity(batched=True)

    def test_legacy_cli_rejects_snapshot_before_scope_unaware_generation(self):
        legacy = answer_engine.base
        with mock.patch.object(sys, "argv", ["legacy", "架空展示の日程", "--index", str(self.index)]), \
             mock.patch.object(legacy, "retrieve", return_value=(self.policy["metadata"], self.packets)), \
             mock.patch.object(legacy, "audit_answerability") as audit_call, \
             mock.patch.object(legacy, "generate_answer") as generation_call:
            with self.assertRaisesRegex(SystemExit, "reading_snapshot_requires_scope_aware_answer_v2"):
                legacy.main()
        audit_call.assert_not_called()
        generation_call.assert_not_called()

    def final_record(self):
        record, packets = answer_fixture()
        record["reading_snapshot"] = copy.deepcopy(self.context)
        record["index"]["reading_snapshot_sha256"] = self.context["contract_sha256"]
        metadata = {field: "hash-" + field for field in FIELDS}
        metadata["reading_snapshot"] = self.context
        policy = {"metadata": metadata, "eligible_evidence_ids": {p["evidence_id"] for p in packets},
                  "graph_sha256": metadata["graph_sha256"], "partition_sha256": metadata["graph_security_partition_sha256"],
                  "eligible_evidence_set_sha256": metadata["graph_retrievable_evidence_set_sha256"], "source_graph": {}}
        return record, packets, policy

    def run_final(self, record, packets, policy, result=None):
        verdict = {"verdict": "verified", "reason": "原文と一致", "unsupported_claims": []}
        llm = mock.Mock(return_value=(verdict, {"wall_seconds": 0.0}), side_effect=result)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "record.json"
            path.write_text(json.dumps(record, ensure_ascii=False))
            with mock.patch.object(sys, "argv", ["audit", "--record", str(path), "--index", "unused.sqlite"]), \
                 mock.patch.object(audit.answer_engine, "load_answer_evidence_records", return_value=(packets, policy)), \
                 mock.patch.object(audit.question_graph, "validate_question_evidence_graph", return_value={"status": "not_applicable", "failures": []}), \
                 mock.patch.object(audit, "audit", llm), contextlib.redirect_stdout(io.StringIO()) as output:
                audit.main()
            return json.loads(output.getvalue()), llm

    def test_final_audit_main_receives_and_retains_exact_scope(self):
        record, packets, policy = self.final_record()
        result, llm = self.run_final(record, packets, policy)
        self.assertEqual(llm.call_count, 1)
        self.assertEqual(llm.call_args.args[5]["reading_snapshot"], self.context)
        self.assertEqual(result["reading_snapshot"], self.context)
        self.assertEqual(result["index"]["reading_snapshot_sha256"], self.context["contract_sha256"])

    def test_reaudit_keeps_same_scope_after_qualified_answer_repair(self):
        record, packets, policy = self.final_record()
        responses = [
            ({"verdict": "qualified", "reason": "暫定引用の扱いを再確認", "unsupported_claims": ["870年頃？（平安時代）"]},
             {"wall_seconds": 0.0}),
            ({"verdict": "verified", "reason": "残る回答は支持される", "unsupported_claims": []},
             {"wall_seconds": 0.0}),
        ]
        result, llm = self.run_final(record, packets, policy, responses)
        self.assertEqual(llm.call_count, 2)
        for call in llm.call_args_list:
            self.assertEqual(call.args[5]["reading_snapshot"], self.context)
        self.assertEqual(result["reading_snapshot"], self.context)
        self.assertEqual(result["answerability_reaudit"]["excluded_field_ids"], ["F2"])

    def test_final_audit_record_and_index_mismatch_stop_before_model(self):
        for kind in ("missing_scope", "other_scope", "missing_binding", "wrong_binding"):
            record, packets, policy = self.final_record()
            if kind == "missing_scope":
                record.pop("reading_snapshot")
            elif kind == "other_scope":
                record["reading_snapshot"]["snapshot_id"] = "reading_other"
            elif kind == "missing_binding":
                record["index"].pop("reading_snapshot_sha256")
            else:
                record["index"]["reading_snapshot_sha256"] = "bad"
            with self.subTest(kind=kind), mock.patch.object(audit.LOCAL_HTTP_OPENER, "open") as network:
                with self.assertRaisesRegex(ValueError, "answer_snapshot_"):
                    self.run_final(record, packets, policy)
                network.assert_not_called()

    def test_final_audit_transport_prompt_includes_scope_but_not_as_evidence(self):
        record, packets, _ = self.final_record()
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps({
            "message": {"content": json.dumps({"verdict": "verified", "reason": "引用一致", "unsupported_claims": []})},
            "done": True, "done_reason": "stop", "prompt_eval_count": 1500, "eval_count": 100,
        }).encode()
        with mock.patch.object(audit.LOCAL_HTTP_OPENER, "open", return_value=response) as transport:
            result, _ = audit.audit("test-model", "表示色は何ですか？", record["answer"], packets, 10,
                                    {"reading_snapshot": self.context})
        self.assertEqual(result["verdict"], "verified")
        prompt = json.loads(transport.call_args.args[0].data)["messages"][1]["content"]
        self.assertIn('"reading_scope"', prompt)
        self.assertIn("broken.xlsx", prompt)
        self.assertIn("追加読取待ち", prompt)
        evidence_part = prompt.split("Evidence:\n", 1)[1].split("機械検証済み情報", 1)[0].strip()
        self.assertEqual(json.loads(evidence_part), packets)

    def test_workflow_group_prompt_also_retains_scope_after_prompt_replacement(self):
        from test_workflow_source_metadata import grouped_fixture
        record, packets = grouped_fixture()
        _, graph, validation = audit.claim_validator.build_and_validate(record, packets)
        self.assertEqual(validation["status"], "pass")
        groups = audit.workflow_group_audit_context(record, graph, packets)
        verdict = {"verdict": "verified", "reason": "原文と一致", "unsupported_claims": [],
                   "group_checks": [{"group_id": group["group_id"], "checks": ["pass", "pass", "pass"]}
                                    for group in groups["groups"]],
                   "coverage": "pass", "missing_evidence_ids": [], "diagnostics": [], "diagnostics_omitted": "0"}
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps({
            "message": {"content": json.dumps(verdict)}, "done": True, "done_reason": "stop",
            "prompt_eval_count": 1800, "eval_count": 500,
        }).encode()
        with mock.patch.object(audit.LOCAL_HTTP_OPENER, "open", return_value=response) as transport:
            result, _ = audit.audit("test-model", record["query"], record["answer"], packets, 10,
                                    {"reading_snapshot": self.context, "workflow_groups": groups})
        self.assertEqual(result["verdict"], "verified")
        prompt = json.loads(transport.call_args.args[0].data)["messages"][1]["content"]
        self.assertIn("UNTRUSTED_WORKFLOW_DATA", prompt)
        self.assertIn("READING SCOPE: metadata, not Evidence", prompt)
        self.assertIn("unread.png", prompt)
        self.assertIn("broken.xlsx", prompt)
        for packet in packets:
            self.assertIn(packet["text"], prompt)

    def test_workflow_input_reconstruction_reuses_index_snapshot_scope(self):
        import test_workflow_final_audit as workflow_fixture
        metadata = workflow_fixture.metadata()
        metadata["reading_snapshot"] = self.context
        # The fixture exercises the actual answer orchestration and hashes its
        # three generated prompts. Only model replies are synthetic. No final
        # auditor is mocked to PASS: this checks deterministic prompt binding.
        with mock.patch.object(workflow_fixture, "metadata", return_value=metadata):
            record, rows, policy, _ = workflow_fixture.fixture()
        self.assertEqual(record["reading_snapshot"], self.context)
        self.assertEqual(record["workflow_reasoning"]["status"], "checked")
        self.assertEqual(len(record["workflow_reasoning"]["model_calls"]), 3)
        with mock.patch.object(audit.LOCAL_HTTP_OPENER, "open") as network:
            verified = audit.validate_workflow_retrieval_binding(record, rows, policy)
            self.assertEqual(verified, {"status": "pass", "failures": []})
            missing = copy.deepcopy(policy)
            missing["metadata"].pop("reading_snapshot")
            rejected = audit.validate_workflow_retrieval_binding(record, rows, missing)
            self.assertEqual(rejected["status"], "blocked")
            self.assertTrue(rejected["failures"])
            network.assert_not_called()

    def test_ui_limits_visible_for_qualified_rejected_and_incomplete_audit(self):
        for verdict in ("verified", "qualified", "rejected", None):
            for applied in (False, True):
                record = {"reading_snapshot": self.context,
                          "independent_final_audit": {"verdict": verdict},
                          "answerability_policy": {"applied": applied, "confirmed_field_ids": ["F1"]}}
                with self.subTest(verdict=verdict, applied=applied):
                    rendered = self.ui["answerability_notice"](record)
                    self.assertIn('aria-label="保存JSONの読取範囲"', rendered)
                    self.assertIn("3資料", rendered)
                    self.assertIn("追加読取待ち1箇所", rendered)
                    self.assertIn("読取失敗・保留1資料", rendered)
        self.assertIn("網羅性は未確認", rendered)


class SnapshotContextComparisonTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.harness = load("comparison_harness", ROOT / "scripts/test_local_reading_snapshot.py")

    def baseline(self, **changes):
        return {"number": 1, "status": "completed_backend_only", "answer_status": "insufficient",
                "question_sha256": "question-one", **changes}

    def test_default_rejects_repeat_but_new_question_is_unchanged(self):
        attempts = [self.baseline()]
        self.assertEqual(self.harness.snapshot_context_comparison(attempts, "question-two", False), {})
        with self.assertRaisesRegex(ValueError, "no_automatic_retry"):
            self.harness.snapshot_context_comparison(attempts, "question-one", False)

    def test_explicit_comparison_is_bound_to_completed_insufficient_baseline(self):
        attempts = [self.baseline()]
        before = copy.deepcopy(attempts)
        self.assertEqual(self.harness.snapshot_context_comparison(attempts, "question-one", True), {
            "change_id": "CR-SNAPSHOT-CONTEXT-8192", "baseline_attempt": 1,
            "requested_field_context_tokens": 8192,
        })
        self.assertEqual(attempts, before, "Baseline history must not be rewritten")

    def test_comparison_cannot_target_new_unfinished_successful_or_duplicate_attempts(self):
        candidates = [[], [self.baseline(question_sha256="different")],
                      [self.baseline(status="running")], [self.baseline(status="failed")],
                      [self.baseline(answer_status="answered")], [self.baseline(answer_status="qualified")],
                      [self.baseline(), self.baseline(number=2)]]
        for attempts in candidates:
            with self.subTest(attempts=attempts), self.assertRaisesRegex(ValueError, "requires_one_completed_insufficient"):
                self.harness.snapshot_context_comparison(attempts, "question-one", True)

    def test_running_failed_or_finished_comparison_consumes_the_only_retry(self):
        comparison = self.harness.snapshot_context_comparison([self.baseline()], "question-one", True)
        for status in ("running", "failed", "completed_backend_only"):
            for repeated_question in ("question-one", "different"):
                attempts = [self.baseline(), self.baseline(number=2, status=status,
                            question_sha256=repeated_question, context_comparison=comparison)]
                before = copy.deepcopy(attempts)
                with self.subTest(status=status, repeated_question=repeated_question), \
                     self.assertRaisesRegex(ValueError, "comparison_already_attempted"):
                    self.harness.snapshot_context_comparison(attempts, "question-one", True)
                self.assertEqual(attempts, before)

    def test_damaged_comparison_marker_cannot_restore_retry_permission(self):
        for marker in ({}, None, False):
            attempts = [self.baseline(), self.baseline(number=2, status="failed",
                        question_sha256="different", context_comparison=marker)]
            with self.subTest(marker=marker), self.assertRaisesRegex(ValueError, "comparison_already_attempted"):
                self.harness.snapshot_context_comparison(attempts, "question-one", True)

    def test_comparison_flag_without_query_is_rejected_before_loading_app(self):
        with mock.patch.object(sys, "argv", ["harness", "--snapshot", "unused.json", "--source-root", "unused",
                                             "--output", "unused", "--compare-context-8192"]), \
             mock.patch.object(self.harness, "load_app") as loader, contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as error:
                self.harness.main()
        self.assertEqual(error.exception.code, 2)
        loader.assert_not_called()


if __name__ == "__main__":
    unittest.main()
