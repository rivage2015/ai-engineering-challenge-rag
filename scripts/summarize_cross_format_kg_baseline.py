#!/usr/bin/env python3
"""Create a stable, reviewable summary of the cross-format Phase 1 report."""

from __future__ import annotations

import argparse
from collections import Counter
import json
import os
from pathlib import Path
from typing import Any


DATASET_ID = "cross-format-kg-v0.1"
METHODS = (
    "distribution-lexical-token-proxy",
    "layer1-real-bm25",
    "layer1-adapter-document-support-through-distribution-safe-stream-proxy",
)
REQUIRED_CROSS_DOCUMENT_RELATIONS = (
    "ASSIGNED_TO",
    "IDENTIFIES_PERSON",
    "SUPERSEDES",
    "CONTRADICTS",
)


def _failed_phrase_cases(value: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "eval_case_id": case["eval_case_id"],
            "missing_phrases": case["missing_phrases"],
        }
        for case in value.get("cases", [])
        if case.get("missing_phrases")
    ]


def _retrieval_summary(value: dict[str, Any]) -> dict[str, Any]:
    failed = []
    for case in value.get("cases", []):
        if case.get("all_relevant_at_5") == 1:
            continue
        failed.append({
            "eval_case_id": case["eval_case_id"],
            "source_recall_at_5": case["source_recall_at_5"],
            "missing_relevant_sources": sorted(
                set(case["relevant_sources"])
                - {item["relative_path"] for item in case.get("retrieved", [])}
            ),
        })
    return {
        "metrics": value["metrics"],
        "failed_all_relevant_at_5": failed,
    }


def _relation_inventory(root: Path | None) -> dict[str, Any]:
    if root is None:
        return {"status": "not_supplied"}
    targets = {
        "distribution": root / "distribution" / "semantic-relations.jsonl",
        "layer1": root / "layer1" / "intermediate" / "relations.jsonl",
    }
    result: dict[str, Any] = {}
    observed_types: set[str] = set()
    for label, path in targets.items():
        if not path.is_file():
            result[label] = {"status": "missing"}
            continue
        rows = [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        relation_types = Counter(str(row.get("relation_type")) for row in rows)
        relation_classes = Counter(
            str(row.get("relation_class", "legacy-unclassified")) for row in rows
        )
        observed_types.update(relation_types)
        result[label] = {
            "status": "present",
            "relation_count": len(rows),
            "relation_types": dict(sorted(relation_types.items())),
            "relation_classes": dict(sorted(relation_classes.items())),
        }
    result["required_cross_document_relation_types"] = list(
        REQUIRED_CROSS_DOCUMENT_RELATIONS
    )
    result["required_cross_document_relation_types_present"] = sorted(
        observed_types & set(REQUIRED_CROSS_DOCUMENT_RELATIONS)
    )
    return result


def summarize(
    report: dict[str, Any], *, baseline_root: Path | None = None
) -> dict[str, Any]:
    coverage = report["coverage"]
    phrase = report["expected_phrase_coverage"]
    retrieval_by_method = {
        item["method"]: item for item in report["retrieval_comparison"]
    }
    missing_methods = [method for method in METHODS if method not in retrieval_by_method]
    if missing_methods:
        raise ValueError(f"missing retrieval methods: {missing_methods}")

    distribution_missing = _failed_phrase_cases(phrase["distribution"])
    relationship = report.get("relationship_context_audit", {})
    graph_traversal = report["modes"].get(
        "semantic_graph_traversal", "UNDECLARED_NOT_EVALUATED"
    )
    return {
        "schema_version": "0.1",
        "record_type": "cross_format_kg_phase_1_baseline_result",
        "dataset_id": DATASET_ID,
        "decision": "BASELINE_ONLY_NOT_GRAPH_PROOF",
        "coverage": {
            "case_count": coverage["case_count"],
            "dataset_files": coverage["dataset_files"],
            "formats": coverage["formats"],
            "distribution_document_count": coverage["distribution"]["document_count"],
            "layer1_input_files": coverage["layer1"]["input_files"],
            "layer1_statuses": coverage["layer1"]["statuses"],
        },
        "offline": {
            "external_network_used": report["modes"]["external_network_used"],
        },
        "expected_phrase_coverage": {
            "distribution": {
                "all_pass": phrase["distribution"]["all_pass"],
                "failed_cases": distribution_missing,
                "diagnosis": (
                    "The legacy OOXML reader exposes typed XLSX dates as Excel serials."
                    if distribution_missing
                    else None
                ),
            },
            "layer1_adapter": {
                "all_pass": phrase["layer1_adapter"]["all_pass"],
                "failed_cases": _failed_phrase_cases(phrase["layer1_adapter"]),
            },
        },
        "retrieval": {
            method: _retrieval_summary(retrieval_by_method[method])
            for method in METHODS
        },
        "graph_and_answer": {
            "semantic_graph_traversal": graph_traversal,
            "llm_answer_generation": report["modes"]["llm_answer_generation"],
            "relationship_context_audit": {
                "all_pass": relationship.get("all_pass", False),
                "case_count": len(relationship.get("cases", [])),
            },
            "phase_2_e2e_qa_case_count": 0,
            "existing_graph_inventory": _relation_inventory(baseline_root),
        },
        "findings": [
            "All five source files were ingested by both extraction paths.",
            "Layer 1 preserved every expected phrase, including typed XLSX dates.",
            "Real BM25 found at least one relevant source for every case, but returned all required sources for only three of five cases at k=5.",
            "The two BM25 misses require an ID-to-person-name bridge that flat lexical retrieval cannot infer from the question alone.",
            "Phase 1 did not execute semantic Edge traversal or answer generation, so it does not prove cross-document Knowledge Graph use.",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()
    if args.overwrite and args.out is None:
        parser.error("--overwrite requires --out")
    report = json.loads(args.report.read_text(encoding="utf-8"))
    value = summarize(report, baseline_root=args.report.parent)
    rendered = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    if args.out is None:
        print(rendered, end="")
        return 0
    if args.out.exists() and not args.overwrite:
        raise SystemExit(f"refusing to overwrite: {args.out}")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.out.with_name(f".{args.out.name}.building")
    try:
        temporary.write_text(rendered, encoding="utf-8")
        os.replace(temporary, args.out)
    finally:
        temporary.unlink(missing_ok=True)
    print(args.out.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
