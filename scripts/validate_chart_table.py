#!/usr/bin/env python3
"""Validate ChartTable records without third-party dependencies."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
CHART_TABLE_ID_RE = re.compile(r"^ct_[0-9a-f]{16,64}$")


def validate(record: object) -> list[str]:
    errors: list[str] = []
    if not isinstance(record, dict):
        return ["root must be an object"]
    required = {
        "schema_version", "record_type", "chart_table_id", "source", "chart_type",
        "axes", "x_values", "series", "completeness", "provenance",
    }
    missing = sorted(required - set(record))
    if missing:
        errors.append("missing root keys: " + ", ".join(missing))
    if record.get("schema_version") != "0.1":
        errors.append("schema_version must be 0.1")
    if record.get("record_type") != "chart_table":
        errors.append("record_type must be chart_table")
    if not CHART_TABLE_ID_RE.fullmatch(str(record.get("chart_table_id", ""))):
        errors.append("invalid chart_table_id")

    source = record.get("source")
    if not isinstance(source, dict):
        errors.append("source must be an object")
    else:
        if not source.get("chart_path"):
            errors.append("source.chart_path must be non-empty")
        if not SHA256_RE.fullmatch(str(source.get("chart_sha256", ""))):
            errors.append("source.chart_sha256 must be lowercase SHA-256")
        if source.get("source_type") not in {"native_data", "image_derendered", "hybrid"}:
            errors.append("invalid source.source_type")

    axes = record.get("axes")
    axis_ids: set[str] = set()
    if not isinstance(axes, list) or len(axes) < 2:
        errors.append("axes must contain at least two axes")
    else:
        for index, axis in enumerate(axes):
            if not isinstance(axis, dict):
                errors.append(f"axes[{index}] must be an object")
                continue
            axis_id = axis.get("axis_id")
            if not isinstance(axis_id, str) or not axis_id:
                errors.append(f"axes[{index}].axis_id must be non-empty")
            elif axis_id in axis_ids:
                errors.append(f"duplicate axis_id: {axis_id}")
            else:
                axis_ids.add(axis_id)
            if axis.get("exactness") not in {"exact", "estimated", "unresolved"}:
                errors.append(f"axes[{index}].exactness is invalid")

    x_values = record.get("x_values")
    if not isinstance(x_values, list) or not x_values:
        errors.append("x_values must be a non-empty array")

    series = record.get("series")
    if not isinstance(series, list) or not series:
        errors.append("series must be a non-empty array")
        series = []
    series_ids: set[str] = set()
    for index, item in enumerate(series):
        if not isinstance(item, dict):
            errors.append(f"series[{index}] must be an object")
            continue
        series_id = item.get("series_id")
        if not isinstance(series_id, str) or not series_id:
            errors.append(f"series[{index}].series_id must be non-empty")
        elif series_id in series_ids:
            errors.append(f"duplicate series_id: {series_id}")
        else:
            series_ids.add(series_id)
        if item.get("axis_id") not in axis_ids:
            errors.append(f"series[{index}].axis_id does not reference an axis")
        points = item.get("points")
        if not isinstance(points, list) or not points:
            errors.append(f"series[{index}].points must be non-empty")
            continue
        if isinstance(x_values, list) and len(points) != len(x_values):
            errors.append(f"series[{index}] point count differs from x_values")
        for point_index, point in enumerate(points):
            if not isinstance(point, dict):
                errors.append(f"series[{index}].points[{point_index}] must be an object")
                continue
            if point.get("status") not in {"exact", "estimated", "unresolved"}:
                errors.append(f"series[{index}].points[{point_index}].status is invalid")
            if point.get("status") == "unresolved" and point.get("y") is not None:
                errors.append(f"series[{index}].points[{point_index}] unresolved y must be null")

    completeness = record.get("completeness")
    if not isinstance(completeness, dict):
        errors.append("completeness must be an object")
    else:
        if completeness.get("status") not in {"verified", "partial", "unresolved"}:
            errors.append("invalid completeness.status")
        if completeness.get("output_series_count") != len(series):
            errors.append("completeness.output_series_count differs from series length")
        if completeness.get("detected_series_count") != len(series) and completeness.get("status") == "verified":
            errors.append("verified record must contain every detected series")
        if not isinstance(completeness.get("checks"), list):
            errors.append("completeness.checks must be an array")
        if not isinstance(completeness.get("warnings"), list):
            errors.append("completeness.warnings must be an array")

    provenance = record.get("provenance")
    if not isinstance(provenance, dict):
        errors.append("provenance must be an object")
    else:
        if provenance.get("question_independent") is not True:
            errors.append("provenance.question_independent must be true")
        if provenance.get("method") not in {"static_source_recovery", "visual_derendering", "hybrid_consensus"}:
            errors.append("invalid provenance.method")
        if not SHA256_RE.fullmatch(str(provenance.get("code_sha256", ""))):
            errors.append("provenance.code_sha256 must be lowercase SHA-256")
        data_paths = provenance.get("data_paths")
        data_hashes = provenance.get("data_sha256")
        if not isinstance(data_paths, list) or not data_paths:
            errors.append("provenance.data_paths must be non-empty")
        if not isinstance(data_hashes, list) or not data_hashes:
            errors.append("provenance.data_sha256 must be non-empty")
        elif any(not SHA256_RE.fullmatch(str(value)) for value in data_hashes):
            errors.append("provenance.data_sha256 contains an invalid hash")
        if isinstance(data_paths, list) and isinstance(data_hashes, list) and len(data_paths) != len(data_hashes):
            errors.append("provenance data path/hash counts differ")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("chart_table", type=Path)
    args = parser.parse_args()
    try:
        record = json.loads(args.chart_table.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}")
        return 1
    errors = validate(record)
    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        print(f"validation failed: {len(errors)} error(s)")
        return 1
    print(f"validation passed: {args.chart_table}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
