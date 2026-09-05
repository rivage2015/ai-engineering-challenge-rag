#!/usr/bin/env python3
"""First-run and index-building orchestration for Local Memory Search."""

from __future__ import annotations

import argparse
import fcntl
import functools
import hashlib
import importlib.util
import json
import os
import platform
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path


APP_NAME = "LocalMemorySearch"
SUPPORT = Path.home() / "Library" / "Application Support" / APP_NAME
CONFIG = SUPPORT / "config.json"
STATE = SUPPORT / "state.json"
ENGINE = Path(__file__).resolve().parent / "engine"
OLLAMA = "http://127.0.0.1:11434"
LOCAL_HTTP_OPENER = urllib.request.build_opener(
    urllib.request.ProxyHandler({})
)
IMAGE_FALLBACK_MODEL = "gemma4:12b"
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp"}
VISUAL_READER_SUFFIXES = IMAGE_SUFFIXES | {
    ".pdf", ".docx", ".xlsx", ".pptx", ".ipynb",
}
GENERATION_NAME = re.compile(r"generation-[0-9a-f]{32}")
GENERATION_MARKER = "build-generation.json"
READER_GENERATION_CONTRACT_CONFIG_KEY = "reader_generation_contract"
READER_GENERATION_CONTRACT_FILENAME = "reader-generation-contract.json"
READER_GENERATION_CONTRACT_SCHEMA_VERSION = "0.1"
READER_PROCESSING_CODE_FILES = (
    "build_intermediate_records.py",
    "probe_intermediate_records.py",
    "evidence_text_chunking.py",
    "extract_ocr_observations.py",
    "classify_visual_assets.py",
    "validate_ocr_observations.py",
    "validate_visual_classifications.py",
    "local_image_ocr.py",
    "local_paddle_ocr.py",
    "local_pdf_page_renderer.py",
    "local_visual_observation.py",
    "apple_vision_ocr.swift",
    "image_canonicalizer.swift",
    "pdf_page_renderer.js",
)
READER_SCHEMA_FILES = (
    "document.schema.json",
    "evidence.schema.json",
    "relation.schema.json",
    "search-unit.schema.json",
    "ocr-observation.schema.json",
    "visual-classification.schema.json",
)
CROSS_DOCUMENT_SHADOW_FLAG = "cross_document_semantic_graph_shadow_enabled"
CROSS_DOCUMENT_STORAGE_FLAG = "cross_document_semantic_graph_storage_enabled"
CROSS_DOCUMENT_QUERY_CANDIDATE_FLAG = (
    "cross_document_semantic_graph_query_candidate_enabled"
)
CROSS_DOCUMENT_INDEPENDENT_EDGE_AUDIT_FLAG = (
    "cross_document_semantic_graph_independent_edge_audit_enabled"
)
CROSS_DOCUMENT_ANSWER_PROMOTION_FLAG = (
    "cross_document_semantic_graph_answer_promotion_enabled"
)
CROSS_DOCUMENT_SHADOW_DIR = "04-semantic-graph-shadow"
CROSS_DOCUMENT_SHADOW_RUN_STATE = "shadow-run-state.json"
CROSS_DOCUMENT_STORAGE_DIR = "05-semantic-answer-index"
CROSS_DOCUMENT_STORAGE_RUN_STATE = "semantic-answer-index-state.json"
CROSS_DOCUMENT_STORAGE_CONFIG_KEY = "cross_document_semantic_graph_storage"
CROSS_DOCUMENT_TRUST_CONFIG_KEY = "cross_document_semantic_graph_trust"
CROSS_DOCUMENT_STORAGE_TOOL = "project_cross_document_graph_to_answer_index.py"
BASE_ANSWER_INDEX_SHA256_KEY = "base_answer_index_sha256"
CROSS_DOCUMENT_SHADOW_TIMEOUT_SECONDS = 300.0
CROSS_DOCUMENT_SHADOW_TOOLS = (
    "build_cross_document_semantic_graph.py",
    "query_cross_document_semantic_graph.py",
    "validate_cross_document_semantic_graph.py",
)
BUILD_EXECUTION_LOCK_FILENAME = ".build-execution-v1.lock"
BUILD_EXECUTION_LEASE_VERSION_KEY = "build_execution_lease_version"
BUILD_EXECUTION_LEASE_VERSION = 1
LEGACY_OWNER_START_TOLERANCE_SECONDS = 2.0


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


@contextmanager
def config_read_lease(*, blocking: bool = True):
    """Hold a cross-process read lease while CONFIG is used for activation.

    Every CONFIG writer in this module takes the matching exclusive advisory
    lease through :func:`atomic_json`.  The lock is intentionally separate
    from ``config.json`` because publication replaces that file atomically.
    """
    lock_path = CONFIG.parent / ".config-activation-v1.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(lock_path, flags, 0o600)
    acquired = False
    try:
        os.fchmod(descriptor, 0o600)
        operation = fcntl.LOCK_SH
        if not blocking:
            operation |= fcntl.LOCK_NB
        fcntl.flock(descriptor, operation)
        acquired = True
        yield
    finally:
        if acquired:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


@contextmanager
def _config_write_lease():
    lock_path = CONFIG.parent / ".config-activation-v1.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(lock_path, flags, 0o600)
    acquired = False
    try:
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        acquired = True
        yield
    finally:
        if acquired:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


@contextmanager
def build_execution_lease(*, blocking: bool = False):
    """Allow only one index build across UI and CLI processes."""
    lock_path = SUPPORT / BUILD_EXECUTION_LOCK_FILENAME
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(lock_path, flags, 0o600)
    acquired = False
    try:
        os.fchmod(descriptor, 0o600)
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or metadata.st_nlink != 1
        ):
            raise RuntimeError("build_execution_lock_invalid")
        operation = fcntl.LOCK_EX
        if not blocking:
            operation |= fcntl.LOCK_NB
        try:
            fcntl.flock(descriptor, operation)
        except BlockingIOError as exc:
            raise RuntimeError("build_already_running") from exc
        acquired = True
        yield
    finally:
        if acquired:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _single_build_execution(function):
    """Decorate the public build entrypoint with its process-wide lease."""
    @functools.wraps(function)
    def serialized(*args, **kwargs):
        with build_execution_lease(blocking=False):
            return function(*args, **kwargs)

    return serialized


def _single_recovery_execution(function):
    """Skip startup recovery while another process owns the build lease."""
    @functools.wraps(function)
    def serialized(*args, **kwargs):
        lease = build_execution_lease(blocking=False)
        try:
            lease.__enter__()
        except RuntimeError as exc:
            if str(exc) == "build_already_running":
                return {"status": "active_build", "removed": []}
            raise
        try:
            return function(*args, **kwargs)
        finally:
            lease.__exit__(None, None, None)

    return serialized


def _atomic_json_unlocked(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
        text=True,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(
                json.dumps(value, ensure_ascii=False, indent=2) + "\n"
            )
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory_descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def atomic_json(path: Path, value: dict) -> None:
    """Atomically replace JSON; CONFIG callers doing RMW must use CAS below."""
    if path == CONFIG:
        with _config_write_lease():
            _atomic_json_unlocked(path, value)
        return
    _atomic_json_unlocked(path, value)


def load_config_snapshot() -> tuple[bool, dict]:
    """Read CONFIG and its existence under one cross-process read lease."""
    with config_read_lease():
        exists = CONFIG.exists()
        return exists, load_json(CONFIG, {})


def atomic_config_compare_and_swap(
    expected: dict,
    value: dict,
    *,
    expected_exists: bool = True,
) -> None:
    """Publish a CONFIG update only if the exact loaded snapshot is current."""
    if not isinstance(expected, dict) or not isinstance(value, dict):
        raise TypeError("configuration_compare_and_swap_requires_dict")
    with _config_write_lease():
        current_exists = CONFIG.exists()
        current = load_json(CONFIG, {})
        if current_exists is not expected_exists or current != expected:
            raise RuntimeError("configuration_changed_before_publish")
        _atomic_json_unlocked(CONFIG, value)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_json(path: Path, default: dict | None = None) -> dict:
    if not path.exists():
        return dict(default or {})
    return json.loads(path.read_text(encoding="utf-8"))


def _canonical_json_sha256(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _reader_file_identity(path: Path) -> dict[str, object]:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"reader_contract_resource_invalid:{path.name}")
    return {
        "status": "available",
        "sha256": sha256_file(path),
        "size_bytes": path.stat().st_size,
    }


def _reader_tools_dir() -> Path:
    candidates = (
        ENGINE / "layer1" / "scripts",
        Path(__file__).resolve().parents[3] / "scripts",
    )
    required = {*READER_PROCESSING_CODE_FILES, "adapt_layer1_to_local_memory.py"}
    for candidate in candidates:
        if (
            candidate.is_dir()
            and not candidate.is_symlink()
            and all(
                (candidate / name).is_file()
                and not (candidate / name).is_symlink()
                for name in required
            )
        ):
            return candidate.resolve(strict=True)
    raise RuntimeError("reader_generation_contract_tools_missing")


def _reader_schema_dir() -> Path:
    candidates = (
        ENGINE / "layer1" / "schemas",
        Path(__file__).resolve().parents[3] / "schemas",
    )
    for candidate in candidates:
        if (
            candidate.is_dir()
            and not candidate.is_symlink()
            and all(
                (candidate / name).is_file()
                and not (candidate / name).is_symlink()
                for name in READER_SCHEMA_FILES
            )
        ):
            return candidate.resolve(strict=True)
    raise RuntimeError("reader_generation_contract_schemas_missing")


def _current_reader_resource_contract() -> dict[str, object]:
    """Fingerprint executable Reader bytes, not producer version assertions."""
    tools = _reader_tools_dir()
    schemas = _reader_schema_dir()
    return {
        "adaptive_builder": _reader_file_identity(
            ENGINE / "build_adaptive_semantic_graph.py"
        ),
        "adaptive_validator": _reader_file_identity(
            ENGINE / "validate_adaptive_semantic_graph.py"
        ),
        "adapter": _reader_file_identity(
            tools / "adapt_layer1_to_local_memory.py"
        ),
        "processing_code": {
            name: _reader_file_identity(tools / name)
            for name in READER_PROCESSING_CODE_FILES
        },
        "schemas": {
            name: _reader_file_identity(schemas / name)
            for name in READER_SCHEMA_FILES
        },
    }


def _required_reader_json(path: Path) -> dict:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 16 * 1024 * 1024:
        raise ValueError(f"reader_contract_artifact_invalid:{path.name}")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"reader_contract_artifact_not_object:{path.name}")
    return value


def _reader_stage_identity(
    semantic: Path,
    adaptive_state: dict,
    stage_name: str,
    relative_path: str,
) -> dict[str, object]:
    stages = adaptive_state.get("stages")
    stage = stages.get(stage_name) if isinstance(stages, dict) else None
    if (
        not isinstance(stage, dict)
        or stage.get("path") != relative_path
        or not isinstance(stage.get("sha256"), str)
    ):
        raise ValueError(f"reader_contract_stage_invalid:{stage_name}")
    identity = _reader_file_identity(semantic / relative_path)
    if stage["sha256"] != identity["sha256"]:
        raise ValueError(f"reader_contract_stage_hash_mismatch:{stage_name}")
    return identity


def _reader_output_identity(
    semantic: Path,
    adaptive_state: dict,
    output_name: str,
) -> dict[str, object]:
    outputs = adaptive_state.get("outputs")
    output = outputs.get(output_name) if isinstance(outputs, dict) else None
    file_name = f"semantic-{output_name}.jsonl"
    if (
        not isinstance(output, dict)
        or output.get("path") != file_name
        or not isinstance(output.get("sha256"), str)
        or not isinstance(output.get("count"), int)
        or isinstance(output.get("count"), bool)
        or output.get("count", -1) < 0
    ):
        raise ValueError(f"reader_contract_output_invalid:{output_name}")
    identity = _reader_file_identity(semantic / file_name)
    if output["sha256"] != identity["sha256"]:
        raise ValueError(f"reader_contract_output_hash_mismatch:{output_name}")
    return {**identity, "count": output["count"]}


def _reader_generation_contract_body(semantic: Path) -> dict[str, object]:
    if semantic.is_symlink() or not semantic.is_dir():
        raise ValueError("reader_contract_semantic_directory_invalid")
    semantic = semantic.resolve(strict=True)
    adaptive_path = semantic / "adaptive-reader-state.json"
    adaptive_state = _required_reader_json(adaptive_path)
    if adaptive_state.get("status") not in {"complete", "complete_with_limits"}:
        raise ValueError("reader_contract_adaptive_state_incomplete")

    artifacts = {
        "adaptive_state": _reader_file_identity(adaptive_path),
        "intermediate_state": _reader_stage_identity(
            semantic,
            adaptive_state,
            "intermediate",
            "layer1-intermediate/build-state.json",
        ),
        "intermediate_validation": _reader_stage_identity(
            semantic,
            adaptive_state,
            "intermediate_validation",
            "layer1-validation-state.json",
        ),
        "search_state": _reader_stage_identity(
            semantic,
            adaptive_state,
            "search",
            "layer1-search/search-build-state.json",
        ),
        "adapter_state": _reader_stage_identity(
            semantic,
            adaptive_state,
            "adapter",
            "layer1-adapter/layer1-adapter-state.json",
        ),
        "semantic_documents": _reader_output_identity(
            semantic, adaptive_state, "documents"
        ),
        "semantic_evidence": _reader_output_identity(
            semantic, adaptive_state, "evidence"
        ),
    }

    intermediate = _required_reader_json(
        semantic / "layer1-intermediate/build-state.json"
    )
    validation = _required_reader_json(semantic / "layer1-validation-state.json")
    adapter = _required_reader_json(
        semantic / "layer1-adapter/layer1-adapter-state.json"
    )
    fingerprint = intermediate.get("processing_fingerprint")
    if not isinstance(fingerprint, dict) or not isinstance(
        fingerprint.get("payload"), dict
    ):
        raise ValueError("reader_contract_processing_fingerprint_missing")
    if fingerprint.get("sha256") != _canonical_json_sha256(fingerprint["payload"]):
        raise ValueError("reader_contract_processing_fingerprint_hash_mismatch")

    resources = _current_reader_resource_contract()
    stored_processing_code = fingerprint["payload"].get("code")
    if stored_processing_code != resources["processing_code"]:
        raise ValueError("reader_contract_processing_code_mismatch")
    if (
        intermediate.get("extractor") != fingerprint["payload"].get("extractor")
        or intermediate.get("extractor_version")
        != fingerprint["payload"].get("extractor_version")
    ):
        raise ValueError("reader_contract_extractor_binding_mismatch")
    if (
        validation.get("status") != "pass"
        or validation.get("intermediate_state_sha256")
        != artifacts["intermediate_state"]["sha256"]
        or validation.get("schema_validation")
        not in {"draft202012", "structural_contract_only"}
    ):
        raise ValueError("reader_contract_validation_binding_mismatch")
    adapter_source_state = adapter.get("source_state")
    adapter_outputs = adapter.get("outputs")
    adapter_documents = (
        adapter_outputs.get("documents") if isinstance(adapter_outputs, dict) else None
    )
    adapter_evidence = (
        adapter_outputs.get("evidence") if isinstance(adapter_outputs, dict) else None
    )
    if (
        not isinstance(adapter_source_state, dict)
        or adapter_source_state.get("sha256")
        != artifacts["intermediate_state"]["sha256"]
        or not isinstance(adapter_documents, dict)
        or adapter_documents.get("sha256")
        != artifacts["semantic_documents"]["sha256"]
        or not isinstance(adapter_evidence, dict)
        or adapter_evidence.get("sha256")
        != artifacts["semantic_evidence"]["sha256"]
    ):
        raise ValueError("reader_contract_adapter_binding_mismatch")

    body: dict[str, object] = {
        "schema_version": READER_GENERATION_CONTRACT_SCHEMA_VERSION,
        "record_type": "reader_generation_contract",
        "semantic_directory_name": semantic.name,
        "resources": resources,
        "generation_artifacts": artifacts,
        "producer_records": {
            "builder": {
                "name": adaptive_state.get("builder"),
                "version": adaptive_state.get("builder_version"),
            },
            "extractor": {
                "name": intermediate.get("extractor"),
                "version": intermediate.get("extractor_version"),
                "processing_fingerprint_sha256": fingerprint.get("sha256"),
            },
            "adapter": {
                "name": adapter.get("adapter"),
                "version": adapter.get("adapter_version"),
            },
            "schema_validation": validation.get("schema_validation"),
        },
    }
    return {
        **body,
        "logical_sha256": _canonical_json_sha256(body),
    }


def write_reader_generation_contract(
    semantic: Path,
    generation_name: str,
) -> dict[str, object]:
    if GENERATION_NAME.fullmatch(generation_name) is None:
        raise ValueError("reader_contract_generation_name_invalid")
    if semantic.parent.name != generation_name or semantic.parent.is_symlink():
        raise ValueError("reader_contract_generation_binding_invalid")
    contract = _reader_generation_contract_body(semantic)
    contract_path = semantic / READER_GENERATION_CONTRACT_FILENAME
    if contract_path.exists() or contract_path.is_symlink():
        raise FileExistsError("reader_generation_contract_already_exists")
    atomic_json(contract_path, contract)
    return {
        "schema_version": READER_GENERATION_CONTRACT_SCHEMA_VERSION,
        "status": "current",
        "generation": generation_name,
        "path": str(contract_path),
        "sha256": sha256_file(contract_path),
        "logical_sha256": contract["logical_sha256"],
    }


def reader_generation_contract_status(config: dict) -> dict[str, object]:
    """Report migration need without changing or invalidating the active index."""
    index_value = config.get("index_path")
    if not isinstance(index_value, str) or not index_value or not Path(index_value).is_file():
        return {"state": "not_built", "migration_required": False}
    try:
        generation_name = config.get("active_generation")
        if not isinstance(generation_name, str) or GENERATION_NAME.fullmatch(
            generation_name
        ) is None:
            raise ValueError("active_generation_invalid")
        semantic_value = config.get("semantic_path")
        if not isinstance(semantic_value, str) or not semantic_value:
            raise ValueError("semantic_path_missing")
        semantic = Path(semantic_value)
        generation = semantic.parent
        if generation.name != generation_name or semantic.is_symlink():
            raise ValueError("semantic_generation_binding_invalid")
        registration = config.get(READER_GENERATION_CONTRACT_CONFIG_KEY)
        if not isinstance(registration, dict):
            raise ValueError("reader_generation_contract_registration_missing")
        contract_path = semantic / READER_GENERATION_CONTRACT_FILENAME
        expected_registration = {
            "schema_version": READER_GENERATION_CONTRACT_SCHEMA_VERSION,
            "status": "current",
            "generation": generation_name,
            "path": str(contract_path),
        }
        if any(
            registration.get(key) != value
            for key, value in expected_registration.items()
        ):
            raise ValueError("reader_generation_contract_registration_invalid")
        if contract_path.is_symlink() or not contract_path.is_file():
            raise ValueError("reader_generation_contract_missing")
        if registration.get("sha256") != sha256_file(contract_path):
            raise ValueError("reader_generation_contract_hash_mismatch")
        contract = _required_reader_json(contract_path)
        expected_contract = _reader_generation_contract_body(semantic)
        if contract != expected_contract:
            raise ValueError("reader_generation_contract_content_mismatch")
        if registration.get("logical_sha256") != contract.get("logical_sha256"):
            raise ValueError("reader_generation_contract_logical_hash_mismatch")
        return {
            "state": "current",
            "migration_required": False,
            "generation": generation_name,
            "logical_sha256": contract["logical_sha256"],
        }
    except (OSError, ValueError, TypeError, RuntimeError, json.JSONDecodeError) as exc:
        reason = str(exc).split(":", 1)[0] or type(exc).__name__
        return {
            "state": "reader_migration_required",
            "migration_required": True,
            "reason_code": reason,
        }


def free_gb(path: Path) -> float:
    return shutil.disk_usage(path).free / 1024**3


def total_memory_gb() -> float | None:
    try:
        value = subprocess.check_output(["sysctl", "-n", "hw.memsize"], text=True, timeout=5)
        return int(value.strip()) / 1024**3
    except Exception:
        return None


def ollama_binary() -> str | None:
    candidates = [
        shutil.which("ollama"),
        "/opt/homebrew/bin/ollama",
        "/usr/local/bin/ollama",
        "/Applications/Ollama.app/Contents/Resources/ollama",
        str(Path.home() / "Applications/Ollama.app/Contents/Resources/ollama"),
    ]
    return next((item for item in candidates if item and Path(item).is_file()), None)


def ollama_online(timeout: int = 3) -> bool:
    try:
        with LOCAL_HTTP_OPENER.open(
            f"{OLLAMA}/api/tags", timeout=timeout
        ) as response:
            return response.status == 200
    except (OSError, urllib.error.URLError):
        return False


def model_names() -> set[str]:
    try:
        with LOCAL_HTTP_OPENER.open(
            f"{OLLAMA}/api/tags", timeout=5
        ) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return {str(item.get("name", "")) for item in payload.get("models", [])}
    except Exception:
        return set()


def diagnose() -> dict:
    system_version = platform.mac_ver()[0]
    memory = total_memory_gb()
    free = free_gb(Path.home())
    python_ok = sys.version_info >= (3, 10)
    ollama_path = ollama_binary()
    models = model_names() if ollama_online() else set()
    try:
        config = load_json(CONFIG)
        if not isinstance(config, dict):
            raise TypeError("config_not_object")
    except (OSError, ValueError, TypeError):
        config = {}
    source = Path(config["source_root"]) if config.get("source_root") else None
    index = Path(config["index_path"]) if config.get("index_path") else None
    reader_contract = reader_generation_contract_status(config)
    return {
        "macos": system_version,
        "architecture": platform.machine(),
        "memory_gb": round(memory, 1) if memory else None,
        "free_gb": round(free, 1),
        "python": platform.python_version(),
        "python_ok": python_ok,
        "ollama_installed": bool(ollama_path),
        "ollama_online": ollama_online(),
        "models": sorted(models),
        "source_root": str(source) if source else "",
        "source_exists": bool(source and source.is_dir()),
        "index_path": str(index) if index else "",
        "index_ready": bool(index and index.is_file()),
        "reader_generation_contract": reader_contract,
        "reader_migration_required": (
            reader_contract.get("migration_required") is True
        ),
        "cross_document_semantic_graph_shadow_enabled": (
            config.get(CROSS_DOCUMENT_SHADOW_FLAG, True) is True
        ),
        "cross_document_semantic_graph_storage_enabled": (
            config.get(CROSS_DOCUMENT_STORAGE_FLAG, True) is True
        ),
        "cross_document_semantic_graph_query_candidate_enabled": (
            config.get(CROSS_DOCUMENT_QUERY_CANDIDATE_FLAG, True) is True
        ),
        "cross_document_semantic_graph_independent_edge_audit_enabled": (
            config.get(CROSS_DOCUMENT_INDEPENDENT_EDGE_AUDIT_FLAG, True)
            is True
        ),
        "cross_document_semantic_graph_answer_promotion_enabled": (
            config.get(CROSS_DOCUMENT_ANSWER_PROMOTION_FLAG, False) is True
        ),
        "cross_document_semantic_graph_answer_promotion_configured": (
            CROSS_DOCUMENT_ANSWER_PROMOTION_FLAG in config
        ),
        "cross_document_semantic_graph_storage": config.get(
            CROSS_DOCUMENT_STORAGE_CONFIG_KEY
        ),
        "cross_document_semantic_graph_trust": config.get(
            CROSS_DOCUMENT_TRUST_CONFIG_KEY
        ),
        "ready": bool(python_ok and ollama_path and source and source.is_dir() and index and index.is_file()),
        "warnings": [
            *( ["RAMは24GB以上を推奨します。"] if memory and memory < 23 else [] ),
            *( ["空き容量は40GB以上を推奨します。"] if free < 40 else [] ),
            *( ["Intel Macでは12Bモデルの応答が大幅に遅くなります。"] if platform.machine() == "x86_64" else [] ),
        ],
    }


def run(
    command: list[str],
    log,
    *,
    environment: dict[str, str] | None = None,
) -> None:
    log.write("$ " + " ".join(command) + "\n")
    log.flush()
    process = subprocess.Popen(
        command,
        stdout=log,
        stderr=subprocess.STDOUT,
        text=True,
        env=environment,
    )
    code = process.wait()
    if code:
        raise RuntimeError(f"command_failed:{code}:{command[0]}")


def run_shadow_command(
    command: list[str],
    log,
    timeout_seconds: float = CROSS_DOCUMENT_SHADOW_TIMEOUT_SECONDS,
) -> None:
    """Run an observational tool with a hard timeout and no shell."""
    log.write("$ " + " ".join(command) + "\n")
    log.flush()
    process = subprocess.Popen(
        command,
        stdout=log,
        stderr=subprocess.STDOUT,
        text=True,
        close_fds=True,
    )
    label = Path(command[1]).name if len(command) > 1 else Path(command[0]).name
    try:
        code = process.wait(timeout=max(1.0, timeout_seconds))
    except subprocess.TimeoutExpired as exc:
        process.kill()
        process.wait()
        raise RuntimeError(f"shadow_command_timeout:{label}") from exc
    if code:
        raise RuntimeError(f"shadow_command_failed:{code}:{label}")


def _write_log(log, message: str) -> None:
    if log is not None:
        log.write(message.rstrip() + "\n")
        log.flush()


def _write_shadow_log(log, message: str) -> None:
    """Write observer diagnostics without making them a production gate."""
    try:
        _write_log(log, message)
    except Exception:
        # A broken diagnostic sink must not change a completed/held shadow
        # result or the availability of the already-published answer index.
        pass


def _loopback_no_proxy_environment() -> dict[str, str]:
    """Preserve external proxy settings while forcing Ollama traffic local."""
    environment = dict(os.environ)
    for key in ("NO_PROXY", "no_proxy"):
        existing = environment.get(key, "")
        values = [
            value.strip()
            for value in existing.split(",")
            if value.strip()
        ]
        for value in ("127.0.0.1", "localhost"):
            if value not in values:
                values.append(value)
        environment[key] = ",".join(values)
    return environment


def _wait_for_ollama(timeout: float) -> bool:
    deadline = time.monotonic() + max(0.0, timeout)
    while time.monotonic() < deadline:
        if ollama_online():
            return True
        time.sleep(0.25)
    return ollama_online()


def ollama_app_bundle() -> Path | None:
    apps = [Path("/Applications/Ollama.app"), Path.home() / "Applications" / "Ollama.app"]
    return next((item for item in apps if item.exists()), None)


def start_ollama(log=None, timeout: float = 30.0) -> None:
    """Start only a loopback Ollama daemon, retaining the app-first route.

    A Homebrew CLI installation has no app bundle to launch.  In that case we
    start ``ollama serve`` without a shell, pin its bind address to loopback,
    and keep its stdout/stderr in an application-owned log.
    """
    if ollama_online():
        return
    app = ollama_app_bundle()
    if app:
        _write_log(log, f"Ollama app start requested: {app}")
        subprocess.run(["open", "-gja", str(app)], check=False)
        if _wait_for_ollama(min(timeout, 15.0)):
            _write_log(log, "Ollama app daemon is reachable on 127.0.0.1:11434")
            return

    binary = ollama_binary()
    if not binary:
        raise RuntimeError("ollama_not_installed")
    serve_log_path = SUPPORT / "logs" / "ollama-serve.log"
    serve_log_path.parent.mkdir(parents=True, exist_ok=True)
    environment = _loopback_no_proxy_environment()
    environment["OLLAMA_HOST"] = "127.0.0.1:11434"
    with serve_log_path.open("a", encoding="utf-8", buffering=1) as serve_log:
        serve_log.write(f"[{now_iso()}] starting {binary} serve on loopback\n")
        process = subprocess.Popen(
            [binary, "serve"],
            stdout=serve_log,
            stderr=subprocess.STDOUT,
            text=True,
            env=environment,
            start_new_session=True,
            close_fds=True,
        )
    _write_log(
        log,
        f"Ollama CLI daemon start requested: {binary} serve; "
        f"pid={process.pid}; log={serve_log_path}",
    )
    if _wait_for_ollama(timeout):
        _write_log(log, "Ollama CLI daemon is reachable on 127.0.0.1:11434")
        return
    exit_code = process.poll()
    _write_log(log, f"Ollama CLI daemon did not become ready; exit_code={exit_code}")
    raise RuntimeError("ollama_not_running_after_local_serve")


def _model_installed(model: str, installed: set[str]) -> bool:
    canonical = model if ":" in model else f"{model}:latest"
    return model in installed or canonical in installed


def local_model_available(model: str) -> bool:
    return ollama_online() and _model_installed(model, model_names())


def ensure_models(models: list[str], log) -> list[str]:
    binary = ollama_binary()
    if not binary:
        raise RuntimeError("ollama_not_installed")
    start_ollama(log)
    installed = model_names()
    pulled: list[str] = []
    for model in dict.fromkeys(models):
        if not _model_installed(model, installed):
            run(
                [binary, "pull", model],
                log,
                environment=_loopback_no_proxy_environment(),
            )
            installed = model_names()
            if not _model_installed(model, installed):
                raise RuntimeError(f"model_pull_not_visible:{model}")
            pulled.append(model)
    return pulled


def configure_source(source: Path) -> dict:
    source = source.expanduser().resolve(strict=True)
    if not source.is_dir() or source.is_symlink():
        raise SystemExit("検索対象は実フォルダを指定してください。")
    defaults = {
        "embedding_model": "embeddinggemma:latest",
        "answer_model": "gemma4:12b",
        "audit_model": "gemma4:12b",
        "model_profile": "gemma4-validated-v1",
        "sequential_model_loading": True,
        CROSS_DOCUMENT_SHADOW_FLAG: True,
        CROSS_DOCUMENT_STORAGE_FLAG: True,
        CROSS_DOCUMENT_QUERY_CANDIDATE_FLAG: True,
        CROSS_DOCUMENT_INDEPENDENT_EDGE_AUDIT_FLAG: True,
        CROSS_DOCUMENT_ANSWER_PROMOTION_FLAG: True,
        "port": 8765,
    }
    config_exists, loaded_config = load_config_snapshot()
    config = dict(loaded_config) if config_exists else defaults
    if (
        config.get("answer_model") == "qwen3.5:9b"
        and config.get("audit_model") == "gemma4:12b"
        and not config.get("model_profile")
    ):
        config["answer_model"] = "gemma4:12b"
        config["model_profile"] = "gemma4-validated-v1"
    config.setdefault(CROSS_DOCUMENT_SHADOW_FLAG, True)
    config.setdefault(CROSS_DOCUMENT_STORAGE_FLAG, True)
    config.setdefault(CROSS_DOCUMENT_QUERY_CANDIDATE_FLAG, True)
    config.setdefault(CROSS_DOCUMENT_INDEPENDENT_EDGE_AUDIT_FLAG, True)
    # Fresh installs opt into the fully gated Step 5 path through the defaults
    # above.  Preserve a missing key in an older config so the UI can offer an
    # explicit rebuild migration instead of silently turning answer promotion
    # on while the old generation is still active.
    config["source_root"] = str(source)
    config["workspace"] = str(SUPPORT / "data")
    # Selecting a source invalidates the active generation immediately.  A
    # complete reader/security/index generation will publish fresh pointers.
    config["index_path"] = ""
    for key in (
        "active_generation",
        "path_graph_path",
        "semantic_path",
        "security_path",
        "semantic_graph_shadow_path",
        READER_GENERATION_CONTRACT_CONFIG_KEY,
        CROSS_DOCUMENT_STORAGE_CONFIG_KEY,
        CROSS_DOCUMENT_TRUST_CONFIG_KEY,
        BASE_ANSWER_INDEX_SHA256_KEY,
    ):
        config.pop(key, None)
    atomic_config_compare_and_swap(
        loaded_config,
        config,
        expected_exists=config_exists,
    )
    return config


def _generations_root(workspace: Path) -> Path | None:
    generations = workspace / "generations"
    try:
        support = SUPPORT.resolve(strict=False)
        resolved = generations.resolve(strict=False)
        resolved.relative_to(support)
    except (OSError, ValueError):
        return None
    if workspace.is_symlink() or generations.is_symlink():
        return None
    return generations


def _generation_path(workspace: Path, generation_name: str) -> Path | None:
    if not GENERATION_NAME.fullmatch(generation_name):
        return None
    generations = _generations_root(workspace)
    if generations is None:
        return None
    candidate = generations / generation_name
    if candidate.is_symlink():
        return None
    try:
        candidate.absolute().relative_to(generations.absolute())
    except ValueError:
        return None
    return candidate


def _pid_is_alive(pid: object) -> bool:
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _uses_build_execution_lease(record: object) -> bool:
    """Identify lifecycle records written while the build lease was held.

    Recovery itself holds that same exclusive lease.  Therefore a matching
    version proves that the original builder is no longer active even if its
    saved PID has since been reused by an unrelated process.  Legacy records
    lack the version and retain the conservative PID check.
    """
    return (
        isinstance(record, dict)
        and type(record.get(BUILD_EXECUTION_LEASE_VERSION_KEY)) is int
        and record.get(BUILD_EXECUTION_LEASE_VERSION_KEY)
        == BUILD_EXECUTION_LEASE_VERSION
    )


def _legacy_process_identity(pid: int) -> dict | None:
    """Read a bounded process identity for versionless build records."""
    try:
        completed = subprocess.run(
            [
                "/bin/ps",
                "-p",
                str(pid),
                "-o",
                "lstart=",
                "-o",
                "stat=",
                "-o",
                "command=",
            ],
            capture_output=True,
            text=True,
            timeout=1,
            check=False,
            close_fds=True,
            env={**os.environ, "LC_ALL": "C"},
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    fields = completed.stdout.strip().split(None, 6)
    if len(fields) < 6:
        return None
    try:
        started_at = time.mktime(
            time.strptime(" ".join(fields[:5]), "%a %b %d %H:%M:%S %Y")
        )
    except (OverflowError, ValueError):
        return None
    return {
        "started_at": started_at,
        "state": fields[5],
        "command": fields[6] if len(fields) == 7 else "",
    }


def _record_started_timestamp(*records: object) -> float | None:
    timestamps: list[float] = []
    for record in records:
        if not isinstance(record, dict):
            continue
        value = record.get("started_at")
        if not isinstance(value, str) or not value:
            continue
        try:
            timestamps.append(datetime.fromisoformat(value).timestamp())
        except (OverflowError, ValueError):
            continue
    return min(timestamps) if timestamps else None


def _legacy_build_owner_is_live(pid: object, *records: object) -> bool:
    """Reject a reused or zombie PID while preserving a real legacy build."""
    if not _pid_is_alive(pid):
        return False
    assert isinstance(pid, int) and not isinstance(pid, bool)
    identity = _legacy_process_identity(pid)
    if identity is None:
        # Inspection may be restricted by local policy.  Never retire a
        # possibly live legacy build merely because ``ps`` was unavailable.
        return True
    state = identity.get("state")
    if isinstance(state, str) and state.upper().startswith("Z"):
        return False
    record_started_at = _record_started_timestamp(*records)
    process_started_at = identity.get("started_at")
    if (
        record_started_at is not None
        and isinstance(process_started_at, (int, float))
        and not isinstance(process_started_at, bool)
    ):
        return (
            float(process_started_at)
            <= record_started_at + LEGACY_OWNER_START_TOLERANCE_SECONDS
        )
    command = identity.get("command")
    if isinstance(command, str) and command:
        return any(
            name in command
            for name in ("bootstrap.py", "local_memory_server.py")
        )
    return True


def _published_generation_inputs_ready(config: dict, generation: Path) -> bool:
    required = {
        "path_graph_path": "directory",
        "semantic_path": "directory",
        "security_path": "directory",
    }
    for key, kind in required.items():
        value = config.get(key)
        if not isinstance(value, str) or not value:
            return False
        candidate = Path(value)
        try:
            candidate.resolve(strict=True).relative_to(generation.resolve(strict=True))
        except (OSError, ValueError):
            return False
        if kind == "directory" and not candidate.is_dir():
            return False
    reader_state = Path(config["semantic_path"]) / "adaptive-reader-state.json"
    try:
        status = load_json(reader_state).get("status")
    except (OSError, ValueError, TypeError):
        return False
    return status in {"complete", "complete_with_limits"}


def _published_index_pointer_kind(
    config: dict,
    generation: Path,
) -> str | None:
    """Accept only the two non-symlink index locations owned by a generation."""
    value = config.get("index_path")
    if not isinstance(value, str) or not value:
        return None
    candidate = Path(value)
    if not candidate.is_absolute() or candidate.is_symlink():
        return None
    base = generation / "safe-answer-index.sqlite3"
    storage_dir = generation / CROSS_DOCUMENT_STORAGE_DIR
    enriched = storage_dir / "safe-answer-index.sqlite3"
    if candidate == base:
        kind = "base"
    elif candidate == enriched and not storage_dir.is_symlink():
        kind = "semantic_storage"
    else:
        return None
    if not candidate.is_file():
        return None
    try:
        candidate.resolve(strict=True).relative_to(generation.resolve(strict=True))
    except (OSError, ValueError):
        return None
    return kind


def _published_generation_ready(config: dict, generation: Path) -> bool:
    if not _published_generation_inputs_ready(config, generation):
        return False
    return _published_index_pointer_kind(config, generation) is not None


def _published_generation_base_ready(config: dict, generation: Path) -> bool:
    """Validate a published generation through its immutable base index.

    The active pointer may already name the semantic-storage copy, or may be
    the one field damaged by an interrupted pointer switch.  Recovery is
    therefore anchored to the original published index at the generation
    root, which the storage projector never mutates.
    """
    base_index = generation / "safe-answer-index.sqlite3"
    if base_index.is_symlink() or not base_index.is_file():
        return False
    try:
        if base_index.resolve(strict=True) != (
            generation.resolve(strict=True) / "safe-answer-index.sqlite3"
        ):
            return False
    except OSError:
        return False
    base_config = {**config, "index_path": str(base_index)}
    return _published_generation_ready(base_config, generation)


def _validate_answer_index_for_semantic_storage(index: Path) -> dict:
    """Validate an answer index with the production answer-graph contract."""
    if index.is_symlink() or not index.is_file():
        raise ValueError("semantic_storage_answer_index_invalid")
    validator_path = next(
        (
            candidate
            for candidate in (
                ENGINE / "answer_local_memory.py",
                Path(__file__).resolve().parents[1]
                / "engine"
                / "answer_local_memory.py",
            )
            if candidate.is_file() and not candidate.is_symlink()
        ),
        None,
    )
    if validator_path is None:
        raise ValueError("semantic_storage_answer_validator_missing")
    specification = importlib.util.spec_from_file_location(
        "local_memory_storage_base_answer_validator",
        validator_path,
    )
    if specification is None or specification.loader is None:
        raise ValueError("semantic_storage_answer_validator_unavailable")
    validator = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = validator
    try:
        specification.loader.exec_module(validator)
        connection = sqlite3.connect(
            index.resolve(strict=True).as_uri() + "?mode=ro", uri=True
        )
        try:
            connection.execute("PRAGMA query_only=ON")
            if connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                raise ValueError("semantic_storage_answer_integrity_invalid")
            return validator.validate_answer_graph_contract(connection)
        finally:
            connection.close()
    except sqlite3.Error as exc:
        raise ValueError("semantic_storage_answer_database_invalid") from exc
    finally:
        sys.modules.pop(specification.name, None)


def _validate_base_answer_index_for_storage_recovery(
    generation: Path,
) -> dict:
    """Prove the rollback target is the intact safe-answer graph index."""
    base_index = generation / "safe-answer-index.sqlite3"
    try:
        return _validate_answer_index_for_semantic_storage(base_index)
    except Exception as exc:
        raise ValueError("semantic_storage_base_index_invalid") from exc


def _ready_state(
    reader_state: dict,
    *,
    recovered: bool = False,
    semantic_graph_shadow: dict | None = None,
    semantic_graph_storage: dict | None = None,
    semantic_graph_query_candidate_enabled: bool | None = None,
    semantic_graph_independent_edge_audit_enabled: bool | None = None,
    semantic_graph_answer_promotion_enabled: bool | None = None,
) -> dict:
    recovered_fields = {
        "recovered_after_interruption": True,
        "recovered_at": now_iso(),
    } if recovered else {}
    shadow_fields = (
        {"cross_document_semantic_graph_shadow": semantic_graph_shadow}
        if isinstance(semantic_graph_shadow, dict)
        else {}
    )
    storage_fields = (
        {"cross_document_semantic_graph_storage": semantic_graph_storage}
        if isinstance(semantic_graph_storage, dict)
        else {}
    )
    candidate_fields = (
        {
            "cross_document_semantic_graph_query_candidate_enabled": (
                semantic_graph_query_candidate_enabled
            )
        }
        if isinstance(semantic_graph_query_candidate_enabled, bool)
        else {}
    )
    independent_audit_fields = (
        {
            "cross_document_semantic_graph_independent_edge_audit_enabled": (
                semantic_graph_independent_edge_audit_enabled
            )
        }
        if isinstance(semantic_graph_independent_edge_audit_enabled, bool)
        else {}
    )
    answer_promotion_fields = (
        {
            "cross_document_semantic_graph_answer_promotion_enabled": (
                semantic_graph_answer_promotion_enabled
            )
        }
        if isinstance(semantic_graph_answer_promotion_enabled, bool)
        else {}
    )
    if reader_state.get("status") == "complete_with_limits":
        return {
            "phase": "ready_with_limits",
            "message": "索引は作成しましたが、未対応または部分読取りのファイルがあります。",
            "error": "",
            "reader_limitations": reader_state.get("limitations", {}),
            **recovered_fields,
            **shadow_fields,
            **storage_fields,
            **candidate_fields,
            **independent_audit_fields,
            **answer_promotion_fields,
        }
    return {
        "phase": "ready",
        "message": "索引の作成が完了しました。",
        "error": "",
        **recovered_fields,
        **shadow_fields,
        **storage_fields,
        **candidate_fields,
        **independent_audit_fields,
        **answer_promotion_fields,
    }


def _observer_state_build_id(value: object) -> str | None:
    if not isinstance(value, dict):
        return None
    direct = value.get("build_id")
    if isinstance(direct, str) and direct:
        return direct
    shadow = value.get("shadow")
    nested = shadow.get("build_id") if isinstance(shadow, dict) else None
    return nested if isinstance(nested, str) and nested else None


def _base_index_hash_anchor(value: object) -> str | None:
    """Return a recorded base hash; malformed recorded values fail closed."""
    if not isinstance(value, dict):
        return None
    for key in (BASE_ANSWER_INDEX_SHA256_KEY, "base_index_sha256"):
        if key in value:
            candidate = value.get(key)
            return (
                candidate
                if isinstance(candidate, str)
                and re.fullmatch(r"[0-9a-f]{64}", candidate)
                else ""
            )
    base = value.get("base")
    if isinstance(base, dict) and "sqlite_sha256" in base:
        candidate = base.get("sqlite_sha256")
        return (
            candidate
            if isinstance(candidate, str)
            and re.fullmatch(r"[0-9a-f]{64}", candidate)
            else ""
        )
    return None


def _reconstruct_published_marker_for_observer_recovery(
    config: dict,
    current: dict,
    generation: Path,
) -> dict:
    """Rebuild only the lifecycle facts still attested by runtime state.

    The normal publication order can leave CONFIG and STATE durable while the
    generation-marker write is missing or truncated.  Runtime observer state
    is the independent lifecycle anchor in that window.  A completed storage
    artifact may corroborate its build id, but can never establish one by
    itself; disagreement deliberately produces an untrusted id so promotion
    fails closed and the validated base index is restored.
    """
    current_ids = {
        value
        for value in (
            _observer_state_build_id(
                current.get("cross_document_semantic_graph_shadow")
            ),
            _observer_state_build_id(
                current.get("cross_document_semantic_graph_storage")
            ),
        )
        if value is not None
    }
    artifact_id: str | None = None
    final_state = (
        generation
        / CROSS_DOCUMENT_STORAGE_DIR
        / CROSS_DOCUMENT_STORAGE_RUN_STATE
    )
    if (
        not final_state.is_symlink()
        and final_state.is_file()
        and not final_state.parent.is_symlink()
    ):
        try:
            artifact_id = _observer_state_build_id(load_json(final_state))
        except (OSError, ValueError, TypeError):
            artifact_id = None
    build_id = (
        next(iter(current_ids))
        if len(current_ids) == 1
        and (artifact_id is None or artifact_id in current_ids)
        else f"untrusted-recovery-{generation.name}"
    )
    base_hash = _base_index_hash_anchor(config)
    return {
        "schema_version": "0.1",
        "status": "published",
        "generation": generation.name,
        "build_id": build_id,
        "owner_pid": 0,
        "reconstructed_after_interruption": True,
        **(
            {BASE_ANSWER_INDEX_SHA256_KEY: base_hash}
            if base_hash
            else {}
        ),
        "cross_document_semantic_graph_shadow_enabled": (
            config.get(CROSS_DOCUMENT_SHADOW_FLAG, True) is True
        ),
        "cross_document_semantic_graph_storage_enabled": (
            config.get(CROSS_DOCUMENT_STORAGE_FLAG, True) is True
        ),
        "cross_document_semantic_graph_query_candidate_enabled": (
            config.get(CROSS_DOCUMENT_QUERY_CANDIDATE_FLAG, True) is True
        ),
        "cross_document_semantic_graph_independent_edge_audit_enabled": (
            config.get(CROSS_DOCUMENT_INDEPENDENT_EDGE_AUDIT_FLAG, True)
            is True
        ),
        "cross_document_semantic_graph_answer_promotion_enabled": (
            config.get(CROSS_DOCUMENT_ANSWER_PROMOTION_FLAG, False) is True
        ),
    }


@_single_recovery_execution
def recover_interrupted_build() -> dict:
    """Recover a published generation or retire a dead unpublished build.

    This is called once when the local server starts.  A live owner PID is
    never interrupted.  Deletion is restricted to either an exact unpublished
    generation whose marker still says ``building`` or the fixed unfinished
    shadow candidate inside an otherwise published generation.
    """
    try:
        config = load_json(CONFIG)
        if not isinstance(config, dict):
            raise TypeError("config_not_object")
    except (OSError, ValueError, TypeError) as exc:
        atomic_json(STATE, {
            "phase": "error",
            "message": "設定ファイルを読み取れないため、回答を停止しました。",
            "error": f"configuration_invalid: {type(exc).__name__}",
            "recovery_action": "reconfigure_source",
        })
        return {"status": "invalid_configuration", "removed": []}
    try:
        current = load_json(
            STATE,
            {
                "phase": "not_started",
                "message": "まだ索引は作成されていません。",
                "error": "",
            },
        )
        if not isinstance(current, dict):
            raise TypeError("state_not_object")
    except (OSError, ValueError, TypeError) as exc:
        atomic_json(STATE, {
            "phase": "error",
            "message": "実行状態を読み取れないため、回答を停止しました。",
            "error": f"runtime_state_invalid: {type(exc).__name__}",
            "recovery_action": "rebuild_index",
        })
        return {"status": "invalid_runtime_state", "removed": []}
    workspace = Path(config.get("workspace", SUPPORT / "data"))
    active_generation = config.get("active_generation")
    removed: list[str] = []

    def owner_is_live(pid: object, *records: object) -> bool:
        # Recovery runs before this server starts a build thread.  Equality can
        # only be a PID reused after reboot/process death, not a live owner.
        return (
            pid != os.getpid()
            and _legacy_build_owner_is_live(pid, *records)
        )

    def retire_if_orphan(name: str) -> bool:
        generation = _generation_path(workspace, name)
        if generation is None or not generation.is_dir() or name == active_generation:
            return False
        marker_path = generation / GENERATION_MARKER
        try:
            marker = load_json(marker_path)
        except (OSError, ValueError, TypeError):
            return False
        if (
            marker.get("status") != "building"
            or marker.get("generation") != name
            or (
                not _uses_build_execution_lease(marker)
                and owner_is_live(marker.get("owner_pid"), marker)
            )
        ):
            return False
        shutil.rmtree(generation)
        removed.append(name)
        return True

    if (
        current.get("phase") in {"ready", "ready_with_limits"}
        and not isinstance(active_generation, str)
    ):
        failed_state = {
            **current,
            "phase": "error",
            "message": "公開済み索引の世代情報がないため、回答を停止しました。",
            "error": "active_generation_missing",
            "recovery_action": "rebuild_index",
        }
        atomic_json(STATE, failed_state)
        return {"status": "invalid_active_generation", "removed": removed}

    if current.get("phase") in {"ready", "ready_with_limits"}:
        generation = _generation_path(workspace, active_generation)
        if generation is None:
            failed_state = {
                **current,
                "phase": "error",
                "message": (
                    "公開済み索引の世代パスを安全に確認できないため、回答を停止しました。"
                ),
                "error": "active_generation_boundary_invalid",
                "recovery_action": "rebuild_index",
            }
            atomic_json(STATE, failed_state)
            return {
                "status": "invalid_active_generation",
                "generation": active_generation,
                "removed": removed,
            }
        pointer_kind = _published_index_pointer_kind(config, generation)
        if pointer_kind is None:
            failed_state = {
                **current,
                "phase": "error",
                "message": (
                    "公開済み索引の参照先が世代境界と一致しないため、回答を停止しました。"
                ),
                "error": "active_index_pointer_boundary_invalid",
                "recovery_action": "rebuild_index",
            }
            atomic_json(STATE, failed_state)
            return {
                "status": "invalid_active_index_pointer",
                "generation": active_generation,
                "removed": removed,
            }
        active_base_hash = _base_index_hash_anchor(config)
        if (
            pointer_kind == "base"
            and active_base_hash is not None
            and (
                not active_base_hash
                or sha256_file(generation / "safe-answer-index.sqlite3")
                != active_base_hash
            )
        ):
            failed_state = {
                **current,
                "phase": "error",
                "message": (
                    "公開済みの元索引が作成時の内容と一致しないため、回答を停止しました。"
                ),
                "error": "active_base_index_hash_mismatch",
                "recovery_action": "rebuild_index",
            }
            atomic_json(STATE, failed_state)
            return {
                "status": "active_index_hash_invalid",
                "generation": active_generation,
                "removed": removed,
            }
        marker_path = generation / GENERATION_MARKER
        marker_was_readable = False
        marker: dict = {}
        if not marker_path.is_symlink() and marker_path.is_file():
            try:
                loaded_marker = load_json(marker_path)
                if isinstance(loaded_marker, dict):
                    marker = loaded_marker
                    marker_was_readable = True
            except (OSError, ValueError, TypeError):
                pass
        marker_reconstructed = False
        current_shadow = current.get("cross_document_semantic_graph_shadow")
        marker_shadow = marker.get("cross_document_semantic_graph_shadow")
        shadow_pending = (
            isinstance(current_shadow, dict)
            and current_shadow.get("status") == "pending"
        ) or (
            isinstance(marker_shadow, dict)
            and marker_shadow.get("status") == "pending"
        )
        current_storage = current.get(
            "cross_document_semantic_graph_storage"
        )
        marker_storage = marker.get(
            "cross_document_semantic_graph_storage"
        )
        storage_pending = (
            isinstance(current_storage, dict)
            and current_storage.get("status") == "pending"
        ) or (
            isinstance(marker_storage, dict)
            and marker_storage.get("status") == "pending"
        )
        storage_artifact_present = bool(
            generation is not None
            and (
                (
                    generation
                    / (CROSS_DOCUMENT_STORAGE_DIR + ".building")
                ).exists()
                or (
                    generation
                    / (CROSS_DOCUMENT_STORAGE_DIR + ".building")
                ).is_symlink()
                or (generation / CROSS_DOCUMENT_STORAGE_DIR).exists()
                or (generation / CROSS_DOCUMENT_STORAGE_DIR).is_symlink()
            )
        )
        storage_pointer_selected = bool(
            generation is not None
            and config.get("index_path")
            == str(
                generation
                / CROSS_DOCUMENT_STORAGE_DIR
                / "safe-answer-index.sqlite3"
            )
        )
        storage_registered = isinstance(
            config.get(CROSS_DOCUMENT_STORAGE_CONFIG_KEY), dict
        )
        storage_rollback_requested = (
            config.get(CROSS_DOCUMENT_STORAGE_FLAG, True) is not True
            and (
                storage_registered
                or (
                    storage_pointer_selected
                )
            )
        )
        storage_recovery_needed = (
            storage_pending
            or storage_registered
            or storage_artifact_present
            or storage_pointer_selected
            or storage_rollback_requested
        )
        recover_observers = shadow_pending or storage_recovery_needed
        marker_lifecycle_valid = (
            marker_was_readable
            and marker.get("status") in {"published", "building"}
            and marker.get("generation") == active_generation
            and isinstance(marker.get("build_id"), str)
            and bool(marker.get("build_id"))
        )
        if (
            recover_observers
            and not marker_lifecycle_valid
            and _published_generation_inputs_ready(config, generation)
        ):
            marker = _reconstruct_published_marker_for_observer_recovery(
                config,
                current,
                generation,
            )
            marker_reconstructed = True
        observer_recovery_boundary_ready = (
            marker.get("status") in {"published", "building"}
            and marker.get("generation") == active_generation
            and (
                _published_generation_base_ready(config, generation)
                or (
                    storage_recovery_needed
                    and _published_generation_inputs_ready(config, generation)
                )
            )
        )
        if recover_observers and not observer_recovery_boundary_ready:
            failed_state = {
                **current,
                "phase": "error",
                "message": (
                    "公開済み索引の回復境界を検証できないため、回答を停止しました。"
                ),
                "error": "published_observer_recovery_boundary_invalid",
                "recovery_action": "rebuild_index",
            }
            atomic_json(STATE, failed_state)
            return {
                "status": "invalid_published_observer_boundary",
                "generation": active_generation,
                "removed": removed,
            }
        if recover_observers:
            if (
                (shadow_pending or storage_pending)
                and not _uses_build_execution_lease(marker)
                and owner_is_live(marker.get("owner_pid"), marker, current)
            ):
                return {
                    "status": (
                        "active_shadow"
                        if shadow_pending
                        else "active_semantic_storage"
                    ),
                    "owner_pid": marker.get("owner_pid"),
                    "removed": removed,
                }
            recovered_shadow = (
                _recover_published_shadow_observer(
                    generation,
                    marker,
                    enabled=(
                        marker.get(
                            "cross_document_semantic_graph_shadow_enabled",
                            config.get(CROSS_DOCUMENT_SHADOW_FLAG, True),
                        )
                        is True
                    ),
                )
                if shadow_pending
                else (
                    current_shadow
                    if isinstance(current_shadow, dict)
                    else marker_shadow
                    if isinstance(marker_shadow, dict)
                    else None
                )
            )
            if storage_recovery_needed:
                recovered_config, recovered_storage, storage_action = (
                    _recover_published_semantic_graph_storage(
                        config, generation, marker, current
                    )
                )
            else:
                recovered_config = config
                recovered_storage = (
                    current_storage
                    if isinstance(current_storage, dict)
                    else marker_storage
                    if isinstance(marker_storage, dict)
                    else None
                )
                storage_action = "not_applicable"
            config = recovered_config
            query_candidate_enabled = (
                config.get(CROSS_DOCUMENT_QUERY_CANDIDATE_FLAG, True) is True
            )
            independent_edge_audit_enabled = (
                config.get(
                    CROSS_DOCUMENT_INDEPENDENT_EDGE_AUDIT_FLAG, True
                )
                is True
            )
            answer_promotion_enabled = (
                config.get(CROSS_DOCUMENT_ANSWER_PROMOTION_FLAG, False)
                is True
            )
            storage_is_steady = (
                not marker_reconstructed
                and not shadow_pending
                and marker.get("status") == "published"
                and storage_action in {"verified_complete", "disabled_steady"}
                and isinstance(recovered_storage, dict)
                and recovered_storage == current_storage
                and recovered_storage == marker_storage
                and current.get(
                    "cross_document_semantic_graph_query_candidate_enabled"
                )
                is query_candidate_enabled
                and marker.get(
                    "cross_document_semantic_graph_query_candidate_enabled"
                )
                is query_candidate_enabled
                and current.get(
                    "cross_document_semantic_graph_independent_edge_audit_enabled"
                )
                is independent_edge_audit_enabled
                and marker.get(
                    "cross_document_semantic_graph_independent_edge_audit_enabled"
                )
                is independent_edge_audit_enabled
                and current.get(
                    "cross_document_semantic_graph_answer_promotion_enabled"
                )
                is answer_promotion_enabled
                and marker.get(
                    "cross_document_semantic_graph_answer_promotion_enabled"
                )
                is answer_promotion_enabled
            )
            if storage_is_steady:
                return {
                    "status": "unchanged",
                    "generation": active_generation,
                    "storage_status": recovered_storage.get("status"),
                    "storage_action": storage_action,
                    "removed": removed,
                }
            marker_update = {
                **marker,
                "status": "published",
                "published_at": marker.get("published_at", now_iso()),
                "index_path": config.get("index_path", ""),
                "cross_document_semantic_graph_query_candidate_enabled": (
                    query_candidate_enabled
                ),
                "cross_document_semantic_graph_independent_edge_audit_enabled": (
                    independent_edge_audit_enabled
                ),
                "cross_document_semantic_graph_answer_promotion_enabled": (
                    answer_promotion_enabled
                ),
            }
            if isinstance(recovered_storage, dict):
                marker_update[
                    "cross_document_semantic_graph_storage"
                ] = recovered_storage
            if isinstance(recovered_shadow, dict):
                marker_update[
                    "cross_document_semantic_graph_shadow"
                ] = recovered_shadow
            atomic_json(marker_path, marker_update)
            recovered_state = {
                **current,
                "cross_document_semantic_graph_query_candidate_enabled": (
                    query_candidate_enabled
                ),
                "cross_document_semantic_graph_independent_edge_audit_enabled": (
                    independent_edge_audit_enabled
                ),
                "cross_document_semantic_graph_answer_promotion_enabled": (
                    answer_promotion_enabled
                ),
            }
            if isinstance(recovered_storage, dict):
                recovered_state[
                    "cross_document_semantic_graph_storage"
                ] = recovered_storage
            if (
                storage_recovery_needed
                and storage_action != "base_index_invalid"
            ):
                recovered_state[
                    "semantic_storage_recovered_after_interruption"
                ] = True
                recovered_state["semantic_storage_recovered_at"] = now_iso()
            if isinstance(recovered_shadow, dict):
                recovered_state[
                    "cross_document_semantic_graph_shadow"
                ] = recovered_shadow
            if shadow_pending:
                recovered_state["shadow_recovered_after_interruption"] = True
                recovered_state["shadow_recovered_at"] = now_iso()
            if storage_action == "base_index_invalid":
                recovered_state.pop(
                    "semantic_storage_recovered_after_interruption", None
                )
                recovered_state.pop("semantic_storage_recovered_at", None)
                recovered_state.update({
                    "phase": "error",
                    "message": (
                        "元の回答索引を検証できないため、安全のため回答を停止しました。"
                    ),
                    "error": "semantic_storage_base_index_invalid",
                    "recovery_action": "rebuild_index",
                    "semantic_storage_recovery_failed_at": now_iso(),
                })
            atomic_json(STATE, recovered_state)
            return {
                "status": (
                    "semantic_storage_recovery_failed_closed"
                    if storage_action == "base_index_invalid"
                    else "recovered_published_shadow"
                    if shadow_pending and not storage_recovery_needed
                    else "recovered_published_observers"
                ),
                "generation": active_generation,
                "shadow_status": (
                    recovered_shadow.get("status")
                    if isinstance(recovered_shadow, dict)
                    else None
                ),
                "storage_status": (
                    recovered_storage.get("status")
                    if isinstance(recovered_storage, dict)
                    else None
                ),
                "storage_action": storage_action,
                "removed": removed,
            }

    if current.get("phase") == "building":
        owner_pid = current.get("owner_pid")
        generation_name = current.get("generation")
        generation = (
            _generation_path(workspace, generation_name)
            if isinstance(generation_name, str) else None
        )
        marker_path = (
            generation / GENERATION_MARKER
            if generation is not None
            else None
        )
        published_marker: dict = {}
        if (
            marker_path is not None
            and not marker_path.is_symlink()
            and marker_path.is_file()
        ):
            try:
                loaded_marker = load_json(marker_path)
                if isinstance(loaded_marker, dict):
                    published_marker = loaded_marker
            except (OSError, ValueError, TypeError):
                pass
        if (
            not _uses_build_execution_lease(current)
            and not _uses_build_execution_lease(published_marker)
            and owner_is_live(owner_pid, current, published_marker)
        ):
            return {
                "status": "active",
                "owner_pid": owner_pid,
                "removed": removed,
            }
        current_build_id = current.get("build_id")
        lifecycle_is_bound = (
            isinstance(current_build_id, str)
            and bool(current_build_id)
            and published_marker.get("status") in {"building", "published"}
            and published_marker.get("generation") == generation_name
            and published_marker.get("build_id") == current_build_id
            and published_marker.get("owner_pid") == owner_pid
        )
        if (
            generation is not None
            and generation_name == active_generation
            and lifecycle_is_bound
            and _published_index_pointer_kind(config, generation) == "base"
            and _published_generation_base_ready(config, generation)
        ):
            reader_state = load_json(Path(config["semantic_path"]) / "adaptive-reader-state.json")
            recovered_marker = {
                **published_marker,
                "status": "published",
                "published_at": now_iso(),
                "cross_document_semantic_graph_query_candidate_enabled": (
                    config.get(CROSS_DOCUMENT_QUERY_CANDIDATE_FLAG, True)
                    is True
                ),
                "cross_document_semantic_graph_independent_edge_audit_enabled": (
                    config.get(
                        CROSS_DOCUMENT_INDEPENDENT_EDGE_AUDIT_FLAG, True
                    )
                    is True
                ),
                "cross_document_semantic_graph_answer_promotion_enabled": (
                    config.get(CROSS_DOCUMENT_ANSWER_PROMOTION_FLAG, False)
                    is True
                ),
            }
            recovered_shadow = _recover_published_shadow_observer(
                generation,
                recovered_marker,
                enabled=(
                    recovered_marker.get(
                        "cross_document_semantic_graph_shadow_enabled",
                        config.get(CROSS_DOCUMENT_SHADOW_FLAG, True),
                    )
                    is True
                ),
            )
            recovered_marker[
                "cross_document_semantic_graph_shadow"
            ] = recovered_shadow
            storage_recovery_needed = (
                CROSS_DOCUMENT_STORAGE_FLAG in recovered_marker
                or isinstance(
                    current.get("cross_document_semantic_graph_storage"),
                    dict,
                )
                or isinstance(
                    recovered_marker.get(
                        "cross_document_semantic_graph_storage"
                    ),
                    dict,
                )
                or isinstance(
                    config.get(CROSS_DOCUMENT_STORAGE_CONFIG_KEY), dict
                )
                or (
                    generation
                    / (CROSS_DOCUMENT_STORAGE_DIR + ".building")
                ).exists()
                or (
                    generation
                    / (CROSS_DOCUMENT_STORAGE_DIR + ".building")
                ).is_symlink()
                or (generation / CROSS_DOCUMENT_STORAGE_DIR).exists()
                or (generation / CROSS_DOCUMENT_STORAGE_DIR).is_symlink()
                or config.get("index_path")
                == str(
                    generation
                    / CROSS_DOCUMENT_STORAGE_DIR
                    / "safe-answer-index.sqlite3"
                )
            )
            if storage_recovery_needed:
                recovered_config, recovered_storage, storage_action = (
                    _recover_published_semantic_graph_storage(
                        config, generation, recovered_marker, current
                    )
                )
            else:
                recovered_config = config
                recovered_storage = None
                storage_action = "not_applicable"
            config = recovered_config
            recovered_marker["index_path"] = config.get("index_path", "")
            if isinstance(recovered_storage, dict):
                recovered_marker[
                    "cross_document_semantic_graph_storage"
                ] = recovered_storage
            atomic_json(marker_path, recovered_marker)
            recovered_runtime_state = _ready_state(
                reader_state,
                recovered=(storage_action != "base_index_invalid"),
                semantic_graph_shadow=recovered_shadow,
                semantic_graph_storage=(
                    recovered_storage
                    if isinstance(recovered_storage, dict)
                    else None
                ),
                semantic_graph_query_candidate_enabled=(
                    config.get(CROSS_DOCUMENT_QUERY_CANDIDATE_FLAG, True)
                    is True
                ),
                semantic_graph_independent_edge_audit_enabled=(
                    config.get(
                        CROSS_DOCUMENT_INDEPENDENT_EDGE_AUDIT_FLAG, True
                    )
                    is True
                ),
                semantic_graph_answer_promotion_enabled=(
                    config.get(CROSS_DOCUMENT_ANSWER_PROMOTION_FLAG, False)
                    is True
                ),
            )
            if storage_action == "base_index_invalid":
                recovered_runtime_state.update({
                    "phase": "error",
                    "message": (
                        "元の回答索引を検証できないため、安全のため回答を停止しました。"
                    ),
                    "error": "semantic_storage_base_index_invalid",
                    "recovery_action": "rebuild_index",
                    "semantic_storage_recovery_failed_at": now_iso(),
                })
            atomic_json(STATE, recovered_runtime_state)
            return {
                "status": (
                    "semantic_storage_recovery_failed_closed"
                    if storage_action == "base_index_invalid"
                    else "recovered_published"
                ),
                "generation": generation_name,
                "shadow_status": recovered_shadow.get("status"),
                "storage_status": (
                    recovered_storage.get("status")
                    if isinstance(recovered_storage, dict)
                    else None
                ),
                "storage_action": storage_action,
                "removed": removed,
            }
        if isinstance(generation_name, str):
            retire_if_orphan(generation_name)
        atomic_json(STATE, {
            "phase": "error",
            "message": "前回の索引作成が途中で終了しました。安全に再実行できます。",
            "error": "interrupted_build_recovered",
            "recovery_action": "retry_build",
            "recovered_at": now_iso(),
            "removed_orphan_generations": removed,
        })
        return {
            "status": "interrupted_build_failed_closed",
            "generation": generation_name,
            "removed": removed,
        }

    generations = _generations_root(workspace)
    if generations is not None and generations.is_dir():
        for candidate in sorted(generations.iterdir(), key=lambda item: item.name):
            if candidate.is_dir() and GENERATION_NAME.fullmatch(candidate.name):
                retire_if_orphan(candidate.name)
    return {"status": "recovered" if removed else "unchanged", "removed": removed}


def run_semantic_pipeline(
    source: Path,
    paths: Path,
    semantic: Path,
    security: Path,
    log,
) -> dict:
    for path in (semantic, security):
        path.mkdir(parents=True, exist_ok=False)
    run([
        sys.executable, str(ENGINE / "build_adaptive_semantic_graph.py"),
        "--inventory", str(paths / "path-source-inventory.jsonl"),
        "--source-root", str(source), "--output-dir", str(semantic),
    ], log)
    run([
        sys.executable, str(ENGINE / "validate_adaptive_semantic_graph.py"),
        "--output-dir", str(semantic), "--source-root", str(source),
        "--inventory", str(paths / "path-source-inventory.jsonl"),
    ], log)
    reader_state = load_json(semantic / "adaptive-reader-state.json")
    run([
        sys.executable, str(ENGINE / "content_security_gate.py"),
        "--evidence", str(semantic / "semantic-evidence.jsonl"),
        "--documents", str(semantic / "semantic-documents.jsonl"),
        "--output-dir", str(security),
    ], log)
    run([
        sys.executable, str(ENGINE / "validate_content_security_gate.py"),
        "--evidence", str(semantic / "semantic-evidence.jsonl"),
        "--documents", str(semantic / "semantic-documents.jsonl"),
        "--gate-dir", str(security),
    ], log)
    return reader_state


def semantic_contains_images(semantic: Path) -> bool:
    manifest = load_json(semantic / "layer1-input-manifest.json")
    paths = manifest.get("paths", [])
    return isinstance(paths, list) and any(
        isinstance(value, str)
        and Path(value).suffix.casefold() in VISUAL_READER_SUFFIXES
        for value in paths
    )


def _cross_document_shadow_tools_dir() -> Path:
    candidates = (
        ENGINE / "layer1" / "scripts",
        Path(__file__).resolve().parents[3] / "scripts",
    )
    for candidate in candidates:
        if candidate.is_dir() and all(
            (candidate / name).is_file()
            and not (candidate / name).is_symlink()
            for name in CROSS_DOCUMENT_SHADOW_TOOLS
        ):
            return candidate
    raise RuntimeError("cross_document_semantic_graph_shadow_tools_missing")


def _cross_document_storage_tool() -> Path:
    candidate = _cross_document_shadow_tools_dir() / CROSS_DOCUMENT_STORAGE_TOOL
    if candidate.is_file() and not candidate.is_symlink():
        return candidate
    raise RuntimeError("cross_document_semantic_graph_storage_tool_missing")


def _semantic_graph_trust_module():
    """Load the packaged trust module without relying on caller sys.path."""
    module_name = "local_memory_semantic_graph_trust"
    existing = sys.modules.get(module_name)
    if existing is not None:
        return existing
    path = Path(__file__).resolve().with_name("semantic_graph_trust.py")
    if path.is_symlink() or not path.is_file():
        raise RuntimeError("semantic_graph_trust_module_missing")
    specification = importlib.util.spec_from_file_location(module_name, path)
    if specification is None or specification.loader is None:
        raise RuntimeError("semantic_graph_trust_module_unavailable")
    module = importlib.util.module_from_spec(specification)
    sys.modules[module_name] = module
    try:
        specification.loader.exec_module(module)
    except Exception:
        sys.modules.pop(module_name, None)
        raise
    return module


def _trust_locator_from_verified_root(module, verified: dict) -> dict:
    locator = {
        key: verified[key]
        for key in module.TRUST_REGISTRATION_FIELDS
    }
    return locator


def _publish_semantic_graph_trust_root(
    generation: Path,
    build_id: str,
    registration: dict,
    storage_state: dict,
) -> dict:
    """Create a per-generation Keychain root before answer activation."""
    module = _semantic_graph_trust_module()
    locator = module.publish_trust_root(
        generation,
        build_id,
        registration,
        storage_state,
        module.KeychainTrustStore(),
    )
    module.validate_trust_registration(
        locator,
        generation,
        registration,
    )
    return locator


def _recover_semantic_graph_trust_root(
    generation: Path,
    build_id: str,
    registration: dict,
    storage_state: dict,
) -> dict:
    """Recover only when an already-existing Keychain root proves the bytes."""
    module = _semantic_graph_trust_module()
    verified = module.recover_trust_manifest(
        generation,
        build_id,
        registration,
        storage_state,
        module.KeychainTrustStore(),
    )
    locator = _trust_locator_from_verified_root(module, verified)
    module.validate_trust_registration(
        locator,
        generation,
        registration,
        verified_root=verified,
    )
    return locator


def _content_security_shadow_validator() -> Path:
    candidates = (
        ENGINE / "validate_content_security_gate.py",
        Path(__file__).resolve().parents[1]
        / "engine"
        / "validate_content_security_gate.py",
    )
    for candidate in candidates:
        builder = candidate.with_name("content_security_gate.py")
        if (
            candidate.is_file()
            and not candidate.is_symlink()
            and builder.is_file()
            and not builder.is_symlink()
        ):
            return candidate
    raise RuntimeError("content_security_shadow_validator_missing")


def _require_generation_input(path: Path, generation: Path, expected_name: str) -> None:
    if path.name != expected_name or path.is_symlink() or not path.is_file():
        raise ValueError(f"shadow_input_invalid:{expected_name}")
    try:
        path.resolve(strict=True).relative_to(generation.resolve(strict=True))
    except (OSError, ValueError) as exc:
        raise ValueError(f"shadow_input_outside_generation:{expected_name}") from exc


def _attest_cross_document_shadow_inputs(
    semantic: Path,
    security: Path,
    generation: Path,
) -> dict[str, str]:
    documents = semantic / "semantic-documents.jsonl"
    source_evidence = semantic / "semantic-evidence.jsonl"
    evidence = security / "safe-answer-evidence.jsonl"
    security_state_path = security / "content-security-state.json"
    _require_generation_input(
        documents, generation, "semantic-documents.jsonl"
    )
    _require_generation_input(
        source_evidence, generation, "semantic-evidence.jsonl"
    )
    _require_generation_input(
        evidence, generation, "safe-answer-evidence.jsonl"
    )
    _require_generation_input(
        security_state_path, generation, "content-security-state.json"
    )
    security_state = load_json(security_state_path)
    required_security = {
        "schema_version": "0.1",
        "policy_version": "0.2.0",
        "classifier": "deterministic_content_security_gate",
        "question_independent": True,
        "llm_used_for_classification": False,
        "all_source_content_trust": "untrusted",
        "execution_policy": "never_execute",
        "safe_answer_index_allowed": True,
        "prompt_library_requires_explicit_mode": True,
        "quarantine_index_allowed": False,
    }
    if any(
        security_state.get(key) != expected
        for key, expected in required_security.items()
    ):
        raise ValueError("shadow_content_security_contract_invalid")
    source_evidence_state = security_state.get("source_evidence")
    source_documents = security_state.get("source_documents")
    outputs = security_state.get("outputs")
    safe_output = (
        outputs.get("safe-answer-evidence.jsonl")
        if isinstance(outputs, dict)
        else None
    )
    documents_sha256 = sha256_file(documents)
    source_evidence_sha256 = sha256_file(source_evidence)
    evidence_sha256 = sha256_file(evidence)
    if (
        not isinstance(source_evidence_state, dict)
        or source_evidence_state.get("sha256") != source_evidence_sha256
    ):
        raise ValueError("shadow_semantic_evidence_hash_mismatch")
    if (
        not isinstance(source_documents, dict)
        or source_documents.get("sha256") != documents_sha256
    ):
        raise ValueError("shadow_semantic_documents_hash_mismatch")
    if (
        not isinstance(safe_output, dict)
        or safe_output.get("sha256") != evidence_sha256
        or safe_output.get("size_bytes") != evidence.stat().st_size
    ):
        raise ValueError("shadow_safe_evidence_hash_mismatch")
    return {
        "semantic_documents_sha256": documents_sha256,
        "semantic_evidence_sha256": source_evidence_sha256,
        "safe_answer_evidence_sha256": evidence_sha256,
        "content_security_state_sha256": sha256_file(security_state_path),
    }


def _shadow_run_base(
    generation: Path,
    build_id: str,
    *,
    status: str,
    reason_code: str,
    elapsed_ms: int,
) -> dict:
    return {
        "schema_version": "0.1",
        "record_type": "cross_document_semantic_graph_shadow_run",
        "status": status,
        "reason_code": reason_code,
        "shadow_only": True,
        "used_for_index": False,
        "used_for_answers": False,
        "feature_flag": CROSS_DOCUMENT_SHADOW_FLAG,
        "generation": generation.name,
        "build_id": build_id,
        "elapsed_ms": elapsed_ms,
        "output_directory": CROSS_DOCUMENT_SHADOW_DIR,
        "execution_mode": "post_publish_observer",
        "failure_gates_production_index": False,
    }


def _storage_run_base(
    generation: Path,
    build_id: str,
    *,
    status: str,
    reason_code: str,
    elapsed_ms: int,
) -> dict:
    return {
        "schema_version": "0.1",
        "record_type": (
            "cross_document_semantic_graph_answer_index_projection_state"
        ),
        "status": status,
        "reason_code": reason_code,
        "storage_only": True,
        "retrieval_enabled": False,
        "used_for_answers": False,
        "answer_behavior_changed": False,
        "feature_flag": CROSS_DOCUMENT_STORAGE_FLAG,
        "generation": generation.name,
        "build_id": build_id,
        "elapsed_ms": elapsed_ms,
        "output_directory": CROSS_DOCUMENT_STORAGE_DIR,
        "failure_gates_production_index": False,
    }


def _remove_shadow_candidate(candidate: Path, generation: Path) -> None:
    if (
        candidate.parent == generation
        and candidate.name == CROSS_DOCUMENT_SHADOW_DIR + ".building"
        and (candidate.exists() or candidate.is_symlink())
    ):
        if candidate.is_symlink():
            candidate.unlink()
        elif candidate.is_dir():
            shutil.rmtree(candidate)


def _remove_shadow_failure_candidate(candidate: Path, generation: Path) -> None:
    if (
        candidate.parent == generation
        and candidate.name == CROSS_DOCUMENT_SHADOW_DIR + ".held-building"
        and (candidate.exists() or candidate.is_symlink())
    ):
        if candidate.is_symlink():
            candidate.unlink()
        elif candidate.is_dir():
            shutil.rmtree(candidate)


def _remove_storage_candidate(candidate: Path, generation: Path) -> None:
    if (
        candidate.parent == generation
        and candidate.name == CROSS_DOCUMENT_STORAGE_DIR + ".building"
        and (candidate.exists() or candidate.is_symlink())
    ):
        if candidate.is_symlink():
            candidate.unlink()
        elif candidate.is_dir():
            shutil.rmtree(candidate)


def _publish_shadow_failure_state(
    generation: Path,
    state: dict,
    log,
) -> None:
    final = generation / CROSS_DOCUMENT_SHADOW_DIR
    candidate = generation / (CROSS_DOCUMENT_SHADOW_DIR + ".held-building")
    try:
        if final.exists() or final.is_symlink():
            raise FileExistsError("shadow_failure_state_target_exists")
        _remove_shadow_failure_candidate(candidate, generation)
        candidate.mkdir()
        atomic_json(candidate / CROSS_DOCUMENT_SHADOW_RUN_STATE, state)
        os.replace(candidate, final)
    except Exception as exc:
        try:
            _remove_shadow_failure_candidate(candidate, generation)
        except Exception:
            pass
        _write_shadow_log(
            log,
            "Cross-document semantic graph shadow failure state could not be "
            f"persisted: {type(exc).__name__}: {exc}",
        )


def _recover_published_shadow_observer(
    generation: Path,
    marker: dict,
    *,
    enabled: bool,
) -> dict:
    recorded = marker.get("cross_document_semantic_graph_shadow")
    if isinstance(recorded, dict) and recorded.get("status") != "pending":
        return recorded
    if not enabled and not isinstance(recorded, dict):
        return _shadow_run_base(
            generation,
            str(marker.get("build_id", "unknown")),
            status="disabled",
            reason_code="feature_disabled",
            elapsed_ms=0,
        )

    candidate = generation / (CROSS_DOCUMENT_SHADOW_DIR + ".building")
    held_candidate = generation / (
        CROSS_DOCUMENT_SHADOW_DIR + ".held-building"
    )
    candidate_existed = candidate.exists() or candidate.is_symlink()
    held_candidate_existed = (
        held_candidate.exists() or held_candidate.is_symlink()
    )
    _remove_shadow_candidate(candidate, generation)
    _remove_shadow_failure_candidate(held_candidate, generation)
    final = generation / CROSS_DOCUMENT_SHADOW_DIR
    if final.is_dir() and not final.is_symlink():
        try:
            persisted = load_json(final / CROSS_DOCUMENT_SHADOW_RUN_STATE)
        except (OSError, ValueError, TypeError):
            persisted = {}
        if (
            persisted.get("status") in {"complete", "held"}
            and persisted.get("shadow_only") is True
            and persisted.get("used_for_index") is False
            and persisted.get("used_for_answers") is False
            and persisted.get("generation") == generation.name
            and persisted.get("build_id") == marker.get("build_id")
        ):
            return persisted

    state = {
        **_shadow_run_base(
            generation,
            str(marker.get("build_id", "unknown")),
            status="held",
            reason_code=(
                "shadow_interrupted_after_production_publish"
                if isinstance(recorded, dict)
                else "shadow_interrupted_before_observer_start"
            ),
            elapsed_ms=0,
        ),
        "external_network_used": None,
        "error": "shadow_observer_interrupted_after_production_publish",
        "upstream": {},
        "recovered_at": now_iso(),
        "removed_incomplete_candidate": (
            candidate_existed and not candidate.exists()
        ),
        "removed_incomplete_failure_state": (
            held_candidate_existed and not held_candidate.exists()
        ),
    }
    if not final.exists() and not final.is_symlink():
        _publish_shadow_failure_state(generation, state, None)
    return state


def run_cross_document_semantic_graph_shadow(
    config: dict,
    semantic: Path,
    security: Path,
    generation: Path,
    build_id: str,
    log,
) -> dict:
    """Build a validated observational graph without changing answer inputs."""
    if config.get(CROSS_DOCUMENT_SHADOW_FLAG, True) is not True:
        return _shadow_run_base(
            generation,
            build_id,
            status="disabled",
            reason_code="feature_disabled",
            elapsed_ms=0,
        )

    started = time.monotonic()
    final = generation / CROSS_DOCUMENT_SHADOW_DIR
    candidate = generation / (CROSS_DOCUMENT_SHADOW_DIR + ".building")
    documents = semantic / "semantic-documents.jsonl"
    source_evidence = semantic / "semantic-evidence.jsonl"
    evidence = security / "safe-answer-evidence.jsonl"
    security_state_path = security / "content-security-state.json"
    graph_path = candidate / "semantic-graph.sqlite3"
    graph_state_path = candidate / "semantic-graph-state.json"
    validation_state_path = candidate / "semantic-graph-validation.json"
    input_hashes: dict[str, str] = {}
    timeout_value = config.get(
        "cross_document_semantic_graph_shadow_timeout_seconds",
        CROSS_DOCUMENT_SHADOW_TIMEOUT_SECONDS,
    )
    timeout_seconds = (
        float(timeout_value)
        if isinstance(timeout_value, (int, float))
        and not isinstance(timeout_value, bool)
        and 1 <= float(timeout_value) <= 1800
        else CROSS_DOCUMENT_SHADOW_TIMEOUT_SECONDS
    )
    try:
        if (
            final.exists()
            or final.is_symlink()
            or candidate.exists()
            or candidate.is_symlink()
        ):
            raise FileExistsError("shadow_output_target_exists")
        input_hashes = _attest_cross_document_shadow_inputs(
            semantic, security, generation
        )
        tools_dir = _cross_document_shadow_tools_dir()
        security_validator = _content_security_shadow_validator()
        candidate.mkdir()
        _write_shadow_log(
            log,
            "Cross-document semantic graph shadow started; output is isolated "
            "from the production index and answer path.",
        )
        run_shadow_command(
            [
                sys.executable,
                str(tools_dir / "build_cross_document_semantic_graph.py"),
                "--documents",
                str(documents),
                "--evidence",
                str(evidence),
                "--output",
                str(graph_path),
                "--state",
                str(graph_state_path),
            ],
            log,
            timeout_seconds,
        )
        remaining_seconds = timeout_seconds - (time.monotonic() - started)
        if remaining_seconds <= 0:
            raise RuntimeError("shadow_total_timeout_before_validation")
        run_shadow_command(
            [
                sys.executable,
                str(tools_dir / "validate_cross_document_semantic_graph.py"),
                "--database",
                str(graph_path),
                "--state",
                str(graph_state_path),
                "--documents",
                str(documents),
                "--source-evidence",
                str(source_evidence),
                "--evidence",
                str(evidence),
                "--security-state",
                str(security_state_path),
                "--security-gate-dir",
                str(security),
                "--security-validator",
                str(security_validator),
                "--generation-dir",
                str(generation),
                "--output",
                str(validation_state_path),
            ],
            log,
            remaining_seconds,
        )
        validation = load_json(validation_state_path)
        if (
            validation.get("status") != "complete"
            or validation.get("question_independent") is not True
            or validation.get("external_network_used") is not False
            or validation.get("documents_input_sha256")
            != input_hashes["semantic_documents_sha256"]
            or validation.get("source_evidence_input_sha256")
            != input_hashes["semantic_evidence_sha256"]
            or validation.get("evidence_input_sha256")
            != input_hashes["safe_answer_evidence_sha256"]
            or validation.get("content_security_state_sha256")
            != input_hashes["content_security_state_sha256"]
            or validation.get("sqlite_sha256") != sha256_file(graph_path)
        ):
            raise ValueError("shadow_validation_state_invalid")
        state = {
            **_shadow_run_base(
                generation,
                build_id,
                status="complete",
                reason_code="none",
                elapsed_ms=round((time.monotonic() - started) * 1000),
            ),
            "external_network_used": False,
            "timeout_seconds": timeout_seconds,
            "graph_snapshot_id": validation.get("graph_snapshot_id"),
            "logical_snapshot_sha256": validation.get(
                "logical_snapshot_sha256"
            ),
            "sqlite_sha256": validation.get("sqlite_sha256"),
            "sqlite_size_bytes": graph_path.stat().st_size,
            "counts": validation.get("counts", {}),
            "relation_type_counts": validation.get(
                "relation_type_counts", {}
            ),
            "upstream": input_hashes,
            "tool_sha256": {
                name: sha256_file(tools_dir / name)
                for name in CROSS_DOCUMENT_SHADOW_TOOLS
            },
            "artifacts": {
                "database": graph_path.name,
                "builder_state": graph_state_path.name,
                "validation_state": validation_state_path.name,
                "run_state": CROSS_DOCUMENT_SHADOW_RUN_STATE,
            },
        }
        atomic_json(candidate / CROSS_DOCUMENT_SHADOW_RUN_STATE, state)
        os.replace(candidate, final)
        _write_shadow_log(
            log,
            "Cross-document semantic graph shadow complete; "
            f"snapshot={state['graph_snapshot_id']}; "
            f"nodes={state['counts'].get('nodes', 0)}; "
            f"edges={state['counts'].get('edges', 0)}; "
            "used_for_answers=false.",
        )
        return state
    except Exception as exc:
        try:
            _remove_shadow_candidate(candidate, generation)
        except Exception:
            pass
        state = {
            **_shadow_run_base(
                generation,
                build_id,
                status="held",
                reason_code="shadow_generation_failed_non_gating",
                elapsed_ms=round((time.monotonic() - started) * 1000),
            ),
            "external_network_used": None,
            "timeout_seconds": timeout_seconds,
            "error": f"{type(exc).__name__}: {exc}",
            "upstream": input_hashes,
        }
        _write_shadow_log(
            log,
            "Cross-document semantic graph shadow held without blocking the "
            f"published production index: {state['error']}",
        )
        try:
            _publish_shadow_failure_state(generation, state, log)
        except Exception as persist_exc:
            _write_shadow_log(
                log,
                "Cross-document semantic graph shadow failure-state publisher "
                f"raised unexpectedly: {type(persist_exc).__name__}: "
                f"{persist_exc}",
            )
        return state


def run_cross_document_semantic_graph_storage(
    config: dict,
    semantic: Path,
    security: Path,
    generation: Path,
    build_id: str,
    base_index: Path,
    shadow_state: dict,
    log,
) -> dict:
    """Project a validated shadow into a new, non-routable answer-index copy."""
    if config.get(CROSS_DOCUMENT_STORAGE_FLAG, True) is not True:
        return _storage_run_base(
            generation,
            build_id,
            status="disabled",
            reason_code="feature_disabled",
            elapsed_ms=0,
        )
    if shadow_state.get("status") != "complete":
        return _storage_run_base(
            generation,
            build_id,
            status="held",
            reason_code="validated_shadow_required",
            elapsed_ms=0,
        )

    started = time.monotonic()
    candidate = generation / (CROSS_DOCUMENT_STORAGE_DIR + ".building")
    final = generation / CROSS_DOCUMENT_STORAGE_DIR
    shadow_dir = generation / CROSS_DOCUMENT_SHADOW_DIR
    output_index = candidate / "safe-answer-index.sqlite3"
    output_state = candidate / CROSS_DOCUMENT_STORAGE_RUN_STATE
    timeout_value = config.get(
        "cross_document_semantic_graph_storage_timeout_seconds",
        CROSS_DOCUMENT_SHADOW_TIMEOUT_SECONDS,
    )
    timeout_seconds = (
        float(timeout_value)
        if isinstance(timeout_value, (int, float))
        and not isinstance(timeout_value, bool)
        and 1 <= float(timeout_value) <= 1800
        else CROSS_DOCUMENT_SHADOW_TIMEOUT_SECONDS
    )
    try:
        if (
            final.exists()
            or final.is_symlink()
            or candidate.exists()
            or candidate.is_symlink()
        ):
            raise FileExistsError("semantic_storage_output_target_exists")
        _require_generation_input(
            base_index, generation, "safe-answer-index.sqlite3"
        )
        base_index_sha256_before = sha256_file(base_index)
        input_hashes = _attest_cross_document_shadow_inputs(
            semantic, security, generation
        )
        projector = _cross_document_storage_tool()
        security_validator = _content_security_shadow_validator()
        run_shadow_command(
            [
                sys.executable,
                str(projector),
                "--base-index",
                str(base_index),
                "--shadow-dir",
                str(shadow_dir),
                "--documents",
                str(semantic / "semantic-documents.jsonl"),
                "--source-evidence",
                str(semantic / "semantic-evidence.jsonl"),
                "--evidence",
                str(security / "safe-answer-evidence.jsonl"),
                "--security-state",
                str(security / "content-security-state.json"),
                "--security-gate-dir",
                str(security),
                "--security-validator",
                str(security_validator),
                "--generation-dir",
                str(generation),
                "--output",
                str(output_index),
                "--state",
                str(output_state),
            ],
            log,
            timeout_seconds,
        )
        state = load_json(output_state)
        output = state.get("output")
        base = state.get("base")
        shadow = state.get("shadow")
        if (
            state.get("schema_version") != "0.1"
            or state.get("record_type")
            != "cross_document_semantic_graph_answer_index_projection_state"
            or state.get("status") != "complete"
            or state.get("question_independent") is not True
            or state.get("external_network_used") is not False
            or state.get("storage_only") is not True
            or state.get("retrieval_enabled") is not False
            or state.get("used_for_answers") is not False
            or state.get("answer_behavior_changed") is not False
            or state.get("generation") != generation.name
            or not isinstance(output, dict)
            or output.get("sqlite_file") != output_index.name
            or output.get("state_file") != output_state.name
            or output.get("sqlite_sha256") != sha256_file(output_index)
            or not isinstance(base, dict)
            or base.get("sqlite_file") != base_index.name
            or base.get("sqlite_sha256") != base_index_sha256_before
            or sha256_file(base_index) != base_index_sha256_before
            or not isinstance(shadow, dict)
            or shadow.get("directory") != shadow_dir.name
            or shadow.get("build_id") != shadow_state.get("build_id")
            or shadow.get("graph_snapshot_id")
            != shadow_state.get("graph_snapshot_id")
            or state.get("inputs") != {
                "content_security_state_sha256": input_hashes[
                    "content_security_state_sha256"
                ],
                "documents_input_sha256": input_hashes[
                    "semantic_documents_sha256"
                ],
                "evidence_input_sha256": input_hashes[
                    "safe_answer_evidence_sha256"
                ],
                "source_evidence_input_sha256": input_hashes[
                    "semantic_evidence_sha256"
                ],
            }
        ):
            raise ValueError("semantic_storage_state_invalid")
        os.replace(candidate, final)
        _write_shadow_log(
            log,
            "Cross-document semantic graph stored in a validated answer-index "
            "copy; retrieval_enabled=false; used_for_answers=false.",
        )
        return state
    except Exception as exc:
        try:
            _remove_storage_candidate(candidate, generation)
        except Exception:
            pass
        state = {
            **_storage_run_base(
                generation,
                build_id,
                status="held",
                reason_code="semantic_storage_failed_non_gating",
                elapsed_ms=round((time.monotonic() - started) * 1000),
            ),
            "external_network_used": None,
            "timeout_seconds": timeout_seconds,
            "error": f"{type(exc).__name__}: {exc}",
        }
        _write_shadow_log(
            log,
            "Cross-document semantic graph storage held; the published base "
            f"answer index remains active: {state['error']}",
        )
        return state


def _independently_validate_semantic_storage(
    *,
    generation: Path,
    semantic: Path,
    security: Path,
    base_index: Path,
    index: Path,
    state_path: Path,
    expected_build_id: str | None,
) -> dict:
    """Replay all storage, answer-graph, shadow, and Security bindings."""
    projector_path = _cross_document_storage_tool()
    specification = importlib.util.spec_from_file_location(
        "local_memory_semantic_storage_projector",
        projector_path,
    )
    if specification is None or specification.loader is None:
        raise ValueError("semantic_storage_projector_unavailable")
    projector = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = projector
    try:
        specification.loader.exec_module(projector)
        return projector.validate_existing_projection(
            base_index=base_index,
            shadow_dir=generation / CROSS_DOCUMENT_SHADOW_DIR,
            documents=semantic / "semantic-documents.jsonl",
            source_evidence=semantic / "semantic-evidence.jsonl",
            evidence=security / "safe-answer-evidence.jsonl",
            security_state=security / "content-security-state.json",
            security_gate_dir=security,
            security_validator=_content_security_shadow_validator(),
            generation_dir=generation,
            output=index,
            state=state_path,
            expected_build_id=expected_build_id,
        )
    finally:
        sys.modules.pop(specification.name, None)


def _semantic_storage_registration(
    generation: Path,
    state: dict,
    *,
    semantic: Path,
    security: Path,
    expected_build_id: str | None = None,
) -> dict:
    final = generation / CROSS_DOCUMENT_STORAGE_DIR
    index = final / "safe-answer-index.sqlite3"
    state_path = final / CROSS_DOCUMENT_STORAGE_RUN_STATE
    if (
        final.is_symlink()
        or not final.is_dir()
        or index.is_symlink()
        or not index.is_file()
        or state_path.is_symlink()
        or not state_path.is_file()
        or load_json(state_path) != state
    ):
        raise ValueError("semantic_storage_artifact_invalid")
    output = state.get("output")
    base = state.get("base")
    shadow = state.get("shadow")
    if (
        state.get("schema_version") != "0.1"
        or state.get("record_type")
        != "cross_document_semantic_graph_answer_index_projection_state"
        or state.get("projector")
        != "cross-document-semantic-graph-answer-index-projector"
        or state.get("status") != "complete"
        or state.get("question_independent") is not True
        or state.get("external_network_used") is not False
        or state.get("generation") != generation.name
        or state.get("storage_only") is not True
        or state.get("retrieval_enabled") is not False
        or state.get("used_for_answers") is not False
        or state.get("answer_behavior_changed") is not False
        or not isinstance(output, dict)
        or output.get("sqlite_file") != index.name
        or output.get("state_file") != state_path.name
        or output.get("sqlite_sha256") != sha256_file(index)
        or not isinstance(base, dict)
        or not isinstance(shadow, dict)
        or shadow.get("directory") != CROSS_DOCUMENT_SHADOW_DIR
        or not isinstance(shadow.get("build_id"), str)
        or not shadow.get("build_id")
        or (
            expected_build_id is not None
            and shadow.get("build_id") != expected_build_id
        )
    ):
        raise ValueError("semantic_storage_registration_invalid")
    base_index = generation / "safe-answer-index.sqlite3"
    if (
        base.get("sqlite_file") != base_index.name
        or base_index.is_symlink()
        or not base_index.is_file()
        or base.get("sqlite_sha256") != sha256_file(base_index)
    ):
        raise ValueError("semantic_storage_base_index_invalid")
    base_answer_contract = _validate_answer_index_for_semantic_storage(
        base_index
    )
    stored_answer_contract = _validate_answer_index_for_semantic_storage(index)
    normalized_stored_contract = {
        **stored_answer_contract,
        "metadata": {
            key: value
            for key, value in stored_answer_contract.get("metadata", {}).items()
            if not key.startswith("cross_document_semantic_graph_")
        },
    }
    if normalized_stored_contract != base_answer_contract:
        raise ValueError("semantic_storage_answer_contract_changed")

    independently_validated = _independently_validate_semantic_storage(
        generation=generation,
        semantic=semantic,
        security=security,
        base_index=base_index,
        index=index,
        state_path=state_path,
        expected_build_id=expected_build_id,
    )
    if independently_validated != state:
        raise ValueError("semantic_storage_independent_validation_mismatch")

    shadow_dir = generation / CROSS_DOCUMENT_SHADOW_DIR
    shadow_files = {
        "sqlite_sha256": shadow_dir / "semantic-graph.sqlite3",
        "builder_state_sha256": shadow_dir / "semantic-graph-state.json",
        "validation_state_sha256": (
            shadow_dir / "semantic-graph-validation.json"
        ),
        "run_state_sha256": shadow_dir / CROSS_DOCUMENT_SHADOW_RUN_STATE,
    }
    if shadow_dir.is_symlink() or not shadow_dir.is_dir():
        raise ValueError("semantic_storage_shadow_invalid")
    for key, path in shadow_files.items():
        if (
            path.is_symlink()
            or not path.is_file()
            or shadow.get(key) != sha256_file(path)
        ):
            raise ValueError(f"semantic_storage_shadow_hash_invalid:{key}")
    shadow_run = load_json(shadow_files["run_state_sha256"])
    if (
        shadow_run.get("status") != "complete"
        or shadow_run.get("generation") != generation.name
        or shadow_run.get("build_id") != shadow.get("build_id")
        or shadow_run.get("graph_snapshot_id")
        != shadow.get("graph_snapshot_id")
        or shadow_run.get("logical_snapshot_sha256")
        != shadow.get("logical_snapshot_sha256")
        or shadow_run.get("used_for_answers") is not False
    ):
        raise ValueError("semantic_storage_shadow_binding_invalid")

    counts = state.get("counts")
    if (
        not isinstance(counts, dict)
        or any(
            not isinstance(counts.get(key), int)
            or isinstance(counts.get(key), bool)
            or counts[key] < 0
            for key in ("nodes", "edges", "edge_evidence")
        )
    ):
        raise ValueError("semantic_storage_counts_invalid")
    try:
        connection = sqlite3.connect(
            index.resolve(strict=True).as_uri() + "?mode=ro", uri=True
        )
        try:
            connection.execute("PRAGMA query_only=ON")
            if connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                raise ValueError("semantic_storage_integrity_invalid")
            if connection.execute("PRAGMA foreign_key_check").fetchall():
                raise ValueError("semantic_storage_foreign_key_invalid")
            present_tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            required_tables = {
                "semantic_graph_nodes",
                "semantic_graph_edges",
                "semantic_graph_edge_evidence",
            }
            if not required_tables.issubset(present_tables):
                raise ValueError("semantic_storage_tables_missing")
            actual_counts = {
                "nodes": connection.execute(
                    "SELECT COUNT(*) FROM semantic_graph_nodes"
                ).fetchone()[0],
                "edges": connection.execute(
                    "SELECT COUNT(*) FROM semantic_graph_edges"
                ).fetchone()[0],
                "edge_evidence": connection.execute(
                    "SELECT COUNT(*) FROM semantic_graph_edge_evidence"
                ).fetchone()[0],
            }
            if actual_counts != {
                key: counts[key]
                for key in ("nodes", "edges", "edge_evidence")
            }:
                raise ValueError("semantic_storage_count_mismatch")
            stored_semantic_rows = {
                "nodes": connection.execute(
                    "SELECT node_id, node_type, canonical_key, status, "
                    "properties_json, record_sha256 FROM semantic_graph_nodes "
                    "ORDER BY node_id"
                ).fetchall(),
                "edges": connection.execute(
                    "SELECT edge_id, from_node_id, relation_type, to_node_id, "
                    "relation_class, status, basis_kind, basis_rule, "
                    "properties_json, record_sha256 FROM semantic_graph_edges "
                    "ORDER BY edge_id"
                ).fetchall(),
                "edge_evidence": connection.execute(
                    "SELECT edge_id, evidence_id FROM "
                    "semantic_graph_edge_evidence ORDER BY edge_id, evidence_id"
                ).fetchall(),
            }
            metadata = {
                key: json.loads(value)
                for key, value in connection.execute(
                    "SELECT key, value FROM metadata"
                )
            }
        finally:
            connection.close()
    except sqlite3.Error as exc:
        raise ValueError("semantic_storage_database_invalid") from exc

    try:
        shadow_connection = sqlite3.connect(
            shadow_files["sqlite_sha256"].resolve(strict=True).as_uri()
            + "?mode=ro",
            uri=True,
        )
        try:
            shadow_connection.execute("PRAGMA query_only=ON")
            if shadow_connection.execute(
                "PRAGMA integrity_check"
            ).fetchall() != [("ok",)]:
                raise ValueError("semantic_storage_source_integrity_invalid")
            source_semantic_rows = {
                "nodes": shadow_connection.execute(
                    "SELECT node_id, node_type, canonical_key, status, "
                    "properties_json, record_sha256 FROM nodes ORDER BY node_id"
                ).fetchall(),
                "edges": shadow_connection.execute(
                    "SELECT edge_id, from_node_id, relation_type, to_node_id, "
                    "relation_class, status, basis_kind, basis_rule, "
                    "properties_json, record_sha256 FROM edges ORDER BY edge_id"
                ).fetchall(),
                "edge_evidence": shadow_connection.execute(
                    "SELECT edge_id, evidence_id FROM edge_evidence "
                    "ORDER BY edge_id, evidence_id"
                ).fetchall(),
            }
        finally:
            shadow_connection.close()
    except sqlite3.Error as exc:
        raise ValueError("semantic_storage_source_database_invalid") from exc
    if stored_semantic_rows != source_semantic_rows:
        raise ValueError("semantic_storage_independent_row_mismatch")

    expected_metadata = {
        "cross_document_semantic_graph_storage_schema_version": "0.1",
        "cross_document_semantic_graph_storage_status": (
            "validated_storage_only"
        ),
        "cross_document_semantic_graph_retrieval_enabled": False,
        "cross_document_semantic_graph_used_for_answers": False,
        "cross_document_semantic_graph_question_independent": True,
        "cross_document_semantic_graph_external_network_used": False,
        "cross_document_semantic_graph_snapshot_id": shadow.get(
            "graph_snapshot_id"
        ),
        "cross_document_semantic_graph_logical_snapshot_sha256": shadow.get(
            "logical_snapshot_sha256"
        ),
        "cross_document_semantic_graph_node_count": counts["nodes"],
        "cross_document_semantic_graph_edge_count": counts["edges"],
        "cross_document_semantic_graph_edge_evidence_count": counts[
            "edge_evidence"
        ],
        "cross_document_semantic_graph_projection_sha256": state.get(
            "projection_sha256"
        ),
        "cross_document_semantic_graph_base_logical_snapshot_sha256": (
            base.get("logical_snapshot_sha256")
        ),
    }
    if any(metadata.get(key) != value for key, value in expected_metadata.items()):
        raise ValueError("semantic_storage_metadata_invalid")
    return {
        "schema_version": "0.1",
        "status": "validated_storage_only",
        "generation": generation.name,
        "database_path": str(index),
        "database_sha256": output["sqlite_sha256"],
        "state_path": str(state_path),
        "state_sha256": sha256_file(state_path),
        "base_index_path": str(base_index),
        "base_index_sha256": base["sqlite_sha256"],
        "graph_snapshot_id": shadow.get("graph_snapshot_id"),
        "logical_snapshot_sha256": shadow.get(
            "logical_snapshot_sha256"
        ),
        "counts": counts,
        "retrieval_enabled": False,
        "used_for_answers": False,
    }


def _recover_published_semantic_graph_storage(
    config: dict,
    generation: Path,
    marker: dict,
    current: dict,
) -> tuple[dict, dict, str]:
    """Finish or roll back only the storage-pointer portion of a build.

    A completed, strictly bound copy can be registered after a crash.  An
    incomplete or invalid copy is never routed: the immutable base index stays
    active and only the exact ``.building`` candidate is removed.
    """
    candidate = generation / (CROSS_DOCUMENT_STORAGE_DIR + ".building")
    final = generation / CROSS_DOCUMENT_STORAGE_DIR
    base_index = generation / "safe-answer-index.sqlite3"
    marker_build_id = marker.get("build_id")
    build_id = (
        marker_build_id
        if isinstance(marker_build_id, str) and marker_build_id
        else "unknown"
    )
    current_storage = current.get("cross_document_semantic_graph_storage")
    marker_storage = marker.get("cross_document_semantic_graph_storage")
    recorded = (
        current_storage
        if isinstance(current_storage, dict)
        else marker_storage
        if isinstance(marker_storage, dict)
        else None
    )
    candidate_existed = candidate.exists() or candidate.is_symlink()
    _remove_storage_candidate(candidate, generation)

    try:
        _validate_base_answer_index_for_storage_recovery(generation)
        recorded_base_hashes = [
            value
            for value in (
                _base_index_hash_anchor(config),
                _base_index_hash_anchor(marker),
                _base_index_hash_anchor(
                    config.get(CROSS_DOCUMENT_STORAGE_CONFIG_KEY)
                ),
                _base_index_hash_anchor(current_storage),
                _base_index_hash_anchor(marker_storage),
            )
            if value is not None
        ]
        actual_base_hash = sha256_file(base_index)
        if recorded_base_hashes and (
            "" in recorded_base_hashes
            or len(set(recorded_base_hashes)) != 1
            or recorded_base_hashes[0] != actual_base_hash
        ):
            raise ValueError("semantic_storage_base_hash_anchor_mismatch")
    except Exception as exc:
        return (
            config,
            {
                **_storage_run_base(
                    generation,
                    build_id,
                    status="held",
                    reason_code="semantic_storage_base_index_invalid",
                    elapsed_ms=0,
                ),
                "external_network_used": None,
                "error": f"{type(exc).__name__}: {exc}",
                "recovered_at": now_iso(),
                "removed_incomplete_candidate": (
                    candidate_existed and not candidate.exists()
                ),
            },
            "base_index_invalid",
        )

    enabled = (
        config.get(
            CROSS_DOCUMENT_STORAGE_FLAG,
            marker.get("cross_document_semantic_graph_storage_enabled", True),
        )
        is True
    )
    promotion_enabled = (
        config.get(
            CROSS_DOCUMENT_ANSWER_PROMOTION_FLAG,
            marker.get(
                "cross_document_semantic_graph_answer_promotion_enabled",
                False,
            ),
        )
        is True
    )

    def base_config() -> dict:
        restored = {
            **config,
            "index_path": str(base_index),
        }
        restored.pop(CROSS_DOCUMENT_STORAGE_CONFIG_KEY, None)
        restored.pop(CROSS_DOCUMENT_TRUST_CONFIG_KEY, None)
        return restored

    observer_build_ids = {
        value
        for value in (
            _observer_state_build_id(
                current.get("cross_document_semantic_graph_shadow")
            ),
            _observer_state_build_id(current_storage),
            _observer_state_build_id(
                marker.get("cross_document_semantic_graph_shadow")
            ),
            _observer_state_build_id(marker_storage),
        )
        if value is not None
    }
    if enabled and (
        not isinstance(marker_build_id, str)
        or not marker_build_id
        or any(value != marker_build_id for value in observer_build_ids)
    ):
        restored = base_config()
        if restored != config:
            atomic_config_compare_and_swap(config, restored)
        return (
            restored,
            {
                **_storage_run_base(
                    generation,
                    build_id,
                    status="held",
                    reason_code="semantic_storage_lifecycle_mismatch",
                    elapsed_ms=0,
                ),
                "external_network_used": None,
                "error": "semantic_storage_lifecycle_mismatch",
                "recovered_at": now_iso(),
                "removed_incomplete_candidate": (
                    candidate_existed and not candidate.exists()
                ),
            },
            "rolled_back_lifecycle_mismatch",
        )

    if not enabled:
        restored = base_config()
        if (
            restored == config
            and not candidate_existed
            and isinstance(current_storage, dict)
            and current_storage.get("status") == "disabled"
            and current_storage == marker_storage
        ):
            return restored, current_storage, "disabled_steady"
        if restored != config:
            atomic_config_compare_and_swap(config, restored)
        return (
            restored,
            {
                **_storage_run_base(
                    generation,
                    build_id,
                    status="disabled",
                    reason_code="feature_disabled",
                    elapsed_ms=0,
                ),
                "recovered_at": now_iso(),
                "removed_incomplete_candidate": (
                    candidate_existed and not candidate.exists()
                ),
            },
            "rolled_back_to_base" if restored != config else "disabled",
        )

    registration_error = ""
    if final.is_dir() and not final.is_symlink():
        try:
            persisted = load_json(final / CROSS_DOCUMENT_STORAGE_RUN_STATE)
            registration = _semantic_storage_registration(
                generation,
                persisted,
                semantic=Path(config["semantic_path"]),
                security=Path(config["security_path"]),
                expected_build_id=build_id,
            )
            trust_registration = None
            if promotion_enabled:
                trust_registration = _recover_semantic_graph_trust_root(
                    generation,
                    build_id,
                    registration,
                    persisted,
                )
            elif isinstance(
                config.get(CROSS_DOCUMENT_TRUST_CONFIG_KEY), dict
            ):
                try:
                    trust_registration = _recover_semantic_graph_trust_root(
                        generation,
                        build_id,
                        registration,
                        persisted,
                    )
                except Exception:
                    trust_registration = None
            promoted = {
                **config,
                "index_path": registration["database_path"],
                CROSS_DOCUMENT_STORAGE_CONFIG_KEY: registration,
            }
            if trust_registration is not None:
                promoted[CROSS_DOCUMENT_TRUST_CONFIG_KEY] = trust_registration
            else:
                promoted.pop(CROSS_DOCUMENT_TRUST_CONFIG_KEY, None)
            action = (
                "verified_complete"
                if promoted == config
                else "completed_pointer_switch"
            )
            if promoted != config:
                atomic_config_compare_and_swap(config, promoted)
            return promoted, persisted, action
        except Exception as exc:
            registration_error = f"{type(exc).__name__}: {exc}"
    elif final.exists() or final.is_symlink():
        registration_error = "ValueError: semantic_storage_final_invalid"

    restored = base_config()
    config_changed = restored != config
    if config_changed:
        atomic_config_compare_and_swap(config, restored)
    if (
        isinstance(recorded, dict)
        and recorded.get("status") in {"held", "disabled"}
        and not registration_error
        and not candidate_existed
    ):
        return (
            restored,
            recorded,
            "rolled_back_to_base" if config_changed else "kept_base",
        )
    held = {
        **_storage_run_base(
            generation,
            build_id,
            status="held",
            reason_code=(
                "semantic_storage_invalid_after_interruption"
                if registration_error
                else "semantic_storage_interrupted_after_base_publish"
            ),
            elapsed_ms=0,
        ),
        "external_network_used": None,
        "error": registration_error or "semantic_storage_projection_interrupted",
        "recovered_at": now_iso(),
        "removed_incomplete_candidate": (
            candidate_existed and not candidate.exists()
        ),
    }
    return (
        restored,
        held,
        "rolled_back_to_base" if config_changed else "kept_base",
    )


@_single_build_execution
def build_index() -> None:
    config_exists, loaded_config = load_config_snapshot()
    if not config_exists:
        raise RuntimeError("configuration_missing")
    config = dict(loaded_config)
    # Starting a rebuild is the explicit migration boundary for older
    # installations. Missing feature flags adopt the current fully-gated
    # pipeline, while an explicit false remains the rollback switch.
    config.setdefault(CROSS_DOCUMENT_SHADOW_FLAG, True)
    config.setdefault(CROSS_DOCUMENT_STORAGE_FLAG, True)
    config.setdefault(CROSS_DOCUMENT_QUERY_CANDIDATE_FLAG, True)
    config.setdefault(CROSS_DOCUMENT_INDEPENDENT_EDGE_AUDIT_FLAG, True)
    if CROSS_DOCUMENT_ANSWER_PROMOTION_FLAG not in config:
        config[CROSS_DOCUMENT_ANSWER_PROMOTION_FLAG] = True
        atomic_config_compare_and_swap(loaded_config, config)
    source = Path(config["source_root"]).resolve(strict=True)
    workspace = Path(config.get("workspace", SUPPORT / "data"))
    generations = workspace / "generations"
    generations.mkdir(parents=True, exist_ok=True)
    generation = generations / f"generation-{uuid.uuid4().hex}"
    generation.mkdir()
    build_id = uuid.uuid4().hex
    paths = generation / "01-path"
    semantic = generation / "02-semantic"
    security = generation / "03-security"
    index = generation / "safe-answer-index.sqlite3"
    log_path = SUPPORT / "logs" / "build.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    marker_path = generation / GENERATION_MARKER
    marker = {
        "schema_version": "0.1",
        "status": "building",
        BUILD_EXECUTION_LEASE_VERSION_KEY: BUILD_EXECUTION_LEASE_VERSION,
        "build_id": build_id,
        "generation": generation.name,
        "owner_pid": os.getpid(),
        "started_at": now_iso(),
        "source_root": str(source),
        "cross_document_semantic_graph_shadow_enabled": (
            config.get(CROSS_DOCUMENT_SHADOW_FLAG, True) is True
        ),
        "cross_document_semantic_graph_storage_enabled": (
            config.get(CROSS_DOCUMENT_STORAGE_FLAG, True) is True
        ),
        "cross_document_semantic_graph_query_candidate_enabled": (
            config.get(CROSS_DOCUMENT_QUERY_CANDIDATE_FLAG, True) is True
        ),
        "cross_document_semantic_graph_independent_edge_audit_enabled": (
            config.get(CROSS_DOCUMENT_INDEPENDENT_EDGE_AUDIT_FLAG, True)
            is True
        ),
        "cross_document_semantic_graph_answer_promotion_enabled": (
            config.get(CROSS_DOCUMENT_ANSWER_PROMOTION_FLAG, False) is True
        ),
    }
    atomic_json(marker_path, marker)
    state = {
        "phase": "building",
        "message": "索引を作成中です。",
        "error": "",
        BUILD_EXECUTION_LEASE_VERSION_KEY: BUILD_EXECUTION_LEASE_VERSION,
        "build_id": build_id,
        "generation": generation.name,
        "owner_pid": os.getpid(),
        "started_at": marker["started_at"],
        "cross_document_semantic_graph_query_candidate_enabled": (
            config.get(CROSS_DOCUMENT_QUERY_CANDIDATE_FLAG, True) is True
        ),
        "cross_document_semantic_graph_independent_edge_audit_enabled": (
            config.get(CROSS_DOCUMENT_INDEPENDENT_EDGE_AUDIT_FLAG, True)
            is True
        ),
        "cross_document_semantic_graph_answer_promotion_enabled": (
            config.get(CROSS_DOCUMENT_ANSWER_PROMOTION_FLAG, False) is True
        ),
    }
    atomic_json(STATE, state)
    generation_published = False
    reader_state: dict = {}
    reader_contract_registration: dict[str, object] = {}
    shadow_state = _shadow_run_base(
        generation,
        build_id,
        status="disabled",
        reason_code="not_started",
        elapsed_ms=0,
    )
    storage_state = _storage_run_base(
        generation,
        build_id,
        status="disabled",
        reason_code="not_started",
        elapsed_ms=0,
    )
    try:
        with log_path.open("w", encoding="utf-8", buffering=1) as log:
            paths.mkdir(parents=True, exist_ok=False)
            run([sys.executable, str(ENGINE / "build_path_graph.py"), str(source), "--output-dir", str(paths)], log)
            run([sys.executable, str(ENGINE / "validate_path_graph.py"), str(paths / "path-evidence-graph.json"), str(paths / "path-source-inventory.jsonl")], log)
            image_fallback_available_before_reader = local_model_available(
                IMAGE_FALLBACK_MODEL
            )
            reader_state = run_semantic_pipeline(source, paths, semantic, security, log)
            # Reader validity and content safety are established before any
            # model pull.  The /build action is the existing user-authorized
            # boundary for model downloads.
            pulled_models = ensure_models([
                config["embedding_model"],
                config["answer_model"],
                config["audit_model"],
                IMAGE_FALLBACK_MODEL,
            ], log)
            image_fallback_available_after_models = local_model_available(
                IMAGE_FALLBACK_MODEL
            )
            # On a clean Mac the first native pass cannot use Gemma because it
            # is intentionally pulled only after reader/security validation.
            # Re-run the semantic and security stages into new empty paths so
            # the same authorized build can retain an unlocated transcript.
            if (
                not image_fallback_available_before_reader
                and image_fallback_available_after_models
                and semantic_contains_images(semantic)
            ):
                semantic_after_pull = generation / "02-semantic-model-ready"
                security_after_pull = generation / "03-security-model-ready"
                _write_log(
                    log,
                    "Gemma image fallback became available; rebuilding the validated "
                    "semantic/security generation before publication; newly_pulled="
                    f"{IMAGE_FALLBACK_MODEL in pulled_models}.",
                )
                reader_state = run_semantic_pipeline(
                    source, paths, semantic_after_pull, security_after_pull, log
                )
                semantic = semantic_after_pull
                security = security_after_pull
            reader_contract_registration = write_reader_generation_contract(
                semantic,
                generation.name,
            )
            run([
                sys.executable, str(ENGINE / "build_local_semantic_index.py"),
                "--evidence", str(security / "safe-answer-evidence.jsonl"),
                "--documents", str(semantic / "semantic-documents.jsonl"),
                "--security-state", str(security / "content-security-state.json"),
                "--source-root", str(source),
                "--source-inventory", str(paths / "path-source-inventory.jsonl"),
                "--index-purpose", "safe_answer", "--model", config["embedding_model"],
                "--output", str(index),
            ], log)
            base_index_sha256 = sha256_file(index)
            published_exists, published_snapshot = load_config_snapshot()
            if not published_exists:
                raise RuntimeError("configuration_changed_during_build")
            published_config = dict(published_snapshot)
            try:
                configuration_matches_build = (
                    Path(published_config["source_root"]).resolve(strict=True)
                    == source
                    and Path(
                        published_config.get("workspace", SUPPORT / "data")
                    ).resolve(strict=False)
                    == workspace.resolve(strict=False)
                    and published_config.get("embedding_model")
                    == config.get("embedding_model")
                )
            except (KeyError, OSError, TypeError, ValueError):
                configuration_matches_build = False
            if not configuration_matches_build:
                raise RuntimeError("configuration_changed_during_build")
            published_config.setdefault(CROSS_DOCUMENT_SHADOW_FLAG, True)
            published_config.setdefault(CROSS_DOCUMENT_STORAGE_FLAG, True)
            published_config.setdefault(CROSS_DOCUMENT_QUERY_CANDIDATE_FLAG, True)
            published_config.setdefault(
                CROSS_DOCUMENT_INDEPENDENT_EDGE_AUDIT_FLAG, True
            )
            published_config.setdefault(
                CROSS_DOCUMENT_ANSWER_PROMOTION_FLAG, True
            )
            published_config.pop("semantic_graph_shadow_path", None)
            published_config.pop(CROSS_DOCUMENT_STORAGE_CONFIG_KEY, None)
            published_config.pop(CROSS_DOCUMENT_TRUST_CONFIG_KEY, None)
            published_config.update({
                "active_generation": generation.name,
                "path_graph_path": str(paths),
                "semantic_path": str(semantic),
                "security_path": str(security),
                "index_path": str(index),
                READER_GENERATION_CONTRACT_CONFIG_KEY: (
                    reader_contract_registration
                ),
                BASE_ANSWER_INDEX_SHA256_KEY: base_index_sha256,
            })
            atomic_config_compare_and_swap(
                published_snapshot,
                published_config,
            )
            active_index = index
            generation_published = True
            published_at = now_iso()
            shadow_state = _shadow_run_base(
                generation,
                build_id,
                status=(
                    "pending"
                    if published_config.get(CROSS_DOCUMENT_SHADOW_FLAG, True)
                    is True
                    else "disabled"
                ),
                reason_code=(
                    "scheduled_after_production_publish"
                    if published_config.get(CROSS_DOCUMENT_SHADOW_FLAG, True)
                    is True
                    else "feature_disabled"
                ),
                elapsed_ms=0,
            )
            storage_enabled = (
                published_config.get(CROSS_DOCUMENT_STORAGE_FLAG, True) is True
            )
            storage_state = _storage_run_base(
                generation,
                build_id,
                status=("pending" if storage_enabled else "disabled"),
                reason_code=(
                    "awaiting_validated_shadow"
                    if storage_enabled
                    else "feature_disabled"
                ),
                elapsed_ms=0,
            )
            published_marker = {
                **marker,
                "status": "published",
                "published_at": published_at,
                "semantic_path": str(semantic),
                "security_path": str(security),
                "index_path": str(index),
                READER_GENERATION_CONTRACT_CONFIG_KEY: (
                    reader_contract_registration
                ),
                BASE_ANSWER_INDEX_SHA256_KEY: base_index_sha256,
                "cross_document_semantic_graph_query_candidate_enabled": (
                    published_config.get(
                        CROSS_DOCUMENT_QUERY_CANDIDATE_FLAG, True
                    )
                    is True
                ),
                "cross_document_semantic_graph_independent_edge_audit_enabled": (
                    published_config.get(
                        CROSS_DOCUMENT_INDEPENDENT_EDGE_AUDIT_FLAG, True
                    )
                    is True
                ),
                "cross_document_semantic_graph_answer_promotion_enabled": (
                    published_config.get(
                        CROSS_DOCUMENT_ANSWER_PROMOTION_FLAG, False
                    )
                    is True
                ),
                "cross_document_semantic_graph_shadow": shadow_state,
                "cross_document_semantic_graph_storage": storage_state,
            }
            marker_warning = ""
            try:
                atomic_json(marker_path, published_marker)
            except OSError as exc:
                marker_warning = f"{type(exc).__name__}: {exc}"
            state = _ready_state(
                reader_state,
                semantic_graph_shadow=shadow_state,
                semantic_graph_storage=storage_state,
                semantic_graph_query_candidate_enabled=(
                    published_config.get(
                        CROSS_DOCUMENT_QUERY_CANDIDATE_FLAG, True
                    )
                    is True
                ),
                semantic_graph_independent_edge_audit_enabled=(
                    published_config.get(
                        CROSS_DOCUMENT_INDEPENDENT_EDGE_AUDIT_FLAG, True
                    )
                    is True
                ),
                semantic_graph_answer_promotion_enabled=(
                    published_config.get(
                        CROSS_DOCUMENT_ANSWER_PROMOTION_FLAG, False
                    )
                    is True
                ),
            )
            if marker_warning:
                state["generation_marker_warning"] = marker_warning
            # Production becomes queryable before the observer starts.  The
            # shadow can consume time or fail without delaying index publication.
            atomic_json(STATE, state)

            try:
                shadow_state = run_cross_document_semantic_graph_shadow(
                    published_config,
                    semantic,
                    security,
                    generation,
                    build_id,
                    log,
                )
                if not isinstance(shadow_state, dict):
                    raise TypeError("shadow_state_not_object")
            except Exception as exc:
                # A shadow implementation defect must not become an answer-index
                # availability failure.  The shadow remains fail-closed while
                # the already-published production path continues unchanged.
                shadow_state = {
                    **_shadow_run_base(
                        generation,
                        build_id,
                        status="held",
                        reason_code="shadow_orchestrator_failed_non_gating",
                        elapsed_ms=0,
                    ),
                    "external_network_used": None,
                    "error": f"{type(exc).__name__}: {exc}",
                    "upstream": {},
                }
                _write_shadow_log(
                    log,
                    "Cross-document semantic graph shadow orchestrator held; "
                    "the production index was already published: "
                    f"{shadow_state['error']}",
                )
                try:
                    _publish_shadow_failure_state(
                        generation, shadow_state, log
                    )
                except Exception as persist_exc:
                    _write_shadow_log(
                        log,
                        "Cross-document semantic graph shadow failure-state "
                        "publisher raised unexpectedly after production "
                        f"publication: {type(persist_exc).__name__}: "
                        f"{persist_exc}",
                    )

            latest_observer_config = load_json(CONFIG)
            if (
                latest_observer_config.get("active_generation")
                != generation.name
                or latest_observer_config.get("index_path") != str(index)
            ):
                raise RuntimeError("configuration_changed_during_observer")
            published_config = latest_observer_config
            try:
                storage_state = run_cross_document_semantic_graph_storage(
                    published_config,
                    semantic,
                    security,
                    generation,
                    build_id,
                    index,
                    shadow_state,
                    log,
                )
                if not isinstance(storage_state, dict):
                    raise TypeError("semantic_storage_state_not_object")
            except Exception as exc:
                storage_state = {
                    **_storage_run_base(
                        generation,
                        build_id,
                        status="held",
                        reason_code="semantic_storage_orchestrator_failed_non_gating",
                        elapsed_ms=0,
                    ),
                    "external_network_used": None,
                    "error": f"{type(exc).__name__}: {exc}",
                }
                _write_shadow_log(
                    log,
                    "Cross-document semantic graph storage orchestrator held; "
                    "the published base answer index remains active: "
                    f"{storage_state['error']}",
                )

            if storage_state.get("status") == "complete":
                latest_promotion_config = load_json(CONFIG)
                if (
                    latest_promotion_config.get("active_generation")
                    != generation.name
                    or latest_promotion_config.get("index_path") != str(index)
                ):
                    raise RuntimeError(
                        "configuration_changed_before_storage_registration"
                    )
                published_config = latest_promotion_config
                if (
                    published_config.get(CROSS_DOCUMENT_STORAGE_FLAG, True)
                    is not True
                ):
                    prepared = storage_state
                    storage_state = {
                        **_storage_run_base(
                            generation,
                            build_id,
                            status="disabled",
                            reason_code="feature_disabled_before_registration",
                            elapsed_ms=prepared.get("elapsed_ms", 0),
                        ),
                        "external_network_used": False,
                        "prepared_projection": {
                            "output": prepared.get("output", {}),
                            "shadow": prepared.get("shadow", {}),
                        },
                    }
                    _write_shadow_log(
                        log,
                        "Cross-document semantic graph storage was disabled before "
                        "registration; the published base answer index remains active.",
                    )
                else:
                    try:
                        registration = _semantic_storage_registration(
                            generation,
                            storage_state,
                            semantic=semantic,
                            security=security,
                            expected_build_id=build_id,
                        )
                        latest_registration_config = load_json(CONFIG)
                        if (
                            latest_registration_config.get(
                                "active_generation"
                            )
                            != generation.name
                            or latest_registration_config.get("index_path")
                            != str(index)
                        ):
                            raise RuntimeError(
                                "configuration_changed_during_storage_registration"
                            )
                        if (
                            latest_registration_config.get(
                                CROSS_DOCUMENT_STORAGE_FLAG, True
                            )
                            is not True
                        ):
                            prepared = storage_state
                            published_config = latest_registration_config
                            storage_state = {
                                **_storage_run_base(
                                    generation,
                                    build_id,
                                    status="disabled",
                                    reason_code=(
                                        "feature_disabled_during_registration"
                                    ),
                                    elapsed_ms=prepared.get("elapsed_ms", 0),
                                ),
                                "external_network_used": False,
                                "prepared_projection": {
                                    "output": prepared.get("output", {}),
                                    "shadow": prepared.get("shadow", {}),
                                },
                            }
                            _write_shadow_log(
                                log,
                                "Cross-document semantic graph storage was "
                                "disabled during registration validation; the "
                                "published base answer index remains active.",
                            )
                        else:
                            promotion_enabled = (
                                latest_registration_config.get(
                                    CROSS_DOCUMENT_ANSWER_PROMOTION_FLAG,
                                    False,
                                )
                                is True
                            )
                            trust_registration = (
                                _publish_semantic_graph_trust_root(
                                    generation,
                                    build_id,
                                    registration,
                                    storage_state,
                                )
                                if promotion_enabled
                                else None
                            )
                            (
                                latest_activation_exists,
                                latest_activation_snapshot,
                            ) = load_config_snapshot()
                            if not latest_activation_exists:
                                raise RuntimeError(
                                    "configuration_changed_during_trust_registration"
                                )
                            latest_activation_config = dict(
                                latest_activation_snapshot
                            )
                            if (
                                latest_activation_config.get(
                                    "active_generation"
                                )
                                != generation.name
                                or latest_activation_config.get("index_path")
                                != str(index)
                                or latest_activation_config.get(
                                    CROSS_DOCUMENT_STORAGE_FLAG, True
                                )
                                is not True
                                or (
                                    latest_activation_config.get(
                                        CROSS_DOCUMENT_ANSWER_PROMOTION_FLAG,
                                        False,
                                    )
                                    is True
                                )
                                is not promotion_enabled
                            ):
                                raise RuntimeError(
                                    "configuration_changed_during_trust_registration"
                                )
                            promoted_config = {
                                **latest_activation_config,
                                "index_path": registration["database_path"],
                                CROSS_DOCUMENT_STORAGE_CONFIG_KEY: registration,
                            }
                            if trust_registration is not None:
                                promoted_config[
                                    CROSS_DOCUMENT_TRUST_CONFIG_KEY
                                ] = trust_registration
                            else:
                                promoted_config.pop(
                                    CROSS_DOCUMENT_TRUST_CONFIG_KEY, None
                                )
                            atomic_config_compare_and_swap(
                                latest_activation_snapshot,
                                promoted_config,
                            )
                            published_config = promoted_config
                            active_index = Path(registration["database_path"])
                    except Exception as exc:
                        prepared = storage_state
                        storage_state = {
                            **_storage_run_base(
                                generation,
                                build_id,
                                status="held",
                                reason_code=(
                                    "semantic_storage_registration_failed_non_gating"
                                ),
                                elapsed_ms=prepared.get("elapsed_ms", 0),
                            ),
                            "external_network_used": None,
                            "error": f"{type(exc).__name__}: {exc}",
                            "prepared_projection": {
                                "output": prepared.get("output", {}),
                                "shadow": prepared.get("shadow", {}),
                            },
                        }
                        _write_shadow_log(
                            log,
                            "Cross-document semantic graph storage registration held; "
                            "the published base answer index remains active: "
                            f"{storage_state['error']}",
                        )

            marker_warning = ""
            try:
                atomic_json(marker_path, {
                    **published_marker,
                    "index_path": str(active_index),
                    "cross_document_semantic_graph_shadow_enabled": (
                        published_config.get(CROSS_DOCUMENT_SHADOW_FLAG, True)
                        is True
                    ),
                    "cross_document_semantic_graph_storage_enabled": (
                        published_config.get(CROSS_DOCUMENT_STORAGE_FLAG, True)
                        is True
                    ),
                    "cross_document_semantic_graph_query_candidate_enabled": (
                        published_config.get(
                            CROSS_DOCUMENT_QUERY_CANDIDATE_FLAG, True
                        )
                        is True
                    ),
                    "cross_document_semantic_graph_independent_edge_audit_enabled": (
                        published_config.get(
                            CROSS_DOCUMENT_INDEPENDENT_EDGE_AUDIT_FLAG, True
                        )
                        is True
                    ),
                    "cross_document_semantic_graph_answer_promotion_enabled": (
                        published_config.get(
                            CROSS_DOCUMENT_ANSWER_PROMOTION_FLAG, False
                        )
                        is True
                    ),
                    "cross_document_semantic_graph_shadow": shadow_state,
                    "cross_document_semantic_graph_storage": storage_state,
                })
            except OSError as exc:
                marker_warning = f"{type(exc).__name__}: {exc}"
            state = _ready_state(
                reader_state,
                semantic_graph_shadow=shadow_state,
                semantic_graph_storage=storage_state,
                semantic_graph_query_candidate_enabled=(
                    published_config.get(
                        CROSS_DOCUMENT_QUERY_CANDIDATE_FLAG, True
                    )
                    is True
                ),
                semantic_graph_independent_edge_audit_enabled=(
                    published_config.get(
                        CROSS_DOCUMENT_INDEPENDENT_EDGE_AUDIT_FLAG, True
                    )
                    is True
                ),
                semantic_graph_answer_promotion_enabled=(
                    published_config.get(
                        CROSS_DOCUMENT_ANSWER_PROMOTION_FLAG, False
                    )
                    is True
                ),
            )
            if marker_warning:
                state["generation_marker_warning"] = marker_warning
    except Exception as exc:
        state = {"phase": "error", "message": "索引の作成に失敗しました。", "error": f"{type(exc).__name__}: {exc}"}
        raise
    finally:
        if not generation_published and generation.exists():
            shutil.rmtree(generation)
        atomic_json(STATE, state)
def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("diagnose")
    configure = sub.add_parser("configure")
    configure.add_argument("source")
    sub.add_parser("build")
    args = parser.parse_args()
    if args.command == "diagnose":
        print(json.dumps(diagnose(), ensure_ascii=False, indent=2))
    elif args.command == "configure":
        print(json.dumps(configure_source(Path(args.source)), ensure_ascii=False, indent=2))
    else:
        build_index()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
