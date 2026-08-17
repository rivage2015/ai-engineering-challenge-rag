#!/usr/bin/env python3
"""Overlay independently computed source answers onto an existing submission.

The structured engine sees only ``index,question`` and the shared-drive source
root.  Existing predictions are loaded only after every candidate decision has
finished, so prior answers cannot influence question understanding, source
selection, or calculation.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import Sequence


ROOT = Path(__file__).resolve().parents[1]
RAG = ROOT / "rag"
if str(RAG) not in sys.path:
    sys.path.insert(0, str(RAG))

from glossary import build_glossary  # noqa: E402
from structured_candidate import (  # noqa: E402
    CANDIDATE_VERSION,
    StructuredCandidateEngine,
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _read_questions(path: Path) -> list[tuple[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.reader(handle))
    if not rows or rows[0] != ["index", "question"]:
        raise ValueError(
            "questions input must contain exactly index,question; answer columns are forbidden"
        )
    questions: list[tuple[str, str]] = []
    for row_number, row in enumerate(rows[1:], 2):
        if len(row) != 2 or not row[0] or not row[1]:
            raise ValueError(f"invalid question row {row_number}")
        questions.append((row[0], row[1]))
    indices = [index for index, _ in questions]
    if len(indices) != len(set(indices)):
        raise ValueError("question indices must be unique")
    return questions


def _read_predictions(
    path: Path,
    expected_indices: Sequence[str],
) -> dict[str, str]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.reader(handle))
    if any(len(row) != 2 for row in rows):
        raise ValueError("base predictions must be headerless two-column CSV")
    indices = [row[0] for row in rows]
    if indices != list(expected_indices):
        raise ValueError("base prediction indices/order do not match questions")
    if any(not row[1].strip() for row in rows):
        raise ValueError("base predictions cannot contain an empty answer")
    return {index: answer for index, answer in rows}


def _atomic_csv(path: Path, rows: Sequence[tuple[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() or path.is_symlink():
        raise ValueError(f"refusing to overwrite output: {path}")
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        newline="",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    ) as handle:
        temporary = Path(handle.name)
        writer = csv.writer(handle)
        writer.writerows(rows)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() or path.is_symlink():
        raise ValueError(f"refusing to overwrite output: {path}")
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
        allow_nan=False,
    ) + "\n"
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    ) as handle:
        temporary = Path(handle.name)
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _atomic_zip(path: Path, csv_path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() or path.is_symlink():
        raise ValueError(f"refusing to overwrite output: {path}")
    descriptor, name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    os.close(descriptor)
    temporary = Path(name)
    try:
        with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.write(csv_path, "predictions.csv")
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def build_submission(
    questions_path: Path,
    base_predictions_path: Path,
    source_root: Path,
    output_csv: Path,
    output_zip: Path,
    log_path: Path,
) -> dict[str, object]:
    questions = _read_questions(questions_path)
    glossary = build_glossary(source_root)
    engine = StructuredCandidateEngine(source_root, glossary)

    # Complete the source-only pass before opening prior predictions.
    decisions = {
        index: engine.decide(index, question)
        for index, question in questions
    }
    base = _read_predictions(
        base_predictions_path,
        [index for index, _ in questions],
    )

    rows: list[tuple[str, str]] = []
    resolved: list[dict[str, object]] = []
    errors: list[dict[str, str]] = []
    changed = 0
    for index, _ in questions:
        decision = decisions[index]
        answer = base[index]
        if decision.status == "resolved" and decision.result is not None:
            answer = decision.result.answer
            changed += answer != base[index]
            resolved.append(
                {
                    "answer": answer,
                    "changed": answer != base[index],
                    "index": index,
                    "operation_count": decision.result.operation_count,
                    "reason": decision.reason,
                    "source_paths": list(decision.result.source_paths),
                    "source_sha256": decision.result.source_sha256,
                }
            )
        elif decision.status == "error":
            errors.append({"index": index, "reason": decision.reason})
        rows.append((index, answer))

    _atomic_csv(output_csv, rows)
    _atomic_zip(output_zip, output_csv)
    log = {
        "base_predictions_sha256": _sha256(base_predictions_path),
        "candidate_version": CANDIDATE_VERSION,
        "changed_count": changed,
        "error_count": len(errors),
        "errors": errors,
        "output_csv_sha256": _sha256(output_csv),
        "output_zip_sha256": _sha256(output_zip),
        "question_count": len(questions),
        "questions_sha256": _sha256(questions_path),
        "resolved": resolved,
        "resolved_count": len(resolved),
        "source_root": str(source_root.resolve()),
    }
    _atomic_json(log_path, log)
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
        )
    except (OSError, UnicodeDecodeError, ValueError, zipfile.BadZipFile) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(
        f"OK: resolved={log['resolved_count']} changed={log['changed_count']} "
        f"errors={log['error_count']} rows={log['question_count']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
