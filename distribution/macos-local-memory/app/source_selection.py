"""Explicit, short-lived local folder selections; no document content is read."""
from __future__ import annotations

import copy
import secrets
import subprocess
import threading
import time
from pathlib import Path


PICKER_SCRIPT = '''try
return POSIX path of (choose folder with prompt "読み込んで質問したいフォルダを選んでください")
on error number -128
return ""
end try'''
SELECTION_TTL_SECONDS = 30 * 60


def choose_folder() -> Path | None:
    # No user text is interpolated into AppleScript or a shell command.
    result = subprocess.run(
        ["/usr/bin/osascript", "-e", PICKER_SCRIPT],
        capture_output=True, text=True, check=True, timeout=120,
    )
    value = result.stdout.removesuffix("\n")
    return Path(value) if value else None


class SourceSelection:
    def __init__(self, bootstrap):
        self.bootstrap = bootstrap
        self.lock = threading.Lock()
        self.candidate = None
        self.phase = "idle"
        self.error = ""
        self.started = None

    def snapshot(self):
        with self.lock:
            candidate = self.candidate
            expired = bool(candidate and time.monotonic() >= candidate["expires"])
            return {
                "phase": self.phase, "error": self.error,
                "elapsed_seconds": int(time.monotonic() - self.started) if self.started else 0,
                "path": candidate["identity"]["path"] if candidate else "",
                "ticket": candidate["ticket"] if candidate and not expired else "",
                "expired": expired,
            }

    def pick(self):
        with self.lock:
            self.candidate = None
            self.phase, self.error, self.started = "picking", "", time.monotonic()
        snapshot = self.bootstrap.load_config_snapshot()
        if not snapshot[0]:
            raise ValueError("source_configuration_missing")
        source = choose_folder()
        if source is None:
            self.cancel()
            return
        identity = self.bootstrap.source_selection_identity(source)
        with self.lock:
            self.candidate = {
                "identity": identity, "config": copy.deepcopy(snapshot[1]),
                "ticket": secrets.token_urlsafe(32),
                "expires": time.monotonic() + SELECTION_TTL_SECONDS,
            }
            self.phase, self.started = "selected", None

    def consume(self, ticket: str, *, confirmed: bool):
        with self.lock:
            candidate = self.candidate
            if (confirmed is not True or not candidate or not ticket
                    or not secrets.compare_digest(ticket, candidate["ticket"])
                    or time.monotonic() >= candidate["expires"]):
                raise ValueError("source_confirmation_invalid")
            self.candidate = None  # Replay, even after a failure, must be rejected.
            self.phase, self.error, self.started = "building", "", time.monotonic()
            return copy.deepcopy(candidate)

    def cancel(self):
        with self.lock:
            self.candidate = None
            self.phase, self.error, self.started = "cancelled", "", None

    def complete(self):
        with self.lock:
            self.phase, self.error, self.started = "complete", "", None

    def fail(self, error):
        with self.lock:
            self.candidate = None
            code = str(error) if str(error) in {
                "source_is_application_data", "source_directory_changed",
                "configuration_changed_before_publish", "build_already_running",
                "source_configuration_missing", "source_confirmation_invalid",
            } else type(error).__name__
            self.phase, self.error, self.started = "error", code, None
