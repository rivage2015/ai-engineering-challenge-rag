#!/usr/bin/env python3
"""Validate a persisted sequential visual-analysis record without dependencies."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
ANALYSIS_ID_RE = re.compile(r"^va_[0-9a-f]{16,64}$")
STAGES = ("transcription", "visual_state", "fusion")


def validate(record: object) -> list[str]:
    errors: list[str] = []
    if not isinstance(record, dict):
        return ["root must be an object"]
    required = {
        "schema_version", "record_type", "analysis_id", "source", "model",
        "transcription", "visual_state", "fusion", "verification", "provenance",
    }
    missing = sorted(required - set(record))
    if missing:
        errors.append("missing root keys: " + ", ".join(missing))
    if record.get("schema_version") != "0.1":
        errors.append("schema_version must be 0.1")
    if record.get("record_type") != "visual_analysis":
        errors.append("record_type must be visual_analysis")
    if not ANALYSIS_ID_RE.fullmatch(str(record.get("analysis_id", ""))):
        errors.append("invalid analysis_id")

    source = record.get("source")
    if not isinstance(source, dict):
        errors.append("source must be an object")
    else:
        for key in ("path", "mime_type", "sha256", "bytes"):
            if key not in source:
                errors.append(f"source missing {key}")
        if not SHA256_RE.fullmatch(str(source.get("sha256", ""))):
            errors.append("source.sha256 must be lowercase SHA-256")
        if not isinstance(source.get("bytes"), int) or source.get("bytes", 0) < 1:
            errors.append("source.bytes must be a positive integer")

    model = record.get("model")
    if not isinstance(model, dict):
        errors.append("model must be an object")
    else:
        for key in ("requested", "resolved", "digest"):
            if not isinstance(model.get(key), str) or not model[key]:
                errors.append(f"model.{key} must be a non-empty string")

    for stage_name in STAGES:
        stage = record.get(stage_name)
        if not isinstance(stage, dict):
            errors.append(f"{stage_name} must be an object")
            continue
        if stage.get("status") != "completed":
            errors.append(f"{stage_name}.status must be completed")
        if not isinstance(stage.get("prompt_version"), str) or not stage["prompt_version"]:
            errors.append(f"{stage_name}.prompt_version must be non-empty")
        if not isinstance(stage.get("elapsed_seconds"), (int, float)) or stage["elapsed_seconds"] < 0:
            errors.append(f"{stage_name}.elapsed_seconds must be non-negative")
        if not isinstance(stage.get("output"), dict):
            errors.append(f"{stage_name}.output must be an object")

    verification = record.get("verification")
    if not isinstance(verification, dict):
        errors.append("verification must be an object")
    else:
        if verification.get("status") not in {"verified", "needs_retry", "unresolved"}:
            errors.append("invalid verification.status")
        if not isinstance(verification.get("checks"), list):
            errors.append("verification.checks must be an array")
        if not isinstance(verification.get("warnings"), list):
            errors.append("verification.warnings must be an array")
        retry_count = verification.get("retry_count")
        if not isinstance(retry_count, int) or not 0 <= retry_count <= 2:
            errors.append("verification.retry_count must be between 0 and 2")

    provenance = record.get("provenance")
    if not isinstance(provenance, dict):
        errors.append("provenance must be an object")
    else:
        if provenance.get("orchestrator") != "sequential-visual-orchestrator":
            errors.append("invalid provenance.orchestrator")
        if provenance.get("sequential") is not True:
            errors.append("provenance.sequential must be true")
        versions = provenance.get("prompt_versions")
        if not isinstance(versions, dict) or any(not versions.get(stage) for stage in STAGES):
            errors.append("provenance.prompt_versions is incomplete")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("analysis", type=Path)
    args = parser.parse_args()
    try:
        record = json.loads(args.analysis.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}")
        return 1
    errors = validate(record)
    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        print(f"validation failed: {len(errors)} error(s)")
        return 1
    print(f"validation passed: {args.analysis}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
