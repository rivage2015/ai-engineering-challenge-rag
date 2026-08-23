#!/usr/bin/env python3
"""Run PP-OCRv6 medium Japanese over the closed OCR PoC fixtures.

This runner deliberately lives outside the production OCR adapters.  PaddleOCR
is imported only after the cache/download preflight so a missing model can
never trigger an implicit network request without ``--allow-model-download``.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import io
import math
import os
import platform
import sys
import time
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import ocr_poc_adapters as adapters  # noqa: E402
import ocr_poc_contract as contract  # noqa: E402
import run_ocr_poc as common_runner  # noqa: E402


ENGINE_NAME = "paddleocr_ppocrv6_medium_japan"
ENGINE_VERSION = "PP-OCRv6 medium / PaddleOCR 3.7.0"
DETECTION_MODEL = "PP-OCRv6_medium_det"
RECOGNITION_MODEL = "PP-OCRv6_medium_rec"
PACKAGE_NAMES = ("paddlepaddle", "paddleocr", "paddlex")
DEFAULT_CACHE_ROOT = Path("/private/tmp/aiec-ocr-poc-20260817/paddlex-cache")
DEFAULT_FIXTURES = ROOT / "artifacts" / "ocr-poc-v0.1" / "manifest.verified.jsonl"
DEFAULT_OUTPUT = ROOT / "artifacts" / "ocr-poc-v0.1" / "paddle-runs.jsonl"

RUNTIME_SETTINGS: dict[str, Any] = {
    "language": "japan",
    "ocr_version": "PP-OCRv6",
    "text_detection_model": DETECTION_MODEL,
    "text_recognition_model": RECOGNITION_MODEL,
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


def _json_ready(value: Any) -> Any:
    """Convert PaddleX configuration containers without changing values."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _json_ready(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_ready(item) for item in value]
    item = getattr(value, "item", None)
    if callable(item):
        scalar = item()
        if scalar is not value:
            return _json_ready(scalar)
    raise TypeError(f"unsupported value in PaddleOCR configuration: {type(value)!r}")


def _hash_manifest(entries: Iterable[tuple[str, Path]]) -> dict[str, Any]:
    """Hash a named set of regular files using an explicit manifest format."""
    digest = hashlib.sha256()
    count = 0
    total_bytes = 0
    for relative, path in sorted(entries, key=lambda item: item[0]):
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"fingerprinted entry must be a regular non-symlink file: {path}")
        size = path.stat().st_size
        sha256 = contract.sha256_file(path)
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(size).encode("ascii"))
        digest.update(b"\0")
        digest.update(sha256.encode("ascii"))
        digest.update(b"\n")
        count += 1
        total_bytes += size
    if not count:
        raise ValueError("cannot fingerprint an empty file manifest")
    return {
        "algorithm": "sha256(relative_path\\0size\\0file_sha256\\n)",
        "file_count": count,
        "total_bytes": total_bytes,
        "manifest_sha256": digest.hexdigest(),
    }


def directory_fingerprint(root: Path) -> dict[str, Any]:
    if root.is_symlink():
        raise ValueError(f"model directory must not be a symlink: {root}")
    root = root.resolve(strict=True)
    if not root.is_dir():
        raise ValueError(f"model directory must be a real directory: {root}")
    entries: list[tuple[str, Path]] = []
    for path in root.rglob("*"):
        relative = path.relative_to(root)
        # Paddle may create runtime optimization caches under the immutable
        # official model directory.  Those are runtime artifacts, not weights.
        if ".cache" in relative.parts or "__pycache__" in relative.parts:
            continue
        if path.is_symlink():
            raise ValueError(f"model directory contains a symlink: {path}")
        if path.is_file():
            entries.append((relative.as_posix(), path))
    value = _hash_manifest(entries)
    return value


def stable_pipeline_config(
    value: Any, *, model_paths: Mapping[Path, str]
) -> Any:
    """Replace model locations with stable names before identity hashing.

    Model bytes are independently fingerprinted, so an absolute cache location
    is execution metadata rather than engine identity.  Rejecting unrecognized
    absolute ``model_dir`` values prevents a host path from silently entering
    the supposedly portable fingerprint.
    """
    approved_names = {DETECTION_MODEL, RECOGNITION_MODEL}
    path_to_name: dict[Path, str] = {}
    name_to_path: dict[str, Path] = {}
    for raw_path, model_name in model_paths.items():
        if model_name not in approved_names:
            raise ValueError(f"unapproved PaddleOCR model_name: {model_name!r}")
        path = Path(raw_path)
        if path.is_symlink():
            raise ValueError(f"approved PaddleOCR model root must not be a symlink: {path}")
        path = path.resolve(strict=True)
        if not path.is_dir():
            raise ValueError(f"approved PaddleOCR model root is not a directory: {path}")
        if path in path_to_name:
            raise ValueError(f"duplicate resolved PaddleOCR model path: {path}")
        if model_name in name_to_path:
            raise ValueError(f"duplicate approved PaddleOCR model_name: {model_name}")
        path_to_name[path] = model_name
        name_to_path[model_name] = path
    missing_names = approved_names - name_to_path.keys()
    if missing_names:
        raise ValueError(
            "approved PaddleOCR model mapping is incomplete: "
            + ", ".join(sorted(missing_names))
        )

    def convert(item: Any) -> Any:
        if isinstance(item, Mapping):
            model_name = item.get("model_name")
            converted: dict[str, Any] = {}
            for raw_key, raw_value in item.items():
                key = str(raw_key)
                if key.lower().endswith("model_dir"):
                    if raw_value is None and model_name not in name_to_path:
                        # Disabled/default submodels with no local files do not
                        # enter the identity core and need no tokenization.
                        converted[key] = None
                    else:
                        if not isinstance(model_name, str) or model_name not in name_to_path:
                            raise ValueError(
                                f"unapproved PaddleOCR model_name for model_dir: {model_name!r}"
                            )
                        if raw_value is not None:
                            if not isinstance(raw_value, (str, Path)):
                                raise TypeError(
                                    "unsupported PaddleOCR model_dir value: "
                                    f"{type(raw_value)!r}"
                                )
                            raw_path = Path(raw_value)
                            if raw_path.is_symlink():
                                raise ValueError(
                                    f"PaddleOCR model_dir must not be a symlink: {raw_path}"
                                )
                            resolved = raw_path.resolve(strict=False)
                            expected = name_to_path[model_name]
                            if resolved != expected:
                                actual_name = path_to_name.get(resolved)
                                if actual_name is not None:
                                    raise ValueError(
                                        "PaddleOCR model_dir/model_name mismatch: "
                                        f"name={model_name!r} path belongs to {actual_name!r}"
                                    )
                                raise ValueError(
                                    "unapproved PaddleOCR model_dir for "
                                    f"{model_name!r}: {raw_value}"
                                )
                        converted[key] = f"model://{model_name}"
                else:
                    converted[key] = convert(raw_value)
            return converted
        if isinstance(item, (list, tuple)):
            return [convert(child) for child in item]
        return _json_ready(item)

    return convert(value)


def distribution_fingerprint(name: str) -> dict[str, Any]:
    distribution = importlib.metadata.distribution(name)
    files = distribution.files
    if files is None:
        raise ValueError(f"installed distribution has no RECORD file list: {name}")
    entries: list[tuple[str, Path]] = []
    for item in files:
        path = Path(distribution.locate_file(item)).resolve(strict=True)
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"distribution entry is not a regular file: {path}")
        entries.append((str(item), path))
    value = _hash_manifest(entries)
    value.update({"distribution": name, "version": distribution.version})
    return value


def _model_dirs(cache_root: Path) -> tuple[Path, Path]:
    official = cache_root / "official_models"
    return official / DETECTION_MODEL, official / RECOGNITION_MODEL


def require_models_or_download_permission(
    cache_root: Path, *, allow_model_download: bool
) -> tuple[Optional[Path], Optional[Path]]:
    detection_dir, recognition_dir = _model_dirs(cache_root)
    missing = [
        str(path)
        for path in (detection_dir, recognition_dir)
        if not path.is_dir()
    ]
    if missing and not allow_model_download:
        raise ValueError(
            "PP-OCRv6 model download is required but was not authorized. "
            "Missing: "
            + ", ".join(missing)
            + ". Re-run with --allow-model-download only after network approval."
        )
    if missing:
        return None, None
    return detection_dir, recognition_dir


def _numeric_sequence(value: Any, label: str) -> list[Any]:
    if hasattr(value, "tolist"):
        value = value.tolist()
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"PaddleOCR {label} must be a sequence")
    return list(value)


def _pixel_bbox_to_normalized(box: Any, width: int, height: int) -> list[int]:
    values = _numeric_sequence(box, "rec_box")
    if len(values) != 4:
        raise ValueError(f"PaddleOCR rec_box must have four coordinates: {values!r}")
    if any(isinstance(item, bool) or not isinstance(item, (int, float)) for item in values):
        raise ValueError(f"PaddleOCR rec_box must be numeric: {values!r}")
    left, top, right, bottom = (float(item) for item in values)
    if not all(math.isfinite(item) for item in (left, top, right, bottom)):
        raise ValueError(f"PaddleOCR rec_box contains a non-finite value: {values!r}")
    if left < 0 or top < 0 or right <= left or bottom <= top:
        raise ValueError(f"PaddleOCR rec_box is invalid: {values!r}")
    if right > width or bottom > height:
        raise ValueError(
            f"PaddleOCR rec_box exceeds crop dimensions {width}x{height}: {values!r}"
        )
    return _quantized_bbox(left, top, right, bottom, width, height)


def _quantized_bbox(
    left: float,
    top: float,
    right: float,
    bottom: float,
    image_width: int,
    image_height: int,
) -> list[int]:
    x = max(0, min(999, math.floor(left * 1000 / image_width)))
    y = max(0, min(999, math.floor(top * 1000 / image_height)))
    normalized_right = max(
        x + 1, min(1000, math.ceil(right * 1000 / image_width))
    )
    normalized_bottom = max(
        y + 1, min(1000, math.ceil(bottom * 1000 / image_height))
    )
    return [x, y, normalized_right - x, normalized_bottom - y]


def paddle_result_to_lines(
    result: Mapping[str, Any], *, width_px: int, height_px: int
) -> list[dict[str, Any]]:
    """Map Paddle's raw result order to the closed run schema without sorting."""
    texts = _numeric_sequence(result.get("rec_texts"), "rec_texts")
    scores = _numeric_sequence(result.get("rec_scores"), "rec_scores")
    boxes = _numeric_sequence(result.get("rec_boxes"), "rec_boxes")
    if not (len(texts) == len(scores) == len(boxes)):
        raise ValueError(
            "PaddleOCR output length mismatch: "
            f"texts={len(texts)} scores={len(scores)} boxes={len(boxes)}"
        )
    lines: list[dict[str, Any]] = []
    for index, (raw_text, raw_score, raw_box) in enumerate(
        zip(texts, scores, boxes), 1
    ):
        if not isinstance(raw_text, str) or raw_text == "":
            raise ValueError(f"PaddleOCR rec_texts[{index - 1}] is not a non-empty string")
        if isinstance(raw_score, bool) or not isinstance(raw_score, (int, float)):
            item = getattr(raw_score, "item", None)
            if not callable(item):
                raise ValueError(f"PaddleOCR rec_scores[{index - 1}] is not numeric")
            raw_score = item()
        confidence = float(raw_score)
        if not math.isfinite(confidence) or not 0 <= confidence <= 1:
            raise ValueError(
                f"PaddleOCR rec_scores[{index - 1}] is outside [0, 1]: {confidence}"
            )
        lines.append(
            {
                "line_id": f"line_{index}",
                "sequence": index,
                "raw_text": raw_text,
                "bbox": _pixel_bbox_to_normalized(
                    raw_box, width_px, height_px
                ),
                "confidence": confidence,
            }
        )
    return lines


class PaddleOCRAdapter:
    name = ENGINE_NAME

    def __init__(
        self,
        *,
        cache_root: Path,
        allow_model_download: bool,
    ) -> None:
        self.cache_root = cache_root.resolve()
        self.allow_model_download = allow_model_download
        self.pipeline: Optional[Any] = None
        self._fingerprint: Optional[dict[str, Any]] = None

    def _prepare(self) -> float:
        if self.pipeline is not None:
            return 0.0
        started = time.monotonic_ns()
        detection_dir, recognition_dir = require_models_or_download_permission(
            self.cache_root, allow_model_download=self.allow_model_download
        )
        os.environ["PADDLE_PDX_CACHE_HOME"] = str(self.cache_root)
        from paddleocr import PaddleOCR

        kwargs = {
            "lang": "japan",
            "ocr_version": "PP-OCRv6",
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
        if detection_dir is not None and recognition_dir is not None:
            # Explicit directories prevent even connectivity checks when a
            # verified local cache is available.
            kwargs.update(
                {
                    "text_detection_model_name": DETECTION_MODEL,
                    "text_detection_model_dir": str(detection_dir),
                    "text_recognition_model_name": RECOGNITION_MODEL,
                    "text_recognition_model_dir": str(recognition_dir),
                    "lang": None,
                    "ocr_version": None,
                }
            )
        pipeline = PaddleOCR(**kwargs)
        inner = pipeline.paddlex_pipeline
        actual_detection_dir = Path(inner.text_det_model.model_dir)
        actual_recognition_dir = Path(inner.text_rec_model.model_dir)
        if inner.text_det_model.model_name != DETECTION_MODEL:
            raise ValueError(
                "unexpected PaddleOCR detection model: "
                f"{inner.text_det_model.model_name!r}"
            )
        if inner.text_rec_model.model_name != RECOGNITION_MODEL:
            raise ValueError(
                "unexpected PaddleOCR recognition model: "
                f"{inner.text_rec_model.model_name!r}"
            )
        if inner.device != "cpu" or inner.engine != "paddle_static":
            raise ValueError(
                f"unexpected PaddleOCR runtime: device={inner.device!r} engine={inner.engine!r}"
            )
        models = {
            "text_detection": {
                "name": DETECTION_MODEL,
                **directory_fingerprint(actual_detection_dir),
            },
            "text_recognition": {
                "name": RECOGNITION_MODEL,
                **directory_fingerprint(actual_recognition_dir),
            },
        }
        resolved_config = stable_pipeline_config(
            pipeline._merged_paddlex_config,
            model_paths={
                actual_detection_dir: DETECTION_MODEL,
                actual_recognition_dir: RECOGNITION_MODEL,
            },
        )
        packages = {
            name: distribution_fingerprint(name) for name in PACKAGE_NAMES
        }
        runtime = {
            "settings": RUNTIME_SETTINGS,
            "resolved_pipeline_config_sha256": contract.sha256_json(resolved_config),
            "packages": packages,
            "models": models,
            "python": platform.python_version(),
            "platform": platform.platform(),
            "machine": platform.machine(),
            "pipeline_class": f"{type(inner).__module__}.{type(inner).__qualname__}",
            "text_detection_predictor_class": (
                f"{type(inner.text_det_model).__module__}."
                f"{type(inner.text_det_model).__qualname__}"
            ),
            "text_recognition_predictor_class": (
                f"{type(inner.text_rec_model).__module__}."
                f"{type(inner.text_rec_model).__qualname__}"
            ),
        }
        fingerprint_sha256 = contract.sha256_json(
            {"name": self.name, "version": ENGINE_VERSION, "runtime": runtime}
        )
        self.pipeline = pipeline
        self._fingerprint = {
            "name": self.name,
            "version": ENGINE_VERSION,
            "fingerprint_sha256": fingerprint_sha256,
            "runtime": runtime,
        }
        return (time.monotonic_ns() - started) / 1_000_000

    def fingerprint(self) -> dict[str, Any]:
        if self._fingerprint is None:
            raise RuntimeError("PaddleOCR adapter fingerprint requested before setup")
        return self._fingerprint

    def run(
        self, value: adapters.OCRInput, *, timeout: float
    ) -> adapters.AdapterResult:
        setup_ms = self._prepare()
        from PIL import Image
        import numpy as np

        with Image.open(io.BytesIO(value.image_bytes)) as image:
            image.load()
            array = np.asarray(image.convert("RGB"))
        started = time.monotonic_ns()
        try:
            results = self.pipeline.predict(array)
            inference_ms = (time.monotonic_ns() - started) / 1_000_000
            if inference_ms > timeout * 1000:
                return adapters.AdapterResult(
                    status="timeout",
                    lines=[],
                    warnings=[],
                    error=(
                        "PaddleOCR inference exceeded the post-return timeout "
                        f"limit: {inference_ms / 1000:.3f}s > {timeout:.3f}s"
                    ),
                    setup_ms=setup_ms,
                    inference_ms=inference_ms,
                )
            if len(results) != 1:
                raise ValueError(
                    f"PaddleOCR returned {len(results)} results for one crop"
                )
            lines = paddle_result_to_lines(
                results[0], width_px=value.width_px, height_px=value.height_px
            )
            warnings: list[str] = []
            status = "completed"
            if not lines:
                status = "needs_review"
                warnings.append("PaddleOCR returned no OCR lines")
            return adapters.AdapterResult(
                status=status,
                lines=lines,
                warnings=warnings,
                error=None,
                setup_ms=setup_ms,
                inference_ms=inference_ms,
            )
        except Exception as exc:
            inference_ms = (time.monotonic_ns() - started) / 1_000_000
            return adapters.AdapterResult(
                status="failed",
                lines=[],
                warnings=[],
                error=f"{type(exc).__name__}: {exc}",
                setup_ms=setup_ms,
                inference_ms=inference_ms,
            )


def _load_verified_fixtures(
    fixtures_path: Path, repository_root: Path
) -> list[dict[str, Any]]:
    fixtures = contract.load_jsonl(fixtures_path)
    seen: set[str] = set()
    for position, fixture in enumerate(fixtures, 1):
        errors = contract.validate_fixture(
            fixture, repository_root=repository_root, require_verified=True
        )
        if errors:
            raise ValueError(f"fixture {position} is invalid: " + "; ".join(errors))
        fixture_id = fixture["fixture_id"]
        if fixture_id in seen:
            raise ValueError(f"duplicate fixture_id: {fixture_id}")
        seen.add(fixture_id)
    return fixtures


def run_manifest(
    fixtures_path: Path,
    output_path: Path,
    *,
    repository_root: Path,
    cache_root: Path,
    allow_model_download: bool,
    timeout: float,
    overwrite: bool,
) -> list[dict[str, Any]]:
    if output_path.exists() and not overwrite:
        raise ValueError(f"output already exists; pass --overwrite to replace it: {output_path}")
    fixtures = _load_verified_fixtures(fixtures_path, repository_root)
    adapter = PaddleOCRAdapter(
        cache_root=cache_root,
        allow_model_download=allow_model_download,
    )
    runs: list[dict[str, Any]] = []
    for position, fixture in enumerate(fixtures, 1):
        value = adapters.crop_input(fixture, repository_root)
        result = adapter.run(value, timeout=timeout)
        record = common_runner.make_run(fixture, adapter, value, result)
        runs.append(record)
        if position == 1:
            print(
                "smoke fixture: "
                f"{fixture['fixture_id']} status={record['status']} "
                f"lines={len(record['lines'])} inference_ms={record['timing']['inference_ms']}",
                file=sys.stderr,
            )
    contract.write_jsonl(output_path, runs)
    return runs


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("--fixtures", type=Path, default=DEFAULT_FIXTURES)
    value.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    value.add_argument("--repository-root", type=Path, default=ROOT)
    value.add_argument("--cache-root", type=Path, default=DEFAULT_CACHE_ROOT)
    value.add_argument(
        "--allow-model-download",
        action="store_true",
        help="authorize PaddleX to download missing official model files",
    )
    value.add_argument("--timeout", type=float, default=180.0)
    value.add_argument("--overwrite", action="store_true")
    return value


def main() -> int:
    args = parser().parse_args()
    if args.timeout <= 0:
        print("error: --timeout must be positive", file=sys.stderr)
        return 2
    try:
        runs = run_manifest(
            args.fixtures,
            args.output,
            repository_root=args.repository_root,
            cache_root=args.cache_root,
            allow_model_download=args.allow_model_download,
            timeout=args.timeout,
            overwrite=args.overwrite,
        )
    except (OSError, ValueError, RuntimeError, TypeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    status_counts: dict[str, int] = {}
    for run in runs:
        status_counts[run["status"]] = status_counts.get(run["status"], 0) + 1
    output_sha256 = contract.sha256_file(args.output)
    inference_values = [run["timing"]["inference_ms"] for run in runs]
    print(f"wrote {len(runs)} PaddleOCR PoC runs to {args.output}")
    print(
        "statuses: "
        + ", ".join(
            f"{key}={count}" for key, count in sorted(status_counts.items())
        )
    )
    print(f"output_sha256: {output_sha256}")
    print(f"total_inference_ms: {sum(inference_values):.6f}")
    print(f"mean_inference_ms: {sum(inference_values) / len(inference_values):.6f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
