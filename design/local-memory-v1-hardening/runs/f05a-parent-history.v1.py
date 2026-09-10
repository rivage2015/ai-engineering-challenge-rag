"""Read-only supplemental checks of F05a frozen gold and failure history."""
import ast
import hashlib
import json
from pathlib import Path
import runpy

ROOT = Path(__file__).resolve().parents[3]
RUNS = ROOT / 'design/local-memory-v1-hardening/runs'


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read(path):
    return json.loads(path.read_bytes())


def methods(path):
    tree = ast.parse(path.read_bytes())
    return {node.name: ast.dump(node) for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name.startswith('test_')}


def main():
    artifact = RUNS / 'f05a-graph-artifact.v1.json'
    assert sha(artifact) == '7b7414f3e58a62b6eb34fc71534da11eec51c1dd58c316326ba03e9ab436bf61'
    for item in read(artifact)['sources']:
        path = Path(item['path'])
        assert sha(path) == item['sha256'] and path.stat().st_size == item['bytes']
    manifest = read(RUNS / 'f05a-executor-manifest.v1.json')
    for item in manifest['files']:
        path = ROOT / item['path']
        assert sha(path) == item['sha256'] and path.stat().st_size == item['bytes']
    root_gold = read(RUNS / 'f05a-root-gold.v1.json')
    assert sha(ROOT / root_gold['test_path']) == root_gold['test_sha256']
    assert set(methods(ROOT / root_gold['test_path'])) == set(root_gold['methods'])
    assert sha(RUNS / 'f05a-root-run.v1.py') == root_gold['runner_sha256']
    assert sha(RUNS / 'f05a-task-contract.v1.md') == root_gold['contract_sha256']
    assert sha(RUNS / 'f05a-contract-addendum-001.v1.md') == root_gold['ownership_addendum_sha256']
    pure_gold = read(RUNS / 'f05a-executor-gold.v1.json')
    additive = read(RUNS / 'f05a-executor-gold.v2.json')
    original = RUNS / 'f05a-executor-gold-test.v1.py'
    final = ROOT / 'tests/test_version_graph_reconstruction.py'
    assert sha(original) == pure_gold['test_sha256'] == additive['original_gold_test_sha256']
    assert sha(final) == additive['new_gold_test_sha256']
    assert sha(RUNS / 'f05a-executor-gold-additive.v2.diff') == additive['additive_delta_sha256']
    assert sha(RUNS / 'f05a-executor-pre-review-resolver.v1.py') == additive['interim_resolver_sha256']
    assert sha(RUNS / 'f05a-executor-before-resolver.v1.py') == pure_gold['source_sha256'] == root_gold['resolver_before_sha256']
    assert sha(RUNS / 'f05a-executor-run.v1.py') == pure_gold['runner_sha256']
    old_methods, new_methods = methods(original), methods(final)
    assert len(old_methods) == 23 and len(new_methods) == 25
    assert set(old_methods) == set(pure_gold['tests'])
    assert set(new_methods) - set(old_methods) == set(additive['added_methods'])
    assert all(new_methods[name] == body for name, body in old_methods.items())
    supervisor = ROOT / 'scripts/run_local_memory_hardening_tests.py'
    assert sha(supervisor) == '6b3bde90b6ed649a82325f6b5ad9661756bef7b6e0303fa74c2d045fb26eb4a9'
    terminal = runpy.run_path(str(supervisor), run_name='f05a_history_supervisor')['terminal_summary']
    failures = []
    for name, expected_count, expected_details in [
        ('f05a-root-red-app-001', 4, {'failures': 4}),
        ('f05a-executor-red-pure-001', 8, {'failures': 26}),
        ('f05a-executor-review-red-001', 25, {'failures': 2, 'errors': 2}),
    ]:
        result_path = RUNS / name / 'result.json'
        result = read(result_path)
        raw = Path(result['log_path']).read_bytes()
        count, outcome, details = terminal(raw.decode('utf-8'))
        assert (count, outcome, details) == (expected_count, 'FAILED', expected_details)
        assert result['status'] == 'failed' and result['exit_code'] != 0
        assert hashlib.sha256(raw).hexdigest() == result['log_sha256']
        failures.append(dict(run=name, methods=count, failures=details.get('failures', 0), errors=details.get('errors', 0), result_sha256=sha(result_path)))
    print(json.dumps(dict(task_id=read(artifact)['task_id'], status='history_integrity_pass_not_formal_acceptance', artifact_sha256=sha(artifact), validator_sha256=sha(Path(__file__)), frozen_sources=115, executor_manifest_files=len(manifest['files']), original_pure_gold_methods=23, unchanged_original_methods=23, additive_methods=2, app_gold_methods=len(root_gold['methods']), failure_history=failures), indent=2))


if __name__ == '__main__':
    main()
