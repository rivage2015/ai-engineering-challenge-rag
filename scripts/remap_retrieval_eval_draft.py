#!/usr/bin/env python3
"""Remap reviewed evaluation targets across SearchUnit schema versions."""

from __future__ import annotations

import argparse
import json
import os
from collections import defaultdict
from pathlib import Path
from typing import Any

from lexical_search_common import canonical_json


def load_units(directory: Path) -> dict[str, dict[str, Any]]:
    units: dict[str, dict[str, Any]] = {}
    with (directory / "search_units.jsonl").open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                unit = json.loads(line)
                units[unit["search_unit_id"]] = unit
    return units


def signature(unit: dict[str, Any]) -> str:
    return canonical_json({
        "document_id": unit["document_id"],
        "unit_type": unit["unit_type"],
        "locator": unit["locator"],
    })


def remap(old_search: Path, new_search: Path, evaluation_set: Path, output: Path) -> dict[str, int]:
    if output.exists():
        raise ValueError(f"output already exists: {output}")
    old_units = load_units(old_search)
    new_units = load_units(new_search)
    new_by_signature: dict[str, list[str]] = defaultdict(list)
    for search_unit_id, unit in new_units.items():
        new_by_signature[signature(unit)].append(search_unit_id)
    drafts: list[dict[str, Any]] = []
    changed_targets = 0
    with evaluation_set.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            case = json.loads(line)
            mapped: list[str] = []
            for old_id in case["relevant_search_unit_ids"]:
                old_unit = old_units.get(old_id)
                if old_unit is None:
                    raise ValueError(f"{evaluation_set}:{line_number}: old target is missing: {old_id}")
                candidates = new_by_signature.get(signature(old_unit), [])
                if len(candidates) != 1:
                    raise ValueError(
                        f"{evaluation_set}:{line_number}: target signature maps to {len(candidates)} new units: {old_id}"
                    )
                mapped.append(candidates[0])
                changed_targets += candidates[0] != old_id
            if len(mapped) != len(set(mapped)):
                raise ValueError(f"{evaluation_set}:{line_number}: remapping collapsed distinct targets")
            drafts.append({
                "query": case["query"],
                "relevant_search_unit_ids": mapped,
                "category": case["category"],
                "review": case["review"],
            })
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        for draft in drafts:
            handle.write(canonical_json(draft) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, output)
    return {"cases": len(drafts), "changed_targets": changed_targets}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--old-search-output", required=True, type=Path)
    parser.add_argument("--new-search-output", required=True, type=Path)
    parser.add_argument("--evaluation-set", required=True, type=Path)
    parser.add_argument("--out-draft", required=True, type=Path)
    args = parser.parse_args()
    print(canonical_json(remap(
        args.old_search_output.resolve(),
        args.new_search_output.resolve(),
        args.evaluation_set.resolve(),
        args.out_draft.resolve(),
    )))


if __name__ == "__main__":
    main()
