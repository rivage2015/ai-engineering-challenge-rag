#!/usr/bin/env python3
"""Engine adapters for the OCR PoC; production OCR contracts remain untouched."""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import extract_ocr_observations as extractor
import ocr_poc_contract as poc
import validate_ocr_observations as production_contract


@dataclass(frozen=True)
class OCRInput:
    image_bytes: bytes
    image_sha256: str
    width_px: int
    height_px: int


@dataclass(frozen=True)
class AdapterResult:
    status: str
    lines: list[dict[str, Any]]
    warnings: list[str]
    error: str | None
    setup_ms: float
    inference_ms: float


class OCREngineAdapter(Protocol):
    name: str

    def fingerprint(self) -> dict[str, Any]: ...

    def run(self, value: OCRInput, *, timeout: float) -> AdapterResult: ...


class AppleVisionAdapter:
    name = "apple_vision"

    def __init__(self, build_dir: Path) -> None:
        self.build_dir = build_dir
        self.binary: Path | None = None
        self._setup_ms = 0.0

    def fingerprint(self) -> dict[str, Any]:
        engine = production_contract.expected_engine(self.name)
        return {
            "name": self.name,
            "version": engine["version"],
            "fingerprint_sha256": engine["digest"],
            "runtime": engine["runtime"],
        }

    def _prepare(self, timeout: float) -> float:
        if self.binary is not None:
            return 0.0
        started = time.monotonic_ns()
        self.binary = extractor.compile_vision_helper(
            extractor.VISION_SOURCE, self.build_dir, timeout=timeout
        )
        self._setup_ms = (time.monotonic_ns() - started) / 1_000_000
        return self._setup_ms

    def run(self, value: OCRInput, *, timeout: float) -> AdapterResult:
        setup_ms = self._prepare(timeout)
        dimensions = {"width_px": value.width_px, "height_px": value.height_px}
        started = time.monotonic_ns()
        try:
            status, lines, warnings, error = extractor.run_apple_vision_raw(
                self.binary, value.image_bytes, dimensions, timeout=timeout
            )
        except Exception as exc:  # exact error is retained in the shadow run
            inference_ms = (time.monotonic_ns() - started) / 1_000_000
            return AdapterResult(
                status="failed",
                lines=[],
                warnings=[],
                error=f"{type(exc).__name__}: {exc}",
                setup_ms=setup_ms,
                inference_ms=inference_ms,
            )
        inference_ms = (time.monotonic_ns() - started) / 1_000_000
        return AdapterResult(status, lines, warnings, error, setup_ms, inference_ms)


class TesseractAdapter:
    name = "tesseract"

    def __init__(self, executable: str = "tesseract") -> None:
        self.executable_name = executable
        self.executable: Path | None = None

    def fingerprint(self) -> dict[str, Any]:
        engine = production_contract.expected_engine(self.name)
        return {
            "name": self.name,
            "version": engine["version"],
            "fingerprint_sha256": engine["digest"],
            "runtime": engine["runtime"],
        }

    def _prepare(self, timeout: float) -> float:
        if self.executable is not None:
            return 0.0
        started = time.monotonic_ns()
        self.executable = extractor.verify_tesseract(
            self.executable_name, timeout=timeout
        )
        return (time.monotonic_ns() - started) / 1_000_000

    def run(self, value: OCRInput, *, timeout: float) -> AdapterResult:
        setup_ms = self._prepare(timeout)
        dimensions = {"width_px": value.width_px, "height_px": value.height_px}
        started = time.monotonic_ns()
        try:
            status, lines, warnings, error = extractor.run_tesseract_raw(
                self.executable, value.image_bytes, dimensions, timeout=timeout
            )
        except Exception as exc:
            inference_ms = (time.monotonic_ns() - started) / 1_000_000
            return AdapterResult(
                status="failed",
                lines=[],
                warnings=[],
                error=f"{type(exc).__name__}: {exc}",
                setup_ms=setup_ms,
                inference_ms=inference_ms,
            )
        inference_ms = (time.monotonic_ns() - started) / 1_000_000
        return AdapterResult(status, lines, warnings, error, setup_ms, inference_ms)


def crop_input(
    fixture: dict[str, Any], repository_root: Path
) -> OCRInput:
    """Verify the source image then return the exact normalized crop as PNG."""
    import io

    from PIL import Image

    image_path = poc.resolve_fixture_image(fixture, repository_root)
    if poc.sha256_file(image_path) != fixture["asset_ref"]["image_sha256"]:
        raise ValueError("fixture image SHA-256 changed")
    with Image.open(image_path) as source:
        source.load()
        width, height = int(source.width), int(source.height)
        if {"width_px": width, "height_px": height} != fixture["asset_ref"]["dimensions"]:
            raise ValueError("fixture image dimensions changed")
        x, y, crop_width, crop_height = fixture["crop"]["bbox"]
        left = x * width // 1000
        top = y * height // 1000
        right = min(width, (x + crop_width) * width // 1000)
        bottom = min(height, (y + crop_height) * height // 1000)
        if right <= left or bottom <= top:
            raise ValueError("fixture crop is empty after pixel conversion")
        cropped = source.crop((left, top, right, bottom)).convert("RGB")
        output = io.BytesIO()
        cropped.save(output, format="PNG", optimize=False)
    image_bytes = output.getvalue()
    return OCRInput(
        image_bytes=image_bytes,
        image_sha256=hashlib.sha256(image_bytes).hexdigest(),
        width_px=right - left,
        height_px=bottom - top,
    )


def built_in_adapters(cache_dir: Path, names: list[str]) -> list[OCREngineAdapter]:
    factories = {
        "apple_vision": lambda: AppleVisionAdapter(cache_dir / "apple-vision-build"),
        "tesseract": TesseractAdapter,
    }
    unknown = [name for name in names if name not in factories]
    if unknown:
        raise ValueError("unsupported built-in OCR PoC engine(s): " + ", ".join(unknown))
    return [factories[name]() for name in names]
