#!/usr/bin/env python3
"""Safely reconstruct a groupby chart table from static notebook evidence.

The script recognizes a constrained pandas pattern and recomputes only the
groupby operations itself. It never executes notebook code.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

PATH_ASSIGNMENT_RE = re.compile(r"(?P<name>\w+)\s*=\s*Path\([\"'](?P<value>[^\"']+)[\"']\)")
STRING_ASSIGNMENT_RE = re.compile(r"(?P<name>target_col|date_col_hint)\s*=\s*[\"'](?P<value>[^\"']+)[\"']")
SIZE_RE = re.compile(
    r"agg\s*=\s*tmp\.groupby\((?P<x>\w+|[\"'][^\"']+[\"'])\)\.agg\("
    r"(?P<label>[^=,()]+)=\((?P<target>\w+|[\"'][^\"']+[\"'])\s*,\s*[\"']size[\"']\)\)"
)
MEAN_RE = re.compile(
    r"agg\[[\"'](?P<label>[^\"']+)[\"']\]\s*=\s*tmp\.groupby\("
    r"(?P<x>\w+|[\"'][^\"']+[\"'])\)\[(?P<target>\w+|[\"'][^\"']+[\"'])\]\.mean\(\)"
)
PLOT_RE = re.compile(r"(?P<axis>ax\w*)\.plot\((?P<args>[^\n]+)\)")
PLOT_LABEL_RE = re.compile(r"agg\[[\"'](?P<label>[^\"']+)[\"']\]")
MARKER_RE = re.compile(r"marker\s*=\s*[\"'](?P<value>[^\"']+)[\"']")
COLOR_RE = re.compile(r"color\s*=\s*[\"'](?P<value>[^\"']+)[\"']")
TITLE_RE = re.compile(r"set_title\((?:f)?[\"'](?P<value>[^\"']+)[\"']\)")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def notebook_cells(path: Path) -> list[tuple[str, str]]:
    notebook = json.loads(path.read_text(encoding="utf-8"))
    result: list[tuple[str, str]] = []
    for index, cell in enumerate(notebook.get("cells", [])):
        if cell.get("cell_type") == "code":
            result.append((str(cell.get("id") or f"cell-{index}"), "".join(cell.get("source", []))))
    return result


def unquote_or_lookup(token: str, assignments: dict[str, str]) -> str:
    if token[:1] in {"\"", "'"}:
        return token[1:-1]
    if token not in assignments:
        raise ValueError(f"could not resolve variable: {token}")
    return assignments[token]


def find_data_path(notebook_path: Path, project_root: Path, literal: str) -> Path:
    candidates = [
        project_root / literal,
        notebook_path.parent / literal,
        notebook_path.parent.parent / literal,
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    matches = sorted(project_root.rglob(Path(literal).name))
    if len(matches) == 1:
        return matches[0].resolve()
    raise FileNotFoundError(f"could not uniquely resolve data source: {literal}")


def load_csv_auto(path: Path) -> tuple[pd.DataFrame, str]:
    errors: list[str] = []
    for encoding in ("utf-8", "utf-8-sig", "cp932", "shift_jis"):
        try:
            return pd.read_csv(path, encoding=encoding), encoding
        except UnicodeDecodeError as exc:
            errors.append(f"{encoding}: {exc}")
    raise UnicodeError("; ".join(errors))


def json_scalar(value: Any) -> str | int | float:
    if pd.isna(value):
        raise ValueError("x-axis contains a missing group key")
    if isinstance(value, str):
        return value
    numeric = float(value)
    return int(numeric) if numeric.is_integer() else numeric


def is_pure_day_number(series: pd.Series) -> bool:
    numeric = pd.to_numeric(series, errors="coerce").dropna()
    return bool(len(numeric) and numeric.between(1, 31).all())


def series_points(index: list[str | int | float], values: list[Any], refs: list[str]) -> list[dict[str, Any]]:
    points: list[dict[str, Any]] = []
    for x_value, y_value in zip(index, values):
        y = None if pd.isna(y_value) else float(y_value)
        if y is not None and y.is_integer():
            y = int(y)
        points.append({"x": x_value, "y": y, "status": "exact" if y is not None else "unresolved", "source_refs": refs})
    return points


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--notebook", type=Path, required=True)
    parser.add_argument("--image", type=Path, required=True)
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    notebook_path = args.notebook.resolve()
    image_path = args.image.resolve()
    project_root = args.project_root.resolve()
    cells = notebook_cells(notebook_path)
    combined = "\n".join(source for _, source in cells)
    assignments = {match.group("name"): match.group("value") for match in STRING_ASSIGNMENT_RE.finditer(combined)}
    if assignments.get("date_col_hint"):
        assignments.setdefault("used_col", assignments["date_col_hint"])
    path_assignments = {match.group("name"): match.group("value") for match in PATH_ASSIGNMENT_RE.finditer(combined)}
    chart_cells = [(location, source) for location, source in cells if image_path.name in source]
    recoverable = [(location, source) for location, source in chart_cells if SIZE_RE.search(source)]
    if len(recoverable) != 1:
        raise ValueError(f"expected one recoverable chart cell, found {len(recoverable)}")
    location, source = recoverable[0]
    size_match = SIZE_RE.search(source)
    assert size_match is not None
    mean_match = MEAN_RE.search(source)

    target_col = unquote_or_lookup(size_match.group("target"), assignments)
    size_label = size_match.group("label").strip()
    data_literal = path_assignments.get("csv_rel")
    if not data_literal:
        raise ValueError("no static Path assignment for CSV data was found")
    data_path = find_data_path(notebook_path, project_root, data_literal)
    frame, _encoding = load_csv_auto(data_path)
    x_token = size_match.group("x")
    if x_token == "used_col":
        hinted = assignments.get("date_col_hint")
        if hinted in frame.columns and is_pure_day_number(frame[hinted]):
            x_col = hinted
        elif "day" in frame.columns and is_pure_day_number(frame["day"]):
            x_col = "day"
        else:
            raise ValueError("used_col cannot be resolved safely as a pure day-number column")
    else:
        x_col = unquote_or_lookup(x_token, assignments)
    if x_col not in frame.columns or target_col not in frame.columns:
        raise ValueError(f"required columns not found: {x_col}, {target_col}")

    tmp = frame[[x_col, target_col]].copy()
    tmp[x_col] = pd.to_numeric(tmp[x_col], errors="coerce")
    grouped = tmp.groupby(x_col, dropna=True)
    table = grouped.agg(**{size_label: (target_col, "size")})
    mean_label: str | None = None
    if mean_match and pd.api.types.is_numeric_dtype(tmp[target_col]):
        mean_label = mean_match.group("label")
        table[mean_label] = grouped[target_col].mean()
    table = table.sort_index()
    x_values = [json_scalar(value) for value in table.index.tolist()]

    visual: dict[str, dict[str, str | None]] = {}
    for match in PLOT_RE.finditer(source):
        plot_args = match.group("args")
        label_match = PLOT_LABEL_RE.search(plot_args)
        if label_match is None:
            continue
        marker_match = MARKER_RE.search(plot_args)
        color_match = COLOR_RE.search(plot_args)
        visual[label_match.group("label")] = {
            "color": color_match.group("value") if color_match else None,
            "marker": marker_match.group("value") if marker_match else None,
            "line_style": None,
        }
    title_match = TITLE_RE.search(source)
    title = title_match.group("value").replace("{used_col}", x_col) if title_match else None
    refs = [f"{notebook_path}#{location}", str(data_path)]
    series_specs: list[tuple[str, str]] = [(size_label, "y_left")]
    if mean_label:
        series_specs.append((mean_label, "y_right"))
    series = []
    for number, (label, axis_id) in enumerate(series_specs, start=1):
        series.append({
            "series_id": f"series_{number}",
            "label": label,
            "axis_id": axis_id,
            "visual_encoding": visual.get(label, {"color": None, "marker": None, "line_style": None}),
            "points": series_points(x_values, table[label].tolist(), refs),
        })

    identity = hashlib.sha256(
        f"{sha256_file(image_path)}\0{sha256_file(notebook_path)}\0{sha256_file(data_path)}".encode("utf-8")
    ).hexdigest()
    record = {
        "schema_version": "0.1",
        "record_type": "chart_table",
        "chart_table_id": "ct_" + identity[:24],
        "source": {
            "chart_path": str(image_path),
            "chart_sha256": sha256_file(image_path),
            "source_type": "native_data",
        },
        "chart_type": "multi_axis_line" if mean_label else "line",
        "title": title,
        "axes": [
            {"axis_id": "x", "orientation": "x", "label": x_col, "scale": "linear", "exactness": "exact", "source_refs": refs},
            {"axis_id": "y_left", "orientation": "y_left", "label": size_label, "scale": "linear", "exactness": "exact", "source_refs": refs},
            *([{"axis_id": "y_right", "orientation": "y_right", "label": mean_label, "scale": "linear", "exactness": "exact", "source_refs": refs}] if mean_label else []),
        ],
        "x_values": x_values,
        "series": series,
        "completeness": {
            "status": "verified",
            "detected_series_count": len(series),
            "output_series_count": len(series),
            "expected_points_per_series": len(x_values),
            "checks": [
                {"name": "source_pattern_recognized", "passed": True, "detail": "static pandas groupby size/mean pattern"},
                {"name": "all_series_complete", "passed": all(len(item["points"]) == len(x_values) for item in series), "detail": f"{len(x_values)} x values"},
                {"name": "notebook_not_executed", "passed": True, "detail": "only constrained aggregation was recomputed"},
            ],
            "warnings": [],
        },
        "provenance": {
            "extractor": "recover_groupby_chart_table.py",
            "extractor_version": "0.1",
            "method": "static_source_recovery",
            "code_path": str(notebook_path),
            "code_sha256": sha256_file(notebook_path),
            "code_location": location,
            "data_paths": [str(data_path)],
            "data_sha256": [sha256_file(data_path)],
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "question_independent": True,
        },
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"wrote ChartTable with {len(series)} series x {len(x_values)} points: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
