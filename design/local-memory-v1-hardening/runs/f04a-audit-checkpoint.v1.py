"""Read-only frozen-input and log identity checkpoint for the F04a audit."""
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[3]
RUNS = ROOT / "design/local-memory-v1-hardening/runs"
artifact_path = RUNS / "f04a-graph-artifact.v1.json"
artifact = json.loads(artifact_path.read_text())
expected_artifact = "9b20f4ca8a9d10b5a587b2b7537acefb37556e9b336be9453aeec5c4a7266b56"


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


rows = [{"id": source["id"], "path": source["path"], "expected_sha256": source["sha256"],
         "observed_sha256": sha(source["path"])} for source in artifact["sources"]]
extra = ["distribution/macos-local-memory/app/bootstrap.py",
         "distribution/macos-local-memory/app/local_memory_server.py",
         "distribution/macos-local-memory/engine/build_path_graph.py",
         "distribution/macos-local-memory/engine/validate_path_graph.py",
         "distribution/macos-local-memory/engine/build_local_semantic_index.py",
         "distribution/macos-local-memory/engine/answer_local_memory.py",
         "distribution/macos-local-memory/engine/answer_local_memory_v2.py",
         "distribution/macos-local-memory/app/final_answer_audit.py",
         "scripts/run_local_memory_hardening_tests.py", "scripts/build_intermediate_records.py",
         "scripts/probe_intermediate_records.py",
         "design/local-memory-v1-hardening/runs/f04a-audit-run.v1.py",
         "design/local-memory-v1-hardening/runs/f04a-adversarial-tests.v1.py"]
logs = []
for source in artifact["sources"]:
    if source["id"] in {"RED", "GREEN_RESOLVER", "GREEN_E2E"}:
        result = json.loads(Path(source["path"]).read_text())
        logs.append({"source_id": source["id"], "path": result["log_path"],
                     "expected_sha256": result["log_sha256"], "observed_sha256": sha(result["log_path"])})
value = {"schema_version": "1.0", "checkpoint": sys.argv[1],
         "artifact_sha256": sha(artifact_path), "expected_artifact_sha256": expected_artifact,
         "sources": rows, "extra_sources": {path: sha(ROOT / path) for path in extra}, "logs": logs}
value["all_frozen_identities_match"] = value["artifact_sha256"] == expected_artifact and all(row["expected_sha256"] == row["observed_sha256"] for row in [*rows, *logs])
print(json.dumps(value, ensure_ascii=False, indent=2))
if not value["all_frozen_identities_match"]:
    raise SystemExit(1)
