"""Read-only reconstruction of frozen root test gold and method identities.

Prints JSON for orchestrator apply_patch; imports no product/test modules.
The original controls revision is reconstructed by removing only the known
additive helper/method section, then matched to its pre-run frozen byte hash.
"""
from pathlib import Path
import ast
import hashlib
import json

ROOT = Path(__file__).resolve().parents[3]


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def methods(raw):
    tree = ast.parse(raw.decode())
    return {node.name + "." + child.name: digest(ast.dump(child, include_attributes=False).encode())
            for node in tree.body if isinstance(node, ast.ClassDef)
            for child in node.body if isinstance(child, ast.FunctionDef) and child.name.startswith("test_")}


def main():
    seed = (ROOT / "tests/test_decision_snapshot_e2e.py").read_bytes()
    controls = (ROOT / "tests/test_decision_snapshot_controls.py").read_bytes()
    prefix, marker, tail = controls.partition(b"\n    def literal_selection_fixture(self):")
    assert marker and tail.count(b'\n\nif __name__ == "__main__":') == 1
    _, suffix_marker, suffix = tail.partition(b'\n\nif __name__ == "__main__":')
    original_controls = prefix + suffix_marker + suffix
    assert digest(seed) == "14ad1a5a53f9e5251f0947e20e33980eb5d62ec284a5283cef17d2d22d2ab27c"
    assert digest(original_controls) == "eead732427b0a04e0162503bf6af272c85721071c32cc6099ef269698dea4ccf"
    assert digest(controls) == "3036775b63386c4a67dadadab93d495886a31a504fbc740b5a8f53a0a87763e0"
    old_methods, new_methods = methods(original_controls), methods(controls)
    assert len(methods(seed)) == 6 and len(old_methods) == 7 and len(new_methods) == 10
    assert all(new_methods[key] == value for key, value in old_methods.items())
    snapshots = []
    for name, raw in (("app-gold-v1", seed), ("controls-gold-v1", original_controls), ("controls-gold-v2", controls)):
        snapshots.append(dict(name=name, sha256=digest(raw), bytes=len(raw), source=raw.decode(), methods=methods(raw)))
    print(json.dumps(dict(task_id="local-memory-v1-f05b-snapshot-complete-selection",
                         status="frozen_root_gold_identity_only_not_product_pass",
                         original_control_methods_unchanged=True, snapshots=snapshots), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
