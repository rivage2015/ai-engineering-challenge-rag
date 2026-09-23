"""Synthetic folder-selection consent and activation safety regressions.

All mutable bootstrap paths are redirected to temporary fixtures. Folder
dialogs, build bodies, model operations, HTTP, and subprocesses are mocked or
forbidden; no installed app or actual document collection is touched.
"""
from __future__ import annotations

import importlib.util
import json
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "distribution/macos-local-memory/app"


def load_module(name, filename):
    spec = importlib.util.spec_from_file_location(name, APP / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


bootstrap = load_module("source_selection_test_bootstrap", "bootstrap.py")
selection = load_module("source_selection_test_subject", "source_selection.py")


class SourceSelectionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="source-selection-test-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.support = self.base / "support"
        self.support.mkdir()
        self.source = self.base / "new documents"
        self.source.mkdir()
        self.original = self.source / "Guide2026.txt"
        self.original.write_bytes(b"Synthetic instructions: ask the coordinator.\n")
        self.old_source = self.base / "old documents"
        self.old_source.mkdir()
        self.old_document = self.old_source / "Guide2025.txt"
        self.old_document.write_bytes(b"Synthetic previous instructions.\n")
        self.old_index = self.support / "data/generations/generation-old/index.sqlite"
        self.old_index.parent.mkdir(parents=True)
        self.old_index.write_bytes(b"synthetic old index, not a database")
        self.originals = {
            path: path.read_bytes()
            for path in (self.original, self.old_document, self.old_index)
        }
        for name, value in {
            "SUPPORT": self.support,
            "CONFIG": self.support / "config.json",
            "STATE": self.support / "state.json",
            "DOCUMENT_VERSION_DECISIONS": self.support / "document-version-decisions.json",
            "DOCUMENT_VERSION_REVIEW": self.support / "document-version-review.json",
        }.items():
            patcher = mock.patch.object(bootstrap, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.expected = {
            "source_root": str(self.old_source),
            "workspace": str(self.support / "data"),
            "embedding_model": "synthetic-embedding",
            "answer_model": "synthetic-answer",
            "audit_model": "synthetic-audit",
            "sequential_model_loading": False,
            "port": 9876,
            "custom_setting": {"nested": ["keep"]},
            "index_path": str(self.old_index),
            "active_generation": "generation-old",
            "source_update_origins": {"Guide2025.txt": {"path": "/synthetic/old"}},
        }
        self.generation_fields = (
            "active_generation", "path_graph_path", "semantic_path", "security_path",
            "semantic_graph_shadow_path", bootstrap.READER_GENERATION_CONTRACT_CONFIG_KEY,
            bootstrap.CROSS_DOCUMENT_STORAGE_CONFIG_KEY,
            bootstrap.CROSS_DOCUMENT_TRUST_CONFIG_KEY, bootstrap.BASE_ANSWER_INDEX_SHA256_KEY,
        )
        self.flags = (
            bootstrap.CROSS_DOCUMENT_SHADOW_FLAG,
            bootstrap.CROSS_DOCUMENT_STORAGE_FLAG,
            bootstrap.CROSS_DOCUMENT_QUERY_CANDIDATE_FLAG,
            bootstrap.CROSS_DOCUMENT_INDEPENDENT_EDGE_AUDIT_FLAG,
            bootstrap.CROSS_DOCUMENT_ANSWER_PROMOTION_FLAG,
        )
        for index, flag in enumerate(self.flags):
            self.expected[flag] = bool(index % 2)
        for field in self.generation_fields:
            self.expected.setdefault(field, {"synthetic_previous": field})
        bootstrap.atomic_json(bootstrap.CONFIG, self.expected)
        bootstrap.atomic_json(bootstrap.STATE, {"phase": "ready", "old": True})
        self.before_config = bootstrap.CONFIG.read_bytes()
        self.before_state = bootstrap.STATE.read_bytes()
        self.service = selection.SourceSelection(bootstrap)
        self.clock = self.patch(selection.time, "monotonic", return_value=100.0)
        self.picker = self.patch(selection, "choose_folder", return_value=self.source)
        self.build = self.patch(bootstrap.build_index, "__wrapped__")
        for owner, name in (
            (bootstrap, "start_ollama"), (bootstrap, "ensure_models"),
            (bootstrap, "model_names"), (bootstrap, "run"),
            (bootstrap.LOCAL_HTTP_OPENER, "open"),
            (subprocess, "run"), (subprocess, "Popen"),
        ):
            self.patch(owner, name, side_effect=AssertionError(f"live operation forbidden: {name}"))

    def patch(self, owner, name, **kwargs):
        patcher = mock.patch.object(owner, name, **kwargs)
        result = patcher.start()
        self.addCleanup(patcher.stop)
        return result

    def config(self):
        return bootstrap.load_json(bootstrap.CONFIG)

    def pick(self):
        self.service.pick()
        snapshot = self.service.snapshot()
        self.assertEqual("selected", snapshot["phase"])
        self.assertEqual(str(self.source), snapshot["path"])
        self.assertTrue(snapshot["ticket"])
        return snapshot["ticket"]

    def consume(self):
        return self.service.consume(self.pick(), confirmed=True)

    def apply(self, candidate=None):
        candidate = candidate or self.consume()
        bootstrap.apply_source_selection(
            Path(candidate["identity"]["path"]), candidate["config"], candidate["identity"]
        )

    def assert_not_activated(self):
        self.assertEqual(self.before_config, bootstrap.CONFIG.read_bytes())
        self.assertEqual(self.before_state, bootstrap.STATE.read_bytes())
        self.assertFalse((self.support / "backups").exists())
        self.build.assert_not_called()
        self.assert_originals_unchanged()

    def assert_originals_unchanged(self):
        for path, content in self.originals.items():
            self.assertEqual(content, path.read_bytes(), str(path))

    def assert_invalidated(self):
        current = self.config()
        self.assertEqual("", current["index_path"])
        self.assertEqual(str(self.source), current["source_root"])
        for field in (*self.generation_fields, "source_update_origins"):
            self.assertNotIn(field, current)

    def test_picker_selection_changes_neither_configuration_nor_build_state(self):
        self.pick()
        self.assert_not_activated()

    def test_parent_of_application_storage_is_not_a_source(self):
        self.picker.return_value = self.base
        with self.assertRaisesRegex(ValueError, "source_is_application_data"):
            self.service.pick()
        self.assert_not_activated()

    def test_configured_workspace_cannot_be_inside_selected_source(self):
        configured = {**self.expected, "workspace": str(self.source / "generated-indexes")}
        bootstrap.atomic_json(bootstrap.CONFIG, configured)
        self.before_config = bootstrap.CONFIG.read_bytes()
        with self.assertRaisesRegex(ValueError, "source_is_application_data"):
            self.apply()
        self.assert_not_activated()

    def test_selected_source_cannot_be_inside_configured_workspace(self):
        configured = {**self.expected, "workspace": str(self.base)}
        bootstrap.atomic_json(bootstrap.CONFIG, configured)
        self.before_config = bootstrap.CONFIG.read_bytes()
        with self.assertRaisesRegex(ValueError, "source_is_application_data"):
            self.apply()
        self.assert_not_activated()

    def test_dialog_cancel_changes_neither_configuration_nor_state(self):
        self.picker.return_value = None
        self.service.pick()
        snapshot = self.service.snapshot()
        self.assertEqual("cancelled", snapshot["phase"])
        self.assertEqual("", snapshot["ticket"])
        self.assert_not_activated()

    def test_explicit_cancel_invalidates_the_confirmation(self):
        ticket = self.pick()
        self.service.cancel()
        with self.assertRaisesRegex(ValueError, "source_confirmation_invalid"):
            self.service.consume(ticket, confirmed=True)
        self.assert_not_activated()

    def test_reselection_invalidates_previous_ticket(self):
        old_ticket = self.pick()
        new_ticket = self.pick()
        self.assertNotEqual(old_ticket, new_ticket)
        with self.assertRaisesRegex(ValueError, "source_confirmation_invalid"):
            self.service.consume(old_ticket, confirmed=True)
        self.service.consume(new_ticket, confirmed=True)
        self.assert_not_activated()

    def test_explicit_boolean_confirmation_required(self):
        ticket = self.pick()
        for unconfirmed in (False, None, "true", 1):
            with self.subTest(confirmed=unconfirmed):
                with self.assertRaisesRegex(ValueError, "source_confirmation_invalid"):
                    self.service.consume(ticket, confirmed=unconfirmed)
        self.assert_not_activated()

    def test_unknown_empty_and_altered_tickets_rejected(self):
        ticket = self.pick()
        for invalid in ("", "forged", ticket + "x"):
            with self.subTest(ticket=invalid):
                with self.assertRaisesRegex(ValueError, "source_confirmation_invalid"):
                    self.service.consume(invalid, confirmed=True)
        self.assert_not_activated()

    def test_ticket_expired_exactly_at_ttl_is_unusable(self):
        ticket = self.pick()
        self.clock.return_value = 100 + selection.SELECTION_TTL_SECONDS
        snapshot = self.service.snapshot()
        self.assertTrue(snapshot["expired"])
        self.assertEqual("", snapshot["ticket"])
        with self.assertRaisesRegex(ValueError, "source_confirmation_invalid"):
            self.service.consume(ticket, confirmed=True)
        self.assert_not_activated()

    def test_ticket_just_before_ttl_is_valid_but_single_use(self):
        ticket = self.pick()
        self.clock.return_value = 100 + selection.SELECTION_TTL_SECONDS - 0.01
        candidate = self.service.consume(ticket, confirmed=True)
        self.assertEqual(self.expected, candidate["config"])
        self.assertEqual("building", self.service.snapshot()["phase"])
        with self.assertRaisesRegex(ValueError, "source_confirmation_invalid"):
            self.service.consume(ticket, confirmed=True)
        self.assert_not_activated()

    def test_returned_candidate_does_not_leave_a_reusable_internal_ticket(self):
        candidate = self.consume()
        candidate["config"]["custom_setting"]["nested"].append("mutated")
        self.assertEqual(["keep"], self.config()["custom_setting"]["nested"])
        self.assertIsNone(self.service.candidate)
        self.assertEqual("", self.service.snapshot()["ticket"])

    def test_missing_configuration_does_not_open_picker_or_create_config(self):
        bootstrap.CONFIG.unlink()
        with self.assertRaisesRegex(ValueError, "source_configuration_missing"):
            self.service.pick()
        self.picker.assert_not_called()
        self.build.assert_not_called()
        self.assertFalse(bootstrap.CONFIG.exists())
        self.assertEqual(self.before_state, bootstrap.STATE.read_bytes())

    def test_failure_has_safe_error_type_and_consumed_ticket_cannot_be_reused(self):
        ticket = self.pick()
        self.service.consume(ticket, confirmed=True)
        self.service.fail(OSError("sensitive source path in detailed error"))
        snapshot = self.service.snapshot()
        self.assertEqual("error", snapshot["phase"])
        self.assertEqual("OSError", snapshot["error"])
        self.assertEqual("", snapshot["ticket"])
        with self.assertRaisesRegex(ValueError, "source_confirmation_invalid"):
            self.service.consume(ticket, confirmed=True)
        self.assert_not_activated()

    def test_completion_keeps_ticket_consumed(self):
        ticket = self.pick()
        self.service.consume(ticket, confirmed=True)
        self.service.complete()
        self.assertEqual("complete", self.service.snapshot()["phase"])
        with self.assertRaisesRegex(ValueError, "source_confirmation_invalid"):
            self.service.consume(ticket, confirmed=True)

    def test_direct_symlink_ancestor_symlink_and_regular_file_rejected(self):
        direct = self.base / "source-link"
        direct.symlink_to(self.source, target_is_directory=True)
        ancestor = self.base / "parent-link"
        ancestor.symlink_to(self.base, target_is_directory=True)
        for invalid in (direct, ancestor / self.source.name, self.original):
            with self.subTest(path=invalid):
                self.picker.return_value = invalid
                with self.assertRaisesRegex(ValueError, "source_update_directory_invalid"):
                    self.service.pick()
        self.assert_not_activated()

    def test_application_data_and_descendant_cannot_be_chosen(self):
        for invalid in (self.support, self.support / "data"):
            with self.subTest(path=invalid):
                self.picker.return_value = invalid
                with self.assertRaisesRegex(ValueError, "source_is_application_data"):
                    self.service.pick()
        self.assert_not_activated()

    def test_parent_traversal_cannot_be_chosen(self):
        self.picker.return_value = self.source / ".." / self.source.name
        with self.assertRaisesRegex(ValueError, "source_update_directory_invalid"):
            self.service.pick()
        self.assert_not_activated()

    def test_directory_replacement_after_confirmation_is_rejected(self):
        candidate = self.consume()
        moved = self.base / "original source preserved"
        self.source.rename(moved)
        self.source.mkdir()
        with self.assertRaisesRegex(RuntimeError, "source_directory_changed"):
            self.apply(candidate)
        self.build.assert_not_called()
        self.assertEqual(self.before_config, bootstrap.CONFIG.read_bytes())
        self.assertFalse((self.support / "backups").exists())
        self.assertEqual(self.originals[self.original], (moved / self.original.name).read_bytes())

    def test_path_cannot_be_replaced_by_symlink_after_confirmation(self):
        candidate = self.consume()
        moved = self.base / "original source preserved"
        self.source.rename(moved)
        self.source.symlink_to(moved, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "source_update_directory_invalid"):
            self.apply(candidate)
        self.build.assert_not_called()
        self.assertEqual(self.before_config, bootstrap.CONFIG.read_bytes())

    def test_changed_configuration_invalidates_selection(self):
        candidate = self.consume()
        changed = {**self.expected, "port": 9911}
        bootstrap.atomic_json(bootstrap.CONFIG, changed)
        with self.assertRaisesRegex(RuntimeError, "configuration_changed_before_publish"):
            self.apply(candidate)
        self.assertEqual(changed, self.config())
        self.build.assert_not_called()
        self.assertFalse((self.support / "backups").exists())

    def test_deleted_configuration_invalidates_selection(self):
        candidate = self.consume()
        bootstrap.CONFIG.unlink()
        with self.assertRaisesRegex(RuntimeError, "configuration_changed_before_publish"):
            self.apply(candidate)
        self.assertFalse(bootstrap.CONFIG.exists())
        self.build.assert_not_called()

    def test_final_compare_and_swap_rejects_configuration_race(self):
        candidate = self.consume()
        changed = {**self.expected, "port": 9922}
        original_cas = bootstrap.atomic_config_compare_and_swap

        def replace_then_publish(*args, **kwargs):
            bootstrap.atomic_json(bootstrap.CONFIG, changed)
            original_cas(*args, **kwargs)

        self.patch(bootstrap, "atomic_config_compare_and_swap", side_effect=replace_then_publish)
        with self.assertRaisesRegex(RuntimeError, "configuration_changed_before_publish"):
            self.apply(candidate)
        self.assertEqual(changed, self.config())
        self.assertEqual(self.before_state, bootstrap.STATE.read_bytes())
        self.build.assert_not_called()

    def test_activation_preserves_settings_invalidates_old_generation_and_disables_downloads(self):
        def inspect_build(*, allow_model_downloads):
            self.assertFalse(allow_model_downloads)
            self.assert_invalidated()
            with self.assertRaisesRegex(RuntimeError, "build_already_running"):
                with bootstrap.build_execution_lease():
                    self.fail("build lease released before rebuild")
            preserved = set(self.expected) - set(self.generation_fields) - {
                "source_root", "source_update_origins", "index_path"
            }
            current = self.config()
            for key in preserved:
                self.assertEqual(self.expected[key], current[key], key)

        self.build.side_effect = inspect_build
        self.apply()
        self.build.assert_called_once_with(allow_model_downloads=False)
        self.assert_originals_unchanged()
        self.assert_invalidated()
        with bootstrap.build_execution_lease():
            pass

    def test_missing_model_and_feature_preferences_are_not_silently_added(self):
        for key in (*self.flags, "model_profile"):
            self.expected.pop(key, None)
        bootstrap.atomic_json(bootstrap.CONFIG, self.expected)
        self.apply()
        for key in (*self.flags, "model_profile"):
            self.assertNotIn(key, self.config())
        self.build.assert_called_once_with(allow_model_downloads=False)

    def test_private_original_configuration_backup_precedes_activation(self):
        original_cas = bootstrap.atomic_config_compare_and_swap

        def inspect_backup(*args, **kwargs):
            backups = list((self.support / "backups").glob("source-selection-*/config.json"))
            self.assertEqual(1, len(backups))
            self.assertEqual(self.expected, json.loads(backups[0].read_text()))
            self.assertEqual(0o700, stat.S_IMODE(backups[0].parent.stat().st_mode))
            self.assertEqual(0o600, stat.S_IMODE(backups[0].stat().st_mode))
            return original_cas(*args, **kwargs)

        self.patch(bootstrap, "atomic_config_compare_and_swap", side_effect=inspect_backup)
        self.apply()
        self.assert_originals_unchanged()

    def test_backup_failure_does_not_invalidate_old_configuration(self):
        candidate = self.consume()
        self.patch(bootstrap, "atomic_json", side_effect=OSError("synthetic backup failure"))
        with self.assertRaisesRegex(OSError, "synthetic backup failure"):
            self.apply(candidate)
        self.assertEqual(self.before_config, bootstrap.CONFIG.read_bytes())
        self.assertEqual(self.before_state, bootstrap.STATE.read_bytes())
        self.build.assert_not_called()
        self.assert_originals_unchanged()

    def test_concurrent_build_rejected_before_config_or_backup_changes(self):
        candidate = self.consume()
        with bootstrap.build_execution_lease():
            with self.assertRaisesRegex(RuntimeError, "build_already_running"):
                self.apply(candidate)
        self.assert_not_activated()

    def test_failed_new_build_does_not_fall_back_to_old_index(self):
        self.build.side_effect = RuntimeError("synthetic extraction failure")
        with self.assertRaisesRegex(RuntimeError, "synthetic extraction failure"):
            self.apply()
        self.assert_invalidated()
        self.assert_originals_unchanged()
        state = bootstrap.load_json(bootstrap.STATE)
        self.assertEqual("error", state["phase"])
        self.assertIn("synthetic extraction failure", state["error"])
        self.assertIn("旧資料には戻していません", state["message"])
        self.build.assert_called_once_with(allow_model_downloads=False)
        with bootstrap.build_execution_lease():
            pass

    def test_state_write_failure_also_leaves_old_generation_invalid(self):
        candidate = self.consume()
        real_atomic = bootstrap.atomic_json
        state_calls = 0

        def fail_first_state_write(path, value):
            nonlocal state_calls
            if path == bootstrap.STATE:
                state_calls += 1
                if state_calls == 1:
                    raise OSError("synthetic state write failure")
            return real_atomic(path, value)

        self.patch(bootstrap, "atomic_json", side_effect=fail_first_state_write)
        with self.assertRaisesRegex(OSError, "synthetic state write failure"):
            self.apply(candidate)
        self.assert_invalidated()
        self.build.assert_not_called()
        self.assertEqual("error", bootstrap.load_json(bootstrap.STATE)["phase"])
        self.assert_originals_unchanged()


class NativePickerTests(unittest.TestCase):
    def test_picker_uses_fixed_script_without_shell_or_interpolated_path(self):
        path = "/synthetic/Documents/日本語 folder "
        with mock.patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 0, path + "\n", "")) as run:
            self.assertEqual(Path(path), selection.choose_folder())
        run.assert_called_once_with(
            ["/usr/bin/osascript", "-e", selection.PICKER_SCRIPT],
            capture_output=True, text=True, check=True, timeout=120,
        )

    def test_picker_cancel_is_none(self):
        with mock.patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "\n", "")):
            self.assertIsNone(selection.choose_folder())

    def test_picker_timeout_is_not_a_successful_selection(self):
        with mock.patch.object(subprocess, "run", side_effect=subprocess.TimeoutExpired("osascript", 120)):
            with self.assertRaises(subprocess.TimeoutExpired):
                selection.choose_folder()


if __name__ == "__main__":
    unittest.main()
