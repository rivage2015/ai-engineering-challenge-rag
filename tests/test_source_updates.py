"""Synthetic F2 consent, freshness, and immutable-source integration tests.

No production configuration, documents, model calls, or live scans are used.
The discovery/copy implementation is real; publication is a recording fake.
"""
from __future__ import annotations

import builtins
import copy
from contextlib import contextmanager
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "distribution" / "macos-local-memory" / "app"
ENGINE = APP.parent / "engine"
SPEC = importlib.util.spec_from_file_location("source_updates_test_subject", APP / "source_updates.py")
updates = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(updates)
locations = updates.locations_module(ENGINE)
RESOLVER_SPEC = importlib.util.spec_from_file_location(
    "source_updates_test_resolver", ENGINE / "document_version_resolver.py"
)
resolver = importlib.util.module_from_spec(RESOLVER_SPEC)
sys.modules[RESOLVER_SPEC.name] = resolver
RESOLVER_SPEC.loader.exec_module(resolver)


class FakeBootstrap:
    def __init__(self, support, source):
        self.SUPPORT = support
        self.ENGINE = ENGINE
        self.config = {"source_root": str(source), "workspace": str(support / "data"),
                       "active_generation": "generation-test", "answer_model": "local-model"}
        self.generation = support / "data" / "generations" / "generation-test"
        self.inventory = self.generation / "01-path" / "path-source-inventory.jsonl"
        self.inventory.parent.mkdir(parents=True)
        self.context = {}
        self.validation_requests = []
        self.validation_error = None
        self.apply_source_update = Mock()

    def load_config_snapshot(self):
        return True, copy.deepcopy(self.config)

    def current_document_version_review_context(self, *, validate_source=False):
        self.validation_requests.append(validate_source)
        if validate_source and self.validation_error:
            raise self.validation_error
        return copy.deepcopy(self.context)

    def _generation_path(self, workspace, generation):
        return self.generation if generation == "generation-test" else None

    def _decision_resolver(self):
        return resolver

    def publish_test_inventory(self, records):
        raw = "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records).encode()
        self.inventory.write_bytes(raw)
        self.context = {"base_revision": {"inventory_sha256": hashlib.sha256(raw).hexdigest()}}


class SourceUpdatesTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="source-updates-test-")
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        self.pc = self.base / "pc"
        self.pc.mkdir()
        self.source = self.base / "existing-managed-source"
        self.source.mkdir()
        self.support = self.base / "support"
        self.bootstrap = FakeBootstrap(self.support, self.source)
        self.records = []
        self.old = self.index_file("procedures/Loan2025.txt", b"original procedure")
        self.unrelated = self.index_file("Other.txt", b"unrelated source")
        self.candidate = self.document("Documents/Loan2026.txt", b"candidate procedure")
        self.service = self.new_service()

    def new_service(self, **kwargs):
        return updates.SourceUpdates(self.bootstrap, root=self.pc, user_name="owner", **kwargs)

    def document(self, relative, content):
        path = self.pc / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def index_file(self, relative, content):
        path = self.source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        observed = path.stat()
        self.records.append({"relative_path": relative, "kind": "file", "size_bytes": observed.st_size,
                             "mtime_ns": observed.st_mtime_ns,
                             "sha256": hashlib.sha256(content).hexdigest()})
        self.bootstrap.publish_test_inventory(self.records)
        return path

    def scan(self, service=None):
        service = service or self.service
        service.reserve_scan()
        service.scan_reserved()
        return service.snapshot()

    def ticket(self):
        snapshot = self.scan()
        self.assertEqual(snapshot["phase"], "complete", snapshot)
        self.assertEqual(len(snapshot["candidates"]), 1, snapshot)
        return snapshot["candidates"][0]["ticket"]

    def adopt(self):
        item = self.service.reserve_adoption(self.ticket(), confirmed=True)
        self.service.adopt_reserved(item)
        return self.service.snapshot()

    @contextmanager
    def forbid_candidate_content_reads(self):
        builtin_open, io_open, os_open = builtins.open, io.open, os.open
        directory_fds = {}

        def absolute(path, dir_fd=None):
            if isinstance(path, int):
                return directory_fds.get(path)
            result = Path(os.fsdecode(path))
            if not result.is_absolute() and dir_fd in directory_fds:
                result = directory_fds[dir_fd] / result
            return Path(os.path.abspath(result))

        def file_open(original, path, *args, **kwargs):
            target = absolute(path)
            if target is not None and target.is_relative_to(self.pc):
                raise AssertionError(f"Unapproved candidate contents opened: {target}")
            return original(path, *args, **kwargs)

        def fd_open(path, flags, *args, **kwargs):
            target = absolute(path, kwargs.get("dir_fd"))
            if target is not None and target.is_relative_to(self.pc):
                self.assertTrue(flags & os.O_DIRECTORY, f"Unapproved regular file open: {target}")
            descriptor = os_open(path, flags, *args, **kwargs)
            if flags & os.O_DIRECTORY:
                directory_fds[descriptor] = target
            return descriptor

        with patch("builtins.open", side_effect=lambda p, *a, **kw: file_open(builtin_open, p, *a, **kw)), \
             patch("io.open", side_effect=lambda p, *a, **kw: file_open(io_open, p, *a, **kw)), \
             patch("os.open", side_effect=fd_open):
            yield

    def test_metadata_discovery_never_reads_candidate_contents_or_publishes(self):
        before = self.bootstrap.load_config_snapshot()
        with self.forbid_candidate_content_reads():
            result = self.scan()
        self.assertEqual(result["phase"], "complete", result)
        self.assertTrue(result["summary"]["metadata_only"])
        self.assertFalse(result["summary"]["content_read"])
        self.assertFalse(result["summary"]["answer_evidence"])
        self.assertEqual(len(result["candidates"]), 1)
        self.assertEqual(self.bootstrap.load_config_snapshot(), before)
        self.bootstrap.apply_source_update.assert_not_called()

    def test_filename_family_across_folders_is_a_proposal_not_automatic_replacement(self):
        older = self.document("Archive/Loan2024.txt", b"older version")
        self.document("Documents/Unrelated2026.txt", b"unrelated document")
        result = self.scan()
        paths = {entry["record"]["path"] for entry in result["candidates"]}
        self.assertEqual(paths, {str(self.candidate), str(older)})
        self.assertTrue(all(entry["indexed_path"] == str(self.old) for entry in result["candidates"]))
        self.bootstrap.apply_source_update.assert_not_called()
        self.assertEqual(self.old.read_bytes(), b"original procedure")

    def test_explicit_confirmation_is_required_before_any_copy(self):
        ticket = self.ticket()
        with patch.object(locations, "copy_approved_document") as copier:
            with self.assertRaisesRegex(ValueError, "explicit_confirmation_required"):
                self.service.reserve_adoption(ticket)
            copier.assert_not_called()
        self.assertEqual(self.service.snapshot()["phase"], "complete")
        self.bootstrap.apply_source_update.assert_not_called()

    def test_unknown_and_expired_tickets_do_not_authorize_adoption(self):
        ticket = self.ticket()
        with self.assertRaisesRegex(ValueError, "candidate_unknown"):
            self.service.reserve_adoption("forged-ticket", confirmed=True)
        self.service.expires = 0
        with self.assertRaisesRegex(ValueError, "confirmation_expired"):
            self.service.reserve_adoption(ticket, confirmed=True)
        self.bootstrap.apply_source_update.assert_not_called()

    def test_new_scan_invalidates_old_confirmation_ticket(self):
        ticket = self.ticket()
        self.scan()
        with self.assertRaisesRegex(ValueError, "candidate_unknown"):
            self.service.reserve_adoption(ticket, confirmed=True)
        self.bootstrap.apply_source_update.assert_not_called()

    def test_configuration_change_before_confirmation_rejects_ticket(self):
        ticket = self.ticket()
        self.bootstrap.config["answer_model"] = "another-local-model"
        with self.assertRaisesRegex(ValueError, "configuration_changed"):
            self.service.reserve_adoption(ticket, confirmed=True)
        self.bootstrap.apply_source_update.assert_not_called()

    def test_configuration_change_after_confirmation_blocks_publication(self):
        item = self.service.reserve_adoption(self.ticket(), confirmed=True)
        self.bootstrap.config["answer_model"] = "another-local-model"
        self.service.adopt_reserved(item)
        self.assertEqual(self.service.snapshot()["phase"], "error")
        self.assertEqual(self.service.snapshot()["error"], "update_basis_changed")
        self.bootstrap.apply_source_update.assert_not_called()

    def test_candidate_change_since_discovery_is_not_adopted(self):
        item = self.service.reserve_adoption(self.ticket(), confirmed=True)
        self.candidate.write_bytes(b"changed after confirmation")
        self.service.adopt_reserved(item)
        self.assertEqual(self.service.snapshot()["phase"], "error")
        self.bootstrap.apply_source_update.assert_not_called()
        self.assertEqual(self.old.read_bytes(), b"original procedure")

    def test_candidate_replaced_by_symlink_is_not_adopted(self):
        item = self.service.reserve_adoption(self.ticket(), confirmed=True)
        self.candidate.unlink()
        self.candidate.symlink_to(self.unrelated)
        self.service.adopt_reserved(item)
        self.assertEqual(self.service.snapshot()["phase"], "error")
        self.bootstrap.apply_source_update.assert_not_called()

    def test_candidate_replaced_with_same_size_and_mtime_is_not_adopted(self):
        item = self.service.reserve_adoption(self.ticket(), confirmed=True)
        before = self.candidate.stat()
        replacement = self.candidate.with_name("replacement.txt")
        replacement.write_bytes(b"x" * before.st_size)
        os.utime(replacement, ns=(before.st_atime_ns, before.st_mtime_ns))
        os.replace(replacement, self.candidate)
        self.service.adopt_reserved(item)
        self.assertEqual(self.service.snapshot()["phase"], "error")
        self.bootstrap.apply_source_update.assert_not_called()

    def test_dismissal_persists_only_for_the_same_candidate_identity(self):
        ticket = self.ticket()
        with self.forbid_candidate_content_reads():
            self.service.dismiss(ticket)
            repeated = self.new_service()
            self.assertEqual(self.scan(repeated)["candidates"], [])
        self.candidate.write_bytes(b"new revision after dismissal")
        changed = self.scan(repeated)
        self.assertEqual(len(changed["candidates"]), 1)
        self.assertEqual(stat.S_IMODE((self.service.store / "dismissed.json").stat().st_mode), 0o600)
        self.bootstrap.apply_source_update.assert_not_called()

    def test_partial_scan_is_not_reported_as_no_updates_complete(self):
        real_scan = locations.scan
        with patch.object(locations, "scan", side_effect=lambda root, out, **kw:
                          real_scan(root, out, max_entries=1, **kw)):
            snapshot = self.scan()
        self.assertEqual(snapshot["phase"], "partial", snapshot)
        self.assertEqual(snapshot["summary"]["status"], "partial")
        self.assertEqual(snapshot["summary"]["stopped_reason"], "entry_limit")
        self.bootstrap.apply_source_update.assert_not_called()

    def test_candidate_limit_is_reported_as_partial(self):
        self.document("Archive/Loan2024.txt", b"older version")
        with patch.object(updates, "MAX_CANDIDATES", 1):
            snapshot = self.scan()
        self.assertEqual(snapshot["phase"], "partial", snapshot)
        self.assertEqual(snapshot["summary"]["candidate_overflow"], 1)
        self.assertEqual(len(snapshot["candidates"]), 1)

    def test_existing_non_answer_files_are_preserved_not_silently_dropped(self):
        hidden = self.index_file('.DS_Store', b'finder metadata')
        binary = self.index_file('auxiliary.bin', b'other indexed bytes')
        self.adopt()
        self.assertEqual('applied', self.service.snapshot()['phase'])
        stage = self.bootstrap.apply_source_update.call_args.args[0]
        self.assertEqual(hidden.read_bytes(), (stage / '.DS_Store').read_bytes())
        self.assertEqual(binary.read_bytes(), (stage / 'auxiliary.bin').read_bytes())

    def test_string_false_is_not_an_explicit_confirmation(self):
        with self.assertRaisesRegex(ValueError, 'update_explicit_confirmation_required'):
            self.service.reserve_adoption(self.ticket(), confirmed='false')
        self.bootstrap.apply_source_update.assert_not_called()

    def test_approved_replacement_preserves_unrelated_files_and_old_originals(self):
        expected_config = copy.deepcopy(self.bootstrap.config)
        snapshot = self.adopt()
        self.assertEqual(snapshot["phase"], "applied", snapshot)
        self.bootstrap.apply_source_update.assert_called_once()
        stage, config, origins = self.bootstrap.apply_source_update.call_args.args
        self.assertEqual(config, expected_config)
        self.assertEqual((stage / "procedures" / "Loan2026.txt").read_bytes(), b"candidate procedure")
        self.assertFalse((stage / "procedures" / "Loan2025.txt").exists())
        self.assertEqual((stage / "Other.txt").read_bytes(), b"unrelated source")
        self.assertEqual(self.old.read_bytes(), b"original procedure")
        self.assertEqual(self.unrelated.read_bytes(), b"unrelated source")
        self.assertEqual(self.candidate.read_bytes(), b"candidate procedure")
        self.assertEqual(stat.S_IMODE(stage.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((stage / "procedures").stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((stage / "Other.txt").stat().st_mode), 0o600)
        origin = origins["procedures/Loan2026.txt"]
        self.assertEqual(origin["path"], str(self.candidate))
        self.assertEqual(origin["sha256"], hashlib.sha256(b"candidate procedure").hexdigest())
        self.assertIn(True, self.bootstrap.validation_requests)

    def test_existing_unrelated_content_change_is_blocked_even_with_same_size_and_mtime(self):
        item = self.service.reserve_adoption(self.ticket(), confirmed=True)
        before = self.unrelated.stat()
        self.unrelated.write_bytes(b"x" * before.st_size)
        os.utime(self.unrelated, ns=(before.st_atime_ns, before.st_mtime_ns))
        self.service.adopt_reserved(item)
        snapshot = self.service.snapshot()
        self.assertEqual(snapshot["phase"], "error")
        self.assertEqual(snapshot["error"], "update_existing_source_changed", snapshot)
        self.bootstrap.apply_source_update.assert_not_called()

    def test_existing_source_validator_rejection_blocks_replacement(self):
        item = self.service.reserve_adoption(self.ticket(), confirmed=True)
        self.bootstrap.validation_error = ValueError("source_inventory_does_not_match")
        self.service.adopt_reserved(item)
        self.assertEqual(self.service.snapshot()["phase"], "error")
        self.assertIn(True, self.bootstrap.validation_requests)
        self.bootstrap.apply_source_update.assert_not_called()

    def test_existing_destination_name_collision_does_not_overwrite_another_source(self):
        other_version = self.index_file("procedures/Loan2026.txt", b"separate approved document")
        snapshot = self.scan()
        matching = [entry for entry in snapshot["candidates"]
                    if entry["target"]["relative_path"] == "procedures/Loan2025.txt"]
        self.assertEqual(len(matching), 1)
        item = self.service.reserve_adoption(matching[0]["ticket"], confirmed=True)
        self.service.adopt_reserved(item)
        result = self.service.snapshot()
        self.assertEqual(result["phase"], "error")
        self.assertEqual(result["error"], "update_destination_collision")
        self.assertEqual(other_version.read_bytes(), b"separate approved document")
        self.bootstrap.apply_source_update.assert_not_called()

    def test_unrelated_original_tracking_is_preserved_on_single_replacement(self):
        other_origin = {"path": "/synthetic/original/Other.txt", "sha256": "unrelated-digest"}
        self.bootstrap.config["source_update_origins"] = {"Other.txt": other_origin}
        snapshot = self.adopt()
        self.assertEqual(snapshot["phase"], "applied", snapshot)
        origins = self.bootstrap.apply_source_update.call_args.args[2]
        self.assertEqual(origins["Other.txt"], other_origin)

    def test_index_inventory_tampering_rejects_discovery(self):
        self.bootstrap.inventory.write_bytes(b"{}\n")
        snapshot = self.scan()
        self.assertEqual(snapshot["phase"], "error")
        self.assertEqual(snapshot["error"], "update_inventory_changed")
        self.assertEqual(snapshot["candidates"], [])

    def test_rebuild_failure_does_not_report_applied_or_remove_old_source(self):
        self.bootstrap.apply_source_update.side_effect = RuntimeError("synthetic build failure")
        snapshot = self.adopt()
        self.bootstrap.apply_source_update.assert_called_once()
        self.assertEqual(snapshot["phase"], "error")
        self.assertEqual(snapshot["error"], "RuntimeError")
        self.assertEqual(self.old.read_bytes(), b"original procedure")
        self.assertEqual(self.unrelated.read_bytes(), b"unrelated source")
        self.assertEqual(self.candidate.read_bytes(), b"candidate procedure")

    def test_origin_tracking_suppresses_only_unchanged_previously_approved_original(self):
        snapshot = self.adopt()
        self.assertEqual(snapshot["phase"], "applied", snapshot)
        stage, _, origins = self.bootstrap.apply_source_update.call_args.args
        approved = stage / "procedures" / "Loan2026.txt"
        self.bootstrap.config.update(source_root=str(stage), source_update_origins=origins)
        approved_stat = approved.stat()
        self.bootstrap.publish_test_inventory([
            {"relative_path": "procedures/Loan2026.txt", "kind": "file",
             "size_bytes": approved_stat.st_size, "mtime_ns": approved_stat.st_mtime_ns,
             "sha256": hashlib.sha256(approved.read_bytes()).hexdigest()}
        ])
        next_service = self.new_service()
        self.assertEqual(self.scan(next_service)["candidates"], [])
        self.candidate.write_bytes(b"new original revision")
        self.assertEqual(len(self.scan(next_service)["candidates"]), 1)

    def test_only_one_operation_can_be_reserved(self):
        self.service.reserve_scan()
        with self.assertRaisesRegex(ValueError, "update_busy"):
            self.service.reserve_scan()
        self.service.scan_reserved()
        ticket = self.service.snapshot()["candidates"][0]["ticket"]
        self.service.reserve_adoption(ticket, confirmed=True)
        with self.assertRaisesRegex(ValueError, "update_busy"):
            self.service.reserve_scan()
        with self.assertRaises(ValueError):
            self.service.reserve_adoption(ticket, confirmed=True)

    def test_state_directory_symlink_is_not_followed_or_chmodded(self):
        outside = self.base / "outside-state"
        outside.mkdir(mode=0o755)
        os.chmod(outside, 0o755)
        self.service.store.symlink_to(outside, target_is_directory=True)
        snapshot = self.scan()
        self.assertEqual(snapshot["phase"], "error")
        self.assertEqual(stat.S_IMODE(outside.stat().st_mode), 0o755)
        self.assertEqual(list(outside.iterdir()), [])
        self.bootstrap.apply_source_update.assert_not_called()

    def test_dismissal_file_symlink_is_not_read_or_replaced(self):
        ticket = self.ticket()
        outside = self.base / "outside-state.json"
        outside.write_text("[]", encoding="utf-8")
        (self.service.store / "dismissed.json").symlink_to(outside)
        with self.assertRaisesRegex(ValueError, "dismissals_invalid"):
            self.service.dismiss(ticket)
        self.assertEqual(outside.read_text(encoding="utf-8"), "[]")
        self.assertTrue((self.service.store / "dismissed.json").is_symlink())


if __name__ == "__main__":
    unittest.main()
