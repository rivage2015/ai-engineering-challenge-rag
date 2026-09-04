#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import threading
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock


BOOTSTRAP_PATH = Path(__file__).resolve().parents[1] / "app" / "bootstrap.py"
APPLE_PYTHON = Path("/usr/bin/python3")


CHILD_SETUP = """
import importlib.util
import json
import os
import sys
from pathlib import Path

bootstrap_path = Path(os.environ["STEP5_BOOTSTRAP_PATH"])
specification = importlib.util.spec_from_file_location(
    "bootstrap_config_lease_subprocess",
    bootstrap_path,
)
assert specification is not None and specification.loader is not None
bootstrap = importlib.util.module_from_spec(specification)
specification.loader.exec_module(bootstrap)
bootstrap.SUPPORT = Path(os.environ["STEP5_SUPPORT"])
bootstrap.CONFIG = Path(os.environ["STEP5_CONFIG"])
"""


def load_bootstrap():
    specification = importlib.util.spec_from_file_location(
        "bootstrap_config_lease_test",
        BOOTSTRAP_PATH,
    )
    assert specification is not None and specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


class ConfigActivationLeaseTests(unittest.TestCase):
    @staticmethod
    def _require_apple_python_39() -> None:
        if not APPLE_PYTHON.exists():
            raise unittest.SkipTest("Apple /usr/bin/python3 is unavailable")
        version = subprocess.check_output(
            [str(APPLE_PYTHON), "-c", "import sys; print(sys.version_info[:2])"],
            text=True,
        ).strip()
        if version != "(3, 9)":
            raise unittest.SkipTest(
                f"Apple /usr/bin/python3 is not Python 3.9: {version}"
            )

    @staticmethod
    def _child_environment(
        temporary: str,
        config: Path,
        **values: str,
    ) -> dict[str, str]:
        environment = os.environ.copy()
        environment.update(
            {
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONPYCACHEPREFIX": str(Path(temporary) / "pycache"),
                "STEP5_BOOTSTRAP_PATH": str(BOOTSTRAP_PATH),
                "STEP5_SUPPORT": temporary,
                "STEP5_CONFIG": str(config),
                **values,
            }
        )
        return environment

    @staticmethod
    def _spawn_child(
        script: str,
        environment: dict[str, str],
    ) -> subprocess.Popen:
        return subprocess.Popen(
            [str(APPLE_PYTHON), "-c", CHILD_SETUP + script],
            env=environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    @staticmethod
    def _stop_child(process: subprocess.Popen) -> None:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=1.0)

    def _wait_for_marker(
        self,
        marker: Path,
        process: subprocess.Popen,
        timeout: float = 3.0,
    ) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if marker.exists():
                return
            if process.poll() is not None:
                stdout, stderr = process.communicate()
                self.fail(
                    f"child exited before {marker.name}: "
                    f"returncode={process.returncode}, stdout={stdout!r}, "
                    f"stderr={stderr!r}"
                )
            time.sleep(0.01)
        self.fail(f"child did not create {marker.name} within {timeout}s")

    def test_nonblocking_reader_rejects_active_writer(self) -> None:
        bootstrap = load_bootstrap()
        with tempfile.TemporaryDirectory() as temporary:
            bootstrap.SUPPORT = Path(temporary)
            bootstrap.CONFIG = bootstrap.SUPPORT / "config.json"
            with bootstrap._config_write_lease():
                with self.assertRaises(BlockingIOError):
                    with bootstrap.config_read_lease(blocking=False):
                        self.fail("conflicting read lease was granted")

    def test_config_writer_waits_until_activation_reader_releases(self) -> None:
        bootstrap = load_bootstrap()
        with tempfile.TemporaryDirectory() as temporary:
            bootstrap.SUPPORT = Path(temporary)
            bootstrap.CONFIG = bootstrap.SUPPORT / "config.json"
            writer_started = threading.Event()
            write_entered = threading.Event()
            original_write = bootstrap._atomic_json_unlocked

            def observed_write(path: Path, value: dict) -> None:
                write_entered.set()
                original_write(path, value)

            def writer() -> None:
                writer_started.set()
                bootstrap.atomic_json(bootstrap.CONFIG, {"value": 1})

            with mock.patch.object(
                bootstrap,
                "_atomic_json_unlocked",
                side_effect=observed_write,
            ):
                with bootstrap.config_read_lease():
                    thread = threading.Thread(target=writer)
                    thread.start()
                    self.assertTrue(writer_started.wait(1.0))
                    self.assertFalse(write_entered.wait(0.1))
                    self.assertTrue(thread.is_alive())
                thread.join(1.0)

            self.assertFalse(thread.is_alive())
            self.assertTrue(write_entered.is_set())
            self.assertEqual(
                {"value": 1},
                bootstrap.load_json(bootstrap.CONFIG),
            )

    def test_stale_writer_cannot_restore_a_concurrent_rollback(self) -> None:
        bootstrap = load_bootstrap()
        with tempfile.TemporaryDirectory() as temporary:
            bootstrap.SUPPORT = Path(temporary)
            bootstrap.CONFIG = bootstrap.SUPPORT / "config.json"
            original = {
                "source_root": "source-a",
                "cross_document_semantic_graph_answer_promotion_enabled": True,
            }
            rollback = {
                "source_root": "source-b",
                "cross_document_semantic_graph_answer_promotion_enabled": False,
            }
            bootstrap.atomic_json(bootstrap.CONFIG, original)
            snapshot_loaded = threading.Event()
            rollback_published = threading.Event()
            stale_errors: list[Exception] = []

            def stale_writer() -> None:
                exists, stale = bootstrap.load_config_snapshot()
                self.assertTrue(exists)
                snapshot_loaded.set()
                rollback_published.wait(1.0)
                try:
                    bootstrap.atomic_config_compare_and_swap(
                        stale,
                        {**stale, "stale_writer": True},
                    )
                except Exception as exc:
                    stale_errors.append(exc)

            thread = threading.Thread(target=stale_writer)
            thread.start()
            self.assertTrue(snapshot_loaded.wait(1.0))
            bootstrap.atomic_json(bootstrap.CONFIG, rollback)
            rollback_published.set()
            thread.join(1.0)

            self.assertFalse(thread.is_alive())
            self.assertEqual(1, len(stale_errors))
            self.assertEqual(
                "configuration_changed_before_publish",
                str(stale_errors[0]),
            )
            self.assertEqual(rollback, bootstrap.load_json(bootstrap.CONFIG))

    def test_shared_lease_blocks_cas_writer_across_apple_python_processes(
        self,
    ) -> None:
        self._require_apple_python_39()
        bootstrap = load_bootstrap()
        with tempfile.TemporaryDirectory() as temporary:
            bootstrap.SUPPORT = Path(temporary)
            bootstrap.CONFIG = bootstrap.SUPPORT / "config.json"
            original = {"source_root": "source-a", "generation": 1}
            replacement = {"source_root": "source-a", "generation": 2}
            bootstrap.atomic_json(bootstrap.CONFIG, original)
            reader_ready = bootstrap.SUPPORT / "reader-ready"
            writer_attempted = bootstrap.SUPPORT / "writer-attempted"

            reader = self._spawn_child(
                """
with bootstrap.config_read_lease():
    Path(os.environ["STEP5_READER_READY"]).write_text("ready", encoding="utf-8")
    if sys.stdin.readline() == "":
        raise RuntimeError("reader_release_signal_missing")
print("RELEASED", flush=True)
""",
                self._child_environment(
                    temporary,
                    bootstrap.CONFIG,
                    STEP5_READER_READY=str(reader_ready),
                ),
            )
            self.addCleanup(self._stop_child, reader)
            self._wait_for_marker(reader_ready, reader)

            writer = self._spawn_child(
                """
expected = json.loads(os.environ["STEP5_EXPECTED"])
replacement = json.loads(os.environ["STEP5_REPLACEMENT"])
Path(os.environ["STEP5_WRITER_ATTEMPTED"]).write_text("attempted", encoding="utf-8")
bootstrap.atomic_config_compare_and_swap(expected, replacement)
print("PUBLISHED", flush=True)
""",
                self._child_environment(
                    temporary,
                    bootstrap.CONFIG,
                    STEP5_EXPECTED=json.dumps(original),
                    STEP5_REPLACEMENT=json.dumps(replacement),
                    STEP5_WRITER_ATTEMPTED=str(writer_attempted),
                ),
            )
            self.addCleanup(self._stop_child, writer)
            self._wait_for_marker(writer_attempted, writer)

            with self.assertRaises(subprocess.TimeoutExpired):
                writer.wait(timeout=0.2)
            self.assertEqual(original, bootstrap.load_json(bootstrap.CONFIG))

            assert reader.stdin is not None
            reader.stdin.write("release\n")
            reader.stdin.flush()
            reader_stdout, reader_stderr = reader.communicate(timeout=3.0)
            writer_stdout, writer_stderr = writer.communicate(timeout=3.0)

            self.assertEqual(0, reader.returncode, reader_stderr)
            self.assertIn("RELEASED", reader_stdout)
            self.assertEqual(0, writer.returncode, writer_stderr)
            self.assertIn("PUBLISHED", writer_stdout)
            self.assertEqual(replacement, bootstrap.load_json(bootstrap.CONFIG))

    def test_stale_snapshot_cas_fails_after_another_apple_python_process_updates(
        self,
    ) -> None:
        self._require_apple_python_39()
        bootstrap = load_bootstrap()
        with tempfile.TemporaryDirectory() as temporary:
            bootstrap.SUPPORT = Path(temporary)
            bootstrap.CONFIG = bootstrap.SUPPORT / "config.json"
            original = {"source_root": "source-a", "generation": 1}
            replacement = {"source_root": "source-b", "generation": 2}
            stale_replacement = {
                "source_root": "source-a",
                "generation": 1,
                "stale_writer": True,
            }
            bootstrap.atomic_json(bootstrap.CONFIG, original)
            snapshot_ready = bootstrap.SUPPORT / "snapshot-ready"

            stale_writer = self._spawn_child(
                """
exists, snapshot = bootstrap.load_config_snapshot()
if not exists:
    raise RuntimeError("expected_config_snapshot_missing")
Path(os.environ["STEP5_SNAPSHOT_READY"]).write_text("ready", encoding="utf-8")
if sys.stdin.readline() == "":
    raise RuntimeError("stale_writer_release_signal_missing")
try:
    bootstrap.atomic_config_compare_and_swap(
        snapshot,
        json.loads(os.environ["STEP5_STALE_REPLACEMENT"]),
    )
except RuntimeError as exc:
    if str(exc) != "configuration_changed_before_publish":
        raise
    print(str(exc), flush=True)
else:
    raise RuntimeError("stale_compare_and_swap_was_accepted")
""",
                self._child_environment(
                    temporary,
                    bootstrap.CONFIG,
                    STEP5_SNAPSHOT_READY=str(snapshot_ready),
                    STEP5_STALE_REPLACEMENT=json.dumps(stale_replacement),
                ),
            )
            self.addCleanup(self._stop_child, stale_writer)
            self._wait_for_marker(snapshot_ready, stale_writer)

            updater = self._spawn_child(
                """
bootstrap.atomic_config_compare_and_swap(
    json.loads(os.environ["STEP5_EXPECTED"]),
    json.loads(os.environ["STEP5_REPLACEMENT"]),
)
print("PUBLISHED", flush=True)
""",
                self._child_environment(
                    temporary,
                    bootstrap.CONFIG,
                    STEP5_EXPECTED=json.dumps(original),
                    STEP5_REPLACEMENT=json.dumps(replacement),
                ),
            )
            self.addCleanup(self._stop_child, updater)
            updater_stdout, updater_stderr = updater.communicate(timeout=3.0)
            self.assertEqual(0, updater.returncode, updater_stderr)
            self.assertIn("PUBLISHED", updater_stdout)

            assert stale_writer.stdin is not None
            stale_writer.stdin.write("continue\n")
            stale_writer.stdin.flush()
            stale_stdout, stale_stderr = stale_writer.communicate(timeout=3.0)

            self.assertEqual(0, stale_writer.returncode, stale_stderr)
            self.assertIn("configuration_changed_before_publish", stale_stdout)
            self.assertEqual(replacement, bootstrap.load_json(bootstrap.CONFIG))

    def test_build_execution_lease_is_exclusive_across_apple_python_processes(
        self,
    ) -> None:
        self._require_apple_python_39()
        with tempfile.TemporaryDirectory() as temporary:
            support = Path(temporary)
            config = support / "config.json"
            holder_ready = support / "build-holder-ready"
            contender_rejected = support / "build-contender-rejected"
            contender_acquired = support / "build-contender-acquired"

            holder = self._spawn_child(
                """
with bootstrap.build_execution_lease(blocking=False):
    Path(os.environ["STEP5_HOLDER_READY"]).write_text("ready", encoding="utf-8")
    if sys.stdin.readline() == "":
        raise RuntimeError("build_holder_release_signal_missing")
print("RELEASED", flush=True)
""",
                self._child_environment(
                    temporary,
                    config,
                    STEP5_HOLDER_READY=str(holder_ready),
                ),
            )
            self.addCleanup(self._stop_child, holder)
            self._wait_for_marker(holder_ready, holder)

            contender = self._spawn_child(
                """
try:
    with bootstrap.build_execution_lease(blocking=False):
        raise RuntimeError("concurrent_build_lease_was_granted")
except RuntimeError as exc:
    if str(exc) != "build_already_running":
        raise
Path(os.environ["STEP5_CONTENDER_REJECTED"]).write_text(
    "rejected",
    encoding="utf-8",
)
print("REJECTED", flush=True)
if sys.stdin.readline() == "":
    raise RuntimeError("build_contender_retry_signal_missing")
with bootstrap.build_execution_lease(blocking=False):
    Path(os.environ["STEP5_CONTENDER_ACQUIRED"]).write_text(
        "acquired",
        encoding="utf-8",
    )
print("ACQUIRED", flush=True)
""",
                self._child_environment(
                    temporary,
                    config,
                    STEP5_CONTENDER_REJECTED=str(contender_rejected),
                    STEP5_CONTENDER_ACQUIRED=str(contender_acquired),
                ),
            )
            self.addCleanup(self._stop_child, contender)
            self._wait_for_marker(contender_rejected, contender)
            self.assertFalse(contender_acquired.exists())
            self.assertIsNone(contender.poll())

            assert holder.stdin is not None
            holder.stdin.write("release\n")
            holder.stdin.flush()
            holder_stdout, holder_stderr = holder.communicate(timeout=3.0)
            self.assertEqual(0, holder.returncode, holder_stderr)
            self.assertIn("RELEASED", holder_stdout)

            assert contender.stdin is not None
            contender.stdin.write("retry\n")
            contender.stdin.flush()
            contender_stdout, contender_stderr = contender.communicate(timeout=3.0)
            self.assertEqual(0, contender.returncode, contender_stderr)
            self.assertIn("REJECTED", contender_stdout)
            self.assertIn("ACQUIRED", contender_stdout)
            self.assertTrue(contender_acquired.exists())

    def test_recovery_is_non_destructive_while_another_process_builds(
        self,
    ) -> None:
        self._require_apple_python_39()
        bootstrap = load_bootstrap()
        with tempfile.TemporaryDirectory() as temporary:
            support = Path(temporary)
            workspace = support / "data"
            bootstrap.SUPPORT = support
            bootstrap.CONFIG = support / "config.json"
            bootstrap.STATE = support / "state.json"
            bootstrap.atomic_json(
                bootstrap.CONFIG,
                {
                    "source_root": str(support / "source"),
                    "workspace": str(workspace),
                    "active_generation": None,
                },
            )
            bootstrap.atomic_json(
                bootstrap.STATE,
                {
                    "phase": "building",
                    "message": "build in progress",
                    "error": "",
                },
            )
            config_before = (
                bootstrap.CONFIG.read_bytes(),
                bootstrap.CONFIG.stat().st_ino,
                bootstrap.CONFIG.stat().st_mtime_ns,
            )
            state_before = (
                bootstrap.STATE.read_bytes(),
                bootstrap.STATE.stat().st_ino,
                bootstrap.STATE.stat().st_mtime_ns,
            )
            holder_ready = support / "recovery-build-holder-ready"

            holder = self._spawn_child(
                """
with bootstrap.build_execution_lease(blocking=False):
    Path(os.environ["STEP5_HOLDER_READY"]).write_text("ready", encoding="utf-8")
    if sys.stdin.readline() == "":
        raise RuntimeError("recovery_build_holder_release_signal_missing")
print("RELEASED", flush=True)
""",
                self._child_environment(
                    temporary,
                    bootstrap.CONFIG,
                    STEP5_HOLDER_READY=str(holder_ready),
                ),
            )
            self.addCleanup(self._stop_child, holder)
            self._wait_for_marker(holder_ready, holder)

            self.assertEqual(
                {"status": "active_build", "removed": []},
                bootstrap.recover_interrupted_build(),
            )
            self.assertEqual(
                config_before,
                (
                    bootstrap.CONFIG.read_bytes(),
                    bootstrap.CONFIG.stat().st_ino,
                    bootstrap.CONFIG.stat().st_mtime_ns,
                ),
            )
            self.assertEqual(
                state_before,
                (
                    bootstrap.STATE.read_bytes(),
                    bootstrap.STATE.stat().st_ino,
                    bootstrap.STATE.stat().st_mtime_ns,
                ),
            )

            assert holder.stdin is not None
            holder.stdin.write("release\n")
            holder.stdin.flush()
            holder_stdout, holder_stderr = holder.communicate(timeout=3.0)
            self.assertEqual(0, holder.returncode, holder_stderr)
            self.assertIn("RELEASED", holder_stdout)


if __name__ == "__main__":
    unittest.main()
