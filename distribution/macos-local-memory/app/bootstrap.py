#!/usr/bin/env python3
"""First-run and index-building orchestration for Local Memory Search."""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path


APP_NAME = "LocalMemorySearch"
SUPPORT = Path.home() / "Library" / "Application Support" / APP_NAME
CONFIG = SUPPORT / "config.json"
STATE = SUPPORT / "state.json"
ENGINE = Path(__file__).resolve().parent / "engine"
OLLAMA = "http://127.0.0.1:11434"


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


def start_ollama() -> None:
    if ollama_online():
        return
    apps = [Path("/Applications/Ollama.app"), Path.home() / "Applications" / "Ollama.app"]
    app = next((item for item in apps if item.exists()), None)
    if app:
        subprocess.run(["open", str(app)], check=False)
        for _ in range(20):
            import time
            if ollama_online():
                return
            time.sleep(1)
    raise RuntimeError("ollama_not_running")


def ensure_models(models: list[str], log) -> None:
    binary = ollama_binary()
    if not binary:
        raise RuntimeError("ollama_not_installed")
    start_ollama()
    installed = model_names()
    for model in models:
        if model not in installed and f"{model}:latest" not in installed:
            run([binary, "pull", model], log)


def configure_source(source: Path) -> dict:
    source = source.expanduser().resolve(strict=True)
    if not source.is_dir() or source.is_symlink():
        raise SystemExit("検索対象は実フォルダを指定してください。")
    config = load_json(CONFIG, {
        "embedding_model": "embeddinggemma:latest",
        "answer_model": "qwen3.5:9b",
        "audit_model": "gemma4:12b",
        "port": 8765,
    })
    config["source_root"] = str(source)
    config["workspace"] = str(SUPPORT / "data")
    config["index_path"] = str(SUPPORT / "data" / "safe-answer-index.sqlite3")
    atomic_json(CONFIG, config)
    return config


def build_index() -> None:
    config = load_json(CONFIG)
    source = Path(config["source_root"]).resolve(strict=True)
    workspace = Path(config.get("workspace", SUPPORT / "data"))
    paths = workspace / "01-path"
    semantic = workspace / "02-semantic"
    security = workspace / "03-security"
    index = Path(config.get("index_path", workspace / "safe-answer-index.sqlite3"))
    log_path = SUPPORT / "logs" / "build.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    state = {"phase": "building", "message": "索引を作成中です。", "error": ""}
    atomic_json(STATE, state)
    try:
        with log_path.open("w", encoding="utf-8", buffering=1) as log:
            ensure_models([config["embedding_model"], config["answer_model"], config["audit_model"]], log)
            for path in (paths, semantic, security):
                path.mkdir(parents=True, exist_ok=True)
            run([sys.executable, str(ENGINE / "build_path_graph.py"), str(source), "--output-dir", str(paths)], log)
            run([sys.executable, str(ENGINE / "validate_path_graph.py"), str(paths / "path-evidence-graph.json"), str(paths / "path-source-inventory.jsonl")], log)
            run([
                sys.executable, str(ENGINE / "build_semantic_graph.py"),
                "--inventory", str(paths / "path-source-inventory.jsonl"),
                "--source-root", str(source), "--output-dir", str(semantic),
            ], log)
            run([
                sys.executable, str(ENGINE / "validate_semantic_graph.py"),
                "--output-dir", str(semantic),
            ], log)
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
            run([
                sys.executable, str(ENGINE / "build_local_semantic_index.py"),
                "--evidence", str(security / "safe-answer-evidence.jsonl"),
                "--documents", str(semantic / "semantic-documents.jsonl"),
                "--security-state", str(security / "content-security-state.json"),
                "--index-purpose", "safe_answer", "--model", config["embedding_model"],
                "--output", str(index),
            ], log)
        state = {"phase": "ready", "message": "索引の作成が完了しました。", "error": ""}
    except Exception as exc:
        state = {"phase": "error", "message": "索引の作成に失敗しました。", "error": f"{type(exc).__name__}: {exc}"}
        raise
    finally:
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
