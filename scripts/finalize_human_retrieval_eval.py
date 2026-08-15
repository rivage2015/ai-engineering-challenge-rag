#!/usr/bin/env python3
"""Finalize reviewed retrieval drafts into deterministic evaluation records."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
from typing import Any

from lexical_search_common import canonical_json, digest_file, tokenize


GENERATOR = "human-retrieval-eval-finalizer"
GENERATOR_VERSION = "0.1.0"
EVAL_FILE = "evaluation-set.jsonl"
STATE_FILE = "evaluation-set-state.json"
CATEGORIES = {
    "paragraph_chunk", "table_row", "slide_text", "page_text", "text_chunk",
    "code_chunk", "notebook_cell", "chart_summary", "chart_series", "cross_format", "other",
}
REVIEW_METHODS = {"structural", "visual_and_structural"}


def stable_id(prefix: str, value: Any) -> str:
    digest = hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()
    return f"{prefix}_{digest[:32]}"


def load_search_units(search_output: Path) -> tuple[dict[str, dict[str, Any]], dict[str, Any], Path]:
    state_path = search_output / "search-build-state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    units_path = search_output / state.get("output", {}).get("relative_path", "")
    if state.get("build_status") != "complete" or digest_file(units_path) != state.get("output", {}).get("sha256"):
        raise ValueError("SearchUnit input is incomplete or does not match its state")
    units: dict[str, dict[str, Any]] = {}
    with units_path.open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                unit = json.loads(line)
                units[unit["search_unit_id"]] = {"unit_type": unit["unit_type"]}
    return units, state, state_path


def load_drafts(path: Path, units: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    seen_queries: set[str] = set()
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                draft = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_number}: invalid JSON: {exc}") from exc
            allowed = {"query", "relevant_search_unit_ids", "category", "review"}
            if set(draft) != allowed:
                raise ValueError(f"{path}:{line_number}: draft fields must be exactly {sorted(allowed)}")
            query = draft["query"].strip()
            relevant = draft["relevant_search_unit_ids"]
            category = draft["category"]
            review = draft["review"]
            if not query or not tokenize(query) or query in seen_queries:
                raise ValueError(f"{path}:{line_number}: query is empty, unsearchable, or duplicated")
            if not relevant or len(relevant) != len(set(relevant)):
                raise ValueError(f"{path}:{line_number}: relevant IDs must be nonempty and unique")
            missing = [search_unit_id for search_unit_id in relevant if search_unit_id not in units]
            if missing:
                raise ValueError(f"{path}:{line_number}: relevant SearchUnits are missing: {missing}")
            if category not in CATEGORIES:
                raise ValueError(f"{path}:{line_number}: invalid category {category!r}")
            if category in {
                "paragraph_chunk", "table_row", "slide_text", "page_text", "text_chunk",
                "code_chunk", "notebook_cell", "chart_summary", "chart_series",
            }:
                mismatched = [item for item in relevant if units[item]["unit_type"] != category]
                if mismatched:
                    raise ValueError(f"{path}:{line_number}: category does not match relevant units: {mismatched}")
            if set(review) != {"reviewed", "method", "source_locations"}:
                raise ValueError(f"{path}:{line_number}: invalid review fields")
            locations = review["source_locations"]
            if review["reviewed"] is not True or review["method"] not in REVIEW_METHODS:
                raise ValueError(f"{path}:{line_number}: review is not complete")
            if not locations or len(locations) != len(set(locations)) or any(not item.strip() for item in locations):
                raise ValueError(f"{path}:{line_number}: source locations must be nonempty and unique")
            identity = {
                "query": query,
                "relevant_search_unit_ids": relevant,
                "method": "human_reviewed",
                "generator": GENERATOR,
                "generator_version": GENERATOR_VERSION,
            }
            records.append({
                "schema_version": "0.1",
                "record_type": "retrieval_eval_case",
                "eval_case_id": stable_id("qe", identity),
                "query": query,
                "relevant_search_unit_ids": relevant,
                "category": category,
                "review": review,
                "provenance": {
                    "method": "human_reviewed",
                    "generator": GENERATOR,
                    "generator_version": GENERATOR_VERSION,
                    "deterministic": True,
                },
            })
            seen_queries.add(query)
    if not records:
        raise ValueError("draft evaluation set is empty")
    return records


def finalize(search_output: Path, draft: Path, output: Path) -> dict[str, Any]:
    if output.exists() and any(output.iterdir()):
        raise ValueError(f"output directory is not empty: {output}")
    output.mkdir(parents=True, exist_ok=True)
    units, search_state, search_state_path = load_search_units(search_output)
    records = load_drafts(draft, units)
    eval_path = output / EVAL_FILE
    temporary_eval = output / f".{EVAL_FILE}.tmp"
    with temporary_eval.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(canonical_json(record) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary_eval, eval_path)
    categories = sorted({record["category"] for record in records})
    state = {
        "state_version": "1",
        "build_status": "complete",
        "generator": GENERATOR,
        "generator_version": GENERATOR_VERSION,
        "method": "human_reviewed",
        "deterministic": True,
        "source": {
            "search_units_sha256": search_state["output"]["sha256"],
            "search_state_sha256": digest_file(search_state_path),
            "reviewed_draft_sha256": digest_file(draft),
        },
        "output": {
            "relative_path": EVAL_FILE,
            "sha256": digest_file(eval_path),
            "size_bytes": eval_path.stat().st_size,
            "record_count": len(records),
        },
        "counts_by_category": {
            category: sum(record["category"] == category for record in records)
            for category in categories
        },
    }
    temporary_state = output / f".{STATE_FILE}.tmp"
    with temporary_state.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(canonical_json(state) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary_state, output / STATE_FILE)
    return state


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--search-output", required=True, type=Path)
    parser.add_argument("--draft", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    print(canonical_json(finalize(args.search_output.resolve(), args.draft.resolve(), args.out.resolve())))


if __name__ == "__main__":
    main()
