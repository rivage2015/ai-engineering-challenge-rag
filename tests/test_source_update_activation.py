"""Human-approved source activation through the real, synthetic build harness.

All configuration and source paths are private temporary fixtures. The reused
harness forbids real HTTP, model inference, model launch, and subprocesses.
"""
from __future__ import annotations

import contextlib
import importlib.util
import sqlite3
import stat
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "distribution/macos-local-memory"
SPEC = importlib.util.spec_from_file_location(
    "source_update_activation_fixture",
    PACKAGE / "tests/test_versioned_safe_index_e2e.py",
)
fixture = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(fixture)


class SourceUpdateActivationTests(unittest.TestCase):
    def setUp(self):
        self.h = fixture.VersionedSafeIndexE2E()
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)
        self.bootstrap = self.h.bootstrap
        self.staged = self.h.base / "staged-source"
        self.staged.mkdir(mode=0o700)
        (self.staged / "Guide.csv").write_text(
            "task,owner\nnew,bob\n", encoding="utf-8"
        )
        self.origins = {
            "Guide.csv": {"path": "/synthetic/original/Guide2026.csv", "size": 19}
        }

    def config(self):
        return self.bootstrap.load_json(self.bootstrap.CONFIG)

    def installed_models(self):
        config = self.config()
        return {
            config["embedding_model"], config["answer_model"],
            config["audit_model"], self.bootstrap.IMAGE_FALLBACK_MODEL,
        }

    def assert_build_lease_held(self):
        with self.assertRaisesRegex(RuntimeError, "build_already_running"):
            with self.bootstrap.build_execution_lease():
                self.fail("activation released the build lease")

    def assert_invalidated(self, config):
        self.assertEqual("", config["index_path"])
        for key in (
            "active_generation", "path_graph_path", "semantic_path",
            "security_path", "semantic_graph_shadow_path",
            self.bootstrap.READER_GENERATION_CONTRACT_CONFIG_KEY,
            self.bootstrap.CROSS_DOCUMENT_STORAGE_CONFIG_KEY,
            self.bootstrap.CROSS_DOCUMENT_TRUST_CONFIG_KEY,
            self.bootstrap.BASE_ANSWER_INDEX_SHA256_KEY,
        ):
            self.assertNotIn(key, config)

    def publish_original(self):
        self.h.seed()
        self.bootstrap.build_index()
        return self.config()

    def test_stale_confirmation_preserves_configuration_without_build(self):
        expected = self.config()
        newer = {**expected, "port": 9991}
        self.bootstrap.atomic_json(self.bootstrap.CONFIG, newer)
        before = self.bootstrap.CONFIG.read_bytes()
        with mock.patch.object(self.bootstrap.build_index, "__wrapped__") as build:
            with self.assertRaisesRegex(RuntimeError, "configuration_changed"):
                self.bootstrap.apply_source_update(self.staged, expected, self.origins)
        build.assert_not_called()
        self.assertEqual(before, self.bootstrap.CONFIG.read_bytes())
        self.assertFalse((self.bootstrap.SUPPORT / "backups").exists())

    def test_activation_preserves_flags_models_workspace_and_saves_private_backup(self):
        expected = self.publish_original()
        expected.update({
            "answer_model": "qwen3.5:9b", "audit_model": "gemma4:12b",
            "sequential_model_loading": False, "port": 9876,
            "user_custom_setting": {"keep": [1, 2]},
        })
        expected.pop("model_profile", None)
        self.bootstrap.atomic_json(self.bootstrap.CONFIG, expected)
        old_index = Path(expected["index_path"])
        old_index_bytes = old_index.read_bytes()
        original_cas = self.bootstrap.atomic_config_compare_and_swap

        def checked_cas(*args, **kwargs):
            self.assert_build_lease_held()
            backups = list((self.bootstrap.SUPPORT / "backups").glob("source-update-*/config.json"))
            self.assertEqual(1, len(backups))
            self.assertEqual(expected, self.bootstrap.load_json(backups[0]))
            return original_cas(*args, **kwargs)

        def checked_build(*, allow_model_downloads):
            self.assertFalse(allow_model_downloads)
            self.assert_build_lease_held()
            updated = self.config()
            self.assert_invalidated(updated)
            for key in (
                "workspace", "embedding_model", "answer_model", "audit_model",
                "sequential_model_loading", "port", "user_custom_setting",
                self.bootstrap.CROSS_DOCUMENT_SHADOW_FLAG,
                self.bootstrap.CROSS_DOCUMENT_STORAGE_FLAG,
                self.bootstrap.CROSS_DOCUMENT_QUERY_CANDIDATE_FLAG,
                self.bootstrap.CROSS_DOCUMENT_INDEPENDENT_EDGE_AUDIT_FLAG,
                self.bootstrap.CROSS_DOCUMENT_ANSWER_PROMOTION_FLAG,
            ):
                self.assertEqual(expected[key], updated[key], key)
            self.assertNotIn("model_profile", updated)
            self.assertEqual(str(self.staged), updated["source_root"])
            self.assertEqual(self.origins, updated["source_update_origins"])

        with mock.patch.object(self.bootstrap, "atomic_config_compare_and_swap", side_effect=checked_cas), \
             mock.patch.object(self.bootstrap.build_index, "__wrapped__", side_effect=checked_build) as build:
            self.bootstrap.apply_source_update(self.staged, expected, self.origins)
        build.assert_called_once_with(allow_model_downloads=False)
        backup = next((self.bootstrap.SUPPORT / "backups").glob("source-update-*/config.json"))
        self.assertEqual(0o700, stat.S_IMODE(backup.parent.stat().st_mode))
        self.assertEqual(0o600, stat.S_IMODE(backup.stat().st_mode))
        self.assertEqual(old_index_bytes, old_index.read_bytes())
        with self.bootstrap.build_execution_lease():
            pass

    def test_real_build_keeps_all_existing_validation_gates_and_never_pulls(self):
        expected = self.publish_original()
        self.h.commands.clear()
        with mock.patch.object(self.bootstrap, "start_ollama") as start, \
             mock.patch.object(self.bootstrap, "model_names", return_value=self.installed_models()), \
             mock.patch.object(self.bootstrap, "ensure_models", side_effect=AssertionError("pull forbidden")) as pull:
            self.bootstrap.apply_source_update(self.staged, expected, self.origins)
        start.assert_called_once()
        pull.assert_not_called()
        updated = self.config()
        self.assertNotEqual(expected["active_generation"], updated["active_generation"])
        self.assertEqual(expected["workspace"], updated["workspace"])
        self.assertEqual(self.origins, updated["source_update_origins"])
        self.assertEqual("current", self.bootstrap.reader_generation_contract_status(updated)["state"])
        commands = [Path(command[1]).name for command in self.h.commands]
        for required in (
            "build_path_graph.py", "validate_path_graph.py",
            "document_version_resolver.py", "build_adaptive_semantic_graph.py",
            "validate_adaptive_semantic_graph.py", "content_security_gate.py",
            "validate_content_security_gate.py", "build_local_semantic_index.py",
        ):
            self.assertIn(required, commands)
        with contextlib.closing(sqlite3.connect(updated["index_path"])) as connection:
            paths = {row[0] for row in connection.execute("SELECT relative_path FROM evidence")}
        self.assertEqual({"Guide.csv"}, paths)
        self.assertTrue(Path(expected["index_path"]).is_file())

    def test_build_failure_leaves_old_index_invalid_and_preserves_old_files(self):
        expected = self.publish_original()
        old_index = Path(expected["index_path"])
        old_bytes = old_index.read_bytes()
        with mock.patch.object(self.bootstrap, "run", side_effect=RuntimeError("synthetic_validation_failure")):
            with self.assertRaisesRegex(RuntimeError, "synthetic_validation_failure"):
                self.bootstrap.apply_source_update(self.staged, expected, self.origins)
        updated = self.config()
        self.assert_invalidated(updated)
        self.assertEqual(str(self.staged), updated["source_root"])
        self.assertEqual(old_bytes, old_index.read_bytes())
        self.assertEqual("error", self.bootstrap.load_json(self.bootstrap.STATE)["phase"])

    def test_update_does_not_migrate_missing_feature_flags(self):
        expected = self.publish_original()
        flags = (
            self.bootstrap.CROSS_DOCUMENT_SHADOW_FLAG,
            self.bootstrap.CROSS_DOCUMENT_STORAGE_FLAG,
            self.bootstrap.CROSS_DOCUMENT_QUERY_CANDIDATE_FLAG,
            self.bootstrap.CROSS_DOCUMENT_INDEPENDENT_EDGE_AUDIT_FLAG,
            self.bootstrap.CROSS_DOCUMENT_ANSWER_PROMOTION_FLAG,
        )
        for flag in flags:
            expected.pop(flag, None)
        self.bootstrap.atomic_json(self.bootstrap.CONFIG, expected)
        with mock.patch.object(self.bootstrap, "start_ollama"), \
             mock.patch.object(self.bootstrap, "model_names", return_value=self.installed_models()), \
             mock.patch.object(self.bootstrap, "ensure_models", side_effect=AssertionError("pull forbidden")):
            self.bootstrap.apply_source_update(self.staged, expected, self.origins)
        for flag in flags:
            self.assertNotIn(flag, self.config())

    def test_missing_installed_model_stops_without_model_pull_or_index_publication(self):
        expected = self.publish_original()
        self.h.commands.clear()
        with mock.patch.object(self.bootstrap, "start_ollama") as start, \
             mock.patch.object(self.bootstrap, "model_names", return_value=set()) as models, \
             mock.patch.object(self.bootstrap, "ensure_models", side_effect=AssertionError("pull forbidden")) as pull:
            with self.assertRaisesRegex(RuntimeError, "model_downloads_disabled_missing:synthetic-embedding"):
                self.bootstrap.apply_source_update(self.staged, expected, self.origins)
        start.assert_called_once()
        models.assert_called_once()
        pull.assert_not_called()
        self.assert_invalidated(self.config())
        self.assertFalse(any(Path(command[1]).name == "build_local_semantic_index.py" for command in self.h.commands))
        self.assertEqual([], self.h.model_requests)

    def test_direct_and_ancestor_symlinks_and_files_are_rejected_before_activation(self):
        expected = self.config()
        before = self.bootstrap.CONFIG.read_bytes()
        direct = self.h.base / "linked-source"
        direct.symlink_to(self.staged, target_is_directory=True)
        parent = self.h.base / "linked-parent"
        parent.symlink_to(self.h.base, target_is_directory=True)
        for invalid in (direct, parent / self.staged.name, self.staged / "Guide.csv"):
            with self.subTest(path=invalid):
                with self.assertRaisesRegex(ValueError, "source_update_directory_invalid"):
                    self.bootstrap.apply_source_update(invalid, expected, self.origins)
                self.assertEqual(before, self.bootstrap.CONFIG.read_bytes())

    def test_existing_source_cannot_be_reactivated_as_fresh_staging(self):
        expected = self.config()
        with self.assertRaisesRegex(ValueError, "source_update_requires_fresh_directory"):
            self.bootstrap.apply_source_update(self.h.source, expected, self.origins)
        self.assertEqual(expected, self.config())

    def test_concurrent_build_blocks_activation_and_preserves_configuration(self):
        expected = self.config()
        before = self.bootstrap.CONFIG.read_bytes()
        with self.bootstrap.build_execution_lease():
            with self.assertRaisesRegex(RuntimeError, "build_already_running"):
                self.bootstrap.apply_source_update(self.staged, expected, self.origins)
        self.assertEqual(before, self.bootstrap.CONFIG.read_bytes())
        self.assertFalse((self.bootstrap.SUPPORT / "backups").exists())

    def test_configuration_race_is_rejected_by_final_compare_and_swap(self):
        expected = self.config()
        changed = {**expected, "port": 9911}
        original_cas = self.bootstrap.atomic_config_compare_and_swap

        def race(*args, **kwargs):
            self.bootstrap.atomic_json(self.bootstrap.CONFIG, changed)
            return original_cas(*args, **kwargs)

        with mock.patch.object(self.bootstrap, "atomic_config_compare_and_swap", side_effect=race), \
             mock.patch.object(self.bootstrap.build_index, "__wrapped__") as build:
            with self.assertRaisesRegex(RuntimeError, "configuration_changed_before_publish"):
                self.bootstrap.apply_source_update(self.staged, expected, self.origins)
        build.assert_not_called()
        self.assertEqual(changed, self.config())

    def test_backup_failure_cannot_invalidate_configuration(self):
        expected = self.config()
        before = self.bootstrap.CONFIG.read_bytes()
        with mock.patch.object(self.bootstrap, "atomic_json", side_effect=OSError("synthetic_backup_failure")), \
             mock.patch.object(self.bootstrap.build_index, "__wrapped__") as build:
            with self.assertRaisesRegex(OSError, "synthetic_backup_failure"):
                self.bootstrap.apply_source_update(self.staged, expected, self.origins)
        build.assert_not_called()
        self.assertEqual(before, self.bootstrap.CONFIG.read_bytes())


if __name__ == "__main__":
    unittest.main()
