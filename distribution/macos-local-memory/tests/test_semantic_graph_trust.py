from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock


REPOSITORY = Path(__file__).resolve().parents[3]
MODULE_PATH = (
    REPOSITORY
    / "distribution"
    / "macos-local-memory"
    / "app"
    / "semantic_graph_trust.py"
)


def load_module(name: str, path: Path) -> Any:
    specification = importlib.util.spec_from_file_location(name, path)
    assert specification is not None and specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    sys.modules[name] = module
    specification.loader.exec_module(module)
    return module


trust = load_module("semantic_graph_trust_test_target", MODULE_PATH)


class FakeTrustStore:
    def __init__(self) -> None:
        self.roots: dict[str, str] = {}
        self.create_calls: list[tuple[str, str]] = []
        self.read_calls: list[str] = []

    def create_root(self, generation: str, root_sha256: str) -> None:
        self.create_calls.append((generation, root_sha256))
        if generation in self.roots:
            raise trust.SemanticGraphTrustError("trust_store_create_failed")
        self.roots[generation] = root_sha256

    def read_root(self, generation: str) -> str:
        self.read_calls.append(generation)
        try:
            return self.roots[generation]
        except KeyError as exc:
            raise trust.SemanticGraphTrustError(
                "trust_store_read_failed"
            ) from exc


class SemanticGraphTrustTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.generation = self.root / ("generation-" + "a" * 32)
        self.storage = self.generation / trust.STORAGE_DIR
        self.storage.mkdir(parents=True)
        self.database = self.storage / trust.STORAGE_DATABASE
        self.state_path = self.storage / trust.STORAGE_STATE
        self.base = self.generation / trust.BASE_DATABASE
        self.database.write_bytes(b"semantic storage database\n")
        self.base.write_bytes(b"base answer database\n")
        self.build_id = "b" * 32
        self.logical_sha256 = "c" * 64
        self.snapshot_id = "xkgs_" + self.logical_sha256[:32]
        self.counts = {"nodes": 7, "edges": 11, "edge_evidence": 13}
        self.storage_state = {
            "schema_version": "0.1",
            "record_type": (
                "cross_document_semantic_graph_answer_index_projection_state"
            ),
            "projector": (
                "cross-document-semantic-graph-answer-index-projector"
            ),
            "status": "complete",
            "generation": self.generation.name,
            "question_independent": True,
            "external_network_used": False,
            "storage_only": True,
            "retrieval_enabled": False,
            "used_for_answers": False,
            "answer_behavior_changed": False,
            "output": {
                "sqlite_file": trust.STORAGE_DATABASE,
                "state_file": trust.STORAGE_STATE,
                "sqlite_sha256": self.digest(self.database),
            },
            "base": {
                "sqlite_file": trust.BASE_DATABASE,
                "sqlite_sha256": self.digest(self.base),
            },
            "shadow": {
                "build_id": self.build_id,
                "graph_snapshot_id": self.snapshot_id,
                "logical_snapshot_sha256": self.logical_sha256,
            },
            "projection_sha256": "d" * 64,
            "counts": dict(self.counts),
        }
        self.write_state()
        self.registration = self.make_registration()
        self.store = FakeTrustStore()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def digest(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def write_state(self) -> None:
        self.state_path.write_text(
            trust.canonical_json(self.storage_state) + "\n",
            encoding="utf-8",
        )

    def make_registration(self) -> dict[str, Any]:
        return {
            "schema_version": "0.1",
            "status": "validated_storage_only",
            "generation": self.generation.name,
            "database_path": str(self.database),
            "database_sha256": self.digest(self.database),
            "state_path": str(self.state_path),
            "state_sha256": self.digest(self.state_path),
            "base_index_path": str(self.base),
            "base_index_sha256": self.digest(self.base),
            "graph_snapshot_id": self.snapshot_id,
            "logical_snapshot_sha256": self.logical_sha256,
            "counts": dict(self.counts),
            "retrieval_enabled": False,
            "used_for_answers": False,
        }

    def assert_reason(self, reason: str, call) -> None:
        with self.assertRaises(trust.SemanticGraphTrustError) as observed:
            call()
        self.assertEqual(reason, observed.exception.reason_code)

    def publish(self) -> dict[str, Any]:
        return trust.publish_trust_root(
            self.generation,
            self.build_id,
            self.registration,
            self.storage_state,
            self.store,
        )

    def test_build_manifest_is_closed_deterministic_and_fully_bound(self) -> None:
        first = trust.build_trust_manifest(
            self.generation,
            self.build_id,
            self.registration,
            self.storage_state,
        )
        second = trust.build_trust_manifest(
            self.generation,
            self.build_id,
            copy.deepcopy(self.registration),
            copy.deepcopy(self.storage_state),
        )
        self.assertEqual(first, second)
        self.assertEqual(trust.MANIFEST_FIELDS, set(first))
        self.assertEqual(
            trust.canonical_sha256(self.registration),
            first["storage_registration_sha256"],
        )
        self.assertEqual(self.snapshot_id, first["graph"]["graph_snapshot_id"])
        self.assertEqual("d" * 64, first["graph"]["projection_sha256"])
        self.assertEqual(self.counts, first["graph"]["counts"])

        extra = copy.deepcopy(first)
        extra["created_at"] = "2026-09-04T00:00:00+09:00"
        self.assert_reason(
            "trust_manifest_contract_invalid",
            lambda: trust.validate_trust_manifest(
                extra, self.generation, self.registration
            ),
        )

    def test_publish_uses_private_manifest_and_create_only_root(self) -> None:
        registration = self.publish()
        manifest_path = trust.trust_manifest_path(self.generation)
        self.assertEqual(0o600, stat.S_IMODE(manifest_path.stat().st_mode))
        self.assertEqual(0o700, stat.S_IMODE(self.storage.stat().st_mode))
        self.assertEqual("trusted", registration["status"])
        self.assertEqual(1, len(self.store.create_calls))
        self.assertEqual(1, len(self.store.read_calls))

        verified = trust.validate_trust_root(
            self.generation, self.registration, self.store
        )
        self.assertEqual("trusted", verified["status"])
        self.assertTrue(verified["allows_answer_activation"])
        self.assertEqual(self.snapshot_id, verified["graph_snapshot_id"])
        self.assert_reason(
            "trust_manifest_already_exists",
            self.publish,
        )
        self.assertEqual(1, len(self.store.create_calls))

    def test_config_locator_is_closed_and_binds_published_to_verified_root(self) -> None:
        locator = self.publish()
        verified = trust.validate_trust_root(
            self.generation, self.registration, self.store
        )
        self.assertEqual(trust.TRUST_REGISTRATION_FIELDS, set(locator))
        self.assertEqual(
            locator,
            trust.validate_trust_registration(
                locator,
                self.generation,
                self.registration,
                verified_root=verified,
            ),
        )

        extra = {**locator, "allows_answer_activation": True}
        self.assert_reason(
            "trust_registration_locator_invalid",
            lambda: trust.validate_trust_registration(
                extra, self.generation, self.registration
            ),
        )
        swapped = {**locator, "projection_sha256": "0" * 64}
        self.assert_reason(
            "trust_verified_root_binding_invalid",
            lambda: trust.validate_trust_registration(
                swapped,
                self.generation,
                self.registration,
                verified_root=verified,
            ),
        )

    def test_missing_or_mismatched_root_fails_closed(self) -> None:
        manifest = trust.build_trust_manifest(
            self.generation,
            self.build_id,
            self.registration,
            self.storage_state,
        )
        trust.write_trust_manifest(
            self.generation, manifest, self.registration
        )
        missing = trust.inspect_trust_root(
            self.generation, self.registration, self.store
        )
        self.assertEqual("rejected", missing["status"])
        self.assertEqual("trust_store_read_failed", missing["reason_code"])
        self.assertFalse(missing["allows_answer_activation"])

        self.store.roots[self.generation.name] = "0" * 64
        mismatch = trust.inspect_trust_root(
            self.generation, self.registration, self.store
        )
        self.assertEqual("trust_root_mismatch", mismatch["reason_code"])
        self.assertFalse(mismatch["allows_answer_activation"])

    def test_artifact_and_coordinated_registration_tampering_are_rejected(self) -> None:
        self.publish()
        self.database.write_bytes(b"modified database\n")
        changed_file = trust.inspect_trust_root(
            self.generation, self.registration, self.store
        )
        self.assertEqual(
            "trust_artifact_hash_mismatch", changed_file["reason_code"]
        )

        self.database.write_bytes(b"coordinated replacement database\n")
        self.storage_state["output"]["sqlite_sha256"] = self.digest(
            self.database
        )
        self.storage_state["projection_sha256"] = "e" * 64
        self.write_state()
        rewritten_registration = self.make_registration()
        coordinated = trust.inspect_trust_root(
            self.generation, rewritten_registration, self.store
        )
        self.assertEqual(
            "trust_manifest_contract_invalid", coordinated["reason_code"]
        )
        self.assertFalse(coordinated["allows_answer_activation"])

    def test_manifest_mode_symlink_hardlink_and_noncanonical_bytes_rejected(self) -> None:
        self.publish()
        manifest_path = trust.trust_manifest_path(self.generation)
        os.chmod(self.storage, 0o755)
        self.assertEqual(
            "trust_manifest_directory_invalid",
            trust.inspect_trust_root(
                self.generation, self.registration, self.store
            )["reason_code"],
        )
        os.chmod(self.storage, 0o700)
        os.chmod(manifest_path, 0o644)
        self.assertEqual(
            "trust_manifest_file_invalid",
            trust.inspect_trust_root(
                self.generation, self.registration, self.store
            )["reason_code"],
        )
        os.chmod(manifest_path, 0o600)
        content = manifest_path.read_bytes()
        manifest_path.unlink()
        target = self.root / "outside-manifest.json"
        target.write_bytes(content)
        os.chmod(target, 0o600)
        manifest_path.symlink_to(target)
        self.assertEqual(
            "trust_manifest_file_invalid",
            trust.inspect_trust_root(
                self.generation, self.registration, self.store
            )["reason_code"],
        )
        manifest_path.unlink()
        os.link(target, manifest_path)
        self.assertEqual(
            "trust_manifest_file_invalid",
            trust.inspect_trust_root(
                self.generation, self.registration, self.store
            )["reason_code"],
        )
        manifest_path.unlink()
        target.unlink()
        manifest = json.loads(content.decode("utf-8"))
        manifest_path.write_text(
            trust.canonical_json(manifest), encoding="utf-8"
        )
        os.chmod(manifest_path, 0o600)
        self.assertEqual(
            "trust_manifest_not_canonical",
            trust.inspect_trust_root(
                self.generation, self.registration, self.store
            )["reason_code"],
        )

    def test_query_validation_reads_state_from_one_nofollow_descriptor(self) -> None:
        self.publish()
        with mock.patch.object(
            Path,
            "read_bytes",
            side_effect=AssertionError("path reopened outside guarded descriptor"),
        ):
            verified = trust.validate_trust_root(
                self.generation, self.registration, self.store
            )
        self.assertEqual("trusted", verified["status"])

    def test_duplicate_keys_and_nonfinite_json_are_rejected(self) -> None:
        manifest_path = trust.trust_manifest_path(self.generation)
        os.chmod(self.storage, 0o700)
        for payload in (
            b'{"schema_version":"0.1","schema_version":"0.1"}\n',
            b'{"projection_sha256":NaN}\n',
        ):
            with self.subTest(payload=payload):
                manifest_path.unlink(missing_ok=True)
                manifest_path.write_bytes(payload)
                os.chmod(manifest_path, 0o600)
                observed = trust.inspect_trust_root(
                    self.generation, self.registration, self.store
                )
                self.assertEqual(
                    "trust_manifest_json_invalid", observed["reason_code"]
                )
                self.assertFalse(observed["allows_answer_activation"])

    def test_recovery_requires_preexisting_matching_root_and_never_creates(self) -> None:
        expected = trust.build_trust_manifest(
            self.generation,
            self.build_id,
            self.registration,
            self.storage_state,
        )
        self.store.roots[self.generation.name] = trust.manifest_sha256(
            expected
        )
        recovered = trust.recover_trust_manifest(
            self.generation,
            self.build_id,
            self.registration,
            self.storage_state,
            self.store,
        )
        self.assertEqual("trusted", recovered["status"])
        self.assertEqual([], self.store.create_calls)
        self.assertTrue(trust.trust_manifest_path(self.generation).is_file())

        second_root = Path(self.temporary.name) / "second"
        second_generation = second_root / ("generation-" + "b" * 32)
        second_storage = second_generation / trust.STORAGE_DIR
        second_storage.mkdir(parents=True)
        second_database = second_storage / trust.STORAGE_DATABASE
        second_state = second_storage / trust.STORAGE_STATE
        second_base = second_generation / trust.BASE_DATABASE
        second_database.write_bytes(self.database.read_bytes())
        second_base.write_bytes(self.base.read_bytes())
        second_value = copy.deepcopy(self.storage_state)
        second_value["generation"] = second_generation.name
        second_state.write_text(
            trust.canonical_json(second_value) + "\n", encoding="utf-8"
        )
        second_registration = copy.deepcopy(self.registration)
        second_registration.update({
            "generation": second_generation.name,
            "database_path": str(second_database),
            "state_path": str(second_state),
            "base_index_path": str(second_base),
            "database_sha256": self.digest(second_database),
            "state_sha256": self.digest(second_state),
            "base_index_sha256": self.digest(second_base),
        })
        self.assert_reason(
            "trust_store_read_failed",
            lambda: trust.recover_trust_manifest(
                second_generation,
                self.build_id,
                second_registration,
                second_value,
                self.store,
            ),
        )
        self.assertFalse(trust.trust_manifest_path(second_generation).exists())

    def test_registration_and_storage_state_contracts_fail_closed(self) -> None:
        unsafe = copy.deepcopy(self.registration)
        unsafe["used_for_answers"] = True
        self.assert_reason(
            "trust_registration_contract_invalid",
            lambda: trust.build_trust_manifest(
                self.generation,
                self.build_id,
                unsafe,
                self.storage_state,
            ),
        )
        swapped = copy.deepcopy(self.storage_state)
        swapped["projection_sha256"] = "not-a-hash"
        self.assert_reason(
            "trust_storage_state_invalid",
            lambda: trust.build_trust_manifest(
                self.generation,
                self.build_id,
                self.registration,
                swapped,
            ),
        )

    def test_keychain_backend_is_create_only_read_only_and_strict(self) -> None:
        calls: list[tuple[list[str], dict[str, Any]]] = []
        digest = "f" * 64

        def runner(command: list[str], **kwargs: Any):
            calls.append((command, kwargs))
            output = digest + "\n" if command[1] == "find-generic-password" else ""
            return subprocess.CompletedProcess(command, 0, output, "")

        backend = trust.KeychainTrustStore(runner=runner)
        with mock.patch.object(trust.sys, "platform", "darwin"):
            backend.create_root(self.generation.name, digest)
            self.assertEqual(digest, backend.read_root(self.generation.name))
        create_command, create_options = calls[0]
        read_command, read_options = calls[1]
        self.assertEqual("/usr/bin/security", create_command[0])
        self.assertIn("add-generic-password", create_command)
        self.assertNotIn("-U", create_command)
        self.assertNotIn("delete-generic-password", create_command)
        self.assertIn("find-generic-password", read_command)
        self.assertEqual(subprocess.DEVNULL, create_options["stdin"])
        self.assertEqual(subprocess.DEVNULL, read_options["stdin"])
        self.assertTrue(create_options["check"])
        self.assertTrue(read_options["close_fds"])

    def test_keychain_timeout_and_malformed_output_fail_closed(self) -> None:
        def timeout(command: list[str], **_kwargs: Any):
            raise subprocess.TimeoutExpired(command, 1)

        timed = trust.KeychainTrustStore(runner=timeout, timeout_seconds=1)
        with mock.patch.object(trust.sys, "platform", "darwin"):
            self.assert_reason(
                "trust_store_timeout",
                lambda: timed.read_root(self.generation.name),
            )

        malformed = trust.KeychainTrustStore(
            runner=lambda command, **kwargs: subprocess.CompletedProcess(
                command, 0, "f" * 64 + "\nextra\n", ""
            )
        )
        with mock.patch.object(trust.sys, "platform", "darwin"):
            self.assert_reason(
                "trust_store_output_invalid",
                lambda: malformed.read_root(self.generation.name),
            )


if __name__ == "__main__":
    unittest.main()
