"""Read-only additive root gold snapshot; stdout only, no product imports."""
from pathlib import Path
import ast
import hashlib
import json

ROOT = Path(__file__).resolve().parents[3]
RUNS = ROOT / "design/local-memory-v1-hardening/runs"


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def methods(raw):
    return {node.name + "." + child.name: digest(ast.dump(child, include_attributes=False).encode())
            for node in ast.parse(raw.decode()).body if isinstance(node, ast.ClassDef)
            for child in node.body if isinstance(child, ast.FunctionDef) and child.name.startswith("test_")}


def main():
    previous = json.loads((RUNS / "f05b-root-gold-snapshots.v1.json").read_bytes())
    for snapshot in previous["snapshots"]:
        assert digest(snapshot["source"].encode()) == snapshot["sha256"]
        assert methods(snapshot["source"].encode()) == snapshot["methods"]
    raw = (ROOT / "tests/test_decision_snapshot_controls.py").read_bytes()
    assert digest(raw) == "62fec8a6f18c575be5015ddace1af5d1421d0311ca5dfd8e3912720ab8c5eef5"
    current = methods(raw)
    assert len(current) == 13
    old = previous["snapshots"][-1]["methods"]
    assert len(old) == 10 and all(current[key] == value for key, value in old.items())
    print(json.dumps(dict(task_id=previous["task_id"], status="root_gold_identity_only_not_product_pass",
                         previous_snapshot_packet_sha256=digest((RUNS / "f05b-root-gold-snapshots.v1.json").read_bytes()),
                         original_ten_methods_unchanged=True,
                         snapshot=dict(name="controls-gold-v3", sha256=digest(raw), bytes=len(raw), source=raw.decode(), methods=current)), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
