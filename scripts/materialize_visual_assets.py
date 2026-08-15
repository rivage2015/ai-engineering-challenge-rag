#!/usr/bin/env python3
"""Materialize selected visual assets without overwriting existing evidence.

The input is JSONL.  Every record must contain ``asset_id``, a source path and
SHA-256, and ``origin.kind``.  Source fields may be flat or use the visual-asset
manifest's ``source.relative_path``/``source.sha256`` shape.  The supported
origins are a single PDF page, an embedded Office ZIP member, and a standalone
raster image.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import datetime as dt
import hashlib
import io
import json
import mimetypes
import os
import re
import shutil
import subprocess
import tempfile
import unicodedata
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterable


MATERIALIZER = "visual-asset-materializer"
MATERIALIZER_VERSION = "0.1"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
ASSET_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
OFFICE_MEDIA_PREFIXES = ("word/media/", "ppt/media/", "xl/media/")
DIRECT_IMAGE_FORMATS = {"PNG", "JPEG", "GIF", "WEBP"}
FORMAT_SUFFIXES = {
    "PNG": ".png",
    "JPEG": ".jpg",
    "GIF": ".gif",
    "WEBP": ".webp",
    "BMP": ".bmp",
    "TIFF": ".tiff",
}
FORMAT_MIME_TYPES = {
    "PNG": "image/png",
    "JPEG": "image/jpeg",
    "GIF": "image/gif",
    "WEBP": "image/webp",
    "BMP": "image/bmp",
    "TIFF": "image/tiff",
}
NOTEBOOK_OUTPUT_RE = re.compile(
    r"^cells/(?P<cell>[0-9]+)/outputs/(?P<output>[0-9]+)/data/(?P<mime>image/[^/]+)$"
)
NOTEBOOK_ATTACHMENT_RE = re.compile(
    r"^cells/(?P<cell>[0-9]+)/attachments/(?P<name>[^/]+)/(?P<mime>image/[^/]+)$"
)
SUPPORTED_NOTEBOOK_MIME_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp"}


class MaterializationError(RuntimeError):
    """A selected asset cannot be materialized without losing provenance."""


class UnsupportedMediaError(MaterializationError):
    """The source is real media, but v1 cannot safely rasterize it."""


@dataclass(frozen=True)
class ImageInfo:
    format: str
    mime_type: str
    suffix: str
    width: int
    height: int


@dataclass(frozen=True)
class MaterializedAsset:
    path: Path
    sha256: str
    mime_type: str
    width: int
    height: int
    size_bytes: int
    cache_hit: bool
    operation: str
    renderer: str
    renderer_version: str
    signature: str
    generated_at: str


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _pillow_image() -> Any:
    try:
        from PIL import Image
    except ImportError as exc:  # pragma: no cover - dependency is present in CI/runtime
        raise MaterializationError("Pillow is required to inspect raster images") from exc
    return Image


def pillow_version() -> str:
    try:
        import PIL
    except ImportError as exc:  # pragma: no cover - dependency is present in CI/runtime
        raise MaterializationError("Pillow is required to inspect raster images") from exc
    return str(PIL.__version__)


def pdftoppm_version(executable: str) -> str:
    try:
        completed = subprocess.run(
            [executable, "-v"], check=False, capture_output=True, text=True
        )
    except FileNotFoundError as exc:
        raise MaterializationError(f"pdftoppm executable not found: {executable}") from exc
    output = (completed.stderr or completed.stdout or "").strip().splitlines()
    if completed.returncode != 0 or not output:
        raise MaterializationError(f"cannot determine pdftoppm version: {executable}")
    return output[0].strip()


def soffice_identity(executable: str) -> tuple[str, str]:
    resolved_text = shutil.which(executable) if not Path(executable).is_absolute() else executable
    if not resolved_text:
        raise MaterializationError(f"LibreOffice executable not found: {executable}")
    resolved = Path(resolved_text).resolve()
    if not resolved.is_file():
        raise MaterializationError(f"LibreOffice executable is not a file: {resolved}")
    try:
        completed = subprocess.run(
            [str(resolved), "--version"], check=False, capture_output=True, text=True
        )
    except OSError as exc:
        raise MaterializationError(f"cannot execute LibreOffice: {resolved}") from exc
    output = (completed.stdout or completed.stderr or "").strip().splitlines()
    if completed.returncode != 0 or not output:
        raise MaterializationError(f"cannot determine LibreOffice version: {resolved}")
    identity = f"{output[0].strip()}|binary_sha256={sha256_file(resolved)}"
    return str(resolved), identity


def inspect_image_bytes(data: bytes) -> ImageInfo:
    Image = _pillow_image()
    try:
        with Image.open(io.BytesIO(data)) as image:
            image_format = str(image.format or "").upper()
            width, height = int(image.width), int(image.height)
            image.verify()
    except Exception as exc:
        raise MaterializationError(f"unsupported or corrupt raster image: {exc}") from exc
    if not image_format or image_format not in FORMAT_SUFFIXES:
        raise MaterializationError(f"unsupported raster format: {image_format or 'unknown'}")
    if width < 1 or height < 1:
        raise MaterializationError("image dimensions must be positive")
    mime_type = FORMAT_MIME_TYPES.get(image_format)
    if not mime_type:
        mime_type = mimetypes.guess_type("asset" + FORMAT_SUFFIXES[image_format])[0] or ""
    if not mime_type.startswith("image/"):
        raise MaterializationError(f"cannot determine image MIME type for {image_format}")
    return ImageInfo(image_format, mime_type, FORMAT_SUFFIXES[image_format], width, height)


def convert_to_png(data: bytes) -> bytes:
    Image = _pillow_image()
    destination = io.BytesIO()
    try:
        with Image.open(io.BytesIO(data)) as image:
            image.load()
            image.save(destination, format="PNG")
    except Exception as exc:
        raise MaterializationError(f"failed to convert raster image to PNG: {exc}") from exc
    return destination.getvalue()


def ensure_relative_source(root: Path, value: object) -> tuple[Path, str]:
    if not isinstance(value, str) or not value.strip():
        raise MaterializationError("source_path must be a non-empty relative path")
    if "\\" in value:
        raise MaterializationError("source_path must use forward slashes")
    source_path = PurePosixPath(value)
    if source_path.is_absolute():
        raise MaterializationError("source_path must be relative to --root")
    if any(part in {"", ".", ".."} for part in source_path.parts):
        raise MaterializationError("source_path contains an unsafe component")
    root_resolved = root.resolve()
    current = root_resolved
    canonical_parts: list[str] = []
    for index, component in enumerate(source_path.parts):
        canonical_component = unicodedata.normalize("NFC", component)
        canonical_parts.append(canonical_component)
        try:
            matches = [
                entry for entry in current.iterdir()
                if unicodedata.normalize("NFC", entry.name) == canonical_component
            ]
        except OSError as exc:
            raise MaterializationError(
                f"cannot inspect source_path component: {'/'.join(canonical_parts[:-1]) or '.'}"
            ) from exc
        if not matches:
            raise MaterializationError(
                f"source file not found: {'/'.join(canonical_parts + list(source_path.parts[index + 1:]))}"
            )
        if len(matches) != 1:
            raise MaterializationError(
                f"ambiguous Unicode-normalized source_path component: {'/'.join(canonical_parts)}"
            )
        current = matches[0]
        try:
            current.resolve().relative_to(root_resolved)
        except ValueError as exc:
            raise MaterializationError("source_path escapes --root") from exc
        if index < len(source_path.parts) - 1 and not current.is_dir():
            raise MaterializationError(
                f"source_path component is not a directory: {'/'.join(canonical_parts)}"
            )
    resolved = current.resolve()
    if not resolved.is_file():
        raise MaterializationError(f"source file not found: {'/'.join(canonical_parts)}")
    return resolved, "/".join(canonical_parts)


def normalized_safe_member_path(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise MaterializationError(f"{label} must be a non-empty path")
    if "\\" in value:
        raise MaterializationError(f"{label} must use forward slashes")
    pure = PurePosixPath(value)
    if (
        pure.is_absolute()
        or pure.as_posix() != value
        or any(part in {"", ".", ".."} for part in pure.parts)
    ):
        raise MaterializationError(f"unsafe {label}")
    return "/".join(unicodedata.normalize("NFC", part) for part in pure.parts)


def validate_office_member(value: object) -> str:
    normalized = normalized_safe_member_path(value, "Office member path")
    if not normalized.startswith(OFFICE_MEDIA_PREFIXES):
        raise MaterializationError("Office member must be under word/media, ppt/media, or xl/media")
    return normalized


def read_office_member(source: Path, member_path: str, max_member_bytes: int) -> bytes:
    try:
        with zipfile.ZipFile(source) as archive:
            matching = []
            for info in archive.infolist():
                try:
                    normalized_name = normalized_safe_member_path(
                        info.filename, "Office archive member path"
                    )
                except MaterializationError:
                    continue
                if normalized_name == member_path:
                    matching.append(info)
            if not matching:
                raise MaterializationError(f"Office member not found: {member_path}")
            if len(matching) != 1:
                raise MaterializationError(
                    f"ambiguous Unicode-normalized Office member path: {member_path}"
                )
            info = matching[0]
            unix_mode = (info.external_attr >> 16) & 0o170000
            if info.is_dir() or unix_mode == 0o120000:
                raise MaterializationError("Office member must be a regular file")
            if info.file_size < 1:
                raise MaterializationError("Office member is empty")
            if info.file_size > max_member_bytes:
                raise MaterializationError(
                    f"Office member exceeds --max-member-bytes ({info.file_size} > {max_member_bytes})"
                )
            data = archive.read(info)
    except zipfile.BadZipFile as exc:
        raise MaterializationError(f"invalid Office ZIP container: {source.name}") from exc
    if len(data) != info.file_size:
        raise MaterializationError("Office member size changed while reading")
    return data


def decode_notebook_payload(value: object, member_path: str) -> bytes:
    if isinstance(value, str):
        encoded = value
    elif isinstance(value, list) and value and all(isinstance(part, str) for part in value):
        encoded = "".join(value)
    else:
        raise MaterializationError(f"notebook image payload is not a base64 string: {member_path}")
    compact = "".join(encoded.split())
    try:
        return base64.b64decode(compact, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise MaterializationError(f"invalid notebook base64 payload: {member_path}") from exc


def normalized_mapping_value(mapping: object, key: str, label: str) -> Any:
    if not isinstance(mapping, dict):
        raise MaterializationError(f"{label} must be an object")
    normalized_key = unicodedata.normalize("NFC", key)
    matching = [
        (raw_key, value)
        for raw_key, value in mapping.items()
        if isinstance(raw_key, str) and unicodedata.normalize("NFC", raw_key) == normalized_key
    ]
    if not matching:
        raise MaterializationError(f"{label} key not found: {normalized_key}")
    if len(matching) != 1:
        raise MaterializationError(
            f"ambiguous Unicode-normalized {label} key: {normalized_key}"
        )
    return matching[0][1]


def read_notebook_member(source: Path, member_path: object, max_member_bytes: int) -> tuple[bytes, str]:
    if not isinstance(member_path, str) or not member_path:
        raise MaterializationError("notebook_embedded_image origin.member_path is required")
    normalized_member_path = normalized_safe_member_path(member_path, "notebook member_path")
    output_match = NOTEBOOK_OUTPUT_RE.fullmatch(normalized_member_path)
    attachment_match = NOTEBOOK_ATTACHMENT_RE.fullmatch(normalized_member_path)
    match = output_match or attachment_match
    if match is None:
        raise MaterializationError(f"invalid notebook member_path: {normalized_member_path}")
    mime_type = match.group("mime")
    if mime_type not in SUPPORTED_NOTEBOOK_MIME_TYPES:
        raise UnsupportedMediaError(
            f"notebook media type {mime_type} is not a supported raster payload in v1"
        )
    try:
        notebook = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MaterializationError(f"invalid notebook JSON: {source.name}") from exc
    try:
        cell = notebook["cells"][int(match.group("cell"))]
        if output_match is not None:
            output = cell["outputs"][int(match.group("output"))]
            container = normalized_mapping_value(output, "data", "notebook output")
        else:
            attachments = normalized_mapping_value(cell, "attachments", "notebook cell")
            container = normalized_mapping_value(
                attachments, match.group("name"), "notebook attachment"
            )
        payload = normalized_mapping_value(container, mime_type, "notebook media")
    except (KeyError, IndexError, TypeError) as exc:
        raise MaterializationError(f"notebook member not found: {member_path}") from exc
    data = decode_notebook_payload(payload, normalized_member_path)
    if not data:
        raise MaterializationError(f"notebook image payload is empty: {member_path}")
    if len(data) > max_member_bytes:
        raise MaterializationError(
            f"notebook payload exceeds --max-member-bytes ({len(data)} > {max_member_bytes})"
        )
    return data, mime_type


def verify_member_provenance(data: bytes, origin: dict[str, Any], label: str) -> None:
    expected_sha256 = origin.get("member_sha256")
    if expected_sha256 is not None:
        if not isinstance(expected_sha256, str) or not SHA256_RE.fullmatch(expected_sha256):
            raise MaterializationError("origin.member_sha256 must be a lowercase SHA-256 or null")
        actual_sha256 = sha256_bytes(data)
        if actual_sha256 != expected_sha256:
            raise MaterializationError(
                f"{label} SHA-256 mismatch: expected {expected_sha256}, got {actual_sha256}"
            )
    expected_size = origin.get("member_size_bytes")
    if expected_size is not None:
        if isinstance(expected_size, bool) or not isinstance(expected_size, int) or expected_size < 1:
            raise MaterializationError("origin.member_size_bytes must be a positive integer or null")
        if len(data) != expected_size:
            raise MaterializationError(
                f"{label} size mismatch: expected {expected_size}, got {len(data)}"
            )


def materialization_signature(
    source_sha256: str,
    origin: dict[str, Any],
    dpi: int,
    renderer: str,
    renderer_version: str,
) -> str:
    kind = origin.get("kind")
    if kind == "office_embedded_image":
        kind = "office_embedded"
    relevant: dict[str, Any] = {"kind": kind}
    if kind == "pdf_page":
        relevant.update({"page_number": origin.get("page_number"), "dpi": dpi})
    elif kind in {"office_embedded", "notebook_embedded_image"}:
        relevant["member_path"] = origin.get("member_path")
    value = {
        "materializer": MATERIALIZER,
        "version": MATERIALIZER_VERSION,
        "source_sha256": source_sha256,
        "origin": relevant,
        "renderer": renderer,
        "renderer_version": renderer_version,
    }
    return sha256_bytes(canonical_json(value).encode("utf-8"))


def _temporary_bytes(parent: Path, data: bytes) -> Path:
    parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=".materialize-", dir=parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return temporary


def files_match_exactly(left: Path, right: Path) -> bool:
    if left.stat().st_size != right.stat().st_size:
        return False
    if sha256_file(left) != sha256_file(right):
        return False
    with left.open("rb") as left_handle, right.open("rb") as right_handle:
        while True:
            left_block = left_handle.read(1024 * 1024)
            right_block = right_handle.read(1024 * 1024)
            if left_block != right_block:
                return False
            if not left_block:
                return True


def publish_no_overwrite(temporary: Path, destination: Path) -> bool:
    """Publish a complete file atomically; return True only for an identical cache hit."""
    try:
        os.link(temporary, destination)
        return False
    except FileExistsError:
        if destination.is_symlink() or not destination.is_file():
            raise MaterializationError(f"refusing non-regular destination: {destination}")
        if files_match_exactly(temporary, destination):
            return True
        raise MaterializationError(f"refusing to overwrite differing file: {destination}")
    finally:
        temporary.unlink(missing_ok=True)


def write_bytes_no_overwrite(destination: Path, data: bytes) -> bool:
    return publish_no_overwrite(_temporary_bytes(destination.parent, data), destination)


def cache_metadata_path(path: Path) -> Path:
    return path.with_name(path.name + ".meta.json")


def read_cached(
    path: Path,
    signature: str,
    operation: str,
    renderer: str,
    renderer_version: str,
) -> MaterializedAsset | None:
    metadata_path = cache_metadata_path(path)
    if path.is_symlink() or metadata_path.is_symlink():
        raise MaterializationError(f"materialization cache must use regular files: {path}")
    image_exists = path.exists()
    metadata_exists = metadata_path.exists()
    if not image_exists and not metadata_exists:
        return None
    if image_exists and path.is_file() and not metadata_exists:
        # A crash may occur after the immutable image link is published but
        # before its metadata link.  The caller must regenerate the candidate
        # bytes; persist_asset will accept only an exact byte/hash match before
        # creating the missing metadata.
        return None
    if not path.is_file() or not metadata_path.is_file():
        raise MaterializationError(f"incomplete materialization cache: {path}")
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MaterializationError(f"invalid materialization cache metadata: {metadata_path}") from exc
    if metadata.get("signature") != signature:
        raise MaterializationError(f"materialization cache signature mismatch: {path}")
    if metadata.get("operation") != operation:
        raise MaterializationError(f"materialization cache operation mismatch: {path}")
    if metadata.get("renderer") != renderer or metadata.get("renderer_version") != renderer_version:
        raise MaterializationError(f"materialization cache renderer mismatch: {path}")
    digest = sha256_file(path)
    if metadata.get("materialized_sha256") != digest:
        raise MaterializationError(f"materialization cache hash mismatch: {path}")
    info = inspect_image_bytes(path.read_bytes())
    expected = (metadata.get("mime_type"), metadata.get("width"), metadata.get("height"))
    actual = (info.mime_type, info.width, info.height)
    if expected != actual:
        raise MaterializationError(f"materialization cache image metadata mismatch: {path}")
    generated_at = metadata.get("generated_at")
    if not isinstance(generated_at, str) or not generated_at:
        raise MaterializationError(f"materialization cache generated_at is missing: {path}")
    return MaterializedAsset(
        path, digest, info.mime_type, info.width, info.height, path.stat().st_size,
        True, operation, renderer, renderer_version, signature, generated_at,
    )


def persist_asset(
    path: Path,
    data: bytes,
    info: ImageInfo,
    operation: str,
    renderer: str,
    renderer_version: str,
    signature: str,
) -> MaterializedAsset:
    cached = read_cached(path, signature, operation, renderer, renderer_version)
    if cached is not None:
        return cached
    digest = sha256_bytes(data)
    generated_at = dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")
    write_bytes_no_overwrite(path, data)
    metadata = {
        "schema_version": "0.1",
        "record_type": "visual_asset_materialization_cache",
        "signature": signature,
        "materialized_sha256": digest,
        "mime_type": info.mime_type,
        "width": info.width,
        "height": info.height,
        "operation": operation,
        "renderer": renderer,
        "renderer_version": renderer_version,
        "generated_at": generated_at,
    }
    metadata_bytes = (json.dumps(metadata, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    try:
        write_bytes_no_overwrite(cache_metadata_path(path), metadata_bytes)
    except Exception:
        # The immutable image is retained.  A future run regenerates its bytes,
        # requires an exact match, and can then publish the missing metadata.
        raise
    return MaterializedAsset(
        path, digest, info.mime_type, info.width, info.height, len(data), False,
        operation, renderer, renderer_version, signature, generated_at,
    )


def render_pdf_page(
    source: Path,
    page_number: int,
    dpi: int,
    pdftoppm: str,
    temporary_parent: Path,
) -> bytes:
    temporary_parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".pdf-page-", dir=temporary_parent) as temporary:
        prefix = Path(temporary) / "page"
        command = [
            pdftoppm,
            "-f", str(page_number),
            "-l", str(page_number),
            "-r", str(dpi),
            "-png",
            "-singlefile",
            str(source),
            str(prefix),
        ]
        try:
            completed = subprocess.run(command, check=False, capture_output=True, text=True)
        except FileNotFoundError as exc:
            raise MaterializationError(f"pdftoppm executable not found: {pdftoppm}") from exc
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "unknown pdftoppm error").strip()
            raise MaterializationError(f"pdftoppm failed for page {page_number}: {detail[:500]}")
        rendered = prefix.with_suffix(".png")
        if not rendered.is_file():
            raise MaterializationError(f"pdftoppm produced no PNG for page {page_number}")
        return rendered.read_bytes()


def convert_vector_office_member_to_png(
    data: bytes,
    suffix: str,
    soffice: str,
    temporary_parent: Path,
) -> tuple[bytes, str]:
    """Rasterize an EMF/WMF member with a version-and-binary-locked renderer."""
    executable, renderer_version = soffice_identity(soffice)
    temporary_parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".office-vector-", dir=temporary_parent) as temporary:
        work = Path(temporary)
        source = work / f"source{suffix}"
        output = work / "output"
        profile = work / "profile"
        output.mkdir()
        profile.mkdir()
        source.write_bytes(data)
        command = [
            executable,
            f"-env:UserInstallation={profile.resolve().as_uri()}",
            "--headless",
            "--convert-to", "png",
            "--outdir", str(output),
            str(source),
        ]
        try:
            completed = subprocess.run(command, check=False, capture_output=True, text=True)
        except OSError as exc:
            raise MaterializationError("LibreOffice vector conversion could not start") from exc
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "unknown LibreOffice error").strip()
            raise MaterializationError(f"LibreOffice vector conversion failed: {detail[:500]}")
        rendered = output / "source.png"
        if not rendered.is_file():
            raise MaterializationError("LibreOffice vector conversion produced no PNG")
        return rendered.read_bytes(), renderer_version


def safe_asset_id(value: object) -> str:
    if not isinstance(value, str) or not ASSET_ID_RE.fullmatch(value):
        raise MaterializationError("asset_id must contain only letters, digits, dot, underscore, or hyphen")
    return value


def record_source(record: dict[str, Any]) -> tuple[object, object]:
    if "source_path" in record or "source_sha256" in record:
        return record.get("source_path"), record.get("source_sha256")
    source = record.get("source")
    if isinstance(source, dict):
        return source.get("relative_path"), source.get("sha256")
    return None, None


def is_visual_asset_manifest_record(record: dict[str, Any]) -> bool:
    return isinstance(record.get("source"), dict)


def output_path_for(out_dir: Path, asset_id: str, signature: str, suffix: str) -> Path:
    return out_dir / f"{asset_id}--{signature[:16]}{suffix}"


def materialize_asset(
    record: dict[str, Any],
    root: Path,
    out_dir: Path,
    dpi: int = 200,
    pdftoppm: str = "pdftoppm",
    soffice: str = "soffice",
    max_member_bytes: int = 100 * 1024 * 1024,
) -> tuple[MaterializedAsset, str]:
    asset_id = safe_asset_id(record.get("asset_id"))
    source_path, expected_sha256 = record_source(record)
    source, relative_source = ensure_relative_source(root, source_path)
    if not isinstance(expected_sha256, str) or not SHA256_RE.fullmatch(expected_sha256):
        raise MaterializationError("source_sha256 must be a lowercase SHA-256")
    actual_sha256 = sha256_file(source)
    if actual_sha256 != expected_sha256:
        raise MaterializationError(
            f"source SHA-256 mismatch for {relative_source}: expected {expected_sha256}, got {actual_sha256}"
        )
    origin = record.get("origin")
    if not isinstance(origin, dict):
        raise MaterializationError("origin must be an object")
    kind = origin.get("kind")
    if kind not in {
        "pdf_page", "office_embedded", "office_embedded_image",
        "notebook_embedded_image", "standalone_image",
    }:
        raise MaterializationError(f"unsupported origin.kind: {kind!r}")
    if kind == "pdf_page":
        page_number = origin.get("page_number")
        if isinstance(page_number, bool) or not isinstance(page_number, int) or page_number < 1:
            raise MaterializationError("pdf_page origin.page_number must be a positive integer")
        operation = "pdf_page_render"
        renderer = "pdftoppm"
        renderer_version = pdftoppm_version(pdftoppm)
        signature = materialization_signature(
            expected_sha256, origin, dpi, renderer, renderer_version
        )
        destination = output_path_for(out_dir, asset_id, signature, ".png")
        cached = read_cached(
            destination, signature, operation, renderer, renderer_version
        )
        if cached is not None:
            return cached, relative_source
        data = render_pdf_page(source, page_number, dpi, pdftoppm, out_dir)
        info = inspect_image_bytes(data)
        if info.format != "PNG":
            raise MaterializationError("pdftoppm output is not a PNG")
        return persist_asset(
            destination, data, info, operation, renderer, renderer_version, signature
        ), relative_source

    if kind in {"office_embedded", "office_embedded_image"}:
        member_path = validate_office_member(origin.get("member_path"))
        member_suffix = PurePosixPath(member_path).suffix.lower()
        data = read_office_member(source, member_path, max_member_bytes)
        verify_member_provenance(data, origin, f"Office member {member_path}")
        if member_suffix in {".emf", ".wmf"}:
            data, renderer_version = convert_vector_office_member_to_png(
                data, member_suffix, soffice, out_dir
            )
            info = inspect_image_bytes(data)
            if info.format != "PNG":
                raise MaterializationError("LibreOffice vector conversion output is not PNG")
            operation = "office_vector_convert_to_png"
            renderer = "LibreOffice"
            signature = materialization_signature(
                expected_sha256, origin, dpi, renderer, renderer_version
            )
            destination = output_path_for(out_dir, asset_id, signature, ".png")
            return persist_asset(
                destination, data, info, operation, renderer, renderer_version, signature
            ), relative_source
        info = inspect_image_bytes(data)
        if info.format in DIRECT_IMAGE_FORMATS:
            operation = "office_member_copy"
            renderer = "byte_copy"
            renderer_version = MATERIALIZER_VERSION
        else:
            data = convert_to_png(data)
            info = inspect_image_bytes(data)
            operation = "office_member_convert_to_png"
            renderer = "Pillow"
            renderer_version = pillow_version()
        signature = materialization_signature(
            expected_sha256, origin, dpi, renderer, renderer_version
        )
        destination = output_path_for(out_dir, asset_id, signature, info.suffix)
        return persist_asset(
            destination, data, info, operation, renderer, renderer_version, signature
        ), relative_source

    if kind == "notebook_embedded_image":
        data, declared_mime_type = read_notebook_member(
            source, origin.get("member_path"), max_member_bytes
        )
        verify_member_provenance(data, origin, f"notebook member {origin.get('member_path')}")
        declared_origin_mime = origin.get("media_type")
        if declared_origin_mime is not None and declared_origin_mime != declared_mime_type:
            raise MaterializationError(
                f"notebook media_type mismatch: expected {declared_origin_mime}, "
                f"member_path declares {declared_mime_type}"
            )
        info = inspect_image_bytes(data)
        if info.mime_type != declared_mime_type:
            raise MaterializationError(
                f"notebook payload MIME mismatch: declared {declared_mime_type}, "
                f"decoded image is {info.mime_type}"
            )
        operation = "notebook_base64_decode"
        renderer = "base64"
        renderer_version = "RFC4648"
        signature = materialization_signature(
            expected_sha256, origin, dpi, renderer, renderer_version
        )
        destination = output_path_for(out_dir, asset_id, signature, info.suffix)
        return persist_asset(
            destination, data, info, operation, renderer, renderer_version, signature
        ), relative_source

    data = source.read_bytes()
    info = inspect_image_bytes(data)
    operation = "standalone_image_copy"
    renderer = "byte_copy"
    renderer_version = MATERIALIZER_VERSION
    signature = materialization_signature(
        expected_sha256, origin, dpi, renderer, renderer_version
    )
    destination = output_path_for(out_dir, asset_id, signature, info.suffix)
    return persist_asset(
        destination, data, info, operation, renderer, renderer_version, signature
    ), relative_source


def display_path(path: Path, root: Path) -> str:
    resolved = path.resolve()
    try:
        return resolved.relative_to(root.resolve()).as_posix()
    except ValueError:
        return str(resolved)


def materialized_record(
    record: dict[str, Any],
    root: Path,
    out_dir: Path,
    dpi: int = 200,
    pdftoppm: str = "pdftoppm",
    soffice: str = "soffice",
    max_member_bytes: int = 100 * 1024 * 1024,
) -> dict[str, Any]:
    canonical_manifest = is_visual_asset_manifest_record(record)
    _, expected_source_sha256 = record_source(record)
    try:
        asset, relative_source = materialize_asset(
            record, root, out_dir, dpi=dpi, pdftoppm=pdftoppm,
            soffice=soffice,
            max_member_bytes=max_member_bytes,
        )
    except (MaterializationError, OSError) as exc:
        status = "unsupported_media" if isinstance(exc, UnsupportedMediaError) else "error"
        if canonical_manifest and status == "error":
            status = "materialization_error"
        failure = {
            **record,
            "status": status,
            "materialized_path": None,
            "error": str(exc),
        }
        if canonical_manifest:
            failure["materialization"] = None
        else:
            failure.update({
                "materialized_sha256": None,
                "mime_type": None,
                "width": None,
                "height": None,
                "provenance": {
                    "materializer": MATERIALIZER,
                    "version": MATERIALIZER_VERSION,
                },
            })
        return failure
    materialized_path = (
        str(asset.path.resolve()) if canonical_manifest else display_path(asset.path, root)
    )
    if canonical_manifest:
        return {
            **record,
            "status": "pending_classification",
            "materialized_path": materialized_path,
            "error": None,
            "materialization": {
                "output_path": materialized_path,
                "sha256": asset.sha256,
                "size_bytes": asset.size_bytes,
                "mime_type": asset.mime_type,
                "width_px": asset.width,
                "height_px": asset.height,
                "renderer": asset.renderer,
                "renderer_version": asset.renderer_version,
                "dpi": dpi if record.get("origin", {}).get("kind") == "pdf_page" else None,
                "signature": asset.signature,
                "cache_hit": asset.cache_hit,
                "generated_at": asset.generated_at,
            },
        }
    return {
        **record,
        "status": "materialized",
        "materialized_path": materialized_path,
        "materialized_sha256": asset.sha256,
        "mime_type": asset.mime_type,
        "width": asset.width,
        "height": asset.height,
        "provenance": {
            "materializer": MATERIALIZER,
            "version": MATERIALIZER_VERSION,
            "operation": asset.operation,
            "source_path": relative_source,
            "source_sha256": expected_source_sha256,
            "materialization_signature": asset.signature,
            "cache_hit": asset.cache_hit,
            "dpi": dpi if record.get("origin", {}).get("kind") == "pdf_page" else None,
        },
    }


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    try:
        lines: Iterable[str] = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise MaterializationError(f"cannot read input JSONL: {path}: {exc}") from exc
    for line_number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise MaterializationError(f"invalid JSON at {path}:{line_number}: {exc}") from exc
        if not isinstance(value, dict):
            raise MaterializationError(f"record at {path}:{line_number} must be an object")
        asset_id = safe_asset_id(value.get("asset_id"))
        if asset_id in seen_ids:
            raise MaterializationError(f"duplicate asset_id at {path}:{line_number}: {asset_id}")
        seen_ids.add(asset_id)
        records.append(value)
    if not records:
        raise MaterializationError("input JSONL contains no records")
    return records


def atomic_write_jsonl(path: Path, records: Iterable[dict[str, Any]]) -> None:
    data = "".join(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n" for record in records)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=".jsonl-", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True, help="Root for relative source_path values")
    parser.add_argument("--input", type=Path, required=True, help="Selected visual assets JSONL")
    parser.add_argument("--out-dir", type=Path, required=True, help="Immutable materialized image directory")
    parser.add_argument("--output", type=Path, required=True, help="Output materialization JSONL")
    parser.add_argument("--dpi", type=int, default=200, help="PDF rendering DPI (default: 200)")
    parser.add_argument("--pdftoppm", default="pdftoppm", help="pdftoppm executable")
    parser.add_argument("--soffice", default="soffice", help="LibreOffice executable for EMF/WMF")
    parser.add_argument(
        "--max-member-bytes", type=int, default=100 * 1024 * 1024,
        help="Maximum uncompressed Office media member size",
    )
    args = parser.parse_args()
    if not args.root.resolve().is_dir():
        parser.error(f"--root is not a directory: {args.root}")
    if not 1 <= args.dpi <= 1200:
        parser.error("--dpi must be between 1 and 1200")
    if args.max_member_bytes < 1:
        parser.error("--max-member-bytes must be positive")
    try:
        records = read_jsonl(args.input)
    except MaterializationError as exc:
        parser.error(str(exc))
    results = [
        materialized_record(
            record, args.root, args.out_dir, dpi=args.dpi, pdftoppm=args.pdftoppm,
            soffice=args.soffice,
            max_member_bytes=args.max_member_bytes,
        )
        for record in records
    ]
    atomic_write_jsonl(args.output, results)
    errors = sum(result["status"] in {"error", "materialization_error"} for result in results)
    unsupported = sum(result["status"] == "unsupported_media" for result in results)
    cached = sum(bool(
        (result.get("materialization") or result.get("provenance") or {}).get("cache_hit")
    ) for result in results)
    completed = sum(result["status"] in {"materialized", "pending_classification"} for result in results)
    print(
        f"materialized={completed} errors={errors} "
        f"unsupported={unsupported} cache_hits={cached} "
        f"output={args.output}"
    )
    return 1 if errors or unsupported else 0


if __name__ == "__main__":
    raise SystemExit(main())
