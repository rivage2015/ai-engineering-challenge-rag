#!/usr/bin/env python3
"""Extract question-independent OCR observations with Apple Vision and Tesseract."""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import io
import json
import math
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
from collections import OrderedDict
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import classify_visual_assets as classifier  # noqa: E402
import validate_ocr_observations as contract  # noqa: E402
import validate_visual_classifications as classification_validator  # noqa: E402


VISION_SOURCE = ROOT / "scripts" / "apple_vision_ocr.swift"
DEFAULT_TIMEOUT = 180.0
MAX_IMAGE_PIXELS = contract.MAX_IMAGE_PIXELS
TESSERACT_VERSION_RE = re.compile(r"^tesseract\s+([0-9]+(?:\.[0-9]+){1,3})$")


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise


def atomic_write_json(path: Path, value: Any) -> None:
    atomic_write_bytes(
        path,
        (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode(
            "utf-8"
        ),
    )


def atomic_write_jsonl(path: Path, records: Iterable[dict[str, Any]]) -> None:
    payload = "".join(contract.canonical_json(record) + "\n" for record in records)
    atomic_write_bytes(path, payload.encode("utf-8"))


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def ensure_cache_subdirectory(cache_root: Path, name: str) -> Path:
    """Create one direct cache subdirectory without following a child symlink."""
    if not name or name in {".", ".."} or os.sep in name:
        raise ValueError(f"invalid cache subdirectory name: {name!r}")
    if cache_root.is_symlink() or not cache_root.is_dir():
        raise ValueError(f"cache root must be a real directory: {cache_root}")
    resolved_root = cache_root.resolve(strict=True)
    target = cache_root / name
    if target.is_symlink():
        raise ValueError(f"cache subdirectory must not be a symlink: {target}")
    target.mkdir(mode=0o700, exist_ok=True)
    if target.is_symlink() or not target.is_dir():
        raise ValueError(f"cache subdirectory must be a real directory: {target}")
    resolved_target = target.resolve(strict=True)
    if not _inside(resolved_target, resolved_root):
        raise ValueError(f"cache subdirectory escapes cache root: {target}")
    return target


def normalize_assets(
    raw_assets: list[dict[str, Any]], asset_root: Path
) -> list[dict[str, Any]]:
    if asset_root.is_symlink():
        raise ValueError(f"asset_root must not be a symlink: {asset_root}")
    root = asset_root.resolve(strict=True)
    if not root.is_dir():
        raise ValueError(f"asset_root is not a directory: {root}")
    assets = [classifier.normalize_asset(record, root) for record in raw_assets]
    identifiers = [asset["asset_id"] for asset in assets]
    if len(identifiers) != len(set(identifiers)):
        raise ValueError("--assets contains duplicate asset_id values")
    for asset in assets:
        # Reject both lexical escapes and every symlink component.  Resolving
        # only the final path is insufficient because an in-root symlink may
        # point outside the declared materialization directory.
        contract._reject_symlink_or_escape(asset, root)
        if not _inside(asset["path"], root):
            raise ValueError(
                f"{asset['asset_id']}: materialized image resolves outside asset_root"
            )
    return assets


def verified_image_bytes(asset: dict[str, Any], asset_root: Path) -> bytes:
    contract._reject_symlink_or_escape(asset, asset_root)
    maximum_bytes = getattr(contract, "MAX_IMAGE_BYTES", 200 * 1024 * 1024)
    actual_size = asset["path"].stat().st_size
    if actual_size > maximum_bytes:
        raise ValueError(
            f"{asset['asset_id']}: image exceeds {maximum_bytes} byte safety limit"
        )
    image_bytes = classifier.verified_asset_bytes(asset)
    mime_type, width, height = contract._actual_image_metadata(
        image_bytes, asset["path"]
    )
    if mime_type != asset["mime_type"]:
        raise ValueError(
            f"{asset['asset_id']}: image MIME mismatch: "
            f"declared={asset['mime_type']} actual={mime_type}"
        )
    dimensions = {"width_px": width, "height_px": height}
    if dimensions != asset["dimensions"]:
        raise ValueError(
            f"{asset['asset_id']}: image dimensions mismatch: "
            f"declared={asset['dimensions']} actual={dimensions}"
        )
    if width * height > MAX_IMAGE_PIXELS:
        raise ValueError(
            f"{asset['asset_id']}: image exceeds {MAX_IMAGE_PIXELS} pixel safety limit"
        )
    return image_bytes


def preflight_asset_files(assets: list[dict[str, Any]], asset_root: Path) -> None:
    """Reject unsafe paths and oversized files before any validator reads bytes."""
    maximum_bytes = getattr(contract, "MAX_IMAGE_BYTES", 200 * 1024 * 1024)
    for asset in assets:
        contract._reject_symlink_or_escape(asset, asset_root)
        actual_size = asset["path"].stat().st_size
        if actual_size > maximum_bytes:
            raise ValueError(
                f"{asset['asset_id']}: image exceeds {maximum_bytes} byte safety limit"
            )


def eligible_inputs(
    assets: list[dict[str, Any]], classifications: list[dict[str, Any]]
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    if len(assets) != len(classifications):
        raise ValueError(
            f"upstream count mismatch: assets={len(assets)} "
            f"classifications={len(classifications)}"
        )
    eligible: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for position, (asset, classification) in enumerate(
        zip(assets, classifications), 1
    ):
        if classification.get("asset_id") != asset["asset_id"]:
            raise ValueError(
                f"upstream record {position}: asset/classification order mismatch"
            )
        errors = classification_validator.validate(classification)
        if errors:
            raise ValueError(
                f"classification record {position} is invalid: " + "; ".join(errors)
            )
        routes = classification.get("routes")
        if isinstance(routes, list) and "ocr_text" in routes:
            eligible.append((asset, classification))
    if not eligible:
        raise ValueError("no classification record is routed to ocr_text")
    return eligible


def _swift_target() -> str:
    machine = platform.machine().lower()
    if machine in {"arm64", "aarch64"}:
        architecture = "arm64"
    elif machine in {"x86_64", "amd64"}:
        architecture = "x86_64"
    else:
        raise RuntimeError(f"unsupported macOS architecture for Vision OCR: {machine}")
    target = contract.APPLE_COMPILE_TARGET
    expected = f"{architecture}-apple-macosx13.0"
    if target != expected:
        raise RuntimeError(
            f"Apple Vision compile target mismatch: contract={target} host={expected}"
        )
    return target


def compile_vision_helper(
    source: Path, build_dir: Path, *, timeout: float
) -> Path:
    if platform.system() != "Darwin":
        raise RuntimeError(
            "Apple Vision OCR requires macOS; unit tests must mock the internal runner"
        )
    if source.is_symlink() or not source.is_file():
        raise ValueError(f"Vision helper source must be a regular non-symlink file: {source}")
    source = source.resolve(strict=True)
    xcrun = shutil.which("xcrun")
    if not xcrun:
        raise RuntimeError("xcrun is required to compile the Apple Vision helper")
    version_process = subprocess.run(
        [xcrun, "swiftc", "--version"],
        capture_output=True,
        text=True,
        timeout=min(timeout, 30.0),
        check=False,
    )
    if version_process.returncode != 0:
        raise RuntimeError(
            "cannot query swiftc: "
            + (version_process.stderr.strip() or version_process.stdout.strip())
        )
    target = _swift_target()
    runtime = contract.current_engine_runtime("apple_vision")
    source_sha256 = hashlib.sha256(source.read_bytes()).hexdigest()
    if source_sha256 != runtime["wrapper_sha256"]:
        raise RuntimeError("Vision source hash does not match the fixed runtime contract")
    if version_process.stdout.strip() != runtime["swiftc_version"]:
        raise RuntimeError("swiftc version changed during Vision runtime verification")
    if target != runtime["compile_target"]:
        raise RuntimeError("Vision compile target does not match the runtime contract")
    build_signature = contract.apple_vision_build_signature(runtime)
    build_dir.mkdir(parents=True, exist_ok=True)
    if build_dir.is_symlink():
        raise ValueError(f"Vision build directory must not be a symlink: {build_dir}")
    binary = build_dir / f"apple_vision_ocr-{build_signature[:24]}"
    metadata = binary.with_suffix(".json")
    if binary.is_symlink() or metadata.is_symlink():
        raise ValueError("Vision build cache files must not be symlinks")
    if binary.exists() or metadata.exists():
        if (
            binary.is_symlink()
            or metadata.is_symlink()
            or not binary.is_file()
            or not metadata.is_file()
        ):
            raise ValueError("Vision build cache is incomplete or contains a symlink")
        try:
            cached = json.loads(metadata.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"invalid Vision build cache metadata: {exc}") from exc
        actual_binary_sha = hashlib.sha256(binary.read_bytes()).hexdigest()
        if cached != {
            "build_signature": build_signature,
            "binary_sha256": actual_binary_sha,
        }:
            raise ValueError("Vision build cache metadata or binary hash mismatch")
        if not os.access(binary, os.X_OK):
            raise ValueError(f"cached Vision helper is not executable: {binary}")
        return binary

    module_cache = build_dir / "swift-module-cache"
    if module_cache.is_symlink():
        raise ValueError(f"Swift module cache must not be a symlink: {module_cache}")
    module_cache.mkdir(parents=True, exist_ok=True)
    if module_cache.is_symlink() or not module_cache.is_dir():
        raise ValueError(f"Swift module cache must be a real directory: {module_cache}")
    temporary = build_dir / f".{binary.name}.{os.getpid()}.tmp"
    command = [
        xcrun,
        "swiftc",
        "-module-cache-path",
        str(module_cache),
        "-target",
        target,
        "-O",
        "-o",
        str(temporary),
        str(source),
    ]
    process = subprocess.run(
        command, capture_output=True, text=True, timeout=timeout, check=False
    )
    if process.returncode != 0:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise RuntimeError(
            "Apple Vision helper compilation failed: "
            + (process.stderr.strip() or process.stdout.strip())
        )
    if temporary.is_symlink() or not temporary.is_file():
        raise RuntimeError("swiftc did not create a regular Vision helper binary")
    os.chmod(temporary, 0o755)
    os.replace(temporary, binary)
    atomic_write_json(
        metadata,
        {
            "build_signature": build_signature,
            "binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
        },
    )
    return binary


def resolve_vision_binary(
    value: Path | None,
    source: Path,
    build_dir: Path,
    *,
    timeout: float,
) -> Path:
    """Resolve a helper for low-level tests; production extraction always compiles source."""
    if value is None:
        return compile_vision_helper(source, build_dir, timeout=timeout)
    if value.is_symlink() or not value.is_file():
        raise ValueError(f"--vision-binary must be a regular non-symlink file: {value}")
    resolved = value.resolve(strict=True)
    if not os.access(resolved, os.X_OK):
        raise ValueError(f"--vision-binary is not executable: {resolved}")
    return resolved


def verify_tesseract(command: str | Path, *, timeout: float) -> Path:
    raw = str(command)
    resolved_name = shutil.which(raw) if os.sep not in raw else raw
    if not resolved_name:
        raise RuntimeError(f"Tesseract executable not found: {raw}")
    path = Path(resolved_name).resolve(strict=True)
    if not path.is_file() or not os.access(path, os.X_OK):
        raise ValueError(f"Tesseract must be an executable file: {path}")
    version = subprocess.run(
        [str(path), "--version"],
        capture_output=True,
        text=True,
        timeout=min(timeout, 30.0),
        check=False,
    )
    first_line = (version.stdout or version.stderr).splitlines()
    first_line = first_line[0].strip() if first_line else ""
    match = TESSERACT_VERSION_RE.fullmatch(first_line)
    if version.returncode != 0 or not match or match.group(1) != "5.5.2":
        raise RuntimeError(
            f"OCR v0.1 requires tesseract 5.5.2, received: {first_line or 'no version'}"
        )
    languages = subprocess.run(
        [str(path), "--list-langs"],
        capture_output=True,
        text=True,
        timeout=min(timeout, 30.0),
        check=False,
    )
    if languages.returncode != 0:
        raise RuntimeError("cannot list Tesseract languages")
    available = {line.strip() for line in languages.stdout.splitlines()[1:] if line.strip()}
    missing = {"jpn", "eng"} - available
    if missing:
        raise RuntimeError(
            "Tesseract is missing required OCR languages: " + ", ".join(sorted(missing))
        )
    expected_runtime = contract.current_engine_runtime("tesseract")
    if str(path) != expected_runtime["executable_path"]:
        raise ValueError(
            "Tesseract executable does not match the fixed PATH runtime: "
            f"actual={path} expected={expected_runtime['executable_path']}"
        )
    actual_binary_sha = contract.sha256_file(path)
    if actual_binary_sha != expected_runtime["binary_sha256"]:
        raise ValueError("Tesseract executable hash changed during runtime verification")
    return path


def _valid_bbox(value: Any) -> bool:
    if (
        not isinstance(value, list)
        or len(value) != 4
        or any(isinstance(item, bool) or not isinstance(item, int) for item in value)
    ):
        return False
    x, y, width, height = value
    return (
        x >= 0
        and y >= 0
        and width > 0
        and height > 0
        and x + width <= 1000
        and y + height <= 1000
    )


def _quantized_pixel_bbox(
    left: int, top: int, width: int, height: int, image_width: int, image_height: int
) -> list[int]:
    if left < 0 or top < 0 or width <= 0 or height <= 0:
        raise ValueError("OCR line bbox has invalid pixel coordinates")
    if left + width > image_width or top + height > image_height:
        raise ValueError("OCR line bbox exceeds decoded image dimensions")
    x = max(0, min(999, math.floor(left * 1000 / image_width)))
    y = max(0, min(999, math.floor(top * 1000 / image_height)))
    right = max(x + 1, min(1000, math.ceil((left + width) * 1000 / image_width)))
    bottom = max(y + 1, min(1000, math.ceil((top + height) * 1000 / image_height)))
    return [x, y, right - x, bottom - y]


def _standard_line(
    sequence: int, raw_text: str, bbox: list[int], confidence: float | None
) -> dict[str, Any]:
    if not isinstance(raw_text, str):
        raise ValueError("OCR raw_text must be a string")
    if not raw_text.strip() or any(
        character in raw_text for character in ("\n", "\r", "\x00")
    ):
        raise ValueError("OCR raw_text must contain exactly one non-empty line")
    if not _valid_bbox(bbox):
        raise ValueError(f"OCR bbox is outside normalized_1000 bounds: {bbox}")
    if confidence is not None:
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
            raise ValueError("OCR confidence must be numeric or null")
        confidence = float(confidence)
        if not math.isfinite(confidence) or not 0 <= confidence <= 1:
            raise ValueError("OCR confidence must be between 0 and 1")
    return {
        "line_id": f"line_{sequence}",
        "sequence": sequence,
        "raw_text": raw_text,
        "bbox": bbox,
        "confidence": confidence,
    }


def run_apple_vision_raw(
    binary: Path,
    image_bytes: bytes,
    dimensions: dict[str, int],
    *,
    timeout: float,
) -> tuple[str, list[dict[str, Any]], list[str], str | None]:
    process = subprocess.run(
        [str(binary)],
        input=image_bytes,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    try:
        payload = json.loads(process.stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        detail = process.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(
            f"Apple Vision helper returned invalid JSON: {exc}; stderr={detail[:300]}"
        ) from exc
    if not isinstance(payload, dict):
        raise RuntimeError("Apple Vision helper output must be an object")
    if process.returncode != 0 or payload.get("status") == "failed":
        error = payload.get("error")
        raise RuntimeError(
            str(error) if isinstance(error, str) and error else "Apple Vision OCR failed"
        )
    if payload.get("runner") != contract.ENGINE_RUNNERS["apple_vision"]:
        raise RuntimeError("Apple Vision helper runner identity mismatch")
    if payload.get("runner_version") != contract.RUNNER_VERSION:
        raise RuntimeError("Apple Vision helper version mismatch")
    if payload.get("request_revision") != contract.APPLE_VISION_CONFIG["request_revision"]:
        raise RuntimeError("Apple Vision request revision mismatch")
    if (
        payload.get("width_px") != dimensions["width_px"]
        or payload.get("height_px") != dimensions["height_px"]
    ):
        raise RuntimeError("Apple Vision decoded dimensions do not match verified image")
    raw_lines = payload.get("lines")
    if not isinstance(raw_lines, list):
        raise RuntimeError("Apple Vision lines must be an array")
    lines: list[dict[str, Any]] = []
    for sequence, item in enumerate(raw_lines, 1):
        if not isinstance(item, dict):
            raise RuntimeError("Apple Vision line must be an object")
        if item.get("sequence") != sequence:
            raise RuntimeError("Apple Vision line order is not sequential")
        lines.append(
            _standard_line(
                sequence,
                item.get("raw_text"),
                item.get("bbox"),
                item.get("confidence"),
            )
        )
    raw_warnings = payload.get("warnings")
    warnings = (
        [str(value) for value in raw_warnings if isinstance(value, str) and value]
        if isinstance(raw_warnings, list)
        else []
    )
    if not lines and "Apple Vision returned no text lines" not in warnings:
        warnings.append("Apple Vision returned no text lines")
    status = "completed" if lines and not warnings else "needs_review"
    return status, lines, list(dict.fromkeys(warnings)), None


def _tesseract_line_groups(tsv_text: str) -> list[dict[str, Any]]:
    reader = csv.DictReader(io.StringIO(tsv_text), delimiter="\t")
    required = {
        "level", "page_num", "block_num", "par_num", "line_num", "word_num",
        "left", "top", "width", "height", "conf", "text",
    }
    if reader.fieldnames is None or not required.issubset(reader.fieldnames):
        raise ValueError("Tesseract TSV header is incomplete")
    groups: OrderedDict[tuple[int, int, int, int], dict[str, Any]] = OrderedDict()
    for row_number, row in enumerate(reader, 2):
        text = (row.get("text") or "").strip()
        if not text:
            continue
        try:
            level = int(row["level"])
            confidence = float(row["conf"])
            key = tuple(
                int(row[name]) for name in ("page_num", "block_num", "par_num", "line_num")
            )
            left, top, width, height = (
                int(row[name]) for name in ("left", "top", "width", "height")
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"Tesseract TSV row {row_number} is invalid") from exc
        if level != 5 or confidence < 0:
            continue
        if width <= 0 or height <= 0 or left < 0 or top < 0:
            raise ValueError(f"Tesseract TSV row {row_number} has an invalid bbox")
        group = groups.setdefault(
            key,
            {
                "left": left,
                "top": top,
                "right": left + width,
                "bottom": top + height,
                "confidences": [],
            },
        )
        group["left"] = min(group["left"], left)
        group["top"] = min(group["top"], top)
        group["right"] = max(group["right"], left + width)
        group["bottom"] = max(group["bottom"], top + height)
        group["confidences"].append(max(0.0, min(100.0, confidence)) / 100.0)
    return list(groups.values())


def parse_tesseract_outputs(
    txt_text: str,
    tsv_text: str,
    dimensions: dict[str, int],
) -> tuple[str, list[dict[str, Any]], list[str], str | None]:
    raw_lines = [line for line in txt_text.splitlines() if line.strip()]
    groups = _tesseract_line_groups(tsv_text)
    warnings: list[str] = []
    if len(raw_lines) != len(groups):
        warnings.append(
            "Tesseract txt/TSV line alignment mismatch: "
            f"txt={len(raw_lines)} tsv={len(groups)}"
        )
    count = min(len(raw_lines), len(groups))
    lines: list[dict[str, Any]] = []
    for index in range(count):
        group = groups[index]
        bbox = _quantized_pixel_bbox(
            group["left"],
            group["top"],
            group["right"] - group["left"],
            group["bottom"] - group["top"],
            dimensions["width_px"],
            dimensions["height_px"],
        )
        confidence_values = group["confidences"]
        confidence = min(confidence_values) if confidence_values else None
        lines.append(_standard_line(index + 1, raw_lines[index], bbox, confidence))
    if not lines:
        warnings.append("Tesseract returned no aligned text lines")
    status = "completed" if lines and not warnings else "needs_review"
    return status, lines, list(dict.fromkeys(warnings)), None


def run_tesseract_raw(
    executable: Path,
    image_bytes: bytes,
    dimensions: dict[str, int],
    *,
    timeout: float,
) -> tuple[str, list[dict[str, Any]], list[str], str | None]:
    with tempfile.TemporaryDirectory(prefix="aiec-ocr-tesseract-") as temporary:
        output_base = Path(temporary) / "ocr"
        command = [
            str(executable),
            "stdin",
            str(output_base),
            "-l",
            "jpn+eng",
            "--oem",
            "1",
            "--psm",
            "3",
            "-c",
            "preserve_interword_spaces=1",
            "txt",
            "tsv",
        ]
        process = subprocess.run(
            command,
            input=image_bytes,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
        if process.returncode != 0:
            detail = process.stderr.decode("utf-8", errors="replace").strip()
            raise RuntimeError(f"Tesseract OCR failed: {detail[:500] or 'no detail'}")
        txt_path = output_base.with_suffix(".txt")
        tsv_path = output_base.with_suffix(".tsv")
        if txt_path.is_symlink() or tsv_path.is_symlink():
            raise RuntimeError("Tesseract produced a symlink output")
        try:
            txt_text = txt_path.read_text(encoding="utf-8")
            tsv_text = tsv_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise RuntimeError(f"Tesseract did not produce txt and TSV outputs: {exc}") from exc
    return parse_tesseract_outputs(txt_text, tsv_text, dimensions)


def _run_envelope(
    name: str,
    asset: dict[str, Any],
    result: tuple[str, list[dict[str, Any]], list[str], str | None],
    *,
    cache_hit: bool,
    inference_generated_at: str | None = None,
) -> dict[str, Any]:
    generated_at = utc_now()
    if inference_generated_at is None:
        inference_generated_at = generated_at
    status, lines, warnings, error = result
    run: dict[str, Any] = {
        "run_id": "ocr_run_" + "0" * 24,
        "engine": contract.expected_engine(name),
        "config": contract.EXPECTED_CONFIGS[name],
        "status": status,
        "lines": lines,
        "warnings": warnings,
        "error": error,
        "hashes": {
            "input_sha256": contract.engine_input_sha256(asset),
            "output_sha256": "0" * 64,
            "signature_sha256": "0" * 64,
        },
        "provenance": {
            "runner": contract.ENGINE_RUNNERS[name],
            "runner_version": contract.RUNNER_VERSION,
            "generated_at": generated_at,
            "inference_generated_at": inference_generated_at,
            "cache_hit": cache_hit,
            "question_independent": True,
        },
    }
    signature = contract.engine_signature_sha256(asset, run)
    run["run_id"] = contract.expected_run_id(signature)
    run["hashes"]["signature_sha256"] = signature
    run["hashes"]["output_sha256"] = contract.engine_output_sha256(run)
    return run


def failed_run(name: str, asset: dict[str, Any], error: Exception) -> dict[str, Any]:
    return _run_envelope(
        name,
        asset,
        (
            "failed",
            [],
            [],
            f"{type(error).__name__}: {error}",
        ),
        cache_hit=False,
    )


def _prototype_run(name: str, asset: dict[str, Any]) -> dict[str, Any]:
    return _run_envelope(
        name,
        asset,
        ("needs_review", [], ["signature prototype"], None),
        cache_hit=False,
        inference_generated_at="1970-01-01T00:00:00+00:00",
    )


def cache_path_for(cache_dir: Path, name: str, asset: dict[str, Any]) -> Path:
    prototype = _prototype_run(name, asset)
    signature = prototype["hashes"]["signature_sha256"]
    engine_dir = ensure_cache_subdirectory(cache_dir, name)
    path = engine_dir / f"{signature}.json"
    if path.is_symlink():
        raise ValueError(f"OCR cache entry must not be a symlink: {path}")
    return path


def _validate_cached_run(
    value: Any, name: str, asset: dict[str, Any]
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("cached OCR run must be an object")
    errors: list[str] = []
    contract._validate_run(value, contract.ENGINE_ORDER.index(name), errors)
    if errors:
        raise ValueError("invalid cached OCR run: " + "; ".join(errors))
    if value.get("status") == "failed":
        raise ValueError("failed OCR engine runs are not reusable cache entries")
    if value.get("engine") != contract.expected_engine(name):
        raise ValueError("cached OCR run engine does not match the current runtime")
    hashes = value.get("hashes", {})
    if hashes.get("input_sha256") != contract.engine_input_sha256(asset):
        raise ValueError("cached OCR run input hash does not match current image")
    expected_signature = contract.engine_signature_sha256(asset, value)
    if hashes.get("signature_sha256") != expected_signature:
        raise ValueError("cached OCR run signature does not match current contract")
    if value.get("run_id") != contract.expected_run_id(expected_signature):
        raise ValueError("cached OCR run ID does not match current contract")
    provenance = value.get("provenance", {})
    inference_generated_at = provenance.get("inference_generated_at")
    if not isinstance(inference_generated_at, str) or not inference_generated_at:
        raise ValueError("cached OCR run lacks inference_generated_at")
    return _run_envelope(
        name,
        asset,
        (
            value["status"],
            value["lines"],
            value["warnings"],
            value["error"],
        ),
        cache_hit=True,
        inference_generated_at=inference_generated_at,
    )


def load_cached_run(path: Path, name: str, asset: dict[str, Any]) -> dict[str, Any] | None:
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"OCR cache entry must be a regular non-symlink file: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot parse OCR cache entry {path}: {exc}") from exc
    cached_hashes = value.get("hashes") if isinstance(value, dict) else None
    cached_signature = (
        cached_hashes.get("signature_sha256")
        if isinstance(cached_hashes, dict)
        else None
    )
    if cached_signature != path.stem:
        raise ValueError("OCR cache filename does not match its engine signature")
    return _validate_cached_run(value, name, asset)


def run_or_cache_engine(
    name: str,
    asset: dict[str, Any],
    image_bytes: bytes,
    cache_dir: Path,
    *,
    restart: bool,
    vision_binary: Path,
    tesseract: Path,
    timeout: float,
) -> dict[str, Any]:
    cache_path = cache_path_for(cache_dir, name, asset)
    if not restart:
        cached = load_cached_run(cache_path, name, asset)
        if cached is not None:
            return cached
    try:
        if name == "apple_vision":
            result = run_apple_vision_raw(
                vision_binary, image_bytes, asset["dimensions"], timeout=timeout
            )
        elif name == "tesseract":
            result = run_tesseract_raw(
                tesseract, image_bytes, asset["dimensions"], timeout=timeout
            )
        else:
            raise ValueError(f"unknown OCR engine: {name}")
        run = _run_envelope(name, asset, result, cache_hit=False)
    except Exception as exc:
        return failed_run(name, asset, exc)
    if run["status"] != "failed":
        atomic_write_json(cache_path, run)
    return run


def observation_record(
    asset: dict[str, Any],
    classification: dict[str, Any],
    runs: list[dict[str, Any]],
) -> dict[str, Any]:
    if [run.get("engine", {}).get("name") for run in runs] != list(
        contract.ENGINE_ORDER
    ):
        raise ValueError("engine runs must be Apple Vision then Tesseract")
    input_sha256 = contract.sha256_json(
        contract.observation_input_payload(asset, classification)
    )
    consensus = contract.build_consensus(asset["asset_id"], runs)
    exactness = contract.expected_exactness(consensus, runs)
    status = contract.expected_status(exactness, runs)
    warnings = contract.expected_warnings(consensus, runs)
    signature = contract.observation_signature_sha256(input_sha256, runs)
    record: dict[str, Any] = {
        "schema_version": "0.1",
        "record_type": "ocr_observation",
        "observation_id": "ocr_" + signature[:24],
        "asset_id": asset["asset_id"],
        "asset": contract.expected_asset_envelope(asset),
        "source": asset["source"],
        "origin": asset["origin"],
        "classification_ref": contract.expected_classification_ref(classification),
        "engine_runs": runs,
        "consensus": consensus,
        "exactness": exactness,
        "warnings": warnings,
        "status": status,
        "hashes": {
            "input_sha256": input_sha256,
            "output_sha256": "0" * 64,
            "signature_sha256": signature,
        },
        "provenance": {
            "observer": contract.OBSERVER,
            "observer_version": contract.OBSERVER_VERSION,
            "generated_at": utc_now(),
            "cache_hit": all(
                run["provenance"]["cache_hit"] is True for run in runs
            ),
            "question_independent": True,
            "evidence_connected": False,
            "search_unit_connected": False,
        },
    }
    record["hashes"]["output_sha256"] = contract.sha256_json(
        contract.observation_output_payload(record)
    )
    errors = contract.validate(record)
    if errors:
        raise ValueError("constructed OCR observation is invalid: " + "; ".join(errors))
    return record


def extract_file(
    assets_path: Path,
    classifications_path: Path,
    output_path: Path,
    *,
    asset_root: Path | None = None,
    cache_dir: Path | None = None,
    vision_source: Path = VISION_SOURCE,
    vision_binary: Path | None = None,
    tesseract_command: str | Path = "tesseract",
    timeout: float = DEFAULT_TIMEOUT,
    restart: bool = False,
    max_assets: int | None = None,
) -> dict[str, Any]:
    if timeout <= 0:
        raise ValueError("timeout must be positive")
    if max_assets is not None and max_assets < 1:
        raise ValueError("max_assets must be positive")
    if output_path.is_symlink():
        raise ValueError(f"output path must not be a symlink: {output_path}")
    if vision_binary is not None:
        raise ValueError(
            "production OCR extraction does not accept a precompiled Vision binary"
        )
    if vision_source.is_symlink() or not vision_source.is_file():
        raise ValueError(
            f"Vision helper source must be the canonical repository file: {vision_source}"
        )
    if vision_source.resolve(strict=True) != VISION_SOURCE.resolve(strict=True):
        raise ValueError(
            "production OCR extraction does not accept an alternate Vision source"
        )
    if str(tesseract_command) != "tesseract":
        raise ValueError(
            "production OCR extraction uses only the Tesseract executable resolved by PATH"
        )
    raw_assets = classifier.load_jsonl(assets_path)
    classifications = classifier.load_jsonl(classifications_path)
    if not raw_assets:
        raise ValueError("--assets JSONL contains no records")
    if not classifications:
        raise ValueError("--classifications JSONL contains no records")
    root = (asset_root or assets_path.parent).absolute()
    assets = normalize_assets(raw_assets, root)
    preflight_asset_files(assets, root.resolve(strict=True))

    # Prove the complete upstream binding before filtering the ocr_text route.
    classification_validator.validate_jsonl(
        classifications_path, assets_path, asset_root=root
    )
    eligible = eligible_inputs(assets, classifications)
    if max_assets is not None:
        eligible = eligible[:max_assets]

    cache_root = (cache_dir or output_path.with_name(output_path.name + ".cache"))
    if cache_root.exists() and cache_root.is_symlink():
        raise ValueError(f"cache directory must not be a symlink: {cache_root}")
    cache_root.mkdir(parents=True, exist_ok=True)
    if cache_root.is_symlink() or not cache_root.is_dir():
        raise ValueError(f"cache directory must be a real directory: {cache_root}")
    build_dir = ensure_cache_subdirectory(cache_root, "_vision_build")
    resolved_vision = resolve_vision_binary(
        None, VISION_SOURCE, build_dir, timeout=timeout
    )
    resolved_tesseract = verify_tesseract("tesseract", timeout=timeout)

    records: list[dict[str, Any]] = []
    stats: dict[str, Any] = {
        "eligible": len(eligible),
        "records": 0,
        "observed": 0,
        "needs_review": 0,
        "failed": 0,
        "engine_cache_hits": 0,
        "engines": list(contract.ENGINE_ORDER),
    }
    for position, (asset, classification) in enumerate(eligible, 1):
        try:
            image_bytes = verified_image_bytes(asset, root.resolve(strict=True))
        except Exception as exc:
            runs = [failed_run(name, asset, exc) for name in contract.ENGINE_ORDER]
        else:
            runs = [
                run_or_cache_engine(
                    name,
                    asset,
                    image_bytes,
                    cache_root,
                    restart=restart,
                    vision_binary=resolved_vision,
                    tesseract=resolved_tesseract,
                    timeout=timeout,
                )
                for name in contract.ENGINE_ORDER
            ]
        record = observation_record(asset, classification, runs)
        records.append(record)
        stats[record["status"]] += 1
        stats["records"] += 1
        stats["engine_cache_hits"] += sum(
            run["provenance"]["cache_hit"] is True for run in runs
        )
        atomic_write_jsonl(output_path, records)
        engine_state = ",".join(
            f"{run['engine']['name']}={run['status']}"
            + ("(cache)" if run["provenance"]["cache_hit"] else "")
            for run in runs
        )
        print(
            f"[{position}/{len(eligible)}] {asset['asset_id']} "
            f"{record['status']} {engine_state}",
            flush=True,
        )
    return stats


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Extract image-bound, question-independent Apple Vision and Tesseract "
            "OCR observations for assets routed to ocr_text."
        )
    )
    parser.add_argument("--assets", type=Path, required=True)
    parser.add_argument("--classifications", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--asset-root", type=Path,
        help="root containing materialized images (default: --assets parent)",
    )
    parser.add_argument("--cache-dir", type=Path)
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    parser.add_argument("--restart", action="store_true", help="ignore OCR run caches")
    parser.add_argument("--max-assets", type=int)
    args = parser.parse_args()
    try:
        stats = extract_file(
            args.assets,
            args.classifications,
            args.output,
            asset_root=args.asset_root,
            cache_dir=args.cache_dir,
            timeout=args.timeout,
            restart=args.restart,
            max_assets=args.max_assets,
        )
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(contract.canonical_json(stats))
    return 1 if stats["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
