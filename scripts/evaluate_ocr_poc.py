#!/usr/bin/env python3
"""Evaluate raw OCR PoC runs against verified region transcriptions."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import statistics
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import ocr_poc_contract as contract  # noqa: E402


LIMITATIONS = [
    {
        "code": "diagnostic_fixture_selection",
        "description": (
            "Fixtures are human-verified diagnostic regions selected from the existing "
            "corpus with help from prior OCR observations; this is not an unbiased held-out set."
        ),
    },
    {
        "code": "no_handwriting_photo_vertical",
        "description": (
            "The current corpus fixtures do not establish accuracy for natural photographs, "
            "handwriting, or Japanese vertical text."
        ),
    },
    {
        "code": "region_text_only",
        "description": (
            "CER and exact match are measured on verified regions, not on full-page reading order, "
            "table structure, chart values, or end-to-end retrieval and answer accuracy."
        ),
    },
    {
        "code": "raw_and_collapsed_metrics",
        "description": (
            "Whitespace-collapsed text CER and raw CER are both reported because layout whitespace "
            "changes must not be silently normalized away."
        ),
    },
]


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, math.ceil(fraction * len(ordered)) - 1)
    return ordered[index]


def aggregate(items: list[dict[str, Any]]) -> dict[str, Any]:
    total_reference = sum(item["metrics"]["reference_chars"] for item in items)
    total_distance = sum(item["metrics"]["edit_distance"] for item in items)
    collapsed_reference = sum(
        item["metrics"]["whitespace_collapsed_reference_chars"] for item in items
    )
    collapsed_distance = sum(
        item["metrics"]["whitespace_collapsed_edit_distance"] for item in items
    )
    total_spans = sum(item["metrics"]["important_span_total"] for item in items)
    matched_spans = sum(item["metrics"]["important_span_matched"] for item in items)
    inference_durations = [item["inference_ms"] for item in items]
    setup_durations = [item["setup_ms"] for item in items]
    total_durations = [
        item["setup_ms"] + item["inference_ms"] for item in items
    ]
    status_counts: dict[str, int] = {}
    for item in items:
        status_counts[item["status"]] = status_counts.get(item["status"], 0) + 1
    return {
        "fixture_count": len(items),
        "status_counts": dict(sorted(status_counts.items())),
        "completed_rate": (
            status_counts.get("completed", 0) / len(items) if items else None
        ),
        "needs_review_rate": (
            status_counts.get("needs_review", 0) / len(items) if items else None
        ),
        "failure_rate": (
            sum(status_counts.get(status, 0) for status in ("failed", "timeout", "unavailable"))
            / len(items)
            if items
            else None
        ),
        "exact_match_count": sum(bool(item["metrics"]["exact_match"]) for item in items),
        "exact_match_rate": (
            sum(bool(item["metrics"]["exact_match"]) for item in items) / len(items)
            if items
            else None
        ),
        "micro_cer": total_distance / total_reference if total_reference else None,
        "macro_cer": statistics.fmean(item["metrics"]["cer"] for item in items) if items else None,
        "whitespace_collapsed_micro_cer": (
            collapsed_distance / collapsed_reference if collapsed_reference else None
        ),
        "whitespace_collapsed_macro_cer": (
            statistics.fmean(
                item["metrics"]["whitespace_collapsed_cer"] for item in items
            )
            if items
            else None
        ),
        "important_span_recall": matched_spans / total_spans if total_spans else None,
        "important_span_matched": matched_spans,
        "important_span_total": total_spans,
        "inference_ms": {
            "total": sum(inference_durations),
            "mean": statistics.fmean(inference_durations) if inference_durations else None,
            "p50": percentile(inference_durations, 0.5),
            "p90": percentile(inference_durations, 0.9),
            "max": max(inference_durations) if inference_durations else None,
        },
        "setup_ms": {
            "total": sum(setup_durations),
            "max": max(setup_durations) if setup_durations else None,
        },
        "total_ms": {
            "total": sum(total_durations),
            "mean": statistics.fmean(total_durations) if total_durations else None,
            "p50": percentile(total_durations, 0.5),
            "p90": percentile(total_durations, 0.9),
            "max": max(total_durations) if total_durations else None,
            "first_fixture": total_durations[0] if total_durations else None,
            "warm_p50": percentile(total_durations[1:], 0.5),
        },
    }


def build_report(
    fixtures: list[dict[str, Any]],
    runs: list[dict[str, Any]],
    fixtures_path: Path,
    runs_path: Path,
    expected_engines: dict[str, str],
) -> dict[str, Any]:
    fixture_map = {fixture["fixture_id"]: fixture for fixture in fixtures}
    if len(fixture_map) != len(fixtures):
        raise ValueError("fixture manifest contains duplicate fixture_id values")
    seen_pairs: set[tuple[str, str]] = set()
    engine_identities = contract.validate_expected_engines(runs, expected_engines)
    details: list[dict[str, Any]] = []
    for position, run in enumerate(runs, 1):
        errors = contract.validate_run(run)
        if errors:
            raise ValueError(f"run {position} is invalid: " + "; ".join(errors))
        fixture_id = run["fixture_ref"]["fixture_id"]
        fixture = fixture_map.get(fixture_id)
        if fixture is None:
            raise ValueError(f"run references unknown fixture: {fixture_id}")
        if run["fixture_ref"]["signature_sha256"] != fixture["hashes"]["signature_sha256"]:
            raise ValueError(f"run fixture signature mismatch: {fixture_id}")
        pair = (fixture_id, run["engine"]["name"])
        if pair in seen_pairs:
            raise ValueError(f"duplicate fixture/engine run: {pair}")
        seen_pairs.add(pair)
        reference = fixture["reference"]
        metrics = contract.fixture_metrics(
            reference["raw_text"], run["raw_text"], reference["important_spans"]
        )
        details.append(
            {
                "fixture_id": fixture_id,
                "engine": run["engine"]["name"],
                "status": run["status"],
                "origin_kind": fixture["asset_ref"]["origin_kind"],
                "document_family": fixture["strata"]["document_family"],
                "purpose": fixture["crop"]["purpose"],
                "difficulty": fixture["strata"]["difficulty"],
                "inference_ms": run["timing"]["inference_ms"],
                "setup_ms": run["timing"]["setup_ms"],
                "metrics": metrics,
            }
        )
    engines = sorted({item["engine"] for item in details})
    expected_pairs = {(fixture_id, engine) for fixture_id in fixture_map for engine in engines}
    missing = sorted(expected_pairs - seen_pairs)
    if missing:
        raise ValueError(f"run matrix is incomplete; first missing pair: {missing[0]}")
    by_engine: dict[str, Any] = {}
    for engine in engines:
        engine_items = [item for item in details if item["engine"] == engine]
        strata: dict[str, Any] = {}
        families = sorted({item["document_family"] for item in engine_items})
        for family in families:
            strata[family] = aggregate(
                [item for item in engine_items if item["document_family"] == family]
            )
        by_engine[engine] = {"overall": aggregate(engine_items), "by_document_family": strata}
    return {
        "schema_version": "0.1",
        "record_type": "ocr_poc_evaluation_report",
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "fixture_manifest": {
            "path": str(fixtures_path),
            "sha256": contract.sha256_file(fixtures_path),
            "verified_fixture_count": len(fixtures),
        },
        "runs": {"path": str(runs_path), "sha256": contract.sha256_file(runs_path)},
        "engines": by_engine,
        "experiment_plan": {
            "expected_engines": {
                name: {
                    "fingerprint_sha256": expected_engines[name],
                    "version": engine_identities[name]["version"],
                }
                for name in sorted(expected_engines)
            }
        },
        "details": details,
        "limitations": LIMITATIONS,
        "decision_policy": {
            "automatic_winner_selected": False,
            "required_dimensions": [
                "cer",
                "important_span_recall",
                "failure_rate",
                "runtime",
                "provenance",
            ],
        },
    }


def markdown(report: dict[str, Any]) -> str:
    lines = [
        "# OCR PoC evaluation",
        "",
        f"- verified fixtures: {report['fixture_manifest']['verified_fixture_count']}",
        f"- manifest SHA-256: `{report['fixture_manifest']['sha256']}`",
        f"- runs SHA-256: `{report['runs']['sha256']}`",
        "- automatic winner: disabled",
        "",
        "## Evaluation limits",
        "",
        *[
            f"- `{item['code']}`: {item['description']}"
            for item in report["limitations"]
        ],
        "",
        "## Metrics",
        "",
        "| engine | exact | text CER | raw CER | span recall | warm p50 ms | first ms | review | failure |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for engine, value in report["engines"].items():
        overall = value["overall"]
        recall = overall["important_span_recall"]
        recall_text = "n/a" if recall is None else f"{recall:.4f}"
        lines.append(
            f"| {engine} | {overall['exact_match_count']}/{overall['fixture_count']} | "
            f"{overall['whitespace_collapsed_micro_cer']:.4f} | "
            f"{overall['micro_cer']:.4f} | {recall_text} | "
            f"{overall['total_ms']['warm_p50']:.2f} | "
            f"{overall['total_ms']['first_fixture']:.2f} | "
            f"{overall['needs_review_rate']:.4f} | {overall['failure_rate']:.4f} |"
        )
    lines.extend(
        [
            "",
            "CERだけでは採否を決めない。重要語句、失敗、速度、bbox・来歴、最終RAG精度を別々に確認する。",
            "",
        ]
    )
    return "\n".join(lines)


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("--fixtures", type=Path, required=True)
    value.add_argument("--runs", type=Path, required=True)
    value.add_argument(
        "--expected-engine",
        action="append",
        required=True,
        metavar="NAME=SHA256",
        help="repeat for every engine required by this experiment plan",
    )
    value.add_argument("--report-json", type=Path, required=True)
    value.add_argument("--report-md", type=Path, required=True)
    value.add_argument("--repository-root", type=Path, default=ROOT)
    value.add_argument("--overwrite", action="store_true")
    return value


def parse_expected_engines(values: list[str]) -> dict[str, str]:
    expected: dict[str, str] = {}
    for value in values:
        name, separator, fingerprint = value.partition("=")
        if not separator or not name or len(fingerprint) != 64:
            raise ValueError(
                f"invalid --expected-engine value {value!r}; use NAME=64_HEX_SHA256"
            )
        try:
            int(fingerprint, 16)
        except ValueError as exc:
            raise ValueError(f"invalid engine fingerprint for {name}") from exc
        if name in expected:
            raise ValueError(f"duplicate expected engine: {name}")
        expected[name] = fingerprint
    return expected


def main() -> int:
    args = parser().parse_args()
    for output in (args.report_json, args.report_md):
        if output.exists() and not args.overwrite:
            print(f"error: output exists; pass --overwrite: {output}", file=sys.stderr)
            return 2
    try:
        fixtures = contract.load_jsonl(args.fixtures)
        for position, fixture in enumerate(fixtures, 1):
            errors = contract.validate_fixture(
                fixture, repository_root=args.repository_root, require_verified=True
            )
            if errors:
                raise ValueError(f"fixture {position} is invalid: " + "; ".join(errors))
        runs = contract.load_jsonl(args.runs)
        expected_engines = parse_expected_engines(args.expected_engine)
        report = build_report(
            fixtures, runs, args.fixtures, args.runs, expected_engines
        )
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    args.report_json.parent.mkdir(parents=True, exist_ok=True)
    args.report_md.parent.mkdir(parents=True, exist_ok=True)
    args.report_json.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    args.report_md.write_text(markdown(report), encoding="utf-8")
    print(f"wrote OCR PoC report: {args.report_json}")
    print(f"wrote OCR PoC summary: {args.report_md}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
