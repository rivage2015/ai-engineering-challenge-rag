"""User-required date-family approval controls. Pure synthetic path records.

No source document reads, inventory writes, models, network or UI startup.
These are desired-contract tests; preserve their initial failing results.
"""
import importlib.util
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "dated_human_gate_resolver",
    ROOT / "distribution/macos-local-memory/engine/document_version_resolver.py",
)
resolver = importlib.util.module_from_spec(spec)
spec.loader.exec_module(resolver)


def item(path):
    return resolver.candidate({
        "kind": "file", "read_status": "observed", "relative_path": path,
        "sha256": "a" * 64, "size_bytes": 10,
        "mtime_ns": 1, "birthtime_ns": 1,
    })


class DatedDocumentHumanGateTests(unittest.TestCase):
    def test_compact_full_dates_form_one_candidate_family(self):
        self.assertEqual(resolver.family_key("DAWN/受付手順_20240101.xlsx"),
                         resolver.family_key("DAWN/受付手順_20250202.xlsx"))

    def test_japanese_full_dates_form_one_candidate_family(self):
        self.assertEqual(resolver.family_key("DAWN/受付手順_2024年1月1日.xlsx"),
                         resolver.family_key("DAWN/受付手順_2025年2月2日.xlsx"))

    def test_dated_current_marker_still_requires_human_approval(self):
        candidates = [item("DAWN/受付手順_2024.xlsx"),
                      item("DAWN/受付手順_現行_2025.xlsx")]
        selected, reason, conflicts = resolver.automatic_selection(candidates)
        self.assertIsNone(selected, "a current-name marker must not approve dated peers")
        self.assertTrue(reason)

    def test_unrelated_procedures_remain_separate(self):
        self.assertNotEqual(resolver.family_key("DAWN/受付手順_2024.xlsx"),
                            resolver.family_key("DAWN/配膳手順_2025.xlsx"))

    def test_year_only_peers_already_hold(self):
        selected, reason, conflicts = resolver.automatic_selection([
            item("DAWN/受付手順_2024.xlsx"), item("DAWN/受付手順_2025.xlsx")])
        self.assertIsNone(selected)
        self.assertEqual(reason, "year_order_does_not_establish_supersession")


if __name__ == "__main__":
    unittest.main(verbosity=2)
