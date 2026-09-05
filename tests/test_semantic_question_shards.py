from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import adapt_layer1_to_local_memory as adapter  # noqa: E402

VALIDATOR_PATH = (
    ROOT / "distribution" / "macos-local-memory" / "engine"
    / "validate_adaptive_semantic_graph.py"
)
VALIDATOR_SPEC = importlib.util.spec_from_file_location(
    "semantic_question_shard_validator", VALIDATOR_PATH
)
if VALIDATOR_SPEC is None or VALIDATOR_SPEC.loader is None:
    raise ImportError(f"cannot load {VALIDATOR_PATH}")
semantic_validator = importlib.util.module_from_spec(VALIDATOR_SPEC)
sys.modules[VALIDATOR_SPEC.name] = semantic_validator
VALIDATOR_SPEC.loader.exec_module(semantic_validator)


DOCUMENT_ID = "doc_" + "d" * 32
PARAGRAPH_ID = "ev_" + "1" * 32
TABLE_CELL_ID = "ev_" + "2" * 32
IMAGE_ID = "ev_" + "3" * 32
OCR_ID = "ev_" + "4" * 32
VLM_ID = "ev_" + "7" * 32
MARKER = adapter.PROVISIONAL_OCR_MARKER


def text_sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def projection(
    evidence_id: str,
    observed_text: str,
    *,
    source_record_type: str,
    unit_type: str | None = None,
    provisional: bool = False,
) -> dict[str, object]:
    adapter_metadata: dict[str, object] = {
        "name": adapter.ADAPTER,
        "version": adapter.ADAPTER_VERSION,
        "source_record_type": source_record_type,
        "text_projection": "search_unit_text" if unit_type else "raw_text",
        "execution_policy": "never_execute",
    }
    if unit_type is not None:
        adapter_metadata.update({
            "source_search_unit_id": "su_" + unit_type,
            "source_evidence_ids": [TABLE_CELL_ID],
            "unit_type": unit_type,
        })
    value: dict[str, object] = {
        "schema_version": "0.1",
        "evidence_id": evidence_id,
        "document_id": DOCUMENT_ID,
        "ordinal": 7,
        "locator": {"section": "fixture"},
        "observed_text": observed_text,
        "source": {"relative_path": "sample.txt", "sha256": "a" * 64},
        "extraction_method": "fixture",
        "status": "observed",
        "adapter": adapter_metadata,
    }
    if provisional:
        value.update({
            "quality_tier": "provisional",
            "agreement_types": ["same_engine_agreement"],
            "bbox_coordinate_system": "display_oriented_top_left_normalized_1000",
            "reading_order_method": "geometry_row_bands_v1",
            "row_band_count": 1,
            "provisional_marker": MARKER,
        })
    return value


def reconstructed_payload(shards: list[dict[str, object]]) -> str:
    values: list[str] = []
    for shard in shards:
        metadata = shard["adapter"]["question_shard"]  # type: ignore[index]
        prefix = metadata["observed_text_prefix"]
        observed_text = shard["observed_text"]
        assert isinstance(prefix, str) and isinstance(observed_text, str)
        values.append(observed_text[len(prefix):])
    return "".join(values)


def jsonl(path: Path, records: list[dict[str, object]]) -> None:
    path.write_text(
        "".join(adapter.canonical(record) + "\n" for record in records),
        encoding="utf-8",
    )


class SemanticQuestionShardTests(unittest.TestCase):
    def source_projections(self) -> list[tuple[str, dict[str, object]]]:
        paragraph_text = "".join(f"段落{i:04d}:記録を忠実に保持する。\n" for i in range(260))
        table_text = "".join(f"列A: 値{i:04d} | 列B: 関連{i:04d}\n" for i in range(230))
        image_text = "Image file: sample.png\n" + MARKER + " " + "画像の忠実な暫定読取" * 520
        return [
            (
                "paragraph",
                projection(
                    PARAGRAPH_ID,
                    paragraph_text,
                    source_record_type="paragraph",
                ),
            ),
            (
                "table_row",
                projection(
                    "ev_" + "5" * 32,
                    table_text,
                    source_record_type="search_unit",
                    unit_type="table_row",
                ),
            ),
            (
                "image_text_packet",
                projection(
                    "ev_" + "6" * 32,
                    image_text,
                    source_record_type="search_unit",
                    unit_type="image_text_packet",
                    provisional=True,
                ),
            ),
        ]

    def test_paragraph_table_and_image_round_trip_exactly(self) -> None:
        for label, source in self.source_projections():
            with self.subTest(label):
                shards = adapter.question_shards(source)
                self.assertGreater(len(shards), 1)
                self.assertEqual(
                    adapter.validate_question_shard_reconstruction(source, shards),
                    source["observed_text"],
                )
                self.assertEqual(reconstructed_payload(shards), source["observed_text"])
                self.assertTrue(
                    all(
                        len(str(item["observed_text"]))
                        <= adapter.MAX_QUESTION_EVIDENCE_CHARS
                        for item in shards
                    )
                )
                self.assertEqual(shards, adapter.question_shards(source))
                for index, item in enumerate(shards, 1):
                    metadata = item["adapter"]["question_shard"]  # type: ignore[index]
                    self.assertEqual(metadata["chunk_index"], index)
                    self.assertEqual(metadata["chunk_count"], len(shards))
                    self.assertEqual(
                        metadata["source_projection_id"], source["evidence_id"]
                    )
                    self.assertEqual(
                        metadata["source_projection_sha256"],
                        adapter.sha256_canonical(source),
                    )
                    self.assertEqual(
                        metadata["source_text_sha256"],
                        text_sha256(str(source["observed_text"])),
                    )
                if label == "image_text_packet":
                    for item in shards:
                        self.assertTrue(str(item["observed_text"]).startswith(MARKER))
                        self.assertEqual(item["quality_tier"], "provisional")
                        self.assertEqual(item["provisional_marker"], MARKER)
                        self.assertEqual(
                            item["agreement_types"], ["same_engine_agreement"]
                        )

    def test_tampering_fails_closed_for_every_projection_kind(self) -> None:
        for label, source in self.source_projections():
            with self.subTest(label):
                shards = adapter.question_shards(source)
                altered = copy.deepcopy(shards)
                if label == "paragraph":
                    altered[0]["observed_text"] = "X" + str(altered[0]["observed_text"])[1:]
                elif label == "table_row":
                    altered[0]["adapter"]["source_evidence_ids"].append("ev_tampered")  # type: ignore[index]
                else:
                    altered[0]["observed_text"] = str(altered[0]["observed_text"])[len(MARKER):]
                with self.assertRaises(ValueError):
                    adapter.validate_question_shard_reconstruction(source, altered)

    def test_independent_validator_reconstructs_and_rejects_shard_tampering(self) -> None:
        for label, source in self.source_projections():
            with self.subTest(label):
                actual = adapter.question_shards(source)
                expected = semantic_validator.expected_question_shards(source)
                semantic_validator.validate_exact_projection(actual, expected)
                self.assertEqual(actual, expected)

                altered = copy.deepcopy(actual)
                altered[-1]["adapter"]["question_shard"]["character_end"] -= 1
                with self.assertRaisesRegex(
                    ValueError, "semantic_evidence_projection_mismatch"
                ):
                    semantic_validator.validate_exact_projection(altered, expected)

    def test_exact_boundary_is_not_sharded(self) -> None:
        source = projection(
            PARAGRAPH_ID,
            "x" * adapter.MAX_QUESTION_EVIDENCE_CHARS,
            source_record_type="paragraph",
        )
        self.assertEqual(adapter.question_shards(source), [source])
        self.assertNotIn("question_shard", source["adapter"])

    def test_adapter_replaces_all_oversized_raw_and_relationship_projections(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_root = root / "source"
            intermediate = root / "intermediate"
            search = root / "search"
            output = root / "semantic"
            source_root.mkdir()
            intermediate.mkdir()
            search.mkdir()
            source_root = source_root.resolve()
            source_path = source_root / "sample.txt"
            source_path.write_text("source binding", encoding="utf-8")
            source_hash = adapter.sha256_file(source_path)

            paragraph_text = "".join(
                f"長い段落{i:04d}の後半も質問に届ける。\n" for i in range(220)
            )
            table_text = "".join(
                f"項目{i:04d}: 値{i:04d} | 関連: 保持{i:04d}\n" for i in range(210)
            )
            ocr_payload = "画像内の文字を暫定として後半まで保持" * 300
            image_text = f"Image file: sample.png\n{MARKER} {ocr_payload}"
            vlm_payload = "座標を持たない画像全体の忠実な暫定読取" * 220
            vlm_text = f"{MARKER} {vlm_payload}"

            document = {
                "schema_version": "0.1",
                "record_type": "document",
                "document_id": DOCUMENT_ID,
                "source": {
                    "relative_path": "sample.txt",
                    "sha256": source_hash,
                    "size_bytes": source_path.stat().st_size,
                    "extension": ".txt",
                },
                "extraction": {
                    "status": "complete",
                    "parser": "fixture",
                    "parser_version": "1",
                },
            }
            image_origin = {
                "kind": "standalone_image",
                "source_relative_path": "sample.txt",
                "source_sha256": source_hash,
                "source_location": {"object_index": 1},
                "materialization": {
                    "runner": "fixture",
                    "external_network_used": False,
                    "source_sha256": source_hash,
                    "rendered_sha256": source_hash,
                },
            }
            paragraph = {
                "evidence_id": PARAGRAPH_ID,
                "document_id": DOCUMENT_ID,
                "evidence_type": "paragraph",
                "ordinal": 1,
                "location": {"paragraph_index": 1},
                "content": {"raw_text": paragraph_text},
                "provenance": {"extraction_method": "fixture_paragraph"},
            }
            table_cell = {
                "evidence_id": TABLE_CELL_ID,
                "document_id": DOCUMENT_ID,
                "evidence_type": "table_cell",
                "ordinal": 2,
                "location": {"cell": "A1"},
                "content": {"raw_text": "表の元セル"},
                "provenance": {"extraction_method": "fixture_cell"},
            }
            image = {
                "evidence_id": IMAGE_ID,
                "document_id": DOCUMENT_ID,
                "evidence_type": "image",
                "ordinal": 3,
                "location": {"object_index": 1},
                "content": {"content_ref": "sample.png", "sha256": source_hash},
                "provenance": {"extraction_method": "fixture_image"},
                "native_properties": {
                    "source_sha256": source_hash,
                    "visual_origin": copy.deepcopy(image_origin),
                },
            }
            ocr = {
                "evidence_id": OCR_ID,
                "document_id": DOCUMENT_ID,
                "evidence_type": "ocr_line",
                "ordinal": 4,
                "location": {"object_index": 1},
                "content": {"raw_text": ocr_payload},
                "parent_evidence_id": IMAGE_ID,
                "geometry": {
                    "coordinate_space": "image",
                    "coordinate_origin": "top_left",
                    "unit": "normalized_1000",
                    "x": 100,
                    "y": 100,
                    "width": 400,
                    "height": 80,
                },
                "provenance": {"extraction_method": "adaptive_local_ocr_provisional"},
                "native_properties": {
                    "agreement_type": "same_engine_agreement",
                    "quality_tier": "provisional",
                    "provisional_marker": MARKER,
                    "independent_engines": False,
                    "spatial_overlap": 1.0,
                    "bbox_coordinate_system": "display_oriented_top_left_normalized_1000",
                    "primary_confidence": 0.95,
                    "audit_confidence": 0.9,
                    "observation_provenance": {
                        "primary_pass": "apple_vision_primary",
                        "primary_engine": "apple_vision",
                        "primary_independence_group": "apple_vision",
                        "primary_line_id": "primary-1",
                        "primary_bbox_coordinate_system": (
                            "display_oriented_top_left_normalized_1000"
                        ),
                        "audit_pass": "apple_vision_literal",
                        "audit_engine": "apple_vision",
                        "audit_independence_group": "apple_vision",
                        "audit_line_id": "audit-1",
                        "audit_bbox_coordinate_system": (
                            "display_oriented_top_left_normalized_1000"
                        ),
                        "comparison_coordinate_system": (
                            "display_oriented_top_left_normalized_1000"
                        ),
                        "supporters": [
                            {
                                "pass": "apple_vision_primary",
                                "engine": "apple_vision",
                                "independence_group": "apple_vision",
                                "line_id": "primary-1",
                                "raw_text": ocr_payload,
                                "bbox": [100, 100, 400, 80],
                                "bbox_coordinate_system": (
                                    "display_oriented_top_left_normalized_1000"
                                ),
                                "confidence": 0.95,
                            },
                            {
                                "pass": "apple_vision_literal",
                                "engine": "apple_vision",
                                "independence_group": "apple_vision",
                                "line_id": "audit-1",
                                "raw_text": ocr_payload,
                                "bbox": [100, 100, 400, 80],
                                "bbox_coordinate_system": (
                                    "display_oriented_top_left_normalized_1000"
                                ),
                                "confidence": 0.9,
                            },
                        ],
                    },
                    "visual_origin": copy.deepcopy(image_origin),
                },
            }
            vlm = {
                "evidence_id": VLM_ID,
                "document_id": DOCUMENT_ID,
                "evidence_type": "text_block",
                "ordinal": 5,
                "location": {
                    "object_index": 2,
                    "locator_text": "location_status=unlocated;source=image",
                },
                "content": {"raw_text": f"{MARKER}\n{vlm_payload}"},
                "parent_evidence_id": IMAGE_ID,
                "provenance": {
                    "extraction_method": (
                        "local_vlm_unlocated_transcript_provisional"
                    ),
                    "deterministic": False,
                },
                "native_properties": {
                    "location_status": "unlocated",
                    "quality_tier": "provisional",
                    "provisional_marker": MARKER,
                    "transcript_type": "whole_image_faithful_transcript",
                    "question_independent": True,
                    "visual_origin": copy.deepcopy(image_origin),
                },
            }
            jsonl(intermediate / "documents.jsonl", [document])
            layer_evidence = [paragraph, table_cell, image, ocr, vlm]
            jsonl(intermediate / "evidence.jsonl", layer_evidence)
            (intermediate / "build-state.json").write_text(
                json.dumps(
                    {
                        "build_status": "complete",
                        "source_root": str(source_root),
                        "entries": {"sample.txt": {"document_id": DOCUMENT_ID}},
                    }
                ),
                encoding="utf-8",
            )

            search_units = [
                {
                    "search_unit_id": "su_table",
                    "document_id": DOCUMENT_ID,
                    "unit_type": "table_row",
                    "source_evidence_ids": [TABLE_CELL_ID],
                    "locator": {"row": 1},
                    "text": {"search_text": table_text, "sha256": text_sha256(table_text)},
                    "context": {"container_kind": "table"},
                },
                {
                    "search_unit_id": "su_image",
                    "document_id": DOCUMENT_ID,
                    "unit_type": "image_text_packet",
                    "source_evidence_ids": [IMAGE_ID, OCR_ID],
                    "locator": {
                        "object_index": 1,
                        "locator_text": (
                            "container_kind=standalone_image;"
                            "quality_tier=provisional;"
                            "bbox_coordinate_system="
                            "display_oriented_top_left_normalized_1000"
                        ),
                    },
                    "text": {"search_text": image_text, "sha256": text_sha256(image_text)},
                    "context": {
                        "container_kind": "standalone_image",
                        "quality_tier": "provisional",
                        "agreement_types": ["same_engine_agreement"],
                        "provisional_marker": MARKER,
                        "bbox_coordinate_system": "display_oriented_top_left_normalized_1000",
                        "reading_order_method": "geometry_row_bands_v1",
                        "row_band_count": 1,
                    },
                },
            ]
            jsonl(search / "search_units.jsonl", search_units)
            (search / "search-build-state.json").write_text("{}\n", encoding="utf-8")

            with mock.patch.object(adapter, "validate_search_units", return_value={"status": "PASS"}):
                state = adapter.adapt(intermediate, source_root, output, search)

            records = [
                json.loads(line)
                for line in (output / "semantic-evidence.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
                if line.strip()
            ]
            self.assertTrue(records)
            self.assertTrue(
                all(
                    len(record["observed_text"])
                    <= adapter.MAX_QUESTION_EVIDENCE_CHARS
                    for record in records
                )
            )

            groups = {
                "paragraph": [
                    item
                    for item in records
                    if item.get("adapter", {}).get("source_record_type") == "paragraph"
                ],
                "table_row": [
                    item
                    for item in records
                    if item.get("adapter", {}).get("unit_type") == "table_row"
                ],
                "image_text_packet": [
                    item
                    for item in records
                    if item.get("adapter", {}).get("unit_type") == "image_text_packet"
                ],
                "provisional_vlm_text": [
                    item
                    for item in records
                    if item.get("adapter", {}).get("source_record_type")
                    == "text_block"
                    and item.get("extraction_method")
                    == "local_vlm_unlocated_transcript_provisional"
                ],
            }
            for label, expected in {
                "paragraph": paragraph_text,
                "table_row": table_text,
                "image_text_packet": image_text,
                "provisional_vlm_text": vlm_text,
            }.items():
                with self.subTest(label):
                    self.assertGreater(len(groups[label]), 1)
                    self.assertEqual(reconstructed_payload(groups[label]), expected)
                    original_id = groups[label][0]["adapter"]["question_shard"][
                        "source_projection_id"
                    ]
                    self.assertNotIn(original_id, {item["evidence_id"] for item in records})
            for item in groups["image_text_packet"]:
                self.assertTrue(item["observed_text"].startswith(MARKER))
                self.assertEqual(item["quality_tier"], "provisional")
                self.assertEqual(item["provisional_marker"], MARKER)
            for item in groups["provisional_vlm_text"]:
                self.assertTrue(item["observed_text"].startswith(MARKER))
                self.assertEqual(item["quality_tier"], "provisional")
                self.assertEqual(item["provisional_marker"], MARKER)
                self.assertNotIn("agreement_types", item)

            expected_records = semantic_validator.expected_semantic_evidence(
                [document], layer_evidence, search_units
            )
            semantic_validator.validate_exact_projection(records, expected_records)

            self.assertEqual(state["adapter_version"], "0.7.0")
            self.assertEqual(state["question_sharding"]["max_observed_text_chars"], 1600)
            self.assertEqual(
                state["question_sharding"]["source_record_type_counts"]["paragraph"],
                1,
            )
            self.assertEqual(
                state["question_sharding"]["source_record_type_counts"][
                    "search_unit:table_row"
                ],
                1,
            )
            self.assertEqual(
                state["question_sharding"]["source_record_type_counts"][
                    "text_block"
                ],
                1,
            )
            document_record = json.loads(
                (output / "semantic-documents.jsonl").read_text(encoding="utf-8")
            )
            self.assertEqual(
                document_record["evidence_ids"],
                [item["evidence_id"] for item in records],
            )


if __name__ == "__main__":
    unittest.main()
