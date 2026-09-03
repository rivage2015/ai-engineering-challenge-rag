#!/usr/bin/env python3
"""First-run and index-building orchestration for Local Memory Search."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime
from pathlib import Path


APP_NAME = "LocalMemorySearch"
SUPPORT = Path.home() / "Library" / "Application Support" / APP_NAME
CONFIG = SUPPORT / "config.json"
STATE = SUPPORT / "state.json"
ENGINE = Path(__file__).resolve().parent / "engine"
OLLAMA = "http://127.0.0.1:11434"
IMAGE_FALLBACK_MODEL = "gemma4:12b"
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp"}
GENERATION_NAME = re.compile(r"generation-[0-9a-f]{32}")
GENERATION_MARKER = "build-generation.json"
CROSS_DOCUMENT_SHADOW_FLAG = "cross_document_semantic_graph_shadow_enabled"
CROSS_DOCUMENT_SHADOW_DIR = "04-semantic-graph-shadow"
CROSS_DOCUMENT_SHADOW_RUN_STATE = "shadow-run-state.json"
CROSS_DOCUMENT_SHADOW_TIMEOUT_SECONDS = 300.0
CROSS_DOCUMENT_SHADOW_TOOLS = (
    "build_cross_document_semantic_graph.py",
    "query_cross_document_semantic_graph.py",
    "validate_cross_document_semantic_graph.py",
)


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


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
        with urllib.request.urlopen(f"{OLLAMA}/api/tags", timeout=timeout) as response:
            return response.status == 200
    except (OSError, urllib.error.URLError):
        return False


def model_names() -> set[str]:
    try:
        with urllib.request.urlopen(f"{OLLAMA}/api/tags", timeout=5) as response:
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
    config = load_json(CONFIG)
    source = Path(config["source_root"]) if config.get("source_root") else None
    index = Path(config["index_path"]) if config.get("index_path") else None
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
        "cross_document_semantic_graph_shadow_enabled": (
            config.get(CROSS_DOCUMENT_SHADOW_FLAG, True) is True
        ),
        "ready": bool(python_ok and ollama_path and source and source.is_dir() and index and index.is_file()),
        "warnings": [
            *( ["RAMは24GB以上を推奨します。"] if memory and memory < 23 else [] ),
            *( ["空き容量は40GB以上を推奨します。"] if free < 40 else [] ),
            *( ["Intel Macでは12Bモデルの応答が大幅に遅くなります。"] if platform.machine() == "x86_64" else [] ),
        ],
    }


def run(command: list[str], log) -> None:
    log.write("$ " + " ".join(command) + "\n")
    log.flush()
    process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT, text=True)
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
    environment = dict(os.environ)
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
            run([binary, "pull", model], log)
            installed = model_names()
            if not _model_installed(model, installed):
                raise RuntimeError(f"model_pull_not_visible:{model}")
            pulled.append(model)
    return pulled


def configure_source(source: Path) -> dict:
    source = source.expanduser().resolve(strict=True)
    if not source.is_dir() or source.is_symlink():
        raise SystemExit("検索対象は実フォルダを指定してください。")
    config = load_json(CONFIG, {
        "embedding_model": "embeddinggemma:latest",
        "answer_model": "gemma4:12b",
        "audit_model": "gemma4:12b",
        "model_profile": "gemma4-validated-v1",
        "sequential_model_loading": True,
        CROSS_DOCUMENT_SHADOW_FLAG: True,
        "port": 8765,
    })
    if (
        config.get("answer_model") == "qwen3.5:9b"
        and config.get("audit_model") == "gemma4:12b"
        and not config.get("model_profile")
    ):
        config["answer_model"] = "gemma4:12b"
        config["model_profile"] = "gemma4-validated-v1"
    config.setdefault(CROSS_DOCUMENT_SHADOW_FLAG, True)
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
    ):
        config.pop(key, None)
    atomic_json(CONFIG, config)
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


def _published_generation_ready(config: dict, generation: Path) -> bool:
    required = {
        "path_graph_path": "directory",
        "semantic_path": "directory",
        "security_path": "directory",
        "index_path": "file",
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
        if kind == "file" and not candidate.is_file():
            return False
    reader_state = Path(config["semantic_path"]) / "adaptive-reader-state.json"
    try:
        status = load_json(reader_state).get("status")
    except (OSError, ValueError, TypeError):
        return False
    return status in {"complete", "complete_with_limits"}


def _ready_state(
    reader_state: dict,
    *,
    recovered: bool = False,
    semantic_graph_shadow: dict | None = None,
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
    if reader_state.get("status") == "complete_with_limits":
        return {
            "phase": "ready_with_limits",
            "message": "索引は作成しましたが、未対応または部分読取りのファイルがあります。",
            "error": "",
            "reader_limitations": reader_state.get("limitations", {}),
            **recovered_fields,
            **shadow_fields,
        }
    return {
        "phase": "ready",
        "message": "索引の作成が完了しました。",
        "error": "",
        **recovered_fields,
        **shadow_fields,
    }


def recover_interrupted_build() -> dict:
    """Recover a published generation or retire a dead unpublished build.

    This is called once when the local server starts.  A live owner PID is
    never interrupted.  Deletion is restricted to either an exact unpublished
    generation whose marker still says ``building`` or the fixed unfinished
    shadow candidate inside an otherwise published generation.
    """
    config = load_json(CONFIG)
    current = load_json(
        STATE,
        {"phase": "not_started", "message": "まだ索引は作成されていません。", "error": ""},
    )
    workspace = Path(config.get("workspace", SUPPORT / "data"))
    active_generation = config.get("active_generation")
    removed: list[str] = []

    def owner_is_live(pid: object) -> bool:
        # Recovery runs before this server starts a build thread.  Equality can
        # only be a PID reused after reboot/process death, not a live owner.
        return pid != os.getpid() and _pid_is_alive(pid)

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
            or owner_is_live(marker.get("owner_pid"))
        ):
            return False
        shutil.rmtree(generation)
        removed.append(name)
        return True

    if (
        current.get("phase") in {"ready", "ready_with_limits"}
        and isinstance(active_generation, str)
    ):
        generation = _generation_path(workspace, active_generation)
        marker_path = (
            generation / GENERATION_MARKER
            if generation is not None
            else None
        )
        try:
            marker = load_json(marker_path) if marker_path is not None else {}
        except (OSError, ValueError, TypeError):
            marker = {}
        current_shadow = current.get("cross_document_semantic_graph_shadow")
        marker_shadow = marker.get("cross_document_semantic_graph_shadow")
        pending = (
            isinstance(current_shadow, dict)
            and current_shadow.get("status") == "pending"
        ) or (
            isinstance(marker_shadow, dict)
            and marker_shadow.get("status") == "pending"
        )
        if (
            pending
            and generation is not None
            and marker.get("status") == "published"
            and marker.get("generation") == active_generation
            and _published_generation_ready(config, generation)
        ):
            if owner_is_live(marker.get("owner_pid")):
                return {
                    "status": "active_shadow",
                    "owner_pid": marker.get("owner_pid"),
                    "removed": removed,
                }
            recovered_shadow = _recover_published_shadow_observer(
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
            atomic_json(marker_path, {
                **marker,
                "cross_document_semantic_graph_shadow": recovered_shadow,
            })
            recovered_state = {
                **current,
                "cross_document_semantic_graph_shadow": recovered_shadow,
                "shadow_recovered_after_interruption": True,
                "shadow_recovered_at": now_iso(),
            }
            atomic_json(STATE, recovered_state)
            return {
                "status": "recovered_published_shadow",
                "generation": active_generation,
                "shadow_status": recovered_shadow.get("status"),
                "removed": removed,
            }

    if current.get("phase") == "building":
        owner_pid = current.get("owner_pid")
        if owner_is_live(owner_pid):
            return {"status": "active", "owner_pid": owner_pid, "removed": removed}
        generation_name = current.get("generation")
        generation = (
            _generation_path(workspace, generation_name)
            if isinstance(generation_name, str) else None
        )
        if (
            generation is not None
            and generation_name == active_generation
            and _published_generation_ready(config, generation)
        ):
            reader_state = load_json(Path(config["semantic_path"]) / "adaptive-reader-state.json")
            marker_path = generation / GENERATION_MARKER
            recovered_marker = {
                **load_json(marker_path),
                "status": "published",
                "published_at": now_iso(),
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
            atomic_json(marker_path, recovered_marker)
            atomic_json(
                STATE,
                _ready_state(
                    reader_state,
                    recovered=True,
                    semantic_graph_shadow=recovered_shadow,
                ),
            )
            return {
                "status": "recovered_published",
                "generation": generation_name,
                "shadow_status": recovered_shadow.get("status"),
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
        isinstance(value, str) and Path(value).suffix.casefold() in IMAGE_SUFFIXES
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


def build_index() -> None:
    config = load_json(CONFIG)
    # Existing installations predate the shadow flag.  Missing means enabled;
    # an explicit false remains the rollback switch.
    config.setdefault(CROSS_DOCUMENT_SHADOW_FLAG, True)
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
        "build_id": build_id,
        "generation": generation.name,
        "owner_pid": os.getpid(),
        "started_at": now_iso(),
        "source_root": str(source),
        "cross_document_semantic_graph_shadow_enabled": (
            config.get(CROSS_DOCUMENT_SHADOW_FLAG, True) is True
        ),
    }
    atomic_json(marker_path, marker)
    state = {
        "phase": "building",
        "message": "索引を作成中です。",
        "error": "",
        "build_id": build_id,
        "generation": generation.name,
        "owner_pid": os.getpid(),
        "started_at": marker["started_at"],
    }
    atomic_json(STATE, state)
    generation_published = False
    reader_state: dict = {}
    shadow_state = _shadow_run_base(
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
            published_config = dict(config)
            published_config.pop("semantic_graph_shadow_path", None)
            published_config.update({
                "active_generation": generation.name,
                "path_graph_path": str(paths),
                "semantic_path": str(semantic),
                "security_path": str(security),
                "index_path": str(index),
            })
            atomic_json(CONFIG, published_config)
            generation_published = True
            published_at = now_iso()
            shadow_state = _shadow_run_base(
                generation,
                build_id,
                status=(
                    "pending"
                    if config.get(CROSS_DOCUMENT_SHADOW_FLAG, True) is True
                    else "disabled"
                ),
                reason_code=(
                    "scheduled_after_production_publish"
                    if config.get(CROSS_DOCUMENT_SHADOW_FLAG, True) is True
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
                "cross_document_semantic_graph_shadow": shadow_state,
            }
            marker_warning = ""
            try:
                atomic_json(marker_path, published_marker)
            except OSError as exc:
                marker_warning = f"{type(exc).__name__}: {exc}"
            state = _ready_state(
                reader_state,
                semantic_graph_shadow=shadow_state,
            )
            if marker_warning:
                state["generation_marker_warning"] = marker_warning
            # Production becomes queryable before the observer starts.  The
            # shadow can consume time or fail without delaying index publication.
            atomic_json(STATE, state)

            try:
                shadow_state = run_cross_document_semantic_graph_shadow(
                    config,
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

            marker_warning = ""
            try:
                atomic_json(marker_path, {
                    **published_marker,
                    "cross_document_semantic_graph_shadow": shadow_state,
                })
            except OSError as exc:
                marker_warning = f"{type(exc).__name__}: {exc}"
            state = _ready_state(
                reader_state,
                semantic_graph_shadow=shadow_state,
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
