"""Read-only repair1 source/log hash checks; inverse/forward patches stay in memory."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
artifact_path = Path(__file__).with_name("f10-graph-artifact.v2.json")
artifact = json.loads(artifact_path.read_text())
executor = json.loads(Path(__file__).with_name("f10-executor.v2.json").read_text())
prior = json.loads(Path(__file__).with_name("f10-graph-artifact.v1.json").read_text())
sha = lambda raw: hashlib.sha256(raw).hexdigest()
rows = []
for source in artifact["sources"]:
    actual = sha(Path(source["path"]).read_bytes())
    rows.append({"id": source["id"], "actual_sha256": actual, "matches": actual == source["sha256"]})
logs = []
for run in executor["results"]:
    result_path = Path(run["result_path"])
    result = json.loads(result_path.read_text())
    raw = Path(result["log_path"]).read_bytes()
    logs.append({"path": str(result_path), "status": result["status"], "tests_reported": result["tests_reported"], "skips": result["skipped_reported"], "result_matches": sha(result_path.read_bytes()) == run["result_sha256"], "log_matches": sha(raw) == run["log_sha256"] == result["log_sha256"], "bytes_match": len(raw) == result["log_bytes"]})

def transform(source, patch, reverse):
    for hunk in re.split(r"(?m)^@@[^\n]*\n", patch)[1:]:
        lines = hunk.splitlines(keepends=True)
        old = "".join(line[1:] for line in lines if line.startswith((" ", "-")))
        new = "".join(line[1:] for line in lines if line.startswith((" ", "+")))
        needle, replacement = (new, old) if reverse else (old, new)
        assert source.count(needle) == 1, "patch context must be unique"
        source = source.replace(needle, replacement, 1)
    return source

current = (ROOT / "scripts/probe_intermediate_records.py").read_text()
base = transform(current, artifact["parser_patch_snapshot"], True)
before_repair = transform(base, prior["parser_patch_snapshot"], False)
output = {
    "artifact_sha256": sha(artifact_path.read_bytes()),
    "artifact_matches_contract": sha(artifact_path.read_bytes()) == "20087e0774f67c9586e537117afd27dce41fedb1274d77f187b059f37058dbb5",
    "source_hashes": rows,
    "saved_logs": logs,
    "cumulative_base_sha256_reconstructed": sha(base.encode()),
    "cumulative_base_matches": sha(base.encode()) == executor["cumulative_patch_base_sha256"],
    "source_before_repair_sha256_reconstructed": sha(before_repair.encode()),
    "source_before_repair_matches": sha(before_repair.encode()) == executor["source_before_repair_sha256"],
    "scope": "No product source or existing artifact/log was written.",
}
print(json.dumps(output, ensure_ascii=False, indent=2))
assert output["artifact_matches_contract"] and output["cumulative_base_matches"] and output["source_before_repair_matches"]
assert all(row["matches"] for row in rows)
assert all(row["log_matches"] and row["bytes_match"] and row["result_matches"] for row in logs)
