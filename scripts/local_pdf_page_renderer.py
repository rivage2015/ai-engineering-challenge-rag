#!/usr/bin/env python3
"""Render bounded PDF pages locally with macOS PDFKit and no build tools."""

from __future__ import annotations

import hashlib
import json
import math
import os
import selectors
import stat
import subprocess
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator


RUNNER = "aiec-local-pdf-page-renderer"
RUNNER_VERSION = "0.3"
# Compatibility names retained for existing provenance consumers.
SWIFT_RUNNER = "aiec-pdf-page-renderer"
SWIFT_RUNNER_VERSION = "0.3"
JXA_SOURCE = Path(__file__).with_name("pdf_page_renderer.js")
OSASCRIPT = Path("/usr/bin/osascript")
MAX_PDF_BYTES = 512 * 1024 * 1024
MAX_PDF_PAGES = 500
MAX_PDF_DOCUMENT_RENDERED_PIXELS = 500_000_000
MAX_PDF_DOCUMENT_RENDERED_BYTES = 1024 * 1024 * 1024
MAX_PDF_DOCUMENT_NATIVE_TEXT_CHARS = 16_000_000
# Native text and visual interpretation have separate clocks so a slow OCR/VLM
# pass cannot consume the budget needed to retain later native-text pages.
MAX_PDF_DOCUMENT_NATIVE_SECONDS = 900.0
MAX_PDF_DOCUMENT_SECONDS = 1800.0
MAX_NATIVE_TEXT_CHARS = 1_000_000
MAX_RENDERED_BYTES = 200 * 1024 * 1024
MAX_RENDERED_PIXELS = 50_000_000
MAX_RENDERED_DIMENSION = 32_768
MAX_STDOUT_BYTES = 8 * 1024 * 1024
MAX_STDERR_BYTES = 64 * 1024
DEFAULT_DPI = 200
MAX_TIMEOUT_SECONDS = 180.0


@dataclass(frozen=True)
class PDFSnapshot:
    path: Path
    source_sha256: str
    source_size_bytes: int
    helper_path: Path
    helper_sha256: str


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _private_directory(path: Path, *, label: str) -> Path:
    metadata = path.lstat()
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise ValueError(f"{label} must be a private directory owned by this user")
    return path.resolve(strict=True)


def _copy_stable_regular_file(
    source: Path,
    destination: Path,
    *,
    label: str,
    maximum_bytes: int,
    required_prefix: bytes | None = None,
) -> tuple[str, int]:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        source_fd = os.open(source, flags)
    except OSError as exc:
        raise ValueError(f"{label} cannot be opened safely") from exc
    output_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    output_fd: int | None = None
    digest = hashlib.sha256()
    copied = 0
    try:
        before = os.fstat(source_fd)
        if not stat.S_ISREG(before.st_mode) or not 0 < before.st_size <= maximum_bytes:
            raise ValueError(f"{label} is not a bounded regular file")
        output_fd = os.open(destination, output_flags, 0o600)
        prefix = b""
        while True:
            block = os.read(source_fd, min(1024 * 1024, maximum_bytes + 1 - copied))
            if not block:
                break
            copied += len(block)
            if copied > maximum_bytes:
                raise ValueError(f"{label} exceeds the safety limit")
            if len(prefix) < 8:
                prefix += block[: 8 - len(prefix)]
            digest.update(block)
            view = memoryview(block)
            while view:
                written = os.write(output_fd, view)
                view = view[written:]
        os.fsync(output_fd)
        after = os.fstat(source_fd)
        if (
            copied != before.st_size
            or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
            != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        ):
            raise ValueError(f"{label} changed while being snapshotted")
        if required_prefix is not None and not prefix.startswith(required_prefix):
            raise ValueError(f"{label} has an invalid file signature")
    except Exception:
        destination.unlink(missing_ok=True)
        raise
    finally:
        os.close(source_fd)
        if output_fd is not None:
            os.close(output_fd)
    return digest.hexdigest(), copied


@contextmanager
def snapshot_pdf(source: Path) -> Iterator[PDFSnapshot]:
    """Freeze a source PDF and helper script in a random 0700 directory."""
    with tempfile.TemporaryDirectory(prefix="aiec-pdf-snapshot-") as temporary:
        root = _private_directory(Path(temporary), label="PDF snapshot directory")
        snapshot_path = root / "source.pdf"
        source_sha256, source_size = _copy_stable_regular_file(
            Path(source),
            snapshot_path,
            label="PDF source",
            maximum_bytes=MAX_PDF_BYTES,
            required_prefix=b"%PDF-",
        )
        helper_path = root / "pdf_page_renderer.js"
        helper_sha256, _ = _copy_stable_regular_file(
            JXA_SOURCE,
            helper_path,
            label="PDFKit JXA helper",
            maximum_bytes=1024 * 1024,
        )
        yield PDFSnapshot(
            path=snapshot_path,
            source_sha256=source_sha256,
            source_size_bytes=source_size,
            helper_path=helper_path,
            helper_sha256=helper_sha256,
        )


def _trusted_osascript() -> tuple[Path, str]:
    executable = OSASCRIPT.resolve(strict=True)
    metadata = executable.lstat()
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != 0
        or metadata.st_nlink != 1
        or stat.S_IMODE(metadata.st_mode) & 0o022
        or not os.access(executable, os.X_OK)
    ):
        raise RuntimeError("system osascript failed the executable trust check")
    return executable, _sha256_file(executable)


def _run_bounded(
    command: list[str], *, timeout: float
) -> tuple[int, bytes, bytes]:
    """Capture child output while killing it as soon as a bound is crossed."""
    process = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        close_fds=True,
    )
    if process.stdout is None or process.stderr is None:
        process.kill()
        raise RuntimeError("cannot capture PDFKit renderer output")
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ, ("stdout", MAX_STDOUT_BYTES))
    selector.register(process.stderr, selectors.EVENT_READ, ("stderr", MAX_STDERR_BYTES))
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    deadline = time.monotonic() + timeout
    try:
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                process.kill()
                raise TimeoutError("PDFKit renderer exceeded the time limit")
            events = selector.select(min(remaining, 0.25))
            for key, _ in events:
                name, maximum = key.data
                block = os.read(key.fileobj.fileno(), 64 * 1024)
                if not block:
                    selector.unregister(key.fileobj)
                    key.fileobj.close()
                    continue
                buffers[name].extend(block)
                if len(buffers[name]) > maximum:
                    process.kill()
                    raise RuntimeError(f"PDFKit renderer {name} exceeds the safety limit")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            process.kill()
            raise TimeoutError("PDFKit renderer exceeded the time limit")
        return_code = process.wait(timeout=remaining)
    finally:
        selector.close()
        if process.poll() is None:
            process.kill()
            process.wait()
    return return_code, bytes(buffers["stdout"]), bytes(buffers["stderr"])


def _strict_json_object(raw: bytes) -> dict[str, Any]:
    if not raw or len(raw) > MAX_STDOUT_BYTES:
        raise RuntimeError("PDFKit renderer returned empty or oversized output")

    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    try:
        value = json.loads(raw, object_pairs_hook=reject_duplicates)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise RuntimeError("PDFKit renderer returned invalid JSON") from exc
    if not isinstance(value, dict):
        raise RuntimeError("PDFKit renderer returned a non-object JSON value")
    return value


def _run_pdfkit(snapshot: PDFSnapshot, arguments: list[str], *, timeout: float) -> dict[str, Any]:
    executable, executable_sha256 = _trusted_osascript()
    if _sha256_file(snapshot.helper_path) != snapshot.helper_sha256:
        raise RuntimeError("PDFKit helper snapshot changed before execution")
    command = [
        str(executable), "-l", "JavaScript", str(snapshot.helper_path), "--",
        "--input", str(snapshot.path), *arguments,
    ]
    return_code, stdout, stderr = _run_bounded(command, timeout=timeout)
    _, executable_sha256_after = _trusted_osascript()
    if executable_sha256_after != executable_sha256:
        raise RuntimeError("system osascript changed during execution")
    payload = _strict_json_object(stdout)
    if return_code != 0 or payload.get("status") != "completed":
        detail = payload.get("error")
        if not isinstance(detail, str) or not detail.strip():
            detail = stderr.decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"PDFKit renderer failed: {detail[:500]}")
    payload["_backend_executable_sha256"] = executable_sha256
    return payload


def _bounded_page_count(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_PDF_PAGES:
        raise RuntimeError("PDF page count exceeds the safety limit")
    return value


def _validate_page_geometry(width: Any, height: Any, dpi: int) -> tuple[float, float]:
    if (
        isinstance(width, bool)
        or isinstance(height, bool)
        or not isinstance(width, (int, float))
        or not isinstance(height, (int, float))
    ):
        raise RuntimeError("PDFKit page geometry is invalid")
    width_value = float(width)
    height_value = float(height)
    pixel_width = math.ceil(width_value * dpi / 72.0)
    pixel_height = math.ceil(height_value * dpi / 72.0)
    if (
        not math.isfinite(width_value)
        or not math.isfinite(height_value)
        or width_value <= 0
        or height_value <= 0
        or pixel_width < 1
        or pixel_height < 1
        or pixel_width > MAX_RENDERED_DIMENSION
        or pixel_height > MAX_RENDERED_DIMENSION
        or pixel_width * pixel_height > MAX_RENDERED_PIXELS
    ):
        raise RuntimeError("rendered PDF page exceeds the pixel safety limit")
    return width_value, height_value


def inspect_pdf_snapshot(
    snapshot: PDFSnapshot, *, timeout: float = MAX_TIMEOUT_SECONDS
) -> dict[str, Any]:
    bounded_timeout = max(1.0, min(float(timeout), MAX_TIMEOUT_SECONDS))
    payload = _run_pdfkit(snapshot, ["--inspect"], timeout=bounded_timeout)
    page_count = _bounded_page_count(payload.get("page_count"))
    if (
        payload.get("runner") != SWIFT_RUNNER
        or payload.get("runner_version") != SWIFT_RUNNER_VERSION
        or payload.get("locked") is not False
    ):
        raise RuntimeError("PDFKit inspector output contract failed")
    raw_pages = payload.get("pages")
    if not isinstance(raw_pages, list) or len(raw_pages) != page_count:
        raise RuntimeError("PDFKit inspector page manifest is invalid")
    pages: list[dict[str, Any]] = []
    total_pixels = 0
    for page_number, raw_page in enumerate(raw_pages, 1):
        if not isinstance(raw_page, dict) or raw_page.get("page_number") != page_number:
            raise RuntimeError("PDFKit inspector page order is invalid")
        width, height = _validate_page_geometry(
            raw_page.get("page_width_pt"),
            raw_page.get("page_height_pt"),
            DEFAULT_DPI,
        )
        pixel_width = math.ceil(width * DEFAULT_DPI / 72.0)
        pixel_height = math.ceil(height * DEFAULT_DPI / 72.0)
        total_pixels += pixel_width * pixel_height
        pages.append({
            "page_number": page_number,
            "page_width_pt": width,
            "page_height_pt": height,
            "page_rotation": raw_page.get("page_rotation"),
            "render_width_px": pixel_width,
            "render_height_px": pixel_height,
        })
    return {
        "runner": RUNNER,
        "runner_version": RUNNER_VERSION,
        "backend": "apple_pdfkit_jxa",
        "backend_version": SWIFT_RUNNER_VERSION,
        "backend_executable_sha256": payload["_backend_executable_sha256"],
        "backend_build": {
            "source_sha256": snapshot.helper_sha256,
            "runtime": "system_osascript_jxa",
        },
        "external_network_used": False,
        "source_sha256": snapshot.source_sha256,
        "page_count": page_count,
        "pages": pages,
        "planned_render_pixels": total_pixels,
        "planned_render_dpi": DEFAULT_DPI,
        "visual_render_budget_exceeded": (
            total_pixels > MAX_PDF_DOCUMENT_RENDERED_PIXELS
        ),
        "encrypted": payload.get("encrypted") is True,
        "locked": False,
    }


def inspect_pdf(source: Path, *, timeout: float = MAX_TIMEOUT_SECONDS) -> dict[str, Any]:
    """Return a bounded PDFKit page count without Python PDF dependencies."""
    with snapshot_pdf(source) as snapshot:
        return inspect_pdf_snapshot(snapshot, timeout=timeout)


def _png_dimensions(path: Path) -> tuple[int, int]:
    with path.open("rb") as handle:
        header = handle.read(24)
    if len(header) != 24 or not header.startswith(b"\x89PNG\r\n\x1a\n") or header[12:16] != b"IHDR":
        raise RuntimeError("PDF page renderer did not produce a PNG")
    width = int.from_bytes(header[16:20], "big")
    height = int.from_bytes(header[20:24], "big")
    if (
        width <= 0
        or height <= 0
        or width > MAX_RENDERED_DIMENSION
        or height > MAX_RENDERED_DIMENSION
        or width * height > MAX_RENDERED_PIXELS
    ):
        raise RuntimeError("rendered PDF page exceeds the pixel safety limit")
    return width, height


def _validated_page_payload(
    payload: dict[str, Any], page_number: int, dpi: int
) -> tuple[int, float, float, str]:
    page_count = _bounded_page_count(payload.get("page_count"))
    if (
        payload.get("runner") != SWIFT_RUNNER
        or payload.get("runner_version") != SWIFT_RUNNER_VERSION
        or payload.get("page_number") != page_number
        or payload.get("dpi") != dpi
        or page_count < page_number
    ):
        raise RuntimeError("PDFKit page output contract failed")
    page_width, page_height = _validate_page_geometry(
        payload.get("page_width_pt"), payload.get("page_height_pt"), dpi
    )
    native_text = payload.get("native_text")
    if not isinstance(native_text, str) or len(native_text) > MAX_NATIVE_TEXT_CHARS:
        raise RuntimeError("PDFKit native page text is invalid or exceeds the safety limit")
    return page_count, page_width, page_height, native_text


def read_pdf_snapshot_page(
    snapshot: PDFSnapshot,
    page_number: int,
    *,
    dpi: int = DEFAULT_DPI,
    timeout: float = MAX_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Read bounded native page text before the independent visual render."""
    if isinstance(page_number, bool) or not isinstance(page_number, int) or page_number < 1:
        raise ValueError("page_number must be a positive integer")
    if isinstance(dpi, bool) or not isinstance(dpi, int) or not 72 <= dpi <= 300:
        raise ValueError("dpi must be between 72 and 300")
    bounded_timeout = max(1.0, min(float(timeout), MAX_TIMEOUT_SECONDS))
    payload = _run_pdfkit(
        snapshot,
        ["--page-info", "--page", str(page_number), "--dpi", str(dpi)],
        timeout=bounded_timeout,
    )
    page_count, width, height, native_text = _validated_page_payload(
        payload, page_number, dpi
    )
    return {
        "runner": RUNNER,
        "runner_version": RUNNER_VERSION,
        "external_network_used": False,
        "source_sha256": snapshot.source_sha256,
        "page_number": page_number,
        "dpi": dpi,
        "backend": "apple_pdfkit_jxa",
        "backend_version": SWIFT_RUNNER_VERSION,
        "backend_executable_sha256": payload["_backend_executable_sha256"],
        "backend_build": {
            "source_sha256": snapshot.helper_sha256,
            "runtime": "system_osascript_jxa",
        },
        "page_count": page_count,
        "page_width_pt": width,
        "page_height_pt": height,
        "page_rotation": payload.get("page_rotation"),
        "native_text": native_text,
    }


def _publish_rendered_file(source: Path, output: Path) -> Path:
    parent = _private_directory(output.parent, label="rendered PDF output directory")
    if output.name in {"", ".", ".."} or output.suffix.casefold() != ".png":
        raise ValueError("output must be a PNG filename")
    parent_fd = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    destination_fd: int | None = None
    try:
        destination_fd = os.open(
            output.name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
            0o600,
            dir_fd=parent_fd,
        )
        with source.open("rb") as source_handle:
            copied = 0
            while True:
                block = source_handle.read(1024 * 1024)
                if not block:
                    break
                copied += len(block)
                if copied > MAX_RENDERED_BYTES:
                    raise RuntimeError("rendered PDF page exceeds the byte safety limit")
                view = memoryview(block)
                while view:
                    written = os.write(destination_fd, view)
                    view = view[written:]
        os.fsync(destination_fd)
    except Exception:
        try:
            os.unlink(output.name, dir_fd=parent_fd)
        except FileNotFoundError:
            pass
        raise
    finally:
        if destination_fd is not None:
            os.close(destination_fd)
        os.close(parent_fd)
    return parent / output.name


def render_pdf_snapshot_page(
    snapshot: PDFSnapshot,
    page_number: int,
    output: Path,
    *,
    dpi: int = DEFAULT_DPI,
    timeout: float = MAX_TIMEOUT_SECONDS,
    require_native_text: bool = False,
) -> dict[str, Any]:
    del require_native_text  # PDFKit always returns bounded native page text.
    if isinstance(page_number, bool) or not isinstance(page_number, int) or page_number < 1:
        raise ValueError("page_number must be a positive integer")
    if isinstance(dpi, bool) or not isinstance(dpi, int) or not 72 <= dpi <= 300:
        raise ValueError("dpi must be between 72 and 300")
    bounded_timeout = max(1.0, min(float(timeout), MAX_TIMEOUT_SECONDS))
    output = Path(output)
    _private_directory(output.parent, label="rendered PDF output directory")
    if os.path.lexists(output):
        raise ValueError("output path must not already exist")

    with tempfile.TemporaryDirectory(prefix="render-", dir=output.parent) as temporary:
        render_root = _private_directory(Path(temporary), label="PDFKit render directory")
        private_output = render_root / "page.png"
        payload = _run_pdfkit(
            snapshot,
            [
                "--page", str(page_number), "--dpi", str(dpi),
                "--output", str(private_output),
            ],
            timeout=bounded_timeout,
        )
        if payload.get("output_format") != "PNG":
            raise RuntimeError("PDFKit renderer output contract failed")
        page_count, page_width, page_height, native_text = _validated_page_payload(
            payload, page_number, dpi
        )
        if private_output.is_symlink() or not private_output.is_file():
            raise RuntimeError("PDFKit renderer did not create a regular output file")
        if not 0 < private_output.stat().st_size <= MAX_RENDERED_BYTES:
            raise RuntimeError("rendered PDF page exceeds the byte safety limit")
        if payload.get("rendered_size_bytes") != private_output.stat().st_size:
            raise RuntimeError("PDFKit rendered byte count does not match the PNG")
        width, height = _png_dimensions(private_output)
        if payload.get("width_px") != width or payload.get("height_px") != height:
            raise RuntimeError("PDFKit rendered dimensions do not match the PNG")
        rendered = _publish_rendered_file(private_output, output)

    return {
        "runner": RUNNER,
        "runner_version": RUNNER_VERSION,
        "external_network_used": False,
        "source_sha256": snapshot.source_sha256,
        "page_number": page_number,
        "dpi": dpi,
        "rendered_sha256": _sha256_file(rendered),
        "rendered_size_bytes": rendered.stat().st_size,
        "width_px": width,
        "height_px": height,
        "backend": "apple_pdfkit_jxa",
        "backend_version": SWIFT_RUNNER_VERSION,
        "backend_executable_sha256": payload["_backend_executable_sha256"],
        "backend_build": {
            "source_sha256": snapshot.helper_sha256,
            "runtime": "system_osascript_jxa",
        },
        "page_count": page_count,
        "page_width_pt": page_width,
        "page_height_pt": page_height,
        "page_rotation": payload.get("page_rotation"),
        "native_text": native_text,
    }


def render_pdf_page(
    source: Path,
    page_number: int,
    output: Path,
    *,
    dpi: int = DEFAULT_DPI,
    timeout: float = MAX_TIMEOUT_SECONDS,
    require_native_text: bool = False,
) -> dict[str, Any]:
    """Snapshot and render one page for standalone callers."""
    with snapshot_pdf(source) as snapshot:
        return render_pdf_snapshot_page(
            snapshot,
            page_number,
            output,
            dpi=dpi,
            timeout=timeout,
            require_native_text=require_native_text,
        )


__all__ = [
    "DEFAULT_DPI",
    "MAX_PDF_PAGES",
    "MAX_PDF_DOCUMENT_RENDERED_BYTES",
    "MAX_PDF_DOCUMENT_NATIVE_TEXT_CHARS",
    "MAX_PDF_DOCUMENT_NATIVE_SECONDS",
    "MAX_PDF_DOCUMENT_SECONDS",
    "PDFSnapshot",
    "inspect_pdf",
    "inspect_pdf_snapshot",
    "read_pdf_snapshot_page",
    "render_pdf_page",
    "render_pdf_snapshot_page",
    "snapshot_pdf",
]
