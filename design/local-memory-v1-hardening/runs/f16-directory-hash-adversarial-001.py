"""Pure independent digest holdout: sibling ordering, Unicode, and depth."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location('directory_holdout_builder', ROOT / 'distribution/macos-local-memory/engine/build_path_graph.py')
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


def digest(children):
    return hashlib.sha256(json.dumps(children, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')).hexdigest()


def child(name, kind, size, value):
    return {'basename': name, 'kind': kind, 'size_bytes': size, 'sha256': value}


class DirectoryHoldout(unittest.TestCase):
    def test_siblings_unicode_and_deep_descendant_match_explicit_child_lists(self):
        prior = None
        for leaf_hash in ('b' * 64, 'c' * 64):
            empty = digest([])
            sub = digest([child('record', 'file', 6, leaf_hash)])
            unicode_dir = digest([child('sub', 'directory', 0, sub)])
            root = digest([
                child('a', 'directory', 0, empty),
                child('z.txt', 'file', 6, 'a' * 64),
                child('資料', 'directory', 0, unicode_dir),
            ])
            expected = {'.': root, 'a': empty, '資料': unicode_dir, '資料/sub': sub}
            entries = [
                {'relative_path': '資料/sub/record', 'parent_path': '資料/sub', **child('record', 'file', 6, leaf_hash)},
                {'relative_path': 'z.txt', 'parent_path': '.', **child('z.txt', 'file', 6, 'a' * 64)},
                {'relative_path': '資料/sub', 'parent_path': '資料', **child('sub', 'directory', 0, None)},
                {'relative_path': '資料', 'parent_path': '.', **child('資料', 'directory', 0, None)},
                {'relative_path': 'a', 'parent_path': '.', **child('a', 'directory', 0, None)},
            ]
            with mock.patch.object(os, 'open', side_effect=AssertionError('unexpected filesystem IO')), mock.patch.object(os, 'read', side_effect=AssertionError('unexpected filesystem IO')), mock.patch.object(Path, 'open', side_effect=AssertionError('unexpected filesystem IO')):
                actual = builder.directory_hashes(entries)
                reversed_actual = builder.directory_hashes(list(reversed(entries)))
            self.assertEqual(actual, expected)
            self.assertEqual(reversed_actual, expected)
            if prior is not None:
                self.assertEqual(prior['a'], actual['a'])
                for name in ('.', '資料', '資料/sub'):
                    self.assertNotEqual(prior[name], actual[name])
            prior = actual


if __name__ == '__main__':
    unittest.main()
