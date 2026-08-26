from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
ENGINE = REPO / "distribution" / "macos-local-memory" / "engine"
sys.path.insert(0, str(ENGINE))
SPEC = importlib.util.spec_from_file_location(
    "document_support_answer_local_memory_v2", ENGINE / "answer_local_memory_v2.py",
)
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


def candidate(document_id: str, evidence_id: str, score: float, text: str) -> dict:
    return {
        "document_id": document_id,
        "evidence_id": evidence_id,
        "relative_path": f"{document_id}.docx",
        "score": score,
        "semantic_score": 0.0,
        "lexical_score": 0.0,
        "token_score": score / 0.30,
        "text": text,
    }


class LocalMemoryDocumentSupportRankingTest(unittest.TestCase):
    def test_distinct_supporting_evidence_can_resolve_close_document_ranking(self) -> None:
        candidates = [
            candidate("final", "f1", 0.2325, "finalized theme"),
            candidate("final", "f2", 0.1525, "handout owner"),
            candidate("final", "f3", 0.1525, "venue details"),
            candidate("old", "o1", 0.2375, "old draft theme"),
            candidate("old", "o2", 0.1450, "old handout owner"),
            candidate("old", "o3", 0.1225, "old venue details"),
        ]
        ranked = MODULE.rerank_with_document_support(candidates)
        self.assertEqual([item["document_id"] for item in ranked[:2]], ["final", "old"])
        self.assertGreater(ranked[0]["document_support_bonus"], 0.0)

    def test_duplicate_text_does_not_create_document_support(self) -> None:
        candidates = [
            candidate("duplicate", "d1", 0.16, "same extracted text"),
            candidate("duplicate", "d2", 0.16, "same extracted text"),
            candidate("single", "s1", 0.17, "one stronger observation"),
        ]
        ranked = MODULE.rerank_with_document_support(candidates)
        self.assertEqual(ranked[0]["document_id"], "single")
        duplicate = next(item for item in ranked if item["document_id"] == "duplicate")
        self.assertEqual(duplicate["document_support_bonus"], 0.0)

    def test_many_weak_chunks_do_not_overpower_one_stronger_match(self) -> None:
        candidates = [candidate("strong", "s1", 0.06, "direct match")]
        candidates.extend(
            candidate("weak", f"w{index}", 0.049, f"weak fragment {index}")
            for index in range(20)
        )
        ranked = MODULE.rerank_with_document_support(candidates)
        self.assertEqual(ranked[0]["document_id"], "strong")
        weak = next(item for item in ranked if item["document_id"] == "weak")
        self.assertEqual(weak["document_support_bonus"], 0.0)


if __name__ == "__main__":
    unittest.main()
