"""Read-only inverse/AST freeze verification; never import product/test code."""
import ast
import hashlib
import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[3]
RUNS = ROOT / "design/local-memory-v1-hardening/runs"
SOURCES = (
    ("scripts/probe_intermediate_records.py", "probe", "py"),
    ("scripts/build_intermediate_records.py", "managed", "py"),
    ("scripts/validate_intermediate_records.py", "intermediate-native", "py"),
    ("scripts/validate_intermediate_records_streaming.py", "intermediate-stream", "py"),
    ("scripts/build_search_units.py", "search-builder", "py"),
    ("scripts/validate_search_units.py", "search-native", "py"),
    ("scripts/validate_search_units_streaming.py", "search-stream", "py"),
    ("schemas/evidence.schema.json", "evidence-schema", "json"),
    ("schemas/search-unit.schema.json", "search-schema", "json"),
    ("distribution/macos-local-memory/engine/validate_adaptive_semantic_graph.py", "adaptive-validator", "py"),
)


def apply_delta(source, patch, reverse=False):
    lines = source.splitlines(keepends=True)
    output = []
    position = 0
    hunks = []
    current = None
    for line in patch.splitlines(keepends=True)[2:]:
        if line.startswith("@@ "):
            match = re.match(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", line)
            assert match is not None, "bad unified hunk"
            old_start, old_count, new_start, new_count = match.groups()
            current = (int(old_start), int(old_count or 1), int(new_start), int(new_count or 1), [])
            hunks.append(current)
        else:
            assert current is not None and line[:1] in {" ", "+", "-"}, "unexpected delta line"
            current[4].append(line)
    for old_start, old_count, new_start, new_count, body in hunks:
        count = new_count if reverse else old_count
        start = (new_start if reverse else old_start) - (1 if count else 0)
        assert start >= position
        output.extend(lines[position:start])
        position = start
        consumed = produced = 0
        for line in body:
            prefix, text = line[0], line[1:]
            if reverse and prefix in "+-":
                prefix = "+" if prefix == "-" else "-"
            if prefix in " -":
                assert position < len(lines) and lines[position] == text, "context/source mismatch"
                position += 1
                consumed += 1
            if prefix in " +":
                output.append(text)
                produced += 1
        assert consumed == count
        assert produced == (old_count if reverse else new_count)
    output.extend(lines[position:])
    return "".join(output)


def main():
    checks = []
    coherent = json.loads((RUNS / "f11a-executor-coherent.v1.json").read_text())
    frozen = {item["path"]: item["sha256"] for item in coherent["sources"]}
    for source_path, name, extension in SOURCES:
        before = (RUNS / f"f11a-executor-before-{name}.v1.{extension}").read_text()
        after_bytes = (ROOT / source_path).read_bytes()
        after = after_bytes.decode("utf-8")
        patch = (RUNS / f"f11a-executor-{name}-delta.v1.diff").read_text()
        assert apply_delta(before, patch) == after
        assert apply_delta(after, patch, reverse=True) == before
        digest = hashlib.sha256(after_bytes).hexdigest()
        assert digest == frozen[source_path]
        checks.append({"path": source_path, "forward_exact": True, "inverse_exact": True,
                       "coherent_sha256_unchanged": True, "sha256": digest})
    old = ast.parse((RUNS / "f11a-executor-gold-test.v1.py").read_text())
    current_path = ROOT / "tests/test_notebook_metadata_binding.py"
    current = ast.parse(current_path.read_text())
    reduced = ast.Module(body=[node for node in current.body if not (
        isinstance(node, ast.ClassDef) and node.name == "NotebookMetadataPostAPITests"
    )], type_ignores=[])
    assert ast.dump(old, include_attributes=False) == ast.dump(reduced, include_attributes=False)
    assert current_path.read_bytes() == (RUNS / "f11a-executor-gold-test.v2.py").read_bytes()
    print(json.dumps({"status": "static_freeze_checks_passed_not_product_audit",
                      "scope": "read-only source/JSON/AST/delta processing, no product imports",
                      "product_checks": checks, "initial_gold_AST_unchanged": True,
                      "full_gold_bytes_unchanged": True}, indent=2))


if __name__ == "__main__":
    main()
