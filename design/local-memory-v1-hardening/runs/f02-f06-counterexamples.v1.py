#!/usr/bin/env python3
"""Bounded in-memory observations of the unchanged resolver; no product edits.

Preflight: document_version_resolver.py is stdlib-only, has no import-time
source/model work, and only main() invokes CLI. This script calls candidate,
family_key, resolve_group and validate; it does not call build/record_decision.
The validator receives duck-typed in-memory paths. This verifies its logical
checks, not filesystem confinement or source discovery.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import signal
import sys
import time


def main() -> int:
    started = time.monotonic()
    root = Path(__file__).resolve().parents[3]
    source = root / "distribution/macos-local-memory/engine/document_version_resolver.py"
    source_bytes = source.read_bytes()
    if len(source_bytes) > 1024 * 1024:
        raise RuntimeError("resolver_source_limit")
    source_sha256 = hashlib.sha256(source_bytes).hexdigest()
    # Load required stdlib modules before denying filesystem/network/process IO.
    import tempfile
    import unicodedata
    import re
    import typing
    from datetime import datetime
    _ = argparse, tempfile, unicodedata, re, typing, datetime

    def deadline(_signum, _frame):
        raise TimeoutError("30_second_deadline")

    signal.signal(signal.SIGALRM, deadline)
    signal.alarm(30)

    def guard(event, args):
        if event == "open" or event.startswith(("socket.", "subprocess.", "ctypes.")):
            raise RuntimeError("forbidden_runtime_io:" + event)
        if event in {"os.system", "os.fork", "os.forkpty", "os.posix_spawn", "os.exec", "os.mkdir", "os.remove", "os.rename", "os.rmdir", "os.symlink", "os.link", "os.truncate"}:
            raise RuntimeError("forbidden_runtime_io:" + event)

    sys.addaudithook(guard)
    namespace = {"__name__": "resolver_readonly_probe", "__file__": str(source)}
    exec(compile(source_bytes, str(source), "exec"), namespace)

    def record(relative, token):
        return {"relative_path": relative, "kind": "file", "size_bytes": 1,
                "mtime_ns": 2, "birthtime_ns": 1, "sha256": token * 64,
                "read_status": "observed"}

    def candidates_for(records):
        return [item for item in map(namespace["candidate"], records) if item is not None]

    def resolve(records):
        candidates = candidates_for(records)
        keys = {namespace["family_key"](item["relative_path"]) for item in candidates}
        assert len(keys) == 1 and len(candidates) > 1
        return namespace["resolve_group"](next(iter(keys)), candidates, None)

    annual = [record("記録/実績2024.csv", "a"), record("記録/実績2025.csv", "b")]
    unmarked = [record("手順/業務内容2024.xlsx", "a"), record("手順/業務内容.xlsx", "b")]
    conflict = [record("現行/業務_ver1.xlsx", "a"), record("業務_ver2.xlsx", "b")]
    annual_group = resolve(annual)
    conflict_group = resolve(conflict)

    class MemoryPath:
        def __init__(self, data):
            self.data = data

        def read_text(self, encoding="utf-8"):
            return self.data.decode(encoding)

        def open(self, mode="r", **kwargs):
            assert mode == "rb", "validator unexpected IO operation"
            return io.BytesIO(self.data)

    inventory_bytes = b"".join((namespace["canonical_json"](item) + "\n").encode() for item in annual)
    fake_core = {"schema_version": namespace["SCHEMA_VERSION"],
                 "source": {"inventory_sha256": hashlib.sha256(inventory_bytes).hexdigest()},
                 "groups": [], "nodes": [], "edges": [],
                 "counts": {"groups": 0, "resolved": 0, "needs_human_review": 0}}
    fake_graph = {**fake_core, "graph_sha256": namespace["sha256_json"](fake_core)}
    graph_bytes = namespace["canonical_json"](fake_graph).encode()
    validator_result = namespace["validate"](MemoryPath(graph_bytes), MemoryPath(inventory_bytes))
    fixture_bytes = len(json.dumps([annual, unmarked, conflict], ensure_ascii=False).encode()) + len(inventory_bytes) + len(graph_bytes)
    assert fixture_bytes <= 1024 * 1024
    observations = [
        {"id": "F02", "method": "candidate/family_key/resolve_group", "actual": {key: annual_group[key] for key in ("status", "selected_relative_path", "reason_code")}, "desired": "Year labels alone do not establish replacement; annual vs revision stays needs_review until an explicit policy or human classification exists."},
        {"id": "F03", "method": "candidate; build grouping exclusion confirmed by source lines 364-372", "actual": {"inventory_count": len(unmarked), "candidate_count": len(candidates_for(unmarked)), "unmarked_candidate": namespace["candidate"](unmarked[1])}, "desired": "Unmarked potential peers must not silently escape the declared candidate universe; identity scope is still needs_review."},
        {"id": "F04", "method": "candidate/family_key/resolve_group", "actual": {key: conflict_group[key] for key in ("status", "selected_relative_path", "reason_code")}, "desired": {"status": "needs_human_review", "selected_relative_path": None, "reason_code": "current_marker_conflicts_with_latest_version"}},
        {"id": "F05", "method": "validate with in-memory graph/inventory paths", "actual": validator_result, "desired": "FAIL: a nonempty inventory with required version families cannot validate an empty self-rehashed partition."},
    ]
    reproduced = (annual_group["selected_relative_path"] == "記録/実績2025.csv"
                  and namespace["candidate"](unmarked[1]) is None
                  and conflict_group["selected_relative_path"] == "現行/業務_ver1.xlsx"
                  and validator_result["status"] == "PASS")
    result = {"schema_version": "1.0", "status": "defects_reproduced" if reproduced else "baseline_changed",
              "source": str(source), "source_sha256": source_sha256,
              "python": sys.version.split()[0], "executable": sys.executable,
              "wall_seconds_limit": 30, "elapsed_seconds": round(time.monotonic() - started, 6),
              "synthetic_fixture_bytes": fixture_bytes, "runtime_io_guard": "deny open/socket/subprocess/ctypes/process and filesystem mutation after stdlib preloading",
              "observations": observations,
              "not_run": ["product edits", "resolver build CLI", "record_decision", "Reader/index/question integration", "HITL UI/runtime", "model/network/GUI/credentials/real source documents", "OS isolation certification"]}
    signal.alarm(0)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if reproduced else 1


if __name__ == "__main__":
    raise SystemExit(main())
