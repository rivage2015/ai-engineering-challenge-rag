#!/usr/bin/env python3
"""Run an isolated, offline PP-DocLayoutV3 layout-analysis PoC.

The runner consumes only the verified OCR fixture manifest and its already
materialized page images.  It never opens source documents, questions, gold
labels, predictions, or answers.  It cannot download a model: inference is
allowed only from a caller-supplied local directory whose pinned revision and
model.safetensors SHA-256 are recorded and verified.
"""

from __future__ import annotations

import argparse
import copy
import datetime as dt
import hashlib
import importlib
import importlib.metadata
import json
import math
import os
import platform
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, MutableMapping, Optional, Sequence

from PIL import Image
from jsonschema import Draft202012Validator, FormatChecker


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import ocr_poc_contract as ocr_contract  # noqa: E402


SCHEMA_PATH = ROOT / "schemas" / "pp-doclayout-poc-run.schema.json"
RUNNER_VERSION = "0.1"
MODEL_REPO_ID = "PaddlePaddle/PP-DocLayoutV3_safetensors"
PINNED_MODEL_REVISION = "97d101e6db2642e162a1d05392d1b0231c91033e"
PINNED_WEIGHT_SHA256 = "5ea422c6cc5fe759a47e1357c35639b58173508e025a3131cbe4b6ac59e2b85e"
REQUIRED_MODEL_FILES = (
    "config.json",
    "model.safetensors",
    "preprocessor_config.json",
)
OFFLINE_ENVIRONMENT = {
    "HF_HUB_OFFLINE": "1",
    "TRANSFORMERS_OFFLINE": "1",
    "HF_DATASETS_OFFLINE": "1",
}
PACKAGE_DISTRIBUTIONS = {
    "torch": "torch",
    "torchvision": "torchvision",
    "transformers": "transformers",
    "safetensors": "safetensors",
    "numpy": "numpy",
    "opencv_python": "opencv-python",
    "pillow": "Pillow",
    "jsonschema": "jsonschema",
    "huggingface_hub": "huggingface-hub",
}
MAX_MANIFEST_BYTES = 64 * 1024 * 1024
MAX_MANIFEST_RECORDS = 10000
MAX_DETECTIONS = 1000
FORBIDDEN_DATA_RE = re.compile(
    r"(?:^|[-_.])(questions?|gold|predictions?|answers?)(?:[-_.]|$)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ManifestFixture:
    line_number: int
    record: dict[str, Any]


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_json(value: Any) -> str:
    return sha256_bytes(canonical_json(value).encode("utf-8"))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _safe_relative_path(raw: str, label: str) -> Path:
    if raw.startswith(("/", "\\")):
        raise ValueError(f"{label} must be relative")
    path = Path(raw)
    if path.is_absolute() or not path.parts:
        raise ValueError(f"{label} must be a non-empty relative path")
    if any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError(f"{label} contains an unsafe path component")
    return path


def _has_symlink_component(path: Path, trusted_root: Path) -> bool:
    absolute = path if path.is_absolute() else trusted_root / path
    root = trusted_root.resolve(strict=True)
    try:
        relative = absolute.absolute().relative_to(trusted_root.absolute())
    except ValueError:
        return True
    current = trusted_root
    if current.is_symlink():
        return True
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            return True
    try:
        resolved = absolute.resolve(strict=True)
    except OSError:
        return False
    return not _inside(resolved, root)


def _repo_path(path: Path, repository_root: Path, label: str) -> Path:
    root = repository_root.resolve(strict=True)
    candidate = path if path.is_absolute() else repository_root / path
    if _has_symlink_component(candidate, repository_root):
        raise ValueError(f"{label} must be a regular non-symlink path: {candidate}")
    resolved = candidate.resolve(strict=True)
    if not _inside(resolved, root):
        raise ValueError(f"{label} escapes repository root")
    return resolved


def _relative_to_repo(path: Path, repository_root: Path, label: str) -> str:
    resolved = _repo_path(path, repository_root, label)
    return resolved.relative_to(repository_root.resolve(strict=True)).as_posix()


def _reject_forbidden_path(raw: str, label: str) -> None:
    relative = _safe_relative_path(raw, label)
    if any(FORBIDDEN_DATA_RE.search(part) for part in relative.parts):
        raise ValueError(f"{label} looks like prohibited question/gold/prediction/answer data")


def _assert_manifest_path(path: Path, repository_root: Path) -> Path:
    candidate = path if path.is_absolute() else repository_root / path
    if any(FORBIDDEN_DATA_RE.search(part) for part in candidate.parts):
        raise ValueError("manifest path looks like prohibited question/gold/prediction/answer data")
    if _has_symlink_component(candidate, repository_root) or not candidate.is_file():
        raise ValueError(f"manifest must be a regular non-symlink file: {candidate}")
    resolved = _repo_path(candidate, repository_root, "manifest")
    if resolved.stat().st_size > MAX_MANIFEST_BYTES:
        raise ValueError(f"manifest exceeds {MAX_MANIFEST_BYTES} bytes")
    return resolved


def load_verified_manifest(
    path: Path,
    repository_root: Path = ROOT,
) -> list[ManifestFixture]:
    """Validate the existing OCR fixture manifest without using its text labels."""

    manifest = _assert_manifest_path(path, repository_root)
    output: list[ManifestFixture] = []
    with manifest.open("r", encoding="utf-8") as handle:
        for line_number, raw in enumerate(handle, 1):
            if not raw.strip():
                raise ValueError(f"{manifest}:{line_number}: blank JSONL line")
            try:
                record = json.loads(raw, object_pairs_hook=_reject_duplicate_keys)
            except (json.JSONDecodeError, ValueError) as exc:
                raise ValueError(f"{manifest}:{line_number}: invalid JSON: {exc}") from exc
            if not isinstance(record, dict):
                raise ValueError(f"{manifest}:{line_number}: record must be an object")
            if record.get("record_type") != "ocr_poc_fixture":
                raise ValueError(f"{manifest}:{line_number}: unexpected record_type")
            provenance = record.get("provenance")
            if not isinstance(provenance, dict):
                raise ValueError(f"{manifest}:{line_number}: missing provenance")
            required_flags = {
                "question_independent": True,
                "question_data_used": False,
                "answer_data_used": False,
                "prediction_data_used": False,
            }
            for key, expected in required_flags.items():
                if provenance.get(key) is not expected:
                    raise ValueError(f"{manifest}:{line_number}: unsafe provenance flag {key}")
            asset_ref = record.get("asset_ref")
            if not isinstance(asset_ref, dict):
                raise ValueError(f"{manifest}:{line_number}: missing asset_ref")
            _reject_forbidden_path(str(asset_ref.get("materialized_path", "")), "materialized_path")
            _reject_forbidden_path(str(asset_ref.get("source_relative_path", "")), "source_relative_path")
            errors = ocr_contract.validate_fixture(
                record,
                repository_root=repository_root,
                require_verified=True,
            )
            if errors:
                raise ValueError(
                    f"{manifest}:{line_number}: invalid OCR fixture: " + "; ".join(errors)
                )
            output.append(ManifestFixture(line_number=line_number, record=record))
            if len(output) > MAX_MANIFEST_RECORDS:
                raise ValueError(f"manifest exceeds {MAX_MANIFEST_RECORDS} records")
    if not output:
        raise ValueError("fixture manifest is empty")
    return output


def sample_id_payload(sample: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: copy.deepcopy(value)
        for key, value in sample.items()
        if key != "sample_id"
    }


def expected_sample_id(sample: Mapping[str, Any]) -> str:
    return "ppsrc_" + sha256_json(sample_id_payload(sample))[:24]


def _group_structural_fixtures(
    fixtures: Sequence[ManifestFixture],
) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for loaded in fixtures:
        record = loaded.record
        asset_ref = record["asset_ref"]
        routes = record["strata"]["routes"]
        if "table_structure" not in routes:
            continue
        origin_kind = asset_ref["origin_kind"]
        if origin_kind not in {"pdf_page", "office_embedded_image"}:
            continue
        asset_id = asset_ref["asset_id"]
        if asset_id not in grouped:
            grouped[asset_id] = {
                "asset_ref": copy.deepcopy(asset_ref),
                "fixtures": [],
                "purposes": set(),
                "first_line": loaded.line_number,
            }
            order.append(asset_id)
        group = grouped[asset_id]
        if group["asset_ref"] != asset_ref:
            raise ValueError(f"inconsistent asset metadata for {asset_id}")
        group["fixtures"].append(loaded)
        group["purposes"].add(record["crop"]["purpose"])
    return [grouped[asset_id] for asset_id in order]


def select_poc_samples(
    fixtures: Sequence[ManifestFixture],
    *,
    manifest_path: Path,
    repository_root: Path = ROOT,
) -> list[dict[str, Any]]:
    """Choose two geometry-only strata in manifest order.

    No reference text, question, answer, gold, or previous prediction is part
    of this decision.  Both samples must expose a table header and table cell.
    """

    groups = _group_structural_fixtures(fixtures)
    required_purposes = {"table_header", "table_cell"}
    complex_candidates = [
        group
        for group in groups
        if group["asset_ref"]["origin_kind"] == "pdf_page"
        and required_purposes.issubset(group["purposes"])
    ]
    clean_candidates = [
        group
        for group in groups
        if group["asset_ref"]["origin_kind"] == "office_embedded_image"
        and required_purposes.issubset(group["purposes"])
    ]
    if not complex_candidates:
        raise ValueError("manifest has no PDF-page table header/cell structural sample")
    if not clean_candidates:
        raise ValueError("manifest has no office table header/cell structural sample")

    manifest = _assert_manifest_path(manifest_path, repository_root)
    manifest_relative = manifest.relative_to(repository_root.resolve(strict=True)).as_posix()
    manifest_sha256 = sha256_file(manifest)
    selections = [
        ("complex_pdf_page", complex_candidates[0]),
        ("clean_table_page", clean_candidates[0]),
    ]
    output: list[dict[str, Any]] = []
    for role, group in selections:
        asset_ref = group["asset_ref"]
        fixture_refs = [
            {
                "line_number": loaded.line_number,
                "fixture_id": loaded.record["fixture_id"],
                "signature_sha256": loaded.record["hashes"]["signature_sha256"],
            }
            for loaded in sorted(group["fixtures"], key=lambda value: value.line_number)
        ]
        base: dict[str, Any] = {
            "role": role,
            "asset_id": asset_ref["asset_id"],
            "fixture_refs": fixture_refs,
            "manifest_relative_path": manifest_relative,
            "manifest_sha256": manifest_sha256,
            "materialized_path": asset_ref["materialized_path"],
            "image_sha256": asset_ref["image_sha256"],
            "dimensions": copy.deepcopy(asset_ref["dimensions"]),
            "origin_kind": asset_ref["origin_kind"],
            "source_relative_path": asset_ref["source_relative_path"],
            "source_sha256": asset_ref["source_sha256"],
            "page_number": asset_ref["page_number"],
        }
        base["sample_id"] = expected_sample_id(base)
        output.append(base)
    if output[0]["image_sha256"] == output[1]["image_sha256"]:
        raise ValueError("complex and clean samples must reference distinct images")
    return output


def resolve_sample_image(sample: Mapping[str, Any], repository_root: Path) -> Path:
    _reject_forbidden_path(str(sample["materialized_path"]), "materialized_path")
    relative = _safe_relative_path(str(sample["materialized_path"]), "materialized_path")
    image = _repo_path(repository_root / relative, repository_root, "materialized image")
    if not image.is_file():
        raise ValueError(f"materialized image is not a regular file: {image}")
    if sha256_file(image) != sample["image_sha256"]:
        raise ValueError(f"image hash mismatch: {sample['materialized_path']}")
    with Image.open(image) as value:
        dimensions = {"width_px": int(value.width), "height_px": int(value.height)}
        value.verify()
    if dimensions != sample["dimensions"]:
        raise ValueError(f"image dimensions mismatch: {sample['materialized_path']}")
    return image


def _load_json_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_reject_duplicate_keys)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError(f"invalid {label}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label} must contain a JSON object")
    return value


def fingerprint_local_model(
    model_dir: Path,
    *,
    repository_root: Path = ROOT,
    revision: str,
    expected_weight_sha256: str,
) -> dict[str, Any]:
    """Fingerprint exactly the three local files needed by Transformers."""

    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("model revision must be a full 40-character Git commit")
    if not re.fullmatch(r"[0-9a-f]{64}", expected_weight_sha256):
        raise ValueError("expected weight SHA-256 must be 64 lowercase hex characters")
    model = _repo_path(model_dir, repository_root, "model directory")
    if not model.is_dir() or model.is_symlink():
        raise ValueError(f"model directory must be a regular non-symlink directory: {model}")

    files: list[dict[str, Any]] = []
    for relative in REQUIRED_MODEL_FILES:
        candidate = model / relative
        if candidate.is_symlink() or not candidate.is_file():
            raise ValueError(f"required model file is unavailable or a symlink: {candidate}")
        resolved = candidate.resolve(strict=True)
        if not _inside(resolved, model.resolve(strict=True)):
            raise ValueError(f"required model file escapes model directory: {candidate}")
        files.append(
            {
                "relative_path": relative,
                "size_bytes": candidate.stat().st_size,
                "sha256": sha256_file(candidate),
            }
        )
    files.sort(key=lambda item: item["relative_path"])
    config = _load_json_object(model / "config.json", "config.json")
    if config.get("model_type") != "pp_doclayout_v3":
        raise ValueError("config.json model_type must be pp_doclayout_v3")
    preprocessor = _load_json_object(
        model / "preprocessor_config.json", "preprocessor_config.json"
    )
    if preprocessor.get("image_processor_type") != "PPDocLayoutV3ImageProcessor":
        raise ValueError(
            "preprocessor_config.json image_processor_type must be PPDocLayoutV3ImageProcessor"
        )
    weight = next(item for item in files if item["relative_path"] == "model.safetensors")
    if weight["sha256"] != expected_weight_sha256:
        raise ValueError(
            "model.safetensors weight hash mismatch: "
            f"expected {expected_weight_sha256}, got {weight['sha256']}"
        )
    fingerprint_payload = {
        "repo_id": MODEL_REPO_ID,
        "revision": revision,
        "files": files,
    }
    return {
        "repo_id": MODEL_REPO_ID,
        "revision": revision,
        "local_path": model.relative_to(repository_root.resolve(strict=True)).as_posix(),
        "local_files_only": True,
        "trust_remote_code": False,
        "files": files,
        "file_count": len(files),
        "size_bytes": sum(item["size_bytes"] for item in files),
        "artifact_fingerprint_sha256": sha256_json(fingerprint_payload),
        "weights_relative_path": "model.safetensors",
        "weights_size_bytes": weight["size_bytes"],
        "weights_sha256": weight["sha256"],
    }


def installed_package_versions() -> dict[str, str]:
    output: dict[str, str] = {}
    for key, distribution in PACKAGE_DISTRIBUTIONS.items():
        try:
            output[key] = importlib.metadata.version(distribution)
        except importlib.metadata.PackageNotFoundError as exc:
            raise ValueError(f"required package is not installed: {distribution}") from exc
    return output


def package_fingerprint_payload(
    *,
    packages: Mapping[str, str],
    python_version: str,
    platform_value: str,
    device_requested: str,
    device_effective: str,
    inference_device: str,
    postprocess_device: str,
) -> dict[str, Any]:
    return {
        "packages": dict(packages),
        "python_version": python_version,
        "platform": platform_value,
        "device_requested": device_requested,
        "device_effective": device_effective,
        "inference_device": inference_device,
        "postprocess_device": postprocess_device,
    }


def build_configuration(
    *,
    model: Mapping[str, Any],
    packages: Mapping[str, str],
    device_requested: str,
    device_effective: str,
    threshold: float,
    python_version: Optional[str] = None,
    platform_value: Optional[str] = None,
) -> dict[str, Any]:
    if set(packages) != set(PACKAGE_DISTRIBUTIONS):
        raise ValueError("package version map is incomplete or contains unknown packages")
    if device_requested not in {"mps", "cpu"} or device_effective not in {"mps", "cpu"}:
        raise ValueError("device must be mps or cpu")
    if device_requested != device_effective:
        raise ValueError("implicit device fallback is prohibited")
    inference_device = device_effective
    postprocess_device = "cpu"
    if not math.isfinite(float(threshold)) or not 0 <= float(threshold) <= 1:
        raise ValueError("threshold must be a finite number between 0 and 1")
    python_value = python_version or platform.python_version()
    platform_string = platform_value or platform.platform()
    fingerprint_payload = package_fingerprint_payload(
        packages=packages,
        python_version=python_value,
        platform_value=platform_string,
        device_requested=device_requested,
        device_effective=device_effective,
        inference_device=inference_device,
        postprocess_device=postprocess_device,
    )
    return {
        "runner_version": RUNNER_VERSION,
        "python_version": python_value,
        "platform": platform_string,
        "packages": dict(packages),
        "package_fingerprint_sha256": sha256_json(fingerprint_payload),
        "device_requested": device_requested,
        "device_effective": device_effective,
        "inference_device": inference_device,
        "postprocess_device": postprocess_device,
        "offline": True,
        "no_implicit_download": True,
        "offline_environment": dict(OFFLINE_ENVIRONMENT),
        "model": copy.deepcopy(dict(model)),
        "inference": {
            "task": "object_detection_with_reading_order",
            "input_format": "image",
            "batch_size": 1,
            "threshold": float(threshold),
            "postprocess": "PPDocLayoutV3ImageProcessor.post_process_object_detection",
            "preserve_raw": True,
            "question_conditioning": False,
        },
    }


def require_offline_environment(
    environ: Optional[Mapping[str, str]] = None,
) -> dict[str, str]:
    values = os.environ if environ is None else environ
    missing = [key for key, expected in OFFLINE_ENVIRONMENT.items() if values.get(key) != expected]
    if missing:
        assignments = " ".join(f"{key}=1" for key in missing)
        raise ValueError(f"offline mode requires environment variables: {assignments}")
    return dict(OFFLINE_ENVIRONMENT)


def load_local_components(
    model_dir: Path,
    *,
    device: str,
    environ: Optional[Mapping[str, str]] = None,
    transformers_api: Optional[Any] = None,
    torch_api: Optional[Any] = None,
) -> tuple[Any, Any]:
    """Load only a local directory; this function has no Hub/repo-ID path."""

    require_offline_environment(environ)
    if model_dir.is_symlink() or not model_dir.is_dir():
        raise ValueError(f"local model directory is unavailable: {model_dir}")
    local_model = model_dir.resolve(strict=True)
    if transformers_api is None:
        transformers_api = importlib.import_module("transformers")
    if torch_api is None:
        torch_api = importlib.import_module("torch")
    if device == "mps":
        if not bool(torch_api.backends.mps.is_available()):
            raise ValueError("MPS was requested but is unavailable; implicit CPU fallback is prohibited")
    elif device != "cpu":
        raise ValueError("device must be mps or cpu")
    load_kwargs = {
        "local_files_only": True,
        "trust_remote_code": False,
    }
    processor = transformers_api.AutoImageProcessor.from_pretrained(
        str(local_model), **load_kwargs
    )
    model = transformers_api.AutoModelForObjectDetection.from_pretrained(
        str(local_model), **load_kwargs
    )
    model = model.to(device)
    model.eval()
    return processor, model


def _to_python(value: Any) -> Any:
    if hasattr(value, "detach"):
        value = value.detach()
    if hasattr(value, "cpu"):
        value = value.cpu()
    if hasattr(value, "tolist"):
        value = value.tolist()
    elif hasattr(value, "item") and not isinstance(value, (str, bytes)):
        try:
            value = value.item()
        except (TypeError, ValueError):
            pass
    if isinstance(value, tuple):
        return [_to_python(item) for item in value]
    if isinstance(value, list):
        return [_to_python(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _to_python(item) for key, item in value.items()}
    return value


def _move_postprocess_value_to_cpu(value: Any, torch_api: Any) -> Any:
    """Recursively copy tensors to deterministic CPU post-processing dtypes."""

    if torch_api.is_tensor(value):
        detached = value.detach()
        dtype = torch_api.float32 if detached.is_floating_point() else torch_api.int64
        return detached.to(device="cpu", dtype=dtype)
    if isinstance(value, Mapping):
        return {
            key: _move_postprocess_value_to_cpu(item, torch_api)
            for key, item in value.items()
        }
    if isinstance(value, tuple):
        converted = tuple(
            _move_postprocess_value_to_cpu(item, torch_api) for item in value
        )
        if hasattr(value, "_fields"):
            return type(value)(*converted)
        return converted
    if isinstance(value, list):
        return [_move_postprocess_value_to_cpu(item, torch_api) for item in value]
    return value


def move_model_output_to_cpu(outputs: Any, torch_api: Any) -> Any:
    """Rebuild a Transformers ModelOutput with no accelerator-resident tensors."""

    if not isinstance(outputs, Mapping):
        raise ValueError("model output must be a Transformers-compatible mapping")
    converted = {
        key: _move_postprocess_value_to_cpu(value, torch_api)
        for key, value in outputs.items()
    }
    try:
        return type(outputs)(**converted)
    except TypeError as exc:
        raise ValueError(
            "model output could not be deterministically rebuilt for CPU post-processing"
        ) from exc


def _finite_number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a number")
    output = float(value)
    if not math.isfinite(output):
        raise ValueError(f"{label} must be finite")
    return output


def _normalize_bbox(value: Any, dimensions: Mapping[str, int], label: str) -> list[float]:
    raw = _to_python(value)
    if not isinstance(raw, list) or len(raw) != 4:
        raise ValueError(f"{label} must have four coordinates")
    box = [_finite_number(item, label) for item in raw]
    x0, y0, x1, y1 = box
    if min(box) < 0 or x1 < x0 or y1 < y0:
        raise ValueError(f"{label} is not a valid top-left/bottom-right bbox")
    if x1 > dimensions["width_px"] + 1e-3 or y1 > dimensions["height_px"] + 1e-3:
        raise ValueError(f"{label} exceeds image bounds")
    return box


def _normalize_raw_polygon(value: Any, label: str) -> tuple[Any, list[list[float]]]:
    raw = _to_python(value)
    if not isinstance(raw, list):
        raise ValueError(f"{label} must be a coordinate list")
    if raw and all(isinstance(item, (int, float)) and not isinstance(item, bool) for item in raw):
        if len(raw) < 8 or len(raw) % 2:
            raise ValueError(f"{label} flat polygon must have an even count of at least 8")
        flat = [_finite_number(item, label) for item in raw]
        return flat, [[flat[index], flat[index + 1]] for index in range(0, len(flat), 2)]
    points: list[list[float]] = []
    for item in raw:
        if not isinstance(item, list) or len(item) != 2:
            raise ValueError(f"{label} point must contain two coordinates")
        points.append([_finite_number(item[0], label), _finite_number(item[1], label)])
    if len(points) < 4:
        raise ValueError(f"{label} must contain at least four points")
    return copy.deepcopy(points), points


def _validate_polygon_bounds(
    polygon: Sequence[Sequence[float]],
    dimensions: Mapping[str, int],
    label: str,
) -> None:
    for point in polygon:
        x, y = point
        if x < 0 or y < 0:
            raise ValueError(f"{label} contains a negative coordinate")
        if x > dimensions["width_px"] + 1e-3 or y > dimensions["height_px"] + 1e-3:
            raise ValueError(f"{label} exceeds image bounds")


def normalize_prediction(
    result: Mapping[str, Any],
    *,
    id2label: Mapping[Any, Any],
    dimensions: Mapping[str, int],
) -> dict[str, Any]:
    required = ("scores", "labels", "boxes", "polygon_points", "order_seq")
    if any(key not in result for key in required):
        raise ValueError(
            "postprocessed result must contain scores, labels, boxes, "
            "polygon_points, and order_seq"
        )
    scores = _to_python(result["scores"])
    labels = _to_python(result["labels"])
    boxes = _to_python(result["boxes"])
    polygons = _to_python(result["polygon_points"])
    order_sequence = _to_python(result["order_seq"])
    if not all(
        isinstance(value, list)
        for value in (scores, labels, boxes, polygons, order_sequence)
    ):
        raise ValueError("postprocessed result arrays must be lists")
    count = len(scores)
    if count < 1 or count > MAX_DETECTIONS:
        raise ValueError(f"postprocessed result must contain 1-{MAX_DETECTIONS} detections")
    if (
        len(labels) != count
        or len(boxes) != count
        or len(polygons) != count
        or len(order_sequence) != count
    ):
        raise ValueError("postprocessed result arrays have different lengths")

    normalized_map: dict[int, str] = {}
    for key, label in id2label.items():
        try:
            label_id = int(key)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"id2label key is not an integer: {key!r}") from exc
        label_text = str(label)
        if label_id < 0 or not label_text:
            raise ValueError("id2label entries must contain a non-negative id and non-empty label")
        normalized_map[label_id] = label_text

    raw_scores: list[float] = []
    raw_labels: list[int] = []
    raw_boxes: list[list[float]] = []
    raw_polygons: list[Any] = []
    raw_order_sequence: list[int] = []
    detections: list[dict[str, Any]] = []
    for index, (
        score_value,
        label_value,
        box_value,
        polygon_value,
        raw_order_value,
    ) in enumerate(
        zip(scores, labels, boxes, polygons, order_sequence), 1
    ):
        score = _finite_number(score_value, f"scores/{index - 1}")
        if not 0 <= score <= 1:
            raise ValueError(f"scores/{index - 1} must be between 0 and 1")
        if isinstance(label_value, bool) or not isinstance(label_value, int):
            raise ValueError(f"labels/{index - 1} must be an integer")
        label_id = int(label_value)
        if label_id not in normalized_map:
            raise ValueError(f"labels/{index - 1} is absent from id2label: {label_id}")
        if isinstance(raw_order_value, bool) or not isinstance(raw_order_value, int):
            raise ValueError(f"order_seq/{index - 1} must be a non-negative integer")
        raw_order = int(raw_order_value)
        if raw_order < 0:
            raise ValueError(f"order_seq/{index - 1} must be a non-negative integer")
        box = _normalize_bbox(box_value, dimensions, f"boxes/{index - 1}")
        raw_polygon, polygon = _normalize_raw_polygon(
            polygon_value, f"polygon_points/{index - 1}"
        )
        _validate_polygon_bounds(polygon, dimensions, f"polygon_points/{index - 1}")
        raw_scores.append(score)
        raw_labels.append(label_id)
        raw_boxes.append(box)
        raw_polygons.append(raw_polygon)
        raw_order_sequence.append(raw_order)
        detections.append(
            {
                "order": index,
                "label_id": label_id,
                "label": normalized_map[label_id],
                "score": score,
                "bbox": copy.deepcopy(box),
                "polygon_points": copy.deepcopy(polygon),
                "raw": {
                    "score": score,
                    "label_id": label_id,
                    "box": copy.deepcopy(box),
                    "polygon_points": copy.deepcopy(raw_polygon),
                    "order_seq": raw_order,
                },
            }
        )
    label_map = [
        {"label_id": label_id, "label": label}
        for label_id, label in sorted(normalized_map.items())
    ]
    return {
        "detections": detections,
        "raw_output": {
            "format": "transformers_pp_doclayout_v3_postprocess_v1",
            "scores": raw_scores,
            "labels": raw_labels,
            "boxes": raw_boxes,
            "polygon_points": raw_polygons,
            "order_seq": raw_order_sequence,
            "result_order": list(range(1, count + 1)),
            "label_map": label_map,
        },
    }


class TransformersPPDocLayoutBackend:
    def __init__(self, processor: Any, model: Any, torch_api: Any, device: str) -> None:
        self.processor = processor
        self.model = model
        self.torch = torch_api
        self.device = device

    def predict(
        self,
        image_path: Path,
        *,
        threshold: float,
        role: str,
        dimensions: Mapping[str, int],
    ) -> dict[str, Any]:
        del role
        with Image.open(image_path) as source:
            image = source.convert("RGB")
            inputs = self.processor(images=image, return_tensors="pt")
            if hasattr(inputs, "to"):
                inputs = inputs.to(self.device)
            else:
                inputs = {
                    key: value.to(self.device) if hasattr(value, "to") else value
                    for key, value in inputs.items()
                }
            with self.torch.inference_mode():
                outputs = self.model(**inputs)
            cpu_outputs = move_model_output_to_cpu(outputs, self.torch)
            target_sizes = self.torch.tensor(
                [[int(image.height), int(image.width)]],
                device="cpu",
                dtype=self.torch.int64,
            )
            postprocessed = self.processor.post_process_object_detection(
                cpu_outputs,
                threshold=threshold,
                target_sizes=target_sizes,
            )
        if not isinstance(postprocessed, Sequence) or len(postprocessed) != 1:
            raise ValueError("processor must return exactly one postprocessed page result")
        return normalize_prediction(
            postprocessed[0],
            id2label=self.model.config.id2label,
            dimensions=dimensions,
        )


def _output_payload(record: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "status": record["status"],
        "detections": record["detections"],
        "raw_output": record["raw_output"],
        "warnings": record["warnings"],
        "error": record["error"],
    }


def _signature_payload(record: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": record["schema_version"],
        "record_type": record["record_type"],
        "sample_id": record["input"]["sample_id"],
        "input_sha256": record["hashes"]["input_sha256"],
        "manifest_sha256": record["hashes"]["manifest_sha256"],
        "package_fingerprint_sha256": record["configuration"]["package_fingerprint_sha256"],
        "model_artifact_fingerprint_sha256": record["configuration"]["model"][
            "artifact_fingerprint_sha256"
        ],
        "device_effective": record["configuration"]["device_effective"],
        "inference_device": record["configuration"]["inference_device"],
        "postprocess_device": record["configuration"]["postprocess_device"],
        "threshold": record["configuration"]["inference"]["threshold"],
        "output_sha256": record["hashes"]["output_sha256"],
        "runner_version": record["configuration"]["runner_version"],
    }


def record_integrity_payload(record: Mapping[str, Any]) -> dict[str, Any]:
    payload = copy.deepcopy(dict(record))
    payload["hashes"].pop("record_integrity_sha256", None)
    return payload


def _validator() -> Draft202012Validator:
    schema = json.loads(
        SCHEMA_PATH.read_text(encoding="utf-8"),
        object_pairs_hook=_reject_duplicate_keys,
    )
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema, format_checker=FormatChecker())


def validate_record(record: Mapping[str, Any]) -> list[str]:
    errors: list[str] = []
    for error in sorted(_validator().iter_errors(record), key=lambda item: list(item.path)):
        location = "/" + "/".join(str(value) for value in error.path)
        errors.append(f"{location}: {error.message}")
    if errors:
        return errors

    if record["input"]["sample_id"] != expected_sample_id(record["input"]):
        errors.append("/input/sample_id: does not match canonical input payload")
    if record["hashes"]["input_sha256"] != record["input"]["image_sha256"]:
        errors.append("/hashes/input_sha256: does not match input image hash")
    if record["hashes"]["manifest_sha256"] != record["input"]["manifest_sha256"]:
        errors.append("/hashes/manifest_sha256: does not match input manifest hash")

    configuration = record["configuration"]
    expected_packages = sha256_json(
        package_fingerprint_payload(
            packages=configuration["packages"],
            python_version=configuration["python_version"],
            platform_value=configuration["platform"],
            device_requested=configuration["device_requested"],
            device_effective=configuration["device_effective"],
            inference_device=configuration["inference_device"],
            postprocess_device=configuration["postprocess_device"],
        )
    )
    if configuration["package_fingerprint_sha256"] != expected_packages:
        errors.append(
            "/configuration/package_fingerprint_sha256: does not match package/runtime payload"
        )
    if configuration["inference_device"] != configuration["device_effective"]:
        errors.append(
            "/configuration/inference_device: must match device_effective"
        )
    if configuration["postprocess_device"] != "cpu":
        errors.append("/configuration/postprocess_device: must be cpu")
    model = configuration["model"]
    files = model["files"]
    expected_artifact = sha256_json(
        {"repo_id": model["repo_id"], "revision": model["revision"], "files": files}
    )
    if model["artifact_fingerprint_sha256"] != expected_artifact:
        errors.append(
            "/configuration/model/artifact_fingerprint_sha256: does not match model files"
        )
    if model["file_count"] != len(files):
        errors.append("/configuration/model/file_count: does not match files length")
    if model["size_bytes"] != sum(item["size_bytes"] for item in files):
        errors.append("/configuration/model/size_bytes: does not match files")
    weight_files = [item for item in files if item["relative_path"] == "model.safetensors"]
    if len(weight_files) != 1:
        errors.append("/configuration/model/files: exactly one model.safetensors is required")
    else:
        weight = weight_files[0]
        if model["weights_size_bytes"] != weight["size_bytes"]:
            errors.append("/configuration/model/weights_size_bytes: does not match file")
        if model["weights_sha256"] != weight["sha256"]:
            errors.append("/configuration/model/weights_sha256: does not match file")

    output_hash = sha256_json(_output_payload(record))
    if record["hashes"]["output_sha256"] != output_hash:
        errors.append("/hashes/output_sha256: does not match output payload")
    signature = sha256_json(_signature_payload(record))
    if record["hashes"]["signature_sha256"] != signature:
        errors.append("/hashes/signature_sha256: does not match signature payload")
    if record["run_id"] != "ppdlpoc_" + signature[:24]:
        errors.append("/run_id: does not match signature")

    provenance = record["provenance"]
    if provenance["inference_device"] != configuration["inference_device"]:
        errors.append(
            "/provenance/inference_device: does not match configuration"
        )
    if provenance["postprocess_device"] != configuration["postprocess_device"]:
        errors.append(
            "/provenance/postprocess_device: does not match configuration"
        )

    if record["status"] == "completed":
        raw = record["raw_output"]
        detections = record["detections"]
        lengths = [
            len(raw["scores"]),
            len(raw["labels"]),
            len(raw["boxes"]),
            len(raw["polygon_points"]),
            len(raw["order_seq"]),
            len(raw["result_order"]),
            len(detections),
        ]
        if len(set(lengths)) != 1:
            errors.append("/raw_output: output arrays and detections have different lengths")
        else:
            expected_order = list(range(1, len(detections) + 1))
            if raw["result_order"] != expected_order:
                errors.append("/raw_output/result_order: must be contiguous and one-based")
            labels_by_id = {
                item["label_id"]: item["label"] for item in raw["label_map"]
            }
            for index, detection in enumerate(detections):
                order = index + 1
                if detection["order"] != order:
                    errors.append(f"/detections/{index}/order: must equal {order}")
                expected_raw = {
                    "score": raw["scores"][index],
                    "label_id": raw["labels"][index],
                    "box": raw["boxes"][index],
                    "polygon_points": raw["polygon_points"][index],
                    "order_seq": raw["order_seq"][index],
                }
                if canonical_json(detection["raw"]) != canonical_json(expected_raw):
                    errors.append(f"/detections/{index}/raw: does not match raw output arrays")
                try:
                    _, polygon = _normalize_raw_polygon(
                        raw["polygon_points"][index], f"raw_output/polygon_points/{index}"
                    )
                    if canonical_json(detection["polygon_points"]) != canonical_json(polygon):
                        errors.append(
                            f"/detections/{index}/polygon_points: does not match raw polygon"
                        )
                    _normalize_bbox(
                        detection["bbox"], record["input"]["dimensions"], f"detections/{index}/bbox"
                    )
                    _validate_polygon_bounds(
                        detection["polygon_points"],
                        record["input"]["dimensions"],
                        f"detections/{index}/polygon_points",
                    )
                except ValueError as exc:
                    errors.append(f"/detections/{index}: {exc}")
                if detection["label_id"] != raw["labels"][index]:
                    errors.append(f"/detections/{index}/label_id: does not match raw label")
                if labels_by_id.get(detection["label_id"]) != detection["label"]:
                    errors.append(f"/detections/{index}/label: does not match label map")

    expected_integrity = sha256_json(record_integrity_payload(record))
    if record["hashes"]["record_integrity_sha256"] != expected_integrity:
        errors.append(
            "/hashes/record_integrity_sha256: does not match the complete record"
        )
    return errors


def finalize_record(
    *,
    sample: Mapping[str, Any],
    configuration: Mapping[str, Any],
    prediction: Optional[Mapping[str, Any]],
    setup_ms: float,
    inference_ms: float,
    generated_at: Optional[str] = None,
    warnings: Sequence[str] = (),
    error: Optional[Mapping[str, str]] = None,
) -> dict[str, Any]:
    completed = prediction is not None and error is None
    if completed:
        detections = copy.deepcopy(prediction["detections"])
        raw_output = copy.deepcopy(prediction["raw_output"])
        status = "completed"
        error_value = None
    else:
        detections = []
        raw_output = None
        status = "failed"
        if error is None:
            raise ValueError("failed record requires an error")
        error_value = dict(error)
    record: dict[str, Any] = {
        "schema_version": "0.1",
        "record_type": "pp_doclayout_poc_run",
        "run_id": "ppdlpoc_" + "0" * 24,
        "input": copy.deepcopy(dict(sample)),
        "configuration": copy.deepcopy(dict(configuration)),
        "status": status,
        "detections": detections,
        "raw_output": raw_output,
        "timing": {
            "setup_ms": round(max(0.0, float(setup_ms)), 6),
            "inference_ms": round(max(0.0, float(inference_ms)), 6),
        },
        "warnings": list(dict.fromkeys(str(value) for value in warnings if value)),
        "error": error_value,
        "hashes": {
            "input_sha256": sample["image_sha256"],
            "manifest_sha256": sample["manifest_sha256"],
            "output_sha256": "0" * 64,
            "signature_sha256": "0" * 64,
            "record_integrity_sha256": "0" * 64,
        },
        "provenance": {
            "generated_at": generated_at or utc_now(),
            "selection_method": "verified-manifest-layout-strata-v0.1",
            "question_independent": True,
            "question_data_used": False,
            "gold_data_used": False,
            "prediction_data_used": False,
            "answer_data_used": False,
            "source_document_opened": False,
            "source_data_used": True,
            "layout_candidate": True,
            "inference_device": configuration["inference_device"],
            "postprocess_device": configuration["postprocess_device"],
            "evidence_connected": False,
            "search_unit_connected": False,
            "mainline_connected": False,
        },
    }
    record["hashes"]["output_sha256"] = sha256_json(_output_payload(record))
    signature = sha256_json(_signature_payload(record))
    record["hashes"]["signature_sha256"] = signature
    record["run_id"] = "ppdlpoc_" + signature[:24]
    record["hashes"]["record_integrity_sha256"] = sha256_json(
        record_integrity_payload(record)
    )
    problems = validate_record(record)
    if problems:
        raise ValueError("generated PP-DocLayout record is invalid: " + "; ".join(problems))
    return record


def run_samples(
    samples: Sequence[Mapping[str, Any]],
    *,
    repository_root: Path,
    backend: Any,
    configuration: Mapping[str, Any],
    setup_ms: float = 0.0,
    generated_at: Optional[str] = None,
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for sample in samples:
        started = time.perf_counter()
        try:
            image = resolve_sample_image(sample, repository_root)
            prediction = backend.predict(
                image,
                threshold=configuration["inference"]["threshold"],
                role=sample["role"],
                dimensions=sample["dimensions"],
            )
            elapsed = (time.perf_counter() - started) * 1000
            records.append(
                finalize_record(
                    sample=sample,
                    configuration=configuration,
                    prediction=prediction,
                    setup_ms=setup_ms,
                    inference_ms=elapsed,
                    generated_at=generated_at,
                )
            )
        except Exception as exc:  # preserve each individual failure in the PoC record
            elapsed = (time.perf_counter() - started) * 1000
            records.append(
                finalize_record(
                    sample=sample,
                    configuration=configuration,
                    prediction=None,
                    setup_ms=setup_ms,
                    inference_ms=elapsed,
                    generated_at=generated_at,
                    error={
                        "component": "pp_doclayout_v3",
                        "type": type(exc).__name__,
                        "message": str(exc) or type(exc).__name__,
                    },
                )
            )
    return records


def write_jsonl(
    path: Path,
    records: Iterable[Mapping[str, Any]],
    *,
    repository_root: Path,
    overwrite: bool,
) -> None:
    root = repository_root.resolve(strict=True)
    output = path if path.is_absolute() else repository_root / path
    try:
        output.absolute().relative_to(repository_root.absolute())
    except ValueError as exc:
        raise ValueError("output must stay inside repository root") from exc
    if output.exists() and not overwrite:
        raise ValueError(f"output exists; pass --overwrite to replace it: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.is_symlink() or output.parent.is_symlink():
        raise ValueError("output path and parent must not be symlinks")
    if not _inside(output.parent.resolve(strict=True), root):
        raise ValueError("output parent escapes repository root")
    payload = "".join(canonical_json(record) + "\n" for record in records)
    temporary = output.with_name(output.name + ".tmp")
    temporary.write_text(payload, encoding="utf-8")
    temporary.replace(output)


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("--fixtures", type=Path, required=True)
    value.add_argument("--model-dir", type=Path, required=True)
    value.add_argument("--repository-root", type=Path, default=ROOT)
    value.add_argument(
        "--output",
        type=Path,
        default=Path("artifacts/pp-doclayout-poc-v0.1/runs.jsonl"),
    )
    value.add_argument("--device", choices=["mps", "cpu"], default="mps")
    value.add_argument("--threshold", type=float, default=0.5)
    value.add_argument("--check", action="store_true", help="verify local inputs without loading weights")
    value.add_argument("--overwrite", action="store_true")
    return value


def main() -> int:
    args = parser().parse_args()
    try:
        repository_root = args.repository_root.resolve(strict=True)
        require_offline_environment()
        fixtures = load_verified_manifest(args.fixtures, repository_root)
        samples = select_poc_samples(
            fixtures,
            manifest_path=args.fixtures,
            repository_root=repository_root,
        )
        for sample in samples:
            resolve_sample_image(sample, repository_root)
        model = fingerprint_local_model(
            args.model_dir,
            repository_root=repository_root,
            revision=PINNED_MODEL_REVISION,
            expected_weight_sha256=PINNED_WEIGHT_SHA256,
        )
        packages = installed_package_versions()
        torch_api = importlib.import_module("torch")
        if args.device == "mps" and not bool(torch_api.backends.mps.is_available()):
            raise ValueError("MPS was requested but is unavailable; implicit CPU fallback is prohibited")
        configuration = build_configuration(
            model=model,
            packages=packages,
            device_requested=args.device,
            device_effective=args.device,
            threshold=args.threshold,
        )
        if args.check:
            print(
                canonical_json(
                    {
                        "status": "ready",
                        "samples": [sample["sample_id"] for sample in samples],
                        "roles": [sample["role"] for sample in samples],
                        "model": model,
                        "offline_environment": OFFLINE_ENVIRONMENT,
                        "package_fingerprint_sha256": configuration[
                            "package_fingerprint_sha256"
                        ],
                    }
                )
            )
            return 0

        setup_started = time.perf_counter()
        processor, loaded_model = load_local_components(
            repository_root / model["local_path"],
            device=args.device,
            torch_api=torch_api,
        )
        setup_ms = (time.perf_counter() - setup_started) * 1000
        backend = TransformersPPDocLayoutBackend(
            processor,
            loaded_model,
            torch_api,
            args.device,
        )
        records = run_samples(
            samples,
            repository_root=repository_root,
            backend=backend,
            configuration=configuration,
            setup_ms=setup_ms,
        )
        write_jsonl(
            args.output,
            records,
            repository_root=repository_root,
            overwrite=args.overwrite,
        )
    except (OSError, ValueError, RuntimeError, ImportError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    counts: dict[str, int] = {}
    for record in records:
        counts[record["status"]] = counts.get(record["status"], 0) + 1
    print(f"wrote {len(records)} isolated PP-DocLayoutV3 records to {args.output}")
    print(
        "statuses: "
        + ", ".join(f"{key}={value}" for key, value in sorted(counts.items()))
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
