#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import io
import json
import subprocess
import types
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / "engine"


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def prepare_shadow_inputs(bootstrap, generation: Path) -> tuple[Path, Path]:
    semantic = generation / "02-semantic-model-ready"
    security = generation / "03-security-model-ready"
    semantic.mkdir(parents=True)
    security.mkdir(parents=True)
    documents = semantic / "semantic-documents.jsonl"
    source_evidence = semantic / "semantic-evidence.jsonl"
    evidence = security / "safe-answer-evidence.jsonl"
    documents.write_text('{"document_id":"doc"}\n', encoding="utf-8")
    source_evidence.write_text('{"evidence_id":"ev"}\n', encoding="utf-8")
    evidence.write_text('{"evidence_id":"ev"}\n', encoding="utf-8")
    bootstrap.atomic_json(security / "content-security-state.json", {
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
        "source_evidence": {
            "sha256": bootstrap.sha256_file(source_evidence),
        },
        "source_documents": {"sha256": bootstrap.sha256_file(documents)},
        "outputs": {
            "safe-answer-evidence.jsonl": {
                "sha256": bootstrap.sha256_file(evidence),
                "size_bytes": evidence.stat().st_size,
            }
        },
    })
    return semantic, security


def prepare_real_shadow_inputs(generation: Path) -> tuple[Path, Path]:
    semantic = generation / "02-semantic-model-ready"
    security = generation / "03-security-model-ready"
    semantic.mkdir(parents=True)
    security.mkdir(parents=True)
    documents = semantic / "semantic-documents.jsonl"
    source_evidence = semantic / "semantic-evidence.jsonl"
    source = {
        "relative_path": "assignments.xlsx",
        "sha256": "1" * 64,
        "extension": "xlsx",
    }
    headers = (
        "Project ID",
        "Work ID",
        "Role",
        "Assignee ID",
        "Valid From",
        "Status",
    )
    values = (
        "PRJ-1",
        "WORK-1",
        "主担当",
        "EMP-1",
        "2022-01-01",
        "final",
    )
    records = []
    for row_number, row in enumerate((headers, values), 1):
        for column_number, value in enumerate(row, 1):
            records.append({
                "evidence_id": f"ev_{row_number}_{column_number}",
                "document_id": "doc_assignments",
                "source": source,
                "locator": {
                    "sheet_name": "Assignments",
                    "cell": f"{chr(64 + column_number)}{row_number}",
                },
                "observed_text": value,
                "ordinal": len(records) + 1,
                "adapter": {
                    "execution_policy": "never_execute",
                    "source_record_type": "table_cell",
                },
                "status": "observed",
            })
    documents.write_text(
        json.dumps({
            "document_id": "doc_assignments",
            "source": source,
            "evidence_ids": [item["evidence_id"] for item in records],
            "status": "extracted",
        }, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    source_evidence.write_text(
        "".join(
            json.dumps(
                record,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ) + "\n"
            for record in records
        ),
        encoding="utf-8",
    )
    security_builder = load_module(
        "runtime_shadow_real_security_builder",
        ENGINE / "content_security_gate.py",
    )
    security_builder.build(
        source_evidence,
        documents,
        security,
        created_at="2026-09-03T00:00:00+09:00",
    )
    return semantic, security


def prepare_published_generation(bootstrap, base: Path) -> tuple[Path, dict]:
    support = base / "support"
    workspace = support / "data"
    generation = (
        workspace / "generations" / ("generation-" + "9" * 32)
    )
    paths = generation / "01-path"
    semantic = generation / "02-semantic"
    security = generation / "03-security"
    for directory in (paths, semantic, security):
        directory.mkdir(parents=True)
    index = generation / "safe-answer-index.sqlite3"
    index.write_bytes(b"published-production-index")
    bootstrap.atomic_json(
        semantic / "adaptive-reader-state.json",
        {"status": "complete", "limitations": {}},
    )
    bootstrap.SUPPORT = support
    bootstrap.CONFIG = support / "config.json"
    bootstrap.STATE = support / "state.json"
    pending = bootstrap._shadow_run_base(
        generation,
        "build-published-shadow",
        status="pending",
        reason_code="scheduled_after_production_publish",
        elapsed_ms=0,
    )
    source = base / "source"
    source.mkdir()
    bootstrap.atomic_json(bootstrap.CONFIG, {
        "source_root": str(source),
        "workspace": str(workspace),
        "active_generation": generation.name,
        "path_graph_path": str(paths),
        "semantic_path": str(semantic),
        "security_path": str(security),
        "index_path": str(index),
        bootstrap.CROSS_DOCUMENT_SHADOW_FLAG: True,
    })
    bootstrap.atomic_json(
        generation / bootstrap.GENERATION_MARKER,
        {
            "schema_version": "0.1",
            "status": "published",
            "build_id": "build-published-shadow",
            "generation": generation.name,
            "owner_pid": 99_999_999,
            "cross_document_semantic_graph_shadow": pending,
        },
    )
    bootstrap.atomic_json(bootstrap.STATE, {
        "phase": "ready",
        "message": "索引の作成が完了しました。",
        "error": "",
        "cross_document_semantic_graph_shadow": pending,
    })
    return generation, pending


class RuntimeRecoveryTests(unittest.TestCase):
    def test_configure_source_enables_shadow_without_publishing_a_graph_pointer(self) -> None:
        bootstrap = load_module(
            "runtime_bootstrap_shadow_configure", ROOT / "app" / "bootstrap.py"
        )
        with TemporaryDirectory() as temporary:
            base = Path(temporary)
            source = base / "source"
            source.mkdir()
            bootstrap.SUPPORT = base / "support"
            bootstrap.CONFIG = bootstrap.SUPPORT / "config.json"
            bootstrap.atomic_json(bootstrap.CONFIG, {
                "embedding_model": "embeddinggemma:latest",
                "answer_model": "gemma4:12b",
                "audit_model": "gemma4:12b",
                "active_generation": "generation-" + "f" * 32,
                "semantic_graph_shadow_path": "/stale/shadow.sqlite3",
            })

            configured = bootstrap.configure_source(source)

            self.assertTrue(configured[bootstrap.CROSS_DOCUMENT_SHADOW_FLAG])
            self.assertNotIn("active_generation", configured)
            self.assertNotIn("semantic_graph_shadow_path", configured)

    def test_configure_source_preserves_explicit_shadow_rollback(self) -> None:
        bootstrap = load_module(
            "runtime_bootstrap_shadow_rollback", ROOT / "app" / "bootstrap.py"
        )
        with TemporaryDirectory() as temporary:
            base = Path(temporary)
            source = base / "source"
            source.mkdir()
            bootstrap.SUPPORT = base / "support"
            bootstrap.CONFIG = bootstrap.SUPPORT / "config.json"
            bootstrap.atomic_json(bootstrap.CONFIG, {
                "embedding_model": "embeddinggemma:latest",
                "answer_model": "gemma4:12b",
                "audit_model": "gemma4:12b",
                bootstrap.CROSS_DOCUMENT_SHADOW_FLAG: False,
            })

            configured = bootstrap.configure_source(source)

            self.assertFalse(configured[bootstrap.CROSS_DOCUMENT_SHADOW_FLAG])

    def test_llm_extraction_flag_is_derived_from_layer1_provenance(self) -> None:
        builder = load_module(
            "runtime_builder", ENGINE / "build_adaptive_semantic_graph.py"
        )
        validator = load_module(
            "runtime_validator", ENGINE / "validate_adaptive_semantic_graph.py"
        )
        records = [{
            "evidence_id": "ev_local_vlm",
            "provenance": {
                "extraction_method": "local_vlm_unlocated_transcript_provisional"
            },
            "native_properties": {"runner": "ollama_loopback_chat"},
        }]
        expected = {
            "used": True,
            "evidence_count": 1,
            "methods": {"local_vlm_unlocated_transcript_provisional": 1},
        }
        self.assertEqual(builder.derive_llm_extraction(records), expected)
        self.assertEqual(validator.derive_llm_extraction(records), expected)
        self.assertEqual(
            builder.derive_llm_extraction([{
                "provenance": {"extraction_method": "dual_local_ocr_consensus"},
                "native_properties": {},
            }]),
            {"used": False, "evidence_count": 0, "methods": {}},
        )

    def test_clean_first_build_rebuilds_semantic_after_gemma_pull(self) -> None:
        bootstrap = load_module("runtime_bootstrap_build", ROOT / "app" / "bootstrap.py")
        with TemporaryDirectory() as temporary:
            base = Path(temporary)
            support = base / "support"
            source = base / "source"
            source.mkdir()
            (source / "scan.png").write_bytes(b"not-read-by-this-mock")
            workspace = support / "data"
            config_path = support / "config.json"
            state_path = support / "state.json"
            bootstrap.SUPPORT = support
            bootstrap.CONFIG = config_path
            bootstrap.STATE = state_path
            bootstrap.atomic_json(config_path, {
                "source_root": str(source),
                "workspace": str(workspace),
                "embedding_model": "embeddinggemma:latest",
                "answer_model": "gemma4:12b",
                "audit_model": "gemma4:12b",
                "semantic_graph_shadow_path": "/stale/shadow.sqlite3",
            })

            semantic_calls: list[Path] = []

            def fake_semantic(_source, _paths, semantic, security, _log):
                semantic_calls.append(semantic)
                semantic.mkdir(parents=True)
                security.mkdir(parents=True)
                bootstrap.atomic_json(
                    semantic / "layer1-input-manifest.json", {"paths": ["scan.png"]}
                )
                state = {
                    "status": "complete_with_limits",
                    "limitations": {"partial_documents": 1},
                    "llm_used_for_extraction": len(semantic_calls) == 2,
                }
                bootstrap.atomic_json(semantic / "adaptive-reader-state.json", state)
                return state

            def fake_run(command, _log):
                if any(str(value).endswith("build_path_graph.py") for value in command):
                    output = Path(command[command.index("--output-dir") + 1])
                    (output / "path-source-inventory.jsonl").write_text(
                        json.dumps({
                            "kind": "file",
                            "relative_path": "scan.png",
                            "read_status": "observed",
                        }) + "\n",
                        encoding="utf-8",
                    )
                if any(str(value).endswith("build_local_semantic_index.py") for value in command):
                    Path(command[command.index("--output") + 1]).write_bytes(b"index")

            identifiers = [
                types.SimpleNamespace(hex="1" * 32),
                types.SimpleNamespace(hex="2" * 32),
            ]
            with (
                mock.patch.object(bootstrap.uuid, "uuid4", side_effect=identifiers),
                mock.patch.object(bootstrap, "run", side_effect=fake_run),
                mock.patch.object(
                    bootstrap, "run_semantic_pipeline", side_effect=fake_semantic
                ),
                mock.patch.object(
                    bootstrap,
                    "ensure_models",
                    return_value=[bootstrap.IMAGE_FALLBACK_MODEL],
                ),
                mock.patch.object(
                    bootstrap,
                    "local_model_available",
                    side_effect=[False, True],
                ),
                mock.patch.object(
                    bootstrap,
                    "run_cross_document_semantic_graph_shadow",
                    return_value={
                        "status": "complete",
                        "shadow_only": True,
                        "used_for_answers": False,
                    },
                ) as shadow,
            ):
                bootstrap.build_index()

            self.assertEqual(len(semantic_calls), 2)
            self.assertEqual(semantic_calls[0].name, "02-semantic")
            self.assertEqual(semantic_calls[1].name, "02-semantic-model-ready")
            published = bootstrap.load_json(config_path)
            self.assertTrue(published["semantic_path"].endswith("02-semantic-model-ready"))
            self.assertTrue(published["security_path"].endswith("03-security-model-ready"))
            self.assertTrue(Path(published["index_path"]).is_file())
            self.assertTrue(published[bootstrap.CROSS_DOCUMENT_SHADOW_FLAG])
            runtime_state = bootstrap.load_json(state_path)
            self.assertEqual(runtime_state["phase"], "ready_with_limits")
            self.assertEqual(
                "complete",
                runtime_state["cross_document_semantic_graph_shadow"]["status"],
            )
            shadow.assert_called_once()
            shadow_args = shadow.call_args.args
            self.assertEqual("02-semantic-model-ready", shadow_args[1].name)
            self.assertEqual("03-security-model-ready", shadow_args[2].name)

    def test_dead_build_owner_removes_only_marked_unpublished_generation(self) -> None:
        bootstrap = load_module("runtime_bootstrap_recovery", ROOT / "app" / "bootstrap.py")
        with TemporaryDirectory() as temporary:
            base = Path(temporary)
            support = base / "support"
            workspace = support / "data"
            generations = workspace / "generations"
            orphan_name = "generation-" + "a" * 32
            orphan = generations / orphan_name
            orphan.mkdir(parents=True)
            bootstrap.SUPPORT = support
            bootstrap.CONFIG = support / "config.json"
            bootstrap.STATE = support / "state.json"
            bootstrap.atomic_json(bootstrap.CONFIG, {"workspace": str(workspace)})
            bootstrap.atomic_json(orphan / bootstrap.GENERATION_MARKER, {
                "status": "building",
                "generation": orphan_name,
                "owner_pid": 99999999,
            })
            bootstrap.atomic_json(bootstrap.STATE, {
                "phase": "building",
                "generation": orphan_name,
                "owner_pid": 99999999,
            })

            with mock.patch.object(bootstrap, "_pid_is_alive", return_value=False):
                result = bootstrap.recover_interrupted_build()

            self.assertFalse(orphan.exists())
            self.assertIn(orphan_name, result["removed"])
            recovered_state = bootstrap.load_json(bootstrap.STATE)
            self.assertEqual(recovered_state["phase"], "error")
            self.assertEqual(recovered_state["recovery_action"], "retry_build")

    def test_dead_owner_restores_generation_already_atomically_published(self) -> None:
        bootstrap = load_module("runtime_bootstrap_published", ROOT / "app" / "bootstrap.py")
        with TemporaryDirectory() as temporary:
            base = Path(temporary)
            support = base / "support"
            workspace = support / "data"
            name = "generation-" + "b" * 32
            generation = workspace / "generations" / name
            paths = generation / "01-path"
            semantic = generation / "02-semantic"
            security = generation / "03-security"
            for path in (paths, semantic, security):
                path.mkdir(parents=True)
            index = generation / "safe-answer-index.sqlite3"
            index.write_bytes(b"published-index")
            bootstrap.SUPPORT = support
            bootstrap.CONFIG = support / "config.json"
            bootstrap.STATE = support / "state.json"
            bootstrap.atomic_json(semantic / "adaptive-reader-state.json", {
                "status": "complete",
                "limitations": {},
            })
            bootstrap.atomic_json(generation / bootstrap.GENERATION_MARKER, {
                "status": "building",
                "generation": name,
                "owner_pid": 99999999,
            })
            bootstrap.atomic_json(bootstrap.CONFIG, {
                "workspace": str(workspace),
                "active_generation": name,
                "path_graph_path": str(paths),
                "semantic_path": str(semantic),
                "security_path": str(security),
                "index_path": str(index),
            })
            bootstrap.atomic_json(bootstrap.STATE, {
                "phase": "building",
                "generation": name,
                "owner_pid": 99999999,
            })

            with mock.patch.object(bootstrap, "_pid_is_alive", return_value=False):
                result = bootstrap.recover_interrupted_build()

            self.assertEqual(result["status"], "recovered_published")
            self.assertEqual(result["shadow_status"], "held")
            self.assertTrue(generation.is_dir())
            recovered_state = bootstrap.load_json(bootstrap.STATE)
            self.assertEqual(recovered_state["phase"], "ready")
            self.assertTrue(recovered_state["recovered_after_interruption"])
            self.assertEqual(
                "held",
                recovered_state[
                    "cross_document_semantic_graph_shadow"
                ]["status"],
            )
            self.assertEqual(
                bootstrap.load_json(generation / bootstrap.GENERATION_MARKER)["status"],
                "published",
            )

    def test_building_state_with_published_pending_shadow_recovers_in_one_startup(self) -> None:
        bootstrap = load_module(
            "runtime_bootstrap_shadow_publish_window_recovery",
            ROOT / "app" / "bootstrap.py",
        )
        with TemporaryDirectory() as temporary:
            base = Path(temporary)
            generation, _pending = prepare_published_generation(
                bootstrap, base
            )
            candidate = generation / (
                bootstrap.CROSS_DOCUMENT_SHADOW_DIR + ".building"
            )
            candidate.mkdir()
            bootstrap.atomic_json(bootstrap.STATE, {
                "phase": "building",
                "generation": generation.name,
                "owner_pid": 99_999_999,
            })

            with mock.patch.object(
                bootstrap, "_pid_is_alive", return_value=False
            ):
                report = bootstrap.recover_interrupted_build()

            self.assertEqual("recovered_published", report["status"])
            self.assertEqual("held", report["shadow_status"])
            self.assertFalse(candidate.exists())
            recovered_state = bootstrap.load_json(bootstrap.STATE)
            self.assertEqual("ready", recovered_state["phase"])
            self.assertEqual(
                "held",
                recovered_state[
                    "cross_document_semantic_graph_shadow"
                ]["status"],
            )

    def test_ready_generation_recovers_interrupted_shadow_as_held(self) -> None:
        bootstrap = load_module(
            "runtime_bootstrap_shadow_pending_recovery",
            ROOT / "app" / "bootstrap.py",
        )
        with TemporaryDirectory() as temporary:
            base = Path(temporary)
            generation, _pending = prepare_published_generation(
                bootstrap, base
            )
            candidate = generation / (
                bootstrap.CROSS_DOCUMENT_SHADOW_DIR + ".building"
            )
            candidate.mkdir()
            (candidate / "incomplete.sqlite3").write_bytes(b"incomplete")
            held_candidate = generation / (
                bootstrap.CROSS_DOCUMENT_SHADOW_DIR + ".held-building"
            )
            held_candidate.mkdir()
            (held_candidate / "partial-state.json").write_text(
                "{}\n", encoding="utf-8"
            )
            production_sentinel = generation / "production-sentinel.txt"
            production_sentinel.write_text("keep", encoding="utf-8")

            with mock.patch.object(
                bootstrap, "_pid_is_alive", return_value=False
            ):
                report = bootstrap.recover_interrupted_build()

            self.assertEqual("recovered_published_shadow", report["status"])
            self.assertEqual("held", report["shadow_status"])
            self.assertFalse(candidate.exists())
            self.assertFalse(held_candidate.exists())
            self.assertEqual(
                b"published-production-index",
                Path(bootstrap.load_json(bootstrap.CONFIG)["index_path"])
                .read_bytes(),
            )
            self.assertEqual(
                "keep", production_sentinel.read_text(encoding="utf-8")
            )
            final = generation / bootstrap.CROSS_DOCUMENT_SHADOW_DIR
            recovered_shadow = bootstrap.load_json(
                final / bootstrap.CROSS_DOCUMENT_SHADOW_RUN_STATE
            )
            self.assertEqual("held", recovered_shadow["status"])
            self.assertTrue(recovered_shadow["removed_incomplete_candidate"])
            runtime_state = bootstrap.load_json(bootstrap.STATE)
            self.assertEqual("ready", runtime_state["phase"])
            self.assertEqual(
                "held",
                runtime_state["cross_document_semantic_graph_shadow"]["status"],
            )

    def test_recovery_preserves_completed_shadow_if_state_update_was_interrupted(self) -> None:
        bootstrap = load_module(
            "runtime_bootstrap_shadow_complete_recovery",
            ROOT / "app" / "bootstrap.py",
        )
        with TemporaryDirectory() as temporary:
            base = Path(temporary)
            generation, _pending = prepare_published_generation(
                bootstrap, base
            )
            final = generation / bootstrap.CROSS_DOCUMENT_SHADOW_DIR
            final.mkdir()
            graph = final / "semantic-graph.sqlite3"
            graph.write_bytes(b"completed-shadow")
            complete = {
                **bootstrap._shadow_run_base(
                    generation,
                    "build-published-shadow",
                    status="complete",
                    reason_code="none",
                    elapsed_ms=12,
                ),
                "external_network_used": False,
            }
            bootstrap.atomic_json(
                final / bootstrap.CROSS_DOCUMENT_SHADOW_RUN_STATE,
                complete,
            )

            with mock.patch.object(
                bootstrap, "_pid_is_alive", return_value=False
            ):
                report = bootstrap.recover_interrupted_build()

            self.assertEqual("recovered_published_shadow", report["status"])
            self.assertEqual("complete", report["shadow_status"])
            self.assertEqual(b"completed-shadow", graph.read_bytes())
            runtime_state = bootstrap.load_json(bootstrap.STATE)
            self.assertEqual("ready", runtime_state["phase"])
            self.assertEqual(
                complete,
                runtime_state["cross_document_semantic_graph_shadow"],
            )

    def test_cross_document_shadow_disabled_creates_no_artifact(self) -> None:
        bootstrap = load_module(
            "runtime_bootstrap_shadow_disabled", ROOT / "app" / "bootstrap.py"
        )
        with TemporaryDirectory() as temporary:
            generation = Path(temporary) / ("generation-" + "c" * 32)
            generation.mkdir()
            semantic, security = prepare_shadow_inputs(bootstrap, generation)
            log = io.StringIO()
            with mock.patch.object(bootstrap, "run_shadow_command") as runner:
                state = bootstrap.run_cross_document_semantic_graph_shadow(
                    {bootstrap.CROSS_DOCUMENT_SHADOW_FLAG: False},
                    semantic,
                    security,
                    generation,
                    "build-disabled",
                    log,
                )

            runner.assert_not_called()
            self.assertEqual("disabled", state["status"])
            self.assertFalse(
                (generation / bootstrap.CROSS_DOCUMENT_SHADOW_DIR).exists()
            )
            self.assertFalse(state["used_for_index"])
            self.assertFalse(state["used_for_answers"])

    def test_cross_document_shadow_publishes_only_after_validation(self) -> None:
        bootstrap = load_module(
            "runtime_bootstrap_shadow_success", ROOT / "app" / "bootstrap.py"
        )
        with TemporaryDirectory() as temporary:
            base = Path(temporary)
            generation = base / ("generation-" + "d" * 32)
            generation.mkdir()
            semantic, security = prepare_shadow_inputs(bootstrap, generation)
            tools_dir = base / "tools"
            tools_dir.mkdir()
            for name in bootstrap.CROSS_DOCUMENT_SHADOW_TOOLS:
                (tools_dir / name).write_text(f"# {name}\n", encoding="utf-8")
            commands: list[list[str]] = []
            real_write_log = bootstrap._write_log

            def fail_only_after_shadow_publication(log, message):
                if "semantic graph shadow complete" in message:
                    raise OSError("completion log unavailable")
                return real_write_log(log, message)

            def fake_shadow_command(command, _log, _timeout):
                commands.append(command)
                if command[1].endswith("build_cross_document_semantic_graph.py"):
                    database = Path(command[command.index("--output") + 1])
                    state_path = Path(command[command.index("--state") + 1])
                    database.write_bytes(b"validated-shadow-database")
                    bootstrap.atomic_json(state_path, {"status": "complete"})
                    return
                validation_path = Path(command[command.index("--output") + 1])
                database = Path(command[command.index("--database") + 1])
                documents = Path(command[command.index("--documents") + 1])
                evidence = Path(command[command.index("--evidence") + 1])
                source_evidence = Path(
                    command[command.index("--source-evidence") + 1]
                )
                security_state = Path(
                    command[command.index("--security-state") + 1]
                )
                bootstrap.atomic_json(validation_path, {
                    "status": "complete",
                    "question_independent": True,
                    "external_network_used": False,
                    "graph_snapshot_id": "xkgs_" + "2" * 32,
                    "logical_snapshot_sha256": "3" * 64,
                    "sqlite_sha256": bootstrap.sha256_file(database),
                    "documents_input_sha256": bootstrap.sha256_file(documents),
                    "source_evidence_input_sha256": bootstrap.sha256_file(
                        source_evidence
                    ),
                    "evidence_input_sha256": bootstrap.sha256_file(evidence),
                    "content_security_state_sha256": bootstrap.sha256_file(
                        security_state
                    ),
                    "counts": {
                        "documents": 1,
                        "source_evidence": 1,
                        "nodes": 3,
                        "edges": 1,
                        "edge_evidence": 6,
                    },
                    "relation_type_counts": {"ASSIGNED_TO": 1},
                })

            with (
                mock.patch.object(
                    bootstrap,
                    "_cross_document_shadow_tools_dir",
                    return_value=tools_dir,
                ),
                mock.patch.object(
                    bootstrap,
                    "run_shadow_command",
                    side_effect=fake_shadow_command,
                ),
                mock.patch.object(
                    bootstrap,
                    "_write_log",
                    side_effect=fail_only_after_shadow_publication,
                ),
            ):
                state = bootstrap.run_cross_document_semantic_graph_shadow(
                    {},
                    semantic,
                    security,
                    generation,
                    "build-success",
                    io.StringIO(),
                )

            final = generation / bootstrap.CROSS_DOCUMENT_SHADOW_DIR
            self.assertEqual("complete", state["status"], state)
            self.assertTrue(final.is_dir())
            self.assertFalse(
                (generation / (bootstrap.CROSS_DOCUMENT_SHADOW_DIR + ".building")).exists()
            )
            self.assertEqual(2, len(commands))
            builder_command = commands[0]
            self.assertEqual(
                semantic / "semantic-documents.jsonl",
                Path(builder_command[builder_command.index("--documents") + 1]),
            )
            self.assertEqual(
                security / "safe-answer-evidence.jsonl",
                Path(builder_command[builder_command.index("--evidence") + 1]),
            )
            self.assertNotEqual(
                final / "semantic-graph.sqlite3",
                generation / "safe-answer-index.sqlite3",
            )
            self.assertFalse(state["used_for_index"])
            self.assertFalse(state["used_for_answers"])
            self.assertEqual(
                state,
                bootstrap.load_json(final / bootstrap.CROSS_DOCUMENT_SHADOW_RUN_STATE),
            )

    def test_real_shadow_cli_pipeline_builds_validated_candidate(self) -> None:
        bootstrap = load_module(
            "runtime_bootstrap_shadow_real_pipeline",
            ROOT / "app" / "bootstrap.py",
        )
        with TemporaryDirectory() as temporary:
            generation = Path(temporary) / ("generation-" + "c" * 32)
            generation.mkdir()
            semantic, security = prepare_real_shadow_inputs(generation)

            shadow_log = generation / "shadow-test.log"
            with shadow_log.open("w+", encoding="utf-8") as log:
                state = bootstrap.run_cross_document_semantic_graph_shadow(
                    {bootstrap.CROSS_DOCUMENT_SHADOW_FLAG: True},
                    semantic,
                    security,
                    generation,
                    "build-real-pipeline",
                    log,
                )

            final = generation / bootstrap.CROSS_DOCUMENT_SHADOW_DIR
            self.assertEqual(
                "complete",
                state["status"],
                {"state": state, "log": shadow_log.read_text(encoding="utf-8")},
            )
            self.assertEqual(3, state["counts"]["nodes"])
            self.assertEqual(1, state["counts"]["edges"])
            self.assertTrue((final / "semantic-graph.sqlite3").is_file())
            self.assertTrue(
                (final / "semantic-graph-validation.json").is_file()
            )
            self.assertFalse(
                (generation / (bootstrap.CROSS_DOCUMENT_SHADOW_DIR + ".building"))
                .exists()
            )

    def test_cross_document_shadow_failure_is_held_without_graph_publication(self) -> None:
        bootstrap = load_module(
            "runtime_bootstrap_shadow_failure", ROOT / "app" / "bootstrap.py"
        )
        with TemporaryDirectory() as temporary:
            base = Path(temporary)
            generation = base / ("generation-" + "e" * 32)
            generation.mkdir()
            semantic, security = prepare_shadow_inputs(bootstrap, generation)
            tools_dir = base / "tools"
            tools_dir.mkdir()
            for name in bootstrap.CROSS_DOCUMENT_SHADOW_TOOLS:
                (tools_dir / name).write_text(f"# {name}\n", encoding="utf-8")
            with (
                mock.patch.object(
                    bootstrap,
                    "_cross_document_shadow_tools_dir",
                    return_value=tools_dir,
                ),
                mock.patch.object(
                    bootstrap,
                    "run_shadow_command",
                    side_effect=RuntimeError("shadow boom"),
                ),
            ):
                state = bootstrap.run_cross_document_semantic_graph_shadow(
                    {bootstrap.CROSS_DOCUMENT_SHADOW_FLAG: True},
                    semantic,
                    security,
                    generation,
                    "build-held",
                    io.StringIO(),
                )

            final = generation / bootstrap.CROSS_DOCUMENT_SHADOW_DIR
            self.assertEqual("held", state["status"])
            self.assertEqual(
                "shadow_generation_failed_non_gating", state["reason_code"]
            )
            self.assertTrue(final.is_dir())
            self.assertFalse((final / "semantic-graph.sqlite3").exists())
            self.assertTrue(
                (final / bootstrap.CROSS_DOCUMENT_SHADOW_RUN_STATE).is_file()
            )

    def test_shadow_command_timeout_terminates_the_observer(self) -> None:
        bootstrap = load_module(
            "runtime_bootstrap_shadow_timeout", ROOT / "app" / "bootstrap.py"
        )
        process = mock.Mock()
        process.wait.side_effect = [
            subprocess.TimeoutExpired(cmd=["python", "shadow.py"], timeout=1),
            0,
        ]
        with mock.patch.object(
            bootstrap.subprocess, "Popen", return_value=process
        ):
            with self.assertRaisesRegex(
                RuntimeError, "shadow_command_timeout:shadow.py"
            ):
                bootstrap.run_shadow_command(
                    ["python", "shadow.py"],
                    io.StringIO(),
                    timeout_seconds=1,
                )
        process.kill.assert_called_once_with()

    def test_shadow_orchestrator_exception_does_not_abort_main_index(self) -> None:
        bootstrap = load_module(
            "runtime_bootstrap_shadow_non_gating", ROOT / "app" / "bootstrap.py"
        )
        with TemporaryDirectory() as temporary:
            base = Path(temporary)
            support = base / "support"
            source = base / "source"
            source.mkdir()
            workspace = support / "data"
            bootstrap.SUPPORT = support
            bootstrap.CONFIG = support / "config.json"
            bootstrap.STATE = support / "state.json"
            bootstrap.atomic_json(bootstrap.CONFIG, {
                "source_root": str(source),
                "workspace": str(workspace),
                "embedding_model": "embeddinggemma:latest",
                "answer_model": "gemma4:12b",
                "audit_model": "gemma4:12b",
                bootstrap.CROSS_DOCUMENT_SHADOW_FLAG: True,
                "semantic_graph_shadow_path": "/stale/shadow.sqlite3",
            })

            def fake_semantic(_source, _paths, semantic, security, _log):
                semantic.mkdir(parents=True)
                security.mkdir(parents=True)
                bootstrap.atomic_json(
                    semantic / "layer1-input-manifest.json", {"paths": []}
                )
                reader_state = {"status": "complete", "limitations": {}}
                bootstrap.atomic_json(
                    semantic / "adaptive-reader-state.json", reader_state
                )
                return reader_state

            def fake_run(command, _log):
                if any(str(value).endswith("build_path_graph.py") for value in command):
                    output = Path(command[command.index("--output-dir") + 1])
                    (output / "path-source-inventory.jsonl").write_text(
                        "", encoding="utf-8"
                    )
                if any(
                    str(value).endswith("build_local_semantic_index.py")
                    for value in command
                ):
                    Path(command[command.index("--output") + 1]).write_bytes(
                        b"production-index"
                    )

            def fail_after_publication(*_args):
                published_during_shadow = bootstrap.load_json(bootstrap.CONFIG)
                state_during_shadow = bootstrap.load_json(bootstrap.STATE)
                self.assertTrue(
                    Path(published_during_shadow["index_path"]).is_file()
                )
                self.assertIn(
                    state_during_shadow["phase"],
                    {"ready", "ready_with_limits"},
                )
                self.assertEqual(
                    "pending",
                    state_during_shadow[
                        "cross_document_semantic_graph_shadow"
                    ]["status"],
                )
                raise RuntimeError("shadow implementation bug")

            real_write_log = bootstrap._write_log

            def fail_shadow_fallback_logs(log, message):
                if "semantic graph shadow" in message:
                    raise OSError("shadow log unavailable")
                return real_write_log(log, message)

            identifiers = [
                types.SimpleNamespace(hex="3" * 32),
                types.SimpleNamespace(hex="4" * 32),
            ]
            with (
                mock.patch.object(bootstrap.uuid, "uuid4", side_effect=identifiers),
                mock.patch.object(bootstrap, "run", side_effect=fake_run),
                mock.patch.object(
                    bootstrap,
                    "run_semantic_pipeline",
                    side_effect=fake_semantic,
                ),
                mock.patch.object(bootstrap, "ensure_models", return_value=[]),
                mock.patch.object(
                    bootstrap, "local_model_available", return_value=True
                ),
                mock.patch.object(
                    bootstrap,
                    "run_cross_document_semantic_graph_shadow",
                    side_effect=fail_after_publication,
                ),
                mock.patch.object(
                    bootstrap,
                    "_publish_shadow_failure_state",
                    side_effect=OSError("shadow state unavailable"),
                ),
                mock.patch.object(
                    bootstrap,
                    "_write_log",
                    side_effect=fail_shadow_fallback_logs,
                ),
            ):
                bootstrap.build_index()

            published = bootstrap.load_json(bootstrap.CONFIG)
            runtime_state = bootstrap.load_json(bootstrap.STATE)
            self.assertTrue(Path(published["index_path"]).is_file())
            self.assertEqual("ready", runtime_state["phase"])
            self.assertEqual(
                "held",
                runtime_state["cross_document_semantic_graph_shadow"]["status"],
            )
            self.assertNotIn("semantic_graph_shadow_path", published)
            marker = bootstrap.load_json(
                Path(published["index_path"]).parent / bootstrap.GENERATION_MARKER
            )
            self.assertEqual("published", marker["status"])
            self.assertEqual(
                "held", marker["cross_document_semantic_graph_shadow"]["status"]
            )

    def test_cli_only_ollama_is_started_on_loopback_with_owned_log(self) -> None:
        bootstrap = load_module("runtime_bootstrap_ollama", ROOT / "app" / "bootstrap.py")
        with TemporaryDirectory() as temporary:
            bootstrap.SUPPORT = Path(temporary) / "support"
            process = mock.Mock(pid=321)
            log = io.StringIO()
            with (
                mock.patch.object(bootstrap, "ollama_online", return_value=False),
                mock.patch.object(bootstrap, "ollama_app_bundle", return_value=None),
                mock.patch.object(
                    bootstrap, "ollama_binary", return_value="/opt/homebrew/bin/ollama"
                ),
                mock.patch.object(bootstrap, "_wait_for_ollama", return_value=True),
                mock.patch.object(bootstrap.subprocess, "Popen", return_value=process) as popen,
            ):
                bootstrap.start_ollama(log=log, timeout=1)

            args, kwargs = popen.call_args
            self.assertEqual(args[0], ["/opt/homebrew/bin/ollama", "serve"])
            self.assertEqual(kwargs["env"]["OLLAMA_HOST"], "127.0.0.1:11434")
            self.assertTrue(kwargs["start_new_session"])
            serve_log = bootstrap.SUPPORT / "logs" / "ollama-serve.log"
            self.assertTrue(serve_log.is_file())
            self.assertIn("pid=321", log.getvalue())


if __name__ == "__main__":
    unittest.main()
