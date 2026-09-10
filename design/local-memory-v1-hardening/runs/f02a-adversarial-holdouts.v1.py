"""Independent F02a holdouts; run only against the root-frozen artifact.

Synthetic fixtures only. The frozen F04a worker supplies file/network/process
guards and a real in-process Reader/index dispatcher with model stubs. The
outer supervisor limits each run to 30 seconds and 1 MiB output. Current-marker
and undiscovered-candidate observations are residual witnesses, not acceptance
of independent annual retention. This is not an OS sandbox certification.
"""
from __future__ import annotations

import contextlib
import copy
import hashlib
import importlib.util
import itertools
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[3]
RUNS = ROOT / "design/local-memory-v1-hardening/runs"
ENGINE = ROOT / "distribution/macos-local-memory/engine"
LIMIT = 1048576


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


if __name__ != "__main__":
    resolver = load(ENGINE / "document_version_resolver.py", "f02a_holdout_resolver")
    builder = load(ENGINE / "build_adaptive_semantic_graph.py", "f02a_holdout_reader")
    app_fixture_module = sys.modules["f04a_isolated"]


def record(path):
    return {"kind": "file", "read_status": "observed", "relative_path": path,
            "sha256": hashlib.sha256(path.encode()).hexdigest(), "size_bytes": 9,
            "mtime_ns": 7, "birthtime_ns": 3}


def resolve(paths, decision=None):
    records = [record(path) if isinstance(path, str) else copy.deepcopy(path) for path in paths]
    candidates = [resolver.candidate(item) for item in records]
    if not all(item is not None for item in candidates):
        raise AssertionError("holdout_candidate_discovery_changed")
    keys = {resolver.family_key(item["relative_path"]) for item in candidates}
    if len(keys) != 1 or len(json.dumps(records).encode()) > LIMIT:
        raise AssertionError("holdout_fixture_invalid")
    return resolver.resolve_group(keys.pop(), candidates, decision)


class F02aIndependentPureHoldouts(unittest.TestCase):
    def held(self, paths, group, reason="year_order_does_not_establish_supersession"):
        expected = {path if isinstance(path, str) else path["relative_path"] for path in paths}
        self.assertEqual("needs_human_review", group["status"])
        self.assertEqual(reason, group["reason_code"])
        self.assertIsNone(group["selected_relative_path"])
        self.assertIsNone(group["resolution_basis"])
        self.assertEqual(expected, {item["relative_path"] for item in group["candidates"]})
        self.assertEqual(len(paths), len(group["candidates"]))
        self.assertTrue(all(item["disposition"] == "needs_human_review" for item in group["candidates"]))
        nodes, edges = resolver.graph_projection([group])
        self.assertEqual(expected, {item["relative_path"] for item in nodes if item["node_type"] == "document_version"})
        self.assertTrue(all(item["status"] == "needs_human_review" for item in nodes))
        self.assertEqual(len(paths), len(edges))
        self.assertTrue(all(item["edge_type"] == "candidate_for" and item["status"] == "candidate" for item in edges))
        if reason == "year_order_does_not_establish_supersession":
            self.assertEqual(expected, set(group["conflicts"]))

    def test_three_periods_every_order_preserves_unknown_relationship(self):
        paths = ["部門/集計2003.csv", "部門/集計2020.csv", "部門/集計2041.csv"]
        reference = None
        for permutation in itertools.permutations(paths):
            group = resolve(permutation)
            self.held(paths, group)
            value = (group, resolver.graph_projection([group]))
            if reference is None:
                reference = value
            self.assertEqual(reference, value)

    def test_directory_fullwidth_year_and_vocabulary_do_not_select(self):
        cases = [
            ["２０２１年/計測.xlsx", "２０３７年/計測.xlsx"],
            ["監査/名簿2000.pdf", "監査/名簿2099.pdf"],
            ["報告2011_版.csv", "報告2050_版.csv"],
        ]
        for paths in cases:
            with self.subTest(paths=paths):
                self.held(paths, resolve(paths))

    def test_reverse_numeric_versions_cannot_receive_year_branch_fallthrough(self):
        paths = ["規約2008_ver20.5.csv", "規約2031_ver2.10.csv", "規約2040_ver1.9.csv"]
        group = resolve(paths)
        self.held(paths, group)
        self.assertTrue(all(item["explicit_versions"] for item in group["candidates"]))

    def test_multiple_year_tokens_with_unique_maximum_remain_held(self):
        paths = ["対象2020-2022.csv", "対象2023-2030.csv"]
        self.held(paths, resolve(paths))

    def test_latest_historical_draft_and_tie_retain_existing_reasons(self):
        for paths, reason in (
            (["基準2010.csv", "旧版/基準2045.csv"], "latest_year_marked_historical"),
            (["基準2010.csv", "編集中/基準2045.csv"], "latest_year_is_draft"),
            (["基準2045.csv", "基準2045_コピー.csv", "基準2010.csv"], "latest_year_not_unique"),
        ):
            with self.subTest(reason=reason):
                self.held(paths, resolve(paths), reason)

    def test_hash_bound_oldest_human_choice_survives_candidate_order(self):
        paths = ["内訳2004.csv", "内訳2024.csv", "内訳2044.csv"]
        group = resolve(paths)
        chosen = next(item for item in group["candidates"] if item["relative_path"] == paths[0])
        decision = {"candidate_set_sha256": group["candidate_set_sha256"],
                    "selected_relative_path": chosen["relative_path"],
                    "selected_source_sha256": chosen["source_sha256"]}
        selected = resolve(list(reversed(paths)), decision)
        self.assertEqual(paths[0], selected["selected_relative_path"])
        self.assertEqual("human", selected["resolution_basis"])
        self.assertEqual("human_confirmed_active", selected["reason_code"])
        self.assertEqual(1, sum(item["edge_type"] == "active_version" for item in resolver.graph_projection([selected])[1]))
        self.assertEqual(2, sum(item["disposition"] == "historical" for item in selected["candidates"]))

    def test_selected_nonselected_hash_membership_and_decision_hash_stale(self):
        paths = ["点検2012.csv", "点検2023.csv", "点検2034.csv"]
        initial = resolve(paths)
        chosen = initial["candidates"][0]
        decision = {"candidate_set_sha256": initial["candidate_set_sha256"],
                    "selected_relative_path": chosen["relative_path"],
                    "selected_source_sha256": chosen["source_sha256"]}
        for index in range(3):
            values = [record(path) for path in paths]
            values[index]["sha256"] = "f" * 64
            self.held(values, resolve(values, decision), "stale_human_decision")
        added = [*paths, "点検2045.csv"]
        self.held(added, resolve(added, decision), "stale_human_decision")
        remaining = paths[:2]
        self.held(remaining, resolve(remaining, decision), "stale_human_decision")
        self.held(paths, resolve(paths, {**decision, "selected_source_sha256": "0" * 64}), "stale_human_decision")

    def test_numeric_and_current_positive_controls_remain(self):
        cases = [
            (["標準_ver9.99.csv", "標準_ver10.1.csv"], "標準_ver10.1.csv", "unique_latest_explicit_version"),
            (["旧版/標準.csv", "現行/標準.csv"], "現行/標準.csv", "unique_explicit_current_marker"),
            (["標準_ver1.9.csv", "現行/標準_ver1.10.csv"], "現行/標準_ver1.10.csv", "unique_explicit_current_marker"),
        ]
        for paths, selected, reason in cases:
            group = resolve(paths)
            self.assertEqual("resolved", group["status"])
            self.assertEqual(selected, group["selected_relative_path"])
            self.assertEqual(reason, group["reason_code"])

    def test_f04a_and_current_year_conflict_precedence_remain(self):
        cases = [
            (["現行/約款_ver1.9.csv", "下書き/約款_ver2.1.csv"], "current_marker_conflicts_with_latest_version"),
            (["現行/約款_ver1.9.csv", "旧版/約款_ver2.1.csv"], "current_marker_conflicts_with_latest_version"),
            (["現行/約款2010_ver4.csv", "約款2030_ver2.csv"], "current_marker_conflicts_with_latest_year"),
            (["現行/約款2010.csv", "承認済み/約款2030.csv"], "multiple_current_markers"),
        ]
        for paths, reason in cases:
            self.held(paths, resolve(paths), reason)

    def test_reader_policy_holds_three_years_without_mutating_metadata(self):
        paths = ["証跡2015.csv", "証跡2025.csv", "証跡2035.csv"]
        group = resolve(paths)
        self.held(paths, group)
        records = [record(path) for path in paths] + [record("窓口.txt")]
        before = copy.deepcopy(records)
        core = {"groups": [group], "source": {"inventory_sha256": "a" * 64}}
        graph = {**core, "graph_sha256": resolver.sha256_json(core)}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "version.json"
            path.write_text(json.dumps(graph), encoding="utf-8")
            eligible, counts, binding = builder.apply_document_version_policy(records, path, "a" * 64)
        self.assertEqual([records[-1]], eligible)
        self.assertEqual(3, counts.get("version_needs_human_review", 0))
        self.assertEqual(0, counts.get("version_active", 0))
        self.assertEqual(0, counts.get("version_historical", 0))
        self.assertEqual(1, counts.get("version_ungrouped", 0))
        self.assertEqual(graph["graph_sha256"], binding["graph_sha256"])
        self.assertEqual(before, records)


class F02aIndependentAppHoldouts(unittest.TestCase):
    def setUp(self):
        self.fixture = app_fixture_module.VersionedSafeIndexE2E()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def seed(self, files):
        self.assertLess(sum(len(value.encode()) for value in files.values()), LIMIT)
        for name, content in files.items():
            path = self.fixture.source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")

    def snapshot(self):
        return {str(path.relative_to(self.fixture.source)): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in self.fixture.source.rglob("*") if path.is_file()}

    def published(self):
        fixture = self.fixture
        config = fixture.bootstrap.load_json(fixture.bootstrap.CONFIG)
        graph = fixture.bootstrap.load_json(Path(config["path_graph_path"]) / "document-version-graph.json")
        with contextlib.closing(sqlite3.connect(config["index_path"])) as connection:
            paths = {row[0] for row in connection.execute("SELECT relative_path FROM evidence")}
            metadata = {key: json.loads(value) for key, value in connection.execute("SELECT key,value FROM metadata")}
        self.assertEqual(graph["graph_sha256"], metadata["document_version_graph"]["graph_sha256"])
        self.assertEqual("current", fixture.bootstrap.reader_generation_contract_status(config)["state"])
        return config, graph, paths

    def test_three_year_directories_publish_only_unrelated_evidence(self):
        self.seed({"2014年/集計.csv": "task,owner\na,alpha\n",
                   "2027年/集計.csv": "task,owner\nb,beta\n",
                   "2042年/集計.csv": "task,owner\nc,gamma\n",
                   "窓口.txt": "front desk contact: lobby\n"})
        before = self.snapshot()
        self.fixture.bootstrap.build_index()
        config, graph, paths = self.published()
        self.assertEqual({"窓口.txt"}, paths)
        self.assertEqual({"groups": 1, "resolved": 0, "needs_human_review": 1}, graph["counts"])
        group = graph["groups"][0]
        self.assertEqual(3, len(group["candidates"]))
        self.assertEqual("year_order_does_not_establish_supersession", group["reason_code"])
        self.assertTrue(all(item["disposition"] == "needs_human_review" for item in group["candidates"]))
        self.assertFalse(any(item["edge_type"] == "active_version" for item in graph["edges"]))
        reader = self.fixture.bootstrap.load_json(Path(config["semantic_path"]) / "adaptive-reader-state.json")
        self.assertEqual(3, reader["limitations"]["version_files_needing_human_review"])
        self.assertEqual(0, reader["limitations"]["historical_version_files_held"])
        self.assertEqual(before, self.snapshot())
        self.assertEqual([], self.fixture.model_requests)

    def test_all_held_failure_preserves_previous_config_and_index(self):
        self.seed({"確認_ver1.9.csv": "task,owner\na,alpha\n", "確認_ver1.10.csv": "task,owner\nb,beta\n"})
        fixture = self.fixture
        fixture.bootstrap.build_index()
        config, _graph, paths = self.published()
        self.assertEqual({"確認_ver1.10.csv"}, paths)
        config_before = fixture.bootstrap.CONFIG.read_bytes()
        index = Path(config["index_path"])
        index_before = hashlib.sha256(index.read_bytes()).hexdigest()
        (fixture.source / "確認_ver1.9.csv").rename(fixture.source / "確認2001.csv")
        (fixture.source / "確認_ver1.10.csv").rename(fixture.source / "確認2039.csv")
        self.seed({"確認2021.csv": "task,owner\nmiddle,delta\n"})
        before = self.snapshot()
        commands_before = len(fixture.commands)
        with self.assertRaisesRegex(SystemExit, "adaptive_reader_no_supported_files"):
            fixture.bootstrap.build_index()
        self.assertEqual(config_before, fixture.bootstrap.CONFIG.read_bytes())
        self.assertEqual(index_before, hashlib.sha256(index.read_bytes()).hexdigest())
        self.assertEqual(before, self.snapshot())
        self.assertFalse(any(Path(command[1]).name == "build_local_semantic_index.py" for command in fixture.commands[commands_before:]))
        self.assertEqual([], fixture.model_requests)

    def test_normal_answer_and_final_audit_are_still_source_bound(self):
        fixture = self.fixture
        result = fixture.application_query("業務内容に書かれた語句を教えてください")
        self.assertEqual("answered", result["answer"]["answer_status"])
        self.assertIn("bob", result["answer"]["answer"])
        self.assertEqual("accepted", result["orchestration_decision"]["status"])
        self.assertTrue(any(Path(command[1]).name == "final_answer_audit.py" for command in fixture.commands))
        self.assertNotIn("業務内容_ver1.csv", {item["relative_path"] for item in result["retrieved"]})


class F02aResidualWitnessesNotAcceptance(unittest.TestCase):
    def test_current_marker_can_still_supersede_across_years(self):
        paths = ["実績2013.csv", "現行/実績2033.csv"]
        group = resolve(paths)
        self.assertEqual("現行/実績2033.csv", group["selected_relative_path"])
        self.assertEqual("unique_explicit_current_marker", group["reason_code"])
        print(json.dumps({"residual": "current_marker_annual_identity_open", "acceptance_claim": False}), flush=True)

    def test_unsignalled_candidate_discovery_is_still_incomplete(self):
        self.assertIsNone(resolver.candidate(record("実績.csv")))
        self.assertIsNotNone(resolver.candidate(record("実績2033.csv")))
        print(json.dumps({"residual": "F03_unsignalled_candidate_discovery_open", "acceptance_claim": False}), flush=True)


def main():
    run_id = sys.argv[1]
    if not run_id.startswith("f02a-audit-holdouts-") or len(run_id) > 80:
        raise SystemExit("invalid F02a audit run id")
    if "--worker" in sys.argv:
        guard = load(RUNS / "f04a-executor-run.v1.py", "f02a_holdout_guard")
        original_load = guard.load
        def redirected(path, name):
            if path.name == "test_document_version_resolver.py":
                return original_load(Path(__file__).resolve(), "f02a_holdout_tests")
            return original_load(path, name)
        guard.load = redirected
        return guard.worker("resolver")
    supervisor = load(ROOT / "scripts/run_local_memory_hardening_tests.py", "f02a_holdout_supervisor")
    result = supervisor.run_bounded(
        [sys.executable, "-I", "-B", str(Path(__file__).resolve()), run_id, "--worker"],
        RUNS / run_id, cwd=ROOT, timeout_seconds=30, max_log_bytes=LIMIT,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
