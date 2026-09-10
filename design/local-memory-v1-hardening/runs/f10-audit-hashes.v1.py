"""Read-only hash and saved-record provenance checks; emits JSON to stdout."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
artifact_path = Path(__file__).with_name("f10-graph-artifact.v1.json")
artifact = json.loads(artifact_path.read_text())
executor = json.loads(Path(__file__).with_name("f10-executor.v1.json").read_text())
sha = lambda raw: hashlib.sha256(raw).hexdigest()
rows = []
for source in artifact["sources"]:
    actual = sha(Path(source["path"]).read_bytes())
    rows.append({"id": source["id"], "actual_sha256": actual, "matches": actual == source["sha256"]})
log_rows = []
for run in executor["runs"]:
    if "result_path" not in run:
        continue
    result_path = ROOT / run["result_path"]
    result = json.loads(result_path.read_text())
    log_path = Path(result["log_path"])
    raw = log_path.read_bytes()
    log_rows.append({"id": run["id"], "status": result["status"], "tests_reported": result["tests_reported"], "skips": result["skipped_reported"], "log_matches": sha(raw) == run["log_sha256"] == result["log_sha256"], "bytes_match": len(raw) == result["log_bytes"], "actual_log_sha256": sha(raw)})

current = (ROOT / "scripts/probe_intermediate_records.py").read_text()
before = current
for hunk in re.split(r"(?m)^@@[^\n]*\n", executor["parser_patch_snapshot"])[1:]:
    lines = hunk.splitlines(keepends=True)
    old = "".join(line[1:] for line in lines if line.startswith((" ", "-")))
    new = "".join(line[1:] for line in lines if line.startswith((" ", "+")))
    assert before.count(new) == 1, "inverse snapshot hunk must be unique"
    before = before.replace(new, old, 1)
before_sha = sha(before.encode())
output = {
    "artifact_sha256": sha(artifact_path.read_bytes()),
    "artifact_matches_contract": sha(artifact_path.read_bytes()) == "0121d75f0d54126c8534507caa5e9d9b1af508627879756996a042daf7d6df77",
    "source_hashes": rows,
    "saved_logs": log_rows,
    "source_before_sha256_reconstructed_in_memory": before_sha,
    "source_before_matches": before_sha == executor["source_before_sha256"],
    "scope": "No product sources or artifact/logs were written; inverse diff exists only in memory.",
}
print(json.dumps(output, ensure_ascii=False, indent=2))
assert output["artifact_matches_contract"] and output["source_before_matches"]
assert all(row["matches"] for row in rows)
assert all(row["log_matches"] and row["bytes_match"] for row in log_rows)
