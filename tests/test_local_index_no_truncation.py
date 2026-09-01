from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = (
    ROOT / "distribution" / "macos-local-memory" / "engine"
    / "build_local_semantic_index.py"
)
SPEC = importlib.util.spec_from_file_location("local_index_no_truncation", MODULE_PATH)
if SPEC is None or SPEC.loader is None:
    raise ImportError(f"cannot load {MODULE_PATH}")
index_builder = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = index_builder
SPEC.loader.exec_module(index_builder)


class LocalIndexNoTruncationTests(unittest.TestCase):
    def test_complete_inputs_are_accepted(self) -> None:
        index_builder.require_complete_embedding_inputs([
            ({"evidence_id": "one"}, "text", False, "text", "one.txt"),
        ])

    def test_any_truncated_input_fails_before_index_publication(self) -> None:
        with self.assertRaisesRegex(
            ValueError, "embedding_input_truncation_forbidden"
        ):
            index_builder.require_complete_embedding_inputs([
                ({"evidence_id": "one"}, "prefix", True, "full", "one.txt"),
            ])


if __name__ == "__main__":
    unittest.main()
