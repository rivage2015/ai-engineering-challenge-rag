#!/usr/bin/env python3
"""Validate large intermediate JSONL outputs with a disk-backed ID registry."""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import tempfile
import unicodedata
from pathlib import Path
from typing import Any, Iterator

from lexical_search_common import canonical_json, digest_file
from intermediate_build_integrity import validate_managed_build_integrity
from probe_intermediate_records import digest_value, normalize_text, stable_id
from validate_intermediate_records import (
    ALLOWED,
    published_schema_validators,
    question_boundary_errors,
    schema_record_errors,
    strict_json_loads,
)
from validate_search_units import (
    IMAGE_CONTAINER_KINDS,
    _visual_origin_errors,
    display_transform_unresolved_contract_errors,
)


PATTERNS = {
    "document": re.compile(r"^doc_[0-9a-f]{16,64}$"),
    "evidence": re.compile(r"^ev_[0-9a-f]{16,64}$"),
    "relation": re.compile(r"^rel_[0-9a-f]{16,64}$"),
}
REQUIRED = {
    "document": {"schema_version", "record_type", "document_id", "source", "extraction"},
    "evidence": {"schema_version", "record_type", "evidence_id", "document_id", "evidence_type", "location", "content", "provenance"},
    "relation": {"schema_version", "record_type", "relation_id", "relation_class", "relation_type", "from_ref", "to_ref", "provenance", "status"},
}
PROVISIONAL_OCR_MARKER = "[暫定読取]"
OCR_QUALITY_BY_AGREEMENT = {
    "independent_agreement": "high",
    "same_engine_agreement": "provisional",
    "provisional_single_pass": "provisional",
    "display_transform_unresolved": "provisional",
}
OCR_BBOX_COORDINATE_SYSTEMS = {
    "raw_raster_top_left_normalized_1000",
    "display_oriented_top_left_normalized_1000",
    "source_orientation_1_top_left_normalized_1000",
}
OCR_ENGINE_BY_PASS = {
    "apple_vision_primary": "apple_vision",
    "apple_vision_literal": "apple_vision",
    "apple_vision_fast_sparse": "apple_vision",
    "paddleocr_primary": "paddleocr",
    "tesseract_psm3": "tesseract",
    "tesseract_psm6": "tesseract",
    "tesseract_psm11": "tesseract",
}


def records(path: Path) -> Iterator[tuple[int, object]]:
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                yield line_number, strict_json_loads(line)
            except (json.JSONDecodeError, ValueError) as exc:
                raise ValueError(f"{path}:{line_number}: invalid JSON: {exc}") from exc


def content_hash_payload(item: dict[str, Any]) -> dict[str, Any]:
    for key in ("raw_text", "raw_value", "content_ref"):
        if key in item:
            return {key: item[key]}
    raise ValueError("content has none of raw_text/raw_value/content_ref")


def _ocr_match_text(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("OCR supporter raw text is missing")
    return unicodedata.normalize("NFC", value).strip()


def _ocr_bbox(value: object) -> list[int]:
    if (
        not isinstance(value, list)
        or len(value) != 4
        or any(isinstance(item, bool) or not isinstance(item, int) for item in value)
        or value[0] < 0
        or value[1] < 0
        or value[2] <= 0
        or value[3] <= 0
        or value[0] + value[2] > 1000
        or value[1] + value[3] > 1000
    ):
        raise ValueError("OCR supporter bbox is invalid")
    return list(value)


def _ocr_overlap(first: list[int], second: list[int]) -> float:
    left = max(first[0], second[0])
    top = max(first[1], second[1])
    right = min(first[0] + first[2], second[0] + second[2])
    bottom = min(first[1] + first[3], second[1] + second[3])
    if right <= left or bottom <= top:
        return 0.0
    intersection = (right - left) * (bottom - top)
    smaller = min(first[2] * first[3], second[2] * second[3])
    return intersection / smaller if smaller else 0.0


def ocr_supporter_contract_errors(
    record: dict[str, Any], label: str
) -> list[str]:
    """Recompute OCR agreement from immutable raw supporter observations."""
    if record.get("evidence_type") != "ocr_line":
        return []
    try:
        native = record.get("native_properties")
        if not isinstance(native, dict):
            raise ValueError("OCR native_properties are missing")
        observation = native.get("observation_provenance")
        if not isinstance(observation, dict):
            raise ValueError("OCR observation provenance is missing")
        agreement_type = native.get("agreement_type")
        primary_pass = observation.get("primary_pass")
        primary_engine = OCR_ENGINE_BY_PASS.get(primary_pass)
        if primary_engine is None:
            raise ValueError("OCR primary pass is unsupported")
        primary_group = primary_engine
        if (
            observation.get("primary_engine") != primary_engine
            or observation.get("primary_independence_group") != primary_group
        ):
            raise ValueError("OCR primary supporter identity is invalid")

        audit_pass = observation.get("audit_pass")
        audit_engine = OCR_ENGINE_BY_PASS.get(audit_pass) if audit_pass is not None else None
        audit_group = audit_engine
        if audit_pass is not None and (
            audit_engine is None
            or observation.get("audit_engine") != audit_engine
            or observation.get("audit_independence_group") != audit_group
        ):
            raise ValueError("OCR audit supporter identity is invalid")
        if agreement_type in {
            "independent_agreement", "display_transform_unresolved",
        } and (
            audit_group is None or primary_group == audit_group
        ):
            raise ValueError("high OCR does not have independent supporter groups")
        if agreement_type == "same_engine_agreement" and (
            audit_group is None or primary_group != audit_group
        ):
            raise ValueError("same-engine OCR does not have one supporter group")
        if agreement_type == "provisional_single_pass" and audit_group is not None:
            raise ValueError("single-pass OCR unexpectedly has an audit supporter")

        coordinate_system = native.get("bbox_coordinate_system")
        expected = [(
            primary_pass,
            primary_engine,
            primary_group,
            observation.get("primary_line_id"),
            observation.get("primary_bbox_coordinate_system"),
            native.get("primary_confidence"),
        )]
        if audit_pass is not None:
            expected.append((
                audit_pass,
                audit_engine,
                audit_group,
                observation.get("audit_line_id"),
                observation.get("audit_bbox_coordinate_system"),
                native.get("audit_confidence"),
            ))
        if any(contract[4] != coordinate_system for contract in expected):
            raise ValueError("OCR supporter coordinate frame differs from consensus")
        if (
            len(expected) == 2
            and observation.get("comparison_coordinate_system") != coordinate_system
        ):
            raise ValueError("OCR comparison coordinate frame differs from consensus")

        supporters = observation.get("supporters")
        if not isinstance(supporters, list) or len(supporters) != len(expected):
            raise ValueError("OCR raw supporters are missing or incomplete")
        content = record.get("content", {})
        line_text = _ocr_match_text(content.get("raw_text"))
        boxes: list[list[int]] = []
        for supporter, contract in zip(supporters, expected):
            if not isinstance(supporter, dict):
                raise ValueError("OCR supporter must be an object")
            pass_name, engine, group, line_id, frame, confidence = contract
            if (
                supporter.get("pass") != pass_name
                or supporter.get("engine") != engine
                or supporter.get("independence_group") != group
                or supporter.get("line_id") != line_id
                or supporter.get("bbox_coordinate_system") != frame
            ):
                raise ValueError("OCR supporter identity disagrees with provenance")
            if _ocr_match_text(supporter.get("raw_text")) != line_text:
                raise ValueError("OCR supporter text does not reproduce the consensus")
            actual_confidence = supporter.get("confidence")
            if (
                isinstance(actual_confidence, bool)
                or not isinstance(actual_confidence, (int, float))
                or isinstance(confidence, bool)
                or not isinstance(confidence, (int, float))
                or float(actual_confidence) != float(confidence)
            ):
                raise ValueError("OCR supporter confidence disagrees with provenance")
            boxes.append(_ocr_bbox(supporter.get("bbox")))

        geometry = record.get("geometry")
        if not isinstance(geometry, dict):
            raise ValueError("OCR consensus geometry is missing")
        result_bbox = _ocr_bbox([
            geometry.get("x"), geometry.get("y"),
            geometry.get("width"), geometry.get("height"),
        ])
        union = [
            min(box[0] for box in boxes),
            min(box[1] for box in boxes),
            max(box[0] + box[2] for box in boxes),
            max(box[1] + box[3] for box in boxes),
        ]
        union[2] -= union[0]
        union[3] -= union[1]
        if result_bbox != union:
            raise ValueError("OCR consensus bbox does not reproduce the supporter union")
        claimed_overlap = native.get("spatial_overlap")
        if len(boxes) == 1:
            if claimed_overlap != 0 or agreement_type != "provisional_single_pass":
                raise ValueError("single-pass OCR supporter contract is invalid")
        else:
            recomputed_overlap = _ocr_overlap(boxes[0], boxes[1])
            if (
                isinstance(claimed_overlap, bool)
                or not isinstance(claimed_overlap, (int, float))
                or abs(float(claimed_overlap) - round(recomputed_overlap, 6)) > 0.000001
                or recomputed_overlap < 0.5
            ):
                raise ValueError("OCR supporter overlap does not reproduce the consensus")
    except ValueError as exc:
        return [f"{label}: {exc}"]
    return []


def image_ocr_contract_errors(record: dict[str, Any], label: str) -> list[str]:
    """Validate OCR tier invariants even without the optional schema runtime."""
    if record.get("evidence_type") != "ocr_line":
        return []
    errors: list[str] = []
    native = record.get("native_properties")
    if not isinstance(native, dict):
        return [f"{label}: OCR native_properties are missing"]
    agreement_type = native.get("agreement_type")
    expected_tier = OCR_QUALITY_BY_AGREEMENT.get(agreement_type)
    quality_tier = native.get("quality_tier")
    if expected_tier is None:
        errors.append(f"{label}: unsupported OCR agreement type {agreement_type!r}")
        return errors
    if quality_tier != expected_tier:
        errors.append(
            f"{label}: OCR agreement {agreement_type!r} requires tier {expected_tier!r}"
        )
    marker_present = "provisional_marker" in native
    marker = native.get("provisional_marker")
    provenance = record.get("provenance", {})
    method = provenance.get("extraction_method") if isinstance(provenance, dict) else None
    overlap = native.get("spatial_overlap")
    bbox_coordinate_system = native.get("bbox_coordinate_system")
    if bbox_coordinate_system not in OCR_BBOX_COORDINATE_SYSTEMS:
        errors.append(f"{label}: OCR bbox coordinate system is missing or unsupported")
    numeric_overlap = (
        isinstance(overlap, (int, float)) and not isinstance(overlap, bool)
    )
    if expected_tier == "high":
        if marker_present:
            errors.append(f"{label}: high OCR Evidence carries a provisional marker")
        if native.get("independent_engines") is not True:
            errors.append(f"{label}: high OCR Evidence lacks independent engine groups")
        if method != "dual_local_ocr_consensus":
            errors.append(f"{label}: high OCR Evidence has non-independent provenance")
        if not numeric_overlap or overlap < 0.5:
            errors.append(f"{label}: high OCR Evidence lacks spatial agreement")
        if bbox_coordinate_system != "source_orientation_1_top_left_normalized_1000":
            errors.append(
                f"{label}: high OCR Evidence lacks a shared source-orientation-1 frame"
            )
    else:
        if marker != PROVISIONAL_OCR_MARKER:
            errors.append(f"{label}: provisional OCR Evidence lacks the canonical marker")
        if method != "adaptive_local_ocr_provisional":
            errors.append(f"{label}: provisional OCR Evidence has invalid provenance")
        if agreement_type in {
            "same_engine_agreement", "display_transform_unresolved",
        }:
            if not numeric_overlap or overlap < 0.5:
                errors.append(f"{label}: same-engine OCR agreement lacks spatial overlap")
            if (
                agreement_type == "display_transform_unresolved"
                and native.get("independent_engines") is not True
            ):
                errors.append(
                    f"{label}: display-transform-unresolved OCR lacks independent engines"
                )
        elif overlap != 0:
            errors.append(f"{label}: single-pass provisional OCR overlap must be zero")
    errors.extend(display_transform_unresolved_contract_errors(record, label))
    errors.extend(ocr_supporter_contract_errors(record, label))
    return errors


def visual_source_binding_contract_errors(
    child: dict[str, Any],
    parent: dict[str, Any] | None,
    document: dict[str, Any] | None,
    label: str,
) -> list[str]:
    """Verify an OCR line belongs to one canonical, document-bound image."""
    if child.get("evidence_type") != "ocr_line":
        return []
    errors: list[str] = []
    if not isinstance(parent, dict) or parent.get("evidence_type") != "image":
        return [f"{label}: parent image is missing or invalid"]
    if parent.get("document_id") != child.get("document_id"):
        errors.append(f"{label}: parent image belongs to another document")
    if not isinstance(document, dict) or (
        document.get("document_id") != child.get("document_id")
    ):
        errors.append(f"{label}: source Document is missing or invalid")
    native = child.get("native_properties", {})
    origin = native.get("visual_origin") if isinstance(native, dict) else None
    origin_kind = origin.get("kind") if isinstance(origin, dict) else None
    if origin_kind not in IMAGE_CONTAINER_KINDS:
        errors.append(f"{label}: visual origin is missing or invalid")
        return errors
    errors.extend(
        f"{label}: {error}"
        for error in _visual_origin_errors(
            parent,
            [child],
            str(origin_kind),
            document if isinstance(document, dict) else None,
        )
    )
    return errors


def initialize(connection: sqlite3.Connection) -> None:
    connection.executescript("""
        PRAGMA journal_mode=OFF;
        PRAGMA synchronous=OFF;
        PRAGMA temp_store=FILE;
        CREATE TABLE documents(
            id TEXT PRIMARY KEY,
            relative_path TEXT NOT NULL,
            record_json TEXT NOT NULL
        );
        CREATE TABLE evidence(
            id TEXT PRIMARY KEY,
            document_id TEXT NOT NULL,
            evidence_type TEXT,
            parent_id TEXT,
            record_json TEXT NOT NULL
        );
        CREATE INDEX evidence_document_idx ON evidence(document_id);
        CREATE INDEX evidence_parent_idx ON evidence(parent_id);
        CREATE TABLE relations(id TEXT PRIMARY KEY);
        CREATE TABLE refs(relation_id TEXT NOT NULL, side TEXT NOT NULL, kind TEXT NOT NULL, record_id TEXT NOT NULL);
        CREATE INDEX refs_kind_record_idx ON refs(kind, record_id);
        CREATE TABLE supporting(relation_id TEXT NOT NULL, evidence_id TEXT NOT NULL);
        CREATE INDEX supporting_evidence_idx ON supporting(evidence_id);
    """)


def validate(
    directory: Path,
    source_root: Path | None = None,
    *,
    published_schema: bool = True,
) -> dict[str, int]:
    if (directory / "build-state.json").is_file():
        validate_managed_build_integrity(directory)
    schema_validators = published_schema_validators() if published_schema else {
        "document": None, "evidence": None, "relation": None,
    }
    errors: list[str] = []
    counts = {"document": 0, "evidence": 0, "relation": 0}
    with tempfile.TemporaryDirectory(prefix="aiec-intermediate-validation-") as temporary:
        connection = sqlite3.connect(Path(temporary) / "ids.sqlite3")
        initialize(connection)

        for line_number, record in records(directory / "documents.jsonl"):
            counts["document"] += 1
            label = f"document[{line_number}]"
            record_schema_errors = (
                schema_record_errors("document", record, label, schema_validators["document"])
                if published_schema else []
            )
            errors.extend(record_schema_errors)
            if not isinstance(record, dict):
                continue
            missing = REQUIRED["document"] - record.keys()
            if missing:
                errors.append(f"{label}: missing {sorted(missing)}")
            extra = record.keys() - ALLOWED["document"]
            if extra:
                errors.append(f"{label}: unexpected fields {sorted(extra)}")
            errors.extend(question_boundary_errors("document", record, label))
            if record_schema_errors:
                continue
            record_id = record.get("document_id", "")
            if record.get("schema_version") != "0.1" or record.get("record_type") != "document":
                errors.append(f"{label}: schema_version/record_type mismatch")
            if not PATTERNS["document"].fullmatch(record_id):
                errors.append(f"{label}: malformed document_id")
            source = record.get("source", {})
            expected = stable_id("doc", {
                "relative_path": source.get("relative_path"), "source_sha256": source.get("sha256"),
            })
            if record_id != expected:
                errors.append(f"{label}: unstable document id")
            try:
                connection.execute(
                    "INSERT INTO documents VALUES (?, ?, ?)",
                    (
                        record_id,
                        source.get("relative_path", ""),
                        canonical_json(record),
                    ),
                )
            except sqlite3.IntegrityError:
                errors.append(f"{label}: duplicate document id {record_id}")
            if source_root is not None:
                root = source_root.resolve()
                source_path = (root / source.get("relative_path", "")).resolve()
                try:
                    source_path.relative_to(root)
                except ValueError:
                    errors.append(f"{label}: source path escapes root")
                else:
                    if not source_path.is_file():
                        errors.append(f"{label}: source file is missing")
                    elif source_path.stat().st_size != source.get("size_bytes") or digest_file(source_path) != source.get("sha256"):
                        errors.append(f"{label}: source size or hash mismatch")
        connection.commit()

        for line_number, record in records(directory / "evidence.jsonl"):
            counts["evidence"] += 1
            label = f"evidence[{line_number}]"
            record_schema_errors = (
                schema_record_errors("evidence", record, label, schema_validators["evidence"])
                if published_schema else []
            )
            errors.extend(record_schema_errors)
            if not isinstance(record, dict):
                continue
            missing = REQUIRED["evidence"] - record.keys()
            if missing:
                errors.append(f"{label}: missing {sorted(missing)}")
            extra = record.keys() - ALLOWED["evidence"]
            if extra:
                errors.append(f"{label}: unexpected fields {sorted(extra)}")
            errors.extend(question_boundary_errors("evidence", record, label))
            errors.extend(image_ocr_contract_errors(record, label))
            if record_schema_errors:
                continue
            evidence_id = record.get("evidence_id", "")
            document_id = record.get("document_id", "")
            if record.get("schema_version") != "0.1" or record.get("record_type") != "evidence":
                errors.append(f"{label}: schema_version/record_type mismatch")
            if not PATTERNS["evidence"].fullmatch(evidence_id):
                errors.append(f"{label}: malformed evidence_id")
            item_content = record.get("content", {})
            try:
                if digest_value(content_hash_payload(item_content)) != item_content.get("sha256"):
                    errors.append(f"{label}: content hash mismatch")
                expected = stable_id("ev", {
                    "document_id": document_id,
                    "evidence_type": record.get("evidence_type"),
                    "location": record.get("location"),
                    "content_sha256": item_content.get("sha256"),
                })
                if evidence_id != expected:
                    errors.append(f"{label}: unstable evidence id")
                if "raw_text" in item_content and item_content.get("normalized_text") != normalize_text(item_content["raw_text"]):
                    errors.append(f"{label}: normalized_text mismatch")
                if "raw_value" in item_content and item_content.get("normalized_value") != item_content["raw_value"]:
                    errors.append(f"{label}: normalized_value mismatch")
            except ValueError as exc:
                errors.append(f"{label}: {exc}")
            try:
                connection.execute(
                    "INSERT INTO evidence VALUES (?, ?, ?, ?, ?)",
                    (
                        evidence_id,
                        document_id,
                        record.get("evidence_type"),
                        record.get("parent_evidence_id"),
                        canonical_json(record),
                    ),
                )
            except sqlite3.IntegrityError:
                errors.append(f"{label}: duplicate evidence id {evidence_id}")
            if counts["evidence"] % 100000 == 0:
                connection.commit()
        connection.commit()

        for document_id, count in connection.execute(
            "SELECT e.document_id, COUNT(*) FROM evidence e LEFT JOIN documents d ON d.id=e.document_id "
            "WHERE d.id IS NULL GROUP BY e.document_id LIMIT 100"
        ):
            errors.append(f"{count} Evidence record(s) have dangling document_id {document_id}")
        for evidence_id, parent_id in connection.execute(
            "SELECT child.id, child.parent_id FROM evidence child LEFT JOIN evidence parent ON parent.id=child.parent_id "
            "WHERE child.parent_id IS NOT NULL AND parent.id IS NULL LIMIT 100"
        ):
            errors.append(f"{evidence_id}: dangling parent {parent_id}")
        for evidence_id, parent_id in connection.execute(
            "SELECT child.id, child.parent_id FROM evidence child JOIN evidence parent ON parent.id=child.parent_id "
            "WHERE child.document_id != parent.document_id LIMIT 100"
        ):
            errors.append(f"{evidence_id}: parent {parent_id} belongs to another document")
        for evidence_id, child_json, parent_json, document_json in connection.execute(
            "SELECT child.id, child.record_json, parent.record_json, document.record_json "
            "FROM evidence child "
            "LEFT JOIN evidence parent ON parent.id=child.parent_id "
            "LEFT JOIN documents document ON document.id=child.document_id "
            "WHERE child.evidence_type='ocr_line'"
        ):
            label = f"{evidence_id}: OCR visual source binding"
            child = json.loads(child_json)
            parent = json.loads(parent_json) if parent_json is not None else None
            document = json.loads(document_json) if document_json is not None else None
            errors.extend(
                visual_source_binding_contract_errors(
                    child, parent, document, label
                )
            )

        for line_number, record in records(directory / "relations.jsonl"):
            counts["relation"] += 1
            label = f"relation[{line_number}]"
            record_schema_errors = (
                schema_record_errors("relation", record, label, schema_validators["relation"])
                if published_schema else []
            )
            errors.extend(record_schema_errors)
            if not isinstance(record, dict):
                continue
            missing = REQUIRED["relation"] - record.keys()
            if missing:
                errors.append(f"{label}: missing {sorted(missing)}")
            extra = record.keys() - ALLOWED["relation"]
            if extra:
                errors.append(f"{label}: unexpected fields {sorted(extra)}")
            errors.extend(question_boundary_errors("relation", record, label))
            if record_schema_errors:
                continue
            relation_id = record.get("relation_id", "")
            if record.get("schema_version") != "0.1" or record.get("record_type") != "relation":
                errors.append(f"{label}: schema_version/record_type mismatch")
            if not PATTERNS["relation"].fullmatch(relation_id):
                errors.append(f"{label}: malformed relation_id")
            expected = stable_id("rel", {
                "class": record.get("relation_class"),
                "type": record.get("relation_type"),
                "from": record.get("from_ref"),
                "to": record.get("to_ref"),
                "generator": record.get("provenance", {}).get("generated_by"),
                "generator_version": record.get("provenance", {}).get("generator_version"),
            })
            if relation_id != expected:
                errors.append(f"{label}: unstable relation id")
            try:
                connection.execute("INSERT INTO relations VALUES (?)", (relation_id,))
            except sqlite3.IntegrityError:
                errors.append(f"{label}: duplicate relation id {relation_id}")
            for side in ("from_ref", "to_ref"):
                ref = record.get(side, {})
                kind = ref.get("record_type", "")
                record_id = ref.get("record_id", "")
                if kind not in {"document", "evidence"}:
                    errors.append(f"{label}: invalid {side} type {kind!r}")
                else:
                    connection.execute("INSERT INTO refs VALUES (?, ?, ?, ?)", (relation_id, side, kind, record_id))
            connection.executemany(
                "INSERT INTO supporting VALUES (?, ?)",
                ((relation_id, evidence_id) for evidence_id in record.get("supporting_evidence_ids", [])),
            )
            if counts["relation"] % 100000 == 0:
                connection.commit()
        connection.commit()

        for relation_id, side, kind, record_id in connection.execute(
            "SELECT r.relation_id,r.side,r.kind,r.record_id FROM refs r "
            "LEFT JOIN documents d ON r.kind='document' AND d.id=r.record_id "
            "LEFT JOIN evidence e ON r.kind='evidence' AND e.id=r.record_id "
            "WHERE (r.kind='document' AND d.id IS NULL) OR (r.kind='evidence' AND e.id IS NULL) LIMIT 100"
        ):
            errors.append(f"{relation_id}: dangling {side} {kind} {record_id}")
        for relation_id, evidence_id in connection.execute(
            "SELECT s.relation_id,s.evidence_id FROM supporting s LEFT JOIN evidence e ON e.id=s.evidence_id "
            "WHERE e.id IS NULL LIMIT 100"
        ):
            errors.append(f"{relation_id}: dangling supporting Evidence {evidence_id}")
        connection.close()

    if errors:
        preview = errors[:100]
        suffix = f"\n- ... {len(errors) - 100} more" if len(errors) > 100 else ""
        raise ValueError("validation failed:\n- " + "\n- ".join(preview) + suffix)
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--root", type=Path)
    parser.add_argument(
        "--allow-structural-schema-fallback", action="store_true",
        help="run stable-ID/hash/lineage checks without jsonschema; report this limitation explicitly",
    )
    args = parser.parse_args()
    published_schema = True
    if args.allow_structural_schema_fallback:
        try:
            import jsonschema  # noqa: F401
        except ImportError:
            published_schema = False
    print(canonical_json({
        "status": "ok",
        "schema_validation": "draft202012" if published_schema else "structural_contract_only",
        "counts": validate(
            args.directory.resolve(), args.root.resolve() if args.root else None,
            published_schema=published_schema,
        ),
    }))


if __name__ == "__main__":
    main()
