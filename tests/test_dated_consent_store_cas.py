"""Synthetic decision-store CAS tests; no application or user data access."""
from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "dated_consent_store_resolver",
    ROOT / "distribution/macos-local-memory/engine/document_version_resolver.py",
)
resolver = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(resolver)


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def consent(group="1", selected="業務2025.csv"):
    set_hash = ("a" if group == "1" else "b") * 64
    source_hash = ("c" if group == "1" else "d") * 64
    return {
        "decision_schema_version": "2.0",
        "group_id": "version_set_" + group * 32,
        "candidate_set_sha256": set_hash,
        "relation": "same_work_revisions",
        "selected_relative_path": selected,
        "selected_source_sha256": source_hash,
        "current_applicability_confirmed": True,
        "allow_ingest_index_answer": True,
        "reviewed_revision": {
            "generation": "generation-" + "e" * 32,
            "source_scope_sha256": "f" * 64,
            "graph_sha256": "1" * 64,
            "graph_file_sha256": "2" * 64,
            "inventory_sha256": "3" * 64,
            "candidate_set_sha256": set_hash,
            "decisions_sha256": None,
            "resolver_version": "0.1.6",
        },
        "decided_by": "local-ui-human",
        "decided_at": "2026-09-09T12:00:00+00:00",
    }


class DatedConsentStoreCasTests(unittest.TestCase):
    def test_absent_store_is_distinct_and_first_write_succeeds(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "decisions.json"
            self.assertEqual(
                resolver.decision_store_revision(path),
                {"exists": False, "sha256": None, "byte_count": 0},
            )
            saved = resolver.record_dated_consent_cas(path, consent(), None)
            self.assertEqual(saved, consent())
            value = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(value, {
                "schema_version": "2.0", "decisions": [consent()],
                "inactive_decisions": [],
            })
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_present_empty_store_does_not_match_absent_revision(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "decisions.json"
            raw = b'{"schema_version":"2.0","decisions":[],"inactive_decisions":[]}\n'
            path.write_bytes(raw)
            revision = resolver.decision_store_revision(path)
            self.assertEqual(revision["sha256"], digest(raw))
            with self.assertRaisesRegex(ValueError, "^dated_consent_store_changed$"):
                resolver.record_dated_consent_cas(path, consent(), None)
            self.assertEqual(path.read_bytes(), raw)

    def test_stale_or_duplicate_submission_never_overwrites(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "decisions.json"
            first = consent()
            resolver.record_dated_consent_cas(path, first, None)
            after_first = path.read_bytes()
            with self.assertRaisesRegex(ValueError, "^dated_consent_store_changed$"):
                resolver.record_dated_consent_cas(path, first, None)
            self.assertEqual(path.read_bytes(), after_first)

    def test_two_groups_from_one_display_revision_conflict(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "decisions.json"
            initial = {"schema_version": "2.0", "decisions": [], "inactive_decisions": []}
            path.write_text(json.dumps(initial) + "\n", encoding="utf-8")
            expected = resolver.decision_store_revision(path)["sha256"]
            resolver.record_dated_consent_cas(path, consent("1"), expected)
            after_first = path.read_bytes()
            with self.assertRaisesRegex(ValueError, "^dated_consent_store_changed$"):
                resolver.record_dated_consent_cas(path, consent("2"), expected)
            self.assertEqual(path.read_bytes(), after_first)

    def test_replacement_preserves_other_active_and_old_record_as_history(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "decisions.json"
            old = consent("1", "業務2024.csv")
            other = consent("2", "他業務2025.csv")
            initial = {
                "schema_version": "2.0", "decisions": [old, other],
                "inactive_decisions": [],
            }
            path.write_text(json.dumps(initial, ensure_ascii=False) + "\n", encoding="utf-8")
            expected = resolver.decision_store_revision(path)["sha256"]
            new = consent("1")
            resolver.record_dated_consent_cas(path, new, expected)
            value = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(value["decisions"], [new, other])
            self.assertEqual(value["inactive_decisions"], [old])

    def test_invalid_record_and_bad_existing_json_leave_bytes_unchanged(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "decisions.json"
            raw = b'{"schema_version":"2.0","decisions":[],"decisions":[]}\n'
            path.write_bytes(raw)
            expected = digest(raw)
            with self.assertRaises(ValueError):
                resolver.record_dated_consent_cas(path, consent(), expected)
            self.assertEqual(path.read_bytes(), raw)
            path.unlink()
            invalid = copy.deepcopy(consent())
            invalid["allow_ingest_index_answer"] = 1
            with self.assertRaisesRegex(ValueError, "^dated_consent_invalid$"):
                resolver.record_dated_consent_cas(path, invalid, None)
            self.assertFalse(path.exists())

    def test_symlink_store_is_rejected_without_touching_target(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "target.json"
            target.write_text("keep", encoding="utf-8")
            path = root / "decisions.json"
            path.symlink_to(target)
            with self.assertRaises(ValueError):
                resolver.decision_store_revision(path)
            self.assertEqual(target.read_text(encoding="utf-8"), "keep")


if __name__ == "__main__":
    unittest.main()
