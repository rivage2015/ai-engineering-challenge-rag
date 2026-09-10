"""Prepared independent F05b holdouts; NOT EXECUTED during preparation.

Import only through a post-freeze, reviewed F04a + F05b budget runner. The
runner must explicitly set GUARDED_RUN_APPROVED=True before setUp. No product
or shared fixture is imported at module import time. No expectation is learned
from a failed run; literal outcomes are frozen in the companion gold.
"""
from __future__ import annotations
import contextlib
import copy
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

ROOT = Path(__file__).resolve().parents[3]
PACKAGE = ROOT / "distribution/macos-local-memory"
ENGINE = PACKAGE / "engine"
GUARDED_RUN_APPROVED = False
builder = types.SimpleNamespace()  # F04a target-loader compatibility only.
EMPTY = b'{"schema_version":"1.0","decisions":[]}\n'
EXPLICIT_BYTES = 0
COUNTS = {"inventory_unresolved": 1, "policy_excluded": 1, "selected": 3,
          "unsupported": 1, "version_active": 1, "version_historical": 1,
          "version_ungrouped": 1}
LIMITS = {"unsupported_files": 1, "policy_excluded_files": 1,
          "inventory_unresolved_files": 1, "historical_version_files_held": 1,
          "version_files_needing_human_review": 0}


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


class UnexpectedWork(BaseException):
    pass


class HoldoutBase(unittest.TestCase):
    def setUp(self):
        self.assertIs(GUARDED_RUN_APPROVED, True, "requires coherent source freeze and approved guard runner")
        path = PACKAGE / "tests/test_versioned_safe_index_e2e.py"
        spec = importlib.util.spec_from_file_location("f05b_independent_fixture", path)
        fixture = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(fixture)
        self.h = fixture.VersionedSafeIndexE2E()
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)
        self.source_bytes = 0
        self.seed()

    def write(self, path, raw, cap=65536):
        global EXPLICIT_BYTES
        raw = raw.encode("utf-8") if isinstance(raw, str) else raw
        self.assertLessEqual(len(raw), cap)
        EXPLICIT_BYTES += len(raw)
        self.assertLessEqual(EXPLICIT_BYTES, 1048576)
        if path.is_relative_to(self.h.source):
            self.source_bytes += len(raw)
            self.assertLessEqual(self.source_bytes, 16384)
        path.write_bytes(raw)

    def seed(self):
        for name, raw in {"Guide_ver1.csv": b"task,owner\nold,alice\n",
                          "Guide_ver2.csv": b"task,owner\ncurrent,bob\n",
                          "Contact.txt": b"contact: independent front desk\n"}.items():
            self.write(self.h.source / name, raw, 16384)

    def build_app(self):
        self.h.bootstrap.build_index()
        config = self.h.bootstrap.load_json(self.h.bootstrap.CONFIG)
        self.assertEqual("current", self.h.bootstrap.reader_generation_contract_status(config)["state"])
        return config

    def paths(self, config):
        paths = Path(config["path_graph_path"])
        self.assertEqual("01-path", paths.name)
        self.assertEqual(config["active_generation"], paths.parent.name)
        return paths

    def snapshot(self, config):
        return self.paths(config) / "document-version-decisions.snapshot.json"

    def file_map(self, directory):
        result = {}
        total = 0
        for path in sorted(directory.rglob("*")):
            self.assertFalse(path.is_symlink(), "byte-map control expects regular owned generation")
            if path.is_file():
                raw = path.read_bytes()
                total += len(raw)
                self.assertLessEqual(total, 4 * 1048576, "bounded in-memory observation, not fixture-write allowance")
                result[str(path.relative_to(directory))] = raw
        return result

    def publication(self, config):
        return self.h.bootstrap.CONFIG.read_bytes(), Path(config["index_path"]).read_bytes()

    def assert_publication(self, config, before):
        self.assertEqual(before, self.publication(config))

    def evidence_paths(self, config):
        with contextlib.closing(sqlite3.connect(config["index_path"])) as connection:
            return {row[0] for row in connection.execute("SELECT relative_path FROM evidence")}

    def decisions(self, config, selections):
        graph_path = self.paths(config) / "document-version-graph.json"
        graph = json.loads(graph_path.read_bytes())
        output = self.h.base / "independent-decisions.json"
        resolver = self.h.module(ENGINE / "document_version_resolver.py")
        def bounded_atomic(path, value):
            self.write(path, canonical(value), 8192)
        with mock.patch.object(resolver, "atomic_json", side_effect=bounded_atomic):
            for selected in selections:
                group = next(g for g in graph["groups"] if selected in {c["relative_path"] for c in g["candidates"]})
                resolver.record_decision(graph_path, output, group["group_id"], selected, "independent-synthetic-human")
        return output.read_bytes()

    @contextlib.contextmanager
    def watch_input_opens(self, expected_paths, *, forbid=()):
        """Observe Python path-open events; assert counts outside this context.

        This is not an OS sandbox or already-open FD proof. Inputs are unique
        absolute paths. A relative dir_fd open that cannot be attributed is a
        harness preflight issue, never a reason to silently waive a count.
        """
        keys = {str(path): 0 for path in expected_paths}
        forbidden = {str(path) for path in forbid}
        active = [True]
        def audit(event, args):
            if not active[0] or event != "open" or not isinstance(args[0], (str, bytes)):
                return
            path = os.fsdecode(args[0])
            mode, flags = args[1:3]
            writing = (isinstance(mode, str) and any(c in mode for c in "wax+")) or (isinstance(flags, int) and bool(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND)))
            if writing:
                return
            self.assertNotIn(path, forbidden, "unexpected source/shared input open")
            if path in keys:
                keys[path] += 1
                self.assertEqual(1, keys[path], "second open of an attested input")
        sys.addaudithook(audit)
        try:
            yield keys
        finally:
            active[0] = False

    def fail_capture_before_resolver(self):
        path_gate = []
        def checked(command, log=None):
            name = Path(command[1]).name
            if name == "document_version_resolver.py":
                raise UnexpectedWork("capture failure reached resolver")
            result = self.h.run_cli(command, log)
            if name == "validate_path_graph.py":
                path_gate.append(True)
            return result
        with mock.patch.object(self.h.bootstrap, "run", side_effect=checked):
            with self.assertRaises((ValueError, OSError, SystemExit)):
                self.h.bootstrap.build_index()
        self.assertEqual([True], path_gate, "capture failure must be after actual Path validation")


class IndependentSnapshotApplicationHoldouts(HoldoutBase):
    def test_capture_exact_present_bytes_retains_two_groups(self):
        self.write(self.h.source / "Plan_ver1.csv", b"task,owner\nprior,charlie\n", 16384)
        self.write(self.h.source / "Plan_ver2.csv", b"task,owner\nnew,dana\n", 16384)
        first = self.build_app()
        payload = json.loads(self.decisions(first, ["Guide_ver1.csv", "Plan_ver2.csv"]))
        self.assertEqual(2, len(payload["decisions"]))
        raw = (json.dumps(payload, ensure_ascii=False, indent=3) + "  \n").encode()
        self.write(self.h.bootstrap.DOCUMENT_VERSION_DECISIONS, raw, 8192)
        second = self.build_app()
        self.assertEqual(raw, self.snapshot(second).read_bytes())
        self.assertEqual(raw, self.h.bootstrap.DOCUMENT_VERSION_DECISIONS.read_bytes())
        self.assertEqual({"Contact.txt", "Guide_ver1.csv", "Plan_ver2.csv"}, self.evidence_paths(second))
        graph = json.loads((self.paths(second) / "document-version-graph.json").read_bytes())
        self.assertEqual(sha(raw), graph["source"]["decisions_sha256"])
        self.assertEqual(2, len(graph["groups"]))
        self.assertTrue(all(g["resolution_basis"] == "human" for g in graph["groups"]))

    def test_capture_rejects_strict_invalid_present_json_before_resolver(self):
        config = self.build_app()
        generation = self.paths(config).parent
        old_files, publication = self.file_map(generation), self.publication(config)
        payloads = (b"not json", b"[]", b"\xff", b'{"schema_version":"1.0","decisions":[],"decisions":[]}',
                    b'{"schema_version":"1.0","decisions":[],"unused":{"k":1,"k":2}}',
                    b'{"schema_version":"1.0","decisions":[],"unused":NaN}',
                    b'{"schema_version":"1.0","decisions":[],"unused":1e999}')
        for raw in payloads:
            with self.subTest(raw=raw):
                self.write(self.h.bootstrap.DOCUMENT_VERSION_DECISIONS, raw, 8192)
                self.fail_capture_before_resolver()
                self.assertEqual(raw, self.h.bootstrap.DOCUMENT_VERSION_DECISIONS.read_bytes())
                self.assertEqual(old_files, self.file_map(generation))
                self.assert_publication(config, publication)

    def test_capture_rejects_shared_symlink_and_directory(self):
        config = self.build_app()
        generation = self.paths(config).parent
        old_files, publication = self.file_map(generation), self.publication(config)
        shared = self.h.bootstrap.DOCUMENT_VERSION_DECISIONS
        target = self.h.base / "regular-shared-decisions.json"
        self.write(target, EMPTY, 8192)
        shared.symlink_to(target)
        self.fail_capture_before_resolver()
        self.assertTrue(shared.is_symlink())
        self.assertEqual(EMPTY, target.read_bytes())
        shared.unlink()  # owned synthetic symlink only
        shared.mkdir()
        self.fail_capture_before_resolver()
        self.assertTrue(shared.is_dir())
        self.assertEqual([], list(shared.iterdir()))
        self.assertEqual(old_files, self.file_map(generation))
        self.assert_publication(config, publication)

    def test_capture_preserves_existing_directory_and_symlink_targets(self):
        config = self.build_app()
        generation = self.paths(config).parent
        old_files, publication = self.file_map(generation), self.publication(config)
        backing = self.h.base / "existing-decisions.json"
        self.write(backing, b"existing independent bytes", 8192)
        for kind in ("directory", "symlink", "dangling"):
            targets = []
            def inject(command, log=None):
                if Path(command[1]).name == "document_version_resolver.py":
                    raise UnexpectedWork("capture accepted preexisting nonregular target")
                result = self.h.run_cli(command, log)
                if Path(command[1]).name == "validate_path_graph.py":
                    path = Path(command[2]).parent / "document-version-decisions.snapshot.json"
                    if kind == "directory":
                        path.mkdir()
                    else:
                        path.symlink_to(backing if kind == "symlink" else self.h.base / "missing-independent-decisions.json")
                    targets.append(path)
                return result
            with self.subTest(kind=kind), mock.patch.object(self.h.bootstrap, "run", side_effect=inject):
                with self.assertRaises((ValueError, OSError, SystemExit)):
                    self.h.bootstrap.build_index()
            self.assertEqual(1, len(targets))
            if kind == "directory":
                self.assertTrue(targets[0].is_dir())
                self.assertEqual([], list(targets[0].iterdir()))
            else:
                self.assertTrue(targets[0].is_symlink())
            self.assertEqual(b"existing independent bytes", backing.read_bytes())
            self.assertEqual(old_files, self.file_map(generation))
            self.assert_publication(config, publication)

    def captured_pipeline(self):
        pipeline = self.h.bootstrap.run_semantic_pipeline
        calls = []
        def traced(*args, **kwargs):
            self.assertIn("decision_snapshot", kwargs)
            calls.append((args, copy.deepcopy(kwargs)))
            return pipeline(*args, **kwargs)
        with mock.patch.object(self.h.bootstrap, "run_semantic_pipeline", side_effect=traced):
            config = self.build_app()
        self.assertEqual(1, len(calls))
        return config, pipeline, calls[0]

    def test_pipeline_rejects_descriptor_omission_types_and_wrong_generation(self):
        config, pipeline, (args, kwargs) = self.captured_pipeline()
        generation = self.paths(config).parent
        old_files, publication = self.file_map(generation), self.publication(config)
        descriptor = kwargs["decision_snapshot"]
        self.assertEqual({"generation", "path", "sha256", "byte_count"}, set(descriptor))
        self.assertEqual({"generation": config["active_generation"], "path": str(self.snapshot(config)),
                          "sha256": sha(EMPTY), "byte_count": len(EMPTY)}, descriptor)
        omitted = dict(kwargs)
        omitted.pop("decision_snapshot")
        with mock.patch.object(self.h.bootstrap, "run", side_effect=UnexpectedWork("invalid descriptor reached command")):
            with self.assertRaises(TypeError):
                pipeline(*args, **omitted)
            variants = [dict(descriptor, generation="other-generation"), dict(descriptor, byte_count=True),
                        dict(descriptor, byte_count=float(len(EMPTY))), dict(descriptor, sha256=None),
                        dict(descriptor, path=str(self.h.bootstrap.DOCUMENT_VERSION_DECISIONS)),
                        dict(descriptor, path=str(self.paths(config) / ".." / "01-path" / self.snapshot(config).name)),
                        dict(descriptor, extra="not-authority"), {k: v for k, v in descriptor.items() if k != "generation"}]
            for changed in variants:
                with self.subTest(descriptor=changed), self.assertRaises((ValueError, OSError, SystemExit)):
                    pipeline(*args, **dict(kwargs, decision_snapshot=changed))
        self.assertEqual(old_files, self.file_map(generation))
        self.assert_publication(config, publication)

    def test_pipeline_rejects_01_path_symlink_to_other_generation_identical_bytes(self):
        config, pipeline, (args, kwargs) = self.captured_pipeline()
        paths = self.paths(config)
        old_path_files = self.file_map(paths)
        other = self.h.base / "other-generation" / "01-path"
        other.mkdir(parents=True)
        for relative, raw in old_path_files.items():
            destination = other / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            self.write(destination, raw, 65536)
        saved = paths.with_name("01-path-owned-before-symlink")
        paths.rename(saved)
        paths.symlink_to(other, target_is_directory=True)
        publication = self.publication(config)
        self.assertEqual(EMPTY, (other / "document-version-decisions.snapshot.json").read_bytes())
        with mock.patch.object(self.h.bootstrap, "run", side_effect=UnexpectedWork("symlink ancestor accepted before commands")):
            with self.assertRaises((ValueError, OSError, SystemExit)):
                pipeline(*args, **kwargs)
        self.assertTrue(paths.is_symlink())
        self.assertEqual(old_path_files, self.file_map(saved))
        self.assertEqual(old_path_files, self.file_map(other))
        self.assert_publication(config, publication)

    def test_next_d1_generation_preserves_all_d0_generation_bytes(self):
        first = self.build_app()
        first_generation = self.paths(first).parent
        old_files = self.file_map(first_generation)
        old_index = Path(first["index_path"]).read_bytes()
        raw = self.decisions(first, ["Guide_ver1.csv"])
        shared = self.h.bootstrap.DOCUMENT_VERSION_DECISIONS
        self.write(shared, raw, 8192)
        with self.watch_input_opens([], forbid=[shared]):
            self.assertEqual("current", self.h.bootstrap.reader_generation_contract_status(first)["state"])
        second = self.build_app()
        self.assertNotEqual(first["active_generation"], second["active_generation"])
        self.assertEqual(raw, self.snapshot(second).read_bytes())
        self.assertEqual({"Contact.txt", "Guide_ver1.csv"}, self.evidence_paths(second))
        self.assertEqual(old_files, self.file_map(first_generation))
        self.assertEqual(old_index, Path(first["index_path"]).read_bytes())

    def test_model_ready_shared_change_between_calls_keeps_captured_d0(self):
        pipeline = self.h.bootstrap.run_semantic_pipeline
        descriptors = []
        d1 = b' { "schema_version" : "1.0", "decisions" : [] } \n'
        def traced(*args, **kwargs):
            descriptor = copy.deepcopy(kwargs["decision_snapshot"])
            descriptors.append(descriptor)
            if len(descriptors) == 2:
                self.write(self.h.bootstrap.DOCUMENT_VERSION_DECISIONS, d1, 8192)
            return pipeline(*args, **kwargs)
        with mock.patch.object(self.h.bootstrap, "run_semantic_pipeline", side_effect=traced), \
             mock.patch.object(self.h.bootstrap, "local_model_available", side_effect=[False, True]), \
             mock.patch.object(self.h.bootstrap, "semantic_contains_images", return_value=True):
            config = self.build_app()
        self.assertEqual(2, len(descriptors))
        self.assertEqual(descriptors[0], descriptors[1])
        self.assertEqual(sha(EMPTY), descriptors[0]["sha256"])
        self.assertEqual(len(EMPTY), descriptors[0]["byte_count"])
        self.assertEqual(EMPTY, self.snapshot(config).read_bytes())
        self.assertEqual(d1, self.h.bootstrap.DOCUMENT_VERSION_DECISIONS.read_bytes())
        self.assertTrue(str(config["semantic_path"]).endswith("02-semantic-model-ready"))


class IndependentSnapshotConsumerHoldouts(HoldoutBase):
    def fixture(self):
        self.write(self.h.source / ".env", b"synthetic excluded value\n", 16384)
        self.write(self.h.source / "blob.bin", b"independent unsupported data", 16384)
        records = []
        for path in sorted(self.h.source.iterdir()):
            raw = path.read_bytes()
            records.append({"relative_path": path.name, "kind": "file", "sha256": sha(raw), "size_bytes": len(raw),
                            "read_status": "observed", "mtime_ns": path.stat().st_mtime_ns, "birthtime_ns": None})
        records.append({"relative_path": "Unseen.csv", "kind": "file", "sha256": None, "size_bytes": 1,
                        "read_status": "read_failed", "mtime_ns": None, "birthtime_ns": None})
        self.assertEqual(6, len(records))
        inventory = self.h.base / "independent-inventory.jsonl"
        graph = self.h.base / "independent-version-graph.json"
        decisions = self.h.base / "independent-snapshot-decisions.json"
        self.write(inventory, "".join(canonical(record) + "\n" for record in records), 16384)
        self.write(decisions, EMPTY, 8192)
        resolver = self.h.module(ENGINE / "document_version_resolver.py")
        with mock.patch.object(resolver, "atomic_json", side_effect=lambda path, value: self.write(path, canonical(value), 65536)):
            resolver.build(inventory, graph, decisions)
        reader = self.h.module(ENGINE / "build_adaptive_semantic_graph.py")
        validator = self.h.module(ENGINE / "validate_adaptive_semantic_graph.py")
        kwargs = {"version_authority_mode": "snapshot", "version_decisions_path": decisions,
                  "version_decisions_sha256": sha(EMPTY)}
        output = self.h.base / "independent-semantic"
        return inventory, graph, decisions, output, reader, validator, kwargs

    def assert_literal(self, output, state):
        manifest = json.loads((output / "layer1-input-manifest.json").read_bytes())
        self.assertEqual(["Contact.txt", "Guide_ver2.csv"], manifest["paths"])
        self.assertEqual("2", canonical(state["selected_file_count"]))
        self.assertEqual(canonical(COUNTS), canonical(state["selection_counts"]))
        self.assertEqual(canonical(LIMITS), canonical({key: state["limitations"][key] for key in LIMITS}))

    def test_reader_opens_each_attested_input_once_and_uses_literal_selection(self):
        inventory, graph, decisions, output, reader, _validator, kwargs = self.fixture()
        with self.watch_input_opens([inventory, graph, decisions]) as counts:
            state = reader.build(self.h.source, inventory, output, ROOT / "scripts", graph, **kwargs)
        self.assertEqual({str(p): 1 for p in (inventory, graph, decisions)}, counts)
        self.assert_literal(output, state)

    def test_validator_opens_each_attested_input_once_per_invocation(self):
        inventory, graph, decisions, output, reader, validator, kwargs = self.fixture()
        state = reader.build(self.h.source, inventory, output, ROOT / "scripts", graph, **kwargs)
        self.assert_literal(output, state)
        validator.validate(output, self.h.source, inventory, graph, initialize_lineage=True, **kwargs)
        before = self.file_map(output)
        with self.watch_input_opens([inventory, graph, decisions]) as counts:
            result = validator.validate(output, self.h.source, inventory, graph, **kwargs)
        self.assertEqual("PASS", result["status"])
        self.assertEqual({str(p): 1 for p in (inventory, graph, decisions)}, counts)
        self.assertEqual(before, self.file_map(output))

    def test_reader_wrong_snapshot_expectation_stops_before_source_open(self):
        inventory, graph, decisions, output, reader, _validator, kwargs = self.fixture()
        source_files = [path for path in self.h.source.iterdir() if path.is_file()]
        before = {str(path): path.read_bytes() for path in source_files}
        with self.watch_input_opens([], forbid=source_files):
            with self.assertRaises(ValueError):
                reader.build(self.h.source, inventory, output, ROOT / "scripts", graph,
                             **dict(kwargs, version_decisions_sha256="0" * 64))
        self.assertEqual(before, {str(path): path.read_bytes() for path in source_files})

    def test_strict_count_types_extra_zero_missing_and_inventory_digest_fail(self):
        inventory, graph, decisions, output, reader, validator, kwargs = self.fixture()
        state = reader.build(self.h.source, inventory, output, ROOT / "scripts", graph, **kwargs)
        self.assert_literal(output, state)
        validator.validate(output, self.h.source, inventory, graph, initialize_lineage=True, **kwargs)
        state_path = output / "adaptive-reader-state.json"
        original = json.loads(state_path.read_bytes())
        variants = []
        for field, key, value in (("selection_counts", "unsupported", 1.0),
                                  ("selection_counts", "version_needs_human_review", 0),
                                  ("limitations", "version_files_needing_human_review", False),
                                  ("source_inventory", "sha256", "0" * 64)):
            changed = copy.deepcopy(original)
            changed[field][key] = value
            variants.append(changed)
        changed = copy.deepcopy(original)
        changed["selected_file_count"] = 2.0
        variants.append(changed)
        changed = copy.deepcopy(original)
        del changed["selection_counts"]["version_ungrouped"]
        variants.append(changed)
        for number, changed in enumerate(variants):
            with self.subTest(case=number):
                self.write(state_path, canonical(changed))
                with self.assertRaises(ValueError):
                    validator.validate(output, self.h.source, inventory, graph, **kwargs)

    def test_generic_unversioned_success_is_distinct_from_authority_without_graph(self):
        inventory, _graph, _decisions, output, reader, validator, kwargs = self.fixture()
        state = reader.build(self.h.source, inventory, output, ROOT / "scripts")
        self.assertEqual(["Contact.txt", "Guide_ver1.csv", "Guide_ver2.csv"],
                         json.loads((output / "layer1-input-manifest.json").read_bytes())["paths"])
        result = validator.validate(output, self.h.source, inventory, initialize_lineage=True)
        self.assertEqual("PASS", result["status"])
        self.assertEqual("3", canonical(state["selected_file_count"]))
        with self.assertRaises(ValueError):
            validator.validate(output, self.h.source, inventory, **kwargs)


if __name__ == "__main__":
    raise SystemExit("Prepared only: use a source-frozen, approved independent guard runner; direct execution is disabled.")
