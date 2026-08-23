#!/usr/bin/env python3
"""Closed contracts and deterministic metrics for the isolated OCR PoC."""

from __future__ import annotations

import copy
import hashlib
import json
import math
import unicodedata
from pathlib import Path
from typing import Any, Iterable

from PIL import Image
from jsonschema import Draft202012Validator, FormatChecker


ROOT = Path(__file__).resolve().parents[1]
FIXTURE_SCHEMA_PATH = ROOT / "schemas" / "ocr-poc-manifest.schema.json"
RUN_SCHEMA_PATH = ROOT / "schemas" / "ocr-poc-run.schema.json"
MAX_JSONL_BYTES = 64 * 1024 * 1024
MAX_RECORDS = 10000
MAX_EVALUATION_TEXT_CHARS = 20000
MAX_EDIT_CELLS = 5_000_000


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _schema(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(value)
    return value


FIXTURE_VALIDATOR = Draft202012Validator(
    _schema(FIXTURE_SCHEMA_PATH), format_checker=FormatChecker()
)
RUN_VALIDATOR = Draft202012Validator(
    _schema(RUN_SCHEMA_PATH), format_checker=FormatChecker()
)


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"JSONL input must be a regular non-symlink file: {path}")
    if path.stat().st_size > MAX_JSONL_BYTES:
        raise ValueError(f"JSONL input exceeds {MAX_JSONL_BYTES} bytes: {path}")
    records: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, raw in enumerate(handle, 1):
            if not raw.strip():
                raise ValueError(f"{path}:{line_number}: blank JSONL line")
            try:
                value = json.loads(raw, object_pairs_hook=_reject_duplicate_keys)
            except (json.JSONDecodeError, ValueError) as exc:
                raise ValueError(f"{path}:{line_number}: invalid JSON: {exc}") from exc
            if not isinstance(value, dict):
                raise ValueError(f"{path}:{line_number}: record must be an object")
            records.append(value)
            if len(records) > MAX_RECORDS:
                raise ValueError(f"JSONL input exceeds {MAX_RECORDS} records")
    if not records:
        raise ValueError(f"JSONL input is empty: {path}")
    return records


def write_jsonl(path: Path, records: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = "".join(canonical_json(record) + "\n" for record in records)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(payload, encoding="utf-8")
    temporary.replace(path)


def fixture_signature_payload(record: dict[str, Any]) -> dict[str, Any]:
    provenance = copy.deepcopy(record["provenance"])
    provenance.pop("created_at", None)
    return {
        "schema_version": record["schema_version"],
        "record_type": record["record_type"],
        "asset_ref": record["asset_ref"],
        "crop": record["crop"],
        "strata": record["strata"],
        "reference": record["reference"],
        "provenance": provenance,
    }


def expected_fixture_signature(record: dict[str, Any]) -> str:
    return sha256_json(fixture_signature_payload(record))


def expected_fixture_id(signature: str) -> str:
    return "ocrfx_" + signature[:24]


def run_output_payload(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": record["status"],
        "lines": record["lines"],
        "raw_text": record["raw_text"],
        "warnings": record["warnings"],
        "error": record["error"],
    }


def run_signature_payload(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": record["schema_version"],
        "record_type": record["record_type"],
        "fixture_ref": record["fixture_ref"],
        "engine": record["engine"],
        "input_sha256": record["hashes"]["input_sha256"],
        "output_sha256": record["hashes"]["output_sha256"],
        "runner_version": record["provenance"]["runner_version"],
    }


def expected_run_signature(record: dict[str, Any]) -> str:
    return sha256_json(run_signature_payload(record))


def expected_run_id(signature: str) -> str:
    return "ocrpoc_" + signature[:24]


def run_record_payload(record: dict[str, Any]) -> dict[str, Any]:
    """Return the full persisted record except its self-referential integrity hash."""
    payload = copy.deepcopy(record)
    payload["hashes"].pop("record_sha256", None)
    return payload


def expected_record_sha256(record: dict[str, Any]) -> str:
    return sha256_json(run_record_payload(record))


def consistent_engine_identities(
    records: Iterable[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Reject mixing different versions/fingerprints/runtime under one engine name."""
    identities: dict[str, dict[str, Any]] = {}
    canonical: dict[str, str] = {}
    for position, record in enumerate(records, 1):
        engine = record.get("engine")
        if not isinstance(engine, dict) or not isinstance(engine.get("name"), str):
            raise ValueError(f"run {position} has no valid engine identity")
        name = engine["name"]
        encoded = canonical_json(engine)
        previous = canonical.get(name)
        if previous is not None and previous != encoded:
            raise ValueError(
                f"engine identity drift for {name}: version, fingerprint, or runtime differs"
            )
        canonical[name] = encoded
        identities[name] = copy.deepcopy(engine)
    return identities


def validate_expected_engines(
    records: Iterable[dict[str, Any]], expected: dict[str, str]
) -> dict[str, dict[str, Any]]:
    if not expected:
        raise ValueError("expected engine plan must not be empty")
    identities = consistent_engine_identities(records)
    missing = sorted(set(expected) - set(identities))
    unexpected = sorted(set(identities) - set(expected))
    if missing:
        raise ValueError(f"expected engine is missing from runs: {missing[0]}")
    if unexpected:
        raise ValueError(f"unexpected engine is present in runs: {unexpected[0]}")
    for name, fingerprint in expected.items():
        actual = identities[name]["fingerprint_sha256"]
        if actual != fingerprint:
            raise ValueError(
                f"engine fingerprint differs from experiment plan for {name}: "
                f"expected {fingerprint}, got {actual}"
            )
    return identities


def _safe_relative_path(raw: str, label: str) -> Path:
    path = Path(raw)
    if path.is_absolute() or raw.startswith(("/", "\\")):
        raise ValueError(f"{label} must be relative")
    if not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError(f"{label} contains an unsafe path component")
    return path


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def resolve_fixture_image(record: dict[str, Any], repository_root: Path) -> Path:
    relative = _safe_relative_path(
        record["asset_ref"]["materialized_path"], "materialized_path"
    )
    root = repository_root.resolve(strict=True)
    candidate = repository_root / relative
    if candidate.is_symlink() or not candidate.is_file():
        raise ValueError(f"fixture image must be a regular non-symlink file: {candidate}")
    resolved = candidate.resolve(strict=True)
    if not _inside(resolved, root):
        raise ValueError("fixture image escapes repository root")
    return resolved


def _schema_errors(validator: Draft202012Validator, record: Any) -> list[str]:
    errors: list[str] = []
    for error in sorted(validator.iter_errors(record), key=lambda item: list(item.path)):
        location = "/" + "/".join(str(value) for value in error.path)
        errors.append(f"{location}: {error.message}")
    return errors


def validate_fixture(
    record: dict[str, Any],
    *,
    repository_root: Path | None = None,
    require_verified: bool = False,
) -> list[str]:
    errors = _schema_errors(FIXTURE_VALIDATOR, record)
    if errors:
        return errors
    bbox = record["crop"]["bbox"]
    if bbox[0] + bbox[2] > 1000 or bbox[1] + bbox[3] > 1000:
        errors.append("/crop/bbox: crop exceeds normalized image bounds")
    try:
        _safe_relative_path(record["asset_ref"]["materialized_path"], "materialized_path")
        _safe_relative_path(record["asset_ref"]["source_relative_path"], "source_relative_path")
    except ValueError as exc:
        errors.append(str(exc))
    reference = record["reference"]
    if require_verified and reference["status"] != "verified":
        errors.append("/reference/status: verified fixture required")
    if reference["status"] == "verified":
        raw_text = unicodedata.normalize("NFC", reference["raw_text"])
        if not raw_text.strip():
            errors.append("/reference/raw_text: verified text must contain a non-whitespace character")
        for span in reference["important_spans"]:
            if unicodedata.normalize("NFC", span) not in raw_text:
                errors.append(f"/reference/important_spans: span is not in raw_text: {span!r}")
    expected_signature = expected_fixture_signature(record)
    if record["hashes"]["signature_sha256"] != expected_signature:
        errors.append("/hashes/signature_sha256: does not match canonical fixture payload")
    if record["fixture_id"] != expected_fixture_id(expected_signature):
        errors.append("/fixture_id: does not match canonical fixture payload")
    if repository_root is not None:
        try:
            image_path = resolve_fixture_image(record, repository_root)
            actual_sha = sha256_file(image_path)
            if actual_sha != record["asset_ref"]["image_sha256"]:
                errors.append("/asset_ref/image_sha256: does not match image bytes")
            with Image.open(image_path) as image:
                dimensions = {"width_px": int(image.width), "height_px": int(image.height)}
            if dimensions != record["asset_ref"]["dimensions"]:
                errors.append("/asset_ref/dimensions: does not match decoded image")
        except (OSError, ValueError) as exc:
            errors.append(f"/asset_ref/materialized_path: {exc}")
    return errors


def validate_run(record: dict[str, Any]) -> list[str]:
    errors = _schema_errors(RUN_VALIDATOR, record)
    if errors:
        return errors
    lines = record["lines"]
    for expected, line in enumerate(lines, 1):
        if line["sequence"] != expected or line["line_id"] != f"line_{expected}":
            errors.append("/lines: line sequence and line_id must be contiguous")
            break
        bbox = line["bbox"]
        if bbox[0] + bbox[2] > 1000 or bbox[1] + bbox[3] > 1000:
            errors.append(f"/lines/{expected - 1}/bbox: line exceeds normalized bounds")
    expected_text = "\n".join(line["raw_text"] for line in lines)
    if record["raw_text"] != expected_text:
        errors.append("/raw_text: must be the newline join of raw line text")
    if record["status"] == "completed" and not lines:
        errors.append("/status: completed run must contain at least one line")
    expected_output = sha256_json(run_output_payload(record))
    if record["hashes"]["output_sha256"] != expected_output:
        errors.append("/hashes/output_sha256: does not match run output")
    expected_signature = expected_run_signature(record)
    if record["hashes"]["signature_sha256"] != expected_signature:
        errors.append("/hashes/signature_sha256: does not match canonical run payload")
    if record["run_id"] != expected_run_id(expected_signature):
        errors.append("/run_id: does not match canonical run payload")
    expected_record = expected_record_sha256(record)
    if record["hashes"]["record_sha256"] != expected_record:
        errors.append("/hashes/record_sha256: does not match full persisted record")
    return errors


def levenshtein_counts(reference: str, prediction: str) -> dict[str, int]:
    """Return deterministic edit counts with bounded two-row dynamic programming."""
    if len(reference) > MAX_EVALUATION_TEXT_CHARS or len(prediction) > MAX_EVALUATION_TEXT_CHARS:
        raise ValueError(
            f"OCR text exceeds evaluation limit of {MAX_EVALUATION_TEXT_CHARS} characters"
        )
    cells = (len(reference) + 1) * (len(prediction) + 1)
    if cells > MAX_EDIT_CELLS:
        raise ValueError(
            f"OCR edit matrix exceeds controlled limit of {MAX_EDIT_CELLS} cells"
        )
    # Each cell is (distance, substitutions, deletions, insertions).
    previous = [(col, 0, 0, col) for col in range(len(prediction) + 1)]
    priority = {"equal": 0, "substitute": 1, "delete": 2, "insert": 3}
    for row in range(1, len(reference) + 1):
        current: list[tuple[int, int, int, int]] = [(row, 0, row, 0)]
        for col in range(1, len(prediction) + 1):
            substitution = 0 if reference[row - 1] == prediction[col - 1] else 1
            diagonal = previous[col - 1]
            deletion = previous[col]
            insertion = current[col - 1]
            candidates = [
                (
                    (
                        diagonal[0] + substitution,
                        diagonal[1] + substitution,
                        diagonal[2],
                        diagonal[3],
                    ),
                    "equal" if substitution == 0 else "substitute",
                ),
                ((deletion[0] + 1, deletion[1], deletion[2] + 1, deletion[3]), "delete"),
                ((insertion[0] + 1, insertion[1], insertion[2], insertion[3] + 1), "insert"),
            ]
            value, _operation = min(
                candidates, key=lambda item: (item[0][0], priority[item[1]])
            )
            current.append(value)
        previous = current
    distance, substitutions, deletions, insertions = previous[-1]
    equal = max(0, len(reference) - substitutions - deletions)
    return {
        "equal": equal,
        "substitute": substitutions,
        "delete": deletions,
        "insert": insertions,
        "distance": distance,
    }


def fixture_metrics(reference: str, prediction: str, important_spans: list[str]) -> dict[str, Any]:
    reference_nfc = unicodedata.normalize("NFC", reference)
    prediction_nfc = unicodedata.normalize("NFC", prediction)
    edits = levenshtein_counts(reference_nfc, prediction_nfc)
    reference_collapsed = " ".join(reference_nfc.split())
    prediction_collapsed = " ".join(prediction_nfc.split())
    if not reference_collapsed:
        raise ValueError("verified reference text must contain a non-whitespace character")
    collapsed_edits = levenshtein_counts(reference_collapsed, prediction_collapsed)
    reference_chars = len(reference_nfc)
    if reference_chars == 0:
        raise ValueError("verified reference text must not be empty")
    matched = [span for span in important_spans if unicodedata.normalize("NFC", span) in prediction_nfc]
    recall = len(matched) / len(important_spans) if important_spans else None
    cer = edits["distance"] / reference_chars
    if not math.isfinite(cer):
        raise RuntimeError("non-finite CER")
    return {
        "reference_chars": reference_chars,
        "prediction_chars": len(prediction_nfc),
        "edit_distance": edits["distance"],
        "substitutions": edits["substitute"],
        "deletions": edits["delete"],
        "insertions": edits["insert"],
        "cer": cer,
        "exact_match": reference_nfc == prediction_nfc,
        "whitespace_collapsed_reference_chars": len(reference_collapsed),
        "whitespace_collapsed_prediction_chars": len(prediction_collapsed),
        "whitespace_collapsed_edit_distance": collapsed_edits["distance"],
        "whitespace_collapsed_cer": (
            collapsed_edits["distance"] / len(reference_collapsed)
        ),
        "whitespace_collapsed_exact_match": (
            reference_collapsed == prediction_collapsed
        ),
        "important_span_total": len(important_spans),
        "important_span_matched": len(matched),
        "important_span_recall": recall,
        "matched_spans": matched,
    }
