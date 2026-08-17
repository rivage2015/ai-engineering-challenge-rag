"""End-to-end tests for the question-independent Data Catalog engine.

All records are synthetic source metadata.  No question, answer, evaluation,
or competition dataset is read.
"""

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

from build_data_catalog import (
    CatalogContractError,
    Limits,
    _loads_strict,
    build_data_catalog,
)
from validate_data_catalog import validate_data_catalog


STAMP = "2026-08-17T00:00:00Z"


def _document(index: int, *, sha: str | None = None) -> dict[str, object]:
    file_name = f"dataset_{index}.parquet"
    return {
        "schema_version": "0.1",
        "record_type": "document",
        "document_id": f"doc_{index:032x}",
        "source": {
            "relative_path": f"プロジェクト/未知組織{index}/{file_name}",
            "file_name": file_name,
            "extension": "parquet",
            "media_type": "application/octet-stream",
            "size_bytes": 123 + index,
            "sha256": sha or f"{index + 20:064x}",
        },
        "extraction": {
            "status": "success",
            "parser": "synthetic-source-parser",
            "parser_version": "0.1",
            "extracted_at": STAMP,
            "warnings": [],
            "errors": [],
        },
    }


def _search_unit(document_id: str) -> dict[str, object]:
    text = "SECRET_ROW_VALUE must not enter the catalog"
    return {
        "schema_version": "0.1",
        "record_type": "search_unit",
        "search_unit_id": "su_" + "a" * 32,
        "document_id": document_id,
        "unit_type": "table_row",
        "source_evidence_ids": ["ev_" + "b" * 32],
        "locator": {
            "sheet_name": "OrbitSheet",
            "table_index": 1,
            "row_index": 2,
        },
        "text": {
            "search_text": text,
            "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "char_count": len(text),
        },
        "context": {
            "container_kind": "table",
            "header_labels": ["ProjectID", "RiskState"],
            "header_evidence_ids": ["ev_" + "c" * 32, "ev_" + "d" * 32],
            "header_method": "source_header_row",
            "is_header_candidate": False,
        },
        "provenance": {
            "builder": "search-unit-builder",
            "builder_version": "0.1",
            "generated_at": STAMP,
            "deterministic": True,
        },
    }


def _write_jsonl(path: Path, records: list[dict[str, object]]) -> None:
    path.write_text(
        "".join(
            json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            + "\n"
            for record in records
        ),
        encoding="utf-8",
    )


class DataCatalogEngineTest(unittest.TestCase):
    def _paths(self, directory: Path):
        return (
            directory / "documents.jsonl",
            directory / "search_units.jsonl",
            directory / "data-catalog-entries.jsonl",
            directory / "data-catalog-snapshot.json",
        )

    def test_build_is_byte_stable_value_free_and_source_validated(self):
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = Path(raw_directory)
            documents, search_units, entries, snapshot = self._paths(directory)
            first_document = _document(1)
            _write_jsonl(documents, [first_document, _document(2)])
            _write_jsonl(search_units, [_search_unit(first_document["document_id"])])

            first = build_data_catalog(documents, search_units, entries, snapshot)
            first_entries = entries.read_bytes()
            first_snapshot = snapshot.read_bytes()
            second = build_data_catalog(documents, search_units, entries, snapshot)
            self.assertEqual(first_entries, entries.read_bytes())
            self.assertEqual(first_snapshot, snapshot.read_bytes())
            self.assertEqual(first.snapshot_id, second.snapshot_id)
            self.assertEqual([], validate_data_catalog(
                entries,
                snapshot,
                documents_path=documents,
                search_units_path=search_units,
            ))

            rendered = entries.read_text(encoding="utf-8")
            self.assertNotIn("SECRET_ROW_VALUE", rendered)
            self.assertNotIn("search_text", rendered)
            records = [json.loads(line) for line in rendered.splitlines()]
            self.assertEqual(3, len(records))  # two documents plus one table
            table = next(
                item for item in records if item["address"]["container_kind"] == "table"
            )
            self.assertEqual(["lexical"], table["capabilities"]["retrieval_channels"])
            self.assertEqual([], table["capabilities"]["predicate_operators"])
            self.assertEqual([], table["capabilities"]["graph_operators"])
            self.assertEqual(["ProjectID", "RiskState"], [f["surface"] for f in table["fields"]])
            self.assertTrue(
                all(ref.startswith("su_") for field in table["fields"] for ref in field["source_refs"])
            )
            self.assertTrue(
                all(len(field["source_refs"]) == 1 for field in table["fields"])
            )

    def test_source_sha_changes_only_affected_entries_and_snapshot(self):
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = Path(raw_directory)
            documents, search_units, entries, snapshot = self._paths(directory)
            first_document = _document(1)
            second_document = _document(2)
            _write_jsonl(documents, [first_document, second_document])
            _write_jsonl(search_units, [_search_unit(first_document["document_id"])])
            first = build_data_catalog(documents, search_units, entries, snapshot)
            before = [json.loads(line) for line in entries.read_text(encoding="utf-8").splitlines()]

            changed = copy.deepcopy(first_document)
            changed["source"]["sha256"] = "f" * 64
            _write_jsonl(documents, [changed, second_document])
            second = build_data_catalog(documents, search_units, entries, snapshot)
            after = [json.loads(line) for line in entries.read_text(encoding="utf-8").splitlines()]
            self.assertNotEqual(first.snapshot_id, second.snapshot_id)
            stable_before = {
                item["data_catalog_entry_id"]
                for item in before
                if item["document_id"] == second_document["document_id"]
            }
            stable_after = {
                item["data_catalog_entry_id"]
                for item in after
                if item["document_id"] == second_document["document_id"]
            }
            self.assertEqual(stable_before, stable_after)
            changed_before = {
                item["data_catalog_entry_id"]
                for item in before
                if item["document_id"] == first_document["document_id"]
            }
            changed_after = {
                item["data_catalog_entry_id"]
                for item in after
                if item["document_id"] == first_document["document_id"]
            }
            self.assertTrue(changed_before.isdisjoint(changed_after))

    def test_exact_structured_rows_publish_types_and_capability_without_values(self):
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = Path(raw_directory)
            documents, search_units, entries, snapshot = self._paths(directory)
            document = _document(1)
            unit = _search_unit(document["document_id"])
            unit["context"]["is_header_candidate"] = False
            row_text = "ProjectID: opaque_9001\nRiskState: Green\nAmount: 42.5"
            unit["context"]["header_labels"] = [
                "ProjectID",
                "RiskState",
                "Amount",
            ]
            unit["text"] = {
                "search_text": row_text,
                "sha256": hashlib.sha256(row_text.encode("utf-8")).hexdigest(),
                "char_count": len(row_text),
            }
            _write_jsonl(documents, [document])
            _write_jsonl(search_units, [unit])
            build_data_catalog(documents, search_units, entries, snapshot)
            records = [
                json.loads(line)
                for line in entries.read_text(encoding="utf-8").splitlines()
            ]
            table = next(item for item in records if item["fields"])
            self.assertEqual(
                ["lexical", "structured"],
                table["capabilities"]["retrieval_channels"],
            )
            self.assertEqual(
                ["string", "string", "number"],
                [field["data_type"] for field in table["fields"]],
            )
            rendered = entries.read_text(encoding="utf-8")
            self.assertNotIn("opaque_9001", rendered)
            self.assertNotIn("Green", rendered)
            self.assertNotIn("42.5", rendered)

    def test_validator_rejects_stream_and_local_id_tampering(self):
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = Path(raw_directory)
            documents, search_units, entries, snapshot = self._paths(directory)
            first_document = _document(1)
            _write_jsonl(documents, [first_document])
            _write_jsonl(search_units, [_search_unit(first_document["document_id"])])
            build_data_catalog(documents, search_units, entries, snapshot)

            records = [json.loads(line) for line in entries.read_text(encoding="utf-8").splitlines()]
            table = next(item for item in records if item["fields"])
            table["fields"][0]["field_id"] = "dcf_" + "f" * 32
            _write_jsonl(entries, records)
            errors = validate_data_catalog(entries, snapshot)
            self.assertTrue(any("field ID" in error or "SHA-256" in error for error in errors), errors)

    def test_question_answer_paths_and_resource_exhaustion_are_rejected(self):
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = Path(raw_directory)
            forbidden_documents = directory / "questions_valid.jsonl"
            search_units = directory / "search_units.jsonl"
            entries = directory / "entries.jsonl"
            snapshot = directory / "snapshot.json"
            document = _document(1)
            _write_jsonl(forbidden_documents, [document])
            _write_jsonl(search_units, [_search_unit(document["document_id"])])
            with self.assertRaises(CatalogContractError):
                build_data_catalog(
                    forbidden_documents, search_units, entries, snapshot
                )
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = Path(raw_directory)
            documents, search_units, entries, snapshot = self._paths(directory)
            legitimate = _document(1)
            legitimate["source"]["relative_path"] = (
                "プロジェクト/白峰信用リスク評価株式会社/評価報告書.parquet"
            )
            legitimate["source"]["file_name"] = "評価報告書.parquet"
            _write_jsonl(documents, [legitimate])
            _write_jsonl(search_units, [_search_unit(legitimate["document_id"])])
            build_data_catalog(documents, search_units, entries, snapshot)
            self.assertEqual(
                [],
                validate_data_catalog(
                    entries,
                    snapshot,
                    documents_path=documents,
                    search_units_path=search_units,
                ),
            )

            forbidden_source = copy.deepcopy(legitimate)
            forbidden_source["source"]["relative_path"] = (
                "share/質問回答/questions_valid.csv"
            )
            forbidden_source["source"]["file_name"] = "questions_valid.csv"
            _write_jsonl(documents, [forbidden_source])
            with self.assertRaises(CatalogContractError):
                build_data_catalog(documents, search_units, entries, snapshot)
        with self.assertRaises(CatalogContractError):
            _loads_strict(
                ("[" * 1000 + "0" + "]" * 1000).encode("utf-8"),
                source="deep",
                limits=Limits(max_depth=64),
            )

    def test_archive_root_member_is_canonicalized_but_traversal_is_rejected(self):
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = Path(raw_directory)
            documents, search_units, entries, snapshot = self._paths(directory)
            document = _document(1)
            unit = _search_unit(document["document_id"])
            unit["locator"]["source_member"] = "/word/footer1.xml"
            _write_jsonl(documents, [document])
            _write_jsonl(search_units, [unit])
            build_data_catalog(documents, search_units, entries, snapshot)
            catalog = [
                json.loads(line)
                for line in entries.read_text(encoding="utf-8").splitlines()
            ]
            table = next(item for item in catalog if item["fields"])
            self.assertEqual("word/footer1.xml", table["address"]["source_member"])

            unit["locator"]["source_member"] = "../private/answer.xml"
            _write_jsonl(search_units, [unit])
            with self.assertRaises(CatalogContractError):
                build_data_catalog(documents, search_units, entries, snapshot)


if __name__ == "__main__":
    unittest.main()
