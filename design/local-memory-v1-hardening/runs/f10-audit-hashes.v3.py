"""Final hash checks and in-memory before-source reconstruction, no product writes."""
import hashlib
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
stage = sys.argv[1]
assert stage in {"before", "after"}
artifact_path = Path(__file__).with_name("f10-graph-artifact.v3.json")
artifact = json.loads(artifact_path.read_text())
executor = json.loads(Path(__file__).with_name("f10-executor.v3.json").read_text())
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
current = (ROOT / "scripts/probe_intermediate_records.py").read_text()
base = current
for hunk in re.split(r"(?m)^@@[^\n]*\n", artifact["parser_patch_snapshot"])[1:]:
    lines = hunk.splitlines(keepends=True)
    old = "".join(line[1:] for line in lines if line.startswith((" ", "-")))
    new = "".join(line[1:] for line in lines if line.startswith((" ", "+")))
    assert base.count(new) == 1
    base = base.replace(new, old, 1)
delta = (
    "        # URI terminal dot segments denote a directory, even though POSIX\n"
    "        # normpath would erase that meaning and allow a file-part lookup.\n"
    '        or target.rsplit("/", 1)[-1] in {".", ".."}\n'
)
assert current.count(delta) == 1
before_repair = current.replace(delta, "", 1)
output = {
    "stage": stage,
    "artifact_sha256": sha(artifact_path.read_bytes()),
    "artifact_matches_contract": sha(artifact_path.read_bytes()) == "0d2ec62aa7b749223479cba69b71c1dd84d1589d603d90b1ab9ab9f384cb4cf1",
    "source_hashes": rows,
    "saved_logs": logs,
    "cumulative_base_sha256_reconstructed": sha(base.encode()),
    "cumulative_base_matches": sha(base.encode()) == executor["cumulative_patch_base_sha256"],
    "source_before_repair_sha256_reconstructed": sha(before_repair.encode()),
    "source_before_repair_matches": sha(before_repair.encode()) == executor["source_before_repair_sha256"],
    "scope": "Only this audit hash-result file is generated; product/prior artifacts unchanged.",
}
assert output["artifact_matches_contract"] and output["cumulative_base_matches"] and output["source_before_repair_matches"]
assert all(row["matches"] for row in rows)
assert all(row["log_matches"] and row["bytes_match"] and row["result_matches"] for row in logs)
output_path = Path(__file__).with_name("f10-audit-hashes-" + stage + ".v3.json")
with output_path.open("x", encoding="utf-8") as stream:
    json.dump(output, stream, ensure_ascii=False, indent=2)
    stream.write("\n")
print(json.dumps({"path": str(output_path), "sha256": sha(output_path.read_bytes()), "artifact_matches": True, "source_count": len(rows), "saved_log_count": len(logs), "before_repair_matches": True, "cumulative_base_matches": True}, ensure_ascii=False, indent=2))
