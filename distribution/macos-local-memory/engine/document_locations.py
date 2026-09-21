#!/usr/bin/env python3
"""Private document discovery and explicitly approved staging copies.

Discovery never opens regular source files and is NOT a content index or an
attestation. Directory handles are no-follow and bound to observed identities.
Omitted/changed/unreadable regions stay explicit. The separate copy primitive
reads exactly one caller-approved candidate; it never publishes an index.
"""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import time

import build_path_graph as path_safety

# Darwin sys/stat.h SF_DATALESS: stat does not hydrate the object's contents.
SF_DATALESS = 0x40000000
SUFFIXES = set('doc docx docm xls xlsx xlsm ppt pptx pptm pdf rtf odt ods odp '
               'pages numbers key csv tsv txt md rst html htm epub json xml yaml yml '
               'png jpg jpeg tif tiff bmp gif webp heic heif avif svg'.split())
SYSTEM_TOP = {'applications', 'library', 'system', 'volumes', 'private', 'usr',
              'bin', 'sbin', 'dev', 'cores', 'opt', 'sw', 'pkg', 'mnt', 'home',
              'mobilesoftwareupdate', 'macos install data'}
SKIP_DIRS = {'applications', 'library', 'cache', 'caches', 'node_modules',
             '__pycache__', 'venv', 'trash', 'ゴミ箱'}
BUNDLES = {'.app', '.framework', '.bundle', '.plugin', '.photoslibrary', '.photolibrary'}
SENSITIVE = re.compile(r'(^|[^a-z0-9])(credentials?|secrets?|tokens?|passwords?|apikey|api[_ -]?key)($|[^a-z0-9])|パスワード|認証情報|秘密鍵', re.I)


def exclusion_reason(relative: Path, metadata, user_name: str) -> str | None:
    name = relative.name
    parts = relative.parts
    if name.startswith('.') or name.startswith('~$'):
        return 'hidden_or_temporary'
    if stat.S_ISLNK(metadata.st_mode):
        return 'symlink_not_followed'
    if getattr(metadata, 'st_flags', 0) & SF_DATALESS or name.endswith('.icloud'):
        return 'cloud_not_downloaded'
    if SENSITIVE.search(name) or name.casefold() in {'settings.local.json', 'id_rsa', 'id_ed25519'}:
        return 'credential_name'
    if len(parts) == 1 and name.casefold() in SYSTEM_TOP:
        return 'system_or_application'
    if len(parts) >= 2 and parts[0] == 'Users' and parts[1] not in {user_name, 'Shared'}:
        return 'other_user_private_area'
    if stat.S_ISDIR(metadata.st_mode):
        if name.casefold() in SKIP_DIRS or Path(name).suffix.casefold() in BUNDLES:
            return 'application_cache_or_generated'
    elif not stat.S_ISREG(metadata.st_mode):
        return 'non_regular'
    return None


@contextmanager
def _private_output_directory(out: Path):
    """Create a new output, including missing parents, without following links."""
    out = path_safety._absolute_root(out)
    if len(out.parts) < 2:
        raise FileExistsError('new_output_directory_required')
    with path_safety._root_descriptors(Path(out.anchor)) as root_bindings, ExitStack() as handles:
        bindings = list(root_bindings)

        def verify():
            for descriptor, parent_fd, name, observed in bindings:
                path_safety._require_same(observed, os.fstat(descriptor), revision=False)
                if parent_fd is not None:
                    path_safety._require_same(observed, os.stat(name, dir_fd=parent_fd,
                        follow_symlinks=False), revision=False)

        parent_fd = bindings[-1][0]
        for index, name in enumerate(out.parts[1:], start=1):
            verify()
            is_output = index == len(out.parts) - 1
            if is_output:
                # mkdir is exclusive: an existing directory or dangling link
                # cannot turn a new inventory into an overwrite.
                os.mkdir(name, mode=0o700, dir_fd=parent_fd)
            else:
                try:
                    os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                except FileNotFoundError:
                    try:
                        os.mkdir(name, mode=0o700, dir_fd=parent_fd)
                    except FileExistsError:
                        pass
            observed = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            if not stat.S_ISDIR(observed.st_mode):
                raise OSError('inventory_output_ancestor_not_directory')
            descriptor = path_safety._open_verified_directory(parent_fd, name, observed)
            handles.callback(os.close, descriptor)
            bindings.append((descriptor, parent_fd, name, observed))
            parent_fd = descriptor
            verify()
        os.fchmod(parent_fd, 0o700)

        def verify_private():
            verify()
            metadata = os.fstat(parent_fd)
            if stat.S_IMODE(metadata.st_mode) != 0o700 or metadata.st_uid != os.geteuid():
                raise OSError('inventory_output_not_private')

        verify_private()
        yield parent_fd, verify_private
        verify_private()


@contextmanager
def _private_output_file(output, name: str):
    descriptor, verify = output
    verify()
    fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                 0o600, dir_fd=descriptor)
    with os.fdopen(fd, 'w', encoding='utf-8') as stream:
        os.fchmod(stream.fileno(), 0o600)
        initial = os.fstat(stream.fileno())
        yield stream
        stream.flush()
        verify()
        current = os.fstat(stream.fileno())
        path_safety._require_same(initial, current, revision=False)
        path_safety._require_same(current, os.stat(name, dir_fd=descriptor,
                                                  follow_symlinks=False))
        if stat.S_IMODE(current.st_mode) != 0o600 or current.st_nlink != 1:
            raise OSError('inventory_output_file_changed')


def scan(root: Path, out: Path, *, user_name: str, max_entries: int = 1_000_000,
         max_seconds: float = 900, progress=None) -> dict:
    root = path_safety._absolute_root(root)
    out = path_safety._absolute_root(out)
    if max_entries < 1 or max_seconds <= 0:
        raise ValueError('positive_bounds_required')
    if out.exists():
        raise FileExistsError('new_output_directory_required')
    started = time.monotonic()
    summary = dict(schema='local-document-locations/0.1', root=str(root),
                   started_at=datetime.now(timezone.utc).isoformat(),
                   status='running', content_read=False, answer_evidence=False,
                   entries_seen=0, candidate_files=0, directories_scanned=0,
                   bytes_total=0, errors=0, stopped_reason=None)
    excluded, extensions = Counter(), Counter()
    last_progress = started
    with path_safety._root_descriptors(root) as bindings, \
            _private_output_directory(out) as output, ExitStack() as handles:
        streams = {name: handles.enter_context(_private_output_file(output, name + '.jsonl'))
                   for name in ('documents', 'directories', 'omissions')}
        def emit(stream, value):
            streams[stream].write(json.dumps(value, ensure_ascii=False) + '\n')

        def omit(relative, reason, error=False):
            excluded[reason] += 1
            emit('omissions', {'path': str(root / relative), 'reason': reason})
            if error:
                summary['errors'] += 1
                summary['status'] = 'partial'

        root_fd, _, _, root_stat = bindings[-1]
        stack = []
        def enter(fd, relative, owned, observed):
            try:
                iterator = os.scandir(fd)
            except OSError as exc:
                omit(relative, f'scandir_{type(exc).__name__}', True)
                if owned:
                    os.close(fd)
                return
            stack.append((fd, relative, owned, observed, iterator))
            summary['directories_scanned'] += 1
            emit('directories', {'path': str(root / relative),
                                  'parent_path': str((root / relative).parent),
                                  'content_status': 'metadata_only'})
        enter(root_fd, Path('.'), False, root_stat)
        try:
            while stack:
                now = time.monotonic()
                if summary['entries_seen'] >= max_entries or now - started >= max_seconds:
                    summary.update(status='partial', stopped_reason=(
                        'entry_limit' if summary['entries_seen'] >= max_entries else 'time_limit'))
                    for _, pending, _, _, _ in stack:
                        omit(pending, 'scan_interrupted_' + summary['stopped_reason'])
                    break
                if progress and now - last_progress >= 10:
                    progress({**summary, 'elapsed_seconds': round(now - started, 2)})
                    last_progress = now
                fd, relative, owned, observed, iterator = stack[-1]
                try:
                    entry = next(iterator)
                except StopIteration:
                    current = os.fstat(fd)
                    if path_safety._revision(current) != path_safety._revision(observed):
                        omit(relative, 'directory_changed_during_scan', True)
                    iterator.close()
                    if owned:
                        os.close(fd)
                    stack.pop()
                    continue
                except OSError as exc:
                    omit(relative, f'scandir_{type(exc).__name__}', True)
                    iterator.close()
                    if owned:
                        os.close(fd)
                    stack.pop()
                    continue
                summary['entries_seen'] += 1
                child = relative / entry.name
                try:
                    metadata = os.stat(entry.name, dir_fd=fd, follow_symlinks=False)
                    reason = exclusion_reason(child, metadata, user_name)
                    if reason:
                        omit(child, reason)
                        continue
                    if metadata.st_dev != root_stat.st_dev:
                        omit(child, 'other_volume')
                        continue
                    if root / child == out:
                        omit(child, 'inventory_output')
                        continue
                    if stat.S_ISDIR(metadata.st_mode):
                        if len(stack) >= 80:
                            omit(child, 'depth_limit', True)
                            continue
                        child_fd = path_safety._open_verified_directory(fd, entry.name, metadata)
                        enter(child_fd, child, True, metadata)
                        continue
                    suffix = Path(entry.name).suffix.casefold().lstrip('.')
                    if suffix not in SUFFIXES:
                        excluded['not_document_or_image'] += 1
                        continue
                    summary['candidate_files'] += 1
                    summary['bytes_total'] += metadata.st_size
                    extensions[suffix] += 1
                    emit('documents', {'name': entry.name, 'path': str(root / child),
                         'parent_path': str((root / child).parent), 'extension': '.' + suffix,
                         'size_bytes': metadata.st_size, 'mtime_ns': metadata.st_mtime_ns,
                         'dev': metadata.st_dev, 'ino': metadata.st_ino,
                         'ctime_ns': metadata.st_ctime_ns,
                         'content_status': 'unread', 'reader_support': 'not_checked'})
                except OSError as exc:
                    omit(child, f'metadata_{type(exc).__name__}', True)
        finally:
            for fd, relative, owned, observed, iterator in reversed(stack):
                iterator.close()
                if owned:
                    os.close(fd)
            # Ancestor identity checks, not content/mtime attestation.
            for descriptor, parent_fd, name, observed in bindings:
                try:
                    path_safety._require_same(observed, os.fstat(descriptor), revision=False)
                    if parent_fd is not None:
                        path_safety._require_same(observed, os.stat(name, dir_fd=parent_fd,
                            follow_symlinks=False), revision=False)
                except OSError:
                    omit(Path('.'), 'root_identity_changed', True)
        if summary['status'] == 'running':
            summary['status'] = 'complete'
        summary.update(elapsed_seconds=round(time.monotonic() - started, 3),
                       excluded_counts=dict(excluded), extensions=dict(extensions),
                       finished_at=datetime.now(timezone.utc).isoformat(),
                       limitations=['metadata_snapshot_not_current_version_guarantee',
                           'hidden_and_application_managed_areas_excluded',
                           'credential_filter_is_filename_based_not_content_inspection',
                           'unread_files_not_answer_evidence', 'archive_contents_not_enumerated'])
        with _private_output_file(output, 'summary.json') as handle:
            json.dump(summary, handle, ensure_ascii=False, indent=2)
    return summary


def copy_approved_document(root: Path, record: dict, destination: Path, *,
                           user_name: str, max_bytes: int = 512 * 1024 * 1024,
                           max_seconds: float = 300) -> dict:
    """Copy one caller-approved, server-owned discovery record into staging.

    ``root`` and ``record`` must come from the trusted completed scan, never
    directly from a browser request. This primitive does not grant consent or
    publish an index. The caller supplies an existing private staging directory
    and must discard/quarantine that stage on any exception; an incomplete file
    can remain there. Source files are opened read-only, without following links.
    """
    return _copy_bound_file(root, record, destination, user_name=user_name,
                            max_bytes=max_bytes, max_seconds=max_seconds)


def copy_indexed_file(root: Path, record: dict, expected_sha256: str, destination: Path, *,
                      user_name: str, max_bytes: int = 512 * 1024 * 1024,
                      max_seconds: float = 300) -> dict:
    """Retain a file already present in the trusted, attested source inventory.

    The caller must bind root/record/digest to the existing indexed snapshot,
    never request fields. Discovery name/extension filtering does not apply to
    retention of that snapshot. This does not authorize discovering or adopting
    new excluded files. All filesystem, identity and copy bounds still apply.
    A digest mismatch raises and leaves only unpublished private staging data.
    """
    if not isinstance(expected_sha256, str) or re.fullmatch(r'[0-9a-fA-F]{64}', expected_sha256) is None:
        raise ValueError('indexed_file_sha256_required')
    result = _copy_bound_file(root, record, destination, user_name=user_name,
                              max_bytes=max_bytes, max_seconds=max_seconds,
                              retain_indexed=True)
    if result['sha256'] != expected_sha256.lower():
        raise path_safety.SourceChangedError('indexed_file_sha256_mismatch')
    return result


def _copy_bound_file(root: Path, record: dict, destination: Path, *, user_name: str,
                     max_bytes: int, max_seconds: float, retain_indexed: bool = False) -> dict:
    """Shared fd-bound mechanics; policy is selected only by the public wrappers."""
    if (not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or max_bytes < 1
            or not isinstance(max_seconds, (int, float)) or isinstance(max_seconds, bool)
            or not math.isfinite(max_seconds) or max_seconds <= 0):
        raise ValueError('positive_finite_copy_bounds_required')
    started = time.monotonic()

    def check_deadline():
        if time.monotonic() - started >= max_seconds:
            raise TimeoutError('approved_document_copy_time_limit')

    root = path_safety._absolute_root(Path(root))
    if not isinstance(record, dict) or not isinstance(record.get('path'), str):
        raise ValueError('trusted_discovery_record_required')
    source = Path(record['path'])
    if (not source.is_absolute() or '..' in source.parts
            or str(source) != record['path']):
        raise ValueError('candidate_path_must_be_absolute_without_traversal')
    try:
        relative = source.relative_to(root)
    except ValueError:
        raise ValueError('candidate_outside_discovery_root') from None
    if (not relative.parts or record.get('name') != source.name
            or record.get('parent_path') != str(source.parent)
            or record.get('extension') != source.suffix.casefold()
            or (not retain_indexed and source.suffix.casefold().lstrip('.') not in SUFFIXES)):
        raise ValueError('candidate_path_metadata_mismatch')
    identity_fields = ('size_bytes', 'mtime_ns', 'dev', 'ino', 'ctime_ns')
    if any(not isinstance(record.get(key), int) or isinstance(record.get(key), bool)
           for key in identity_fields) or record['size_bytes'] < 0:
        raise ValueError('candidate_identity_metadata_required')
    if record['size_bytes'] > max_bytes:
        raise ValueError('approved_document_copy_byte_limit')
    expected_record = tuple(record[key] for key in identity_fields)
    destination = path_safety._absolute_root(Path(destination))

    def require_record(metadata):
        observed_record = (metadata.st_size, metadata.st_mtime_ns, metadata.st_dev,
                           metadata.st_ino, metadata.st_ctime_ns)
        if expected_record != observed_record:
            raise path_safety.SourceChangedError('candidate_changed_since_discovery')

    with ExitStack() as handles:
        source_bindings = handles.enter_context(path_safety._root_descriptors(root))
        root_fd, _, _, root_metadata = source_bindings[-1]
        source_children = []

        def require_eligible(path, metadata):
            if not retain_indexed:
                reason = exclusion_reason(path, metadata, user_name)
            elif stat.S_ISLNK(metadata.st_mode):
                reason = 'symlink_not_followed'
            elif getattr(metadata, 'st_flags', 0) & SF_DATALESS or path.name.endswith('.icloud'):
                reason = 'cloud_not_downloaded'
            elif not (stat.S_ISREG(metadata.st_mode) or stat.S_ISDIR(metadata.st_mode)):
                reason = 'non_regular'
            else:
                reason = None
            if reason:
                raise OSError('candidate_excluded_' + reason)
            if metadata.st_dev != root_metadata.st_dev:
                raise OSError('candidate_other_volume')

        def verify_bindings(bindings):
            for descriptor, parent_fd, name, observed in bindings:
                current = os.fstat(descriptor)
                path_safety._require_same(observed, current, revision=False)
                if getattr(current, 'st_flags', 0) & SF_DATALESS:
                    raise OSError('candidate_excluded_cloud_not_downloaded')
                if parent_fd is not None:
                    path_metadata = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                    path_safety._require_same(observed, path_metadata, revision=False)
                    if getattr(path_metadata, 'st_flags', 0) & SF_DATALESS:
                        raise OSError('candidate_excluded_cloud_not_downloaded')

        verify_bindings(source_bindings)
        parent_fd = root_fd
        child_relative = Path('.')
        for name in relative.parts[:-1]:
            check_deadline()
            child_relative = child_relative / name
            observed = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            require_eligible(child_relative, observed)
            if not stat.S_ISDIR(observed.st_mode):
                raise OSError('candidate_ancestor_not_directory')
            descriptor = path_safety._open_verified_directory(parent_fd, name, observed)
            handles.callback(os.close, descriptor)
            source_children.append((descriptor, parent_fd, name, observed))
            parent_fd = descriptor

        expected = os.stat(relative.name, dir_fd=parent_fd, follow_symlinks=False)
        require_eligible(relative, expected)
        if not stat.S_ISREG(expected.st_mode):
            raise OSError('candidate_not_regular_file')
        require_record(expected)
        verify_bindings(source_bindings + source_children)
        check_deadline()
        source_fd = os.open(relative.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                            dir_fd=parent_fd)
        handles.callback(os.close, source_fd)

        def verify_source():
            check_deadline()
            verify_bindings(source_bindings + source_children)
            for metadata in (os.fstat(source_fd), os.stat(relative.name, dir_fd=parent_fd,
                                                        follow_symlinks=False)):
                require_eligible(relative, metadata)
                require_record(metadata)
                path_safety._require_same(expected, metadata)

        verify_source()
        destination_bindings = handles.enter_context(
            path_safety._root_descriptors(destination.parent))
        destination_parent_fd, _, _, staging_metadata = destination_bindings[-1]
        if (stat.S_IMODE(staging_metadata.st_mode) & 0o077
                or staging_metadata.st_uid != os.geteuid()):
            raise OSError('private_owned_staging_directory_required')
        verify_bindings(destination_bindings)
        destination_fd = os.open(destination.name,
                                 os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                 0o600, dir_fd=destination_parent_fd)
        handles.callback(os.close, destination_fd)
        os.fchmod(destination_fd, 0o600)
        destination_identity = os.fstat(destination_fd)
        digest = hashlib.sha256()
        copied = 0
        while copied < expected.st_size:
            verify_source()
            block = os.read(source_fd, min(1024 * 1024, expected.st_size - copied))
            check_deadline()
            if not block:
                raise path_safety.SourceChangedError('candidate_truncated_during_copy')
            if copied + len(block) > min(max_bytes, expected.st_size):
                raise ValueError('approved_document_copy_byte_limit')
            view = memoryview(block)
            while view:
                check_deadline()
                written = os.write(destination_fd, view)
                if written <= 0:
                    raise OSError('staging_write_incomplete')
                view = view[written:]
            digest.update(block)
            copied += len(block)
        # A snapshot's copy time is not the document's revision time. Preserve
        # source mtime on the destination fd only; never touch the original.
        os.utime(destination_fd, ns=(expected.st_atime_ns, expected.st_mtime_ns))
        os.fsync(destination_fd)
        verify_source()
        verify_bindings(destination_bindings)
        written_metadata = os.fstat(destination_fd)
        path_safety._require_same(destination_identity, written_metadata, revision=False)
        path_safety._require_same(written_metadata, os.stat(destination.name,
            dir_fd=destination_parent_fd, follow_symlinks=False))
        if written_metadata.st_size != copied or stat.S_IMODE(written_metadata.st_mode) != 0o600:
            raise OSError('staging_file_metadata_changed')
        check_deadline()
    return {'sha256': digest.hexdigest(), 'bytes': copied}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--user', required=True)
    parser.add_argument('--max-entries', type=int, default=1_000_000)
    parser.add_argument('--max-seconds', type=float, default=900)
    args = parser.parse_args()
    result = scan(args.root, args.out, user_name=args.user,
                  max_entries=args.max_entries, max_seconds=args.max_seconds,
                  progress=lambda value: print(json.dumps(value, ensure_ascii=False), flush=True))
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return 0 if result['status'] == 'complete' else 2


if __name__ == '__main__':
    raise SystemExit(main())
