#!/usr/bin/env python3
"""Export the auditable Layer-1 inventory, text views, chunks, and evaluation reports."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from build_intermediate_records import SUPPORTED_SUFFIXES
from lexical_search_common import canonical_json, digest_file
from probe_intermediate_records import normalize_text, stable_id


IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".tif", ".tiff"}
IGNORED_SUFFIXES = {".pyc"}
IGNORED_NAMES = {".DS_Store"}
SKIP_DIRECTORIES = {".git", "__pycache__", ".ipynb_checkpoints", "node_modules"}
INVENTORY_FIELDS = [
    "file_id", "file_path", "file_name", "extension", "file_size", "source_sha256", "modified_at",
    "document_type", "processing_layer", "text_extractable", "page_count", "sheet_count",
    "slide_count", "extraction_status", "notes",
]
ISSUE_FIELDS = [
    "issue_id", "file_id", "document_id", "evidence_id", "source_path",
    "issue_type", "severity", "details", "status",
]
EVAL_FIELDS = [
    "retrieval_method", "eval_case_id", "ground_truth_status", "category", "query", "first_relevant_rank",
    "reciprocal_rank", "hit_at_1", "hit_at_3", "hit_at_5", "hit_at_10",
]
EXPERIMENT_FIELDS = [
    "experiment_id", "retrieval_method", "evaluation_cases", "hit_at_1", "hit_at_3",
    "hit_at_5", "hit_at_10", "recall_at_1", "recall_at_3", "recall_at_5",
    "recall_at_10", "mrr", "field_value_weight", "parent_context_penalty",
    "semantic_weight", "adaptive_semantic", "report_sha256",
]
NARRATIVE_TEXT_TYPES = {
    "page", "heading", "paragraph", "shape", "text_block", "notebook_cell",
    "code_block", "speaker_note", "header", "footer", "comment",
}
ORDER_KEYS = (
    "page_number", "slide_number", "paragraph_index", "notebook_cell_index",
    "code_line_start", "row_index",
)
MOJIBAKE_MARKERS = ("\ufffd", "Ã", "Â", "â€™", "â€œ", "â€", "縺", "蜿", "莠")


def atomic_text(path: Path, text: str) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def prepare_output(output: Path) -> None:
    if output.exists() and (not output.is_dir() or any(output.iterdir())):
        raise ValueError(f"refusing to overwrite non-empty output directory: {output}")
    output.mkdir(parents=True, exist_ok=True)


def read_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_number}: invalid JSON: {exc}") from exc


def source_files(root: Path) -> list[Path]:
    return sorted(
        (
            path for path in root.rglob("*")
            if path.is_file()
            and path.name not in IGNORED_NAMES
            and path.suffix.lower() not in IGNORED_SUFFIXES
            and not path.name.startswith("~$")
            and not any(part in SKIP_DIRECTORIES for part in path.parts)
        ),
        key=lambda item: unicodedata.normalize("NFC", item.relative_to(root).as_posix()),
    )


def document_type(suffix: str) -> str:
    return {
        ".docx": "word", ".xlsx": "spreadsheet", ".pptx": "presentation", ".pdf": "pdf",
        ".csv": "delimited_table", ".tsv": "delimited_table", ".json": "json", ".xml": "xml",
        ".ipynb": "notebook", ".md": "markdown", ".txt": "text", ".py": "source_code",
        ".toml": "configuration", ".yaml": "configuration", ".yml": "configuration",
        ".rst": "text", ".sql": "source_code", ".sh": "source_code", ".command": "source_code",
    }.get(suffix, "image" if suffix in IMAGE_SUFFIXES else "unsupported")


def issue_record(
    *, file_id: str, document_id: str, evidence_id: str, source_path: str,
    issue_type: str, severity: str, details: str, status: str,
) -> dict[str, str]:
    identity = {
        "file_id": file_id, "document_id": document_id, "evidence_id": evidence_id,
        "issue_type": issue_type, "details": details,
    }
    return {
        "issue_id": stable_id("issue", identity),
        "file_id": file_id,
        "document_id": document_id,
        "evidence_id": evidence_id,
        "source_path": source_path,
        "issue_type": issue_type,
        "severity": severity,
        "details": details,
        "status": status,
    }


def repeated_pdf_page_edges(
    intermediate: Path,
    documents: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    state = json.loads((intermediate / "build-state.json").read_text(encoding="utf-8"))
    entries = {
        entry["document_id"]: entry for entry in state.get("entries", {}).values()
    }
    result: dict[str, dict[str, set[str]]] = {}
    for document_id, document in documents.items():
        if document["source"]["extension"].lower() != "pdf":
            continue
        entry = entries.get(document_id, {})
        shard = entry.get("shards", {}).get("evidence", {})
        shard_path = intermediate / shard.get("relative_path", "")
        if not shard_path.is_file():
            raise ValueError(f"PDF Evidence shard is missing: {document_id}")
        first_lines: Counter[str] = Counter()
        last_lines: Counter[str] = Counter()
        first_pages: dict[str, list[int]] = defaultdict(list)
        last_pages: dict[str, list[int]] = defaultdict(list)
        text_pages = 0
        for evidence in read_jsonl(shard_path):
            if evidence.get("evidence_type") != "page":
                continue
            raw_text = evidence.get("content", {}).get("raw_text")
            if not raw_text:
                continue
            lines = [line.strip() for line in normalize_text(raw_text).splitlines() if line.strip()]
            if not lines:
                continue
            text_pages += 1
            first_lines[lines[0]] += 1
            last_lines[lines[-1]] += 1
            page_number = int(evidence.get("location", {}).get("page_number", text_pages))
            first_pages[lines[0]].append(page_number)
            last_pages[lines[-1]].append(page_number)
        if text_pages >= 3:
            first = {line for line, count in first_lines.items() if count >= 3}
            last = {line for line, count in last_lines.items() if count >= 3}
            if first or last:
                result[document_id] = {
                    "first": first,
                    "last": last,
                    "first_keep_page": {line: min(first_pages[line]) for line in first},
                    "last_keep_page": {line: min(last_pages[line]) for line in last},
                }
    return result


def normalize_page_with_edges(
    raw_text: str,
    repeated_edges: dict[str, Any],
    page_number: int | None = None,
) -> tuple[str, list[dict[str, str]]]:
    normalized = normalize_text(raw_text)
    lines = normalized.splitlines()
    operations: list[dict[str, str]] = []
    first_text = lines[0].strip() if lines else ""
    keep_first = repeated_edges.get("first_keep_page", {}).get(first_text)
    if (
        lines and first_text in repeated_edges.get("first", set())
        and (keep_first is None or page_number != keep_first)
    ):
        removed = lines.pop(0).strip()
        operations.append({"operation": "remove_repeated_pdf_header", "text": removed})
        while lines and not lines[0].strip():
            lines.pop(0)
    last_text = lines[-1].strip() if lines else ""
    keep_last = repeated_edges.get("last_keep_page", {}).get(last_text)
    if (
        lines and last_text in repeated_edges.get("last", set())
        and (keep_last is None or page_number != keep_last)
    ):
        removed = lines.pop().strip()
        operations.append({"operation": "remove_repeated_pdf_footer", "text": removed})
        while lines and not lines[-1].strip():
            lines.pop()
    return "\n".join(lines), operations


def build_evidence_views(
    intermediate: Path,
    output: Path,
    documents: dict[str, dict[str, Any]],
) -> tuple[dict[str, dict[str, Any]], list[dict[str, str]], Counter[str]]:
    stats: dict[str, dict[str, Any]] = defaultdict(lambda: {
        "types": Counter(), "pages": set(), "text_pages": set(), "ocr_pages": set(),
        "sheets": set(), "slides": set(), "raw_records": 0, "text_chars": 0,
        "quality_counts": Counter(), "quality_examples": {}, "last_order": {},
        "narrative_hashes": Counter(), "header_footer_hashes": Counter(),
        "page_edge_lines": Counter(), "table_expected": {}, "table_children": Counter(),
        "normalization_operations": 0,
    })
    issues: list[dict[str, str]] = []
    evidence_counts: Counter[str] = Counter()
    file_ids = {
        document_id: stable_id("file", {
            "relative_path": document["source"]["relative_path"],
            "source_sha256": document["source"]["sha256"],
        })
        for document_id, document in documents.items()
    }
    repeated_edges = repeated_pdf_page_edges(intermediate, documents)
    raw_path = output / "native_text_raw.jsonl"
    normalized_path = output / "native_text_normalized.jsonl"
    with raw_path.open("w", encoding="utf-8", newline="\n") as raw_out, normalized_path.open(
        "w", encoding="utf-8", newline="\n"
    ) as normalized_out:
        for evidence in read_jsonl(intermediate / "evidence.jsonl"):
            document_id = evidence["document_id"]
            document = documents[document_id]
            source = document["source"]
            file_id = file_ids[document_id]
            evidence_id = evidence["evidence_id"]
            evidence_type = evidence["evidence_type"]
            content = evidence["content"]
            location = evidence.get("location", {})
            item_stats = stats[document_id]
            item_stats["types"][evidence_type] += 1
            evidence_counts[evidence_type] += 1

            def quality(issue_type: str, severity: str, details: str) -> None:
                key = (issue_type, severity, details)
                item_stats["quality_counts"][key] += 1
                item_stats["quality_examples"].setdefault(key, evidence_id)

            if "page_number" in location:
                item_stats["pages"].add(location["page_number"])
                if content.get("content_ref"):
                    item_stats["ocr_pages"].add(location["page_number"])
                    quality("body_missing", "warning", "page has no native text layer")
                else:
                    item_stats["text_pages"].add(location["page_number"])
            if "sheet_name" in location:
                item_stats["sheets"].add(location["sheet_name"])
            if "slide_number" in location:
                item_stats["slides"].add(location["slide_number"])

            for order_key in ORDER_KEYS:
                order_value = location.get(order_key)
                if not isinstance(order_value, int):
                    continue
                scope = (
                    evidence_type, order_key, location.get("sheet_name"),
                    location.get("table_index"),
                    None if order_key == "slide_number" else location.get("slide_number"),
                    location.get("shape_id"), location.get("source_member"),
                )
                previous = item_stats["last_order"].get(scope)
                if previous is not None and order_value < previous:
                    quality("reading_order_anomaly", "warning", f"{order_key} decreases in extraction order")
                item_stats["last_order"][scope] = order_value

            if evidence_type == "table":
                value = content.get("raw_value")
                if isinstance(value, dict) and isinstance(value.get("rows"), int) and isinstance(value.get("columns"), int):
                    item_stats["table_expected"][evidence_id] = (
                        value["rows"] * value["columns"], evidence_id,
                    )
            if evidence_type in {"table_cell", "table_row"} and evidence.get("parent_evidence_id"):
                item_stats["table_children"][evidence["parent_evidence_id"]] += 1
            if evidence_type == "table_row":
                native = evidence.get("native_properties", {})
                headers = native.get("headers")
                values = native.get("values")
                if isinstance(headers, list) and isinstance(values, list) and len(headers) != len(values):
                    quality("table_structure_collapse", "warning", "table row width differs from its header width")

            if "raw_text" not in content and "raw_value" not in content:
                continue
            common = {
                "file_id": file_id,
                "document_id": document_id,
                "evidence_id": evidence_id,
                "source_path": source["relative_path"],
                "file_name": source["file_name"],
                "file_type": source["extension"],
                "evidence_type": evidence_type,
                "location": location,
            }
            if "raw_text" in content:
                raw_value: Any = content["raw_text"]
                normalized_value: Any = content.get("normalized_text", normalize_text(content["raw_text"]))
                raw_field = "raw_text"
                normalized_field = "normalized_text"
                normalization_operations: list[dict[str, str]] = []
                if evidence_type == "page" and document_id in repeated_edges:
                    normalized_value, normalization_operations = normalize_page_with_edges(
                        content["raw_text"], repeated_edges[document_id], location.get("page_number")
                    )
            else:
                raw_value = content["raw_value"]
                normalized_value = content.get("normalized_value", raw_value)
                raw_field = "raw_value"
                normalized_field = "normalized_value"
                normalization_operations = []
            raw_out.write(canonical_json({**common, raw_field: raw_value}) + "\n")
            normalized_record = {**common, normalized_field: normalized_value}
            if normalization_operations:
                normalized_record["normalization_operations"] = normalization_operations
                item_stats["normalization_operations"] += len(normalization_operations)
            normalized_out.write(canonical_json(normalized_record) + "\n")
            item_stats["raw_records"] += 1

            text = content.get("raw_text")
            if text is not None:
                stripped = str(text).strip()
                item_stats["text_chars"] += len(stripped)
                if not stripped:
                    quality("empty_text", "warning", "extracted text is empty or whitespace-only")
                if any(marker in str(text) for marker in MOJIBAKE_MARKERS):
                    quality("mojibake", "error", "replacement character or common mojibake marker found")
                controls = re.findall(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", text)
                if controls:
                    quality("control_character", "warning", "control characters found")
                longest_line = max((len(line) for line in text.splitlines()), default=0)
                if longest_line > 10000:
                    quality("abnormally_long_line", "warning", "a line exceeds 10000 characters")
                if "normalized_text" not in content:
                    quality("missing_normalized_text", "error", "raw_text has no normalized_text companion")
                compact = re.sub(r"\s+", "", stripped)
                if evidence_type == "page" and 0 < len(compact) < 20:
                    quality("abnormally_short_text", "warning", "native PDF page has fewer than 20 non-space characters")
                if (
                    evidence_type in {"page", "paragraph", "text_block"}
                    and compact
                    and re.fullmatch(r"[\d.,+\-/%年月日時分秒円$¥€£()]+", compact)
                ):
                    quality("numeric_only_text", "info", "narrative text contains only numbers, units, or punctuation")
                if evidence_type in NARRATIVE_TEXT_TYPES and stripped:
                    text_hash = hashlib.sha256(stripped.encode("utf-8")).hexdigest()
                    item_stats["narrative_hashes"][(evidence_type, text_hash)] += 1
                    if evidence_type in {"header", "footer"}:
                        item_stats["header_footer_hashes"][(evidence_type, text_hash)] += 1
                if evidence_type == "page" and stripped:
                    lines = [line.strip() for line in stripped.splitlines() if line.strip()]
                    if lines:
                        item_stats["page_edge_lines"][("first", lines[0])] += 1
                        item_stats["page_edge_lines"][("last", lines[-1])] += 1

    for document_id, document in documents.items():
        item_stats = stats[document_id]
        source = document["source"]
        file_id = file_ids[document_id]
        extension = source["extension"].lower()
        if item_stats["raw_records"] == 0:
            key = ("body_missing", "warning", "document has no native raw text or value records")
            item_stats["quality_counts"][key] += 1
            item_stats["quality_examples"].setdefault(key, "")
        elif extension in {"pdf", "docx", "pptx", "md", "txt", "rst"} and item_stats["text_chars"] < 20:
            key = ("abnormally_short_text", "warning", "document has fewer than 20 native text characters")
            item_stats["quality_counts"][key] += 1
            item_stats["quality_examples"].setdefault(key, "")
        if extension == "pdf" and item_stats["pages"]:
            expected = set(range(1, max(item_stats["pages"]) + 1))
            if expected != item_stats["pages"]:
                key = ("page_missing", "error", "one or more page numbers are absent from extracted Evidence")
                item_stats["quality_counts"][key] += 1
                item_stats["quality_examples"].setdefault(key, "")
        if extension == "xlsx" and not item_stats["sheets"]:
            key = ("sheet_missing", "error", "spreadsheet has no extracted worksheet")
            item_stats["quality_counts"][key] += 1
            item_stats["quality_examples"].setdefault(key, "")
        if extension == "pptx" and not item_stats["slides"]:
            key = ("slide_missing", "error", "presentation has no extracted slide")
            item_stats["quality_counts"][key] += 1
            item_stats["quality_examples"].setdefault(key, "")
        bad_tables = sum(
            1 for parent_id, (expected_children, _) in item_stats["table_expected"].items()
            if item_stats["table_children"][parent_id] != expected_children
        )
        if bad_tables:
            key = ("table_structure_collapse", "warning", "table cell count differs from declared rows x columns")
            item_stats["quality_counts"][key] += bad_tables
            first = next(
                evidence_id for parent_id, (expected_children, evidence_id) in item_stats["table_expected"].items()
                if item_stats["table_children"][parent_id] != expected_children
            )
            item_stats["quality_examples"].setdefault(key, first)
        duplicate_values = sum(count - 1 for count in item_stats["narrative_hashes"].values() if count >= 5)
        if duplicate_values:
            key = ("mass_duplicate_text", "warning", "exact narrative text occurs at least five times")
            item_stats["quality_counts"][key] += duplicate_values
            item_stats["quality_examples"].setdefault(key, "")
        duplicate_header_footer = sum(
            count - 1 for count in item_stats["header_footer_hashes"].values() if count >= 2
        )
        repeated_page_edges = sum(
            count - 1 for count in item_stats["page_edge_lines"].values()
            if count >= 3 and len(item_stats["pages"]) >= 3
        )
        if duplicate_header_footer or repeated_page_edges:
            key = (
                "duplicate_header_footer", "info",
                "repeated native header/footer text or repeated first/last PDF page line detected",
            )
            item_stats["quality_counts"][key] += duplicate_header_footer + repeated_page_edges
            item_stats["quality_examples"].setdefault(key, "")
        for (issue_type, severity, details), count in sorted(item_stats["quality_counts"].items()):
            issues.append(issue_record(
                file_id=file_id,
                document_id=document_id,
                evidence_id=item_stats["quality_examples"].get((issue_type, severity, details), ""),
                source_path=source["relative_path"],
                issue_type=issue_type,
                severity=severity,
                details=f"{details}; occurrences={count}",
                status="deferred" if issue_type == "body_missing" else "unresolved",
            ))
    return stats, issues, evidence_counts


def build_inventory(
    root: Path,
    documents: dict[str, dict[str, Any]],
    stats: dict[str, dict[str, Any]],
    output: Path,
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    by_path = {item["source"]["relative_path"]: item for item in documents.values()}
    rows: list[dict[str, Any]] = []
    file_id_by_path: dict[str, str] = {}
    for path in source_files(root):
        relative_path = unicodedata.normalize("NFC", path.relative_to(root).as_posix())
        source_sha = digest_file(path)
        file_id = stable_id("file", {"relative_path": relative_path, "source_sha256": source_sha})
        file_id_by_path[relative_path] = file_id
        suffix = path.suffix.lower()
        document = by_path.get(relative_path)
        document_id = document["document_id"] if document else ""
        item_stats = stats.get(document_id, {})
        types = item_stats.get("types", Counter())
        layers: list[str] = []
        notes: list[str] = []
        text_extractable = False
        extraction_status = "deferred"
        if suffix in SUPPORTED_SUFFIXES:
            layers.append("native_text")
            text_extractable = bool(document and item_stats.get("raw_records", 0))
            extraction_status = document["extraction"]["status"] if document else "missing"
        elif suffix in IMAGE_SUFFIXES:
            lowered = relative_path.lower()
            if "/figures/" in lowered or any(token in path.stem.lower() for token in ("chart", "graph", "plot", "heatmap", "distribution", "trend")):
                layers.append("graph_required")
            else:
                layers.append("ocr_required")
        else:
            layers.append("unsupported")
            extraction_status = "unsupported"
        if item_stats.get("ocr_pages"):
            layers.append("ocr_required")
            notes.append(f"OCR deferred for pages {sorted(item_stats['ocr_pages'])}")
        if types.get("chart") or types.get("chart_series") or types.get("pivot_table"):
            layers.append("graph_required")
        if types.get("image"):
            if suffix == ".ipynb" or "/analysis_project/" in relative_path.lower():
                layers.append("graph_required")
            else:
                layers.append("ocr_required")
            notes.append(f"{types['image']} embedded image(s) recorded")
        if document:
            notes.extend(document["extraction"].get("warnings", []))
            notes.extend(document["extraction"].get("errors", []))
        layers = list(dict.fromkeys(layers))
        stat = path.stat()
        rows.append({
            "file_id": file_id,
            "file_path": relative_path,
            "file_name": unicodedata.normalize("NFC", path.name),
            "extension": suffix.lstrip("."),
            "file_size": stat.st_size,
            "source_sha256": source_sha,
            "modified_at": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
            "document_type": document_type(suffix),
            "processing_layer": ";".join(layers),
            "text_extractable": str(text_extractable).lower(),
            "page_count": len(item_stats.get("pages", set())) or "",
            "sheet_count": len(item_stats.get("sheets", set())) or "",
            "slide_count": len(item_stats.get("slides", set())) or "",
            "extraction_status": extraction_status,
            "notes": " | ".join(dict.fromkeys(notes)),
        })
    with (output / "text_inventory.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=INVENTORY_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    return rows, file_id_by_path


def append_document_issues(
    documents: dict[str, dict[str, Any]],
    file_id_by_path: dict[str, str],
    issues: list[dict[str, str]],
) -> None:
    seen = {item["issue_id"] for item in issues}
    for document in documents.values():
        source = document["source"]
        file_id = file_id_by_path[source["relative_path"]]
        extraction = document["extraction"]
        for kind, severity in (("warnings", "warning"), ("errors", "error")):
            for detail in extraction.get(kind, []):
                if "OCR deferred" in detail:
                    issue_type, issue_severity, status = "ocr_deferred", "warning", "deferred"
                elif "embedded image(s) recorded" in detail:
                    issue_type, issue_severity, status = "visual_processing_deferred", "info", "deferred"
                elif "PDF native text is preserved" in detail:
                    issue_type, issue_severity, status = "layout_analysis_deferred", "info", "deferred"
                elif "password-protected Office source decrypted" in detail:
                    issue_type, issue_severity, status = "password_decrypted", "info", "resolved"
                else:
                    issue_type, issue_severity, status = f"document_{kind[:-1]}", severity, "unresolved"
                record = issue_record(
                    file_id=file_id, document_id=document["document_id"], evidence_id="",
                    source_path=source["relative_path"], issue_type=issue_type,
                    severity=issue_severity, details=detail, status=status,
                )
                if record["issue_id"] not in seen:
                    seen.add(record["issue_id"])
                    issues.append(record)


def write_issues(output: Path, issues: list[dict[str, str]]) -> None:
    with (output / "text_extraction_issues.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=ISSUE_FIELDS)
        writer.writeheader()
        writer.writerows(sorted(issues, key=lambda item: (item["source_path"], item["issue_type"], item["evidence_id"])))


def chunk_record(unit: dict[str, Any], document: dict[str, Any], previous_id: str | None, next_id: str | None) -> dict[str, Any]:
    locator = unit.get("locator", {})
    context = unit.get("context", {})
    parent_text = context.get("container_heading_text") or context.get("heading_text") or unit["text"]["search_text"]
    return {
        "chunk_id": unit["search_unit_id"],
        "document_id": unit["document_id"],
        "source_path": document["source"]["relative_path"],
        "file_name": document["source"]["file_name"],
        "file_type": document["source"]["extension"],
        "page": locator.get("page_number"),
        "sheet": locator.get("sheet_name"),
        "slide": locator.get("slide_number"),
        "section_path": locator.get("locator_text") or context.get("heading_text") or context.get("container_heading_text"),
        "row_number": locator.get("row_index"),
        "chunk_text": unit["text"]["search_text"],
        "parent_text": parent_text,
        "previous_chunk_id": previous_id,
        "next_chunk_id": next_id,
        "source_evidence_ids": unit["source_evidence_ids"],
        "modality": "native_text" if not unit["unit_type"].startswith("chart_") else "chart_table",
    }


def build_chunks(search_output: Path, output: Path, documents: dict[str, dict[str, Any]]) -> Counter[str]:
    counts: Counter[str] = Counter()
    units = read_jsonl(search_output / "search_units.jsonl")
    with (output / "text_chunks.jsonl").open("w", encoding="utf-8", newline="\n") as destination:
        previous: dict[str, Any] | None = None
        previous_previous_id: str | None = None
        for current in units:
            counts[current["unit_type"]] += 1
            if previous is not None:
                same_document = previous["document_id"] == current["document_id"]
                destination.write(canonical_json(chunk_record(
                    previous,
                    documents[previous["document_id"]],
                    previous_previous_id,
                    current["search_unit_id"] if same_document else None,
                )) + "\n")
                previous_previous_id = previous["search_unit_id"] if same_document else None
            previous = current
        if previous is not None:
            destination.write(canonical_json(chunk_record(
                previous, documents[previous["document_id"]], previous_previous_id, None,
            )) + "\n")
    return counts


def load_reports(paths: list[Path]) -> list[tuple[Path, dict[str, Any]]]:
    if not paths:
        raise ValueError("at least one retrieval evaluation report is required")
    reports: list[tuple[Path, dict[str, Any]]] = []
    for path in paths:
        report = json.loads(path.read_text(encoding="utf-8"))
        if not report.get("cases") or not report.get("overall"):
            raise ValueError(f"evaluation report is incomplete: {path}")
        if not report.get("inputs", {}).get("intermediate_states"):
            raise ValueError(f"evaluation report has no intermediate source trace: {path}")
        for case in report["cases"]:
            for result in case.get("retrieved_results", []):
                required_trace = {
                    "file", "page", "sheet", "slide", "section", "chunk_id", "evidence_text",
                }
                if not required_trace <= result.keys() or not result.get("file"):
                    raise ValueError(f"evaluation result lacks source trace fields: {path}")
        reports.append((path, report))
    evaluation_hashes = {
        report.get("inputs", {}).get("evaluation_set_sha256") for _, report in reports
    }
    case_sets = {
        tuple(case.get("eval_case_id") for case in report["cases"]) for _, report in reports
    }
    methods = [report.get("retrieval_method") for _, report in reports]
    if None in evaluation_hashes or len(evaluation_hashes) != 1 or len(case_sets) != 1:
        raise ValueError("retrieval reports must use the same evaluation set and ordered cases")
    if len(methods) != len(set(methods)):
        raise ValueError("retrieval reports must use distinct retrieval methods")
    return reports


def write_evaluation(output: Path, reports: list[tuple[Path, dict[str, Any]]]) -> None:
    with (output / "text_retrieval_eval.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=EVAL_FIELDS)
        writer.writeheader()
        for _, report in reports:
            method = report.get("retrieval_method") or "BM25"
            for case in report.get("cases", []):
                rank = case.get("first_relevant_rank")
                writer.writerow({
                    "retrieval_method": method,
                    "eval_case_id": case.get("eval_case_id"),
                    "ground_truth_status": case.get("ground_truth_status", "needs_human_review"),
                    "category": case.get("category"),
                    "query": case.get("query"),
                    "first_relevant_rank": rank if rank is not None else "",
                    "reciprocal_rank": case.get("reciprocal_rank"),
                    **{
                        f"hit_at_{cutoff}": int(rank is not None and rank <= cutoff)
                        for cutoff in (1, 3, 5, 10)
                    },
                })

    with (output / "text_experiment_log.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=EXPERIMENT_FIELDS)
        writer.writeheader()
        for path, report in reports:
            overall = report.get("overall", {})
            method = report.get("retrieval_method") or "BM25"
            report_sha = digest_file(path)
            writer.writerow({
                "experiment_id": stable_id("exp", {"method": method, "report_sha256": report_sha}),
                "retrieval_method": method,
                "evaluation_cases": overall.get("case_count"),
                **{key: overall.get(key) for key in (
                    "hit_at_1", "hit_at_3", "hit_at_5", "hit_at_10",
                    "recall_at_1", "recall_at_3", "recall_at_5", "recall_at_10", "mrr",
                )},
                "field_value_weight": report.get("field_value_weight"),
                "parent_context_penalty": report.get("parent_context_penalty"),
                "semantic_weight": report.get("semantic_weight"),
                "adaptive_semantic": report.get("adaptive_semantic"),
                "report_sha256": report_sha,
            })

    lines = [
        "# Text retrieval summary", "",
        "Ground Truthは人手確認済み評価セットだけを使用し、検索後に正解IDを照合した。", "",
        "| Retrieval | Cases | Hit@1 | Hit@3 | Hit@5 | Hit@10 | Recall@10 | MRR |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for _, report in reports:
        overall = report.get("overall", {})
        lines.append(
            f"| {report.get('retrieval_method') or 'BM25'} | {overall.get('case_count', 0)} | "
            f"{overall.get('hit_at_1', 0):.4f} | {overall.get('hit_at_3', 0):.4f} | "
            f"{overall.get('hit_at_5', 0):.4f} | {overall.get('hit_at_10', 0):.4f} | "
            f"{overall.get('recall_at_10', 0):.4f} | {overall.get('mrr', 0):.4f} |"
        )
    best_report = max(
        (report for _, report in reports),
        key=lambda report: (
            report.get("overall", {}).get("hit_at_1", 0),
            report.get("overall", {}).get("mrr", 0),
            report.get("overall", {}).get("hit_at_10", 0),
        ),
    )
    lines.extend([
        "",
        "## Best observed setting",
        "",
        f"Hit@1、MRR、Hit@10の順で比較した最良設定: `{best_report.get('retrieval_method')}`。",
    ])
    atomic_text(output / "text_retrieval_summary.md", "\n".join(lines) + "\n")

    failures = [
        (report.get("retrieval_method") or "BM25", case)
        for _, report in reports
        for case in report.get("cases", [])
        if case.get("first_relevant_rank") is None or case.get("first_relevant_rank", 999) > 5
    ]
    error_lines = [
        "# Text retrieval error analysis", "",
        "検索上位5件に正解根拠がないケースを、ランキング失敗候補として記録する。", "",
        f"対象: {len(failures)}件", "",
    ]
    for method, case in failures:
        rank = case.get("first_relevant_rank")
        lowered_method = method.lower()
        if "embedding" in lowered_method or "cosine" in lowered_method:
            failure_class = "I: Embedding failure"
        elif "rrf" in lowered_method or "hybrid" in lowered_method or "semantic" in lowered_method:
            failure_class = "J: ranking failure"
        else:
            failure_class = "H: BM25 failure"
        error_lines.extend([
            f"## {case.get('eval_case_id')}", "",
            f"- Retrieval: {method}",
            f"- Category: {case.get('category')}",
            f"- First relevant rank: {rank if rank is not None else 'not retrieved'}",
            f"- Failure class: {failure_class}",
            f"- Query: {case.get('query')}", "",
        ])
    atomic_text(output / "text_error_analysis.md", "\n".join(error_lines) + "\n")


def write_state(
    output: Path,
    root: Path,
    intermediate: Path,
    search_output: Path,
    report_paths: list[Path],
    inventory: list[dict[str, Any]],
    documents: dict[str, dict[str, Any]],
    evidence_counts: Counter[str],
    chunk_counts: Counter[str],
    issues: list[dict[str, str]],
    native_text_records: int,
    normalization_operations: int,
) -> dict[str, Any]:
    file_names = [
        "text_inventory.csv", "native_text_raw.jsonl", "native_text_normalized.jsonl",
        "text_extraction_issues.csv", "text_chunks.jsonl", "text_retrieval_eval.csv",
        "text_retrieval_summary.md", "text_error_analysis.md", "text_experiment_log.csv",
    ]
    intermediate_state_path = intermediate / "build-state.json"
    search_state_path = search_output / "search-build-state.json"
    search_state = json.loads(search_state_path.read_text(encoding="utf-8"))
    state = {
        "state_version": "1",
        "build_status": "complete",
        "inputs": {
            "source_root": unicodedata.normalize("NFC", str(root.resolve())),
            "intermediate": {
                "path": unicodedata.normalize("NFC", str(intermediate.resolve())),
                "state_sha256": digest_file(intermediate_state_path),
            },
            "search_output": {
                "path": unicodedata.normalize("NFC", str(search_output.resolve())),
                "state_sha256": digest_file(search_state_path),
                "search_units_sha256": search_state.get("output", {}).get("sha256"),
                "record_count": search_state.get("output", {}).get("record_count"),
                "target_chars": search_state.get("target_chars"),
            },
            "evaluation_reports": [
                {
                    "path": unicodedata.normalize("NFC", str(path.resolve())),
                    "sha256": digest_file(path),
                }
                for path in report_paths
            ],
        },
        "files": {
            name: {
                "sha256": digest_file(output / name),
                "size_bytes": (output / name).stat().st_size,
            }
            for name in file_names
        },
        "counts": {
            "inventory_files": len(inventory),
            "documents": len(documents),
            "issues": len(issues),
            "native_text_records": native_text_records,
            "normalization_operations": normalization_operations,
            "evidence_by_type": dict(sorted(evidence_counts.items())),
            "chunks_by_type": dict(sorted(chunk_counts.items())),
            "processing_layers": dict(sorted(Counter(
                layer for row in inventory for layer in row["processing_layer"].split(";") if layer
            ).items())),
            "extraction_status": dict(sorted(Counter(row["extraction_status"] for row in inventory).items())),
        },
    }
    atomic_text(output / "layer1-state.json", canonical_json(state) + "\n")
    return state


def build(
    root: Path,
    intermediate: Path,
    search_output: Path,
    output: Path,
    report_paths: list[Path],
) -> dict[str, Any]:
    prepare_output(output)
    documents = {
        item["document_id"]: item for item in read_jsonl(intermediate / "documents.jsonl")
    }
    stats, issues, evidence_counts = build_evidence_views(intermediate, output, documents)
    inventory, file_id_by_path = build_inventory(root, documents, stats, output)
    append_document_issues(documents, file_id_by_path, issues)
    write_issues(output, issues)
    chunk_counts = build_chunks(search_output, output, documents)
    reports = load_reports(report_paths)
    write_evaluation(output, reports)
    return write_state(
        output,
        root,
        intermediate,
        search_output,
        report_paths,
        inventory,
        documents,
        evidence_counts,
        chunk_counts,
        issues,
        sum(item["raw_records"] for item in stats.values()),
        sum(item["normalization_operations"] for item in stats.values()),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--intermediate", required=True, type=Path)
    parser.add_argument("--search-output", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--evaluation-report", action="append", type=Path, default=[])
    args = parser.parse_args()
    print(canonical_json(build(
        args.root.resolve(), args.intermediate.resolve(), args.search_output.resolve(),
        args.out.resolve(), [path.resolve() for path in args.evaluation_report],
    )))


if __name__ == "__main__":
    main()
