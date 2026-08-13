#!/usr/bin/env python3
"""Build baseline intermediate records from a directory or explicit files.

The builder is question-independent.  It recursively discovers supported
office/PDF files, performs no arbitrary leaf sampling, and does not connect the
result to the answer pipeline.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from probe_intermediate_records import Probe, canonical_json


SUPPORTED_SUFFIXES = {".docx", ".xlsx", ".pptx", ".pdf"}
EXTRACTOR = "intermediate-record-extractor"
EXTRACTOR_VERSION = "0.2.0"


def discover(root: Path) -> list[Path]:
    return sorted(
        (
            path for path in root.rglob("*")
            if path.is_file()
            and path.suffix.lower() in SUPPORTED_SUFFIXES
            and not path.name.startswith("~$")
        ),
        key=lambda path: path.as_posix(),
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path, help="source root used for relative paths")
    parser.add_argument("--out", required=True, type=Path, help="new output directory")
    parser.add_argument("--input", type=Path, nargs="*", help="optional explicit files; otherwise discover recursively")
    parser.add_argument("--run-at", default=datetime.now(timezone.utc).isoformat())
    parser.add_argument("--fail-fast", action="store_true", help="stop on the first unreadable file")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    root = args.root.resolve()
    output = args.out.resolve()
    if not root.is_dir():
        raise SystemExit(f"--root is not a directory: {root}")
    if output.exists():
        if not output.is_dir():
            raise SystemExit(f"--out exists and is not a directory: {output}")
        if any(output.iterdir()):
            raise SystemExit(f"refusing to overwrite non-empty output directory: {output}")
    try:
        output.relative_to(root)
    except ValueError:
        pass
    else:
        raise SystemExit("--out must be outside --root to prevent recursive self-ingestion")

    inputs = sorted({path.resolve() for path in args.input}, key=lambda path: path.as_posix()) if args.input else discover(root)
    if not inputs:
        raise SystemExit("no supported input files found")
    unsupported = [path for path in inputs if path.suffix.lower() not in SUPPORTED_SUFFIXES]
    if unsupported:
        raise SystemExit(f"unsupported explicit input: {unsupported[0]}")
    for path in inputs:
        if not path.is_file():
            raise SystemExit(f"input is not a file: {path}")
        try:
            path.relative_to(root)
        except ValueError:
            raise SystemExit(f"input is outside --root: {path}") from None

    extractor = Probe(
        root, args.run_at, None,
        diagnostic=False,
        extractor=EXTRACTOR,
        extractor_version=EXTRACTOR_VERSION,
    )
    failures = 0
    for path in inputs:
        try:
            extractor.extract(path)
        except Exception as error:
            failures += 1
            extractor.record_failure(path, error)
            if args.fail_fast:
                raise
    extractor.write(output)
    print(canonical_json({
        "documents": len(extractor.documents),
        "evidence": len(extractor.evidence),
        "relations": len(extractor.relations),
        "failed_documents": failures,
        "input_files": len(inputs),
        "output": str(output),
        "extractor": EXTRACTOR,
        "extractor_version": EXTRACTOR_VERSION,
    }))


if __name__ == "__main__":
    main()
