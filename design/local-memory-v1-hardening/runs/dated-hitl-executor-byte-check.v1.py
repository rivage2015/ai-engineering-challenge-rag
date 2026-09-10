"""Read-only selected-byte/AST check, not a product test or audit acceptance."""
import ast
import difflib
import hashlib
import json
from pathlib import Path
import signal

ROOT = Path(__file__).resolve().parents[3]
RUNS = "design/local-memory-v1-hardening/runs/"
PINS = {
    "distribution/macos-local-memory/engine/document_version_resolver.py":
        "11218cc2b5170ba59a69203bcc549a50dc6eb6f88e487d191fb55a76048973a8",
    RUNS + "dated-hitl-executor-before-resolver.v1.py":
        "14ecce2c01d68bfbfaa27d3022bfc6076a97465a331eb03443af0e6d50cc006f",
    RUNS + "dated-hitl-executor-after-resolver.v1.py":
        "11218cc2b5170ba59a69203bcc549a50dc6eb6f88e487d191fb55a76048973a8",
    RUNS + "dated-hitl-executor-resolver-delta.v1.patch":
        "2ccb23aa6a63413b76fa56ca13c74faf552a312d4dae1c82a7d9e5a0a5ed461d",
    "tests/test_dated_temporal_candidates.py":
        "6a9a877771117441b33cdb94f5bc4f459b067bf682c538dbade00593189c7154",
    RUNS + "dated-hitl-executor-gold.v1.py":
        "6a9a877771117441b33cdb94f5bc4f459b067bf682c538dbade00593189c7154",
    "tests/test_dated_document_human_gate.py":
        "ee62aaf42632bca032c80a40a54893a817cd78f72c5d95e1c1078ba96a69586a",
    "tests/test_year_only_supersession.py":
        "70a1226cb30daa6692533d2c6f1fe270fe301a8f759c0db56a142acbc945b8c0",
    "tests/test_document_version_resolver.py":
        "594089cdee5e0298c5c732e7da369417e14de7fea6282594acc43a55c6a4a126",
    "tests/test_unmarked_version_candidates.py":
        "7bf83e2823742cc1449fdd69890d780e8c8b8daf69911905ce6cb20af2ad1f29",
    RUNS + "dated-hitl-executor-run.v1.py":
        "8932d3d909c903678af4ed5ad5f0abf6352b4487ba20739765b62e494e80350b",
}


def timeout(signum, frame):
    raise TimeoutError("selected-byte-check:30sec")


signal.signal(signal.SIGALRM, timeout)
signal.alarm(30)
blobs = {}
total = 0
for relative, expected in PINS.items():
    with (ROOT / relative).open("rb") as handle:
        raw = handle.read(1048577)
    assert len(raw) <= 1048576, relative + ":1MiB limit"
    total += len(raw)
    assert total <= 8388608, "8MiB total limit"
    assert hashlib.sha256(raw).hexdigest() == expected, relative + ":hash drift"
    blobs[relative] = raw

before = blobs[RUNS + "dated-hitl-executor-before-resolver.v1.py"].decode("utf-8")
after = blobs[RUNS + "dated-hitl-executor-after-resolver.v1.py"].decode("utf-8")
assert after.encode() == blobs["distribution/macos-local-memory/engine/document_version_resolver.py"]
assert blobs["tests/test_dated_temporal_candidates.py"] == blobs[RUNS + "dated-hitl-executor-gold.v1.py"]
delta = "".join(difflib.unified_diff(
    before.splitlines(keepends=True), after.splitlines(keepends=True),
    fromfile="before/document_version_resolver.py",
    tofile="distribution/macos-local-memory/engine/document_version_resolver.py"))
assert delta.encode() == blobs[RUNS + "dated-hitl-executor-resolver-delta.v1.patch"]

def functions(source):
    return {node.name: ast.dump(node, include_attributes=False)
            for node in ast.parse(source).body if isinstance(node, ast.FunctionDef)}

old_functions, new_functions = functions(before), functions(after)
changed = sorted(name for name in old_functions if old_functions[name] != new_functions.get(name))
added = sorted(set(new_functions) - set(old_functions))
assert changed == ["_family_component", "automatic_selection", "candidate", "family_key",
                   "has_version_signal", "validation_policy"]
assert added == ["_calendar_year", "_has_temporal_signal", "_temporal_component"]
assert set(old_functions) <= set(new_functions)
tree = ast.parse(blobs["tests/test_dated_temporal_candidates.py"])
classes = {node.name: [child.name for child in node.body
                      if isinstance(child, ast.FunctionDef) and child.name.startswith("test_")]
           for node in tree.body if isinstance(node, ast.ClassDef)}
assert {name: len(methods) for name, methods in classes.items()} == {
    "DatedTemporalCandidateTests": 16, "DatedLegacyDecisionResidualTests": 1}
signal.alarm(0)
print(json.dumps({
    "status": "selected_bytes_and_ast_match", "audit_acceptance": False,
    "selected_files": len(PINS), "bytes_read": total, "product_imports": 0,
    "before_sha256": hashlib.sha256(before.encode()).hexdigest(),
    "after_sha256": hashlib.sha256(after.encode()).hexdigest(),
    "gold_unchanged": True, "delta_exact": True,
    "changed_functions": changed, "added_functions": added,
    "other_original_functions_ast_unchanged": len(old_functions) - len(changed),
}, indent=2))
