"""Exact, pure-memory directory digest golds independent of tree traversal."""
from __future__ import annotations

import hashlib
import importlib.util
from pathlib import Path
import unittest


ENGINE = Path(__file__).resolve().parents[1] / "distribution/macos-local-memory/engine"
SPEC = importlib.util.spec_from_file_location("directory_digest_builder", ENGINE / "build_path_graph.py")
builder = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(builder)


def directory(relative, parent):
    return {"relative_path": relative, "parent_path": parent, "basename": relative.rsplit("/", 1)[-1],
            "kind": "directory", "size_bytes": 0, "sha256": None}


def leaf(relative, parent, digest):
    return {"relative_path": relative, "parent_path": parent, "basename": relative.rsplit("/", 1)[-1],
            "kind": "file", "size_bytes": 6, "sha256": digest}


def digest_literal(text):
    # Deliberately do not use the producer's canonical/hash helpers. The gold
    # material below spells the complete sorted-key canonical child arrays.
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class DirectoryHashGoldTests(unittest.TestCase):
    def test_empty_root_has_exact_empty_children_digest(self):
        self.assertEqual(builder.directory_hashes([]), {".": digest_literal("[]")})

    def test_single_direct_file_has_exact_children_digest(self):
        expected = digest_literal('[{"basename":"memo.txt","kind":"file","sha256":"' + "a" * 64 + '","size_bytes":6}]')
        self.assertEqual(builder.directory_hashes([leaf("memo.txt", ".", "a" * 64)]), {".": expected})

    def test_one_directory_root_includes_actual_child_digest(self):
        entries = [directory("bucket", "."), leaf("bucket/memo.txt", "bucket", "a" * 64)]
        child = digest_literal('[{"basename":"memo.txt","kind":"file","sha256":"' + "a" * 64 + '","size_bytes":6}]')
        root = digest_literal('[{"basename":"bucket","kind":"directory","sha256":"' + child + '","size_bytes":0}]')
        self.assertEqual(builder.directory_hashes(entries), {"bucket": child, ".": root})

    def test_three_levels_propagate_changed_leaf_to_every_ancestor(self):
        entries = [directory("a", "."), directory("a/b", "a"), directory("a/b/c", "a/b"), leaf("a/b/c/memo.txt", "a/b/c", "a" * 64)]
        changed = [dict(item) for item in entries]
        changed[-1]["sha256"] = "b" * 64
        before, after = builder.directory_hashes(entries), builder.directory_hashes(changed)
        self.assertEqual(set(before), {".", "a", "a/b", "a/b/c"})
        for path in before:
            with self.subTest(path=path):
                self.assertNotEqual(before[path], after[path])

    def test_record_order_does_not_change_directory_digests(self):
        entries = [directory("bucket", "."), leaf("z.txt", ".", "b" * 64), leaf("bucket/memo.txt", "bucket", "a" * 64)]
        self.assertEqual(builder.directory_hashes(entries), builder.directory_hashes(list(reversed(entries))))

    def test_empty_child_directory_is_hashed_before_root(self):
        empty = digest_literal("[]")
        root = digest_literal('[{"basename":"empty","kind":"directory","sha256":"' + empty + '","size_bytes":0}]')
        self.assertEqual(builder.directory_hashes([directory("empty", ".")]), {"empty": empty, ".": root})


if __name__ == "__main__":
    unittest.main()
