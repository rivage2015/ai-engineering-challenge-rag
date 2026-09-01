from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from evidence_text_chunking import exact_text_chunks  # noqa: E402


class EvidenceTextChunkingTests(unittest.TestCase):
    def test_long_text_is_exact_and_each_chunk_reaches_the_question_path(self) -> None:
        value = ("先頭\n" + "A" * 900 + "\n後半の質問根拠\n") * 5
        chunks = exact_text_chunks(value, max_chars=1600)

        self.assertGreater(len(chunks), 1)
        self.assertEqual("".join(chunk.text for chunk in chunks), value)
        self.assertTrue(all(len(chunk.text) <= 1600 for chunk in chunks))
        self.assertEqual(chunks[0].start, 0)
        self.assertEqual(chunks[-1].end, len(value))
        self.assertIn("後半の質問根拠", chunks[-1].text)

    def test_hard_split_makes_progress_without_newlines(self) -> None:
        value = "後" * 4000
        chunks = exact_text_chunks(value, max_chars=1000)
        self.assertEqual([len(chunk.text) for chunk in chunks], [1000] * 4)
        self.assertEqual("".join(chunk.text for chunk in chunks), value)

    def test_newline_exactly_at_limit_stays_within_limit(self) -> None:
        value = "A" * 1600 + "\nB"
        chunks = exact_text_chunks(value, max_chars=1600)

        self.assertEqual("".join(chunk.text for chunk in chunks), value)
        self.assertEqual([len(chunk.text) for chunk in chunks], [1600, 2])


if __name__ == "__main__":
    unittest.main()
