#!/usr/bin/env python3
"""Classify materialized visual assets with a local, resumable Gemma call."""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import hashlib
import json
import math
import mimetypes
import os
import re
import sys
import urllib.error
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from ollama_embedding_common import model_info, request_json  # noqa: E402


CLASSIFIER = "local-gemma-visual-classifier"
CLASSIFIER_VERSION = "0.2"
PROMPT_NAME = "visual-asset-classification"
PROMPT_VERSION = "visual-asset-classification-v0.2"

CONTENT_TYPES = (
    "text_document",
    "table",
    "chart",
    "diagram",
    "screenshot",
    "formula",
    "photo",
    "illustration",
    "decoration",
    "unknown",
)
INFORMATION_ROLES = ("primary", "supporting", "decorative", "unknown")
ROUTES = (
    "ocr_text",
    "table_structure",
    "chart_source_recovery",
    "diagram_relations",
    "formula_ocr",
    "image_description",
    "skip",
    "review",
)
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
MODEL_DIGEST_RE = re.compile(r"^[0-9a-f]{16,128}$")

CLASSIFICATION_PROMPT = """画像に固有の可視内容とレイアウトだけを分類してください。
用途、外部タスク、期待される事実には依存せず、画像だけを観測します。
ファイル名、原本の位置、処理対象の識別子など、画像以外のメタデータは利用しません。
内容を転記したり、数値を推測したり、内容の意味を作らないでください。

画像全体の primary_type を1つ、存在する content_types を1つ以上選びます。
content_types は複数選択可能です。文章と表が同居する場合は両方を選びます。
使用できる種類は次だけです。
text_document, table, chart, diagram, screenshot, formula, photo, illustration, decoration, unknown

次の境界を必ず守ります。
- 読める文字、軸、凡例、目盛線、表の罫線、意味のある箱、矢印が1つでもあるなら、
  decoration だけにはしません。対応する内容ラベルを必ず併記します。
- 軸、凡例、系列、点・線・棒などの符号で比較や変化を表すものは chart です。
  行と列の交点から値を探す構造は table です。軸付きグラフを罫線だけで table にしません。
- 箱、矢印、接続、空間配置で関係を表すものは diagram です。diagram に
  illustration, text_document, table などが同居するなら、積極的に複数ラベルにします。

information_role は primary, supporting, decorative, unknown のいずれかです。
意味のある領域が分かる場合は regions を返します。bbox は左上原点の
[x, y, width, height] で、すべて 0.0 から 1.0 に正規化します。
領域ごとに types を1つ以上付けます。
判断できない場合は補完せず unknown とします。
正確性は後段が決めるため、exactness や confidence は返しません。

次のJSONオブジェクトだけを返してください。
{
  "primary_type": "text_document",
  "content_types": ["text_document"],
  "information_role": "primary",
  "regions": [
    {"region_id": "r1", "bbox": [0.0, 0.0, 1.0, 1.0], "types": ["text_document"]}
  ],
  "warnings": []
}
""".strip()

ALIASES = {
    "text": "text_document",
    "document": "text_document",
    "paragraph": "text_document",
    "graph": "chart",
    "plot": "chart",
    "flowchart": "diagram",
    "screen_capture": "screenshot",
    "equation": "formula",
    "photograph": "photo",
    "drawing": "illustration",
    "ornament": "decoration",
}


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_json(value: Any) -> str:
    return sha256_bytes(canonical_json(value).encode("utf-8"))


def atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def atomic_write_jsonl(path: Path, records: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(canonical_json(record) + "\n")
    os.replace(temporary, path)


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_number}: invalid JSON: {exc}") from exc
            if not isinstance(value, dict):
                raise ValueError(f"{path}:{line_number}: record must be an object")
            records.append(value)
    return records


def _first(record: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value: Any = record
        for component in key.split("."):
            if not isinstance(value, dict) or component not in value:
                value = None
                break
            value = value[component]
        if value is not None:
            return value
    return None


def normalize_asset(record: dict[str, Any], asset_root: Path) -> dict[str, Any]:
    asset_id = _first(record, "asset_id")
    if not isinstance(asset_id, str) or not asset_id.strip():
        raise ValueError("asset_id must be a non-empty string")
    raw_path = _first(
        record,
        "materialized_path",
        "materialization.output_path",
        "materialized.path",
        "asset.materialized_path",
    )
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise ValueError(f"{asset_id}: materialized_path is required")
    resolved_root = asset_root.resolve()
    declared_path = Path(raw_path)
    path = (declared_path if declared_path.is_absolute() else resolved_root / declared_path).resolve()
    try:
        path.relative_to(resolved_root)
    except ValueError as exc:
        raise ValueError(
            f"{asset_id}: materialized_path must resolve inside asset_root: {raw_path}"
        ) from exc

    sha256 = _first(
        record,
        "sha256",
        "sha",
        "materialization.sha256",
        "materialized_sha256",
        "materialized.sha256",
        "asset.sha256",
        "source.sha256",
    )
    mime_type = _first(
        record,
        "mime_type",
        "mime",
        "materialization.mime_type",
        "materialized.mime_type",
        "asset.mime_type",
        "source.mime_type",
    )
    if not isinstance(mime_type, str) or not mime_type:
        mime_type = mimetypes.guess_type(path.name)[0] or ""

    dimensions = _first(
        record, "dimensions", "materialization.dimensions", "materialized.dimensions", "asset.dimensions"
    )
    width = _first(
        record,
        "width_px",
        "width",
        "materialization.width_px",
        "materialized.width_px",
        "asset.width_px",
    )
    height = _first(
        record,
        "height_px",
        "height",
        "materialization.height_px",
        "materialized.height_px",
        "asset.height_px",
    )
    if isinstance(dimensions, dict):
        width = dimensions.get("width_px", dimensions.get("width", width))
        height = dimensions.get("height_px", dimensions.get("height", height))
    elif isinstance(dimensions, list) and len(dimensions) == 2:
        width, height = dimensions

    source = record.get("source")
    origin = record.get("origin")
    if not isinstance(source, dict):
        raise ValueError(f"{asset_id}: source must be an object")
    if not isinstance(origin, dict):
        raise ValueError(f"{asset_id}: origin must be an object")

    if not isinstance(sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", sha256):
        raise ValueError(f"{asset_id}: sha256 must be a lowercase SHA-256")
    if not mime_type.startswith("image/"):
        raise ValueError(f"{asset_id}: materialized asset must have an image MIME type")
    if isinstance(width, bool) or not isinstance(width, int) or width < 1:
        raise ValueError(f"{asset_id}: width_px must be a positive integer")
    if isinstance(height, bool) or not isinstance(height, int) or height < 1:
        raise ValueError(f"{asset_id}: height_px must be a positive integer")

    stable_input = {
        "asset_id": asset_id,
        "source_sha256": _first(record, "source.sha256", "source_sha256"),
        "origin": origin,
        "materialized": {
            "sha256": sha256,
            "mime_type": mime_type,
            "dimensions": {"width_px": width, "height_px": height},
        },
    }

    return {
        "asset_id": asset_id,
        "declared_path": raw_path,
        "path": path,
        "sha256": sha256,
        "mime_type": mime_type,
        "dimensions": {"width_px": width, "height_px": height},
        "source": source,
        "origin": origin,
        "input_sha256": sha256_json(stable_input),
    }


def parse_model_json(response: dict[str, Any]) -> dict[str, Any]:
    message = response.get("message")
    content: Any = message.get("content") if isinstance(message, dict) else None
    if content is None:
        content = response.get("response")
    if isinstance(content, dict):
        return content
    if not isinstance(content, str) or not content.strip():
        raise RuntimeError("visual classifier returned empty content")
    stripped = re.sub(
        r"^\s*```(?:json)?\s*|\s*```\s*$", "", content.strip(), flags=re.IGNORECASE
    ).strip()
    try:
        value = json.loads(stripped)
    except json.JSONDecodeError:
        decoder = json.JSONDecoder()
        value = None
        for index, character in enumerate(stripped):
            if character != "{":
                continue
            try:
                candidate, _ = decoder.raw_decode(stripped[index:])
            except json.JSONDecodeError:
                continue
            if isinstance(candidate, dict):
                value = candidate
                break
        if value is None:
            raise RuntimeError(
                "visual classifier returned invalid JSON: " + stripped[:300]
            )
    if not isinstance(value, dict):
        raise RuntimeError("visual classifier output must be a JSON object")
    nested = value.get("classification")
    return nested if isinstance(nested, dict) else value


def request_classification(
    base_url: str,
    model: str,
    image_bytes: bytes,
    timeout: float,
) -> dict[str, Any]:
    response = request_json(
        base_url,
        "/api/chat",
        {
            "model": model,
            "messages": [
                {
                    "role": "user",
                    "content": CLASSIFICATION_PROMPT,
                    "images": [base64.b64encode(image_bytes).decode("ascii")],
                }
            ],
            "stream": False,
            "format": "json",
            "think": False,
            "keep_alive": "10m",
            "options": {
                "temperature": 0,
                "seed": 42,
                "num_ctx": 32768,
                "num_predict": 4096,
            },
        },
        timeout,
    )
    return parse_model_json(response)


def _content_type(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower().replace("-", "_").replace(" ", "_")
    normalized = ALIASES.get(normalized, normalized)
    return normalized if normalized in CONTENT_TYPES else None


def _normalized_content_type(value: Any, field: str, warnings: list[str]) -> str | None:
    if not isinstance(value, str) or not value.strip():
        warnings.append(f"{field} must be a non-empty content type string")
        return None
    raw = value.strip().lower().replace("-", "_").replace(" ", "_")
    normalized = ALIASES.get(raw, raw)
    if normalized not in CONTENT_TYPES:
        warnings.append(f"{field} contained unknown label: {value}")
        return None
    if normalized != value:
        warnings.append(f"{field} label was normalized from {value} to {normalized}")
    return normalized


def _unique(values: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(values))


def _normalize_bbox(value: Any) -> tuple[list[float] | None, str | None]:
    if isinstance(value, dict):
        value = [value.get("x"), value.get("y"), value.get("width"), value.get("height")]
    if not isinstance(value, list) or len(value) != 4:
        return None, "bbox must contain [x, y, width, height]"
    if any(
        isinstance(item, bool)
        or not isinstance(item, (int, float))
        or not math.isfinite(float(item))
        for item in value
    ):
        return None, "bbox values must be numbers"
    bbox = [float(item) for item in value]
    if max(bbox) > 1.0 and min(bbox) >= 0 and max(bbox) <= 1000:
        bbox = [item / 1000.0 for item in bbox]
    x, y, width, height = bbox
    if x < 0 or y < 0 or width <= 0 or height <= 0:
        return None, "bbox coordinates and size are invalid"
    if x > 1 or y > 1 or width > 1 or height > 1 or x + width > 1.000001 or y + height > 1.000001:
        return None, "bbox must fit inside normalized image bounds"
    rounded = [round(item, 6) for item in bbox]
    return rounded, None


def derive_routes(content_types: list[str], status_needs_review: bool = False) -> list[str]:
    types = set(content_types)
    routes: list[str] = []
    if types & {"text_document", "table", "diagram", "screenshot"}:
        routes.append("ocr_text")
    if "table" in types:
        routes.append("table_structure")
    if "chart" in types:
        routes.append("chart_source_recovery")
    if "diagram" in types:
        routes.append("diagram_relations")
    if "formula" in types:
        routes.append("formula_ocr")
    if types & {"photo", "illustration"}:
        routes.append("image_description")
    substantive = types - {"decoration"}
    if not substantive:
        routes.append("skip")
    if "unknown" in types or (types == {"screenshot"}) or status_needs_review:
        routes.append("review")
    return _unique(routes or ["review"])


def normalize_classification(value: dict[str, Any]) -> dict[str, Any]:
    expected_keys = {"primary_type", "content_types", "information_role", "regions", "warnings"}
    warnings: list[str] = []
    unknown_keys = sorted(set(value) - expected_keys)
    if unknown_keys:
        warnings.append("model output contained unexpected fields: " + ", ".join(unknown_keys))
    raw_warnings = value.get("warnings")
    if not isinstance(raw_warnings, list):
        warnings.append("warnings was not an array")
    else:
        for index, warning in enumerate(raw_warnings, 1):
            if isinstance(warning, str) and warning.strip():
                warnings.append(warning.strip())
            else:
                warnings.append(f"warnings[{index}] was not a non-empty string")

    raw_types = value.get("content_types")
    if isinstance(raw_types, str):
        warnings.append("content_types was a string and was coerced to an array")
        raw_types = [raw_types]
    elif not isinstance(raw_types, list):
        warnings.append("content_types was missing or not an array")
        raw_types = []
    elif not raw_types:
        warnings.append("content_types was empty")
    normalized_types: list[str] = []
    for index, raw in enumerate(raw_types, 1):
        normalized = _normalized_content_type(raw, f"content_types[{index}]", warnings)
        if normalized is not None:
            normalized_types.append(normalized)
    types = _unique(normalized_types)
    if len(types) != len(normalized_types):
        warnings.append("content_types contained duplicate labels")

    primary = _normalized_content_type(value.get("primary_type"), "primary_type", warnings)
    if primary is None and types:
        primary = types[0]
        warnings.append("primary_type was missing or invalid; used the first content type")
    if primary is None:
        primary = "unknown"
        warnings.append("no valid content type was returned")
    if not types:
        types = [primary]
    if primary not in types:
        types.insert(0, primary)
        warnings.append("primary_type was added to content_types")
    if "unknown" in types and len(types) > 1:
        types = [item for item in types if item != "unknown"]
        if primary == "unknown":
            primary = types[0]
        warnings.append("unknown was removed because recognized content types were present")

    information_role = value.get("information_role")
    if not isinstance(information_role, str) or information_role not in INFORMATION_ROLES:
        information_role = "unknown"
        warnings.append("information_role was missing or invalid")
    elif information_role == "unknown":
        warnings.append("information_role is unknown")

    regions: list[dict[str, Any]] = []
    seen_region_ids: set[str] = set()
    raw_regions = value.get("regions")
    if not isinstance(raw_regions, list):
        raw_regions = []
        warnings.append("regions was missing or not an array")
    for index, raw_region in enumerate(raw_regions, 1):
        if not isinstance(raw_region, dict):
            warnings.append(f"region {index} was not an object and was dropped")
            continue
        extra_region_keys = sorted(set(raw_region) - {"region_id", "bbox", "types"})
        if extra_region_keys:
            warnings.append(
                f"region {index} contained unexpected fields: " + ", ".join(extra_region_keys)
            )
        if isinstance(raw_region.get("bbox"), dict):
            warnings.append(f"region {index} bbox object was coerced to an array")
        bbox, bbox_warning = _normalize_bbox(raw_region.get("bbox"))
        if bbox is None:
            warnings.append(f"region {index} was dropped: {bbox_warning}")
            continue
        region_id = raw_region.get("region_id")
        if not isinstance(region_id, str) or not region_id.strip() or region_id in seen_region_ids:
            region_id = f"r{index}"
            while region_id in seen_region_ids:
                region_id += "_"
            warnings.append(f"region {index} received a generated region_id")
        seen_region_ids.add(region_id)
        raw_region_types = raw_region.get("types", raw_region.get("content_types", []))
        if isinstance(raw_region_types, str):
            warnings.append(f"region {region_id} types was a string and was coerced to an array")
            raw_region_types = [raw_region_types]
        elif not isinstance(raw_region_types, list):
            warnings.append(f"region {region_id} types was missing or not an array")
            raw_region_types = []
        normalized_region_types: list[str] = []
        for type_index, raw in enumerate(raw_region_types, 1):
            normalized = _normalized_content_type(
                raw, f"region {region_id} types[{type_index}]", warnings
            )
            if normalized is None:
                continue
            if normalized not in types:
                warnings.append(
                    f"region {region_id} type {normalized} was not present in content_types"
                )
                continue
            normalized_region_types.append(normalized)
        region_types = _unique(normalized_region_types)
        if len(region_types) != len(normalized_region_types):
            warnings.append(f"region {region_id} contained duplicate types")
        if not region_types:
            warnings.append(f"region {region_id} had no type present in the asset and was dropped")
            continue
        regions.append({"region_id": region_id, "bbox": bbox, "types": region_types})
        if bbox_warning:
            warnings.append(f"region {region_id}: {bbox_warning}")

    # Model-only classification can never establish exact facts.  Exactness is
    # therefore a pipeline decision, not a model field: recognized types are
    # estimated and unknown content is unresolved.
    exactness = "unresolved" if "unknown" in types else "estimated"
    warnings = _unique(warnings)
    needs_review = bool(warnings) or exactness == "unresolved" or "unknown" in types
    routes = derive_routes(types, needs_review)
    status = "needs_review" if needs_review else "classified"
    return {
        "primary_type": primary,
        "content_types": types,
        "information_role": information_role,
        "regions": regions,
        "routes": routes,
        "exactness": exactness,
        "warnings": warnings,
        "status": status,
    }


def classification_payload(record: dict[str, Any]) -> dict[str, Any]:
    return {
        key: record[key]
        for key in (
            "primary_type",
            "content_types",
            "information_role",
            "regions",
            "routes",
            "exactness",
            "warnings",
            "status",
        )
    }


def signature_for_asset(asset: dict[str, Any], model_digest: str) -> str:
    asset_id = asset.get("asset_id")
    materialized_sha256 = asset.get("sha256")
    if not isinstance(asset_id, str) or not asset_id:
        raise ValueError("asset_id must be a non-empty string for classification signature")
    if not isinstance(materialized_sha256, str) or not SHA256_RE.fullmatch(
        materialized_sha256
    ):
        raise ValueError(
            "materialized asset SHA-256 must be a lowercase string for classification signature"
        )
    if not isinstance(model_digest, str) or not MODEL_DIGEST_RE.fullmatch(model_digest):
        raise ValueError("model digest must be a lowercase hexadecimal string")
    return sha256_json(
        {
            "asset_id": asset_id,
            "materialized_sha256": materialized_sha256,
            "model_digest": model_digest,
            "classifier_version": CLASSIFIER_VERSION,
            "prompt_version": PROMPT_VERSION,
        }
    )


def validate_model_metadata(value: Any) -> dict[str, str]:
    if not isinstance(value, dict) or set(value) != {"requested", "resolved", "digest"}:
        raise ValueError("model metadata keys are incomplete or unknown")
    requested = value.get("requested")
    resolved = value.get("resolved")
    digest = value.get("digest")
    if not isinstance(requested, str) or not requested:
        raise ValueError("model.requested must be a non-empty string")
    if not isinstance(resolved, str) or not resolved:
        raise ValueError("model.resolved must be a non-empty string")
    if not isinstance(digest, str) or not MODEL_DIGEST_RE.fullmatch(digest):
        raise ValueError("model.digest must be a lowercase hexadecimal string")
    allowed_resolved = {requested}
    if ":" not in requested:
        allowed_resolved.add(requested + ":latest")
    if resolved not in allowed_resolved:
        raise ValueError("model.resolved does not correspond to model.requested")
    return {"requested": requested, "resolved": resolved, "digest": digest}


def _record_envelope(
    asset: dict[str, Any],
    model: dict[str, str],
    signature: str,
    classification: dict[str, Any],
    *,
    cache_hit: bool = False,
    inference_generated_at: str | None = None,
) -> dict[str, Any]:
    model = validate_model_metadata(model)
    generated_at = dt.datetime.now(dt.timezone.utc).isoformat()
    if inference_generated_at is None:
        inference_generated_at = generated_at
    record: dict[str, Any] = {
        "schema_version": "0.1",
        "record_type": "visual_classification",
        "classification_id": "vc_" + signature[:24],
        "asset_id": asset["asset_id"],
        "asset": {
            "materialized_path": asset["declared_path"],
            "sha256": asset["sha256"],
            "mime_type": asset["mime_type"],
            "dimensions": asset["dimensions"],
        },
        "source": asset["source"],
        "origin": asset["origin"],
        **classification,
        "model": model,
        "prompt": {
            "name": PROMPT_NAME,
            "version": PROMPT_VERSION,
            "question_independent": True,
        },
        "hashes": {
            "input_sha256": asset["input_sha256"],
            "output_sha256": "0" * 64,
            "signature_sha256": signature,
        },
        "provenance": {
            "classifier": CLASSIFIER,
            "classifier_version": CLASSIFIER_VERSION,
            "method": "model_only_visual_classification",
            "generated_at": generated_at,
            "inference_generated_at": inference_generated_at,
            "cache_hit": cache_hit,
            "question_independent": True,
        },
    }
    record["hashes"]["output_sha256"] = sha256_json(classification_payload(record))
    return record


def failure_record(
    asset: dict[str, Any], model: dict[str, str], signature: str, error: Exception
) -> dict[str, Any]:
    classification = {
        "primary_type": "unknown",
        "content_types": ["unknown"],
        "information_role": "unknown",
        "regions": [],
        "routes": ["review"],
        "exactness": "unresolved",
        "warnings": [f"classification failed: {type(error).__name__}: {error}"],
        "status": "failed",
    }
    return _record_envelope(asset, model, signature, classification)


def classify_asset(
    asset: dict[str, Any],
    model: dict[str, str],
    base_url: str,
    timeout: float,
    image_bytes: bytes | None = None,
) -> dict[str, Any]:
    model = validate_model_metadata(model)
    signature = signature_for_asset(asset, model["digest"])
    try:
        if image_bytes is None:
            image_bytes = verified_asset_bytes(asset)
        raw = request_classification(base_url, model["requested"], image_bytes, timeout)
        classification = normalize_classification(raw)
        return _record_envelope(asset, model, signature, classification)
    except Exception as exc:
        return failure_record(asset, model, signature, exc)


def verified_asset_bytes(asset: dict[str, Any]) -> bytes:
    if not asset["path"].is_file():
        raise FileNotFoundError(f"materialized asset not found: {asset['path']}")
    image_bytes = asset["path"].read_bytes()
    actual_sha256 = sha256_bytes(image_bytes)
    if actual_sha256 != asset["sha256"]:
        raise ValueError(
            f"asset SHA-256 mismatch: declared={asset['sha256']} actual={actual_sha256}"
        )
    return image_bytes


def _cached_payload(value: Any, signature: str) -> dict[str, Any] | None:
    if not isinstance(value, dict) or value.get("status") == "failed":
        return None
    try:
        from validate_visual_classifications import validate
    except ImportError:
        return None
    if validate(value):
        return None
    hashes = value.get("hashes")
    if not isinstance(hashes, dict) or hashes.get("signature_sha256") != signature:
        return None
    provenance = value.get("provenance")
    if not isinstance(provenance, dict):
        return None
    inference_generated_at = provenance.get("inference_generated_at")
    if not isinstance(inference_generated_at, str) or not inference_generated_at:
        return None
    try:
        classification = classification_payload(value)
    except KeyError:
        return None
    return {
        "classification": classification,
        "inference_generated_at": inference_generated_at,
    }


def _load_cached(path: Path, signature: str) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return _cached_payload(value, signature)


def classify_file(
    input_path: Path,
    output_path: Path,
    *,
    asset_root: Path | None = None,
    cache_dir: Path | None = None,
    base_url: str = "http://127.0.0.1:11434",
    model_name: str = "gemma4:12b",
    timeout: float = 900.0,
    restart: bool = False,
    max_assets: int | None = None,
) -> dict[str, int]:
    if timeout <= 0:
        raise ValueError("timeout must be positive")
    records = load_jsonl(input_path)
    if not records:
        raise ValueError("input JSONL contains no materialized assets")
    if max_assets is not None:
        if max_assets < 1:
            raise ValueError("max_assets must be positive")
        records = records[:max_assets]
    root = (asset_root or input_path.parent).resolve()
    if not root.is_dir():
        raise ValueError(f"asset_root is not a directory: {root}")
    assets = [normalize_asset(record, root) for record in records]
    asset_ids = [asset["asset_id"] for asset in assets]
    if len(asset_ids) != len(set(asset_ids)):
        raise ValueError("input contains duplicate asset_id values")

    resolved_model = validate_model_metadata(
        model_info(base_url, model_name, timeout=min(timeout, 30.0))
    )
    cache_root = cache_dir or output_path.with_name(output_path.name + ".cache")
    cache_root.mkdir(parents=True, exist_ok=True)

    existing_by_asset: dict[str, dict[str, Any]] = {}
    if output_path.is_file() and not restart:
        for record in load_jsonl(output_path):
            asset_id = record.get("asset_id")
            if isinstance(asset_id, str):
                existing_by_asset[asset_id] = record

    ordered_results: list[dict[str, Any]] = []
    stats = {"total": len(assets), "classified": 0, "needs_review": 0, "failed": 0, "cached": 0}
    for position, asset in enumerate(assets, 1):
        signature = signature_for_asset(asset, resolved_model["digest"])
        try:
            image_bytes = verified_asset_bytes(asset)
        except Exception as exc:
            result = failure_record(asset, resolved_model, signature, exc)
            ordered_results.append(result)
            stats["failed"] += 1
            atomic_write_jsonl(output_path, ordered_results)
            print(f"[{position}/{len(assets)}] failed {asset['asset_id']}: {exc}")
            continue

        cached_payload = None
        if not restart:
            existing = existing_by_asset.get(asset["asset_id"])
            if existing is not None:
                cached_payload = _cached_payload(existing, signature)
            if cached_payload is None:
                cached_payload = _load_cached(cache_root / f"{signature}.json", signature)
        if cached_payload is not None:
            result = _record_envelope(
                asset,
                resolved_model,
                signature,
                cached_payload["classification"],
                cache_hit=True,
                inference_generated_at=cached_payload["inference_generated_at"],
            )
            stats["cached"] += 1
            print(f"[{position}/{len(assets)}] cache {asset['asset_id']}")
        else:
            print(f"[{position}/{len(assets)}] classify {asset['asset_id']}", flush=True)
            result = classify_asset(
                asset, resolved_model, base_url, timeout, image_bytes=image_bytes
            )
        atomic_write_json(cache_root / f"{signature}.json", result)
        ordered_results.append(result)
        stats[result["status"]] += 1
        atomic_write_jsonl(output_path, ordered_results)
    return stats


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Classify materialized visual assets with local Gemma, sequentially and resumably."
    )
    parser.add_argument("input", type=Path, help="materialized asset JSONL")
    parser.add_argument("--out", type=Path, required=True, help="classification JSONL")
    parser.add_argument("--asset-root", type=Path, help="base directory for relative materialized paths")
    parser.add_argument("--cache-dir", type=Path)
    parser.add_argument("--model", default="gemma4:12b")
    parser.add_argument("--base-url", default="http://127.0.0.1:11434")
    parser.add_argument("--timeout", type=float, default=900.0)
    parser.add_argument("--restart", action="store_true", help="ignore prior output and cache entries")
    parser.add_argument("--max-assets", type=int)
    args = parser.parse_args()
    try:
        stats = classify_file(
            args.input,
            args.out,
            asset_root=args.asset_root,
            cache_dir=args.cache_dir,
            base_url=args.base_url,
            model_name=args.model,
            timeout=args.timeout,
            restart=args.restart,
            max_assets=args.max_assets,
        )
    except (OSError, ValueError, RuntimeError, urllib.error.URLError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(canonical_json(stats))
    return 1 if stats["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
