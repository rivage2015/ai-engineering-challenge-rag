#!/usr/bin/env python3
"""Execute one resolved CatalogResolutionRun over certified SearchUnit rows.

The CLI prints counts and identifiers only.  Source values remain in memory;
it does not create an answer, retrieval hit, or persistent value cache.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from build_data_catalog import (  # noqa: E402
    CatalogContractError,
    Limits,
    _container_for_unit,
    _iter_jsonl,
    _validator,
    assert_safe_path,
)
from build_question_understanding import validate_understanding_run  # noqa: E402
from resolve_catalog import ResolutionError, validate_resolution_record_local  # noqa: E402
from structured_search_units import (  # noqa: E402
    DecodedTableRow,
    StructuredExecution,
    StructuredProfileAccumulator,
    StructuredRowError,
    capabilities_for_profile,
    decode_table_row,
    execute_operation_graph,
)
from validate_catalog_resolution import validate_catalog_resolution  # noqa: E402
from validate_data_catalog import (  # noqa: E402
    _read_entries,
    _read_snapshot,
    validate_data_catalog,
)
from validate_question_clause_ir import canonical_json, load_json_records  # noqa: E402


def _address_key(value: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        value["container_kind"],
        value["container_name"],
        value["container_index"],
        value["source_member"],
    )


def _unit_address_key(unit: Mapping[str, Any]) -> tuple[Any, ...]:
    kind, name, index, member, _ = _container_for_unit(unit)
    return kind, name, index, member


def load_certified_rows(
    entry: Mapping[str, Any],
    search_units_path: str | Path,
    *,
    expected_search_input: Mapping[str, Any] | None = None,
    limits: Limits = Limits(),
) -> tuple[DecodedTableRow, ...]:
    """Recompute the catalog row certificate and return only the bound rows."""

    path = assert_safe_path(
        search_units_path, role="structured search_units", must_exist=True
    )
    iterator, input_stats = _iter_jsonl(
        path,
        role="structured search_units",
        record_type="search_unit",
        validator=_validator("search-unit.schema.json"),
        limits=limits,
    )
    expected_address = _address_key(entry["address"])
    accumulator = StructuredProfileAccumulator()
    rows: list[DecodedTableRow] = []
    seen_ids: set[str] = set()
    for _, unit in iterator:
        if unit["search_unit_id"] in seen_ids:
            raise StructuredRowError("duplicate SearchUnit ID during execution")
        seen_ids.add(unit["search_unit_id"])
        if unit["document_id"] != entry["document_id"]:
            continue
        if _unit_address_key(unit) != expected_address:
            continue
        text = unit["text"]
        if len(text["search_text"]) != text["char_count"]:
            raise StructuredRowError("SearchUnit char_count mismatch")
        if hashlib.sha256(text["search_text"].encode("utf-8")).hexdigest() != text["sha256"]:
            raise StructuredRowError("SearchUnit text SHA-256 mismatch")
        accumulator.observe(unit)
        context = unit.get("context") or {}
        if context.get("is_header_candidate") is True:
            continue
        rows.append(decode_table_row(unit))
    observed_input = input_stats.frozen()
    if expected_search_input is not None and (
        observed_input.record_type != expected_search_input.get("record_type")
        or observed_input.schema_version != expected_search_input.get("schema_version")
        or observed_input.sha256 != expected_search_input.get("sha256")
        or observed_input.record_count != expected_search_input.get("record_count")
    ):
        raise StructuredRowError(
            "SearchUnit stream differs from the catalog snapshot input digest"
        )
    profile = accumulator.finish()
    if profile is None or not rows:
        raise StructuredRowError("catalog entry no longer has a certified row profile")
    expected_fields = [
        (header, profile.data_types[index])
        for index, header in enumerate(profile.headers)
    ]
    actual_fields = [
        (field["surface"], field["data_type"])
        for field in sorted(entry["fields"], key=lambda item: item["ordinal"])
    ]
    if actual_fields != expected_fields:
        raise StructuredRowError("catalog field/type profile differs from SearchUnits")
    if entry["capabilities"] != capabilities_for_profile(profile, lexical=True):
        raise StructuredRowError("catalog capability profile differs from SearchUnits")
    return tuple(rows)


def execute_resolved_query(
    qur: dict[str, Any],
    resolution: dict[str, Any],
    entries: Sequence[dict[str, Any]],
    search_units_path: str | Path,
    *,
    expected_search_input: Mapping[str, Any] | None = None,
    limits: Limits = Limits(),
) -> StructuredExecution:
    """Validate the resolved branch, reload its rows, and execute its DAG."""

    qur_errors = validate_understanding_run(qur)
    if qur_errors:
        raise ResolutionError("invalid QuestionUnderstandingRun: " + "; ".join(qur_errors[:8]))
    resolution_errors = validate_resolution_record_local(resolution)
    if resolution_errors:
        raise ResolutionError("invalid CatalogResolutionRun: " + "; ".join(resolution_errors[:8]))
    if resolution["final_status"] != "resolved":
        raise ResolutionError("structured execution requires a resolved catalog run")
    if resolution["question_understanding_run_id"] != qur["question_understanding_run_id"]:
        raise ResolutionError("resolution does not reference the supplied QUR")
    if len(resolution["branch_resolutions"]) != 1:
        raise ResolutionError("structured execution requires exactly one resolved branch")
    branch_resolution = resolution["branch_resolutions"][0]
    if (
        branch_resolution["status"] != "resolved"
        or len(branch_resolution["candidate_bindings"]) != 1
    ):
        raise ResolutionError("structured execution requires one resolved candidate")
    binding = branch_resolution["candidate_bindings"][0]
    if binding["status"] != "resolved" or any(
        check["status"] != "pass" for check in binding["capability_checks"]
    ):
        raise ResolutionError("catalog binding is not executable")
    entry_matches = [
        entry
        for entry in entries
        if entry["data_catalog_entry_id"] == binding["catalog_entry_ref"]
    ]
    if len(entry_matches) != 1:
        raise ResolutionError("resolved catalog entry is absent or duplicated")
    branches = [
        branch
        for branch in qur["candidate_query_paths"]
        if branch["branch_id"] == branch_resolution["branch_id"]
    ]
    if len(branches) != 1:
        raise ResolutionError("resolved branch is absent or duplicated in the QUR")
    rows = load_certified_rows(
        entry_matches[0],
        search_units_path,
        expected_search_input=expected_search_input,
        limits=limits,
    )
    return execute_operation_graph(branches[0]["candidate_intent"], rows)


def execute_resolved_query_files(
    qur_path: str | Path,
    clause_ir_path: str | Path,
    resolution_path: str | Path,
    entries_path: str | Path,
    snapshot_path: str | Path,
    search_units_path: str | Path,
    *,
    limits: Limits = Limits(),
) -> StructuredExecution:
    qurs = load_json_records(Path(qur_path))
    clauses = load_json_records(Path(clause_ir_path))
    resolutions = load_json_records(Path(resolution_path))
    if len(qurs) != 1 or len(clauses) != 1 or len(resolutions) != 1:
        raise ResolutionError(
            "executor v0.1 accepts exactly one QUR, ClauseIR, and resolution"
        )
    catalog_errors = validate_data_catalog(entries_path, snapshot_path, limits=limits)
    if catalog_errors:
        raise CatalogContractError("invalid Data Catalog: " + "; ".join(catalog_errors[:8]))
    entries, _ = _read_entries(Path(entries_path), limits)
    snapshot = _read_snapshot(Path(snapshot_path), limits)
    if resolutions[0]["data_catalog_snapshot_id"] != snapshot["data_catalog_snapshot_id"]:
        raise ResolutionError("resolution does not reference the supplied catalog snapshot")
    resolution_errors = validate_catalog_resolution(
        resolution_path,
        qur_path=qur_path,
        clause_ir_path=clause_ir_path,
        entries_path=entries_path,
        snapshot_path=snapshot_path,
    )
    if resolution_errors:
        raise ResolutionError(
            "resolution differs from deterministic inputs: "
            + "; ".join(resolution_errors[:8])
        )
    search_inputs = [
        item for item in snapshot["inputs"] if item["record_type"] == "search_unit"
    ]
    if len(search_inputs) != 1:
        raise ResolutionError("catalog snapshot has no unique SearchUnit input")
    return execute_resolved_query(
        qurs[0],
        resolutions[0],
        entries,
        search_units_path,
        expected_search_input=search_inputs[0],
        limits=limits,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--qur", required=True, type=Path)
    parser.add_argument("--clause-ir", required=True, type=Path)
    parser.add_argument("--resolution", required=True, type=Path)
    parser.add_argument("--entries", required=True, type=Path)
    parser.add_argument("--snapshot", required=True, type=Path)
    parser.add_argument("--search-units", required=True, type=Path)
    parser.add_argument("--max-record-bytes", type=int, default=Limits().max_record_bytes)
    parser.add_argument("--max-depth", type=int, default=Limits().max_depth)
    args = parser.parse_args(argv)
    try:
        result = execute_resolved_query_files(
            args.qur,
            args.clause_ir,
            args.resolution,
            args.entries,
            args.snapshot,
            args.search_units,
            limits=Limits(args.max_record_bytes, args.max_depth),
        )
    except (
        CatalogContractError,
        ResolutionError,
        StructuredRowError,
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        ValueError,
    ) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(
        canonical_json(
            {
                "operation_count": len(result.operation_values),
                "output_count": len(result.requested_outputs),
                "source_search_unit_count": len(result.source_search_unit_ids),
                "status": "executed",
                "values_persisted": False,
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
