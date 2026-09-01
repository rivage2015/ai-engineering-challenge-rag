from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import sys
import unittest
from pathlib import Path

try:
    import jsonschema
except ModuleNotFoundError:  # The repository's minimal stdlib test runtime omits it.
    jsonschema = None


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import build_search_units as search_units  # noqa: E402
import build_lexical_index as lexical_index  # noqa: E402
import adapt_layer1_to_local_memory as adapter  # noqa: E402
import validate_intermediate_records_streaming as intermediate_validator  # noqa: E402
import validate_search_units as search_validator  # noqa: E402


ADAPTIVE_VALIDATOR_PATH = (
    ROOT
    / "distribution"
    / "macos-local-memory"
    / "engine"
    / "validate_adaptive_semantic_graph.py"
)
ADAPTIVE_VALIDATOR_SPEC = importlib.util.spec_from_file_location(
    "adaptive_semantic_validator_for_image_tests", ADAPTIVE_VALIDATOR_PATH
)
assert ADAPTIVE_VALIDATOR_SPEC is not None and ADAPTIVE_VALIDATOR_SPEC.loader is not None
adaptive_validator = importlib.util.module_from_spec(ADAPTIVE_VALIDATOR_SPEC)
ADAPTIVE_VALIDATOR_SPEC.loader.exec_module(adaptive_validator)


RUN_AT = "2026-09-01T00:00:00+00:00"
DOCUMENT_ID = "doc_" + "d" * 32
IMAGE_ID = "ev_" + "0" * 32
HIGH_ID = "ev_" + "1" * 32
SAME_ENGINE_ID = "ev_" + "2" * 32
SINGLE_PASS_ID = "ev_" + "3" * 32
PROVISIONAL_MARKER = "[暫定読取]"


def text_content(value: str) -> dict[str, object]:
    return {
        "raw_text": value,
        "value_type": "text",
        "sha256": hashlib.sha256(value.encode("utf-8")).hexdigest(),
        "is_truncated": False,
    }


def provenance(extraction_method: str) -> dict[str, object]:
    return {
        "extraction_method": extraction_method,
        "extractor": "intermediate-record-probe",
        "extractor_version": "0.1-test",
        "extracted_at": RUN_AT,
        "deterministic": False,
        "confidence": 0.9,
    }


def image_evidence() -> dict[str, object]:
    return {
        "schema_version": "0.1",
        "record_type": "evidence",
        "evidence_id": IMAGE_ID,
        "document_id": DOCUMENT_ID,
        "evidence_type": "image",
        "location": {"object_index": 1},
        "content": {
            "content_ref": "sample.png",
            "value_type": "binary",
            "sha256": "a" * 64,
            "is_truncated": False,
        },
        "provenance": provenance("verified_image_bytes"),
    }


def ocr_line(
    evidence_id: str,
    text: str,
    agreement_type: str,
    quality_tier: str,
) -> dict[str, object]:
    provisional = quality_tier == "provisional"
    native_properties: dict[str, object] = {
        "consensus_method": "spatial-nfc-exact-or-provisional",
        "agreement_type": agreement_type,
        "quality_tier": quality_tier,
        "bbox_coordinate_system": (
            "source_orientation_1_top_left_normalized_1000"
            if agreement_type == "independent_agreement"
            else "display_oriented_top_left_normalized_1000"
        ),
        "spatial_overlap": 0.0 if agreement_type == "provisional_single_pass" else 0.8,
        "primary_confidence": 0.95,
        "audit_confidence": None if agreement_type == "provisional_single_pass" else 0.9,
        "independent_engines": agreement_type == "independent_agreement",
        "observation_provenance": {"primary_pass": "fixture"},
    }
    if provisional:
        native_properties["provisional_marker"] = PROVISIONAL_MARKER
    return {
        "schema_version": "0.1",
        "record_type": "evidence",
        "evidence_id": evidence_id,
        "document_id": DOCUMENT_ID,
        "evidence_type": "ocr_line",
        "location": {"object_index": 1},
        "content": text_content(text),
        "parent_evidence_id": IMAGE_ID,
        "ordinal": 1,
        "geometry": {
            "coordinate_space": "image",
            "coordinate_origin": "top_left",
            "unit": "normalized_1000",
            "x": 100,
            "y": 100,
            "width": 400,
            "height": 80,
        },
        "native_properties": native_properties,
        "provenance": provenance(
            "dual_local_ocr_consensus"
            if quality_tier == "high"
            else "adaptive_local_ocr_provisional"
        ),
    }


class ImageQualityTierTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        evidence_schema = json.loads(
            (ROOT / "schemas" / "evidence.schema.json").read_text(encoding="utf-8")
        )
        search_schema = json.loads(
            (ROOT / "schemas" / "search-unit.schema.json").read_text(
                encoding="utf-8"
            )
        )
        if jsonschema is None:
            cls.evidence_validator = None
            cls.search_validator = None
        else:
            jsonschema.Draft202012Validator.check_schema(evidence_schema)
            jsonschema.Draft202012Validator.check_schema(search_schema)
            cls.evidence_validator = jsonschema.Draft202012Validator(evidence_schema)
            cls.search_validator = jsonschema.Draft202012Validator(search_schema)

    def test_public_evidence_schema_accepts_each_valid_image_tier(self) -> None:
        if self.evidence_validator is None:
            self.skipTest("jsonschema is not installed in the minimal test runtime")
        valid = [
            ocr_line(HIGH_ID, "独立合意", "independent_agreement", "high"),
            ocr_line(
                SAME_ENGINE_ID,
                "同一エンジン合意",
                "same_engine_agreement",
                "provisional",
            ),
            ocr_line(
                SINGLE_PASS_ID,
                "単独読取",
                "provisional_single_pass",
                "provisional",
            ),
        ]

        for record in valid:
            with self.subTest(record["native_properties"]["agreement_type"]):
                self.evidence_validator.validate(record)

    def test_public_evidence_schema_rejects_agreement_tier_contradictions(self) -> None:
        if self.evidence_validator is None:
            self.skipTest("jsonschema is not installed in the minimal test runtime")
        contradictions = [
            ocr_line(HIGH_ID, "誤ったhigh", "same_engine_agreement", "high"),
            ocr_line(HIGH_ID, "誤ったhigh", "provisional_single_pass", "high"),
            ocr_line(HIGH_ID, "誤った暫定", "independent_agreement", "provisional"),
        ]

        for record in contradictions:
            with self.subTest(record["native_properties"]):
                with self.assertRaises(jsonschema.ValidationError):
                    self.evidence_validator.validate(record)

    def test_public_evidence_schema_enforces_canonical_marker(self) -> None:
        if self.evidence_validator is None:
            self.skipTest("jsonschema is not installed in the minimal test runtime")
        missing = ocr_line(
            SAME_ENGINE_ID,
            "マーカーなし",
            "same_engine_agreement",
            "provisional",
        )
        missing["native_properties"].pop("provisional_marker")
        wrong = ocr_line(
            SINGLE_PASS_ID,
            "誤マーカー",
            "provisional_single_pass",
            "provisional",
        )
        wrong["native_properties"]["provisional_marker"] = "[仮読取]"
        high_marked = ocr_line(
            HIGH_ID,
            "highにマーカー",
            "independent_agreement",
            "high",
        )
        high_marked["native_properties"]["provisional_marker"] = PROVISIONAL_MARKER

        for record in (missing, wrong, high_marked):
            with self.subTest(record["content"]["raw_text"]):
                with self.assertRaises(jsonschema.ValidationError):
                    self.evidence_validator.validate(record)

    def test_builder_splits_high_and_provisional_image_packets(self) -> None:
        emitted: list[dict[str, object]] = []
        deriver = search_units.DocumentDeriver(
            DOCUMENT_ID,
            RUN_AT,
            emitted.append,
            500,
        )
        records = [
            image_evidence(),
            ocr_line(HIGH_ID, "独立合意", "independent_agreement", "high"),
            ocr_line(
                SAME_ENGINE_ID,
                "同一エンジン合意",
                "same_engine_agreement",
                "provisional",
            ),
            ocr_line(
                SINGLE_PASS_ID,
                "単独読取",
                "provisional_single_pass",
                "provisional",
            ),
        ]
        for record in records:
            deriver.consume(record)

        self.assertEqual(deriver.finish(), {"image_text_packet": 2})
        self.assertEqual(len(emitted), 2)
        by_tier = {record["context"]["quality_tier"]: record for record in emitted}
        self.assertEqual(set(by_tier), {"high", "provisional"})

        high = by_tier["high"]
        provisional = by_tier["provisional"]
        self.assertEqual(high["unit_type"], "image_text_packet")
        self.assertEqual(high["context"]["container_kind"], "standalone_image")
        self.assertEqual(high["context"]["agreement_types"], ["independent_agreement"])
        self.assertEqual(
            high["context"]["bbox_coordinate_system"],
            "source_orientation_1_top_left_normalized_1000",
        )
        self.assertEqual(high["context"]["reading_order_method"], "geometry_row_bands_v1")
        self.assertEqual(high["context"]["row_band_count"], 1)
        self.assertNotIn("provisional_marker", high["context"])
        self.assertNotIn(PROVISIONAL_MARKER, high["text"]["search_text"])
        self.assertEqual(set(high["source_evidence_ids"]), {IMAGE_ID, HIGH_ID})

        self.assertEqual(provisional["unit_type"], "image_text_packet")
        self.assertEqual(
            provisional["context"]["container_kind"], "standalone_image"
        )
        self.assertEqual(
            set(provisional["context"]["agreement_types"]),
            {"same_engine_agreement", "provisional_single_pass"},
        )
        self.assertEqual(
            provisional["context"]["provisional_marker"],
            PROVISIONAL_MARKER,
        )
        provisional_lines = provisional["text"]["search_text"].splitlines()[1:]
        self.assertTrue(provisional_lines)
        self.assertTrue(
            all(line.startswith(f"{PROVISIONAL_MARKER} ") for line in provisional_lines)
        )
        self.assertEqual(
            set(provisional["source_evidence_ids"]),
            {IMAGE_ID, SAME_ENGINE_ID, SINGLE_PASS_ID},
        )

        if self.search_validator is not None:
            for packet in emitted:
                self.search_validator.validate(packet)

    def test_builder_reconstructs_visual_rows_before_packetizing(self) -> None:
        left = ocr_line(
            "ev_" + "4" * 32, "左", "independent_agreement", "high"
        )
        right = ocr_line(
            "ev_" + "5" * 32, "右", "independent_agreement", "high"
        )
        bottom = ocr_line(
            "ev_" + "6" * 32, "下", "independent_agreement", "high"
        )
        left["geometry"].update({"x": 100, "y": 100, "width": 200, "height": 60})
        right["geometry"].update({"x": 600, "y": 105, "width": 200, "height": 60})
        bottom["geometry"].update({"x": 100, "y": 300, "width": 200, "height": 60})

        emitted: list[dict[str, object]] = []
        deriver = search_units.DocumentDeriver(
            DOCUMENT_ID, RUN_AT, emitted.append, 500
        )
        for record in (image_evidence(), bottom, right, left):
            deriver.consume(record)
        self.assertEqual(deriver.finish(), {"image_text_packet": 1})

        packet = emitted[0]
        self.assertEqual(
            packet["text"]["search_text"],
            "Image file: sample.png\n左 右\n下",
        )
        self.assertEqual(packet["context"]["row_band_count"], 2)
        self.assertEqual(
            packet["source_evidence_ids"],
            [IMAGE_ID, left["evidence_id"], right["evidence_id"], bottom["evidence_id"]],
        )

    def test_builder_never_merges_different_image_coordinate_frames(self) -> None:
        display = ocr_line(
            SAME_ENGINE_ID,
            "Vision回転後",
            "same_engine_agreement",
            "provisional",
        )
        raw = ocr_line(
            SINGLE_PASS_ID,
            "Tesseract生ラスタ",
            "provisional_single_pass",
            "provisional",
        )
        raw["native_properties"]["bbox_coordinate_system"] = (
            "raw_raster_top_left_normalized_1000"
        )

        emitted: list[dict[str, object]] = []
        deriver = search_units.DocumentDeriver(
            DOCUMENT_ID, RUN_AT, emitted.append, 500
        )
        for record in (image_evidence(), display, raw):
            deriver.consume(record)
        self.assertEqual(deriver.finish(), {"image_text_packet": 2})
        self.assertEqual(
            {packet["context"]["bbox_coordinate_system"] for packet in emitted},
            {
                "display_oriented_top_left_normalized_1000",
                "raw_raster_top_left_normalized_1000",
            },
        )
        self.assertTrue(all(packet["context"]["row_band_count"] == 1 for packet in emitted))
        self.assertTrue(all(len(packet["source_evidence_ids"]) == 2 for packet in emitted))

    def test_builder_rejects_agreement_tier_or_marker_contradictions(self) -> None:
        false_high = ocr_line(
            HIGH_ID,
            "同一エンジンなのにhigh",
            "same_engine_agreement",
            "high",
        )
        missing_marker = ocr_line(
            SAME_ENGINE_ID,
            "暫定なのにマーカーなし",
            "same_engine_agreement",
            "provisional",
        )
        missing_marker["native_properties"].pop("provisional_marker")

        for record, message in (
            (false_high, "quality tier disagrees"),
            (missing_marker, "canonical marker"),
        ):
            emitted: list[dict[str, object]] = []
            deriver = search_units.DocumentDeriver(
                DOCUMENT_ID,
                RUN_AT,
                emitted.append,
                500,
            )
            deriver.consume(image_evidence())
            with self.subTest(record["content"]["raw_text"]):
                with self.assertRaisesRegex(ValueError, message):
                    deriver.consume(record)
            self.assertEqual(emitted, [])

    def test_public_search_schema_rejects_mixed_or_unmarked_packets(self) -> None:
        if self.search_validator is None:
            self.skipTest("jsonschema is not installed in the minimal test runtime")
        valid_provisional = search_units.make_unit(
            DOCUMENT_ID,
            "image_text_packet",
            [IMAGE_ID, SAME_ENGINE_ID],
            {"object_index": 1},
            f"Image file: sample.png\n{PROVISIONAL_MARKER} 暫定読取",
            RUN_AT,
            {
                "container_kind": "standalone_image",
                "quality_tier": "provisional",
                "agreement_types": ["same_engine_agreement"],
                "provisional_marker": PROVISIONAL_MARKER,
                "bbox_coordinate_system": "display_oriented_top_left_normalized_1000",
                "reading_order_method": "geometry_row_bands_v1",
                "row_band_count": 1,
            },
        )
        self.search_validator.validate(valid_provisional)

        unmarked = copy.deepcopy(valid_provisional)
        unmarked["text"]["search_text"] = "Image file: sample.png\n暫定読取"
        unmarked["text"]["sha256"] = hashlib.sha256(
            unmarked["text"]["search_text"].encode("utf-8")
        ).hexdigest()
        unmarked["text"]["char_count"] = len(unmarked["text"]["search_text"])
        partially_unmarked = copy.deepcopy(valid_provisional)
        partially_unmarked["text"]["search_text"] = (
            f"Image file: sample.png\n{PROVISIONAL_MARKER} 暫定読取\nマーカーなし"
        )
        partially_unmarked["text"]["sha256"] = hashlib.sha256(
            partially_unmarked["text"]["search_text"].encode("utf-8")
        ).hexdigest()
        partially_unmarked["text"]["char_count"] = len(
            partially_unmarked["text"]["search_text"]
        )
        mixed = copy.deepcopy(valid_provisional)
        mixed["context"]["agreement_types"].append("independent_agreement")
        false_high = copy.deepcopy(valid_provisional)
        false_high["context"]["quality_tier"] = "high"
        missing_order = copy.deepcopy(valid_provisional)
        missing_order["context"].pop("reading_order_method")

        for packet in (unmarked, partially_unmarked, mixed, false_high, missing_order):
            with self.subTest(packet["context"]):
                with self.assertRaises(jsonschema.ValidationError):
                    self.search_validator.validate(packet)

    def test_adapter_and_semantic_validator_preserve_tier_lineage(self) -> None:
        high = ocr_line(HIGH_ID, "独立合意", "independent_agreement", "high")
        provisional = ocr_line(
            SAME_ENGINE_ID,
            "同一エンジン合意",
            "same_engine_agreement",
            "provisional",
        )
        self.assertEqual(
            adapter.ocr_evidence_quality(high),
            ("high", ["independent_agreement"], None),
        )
        contract = adapter.ocr_evidence_quality(provisional)
        self.assertEqual(
            contract,
            ("provisional", ["same_engine_agreement"], PROVISIONAL_MARKER),
        )
        projected = {
            "quality_tier": contract[0],
            "agreement_types": contract[1],
            "provisional_marker": contract[2],
            "observed_text": f"{PROVISIONAL_MARKER} 同一エンジン合意",
        }
        adaptive_validator.validate_quality_projection(
            projected,
            expected_tier=contract[0],
            expected_agreements=contract[1],
            expected_marker=contract[2],
            packet=False,
        )

        promoted = copy.deepcopy(provisional)
        promoted["native_properties"]["quality_tier"] = "high"
        with self.assertRaisesRegex(ValueError, "quality tier disagrees"):
            adapter.ocr_evidence_quality(promoted)
        projected.pop("provisional_marker")
        with self.assertRaisesRegex(ValueError, "marker_mismatch"):
            adaptive_validator.validate_quality_projection(
                projected,
                expected_tier=contract[0],
                expected_agreements=contract[1],
                expected_marker=contract[2],
                packet=False,
            )

    def test_semantic_validator_derives_projection_instead_of_trusting_output_labels(self) -> None:
        provisional = ocr_line(
            SAME_ENGINE_ID,
            "座標付き暂定読取",
            "same_engine_agreement",
            "provisional",
        )
        emitted: list[dict[str, object]] = []
        deriver = search_units.DocumentDeriver(
            DOCUMENT_ID, RUN_AT, emitted.append, 500
        )
        for record in (image_evidence(), provisional):
            deriver.consume(record)
        deriver.finish()
        document = {
            "document_id": DOCUMENT_ID,
            "source": {
                "relative_path": "sample.png",
                "sha256": "b" * 64,
            },
        }
        expected = adaptive_validator.expected_semantic_evidence(
            [document], [image_evidence(), provisional], emitted
        )
        self.assertEqual(len(expected), 2)

        disguised = copy.deepcopy(expected)
        disguised[0]["adapter"]["source_record_type"] = "paragraph"
        disguised[0]["observed_text"] = "座標付き暂定読取"
        for key in (
            "quality_tier",
            "agreement_types",
            "provisional_marker",
            "bbox_coordinate_system",
        ):
            disguised[0].pop(key, None)
        with self.assertRaisesRegex(ValueError, "projection_mismatch"):
            adaptive_validator.validate_exact_projection(disguised, expected)

        altered_source_values = copy.deepcopy(expected)
        altered_source_values[0]["geometry"]["x"] += 1
        altered_source_values[1]["observed_text"] = (
            f"Image file: sample.png\n{PROVISIONAL_MARKER} 別の文字列"
        )
        altered_source_values[1]["locator"]["locator_text"] = "偽の位置"
        with self.assertRaisesRegex(ValueError, "projection_mismatch"):
            adaptive_validator.validate_exact_projection(
                altered_source_values, expected
            )

    def test_structural_streaming_validator_enforces_ocr_quality_without_schema(self) -> None:
        valid = ocr_line(
            SINGLE_PASS_ID,
            "単独読取",
            "provisional_single_pass",
            "provisional",
        )
        self.assertEqual(
            intermediate_validator.image_ocr_contract_errors(valid, "fixture"),
            [],
        )
        missing_marker = copy.deepcopy(valid)
        missing_marker["native_properties"].pop("provisional_marker")
        self.assertTrue(
            any(
                "canonical marker" in error
                for error in intermediate_validator.image_ocr_contract_errors(
                    missing_marker, "fixture"
                )
            )
        )
        false_high = ocr_line(
            SAME_ENGINE_ID,
            "誤ったhigh",
            "same_engine_agreement",
            "high",
        )
        self.assertTrue(
            any(
                "requires tier 'provisional'" in error
                for error in intermediate_validator.image_ocr_contract_errors(
                    false_high, "fixture"
                )
            )
        )

    def test_search_validator_rejects_cross_tier_source_in_one_packet(self) -> None:
        high = ocr_line(HIGH_ID, "独立合意", "independent_agreement", "high")
        provisional = ocr_line(
            SAME_ENGINE_ID,
            "同一エンジン合意",
            "same_engine_agreement",
            "provisional",
        )
        packet = search_units.make_unit(
            DOCUMENT_ID,
            "image_text_packet",
            [IMAGE_ID, HIGH_ID],
            {"object_index": 1, "locator_text": "quality_tier=high"},
            "Image file: sample.png\n独立合意",
            RUN_AT,
            {
                "container_kind": "standalone_image",
                "quality_tier": "high",
                "agreement_types": ["independent_agreement"],
                "bbox_coordinate_system": "source_orientation_1_top_left_normalized_1000",
                "reading_order_method": "geometry_row_bands_v1",
                "row_band_count": 1,
            },
        )
        evidence = {
            IMAGE_ID: image_evidence(),
            HIGH_ID: high,
            SAME_ENGINE_ID: provisional,
        }
        self.assertEqual(
            search_validator.image_packet_contract_errors(packet, "fixture", evidence),
            [],
        )
        packet["source_evidence_ids"].append(SAME_ENGINE_ID)
        self.assertTrue(
            any(
                "mixes source Evidence quality tiers" in error
                for error in search_validator.image_packet_contract_errors(
                    packet, "fixture", evidence
                )
            )
        )

    def test_bm25_ignores_only_repeated_provisional_audit_labels(self) -> None:
        packet = search_units.make_unit(
            DOCUMENT_ID,
            "image_text_packet",
            [IMAGE_ID, SAME_ENGINE_ID, SINGLE_PASS_ID],
            {"object_index": 1, "locator_text": "quality_tier=provisional"},
            (
                "Image file: sample.png\n"
                f"{PROVISIONAL_MARKER} 同一エンジン合意\n"
                f"{PROVISIONAL_MARKER} 単独読取"
            ),
            RUN_AT,
            {
                "container_kind": "standalone_image",
                "quality_tier": "provisional",
                "agreement_types": [
                    "provisional_single_pass",
                    "same_engine_agreement",
                ],
                "provisional_marker": PROVISIONAL_MARKER,
                "bbox_coordinate_system": "display_oriented_top_left_normalized_1000",
                "reading_order_method": "geometry_row_bands_v1",
                "row_band_count": 2,
            },
        )
        indexed = lexical_index.indexable_search_text(packet)

        self.assertNotIn(PROVISIONAL_MARKER, indexed)
        self.assertIn("同一エンジン合意", indexed)
        self.assertIn("単独読取", indexed)
        self.assertIn(PROVISIONAL_MARKER, packet["text"]["search_text"])


if __name__ == "__main__":
    unittest.main()
