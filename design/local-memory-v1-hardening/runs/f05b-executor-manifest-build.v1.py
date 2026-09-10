"""Read-only F05b handoff manifest builder; output is saved separately via apply_patch."""
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
RUNS = ROOT / "design/local-memory-v1-hardening/runs"
checks = json.loads((RUNS / "f05b-executor-delta-checks.v2.json").read_bytes())
paths = {ROOT / row[key] for row in checks["deltas"] for key in ("path", "before", "delta")}
paths.update(ROOT / path for path in (
    "tests/test_decision_snapshot_attestation.py",
    "design/local-memory-v1-hardening/runs/f05b-task-contract.v1.md",
    "design/local-memory-v1-hardening/runs/f05b-contract-clarification.v1.md",
    "design/local-memory-v1-hardening/runs/f05b-api-preflight.v1.md",
    "design/local-memory-v1-hardening/runs/f05b-contract-refinement.v1.md",
    "design/local-memory-v1-hardening/runs/f04a-executor-run.v1.py",
    "design/local-memory-v1-hardening/runs/f18-test-run.v2.py",
    "scripts/run_local_memory_hardening_tests.py",
))
for path in RUNS.glob("f05b-executor-*"):
    if path.name.startswith("f05b-executor-manifest."):
        continue
    if path.is_file():
        paths.add(path)
    elif path.is_dir():
        paths.update(child for child in path.iterdir() if child.is_file())
files, runs = [], []
for path in sorted(paths):
    raw = path.read_bytes()
    relative = str(path.relative_to(ROOT))
    files.append({"path": relative, "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)})
    if path.name == "result.json" and path.parent.name.startswith("f05b-executor-"):
        result = json.loads(raw)
        runs.append({"path": relative, "status": result["status"], "tests": result["tests_reported"],
                     "elapsed_seconds": result["elapsed_seconds"], "skips": result["skipped_reported"],
                     "expected_failures": result["expected_failures_reported"],
                     "log_path": result["log_path"], "log_sha256": result["log_sha256"], "log_bytes": result["log_bytes"],
                     "source_snapshot": "see chronological summary; exact earlier and final run identities retained"})
isolated = [[row["path"], row["before"], row["delta"]] for row in checks["deltas"]]
isolated.append(["tests/test_decision_snapshot_attestation.py",
                 "design/local-memory-v1-hardening/runs/f05b-executor-gold-test.v1.py",
                 "design/local-memory-v1-hardening/runs/f05b-executor-gold-additive.v3.diff"])
print(json.dumps({"schema_version": "1.0", "task_id": "local-memory-v1-f05b-snapshot-complete-selection",
                  "status": "executor_handoff_for_independent_audit_not_self_approval", "source_stable": True,
                  "formal_audit_repairs": 0, "files": files, "runs": runs, "isolated_deltas": isolated,
                  "limits": ["Only frozen F05b invariant; root owns separate audit and validation.",
                             "Synthetic model-free tests; no all-format, real-model, Human-authenticity, global-race, lease or full V1 claim.",
                             "Inherited compatibility resource guard is not a whole-process cap; see summary and root stronger budget reruns."]}, indent=2))
