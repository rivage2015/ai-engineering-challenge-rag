"""F05b additive app controls, fixed before their implementation acceptance run.

Small synthetic sources only. New public kwargs are post-API controls, not RED.
"""
from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import sys
import types
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "distribution/macos-local-memory"
ENGINE = PACKAGE / "engine"
spec = importlib.util.spec_from_file_location("f05b_seed_tests", ROOT / "tests/test_decision_snapshot_e2e.py")
seed_tests = importlib.util.module_from_spec(spec)
spec.loader.exec_module(seed_tests)
builder = types.SimpleNamespace()
EMPTY = seed_tests.EMPTY


class SnapshotControls(unittest.TestCase):
    def setUp(self):
        self.seed_case = seed_tests.DecisionSnapshotApplicationTests()
        self.seed_case.setUp()
        self.addCleanup(self.seed_case.doCleanups)
        self.h = self.seed_case.h
        self.seed_case.seed()

    def build(self):
        self.h.bootstrap.build_index()
        return self.h.bootstrap.load_json(self.h.bootstrap.CONFIG)

    def snapshot(self, config):
        return Path(config["path_graph_path"]) / "document-version-decisions.snapshot.json"

    def snapshot_kwargs(self, config, expected=None):
        path = self.snapshot(config)
        return dict(version_authority_mode="snapshot", version_decisions_path=path,
                    version_decisions_sha256=expected or hashlib.sha256(path.read_bytes()).hexdigest())

    def validate(self, config, kwargs):
        paths = Path(config["path_graph_path"])
        return self.h.module(ENGINE / "validate_adaptive_semantic_graph.py").validate(
            Path(config["semantic_path"]), self.h.source,
            paths / "path-source-inventory.jsonl", paths / "document-version-graph.json", **kwargs)

    def evidence_paths(self, config):
        with contextlib.closing(sqlite3.connect(config["index_path"])) as connection:
            return {row[0] for row in connection.execute("SELECT relative_path FROM evidence")}

    def test_empty_snapshot_digest_propagates_to_reader_validator_index_and_commands(self):
        config = self.build()
        snapshot = self.snapshot(config)
        expected = hashlib.sha256(EMPTY).hexdigest()
        self.assertEqual(EMPTY, snapshot.read_bytes())
        graph = json.loads((snapshot.parent / "document-version-graph.json").read_bytes())
        self.assertEqual(expected, graph["source"]["decisions_sha256"])
        state = json.loads((Path(config["semantic_path"]) / "adaptive-reader-state.json").read_bytes())
        result = self.validate(config, self.snapshot_kwargs(config, expected))
        with contextlib.closing(sqlite3.connect(config["index_path"])) as connection:
            index_binding = json.loads(connection.execute("SELECT value FROM metadata WHERE key='document_version_graph'").fetchone()[0])
        authority = {"mode": "snapshot", "path": str(snapshot), "sha256": expected, "byte_count": len(EMPTY)}
        for binding in (state["document_version_graph"], result["document_version_graph"], index_binding):
            self.assertEqual(authority, binding["decision_authority"])
        consumers = {"build_adaptive_semantic_graph.py", "validate_adaptive_semantic_graph.py", "build_local_semantic_index.py"}
        commands = [command for command in self.h.commands if Path(command[1]).name in consumers]
        self.assertEqual(consumers, {Path(command[1]).name for command in commands})
        for command in commands:
            self.assertEqual("snapshot", command[command.index("--version-authority-mode") + 1])
            self.assertEqual(str(snapshot), command[command.index("--version-decisions") + 1])
            self.assertEqual(expected, command[command.index("--version-decisions-sha256") + 1])
        self.assertEqual({"Guide_ver2.csv", "Contact.txt"}, self.evidence_paths(config))
        self.assertEqual("current", self.h.bootstrap.reader_generation_contract_status(config)["state"])

    def test_shared_change_corruption_and_removal_do_not_change_saved_generation(self):
        first = self.build()
        snapshot = self.snapshot(first)
        expected = hashlib.sha256(snapshot.read_bytes()).hexdigest()
        old_config, config_bytes, index_bytes = self.seed_case.published()
        resolver = self.h.module(ENGINE / "document_version_resolver.py")
        graph_path = snapshot.parent / "document-version-graph.json"
        group = json.loads(graph_path.read_bytes())["groups"][0]
        resolver.record_decision(graph_path, self.h.bootstrap.DOCUMENT_VERSION_DECISIONS, group["group_id"], "Guide_ver1.csv", "synthetic-human")
        shared = self.h.bootstrap.DOCUMENT_VERSION_DECISIONS
        d1 = shared.read_bytes()
        self.assertLessEqual(len(d1), 8192)
        for value in (d1, b"not json", None):
            if value is None:
                shared.unlink()
            else:
                shared.write_bytes(value)
            original_open = Path.open
            def no_shared_open(path, *args, **kwargs):
                self.assertNotEqual(str(shared), str(path), "saved generation must not reopen shared decisions")
                return original_open(path, *args, **kwargs)
            with mock.patch.object(Path, "open", no_shared_open):
                self.assertEqual("current", self.h.bootstrap.reader_generation_contract_status(first)["state"])
                self.assertEqual("PASS", self.validate(first, self.snapshot_kwargs(first, expected))["status"])
            self.assertEqual(EMPTY, snapshot.read_bytes())
            self.assertEqual(config_bytes, self.h.bootstrap.CONFIG.read_bytes())
            self.assertEqual(index_bytes, Path(old_config["index_path"]).read_bytes())
        shared.write_bytes(d1)
        second = self.build()
        self.assertNotEqual(first["active_generation"], second["active_generation"])
        self.assertEqual(d1, self.snapshot(second).read_bytes())
        self.assertEqual({"Contact.txt", "Guide_ver1.csv"}, self.evidence_paths(second))
        self.assertEqual(EMPTY, snapshot.read_bytes())

    def test_changed_missing_and_wrong_expected_snapshot_fail_readonly(self):
        config = self.build()
        snapshot = self.snapshot(config)
        original = snapshot.read_bytes()
        expected = hashlib.sha256(original).hexdigest()
        before = self.seed_case.published()
        kwargs = self.snapshot_kwargs(config, expected)
        wrong = dict(kwargs, version_decisions_sha256="0" * 64)
        with self.assertRaises(ValueError):
            self.validate(config, wrong)
        for data in (original + b" ", None):
            if data is None:
                snapshot.unlink()
            else:
                snapshot.write_bytes(data)
            with self.assertRaises(ValueError):
                self.validate(config, kwargs)
            self.assertTrue(self.h.bootstrap.reader_generation_contract_status(config)["migration_required"])
            self.assertEqual(data, snapshot.read_bytes() if snapshot.exists() else None)
            self.seed_case.assert_preserved(before)

    def test_model_ready_pass_uses_same_snapshot_descriptor(self):
        pipeline = self.h.bootstrap.run_semantic_pipeline
        descriptors = []
        def traced(*args, **kwargs):
            descriptor = kwargs["decision_snapshot"]
            descriptors.append(json.loads(json.dumps(descriptor)))
            return pipeline(*args, **kwargs)
        with mock.patch.object(self.h.bootstrap, "run_semantic_pipeline", side_effect=traced), \
             mock.patch.object(self.h.bootstrap, "local_model_available", side_effect=[False, True]), \
             mock.patch.object(self.h.bootstrap, "semantic_contains_images", return_value=True):
            config = self.build()
        self.assertEqual(2, len(descriptors))
        self.assertEqual(descriptors[0], descriptors[1])
        self.assertEqual({"generation", "path", "sha256", "byte_count"}, set(descriptors[0]))
        self.assertEqual(config["active_generation"], descriptors[0]["generation"])
        self.assertEqual(str(self.snapshot(config)), descriptors[0]["path"])
        self.assertTrue(str(config["semantic_path"]).endswith("02-semantic-model-ready"))

    def test_invalid_capacity_config_and_oversize_preserve_previous_publication(self):
        self.build()
        shared = self.h.bootstrap.DOCUMENT_VERSION_DECISIONS
        shared.write_bytes(EMPTY)
        for value in (True, 0, -1, "100", 1.5, 67_108_865, len(EMPTY) - 1):
            config = self.h.bootstrap.load_json(self.h.bootstrap.CONFIG)
            config["max_decision_snapshot_bytes"] = value
            self.h.bootstrap.atomic_json(self.h.bootstrap.CONFIG, config)
            before = self.seed_case.published()
            with self.assertRaises((ValueError, SystemExit)):
                self.h.bootstrap.build_index()
            self.seed_case.assert_preserved(before)
            self.assertEqual(EMPTY, shared.read_bytes())
        config = self.h.bootstrap.load_json(self.h.bootstrap.CONFIG)
        config["max_decision_snapshot_bytes"] = len(EMPTY)
        self.h.bootstrap.atomic_json(self.h.bootstrap.CONFIG, config)
        current = self.build()
        self.assertEqual(EMPTY, self.snapshot(current).read_bytes())

    def test_existing_snapshot_target_is_not_overwritten(self):
        self.build()
        before = self.seed_case.published()
        targets = []
        sentinel = b"existing snapshot must remain"
        def intercept(command, log=None):
            result = self.h.run_cli(command, log)
            if Path(command[1]).name == "validate_path_graph.py":
                target = Path(command[2]).parent / "document-version-decisions.snapshot.json"
                target.write_bytes(sentinel)
                targets.append(target)
            return result
        with mock.patch.object(self.h.bootstrap, "run", side_effect=intercept):
            with self.assertRaises((OSError, ValueError, SystemExit)):
                self.h.bootstrap.build_index()
        self.assertEqual(1, len(targets))
        self.assertEqual(sentinel, targets[0].read_bytes())
        self.seed_case.assert_preserved(before)

    def test_state_authority_canary_is_not_touched_during_saved_registration_check(self):
        config = self.build()
        state_path = Path(config["semantic_path"]) / "adaptive-reader-state.json"
        state = json.loads(state_path.read_bytes())
        self.assertIn("decision_authority", state["document_version_graph"])
        canary = self.h.base / "must-not-inspect-authority.json"
        state["document_version_graph"]["decision_authority"]["path"] = str(canary)
        state_path.write_text(json.dumps(state), encoding="utf-8")
        original_open, original_stat, original_resolve = Path.open, Path.stat, Path.resolve
        def guarded(original):
            def call(path, *args, **kwargs):
                self.assertNotEqual(str(canary), str(path), "producer-controlled authority must not be inspected")
                return original(path, *args, **kwargs)
            return call
        with mock.patch.object(Path, "open", guarded(original_open)), \
             mock.patch.object(Path, "stat", guarded(original_stat)), \
             mock.patch.object(Path, "resolve", guarded(original_resolve)):
            self.assertTrue(self.h.bootstrap.reader_generation_contract_status(config)["migration_required"])

    def literal_selection_fixture(self):
        (self.h.source / ".env").write_text("synthetic excluded value\n", encoding="utf-8")
        (self.h.source / "blob.bin").write_bytes(b"unsupported synthetic data")
        records = []
        for path in sorted(self.h.source.iterdir()):
            data = path.read_bytes()
            records.append({"relative_path": path.name, "kind": "file", "size_bytes": len(data),
                            "sha256": hashlib.sha256(data).hexdigest(), "read_status": "observed",
                            "mtime_ns": path.stat().st_mtime_ns, "birthtime_ns": None})
        records.append({"relative_path": "Unseen.csv", "kind": "file", "size_bytes": 1,
                        "sha256": None, "read_status": "read_failed", "mtime_ns": None, "birthtime_ns": None})
        self.assertEqual(6, len(records))
        inventory = self.h.base / "literal-inventory.jsonl"
        payload = "".join(json.dumps(record) + "\n" for record in records)
        self.assertLessEqual(len(payload.encode()), 16384)
        inventory.write_text(payload, encoding="utf-8")
        graph = self.h.base / "literal-graph.json"
        resolver = self.h.module(ENGINE / "document_version_resolver.py")
        resolver.build(inventory, graph)
        output = self.h.base / "literal-semantic"
        reader = self.h.module(ENGINE / "build_adaptive_semantic_graph.py")
        state = reader.build(self.h.source, inventory, output, ROOT / "scripts", graph,
                             version_authority_mode="no_decisions")
        validator = self.h.module(ENGINE / "validate_adaptive_semantic_graph.py")
        result = validator.validate(output, self.h.source, inventory, graph,
                                    version_authority_mode="no_decisions", initialize_lineage=True)
        self.assertEqual("PASS", result["status"])
        return inventory, graph, output, state, validator

    def test_full_inventory_literal_exclusions_and_selection_counts(self):
        _inventory, _graph, output, state, _validator = self.literal_selection_fixture()
        manifest = json.loads((output / "layer1-input-manifest.json").read_bytes())
        self.assertEqual(["Contact.txt", "Guide_ver2.csv"], manifest["paths"])
        self.assertEqual(2, state["selected_file_count"])
        self.assertEqual({"inventory_unresolved": 1, "policy_excluded": 1, "selected": 3,
                          "unsupported": 1, "version_active": 1, "version_historical": 1,
                          "version_ungrouped": 1}, state["selection_counts"])
        expected_limits = {"unsupported_files": 1, "policy_excluded_files": 1,
                           "inventory_unresolved_files": 1, "historical_version_files_held": 1,
                           "version_files_needing_human_review": 0}
        self.assertEqual(expected_limits, {key: state["limitations"][key] for key in expected_limits})

    def test_selection_counter_and_limitation_forgery_rejected(self):
        inventory, graph, output, original, validator = self.literal_selection_fixture()
        state_path = output / "adaptive-reader-state.json"
        for field, key, value in (
            ("selection_counts", "selected", 2),
            ("selection_counts", "version_active", True),
            ("selection_counts", "unsupported", 0),
            ("limitations", "unsupported_files", 0),
            ("limitations", "policy_excluded_files", 0),
            ("limitations", "inventory_unresolved_files", 0),
            ("limitations", "historical_version_files_held", 0),
            ("limitations", "version_files_needing_human_review", 1),
        ):
            with self.subTest(field=field, key=key):
                state = json.loads(json.dumps(original))
                state[field][key] = value
                state_path.write_text(json.dumps(state), encoding="utf-8")
                with self.assertRaises(ValueError):
                    validator.validate(output, self.h.source, inventory, graph, version_authority_mode="no_decisions")

    def test_resealed_manifest_order_duplicate_and_inventory_digest_rejected(self):
        inventory, graph, output, original, validator = self.literal_selection_fixture()
        manifest_path = output / "layer1-input-manifest.json"
        original_manifest = json.loads(manifest_path.read_bytes())
        for key, value in (
            ("paths", ["Guide_ver2.csv", "Contact.txt"]),
            ("paths", ["Contact.txt", "Contact.txt"]),
            ("source_inventory_sha256", "0" * 64),
        ):
            with self.subTest(key=key, value=value):
                manifest = dict(original_manifest, **{key: value})
                manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
                state = json.loads(json.dumps(original))
                state["stages"]["input_manifest"]["sha256"] = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
                (output / "adaptive-reader-state.json").write_text(json.dumps(state), encoding="utf-8")
                with self.assertRaises(ValueError):
                    validator.validate(output, self.h.source, inventory, graph, version_authority_mode="no_decisions")


    def test_new_app_registration_rejects_removed_version_binding(self):
        self.build()
        before = self.seed_case.published()
        register = self.h.bootstrap.write_reader_generation_contract
        reached = []
        def forged(semantic, *args, **kwargs):
            path = semantic / "adaptive-reader-state.json"
            state = json.loads(path.read_bytes())
            self.assertIn("decision_authority", state["document_version_graph"])
            state.pop("document_version_graph")
            path.write_text(json.dumps(state), encoding="utf-8")
            reached.append(True)
            try:
                register(semantic, *args, **kwargs)
            except ValueError as exc:
                raise seed_tests.ObservedGate(False, str(exc))
            raise seed_tests.ObservedGate(True)
        with mock.patch.object(self.h.bootstrap, "write_reader_generation_contract", side_effect=forged):
            with self.assertRaises(seed_tests.ObservedGate) as outcome:
                self.h.bootstrap.build_index()
        self.assertEqual([True], reached)
        self.assertFalse(outcome.exception.accepted, "new app registration cannot infer legacy mode from absent producer binding")
        self.seed_case.assert_preserved(before)

    def test_self_consistent_d1_producer_forgery_does_not_replace_registered_d0(self):
        config = self.build()
        snapshot = self.snapshot(config)
        graph_path = snapshot.parent / "document-version-graph.json"
        inventory = snapshot.parent / "path-source-inventory.jsonl"
        semantic = Path(config["semantic_path"])
        state_path = semantic / "adaptive-reader-state.json"
        before = self.seed_case.published()
        saved_contract = (semantic / self.h.bootstrap.READER_GENERATION_CONTRACT_FILENAME).read_bytes()
        unchanged = {str(path): path.read_bytes() for path in semantic.rglob("*")
                     if path.is_file() and path != state_path}
        resolver = self.h.module(ENGINE / "document_version_resolver.py")
        group = json.loads(graph_path.read_bytes())["groups"][0]
        # Choosing the already-active ver2 gives a different legitimate Human
        # D1 without changing Reader selection; downstream list consistency
        # therefore cannot be the only reason to reject this registered swap.
        resolver.record_decision(graph_path, self.h.bootstrap.DOCUMENT_VERSION_DECISIONS,
                                 group["group_id"], "Guide_ver2.csv", "synthetic-human")
        d1 = self.h.bootstrap.DOCUMENT_VERSION_DECISIONS.read_bytes()
        self.assertLessEqual(len(d1), 8192)
        snapshot.write_bytes(d1)
        graph = resolver.build(inventory, graph_path, snapshot)
        self.assertEqual("human", graph["groups"][0]["resolution_basis"])
        state = json.loads(state_path.read_bytes())
        binding = state["document_version_graph"]
        binding["sha256"] = hashlib.sha256(graph_path.read_bytes()).hexdigest()
        binding["graph_sha256"] = graph["graph_sha256"]
        binding["decision_authority"] = {"mode": "snapshot", "path": str(snapshot),
                                          "sha256": hashlib.sha256(d1).hexdigest(), "byte_count": len(d1)}
        state_path.write_text(json.dumps(state), encoding="utf-8")
        state_bytes, graph_bytes = state_path.read_bytes(), graph_path.read_bytes()
        self.assertTrue(self.h.bootstrap.reader_generation_contract_status(config)["migration_required"])
        self.seed_case.assert_preserved(before)
        self.assertEqual(saved_contract, (semantic / self.h.bootstrap.READER_GENERATION_CONTRACT_FILENAME).read_bytes())
        self.assertEqual(unchanged, {str(path): path.read_bytes() for path in semantic.rglob("*")
                                     if path.is_file() and path != state_path})
        self.assertEqual((d1, state_bytes, graph_bytes), (snapshot.read_bytes(), state_path.read_bytes(), graph_path.read_bytes()))

    def test_unregistered_contract_change_rejected_before_snapshot_access(self):
        config = self.build()
        snapshot = self.snapshot(config)
        contract_path = Path(config["semantic_path"]) / self.h.bootstrap.READER_GENERATION_CONTRACT_FILENAME
        contract = json.loads(contract_path.read_bytes())
        contract["producer_records"]["builder"]["version"] = "synthetic-forged-version"
        core = {key: value for key, value in contract.items() if key != "logical_sha256"}
        contract["logical_sha256"] = hashlib.sha256(json.dumps(core, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        contract_path.write_text(json.dumps(contract), encoding="utf-8")
        config_bytes = self.h.bootstrap.CONFIG.read_bytes()
        modified = contract_path.read_bytes()
        active = [True]
        def audit(event, args):
            if active[0] and event == "open" and isinstance(args[0], (str, bytes)):
                path = args[0].decode() if isinstance(args[0], bytes) else args[0]
                self.assertNotEqual(str(snapshot), path, "check CONFIG contract hash before opening snapshot")
        sys.addaudithook(audit)
        try:
            self.assertTrue(self.h.bootstrap.reader_generation_contract_status(config)["migration_required"])
        finally:
            active[0] = False
        self.assertEqual(config_bytes, self.h.bootstrap.CONFIG.read_bytes())
        self.assertEqual(modified, contract_path.read_bytes())


    def test_only_config_registered_hash_mismatch_rejected_before_snapshot_access(self):
        config = self.build()
        self.assertEqual("current", self.h.bootstrap.reader_generation_contract_status(config)["state"])
        snapshot = self.snapshot(config)
        modified_config = json.loads(json.dumps(config))
        registration = modified_config[self.h.bootstrap.READER_GENERATION_CONTRACT_CONFIG_KEY]
        registration["sha256"] = "0" * 64 if registration["sha256"] != "0" * 64 else "1" * 64
        all_before = {str(path): path.read_bytes() for path in snapshot.parent.parent.rglob("*") if path.is_file()}
        config_bytes = self.h.bootstrap.CONFIG.read_bytes()
        active = [True]
        def audit(event, args):
            if active[0] and event == "open" and isinstance(args[0], (str, bytes)):
                path = args[0].decode() if isinstance(args[0], bytes) else args[0]
                self.assertNotEqual(str(snapshot), path, "CONFIG hash mismatch must stop before snapshot read")
        sys.addaudithook(audit)
        try:
            status = self.h.bootstrap.reader_generation_contract_status(modified_config)
        finally:
            active[0] = False
        self.assertTrue(status["migration_required"])
        self.assertEqual("reader_generation_contract_hash_mismatch", status["reason_code"])
        self.assertEqual(config_bytes, self.h.bootstrap.CONFIG.read_bytes())
        self.assertEqual(all_before, {str(path): path.read_bytes() for path in snapshot.parent.parent.rglob("*") if path.is_file()})

    def test_new_registration_rejects_canary_without_inspecting_it(self):
        self.build()
        before = self.seed_case.published()
        register = self.h.bootstrap.write_reader_generation_contract
        canary = self.h.base / "new-registration-canary.json"
        reached = []
        def forged(semantic, *args, **kwargs):
            state_path = semantic / "adaptive-reader-state.json"
            state = json.loads(state_path.read_bytes())
            self.assertEqual("snapshot", state["document_version_graph"]["decision_authority"]["mode"])
            state["document_version_graph"]["decision_authority"]["path"] = str(canary)
            state_path.write_text(json.dumps(state), encoding="utf-8")
            reached.append(True)
            active = [True]
            def audit(event, audit_args):
                if active[0] and event == "open" and isinstance(audit_args[0], (str, bytes)):
                    path = audit_args[0].decode() if isinstance(audit_args[0], bytes) else audit_args[0]
                    self.assertNotEqual(str(canary), path)
            sys.addaudithook(audit)
            original_stat, original_resolve = Path.stat, Path.resolve
            def guarded(original):
                def call(path, *a, **kw):
                    self.assertNotEqual(str(canary), str(path))
                    return original(path, *a, **kw)
                return call
            try:
                with mock.patch.object(Path, "stat", guarded(original_stat)), mock.patch.object(Path, "resolve", guarded(original_resolve)), mock.patch.object(os, "stat", guarded(os.stat)), mock.patch.object(os, "lstat", guarded(os.lstat)):
                    try:
                        register(semantic, *args, **kwargs)
                    except ValueError as exc:
                        raise seed_tests.ObservedGate(False, str(exc))
                    raise seed_tests.ObservedGate(True)
            finally:
                active[0] = False
        with mock.patch.object(self.h.bootstrap, "write_reader_generation_contract", side_effect=forged):
            with self.assertRaises(seed_tests.ObservedGate) as outcome:
                self.h.bootstrap.build_index()
        self.assertEqual([True], reached)
        self.assertFalse(outcome.exception.accepted)
        self.seed_case.assert_preserved(before)

    def test_saved_generation_never_opens_shared_decisions_via_python_file_apis(self):
        config = self.build()
        shared = self.h.bootstrap.DOCUMENT_VERSION_DECISIONS
        shared.write_bytes(b"corrupt shared file must be irrelevant")
        kwargs = self.snapshot_kwargs(config)
        active = [True]
        def audit(event, args):
            if active[0] and event == "open" and isinstance(args[0], (str, bytes)):
                path = args[0].decode() if isinstance(args[0], bytes) else args[0]
                self.assertNotEqual(str(shared), path, "no Python file API may reopen today's shared decisions")
        sys.addaudithook(audit)
        try:
            self.assertEqual("current", self.h.bootstrap.reader_generation_contract_status(config)["state"])
            self.assertEqual("PASS", self.validate(config, kwargs)["status"])
        finally:
            active[0] = False


if __name__ == "__main__":
    unittest.main(verbosity=2)
