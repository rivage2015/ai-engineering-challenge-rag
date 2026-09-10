"""Three tiny independent source-binding checks using the guarded fixture."""
import importlib.util
import json
import os
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location('auditor_fixture', ROOT / 'tests/test_path_graph_validator_binding.py')
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)


class Adversarial(fixture.PathValidatorBindingTests):
    def test_nested_same_size_changed_bytes_fail_even_with_restored_mtime(self):
        bucket = self.root / 'bucket'
        bucket.mkdir()
        nested = bucket / 'nested.txt'
        nested.write_bytes(b'BEFORE')
        self.snapshot()
        previous = nested.stat()
        nested.write_bytes(b'AFTER!')
        os.utime(nested, ns=(previous.st_atime_ns, previous.st_mtime_ns))
        with self.canary_reads_forbidden():
            result = self.check()
        self.assertEqual(result['status'], 'FAIL', result)
        self.assertIn('current_source_sha256_changed:bucket/nested.txt', result['errors'])
        self.assertIn('node_source_hash_mismatch:bucket/nested.txt', result['errors'])

    def test_forged_source_path_is_rejected_before_source_io(self):
        self.graph['nodes'][1]['source']['path'] = str(self.canary)
        self.save()
        with self.source_reads_forbidden():
            result = self.check()
        self.assertEqual(result['status'], 'FAIL', result)
        self.assertIn('node_source_path_binding_mismatch', result['errors'])

    def test_duplicate_graph_json_key_is_rejected_before_source_io(self):
        payload = json.dumps(self.graph)
        token = '"schema_version": "1.1"'
        self.assertEqual(payload.count(token), 1)
        self.graph_path.write_text(payload.replace(token, token + ', ' + token), encoding='utf-8')
        with self.source_reads_forbidden():
            result = self.check()
        self.assertEqual(result['status'], 'FAIL', result)
        self.assertEqual(result['errors'], ['path_validation_input_or_source_error:ValueError'])


if __name__ == '__main__':
    names = [
        'test_nested_same_size_changed_bytes_fail_even_with_restored_mtime',
        'test_forged_source_path_is_rejected_before_source_io',
        'test_duplicate_graph_json_key_is_rejected_before_source_io',
    ]
    result = unittest.TextTestRunner(verbosity=2).run(unittest.TestSuite(Adversarial(name) for name in names))
    raise SystemExit(not result.wasSuccessful())
