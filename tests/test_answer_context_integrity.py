from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / "distribution" / "macos-local-memory" / "engine"
MODULE_PATH = ENGINE / "answer_local_memory_v2.py"
SPEC = importlib.util.spec_from_file_location("answer_context_integrity", MODULE_PATH)
if SPEC is None or SPEC.loader is None:
    raise ImportError(f"cannot load {MODULE_PATH}")
answer = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = answer
SPEC.loader.exec_module(answer)


def result(evidence_id: str, text: str) -> dict[str, object]:
    return {
        "evidence_id": evidence_id,
        "relative_path": "document.txt",
        "locator": {"object_index": evidence_id},
        "text": text,
    }


class AnswerContextIntegrityTests(unittest.TestCase):
    def test_packet_is_included_whole_or_not_at_all(self) -> None:
        records = [
            result("one", "A" * 1600),
            result("two", "B" * 1600),
            result("three", "C" * 1600),
        ]

        context, packet_ids = answer.compact_context(records, max_characters=4200)

        self.assertEqual(packet_ids, {"E1": "one", "E2": "two"})
        self.assertIn("A" * 1600, context)
        self.assertIn("B" * 1600, context)
        self.assertNotIn("C" * 1600, context)

    def test_oversized_packet_is_rejected_instead_of_sliced(self) -> None:
        context, packet_ids = answer.compact_context(
            [result("oversized", "X" * 1801), result("bounded", "末尾根拠")],
            max_characters=2600,
        )

        self.assertEqual(packet_ids, {"E1": "bounded"})
        self.assertNotIn("X", context)
        self.assertIn("末尾根拠", context)

    def test_every_returned_packet_id_maps_to_its_exact_full_text(self) -> None:
        records = [result("first", "甲\n乙"), result("second", "丙" * 900)]
        context, packet_ids = answer.compact_context(records, max_characters=4200)

        by_id = {item["evidence_id"]: item["text"] for item in records}
        for evidence_id in packet_ids.values():
            self.assertIn(by_id[evidence_id], context)

    def test_batch_requires_every_fields_primary_evidence(self) -> None:
        field_inputs = [
            {"item": {"item_id": "F1"}, "retrieved": [result("one", "A")]},
            {"item": {"item_id": "F2"}, "retrieved": [result("two", "B")]},
        ]

        with self.assertRaisesRegex(
            ValueError, "batch_context_missing_primary_evidence"
        ):
            answer.require_batch_primary_coverage(field_inputs, {"E1": "one"})

        answer.require_batch_primary_coverage(
            field_inputs, {"E1": "one", "E2": "two"}
        )


if __name__ == "__main__":
    unittest.main()
