#!/usr/bin/env python3
"""Validate AnswerSourceAudit records and their fail-closed semantics."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

import jsonschema

ROOT = Path(__file__).resolve().parents[1]


def sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def validate(path: Path, schema_path: Path) -> list[str]:
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    validator = jsonschema.Draft202012Validator(schema)
    errors: list[str] = []
    seen: set[str] = set()
    previous = -1
    with path.open(encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, 1):
            record = json.loads(line)
            for issue in validator.iter_errors(record):
                errors.append(f"line {line_no}: schema: {issue.message}")
            qid = record.get("question_id", "")
            if qid in seen: errors.append(f"line {line_no}: duplicate question_id")
            seen.add(qid)
            if qid.isdigit() and int(qid) <= previous: errors.append(f"line {line_no}: questions are not strictly sorted")
            if qid.isdigit(): previous = int(qid)
            if record.get("question_sha256") != sha(record.get("question", "")): errors.append(f"line {line_no}: question hash mismatch")
            if record.get("current_answer_sha256") != sha(record.get("current_answer", "")): errors.append(f"line {line_no}: answer hash mismatch")
            verification = record.get("verification", {})
            status = record.get("audit_status")
            if (status in {"verified", "contradicted"}) != bool(verification.get("proof_complete")):
                errors.append(f"line {line_no}: proof/status mismatch")
            if verification.get("method") == "retrieval_observation_only" and status != "unverified":
                errors.append(f"line {line_no}: retrieval-only evidence cannot verify or contradict")
            if bool(verification.get("verified_source_paths")) != bool(verification.get("proof_complete")):
                errors.append(f"line {line_no}: verified source paths/proof mismatch")
            retrieval = record.get("retrieval", {})
            if retrieval.get("query_sha256") != record.get("question_sha256"):
                errors.append(f"line {line_no}: retrieval query is not question-only")
            results = retrieval.get("results", [])
            if retrieval.get("result_count") != len(results): errors.append(f"line {line_no}: result count mismatch")
            if retrieval.get("answer_exact_occurrences") != sum(bool(x.get("contains_current_answer_exact")) for x in results):
                errors.append(f"line {line_no}: answer occurrence count mismatch")
            for result in results:
                if result.get("text_sha256") != sha(result.get("text", "")): errors.append(f"line {line_no}: result text hash mismatch")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__); parser.add_argument("input", type=Path)
    parser.add_argument("--schema", type=Path, default=ROOT / "schemas" / "answer-source-audit.schema.json")
    args = parser.parse_args()
    try: errors = validate(args.input, args.schema)
    except (OSError, ValueError, json.JSONDecodeError) as exc: print(f"ERROR: {exc}", file=sys.stderr); return 1
    if errors:
        for error in errors[:50]: print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print(json.dumps({"valid": True, "input": str(args.input)}, ensure_ascii=False)); return 0


if __name__ == "__main__": raise SystemExit(main())
