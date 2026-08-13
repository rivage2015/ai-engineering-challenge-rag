#!/usr/bin/env python3
"""Find chart-generating source without using evaluation questions or answers."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Iterator

SAVEFIG_RE = re.compile(
    r"savefig\s*\(\s*(?P<expression>[^\n,]+)", re.MULTILINE
)
IMAGE_LITERAL_RE = re.compile(r"[\"'](?P<path>[^\"']+\.(?:png|jpe?g|webp|svg))[\"']", re.IGNORECASE)
DATA_LITERAL_RE = re.compile(r"[\"'](?P<path>[^\"']+\.(?:csv|tsv|xlsx?|json|parquet))[\"']", re.IGNORECASE)
TRANSFORM_TOKENS = (
    "groupby", ".agg(", ".mean(", ".median(", ".sum(", ".size(",
    "pivot", "resample", "value_counts", "rolling",
)
PLOT_TOKENS = (".plot(", "sns.", "plt.bar(", "plt.scatter(", "plt.imshow(")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def source_units(path: Path) -> Iterator[tuple[str, str]]:
    if path.suffix == ".ipynb":
        notebook = json.loads(path.read_text(encoding="utf-8"))
        for index, cell in enumerate(notebook.get("cells", [])):
            if cell.get("cell_type") != "code":
                continue
            location = str(cell.get("id") or f"cell-{index}")
            yield location, "".join(cell.get("source", []))
    else:
        yield "file", path.read_text(encoding="utf-8", errors="replace")


def resolve_literal(root: Path, code_path: Path, literal: str) -> list[str]:
    literal_path = Path(literal)
    direct_candidates = [code_path.parent / literal_path, code_path.parent.parent / literal_path, root / literal_path]
    resolved = {str(path.resolve()) for path in direct_candidates if path.is_file()}
    if not resolved:
        resolved.update(str(path.resolve()) for path in root.rglob(literal_path.name) if path.is_file())
    return sorted(resolved)


def candidate_records(root: Path, image_name: str | None) -> list[dict[str, object]]:
    code_paths = sorted([*root.rglob("*.ipynb"), *root.rglob("*.py")])
    records: list[dict[str, object]] = []
    seen_ids: set[str] = set()
    for code_path in code_paths:
        raw = code_path.read_bytes()
        units = list(source_units(code_path))
        all_source = "\n".join(source for _, source in units)
        global_data_literals = sorted(set(match.group("path") for match in DATA_LITERAL_RE.finditer(all_source)))
        for location, source in units:
            for save_match in SAVEFIG_RE.finditer(source):
                expression = save_match.group("expression")
                image_match = IMAGE_LITERAL_RE.search(expression)
                if image_match is None:
                    continue
                image_literal = image_match.group("path")
                if image_name and Path(image_literal).name != Path(image_name).name:
                    continue
                data_literals = sorted(set(global_data_literals) | set(
                    match.group("path") for match in DATA_LITERAL_RE.finditer(source)
                ))
                image_paths = resolve_literal(root, code_path, image_literal)
                transforms = [token.rstrip("(").lstrip(".") for token in TRANSFORM_TOKENS if token in source]
                plot_calls = [token.rstrip("(").lstrip(".") for token in PLOT_TOKENS if token in source]
                identity = f"{code_path.resolve()}\0{location}\0{image_literal}".encode("utf-8")
                candidate_id = "cs_" + sha256_bytes(identity)[:24]
                if candidate_id in seen_ids:
                    continue
                seen_ids.add(candidate_id)
                records.append({
                    "schema_version": "0.1",
                    "record_type": "chart_source_candidate",
                    "candidate_id": candidate_id,
                    "image": {
                        "literal": image_literal,
                        "resolved_paths": image_paths,
                    },
                    "code": {
                        "path": str(code_path.resolve()),
                        "sha256": sha256_bytes(raw),
                        "location": location,
                        "savefig_expression": expression.strip(),
                        "source": source,
                    },
                    "data": [
                        {"literal": literal, "resolved_paths": resolve_literal(root, code_path, literal)}
                        for literal in data_literals
                    ],
                    "signals": {
                        "transforms": sorted(set(transforms)),
                        "plot_calls": sorted(set(plot_calls)),
                        "has_groupby": "groupby" in source,
                        "has_native_data_reference": bool(data_literals),
                    },
                    "question_independent": True,
                })
    return records


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--image", help="Optional chart basename filter")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    root = args.root.resolve()
    records = candidate_records(root, args.image)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(records, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {len(records)} candidate(s): {args.out}")
    return 0 if records else 2


if __name__ == "__main__":
    raise SystemExit(main())
