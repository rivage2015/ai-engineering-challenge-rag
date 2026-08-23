"""Regression tests for memory-conscious local Ollama context selection."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RAG = ROOT / "rag"
if str(RAG) not in sys.path:
    sys.path.insert(0, str(RAG))

from answer import OllamaAnswerClient  # noqa: E402


class _CapturingClient(OllamaAnswerClient):
    def __init__(self) -> None:
        super().__init__(model="fixture-model")
        self.payload = None

    def _request(self, path: str, payload=None, timeout=None):
        self.payload = payload
        return {"message": {"content": "確認済み"}}


class OllamaDynamicContextTest(unittest.TestCase):
    def test_short_prompt_uses_8k_context(self) -> None:
        client = _CapturingClient()
        self.assertEqual(
            "確認済み",
            client.generate([{"role": "user", "content": "短い質問です"}]),
        )
        self.assertEqual(8192, client.payload["options"]["num_ctx"])

    def test_context_grows_with_japanese_prompt(self) -> None:
        client = _CapturingClient()
        client.generate([{"role": "user", "content": "根" * 9000}])
        self.assertEqual(16384, client.payload["options"]["num_ctx"])

        client.generate([{"role": "user", "content": "根" * 18000}])
        self.assertEqual(32768, client.payload["options"]["num_ctx"])

    def test_large_prompt_retains_previous_65k_ceiling(self) -> None:
        client = _CapturingClient()
        client.generate([{"role": "user", "content": "根" * 40000}])
        self.assertEqual(65536, client.payload["options"]["num_ctx"])


if __name__ == "__main__":
    unittest.main()
