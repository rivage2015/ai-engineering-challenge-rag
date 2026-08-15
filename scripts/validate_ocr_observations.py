#!/usr/bin/env python3
"""Strictly validate image-bound dual-engine OCR observations."""

from __future__ import annotations

import argparse
import datetime as dt
import functools
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
import unicodedata
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import classify_visual_assets as classifier  # noqa: E402
import validate_visual_classifications as classification_validator  # noqa: E402

SCHEMA_PATH = ROOT / "schemas" / "ocr-observation.schema.json"

OBSERVER = "dual-local-ocr-observer"
OBSERVER_VERSION = "0.1"
CONSENSUS_METHOD = "strict-spatial-nfc-exact"
CONSENSUS_VERSION = "0.1"
COORDINATE_SYSTEM = "top_left_normalized_1000"
SPATIAL_OVERLAP_THRESHOLD = 0.5
MAX_IMAGE_PIXELS = 50_000_000
MAX_IMAGE_BYTES = 200 * 1024 * 1024
MAX_JSONL_BYTES = 64 * 1024 * 1024
MAX_JSONL_RECORDS = 100_000
MAX_LINES_PER_ENGINE = 2_000

ENGINE_ORDER = ("apple_vision", "tesseract")
ENGINE_VERSIONS = {
    "apple_vision": "vision-revision-3",
    "tesseract": "tesseract-5.5.2",
}
ENGINE_GROUPS = {
    "apple_vision": "apple_vision",
    "tesseract": "tesseract_lstm",
}
ENGINE_RUNNERS = {
    "apple_vision": "apple-vision-swift-ocr",
    "tesseract": "tesseract-tsv-ocr",
}
RUNNER_VERSION = "0.1"
APPLE_COMPILE_TARGET = f"{platform.machine()}-apple-macosx13.0"

APPLE_VISION_CONFIG: dict[str, Any] = {
    "coordinate_system": COORDINATE_SYSTEM,
    "recognition_level": "accurate",
    "recognition_languages": ["ja-JP", "en-US"],
    "uses_language_correction": True,
    "automatically_detects_language": False,
    "request_revision": 3,
}
TESSERACT_CONFIG: dict[str, Any] = {
    "coordinate_system": COORDINATE_SYSTEM,
    "languages": ["jpn", "eng"],
    "oem": 1,
    "psm": 3,
    "preserve_interword_spaces": True,
    "text_line_source": "tesseract_txt",
    "geometry_source": "tesseract_tsv_line_group",
    "line_alignment": "strict_one_to_one",
    "line_confidence_aggregation": "minimum_word_confidence",
}
EXPECTED_CONFIGS = {
    "apple_vision": APPLE_VISION_CONFIG,
    "tesseract": TESSERACT_CONFIG,
}

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
MIME_RE = re.compile(r"^image/[A-Za-z0-9.+-]+$")
ASSET_ID_RE = re.compile(r"^asset_[0-9a-f]{32}$")
CLASSIFICATION_ID_RE = re.compile(r"^vc_[0-9a-f]{16,64}$")
OBSERVATION_ID_RE = re.compile(r"^ocr_[0-9a-f]{24}$")
RUN_ID_RE = re.compile(r"^ocr_run_[0-9a-f]{24}$")
LINE_ID_RE = re.compile(r"^line_[0-9]+$")
CONSENSUS_LINE_ID_RE = re.compile(r"^ocr_line_[0-9a-f]{16}$")

ROOT_KEYS = {
    "schema_version", "record_type", "observation_id", "asset_id", "asset",
    "source", "origin", "classification_ref", "engine_runs", "consensus",
    "exactness", "warnings", "status", "hashes", "provenance",
}
ASSET_KEYS = {"materialized_path", "sha256", "mime_type", "dimensions"}
CLASSIFICATION_REF_KEYS = {
    "classification_id", "output_sha256", "signature_sha256", "routes",
}
RUN_KEYS = {
    "run_id", "engine", "config", "status", "lines", "warnings", "error",
    "hashes", "provenance",
}
ENGINE_KEYS = {"name", "version", "digest", "independence_group", "runtime"}
APPLE_RUNTIME_KEYS = {
    "os_version", "os_build", "architecture", "compile_target", "swiftc_version",
    "wrapper_path", "wrapper_sha256", "build_signature_sha256",
}
TESSERACT_RUNTIME_KEYS = {
    "executable_path", "binary_sha256", "tessdata_path", "traineddata",
}
LINE_KEYS = {"line_id", "sequence", "raw_text", "bbox", "confidence"}
RUN_HASH_KEYS = {"input_sha256", "output_sha256", "signature_sha256"}
RUN_PROVENANCE_KEYS = {
    "runner", "runner_version", "generated_at", "inference_generated_at",
    "cache_hit", "question_independent",
}
CONSENSUS_KEYS = {
    "method", "version", "coordinate_system", "overlap_threshold", "lines",
    "unresolved_count",
}
CONSENSUS_LINE_KEYS = {
    "consensus_line_id", "bbox", "exactness", "text", "readings",
}
READING_KEYS = {"run_id", "line_id", "raw_text", "bbox"}
HASH_KEYS = {"input_sha256", "output_sha256", "signature_sha256"}
PROVENANCE_KEYS = {
    "observer", "observer_version", "generated_at", "cache_hit",
    "question_independent", "evidence_connected", "search_unit_connected",
}
ROUTES = {
    "ocr_text", "table_structure", "chart_source_recovery", "diagram_relations",
    "formula_ocr", "image_description", "skip", "review",
}


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_json(value: Any) -> str:
    return sha256_bytes(canonical_json(value).encode("utf-8"))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _command_text(command: list[str]) -> str:
    try:
        completed = subprocess.run(
            command, check=False, capture_output=True, text=True, timeout=30
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError(f"cannot execute runtime fingerprint command {command[0]}: {exc}") from exc
    output = (completed.stdout or completed.stderr or "").strip()
    if completed.returncode != 0 or not output:
        raise ValueError(
            f"runtime fingerprint command failed ({completed.returncode}): {' '.join(command)}"
        )
    return output


@functools.lru_cache(maxsize=2)
def current_engine_runtime(name: str) -> dict[str, Any]:
    """Fingerprint the actual local runtime used by an OCR engine."""
    if name == "apple_vision":
        wrapper = ROOT / "scripts" / "apple_vision_ocr.swift"
        if wrapper.is_symlink() or not wrapper.is_file():
            raise ValueError(f"Apple Vision wrapper must be a regular repository file: {wrapper}")
        os_version = _command_text(["sw_vers", "-productVersion"]).splitlines()[0]
        os_build = _command_text(["sw_vers", "-buildVersion"]).splitlines()[0]
        swiftc_version = _command_text(["xcrun", "swiftc", "--version"])
        runtime = {
            "os_version": os_version,
            "os_build": os_build,
            "architecture": platform.machine(),
            "compile_target": APPLE_COMPILE_TARGET,
            "swiftc_version": swiftc_version,
            "wrapper_path": "scripts/apple_vision_ocr.swift",
            "wrapper_sha256": sha256_file(wrapper),
        }
        runtime["build_signature_sha256"] = apple_vision_build_signature(runtime)
        return runtime
    if name == "tesseract":
        executable_raw = shutil.which("tesseract")
        if not executable_raw:
            raise ValueError("tesseract executable is not available")
        executable = Path(executable_raw).resolve()
        if not executable.is_file():
            raise ValueError(f"tesseract executable is not a regular file: {executable}")
        version_line = _command_text([str(executable), "--version"]).splitlines()[0]
        if version_line != "tesseract 5.5.2":
            raise ValueError(
                f"fixed OCR contract requires tesseract 5.5.2, got {version_line}"
            )
        language_output = _command_text([str(executable), "--list-langs"])
        first_line = language_output.splitlines()[0]
        match = re.fullmatch(r'List of available languages in "(.+)" \([0-9]+\):', first_line)
        if match is None:
            raise ValueError("cannot determine Tesseract tessdata directory")
        tessdata = Path(match.group(1)).resolve()
        if not tessdata.is_dir():
            raise ValueError(f"Tesseract tessdata directory is missing: {tessdata}")
        traineddata: dict[str, dict[str, str]] = {}
        for language in ("jpn", "eng"):
            entry = tessdata / f"{language}.traineddata"
            try:
                resolved = entry.resolve(strict=True)
            except OSError as exc:
                raise ValueError(f"Tesseract traineddata is missing: {entry}") from exc
            if not resolved.is_file() or resolved.is_symlink():
                raise ValueError(
                    f"Tesseract traineddata target must be a regular file: {resolved}"
                )
            traineddata[language] = {
                "path": str(resolved),
                "sha256": sha256_file(resolved),
            }
        return {
            "executable_path": str(executable),
            "binary_sha256": sha256_file(executable),
            "tessdata_path": str(tessdata),
            "traineddata": traineddata,
        }
    raise ValueError(f"unsupported OCR engine: {name}")


def apple_vision_build_signature(runtime: dict[str, Any]) -> str:
    """Return the path-independent signature used by the canonical Swift build."""
    return sha256_json({
        "source_sha256": runtime.get("wrapper_sha256"),
        "swiftc_version": runtime.get("swiftc_version"),
        "target": runtime.get("compile_target"),
        "runner_version": RUNNER_VERSION,
    })


def expected_engine_digest(name: str, runtime: dict[str, Any] | None = None) -> str:
    """Hash the fixed contract version and its actual runtime fingerprint."""
    runtime_value = current_engine_runtime(name) if runtime is None else runtime
    return sha256_json({
        "contract": "ocr-engine-v0.1",
        "name": name,
        "version": ENGINE_VERSIONS[name],
        "runtime": runtime_value,
    })


def expected_engine(name: str) -> dict[str, Any]:
    runtime = current_engine_runtime(name)
    return {
        "name": name,
        "version": ENGINE_VERSIONS[name],
        "digest": expected_engine_digest(name, runtime),
        "independence_group": ENGINE_GROUPS[name],
        "runtime": runtime,
    }


def expected_asset_envelope(asset: dict[str, Any]) -> dict[str, Any]:
    return {
        "materialized_path": asset["declared_path"],
        "sha256": asset["sha256"],
        "mime_type": asset["mime_type"],
        "dimensions": asset["dimensions"],
    }


def expected_classification_ref(classification: dict[str, Any]) -> dict[str, Any]:
    hashes = classification.get("hashes")
    if not isinstance(hashes, dict):
        raise ValueError("classification hashes must be an object")
    return {
        "classification_id": classification.get("classification_id"),
        "output_sha256": hashes.get("output_sha256"),
        "signature_sha256": hashes.get("signature_sha256"),
        "routes": classification.get("routes"),
    }


def observation_input_payload(
    asset: dict[str, Any], classification: dict[str, Any]
) -> dict[str, Any]:
    return {
        "asset_id": asset["asset_id"],
        "asset": expected_asset_envelope(asset),
        "source": asset["source"],
        "origin": asset["origin"],
        "classification_ref": expected_classification_ref(classification),
    }


def engine_input_payload(asset: dict[str, Any]) -> dict[str, Any]:
    return {
        "asset_id": asset["asset_id"],
        "image_sha256": asset["sha256"],
        "mime_type": asset["mime_type"],
        "dimensions": asset["dimensions"],
    }


def engine_input_sha256(asset: dict[str, Any]) -> str:
    return sha256_json(engine_input_payload(asset))


def engine_output_payload(run: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": run.get("status"),
        "lines": run.get("lines"),
        "warnings": run.get("warnings"),
        "error": run.get("error"),
    }


def engine_output_sha256(run: dict[str, Any]) -> str:
    return sha256_json(engine_output_payload(run))


def engine_signature_sha256(asset: dict[str, Any], run: dict[str, Any]) -> str:
    provenance = run.get("provenance")
    runner = provenance.get("runner") if isinstance(provenance, dict) else None
    runner_version = (
        provenance.get("runner_version") if isinstance(provenance, dict) else None
    )
    return sha256_json({
        "input_sha256": engine_input_sha256(asset),
        "engine": run.get("engine"),
        "config": run.get("config"),
        "runner": runner,
        "runner_version": runner_version,
    })


def expected_run_id(signature: str) -> str:
    # This is intentionally an input-contract/cache identity.  OCR bytes have
    # a separate output_sha256 so nondeterministic output never changes which
    # cache slot is checked, while any changed output remains detectable.
    return "ocr_run_" + signature[:24]


def _is_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


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


def _bbox_overlap_over_min(left: list[int], right: list[int]) -> float:
    lx, ly, lw, lh = left
    rx, ry, rw, rh = right
    intersection_width = max(0, min(lx + lw, rx + rw) - max(lx, rx))
    intersection_height = max(0, min(ly + lh, ry + rh) - max(ly, ry))
    intersection = intersection_width * intersection_height
    minimum_area = min(lw * lh, rw * rh)
    return intersection / minimum_area if minimum_area else 0.0


def _bbox_envelope(values: Iterable[list[int]]) -> list[int]:
    boxes = list(values)
    left = min(box[0] for box in boxes)
    top = min(box[1] for box in boxes)
    right = max(box[0] + box[2] for box in boxes)
    bottom = max(box[1] + box[3] for box in boxes)
    return [left, top, right - left, bottom - top]


def comparison_text(value: str) -> str:
    """Perform only the comparison normalization allowed by the OCR contract."""
    return unicodedata.normalize("NFC", value)


def _reading(run: dict[str, Any], line: dict[str, Any]) -> dict[str, Any]:
    return {
        "run_id": run["run_id"],
        "line_id": line["line_id"],
        "raw_text": line["raw_text"],
        "bbox": line["bbox"],
    }


def _usable_consensus_line(run: Any, line: Any) -> bool:
    return (
        isinstance(run, dict)
        and isinstance(run.get("run_id"), str)
        and isinstance(line, dict)
        and isinstance(line.get("line_id"), str)
        and isinstance(line.get("raw_text"), str)
        and bool(line["raw_text"])
        and _valid_bbox(line.get("bbox"))
    )


def _consensus_line(asset_id: str, readings: list[dict[str, Any]], exactness: str) -> dict[str, Any]:
    text = readings[0]["raw_text"] if exactness == "observed" else None
    identity = sha256_json({"asset_id": asset_id, "readings": readings})[:16]
    return {
        "consensus_line_id": "ocr_line_" + identity,
        "bbox": _bbox_envelope([reading["bbox"] for reading in readings]),
        "exactness": exactness,
        "text": text,
        "readings": readings,
    }


def build_consensus(asset_id: str, runs: list[dict[str, Any]]) -> dict[str, Any]:
    """Build deterministic one-to-one spatial/NFC consensus from fixed engine order."""
    if len(runs) != 2:
        raise ValueError("OCR consensus requires exactly two engine runs")
    left_run, right_run = runs
    if not isinstance(left_run, dict) or not isinstance(right_run, dict):
        raise ValueError("OCR consensus engine runs must be objects")
    left_raw = left_run.get("lines")
    right_raw = right_run.get("lines")
    left_lines = left_raw if isinstance(left_raw, list) else []
    right_lines = right_raw if isinstance(right_raw, list) else []
    if len(left_lines) > MAX_LINES_PER_ENGINE or len(right_lines) > MAX_LINES_PER_ENGINE:
        raise ValueError(
            f"OCR engine line count exceeds {MAX_LINES_PER_ENGINE} safety limit"
        )

    candidates: list[tuple[float, int, int]] = []
    for left_index, left in enumerate(left_lines):
        if not _usable_consensus_line(left_run, left):
            continue
        for right_index, right in enumerate(right_lines):
            if not _usable_consensus_line(right_run, right):
                continue
            overlap = _bbox_overlap_over_min(left["bbox"], right["bbox"])
            if overlap >= SPATIAL_OVERLAP_THRESHOLD:
                candidates.append((-overlap, left_index, right_index))
    candidates.sort()

    matched_left: set[int] = set()
    matched_right: set[int] = set()
    matches: list[tuple[int, int]] = []
    for _negative_overlap, left_index, right_index in candidates:
        if left_index in matched_left or right_index in matched_right:
            continue
        matched_left.add(left_index)
        matched_right.add(right_index)
        matches.append((left_index, right_index))

    engines_clean = all(
        run.get("status") == "completed"
        and run.get("error") is None
        and run.get("warnings") == []
        for run in runs
    )
    lines: list[dict[str, Any]] = []
    for left_index, right_index in matches:
        left = left_lines[left_index]
        right = right_lines[right_index]
        readings = [_reading(left_run, left), _reading(right_run, right)]
        equal = (
            engines_clean
            and comparison_text(left["raw_text"]) != ""
            and comparison_text(left["raw_text"]) == comparison_text(right["raw_text"])
        )
        lines.append(_consensus_line(asset_id, readings, "observed" if equal else "unresolved"))
    for index, line in enumerate(left_lines):
        if index not in matched_left and _usable_consensus_line(left_run, line):
            lines.append(_consensus_line(asset_id, [_reading(left_run, line)], "unresolved"))
    for index, line in enumerate(right_lines):
        if index not in matched_right and _usable_consensus_line(right_run, line):
            lines.append(_consensus_line(asset_id, [_reading(right_run, line)], "unresolved"))

    lines.sort(key=lambda line: (
        line["bbox"][1], line["bbox"][0], line["bbox"][3], line["bbox"][2],
        line["consensus_line_id"],
    ))
    unresolved_count = sum(line["exactness"] == "unresolved" for line in lines)
    return {
        "method": CONSENSUS_METHOD,
        "version": CONSENSUS_VERSION,
        "coordinate_system": COORDINATE_SYSTEM,
        "overlap_threshold": SPATIAL_OVERLAP_THRESHOLD,
        "lines": lines,
        "unresolved_count": unresolved_count,
    }


def expected_exactness(consensus: dict[str, Any], runs: list[dict[str, Any]]) -> str:
    lines = consensus.get("lines")
    if (
        isinstance(lines, list)
        and bool(lines)
        and consensus.get("unresolved_count") == 0
        and all(run.get("status") == "completed" for run in runs)
        and all(run.get("warnings") == [] and run.get("error") is None for run in runs)
    ):
        return "observed"
    return "unresolved"


def expected_status(exactness: str, runs: list[dict[str, Any]]) -> str:
    if exactness == "observed":
        return "observed"
    if all(run.get("status") == "failed" for run in runs):
        return "failed"
    return "needs_review"


def expected_warnings(consensus: dict[str, Any], runs: list[dict[str, Any]]) -> list[str]:
    warnings: list[str] = []
    for run in runs:
        engine = run.get("engine")
        name = engine.get("name") if isinstance(engine, dict) else "unknown_engine"
        raw_warnings = run.get("warnings")
        if isinstance(raw_warnings, list):
            warnings.extend(
                f"{name}: {warning}" for warning in raw_warnings
                if isinstance(warning, str) and warning
            )
        if run.get("status") != "completed":
            detail = run.get("error") if isinstance(run.get("error"), str) else "no error detail"
            warnings.append(f"{name}: {run.get('status')}: {detail}")
    lines = consensus.get("lines")
    if not isinstance(lines, list) or not lines:
        warnings.append("consensus contains no lines")
    elif consensus.get("unresolved_count"):
        warnings.append(f"consensus unresolved lines: {consensus.get('unresolved_count')}")
    return list(dict.fromkeys(warnings))


def observation_signature_sha256(input_sha256: str, runs: list[dict[str, Any]]) -> str:
    # Like run_id, observation_id identifies the fixed inputs and engine
    # contract. observation_output_payload is independently content-hashed.
    return sha256_json({
        "observer": OBSERVER,
        "observer_version": OBSERVER_VERSION,
        "input_sha256": input_sha256,
        "engine_signatures": [
            run.get("hashes", {}).get("signature_sha256")
            if isinstance(run.get("hashes"), dict) else None
            for run in runs
        ],
        "consensus_method": CONSENSUS_METHOD,
        "consensus_version": CONSENSUS_VERSION,
    })


def observation_output_payload(record: dict[str, Any]) -> dict[str, Any]:
    runs = record.get("engine_runs") if isinstance(record.get("engine_runs"), list) else []
    return {
        "engine_outputs": [
            {
                "run_id": run.get("run_id") if isinstance(run, dict) else None,
                "output_sha256": (
                    run.get("hashes", {}).get("output_sha256")
                    if isinstance(run, dict) and isinstance(run.get("hashes"), dict)
                    else None
                ),
            }
            for run in runs
        ],
        "consensus": record.get("consensus"),
        "exactness": record.get("exactness"),
        "warnings": record.get("warnings"),
        "status": record.get("status"),
    }


def _validate_timestamp(value: Any, name: str, errors: list[str]) -> dt.datetime | None:
    if not isinstance(value, str) or not value:
        errors.append(f"{name} must be a timezone-aware ISO datetime")
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError
    except ValueError:
        errors.append(f"{name} must be a timezone-aware ISO datetime")
        return None
    return parsed


def _validate_string_array(value: Any, name: str, errors: list[str]) -> list[str]:
    if not isinstance(value, list):
        errors.append(f"{name} must be an array")
        return []
    if len(value) > 100:
        errors.append(f"{name} exceeds 100 item safety limit")
    if any(not isinstance(item, str) or not item for item in value):
        errors.append(f"{name} must contain only non-empty strings")
    if any(isinstance(item, str) and len(item) > 10_000 for item in value):
        errors.append(f"{name} items must not exceed 10000 characters")
    strings = [item for item in value if isinstance(item, str)]
    if len(strings) != len(set(strings)):
        errors.append(f"{name} must not contain duplicates")
    return strings


def _validate_declared_runtime(
    name: str, runtime: Any, prefix: str, errors: list[str]
) -> dict[str, Any] | None:
    expected_keys = APPLE_RUNTIME_KEYS if name == "apple_vision" else TESSERACT_RUNTIME_KEYS
    if not isinstance(runtime, dict) or set(runtime) != expected_keys:
        errors.append(f"{prefix}.runtime keys are incomplete or unknown")
        return None
    if name == "apple_vision":
        for key in ("os_version", "os_build", "architecture", "swiftc_version"):
            if not isinstance(runtime.get(key), str) or not runtime[key]:
                errors.append(f"{prefix}.runtime.{key} must be non-empty")
        architecture = runtime.get("architecture")
        expected_target = (
            f"{architecture}-apple-macosx13.0"
            if architecture in {"arm64", "x86_64"}
            else None
        )
        if runtime.get("compile_target") != expected_target:
            errors.append(
                f"{prefix}.runtime.compile_target must match its architecture and macOS 13.0"
            )
        if runtime.get("wrapper_path") != "scripts/apple_vision_ocr.swift":
            errors.append(f"{prefix}.runtime.wrapper_path is invalid")
        wrapper_sha = runtime.get("wrapper_sha256")
        if not isinstance(wrapper_sha, str) or not SHA256_RE.fullmatch(wrapper_sha):
            errors.append(f"{prefix}.runtime.wrapper_sha256 must be lowercase SHA-256")
        build_signature = runtime.get("build_signature_sha256")
        if (
            not isinstance(build_signature, str)
            or not SHA256_RE.fullmatch(build_signature)
            or build_signature != apple_vision_build_signature(runtime)
        ):
            errors.append(
                f"{prefix}.runtime.build_signature_sha256 does not match the canonical build"
            )
    else:
        for key in ("executable_path", "tessdata_path"):
            value = runtime.get(key)
            if not isinstance(value, str) or not value or not Path(value).is_absolute():
                errors.append(f"{prefix}.runtime.{key} must be a non-empty absolute path")
        binary_sha = runtime.get("binary_sha256")
        if not isinstance(binary_sha, str) or not SHA256_RE.fullmatch(binary_sha):
            errors.append(f"{prefix}.runtime.binary_sha256 must be lowercase SHA-256")
        traineddata = runtime.get("traineddata")
        if not isinstance(traineddata, dict) or set(traineddata) != {"jpn", "eng"}:
            errors.append(
                f"{prefix}.runtime.traineddata must contain only jpn and eng"
            )
        else:
            for language in ("jpn", "eng"):
                value = traineddata.get(language)
                if not isinstance(value, dict) or set(value) != {"path", "sha256"}:
                    errors.append(
                        f"{prefix}.runtime.traineddata.{language} keys are incomplete or unknown"
                    )
                    continue
                path = value.get("path")
                if not isinstance(path, str) or not path or not Path(path).is_absolute():
                    errors.append(
                        f"{prefix}.runtime.traineddata.{language}.path must be absolute"
                    )
                digest = value.get("sha256")
                if not isinstance(digest, str) or not SHA256_RE.fullmatch(digest):
                    errors.append(
                        f"{prefix}.runtime.traineddata.{language}.sha256 must be lowercase SHA-256"
                    )
    return runtime


def _validate_line(line: Any, index: int, prefix: str, errors: list[str]) -> None:
    if not isinstance(line, dict):
        errors.append(f"{prefix}[{index}] must be an object")
        return
    if set(line) != LINE_KEYS:
        errors.append(f"{prefix}[{index}] keys are incomplete or unknown")
    expected_sequence = index + 1
    if line.get("line_id") != f"line_{expected_sequence}":
        errors.append(f"{prefix}[{index}].line_id must be line_{expected_sequence}")
    if line.get("sequence") != expected_sequence:
        errors.append(f"{prefix}[{index}].sequence must be {expected_sequence}")
    raw_text = line.get("raw_text")
    if not isinstance(raw_text, str) or not raw_text:
        errors.append(f"{prefix}[{index}].raw_text must be non-empty")
    elif len(raw_text) > 10_000:
        errors.append(f"{prefix}[{index}].raw_text exceeds 10000 characters")
    elif "\n" in raw_text or "\r" in raw_text or "\x00" in raw_text:
        errors.append(f"{prefix}[{index}].raw_text must contain exactly one raw line")
    if not _valid_bbox(line.get("bbox")):
        errors.append(f"{prefix}[{index}].bbox must fit top-left normalized_1000 bounds")
    confidence = line.get("confidence")
    if confidence is not None and (
        not _is_number(confidence) or float(confidence) < 0 or float(confidence) > 1
    ):
        errors.append(f"{prefix}[{index}].confidence must be null or between 0 and 1")


def _validate_run(run: Any, index: int, errors: list[str]) -> None:
    prefix = f"engine_runs[{index}]"
    if not isinstance(run, dict):
        errors.append(f"{prefix} must be an object")
        return
    if set(run) != RUN_KEYS:
        errors.append(f"{prefix} keys are incomplete or unknown")
    expected_name = ENGINE_ORDER[index] if index < len(ENGINE_ORDER) else None
    engine = run.get("engine")
    if not isinstance(engine, dict) or set(engine) != ENGINE_KEYS:
        errors.append(f"{prefix}.engine keys are incomplete or unknown")
        engine = {}
    if expected_name is not None:
        if engine.get("name") != expected_name:
            errors.append(f"{prefix}.engine.name must be {expected_name}")
        if engine.get("version") != ENGINE_VERSIONS[expected_name]:
            errors.append(
                f"{prefix}.engine.version must be {ENGINE_VERSIONS[expected_name]}"
            )
        if engine.get("independence_group") != ENGINE_GROUPS[expected_name]:
            errors.append(f"{prefix}.engine.independence_group is invalid")
        runtime = _validate_declared_runtime(
            expected_name, engine.get("runtime"), f"{prefix}.engine", errors
        )
        digest = engine.get("digest")
        if runtime is not None and digest != expected_engine_digest(expected_name, runtime):
            errors.append(f"{prefix}.engine.digest does not match runtime fingerprint")
    config = run.get("config")
    if expected_name is not None and config != EXPECTED_CONFIGS[expected_name]:
        errors.append(f"{prefix}.config does not match fixed {expected_name} v0.1 config")
    status = run.get("status")
    if status not in {"completed", "needs_review", "failed"}:
        errors.append(f"{prefix}.status is invalid")
    lines = run.get("lines")
    if not isinstance(lines, list):
        errors.append(f"{prefix}.lines must be an array")
        lines = []
    if len(lines) > MAX_LINES_PER_ENGINE:
        errors.append(
            f"{prefix}.lines exceeds {MAX_LINES_PER_ENGINE} item safety limit"
        )
    for line_index, line in enumerate(lines[:MAX_LINES_PER_ENGINE]):
        _validate_line(line, line_index, f"{prefix}.lines", errors)
    warnings = _validate_string_array(run.get("warnings"), f"{prefix}.warnings", errors)
    error = run.get("error")
    if status == "completed" and (error is not None or warnings):
        errors.append(f"{prefix} completed status requires null error and no warnings")
    if status == "needs_review" and not warnings and not isinstance(error, str):
        errors.append(f"{prefix} needs_review status requires a warning or error")
    if status == "failed":
        if lines:
            errors.append(f"{prefix} failed status requires empty lines")
        if not isinstance(error, str) or not error:
            errors.append(f"{prefix} failed status requires a non-empty error")
    if error is not None and (not isinstance(error, str) or not error):
        errors.append(f"{prefix}.error must be null or a non-empty string")
    if isinstance(error, str) and len(error) > 10_000:
        errors.append(f"{prefix}.error exceeds 10000 characters")
    hashes = run.get("hashes")
    if not isinstance(hashes, dict) or set(hashes) != RUN_HASH_KEYS:
        errors.append(f"{prefix}.hashes keys are incomplete or unknown")
    else:
        for key in RUN_HASH_KEYS:
            if not isinstance(hashes.get(key), str) or not SHA256_RE.fullmatch(hashes[key]):
                errors.append(f"{prefix}.hashes.{key} must be lowercase SHA-256")
        if hashes.get("output_sha256") != engine_output_sha256(run):
            errors.append(f"{prefix}.hashes.output_sha256 does not match engine output")
        signature = hashes.get("signature_sha256")
        if (
            isinstance(signature, str)
            and SHA256_RE.fullmatch(signature)
            and run.get("run_id") != expected_run_id(signature)
        ):
            errors.append(f"{prefix}.run_id does not match signature")
    provenance = run.get("provenance")
    if not isinstance(provenance, dict) or set(provenance) != RUN_PROVENANCE_KEYS:
        errors.append(f"{prefix}.provenance keys are incomplete or unknown")
    else:
        if expected_name is not None and provenance.get("runner") != ENGINE_RUNNERS[expected_name]:
            errors.append(f"{prefix}.provenance.runner is invalid")
        if provenance.get("runner_version") != RUNNER_VERSION:
            errors.append(f"{prefix}.provenance.runner_version must be {RUNNER_VERSION}")
        if provenance.get("question_independent") is not True:
            errors.append(f"{prefix}.provenance.question_independent must be true")
        if not isinstance(provenance.get("cache_hit"), bool):
            errors.append(f"{prefix}.provenance.cache_hit must be boolean")
        generated = _validate_timestamp(
            provenance.get("generated_at"), f"{prefix}.provenance.generated_at", errors
        )
        inference = _validate_timestamp(
            provenance.get("inference_generated_at"),
            f"{prefix}.provenance.inference_generated_at", errors,
        )
        if generated is not None and inference is not None and inference > generated:
            errors.append(f"{prefix}.provenance.inference_generated_at must not follow generated_at")


def _validate_consensus_shape(consensus: Any, errors: list[str]) -> None:
    if not isinstance(consensus, dict):
        errors.append("consensus must be an object")
        return
    if set(consensus) != CONSENSUS_KEYS:
        errors.append("consensus keys are incomplete or unknown")
    if consensus.get("method") != CONSENSUS_METHOD:
        errors.append(f"consensus.method must be {CONSENSUS_METHOD}")
    if consensus.get("version") != CONSENSUS_VERSION:
        errors.append(f"consensus.version must be {CONSENSUS_VERSION}")
    if consensus.get("coordinate_system") != COORDINATE_SYSTEM:
        errors.append(f"consensus.coordinate_system must be {COORDINATE_SYSTEM}")
    if consensus.get("overlap_threshold") != SPATIAL_OVERLAP_THRESHOLD:
        errors.append(f"consensus.overlap_threshold must be {SPATIAL_OVERLAP_THRESHOLD}")
    lines = consensus.get("lines")
    if not isinstance(lines, list):
        errors.append("consensus.lines must be an array")
        lines = []
    if len(lines) > MAX_LINES_PER_ENGINE * 2:
        errors.append(
            f"consensus.lines exceeds {MAX_LINES_PER_ENGINE * 2} item safety limit"
        )
    seen_ids: set[str] = set()
    unresolved = 0
    for index, line in enumerate(lines[:MAX_LINES_PER_ENGINE * 2]):
        prefix = f"consensus.lines[{index}]"
        if not isinstance(line, dict):
            errors.append(f"{prefix} must be an object")
            continue
        if set(line) != CONSENSUS_LINE_KEYS:
            errors.append(f"{prefix} keys are incomplete or unknown")
        line_id = line.get("consensus_line_id")
        if not isinstance(line_id, str) or not CONSENSUS_LINE_ID_RE.fullmatch(line_id):
            errors.append(f"{prefix}.consensus_line_id is invalid")
        elif line_id in seen_ids:
            errors.append(f"duplicate consensus_line_id: {line_id}")
        else:
            seen_ids.add(line_id)
        if not _valid_bbox(line.get("bbox")):
            errors.append(f"{prefix}.bbox must fit top-left normalized_1000 bounds")
        exactness = line.get("exactness")
        if exactness not in {"observed", "unresolved"}:
            errors.append(f"{prefix}.exactness is invalid")
        text = line.get("text")
        if exactness == "observed" and (not isinstance(text, str) or not text):
            errors.append(f"{prefix} observed line requires non-empty text")
        if isinstance(text, str) and len(text) > 10_000:
            errors.append(f"{prefix}.text exceeds 10000 characters")
        if exactness == "unresolved":
            unresolved += 1
            if text is not None:
                errors.append(f"{prefix} unresolved line requires null text")
        readings = line.get("readings")
        if not isinstance(readings, list) or not 1 <= len(readings) <= 2:
            errors.append(f"{prefix}.readings must contain one or two readings")
            continue
        for reading_index, reading in enumerate(readings):
            reading_prefix = f"{prefix}.readings[{reading_index}]"
            if not isinstance(reading, dict) or set(reading) != READING_KEYS:
                errors.append(f"{reading_prefix} keys are incomplete or unknown")
                continue
            if not isinstance(reading.get("run_id"), str) or not RUN_ID_RE.fullmatch(reading["run_id"]):
                errors.append(f"{reading_prefix}.run_id is invalid")
            if not isinstance(reading.get("line_id"), str) or not LINE_ID_RE.fullmatch(reading["line_id"]):
                errors.append(f"{reading_prefix}.line_id is invalid")
            if not isinstance(reading.get("raw_text"), str) or not reading["raw_text"]:
                errors.append(f"{reading_prefix}.raw_text must be non-empty")
            elif len(reading["raw_text"]) > 10_000:
                errors.append(f"{reading_prefix}.raw_text exceeds 10000 characters")
            if not _valid_bbox(reading.get("bbox")):
                errors.append(f"{reading_prefix}.bbox is invalid")
    if consensus.get("unresolved_count") != unresolved:
        errors.append("consensus.unresolved_count does not match unresolved lines")


def validate(record: object) -> list[str]:
    """Validate one OCR record without consulting its external asset inputs."""
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
    if record.get("record_type") != "ocr_observation":
        errors.append("record_type must be ocr_observation")
    if not isinstance(record.get("observation_id"), str) or not OBSERVATION_ID_RE.fullmatch(record["observation_id"]):
        errors.append("observation_id is invalid")
    if not isinstance(record.get("asset_id"), str) or not ASSET_ID_RE.fullmatch(record["asset_id"]):
        errors.append("asset_id is invalid")
    asset = record.get("asset")
    if not isinstance(asset, dict) or set(asset) != ASSET_KEYS:
        errors.append("asset keys are incomplete or unknown")
    else:
        if not isinstance(asset.get("materialized_path"), str) or not asset["materialized_path"]:
            errors.append("asset.materialized_path must be non-empty")
        if not isinstance(asset.get("sha256"), str) or not SHA256_RE.fullmatch(asset["sha256"]):
            errors.append("asset.sha256 must be lowercase SHA-256")
        if (
            not isinstance(asset.get("mime_type"), str)
            or not MIME_RE.fullmatch(asset["mime_type"])
        ):
            errors.append("asset.mime_type must be an image MIME type")
        dimensions = asset.get("dimensions")
        if not isinstance(dimensions, dict) or set(dimensions) != {"width_px", "height_px"}:
            errors.append("asset.dimensions keys are incomplete or unknown")
        else:
            for key in ("width_px", "height_px"):
                value = dimensions.get(key)
                if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                    errors.append(f"asset.dimensions.{key} must be a positive integer")
    if not isinstance(record.get("source"), dict):
        errors.append("source must be an object")
    if not isinstance(record.get("origin"), dict):
        errors.append("origin must be an object")
    classification_ref = record.get("classification_ref")
    if not isinstance(classification_ref, dict) or set(classification_ref) != CLASSIFICATION_REF_KEYS:
        errors.append("classification_ref keys are incomplete or unknown")
    else:
        classification_id = classification_ref.get("classification_id")
        if (
            not isinstance(classification_id, str)
            or not CLASSIFICATION_ID_RE.fullmatch(classification_id)
        ):
            errors.append("classification_ref.classification_id is invalid")
        for key in ("output_sha256", "signature_sha256"):
            value = classification_ref.get(key)
            if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
                errors.append(f"classification_ref.{key} must be lowercase SHA-256")
        routes = _validate_string_array(
            classification_ref.get("routes"), "classification_ref.routes", errors
        )
        if any(route not in ROUTES for route in routes):
            errors.append("classification_ref.routes contains an invalid route")
        if "ocr_text" not in routes:
            errors.append("classification_ref.routes must include ocr_text")
    runs = record.get("engine_runs")
    if not isinstance(runs, list) or len(runs) != 2:
        errors.append("engine_runs must contain exactly Apple Vision then Tesseract")
        runs = [] if not isinstance(runs, list) else runs
    for index, run in enumerate(runs):
        _validate_run(run, index, errors)
    _validate_consensus_shape(record.get("consensus"), errors)
    expected_consensus: dict[str, Any] | None = None
    if len(runs) == 2 and isinstance(record.get("asset_id"), str):
        try:
            expected_consensus = build_consensus(record["asset_id"], runs)
        except (KeyError, TypeError, ValueError) as exc:
            errors.append(f"consensus cannot be recomputed safely: {exc}")
    if expected_consensus is not None:
        if record.get("consensus") != expected_consensus:
            errors.append("consensus does not match strict spatial/NFC recomputation")
        exactness = expected_exactness(expected_consensus, runs)
        if record.get("exactness") != exactness:
            errors.append(f"exactness must be recomputed as {exactness}")
        status = expected_status(exactness, runs)
        if record.get("status") != status:
            errors.append(f"status must be recomputed as {status}")
        warnings = expected_warnings(expected_consensus, runs)
        if record.get("warnings") != warnings:
            errors.append("warnings do not match deterministic run/consensus warnings")
    if record.get("exactness") not in {"observed", "unresolved"}:
        errors.append("exactness is invalid")
    _validate_string_array(record.get("warnings"), "warnings", errors)
    if record.get("status") not in {"observed", "needs_review", "failed"}:
        errors.append("status is invalid")
    hashes = record.get("hashes")
    if not isinstance(hashes, dict) or set(hashes) != HASH_KEYS:
        errors.append("hashes keys are incomplete or unknown")
    else:
        for key in HASH_KEYS:
            value = hashes.get(key)
            if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
                errors.append(f"hashes.{key} must be lowercase SHA-256")
        if hashes.get("output_sha256") != sha256_json(observation_output_payload(record)):
            errors.append("hashes.output_sha256 does not match OCR output payload")
        if len(runs) == 2 and all(isinstance(run, dict) for run in runs):
            expected_signature = observation_signature_sha256(
                str(hashes.get("input_sha256", "")), runs
            )
            if hashes.get("signature_sha256") != expected_signature:
                errors.append("hashes.signature_sha256 does not match OCR contract")
            if record.get("observation_id") != "ocr_" + expected_signature[:24]:
                errors.append("observation_id does not match signature")
    provenance = record.get("provenance")
    if not isinstance(provenance, dict) or set(provenance) != PROVENANCE_KEYS:
        errors.append("provenance keys are incomplete or unknown")
    else:
        if provenance.get("observer") != OBSERVER:
            errors.append(f"provenance.observer must be {OBSERVER}")
        if provenance.get("observer_version") != OBSERVER_VERSION:
            errors.append(f"provenance.observer_version must be {OBSERVER_VERSION}")
        if not isinstance(provenance.get("cache_hit"), bool):
            errors.append("provenance.cache_hit must be boolean")
        if provenance.get("question_independent") is not True:
            errors.append("provenance.question_independent must be true")
        if provenance.get("evidence_connected") is not False:
            errors.append("provenance.evidence_connected must be false")
        if provenance.get("search_unit_connected") is not False:
            errors.append("provenance.search_unit_connected must be false")
        _validate_timestamp(provenance.get("generated_at"), "provenance.generated_at", errors)
        if (
            len(runs) == 2
            and all(isinstance(run, dict) for run in runs)
            and isinstance(provenance.get("cache_hit"), bool)
        ):
            expected_cache_hit = all(
                isinstance(run.get("provenance"), dict)
                and run["provenance"].get("cache_hit") is True
                for run in runs
            )
            if provenance["cache_hit"] != expected_cache_hit:
                errors.append("provenance.cache_hit must equal all engine cache hits")
    return errors


def _load_published_schema() -> dict[str, Any]:
    try:
        value = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot load published schema {SCHEMA_PATH}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError("published OCR schema must be an object")
    if value.get("$schema") != "https://json-schema.org/draft/2020-12/schema":
        raise ValueError("published OCR schema must use JSON Schema Draft 2020-12")
    if value.get("additionalProperties") is not False:
        raise ValueError("published OCR schema must reject unknown root properties")
    if set(value.get("required", [])) != ROOT_KEYS:
        raise ValueError("published OCR schema required fields do not match validator contract")
    if set(value.get("properties", {})) != ROOT_KEYS:
        raise ValueError("published OCR schema properties do not match validator contract")
    engine_schema = value.get("properties", {}).get("engine_runs", {})
    prefix_items = engine_schema.get("prefixItems") if isinstance(engine_schema, dict) else None
    if not isinstance(prefix_items, list) or len(prefix_items) != 2:
        raise ValueError("published OCR schema must explicitly define both engines")
    return value


def _compile_published_schema(schema: dict[str, Any]) -> tuple[Any | None, str]:
    try:
        import jsonschema
    except ImportError as exc:
        raise ValueError(
            "jsonschema is required for OCR Draft 2020-12 validation"
        ) from exc
    try:
        jsonschema.Draft202012Validator.check_schema(schema)
        validator = jsonschema.Draft202012Validator(
            schema, format_checker=jsonschema.FormatChecker()
        )
    except Exception as exc:
        raise ValueError(f"invalid published OCR Draft 2020-12 schema: {exc}") from exc
    return validator, "jsonschema_draft202012_format"


def _schema_errors(record: object, validator: Any | None) -> list[str]:
    if validator is None:
        return []
    errors: list[str] = []
    for error in sorted(
        validator.iter_errors(record),
        key=lambda item: tuple(str(component) for component in item.absolute_path),
    ):
        location = ".".join(str(component) for component in error.absolute_path) or "root"
        errors.append(f"schema {location}: {error.message}")
    return errors


def _load_jsonl_limited(path: Path, label: str) -> list[dict[str, Any]]:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"{label} must be a regular non-symlink JSONL file: {path}")
    size = path.stat().st_size
    if size > MAX_JSONL_BYTES:
        raise ValueError(
            f"{label} exceeds {MAX_JSONL_BYTES} byte safety limit: {size}"
        )
    records = classifier.load_jsonl(path)
    if len(records) > MAX_JSONL_RECORDS:
        raise ValueError(
            f"{label} exceeds {MAX_JSONL_RECORDS} record safety limit"
        )
    return records


def _reject_symlink_or_escape(asset: dict[str, Any], asset_root: Path) -> None:
    root = asset_root.absolute()
    if root.is_symlink():
        raise ValueError(f"asset_root must not be a symlink: {asset_root}")
    root = root.resolve()
    declared = Path(asset["declared_path"])
    lexical = declared if declared.is_absolute() else root / declared
    lexical = Path(os.path.abspath(lexical))
    try:
        relative = lexical.relative_to(root)
    except ValueError as exc:
        raise ValueError(
            f"{asset['asset_id']}: materialized path escapes asset_root: {asset['declared_path']}"
        ) from exc
    current = root
    for component in relative.parts:
        current = current / component
        if current.is_symlink():
            raise ValueError(f"{asset['asset_id']}: materialized path contains symlink: {current}")
    if lexical.resolve() != asset["path"]:
        raise ValueError(f"{asset['asset_id']}: materialized path resolution changed")


def _actual_image_metadata(data: bytes, path: Path) -> tuple[str, int, int]:
    try:
        from PIL import Image
    except ImportError as exc:
        raise ValueError("Pillow is required to verify OCR input images") from exc
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
        with Image.open(io.BytesIO(data)) as image:
            image.load()
            if int(image.width) != width or int(image.height) != height:
                raise ValueError("image dimensions changed during full decode")
    except Exception as exc:
        raise ValueError(f"cannot fully decode OCR input image {path}: {exc}") from exc
    mime_type = Image.MIME.get(image_format)
    if not mime_type:
        raise ValueError(f"cannot determine OCR input image MIME type: {path}")
    return mime_type, width, height


def _verify_actual_asset(asset: dict[str, Any], asset_root: Path) -> None:
    _reject_symlink_or_escape(asset, asset_root)
    encoded_size = asset["path"].stat().st_size
    if encoded_size > MAX_IMAGE_BYTES:
        raise ValueError(
            f"{asset['asset_id']}: image exceeds {MAX_IMAGE_BYTES} encoded byte safety limit: "
            f"{encoded_size}"
        )
    data = classifier.verified_asset_bytes(asset)
    mime_type, width, height = _actual_image_metadata(data, asset["path"])
    if mime_type != asset["mime_type"]:
        raise ValueError(
            f"{asset['asset_id']}: image MIME mismatch: declared={asset['mime_type']} actual={mime_type}"
        )
    dimensions = {"width_px": width, "height_px": height}
    if dimensions != asset["dimensions"]:
        raise ValueError(
            f"{asset['asset_id']}: image dimensions mismatch: "
            f"declared={asset['dimensions']} actual={dimensions}"
        )


def _validate_against_inputs(
    record: object,
    asset: dict[str, Any],
    classification: dict[str, Any],
    position: int,
) -> list[str]:
    if not isinstance(record, dict):
        return [f"record {position}: OCR observation must be an object"]
    errors: list[str] = []
    if record.get("asset_id") != asset["asset_id"]:
        errors.append(
            f"record {position}: asset_id/order mismatch: "
            f"ocr={record.get('asset_id')} expected={asset['asset_id']}"
        )
    if record.get("asset") != expected_asset_envelope(asset):
        errors.append(f"record {position}: asset metadata does not match --assets")
    if record.get("source") != asset["source"]:
        errors.append(f"record {position}: source does not match --assets")
    if record.get("origin") != asset["origin"]:
        errors.append(f"record {position}: origin does not match --assets")
    if record.get("classification_ref") != expected_classification_ref(classification):
        errors.append(f"record {position}: classification_ref does not match --classifications")
    hashes = record.get("hashes")
    expected_input = sha256_json(observation_input_payload(asset, classification))
    if not isinstance(hashes, dict) or hashes.get("input_sha256") != expected_input:
        errors.append(f"record {position}: hashes.input_sha256 does not bind the upstream inputs")
    runs = record.get("engine_runs")
    if isinstance(runs, list) and len(runs) == 2:
        for run_index, run in enumerate(runs):
            if not isinstance(run, dict):
                continue
            expected_name = ENGINE_ORDER[run_index]
            if run.get("engine") != expected_engine(expected_name):
                errors.append(
                    f"record {position}: engine_runs[{run_index}].engine does not match "
                    f"the current {expected_name} runtime fingerprint"
                )
            run_hashes = run.get("hashes")
            if not isinstance(run_hashes, dict):
                continue
            expected_run_input = engine_input_sha256(asset)
            if run_hashes.get("input_sha256") != expected_run_input:
                errors.append(
                    f"record {position}: engine_runs[{run_index}].hashes.input_sha256 is invalid"
                )
            expected_signature = engine_signature_sha256(asset, run)
            if run_hashes.get("signature_sha256") != expected_signature:
                errors.append(
                    f"record {position}: engine_runs[{run_index}].hashes.signature_sha256 is invalid"
                )
            if run.get("run_id") != expected_run_id(expected_signature):
                errors.append(f"record {position}: engine_runs[{run_index}].run_id is invalid")
        if isinstance(hashes, dict):
            expected_signature = observation_signature_sha256(expected_input, runs)
            if hashes.get("signature_sha256") != expected_signature:
                errors.append(f"record {position}: hashes.signature_sha256 is invalid")
            if record.get("observation_id") != "ocr_" + expected_signature[:24]:
                errors.append(f"record {position}: observation_id is invalid")
    return errors


def validate_jsonl(
    observations_path: Path,
    assets_path: Path,
    classifications_path: Path,
    *,
    asset_root: Path | None = None,
    expected_count: int | None = None,
) -> dict[str, Any]:
    if expected_count is not None and expected_count < 1:
        raise ValueError("expected_count must be positive or null")
    records = _load_jsonl_limited(observations_path, "OCR observations")
    raw_assets = _load_jsonl_limited(assets_path, "--assets")
    classifications = _load_jsonl_limited(classifications_path, "--classifications")
    if not records:
        raise ValueError("OCR observation JSONL contains no records")
    if not raw_assets:
        raise ValueError("--assets JSONL contains no records")
    if not classifications:
        raise ValueError("--classifications JSONL contains no records")
    root = (asset_root or assets_path.parent).absolute()
    if root.is_symlink():
        raise ValueError(f"asset_root must not be a symlink: {root}")
    root = root.resolve()
    if not root.is_dir():
        raise ValueError(f"asset_root is not a directory: {root}")

    # Enforce encoded-size and pixel limits before the upstream classification
    # validator is allowed to materialize any image bytes of its own.
    assets = [classifier.normalize_asset(record, root) for record in raw_assets]
    for asset in assets:
        _verify_actual_asset(asset, root)
    # Then prove that the complete classification batch is still exactly bound
    # to the complete materialized batch before selecting the OCR route.
    classification_validator.validate_jsonl(
        classifications_path, assets_path, asset_root=root
    )
    if len(assets) != len(classifications):
        raise ValueError(
            f"upstream count mismatch: assets={len(assets)} classifications={len(classifications)}"
        )
    eligible: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for position, (asset, classification) in enumerate(zip(assets, classifications), 1):
        if classification.get("asset_id") != asset["asset_id"]:
            raise ValueError(
                f"upstream record {position}: asset/classification order mismatch"
            )
        routes = classification.get("routes")
        if isinstance(routes, list) and "ocr_text" in routes:
            eligible.append((asset, classification))
    if expected_count is not None and len(eligible) != expected_count:
        raise ValueError(
            f"eligible OCR asset count mismatch: expected={expected_count} actual={len(eligible)}"
        )
    if len(records) != len(eligible):
        raise ValueError(
            f"OCR record count mismatch: observations={len(records)} eligible={len(eligible)}"
        )

    schema = _load_published_schema()
    schema_validator, schema_validation = _compile_published_schema(schema)
    all_errors: list[str] = []
    seen_assets: set[str] = set()
    stats: dict[str, Any] = {
        "records": 0,
        "eligible": len(eligible),
        "observed": 0,
        "needs_review": 0,
        "failed": 0,
        "consensus_lines": 0,
        "unresolved_lines": 0,
        "schema_validation": schema_validation,
        "engines": list(ENGINE_ORDER),
    }
    for line_number, record in enumerate(records, 1):
        errors = validate(record)
        errors.extend(_schema_errors(record, schema_validator))
        all_errors.extend(f"line {line_number}: {error}" for error in errors)
        if isinstance(record, dict):
            asset_id = record.get("asset_id")
            if isinstance(asset_id, str):
                if asset_id in seen_assets:
                    all_errors.append(f"line {line_number}: duplicate asset_id: {asset_id}")
                seen_assets.add(asset_id)
            status = record.get("status")
            if status in {"observed", "needs_review", "failed"}:
                stats[status] += 1
            consensus = record.get("consensus")
            if isinstance(consensus, dict) and isinstance(consensus.get("lines"), list):
                stats["consensus_lines"] += len(consensus["lines"])
                stats["unresolved_lines"] += sum(
                    isinstance(item, dict) and item.get("exactness") == "unresolved"
                    for item in consensus["lines"]
                )
        asset, classification = eligible[line_number - 1]
        all_errors.extend(
            _validate_against_inputs(record, asset, classification, line_number)
        )
        stats["records"] += 1
    if all_errors:
        raise ValueError("\n".join(all_errors))
    return stats


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate image-bound Apple Vision/Tesseract OCR observation JSONL."
    )
    parser.add_argument("observations", type=Path)
    parser.add_argument("--assets", type=Path, required=True)
    parser.add_argument("--classifications", type=Path, required=True)
    parser.add_argument(
        "--asset-root", type=Path,
        help="root containing materialized images (default: --assets parent)",
    )
    parser.add_argument(
        "--expected-count", type=int,
        help="optional required ocr_text asset count (otherwise derive it from classifications)",
    )
    args = parser.parse_args()
    try:
        stats = validate_jsonl(
            args.observations,
            args.assets,
            args.classifications,
            asset_root=args.asset_root,
            expected_count=args.expected_count,
        )
    except (OSError, ValueError) as exc:
        for line in str(exc).splitlines():
            print(f"ERROR: {line}")
        return 1
    print(json.dumps(stats, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
