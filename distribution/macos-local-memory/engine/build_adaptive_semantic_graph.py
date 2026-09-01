#!/usr/bin/env python3
"""Build Local Memory semantic Evidence through the verified Layer 1 pipeline.

The bridge keeps the existing content-security and answer-index boundary.  It
selects source files from the already validated path inventory, builds native
intermediate records and SearchUnits, and then applies the one-way Local Memory
adapter.  Source content is never written to stdout.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import unicodedata
import zipfile
from collections import Counter
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any, Callable


BUILDER = "adaptive-layer1-semantic-bridge"
BUILDER_VERSION = "0.2.0"
SCHEMA_VERSION = "0.1"
LOCAL_LLM_RUNNERS = {"ollama_loopback_chat"}

SUPPORTED_SUFFIXES = {
    ".docx", ".xlsx", ".pptx", ".pdf",
    ".csv", ".tsv", ".json", ".xml", ".ipynb",
    ".md", ".txt", ".py", ".toml", ".yaml", ".yml", ".rst", ".sql", ".sh", ".command",
    ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp",
}
SKIP_DIRECTORY_NAMES = {".git", "__pycache__", ".ipynb_checkpoints", "node_modules"}
GENERATED_COMPONENTS = {".next", ".obsidian", ".cursor"}
GENERATED_SUFFIXES = {".pyc", ".map", ".css", ".ダウンロード"}
GENERATED_NAMES = {".DS_Store", "desktop.ini"}
SENSITIVE_NAMES = {".env", ".npmrc", ".pypirc", "settings.local.json"}
SENSITIVE_TOKENS = {"credential", "credentials", "secret", "secrets", "apikey", "token", "tokens"}

REQUIRED_TOOLS = (
    "build_intermediate_records.py",
    "probe_intermediate_records.py",
    "evidence_text_chunking.py",
    "build_search_units.py",
    "validate_search_units.py",
    "validate_intermediate_records.py",
    "validate_intermediate_records_streaming.py",
    "lexical_search_common.py",
    "adapt_layer1_to_local_memory.py",
)
HARD_DEPENDENCIES = {
    ".docx": ("docx", "python-docx"),
    ".pptx": ("pptx", "python-pptx"),
    ".pdf": ("pypdf", "pypdf"),
}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def atomic_copy(source: Path, destination: Path) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=destination.name + ".", suffix=".tmp", dir=destination.parent
    )
    temporary = Path(temporary_name)
    try:
        with source.open("rb") as source_handle, os.fdopen(descriptor, "wb") as destination_handle:
            for block in iter(lambda: source_handle.read(1024 * 1024), b""):
                destination_handle.write(block)
            destination_handle.flush()
            os.fsync(destination_handle.fileno())
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid JSONL at line {line_number}: {path.name}") from exc
            if not isinstance(value, dict):
                raise ValueError(f"JSONL record must be an object at line {line_number}: {path.name}")
            records.append(value)
    return records


def derive_llm_extraction(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Derive model-assisted extraction use from immutable Layer 1 provenance."""
    methods: Counter[str] = Counter()
    for record in records:
        provenance = record.get("provenance", {})
        native = record.get("native_properties", {})
        method = provenance.get("extraction_method") if isinstance(provenance, dict) else None
        runner = native.get("runner") if isinstance(native, dict) else None
        uses_llm = (
            isinstance(method, str) and method.startswith("local_vlm_")
        ) or runner in LOCAL_LLM_RUNNERS
        if uses_llm:
            methods[method if isinstance(method, str) and method else "unknown"] += 1
    return {
        "used": bool(methods),
        "evidence_count": sum(methods.values()),
        "methods": dict(sorted(methods.items())),
    }


def safe_relative(value: str) -> PurePosixPath:
    relative = PurePosixPath(value)
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        raise ValueError("source inventory contains an unsafe relative path")
    return relative


def selection_reason(relative_path: str) -> str:
    relative = safe_relative(relative_path)
    parts = set(relative.parts)
    name = relative.name
    lowered = name.casefold()
    suffix = Path(name).suffix.casefold()
    if name.startswith("~$"):
        return "policy_excluded"
    basename_tokens = set(filter(None, re.split(r"[^a-z0-9]+", Path(lowered).stem)))
    api_key_name = {"api", "key"} <= basename_tokens
    if name in SENSITIVE_NAMES or basename_tokens & SENSITIVE_TOKENS or api_key_name:
        return "policy_excluded"
    if parts & (SKIP_DIRECTORY_NAMES | GENERATED_COMPONENTS):
        return "policy_excluded"
    if suffix in GENERATED_SUFFIXES or name in GENERATED_NAMES:
        return "policy_excluded"
    return "selected" if suffix in SUPPORTED_SUFFIXES else "unsupported"


def select_inventory(inventory: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, int]]:
    selected: list[dict[str, Any]] = []
    counts: Counter[str] = Counter()
    seen: set[str] = set()
    for item in inventory:
        if item.get("kind") != "file":
            continue
        relative = item.get("relative_path")
        if not isinstance(relative, str) or not relative or relative in seen:
            raise ValueError("source inventory has an invalid or duplicate file path")
        seen.add(relative)
        if item.get("read_status") != "observed" or not isinstance(item.get("sha256"), str):
            counts["inventory_unresolved"] += 1
            continue
        reason = selection_reason(relative)
        counts[reason] += 1
        if reason == "selected":
            selected.append(item)
    selected.sort(key=lambda item: unicodedata.normalize("NFC", item["relative_path"]))
    return selected, dict(sorted(counts.items()))


def source_path(root: Path, relative_path: str) -> Path:
    root = root.resolve(strict=True)
    relative = safe_relative(relative_path)
    path = root.joinpath(*relative.parts)
    if path.is_symlink() or not path.is_file():
        raise ValueError("selected source is not a regular file")
    resolved = path.resolve(strict=True)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError("selected source escapes source_root") from exc
    return resolved


def validate_inventory_binding(root: Path, selected: list[dict[str, Any]]) -> None:
    for item in selected:
        path = source_path(root, item["relative_path"])
        if path.stat().st_size != item.get("size_bytes") or sha256_file(path) != item.get("sha256"):
            raise ValueError("selected source changed after path inventory")


def missing_dependencies(
    root: Path,
    selected: list[dict[str, Any]],
    finder: Callable[[str], Any] = importlib.util.find_spec,
) -> list[dict[str, Any]]:
    suffixes = {Path(item["relative_path"]).suffix.casefold() for item in selected}
    missing: dict[str, dict[str, Any]] = {}
    for suffix, (module, package) in HARD_DEPENDENCIES.items():
        if suffix in suffixes and finder(module) is None:
            missing[module] = {"module": module, "package": package, "formats": [suffix]}
    encrypted_formats: set[str] = set()
    for item in selected:
        suffix = Path(item["relative_path"]).suffix.casefold()
        if suffix in {".docx", ".xlsx", ".pptx"} and not zipfile.is_zipfile(source_path(root, item["relative_path"])):
            encrypted_formats.add(suffix)
    if encrypted_formats and finder("msoffcrypto") is None:
        missing["msoffcrypto"] = {
            "module": "msoffcrypto", "package": "msoffcrypto-tool",
            "formats": sorted(encrypted_formats),
        }
    return [missing[key] for key in sorted(missing)]


def require_tools(tools_dir: Path) -> None:
    missing = [name for name in REQUIRED_TOOLS if not (tools_dir / name).is_file()]
    if missing:
        raise ValueError("adaptive reader package is incomplete: " + ",".join(missing))


def default_tools_dir() -> Path:
    packaged = Path(__file__).resolve().parent / "layer1" / "scripts"
    if packaged.is_dir():
        return packaged
    source_checkout = Path(__file__).resolve().parents[3] / "scripts"
    return source_checkout


def run_tool(label: str, command: list[str], tools_dir: Path, log_path: Path) -> None:
    environment = dict(os.environ)
    existing = environment.get("PYTHONPATH", "")
    environment["PYTHONPATH"] = str(tools_dir) + (os.pathsep + existing if existing else "")
    process = subprocess.run(
        command, cwd=tools_dir, env=environment,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False,
    )
    with log_path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(f"[{label}] returncode={process.returncode}\n")
        if process.stdout:
            handle.write(process.stdout)
            if not process.stdout.endswith("\n"):
                handle.write("\n")
        if process.stderr:
            handle.write(process.stderr)
            if not process.stderr.endswith("\n"):
                handle.write("\n")
    if process.returncode:
        raise RuntimeError(f"adaptive_reader_stage_failed:{label}:exit_{process.returncode}")


def build(source_root: Path, inventory_path: Path, output: Path, tools_dir: Path) -> dict[str, Any]:
    source_root = source_root.resolve(strict=True)
    inventory_path = inventory_path.resolve(strict=True)
    tools_dir = tools_dir.resolve(strict=True)
    output = output.resolve()
    if not source_root.is_dir() or source_root.is_symlink():
        raise ValueError("source_root must be a real directory")
    try:
        output.relative_to(source_root)
    except ValueError:
        pass
    else:
        raise ValueError("output must be outside source_root")
    if output.exists() and (not output.is_dir() or any(output.iterdir())):
        raise ValueError("output directory must be empty")
    output.mkdir(parents=True, exist_ok=True)
    require_tools(tools_dir)

    inventory = read_jsonl(inventory_path)
    selected, selection_counts = select_inventory(inventory)
    if not selected:
        state = {
            "schema_version": SCHEMA_VERSION, "builder": BUILDER,
            "builder_version": BUILDER_VERSION, "status": "blocked_no_supported_files",
            "selection_counts": selection_counts,
        }
        atomic_json(output / "adaptive-reader-state.json", state)
        raise ValueError("adaptive_reader_no_supported_files")
    validate_inventory_binding(source_root, selected)
    dependencies = missing_dependencies(source_root, selected)

    manifest = {
        "schema_version": SCHEMA_VERSION,
        "source_root": str(source_root),
        "source_inventory_sha256": sha256_file(inventory_path),
        "paths": [item["relative_path"] for item in selected],
    }
    manifest_path = output / "layer1-input-manifest.json"
    atomic_json(manifest_path, manifest)
    intermediate = output / "layer1-intermediate"
    search = output / "layer1-search"
    adapter = output / "layer1-adapter"
    log_path = output / "adaptive-reader-tools.log"

    run_tool("intermediate", [
        sys.executable, str(tools_dir / "build_intermediate_records.py"),
        "--root", str(source_root), "--out", str(intermediate),
        "--input-manifest", str(manifest_path),
    ], tools_dir, log_path)
    intermediate_state = json.loads((intermediate / "build-state.json").read_text(encoding="utf-8"))
    if intermediate_state.get("build_status") not in {"complete", "complete_with_failures"}:
        atomic_json(output / "adaptive-reader-state.json", {
            "schema_version": SCHEMA_VERSION, "builder": BUILDER,
            "builder_version": BUILDER_VERSION, "status": "blocked_incomplete_extraction",
            "selection_counts": selection_counts,
            "layer1_build_status": intermediate_state.get("build_status"),
            "external_network_used": False,
        })
        raise ValueError("adaptive_reader_extraction_incomplete")
    if intermediate_state.get("input_paths") != manifest["paths"]:
        raise ValueError("Layer 1 input coverage differs from curated manifest")

    full_schema_validation = importlib.util.find_spec("jsonschema") is not None
    validation_command = [
        sys.executable, str(tools_dir / "validate_intermediate_records_streaming.py"),
        str(intermediate), "--root", str(source_root),
    ]
    if not full_schema_validation:
        validation_command.append("--allow-structural-schema-fallback")
    run_tool("intermediate_validation", validation_command, tools_dir, log_path)
    validation_state_path = output / "layer1-validation-state.json"
    atomic_json(validation_state_path, {
        "validator": "validate_intermediate_records_streaming.py",
        "schema_validation": "draft202012" if full_schema_validation else "structural_contract_only",
        "intermediate_state_sha256": sha256_file(intermediate / "build-state.json"),
        "source_root": str(source_root),
        "status": "pass",
    })

    run_tool("search_units", [
        sys.executable, str(tools_dir / "build_search_units.py"),
        "--intermediate", str(intermediate), "--out", str(search),
    ], tools_dir, log_path)
    run_tool("adapter", [
        sys.executable, str(tools_dir / "adapt_layer1_to_local_memory.py"),
        "--intermediate", str(intermediate), "--search-output", str(search),
        "--source-root", str(source_root), "--out", str(adapter),
    ], tools_dir, log_path)

    documents_path = output / "semantic-documents.jsonl"
    evidence_path = output / "semantic-evidence.jsonl"
    atomic_copy(adapter / documents_path.name, documents_path)
    atomic_copy(adapter / evidence_path.name, evidence_path)
    adapter_state_path = adapter / "layer1-adapter-state.json"
    adapter_state = json.loads(adapter_state_path.read_text(encoding="utf-8"))
    layer1_evidence = read_jsonl(intermediate / "evidence.jsonl")
    llm_extraction = derive_llm_extraction(layer1_evidence)
    documents = read_jsonl(documents_path)
    status_counts = Counter(item.get("status", "unknown") for item in documents)
    limitations = {
        "unsupported_files": selection_counts.get("unsupported", 0),
        "policy_excluded_files": selection_counts.get("policy_excluded", 0),
        "inventory_unresolved_files": selection_counts.get("inventory_unresolved", 0),
        "partial_documents": int(adapter_state.get("layer1_status_counts", {}).get("partial", 0)),
        "deferred_documents": int(adapter_state.get("layer1_status_counts", {}).get("deferred", 0)),
        "empty_after_extraction_documents": status_counts.get("empty_after_extraction", 0),
        "failed_documents": status_counts.get("extraction_failed", 0),
        "missing_reader_dependencies": len(dependencies),
        "structural_schema_validation_only": 0 if full_schema_validation else 1,
    }
    has_limits = any(limitations.values())
    result = {
        "schema_version": SCHEMA_VERSION,
        "builder": BUILDER,
        "builder_version": BUILDER_VERSION,
        "status": "complete_with_limits" if has_limits else "complete",
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "question_independent": True,
        "execution_policy": "never_execute",
        "external_network_used": False,
        "llm_used_for_extraction": llm_extraction["used"],
        "llm_extraction": llm_extraction,
        "requires_content_security_gate": True,
        "source_root": str(source_root),
        "source_inventory": {"path": str(inventory_path), "sha256": sha256_file(inventory_path)},
        "selection_counts": selection_counts,
        "selected_file_count": len(selected),
        "limitations": limitations,
        "missing_dependencies": dependencies,
        "layer1_status_counts": adapter_state.get("layer1_status_counts", {}),
        "document_status_counts": dict(sorted(status_counts.items())),
        "stages": {
            "input_manifest": {"path": manifest_path.name, "sha256": sha256_file(manifest_path)},
            "intermediate": {"path": "layer1-intermediate/build-state.json", "sha256": sha256_file(intermediate / "build-state.json")},
            "intermediate_validation": {"path": validation_state_path.name, "sha256": sha256_file(validation_state_path)},
            "search": {"path": "layer1-search/search-build-state.json", "sha256": sha256_file(search / "search-build-state.json")},
            "adapter": {"path": "layer1-adapter/layer1-adapter-state.json", "sha256": sha256_file(adapter_state_path)},
        },
        "outputs": {
            "documents": {"path": documents_path.name, "sha256": sha256_file(documents_path), "count": len(documents)},
            "evidence": {"path": evidence_path.name, "sha256": sha256_file(evidence_path), "count": adapter_state["outputs"]["evidence"]["count"]},
        },
        "search_unit_projection": adapter_state.get("search_unit_projection", {}),
    }
    atomic_json(output / "adaptive-reader-state.json", result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inventory", required=True, type=Path)
    parser.add_argument("--source-root", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument(
        "--tools-dir", type=Path,
        default=default_tools_dir(),
    )
    args = parser.parse_args()
    try:
        result = build(args.source_root, args.inventory, args.output_dir, args.tools_dir)
    except Exception as exc:
        raise SystemExit(f"{type(exc).__name__}:{exc}") from exc
    print(canonical({
        "status": result["status"],
        "selected_files": result["selected_file_count"],
        "documents": result["outputs"]["documents"]["count"],
        "evidence": result["outputs"]["evidence"]["count"],
        "limitations": result["limitations"],
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
