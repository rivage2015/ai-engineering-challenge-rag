"""F05a literal inventory/decision-boundary gold; no Reader or model imports."""
from __future__ import annotations

import contextlib
import copy
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "distribution/macos-local-memory/engine/document_version_resolver.py"
SPEC = importlib.util.spec_from_file_location("f05a_pure_resolver", SOURCE)
resolver = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(resolver)
SERIALIZED_BYTES = 0


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def record(path, token="a"):
    return {"relative_path": path, "kind": "file", "read_status": "observed",
            "sha256": token * 64, "size_bytes": 1, "mtime_ns": 2, "birthtime_ns": 1}


def project(groups):
    """Attacker-side independent resealing; never used as an expected outcome."""
    nodes, edges = [], []
    for group in groups:
        gid = group["group_id"]
        nodes.append({"node_id": gid, "node_type": "document_version_set",
                      "status": group["status"], "candidate_set_sha256": group["candidate_set_sha256"]})
        ids = {}
        for item in group["candidates"]:
            did = "document_version_" + digest({"relative_path": item["relative_path"], "source_sha256": item["source_sha256"]})[:32]
            ids[item["relative_path"]] = did
            nodes.append({"node_id": did, "node_type": "document_version", "status": item["disposition"],
                          **{key: item[key] for key in ("relative_path", "source_sha256", "explicit_years", "explicit_versions")}})
            edges.append({"edge_id": "edge_" + digest([did, "candidate_for", gid])[:32],
                          "from_node_id": did, "edge_type": "candidate_for", "to_node_id": gid,
                          "basis": "normalized_filename_family", "status": "candidate"})
        if group["selected_relative_path"]:
            did = ids[group["selected_relative_path"]]
            edges.append({"edge_id": "edge_" + digest([gid, "active_version", did])[:32],
                          "from_node_id": gid, "edge_type": "active_version", "to_node_id": did,
                          "basis": group["reason_code"],
                          "status": "verified" if group["resolution_basis"] == "human" else "policy_resolved"})
    return sorted(nodes, key=lambda item: item["node_id"]), sorted(edges, key=lambda item: item["edge_id"])


class DocumentVersionGraphValidationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="f05a-pure-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.inventory = self.base / "inventory.jsonl"
        self.graph_path = self.base / "graph.json"
        self.decisions = self.base / "decisions.json"
        self.source_before = SOURCE.read_bytes()
        self.addCleanup(lambda: self.assertEqual(self.source_before, SOURCE.read_bytes()))
        # The actual builder computation runs; only its fixture write is capped.
        atomic = mock.patch.object(resolver, "atomic_json", side_effect=self.atomic)
        atomic.start()
        self.addCleanup(atomic.stop)

    def write(self, path, raw, cap):
        global SERIALIZED_BYTES
        raw = raw.encode("utf-8") if isinstance(raw, str) else raw
        self.assertLessEqual(len(raw), cap)
        SERIALIZED_BYTES += len(raw)
        self.assertLessEqual(SERIALIZED_BYTES, 1048576)
        path.write_bytes(raw)

    def atomic(self, path, value):
        self.write(path, canonical(value), 8192 if path == self.decisions else 65536)

    def records(self, records):
        self.assertLessEqual(len(records), 16)
        self.write(self.inventory, "".join(canonical(item) + "\n" for item in records), 16384)

    def fixture(self, records=None, decisions=None):
        records = records or [record("Guide_ver1.csv"), record("Guide.csv", "b")]
        self.records(records)
        return resolver.build(self.inventory, self.graph_path, decisions)

    def save(self, graph, *, projection=False):
        graph = copy.deepcopy(graph)
        if projection:
            for group in graph["groups"]:
                group["candidate_set_sha256"] = digest([
                    {key: item[key] for key in ("relative_path", "source_sha256", "mtime_ns", "birthtime_ns", "explicit_years", "explicit_versions", "current_markers", "historical_markers", "draft_markers")}
                    for item in group["candidates"]
                ])
            graph["nodes"], graph["edges"] = project(graph["groups"])
            graph["counts"] = {"groups": len(graph["groups"]),
                               "resolved": sum(g["status"] == "resolved" for g in graph["groups"]),
                               "needs_human_review": sum(g["status"] == "needs_human_review" for g in graph["groups"])}
        graph["graph_sha256"] = digest({k: v for k, v in graph.items() if k != "graph_sha256"})
        self.atomic(self.graph_path, graph)
        return graph

    def validate(self, *args):
        before = {path: path.read_bytes() for path in (self.inventory, self.graph_path, self.decisions) if path.exists()}
        with mock.patch.object(resolver, "atomic_json", side_effect=AssertionError("validation must not write")), mock.patch.object(resolver, "build", side_effect=AssertionError("validation must not build")), mock.patch.object(resolver, "record_decision", side_effect=AssertionError("validation must not decide")):
            result = resolver.validate(self.graph_path, self.inventory, *args)
        self.assertEqual(before, {path: path.read_bytes() for path in before})
        self.assertIsInstance(result, dict)
        self.assertIsInstance(result["errors"], list)
        return result

    def rejects(self, *args):
        report = self.validate(*args)
        self.assertEqual("FAIL", report["status"], report)
        self.assertTrue(report["errors"])
        return report

    def accepts(self, *args):
        self.assertEqual({"status": "PASS", "errors": []}, self.validate(*args))

    def choose(self, graph, selected="Guide.csv"):
        group = graph["groups"][0]
        item = next(c for c in group["candidates"] if c["relative_path"] == selected)
        self.atomic(self.decisions, {"schema_version": "1.0", "decisions": [{
            "group_id": group["group_id"], "candidate_set_sha256": group["candidate_set_sha256"],
            "selected_relative_path": selected, "selected_source_sha256": item["source_sha256"],
            "decided_by": "synthetic-human", "decided_at": "2026-09-09T00:00:00+00:00"}]})
        return resolver.build(self.inventory, self.graph_path, self.decisions)

    def activate(self, graph, basis="automatic"):
        group = graph["groups"][0]
        group.update(status="resolved", selected_relative_path="Guide.csv", resolution_basis=basis,
                     reason_code="human_confirmed_active" if basis == "human" else "fabricated_policy", conflicts=[])
        for candidate in group["candidates"]:
            candidate["disposition"] = "active" if candidate["relative_path"] == "Guide.csv" else "historical"

    def test_rejects_empty_resealed_graph(self):
        graph = self.fixture()
        graph["groups"] = []
        self.save(graph, projection=True)
        self.rejects()

    def test_rejects_omitted_unmarked_candidate(self):
        graph = self.fixture()
        group = graph["groups"][0]
        group["candidates"] = [c for c in group["candidates"] if c["relative_path"] != "Guide.csv"]
        group["conflicts"] = ["Guide_ver1.csv"]
        self.save(graph, projection=True)
        self.rejects()

    def test_rejects_absent_inventory_candidate(self):
        graph = self.fixture()
        graph["groups"][0]["candidates"][0].update(relative_path="NeverInInventory.csv", source_sha256="0" * 64)
        self.save(graph, projection=True)
        self.rejects()

    def test_rejects_resealed_candidate_field_changes(self):
        initial = self.fixture()
        changes = {"source_sha256": "0" * 64, "size_bytes": 99, "mtime_ns": 99, "birthtime_ns": 99,
                   "explicit_years": [2025], "explicit_versions": [[77]], "current_markers": ["current"],
                   "historical_markers": ["archive"], "draft_markers": ["draft"]}
        for field, value in changes.items():
            with self.subTest(field=field):
                graph = copy.deepcopy(initial)
                graph["groups"][0]["candidates"][0][field] = value
                self.save(graph, projection=True)
                self.rejects()

    def test_rejects_duplicated_or_reassigned_candidate(self):
        initial = self.fixture()
        for mode in ("duplicate", "reassign", "extra_group", "extra_candidate"):
            with self.subTest(mode=mode):
                graph = copy.deepcopy(initial)
                if mode == "duplicate":
                    graph["groups"][0]["candidates"].append(copy.deepcopy(graph["groups"][0]["candidates"][0]))
                elif mode == "reassign":
                    graph["groups"][0]["group_id"] = "version_set_" + "0" * 32
                    graph["groups"][0]["family_key_sha256"] = "0" * 64
                elif mode == "extra_group":
                    graph["groups"].append(copy.deepcopy(graph["groups"][0]))
                else:
                    extra = copy.deepcopy(graph["groups"][0]["candidates"][0])
                    extra["relative_path"] = "Guide_ver99.csv"
                    graph["groups"][0]["candidates"].append(extra)
                self.save(graph, projection=True)
                self.rejects()

    def test_rejects_fabricated_automatic_selection(self):
        graph = self.fixture()
        self.activate(graph)
        self.save(graph, projection=True)
        self.rejects()

    def test_rejects_embedded_human_authority_without_reading_graph_path(self):
        graph = self.fixture()
        self.activate(graph, "human")
        graph["source"]["decisions_path"] = "/f05a-forbidden-canary/decisions.json"
        self.save(graph, projection=True)
        original_open, original_stat = Path.open, Path.stat
        def forbid(method):
            def guarded(path, *args, **kwargs):
                self.assertNotIn("f05a-forbidden-canary", str(path))
                return method(path, *args, **kwargs)
            return guarded
        with mock.patch.object(Path, "open", forbid(original_open)), mock.patch.object(Path, "stat", forbid(original_stat)):
            self.rejects()

    def test_rejects_resealed_redundant_semantic_changes(self):
        initial = self.fixture()
        mutations = {
            "policy": lambda g: g["policy"].update(year_order_establishes_supersession=True),
            "count": lambda g: g["counts"].update(groups=99),
            "bool_count": lambda g: g["counts"].update(groups=True),
            "reason": lambda g: g["groups"][0].update(reason_code="false_reason"),
            "conflict": lambda g: g["groups"][0].update(conflicts=[]),
            "disposition": lambda g: g["groups"][0]["candidates"][0].update(disposition="historical"),
            "node": lambda g: g["nodes"][0].update(status="fabricated"),
            "edge": lambda g: g["edges"][0].update(basis="human"),
            "extra_edge": lambda g: g["edges"].append(copy.deepcopy(g["edges"][0])),
            "ordering": lambda g: g["groups"][0]["candidates"].reverse(),
        }
        for label, mutate in mutations.items():
            with self.subTest(label=label):
                graph = copy.deepcopy(initial)
                mutate(graph)
                self.save(graph)
                self.rejects()

    def assert_hold(self, graph, reason, paths):
        self.assertEqual({"groups": 1, "resolved": 0, "needs_human_review": 1}, graph["counts"])
        group = graph["groups"][0]
        self.assertEqual(reason, group["reason_code"])
        self.assertIsNone(group["selected_relative_path"])
        self.assertIsNone(group["resolution_basis"])
        self.assertEqual(paths, [c["relative_path"] for c in group["candidates"]])
        self.assertEqual(["needs_human_review"] * len(paths), [c["disposition"] for c in group["candidates"]])
        self.assertFalse(any(e["edge_type"] == "active_version" for e in graph["edges"]))

    def test_accepts_mixed_hold_and_exact_membership(self):
        graph = self.fixture()
        self.assert_hold(graph, "unmarked_candidate_requires_human_review", ["Guide.csv", "Guide_ver1.csv"])
        self.accepts()

    def test_accepts_numeric_version_control(self):
        graph = self.fixture([record("Guide_ver1.2.csv"), record("Guide_ver1.10.csv", "b")])
        group = graph["groups"][0]
        self.assertEqual("Guide_ver1.10.csv", group["selected_relative_path"])
        self.assertEqual("unique_latest_explicit_version", group["reason_code"])
        self.assertEqual(["active", "historical"], [c["disposition"] for c in group["candidates"]])
        self.accepts()

    def test_accepts_year_hold_control(self):
        graph = self.fixture([record("Guide2024.csv"), record("Guide2025.csv", "b")])
        self.assert_hold(graph, "year_order_does_not_establish_supersession", ["Guide2024.csv", "Guide2025.csv"])
        self.accepts()

    def test_accepts_current_version_conflict_control(self):
        graph = self.fixture([record("current/Guide_ver1.csv"), record("Guide_ver2.csv", "b")])
        self.assert_hold(graph, "current_marker_conflicts_with_latest_version", ["Guide_ver2.csv", "current/Guide_ver1.csv"])
        self.accepts()

    def test_accepts_existing_group_exclusion_controls(self):
        for records in ([record("Guide.csv"), record("Guide copy.csv", "b")], [record("Guide_ver1.csv")],
                        [record("Guide_ver1.csv"), record("Else_ver2.csv", "b")],
                        [record("Guide_ver1.csv"), record("Guide_ver2.txt", "b"), {**record("Guide_ver3.csv", "c"), "read_status": "failed"}]):
            with self.subTest(paths=[r["relative_path"] for r in records]):
                graph = self.fixture(records)
                self.assertEqual([], graph["groups"])
                self.accepts()

    def test_accepts_explicit_human_choice_and_rejects_missing_or_wrong_authority(self):
        for selected in ("Guide.csv", "Guide_ver1.csv"):
            with self.subTest(selected=selected):
                graph = self.choose(self.fixture(), selected)
                group = graph["groups"][0]
                self.assertEqual(selected, group["selected_relative_path"])
                self.assertEqual("human", group["resolution_basis"])
                self.assertEqual("human_confirmed_active", group["reason_code"])
                self.assertEqual({"active", "historical"}, {c["disposition"] for c in group["candidates"]})
                self.accepts(self.decisions)
                self.rejects()
                wrong = self.base / "wrong.json"
                self.write(wrong, '{"decisions":[]}', 8192)
                self.rejects(wrong)
                missing = self.base / "missing.json"
                self.rejects(missing)

    def test_stale_human_choice_holds_when_either_peer_changes(self):
        for changed in (0, 1):
            with self.subTest(changed=changed):
                records = [record("Guide_ver1.csv"), record("Guide.csv", "b")]
                self.choose(self.fixture(records))
                records[changed]["sha256"] = "c" * 64
                self.records(records)
                graph = resolver.build(self.inventory, self.graph_path, self.decisions)
                self.assert_hold(graph, "stale_human_decision", ["Guide.csv", "Guide_ver1.csv"])
                self.accepts(self.decisions)
                self.activate(graph, "human")
                self.save(graph, projection=True)
                self.rejects(self.decisions)

    def test_accepts_missing_explicit_decision_file_and_ignores_path_only_metadata(self):
        missing = self.base / "missing.json"
        graph = self.fixture(decisions=missing)
        self.assertIsNone(graph["source"]["decisions_sha256"])
        self.accepts(missing)
        graph["source"]["inventory_path"] = "/f05a-forbidden-canary/inventory.jsonl"
        graph["source"]["decisions_path"] = "/f05a-forbidden-canary/decisions.json"
        self.save(graph)
        original_open, original_stat = Path.open, Path.stat
        def forbid(method):
            def guarded(path, *args, **kwargs):
                self.assertNotIn("f05a-forbidden-canary", str(path))
                return method(path, *args, **kwargs)
            return guarded
        with mock.patch.object(Path, "open", forbid(original_open)), mock.patch.object(Path, "stat", forbid(original_stat)):
            self.accepts()

    def test_hashes_and_parses_each_authorized_snapshot_once(self):
        self.choose(self.fixture())
        snapshots = {p: p.read_bytes() for p in (self.graph_path, self.inventory, self.decisions)}
        calls = []
        def snapshot(path):
            self.assertIn(path, snapshots)
            self.assertNotIn(path, calls)
            calls.append(path)
            return snapshots[path]
        with mock.patch.object(Path, "read_bytes", snapshot), mock.patch.object(Path, "open", side_effect=AssertionError("must hash read snapshot")), mock.patch.object(resolver, "atomic_json", side_effect=AssertionError("must be read only")), mock.patch.object(resolver, "build", side_effect=AssertionError("must reconstruct without writing")):
            self.assertEqual({"status": "PASS", "errors": []}, resolver.validate(self.graph_path, self.inventory, self.decisions))
        self.assertCountEqual(list(snapshots), calls)

    def test_rejects_duplicate_json_keys_and_nonfinite_constants(self):
        graph = self.fixture()
        self.write(self.graph_path, canonical(graph)[:-1] + ',"counts":' + canonical(graph["counts"]) + '}', 65536)
        self.rejects()
        self.save(graph)
        raw = canonical(record("Guide.csv"))[:-1] + ',"relative_path":"Guide.csv"}\n'
        self.write(self.inventory, raw, 16384)
        graph["source"]["inventory_sha256"] = hashlib.sha256(raw.encode()).hexdigest()
        self.save(graph)
        self.rejects()
        for constant in ("NaN", "Infinity", "-Infinity"):
            with self.subTest(constant=constant):
                graph = self.fixture()
                raw = canonical(graph).replace('"size_bytes":1', '"size_bytes":' + constant, 1)
                self.write(self.graph_path, raw, 65536)
                self.rejects()
        self.choose(self.fixture())
        raw = '{"decisions":[],"decisions":[]}'
        self.write(self.decisions, raw, 8192)
        graph = json.loads(self.graph_path.read_bytes())
        graph["source"]["decisions_sha256"] = hashlib.sha256(raw.encode()).hexdigest()
        self.save(graph)
        self.rejects(self.decisions)
        self.write(self.graph_path, b'\xff', 65536)
        self.rejects()

    def test_rejects_malformed_graph_shapes_and_identity(self):
        initial = self.fixture()
        for field, value in (("source", []), ("groups", {}), ("nodes", [None]), ("edges", [7]),
                             ("counts", []), ("schema_version", "unknown"), ("resolver", "unknown"),
                             ("resolver_version", "0.1.3"), ("created_at", ""), ("created_at", None)):
            with self.subTest(field=field, value=value):
                graph = copy.deepcopy(initial)
                graph[field] = value
                self.save(graph)
                self.rejects()
        for label in ("missing", "extra", "nested_duplicate", "root_array"):
            with self.subTest(label=label):
                graph = copy.deepcopy(initial)
                if label == "missing":
                    del graph["policy"]
                    self.save(graph)
                elif label == "extra":
                    graph["alternative_groups"] = []
                    self.save(graph)
                elif label == "nested_duplicate":
                    self.write(self.graph_path, canonical(graph).replace('"size_bytes":1', '"size_bytes":1,"size_bytes":1', 1), 65536)
                else:
                    self.write(self.graph_path, "[]", 65536)
                self.rejects()

    def test_rejects_duplicate_inventory_paths_and_decision_group_ids(self):
        graph = self.fixture([record("Guide_ver1.csv"), record("Guide_ver1.csv", "b")])
        self.rejects()
        graph = self.choose(self.fixture())
        decisions = json.loads(self.decisions.read_bytes())
        decisions["decisions"].append(copy.deepcopy(decisions["decisions"][0]))
        self.atomic(self.decisions, decisions)
        graph["source"]["decisions_sha256"] = hashlib.sha256(self.decisions.read_bytes()).hexdigest()
        self.save(graph)
        self.rejects(self.decisions)

    def test_rejects_inventory_and_graph_hash_changes(self):
        graph = self.fixture()
        graph["graph_sha256"] = "0" * 64
        self.atomic(self.graph_path, graph)
        self.assertIn("graph_hash_mismatch", self.rejects()["errors"])
        graph = self.fixture()
        self.records([record("Else.csv")])
        self.assertIn("inventory_changed", self.rejects()["errors"])

    def test_inventory_order_and_unrelated_addition_preserve_existing_family(self):
        for records in ([record("Guide.csv", "b"), record("Guide_ver1.csv")],
                        [record("Guide_ver1.csv"), record("Guide.csv", "b"), record("Other_ver9.csv", "c")]):
            graph = self.fixture(records)
            self.assert_hold(graph, "unmarked_candidate_requires_human_review", ["Guide.csv", "Guide_ver1.csv"])
            self.accepts()

    def test_validate_cli_explicit_decision_authority(self):
        self.choose(self.fixture())
        base = ["resolver", "validate", "--graph", str(self.graph_path), "--inventory", str(self.inventory)]
        for extra, expected in ((["--decisions", str(self.decisions)], 0), ([], 1)):
            with mock.patch.object(sys, "argv", base + extra), contextlib.redirect_stdout(io.StringIO()) as captured:
                self.assertEqual(expected, resolver.main())
            self.assertEqual("PASS" if expected == 0 else "FAIL", json.loads(captured.getvalue())["status"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
