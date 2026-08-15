#!/usr/bin/env python3
"""Validate visual-classification JSONL records and routing coherence."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import io
import json
import math
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import classify_visual_assets as classifier  # noqa: E402

SCHEMA_PATH = ROOT / "schemas" / "visual-classification.schema.json"

CONTENT_TYPES = {
    "text_document", "table", "chart", "diagram", "screenshot", "formula",
    "photo", "illustration", "decoration", "unknown",
}
INFORMATION_ROLES = {"primary", "supporting", "decorative", "unknown"}
ROUTES = {
    "ocr_text", "table_structure", "chart_source_recovery", "diagram_relations",
    "formula_ocr", "image_description", "skip", "review",
}
STATUSES = {"classified", "needs_review", "failed"}
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
MODEL_DIGEST_RE = re.compile(r"^[0-9a-f]{16,128}$")
CLASSIFICATION_ID_RE = re.compile(r"^vc_[0-9a-f]{16,64}$")
MAX_IMAGE_PIXELS = 50_000_000

ROOT_KEYS = {
    "schema_version", "record_type", "classification_id", "asset_id", "asset",
    "source", "origin", "primary_type", "content_types", "information_role",
    "regions", "routes", "exactness", "warnings", "model", "prompt", "hashes",
    "status", "provenance",
}


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def classification_payload(record: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "primary_type", "content_types", "information_role", "regions", "routes",
        "exactness", "warnings", "status",
    )
    return {key: record.get(key) for key in keys}


def _is_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def _unique_string_array(value: Any, name: str, allowed: set[str], errors: list[str]) -> list[str]:
    if not isinstance(value, list) or not value:
        errors.append(f"{name} must be a non-empty array")
        return []
    if any(not isinstance(item, str) or item not in allowed for item in value):
        errors.append(f"{name} contains an invalid value")
    strings = [item for item in value if isinstance(item, str)]
    if len(strings) != len(set(strings)):
        errors.append(f"{name} must not contain duplicates")
    return strings


def validate(record: object) -> list[str]:
    errors: list[str] = []
    if not isinstance(record, dict):
        return ["root must be an object"]
    missing = sorted(ROOT_KEYS - set(record))
    extra = sorted(set(record) - ROOT_KEYS)
    if missing:
        errors.append("missing root keys: " + ", ".join(missing))
    if extra:
        errors.append("unknown root keys: " + ", ".join(extra))
    if record.get("schema_version") != "0.1":
        errors.append("schema_version must be 0.1")
    if record.get("record_type") != "visual_classification":
        errors.append("record_type must be visual_classification")
    classification_id = record.get("classification_id")
    if not isinstance(classification_id, str) or not CLASSIFICATION_ID_RE.fullmatch(
        classification_id
    ):
        errors.append("invalid classification_id")
    if not isinstance(record.get("asset_id"), str) or not record["asset_id"]:
        errors.append("asset_id must be a non-empty string")

    asset = record.get("asset")
    if not isinstance(asset, dict):
        errors.append("asset must be an object")
    else:
        if set(asset) != {"materialized_path", "sha256", "mime_type", "dimensions"}:
            errors.append("asset keys are incomplete or unknown")
        if not isinstance(asset.get("materialized_path"), str) or not asset["materialized_path"]:
            errors.append("asset.materialized_path must be non-empty")
        asset_sha256 = asset.get("sha256")
        if not isinstance(asset_sha256, str) or not SHA256_RE.fullmatch(asset_sha256):
            errors.append("asset.sha256 must be lowercase SHA-256")
        if not isinstance(asset.get("mime_type"), str) or not asset["mime_type"].startswith("image/"):
            errors.append("asset.mime_type must be an image MIME type")
        dimensions = asset.get("dimensions")
        if not isinstance(dimensions, dict) or set(dimensions) != {"width_px", "height_px"}:
            errors.append("asset.dimensions must contain only width_px and height_px")
        else:
            for key in ("width_px", "height_px"):
                value = dimensions.get(key)
                if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                    errors.append(f"asset.dimensions.{key} must be a positive integer")
    if not isinstance(record.get("source"), dict):
        errors.append("source must be an object")
    if not isinstance(record.get("origin"), dict):
        errors.append("origin must be an object")

    primary_type = record.get("primary_type")
    if not isinstance(primary_type, str) or primary_type not in CONTENT_TYPES:
        errors.append("primary_type is invalid")
    content_types = _unique_string_array(record.get("content_types"), "content_types", CONTENT_TYPES, errors)
    if isinstance(primary_type, str) and primary_type in CONTENT_TYPES and primary_type not in content_types:
        errors.append("primary_type must be present in content_types")
    if "unknown" in content_types and len(content_types) > 1:
        errors.append("unknown cannot be combined with recognized content types")
    information_role = record.get("information_role")
    if not isinstance(information_role, str) or information_role not in INFORMATION_ROLES:
        errors.append("information_role is invalid")

    regions = record.get("regions")
    if not isinstance(regions, list):
        errors.append("regions must be an array")
        regions = []
    region_ids: set[str] = set()
    for index, region in enumerate(regions):
        prefix = f"regions[{index}]"
        if not isinstance(region, dict):
            errors.append(f"{prefix} must be an object")
            continue
        if set(region) != {"region_id", "bbox", "types"}:
            errors.append(f"{prefix} keys are incomplete or unknown")
        region_id = region.get("region_id")
        if not isinstance(region_id, str) or not region_id:
            errors.append(f"{prefix}.region_id must be non-empty")
        elif region_id in region_ids:
            errors.append(f"duplicate region_id: {region_id}")
        else:
            region_ids.add(region_id)
        bbox = region.get("bbox")
        if not isinstance(bbox, list) or len(bbox) != 4 or any(not _is_number(item) for item in bbox):
            errors.append(f"{prefix}.bbox must be four numbers")
        else:
            x, y, width, height = bbox
            if x < 0 or y < 0 or width <= 0 or height <= 0:
                errors.append(f"{prefix}.bbox has invalid coordinates or size")
            if x > 1 or y > 1 or width > 1 or height > 1 or x + width > 1.000001 or y + height > 1.000001:
                errors.append(f"{prefix}.bbox must fit normalized image bounds")
        region_types = _unique_string_array(region.get("types"), f"{prefix}.types", CONTENT_TYPES, errors)
        if any(item not in content_types for item in region_types):
            errors.append(f"{prefix}.types must be a subset of content_types")
        if "unknown" in region_types and len(region_types) > 1:
            errors.append(f"{prefix}.types cannot mix unknown with recognized types")

    routes = _unique_string_array(record.get("routes"), "routes", ROUTES, errors)
    route_set = set(routes)
    type_set = set(content_types)
    required_routes: dict[str, set[str]] = {
        "table": {"table_structure"},
        "chart": {"chart_source_recovery"},
        "diagram": {"diagram_relations"},
        "formula": {"formula_ocr"},
    }
    for content_type, expected in required_routes.items():
        if content_type in type_set and not expected.issubset(route_set):
            errors.append(f"{content_type} requires route {sorted(expected)[0]}")
    if type_set & {"text_document", "table", "diagram", "screenshot"} and "ocr_text" not in route_set:
        errors.append("text-bearing content requires route ocr_text")
    if type_set & {"photo", "illustration"} and "image_description" not in route_set:
        errors.append("photo or illustration requires route image_description")
    if type_set == {"decoration"}:
        if "skip" not in route_set:
            errors.append("decoration-only content requires route skip")
    elif "skip" in route_set:
        errors.append("skip is only valid for decoration-only content")
    if "unknown" in type_set and "review" not in route_set:
        errors.append("unknown content requires route review")

    route_compatibility = {
        "ocr_text": {"text_document", "table", "diagram", "screenshot"},
        "table_structure": {"table"},
        "chart_source_recovery": {"chart"},
        "diagram_relations": {"diagram"},
        "formula_ocr": {"formula"},
        "image_description": {"photo", "illustration"},
        "skip": {"decoration"},
    }
    for route, compatible in route_compatibility.items():
        if route in route_set and not (type_set & compatible):
            errors.append(f"route {route} is incompatible with content_types")

    exactness = record.get("exactness")
    if not isinstance(exactness, str) or exactness not in {"exact", "estimated", "unresolved"}:
        errors.append("exactness is invalid")
    warnings = record.get("warnings")
    if not isinstance(warnings, list) or any(not isinstance(item, str) or not item for item in warnings):
        errors.append("warnings must be an array of non-empty strings")
    status = record.get("status")
    if not isinstance(status, str) or status not in STATUSES:
        errors.append("status is invalid")
    if isinstance(status, str) and status in {"needs_review", "failed"} and "review" not in route_set:
        errors.append(f"{status} status requires route review")
    recognized = (
        bool(content_types)
        and "unknown" not in content_types
        and primary_type != "unknown"
        and information_role != "unknown"
    )
    warning_free = isinstance(warnings, list) and not warnings
    ready_to_classify = recognized and exactness == "estimated" and warning_free
    if status == "classified" and not ready_to_classify:
        errors.append("classified status requires recognized estimated content with no warnings")
    if ready_to_classify and status != "classified":
        errors.append("recognized estimated content with no warnings must have classified status")
    if isinstance(warnings, list) and warnings and status not in {"needs_review", "failed"}:
        errors.append("records with warnings must have needs_review or failed status")
    if status == "needs_review" and not (
        (isinstance(warnings, list) and bool(warnings))
        or not recognized
        or exactness == "unresolved"
    ):
        errors.append("needs_review status requires a warning or unresolved classification")
    if status != "failed" and content_types:
        expected_exactness = "unresolved" if "unknown" in content_types else "estimated"
        if exactness != expected_exactness:
            errors.append(
                f"{status} status with current content types requires exactness {expected_exactness}"
            )
    if status == "failed":
        if (
            record.get("exactness") != "unresolved"
            or primary_type != "unknown"
            or content_types != ["unknown"]
            or information_role != "unknown"
            or regions
            or routes != ["review"]
        ):
            errors.append("failed records must be unresolved unknown classifications")
        if not warnings or not all(
            isinstance(warning, str) and warning.startswith("classification failed:")
            for warning in warnings
        ):
            errors.append("failed records must contain a warning")

    model = record.get("model")
    model_digest: str | None = None
    if not isinstance(model, dict) or set(model) != {"requested", "resolved", "digest"}:
        errors.append("model keys are incomplete or unknown")
    elif (
        not isinstance(model.get("requested"), str) or not model["requested"]
        or not isinstance(model.get("resolved"), str) or not model["resolved"]
        or not isinstance(model.get("digest"), str)
        or not MODEL_DIGEST_RE.fullmatch(model["digest"])
    ):
        errors.append("model fields are invalid")
    else:
        model_digest = model["digest"]
        requested = model["requested"]
        allowed_resolved = {requested}
        if ":" not in requested:
            allowed_resolved.add(requested + ":latest")
        if model["resolved"] not in allowed_resolved:
            errors.append("model.resolved does not correspond to model.requested")

    prompt = record.get("prompt")
    if not isinstance(prompt, dict) or set(prompt) != {"name", "version", "question_independent"}:
        errors.append("prompt keys are incomplete or unknown")
    else:
        if prompt.get("name") != "visual-asset-classification":
            errors.append("prompt.name is invalid")
        if prompt.get("version") != classifier.PROMPT_VERSION:
            errors.append(f"prompt.version must be {classifier.PROMPT_VERSION}")
        if prompt.get("question_independent") is not True:
            errors.append("prompt.question_independent must be true")

    hashes = record.get("hashes")
    if not isinstance(hashes, dict) or set(hashes) != {"input_sha256", "output_sha256", "signature_sha256"}:
        errors.append("hashes keys are incomplete or unknown")
    else:
        for key in ("input_sha256", "output_sha256", "signature_sha256"):
            value = hashes.get(key)
            if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
                errors.append(f"hashes.{key} must be lowercase SHA-256")
        expected_output = hashlib.sha256(
            canonical_json(classification_payload(record)).encode("utf-8")
        ).hexdigest()
        if hashes.get("output_sha256") != expected_output:
            errors.append("hashes.output_sha256 does not match classification payload")
        if (
            isinstance(asset, dict)
            and isinstance(record.get("asset_id"), str)
            and model_digest is not None
            and isinstance(asset.get("sha256"), str)
            and SHA256_RE.fullmatch(asset["sha256"])
        ):
            expected_signature = classifier.signature_for_asset(
                {"asset_id": record["asset_id"], "sha256": asset["sha256"]},
                model_digest,
            )
            if hashes.get("signature_sha256") != expected_signature:
                errors.append("hashes.signature_sha256 does not match asset/model contract")
            if record.get("classification_id") != "vc_" + expected_signature[:24]:
                errors.append("classification_id does not match signature")

    provenance = record.get("provenance")
    if not isinstance(provenance, dict) or set(provenance) != {
        "classifier", "classifier_version", "method", "generated_at",
        "inference_generated_at", "cache_hit", "question_independent",
    }:
        errors.append("provenance keys are incomplete or unknown")
    else:
        if provenance.get("classifier") != "local-gemma-visual-classifier":
            errors.append("provenance.classifier is invalid")
        if provenance.get("classifier_version") != classifier.CLASSIFIER_VERSION:
            errors.append(
                f"provenance.classifier_version must be {classifier.CLASSIFIER_VERSION}"
            )
        if provenance.get("method") != "model_only_visual_classification":
            errors.append("provenance.method must be model_only_visual_classification")
        if provenance.get("question_independent") is not True:
            errors.append("provenance.question_independent must be true")
        if not isinstance(provenance.get("cache_hit"), bool):
            errors.append("provenance.cache_hit must be boolean")
        parsed_timestamps: dict[str, dt.datetime] = {}
        for key in ("generated_at", "inference_generated_at"):
            timestamp = provenance.get(key)
            if not isinstance(timestamp, str) or not timestamp:
                errors.append(f"provenance.{key} must be a timezone-aware ISO datetime")
                continue
            try:
                parsed_at = dt.datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
                if parsed_at.tzinfo is None:
                    raise ValueError
            except ValueError:
                errors.append(f"provenance.{key} must be a timezone-aware ISO datetime")
            else:
                parsed_timestamps[key] = parsed_at
        if (
            "generated_at" in parsed_timestamps
            and "inference_generated_at" in parsed_timestamps
            and parsed_timestamps["inference_generated_at"]
            > parsed_timestamps["generated_at"]
        ):
            errors.append("provenance.inference_generated_at must not follow generated_at")
        if provenance.get("method") == "model_only_visual_classification" and record.get("exactness") == "exact":
            errors.append("model-only classification must never have exact exactness")
    return errors


def _load_published_schema() -> dict[str, Any]:
    try:
        value = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot load published schema {SCHEMA_PATH}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"published schema must be an object: {SCHEMA_PATH}")
    if value.get("$schema") != "https://json-schema.org/draft/2020-12/schema":
        raise ValueError("published schema must use JSON Schema Draft 2020-12")
    if value.get("additionalProperties") is not False:
        raise ValueError("published schema must reject unknown root properties")
    if set(value.get("required", [])) != ROOT_KEYS:
        raise ValueError("published schema required fields do not match validator contract")
    if set(value.get("properties", {})) != ROOT_KEYS:
        raise ValueError("published schema properties do not match validator contract")
    return value


def _compile_published_schema(schema: dict[str, Any]) -> tuple[Any | None, str]:
    try:
        import jsonschema
    except ImportError:
        return None, "strict_manual_fallback"
    try:
        jsonschema.Draft202012Validator.check_schema(schema)
        validator = jsonschema.Draft202012Validator(
            schema, format_checker=jsonschema.FormatChecker()
        )
    except Exception as exc:
        raise ValueError(f"invalid published Draft 2020-12 schema: {exc}") from exc
    return validator, "jsonschema_draft202012_format"


def _schema_errors(record: object, validator: Any | None) -> list[str]:
    if validator is None:
        # validate() above is the dependency-free strict validator.  The CLI
        # explicitly reports this fallback instead of silently skipping schema
        # validation when jsonschema is unavailable.
        return []
    errors = []
    for error in sorted(
        validator.iter_errors(record),
        key=lambda item: tuple(str(component) for component in item.absolute_path),
    ):
        location = ".".join(str(component) for component in error.absolute_path) or "root"
        errors.append(f"schema {location}: {error.message}")
    return errors


def _actual_image_metadata(data: bytes, path: Path) -> tuple[str, int, int]:
    try:
        from PIL import Image
    except ImportError as exc:
        raise ValueError("Pillow is required to verify materialized image metadata") from exc
    try:
        with Image.open(io.BytesIO(data)) as image:
            image_format = str(image.format or "").upper()
            width, height = int(image.width), int(image.height)
            if width < 1 or height < 1:
                raise ValueError("image dimensions must be positive")
            if width * height > MAX_IMAGE_PIXELS:
                raise ValueError(
                    f"image exceeds {MAX_IMAGE_PIXELS} pixel safety limit: {width}x{height}"
                )
            if image_format == "JPEG" and not data.rstrip(b"\x00\t\r\n ").endswith(b"\xff\xd9"):
                raise ValueError("JPEG is missing its terminal EOI marker")
            image.verify()
        # verify() checks container structure but deliberately does not decode
        # pixels.  Re-open and load every pixel so tail truncation and decoder
        # failures cannot pass the assets-bound validator.
        with Image.open(io.BytesIO(data)) as image:
            image.load()
            if int(image.width) != width or int(image.height) != height:
                raise ValueError("image dimensions changed during full decode")
    except Exception as exc:
        raise ValueError(f"cannot verify materialized image metadata: {path}: {exc}") from exc
    mime_type = Image.MIME.get(image_format)
    if not mime_type:
        raise ValueError(f"cannot determine materialized image MIME type: {path}")
    return mime_type, width, height


def _validate_against_asset(
    record: object,
    asset: dict[str, Any],
    position: int,
) -> list[str]:
    errors: list[str] = []
    if not isinstance(record, dict):
        return [f"record {position}: classification must be an object"]
    if record.get("asset_id") != asset["asset_id"]:
        errors.append(
            f"record {position}: asset_id/order mismatch: "
            f"classification={record.get('asset_id')} assets={asset['asset_id']}"
        )
    expected_asset = {
        "materialized_path": asset["declared_path"],
        "sha256": asset["sha256"],
        "mime_type": asset["mime_type"],
        "dimensions": asset["dimensions"],
    }
    if record.get("asset") != expected_asset:
        errors.append(f"record {position}: asset metadata does not match --assets")
    if record.get("source") != asset["source"]:
        errors.append(f"record {position}: source does not match --assets")
    if record.get("origin") != asset["origin"]:
        errors.append(f"record {position}: origin does not match --assets")

    try:
        image_bytes = classifier.verified_asset_bytes(asset)
    except (OSError, ValueError) as exc:
        errors.append(f"record {position}: {exc}")
    else:
        try:
            actual_mime, actual_width, actual_height = _actual_image_metadata(
                image_bytes, asset["path"]
            )
        except ValueError as exc:
            errors.append(f"record {position}: {exc}")
        else:
            if actual_mime != asset["mime_type"]:
                errors.append(
                    f"record {position}: materialized MIME mismatch: "
                    f"declared={asset['mime_type']} actual={actual_mime}"
                )
            actual_dimensions = {"width_px": actual_width, "height_px": actual_height}
            if actual_dimensions != asset["dimensions"]:
                errors.append(
                    f"record {position}: materialized dimensions mismatch: "
                    f"declared={asset['dimensions']} actual={actual_dimensions}"
                )

    hashes = record.get("hashes")
    model = record.get("model")
    if isinstance(hashes, dict):
        if hashes.get("input_sha256") != asset["input_sha256"]:
            errors.append(f"record {position}: hashes.input_sha256 does not match --assets")
    digest = model.get("digest") if isinstance(model, dict) else None
    if isinstance(digest, str) and MODEL_DIGEST_RE.fullmatch(digest):
        expected_signature = classifier.signature_for_asset(asset, digest)
        if not isinstance(hashes, dict) or hashes.get("signature_sha256") != expected_signature:
            errors.append(f"record {position}: hashes.signature_sha256 is invalid")
        expected_id = "vc_" + expected_signature[:24]
        if record.get("classification_id") != expected_id:
            errors.append(f"record {position}: classification_id does not match signature")
    else:
        errors.append(f"record {position}: model digest cannot be verified")
    return errors


def validate_jsonl(
    path: Path,
    assets_path: Path,
    *,
    asset_root: Path | None = None,
) -> dict[str, Any]:
    stats: dict[str, Any] = {
        "records": 0,
        "classified": 0,
        "needs_review": 0,
        "failed": 0,
    }
    seen_asset_ids: set[str] = set()
    all_errors: list[str] = []
    records = classifier.load_jsonl(path)
    raw_assets = classifier.load_jsonl(assets_path)
    if not records:
        raise ValueError("classification JSONL contains no records")
    if not raw_assets:
        raise ValueError("--assets JSONL contains no records")
    root = (asset_root or assets_path.parent).resolve()
    if not root.is_dir():
        raise ValueError(f"asset_root is not a directory: {root}")
    assets = [classifier.normalize_asset(record, root) for record in raw_assets]
    asset_ids = [asset["asset_id"] for asset in assets]
    if len(asset_ids) != len(set(asset_ids)):
        raise ValueError("--assets contains duplicate asset_id values")
    if len(records) != len(assets):
        all_errors.append(
            f"record count mismatch: classifications={len(records)} assets={len(assets)}"
        )

    schema = _load_published_schema()
    schema_validator, schema_validation = _compile_published_schema(schema)
    stats["schema_validation"] = schema_validation
    batch_model: dict[str, Any] | None = None
    for line_number, record in enumerate(records, 1):
        errors = validate(record)
        errors.extend(_schema_errors(record, schema_validator))
        all_errors.extend(f"line {line_number}: {error}" for error in errors)
        if isinstance(record, dict):
            asset_id = record.get("asset_id")
            if isinstance(asset_id, str):
                if asset_id in seen_asset_ids:
                    all_errors.append(f"line {line_number}: duplicate asset_id: {asset_id}")
                seen_asset_ids.add(asset_id)
            status = record.get("status")
            if isinstance(status, str) and status in STATUSES:
                stats[status] += 1
            model = record.get("model")
            if isinstance(model, dict):
                if batch_model is None:
                    batch_model = model
                elif model != batch_model:
                    all_errors.append(
                        f"line {line_number}: model metadata differs within classification batch"
                    )
        stats["records"] += 1
        if line_number <= len(assets):
            all_errors.extend(
                _validate_against_asset(record, assets[line_number - 1], line_number)
            )
    if all_errors:
        raise ValueError("\n".join(all_errors))
    return stats


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate visual-classification JSONL.")
    parser.add_argument("classifications", type=Path)
    parser.add_argument("--assets", type=Path, required=True, help="materialized asset JSONL")
    parser.add_argument(
        "--asset-root",
        type=Path,
        help="root containing materialized paths (default: --assets parent)",
    )
    args = parser.parse_args()
    try:
        stats = validate_jsonl(
            args.classifications, args.assets, asset_root=args.asset_root
        )
    except (OSError, ValueError) as exc:
        for line in str(exc).splitlines():
            print(f"ERROR: {line}")
        return 1
    print(json.dumps(stats, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
