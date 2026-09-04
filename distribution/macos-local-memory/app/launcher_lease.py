#!/usr/bin/env python3
"""Run the macOS launcher under one cross-process advisory lease."""

from __future__ import annotations

import fcntl
import os
import stat
import subprocess
import sys
from pathlib import Path


LEASE_ENVIRONMENT_KEY = "LOCAL_MEMORY_LAUNCH_LEASE_HELD"


def _open_private_lock(path: Path) -> int:
    if not path.is_absolute() or path.parent.is_symlink():
        raise RuntimeError("launcher_lock_path_invalid")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(
        path,
        os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        os.fchmod(descriptor, 0o600)
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or metadata.st_nlink != 1
        ):
            raise RuntimeError("launcher_lock_invalid")
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def run_with_lease(
    lock_path: Path,
    launcher: Path,
    launcher_arguments: list[str],
) -> int:
    if not launcher.is_absolute() or launcher.is_symlink():
        raise RuntimeError("launcher_script_invalid")
    launcher = launcher.resolve(strict=True)
    if not launcher.is_file():
        raise RuntimeError("launcher_script_invalid")
    descriptor = _open_private_lock(lock_path)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        environment = dict(os.environ)
        environment[LEASE_ENVIRONMENT_KEY] = "1"
        completed = subprocess.run(
            ["/bin/zsh", str(launcher), *launcher_arguments],
            env=environment,
            close_fds=True,
            check=False,
        )
        return completed.returncode
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def main() -> int:
    if len(sys.argv) < 3:
        raise SystemExit(
            "usage: launcher_lease.py LOCK_PATH LAUNCHER [ARG ...]"
        )
    return run_with_lease(
        Path(sys.argv[1]),
        Path(sys.argv[2]),
        sys.argv[3:],
    )


if __name__ == "__main__":
    raise SystemExit(main())
