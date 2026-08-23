#!/usr/bin/env python3
"""Build verified OCR region fixtures from human-reviewed selection records."""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import ocr_poc_contract as contract  # noqa: E402


SELECTION_KEYS = {"asset_id", "crop", "strata", "reference"}


def build_fixture(
    observation: dict[str, Any], selection: dict[str, Any], *, created_at: str
) -> dict[str, Any]:
    if set(selection) != SELECTION_KEYS:
        raise ValueError(
            "selection keys must be exactly: " + ", ".join(sorted(SELECTION_KEYS))
        )
    asset_path = Path(observation["asset"]["materialized_path"])
    try:
        relative_image = asset_path.resolve(strict=True).relative_to(ROOT.resolve(strict=True))
    except (OSError, ValueError) as exc:
        raise ValueError("selected materialized image is outside repository root") from exc
    record: dict[str, Any] = {
        "schema_version": "0.1",
        "record_type": "ocr_poc_fixture",
        "fixture_id": "ocrfx_" + "0" * 24,
        "asset_ref": {
            "asset_id": observation["asset_id"],
            "materialized_path": relative_image.as_posix(),
            "image_sha256": observation["asset"]["sha256"],
            "dimensions": observation["asset"]["dimensions"],
            "source_relative_path": observation["source"]["relative_path"],
            "source_sha256": observation["source"]["sha256"],
            "origin_kind": observation["origin"]["kind"],
            "page_number": observation["origin"].get("page_number"),
        },
        "crop": selection["crop"],
        "strata": selection["strata"],
        "reference": selection["reference"],
        "hashes": {"signature_sha256": "0" * 64},
        "provenance": {
            "created_at": created_at,
            "selection_method": "human-stratified-region-v0.1",
            "question_independent": True,
            "question_data_used": False,
            "answer_data_used": False,
            "prediction_data_used": False,
            "source_data_used": True,
        },
    }
    signature = contract.expected_fixture_signature(record)
    record["hashes"]["signature_sha256"] = signature
    record["fixture_id"] = contract.expected_fixture_id(signature)
    errors = contract.validate_fixture(record, repository_root=ROOT)
    if errors:
        raise ValueError("generated fixture is invalid: " + "; ".join(errors))
    return record


def build_manifest(
    observations_path: Path,
    selections_path: Path,
    output_path: Path,
    *,
    created_at: str,
    overwrite: bool,
) -> list[dict[str, Any]]:
    if output_path.exists() and not overwrite:
        raise ValueError(f"output exists; pass --overwrite: {output_path}")
    observations = contract.load_jsonl(observations_path)
    by_asset = {record.get("asset_id"): record for record in observations}
    if len(by_asset) != len(observations) or None in by_asset:
        raise ValueError("observations contain missing or duplicate asset_id values")
    selections = contract.load_jsonl(selections_path)
    fixtures: list[dict[str, Any]] = []
    seen: set[str] = set()
    for position, selection in enumerate(selections, 1):
        asset_id = selection.get("asset_id")
        observation = by_asset.get(asset_id)
        if observation is None:
            raise ValueError(f"selection {position} references unknown asset_id: {asset_id}")
        fixture = build_fixture(observation, selection, created_at=created_at)
        if fixture["fixture_id"] in seen:
            raise ValueError(f"selection {position} duplicates a fixture")
        seen.add(fixture["fixture_id"])
        fixtures.append(fixture)
    contract.write_jsonl(output_path, fixtures)
    return fixtures


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("--observations", type=Path, required=True)
    value.add_argument("--selections", type=Path, required=True)
    value.add_argument("--output", type=Path, required=True)
    value.add_argument(
        "--created-at",
        default=dt.datetime.now(dt.timezone.utc).isoformat(),
        help="ISO-8601 audit timestamp; excluded from fixture identity",
    )
    value.add_argument("--overwrite", action="store_true")
    return value


def main() -> int:
    args = parser().parse_args()
    try:
        fixtures = build_manifest(
            args.observations,
            args.selections,
            args.output,
            created_at=args.created_at,
            overwrite=args.overwrite,
        )
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"wrote {len(fixtures)} OCR PoC fixtures to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
