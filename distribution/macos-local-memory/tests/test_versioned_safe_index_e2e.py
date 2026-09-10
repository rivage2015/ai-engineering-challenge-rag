"""Synthetic application wiring E2E; no model, network, GUI or production state.

CLI argument parsing and production functions are real. Subprocess boundaries
are dispatched in-process so model/HTTP guards apply to every Reader stage.
This is not a real-model quality or packaged macOS acceptance test.
"""
from __future__ import annotations

import contextlib
import hashlib
import http.client
import importlib.util
import io
import json
import re
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import urllib.request
from pathlib import Path
from unittest import mock


PACKAGE = Path(__file__).resolve().parents[1]
ROOT = PACKAGE.parents[1]
ENGINE = PACKAGE / "engine"
APP = PACKAGE / "app"


def load_module(path: Path):
    name = "versioned_e2e_" + path.stem
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def synthetic_embed(_model, texts, _timeout):
    # Same fixed finite space for the build and query probe. Quality is not
    # being measured: lexical matching and source/graph validation remain real.
    return [[1.0, 0.0, 0.0] for _ in texts]


class VersionedSafeIndexE2E(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="versioned-safe-e2e-")
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        self.source = self.base / "source"
        self.source.mkdir()
        self.modules = {}
        self.commands = []
        self.model_requests = []
        self.requested_label = "記載語句"
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(mock.patch.object(sys, "path", [str(ROOT / "scripts"), *sys.path]))
        self.stack.enter_context(mock.patch.object(http.client.HTTPConnection, "connect", side_effect=AssertionError("real HTTP forbidden")))
        self.stack.enter_context(mock.patch.object(urllib.request.OpenerDirector, "open", side_effect=AssertionError("real model HTTP forbidden")))
        self.stack.enter_context(mock.patch.object(subprocess, "Popen", side_effect=AssertionError("real subprocess forbidden")))
        self.bootstrap = load_module(APP / "bootstrap.py")
        self.bootstrap.ENGINE = ENGINE
        self.bootstrap.SUPPORT = self.base / "support"
        self.bootstrap.CONFIG = self.bootstrap.SUPPORT / "config.json"
        self.bootstrap.STATE = self.bootstrap.SUPPORT / "state.json"
        self.bootstrap.DOCUMENT_VERSION_DECISIONS = self.bootstrap.SUPPORT / "document-version-decisions.json"
        self.bootstrap.DOCUMENT_VERSION_REVIEW = self.bootstrap.SUPPORT / "document-version-review.json"
        self.stack.enter_context(mock.patch.object(self.bootstrap, "run", side_effect=self.run_cli))
        self.stack.enter_context(mock.patch.object(self.bootstrap, "local_model_available", return_value=False))
        self.stack.enter_context(mock.patch.object(self.bootstrap, "ensure_models", return_value=[]))
        self.stack.enter_context(mock.patch.object(self.bootstrap, "start_ollama", side_effect=AssertionError("model launch forbidden")))
        self.config = {
            "source_root": str(self.source), "workspace": str(self.base / "workspace"),
            "embedding_model": "synthetic-embedding", "answer_model": "synthetic-answer",
            "audit_model": "synthetic-answer", "sequential_model_loading": False,
            self.bootstrap.CROSS_DOCUMENT_SHADOW_FLAG: False,
            self.bootstrap.CROSS_DOCUMENT_STORAGE_FLAG: False,
            self.bootstrap.CROSS_DOCUMENT_QUERY_CANDIDATE_FLAG: False,
            self.bootstrap.CROSS_DOCUMENT_INDEPENDENT_EDGE_AUDIT_FLAG: False,
            self.bootstrap.CROSS_DOCUMENT_ANSWER_PROMOTION_FLAG: False,
        }
        self.bootstrap.atomic_json(self.bootstrap.CONFIG, self.config)

    def module(self, path):
        path = Path(path)
        if path not in self.modules:
            module = load_module(path)
            if path.name == "build_adaptive_semantic_graph.py":
                module.run_tool = lambda _label, command, _tools, _log: self.run_cli(command)
            elif path.name == "build_intermediate_records.py":
                # Keep real processing-code identities; do not inspect installed
                # OCR/VLM executables, model directories or Ollama metadata.
                module._local_vlm_identities = lambda: {"status": "synthetic_unavailable"}
                module._fixed_ocr_runtime_identity = lambda _kind: {"status": "synthetic_unavailable"}
                module._paddle_runtime_identity = lambda: {"status": "synthetic_unavailable"}
                module._pdfkit_jxa_backend_identity = lambda: {"status": "synthetic_unavailable"}
                module.paddle_build_session = contextlib.nullcontext
                module.discover_password_candidates = lambda _root: ()
            elif path.name == "build_local_semantic_index.py":
                module.embed = synthetic_embed
            elif path.name == "answer_local_memory_v2.py":
                module.base.post_json = self.model_json
            elif path.name == "final_answer_audit.py":
                module.LOCAL_HTTP_OPENER = mock.Mock(open=self.final_audit_response)
            self.modules[path] = module
        return self.modules[path]

    def model_json(self, url, payload, timeout):
        self.model_requests.append(payload)
        if "input" in payload:
            return {"embeddings": [[1.0, 0.0, 0.0]]}
        properties = payload["format"]["properties"]
        if "items" in properties:
            value = {
                "items": [{"item_id": "F1", "label": self.requested_label, "required_claim": "業務内容の" + self.requested_label,
                           "retrieval_query": "業務内容 owner", "required": True}],
                "answer_shape": "one value", "partial_answer_allowed": False,
            }
        else:
            prompt = payload["messages"][-1]["content"]
            packets = re.findall(r"\[EVIDENCE (E\d+)\](.*?)(?=\[EVIDENCE |\Z)", prompt, re.S)
            supporting = [key for key, text in packets if "bob" in text]
            self.assertTrue(supporting, "fixed expected gold value must be in real retrieved Evidence")
            audit = {"item_id": "F1", "verdict": "supported", "supported_value": "bob",
                     "supporting_packet_ids": supporting, "competing_packet_ids": [],
                     "reason_code": "none", "defect": "", "missing_information": []}
            value = {"audits": [audit]} if "audits" in properties else audit
        return {"message": {"content": json.dumps(value, ensure_ascii=False)}}

    def final_audit_response(self, request, timeout):
        self.model_requests.append(json.loads(request.data))
        return io.BytesIO(json.dumps({"message": {"content": json.dumps({
            "verdict": "verified", "reason": "synthetic model response, not a quality evaluation",
            "unsupported_claims": [],
        })}}).encode("utf-8"))

    def run_cli(self, command, log=None):
        self.assertEqual(command[0], sys.executable)
        path = Path(command[1])
        self.assertIn(path.parent, {ENGINE, APP, ROOT / "scripts"})
        self.commands.append(list(command))
        module = self.module(path)
        output = io.StringIO()
        with mock.patch.object(sys, "argv", command[1:]), contextlib.redirect_stdout(output):
            result = module.main()
        self.assertIn(result, (0, None), output.getvalue())
        if log is not None:
            log.write(output.getvalue())
        return output.getvalue()

    def seed(self, *, year_only=False):
        first, second = ("2024", "2025") if year_only else ("_ver1", "_ver2")
        files = {
            f"業務内容{first}.csv": "task,owner\nold,alice\n",
            f"業務内容{second}.csv": "task,owner\ncurrent,bob\n",
            "連絡先.txt": "contact: front desk\n",
        }
        self.assertLessEqual(sum(len(text.encode("utf-8")) for text in files.values()), 1048576)
        for relative, text in files.items():
            (self.source / relative).write_text(text, encoding="utf-8")

    def test_versioned_build_publishes_safe_index(self):
        self.seed()
        originals = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in self.source.iterdir()}
        self.bootstrap.build_index()
        config = self.bootstrap.load_json(self.bootstrap.CONFIG)
        index = Path(config["index_path"])
        with contextlib.closing(sqlite3.connect(index)) as connection:
            paths = {row[0] for row in connection.execute("SELECT relative_path FROM evidence")}
            metadata = {key: json.loads(value) for key, value in connection.execute("SELECT key,value FROM metadata")}
        self.assertEqual(paths, {"業務内容_ver2.csv", "連絡先.txt"})
        self.assertTrue(metadata["answer_generation_allowed"])
        self.assertEqual(self.bootstrap.reader_generation_contract_status(config)["state"], "current")
        self.assertEqual(self.bootstrap.load_json(self.bootstrap.STATE)["phase"], "ready_with_limits")
        version = self.bootstrap.load_json(Path(config["path_graph_path"]) / "document-version-graph.json")
        self.assertEqual(metadata["document_version_graph"]["graph_sha256"], version["graph_sha256"])
        self.assertEqual(originals, {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in self.source.iterdir()})
        self.assertTrue(any(Path(command[1]).name == "build_local_semantic_index.py" for command in self.commands))

    def test_year_only_build_holds_candidates_and_keeps_unrelated_evidence(self):
        self.seed(year_only=True)
        originals = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in self.source.iterdir()}
        self.bootstrap.build_index()
        config = self.bootstrap.load_json(self.bootstrap.CONFIG)
        version = self.bootstrap.load_json(Path(config["path_graph_path"]) / "document-version-graph.json")
        self.assertEqual({"groups": 1, "resolved": 0, "needs_human_review": 1}, version["counts"])
        group = version["groups"][0]
        self.assertEqual("year_order_does_not_establish_supersession", group["reason_code"])
        self.assertIsNone(group["selected_relative_path"])
        self.assertTrue(all(item["disposition"] == "needs_human_review" for item in group["candidates"]))
        self.assertFalse(any(edge["edge_type"] == "active_version" for edge in version["edges"]))
        with contextlib.closing(sqlite3.connect(config["index_path"])) as connection:
            paths = {row[0] for row in connection.execute("SELECT relative_path FROM evidence")}
            metadata = {key: json.loads(value) for key, value in connection.execute("SELECT key,value FROM metadata")}
        self.assertEqual({"連絡先.txt"}, paths)
        self.assertEqual(metadata["document_version_graph"]["graph_sha256"], version["graph_sha256"])
        reader = self.bootstrap.load_json(Path(config["semantic_path"]) / "adaptive-reader-state.json")
        self.assertEqual(2, reader["limitations"]["version_files_needing_human_review"])
        self.assertEqual(0, reader["limitations"]["historical_version_files_held"])
        self.assertEqual(self.bootstrap.reader_generation_contract_status(config)["state"], "current")
        self.assertEqual(originals, {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in self.source.iterdir()})

    def test_human_review_submission_rejects_live_source_changed_after_display(self):
        confined_config = self.bootstrap.load_json(self.bootstrap.CONFIG)
        confined_config["workspace"] = str(self.bootstrap.SUPPORT / "data")
        self.bootstrap.atomic_json(self.bootstrap.CONFIG, confined_config)
        self.seed(year_only=True)
        self.bootstrap.build_index()
        displayed = self.bootstrap.current_document_version_review_context()
        self.assertEqual(len(displayed["dated_group_ids"]), 1)
        validated = self.bootstrap.current_document_version_review_context(
            validate_source=True
        )
        self.assertEqual(
            validated["base_revision"], displayed["base_revision"]
        )
        (self.source / "業務内容2025.csv").write_text(
            "task,owner\nchanged,mallory\n", encoding="utf-8"
        )
        with self.assertRaisesRegex(ValueError, "review_source_changed"):
            self.bootstrap.current_document_version_review_context(
                validate_source=True
            )

    def test_all_year_candidates_held_failure_preserves_previous_public_generation(self):
        self.seed()
        self.bootstrap.build_index()
        config_before = self.bootstrap.CONFIG.read_bytes()
        index = Path(self.bootstrap.load_json(self.bootstrap.CONFIG)["index_path"])
        index_before = hashlib.sha256(index.read_bytes()).hexdigest()
        for previous, current in (("_ver1", "2024"), ("_ver2", "2025")):
            (self.source / f"業務内容{previous}.csv").rename(self.source / f"業務内容{current}.csv")
        saved_contact = self.base / "saved-contact.txt"
        (self.source / "連絡先.txt").rename(saved_contact)
        originals = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in self.source.iterdir()}
        contact_hash = hashlib.sha256(saved_contact.read_bytes()).hexdigest()
        previous_command_count = len(self.commands)
        previous_model_requests = len(self.model_requests)
        with self.assertRaisesRegex(SystemExit, "ValueError:adaptive_reader_no_supported_files"):
            self.bootstrap.build_index()
        self.assertEqual(config_before, self.bootstrap.CONFIG.read_bytes())
        self.assertEqual(index_before, hashlib.sha256(index.read_bytes()).hexdigest())
        self.assertEqual(originals, {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in self.source.iterdir()})
        self.assertEqual(contact_hash, hashlib.sha256(saved_contact.read_bytes()).hexdigest())
        self.assertEqual(previous_model_requests, len(self.model_requests))
        self.assertFalse(any(
            Path(command[1]).name == "build_local_semantic_index.py"
            for command in self.commands[previous_command_count:]
        ))
        # Shared pending review remains an F06 limitation; it is not the
        # published CONFIG generation and is not used as answer acceptance.
        pending = self.bootstrap.load_json(self.bootstrap.DOCUMENT_VERSION_REVIEW)
        self.assertEqual({"groups": 1, "resolved": 0, "needs_human_review": 1}, pending["counts"])
        self.assertTrue(all(item["disposition"] == "needs_human_review" for item in pending["groups"][0]["candidates"]))

    def unpublished_reader(self, *, versioned=True):
        self.seed()
        generation = self.base / ("generation-" + "3" * 32)
        paths, semantic, security = (generation / name for name in ("01-path", "02-semantic", "03-security"))
        inventory = paths / "path-source-inventory.jsonl"
        graph = paths / "document-version-graph.json"
        self.run_cli([sys.executable, str(ENGINE / "build_path_graph.py"), str(self.source), "--output-dir", str(paths)])
        if versioned:
            snapshot = self.bootstrap.capture_decision_snapshot(paths, self.bootstrap.DEFAULT_DECISION_SNAPSHOT_BYTES)
            self.run_cli([sys.executable, str(ENGINE / "document_version_resolver.py"), "build", "--inventory", str(inventory), "--output", str(graph), "--decisions", snapshot["path"]])
        reader_args = ["--source-root", str(self.source), "--inventory", str(inventory), "--output-dir", str(semantic)]
        if versioned:
            reader_args += ["--version-graph", str(graph), *self.bootstrap._snapshot_flags(snapshot)]
        self.run_cli([sys.executable, str(ENGINE / "build_adaptive_semantic_graph.py"), *reader_args])
        self.run_cli([sys.executable, str(ENGINE / "validate_adaptive_semantic_graph.py"), "--initialize-lineage", *reader_args])
        security.mkdir()
        self.run_cli([sys.executable, str(ENGINE / "content_security_gate.py"), "--evidence", str(semantic / "semantic-evidence.jsonl"), "--documents", str(semantic / "semantic-documents.jsonl"), "--output-dir", str(security)])
        output = generation / "safe-answer-index.sqlite3"
        args = [sys.executable, str(ENGINE / "build_local_semantic_index.py"),
                "--evidence", str(security / "safe-answer-evidence.jsonl"),
                "--documents", str(semantic / "semantic-documents.jsonl"),
                "--security-state", str(security / "content-security-state.json"),
                "--source-root", str(self.source), "--source-inventory", str(inventory),
                "--index-purpose", "safe_answer", "--output", str(output)]
        if versioned:
            args += self.bootstrap._snapshot_flags(snapshot)
        return args, output, semantic, graph

    def test_legacy_unversioned_reader_remains_explicitly_supported(self):
        args, output, semantic, _graph = self.unpublished_reader(versioned=False)
        self.run_cli(args)
        with contextlib.closing(sqlite3.connect(output)) as connection:
            binding = json.loads(connection.execute("SELECT value FROM metadata WHERE key='document_version_graph'").fetchone()[0])
            paths = {row[0] for row in connection.execute("SELECT relative_path FROM evidence")}
        self.assertIsNone(binding)
        self.assertEqual(paths, {"業務内容_ver1.csv", "業務内容_ver2.csv", "連絡先.txt"})
        contract = self.bootstrap._reader_generation_contract_body(semantic, legacy_unversioned=True)
        self.assertNotIn("document_version_graph", contract["generation_artifacts"])

    def test_versioned_reader_omission_never_replaces_existing_index(self):
        args, output, _semantic, _graph = self.unpublished_reader()
        output.write_bytes(b"prior-index-sentinel")
        with self.assertRaisesRegex(ValueError, "unexpected_version_decision_authority"):
            self.run_cli(args)
        self.assertEqual(output.read_bytes(), b"prior-index-sentinel")

    def test_wrong_version_graph_is_rejected(self):
        args, output, _semantic, graph = self.unpublished_reader()
        wrong_graph = graph.with_name("other-version-graph.json")
        value = self.bootstrap.load_json(graph)
        value["extra_generation_marker"] = "different"
        core = {key: item for key, item in value.items() if key != "graph_sha256"}
        value["graph_sha256"] = self.bootstrap._canonical_json_sha256(core)
        self.bootstrap.atomic_json(wrong_graph, value)
        with self.assertRaisesRegex(ValueError, "document_version_attestation_failed"):
            self.run_cli([*args, "--version-graph", str(wrong_graph)])
        self.assertFalse(output.exists())

    def test_missing_version_graph_is_rejected(self):
        args, output, _semantic, graph = self.unpublished_reader()
        with self.assertRaises(FileNotFoundError):
            self.run_cli([*args, "--version-graph", str(graph.with_name("missing.json"))])
        self.assertFalse(output.exists())

    def test_registration_detects_changed_version_artifact(self):
        self.seed()
        self.bootstrap.build_index()
        config = self.bootstrap.load_json(self.bootstrap.CONFIG)
        graph = Path(config["path_graph_path"]) / "document-version-graph.json"
        graph.write_text(graph.read_text(encoding="utf-8") + " ", encoding="utf-8")
        status = self.bootstrap.reader_generation_contract_status(config)
        self.assertEqual(status["reason_code"], "reader_contract_version_attestation_failed")

    def test_post_attestation_state_swap_cannot_change_published_binding(self):
        """A transient producer-state swap must not become trusted metadata."""
        self.seed()
        index_module = self.module(ENGINE / "build_local_semantic_index.py")
        originals = {}
        attested_bindings = []
        spoof = {
            "path": "/synthetic-unvalidated-version.json",
            "sha256": "0" * 64, "graph_sha256": "1" * 64,
            "decision_authority": {"mode": "snapshot", "path": "/synthetic-unvalidated-snapshot.json", "sha256": "2" * 64, "byte_count": 37},
        }

        def swap_after_validation(model, texts, timeout):
            if texts == [index_module.EMBEDDING_SPACE_PROBE_TEXT]:
                documents = Path(sys.argv[sys.argv.index("--documents") + 1])
                state_path = documents.parent / "adaptive-reader-state.json"
                raw = state_path.read_bytes()
                value = json.loads(raw)
                attested_bindings.append(value["document_version_graph"])
                originals[state_path] = raw
                value["document_version_graph"] = spoof
                self.bootstrap.atomic_json(state_path, value)
            return synthetic_embed(model, texts, timeout)

        def restore_before_app_publication(command, log=None):
            try:
                return self.run_cli(command, log)
            finally:
                if Path(command[1]).name == "build_local_semantic_index.py":
                    for path, raw in originals.items():
                        path.write_bytes(raw)

        with (
            mock.patch.object(index_module, "embed", side_effect=swap_after_validation),
            mock.patch.object(self.bootstrap, "run", side_effect=restore_before_app_publication),
        ):
            self.bootstrap.build_index()
        self.assertEqual(len(attested_bindings), 1)
        config = self.bootstrap.load_json(self.bootstrap.CONFIG)
        self.assertEqual(self.bootstrap.reader_generation_contract_status(config)["state"], "current")
        with contextlib.closing(sqlite3.connect(config["index_path"])) as connection:
            metadata = {key: json.loads(value) for key, value in connection.execute("SELECT key,value FROM metadata")}
        self.assertTrue(metadata["answer_generation_allowed"])
        self.assertEqual(metadata["document_version_graph"], attested_bindings[0])
        self.assertNotEqual(metadata["document_version_graph"], spoof)

    def test_lineage_context_rejects_unknown_keys_and_null_version(self):
        builder = self.module(ENGINE / "build_local_semantic_index.py")
        context = {"output_dir": self.base, "source_root": self.source, "inventory": self.bootstrap.CONFIG}
        with self.assertRaisesRegex(ValueError, "graph_lineage_validation_context_required"):
            builder._attest_lineage_context([], [], [], {**context, "trust_me": True})
        with self.assertRaisesRegex(ValueError, "graph_lineage_validation_context_invalid"):
            builder._attest_lineage_context([], [], [], {**context, "version_graph": None, "version_authority_mode": "no_decisions"})

    def test_lineage_context_requires_explicit_version_attestation_result(self):
        builder = self.module(ENGINE / "build_local_semantic_index.py")
        context = {"output_dir": self.base, "source_root": self.source, "inventory": self.bootstrap.CONFIG}
        old_report_only = mock.Mock(validate=mock.Mock(return_value={"status": "PASS"}))
        with mock.patch.object(builder, "_load_lineage_validator", return_value=old_report_only):
            with self.assertRaisesRegex(ValueError, "graph_lineage_version_attestation_missing"):
                builder._attest_lineage_context([], [], [], context)

    def test_legacy_reader_rejects_unexpected_explicit_version_graph(self):
        args, output, _semantic, graph = self.unpublished_reader(versioned=False)
        inventory = graph.parent / "path-source-inventory.jsonl"
        self.run_cli([sys.executable, str(ENGINE / "document_version_resolver.py"), "build", "--inventory", str(inventory), "--output", str(graph)])
        with self.assertRaisesRegex(ValueError, "document_version_graph_binding_mismatch"):
            self.run_cli([*args, "--version-graph", str(graph), "--version-authority-mode", "no_decisions"])
        self.assertFalse(output.exists())

    def test_resolver_code_change_invalidates_reader_registration(self):
        self.seed()
        self.bootstrap.build_index()
        config = self.bootstrap.load_json(self.bootstrap.CONFIG)
        actual_identity = self.bootstrap._reader_file_identity
        def different_resolver(path):
            value = actual_identity(path)
            return {**value, "sha256": "0" * 64} if path.name == "document_version_resolver.py" else value
        with mock.patch.object(self.bootstrap, "_reader_file_identity", side_effect=different_resolver):
            self.assertEqual(self.bootstrap.reader_generation_contract_status(config)["reason_code"], "reader_generation_contract_content_mismatch")

    def test_human_choice_rebuild_and_source_change_remain_version_bound(self):
        self.seed()
        current = self.source / "現行"
        current.mkdir()
        (self.source / "業務内容_ver1.csv").rename(current / "業務内容_ver1.csv")
        self.bootstrap.build_index()
        first_config = self.bootstrap.load_json(self.bootstrap.CONFIG)
        first_graph = Path(first_config["path_graph_path"]) / "document-version-graph.json"
        group = self.bootstrap.load_json(first_graph)["groups"][0]
        self.assertEqual(group["status"], "needs_human_review")
        resolver = self.module(ENGINE / "document_version_resolver.py")
        resolver.record_decision(first_graph, self.bootstrap.DOCUMENT_VERSION_DECISIONS,
                                 group["group_id"], "業務内容_ver2.csv", "synthetic-human")
        self.bootstrap.build_index()
        selected_config = self.bootstrap.load_json(self.bootstrap.CONFIG)
        selected_graph = self.bootstrap.load_json(Path(selected_config["path_graph_path"]) / "document-version-graph.json")
        self.assertEqual(selected_graph["groups"][0]["resolution_basis"], "human")
        with contextlib.closing(sqlite3.connect(selected_config["index_path"])) as connection:
            self.assertIn("業務内容_ver2.csv", {row[0] for row in connection.execute("SELECT relative_path FROM evidence")})
        (self.source / "業務内容_ver2.csv").write_text("task,owner\ncurrent,cara\n", encoding="utf-8")
        self.bootstrap.build_index()
        changed_config = self.bootstrap.load_json(self.bootstrap.CONFIG)
        changed_graph = self.bootstrap.load_json(Path(changed_config["path_graph_path"]) / "document-version-graph.json")
        self.assertEqual(changed_graph["groups"][0]["reason_code"], "stale_human_decision")
        with contextlib.closing(sqlite3.connect(changed_config["index_path"])) as connection:
            self.assertEqual({row[0] for row in connection.execute("SELECT relative_path FROM evidence")}, {"連絡先.txt"})
            binding = json.loads(connection.execute("SELECT value FROM metadata WHERE key='document_version_graph'").fetchone()[0])
        self.assertEqual(binding["graph_sha256"], changed_graph["graph_sha256"])
        self.assertNotEqual(first_config["active_generation"], selected_config["active_generation"])
        self.assertNotEqual(selected_config["active_generation"], changed_config["active_generation"])

    def test_failed_rebuild_retains_previous_published_generation(self):
        self.seed()
        self.bootstrap.build_index()
        config_before = self.bootstrap.CONFIG.read_bytes()
        index = Path(self.bootstrap.load_json(self.bootstrap.CONFIG)["index_path"])
        index_before = hashlib.sha256(index.read_bytes()).hexdigest()
        def omit_binding(command, log):
            if Path(command[1]).name == "build_local_semantic_index.py":
                position = command.index("--version-graph")
                command = command[:position] + command[position + 2:]
            return self.run_cli(command, log)
        with mock.patch.object(self.bootstrap, "run", side_effect=omit_binding):
            with self.assertRaisesRegex(ValueError, "unexpected_version_decision_authority"):
                self.bootstrap.build_index()
        self.assertEqual(self.bootstrap.CONFIG.read_bytes(), config_before)
        self.assertEqual(hashlib.sha256(index.read_bytes()).hexdigest(), index_before)

    def application_query(self, query):
        self.seed()
        self.bootstrap.build_index()
        with mock.patch.dict(sys.modules, {"bootstrap": self.bootstrap}), mock.patch.object(sys, "path", [str(APP), *sys.path]):
            server = load_module(APP / "local_memory_server.py")
        server.ENGINE = ENGINE
        def dispatched_process(command, **kwargs):
            return subprocess.CompletedProcess(command, 0, self.run_cli(command), "")
        with mock.patch.object(self.bootstrap, "start_ollama", return_value=None), mock.patch.object(subprocess, "run", side_effect=dispatched_process):
            return server.answer_query(query)

    def test_application_build_query_and_final_audit(self):
        result = self.application_query("業務内容に書かれた語句を教えてください")
        self.assertEqual(result["answer"]["answer_status"], "answered", result)
        self.assertIn("bob", result["answer"]["answer"])
        self.assertEqual(result["orchestration_decision"]["status"], "accepted", result)
        self.assertTrue(self.model_requests)
        self.assertTrue(any(Path(command[1]).name == "final_answer_audit.py" for command in self.commands))
        self.assertNotIn("業務内容_ver1.csv", {item["relative_path"] for item in result["retrieved"]})

    def test_ambiguous_graph_question_is_not_promoted_by_mock_model(self):
        self.requested_label = "owner"
        result = self.application_query("業務内容のownerを教えてください")
        self.assertEqual(result["answer"]["answer_status"], "insufficient")
        self.assertEqual(result["orchestration_decision"]["status"], "rejected")
        self.assertEqual(result["question_evidence_graph"]["reason"], "record_lookup_subject_not_found")


if __name__ == "__main__":
    unittest.main()
