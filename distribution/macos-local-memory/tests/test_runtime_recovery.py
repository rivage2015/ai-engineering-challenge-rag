#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import io
import json
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


class RuntimeRecoveryTests(unittest.TestCase):
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
            ):
                bootstrap.build_index()

            self.assertEqual(len(semantic_calls), 2)
            self.assertEqual(semantic_calls[0].name, "02-semantic")
            self.assertEqual(semantic_calls[1].name, "02-semantic-model-ready")
            published = bootstrap.load_json(config_path)
            self.assertTrue(published["semantic_path"].endswith("02-semantic-model-ready"))
            self.assertTrue(published["security_path"].endswith("03-security-model-ready"))
            self.assertTrue(Path(published["index_path"]).is_file())
            self.assertEqual(bootstrap.load_json(state_path)["phase"], "ready_with_limits")

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
            self.assertTrue(generation.is_dir())
            recovered_state = bootstrap.load_json(bootstrap.STATE)
            self.assertEqual(recovered_state["phase"], "ready")
            self.assertTrue(recovered_state["recovered_after_interruption"])
            self.assertEqual(
                bootstrap.load_json(generation / bootstrap.GENERATION_MARKER)["status"],
                "published",
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
