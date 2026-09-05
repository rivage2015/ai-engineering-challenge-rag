from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import struct
import sys
import tempfile
import threading
import time
import types
import unittest
from contextlib import ExitStack
from contextlib import contextmanager
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import build_intermediate_records as builder  # noqa: E402
import local_image_ocr as reader  # noqa: E402
import local_paddle_ocr as worker  # noqa: E402
import local_visual_observation as visual  # noqa: E402
import probe_intermediate_records as records  # noqa: E402


def png_header(width: int = 80, height: int = 40) -> bytes:
    return (
        b"\x89PNG\r\n\x1a\n"
        + b"\x00\x00\x00\rIHDR"
        + struct.pack(">II", width, height)
        + b"\x08\x02\x00\x00\x00"
    )


def paddle_result(
    status: str = "completed",
) -> tuple[
    str,
    list[dict[str, object]],
    list[str],
    str | None,
    dict[str, object] | None,
    dict[str, object] | None,
]:
    lines = (
        [{
            "line_id": "line_1",
            "sequence": 1,
            "raw_text": "中野",
            "bbox": [100, 100, 300, 80],
            "confidence": 0.99,
        }]
        if status == "completed" else []
    )
    return (
        status,
        lines,
        [] if status == "completed" else ["no lines"],
        "failed" if status == "failed" else None,
        {"fingerprint_sha256": "a" * 64} if status != "failed" else None,
        {"setup_ms": 1.0, "inference_ms": 2.0}
        if status != "failed" else None,
    )


def visual_result(raw: bytes, text: str = "[暫定読取] object o1: chart") -> dict[str, object]:
    return {
        "text": text,
        "observation_type": "whole_image_literal_visual_observation",
        "observation": {
            "visible_objects": [],
            "explicit_labels": [],
            "explicit_relations": [],
            "labeled_values": [],
            "warnings": [],
        },
        "model": "gemma4:12b",
        "model_digest": "a" * 64,
        "prompt_sha256": "b" * 64,
        "input_image_sha256": hashlib.sha256(raw).hexdigest(),
        "model_output_sha256": "c" * 64,
        "runner": "ollama_loopback_chat",
        "runner_version": "test",
        "host": "127.0.0.1",
        "temperature": 0,
        "strict_json": True,
    }


class PaddleWorkerSessionTests(unittest.TestCase):
    def setUp(self) -> None:
        reader._LOCAL_MODEL_TIMEOUT_LATCH.clear()
        reader._UNREAPED_PADDLE_PROCESSES.clear()

    def tearDown(self) -> None:
        reader._LOCAL_MODEL_TIMEOUT_LATCH.clear()
        reader._UNREAPED_PADDLE_PROCESSES.clear()

    def test_successful_result_is_memoized_only_within_one_session(self) -> None:
        session = reader.PaddleOCRSession(runtime={"source": "test"})
        uncached = mock.Mock(return_value=paddle_result())
        raw = png_header()
        dimensions = {"width_px": 80, "height_px": 40}
        with mock.patch.object(session, "_run_uncached", uncached):
            first = session.run(raw, dimensions, timeout=10)
            second = session.run(raw, dimensions, timeout=10)

        self.assertEqual(uncached.call_count, 1)
        self.assertFalse(first[5]["cache_hit"])
        self.assertTrue(second[5]["cache_hit"])
        self.assertEqual(first[:5], second[:5])
        self.assertEqual(second[5]["setup_ms"], 0.0)
        self.assertEqual(second[5]["inference_ms"], 0.0)
        self.assertEqual(second[5]["cache_scope"], "build")
        self.assertEqual(
            second[5]["cached_result_sha256"], first[5]["result_sha256"]
        )

        other_session = reader.PaddleOCRSession(runtime={"source": "test"})
        other_uncached = mock.Mock(return_value=paddle_result())
        with mock.patch.object(other_session, "_run_uncached", other_uncached):
            other_session.run(raw, dimensions, timeout=10)
        other_uncached.assert_called_once()

    def test_failed_result_is_never_memoized(self) -> None:
        session = reader.PaddleOCRSession(runtime={"source": "test"})
        uncached = mock.Mock(return_value=paddle_result("failed"))
        with mock.patch.object(session, "_run_uncached", uncached):
            session.run(png_header(), {"width_px": 80, "height_px": 40}, timeout=10)
            session.run(png_header(), {"width_px": 80, "height_px": 40}, timeout=10)
        self.assertEqual(uncached.call_count, 2)

    def test_needs_review_is_a_successful_worker_result_and_is_memoized(self) -> None:
        session = reader.PaddleOCRSession(runtime={"source": "test"})
        uncached = mock.Mock(return_value=paddle_result("needs_review"))
        with mock.patch.object(session, "_run_uncached", uncached):
            first = session.run(
                png_header(), {"width_px": 80, "height_px": 40}, timeout=10
            )
            second = session.run(
                png_header(), {"width_px": 80, "height_px": 40}, timeout=10
            )
        self.assertEqual(uncached.call_count, 1)
        self.assertFalse(first[5]["cache_hit"])
        self.assertTrue(second[5]["cache_hit"])

    def test_cache_key_binds_bytes_and_dimensions(self) -> None:
        session = reader.PaddleOCRSession(runtime={"source": "test"})
        uncached = mock.Mock(return_value=paddle_result())
        with mock.patch.object(session, "_run_uncached", uncached):
            session.run(png_header(), {"width_px": 80, "height_px": 40}, timeout=10)
            session.run(png_header(81, 40), {"width_px": 81, "height_px": 40}, timeout=10)
        self.assertEqual(uncached.call_count, 2)

    def test_cache_evicts_lru_entry_at_count_limit(self) -> None:
        session = reader.PaddleOCRSession(runtime={"source": "test"})
        uncached = mock.Mock(return_value=paddle_result())
        dimensions = {"width_px": 80, "height_px": 40}
        raws = [png_header() + bytes([suffix]) for suffix in range(3)]
        with (
            mock.patch.object(reader, "MAX_PADDLE_SESSION_CACHE_ENTRIES", 2),
            mock.patch.object(
                reader, "MAX_PADDLE_SESSION_CACHE_BYTES", 1024 * 1024
            ),
            mock.patch.object(session, "_run_uncached", uncached),
        ):
            for raw in raws:
                session.run(raw, dimensions, timeout=10)
            session.run(raws[0], dimensions, timeout=10)

        self.assertEqual(uncached.call_count, 4)
        self.assertEqual(len(session._cache), 2)

    def test_cache_evicts_at_serialized_byte_budget(self) -> None:
        session = reader.PaddleOCRSession(runtime={"source": "test"})
        result = paddle_result()
        one_entry_bytes = reader._paddle_cache_entry_size(result)
        self.assertIsInstance(one_entry_bytes, int)
        uncached = mock.Mock(return_value=result)
        dimensions = {"width_px": 80, "height_px": 40}
        first = png_header() + b"a"
        second = png_header() + b"b"
        with (
            mock.patch.object(reader, "MAX_PADDLE_SESSION_CACHE_ENTRIES", 32),
            mock.patch.object(
                reader, "MAX_PADDLE_SESSION_CACHE_BYTES", one_entry_bytes
            ),
            mock.patch.object(session, "_run_uncached", uncached),
        ):
            session.run(first, dimensions, timeout=10)
            session.run(second, dimensions, timeout=10)
            session.run(first, dimensions, timeout=10)

        self.assertEqual(uncached.call_count, 3)
        self.assertLessEqual(session._cache_bytes, one_entry_bytes)
        self.assertEqual(len(session._cache), 1)

    def test_safe_auto_overlap_gate_rejects_24_gib_host(self) -> None:
        with mock.patch.object(
            reader, "_physical_memory_bytes", return_value=24 * 1024**3
        ):
            session = reader.PaddleOCRSession(runtime={"source": "test"})
        self.assertFalse(session.overlap_allowed)
        self.assertEqual(session.overlap_gate_reason, "physical_memory_below_48_gib")

    def test_safe_auto_overlap_gate_requires_available_memory(self) -> None:
        with (
            mock.patch.object(
                reader, "_physical_memory_bytes", return_value=64 * 1024**3
            ),
            mock.patch.object(reader, "_available_memory_bytes", return_value=None),
        ):
            unavailable = reader.PaddleOCRSession(runtime={"source": "test"})
        self.assertFalse(unavailable.overlap_allowed)
        self.assertEqual(
            unavailable.overlap_gate_reason, "available_memory_unavailable"
        )

        with (
            mock.patch.object(
                reader, "_physical_memory_bytes", return_value=64 * 1024**3
            ),
            mock.patch.object(
                reader, "_available_memory_bytes", return_value=20 * 1024**3
            ),
        ):
            sufficient = reader.PaddleOCRSession(runtime={"source": "test"})
        self.assertTrue(sufficient.overlap_allowed)
        self.assertEqual(
            sufficient.overlap_gate_reason,
            "physical_and_available_memory_sufficient",
        )

    def test_context_activation_always_closes_session(self) -> None:
        session = mock.Mock()
        session.__enter__ = mock.Mock(return_value=session)
        session.__exit__ = mock.Mock(return_value=None)
        with builder.paddle_build_session(session_factory=lambda: session):
            self.assertIs(reader.active_paddle_session(), session)
        session.__enter__.assert_called_once_with()
        session.__exit__.assert_called_once()
        self.assertIsNone(reader.active_paddle_session())

    def test_build_scope_cleans_up_after_extraction_exception(self) -> None:
        session = mock.Mock()
        session.__enter__ = mock.Mock(return_value=session)
        session.__exit__ = mock.Mock(return_value=None)
        with self.assertRaisesRegex(RuntimeError, "fixture failure"):
            with builder.paddle_build_session(session_factory=lambda: session):
                raise RuntimeError("fixture failure")
        session.__exit__.assert_called_once()
        self.assertIsNone(reader.active_paddle_session())

    def test_protocol_hash_mismatch_fails_closed(self) -> None:
        input_metadata = {
            "sha256": "b" * 64,
            "width_px": 80,
            "height_px": 40,
        }
        response = {
            "protocol_version": reader.PADDLE_SESSION_PROTOCOL_VERSION,
            "type": "ocr_result",
            "request_id": "request-1",
            "input": input_metadata,
            "result_sha256": "0" * 64,
            "result": {"status": "completed"},
        }
        with self.assertRaisesRegex(RuntimeError, "result hash"):
            reader.PaddleOCRSession._decode_session_response(
                (json.dumps(response) + "\n").encode("utf-8"),
                request_id="request-1",
                input_metadata=input_metadata,
            )

    def test_paddle_json_boundaries_reject_nonfinite_constants(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "invalid JSON"):
            reader._decode_paddle_worker_payload(b'{"status":NaN}')
        with self.assertRaisesRegex(ValueError, "invalid JSON"):
            worker._read_bounded_session_request(
                io.BytesIO(b'{"protocol_version":NaN}\n')
            )
        with self.assertRaises(ValueError):
            worker.canonical_json({"timing": float("inf")})

    def test_paddle_session_response_rejects_float_dimensions(self) -> None:
        expected_input = {
            "sha256": "b" * 64,
            "width_px": 80,
            "height_px": 40,
        }
        response_input = {**expected_input, "width_px": 80.0}
        result = {"status": "completed"}
        response = {
            "protocol_version": reader.PADDLE_SESSION_PROTOCOL_VERSION,
            "type": "ocr_result",
            "request_id": "request-1",
            "input": response_input,
            "result_sha256": hashlib.sha256(
                json.dumps(
                    result,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest(),
            "result": result,
        }
        with self.assertRaisesRegex(RuntimeError, "identity mismatch"):
            reader.PaddleOCRSession._decode_session_response(
                (json.dumps(response) + "\n").encode("utf-8"),
                request_id="request-1",
                input_metadata=expected_input,
            )

    def test_timeout_aborts_worker_before_returning(self) -> None:
        session = reader.PaddleOCRSession(runtime={"source": "test"})
        process = mock.Mock()
        process.stdin = io.BytesIO()
        with (
            mock.patch.object(session, "_start_worker", return_value=process),
            mock.patch.object(
                session,
                "_read_response_line",
                side_effect=TimeoutError("deadline"),
            ),
            mock.patch.object(session, "_abort_worker") as abort,
        ):
            with self.assertRaisesRegex(TimeoutError, "deadline"):
                session._run_uncached(
                    png_header(),
                    {"width_px": 80, "height_px": 40},
                    timeout=0.01,
                )
        abort.assert_called_once_with()

    def test_post_start_temporary_file_failure_aborts_worker(self) -> None:
        session = reader.PaddleOCRSession(runtime={"source": "test"})
        process = mock.Mock()
        with (
            mock.patch.object(session, "_start_worker", return_value=process),
            mock.patch.object(
                reader.tempfile,
                "TemporaryDirectory",
                side_effect=OSError("disk unavailable"),
            ),
            mock.patch.object(session, "_abort_worker") as abort,
        ):
            with self.assertRaisesRegex(OSError, "disk unavailable"):
                session._run_uncached(
                    png_header(),
                    {"width_px": 80, "height_px": 40},
                    timeout=1,
                )
        abort.assert_called_once_with()

    def test_parent_request_writer_retries_short_writes(self) -> None:
        class ShortWriter(io.BytesIO):
            def write(self, value):
                return super().write(bytes(value[:3]))

        destination = ShortWriter()
        reader._write_all(destination, b"abcdefgh", "test request")
        self.assertEqual(destination.getvalue(), b"abcdefgh")

    def test_cancel_waits_for_worker_start_and_terminates_child(self) -> None:
        session = reader.PaddleOCRSession(runtime={
            "source": "test",
            "network_sandbox": Path("/sandbox"),
            "network_profile": "deny",
            "python": Path("/python"),
            "worker": Path("/worker"),
            "model_root": Path("/models"),
            "runtime_lock": Path("/lock"),
        })
        process = mock.Mock()
        process.stdin = io.BytesIO()
        process.stdout = io.BytesIO()
        process.pid = 123456
        process.poll.return_value = None
        process.wait.return_value = 0
        popen_started = threading.Event()
        allow_popen_return = threading.Event()

        def delayed_popen(*_args, **_kwargs):
            popen_started.set()
            self.assertTrue(allow_popen_return.wait(2))
            return process

        started_worker: list[object] = []
        starter = threading.Thread(
            target=lambda: started_worker.append(session._start_worker())
        )
        with (
            mock.patch.object(reader.subprocess, "Popen", side_effect=delayed_popen),
            mock.patch.object(reader.os, "killpg") as killpg,
        ):
            starter.start()
            self.assertTrue(popen_started.wait(1))
            canceller = threading.Thread(target=session.cancel_active_request)
            canceller.start()
            allow_popen_return.set()
            starter.join(timeout=2)
            canceller.join(timeout=2)

        self.assertFalse(starter.is_alive())
        self.assertFalse(canceller.is_alive())
        self.assertEqual(started_worker, [process])
        self.assertIsNone(session._process)
        killpg.assert_called_once_with(process.pid, reader.signal.SIGTERM)

    def test_request_cancelled_before_process_lock_never_starts_worker(self) -> None:
        session = reader.PaddleOCRSession(runtime={"source": "test"})
        cancel_event = threading.Event()
        ready = threading.Event()
        errors: list[BaseException] = []

        def start() -> None:
            ready.set()
            try:
                session._start_worker(cancel_event)
            except BaseException as exc:
                errors.append(exc)

        with mock.patch.object(reader.subprocess, "Popen") as popen:
            with session._process_lock:
                starter = threading.Thread(target=start)
                starter.start()
                self.assertTrue(ready.wait(1))
                cancel_event.set()
            starter.join(timeout=2)

        self.assertFalse(starter.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], reader.concurrent.futures.CancelledError)
        popen.assert_not_called()

    def test_timeout_latch_wins_during_runtime_resolution_before_worker_start(self) -> None:
        runtime = {
            "source": "test",
            "network_sandbox": Path("/sandbox"),
            "network_profile": "deny",
            "python": Path("/python"),
            "worker": Path("/worker"),
            "model_root": Path("/models"),
            "runtime_lock": Path("/lock"),
        }
        session = reader.PaddleOCRSession()
        resolving = threading.Event()
        finish_resolution = threading.Event()
        errors: list[BaseException] = []

        def delayed_runtime() -> dict[str, object]:
            resolving.set()
            self.assertTrue(finish_resolution.wait(2))
            return runtime

        def start() -> None:
            try:
                session._start_worker()
            except BaseException as exc:
                errors.append(exc)

        with (
            mock.patch.object(
                session, "_resolved_runtime", side_effect=delayed_runtime
            ),
            mock.patch.object(reader.subprocess, "Popen") as popen,
        ):
            starter = threading.Thread(target=start)
            starter.start()
            self.assertTrue(resolving.wait(1))
            reader.latch_local_model_timeout()
            finish_resolution.set()
            starter.join(timeout=2)

        self.assertFalse(starter.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], RuntimeError)
        self.assertIn("restart is disabled", str(errors[0]))
        popen.assert_not_called()

    def test_latch_requested_during_popen_retires_new_worker(self) -> None:
        runtime = {
            "source": "test",
            "network_sandbox": Path("/sandbox"),
            "network_profile": "deny",
            "python": Path("/python"),
            "worker": Path("/worker"),
            "model_root": Path("/models"),
            "runtime_lock": Path("/lock"),
        }
        session = reader.PaddleOCRSession(runtime=runtime)
        process = mock.Mock()
        process.stdin = io.BytesIO()
        process.stdout = io.BytesIO()
        process.pid = 424246
        process.poll.return_value = None
        process.wait.return_value = 0
        popen_entered = threading.Event()
        finish_popen = threading.Event()
        latch_finished = threading.Event()
        errors: list[BaseException] = []

        def delayed_popen(*_args, **_kwargs):
            popen_entered.set()
            self.assertTrue(finish_popen.wait(2))
            return process

        def start() -> None:
            try:
                session._start_worker()
            except BaseException as exc:
                errors.append(exc)

        def latch() -> None:
            reader.latch_local_model_timeout()
            latch_finished.set()

        with (
            mock.patch.object(reader.subprocess, "Popen", side_effect=delayed_popen),
            mock.patch.object(reader.os, "killpg") as killpg,
        ):
            starter = threading.Thread(target=start)
            starter.start()
            self.assertTrue(popen_entered.wait(1))
            latcher = threading.Thread(target=latch)
            latcher.start()
            self.assertTrue(reader.local_model_timeout_latched())
            finish_popen.set()
            starter.join(timeout=2)
            latcher.join(timeout=2)

        self.assertFalse(starter.is_alive())
        self.assertFalse(latcher.is_alive())
        self.assertTrue(latch_finished.is_set())
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], RuntimeError)
        self.assertIsNone(session._process)
        killpg.assert_called_once_with(process.pid, reader.signal.SIGTERM)

    def test_unreaped_worker_is_retained_and_poison_blocks_gemma(self) -> None:
        session = reader.PaddleOCRSession(runtime={"source": "test"})
        process = mock.Mock()
        process.stdin = io.BytesIO()
        process.stdout = io.BytesIO()
        process.pid = 424247
        process.poll.return_value = None
        process.wait.side_effect = [
            reader.subprocess.TimeoutExpired("idle", 5),
            reader.subprocess.TimeoutExpired("term", 2),
            reader.subprocess.TimeoutExpired("kill", 2),
        ]
        session._process = process

        with mock.patch.object(reader.os, "killpg") as killpg:
            session.release_idle_worker()

        self.assertIs(session._process, process)
        self.assertTrue(reader.local_model_timeout_latched())
        self.assertTrue(
            any(item is process for item in reader._UNREAPED_PADDLE_PROCESSES)
        )
        self.assertEqual(
            killpg.call_args_list,
            [
                mock.call(process.pid, reader.signal.SIGTERM),
                mock.call(process.pid, reader.signal.SIGKILL),
            ],
        )
        with self.assertRaisesRegex(
            visual.VisualObservationError, "disabled after an earlier hard timeout"
        ):
            visual.observe_image(png_header(), timeout=1)

    def test_keyboard_interrupt_during_session_request_aborts_and_latches(self) -> None:
        session = reader.PaddleOCRSession(runtime={"source": "test"})
        process = mock.Mock()
        with (
            mock.patch.object(session, "_start_worker", return_value=process),
            mock.patch.object(
                session, "_run_uncached_with_process", side_effect=KeyboardInterrupt
            ),
            mock.patch.object(session, "_abort_worker") as abort,
            self.assertRaises(KeyboardInterrupt),
        ):
            session._run_uncached(
                png_header(),
                {"width_px": 80, "height_px": 40},
                timeout=1,
            )

        abort.assert_called_once_with()
        self.assertTrue(reader.local_model_timeout_latched())

    def test_session_popen_interrupt_poison_without_a_handle(self) -> None:
        session = reader.PaddleOCRSession(runtime={
            "source": "test",
            "network_sandbox": Path("/sandbox"),
            "network_profile": "deny",
            "python": Path("/python"),
            "worker": Path("/worker"),
            "model_root": Path("/models"),
            "runtime_lock": Path("/lock"),
        })
        with (
            mock.patch.object(
                reader.subprocess, "Popen", side_effect=KeyboardInterrupt
            ),
            self.assertRaises(KeyboardInterrupt),
        ):
            session._start_worker()
        self.assertTrue(reader.local_model_timeout_latched())
        self.assertIsNone(session._process)

    def test_session_interrupt_after_popen_reaps_registered_handle(self) -> None:
        class Process:
            pid = 424251
            returncode: int | None = None
            stdout = io.BytesIO()

            def __init__(self) -> None:
                self._stdin_reads = 0
                self._stdin = io.BytesIO()

            @property
            def stdin(self):
                self._stdin_reads += 1
                if self._stdin_reads == 1:
                    raise KeyboardInterrupt()
                return self._stdin

            def poll(self):
                return self.returncode

            def wait(self, *, timeout: float):
                self.returncode = -15
                return self.returncode

        process = Process()
        session = reader.PaddleOCRSession(runtime={
            "source": "test",
            "network_sandbox": Path("/sandbox"),
            "network_profile": "deny",
            "python": Path("/python"),
            "worker": Path("/worker"),
            "model_root": Path("/models"),
            "runtime_lock": Path("/lock"),
        })
        with (
            mock.patch.object(reader.subprocess, "Popen", return_value=process),
            mock.patch.object(reader.os, "killpg") as killpg,
            self.assertRaises(KeyboardInterrupt),
        ):
            session._start_worker()

        self.assertTrue(reader.local_model_timeout_latched())
        self.assertIsNone(session._process)
        killpg.assert_called_once_with(process.pid, reader.signal.SIGTERM)

    def test_interrupt_during_start_return_is_caught_by_request_scope(self) -> None:
        session = reader.PaddleOCRSession(runtime={"source": "test"})
        process = mock.Mock()
        process.pid = 424252
        process.stdin = io.BytesIO()
        process.stdout = io.BytesIO()
        process.poll.return_value = None
        process.wait.return_value = 0

        def interrupted_start(_cancel_event=None):
            session._process = process
            raise KeyboardInterrupt()

        with (
            mock.patch.object(
                session, "_start_worker", side_effect=interrupted_start
            ),
            mock.patch.object(reader.os, "killpg"),
            self.assertRaises(KeyboardInterrupt),
        ):
            session._run_uncached(
                png_header(),
                {"width_px": 80, "height_px": 40},
                timeout=1,
            )

        self.assertTrue(reader.local_model_timeout_latched())
        self.assertIsNone(session._process)

    def test_interrupt_while_closing_stdin_still_reaps_and_latches(self) -> None:
        class InterruptingStream(io.BytesIO):
            def close(self) -> None:
                raise KeyboardInterrupt()

        session = reader.PaddleOCRSession(runtime={"source": "test"})
        process = mock.Mock()
        process.pid = 424253
        process.stdin = InterruptingStream()
        process.stdout = io.BytesIO()
        process.poll.return_value = None
        process.wait.return_value = 0
        session._process = process

        with (
            mock.patch.object(reader.os, "killpg"),
            self.assertRaises(KeyboardInterrupt),
        ):
            session._abort_worker()

        self.assertTrue(reader.local_model_timeout_latched())
        self.assertIsNone(session._process)

    def test_async_close_interrupt_finishes_cleanup_before_marking_done(self) -> None:
        job = reader._PaddleAsyncJob.__new__(reader._PaddleAsyncJob)
        job._session = mock.Mock()
        job._cancel_event = mock.Mock()
        job._future = mock.Mock()
        job._future.cancel.side_effect = KeyboardInterrupt()
        job._future.done.return_value = False
        job._executor = mock.Mock()
        job._close_lock = threading.RLock()
        job._finished = False

        with self.assertRaises(KeyboardInterrupt):
            job.close()

        job._session.cancel_active_request.assert_called_once_with()
        job._executor.shutdown.assert_called_once_with(
            wait=True, cancel_futures=True
        )
        self.assertTrue(job._finished)
        self.assertTrue(reader.local_model_timeout_latched())

    def test_async_submit_interrupt_shuts_executor_and_poison_latches(self) -> None:
        executor = mock.Mock()
        executor.submit.side_effect = KeyboardInterrupt()
        session = mock.Mock()
        with (
            mock.patch.object(
                reader.concurrent.futures,
                "ThreadPoolExecutor",
                return_value=executor,
            ),
            self.assertRaises(KeyboardInterrupt),
        ):
            reader._PaddleAsyncJob(
                session,
                png_header(),
                {"width_px": 80, "height_px": 40},
                timeout=1,
            )

        executor.shutdown.assert_called_once_with(wait=True, cancel_futures=True)
        session.cancel_active_request.assert_called_once_with()
        self.assertTrue(reader.local_model_timeout_latched())

    def _live_mock_process(self, pid: int) -> mock.Mock:
        process = mock.Mock()
        process.pid = pid
        process.stdin = io.BytesIO()
        process.stdout = io.BytesIO()
        process.poll.return_value = None
        process.wait.return_value = 0
        return process

    def test_run_outer_scope_retires_worker_on_post_result_interrupt(self) -> None:
        session = reader.PaddleOCRSession(runtime={"source": "test"})
        process = self._live_mock_process(424257)

        def interrupted_impl(*_args, **_kwargs):
            session._process = process
            raise KeyboardInterrupt()

        with (
            mock.patch.object(session, "_run_impl", side_effect=interrupted_impl),
            mock.patch.object(reader.os, "killpg"),
            self.assertRaises(KeyboardInterrupt),
        ):
            session.run(
                png_header(),
                {"width_px": 80, "height_px": 40},
                timeout=1,
            )
        self.assertTrue(reader.local_model_timeout_latched())
        self.assertIsNone(session._process)

    def test_release_outer_scope_retires_worker_on_capture_interrupt(self) -> None:
        session = reader.PaddleOCRSession(runtime={"source": "test"})
        process = self._live_mock_process(424258)

        def interrupted_impl():
            session._process = process
            raise KeyboardInterrupt()

        with (
            mock.patch.object(
                session,
                "_release_idle_worker_impl",
                side_effect=interrupted_impl,
            ),
            mock.patch.object(reader.os, "killpg"),
            self.assertRaises(KeyboardInterrupt),
        ):
            session.release_idle_worker()
        self.assertTrue(reader.local_model_timeout_latched())
        self.assertIsNone(session._process)

    def test_close_outer_scope_retires_worker_on_capture_interrupt(self) -> None:
        session = reader.PaddleOCRSession(runtime={"source": "test"})
        process = self._live_mock_process(424259)

        def interrupted_impl():
            session._process = process
            session._closed = True
            raise KeyboardInterrupt()

        with (
            mock.patch.object(session, "_close_impl", side_effect=interrupted_impl),
            mock.patch.object(reader.os, "killpg"),
            self.assertRaises(KeyboardInterrupt),
        ):
            session.close()
        self.assertTrue(reader.local_model_timeout_latched())
        self.assertIsNone(session._process)

    def test_release_idle_worker_keeps_memo_and_allows_later_restart(self) -> None:
        runtime = {
            "source": "test",
            "network_sandbox": Path("/sandbox"),
            "network_profile": "deny",
            "python": Path("/python"),
            "worker": Path("/worker"),
            "model_root": Path("/models"),
            "runtime_lock": Path("/lock"),
        }
        session = reader.PaddleOCRSession(runtime=runtime)
        key = ("a" * 64, 1, 1, 1)
        session._cache[key] = (paddle_result(), 10)
        session._cache_bytes = 10
        old_process = mock.Mock()
        old_process.stdin = io.BytesIO()
        old_process.stdout = io.BytesIO()
        old_process.poll.return_value = 0
        session._process = old_process

        session.release_idle_worker()

        self.assertIsNone(session._process)
        self.assertFalse(session._closed)
        self.assertIn(key, session._cache)
        self.assertEqual(session._cache_bytes, 10)
        new_process = mock.Mock()
        new_process.stdin = io.BytesIO()
        new_process.stdout = io.BytesIO()
        new_process.poll.return_value = 0
        with mock.patch.object(
            reader.subprocess, "Popen", return_value=new_process
        ) as popen:
            restarted = session._start_worker()
        self.assertIs(restarted, new_process)
        popen.assert_called_once()
        session.release_idle_worker()

    def test_response_reader_accepts_partial_pipe_writes_without_blocking(self) -> None:
        session = reader.PaddleOCRSession(runtime={"source": "test"})
        read_fd, write_fd = os.pipe()

        class Process:
            stdout = os.fdopen(read_fd, "rb", buffering=0)

            @staticmethod
            def poll():
                return None

        def write_parts() -> None:
            try:
                os.write(write_fd, b'{"partial":')
                time.sleep(0.01)
                os.write(write_fd, b'true}\n')
            finally:
                os.close(write_fd)

        writer = threading.Thread(target=write_parts)
        writer.start()
        try:
            raw = session._read_response_line(Process(), timeout=1)
        finally:
            Process.stdout.close()
            writer.join(timeout=1)
        self.assertEqual(raw, b'{"partial":true}\n')

    def test_response_reader_times_out_on_silent_pipe(self) -> None:
        session = reader.PaddleOCRSession(runtime={"source": "test"})
        read_fd, write_fd = os.pipe()

        class Process:
            stdout = os.fdopen(read_fd, "rb", buffering=0)

            @staticmethod
            def poll():
                return None

        try:
            with self.assertRaises(reader.subprocess.TimeoutExpired):
                session._read_response_line(Process(), timeout=0.01)
        finally:
            Process.stdout.close()
            os.close(write_fd)


class PaddleOverlapSchedulingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="aiec-paddle-overlap-")
        self.image = Path(self.temporary.name) / "image.png"
        self.image.write_bytes(png_header())

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def canonicalization(raw: bytes) -> dict[str, object]:
        dimensions = {"width_px": 80, "height_px": 40}
        return {
            "status": "completed",
            "method": "test",
            "runner": reader.CANONICALIZER_RUNNER,
            "runner_version": reader.CANONICALIZER_VERSION,
            "source_orientation": 1,
            "source_dimensions": dimensions,
            "canonical_orientation": 1,
            "canonical_dimensions": dimensions,
            "canonical_sha256": hashlib.sha256(raw).hexdigest(),
            "format": "PNG",
            "color_space": "sRGB",
            "pixel_format": "RGBA8",
            "alpha_policy": "flattened_on_white",
            "source_sha256": hashlib.sha256(raw).hexdigest(),
            "build": {},
        }

    def common_patches(self, session: mock.Mock):
        raw = self.image.read_bytes()
        return (
            mock.patch.object(
                reader,
                "canonicalize_image_bytes",
                return_value=(raw, self.canonicalization(raw)),
            ),
            mock.patch.object(
                reader.ocr, "resolve_vision_binary", return_value=Path("/vision")
            ),
            mock.patch.object(
                reader.ocr,
                "verify_tesseract",
                side_effect=FileNotFoundError("not installed"),
            ),
            reader.activate_paddle_session(session),
        )

    def test_allowed_session_starts_paddle_before_vision(self) -> None:
        started = threading.Event()
        vision_observed_start = threading.Event()
        session = mock.Mock(
            overlap_allowed=True,
            overlap_gate_reason="physical_memory_at_least_48_gib",
            runtime_source="test",
        )

        def run_paddle(*_args, **_kwargs):
            started.set()
            if not vision_observed_start.wait(2):
                raise AssertionError("Vision did not overlap the Paddle request")
            return paddle_result("needs_review")

        def run_vision(*_args, **_kwargs):
            if not started.wait(2):
                raise AssertionError("Paddle did not start after canonicalization")
            vision_observed_start.set()
            return "completed", [], [], None, 1

        session.run.side_effect = run_paddle
        patches = self.common_patches(session)
        with ExitStack() as stack:
            for patch in patches:
                stack.enter_context(patch)
            stack.enter_context(
                mock.patch.object(
                    reader, "_run_vision_pass", side_effect=run_vision
                )
            )
            result = reader.extract(self.image, allow_vlm=False)

        self.assertEqual(session.run.call_count, 1)
        self.assertEqual(
            result["engines"]["paddleocr"]["execution_mode"],
            "overlapped_with_vision_tesseract",
        )
        self.assertFalse(result["engines"]["paddleocr"]["cache_hit"])

    def test_24_gib_gate_keeps_heavy_ocr_serial(self) -> None:
        paddle_started = threading.Event()
        session = mock.Mock(
            overlap_allowed=False,
            overlap_gate_reason="physical_memory_below_48_gib",
            runtime_source="test",
        )
        session.run.side_effect = lambda *_args, **_kwargs: (
            paddle_started.set(), paddle_result("needs_review")
        )[1]

        def run_vision(*_args, **_kwargs):
            self.assertFalse(paddle_started.is_set())
            return "completed", [], [], None, 1

        patches = self.common_patches(session)
        with ExitStack() as stack:
            for patch in patches:
                stack.enter_context(patch)
            stack.enter_context(
                mock.patch.object(
                    reader, "_run_vision_pass", side_effect=run_vision
                )
            )
            result = reader.extract(self.image, allow_vlm=False)

        self.assertTrue(paddle_started.is_set())
        self.assertEqual(
            result["engines"]["paddleocr"]["execution_mode"],
            "serial_resource_gate",
        )
        self.assertEqual(
            result["engines"]["paddleocr"]["overlap_gate_reason"],
            "physical_memory_below_48_gib",
        )

    def test_intermediate_exception_explicitly_cancels_async_job(self) -> None:
        release = threading.Event()
        started = threading.Event()
        session = mock.Mock(
            overlap_allowed=True,
            overlap_gate_reason="test_resources_sufficient",
            runtime_source="test",
        )

        def run_paddle(*_args, **_kwargs):
            started.set()
            if not release.wait(2):
                raise AssertionError("async job was not explicitly cancelled")
            return paddle_result("needs_review")

        session.run.side_effect = run_paddle
        session.cancel_active_request.side_effect = release.set
        patches = self.common_patches(session)
        with ExitStack() as stack:
            for patch in patches:
                stack.enter_context(patch)
            stack.enter_context(mock.patch.object(
                reader,
                "_run_vision_pass",
                return_value=("completed", [], [], None, 1),
            ))
            stack.enter_context(mock.patch.object(
                reader,
                "_match_lines",
                side_effect=RuntimeError("intermediate failure"),
            ))
            with self.assertRaisesRegex(RuntimeError, "intermediate failure"):
                reader.extract(self.image, allow_vlm=False)

        self.assertTrue(started.is_set())
        session.cancel_active_request.assert_called()

    def test_idle_paddle_worker_is_released_before_gemma_fallback(self) -> None:
        events: list[str] = []
        session = mock.Mock(
            overlap_allowed=False,
            overlap_gate_reason="explicit_serial_policy",
            runtime_source="test",
        )
        session.run.return_value = paddle_result("needs_review")
        session.release_idle_worker.side_effect = lambda: events.append(
            "paddle_released"
        )

        def fallback(*_args, **_kwargs):
            events.append("gemma_started")
            return {
                "transcript": "中野",
                "model_digest": "sha256:" + "b" * 64,
                "prompt_sha256": reader.UNLOCATED_TRANSCRIPT_PROMPT_SHA256,
            }

        patches = self.common_patches(session)
        with ExitStack() as stack:
            for patch in patches:
                stack.enter_context(patch)
            stack.enter_context(mock.patch.object(
                reader,
                "_run_vision_pass",
                return_value=("completed", [], [], None, 1),
            ))
            stack.enter_context(mock.patch.object(
                reader,
                "run_unlocated_transcript_fallback",
                side_effect=fallback,
            ))
            result = reader.extract(self.image)

        self.assertEqual(events, ["paddle_released", "gemma_started"])
        self.assertEqual(result["unlocated_transcript"]["transcript"], "中野")

    def test_probe_releases_idle_paddle_before_visual_gemma_observation(self) -> None:
        events: list[str] = []
        session = mock.Mock()
        session.release_idle_worker.side_effect = lambda: events.append(
            "paddle_released"
        )
        visual_module = types.ModuleType("local_visual_observation")

        def observe_path(*_args, **_kwargs):
            events.append("gemma_started")
            raise RuntimeError("stop after ordering assertion")

        visual_module.observe_path = observe_path
        probe = mock.Mock()
        with (
            reader.activate_paddle_session(session),
            mock.patch.dict(
                sys.modules,
                {"local_visual_observation": visual_module},
            ),
        ):
            result = builder.Probe._add_local_visual_observation(
                probe,
                self.image,
                {"document_id": "doc_fixture"},
                parent_id="ev_fixture",
                location_prefix={"object_index": 1},
                visual_origin={"kind": "standalone_image"},
                ordinal=1,
            )

        self.assertFalse(result)
        self.assertEqual(events, ["paddle_released", "gemma_started"])
        probe.mark_partial.assert_called_once()


class DeferredPerDocumentVisualTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="aiec-visual-phases-")
        self.root = Path(self.temporary.name)
        self.source = self.root / "source.txt"
        self.source.write_text("source", encoding="utf-8")
        self.first = self.root / "first.png"
        self.second = self.root / "second.png"
        self.first.write_bytes(png_header() + b"first")
        self.second.write_bytes(png_header() + b"second")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def origin(path: Path, *, kind: str = "test_image") -> dict[str, object]:
        raw = path.read_bytes()
        return {
            "kind": kind,
            "source_relative_path": "source.txt",
            "source_sha256": "d" * 64,
            "source_location": {"object_index": 1},
            "materialization": {
                "rendered_sha256": hashlib.sha256(raw).hexdigest(),
                "rendered_size_bytes": len(raw),
                "external_network_used": False,
            },
        }

    def test_root_runs_all_ocr_before_one_release_and_ordered_gemma(self) -> None:
        probe = records.Probe(
            self.root,
            "2031-01-01T00:00:00+00:00",
            None,
            diagnostic=False,
            visual_observation_mode="deferred_per_document",
        )
        events: list[str] = []
        spool_roots: list[Path] = []

        def handler(_path: Path) -> None:
            document = probe.add_document(self.source, "fixture")
            events.append("ocr1")
            self.assertTrue(probe._schedule_local_visual_observation(
                self.first,
                document,
                parent_id="ev_image_1",
                location_prefix={"object_index": 1},
                visual_origin=self.origin(self.first),
                ordinal=1,
            ))
            spool_roots.append(probe._visual_spool_root)
            events.append("ocr2")
            self.assertTrue(probe._schedule_local_visual_observation(
                self.second,
                document,
                parent_id="ev_image_2",
                location_prefix={"object_index": 2},
                visual_origin=self.origin(self.second),
                ordinal=1,
            ))

        def observe(raw: bytes, *, expected_input_sha256: str, timeout: float):
            self.assertEqual(hashlib.sha256(raw).hexdigest(), expected_input_sha256)
            self.assertGreater(timeout, 0)
            event = "gemma1" if raw.endswith(b"first") else "gemma2"
            events.append(event)
            return visual_result(raw)

        session = mock.Mock()
        session.release_idle_worker.side_effect = lambda: events.append("release")
        with (
            mock.patch.object(probe, "extract_plain_text", side_effect=handler),
            mock.patch(
                "local_visual_observation.observe_image", side_effect=observe
            ),
            reader.activate_paddle_session(session),
        ):
            probe.extract(self.source)

        self.assertEqual(events, ["ocr1", "ocr2", "release", "gemma1", "gemma2"])
        session.release_idle_worker.assert_called_once_with()
        self.assertEqual(len([
            item for item in probe.evidence
            if item.get("provenance", {}).get("extraction_method")
            == "local_vlm_visual_observation_provisional"
        ]), 2)
        self.assertIsNone(probe._visual_spool_root)
        self.assertEqual(probe._deferred_visual_tasks, [])
        self.assertTrue(spool_roots[0] is not None)
        self.assertFalse(spool_roots[0].exists())

    def test_child_task_is_rebound_to_projected_parent_location_and_origin(self) -> None:
        parent = records.Probe(
            self.root,
            "2031-01-01T00:00:00+00:00",
            None,
            diagnostic=False,
            visual_observation_mode="deferred_per_document",
        )
        document = parent.add_document(self.source, "fixture")
        raw = self.first.read_bytes()
        materialization = {
            "rendered_sha256": hashlib.sha256(raw).hexdigest(),
            "rendered_size_bytes": len(raw),
            "external_network_used": False,
        }

        def child_extract(child: records.Probe, image_path: Path) -> None:
            self.assertEqual(child.visual_observation_mode, "suppressed")
            child_document = child.add_document(image_path, "fixture-image")
            image = child.add_evidence(
                child_document["document_id"],
                "image",
                {"object_index": 1},
                records.content(content_ref=image_path.name, mime_type="image/png"),
                ordinal=1,
                native_properties={
                    "source_sha256": hashlib.sha256(image_path.read_bytes()).hexdigest(),
                },
            )
            child.contain_document(child_document["document_id"], image["evidence_id"])
            child._schedule_local_visual_observation(
                image_path,
                child_document,
                parent_id=image["evidence_id"],
                location_prefix={"object_index": 1},
                visual_origin={"kind": "standalone_image"},
                ordinal=2,
            )
            child.finalize_document()

        try:
            with mock.patch.object(
                records.Probe, "extract", autospec=True, side_effect=child_extract
            ):
                projected = parent._project_local_image_evidence(
                    self.first,
                    document,
                    parent_id="ev_page",
                    location_prefix={"source_member": "word/media/image1.png", "object_index": 3},
                    content_ref="source.txt#image=1",
                    visual_origin_kind="office_embedded_image",
                    materialization=materialization,
                )
            self.assertEqual(projected, 1)
            self.assertEqual(len(parent._deferred_visual_tasks), 1)
            task = parent._deferred_visual_tasks[0]
            projected_image = next(
                item for item in parent.evidence if item["evidence_type"] == "image"
            )
            self.assertEqual(task["parent_id"], projected_image["evidence_id"])
            self.assertEqual(task["ordinal"], 2)
            self.assertEqual(task["location"], {
                "source_member": "word/media/image1.png",
                "image_object_index": 3,
                "object_index": 1,
                "locator_text": "visual_observation=whole_image",
            })
            self.assertEqual(task["visual_origin"]["kind"], "office_embedded_image")
            self.assertEqual(
                task["visual_origin"]["source_relative_path"], self.source.name
            )
            self.assertEqual(
                task["visual_origin"]["source_location"],
                projected_image["location"],
            )
        finally:
            parent._cleanup_visual_spool()

    def test_spool_symlink_tamper_fails_closed_and_extract_cleans_up(self) -> None:
        probe = records.Probe(
            self.root,
            "2031-01-01T00:00:00+00:00",
            None,
            diagnostic=False,
            visual_observation_mode="deferred_per_document",
        )
        spool_roots: list[Path] = []

        def handler(_path: Path) -> None:
            document = probe.add_document(self.source, "fixture")
            probe._schedule_local_visual_observation(
                self.first,
                document,
                parent_id="ev_image",
                location_prefix={"object_index": 1},
                visual_origin=self.origin(self.first),
                ordinal=1,
            )
            spool = probe._deferred_visual_tasks[0]["spool_path"]
            spool_roots.append(spool.parent)
            spool.unlink()
            spool.symlink_to(self.second)

        with mock.patch.object(probe, "extract_plain_text", side_effect=handler):
            with self.assertRaisesRegex(
                records.DeferredVisualStoreError, "cannot be read safely"
            ):
                probe.extract(self.source)

        self.assertEqual(len(spool_roots), 1)
        self.assertFalse(spool_roots[0].exists())
        self.assertTrue(self.first.exists())
        self.assertTrue(self.second.exists())

    def test_projected_ocr_is_bound_to_materialized_image_sha256(self) -> None:
        parent = records.Probe(
            self.root,
            "2031-01-01T00:00:00+00:00",
            None,
            diagnostic=False,
            visual_observation_mode="deferred_per_document",
        )
        document = parent.add_document(self.source, "fixture")
        raw = self.first.read_bytes()
        materialization = {
            "rendered_sha256": hashlib.sha256(raw).hexdigest(),
            "rendered_size_bytes": len(raw),
            "external_network_used": False,
        }

        def mismatched_child_extract(child: records.Probe, image_path: Path) -> None:
            child_document = child.add_document(image_path, "fixture-image")
            image = child.add_evidence(
                child_document["document_id"],
                "image",
                {"object_index": 1},
                records.content(content_ref=image_path.name, mime_type="image/png"),
                ordinal=1,
                native_properties={
                    "source_sha256": hashlib.sha256(b"different image bytes").hexdigest(),
                },
            )
            child.add_evidence(
                child_document["document_id"],
                "ocr_line",
                {"object_index": 1},
                records.content(raw_text="MALICIOUS"),
                parent_id=image["evidence_id"],
                ordinal=1,
            )
            child.finalize_document()

        with mock.patch.object(
            records.Probe,
            "extract",
            autospec=True,
            side_effect=mismatched_child_extract,
        ):
            with self.assertRaisesRegex(
                records.DeferredVisualStoreError,
                "local image reader output differs from the materialization contract",
            ):
                parent._project_local_image_evidence(
                    self.first,
                    document,
                    parent_id="ev_page",
                    location_prefix={"object_index": 1},
                    content_ref="source.txt#image=1",
                    visual_origin_kind="pdf_page_image",
                    materialization=materialization,
                )

        self.assertFalse(any(
            item.get("content", {}).get("raw_text") == "MALICIOUS"
            for item in parent.evidence
        ))
        self.assertEqual(parent._deferred_visual_tasks, [])

    def test_forged_oversized_contract_is_hard_failure(self) -> None:
        probe = records.Probe(
            self.root,
            "2031-01-01T00:00:00+00:00",
            None,
            diagnostic=False,
            visual_observation_mode="deferred_per_document",
        )
        document = probe.add_document(self.source, "fixture")
        origin = self.origin(self.first)
        origin["materialization"]["rendered_size_bytes"] = (
            visual.MAX_IMAGE_BYTES + 1
        )
        with self.assertRaisesRegex(
            records.DeferredVisualStoreError,
            "differs from its materialization contract",
        ):
            probe._schedule_local_visual_observation(
                self.first,
                document,
                parent_id="ev_image",
                location_prefix={"object_index": 1},
                visual_origin=origin,
                ordinal=1,
            )

        self.assertEqual(document["extraction"]["status"], "success")
        self.assertEqual(probe._deferred_visual_tasks, [])
        self.assertIsNone(probe._visual_spool_root)

    def test_matching_vlm_input_limit_is_partial_skip_not_hard_failure(self) -> None:
        large = self.root / "large.png"
        raw = png_header() + b"x" * (visual.MAX_IMAGE_BYTES + 1 - len(png_header()))
        large.write_bytes(raw)
        probe = records.Probe(
            self.root,
            "2031-01-01T00:00:00+00:00",
            None,
            diagnostic=False,
            visual_observation_mode="deferred_per_document",
        )
        document = probe.add_document(self.source, "fixture")
        queued = probe._schedule_local_visual_observation(
            large,
            document,
            parent_id="ev_image",
            location_prefix={"object_index": 1},
            visual_origin=self.origin(large),
            ordinal=1,
        )

        self.assertFalse(queued)
        self.assertEqual(document["extraction"]["status"], "partial")
        self.assertEqual(probe._deferred_visual_tasks, [])
        self.assertIsNone(probe._visual_spool_root)

    def test_visual_source_read_safety_failure_is_hard(self) -> None:
        probe = records.Probe(
            self.root,
            "2031-01-01T00:00:00+00:00",
            None,
            diagnostic=False,
            visual_observation_mode="deferred_per_document",
        )
        document = probe.add_document(self.source, "fixture")
        with mock.patch(
            "local_image_ocr.read_checked_image_bytes",
            side_effect=ValueError("fixture source mutation"),
        ):
            with self.assertRaisesRegex(
                records.DeferredVisualStoreError,
                "visual source cannot be read safely",
            ):
                probe._schedule_local_visual_observation(
                    self.first,
                    document,
                    parent_id="ev_image",
                    location_prefix={"object_index": 1},
                    visual_origin=self.origin(self.first),
                    ordinal=1,
                )

        self.assertEqual(document["extraction"]["status"], "success")
        self.assertEqual(probe._deferred_visual_tasks, [])
        self.assertIsNone(probe._visual_spool_root)

    def test_cleanup_refuses_replaced_root_without_deleting_replacement(self) -> None:
        probe = records.Probe(
            self.root,
            "2031-01-01T00:00:00+00:00",
            None,
            diagnostic=False,
            visual_observation_mode="deferred_per_document",
        )
        document = probe.add_document(self.source, "fixture")
        self.assertTrue(probe._schedule_local_visual_observation(
            self.first,
            document,
            parent_id="ev_image",
            location_prefix={"object_index": 1},
            visual_origin=self.origin(self.first),
            ordinal=1,
        ))
        spool_root = probe._visual_spool_root
        self.assertIsNotNone(spool_root)
        held_root = spool_root.with_name(f"{spool_root.name}-held")
        spool_root.rename(held_root)
        spool_root.mkdir(mode=0o700)
        sentinel = spool_root / "must-not-be-deleted.txt"
        sentinel.write_text("preserve replacement", encoding="utf-8")
        try:
            with self.assertRaisesRegex(
                records.DeferredVisualStoreError,
                "root identity changed before cleanup",
            ):
                probe._cleanup_visual_spool()
            self.assertEqual(
                sentinel.read_text(encoding="utf-8"),
                "preserve replacement",
            )
            self.assertTrue(held_root.is_dir())
            self.assertEqual(probe._visual_spool_root, spool_root)
            self.assertTrue(probe._visual_spool_by_sha256)
        finally:
            shutil.rmtree(spool_root, ignore_errors=True)
            shutil.rmtree(held_root, ignore_errors=True)
            probe._deferred_visual_tasks.clear()
            probe._visual_spool_by_sha256.clear()
            probe._visual_spool_bytes = 0
            probe._visual_spool_root = None
            probe._visual_spool_root_identity = None

    def test_spool_write_failure_unlinks_only_from_opened_root_descriptor(self) -> None:
        probe = records.Probe(
            self.root,
            "2031-01-01T00:00:00+00:00",
            None,
            diagnostic=False,
            visual_observation_mode="deferred_per_document",
        )
        raw = self.first.read_bytes()
        sha256 = hashlib.sha256(raw).hexdigest()
        held_root: Path | None = None
        replacement_root: Path | None = None
        sentinel: Path | None = None

        def replace_root_then_fail(_descriptor: int, _raw: bytes) -> None:
            nonlocal held_root, replacement_root, sentinel
            replacement_root = probe._visual_spool_root
            self.assertIsNotNone(replacement_root)
            held_root = replacement_root.with_name(
                f"{replacement_root.name}-held-write"
            )
            replacement_root.rename(held_root)
            replacement_root.mkdir(mode=0o700)
            sentinel = replacement_root / "must-not-be-unlinked.txt"
            sentinel.write_text("replacement", encoding="utf-8")
            raise OSError("fixture short write")

        try:
            with (
                mock.patch.object(
                    probe,
                    "_write_all_descriptor",
                    side_effect=replace_root_then_fail,
                ),
                self.assertRaisesRegex(
                    records.DeferredVisualStoreError,
                    "write could not be completed safely",
                ),
            ):
                probe._spool_visual_bytes(raw, sha256)

            self.assertIsNotNone(held_root)
            self.assertEqual(list(held_root.iterdir()), [])
            self.assertIsNotNone(sentinel)
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "replacement")
        finally:
            if replacement_root is not None:
                shutil.rmtree(replacement_root, ignore_errors=True)
            if held_root is not None:
                shutil.rmtree(held_root, ignore_errors=True)
            probe._deferred_visual_tasks.clear()
            probe._visual_spool_by_sha256.clear()
            probe._visual_spool_bytes = 0
            probe._visual_spool_root = None
            probe._visual_spool_root_identity = None

    def test_visual_deadline_expiring_after_queue_skips_before_gemma(self) -> None:
        probe = records.Probe(
            self.root,
            "2031-01-01T00:00:00+00:00",
            None,
            diagnostic=False,
            visual_observation_mode="deferred_per_document",
        )
        document = probe.add_document(self.source, "fixture")
        deadline = time.monotonic() + 60
        try:
            self.assertTrue(probe._schedule_local_visual_observation(
                self.first,
                document,
                parent_id="ev_image",
                location_prefix={"object_index": 1},
                visual_origin=self.origin(self.first),
                ordinal=1,
                deadline_at=deadline,
            ))
            with (
                mock.patch.object(records.time, "monotonic", return_value=deadline + 1),
                mock.patch("local_visual_observation.observe_image") as observe,
            ):
                probe._flush_deferred_visual_observations()
            observe.assert_not_called()
            self.assertEqual(document["extraction"]["status"], "partial")
        finally:
            probe._cleanup_visual_spool()

    def test_each_source_releases_before_its_visual_phase(self) -> None:
        second_source = self.root / "source-2.txt"
        second_source.write_text("source-2", encoding="utf-8")
        events: list[str] = []
        session = mock.Mock()
        session.release_idle_worker.side_effect = lambda: events.append("release")

        def observe(raw: bytes, **_kwargs):
            events.append("gemma")
            return visual_result(raw)

        with (
            reader.activate_paddle_session(session),
            mock.patch("local_visual_observation.observe_image", side_effect=observe),
        ):
            for source in (self.source, second_source):
                probe = records.Probe(
                    self.root,
                    "2031-01-01T00:00:00+00:00",
                    None,
                    diagnostic=False,
                    visual_observation_mode="deferred_per_document",
                )

                def handler(_path: Path, *, current=source, owner=probe) -> None:
                    document = owner.add_document(current, "fixture")
                    events.append("ocr")
                    owner._schedule_local_visual_observation(
                        self.first,
                        document,
                        parent_id="ev_image",
                        location_prefix={"object_index": 1},
                        visual_origin=self.origin(self.first),
                        ordinal=1,
                    )

                with mock.patch.object(
                    probe, "extract_plain_text", side_effect=handler
                ):
                    probe.extract(source)

        self.assertEqual(
            events,
            ["ocr", "release", "gemma", "ocr", "release", "gemma"],
        )
        self.assertEqual(session.release_idle_worker.call_count, 2)

    def test_second_visual_model_error_keeps_first_and_cleans_spool(self) -> None:
        probe = records.Probe(
            self.root,
            "2031-01-01T00:00:00+00:00",
            None,
            diagnostic=False,
            visual_observation_mode="deferred_per_document",
        )
        spool_roots: list[Path] = []

        def handler(_path: Path) -> None:
            document = probe.add_document(self.source, "fixture")
            for index, image in enumerate((self.first, self.second), 1):
                probe._schedule_local_visual_observation(
                    image,
                    document,
                    parent_id=f"ev_image_{index}",
                    location_prefix={"object_index": index},
                    visual_origin=self.origin(image),
                    ordinal=1,
                )
            spool_roots.append(probe._visual_spool_root)

        calls = 0

        def observe(raw: bytes, **_kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("fixture Gemma failure")
            return visual_result(raw)

        with (
            mock.patch.object(probe, "extract_plain_text", side_effect=handler),
            mock.patch("local_visual_observation.observe_image", side_effect=observe),
        ):
            probe.extract(self.source)

        retained = [
            item for item in probe.evidence
            if item.get("provenance", {}).get("extraction_method")
            == "local_vlm_visual_observation_provisional"
        ]
        self.assertEqual(len(retained), 1)
        self.assertEqual(probe.documents[0]["extraction"]["status"], "partial")
        self.assertTrue(any(
            "fixture Gemma failure" in warning
            for warning in probe.documents[0]["extraction"]["warnings"]
        ))
        self.assertFalse(spool_roots[0].exists())

    def test_spool_hash_tamper_rolls_back_process_file(self) -> None:
        output = self.root / "rollback-out"

        def handler(probe: records.Probe, path: Path) -> None:
            document = probe.add_document(path, "fixture")
            text = probe.add_evidence(
                document["document_id"],
                "text_block",
                {"object_index": 1},
                records.content(raw_text="must be rolled back"),
                ordinal=1,
            )
            probe.contain_document(document["document_id"], text["evidence_id"])
            probe._schedule_local_visual_observation(
                self.first,
                document,
                parent_id=text["evidence_id"],
                location_prefix={"object_index": 1},
                visual_origin=self.origin(self.first),
                ordinal=2,
            )
            spool = probe._deferred_visual_tasks[0]["spool_path"]
            mutated = bytearray(spool.read_bytes())
            mutated[-1] ^= 1
            spool.write_bytes(mutated)

        with mock.patch.object(
            records.Probe, "extract_plain_text", autospec=True, side_effect=handler
        ):
            entry, error = builder.process_file(
                output,
                self.root,
                self.source,
                "2031-01-01T00:00:00+00:00",
                hashlib.sha256(self.source.read_bytes()).hexdigest(),
                (),
            )

        self.assertIsInstance(error, records.DeferredVisualStoreError)
        self.assertEqual(entry["status"], "failed")
        self.assertEqual(entry["shards"]["evidence"]["record_count"], 0)
        self.assertEqual(entry["shards"]["relations"]["record_count"], 0)
        document_path = output / entry["shards"]["documents"]["relative_path"]
        failed = json.loads(document_path.read_text(encoding="utf-8"))
        self.assertEqual(failed["extraction"]["status"], "failed")
        self.assertIn("DeferredVisualStoreError", failed["extraction"]["errors"][0])

    def test_embedded_temp_is_spooled_then_flushed_with_parent_identity(self) -> None:
        parent = records.Probe(
            self.root,
            "2031-01-01T00:00:00+00:00",
            None,
            diagnostic=False,
            visual_observation_mode="deferred_per_document",
        )
        document = parent.add_document(self.source, "fixture")
        container = parent.add_evidence(
            document["document_id"],
            "image",
            {"source_member": "word/media/image1.png", "object_index": 5},
            records.content(content_ref="source.txt#embedded=1", mime_type="image/png"),
            ordinal=1,
        )
        raw = self.first.read_bytes()

        def child_extract(child: records.Probe, image_path: Path) -> None:
            child_document = child.add_document(image_path, "fixture-image")
            image = child.add_evidence(
                child_document["document_id"],
                "image",
                {"object_index": 1},
                records.content(content_ref=image_path.name, mime_type="image/png"),
                ordinal=1,
                native_properties={
                    "source_sha256": hashlib.sha256(image_path.read_bytes()).hexdigest(),
                },
            )
            child._schedule_local_visual_observation(
                image_path,
                child_document,
                parent_id=image["evidence_id"],
                location_prefix={"object_index": 1},
                visual_origin={"kind": "standalone_image"},
                ordinal=2,
            )
            child.finalize_document()

        observed: list[bytes] = []

        def observe(image_bytes: bytes, **_kwargs):
            observed.append(image_bytes)
            return visual_result(image_bytes)

        try:
            with (
                mock.patch.object(
                    records.Probe, "extract", autospec=True, side_effect=child_extract
                ),
                mock.patch.object(records.time, "monotonic", return_value=100.0),
            ):
                projected = parent._project_embedded_image_bytes(
                    raw,
                    document,
                    parent_id=container["evidence_id"],
                    location_prefix={
                        "source_member": "word/media/image1.png",
                        "object_index": 5,
                    },
                    content_ref="source.txt#embedded=1",
                    source_name="image1.png",
                )
                self.assertEqual(projected, 1)
                task = parent._deferred_visual_tasks[0]
                self.assertFalse(task["image_path"].exists())
                self.assertEqual(task["deadline_at"], 1000.0)
                with mock.patch(
                    "local_visual_observation.observe_image", side_effect=observe
                ):
                    parent._flush_deferred_visual_observations()
            self.assertEqual(observed, [raw])
            visual = next(
                item for item in parent.evidence
                if item.get("provenance", {}).get("extraction_method")
                == "local_vlm_visual_observation_provisional"
            )
            self.assertEqual(visual["parent_evidence_id"], container["evidence_id"])
            self.assertEqual(visual["ordinal"], 2)
            self.assertEqual(visual["location"], {
                "source_member": "word/media/image1.png",
                "image_object_index": 5,
                "object_index": 1,
                "locator_text": "visual_observation=whole_image",
            })
            origin = visual["native_properties"]["visual_origin"]
            self.assertEqual(origin["kind"], "office_embedded_image")
            self.assertEqual(origin["source_relative_path"], self.source.name)
            self.assertEqual(origin["materialization"]["rendered_sha256"], hashlib.sha256(raw).hexdigest())
        finally:
            parent._cleanup_visual_spool()

    def test_embedded_store_contract_error_escapes_partial_wrapper(self) -> None:
        parent = records.Probe(
            self.root,
            "2031-01-01T00:00:00+00:00",
            None,
            diagnostic=False,
            visual_observation_mode="deferred_per_document",
        )
        document = parent.add_document(self.source, "fixture")
        with mock.patch.object(
            parent,
            "_project_local_image_evidence",
            side_effect=records.DeferredVisualStoreError("fixture contract failure"),
        ):
            with self.assertRaisesRegex(
                records.DeferredVisualStoreError, "fixture contract failure"
            ):
                parent._project_embedded_image_bytes(
                    self.first.read_bytes(),
                    document,
                    parent_id="ev_image",
                    location_prefix={"object_index": 1},
                    content_ref="source.txt#embedded=1",
                    source_name="image1.png",
                )

    def test_text_only_deferred_document_starts_no_visual_phase(self) -> None:
        probe = records.Probe(
            self.root,
            "2031-01-01T00:00:00+00:00",
            None,
            diagnostic=False,
            visual_observation_mode="deferred_per_document",
        )
        session = mock.Mock()
        with (
            reader.activate_paddle_session(session),
            mock.patch("local_visual_observation.observe_image") as observe,
        ):
            probe.extract(self.source)
        session.release_idle_worker.assert_not_called()
        observe.assert_not_called()
        self.assertIsNone(probe._visual_spool_root)
        self.assertEqual(probe._deferred_visual_tasks, [])

    def test_builder_root_explicitly_selects_deferred_mode(self) -> None:
        output = self.root / "out"
        fake_probe = mock.Mock()

        def extract(_path: Path) -> None:
            sink = probe_factory.call_args.kwargs["record_sink"]
            source_sha256 = hashlib.sha256(self.source.read_bytes()).hexdigest()
            sink("documents", {
                "document_id": records.stable_id(
                    "doc",
                    {"relative_path": self.source.name, "source_sha256": source_sha256},
                ),
                "source": {"sha256": source_sha256},
                "extraction": {"status": "success"},
            })

        fake_probe.extract.side_effect = extract
        with mock.patch.object(builder, "Probe", return_value=fake_probe) as probe_factory:
            entry, error = builder.process_file(
                output,
                self.root,
                self.source,
                "2031-01-01T00:00:00+00:00",
                hashlib.sha256(self.source.read_bytes()).hexdigest(),
                (),
            )
        self.assertIsNone(error)
        self.assertEqual(entry["status"], "success")
        self.assertEqual(
            probe_factory.call_args.kwargs["visual_observation_mode"],
            "deferred_per_document",
        )


class PaddleSessionProtocolTests(unittest.TestCase):
    def test_request_rejects_symlink_and_oversize_dimensions(self) -> None:
        with tempfile.TemporaryDirectory(prefix="aiec-paddle-protocol-") as temporary:
            root = Path(temporary)
            source = root / "source.png"
            source.write_bytes(png_header())
            alias = root / "alias.png"
            alias.symlink_to(source)
            request = worker.session_request(
                "request-1",
                alias,
                hashlib.sha256(source.read_bytes()).hexdigest(),
                {"width_px": 80, "height_px": 40},
            )
            with self.assertRaisesRegex(ValueError, "non-symlink"):
                worker.validate_session_request(request)

            request = worker.session_request(
                "request-2",
                source,
                hashlib.sha256(source.read_bytes()).hexdigest(),
                {"width_px": worker.MAX_IMAGE_PIXELS + 1, "height_px": 1},
            )
            with self.assertRaisesRegex(ValueError, "dimensions"):
                worker.validate_session_request(request)

    def test_response_hash_binds_request_input_and_result(self) -> None:
        input_metadata = {
            "sha256": "b" * 64,
            "width_px": 80,
            "height_px": 40,
        }
        result = {"status": "completed", "input": input_metadata, "lines": []}
        response = worker.session_response("request-1", input_metadata, result)
        self.assertEqual(
            response["result_sha256"],
            hashlib.sha256(worker.canonical_json(result).encode("utf-8")).hexdigest(),
        )
        self.assertLess(
            len(worker.canonical_json(response).encode("utf-8")),
            worker.MAX_SESSION_RESPONSE_BYTES,
        )

    def test_worker_response_writer_retries_short_writes(self) -> None:
        class ShortWriter(io.BytesIO):
            def write(self, value):
                return super().write(bytes(value[:5]))

        destination = ShortWriter()
        worker._write_bounded_session_response(
            destination,
            {"status": "completed", "lines": []},
        )
        self.assertEqual(
            json.loads(destination.getvalue()),
            {"status": "completed", "lines": []},
        )

    def test_worker_decodes_the_same_read_once_bytes_after_path_replacement(self) -> None:
        with tempfile.TemporaryDirectory(prefix="aiec-paddle-aba-") as temporary:
            source = Path(temporary) / "source.png"
            original = png_header()
            replacement = png_header(81, 40)
            source.write_bytes(original)
            observed: list[bytes] = []

            class ImageValue:
                size = (80, 40)

                def __enter__(self):
                    return self

                def __exit__(self, *_args):
                    return None

                def load(self):
                    return None

                def convert(self, _mode):
                    return "rgb"

            class ImageModule:
                @staticmethod
                def open(stream):
                    observed.append(stream.read())
                    source.write_bytes(replacement)
                    return ImageValue()

            class NumpyModule:
                @staticmethod
                def asarray(value):
                    return ("array", value)

            class Pipeline:
                @staticmethod
                def predict(value):
                    self.assertEqual(value, ("array", "rgb"))
                    return [{
                        "rec_texts": ["中野"],
                        "rec_scores": [0.99],
                        "rec_boxes": [[0, 0, 10, 10]],
                    }]

            result = worker._run_prepared_worker(
                source,
                {
                    "image_module": ImageModule,
                    "numpy_module": NumpyModule,
                    "pipeline": Pipeline,
                    "engine": {},
                    "setup_ms": 1.0,
                    "request_count": 0,
                },
                expected_input={
                    "sha256": hashlib.sha256(original).hexdigest(),
                    "width_px": 80,
                    "height_px": 40,
                },
            )
            self.assertEqual(source.read_bytes(), replacement)

        self.assertEqual(observed, [original])
        self.assertEqual(result["input"]["sha256"], hashlib.sha256(original).hexdigest())

    def test_worker_checks_dimensions_before_decoding_pixels(self) -> None:
        with tempfile.TemporaryDirectory(prefix="aiec-paddle-bomb-") as temporary:
            source = Path(temporary) / "source.png"
            source.write_bytes(png_header())
            load = mock.Mock()

            class ImageValue:
                size = (worker.MAX_IMAGE_PIXELS + 1, 1)

                def __enter__(self):
                    return self

                def __exit__(self, *_args):
                    return None

                def load(self):
                    load()

            class ImageModule:
                @staticmethod
                def open(_stream):
                    return ImageValue()

            with self.assertRaisesRegex(ValueError, "dimensions"):
                worker._run_prepared_worker(
                    source,
                    {
                        "image_module": ImageModule,
                        "numpy_module": mock.Mock(),
                        "pipeline": mock.Mock(),
                        "engine": {},
                        "setup_ms": 1.0,
                        "request_count": 0,
                    },
                )
        load.assert_not_called()

    def test_two_requests_share_one_lifelong_socket_guard(self) -> None:
        with tempfile.TemporaryDirectory(prefix="aiec-paddle-loop-") as temporary:
            root = Path(temporary)
            first = root / "first.png"
            second = root / "second.png"
            first.write_bytes(png_header())
            second.write_bytes(png_header(81, 40))
            requests = [
                worker.session_request(
                    "request-1",
                    first,
                    hashlib.sha256(first.read_bytes()).hexdigest(),
                    {"width_px": 80, "height_px": 40},
                ),
                worker.session_request(
                    "request-2",
                    second,
                    hashlib.sha256(second.read_bytes()).hexdigest(),
                    {"width_px": 81, "height_px": 40},
                ),
            ]
            source = io.BytesIO(b"".join(
                (worker.canonical_json(item) + "\n").encode("utf-8")
                for item in requests
            ))
            destination = io.BytesIO()
            guard_events: list[str] = []
            prepared = {"identity": object()}
            observed_prepared: list[object] = []

            @contextmanager
            def tracked_guard():
                guard_events.append("enter")
                try:
                    yield
                finally:
                    guard_events.append("exit")

            def run_prepared(path, value, *, expected_input, input_bytes):
                observed_prepared.append(value["identity"])
                self.assertIsInstance(input_bytes, bytes)
                return {
                    "status": "completed",
                    "input": expected_input,
                    "lines": [{"path": Path(path).name}],
                }

            with (
                mock.patch.object(worker, "offline_socket_guard", tracked_guard),
                mock.patch.object(worker, "_prepare_worker", return_value=prepared),
                mock.patch.object(
                    worker, "_run_prepared_worker", side_effect=run_prepared
                ),
            ):
                code = worker.run_session(
                    root,
                    root / "runtime.lock",
                    input_stream=source,
                    output_stream=destination,
                )

        self.assertEqual(code, 0)
        self.assertEqual(guard_events, ["enter", "exit"])
        self.assertEqual(len(observed_prepared), 2)
        self.assertIs(observed_prepared[0], observed_prepared[1])
        responses = [json.loads(line) for line in destination.getvalue().splitlines()]
        self.assertEqual(
            [item["request_id"] for item in responses],
            ["request-1", "request-2"],
        )


if __name__ == "__main__":
    unittest.main()
