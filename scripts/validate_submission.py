#!/usr/bin/env python3
"""Validate the headerless two-column competition submission format."""

from __future__ import annotations

import argparse
import csv
import io
import zipfile
from pathlib import Path


def read_predictions(path: Path) -> list[list[str]]:
    if path.suffix.lower() == ".zip":
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            if names != ["predictions.csv"]:
                raise ValueError("ZIP must contain only predictions.csv at its root")
            text = archive.read("predictions.csv").decode("utf-8-sig")
            return list(csv.reader(io.StringIO(text)))
    if path.suffix.lower() != ".csv":
        raise ValueError("predictions must be a .csv or .zip file")
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.reader(handle))


def load_expected_indices(path: Path) -> list[str]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.reader(handle))
    if not rows or rows[0][:2] != ["index", "question"]:
        raise ValueError("questions CSV must start with index,question")
    return [row[0] for row in rows[1:]]


def token_counts(answers: list[str], model: str) -> tuple[list[int] | None, str | None]:
    try:
        import tiktoken
        try:
            encoding = tiktoken.encoding_for_model(model)
        except KeyError:
            encoding = tiktoken.get_encoding("cl100k_base")
    except Exception as exc:
        return None, f"token limit was not checked: {type(exc).__name__}"
    return [len(encoding.encode(answer)) for answer in answers], None


def validate(questions: Path, predictions: Path, max_tokens: int, model: str) -> dict:
    expected = load_expected_indices(questions)
    rows = read_predictions(predictions)
    if any(len(row) != 2 for row in rows):
        raise ValueError("every predictions row must have exactly two columns")
    indices = [row[0] for row in rows]
    answers = [row[1] for row in rows]
    if indices != expected:
        raise ValueError("prediction indices must exactly match question order and count")
    if len(set(indices)) != len(indices):
        raise ValueError("prediction indices must be unique")
    if any(not answer.strip() for answer in answers):
        raise ValueError("answers must not be empty")
    counts, warning = token_counts(answers, model)
    if counts is not None and max(counts, default=0) > max_tokens:
        row = counts.index(max(counts))
        raise ValueError(
            f"answer {indices[row]} has {counts[row]} tokens; maximum is {max_tokens}"
        )
    return {
        "rows": len(rows),
        "first_index": indices[0] if indices else None,
        "last_index": indices[-1] if indices else None,
        "max_tokens": max(counts, default=0) if counts is not None else None,
        "warning": warning,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--questions", required=True, type=Path)
    parser.add_argument("--predictions", required=True, type=Path)
    parser.add_argument("--max-tokens", type=int, default=1000)
    parser.add_argument("--model", default="gpt-5.2-2025-12-11")
    args = parser.parse_args()
    result = validate(
        args.questions.resolve(), args.predictions.resolve(), args.max_tokens, args.model
    )
    print(f"OK: {result['rows']} rows / index {result['first_index']}..{result['last_index']}")
    if result["max_tokens"] is not None:
        print(f"maximum answer tokens: {result['max_tokens']} / {args.max_tokens}")
    if result["warning"]:
        print(f"WARNING: {result['warning']}")


if __name__ == "__main__":
    main()
