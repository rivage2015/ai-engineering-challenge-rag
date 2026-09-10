"""Read-only latest additive root gold identity; JSON stdout only."""
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
    prior_path = RUNS / "f05b-root-gold-snapshots.v2.json"
    prior = json.loads(prior_path.read_bytes())
    old = prior["snapshot"]
    assert digest(old["source"].encode()) == old["sha256"]
    assert methods(old["source"].encode()) == old["methods"]
    raw = (ROOT / "tests/test_decision_snapshot_controls.py").read_bytes()
    assert digest(raw) == "ff28bd5b93c8410f7928b2cb38ca981d1096abc44cf9373f970d64b2b587b7e6"
    current = methods(raw)
    assert len(current) == 16 and len(old["methods"]) == 13
    assert all(current[key] == value for key, value in old["methods"].items())
    print(json.dumps(dict(task_id=prior["task_id"], status="root_gold_identity_only_not_product_pass",
                         previous_snapshot_packet_sha256=digest(prior_path.read_bytes()),
                         original_thirteen_methods_unchanged=True,
                         snapshot=dict(name="controls-gold-v4", sha256=digest(raw), bytes=len(raw), source=raw.decode(), methods=current)), ensure_ascii=False, indent=2))

if __name__ == "__main__":
    main()
