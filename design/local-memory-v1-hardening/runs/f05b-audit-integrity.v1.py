"""Independent read-only byte patch, AST and terminal-log verification.

No product/test imports. Output goes to stdout for apply_patch preservation.
Does not reuse the executor/root patch application or acceptance decisions.
"""
from pathlib import Path
import ast
import hashlib
import json
import math
import re

ROOT = Path(__file__).resolve().parents[3]
RUNS = ROOT / "design/local-memory-v1-hardening/runs"
ARTIFACT_HASH = "721db8b316bd22101ddcc9738d4006684eb50d9270af4a05e64c6b58af73b987"


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def strict(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            assert key not in result, key
            result[key] = value
        return result
    def number(value):
        result = float(value)
        assert math.isfinite(result)
        return result
    def invalid(value):
        raise ValueError(value)
    return json.loads(raw, object_pairs_hook=pairs, parse_float=number, parse_constant=invalid)


def transform(raw, delta, reverse=False):
    """Apply a unified diff with exact byte context and both hunk counts."""
    lines, patch = raw.splitlines(keepends=True), delta.splitlines(keepends=True)
    assert patch[0].startswith(b"--- ") and patch[1].startswith(b"+++ ")
    cursor, result, offset = 0, [], 2
    while offset < len(patch):
        match = re.fullmatch(rb"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@[^\n]*\n", patch[offset])
        assert match, patch[offset]
        old_at, old_count, new_at, new_count = [int(value) if value is not None else 1 for value in match.groups()]
        left, right = [], []
        offset += 1
        while offset < len(patch) and not patch[offset].startswith(b"@@ "):
            line = patch[offset]
            assert line[:1] in (b" ", b"-", b"+"), line
            token, text = line[:1], line[1:]
            if offset + 1 < len(patch) and patch[offset + 1].startswith(b"\\ No newline at end of file"):
                assert text.endswith(b"\n")
                text = text[:-1]
                offset += 1
            if token in (b" ", b"-"):
                left.append(text)
            if token in (b" ", b"+"):
                right.append(text)
            offset += 1
        assert len(left) == old_count and len(right) == new_count
        at, source, replacement = (new_at, right, left) if reverse else (old_at, left, right)
        at = at - 1 if source else at
        assert cursor <= at and lines[at:at + len(source)] == source
        result.extend(lines[cursor:at])
        result.extend(replacement)
        cursor = at + len(source)
    result.extend(lines[cursor:])
    return b"".join(result)


def methods(raw):
    return {node.name + "." + child.name: sha(ast.dump(child, include_attributes=False).encode())
            for node in ast.parse(raw).body if isinstance(node, ast.ClassDef)
            for child in node.body if isinstance(child, ast.FunctionDef) and child.name.startswith("test_")}


def log_record(path, expected_status=None, expected_methods=None):
    result = strict(path.read_bytes())
    log = Path(result["log_path"])
    raw = log.read_bytes()
    assert log.parent == path.parent and sha(raw) == result["log_sha256"]
    assert len(raw) == result["log_bytes"] <= 1048576
    assert result["timeout_seconds"] == 30 and result["max_log_bytes"] == 1048576
    count, status = result["tests_reported"], result["status"]
    if expected_status is not None:
        assert expected_status == status and expected_methods == count
    footer = re.search(rb"Ran (\d+) tests? in [0-9.]+s\s+(OK|FAILED)(?: \(([^\r\n]*)\))?\s*\Z", raw)
    if count is None:
        assert status == "failed" and result["exit_code"] != 0 and footer is None and b"SyntaxError:" in raw
    else:
        assert type(count) is int and count > 0 and footer and int(footer[1]) == count
        if status == "passed":
            assert footer[2] == b"OK" and footer[3] is None and result["exit_code"] == 0
            assert result["skipped_reported"] == result["expected_failures_reported"] == 0
        elif status == "completed_with_skips":
            assert footer[2] == b"OK" and footer[3] == b"skipped=1" and result["exit_code"] == 0
            assert result["skipped_reported"] == 1 and result["expected_failures_reported"] == 0
        else:
            assert status == "failed" and footer[2] == b"FAILED" and result["exit_code"] != 0
            assert result["skipped_reported"] == result["expected_failures_reported"] == 0
    budget = re.search(rb"F05b explicit fixture budget: (\{[^\n]+\})", raw)
    return {"path": str(path), "sha256": sha(path.read_bytes()), "status": status, "methods": count,
            "log_sha256": sha(raw), "log_bytes": len(raw),
            "terminal": footer[0].decode().strip() if footer else "SyntaxError before unittest methods",
            "fixture_budget": strict(budget[1]) if budget else None}


def main():
    artifact_raw = (RUNS / "f05b-graph-artifact.v1.json").read_bytes()
    assert sha(artifact_raw) == ARTIFACT_HASH
    artifact = strict(artifact_raw)
    sources = {source["id"]: source for source in artifact["sources"]}
    assert len(sources) == len(artifact["sources"]) == 210
    for source in sources.values():
        path = Path(source["path"])
        assert path.is_absolute() and path.is_relative_to(ROOT)
        raw = path.read_bytes()
        assert sha(raw) == source["sha256"] and len(raw) == source["bytes"], source["id"]
    def source_bytes(sid):
        return Path(sources[sid]["path"]).read_bytes()
    deltas = []
    for current_id, before_id, delta_id in artifact["isolated_deltas"]:
        current, before, delta = map(source_bytes, (current_id, before_id, delta_id))
        assert transform(before, delta) == current, current_id
        assert transform(current, delta, reverse=True) == before, current_id
        deltas.append({"current_id": current_id, "before_id": before_id, "delta_id": delta_id,
                       "forward_exact": True, "reverse_exact": True})
    readme = strict(source_bytes("ROOT_README_DELTA"))
    before, current, delta = readme["before_source"].encode(), source_bytes("README"), readme["isolated_unified_diff"].encode()
    assert sha(before) == readme["before_sha256"] and sha(current) == readme["after_sha256"]
    assert transform(before, delta) == current and transform(current, delta, reverse=True) == before
    packets = [strict((RUNS / ("f05b-root-gold-snapshots.v%d.json" % v)).read_bytes()) for v in (1, 2, 3)]
    snapshots = packets[0]["snapshots"] + [packets[1]["snapshot"], packets[2]["snapshot"]]
    gold = []
    for snapshot in snapshots:
        raw = snapshot["source"].encode()
        assert sha(raw) == snapshot["sha256"] and len(raw) == snapshot["bytes"]
        assert methods(raw) == snapshot["methods"]
        gold.append({"sha256": sha(raw), "methods": len(methods(raw))})
    assert [item["methods"] for item in gold] == [6, 7, 10, 13, 16]
    for old, new in zip(snapshots[1:], snapshots[2:]):
        assert all(new["methods"].get(key) == value for key, value in old["methods"].items())
    assert source_bytes("ROOT_APP_TEST") == snapshots[0]["source"].encode()
    assert source_bytes("ROOT_CONTROLS") == snapshots[-1]["source"].encode()
    pure = [(RUNS / ("f05b-executor-gold-test.v%d.py" % v)).read_bytes() for v in (1, 2, 3)]
    assert [len(methods(raw)) for raw in pure] == [26, 27, 28]
    for old, new in zip(pure, pure[1:]):
        assert all(methods(new).get(key) == value for key, value in methods(old).items())
    assert pure[-1] == source_bytes("PURE_TEST")
    manifest = strict(source_bytes("EXECUTOR_MANIFEST"))
    for item in manifest["files"]:
        raw = (ROOT / item["path"]).read_bytes()
        assert sha(raw) == item["sha256"] and len(raw) == item["bytes"]
    history = [log_record(Path(sources[r["source_id"]]["path"]), r["status"], r["methods"]) for r in artifact["required_runs"]]
    assert len(history) == 30
    red = (RUNS / "f05b-root-red-v1/unittest.log").read_text()
    assert "FAILED (failures=6)" in red and "TypeError:" not in red and red.count("FAIL: test_") == 6
    assert red.count("AssertionError:") == 6
    audit = [log_record(path) for path in sorted(RUNS.glob("f05b-audit-*/result.json"))]
    assert len(audit) == 18 and sum(r["methods"] for r in audit) == 205
    assert all(r["status"] == "passed" for r in audit)
    print(json.dumps({"task_id": artifact["task_id"], "artifact_sha256": ARTIFACT_HASH,
        "status": "independent_integrity_verified_not_semantic_verdict", "source_count": len(sources),
        "executor_manifest_files": len(manifest["files"]), "bidirectional_deltas": deltas,
        "readme_bidirectional_exact": True, "root_gold": gold, "pure_gold_methods": [26, 27, 28],
        "original_root_six_assertion_red": True, "historical_runs": history, "audit_runs": audit,
        "total_methods": 205, "acceptance_methods": 202, "residual_methods": 3,
        "failed_attempt_methods": 0}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
