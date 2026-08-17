"""Opaque new-data substitutions across the complete Phase 2.5 shadow path.

The generated organizations, files, fields, values, and identifiers are not
present in production registries.  This is a metamorphic coverage test, not an
answer-set evaluation, and it reads no competition question or answer file.
"""

from __future__ import annotations

import hashlib
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from build_data_catalog import Limits, build_data_catalog  # noqa: E402
from build_question_clause_ir import build_question_clause_ir  # noqa: E402
from execute_structured_resolution import execute_resolved_query  # noqa: E402
from resolve_catalog import resolve_catalog  # noqa: E402
from tests.test_data_catalog_engine import _document, _search_unit, _write_jsonl  # noqa: E402
from tests.test_question_understanding_engine import (  # noqa: E402
    compile_fixture,
    generic_compound_fixture,
    generic_list_fixture,
)
from validate_data_catalog import _read_entries, _read_snapshot  # noqa: E402


STAMP = "2026-08-17T00:00:00Z"
OPAQUE_LIST_CASES = 50
OPAQUE_COMPOUND_CASES = 20


class Phase25OpaqueHoldoutTest(unittest.TestCase):
    def test_seventy_unregistered_data_names_resolve_and_execute_without_code_changes(self):
        questions = []
        documents = []
        units = []
        expected_outputs: dict[str, tuple[object, ...]] = {}
        for index in range(OPAQUE_LIST_CASES):
            question, draft = generic_list_fixture(f"opaque_{index:02d}")
            qur = compile_fixture(question, draft)
            requested = qur["question_intent_contract"]["requested"]
            document = _document(index + 1000)
            document["source"].update(
                {
                    "relative_path": (
                        f"プロジェクト/{requested['scope']['location']}/"
                        f"{requested['scope']['container']}"
                    ),
                    "file_name": requested["scope"]["container"],
                    "extension": "xlsx",
                    "media_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                }
            )
            unit = _search_unit(document["document_id"])
            unit["search_unit_id"] = f"su_{index + 1000:032x}"
            unit["locator"] = {"row_index": 2, "sheet_name": f"Sheet{index:02d}"}
            filter_field = requested["scope"]["filters"][0]["field"]
            filter_value = requested["scope"]["filters"][0]["value"]
            target = requested["target"]["surface"]
            identifier = f"opaque_result_{index:02d}"
            unit["context"]["header_labels"] = [filter_field, target]
            unit["context"]["is_header_candidate"] = False
            text = f"{filter_field}: {filter_value}\n{target}: {identifier}"
            unit["text"] = {
                "search_text": text,
                "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                "char_count": len(text),
            }
            questions.append((question, qur))
            documents.append(document)
            units.append(unit)
            expected_outputs[question["question_id"]] = ((identifier,),)

        for index in range(OPAQUE_COMPOUND_CASES):
            question, draft = generic_compound_fixture(f"opaque_{index:02d}")
            qur = compile_fixture(question, draft)
            requested = qur["question_intent_contract"]["requested"]
            document = _document(index + 2000)
            document["source"].update(
                {
                    "relative_path": (
                        f"プロジェクト/{requested['scope']['location']}/"
                        f"{requested['scope']['container']}"
                    ),
                    "file_name": requested["scope"]["container"],
                    "extension": "csv",
                    "media_type": "text/csv",
                }
            )
            equality = requested["scope"]["filters"][0]
            threshold = requested["scope"]["filters"][1]
            metric = requested["operation_graph"]["nodes"][2]["fields"][0]
            identifier_field = requested["operation_graph"]["nodes"][5]["fields"][0]
            headers = [
                equality["field"],
                threshold["field"],
                metric,
                identifier_field,
            ]
            identifiers = []
            for row_index, metric_value in enumerate(("10", "20", "30"), start=1):
                unit = _search_unit(document["document_id"])
                unit["search_unit_id"] = f"su_{3000 + index * 10 + row_index:032x}"
                unit["locator"] = {
                    "row_index": row_index + 1,
                    "sheet_name": f"MetricSheet{index:02d}",
                }
                identifier = f"opaque_compound_{index:02d}_{row_index}"
                identifiers.append(identifier)
                values = [
                    str(equality["value"]),
                    str(int(threshold["value"]) + 100 + row_index),
                    metric_value,
                    identifier,
                ]
                unit["context"]["header_labels"] = headers
                unit["context"]["is_header_candidate"] = False
                text = "\n".join(
                    f"{header}: {value}" for header, value in zip(headers, values)
                )
                unit["text"] = {
                    "search_text": text,
                    "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                    "char_count": len(text),
                }
                units.append(unit)
            questions.append((question, qur))
            documents.append(document)
            expected_outputs[question["question_id"]] = (
                Decimal("20"),
                (identifiers[1],),
            )

        with tempfile.TemporaryDirectory() as raw_directory:
            directory = Path(raw_directory)
            documents_path = directory / "documents.jsonl"
            units_path = directory / "search_units.jsonl"
            entries_path = directory / "entries.jsonl"
            snapshot_path = directory / "snapshot.json"
            _write_jsonl(documents_path, documents)
            _write_jsonl(units_path, units)
            build_data_catalog(
                documents_path,
                units_path,
                entries_path,
                snapshot_path,
                generated_at=STAMP,
            )
            entries, _ = _read_entries(entries_path, Limits())
            snapshot = _read_snapshot(snapshot_path, Limits())
            operator_signatures = set()
            resolution_ids = set()
            for question, qur in questions:
                qic = qur["question_intent_contract"]
                clause_ir = build_question_clause_ir(
                    question, generated_at=STAMP, qic=qic
                )
                resolution = resolve_catalog(
                    qur, clause_ir, entries, snapshot, generated_at=STAMP
                )
                self.assertEqual("resolved", resolution["final_status"])
                execution = execute_resolved_query(
                    qur, resolution, entries, units_path
                )
                self.assertEqual(
                    expected_outputs[question["question_id"]],
                    tuple(
                        item["value"] for item in execution.requested_outputs
                    ),
                )
                operator_signatures.add(
                    tuple(
                        node["operator"]
                        for node in qic["requested"]["operation_graph"]["nodes"]
                    )
                )
                resolution_ids.add(resolution["catalog_resolution_run_id"])
            self.assertEqual(
                {
                    ("filter", "project"),
                    (
                        "filter",
                        "filter",
                        "project",
                        "mean",
                        "argmin_all",
                        "project",
                    ),
                },
                operator_signatures,
            )
            self.assertEqual(
                OPAQUE_LIST_CASES + OPAQUE_COMPOUND_CASES,
                len(resolution_ids),
            )
            catalog_text = entries_path.read_text(encoding="utf-8")
            self.assertNotIn("opaque_result_", catalog_text)
            self.assertNotIn("opaque_compound_", catalog_text)


if __name__ == "__main__":
    unittest.main()
