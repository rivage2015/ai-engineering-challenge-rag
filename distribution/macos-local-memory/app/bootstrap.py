#!/usr/bin/env python3
"""First-run and index-building orchestration for Local Memory Search."""

from __future__ import annotations

import argparse
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


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


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


def _write_log(log, message: str) -> None:
    if log is not None:
        log.write(message.rstrip() + "\n")
        log.flush()


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
        "port": 8765,
    })
    if (
        config.get("answer_model") == "qwen3.5:9b"
        and config.get("audit_model") == "gemma4:12b"
        and not config.get("model_profile")
    ):
        config["answer_model"] = "gemma4:12b"
        config["model_profile"] = "gemma4-validated-v1"
    config["source_root"] = str(source)
    config["workspace"] = str(SUPPORT / "data")
    # Selecting a source invalidates the active generation immediately.  A
    # complete reader/security/index generation will publish fresh pointers.
    config["index_path"] = ""
    for key in ("active_generation", "path_graph_path", "semantic_path", "security_path"):
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


def _ready_state(reader_state: dict, *, recovered: bool = False) -> dict:
    recovered_fields = {
        "recovered_after_interruption": True,
        "recovered_at": now_iso(),
    } if recovered else {}
    if reader_state.get("status") == "complete_with_limits":
        return {
            "phase": "ready_with_limits",
            "message": "索引は作成しましたが、未対応または部分読取りのファイルがあります。",
            "error": "",
            "reader_limitations": reader_state.get("limitations", {}),
            **recovered_fields,
        }
    return {
        "phase": "ready",
        "message": "索引の作成が完了しました。",
        "error": "",
        **recovered_fields,
    }


def recover_interrupted_build() -> dict:
    """Recover a published generation or retire a dead unpublished build.

    This is called once when the local server starts.  A live owner PID is
    never interrupted.  Deletion is restricted to an exact, app-generated
    generation directory whose marker still says ``building``.
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
            atomic_json(marker_path, {
                **load_json(marker_path),
                "status": "published",
                "published_at": now_iso(),
            })
            atomic_json(STATE, _ready_state(reader_state, recovered=True))
            return {"status": "recovered_published", "generation": generation_name, "removed": removed}
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


def build_index() -> None:
    config = load_json(CONFIG)
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
        published_config.update({
            "active_generation": generation.name,
            "path_graph_path": str(paths),
            "semantic_path": str(semantic),
            "security_path": str(security),
            "index_path": str(index),
        })
        atomic_json(CONFIG, published_config)
        generation_published = True
        marker_warning = ""
        try:
            atomic_json(marker_path, {
                **marker,
                "status": "published",
                "published_at": now_iso(),
                "semantic_path": str(semantic),
                "security_path": str(security),
                "index_path": str(index),
            })
        except OSError as exc:
            marker_warning = f"{type(exc).__name__}: {exc}"
        state = _ready_state(reader_state)
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
