"""F05b Phase A literal unit gold; new attest cases are post-API, never RED by absence."""
from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "distribution/macos-local-memory/engine/document_version_resolver.py"
SPEC = importlib.util.spec_from_file_location("f05b_attestation_resolver", SOURCE)
resolver = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(resolver)
SERIALIZED_BYTES = 0
ACTIVE_OPEN_WATCH = None


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def json_sha(value):
    return sha(canonical(value).encode("utf-8"))


def record(path, token="a"):
    return {"relative_path": path, "kind": "file", "read_status": "observed",
            "sha256": token * 64, "size_bytes": 1, "mtime_ns": 2, "birthtime_ns": 1}


def watch_opens(event, args):
    watch = ACTIVE_OPEN_WATCH
    if watch is None or event != "open" or not isinstance(args[0], (str, bytes, os.PathLike)):
        return
    path = os.path.abspath(os.fsdecode(args[0]))
    if "f05b-forbidden-authority" in path:
        raise AssertionError("graph metadata cannot choose an input path")
    if path not in watch:
        return
    mode, flags = args[1:3]
    writing = (isinstance(mode, str) and any(char in mode for char in "wax+")) or (
        isinstance(flags, int) and bool(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND)))
    if writing:
        raise AssertionError("attestation cannot write its inputs")
    watch[path] += 1
    if watch[path] != 1:
        raise AssertionError("second open would permit a different snapshot")


sys.addaudithook(watch_opens)


class DecisionSnapshotAttestationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="f05b-attestation-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.inventory = self.base / "inventory.jsonl"
        self.graph_path = self.base / "graph.json"
        self.snapshot = self.base / "decisions.snapshot.json"
        self.shared = self.base / "shared-decisions.json"
        self.source_before = SOURCE.read_bytes()
        self.addCleanup(lambda: self.assertEqual(self.source_before, SOURCE.read_bytes()))
        patcher = mock.patch.object(resolver, "atomic_json", side_effect=self.atomic)
        patcher.start()
        self.addCleanup(patcher.stop)

    def write(self, path, raw, cap):
        global SERIALIZED_BYTES
        raw = raw.encode("utf-8") if isinstance(raw, str) else raw
        self.assertLessEqual(len(raw), cap)
        SERIALIZED_BYTES += len(raw)
        self.assertLessEqual(SERIALIZED_BYTES, 1048576)
        path.write_bytes(raw)

    def atomic(self, path, value):
        self.write(path, canonical(value), 65536 if path == self.graph_path else 8192)

    def write_inventory(self, records):
        self.assertLessEqual(len(records), 16)
        self.write(self.inventory, "".join(canonical(item) + "\n" for item in records), 16384)

    def fixture(self, records=None, *, decision_path=None):
        records = records or [record("Guide_ver1.csv"), record("Guide.csv", "b")]
        self.write_inventory(records)
        return resolver.build(self.inventory, self.graph_path, decision_path)

    def empty_snapshot(self):
        self.write(self.snapshot, b'{"schema_version":"1.0","decisions":[]}\n', 8192)
        return self.fixture(decision_path=self.snapshot)

    def human_snapshot(self, selected="Guide.csv"):
        initial = self.fixture()
        group = initial["groups"][0]
        selected_hash = "b" * 64 if selected == "Guide.csv" else "a" * 64
        decision = {"group_id": group["group_id"], "candidate_set_sha256": group["candidate_set_sha256"],
                    "selected_relative_path": selected, "selected_source_sha256": selected_hash,
                    "decided_by": "synthetic-human", "decided_at": "2026-09-09T00:00:00+00:00"}
        self.atomic(self.snapshot, {"schema_version": "1.0", "decisions": [decision]})
        return resolver.build(self.inventory, self.graph_path, self.snapshot)

    def reseal(self, graph):
        graph = copy.deepcopy(graph)
        graph["graph_sha256"] = json_sha({key: value for key, value in graph.items() if key != "graph_sha256"})
        self.atomic(self.graph_path, graph)
        return graph

    def attest(self, mode="snapshot", *, path=None, expected=None):
        kwargs = {"decision_mode": mode}
        if mode == "snapshot":
            kwargs.update(decisions_path=self.snapshot if path is None else path,
                          expected_decisions_sha256=sha(self.snapshot.read_bytes()) if expected is None else expected)
        elif path is not None:
            kwargs["decisions_path"] = path
        if mode != "snapshot" and expected is not None:
            kwargs["expected_decisions_sha256"] = expected
        return self.invoke(kwargs)

    def invoke(self, kwargs):
        before = {path: path.read_bytes() for path in (self.inventory, self.graph_path, self.snapshot, self.shared) if path.exists()}
        with mock.patch.object(resolver, "build", side_effect=AssertionError("attest cannot build")), mock.patch.object(resolver, "atomic_json", side_effect=AssertionError("attest cannot write")), mock.patch.object(resolver, "record_decision", side_effect=AssertionError("attest cannot record decisions")):
            report = resolver.attest(self.graph_path, self.inventory, **kwargs)
        self.assertEqual(before, {path: path.read_bytes() for path in before})
        return report

    def failure(self, report):
        self.assertEqual({"status", "errors"}, set(report))
        self.assertEqual("FAIL", report["status"])
        self.assertTrue(report["errors"])
        self.assertTrue(all(isinstance(error, str) and error for error in report["errors"]))

    def exact_success(self, report, *, mode="snapshot", selected=None, reason="unmarked_candidate_requires_human_review"):
        self.assertEqual({"status", "errors", "inventory", "version", "document_version_graph"}, set(report))
        self.assertEqual("PASS", report["status"])
        self.assertEqual([], report["errors"])
        self.assertEqual({"sha256", "records"}, set(report["inventory"]))
        raw_inventory = self.inventory.read_bytes()
        self.assertEqual(sha(raw_inventory), report["inventory"]["sha256"])
        self.assertEqual([json.loads(line) for line in raw_inventory.split(b"\n") if line], report["inventory"]["records"])
        self.assertEqual({"groups", "dispositions"}, set(report["version"]))
        group = report["version"]["groups"][0]
        self.assertEqual(["Guide.csv", "Guide_ver1.csv"], [item["relative_path"] for item in group["candidates"]])
        self.assertEqual(selected, group["selected_relative_path"])
        self.assertEqual(reason, group["reason_code"])
        self.assertEqual("resolved" if selected else "needs_human_review", group["status"])
        self.assertEqual("human" if selected else None, group["resolution_basis"])
        expected_dispositions = {path: ("active" if path == selected else "historical" if selected else "needs_human_review") for path in ("Guide.csv", "Guide_ver1.csv")}
        self.assertEqual(expected_dispositions, report["version"]["dispositions"])
        self.assertEqual(expected_dispositions, {item["relative_path"]: item["disposition"] for item in group["candidates"]})
        binding = report["document_version_graph"]
        self.assertEqual({"path", "sha256", "graph_sha256", "decision_authority"}, set(binding))
        self.assertEqual(str(self.graph_path), binding["path"])
        self.assertEqual(sha(self.graph_path.read_bytes()), binding["sha256"])
        stored = json.loads(self.graph_path.read_bytes())
        self.assertEqual(json_sha({key: value for key, value in stored.items() if key != "graph_sha256"}), binding["graph_sha256"])
        authority = {"mode": mode}
        if mode == "snapshot":
            raw = self.snapshot.read_bytes()
            authority.update(path=str(self.snapshot), sha256=sha(raw), byte_count=len(raw))
        self.assertEqual(authority, binding["decision_authority"])

    def test_no_decisions_returns_exact_detached_payload(self):
        self.fixture()
        self.exact_success(self.attest("no_decisions"), mode="no_decisions")

    def test_snapshot_empty_payload_is_real_authority(self):
        self.empty_snapshot()
        self.exact_success(self.attest())

    def test_snapshot_human_either_member_has_exact_bound_payload(self):
        for selected in ("Guide.csv", "Guide_ver1.csv"):
            with self.subTest(selected=selected):
                self.human_snapshot(selected)
                self.exact_success(self.attest(), selected=selected, reason="human_confirmed_active")

    def test_snapshot_d0_survives_shared_d1_corruption_and_absence(self):
        self.human_snapshot("Guide.csv")
        capture_digest = sha(self.snapshot.read_bytes())
        d1 = json.loads(self.snapshot.read_bytes())
        d1["decisions"][0].update(selected_relative_path="Guide_ver1.csv", selected_source_sha256="a" * 64)
        for state in ("d1", "corrupt", "absent"):
            with self.subTest(state=state):
                if state == "d1":
                    self.atomic(self.shared, d1)
                elif state == "corrupt":
                    self.write(self.shared, b'{broken', 8192)
                else:
                    self.shared.unlink()
                self.exact_success(self.attest(expected=capture_digest), selected="Guide.csv", reason="human_confirmed_active")

    def test_next_explicit_snapshot_uses_d1_without_mutating_d0_result(self):
        self.human_snapshot("Guide.csv")
        d0 = self.attest()
        d0_value = copy.deepcopy(d0)
        d0_files = {path: path.read_bytes() for path in (self.snapshot, self.graph_path)}
        self.snapshot = self.base / "d1.snapshot.json"
        self.graph_path = self.base / "d1.graph.json"
        self.human_snapshot("Guide_ver1.csv")
        d1 = self.attest()
        self.exact_success(d1, selected="Guide_ver1.csv", reason="human_confirmed_active")
        self.assertEqual(d0_value, d0)
        self.assertEqual(d0_files, {path: path.read_bytes() for path in d0_files})
        self.assertEqual("Guide.csv", d0["version"]["groups"][0]["selected_relative_path"])
        self.assertNotEqual(d0["document_version_graph"]["decision_authority"]["sha256"], d1["document_version_graph"]["decision_authority"]["sha256"])

    def test_snapshot_missing_file_fails_without_legacy_fallback(self):
        self.empty_snapshot()
        expected = sha(self.snapshot.read_bytes())
        self.snapshot.unlink()
        self.atomic(self.shared, {"decisions": []})
        self.failure(self.attest(expected=expected))

    def test_snapshot_changed_bytes_fail_against_capture_digest(self):
        graph = self.empty_snapshot()
        expected = sha(self.snapshot.read_bytes())
        self.write(self.snapshot, self.snapshot.read_bytes() + b" ", 8192)
        graph["source"]["decisions_sha256"] = sha(self.snapshot.read_bytes())
        self.reseal(graph)
        self.failure(self.attest(expected=expected))

    def test_snapshot_wrong_expected_digest_fails_even_with_valid_graph(self):
        self.human_snapshot()
        self.failure(self.attest(expected="0" * 64))

    def test_snapshot_mode_requires_complete_typed_authority(self):
        self.empty_snapshot()
        good_hash = sha(self.snapshot.read_bytes())
        cases = [
            {"decision_mode": "snapshot"},
            {"decision_mode": "snapshot", "decisions_path": self.snapshot},
            {"decision_mode": "snapshot", "expected_decisions_sha256": good_hash},
            {"decision_mode": "snapshot", "decisions_path": None, "expected_decisions_sha256": good_hash},
            {"decision_mode": "snapshot", "decisions_path": self.snapshot, "expected_decisions_sha256": None},
            {"decision_mode": "snapshot", "decisions_path": self.snapshot, "expected_decisions_sha256": "short"},
            {"decision_mode": "snapshot", "decisions_path": self.snapshot, "expected_decisions_sha256": True},
        ]
        for kwargs in cases:
            with self.subTest(kwargs=kwargs):
                self.failure(self.invoke(kwargs))

    def test_no_decisions_forbids_external_authority_inputs(self):
        self.fixture()
        for kwargs in ({"decision_mode": "no_decisions", "decisions_path": self.snapshot},
                       {"decision_mode": "no_decisions", "expected_decisions_sha256": "0" * 64}):
            with self.subTest(kwargs=kwargs):
                self.failure(self.invoke(kwargs))

    def test_unknown_modes_fail_without_partial_payload(self):
        self.fixture()
        for mode in (None, "", "automatic", True, 1):
            with self.subTest(mode=mode):
                self.failure(self.invoke({"decision_mode": mode}))

    def test_no_decisions_rejects_graph_with_human_digest(self):
        self.human_snapshot()
        self.failure(self.attest("no_decisions"))

    def test_failure_has_no_inventory_groups_or_binding(self):
        graph = self.empty_snapshot()
        graph.update(groups=[], nodes=[], edges=[], counts={"groups": 0, "resolved": 0, "needs_human_review": 0})
        self.reseal(graph)
        self.failure(self.attest())

    def test_duplicate_or_nonfinite_snapshot_is_not_empty_fallback(self):
        graph = self.empty_snapshot()
        for raw in (b'{"decisions":[],"decisions":[]}', b'{"decisions":[],"unused":NaN}',
                    b'{"decisions":[],"unused":1e309}', b'{"decisions":{}}', b'\xff'):
            with self.subTest(raw=raw):
                self.write(self.snapshot, raw, 8192)
                graph["source"]["decisions_sha256"] = sha(raw)
                self.reseal(graph)
                self.failure(self.attest(expected=sha(raw)))

    def test_duplicate_decision_ids_fail_before_collapse(self):
        graph = self.human_snapshot()
        value = json.loads(self.snapshot.read_bytes())
        value["decisions"].append(copy.deepcopy(value["decisions"][0]))
        self.atomic(self.snapshot, value)
        graph["source"]["decisions_sha256"] = sha(self.snapshot.read_bytes())
        self.reseal(graph)
        self.failure(self.attest())

    def test_snapshot_small_injected_capacity_boundary(self):
        self.assertEqual(67108864, resolver.MAX_DECISION_SNAPSHOT_BYTES)
        envelope = b'{"decisions":[],"padding":""}\n'
        with mock.patch.object(resolver, "MAX_DECISION_SNAPSHOT_BYTES", 128):
            for size in (127, 128, 129):
                with self.subTest(size=size):
                    raw = b'{"decisions":[],"padding":"' + b"x" * (size - len(envelope)) + b'"}\n'
                    self.assertEqual(size, len(raw))
                    self.write(self.snapshot, raw, 8192)
                    self.fixture(decision_path=self.snapshot)
                    result = self.attest(expected=sha(raw))
                    if size > 128:
                        self.failure(result)
                        self.assertTrue(any("decision_snapshot_too_large" in error for error in result["errors"]))
                    else:
                        self.exact_success(result)

    def test_snapshot_reads_each_explicit_input_once_and_returns_those_bytes(self):
        global ACTIVE_OPEN_WATCH
        self.human_snapshot()
        original = {path: path.read_bytes() for path in (self.graph_path, self.inventory, self.snapshot)}
        counts = {str(path): 0 for path in original}
        ACTIVE_OPEN_WATCH = counts
        try:
            with mock.patch.object(resolver, "build", side_effect=AssertionError("cannot rebuild to validate")), mock.patch.object(resolver, "atomic_json", side_effect=AssertionError("cannot write to validate")):
                report = resolver.attest(self.graph_path, self.inventory, decision_mode="snapshot",
                                         decisions_path=self.snapshot, expected_decisions_sha256=sha(original[self.snapshot]))
        finally:
            ACTIVE_OPEN_WATCH = None
        self.assertEqual({str(path): 1 for path in original}, counts)
        self.exact_success(report, selected="Guide.csv", reason="human_confirmed_active")
        self.assertEqual(sha(original[self.inventory]), report["inventory"]["sha256"])
        self.assertEqual(sha(original[self.graph_path]), report["document_version_graph"]["sha256"])

    def test_metadata_canaries_never_choose_paths_or_returned_authority(self):
        graph = self.human_snapshot()
        graph["source"]["inventory_path"] = "/f05b-forbidden-authority/inventory.jsonl"
        graph["source"]["decisions_path"] = "/f05b-forbidden-authority/decisions.json"
        self.reseal(graph)
        def guarded(function):
            def call(path, *args, **kwargs):
                if isinstance(path, (str, bytes, os.PathLike)):
                    self.assertNotIn("f05b-forbidden-authority", os.fsdecode(path))
                return function(path, *args, **kwargs)
            return call
        with mock.patch.object(os, "stat", guarded(os.stat)), mock.patch.object(os, "lstat", guarded(os.lstat)), mock.patch.object(Path, "open", guarded(Path.open)), mock.patch.object(Path, "resolve", guarded(Path.resolve)):
            report = self.attest()
        self.exact_success(report, selected="Guide.csv", reason="human_confirmed_active")

    def test_returned_payload_cannot_change_later_attestation(self):
        self.human_snapshot()
        first = self.attest()
        expected = copy.deepcopy(first)
        first["inventory"]["records"][0]["sha256"] = "0" * 64
        first["version"]["groups"].clear()
        first["version"]["dispositions"].clear()
        first["document_version_graph"]["decision_authority"]["sha256"] = "0" * 64
        self.assertEqual(expected, self.attest())

    def test_changed_selected_or_peer_source_keeps_stale_hold(self):
        for changed in (0, 1):
            with self.subTest(changed=changed):
                self.human_snapshot()
                capture_hash = sha(self.snapshot.read_bytes())
                records = [record("Guide_ver1.csv"), record("Guide.csv", "b")]
                records[changed]["sha256"] = "c" * 64
                self.write_inventory(records)
                resolver.build(self.inventory, self.graph_path, self.snapshot)
                self.exact_success(self.attest(expected=capture_hash), reason="stale_human_decision")

    def test_new_candidate_invalidates_prior_human_choice(self):
        self.human_snapshot()
        expected = sha(self.snapshot.read_bytes())
        self.write_inventory([record("Guide_ver1.csv"), record("Guide.csv", "b"), record("Guide_ver2.csv", "c")])
        resolver.build(self.inventory, self.graph_path, self.snapshot)
        report = self.attest(expected=expected)
        self.assertEqual("PASS", report["status"])
        self.assertEqual("stale_human_decision", report["version"]["groups"][0]["reason_code"])
        self.assertIsNone(report["version"]["groups"][0]["selected_relative_path"])
        self.assertEqual({"Guide.csv": "needs_human_review", "Guide_ver1.csv": "needs_human_review", "Guide_ver2.csv": "needs_human_review"}, report["version"]["dispositions"])

    def test_legacy_validate_no_decisions_keeps_status_only_shape(self):
        self.fixture()
        self.assertEqual({"status": "PASS", "errors": []}, resolver.validate(self.graph_path, self.inventory))

    def test_explicit_decisions_present_has_exact_internal_authority(self):
        self.human_snapshot()
        raw = self.snapshot.read_bytes()
        report = self.attest("explicit_decisions", path=self.snapshot)
        self.assertEqual("PASS", report["status"])
        self.assertEqual({"mode": "explicit_decisions", "path": str(self.snapshot), "sha256": sha(raw), "byte_count": len(raw)}, report["document_version_graph"]["decision_authority"])
        self.assertEqual("Guide.csv", report["version"]["groups"][0]["selected_relative_path"])

    def test_explicit_decisions_missing_has_exact_absence_authority(self):
        missing = self.base / "missing.json"
        self.fixture(decision_path=missing)
        report = self.attest("explicit_decisions", path=missing)
        self.assertEqual("PASS", report["status"])
        self.assertEqual({"mode": "explicit_decisions", "path": str(missing), "sha256": None, "byte_count": 0}, report["document_version_graph"]["decision_authority"])
        self.assertEqual("unmarked_candidate_requires_human_review", report["version"]["groups"][0]["reason_code"])

    def test_legacy_validate_explicit_missing_keeps_empty_semantics(self):
        missing = self.base / "missing.json"
        self.fixture(decision_path=missing)
        self.assertEqual({"status": "PASS", "errors": []}, resolver.validate(self.graph_path, self.inventory, missing))

    def test_legacy_validate_human_explicit_input_keeps_status_only_shape(self):
        self.human_snapshot()
        self.assertEqual({"status": "PASS", "errors": []}, resolver.validate(self.graph_path, self.inventory, self.snapshot))

    def test_snapshot_symlink_directory_and_dangling_link_fail_read_only(self):
        self.empty_snapshot()
        raw = self.snapshot.read_bytes()
        expected = sha(raw)
        for kind in ("symlink", "directory", "dangling"):
            with self.subTest(kind=kind):
                path = self.base / kind
                if kind == "directory":
                    path.mkdir()
                else:
                    path.symlink_to(self.snapshot if kind == "symlink" else self.base / "absent")
                before = path.lstat()
                self.failure(resolver.attest(self.graph_path, self.inventory, decision_mode="snapshot",
                                             decisions_path=path, expected_decisions_sha256=expected))
                self.assertEqual(before, path.lstat())
                self.assertEqual(raw, self.snapshot.read_bytes())

    def test_late_input_substitution_cannot_replace_verified_snapshot_payload(self):
        self.human_snapshot()
        original = {path: path.read_bytes() for path in (self.graph_path, self.inventory, self.snapshot)}
        expected = resolver.attest(self.graph_path, self.inventory, decision_mode="snapshot",
                                   decisions_path=self.snapshot, expected_decisions_sha256=sha(original[self.snapshot]))
        self.exact_success(expected, selected="Guide.csv", reason="human_confirmed_active")
        reconstruct = resolver._validation_components
        substituted = []
        def swap_after_all_reads(records, decisions):
            substituted.append(True)
            self.write_inventory([record("Contact.txt", "c")])
            self.write(self.graph_path, b'{"synthetic_replacement":true}', 65536)
            self.write(self.snapshot, b'{"decisions":[]}\n', 8192)
            return reconstruct(records, decisions)
        with mock.patch.object(resolver, "_validation_components", side_effect=swap_after_all_reads):
            report = resolver.attest(self.graph_path, self.inventory, decision_mode="snapshot",
                                     decisions_path=self.snapshot, expected_decisions_sha256=sha(original[self.snapshot]))
        self.assertEqual([True], substituted)
        self.assertEqual(expected, report)
        self.assertEqual(sha(original[self.inventory]), report["inventory"]["sha256"])
        self.assertEqual("Guide.csv", report["version"]["groups"][0]["selected_relative_path"])
        self.assertTrue(all(path.read_bytes() != raw for path, raw in original.items()))


if __name__ == "__main__":
    unittest.main(verbosity=2)
