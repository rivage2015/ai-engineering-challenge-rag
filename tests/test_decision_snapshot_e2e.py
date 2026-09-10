"""Frozen F05b semantic RED oracles; synthetic app dispatch, no real inference.

Current APIs are used for RED. An accepted-entry sentinel distinguishes an
unsafe gate acceptance from unrelated errors later in the pipeline.
"""
from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import types
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "distribution/macos-local-memory"
ENGINE = PACKAGE / "engine"
spec = importlib.util.spec_from_file_location("f05b_app_fixture", PACKAGE / "tests/test_versioned_safe_index_e2e.py")
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)
builder = types.SimpleNamespace()  # reviewed guard's test dispatch seam
EMPTY = b'{"schema_version":"1.0","decisions":[]}\n'


class AcceptedSourceEntry(BaseException):
    pass


class ObservedGate(BaseException):
    def __init__(self, accepted, reason=""):
        self.accepted, self.reason = accepted, reason


class DecisionSnapshotApplicationTests(unittest.TestCase):
    def setUp(self):
        self.h = fixture.VersionedSafeIndexE2E()
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)

    def seed(self):
        for path, text in {
            "Guide_ver1.csv": "task,owner\nold,alice\n",
            "Guide_ver2.csv": "task,owner\ncurrent,bob\n",
            "Contact.txt": "contact: front desk\n",
        }.items():
            (self.h.source / path).write_text(text, encoding="utf-8")

    def cli_value(self, command, flag):
        return Path(command[command.index(flag) + 1])

    def run_raw(self, command):
        module = self.h.module(Path(command[1]))
        with mock.patch.object(sys, "argv", command[1:]), contextlib.redirect_stdout(io.StringIO()):
            return module.main()

    def published(self):
        config = self.h.bootstrap.load_json(self.h.bootstrap.CONFIG)
        return config, self.h.bootstrap.CONFIG.read_bytes(), Path(config["index_path"]).read_bytes()

    def assert_preserved(self, before):
        config, config_bytes, index_bytes = before
        self.assertEqual(config_bytes, self.h.bootstrap.CONFIG.read_bytes())
        self.assertEqual(index_bytes, Path(config["index_path"]).read_bytes())

    def test_new_app_generation_materializes_empty_decision_snapshot(self):
        self.seed()
        self.h.bootstrap.build_index()
        config = self.h.bootstrap.load_json(self.h.bootstrap.CONFIG)
        snapshot = Path(config["path_graph_path"]) / "document-version-decisions.snapshot.json"
        self.assertTrue(snapshot.is_file(), "new app generation must capture a fixed decision snapshot")
        self.assertEqual(EMPTY, snapshot.read_bytes())
        self.assertFalse(self.h.bootstrap.DOCUMENT_VERSION_DECISIONS.exists())

    def test_reader_rejects_omitted_authority_before_source_entry(self):
        self.seed()
        self.h.bootstrap.build_index()
        config = self.h.bootstrap.load_json(self.h.bootstrap.CONFIG)
        paths = Path(config["path_graph_path"])
        reader = self.h.module(ENGINE / "build_adaptive_semantic_graph.py")
        accepted = False
        reason = ""
        with mock.patch.object(reader, "validate_inventory_binding", side_effect=AcceptedSourceEntry):
            try:
                reader.build(self.h.source, paths / "path-source-inventory.jsonl", self.h.base / "omitted-reader", ROOT / "scripts", paths / "document-version-graph.json")
            except AcceptedSourceEntry:
                accepted = True
            except ValueError as exc:
                reason = str(exc)
        self.assertFalse(accepted, "versioned Reader accepted omitted authority and reached source access")
        self.assertIn("version_decision_authority_required", reason)

    def test_validator_rejects_omitted_authority(self):
        self.seed()
        self.h.bootstrap.build_index()
        config = self.h.bootstrap.load_json(self.h.bootstrap.CONFIG)
        paths = Path(config["path_graph_path"])
        validator = self.h.module(ENGINE / "validate_adaptive_semantic_graph.py")
        with self.assertRaisesRegex(ValueError, "version_decision_authority_required"):
            validator.validate(Path(config["semantic_path"]), self.h.source, paths / "path-source-inventory.jsonl", paths / "document-version-graph.json")

    def test_reader_rejects_resealed_active_after_initial_gate(self):
        self.seed()
        self.h.bootstrap.build_index()
        before = self.published()
        (self.h.source / "Guide_ver1.csv").rename(self.h.source / "Guide.csv")
        original_sources = {p.name: p.read_bytes() for p in self.h.source.iterdir()}
        initial_gate = []
        reader = self.h.module(ENGINE / "build_adaptive_semantic_graph.py")
        resolver = self.h.module(ENGINE / "document_version_resolver.py")

        def intercept(command, log=None):
            name = Path(command[1]).name
            if name == "document_version_resolver.py" and command[2] == "validate":
                result = self.h.run_cli(command, log)
                self.assertEqual("PASS", json.loads(result)["status"])
                initial_gate.append(True)
                return result
            if name != "build_adaptive_semantic_graph.py":
                return self.h.run_cli(command, log)
            self.assertEqual([True], initial_gate)
            graph_path = self.cli_value(command, "--version-graph")
            graph = json.loads(graph_path.read_bytes())
            group = graph["groups"][0]
            self.assertEqual("unmarked_candidate_requires_human_review", group["reason_code"])
            group.update(status="resolved", selected_relative_path="Guide.csv", resolution_basis="automatic", reason_code="fabricated_policy", conflicts=[])
            for item in group["candidates"]:
                item["disposition"] = "active" if item["relative_path"] == "Guide.csv" else "historical"
            graph["nodes"], graph["edges"] = resolver.graph_projection(graph["groups"])
            graph["counts"] = {"groups": 1, "resolved": 1, "needs_human_review": 0}
            core = {k: v for k, v in graph.items() if k != "graph_sha256"}
            graph["graph_sha256"] = hashlib.sha256(json.dumps(core, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            payload = json.dumps(graph, ensure_ascii=False)
            self.assertLessEqual(len(payload.encode()), 65536)
            graph_path.write_text(payload, encoding="utf-8")
            with mock.patch.object(reader, "validate_inventory_binding", side_effect=AcceptedSourceEntry):
                try:
                    self.run_raw(command)
                except AcceptedSourceEntry:
                    raise ObservedGate(True)
                except SystemExit as exc:
                    self.assertIn("ValueError:", str(exc))
                    raise ObservedGate(False, str(exc))
            self.fail("Reader unexpectedly returned without source entry or rejection")

        with mock.patch.object(self.h.bootstrap, "run", side_effect=intercept):
            with self.assertRaises(ObservedGate) as observed:
                self.h.bootstrap.build_index()
        self.assert_preserved(before)
        self.assertEqual(original_sources, {p.name: p.read_bytes() for p in self.h.source.iterdir()})
        self.assertFalse(observed.exception.accepted, "resealed false selection passed Reader after real initial validation")
        self.assertTrue(observed.exception.reason)

    def test_validator_rejects_coherent_eligible_contact_omission(self):
        self.seed()
        self.h.bootstrap.build_index()
        before = self.published()
        reader = self.h.module(ENGINE / "build_adaptive_semantic_graph.py")
        original_selector = reader.select_inventory
        shortened = []

        def omit(records):
            selected, counts = original_selector(records)
            self.assertIn("Contact.txt", [item["relative_path"] for item in selected])
            counts["selected"] -= 1
            return [item for item in selected if item["relative_path"] != "Contact.txt"], counts

        def intercept(command, log=None):
            name = Path(command[1]).name
            if name == "build_adaptive_semantic_graph.py":
                with mock.patch.object(reader, "select_inventory", side_effect=omit):
                    result = self.h.run_cli(command, log)
                manifest = json.loads((self.cli_value(command, "--output-dir") / "layer1-input-manifest.json").read_bytes())
                self.assertEqual(["Guide_ver2.csv"], manifest["paths"])
                shortened.append(True)
                return result
            if name != "validate_adaptive_semantic_graph.py":
                return self.h.run_cli(command, log)
            self.assertEqual([True], shortened)
            self.assertIs(reader.select_inventory, original_selector)
            try:
                code = self.run_raw(command)
            except ValueError as exc:
                raise ObservedGate(False, str(exc))
            self.assertIn(code, (0, None))
            raise ObservedGate(True)

        with mock.patch.object(self.h.bootstrap, "run", side_effect=intercept):
            with self.assertRaises(ObservedGate) as observed:
                self.h.bootstrap.build_index()
        self.assert_preserved(before)
        self.assertFalse(observed.exception.accepted, "coherently omitted eligible Contact was accepted by Validator")
        self.assertTrue(observed.exception.reason)

    def test_projector_rejects_omitted_authority_before_index_changes(self):
        args, output, _semantic, graph = self.h.unpublished_reader()
        filtered = []
        i = 0
        authority = {"--version-authority-mode", "--version-decisions", "--version-decisions-sha256"}
        while i < len(args):
            if args[i] in authority:
                i += 2
            else:
                filtered.append(args[i])
                i += 1
        if "--version-graph" not in filtered:
            filtered += ["--version-graph", str(graph)]
        sentinel = b"previous synthetic index - preserve"
        output.write_bytes(sentinel)
        projector = self.h.module(ENGINE / "build_local_semantic_index.py")
        with mock.patch.object(projector, "embed", side_effect=AcceptedSourceEntry):
            accepted = False
            reason = ""
            try:
                self.run_raw(filtered)
            except AcceptedSourceEntry:
                accepted = True
            except ValueError as exc:
                reason = str(exc)
        self.assertFalse(accepted, "projector reached embedding with omitted version authority")
        self.assertTrue(reason)
        self.assertEqual(sentinel, output.read_bytes())
        self.assertFalse(output.with_suffix(output.suffix + ".building").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
