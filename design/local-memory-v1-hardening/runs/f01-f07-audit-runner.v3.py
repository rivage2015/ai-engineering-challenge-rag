from __future__ import annotations

import hashlib
import http.client
import json
import runpy
import subprocess
import sys
import urllib.request
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[3]
TARGETS = {
    "focused": "distribution/macos-local-memory/tests/test_versioned_safe_index_e2e.py",
    "counterexample": "design/local-memory-v1-hardening/runs/f01-f07-adversarial-test.v1.py",
    "schema": "tests/test_local_graph_index_schema.py",
    "related": "design/local-memory-v1-hardening/runs/F01-003-regression-wrapper.py",
}
mode = sys.argv[1]
target = ROOT / TARGETS[mode]
artifact_path = ROOT / "design/local-memory-v1-hardening/runs/f01-f07-graph-artifact.v3.json"
artifact = json.loads(artifact_path.read_text())
monitored = [Path(source["path"]) for source in artifact["sources"]]
monitored += [
    ROOT / "distribution/macos-local-memory/engine/build_path_graph.py",
    ROOT / "scripts/probe_intermediate_records.py",
    ROOT / "distribution/macos-local-memory/engine/answer_local_memory.py",
    ROOT / "distribution/macos-local-memory/engine/answer_local_memory_v2.py",
    ROOT / "tests/test_semantic_lineage_relations.py",
    Path(__file__).resolve(),
    artifact_path,
    target,
]


def hashes():
    return {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest() for path in monitored}


before = hashes()
print("observed_hashes=" + json.dumps(before, sort_keys=True), flush=True)
print("python=" + sys.version, flush=True)
sys.path.insert(0, str(target.parent))
sys.argv = [str(target), "--worker"] if mode == "related" else [str(target), "-v"]
code = 0
with mock.patch.object(http.client.HTTPConnection, "connect", side_effect=AssertionError("audit HTTP forbidden")), mock.patch.object(urllib.request.OpenerDirector, "open", side_effect=AssertionError("audit HTTP forbidden")), mock.patch.object(subprocess, "Popen", side_effect=AssertionError("audit subprocess forbidden")):
    try:
        runpy.run_path(str(target), run_name="__main__")
    except SystemExit as error:
        code = error.code if isinstance(error.code, int) else 1
after = hashes()
print("hashes_unchanged=" + json.dumps(before == after), flush=True)
if before != after:
    print("changed_hashes=" + json.dumps({path: digest for path, digest in after.items() if before.get(path) != digest}, sort_keys=True), flush=True)
    code = 2
sys.exit(code)
