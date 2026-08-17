#!/usr/bin/env python3
"""Build deterministic, question-only QuestionClauseIR records.

Only ``question_id`` and ``original_question`` are accepted as semantic input.
Unsupported questions are retained as an incomplete unresolved record; they
are never guessed into a supported grammar.
"""

from __future__ import annotations

import argparse
import json
import os
import stat
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from question_language_registry import (
    REGISTRY_NAME,
    REGISTRY_VERSION,
    registry_digest,
)
from validate_question_clause_ir import (
    PARSER,
    PARSER_VERSION,
    RULE_VERSION,
    canonical_json,
    deterministic_clause_id,
    deterministic_ir_id,
    load_json_records,
    parse_certified_question,
    question_sha256,
    validate_question_clause_ir,
    validate_question_input,
)


def _timestamp(value: str | None) -> str:
    if value is None:
        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    if not isinstance(value, str) or not value:
        raise ValueError("generated_at must be a non-empty RFC 3339 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("generated_at must be an RFC 3339 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError("generated_at must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def build_question_clause_ir(
    question_input: dict[str, Any],
    *,
    generated_at: str | None = None,
    qic: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Compile and validate one question-only ClauseIR record."""

    input_errors = validate_question_input(question_input)
    if input_errors:
        raise ValueError("invalid question input: " + "; ".join(input_errors[:8]))
    question = question_input["original_question"]
    question_hash = question_sha256(question)
    grammar_profile, id_free_clauses = parse_certified_question(question)
    clauses: list[dict[str, Any]] = []
    for value in id_free_clauses:
        clause = dict(value)
        clause["clause_id"] = deterministic_clause_id(
            question_hash, grammar_profile, clause
        )
        # Canonical field order is irrelevant to JSON semantics but keeping the
        # identifier first makes diagnostic exports easier to inspect.
        clause = {"clause_id": clause.pop("clause_id"), **clause}
        clauses.append(clause)

    unresolved = [
        clause["clause_id"]
        for clause in clauses
        if clause["disposition"] == "unresolved"
    ]
    conflicts = [
        clause["clause_id"]
        for clause in clauses
        if clause["disposition"] == "conflict"
    ]
    covered = sum(
        clause["span"]["end"] - clause["span"]["start"]
        for clause in clauses
        if clause["disposition"] in {"mapped", "syntax"}
    )
    status = "conflict" if conflicts else ("incomplete" if unresolved else "complete")
    record: dict[str, Any] = {
        "schema_version": "0.1",
        "record_type": "question_clause_ir",
        "question_clause_ir_id": "qcir_" + "0" * 20,
        "question_id": question_input["question_id"],
        "original_question": question,
        "grammar_profile": grammar_profile,
        "clauses": clauses,
        "coverage": {
            "status": status,
            "total_codepoints": len(question),
            "covered_codepoints": covered,
            "unresolved_clause_refs": unresolved,
            "conflict_clause_refs": conflicts,
            "unbound_qic_paths": [],
        },
        "provenance": {
            "parser": PARSER,
            "parser_version": PARSER_VERSION,
            "registry_name": REGISTRY_NAME,
            "registry_version": REGISTRY_VERSION,
            "registry_sha256": registry_digest(),
            "rule_version": RULE_VERSION,
            "generated_at": _timestamp(generated_at),
            "input_question_sha256": question_hash,
            "deterministic": True,
            "question_only": True,
            "catalog_used": False,
            "answer_data_used": False,
            "past_answers_used": False,
        },
    }
    record["question_clause_ir_id"] = deterministic_ir_id(record)
    errors = validate_question_clause_ir(record, qic)
    if errors:
        raise ValueError("compiled QuestionClauseIR is invalid: " + "; ".join(errors[:12]))
    return record


def build_many(
    question_inputs: Iterable[dict[str, Any]],
    *,
    generated_at: str | None = None,
    qics: Sequence[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    values = list(question_inputs)
    if qics is not None and len(qics) != len(values):
        raise ValueError("QIC record count must equal question record count")
    timestamp = _timestamp(generated_at)
    return [
        build_question_clause_ir(
            value,
            generated_at=timestamp,
            qic=None if qics is None else qics[index],
        )
        for index, value in enumerate(values)
    ]


def _atomic_write_jsonl(path: Path, records: Iterable[dict[str, Any]]) -> None:
    path = Path(os.path.abspath(path.expanduser()))
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and (path.is_symlink() or not path.is_file()):
        raise ValueError(f"output must be a regular non-symlink file: {path}")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            for record in records:
                handle.write(canonical_json(record))
                handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        if stat.S_ISLNK(os.lstat(temporary).st_mode):
            raise ValueError("temporary output unexpectedly became a symlink")
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="question input JSON or JSONL")
    parser.add_argument("--output", required=True, type=Path, help="ClauseIR JSONL")
    parser.add_argument("--qic", type=Path, help="optional aligned QIC JSON or JSONL")
    parser.add_argument("--generated-at")
    args = parser.parse_args()
    try:
        questions = load_json_records(args.input)
        qics = load_json_records(args.qic) if args.qic is not None else None
        records = build_many(
            questions,
            generated_at=args.generated_at,
            qics=qics,
        )
        _atomic_write_jsonl(args.output, records)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(canonical_json({"records": len(records), "output": str(args.output)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
