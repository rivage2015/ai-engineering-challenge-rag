from __future__ import annotations

import hashlib
import json
import struct
import subprocess
import sys
import threading
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import local_visual_observation as visual  # noqa: E402
import local_image_ocr as image_reader  # noqa: E402


MODEL_DIGEST = "a" * 64
IMAGE_BYTES = (
    b"\x89PNG\r\n\x1a\n"
    + b"\x00\x00\x00\rIHDR"
    + struct.pack(">II", 80, 40)
    + b"local-visual-observation-fixture"
)


def observation_payload() -> dict[str, object]:
    return {
        "visible_objects": [
            {
                "object_id": "o1",
                "kind": "chart",
                "description": "青と灰色の棒グラフ",
            }
        ],
        "explicit_labels": [
            {"label_id": "l1", "text": "2026年"},
            {"label_id": "l2", "text": "利用件数"},
        ],
        "explicit_relations": [
            {"source_ref": "l2", "relation": "labels", "target_ref": "o1"}
        ],
        "labeled_values": [
            {
                "value_id": "v1",
                "label_text": "2026年",
                "series_label": "利用件数",
                "value_text": "120",
                "unit_text": "件",
                "value_status": "exact_label",
                "unclear_reason": "",
            },
            {
                "value_id": "v2",
                "label_text": "2025年",
                "series_label": "利用件数",
                "value_text": "",
                "unit_text": "件",
                "value_status": "unclear",
                "unclear_reason": "データラベルの数字を判読できない",
            },
        ],
        "warnings": [],
    }


def tags_response(digest: str = MODEL_DIGEST) -> dict[str, object]:
    return {
        "models": [
            {
                "name": "gemma4:12b",
                "model": "gemma4:12b",
                "digest": digest,
            }
        ]
    }


def chat_response(
    payload: dict[str, object] | None = None,
    *,
    model: str = "gemma4:12b",
) -> dict[str, object]:
    return {
        "model": model,
        "message": {
            "role": "assistant",
            "content": json.dumps(
                payload if payload is not None else observation_payload(),
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        },
        "done": True,
    }


class LocalVisualObservationTests(unittest.TestCase):
    def setUp(self) -> None:
        image_reader._LOCAL_MODEL_TIMEOUT_LATCH.clear()
        visual._UNREAPED_VISUAL_WORKERS.clear()

    def tearDown(self) -> None:
        image_reader._LOCAL_MODEL_TIMEOUT_LATCH.clear()
        visual._UNREAPED_VISUAL_WORKERS.clear()

    def test_fixed_local_request_and_provisional_provenance(self) -> None:
        request = mock.Mock(
            side_effect=[tags_response(), chat_response(), tags_response()]
        )
        with mock.patch.object(visual, "_ollama_json", request):
            result = visual._observe_image_inline(IMAGE_BYTES, timeout=12)

        self.assertEqual(request.call_count, 3)
        tags_call, chat_call, final_tags_call = request.call_args_list
        self.assertEqual(tags_call.args, ("GET", "/api/tags"))
        self.assertIsNone(tags_call.kwargs["payload"])
        self.assertEqual(chat_call.args, ("POST", "/api/chat"))
        self.assertEqual(final_tags_call.args, ("GET", "/api/tags"))
        payload = chat_call.kwargs["payload"]
        self.assertEqual(payload["model"], "gemma4:12b")
        self.assertIs(payload["stream"], False)
        self.assertEqual(payload["format"], visual.VISUAL_OBSERVATION_WIRE_SCHEMA)
        self.assertFalse(
            any(
                keyword in json.dumps(payload["format"], sort_keys=True)
                for keyword in ("pattern", "minLength", "maxLength", "maxItems")
            )
        )
        self.assertIs(payload["think"], False)
        self.assertEqual(payload["keep_alive"], 0)
        self.assertEqual(payload["options"]["temperature"], 0)
        self.assertEqual(payload["options"]["seed"], 42)
        self.assertEqual(len(payload["messages"]), 2)
        self.assertEqual(
            payload["messages"][1]["content"],
            visual.VISUAL_OBSERVATION_PROMPT,
        )
        self.assertNotIn("question", payload)

        self.assertEqual(result["status"], "provisional")
        self.assertEqual(result["quality_tier"], "provisional")
        self.assertEqual(result["provisional_marker"], "[暫定読取]")
        self.assertTrue(result["question_independent"])
        self.assertEqual(result["model"], "gemma4:12b")
        self.assertEqual(result["model_digest"], MODEL_DIGEST)
        self.assertEqual(
            result["prompt_sha256"],
            hashlib.sha256(
                visual.VISUAL_OBSERVATION_PROMPT.encode("utf-8")
            ).hexdigest(),
        )
        self.assertEqual(
            result["input_image_sha256"], hashlib.sha256(IMAGE_BYTES).hexdigest()
        )
        self.assertTrue(result["strict_json"])
        self.assertFalse(result["external_network_used"])
        self.assertFalse(result["downloads_performed"])
        self.assertEqual(
            {item["value_status"] for item in result["observation"]["labeled_values"]},
            {"exact_label", "unclear"},
        )
        self.assertNotIn("estimated", json.dumps(result, ensure_ascii=False))
        self.assertTrue(
            all(
                line.startswith(visual.PROVISIONAL_MARKER + " ")
                for line in result["text"].splitlines()
            )
        )

    def test_three_ollama_calls_share_one_absolute_deadline(self) -> None:
        request = mock.Mock(side_effect=[tags_response(), chat_response()])
        with (
            mock.patch.object(visual, "_ollama_json", request),
            mock.patch.object(
                visual.time,
                "monotonic",
                side_effect=[100.0, 100.1, 105.0, 110.0, 110.1, 113.0],
            ),
            self.assertRaisesRegex(
                visual.VisualObservationError, "deadline was exceeded"
            ),
        ):
            visual._observe_image_inline(IMAGE_BYTES, timeout=12)
        self.assertEqual(request.call_count, 2)

    def test_public_observer_uses_hashed_raw_byte_worker_protocol(self) -> None:
        inline_request = mock.Mock(
            side_effect=[tags_response(), chat_response(), tags_response()]
        )
        with mock.patch.object(visual, "_ollama_json", inline_request):
            expected = visual._observe_image_inline(IMAGE_BYTES, timeout=12)

        class FakeProcess:
            pid = 424242
            returncode: int | None = None
            stdin = None
            stdout = None

            def communicate(self, *, input: bytes, timeout: float):
                self.assert_timeout = timeout
                header_raw, separator, raw = input.partition(b"\n")
                if not separator or raw != IMAGE_BYTES:
                    raise AssertionError("worker did not receive the exact raw bytes")
                header = json.loads(header_raw)
                result_sha256 = hashlib.sha256(
                    visual._canonical_json_bytes(expected)
                ).hexdigest()
                envelope = {
                    "protocol_version": visual.WORKER_PROTOCOL_VERSION,
                    "type": "result",
                    "request_id": header["request_id"],
                    "task": header["task"],
                    "input": header["input"],
                    "prompt_sha256": header["prompt_sha256"],
                    "result_sha256": result_sha256,
                    "result": expected,
                }
                self.returncode = 0
                return visual._canonical_json_bytes(envelope) + b"\n", b""

            def poll(self):
                return self.returncode

        fake = FakeProcess()
        with mock.patch.object(visual.subprocess, "Popen", return_value=fake) as popen:
            result = visual.observe_image(
                IMAGE_BYTES,
                expected_input_sha256=hashlib.sha256(IMAGE_BYTES).hexdigest(),
                timeout=12,
            )
        self.assertEqual(result, expected)
        self.assertGreater(fake.assert_timeout, 0)
        self.assertIs(popen.call_args.kwargs["start_new_session"], True)
        self.assertEqual(popen.call_args.kwargs["stderr"], subprocess.DEVNULL)

    def test_worker_timeout_latches_and_blocks_later_paddle_restart(self) -> None:
        class HangingProcess:
            pid = 424243
            returncode: int | None = None
            stdin = None
            stdout = None

            @staticmethod
            def communicate(*, input: bytes, timeout: float):
                raise subprocess.TimeoutExpired("visual-worker", timeout)

            def poll(self):
                return self.returncode

        process = HangingProcess()

        def terminate(value) -> None:
            self.assertIs(value, process)
            process.returncode = -15

        with (
            mock.patch.object(visual.subprocess, "Popen", return_value=process),
            mock.patch.object(
                visual, "_terminate_worker_process", side_effect=terminate
            ) as terminate_worker,
            self.assertRaisesRegex(
                visual.VisualObservationError, "hard wall-clock deadline"
            ),
        ):
            visual.observe_image(IMAGE_BYTES, timeout=0.01)

        terminate_worker.assert_called_once_with(process)
        self.assertTrue(image_reader.local_model_timeout_latched())
        session = image_reader.PaddleOCRSession(runtime={"source": "fixture"})
        with self.assertRaisesRegex(RuntimeError, "restart is disabled"):
            session.run(
                IMAGE_BYTES,
                {"width_px": 80, "height_px": 40},
                timeout=1,
            )
        with (
            mock.patch.object(
                visual.subprocess,
                "Popen",
                side_effect=AssertionError("latched observer must not start"),
            ) as popen,
            self.assertRaisesRegex(
                visual.VisualObservationError, "disabled after an earlier hard timeout"
            ),
        ):
            visual.observe_image(IMAGE_BYTES, timeout=1)
        popen.assert_not_called()

    def test_keyboard_interrupt_reaps_worker_and_latches_future_starts(self) -> None:
        class InterruptedProcess:
            pid = 424244
            returncode: int | None = None
            stdin = None
            stdout = None

            @staticmethod
            def communicate(*, input: bytes, timeout: float):
                raise KeyboardInterrupt()

            def poll(self):
                return self.returncode

        process = InterruptedProcess()

        def terminate(value) -> None:
            self.assertIs(value, process)
            process.returncode = -15

        with (
            mock.patch.object(visual.subprocess, "Popen", return_value=process),
            mock.patch.object(
                visual, "_terminate_worker_process", side_effect=terminate
            ) as terminate_worker,
            self.assertRaises(KeyboardInterrupt),
        ):
            visual.observe_image(IMAGE_BYTES, timeout=1)

        terminate_worker.assert_called_once_with(process)
        self.assertTrue(image_reader.local_model_timeout_latched())

    def test_timeout_latch_wins_before_visual_worker_process_creation(self) -> None:
        building_header = threading.Event()
        finish_header = threading.Event()
        errors: list[BaseException] = []

        def delayed_request_id(_size: int) -> str:
            building_header.set()
            self.assertTrue(finish_header.wait(2))
            return "1" * 32

        def observe() -> None:
            try:
                visual.observe_image(IMAGE_BYTES, timeout=1)
            except BaseException as exc:
                errors.append(exc)

        with (
            mock.patch.object(
                visual.secrets, "token_hex", side_effect=delayed_request_id
            ),
            mock.patch.object(visual.subprocess, "Popen") as popen,
        ):
            starter = threading.Thread(target=observe)
            starter.start()
            self.assertTrue(building_header.wait(1))
            image_reader.latch_local_model_timeout()
            finish_header.set()
            starter.join(timeout=2)

        self.assertFalse(starter.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], visual.VisualObservationError)
        self.assertIn("disabled after an earlier hard timeout", str(errors[0]))
        popen.assert_not_called()

    def test_worker_stdout_uses_a_bounded_file_before_json_decode(self) -> None:
        class CompletedProcess:
            pid = 424245
            returncode = 0
            stdin = None
            stdout = None

            @staticmethod
            def communicate(*, input: bytes, timeout: float):
                return None, None

            @staticmethod
            def poll():
                return 0

        def start(*_args, **kwargs):
            output = kwargs["stdout"]
            output.write(b"x" * (visual.MAX_WORKER_RESPONSE_BYTES + 1))
            output.flush()
            return CompletedProcess()

        with (
            mock.patch.object(visual.subprocess, "Popen", side_effect=start),
            self.assertRaisesRegex(
                visual.VisualObservationError, "response exceeds the safety limit"
            ),
        ):
            visual.observe_image(IMAGE_BYTES, timeout=1)

    def test_unreaped_visual_worker_is_retained_and_poisoned(self) -> None:
        process = mock.Mock()
        process.pid = 424249
        process.stdin = None
        process.stdout = None
        process.poll.return_value = None
        process.wait.side_effect = [
            subprocess.TimeoutExpired("term", 0.25),
            subprocess.TimeoutExpired("kill", 1.0),
        ]

        with mock.patch.object(visual.os, "killpg") as killpg:
            reaped = visual._terminate_worker_process(process)

        self.assertFalse(reaped)
        self.assertTrue(image_reader.local_model_timeout_latched())
        self.assertTrue(
            any(item is process for item in visual._UNREAPED_VISUAL_WORKERS)
        )
        self.assertEqual(
            killpg.call_args_list,
            [
                mock.call(process.pid, visual.signal.SIGTERM),
                mock.call(process.pid, visual.signal.SIGKILL),
            ],
        )

    def test_worker_error_envelope_is_identity_bound_and_control_free(self) -> None:
        request_id = "1" * 32
        common = {
            "protocol_version": visual.WORKER_PROTOCOL_VERSION,
            "type": "error",
            "request_id": request_id,
            "error_type": "RuntimeError",
            "error": "fixture failure",
        }
        invalid = [
            {**common, "request_id": "2" * 32},
            {**common, "error_type": 7},
            {**common, "error": "first line\nsecond line"},
        ]
        for envelope in invalid:
            with self.subTest(envelope=envelope), self.assertRaisesRegex(
                visual.VisualObservationError, "error envelope is invalid"
            ):
                visual._decode_worker_envelope(
                    visual._canonical_json_bytes(envelope),
                    request_id=request_id,
                    task="visual_observation",
                    input_sha256="a" * 64,
                    input_size=10,
                    prompt_sha256="b" * 64,
                )

    def test_worker_result_rejects_float_input_size(self) -> None:
        result = {"fixture": True}
        envelope = {
            "protocol_version": visual.WORKER_PROTOCOL_VERSION,
            "type": "result",
            "request_id": "1" * 32,
            "task": "visual_observation",
            "input": {"sha256": "a" * 64, "size_bytes": 10.0},
            "prompt_sha256": "b" * 64,
            "result_sha256": hashlib.sha256(
                visual._canonical_json_bytes(result)
            ).hexdigest(),
            "result": result,
        }
        with self.assertRaisesRegex(
            visual.VisualObservationError, "response identity mismatch"
        ):
            visual._decode_worker_envelope(
                visual._canonical_json_bytes(envelope),
                request_id="1" * 32,
                task="visual_observation",
                input_sha256="a" * 64,
                input_size=10,
                prompt_sha256="b" * 64,
            )

    def test_visual_popen_interrupt_poison_without_a_handle(self) -> None:
        with (
            mock.patch.object(
                visual.subprocess, "Popen", side_effect=KeyboardInterrupt
            ),
            self.assertRaises(KeyboardInterrupt),
        ):
            visual.observe_image(IMAGE_BYTES, timeout=1)
        self.assertTrue(image_reader.local_model_timeout_latched())

    def test_abnormal_worker_exit_poison_blocks_future_local_models(self) -> None:
        class CrashedProcess:
            pid = 424254
            returncode = -9
            stdin = None
            stdout = None

            @staticmethod
            def communicate(*, input: bytes, timeout: float):
                return b"", b""

            @staticmethod
            def poll():
                return -9

        with (
            mock.patch.object(
                visual.subprocess, "Popen", return_value=CrashedProcess()
            ),
            self.assertRaisesRegex(
                visual.VisualObservationError, "exited unexpectedly"
            ),
        ):
            visual.observe_image(IMAGE_BYTES, timeout=1)
        self.assertTrue(image_reader.local_model_timeout_latched())

    def test_worker_communication_error_poison_reaps_live_child(self) -> None:
        class BrokenProcess:
            pid = 424255
            returncode: int | None = None
            stdin = None
            stdout = None

            @staticmethod
            def communicate(*, input: bytes, timeout: float):
                raise OSError("fixture pipe failure")

            def poll(self):
                return self.returncode

        process = BrokenProcess()

        def terminate(value) -> bool:
            self.assertIs(value, process)
            process.returncode = -15
            return True

        with (
            mock.patch.object(visual.subprocess, "Popen", return_value=process),
            mock.patch.object(
                visual, "_terminate_worker_process", side_effect=terminate
            ) as terminate_worker,
            self.assertRaisesRegex(OSError, "fixture pipe failure"),
        ):
            visual.observe_image(IMAGE_BYTES, timeout=1)

        self.assertTrue(image_reader.local_model_timeout_latched())
        terminate_worker.assert_called_once_with(process)

    def test_worker_error_exit_poison_blocks_paddle_restart(self) -> None:
        class ErrorProcess:
            pid = 424256
            returncode = 2
            stdin = None
            stdout = None

            def communicate(self, *, input: bytes, timeout: float):
                header = json.loads(input.partition(b"\n")[0])
                envelope = {
                    "protocol_version": visual.WORKER_PROTOCOL_VERSION,
                    "type": "error",
                    "request_id": header["request_id"],
                    "error_type": "RuntimeError",
                    "error": "fixture worker failure",
                }
                return visual._canonical_json_bytes(envelope), b""

            @staticmethod
            def poll():
                return 2

        with (
            mock.patch.object(
                visual.subprocess, "Popen", return_value=ErrorProcess()
            ),
            self.assertRaisesRegex(
                visual.VisualObservationError, "fixture worker failure"
            ),
        ):
            visual.observe_image(IMAGE_BYTES, timeout=1)
        self.assertTrue(image_reader.local_model_timeout_latched())
        session = image_reader.PaddleOCRSession(runtime={"source": "fixture"})
        with self.assertRaisesRegex(RuntimeError, "restart is disabled"):
            session.run(
                IMAGE_BYTES,
                {"width_px": 80, "height_px": 40},
                timeout=1,
            )

    def test_expected_input_digest_is_verified_before_http(self) -> None:
        request = mock.Mock(side_effect=AssertionError("HTTP must not run"))
        with (
            mock.patch.object(visual, "_ollama_json", request),
            self.assertRaisesRegex(ValueError, "SHA-256 mismatch"),
        ):
            visual._observe_image_inline(
                IMAGE_BYTES,
                expected_input_sha256="b" * 64,
            )
        request.assert_not_called()

        with (
            mock.patch.object(
                visual,
                "VISUAL_OBSERVATION_PROMPT",
                visual.VISUAL_OBSERVATION_PROMPT + " tampered",
            ),
            mock.patch.object(
                visual,
                "_ollama_json",
                side_effect=AssertionError("HTTP must not run"),
            ) as prompt_request,
            self.assertRaisesRegex(
                visual.VisualObservationError, "prompt digest mismatch"
            ),
        ):
            visual._observe_image_inline(IMAGE_BYTES)
        prompt_request.assert_not_called()

    def test_unsupported_or_oversized_raster_is_rejected_before_http(self) -> None:
        request = mock.Mock(side_effect=AssertionError("HTTP must not run"))
        with (
            mock.patch.object(visual, "_ollama_json", request),
            self.assertRaisesRegex(ValueError, "unsupported standalone image bytes"),
        ):
            visual._observe_image_inline(b"not-an-image")
        request.assert_not_called()

        huge_header = (
            b"\x89PNG\r\n\x1a\n"
            + b"\x00\x00\x00\rIHDR"
            + struct.pack(">II", 32_769, 1)
        )
        with (
            mock.patch.object(visual, "_ollama_json", request),
            self.assertRaisesRegex(ValueError, "dimensions exceed"),
        ):
            visual._observe_image_inline(huge_header)
        request.assert_not_called()

    def test_model_digest_and_response_model_are_fail_closed(self) -> None:
        with (
            mock.patch.object(
                visual,
                "_ollama_json",
                side_effect=[tags_response("not-a-digest"), chat_response()],
            ),
            self.assertRaisesRegex(visual.VisualObservationError, "digest"),
        ):
            visual._observe_image_inline(IMAGE_BYTES)

        with (
            mock.patch.object(
                visual,
                "_ollama_json",
                side_effect=[
                    tags_response(),
                    chat_response(),
                    tags_response("b" * 64),
                ],
            ),
            self.assertRaisesRegex(visual.VisualObservationError, "changed during"),
        ):
            visual._observe_image_inline(IMAGE_BYTES)

        incomplete = chat_response()
        incomplete["done"] = False
        with (
            mock.patch.object(
                visual,
                "_ollama_json",
                side_effect=[tags_response(), incomplete],
            ),
            self.assertRaisesRegex(visual.VisualObservationError, "incomplete"),
        ):
            visual._observe_image_inline(IMAGE_BYTES)

        with (
            mock.patch.object(
                visual,
                "_ollama_json",
                side_effect=[tags_response(), chat_response(model="other:latest")],
            ),
            self.assertRaisesRegex(visual.VisualObservationError, "does not match"),
        ):
            visual._observe_image_inline(IMAGE_BYTES)

    def test_model_content_must_be_one_strict_json_object(self) -> None:
        fenced = chat_response()
        fenced["message"]["content"] = (
            "```json\n"
            + json.dumps(observation_payload(), ensure_ascii=False)
            + "\n```"
        )
        with (
            mock.patch.object(
                visual, "_ollama_json", side_effect=[tags_response(), fenced]
            ),
            self.assertRaisesRegex(visual.VisualObservationError, "strict JSON"),
        ):
            visual._observe_image_inline(IMAGE_BYTES)

        duplicate = chat_response()
        duplicate["message"]["content"] = (
            '{"visible_objects":[],"visible_objects":[],"explicit_labels":[],'
            '"explicit_relations":[],"labeled_values":[],"warnings":[]}'
        )
        with (
            mock.patch.object(
                visual, "_ollama_json", side_effect=[tags_response(), duplicate]
            ),
            self.assertRaisesRegex(visual.VisualObservationError, "duplicate key"),
        ):
            visual._observe_image_inline(IMAGE_BYTES)

        extra = observation_payload()
        extra["estimated_values"] = [{"value": 119}]
        with (
            mock.patch.object(
                visual,
                "_ollama_json",
                side_effect=[tags_response(), chat_response(extra)],
            ),
            self.assertRaisesRegex(visual.VisualObservationError, "strict schema"),
        ):
            visual._observe_image_inline(IMAGE_BYTES)

    def test_estimated_or_guessed_values_are_rejected(self) -> None:
        estimated = observation_payload()
        estimated["labeled_values"][0]["value_status"] = "estimated"
        with self.assertRaisesRegex(
            visual.VisualObservationError, "exact_label or unclear"
        ):
            visual.validate_observation(estimated)

        guessed = observation_payload()
        guessed["labeled_values"][1]["value_text"] = "95"
        with self.assertRaisesRegex(
            visual.VisualObservationError, "must not contain a guessed value"
        ):
            visual.validate_observation(guessed)

    def test_unknown_or_self_relations_are_omitted_without_losing_labels(self) -> None:
        unsafe_relations = observation_payload()
        unsafe_relations["explicit_relations"].extend([
            {
                "source_ref": "l1",
                "relation": "labels",
                "target_ref": "missing",
            },
            {
                "source_ref": "l1",
                "relation": "contains",
                "target_ref": "l1",
            },
        ])
        normalized = visual.validate_observation(unsafe_relations)
        self.assertEqual(
            normalized["explicit_relations"],
            observation_payload()["explicit_relations"],
        )
        self.assertEqual(len(normalized["explicit_labels"]), 2)
        self.assertTrue(any(
            "unknown ID" in warning for warning in normalized["warnings"]
        ))
        self.assertTrue(any(
            "self relation" in warning for warning in normalized["warnings"]
        ))

    def test_identity_or_sensitive_inference_fields_are_not_in_schema(self) -> None:
        unsafe = observation_payload()
        unsafe["visible_objects"][0]["identity"] = "named person"
        with self.assertRaisesRegex(visual.VisualObservationError, "strict schema"):
            visual.validate_observation(unsafe)

        face_identification = observation_payload()
        face_identification["visible_objects"][0] = {
            "object_id": "o1",
            "kind": "person",
            "description": "画像の顔は某氏",
        }
        with self.assertRaisesRegex(
            visual.VisualObservationError, "person description must remain generic"
        ):
            visual.validate_observation(face_identification)

        generic_person = observation_payload()
        generic_person["visible_objects"][0] = {
            "object_id": "o1",
            "kind": "person",
            "description": "人物",
        }
        normalized = visual.validate_observation(generic_person)
        self.assertEqual(normalized["visible_objects"][0]["description"], "person")
        prompt = visual.VISUAL_OBSERVATION_PROMPT
        self.assertIn("個人を特定しません", prompt)
        self.assertIn("センシティブ属性を推測しません", prompt)

    def test_http_transport_is_literal_loopback_and_endpoint_allowlisted(self) -> None:
        class FakeResponse:
            status = 200

            @staticmethod
            def read(_limit: int) -> bytes:
                return b'{"models":[]}'

        class FakeConnection:
            instances: list["FakeConnection"] = []

            def __init__(self, host: str, port: int, *, timeout: float) -> None:
                self.host = host
                self.port = port
                self.timeout = timeout
                self.request_args: tuple[object, ...] | None = None
                self.closed = False
                self.__class__.instances.append(self)

            def request(self, *args: object, **_kwargs: object) -> None:
                self.request_args = args

            @staticmethod
            def getresponse() -> FakeResponse:
                return FakeResponse()

            def close(self) -> None:
                self.closed = True

        with mock.patch.object(
            visual.http.client, "HTTPConnection", FakeConnection
        ):
            response = visual._ollama_json(
                "GET", "/api/tags", payload=None, timeout=5
            )
        self.assertEqual(response, {"models": []})
        connection = FakeConnection.instances[0]
        self.assertEqual((connection.host, connection.port), ("127.0.0.1", 11434))
        self.assertEqual(connection.request_args[:2], ("GET", "/api/tags"))
        self.assertTrue(connection.closed)

        with (
            mock.patch.object(
                visual.http.client,
                "HTTPConnection",
                side_effect=AssertionError("must not connect"),
            ) as constructor,
            self.assertRaisesRegex(ValueError, "unsupported loopback"),
        ):
            visual._ollama_json(
                "GET", "https://example.com/", payload=None, timeout=5
            )
        constructor.assert_not_called()

        with self.assertRaisesRegex(ValueError, "body does not match"):
            visual._ollama_json(
                "POST", "/api/chat", payload=None, timeout=5
            )

    def test_http_response_chunks_share_one_absolute_deadline(self) -> None:
        class FakeResponse:
            status = 200

            def __init__(self) -> None:
                self.read_count = 0

            def read1(self, _limit: int) -> bytes:
                self.read_count += 1
                return b"{" if self.read_count == 1 else b'"models":[]}'

        class FakeConnection:
            instance: "FakeConnection | None" = None

            def __init__(self, _host: str, _port: int, *, timeout: float) -> None:
                self.timeout = timeout
                self.sock = None
                self.response = FakeResponse()
                self.__class__.instance = self

            def request(self, *_args: object, **_kwargs: object) -> None:
                return None

            def getresponse(self) -> FakeResponse:
                return self.response

            def close(self) -> None:
                return None

        with (
            mock.patch.object(
                visual.http.client, "HTTPConnection", FakeConnection
            ),
            mock.patch.object(
                visual.time,
                "monotonic",
                side_effect=[100.0, 100.1, 100.2, 100.3, 106.0],
            ),
            self.assertRaisesRegex(
                visual.VisualObservationError, "deadline was exceeded"
            ),
        ):
            visual._ollama_json("GET", "/api/tags", payload=None, timeout=5)
        self.assertIsNotNone(FakeConnection.instance)
        self.assertEqual(FakeConnection.instance.response.read_count, 1)


if __name__ == "__main__":
    unittest.main()
