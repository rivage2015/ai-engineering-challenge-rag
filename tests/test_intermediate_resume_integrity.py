from __future__ import annotations

import contextlib
import hashlib
import io
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


REPOSITORY = Path(__file__).resolve().parents[1]
SCRIPTS = REPOSITORY / "scripts"
sys.path.insert(0, str(SCRIPTS))

import adapt_layer1_to_local_memory as adapter  # noqa: E402
import build_intermediate_records as builder  # noqa: E402
import validate_intermediate_records_streaming as streaming_validator  # noqa: E402


RUN_AT = "2026-09-04T00:00:00+00:00"


def fingerprint(label: str) -> dict[str, object]:
    payload = {"fixture": label}
    return {
        "version": "1",
        "sha256": hashlib.sha256(
            json.dumps(
                payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
        ).hexdigest(),
        "payload": payload,
    }


class IntermediateResumeIntegrityTests(unittest.TestCase):
    def run_builder(
        self,
        root: Path,
        output: Path,
        *arguments: str,
        fingerprint_value: dict[str, object],
    ) -> dict[str, object]:
        argv = [
            "build_intermediate_records.py",
            "--root",
            str(root),
            "--out",
            str(output),
            *arguments,
        ]
        stdout = io.StringIO()
        with (
            mock.patch.object(sys, "argv", argv),
            mock.patch.object(
                builder, "processing_fingerprint", return_value=fingerprint_value
            ),
            contextlib.redirect_stdout(stdout),
        ):
            builder.main()
        return json.loads(stdout.getvalue())

    @staticmethod
    def state(output: Path) -> dict[str, object]:
        return json.loads((output / "build-state.json").read_text(encoding="utf-8"))

    @staticmethod
    def fingerprint_for_reader_version(version: str) -> dict[str, object]:
        fixed_runtime = {"status": "unavailable"}
        with (
            mock.patch.object(builder, "_code_identity", return_value=fixed_runtime),
            mock.patch.object(
                builder,
                "_reader_distribution_identities",
                return_value={
                    "openpyxl": {
                        "status": "available",
                        "version": version,
                        "distribution_files": {
                            "status": "available",
                            "manifest_sha256": "f" * 64,
                        },
                        "module_files": {
                            "openpyxl": {
                                "status": "available",
                                "sha256": "e" * 64,
                            }
                        },
                    }
                },
            ),
            mock.patch.object(
                builder, "_fixed_ocr_runtime_identity", return_value=fixed_runtime
            ),
            mock.patch.object(builder, "_paddle_runtime_identity", return_value=fixed_runtime),
            mock.patch.object(builder, "_local_vlm_identities", return_value=[]),
            mock.patch.object(
                builder, "_pdfkit_jxa_backend_identity", return_value=fixed_runtime
            ),
        ):
            return builder.processing_fingerprint()

    @staticmethod
    def fingerprint_for_ollama_version(version: str) -> dict[str, object]:
        fixed_runtime = {"status": "unavailable"}
        with (
            mock.patch.object(builder, "_code_identity", return_value=fixed_runtime),
            mock.patch.object(
                builder, "_reader_distribution_identities", return_value={}
            ),
            mock.patch.object(
                builder, "_fixed_ocr_runtime_identity", return_value=fixed_runtime
            ),
            mock.patch.object(
                builder, "_paddle_runtime_identity", return_value=fixed_runtime
            ),
            mock.patch.object(
                builder, "_pdfkit_jxa_backend_identity", return_value=fixed_runtime
            ),
            mock.patch.object(
                builder,
                "_local_vlm_identities",
                return_value=[{
                    "model": "gemma4:12b",
                    "prompt_sha256": "p" * 64,
                    "ollama_endpoint": {
                        "status": "available",
                        "server_version_status": "available",
                        "server_version": version,
                        "api_path": "/api/version",
                        "local_executable_candidates": {
                            "status": "unavailable",
                            "candidates": [],
                        },
                    },
                    "installed_model": {
                        "status": "available",
                        "digest": "d" * 64,
                    },
                }],
            ),
        ):
            return builder.processing_fingerprint()

    @staticmethod
    def fingerprint_for_paddle_dependency(
        dependency: str, version: str
    ) -> dict[str, object]:
        fixed_runtime = {"status": "unavailable"}
        packages = {
            "numpy": "2.4.3",
            "paddleocr": "3.7.0",
            "paddlepaddle": "3.3.0",
            "paddlex": "3.7.0",
            "pillow": "12.1.1",
        }
        packages[dependency] = version
        entries = [[name, packages[name]] for name in sorted(packages)]
        paddle_identity = {
            "status": "available",
            "installed_distributions": {
                "normalization": "re.sub(r'[-_.]+', '-', name).lower()",
                "package_count": len(entries),
                "manifest_sha256": hashlib.sha256(
                    json.dumps(
                        entries,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ).hexdigest(),
                "packages": packages,
                "runtime_lock": {
                    "sha256": "l" * 64,
                    "package_count": len(entries),
                    "fully_matched": True,
                },
            },
        }
        with (
            mock.patch.object(builder, "_code_identity", return_value=fixed_runtime),
            mock.patch.object(
                builder, "_reader_distribution_identities", return_value={}
            ),
            mock.patch.object(
                builder, "_fixed_ocr_runtime_identity", return_value=fixed_runtime
            ),
            mock.patch.object(
                builder, "_paddle_runtime_identity", return_value=paddle_identity
            ),
            mock.patch.object(
                builder, "_pdfkit_jxa_backend_identity", return_value=fixed_runtime
            ),
            mock.patch.object(builder, "_local_vlm_identities", return_value=[]),
        ):
            return builder.processing_fingerprint()

    @staticmethod
    def fingerprint_for_code_dependency(
        dependency: str, version: str
    ) -> dict[str, object]:
        fixed_runtime = {"status": "unavailable"}

        def code_identity(path: Path) -> dict[str, object]:
            identity_version = version if path.name == dependency else "stable"
            raw = f"{path.name}:{identity_version}".encode("utf-8")
            return {
                "status": "available",
                "sha256": hashlib.sha256(raw).hexdigest(),
                "size_bytes": len(raw),
            }

        with (
            mock.patch.object(builder, "_code_identity", side_effect=code_identity),
            mock.patch.object(
                builder, "_reader_distribution_identities", return_value={}
            ),
            mock.patch.object(
                builder, "_fixed_ocr_runtime_identity", return_value=fixed_runtime
            ),
            mock.patch.object(
                builder, "_paddle_runtime_identity", return_value=fixed_runtime
            ),
            mock.patch.object(
                builder, "_pdfkit_jxa_backend_identity", return_value=fixed_runtime
            ),
            mock.patch.object(builder, "_local_vlm_identities", return_value=[]),
        ):
            return builder.processing_fingerprint()

    def test_max_files_keeps_terminal_state_in_progress_until_all_forced_inputs_run(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="aiec-force-resume-") as temporary:
            base = Path(temporary)
            root = base / "source"
            root.mkdir()
            first = root / "a.txt"
            second = root / "b.txt"
            first.write_text("first\n", encoding="utf-8")
            second.write_text("second\n", encoding="utf-8")
            output = base / "intermediate"
            fp = fingerprint("same-reader")

            initial = self.run_builder(
                root, output, "--run-at", RUN_AT, fingerprint_value=fp
            )
            self.assertEqual(initial["build_status"], "complete")

            limited = self.run_builder(
                root,
                output,
                "--resume",
                "--force-input",
                str(first),
                str(second),
                "--max-files",
                "1",
                fingerprint_value=fp,
            )
            self.assertEqual(limited["processed_now"], 1)
            self.assertEqual(limited["build_status"], "in_progress")
            state = self.state(output)
            self.assertEqual(state["build_status"], "in_progress")
            self.assertNotIn("totals", state)
            self.assertNotIn("aggregates", state)

            completed = self.run_builder(
                root,
                output,
                "--resume",
                "--force-input",
                str(second),
                "--max-files",
                "1",
                fingerprint_value=fp,
            )
            self.assertEqual(completed["processed_now"], 1)
            self.assertEqual(completed["build_status"], "complete")

    def test_partial_reuse_requires_current_reader_fingerprint(self) -> None:
        with tempfile.TemporaryDirectory(prefix="aiec-partial-fingerprint-") as temporary:
            base = Path(temporary)
            root = base / "source"
            root.mkdir()
            (root / "empty.txt").write_bytes(b"")
            output = base / "intermediate"
            first_fp = fingerprint("reader-v1")
            second_fp = fingerprint("reader-v2")

            initial = self.run_builder(
                root, output, "--run-at", RUN_AT, fingerprint_value=first_fp
            )
            self.assertEqual(initial["processed_now"], 1)
            entry = self.state(output)["entries"]["empty.txt"]
            self.assertEqual(entry["status"], "partial")
            self.assertEqual(
                entry["processing_fingerprint_sha256"], first_fp["sha256"]
            )

            unchanged = self.run_builder(
                root, output, "--resume", fingerprint_value=first_fp
            )
            self.assertEqual(unchanged["processed_now"], 0)
            self.assertEqual(unchanged["skipped_now"], 1)

            changed = self.run_builder(
                root, output, "--resume", fingerprint_value=second_fp
            )
            self.assertEqual(changed["processed_now"], 1)
            self.assertEqual(
                self.state(output)["entries"]["empty.txt"][
                    "processing_fingerprint_sha256"
                ],
                second_fp["sha256"],
            )

            state = self.state(output)
            del state["entries"]["empty.txt"]["processing_fingerprint_sha256"]
            (output / "build-state.json").write_text(
                json.dumps(state, ensure_ascii=False, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            legacy_missing = self.run_builder(
                root, output, "--resume", fingerprint_value=second_fp
            )
            self.assertEqual(legacy_missing["processed_now"], 1)

    def test_success_is_reprocessed_when_reader_fingerprint_changes(self) -> None:
        with tempfile.TemporaryDirectory(prefix="aiec-success-fingerprint-") as temporary:
            base = Path(temporary)
            root = base / "source"
            root.mkdir()
            (root / "complete.txt").write_text("complete\n", encoding="utf-8")
            output = base / "intermediate"
            self.run_builder(
                root,
                output,
                "--run-at",
                RUN_AT,
                fingerprint_value=fingerprint("reader-v1"),
            )
            unchanged = self.run_builder(
                root,
                output,
                "--resume",
                fingerprint_value=fingerprint("reader-v1"),
            )
            self.assertEqual(unchanged["processed_now"], 0)
            self.assertEqual(unchanged["skipped_now"], 1)
            resumed = self.run_builder(
                root,
                output,
                "--resume",
                fingerprint_value=fingerprint("reader-v2"),
            )
            self.assertEqual(resumed["processed_now"], 1)
            self.assertEqual(resumed["skipped_now"], 0)
            self.assertEqual(resumed["build_status"], "complete")
            self.assertEqual(
                self.state(output)["entries"]["complete.txt"][
                    "processing_fingerprint_sha256"
                ],
                fingerprint("reader-v2")["sha256"],
            )

    def test_parser_distribution_version_change_reprocesses_every_terminal_status(
        self,
    ) -> None:
        first_fp = self.fingerprint_for_reader_version("3.1.5")
        second_fp = self.fingerprint_for_reader_version("3.1.6")
        self.assertNotEqual(first_fp["sha256"], second_fp["sha256"])
        with tempfile.TemporaryDirectory(prefix="aiec-parser-version-") as temporary:
            base = Path(temporary)
            root = base / "source"
            root.mkdir()
            (root / "sample.txt").write_text("source\n", encoding="utf-8")
            baseline = base / "baseline"
            self.run_builder(
                root,
                baseline,
                "--run-at",
                RUN_AT,
                fingerprint_value=first_fp,
            )
            for status in sorted(builder.TERMINAL_STATUSES):
                with self.subTest(status=status):
                    output = base / f"resume-{status}"
                    shutil.copytree(baseline, output)
                    state = self.state(output)
                    state["entries"]["sample.txt"]["status"] = status
                    if status == "failed":
                        state["build_status"] = "complete_with_failures"
                    (output / "build-state.json").write_text(
                        json.dumps(state, ensure_ascii=False, sort_keys=True) + "\n",
                        encoding="utf-8",
                    )
                    resumed = self.run_builder(
                        root,
                        output,
                        "--resume",
                        fingerprint_value=second_fp,
                    )
                    self.assertEqual(resumed["processed_now"], 1)
                    self.assertEqual(resumed["skipped_now"], 0)
                    self.assertEqual(resumed["build_status"], "complete")

    def test_paddle_non_primary_dependency_change_reprocesses_every_terminal_status(
        self,
    ) -> None:
        first_fp = self.fingerprint_for_paddle_dependency("numpy", "2.4.3")
        second_fp = self.fingerprint_for_paddle_dependency("numpy", "2.4.4")
        self.assertNotEqual(first_fp["sha256"], second_fp["sha256"])
        first_manifest = first_fp["payload"]["ocr"]["paddleocr"][
            "installed_distributions"
        ]
        second_manifest = second_fp["payload"]["ocr"]["paddleocr"][
            "installed_distributions"
        ]
        self.assertEqual(first_manifest["packages"]["numpy"], "2.4.3")
        self.assertEqual(second_manifest["packages"]["numpy"], "2.4.4")
        self.assertNotEqual(
            first_manifest["manifest_sha256"],
            second_manifest["manifest_sha256"],
        )
        with tempfile.TemporaryDirectory(prefix="aiec-paddle-dependency-") as temporary:
            base = Path(temporary)
            root = base / "source"
            root.mkdir()
            (root / "sample.txt").write_text("source\n", encoding="utf-8")
            baseline = base / "baseline"
            self.run_builder(
                root,
                baseline,
                "--run-at",
                RUN_AT,
                fingerprint_value=first_fp,
            )
            for status in sorted(builder.TERMINAL_STATUSES):
                with self.subTest(status=status):
                    output = base / f"resume-{status}"
                    shutil.copytree(baseline, output)
                    state = self.state(output)
                    state["entries"]["sample.txt"]["status"] = status
                    if status == "failed":
                        state["build_status"] = "complete_with_failures"
                    (output / "build-state.json").write_text(
                        json.dumps(
                            state,
                            ensure_ascii=False,
                            sort_keys=True,
                        ) + "\n",
                        encoding="utf-8",
                    )
                    resumed = self.run_builder(
                        root,
                        output,
                        "--resume",
                        fingerprint_value=second_fp,
                    )
                    self.assertEqual(resumed["processed_now"], 1)
                    self.assertEqual(resumed["skipped_now"], 0)
                    self.assertEqual(resumed["build_status"], "complete")

    def test_invalid_paddle_distribution_manifest_fails_closed(self) -> None:
        import local_image_ocr
        import local_paddle_ocr

        runtime = {
            "python": Path("/runtime/bin/python"),
            "python_target": Path("/runtime/bin/python3.12"),
            "worker": Path("/runtime/local_paddle_ocr.py"),
            "model_root": Path("/runtime/models"),
            "runtime_lock": Path("/runtime/requirements.lock"),
        }
        with (
            mock.patch.object(
                local_image_ocr,
                "resolve_paddle_runtime",
                return_value=runtime,
            ),
            mock.patch.object(
                local_paddle_ocr,
                "verify_models",
                return_value=({}, {}),
            ),
            mock.patch.object(
                builder,
                "_paddle_distribution_manifest_identity",
                side_effect=RuntimeError("duplicate or non-lock-matching manifest"),
            ),
        ):
            self.assertEqual(
                builder._paddle_runtime_identity(),
                {
                    "configuration": {
                        "AIEC_PADDLE_PYTHON": None,
                        "AIEC_PADDLE_MODEL_ROOT": None,
                    },
                    "status": "unavailable",
                },
            )

    def test_paddle_manifest_probe_rejects_invalid_duplicate_and_lock_mismatch(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="aiec-paddle-manifest-") as temporary:
            base = Path(temporary)
            runtime_lock = base / "requirements.lock"
            runtime_lock.write_text(
                "numpy==1.0\nPillow==2.0\n",
                encoding="utf-8",
            )
            lock_sha256 = hashlib.sha256(runtime_lock.read_bytes()).hexdigest()
            lock_proof = {
                "sha256": lock_sha256,
                "package_count": 2,
                "fully_matched": True,
            }

            def probe(payload: object) -> dict[str, object]:
                def fake_run(command: list[str], **_kwargs: object) -> mock.Mock:
                    Path(command[5]).write_text(
                        json.dumps(
                            payload,
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                        encoding="utf-8",
                    )
                    return mock.Mock(returncode=0)

                with mock.patch.object(
                    builder.subprocess,
                    "run",
                    side_effect=fake_run,
                ):
                    return builder._paddle_distribution_manifest_identity(
                        base / "python",
                        base / "local_paddle_ocr.py",
                        runtime_lock,
                        base / "models",
                        lock_sha256,
                        {},
                    )

            valid = probe({
                "runtime_lock": lock_proof,
                "installed_distributions": [
                    ["numpy", "1.0"],
                    ["pillow", "2.0"],
                ],
            })
            self.assertEqual(
                valid["packages"],
                {"numpy": "1.0", "pillow": "2.0"},
            )
            corruptions = (
                (
                    "invalid",
                    {
                        "runtime_lock": lock_proof,
                        "installed_distributions": "not-a-list",
                    },
                    "entries are invalid",
                ),
                (
                    "duplicate",
                    {
                        "runtime_lock": lock_proof,
                        "installed_distributions": [
                            ["numpy", "1.0"],
                            ["numpy", "1.0"],
                        ],
                    },
                    "not canonical and unique",
                ),
                (
                    "lock-mismatch",
                    {
                        "runtime_lock": lock_proof,
                        "installed_distributions": [
                            ["numpy", "1.1"],
                            ["pillow", "2.0"],
                        ],
                    },
                    "does not match the lock",
                ),
            )
            for label, payload, message in corruptions:
                with self.subTest(label=label):
                    with self.assertRaisesRegex(RuntimeError, message):
                        probe(payload)

    def test_ollama_version_change_reprocesses_every_terminal_status(self) -> None:
        first_fp = self.fingerprint_for_ollama_version("0.11.8")
        second_fp = self.fingerprint_for_ollama_version("0.11.9")
        self.assertNotEqual(first_fp["sha256"], second_fp["sha256"])
        with tempfile.TemporaryDirectory(prefix="aiec-ollama-version-") as temporary:
            base = Path(temporary)
            root = base / "source"
            root.mkdir()
            (root / "sample.txt").write_text("source\n", encoding="utf-8")
            baseline = base / "baseline"
            self.run_builder(
                root,
                baseline,
                "--run-at",
                RUN_AT,
                fingerprint_value=first_fp,
            )
            for status in sorted(builder.TERMINAL_STATUSES):
                with self.subTest(status=status):
                    output = base / f"resume-{status}"
                    shutil.copytree(baseline, output)
                    state = self.state(output)
                    state["entries"]["sample.txt"]["status"] = status
                    if status == "failed":
                        state["build_status"] = "complete_with_failures"
                    (output / "build-state.json").write_text(
                        json.dumps(state, ensure_ascii=False, sort_keys=True) + "\n",
                        encoding="utf-8",
                    )
                    resumed = self.run_builder(
                        root,
                        output,
                        "--resume",
                        fingerprint_value=second_fp,
                    )
                    self.assertEqual(resumed["processed_now"], 1)
                    self.assertEqual(resumed["skipped_now"], 0)
                    self.assertEqual(resumed["build_status"], "complete")

    def test_direct_code_dependency_change_reprocesses_every_terminal_status(
        self,
    ) -> None:
        dependencies = (
            "evidence_text_chunking.py",
            "classify_visual_assets.py",
            "validate_visual_classifications.py",
        )
        self.assertTrue(set(dependencies) <= set(builder.PROCESSING_CODE_FILES))
        with tempfile.TemporaryDirectory(prefix="aiec-code-dependency-") as temporary:
            base = Path(temporary)
            root = base / "source"
            root.mkdir()
            (root / "sample.txt").write_text("source\n", encoding="utf-8")
            for dependency in dependencies:
                first_fp = self.fingerprint_for_code_dependency(dependency, "v1")
                second_fp = self.fingerprint_for_code_dependency(dependency, "v2")
                self.assertNotEqual(first_fp["sha256"], second_fp["sha256"])
                baseline = base / f"baseline-{dependency}"
                self.run_builder(
                    root,
                    baseline,
                    "--run-at",
                    RUN_AT,
                    fingerprint_value=first_fp,
                )
                for status in sorted(builder.TERMINAL_STATUSES):
                    with self.subTest(dependency=dependency, status=status):
                        output = base / f"resume-{dependency}-{status}"
                        shutil.copytree(baseline, output)
                        state = self.state(output)
                        state["entries"]["sample.txt"]["status"] = status
                        if status == "failed":
                            state["build_status"] = "complete_with_failures"
                        (output / "build-state.json").write_text(
                            json.dumps(
                                state,
                                ensure_ascii=False,
                                sort_keys=True,
                            ) + "\n",
                            encoding="utf-8",
                        )
                        resumed = self.run_builder(
                            root,
                            output,
                            "--resume",
                            fingerprint_value=second_fp,
                        )
                        self.assertEqual(resumed["processed_now"], 1)
                        self.assertEqual(resumed["skipped_now"], 0)
                        self.assertEqual(resumed["build_status"], "complete")

    def test_resume_rejects_noncanonical_input_path_state_before_mutation(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="aiec-resume-input-paths-") as temporary:
            base = Path(temporary)
            root = base / "source"
            root.mkdir()
            (root / "a.txt").write_text("first\n", encoding="utf-8")
            (root / "é.txt").write_text("second\n", encoding="utf-8")
            baseline = base / "baseline"
            fp = fingerprint("canonical-inputs")
            self.run_builder(
                root,
                baseline,
                "--run-at",
                RUN_AT,
                fingerprint_value=fp,
            )
            original_paths = self.state(baseline)["input_paths"]
            self.assertEqual(original_paths, ["a.txt", "é.txt"])
            corruptions = (
                ("not-list", "a.txt", "non-empty list of strings"),
                ("empty", [], "non-empty list of strings"),
                ("non-string", ["a.txt", 7], "non-empty list of strings"),
                ("duplicate", ["a.txt", "a.txt"], "must be unique"),
                (
                    "reverse-order",
                    list(reversed(original_paths)),
                    "canonical sorted order",
                ),
                (
                    "dot-segment",
                    ["./a.txt", "é.txt"],
                    "non-canonical relative path",
                ),
                (
                    "non-nfc",
                    ["a.txt", "e\u0301.txt"],
                    "non-canonical relative path",
                ),
            )
            for label, input_paths, message in corruptions:
                with self.subTest(label=label):
                    output = base / label
                    shutil.copytree(baseline, output)
                    state = self.state(output)
                    state["input_paths"] = input_paths
                    state_path = output / "build-state.json"
                    state_path.write_text(
                        json.dumps(
                            state,
                            ensure_ascii=False,
                            sort_keys=True,
                        ) + "\n",
                        encoding="utf-8",
                    )
                    before = state_path.read_bytes()
                    with self.assertRaisesRegex(SystemExit, message):
                        self.run_builder(
                            root,
                            output,
                            "--resume",
                            fingerprint_value=fp,
                        )
                    self.assertEqual(state_path.read_bytes(), before)

    def test_aggregate_append_is_rejected_by_validator_and_adapter(self) -> None:
        with tempfile.TemporaryDirectory(prefix="aiec-aggregate-binding-") as temporary:
            base = Path(temporary)
            root = base / "source"
            root.mkdir()
            (root / "sample.txt").write_text("trusted source\n", encoding="utf-8")
            output = base / "intermediate"
            fp = fingerprint("reader")
            self.run_builder(
                root, output, "--run-at", RUN_AT, fingerprint_value=fp
            )

            state = self.state(output)
            self.assertEqual(set(state["aggregates"]), set(builder.RECORD_FILES))
            self.assertEqual(
                streaming_validator.validate(output, root, published_schema=False),
                {"document": 1, "evidence": 1, "relation": 1},
            )
            adapter.adapt(output, root.resolve(), base / "adapter-before-tamper")

            with (output / "evidence.jsonl").open("ab") as handle:
                handle.write(b"\n")

            with self.assertRaisesRegex(ValueError, "aggregate.evidence"):
                streaming_validator.validate(output, root, published_schema=False)
            with self.assertRaisesRegex(ValueError, "aggregate.evidence"):
                adapter.adapt(output, root.resolve(), base / "adapter-after-tamper")

    def test_processing_fingerprint_binds_local_visual_model_and_prompt(self) -> None:
        digest = "d" * 64
        endpoint_identity = {
            "status": "available",
            "server_version_status": "available",
            "server_version": "0.11.9",
            "api_path": "/api/version",
            "local_executable_candidates": {
                "status": "unavailable",
                "candidates": [],
            },
        }
        with (
            mock.patch.object(
                builder, "_ollama_endpoint_identity", return_value=endpoint_identity
            ),
            mock.patch.object(
                builder,
                "_ollama_inventory",
                return_value={"gemma4:12b": {digest}},
            ),
        ):
            identities = builder._local_vlm_identities()
        self.assertEqual(len(identities), 2)
        for identity in identities:
            self.assertEqual(identity["model"], "gemma4:12b")
            self.assertRegex(identity["prompt_sha256"], r"^[0-9a-f]{64}$")
            self.assertEqual(identity["installed_model"], {
                "status": "available", "digest": digest,
            })
            self.assertEqual(identity["ollama_endpoint"], endpoint_identity)

    def test_ollama_endpoint_identity_is_normalized_and_loopback_only(self) -> None:
        with (
            mock.patch.object(
                builder,
                "_ollama_json",
                return_value={"version": "  v0.11.9-rc.1  "},
            ) as get_json,
            mock.patch.object(
                builder,
                "_ollama_executable_identities",
                return_value={"status": "unavailable", "candidates": []},
            ),
        ):
            identity = builder._ollama_endpoint_identity("127.0.0.1", 11434)
        self.assertEqual(identity["status"], "available")
        self.assertEqual(identity["server_version"], "0.11.9-rc.1")
        get_json.assert_called_once_with(
            "127.0.0.1",
            11434,
            "/api/version",
            maximum_bytes=64 * 1024,
        )

        with mock.patch.object(builder, "_ollama_json") as get_json:
            rejected = builder._ollama_endpoint_identity("localhost", 11434)
        self.assertEqual(rejected["status"], "rejected_non_loopback")
        self.assertEqual(rejected["server_version_status"], "unavailable")
        get_json.assert_not_called()

    def test_ollama_endpoint_identity_fails_closed_without_valid_version(self) -> None:
        fixed_executables = {"status": "unavailable", "candidates": []}
        for payload, expected_status in (
            (None, "endpoint_unavailable"),
            ({"version": "not a version"}, "invalid_contract"),
        ):
            with self.subTest(payload=payload):
                with (
                    mock.patch.object(builder, "_ollama_json", return_value=payload),
                    mock.patch.object(
                        builder,
                        "_ollama_executable_identities",
                        return_value=fixed_executables,
                    ),
                ):
                    identity = builder._ollama_endpoint_identity(
                        "127.0.0.1", 11434
                    )
                self.assertEqual(identity["status"], "unavailable")
                self.assertEqual(
                    identity["server_version_status"], expected_status
                )
                self.assertNotIn("server_version", identity)

    def test_model_digest_change_changes_processing_fingerprint(self) -> None:
        fixed_runtime = {"status": "unavailable"}
        with (
            mock.patch.object(builder, "_code_identity", return_value=fixed_runtime),
            mock.patch.object(
                builder, "_fixed_ocr_runtime_identity", return_value=fixed_runtime
            ),
            mock.patch.object(builder, "_paddle_runtime_identity", return_value=fixed_runtime),
            mock.patch.object(
                builder,
                "_local_vlm_identities",
                return_value=[{
                    "model": "gemma4:12b",
                    "prompt_sha256": "p" * 64,
                    "installed_model": {"status": "available", "digest": "a" * 64},
                }],
            ),
        ):
            first = builder.processing_fingerprint()["sha256"]
        with (
            mock.patch.object(builder, "_code_identity", return_value=fixed_runtime),
            mock.patch.object(
                builder, "_fixed_ocr_runtime_identity", return_value=fixed_runtime
            ),
            mock.patch.object(builder, "_paddle_runtime_identity", return_value=fixed_runtime),
            mock.patch.object(
                builder,
                "_local_vlm_identities",
                return_value=[{
                    "model": "gemma4:12b",
                    "prompt_sha256": "p" * 64,
                    "installed_model": {"status": "available", "digest": "b" * 64},
                }],
            ),
        ):
            second = builder.processing_fingerprint()["sha256"]
        self.assertNotEqual(first, second)


if __name__ == "__main__":
    unittest.main()
