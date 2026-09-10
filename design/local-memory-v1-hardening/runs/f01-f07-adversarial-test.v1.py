from __future__ import annotations

import contextlib
import hashlib
import json
import sqlite3
import sys
import unittest
from pathlib import Path
from unittest import mock

REPOSITORY = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPOSITORY / "distribution/macos-local-memory/tests"))
from test_versioned_safe_index_e2e import VersionedSafeIndexE2E, ENGINE, synthetic_embed


class IndependentVersionBindingAudit(VersionedSafeIndexE2E):
    def test_audit_state_reread_must_not_publish_unvalidated_version_binding(self):
        self.seed()
        index_module = self.module(ENGINE / "build_local_semantic_index.py")
        restore = {}
        trusted_bindings = []
        spoof = {"path": "/synthetic-unvalidated-version.json", "sha256": "0" * 64, "graph_sha256": "1" * 64}

        def transient_state_change(model, texts, timeout):
            if texts == [index_module.EMBEDDING_SPACE_PROBE_TEXT]:
                documents = Path(sys.argv[sys.argv.index("--documents") + 1])
                state = documents.parent / "adaptive-reader-state.json"
                raw = state.read_bytes()
                value = json.loads(raw)
                trusted_bindings.append(value["document_version_graph"])
                value["document_version_graph"] = spoof
                restore[state] = raw
                state.write_text(json.dumps(value), encoding="utf-8")
            return synthetic_embed(model, texts, timeout)

        def dispatch_restore(command, log=None):
            try:
                return self.run_cli(command, log)
            finally:
                if Path(command[1]).name == "build_local_semantic_index.py":
                    for path, raw in restore.items():
                        path.write_bytes(raw)

        with mock.patch.object(index_module, "embed", side_effect=transient_state_change), mock.patch.object(self.bootstrap, "run", side_effect=dispatch_restore):
            self.bootstrap.build_index()
        self.assertEqual(len(trusted_bindings), 1)
        config = self.bootstrap.load_json(self.bootstrap.CONFIG)
        index = Path(config["index_path"])
        self.assertEqual(self.bootstrap.reader_generation_contract_status(config)["state"], "current")
        with contextlib.closing(sqlite3.connect(index)) as connection:
            actual = json.loads(connection.execute("SELECT value FROM metadata WHERE key='document_version_graph'").fetchone()[0])
            allowed = json.loads(connection.execute("SELECT value FROM metadata WHERE key='answer_generation_allowed'").fetchone()[0])
        self.assertTrue(allowed)
        self.assertEqual(actual, trusted_bindings[0], "Published/current generation metadata must retain the validator-attested binding, not a later unvalidated state reread")

    def test_audit_wrong_same_bytes_path_rejected_by_app_registration(self):
        args, _output, semantic, graph = self.unpublished_reader()
        copied = graph.with_name("copied-version.json")
        copied.write_bytes(graph.read_bytes())
        state_path = semantic / "adaptive-reader-state.json"
        value = self.bootstrap.load_json(state_path)
        value["document_version_graph"]["path"] = str(copied)
        self.bootstrap.atomic_json(state_path, value)
        with self.assertRaisesRegex(ValueError, "reader_contract_version_graph_path_mismatch"):
            self.bootstrap._reader_generation_contract_body(semantic)


if __name__ == "__main__":
    parser_path = REPOSITORY / "scripts/probe_intermediate_records.py"
    print("observed_parser_sha256=" + hashlib.sha256(parser_path.read_bytes()).hexdigest(), flush=True)
    names = [name for name in dir(IndependentVersionBindingAudit) if name.startswith("test_audit_")]
    result = unittest.TextTestRunner(verbosity=2).run(unittest.TestSuite(IndependentVersionBindingAudit(name) for name in names))
    print("final_parser_sha256=" + hashlib.sha256(parser_path.read_bytes()).hexdigest(), flush=True)
    sys.exit(0 if result.wasSuccessful() else 1)
