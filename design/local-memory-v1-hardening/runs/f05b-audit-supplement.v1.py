"""Additional frozen F05b boundary gold; reviewed guarded runner only."""
from __future__ import annotations
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import types
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[3]
RUNS = ROOT / "design/local-memory-v1-hardening/runs"
ENGINE = ROOT / "distribution/macos-local-memory/engine"
APPROVED = False
builder = types.SimpleNamespace()


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class SupplementTests(unittest.TestCase):
    def setUp(self):
        self.assertIs(APPROVED, True)
        holdouts = load(RUNS / "f05b-audit-holdouts.v1.py", "f05b_supplement_helpers")
        holdouts.GUARDED_RUN_APPROVED = True
        self.helpers = holdouts
        self.case = holdouts.HoldoutBase()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.h, self.b = self.case.h, self.case.h.bootstrap

    def test_generic_registration_requires_explicit_legacy_and_app_descriptor(self):
        _args, _output, semantic, _graph = self.h.unpublished_reader(versioned=False)
        before, config = self.case.file_map(semantic.parent), self.b.CONFIG.read_bytes()
        for kwargs in ({}, {"legacy_unversioned": False}):
            with self.subTest(kwargs=kwargs), self.assertRaisesRegex(ValueError, "decision_snapshot_descriptor_invalid"):
                self.b._reader_generation_contract_body(semantic, **kwargs)
        contract = self.b._reader_generation_contract_body(semantic, legacy_unversioned=True)
        self.assertEqual("legacy_unversioned", contract["authority_mode"])
        self.assertIsNone(contract["decision_snapshot"])
        self.assertNotIn("document_version_graph", contract["generation_artifacts"])
        with self.assertRaises(TypeError):
            self.b.write_reader_generation_contract(semantic, semantic.parent.name)
        with self.assertRaisesRegex(ValueError, "decision_snapshot_descriptor_invalid"):
            self.b.write_reader_generation_contract(semantic, semantic.parent.name, decision_snapshot=None)
        self.assertEqual(before, self.case.file_map(semantic.parent))
        self.assertEqual(config, self.b.CONFIG.read_bytes())

    def test_actual_new_registration_descriptor_omission_keeps_prior_generation(self):
        config = self.case.build_app()
        generation = self.case.paths(config).parent
        before, publication = self.case.file_map(generation), self.case.publication(config)
        register, reached = self.b.write_reader_generation_contract, []
        def omit(semantic, generation_name, *, decision_snapshot):
            self.assertEqual(config["active_generation"], generation.name)
            self.assertEqual(generation_name, decision_snapshot["generation"])
            reached.append(True)
            return register(semantic, generation_name)
        with mock.patch.object(self.b, "write_reader_generation_contract", side_effect=omit):
            with self.assertRaises(TypeError):
                self.b.build_index()
        self.assertEqual([True], reached)
        self.assertEqual(before, self.case.file_map(generation))
        self.case.assert_publication(config, publication)

    def test_saved_status_passes_registered_d0_before_snapshot_open(self):
        config = self.case.build_app()
        snapshot = self.case.snapshot(config)
        contract_path = Path(config["semantic_path"]) / self.b.READER_GENERATION_CONTRACT_FILENAME
        contract = json.loads(contract_path.read_bytes())
        descriptor = contract["decision_snapshot"]
        self.assertEqual(hashlib.sha256(self.helpers.EMPTY).hexdigest(), descriptor["sha256"])
        before = self.case.file_map(snapshot.parent.parent)
        resolver = self.b._decision_resolver()
        attest, calls = resolver.attest, []
        with self.case.watch_input_opens([snapshot], forbid=[self.b.DOCUMENT_VERSION_DECISIONS]) as counts:
            def checked(*args, **kwargs):
                self.assertEqual(0, counts[str(snapshot)], "expected digest must precede reading candidate snapshot")
                self.assertEqual({"decision_mode": "snapshot", "decisions_path": snapshot,
                                  "expected_decisions_sha256": descriptor["sha256"]}, kwargs)
                calls.append(True)
                return attest(*args, **kwargs)
            with mock.patch.object(resolver, "attest", side_effect=checked), mock.patch.object(self.b, "_decision_resolver", return_value=resolver):
                self.assertEqual("current", self.b.reader_generation_contract_status(config)["state"])
        self.assertEqual([True], calls)
        self.assertEqual({str(snapshot): 1}, counts)
        self.assertEqual(before, self.case.file_map(snapshot.parent.parent))

    def test_historical_01_contract_current_in_old_reader_migrates_without_repair(self):
        # Use the frozen historical serializer/status, not a guessed current
        # contract with one missing key. Synthetic stage resources are current;
        # this is a real old-format registration, not a whole old-app replay.
        _args, index, semantic, graph = self.h.unpublished_reader(versioned=True)
        paths, snapshot = graph.parent, graph.parent / "document-version-decisions.snapshot.json"
        resolver = self.h.module(ENGINE / "document_version_resolver.py")
        with mock.patch.object(resolver, "atomic_json", side_effect=lambda p, v: self.case.write(p, self.helpers.canonical(v))):
            old_graph = resolver.build(paths / "path-source-inventory.jsonl", graph)
        state_path = semantic / "adaptive-reader-state.json"
        state = json.loads(state_path.read_bytes())
        state["builder_version"] = "0.4.0"
        state["document_version_graph"] = {"path": str(graph), "sha256": hashlib.sha256(graph.read_bytes()).hexdigest(),
                                           "graph_sha256": old_graph["graph_sha256"]}
        self.case.write(state_path, self.helpers.canonical(state))
        snapshot.unlink()  # owned synthetic unpublished fixture only
        self.case.write(index, b"historical synthetic index", 1024)
        historical_path = RUNS / "f05b-executor-before-bootstrap.v1.py"
        self.assertEqual("cad0bfee06fbdf0af0319d6fed2d467310ef697e848a9b6d170eacf061f31b55", hashlib.sha256(historical_path.read_bytes()).hexdigest())
        historical = load(historical_path, "f05b_historical_registration_only")
        historical.ENGINE = ENGINE
        self.assertEqual("0.1", historical.READER_GENERATION_CONTRACT_SCHEMA_VERSION)
        with mock.patch.object(historical, "atomic_json", side_effect=lambda p, v: self.case.write(p, self.helpers.canonical(v))):
            registration = historical.write_reader_generation_contract(semantic, semantic.parent.name)
        old_contract = json.loads((semantic / historical.READER_GENERATION_CONTRACT_FILENAME).read_bytes())
        self.assertEqual("0.1", old_contract["schema_version"])
        self.assertEqual("0.4.0", old_contract["producer_records"]["builder"]["version"])
        self.assertIn("document_version_graph", old_contract["generation_artifacts"])
        self.assertNotIn("decision_snapshot", old_contract)
        config = {"active_generation": semantic.parent.name, "semantic_path": str(semantic), "index_path": str(index),
                  self.b.READER_GENERATION_CONTRACT_CONFIG_KEY: registration}
        self.assertEqual("current", historical.reader_generation_contract_status(config)["state"])
        self.case.write(self.b.DOCUMENT_VERSION_DECISIONS, b"malformed today's shared decisions", 8192)
        before, original_config = self.case.file_map(semantic.parent), copy.deepcopy(config)
        with self.case.watch_input_opens([], forbid=[snapshot, self.b.DOCUMENT_VERSION_DECISIONS]):
            status = self.b.reader_generation_contract_status(config)
        self.assertEqual("reader_migration_required", status["state"])
        self.assertEqual("reader_generation_contract_registration_invalid", status["reason_code"])
        self.assertEqual(before, self.case.file_map(semantic.parent))
        self.assertEqual(original_config, config)
        self.assertFalse(snapshot.exists())

    def test_serialized_context_exact_sets_types_and_nulls_reject_before_path_io(self):
        projector = self.h.module(ENGINE / "build_local_semantic_index.py")
        base = {"output_dir": self.h.base, "source_root": self.h.source, "inventory": self.h.base / "inventory.jsonl"}
        no_decisions = dict(base, version_graph=self.h.base / "graph.json", version_authority_mode="no_decisions")
        snapshot = dict(no_decisions, version_authority_mode="snapshot", version_decisions_path=self.h.base / "snapshot.json", version_decisions_sha256="a" * 64)
        self.assertEqual({}, projector._version_context_kwargs(base))
        self.assertEqual({"version_authority_mode": "no_decisions"}, projector._version_context_kwargs(no_decisions))
        self.assertEqual({"version_authority_mode": "snapshot", "version_decisions_path": snapshot["version_decisions_path"],
                          "version_decisions_sha256": "a" * 64}, projector._version_context_kwargs(snapshot))
        cases = [dict(base, version_authority_mode="no_decisions"), dict(base, version_graph=self.h.base / "graph.json"),
                 dict(no_decisions, version_decisions_path=None), dict(no_decisions, version_decisions_sha256=None),
                 dict(no_decisions, version_decisions_path=None, version_decisions_sha256=None),
                 dict(snapshot, extra=True), {k: v for k, v in snapshot.items() if k != "version_decisions_path"},
                 {k: v for k, v in snapshot.items() if k != "version_decisions_sha256"}]
        for key in ("output_dir", "source_root", "inventory", "version_graph", "version_decisions_path"):
            for value in (None, True, 1, ""):
                cases.append(dict(snapshot, **{key: value}))
        for mode in (None, True, 1, "explicit_decisions", "unknown"):
            cases.append(dict(snapshot, version_authority_mode=mode))
        for digest in (None, True, 1, "A" * 64, "short"):
            cases.append(dict(snapshot, version_decisions_sha256=digest))
        with mock.patch.object(Path, "resolve", side_effect=self.helpers.UnexpectedWork("invalid context performed path resolution")), \
             mock.patch.object(projector, "_load_lineage_validator", side_effect=self.helpers.UnexpectedWork("invalid context loaded validator")):
            for number, context in enumerate(cases):
                with self.subTest(case=number), self.assertRaises(ValueError):
                    projector._attest_lineage_context([], [], [], context)

    def test_resolver_only_suffix_families_are_reconstructed_before_reader_filter(self):
        records = [{"relative_path": name, "kind": "file", "read_status": "observed", "size_bytes": 1,
                    "sha256": token * 64, "mtime_ns": 2, "birthtime_ns": 1}
                   for name, token in (("Opaque_ver1.doc", "a"), ("Opaque_ver2.doc", "b"), ("Contact.txt", "c"))]
        inventory, graph = self.h.base / "full-inventory.jsonl", self.h.base / "full-graph.json"
        self.case.write(inventory, "".join(self.helpers.canonical(r) + "\n" for r in records), 16384)
        resolver = self.h.module(ENGINE / "document_version_resolver.py")
        reader = self.h.module(ENGINE / "build_adaptive_semantic_graph.py")
        self.assertIn(".doc", resolver.DOCUMENT_SUFFIXES)
        self.assertNotIn(".doc", reader.SUPPORTED_SUFFIXES)
        with mock.patch.object(resolver, "atomic_json", side_effect=lambda p, v: self.case.write(p, self.helpers.canonical(v))):
            value = resolver.build(inventory, graph)
        self.assertEqual(["Opaque_ver1.doc", "Opaque_ver2.doc"], [v["relative_path"] for v in value["groups"][0]["candidates"]])
        selection = reader.select_attested_inventory(inventory, graph, version_authority_mode="no_decisions")
        self.assertEqual(["Contact.txt"], [v["relative_path"] for v in selection["selected"]])
        self.assertEqual({"selected": 1, "unsupported": 2, "version_ungrouped": 1}, selection["selection_counts"])
        value.update(groups=[], nodes=[], edges=[], counts={"groups": 0, "resolved": 0, "needs_human_review": 0})
        value["graph_sha256"] = hashlib.sha256(self.helpers.canonical({k: v for k, v in value.items() if k != "graph_sha256"}).encode()).hexdigest()
        self.case.write(graph, self.helpers.canonical(value))
        with self.assertRaisesRegex(ValueError, "document_version_attestation_failed"):
            reader.select_attested_inventory(inventory, graph, version_authority_mode="no_decisions")

    def test_capture_duplicate_groups_permission_error_and_capacity_reason(self):
        config = self.case.build_app()
        generation = self.case.paths(config).parent
        old_files, publication = self.case.file_map(generation), self.case.publication(config)
        value = json.loads(self.case.decisions(config, ["Guide_ver1.csv"]))
        value["decisions"].append(copy.deepcopy(value["decisions"][0]))
        self.case.write(self.b.DOCUMENT_VERSION_DECISIONS, self.helpers.canonical(value), 8192)
        self.case.fail_capture_before_resolver()
        self.case.write(self.b.DOCUMENT_VERSION_DECISIONS, self.helpers.EMPTY, 8192)
        resolver = self.b._decision_resolver()
        with mock.patch.object(resolver, "read_decision_snapshot", side_effect=PermissionError("synthetic read denied")), \
             mock.patch.object(self.b, "_decision_resolver", return_value=resolver):
            self.case.fail_capture_before_resolver()
        changed = self.b.load_json(self.b.CONFIG)
        changed["max_decision_snapshot_bytes"] = len(self.helpers.EMPTY) - 1
        self.case.write(self.b.CONFIG, self.helpers.canonical(changed))
        configured = self.b.CONFIG.read_bytes()
        with self.assertRaisesRegex(ValueError, "decision_snapshot_too_large: increase max_decision_snapshot_bytes within 67108864; no bytes were truncated"):
            self.b.build_index()
        self.assertEqual(configured, self.b.CONFIG.read_bytes())
        self.assertEqual(old_files, self.case.file_map(generation))
        self.assertEqual(publication[1], Path(config["index_path"]).read_bytes())
        self.assertEqual(self.helpers.EMPTY, self.b.DOCUMENT_VERSION_DECISIONS.read_bytes())


if __name__ == "__main__":
    raise SystemExit("Use the approved guarded audit runner only")
