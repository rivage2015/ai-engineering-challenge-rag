"""Read-only check of actual root result/log/terminal footer and source stage."""
from pathlib import Path
import hashlib
import json
import re

ROOT = Path(__file__).resolve().parents[3]
RUNS = ROOT / "design/local-memory-v1-hardening/runs"
CASES = [
    ("red-v1", "failed", 6, 0),
    ("controls-first-001", "passed", 16, 0),
    ("green-app-001", "passed", 6, 0),
    ("green-controls-001", "passed", 16, 0),
    ("green-app-002", "passed", 6, 0),
    ("green-controls-002", "passed", 16, 0),
    ("final-f05a-pure-001", "passed", 25, 0),
    ("final-unmarked-pure-001", "passed", 19, 0),
    ("final-year-001", "passed", 11, 0),
    ("final-unmarked-001", "passed", 4, 0),
    ("final-lineage-001", "passed", 8, 0),
    ("final-security-001", "completed_with_skips", 1, 1),
    ("final-focused-001", "passed", 13, 0),
    ("final-migration-001", "passed", 7, 0),
    ("final-app-001", "passed", 4, 0),
    ("final-security-native-001", "passed", 1, 0),
]
PRODUCT = {
    "engine/document_version_resolver.py": "14ecce2c01d68bfbfaa27d3022bfc6076a97465a331eb03443af0e6d50cc006f",
    "engine/build_adaptive_semantic_graph.py": "5d2883e2a053776935d71b2486180b177078fe71b5d7a0a02bf8741e8e041c0a",
    "engine/validate_adaptive_semantic_graph.py": "17c5de11a6f8f958ea5d1db447840f5653128ef24314fad193a610f824c5a106",
    "engine/build_local_semantic_index.py": "95b44b327fb0e0c1e747c9d244b077950ebf5b61dd0a8bf56b33b1a6c0e163da",
    "app/bootstrap.py": "e6248aae9ffa89e3cd6a43839af7f41e4d5941482f8f00a48f466de5524b526b",
}

def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

def main():
    records = []
    for suffix, status, count, skips in CASES:
        path = RUNS / ("f05b-root-" + suffix) / "result.json"
        value = json.loads(path.read_bytes())
        log = Path(value["log_path"])
        raw = log.read_bytes()
        assert value["status"] == status and value["tests_reported"] == count
        assert value["skipped_reported"] == skips and value["expected_failures_reported"] == 0
        assert sha(log) == value["log_sha256"] and len(raw) == value["log_bytes"] <= 1048576
        assert value["timeout_seconds"] == 30 and value["max_log_bytes"] == 1048576
        footer = re.search(rb"Ran (\d+) tests? in [0-9.]+s\s+(OK|FAILED)(?: \(([^\r\n]*)\))?\s*\Z", raw)
        assert footer and int(footer[1]) == count
        assert (status == "failed") == (footer[2] == b"FAILED" and value["exit_code"] != 0)
        budget = re.search(rb"F05b explicit fixture budget: (\{[^\r\n]+\})", raw)
        budget_value = json.loads(budget[1]) if budget else None
        if suffix.startswith("green-"):
            assert budget_value and 0 < budget_value["explicit_test_bytes"] <= 1048576
            assert 0 < budget_value["max_source_fixture_bytes"] <= 16384
        records.append(dict(result_path=str(path), result_sha256=sha(path), log_path=str(log), log_sha256=sha(log),
                            status=status, methods=count, skipped=skips, terminal_footer=footer[0].decode(), explicit_fixture_budget=budget_value))
    sources = []
    for name, expected in PRODUCT.items():
        path = ROOT / "distribution/macos-local-memory" / name
        assert sha(path) == expected, (name, sha(path), expected)
        sources.append(dict(path=str(path), sha256=expected))
    checkpoint = json.loads((ROOT / "design/local-memory-v1-hardening/checkpoint.json").read_bytes())
    for path, expected in checkpoint["protected_user_changes"].items():
        assert sha(ROOT / path) == expected
    assert sha(ROOT / checkpoint["plan"]) == checkpoint["plan_sha256"]
    print(json.dumps(dict(task_id="local-memory-v1-f05b-snapshot-complete-selection", status="root_integrity_and_local_tests_only_not_formal_acceptance",
                         latest_product_sources=sources, records=records,
                         residual_witnesses={"unmarked_pure":2,"year":1},
                         notes=["Original RED and first controls functional-only run retained.",
                                "First native-security run skipped because Python3.14 lacks openpyxl; existing3.9 native rerun actually passed1. No dependency install.",
                                "Latest002/root-final runs use the listed product stage; earlier runs are historical pre-static-directory-fix evidence.",
                                "Explicit fixture counters exclude product-created artifacts and do not guarantee whole-process resources."]), ensure_ascii=False, indent=2))

if __name__ == "__main__":
    main()
