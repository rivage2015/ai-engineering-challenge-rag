"""Metadata discovery and explicit-consent updates, separate from answer evidence.

Names suggest a relationship; only a user can approve replacement. No source
contents are opened until adopt() has received the confirmation and its ticket.
"""
from __future__ import annotations

import copy
import getpass
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import secrets
import stat
import sys
import threading
import time

TICKET_SECONDS = 1800
MAX_CANDIDATES = 256
IDENTITY_FIELDS = ('path', 'size_bytes', 'mtime_ns', 'dev', 'ino', 'ctime_ns')


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':')).encode()).hexdigest()


def metadata_record(path):
    observed = path.lstat()
    return dict(path=str(path), name=path.name, parent_path=str(path.parent),
                extension=path.suffix.casefold(), size_bytes=observed.st_size,
                mtime_ns=observed.st_mtime_ns, dev=observed.st_dev,
                ino=observed.st_ino, ctime_ns=observed.st_ctime_ns)


def identity(record):
    return {key: record.get(key) for key in IDENTITY_FIELDS}


def private_directory(path):
    """Create private state directories using bound, no-follow directory fds.

    Only the final directory's mode is changed. Existing ancestors, including
    the filesystem root and shared temporary parents, retain their permissions.
    """
    path = Path(path).absolute()
    if '..' in path.parts or len(path.parts) < 2:
        raise ValueError('update_directory_invalid')
    bindings = []

    def directory_identity(metadata):
        return metadata.st_dev, metadata.st_ino, stat.S_IFMT(metadata.st_mode)

    def verify_bindings():
        for descriptor, parent_fd, name, expected in bindings:
            if directory_identity(os.fstat(descriptor)) != directory_identity(expected):
                raise ValueError('update_directory_changed')
            if parent_fd is not None:
                observed = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                if directory_identity(observed) != directory_identity(expected):
                    raise ValueError('update_directory_changed')

    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    try:
        descriptor = os.open(path.anchor, flags)
        bindings.append((descriptor, None, None, os.fstat(descriptor)))
        for name in path.parts[1:]:
            verify_bindings()
            parent_fd = descriptor
            try:
                expected = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                try:
                    os.mkdir(name, mode=0o700, dir_fd=parent_fd)
                except FileExistsError:
                    pass
                expected = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            if stat.S_ISLNK(expected.st_mode):
                raise ValueError('update_directory_symlink')
            if not stat.S_ISDIR(expected.st_mode):
                raise ValueError('update_directory_invalid')
            descriptor = os.open(name, flags, dir_fd=parent_fd)
            bindings.append((descriptor, parent_fd, name, expected))
            verify_bindings()
        if os.fstat(descriptor).st_uid != os.geteuid():
            raise ValueError('update_directory_owner_invalid')
        verify_bindings()
        os.fchmod(descriptor, 0o700)
        verify_bindings()
        if stat.S_IMODE(os.fstat(descriptor).st_mode) != 0o700:
            raise ValueError('update_directory_mode_changed')
        return path
    finally:
        for descriptor, _, _, _ in reversed(bindings):
            os.close(descriptor)


def private_json(path, value):
    """Replace one private state file without following ancestor or leaf links."""
    path = Path(path).absolute()
    if '..' in path.parts or len(path.parts) < 2:
        raise ValueError('update_state_path_invalid')
    safety = locations_module(Path(__file__).resolve().parent.parent / 'engine').path_safety
    temporary = '.' + path.name + '-' + secrets.token_hex(12)
    with safety._root_descriptors(path.parent) as bindings:
        parent_fd = bindings[-1][0]

        def verify_bindings():
            for descriptor, ancestor_fd, name, expected in bindings:
                safety._require_same(expected, os.fstat(descriptor), revision=False)
                if ancestor_fd is not None:
                    safety._require_same(expected, os.stat(name, dir_fd=ancestor_fd,
                        follow_symlinks=False), revision=False)
            parent = os.fstat(parent_fd)
            if stat.S_IMODE(parent.st_mode) & 0o077 or parent.st_uid != os.geteuid():
                raise ValueError('update_state_parent_not_private')

        def destination_metadata():
            try:
                observed = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                return None
            if not stat.S_ISREG(observed.st_mode) or observed.st_uid != os.geteuid():
                raise ValueError('update_state_destination_invalid')
            return observed

        verify_bindings()
        previous = destination_metadata()
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o600, dir_fd=parent_fd)
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            os.fchmod(handle.fileno(), 0o600)
            json.dump(value, handle, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
            written = os.fstat(handle.fileno())
            safety._require_same(written, os.stat(temporary, dir_fd=parent_fd,
                                                 follow_symlinks=False))
            current = destination_metadata()
            if (previous is None) != (current is None):
                raise ValueError('update_state_destination_changed')
            if previous is not None:
                safety._require_same(previous, current)
            verify_bindings()
            os.replace(temporary, path.name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
            os.fsync(parent_fd)
            verify_bindings()
            published = os.fstat(handle.fileno())
            safety._require_same(written, published, revision=False)
            # Rename legitimately updates ctime. All other file attributes and
            # the live descriptor/path revision must still agree.
            if any(getattr(written, key) != getattr(published, key) for key in
                   ('st_size', 'st_mtime_ns', 'st_mode', 'st_nlink', 'st_uid', 'st_gid')):
                raise ValueError('update_state_file_changed')
            safety._require_same(published, os.stat(path.name, dir_fd=parent_fd,
                                                   follow_symlinks=False))


def relative_name(value):
    if not isinstance(value, str):
        raise ValueError('update_inventory_path_invalid')
    path = Path(value)
    if path.is_absolute() or not path.parts or any(p in {'.', '..'} for p in path.parts):
        raise ValueError('update_inventory_path_invalid')
    return path


def locations_module(engine):
    # The app is both repo-run and packaged (engine is a sibling in the latter).
    key = '_local_memory_document_locations'
    if key in sys.modules:
        return sys.modules[key]
    if not (engine / 'document_locations.py').is_file():
        engine = Path(__file__).resolve().parent.parent / 'engine'
    spec = importlib.util.spec_from_file_location(key, engine / 'document_locations.py')
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(engine))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(str(engine))
    sys.modules[key] = module
    return module


class SourceUpdates:
    def __init__(self, bootstrap, *, root=None, user_name=None, locations=None):
        self.bootstrap = bootstrap
        self.root = Path(root or '/System/Volumes/Data').absolute()
        self.user_name = user_name or getpass.getuser()
        self.locations = locations or locations_module(bootstrap.ENGINE)
        self.lock = threading.RLock()
        self.store = bootstrap.SUPPORT / 'source-updates'
        self.phase = 'idle'
        self.summary = {}
        self.candidates = {}
        self.error = ''
        self.config = None
        self.context = None
        self.records = []
        self.expires = 0
        self.started = None

    def snapshot(self):
        with self.lock:
            stale = self.expires and time.monotonic() >= self.expires
            return copy.deepcopy(dict(phase=self.phase, summary=self.summary,
                candidates=list(self.candidates.values()), error=self.error,
                expired=bool(stale), started=self.started,
                elapsed_seconds=round(time.monotonic() - self.started, 1)
                    if self.started and self.phase in {'scanning', 'adopting'} else None))

    def reserve_scan(self):
        with self.lock:
            if self.phase in {'scanning', 'adopting'}:
                raise ValueError('update_busy')
            self.phase, self.error = 'scanning', ''
            self.candidates, self.summary = {}, {}
            self.expires = 0
            self.started = time.monotonic()

    def fail(self, exc):
        with self.lock:
            self.phase = 'error'
            # Bounded code, not arbitrary paths or private data from exceptions.
            message = str(exc)
            self.error = ('model_downloads_disabled_missing:'
                          if message.startswith('model_downloads_disabled_missing:') else
                          message[:120] if isinstance(exc, ValueError) else type(exc).__name__)

    def _basis(self, *, validate_source=False):
        exists, config = self.bootstrap.load_config_snapshot()
        if not exists:
            raise ValueError('update_configuration_missing')
        context = self.bootstrap.current_document_version_review_context(
            validate_source=validate_source)
        generation = self.bootstrap._generation_path(
            Path(config.get('workspace', self.bootstrap.SUPPORT / 'data')),
            config.get('active_generation'))
        if generation is None:
            raise ValueError('update_generation_invalid')
        path = generation / '01-path' / 'path-source-inventory.jsonl'
        if path.is_symlink():
            raise ValueError('update_inventory_invalid')
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != context['base_revision']['inventory_sha256']:
            raise ValueError('update_inventory_changed')
        if self.bootstrap.load_config_snapshot() != (True, config):
            raise ValueError('update_configuration_changed')
        records = [json.loads(line) for line in raw.splitlines() if line]
        for record in records:
            relative_name(record['relative_path'])
        return config, context, records

    def _dismissed(self):
        path = self.store / 'dismissed.json'
        if not path.exists():
            return set()
        if path.is_symlink() or not path.is_file():
            raise ValueError('update_dismissals_invalid')
        safety = self.locations.path_safety
        with safety._root_descriptors(path.parent) as bindings:
            parent_fd = bindings[-1][0]
            fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent_fd)
            try:
                observed = os.fstat(fd)
                if (not stat.S_ISREG(observed.st_mode) or observed.st_nlink != 1
                        or observed.st_uid != os.geteuid() or observed.st_size > 4 * 1024 * 1024):
                    raise ValueError('update_dismissals_invalid')
                with os.fdopen(os.dup(fd), 'r', encoding='utf-8') as handle:
                    values = json.loads(handle.read(4 * 1024 * 1024 + 1))
                safety._require_same(observed, os.fstat(fd))
                safety._require_same(observed, os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False))
                for descriptor, ancestor_fd, name, expected in bindings:
                    safety._require_same(expected, os.fstat(descriptor), revision=False)
                    if ancestor_fd is not None:
                        safety._require_same(expected, os.stat(name, dir_fd=ancestor_fd,
                                             follow_symlinks=False), revision=False)
            finally:
                os.close(fd)
        if not isinstance(values, list) or not all(isinstance(v, str) for v in values):
            raise ValueError('update_dismissals_invalid')
        return set(values)

    def scan_reserved(self):
        try:
            config, context, records = self._basis()
            private_directory(self.store)
            out = self.store / ('scan-' + secrets.token_hex(16))
            def progress(summary):
                with self.lock:
                    self.summary = copy.deepcopy(summary)
            summary = self.locations.scan(self.root, out, user_name=self.user_name,
                                          progress=progress)
            resolver = self.bootstrap._decision_resolver()
            targets = {}
            for record in records:
                if record.get('kind') == 'file' and record.get('sha256'):
                    name = Path(record['relative_path']).name
                    if Path(name).suffix.casefold().lstrip('.') in self.locations.SUFFIXES:
                        targets.setdefault(resolver.family_key(name), []).append(record)
            dismissed = self._dismissed()
            origins = config.get('source_update_origins', {})
            candidates = {}
            omitted = 0
            basis_key = fingerprint([context['base_revision'], config])
            target_keys = {r['relative_path']: fingerprint(r) for r in records}
            with (out / 'documents.jsonl').open(encoding='utf-8') as handle:
                for line in handle:
                    if time.monotonic() - self.started >= 900:
                        summary.update(status='partial', stopped_reason='candidate_matching_time_limit')
                        break
                    record = json.loads(line)
                    peers = targets.get(resolver.family_key(record['name']), [])
                    for position, target in enumerate(peers):
                        if len(candidates) >= MAX_CANDIDATES:
                            omitted += len(peers) - position
                            break
                        relative = target['relative_path']
                        indexed = str(Path(config['source_root']) / relative)
                        # Only exact previously approved original metadata may be
                        # suppressed. Similar names/dates never prove equivalence.
                        if (record['path'] == indexed and
                            record['size_bytes'] == target['size_bytes'] and
                            record['mtime_ns'] == target['mtime_ns']):
                            continue
                        origin = origins.get(relative, {})
                        if (identity(record) == identity(origin) and
                                origin.get('sha256') == target['sha256']):
                            continue
                        key = fingerprint([identity(record), target_keys[relative], basis_key])
                        if key in dismissed:
                            continue
                        ticket = secrets.token_urlsafe(24)
                        candidates[ticket] = dict(ticket=ticket, key=key,
                            record=record, target=target, indexed_path=indexed)
            if self.bootstrap.load_config_snapshot() != (True, config):
                raise ValueError('update_configuration_changed')
            with self.lock:
                self.config, self.context, self.records = config, context, records
                self.candidates = candidates
                self.summary = {**summary, 'candidate_overflow': omitted,
                                'metadata_only': True}
                self.phase = 'partial' if summary['status'] != 'complete' or omitted else 'complete'
                self.expires = time.monotonic() + TICKET_SECONDS
        except Exception as exc:
            self.fail(exc)

    def _ticket(self, ticket):
        if self.phase not in {'complete', 'partial'} or time.monotonic() >= self.expires:
            raise ValueError('update_confirmation_expired')
        candidate = self.candidates.get(ticket)
        if candidate is None:
            raise ValueError('update_candidate_unknown')
        if self.bootstrap.load_config_snapshot() != (True, self.config):
            raise ValueError('update_configuration_changed')
        return candidate

    def dismiss(self, ticket):
        with self.lock:
            item = self._ticket(ticket)
            dismissed = self._dismissed()
            dismissed.add(item['key'])
            private_json(self.store / 'dismissed.json', sorted(dismissed))
            del self.candidates[ticket]

    def reserve_adoption(self, ticket, *, confirmed=False):
        with self.lock:
            if confirmed is not True:
                raise ValueError('update_explicit_confirmation_required')
            item = copy.deepcopy(self._ticket(ticket))
            self.phase, self.started, self.error = 'adopting', time.monotonic(), ''
            return item

    def adopt_reserved(self, item):
        try:
            config, context, records = self._basis(validate_source=True)
            if config != self.config or context['base_revision'] != self.context['base_revision']:
                raise ValueError('update_basis_changed')
            # Never change the original or the old managed source. Build a fresh
            # private snapshot retaining every unrelated indexed file.
            sources = private_directory(self.bootstrap.SUPPORT / 'sources')
            stage = sources / ('update-' + secrets.token_hex(16))
            private_directory(stage)
            old_relative = item['target']['relative_path']
            new_relative = (relative_name(old_relative).parent / item['record']['name']).as_posix()
            relative_name(new_relative)
            if any(r['relative_path'] == new_relative and new_relative != old_relative for r in records):
                raise ValueError('update_destination_collision')
            origins = copy.deepcopy(config.get('source_update_origins', {}))
            origins.pop(old_relative, None)
            for record in records:
                relative = relative_name(record['relative_path'])
                if relative.as_posix() == old_relative:
                    continue
                destination = stage / relative
                if record.get('kind') == 'directory':
                    private_directory(destination)
                    continue
                if record.get('kind') != 'file' or not record.get('sha256'):
                    raise ValueError('update_existing_source_unreadable')
                private_directory(destination.parent)
                source_root = Path(config['source_root'])
                observed = metadata_record(source_root / relative)
                if any(observed[key] != record[key] for key in ('size_bytes', 'mtime_ns')):
                    raise ValueError('update_existing_source_changed')
                # Existing indexed ancillary files (.DS_Store etc.) are kept,
                # not newly discovered or promoted to answer evidence. Their
                # attested content hashes are mandatory for this separate path.
                try:
                    copied = self.locations.copy_indexed_file(source_root, observed,
                        record['sha256'], destination, user_name=self.user_name)
                except self.locations.path_safety.SourceChangedError as exc:
                    raise ValueError('update_existing_source_changed') from exc
                if copied['sha256'] != record['sha256']:
                    raise ValueError('update_existing_source_changed')
            private_directory((stage / new_relative).parent)
            copied = self.locations.copy_approved_document(self.root, item['record'],
                stage / new_relative, user_name=self.user_name)
            origins[new_relative] = {**identity(item['record']), 'sha256': copied['sha256']}
            self.bootstrap.apply_source_update(stage, config, origins)
            with self.lock:
                self.candidates = {}
                self.phase = 'applied'
        except Exception as exc:
            self.fail(exc)
