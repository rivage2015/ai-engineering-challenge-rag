"""Synthetic tests for copying exactly one human-approved discovery candidate."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch


ENGINE = Path(__file__).resolve().parents[1] / 'distribution/macos-local-memory/engine'
sys.path.insert(0, str(ENGINE))
import document_locations as locations
APP = ENGINE.parent / 'app'
sys.path.insert(0, str(APP))
import source_updates as updates


def metadata_with(metadata, **changes):
    values = {name: getattr(metadata, name) for name in dir(metadata) if name.startswith('st_')}
    return SimpleNamespace(**{**values, **changes})


class ApprovedDocumentCopyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='approved-copy-test-')
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.source = self.base / 'source'
        self.source.mkdir()
        self.stage = self.base / 'private-stage'
        self.stage.mkdir(mode=0o700)
        self.destination = self.stage / 'approved.txt'

    def make_file(self, relative='docs/document.txt', content=b'synthetic original document'):
        path = self.source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def record(self, path):
        metadata = path.lstat()
        return dict(name=path.name, path=str(path), parent_path=str(path.parent),
                    extension=path.suffix.casefold(), size_bytes=metadata.st_size,
                    mtime_ns=metadata.st_mtime_ns, dev=metadata.st_dev,
                    ino=metadata.st_ino, ctime_ns=metadata.st_ctime_ns,
                    content_status='unread', reader_support='not_checked')

    def copy(self, record, **options):
        return locations.copy_approved_document(self.source, record, self.destination,
                                                user_name='owner', **options)

    def test_scan_identity_and_successful_copy_preserve_original(self):
        content = b'approved synthetic content\n' * 90000
        path = self.make_file(content=content)
        before = path.lstat()
        out = self.base / 'inventory'
        summary = locations.scan(self.source, out, user_name='owner')
        records = [json.loads(line) for line in (out / 'documents.jsonl').read_text().splitlines()]
        self.assertEqual(summary['candidate_files'], 1)
        self.assertEqual(records[0], self.record(path))
        result = self.copy(records[0])
        self.assertEqual(result, {'sha256': hashlib.sha256(content).hexdigest(), 'bytes': len(content)})
        self.assertEqual(self.destination.read_bytes(), content)
        self.assertEqual(stat.S_IMODE(self.destination.stat().st_mode), 0o600)
        self.assertEqual(path.read_bytes(), content)
        locations.path_safety._require_same(before, path.lstat())

    def test_changed_source_after_scan_is_rejected(self):
        path = self.make_file(content=b'old')
        record = self.record(path)
        path.write_bytes(b'new contents')
        with self.assertRaisesRegex(OSError, 'changed_since_discovery'):
            self.copy(record)
        self.assertFalse(self.destination.exists())
        self.assertEqual(path.read_bytes(), b'new contents')

    def test_snapshot_copy_does_not_make_old_document_mtime_look_new(self):
        path = self.make_file()
        old_time = 1600000000000000000
        os.utime(path, ns=(old_time, old_time))
        original = path.lstat()
        self.copy(self.record(path))
        self.assertEqual(original.st_mtime_ns, self.destination.stat().st_mtime_ns)
        locations.path_safety._require_same(original, path.lstat())

    def test_changed_source_with_restored_size_and_mtime_is_rejected(self):
        path = self.make_file(content=b'old')
        record = self.record(path)
        path.write_bytes(b'new')
        os.utime(path, ns=(path.stat().st_atime_ns, record['mtime_ns']))
        with self.assertRaisesRegex(OSError, 'changed_since_discovery'):
            self.copy(record)
        self.assertFalse(self.destination.exists())

    def test_replaced_inode_is_rejected(self):
        path = self.make_file()
        record = self.record(path)
        replacement = self.source / 'replacement.txt'
        replacement.write_bytes(path.read_bytes())
        os.utime(replacement, ns=(replacement.stat().st_atime_ns, record['mtime_ns']))
        os.replace(replacement, path)
        with self.assertRaisesRegex(OSError, 'changed_since_discovery'):
            self.copy(record)

    def test_leaf_symlink_swap_between_stat_and_open_cannot_read_target(self):
        path = self.make_file()
        record = self.record(path)
        outside = self.base / 'outside.txt'
        outside.write_bytes(b'outside content')
        original_open = os.open
        original_read = os.read
        read_calls = []

        def swap_before_open(name, flags, *args, **kwargs):
            if name == path.name and not flags & os.O_DIRECTORY:
                path.rename(path.with_name('preserved.txt'))
                path.symlink_to(outside)
            return original_open(name, flags, *args, **kwargs)

        def observe_read(*args):
            read_calls.append(args)
            return original_read(*args)

        with patch.object(locations.os, 'open', side_effect=swap_before_open), \
                patch.object(locations.os, 'read', side_effect=observe_read):
            with self.assertRaises(OSError):
                self.copy(record)
        self.assertEqual(read_calls, [])
        self.assertFalse(self.destination.exists())
        self.assertEqual(outside.read_bytes(), b'outside content')

    def test_ancestor_replacement_during_stream_never_returns_success(self):
        path = self.make_file()
        record = self.record(path)
        outside = self.base / 'outside'
        outside.mkdir()
        (outside / path.name).write_bytes(b'outside content')
        original_read = os.read

        def swap_during_read(*args):
            block = original_read(*args)
            path.parent.rename(self.source / 'preserved-docs')
            path.parent.symlink_to(outside, target_is_directory=True)
            return block

        with patch.object(locations.os, 'read', side_effect=swap_during_read):
            with self.assertRaises(OSError):
                self.copy(record)
        self.assertEqual((outside / path.name).read_bytes(), b'outside content')
        self.assertEqual((self.source / 'preserved-docs' / path.name).read_bytes(),
                         b'synthetic original document')

    def test_content_change_during_stream_never_returns_success(self):
        path = self.make_file(content=b'a' * (2 * 1024 * 1024))
        record = self.record(path)
        original_read = os.read

        def change_after_first_read(*args):
            block = original_read(*args)
            path.write_bytes(b'b' * (2 * 1024 * 1024))
            return block

        with patch.object(locations.os, 'read', side_effect=change_after_first_read):
            with self.assertRaisesRegex(OSError, 'changed_since_discovery'):
                self.copy(record)
        self.assertLess(self.destination.stat().st_size, record['size_bytes'])

    def test_dataless_leaf_and_ancestor_rejected_before_source_open(self):
        path = self.make_file()
        record = self.record(path)
        original_stat = os.stat
        original_open = os.open
        for flagged_name in (path.name, path.parent.name):
            with self.subTest(flagged_name=flagged_name):
                source_opens = []

                def flagged_stat(name, *args, **kwargs):
                    metadata = original_stat(name, *args, **kwargs)
                    if not isinstance(name, int) and Path(name).name == flagged_name:
                        return metadata_with(metadata, st_flags=locations.SF_DATALESS)
                    return metadata

                def observe_open(name, flags, *args, **kwargs):
                    if name == path.name and not flags & os.O_DIRECTORY:
                        source_opens.append(name)
                    return original_open(name, flags, *args, **kwargs)

                with patch.object(locations.os, 'stat', side_effect=flagged_stat), \
                        patch.object(locations.os, 'open', side_effect=observe_open):
                    with self.assertRaisesRegex(OSError, 'cloud_not_downloaded'):
                        self.copy(record)
                self.assertEqual(source_opens, [])
                self.assertFalse(self.destination.exists())

    def test_dataless_flag_after_open_is_rejected_before_read(self):
        path = self.make_file()
        record = self.record(path)
        original_fstat = os.fstat

        def flagged_fstat(descriptor):
            metadata = original_fstat(descriptor)
            if stat.S_ISREG(metadata.st_mode) and metadata.st_ino == record['ino']:
                return metadata_with(metadata, st_flags=locations.SF_DATALESS)
            return metadata

        with patch.object(locations.os, 'fstat', side_effect=flagged_fstat), \
                patch.object(locations.os, 'read', side_effect=AssertionError('content read')):
            with self.assertRaisesRegex(OSError, 'cloud_not_downloaded'):
                self.copy(record)
        self.assertFalse(self.destination.exists())

    def test_outside_root_traversal_and_mismatched_path_are_rejected(self):
        path = self.make_file()
        outside = self.base / 'outside.txt'
        outside.write_bytes(b'outside')
        original = self.record(path)
        bad_records = [self.record(outside),
                       {**original, 'path': str(self.source / '..' / 'outside.txt')},
                       {**original, 'parent_path': str(outside.parent)},
                       {**original, 'name': 'different.txt'}]
        with patch.object(locations.os, 'read', side_effect=AssertionError('content read')):
            for record in bad_records:
                with self.subTest(record=record), self.assertRaises(ValueError):
                    self.copy(record)
        self.assertFalse(self.destination.exists())

    def test_excluded_files_and_ancestors_are_rejected(self):
        for relative in ('.hidden/guide.txt', 'Library/guide.txt', 'docs/Caches/guide.txt',
                         'Tool.app/guide.txt', 'Users/other/Documents/guide.txt',
                         'docs/passwords.txt', 'docs/secrets/guide.txt', 'docs/file.icloud'):
            with self.subTest(relative=relative):
                path = self.make_file(relative)
                with patch.object(locations.os, 'read', side_effect=AssertionError('content read')):
                    with self.assertRaises((OSError, ValueError)):
                        self.copy(self.record(path))
                self.assertEqual(path.read_bytes(), b'synthetic original document')
        self.assertFalse(self.destination.exists())

    def test_other_volume_is_rejected_before_read(self):
        path = self.make_file()
        record = self.record(path)
        original_stat = os.stat

        def other_device(name, *args, **kwargs):
            metadata = original_stat(name, *args, **kwargs)
            if not isinstance(name, int) and Path(name).name == path.parent.name:
                return metadata_with(metadata, st_dev=metadata.st_dev + 1)
            return metadata

        with patch.object(locations.os, 'stat', side_effect=other_device), \
                patch.object(locations.os, 'read', side_effect=AssertionError('content read')):
            with self.assertRaisesRegex(OSError, 'other_volume'):
                self.copy(record)
        self.assertFalse(self.destination.exists())

    def test_byte_cap_rejects_before_source_read(self):
        path = self.make_file()
        with patch.object(locations.os, 'read', side_effect=AssertionError('content read')):
            with self.assertRaisesRegex(ValueError, 'byte_limit'):
                self.copy(self.record(path), max_bytes=1)
        self.assertFalse(self.destination.exists())

    def test_deadline_expiry_during_stream_returns_no_success(self):
        path = self.make_file()
        record = self.record(path)
        original_read = os.read
        clock = [0]

        def delayed_read(*args):
            block = original_read(*args)
            clock[0] = 301
            return block

        with patch.object(locations.time, 'monotonic', side_effect=lambda: clock[0]), \
                patch.object(locations.os, 'read', side_effect=delayed_read):
            with self.assertRaisesRegex(TimeoutError, 'time_limit'):
                self.copy(record)
        self.assertEqual(self.destination.stat().st_size, 0)

    def test_existing_destination_and_symlink_are_never_overwritten(self):
        path = self.make_file()
        record = self.record(path)
        self.destination.write_bytes(b'preserve stage')
        with self.assertRaises(FileExistsError):
            self.copy(record)
        self.assertEqual(self.destination.read_bytes(), b'preserve stage')
        other = self.stage / 'destination-link.txt'
        other.symlink_to(path)
        with self.assertRaises(OSError):
            locations.copy_approved_document(self.source, record, other, user_name='owner')
        self.assertEqual(path.read_bytes(), b'synthetic original document')

    def test_source_and_destination_ancestor_symlinks_are_rejected(self):
        path = self.make_file()
        record = self.record(path)
        alias = self.base / 'source-alias'
        alias.symlink_to(self.source, target_is_directory=True)
        alias_record = {**record, 'path': str(alias / 'docs/document.txt'),
                        'parent_path': str(alias / 'docs')}
        with self.assertRaises(OSError):
            locations.copy_approved_document(alias, alias_record, self.destination, user_name='owner')
        stage_alias = self.base / 'stage-alias'
        stage_alias.symlink_to(self.stage, target_is_directory=True)
        with self.assertRaises(OSError):
            locations.copy_approved_document(self.source, record, stage_alias / 'copy.txt',
                                             user_name='owner')
        self.assertFalse(self.destination.exists())

    def test_staging_directory_must_be_private(self):
        path = self.make_file()
        self.stage.chmod(0o755)
        with self.assertRaisesRegex(OSError, 'private_owned_staging_directory_required'):
            self.copy(self.record(path))
        self.assertFalse(self.destination.exists())

    def test_missing_record_identity_and_invalid_limits_fail_closed(self):
        path = self.make_file()
        record = self.record(path)
        for key in ('dev', 'ino', 'ctime_ns', 'size_bytes', 'mtime_ns'):
            missing = {name: value for name, value in record.items() if name != key}
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.copy(missing)
        for options in ({'max_bytes': 0}, {'max_bytes': True}, {'max_seconds': 0},
                        {'max_seconds': float('nan')}, {'max_seconds': float('inf')}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                self.copy(record, **options)
        self.assertFalse(self.destination.exists())

    def test_indexed_retention_preserves_hidden_and_non_document_files_by_hash(self):
        for index, relative in enumerate(('.DS_Store', '.hidden/state.bin', 'payload.bin',
                                          'docs/passwords.txt')):
            with self.subTest(relative=relative):
                content = b'already indexed bytes ' + str(index).encode()
                path = self.make_file(relative, content)
                before = path.lstat()
                digest = hashlib.sha256(content).hexdigest()
                destination = self.stage / ('retained-' + str(index))
                result = locations.copy_indexed_file(self.source, self.record(path), digest,
                                                      destination, user_name='owner')
                self.assertEqual(result, {'sha256': digest, 'bytes': len(content)})
                self.assertEqual(destination.read_bytes(), content)
                self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o600)
                self.assertEqual(path.read_bytes(), content)
                locations.path_safety._require_same(before, path.lstat())

    def test_indexed_retention_requires_valid_expected_hash_and_rejects_mismatch(self):
        path = self.make_file('payload.bin', b'indexed bytes')
        record = self.record(path)
        for digest in ('', None, 'abc', 'g' * 64, '0' * 65):
            with self.subTest(digest=digest), self.assertRaisesRegex(ValueError, 'sha256_required'):
                locations.copy_indexed_file(self.source, record, digest, self.destination,
                                            user_name='owner')
            self.assertFalse(self.destination.exists())
        with self.assertRaisesRegex(OSError, 'sha256_mismatch'):
            locations.copy_indexed_file(self.source, record, '0' * 64, self.destination,
                                        user_name='owner')
        self.assertEqual(path.read_bytes(), b'indexed bytes')
        self.assertEqual(stat.S_IMODE(self.destination.stat().st_mode), 0o600)

    def test_indexed_retention_still_rejects_symlinks_and_outside_root(self):
        outside = self.base / 'outside.bin'
        outside.write_bytes(b'outside bytes')
        digest = hashlib.sha256(outside.read_bytes()).hexdigest()
        link = self.source / 'linked.bin'
        link.symlink_to(outside)
        for record in (self.record(link), self.record(outside)):
            with self.subTest(path=record['path']), self.assertRaises((OSError, ValueError)):
                locations.copy_indexed_file(self.source, record, digest, self.destination,
                                            user_name='owner')
        self.assertFalse(self.destination.exists())
        self.assertEqual(outside.read_bytes(), b'outside bytes')

    def test_indexed_retention_still_rejects_dataless_before_read(self):
        path = self.make_file('payload.bin', b'indexed bytes')
        record = self.record(path)
        digest = hashlib.sha256(b'indexed bytes').hexdigest()
        original_stat = os.stat

        def dataless(name, *args, **kwargs):
            metadata = original_stat(name, *args, **kwargs)
            if not isinstance(name, int) and Path(name).name == path.name:
                return metadata_with(metadata, st_flags=locations.SF_DATALESS)
            return metadata

        with patch.object(locations.os, 'stat', side_effect=dataless), \
                patch.object(locations.os, 'read', side_effect=AssertionError('content read')):
            with self.assertRaisesRegex(OSError, 'cloud_not_downloaded'):
                locations.copy_indexed_file(self.source, record, digest, self.destination,
                                            user_name='owner')
        self.assertFalse(self.destination.exists())

    def test_indexed_retention_keeps_byte_caps_and_no_overwrite(self):
        path = self.make_file('payload.bin', b'indexed bytes')
        record = self.record(path)
        digest = hashlib.sha256(b'indexed bytes').hexdigest()
        with self.assertRaisesRegex(ValueError, 'byte_limit'):
            locations.copy_indexed_file(self.source, record, digest, self.destination,
                                        user_name='owner', max_bytes=1)
        self.destination.write_bytes(b'preserve destination')
        with self.assertRaises(FileExistsError):
            locations.copy_indexed_file(self.source, record, digest, self.destination,
                                        user_name='owner')
        self.assertEqual(self.destination.read_bytes(), b'preserve destination')

    def test_new_candidate_still_rejects_hidden_unsupported_and_sensitive_names(self):
        for relative in ('.DS_Store', '.hidden/guide.txt', 'payload.bin', 'docs/passwords.txt'):
            path = self.make_file(relative)
            with self.subTest(relative=relative), self.assertRaises((OSError, ValueError)):
                self.copy(self.record(path))
        self.assertFalse(self.destination.exists())


class PrivateStateHelpersTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='private-state-test-')
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.parent = self.base / 'owned'
        self.parent.mkdir(mode=0o755)
        self.outside = self.base / 'outside'
        self.outside.mkdir(mode=0o755)

    def test_private_directory_changes_only_final_mode(self):
        previous = stat.S_IMODE(self.parent.stat().st_mode)
        target = self.parent / 'new' / 'private'
        self.assertEqual(updates.private_directory(target), target)
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.parent.stat().st_mode), previous)
        with patch.object(updates.os, 'fchmod', side_effect=AssertionError('root mode write')):
            with self.assertRaises(ValueError):
                updates.private_directory(Path('/'))

    def test_private_directory_refuses_ancestor_swap_before_open(self):
        original_open = os.open
        outside_mode = stat.S_IMODE(self.outside.stat().st_mode)

        def swap(name, flags, *args, **kwargs):
            if name == self.parent.name:
                self.parent.rename(self.base / 'preserved')
                self.parent.symlink_to(self.outside, target_is_directory=True)
            return original_open(name, flags, *args, **kwargs)

        with patch.object(updates.os, 'open', side_effect=swap):
            with self.assertRaises((OSError, ValueError)):
                updates.private_directory(self.parent / 'private')
        self.assertFalse((self.outside / 'private').exists())
        self.assertEqual(stat.S_IMODE(self.outside.stat().st_mode), outside_mode)

    def test_private_directory_fchmod_cannot_follow_last_moment_symlink(self):
        target = self.parent / 'private'
        target.mkdir(mode=0o755)
        original_fchmod = os.fchmod
        outside_mode = stat.S_IMODE(self.outside.stat().st_mode)

        def swap_before_fchmod(descriptor, mode):
            target.rename(self.parent / 'preserved-private')
            target.symlink_to(self.outside, target_is_directory=True)
            return original_fchmod(descriptor, mode)

        with patch.object(updates.os, 'fchmod', side_effect=swap_before_fchmod):
            with self.assertRaises((OSError, ValueError)):
                updates.private_directory(target)
        self.assertEqual(stat.S_IMODE(self.outside.stat().st_mode), outside_mode)
        self.assertEqual(stat.S_IMODE((self.parent / 'preserved-private').stat().st_mode), 0o700)

    def test_private_json_replaces_private_file_with_mode_0600(self):
        self.parent.chmod(0o700)
        path = self.parent / 'state.json'
        updates.private_json(path, {'sequence': 1})
        self.assertEqual(json.loads(path.read_text()), {'sequence': 1})
        updates.private_json(path, {'sequence': 2})
        self.assertEqual(json.loads(path.read_text()), {'sequence': 2})
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_private_json_refuses_non_private_parent_and_symlink_leaf(self):
        path = self.parent / 'state.json'
        with self.assertRaises(ValueError):
            updates.private_json(path, {})
        self.parent.chmod(0o700)
        original = self.outside / 'original.json'
        original.write_text('preserve outside')
        path.symlink_to(original)
        with self.assertRaises(ValueError):
            updates.private_json(path, {})
        self.assertEqual(original.read_text(), 'preserve outside')

    def test_private_json_detects_ancestor_swap_before_publish(self):
        self.parent.chmod(0o700)
        path = self.parent / 'state.json'
        path.write_text('original state')
        outside_file = self.outside / 'state.json'
        outside_file.write_text('outside state')
        original_dump = json.dump
        preserved = self.base / 'preserved'

        def swap_after_dump(*args, **kwargs):
            result = original_dump(*args, **kwargs)
            self.parent.rename(preserved)
            self.parent.symlink_to(self.outside, target_is_directory=True)
            return result

        with patch.object(updates.json, 'dump', side_effect=swap_after_dump), \
                patch.object(updates.os, 'replace', side_effect=AssertionError('publication')):
            with self.assertRaises((OSError, ValueError)):
                updates.private_json(path, {'changed': True})
        self.assertEqual(outside_file.read_text(), 'outside state')
        self.assertEqual((preserved / 'state.json').read_text(), 'original state')

    def test_private_json_replace_stays_anchored_on_last_moment_swap(self):
        self.parent.chmod(0o700)
        path = self.parent / 'state.json'
        path.write_text('original state')
        outside_file = self.outside / 'state.json'
        outside_file.write_text('outside state')
        original_replace = os.replace
        preserved = self.base / 'preserved'

        def swap_at_replace(src, dst, **kwargs):
            self.assertIn('src_dir_fd', kwargs)
            self.assertEqual(kwargs['src_dir_fd'], kwargs['dst_dir_fd'])
            self.parent.rename(preserved)
            self.parent.symlink_to(self.outside, target_is_directory=True)
            return original_replace(src, dst, **kwargs)

        with patch.object(updates.os, 'replace', side_effect=swap_at_replace):
            with self.assertRaises((OSError, ValueError)):
                updates.private_json(path, {'changed': True})
        self.assertEqual(outside_file.read_text(), 'outside state')
        self.assertEqual(json.loads((preserved / 'state.json').read_text()), {'changed': True})


if __name__ == '__main__':
    unittest.main()
