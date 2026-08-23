#!/usr/bin/env python3
"""Run an isolated, question-independent Docling structural extraction PoC.

The runner deliberately consumes only the verified OCR fixture manifest and the
materialized images it references. It never opens source documents, questions,
gold labels, predictions, or answers. Docling is evaluated as a DocumentGraph
candidate (layout + OCR + table structure), not as an OCR engine alone.
"""

from __future__ import annotations

import argparse
import copy
import datetime as dt
import hashlib
import importlib.metadata
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time
import warnings as py_warnings
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence

from jsonschema import Draft202012Validator, FormatChecker


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import ocr_poc_contract as ocr_contract  # noqa: E402


SCHEMA_PATH = ROOT / "schemas" / "docling-poc-run.schema.json"
RUNNER_VERSION = "0.2"
MAX_MANIFEST_BYTES = 64 * 1024 * 1024
MAX_MANIFEST_RECORDS = 10000
FORBIDDEN_DATA_RE = re.compile(
    r"(?:^|[-_.])(questions?|gold|predictions?|answers?)(?:[-_.]|$)", re.IGNORECASE
)
LAYOUT_FOLDER = "docling-project--docling-layout-heron"
TABLEFORMER_FOLDER = "docling-project--docling-models"
TABLEFORMER_ACCURATE = Path("model_artifacts/tableformer/accurate")


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


def _safe_relative_path(raw: str, label: str) -> Path:
    path = Path(raw)
    if path.is_absolute() or not path.parts:
        raise ValueError(f"{label} must be a non-empty relative path")
    if any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError(f"{label} contains an unsafe path component")
    return path


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _has_symlink_component(path: Path, trusted_root: Path) -> bool:
    absolute = path if path.is_absolute() else Path.cwd() / path
    root = trusted_root if trusted_root.is_absolute() else Path.cwd() / trusted_root
    try:
        relative = absolute.relative_to(root)
    except ValueError:
        return True
    current = root
    if current.is_symlink():
        return True
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            return True
    return False


def _resolve_repo_file(repository_root: Path, relative: str, label: str) -> Path:
    rel = _safe_relative_path(relative, label)
    root = repository_root.resolve(strict=True)
    candidate = repository_root / rel
    if _has_symlink_component(candidate, repository_root) or not candidate.is_file():
        raise ValueError(f"{label} must be a regular non-symlink file: {candidate}")
    resolved = candidate.resolve(strict=True)
    if not _inside(resolved, root):
        raise ValueError(f"{label} escapes repository root")
    return resolved


def _assert_safe_manifest_path(path: Path, repository_root: Path) -> None:
    if FORBIDDEN_DATA_RE.search(path.name):
        raise ValueError(
            "manifest filename looks like prohibited question/gold/prediction/answer data"
        )
    if _has_symlink_component(path, repository_root) or not path.is_file():
        raise ValueError(f"manifest must be a regular non-symlink file: {path}")
    if path.stat().st_size > MAX_MANIFEST_BYTES:
        raise ValueError(f"manifest exceeds {MAX_MANIFEST_BYTES} bytes")


def _assert_no_forbidden_data_path(raw: str, label: str) -> None:
    relative = _safe_relative_path(raw, label)
    if any(FORBIDDEN_DATA_RE.search(part) for part in relative.parts):
        raise ValueError(
            f"{label} looks like prohibited question/gold/prediction/answer data"
        )


def load_verified_manifest(
    path: Path, repository_root: Path = ROOT
) -> list[dict[str, Any]]:
    """Load only the question-independent OCR fixture manifest."""

    _assert_safe_manifest_path(path, repository_root)
    records: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, raw in enumerate(handle, 1):
            if not raw.strip():
                raise ValueError(f"{path}:{line_number}: blank JSONL line")
            try:
                record = json.loads(raw, object_pairs_hook=_reject_duplicate_keys)
            except (json.JSONDecodeError, ValueError) as exc:
                raise ValueError(f"{path}:{line_number}: invalid JSON: {exc}") from exc
            if not isinstance(record, dict):
                raise ValueError(f"{path}:{line_number}: record must be an object")
            provenance = record.get("provenance")
            if not isinstance(provenance, dict):
                raise ValueError(f"{path}:{line_number}: missing provenance")
            required_flags = {
                "question_independent": True,
                "question_data_used": False,
                "answer_data_used": False,
                "prediction_data_used": False,
            }
            for key, expected in required_flags.items():
                if provenance.get(key) is not expected:
                    raise ValueError(
                        f"{path}:{line_number}: unsafe provenance flag {key}"
                    )
            if record.get("record_type") != "ocr_poc_fixture":
                raise ValueError(f"{path}:{line_number}: unexpected record_type")
            reference = record.get("reference")
            if not isinstance(reference, dict) or reference.get("status") != "verified":
                raise ValueError(f"{path}:{line_number}: verified fixture required")
            asset_ref = record.get("asset_ref")
            if not isinstance(asset_ref, dict):
                raise ValueError(f"{path}:{line_number}: missing asset_ref")
            _assert_no_forbidden_data_path(
                str(asset_ref.get("materialized_path", "")), "materialized_path"
            )
            _assert_no_forbidden_data_path(
                str(asset_ref.get("source_relative_path", "")),
                "source_relative_path",
            )
            fixture_errors = ocr_contract.validate_fixture(
                record,
                repository_root=repository_root,
                require_verified=True,
            )
            if fixture_errors:
                raise ValueError(
                    f"{path}:{line_number}: invalid OCR fixture: "
                    + "; ".join(fixture_errors)
                )
            records.append(record)
            if len(records) > MAX_MANIFEST_RECORDS:
                raise ValueError(
                    f"manifest exceeds {MAX_MANIFEST_RECORDS} records"
                )
    if not records:
        raise ValueError("fixture manifest is empty")
    return records


def _role_for_asset(asset_ref: Mapping[str, Any]) -> Optional[str]:
    origin = asset_ref.get("origin_kind")
    if origin == "pdf_page":
        return "image_only_pdf_page"
    if origin == "office_embedded_image":
        return "office_embedded_table_image"
    if origin == "standalone_image":
        return "standalone_table_image"
    return None


def sample_id_payload(sample: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: copy.deepcopy(value)
        for key, value in sample.items()
        if key != "sample_id"
    }


def expected_sample_id(sample: Mapping[str, Any]) -> str:
    return "docsrc_" + sha256_json(sample_id_payload(sample))[:24]


def select_structural_samples(fixtures: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Select one full image from two structural strata without question data."""

    grouped: dict[str, dict[str, Any]] = {}
    group_order: list[str] = []
    for fixture in fixtures:
        asset_ref = fixture.get("asset_ref")
        strata = fixture.get("strata")
        if not isinstance(asset_ref, Mapping) or not isinstance(strata, Mapping):
            raise ValueError("fixture is missing asset_ref or strata")
        routes = strata.get("routes")
        if not isinstance(routes, list) or "table_structure" not in routes:
            continue
        role = _role_for_asset(asset_ref)
        if role is None:
            continue
        asset_id = str(asset_ref.get("asset_id", ""))
        if not asset_id:
            raise ValueError("structural fixture has no asset_id")
        if asset_id not in grouped:
            grouped[asset_id] = {
                "role": role,
                "asset_ref": dict(asset_ref),
                "fixture_refs": [],
            }
            group_order.append(asset_id)
        group = grouped[asset_id]
        if group["role"] != role or group["asset_ref"] != dict(asset_ref):
            raise ValueError(f"inconsistent asset metadata for {asset_id}")
        fixture_id = fixture.get("fixture_id")
        signature = fixture.get("hashes", {}).get("signature_sha256")
        if not isinstance(fixture_id, str) or not isinstance(signature, str):
            raise ValueError("structural fixture has no stable fixture signature")
        group["fixture_refs"].append(
            {"fixture_id": fixture_id, "signature_sha256": signature}
        )

    by_role: dict[str, list[dict[str, Any]]] = {}
    for asset_id in group_order:
        group = grouped[asset_id]
        by_role.setdefault(group["role"], []).append(group)

    selected: list[dict[str, Any]] = []
    for role in ("image_only_pdf_page", "office_embedded_table_image"):
        candidates = by_role.get(role, [])
        if candidates:
            selected.append(candidates[0])
    if len(selected) < 2:
        candidates = by_role.get("standalone_table_image", [])
        if candidates:
            selected.append(candidates[0])
    if len(selected) < 2 or len({sample["role"] for sample in selected}) < 2:
        raise ValueError(
            "need at least two structural input roles: a PDF-page image and an office/standalone table image"
        )

    output: list[dict[str, Any]] = []
    for group in selected[:2]:
        asset_ref = group["asset_ref"]
        base = {
            "role": group["role"],
            "fixture_refs": sorted(
                group["fixture_refs"], key=lambda item: item["fixture_id"]
            ),
            "materialized_path": asset_ref["materialized_path"],
            "image_sha256": asset_ref["image_sha256"],
            "dimensions": asset_ref["dimensions"],
            "origin_kind": asset_ref["origin_kind"],
            "source_relative_path": asset_ref["source_relative_path"],
            "source_sha256": asset_ref["source_sha256"],
            "page_number": asset_ref["page_number"],
        }
        base["sample_id"] = expected_sample_id(base)
        output.append(base)
    return output


def _regular_tree_files(root: Path) -> list[Path]:
    if root.is_symlink() or not root.is_dir():
        raise ValueError(f"model directory is unavailable: {root}")
    resolved_root = root.resolve(strict=True)
    files: list[Path] = []
    for path in sorted(root.rglob("*"), key=lambda value: value.as_posix()):
        if not path.is_file():
            continue
        resolved = path.resolve(strict=True)
        if not _inside(resolved, resolved_root):
            raise ValueError(f"model file escapes its model directory: {path}")
        files.append(path)
    if not files:
        raise ValueError(f"model directory contains no files: {root}")
    return files


def fingerprint_tree(
    path: Path,
    *,
    relative_to: Path,
    repo_id: str,
    revision: str,
) -> dict[str, Any]:
    files = _regular_tree_files(path)
    digest = hashlib.sha256()
    size_bytes = 0
    for file_path in files:
        relative = file_path.relative_to(path).as_posix()
        file_size = file_path.stat().st_size
        file_hash = sha256_file(file_path)
        size_bytes += file_size
        digest.update(
            canonical_json(
                {"path": relative, "size_bytes": file_size, "sha256": file_hash}
            ).encode("utf-8")
        )
        digest.update(b"\n")
    return {
        "repo_id": repo_id,
        "revision": revision,
        "relative_path": path.relative_to(relative_to).as_posix(),
        "file_count": len(files),
        "size_bytes": size_bytes,
        "sha256": digest.hexdigest(),
    }


def check_local_models(models_dir: Path) -> tuple[Path, Path]:
    if models_dir.is_symlink() or not models_dir.is_dir():
        raise ValueError(f"--models-dir must be a real directory: {models_dir}")
    layout = models_dir / LAYOUT_FOLDER
    tableformer = models_dir / TABLEFORMER_FOLDER / TABLEFORMER_ACCURATE
    _regular_tree_files(layout)
    _regular_tree_files(tableformer)
    return layout, tableformer


def _distribution_version(name: str) -> str:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return "unavailable"


def _tesseract_version() -> str:
    completed = subprocess.run(
        ["tesseract", "--version"],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    first = (completed.stdout or completed.stderr).splitlines()
    if completed.returncode != 0 or not first:
        raise ValueError("local Tesseract CLI is unavailable")
    return first[0].strip()


def _fingerprint_files(values: Sequence[tuple[str, Path]]) -> str:
    digest = hashlib.sha256()
    if not values:
        raise ValueError("cannot fingerprint an empty OCR artifact set")
    for label, path in sorted(values, key=lambda item: item[0]):
        if not path.is_file():
            raise ValueError(f"OCR artifact is unavailable: {path}")
        payload = {
            "label": label,
            "size_bytes": path.stat().st_size,
            "sha256": sha256_file(path),
        }
        digest.update(canonical_json(payload).encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def _tesseract_artifacts_sha256() -> str:
    binary_raw = shutil.which("tesseract")
    if not binary_raw:
        raise ValueError("local Tesseract CLI is unavailable")
    completed = subprocess.run(
        [binary_raw, "--list-langs"],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    if completed.returncode != 0:
        raise ValueError("Tesseract language inventory is unavailable")
    match = re.search(r'"([^"]+)"', completed.stdout or completed.stderr)
    if not match:
        raise ValueError("could not locate Tesseract traineddata directory")
    tessdata = Path(match.group(1))
    return _fingerprint_files(
        [
            ("binary", Path(binary_raw).resolve(strict=True)),
            ("traineddata/eng", tessdata / "eng.traineddata"),
            ("traineddata/jpn", tessdata / "jpn.traineddata"),
        ]
    )


def _ocrmac_artifacts_sha256() -> str:
    try:
        distribution = importlib.metadata.distribution("ocrmac")
    except importlib.metadata.PackageNotFoundError as exc:
        raise ValueError("ocrmac distribution is unavailable") from exc
    values: list[tuple[str, Path]] = []
    for entry in distribution.files or []:
        located = Path(distribution.locate_file(entry))
        if located.is_file() and "__pycache__" not in located.parts:
            values.append((str(entry), located))
    return _fingerprint_files(values)


def _ocr_pipeline_configuration(ocr_engine: str) -> dict[str, Any]:
    if ocr_engine == "tesseract_cli":
        return {
            "enabled": True,
            "engine": "tesseract_cli",
            "languages": ["jpn", "eng"],
            "force_full_page": True,
        }
    if ocr_engine == "ocrmac":
        return {
            "enabled": True,
            "engine": "ocrmac",
            "languages": ["ja-JP", "en-US"],
            "force_full_page": True,
            "recognition": "accurate",
            "framework": "vision",
        }
    raise ValueError(f"unsupported OCR engine: {ocr_engine}")


def _ocr_engine_fingerprint(
    ocr_engine: str, configuration: Mapping[str, Any]
) -> dict[str, str]:
    if ocr_engine == "tesseract_cli":
        version = _tesseract_version()
        runtime = version
        artifacts_sha256 = _tesseract_artifacts_sha256()
    elif ocr_engine == "ocrmac":
        version = _distribution_version("ocrmac")
        if version == "unavailable":
            raise ValueError(
                "ocrmac is unavailable; install Docling's pinned macOS OCR extra "
                "with `python -m pip install \"docling-slim[feat-ocr-mac]==2.115.0\"`"
            )
        mac_version = platform.mac_ver()[0] or platform.release()
        runtime = f"macOS Vision {mac_version} ({platform.machine()})"
        artifacts_sha256 = _ocrmac_artifacts_sha256()
    else:
        raise ValueError(f"unsupported OCR engine: {ocr_engine}")
    config_sha256 = sha256_json(configuration)
    payload = {
        "engine": ocr_engine,
        "version": version,
        "runtime": runtime,
        "artifacts_sha256": artifacts_sha256,
        "config_sha256": config_sha256,
    }
    return {
        **payload,
        "fingerprint_sha256": sha256_json(payload),
    }


def package_fingerprint_payload(
    *,
    versions: Mapping[str, str],
    python_version: str,
    platform_value: str,
    num_threads: int,
    ocr_engine_fingerprint: Mapping[str, str],
) -> dict[str, Any]:
    return {
        "versions": dict(versions),
        "python_version": python_version,
        "platform": platform_value,
        "num_threads": num_threads,
        "ocr_engine_fingerprint": dict(ocr_engine_fingerprint),
    }


def build_configuration(
    models_dir: Path, num_threads: int, ocr_engine: str = "tesseract_cli"
) -> dict[str, Any]:
    layout_path, tableformer_path = check_local_models(models_dir)
    versions = {
        "docling": _distribution_version("docling"),
        "docling_core": _distribution_version("docling-core"),
        "docling_ibm_models": _distribution_version("docling-ibm-models"),
        "docling_parse": _distribution_version("docling-parse"),
        "torch": _distribution_version("torch"),
        "torchvision": _distribution_version("torchvision"),
    }
    if ocr_engine == "tesseract_cli":
        versions["tesseract"] = _tesseract_version()
    elif ocr_engine == "ocrmac":
        versions["ocrmac"] = _distribution_version("ocrmac")
    else:
        raise ValueError(f"unsupported OCR engine: {ocr_engine}")
    ocr_configuration = _ocr_pipeline_configuration(ocr_engine)
    ocr_fingerprint = _ocr_engine_fingerprint(ocr_engine, ocr_configuration)
    python_version = platform.python_version()
    platform_value = platform.platform()
    package_payload = package_fingerprint_payload(
        versions=versions,
        python_version=python_version,
        platform_value=platform_value,
        num_threads=num_threads,
        ocr_engine_fingerprint=ocr_fingerprint,
    )
    return {
        "runner_version": RUNNER_VERSION,
        "python_version": python_version,
        "platform": platform_value,
        "num_threads": num_threads,
        "package_versions": versions,
        "package_fingerprint_sha256": sha256_json(package_payload),
        "ocr_engine_fingerprint": ocr_fingerprint,
        "pipeline": {
            "input_format": "image",
            "pipeline": "standard_pdf_pipeline",
            "remote_services": False,
            "external_plugins": False,
            "ocr": ocr_configuration,
            "layout": {
                "enabled": True,
                "model": "docling-project/docling-layout-heron",
                "revision": "main",
                "device_requested": "mps",
            },
            "table_structure": {
                "enabled": True,
                "model": "docling-project/docling-models",
                "revision": "v2.3.0",
                "mode": "accurate",
                "cell_matching": True,
                "device_requested": "mps",
                "device_effective": "cpu",
                "mps_forced_cpu": True,
                "fallback_reason": (
                    "Docling 2.115.0 TableStructureModel explicitly replaces "
                    "an MPS device with CPU because MPS is currently slower."
                ),
            },
        },
        "models": {
            "layout": fingerprint_tree(
                layout_path,
                relative_to=models_dir,
                repo_id="docling-project/docling-layout-heron",
                revision="main",
            ),
            "tableformer": fingerprint_tree(
                tableformer_path,
                relative_to=models_dir,
                repo_id="docling-project/docling-models",
                revision="v2.3.0",
            ),
        },
    }


def _enum_value(value: Any) -> str:
    enum_value = getattr(value, "value", value)
    return str(enum_value)


def _bbox(value: Any) -> Optional[dict[str, Any]]:
    if value is None:
        return None
    if isinstance(value, Mapping):
        get = value.get
    else:
        get = lambda name, default=None: getattr(value, name, default)
    origin = get("coord_origin", "TOPLEFT")
    origin_value = _enum_value(origin)
    if origin_value not in {"TOPLEFT", "BOTTOMLEFT"}:
        origin_value = origin_value.upper()
    return {
        "l": float(get("l")),
        "t": float(get("t")),
        "r": float(get("r")),
        "b": float(get("b")),
        "coord_origin": origin_value,
    }


def _provenance(values: Iterable[Any]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for value in values:
        if isinstance(value, Mapping):
            page_no = value.get("page_no")
            bbox = value.get("bbox")
            charspan = value.get("charspan", (0, 0))
        else:
            page_no = getattr(value, "page_no")
            bbox = getattr(value, "bbox")
            charspan = getattr(value, "charspan", (0, 0))
        span = list(charspan)
        output.append(
            {
                "page_number": int(page_no),
                "bbox": _bbox(bbox),
                "charspan": [max(0, int(span[0])), max(0, int(span[1]))],
            }
        )
    return output


def _empty_item_counts() -> dict[str, int]:
    return {
        "total": 0,
        "text": 0,
        "title": 0,
        "section_header": 0,
        "list_item": 0,
        "table": 0,
        "picture": 0,
        "code": 0,
        "formula": 0,
        "caption": 0,
        "page_header": 0,
        "page_footer": 0,
        "other": 0,
    }


def empty_document() -> dict[str, Any]:
    return {
        "name": None,
        "markdown": None,
        "docling_json": None,
        "item_counts": _empty_item_counts(),
        "pages": [],
        "items": [],
        "tables": [],
        "markdown_sha256": None,
        "docling_json_sha256": None,
    }


def summarize_document(document: Any) -> dict[str, Any]:
    markdown = document.export_to_markdown()
    docling_json = canonical_json(
        document.export_to_dict(
            mode="json", by_alias=True, exclude_none=True, coord_precision=6,
            confid_precision=6
        )
    )
    counts = _empty_item_counts()
    items: list[dict[str, Any]] = []
    known_labels = set(counts) - {"total", "other"}
    for item, depth in document.iterate_items(with_groups=False):
        label = _enum_value(getattr(item, "label", "unknown"))
        bucket = label if label in known_labels else "other"
        counts[bucket] += 1
        counts["total"] += 1
        text_value = getattr(item, "text", None)
        items.append(
            {
                "self_ref": str(getattr(item, "self_ref", "#/unknown")),
                "label": label,
                "depth": int(depth),
                "text": text_value if isinstance(text_value, str) else None,
                "provenance": _provenance(getattr(item, "prov", [])),
            }
        )

    pages: list[dict[str, Any]] = []
    page_values = getattr(document, "pages", {})
    for key, page in sorted(page_values.items(), key=lambda pair: int(pair[0])):
        size = getattr(page, "size")
        pages.append(
            {
                "page_number": int(getattr(page, "page_no", key)),
                "width": float(getattr(size, "width")),
                "height": float(getattr(size, "height")),
            }
        )

    tables: list[dict[str, Any]] = []
    for table_index, table in enumerate(getattr(document, "tables", [])):
        data = getattr(table, "data")
        cells: list[dict[str, Any]] = []
        for cell in getattr(data, "table_cells", []):
            cells.append(
                {
                    "row_start": int(getattr(cell, "start_row_offset_idx")),
                    "row_end": int(getattr(cell, "end_row_offset_idx")),
                    "column_start": int(getattr(cell, "start_col_offset_idx")),
                    "column_end": int(getattr(cell, "end_col_offset_idx")),
                    "row_span": int(getattr(cell, "row_span", 1)),
                    "column_span": int(getattr(cell, "col_span", 1)),
                    "text": str(getattr(cell, "text", "")),
                    "column_header": bool(getattr(cell, "column_header", False)),
                    "row_header": bool(getattr(cell, "row_header", False)),
                    "row_section": bool(getattr(cell, "row_section", False)),
                    "bbox": _bbox(getattr(cell, "bbox", None)),
                }
            )
        tables.append(
            {
                "table_index": table_index,
                "self_ref": str(getattr(table, "self_ref", f"#/tables/{table_index}")),
                "label": _enum_value(getattr(table, "label", "table")),
                "rows": int(getattr(data, "num_rows", 0)),
                "columns": int(getattr(data, "num_cols", 0)),
                "provenance": _provenance(getattr(table, "prov", [])),
                "cells": cells,
            }
        )

    return {
        "name": str(getattr(document, "name", "")) or None,
        "markdown": markdown,
        "docling_json": docling_json,
        "item_counts": counts,
        "pages": pages,
        "items": items,
        "tables": tables,
        "markdown_sha256": sha256_bytes(markdown.encode("utf-8")),
        "docling_json_sha256": sha256_bytes(docling_json.encode("utf-8")),
    }


def _stage_timings(result: Any) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for stage, item in sorted(getattr(result, "timings", {}).items()):
        times = getattr(item, "times", [])
        output.append(
            {
                "stage": str(stage),
                "elapsed_seconds": round(sum(float(value) for value in times), 6),
                "count": int(getattr(item, "count", len(times))),
            }
        )
    return output


def _result_errors(result: Any) -> list[dict[str, str]]:
    output: list[dict[str, str]] = []
    for value in getattr(result, "errors", []):
        output.append(
            {
                "component": _enum_value(getattr(value, "component_type", "docling")),
                "type": _enum_value(getattr(value, "error_kind", type(value).__name__)),
                "message": str(getattr(value, "error_message", value)),
            }
        )
    return output


def _output_payload(record: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "status": record["status"],
        "document": record["document"],
        "warnings": record["warnings"],
        "errors": record["errors"],
    }


def _signature_payload(record: Mapping[str, Any]) -> dict[str, Any]:
    payload = {
        "schema_version": record["schema_version"],
        "record_type": record["record_type"],
        "sample_id": record["input"]["sample_id"],
        "input_sha256": record["hashes"]["input_sha256"],
        "package_fingerprint_sha256": record["configuration"][
            "package_fingerprint_sha256"
        ],
        "model_fingerprints": {
            name: value["sha256"]
            for name, value in record["configuration"]["models"].items()
        },
        "output_sha256": record["hashes"]["output_sha256"],
        "runner_version": record["configuration"]["runner_version"],
    }
    # Records created before the two-engine comparison had no explicit OCR
    # fingerprint. Preserve their signatures while binding every new run to
    # its OCR engine, version, runtime, and exact configuration.
    ocr_fingerprint = record["configuration"].get("ocr_engine_fingerprint")
    if ocr_fingerprint is not None:
        payload["ocr_engine_fingerprint_sha256"] = ocr_fingerprint[
            "fingerprint_sha256"
        ]
    return payload


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
    if record["hashes"]["output_sha256"] != sha256_json(_output_payload(record)):
        errors.append("/hashes/output_sha256: does not match output payload")
    signature = sha256_json(_signature_payload(record))
    if record["hashes"]["signature_sha256"] != signature:
        errors.append("/hashes/signature_sha256: does not match signature payload")
    if record["run_id"] != "docpoc_" + signature[:24]:
        errors.append("/run_id: does not match signature")
    if record["document"]["item_counts"]["total"] != len(
        record["document"]["items"]
    ):
        errors.append("/document/item_counts/total: does not match items length")
    if record["input"]["sample_id"] != expected_sample_id(record["input"]):
        errors.append("/input/sample_id: does not match canonical input payload")
    expected_package_fingerprint = sha256_json(
        package_fingerprint_payload(
            versions=record["configuration"]["package_versions"],
            python_version=record["configuration"]["python_version"],
            platform_value=record["configuration"]["platform"],
            num_threads=record["configuration"]["num_threads"],
            ocr_engine_fingerprint=record["configuration"][
                "ocr_engine_fingerprint"
            ],
        )
    )
    if (
        record["configuration"]["package_fingerprint_sha256"]
        != expected_package_fingerprint
    ):
        errors.append(
            "/configuration/package_fingerprint_sha256: does not match package/runtime payload"
        )
    ocr_fingerprint = record["configuration"].get("ocr_engine_fingerprint")
    if ocr_fingerprint is not None:
        ocr_configuration = record["configuration"]["pipeline"]["ocr"]
        if ocr_fingerprint["engine"] != ocr_configuration["engine"]:
            errors.append(
                "/configuration/ocr_engine_fingerprint/engine: does not match pipeline OCR engine"
            )
        if ocr_fingerprint["config_sha256"] != sha256_json(ocr_configuration):
            errors.append(
                "/configuration/ocr_engine_fingerprint/config_sha256: does not match pipeline OCR configuration"
            )
        fingerprint_payload = {
            key: ocr_fingerprint[key]
            for key in (
                "engine",
                "version",
                "runtime",
                "artifacts_sha256",
                "config_sha256",
            )
        }
        if ocr_fingerprint["fingerprint_sha256"] != sha256_json(
            fingerprint_payload
        ):
            errors.append(
                "/configuration/ocr_engine_fingerprint/fingerprint_sha256: does not match fingerprint payload"
            )
    document = record["document"]
    if document["markdown"] is not None:
        markdown_sha = sha256_bytes(document["markdown"].encode("utf-8"))
        if document["markdown_sha256"] != markdown_sha:
            errors.append(
                "/document/markdown_sha256: does not match Markdown bytes"
            )
    if document["docling_json"] is not None:
        docling_json_sha = sha256_bytes(document["docling_json"].encode("utf-8"))
        if document["docling_json_sha256"] != docling_json_sha:
            errors.append(
                "/document/docling_json_sha256: does not match Docling JSON bytes"
            )
    if record["status"] == "completed" and document["item_counts"]["total"] < 1:
        errors.append("/status: completed record must contain at least one item")
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
    status: str,
    document: Mapping[str, Any],
    total_ms: float,
    stages: Sequence[Mapping[str, Any]],
    warnings: Sequence[str],
    errors: Sequence[Mapping[str, str]],
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "schema_version": "0.2",
        "record_type": "docling_poc_run",
        "run_id": "docpoc_" + "0" * 24,
        "input": dict(sample),
        "configuration": dict(configuration),
        "status": status,
        "document": dict(document),
        "timing": {
            "total_ms": round(max(0.0, total_ms), 6),
            "stages": [dict(item) for item in stages],
        },
        "warnings": list(dict.fromkeys(str(value) for value in warnings if value)),
        "errors": [dict(value) for value in errors],
        "hashes": {
            "input_sha256": sample["image_sha256"],
            "output_sha256": "0" * 64,
            "signature_sha256": "0" * 64,
            "record_integrity_sha256": "0" * 64,
        },
        "provenance": {
            "generated_at": utc_now(),
            "selection_method": "verified-manifest-structural-strata-v0.1",
            "question_independent": True,
            "question_data_used": False,
            "gold_data_used": False,
            "prediction_data_used": False,
            "answer_data_used": False,
            "source_data_used": True,
            "document_graph_candidate": True,
            "evidence_connected": False,
            "search_unit_connected": False,
        },
    }
    record["hashes"]["output_sha256"] = sha256_json(_output_payload(record))
    signature = sha256_json(_signature_payload(record))
    record["hashes"]["signature_sha256"] = signature
    record["run_id"] = "docpoc_" + signature[:24]
    record["hashes"]["record_integrity_sha256"] = sha256_json(
        record_integrity_payload(record)
    )
    problems = validate_record(record)
    if problems:
        raise ValueError("generated Docling record is invalid: " + "; ".join(problems))
    return record


def _build_converter(
    models_dir: Path, num_threads: int, ocr_engine: str = "tesseract_cli"
) -> Any:
    # These imports are intentionally lazy so contract tests do not require Docling.
    from docling.datamodel.accelerator_options import (
        AcceleratorDevice,
        AcceleratorOptions,
    )
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import (
        LayoutOptions,
        OcrMacOptions,
        PdfPipelineOptions,
        TableFormerMode,
        TableStructureOptions,
        TesseractCliOcrOptions,
    )
    from docling.datamodel.settings import settings
    from docling.document_converter import DocumentConverter, ImageFormatOption

    # Profiling data becomes part of the reproducible run record.
    settings.debug.profile_pipeline_timings = True
    if ocr_engine == "tesseract_cli":
        ocr_options = TesseractCliOcrOptions(
            lang=["jpn", "eng"],
            force_full_page_ocr=True,
            tesseract_cmd="tesseract",
        )
    elif ocr_engine == "ocrmac":
        ocr_options = OcrMacOptions(
            lang=["ja-JP", "en-US"],
            recognition="accurate",
            framework="vision",
            force_full_page_ocr=True,
        )
    else:
        raise ValueError(f"unsupported OCR engine: {ocr_engine}")
    pipeline_options = PdfPipelineOptions(
        do_ocr=True,
        do_table_structure=True,
        enable_remote_services=False,
        allow_external_plugins=False,
        artifacts_path=models_dir,
        accelerator_options=AcceleratorOptions(
            device=AcceleratorDevice.MPS, num_threads=num_threads
        ),
        ocr_options=ocr_options,
        layout_options=LayoutOptions(),
        table_structure_options=TableStructureOptions(
            do_cell_matching=True, mode=TableFormerMode.ACCURATE
        ),
    )
    return DocumentConverter(
        allowed_formats=[InputFormat.IMAGE],
        format_options={
            InputFormat.IMAGE: ImageFormatOption(pipeline_options=pipeline_options)
        },
    )


def _status(value: Any) -> str:
    raw = _enum_value(value)
    if raw == "success":
        return "completed"
    if raw == "partial_success":
        return "partial"
    if raw in {"failure", "skipped"}:
        return "failed"
    raise ValueError(f"unknown Docling conversion status: {raw}")


def run_samples(
    samples: Sequence[Mapping[str, Any]],
    *,
    repository_root: Path,
    models_dir: Path,
    num_threads: int,
    ocr_engine: str = "tesseract_cli",
) -> list[dict[str, Any]]:
    # Refuse implicit network access. Required weights must already be local.
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    configuration = build_configuration(models_dir, num_threads, ocr_engine)
    converter = _build_converter(models_dir, num_threads, ocr_engine)
    records: list[dict[str, Any]] = []
    for sample in samples:
        image = _resolve_repo_file(
            repository_root, sample["materialized_path"], "materialized_path"
        )
        actual_hash = sha256_file(image)
        if actual_hash != sample["image_sha256"]:
            raise ValueError(f"image hash mismatch: {sample['materialized_path']}")
        started = time.perf_counter()
        captured: list[str] = []
        try:
            with py_warnings.catch_warnings(record=True) as warning_items:
                py_warnings.simplefilter("always")
                result = converter.convert(image, raises_on_error=False)
            captured = [str(item.message) for item in warning_items]
            status = _status(result.status)
            errors = _result_errors(result)
            if status == "failed":
                document = empty_document()
            else:
                document = summarize_document(result.document)
                if not document["items"]:
                    status = "partial"
                    captured.append("Docling completed without document items")
            stages = _stage_timings(result)
        except Exception as exc:  # preserve the failure in the closed record
            status = "failed"
            document = empty_document()
            stages = []
            errors = [
                {
                    "component": "docling",
                    "type": type(exc).__name__,
                    "message": str(exc) or repr(exc),
                }
            ]
        total_ms = (time.perf_counter() - started) * 1000.0
        records.append(
            finalize_record(
                sample=sample,
                configuration=configuration,
                status=status,
                document=document,
                total_ms=total_ms,
                stages=stages,
                warnings=captured,
                errors=errors,
            )
        )
    return records


def write_jsonl(path: Path, records: Iterable[Mapping[str, Any]], *, overwrite: bool) -> None:
    if path.exists() and not overwrite:
        raise ValueError(f"output exists; pass --overwrite to replace it: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink() or path.parent.is_symlink():
        raise ValueError("output path and parent must not be symlinks")
    payload = "".join(canonical_json(record) + "\n" for record in records)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(payload, encoding="utf-8")
    temporary.replace(path)


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("--fixtures", type=Path, required=True)
    value.add_argument("--models-dir", type=Path, required=True)
    value.add_argument("--repository-root", type=Path, default=ROOT)
    value.add_argument(
        "--ocr-engine",
        choices=["tesseract_cli", "ocrmac"],
        default="tesseract_cli",
        help="OCR engine; defaults to the original Tesseract baseline",
    )
    value.add_argument(
        "--output",
        type=Path,
        default=ROOT / "artifacts" / "ocr-poc-v0.1" / "docling-runs.jsonl",
    )
    value.add_argument("--num-threads", type=int, default=4)
    value.add_argument("--check", action="store_true", help="verify inputs/models without inference")
    value.add_argument("--overwrite", action="store_true")
    return value


def main() -> int:
    args = parser().parse_args()
    if args.num_threads < 1 or args.num_threads > 128:
        print("error: --num-threads must be between 1 and 128", file=sys.stderr)
        return 2
    try:
        repository_root = args.repository_root.resolve(strict=True)
        # Check the caller-supplied path before resolution so a symlink cannot
        # be hidden by Path.resolve().
        fixtures_path = args.fixtures
        fixtures = load_verified_manifest(fixtures_path, repository_root)
        samples = select_structural_samples(fixtures)
        for sample in samples:
            image = _resolve_repo_file(
                repository_root, sample["materialized_path"], "materialized_path"
            )
            if sha256_file(image) != sample["image_sha256"]:
                raise ValueError(f"image hash mismatch: {sample['materialized_path']}")
        configuration = build_configuration(
            args.models_dir, args.num_threads, args.ocr_engine
        )
        if args.check:
            print(
                canonical_json(
                    {
                        "status": "ready",
                        "samples": [sample["sample_id"] for sample in samples],
                        "roles": [sample["role"] for sample in samples],
                        "models": configuration["models"],
                        "package_fingerprint_sha256": configuration[
                            "package_fingerprint_sha256"
                        ],
                        "ocr_engine_fingerprint": configuration[
                            "ocr_engine_fingerprint"
                        ],
                        "tableformer_device_effective": "cpu",
                    }
                )
            )
            return 0
        records = run_samples(
            samples,
            repository_root=repository_root,
            models_dir=args.models_dir,
            num_threads=args.num_threads,
            ocr_engine=args.ocr_engine,
        )
        write_jsonl(args.output, records, overwrite=args.overwrite)
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    counts: dict[str, int] = {}
    for record in records:
        counts[record["status"]] = counts.get(record["status"], 0) + 1
    print(f"wrote {len(records)} Docling structural PoC records to {args.output}")
    print(
        "statuses: "
        + ", ".join(f"{key}={value}" for key, value in sorted(counts.items()))
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
