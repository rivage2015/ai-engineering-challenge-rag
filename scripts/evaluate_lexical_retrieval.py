#!/usr/bin/env python3
"""Evaluate lexical retrieval with Recall@k, Hit@k, and MRR."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
from collections import defaultdict
from pathlib import Path
from typing import Any

from lexical_search_common import canonical_json, digest_file
from retrieval_trace_common import enrich_retrieval, load_document_sources
from search_lexical_index import RANKER, RANKER_VERSION, search


CASE_ID = re.compile(r"^qe_[0-9a-f]{16,64}$")
SEARCH_UNIT_ID = re.compile(r"^su_[0-9a-f]{16,64}$")


def stable_id(prefix: str, value: Any) -> str:
    digest = hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()
    return f"{prefix}_{digest[:32]}"


def load_cases(path: Path) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    seen: set[str] = set()
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                case = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_number}: invalid JSON: {exc}") from exc
            case_id = case.get("eval_case_id", "")
            relevant = case.get("relevant_search_unit_ids", [])
            if case.get("record_type") != "retrieval_eval_case" or not CASE_ID.fullmatch(case_id):
                raise ValueError(f"{path}:{line_number}: invalid evaluation case identity")
            if case_id in seen:
                raise ValueError(f"{path}:{line_number}: duplicate eval_case_id {case_id}")
            if not case.get("query", "").strip() or not relevant or len(relevant) != len(set(relevant)):
                raise ValueError(f"{path}:{line_number}: invalid query or relevance set")
            if any(not SEARCH_UNIT_ID.fullmatch(item) for item in relevant):
                raise ValueError(f"{path}:{line_number}: malformed relevant SearchUnit ID")
            provenance = case.get("provenance", {})
            identity = {
                "query": case.get("query"),
                "relevant_search_unit_ids": relevant,
                "method": provenance.get("method"),
                "generator": provenance.get("generator"),
                "generator_version": provenance.get("generator_version"),
            }
            if case_id != stable_id("qe", identity):
                raise ValueError(f"{path}:{line_number}: unstable or modified eval_case_id")
            if provenance.get("method") == "human_reviewed":
                review = case.get("review", {})
                if review.get("reviewed") is not True or not review.get("source_locations"):
                    raise ValueError(f"{path}:{line_number}: human-reviewed case lacks review evidence")
            seen.add(case_id)
            cases.append(case)
    if not cases:
        raise ValueError("evaluation set is empty")
    return cases


def metrics_for(outcomes: list[dict[str, Any]], cutoffs: list[int]) -> dict[str, Any]:
    result: dict[str, Any] = {"case_count": len(outcomes)}
    result["mrr"] = sum(outcome["reciprocal_rank"] for outcome in outcomes) / len(outcomes)
    for cutoff in cutoffs:
        result[f"hit_at_{cutoff}"] = sum(outcome["first_relevant_rank"] is not None and outcome["first_relevant_rank"] <= cutoff for outcome in outcomes) / len(outcomes)
        result[f"recall_at_{cutoff}"] = sum(outcome["recall_by_k"][str(cutoff)] for outcome in outcomes) / len(outcomes)
    return result


def evaluate(
    index: Path,
    evaluation_set: Path,
    cutoffs: list[int],
    field_value_weight: float = 0.5,
    parent_context_penalty: float = 2.0,
    semantic_index: Path | None = None,
    semantic_weight: float = 0.25,
    base_url: str = "http://127.0.0.1:11434",
    semantic_only: bool = False,
    adaptive_semantic: bool = False,
    intermediates: list[Path] | None = None,
) -> dict[str, Any]:
    normalized_cutoffs = sorted(set(cutoffs))
    if not normalized_cutoffs or normalized_cutoffs[0] < 1:
        raise ValueError("all cutoffs must be positive")
    cases = load_cases(evaluation_set)
    document_sources: dict[str, str] = {}
    intermediate_states: list[dict[str, str]] = []
    if intermediates:
        document_sources, intermediate_states = load_document_sources(intermediates)
    database = index / "lexical-index.sqlite3"
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        indexed_ids = {row[0] for row in connection.execute("SELECT search_unit_id FROM documents")}
    finally:
        connection.close()
    expected_ids = {item for case in cases for item in case["relevant_search_unit_ids"]}
    missing_ids = sorted(expected_ids - indexed_ids)
    if missing_ids:
        preview = ", ".join(missing_ids[:10])
        suffix = f" and {len(missing_ids) - 10} more" if len(missing_ids) > 10 else ""
        raise ValueError(f"evaluation targets are absent from the index: {preview}{suffix}")
    maximum = normalized_cutoffs[-1]
    outcomes: list[dict[str, Any]] = []
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    if semantic_only and semantic_index is None:
        raise ValueError("semantic_only requires a semantic index")
    for case in cases:
        if semantic_only:
            from search_semantic_index import search as search_semantic

            retrieval = search_semantic(
                semantic_index,
                case["query"],
                maximum,
                base_url=base_url,
                snippet_chars=160,
            )
        elif semantic_index is None:
            retrieval = search(
                index, case["query"], maximum, snippet_chars=160,
                field_value_weight=field_value_weight,
                parent_context_penalty=parent_context_penalty,
            )
        else:
            from search_hybrid import search as search_hybrid

            retrieval = search_hybrid(
                index, semantic_index, case["query"], maximum,
                base_url=base_url,
                candidate_k=max(50, maximum),
                semantic_weight=semantic_weight,
                snippet_chars=160,
                adaptive_semantic=adaptive_semantic,
            )
        if document_sources:
            enrich_retrieval(retrieval, document_sources)
        retrieved = [item["search_unit_id"] for item in retrieval["results"]]
        relevant = set(case["relevant_search_unit_ids"])
        ranks = [rank for rank, search_unit_id in enumerate(retrieved, 1) if search_unit_id in relevant]
        first_rank = min(ranks) if ranks else None
        provenance = case.get("provenance", {})
        review = case.get("review", {})
        if provenance.get("method") == "human_reviewed" and review.get("reviewed") is True:
            ground_truth_status = "confirmed"
        elif review:
            ground_truth_status = "provisional"
        else:
            ground_truth_status = "needs_human_review"
        outcome = {
            "eval_case_id": case["eval_case_id"],
            "category": case.get("category", "other"),
            "query": case["query"],
            "ground_truth_status": ground_truth_status,
            "review_source_locations": review.get("source_locations", []),
            "relevant_search_unit_ids": sorted(relevant),
            "first_relevant_rank": first_rank,
            "reciprocal_rank": 1.0 / first_rank if first_rank else 0.0,
            "recall_by_k": {
                str(cutoff): len(relevant.intersection(retrieved[:cutoff])) / len(relevant)
                for cutoff in normalized_cutoffs
            },
            "retrieved_search_unit_ids": retrieved,
            "retrieved_results": retrieval["results"],
        }
        outcomes.append(outcome)
        grouped[outcome["category"]].append(outcome)
    inputs = {
        "evaluation_set_sha256": digest_file(evaluation_set),
        "lexical_index_state_sha256": digest_file(index / "lexical-index-state.json"),
    }
    if intermediate_states:
        inputs["intermediate_states"] = intermediate_states
    semantic_model = None
    if semantic_index is not None:
        semantic_state_path = semantic_index / "semantic-index-state.json"
        semantic_state = json.loads(semantic_state_path.read_text(encoding="utf-8"))
        inputs["semantic_index_state_sha256"] = digest_file(semantic_state_path)
        semantic_model = semantic_state.get("model")
    if semantic_only:
        retrieval_method = "cosine-local-embedding"
        report_ranker = "cosine-local-embedding"
        report_ranker_version = semantic_state.get("builder_version") if semantic_state else None
    elif semantic_index is not None:
        from search_hybrid import FUSER, FUSER_VERSION

        retrieval_method = (
            "BM25-field-parent+local-semantic-RRF-adaptive"
            if adaptive_semantic else "BM25-field-parent+local-semantic-RRF"
        )
        report_ranker = FUSER
        report_ranker_version = FUSER_VERSION
    else:
        retrieval_method = "BM25+field-aware-parent-child" if field_value_weight or parent_context_penalty else "BM25"
        report_ranker = RANKER
        report_ranker_version = RANKER_VERSION
    return {
        "evaluation_method": "post_retrieval_relevance_comparison",
        "retrieval_method": retrieval_method,
        "ranker": report_ranker,
        "ranker_version": report_ranker_version,
        "field_value_weight": None if semantic_only else field_value_weight,
        "parent_context_penalty": None if semantic_only else parent_context_penalty,
        "semantic_weight": semantic_weight if semantic_index is not None and not semantic_only else None,
        "adaptive_semantic": adaptive_semantic if semantic_index is not None and not semantic_only else False,
        "semantic_model": semantic_model,
        "inputs": inputs,
        "cutoffs": normalized_cutoffs,
        "overall": metrics_for(outcomes, normalized_cutoffs),
        "by_category": {category: metrics_for(items, normalized_cutoffs) for category, items in sorted(grouped.items())},
        "cases": outcomes,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--index", required=True, type=Path)
    parser.add_argument("--evaluation-set", required=True, type=Path)
    parser.add_argument("--k", type=int, nargs="+", default=[1, 3, 5, 10])
    parser.add_argument("--out", type=Path)
    parser.add_argument("--field-value-weight", type=float, default=0.5)
    parser.add_argument("--parent-context-penalty", type=float, default=2.0)
    parser.add_argument("--semantic-index", type=Path)
    parser.add_argument("--semantic-weight", type=float, default=0.25)
    parser.add_argument("--base-url", default="http://127.0.0.1:11434")
    parser.add_argument("--semantic-only", action="store_true")
    parser.add_argument(
        "--adaptive-semantic",
        action="store_true",
        help="increase semantic weight only when lexical confidence is low; fixed RRF is the default",
    )
    parser.add_argument(
        "--intermediate", type=Path, nargs="+",
        help="intermediate directories used to preserve file/page/sheet/slide trace fields",
    )
    args = parser.parse_args()
    report = evaluate(
        args.index.resolve(), args.evaluation_set.resolve(), args.k,
        field_value_weight=args.field_value_weight,
        parent_context_penalty=args.parent_context_penalty,
        semantic_index=args.semantic_index.resolve() if args.semantic_index else None,
        semantic_weight=args.semantic_weight,
        base_url=args.base_url,
        semantic_only=args.semantic_only,
        adaptive_semantic=args.adaptive_semantic,
        intermediates=[path.resolve() for path in args.intermediate] if args.intermediate else None,
    )
    rendered = canonical_json(report) + "\n"
    if args.out:
        output = args.out.resolve()
        if output.exists():
            raise ValueError(f"output already exists: {output}")
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary = output.with_name(f".{output.name}.tmp")
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(rendered)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, output)
    print(rendered, end="")


if __name__ == "__main__":
    main()
