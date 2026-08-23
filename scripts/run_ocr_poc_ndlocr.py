#!/usr/bin/env python3
"""Run NDLOCR-Lite 1.2.3 over verified OCR PoC region fixtures.

This is an isolated shadow runner.  It never reads questions, predictions, or
answers.  Crops are materialized under an ASCII-only temporary directory, the
four ONNX sessions are initialized once, and fixtures are then processed in
manifest order through NDLOCR-Lite's persistent in-process API.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import importlib.util
import math
import platform
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import ocr_poc_adapters as adapters  # noqa: E402
import ocr_poc_contract as contract  # noqa: E402
import run_ocr_poc as runner  # noqa: E402


ENGINE_NAME = "ndlocr_lite"
ENGINE_VERSION = "1.2.3"
EXPECTED_COMMIT = "c3cc7676e7f4613c6b5d51bfae7ca764098424d2"
SOURCE_URL = "https://github.com/ndl-lab/ndlocr-lite"
DEFAULT_CHECKOUT = Path("/private/tmp/aiec-ocr-poc-20260817/ndlocr-lite")
DEFAULT_TEMP_ROOT = Path("/private/tmp")
ENTRYPOINT_RELATIVE_PATH = "src/ocr.py"
LICENSE_RELATIVE_PATH = "LICENCE"
DEPENDENCY_LICENSE_MANIFEST_RELATIVE_PATH = "LICENCE_DEPENDENCEIES"
EXECUTION_SOURCE_SUFFIXES = frozenset({".py", ".yaml", ".yml"})
MODEL_RELATIVE_PATHS = (
    "src/model/deim-s-1024x1024.onnx",
    "src/model/parseq-ndl-24x256-30-tiny-189epoch-tegaki3-r8data-202604.onnx",
    "src/model/parseq-ndl-24x384-50-tiny-300epoch-tegaki3-r8data-202604.onnx",
    "src/model/parseq-ndl-24x768-100-tiny-153epoch-tegaki3-r8data-202604.onnx",
)
CONFIG_RELATIVE_PATHS = (
    "src/config/ndl.yaml",
    "src/config/NDLmoji.yaml",
)
CONFIG = {
    "device": "cpu",
    "det_score_threshold": 0.2,
    "det_conf_threshold": 0.25,
    "det_iou_threshold": 0.2,
    "cascade": True,
    "enable_tcy": False,
    "save_viz": False,
    "api": "_run_ocr_on_image_array",
}
LIMITATIONS = {
    "raw_engine_json_sidecar": {
        "implemented": False,
        "scope": "out_of_scope_for_persistent_api_poc_v0.1",
        "required_follow_up": "subprocess_orchestrator_worker",
    },
    "raw_pixel_polygon_sidecar": {
        "implemented": False,
        "scope": "out_of_scope_for_persistent_api_poc_v0.1",
        "required_follow_up": "subprocess_orchestrator_worker",
    },
    "hard_wall_clock_timeout": {
        "implemented": False,
        "scope": "out_of_scope_for_persistent_api_poc_v0.1",
        "required_follow_up": "subprocess_orchestrator_worker",
    },
}
LIMITATION_WARNINGS = (
    "PoC limitation: raw NDLOCR result JSON and original pixel polygons are not persisted as sidecars in v0.1; a subprocess orchestrator worker is required",
    "PoC limitation: persistent in-process inference has no hard wall-clock timeout; a subprocess orchestrator worker is required",
)


def _regular_file(path: Path, label: str) -> Path:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"{label} must be a regular non-symlink file: {path}")
    return path


def _git_text(checkout: Path, arguments: list[str]) -> str:
    completed = subprocess.run(
        ["git", "-C", str(checkout), *arguments],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    return completed.stdout.strip()


def _git_commit(checkout: Path) -> str:
    return _git_text(checkout, ["rev-parse", "HEAD"])


def _git_status(checkout: Path) -> str:
    return _git_text(checkout, ["status", "--porcelain=v1", "--untracked-files=all"])


def _tracked_execution_source_paths(checkout: Path) -> list[str]:
    raw = _git_text(checkout, ["ls-tree", "-r", "--name-only", "HEAD", "--", "src"])
    paths: list[str] = []
    for relative in raw.splitlines():
        path = Path(relative)
        if (
            relative
            and not path.is_absolute()
            and all(part not in {"", ".", ".."} for part in path.parts)
            and path.suffix.lower() in EXECUTION_SOURCE_SUFFIXES
        ):
            paths.append(relative)
    if ENTRYPOINT_RELATIVE_PATH not in paths:
        raise ValueError("NDLOCR entrypoint is not a tracked execution-source file")
    return sorted(paths)


def _git_blob_sha256(checkout: Path, relative: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(checkout), "show", f"HEAD:{relative}"],
        check=True,
        capture_output=True,
        timeout=10,
    )
    return hashlib.sha256(completed.stdout).hexdigest()


def verify_checkout_integrity(checkout: Path) -> dict[str, Any]:
    """Reject dirty or byte-divergent source before importing executable code."""
    checkout = checkout.resolve(strict=True)
    status = _git_status(checkout)
    if status:
        summary = " | ".join(status.splitlines()[:10])
        raise ValueError(f"NDLOCR-Lite checkout is dirty: {summary}")
    manifest: dict[str, str] = {}
    for relative in _tracked_execution_source_paths(checkout):
        source = _regular_file(checkout / relative, "tracked execution source")
        working_sha256 = contract.sha256_file(source)
        committed_sha256 = _git_blob_sha256(checkout, relative)
        if working_sha256 != committed_sha256:
            raise ValueError(
                f"NDLOCR-Lite tracked execution source byte mismatch: {relative}"
            )
        manifest[relative] = working_sha256
    return {
        "git_dirty": False,
        "verified_against_git_head": True,
        "execution_source_file_count": len(manifest),
        "execution_source_tree_sha256": contract.sha256_json(manifest),
        "entrypoint": {
            "path": ENTRYPOINT_RELATIVE_PATH,
            "sha256": manifest[ENTRYPOINT_RELATIVE_PATH],
        },
    }


def _distribution_version(name: str) -> str:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return "not-installed"


def build_fingerprint(checkout: Path) -> dict[str, Any]:
    checkout = checkout.resolve(strict=True)
    commit = _git_commit(checkout)
    if commit != EXPECTED_COMMIT:
        raise ValueError(
            f"NDLOCR-Lite checkout commit mismatch: expected {EXPECTED_COMMIT}, got {commit}"
        )
    source_integrity = verify_checkout_integrity(checkout)
    model_sha256 = {
        relative: contract.sha256_file(_regular_file(checkout / relative, "model"))
        for relative in MODEL_RELATIVE_PATHS
    }
    config_sha256 = {
        relative: contract.sha256_file(_regular_file(checkout / relative, "config"))
        for relative in CONFIG_RELATIVE_PATHS
    }
    license_path = _regular_file(checkout / LICENSE_RELATIVE_PATH, "license")
    dependency_license_path = _regular_file(
        checkout / DEPENDENCY_LICENSE_MANIFEST_RELATIVE_PATH,
        "dependency license manifest",
    )
    runtime: dict[str, Any] = {
        "implementation": "NDLOCR-Lite",
        "git_commit": commit,
        "git_tag": ENGINE_VERSION,
        "source_integrity": source_integrity,
        "model_sha256": model_sha256,
        "config_sha256": config_sha256,
        "configuration": CONFIG,
        "license": {
            "spdx": "CC-BY-4.0",
            "source_url": SOURCE_URL,
            "source_revision_url": f"{SOURCE_URL}/tree/{commit}",
            "attribution_notice": "NDLOCR-Lite by the National Diet Library Lab (NDL Lab), licensed under CC BY 4.0",
            "license_path": LICENSE_RELATIVE_PATH,
            "license_sha256": contract.sha256_file(license_path),
            "dependency_license_manifest_path": DEPENDENCY_LICENSE_MANIFEST_RELATIVE_PATH,
            "dependency_license_manifest_sha256": contract.sha256_file(
                dependency_license_path
            ),
        },
        "limitations": LIMITATIONS,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "packages": {
            "numpy": _distribution_version("numpy"),
            "onnxruntime": _distribution_version("onnxruntime"),
            "opencv-python-headless": _distribution_version("opencv-python-headless"),
            "Pillow": _distribution_version("Pillow"),
            "PyYAML": _distribution_version("PyYAML"),
        },
    }
    payload = {"name": ENGINE_NAME, "version": ENGINE_VERSION, "runtime": runtime}
    return {
        "name": ENGINE_NAME,
        "version": ENGINE_VERSION,
        "fingerprint_sha256": contract.sha256_json(payload),
        "runtime": runtime,
    }


def _load_ndlocr_module(checkout: Path) -> ModuleType:
    source_dir = checkout / "src"
    source = _regular_file(source_dir / "ocr.py", "NDLOCR entrypoint")
    sys.path.insert(0, str(source_dir))
    spec = importlib.util.spec_from_file_location("ndlocr_lite_poc_runtime", source)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot import NDLOCR-Lite from {source}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _model_arguments(checkout: Path) -> SimpleNamespace:
    return SimpleNamespace(
        det_weights=str(checkout / MODEL_RELATIVE_PATHS[0]),
        det_classes=str(checkout / CONFIG_RELATIVE_PATHS[0]),
        det_score_threshold=CONFIG["det_score_threshold"],
        det_conf_threshold=CONFIG["det_conf_threshold"],
        det_iou_threshold=CONFIG["det_iou_threshold"],
        rec_weights30=str(checkout / MODEL_RELATIVE_PATHS[1]),
        rec_weights50=str(checkout / MODEL_RELATIVE_PATHS[2]),
        rec_weights=str(checkout / MODEL_RELATIVE_PATHS[3]),
        rec_classes=str(checkout / CONFIG_RELATIVE_PATHS[1]),
        device=CONFIG["device"],
        enable_tcy=CONFIG["enable_tcy"],
    )


def _normalized_bbox(points: Any, width: int, height: int) -> list[int]:
    """Convert NDLOCR's pixel polygon to the closed run schema rectangle."""
    if not isinstance(points, list) or len(points) != 4:
        raise ValueError("NDLOCR boundingBox must contain four points")
    coordinates: list[tuple[float, float]] = []
    for point in points:
        if not isinstance(point, list) or len(point) != 2:
            raise ValueError("NDLOCR boundingBox point must be [x, y]")
        x, y = point
        if isinstance(x, bool) or isinstance(y, bool) or not isinstance(x, (int, float)) or not isinstance(y, (int, float)):
            raise ValueError("NDLOCR boundingBox coordinates must be numeric")
        x_value, y_value = float(x), float(y)
        if not math.isfinite(x_value) or not math.isfinite(y_value):
            raise ValueError("NDLOCR boundingBox coordinates must be finite")
        coordinates.append((x_value, y_value))
    left = min(point[0] for point in coordinates)
    top = min(point[1] for point in coordinates)
    right = max(point[0] for point in coordinates)
    bottom = max(point[1] for point in coordinates)
    if left < 0 or top < 0 or right > width or bottom > height:
        raise ValueError(
            f"NDLOCR boundingBox is outside the crop: {(left, top, right, bottom)} vs {(width, height)}"
        )
    if right <= left or bottom <= top:
        raise ValueError("NDLOCR boundingBox has no area")

    normalized_left = min(999, max(0, round(left * 1000 / width)))
    normalized_top = min(999, max(0, round(top * 1000 / height)))
    normalized_right = min(1000, max(normalized_left + 1, round(right * 1000 / width)))
    normalized_bottom = min(1000, max(normalized_top + 1, round(bottom * 1000 / height)))
    return [
        normalized_left,
        normalized_top,
        normalized_right - normalized_left,
        normalized_bottom - normalized_top,
    ]


def convert_engine_lines(raw_lines: Any, width: int, height: int) -> list[dict[str, Any]]:
    """Retain NDLOCR line order/text/confidence; only bbox coordinates are normalized."""
    if not isinstance(raw_lines, list):
        raise ValueError("NDLOCR json_lines must be a list")
    lines: list[dict[str, Any]] = []
    for position, raw_line in enumerate(raw_lines, 1):
        if not isinstance(raw_line, dict):
            raise ValueError(f"NDLOCR line {position} must be an object")
        text = raw_line.get("text")
        if not isinstance(text, str):
            raise ValueError(f"NDLOCR line {position} text must be a string")
        if text == "":
            raise ValueError(
                f"NDLOCR line {position} contains empty text, which the closed run schema cannot represent"
            )
        confidence = raw_line.get("confidence")
        if confidence is not None:
            if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
                raise ValueError(f"NDLOCR line {position} confidence must be numeric or null")
            confidence = float(confidence)
            if not math.isfinite(confidence) or not 0 <= confidence <= 1:
                raise ValueError(f"NDLOCR line {position} confidence is outside [0, 1]")
        lines.append(
            {
                "line_id": f"line_{position}",
                "sequence": position,
                "raw_text": text,
                "bbox": _normalized_bbox(raw_line.get("boundingBox"), width, height),
                "confidence": confidence,
            }
        )
    return lines


class NDLOCRLiteAdapter:
    name = ENGINE_NAME

    def __init__(self, checkout: Path) -> None:
        self.checkout = checkout.resolve(strict=True)
        self._fingerprint = build_fingerprint(self.checkout)
        self._module: ModuleType | None = None
        self._models: tuple[Any, Any, Any, Any] | None = None
        self._pending_setup_ms = 0.0

    def fingerprint(self) -> dict[str, Any]:
        return self._fingerprint

    def prepare(self) -> float:
        if self._models is not None:
            return 0.0
        started = time.monotonic_ns()
        current_integrity = verify_checkout_integrity(self.checkout)
        if current_integrity != self._fingerprint["runtime"]["source_integrity"]:
            raise ValueError("NDLOCR-Lite execution source changed after fingerprinting")
        module = _load_ndlocr_module(self.checkout)
        arguments = _model_arguments(self.checkout)
        detector = module.get_detector(arguments)
        recognizer100 = module.get_recognizer(arguments)
        recognizer30 = module.get_recognizer(arguments, weights_path=arguments.rec_weights30)
        recognizer50 = module.get_recognizer(arguments, weights_path=arguments.rec_weights50)
        self._module = module
        self._models = (detector, recognizer30, recognizer50, recognizer100)
        self._pending_setup_ms = (time.monotonic_ns() - started) / 1_000_000
        return self._pending_setup_ms

    def run_path(self, value: adapters.OCRInput, image_path: Path) -> adapters.AdapterResult:
        setup_ms = self._pending_setup_ms
        self._pending_setup_ms = 0.0
        if self._module is None or self._models is None:
            raise RuntimeError("NDLOCR-Lite models have not been prepared")
        started = time.monotonic_ns()
        try:
            import numpy as np
            from PIL import Image

            with Image.open(image_path) as image:
                image.load()
                rgb = image.convert("RGB")
                if rgb.size != (value.width_px, value.height_px):
                    raise ValueError("materialized crop dimensions changed")
                array = np.array(rgb)
            detector, recognizer30, recognizer50, recognizer100 = self._models
            result = self._module._run_ocr_on_image_array(
                detector=detector,
                recognizer30=recognizer30,
                recognizer50=recognizer50,
                recognizer100=recognizer100,
                inputname=image_path.name,
                img=array,
                outputpath=str(image_path.parent),
                save_viz=False,
            )
            if not isinstance(result, dict):
                raise ValueError("NDLOCR-Lite API returned a non-object result")
            lines = convert_engine_lines(
                result.get("json_lines"), value.width_px, value.height_px
            )
            warnings = [
                "NDLOCR pixel boundingBox converted to schema-normalized [x,y,width,height]; engine line order retained",
                *LIMITATION_WARNINGS,
            ]
            status = "completed"
            if not lines:
                status = "needs_review"
                warnings.append("NDLOCR-Lite returned no OCR lines")
            return adapters.AdapterResult(
                status=status,
                lines=lines,
                warnings=warnings,
                error=None,
                setup_ms=setup_ms,
                inference_ms=(time.monotonic_ns() - started) / 1_000_000,
            )
        except Exception as exc:
            return adapters.AdapterResult(
                status="failed",
                lines=[],
                warnings=list(LIMITATION_WARNINGS),
                error=f"{type(exc).__name__}: {exc}",
                setup_ms=setup_ms,
                inference_ms=(time.monotonic_ns() - started) / 1_000_000,
            )


def _validated_fixtures(
    fixtures_path: Path, repository_root: Path, limit: int | None
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
    if limit is not None:
        fixtures = fixtures[:limit]
    return fixtures


def run_manifest(
    fixtures_path: Path,
    output_path: Path,
    *,
    repository_root: Path,
    checkout: Path,
    temp_root: Path,
    limit: int | None,
    overwrite: bool,
) -> list[dict[str, Any]]:
    if output_path.exists() and not overwrite:
        raise ValueError(f"output already exists; pass --overwrite to replace it: {output_path}")
    repository_root = repository_root.resolve(strict=True)
    fixtures = _validated_fixtures(fixtures_path, repository_root, limit)
    if not fixtures:
        raise ValueError("fixture selection is empty")
    if temp_root.is_symlink() or not temp_root.is_dir():
        raise ValueError(f"temporary root must be a non-symlink directory: {temp_root}")

    values: list[adapters.OCRInput] = []
    paths: list[Path] = []
    with tempfile.TemporaryDirectory(prefix="aiec-ndlocr-poc-", dir=temp_root) as raw_temp:
        temp_dir = Path(raw_temp)
        if not str(temp_dir).isascii():
            raise ValueError(f"NDLOCR temporary path must contain only ASCII: {temp_dir}")
        for position, fixture in enumerate(fixtures, 1):
            value = adapters.crop_input(fixture, repository_root)
            path = temp_dir / f"crop_{position:04d}.png"
            path.write_bytes(value.image_bytes)
            if contract.sha256_file(path) != value.image_sha256:
                raise RuntimeError(f"materialized crop hash mismatch: {path.name}")
            values.append(value)
            paths.append(path)

        adapter = NDLOCRLiteAdapter(checkout)
        try:
            adapter.prepare()
        except Exception as exc:
            error = f"NDLOCR-Lite setup failed: {type(exc).__name__}: {exc}"
            runs = [
                runner.make_run(
                    fixture,
                    adapter,
                    value,
                    adapters.AdapterResult(
                        status="unavailable",
                        lines=[],
                        warnings=list(LIMITATION_WARNINGS),
                        error=error,
                        setup_ms=0.0,
                        inference_ms=0.0,
                    ),
                )
                for fixture, value in zip(fixtures, values)
            ]
        else:
            runs = [
                runner.make_run(fixture, adapter, value, adapter.run_path(value, path))
                for fixture, value, path in zip(fixtures, values, paths)
            ]

    contract.write_jsonl(output_path, runs)
    return runs


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("--fixtures", type=Path, required=True)
    value.add_argument("--output", type=Path, required=True)
    value.add_argument("--repository-root", type=Path, default=ROOT)
    value.add_argument("--checkout", type=Path, default=DEFAULT_CHECKOUT)
    value.add_argument("--temp-root", type=Path, default=DEFAULT_TEMP_ROOT)
    value.add_argument("--limit", type=int, help="smoke-test only: process the first N fixtures")
    value.add_argument("--overwrite", action="store_true")
    return value


def main() -> int:
    args = parser().parse_args()
    if args.limit is not None and args.limit <= 0:
        raise SystemExit("--limit must be positive")
    started = time.monotonic_ns()
    try:
        runs = run_manifest(
            args.fixtures,
            args.output,
            repository_root=args.repository_root,
            checkout=args.checkout,
            temp_root=args.temp_root,
            limit=args.limit,
            overwrite=args.overwrite,
        )
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    elapsed_ms = (time.monotonic_ns() - started) / 1_000_000
    status_counts: dict[str, int] = {}
    for run in runs:
        status_counts[run["status"]] = status_counts.get(run["status"], 0) + 1
    print(f"wrote {len(runs)} NDLOCR-Lite OCR PoC runs to {args.output}")
    print("statuses: " + ", ".join(f"{key}={count}" for key, count in sorted(status_counts.items())))
    print(f"elapsed_ms: {elapsed_ms:.3f}")
    print(f"output_sha256: {contract.sha256_file(args.output)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
