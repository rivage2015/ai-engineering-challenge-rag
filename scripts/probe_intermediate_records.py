#!/usr/bin/env python3
"""Generate a small, question-independent intermediate-record sample.

This is a diagnostic probe, not the production answer pipeline.  It accepts
arbitrary input paths and applies the same suffix-based dispatch to every file.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import unicodedata
import zipfile
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable


SCHEMA_VERSION = "0.1"
EXTRACTOR = "intermediate-record-probe"
EXTRACTOR_VERSION = "0.1.0"


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


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
        payload: dict[str, Any] = {"raw_text": raw_text}
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
        payload = {"raw_value": clean}
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
    ) -> None:
        self.root = root.resolve()
        self.run_at = run_at
        self.max_items = max_items
        self.diagnostic = diagnostic
        self.extractor = extractor
        self.extractor_version = extractor_version
        self.record_sink = record_sink
        self.retain_records = retain_records
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
                "deterministic": True,
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
        if suffix == ".docx":
            self.extract_docx(path)
        elif suffix == ".xlsx":
            self.extract_xlsx(path)
        elif suffix == ".pptx":
            self.extract_pptx(path)
        elif suffix == ".pdf":
            self.extract_pdf(path)
        else:
            self.extract_other(path)
        self.finalize_document()

    def extract_docx(self, path: Path) -> None:
        from docx import Document

        parsed = Document(path)
        doc = self.add_document(path, "python-docx")
        doc_id = doc["document_id"]
        if not self.diagnostic:
            self.mark_partial(doc, "headers, footers, comments, and run-level style spans are not yet fully extracted")
        for index, paragraph in enumerate(parsed.paragraphs, 1):
            if not paragraph.text or not self.may_add_leaf(doc_id):
                continue
            style: dict[str, Any] = {}
            if paragraph.style and paragraph.style.style_id:
                style["source_style_id"] = paragraph.style.style_id
            evidence_type = "heading" if paragraph.style and paragraph.style.name.lower().startswith("heading") else "paragraph"
            ev = self.add_evidence(
                doc_id, evidence_type, {"paragraph_index": index}, content(raw_text=paragraph.text),
                ordinal=index, style=style or None,
                native_properties={"paragraph_style_name": paragraph.style.name if paragraph.style else None},
            )
            self.contain_document(doc_id, ev["evidence_id"])
        for table_index, table in enumerate(parsed.tables, 1):
            table_ev = self.add_evidence(
                doc_id, "table", {"table_index": table_index},
                content(raw_value={"rows": len(table.rows), "columns": len(table.columns)}),
                ordinal=table_index,
            )
            self.contain_document(doc_id, table_ev["evidence_id"])
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

    def extract_xlsx(self, path: Path) -> None:
        from openpyxl import load_workbook

        workbook = load_workbook(path, data_only=False, read_only=False)
        doc = self.add_document(path, "openpyxl+ooxml")
        doc_id = doc["document_id"]
        if not self.diagnostic:
            self.mark_partial(doc, "advanced OOXML objects and chart source relations are not yet fully mapped")
        sheet_ids: dict[str, str] = {}
        for sheet_index, sheet in enumerate(workbook.worksheets, 1):
            sheet_ev = self.add_evidence(
                doc_id, "worksheet", {"sheet_name": sheet.title},
                content(raw_value={"title": sheet.title, "max_row": sheet.max_row, "max_column": sheet.max_column}),
                ordinal=sheet_index,
            )
            sheet_ids[sheet.title] = sheet_ev["evidence_id"]
            self.contain_document(doc_id, sheet_ev["evidence_id"])
            for row in sheet.iter_rows():
                for cell_obj in row:
                    if cell_obj.value is None:
                        continue
                    if not self.may_add_leaf(doc_id):
                        break
                    style: dict[str, Any] = {
                        "source_style_id": str(cell_obj.style_id),
                        "number_format": cell_obj.number_format,
                    }
                    if cell_obj.fill and cell_obj.fill.fill_type:
                        style["fill_type"] = cell_obj.fill.fill_type
                    cell_ev = self.add_evidence(
                        doc_id, "table_cell", {"sheet_name": sheet.title, "cell": cell_obj.coordinate},
                        content(raw_value=cell_obj.value), parent_id=sheet_ev["evidence_id"],
                        style=style,
                        native_properties={"data_type": cell_obj.data_type},
                    )
                    if cell_obj.data_type == "f":
                        self.add_evidence(
                            doc_id, "formula", {"sheet_name": sheet.title, "cell": cell_obj.coordinate},
                            content(raw_text=str(cell_obj.value)), parent_id=cell_ev["evidence_id"],
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
        with zipfile.ZipFile(path) as archive:
            chart_members = sorted(name for name in archive.namelist() if name.startswith("xl/charts/") and name.endswith(".xml"))
            for chart_index, member in enumerate(chart_members, 1):
                raw = archive.read(member)
                chart_ev = self.add_evidence(
                    doc_id, "chart", {"source_member": member, "object_index": chart_index},
                    content(raw_value={"source_member": member, "xml_sha256": digest_bytes(raw)}),
                    ordinal=chart_index,
                    native_properties={"ooxml_part": member, "extended_chart": "/chartEx" in member},
                )
                self.contain_document(doc_id, chart_ev["evidence_id"])

    def extract_pptx(self, path: Path) -> None:
        from pptx import Presentation

        presentation = Presentation(path)
        doc = self.add_document(path, "python-pptx")
        doc_id = doc["document_id"]
        if not self.diagnostic:
            self.mark_partial(doc, "speaker notes and grouped-shape internals are not yet fully extracted")
        for slide_number, slide in enumerate(presentation.slides, 1):
            slide_ev = self.add_evidence(
                doc_id, "slide", {"slide_number": slide_number},
                content(raw_value={"slide_number": slide_number, "shape_count": len(slide.shapes)}),
                ordinal=slide_number,
            )
            self.contain_document(doc_id, slide_ev["evidence_id"])
            for shape_index, shape in enumerate(slide.shapes, 1):
                if not self.may_add_leaf(doc_id):
                    break
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
                    {"slide_number": slide_number, "shape_id": str(shape.shape_id), "object_index": shape_index},
                    shape_content, parent_id=slide_ev["evidence_id"], ordinal=shape_index,
                    geometry=geometry,
                    native_properties={"name": shape.name, "shape_type": str(shape.shape_type)},
                )
                if getattr(shape, "has_table", False):
                    table_ev = self.add_evidence(
                        doc_id, "table",
                        {"slide_number": slide_number, "shape_id": str(shape.shape_id)},
                        content(raw_value={"rows": len(shape.table.rows), "columns": len(shape.table.columns)}),
                        parent_id=shape_ev["evidence_id"],
                    )
                    for row_index, row in enumerate(shape.table.rows, 1):
                        for column_index, cell_obj in enumerate(row.cells, 1):
                            if not self.may_add_leaf(doc_id):
                                break
                            self.add_evidence(
                                doc_id, "table_cell",
                                {"slide_number": slide_number, "shape_id": str(shape.shape_id),
                                 "row_index": row_index, "column_index": column_index},
                                content(raw_text=cell_obj.text), parent_id=table_ev["evidence_id"],
                                ordinal=column_index,
                            )
                        if self.limit_reached(doc_id):
                            break
                if getattr(shape, "has_chart", False):
                    chart_ev = self.add_evidence(
                        doc_id, "chart",
                        {"slide_number": slide_number, "shape_id": str(shape.shape_id)},
                        content(raw_value={"series_count": len(shape.chart.series)}),
                        parent_id=shape_ev["evidence_id"],
                    )
                    for series_index, series in enumerate(shape.chart.series, 1):
                        self.add_evidence(
                            doc_id, "chart_series",
                            {"slide_number": slide_number, "shape_id": str(shape.shape_id), "series_index": series_index},
                            content(raw_value={"name": getattr(series, "name", None)}),
                            parent_id=chart_ev["evidence_id"], ordinal=series_index,
                        )

    def extract_pdf(self, path: Path) -> None:
        from pypdf import PdfReader

        reader = PdfReader(path)
        doc = self.add_document(path, "pypdf")
        doc_id = doc["document_id"]
        if not self.diagnostic:
            self.mark_partial(doc, "page text blocks, images, and layout regions are not yet separately extracted")
        pages_without_text = 0
        for page_number, page in enumerate(reader.pages, 1):
            text_value = page.extract_text() or ""
            media_box = page.mediabox
            geometry = {
                "coordinate_space": "page", "unit": "pt",
                "x": 0, "y": 0, "width": float(media_box.width), "height": float(media_box.height),
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
        if pages_without_text:
            self.mark_partial(doc, f"OCR deferred for {pages_without_text} page(s) without a text layer")

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
    probe = Probe(args.root, args.run_at, args.max_items)
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
