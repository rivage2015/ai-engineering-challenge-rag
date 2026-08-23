#!/usr/bin/env python3
"""Validate and deterministically merge isolated OCR PoC run JSONL files."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import ocr_poc_contract as contract  # noqa: E402


def merge_runs(paths: list[Path]) -> list[dict[str, Any]]:
    if not paths:
        raise ValueError("at least one input run JSONL is required")
    merged: list[dict[str, Any]] = []
    seen_pairs: set[tuple[str, str]] = set()
    for path in paths:
        for position, record in enumerate(contract.load_jsonl(path), 1):
            errors = contract.validate_run(record)
            if errors:
                raise ValueError(
                    f"{path}:{position}: invalid OCR PoC run: " + "; ".join(errors)
                )
            pair = (record["fixture_ref"]["fixture_id"], record["engine"]["name"])
            if pair in seen_pairs:
                raise ValueError(f"duplicate fixture/engine run across inputs: {pair}")
            seen_pairs.add(pair)
            merged.append(record)
    contract.consistent_engine_identities(merged)
    return sorted(
        merged,
        key=lambda record: (
            record["fixture_ref"]["fixture_id"],
            record["engine"]["name"],
        ),
    )


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("--input", action="append", type=Path, required=True)
    value.add_argument("--output", type=Path, required=True)
    value.add_argument("--overwrite", action="store_true")
    return value


def main() -> int:
    args = parser().parse_args()
    if args.output.exists() and not args.overwrite:
        print(
            f"error: output exists; pass --overwrite to replace it: {args.output}",
            file=sys.stderr,
        )
        return 2
    try:
        merged = merge_runs(args.input)
        contract.write_jsonl(args.output, merged)
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    engines = sorted({record["engine"]["name"] for record in merged})
    print(f"wrote {len(merged)} validated OCR PoC runs to {args.output}")
    print("engines: " + ", ".join(engines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
