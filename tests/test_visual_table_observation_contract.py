from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import build_visual_table_observation as module  # noqa: E402


def raw_cell(
    row_start: int,
    row_end: int,
    column_start: int,
    column_end: int,
    text: str,
    *,
    bbox: tuple[float, float, float, float],
    column_header: bool = False,
) -> dict[str, Any]:
    left, top, right, bottom = bbox
    return {
        "row_start": row_start,
        "row_end": row_end,
        "column_start": column_start,
        "column_end": column_end,
        "row_span": row_end - row_start,
        "column_span": column_end - column_start,
        "text": text,
        "column_header": column_header,
        "row_header": False,
        "row_section": False,
        "bbox": {
            "l": left,
            "t": top,
            "r": right,
            "b": bottom,
            "coord_origin": "TOPLEFT",
        },
    }


def clean_table() -> dict[str, Any]:
    return {
        "table_index": 0,
        "self_ref": "#/tables/0",
        "label": "table",
        "rows": 2,
        "columns": 2,
        "provenance": [
            {
                "bbox": {
                    "l": 10.0,
                    "t": 90.0,
                    "r": 190.0,
                    "b": 10.0,
                    "coord_origin": "BOTTOMLEFT",
                },
                "charspan": [0, 0],
                "page_number": 1,
            }
        ],
        "cells": [
            raw_cell(0, 1, 0, 1, "role", bbox=(10, 10, 90, 40), column_header=True),
            raw_cell(0, 1, 1, 2, "salary", bbox=(110, 10, 190, 40), column_header=True),
            raw_cell(1, 2, 0, 1, "engineer", bbox=(10, 60, 90, 90)),
            raw_cell(1, 2, 1, 2, "100,000", bbox=(110, 60, 190, 90)),
        ],
    }


def synthetic_asset() -> dict[str, Any]:
    return {
        "asset_id": "asset_" + "a" * 32,
        "document_id": "doc_" + "b" * 32,
        "file_id": "file_" + "c" * 32,
        "source": {
            "relative_path": "project/source.docx",
            "sha256": "1" * 64,
        },
        "origin": {
            "kind": "office_embedded_image",
            "member_path": "word/media/image1.png",
            "member_sha256": "2" * 64,
            "member_size_bytes": 7,
            "page_number": None,
        },
        "materialization": {
            "sha256": "3" * 64,
            "width_px": 200,
            "height_px": 100,
        },
    }


def synthetic_run(table: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "schema_version": "0.2",
        "run_id": "docpoc_" + "4" * 24,
        "input": {
            "sample_id": "docsrc_" + "5" * 24,
            "materialized_path": "artifacts/image.png",
            "dimensions": {"width_px": 200, "height_px": 100},
        },
        "configuration": {
            "pipeline": {"ocr": {"engine": "ocrmac"}},
            "ocr_engine_fingerprint": {"fingerprint_sha256": "6" * 64},
            "package_fingerprint_sha256": "7" * 64,
            "models": {
                "layout": {"sha256": "8" * 64},
                "tableformer": {"sha256": "9" * 64},
            },
        },
        "document": {
            "pages": [{"page_number": 1, "width": 200, "height": 100}],
            "tables": [copy.deepcopy(table or clean_table())],
        },
        "hashes": {
            "output_sha256": "a" * 64,
            "record_integrity_sha256": "b" * 64,
        },
    }


def resign(record: dict[str, Any]) -> None:
    record["hashes"]["table_sha256"] = module.sha256_json(record["table"])
    signature = module.sha256_json(module._signature_payload(record))
    record["hashes"]["signature_sha256"] = signature
    record["observation_id"] = "vtobs_" + signature[:24]
    record["hashes"]["record_integrity_sha256"] = module.sha256_json(
        module.record_integrity_payload(record)
    )


class VisualTableObservationContractTest(unittest.TestCase):
    def test_schema_and_clean_observation_round_trip(self) -> None:
        Draft202012Validator.check_schema(
            json.loads(module.SCHEMA_PATH.read_text(encoding="utf-8"))
        )
        record = module.build_observation(
            asset=synthetic_asset(), run=synthetic_run()
        )

        self.assertEqual(module.validate_observation(record), [])
        self.assertEqual(record["status"], "hold")
        self.assertEqual(record["reasons"], ["cell_text_not_fully_verified"])
        self.assertEqual(record["table"]["structure_status"], "pass")
        self.assertEqual(record["table"]["coverage"]["coverage_ratio"], 1.0)

    def test_merged_cell_uses_slot_coverage_not_cell_count(self) -> None:
        table = clean_table()
        table["cells"] = [
            raw_cell(0, 1, 0, 2, "header", bbox=(10, 10, 190, 40), column_header=True),
            raw_cell(1, 2, 0, 1, "left", bbox=(10, 60, 90, 90)),
            raw_cell(1, 2, 1, 2, "right", bbox=(110, 60, 190, 90)),
        ]
        record = module.build_observation(
            asset=synthetic_asset(), run=synthetic_run(table)
        )

        self.assertEqual(module.validate_observation(record), [])
        self.assertEqual(record["table"]["coverage"]["cell_count"], 3)
        self.assertEqual(record["table"]["coverage"]["expected_slots"], 4)
        self.assertEqual(record["table"]["structure_status"], "pass")

    def test_missing_and_overlapping_slots_remain_observable_holds(self) -> None:
        missing = clean_table()
        missing["cells"].pop()
        missing_record = module.build_observation(
            asset=synthetic_asset(), run=synthetic_run(missing)
        )
        self.assertEqual(module.validate_observation(missing_record), [])
        self.assertEqual(missing_record["table"]["structure_status"], "hold")
        self.assertIn("missing_grid_slots", missing_record["reasons"])

        overlapping = clean_table()
        overlapping["cells"].append(
            raw_cell(0, 1, 0, 2, "overlay", bbox=(10, 10, 190, 40))
        )
        overlap_record = module.build_observation(
            asset=synthetic_asset(), run=synthetic_run(overlapping)
        )
        self.assertEqual(module.validate_observation(overlap_record), [])
        self.assertEqual(overlap_record["table"]["structure_status"], "hold")
        self.assertIn("overlapping_grid_slots", overlap_record["reasons"])

    def test_duplicate_span_bbox_and_resource_errors_fail_closed(self) -> None:
        cases: list[tuple[str, dict[str, Any]]] = []
        duplicate = clean_table()
        duplicate["cells"].append(copy.deepcopy(duplicate["cells"][0]))
        cases.append(("duplicates", duplicate))
        bad_span = clean_table()
        bad_span["cells"][0]["column_span"] = 2
        cases.append(("span", bad_span))
        bad_bbox = clean_table()
        bad_bbox["cells"][0]["bbox"]["r"] = 5
        cases.append(("bbox", bad_bbox))
        noninteger = clean_table()
        noninteger["cells"][0]["row_start"] = 0.5
        cases.append(("integer", noninteger))
        too_large = clean_table()
        too_large["rows"] = 1001
        too_large["columns"] = 1000
        cases.append(("resource", too_large))

        for label, table in cases:
            with self.subTest(label=label):
                with self.assertRaises(ValueError):
                    module.build_observation(
                        asset=synthetic_asset(), run=synthetic_run(table)
                    )

    def test_semantic_recomputation_detects_fully_rehashed_mutations(self) -> None:
        original = module.build_observation(
            asset=synthetic_asset(), run=synthetic_run()
        )
        mutations = []
        bad_coverage = copy.deepcopy(original)
        bad_coverage["table"]["coverage"]["observed_slots"] = 1
        mutations.append(bad_coverage)
        bad_text = copy.deepcopy(original)
        bad_text["table"]["cells"][0]["raw_text"] = "altered"
        mutations.append(bad_text)
        bad_bbox = copy.deepcopy(original)
        bad_bbox["table"]["bbox"]["r"] -= 1
        mutations.append(bad_bbox)
        bad_bundle = copy.deepcopy(original)
        bad_bundle["hashes"]["input_bundle_sha256"] = "f" * 64
        mutations.append(bad_bundle)

        for changed in mutations:
            resign(changed)
            with self.subTest(changed=changed["hashes"]["table_sha256"]):
                self.assertTrue(module.validate_observation(changed))

    def test_contract_is_closed_and_sensitive_paths_are_rejected(self) -> None:
        record = module.build_observation(
            asset=synthetic_asset(), run=synthetic_run()
        )
        for path in (
            (),
            ("source",),
            ("upstream",),
            ("table",),
            ("table", "cells", 0),
            ("hashes",),
            ("provenance",),
        ):
            changed = copy.deepcopy(record)
            target: Any = changed
            for key in path:
                target = target[key]
            target["unexpected"] = True
            resign(changed)
            with self.subTest(path=path):
                self.assertTrue(module.validate_observation(changed))

        for sensitive in (
            "share/質問回答/source.docx",
            "fixtures/questions_test.docx",
            "out/predictions.docx",
        ):
            changed = copy.deepcopy(record)
            changed["source"]["source_relative_path"] = sensitive
            resign(changed)
            with self.subTest(sensitive=sensitive):
                self.assertTrue(module.validate_observation(changed))

    def test_jsonl_rejects_duplicate_keys_nonfinite_and_blank_lines(self) -> None:
        payloads = (
            '{"a":1,"a":2}\n',
            '{"value":NaN}\n',
            '{"ok":true}\n\n',
            '[]\n',
        )
        with tempfile.TemporaryDirectory(dir="/private/tmp") as directory:
            for index, payload in enumerate(payloads):
                path = Path(directory) / f"input-{index}.jsonl"
                path.write_text(payload, encoding="utf-8")
                with self.subTest(payload=payload):
                    with self.assertRaises(ValueError):
                        module.load_jsonl(path, label="synthetic input")

    def test_source_docx_member_is_bound_by_bytes_and_symlink_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory(dir="/private/tmp") as directory:
            root = Path(directory) / "source"
            root.mkdir()
            member = b"payload"
            source_path = root / "source.docx"
            with zipfile.ZipFile(source_path, "w") as archive:
                archive.writestr("word/media/image1.png", member)
            asset = synthetic_asset()
            asset["source"]["relative_path"] = "source.docx"
            asset["source"]["sha256"] = module.sha256_file(source_path)
            asset["origin"]["member_sha256"] = module.sha256_bytes(member)
            asset["origin"]["member_size_bytes"] = len(member)

            module.verify_source_and_member(asset, root)
            changed = copy.deepcopy(asset)
            changed["origin"]["member_sha256"] = "f" * 64
            with self.assertRaises(ValueError):
                module.verify_source_and_member(changed, root)

            link = Path(directory) / "source-link"
            link.symlink_to(root, target_is_directory=True)
            with self.assertRaises(ValueError):
                module.verify_source_and_member(asset, link)

    def test_output_is_deterministic_and_write_is_no_overwrite(self) -> None:
        first = module.build_observation(
            asset=synthetic_asset(), run=synthetic_run()
        )
        second = module.build_observation(
            asset=synthetic_asset(), run=synthetic_run()
        )
        self.assertEqual(module.canonical_json(first), module.canonical_json(second))
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "observation.json"
            module.write_json(output, first, overwrite=False)
            with self.assertRaises(FileExistsError):
                module.write_json(output, second, overwrite=False)
            self.assertEqual(
                output.read_bytes(),
                (module.canonical_json(first) + "\n").encode("utf-8"),
            )


if __name__ == "__main__":
    unittest.main()
