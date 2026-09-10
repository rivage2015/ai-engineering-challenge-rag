#!/usr/bin/env python3
"""Pure F02a regression tests: year order cannot prove document supersession.

Preimplementation baseline is intentionally RED; do not mark expectedFailure.
The separate current-marker class is a residual witness, not annual acceptance.
No source documents, Reader, model, network, GUI or product write APIs are used.
CLI execution installs runtime IO guards after loading inspected stdlib/source.
Importing this test module does not install a process-wide hook on other suites.
"""
from __future__ import annotations

import argparse
import hashlib
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
RESOLVER_SOURCE = ROOT / "distribution/macos-local-memory/engine/document_version_resolver.py"
SOURCE_LIMIT = 1024 * 1024
SOURCE_BYTES = RESOLVER_SOURCE.read_bytes()
if len(SOURCE_BYTES) > SOURCE_LIMIT:
    raise RuntimeError("resolver_source_limit")
RESOLVER_SOURCE_SHA256 = hashlib.sha256(SOURCE_BYTES).hexdigest()
resolver = types.ModuleType("year_only_supersession_resolver")
resolver.__file__ = str(RESOLVER_SOURCE)
exec(compile(SOURCE_BYTES, str(RESOLVER_SOURCE), "exec"), resolver.__dict__)


def record(path: str, token: str) -> dict:
    return {
        "relative_path": path, "kind": "file", "read_status": "observed",
        "sha256": token * 64, "size_bytes": 1, "mtime_ns": 2, "birthtime_ns": 1,
    }


FIXTURES = {
    "year_basename": [record("記録/実績2024.csv", "a"), record("記録/実績2025.csv", "b")],
    "neutral_words": [record("資料/項目2022.xlsx", "a"), record("資料/項目2026.xlsx", "b")],
    "year_directories": [record("2023年/記録.csv", "a"), record("2025年/記録.csv", "b")],
    "year_version_inverse": [record("手順2024_ver2.csv", "a"), record("手順2025_ver1.csv", "b")],
    "version_only": [record("手順_ver1.2.csv", "a"), record("手順_ver1.10.csv", "b")],
    "f04a": [record("現行/手順_ver1.csv", "a"), record("手順_ver2.csv", "b")],
    "draft": [record("資料2024.csv", "a"), record("資料2025_下書き.csv", "b")],
    "tied_year": [record("資料2025.csv", "a"), record("資料2025_コピー.csv", "b")],
    "current_marker_residual": [record("実績2024.csv", "a"), record("現行/実績2025.csv", "b")],
}
SYNTHETIC_FIXTURE_BYTES = len(json.dumps(FIXTURES, ensure_ascii=False).encode("utf-8"))
if SYNTHETIC_FIXTURE_BYTES > SOURCE_LIMIT:
    raise RuntimeError("synthetic_fixture_limit")


def resolve(records: list[dict], decision: dict | None = None) -> tuple[dict, list, list]:
    candidates = [resolver.candidate(item) for item in records]
    if not all(item is not None for item in candidates):
        raise AssertionError("fixture candidate discovery changed")
    keys = {resolver.family_key(item["relative_path"]) for item in candidates}
    if len(keys) != 1:
        raise AssertionError("fixture family identity changed")
    group = resolver.resolve_group(next(iter(keys)), candidates, decision)
    nodes, edges = resolver.graph_projection([group])
    return group, nodes, edges


class YearOnlySupersessionTests(unittest.TestCase):
    """F02a required behavior and existing supported controls, all pure."""

    def assert_complete_hold(self, records: list[dict], result: tuple, reason: str) -> None:
        group, nodes, edges = result
        self.assertEqual("needs_human_review", group["status"])
        self.assertIsNone(group["selected_relative_path"])
        self.assertIsNone(group["resolution_basis"])
        self.assertEqual(reason, group["reason_code"])
        expected_paths = {item["relative_path"] for item in records}
        self.assertEqual(expected_paths, {item["relative_path"] for item in group["candidates"]})
        self.assertEqual(len(records), len(group["candidates"]))
        self.assertTrue(all(item["disposition"] == "needs_human_review" for item in group["candidates"]))
        self.assertFalse(any(edge["edge_type"] == "active_version" for edge in edges))
        self.assertEqual(len(records), sum(edge["edge_type"] == "candidate_for" for edge in edges))
        document_nodes = [node for node in nodes if node["node_type"] == "document_version"]
        self.assertEqual(expected_paths, {node["relative_path"] for node in document_nodes})
        self.assertTrue(all(node["status"] == "needs_human_review" for node in document_nodes))

    def assert_year_hold(self, records: list[dict], result: tuple) -> None:
        self.assert_complete_hold(records, result, "year_order_does_not_establish_supersession")
        self.assertEqual(
            {item["relative_path"] for item in records}, set(result[0]["conflicts"]),
        )

    def test_basename_year_order_is_held(self) -> None:
        records = FIXTURES["year_basename"]
        self.assert_year_hold(records, resolve(records))

    def test_neutral_words_do_not_prove_supersession(self) -> None:
        records = FIXTURES["neutral_words"]
        self.assert_year_hold(records, resolve(records))

    def test_year_directories_are_held(self) -> None:
        records = FIXTURES["year_directories"]
        self.assert_year_hold(records, resolve(records))

    def test_year_version_inverse_is_held_without_fallthrough(self) -> None:
        records = FIXTURES["year_version_inverse"]
        self.assert_year_hold(records, resolve(records))

    def test_candidate_permutation_preserves_complete_hold(self) -> None:
        records = FIXTURES["year_basename"]
        forward = resolve(records)
        reverse = resolve(list(reversed(records)))
        self.assertEqual(forward, reverse)
        self.assert_year_hold(records, forward)
        self.assert_year_hold(records, reverse)

    def test_version_only_numeric_order_remains_selected(self) -> None:
        group, _nodes, edges = resolve(FIXTURES["version_only"])
        self.assertEqual("resolved", group["status"])
        self.assertEqual("手順_ver1.10.csv", group["selected_relative_path"])
        self.assertEqual("unique_latest_explicit_version", group["reason_code"])
        self.assertEqual("automatic", group["resolution_basis"])
        self.assertEqual(1, sum(item["disposition"] == "active" for item in group["candidates"]))
        self.assertEqual(1, sum(item["disposition"] == "historical" for item in group["candidates"]))
        self.assertEqual(1, sum(edge["edge_type"] == "active_version" for edge in edges))

    def test_f04a_single_version_conflict_remains_held(self) -> None:
        records = FIXTURES["f04a"]
        self.assert_complete_hold(records, resolve(records), "current_marker_conflicts_with_latest_version")

    def human_decision(self) -> dict:
        group, _nodes, _edges = resolve(FIXTURES["year_basename"])
        selected = next(item for item in group["candidates"] if item["relative_path"] == "記録/実績2024.csv")
        return {
            "candidate_set_sha256": group["candidate_set_sha256"],
            "selected_relative_path": selected["relative_path"],
            "selected_source_sha256": selected["source_sha256"],
        }

    def test_hash_bound_human_choice_can_select_older_year(self) -> None:
        group, _nodes, edges = resolve(FIXTURES["year_basename"], self.human_decision())
        self.assertEqual("resolved", group["status"])
        self.assertEqual("human", group["resolution_basis"])
        self.assertEqual("human_confirmed_active", group["reason_code"])
        self.assertEqual("記録/実績2024.csv", group["selected_relative_path"])
        self.assertEqual(1, sum(edge["edge_type"] == "active_version" for edge in edges))

    def test_stale_human_choice_holds_changed_or_added_candidates(self) -> None:
        decision = self.human_decision()
        variants = []
        for changed_index in (0, 1):
            changed = [dict(item) for item in FIXTURES["year_basename"]]
            changed[changed_index]["sha256"] = "c" * 64
            variants.append(changed)
        variants.append([*FIXTURES["year_basename"], record("記録/実績2026.csv", "d")])
        for records in variants:
            self.assert_complete_hold(records, resolve(records, decision), "stale_human_decision")

    def test_existing_draft_and_tied_year_holds_remain(self) -> None:
        for name, reason in (("draft", "latest_year_is_draft"), ("tied_year", "latest_year_not_unique")):
            records = FIXTURES[name]
            self.assert_complete_hold(records, resolve(records), reason)


class CurrentMarkerResidualWitnessTests(unittest.TestCase):
    """Record unchanged current-marker limitation; never count as annual acceptance."""

    def test_current_marker_year_residual_is_observable(self) -> None:
        group, _nodes, edges = resolve(FIXTURES["current_marker_residual"])
        self.assertEqual("resolved", group["status"])
        self.assertEqual("現行/実績2025.csv", group["selected_relative_path"])
        self.assertEqual("unique_explicit_current_marker", group["reason_code"])
        self.assertEqual(1, sum(edge["edge_type"] == "active_version" for edge in edges))
        self.assertEqual(
            {"実績2024.csv": "historical", "現行/実績2025.csv": "active"},
            {item["relative_path"]: item["disposition"] for item in group["candidates"]},
        )


class GuardedTextTestResult(unittest.TextTestResult):
    def _exc_info_to_string(self, err, test):
        # Avoid unittest traceback source loading after the open denial. Keep
        # deterministic exception evidence, with test identities in the log.
        return f"{err[0].__name__}: {err[1]}\n"


def install_runtime_guard() -> None:
    def deadline(_signum, _frame):
        raise TimeoutError("30_second_deadline")

    def guard(event, _args):
        if event == "open" or event.startswith(("socket.", "subprocess.", "ctypes.")):
            raise RuntimeError("forbidden_runtime_io:" + event)
        if event in {
            "os.system", "os.fork", "os.forkpty", "os.posix_spawn", "os.exec",
            "os.mkdir", "os.remove", "os.rename", "os.rmdir", "os.symlink",
            "os.link", "os.truncate", "os.listdir", "os.scandir",
        }:
            raise RuntimeError("forbidden_runtime_io:" + event)

    signal.signal(signal.SIGALRM, deadline)
    signal.alarm(30)
    sys.addaudithook(guard)


if __name__ == "__main__":
    suite = unittest.TestSuite([
        unittest.defaultTestLoader.loadTestsFromTestCase(YearOnlySupersessionTests),
        unittest.defaultTestLoader.loadTestsFromTestCase(CurrentMarkerResidualWitnessTests),
    ])
    print(json.dumps({
        "phase": "preimplementation_red", "resolver_sha256": RESOLVER_SOURCE_SHA256,
        "synthetic_fixture_bytes": SYNTHETIC_FIXTURE_BYTES,
        "required_behavior_and_control_methods": 10, "residual_witness_methods": 1,
        "runtime_guard": "deny file/network/process IO after imports; not OS isolation certification",
    }), flush=True)
    install_runtime_guard()
    result = unittest.TextTestRunner(verbosity=2, resultclass=GuardedTextTestResult).run(suite)
    signal.alarm(0)
    raise SystemExit(0 if result.wasSuccessful() else 1)
