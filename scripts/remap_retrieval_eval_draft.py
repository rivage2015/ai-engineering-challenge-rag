#!/usr/bin/env python3
"""Remap reviewed evaluation targets across SearchUnit schema versions."""

from __future__ import annotations

import argparse
import json
import os
from collections import defaultdict
from pathlib import Path
from typing import Any

from lexical_search_common import canonical_json, digest_file


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


def load_id_map(path: Path, evaluation_set: Path, new_search: Path) -> dict[str, dict[str, Any]]:
    config = json.loads(path.read_text(encoding="utf-8"))
    allowed = {
        "schema_version",
        "source_evaluation_set_sha256",
        "target_search_units_sha256",
        "mappings",
    }
    if set(config) != allowed or config["schema_version"] != "0.1":
        raise ValueError(f"{path}: invalid ID-map schema")
    if config["source_evaluation_set_sha256"] != digest_file(evaluation_set):
        raise ValueError(f"{path}: evaluation-set hash does not match")
    units_path = new_search / "search_units.jsonl"
    if config["target_search_units_sha256"] != digest_file(units_path):
        raise ValueError(f"{path}: target SearchUnit hash does not match")
    mappings: dict[str, dict[str, Any]] = {}
    required_mapping_fields = {
        "old_search_unit_id",
        "new_search_unit_id",
        "expected_unit_type",
        "expected_locator",
        "expected_text_sha256",
        "review_source_locations",
    }
    for index, mapping in enumerate(config["mappings"]):
        if set(mapping) != required_mapping_fields:
            raise ValueError(f"{path}: mapping {index} has invalid fields")
        old_id = mapping["old_search_unit_id"]
        if old_id in mappings:
            raise ValueError(f"{path}: duplicate old SearchUnit ID {old_id}")
        locations = mapping["review_source_locations"]
        if not locations or len(locations) != len(set(locations)):
            raise ValueError(f"{path}: mapping {index} has invalid review locations")
        mappings[old_id] = mapping
    if not mappings:
        raise ValueError(f"{path}: ID map is empty")
    return mappings


def load_selected_units(directory: Path, selected_ids: set[str]) -> dict[str, dict[str, Any]]:
    units: dict[str, dict[str, Any]] = {}
    with (directory / "search_units.jsonl").open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            unit = json.loads(line)
            search_unit_id = unit["search_unit_id"]
            if search_unit_id in selected_ids:
                units[search_unit_id] = unit
    missing = sorted(selected_ids - set(units))
    if missing:
        raise ValueError(f"target SearchUnits are missing: {missing}")
    return units


def remap_by_id_map(
    new_search: Path,
    evaluation_set: Path,
    id_map_path: Path,
    output: Path,
) -> dict[str, int]:
    if output.exists():
        raise ValueError(f"output already exists: {output}")
    mappings = load_id_map(id_map_path, evaluation_set, new_search)
    cases: list[dict[str, Any]] = []
    selected_ids: set[str] = {mapping["new_search_unit_id"] for mapping in mappings.values()}
    with evaluation_set.open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            case = json.loads(line)
            cases.append(case)
            selected_ids.update(
                mappings.get(search_unit_id, {}).get("new_search_unit_id", search_unit_id)
                for search_unit_id in case["relevant_search_unit_ids"]
            )
    units = load_selected_units(new_search, selected_ids)
    for old_id, mapping in mappings.items():
        new_id = mapping["new_search_unit_id"]
        unit = units[new_id]
        if unit["unit_type"] != mapping["expected_unit_type"]:
            raise ValueError(f"{id_map_path}: unit type changed for {new_id}")
        if unit["locator"] != mapping["expected_locator"]:
            raise ValueError(f"{id_map_path}: locator changed for {new_id}")
        if unit["text"]["sha256"] != mapping["expected_text_sha256"]:
            raise ValueError(f"{id_map_path}: text hash changed for {new_id}")
        if not any(old_id in case["relevant_search_unit_ids"] for case in cases):
            raise ValueError(f"{id_map_path}: mapped old ID is unused: {old_id}")
    drafts: list[dict[str, Any]] = []
    changed_targets = 0
    for case in cases:
        mapped: list[str] = []
        for old_id in case["relevant_search_unit_ids"]:
            mapping = mappings.get(old_id)
            if mapping is None:
                mapped.append(old_id)
                continue
            expected_locations = set(mapping["review_source_locations"])
            actual_locations = set(case["review"]["source_locations"])
            if not expected_locations <= actual_locations:
                raise ValueError(
                    f"{id_map_path}: review locations do not support mapping {old_id}"
                )
            mapped.append(mapping["new_search_unit_id"])
            changed_targets += 1
        if len(mapped) != len(set(mapped)):
            raise ValueError("remapping collapsed distinct targets")
        mismatched = [search_unit_id for search_unit_id in mapped if units[search_unit_id]["unit_type"] != case["category"]]
        if case["category"] in {
            "paragraph_chunk", "table_row", "slide_text", "page_text", "text_chunk",
            "code_chunk", "notebook_cell", "chart_summary", "chart_series",
        } and mismatched:
            raise ValueError(f"category does not match remapped targets: {mismatched}")
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
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--old-search-output", type=Path)
    source.add_argument("--id-map", type=Path)
    parser.add_argument("--new-search-output", required=True, type=Path)
    parser.add_argument("--evaluation-set", required=True, type=Path)
    parser.add_argument("--out-draft", required=True, type=Path)
    args = parser.parse_args()
    if args.id_map:
        result = remap_by_id_map(
            args.new_search_output.resolve(),
            args.evaluation_set.resolve(),
            args.id_map.resolve(),
            args.out_draft.resolve(),
        )
    else:
        result = remap(
            args.old_search_output.resolve(),
            args.new_search_output.resolve(),
            args.evaluation_set.resolve(),
            args.out_draft.resolve(),
        )
    print(canonical_json(result))


if __name__ == "__main__":
    main()
