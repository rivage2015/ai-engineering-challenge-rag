#!/usr/bin/env python3
"""Run one image through a hash-locked, fully local PaddleOCR worker.

The worker has no model-download mode.  It accepts only the previously
evaluated PP-OCRv6 Japanese model files and blocks Python Internet sockets
before importing PaddleOCR, Pillow, or NumPy.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata as metadata
import json
import math
import os
import platform
import re
import socket
import stat
import sys
import tempfile
import time
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


SCHEMA_VERSION = "0.1"
RUNNER = "aiec-local-paddle-ocr"
RUNNER_VERSION = "0.1"
ENGINE_NAME = "paddleocr_ppocrv6_medium_japan"
ENGINE_VERSION = "PP-OCRv6 medium / PaddleOCR 3.7.0"
ENGINE_PASS = "paddleocr_primary"
INDEPENDENCE_GROUP = "paddleocr"
DETECTION_MODEL = "PP-OCRv6_medium_det"
RECOGNITION_MODEL = "PP-OCRv6_medium_rec"
PACKAGE_VERSIONS = {
    "paddlepaddle": "3.3.0",
    "paddleocr": "3.7.0",
    "paddlex": "3.7.0",
}
RUNTIME_LOCK_SHA256 = (
    "d20aaf7219335bbe016ef7232b3cfd56d409558cd291bfb6b869dd2d4aa8500e"
)
MODEL_CONTRACTS = {
    DETECTION_MODEL: {
        "file_count": 5,
        "total_bytes": 62_298_334,
        "manifest_sha256": (
            "fa0db359feda0ef4ac2cde281d1581cdfca6d64147e78150fdef42d955678081"
        ),
    },
    RECOGNITION_MODEL: {
        "file_count": 5,
        "total_bytes": 76_862_530,
        "manifest_sha256": (
            "afcfe045967e34462496a245242e05ed1067ec05fd5726093acb1af764f7624b"
        ),
    },
}
OFFLINE_ENVIRONMENT = {
    "HF_HUB_OFFLINE": "1",
    "TRANSFORMERS_OFFLINE": "1",
    "HF_DATASETS_OFFLINE": "1",
    "PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK": "1",
}
RUNTIME_SETTINGS: dict[str, Any] = {
    "device": "cpu",
    "engine": "paddle_static",
    "use_doc_orientation_classify": False,
    "use_doc_unwarping": False,
    "use_textline_orientation": False,
    "text_rec_score_thresh": 0.0,
    "return_word_box": False,
    "enable_hpi": False,
    "use_tensorrt": False,
    "precision": "fp32",
    "enable_mkldnn": True,
    "mkldnn_cache_capacity": 10,
    "cpu_threads": 10,
    "enable_cinn": False,
}
MAX_INPUT_BYTES = 200 * 1024 * 1024
MAX_IMAGE_PIXELS = 50_000_000
MAX_OUTPUT_BYTES = 8 * 1024 * 1024
MAX_RESULT_LINES = 100_000
MAX_LINE_TEXT_CHARS = 32_000
MAX_ERROR_CHARS = 1_000


class OfflineNetworkError(RuntimeError):
    """Raised when a dependency attempts an Internet socket."""


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _require_python_312() -> None:
    if sys.version_info[:2] != (3, 12):
        raise RuntimeError("local PaddleOCR worker requires Python 3.12")


def _require_regular_file(path: Path, label: str) -> Path:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"{label} must be a regular non-symlink file")
    mode = path.stat().st_mode
    if not stat.S_ISREG(mode):
        raise ValueError(f"{label} must be a regular non-symlink file")
    return path.resolve(strict=True)


def _require_real_directory(path: Path, label: str) -> Path:
    if path.is_symlink() or not path.is_dir():
        raise ValueError(f"{label} must be a real non-symlink directory")
    mode = path.stat().st_mode
    if not stat.S_ISDIR(mode):
        raise ValueError(f"{label} must be a real non-symlink directory")
    return path.resolve(strict=True)


def validate_paths(
    input_path: Path,
    output_path: Path,
    model_root: Path,
) -> tuple[Path, Path, Path]:
    source = _require_regular_file(input_path, "input")
    source_size = source.stat().st_size
    if source_size <= 0 or source_size > MAX_INPUT_BYTES:
        raise ValueError("input size is outside the supported range")

    root = _require_real_directory(model_root, "model root")
    parent = _require_real_directory(output_path.parent, "output parent")
    destination = parent / output_path.name
    if not output_path.name or output_path.name in {".", ".."}:
        raise ValueError("output name is invalid")
    if destination.exists() or destination.is_symlink():
        raise ValueError("output must not already exist")
    if destination == source:
        raise ValueError("output must differ from input")
    try:
        destination.relative_to(root)
    except ValueError:
        pass
    else:
        raise ValueError("output must be outside the model root")
    return source, destination, root


def _manifest_entries(root: Path) -> list[tuple[str, Path]]:
    entries: list[tuple[str, Path]] = []
    for path in root.rglob("*"):
        if path.is_symlink():
            raise ValueError("model directory must not contain symlinks")
        relative = path.relative_to(root)
        if ".cache" in relative.parts or "__pycache__" in relative.parts:
            continue
        if path.is_file():
            entries.append((relative.as_posix(), path))
        elif not path.is_dir():
            raise ValueError("model directory contains an unsupported file type")
    return sorted(entries, key=lambda item: item[0])


def directory_fingerprint(root: Path) -> dict[str, Any]:
    root = _require_real_directory(root, "model directory")
    digest = hashlib.sha256()
    file_count = 0
    total_bytes = 0
    for relative, path in _manifest_entries(root):
        size = path.stat().st_size
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(size).encode("ascii"))
        digest.update(b"\0")
        digest.update(sha256_file(path).encode("ascii"))
        digest.update(b"\n")
        file_count += 1
        total_bytes += size
    if file_count == 0:
        raise ValueError("model directory is empty")
    return {
        "algorithm": "sha256(relative_path\\0size\\0file_sha256\\n)",
        "file_count": file_count,
        "total_bytes": total_bytes,
        "manifest_sha256": digest.hexdigest(),
    }


def verify_models(model_root: Path) -> tuple[dict[str, Path], dict[str, Any]]:
    official = _require_real_directory(
        model_root / "official_models", "official model directory"
    )
    paths: dict[str, Path] = {}
    fingerprints: dict[str, Any] = {}
    for name in (DETECTION_MODEL, RECOGNITION_MODEL):
        model_path = _require_real_directory(official / name, f"{name} directory")
        fingerprint = directory_fingerprint(model_path)
        if fingerprint != {
            "algorithm": "sha256(relative_path\\0size\\0file_sha256\\n)",
            **MODEL_CONTRACTS[name],
        }:
            raise ValueError(f"{name} does not match the approved model manifest")
        paths[name] = model_path
        fingerprints[name] = {"name": name, **fingerprint}
    return paths, fingerprints


def verify_packages() -> dict[str, dict[str, str]]:
    packages: dict[str, dict[str, str]] = {}
    for name, expected in PACKAGE_VERSIONS.items():
        try:
            actual = metadata.version(name)
        except metadata.PackageNotFoundError as exc:
            raise RuntimeError(f"required package is not installed: {name}") from exc
        if actual != expected:
            raise RuntimeError(
                f"unsupported {name} version: expected {expected}, found {actual}"
            )
        packages[name] = {"version": actual}
    return packages


def _normalized_package_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def verify_runtime_lock(path: Path) -> dict[str, Any]:
    """Require the entire isolated environment to match the reviewed lock."""
    lock_path = _require_regular_file(path, "runtime lock")
    if sha256_file(lock_path) != RUNTIME_LOCK_SHA256:
        raise ValueError("PaddleOCR runtime lock hash does not match the approved lock")
    try:
        raw_lines = lock_path.read_text(encoding="utf-8").splitlines()
    except UnicodeDecodeError as exc:
        raise ValueError("PaddleOCR runtime lock is not UTF-8") from exc
    expected: dict[str, str] = {}
    for raw_line in raw_lines:
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.count("==") != 1:
            raise ValueError("PaddleOCR runtime lock contains an unsupported entry")
        name, version = line.split("==", 1)
        normalized_name = _normalized_package_name(name)
        if not normalized_name or not version or normalized_name in expected:
            raise ValueError("PaddleOCR runtime lock contains an invalid entry")
        expected[normalized_name] = version
    actual: dict[str, str] = {}
    for distribution in metadata.distributions():
        name = distribution.metadata.get("Name")
        if not isinstance(name, str) or not name:
            raise RuntimeError("installed package metadata has no distribution name")
        normalized_name = _normalized_package_name(name)
        if normalized_name in actual:
            raise RuntimeError("installed package metadata contains duplicate names")
        actual[normalized_name] = distribution.version
    if actual != expected:
        missing = sorted(set(expected) - set(actual))
        extra = sorted(set(actual) - set(expected))
        changed = sorted(
            name
            for name in set(actual) & set(expected)
            if actual[name] != expected[name]
        )
        detail = f"missing={missing[:5]}, extra={extra[:5]}, changed={changed[:5]}"
        raise RuntimeError(f"installed PaddleOCR environment differs from lock: {detail}")
    return {
        "sha256": RUNTIME_LOCK_SHA256,
        "package_count": len(expected),
        "fully_matched": True,
    }


def configure_offline_environment(model_root: Path) -> dict[str, str]:
    values = {
        **OFFLINE_ENVIRONMENT,
        "PADDLE_PDX_CACHE_HOME": str(model_root),
    }
    for key, value in values.items():
        os.environ[key] = value
    return values


@contextmanager
def offline_socket_guard() -> Iterator[None]:
    """Deny Python IPv4/IPv6 sockets while allowing local Unix sockets."""
    original_socket = socket.socket
    original_create_connection = socket.create_connection

    class GuardedSocket(original_socket):
        def __init__(
            self,
            family: int = socket.AF_INET,
            type: int = socket.SOCK_STREAM,
            proto: int = 0,
            fileno: int | None = None,
        ) -> None:
            if family in {socket.AF_INET, socket.AF_INET6}:
                raise OfflineNetworkError(
                    "AF_INET and AF_INET6 sockets are disabled in offline OCR"
                )
            super().__init__(family, type, proto, fileno)

    def denied_connection(*_args: Any, **_kwargs: Any) -> Any:
        raise OfflineNetworkError(
            "Internet connections are disabled in offline OCR"
        )

    socket.socket = GuardedSocket
    socket.create_connection = denied_connection
    try:
        yield
    finally:
        socket.socket = original_socket
        socket.create_connection = original_create_connection


def _numeric_sequence(value: Any, label: str) -> list[Any]:
    if hasattr(value, "tolist"):
        value = value.tolist()
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ValueError(f"PaddleOCR {label} must be a sequence")
    return list(value)


def _pixel_bbox_to_normalized(
    box: Any,
    width_px: int,
    height_px: int,
) -> list[int]:
    values = _numeric_sequence(box, "rec_box")
    if len(values) != 4:
        raise ValueError("PaddleOCR rec_box must have four coordinates")
    if any(
        isinstance(item, bool) or not isinstance(item, (int, float))
        for item in values
    ):
        raise ValueError("PaddleOCR rec_box must be numeric")
    left, top, right, bottom = (float(item) for item in values)
    if not all(math.isfinite(item) for item in (left, top, right, bottom)):
        raise ValueError("PaddleOCR rec_box contains a non-finite value")
    if left < 0 or top < 0 or right <= left or bottom <= top:
        raise ValueError("PaddleOCR rec_box is invalid")
    if right > width_px or bottom > height_px:
        raise ValueError("PaddleOCR rec_box exceeds the input dimensions")
    x = max(0, min(999, math.floor(left * 1000 / width_px)))
    y = max(0, min(999, math.floor(top * 1000 / height_px)))
    normalized_right = max(
        x + 1, min(1000, math.ceil(right * 1000 / width_px))
    )
    normalized_bottom = max(
        y + 1, min(1000, math.ceil(bottom * 1000 / height_px))
    )
    return [x, y, normalized_right - x, normalized_bottom - y]


def paddle_result_to_lines(
    result: Mapping[str, Any],
    *,
    width_px: int,
    height_px: int,
) -> list[dict[str, Any]]:
    texts = _numeric_sequence(result.get("rec_texts"), "rec_texts")
    scores = _numeric_sequence(result.get("rec_scores"), "rec_scores")
    boxes = _numeric_sequence(result.get("rec_boxes"), "rec_boxes")
    if not (len(texts) == len(scores) == len(boxes)):
        raise ValueError("PaddleOCR output length mismatch")
    if len(texts) > MAX_RESULT_LINES:
        raise ValueError("PaddleOCR output contains too many lines")

    lines: list[dict[str, Any]] = []
    for index, (raw_text, raw_score, raw_box) in enumerate(
        zip(texts, scores, boxes), 1
    ):
        if not isinstance(raw_text, str) or len(raw_text) > MAX_LINE_TEXT_CHARS:
            raise ValueError("PaddleOCR returned an invalid line string")
        # Detection can legitimately emit an empty recognition candidate on a
        # natural photograph. It is not a text observation. Keep its source
        # index separately while returning a dense transport sequence.
        if not raw_text.strip():
            continue
        if isinstance(raw_score, bool) or not isinstance(raw_score, (int, float)):
            item = getattr(raw_score, "item", None)
            if not callable(item):
                raise ValueError("PaddleOCR confidence must be numeric")
            raw_score = item()
        confidence = float(raw_score)
        if not math.isfinite(confidence) or not 0 <= confidence <= 1:
            raise ValueError("PaddleOCR confidence is outside [0, 1]")
        lines.append(
            {
                "line_id": f"line_{index}",
                "sequence": len(lines) + 1,
                "source_sequence": index,
                "raw_text": raw_text,
                "bbox": _pixel_bbox_to_normalized(
                    raw_box,
                    width_px,
                    height_px,
                ),
                "confidence": confidence,
            }
        )
    return lines


def _pipeline_kwargs(model_paths: Mapping[str, Path]) -> dict[str, Any]:
    return {
        "text_detection_model_name": DETECTION_MODEL,
        "text_detection_model_dir": str(model_paths[DETECTION_MODEL]),
        "text_recognition_model_name": RECOGNITION_MODEL,
        "text_recognition_model_dir": str(model_paths[RECOGNITION_MODEL]),
        "lang": None,
        "ocr_version": None,
        **RUNTIME_SETTINGS,
    }


def _verify_pipeline(pipeline: Any, model_paths: Mapping[str, Path]) -> Any:
    inner = getattr(pipeline, "paddlex_pipeline", None)
    if inner is None:
        raise RuntimeError("PaddleOCR pipeline internals are unavailable")
    detection = getattr(inner, "text_det_model", None)
    recognition = getattr(inner, "text_rec_model", None)
    if detection is None or recognition is None:
        raise RuntimeError("PaddleOCR text models are unavailable")
    if getattr(detection, "model_name", None) != DETECTION_MODEL:
        raise RuntimeError("PaddleOCR detection model identity changed")
    if getattr(recognition, "model_name", None) != RECOGNITION_MODEL:
        raise RuntimeError("PaddleOCR recognition model identity changed")
    actual_detection = Path(str(getattr(detection, "model_dir", ""))).resolve(
        strict=True
    )
    actual_recognition = Path(str(getattr(recognition, "model_dir", ""))).resolve(
        strict=True
    )
    if actual_detection != model_paths[DETECTION_MODEL]:
        raise RuntimeError("PaddleOCR detection model path changed")
    if actual_recognition != model_paths[RECOGNITION_MODEL]:
        raise RuntimeError("PaddleOCR recognition model path changed")
    if getattr(inner, "device", None) != "cpu":
        raise RuntimeError("PaddleOCR did not select the approved CPU device")
    if getattr(inner, "engine", None) != "paddle_static":
        raise RuntimeError("PaddleOCR did not select the approved static engine")
    return inner


def run_worker(
    input_path: Path,
    model_root: Path,
    runtime_lock: Path,
) -> dict[str, Any]:
    _require_python_312()
    model_paths, model_metadata = verify_models(model_root)
    package_metadata = verify_packages()
    runtime_lock_metadata = verify_runtime_lock(runtime_lock)
    offline_values = configure_offline_environment(model_root)

    setup_started = time.monotonic_ns()
    with offline_socket_guard():
        # These imports must stay after both the environment and socket guards.
        from paddleocr import PaddleOCR
        from PIL import Image
        import numpy as np

        pipeline = PaddleOCR(**_pipeline_kwargs(model_paths))
        inner = _verify_pipeline(pipeline, model_paths)
        setup_ms = (time.monotonic_ns() - setup_started) / 1_000_000

        with Image.open(input_path) as image:
            image.load()
            width_px, height_px = image.size
            if (
                not isinstance(width_px, int)
                or not isinstance(height_px, int)
                or isinstance(width_px, bool)
                or isinstance(height_px, bool)
                or width_px <= 0
                or height_px <= 0
                or width_px * height_px > MAX_IMAGE_PIXELS
            ):
                raise ValueError("input image dimensions are outside the supported range")
            array = np.asarray(image.convert("RGB"))

        inference_started = time.monotonic_ns()
        results = list(pipeline.predict(array))
        inference_ms = (time.monotonic_ns() - inference_started) / 1_000_000
        if len(results) != 1 or not isinstance(results[0], Mapping):
            raise ValueError("PaddleOCR must return exactly one mapping result")
        lines = paddle_result_to_lines(
            results[0],
            width_px=width_px,
            height_px=height_px,
        )
        empty_recognition_count = sum(
            isinstance(value, str) and not value.strip()
            for value in _numeric_sequence(
                results[0].get("rec_texts"), "rec_texts"
            )
        )

    runtime = {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "settings": dict(RUNTIME_SETTINGS),
        "pipeline_class": f"{type(inner).__module__}.{type(inner).__qualname__}",
        "offline_environment": offline_values,
        "network_guard": "python_af_inet_and_af_inet6_denied",
        "model_download_permitted": False,
    }
    engine = {
        "name": ENGINE_NAME,
        "version": ENGINE_VERSION,
        "pass": ENGINE_PASS,
        "independence_group": INDEPENDENCE_GROUP,
        "packages": package_metadata,
        "runtime_lock": runtime_lock_metadata,
        "models": {
            "text_detection": model_metadata[DETECTION_MODEL],
            "text_recognition": model_metadata[RECOGNITION_MODEL],
        },
        "runtime": runtime,
    }
    engine["fingerprint_sha256"] = hashlib.sha256(
        canonical_json(engine).encode("utf-8")
    ).hexdigest()
    return {
        "schema_version": SCHEMA_VERSION,
        "runner": RUNNER,
        "runner_version": RUNNER_VERSION,
        "status": "completed" if lines else "needs_review",
        "input": {
            "sha256": sha256_file(input_path),
            "width_px": width_px,
            "height_px": height_px,
        },
        "engine": engine,
        "lines": lines,
        "warnings": [
            *(
                [f"PaddleOCR omitted {empty_recognition_count} empty recognition candidate(s)"]
                if empty_recognition_count else []
            ),
            *([] if lines else ["PaddleOCR returned no OCR lines"]),
        ],
        "error": None,
        "timing": {
            "setup_ms": round(setup_ms, 6),
            "inference_ms": round(inference_ms, 6),
        },
        "external_network_used": False,
        "downloads_performed": False,
    }


def failed_result(exc: Exception) -> dict[str, Any]:
    message = " ".join(str(exc).split())[:MAX_ERROR_CHARS]
    return {
        "schema_version": SCHEMA_VERSION,
        "runner": RUNNER,
        "runner_version": RUNNER_VERSION,
        "status": "failed",
        "lines": [],
        "warnings": [],
        "error": {
            "type": type(exc).__name__,
            "message": message or "local PaddleOCR worker failed",
        },
        "external_network_used": False,
        "downloads_performed": False,
    }


def write_bounded_json(path: Path, value: Mapping[str, Any]) -> None:
    payload = (canonical_json(value) + "\n").encode("utf-8")
    if len(payload) > MAX_OUTPUT_BYTES:
        raise ValueError("worker JSON exceeds the output size limit")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("--input", required=True, type=Path)
    value.add_argument("--output", required=True, type=Path)
    value.add_argument("--model-root", required=True, type=Path)
    value.add_argument("--runtime-lock", required=True, type=Path)
    return value


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        input_path, output_path, model_root = validate_paths(
            args.input,
            args.output,
            args.model_root,
        )
    except Exception as exc:
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    try:
        result = run_worker(input_path, model_root, args.runtime_lock)
    except Exception as exc:
        result = failed_result(exc)
    try:
        write_bounded_json(output_path, result)
    except Exception as exc:
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    return 0 if result["status"] in {"completed", "needs_review"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
