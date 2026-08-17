"""Shadow wiring tests; no retrieval backend or answer artifact is used."""

from __future__ import annotations

import copy
import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from build_data_catalog import (  # noqa: E402
    BUILDER_NAME,
    BUILDER_VERSION,
    _stable_id,
    canonical_json_bytes,
)
from build_question_understanding import (  # noqa: E402
    build_question_understanding,
    derive_supported_intent_draft,
    validate_understanding_run,
)
from run_phase25_shadow import (  # noqa: E402
    main,
    run_phase25_shadow,
    run_phase25_shadow_files,
)
from tests.test_catalog_resolver_engine import _catalog_for_question  # noqa: E402
from tests.test_question_understanding_engine import (  # noqa: E402
    compile_fixture,
    generic_list_fixture,
)
from validate_data_catalog import _snapshot_identity  # noqa: E402


STAMP = "2026-08-17T00:00:00Z"


def _write_jsonl(path: Path, records: list[dict[str, object]]) -> None:
    path.write_bytes(b"".join(canonical_json_bytes(record) + b"\n" for record in records))


class Phase25ShadowGateTest(unittest.TestCase):
    def _inputs(self):
        question, draft = generic_list_fixture("shadow_generic")
        qur = compile_fixture(question, draft)
        entries, snapshot = _catalog_for_question(
            question, qur["question_intent_contract"]
        )
        entry = entries[0]
        for label in entry["scope_labels"]:
            label["label_id"] = _stable_id(
                "dcl",
                {
                    "data_catalog_entry_id": entry["data_catalog_entry_id"],
                    **{key: value for key, value in label.items() if key != "label_id"},
                },
            )
        for field in entry["fields"]:
            field["field_id"] = _stable_id(
                "dcf",
                {
                    "data_catalog_entry_id": entry["data_catalog_entry_id"],
                    **{key: value for key, value in field.items() if key != "field_id"},
                },
            )
        entry["provenance"]["generated_at"] = STAMP
        entry["provenance"]["builder"] = BUILDER_NAME
        entry["provenance"]["builder_version"] = BUILDER_VERSION
        stream = canonical_json_bytes(entry) + b"\n"
        import hashlib

        snapshot["entry_stream"]["sha256"] = hashlib.sha256(stream).hexdigest()
        snapshot["provenance"]["generated_at"] = STAMP
        snapshot["provenance"]["builder"] = BUILDER_NAME
        snapshot["provenance"]["builder_version"] = BUILDER_VERSION
        snapshot["data_catalog_snapshot_id"] = _stable_id(
            "dcs", _snapshot_identity(snapshot)
        )
        return qur, entries, snapshot

    def test_shadow_resolves_without_starting_retrieval_and_is_stable(self):
        qur, entries, snapshot = self._inputs()
        first = run_phase25_shadow(
            [qur], entries, snapshot, generated_at=STAMP
        )
        second = run_phase25_shadow(
            [qur], entries, snapshot, generated_at=STAMP
        )
        self.assertEqual(1, first.ready_count)
        self.assertEqual(0, first.hold_count)
        self.assertEqual(first.clause_irs, second.clause_irs)
        self.assertEqual(first.resolutions, second.resolutions)
        self.assertEqual("complete", first.clause_irs[0]["coverage"]["status"])
        self.assertEqual("resolved", first.resolutions[0]["final_status"])

    def test_unicode_container_with_ascii_extension_is_not_truncated(self):
        question = {
            "question_id": "q_unicode_container_shadow",
            "original_question": (
                "組織Ωの会議録_2025-09-26.docxにおいて、"
                "ActionがOpenに一致するTaskIDをすべて挙げてください。"
            ),
        }
        self.assertIsNotNone(derive_supported_intent_draft(question))
        qur = build_question_understanding(question, generated_at=STAMP)
        self.assertEqual("ready_for_retrieval", qur["final_status"])
        self.assertEqual([], validate_understanding_run(qur))
        self.assertEqual(
            "会議録_2025-09-26.docx",
            qur["question_intent_contract"]["requested"]["scope"]["container"],
        )

    def test_shadow_holds_catalog_without_execution_capability(self):
        qur, entries, snapshot = self._inputs()
        lexical = copy.deepcopy(entries)
        lexical[0]["capabilities"] = {
            "retrieval_channels": ["lexical"],
            "predicate_operators": [],
            "graph_operators": [],
        }
        payload = canonical_json_bytes(lexical[0]) + b"\n"
        import hashlib

        snapshot = copy.deepcopy(snapshot)
        snapshot["entry_stream"]["sha256"] = hashlib.sha256(payload).hexdigest()
        result = run_phase25_shadow(
            [qur], lexical, snapshot, generated_at=STAMP
        )
        self.assertEqual(0, result.ready_count)
        self.assertEqual(1, result.hold_count)
        self.assertEqual("abstained", result.resolutions[0]["final_status"])
        self.assertIn(
            "capability_unsupported", result.resolutions[0]["reason_codes"]
        )

    def test_file_api_and_cli_write_only_clause_and_resolution_records(self):
        qur, entries, snapshot = self._inputs()
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = Path(raw_directory)
            qur_path = directory / "qur.jsonl"
            entries_path = directory / "entries.jsonl"
            snapshot_path = directory / "snapshot.json"
            clause_path = directory / "clause.jsonl"
            resolution_path = directory / "resolution.jsonl"
            _write_jsonl(qur_path, [qur])
            _write_jsonl(entries_path, entries)
            snapshot_path.write_bytes(canonical_json_bytes(snapshot) + b"\n")
            direct = run_phase25_shadow_files(
                qur_path,
                entries_path,
                snapshot_path,
                generated_at=STAMP,
            )
            self.assertEqual(1, direct.ready_count)
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(
                io.StringIO()
            ):
                exit_code = main(
                    [
                        "--qur",
                        str(qur_path),
                        "--entries",
                        str(entries_path),
                        "--snapshot",
                        str(snapshot_path),
                        "--clause-ir-out",
                        str(clause_path),
                        "--resolution-out",
                        str(resolution_path),
                        "--generated-at",
                        STAMP,
                    ]
                )
            self.assertEqual(0, exit_code)
            self.assertEqual(
                direct.clause_irs[0], json.loads(clause_path.read_text(encoding="utf-8"))
            )
            self.assertEqual(
                direct.resolutions[0],
                json.loads(resolution_path.read_text(encoding="utf-8")),
            )
            rendered = clause_path.read_text(encoding="utf-8") + resolution_path.read_text(encoding="utf-8")
            self.assertNotIn("final_answer", rendered)
            self.assertNotIn("retrieval_hits", rendered)

    def test_duplicate_question_identity_and_output_alias_fail_closed(self):
        qur, entries, snapshot = self._inputs()
        with self.assertRaises(ValueError):
            run_phase25_shadow(
                [qur, copy.deepcopy(qur)], entries, snapshot, generated_at=STAMP
            )
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = Path(raw_directory)
            qur_path = directory / "qur.jsonl"
            entries_path = directory / "entries.jsonl"
            snapshot_path = directory / "snapshot.json"
            output = directory / "same.jsonl"
            _write_jsonl(qur_path, [qur])
            _write_jsonl(entries_path, entries)
            snapshot_path.write_bytes(canonical_json_bytes(snapshot) + b"\n")
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(
                io.StringIO()
            ):
                exit_code = main(
                    [
                        "--qur",
                        str(qur_path),
                        "--entries",
                        str(entries_path),
                        "--snapshot",
                        str(snapshot_path),
                        "--clause-ir-out",
                        str(output),
                        "--resolution-out",
                        str(output),
                    ]
                )
            self.assertEqual(1, exit_code)


if __name__ == "__main__":
    unittest.main()
