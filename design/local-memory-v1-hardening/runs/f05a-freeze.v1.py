"""Read-only F05a source packet construction and pre-audit integrity checks.

Emits JSON to stdout; the orchestrator saves it via apply_patch. No product IO
other than reads; no imports of product/test modules and no test execution.
"""
from pathlib import Path
from datetime import datetime, timezone
import hashlib
import json
import re
import runpy

ROOT = Path(__file__).resolve().parents[3]
RUNS = ROOT / 'design/local-memory-v1-hardening/runs'
TASK = 'lms-v1-00-hardening-2026-09-09-f05a'


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read(path):
    return json.loads(path.read_bytes())


def main():
    checkpoint = read(ROOT / 'design/local-memory-v1-hardening/checkpoint.json')
    for name, expected in checkpoint['protected_user_changes'].items():
        assert sha(ROOT / name) == expected, name
    assert sha(ROOT / checkpoint['plan']) == checkpoint['plan_sha256']
    manifest = read(RUNS / 'f05a-executor-manifest.v1.json')
    assert manifest['task_id'] == TASK and len(manifest['files']) == 37
    for item in manifest['files']:
        path = ROOT / item['path']
        assert sha(path) == item['sha256'] and path.stat().st_size == item['bytes'], path
    sources, path_ids = {}, {}

    def add(sid, path):
        path = Path(path)
        assert path.is_absolute() and path.is_file()
        if str(path) in path_ids:
            return path_ids[str(path)]
        assert sid not in sources
        sources[sid] = dict(id=sid, path=str(path), sha256=sha(path), bytes=path.stat().st_size)
        path_ids[str(path)] = sid
        return sid

    keep = '''E2E_TEST YEAR_TEST FOCUSED_TEST LINEAGE_TEST MIGRATION_TEST SECURITY_TEST READER VALIDATOR PROJECTOR SERVER PARSER BUILD_INTERMEDIATE ADAPTER SEARCH_BUILDER INTERMEDIATE_VALIDATOR INTERMEDIATE_STREAM_VALIDATOR SEARCH_VALIDATOR SEARCH_STREAM_VALIDATOR PATH_BUILDER PATH_VALIDATOR ANSWER ANSWER_BASE QEG FINAL_AUDIT SECURITY_GATE GUARD DISPATCHER SUPERVISOR SCHEMA_DOCUMENT SCHEMA_EVIDENCE SCHEMA_RELATION SCHEMA_SEARCH_UNIT'''.split()
    old = {s['id']: s for s in read(RUNS / 'f03a-graph-artifact.v1.json')['sources']}
    for sid in keep:
        s = old[sid]
        assert sha(Path(s['path'])) == s['sha256'], sid
        add(sid, s['path'])
    names = {
        'RESOLVER': ROOT / 'distribution/macos-local-memory/engine/document_version_resolver.py',
        'RESOLVER_TEST': ROOT / 'distribution/macos-local-memory/tests/test_document_version_resolver.py',
        'BOOTSTRAP': ROOT / 'distribution/macos-local-memory/app/bootstrap.py',
        'README': ROOT / 'distribution/macos-local-memory/README.md',
        'PURE_TEST': ROOT / 'tests/test_version_graph_reconstruction.py',
        'APP_TEST': ROOT / 'tests/test_version_graph_validation_e2e.py',
        'UNMARKED_TEST': ROOT / 'tests/test_unmarked_version_candidates.py',
        'UNMARKED_APP_TEST': ROOT / 'tests/test_unmarked_version_e2e.py',
        'CONTRACT': RUNS / 'f05a-task-contract.v1.md',
        'ADDENDUM': RUNS / 'f05a-contract-addendum-001.v1.md',
        'ROOT_DELTAS': RUNS / 'f05a-root-deltas.v1.json',
        'ROOT_GOLD': RUNS / 'f05a-root-gold.v1.json',
        'ROOT_RUNNER': RUNS / 'f05a-root-run.v1.py',
        'ROOT_RUNNER_V2': RUNS / 'f05a-root-run.v2.py',
        'ROOT_DELTA_GENERATOR': RUNS / 'f05a-root-deltas-build.v1.py',
        'EXECUTOR_MANIFEST': RUNS / 'f05a-executor-manifest.v1.json',
        'EXECUTOR_SUMMARY_ORIGINAL': RUNS / 'f05a-executor-summary.v1.json',
        'EXECUTOR_SUMMARY': RUNS / 'f05a-executor-summary.v2.json',
        'EXECUTOR_DELTA_CHECKS': RUNS / 'f05a-executor-delta-checks.v1.json',
        'PARENT_VALIDATOR': RUNS / 'f05a-parent-validation.v1.py',
        'INVERSE_HELPER': RUNS / 'f18-audit-integrity.v1.py',
        'FREEZE_GENERATOR': Path(__file__),
    }
    for sid, path in names.items():
        add(sid, path)
    for index, item in enumerate(manifest['files']):
        add('EXEC_FILE_%02d' % index, ROOT / item['path'])
    required_runs = []
    attempted = [(ROOT / r['path'], r['status'], r['tests']) for r in manifest['runs']]
    root_runs = [('red-app', 4, 'failed'), ('green-app', 4, 'passed'), ('green-e2e', 17, 'passed'), ('green-unmarked', 4, 'passed'), ('green-focused', 13, 'passed'), ('green-lineage', 8, 'passed'), ('green-migration', 7, 'passed'), ('green-security', 1, 'passed'), ('green-unmarked-pure', 19, 'passed'), ('green-year', 11, 'passed')]
    for name, count, status in root_runs:
        attempted.append((RUNS / ('f05a-root-' + name + '-001') / 'result.json', status, count))
    observed_runs = []
    for index, (path, status, count) in enumerate(attempted):
        sid = add('RUN_%02d' % index, path)
        record = read(path)
        log = Path(record['log_path'])
        raw = log.read_bytes()
        assert record['status'] == status and record['tests_reported'] == count
        assert record['skipped_reported'] == record['expected_failures_reported'] == 0
        assert sha(log) == record['log_sha256'] and len(raw) == record['log_bytes'] <= 1048576
        assert record['timeout_seconds'] == 30 and record['max_log_bytes'] == 1048576
        footer = re.search(rb'Ran (\d+) tests? in [0-9.]+s\s+(OK|FAILED(?: \([^\r\n]+\))?)\s*\Z', raw)
        assert footer and int(footer[1]) == count
        assert (status == 'passed') == (record['exit_code'] == 0 and footer[2] == b'OK')
        add('LOG_%02d' % index, log)
        add('STARTED_%02d' % index, path.parent / 'started.json')
        required_runs.append(dict(source_id=sid, status=status, methods=count))
        observed_runs.append(dict(path=str(path), status=status, methods=count, footer=footer[2].decode(), result_sha256=sha(path), log_sha256=sha(log)))
    helper = RUNS / 'f18-audit-integrity.v1.py'
    assert sha(helper) == 'ed13ff27e65d73a6152f6bc3000df0e68b91b168b650344500122466a7bd1dd6'
    reverse = runpy.run_path(str(helper), run_name='f05a_inverse')['reverse_unified']
    deltas = []
    for triple in manifest['isolated_deltas']:
        current, before, patch = (ROOT / p for p in triple)
        assert reverse(current.read_text(), patch.read_text()).encode() == before.read_bytes()
        deltas.append([path_ids[str(ROOT / p)] for p in triple])
    for entry in read(names['ROOT_DELTAS'])['deltas']:
        path = Path(entry['path'])
        assert sha(path) == entry['after_sha256']
        assert hashlib.sha256(reverse(path.read_text(), entry['isolated_unified_diff']).encode()).hexdigest() == entry['before_sha256']

    def node(sid, text, basis):
        return dict(id=sid, text=text, basis=basis)

    artifact = dict(schema_version='1.0', task_id=TASK, artifact_version=1, audit_repair_round=0,
        status='awaiting_separate_audit', pre_audit_implementation_reviews=1, additive_gold_methods=2,
        task_contract=dict(source_ids=['CONTRACT', 'ADDENDUM'], maximum_repair_cycles=2, role_separation='same_model_separate_context'),
        nodes=[
            node('N_INPUTS', 'Only explicit caller inventory and optional decisions snapshots provide validation authority. Parse/hash each read once; reject malformed and ambiguous JSON. Stored graph paths are diagnostic only and are never followed.', ['CONTRACT', 'RESOLVER', 'PURE_TEST']),
            node('N_REBUILD', 'Reconstruct complete candidates/groups/policy/projection/counts from explicit inputs, preserving existing policy. Reject submitted graph forgery even when it is self-hashed, including omitted members and fabricated automatic/Human/stale outcomes.', ['RESOLVER', 'PURE_TEST', 'EXECUTOR_MANIFEST']),
            node('N_GATE', 'Bootstrap passes its explicit configured decisions input into the immediate pre-Reader CLI gate. Four synthetic adversarial app paths reject there and retain prepared prior CONFIG/index/review/source bytes.', ['BOOTSTRAP', 'APP_TEST', 'ROOT_GOLD', path_ids[str(RUNS / 'f05a-root-red-app-001/result.json')], path_ids[str(RUNS / 'f05a-root-green-app-001/result.json')]]),
            node('N_CONTROLS', 'Numeric/held/Human/stale controls and existing synthetic normal app query/final-audit paths remain tested. The final pure25 and resolver20 pass; added CLI-missing/overflow checks retain original23 gold and pre-review failures.', ['PURE_TEST', 'RESOLVER_TEST', 'E2E_TEST', 'UNMARKED_APP_TEST', 'EXECUTOR_MANIFEST', 'EXECUTOR_SUMMARY']),
            node('N_LIMITS', 'Scope is standalone validation plus immediate bootstrap gate, not full F05 or V1. Downstream post-gate revalidation, immutable saved decision snapshots, Human authenticity/lease, source freshness, full format/model/GUI/package acceptance remain open. Existing year/unmarked tests include three residual witnesses, not accepted desired behavior. Pre-audit report correction and all failures retained.', ['CONTRACT', 'README', 'YEAR_TEST', 'UNMARKED_TEST', 'EXECUTOR_SUMMARY', 'EXECUTOR_SUMMARY_ORIGINAL', 'EXECUTOR_DELTA_CHECKS'])],
        edges=[
            dict(id='E_INPUTS_REBUILD', source='N_INPUTS', target='N_REBUILD', relation='supplies explicit same-byte inputs for deterministic reconstruction rather than graph-held authority', basis=['RESOLVER','PURE_TEST']),
            dict(id='E_REBUILD_GATE', source='N_REBUILD', target='N_GATE', relation='rejects reconstructed-result mismatch at bootstrap immediate validation before Reader', basis=['RESOLVER','BOOTSTRAP','APP_TEST']),
            dict(id='E_INPUTS_CONTROLS', source='N_INPUTS', target='N_CONTROLS', relation='explicit matching decision snapshot preserves legitimate Human choice and stale detection', basis=['RESOLVER','PURE_TEST','UNMARKED_APP_TEST']),
            dict(id='E_GATE_LIMITS', source='N_GATE', target='N_LIMITS', relation='bounds acceptance to the initial invocation and does not attest later graph or decision changes', basis=['CONTRACT','BOOTSTRAP','README'])],
        sources=list(sources.values()), isolated_deltas=deltas, required_runs=required_runs)
    for item in artifact['nodes'] + artifact['edges']:
        assert set(item['basis']) <= set(sources)
    print(json.dumps(dict(artifact=artifact, preflight=dict(task_id=TASK, checked_at_utc=datetime.now(timezone.utc).isoformat(), status='integrity_pass_not_formal_acceptance', protected_hashes='pass', plan_hash='pass', manifest_files_checked=37, source_count=len(sources), inverse_deltas_checked=6, historical_and_final_runs=observed_runs)), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
