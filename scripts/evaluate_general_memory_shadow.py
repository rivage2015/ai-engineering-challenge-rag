#!/usr/bin/env python3
"""Compare the distribution path and Layer 1 on fixed synthetic ground truth.

The default mode is fully offline.  It evaluates the distribution extraction
and security gate with the distribution's deterministic lexical/token scoring
functions, and evaluates Layer 1 with its real SQLite BM25 index.  It does not
claim to measure either LLM answerer or the distribution semantic-vector path.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import subprocess
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts"
ENGINE = REPO / "distribution" / "macos-local-memory" / "engine"
FIXED_RUN_AT = "2026-08-27T00:00:00+00:00"
TOP_K = 5


def jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def run(command: list[str]) -> None:
    process = subprocess.run(command, cwd=REPO, text=True, capture_output=True, check=False)
    if process.returncode:
        raise RuntimeError(
            f"command failed ({process.returncode}): {' '.join(command)}\n"
            f"stdout:\n{process.stdout}\nstderr:\n{process.stderr}"
        )


def load_module(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load module: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def validate_cases(cases: list[dict[str, Any]], corpus: Path) -> None:
    if not cases:
        raise ValueError("evaluation set has no cases")
    seen: set[str] = set()
    allowed = {"single_source", "multi_source", "temporal_conflict", "safety_exclusion"}
    for case in cases:
        required = {
            "schema_version", "record_type", "eval_case_id", "category", "query",
            "relevant_sources", "expected_phrases", "provenance",
        }
        missing = required - set(case)
        if missing:
            raise ValueError(f"{case.get('eval_case_id', '?')}: missing fields {sorted(missing)}")
        case_id = case["eval_case_id"]
        if case_id in seen:
            raise ValueError(f"duplicate eval_case_id: {case_id}")
        seen.add(case_id)
        if case["schema_version"] != "0.1" or case["record_type"] != "general_memory_shadow_eval_case":
            raise ValueError(f"{case_id}: incompatible schema or record type")
        if case["category"] not in allowed:
            raise ValueError(f"{case_id}: unknown category")
        if not str(case["query"]).strip() or not case["relevant_sources"]:
            raise ValueError(f"{case_id}: query and relevant_sources are required")
        provenance = case["provenance"]
        if provenance.get("method") != "human_authored_synthetic" or provenance.get("reviewed") is not True:
            raise ValueError(f"{case_id}: ground truth must be human-authored and reviewed")
        for relative in case["relevant_sources"]:
            source = (corpus / relative).resolve()
            try:
                source.relative_to(corpus.resolve())
            except ValueError as exc:
                raise ValueError(f"{case_id}: source escapes corpus: {relative}") from exc
            if not source.is_file():
                raise ValueError(f"{case_id}: missing source: {relative}")
        if case["category"] == "safety_exclusion" and "expected_security_disposition" not in case:
            raise ValueError(f"{case_id}: safety case needs expected_security_disposition")
        for field in ("expected_same_unit_phrases", "forbidden_same_unit_phrases"):
            for group in case.get(field, []):
                if not isinstance(group, list) or len(group) < 2 or not all(
                    isinstance(phrase, str) and phrase.strip() for phrase in group
                ):
                    raise ValueError(f"{case_id}: {field} needs groups of 2+ phrases")


def build_distribution(corpus: Path, output: Path) -> dict[str, Any]:
    output.mkdir(parents=True)
    python = sys.executable
    run([python, str(ENGINE / "build_path_graph.py"), str(corpus), "--output-dir", str(output)])
    run([
        python, str(ENGINE / "build_semantic_graph.py"),
        "--inventory", str(output / "path-source-inventory.jsonl"),
        "--source-root", str(corpus), "--output-dir", str(output),
    ])
    run([
        python, str(ENGINE / "content_security_gate.py"),
        "--evidence", str(output / "semantic-evidence.jsonl"),
        "--documents", str(output / "semantic-documents.jsonl"),
        "--output-dir", str(output),
    ])
    return json.loads((output / "semantic-coverage.json").read_text(encoding="utf-8"))


def build_layer1(corpus: Path, output: Path) -> dict[str, Any]:
    intermediate = output / "intermediate"
    search = output / "search"
    lexical = output / "lexical"
    python = sys.executable
    output.mkdir(parents=True)
    run([
        python, str(SCRIPTS / "build_intermediate_records.py"),
        "--root", str(corpus), "--out", str(intermediate), "--run-at", FIXED_RUN_AT,
    ])
    run([
        python, str(SCRIPTS / "build_search_units.py"),
        "--intermediate", str(intermediate), "--out", str(search),
    ])
    run([
        python, str(SCRIPTS / "build_lexical_index.py"),
        "--search-output", str(search), "--out", str(lexical),
    ])
    return {
        "intermediate": intermediate,
        "search": search,
        "lexical": lexical,
        "state": json.loads((intermediate / "build-state.json").read_text(encoding="utf-8")),
    }


def build_layer1_adapter(corpus: Path, layer1_build: dict[str, Any], output: Path) -> dict[str, Any]:
    python = sys.executable
    run([
        python, str(SCRIPTS / "adapt_layer1_to_local_memory.py"),
        "--intermediate", str(layer1_build["intermediate"]),
        "--search-output", str(layer1_build["search"]),
        "--source-root", str(corpus), "--out", str(output),
    ])
    run([
        python, str(ENGINE / "content_security_gate.py"),
        "--evidence", str(output / "semantic-evidence.jsonl"),
        "--documents", str(output / "semantic-documents.jsonl"),
        "--output-dir", str(output),
    ])
    return json.loads((output / "layer1-adapter-state.json").read_text(encoding="utf-8"))


def distribution_ranker(output: Path) -> Callable[[str, int], list[dict[str, Any]]]:
    sys.path.insert(0, str(ENGINE))
    module = load_module("shadow_answer_local_memory_v2", ENGINE / "answer_local_memory_v2.py")
    documents = {item["document_id"]: item for item in jsonl(output / "semantic-documents.jsonl")}
    evidence = jsonl(output / "safe-answer-evidence.jsonl")

    def rank(query: str, top_k: int) -> list[dict[str, Any]]:
        candidates: list[dict[str, Any]] = []
        for record in evidence:
            relative = documents[record["document_id"]]["source"]["relative_path"]
            text = str(record.get("observed_text", ""))
            lexical = module.lexical_coverage(query, text)
            token = module.token_coverage(query, text)
            score = lexical * 0.15 + token * 0.30
            candidate = {
                "relative_path": relative,
                "score": round(score, 8),
                "lexical_score": round(lexical, 8),
                "token_score": round(token, 8),
                "evidence_id": record["evidence_id"],
                "document_id": record["document_id"],
                "text": text,
            }
            candidates.append(candidate)
        candidates = [
            item for item in candidates
            if not any(pattern.search(item["text"]) for pattern in module.base.INSTRUCTION_LIKE_PATTERNS)
        ]
        best: dict[str, dict[str, Any]] = {}
        for candidate in module.rerank_with_document_support(candidates):
            relative = candidate["relative_path"]
            previous = best.get(relative)
            if previous is None or (
                -candidate["rerank_score"], -candidate["score"], candidate["relative_path"], candidate["evidence_id"]
            ) < (
                -previous["rerank_score"], -previous["score"], previous["relative_path"], previous["evidence_id"]
            ):
                best[relative] = candidate
        return sorted(
            best.values(),
            key=lambda item: (
                -item["rerank_score"], -item["score"], item["relative_path"], item["evidence_id"],
            ),
        )[:top_k]

    return rank


def layer1_ranker(build: dict[str, Any]) -> Callable[[str, int], list[dict[str, Any]]]:
    sys.path.insert(0, str(SCRIPTS))
    lexical_search = load_module("shadow_search_lexical_index", SCRIPTS / "search_lexical_index.py")
    trace = load_module("shadow_retrieval_trace", SCRIPTS / "retrieval_trace_common.py")
    sources, _ = trace.load_document_sources([build["intermediate"]])

    def rank(query: str, top_k: int) -> list[dict[str, Any]]:
        # Layer 1 ranks SearchUnits, while the shared truth is source-file
        # based.  Search a wider unit pool and keep the highest ranked unit per
        # source so the comparison is not distorted by duplicate chunks.
        result = lexical_search.search(build["lexical"], query, max(top_k * 5, 25))
        trace.enrich_retrieval(result, sources)
        unique: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in result["results"]:
            relative = item.get("file")
            if not relative or relative in seen:
                continue
            seen.add(relative)
            unique.append({
                "relative_path": relative,
                "score": item["score"],
                "search_unit_id": item["search_unit_id"],
                "unit_type": item["unit_type"],
            })
            if len(unique) >= top_k:
                break
        return unique

    return rank


def evaluate_method(
    name: str,
    cases: list[dict[str, Any]],
    ranker: Callable[[str, int], list[dict[str, Any]]],
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    metric_cases = [case for case in cases if case["category"] != "safety_exclusion"]
    sums = Counter()
    for case in metric_cases:
        results = ranker(case["query"], TOP_K)
        ranked_paths = [item["relative_path"] for item in results]
        relevant = set(case["relevant_sources"])
        relevant_ranks = [rank for rank, path in enumerate(ranked_paths, 1) if path in relevant]
        first_rank = min(relevant_ranks) if relevant_ranks else None
        row = {
            "eval_case_id": case["eval_case_id"],
            "category": case["category"],
            "query": case["query"],
            "relevant_sources": case["relevant_sources"],
            "first_relevant_rank": first_rank,
            "retrieved": results,
        }
        for cutoff in (1, 3, 5):
            found = relevant & set(ranked_paths[:cutoff])
            row[f"hit_at_{cutoff}"] = int(bool(found))
            row[f"all_relevant_at_{cutoff}"] = int(found == relevant)
            row[f"source_recall_at_{cutoff}"] = len(found) / len(relevant)
            sums[f"hit_at_{cutoff}"] += row[f"hit_at_{cutoff}"]
            sums[f"all_relevant_at_{cutoff}"] += row[f"all_relevant_at_{cutoff}"]
            sums[f"source_recall_at_{cutoff}"] += row[f"source_recall_at_{cutoff}"]
        reciprocal = 1.0 / first_rank if first_rank else 0.0
        row["reciprocal_rank"] = reciprocal
        sums["reciprocal_rank"] += reciprocal
        rows.append(row)
    count = len(metric_cases)
    metrics = {key: round(value / count, 6) for key, value in sorted(sums.items())}
    metrics["case_count"] = count
    return {"method": name, "metrics": metrics, "cases": rows}


def phrase_coverage(cases: list[dict[str, Any]], evidence_dir: Path) -> dict[str, Any]:
    documents = {item["document_id"]: item for item in jsonl(evidence_dir / "semantic-documents.jsonl")}
    text_by_path: dict[str, list[str]] = {}
    for record in jsonl(evidence_dir / "semantic-evidence.jsonl"):
        relative = documents[record["document_id"]]["source"]["relative_path"]
        text_by_path.setdefault(relative, []).append(str(record.get("observed_text", "")))
    rows = []
    for case in cases:
        if case["category"] == "safety_exclusion":
            continue
        source_text = "\n".join(
            part for relative in case["relevant_sources"] for part in text_by_path.get(relative, [])
        )
        missing = [phrase for phrase in case["expected_phrases"] if phrase not in source_text]
        rows.append({
            "eval_case_id": case["eval_case_id"],
            "expected_phrases": case["expected_phrases"],
            "missing_phrases": missing,
            "pass": not missing,
        })
    return {"all_pass": all(row["pass"] for row in rows), "cases": rows}


def relationship_context_audit(cases: list[dict[str, Any]], adapter_dir: Path) -> dict[str, Any]:
    """Check that required phrases coexist in one verified table-row projection."""
    documents = {item["document_id"]: item for item in jsonl(adapter_dir / "semantic-documents.jsonl")}
    rows_by_path: dict[str, list[dict[str, Any]]] = {}
    for record in jsonl(adapter_dir / "safe-answer-evidence.jsonl"):
        adapter = record.get("adapter", {})
        if adapter.get("source_record_type") != "search_unit" or adapter.get("unit_type") != "table_row":
            continue
        relative = documents[record["document_id"]]["source"]["relative_path"]
        rows_by_path.setdefault(relative, []).append(record)
    results = []
    for case in cases:
        groups = case.get("expected_same_unit_phrases", [])
        forbidden_groups = case.get("forbidden_same_unit_phrases", [])
        if not groups and not forbidden_groups:
            continue
        candidate_rows = [
            row
            for relative in case["relevant_sources"]
            for row in rows_by_path.get(relative, [])
        ]
        group_results = []
        for phrases in groups:
            matches = [
                row for row in candidate_rows
                if all(phrase in str(row.get("observed_text", "")) for phrase in phrases)
            ]
            group_results.append({
                "phrases": phrases,
                "matching_evidence_ids": [row["evidence_id"] for row in matches],
                "pass": bool(matches),
            })
        forbidden_results = []
        for phrases in forbidden_groups:
            matches = [
                row for row in candidate_rows
                if all(phrase in str(row.get("observed_text", "")) for phrase in phrases)
            ]
            forbidden_results.append({
                "phrases": phrases,
                "matching_evidence_ids": [row["evidence_id"] for row in matches],
                "pass": not matches,
            })
        results.append({
            "eval_case_id": case["eval_case_id"],
            "groups": group_results,
            "forbidden_groups": forbidden_results,
            "pass": all(item["pass"] for item in group_results + forbidden_results),
        })
    return {"all_pass": bool(results) and all(item["pass"] for item in results), "cases": results}


def safety_audit(
    cases: list[dict[str, Any]],
    distribution: Path,
    adapter: Path,
    distribution_search: Callable[[str, int], list[dict[str, Any]]],
    adapter_search: Callable[[str, int], list[dict[str, Any]]],
    layer1_search: Callable[[str, int], list[dict[str, Any]]],
) -> dict[str, Any]:
    distribution_by_path = {
        item["source"]["relative_path"]: item
        for item in jsonl(distribution / "content-security-documents.jsonl")
    }
    adapter_by_path = {
        item["source"]["relative_path"]: item
        for item in jsonl(adapter / "content-security-documents.jsonl")
    }
    results = []
    for case in cases:
        if case["category"] != "safety_exclusion":
            continue
        distribution_paths = [item["relative_path"] for item in distribution_search(case["query"], TOP_K)]
        adapter_paths = [item["relative_path"] for item in adapter_search(case["query"], TOP_K)]
        layer1_paths = [item["relative_path"] for item in layer1_search(case["query"], TOP_K)]
        for relative in case["relevant_sources"]:
            distribution_record = distribution_by_path.get(relative)
            adapter_record = adapter_by_path.get(relative)
            actual = distribution_record.get("disposition") if distribution_record else "missing"
            adapter_actual = adapter_record.get("disposition") if adapter_record else "missing"
            results.append({
                "eval_case_id": case["eval_case_id"],
                "relative_path": relative,
                "expected": case["expected_security_disposition"],
                "distribution_actual": actual,
                "adapter_actual": adapter_actual,
                "pass": actual == case["expected_security_disposition"] == adapter_actual,
                "distribution_risk_reasons": distribution_record.get("risk_reasons", []) if distribution_record else [],
                "adapter_risk_reasons": adapter_record.get("risk_reasons", []) if adapter_record else [],
                "distribution_safe_stream_exposed_source": relative in distribution_paths,
                "adapter_safe_stream_exposed_source": relative in adapter_paths,
                "layer1_raw_retrieval_exposed_source": relative in layer1_paths,
                "distribution_safe_stream_paths": distribution_paths,
                "adapter_safe_stream_paths": adapter_paths,
                "layer1_raw_retrieval_paths": layer1_paths,
            })
    return {
        "distribution_gate": results,
        "all_pass": all(item["pass"] for item in results),
        "layer1_security_gate": "not_present_in_this_layer; must remain behind distribution safety boundary",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    dataset = args.dataset.resolve(strict=True)
    corpus = (dataset / "corpus").resolve(strict=True)
    cases = jsonl(dataset / "cases.jsonl")
    validate_cases(cases, corpus)
    output = args.out.resolve()
    if output.exists() and (not output.is_dir() or any(output.iterdir())):
        raise SystemExit(f"refusing to overwrite non-empty output directory: {output}")
    output.mkdir(parents=True, exist_ok=True)

    distribution_dir = output / "distribution"
    layer1_dir = output / "layer1"
    adapter_dir = output / "layer1-adapter"
    distribution_coverage = build_distribution(corpus, distribution_dir)
    layer1_build = build_layer1(corpus, layer1_dir)
    adapter_state = build_layer1_adapter(corpus, layer1_build, adapter_dir)
    distribution_search = distribution_ranker(distribution_dir)
    layer1_search = layer1_ranker(layer1_build)
    adapter_search = distribution_ranker(adapter_dir)
    covered_formats = sorted({
        path.suffix.lower().lstrip(".")
        for path in corpus.rglob("*")
        if path.is_file() and path.suffix
    })
    pending_file_formats = [
        item for item in ("docx", "xlsx", "pptx", "pdf", "images")
        if item not in covered_formats
    ]
    comparisons = [
        evaluate_method(
            "distribution-lexical-token-proxy",
            cases,
            distribution_search,
        ),
        evaluate_method("layer1-real-bm25", cases, layer1_search),
        evaluate_method(
            "layer1-adapter-document-support-through-distribution-safe-stream-proxy",
            cases,
            adapter_search,
        ),
    ]
    report = {
        "schema_version": "0.1",
        "record_type": "general_memory_shadow_evaluation_report",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "dataset": str(dataset),
        "ground_truth_policy": "human_authored_before_retrieval; never inferred from system outputs",
        "modes": {
            "distribution": "real extraction + real deterministic security gate + lexical/token proxy ranking",
            "layer1": "real extraction + real SearchUnit derivation + real SQLite BM25 ranking",
            "llm_answer_generation": "not_evaluated",
            "semantic_vector_retrieval": "not_evaluated",
            "document_support_reranking": (
                "real product reranker; bounded top-3 distinct Evidence support; "
                "lexical/token proxy inputs in this offline run"
            ),
            "external_network_used": False,
        },
        "coverage": {
            "dataset_files": sum(path.is_file() for path in corpus.rglob("*")),
            "case_count": len(cases),
            "formats": covered_formats,
            "not_yet_covered": pending_file_formats + ["layout reasoning", "answer synthesis"],
            "distribution": {
                "document_count": distribution_coverage["document_count"],
                "evidence_count": distribution_coverage["evidence_count"],
                "status_counts": distribution_coverage["status_counts"],
            },
            "layer1": {
                "build_status": layer1_build["state"]["build_status"],
                "input_files": len(layer1_build["state"]["input_paths"]),
                "statuses": dict(sorted(Counter(
                    entry["status"] for entry in layer1_build["state"]["entries"].values()
                ).items())),
            },
            "layer1_adapter": {
                "document_count": adapter_state["outputs"]["documents"]["count"],
                "evidence_count": adapter_state["outputs"]["evidence"]["count"],
                "requires_content_security_gate": adapter_state["requires_content_security_gate"],
                "text_projection_counts": adapter_state["text_projection_counts"],
                "search_unit_projection": adapter_state["search_unit_projection"],
            },
        },
        "expected_phrase_coverage": {
            "distribution": phrase_coverage(cases, distribution_dir),
            "layer1_adapter": phrase_coverage(cases, adapter_dir),
        },
        "relationship_context_audit": relationship_context_audit(cases, adapter_dir),
        "retrieval_comparison": comparisons,
        "safety_audit": safety_audit(
            cases, distribution_dir, adapter_dir,
            distribution_search, adapter_search, layer1_search,
        ),
        "decision_rule": (
            "Do not move the 40k-line implementation wholesale. Migrate one generic capability only after "
            "its extraction coverage is preserved and the fixed retrieval/safety checks do not regress."
        ),
        "limitations": [
            "The distribution score is a deliberately labelled offline proxy, not its full semantic hybrid retriever.",
            "The corpus is small and synthetic, so this run is a migration guardrail rather than a production-quality estimate.",
            "Layer 1 is intentionally evaluated without a safety gate; production use must keep it behind the distribution boundary.",
        ],
    }
    report_path = output / "shadow-evaluation-report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    summary = {
        "report": str(report_path),
        "retrieval": {item["method"]: item["metrics"] for item in comparisons},
        "safety_all_pass": report["safety_audit"]["all_pass"],
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
