#!/usr/bin/env python3
"""Attach source-file and locator aliases to retrieval results."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from lexical_search_common import digest_file


def load_document_sources(intermediates: list[Path]) -> tuple[dict[str, str], list[dict[str, str]]]:
    sources: dict[str, str] = {}
    states: list[dict[str, str]] = []
    for directory in intermediates:
        resolved = directory.resolve()
        state_path = resolved / "build-state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        if state.get("build_status") not in {"complete", "complete_with_failures"}:
            raise ValueError(f"intermediate build is incomplete: {resolved}")
        for relative_path in state.get("input_paths", []):
            entry = state.get("entries", {}).get(relative_path, {})
            document_id = entry.get("document_id")
            if not document_id:
                raise ValueError(f"intermediate entry has no document_id: {relative_path}")
            previous = sources.setdefault(document_id, relative_path)
            if previous != relative_path:
                raise ValueError(f"document_id resolves to multiple source files: {document_id}")
        states.append({
            "path": str(resolved),
            "sha256": digest_file(state_path),
        })
    return sources, states


def enrich_retrieval(result: dict[str, Any], document_sources: dict[str, str]) -> dict[str, Any]:
    for item in result.get("results", []):
        locator = item.get("locator", {})
        item.update({
            "file": document_sources.get(item.get("document_id", "")),
            "page": locator.get("page_number"),
            "sheet": locator.get("sheet_name"),
            "slide": locator.get("slide_number"),
            "section": (
                locator.get("locator_text")
                or locator.get("source_member")
                or (
                    f"paragraphs={locator.get('paragraph_start')}-{locator.get('paragraph_end')}"
                    if locator.get("paragraph_start") is not None else None
                )
            ),
            "chunk_id": item.get("search_unit_id"),
            "evidence_text": item.get("text"),
        })
    return result
