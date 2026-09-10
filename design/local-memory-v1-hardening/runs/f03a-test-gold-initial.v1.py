#!/usr/bin/env python3
"""F03a immutable acceptance gold; pure synthetic resolver tests, not UI tests.

Actual resolver build is exercised with only IO boundaries and clock adapted
in memory. CLI execution denies runtime filesystem/network/process IO after
reviewed imports. Importing this module does not install a global audit hook.
ResidualWitnessTests must be reported separately from improvement acceptance.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import itertools
import json
import os
import re
import signal
import sys
import tempfile
import types
import typing
import unicodedata
import unittest
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCE_PATH = ROOT / "distribution/macos-local-memory/engine/document_version_resolver.py"
SOURCE_BYTES = SOURCE_PATH.read_bytes()
assert len(SOURCE_BYTES) <= 1048576
SOURCE_SHA256 = hashlib.sha256(SOURCE_BYTES).hexdigest()
resolver = types.ModuleType("unmarked_candidate_resolver")
resolver.__file__ = str(SOURCE_PATH)
exec(compile(SOURCE_BYTES, str(SOURCE_PATH), "exec"), resolver.__dict__)
FIXTURE_BYTES = 0
SIGNALS = ("explicit_years", "explicit_versions", "current_markers", "historical_markers", "draft_markers")


def record(path, token="a"):
    return {"relative_path": path, "kind": "file", "read_status": "observed",
            "sha256": token * 64, "size_bytes": 1, "mtime_ns": 2, "birthtime_ns": 1}


class MemoryPath:
    def __init__(self, name):
        self.name = name

    def resolve(self, **_kwargs):
        return self

    def exists(self):
        return True

    def __str__(self):
        return "/synthetic-unmarked-gold/" + self.name


class FrozenDateTime:
    @classmethod
    def now(cls):
        return cls()

    def astimezone(self):
        return self

    def isoformat(self, **_kwargs):
        return "2026-09-09T00:00:00+00:00"


def build(records, decisions=None):
    global FIXTURE_BYTES
    records = [dict(item) for item in records]
    decisions = dict(decisions or {})
    inventory_blob = "".join(resolver.canonical_json(item) + "\n" for item in records)
    decision_blob = resolver.canonical_json({"decisions": list(decisions.values())})
    FIXTURE_BYTES += len(inventory_blob.encode("utf-8")) + len(decision_blob.encode("utf-8"))
    assert FIXTURE_BYTES <= 1048576
    blobs = {"inventory": inventory_blob, "decisions": decision_blob}
    sink = []
    adapters = {
        "load_inventory": lambda _path: records,
        "load_decisions": lambda _path: decisions,
        "sha256_file": lambda path: hashlib.sha256(blobs[path.name].encode("utf-8")).hexdigest(),
        "atomic_json": lambda path, value: sink.append((path.name, value)),
        "datetime": FrozenDateTime,
    }
    originals = {name: getattr(resolver, name) for name in adapters}
    try:
        for name, replacement in adapters.items():
            setattr(resolver, name, replacement)
        result = resolver.build(MemoryPath("inventory"), MemoryPath("output"),
                                MemoryPath("decisions") if decisions else None)
        assert len(sink) == 1 and sink[0] == ("output", result)
        return result
    finally:
        for name, original in originals.items():
            setattr(resolver, name, original)


def decision_for(group, selected):
    item = next(candidate for candidate in group["candidates"] if candidate["relative_path"] == selected)
    return {group["group_id"]: {
        "group_id": group["group_id"], "candidate_set_sha256": group["candidate_set_sha256"],
        "selected_relative_path": selected, "selected_source_sha256": item["source_sha256"],
        "decided_by": "synthetic-human", "decided_at": "2026-09-09T00:00:00+00:00",
    }}


class UnmarkedCandidateTests(unittest.TestCase):
    def group(self, result):
        self.assertEqual(1, result["counts"]["groups"])
        self.assertEqual(1, len(result["groups"]))
        return result["groups"][0]

    def held(self, records, result, reason="unmarked_candidate_requires_human_review"):
        group = self.group(result)
        self.assertEqual("needs_human_review", group["status"])
        self.assertEqual(reason, group["reason_code"])
        self.assertIsNone(group["selected_relative_path"])
        self.assertIsNone(group["resolution_basis"])
        paths = {item["relative_path"] for item in records}
        self.assertEqual(len(records), len(group["candidates"]))
        self.assertEqual(paths, {item["relative_path"] for item in group["candidates"]})
        self.assertEqual({item["relative_path"]: item["sha256"] for item in records},
                         {item["relative_path"]: item["source_sha256"] for item in group["candidates"]})
        self.assertTrue(all(item["disposition"] == "needs_human_review" for item in group["candidates"]))
        if reason == "stale_human_decision":
            self.assertEqual(["candidate_set_changed"], group["conflicts"])
        elif reason == "unmarked_candidate_requires_human_review":
            self.assertEqual(paths, set(group["conflicts"]))
        self.assertFalse(any(edge["edge_type"] == "active_version" for edge in result["edges"]))
        candidate_edges = [edge for edge in result["edges"] if edge["edge_type"] == "candidate_for"]
        self.assertEqual(len(records), len(candidate_edges))
        self.assertTrue(all(edge["status"] == "candidate" and edge["basis"] == "normalized_filename_family" for edge in candidate_edges))
        nodes = [node for node in result["nodes"] if node["node_type"] == "document_version"]
        self.assertEqual(paths, {node["relative_path"] for node in nodes})
        self.assertTrue(all(node["status"] == "needs_human_review" for node in nodes))
        return group

    def test_unmarked_candidate_keeps_existing_shape(self):
        candidate = resolver.candidate(record("Notes/Procedure.xlsx"))
        self.assertIsNotNone(candidate)
        self.assertEqual({"relative_path", "source_sha256", "size_bytes", "mtime_ns", "birthtime_ns", *SIGNALS}, set(candidate))
        self.assertTrue(all(candidate[name] == [] for name in SIGNALS))
        self.assertEqual("a" * 64, candidate["source_sha256"])
        self.assertEqual("Notes/Procedure.xlsx", candidate["relative_path"])

    def test_existing_invalid_record_filters_stay_closed(self):
        variants = [{"kind": "directory"}, {"read_status": "failed"}, {"relative_path": "manual.txt"},
                    {"relative_path": None}, {"relative_path": ["manual.csv"]}, {"sha256": None}]
        for patch in variants:
            with self.subTest(patch=patch):
                self.assertIsNone(resolver.candidate({**record("manual.csv"), **patch}))
        missing = record("manual.csv")
        del missing["sha256"]
        self.assertIsNone(resolver.candidate(missing))

    def test_year_and_unmarked_are_held_without_annual_inference(self):
        for dated, plain in [("業務内容2024.xlsx", "業務内容.xlsx"),
                             ("記録/実績2024.csv", "記録/実績.csv"),
                             ("Reports/Memo2023.csv", "Reports/Memo.csv"),
                             ("2024/業務内容.xlsx", "業務内容.xlsx")]:
            records = [record(dated), record(plain, "b")]
            with self.subTest(paths=(dated, plain)):
                self.held(records, build(records))

    def test_numeric_and_unmarked_are_held(self):
        records = [record("Manual_ver1.10.csv"), record("Manual.csv", "b")]
        self.held(records, build(records))

    def test_current_historical_and_draft_cannot_demote_unmarked(self):
        for marked in ("現行/手順.csv", "旧版/手順.csv", "手順_下書き.csv", "current/手順.csv"):
            records = [record(marked), record("手順.csv", "b")]
            with self.subTest(marked=marked):
                self.held(records, build(records))

    def test_mixed_guard_precedes_existing_current_conflicts(self):
        records = [record("現行/手順_ver1.csv"), record("手順_ver2.csv", "b"), record("手順.csv", "c")]
        self.held(records, build(records))

    def test_three_member_permutations_preserve_full_projection_and_hash(self):
        records = [record("Notes/Manual_ver2.csv"), record("Notes/Manual.csv", "b"), record("Notes/Manual_copy.csv", "c")]
        baseline = build(records)
        self.held(records, baseline)
        for ordered in itertools.permutations(records):
            result = build(ordered)
            self.held(records, result)
            self.assertEqual({name: baseline[name] for name in ("groups", "nodes", "edges")},
                             {name: result[name] for name in ("groups", "nodes", "edges")})

    def test_all_unmarked_singleton_and_separate_families_stay_ungrouped(self):
        for paths in [("手順.csv", "手順コピー.csv"), ("業務_内容.xlsx", "業務内容.xlsx"),
                      ("手順2024.csv",), ("A/手順2024.csv", "B/手順.csv"),
                      ("手順2024.xlsx", "手順.pdf"), ("手順2024.csv", "連絡先.csv")]:
            result = build([record(path, chr(97 + index)) for index, path in enumerate(paths)])
            with self.subTest(paths=paths):
                self.assertEqual({"groups": 0, "resolved": 0, "needs_human_review": 0}, result["counts"])
                self.assertEqual([], result["groups"])
                self.assertEqual([], result["edges"])

    def test_unrelated_unmarked_peers_are_not_absorbed_into_mixed_group(self):
        mixed = [record("手順_ver1.csv"), record("手順.csv", "b")]
        others = [record("連絡先.csv", "c"), record("連絡先コピー.csv", "d"), record("A/手順.csv", "e")]
        self.held(mixed, build([*mixed, *others]))

    def test_all_marked_supported_controls_keep_exact_reasons(self):
        for paths, reason, selected in [
            (("実績2024.csv", "実績2025.csv"), "year_order_does_not_establish_supersession", None),
            (("現行/手順_ver1.csv", "手順_ver2.csv"), "current_marker_conflicts_with_latest_version", None),
            (("手順_ver1.2.csv", "手順_ver1.10.csv"), "unique_latest_explicit_version", "手順_ver1.10.csv"),
            (("旧版/手順.csv", "現行/手順.csv"), "unique_explicit_current_marker", "現行/手順.csv"),
        ]:
            result = build([record(paths[0]), record(paths[1], "b")])
            group = self.group(result)
            with self.subTest(paths=paths):
                self.assertEqual(reason, group["reason_code"])
                self.assertEqual(selected, group["selected_relative_path"])
                self.assertEqual(0 if selected is None else 1, sum(edge["edge_type"] == "active_version" for edge in result["edges"]))

    def test_existing_all_marked_candidate_hash_shape_is_unchanged(self):
        group = self.group(build([record("業務内容_ver1.xlsx"), record("業務内容_ver2.xlsx", "b")]))
        self.assertEqual("version_set_e8101fe4d635552c563e221d8cf1a5c1", group["group_id"])
        self.assertEqual("f09ae325919472e249904e42956f26daf812c7c2507810141417e3416538ac8e", group["candidate_set_sha256"])

    def test_full_set_human_can_choose_marked_or_unmarked(self):
        records = [record("現行/手順.csv"), record("手順.csv", "b")]
        initial = self.held(records, build(records))
        for selected in ("現行/手順.csv", "手順.csv"):
            result = build(records, decision_for(initial, selected))
            group = self.group(result)
            with self.subTest(selected=selected):
                self.assertEqual("resolved", group["status"])
                self.assertEqual("human", group["resolution_basis"])
                self.assertEqual("human_confirmed_active", group["reason_code"])
                self.assertEqual(selected, group["selected_relative_path"])
                self.assertEqual(initial["candidate_set_sha256"], group["candidate_set_sha256"])
                self.assertEqual(1, sum(item["disposition"] == "active" for item in group["candidates"]))
                self.assertEqual(["verified"], [edge["status"] for edge in result["edges"] if edge["edge_type"] == "active_version"])

    def test_later_unmarked_addition_stales_previously_all_marked_choice(self):
        records = [record("手順_ver1.csv"), record("手順_ver2.csv", "b")]
        initial = self.group(build(records))
        decision = decision_for(initial, "手順_ver1.csv")
        added = [*records, record("手順.csv", "c")]
        stale = self.held(added, build(added, decision), "stale_human_decision")
        self.assertEqual(initial["group_id"], stale["group_id"])
        self.assertNotEqual(initial["candidate_set_sha256"], stale["candidate_set_sha256"])

    def test_mixed_choice_stales_changes_additions_and_removals_in_formed_family(self):
        records = [record("現行/手順.csv"), record("手順.csv", "b"), record("手順コピー.csv", "c")]
        initial = self.held(records, build(records))
        decision = decision_for(initial, "現行/手順.csv")
        variants = []
        for index, field, value in [(0, "sha256", "d" * 64), (1, "sha256", "d" * 64),
                                    (2, "sha256", "d" * 64), (1, "mtime_ns", 3), (2, "birthtime_ns", 4)]:
            changed = [dict(item) for item in records]
            changed[index][field] = value
            variants.append(changed)
        variants.extend([[*records, record("手順_copy(2).csv", "d")], records[:2]])
        for changed in variants:
            self.held(changed, build(changed, decision), "stale_human_decision")

    def test_same_family_marked_to_unmarked_rename_stales_choice(self):
        records = [record("手順_ver1.csv"), record("手順_ver2.csv", "b")]
        initial = self.group(build(records))
        decision = decision_for(initial, "手順_ver1.csv")
        changed = [records[0], record("手順.csv", "b")]
        stale = self.held(changed, build(changed, decision), "stale_human_decision")
        self.assertEqual(initial["group_id"], stale["group_id"])

    def test_unrelated_addition_does_not_stale_valid_mixed_choice(self):
        records = [record("手順_ver1.csv"), record("手順.csv", "b")]
        initial = self.held(records, build(records))
        decision = decision_for(initial, "手順.csv")
        chosen = self.group(build(records, decision))
        augmented = self.group(build([*records, record("A/手順.csv", "c"), record("手順.pdf", "d")], decision))
        self.assertEqual(chosen, augmented)
        self.assertEqual("human", augmented["resolution_basis"])

    def test_resolver_identity_and_policy_disclose_new_guard(self):
        result = build([record("手順_ver1.csv"), record("手順.csv", "b")])
        self.assertEqual("0.1.3", result["resolver_version"])
        self.assertEqual("same_family_valid_documents_with_at_least_one_version_signal", result["policy"]["candidate_rule"])
        self.assertEqual("needs_human_review", result["policy"]["mixed_signal_action"])
        self.assertEqual("mixed_signal_hold_else_unique_explicit_current_without_year_or_single_version_conflict_else_hold_all_dated_else_comparable_latest_version", result["policy"]["automatic_rule"])
        self.assertIs(False, result["policy"]["year_order_establishes_supersession"])


class ResidualWitnessTests(unittest.TestCase):
    """These unchanged boundaries are not F03a improvement acceptance."""

    def test_cross_key_moves_renames_and_suffix_changes_leave_old_group_omitted(self):
        records = [record("手順_ver1.csv"), record("手順_ver2.csv", "b")]
        initial = build(records)["groups"][0]
        decision = decision_for(initial, "手順_ver1.csv")
        for new_path in ("部署B/手順_ver2.csv", "別資料_ver2.csv", "手順_ver2.pdf"):
            result = build([records[0], record(new_path, "b")], decision)
            self.assertEqual([], result["groups"])
        self.assertEqual([], build([record("手順.csv"), record("手順コピー.csv", "b")], decision)["groups"])

    def test_year_kanji_parent_is_not_silently_normalized_more_broadly(self):
        self.assertEqual("年\0業務内容\0.xlsx", resolver.family_key("2024年/業務内容.xlsx"))
        self.assertEqual("\0業務内容\0.xlsx", resolver.family_key("業務内容.xlsx"))
        self.assertEqual([], build([record("2024年/業務内容.xlsx"), record("業務内容.xlsx", "b")])["groups"])


class GuardedTextTestResult(unittest.TextTestResult):
    def _exc_info_to_string(self, err, test):
        return f"{err[0].__name__}: {err[1]}\n"


def install_runtime_guard():
    def deadline(_signum, _frame):
        raise TimeoutError("30_second_deadline")

    def denied(*_args, **_kwargs):
        raise RuntimeError("forbidden_runtime_filesystem")

    def guard(event, _args):
        if event == "open" or event.startswith(("socket.", "subprocess.", "ctypes.")):
            raise RuntimeError("forbidden_runtime_io:" + event)
        if event in {"os.system", "os.fork", "os.forkpty", "os.posix_spawn", "os.exec", "os.mkdir",
                     "os.remove", "os.rename", "os.rmdir", "os.symlink", "os.link", "os.truncate",
                     "os.listdir", "os.scandir", "os.chdir", "os.chmod", "os.chown", "os.utime"}:
            raise RuntimeError("forbidden_runtime_io:" + event)

    signal.signal(signal.SIGALRM, deadline)
    signal.alarm(30)
    sys.addaudithook(guard)
    for name in ("stat", "lstat", "readlink"):
        setattr(os, name, denied)


if __name__ == "__main__":
    required = unittest.defaultTestLoader.loadTestsFromTestCase(UnmarkedCandidateTests)
    residual = unittest.defaultTestLoader.loadTestsFromTestCase(ResidualWitnessTests)
    required_count, residual_count = required.countTestCases(), residual.countTestCases()
    stream = io.StringIO()
    install_runtime_guard()
    result = unittest.TextTestRunner(stream=stream, verbosity=2, resultclass=GuardedTextTestResult).run(unittest.TestSuite([required, residual]))
    metadata = json.dumps({"task_id": "lms-v1-00-hardening-2026-09-09-f03a", "resolver_sha256": SOURCE_SHA256,
                           "source_bytes": len(SOURCE_BYTES), "fixture_bytes_cumulative": FIXTURE_BYTES,
                           "required_acceptance_and_control_methods": required_count, "residual_witness_methods": residual_count,
                           "runtime_guard": "deny filesystem/network/process after reviewed preload; in-memory IO adapters; not OS isolation"})
    output = metadata + "\n" + stream.getvalue()
    assert len(output.encode("utf-8")) <= 1048576
    signal.alarm(0)
    print(output, end="")
    raise SystemExit(0 if result.wasSuccessful() else 1)
