"""Read-only source, AST and exact inverse-delta verification; never imports product."""
import ast
import copy
import hashlib
import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[3]
RUNS = ROOT / "design/local-memory-v1-hardening/runs"

def digest(raw):
    return hashlib.sha256(raw).hexdigest()

def inverse(current, patch):
    lines = current.decode("utf-8").splitlines(keepends=True)
    result, position = [], 0
    for line in patch.decode("utf-8").splitlines(keepends=True)[2:]:
        if line.startswith("@@ "):
            match = re.match(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@", line)
            start = int(match.group(1)) - 1
            assert start >= position
            result.extend(lines[position:start])
            position = start
        elif line.startswith((" ", "+")):
            assert lines[position] == line[1:]
            if line[0] == " ":
                result.append(line[1:])
            position += 1
        elif line.startswith("-"):
            result.append(line[1:])
        else:
            raise AssertionError("unexpected diff line")
    result.extend(lines[position:])
    return "".join(result).encode("utf-8")

checks = json.loads((RUNS / "f05b-executor-delta-checks.v2.json").read_bytes())
rows = []
for row in checks["deltas"]:
    current, before, patch = (ROOT / row[key] for key in ("path", "before", "delta"))
    restored = inverse(current.read_bytes(), patch.read_bytes())
    assert restored == before.read_bytes(), row["id"]
    rows.append({"id": row["id"], "current_sha256": digest(current.read_bytes()),
                 "before_sha256": digest(before.read_bytes()), "inverse_sha256": digest(restored),
                 "delta_sha256": digest(patch.read_bytes()), "exact_inverse": True})

old_path = RUNS / "f05b-executor-gold-test.v1.py"
new_path = ROOT / "tests/test_decision_snapshot_attestation.py"
old, new = ast.parse(old_path.read_bytes()), ast.parse(new_path.read_bytes())
old_class = next(node for node in old.body if isinstance(node, ast.ClassDef))
new_class = next(node for node in new.body if isinstance(node, ast.ClassDef))
old_methods = {node.name for node in old_class.body if isinstance(node, ast.FunctionDef)}
test_names = [node.name for node in new_class.body if isinstance(node, ast.FunctionDef) and node.name.startswith("test_")]
new_class.body = [node for node in new_class.body if not isinstance(node, ast.FunctionDef) or node.name in old_methods]
assert ast.dump(old, include_attributes=False) == ast.dump(new, include_attributes=False)
assert inverse(new_path.read_bytes(), (RUNS / "f05b-executor-gold-additive.v3.diff").read_bytes()) == old_path.read_bytes()
print(json.dumps({"status": "PASS", "meaning": "read-only inverse and original-gold preservation, not audit approval",
                  "deltas": rows, "original_gold_methods": 26, "current_gold_methods": test_names,
                  "original_gold_ast_unchanged": True}, indent=2))
