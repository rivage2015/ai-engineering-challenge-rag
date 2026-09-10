#!/usr/bin/env python3
"""Check immutable F02a RED record identities; never report product acceptance."""
from __future__ import annotations

import hashlib
import json
import re
import signal
import sys
from pathlib import Path


def main() -> int:
    root = Path(__file__).resolve().parents[3]
    runs = root / "design/local-memory-v1-hardening/runs"
    contract = json.loads((runs / "f02a-task-contract.v1.json").read_text())
    manifest = json.loads((runs / "f02a-source-manifest.v1.json").read_text())
    red = json.loads((runs / "f02a-red-result.v1.json").read_text())
    source_bytes = {item["path"]: (root / item["path"]).read_bytes() for item in manifest["sources"]}
    log = (root / red["log"]["path"]).read_bytes()
    supervisor_bytes = (root / red["supervisor_result"]["path"]).read_bytes()
    assert sum(map(len, source_bytes.values())) + len(log) + len(supervisor_bytes) <= 1048576

    def guard(event, _args):
        if event == "open" or event.startswith(("socket.", "subprocess.", "ctypes.", "os.")):
            raise RuntimeError("forbidden_verifier_runtime_io:" + event)

    signal.alarm(30)
    sys.addaudithook(guard)
    for item in manifest["sources"]:
        assert hashlib.sha256(source_bytes[item["path"]]).hexdigest() == item["sha256"], item["path"]
    assert hashlib.sha256(log).hexdigest() == red["log"]["sha256"]
    assert len(log) == red["log"]["bytes"] <= red["log"]["limit_bytes"]
    assert hashlib.sha256(supervisor_bytes).hexdigest() == red["supervisor_result"]["sha256"]
    supervisor = json.loads(supervisor_bytes)
    assert supervisor["status"] == "failed" and supervisor["exit_code"] == 1
    assert supervisor["tests_reported"] == 11
    assert supervisor["skipped_reported"] == supervisor["expected_failures_reported"] == 0
    decoded = log.decode("utf-8")
    header = json.loads(decoded.splitlines()[0])
    assert header["synthetic_fixture_bytes"] == red["execution"]["synthetic_fixture_bytes"] <= 1048576
    assert header["resolver_sha256"] == contract["before_sources"][0]["sha256"]
    failures = set(re.findall(r"^FAIL: test_\w+ \(__main__\.([^)]*)\)$", decoded, re.MULTILINE))
    assert failures == set(contract["required_red_methods"]) == set(red["failed_requirements"])
    assert decoded.count("AssertionError: 'needs_human_review' != 'resolved'") == 5
    assert "forbidden_runtime_io:" not in decoded
    assert re.search(r"Ran 11 tests in [0-9.]+s\n\nFAILED \(failures=5\)\n\Z", decoded)
    ok_methods = set(re.findall(r"^test_\w+ \(__main__\.([^)]*)\) \.\.\. ok$", decoded, re.MULTILINE))
    expected_controls = set(contract["unchanged_control_methods"])
    residuals = set(contract["residual_witness_methods_not_improvement_acceptance"])
    assert ok_methods == expected_controls | residuals and not expected_controls & residuals
    print(json.dumps({
        "schema_version": "1.0", "task_id": contract["task_id"],
        "status": "RED_record_consistent_not_product_acceptance",
        "manifest_source_hashes_matched": len(manifest["sources"]),
        "actual_required_red_method_count": len(failures),
        "unchanged_control_method_count": len(expected_controls),
        "residual_witness_count_not_improvement_acceptance": len(residuals),
        "errors_skips_expected_failures": 0,
        "raw_supervisor_status_preserved": supervisor["status"],
        "raw_exit_code_preserved": supervisor["exit_code"],
        "log_sha256": red["log"]["sha256"],
    }, ensure_ascii=False, indent=2))
    signal.alarm(0)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
