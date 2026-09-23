#!/usr/bin/env python3
"""Prepare a private snapshot index, then exercise the real app answer backend.

Without --query, prepare exactly once into a new/empty --output directory.
With --query, reuse that prepared index without extracting or rebuilding it.
With --resume-preparation, resume only a first preparation that stopped at the
security gate before producing any security/index output; revalidate its inputs
without rerunning extraction, projection, or an already started index build.
This is NOT a published app generation, GUI acceptance test, or a replacement
answerer. Every extraction/security/index validator and the real app's answer
and final-audit functions remain in use. No installed application is changed.

Example (use a Python environment containing the existing reader dependencies):
  python scripts/test_local_reading_snapshot.py --snapshot SNAPSHOT.json \
      --source-root SOURCE --output .tmp/local-memory-snapshot-app-20260923/run-01
  python scripts/test_local_reading_snapshot.py --snapshot SNAPSHOT.json \
      --source-root SOURCE --output .tmp/local-memory-snapshot-app-20260923/run-01 \
      --query 'The question to test'

Private source text, answers and exception details are written only below
--output. stdout contains stage names, durations and statuses, never answers.
The script performs real local model calls when executed; there is no mock mode.
"""
from __future__ import annotations

import argparse
import contextlib
from datetime import datetime, timezone
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import time
import traceback
from typing import Any
import uuid


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "distribution" / "macos-local-memory"
APP = PACKAGE / "app"
ENGINE = PACKAGE / "engine"
PRIVATE_ROOT = ROOT / ".tmp" / "local-memory-snapshot-app-20260923"
DEFAULT_CONFIG_TEMPLATE = (
    Path.home() / "Library" / "Application Support" / "LocalMemorySearch" / "config.json"
)
HARNESS_ID = "local-reading-snapshot-app-backend-v1"
STATE_NAME = "snapshot-test-state.json"
MAX_QUERIES = 4
CONTEXT_COMPARISON_ID = "CR-SNAPSHOT-CONTEXT-8192"
EXPECTED_MODELS = {
    "embedding_model": "embeddinggemma:latest",
    "answer_model": "gemma4:12b",
    "audit_model": "gemma4:12b",
}
GRAPH_FLAGS = (
    "cross_document_semantic_graph_shadow_enabled",
    "cross_document_semantic_graph_storage_enabled",
    "cross_document_semantic_graph_query_candidate_enabled",
    "cross_document_semantic_graph_independent_edge_audit_enabled",
    "cross_document_semantic_graph_answer_promotion_enabled",
)
SCOPE = {
    "app_backend_only": True,
    "installed_app_changed": False,
    "gui_acceptance_test": False,
    "published_app_generation": False,
    "source_reextraction": False,
    "cross_document_shadow_built": False,
    "keychain_registration_created": False,
    "cross_document_flags_preserved": True,
    "cross_document_eligibility_note": (
        "No storage/trust generation is registered in this isolated support; "
        "the unchanged app eligibility gate determines whether that optional "
        "candidate path is unavailable. Engine QF and structural-graph checks remain enabled."
    ),
}


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def write_json(path: Path, value: dict) -> None:
    """Atomically update only this harness's private, regular state files."""
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ValueError("private_output_is_not_a_regular_file")
    raw = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    descriptor, name = tempfile.mkstemp(prefix=".snapshot-test-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def read_json(path: Path) -> dict:
    from validate_intermediate_records import strict_json_loads

    if path.is_symlink() or not path.is_file():
        raise ValueError("required_json_is_not_a_regular_file")
    value = strict_json_loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("required_json_is_not_an_object")
    return value


@contextlib.contextmanager
def private_log(path: Path):
    descriptor = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "a", encoding="utf-8") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise ValueError("private_log_not_regular")
        yield handle


def require_inside(path: Path, root: Path) -> Path:
    resolved = path.resolve(strict=True)
    try:
        resolved.relative_to(root.resolve(strict=True))
    except ValueError as exc:
        raise ValueError("prepared_artifact_outside_private_output") from exc
    if path.is_symlink() or not resolved.is_file():
        raise ValueError("prepared_artifact_not_regular")
    return resolved


def output_directory(path: Path, *, prepare: bool) -> Path:
    # Limit this one approved experiment, not arbitrary user directories. Refuse
    # symlink ancestors before resolving so redirects cannot widen the scope.
    absolute = path.expanduser().absolute()
    for candidate in (absolute, *absolute.parents):
        if candidate.is_symlink():
            raise ValueError("output_symlink_forbidden")
    resolved = absolute.resolve()
    private = PRIVATE_ROOT.resolve()
    try:
        relative = resolved.relative_to(private)
    except ValueError as exc:
        raise ValueError("output_must_be_below_approved_private_root") from exc
    if not relative.parts:
        raise ValueError("output_requires_a_new_run_subdirectory")
    if prepare:
        if resolved.exists() and (not resolved.is_dir() or any(resolved.iterdir())):
            raise ValueError("preparation_requires_new_or_empty_output")
        resolved.mkdir(parents=True, exist_ok=True, mode=0o700)
    elif not resolved.is_dir():
        raise ValueError("query_requires_existing_prepared_output")
    return resolved


@contextlib.contextmanager
def private_runtime(output: Path):
    temporary = output / "temporary"
    temporary.mkdir(mode=0o700, exist_ok=True)
    if temporary.is_symlink():
        raise ValueError("private_temporary_directory_symlink")
    previous_env = os.environ.get("TMPDIR")
    previous_tempdir = tempfile.tempdir
    os.environ["TMPDIR"] = str(temporary)
    tempfile.tempdir = str(temporary)
    descriptor = os.open(output / ".harness.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError("harness_lock_not_regular")
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(descriptor)
        tempfile.tempdir = previous_tempdir
        if previous_env is None:
            os.environ.pop("TMPDIR", None)
        else:
            os.environ["TMPDIR"] = previous_env


def load_app(output: Path):
    """Only redirect filesystem constants; never replace a business function."""
    sys.path.insert(0, str(APP))
    try:
        spec = importlib.util.spec_from_file_location(
            "snapshot_test_app_server", APP / "local_memory_server.py"
        )
        if spec is None or spec.loader is None:
            raise RuntimeError("app_server_import_unavailable")
        server = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(server)
    finally:
        sys.path.remove(str(APP))
    bootstrap = server.bootstrap
    support = output / "support"
    support.mkdir(mode=0o700, exist_ok=True)
    if support.is_symlink():
        raise ValueError("private_support_symlink")
    bootstrap.SUPPORT = support
    bootstrap.CONFIG = support / "config.json"
    bootstrap.STATE = support / "state.json"
    bootstrap.DOCUMENT_VERSION_DECISIONS = support / "document-version-decisions.json"
    bootstrap.DOCUMENT_VERSION_REVIEW = support / "document-version-review.json"
    bootstrap.ENGINE = ENGINE
    server.ENGINE = ENGINE
    return server


def source_identity(snapshot: Path, source_root: Path) -> tuple[dict, dict]:
    from freeze_reading_snapshot import load_snapshot
    from adapt_layer1_to_local_memory import validate_source_binding

    if source_root.is_symlink() or not source_root.is_dir():
        raise ValueError("source_root_must_be_a_regular_directory")
    source_root = source_root.resolve(strict=True)
    before = digest(snapshot)
    envelope = load_snapshot(snapshot)
    if digest(snapshot) != before:
        raise ValueError("snapshot_changed_during_validation")
    for document in envelope["payload"]["records"]["documents"]:
        # Existing hash and byte-count check; this does NOT extract source text.
        validate_source_binding(source_root, document["source"])
    return envelope, {
        "snapshot_path": str(snapshot.resolve(strict=True)),
        "snapshot_sha256": before,
        "snapshot_id": envelope["snapshot_id"],
        "source_root": str(source_root),
        "source_manifest": envelope["payload"]["provenance"]["input_manifest"],
    }


def model_config(template: Path) -> dict:
    # Read only a small allowlist. Never copy generation, trust, source or index
    # pointers from an installed app into this separate experiment.
    values = read_json(template)
    for key, expected in EXPECTED_MODELS.items():
        if values.get(key) != expected:
            raise ValueError("configured_model_differs_from_approved_test")
    if values.get("sequential_model_loading") is not True:
        raise ValueError("configured_model_loading_differs_from_approved_test")
    if any(values.get(flag) is not True for flag in GRAPH_FLAGS):
        raise ValueError("configured_graph_flags_differ_from_approved_test")
    return {**EXPECTED_MODELS, "sequential_model_loading": True,
            **{flag: True for flag in GRAPH_FLAGS}}


def require_models(bootstrap, config: dict) -> None:
    # Do not start, download, or replace a model during preflight.
    if not bootstrap.ollama_online():
        raise RuntimeError("local_ollama_must_already_be_running")
    installed = bootstrap.model_names()
    if any(not bootstrap._model_installed(config[key], installed) for key in EXPECTED_MODELS):
        raise RuntimeError("required_local_models_are_not_installed_no_download_attempted")


def run_stage(name: str, arguments: list[str], output: Path, timings: list[dict]) -> None:
    started = time.perf_counter()
    print(json.dumps({"status": "running", "stage": name}), flush=True)
    item: dict[str, Any] = {"stage": name, "started_at": now()}
    timings.append(item)
    try:
        with private_log(output / "execution.log") as log:
            log.write("\n[" + name + "] " + json.dumps(arguments, ensure_ascii=False) + "\n")
            log.flush()
            subprocess.run([sys.executable, *arguments], stdout=log, stderr=subprocess.STDOUT,
                           cwd=ROOT, check=True, timeout=1800)
        item["status"] = "completed"
    except BaseException:
        item["status"] = "failed"
        raise
    finally:
        item["seconds"] = round(time.perf_counter() - started, 6)
        write_json(output / "stage-timings.json", {"stages": timings})
        print(json.dumps(item), flush=True)


def complete_preparation(args, output: Path, server, identity: dict, state: dict,
                         generation: Path, config: dict, flags: list[str], timings: list[dict]) -> None:
    """Finish the one unpublished preparation, never overwrite an index."""
    bootstrap = server.bootstrap
    paths, semantic, security = (generation / name for name in ("01-path", "02-semantic", "03-security"))
    inventory = paths / "path-source-inventory.jsonl"
    version_graph = paths / "document-version-graph.json"
    index = generation / "safe-answer-index.sqlite3"
    if any(os.path.lexists(str(index) + suffix) for suffix in ("", "-wal", "-shm", "-journal")):
        raise ValueError("index_already_exists_no_rebuild_allowed")
    # The gate intentionally expects its caller to create this directory.
    # Refuse any existing output rather than silently redoing a partial gate.
    security.mkdir(mode=0o700)

    def stage(name, script, *arguments):
        run_stage(name, [str(ENGINE / script), *map(str, arguments)], output, timings)

    stage("content_security_gate", "content_security_gate.py", "--evidence", semantic / "semantic-evidence.jsonl",
          "--documents", semantic / "semantic-documents.jsonl", "--output-dir", security)
    stage("content_security_validation", "validate_content_security_gate.py", "--evidence", semantic / "semantic-evidence.jsonl",
          "--documents", semantic / "semantic-documents.jsonl", "--gate-dir", security)
    stage("safe_answer_index", "build_local_semantic_index.py", "--evidence", security / "safe-answer-evidence.jsonl",
          "--documents", semantic / "semantic-documents.jsonl", "--security-state", security / "content-security-state.json",
          "--source-root", args.source_root, "--source-inventory", inventory, "--version-graph", version_graph,
          "--index-purpose", "safe_answer", "--model", config["embedding_model"], *flags, "--output", index)
    _, after = source_identity(args.snapshot, args.source_root)
    if after != identity:
        raise ValueError("input_changed_during_preparation")
    # This fixture is not an app-ready generation and has no trust registration.
    config.update({"source_root": identity["source_root"], "workspace": str(bootstrap.SUPPORT / "data"),
                   "index_path": str(index)})
    bootstrap.atomic_json(bootstrap.CONFIG, config)
    bootstrap.atomic_json(bootstrap.STATE, {"phase": "backend_test_only", "message": "Not an app-ready generation"})
    eligibility, reason = server.semantic_graph_candidate_eligibility(config, index)
    if eligibility:
        raise RuntimeError("unexpected_optional_graph_activation_without_registration")
    state.update({"status": "prepared_backend_only", "index_path": str(index), "index_sha256": digest(index),
                  "config_sha256": digest(bootstrap.CONFIG), "models": {key: config[key] for key in EXPECTED_MODELS},
                  "graph_flags": {key: config[key] for key in GRAPH_FLAGS},
                  "optional_cross_document_eligibility": {"eligible": eligibility, "reason": reason},
                  "stages": timings})


def prepare(args, output: Path, server, identity: dict) -> None:
    bootstrap = server.bootstrap
    config = model_config(args.config_template)
    require_models(bootstrap, config)
    started = time.perf_counter()
    state = {"harness": HARNESS_ID, "status": "preparing", "created_at": now(),
             "input": identity, "scope": SCOPE, "question_attempts": [], "preparation_count": 1,
             "models": {key: config[key] for key in EXPECTED_MODELS},
             "graph_flags": {key: config[key] for key in GRAPH_FLAGS}}
    write_json(output / STATE_NAME, state)
    timings: list[dict] = []
    generation = bootstrap.SUPPORT / "data" / "generations" / ("generation-" + uuid.uuid4().hex)
    generation.mkdir(parents=True, mode=0o700)
    paths, semantic = (generation / name for name in ("01-path", "02-semantic"))
    paths.mkdir()
    inventory = paths / "path-source-inventory.jsonl"
    version_graph = paths / "document-version-graph.json"

    def stage(name, script, *arguments):
        run_stage(name, [str(ENGINE / script), *map(str, arguments)], output, timings)

    try:
        stage("path_inventory", "build_path_graph.py", args.source_root, "--output-dir", paths)
        stage("path_validation", "validate_path_graph.py", paths / "path-evidence-graph.json",
              inventory, "--source-root", args.source_root)
        decisions = bootstrap.capture_decision_snapshot(paths, bootstrap.DEFAULT_DECISION_SNAPSHOT_BYTES)
        flags = bootstrap._snapshot_flags(decisions)
        stage("version_resolution", "document_version_resolver.py", "build", "--inventory", inventory,
              "--output", version_graph, "--decisions", decisions["path"])
        stage("version_validation", "document_version_resolver.py", "validate", "--graph", version_graph,
              "--inventory", inventory, "--decisions", decisions["path"], "--decision-mode", "snapshot",
              "--decisions-sha256", decisions["sha256"])
        stage("snapshot_semantic_projection", "build_adaptive_semantic_graph.py", "--inventory", inventory,
              "--source-root", args.source_root, "--output-dir", semantic, "--version-graph", version_graph,
              *flags, "--reading-snapshot", args.snapshot)
        stage("semantic_lineage_validation", "validate_adaptive_semantic_graph.py", "--output-dir", semantic,
              "--source-root", args.source_root, "--inventory", inventory, "--version-graph", version_graph,
              "--initialize-lineage", *flags)
        complete_preparation(args, output, server, identity, state, generation, config, flags, timings)
    except BaseException:
        state.update({"status": "preparation_failed", "stages": timings})
        raise
    finally:
        state["preparation_seconds"] = round(time.perf_counter() - started, 6)
        write_json(output / STATE_NAME, state)


def resume_preparation(args, output: Path, server, identity: dict) -> None:
    """One narrowly bounded continuation of the missing-security-directory bug."""
    state = read_json(output / STATE_NAME)
    expected_stages = ["path_inventory", "path_validation", "version_resolution", "version_validation",
                       "snapshot_semantic_projection", "semantic_lineage_validation", "content_security_gate"]
    stages = state.get("stages")
    if (state.get("harness") != HARNESS_ID or state.get("status") != "preparation_failed"
            or type(state.get("preparation_count")) is not int or state["preparation_count"] != 1
            or state.get("preparation_resume_count", 0) != 0 or state.get("question_attempts") != []
            or state.get("input") != identity or state.get("scope") != SCOPE
            or not isinstance(stages, list) or len(stages) != len(expected_stages)
            or any(not isinstance(item, dict) for item in stages)
            or [item.get("stage") for item in stages] != expected_stages
            or [item.get("status") for item in stages] != ["completed"] * 6 + ["failed"]):
        raise ValueError("resume_requires_original_security_gate_failure_before_index")
    if read_json(output / "stage-timings.json").get("stages") != stages:
        raise ValueError("failed_stage_history_changed")
    bootstrap = server.bootstrap
    config = model_config(args.config_template)
    for key, expected in (("models", EXPECTED_MODELS), ("graph_flags", {flag: True for flag in GRAPH_FLAGS})):
        # The original failure predates persistence of these fixed allowlists.
        # Its prepare() still enforced exactly these names and all five flags.
        if key in state and state[key] != expected:
            raise ValueError("failed_preparation_model_policy_changed")
    for path in (bootstrap.CONFIG, bootstrap.STATE, bootstrap.DOCUMENT_VERSION_DECISIONS,
                 bootstrap.DOCUMENT_VERSION_REVIEW):
        if os.path.lexists(path):
            raise ValueError("resume_refuses_existing_support_configuration")
    generations = bootstrap.SUPPORT / "data" / "generations"
    for directory in (bootstrap.SUPPORT / "data", generations):
        if directory.is_symlink() or not directory.is_dir():
            raise ValueError("resume_generation_directory_invalid")
    candidates = list(generations.iterdir())
    if (len(candidates) != 1 or candidates[0].is_symlink() or not candidates[0].is_dir()
            or bootstrap.GENERATION_NAME.fullmatch(candidates[0].name) is None):
        raise ValueError("resume_requires_exactly_one_unpublished_generation")
    generation = candidates[0]
    if {path.name for path in generation.iterdir()} != {"01-path", "02-semantic"}:
        raise ValueError("resume_refuses_security_or_index_outputs")
    paths, semantic = (generation / name for name in ("01-path", "02-semantic"))
    for directory in (paths, semantic):
        if directory.is_symlink() or not directory.is_dir():
            raise ValueError("resume_stage_directory_invalid")
    artifacts = {}
    for directory in (paths, semantic):
        for path in directory.rglob("*"):
            if path.is_symlink() or not (path.is_file() or path.is_dir()):
                raise ValueError("resume_stage_artifact_invalid")
            if path.is_file():
                artifacts[str(path.relative_to(generation))] = digest(require_inside(path, output))
    decision_path = require_inside(paths / "document-version-decisions.snapshot.json", output)
    descriptor = bootstrap._decision_snapshot_descriptor(paths, {
        "generation": generation.name, "path": str(decision_path), "sha256": digest(decision_path),
        "byte_count": decision_path.stat().st_size,
    })
    flags = bootstrap._snapshot_flags(descriptor)
    require_models(bootstrap, config)
    # Preserve the failure verbatim in the same state; never relabel its stage
    # as successful, increment preparation_count, or initialize lineage again.
    history = dict(state)
    history["stages"] = [dict(item) for item in stages]
    timings = [dict(item) for item in stages]
    prior_seconds = float(state["preparation_seconds"])
    state.update({"status": "resuming_preparation", "preparation_resume_count": 1,
                  "preparation_failure_history": [history], "resumed_at": now(),
                  "generation": generation.name, "resume_input_artifact_hashes": artifacts,
                  "models": dict(EXPECTED_MODELS), "graph_flags": {flag: True for flag in GRAPH_FLAGS}})
    write_json(output / STATE_NAME, state)
    started = time.perf_counter()

    def stage(name, script, *arguments):
        run_stage(name, [str(ENGINE / script), *map(str, arguments)], output, timings)

    try:
        inventory = paths / "path-source-inventory.jsonl"
        version_graph = paths / "document-version-graph.json"
        stage("resume_path_validation", "validate_path_graph.py", paths / "path-evidence-graph.json",
              inventory, "--source-root", args.source_root)
        stage("resume_version_validation", "document_version_resolver.py", "validate", "--graph", version_graph,
              "--inventory", inventory, "--decisions", descriptor["path"], "--decision-mode", "snapshot",
              "--decisions-sha256", descriptor["sha256"])
        stage("resume_semantic_lineage_validation", "validate_adaptive_semantic_graph.py", "--output-dir", semantic,
              "--source-root", args.source_root, "--inventory", inventory, "--version-graph", version_graph, *flags)
        if any(digest(generation / name) != sha for name, sha in artifacts.items()):
            raise ValueError("prepared_artifact_changed_during_resume_validation")
        complete_preparation(args, output, server, identity, state, generation, config, flags, timings)
    except BaseException:
        state.update({"status": "preparation_failed", "stages": timings})
        raise
    finally:
        state["preparation_resume_seconds"] = round(time.perf_counter() - started, 6)
        state["preparation_seconds"] = round(prior_seconds + state["preparation_resume_seconds"], 6)
        write_json(output / STATE_NAME, state)


def snapshot_context_comparison(attempts: list[dict], question_hash: str, enabled: bool) -> dict:
    """Allow only the explicitly requested one-off comparison of a failed answer."""
    matching = [item for item in attempts if item.get("question_sha256") == question_hash]
    if not enabled:
        if matching:
            raise ValueError("question_already_attempted_no_automatic_retry")
        return {}
    if any("context_comparison" in item for item in attempts):
        raise ValueError("context_comparison_already_attempted")
    if (len(matching) != 1 or matching[0].get("status") != "completed_backend_only"
            or matching[0].get("answer_status") != "insufficient"):
        raise ValueError("context_comparison_requires_one_completed_insufficient_answer")
    return {"change_id": CONTEXT_COMPARISON_ID, "baseline_attempt": matching[0]["number"],
            "requested_field_context_tokens": 8192}


def query(args, output: Path, server, identity: dict) -> None:
    state = read_json(output / STATE_NAME)
    if state.get("harness") != HARNESS_ID or state.get("status") != "prepared_backend_only":
        raise ValueError("query_requires_successfully_prepared_backend")
    if state.get("input") != identity or state.get("scope") != SCOPE:
        raise ValueError("prepared_input_or_scope_changed")
    bootstrap = server.bootstrap
    config = read_json(bootstrap.CONFIG)
    index = require_inside(Path(state["index_path"]), output)
    if config.get("index_path") != str(index) or digest(index) != state.get("index_sha256"):
        raise ValueError("prepared_index_changed")
    if digest(bootstrap.CONFIG) != state.get("config_sha256"):
        raise ValueError("prepared_config_changed")
    if any(config.get(flag) is not True for flag in GRAPH_FLAGS):
        raise ValueError("prepared_graph_flags_changed")
    attempts = state.get("question_attempts")
    if not isinstance(attempts, list) or len(attempts) >= MAX_QUERIES:
        raise ValueError("four_question_attempt_limit_reached")
    if not 1 <= len(args.query.strip()) <= 2000:
        raise ValueError("question_must_contain_1_to_2000_characters")
    question_hash = hashlib.sha256(args.query.encode("utf-8")).hexdigest()
    comparison = snapshot_context_comparison(attempts, question_hash, args.compare_context_8192)
    require_models(bootstrap, config)
    number = len(attempts) + 1
    result_path = output / f"question-{number:02d}.json"
    if os.path.lexists(result_path):
        raise ValueError("question_result_already_exists")
    attempt = {"number": number, "status": "running", "started_at": now(), "question_sha256": question_hash}
    if comparison:
        attempt["context_comparison"] = comparison
    attempts.append(attempt)
    write_json(output / STATE_NAME, state)  # Count interruptions/failures too.
    started = time.perf_counter()
    print(json.dumps({"status": "running", "stage": "app_answer_and_final_audit", "attempt": number}), flush=True)
    try:
        # This documented non-HTTP entry retains the real engine, final audit
        # and optional-path eligibility gates. It does not claim UI intent QA.
        with private_log(output / "execution.log") as log:
            with contextlib.redirect_stdout(log), contextlib.redirect_stderr(log):
                record = server.answer_query(args.query)
        _, after = source_identity(args.snapshot, args.source_root)
        if after != identity or digest(index) != state["index_sha256"] or digest(bootstrap.CONFIG) != state["config_sha256"]:
            raise ValueError("prepared_inputs_changed_during_answer")
        write_json(result_path, {"question": args.query, "record": record, "scope": SCOPE,
                                 "input": identity, "seconds": round(time.perf_counter() - started, 6),
                                 "context_comparison": comparison,
                                 "optional_cross_document_eligibility": state["optional_cross_document_eligibility"]})
        attempt.update({"status": "completed_backend_only", "result_file": result_path.name,
                        "answer_status": record.get("answer", {}).get("answer_status"),
                        "final_audit_verdict": record.get("independent_final_audit", {}).get("verdict")})
    except BaseException:
        attempt["status"] = "failed"
        raise
    finally:
        attempt["seconds"] = round(time.perf_counter() - started, 6)
        write_json(output / STATE_NAME, state)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--snapshot", required=True, type=Path)
    parser.add_argument("--source-root", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--config-template", type=Path, default=DEFAULT_CONFIG_TEMPLATE,
                        help="Read-only source of approved model names and flags; only used during preparation")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--query", help="Use the prepared index; never prepare/rebuild when present")
    mode.add_argument("--resume-preparation", action="store_true",
                      help="Resume only the original missing-directory security failure; never rebuild any completed stage")
    parser.add_argument("--compare-context-8192", action="store_true",
                        help="Run the user-approved context-capacity comparison once against the same insufficient question")
    args = parser.parse_args()
    if args.compare_context_8192 and args.query is None:
        parser.error("--compare-context-8192 requires --query")
    previous_umask = os.umask(0o077)
    output = None
    try:
        if args.source_root.is_symlink() or not args.source_root.is_dir():
            raise ValueError("source_root_must_be_a_regular_directory")
        args.source_root = args.source_root.resolve(strict=True)
        # Reject source/output overlap before creating any directories or logs.
        prospective_output = args.output.expanduser().resolve()
        try:
            prospective_output.relative_to(args.source_root)
        except ValueError:
            pass
        else:
            raise ValueError("output_must_not_be_inside_source_root")
        output = output_directory(args.output, prepare=args.query is None and not args.resume_preparation)
        with private_runtime(output):
            _, identity = source_identity(args.snapshot, args.source_root)
            args.snapshot = args.snapshot.resolve(strict=True)
            args.source_root = args.source_root.resolve(strict=True)
            server = load_app(output)
            if args.resume_preparation:
                resume_preparation(args, output, server, identity)
            elif args.query is None:
                prepare(args, output, server, identity)
            else:
                query(args, output, server, identity)
        print(json.dumps({"status": "completed_backend_only", "stage": "question" if args.query is not None else "preparation",
                          "gui_acceptance_test": False, "installed_app_changed": False}), flush=True)
        return 0
    except Exception as exc:
        if output is not None:
            try:
                with private_log(output / "failure.log") as log:
                    traceback.print_exc(file=log)
            except OSError:
                pass  # Never follow a substituted diagnostic symlink.
        # Exception messages may contain source paths/text. Keep them private.
        print(json.dumps({"status": "failed", "error_type": type(exc).__name__,
                          "details": "private failure.log if output initialization succeeded"}), flush=True)
        return 1
    finally:
        os.umask(previous_umask)


if __name__ == "__main__":
    raise SystemExit(main())
