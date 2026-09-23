"""Process-local visual reuse contracts; no model or worker process is started."""

from __future__ import annotations

import copy
import contextvars
import hashlib
import sys
import threading
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

import local_image_ocr as image_reader  # noqa: E402
import local_visual_observation as visual  # noqa: E402
from test_local_visual_observation import (  # noqa: E402
    IMAGE_BYTES,
    MODEL_DIGEST,
    chat_response,
    tags_response,
)


class LocalVisualObservationMemoTests(unittest.TestCase):
    def setUp(self) -> None:
        image_reader._LOCAL_MODEL_TIMEOUT_LATCH.clear()
        self.addCleanup(image_reader._LOCAL_MODEL_TIMEOUT_LATCH.clear)
        # Build the ordinary, validated result through the existing reader with
        # entirely synthetic Ollama responses. The memo must retain this schema.
        with mock.patch.object(
            visual,
            "_ollama_json",
            side_effect=[tags_response(), chat_response(), tags_response()],
        ):
            self.template = visual._observe_image_inline(IMAGE_BYTES, timeout=12)
        self.model_digest = MODEL_DIGEST
        self.worker_results: list[dict[str, object]] = []
        self.installed = self.enterContext(
            mock.patch.object(visual, "_installed_model", side_effect=self.model)
        )
        self.worker = self.enterContext(
            mock.patch.object(visual, "_run_isolated_task", side_effect=self.result)
        )
        self.enterContext(
            mock.patch.object(
                visual, "_ollama_json", side_effect=AssertionError("real HTTP forbidden")
            )
        )
        self.enterContext(
            mock.patch.object(
                visual.subprocess, "Popen", side_effect=AssertionError("worker forbidden")
            )
        )

    def model(self, *, deadline_at: float) -> dict[str, str]:
        self.assertGreater(deadline_at, 0)
        return {
            "requested": visual.VISUAL_OBSERVATION_MODEL,
            "resolved": visual.VISUAL_OBSERVATION_MODEL,
            "digest": self.model_digest,
        }

    def result(self, task: str, raw: bytes, **kwargs: object) -> dict[str, object]:
        self.assertEqual(task, "visual_observation")
        self.assertEqual(kwargs["input_sha256"], hashlib.sha256(raw).hexdigest())
        result = copy.deepcopy(self.template)
        result.update(
            input_image_sha256=kwargs["input_sha256"],
            prompt_sha256=kwargs["prompt_sha256"],
            model=visual.VISUAL_OBSERVATION_MODEL,
            model_digest=self.model_digest,
            runner_version=visual.VISUAL_OBSERVATION_VERSION,
        )
        self.worker_results.append(result)
        return result

    def observe(self, raw: bytes = IMAGE_BYTES) -> dict[str, object]:
        return visual.observe_image(raw, timeout=12)

    def test_identical_image_reuses_exact_provisional_result(self) -> None:
        with visual.visual_observation_session(enabled=True) as memo:
            first = self.observe()
            installed_before_hit = self.installed.call_count
            second = self.observe()
            self.assertEqual(self.worker.call_count, 1)
            self.assertEqual(self.installed.call_count - installed_before_hit, 2)
            self.assertEqual(first, second)
            self.assertEqual(set(second), set(self.template))
            self.assertEqual(second["status"], "provisional")
            self.assertEqual(second["quality_tier"], "provisional")
            self.assertEqual(memo.stats()["hits"], 1)
            self.assertEqual(memo.stats()["misses"], 1)
            self.assertEqual(memo.stats()["stores"], 1)
            self.assertEqual(memo.stats()["entries"], 1)
            self.assertGreater(memo.stats()["bytes"], 0)
        self.assertEqual(memo.stats()["entries"], 0)
        self.assertEqual(memo.stats()["bytes"], 0)

    def test_different_image_bytes_are_a_miss(self) -> None:
        with visual.visual_observation_session() as memo:
            first = self.observe()
            second = self.observe(IMAGE_BYTES + b"another-image")
            self.assertNotEqual(first["input_image_sha256"], second["input_image_sha256"])
            self.assertEqual(self.worker.call_count, 2)
            self.assertEqual(memo.stats()["hits"], 0)

    def test_changed_installed_model_digest_is_a_miss(self) -> None:
        with visual.visual_observation_session() as memo:
            first = self.observe()
            self.model_digest = "b" * 64
            second = self.observe()
            self.assertEqual(self.worker.call_count, 2)
            self.assertNotEqual(first["model_digest"], second["model_digest"])
            self.assertEqual(memo.stats()["hits"], 0)

    def test_changed_model_name_is_a_miss_even_with_same_digest(self) -> None:
        with visual.visual_observation_session() as memo:
            self.observe()
            with mock.patch.object(visual, "VISUAL_OBSERVATION_MODEL", "fixture-model:2"):
                second = self.observe()
            self.assertEqual(self.worker.call_count, 2)
            self.assertEqual(second["model"], "fixture-model:2")
            self.assertEqual(memo.stats()["hits"], 0)

    def test_changed_verified_prompt_is_a_miss(self) -> None:
        changed_prompt = visual.VISUAL_OBSERVATION_PROMPT + "\nSynthetic test instruction."
        with visual.visual_observation_session() as memo:
            self.observe()
            with (
                mock.patch.object(visual, "VISUAL_OBSERVATION_PROMPT", changed_prompt),
                mock.patch.object(
                    visual,
                    "VISUAL_OBSERVATION_PROMPT_SHA256",
                    hashlib.sha256(changed_prompt.encode("utf-8")).hexdigest(),
                ),
            ):
                self.observe()
            self.assertEqual(self.worker.call_count, 2)
            self.assertEqual(memo.stats()["hits"], 0)

    def test_changed_wire_schema_is_a_miss(self) -> None:
        schema = copy.deepcopy(visual.VISUAL_OBSERVATION_WIRE_SCHEMA)
        schema["description"] = "Synthetic alternate wire schema"
        with visual.visual_observation_session() as memo:
            self.observe()
            with mock.patch.object(visual, "VISUAL_OBSERVATION_WIRE_SCHEMA", schema):
                self.observe()
            self.assertEqual(self.worker.call_count, 2)
            self.assertEqual(memo.stats()["hits"], 0)

    def test_changed_validation_schema_is_a_miss(self) -> None:
        schema = copy.deepcopy(visual.VISUAL_OBSERVATION_SCHEMA)
        schema["description"] = "Synthetic alternate validator schema"
        with visual.visual_observation_session() as memo:
            self.observe()
            with mock.patch.object(visual, "VISUAL_OBSERVATION_SCHEMA", schema):
                self.observe()
            self.assertEqual(self.worker.call_count, 2)
            self.assertEqual(memo.stats()["hits"], 0)

    def test_changed_generation_options_are_a_miss(self) -> None:
        with visual.visual_observation_session() as memo:
            self.observe()
            with mock.patch.object(visual, "MAX_PREDICT_TOKENS", 2048):
                self.observe()
            self.assertEqual(self.worker.call_count, 2)
            self.assertEqual(memo.stats()["hits"], 0)

    def test_changed_runner_version_is_a_miss(self) -> None:
        with visual.visual_observation_session() as memo:
            self.observe()
            with mock.patch.object(visual, "VISUAL_OBSERVATION_VERSION", "fixture-v2"):
                self.observe()
            self.assertEqual(self.worker.call_count, 2)
            self.assertEqual(memo.stats()["hits"], 0)

    def test_failed_observation_is_not_stored_or_replayed(self) -> None:
        for error_class in (
            visual.VisualObservationError,
            visual.CompletedResponseValidationError,
        ):
            with self.subTest(error_class=error_class.__name__):
                self.worker.reset_mock()
                self.worker.side_effect = error_class("synthetic failure")
                with visual.visual_observation_session() as memo:
                    with self.assertRaises(error_class):
                        self.observe()
                    self.assertEqual(memo.stats()["stores"], 0)
                    self.assertEqual(memo.stats()["entries"], 0)
                    self.worker.side_effect = self.result
                    self.observe()
                    self.assertEqual(self.worker.call_count, 2)
                    self.assertEqual(memo.stats()["hits"], 0)

    def test_invalid_worker_result_is_not_cached(self) -> None:
        def invalid(task: str, raw: bytes, **kwargs: object) -> dict[str, object]:
            result = self.result(task, raw, **kwargs)
            result["quality_tier"] = "high"
            return result

        with visual.visual_observation_session() as memo:
            self.worker.side_effect = invalid
            with self.assertRaises(visual.VisualObservationError):
                self.observe()
            self.assertEqual(memo.stats()["stores"], 0)
            self.assertEqual(memo.stats()["entries"], 0)
            self.worker.side_effect = self.result
            self.observe()
            self.assertEqual(self.worker.call_count, 2)

    def test_worker_model_digest_must_match_the_fresh_identity(self) -> None:
        def invalid(task: str, raw: bytes, **kwargs: object) -> dict[str, object]:
            result = self.result(task, raw, **kwargs)
            result["model_digest"] = "c" * 64
            return result

        with visual.visual_observation_session() as memo:
            self.worker.side_effect = invalid
            with self.assertRaises(visual.VisualObservationError):
                self.observe()
            self.assertEqual(memo.stats()["stores"], 0)
            self.assertEqual(memo.stats()["entries"], 0)

    def test_timeout_latch_blocks_even_a_previously_cached_image(self) -> None:
        with visual.visual_observation_session() as memo:
            self.observe()
            image_reader._LOCAL_MODEL_TIMEOUT_LATCH.set()
            installed_before = self.installed.call_count
            with self.assertRaises(visual.VisualObservationError):
                self.observe()
            self.assertEqual(self.worker.call_count, 1)
            self.assertEqual(self.installed.call_count, installed_before)
            self.assertEqual(memo.stats()["hits"], 0)

    def test_hit_rechecks_model_identity_before_returning(self) -> None:
        with visual.visual_observation_session() as memo:
            self.observe()
            current = self.model(deadline_at=1)
            changed = {**current, "digest": "b" * 64}
            self.installed.side_effect = [current, changed]
            with self.assertRaises(visual.VisualObservationError):
                self.observe()
            self.assertEqual(self.worker.call_count, 1)
            self.assertEqual(memo.stats()["hits"], 0)

    def test_missing_model_cannot_be_hidden_by_a_cache_hit(self) -> None:
        with visual.visual_observation_session():
            self.observe()
            self.installed.side_effect = visual.VisualObservationError("model unavailable")
            with self.assertRaisesRegex(visual.VisualObservationError, "model unavailable"):
                self.observe()
            self.assertEqual(self.worker.call_count, 1)

    def test_deadline_exceeded_during_lookup_does_not_return_cached_result(self) -> None:
        with visual.visual_observation_session() as memo:
            self.observe()
            clock = [100.0]
            original_get = memo.get

            def lookup_then_expire(key: str) -> dict[str, object] | None:
                result = original_get(key)
                clock[0] = 200.0
                return result

            before_identity = self.installed.call_count
            with (
                mock.patch.object(visual.time, "monotonic", side_effect=lambda: clock[0]),
                mock.patch.object(memo, "get", side_effect=lookup_then_expire),
                self.assertRaisesRegex(visual.VisualObservationError, "deadline"),
            ):
                self.observe()
            self.assertEqual(self.worker.call_count, 1)
            self.assertEqual(self.installed.call_count - before_identity, 1)
            self.assertEqual(memo.stats()["hits"], 0)

    def test_timeout_latched_during_lookup_does_not_return_cached_result(self) -> None:
        with visual.visual_observation_session() as memo:
            self.observe()
            original_get = memo.get

            def lookup_then_latch(key: str) -> dict[str, object] | None:
                result = original_get(key)
                image_reader._LOCAL_MODEL_TIMEOUT_LATCH.set()
                return result

            before_identity = self.installed.call_count
            with (
                mock.patch.object(memo, "get", side_effect=lookup_then_latch),
                self.assertRaises(visual.VisualObservationError),
            ):
                self.observe()
            self.assertEqual(self.worker.call_count, 1)
            self.assertEqual(self.installed.call_count - before_identity, 1)
            self.assertEqual(memo.stats()["hits"], 0)

    def test_final_identity_check_cannot_hide_a_new_timeout_latch(self) -> None:
        with visual.visual_observation_session() as memo:
            self.observe()
            calls = 0

            def model_then_latch(*, deadline_at: float) -> dict[str, str]:
                nonlocal calls
                calls += 1
                result = self.model(deadline_at=deadline_at)
                if calls == 2:
                    image_reader._LOCAL_MODEL_TIMEOUT_LATCH.set()
                return result

            self.installed.side_effect = model_then_latch
            with self.assertRaises(visual.VisualObservationError):
                self.observe()
            self.assertEqual(calls, 2)
            self.assertEqual(self.worker.call_count, 1)
            self.assertEqual(memo.stats()["hits"], 0)

    def test_final_identity_check_cannot_extend_the_deadline(self) -> None:
        with visual.visual_observation_session() as memo:
            self.observe()
            clock = [100.0]
            calls = 0

            def model_then_expire(*, deadline_at: float) -> dict[str, str]:
                nonlocal calls
                calls += 1
                result = self.model(deadline_at=deadline_at)
                if calls == 2:
                    clock[0] = 200.0
                return result

            self.installed.side_effect = model_then_expire
            with (
                mock.patch.object(visual.time, "monotonic", side_effect=lambda: clock[0]),
                self.assertRaisesRegex(visual.VisualObservationError, "deadline"),
            ):
                self.observe()
            self.assertEqual(calls, 2)
            self.assertEqual(self.worker.call_count, 1)
            self.assertEqual(memo.stats()["hits"], 0)

    def test_schema_changed_during_lookup_rejects_the_stale_hit(self) -> None:
        with visual.visual_observation_session() as memo:
            self.observe()
            schema = copy.deepcopy(visual.VISUAL_OBSERVATION_WIRE_SCHEMA)
            original_get = memo.get

            def lookup_then_change_schema(key: str) -> dict[str, object] | None:
                result = original_get(key)
                self.assertIsNotNone(result)
                schema["description"] = "Contract changed during lookup"
                return result

            with (
                mock.patch.object(visual, "VISUAL_OBSERVATION_WIRE_SCHEMA", schema),
                mock.patch.object(memo, "get", side_effect=lookup_then_change_schema),
                self.assertRaises(visual.VisualObservationError),
            ):
                self.observe()
            self.assertEqual(self.worker.call_count, 1)
            self.assertEqual(memo.stats()["hits"], 0)

    def test_different_owner_pid_does_not_reuse_or_overwrite_the_memo(self) -> None:
        with visual.visual_observation_session() as memo:
            first = self.observe()
            before_stats = memo.stats()
            before_identity = self.installed.call_count
            with mock.patch.object(visual.os, "getpid", return_value=memo.owner[0] + 1):
                child_result = self.observe()
            self.assertEqual(child_result, first)
            self.assertEqual(self.worker.call_count, 2)
            self.assertEqual(self.installed.call_count, before_identity)
            self.assertEqual(memo.stats(), before_stats)
            self.observe()
            self.assertEqual(self.worker.call_count, 2)
            self.assertEqual(memo.stats()["hits"], 1)

    def test_copied_context_in_another_thread_does_not_reuse_parent_memo(self) -> None:
        with visual.visual_observation_session() as memo:
            first = self.observe()
            before_stats = memo.stats()
            before_identity = self.installed.call_count
            inherited = contextvars.copy_context()
            outcomes: list[object] = []

            def observe_in_thread() -> None:
                try:
                    outcomes.append(inherited.run(self.observe))
                except BaseException as error:
                    outcomes.append(error)

            thread = threading.Thread(target=observe_in_thread)
            thread.start()
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())
            self.assertEqual(outcomes, [first])
            self.assertEqual(self.worker.call_count, 2)
            self.assertEqual(self.installed.call_count, before_identity)
            self.assertEqual(memo.stats(), before_stats)
            self.observe()
            self.assertEqual(self.worker.call_count, 2)
            self.assertEqual(memo.stats()["hits"], 1)

    def test_without_session_every_call_uses_the_original_worker_path(self) -> None:
        self.observe()
        self.observe()
        self.assertEqual(self.worker.call_count, 2)
        self.installed.assert_not_called()

    def test_disabled_session_does_not_reuse_results(self) -> None:
        with visual.visual_observation_session(enabled=False) as memo:
            self.observe()
            self.observe()
            self.assertEqual(self.worker.call_count, 2)
            self.assertEqual(memo.stats()["hits"], 0)
            self.assertEqual(memo.stats()["entries"], 0)

    def test_next_session_starts_empty_and_exit_releases_results(self) -> None:
        with visual.visual_observation_session() as first_memo:
            self.observe()
            self.observe()
        self.assertEqual(first_memo.stats()["entries"], 0)
        self.assertEqual(first_memo.stats()["bytes"], 0)
        with visual.visual_observation_session() as next_memo:
            self.observe()
            self.assertEqual(self.worker.call_count, 2)
            self.assertEqual(next_memo.stats()["hits"], 0)
            self.assertEqual(next_memo.stats()["stores"], 1)

    def test_exception_exit_also_releases_results(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "caller aborted"):
            with visual.visual_observation_session() as memo:
                self.observe()
                raise RuntimeError("caller aborted")
        self.assertEqual(memo.stats()["entries"], 0)
        self.assertEqual(memo.stats()["bytes"], 0)

    def test_returned_and_original_worker_objects_cannot_mutate_the_memo(self) -> None:
        with visual.visual_observation_session():
            first = self.observe()
            expected = copy.deepcopy(first)
            first["observation"]["explicit_labels"][0]["text"] = "caller mutation"
            self.worker_results[0]["observation"]["warnings"].append("worker mutation")
            second = self.observe()
            self.assertEqual(second, expected)
            second["observation"]["explicit_labels"].clear()
            third = self.observe()
            self.assertEqual(third, expected)
            self.assertEqual(self.worker.call_count, 1)

    def test_entry_capacity_evicts_without_reusing_another_images_result(self) -> None:
        with (
            mock.patch.object(visual, "MAX_MEMO_ENTRIES", 2),
            visual.visual_observation_session() as memo,
        ):
            for suffix in (b"a", b"b", b"c"):
                result = self.observe(IMAGE_BYTES + suffix)
                self.assertEqual(
                    result["input_image_sha256"], hashlib.sha256(IMAGE_BYTES + suffix).hexdigest()
                )
                self.assertLessEqual(memo.stats()["entries"], 2)
            self.assertGreaterEqual(memo.stats()["evictions"], 1)
            self.observe(IMAGE_BYTES + b"c")
            self.assertEqual(self.worker.call_count, 3)
            self.observe(IMAGE_BYTES + b"a")
            self.assertEqual(self.worker.call_count, 4)
            self.assertLessEqual(memo.stats()["entries"], 2)

    def test_byte_capacity_is_bounded_and_evicts(self) -> None:
        limit = len(visual._canonical_json_bytes(self.template)) * 2 + 4096
        with (
            mock.patch.object(visual, "MAX_MEMO_BYTES", limit),
            visual.visual_observation_session() as memo,
        ):
            for index in range(10):
                self.observe(IMAGE_BYTES + str(index).encode("ascii"))
                self.assertLessEqual(memo.stats()["bytes"], limit)
            self.assertGreater(memo.stats()["stores"], 0)
            self.assertGreater(memo.stats()["evictions"], 0)

    def test_result_larger_than_capacity_is_not_stored(self) -> None:
        with (
            mock.patch.object(visual, "MAX_MEMO_BYTES", 1),
            visual.visual_observation_session() as memo,
        ):
            self.observe()
            self.observe()
            self.assertEqual(self.worker.call_count, 2)
            self.assertEqual(memo.stats()["stores"], 0)
            self.assertEqual(memo.stats()["entries"], 0)
            self.assertEqual(memo.stats()["bytes"], 0)

    def test_expected_input_hash_mismatch_is_rejected_before_cached_lookup(self) -> None:
        with visual.visual_observation_session() as memo:
            self.observe()
            installed_before = self.installed.call_count
            with self.assertRaisesRegex(ValueError, "SHA-256 mismatch"):
                visual.observe_image(IMAGE_BYTES, expected_input_sha256="f" * 64, timeout=12)
            self.assertEqual(self.worker.call_count, 1)
            self.assertEqual(self.installed.call_count, installed_before)
            self.assertEqual(memo.stats()["hits"], 0)

    def test_worker_input_hash_mismatch_is_not_stored(self) -> None:
        def invalid(task: str, raw: bytes, **kwargs: object) -> dict[str, object]:
            result = self.result(task, raw, **kwargs)
            result["input_image_sha256"] = "f" * 64
            return result

        with visual.visual_observation_session() as memo:
            self.worker.side_effect = invalid
            with self.assertRaises(visual.VisualObservationError):
                self.observe()
            self.assertEqual(memo.stats()["stores"], 0)
            self.assertEqual(memo.stats()["entries"], 0)


if __name__ == "__main__":
    unittest.main()
