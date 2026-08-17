#!/usr/bin/env python3
"""Build a conservative, reproducible overlay on an existing submission.

Every structured candidate is derived from ``index,question`` and source data
before the base predictions file is opened.  By default, an eligible candidate
can replace only an answer whose exact value is ``わかりません``.  Additional
exact replacement values require an explicit repeatable ``--replace-exact``.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import re
import sys
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
RAG = ROOT / "rag"
if str(RAG) not in sys.path:
    sys.path.insert(0, str(RAG))

from answer import validate_graph_answer  # noqa: E402
from glossary import build_glossary  # noqa: E402
from question_graph_runtime import (  # noqa: E402
    GRAPH_PLAN_VERSION,
    build_graph_plan,
)
from structured_candidate import (  # noqa: E402
    CANDIDATE_VERSION,
    StructuredCandidateEngine,
)


DEFAULT_REPLACE_EXACT = "わかりません"
FIXED_ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)
FORBIDDEN_QUESTION_NAME_TOKENS = frozenset({"gold", "valid", "validation"})


@dataclass(frozen=True)
class QuestionInput:
    rows: tuple[tuple[str, str], ...]
    sha256: str


@dataclass(frozen=True)
class PredictionInput:
    rows: tuple[tuple[str, str], ...]
    file_sha256: str
    payload_sha256: str
    input_format: str


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_text(value: str) -> str:
    return _sha256_bytes(value.encode("utf-8"))


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _decoded_csv_rows(raw: bytes, *, label: str) -> list[list[str]]:
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError(f"{label} must be UTF-8 CSV") from exc
    return list(csv.reader(io.StringIO(text, newline="")))


def _path_tokens(path: Path) -> set[str]:
    tokens: set[str] = set()
    for part in path.parts:
        tokens.update(
            token
            for token in re.split(r"[^a-z0-9]+", part.casefold())
            if token
        )
    return tokens


def _reject_gold_or_valid_questions(path: Path) -> None:
    original_tokens = _path_tokens(path)
    try:
        resolved_tokens = _path_tokens(path.resolve(strict=True))
    except OSError:
        resolved_tokens = set()
    if (original_tokens | resolved_tokens) & FORBIDDEN_QUESTION_NAME_TOKENS:
        raise ValueError("gold/valid question inputs are forbidden")


def _read_questions(path: Path) -> QuestionInput:
    # Reject known answer-bearing/evaluation paths before opening them.
    _reject_gold_or_valid_questions(path)
    raw = path.read_bytes()
    rows = _decoded_csv_rows(raw, label="questions input")
    if not rows or rows[0] != ["index", "question"]:
        raise ValueError(
            "questions input must contain exactly index,question; "
            "gold/valid/answer columns are forbidden"
        )
    questions: list[tuple[str, str]] = []
    for row_number, row in enumerate(rows[1:], 2):
        if len(row) != 2 or not row[0].strip() or not row[1].strip():
            raise ValueError(f"invalid question row {row_number}")
        questions.append((row[0], row[1]))
    indices = [index for index, _ in questions]
    if len(indices) != len(set(indices)):
        raise ValueError("question indices must be unique")
    return QuestionInput(tuple(questions), _sha256_bytes(raw))


def _prediction_payload(path: Path) -> tuple[bytes, bytes, str]:
    raw = path.read_bytes()
    suffix = path.suffix.casefold()
    if suffix == ".csv":
        return raw, raw, "csv"
    if suffix != ".zip":
        raise ValueError("base predictions must be a .csv or .zip file")
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            if archive.namelist() != ["predictions.csv"]:
                raise ValueError(
                    "base ZIP must contain only predictions.csv at its root"
                )
            info = archive.getinfo("predictions.csv")
            if info.is_dir() or info.flag_bits & 0x1:
                raise ValueError("base predictions.csv must be a readable file")
            payload = archive.read(info)
    except zipfile.BadZipFile as exc:
        raise ValueError("base predictions ZIP is invalid") from exc
    return raw, payload, "zip"


def _read_predictions(
    path: Path,
    expected_indices: Sequence[str],
) -> PredictionInput:
    raw, payload, input_format = _prediction_payload(path)
    rows = _decoded_csv_rows(payload, label="base predictions")
    if any(len(row) != 2 for row in rows):
        raise ValueError("base predictions must be headerless two-column CSV")
    indices = [row[0] for row in rows]
    if indices != list(expected_indices):
        raise ValueError("base prediction indices/order do not match questions")
    if len(indices) != len(set(indices)):
        raise ValueError("base prediction indices must be unique")
    if any(not row[1].strip() for row in rows):
        raise ValueError("base predictions cannot contain an empty answer")
    return PredictionInput(
        rows=tuple((row[0], row[1]) for row in rows),
        file_sha256=_sha256_bytes(raw),
        payload_sha256=_sha256_bytes(payload),
        input_format=input_format,
    )


def _candidate_record(
    engine: StructuredCandidateEngine,
    index: str,
    question: str,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "index": index,
        "question_sha256": _sha256_text(question),
        "graph_plan_version": GRAPH_PLAN_VERSION,
        "qur_sha256": None,
        "strict_status": "error",
        "strict_reasons": [],
        "decision_status": "not_run",
        "decision_reason": "graph_plan_error",
        "candidate_answer": None,
        "candidate_answer_sha256": None,
        "candidate_eligible": False,
        "candidate_rejection_reasons": [],
        "output_validation_status": "not_run",
        "output_contract_violations": [],
        "source_paths": [],
        "source_sha256": None,
        "operation_count": None,
        "output_count": None,
        "error": None,
    }
    try:
        plan = build_graph_plan(index, question, fast_advisory=True)
        record["qur_sha256"] = getattr(plan, "qur_sha256", None)
        record["strict_status"] = getattr(plan, "strict_status", "error")
        record["strict_reasons"] = list(
            getattr(plan, "strict_reasons", ()) or ()
        )

        decision = engine.decide_from_graph(index, question, plan)
        record["decision_status"] = getattr(decision, "status", "error")
        record["decision_reason"] = getattr(
            decision, "reason", "missing_decision_reason"
        )
        result = getattr(decision, "result", None)
        if result is not None:
            answer = getattr(result, "answer", None)
            if isinstance(answer, str):
                record["candidate_answer"] = answer
                record["candidate_answer_sha256"] = _sha256_text(answer)
                violations = tuple(validate_graph_answer(answer, plan))
                record["output_validation_status"] = (
                    "fail" if violations else "pass"
                )
                record["output_contract_violations"] = list(violations)
            else:
                violations = ("candidate_answer_not_text",)
                record["output_validation_status"] = "fail"
                record["output_contract_violations"] = list(violations)
            record["source_paths"] = list(
                getattr(result, "source_paths", ()) or ()
            )
            record["source_sha256"] = getattr(result, "source_sha256", None)
            record["operation_count"] = getattr(
                result, "operation_count", None
            )
            record["output_count"] = getattr(result, "output_count", None)
        else:
            violations = ()

        rejection_reasons: list[str] = []
        if record["strict_status"] != "pass":
            rejection_reasons.append("graph_strict_status_not_pass")
        if record["decision_status"] != "resolved":
            rejection_reasons.append("decision_not_resolved")
        if result is None:
            rejection_reasons.append("resolved_result_missing")
        elif not isinstance(record["candidate_answer"], str) or not record[
            "candidate_answer"
        ].strip():
            rejection_reasons.append("candidate_answer_empty_or_invalid")
        if violations:
            rejection_reasons.append("output_contract_violations")
        record["candidate_rejection_reasons"] = rejection_reasons
        record["candidate_eligible"] = not rejection_reasons
    except Exception as exc:  # Preserve the base answer on any candidate failure.
        record["candidate_rejection_reasons"] = ["candidate_pipeline_error"]
        record["error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
    return record


def _csv_bytes(rows: Sequence[tuple[str, str]]) -> bytes:
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerows(rows)
    return buffer.getvalue().encode("utf-8")


def _zip_bytes(predictions_csv: bytes) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(
        buffer,
        "w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=9,
    ) as archive:
        info = zipfile.ZipInfo("predictions.csv", FIXED_ZIP_TIMESTAMP)
        info.compress_type = zipfile.ZIP_DEFLATED
        info.create_system = 3
        info.external_attr = 0o100644 << 16
        archive.writestr(
            info,
            predictions_csv,
            compress_type=zipfile.ZIP_DEFLATED,
            compresslevel=9,
        )
    return buffer.getvalue()


def _json_bytes(value: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _assert_outputs_available(paths: Sequence[Path]) -> None:
    identities: set[str] = set()
    for path in paths:
        identity = os.path.normcase(str(path.resolve(strict=False)))
        if identity in identities:
            raise ValueError("output paths must be distinct")
        identities.add(identity)
        if path.exists() or path.is_symlink():
            raise ValueError(f"refusing to overwrite output: {path}")


def _fsync_directories(paths: Sequence[Path]) -> None:
    for parent in sorted({path.parent for path in paths}, key=str):
        descriptor = os.open(parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _atomic_write_bundle(payloads: Sequence[tuple[Path, bytes]]) -> None:
    paths = [path for path, _ in payloads]
    _assert_outputs_available(paths)
    for path in paths:
        path.parent.mkdir(parents=True, exist_ok=True)

    staged: list[tuple[Path, Path]] = []
    created: list[tuple[Path, Path]] = []
    try:
        for path, payload in payloads:
            descriptor, name = tempfile.mkstemp(
                dir=path.parent,
                prefix=f".{path.name}.",
                suffix=".tmp",
            )
            temporary = Path(name)
            staged.append((path, temporary))
            try:
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.chmod(temporary, 0o644)
            except Exception:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
                raise

        # A hard-link commit is atomic and, unlike os.replace, cannot overwrite
        # a file created after the preflight check.
        for path, temporary in staged:
            os.link(temporary, path)
            created.append((path, temporary))
        _fsync_directories(paths)
    except Exception:
        for path, temporary in reversed(created):
            try:
                if path.exists() and temporary.exists() and os.path.samefile(
                    path, temporary
                ):
                    path.unlink()
            except OSError:
                pass
        raise
    finally:
        for _, temporary in staged:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


def _replace_values(additional: Sequence[str]) -> tuple[str, ...]:
    values = [DEFAULT_REPLACE_EXACT]
    for value in additional:
        if not isinstance(value, str) or not value:
            raise ValueError("replace-exact values must be non-empty strings")
        if value not in values:
            values.append(value)
    return tuple(values)


def build_submission(
    questions_path: Path,
    base_predictions_path: Path,
    source_root: Path,
    output_csv: Path,
    output_zip: Path,
    log_path: Path,
    *,
    replace_exact: Sequence[str] = (),
) -> dict[str, Any]:
    outputs = (output_csv, output_zip, log_path)
    _assert_outputs_available(outputs)
    replace_values = _replace_values(replace_exact)
    questions_input = _read_questions(questions_path)

    glossary = build_glossary(source_root)
    engine = StructuredCandidateEngine(source_root, glossary)

    # This pass is intentionally complete before _read_predictions is called.
    # No base answer can affect question understanding, graph selection, source
    # resolution, calculation, or output-contract validation.
    candidate_records = [
        _candidate_record(engine, index, question)
        for index, question in questions_input.rows
    ]

    base_input = _read_predictions(
        base_predictions_path,
        [index for index, _ in questions_input.rows],
    )
    base_by_index = {index: answer for index, answer in base_input.rows}

    final_rows: list[tuple[str, str]] = []
    adopted_count = 0
    changed_count = 0
    for record in candidate_records:
        index = str(record["index"])
        base_answer = base_by_index[index]
        replace_match = base_answer in replace_values
        adopted = bool(record["candidate_eligible"] and replace_match)
        candidate_answer = record["candidate_answer"]
        final_answer = (
            str(candidate_answer)
            if adopted and isinstance(candidate_answer, str)
            else base_answer
        )
        changed = final_answer != base_answer
        if adopted:
            adopted_count += 1
        if changed:
            changed_count += 1
        record.update(
            {
                "base_answer_sha256": _sha256_text(base_answer),
                "base_replace_exact_match": replace_match,
                "adopted": adopted,
                "adoption_reason": (
                    "base_exact_replace_match"
                    if adopted
                    else (
                        "candidate_ineligible"
                        if not record["candidate_eligible"]
                        else "base_answer_not_in_replace_exact"
                    )
                ),
                "changed": changed,
                "final_answer_sha256": _sha256_text(final_answer),
            }
        )
        final_rows.append((index, final_answer))

    output_csv_bytes = _csv_bytes(final_rows)
    output_zip_bytes = _zip_bytes(output_csv_bytes)
    log: dict[str, Any] = {
        "builder_version": "0.1",
        "candidate_version": CANDIDATE_VERSION,
        "graph_plan_version": GRAPH_PLAN_VERSION,
        "fast_advisory": True,
        "candidate_input_fields": ["index", "question"],
        "candidate_pass_completed_before_base_read": True,
        "question_count": len(questions_input.rows),
        "eligible_count": sum(
            bool(record["candidate_eligible"]) for record in candidate_records
        ),
        "adopted_count": adopted_count,
        "changed_count": changed_count,
        "replace_exact": list(replace_values),
        "inputs": {
            "questions_path": str(questions_path.resolve()),
            "questions_sha256": questions_input.sha256,
            "base_predictions_path": str(base_predictions_path.resolve()),
            "base_predictions_format": base_input.input_format,
            "base_predictions_file_sha256": base_input.file_sha256,
            "base_predictions_payload_sha256": base_input.payload_sha256,
            "source_root": str(source_root.resolve()),
        },
        "candidates": candidate_records,
        "outputs": {
            "csv_path": str(output_csv.resolve()),
            "csv_sha256": _sha256_bytes(output_csv_bytes),
            "zip_path": str(output_zip.resolve()),
            "zip_sha256": _sha256_bytes(output_zip_bytes),
            "zip_member": "predictions.csv",
            "zip_timestamp": list(FIXED_ZIP_TIMESTAMP),
            "log_path": str(log_path.resolve()),
            "log_payload_sha256_basis": (
                "canonical JSON of this record before log_payload_sha256 is added"
            ),
        },
    }
    log["outputs"]["log_payload_sha256"] = _sha256_bytes(_canonical_json(log))
    log_bytes = _json_bytes(log)

    _atomic_write_bundle(
        (
            (output_csv, output_csv_bytes),
            (output_zip, output_zip_bytes),
            (log_path, log_bytes),
        )
    )
    return log


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--questions", required=True, type=Path)
    parser.add_argument("--base-predictions", required=True, type=Path)
    parser.add_argument(
        "--source-root",
        type=Path,
        default=ROOT / "share" / "共有ドライブ",
    )
    parser.add_argument("--output-csv", required=True, type=Path)
    parser.add_argument("--output-zip", required=True, type=Path)
    parser.add_argument("--log", required=True, type=Path)
    parser.add_argument(
        "--replace-exact",
        action="append",
        default=[],
        metavar="VALUE",
        help=(
            "also allow replacement when the base answer exactly equals VALUE; "
            "repeatable"
        ),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        log = build_submission(
            args.questions.resolve(),
            args.base_predictions.resolve(),
            args.source_root.resolve(),
            args.output_csv.resolve(),
            args.output_zip.resolve(),
            args.log.resolve(),
            replace_exact=args.replace_exact,
        )
    except (OSError, UnicodeError, ValueError, zipfile.BadZipFile) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(
        f"OK: eligible={log['eligible_count']} adopted={log['adopted_count']} "
        f"changed={log['changed_count']} rows={log['question_count']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
