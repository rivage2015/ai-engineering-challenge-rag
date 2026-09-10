#!/usr/bin/env python3
"""Build resumable, streamed intermediate records from source documents.

The builder is question-independent. It recursively discovers supported
office/PDF files, writes per-document shards without retaining all records in
memory, and consolidates them only after every input reaches a terminal state.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import http.client
import importlib.metadata
import importlib.util
import json
import os
import platform
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from probe_intermediate_records import (
    Probe,
    canonical_json,
    digest_file,
    discover_password_candidates,
    nfc_path,
    stable_id,
)
from intermediate_build_integrity import ordered_shard_manifest_sha256
import local_image_ocr


SUPPORTED_SUFFIXES = {
    ".docx", ".xlsx", ".pptx", ".pdf",
    ".csv", ".tsv", ".json", ".xml", ".ipynb",
    ".md", ".txt", ".py", ".toml", ".yaml", ".yml", ".rst", ".sql", ".sh", ".command",
    ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp",
}
SKIP_DIRECTORY_NAMES = {".git", "__pycache__", ".ipynb_checkpoints", "node_modules"}
EXTRACTOR = "intermediate-record-extractor"
EXTRACTOR_VERSION = "0.11.0"
STATE_VERSION = "1"
STATE_FILE = "build-state.json"
LOCK_FILE = "build.lock"
PROCESSING_FINGERPRINT_VERSION = "1"
RECORD_FILES = {
    "documents": "documents.jsonl",
    "evidence": "evidence.jsonl",
    "relations": "relations.jsonl",
}
SKIPPABLE_STATUSES = {"success", "partial", "deferred"}
TERMINAL_STATUSES = SKIPPABLE_STATUSES | {"failed"}
PROCESSING_CODE_FILES = (
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
READER_DISTRIBUTIONS = {
    "Pillow": ("PIL",),
    "lxml": ("lxml",),
    "msoffcrypto-tool": ("msoffcrypto",),
    "openpyxl": ("openpyxl",),
    "python-docx": ("docx",),
    "python-pptx": ("pptx",),
}
OLLAMA_LOOPBACK_ENDPOINT = ("127.0.0.1", 11434)
OLLAMA_VERSION_RE = re.compile(
    r"^[0-9]+(?:\.[0-9]+)+(?:[-+][0-9A-Za-z][0-9A-Za-z.-]*)*$"
)
PADDLE_RUNTIME_LOCK_MAX_BYTES = 1024 * 1024
PADDLE_RUNTIME_MANIFEST_MAX_BYTES = 256 * 1024
PADDLE_RUNTIME_MANIFEST_MAX_DISTRIBUTIONS = 4096
PADDLE_RUNTIME_MANIFEST_PROBE_TIMEOUT_SECONDS = 30
PADDLE_RUNTIME_MANIFEST_PROBE = r'''
import importlib.util
import json
import sys
from pathlib import Path

worker_path = Path(sys.argv[1])
lock_path = Path(sys.argv[2])
output_path = Path(sys.argv[3])
maximum_distributions = int(sys.argv[4])
maximum_bytes = int(sys.argv[5])

specification = importlib.util.spec_from_file_location(
    "aiec_paddle_runtime_manifest_probe", worker_path
)
if specification is None or specification.loader is None:
    raise RuntimeError("PaddleOCR worker module is unavailable")
worker = importlib.util.module_from_spec(specification)
specification.loader.exec_module(worker)
lock = worker.verify_runtime_lock(lock_path)

installed = {}
for distribution in worker.metadata.distributions():
    name = distribution.metadata.get("Name")
    if not isinstance(name, str) or not name:
        raise RuntimeError("installed package metadata has no distribution name")
    normalized_name = worker._normalized_package_name(name)
    if normalized_name in installed:
        raise RuntimeError("installed package metadata contains duplicate names")
    version = distribution.version
    if not isinstance(version, str) or not version:
        raise RuntimeError("installed package metadata has no distribution version")
    installed[normalized_name] = version
    if len(installed) > maximum_distributions:
        raise RuntimeError("installed package manifest exceeds the distribution limit")

payload = {
    "runtime_lock": lock,
    "installed_distributions": [
        [name, installed[name]] for name in sorted(installed)
    ],
}
serialized = json.dumps(
    payload,
    ensure_ascii=False,
    sort_keys=True,
    separators=(",", ":"),
).encode("utf-8")
if len(serialized) > maximum_bytes:
    raise RuntimeError("installed package manifest exceeds the byte limit")
with output_path.open("xb") as handle:
    handle.write(serialized)
'''


def discover(root: Path) -> list[Path]:
    return sorted(
        (
            path.resolve() for path in root.rglob("*")
            if path.is_file()
            and path.suffix.lower() in SUPPORTED_SUFFIXES
            and not path.name.startswith("~$")
            and not any(part in SKIP_DIRECTORY_NAMES for part in path.parts)
        ),
        key=lambda path: nfc_path(path.relative_to(root)),
    )


def normalized_relative(root: Path, path: Path) -> str:
    try:
        return nfc_path(path.resolve().relative_to(root.resolve()))
    except ValueError as exc:
        raise ValueError(f"input is outside --root: {path}") from exc


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(canonical_json(value) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _code_identity(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        return {"status": "unavailable"}
    return {
        "status": "available",
        "sha256": digest_file(path),
        "size_bytes": path.stat().st_size,
    }


def _fixed_ocr_runtime_identity(name: str) -> dict[str, Any]:
    """Fingerprint the exact Apple Vision/Tesseract runtime when usable."""
    try:
        from validate_ocr_observations import current_engine_runtime

        runtime = current_engine_runtime(name)
    except Exception:
        # Availability is itself part of the fingerprint.  Avoid persisting
        # host-specific exception prose, which is not a stable identity.
        return {"status": "unavailable"}
    return {"status": "available", "runtime": runtime}


def _runtime_file_identity(path: Path) -> dict[str, Any]:
    try:
        resolved = path.resolve(strict=True)
    except OSError:
        return {"status": "unavailable"}
    if not resolved.is_file():
        return {"status": "unavailable"}
    return {
        "status": "available",
        "resolved_path": str(resolved),
        "sha256": digest_file(resolved),
        "size_bytes": resolved.stat().st_size,
    }


def _distribution_file_manifest(
    distribution: importlib.metadata.Distribution,
) -> dict[str, Any]:
    digest = hashlib.sha256()
    file_count = 0
    total_bytes = 0
    missing: list[str] = []
    files = distribution.files
    if files is None:
        return {"status": "unavailable"}
    for relative in sorted(files, key=lambda item: str(item)):
        relative_text = str(relative).replace(os.sep, "/")
        if relative_text.endswith(".pyc") or "__pycache__" in PurePosixPath(relative_text).parts:
            continue
        path = Path(distribution.locate_file(relative))
        try:
            resolved = path.resolve(strict=True)
        except OSError:
            missing.append(relative_text)
            continue
        if not resolved.is_file():
            missing.append(relative_text)
            continue
        size = resolved.stat().st_size
        digest.update(relative_text.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(size).encode("ascii"))
        digest.update(b"\0")
        digest.update(digest_file(resolved).encode("ascii"))
        digest.update(b"\n")
        file_count += 1
        total_bytes += size
    return {
        "status": "available" if not missing else "available_with_missing_files",
        "algorithm": "sha256(relative_path\\0size\\0file_sha256\\n)",
        "file_count": file_count,
        "total_bytes": total_bytes,
        "manifest_sha256": digest.hexdigest(),
        "missing_files": missing,
    }


def _reader_distribution_identities() -> dict[str, dict[str, Any]]:
    identities: dict[str, dict[str, Any]] = {}
    for name, modules in READER_DISTRIBUTIONS.items():
        try:
            distribution = importlib.metadata.distribution(name)
        except importlib.metadata.PackageNotFoundError:
            identities[name] = {"status": "unavailable"}
            continue
        module_files: dict[str, dict[str, Any]] = {}
        for module in modules:
            try:
                spec = importlib.util.find_spec(module)
            except (ImportError, AttributeError, ValueError):
                spec = None
            origin = None if spec is None else spec.origin
            module_files[module] = (
                _runtime_file_identity(Path(origin))
                if isinstance(origin, str) and origin not in {"built-in", "frozen"}
                else {"status": "unavailable"}
            )
        identities[name] = {
            "status": "available",
            "version": distribution.version,
            "distribution_files": _distribution_file_manifest(distribution),
            "module_files": module_files,
        }
    return identities


def _framework_bundle_identity(path: Path) -> dict[str, Any]:
    file_identity = _runtime_file_identity(path)
    if file_identity.get("status") != "available":
        return file_identity
    try:
        value = plistlib.loads(path.resolve(strict=True).read_bytes())
    except (OSError, plistlib.InvalidFileException):
        return {**file_identity, "bundle_metadata_status": "unavailable"}
    if not isinstance(value, dict):
        return {**file_identity, "bundle_metadata_status": "unavailable"}
    return {
        **file_identity,
        "bundle_metadata_status": "available",
        "bundle_identifier": value.get("CFBundleIdentifier"),
        "bundle_version": value.get("CFBundleVersion"),
        "short_version": value.get("CFBundleShortVersionString"),
    }


def _pdfkit_jxa_backend_identity() -> dict[str, Any]:
    """Bind PDF rendering to the local JXA executable and system frameworks."""
    osascript = _runtime_file_identity(Path("/usr/bin/osascript"))
    pdfkit = _framework_bundle_identity(
        Path("/System/Library/Frameworks/PDFKit.framework/Resources/Info.plist")
    )
    javascript_core = _framework_bundle_identity(
        Path(
            "/System/Library/Frameworks/JavaScriptCore.framework/Resources/Info.plist"
        )
    )
    return {
        "status": (
            "available"
            if all(
                item.get("status") == "available"
                for item in (osascript, pdfkit, javascript_core)
            )
            else "unavailable"
        ),
        "backend": "osascript_javascript_pdfkit",
        "os_version": platform.mac_ver()[0],
        "os_release": platform.release(),
        "architecture": platform.machine(),
        "osascript": osascript,
        "pdfkit_bundle": pdfkit,
        "javascriptcore_bundle": javascript_core,
    }


def _normalized_distribution_name(value: str) -> str:
    """Match local_paddle_ocr.verify_runtime_lock package normalization."""
    return re.sub(r"[-_.]+", "-", value).lower()


def _locked_paddle_distribution_entries(
    path: Path,
    expected_sha256: str,
) -> list[list[str]]:
    if path.is_symlink() or not path.is_file():
        raise ValueError("PaddleOCR runtime lock is not a regular file")
    if path.stat().st_size > PADDLE_RUNTIME_LOCK_MAX_BYTES:
        raise ValueError("PaddleOCR runtime lock exceeds the byte limit")
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise ValueError("PaddleOCR runtime lock hash mismatch")
    try:
        lines = raw.decode("utf-8").splitlines()
    except UnicodeDecodeError as exc:
        raise ValueError("PaddleOCR runtime lock is not UTF-8") from exc
    expected: dict[str, str] = {}
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.count("==") != 1:
            raise ValueError("PaddleOCR runtime lock contains an unsupported entry")
        name, version = line.split("==", 1)
        normalized_name = _normalized_distribution_name(name)
        if not normalized_name or not version or normalized_name in expected:
            raise ValueError("PaddleOCR runtime lock contains an invalid entry")
        expected[normalized_name] = version
        if len(expected) > PADDLE_RUNTIME_MANIFEST_MAX_DISTRIBUTIONS:
            raise ValueError("PaddleOCR runtime lock exceeds the distribution limit")
    if not expected:
        raise ValueError("PaddleOCR runtime lock is empty")
    return [[name, expected[name]] for name in sorted(expected)]


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate Paddle manifest JSON key: {key!r}")
        value[key] = item
    return value


def _paddle_distribution_manifest_identity(
    python: Path,
    worker: Path,
    runtime_lock: Path,
    model_root: Path,
    expected_lock_sha256: str,
    offline_environment: dict[str, str],
) -> dict[str, Any]:
    """Verify and fingerprint the complete configured Paddle environment."""
    expected_entries = _locked_paddle_distribution_entries(
        runtime_lock, expected_lock_sha256
    )
    environment = dict(os.environ)
    environment.update(offline_environment)
    environment["PADDLE_PDX_CACHE_HOME"] = str(model_root)
    with tempfile.TemporaryDirectory(prefix="aiec-paddle-manifest-") as temporary:
        output = Path(temporary) / "installed-distributions.json"
        completed = subprocess.run(
            [
                str(python),
                "-c",
                PADDLE_RUNTIME_MANIFEST_PROBE,
                str(worker),
                str(runtime_lock),
                str(output),
                str(PADDLE_RUNTIME_MANIFEST_MAX_DISTRIBUTIONS),
                str(PADDLE_RUNTIME_MANIFEST_MAX_BYTES),
            ],
            check=False,
            env=environment,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=PADDLE_RUNTIME_MANIFEST_PROBE_TIMEOUT_SECONDS,
        )
        if completed.returncode != 0:
            raise RuntimeError("configured Paddle package manifest probe failed")
        if output.is_symlink() or not output.is_file():
            raise RuntimeError("configured Paddle package manifest output is missing")
        size = output.stat().st_size
        if size <= 0 or size > PADDLE_RUNTIME_MANIFEST_MAX_BYTES:
            raise RuntimeError("configured Paddle package manifest output is invalid")
        try:
            payload = json.loads(
                output.read_text(encoding="utf-8"),
                object_pairs_hook=_reject_duplicate_json_keys,
            )
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError(
                "configured Paddle package manifest is invalid JSON"
            ) from exc
    if not isinstance(payload, dict) or set(payload) != {
        "runtime_lock",
        "installed_distributions",
    }:
        raise RuntimeError("configured Paddle package manifest contract is invalid")
    expected_lock = {
        "sha256": expected_lock_sha256,
        "package_count": len(expected_entries),
        "fully_matched": True,
    }
    if payload.get("runtime_lock") != expected_lock:
        raise RuntimeError("configured Paddle package manifest lock proof is invalid")
    entries = payload.get("installed_distributions")
    if (
        not isinstance(entries, list)
        or not entries
        or len(entries) > PADDLE_RUNTIME_MANIFEST_MAX_DISTRIBUTIONS
        or any(
            not isinstance(entry, list)
            or len(entry) != 2
            or any(not isinstance(value, str) or not value for value in entry)
            for entry in entries
        )
    ):
        raise RuntimeError("configured Paddle package manifest entries are invalid")
    normalized_entries = [list(entry) for entry in entries]
    names = [entry[0] for entry in normalized_entries]
    if (
        len(names) != len(set(names))
        or normalized_entries != sorted(normalized_entries)
        or any(_normalized_distribution_name(name) != name for name in names)
    ):
        raise RuntimeError(
            "configured Paddle package manifest names are not canonical and unique"
        )
    if normalized_entries != expected_entries:
        raise RuntimeError("configured Paddle package manifest does not match the lock")
    return {
        "normalization": "re.sub(r'[-_.]+', '-', name).lower()",
        "package_count": len(normalized_entries),
        "manifest_sha256": hashlib.sha256(
            canonical_json(normalized_entries).encode("utf-8")
        ).hexdigest(),
        "packages": {
            name: version for name, version in normalized_entries
        },
        "runtime_lock": expected_lock,
    }


def _paddle_runtime_identity() -> dict[str, Any]:
    """Fingerprint the configured offline Paddle runtime and model bytes."""
    configuration = {
        "AIEC_PADDLE_PYTHON": os.environ.get("AIEC_PADDLE_PYTHON"),
        "AIEC_PADDLE_MODEL_ROOT": os.environ.get("AIEC_PADDLE_MODEL_ROOT"),
    }
    try:
        import local_image_ocr
        import local_paddle_ocr

        runtime = local_image_ocr.resolve_paddle_runtime()
        _, models = local_paddle_ocr.verify_models(runtime["model_root"])
        python_target = Path(runtime["python_target"])
        runtime_lock = Path(runtime["runtime_lock"])
        worker = Path(runtime["worker"])
        installed_distributions = _paddle_distribution_manifest_identity(
            Path(runtime["python"]),
            worker,
            runtime_lock,
            Path(runtime["model_root"]),
            local_paddle_ocr.RUNTIME_LOCK_SHA256,
            local_paddle_ocr.OFFLINE_ENVIRONMENT,
        )
        pyvenv = Path(runtime["python"]).parent.parent / "pyvenv.cfg"
        identity = {
            "status": "available",
            "source": runtime["source"],
            "python_path": str(runtime["python"]),
            "python_target_sha256": digest_file(python_target),
            "runtime_lock_sha256": digest_file(runtime_lock),
            "worker_sha256": digest_file(worker),
            "installed_distributions": installed_distributions,
            "models": models,
            "network_sandbox": str(runtime["network_sandbox"]),
            "network_profile": runtime["network_profile"],
        }
        if pyvenv.is_file() and not pyvenv.is_symlink():
            identity["pyvenv_cfg_sha256"] = digest_file(pyvenv)
        return {"configuration": configuration, **identity}
    except Exception:
        return {"configuration": configuration, "status": "unavailable"}


def _ollama_json(
    host: str,
    port: int,
    path: str,
    *,
    maximum_bytes: int,
) -> dict[str, Any] | None:
    """Read one bounded Ollama JSON object from the fixed loopback endpoint."""
    if (host, port) != OLLAMA_LOOPBACK_ENDPOINT:
        return None
    connection = http.client.HTTPConnection(host, port, timeout=2.0)
    try:
        connection.request("GET", path, headers={"Connection": "close"})
        response = connection.getresponse()
        raw = response.read(maximum_bytes + 1)
        if response.status != 200 or len(raw) > maximum_bytes:
            return None
    except (OSError, http.client.HTTPException):
        return None
    finally:
        connection.close()
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _normalize_ollama_version(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    if normalized[:1] in {"v", "V"} and normalized[1:2].isdigit():
        normalized = normalized[1:]
    if len(normalized) > 128 or OLLAMA_VERSION_RE.fullmatch(normalized) is None:
        return None
    return normalized


def _ollama_executable_identities() -> dict[str, Any]:
    """Fingerprint locally discoverable Ollama executables conservatively.

    The listening process cannot be proven from the HTTP API alone, so these
    are explicitly candidates.  The server's own version comes from
    ``/api/version`` below.
    """
    candidates = [
        shutil.which("ollama"),
        "/opt/homebrew/bin/ollama",
        "/usr/local/bin/ollama",
        "/Applications/Ollama.app/Contents/Resources/ollama",
        str(Path.home() / "Applications/Ollama.app/Contents/Resources/ollama"),
    ]
    identities: list[dict[str, Any]] = []
    seen: set[str] = set()
    for candidate in candidates:
        if not candidate:
            continue
        try:
            resolved = str(Path(candidate).resolve(strict=True))
        except OSError:
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        identity = _runtime_file_identity(Path(resolved))
        if identity.get("status") == "available":
            identities.append(identity)
    return {
        "status": "available" if identities else "unavailable",
        "candidates": identities,
    }


def _ollama_endpoint_identity(host: str, port: int) -> dict[str, Any]:
    """Bind VLM reuse to the fixed loopback Ollama server API identity."""
    executables = _ollama_executable_identities()
    if (host, port) != OLLAMA_LOOPBACK_ENDPOINT:
        return {
            "status": "rejected_non_loopback",
            "server_version_status": "unavailable",
            "local_executable_candidates": executables,
        }
    payload = _ollama_json(
        host,
        port,
        "/api/version",
        maximum_bytes=64 * 1024,
    )
    if payload is None:
        return {
            "status": "unavailable",
            "server_version_status": "endpoint_unavailable",
            "local_executable_candidates": executables,
        }
    version = _normalize_ollama_version(payload.get("version"))
    if version is None:
        return {
            "status": "unavailable",
            "server_version_status": "invalid_contract",
            "local_executable_candidates": executables,
        }
    return {
        "status": "available",
        "server_version_status": "available",
        "server_version": version,
        "api_path": "/api/version",
        "local_executable_candidates": executables,
    }


def _ollama_inventory(host: str, port: int) -> dict[str, set[str]] | None:
    payload = _ollama_json(
        host,
        port,
        "/api/tags",
        maximum_bytes=4 * 1024 * 1024,
    )
    if payload is None:
        return None
    models = payload.get("models") if isinstance(payload, dict) else None
    if not isinstance(models, list) or len(models) > 10_000:
        return None
    inventory: dict[str, set[str]] = {}
    for item in models:
        if not isinstance(item, dict):
            continue
        digest = str(item.get("digest", "")).lower().removeprefix("sha256:")
        if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
            continue
        for key in ("name", "model"):
            model = item.get(key)
            if isinstance(model, str) and model:
                inventory.setdefault(model, set()).add(digest)
    return inventory


def _local_vlm_identities() -> list[dict[str, Any]]:
    try:
        import local_image_ocr
        import local_visual_observation
    except Exception:
        return [{"status": "unavailable"}]
    configurations = [
        {
            "purpose": "unlocated_text_transcript",
            "host": local_image_ocr.OLLAMA_HOST,
            "port": local_image_ocr.OLLAMA_PORT,
            "model": local_image_ocr.UNLOCATED_TRANSCRIPT_MODEL,
            "prompt_sha256": local_image_ocr.UNLOCATED_TRANSCRIPT_PROMPT_SHA256,
            "runner": "ollama_loopback_chat",
            "temperature": 0,
            "num_predict": local_image_ocr.MAX_UNLOCATED_TRANSCRIPT_TOKENS,
        },
        {
            "purpose": "visual_observation",
            "host": local_visual_observation.OLLAMA_HOST,
            "port": local_visual_observation.OLLAMA_PORT,
            "model": local_visual_observation.VISUAL_OBSERVATION_MODEL,
            "prompt_sha256": local_visual_observation.VISUAL_OBSERVATION_PROMPT_SHA256,
            "runner": local_visual_observation.VISUAL_OBSERVATION_RUNNER,
            "runner_version": local_visual_observation.VISUAL_OBSERVATION_VERSION,
            "temperature": 0,
            "num_predict": local_visual_observation.MAX_PREDICT_TOKENS,
        },
    ]
    endpoint_identities: dict[tuple[str, int], dict[str, Any]] = {}
    inventories: dict[tuple[str, int], dict[str, set[str]] | None] = {}
    identities: list[dict[str, Any]] = []
    for configuration in configurations:
        endpoint = (configuration["host"], configuration["port"])
        if endpoint not in endpoint_identities:
            endpoint_identities[endpoint] = _ollama_endpoint_identity(*endpoint)
        endpoint_identity = endpoint_identities[endpoint]
        if endpoint not in inventories:
            inventories[endpoint] = (
                _ollama_inventory(*endpoint)
                if endpoint_identity.get("status") == "available"
                else None
            )
        inventory = inventories[endpoint]
        digests = set() if inventory is None else inventory.get(configuration["model"], set())
        if inventory is None:
            model_identity = {"status": "unavailable"}
        elif len(digests) == 1:
            model_identity = {"status": "available", "digest": next(iter(digests))}
        elif not digests:
            model_identity = {"status": "not_installed"}
        else:
            model_identity = {"status": "conflicting_digests", "digests": sorted(digests)}
        identities.append({
            **configuration,
            "ollama_endpoint": endpoint_identity,
            "installed_model": model_identity,
        })
    return identities


def processing_fingerprint() -> dict[str, Any]:
    """Return the deterministic reader/tool/model identity for shard reuse."""
    scripts = Path(__file__).resolve().parent
    payload = {
        "fingerprint_version": PROCESSING_FINGERPRINT_VERSION,
        "extractor": EXTRACTOR,
        "extractor_version": EXTRACTOR_VERSION,
        "code": {
            name: _code_identity(scripts / name)
            for name in PROCESSING_CODE_FILES
        },
        "runtime": {
            "python_implementation": platform.python_implementation(),
            "python_version": platform.python_version(),
            "system": platform.system(),
            "os_release": platform.release(),
            "architecture": platform.machine(),
            "executable_sha256": (
                digest_file(Path(sys.executable).resolve())
                if Path(sys.executable).resolve().is_file()
                else None
            ),
            "reader_distributions": _reader_distribution_identities(),
        },
        "ocr": {
            "apple_vision": _fixed_ocr_runtime_identity("apple_vision"),
            "tesseract": _fixed_ocr_runtime_identity("tesseract"),
            "paddleocr": _paddle_runtime_identity(),
        },
        "pdfkit_jxa_backend": _pdfkit_jxa_backend_identity(),
        "local_vlm": _local_vlm_identities(),
    }
    return {
        "version": PROCESSING_FINGERPRINT_VERSION,
        "sha256": hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest(),
        "payload": payload,
    }


class BuildLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.handle: Any = None

    def __enter__(self) -> "BuildLock":
        self.handle = self.path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self.handle.close()
            raise RuntimeError(f"another build is using this output: {self.path.parent}") from exc
        return self

    def __exit__(self, *_: Any) -> None:
        if self.handle is not None:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            self.handle.close()


@contextmanager
def paddle_build_session(
    *,
    session_factory: Callable[[], local_image_ocr.PaddleOCRSession] | None = None,
) -> Iterator[local_image_ocr.PaddleOCRSession]:
    """Own exactly one lazy Paddle worker and cache for this build invocation."""
    factory = local_image_ocr.PaddleOCRSession if session_factory is None else session_factory
    session = factory()
    with session:
        with local_image_ocr.activate_paddle_session(session):
            yield session


class ShardWriter:
    """Write one source document to temporary JSONL files, then rename atomically."""

    def __init__(self, output: Path, document_id: str) -> None:
        self.output = output
        self.document_id = document_id
        self.shard_dir = output / "shards"
        self.shard_dir.mkdir(parents=True, exist_ok=True)
        self.temp_dir = Path(tempfile.mkdtemp(prefix=f".{document_id}.", dir=self.shard_dir))
        self.handles = {
            kind: (self.temp_dir / file_name).open("w", encoding="utf-8", newline="\n")
            for kind, file_name in RECORD_FILES.items()
        }
        self.counts = {kind: 0 for kind in RECORD_FILES}
        self.last_document: dict[str, Any] | None = None
        self.closed = False

    def emit(self, kind: str, record: dict[str, Any]) -> None:
        if kind not in self.handles:
            raise ValueError(f"unknown record group: {kind}")
        if kind == "documents":
            if self.last_document is not None:
                raise RuntimeError("a document shard may contain only one Document record")
            self.last_document = record
        self.handles[kind].write(canonical_json(record) + "\n")
        self.counts[kind] += 1

    def close(self) -> None:
        if self.closed:
            return
        for handle in self.handles.values():
            handle.flush()
            os.fsync(handle.fileno())
            handle.close()
        self.closed = True

    def discard_uncommitted_records(self) -> None:
        """Roll back every record emitted for the active source file.

        Evidence and Relation records are streamed before the final Document
        record. If extraction later fails, none of that partial graph may be
        published beside a failed Document. The failure Document is emitted
        only after this file-local rollback.
        """
        if self.closed:
            raise RuntimeError("cannot roll back a closed document shard")
        for handle in self.handles.values():
            handle.flush()
            handle.seek(0)
            handle.truncate(0)
        self.counts = {kind: 0 for kind in RECORD_FILES}
        self.last_document = None

    def commit(self) -> dict[str, Any]:
        self.close()
        shards: dict[str, dict[str, Any]] = {}
        for kind, file_name in RECORD_FILES.items():
            source = self.temp_dir / file_name
            destination = self.shard_dir / f"{self.document_id}.{kind}.jsonl"
            os.replace(source, destination)
            shards[kind] = {
                "relative_path": nfc_path(destination.relative_to(self.output)),
                "sha256": digest_file(destination),
                "size_bytes": destination.stat().st_size,
                "record_count": self.counts[kind],
            }
        self.temp_dir.rmdir()
        return shards

    def abort(self) -> None:
        for handle in self.handles.values():
            if not handle.closed:
                handle.close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)
        self.closed = True


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path, help="source root used for relative paths")
    parser.add_argument("--out", required=True, type=Path, help="output directory")
    inputs = parser.add_mutually_exclusive_group()
    inputs.add_argument("--input", type=Path, nargs="*", help="optional explicit files; otherwise discover recursively")
    inputs.add_argument(
        "--input-manifest", type=Path,
        help="JSON manifest of relative source paths; avoids oversized command lines for curated builds",
    )
    parser.add_argument("--run-at", help="ISO-8601 timestamp; a resumed build reuses the stored value")
    parser.add_argument("--resume", action="store_true", help="resume an existing build-state.json")
    parser.add_argument(
        "--force-input", type=Path, nargs="*", default=[],
        help="with --resume, reprocess these original input files even when their shards are valid",
    )
    parser.add_argument("--fail-fast", action="store_true", help="stop after recording the first failed document")
    parser.add_argument("--max-files", type=int, help="process at most this many pending files, then stop resumably")
    return parser.parse_args()


def load_input_manifest(root: Path, manifest_path: Path) -> list[Path]:
    """Load a source-bound list without accepting absolute or parent paths."""
    manifest = json.loads(manifest_path.resolve(strict=True).read_text(encoding="utf-8"))
    if not isinstance(manifest, dict) or manifest.get("schema_version") != "0.1":
        raise ValueError("input manifest schema_version must be 0.1")
    declared_root = manifest.get("source_root")
    if not isinstance(declared_root, str) or Path(declared_root).resolve() != root.resolve():
        raise ValueError("input manifest source_root mismatch")
    values = manifest.get("paths")
    if not isinstance(values, list) or not values:
        raise ValueError("input manifest paths must be a non-empty list")
    if len(values) != len(set(values)):
        raise ValueError("input manifest paths must be unique")
    paths: list[Path] = []
    for value in values:
        if not isinstance(value, str) or not value:
            raise ValueError("input manifest paths must contain non-empty strings")
        relative = PurePosixPath(value)
        if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
            raise ValueError(f"unsafe input manifest path: {value!r}")
        paths.append(root.joinpath(*relative.parts))
    return paths


def validate_inputs(root: Path, paths: list[Path]) -> list[Path]:
    if not paths:
        raise ValueError("no supported input files found")
    unique = sorted({path.resolve() for path in paths}, key=lambda path: normalized_relative(root, path))
    for path in unique:
        if not path.is_file():
            raise ValueError(f"input is not a file: {path}")
        if path.suffix.lower() not in SUPPORTED_SUFFIXES:
            raise ValueError(f"unsupported explicit input: {path}")
        normalized_relative(root, path)
    return unique


def new_state(
    root: Path,
    inputs: list[Path],
    run_at: str,
    fingerprint: dict[str, Any],
) -> dict[str, Any]:
    return {
        "state_version": STATE_VERSION,
        "build_status": "in_progress",
        "source_root": nfc_path(root),
        "extractor": EXTRACTOR,
        "extractor_version": EXTRACTOR_VERSION,
        "run_at": run_at,
        "input_paths": [normalized_relative(root, path) for path in inputs],
        "processing_fingerprint": fingerprint,
        "entries": {},
    }


def load_state(output: Path, root: Path, explicit_inputs: list[Path] | None) -> tuple[dict[str, Any], list[Path]]:
    state_path = output / STATE_FILE
    if not state_path.is_file():
        raise ValueError(f"cannot resume without {state_path}")
    state = json.loads(state_path.read_text(encoding="utf-8"))
    if not isinstance(state, dict):
        raise ValueError("resume state must be a JSON object")
    expected = {
        "state_version": STATE_VERSION,
        "source_root": nfc_path(root),
        "extractor": EXTRACTOR,
        "extractor_version": EXTRACTOR_VERSION,
    }
    for key, value in expected.items():
        if state.get(key) != value:
            raise ValueError(f"resume mismatch for {key}: {state.get(key)!r} != {value!r}")
    stored_paths = state.get("input_paths")
    if (
        not isinstance(stored_paths, list)
        or not stored_paths
        or any(not isinstance(value, str) or not value for value in stored_paths)
    ):
        raise ValueError("resume input_paths must be a non-empty list of strings")
    canonical_relative_paths: list[PurePosixPath] = []
    for value in stored_paths:
        relative = PurePosixPath(value)
        if (
            relative.is_absolute()
            or any(part in {"", ".", ".."} for part in relative.parts)
            or relative.as_posix() != value
            or nfc_path(Path(value)) != value
        ):
            raise ValueError(
                f"resume input_paths contains a non-canonical relative path: {value!r}"
            )
        canonical_relative_paths.append(relative)
    if len(stored_paths) != len(set(stored_paths)):
        raise ValueError("resume input_paths must be unique")
    inputs = [root.joinpath(*relative.parts) for relative in canonical_relative_paths]
    validated_inputs = validate_inputs(root, inputs)
    validated_paths = [
        normalized_relative(root, path) for path in validated_inputs
    ]
    if validated_paths != stored_paths:
        raise ValueError(
            "resume input_paths must match current inputs in canonical sorted order"
        )
    if explicit_inputs is not None:
        requested = [normalized_relative(root, path) for path in validate_inputs(root, explicit_inputs)]
        if requested != stored_paths:
            raise ValueError("--input must match the original build exactly when using --resume")
    return state, validated_inputs


def shard_is_valid(
    output: Path,
    shard: object,
    expected_relative_path: str,
) -> bool:
    if not isinstance(shard, dict) or shard.get("relative_path") != expected_relative_path:
        return False
    relative = PurePosixPath(expected_relative_path)
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        return False
    path = output.joinpath(*relative.parts)
    try:
        resolved = path.resolve(strict=True)
        resolved.relative_to(output.resolve(strict=True))
        metadata = path.lstat()
    except (OSError, ValueError):
        return False
    return (
        not path.is_symlink()
        and path.is_file()
        and metadata.st_size == shard.get("size_bytes")
        and digest_file(path) == shard.get("sha256")
    )


def terminal_entry_is_valid(
    output: Path,
    entry: dict[str, Any] | None,
    source_sha256: str,
    processing_fingerprint_sha256: str,
) -> bool:
    if not entry or entry.get("source_sha256") != source_sha256:
        return False
    status = entry.get("status")
    if status not in TERMINAL_STATUSES:
        return False
    if entry.get("processing_fingerprint_sha256") != processing_fingerprint_sha256:
        return False
    shards = entry.get("shards")
    if not isinstance(shards, dict) or set(shards) != set(RECORD_FILES):
        return False
    document_id = entry.get("document_id")
    if not isinstance(document_id, str) or not document_id:
        return False
    return all(
        shard_is_valid(
            output,
            shards[kind],
            f"shards/{document_id}.{kind}.jsonl",
        )
        for kind in RECORD_FILES
    )


def can_skip(
    output: Path,
    entry: dict[str, Any] | None,
    source_sha256: str,
    processing_fingerprint_sha256: str,
) -> bool:
    return (
        bool(entry)
        and entry.get("status") in SKIPPABLE_STATUSES
        and terminal_entry_is_valid(
            output, entry, source_sha256, processing_fingerprint_sha256
        )
    )


def process_file(
    output: Path,
    root: Path,
    path: Path,
    run_at: str,
    source_sha256: str,
    password_candidates: tuple[str, ...],
    processing_fingerprint_sha256: str | None = None,
) -> tuple[dict[str, Any], Exception | None]:
    relative_path = normalized_relative(root, path)
    document_id = stable_id("doc", {"relative_path": relative_path, "source_sha256": source_sha256})
    writer = ShardWriter(output, document_id)
    extractor = Probe(
        root,
        run_at,
        None,
        diagnostic=False,
        extractor=EXTRACTOR,
        extractor_version=EXTRACTOR_VERSION,
        record_sink=writer.emit,
        retain_records=False,
        password_candidates=password_candidates,
        visual_observation_mode="deferred_per_document",
    )
    extraction_error: Exception | None = None
    try:
        extractor.extract(path)
    except Exception as error:
        extraction_error = error
        try:
            writer.discard_uncommitted_records()
            extractor.record_failure(path, error)
            extractor.finalize_document()
        except Exception:
            writer.abort()
            raise
    document = writer.last_document
    if document is None:
        writer.abort()
        raise RuntimeError(f"extractor emitted no Document record for {relative_path}")
    if document.get("document_id") != document_id:
        writer.abort()
        raise RuntimeError(f"source identity changed before extraction for {relative_path}")
    if document.get("source", {}).get("sha256") != source_sha256 or digest_file(path) != source_sha256:
        writer.abort()
        raise RuntimeError(f"source changed during extraction for {relative_path}")
    try:
        shards = writer.commit()
    except Exception:
        writer.abort()
        raise

    entry = {
        "relative_path": relative_path,
        "source_sha256": source_sha256,
        "document_id": document_id,
        "status": document["extraction"]["status"],
        "shards": shards,
    }
    if processing_fingerprint_sha256 is not None:
        entry["processing_fingerprint_sha256"] = processing_fingerprint_sha256
    return entry, extraction_error


def consolidate(
    output: Path,
    state: dict[str, Any],
) -> tuple[dict[str, int], dict[str, dict[str, Any]]]:
    totals = {kind: 0 for kind in RECORD_FILES}
    aggregates: dict[str, dict[str, Any]] = {}
    entries = state["entries"]
    for kind, file_name in RECORD_FILES.items():
        temporary = output / f".{file_name}.tmp"
        aggregate_digest = hashlib.sha256()
        aggregate_size = 0
        manifest: list[dict[str, Any]] = []
        with temporary.open("wb") as destination:
            for relative_path in state["input_paths"]:
                entry = entries[relative_path]
                shard = entry["shards"][kind]
                with (output / shard["relative_path"]).open("rb") as source:
                    shard_digest = hashlib.sha256()
                    shard_size = 0
                    shard_count = 0
                    for line in source:
                        destination.write(line)
                        shard_digest.update(line)
                        aggregate_digest.update(line)
                        shard_size += len(line)
                        aggregate_size += len(line)
                        if line.strip():
                            shard_count += 1
                measured = {
                    "sha256": shard_digest.hexdigest(),
                    "size_bytes": shard_size,
                    "record_count": shard_count,
                }
                if any(shard.get(key) != value for key, value in measured.items()):
                    raise RuntimeError(
                        f"shard changed before consolidation: {shard['relative_path']}"
                    )
                totals[kind] += shard_count
                manifest.append({
                    "relative_path": shard["relative_path"],
                    **measured,
                })
            destination.flush()
            os.fsync(destination.fileno())
        os.replace(temporary, output / file_name)
        aggregates[kind] = {
            "relative_path": file_name,
            "sha256": aggregate_digest.hexdigest(),
            "size_bytes": aggregate_size,
            "record_count": totals[kind],
            "ordered_shard_manifest_sha256": ordered_shard_manifest_sha256(manifest),
        }
    return totals, aggregates


def main() -> None:
    args = parse_args()
    if args.max_files is not None and args.max_files < 1:
        raise SystemExit("--max-files must be at least 1")
    if args.force_input and not args.resume:
        raise SystemExit("--force-input requires --resume")
    root = args.root.resolve()
    output = args.out.resolve()
    if not root.is_dir():
        raise SystemExit(f"--root is not a directory: {root}")
    try:
        output.relative_to(root)
    except ValueError:
        pass
    else:
        raise SystemExit("--out must be outside --root to prevent recursive self-ingestion")

    try:
        explicit_inputs = (
            load_input_manifest(root, args.input_manifest)
            if args.input_manifest is not None else args.input
        )
    except (OSError, ValueError, json.JSONDecodeError) as error:
        raise SystemExit(str(error)) from error

    if args.resume:
        if not output.is_dir():
            raise SystemExit(f"resume output is not a directory: {output}")
    else:
        if output.exists() and (not output.is_dir() or any(output.iterdir())):
            raise SystemExit(f"refusing to overwrite non-empty output directory: {output}")
        output.mkdir(parents=True, exist_ok=True)

    with BuildLock(output / LOCK_FILE), paddle_build_session():
        current_fingerprint = processing_fingerprint()
        fingerprint_sha256 = current_fingerprint["sha256"]
        if args.resume:
            try:
                state, inputs = load_state(output, root, explicit_inputs)
            except ValueError as error:
                raise SystemExit(str(error)) from error
            run_at = state["run_at"]
        else:
            try:
                inputs = validate_inputs(root, explicit_inputs if explicit_inputs is not None else discover(root))
            except ValueError as error:
                raise SystemExit(str(error)) from error
            run_at = args.run_at or datetime.now(timezone.utc).isoformat()
            state = new_state(root, inputs, run_at, current_fingerprint)
            atomic_json(output / STATE_FILE, state)

        processed_now = 0
        skipped_now = 0
        processed_paths: set[str] = set()
        password_candidates = discover_password_candidates(root)
        forced_paths = {normalized_relative(root, path) for path in args.force_input}
        unknown_forced = forced_paths - set(state["input_paths"])
        if unknown_forced:
            raise SystemExit(f"--force-input was not part of the original build: {sorted(unknown_forced)}")
        # A terminal state must never remain visible while forced or stale
        # entries are being replaced.  Old aggregate files may remain on disk,
        # but downstream consumers fail closed on this in-progress state.
        state["build_status"] = "in_progress"
        state["processing_fingerprint"] = current_fingerprint
        state.pop("totals", None)
        state.pop("aggregates", None)
        atomic_json(output / STATE_FILE, state)
        for path in inputs:
            relative_path = normalized_relative(root, path)
            source_sha256 = digest_file(path)
            existing = state["entries"].get(relative_path)
            if relative_path not in forced_paths and can_skip(
                output, existing, source_sha256, fingerprint_sha256
            ):
                skipped_now += 1
                continue
            if args.max_files is not None and processed_now >= args.max_files:
                break
            entry, extraction_error = process_file(
                output,
                root,
                path,
                run_at,
                source_sha256,
                password_candidates,
                fingerprint_sha256,
            )
            state["entries"][relative_path] = entry
            processed_paths.add(relative_path)
            processed_now += 1
            atomic_json(output / STATE_FILE, state)
            if extraction_error is not None and args.fail_fast:
                raise RuntimeError(f"failed to extract {relative_path}: {extraction_error}") from extraction_error

        all_reached_terminal = True
        for path in inputs:
            relative_path = normalized_relative(root, path)
            source_sha256 = digest_file(path)
            entry = state["entries"].get(relative_path)
            forced_but_not_processed = (
                relative_path in forced_paths and relative_path not in processed_paths
            )
            if forced_but_not_processed:
                all_reached_terminal = False
                break
            if relative_path in processed_paths:
                valid = terminal_entry_is_valid(
                    output, entry, source_sha256, fingerprint_sha256
                )
            else:
                valid = can_skip(
                    output, entry, source_sha256, fingerprint_sha256
                )
            if not valid:
                all_reached_terminal = False
                break
        if all_reached_terminal:
            totals, aggregates = consolidate(output, state)
            total_failures = sum(entry.get("status") == "failed" for entry in state["entries"].values())
            state["build_status"] = "complete_with_failures" if total_failures else "complete"
            state["totals"] = totals
            state["aggregates"] = aggregates
            atomic_json(output / STATE_FILE, state)
        else:
            totals = {kind: sum(
                entry.get("shards", {}).get(kind, {}).get("record_count", 0)
                for entry in state["entries"].values()
            ) for kind in RECORD_FILES}
            state["build_status"] = "in_progress"
            state.pop("totals", None)
            state.pop("aggregates", None)
            atomic_json(output / STATE_FILE, state)

        print(canonical_json({
            "build_status": state["build_status"],
            "documents": totals["documents"],
            "evidence": totals["evidence"],
            "relations": totals["relations"],
            "failed_documents": sum(entry.get("status") == "failed" for entry in state["entries"].values()),
            "input_files": len(inputs),
            "processed_now": processed_now,
            "skipped_now": skipped_now,
            "output": str(output),
            "extractor": EXTRACTOR,
            "extractor_version": EXTRACTOR_VERSION,
        }))


if __name__ == "__main__":
    main()
