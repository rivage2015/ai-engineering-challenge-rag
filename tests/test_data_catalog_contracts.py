"""Schema-only contracts for DataCatalogEntry and DataCatalogSnapshot.

These tests enforce the published structural, vocabulary, and question/data
separation boundary.  They intentionally do not claim semantic recomputation
of content-derived IDs or hashes, canonical stream ordering or counts, source
reference existence, or agreement between a stream and its snapshot.  Those
checks require a future deterministic semantic validator.
"""

from __future__ import annotations

import json
import re
import unittest
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import jsonschema


REPOSITORY = Path(__file__).resolve().parents[1]
ENTRY_SCHEMA_PATH = REPOSITORY / "schemas" / "data-catalog-entry.schema.json"
SNAPSHOT_SCHEMA_PATH = (
    REPOSITORY / "schemas" / "data-catalog-snapshot.schema.json"
)

RFC3339_DATETIME = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}[Tt][0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]+)?(?:[Zz]|[+-][0-9]{2}:[0-9]{2})$"
)


def is_rfc3339_datetime(value: object) -> bool:
    """Supply the optional date-time checker absent from the lean test venv."""

    if not isinstance(value, str) or RFC3339_DATETIME.fullmatch(value) is None:
        return False
    datetime.fromisoformat(value.replace("Z", "+00:00").replace("z", "+00:00"))
    return True


def format_checker() -> jsonschema.FormatChecker:
    checker = jsonschema.FormatChecker()
    if "date-time" not in checker.checkers:
        checker.checks("date-time", raises=ValueError)(is_rfc3339_datetime)
    return checker


def searchable_entry() -> dict[str, Any]:
    document_id = "doc_bbbbbbbbbbbbbbbb"
    return {
        "schema_version": "0.1",
        "record_type": "data_catalog_entry",
        "data_catalog_entry_id": "dce_aaaaaaaaaaaaaaaa",
        "document_id": document_id,
        "source_identity": {
            "relative_path": "incoming/opaque-source.csv",
            "file_name": "opaque-source.csv",
            "extension": "csv",
            "media_type": "text/csv",
            "sha256": "c" * 64,
        },
        "address": {
            "container_kind": "table",
            "container_name": "OpaqueSheet",
            "container_index": 1,
            "source_member": None,
            "parent_entry_ref": None,
        },
        "scope_labels": [
            {
                "label_id": "dcl_dddddddddddddddd",
                "role": "location",
                "surface": "incoming",
                "normalized": "incoming",
                "source_kind": "path_component",
                "source_refs": [document_id],
            }
        ],
        "fields": [
            {
                "field_id": "dcf_eeeeeeeeeeeeeeee",
                "surface": "OpaqueField",
                "normalized": "opaquefield",
                "ordinal": 0,
                "data_type": "string",
                "unit": None,
                "source_refs": [document_id],
            }
        ],
        "capabilities": {
            "retrieval_channels": ["structured"],
            "predicate_operators": ["eq"],
            "graph_operators": ["filter", "project", "list"],
        },
        "availability": {
            "extraction_status": "success",
            "searchable": True,
            "reason_codes": [],
        },
        "provenance": {
            "builder": "data-catalog-builder",
            "builder_version": "0.1",
            "generated_at": "2026-08-16T00:00:00Z",
            "deterministic": True,
            "question_independent": True,
            "source_data_used": True,
            "question_data_used": False,
            "answer_data_used": False,
            "past_answers_used": False,
            "raw_values_embedded": False,
            "input_refs": [document_id],
        },
    }


def nonsearchable_failed_entry() -> dict[str, Any]:
    entry = searchable_entry()
    entry["data_catalog_entry_id"] = "dce_ffffffffffffffff"
    entry["capabilities"] = {
        "retrieval_channels": [],
        "predicate_operators": [],
        "graph_operators": [],
    }
    entry["availability"] = {
        "extraction_status": "failed",
        "searchable": False,
        "reason_codes": ["extraction_failed"],
    }
    return entry


def catalog_snapshot() -> dict[str, Any]:
    return {
        "schema_version": "0.1",
        "record_type": "data_catalog_snapshot",
        "data_catalog_snapshot_id": "dcs_1111111111111111",
        "entry_schema_version": "0.1",
        "entry_stream": {
            "format": "jsonl",
            "relative_path": "artifacts/data-catalog.jsonl",
            "sha256": "2" * 64,
            "record_count": 2,
            "sort_key": "data_catalog_entry_id",
            "canonicalization": "utf8_nfc_canonical_json_per_line_lf",
        },
        "inputs": [
            {
                "record_type": "document",
                "schema_version": "0.1",
                "sha256": "3" * 64,
                "record_count": 4,
            }
        ],
        "build_config_sha256": "4" * 64,
        "provenance": {
            "builder": "data-catalog-builder",
            "builder_version": "0.1",
            "generated_at": "2026-08-16T00:00:00+00:00",
            "deterministic": True,
            "question_independent": True,
            "source_data_used": True,
            "question_data_used": False,
            "answer_data_used": False,
            "past_answers_used": False,
            "raw_values_embedded": False,
        },
    }


class DataCatalogSchemaTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.entry_schema = json.loads(ENTRY_SCHEMA_PATH.read_text(encoding="utf-8"))
        cls.snapshot_schema = json.loads(
            SNAPSHOT_SCHEMA_PATH.read_text(encoding="utf-8")
        )
        jsonschema.Draft202012Validator.check_schema(cls.entry_schema)
        jsonschema.Draft202012Validator.check_schema(cls.snapshot_schema)
        checker = format_checker()
        cls.entry_validator = jsonschema.Draft202012Validator(
            cls.entry_schema, format_checker=checker
        )
        cls.snapshot_validator = jsonschema.Draft202012Validator(
            cls.snapshot_schema, format_checker=checker
        )

    def entry_errors(self, value: object) -> list[str]:
        return sorted(
            error.message for error in self.entry_validator.iter_errors(value)
        )

    def snapshot_errors(self, value: object) -> list[str]:
        return sorted(
            error.message for error in self.snapshot_validator.iter_errors(value)
        )

    def assert_entry_invalid(self, value: object) -> None:
        self.assertTrue(
            self.entry_errors(value), "schema-invalid catalog entry was accepted"
        )

    def assert_snapshot_invalid(self, value: object) -> None:
        self.assertTrue(
            self.snapshot_errors(value),
            "schema-invalid catalog snapshot was accepted",
        )

    def test_searchable_nonsearchable_and_snapshot_examples_are_valid(self) -> None:
        self.assertEqual(
            self.entry_schema["$id"],
            "https://local.ai-engineering-challenge/schemas/data-catalog-entry.schema.json",
        )
        self.assertEqual(
            self.snapshot_schema["$id"],
            "https://local.ai-engineering-challenge/schemas/data-catalog-snapshot.schema.json",
        )
        self.assertEqual(self.entry_errors(searchable_entry()), [])
        self.assertEqual(self.entry_errors(nonsearchable_failed_entry()), [])
        self.assertEqual(self.snapshot_errors(catalog_snapshot()), [])

    def test_every_metadata_container_is_closed(self) -> None:
        entry_containers: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
            "root": lambda value: value,
            "source_identity": lambda value: value["source_identity"],
            "address": lambda value: value["address"],
            "scope_label": lambda value: value["scope_labels"][0],
            "field": lambda value: value["fields"][0],
            "capabilities": lambda value: value["capabilities"],
            "availability": lambda value: value["availability"],
            "provenance": lambda value: value["provenance"],
        }
        for label, select in entry_containers.items():
            with self.subTest(record="entry", container=label):
                entry = searchable_entry()
                select(entry)["unknown_metadata"] = True
                self.assert_entry_invalid(entry)

        snapshot_containers: dict[
            str, Callable[[dict[str, Any]], dict[str, Any]]
        ] = {
            "root": lambda value: value,
            "entry_stream": lambda value: value["entry_stream"],
            "input": lambda value: value["inputs"][0],
            "provenance": lambda value: value["provenance"],
        }
        for label, select in snapshot_containers.items():
            with self.subTest(record="snapshot", container=label):
                snapshot = catalog_snapshot()
                select(snapshot)["unknown_metadata"] = True
                self.assert_snapshot_invalid(snapshot)

    def test_entry_ids_hashes_source_refs_enums_and_timestamp_are_closed(self) -> None:
        mutations: dict[str, Callable[[dict[str, Any]], None]] = {
            "entry_id": lambda value: value.__setitem__(
                "data_catalog_entry_id", "dce_short"
            ),
            "document_id": lambda value: value.__setitem__(
                "document_id", "document_aaaaaaaaaaaaaaaa"
            ),
            "label_id": lambda value: value["scope_labels"][0].__setitem__(
                "label_id", "dcl_not-hex"
            ),
            "field_id": lambda value: value["fields"][0].__setitem__(
                "field_id", "dcf_not-hex"
            ),
            "source_sha256": lambda value: value["source_identity"].__setitem__(
                "sha256", "C" * 64
            ),
            "scope_source_ref": lambda value: value["scope_labels"][0].__setitem__(
                "source_refs", ["qur_aaaaaaaaaaaaaaaa"]
            ),
            "field_source_ref": lambda value: value["fields"][0].__setitem__(
                "source_refs", []
            ),
            "parent_entry_ref": lambda value: value["address"].__setitem__(
                "parent_entry_ref", "dce_parent"
            ),
            "container_kind": lambda value: value["address"].__setitem__(
                "container_kind", "database"
            ),
            "scope_role": lambda value: value["scope_labels"][0].__setitem__(
                "role", "question"
            ),
            "scope_source_kind": lambda value: value["scope_labels"][0].__setitem__(
                "source_kind", "inferred_alias"
            ),
            "field_type": lambda value: value["fields"][0].__setitem__(
                "data_type", "object"
            ),
            "retrieval_channel": lambda value: value["capabilities"].__setitem__(
                "retrieval_channels", ["answer_index"]
            ),
            "predicate_operator": lambda value: value["capabilities"].__setitem__(
                "predicate_operators", ["approximately"]
            ),
            "graph_operator": lambda value: value["capabilities"].__setitem__(
                "graph_operators", ["answer"]
            ),
            "extraction_status": lambda value: value["availability"].__setitem__(
                "extraction_status", "ready"
            ),
            "reason_code": lambda value: value["availability"].__setitem__(
                "reason_codes", ["question_relevant"]
            ),
            "generated_at": lambda value: value["provenance"].__setitem__(
                "generated_at", "not-a-timestamp"
            ),
        }
        for label, mutate in mutations.items():
            with self.subTest(invalid_entry_field=label):
                entry = searchable_entry()
                mutate(entry)
                self.assert_entry_invalid(entry)

    def test_provenance_is_question_independent_and_value_free(self) -> None:
        entry_constants = {
            "builder": "question-aware-builder",
            "builder_version": "v0.1",
            "deterministic": False,
            "question_independent": False,
            "source_data_used": False,
            "question_data_used": True,
            "answer_data_used": True,
            "past_answers_used": True,
            "raw_values_embedded": True,
        }
        for field, replacement in entry_constants.items():
            with self.subTest(record="entry", provenance_field=field):
                entry = searchable_entry()
                entry["provenance"][field] = replacement
                self.assert_entry_invalid(entry)

            with self.subTest(record="snapshot", provenance_field=field):
                snapshot = catalog_snapshot()
                snapshot["provenance"][field] = replacement
                self.assert_snapshot_invalid(snapshot)

    def test_question_answer_value_and_relevance_metadata_cannot_be_smuggled(self) -> None:
        forbidden_keys = (
            "question",
            "answer",
            "raw_value",
            "statistics",
            "embedding",
            "alias",
            "relevance",
        )
        entry_containers: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
            "root": lambda value: value,
            "source_identity": lambda value: value["source_identity"],
            "address": lambda value: value["address"],
            "scope_label": lambda value: value["scope_labels"][0],
            "field": lambda value: value["fields"][0],
            "capabilities": lambda value: value["capabilities"],
            "availability": lambda value: value["availability"],
            "provenance": lambda value: value["provenance"],
        }
        for key in forbidden_keys:
            for label, select in entry_containers.items():
                with self.subTest(forbidden_key=key, container=label):
                    entry = searchable_entry()
                    select(entry)[key] = "smuggled"
                    self.assert_entry_invalid(entry)

        snapshot_containers: dict[
            str, Callable[[dict[str, Any]], dict[str, Any]]
        ] = {
            "root": lambda value: value,
            "entry_stream": lambda value: value["entry_stream"],
            "input": lambda value: value["inputs"][0],
            "provenance": lambda value: value["provenance"],
        }
        for key in forbidden_keys:
            for label, select in snapshot_containers.items():
                with self.subTest(
                    forbidden_key=key, container=f"snapshot.{label}"
                ):
                    snapshot = catalog_snapshot()
                    select(snapshot)[key] = "smuggled"
                    self.assert_snapshot_invalid(snapshot)

        source_names_are_not_metadata = searchable_entry()
        source_names_are_not_metadata["fields"][0].update(
            {"surface": "answer", "normalized": "answer"}
        )
        self.assertEqual(self.entry_errors(source_names_are_not_metadata), [])

    def test_searchability_and_capability_conditions_are_bidirectional(self) -> None:
        partial = searchable_entry()
        partial["availability"].update(
            {
                "extraction_status": "partial",
                "reason_codes": ["extraction_partial"],
            }
        )
        partial["capabilities"]["retrieval_channels"] = ["lexical"]
        self.assertEqual(self.entry_errors(partial), [])

        for status in ("pending", "deferred", "failed"):
            with self.subTest(searchable_status=status):
                entry = searchable_entry()
                entry["availability"]["extraction_status"] = status
                self.assert_entry_invalid(entry)

        no_channel = searchable_entry()
        no_channel["capabilities"]["retrieval_channels"] = []
        self.assert_entry_invalid(no_channel)

        for capability in (
            "retrieval_channels",
            "predicate_operators",
            "graph_operators",
        ):
            with self.subTest(nonsearchable_capability=capability):
                entry = nonsearchable_failed_entry()
                entry["capabilities"][capability] = [
                    {
                        "retrieval_channels": "lexical",
                        "predicate_operators": "eq",
                        "graph_operators": "filter",
                    }[capability]
                ]
                self.assert_entry_invalid(entry)

        no_reason = nonsearchable_failed_entry()
        no_reason["availability"]["reason_codes"] = []
        self.assert_entry_invalid(no_reason)

    def test_snapshot_stream_and_source_input_contracts_are_closed(self) -> None:
        mutations: dict[str, Callable[[dict[str, Any]], None]] = {
            "snapshot_id": lambda value: value.__setitem__(
                "data_catalog_snapshot_id", "dcs_short"
            ),
            "entry_schema_version": lambda value: value.__setitem__(
                "entry_schema_version", "0.2"
            ),
            "stream_format": lambda value: value["entry_stream"].__setitem__(
                "format", "json"
            ),
            "stream_extension": lambda value: value["entry_stream"].__setitem__(
                "relative_path", "artifacts/data-catalog.json"
            ),
            "stream_sha256": lambda value: value["entry_stream"].__setitem__(
                "sha256", "2" * 63
            ),
            "stream_count": lambda value: value["entry_stream"].__setitem__(
                "record_count", -1
            ),
            "stream_sort_key": lambda value: value["entry_stream"].__setitem__(
                "sort_key", "document_id"
            ),
            "stream_canonicalization": lambda value: value["entry_stream"].__setitem__(
                "canonicalization", "pretty_json"
            ),
            "empty_inputs": lambda value: value.__setitem__("inputs", []),
            "input_record_type": lambda value: value["inputs"][0].__setitem__(
                "record_type", "question"
            ),
            "input_schema_version": lambda value: value["inputs"][0].__setitem__(
                "schema_version", "latest"
            ),
            "input_sha256": lambda value: value["inputs"][0].__setitem__(
                "sha256", "G" * 64
            ),
            "input_count": lambda value: value["inputs"][0].__setitem__(
                "record_count", -1
            ),
            "build_config_sha256": lambda value: value.__setitem__(
                "build_config_sha256", "4" * 65
            ),
            "generated_at": lambda value: value["provenance"].__setitem__(
                "generated_at", "yesterday"
            ),
        }
        for label, mutate in mutations.items():
            with self.subTest(invalid_snapshot_field=label):
                snapshot = catalog_snapshot()
                mutate(snapshot)
                self.assert_snapshot_invalid(snapshot)

        duplicate_inputs = catalog_snapshot()
        duplicate_inputs["inputs"].append(dict(duplicate_inputs["inputs"][0]))
        self.assert_snapshot_invalid(duplicate_inputs)


if __name__ == "__main__":
    unittest.main()
