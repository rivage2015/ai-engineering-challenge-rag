from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import sqlite3
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
import validate_search_units_streaming as streaming_search_validator  # noqa: E402


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


def visual_origin(kind: str = "standalone_image") -> dict[str, object]:
    materialization: dict[str, object] = {
        "runner": "fixture",
        "external_network_used": False,
        "source_sha256": "b" * 64,
        "rendered_sha256": "b" * 64,
    }
    if kind in {"office_embedded_image", "notebook_embedded_image"}:
        materialization["embedded_sha256"] = "b" * 64
    return {
        "kind": kind,
        "source_relative_path": "sample.png",
        "source_sha256": "b" * 64,
        "source_location": {"object_index": 1},
        "materialization": materialization,
    }


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
        "native_properties": {
            "source_sha256": "b" * 64,
            "visual_origin": visual_origin(),
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
    bbox = [100, 100, 400, 80]
    primary_pass = "apple_vision_primary"
    primary_engine = "apple_vision"
    if agreement_type in {
        "independent_agreement", "display_transform_unresolved",
    }:
        audit_pass = "tesseract_psm3"
        audit_engine = "tesseract"
    elif agreement_type == "same_engine_agreement":
        audit_pass = "apple_vision_literal"
        audit_engine = "apple_vision"
    else:
        audit_pass = None
        audit_engine = None
    bbox_coordinate_system = (
        "source_orientation_1_top_left_normalized_1000"
        if agreement_type == "independent_agreement"
        else (
            "raw_raster_top_left_normalized_1000"
            if agreement_type == "display_transform_unresolved"
            else "display_oriented_top_left_normalized_1000"
        )
    )
    supporters = [{
        "pass": primary_pass,
        "engine": primary_engine,
        "independence_group": primary_engine,
        "line_id": "primary-1",
        "raw_text": text,
        "bbox": bbox,
        "bbox_coordinate_system": bbox_coordinate_system,
        "confidence": 0.95,
    }]
    if audit_pass is not None:
        supporters.append({
            "pass": audit_pass,
            "engine": audit_engine,
            "independence_group": audit_engine,
            "line_id": "audit-1",
            "raw_text": text,
            "bbox": bbox,
            "bbox_coordinate_system": bbox_coordinate_system,
            "confidence": 0.9,
        })
    observation_provenance: dict[str, object] = {
        "primary_pass": primary_pass,
        "primary_engine": primary_engine,
        "primary_independence_group": primary_engine,
        "primary_line_id": "primary-1",
        "primary_bbox_coordinate_system": bbox_coordinate_system,
        "supporters": supporters,
    }
    if audit_pass is not None:
        observation_provenance.update({
            "audit_pass": audit_pass,
            "audit_engine": audit_engine,
            "audit_independence_group": audit_engine,
            "audit_line_id": "audit-1",
            "audit_bbox_coordinate_system": bbox_coordinate_system,
            "comparison_coordinate_system": bbox_coordinate_system,
        })
    native_properties: dict[str, object] = {
        "consensus_method": "spatial-nfc-exact-or-provisional",
        "agreement_type": agreement_type,
        "quality_tier": quality_tier,
        "bbox_coordinate_system": bbox_coordinate_system,
        "spatial_overlap": 0.0 if agreement_type == "provisional_single_pass" else 1.0,
        "primary_confidence": 0.95,
        "audit_confidence": None if agreement_type == "provisional_single_pass" else 0.9,
        "independent_engines": agreement_type in {
            "independent_agreement", "display_transform_unresolved",
        },
        "observation_provenance": observation_provenance,
        "visual_origin": visual_origin(),
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
            "x": bbox[0],
            "y": bbox[1],
            "width": bbox[2],
            "height": bbox[3],
        },
        "native_properties": native_properties,
        "provenance": provenance(
            "dual_local_ocr_consensus"
            if quality_tier == "high"
            else "adaptive_local_ocr_provisional"
        ),
    }


def office_provisional_ocr(
    agreement_type: str,
    evidence_id: str,
    *,
    container_kind: str = "office_embedded_image",
) -> dict[str, object]:
    record = ocr_line(
        evidence_id,
        "Office埋込画像の暫定読取",
        agreement_type,
        "provisional",
    )
    origin = visual_origin(container_kind)
    origin["source_relative_path"] = (
        "sample.docx"
        if container_kind == "office_embedded_image"
        else "sample.ipynb"
    )
    origin["materialization"].update({
        "display_transform_resolved": False,
        "display_transform_status": "unresolved",
    })
    record["native_properties"].update({
        "display_transform_resolved": False,
        "visual_origin": origin,
    })
    if agreement_type == "display_transform_unresolved":
        record["native_properties"]["embedded_source_agreement_type"] = (
            "independent_agreement"
        )
    return record


def display_transform_unresolved_ocr(
    evidence_id: str = "ev_" + "4" * 32,
) -> dict[str, object]:
    return office_provisional_ocr(
        "display_transform_unresolved", evidence_id
    )


def provisional_vlm_text(
    evidence_id: str,
    *,
    method: str = "local_vlm_unlocated_transcript_provisional",
    evidence_type: str = "text_block",
) -> dict[str, object]:
    native_properties: dict[str, object] = {
        "quality_tier": "provisional",
        "provisional_marker": PROVISIONAL_MARKER,
        "question_independent": True,
        "visual_origin": visual_origin(),
    }
    if method == "local_vlm_unlocated_transcript_provisional":
        native_properties.update({
            "location_status": "unlocated",
            "transcript_type": "whole_image_faithful_transcript",
        })
    return {
        "schema_version": "0.1",
        "record_type": "evidence",
        "evidence_id": evidence_id,
        "document_id": DOCUMENT_ID,
        "evidence_type": evidence_type,
        "location": {"object_index": 2},
        "content": text_content(f"{PROVISIONAL_MARKER}\n画像からの暫定読取"),
        "parent_evidence_id": IMAGE_ID,
        "ordinal": 2,
        "native_properties": native_properties,
        "provenance": provenance(method),
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
            display_transform_unresolved_ocr(),
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

    def test_unresolved_office_display_transform_stays_provisional(self) -> None:
        source = display_transform_unresolved_ocr()
        parent = image_evidence()
        parent["native_properties"]["embedded_sha256"] = "b" * 64
        parent["native_properties"]["visual_origin"] = copy.deepcopy(
            source["native_properties"]["visual_origin"]
        )
        document = {
            "document_id": DOCUMENT_ID,
            "source": {"relative_path": "sample.docx", "sha256": "b" * 64},
        }
        documents = {DOCUMENT_ID: document}
        evidence = {
            parent["evidence_id"]: parent,
            source["evidence_id"]: source,
        }
        self.assertEqual(
            intermediate_validator.image_ocr_contract_errors(source, "fixture"),
            [],
        )
        self.assertEqual(
            intermediate_validator.visual_source_binding_contract_errors(
                source, parent, document, "fixture"
            ),
            [],
        )
        self.assertEqual(
            adaptive_validator.layer_ocr_quality(source),
            (
                "provisional",
                ["display_transform_unresolved"],
                PROVISIONAL_MARKER,
            ),
        )
        adaptive_validator.validate_layer_visual_source_binding(
            source, evidence, documents
        )
        if self.evidence_validator is not None:
            self.evidence_validator.validate(source)

        emitted: list[dict[str, object]] = []
        deriver = search_units.DocumentDeriver(
            DOCUMENT_ID, RUN_AT, emitted.append, 500
        )
        for record in (parent, source):
            deriver.consume(record)
        self.assertEqual(deriver.finish(), {"image_text_packet": 1})
        packet = emitted[0]
        self.assertEqual(
            packet["context"]["agreement_types"],
            ["display_transform_unresolved"],
        )
        self.assertEqual(
            search_validator.image_packet_contract_errors(
                packet, "fixture", evidence, documents
            ),
            [],
        )
        adaptive_validator.layer_image_packet_quality(
            packet, evidence, documents
        )
        if self.search_validator is not None:
            self.search_validator.validate(packet)

        def streaming_reconstruction_errors(
            candidate: dict[str, object],
        ) -> list[str]:
            connection = sqlite3.connect(":memory:")
            streaming_search_validator.initialize(connection)
            connection.execute(
                "INSERT INTO documents VALUES (?, ?)",
                (
                    DOCUMENT_ID,
                    streaming_search_validator.canonical_json(document),
                ),
            )
            for record in (parent, candidate):
                native = record.get("native_properties", {})
                connection.execute(
                    "INSERT INTO evidence VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        record["evidence_id"], record["document_id"],
                        record.get("evidence_type"), native.get("quality_tier"),
                        native.get("agreement_type"),
                        native.get("provisional_marker"),
                        native.get("bbox_coordinate_system"),
                        record.get("parent_evidence_id"),
                        streaming_search_validator.canonical_json(record),
                    ),
                )
            errors = streaming_search_validator.image_packet_reconstruction_errors(
                packet, "fixture", connection
            )
            connection.close()
            return errors

        self.assertEqual(streaming_reconstruction_errors(source), [])

        tampers: list[tuple[str, dict[str, object]]] = []
        for key, value in (
            ("display_transform_resolved", True),
            ("embedded_source_agreement_type", "same_engine_agreement"),
            ("independent_engines", False),
        ):
            changed = copy.deepcopy(source)
            changed["native_properties"][key] = value
            tampers.append((key, changed))
        changed_status = copy.deepcopy(source)
        changed_status["native_properties"]["visual_origin"]["materialization"][
            "display_transform_status"
        ] = "resolved"
        tampers.append(("materialization_status", changed_status))
        changed_origin = copy.deepcopy(source)
        changed_origin["native_properties"]["visual_origin"]["kind"] = (
            "standalone_image"
        )
        tampers.append(("origin_kind", changed_origin))
        missing_supporter = copy.deepcopy(source)
        missing_supporter["native_properties"]["observation_provenance"][
            "supporters"
        ].pop()
        tampers.append(("missing_supporter", missing_supporter))
        restored_high = copy.deepcopy(source)
        restored_high["native_properties"].update({
            "agreement_type": "independent_agreement",
            "quality_tier": "high",
        })
        restored_high["native_properties"].pop("provisional_marker")
        restored_high["provenance"]["extraction_method"] = (
            "dual_local_ocr_consensus"
        )
        tampers.append(("self_consistent_high_restoration", restored_high))
        self.assertTrue(streaming_reconstruction_errors(restored_high))

        for name, changed in tampers:
            changed_evidence = {
                parent["evidence_id"]: parent,
                changed["evidence_id"]: changed,
            }
            with self.subTest(validator="intermediate", tamper=name):
                self.assertTrue(
                    intermediate_validator.image_ocr_contract_errors(
                        changed, "fixture"
                    )
                )
            with self.subTest(validator="search", tamper=name):
                self.assertTrue(
                    search_validator.image_packet_contract_errors(
                        packet, "fixture", changed_evidence, documents
                    )
                )
            with self.subTest(validator="adaptive", tamper=name):
                with self.assertRaises(ValueError):
                    adaptive_validator.layer_ocr_quality(changed)
            if self.evidence_validator is not None and name != "missing_supporter":
                with self.subTest(validator="schema", tamper=name):
                    with self.assertRaises(jsonschema.ValidationError):
                        self.evidence_validator.validate(changed)

    def test_existing_provisional_agreements_survive_embedded_display_downgrade(
        self,
    ) -> None:
        for container_kind in (
            "office_embedded_image", "notebook_embedded_image",
        ):
            relative_path = (
                "sample.docx"
                if container_kind == "office_embedded_image"
                else "sample.ipynb"
            )
            document = {
                "document_id": DOCUMENT_ID,
                "source": {"relative_path": relative_path, "sha256": "b" * 64},
            }
            documents = {DOCUMENT_ID: document}
            for index, agreement in enumerate((
                "same_engine_agreement", "provisional_single_pass",
            )):
                with self.subTest(container=container_kind, agreement=agreement):
                    source = office_provisional_ocr(
                        agreement,
                        "ev_" + ("a" if index == 0 else "b") * 32,
                        container_kind=container_kind,
                    )
                    parent = image_evidence()
                    parent["native_properties"]["embedded_sha256"] = "b" * 64
                    parent["native_properties"]["visual_origin"] = copy.deepcopy(
                        source["native_properties"]["visual_origin"]
                    )
                    evidence = {
                        parent["evidence_id"]: parent,
                        source["evidence_id"]: source,
                    }
                    self.assertEqual(
                        intermediate_validator.image_ocr_contract_errors(
                            source, "fixture"
                        ),
                        [],
                    )
                    self.assertEqual(
                        intermediate_validator.visual_source_binding_contract_errors(
                            source, parent, document, "fixture"
                        ),
                        [],
                    )
                    adaptive_validator.layer_ocr_quality(source)
                    adaptive_validator.validate_layer_visual_source_binding(
                        source, evidence, documents
                    )
                    if self.evidence_validator is not None:
                        self.evidence_validator.validate(source)

                    emitted: list[dict[str, object]] = []
                    deriver = search_units.DocumentDeriver(
                        DOCUMENT_ID, RUN_AT, emitted.append, 500
                    )
                    for record in (parent, source):
                        deriver.consume(record)
                    deriver.finish()
                    self.assertEqual(len(emitted), 1)
                    packet = emitted[0]
                    self.assertEqual(
                        search_validator.image_packet_contract_errors(
                            packet, "fixture", evidence, documents
                        ),
                        [],
                    )
                    adaptive_validator.layer_image_packet_quality(
                        packet, evidence, documents
                    )

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

    def test_independent_validators_reject_image_packet_tampering(self) -> None:
        parent = image_evidence()
        left = ocr_line(
            "ev_" + "4" * 32, "左", "independent_agreement", "high"
        )
        right = ocr_line(
            "ev_" + "5" * 32, "右", "independent_agreement", "high"
        )
        for record, bbox in (
            (left, [100, 100, 200, 60]),
            (right, [600, 105, 200, 60]),
        ):
            record["geometry"].update({
                "x": bbox[0], "y": bbox[1],
                "width": bbox[2], "height": bbox[3],
            })
            for supporter in record["native_properties"]["observation_provenance"]["supporters"]:
                supporter["bbox"] = list(bbox)

        emitted: list[dict[str, object]] = []
        deriver = search_units.DocumentDeriver(
            DOCUMENT_ID, RUN_AT, emitted.append, 500
        )
        for record in (parent, right, left):
            deriver.consume(record)
        deriver.finish()
        valid = emitted[0]
        evidence = {
            parent["evidence_id"]: parent,
            left["evidence_id"]: left,
            right["evidence_id"]: right,
        }
        documents = {
            DOCUMENT_ID: {
                "document_id": DOCUMENT_ID,
                "source": {
                    "relative_path": "sample.png",
                    "sha256": "b" * 64,
                },
            }
        }
        self.assertEqual(
            search_validator.image_packet_contract_errors(
                valid, "fixture", evidence, documents
            ),
            [],
        )
        self.assertEqual(
            adaptive_validator.layer_image_packet_quality(
                valid, evidence, documents
            ),
            ("high", ["independent_agreement"], None),
        )

        text_tamper = copy.deepcopy(valid)
        text_tamper["text"]["search_text"] = "Image file: sample.png\n偽造本文"
        source_order_tamper = copy.deepcopy(valid)
        source_order_tamper["source_evidence_ids"][1:] = reversed(
            source_order_tamper["source_evidence_ids"][1:]
        )
        locator_tamper = copy.deepcopy(valid)
        locator_tamper["locator"]["object_index"] = 99
        container_tamper = copy.deepcopy(valid)
        container_tamper["context"]["container_kind"] = "pdf_page_image"

        other_parent = copy.deepcopy(parent)
        other_parent["evidence_id"] = "ev_" + "f" * 32
        other_parent["location"] = {"object_index": 2}
        other_parent["content"]["content_ref"] = "other.png"
        other_parent["native_properties"]["visual_origin"] = {
            **visual_origin(),
            "source_relative_path": "other.png",
            "source_location": {"object_index": 2},
        }
        wrong_parent = copy.deepcopy(valid)
        wrong_parent["source_evidence_ids"][0] = other_parent["evidence_id"]

        bad_origin_evidence = copy.deepcopy(evidence)
        bad_origin_evidence[parent["evidence_id"]]["native_properties"][
            "visual_origin"
        ]["kind"] = "office_embedded_image"

        origin_tampers: list[tuple[str, dict[str, dict[str, object]]]] = []
        for name, mutate in (
            (
                "child_source_hash",
                lambda origin: origin.__setitem__("source_sha256", "0" * 64),
            ),
            (
                "child_source_path",
                lambda origin: origin.__setitem__("source_relative_path", "other.docx"),
            ),
            (
                "child_source_location",
                lambda origin: origin.__setitem__("source_location", {}),
            ),
            (
                "child_embedded_digest",
                lambda origin: origin["materialization"].__setitem__(
                    "rendered_sha256", "0" * 64
                ),
            ),
        ):
            altered = copy.deepcopy(evidence)
            mutate(
                altered[left["evidence_id"]]["native_properties"]["visual_origin"]
            )
            origin_tampers.append((name, altered))
        child_origin_deleted = copy.deepcopy(evidence)
        child_origin_deleted[left["evidence_id"]]["native_properties"].pop(
            "visual_origin"
        )
        origin_tampers.append(("child_origin_deleted", child_origin_deleted))
        child_materialization_deleted = copy.deepcopy(evidence)
        child_materialization_deleted[left["evidence_id"]]["native_properties"][
            "visual_origin"
        ].pop("materialization")
        origin_tampers.append((
            "child_materialization_deleted", child_materialization_deleted
        ))
        parent_origin_deleted = copy.deepcopy(evidence)
        parent_origin_deleted[parent["evidence_id"]]["native_properties"].pop(
            "visual_origin"
        )
        origin_tampers.append(("parent_origin_deleted", parent_origin_deleted))
        parent_materialization_deleted = copy.deepcopy(evidence)
        parent_materialization_deleted[parent["evidence_id"]]["native_properties"][
            "visual_origin"
        ].pop("materialization")
        origin_tampers.append((
            "parent_materialization_deleted", parent_materialization_deleted
        ))

        cases = [
            ("text", text_tamper, evidence),
            ("source_order", source_order_tamper, evidence),
            ("wrong_parent", wrong_parent, {**evidence, other_parent["evidence_id"]: other_parent}),
            ("locator", locator_tamper, evidence),
            ("container", container_tamper, evidence),
            ("origin", valid, bad_origin_evidence),
            *((name, valid, altered) for name, altered in origin_tampers),
        ]
        for label, packet, source_records in cases:
            with self.subTest(validator="search", tamper=label):
                self.assertTrue(
                    search_validator.image_packet_contract_errors(
                        packet, "fixture", source_records, documents
                    )
                )
            with self.subTest(validator="adaptive", tamper=label):
                with self.assertRaises(ValueError):
                    adaptive_validator.layer_image_packet_quality(
                        packet, source_records, documents
                    )

        connection = sqlite3.connect(":memory:")
        streaming_search_validator.initialize(connection)
        connection.execute(
            "INSERT INTO documents VALUES (?, ?)",
            (
                DOCUMENT_ID,
                streaming_search_validator.canonical_json(
                    documents[DOCUMENT_ID]
                ),
            ),
        )
        for record in (*evidence.values(), other_parent):
            native = record.get("native_properties", {})
            connection.execute(
                "INSERT INTO evidence VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    record["evidence_id"], record["document_id"],
                    record.get("evidence_type"), native.get("quality_tier"),
                    native.get("agreement_type"), native.get("provisional_marker"),
                    native.get("bbox_coordinate_system"),
                    record.get("parent_evidence_id"),
                    streaming_search_validator.canonical_json(record),
                ),
            )
        for label, packet in (
            ("text", text_tamper),
            ("source_order", source_order_tamper),
            ("wrong_parent", wrong_parent),
            ("locator", locator_tamper),
            ("container", container_tamper),
        ):
            with self.subTest(validator="streaming", tamper=label):
                self.assertTrue(
                    streaming_search_validator.image_packet_reconstruction_errors(
                        packet, "fixture", connection
                    )
                )
        connection.close()

        self_consistent_forgery = copy.deepcopy(evidence)
        for record in self_consistent_forgery.values():
            origin = record["native_properties"]["visual_origin"]
            origin["source_relative_path"] = "other.docx"
            origin["source_sha256"] = "0" * 64
            origin["materialization"]["source_sha256"] = "0" * 64
        forged_connection = sqlite3.connect(":memory:")
        streaming_search_validator.initialize(forged_connection)
        forged_connection.execute(
            "INSERT INTO documents VALUES (?, ?)",
            (
                DOCUMENT_ID,
                streaming_search_validator.canonical_json(
                    documents[DOCUMENT_ID]
                ),
            ),
        )
        for record in self_consistent_forgery.values():
            native = record["native_properties"]
            forged_connection.execute(
                "INSERT INTO evidence VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    record["evidence_id"], record["document_id"],
                    record.get("evidence_type"), native.get("quality_tier"),
                    native.get("agreement_type"), native.get("provisional_marker"),
                    native.get("bbox_coordinate_system"),
                    record.get("parent_evidence_id"),
                    streaming_search_validator.canonical_json(record),
                ),
            )
        self.assertTrue(
            streaming_search_validator.image_packet_reconstruction_errors(
                valid, "fixture", forged_connection
            )
        )
        forged_connection.close()

    def test_document_format_rejects_self_consistent_visual_kind_relabeling(
        self,
    ) -> None:
        cases = (
            ("source.pdf", "standalone_image"),
            ("source.docx", "standalone_image"),
            ("source.ipynb", "standalone_image"),
            ("source.png", "pdf_page_image"),
        )
        for relative_path, forged_kind in cases:
            with self.subTest(path=relative_path, forged_kind=forged_kind):
                parent = image_evidence()
                parent["content"]["content_ref"] = relative_path
                forged_origin = visual_origin(forged_kind)
                forged_origin["source_relative_path"] = relative_path
                parent["native_properties"]["visual_origin"] = copy.deepcopy(
                    forged_origin
                )
                child = ocr_line(
                    HIGH_ID,
                    "自己整合した改ざん",
                    "independent_agreement",
                    "high",
                )
                child["native_properties"]["visual_origin"] = copy.deepcopy(
                    forged_origin
                )
                evidence = {
                    parent["evidence_id"]: parent,
                    child["evidence_id"]: child,
                }
                documents = {
                    DOCUMENT_ID: {
                        "document_id": DOCUMENT_ID,
                        "source": {
                            "relative_path": relative_path,
                            "sha256": "b" * 64,
                        },
                    }
                }
                emitted: list[dict[str, object]] = []
                deriver = search_units.DocumentDeriver(
                    DOCUMENT_ID, RUN_AT, emitted.append, 500
                )
                deriver.consume(parent)
                deriver.consume(child)
                deriver.finish()
                packet = emitted[0]

                self.assertTrue(
                    search_validator.image_packet_contract_errors(
                        packet, "fixture", evidence, documents
                    )
                )
                with self.assertRaisesRegex(ValueError, "document_kind_mismatch"):
                    adaptive_validator.layer_image_packet_quality(
                        packet, evidence, documents
                    )

                connection = sqlite3.connect(":memory:")
                streaming_search_validator.initialize(connection)
                connection.execute(
                    "INSERT INTO documents VALUES (?, ?)",
                    (
                        DOCUMENT_ID,
                        streaming_search_validator.canonical_json(
                            documents[DOCUMENT_ID]
                        ),
                    ),
                )
                for record in evidence.values():
                    native = record.get("native_properties", {})
                    connection.execute(
                        "INSERT INTO evidence VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            record["evidence_id"], record["document_id"],
                            record.get("evidence_type"),
                            native.get("quality_tier"),
                            native.get("agreement_type"),
                            native.get("provisional_marker"),
                            native.get("bbox_coordinate_system"),
                            record.get("parent_evidence_id"),
                            streaming_search_validator.canonical_json(record),
                        ),
                    )
                self.assertTrue(
                    streaming_search_validator.image_packet_reconstruction_errors(
                        packet, "fixture", connection
                    )
                )
                connection.close()

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

    def test_ocr_text_that_starts_with_image_file_is_not_a_second_header(self) -> None:
        parent = image_evidence()
        source = ocr_line(
            HIGH_ID,
            "Image file: invoice.png",
            "independent_agreement",
            "high",
        )
        emitted: list[dict[str, object]] = []
        deriver = search_units.DocumentDeriver(
            DOCUMENT_ID, RUN_AT, emitted.append, 500
        )
        for record in (parent, source):
            deriver.consume(record)
        deriver.finish()
        packet = emitted[0]
        evidence = {IMAGE_ID: parent, HIGH_ID: source}
        self.assertEqual(packet["context"]["row_band_count"], 1)
        self.assertEqual(
            packet["text"]["search_text"],
            "Image file: sample.png\nImage file: invoice.png",
        )
        self.assertEqual(
            search_validator.image_packet_contract_errors(
                packet, "fixture", evidence
            ),
            [],
        )
        self.assertEqual(
            streaming_search_validator.image_packet_contract_errors(
                packet, "fixture"
            ),
            [],
        )
        adaptive_validator.layer_image_packet_quality(packet, evidence)

    def test_embedded_image_digest_origin_tampering_fails_closed(self) -> None:
        parent = image_evidence()
        parent["location"] = {
            "source_member": "word/media/image1.png",
            "object_index": 1,
        }
        parent["content"]["content_ref"] = (
            "sample.docx::word/media/image1.png"
        )
        child = display_transform_unresolved_ocr(HIGH_ID)
        parent_origin = copy.deepcopy(
            child["native_properties"]["visual_origin"]
        )
        parent_origin["source_relative_path"] = "sample.docx"
        parent_origin["source_location"] = copy.deepcopy(parent["location"])
        parent["native_properties"] = {
            "embedded_sha256": "b" * 64,
            "visual_origin": parent_origin,
        }
        child["location"] = {
            "source_member": "word/media/image1.png",
            "image_object_index": 1,
            "object_index": 1,
        }
        child["native_properties"]["visual_origin"] = copy.deepcopy(
            parent_origin
        )

        emitted: list[dict[str, object]] = []
        deriver = search_units.DocumentDeriver(
            DOCUMENT_ID, RUN_AT, emitted.append, 500
        )
        deriver.consume(parent)
        deriver.consume(child)
        deriver.finish()
        packet = emitted[0]
        documents = {
            DOCUMENT_ID: {
                "document_id": DOCUMENT_ID,
                "source": {
                    "relative_path": "sample.docx",
                    "sha256": "b" * 64,
                },
            }
        }
        valid_evidence = {
            parent["evidence_id"]: parent,
            child["evidence_id"]: child,
        }
        self.assertEqual(
            search_validator.image_packet_contract_errors(
                packet, "fixture", valid_evidence, documents
            ),
            [],
        )
        self.assertEqual(
            adaptive_validator.layer_image_packet_quality(
                packet, valid_evidence, documents
            ),
            (
                "provisional",
                ["display_transform_unresolved"],
                PROVISIONAL_MARKER,
            ),
        )

        for digest_key in (
            "embedded_sha256", "rendered_sha256", "source_sha256"
        ):
            tampered = copy.deepcopy(valid_evidence)
            tampered[child["evidence_id"]]["native_properties"][
                "visual_origin"
            ]["materialization"][digest_key] = "0" * 64
            with self.subTest(validator="search", digest=digest_key):
                self.assertTrue(
                    search_validator.image_packet_contract_errors(
                        packet, "fixture", tampered, documents
                    )
                )
            with self.subTest(validator="adaptive", digest=digest_key):
                with self.assertRaises(ValueError):
                    adaptive_validator.layer_image_packet_quality(
                        packet, tampered, documents
                    )

        # Evidence IDs do not bind native properties.  A forged family can
        # therefore be internally consistent while relabelling an unresolved
        # Office rendering as a high-confidence standalone image.  The
        # Document format must independently keep that family provisional.
        forged_evidence = copy.deepcopy(valid_evidence)
        forged_parent = forged_evidence[parent["evidence_id"]]
        forged_child = forged_evidence[child["evidence_id"]]
        forged_origin = forged_parent["native_properties"]["visual_origin"]
        forged_origin["kind"] = "standalone_image"
        forged_parent["native_properties"]["source_sha256"] = "b" * 64
        forged_child["native_properties"]["visual_origin"] = copy.deepcopy(
            forged_origin
        )
        forged_child["native_properties"].update({
            "agreement_type": "independent_agreement",
            "quality_tier": "high",
            "bbox_coordinate_system": (
                "source_orientation_1_top_left_normalized_1000"
            ),
        })
        forged_child["native_properties"].pop("provisional_marker")
        forged_child["native_properties"].pop(
            "embedded_source_agreement_type"
        )
        forged_child["provenance"]["extraction_method"] = (
            "dual_local_ocr_consensus"
        )
        forged_observation = forged_child["native_properties"][
            "observation_provenance"
        ]
        forged_observation.update({
            "primary_bbox_coordinate_system": (
                "source_orientation_1_top_left_normalized_1000"
            ),
            "audit_bbox_coordinate_system": (
                "source_orientation_1_top_left_normalized_1000"
            ),
            "comparison_coordinate_system": (
                "source_orientation_1_top_left_normalized_1000"
            ),
        })
        for supporter in forged_observation["supporters"]:
            supporter["bbox_coordinate_system"] = (
                "source_orientation_1_top_left_normalized_1000"
            )
        forged_packets: list[dict[str, object]] = []
        forged_deriver = search_units.DocumentDeriver(
            DOCUMENT_ID, RUN_AT, forged_packets.append, 500
        )
        forged_deriver.consume(forged_parent)
        forged_deriver.consume(forged_child)
        forged_deriver.finish()
        forged_packet = forged_packets[0]

        self.assertTrue(
            search_validator.image_packet_contract_errors(
                forged_packet, "fixture", forged_evidence, documents
            )
        )
        with self.assertRaisesRegex(ValueError, "document_kind_mismatch"):
            adaptive_validator.layer_image_packet_quality(
                forged_packet, forged_evidence, documents
            )

        connection = sqlite3.connect(":memory:")
        streaming_search_validator.initialize(connection)
        connection.execute(
            "INSERT INTO documents VALUES (?, ?)",
            (
                DOCUMENT_ID,
                streaming_search_validator.canonical_json(
                    documents[DOCUMENT_ID]
                ),
            ),
        )
        for record in forged_evidence.values():
            native = record.get("native_properties", {})
            connection.execute(
                "INSERT INTO evidence VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    record["evidence_id"], record["document_id"],
                    record.get("evidence_type"), native.get("quality_tier"),
                    native.get("agreement_type"),
                    native.get("provisional_marker"),
                    native.get("bbox_coordinate_system"),
                    record.get("parent_evidence_id"),
                    streaming_search_validator.canonical_json(record),
                ),
            )
        self.assertTrue(
            streaming_search_validator.image_packet_reconstruction_errors(
                forged_packet, "fixture", connection
            )
        )
        connection.close()

    def test_native_chart_search_units_are_independently_reconstructed(self) -> None:
        for unit_type, evidence_type, raw_value in (
            (
                "chart_summary",
                "chart",
                {"title": "月別売上", "series_count": 1},
            ),
            (
                "chart_series",
                "chart_series",
                {"name": "売上", "points": [10, 20]},
            ),
        ):
            with self.subTest(unit_type=unit_type):
                source_id = (
                    "ev_" + ("7" if evidence_type == "chart" else "8") * 32
                )
                source = {
                    "evidence_id": source_id,
                    "document_id": DOCUMENT_ID,
                    "evidence_type": evidence_type,
                    "location": {
                        "sheet_name": "集計",
                        "object_index": 1,
                        "series_index": 1,
                    },
                    "content": {"raw_value": raw_value},
                    "provenance": provenance("verified_ooxml_chart_cache"),
                }
                search_text = search_units.display_value(source)
                unit = search_units.make_unit(
                    DOCUMENT_ID,
                    unit_type,
                    [source_id],
                    source["location"],
                    search_text,
                    RUN_AT,
                    {"container_kind": "chart"},
                )
                adaptive_validator.layer_native_chart_search_unit(
                    unit, {source_id: source}
                )
                self.assertEqual(
                    search_validator.chart_search_unit_contract_errors(
                        unit, "fixture", {source_id: source}
                    ),
                    [],
                )
                document = {
                    "document_id": DOCUMENT_ID,
                    "source": {
                        "relative_path": "chart.xlsx",
                        "sha256": "c" * 64,
                    },
                }
                projected = adaptive_validator.expected_semantic_evidence(
                    [document], [source], [unit]
                )
                self.assertEqual(len(projected), 2)
                chart_projection = next(
                    item
                    for item in projected
                    if item["adapter"]["source_record_type"] == "search_unit"
                )
                self.assertEqual(
                    chart_projection["adapter"]["unit_type"], unit_type
                )
                relations, coverage = (
                    adaptive_validator.derive_verified_lineage_relations(
                        [unit], projected, [source]
                    )
                )
                self.assertEqual(len(relations), 1)
                self.assertEqual(coverage["projected_search_unit_count"], 1)

                tampers: list[tuple[str, dict[str, object]]] = []
                wrong_text = copy.deepcopy(unit)
                wrong_text["text"]["search_text"] = "改ざん"
                tampers.append(("text", wrong_text))
                wrong_locator = copy.deepcopy(unit)
                wrong_locator["locator"]["object_index"] = 99
                tampers.append(("locator", wrong_locator))
                wrong_context = copy.deepcopy(unit)
                wrong_context["context"] = {"container_kind": "table"}
                tampers.append(("context", wrong_context))
                wrong_source = copy.deepcopy(unit)
                wrong_source["source_evidence_ids"] = ["ev_" + "9" * 32]
                tampers.append(("source", wrong_source))
                for label, tampered in tampers:
                    with self.subTest(unit_type=unit_type, tamper=label):
                        self.assertTrue(
                            search_validator.chart_search_unit_contract_errors(
                                tampered, "fixture", {source_id: source}
                            )
                        )
                        with self.assertRaises(ValueError):
                            adaptive_validator.layer_native_chart_search_unit(
                                tampered, {source_id: source}
                            )
                unverified_source = copy.deepcopy(source)
                unverified_source["provenance"]["extraction_method"] = (
                    "python-pptx-native-chart"
                )
                with self.subTest(unit_type=unit_type, tamper="method"):
                    self.assertTrue(
                        search_validator.chart_search_unit_contract_errors(
                            unit, "fixture", {source_id: unverified_source}
                        )
                    )
                    with self.assertRaisesRegex(ValueError, "source_method"):
                        adaptive_validator.layer_native_chart_search_unit(
                            unit, {source_id: unverified_source}
                        )

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

    def test_allowlisted_provisional_vlm_text_quality_is_strictly_projected(self) -> None:
        records = [
            provisional_vlm_text("ev_" + "7" * 32),
            provisional_vlm_text(
                "ev_" + "8" * 32,
                method="local_vlm_visual_observation_provisional",
                evidence_type="visual_observation",
            ),
        ]
        document = {
            "document_id": DOCUMENT_ID,
            "source": {"relative_path": "sample.png", "sha256": "b" * 64},
        }
        for record in records:
            with self.subTest(record["provenance"]["extraction_method"]):
                self.assertEqual(
                    adapter.provisional_text_evidence_quality(record),
                    ("provisional", PROVISIONAL_MARKER),
                )
                self.assertEqual(
                    adaptive_validator.layer_provisional_text_quality(record),
                    ("provisional", PROVISIONAL_MARKER),
                )
                projected = adaptive_validator.expected_semantic_evidence(
                    [document], [image_evidence(), record], []
                )
                self.assertEqual(len(projected), 1)
                self.assertEqual(projected[0]["quality_tier"], "provisional")
                self.assertEqual(
                    projected[0]["provisional_marker"], PROVISIONAL_MARKER
                )
                self.assertNotIn("agreement_types", projected[0])
                self.assertTrue(
                    all(
                        line.startswith(PROVISIONAL_MARKER + " ")
                        for line in projected[0]["observed_text"].splitlines()
                    )
                )
                adaptive_validator.validate_provisional_text_projection(
                    projected[0]
                )
                unmarked_projection = copy.deepcopy(projected[0])
                unmarked_projection["observed_text"] = "画像からの暫定読取"
                with self.assertRaisesRegex(
                    ValueError, "provisional_vlm_projection_text_unmarked"
                ):
                    adaptive_validator.validate_provisional_text_projection(
                        unmarked_projection
                    )

        malformed = copy.deepcopy(records[0])
        malformed["native_properties"]["quality_tier"] = "high"
        with self.assertRaisesRegex(ValueError, "quality_tier='provisional'"):
            adapter.provisional_text_evidence_quality(malformed)
        with self.assertRaisesRegex(ValueError, "layer_provisional_vlm_quality_invalid"):
            adaptive_validator.layer_provisional_text_quality(malformed)

        located = copy.deepcopy(records[0])
        located["geometry"] = {}
        with self.assertRaisesRegex(ValueError, "must not carry geometry"):
            adapter.provisional_text_evidence_quality(located)
        with self.assertRaisesRegex(
            ValueError, "layer_unlocated_vlm_transcript_has_geometry"
        ):
            adaptive_validator.layer_provisional_text_quality(located)

        unknown_method = copy.deepcopy(records[0])
        unknown_method["provenance"]["extraction_method"] = (
            "local_vlm_unknown"
        )
        with self.assertRaisesRegex(ValueError, "unsupported VLM"):
            adapter.provisional_text_evidence_quality(unknown_method)
        with self.assertRaisesRegex(ValueError, "layer_provisional_vlm_method_invalid"):
            adaptive_validator.layer_provisional_text_quality(unknown_method)

    def test_provisional_visual_text_chunk_is_independently_reconstructed(self) -> None:
        parent = image_evidence()
        parent["location"] = {"page_number": 2, "object_index": 1}
        parent_origin = visual_origin("pdf_page_image")
        parent_origin["source_location"] = {
            "page_number": 2,
            "object_index": 1,
        }
        parent["native_properties"]["visual_origin"] = parent_origin

        source = provisional_vlm_text(
            "ev_" + "9" * 32,
            method="local_vlm_visual_observation_provisional",
            evidence_type="text_block",
        )
        source["parent_evidence_id"] = parent["evidence_id"]
        source["location"] = {
            "page_number": 2,
            "object_index": 2,
            "locator_text": "visual_observation=whole_image",
        }
        source["content"] = text_content(
            f"{PROVISIONAL_MARKER}\n  図表の要約  \n数値 120"
        )
        source["native_properties"]["visual_origin"] = copy.deepcopy(
            parent_origin
        )

        emitted: list[dict[str, object]] = []
        deriver = search_units.DocumentDeriver(
            DOCUMENT_ID, RUN_AT, emitted.append, 500
        )
        deriver.consume(source)
        self.assertEqual(deriver.finish(), {"text_chunk": 1})
        self.assertEqual(len(emitted), 1)
        valid = emitted[0]
        evidence = {
            parent["evidence_id"]: parent,
            source["evidence_id"]: source,
        }
        self.assertEqual(
            valid["text"]["search_text"],
            f"{PROVISIONAL_MARKER} 図表の要約\n"
            f"{PROVISIONAL_MARKER} 数値 120",
        )
        self.assertEqual(
            valid["context"],
            {
                "container_kind": "pdf_page_image",
                "quality_tier": "provisional",
                "provisional_marker": PROVISIONAL_MARKER,
            },
        )
        if self.search_validator is not None:
            self.search_validator.validate(valid)
        self.assertEqual(
            search_validator.image_packet_contract_errors(
                valid, "fixture", evidence
            ),
            [],
        )
        adaptive_validator.layer_provisional_visual_search_unit_quality(
            valid, evidence
        )

        tampered: list[tuple[str, dict[str, object], dict[str, dict[str, object]]]] = []
        missing_context = copy.deepcopy(valid)
        missing_context["context"] = {}
        tampered.append(("missing_context", missing_context, evidence))
        changed_text = copy.deepcopy(valid)
        changed_text["text"]["search_text"] = f"{PROVISIONAL_MARKER} 改ざん"
        tampered.append(("changed_text", changed_text, evidence))
        wrong_locator = copy.deepcopy(valid)
        wrong_locator["locator"]["page_number"] = 99
        tampered.append(("wrong_locator", wrong_locator, evidence))
        wrong_container = copy.deepcopy(valid)
        wrong_container["context"]["container_kind"] = "office_embedded_image"
        tampered.append(("wrong_container", wrong_container, evidence))
        ocr_metadata = copy.deepcopy(valid)
        ocr_metadata["context"]["agreement_types"] = ["same_engine_agreement"]
        tampered.append(("ocr_metadata", ocr_metadata, evidence))

        wrong_method_source = copy.deepcopy(source)
        wrong_method_source["provenance"]["extraction_method"] = "native_parser"
        tampered.append((
            "wrong_method",
            valid,
            {parent["evidence_id"]: parent, source["evidence_id"]: wrong_method_source},
        ))
        wrong_origin_source = copy.deepcopy(source)
        wrong_origin_source["native_properties"]["visual_origin"]["kind"] = (
            "office_embedded_image"
        )
        tampered.append((
            "wrong_origin",
            valid,
            {parent["evidence_id"]: parent, source["evidence_id"]: wrong_origin_source},
        ))

        for name, unit, source_records in tampered:
            with self.subTest(validator="search", tamper=name):
                self.assertTrue(
                    search_validator.image_packet_contract_errors(
                        unit, "fixture", source_records
                    )
                )
            with self.subTest(validator="adaptive", tamper=name):
                with self.assertRaises(ValueError):
                    adaptive_validator.layer_provisional_visual_search_unit_quality(
                        unit, source_records
                    )

        ordinary = copy.deepcopy(source)
        ordinary["provenance"]["extraction_method"] = "native_parser"
        ordinary["native_properties"] = {}
        ordinary_unit = search_units.make_unit(
            DOCUMENT_ID,
            "text_chunk",
            [ordinary["evidence_id"]],
            ordinary["location"],
            "通常本文",
            RUN_AT,
            {
                "container_kind": "standalone_image",
                "quality_tier": "provisional",
                "provisional_marker": PROVISIONAL_MARKER,
            },
        )
        ordinary_evidence = {ordinary["evidence_id"]: ordinary}
        self.assertTrue(
            search_validator.image_packet_contract_errors(
                ordinary_unit, "fixture", ordinary_evidence
            )
        )
        with self.assertRaisesRegex(
            ValueError, "source_method_invalid"
        ):
            adaptive_validator.layer_provisional_visual_search_unit_quality(
                ordinary_unit, ordinary_evidence
            )

    def test_unlocated_vlm_search_unit_is_bound_to_parent_and_document(self) -> None:
        parent = image_evidence()
        source = provisional_vlm_text("ev_" + "6" * 32)
        source["parent_evidence_id"] = parent["evidence_id"]
        source["native_properties"]["visual_origin"] = copy.deepcopy(
            parent["native_properties"]["visual_origin"]
        )
        emitted: list[dict[str, object]] = []
        deriver = search_units.DocumentDeriver(
            DOCUMENT_ID, RUN_AT, emitted.append, 500
        )
        deriver.consume(source)
        self.assertEqual(deriver.finish(), {"text_chunk": 1})
        valid = emitted[0]
        document = {
            "document_id": DOCUMENT_ID,
            "source": {"relative_path": "sample.png", "sha256": "b" * 64},
        }
        documents = {DOCUMENT_ID: document}
        evidence = {
            parent["evidence_id"]: parent,
            source["evidence_id"]: source,
        }

        self.assertEqual(
            search_validator.image_packet_contract_errors(
                valid, "fixture", evidence, documents
            ),
            [],
        )
        adaptive_validator.layer_provisional_visual_search_unit_quality(
            valid, evidence, documents
        )

        tampered: list[tuple[str, dict[str, dict[str, object]]]] = []

        missing_parent = copy.deepcopy(source)
        missing_parent.pop("parent_evidence_id")
        tampered.append((
            "missing_parent",
            {
                parent["evidence_id"]: parent,
                source["evidence_id"]: missing_parent,
            },
        ))

        other_parent = copy.deepcopy(parent)
        other_parent["evidence_id"] = "ev_" + "5" * 32
        other_parent["location"] = {"object_index": 7}
        other_parent["native_properties"]["visual_origin"]["source_location"] = {
            "object_index": 7
        }
        wrong_parent = copy.deepcopy(source)
        wrong_parent["parent_evidence_id"] = other_parent["evidence_id"]
        tampered.append((
            "wrong_parent",
            {
                parent["evidence_id"]: parent,
                other_parent["evidence_id"]: other_parent,
                source["evidence_id"]: wrong_parent,
            },
        ))

        for key, value in (
            ("source_relative_path", "other.png"),
            ("source_sha256", "0" * 64),
            ("source_location", {"object_index": 99}),
        ):
            changed = copy.deepcopy(source)
            changed["native_properties"]["visual_origin"][key] = value
            tampered.append((
                f"child_origin_{key}",
                {
                    parent["evidence_id"]: parent,
                    source["evidence_id"]: changed,
                },
            ))

        missing_origin = copy.deepcopy(source)
        missing_origin["native_properties"].pop("visual_origin")
        tampered.append((
            "missing_child_origin",
            {
                parent["evidence_id"]: parent,
                source["evidence_id"]: missing_origin,
            },
        ))

        missing_materialization = copy.deepcopy(source)
        missing_materialization["native_properties"]["visual_origin"].pop(
            "materialization"
        )
        tampered.append((
            "missing_child_materialization",
            {
                parent["evidence_id"]: parent,
                source["evidence_id"]: missing_materialization,
            },
        ))

        changed_materialization = copy.deepcopy(source)
        changed_materialization["native_properties"]["visual_origin"][
            "materialization"
        ]["rendered_sha256"] = "0" * 64
        tampered.append((
            "child_materialization_hash",
            {
                parent["evidence_id"]: parent,
                source["evidence_id"]: changed_materialization,
            },
        ))

        self_consistent_parent = copy.deepcopy(parent)
        self_consistent_origin = self_consistent_parent["native_properties"][
            "visual_origin"
        ]
        self_consistent_origin["source_relative_path"] = "other.png"
        self_consistent_origin["source_sha256"] = "c" * 64
        self_consistent_origin["materialization"]["source_sha256"] = "c" * 64
        self_consistent_origin["materialization"]["rendered_sha256"] = "c" * 64
        self_consistent_parent["native_properties"]["source_sha256"] = "c" * 64
        self_consistent_source = copy.deepcopy(source)
        self_consistent_source["native_properties"]["visual_origin"] = copy.deepcopy(
            self_consistent_origin
        )
        tampered.append((
            "self_consistent_wrong_document_origin",
            {
                parent["evidence_id"]: self_consistent_parent,
                source["evidence_id"]: self_consistent_source,
            },
        ))

        for name, source_records in tampered:
            with self.subTest(validator="search", tamper=name):
                self.assertTrue(
                    search_validator.image_packet_contract_errors(
                        valid, "fixture", source_records, documents
                    )
                )
            with self.subTest(validator="adaptive", tamper=name):
                with self.assertRaises(ValueError):
                    adaptive_validator.layer_provisional_visual_search_unit_quality(
                        valid, source_records, documents
                    )

    def test_image_packet_quality_accepts_all_supported_visual_containers(self) -> None:
        for container_kind in sorted(adapter.IMAGE_PACKET_CONTAINER_KINDS):
            parent = image_evidence()
            if container_kind in {
                "office_embedded_image", "notebook_embedded_image",
            }:
                source = office_provisional_ocr(
                    "display_transform_unresolved",
                    HIGH_ID,
                    container_kind=container_kind,
                )
                parent["native_properties"]["embedded_sha256"] = "b" * 64
                tier = "provisional"
                agreement = "display_transform_unresolved"
                frame = "raw_raster_top_left_normalized_1000"
                marker = PROVISIONAL_MARKER
                search_text = f"Image file: sample.png\n{marker} {source['content']['raw_text']}"
            else:
                source = ocr_line(
                    HIGH_ID, "独立合意", "independent_agreement", "high"
                )
                source["native_properties"]["visual_origin"] = visual_origin(
                    container_kind
                )
                tier = "high"
                agreement = "independent_agreement"
                frame = "source_orientation_1_top_left_normalized_1000"
                marker = None
                search_text = "Image file: sample.png\n独立合意"
            parent["native_properties"]["visual_origin"] = copy.deepcopy(
                source["native_properties"]["visual_origin"]
            )
            layer_evidence = {IMAGE_ID: parent, HIGH_ID: source}
            context = {
                "container_kind": container_kind,
                "quality_tier": tier,
                "agreement_types": [agreement],
                "bbox_coordinate_system": frame,
                "reading_order_method": "geometry_row_bands_v1",
                "row_band_count": 1,
            }
            if marker is not None:
                context["provisional_marker"] = marker
            packet = search_units.make_unit(
                DOCUMENT_ID,
                "image_text_packet",
                [IMAGE_ID, HIGH_ID],
                {
                    "object_index": 1,
                    "locator_text": (
                        f"container_kind={container_kind};quality_tier={tier};"
                        f"bbox_coordinate_system={frame}"
                    ),
                },
                search_text,
                RUN_AT,
                context,
            )
            with self.subTest(container_kind):
                self.assertEqual(
                    adapter.image_packet_quality(packet),
                    (tier, [agreement], marker),
                )
                self.assertEqual(
                    adaptive_validator.layer_image_packet_quality(
                        packet, layer_evidence
                    ),
                    (tier, [agreement], marker),
                )
                packet["context"]["container_kind"] = "unsupported_image"
                with self.assertRaisesRegex(ValueError, "container kind is invalid"):
                    adapter.image_packet_quality(packet)
                with self.assertRaisesRegex(ValueError, "image_packet_container_invalid"):
                    adaptive_validator.layer_image_packet_quality(
                        packet, layer_evidence
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

    def test_individual_visual_projection_requires_canonical_image_parent(self) -> None:
        parent = image_evidence()
        document = {
            "document_id": DOCUMENT_ID,
            "source": {"relative_path": "sample.png", "sha256": "b" * 64},
        }
        sources = [
            ocr_line(HIGH_ID, "独立合意", "independent_agreement", "high"),
            provisional_vlm_text("ev_" + "c" * 32),
            provisional_vlm_text(
                "ev_" + "d" * 32,
                method="local_vlm_visual_observation_provisional",
            ),
        ]
        for source in sources:
            with self.subTest(
                method=source["provenance"]["extraction_method"]
            ):
                projected = adaptive_validator.expected_semantic_evidence(
                    [document], [parent, source], []
                )
                self.assertEqual(len(projected), 1)

                with self.assertRaisesRegex(
                    ValueError, "visual_source_parent_image_missing"
                ):
                    adaptive_validator.expected_semantic_evidence(
                        [document], [source], []
                    )

                wrong_parent = copy.deepcopy(source)
                wrong_parent["parent_evidence_id"] = "ev_" + "f" * 32
                with self.assertRaisesRegex(
                    ValueError, "visual_source_parent_image_missing"
                ):
                    adaptive_validator.expected_semantic_evidence(
                        [document], [parent, wrong_parent], []
                    )

                missing_origin = copy.deepcopy(source)
                missing_origin["native_properties"].pop("visual_origin")
                with self.assertRaisesRegex(
                    ValueError, "visual_source_origin_invalid"
                ):
                    adaptive_validator.expected_semantic_evidence(
                        [document], [parent, missing_origin], []
                    )

                self.assertEqual(
                    intermediate_validator.visual_source_binding_contract_errors(
                        source, parent, document, "fixture"
                    ) if source["evidence_type"] == "ocr_line" else [],
                    [],
                )
                if source["evidence_type"] == "ocr_line":
                    self.assertTrue(
                        intermediate_validator.visual_source_binding_contract_errors(
                            source, None, document, "fixture"
                        )
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

        forged_supporter = ocr_line(
            HIGH_ID, "独立合意", "independent_agreement", "high"
        )
        forged_supporter["native_properties"]["observation_provenance"][
            "supporters"
        ][1]["raw_text"] = "偽造された別文字列"
        errors = intermediate_validator.image_ocr_contract_errors(
            forged_supporter, "fixture"
        )
        self.assertTrue(
            any("supporter text does not reproduce" in error for error in errors)
        )
        with self.assertRaisesRegex(ValueError, "layer_ocr_supporter_text_mismatch"):
            adaptive_validator.layer_ocr_quality(forged_supporter)
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

    def test_streaming_vlm_detection_cannot_be_bypassed_by_extra_source(self) -> None:
        parent = image_evidence()
        source = provisional_vlm_text(
            "ev_" + "a" * 32,
            method="local_vlm_visual_observation_provisional",
        )
        source["parent_evidence_id"] = parent["evidence_id"]
        source["native_properties"]["visual_origin"] = copy.deepcopy(
            parent["native_properties"]["visual_origin"]
        )
        emitted: list[dict[str, object]] = []
        deriver = search_units.DocumentDeriver(
            DOCUMENT_ID, RUN_AT, emitted.append, 500
        )
        deriver.consume(source)
        deriver.finish()
        valid = emitted[0]
        extra = copy.deepcopy(source)
        extra["evidence_id"] = "ev_" + "e" * 32
        extra["evidence_type"] = "paragraph"
        extra["native_properties"] = {}
        extra["provenance"] = provenance("native_parser")

        document = {
            "document_id": DOCUMENT_ID,
            "source": {
                "relative_path": "sample.png",
                "sha256": "b" * 64,
            },
        }
        connection = sqlite3.connect(":memory:")
        streaming_search_validator.initialize(connection)
        connection.execute(
            "INSERT INTO documents VALUES (?, ?)",
            (
                DOCUMENT_ID,
                streaming_search_validator.canonical_json(document),
            ),
        )
        for record in (parent, source, extra):
            native = record.get("native_properties", {})
            connection.execute(
                "INSERT INTO evidence VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    record["evidence_id"], record["document_id"],
                    record.get("evidence_type"), native.get("quality_tier"),
                    native.get("agreement_type"), native.get("provisional_marker"),
                    native.get("bbox_coordinate_system"),
                    record.get("parent_evidence_id"),
                    streaming_search_validator.canonical_json(record),
                ),
            )
        self.assertEqual(
            streaming_search_validator.provisional_visual_reconstruction_errors(
                valid, "fixture", connection
            ),
            [],
        )
        disguised = copy.deepcopy(valid)
        disguised["source_evidence_ids"].append(extra["evidence_id"])
        disguised.pop("context")
        self.assertTrue(
            streaming_search_validator.provisional_visual_reconstruction_errors(
                disguised, "fixture", connection
            )
        )
        connection.close()

    def test_streaming_vlm_detector_ignores_valid_provisional_ocr_packet(self) -> None:
        parent = image_evidence()
        line = ocr_line(
            SINGLE_PASS_ID,
            "単独読取",
            "provisional_single_pass",
            "provisional",
        )
        emitted: list[dict[str, object]] = []
        deriver = search_units.DocumentDeriver(
            DOCUMENT_ID, RUN_AT, emitted.append, 500
        )
        deriver.consume(parent)
        deriver.consume(line)
        deriver.finish()
        packet = emitted[0]
        document = {
            "document_id": DOCUMENT_ID,
            "source": {
                "relative_path": "sample.png",
                "sha256": "b" * 64,
            },
        }
        connection = sqlite3.connect(":memory:")
        streaming_search_validator.initialize(connection)
        connection.execute(
            "INSERT INTO documents VALUES (?, ?)",
            (
                DOCUMENT_ID,
                streaming_search_validator.canonical_json(document),
            ),
        )
        for record in (parent, line):
            native = record.get("native_properties", {})
            connection.execute(
                "INSERT INTO evidence VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    record["evidence_id"], record["document_id"],
                    record.get("evidence_type"), native.get("quality_tier"),
                    native.get("agreement_type"), native.get("provisional_marker"),
                    native.get("bbox_coordinate_system"),
                    record.get("parent_evidence_id"),
                    streaming_search_validator.canonical_json(record),
                ),
            )
        self.assertEqual(
            streaming_search_validator.provisional_visual_reconstruction_errors(
                packet, "fixture", connection
            ),
            [],
        )
        self.assertEqual(
            streaming_search_validator.image_packet_reconstruction_errors(
                packet, "fixture", connection
            ),
            [],
        )
        connection.close()

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
            {
                "object_index": 1,
                "locator_text": (
                    "container_kind=standalone_image;quality_tier=high;"
                    "bbox_coordinate_system="
                    "source_orientation_1_top_left_normalized_1000"
                ),
            },
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
