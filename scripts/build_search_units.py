#!/usr/bin/env python3
"""Build deterministic, question-independent search units from Evidence shards."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any, Callable


BUILDER = "search-unit-builder"
BUILDER_VERSION = "0.1.0"
SCHEMA_VERSION = "0.1"
STATE_FILE = "search-build-state.json"
CELL_PATTERN = re.compile(r"^([A-Z]{1,3})([1-9][0-9]*)$")


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest_value(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def digest_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def stable_id(prefix: str, value: Any) -> str:
    return f"{prefix}_{digest_value(value)[:32]}"


def display_value(evidence: dict[str, Any]) -> str:
    content = evidence.get("content", {})
    if "raw_text" in content:
        return str(content["raw_text"]).strip()
    if "raw_value" in content:
        value = content["raw_value"]
        if value is None:
            return ""
        if isinstance(value, (dict, list)):
            return canonical_json(value)
        return str(value).strip()
    return ""


def column_letters_to_number(letters: str) -> int:
    result = 0
    for character in letters:
        result = result * 26 + ord(character) - ord("A") + 1
    return result


def table_cell_identity(evidence: dict[str, Any]) -> tuple[tuple[Any, ...], int, str, dict[str, Any]] | None:
    location = evidence.get("location", {})
    if "cell" in location and "sheet_name" in location:
        match = CELL_PATTERN.fullmatch(location["cell"])
        if not match:
            return None
        letters, row_text = match.groups()
        row = int(row_text)
        column = column_letters_to_number(letters)
        locator = {"sheet_name": location["sheet_name"], "row_index": row}
        return (("sheet", location["sheet_name"], row), column, letters, locator)
    if "table_index" in location and "row_index" in location:
        table = location["table_index"]
        row = location["row_index"]
        column = location.get("column_index", 1)
        locator = {"table_index": table, "row_index": row}
        if "slide_number" in location:
            locator["slide_number"] = location["slide_number"]
        if "shape_id" in location:
            locator["shape_id"] = location["shape_id"]
        container = ("table", location.get("slide_number"), location.get("shape_id"), table)
        return (container + (row,), column, str(column), locator)
    if "shape_id" in location and "row_index" in location and "slide_number" in location:
        row = location["row_index"]
        column = location.get("column_index", 1)
        locator = {
            "slide_number": location["slide_number"],
            "shape_id": location["shape_id"],
            "row_index": row,
        }
        container = ("slide_table", location["slide_number"], location["shape_id"])
        return (container + (row,), column, str(column), locator)
    return None


def make_unit(
    document_id: str,
    unit_type: str,
    evidence_ids: list[str],
    locator: dict[str, Any],
    search_text: str,
    generated_at: str,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    text_sha = hashlib.sha256(search_text.encode("utf-8")).hexdigest()
    identity = {
        "document_id": document_id,
        "unit_type": unit_type,
        "source_evidence_ids": evidence_ids,
        "locator": locator,
        "text_sha256": text_sha,
        "builder": BUILDER,
        "builder_version": BUILDER_VERSION,
    }
    record: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "record_type": "search_unit",
        "search_unit_id": stable_id("su", identity),
        "document_id": document_id,
        "unit_type": unit_type,
        "source_evidence_ids": evidence_ids,
        "locator": locator,
        "text": {"search_text": search_text, "sha256": text_sha, "char_count": len(search_text)},
        "provenance": {
            "builder": BUILDER,
            "builder_version": BUILDER_VERSION,
            "generated_at": generated_at,
            "deterministic": True,
        },
    }
    if context:
        record["context"] = context
    return record


class DocumentDeriver:
    def __init__(
        self,
        document_id: str,
        generated_at: str,
        emit: Callable[[dict[str, Any]], None],
        target_chars: int,
    ) -> None:
        self.document_id = document_id
        self.generated_at = generated_at
        self.emit = emit
        self.target_chars = target_chars
        self.paragraphs: list[dict[str, Any]] = []
        self.heading: dict[str, Any] | None = None
        self.current_row_key: tuple[Any, ...] | None = None
        self.current_row_locator: dict[str, Any] = {}
        self.current_row_cells: list[tuple[int, str, str, str]] = []
        self.headers: dict[tuple[Any, ...], list[tuple[int, str, str, str]]] = {}
        self.current_slide: int | None = None
        self.slide_shapes: list[tuple[str, str]] = []
        self.counts: dict[str, int] = {}

    def write(self, record: dict[str, Any]) -> None:
        self.emit(record)
        unit_type = record["unit_type"]
        self.counts[unit_type] = self.counts.get(unit_type, 0) + 1

    def flush_paragraphs(self) -> None:
        if not self.paragraphs:
            return
        text = "\n\n".join(item["text"] for item in self.paragraphs).strip()
        if text:
            ids = [item["id"] for item in self.paragraphs]
            indices = [item["index"] for item in self.paragraphs]
            context = {}
            if self.heading is not None:
                context["heading_text"] = self.heading["text"]
            self.write(make_unit(
                self.document_id,
                "paragraph_chunk",
                ids,
                {"paragraph_start": min(indices), "paragraph_end": max(indices)},
                text,
                self.generated_at,
                context,
            ))
        self.paragraphs = []

    def add_paragraph(self, evidence: dict[str, Any]) -> None:
        value = display_value(evidence)
        if not value:
            return
        is_heading = evidence["evidence_type"] == "heading"
        if is_heading:
            self.flush_paragraphs()
            self.heading = {
                "id": evidence["evidence_id"],
                "text": value,
                "index": evidence["location"]["paragraph_index"],
            }
        candidate = {"id": evidence["evidence_id"], "text": value, "index": evidence["location"]["paragraph_index"]}
        prospective = sum(len(item["text"]) for item in self.paragraphs) + len(value) + 2 * len(self.paragraphs)
        if self.paragraphs and prospective > self.target_chars:
            self.flush_paragraphs()
            if not is_heading and self.heading is not None:
                self.paragraphs.append(self.heading)
        self.paragraphs.append(candidate)

    def flush_row(self) -> None:
        if self.current_row_key is None:
            return
        cells = sorted(self.current_row_cells, key=lambda item: item[0])
        populated = [cell for cell in cells if cell[2]]
        if populated:
            container = self.current_row_key[:-1]
            header = self.headers.get(container)
            context: dict[str, Any]
            if header is None:
                self.headers[container] = cells
                context = {"header_method": "first_non_empty_row_candidate", "is_header_candidate": True}
                lines = [f"{label}: {value}" for _, label, value, _ in populated]
            else:
                labels = {column: value for column, _, value, _ in header if value}
                header_ids = [evidence_id for _, _, value, evidence_id in header if value]
                context = {
                    "header_method": "first_non_empty_row_candidate",
                    "is_header_candidate": False,
                    "header_labels": [labels.get(column, label) for column, label, _, _ in cells],
                    "header_evidence_ids": header_ids,
                }
                lines = [f"{labels.get(column, label)}: {value}" for column, label, value, _ in populated]
            row_evidence_ids = [evidence_id for _, _, _, evidence_id in populated]
            evidence_ids = list(dict.fromkeys(context.get("header_evidence_ids", []) + row_evidence_ids))
            self.write(make_unit(
                self.document_id,
                "table_row",
                evidence_ids,
                self.current_row_locator,
                "\n".join(lines),
                self.generated_at,
                context,
            ))
        self.current_row_key = None
        self.current_row_locator = {}
        self.current_row_cells = []

    def add_cell(self, evidence: dict[str, Any]) -> None:
        identity = table_cell_identity(evidence)
        if identity is None:
            return
        row_key, column, label, locator = identity
        if self.current_row_key != row_key:
            self.flush_row()
            self.current_row_key = row_key
            self.current_row_locator = locator
        self.current_row_cells.append((column, label, display_value(evidence), evidence["evidence_id"]))

    def flush_slide(self) -> None:
        if self.current_slide is not None and self.slide_shapes:
            text = "\n\n".join(value for _, value in self.slide_shapes if value).strip()
            if text:
                self.write(make_unit(
                    self.document_id,
                    "slide_text",
                    [evidence_id for evidence_id, value in self.slide_shapes if value],
                    {"slide_number": self.current_slide},
                    text,
                    self.generated_at,
                ))
        self.current_slide = None
        self.slide_shapes = []

    def add_shape(self, evidence: dict[str, Any]) -> None:
        slide = evidence.get("location", {}).get("slide_number")
        value = str(evidence.get("content", {}).get("raw_text", "")).strip()
        if slide is None or not value:
            return
        if self.current_slide != slide:
            self.flush_slide()
            self.current_slide = slide
        self.slide_shapes.append((evidence["evidence_id"], value))

    def add_page(self, evidence: dict[str, Any]) -> None:
        value = display_value(evidence)
        page = evidence.get("location", {}).get("page_number")
        if value and page is not None:
            self.write(make_unit(
                self.document_id,
                "page_text",
                [evidence["evidence_id"]],
                {"page_number": page},
                value,
                self.generated_at,
            ))

    def consume(self, evidence: dict[str, Any]) -> None:
        evidence_type = evidence.get("evidence_type")
        if evidence_type in {"heading", "paragraph"}:
            self.add_paragraph(evidence)
        elif evidence_type == "table_cell":
            self.add_cell(evidence)
        elif evidence_type == "shape":
            self.add_shape(evidence)
        elif evidence_type == "page":
            self.add_page(evidence)

    def finish(self) -> dict[str, int]:
        self.flush_paragraphs()
        self.flush_row()
        self.flush_slide()
        return self.counts


def load_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read JSON: {path}: {exc}") from exc


def prepare_output(output: Path) -> None:
    if output.exists() and any(output.iterdir()):
        raise ValueError(f"output directory is not empty: {output}")
    output.mkdir(parents=True, exist_ok=True)


def build(intermediate: Path, output: Path, target_chars: int) -> dict[str, Any]:
    if target_chars < 100:
        raise ValueError("--target-chars must be at least 100")
    state_path = intermediate / "build-state.json"
    state = load_json(state_path)
    if state.get("build_status") != "complete":
        raise ValueError("intermediate build must be complete")
    prepare_output(output)
    final_path = output / "search_units.jsonl"
    temporary_path = output / ".search_units.jsonl.tmp"
    counts: dict[str, int] = {}
    document_counts: dict[str, int] = {}
    with temporary_path.open("w", encoding="utf-8", newline="\n") as destination:
        for relative_path in state.get("input_paths", []):
            entry = state["entries"].get(relative_path)
            if entry is None:
                raise ValueError(f"missing intermediate entry: {relative_path}")
            shard = entry.get("shards", {}).get("evidence", {})
            evidence_path = intermediate / shard.get("relative_path", "")
            if not evidence_path.is_file() or digest_file(evidence_path) != shard.get("sha256"):
                raise ValueError(f"invalid evidence shard: {evidence_path}")
            emitted = 0

            def emit(record: dict[str, Any]) -> None:
                nonlocal emitted
                destination.write(canonical_json(record) + "\n")
                emitted += 1

            deriver = DocumentDeriver(entry["document_id"], state["run_at"], emit, target_chars)
            with evidence_path.open(encoding="utf-8") as source:
                for line_number, line in enumerate(source, 1):
                    if not line.strip():
                        continue
                    evidence = json.loads(line)
                    if evidence.get("document_id") != entry["document_id"]:
                        raise ValueError(f"{evidence_path}:{line_number}: document_id mismatch")
                    deriver.consume(evidence)
            per_type = deriver.finish()
            document_counts[entry["document_id"]] = emitted
            for unit_type, count in per_type.items():
                counts[unit_type] = counts.get(unit_type, 0) + count
        destination.flush()
        os.fsync(destination.fileno())
    os.replace(temporary_path, final_path)
    result = {
        "state_version": "1",
        "build_status": "complete",
        "builder": BUILDER,
        "builder_version": BUILDER_VERSION,
        "generated_at": state["run_at"],
        "deterministic": True,
        "target_chars": target_chars,
        "source": {
            "intermediate_state_sha256": digest_file(state_path),
            "extractor": state.get("extractor"),
            "extractor_version": state.get("extractor_version"),
        },
        "output": {
            "relative_path": final_path.name,
            "sha256": digest_file(final_path),
            "size_bytes": final_path.stat().st_size,
            "record_count": sum(counts.values()),
        },
        "counts_by_type": dict(sorted(counts.items())),
        "counts_by_document": document_counts,
    }
    state_output = output / STATE_FILE
    temporary_state = output / f".{STATE_FILE}.tmp"
    with temporary_state.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(canonical_json(result) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary_state, state_output)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--intermediate", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--target-chars", type=int, default=1200)
    args = parser.parse_args()
    print(canonical_json(build(args.intermediate.resolve(), args.out.resolve(), args.target_chars)))


if __name__ == "__main__":
    main()
