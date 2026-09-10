"""Reproduce missing F05 validation; passing is a gap, never product PASS."""
from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import types
import unittest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
SOURCE = ROOT / "distribution/macos-local-memory/engine/document_version_resolver.py"
SOURCE_SHA = "c2b98254ec28a82e8cc7b5ef3f5e780739bcc1b6983ef2a9252609156d4b672f"
assert hashlib.sha256(SOURCE.read_bytes()).hexdigest() == SOURCE_SHA
spec = importlib.util.spec_from_file_location("f05_inventory_resolver", SOURCE)
resolver = importlib.util.module_from_spec(spec)
spec.loader.exec_module(resolver)
builder = types.SimpleNamespace()


class F05StoredGraphGapObservations(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="f05-next-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.inventory = self.base / "inventory.jsonl"
        records = [
            {"relative_path": path, "kind": "file", "read_status": "observed", "sha256": token * 64,
             "size_bytes": 1, "mtime_ns": 2, "birthtime_ns": 1}
            for path, token in (("Guide_ver1.csv", "a"), ("Guide.csv", "b"))
        ]
        raw = "".join(json.dumps(r) + "\n" for r in records)
        self.assertLess(len(raw.encode()), 16384)
        self.inventory.write_text(raw, encoding="utf-8")
        original = self.inventory.read_bytes()
        self.addCleanup(lambda: self.assertEqual(original, self.inventory.read_bytes()))
        self.addCleanup(lambda: self.assertEqual(SOURCE_SHA, hashlib.sha256(SOURCE.read_bytes()).hexdigest()))
        self.good_path = self.base / "normal.json"
        self.good = resolver.build(self.inventory, self.good_path)
        self.assertEqual("unmarked_candidate_requires_human_review", self.good["groups"][0]["reason_code"])
        self.assertEqual("PASS", resolver.validate(self.good_path, self.inventory)["status"])

    def observe_acceptance(self, graph, name):
        graph = copy.deepcopy(graph)
        graph["graph_sha256"] = resolver.sha256_json({k: v for k, v in graph.items() if k != "graph_sha256"})
        raw = json.dumps(graph, ensure_ascii=False)
        self.assertLess(len(raw.encode()), 65536)
        path = self.base / name
        path.write_text(raw, encoding="utf-8")
        self.assertEqual({"status": "PASS", "errors": []}, resolver.validate(path, self.inventory))

    def test_empty_self_consistent_graph_is_currently_accepted(self):
        graph = copy.deepcopy(self.good)
        graph.update(groups=[], nodes=[], edges=[], counts={"groups": 0, "resolved": 0, "needs_human_review": 0})
        self.observe_acceptance(graph, "empty.json")

    def test_fabricated_active_selection_is_currently_accepted(self):
        graph = copy.deepcopy(self.good)
        group = graph["groups"][0]
        group.update(status="resolved", selected_relative_path="Guide.csv", resolution_basis="automatic",
                     reason_code="fabricated_policy", conflicts=[])
        for candidate in group["candidates"]:
            candidate["disposition"] = "active" if candidate["relative_path"] == "Guide.csv" else "historical"
        graph["nodes"], graph["edges"] = resolver.graph_projection(graph["groups"])
        graph["counts"] = {"groups": 1, "resolved": 1, "needs_human_review": 0}
        self.observe_acceptance(graph, "selected.json")

    def test_absent_source_candidate_is_currently_accepted(self):
        graph = copy.deepcopy(self.good)
        group = graph["groups"][0]
        group["candidates"][0].update(relative_path="NeverInInventory.csv", source_sha256="0" * 64)
        group["candidate_set_sha256"] = resolver.candidate_set_hash(group["candidates"])
        group["conflicts"] = [c["relative_path"] for c in group["candidates"]]
        graph["nodes"], graph["edges"] = resolver.graph_projection(graph["groups"])
        self.observe_acceptance(graph, "absent.json")


if __name__ == "__main__":
    unittest.main(verbosity=2)
