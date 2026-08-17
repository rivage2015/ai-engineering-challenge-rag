#!/usr/bin/env python3
"""Run the Phase 2.5 three-contract gate without starting retrieval.

The shadow runner consumes completed QuestionUnderstandingRun records and one
validated Data Catalog.  It deterministically builds QuestionClauseIR records
and CatalogResolutionRun records, but it never calls a model, reads answers,
or invokes a retrieval backend.  This keeps rollout evidence separate from the
current RAG path until the acceptance suite authorizes a live gate.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from build_data_catalog import CatalogContractError, Limits  # noqa: E402
from build_question_clause_ir import (  # noqa: E402
    _atomic_write_jsonl,
    build_question_clause_ir,
)
from build_question_understanding import validate_understanding_run  # noqa: E402
from resolve_catalog import (  # noqa: E402
    ResolutionError,
    resolve_catalog,
    validate_resolution_record_local,
)
from validate_data_catalog import (  # noqa: E402
    _read_entries,
    _read_snapshot,
    validate_data_catalog,
)
from validate_question_clause_ir import (  # noqa: E402
    canonical_json,
    load_json_records,
    validate_question_clause_ir,
)


@dataclass(frozen=True)
class ShadowGateResult:
    clause_irs: tuple[dict[str, Any], ...]
    resolutions: tuple[dict[str, Any], ...]

    @property
    def ready_count(self) -> int:
        return sum(
            record["final_status"] == "resolved" for record in self.resolutions
        )

    @property
    def hold_count(self) -> int:
        return len(self.resolutions) - self.ready_count


def _question_input(qur: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "question_id": qur["question_id"],
        "original_question": qur["original_question"],
    }


def run_phase25_shadow(
    qurs: Sequence[dict[str, Any]],
    entries: Sequence[dict[str, Any]],
    snapshot: dict[str, Any],
    *,
    generated_at: str | None = None,
) -> ShadowGateResult:
    """Return shadow ClauseIR and resolution records without retrieval."""

    if not qurs:
        raise ValueError("at least one QuestionUnderstandingRun is required")
    clause_irs: list[dict[str, Any]] = []
    resolutions: list[dict[str, Any]] = []
    seen_questions: set[tuple[str, str]] = set()
    for index, qur in enumerate(qurs):
        errors = validate_understanding_run(qur)
        if errors:
            raise ValueError(
                f"qur[{index}] is invalid: " + "; ".join(errors[:8])
            )
        identity = (qur["question_id"], qur["original_question"])
        if identity in seen_questions:
            raise ValueError("duplicate question identity in shadow input")
        seen_questions.add(identity)
        qic = qur["question_intent_contract"]
        clause_ir = build_question_clause_ir(
            _question_input(qur),
            generated_at=generated_at or qur["provenance"]["generated_at"],
            qic=qic,
        )
        clause_errors = validate_question_clause_ir(clause_ir, qic)
        if clause_errors:
            raise ValueError(
                f"qur[{index}] produced invalid ClauseIR: "
                + "; ".join(clause_errors[:8])
            )
        resolution = resolve_catalog(
            qur,
            clause_ir,
            entries,
            snapshot,
            generated_at=generated_at,
        )
        resolution_errors = validate_resolution_record_local(resolution)
        if resolution_errors:
            raise ValueError(
                f"qur[{index}] produced invalid resolution: "
                + "; ".join(resolution_errors[:8])
            )
        clause_irs.append(clause_ir)
        resolutions.append(resolution)
    return ShadowGateResult(tuple(clause_irs), tuple(resolutions))


def run_phase25_shadow_files(
    qur_path: str | Path,
    entries_path: str | Path,
    snapshot_path: str | Path,
    *,
    documents_path: str | Path | None = None,
    search_units_path: str | Path | None = None,
    evidence_path: str | Path | None = None,
    generated_at: str | None = None,
    limits: Limits = Limits(),
) -> ShadowGateResult:
    """Load, validate, and run a shadow batch while reading the catalog once."""

    qurs = load_json_records(Path(qur_path))
    catalog_errors = validate_data_catalog(
        entries_path,
        snapshot_path,
        documents_path=documents_path,
        search_units_path=search_units_path,
        evidence_path=evidence_path,
        limits=limits,
    )
    if catalog_errors:
        raise CatalogContractError(
            "invalid Data Catalog: " + "; ".join(catalog_errors[:8])
        )
    entries, _ = _read_entries(Path(entries_path), limits)
    snapshot = _read_snapshot(Path(snapshot_path), limits)
    return run_phase25_shadow(
        qurs,
        entries,
        snapshot,
        generated_at=generated_at,
    )


def _assert_distinct_paths(paths: Sequence[Path]) -> None:
    resolved = [path.expanduser().absolute() for path in paths]
    if len(resolved) != len(set(resolved)):
        raise ValueError("shadow input and output paths must be distinct")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--qur", required=True, type=Path)
    parser.add_argument("--entries", required=True, type=Path)
    parser.add_argument("--snapshot", required=True, type=Path)
    parser.add_argument("--clause-ir-out", required=True, type=Path)
    parser.add_argument("--resolution-out", required=True, type=Path)
    parser.add_argument("--documents", type=Path)
    parser.add_argument("--search-units", type=Path)
    parser.add_argument("--evidence", type=Path)
    parser.add_argument("--generated-at")
    parser.add_argument("--max-record-bytes", type=int, default=Limits().max_record_bytes)
    parser.add_argument("--max-depth", type=int, default=Limits().max_depth)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        _assert_distinct_paths(
            [
                args.qur,
                args.entries,
                args.snapshot,
                args.clause_ir_out,
                args.resolution_out,
            ]
        )
        result = run_phase25_shadow_files(
            args.qur,
            args.entries,
            args.snapshot,
            documents_path=args.documents,
            search_units_path=args.search_units,
            evidence_path=args.evidence,
            generated_at=args.generated_at,
            limits=Limits(args.max_record_bytes, args.max_depth),
        )
        _atomic_write_jsonl(args.clause_ir_out, result.clause_irs)
        _atomic_write_jsonl(args.resolution_out, result.resolutions)
    except (
        CatalogContractError,
        ResolutionError,
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
                "hold_count": result.hold_count,
                "records": len(result.resolutions),
                "ready_count": result.ready_count,
                "retrieval_started": False,
                "shadow_only": True,
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
