#!/usr/bin/env python3
from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / "engine"
APP = ROOT / "app"


def load_module(name: str, path: Path):
    specification = importlib.util.spec_from_file_location(name, path)
    assert specification is not None and specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def load_bootstrap():
    module = load_module("reader_migration_bootstrap", APP / "bootstrap.py")
    # Source checkouts keep engine/ beside app/; the packaged app copies it
    # inside Resources/, where bootstrap's default resolves directly.
    module.ENGINE = ENGINE
    return module


def load_server(bootstrap):
    previous = sys.modules.get("bootstrap")
    sys.modules["bootstrap"] = bootstrap
    sys.path.insert(0, str(APP))
    try:
        return load_module("reader_migration_server", APP / "local_memory_server.py")
    finally:
        sys.path.remove(str(APP))
        if previous is None:
            sys.modules.pop("bootstrap", None)
        else:
            sys.modules["bootstrap"] = previous


class ReaderGenerationMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.bootstrap = load_bootstrap()
        self.temporary = tempfile.TemporaryDirectory(prefix="reader-migration-")
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _old_config(self) -> tuple[dict, Path, Path]:
        source = self.root / "source"
        source.mkdir(exist_ok=True)
        generation = self.root / ("generation-" + "1" * 32)
        semantic = generation / "02-semantic"
        semantic.mkdir(parents=True, exist_ok=True)
        index = generation / "safe-answer-index.sqlite3"
        index.write_bytes(b"existing-step6-index")
        return (
            {
                "source_root": str(source),
                "workspace": str(self.root),
                "active_generation": generation.name,
                "semantic_path": str(semantic),
                "index_path": str(index),
                "cross_document_semantic_graph_answer_promotion_enabled": True,
            },
            source,
            index,
        )

    def _current_config(self) -> tuple[dict, Path]:
        source = self.root / "source"
        source.mkdir(exist_ok=True)
        (source / "memo.txt").write_text(
            "Project ID: READER-CONTRACT\nWork ID: WORK-1\n",
            encoding="utf-8",
        )
        generation = self.root / ("generation-" + "2" * 32)
        generation.mkdir()
        paths = generation / "01-path"
        semantic = generation / "02-semantic"
        security = generation / "03-security"
        process = subprocess.run(
            [
                sys.executable,
                str(ENGINE / "build_path_graph.py"),
                str(source),
                "--output-dir",
                str(paths),
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(process.returncode, 0, process.stderr)
        with (self.root / "semantic-pipeline.log").open("w", encoding="utf-8") as log:
            self.bootstrap.run_semantic_pipeline(
                source,
                paths,
                semantic,
                security,
                log,
            )
        registration = self.bootstrap.write_reader_generation_contract(
            semantic,
            generation.name,
        )
        index = generation / "safe-answer-index.sqlite3"
        index.write_bytes(b"current-index")
        return (
            {
                "source_root": str(source),
                "workspace": str(self.root),
                "active_generation": generation.name,
                "semantic_path": str(semantic),
                "security_path": str(security),
                "index_path": str(index),
                self.bootstrap.READER_GENERATION_CONTRACT_CONFIG_KEY: registration,
            },
            semantic,
        )

    def test_existing_step6_config_requires_reader_migration_without_mutation(self) -> None:
        config, _source, index = self._old_config()
        before_config = copy.deepcopy(config)
        before_index = hashlib.sha256(index.read_bytes()).hexdigest()

        status = self.bootstrap.reader_generation_contract_status(config)

        self.assertEqual(status["state"], "reader_migration_required")
        self.assertEqual(
            status["reason_code"],
            "reader_generation_contract_registration_missing",
        )
        self.assertEqual(config, before_config)
        self.assertEqual(hashlib.sha256(index.read_bytes()).hexdigest(), before_index)

    def test_current_generation_matches_code_processing_and_schema_bytes(self) -> None:
        config, _semantic = self._current_config()

        status = self.bootstrap.reader_generation_contract_status(config)

        self.assertEqual(status["state"], "current")
        self.assertFalse(status["migration_required"])
        self.assertRegex(status["logical_sha256"], r"^[0-9a-f]{64}$")

    def test_missing_contract_requires_migration(self) -> None:
        config, semantic = self._current_config()
        contract_path = semantic / self.bootstrap.READER_GENERATION_CONTRACT_FILENAME
        contract_path.unlink()
        missing = self.bootstrap.reader_generation_contract_status(config)
        self.assertEqual(missing["state"], "reader_migration_required")
        self.assertEqual(missing["reason_code"], "reader_generation_contract_missing")

    def test_tampered_contract_requires_migration(self) -> None:
        config, semantic = self._current_config()
        contract_path = semantic / self.bootstrap.READER_GENERATION_CONTRACT_FILENAME
        contract = json.loads(contract_path.read_text(encoding="utf-8"))
        contract["producer_records"]["builder"]["version"] = (
            "forged-current-looking-version"
        )
        contract_path.write_text(json.dumps(contract), encoding="utf-8")

        status = self.bootstrap.reader_generation_contract_status(config)

        self.assertEqual(status["state"], "reader_migration_required")
        self.assertEqual(
            status["reason_code"], "reader_generation_contract_hash_mismatch"
        )

    def test_generation_artifact_tamper_requires_migration(self) -> None:
        config, semantic = self._current_config()
        adapter_path = semantic / "layer1-adapter" / "layer1-adapter-state.json"
        adapter = json.loads(adapter_path.read_text(encoding="utf-8"))
        adapter["adapter_version"] = "forged-current-looking-version"
        adapter_path.write_text(json.dumps(adapter), encoding="utf-8")

        status = self.bootstrap.reader_generation_contract_status(config)

        self.assertEqual(status["state"], "reader_migration_required")
        self.assertEqual(status["reason_code"], "reader_contract_stage_hash_mismatch")

    def test_builder_adapter_processing_or_schema_byte_change_requires_migration(self) -> None:
        config, _semantic = self._current_config()
        current = self.bootstrap._current_reader_resource_contract()
        mutations = {
            "builder": ("adaptive_builder", "sha256"),
            "adapter": ("adapter", "sha256"),
            "processing": ("processing_code", "probe_intermediate_records.py"),
            "schema": ("schemas", "evidence.schema.json"),
        }
        for label, (group, key) in mutations.items():
            with self.subTest(resource=label):
                changed = copy.deepcopy(current)
                if group in {"adaptive_builder", "adapter"}:
                    changed[group][key] = "f" * 64
                else:
                    changed[group][key]["sha256"] = "f" * 64
                with mock.patch.object(
                    self.bootstrap,
                    "_current_reader_resource_contract",
                    return_value=changed,
                ):
                    status = self.bootstrap.reader_generation_contract_status(config)
                self.assertEqual(status["state"], "reader_migration_required")

    def test_diagnose_keeps_old_pointer_ready_and_ui_offers_explicit_rebuild(self) -> None:
        config, _source, index = self._old_config()
        config_path = self.root / "config.json"
        config_path.write_text(json.dumps(config), encoding="utf-8")
        before_config = config_path.read_bytes()
        before_index = index.read_bytes()
        with (
            mock.patch.object(self.bootstrap, "CONFIG", config_path),
            mock.patch.object(self.bootstrap, "ollama_binary", return_value="/bin/true"),
            mock.patch.object(self.bootstrap, "ollama_online", return_value=False),
            mock.patch.object(self.bootstrap, "total_memory_gb", return_value=24.0),
            mock.patch.object(self.bootstrap, "free_gb", return_value=100.0),
        ):
            diagnosis = self.bootstrap.diagnose()

        self.assertTrue(diagnosis["index_ready"])
        self.assertTrue(diagnosis["ready"])
        self.assertTrue(diagnosis["reader_migration_required"])
        self.assertEqual(config_path.read_bytes(), before_config)
        self.assertEqual(index.read_bytes(), before_index)

        server = load_server(self.bootstrap)
        answer_path = server.semantic_graph_answer_path_status(
            diagnosis,
            {"phase": "ready"},
        )
        self.assertEqual(answer_path["state"], "reader_migration_required")
        self.assertTrue(answer_path["show_rebuild"])
        self.assertIn("現在の索引で回答", answer_path["label"])


if __name__ == "__main__":
    unittest.main()
