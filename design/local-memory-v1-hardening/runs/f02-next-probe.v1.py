#!/usr/bin/env python3
"""Read-only, bounded in-memory F02 baseline observations; no product changes.

Reviewed source: resolver imports stdlib only, constants/regexes at import time,
and CLI under __main__. Load source and stdlib before denying runtime IO. Call
only candidate/family_key/resolve_group/graph_projection, never build/decide.
The audit hook is an in-process guard, not an OS sandbox certification.
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
import time
import typing
import unicodedata
from datetime import datetime
from pathlib import Path


def main() -> int:
    started = time.monotonic()
    root = Path(__file__).resolve().parents[3]
    source = root / "distribution/macos-local-memory/engine/document_version_resolver.py"
    source_bytes = source.read_bytes()
    assert len(source_bytes) <= 1024 * 1024
    source_sha256 = hashlib.sha256(source_bytes).hexdigest()

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
    namespace = {"__name__": "f02_next_readonly_probe", "__file__": str(source)}
    exec(compile(source_bytes, str(source), "exec"), namespace)

    def record(path, token):
        return {"relative_path": path, "kind": "file", "read_status": "observed",
                "sha256": token * 64, "size_bytes": 1, "mtime_ns": 2,
                "birthtime_ns": 1}

    fixtures = {
        "annual_named": [record("記録/実績2024.csv", "a"), record("記録/実績2025.csv", "b")],
        "neutral_named": [record("記録/資料2024.csv", "a"), record("記録/資料2025.csv", "b")],
        "annual_directories": [record("2024年/記録.csv", "a"), record("2025年/記録.csv", "b")],
        "year_version_inverse": [record("手順2024_ver2.csv", "a"), record("手順2025_ver1.csv", "b")],
        "current_year_residual": [record("実績2024.csv", "a"), record("現行/実績2025.csv", "b")],
        "version_normal": [record("手順_ver1.2.csv", "a"), record("手順_ver1.10.csv", "b")],
        "f04a_control": [record("現行/手順_ver1.csv", "a"), record("手順_ver2.csv", "b")],
        "unmarked_f03_residual": [record("実績2024.csv", "a"), record("実績.csv", "b")],
    }
    fixture_bytes = len(json.dumps(fixtures, ensure_ascii=False).encode("utf-8"))
    assert fixture_bytes <= 1024 * 1024
    observations = []

    def resolve(records, decision=None):
        candidates = [namespace["candidate"](item) for item in records]
        assert all(item is not None for item in candidates)
        keys = {namespace["family_key"](item["relative_path"]) for item in candidates}
        assert len(keys) == 1
        group = namespace["resolve_group"](next(iter(keys)), candidates, decision)
        nodes, edges = namespace["graph_projection"]([group])
        return group, nodes, edges

    for case_id, records in fixtures.items():
        if case_id == "unmarked_f03_residual":
            candidates = [namespace["candidate"](item) for item in records]
            observations.append({"id": case_id, "candidate_count": sum(item is not None for item in candidates),
                                 "unmarked_candidate": candidates[1]})
            continue
        group, _nodes, edges = resolve(records)
        reverse = resolve(list(reversed(records)))[0]
        assert reverse == group
        observations.append({"id": case_id, "status": group["status"],
                             "selected": group["selected_relative_path"], "reason": group["reason_code"],
                             "dispositions": {item["relative_path"]: item["disposition"] for item in group["candidates"]},
                             "active_edge_count": sum(edge["edge_type"] == "active_version" for edge in edges),
                             "order_invariant": True})

    initial, _, _ = resolve(fixtures["annual_named"])
    selected = initial["candidates"][0]
    decision = {"candidate_set_sha256": initial["candidate_set_sha256"],
                "selected_relative_path": selected["relative_path"],
                "selected_source_sha256": selected["source_sha256"]}
    human, _, _ = resolve(fixtures["annual_named"], decision)
    changed = [dict(item) for item in fixtures["annual_named"]]
    changed[1]["sha256"] = "c" * 64
    stale, _, stale_edges = resolve(changed, decision)
    observations.append({"id": "human_and_stale_control", "human_status": human["status"],
                         "human_selected": human["selected_relative_path"], "human_basis": human["resolution_basis"],
                         "stale_status": stale["status"], "stale_reason": stale["reason_code"],
                         "stale_selected": stale["selected_relative_path"],
                         "stale_active_edge_count": sum(edge["edge_type"] == "active_version" for edge in stale_edges)})
    assert all(item["reason"] == "unique_latest_explicit_year" for item in observations[:4])
    assert observations[4]["reason"] == "unique_explicit_current_marker"
    assert observations[5]["reason"] == "unique_latest_explicit_version"
    assert observations[6]["status"] == "needs_human_review"
    assert observations[6]["reason"] == "current_marker_conflicts_with_latest_version"
    assert observations[7]["candidate_count"] == 1
    assert human["resolution_basis"] == "human" and human["selected_relative_path"] == "記録/実績2024.csv"
    assert stale["reason_code"] == "stale_human_decision" and stale["selected_relative_path"] is None
    result = {"schema_version": "1.0", "task_id": "lms-v1-00-hardening-2026-09-09-f02-next-contract",
              "status": "baseline_observations_confirmed_not_product_acceptance",
              "source": str(source), "source_sha256": source_sha256,
              "python": sys.version.split()[0], "executable": sys.executable,
              "elapsed_seconds": round(time.monotonic() - started, 6), "wall_seconds_limit": 30,
              "synthetic_fixture_bytes": fixture_bytes, "synthetic_fixture_limit": 1048576,
              "runtime_io_guard": "deny open, directory enumeration, socket, subprocess, ctypes, process and filesystem mutation after source/stdlib preloading",
              "observations": observations,
              "not_run": ["product changes", "product tests", "resolver build/validate/record_decision",
                          "Reader/index/question execution", "HITL UI/runtime", "models/network/GUI/real data", "OS isolation certification"]}
    signal.alarm(0)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
