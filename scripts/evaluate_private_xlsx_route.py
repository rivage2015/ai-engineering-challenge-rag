#!/usr/bin/env python3
"""Probe private XLSX row retrieval without printing source text or identifiers.

The probe deterministically selects up to two row SearchUnit projections per
worksheet, builds a retrieval-only query from two fields in
the same row, and verifies that the expected row is returned by the existing
local hybrid retriever.  It reports aggregate counts only.  It does not call
the answer or audit model and does not write query or source text to disk.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any


def load_module(path: Path) -> Any:
    spec = importlib.util.spec_from_file_location("private_xlsx_answer_engine", path)
    if spec is None or spec.loader is None:
        raise ImportError("cannot load answer engine")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError(f"record {line_number} is not an object")
            records.append(value)
    return records


def query_fields(text: str) -> list[str]:
    fields: list[str] = []
    for line in text.splitlines():
        label, separator, value = line.partition(": ")
        if separator and label.strip() and value.strip():
            fields.append(f"{label.strip()} {value.strip()}")
    return fields


def select_cases(records: list[dict[str, Any]], per_sheet: int) -> list[dict[str, str]]:
    rows: dict[str, list[dict[str, str]]] = defaultdict(list)
    for record in records:
        adapter = record.get("adapter", {})
        locator = record.get("locator", {})
        if adapter.get("unit_type") != "table_row":
            continue
        sheet = locator.get("sheet_name")
        text = record.get("observed_text")
        evidence_id = record.get("evidence_id")
        if not isinstance(sheet, str) or not isinstance(text, str) or not isinstance(evidence_id, str):
            continue
        fields = query_fields(text)
        if len(fields) < 2:
            continue
        rows[sheet].append({
            "evidence_id": evidence_id,
            "query": " ".join(fields[:2]),
        })
    cases: list[dict[str, str]] = []
    for sheet in sorted(rows):
        cases.extend(rows[sheet][:per_sheet])
    return cases


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--semantic-evidence", required=True, type=Path)
    parser.add_argument("--index", required=True, type=Path)
    parser.add_argument("--answer-engine", required=True, type=Path)
    parser.add_argument("--per-sheet", type=int, default=2)
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--timeout", type=int, default=180)
    args = parser.parse_args()
    if not 1 <= args.per_sheet <= 5:
        raise SystemExit("--per-sheet must be between 1 and 5")
    if not 1 <= args.top_k <= 8:
        raise SystemExit("--top-k must be between 1 and 8")

    evidence_path = args.semantic_evidence.resolve(strict=True)
    index_path = args.index.resolve(strict=True)
    engine = load_module(args.answer_engine.resolve(strict=True))
    records = read_jsonl(evidence_path)
    worksheet_names = {
        record.get("locator", {}).get("sheet_name")
        for record in records
        if record.get("adapter", {}).get("unit_type") == "table_row"
        and isinstance(record.get("locator", {}).get("sheet_name"), str)
    }
    cases = select_cases(records, args.per_sheet)
    if not cases:
        raise SystemExit("no private row retrieval cases could be constructed")

    hit_at_1 = 0
    hit_at_k = 0
    for case in cases:
        _, retrieved = engine.retrieve_hybrid(
            index_path, case["query"], args.top_k, args.timeout
        )
        identifiers = [item.get("evidence_id") for item in retrieved]
        hit_at_1 += bool(identifiers and identifiers[0] == case["evidence_id"])
        hit_at_k += case["evidence_id"] in identifiers

    result = {
        "status": "PASS" if hit_at_k == len(cases) else "FAIL",
        "privacy": {
            "source_text_printed": False,
            "queries_printed": False,
            "identifiers_printed": False,
            "external_network_required": False,
            "answer_model_called": False,
        },
        "worksheet_count": len(worksheet_names),
        "case_count": len(cases),
        "hit_at_1": hit_at_1,
        f"hit_at_{args.top_k}": hit_at_k,
    }
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
