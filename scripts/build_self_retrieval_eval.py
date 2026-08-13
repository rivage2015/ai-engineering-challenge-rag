#!/usr/bin/env python3
"""Build a deterministic retrieval wiring benchmark from SearchUnits."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from collections import defaultdict, deque
from pathlib import Path
from typing import Any

from lexical_search_common import canonical_json, digest_file, tokenize


GENERATOR = "self-retrieval-eval-builder"
GENERATOR_VERSION = "0.1.0"
EVAL_FILE = "evaluation-set.jsonl"
STATE_FILE = "evaluation-set-state.json"
WHITESPACE = re.compile(r"\s+")


def stable_id(prefix: str, value: Any) -> str:
    digest = hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()
    return f"{prefix}_{digest[:32]}"


def query_from_unit(unit: dict[str, Any], query_chars: int) -> str:
    text = unit["text"]["search_text"]
    if unit["unit_type"] == "table_row":
        values = []
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            value = stripped.split(":", 1)[1].strip() if ":" in stripped else stripped
            if value and value not in values:
                values.append(value)
            if len(values) >= 4:
                break
        query = " ".join(values)
    else:
        query = WHITESPACE.sub(" ", text).strip()
    query = query[:query_chars].strip()
    if not tokenize(query):
        raise ValueError(f"SearchUnit has no queryable content: {unit['search_unit_id']}")
    return query


def round_robin_sample(groups: dict[str, list[dict[str, Any]]], maximum: int) -> list[dict[str, Any]]:
    queues = {key: deque(value) for key, value in sorted(groups.items())}
    selected: list[dict[str, Any]] = []
    while len(selected) < maximum and any(queues.values()):
        for key in sorted(queues):
            if queues[key] and len(selected) < maximum:
                selected.append(queues[key].popleft())
    return selected


def build(search_output: Path, output: Path, max_cases: int, query_chars: int) -> dict[str, Any]:
    if max_cases < 1:
        raise ValueError("--max-cases must be at least 1")
    if query_chars < 10:
        raise ValueError("--query-chars must be at least 10")
    if output.exists() and any(output.iterdir()):
        raise ValueError(f"output directory is not empty: {output}")
    output.mkdir(parents=True, exist_ok=True)
    search_state_path = search_output / "search-build-state.json"
    search_state = json.loads(search_state_path.read_text(encoding="utf-8"))
    units_path = search_output / search_state.get("output", {}).get("relative_path", "")
    if search_state.get("build_status") != "complete" or digest_file(units_path) != search_state.get("output", {}).get("sha256"):
        raise ValueError("SearchUnit input is incomplete or does not match its state")
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    with units_path.open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                unit = json.loads(line)
                groups[unit["unit_type"]].append(unit)
    selected = round_robin_sample(groups, min(max_cases, sum(map(len, groups.values()))))
    cases: list[dict[str, Any]] = []
    for unit in selected:
        query = query_from_unit(unit, query_chars)
        identity = {
            "query": query,
            "relevant_search_unit_ids": [unit["search_unit_id"]],
            "method": "self_retrieval",
            "generator": GENERATOR,
            "generator_version": GENERATOR_VERSION,
        }
        cases.append({
            "schema_version": "0.1",
            "record_type": "retrieval_eval_case",
            "eval_case_id": stable_id("qe", identity),
            "query": query,
            "relevant_search_unit_ids": [unit["search_unit_id"]],
            "category": unit["unit_type"],
            "provenance": {
                "method": "self_retrieval",
                "generator": GENERATOR,
                "generator_version": GENERATOR_VERSION,
                "deterministic": True,
                "source_search_unit_id": unit["search_unit_id"],
            },
        })
    eval_path = output / EVAL_FILE
    temporary_eval = output / f".{EVAL_FILE}.tmp"
    with temporary_eval.open("w", encoding="utf-8", newline="\n") as handle:
        for case in cases:
            handle.write(canonical_json(case) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary_eval, eval_path)
    counts = {key: sum(case["category"] == key for case in cases) for key in sorted(groups)}
    state = {
        "state_version": "1",
        "build_status": "complete",
        "generator": GENERATOR,
        "generator_version": GENERATOR_VERSION,
        "method": "self_retrieval",
        "deterministic": True,
        "limitations": [
            "Measures lexical retrieval wiring, not semantic question-answering quality.",
            "Queries are derived from the same SearchUnits used as relevance targets.",
        ],
        "source": {
            "search_units_sha256": search_state["output"]["sha256"],
            "search_state_sha256": digest_file(search_state_path),
        },
        "configuration": {"max_cases": max_cases, "query_chars": query_chars},
        "output": {
            "relative_path": EVAL_FILE,
            "sha256": digest_file(eval_path),
            "size_bytes": eval_path.stat().st_size,
            "record_count": len(cases),
        },
        "counts_by_category": counts,
    }
    temporary_state = output / f".{STATE_FILE}.tmp"
    with temporary_state.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(canonical_json(state) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary_state, output / STATE_FILE)
    return state


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--search-output", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--max-cases", type=int, default=100)
    parser.add_argument("--query-chars", type=int, default=80)
    args = parser.parse_args()
    print(canonical_json(build(args.search_output.resolve(), args.out.resolve(), args.max_cases, args.query_chars)))


if __name__ == "__main__":
    main()
