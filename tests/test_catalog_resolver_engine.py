"""Deterministic Catalog Resolver tests using only synthetic metadata."""

from __future__ import annotations

import copy
import hashlib
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from build_data_catalog import build_data_catalog, canonical_json_bytes, normalize_label
from build_question_clause_ir import build_question_clause_ir
from resolve_catalog import resolve_catalog, resolve_catalog_files, validate_resolution_record_local
from validate_catalog_resolution import validate_catalog_resolution
from tests.test_data_catalog_contracts import catalog_snapshot, searchable_entry
from tests.test_data_catalog_engine import _search_unit, _write_jsonl
from tests.test_question_understanding_engine import (
    compile_fixture,
    generic_compound_fixture,
    generic_list_fixture,
)


STAMP = "2026-08-17T00:00:00Z"


def _catalog_for_question(question: dict[str, object], qic: dict[str, object]):
    requested = qic["requested"]
    entry = searchable_entry()
    entry["data_catalog_entry_id"] = "dce_" + "1" * 32
    entry["document_id"] = "doc_" + "2" * 32
    entry["source_identity"].update(
        {
            "relative_path": f"プロジェクト/{requested['scope']['location']}/{requested['scope']['container']}",
            "file_name": requested["scope"]["container"],
            "extension": "xlsx",
            "media_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "sha256": "3" * 64,
        }
    )
    location = requested["scope"]["location"]
    container = requested["scope"]["container"]
    entry["scope_labels"] = [
        {
            "label_id": "dcl_" + "4" * 32,
            "role": "location",
            "surface": location,
            "normalized": normalize_label(location),
            "source_kind": "path_component",
            "source_refs": [entry["document_id"]],
        },
        {
            "label_id": "dcl_" + "5" * 32,
            "role": "container",
            "surface": container,
            "normalized": normalize_label(container),
            "source_kind": "file_name",
            "source_refs": [entry["document_id"]],
        },
    ]
    field_names = [
        requested["scope"]["filters"][0]["field"],
        requested["target"]["surface"],
    ]
    entry["fields"] = [
        {
            "field_id": f"dcf_{index + 6:032x}",
            "surface": name,
            "normalized": normalize_label(name),
            "ordinal": index,
            "data_type": "string",
            "unit": None,
            "source_refs": [entry["document_id"]],
        }
        for index, name in enumerate(field_names)
    ]
    entry["capabilities"] = {
        "retrieval_channels": ["structured"],
        "predicate_operators": ["eq"],
        "graph_operators": ["filter", "project", "list"],
    }
    entry["provenance"]["input_refs"] = [entry["document_id"]]
    raw = canonical_json_bytes(entry) + b"\n"
    snapshot = catalog_snapshot()
    snapshot["data_catalog_snapshot_id"] = "dcs_" + "8" * 32
    snapshot["entry_stream"].update(
        {
            "relative_path": "entries.jsonl",
            "sha256": hashlib.sha256(raw).hexdigest(),
            "record_count": 1,
        }
    )
    snapshot["inputs"] = [
        {
            "record_type": "document",
            "schema_version": "0.1",
            "sha256": "9" * 64,
            "record_count": 1,
        }
    ]
    return [entry], snapshot


class CatalogResolverEngineTest(unittest.TestCase):
    def _inputs(self):
        question, draft = generic_list_fixture("resolver_engine")
        qur = compile_fixture(question, draft)
        qic = qur["question_intent_contract"]
        clause_ir = build_question_clause_ir(
            question, generated_at=STAMP, qic=qic
        )
        entries, snapshot = _catalog_for_question(question, qic)
        return qur, clause_ir, entries, snapshot

    def test_unique_exact_catalog_binding_resolves(self):
        qur, clause_ir, entries, snapshot = self._inputs()
        record = resolve_catalog(
            qur, clause_ir, entries, snapshot, generated_at=STAMP
        )
        self.assertEqual("resolved", record["final_status"])
        self.assertEqual([], record["reason_codes"])
        binding = record["branch_resolutions"][0]["candidate_bindings"][0]
        self.assertEqual("resolved", binding["status"])
        self.assertTrue(binding["target_bindings"])
        self.assertTrue(binding["scope_bindings"])
        self.assertTrue(binding["field_bindings"])
        self.assertTrue(binding["capability_checks"])
        self.assertEqual([], validate_resolution_record_local(record))

    def test_duplicate_exact_entries_are_not_ranked_or_silently_selected(self):
        qur, clause_ir, entries, snapshot = self._inputs()
        duplicate = copy.deepcopy(entries[0])
        duplicate["data_catalog_entry_id"] = "dce_" + "a" * 32
        duplicate["scope_labels"][0]["label_id"] = "dcl_" + "b" * 32
        duplicate["scope_labels"][1]["label_id"] = "dcl_" + "c" * 32
        duplicate["fields"][0]["field_id"] = "dcf_" + "d" * 32
        duplicate["fields"][1]["field_id"] = "dcf_" + "e" * 32
        entries = sorted([entries[0], duplicate], key=lambda item: item["data_catalog_entry_id"])
        raw = b"".join(canonical_json_bytes(item) + b"\n" for item in entries)
        snapshot["entry_stream"]["record_count"] = 2
        snapshot["entry_stream"]["sha256"] = hashlib.sha256(raw).hexdigest()
        record = resolve_catalog(
            qur, clause_ir, entries, snapshot, generated_at=STAMP
        )
        self.assertEqual("clarification_required", record["final_status"])
        self.assertEqual("ambiguous", record["branch_resolutions"][0]["status"])
        self.assertEqual(2, len(record["branch_resolutions"][0]["candidate_bindings"]))

    def test_missing_field_and_unsupported_capability_hold(self):
        qur, clause_ir, entries, snapshot = self._inputs()
        missing = copy.deepcopy(entries)
        missing[0]["fields"] = missing[0]["fields"][:1]
        raw = canonical_json_bytes(missing[0]) + b"\n"
        snapshot_missing = copy.deepcopy(snapshot)
        snapshot_missing["entry_stream"]["sha256"] = hashlib.sha256(raw).hexdigest()
        missing_record = resolve_catalog(
            qur, clause_ir, missing, snapshot_missing, generated_at=STAMP
        )
        self.assertEqual("clarification_required", missing_record["final_status"])
        self.assertIn("field_missing", missing_record["reason_codes"])

        lexical = copy.deepcopy(entries)
        lexical[0]["capabilities"] = {
            "retrieval_channels": ["lexical"],
            "predicate_operators": [],
            "graph_operators": [],
        }
        raw = canonical_json_bytes(lexical[0]) + b"\n"
        snapshot_lexical = copy.deepcopy(snapshot)
        snapshot_lexical["entry_stream"]["sha256"] = hashlib.sha256(raw).hexdigest()
        unsupported = resolve_catalog(
            qur, clause_ir, lexical, snapshot_lexical, generated_at=STAMP
        )
        self.assertEqual("abstained", unsupported["final_status"])
        self.assertIn("capability_unsupported", unsupported["reason_codes"])

        missing_list = copy.deepcopy(entries)
        missing_list[0]["capabilities"]["graph_operators"] = ["filter", "project"]
        raw = canonical_json_bytes(missing_list[0]) + b"\n"
        snapshot_list = copy.deepcopy(snapshot)
        snapshot_list["entry_stream"]["sha256"] = hashlib.sha256(raw).hexdigest()
        all_without_list = resolve_catalog(
            qur, clause_ir, missing_list, snapshot_list, generated_at=STAMP
        )
        self.assertEqual("abstained", all_without_list["final_status"])
        checks = all_without_list["branch_resolutions"][0]["candidate_bindings"][0]["capability_checks"]
        self.assertTrue(
            any(
                check["required_capability"] == "list"
                and check["status"] == "fail"
                for check in checks
            )
        )

    def test_numeric_operations_require_catalog_numeric_types(self):
        question, draft = generic_compound_fixture("resolver_numeric")
        qur = compile_fixture(question, draft)
        qic = qur["question_intent_contract"]
        clause_ir = build_question_clause_ir(question, generated_at=STAMP, qic=qic)
        requested = qic["requested"]
        entry = searchable_entry()
        entry["data_catalog_entry_id"] = "dce_" + "1" * 32
        entry["document_id"] = "doc_" + "2" * 32
        location = requested["scope"]["location"]
        container = requested["scope"]["container"]
        entry["source_identity"].update(
            {
                "relative_path": f"プロジェクト/{location}/{container}",
                "file_name": container,
                "extension": "csv",
                "media_type": "text/csv",
            }
        )
        entry["scope_labels"] = [
            {
                "label_id": "dcl_" + "3" * 32,
                "role": "location",
                "surface": location,
                "normalized": normalize_label(location),
                "source_kind": "path_component",
                "source_refs": [entry["document_id"]],
            },
            {
                "label_id": "dcl_" + "4" * 32,
                "role": "container",
                "surface": container,
                "normalized": normalize_label(container),
                "source_kind": "file_name",
                "source_refs": [entry["document_id"]],
            },
        ]
        field_names = [
            requested["scope"]["filters"][0]["field"],
            requested["scope"]["filters"][1]["field"],
            requested["operation_graph"]["nodes"][2]["fields"][0],
            requested["operation_graph"]["nodes"][5]["fields"][0],
        ]
        entry["fields"] = [
            {
                "field_id": f"dcf_{index + 5:032x}",
                "surface": name,
                "normalized": normalize_label(name),
                "ordinal": index,
                "data_type": "number" if index in {1, 2} else "string",
                "unit": None,
                "source_refs": [entry["document_id"]],
            }
            for index, name in enumerate(field_names)
        ]
        entry["capabilities"] = {
            "retrieval_channels": ["structured"],
            "predicate_operators": ["eq", "gt"],
            "graph_operators": ["filter", "project", "mean", "argmin_all", "list"],
        }
        entry["provenance"]["input_refs"] = [entry["document_id"]]

        def snapshot_for(value):
            raw = canonical_json_bytes(value) + b"\n"
            result = catalog_snapshot()
            result["data_catalog_snapshot_id"] = "dcs_" + "8" * 32
            result["entry_stream"].update(
                {
                    "relative_path": "entries.jsonl",
                    "sha256": hashlib.sha256(raw).hexdigest(),
                    "record_count": 1,
                }
            )
            result["inputs"] = [{"record_type": "document", "schema_version": "0.1", "sha256": "9" * 64, "record_count": 1}]
            return result

        resolved = resolve_catalog(
            qur, clause_ir, [entry], snapshot_for(entry), generated_at=STAMP
        )
        self.assertEqual("resolved", resolved["final_status"])
        unsafe = copy.deepcopy(entry)
        next(field for field in unsafe["fields"] if field["surface"] == field_names[2])["data_type"] = "unknown"
        held = resolve_catalog(
            qur, clause_ir, [unsafe], snapshot_for(unsafe), generated_at=STAMP
        )
        self.assertEqual("abstained", held["final_status"])
        self.assertIn("capability_unsupported", held["reason_codes"])

    def test_resolution_and_binding_id_tampering_is_rejected(self):
        qur, clause_ir, entries, snapshot = self._inputs()
        record = resolve_catalog(
            qur, clause_ir, entries, snapshot, generated_at=STAMP
        )
        record["catalog_resolution_run_id"] = "crr_" + "f" * 32
        self.assertTrue(validate_resolution_record_local(record))
        record = resolve_catalog(
            qur, clause_ir, entries, snapshot, generated_at=STAMP
        )
        record["branch_resolutions"][0]["candidate_bindings"][0]["binding_id"] = (
            "crb_" + "f" * 32
        )
        self.assertTrue(validate_resolution_record_local(record))

    def test_new_source_names_flow_end_to_end_without_code_changes(self):
        question, draft = generic_list_fixture("new_source_omega")
        qur = compile_fixture(question, draft)
        qic = qur["question_intent_contract"]
        clause_ir = build_question_clause_ir(
            question, generated_at=STAMP, qic=qic
        )
        requested = qic["requested"]
        document_id = "doc_" + "9" * 32
        document = {
            "schema_version": "0.1",
            "record_type": "document",
            "document_id": document_id,
            "source": {
                "relative_path": (
                    f"プロジェクト/{requested['scope']['location']}/"
                    f"{requested['scope']['container']}"
                ),
                "file_name": requested["scope"]["container"],
                "extension": "xlsx",
                "media_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                "size_bytes": 999,
                "sha256": "a" * 64,
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
        unit = _search_unit(document_id)
        unit["context"]["header_labels"] = [
            requested["scope"]["filters"][0]["field"],
            requested["target"]["surface"],
        ]
        unit["context"]["is_header_candidate"] = False
        row_text = (
            f"{requested['scope']['filters'][0]['field']}: "
            f"{requested['scope']['filters'][0]['value']}\n"
            f"{requested['target']['surface']}: opaque_identifier_1"
        )
        unit["text"] = {
            "search_text": row_text,
            "sha256": hashlib.sha256(row_text.encode("utf-8")).hexdigest(),
            "char_count": len(row_text),
        }
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = Path(raw_directory)
            documents_path = directory / "documents.jsonl"
            search_path = directory / "search_units.jsonl"
            entries_path = directory / "entries.jsonl"
            snapshot_path = directory / "snapshot.json"
            qur_path = directory / "qur.jsonl"
            clause_path = directory / "clause-ir.jsonl"
            resolution_path = directory / "resolution.jsonl"
            _write_jsonl(documents_path, [document])
            _write_jsonl(search_path, [unit])
            _write_jsonl(qur_path, [qur])
            _write_jsonl(clause_path, [clause_ir])
            build_data_catalog(
                documents_path, search_path, entries_path, snapshot_path
            )
            resolution = resolve_catalog_files(
                qur_path, clause_path, entries_path, snapshot_path,
                generated_at=STAMP,
            )
            # No source, organization, field, or value is registered in code.
            # The exact SearchUnit row profile proves the generic structured
            # capability and the resolver accepts the newly introduced data.
            self.assertEqual("resolved", resolution["final_status"])
            self.assertEqual([], resolution["reason_codes"])
            _write_jsonl(resolution_path, [resolution])
            self.assertEqual(
                [],
                validate_catalog_resolution(
                    resolution_path,
                    qur_path=qur_path,
                    clause_ir_path=clause_path,
                    entries_path=entries_path,
                    snapshot_path=snapshot_path,
                ),
            )


if __name__ == "__main__":
    unittest.main()
