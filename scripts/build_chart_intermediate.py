#!/usr/bin/env python3
"""Adapt verified ChartTable records into Document/Evidence/Relation shards."""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import unicodedata
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from probe_intermediate_records import canonical_json, content, digest_file, nfc_path, stable_id
from validate_chart_table import validate as validate_chart_table


ADAPTER = "chart-table-intermediate-adapter"
ADAPTER_VERSION = "0.1.0"
STATE_FILE = "build-state.json"
RECORD_FILES = {
    "documents": "documents.jsonl",
    "evidence": "evidence.jsonl",
    "relations": "relations.jsonl",
}


def normalized(value: str | Path) -> str:
    return unicodedata.normalize("NFC", str(value))


def write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(canonical_json(record) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    write_jsonl(path, [value])


def resolve_chart_path(root: Path, source: dict[str, Any]) -> Path:
    requested = normalized(source["chart_path"])
    requested_name = Path(requested).name
    expected_sha = source["chart_sha256"]
    matches: list[Path] = []
    for candidate in root.rglob(requested_name):
        if candidate.is_file() and digest_file(candidate) == expected_sha:
            matches.append(candidate.resolve())
    exact = [candidate for candidate in matches if normalized(candidate) == requested]
    if len(exact) == 1:
        return exact[0]
    if len(matches) == 1:
        return matches[0]
    raise ValueError(
        f"ChartTable source resolves to {len(matches)} hash-matching files under {root}: {source['chart_path']}"
    )


def evidence(
    document_id: str,
    evidence_type: str,
    location: dict[str, Any],
    raw_text: str,
    run_at: str,
    confidence: float,
    *,
    parent_evidence_id: str | None = None,
    ordinal: int | None = None,
    native_properties: dict[str, Any] | None = None,
) -> dict[str, Any]:
    item_content = content(raw_text=raw_text)
    evidence_id = stable_id("ev", {
        "document_id": document_id,
        "evidence_type": evidence_type,
        "location": location,
        "content_sha256": item_content["sha256"],
    })
    record: dict[str, Any] = {
        "schema_version": "0.1",
        "record_type": "evidence",
        "evidence_id": evidence_id,
        "document_id": document_id,
        "evidence_type": evidence_type,
        "location": location,
        "content": item_content,
        "native_properties": native_properties or {},
        "provenance": {
            "extraction_method": "verified_chart_table_adaptation",
            "extractor": ADAPTER,
            "extractor_version": ADAPTER_VERSION,
            "extracted_at": run_at,
            "deterministic": True,
            "confidence": confidence,
            "warnings": [],
        },
    }
    if parent_evidence_id:
        record["parent_evidence_id"] = parent_evidence_id
    if ordinal is not None:
        record["ordinal"] = ordinal
    return record


def relation(
    from_ref: dict[str, str],
    to_ref: dict[str, str],
    run_at: str,
    *,
    relation_type: str = "contains",
) -> dict[str, Any]:
    identity = {
        "class": "structural",
        "type": relation_type,
        "from": from_ref,
        "to": to_ref,
        "generator": ADAPTER,
        "generator_version": ADAPTER_VERSION,
    }
    return {
        "schema_version": "0.1",
        "record_type": "relation",
        "relation_id": stable_id("rel", identity),
        "relation_class": "structural",
        "relation_type": relation_type,
        "from_ref": from_ref,
        "to_ref": to_ref,
        "properties": {},
        "supporting_evidence_ids": [],
        "provenance": {
            "generated_by": ADAPTER,
            "generator_version": ADAPTER_VERSION,
            "generated_at": run_at,
            "deterministic": True,
            "confidence": 1.0,
            "rule_or_model": "ChartTable containment",
            "warnings": [],
        },
        "status": "verified",
    }


def numeric_summary(points: list[dict[str, Any]]) -> dict[str, Any]:
    resolved = [(point["x"], point["y"]) for point in points if isinstance(point.get("y"), (int, float))]
    if not resolved:
        return {"resolved_point_count": 0}
    minimum = min(value for _, value in resolved)
    maximum = max(value for _, value in resolved)
    return {
        "resolved_point_count": len(resolved),
        "minimum": minimum,
        "minimum_x": [x for x, value in resolved if value == minimum],
        "maximum": maximum,
        "maximum_x": [x for x, value in resolved if value == maximum],
    }


def chart_summary_text(record: dict[str, Any]) -> str:
    axes = {axis["axis_id"]: axis for axis in record["axes"]}
    lines = [
        f"グラフ: {record.get('title') or record['chart_table_id']}",
        f"グラフ種別: {record['chart_type']}",
        f"完全性: {record['completeness']['status']}",
        "軸: " + "; ".join(
            f"{axis['orientation']}={axis.get('label') or axis['axis_id']} ({axis['exactness']})"
            for axis in record["axes"]
        ),
        "系列:",
    ]
    for series in record["series"]:
        stats = numeric_summary(series["points"])
        axis = axes[series["axis_id"]]
        lines.append(
            f"- {series['label']}: y軸={axis.get('label') or axis['axis_id']}, "
            f"点数={len(series['points'])}, 最小={stats.get('minimum')}, 最大={stats.get('maximum')}"
        )
    return "\n".join(lines)


def chart_series_text(record: dict[str, Any], series: dict[str, Any]) -> str:
    axes = {axis["axis_id"]: axis for axis in record["axes"]}
    x_axis = next(axis for axis in record["axes"] if axis["orientation"] == "x")
    y_axis = axes[series["axis_id"]]
    stats = numeric_summary(series["points"])
    lines = [
        f"グラフ: {record.get('title') or record['chart_table_id']}",
        f"系列: {series['label']}",
        f"x軸: {x_axis.get('label') or x_axis['axis_id']}",
        f"y軸: {y_axis.get('label') or y_axis['axis_id']}",
        f"点数: {len(series['points'])}",
        f"最小値: {stats.get('minimum')} (x={stats.get('minimum_x', [])})",
        f"最大値: {stats.get('maximum')} (x={stats.get('maximum_x', [])})",
        "値:",
    ]
    for point in series["points"]:
        lines.append(f"{x_axis.get('label') or 'x'}={point['x']}, {series['label']}={point.get('y')} ({point['status']})")
    return "\n".join(lines)


def chart_confidence(record: dict[str, Any]) -> float:
    statuses = [axis["exactness"] for axis in record["axes"]]
    statuses.extend(
        point["status"]
        for series in record["series"]
        for point in series["points"]
    )
    if record["completeness"]["status"] == "verified" and all(item == "exact" for item in statuses):
        return 1.0
    if record["completeness"]["status"] == "unresolved" or "unresolved" in statuses:
        return 0.5
    return 0.8


def build(root: Path, chart_tables: list[Path], output: Path, run_at: str) -> dict[str, Any]:
    root = root.resolve()
    chart_tables = [path.resolve() for path in chart_tables]
    output = output.resolve()
    if output.exists() and any(output.iterdir()):
        raise ValueError(f"output directory is not empty: {output}")
    output.mkdir(parents=True, exist_ok=True)
    shard_directory = output / "shards"
    shard_directory.mkdir()
    grouped: dict[Path, list[tuple[Path, dict[str, Any]]]] = defaultdict(list)
    for table_path in chart_tables:
        record = json.loads(table_path.read_text(encoding="utf-8"))
        errors = validate_chart_table(record)
        if errors:
            raise ValueError(f"{table_path}: invalid ChartTable:\n- " + "\n- ".join(errors))
        source_path = resolve_chart_path(root, record["source"])
        grouped[source_path].append((table_path.resolve(), record))
    all_documents: list[dict[str, Any]] = []
    all_evidence: list[dict[str, Any]] = []
    all_relations: list[dict[str, Any]] = []
    entries: dict[str, Any] = {}
    for source_path in sorted(grouped, key=lambda item: normalized(item.relative_to(root))):
        relative_path = normalized(source_path.relative_to(root))
        source_sha = digest_file(source_path)
        document_id = stable_id("doc", {"relative_path": relative_path, "source_sha256": source_sha})
        stat = source_path.stat()
        document = {
            "schema_version": "0.1",
            "record_type": "document",
            "document_id": document_id,
            "source": {
                "relative_path": relative_path,
                "file_name": normalized(source_path.name),
                "extension": source_path.suffix.lower().lstrip("."),
                "media_type": mimetypes.guess_type(source_path.name)[0] or "application/octet-stream",
                "size_bytes": stat.st_size,
                "sha256": source_sha,
                "modified_at": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
            },
            "extraction": {
                "status": "success",
                "parser": ADAPTER,
                "parser_version": ADAPTER_VERSION,
                "extracted_at": run_at,
                "warnings": [],
                "errors": [],
            },
        }
        document_evidence: list[dict[str, Any]] = []
        document_relations: list[dict[str, Any]] = []
        for chart_index, (table_path, record) in enumerate(
            sorted(grouped[source_path], key=lambda item: item[1]["chart_table_id"]),
            1,
        ):
            table_sha = digest_file(table_path)
            confidence = chart_confidence(record)
            chart_record = evidence(
                document_id,
                "chart",
                {"object_index": chart_index, "locator_text": f"chart_table_id={record['chart_table_id']}"},
                chart_summary_text(record),
                run_at,
                confidence,
                ordinal=chart_index,
                native_properties={
                    "chart_table_id": record["chart_table_id"],
                    "chart_table_sha256": table_sha,
                    "chart_type": record["chart_type"],
                    "title": record.get("title"),
                    "axes": record["axes"],
                    "completeness": record["completeness"],
                    "source_type": record["source"]["source_type"],
                    "code_sha256": record["provenance"]["code_sha256"],
                    "data_sha256": record["provenance"]["data_sha256"],
                },
            )
            document_evidence.append(chart_record)
            document_relations.append(relation(
                {"record_type": "document", "record_id": document_id},
                {"record_type": "evidence", "record_id": chart_record["evidence_id"]},
                run_at,
            ))
            for series_index, series in enumerate(record["series"], 1):
                series_record = evidence(
                    document_id,
                    "chart_series",
                    {
                        "object_index": chart_index,
                        "series_index": series_index,
                        "locator_text": f"chart_table_id={record['chart_table_id']};series_id={series['series_id']}",
                    },
                    chart_series_text(record, series),
                    run_at,
                    confidence,
                    parent_evidence_id=chart_record["evidence_id"],
                    ordinal=series_index,
                    native_properties={
                        "chart_table_id": record["chart_table_id"],
                        "chart_table_sha256": table_sha,
                        "series": series,
                        "statistics": numeric_summary(series["points"]),
                    },
                )
                document_evidence.append(series_record)
                document_relations.append(relation(
                    {"record_type": "evidence", "record_id": chart_record["evidence_id"]},
                    {"record_type": "evidence", "record_id": series_record["evidence_id"]},
                    run_at,
                ))
        shard_records = {
            "documents": [document],
            "evidence": document_evidence,
            "relations": document_relations,
        }
        shards: dict[str, Any] = {}
        for kind, records in shard_records.items():
            path = shard_directory / f"{document_id}.{kind}.jsonl"
            write_jsonl(path, records)
            shards[kind] = {
                "relative_path": nfc_path(path.relative_to(output)),
                "sha256": digest_file(path),
                "size_bytes": path.stat().st_size,
                "record_count": len(records),
            }
        entries[relative_path] = {
            "document_id": document_id,
            "relative_path": relative_path,
            "source_sha256": source_sha,
            "status": "success",
            "shards": shards,
        }
        all_documents.append(document)
        all_evidence.extend(document_evidence)
        all_relations.extend(document_relations)
    for kind, records in (
        ("documents", all_documents),
        ("evidence", all_evidence),
        ("relations", all_relations),
    ):
        write_jsonl(output / RECORD_FILES[kind], records)
    state = {
        "state_version": "1",
        "build_status": "complete",
        "source_root": nfc_path(root),
        "extractor": ADAPTER,
        "extractor_version": ADAPTER_VERSION,
        "run_at": run_at,
        "input_paths": sorted(entries),
        "chart_table_sources": [
            {"path": normalized(path.resolve()), "sha256": digest_file(path)}
            for path in sorted(chart_tables, key=lambda item: normalized(item.resolve()))
        ],
        "entries": entries,
        "totals": {
            "documents": len(all_documents),
            "evidence": len(all_evidence),
            "relations": len(all_relations),
        },
    }
    atomic_json(output / STATE_FILE, state)
    return state


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--chart-table", required=True, type=Path, nargs="+")
    parser.add_argument("--out", required=True, type=Path)
    timing = parser.add_mutually_exclusive_group(required=True)
    timing.add_argument("--run-at", help="ISO 8601 timestamp used by a standalone chart intermediate")
    timing.add_argument(
        "--base-intermediate",
        type=Path,
        help="reuse the run_at timestamp from a complete base intermediate for deterministic merging",
    )
    args = parser.parse_args()
    run_at = args.run_at
    if args.base_intermediate:
        base_state_path = args.base_intermediate.resolve() / STATE_FILE
        base_state = json.loads(base_state_path.read_text(encoding="utf-8"))
        if base_state.get("build_status") != "complete" or not base_state.get("run_at"):
            raise ValueError("base intermediate must be complete and contain run_at")
        run_at = base_state["run_at"]
    if run_at is None:
        raise ValueError("run_at could not be resolved")
    result = build(
        args.root.resolve(),
        [path.resolve() for path in args.chart_table],
        args.out.resolve(),
        run_at,
    )
    print(canonical_json({
        "build_status": result["build_status"],
        "extractor": result["extractor"],
        "extractor_version": result["extractor_version"],
        "totals": result["totals"],
        "output": str(args.out.resolve()),
    }))


if __name__ == "__main__":
    main()
