"""End-to-end resolved Catalog binding to SearchUnit execution tests."""

from __future__ import annotations

import copy
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from build_data_catalog import build_data_catalog  # noqa: E402
from build_question_clause_ir import build_question_clause_ir  # noqa: E402
from execute_structured_resolution import (  # noqa: E402
    execute_resolved_query,
    execute_resolved_query_files,
)
from resolve_catalog import ResolutionError, resolve_catalog_files  # noqa: E402
from structured_search_units import StructuredRowError  # noqa: E402
from tests.test_data_catalog_engine import _document, _search_unit, _write_jsonl  # noqa: E402
from tests.test_question_understanding_engine import (  # noqa: E402
    compile_fixture,
    generic_list_fixture,
)


STAMP = "2026-08-17T00:00:00Z"


class StructuredResolutionExecutorTest(unittest.TestCase):
    def _files(self, directory: Path):
        question, draft = generic_list_fixture("executor_opaque")
        qur = compile_fixture(question, draft)
        qic = qur["question_intent_contract"]
        requested = qic["requested"]
        filter_field = requested["scope"]["filters"][0]["field"]
        filter_value = requested["scope"]["filters"][0]["value"]
        target = requested["target"]["surface"]
        document = _document(1)
        document["source"]["relative_path"] = (
            f"プロジェクト/{requested['scope']['location']}/"
            f"{requested['scope']['container']}"
        )
        document["source"]["file_name"] = requested["scope"]["container"]
        document["source"]["extension"] = "xlsx"
        document["source"]["media_type"] = (
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
        units = []
        for index, (identifier, value) in enumerate(
            [("opaque_1", filter_value), ("opaque_2", "other"), ("opaque_3", filter_value)],
            start=1,
        ):
            unit = _search_unit(document["document_id"])
            unit["search_unit_id"] = f"su_{index:032x}"
            unit["locator"] = {"row_index": index, "sheet_name": "SheetX"}
            unit["context"]["header_labels"] = [filter_field, target]
            unit["context"]["is_header_candidate"] = False
            text = f"{filter_field}: {value}\n{target}: {identifier}"
            unit["text"] = {
                "search_text": text,
                "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                "char_count": len(text),
            }
            units.append(unit)

        paths = {
            "documents": directory / "documents.jsonl",
            "search_units": directory / "search_units.jsonl",
            "entries": directory / "entries.jsonl",
            "snapshot": directory / "snapshot.json",
            "qur": directory / "qur.jsonl",
            "clause": directory / "clause.jsonl",
            "resolution": directory / "resolution.jsonl",
        }
        _write_jsonl(paths["documents"], [document])
        _write_jsonl(paths["search_units"], units)
        _write_jsonl(paths["qur"], [qur])
        clause = build_question_clause_ir(question, generated_at=STAMP, qic=qic)
        _write_jsonl(paths["clause"], [clause])
        build_data_catalog(
            paths["documents"],
            paths["search_units"],
            paths["entries"],
            paths["snapshot"],
        )
        resolution = resolve_catalog_files(
            paths["qur"],
            paths["clause"],
            paths["entries"],
            paths["snapshot"],
            generated_at=STAMP,
        )
        self.assertEqual("resolved", resolution["final_status"])
        _write_jsonl(paths["resolution"], [resolution])
        return paths, qur, resolution

    def test_resolved_unknown_source_executes_bound_rows_only(self):
        with tempfile.TemporaryDirectory() as raw_directory:
            paths, _, _ = self._files(Path(raw_directory))
            result = execute_resolved_query_files(
                paths["qur"],
                paths["clause"],
                paths["resolution"],
                paths["entries"],
                paths["snapshot"],
                paths["search_units"],
            )
            self.assertEqual(("opaque_1", "opaque_3"), result.requested_outputs[0]["value"])
            self.assertEqual(3, len(result.source_search_unit_ids))

    def test_row_tamper_and_nonresolved_run_fail_closed(self):
        with tempfile.TemporaryDirectory() as raw_directory:
            paths, qur, resolution = self._files(Path(raw_directory))
            units = [
                json.loads(line)
                for line in paths["search_units"].read_text(encoding="utf-8").splitlines()
            ]
            units[0]["text"]["search_text"] += "tamper"
            _write_jsonl(paths["search_units"], units)
            with self.assertRaises(StructuredRowError):
                execute_resolved_query_files(
                    paths["qur"],
                    paths["clause"],
                    paths["resolution"],
                    paths["entries"],
                    paths["snapshot"],
                    paths["search_units"],
                )

            # Updating the row-local checksum is still insufficient: the
            # complete SearchUnit stream must equal the Catalog snapshot input.
            changed_text = units[0]["text"]["search_text"]
            units[0]["text"]["sha256"] = hashlib.sha256(
                changed_text.encode("utf-8")
            ).hexdigest()
            units[0]["text"]["char_count"] = len(changed_text)
            _write_jsonl(paths["search_units"], units)
            with self.assertRaisesRegex(StructuredRowError, "snapshot input digest"):
                execute_resolved_query_files(
                    paths["qur"],
                    paths["clause"],
                    paths["resolution"],
                    paths["entries"],
                    paths["snapshot"],
                    paths["search_units"],
                )

            held = copy.deepcopy(resolution)
            held["final_status"] = "abstained"
            with self.assertRaises(ResolutionError):
                execute_resolved_query(qur, held, [], paths["search_units"])


if __name__ == "__main__":
    unittest.main()
