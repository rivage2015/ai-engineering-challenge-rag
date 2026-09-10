"""Independent F04a probes: synthetic metadata only; no product edits.

Seven acceptance methods plus one observed out-of-scope limitation witness.
The outer audit runner bounds time/logs/files and denies network/process use.
"""
from __future__ import annotations

import copy
import hashlib
import importlib.util
import itertools
import json
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[3]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


resolver = load("f04a_independent_resolver", ROOT / "distribution/macos-local-memory/engine/document_version_resolver.py")
reader = load("f04a_independent_reader", ROOT / "distribution/macos-local-memory/engine/build_adaptive_semantic_graph.py")


def record(path):
    return {"kind": "file", "read_status": "observed", "relative_path": path,
            "sha256": hashlib.sha256(path.encode()).hexdigest(), "size_bytes": 7,
            "mtime_ns": 10, "birthtime_ns": 5}


def resolve(paths, decision=None):
    records = [record(path) if isinstance(path, str) else path for path in paths]
    candidates = [resolver.candidate(item) for item in records]
    assert all(item is not None for item in candidates)
    families = {resolver.family_key(item["relative_path"]) for item in records}
    assert len(families) == 1
    assert len(json.dumps(records).encode()) < 1048576
    return resolver.resolve_group(families.pop(), candidates, decision)


class IndependentF04aProbes(unittest.TestCase):
    def held(self, group, reason="current_marker_conflicts_with_latest_version"):
        self.assertEqual("needs_human_review", group["status"])
        self.assertIsNone(group["selected_relative_path"])
        self.assertIsNone(group["resolution_basis"])
        self.assertEqual(reason, group["reason_code"])
        self.assertTrue(all(item["disposition"] == "needs_human_review" for item in group["candidates"]))
        nodes, edges = resolver.graph_projection([group])
        self.assertTrue(all(node["status"] == "needs_human_review" for node in nodes))
        self.assertEqual({"candidate_for"}, {edge["edge_type"] for edge in edges})

    def test_numeric_normalization_and_component_comparison(self):
        cases = [
            ("現行/取扱_ver9.99.xlsx", "取扱_ver10.0.xlsx"),
            ("現行/取扱_ver001.09.xlsx", "取扱_ver1.010.xlsx"),
            ("現行/取扱_ＶＥＲ１．９.xlsx", "取扱_version1.10.xlsx"),
            ("現行/取扱_ver1_2.xlsx", "取扱_ver1-10.xlsx"),
            ("現行/取扱_ver1.2.9.xlsx", "取扱_ver1.2.10.xlsx"),
        ]
        for paths in cases:
            with self.subTest(paths=paths):
                self.held(resolve(paths))

    def test_three_peers_all_permutations_and_projection_identity(self):
        paths = ["現行/取扱_ver3.8.xlsx", "下書き/取扱_ver3.9.xlsx", "旧版/取扱_ver4.0.xlsx"]
        reference = None
        for permutation in itertools.permutations(paths):
            group = resolve(permutation)
            self.held(group)
            self.assertEqual(set(paths), set(group["conflicts"]))
            projection = resolver.graph_projection([group])
            value = (group, projection)
            if reference is None:
                reference = value
            self.assertEqual(reference, value)

    def test_equal_normalized_and_newer_current_remain_eligible(self):
        for other in ("取扱_ver001.010.xlsx", "取扱_ver1.9.xlsx"):
            group = resolve(["現行/取扱_ver1.10.xlsx", other])
            self.assertEqual("resolved", group["status"])
            self.assertEqual("現行/取扱_ver1.10.xlsx", group["selected_relative_path"])
            self.assertEqual("unique_explicit_current_marker", group["reason_code"])
            self.assertEqual(1, sum(edge["edge_type"] == "active_version" for edge in resolver.graph_projection([group])[1]))

    def test_both_human_choices_win_then_hash_or_membership_changes_hold(self):
        paths = ["現行/取扱_ver3.xlsx", "取扱_ver4.xlsx"]
        initial = resolve(paths)
        for chosen in initial["candidates"]:
            decision = {"candidate_set_sha256": initial["candidate_set_sha256"],
                        "selected_relative_path": chosen["relative_path"],
                        "selected_source_sha256": chosen["source_sha256"]}
            accepted = resolve(list(reversed(paths)), decision)
            self.assertEqual(chosen["relative_path"], accepted["selected_relative_path"])
            self.assertEqual("human", accepted["resolution_basis"])
            self.assertEqual("human_confirmed_active", accepted["reason_code"])
            for changed in (0, 1):
                records = [record(path) for path in paths]
                records[changed]["sha256"] = "0" * 64
                self.held(resolve(records, decision), "stale_human_decision")
            self.held(resolve([*paths, "取扱_ver2.xlsx"], decision), "stale_human_decision")
            wrong_digest = {**decision, "selected_source_sha256": "0" * 64}
            self.held(resolve(paths, wrong_digest), "stale_human_decision")

    def test_year_conflict_and_multiple_current_precedence(self):
        self.held(resolve(["現行/取扱2024_ver1.xlsx", "取扱2025_ver2.xlsx"]), "current_marker_conflicts_with_latest_year")
        self.held(resolve(["現行/取扱_ver1.xlsx", "承認済み/取扱_ver2.xlsx"]), "multiple_current_markers")

    def test_reader_excludes_all_held_candidates_but_keeps_unrelated(self):
        paths = ["現行/取扱_ver3.csv", "下書き/取扱_ver4.csv", "旧版/取扱_ver5.csv"]
        group = resolve(paths)
        self.held(group)
        records = [record(path) for path in paths]
        ungrouped = record("連絡先.txt")
        before = copy.deepcopy([*records, ungrouped])
        core = {"groups": [group], "source": {"inventory_sha256": "a" * 64}}
        graph = {**core, "graph_sha256": resolver.sha256_json(core)}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "graph.json"
            path.write_text(json.dumps(graph), encoding="utf-8")
            eligible, counts, binding = reader.apply_document_version_policy([*records, ungrouped], path, "a" * 64)
        self.assertEqual([ungrouped], eligible)
        self.assertEqual({"version_needs_human_review": 3, "version_ungrouped": 1}, counts)
        self.assertEqual(core["groups"][0]["candidate_set_sha256"], group["candidate_set_sha256"])
        self.assertEqual(graph["graph_sha256"], binding["graph_sha256"])
        self.assertEqual(before, [*records, ungrouped])

    def test_absent_current_retains_existing_numeric_order(self):
        group = resolve(["取扱_ver9.99.xlsx", "取扱_ver10.0.xlsx"])
        self.assertEqual("取扱_ver10.0.xlsx", group["selected_relative_path"])
        self.assertEqual("unique_latest_explicit_version", group["reason_code"])

    def test_observed_scope_limit_multiple_version_tokens_is_still_open(self):
        group = resolve(["現行/取扱_ver1_ver3.xlsx", "取扱_ver4.xlsx"])
        self.assertEqual("resolved", group["status"])
        self.assertEqual("現行/取扱_ver1_ver3.xlsx", group["selected_relative_path"])
        print(json.dumps({"scope_limit_witness": "multiple_version_tokens_not_fixed", "observed_status": group["status"], "reason": group["reason_code"], "acceptance_claim": False}), flush=True)


if __name__ == "__main__":
    unittest.main()
