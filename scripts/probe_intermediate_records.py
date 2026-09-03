#!/usr/bin/env python3
"""Generate a small, question-independent intermediate-record sample.

This is a diagnostic probe, not the production answer pipeline.  It accepts
arbitrary input paths and applies the same suffix-based dispatch to every file.
"""

from __future__ import annotations

import argparse
import base64
import codecs
import csv
import hashlib
import io
import json
import mimetypes
import posixpath
import re
import unicodedata
import zipfile
from datetime import date, datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable
from xml.etree import ElementTree

from evidence_text_chunking import (
    MAX_QUESTION_EVIDENCE_CHARS,
    exact_text_chunks,
)


SCHEMA_VERSION = "0.1"
EXTRACTOR = "intermediate-record-probe"
EXTRACTOR_VERSION = "0.6.0"
FORMULA_CACHED_VALUE_STATUS = "stored_in_file_not_recalculated"

PLAIN_TEXT_SUFFIXES = {
    ".md", ".txt", ".py", ".toml", ".yaml", ".yml", ".rst", ".sql", ".sh", ".command",
}
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp"}
OCR_QUALITY_BY_AGREEMENT = {
    "independent_agreement": "high",
    "same_engine_agreement": "provisional",
    "provisional_single_pass": "provisional",
}
PROVISIONAL_OCR_MARKER = "[暫定読取]"
OCR_BBOX_COORDINATE_SYSTEMS = {
    "raw_raster_top_left_normalized_1000",
    "display_oriented_top_left_normalized_1000",
    "source_orientation_1_top_left_normalized_1000",
}
OCR_ENGINE_BY_PASS = {
    "apple_vision_primary": "apple_vision",
    "apple_vision_literal": "apple_vision",
    "apple_vision_fast_sparse": "apple_vision",
    "paddleocr_primary": "paddleocr",
    "tesseract_psm3": "tesseract",
    "tesseract_psm6": "tesseract",
    "tesseract_psm11": "tesseract",
}
CODE_SUFFIXES = {".py", ".toml", ".yaml", ".yml", ".sql", ".sh", ".command"}
DIRECT_TEXT_SUFFIXES = PLAIN_TEXT_SUFFIXES | {".csv", ".tsv", ".json", ".xml", ".ipynb"}
MAX_DIRECT_TEXT_BYTES = 64 * 1024 * 1024
STREAM_TEXT_READ_CHARS = 64 * 1024
MAX_STREAM_TEXT_READ_BLOCKS = 4096
TEXT_ENCODING_SNIFF_BYTES = 64 * 1024
TEXT_ENCODING_SCAN_BYTES = 1024 * 1024
MAX_OOXML_ARCHIVE_BYTES = 256 * 1024 * 1024
MAX_OOXML_ZIP_ENTRIES = 10_000
MAX_OOXML_MEMBER_BYTES = 128 * 1024 * 1024
MAX_OOXML_TOTAL_BYTES = 512 * 1024 * 1024
MAX_OOXML_COMPRESSION_RATIO = 500.0
OOXML_FORBIDDEN_XML = (b"<!DOCTYPE", b"<!ENTITY")
DOCX_REQUIRED_OOXML_MEMBERS = frozenset({
    "[Content_Types].xml",
    "_rels/.rels",
    "word/document.xml",
})
PPTX_REQUIRED_OOXML_MEMBERS = frozenset({
    "[Content_Types].xml",
    "_rels/.rels",
    "ppt/presentation.xml",
    "ppt/_rels/presentation.xml.rels",
})
XML_DECLARATION_ENCODING = re.compile(
    r"<\?xml\s+[^>]{0,512}?\bencoding\s*=\s*(['\"])([A-Za-z][A-Za-z0-9._-]*)\1",
    re.IGNORECASE,
)
DATA_URI_PATTERN = re.compile(
    r"data:(image/[A-Za-z0-9.+-]+);base64,([A-Za-z0-9+/=\r\n\t ]+)",
    re.IGNORECASE,
)
CONTROL_PATTERN = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
DATE_TOKEN_PATTERN = re.compile(r"(?<!\d)(20\d{6})(?!\d)")
ALIAS_DATE_PATTERN = re.compile(r"([A-Za-z]{2,})[-_]?((?:20)\d{6})")


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def ocr_engine(pass_name: object) -> str:
    if not isinstance(pass_name, str):
        raise ValueError("OCR provenance pass name is missing")
    try:
        return OCR_ENGINE_BY_PASS[pass_name]
    except KeyError as exc:
        raise ValueError(f"unsupported OCR provenance pass: {pass_name!r}") from exc


def ocr_independence_group(pass_name: object) -> str:
    return ocr_engine(pass_name)


def _ocr_match_text(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("OCR supporter raw text is missing")
    return unicodedata.normalize("NFC", value).strip()


def _ocr_bbox(value: object) -> list[int]:
    if (
        not isinstance(value, list)
        or len(value) != 4
        or any(isinstance(item, bool) or not isinstance(item, int) for item in value)
        or value[0] < 0
        or value[1] < 0
        or value[2] <= 0
        or value[3] <= 0
        or value[0] + value[2] > 1000
        or value[1] + value[3] > 1000
    ):
        raise ValueError("OCR supporter bbox is invalid")
    return list(value)


def _ocr_overlap(first: list[int], second: list[int]) -> float:
    left = max(first[0], second[0])
    top = max(first[1], second[1])
    right = min(first[0] + first[2], second[0] + second[2])
    bottom = min(first[1] + first[3], second[1] + second[3])
    if right <= left or bottom <= top:
        return 0.0
    intersection = (right - left) * (bottom - top)
    smaller = min(first[2] * first[3], second[2] * second[3])
    return intersection / smaller if smaller else 0.0


def validate_ocr_supporters(
    line: dict[str, Any],
    provenance: dict[str, Any],
    *,
    primary_pass: str,
    primary_engine: str,
    primary_group: str,
    audit_pass: str | None,
    audit_engine: str | None,
    audit_group: str | None,
    agreement_type: str,
) -> None:
    """Recompute line agreement from the two raw engine observations."""
    supporters = provenance.get("supporters")
    expected_count = 1 if audit_pass is None else 2
    if not isinstance(supporters, list) or len(supporters) != expected_count:
        raise ValueError("OCR raw supporters are missing or incomplete")
    expected = [
        (
            primary_pass,
            primary_engine,
            primary_group,
            provenance.get("primary_line_id"),
            provenance.get("primary_bbox_coordinate_system"),
            line.get("primary_confidence"),
        )
    ]
    if audit_pass is not None:
        expected.append((
            audit_pass,
            audit_engine,
            audit_group,
            provenance.get("audit_line_id"),
            provenance.get("audit_bbox_coordinate_system"),
            line.get("audit_confidence"),
        ))
    boxes: list[list[int]] = []
    line_text = _ocr_match_text(line.get("text"))
    for supporter, contract in zip(supporters, expected):
        if not isinstance(supporter, dict):
            raise ValueError("OCR supporter must be an object")
        pass_name, engine, group, line_id, coordinate_system, confidence = contract
        if (
            supporter.get("pass") != pass_name
            or supporter.get("engine") != engine
            or supporter.get("independence_group") != group
            or supporter.get("line_id") != line_id
            or supporter.get("bbox_coordinate_system") != coordinate_system
        ):
            raise ValueError("OCR supporter identity disagrees with provenance")
        if _ocr_match_text(supporter.get("raw_text")) != line_text:
            raise ValueError("OCR supporter text does not reproduce the consensus")
        actual_confidence = supporter.get("confidence")
        if (
            isinstance(actual_confidence, bool)
            or not isinstance(actual_confidence, (int, float))
            or confidence is None
            or float(actual_confidence) != float(confidence)
        ):
            raise ValueError("OCR supporter confidence disagrees with provenance")
        boxes.append(_ocr_bbox(supporter.get("bbox")))
    result_bbox = _ocr_bbox(line.get("bbox"))
    union = [
        min(box[0] for box in boxes),
        min(box[1] for box in boxes),
        max(box[0] + box[2] for box in boxes),
        max(box[1] + box[3] for box in boxes),
    ]
    union[2] -= union[0]
    union[3] -= union[1]
    if result_bbox != union:
        raise ValueError("OCR consensus bbox does not reproduce the supporter union")
    claimed_overlap = line.get("overlap")
    if expected_count == 1:
        if claimed_overlap != 0.0 or agreement_type != "provisional_single_pass":
            raise ValueError("single-pass OCR supporter contract is invalid")
        return
    recomputed_overlap = _ocr_overlap(boxes[0], boxes[1])
    if (
        isinstance(claimed_overlap, bool)
        or not isinstance(claimed_overlap, (int, float))
        or abs(float(claimed_overlap) - round(recomputed_overlap, 6)) > 0.000001
        or recomputed_overlap < 0.5
    ):
        raise ValueError("OCR supporter overlap does not reproduce the consensus")


def digest_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def digest_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def digest_value(value: Any) -> str:
    return digest_bytes(canonical_json(value).encode("utf-8"))


def stable_id(prefix: str, value: Any) -> str:
    return f"{prefix}_{digest_value(value)[:32]}"


def normalize_text(value: str) -> str:
    """Normalize transport noise without changing names, numbers, symbols, or negation."""
    normalized = unicodedata.normalize("NFC", value).replace("\r\n", "\n").replace("\r", "\n")
    normalized = CONTROL_PATTERN.sub("", normalized)
    lines = [re.sub(r"[\t\u00a0 ]+", " ", line).rstrip() for line in normalized.split("\n")]
    compact: list[str] = []
    blank = False
    for line in lines:
        if line:
            compact.append(line)
            blank = False
        elif compact and not blank:
            compact.append("")
            blank = True
    while compact and not compact[-1]:
        compact.pop()
    return "\n".join(compact)


def detect_text_encoding(raw: bytes, *, partial_sample: bool = False) -> str:
    """Detect a common native-text encoding from bounded or complete bytes."""
    def decodes(encoding: str) -> bool:
        try:
            decoder = codecs.getincrementaldecoder(encoding)(errors="strict")
            decoder.decode(raw, final=not partial_sample)
            return True
        except UnicodeDecodeError:
            return False

    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return "utf-16"
    if raw and raw.count(b"\x00") / len(raw) > 0.1:
        odd_nuls = raw[1::2].count(0)
        even_nuls = raw[0::2].count(0)
        likely_utf16 = "utf-16-le" if odd_nuls >= even_nuls else "utf-16-be"
        if decodes(likely_utf16):
            return likely_utf16
    for encoding in ("utf-8-sig", "cp932"):
        if decodes(encoding):
            return encoding
    return "utf-8-replacement"


def detect_text_file_encoding(path: Path) -> str:
    """Select a text encoding after a bounded-memory full-file validation.

    An ASCII prefix cannot distinguish UTF-8 from CP932. Choosing from only a
    prefix would corrupt Japanese that appears later in a large file, so every
    candidate decoder is advanced over the complete byte stream while keeping
    only decoder state in memory.
    """
    with path.open("rb") as handle:
        sample = handle.read(TEXT_ENCODING_SNIFF_BYTES)
    if sample.startswith((b"\xff\xfe", b"\xfe\xff")):
        candidates = ["utf-16"]
    elif sample and sample.count(b"\x00") / len(sample) > 0.1:
        odd_nuls = sample[1::2].count(0)
        even_nuls = sample[0::2].count(0)
        likely_utf16 = "utf-16-le" if odd_nuls >= even_nuls else "utf-16-be"
        candidates = [likely_utf16, "utf-8-sig", "cp932"]
    else:
        candidates = ["utf-8-sig", "cp932"]

    decoders = {
        encoding: codecs.getincrementaldecoder(encoding)(errors="strict")
        for encoding in candidates
    }
    with path.open("rb") as handle:
        while block := handle.read(TEXT_ENCODING_SCAN_BYTES):
            for encoding in list(decoders):
                try:
                    decoders[encoding].decode(block, final=False)
                except UnicodeDecodeError:
                    del decoders[encoding]
            if not decoders:
                return "utf-8-replacement"
    for encoding in list(decoders):
        try:
            decoders[encoding].decode(b"", final=True)
        except UnicodeDecodeError:
            del decoders[encoding]
    return next((encoding for encoding in candidates if encoding in decoders), "utf-8-replacement")


def read_text(path: Path) -> tuple[str, str]:
    """Read bounded native text while reporting the selected encoding.

    ``Probe.extract`` routes larger text-like files to the streaming reader
    before this helper is called. Keeping this helper simple preserves exact
    decoding for ordinary files without exposing the adaptive pipeline to an
    unbounded allocation.
    """
    if path.stat().st_size > MAX_DIRECT_TEXT_BYTES:
        raise ValueError("direct_text_resource_limit")
    raw = path.read_bytes()
    encoding = detect_text_encoding(raw)
    if encoding == "utf-8-replacement":
        return raw.decode("utf-8", errors="replace"), encoding
    return raw.decode(encoding), encoding


def validate_ooxml_archive(
    source: str | Path | io.BytesIO,
    *,
    required_members: frozenset[str],
) -> None:
    """Fail closed before parsing OOXML with the standard library.

    The fallback must not turn a ZIP bomb, encrypted member, duplicate/path
    ambiguity, or XML entity payload into an extraction-time resource attack.
    Validation reads XML parts only after their declared sizes and compression
    ratios have passed bounded checks.
    """
    if isinstance(source, io.BytesIO):
        archive_bytes = source.getbuffer().nbytes
        source.seek(0)
    else:
        archive_bytes = Path(source).stat().st_size
    if not 0 < archive_bytes <= MAX_OOXML_ARCHIVE_BYTES:
        raise ValueError("ooxml_archive_resource_limit")

    try:
        with zipfile.ZipFile(source) as archive:
            infos = archive.infolist()
            if not 1 <= len(infos) <= MAX_OOXML_ZIP_ENTRIES:
                raise ValueError("ooxml_archive_invalid")
            seen: set[str] = set()
            logical_paths: set[str] = set()
            total = 0
            for info in infos:
                name = info.filename
                directory = info.is_dir()
                logical_name = name[:-1] if directory else name
                pure = PurePosixPath(logical_name)
                if (
                    not logical_name
                    or "\\" in name
                    or pure.is_absolute()
                    or any(part in {"", ".", ".."} for part in pure.parts)
                    or posixpath.normpath(logical_name) != logical_name
                    or name in seen
                    or logical_name in logical_paths
                    or info.flag_bits & 0x1
                ):
                    raise ValueError("ooxml_archive_invalid")
                seen.add(name)
                logical_paths.add(logical_name)
                if directory:
                    if info.file_size != 0:
                        raise ValueError("ooxml_archive_invalid")
                    continue
                if not 0 <= info.file_size <= MAX_OOXML_MEMBER_BYTES:
                    raise ValueError("ooxml_archive_resource_limit")
                if info.file_size and info.compress_size == 0:
                    raise ValueError("ooxml_archive_invalid")
                if (
                    info.compress_size
                    and info.file_size / info.compress_size > MAX_OOXML_COMPRESSION_RATIO
                ):
                    raise ValueError("ooxml_archive_resource_limit")
                total += info.file_size
                if total > MAX_OOXML_TOTAL_BYTES:
                    raise ValueError("ooxml_archive_resource_limit")
                if name.casefold().endswith((".xml", ".rels")):
                    validate_xml_bytes(archive.read(info))
            if not required_members.issubset(seen):
                raise ValueError("ooxml_archive_invalid")
    finally:
        if isinstance(source, io.BytesIO):
            source.seek(0)


def xml_storage_encoding(value: bytes) -> tuple[str, int]:
    """Return the XML byte encoding selected by BOM/signature sniffing."""
    prefix = value[:4]
    if prefix.startswith(b"\x00\x00\xfe\xff"):
        return "utf-32-be", 4
    if prefix.startswith(b"\xff\xfe\x00\x00"):
        return "utf-32-le", 4
    if prefix.startswith(b"\xfe\xff"):
        return "utf-16-be", 2
    if prefix.startswith(b"\xff\xfe"):
        return "utf-16-le", 2
    if prefix.startswith(b"\xef\xbb\xbf"):
        return "utf-8", 3
    if prefix == b"\x00\x00\x00<":
        return "utf-32-be", 0
    if prefix == b"<\x00\x00\x00":
        return "utf-32-le", 0
    if prefix == b"\x00<\x00?":
        return "utf-16-be", 0
    if prefix == b"<\x00?\x00":
        return "utf-16-le", 0
    return "ascii-compatible", 0


def normalized_xml_encoding(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.casefold())


def validate_xml_bytes(value: bytes) -> None:
    """Reject DTD/entity declarations before ElementTree sees any XML bytes.

    OOXML normally uses UTF-8, but XML also permits BOM/signature-selected
    UTF-16 and UTF-32.  Searching only ASCII bytes leaves those encodings as a
    bypass, so wide encodings are scanned with their exact code-unit layout.
    The XML declaration, when present, must agree with the detected family.
    """
    encoding, bom_bytes = xml_storage_encoding(value)
    if encoding == "ascii-compatible":
        if b"\x00" in value[:512]:
            raise ValueError("ooxml_xml_encoding_invalid")
        upper = value.upper()
        if any(marker in upper for marker in OOXML_FORBIDDEN_XML):
            raise ValueError("ooxml_xml_unsafe")
        prolog = value[:2048].decode("ascii", errors="ignore")
        declaration = XML_DECLARATION_ENCODING.search(prolog)
        if declaration:
            declared = normalized_xml_encoding(declaration.group(2))
            if declared in {"utf16", "utf16le", "utf16be", "utf32", "utf32le", "utf32be"}:
                raise ValueError("ooxml_xml_encoding_invalid")
        return

    if encoding == "utf-8":
        if any(marker in value.upper() for marker in OOXML_FORBIDDEN_XML):
            raise ValueError("ooxml_xml_unsafe")
    elif any(marker.decode("ascii").encode(encoding) in value for marker in OOXML_FORBIDDEN_XML):
        raise ValueError("ooxml_xml_unsafe")
    try:
        prolog = value[bom_bytes:2048].decode(encoding, errors="strict")
    except UnicodeDecodeError as exc:
        raise ValueError("ooxml_xml_encoding_invalid") from exc
    declaration = XML_DECLARATION_ENCODING.search(prolog)
    if not declaration:
        return
    declared = normalized_xml_encoding(declaration.group(2))
    family = (
        "utf32" if encoding.startswith("utf-32")
        else "utf16" if encoding.startswith("utf-16")
        else "utf8"
    )
    compatible = {family, normalized_xml_encoding(encoding)}
    if declared not in compatible:
        raise ValueError("ooxml_xml_encoding_invalid")


def discover_password_candidates(root: Path) -> tuple[str, ...]:
    """Derive generic Office password candidates from path-visible aliases and dates."""
    aliases: set[str] = set()
    dates: set[str] = set()
    embedded: set[str] = set()
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        visible = unicodedata.normalize("NFC", path.as_posix())
        dates.update(DATE_TOKEN_PATTERN.findall(visible))
        for alias, date_token in ALIAS_DATE_PATTERN.findall(visible):
            aliases.add(alias)
            dates.add(date_token)
            embedded.add(f"{alias}{date_token}")
    candidates = set(embedded)
    for alias in aliases:
        for date_token in dates:
            for extension in ("docx", "xlsx", "pptx"):
                candidates.add(f"DA-{alias}-{date_token}-{extension}")
                candidates.add(f"DA-{alias.upper()}-{date_token}-{extension}")
    return tuple(sorted(candidates))


def json_value(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, bytes):
        return {"bytes_sha256": digest_bytes(value), "length": len(value)}
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(key): json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_value(item) for item in value]
    return str(value)


def value_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, datetime):
        return "datetime"
    if isinstance(value, date):
        return "date"
    if isinstance(value, str):
        return "text"
    if isinstance(value, bytes):
        return "binary"
    return "mixed"


def content(*, raw_text: str | None = None, raw_value: Any = None,
            content_ref: str | None = None, mime_type: str | None = None) -> dict[str, Any]:
    if raw_text is not None:
        payload: dict[str, Any] = {"raw_text": raw_text, "normalized_text": normalize_text(raw_text)}
        hashed = {"raw_text": raw_text}
        kind = "text"
        original_length = len(raw_text)
    elif content_ref is not None:
        payload = {"content_ref": content_ref}
        hashed = {"content_ref": content_ref}
        kind = "binary"
        original_length = None
    else:
        clean = json_value(raw_value)
        payload = {"raw_value": clean, "normalized_value": clean}
        hashed = {"raw_value": clean}
        kind = value_type(raw_value)
        original_length = None
    payload.update({"value_type": kind, "sha256": digest_value(hashed), "is_truncated": False})
    if mime_type:
        payload["mime_type"] = mime_type
    if original_length is not None:
        payload["original_length"] = original_length
    return payload


def nfc_path(path: Path) -> str:
    return unicodedata.normalize("NFC", path.as_posix())


class Probe:
    def __init__(
        self,
        root: Path,
        run_at: str,
        max_items: int | None,
        *,
        diagnostic: bool = True,
        extractor: str = EXTRACTOR,
        extractor_version: str = EXTRACTOR_VERSION,
        record_sink: Callable[[str, dict[str, Any]], None] | None = None,
        retain_records: bool = True,
        password_candidates: tuple[str, ...] = (),
    ) -> None:
        self.root = root.resolve()
        self.run_at = run_at
        self.max_items = max_items
        self.diagnostic = diagnostic
        self.extractor = extractor
        self.extractor_version = extractor_version
        self.record_sink = record_sink
        self.retain_records = retain_records
        self.password_candidates = password_candidates
        self.documents: list[dict[str, Any]] = []
        self.evidence: list[dict[str, Any]] = []
        self.relations: list[dict[str, Any]] = []
        self._leaf_counts: dict[str, int] = {}
        self._current_document: dict[str, Any] | None = None

    def emit(self, kind: str, record: dict[str, Any]) -> None:
        if self.retain_records:
            getattr(self, kind).append(record)
        if self.record_sink is not None:
            self.record_sink(kind, record)

    def relative_path(self, path: Path) -> str:
        resolved = path.resolve()
        try:
            relative = resolved.relative_to(self.root)
        except ValueError as exc:
            raise ValueError(f"input is outside --root: {resolved}") from exc
        return nfc_path(relative)

    def add_document(self, path: Path, parser: str) -> dict[str, Any]:
        rel = self.relative_path(path)
        source_sha = digest_file(path)
        doc_id = stable_id("doc", {"relative_path": rel, "source_sha256": source_sha})
        stat = path.stat()
        if self.diagnostic:
            initial_status = "partial"
            initial_warnings = [
                f"diagnostic sample: at most {self.max_items} leaf items per document",
                "must not be used as the answer pipeline index",
            ]
        else:
            initial_status = "success"
            initial_warnings = []
        record = {
            "schema_version": SCHEMA_VERSION,
            "record_type": "document",
            "document_id": doc_id,
            "source": {
                "relative_path": rel,
                "file_name": unicodedata.normalize("NFC", path.name),
                "extension": path.suffix.lower().lstrip("."),
                "media_type": mimetypes.guess_type(path.name)[0] or "application/octet-stream",
                "size_bytes": stat.st_size,
                "sha256": source_sha,
                "modified_at": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
            },
            "extraction": {
                "status": initial_status,
                "parser": parser,
                "parser_version": self.extractor_version,
                "extracted_at": self.run_at,
                "warnings": initial_warnings,
                "errors": [],
            },
        }
        if self._current_document is not None:
            raise RuntimeError("only one source document may be active per Probe instance")
        self._current_document = record
        if self.retain_records:
            self.documents.append(record)
        self._leaf_counts[doc_id] = 0
        return record

    def finalize_document(self) -> dict[str, Any]:
        if self._current_document is None:
            raise RuntimeError("no active source document to finalize")
        document = self._current_document
        if self.record_sink is not None:
            self.record_sink("documents", document)
        self._current_document = None
        return document

    def may_add_leaf(self, doc_id: str) -> bool:
        if self.max_items is not None and self._leaf_counts[doc_id] >= self.max_items:
            return False
        self._leaf_counts[doc_id] += 1
        return True

    def limit_reached(self, doc_id: str) -> bool:
        return self.max_items is not None and self._leaf_counts[doc_id] >= self.max_items

    @staticmethod
    def mark_partial(document: dict[str, Any], warning: str) -> None:
        document["extraction"]["status"] = "partial"
        if warning not in document["extraction"]["warnings"]:
            document["extraction"]["warnings"].append(warning)

    def record_failure(self, path: Path, error: Exception) -> None:
        existing = self._current_document or next(
            (item for item in reversed(self.documents) if item["source"]["relative_path"] == self.relative_path(path)),
            None,
        )
        document = existing or self.add_document(path, f"{path.suffix.lower().lstrip('.') or 'unknown'}-parser")
        document["extraction"]["status"] = "failed"
        document["extraction"]["errors"] = [f"{type(error).__name__}: {error}"]

    def add_evidence(
        self,
        document_id: str,
        evidence_type: str,
        location: dict[str, Any],
        item_content: dict[str, Any],
        *,
        parent_id: str | None = None,
        ordinal: int | None = None,
        style: dict[str, Any] | None = None,
        geometry: dict[str, Any] | None = None,
        native_properties: dict[str, Any] | None = None,
        method: str = "native_parser",
        confidence: float = 1.0,
        deterministic: bool = True,
        warning: str | None = None,
    ) -> dict[str, Any]:
        identity = {
            "document_id": document_id,
            "evidence_type": evidence_type,
            "location": location,
            "content_sha256": item_content["sha256"],
        }
        record: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "record_type": "evidence",
            "evidence_id": stable_id("ev", identity),
            "document_id": document_id,
            "evidence_type": evidence_type,
            "location": location,
            "content": item_content,
            "provenance": {
                "extraction_method": method,
                "extractor": self.extractor,
                "extractor_version": self.extractor_version,
                "extracted_at": self.run_at,
                "deterministic": deterministic,
                "confidence": confidence,
                "warnings": [warning] if warning else [],
            },
        }
        if parent_id:
            record["parent_evidence_id"] = parent_id
        if ordinal is not None:
            record["ordinal"] = ordinal
        if style:
            record["style"] = style
        if geometry:
            record["geometry"] = geometry
        if native_properties:
            record["native_properties"] = native_properties
        self.emit("evidence", record)
        if parent_id:
            self.add_relation(
                "structural", "contains",
                {"record_type": "evidence", "record_id": parent_id},
                {"record_type": "evidence", "record_id": record["evidence_id"]},
            )
        return record

    def add_relation(self, relation_class: str, relation_type: str,
                     from_ref: dict[str, str], to_ref: dict[str, str]) -> None:
        identity = {
            "class": relation_class,
            "type": relation_type,
            "from": from_ref,
            "to": to_ref,
            "generator": self.extractor,
            "generator_version": self.extractor_version,
        }
        self.emit("relations", {
            "schema_version": SCHEMA_VERSION,
            "record_type": "relation",
            "relation_id": stable_id("rel", identity),
            "relation_class": relation_class,
            "relation_type": relation_type,
            "from_ref": from_ref,
            "to_ref": to_ref,
            "properties": {},
            "supporting_evidence_ids": [],
            "provenance": {
                "generated_by": self.extractor,
                "generator_version": self.extractor_version,
                "generated_at": self.run_at,
                "deterministic": True,
                "confidence": 1.0,
                "rule_or_model": "native containment",
                "warnings": [],
            },
            "status": "verified",
        })

    def contain_document(self, doc_id: str, evidence_id: str) -> None:
        self.add_relation(
            "structural", "contains",
            {"record_type": "document", "record_id": doc_id},
            {"record_type": "evidence", "record_id": evidence_id},
        )

    def extract(self, path: Path) -> None:
        suffix = path.suffix.lower()
        if suffix in DIRECT_TEXT_SUFFIXES and path.stat().st_size > MAX_DIRECT_TEXT_BYTES:
            self.extract_large_text(path)
        elif suffix == ".docx":
            self.extract_docx(path)
        elif suffix == ".xlsx":
            self.extract_xlsx(path)
        elif suffix == ".pptx":
            self.extract_pptx(path)
        elif suffix == ".pdf":
            self.extract_pdf(path)
        elif suffix in IMAGE_SUFFIXES:
            self.extract_image(path)
        elif suffix in {".csv", ".tsv"}:
            self.extract_delimited(path)
        elif suffix == ".json":
            self.extract_json(path)
        elif suffix == ".xml":
            self.extract_xml(path)
        elif suffix == ".ipynb":
            self.extract_notebook(path)
        elif suffix in PLAIN_TEXT_SUFFIXES:
            self.extract_plain_text(path)
        else:
            self.extract_other(path)
        self.finalize_document()

    def extract_large_text(self, path: Path) -> None:
        """Retain searchable text from a large file with bounded memory.

        Structural parsers for JSON, XML, notebooks and delimited files first
        materialize the complete source. For a large source, preserving the
        text in deterministic chunks is more useful than either crashing or
        pretending that the structure was fully parsed. The explicit partial
        status lets a later specialized reader replace this fallback.
        """
        doc = self.add_document(path, "bounded-text-stream")
        doc_id = doc["document_id"]
        self.mark_partial(
            doc,
            "large text-like file read with bounded streaming; native structure remains unresolved",
        )
        detected = detect_text_file_encoding(path)
        decoding = "utf-8" if detected == "utf-8-replacement" else detected
        replacement_count = 0
        character_offset = 0
        read_block_count = 0
        chunk_count = 0
        stopped_at_item_limit = False
        with path.open("r", encoding=decoding, errors="replace", newline="") as handle:
            while True:
                if read_block_count >= MAX_STREAM_TEXT_READ_BLOCKS:
                    if handle.read(1):
                        self.mark_partial(
                            doc,
                            "streaming extraction stopped after "
                            f"{MAX_STREAM_TEXT_READ_BLOCKS} read blocks",
                        )
                    break
                block = handle.read(STREAM_TEXT_READ_CHARS)
                if not block:
                    break
                read_block_count += 1
                replacement_count += block.count("\ufffd")
                for question_chunk in exact_text_chunks(block):
                    if not self.may_add_leaf(doc_id):
                        self.mark_partial(
                            doc,
                            "streaming extraction stopped at the configured item limit",
                        )
                        stopped_at_item_limit = True
                        break
                    chunk_count += 1
                    start = character_offset + question_chunk.start
                    end = character_offset + question_chunk.end
                    evidence_type = (
                        "code_block"
                        if path.suffix.lower() in CODE_SUFFIXES else "text_block"
                    )
                    ev = self.add_evidence(
                        doc_id,
                        evidence_type,
                        {
                            "object_index": chunk_count,
                            "locator_text": f"characters={start + 1}-{end}",
                        },
                        content(raw_text=question_chunk.text),
                        ordinal=chunk_count,
                        native_properties={
                            "encoding": detected,
                            "character_start": start,
                            "character_end": end,
                            "character_offset_basis": "zero_based_half_open",
                            "source_stream_read_block": read_block_count,
                            "source_structure_status": "unresolved",
                        },
                        method="bounded_streaming_text",
                        warning=(
                            "native structure was not parsed for this large text-like source"
                        ),
                    )
                    self.contain_document(doc_id, ev["evidence_id"])
                character_offset += len(block)
                if stopped_at_item_limit:
                    break
        if not chunk_count:
            self.mark_partial(doc, "large text-like file contained no decodable text")
        if replacement_count:
            self.mark_partial(
                doc,
                f"streaming decoder inserted {replacement_count} replacement character(s)",
            )

    def office_source(self, path: Path) -> tuple[str | io.BytesIO, bool]:
        if zipfile.is_zipfile(path):
            return str(path), False
        try:
            import msoffcrypto
        except ImportError as exc:
            raise RuntimeError("msoffcrypto-tool is required for password-protected Office files") from exc
        for candidate in self.password_candidates:
            try:
                output = io.BytesIO()
                with path.open("rb") as handle:
                    office = msoffcrypto.OfficeFile(handle)
                    office.load_key(password=candidate)
                    office.decrypt(output)
                output.seek(0)
                return output, True
            except Exception:
                continue
        raise ValueError("password-protected Office file could not be decrypted with derived candidates")

    def extract_docx(self, path: Path) -> None:
        from docx import Document
        from docx.oxml.table import CT_Tbl
        from docx.oxml.text.paragraph import CT_P
        from docx.table import Table
        from docx.text.paragraph import Paragraph

        source, decrypted = self.office_source(path)
        validate_ooxml_archive(
            source,
            required_members=DOCX_REQUIRED_OOXML_MEMBERS,
        )
        parsed = Document(source)
        doc = self.add_document(path, "python-docx")
        doc_id = doc["document_id"]
        if decrypted:
            doc["extraction"]["warnings"].append("password-protected Office source decrypted in memory")
        paragraph_index = 0
        table_index = 0
        body_order = 0
        preceding_heading: dict[str, str] | None = None
        for child in parsed.element.body.iterchildren():
            if isinstance(child, CT_P):
                paragraph_index += 1
                body_order += 1
                paragraph = Paragraph(child, parsed)
                if not paragraph.text or not self.may_add_leaf(doc_id):
                    continue
                style: dict[str, Any] = {}
                if paragraph.style and paragraph.style.style_id:
                    style["source_style_id"] = paragraph.style.style_id
                evidence_type = "heading" if paragraph.style and paragraph.style.name.lower().startswith("heading") else "paragraph"
                ev = self.add_evidence(
                    doc_id, evidence_type, {"paragraph_index": paragraph_index}, content(raw_text=paragraph.text),
                    ordinal=paragraph_index, style=style or None,
                    native_properties={
                        "paragraph_style_name": paragraph.style.name if paragraph.style else None,
                        "body_order": body_order,
                    },
                )
                self.contain_document(doc_id, ev["evidence_id"])
                for run_index, run in enumerate(paragraph.runs, 1):
                    if not run.text or not self.may_add_leaf(doc_id):
                        continue
                    run_style: dict[str, Any] = {}
                    for key in ("bold", "italic", "underline"):
                        value = getattr(run, key, None)
                        if value is not None:
                            run_style[key] = bool(value)
                    if run.font.name:
                        run_style["font_name"] = run.font.name
                    if run.font.size:
                        run_style["font_size_pt"] = float(run.font.size.pt)
                    color = getattr(getattr(run.font, "color", None), "rgb", None)
                    if color:
                        run_style["font_color_argb"] = f"FF{color}"[-8:].upper()
                    highlight = getattr(run.font, "highlight_color", None)
                    if run_style or highlight is not None:
                        self.add_evidence(
                            doc_id,
                            "style_span",
                            {"paragraph_index": paragraph_index, "object_index": run_index},
                            content(raw_text=run.text),
                            parent_id=ev["evidence_id"],
                            ordinal=run_index,
                            style=run_style or None,
                            native_properties={"highlight_color": str(highlight) if highlight is not None else None},
                        )
                if evidence_type == "heading":
                    preceding_heading = {"text": paragraph.text, "evidence_id": ev["evidence_id"]}
            elif isinstance(child, CT_Tbl):
                table_index += 1
                body_order += 1
                table = Table(child, parsed)
                native_properties: dict[str, Any] = {"body_order": body_order}
                if preceding_heading is not None:
                    native_properties.update({
                        "preceding_heading_text": preceding_heading["text"],
                        "preceding_heading_evidence_id": preceding_heading["evidence_id"],
                    })
                table_ev = self.add_evidence(
                    doc_id, "table", {"table_index": table_index},
                    content(raw_value={"rows": len(table.rows), "columns": len(table.columns)}),
                    ordinal=table_index,
                    native_properties=native_properties,
                )
                self.contain_document(doc_id, table_ev["evidence_id"])
                if preceding_heading is not None:
                    self.add_relation(
                        "structural", "section_contains",
                        {"record_type": "evidence", "record_id": preceding_heading["evidence_id"]},
                        {"record_type": "evidence", "record_id": table_ev["evidence_id"]},
                    )
                for row_index, row in enumerate(table.rows, 1):
                    for column_index, cell in enumerate(row.cells, 1):
                        if not self.may_add_leaf(doc_id):
                            break
                        self.add_evidence(
                            doc_id, "table_cell",
                            {"table_index": table_index, "row_index": row_index, "column_index": column_index},
                            content(raw_text=cell.text), parent_id=table_ev["evidence_id"],
                            ordinal=column_index,
                        )
                    if self.limit_reached(doc_id):
                        break

        seen_parts: set[str] = set()
        for section_index, section in enumerate(parsed.sections, 1):
            for evidence_type, part in (("header", section.header), ("footer", section.footer)):
                part_name = str(getattr(getattr(part, "part", None), "partname", f"{evidence_type}-{section_index}"))
                if part_name in seen_parts:
                    continue
                seen_parts.add(part_name)
                values = [paragraph.text for paragraph in part.paragraphs if paragraph.text]
                for table in part.tables:
                    values.extend(
                        " | ".join(cell.text for cell in row.cells)
                        for row in table.rows
                        if any(cell.text for cell in row.cells)
                    )
                text_value = "\n".join(values).strip()
                if text_value and self.may_add_leaf(doc_id):
                    ev = self.add_evidence(
                        doc_id,
                        evidence_type,
                        {"section": f"section-{section_index}", "source_member": part_name},
                        content(raw_text=text_value),
                        ordinal=section_index,
                    )
                    self.contain_document(doc_id, ev["evidence_id"])

        comments = getattr(parsed, "comments", None)
        if comments is not None:
            for comment_index, comment in enumerate(comments, 1):
                text_value = getattr(comment, "text", "")
                if text_value and self.may_add_leaf(doc_id):
                    ev = self.add_evidence(
                        doc_id,
                        "comment",
                        {"object_index": comment_index, "locator_text": f"comment={comment_index}"},
                        content(raw_text=text_value),
                        ordinal=comment_index,
                        native_properties={
                            "author": getattr(comment, "author", None),
                            "initials": getattr(comment, "initials", None),
                        },
                    )
                    self.contain_document(doc_id, ev["evidence_id"])
        if isinstance(source, io.BytesIO):
            source.seek(0)
        with zipfile.ZipFile(source) as archive:
            media_members = sorted(name for name in archive.namelist() if name.startswith("word/media/") and not name.endswith("/"))
            for image_index, member in enumerate(media_members, 1):
                raw = archive.read(member)
                ev = self.add_evidence(
                    doc_id,
                    "image",
                    {"source_member": member, "object_index": image_index},
                    content(content_ref=f"{doc['source']['relative_path']}::{member}", mime_type=mimetypes.guess_type(member)[0]),
                    ordinal=image_index,
                    native_properties={"embedded_sha256": digest_bytes(raw), "size_bytes": len(raw)},
                )
                self.contain_document(doc_id, ev["evidence_id"])

    def extract_xlsx(self, path: Path) -> None:
        try:
            from openpyxl import load_workbook
        except ImportError:
            self.extract_xlsx_ooxml(path)
            return

        source, decrypted = self.office_source(path)
        validate_ooxml_archive(
            source,
            required_members=frozenset({
                "[Content_Types].xml",
                "xl/workbook.xml",
                "xl/_rels/workbook.xml.rels",
            }),
        )
        workbook = load_workbook(source, data_only=False, read_only=False)
        if isinstance(source, io.BytesIO):
            source.seek(0)
        # ``data_only=True`` exposes the result saved in the XLSX package.  It
        # does not calculate the formula, so every downstream representation
        # labels it as a stored, not-recalculated value.
        cached_workbook = load_workbook(source, data_only=True, read_only=True)
        doc = self.add_document(path, "openpyxl+ooxml")
        doc_id = doc["document_id"]
        if decrypted:
            doc["extraction"]["warnings"].append("password-protected Office source decrypted in memory")
        sheet_ids: dict[str, str] = {}
        for sheet_index, sheet in enumerate(workbook.worksheets, 1):
            cached_sheet = cached_workbook[sheet.title]
            cached_rows = iter(cached_sheet.iter_rows())
            sheet_ev = self.add_evidence(
                doc_id, "worksheet", {"sheet_name": sheet.title},
                content(raw_value={"title": sheet.title, "max_row": sheet.max_row, "max_column": sheet.max_column}),
                ordinal=sheet_index,
            )
            sheet_ids[sheet.title] = sheet_ev["evidence_id"]
            self.contain_document(doc_id, sheet_ev["evidence_id"])
            for row in sheet.iter_rows():
                cached_row = next(cached_rows, ())
                cached_by_coordinate = {
                    item.coordinate: item.value
                    for item in cached_row
                    if getattr(item, "coordinate", None)
                }
                for cell_obj in row:
                    if cell_obj.value is None:
                        continue
                    if not self.may_add_leaf(doc_id):
                        break
                    style: dict[str, Any] = {
                        "source_style_id": str(cell_obj.style_id),
                        "number_format": cell_obj.number_format,
                    }
                    if cell_obj.font:
                        if cell_obj.font.bold is not None:
                            style["bold"] = bool(cell_obj.font.bold)
                        if cell_obj.font.italic is not None:
                            style["italic"] = bool(cell_obj.font.italic)
                        if cell_obj.font.underline:
                            style["underline"] = True
                        if cell_obj.font.name:
                            style["font_name"] = cell_obj.font.name
                        if cell_obj.font.sz:
                            style["font_size_pt"] = float(cell_obj.font.sz)
                    if cell_obj.fill and cell_obj.fill.fill_type:
                        style["fill_type"] = cell_obj.fill.fill_type
                    is_formula = cell_obj.data_type == "f"
                    cached_value = (
                        cached_by_coordinate.get(cell_obj.coordinate)
                        if is_formula else None
                    )
                    cell_value = cached_value if is_formula else cell_obj.value
                    native_properties: dict[str, Any] = {
                        "data_type": cell_obj.data_type,
                    }
                    if is_formula:
                        native_properties.update({
                            "cached_value": json_value(cached_value),
                            "cached_value_available": cached_value is not None,
                            "cached_value_status": FORMULA_CACHED_VALUE_STATUS,
                        })
                    cell_ev = self.add_evidence(
                        doc_id, "table_cell", {"sheet_name": sheet.title, "cell": cell_obj.coordinate},
                        content(raw_value=cell_value), parent_id=sheet_ev["evidence_id"],
                        style=style,
                        native_properties=native_properties,
                    )
                    if is_formula:
                        self.add_evidence(
                            doc_id, "formula", {"sheet_name": sheet.title, "cell": cell_obj.coordinate},
                            content(raw_text=str(cell_obj.value)), parent_id=cell_ev["evidence_id"],
                            native_properties={
                                "cached_value": json_value(cached_value),
                                "cached_value_available": cached_value is not None,
                                "cached_value_status": FORMULA_CACHED_VALUE_STATUS,
                            },
                        )
                    if cell_obj.comment is not None:
                        self.add_evidence(
                            doc_id,
                            "comment",
                            {"sheet_name": sheet.title, "cell": cell_obj.coordinate},
                            content(raw_text=cell_obj.comment.text),
                            parent_id=cell_ev["evidence_id"],
                            native_properties={"author": cell_obj.comment.author},
                        )
                if self.limit_reached(doc_id):
                    break
            for merged_index, merged in enumerate(sheet.merged_cells.ranges, 1):
                merged_ev = self.add_evidence(
                    doc_id, "merged_range", {"sheet_name": sheet.title, "range": str(merged)},
                    content(raw_text=str(merged)), parent_id=sheet_ev["evidence_id"], ordinal=merged_index,
                )
                del merged_ev
            for pivot_index, pivot in enumerate(getattr(sheet, "_pivots", []), 1):
                name = getattr(pivot, "name", None) or f"pivot_{pivot_index}"
                self.add_evidence(
                    doc_id, "pivot_table",
                    {"sheet_name": sheet.title, "object_index": pivot_index, "object_id": str(name)},
                    content(raw_value={"name": str(name)}), parent_id=sheet_ev["evidence_id"], ordinal=pivot_index,
                )
            auto_filter = getattr(sheet, "auto_filter", None)
            if auto_filter is not None and getattr(auto_filter, "ref", None):
                ev = self.add_evidence(
                    doc_id,
                    "filter",
                    {"sheet_name": sheet.title, "range": str(auto_filter.ref)},
                    content(raw_value={
                        "ref": str(auto_filter.ref),
                        "filter_columns": [json_value(item) for item in getattr(auto_filter, "filterColumn", [])],
                    }),
                    parent_id=sheet_ev["evidence_id"],
                )
            validations = getattr(getattr(sheet, "data_validations", None), "dataValidation", [])
            for validation_index, validation in enumerate(validations, 1):
                ev = self.add_evidence(
                    doc_id,
                    "data_validation",
                    {"sheet_name": sheet.title, "object_index": validation_index,
                     "range": str(getattr(validation, "sqref", "")) or "unknown"},
                    content(raw_value={
                        "type": getattr(validation, "type", None),
                        "formula1": getattr(validation, "formula1", None),
                        "formula2": getattr(validation, "formula2", None),
                        "operator": getattr(validation, "operator", None),
                    }),
                    parent_id=sheet_ev["evidence_id"],
                    ordinal=validation_index,
                )

        defined_names = getattr(workbook, "defined_names", None)
        if defined_names is not None:
            values = defined_names.values() if hasattr(defined_names, "values") else defined_names.definedName
            for name_index, defined_name in enumerate(values, 1):
                ev = self.add_evidence(
                    doc_id,
                    "defined_name",
                    {"object_index": name_index, "object_id": str(getattr(defined_name, "name", name_index))},
                    content(raw_value={
                        "name": getattr(defined_name, "name", None),
                        "attr_text": getattr(defined_name, "attr_text", None),
                        "local_sheet_id": getattr(defined_name, "localSheetId", None),
                    }),
                    ordinal=name_index,
                )
                self.contain_document(doc_id, ev["evidence_id"])

        if isinstance(source, io.BytesIO):
            source.seek(0)
        with zipfile.ZipFile(source) as archive:
            chart_members = sorted(name for name in archive.namelist() if name.startswith("xl/charts/") and name.endswith(".xml"))
            for chart_index, member in enumerate(chart_members, 1):
                raw = archive.read(member)
                try:
                    root = ElementTree.fromstring(raw)
                    formulas = [node.text.strip() for node in root.iter() if node.tag.endswith("}f") and node.text]
                    labels = [node.text.strip() for node in root.iter() if node.tag.endswith("}v") and node.text]
                except ElementTree.ParseError:
                    formulas, labels = [], []
                chart_ev = self.add_evidence(
                    doc_id, "chart", {"source_member": member, "object_index": chart_index},
                    content(raw_value={
                        "source_member": member,
                        "xml_sha256": digest_bytes(raw),
                        "formulas": formulas,
                        "cached_labels": labels,
                    }),
                    ordinal=chart_index,
                    native_properties={"ooxml_part": member, "extended_chart": "/chartEx" in member},
                )
                self.contain_document(doc_id, chart_ev["evidence_id"])
                for series_index, formula in enumerate(formulas, 1):
                    self.add_evidence(
                        doc_id,
                        "chart_series",
                        {"source_member": member, "object_index": chart_index, "series_index": series_index},
                        content(raw_text=formula),
                        parent_id=chart_ev["evidence_id"],
                        ordinal=series_index,
                    )
            media_members = sorted(name for name in archive.namelist() if name.startswith("xl/media/") and not name.endswith("/"))
            for image_index, member in enumerate(media_members, 1):
                raw = archive.read(member)
                ev = self.add_evidence(
                    doc_id,
                    "image",
                    {"source_member": member, "object_index": image_index},
                    content(content_ref=f"{doc['source']['relative_path']}::{member}", mime_type=mimetypes.guess_type(member)[0]),
                    ordinal=image_index,
                    native_properties={"embedded_sha256": digest_bytes(raw), "size_bytes": len(raw)},
                )
                self.contain_document(doc_id, ev["evidence_id"])
        cached_workbook.close()
        workbook.close()

    def extract_xlsx_ooxml(self, path: Path) -> None:
        """Read core XLSX structure using only OOXML and the standard library.

        This fallback deliberately reports ``partial`` because it does not
        resolve the complete Excel formatting/comment/pivot object model.  It
        still preserves the native sheet name, cell coordinate, formula,
        merge, filter, validation, chart, and embedded-image source binding so
        downstream SearchUnits do not collapse the workbook into flat text.
        """
        source, decrypted = self.office_source(path)
        validate_ooxml_archive(
            source,
            required_members=frozenset({
                "[Content_Types].xml",
                "xl/workbook.xml",
                "xl/_rels/workbook.xml.rels",
            }),
        )
        doc = self.add_document(path, "ooxml-stdlib-xlsx-fallback")
        doc_id = doc["document_id"]
        self.mark_partial(
            doc,
            "openpyxl unavailable; OOXML fallback preserves core workbook structure but limits comments, pivots, and resolved formatting",
        )
        if decrypted:
            doc["extraction"]["warnings"].append("password-protected Office source decrypted in memory")

        def child(element: ElementTree.Element, local_name: str) -> ElementTree.Element | None:
            return next((item for item in element if item.tag.endswith("}" + local_name) or item.tag == local_name), None)

        def attr_by_local_name(element: ElementTree.Element, local_name: str) -> str | None:
            return next((value for key, value in element.attrib.items() if key == local_name or key.endswith("}" + local_name)), None)

        def numeric(raw: str) -> Any:
            # Python floats would round the OOXML decimal lexeme. Preserve
            # decimal/scientific values verbatim; arbitrary-size integers are
            # exact, and every numeric cell also records ``raw_lexeme`` below.
            if not re.fullmatch(r"[+-]?\d+", raw):
                return raw
            try:
                return int(raw)
            except ValueError:
                return raw

        def cell_value(
            cell: ElementTree.Element,
            shared: list[str],
        ) -> tuple[Any, str, str | None, Any, str | None]:
            kind = cell.attrib.get("t", "n")
            formula_node = child(cell, "f")
            value_node = child(cell, "v")
            formula = formula_node.text if formula_node is not None and formula_node.text is not None else None
            raw_value = value_node.text if value_node is not None and value_node.text is not None else ""
            cached: Any = None
            if formula is not None:
                if not raw_value:
                    cached = None
                elif kind == "b":
                    cached = raw_value == "1"
                elif kind in {"str", "e", "d"}:
                    cached = raw_value
                else:
                    cached = numeric(raw_value)
                return cached, "f", formula, cached, raw_value or None
            if kind == "inlineStr":
                value = "".join(item.text or "" for item in cell.iter() if item.tag.endswith("}t") or item.tag == "t")
                return value, "inlineStr", None, None, None
            if kind == "s":
                try:
                    return shared[int(raw_value)], "s", None, None, None
                except (ValueError, IndexError):
                    return raw_value, "s", None, None, None
            if kind == "b":
                return raw_value == "1", "b", None, None, None
            if kind in {"str", "e", "d"}:
                return raw_value, kind, None, None, None
            return numeric(raw_value), kind, None, None, raw_value or None

        if isinstance(source, io.BytesIO):
            source.seek(0)
        with zipfile.ZipFile(source) as archive:
            names = set(archive.namelist())
            if "xl/workbook.xml" not in names:
                raise ValueError("XLSX package has no xl/workbook.xml")

            shared: list[str] = []
            if "xl/sharedStrings.xml" in names:
                shared_root = ElementTree.fromstring(archive.read("xl/sharedStrings.xml"))
                for item in shared_root.iter():
                    if item.tag.endswith("}si") or item.tag == "si":
                        shared.append("".join(
                            node.text or "" for node in item.iter()
                            if node.tag.endswith("}t") or node.tag == "t"
                        ))

            custom_formats: dict[str, str] = {}
            cell_formats: list[dict[str, str]] = []
            if "xl/styles.xml" in names:
                styles_root = ElementTree.fromstring(archive.read("xl/styles.xml"))
                for item in styles_root.iter():
                    if item.tag.endswith("}numFmt") or item.tag == "numFmt":
                        if item.attrib.get("numFmtId") and item.attrib.get("formatCode"):
                            custom_formats[item.attrib["numFmtId"]] = item.attrib["formatCode"]
                    if item.tag.endswith("}cellXfs") or item.tag == "cellXfs":
                        cell_formats = [dict(node.attrib) for node in item if node.tag.endswith("}xf") or node.tag == "xf"]

            relationships: dict[str, str] = {}
            rels_name = "xl/_rels/workbook.xml.rels"
            if rels_name in names:
                rels_root = ElementTree.fromstring(archive.read(rels_name))
                for relation in rels_root:
                    identifier = relation.attrib.get("Id")
                    target = relation.attrib.get("Target")
                    if not identifier or not target or relation.attrib.get("TargetMode") == "External":
                        continue
                    member = posixpath.normpath(
                        target.lstrip("/") if target.startswith("/")
                        else posixpath.join("xl", target)
                    )
                    if member.startswith("xl/") and ".." not in PurePosixPath(member).parts:
                        relationships[identifier] = member

            workbook_root = ElementTree.fromstring(archive.read("xl/workbook.xml"))
            sheets = [
                item for item in workbook_root.iter()
                if item.tag.endswith("}sheet") or item.tag == "sheet"
            ]
            for sheet_index, sheet in enumerate(sheets, 1):
                title = sheet.attrib.get("name") or f"Sheet{sheet_index}"
                relation_id = attr_by_local_name(sheet, "id")
                member = relationships.get(relation_id or "", f"xl/worksheets/sheet{sheet_index}.xml")
                if member not in names:
                    self.mark_partial(doc, f"worksheet OOXML part unavailable for sheet index {sheet_index}")
                    continue
                sheet_root = ElementTree.fromstring(archive.read(member))
                cells = [
                    item for item in sheet_root.iter()
                    if item.tag.endswith("}c") or item.tag == "c"
                ]
                row_numbers: list[int] = []
                column_numbers: list[int] = []
                for cell in cells:
                    coordinate = cell.attrib.get("r", "")
                    match = re.fullmatch(r"([A-Z]{1,4})([1-9][0-9]*)", coordinate)
                    if not match:
                        continue
                    letters, row_text = match.groups()
                    column = 0
                    for character in letters:
                        column = column * 26 + ord(character) - ord("A") + 1
                    column_numbers.append(column)
                    row_numbers.append(int(row_text))
                sheet_ev = self.add_evidence(
                    doc_id, "worksheet", {"sheet_name": title},
                    content(raw_value={
                        "title": title,
                        "max_row": max(row_numbers, default=0),
                        "max_column": max(column_numbers, default=0),
                    }),
                    ordinal=sheet_index,
                    native_properties={"source_member": member, "state": sheet.attrib.get("state", "visible")},
                )
                self.contain_document(doc_id, sheet_ev["evidence_id"])
                for cell in cells:
                    coordinate = cell.attrib.get("r")
                    if not coordinate or not self.may_add_leaf(doc_id):
                        continue
                    value, data_type, formula, cached, raw_lexeme = cell_value(cell, shared)
                    if value in {None, ""} and formula is None:
                        continue
                    style_id = cell.attrib.get("s", "0")
                    style: dict[str, Any] = {"source_style_id": style_id}
                    try:
                        style_record = cell_formats[int(style_id)]
                    except (ValueError, IndexError):
                        style_record = {}
                    number_format_id = style_record.get("numFmtId")
                    if number_format_id is not None:
                        style["number_format"] = custom_formats.get(number_format_id, f"builtin:{number_format_id}")
                    native_properties: dict[str, Any] = {"data_type": data_type, "source_member": member}
                    if raw_lexeme is not None:
                        native_properties[
                            "cached_raw_lexeme" if formula is not None else "raw_lexeme"
                        ] = raw_lexeme
                    if cached is not None:
                        native_properties["cached_value"] = cached
                    if formula is not None:
                        native_properties.update({
                            "cached_value_available": cached is not None,
                            "cached_value_status": FORMULA_CACHED_VALUE_STATUS,
                        })
                    cell_ev = self.add_evidence(
                        doc_id, "table_cell", {"sheet_name": title, "cell": coordinate},
                        content(raw_value=value), parent_id=sheet_ev["evidence_id"],
                        style=style, native_properties=native_properties,
                    )
                    if formula is not None:
                        self.add_evidence(
                            doc_id, "formula", {"sheet_name": title, "cell": coordinate},
                            content(raw_text="=" + formula), parent_id=cell_ev["evidence_id"],
                            native_properties={
                                "cached_value": cached,
                                "cached_value_available": cached is not None,
                                "cached_value_status": FORMULA_CACHED_VALUE_STATUS,
                                **({"cached_raw_lexeme": raw_lexeme} if raw_lexeme is not None else {}),
                            },
                        )
                for merged_index, merged in enumerate(
                    (item for item in sheet_root.iter() if item.tag.endswith("}mergeCell") or item.tag == "mergeCell"),
                    1,
                ):
                    reference = merged.attrib.get("ref")
                    if reference:
                        self.add_evidence(
                            doc_id, "merged_range", {"sheet_name": title, "range": reference},
                            content(raw_text=reference), parent_id=sheet_ev["evidence_id"], ordinal=merged_index,
                        )
                auto_filter = next(
                    (item for item in sheet_root.iter() if item.tag.endswith("}autoFilter") or item.tag == "autoFilter"),
                    None,
                )
                if auto_filter is not None and auto_filter.attrib.get("ref"):
                    self.add_evidence(
                        doc_id, "filter", {"sheet_name": title, "range": auto_filter.attrib["ref"]},
                        content(raw_value={
                            "ref": auto_filter.attrib["ref"],
                            "filter_columns": [dict(item.attrib) for item in auto_filter],
                        }),
                        parent_id=sheet_ev["evidence_id"],
                    )
                for validation_index, validation in enumerate(
                    (item for item in sheet_root.iter() if item.tag.endswith("}dataValidation") or item.tag == "dataValidation"),
                    1,
                ):
                    formula1 = child(validation, "formula1")
                    formula2 = child(validation, "formula2")
                    reference = validation.attrib.get("sqref", "unknown")
                    self.add_evidence(
                        doc_id, "data_validation",
                        {"sheet_name": title, "object_index": validation_index, "range": reference},
                        content(raw_value={
                            "type": validation.attrib.get("type"),
                            "formula1": formula1.text if formula1 is not None else None,
                            "formula2": formula2.text if formula2 is not None else None,
                            "operator": validation.attrib.get("operator"),
                        }),
                        parent_id=sheet_ev["evidence_id"], ordinal=validation_index,
                    )

            defined_names = [
                item for item in workbook_root.iter()
                if item.tag.endswith("}definedName") or item.tag == "definedName"
            ]
            for name_index, defined_name in enumerate(defined_names, 1):
                name = defined_name.attrib.get("name") or str(name_index)
                ev = self.add_evidence(
                    doc_id, "defined_name", {"object_index": name_index, "object_id": name},
                    content(raw_value={
                        "name": name,
                        "attr_text": defined_name.text,
                        "local_sheet_id": defined_name.attrib.get("localSheetId"),
                    }), ordinal=name_index,
                )
                self.contain_document(doc_id, ev["evidence_id"])

            chart_members = sorted(name for name in names if name.startswith("xl/charts/") and name.endswith(".xml"))
            for chart_index, member in enumerate(chart_members, 1):
                raw = archive.read(member)
                try:
                    chart_root = ElementTree.fromstring(raw)
                    formulas = [node.text.strip() for node in chart_root.iter() if node.tag.endswith("}f") and node.text]
                    labels = [node.text.strip() for node in chart_root.iter() if node.tag.endswith("}v") and node.text]
                except ElementTree.ParseError:
                    formulas, labels = [], []
                chart_ev = self.add_evidence(
                    doc_id, "chart", {"source_member": member, "object_index": chart_index},
                    content(raw_value={
                        "source_member": member, "xml_sha256": digest_bytes(raw),
                        "formulas": formulas, "cached_labels": labels,
                    }), ordinal=chart_index,
                    native_properties={"ooxml_part": member, "extended_chart": "/chartEx" in member},
                )
                self.contain_document(doc_id, chart_ev["evidence_id"])
                for series_index, formula in enumerate(formulas, 1):
                    self.add_evidence(
                        doc_id, "chart_series",
                        {"source_member": member, "object_index": chart_index, "series_index": series_index},
                        content(raw_text=formula), parent_id=chart_ev["evidence_id"], ordinal=series_index,
                    )
            media_members = sorted(name for name in names if name.startswith("xl/media/") and not name.endswith("/"))
            for image_index, member in enumerate(media_members, 1):
                raw = archive.read(member)
                ev = self.add_evidence(
                    doc_id, "image", {"source_member": member, "object_index": image_index},
                    content(content_ref=f"{doc['source']['relative_path']}::{member}", mime_type=mimetypes.guess_type(member)[0]),
                    ordinal=image_index,
                    native_properties={"embedded_sha256": digest_bytes(raw), "size_bytes": len(raw)},
                )
                self.contain_document(doc_id, ev["evidence_id"])

    def extract_pptx(self, path: Path) -> None:
        from pptx import Presentation

        source, decrypted = self.office_source(path)
        validate_ooxml_archive(
            source,
            required_members=PPTX_REQUIRED_OOXML_MEMBERS,
        )
        presentation = Presentation(source)
        doc = self.add_document(path, "python-pptx")
        doc_id = doc["document_id"]
        if decrypted:
            doc["extraction"]["warnings"].append("password-protected Office source decrypted in memory")

        def walk_shapes(shapes: Any, prefix: str = "") -> Any:
            for local_index, candidate in enumerate(shapes, 1):
                key = f"{prefix}.{local_index}" if prefix else str(local_index)
                yield key, candidate
                nested = getattr(candidate, "shapes", None)
                if nested is not None:
                    yield from walk_shapes(nested, key)

        for slide_number, slide in enumerate(presentation.slides, 1):
            slide_ev = self.add_evidence(
                doc_id, "slide", {"slide_number": slide_number},
                content(raw_value={"slide_number": slide_number, "shape_count": len(slide.shapes)}),
                ordinal=slide_number,
            )
            self.contain_document(doc_id, slide_ev["evidence_id"])
            for shape_index, (shape_path, shape) in enumerate(walk_shapes(slide.shapes), 1):
                if not self.may_add_leaf(doc_id):
                    break
                shape_locator_id = str(shape.shape_id) if "." not in shape_path else f"{shape.shape_id}:{shape_path}"
                text_value = getattr(shape, "text", "")
                shape_content = content(raw_text=text_value) if text_value else content(
                    raw_value={"shape_type": str(shape.shape_type), "name": shape.name}
                )
                geometry = {
                    "coordinate_space": "slide", "unit": "emu",
                    "x": shape.left, "y": shape.top, "width": shape.width, "height": shape.height,
                }
                shape_ev = self.add_evidence(
                    doc_id, "shape",
                    {"slide_number": slide_number, "shape_id": shape_locator_id, "object_index": shape_index},
                    shape_content, parent_id=slide_ev["evidence_id"], ordinal=shape_index,
                    geometry=geometry,
                    native_properties={"name": shape.name, "shape_type": str(shape.shape_type), "shape_path": shape_path},
                )
                if getattr(shape, "has_table", False):
                    table_ev = self.add_evidence(
                        doc_id, "table",
                        {"slide_number": slide_number, "shape_id": shape_locator_id},
                        content(raw_value={"rows": len(shape.table.rows), "columns": len(shape.table.columns)}),
                        parent_id=shape_ev["evidence_id"],
                    )
                    for row_index, row in enumerate(shape.table.rows, 1):
                        for column_index, cell_obj in enumerate(row.cells, 1):
                            if not self.may_add_leaf(doc_id):
                                break
                            self.add_evidence(
                                doc_id, "table_cell",
                                {"slide_number": slide_number, "shape_id": shape_locator_id,
                                 "row_index": row_index, "column_index": column_index},
                                content(raw_text=cell_obj.text), parent_id=table_ev["evidence_id"],
                                ordinal=column_index,
                            )
                        if self.limit_reached(doc_id):
                            break
                if getattr(shape, "has_chart", False):
                    chart_ev = self.add_evidence(
                        doc_id, "chart",
                        {"slide_number": slide_number, "shape_id": shape_locator_id},
                        content(raw_value={"series_count": len(shape.chart.series)}),
                        parent_id=shape_ev["evidence_id"],
                    )
                    for series_index, series in enumerate(shape.chart.series, 1):
                        self.add_evidence(
                            doc_id, "chart_series",
                            {"slide_number": slide_number, "shape_id": shape_locator_id, "series_index": series_index},
                            content(raw_value={"name": getattr(series, "name", None)}),
                            parent_id=chart_ev["evidence_id"], ordinal=series_index,
                        )
                image = getattr(shape, "image", None)
                if image is not None:
                    blob = image.blob
                    self.add_evidence(
                        doc_id,
                        "image",
                        {"slide_number": slide_number, "shape_id": shape_locator_id},
                        content(
                            content_ref=f"{doc['source']['relative_path']}#slide={slide_number};shape={shape.shape_id}",
                            mime_type=getattr(image, "content_type", None),
                        ),
                        parent_id=shape_ev["evidence_id"],
                        native_properties={
                            "embedded_sha256": digest_bytes(blob),
                            "size_bytes": len(blob),
                            "file_name": getattr(image, "filename", None),
                        },
                    )
            if getattr(slide, "has_notes_slide", False):
                notes_frame = getattr(slide.notes_slide, "notes_text_frame", None)
                notes_text = getattr(notes_frame, "text", "") if notes_frame is not None else ""
                if notes_text.strip() and self.may_add_leaf(doc_id):
                    self.add_evidence(
                        doc_id,
                        "speaker_note",
                        {"slide_number": slide_number, "locator_text": "speaker-notes"},
                        content(raw_text=notes_text),
                        parent_id=slide_ev["evidence_id"],
                    )

    def extract_pdf(self, path: Path) -> None:
        from pypdf import PdfReader

        reader = PdfReader(path)
        doc = self.add_document(path, "pypdf")
        doc_id = doc["document_id"]
        if not self.diagnostic:
            doc["extraction"]["warnings"].append(
                "PDF native text is preserved per page in reader order; image regions are routed to later layers"
            )
        pages_without_text = 0
        for page_number, page in enumerate(reader.pages, 1):
            media_box = page.mediabox
            page_width = float(media_box.width)
            page_height = float(media_box.height)
            blocks: list[dict[str, Any]] = []

            def observe_text(
                text: str,
                _cm: list[float],
                tm: list[float],
                _font: dict[str, Any] | None,
                font_size: float,
            ) -> None:
                value = " ".join(text.split())
                if not value:
                    return
                size = max(float(font_size or 0), 1.0)
                x = max(0.0, min(float(tm[4]), page_width))
                baseline = max(0.0, min(float(tm[5]), page_height))
                height = min(size * 1.25, page_height)
                top = max(0.0, min(page_height - baseline - height, page_height))
                width = min(max(size * 0.48 * len(value), 1.0), max(page_width - x, 1.0))
                blocks.append({
                    "text": value,
                    "geometry": {
                        "coordinate_space": "page",
                        "coordinate_origin": "top_left",
                        "unit": "pt",
                        "x": x,
                        "y": top,
                        "width": width,
                        "height": height,
                    },
                    "font_size_pt": size,
                })

            text_value = page.extract_text(visitor_text=observe_text) or ""
            geometry = {
                "coordinate_space": "page", "unit": "pt",
                "coordinate_origin": "top_left",
                "x": 0, "y": 0, "width": page_width, "height": page_height,
            }
            if text_value.strip():
                item_content = content(raw_text=text_value)
                warning = None
            else:
                pages_without_text += 1
                item_content = content(content_ref=f"{doc['source']['relative_path']}#page={page_number}", mime_type="application/pdf")
                warning = "no text layer; OCR deferred"
            page_ev = self.add_evidence(
                doc_id, "page", {"page_number": page_number}, item_content,
                ordinal=page_number, geometry=geometry,
                native_properties={"text_layer_present": bool(text_value.strip())},
                warning=warning,
            )
            self.contain_document(doc_id, page_ev["evidence_id"])
            for block_index, block in enumerate(blocks, 1):
                if not self.may_add_leaf(doc_id):
                    break
                self.add_evidence(
                    doc_id,
                    "text_block",
                    {"page_number": page_number, "object_index": block_index},
                    content(raw_text=block["text"]),
                    parent_id=page_ev["evidence_id"],
                    ordinal=block_index,
                    geometry=block["geometry"],
                    native_properties={"font_size_pt": block["font_size_pt"], "source": "pdf_text_operator"},
                )
        if pages_without_text:
            self.mark_partial(doc, f"OCR deferred for {pages_without_text} page(s) without a text layer")

    def extract_image(self, path: Path) -> None:
        """Preserve every located reading and distinguish its support tier."""
        doc = self.add_document(path, "adaptive-local-image-reader-v0.5")
        doc_id = doc["document_id"]
        try:
            from local_image_ocr import (
                MAX_UNLOCATED_TRANSCRIPT_TOKENS,
                UNLOCATED_TRANSCRIPT_MODEL,
                UNLOCATED_TRANSCRIPT_PROMPT_SHA256,
                extract,
            )
            observation = extract(path)
        except Exception as exc:
            self.mark_partial(doc, f"local image OCR unavailable: {type(exc).__name__}: {exc}")
            image_ev = self.add_evidence(
                doc_id,
                "image",
                {"object_index": 1},
                content(content_ref=doc["source"]["relative_path"], mime_type=doc["source"]["media_type"]),
                ordinal=1,
                native_properties={"source_sha256": doc["source"]["sha256"]},
                method="verified_image_bytes",
            )
            self.contain_document(doc_id, image_ev["evidence_id"])
            return

        width = observation["dimensions"]["width_px"]
        height = observation["dimensions"]["height_px"]
        image_ev = self.add_evidence(
            doc_id,
            "image",
            {"object_index": 1},
            content(content_ref=doc["source"]["relative_path"], mime_type=doc["source"]["media_type"]),
            ordinal=1,
            geometry={
                "coordinate_space": "image",
                "coordinate_origin": "top_left",
                "unit": "px",
                "x": 0,
                "y": 0,
                "width": width,
                "height": height,
            },
            native_properties={
                "source_sha256": observation["input_sha256"],
                "image_format": observation["image_format"],
                "orientation": observation["orientation"],
                "orientation_source": observation["orientation_source"],
                "source_dimensions": observation["source_dimensions"],
                "ocr_input_sha256": observation["ocr_input_sha256"],
                "ocr_input_dimensions": observation["ocr_input_dimensions"],
                "ocr_input_orientation": observation["ocr_input_orientation"],
                "coordinate_frame_policy": observation["coordinate_frame_policy"],
                "canonicalization": observation["canonicalization"],
                "ocr_engines": observation["engines"],
                "independent_ocr_engines": observation["independent_engines"],
                "unresolved_ocr_line_count": observation["unresolved_count"],
            },
            method="verified_image_bytes",
        )
        self.contain_document(doc_id, image_ev["evidence_id"])
        read_lines = observation.get("read_lines", observation["consensus_lines"])
        for line_index, line in enumerate(read_lines, 1):
            bbox = line["bbox"]
            confidence_values = [
                value for value in (line["primary_confidence"], line["audit_confidence"])
                if value is not None
            ]
            agreement_type = line.get("agreement_type", "independent_agreement")
            expected_quality_tier = OCR_QUALITY_BY_AGREEMENT.get(agreement_type)
            if expected_quality_tier is None:
                raise ValueError(f"unsupported OCR agreement type: {agreement_type!r}")
            quality_tier = line.get("quality_tier", expected_quality_tier)
            if quality_tier != expected_quality_tier:
                raise ValueError(
                    "OCR quality tier disagrees with engine independence: "
                    f"{agreement_type!r} cannot be {quality_tier!r}"
                )
            upstream_marker = line.get("provisional_marker")
            if quality_tier == "provisional":
                if upstream_marker != PROVISIONAL_OCR_MARKER:
                    raise ValueError("provisional OCR marker is missing or not canonical")
            elif upstream_marker is not None:
                raise ValueError("high OCR evidence must not carry a provisional marker")
            provenance = line.get("provenance")
            if not isinstance(provenance, dict):
                raise ValueError("OCR observation provenance is missing")
            primary_pass = provenance.get("primary_pass")
            primary_engine = ocr_engine(primary_pass)
            primary_group = ocr_independence_group(primary_pass)
            if provenance.get("primary_engine") != primary_engine:
                raise ValueError("OCR primary engine is invalid")
            if provenance.get("primary_independence_group") != primary_group:
                raise ValueError("OCR primary independence group is invalid")
            audit_pass = provenance.get("audit_pass")
            audit_engine = None
            audit_group = None
            if audit_pass is not None:
                audit_engine = ocr_engine(audit_pass)
                audit_group = ocr_independence_group(audit_pass)
                if provenance.get("audit_engine") != audit_engine:
                    raise ValueError("OCR audit engine is invalid")
                if provenance.get("audit_independence_group") != audit_group:
                    raise ValueError("OCR audit independence group is invalid")
            if agreement_type == "independent_agreement" and (
                audit_group is None or primary_group == audit_group
            ):
                raise ValueError(
                    "high OCR evidence requires distinct line-level engine groups"
                )
            if agreement_type == "same_engine_agreement" and (
                audit_group is None or primary_group != audit_group
            ):
                raise ValueError(
                    "same-engine OCR evidence requires one line-level engine group"
                )
            if agreement_type == "provisional_single_pass" and audit_group is not None:
                raise ValueError("single-pass OCR evidence must not claim an audit pass")
            validate_ocr_supporters(
                line,
                provenance,
                primary_pass=primary_pass,
                primary_engine=primary_engine,
                primary_group=primary_group,
                audit_pass=audit_pass,
                audit_engine=audit_engine,
                audit_group=audit_group,
                agreement_type=agreement_type,
            )
            if quality_tier == "high" and observation["independent_engines"] is not True:
                raise ValueError("high OCR evidence requires independent engine groups")
            bbox_coordinate_system = line.get("bbox_coordinate_system")
            if bbox_coordinate_system not in OCR_BBOX_COORDINATE_SYSTEMS:
                raise ValueError("OCR bbox coordinate system is missing or unsupported")
            if (
                quality_tier == "high"
                and bbox_coordinate_system
                != "source_orientation_1_top_left_normalized_1000"
            ):
                raise ValueError(
                    "high OCR evidence requires a shared source-orientation-1 frame"
                )
            self.add_evidence(
                doc_id,
                "ocr_line",
                {"object_index": line_index},
                content(raw_text=line["text"]),
                parent_id=image_ev["evidence_id"],
                ordinal=line_index,
                geometry={
                    "coordinate_space": "image",
                    "coordinate_origin": "top_left",
                    "unit": "normalized_1000",
                    "x": bbox[0],
                    "y": bbox[1],
                    "width": bbox[2],
                    "height": bbox[3],
                },
                native_properties={
                    "consensus_method": "spatial-nfc-exact-or-provisional",
                    "agreement_type": agreement_type,
                    "quality_tier": quality_tier,
                    "bbox_coordinate_system": bbox_coordinate_system,
                    **(
                        {"provisional_marker": PROVISIONAL_OCR_MARKER}
                        if quality_tier == "provisional" else {}
                    ),
                    "spatial_overlap": line["overlap"],
                    "primary_confidence": line["primary_confidence"],
                    "audit_confidence": line["audit_confidence"],
                    "independent_engines": observation["independent_engines"],
                    "observation_provenance": provenance,
                },
                method=(
                    "dual_local_ocr_consensus"
                    if quality_tier == "high"
                    else "adaptive_local_ocr_provisional"
                ),
                confidence=min(confidence_values) if confidence_values else 0.0,
                deterministic=False,
            )
        unlocated = observation.get("unlocated_transcript")
        if unlocated is not None:
            if not isinstance(unlocated, dict):
                raise ValueError("unlocated transcript must be an object")
            transcript = unlocated.get("text")
            model_digest = unlocated.get("model_digest")
            prompt_sha256 = unlocated.get("prompt_sha256")
            normalized_model_digest = (
                model_digest.removeprefix("sha256:")
                if isinstance(model_digest, str) else ""
            )
            if not isinstance(transcript, str) or not transcript.strip():
                raise ValueError("unlocated transcript text is missing")
            if (
                unlocated.get("location_status") != "unlocated"
                or unlocated.get("quality_tier") != "provisional"
                or unlocated.get("provisional_marker") != PROVISIONAL_OCR_MARKER
                or unlocated.get("question_independent") is not True
                or unlocated.get("transcript_type")
                != "whole_image_faithful_transcript"
                or unlocated.get("model") != UNLOCATED_TRANSCRIPT_MODEL
                or unlocated.get("runner") != "ollama_loopback_chat"
                or unlocated.get("host") != "127.0.0.1"
                or not isinstance(model_digest, str)
                or not re.fullmatch(r"[0-9a-f]{64}", normalized_model_digest)
                or prompt_sha256 != UNLOCATED_TRANSCRIPT_PROMPT_SHA256
                or unlocated.get("temperature") != 0
                or unlocated.get("num_predict")
                != MAX_UNLOCATED_TRANSCRIPT_TOKENS
            ):
                raise ValueError("unlocated transcript provenance is invalid")
            transcript_value = transcript.strip()
            marker_prefix = f"{PROVISIONAL_OCR_MARKER}\n"
            transcript_chunks = exact_text_chunks(
                transcript_value,
                max_chars=MAX_QUESTION_EVIDENCE_CHARS - len(marker_prefix),
            )
            transcript_sha256 = digest_bytes(transcript_value.encode("utf-8"))
            base_ordinal = len(read_lines)
            for chunk_index, question_chunk in enumerate(transcript_chunks, 1):
                self.add_evidence(
                    doc_id,
                    "text_block",
                    {
                        "object_index": base_ordinal + chunk_index,
                        "locator_text": (
                            "location_status=unlocated;source=image;"
                            f"chunk={chunk_index}/{len(transcript_chunks)};"
                            f"characters={question_chunk.start + 1}-{question_chunk.end}"
                        ),
                    },
                    content(raw_text=marker_prefix + question_chunk.text),
                    parent_id=image_ev["evidence_id"],
                    ordinal=base_ordinal + chunk_index,
                    native_properties={
                        "location_status": "unlocated",
                        "quality_tier": "provisional",
                        "provisional_marker": PROVISIONAL_OCR_MARKER,
                        "transcript_type": unlocated.get("transcript_type"),
                        "question_independent": True,
                        "model": unlocated.get("model"),
                        "model_digest": model_digest,
                        "prompt_sha256": prompt_sha256,
                        "runner": unlocated.get("runner"),
                        "host": "127.0.0.1",
                        "temperature": unlocated.get("temperature"),
                        "num_predict": unlocated.get("num_predict"),
                        "transcript_sha256": transcript_sha256,
                        "transcript_chunk_index": chunk_index,
                        "transcript_chunk_count": len(transcript_chunks),
                        "character_start": question_chunk.start,
                        "character_end": question_chunk.end,
                        "character_offset_basis": "zero_based_half_open",
                    },
                    method="local_vlm_unlocated_transcript_provisional",
                    confidence=0.0,
                    deterministic=False,
                    warning=(
                        "whole-image transcript has no coordinates and is provisional only"
                    ),
                )
            self.mark_partial(
                doc,
                "unlocated whole-image transcript retained as provisional Evidence",
            )
        if not read_lines:
            self.mark_partial(doc, "adaptive local OCR produced no located text observations")
        if observation["unresolved_count"]:
            self.mark_partial(
                doc,
                f"{observation['unresolved_count']} OCR reading(s) remain provisional but are retained",
            )

    def extract_delimited(self, path: Path) -> None:
        text_value, encoding = read_text(path)
        delimiter = "\t" if path.suffix.lower() == ".tsv" else ","
        reader = csv.reader(io.StringIO(text_value, newline=""), delimiter=delimiter)
        doc = self.add_document(path, "python-csv")
        doc_id = doc["document_id"]
        try:
            raw_headers = next(reader)
        except StopIteration:
            self.mark_partial(doc, "delimited file is empty")
            return
        headers = [value.strip() or f"column_{index}" for index, value in enumerate(raw_headers, 1)]
        table_ev = self.add_evidence(
            doc_id,
            "table",
            {"table_index": 1, "locator_text": "delimited-table"},
            content(raw_value={"delimiter": delimiter, "headers": raw_headers}),
            ordinal=1,
            native_properties={"encoding": encoding, "column_count": len(headers)},
        )
        self.contain_document(doc_id, table_ev["evidence_id"])
        for source_row_number, row in enumerate(reader, 2):
            if not any(value != "" for value in row):
                continue
            if not self.may_add_leaf(doc_id):
                break
            values = list(row)
            if len(values) < len(headers):
                values.extend([""] * (len(headers) - len(values)))
            rendered = " / ".join(
                f"{headers[index] if index < len(headers) else f'column_{index + 1}'}:{value}"
                for index, value in enumerate(values)
            )
            self.add_evidence(
                doc_id,
                "table_row",
                {"table_index": 1, "row_index": source_row_number, "locator_text": f"row={source_row_number}"},
                content(raw_text=rendered),
                parent_id=table_ev["evidence_id"],
                ordinal=source_row_number,
                native_properties={"headers": headers, "values": values, "source_row_number": source_row_number},
            )
        if encoding == "utf-8-replacement":
            self.mark_partial(doc, "input required replacement characters during decoding")

    def extract_json(self, path: Path) -> None:
        text_value, encoding = read_text(path)
        parsed = json.loads(text_value)
        doc = self.add_document(path, "python-json")
        doc_id = doc["document_id"]

        def walk(value: Any, pointer: str, parent_id: str | None, ordinal: int) -> None:
            if isinstance(value, (dict, list)):
                container = self.add_evidence(
                    doc_id,
                    "metadata",
                    {"locator_text": pointer or "/"},
                    content(raw_value={
                        "container_type": "object" if isinstance(value, dict) else "array",
                        "item_count": len(value),
                    }),
                    parent_id=parent_id,
                    ordinal=ordinal,
                    native_properties={"json_pointer": pointer or "/"},
                )
                if parent_id is None:
                    self.contain_document(doc_id, container["evidence_id"])
                items = value.items() if isinstance(value, dict) else enumerate(value)
                for child_ordinal, (key, child) in enumerate(items, 1):
                    token = str(key).replace("~", "~0").replace("/", "~1")
                    walk(child, f"{pointer}/{token}", container["evidence_id"], child_ordinal)
                return
            if not self.may_add_leaf(doc_id):
                return
            item_content = content(raw_text=value) if isinstance(value, str) else content(raw_value=value)
            ev = self.add_evidence(
                doc_id,
                "field",
                {"locator_text": pointer or "/"},
                item_content,
                parent_id=parent_id,
                ordinal=ordinal,
                native_properties={"json_pointer": pointer or "/", "encoding": encoding},
            )
            if parent_id is None:
                self.contain_document(doc_id, ev["evidence_id"])

        walk(parsed, "", None, 1)

    def extract_xml(self, path: Path) -> None:
        text_value, encoding = read_text(path)
        root = ElementTree.fromstring(text_value)
        doc = self.add_document(path, "xml.etree.ElementTree")
        doc_id = doc["document_id"]

        def walk(element: ElementTree.Element, parent_path: str, parent_id: str | None, ordinal: int) -> None:
            element_path = f"{parent_path}/{element.tag}[{ordinal}]"
            container = self.add_evidence(
                doc_id,
                "metadata",
                {"locator_text": element_path},
                content(raw_value={"tag": element.tag, "attribute_count": len(element.attrib), "child_count": len(element)}),
                parent_id=parent_id,
                ordinal=ordinal,
                native_properties={"xml_path": element_path, "encoding": encoding},
            )
            if parent_id is None:
                self.contain_document(doc_id, container["evidence_id"])
            for attr_index, (name, value) in enumerate(sorted(element.attrib.items()), 1):
                if self.may_add_leaf(doc_id):
                    self.add_evidence(
                        doc_id,
                        "field",
                        {"locator_text": f"{element_path}/@{name}"},
                        content(raw_text=value),
                        parent_id=container["evidence_id"],
                        ordinal=attr_index,
                        native_properties={"xml_path": f"{element_path}/@{name}"},
                    )
            if element.text and element.text.strip() and self.may_add_leaf(doc_id):
                self.add_evidence(
                    doc_id,
                    "field",
                    {"locator_text": f"{element_path}/text()"},
                    content(raw_text=element.text),
                    parent_id=container["evidence_id"],
                    native_properties={"xml_path": f"{element_path}/text()"},
                )
            for child_index, child in enumerate(list(element), 1):
                walk(child, element_path, container["evidence_id"], child_index)

        walk(root, "", None, 1)

    def extract_plain_text(self, path: Path) -> None:
        text_value, encoding = read_text(path)
        parser = "plain-code" if path.suffix.lower() in CODE_SUFFIXES else "plain-text"
        doc = self.add_document(path, parser)
        doc_id = doc["document_id"]
        if not text_value:
            self.mark_partial(doc, "text file is empty")
            return
        if path.suffix.lower() in CODE_SUFFIXES:
            lines = text_value.splitlines(keepends=True)
            for block_index, start in enumerate(range(0, len(lines), 80), 1):
                if not self.may_add_leaf(doc_id):
                    break
                block = "".join(lines[start:start + 80])
                ev = self.add_evidence(
                    doc_id,
                    "code_block",
                    {"code_line_start": start + 1, "code_line_end": min(start + 80, len(lines)),
                     "locator_text": f"lines={start + 1}-{min(start + 80, len(lines))}"},
                    content(raw_text=block),
                    ordinal=block_index,
                    native_properties={"encoding": encoding, "language": path.suffix.lower().lstrip(".")},
                )
                self.contain_document(doc_id, ev["evidence_id"])
            return

        blocks = re.split(r"\n[\t ]*\n+", text_value)
        paragraph_index = 0
        for block in blocks:
            if not block:
                continue
            if not self.may_add_leaf(doc_id):
                break
            paragraph_index += 1
            stripped = block.strip()
            evidence_type = "heading" if path.suffix.lower() == ".md" and re.match(r"^#{1,6}\s+", stripped) else "paragraph"
            ev = self.add_evidence(
                doc_id,
                evidence_type,
                {"paragraph_index": paragraph_index},
                content(raw_text=block),
                ordinal=paragraph_index,
                native_properties={"encoding": encoding},
            )
            self.contain_document(doc_id, ev["evidence_id"])
        if encoding == "utf-8-replacement":
            self.mark_partial(doc, "input required replacement characters during decoding")

    def extract_notebook(self, path: Path) -> None:
        text_value, encoding = read_text(path)
        notebook = json.loads(text_value)
        doc = self.add_document(path, "nbformat-json")
        doc_id = doc["document_id"]
        image_count = 0

        def strip_data_uri(source_text: str, cell_index: int) -> str:
            nonlocal image_count

            def replace(match: re.Match[str]) -> str:
                nonlocal image_count
                image_count += 1
                try:
                    blob = base64.b64decode(re.sub(r"\s", "", match.group(2)), validate=False)
                    blob_sha = digest_bytes(blob)
                    size_bytes = len(blob)
                except Exception:
                    blob_sha = digest_bytes(match.group(2).encode("ascii", errors="ignore"))
                    size_bytes = 0
                ev = self.add_evidence(
                    doc_id,
                    "image",
                    {"notebook_cell_index": cell_index, "object_index": image_count,
                     "locator_text": f"cell={cell_index};embedded-image={image_count}"},
                    content(content_ref=f"{doc['source']['relative_path']}#cell={cell_index};image={image_count}",
                            mime_type=match.group(1)),
                    ordinal=image_count,
                    native_properties={"embedded_sha256": blob_sha, "size_bytes": size_bytes},
                )
                self.contain_document(doc_id, ev["evidence_id"])
                return f"[embedded image sha256={blob_sha}]"

            return DATA_URI_PATTERN.sub(replace, source_text)

        for cell_index, cell in enumerate(notebook.get("cells", []), 1):
            source_text = "".join(cell.get("source", []))
            source_text = strip_data_uri(source_text, cell_index)
            if source_text and self.may_add_leaf(doc_id):
                ev = self.add_evidence(
                    doc_id,
                    "notebook_cell",
                    {"notebook_cell_index": cell_index, "locator_text": f"cell={cell_index}"},
                    content(raw_text=source_text),
                    ordinal=cell_index,
                    native_properties={"cell_type": cell.get("cell_type"), "encoding": encoding},
                )
                self.contain_document(doc_id, ev["evidence_id"])
            output_index = 0
            for output in cell.get("outputs", []):
                output_index += 1
                data = output.get("data", {})
                text_output = output.get("text", "")
                if isinstance(text_output, list):
                    text_output = "".join(text_output)
                if not text_output:
                    plain = data.get("text/plain", "")
                    text_output = "".join(plain) if isinstance(plain, list) else plain
                if text_output and self.may_add_leaf(doc_id):
                    ev = self.add_evidence(
                        doc_id,
                        "text_block",
                        {"notebook_cell_index": cell_index, "object_index": output_index,
                         "locator_text": f"cell={cell_index};output={output_index}"},
                        content(raw_text=str(text_output)),
                        ordinal=output_index,
                        native_properties={"output_type": output.get("output_type")},
                    )
                    self.contain_document(doc_id, ev["evidence_id"])
                for mime_type, payload in data.items():
                    if not mime_type.startswith("image/"):
                        continue
                    image_count += 1
                    encoded = payload if isinstance(payload, str) else "".join(payload)
                    try:
                        blob = base64.b64decode(encoded, validate=False)
                        blob_sha = digest_bytes(blob)
                        size_bytes = len(blob)
                    except Exception:
                        blob_sha = digest_bytes(encoded.encode("ascii", errors="ignore"))
                        size_bytes = 0
                    ev = self.add_evidence(
                        doc_id,
                        "image",
                        {"notebook_cell_index": cell_index, "object_index": image_count,
                         "locator_text": f"cell={cell_index};output-image={image_count}"},
                        content(content_ref=f"{doc['source']['relative_path']}#cell={cell_index};image={image_count}",
                                mime_type=mime_type),
                        ordinal=image_count,
                        native_properties={"embedded_sha256": blob_sha, "size_bytes": size_bytes},
                    )
                    self.contain_document(doc_id, ev["evidence_id"])
        if image_count:
            doc["extraction"]["warnings"].append(
                f"{image_count} embedded image(s) recorded for graph/OCR processing without image interpretation"
            )

    def extract_other(self, path: Path) -> None:
        doc = self.add_document(path, "metadata-only")
        doc["extraction"]["status"] = "deferred"
        warning = "unsupported format; content extraction deferred"
        if warning not in doc["extraction"]["warnings"]:
            doc["extraction"]["warnings"].append(warning)
        ev = self.add_evidence(
            doc["document_id"], "metadata", {"locator_text": "file"},
            content(content_ref=doc["source"]["relative_path"], mime_type=doc["source"]["media_type"]),
            warning="unsupported format; content extraction deferred",
        )
        self.contain_document(doc["document_id"], ev["evidence_id"])

    def write(self, output: Path) -> None:
        if not self.retain_records:
            raise RuntimeError("write() is unavailable when retain_records is false")
        output.mkdir(parents=True, exist_ok=True)
        for name, records in (
            ("documents.jsonl", self.documents),
            ("evidence.jsonl", self.evidence),
            ("relations.jsonl", self.relations),
        ):
            with (output / name).open("w", encoding="utf-8", newline="\n") as handle:
                for record in sorted(records, key=lambda item: next(
                    item[key] for key in ("document_id", "evidence_id", "relation_id") if key in item
                )):
                    handle.write(canonical_json(record) + "\n")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path, help="root used for normalized relative paths")
    parser.add_argument("--input", required=True, type=Path, nargs="+", help="files to probe")
    parser.add_argument("--out", required=True, type=Path, help="isolated diagnostic output directory")
    parser.add_argument("--max-items", type=int, default=40, help="leaf sample limit per document")
    parser.add_argument(
        "--run-at", default=datetime.now(timezone.utc).isoformat(),
        help="ISO-8601 extraction time; pass the same value to compare byte-for-byte reruns",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.max_items < 1:
        raise SystemExit("--max-items must be at least 1")
    probe = Probe(
        args.root,
        args.run_at,
        args.max_items,
        password_candidates=discover_password_candidates(args.root.resolve()),
    )
    for path in args.input:
        if not path.is_file():
            raise SystemExit(f"input is not a file: {path}")
        probe.extract(path)
    probe.write(args.out)
    print(canonical_json({
        "documents": len(probe.documents),
        "evidence": len(probe.evidence),
        "relations": len(probe.relations),
        "output": str(args.out.resolve()),
    }))


if __name__ == "__main__":
    main()
