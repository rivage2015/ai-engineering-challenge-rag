#!/usr/bin/env python3
"""Bounded F03 baseline observations, not product acceptance or implementation."""
from __future__ import annotations

import argparse
import builtins
import hashlib
import io
import json
import os
import re
import signal
import sys
import tempfile
import time
import typing
import unicodedata
import unittest
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
SOURCE = ROOT / "distribution/macos-local-memory/engine/document_version_resolver.py"
SOURCE_BYTES = SOURCE.read_bytes()
SOURCE_HASH = hashlib.sha256(SOURCE_BYTES).hexdigest()
assert SOURCE_HASH == "9a7443862c015ed768007ef5a7b21bd3bc197c9bb7a3d7ed2f7b39b33374359d"
assert len(SOURCE_BYTES) <= 1048576
OBSERVATIONS = []
FIXTURE_BYTES = 0
STARTED = time.monotonic()


def denied(*_args, **_kwargs):
    raise RuntimeError("forbidden_runtime_filesystem")


def guard(event, _args):
    if event == "open" or event.startswith(("socket.", "subprocess.", "ctypes.")):
        raise RuntimeError("forbidden_runtime_io:" + event)
    if event in {
        "os.system", "os.fork", "os.forkpty", "os.posix_spawn", "os.exec",
        "os.mkdir", "os.remove", "os.rename", "os.rmdir", "os.symlink",
        "os.link", "os.truncate", "os.listdir", "os.scandir", "os.chdir",
        "os.chmod", "os.chown", "os.utime",
    }:
        raise RuntimeError("forbidden_runtime_io:" + event)


def deadline(_signum, _frame):
    raise TimeoutError("30_second_deadline")


signal.signal(signal.SIGALRM, deadline)
signal.alarm(30)
sys.addaudithook(guard)
for name in ("stat", "lstat", "readlink"):
    setattr(os, name, denied)
namespace = {"__name__": "f03a_next_readonly_resolver", "__file__": str(SOURCE)}
exec(compile(SOURCE_BYTES, str(SOURCE), "exec"), namespace)
# Avoid traceback linecache reads if an unexpected assertion fails.
unittest.result.TestResult._exc_info_to_string = (
    lambda self, err, test, **kwargs: f"{err[0].__name__}: {err[1]}"
)


class MemoryPath:
    def __init__(self, name):
        self.name = name

    def resolve(self, **_kwargs):
        return self

    def exists(self):
        return True

    def __str__(self):
        return "/synthetic-memory-only/" + self.name


class FrozenDateTime:
    @classmethod
    def now(cls):
        return cls()

    def astimezone(self):
        return self

    def isoformat(self, **_kwargs):
        return "2026-09-09T00:00:00+00:00"


namespace["datetime"] = FrozenDateTime


def record(path, token="a"):
    return {
        "relative_path": path, "kind": "file", "read_status": "observed",
        "sha256": token * 64, "size_bytes": 1, "mtime_ns": 2, "birthtime_ns": 1,
    }


def build(records, decisions=None):
    global FIXTURE_BYTES
    records = [dict(item) for item in records]
    decisions = dict(decisions or {})
    blobs = {
        "inventory": "".join(namespace["canonical_json"](item) + "\n" for item in records),
        "decisions": namespace["canonical_json"]({"decisions": list(decisions.values())}),
    }
    FIXTURE_BYTES += sum(len(value.encode("utf-8")) for value in blobs.values())
    assert FIXTURE_BYTES <= 1048576
    sinks = []
    namespace["load_inventory"] = lambda _path: records
    namespace["load_decisions"] = lambda _path: decisions
    namespace["sha256_file"] = lambda path: hashlib.sha256(blobs[path.name].encode("utf-8")).hexdigest()
    namespace["atomic_json"] = lambda path, value: sinks.append((path.name, value))
    result = namespace["build"](
        MemoryPath("inventory"), MemoryPath("output"),
        MemoryPath("decisions") if decisions else None,
    )
    assert len(sinks) == 1 and sinks[0] == ("output", result)
    return result


def summary(result):
    return {
        "counts": result["counts"],
        "groups": [{
            "group_id": item["group_id"], "status": item["status"],
            "reason": item["reason_code"], "selected": item["selected_relative_path"],
            "basis": item["resolution_basis"], "candidate_set_sha256": item["candidate_set_sha256"],
            "dispositions": {candidate["relative_path"]: candidate["disposition"] for candidate in item["candidates"]},
        } for item in result["groups"]],
        "active_edge_count": sum(edge["edge_type"] == "active_version" for edge in result["edges"]),
    }


def human_choice(group, selected):
    candidate = next(item for item in group["candidates"] if item["relative_path"] == selected)
    return {group["group_id"]: {
        "group_id": group["group_id"], "candidate_set_sha256": group["candidate_set_sha256"],
        "selected_relative_path": selected, "selected_source_sha256": candidate["source_sha256"],
        "decided_by": "synthetic-human", "decided_at": "2026-09-09T00:00:00+00:00",
    }}


class BaselineObservations(unittest.TestCase):
    def test_pair_membership_boundaries(self):
        cases = [
            ("year_basename", "業務内容2024.xlsx", "業務内容.xlsx", True),
            ("annual_neutral_words", "記録/実績2024.csv", "記録/実績.csv", True),
            ("numeric_version", "手順_ver1.csv", "手順.csv", True),
            ("current_marker", "現行/手順.csv", "手順.csv", True),
            ("historical_marker", "旧版/手順.csv", "手順.csv", True),
            ("draft_marker", "手順_下書き.csv", "手順.csv", True),
            ("year_directory", "2024/業務内容.xlsx", "業務内容.xlsx", True),
            ("year_kanji_directory_residual", "2024年/業務内容.xlsx", "業務内容.xlsx", False),
            ("suffix_change_residual", "業務内容2024.xlsx", "業務内容.pdf", False),
            ("meaningful_rename_residual", "業務内容2024.xlsx", "業務手順.xlsx", False),
            ("different_family_control", "業務内容2024.xlsx", "連絡先.xlsx", False),
            ("different_folder_control", "部署A/業務内容2024.xlsx", "部署B/業務内容.xlsx", False),
            ("all_unmarked_copy_control", "業務内容.xlsx", "業務内容コピー.xlsx", True),
            ("all_unmarked_neutral_control", "業務_内容.xlsx", "業務内容.xlsx", True),
        ]
        for case_id, marked, peer, same_key in cases:
            with self.subTest(case=case_id):
                records = [record(marked), record(peer, "b")]
                result = build(records)
                candidate_flags = [namespace["candidate"](item) is not None for item in records]
                keys = [namespace["family_key"](item["relative_path"]) for item in records]
                self.assertEqual(same_key, keys[0] == keys[1])
                self.assertEqual(0, result["counts"]["groups"])
                self.assertFalse(candidate_flags[1])
                self.assertEqual(not case_id.startswith("all_unmarked"), candidate_flags[0])
                reverse = build(list(reversed(records)))
                self.assertEqual(result["groups"], reverse["groups"])
                self.assertEqual(result["edges"], reverse["edges"])
                OBSERVATIONS.append({"id": case_id, "paths": [marked, peer],
                                     "same_family_key": same_key, "candidate_flags": candidate_flags,
                                     "family_keys": keys, "actual_build": summary(result), "order_invariant": True})

    def test_human_decision_later_addition(self):
        records = [record("業務内容_ver1.xlsx"), record("業務内容_ver2.xlsx", "b")]
        initial = build(records)
        group = initial["groups"][0]
        decision = human_choice(group, "業務内容_ver1.xlsx")
        chosen = build(records, decision)
        variants = [
            ("same_family_unmarked_addition_defect", record("業務内容.xlsx", "c"), "human_confirmed_active", True),
            ("same_family_marked_addition_control", record("業務内容_ver3.xlsx", "c"), "stale_human_decision", False),
            ("other_family_addition_control", record("連絡先.xlsx", "c"), "human_confirmed_active", True),
            ("other_suffix_addition_residual", record("業務内容.pdf", "c"), "human_confirmed_active", True),
            ("other_folder_addition_residual", record("部署B/業務内容.xlsx", "c"), "human_confirmed_active", True),
        ]
        for case_id, added, reason, same_set in variants:
            result = build([*records, added], decision)
            current = result["groups"][0]
            self.assertNotEqual(chosen["source"]["inventory_sha256"], result["source"]["inventory_sha256"])
            self.assertEqual(reason, current["reason_code"])
            self.assertEqual(same_set, current["candidate_set_sha256"] == group["candidate_set_sha256"])
            OBSERVATIONS.append({"id": case_id, "added_path": added["relative_path"],
                                 "inventory_hash_changed": True, "candidate_set_unchanged": same_set,
                                 "actual_build": summary(result)})
        for index in (0, 1):
            changed = [dict(item) for item in records]
            changed[index]["sha256"] = "d" * 64
            result = build(changed, decision)
            self.assertEqual("stale_human_decision", result["groups"][0]["reason_code"])
            OBSERVATIONS.append({"id": f"marked_content_change_{index}_control", "actual_build": summary(result)})
        unmarked_a = build([*records, record("業務内容.xlsx", "c")], decision)
        unmarked_b = build([*records, record("業務内容.xlsx", "d")], decision)
        self.assertEqual(unmarked_a["groups"], unmarked_b["groups"])
        OBSERVATIONS.append({"id": "omitted_unmarked_content_change_defect", "group_unchanged": True,
                             "actual_build": summary(unmarked_b)})

    def test_membership_loss_boundaries_and_existing_controls(self):
        records = [record("業務内容_ver1.xlsx"), record("業務内容_ver2.xlsx", "b")]
        initial = build(records)
        decision = human_choice(initial["groups"][0], "業務内容_ver1.xlsx")
        for case_id, changed_path in [
            ("rename_peer_to_unmarked_defect", "業務内容.xlsx"),
            ("rename_peer_to_other_family_residual", "手順_ver2.xlsx"),
            ("move_peer_other_folder_residual", "部署B/業務内容_ver2.xlsx"),
            ("change_peer_suffix_residual", "業務内容_ver2.pdf"),
        ]:
            result = build([records[0], record(changed_path, "b")], decision)
            self.assertEqual(0, result["counts"]["groups"])
            OBSERVATIONS.append({"id": case_id, "changed_path": changed_path,
                                 "old_decision_group_omitted_not_explicitly_invalidated": True,
                                 "actual_build": summary(result)})
        for case_id, paths, expected in [
            ("f02a_year_hold_control", ["実績2024.csv", "実績2025.csv"], "year_order_does_not_establish_supersession"),
            ("f04a_current_conflict_control", ["現行/手順_ver1.csv", "手順_ver2.csv"], "current_marker_conflicts_with_latest_version"),
            ("numeric_success_control", ["手順_ver1.csv", "手順_ver2.csv"], "unique_latest_explicit_version"),
        ]:
            result = build([record(paths[0]), record(paths[1], "b")])
            self.assertEqual(expected, result["groups"][0]["reason_code"])
            OBSERVATIONS.append({"id": case_id, "actual_build": summary(result)})

    def test_inclusion_only_current_marker_counterfactual(self):
        marked = namespace["candidate"](record("現行/手順.csv"))
        unmarked = {
            "relative_path": "手順.csv", "source_sha256": "b" * 64,
            "size_bytes": 1, "mtime_ns": 2, "birthtime_ns": 1,
            "explicit_years": [], "explicit_versions": [], "current_markers": [],
            "historical_markers": [], "draft_markers": [],
        }
        group = namespace["resolve_group"](namespace["family_key"]("手順.csv"), [marked, unmarked], None)
        self.assertEqual("unique_explicit_current_marker", group["reason_code"])
        self.assertEqual("historical", next(item["disposition"] for item in group["candidates"] if item["relative_path"] == "手順.csv"))
        OBSERVATIONS.append({
            "id": "inclusion_only_is_insufficient_counterfactual",
            "execution_kind": "direct resolve_group with manually supplied signal-free candidate; NOT actual build output",
            "status": group["status"], "reason": group["reason_code"],
            "selected": group["selected_relative_path"],
            "dispositions": {item["relative_path"]: item["disposition"] for item in group["candidates"]},
        })


if __name__ == "__main__":
    stream = io.StringIO()
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(BaselineObservations)
    result = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
    metadata = {
        "schema_version": "1.0", "task_id": "lms-v1-00-hardening-2026-09-09-f03a-next",
        "status": "baseline_observations_confirmed_not_product_acceptance" if result.wasSuccessful() else "baseline_probe_failed",
        "resolver_sha256": SOURCE_HASH, "resolver_bytes": len(SOURCE_BYTES),
        "python": sys.version.split()[0], "executable": sys.executable,
        "fixture_bytes_cumulative": FIXTURE_BYTES, "elapsed_seconds": round(time.monotonic() - STARTED, 6),
        "methods_run": result.testsRun, "observations": OBSERVATIONS,
        "limitations": ["No product modifications", "I/O adapters and frozen clock only in probe memory",
                        "No Reader/index/UI/CONFIG/model/network/record_decision/validate execution",
                        "Python guard is not OS isolation or F05 certification"],
    }
    output = json.dumps(metadata, ensure_ascii=False, indent=2) + "\n" + stream.getvalue()
    assert len(output.encode("utf-8")) <= 1048576
    signal.alarm(0)
    print(output, end="")
    raise SystemExit(0 if result.wasSuccessful() else 1)
