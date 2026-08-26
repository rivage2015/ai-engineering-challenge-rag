#!/usr/bin/env python3
"""Run conservative, local-only OCR for one standalone image.

Text lines must agree across two local OCR passes in both text and position.
Apple Vision plus Tesseract is preferred. If Vision is unavailable, two
Tesseract layout modes are compared and explicitly labelled non-independent.
Single-pass readings remain diagnostics and are never promoted to searchable
facts by this module.
"""

from __future__ import annotations

import hashlib
import subprocess
import tempfile
import unicodedata
from pathlib import Path
from typing import Any

from PIL import Image

import extract_ocr_observations as ocr


OVERLAP_THRESHOLD = 0.5


def _overlap(first: list[int], second: list[int]) -> float:
    left = max(first[0], second[0])
    top = max(first[1], second[1])
    right = min(first[0] + first[2], second[0] + second[2])
    bottom = min(first[1] + first[3], second[1] + second[3])
    if right <= left or bottom <= top:
        return 0.0
    intersection = (right - left) * (bottom - top)
    smaller = min(first[2] * first[3], second[2] * second[3])
    return intersection / smaller if smaller else 0.0


def _normalized(value: str) -> str:
    return unicodedata.normalize("NFC", value).strip()


def _run_tesseract_psm(
    executable: Path,
    raw: bytes,
    dimensions: dict[str, int],
    *,
    psm: int,
    timeout: float,
) -> tuple[str, list[dict[str, Any]], list[str], str | None]:
    with tempfile.TemporaryDirectory(prefix=f"aiec-image-ocr-psm{psm}-") as temporary:
        output = Path(temporary) / "ocr"
        process = subprocess.run(
            [
                str(executable), "stdin", str(output), "-l", "jpn+eng",
                "--oem", "1", "--psm", str(psm),
                "-c", "preserve_interword_spaces=1", "txt", "tsv",
            ],
            input=raw,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
        if process.returncode:
            detail = process.stderr.decode("utf-8", errors="replace").strip()
            raise RuntimeError(f"Tesseract PSM {psm} failed: {detail[:500]}")
        return ocr.parse_tesseract_outputs(
            output.with_suffix(".txt").read_text(encoding="utf-8"),
            output.with_suffix(".tsv").read_text(encoding="utf-8"),
            dimensions,
        )


def extract(path: Path, *, timeout: float = 180.0) -> dict[str, Any]:
    """Return conservative local OCR consensus and non-searchable diagnostics."""
    raw = path.read_bytes()
    with Image.open(path) as image:
        image.load()
        if getattr(image, "n_frames", 1) != 1:
            raise ValueError("standalone image OCR accepts exactly one image frame")
        dimensions = {"width_px": image.width, "height_px": image.height}
        image_format = image.format
        orientation = image.getexif().get(274, 1)

    cache = Path(tempfile.gettempdir()) / "aiec-intermediate-image-ocr-v0.1"
    cache.mkdir(mode=0o700, parents=True, exist_ok=True)
    build_dir = ocr.ensure_cache_subdirectory(cache, "_vision_build")
    vision = ocr.resolve_vision_binary(None, ocr.VISION_SOURCE, build_dir, timeout=timeout)
    tesseract = ocr.verify_tesseract("tesseract", timeout=timeout)

    primary_status, primary_lines, primary_warnings, _ = ocr.run_tesseract_raw(
        tesseract, raw, dimensions, timeout=timeout
    )
    try:
        audit_status, audit_lines, audit_warnings, _ = ocr.run_apple_vision_raw(
            vision, raw, dimensions, timeout=timeout
        )
        audit_name = "apple_vision"
        independent_engines = True
    except Exception as exc:
        audit_status, audit_lines, audit_warnings, _ = _run_tesseract_psm(
            tesseract, raw, dimensions, psm=6, timeout=timeout
        )
        audit_warnings = [
            f"Apple Vision unavailable: {type(exc).__name__}: {exc}",
            "fallback audit uses the same Tesseract engine with PSM 6; not an independent second model",
            *audit_warnings,
        ]
        audit_name = "tesseract_psm6"
        independent_engines = False

    used_audit: set[int] = set()
    consensus: list[dict[str, Any]] = []
    for primary_line in primary_lines:
        candidates = [
            (index, line, _overlap(primary_line["bbox"], line["bbox"]))
            for index, line in enumerate(audit_lines)
            if index not in used_audit
            and _normalized(line["raw_text"]) == _normalized(primary_line["raw_text"])
        ]
        candidates = [item for item in candidates if item[2] >= OVERLAP_THRESHOLD]
        if not candidates:
            continue
        index, audit_line, overlap = max(candidates, key=lambda item: item[2])
        used_audit.add(index)
        x = min(primary_line["bbox"][0], audit_line["bbox"][0])
        y = min(primary_line["bbox"][1], audit_line["bbox"][1])
        right = max(
            primary_line["bbox"][0] + primary_line["bbox"][2],
            audit_line["bbox"][0] + audit_line["bbox"][2],
        )
        bottom = max(
            primary_line["bbox"][1] + primary_line["bbox"][3],
            audit_line["bbox"][1] + audit_line["bbox"][3],
        )
        consensus.append({
            "text": _normalized(primary_line["raw_text"]),
            "bbox": [x, y, right - x, bottom - y],
            "overlap": round(overlap, 6),
            "primary_confidence": primary_line.get("confidence"),
            "audit_confidence": audit_line.get("confidence"),
        })

    return {
        "input_sha256": hashlib.sha256(raw).hexdigest(),
        "dimensions": dimensions,
        "image_format": image_format,
        "orientation": orientation,
        "coordinate_system": "top_left_normalized_1000",
        "independent_engines": independent_engines,
        "engines": {
            "tesseract_psm3": {
                "status": primary_status,
                "line_count": len(primary_lines),
                "warnings": primary_warnings,
            },
            audit_name: {
                "status": audit_status,
                "line_count": len(audit_lines),
                "warnings": audit_warnings,
            },
        },
        "consensus_lines": consensus,
        "unresolved_count": len(primary_lines) + len(audit_lines) - 2 * len(consensus),
    }
