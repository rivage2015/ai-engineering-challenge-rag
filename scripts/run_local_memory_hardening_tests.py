#!/usr/bin/env python3
"""Run a preflight-approved, synthetic unittest file with durable bounded logs.

This is a test supervisor, not a security sandbox. Callers must first review
tests for network, GUI, credential, real-data and model side effects. Successful
runs do not attest to the absence of descendants or external I/O. Timeout/output
overflow kills the newly created process group, never an existing app/model.
SIGTERM/SIGHUP are ordinary cancellation with cleanup. SIGKILL, machine shutdown,
and descendants that escape the created process group cannot be recovered here.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
OUTPUT_ROOT = ROOT / "artifacts" / "local-memory-v1-hardening" / "runs"
TEST_ROOTS = (ROOT / "tests", ROOT / "distribution/macos-local-memory/tests")


class SupervisorInterrupted(BaseException):
    def __init__(self, signum: int):
        self.signum = signum
        super().__init__(f"supervisor received signal {signum}")


def terminal_summary(content: str) -> tuple[int | None, str | None, dict[str, int]]:
    """Use only a complete terminal unittest footer, not text in a traceback."""
    match = re.search(
        r"^Ran (\d+) tests? in [0-9]+(?:\.[0-9]+)?s\r?\n(?:\r?\n)+"
        r"(OK|FAILED)(?: \(([^()\r\n]*)\))?[ \t]*(?:\r?\n)*\Z",
        content, re.MULTILINE,
    )
    if match is None:
        return None, None, {}
    details = {}
    allowed = {"skipped", "expected failures", "unexpected successes", "failures", "errors"}
    if match.group(3) is not None:
        for part in match.group(3).split(", "):
            name, separator, value = part.partition("=")
            if name not in allowed or not separator or not value.isascii() or not value.isdigit() or name in details:
                return None, None, {}
            details[name] = int(value)
    return int(match.group(1)), match.group(2), details


def write_record(path: Path, value: dict) -> None:
    """Only used in a newly reserved run directory; never overwrites a result."""
    with path.open("x", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())


def run_bounded(
    command: list[str], output: Path, *, cwd: Path,
    timeout_seconds: float = 180, max_log_bytes: int = 8 * 1024 * 1024,
    poll_seconds: float = 0.02,
) -> dict:
    if not command or not 0 < timeout_seconds <= 1800:
        raise ValueError("invalid command or timeout")
    if not 1024 <= max_log_bytes <= 64 * 1024 * 1024:
        raise ValueError("invalid log bound")
    if not 0 < poll_seconds <= 0.1:
        raise ValueError("invalid poll interval")
    if threading.current_thread() is not threading.main_thread():
        raise ValueError("supervisor must run on the main thread for cancellation handling")
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    started = {
        "schema_version": "1.0", "status": "started",
        "command": command, "cwd": str(cwd),
        "started_at": datetime.now(timezone.utc).isoformat(),
        "timeout_seconds": timeout_seconds, "max_log_bytes": max_log_bytes,
        "repository_import_root": str(ROOT),
        "safety_scope": "preflight-approved synthetic tests; not an OS sandbox",
    }
    write_record(output / "started.json", started)
    start = time.monotonic()
    reason = None
    process = None
    log_path = output / "unittest.log"
    environment = os.environ.copy()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["PYTHONPATH"] = str(ROOT) + (
        os.pathsep + environment["PYTHONPATH"] if environment.get("PYTHONPATH") else ""
    )
    previous_handlers = {}

    def interrupted(signum, _frame):
        raise SupervisorInterrupted(signum)

    try:
        for signum in (signal.SIGTERM, signal.SIGHUP):
            previous_handlers[signum] = signal.signal(signum, interrupted)
        with log_path.open("xb") as log:
            process = subprocess.Popen(
                command, cwd=cwd, env=environment, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            assert process.stdout is not None
            os.set_blocking(process.stdout.fileno(), False)
            total = 0
            while True:
                block = process.stdout.read(min(65536, max_log_bytes - total + 1))
                if block:
                    accepted = block[:max_log_bytes - total]
                    log.write(accepted)
                    total += len(accepted)
                    if len(block) > len(accepted):
                        reason = "output_limit"
                        break
                if time.monotonic() - start >= timeout_seconds:
                    reason = "timeout"
                    break
                if block == b"" and process.poll() is not None:
                    break
                if not block:
                    time.sleep(min(poll_seconds, max(0, timeout_seconds - (time.monotonic() - start))))
            if reason:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            exit_code = process.wait(timeout=2)
            process.stdout.close()
            log.flush()
            os.fsync(log.fileno())
    except BaseException as exc:
        reason = "interrupted" if isinstance(exc, (KeyboardInterrupt, SupervisorInterrupted)) else "supervisor_error"
        # A repeated ordinary cancellation must not interrupt our own cleanup.
        for signum in previous_handlers:
            signal.signal(signum, signal.SIG_IGN)
        if process is not None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                reason = "cleanup_unconfirmed"
        write_record(output / "interrupted.json", {
            **started, "status": reason, "exception_type": type(exc).__name__,
            "signal": exc.signum if isinstance(exc, SupervisorInterrupted) else None,
            "elapsed_seconds": round(time.monotonic() - start, 6),
        })
        raise
    finally:
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)
    content = log_path.read_bytes()
    decoded = content.decode("utf-8", errors="replace")
    count, outcome, details = terminal_summary(decoded)
    skip_count = details.get("skipped", 0)
    expected_failures = details.get("expected failures", 0)
    if reason:
        status = reason
    elif exit_code != 0 or outcome == "FAILED" or any(details.get(key, 0) for key in ("failures", "errors", "unexpected successes")):
        status = "failed"
    elif count is None or count == 0 or outcome != "OK":
        status = "no_tests"
    elif skip_count:
        status = "completed_with_skips"
    elif expected_failures:
        status = "completed_with_expected_failures"
    else:
        status = "passed"
    result = {
        **started, "status": status, "exit_code": exit_code,
        "elapsed_seconds": round(time.monotonic() - start, 6),
        "tests_reported": count,
        "skipped_reported": skip_count,
        "expected_failures_reported": expected_failures,
        "log_path": str(log_path), "log_bytes": len(content),
        "log_sha256": hashlib.sha256(content).hexdigest(),
        "finished_at": datetime.now(timezone.utc).isoformat(),
    }
    write_record(output / "result.json", result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--test-file", required=True, type=Path)
    parser.add_argument("--case", action="append", default=[])
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--timeout", type=float, default=180)
    parser.add_argument("--max-log-bytes", type=int, default=8 * 1024 * 1024)
    args = parser.parse_args()
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,79}", args.run_id):
        parser.error("run-id must be a short lowercase identifier")
    test_file = args.test_file.resolve(strict=True)
    if test_file.parent not in TEST_ROOTS or test_file.suffix != ".py":
        parser.error("test-file must be a repository unittest file in an approved test directory")
    if any(not re.fullmatch(r"[A-Za-z_]\w*\.[A-Za-z_]\w*", case) for case in args.case):
        parser.error("case must be ClassName.test_method")
    if OUTPUT_ROOT.resolve().is_relative_to(ROOT) is False:
        parser.error("output root escapes repository")
    command = [args.python, "-B", str(test_file), *args.case, "-v"]
    result = run_bounded(
        command, OUTPUT_ROOT / args.run_id, cwd=ROOT,
        timeout_seconds=args.timeout, max_log_bytes=args.max_log_bytes,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
