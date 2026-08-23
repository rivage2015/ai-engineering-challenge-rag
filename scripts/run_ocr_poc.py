#!/usr/bin/env python3
"""Run isolated OCR engines over verified region fixtures."""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import ocr_poc_adapters as adapters  # noqa: E402
import ocr_poc_contract as contract  # noqa: E402


RUNNER = "ocr-poc-runner"
RUNNER_VERSION = "0.1"


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def make_run(
    fixture: dict[str, Any],
    engine: adapters.OCREngineAdapter,
    value: adapters.OCRInput,
    result: adapters.AdapterResult,
) -> dict[str, Any]:
    fingerprint = engine.fingerprint()
    status = result.status
    lines = result.lines
    warnings = list(dict.fromkeys(result.warnings))
    error = result.error
    if status == "completed" and not lines:
        status = "needs_review"
        warnings.append("engine completed without OCR lines")
    if status in {"failed", "timeout", "unavailable"}:
        lines = []
        error = error or f"{engine.name} ended with status {status}"
    raw_text = "\n".join(line["raw_text"] for line in lines)
    input_sha256 = contract.sha256_json(
        {
            "fixture_signature_sha256": fixture["hashes"]["signature_sha256"],
            "crop_image_sha256": value.image_sha256,
            "engine_fingerprint_sha256": fingerprint["fingerprint_sha256"],
            "runner_version": RUNNER_VERSION,
        }
    )
    record: dict[str, Any] = {
        "schema_version": "0.1",
        "record_type": "ocr_poc_run",
        "run_id": "ocrpoc_" + "0" * 24,
        "fixture_ref": {
            "fixture_id": fixture["fixture_id"],
            "signature_sha256": fixture["hashes"]["signature_sha256"],
        },
        "engine": fingerprint,
        "status": status,
        "lines": lines,
        "raw_text": raw_text,
        "timing": {
            "setup_ms": round(result.setup_ms, 6),
            "inference_ms": round(result.inference_ms, 6),
            "cache_hit": False,
        },
        "warnings": warnings,
        "error": error,
        "hashes": {
            "input_sha256": input_sha256,
            "output_sha256": "0" * 64,
            "signature_sha256": "0" * 64,
            "record_sha256": "0" * 64,
        },
        "provenance": {
            "runner": RUNNER,
            "runner_version": RUNNER_VERSION,
            "generated_at": utc_now(),
            "question_independent": True,
            "evidence_connected": False,
            "search_unit_connected": False,
        },
    }
    record["hashes"]["output_sha256"] = contract.sha256_json(
        contract.run_output_payload(record)
    )
    signature = contract.expected_run_signature(record)
    record["hashes"]["signature_sha256"] = signature
    record["run_id"] = contract.expected_run_id(signature)
    record["hashes"]["record_sha256"] = contract.expected_record_sha256(record)
    errors = contract.validate_run(record)
    if errors:
        raise ValueError("generated OCR PoC run is invalid: " + "; ".join(errors))
    return record


def run_manifest(
    fixtures_path: Path,
    output_path: Path,
    *,
    repository_root: Path,
    cache_dir: Path,
    engine_names: list[str],
    timeout: float,
    overwrite: bool,
) -> list[dict[str, Any]]:
    if output_path.exists() and not overwrite:
        raise ValueError(f"output already exists; pass --overwrite to replace it: {output_path}")
    fixtures = contract.load_jsonl(fixtures_path)
    seen: set[str] = set()
    for position, fixture in enumerate(fixtures, 1):
        errors = contract.validate_fixture(
            fixture, repository_root=repository_root, require_verified=True
        )
        if errors:
            raise ValueError(f"fixture {position} is invalid: " + "; ".join(errors))
        fixture_id = fixture["fixture_id"]
        if fixture_id in seen:
            raise ValueError(f"duplicate fixture_id: {fixture_id}")
        seen.add(fixture_id)
    cache_dir.mkdir(parents=True, exist_ok=True)
    if cache_dir.is_symlink():
        raise ValueError(f"cache directory must not be a symlink: {cache_dir}")
    selected = adapters.built_in_adapters(cache_dir, engine_names)
    runs: list[dict[str, Any]] = []
    for fixture in fixtures:
        value = adapters.crop_input(fixture, repository_root)
        for engine in selected:
            result = engine.run(value, timeout=timeout)
            runs.append(make_run(fixture, engine, value, result))
    contract.write_jsonl(output_path, runs)
    return runs


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("--fixtures", type=Path, required=True)
    value.add_argument("--output", type=Path, required=True)
    value.add_argument("--repository-root", type=Path, default=ROOT)
    value.add_argument(
        "--cache-dir", type=Path, default=ROOT / "artifacts" / "ocr-poc-v0.1" / "cache"
    )
    value.add_argument(
        "--engine",
        action="append",
        dest="engines",
        choices=["apple_vision", "tesseract"],
        help="repeat to select engines; defaults to both production baselines",
    )
    value.add_argument("--timeout", type=float, default=180.0)
    value.add_argument("--overwrite", action="store_true")
    return value


def main() -> int:
    args = parser().parse_args()
    if args.timeout <= 0:
        raise SystemExit("--timeout must be positive")
    engines = args.engines or ["apple_vision", "tesseract"]
    try:
        runs = run_manifest(
            args.fixtures,
            args.output,
            repository_root=args.repository_root,
            cache_dir=args.cache_dir,
            engine_names=engines,
            timeout=args.timeout,
            overwrite=args.overwrite,
        )
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    status_counts: dict[str, int] = {}
    for run in runs:
        status_counts[run["status"]] = status_counts.get(run["status"], 0) + 1
    print(f"wrote {len(runs)} OCR PoC runs to {args.output}")
    print("statuses: " + ", ".join(f"{key}={value}" for key, value in sorted(status_counts.items())))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
