#!/usr/bin/env python3
"""Prove question-independent reading coverage for every discovered visual asset."""

from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from pathlib import Path
from typing import Any

import build_visual_asset_manifest as manifest_builder
import validate_ocr_observations as ocr_validator
import validate_visual_asset_manifest as manifest_validator
import validate_visual_classifications as classification_validator


def load_jsonl(path: Path, label: str) -> list[dict[str, Any]]:
    if not path.is_file():
        raise ValueError(f"{label} does not exist: {path}")
    records = manifest_builder.read_jsonl(path)
    if not records:
        raise ValueError(f"{label} is empty")
    return records


def native_success_paths(inventory: Path, native_raw: Path) -> set[str]:
    with inventory.open(encoding="utf-8-sig", newline="") as handle:
        successful = {
            row["file_path"]
            for row in csv.DictReader(handle)
            if row.get("extraction_status") == "success"
            and row.get("text_extractable", "").lower() == "true"
        }
    raw_paths = {
        record.get("source_path")
        for record in load_jsonl(native_raw, "native text evidence")
        if isinstance(record.get("source_path"), str)
    }
    return successful & raw_paths


def validate(
    *, manifest: Path, materializable: Path, materialized: Path,
    classifications: Path, observations: Path, inventory: Path,
    native_raw: Path, source_root: Path, asset_root: Path,
) -> dict[str, Any]:
    manifest_validator.validate(
        manifest, inventory, source_root,
        materializable_batch=materializable,
        materialized_full_batch=materialized,
    )
    classification_validator.validate_jsonl(
        classifications, materialized, asset_root=asset_root
    )
    ocr_validator.validate_jsonl(
        observations, materialized, classifications,
        asset_root=asset_root, expected_count=None,
    )

    all_assets = load_jsonl(manifest, "visual manifest")
    direct = load_jsonl(materialized, "materialized visuals")
    classified = load_jsonl(classifications, "visual classifications")
    observed = load_jsonl(observations, "OCR observations")
    direct_ids = [record["asset_id"] for record in direct]
    if [record.get("asset_id") for record in classified] != direct_ids:
        raise ValueError("classification order does not cover every direct visual asset")
    if any(record.get("status") == "failed" for record in classified):
        raise ValueError("one or more visual classifications failed")
    eligible_ids = [
        record["asset_id"] for record in classified
        if "ocr_text" in record.get("routes", [])
    ]
    if [record.get("asset_id") for record in observed] != eligible_ids:
        raise ValueError("OCR order does not exactly cover every ocr_text route")
    if any(record.get("status") == "failed" for record in observed):
        raise ValueError("one or more OCR observations failed")

    native_paths = native_success_paths(inventory, native_raw)
    direct_id_set = set(direct_ids)
    observation_by_id = {record["asset_id"]: record for record in observed}
    status_counts: Counter[str] = Counter()
    uncovered: list[str] = []
    for asset in all_assets:
        asset_id = asset["asset_id"]
        if asset_id in direct_id_set:
            observation = observation_by_id.get(asset_id)
            if observation is None:
                status_counts["classified_visual_no_ocr_route"] += 1
            else:
                status_counts[f"dual_ocr_{observation['status']}"] += 1
        elif (
            asset["origin"]["kind"] == "visual_container"
            and asset["source"]["relative_path"] in native_paths
        ):
            status_counts["native_container_text"] += 1
        else:
            uncovered.append(asset_id)
    if uncovered:
        raise ValueError(f"uncovered visual assets: {uncovered[:10]}")
    if sum(status_counts.values()) != len(all_assets):
        raise ValueError("coverage accounting does not equal manifest size")
    return {
        "records": len(all_assets),
        "direct_visuals": len(direct),
        "native_containers": status_counts["native_container_text"],
        "ocr_routes": len(observed),
        "uncovered": 0,
        "coverage": dict(sorted(status_counts.items())),
        "question_independent": True,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--materializable", required=True, type=Path)
    parser.add_argument("--materialized", required=True, type=Path)
    parser.add_argument("--classifications", required=True, type=Path)
    parser.add_argument("--observations", required=True, type=Path)
    parser.add_argument("--inventory", required=True, type=Path)
    parser.add_argument("--native-raw", required=True, type=Path)
    parser.add_argument("--source-root", required=True, type=Path)
    parser.add_argument("--asset-root", required=True, type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        result = validate(
            manifest=args.manifest, materializable=args.materializable,
            materialized=args.materialized, classifications=args.classifications,
            observations=args.observations, inventory=args.inventory,
            native_raw=args.native_raw, source_root=args.source_root,
            asset_root=args.asset_root,
        )
    except (OSError, ValueError) as exc:
        raise SystemExit(f"error: {exc}") from exc
    encoded = json.dumps(result, ensure_ascii=False, sort_keys=True)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded + "\n", encoding="utf-8")
    print(encoded)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
