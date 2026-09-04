#!/usr/bin/env python3
"""Independent trust root for local semantic-graph answer promotion.

The generated index, its state, and ``config.json`` live in the same mutable
filesystem tree.  They therefore cannot establish their own integrity.  This
module binds a closed, deterministic manifest to a per-generation SHA-256
stored separately in the macOS login Keychain.

It deliberately does not decide whether an answer may be promoted.  The
orchestrator must still require a valid candidate and an independent Edge
audit.  This module answers only: "are these the locally anchored artifacts?"
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Callable, Protocol


SCHEMA_VERSION = "0.1"
RECORD_TYPE = "cross_document_semantic_graph_trust_manifest"
ACTIVATION_POLICY_VERSION = "semantic-graph-answer-promotion-v0.1"
STORAGE_DIR = "05-semantic-answer-index"
STORAGE_DATABASE = "safe-answer-index.sqlite3"
STORAGE_STATE = "semantic-answer-index-state.json"
BASE_DATABASE = "safe-answer-index.sqlite3"
MANIFEST_NAME = "semantic-answer-trust-manifest.json"
KEYCHAIN_SERVICE = "jp.rivage.local-memory-search.semantic-graph-root.v1"
KEYCHAIN_LABEL = "Local Memory Search Semantic Graph Root"
MAX_MANIFEST_BYTES = 64 * 1024

GENERATION_PATTERN = re.compile(r"generation-[0-9a-f]{32}")
BUILD_ID_PATTERN = re.compile(r"[0-9a-f]{32}")
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")

REGISTRATION_FIELDS = frozenset({
    "schema_version",
    "status",
    "generation",
    "database_path",
    "database_sha256",
    "state_path",
    "state_sha256",
    "base_index_path",
    "base_index_sha256",
    "graph_snapshot_id",
    "logical_snapshot_sha256",
    "counts",
    "retrieval_enabled",
    "used_for_answers",
})
MANIFEST_FIELDS = frozenset({
    "schema_version",
    "record_type",
    "generation",
    "build_id",
    "activation_policy_version",
    "storage_registration_sha256",
    "artifacts",
    "graph",
    "question_independent",
    "external_network_used",
})
ARTIFACT_FIELDS = frozenset({"relative_path", "sha256"})
ARTIFACT_NAMES = frozenset({"database", "state", "base_index"})
GRAPH_FIELDS = frozenset({
    "graph_snapshot_id",
    "logical_snapshot_sha256",
    "projection_sha256",
    "counts",
})
COUNT_FIELDS = frozenset({"nodes", "edges", "edge_evidence"})
TRUST_REGISTRATION_FIELDS = frozenset({
    "schema_version",
    "status",
    "generation",
    "build_id",
    "manifest_path",
    "manifest_sha256",
    "keychain_service",
    "keychain_account",
    "activation_policy_version",
    "storage_registration_sha256",
    "graph_snapshot_id",
    "logical_snapshot_sha256",
    "projection_sha256",
})


class SemanticGraphTrustError(ValueError):
    """A fail-closed trust decision with a stable machine reason."""

    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


class TrustStore(Protocol):
    """Minimal create-only/read-only trust-store contract."""

    def create_root(self, generation: str, root_sha256: str) -> None:
        """Create one generation root; an existing item must not be updated."""

    def read_root(self, generation: str) -> str:
        """Return the exact lowercase SHA-256 stored for one generation."""


Runner = Callable[..., subprocess.CompletedProcess[str]]


class KeychainTrustStore:
    """macOS Keychain backend with no update or delete capability.

    The digest is not a secret.  Keychain is used as a storage boundary from
    the generated artifact tree, not as a claim that an unsigned application
    can resist same-user malware or a local administrator.
    """

    def __init__(
        self,
        *,
        service: str = KEYCHAIN_SERVICE,
        timeout_seconds: float = 5.0,
        runner: Runner = subprocess.run,
    ) -> None:
        if service != KEYCHAIN_SERVICE:
            raise SemanticGraphTrustError("trust_store_service_invalid")
        if (
            type(timeout_seconds) not in {int, float}
            or not 0.1 <= float(timeout_seconds) <= 30.0
        ):
            raise SemanticGraphTrustError("trust_store_timeout_invalid")
        self.service = service
        self.timeout_seconds = float(timeout_seconds)
        self._runner = runner

    @staticmethod
    def _validate_input(generation: str, root_sha256: str | None = None) -> None:
        if (
            not isinstance(generation, str)
            or GENERATION_PATTERN.fullmatch(generation) is None
        ):
            raise SemanticGraphTrustError("trust_generation_invalid")
        if root_sha256 is not None and (
            not isinstance(root_sha256, str)
            or SHA256_PATTERN.fullmatch(root_sha256) is None
        ):
            raise SemanticGraphTrustError("trust_root_sha256_invalid")

    def _execute(self, command: list[str], failure: str) -> subprocess.CompletedProcess[str]:
        if sys.platform != "darwin":
            raise SemanticGraphTrustError("trust_store_platform_unsupported")
        try:
            return self._runner(
                command,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
                check=True,
                close_fds=True,
                stdin=subprocess.DEVNULL,
            )
        except subprocess.TimeoutExpired as exc:
            raise SemanticGraphTrustError("trust_store_timeout") from exc
        except (OSError, subprocess.CalledProcessError) as exc:
            raise SemanticGraphTrustError(failure) from exc

    def create_root(self, generation: str, root_sha256: str) -> None:
        self._validate_input(generation, root_sha256)
        # Deliberately omit -U and all delete operations.  A generation root is
        # immutable; collision or retry is a build failure, not a rotation.
        self._execute(
            [
                "/usr/bin/security",
                "add-generic-password",
                "-a",
                generation,
                "-s",
                self.service,
                "-l",
                KEYCHAIN_LABEL,
                "-w",
                root_sha256,
            ],
            "trust_store_create_failed",
        )

    def read_root(self, generation: str) -> str:
        self._validate_input(generation)
        completed = self._execute(
            [
                "/usr/bin/security",
                "find-generic-password",
                "-a",
                generation,
                "-s",
                self.service,
                "-w",
            ],
            "trust_store_read_failed",
        )
        output = completed.stdout
        if not isinstance(output, str):
            raise SemanticGraphTrustError("trust_store_output_invalid")
        value = output.rstrip("\n")
        if (
            SHA256_PATTERN.fullmatch(value) is None
            or output not in {value, value + "\n"}
        ):
            raise SemanticGraphTrustError("trust_store_output_invalid")
        return value


def canonical_json(value: object) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise SemanticGraphTrustError("trust_json_not_canonicalizable") from exc


def canonical_sha256(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def manifest_bytes(manifest: dict) -> bytes:
    return (canonical_json(manifest) + "\n").encode("utf-8")


def manifest_sha256(manifest: dict) -> str:
    return hashlib.sha256(manifest_bytes(manifest)).hexdigest()


def _require_root_sha256(value: object) -> str:
    if not isinstance(value, str) or SHA256_PATTERN.fullmatch(value) is None:
        raise SemanticGraphTrustError("trust_store_output_invalid")
    return value


def _strict_json(data: bytes, reason: str) -> dict:
    if not data or len(data) > MAX_MANIFEST_BYTES:
        raise SemanticGraphTrustError(reason)

    def pairs(values: list[tuple[str, object]]) -> dict:
        result: dict = {}
        for key, value in values:
            if key in result:
                raise SemanticGraphTrustError(reason)
            result[key] = value
        return result

    def reject_constant(_value: str) -> None:
        raise SemanticGraphTrustError(reason)

    try:
        value = json.loads(
            data.decode("utf-8"),
            object_pairs_hook=pairs,
            parse_constant=reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SemanticGraphTrustError(reason) from exc
    if not isinstance(value, dict):
        raise SemanticGraphTrustError(reason)
    return value


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        try:
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
                raise SemanticGraphTrustError("trust_artifact_file_invalid")
            with os.fdopen(descriptor, "rb", closefd=False) as handle:
                for block in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(block)
        finally:
            os.close(descriptor)
    except SemanticGraphTrustError:
        raise
    except OSError as exc:
        raise SemanticGraphTrustError("trust_artifact_file_invalid") from exc
    return digest.hexdigest()


def _read_regular_bytes(path: Path, maximum: int, reason: str) -> bytes:
    """Read one bounded regular file from the descriptor opened with NOFOLLOW."""
    try:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        try:
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
                raise SemanticGraphTrustError(reason)
            with os.fdopen(descriptor, "rb", closefd=False) as handle:
                data = handle.read(maximum + 1)
        finally:
            os.close(descriptor)
    except SemanticGraphTrustError:
        raise
    except OSError as exc:
        raise SemanticGraphTrustError(reason) from exc
    if not data or len(data) > maximum:
        raise SemanticGraphTrustError(reason)
    return data


def _validate_generation_dir(generation_dir: Path) -> Path:
    generation_dir = Path(generation_dir)
    if (
        not generation_dir.is_absolute()
        or GENERATION_PATTERN.fullmatch(generation_dir.name) is None
        or generation_dir.is_symlink()
        or not generation_dir.is_dir()
    ):
        raise SemanticGraphTrustError("trust_generation_directory_invalid")
    return generation_dir


def _expected_paths(generation_dir: Path) -> dict[str, Path]:
    storage = generation_dir / STORAGE_DIR
    if storage.is_symlink() or not storage.is_dir():
        raise SemanticGraphTrustError("trust_storage_directory_invalid")
    return {
        "database": storage / STORAGE_DATABASE,
        "state": storage / STORAGE_STATE,
        "base_index": generation_dir / BASE_DATABASE,
    }


def _validate_counts(value: object, reason: str) -> dict:
    if (
        not isinstance(value, dict)
        or set(value) != COUNT_FIELDS
        or any(type(value.get(key)) is not int or value[key] < 1 for key in COUNT_FIELDS)
    ):
        raise SemanticGraphTrustError(reason)
    return value


def _validate_registration(registration: object, generation_dir: Path) -> dict:
    expected = _expected_paths(generation_dir)
    if not isinstance(registration, dict) or set(registration) != REGISTRATION_FIELDS:
        raise SemanticGraphTrustError("trust_registration_contract_invalid")
    if (
        registration.get("schema_version") != SCHEMA_VERSION
        or registration.get("status") != "validated_storage_only"
        or registration.get("generation") != generation_dir.name
        or registration.get("retrieval_enabled") is not False
        or registration.get("used_for_answers") is not False
    ):
        raise SemanticGraphTrustError("trust_registration_contract_invalid")
    path_fields = {
        "database_path": expected["database"],
        "state_path": expected["state"],
        "base_index_path": expected["base_index"],
    }
    if any(
        not isinstance(registration.get(key), str)
        or Path(registration[key]) != path
        for key, path in path_fields.items()
    ):
        raise SemanticGraphTrustError("trust_registration_path_invalid")
    for key in (
        "database_sha256",
        "state_sha256",
        "base_index_sha256",
        "logical_snapshot_sha256",
    ):
        if (
            not isinstance(registration.get(key), str)
            or SHA256_PATTERN.fullmatch(registration[key]) is None
        ):
            raise SemanticGraphTrustError("trust_registration_hash_invalid")
    logical = registration["logical_snapshot_sha256"]
    if registration.get("graph_snapshot_id") != "xkgs_" + logical[:32]:
        raise SemanticGraphTrustError("trust_registration_snapshot_invalid")
    _validate_counts(registration.get("counts"), "trust_registration_counts_invalid")
    return registration


def _validate_storage_state(
    state_value: object,
    registration: dict,
    generation: str,
    build_id: str,
) -> dict:
    if not isinstance(state_value, dict):
        raise SemanticGraphTrustError("trust_storage_state_invalid")
    output = state_value.get("output")
    base = state_value.get("base")
    shadow = state_value.get("shadow")
    projection = state_value.get("projection_sha256")
    if (
        state_value.get("schema_version") != SCHEMA_VERSION
        or state_value.get("record_type")
        != "cross_document_semantic_graph_answer_index_projection_state"
        or state_value.get("status") != "complete"
        or state_value.get("generation") != generation
        or state_value.get("question_independent") is not True
        or state_value.get("external_network_used") is not False
        or state_value.get("storage_only") is not True
        or state_value.get("retrieval_enabled") is not False
        or state_value.get("used_for_answers") is not False
        or state_value.get("answer_behavior_changed") is not False
        or not isinstance(output, dict)
        or output.get("sqlite_file") != STORAGE_DATABASE
        or output.get("state_file") != STORAGE_STATE
        or output.get("sqlite_sha256") != registration["database_sha256"]
        or not isinstance(base, dict)
        or base.get("sqlite_file") != BASE_DATABASE
        or base.get("sqlite_sha256") != registration["base_index_sha256"]
        or not isinstance(shadow, dict)
        or shadow.get("build_id") != build_id
        or shadow.get("graph_snapshot_id") != registration["graph_snapshot_id"]
        or shadow.get("logical_snapshot_sha256")
        != registration["logical_snapshot_sha256"]
        or not isinstance(projection, str)
        or SHA256_PATTERN.fullmatch(projection) is None
        or state_value.get("counts") != registration["counts"]
    ):
        raise SemanticGraphTrustError("trust_storage_state_invalid")
    return state_value


def build_trust_manifest(
    generation_dir: Path,
    build_id: str,
    registration: dict,
    storage_state: dict,
) -> dict:
    """Build the deterministic closed manifest without writing or anchoring it."""
    generation_dir = _validate_generation_dir(generation_dir)
    if (
        not isinstance(build_id, str)
        or BUILD_ID_PATTERN.fullmatch(build_id) is None
    ):
        raise SemanticGraphTrustError("trust_build_id_invalid")
    registration = _validate_registration(registration, generation_dir)
    state_value = _validate_storage_state(
        storage_state, registration, generation_dir.name, build_id
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "record_type": RECORD_TYPE,
        "generation": generation_dir.name,
        "build_id": build_id,
        "activation_policy_version": ACTIVATION_POLICY_VERSION,
        "storage_registration_sha256": canonical_sha256(registration),
        "artifacts": {
            "database": {
                "relative_path": f"{STORAGE_DIR}/{STORAGE_DATABASE}",
                "sha256": registration["database_sha256"],
            },
            "state": {
                "relative_path": f"{STORAGE_DIR}/{STORAGE_STATE}",
                "sha256": registration["state_sha256"],
            },
            "base_index": {
                "relative_path": BASE_DATABASE,
                "sha256": registration["base_index_sha256"],
            },
        },
        "graph": {
            "graph_snapshot_id": registration["graph_snapshot_id"],
            "logical_snapshot_sha256": registration[
                "logical_snapshot_sha256"
            ],
            "projection_sha256": state_value["projection_sha256"],
            "counts": dict(registration["counts"]),
        },
        "question_independent": True,
        "external_network_used": False,
    }


def validate_trust_manifest(
    manifest: object,
    generation_dir: Path,
    registration: dict,
    *,
    verify_artifacts: bool = True,
) -> dict:
    """Validate a manifest against registration and optionally current bytes."""
    generation_dir = _validate_generation_dir(generation_dir)
    registration = _validate_registration(registration, generation_dir)
    if not isinstance(manifest, dict) or set(manifest) != MANIFEST_FIELDS:
        raise SemanticGraphTrustError("trust_manifest_contract_invalid")
    if (
        manifest.get("schema_version") != SCHEMA_VERSION
        or manifest.get("record_type") != RECORD_TYPE
        or manifest.get("generation") != generation_dir.name
        or not isinstance(manifest.get("build_id"), str)
        or BUILD_ID_PATTERN.fullmatch(manifest["build_id"]) is None
        or manifest.get("activation_policy_version")
        != ACTIVATION_POLICY_VERSION
        or manifest.get("storage_registration_sha256")
        != canonical_sha256(registration)
        or manifest.get("question_independent") is not True
        or manifest.get("external_network_used") is not False
    ):
        raise SemanticGraphTrustError("trust_manifest_contract_invalid")
    artifacts = manifest.get("artifacts")
    expected_paths = _expected_paths(generation_dir)
    expected_artifacts = {
        "database": (
            f"{STORAGE_DIR}/{STORAGE_DATABASE}",
            registration["database_sha256"],
        ),
        "state": (
            f"{STORAGE_DIR}/{STORAGE_STATE}",
            registration["state_sha256"],
        ),
        "base_index": (BASE_DATABASE, registration["base_index_sha256"]),
    }
    if not isinstance(artifacts, dict) or set(artifacts) != ARTIFACT_NAMES:
        raise SemanticGraphTrustError("trust_manifest_artifacts_invalid")
    for name, (relative_path, digest) in expected_artifacts.items():
        record = artifacts.get(name)
        if (
            not isinstance(record, dict)
            or set(record) != ARTIFACT_FIELDS
            or record.get("relative_path") != relative_path
            or record.get("sha256") != digest
        ):
            raise SemanticGraphTrustError("trust_manifest_artifacts_invalid")
    graph = manifest.get("graph")
    if (
        not isinstance(graph, dict)
        or set(graph) != GRAPH_FIELDS
        or graph.get("graph_snapshot_id") != registration["graph_snapshot_id"]
        or graph.get("logical_snapshot_sha256")
        != registration["logical_snapshot_sha256"]
        or not isinstance(graph.get("projection_sha256"), str)
        or SHA256_PATTERN.fullmatch(graph["projection_sha256"]) is None
        or graph.get("counts") != registration["counts"]
    ):
        raise SemanticGraphTrustError("trust_manifest_graph_invalid")
    if verify_artifacts:
        for name in ("database", "base_index"):
            path = expected_paths[name]
            if _sha256_file(path) != artifacts[name]["sha256"]:
                raise SemanticGraphTrustError("trust_artifact_hash_mismatch")
        state_data = _read_regular_bytes(
            expected_paths["state"],
            MAX_MANIFEST_BYTES,
            "trust_storage_state_invalid",
        )
        if (
            hashlib.sha256(state_data).hexdigest()
            != artifacts["state"]["sha256"]
        ):
            raise SemanticGraphTrustError("trust_artifact_hash_mismatch")
        state_value = _strict_json(state_data, "trust_storage_state_invalid")
        _validate_storage_state(
            state_value,
            registration,
            generation_dir.name,
            manifest["build_id"],
        )
        if state_value["projection_sha256"] != graph["projection_sha256"]:
            raise SemanticGraphTrustError("trust_manifest_graph_invalid")
    return manifest


def trust_manifest_path(generation_dir: Path) -> Path:
    generation_dir = _validate_generation_dir(generation_dir)
    storage = _expected_paths(generation_dir)["state"].parent
    return storage / MANIFEST_NAME


def write_trust_manifest(
    generation_dir: Path,
    manifest: dict,
    registration: dict,
) -> Path:
    """Validate and create the manifest atomically; never replace one."""
    validate_trust_manifest(manifest, generation_dir, registration)
    path = trust_manifest_path(generation_dir)
    payload = manifest_bytes(manifest)
    path.parent.chmod(0o700)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{MANIFEST_NAME}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path, follow_symlinks=False)
        except FileExistsError as exc:
            raise SemanticGraphTrustError("trust_manifest_already_exists") from exc
        temporary.unlink()
        directory_descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return path


def load_trust_manifest(generation_dir: Path) -> tuple[dict, str]:
    """Load exact canonical bytes from a private, single-link regular file."""
    path = trust_manifest_path(generation_dir)
    try:
        parent_metadata = os.stat(path.parent, follow_symlinks=False)
    except OSError as exc:
        raise SemanticGraphTrustError("trust_manifest_directory_invalid") from exc
    if (
        not stat.S_ISDIR(parent_metadata.st_mode)
        or stat.S_IMODE(parent_metadata.st_mode) != 0o700
    ):
        raise SemanticGraphTrustError("trust_manifest_directory_invalid")
    try:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        try:
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_nlink != 1
                or stat.S_IMODE(metadata.st_mode) != 0o600
            ):
                raise SemanticGraphTrustError("trust_manifest_file_invalid")
            with os.fdopen(descriptor, "rb", closefd=False) as handle:
                data = handle.read(MAX_MANIFEST_BYTES + 1)
        finally:
            os.close(descriptor)
    except SemanticGraphTrustError:
        raise
    except OSError as exc:
        raise SemanticGraphTrustError("trust_manifest_file_invalid") from exc
    manifest = _strict_json(data, "trust_manifest_json_invalid")
    if data != manifest_bytes(manifest):
        raise SemanticGraphTrustError("trust_manifest_not_canonical")
    return manifest, hashlib.sha256(data).hexdigest()


def publish_trust_root(
    generation_dir: Path,
    build_id: str,
    registration: dict,
    storage_state: dict,
    trust_store: TrustStore,
) -> dict:
    """Validate artifacts, create manifest, then create and verify Keychain root."""
    manifest = build_trust_manifest(
        generation_dir, build_id, registration, storage_state
    )
    validate_trust_manifest(manifest, generation_dir, registration)
    path = write_trust_manifest(generation_dir, manifest, registration)
    loaded, digest = load_trust_manifest(generation_dir)
    validate_trust_manifest(loaded, generation_dir, registration)
    trust_store.create_root(loaded["generation"], digest)
    observed = _require_root_sha256(
        trust_store.read_root(loaded["generation"])
    )
    if not hmac.compare_digest(digest, observed):
        raise SemanticGraphTrustError("trust_root_mismatch")
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "trusted",
        "generation": loaded["generation"],
        "build_id": loaded["build_id"],
        "manifest_path": str(path),
        "manifest_sha256": digest,
        "keychain_service": KEYCHAIN_SERVICE,
        "keychain_account": loaded["generation"],
        "activation_policy_version": ACTIVATION_POLICY_VERSION,
        "storage_registration_sha256": loaded[
            "storage_registration_sha256"
        ],
        "graph_snapshot_id": loaded["graph"]["graph_snapshot_id"],
        "logical_snapshot_sha256": loaded["graph"][
            "logical_snapshot_sha256"
        ],
        "projection_sha256": loaded["graph"]["projection_sha256"],
    }


def validate_trust_root(
    generation_dir: Path,
    registration: dict,
    trust_store: TrustStore,
) -> dict:
    """Re-read the independent root and all bound artifacts for one query."""
    manifest, digest = load_trust_manifest(generation_dir)
    observed = _require_root_sha256(
        trust_store.read_root(manifest.get("generation"))
    )
    if not hmac.compare_digest(digest, observed):
        raise SemanticGraphTrustError("trust_root_mismatch")
    validate_trust_manifest(manifest, generation_dir, registration)
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "trusted",
        "reason_code": None,
        "generation": manifest["generation"],
        "build_id": manifest["build_id"],
        "manifest_path": str(trust_manifest_path(generation_dir)),
        "manifest_sha256": digest,
        "keychain_service": KEYCHAIN_SERVICE,
        "keychain_account": manifest["generation"],
        "storage_registration_sha256": manifest[
            "storage_registration_sha256"
        ],
        "graph_snapshot_id": manifest["graph"]["graph_snapshot_id"],
        "logical_snapshot_sha256": manifest["graph"][
            "logical_snapshot_sha256"
        ],
        "projection_sha256": manifest["graph"]["projection_sha256"],
        "activation_policy_version": ACTIVATION_POLICY_VERSION,
        "allows_answer_activation": True,
    }


def validate_trust_registration(
    value: object,
    generation_dir: Path,
    storage_registration: dict,
    *,
    verified_root: dict | None = None,
) -> dict:
    """Validate the non-authoritative CONFIG locator against a verified root.

    Passing ``verified_root`` is required at the answer-selection boundary.
    Omitting it only validates the persisted closed schema during bootstrap or
    recovery; that structural check does not grant answer authority.
    """
    generation_dir = _validate_generation_dir(generation_dir)
    storage_registration = _validate_registration(
        storage_registration, generation_dir
    )
    if not isinstance(value, dict) or set(value) != TRUST_REGISTRATION_FIELDS:
        raise SemanticGraphTrustError("trust_registration_locator_invalid")
    if (
        value.get("schema_version") != SCHEMA_VERSION
        or value.get("status") != "trusted"
        or value.get("generation") != generation_dir.name
        or not isinstance(value.get("build_id"), str)
        or BUILD_ID_PATTERN.fullmatch(value["build_id"]) is None
        or value.get("manifest_path")
        != str(trust_manifest_path(generation_dir))
        or value.get("keychain_service") != KEYCHAIN_SERVICE
        or value.get("keychain_account") != generation_dir.name
        or value.get("activation_policy_version")
        != ACTIVATION_POLICY_VERSION
        or value.get("storage_registration_sha256")
        != canonical_sha256(storage_registration)
        or value.get("graph_snapshot_id")
        != storage_registration["graph_snapshot_id"]
        or value.get("logical_snapshot_sha256")
        != storage_registration["logical_snapshot_sha256"]
        or any(
            not isinstance(value.get(key), str)
            or SHA256_PATTERN.fullmatch(value[key]) is None
            for key in ("manifest_sha256", "projection_sha256")
        )
    ):
        raise SemanticGraphTrustError("trust_registration_locator_invalid")
    if verified_root is not None:
        bindings = (
            "generation",
            "build_id",
            "manifest_path",
            "manifest_sha256",
            "keychain_service",
            "keychain_account",
            "activation_policy_version",
            "storage_registration_sha256",
            "graph_snapshot_id",
            "logical_snapshot_sha256",
            "projection_sha256",
        )
        if (
            not isinstance(verified_root, dict)
            or verified_root.get("status") != "trusted"
            or verified_root.get("reason_code") is not None
            or verified_root.get("allows_answer_activation") is not True
            or any(
                verified_root.get(key) != value[key] for key in bindings
            )
        ):
            raise SemanticGraphTrustError("trust_verified_root_binding_invalid")
    return value


def inspect_trust_root(
    generation_dir: Path,
    registration: dict,
    trust_store: TrustStore,
) -> dict:
    """Fail-closed wrapper suitable for the answer orchestrator."""
    try:
        return validate_trust_root(generation_dir, registration, trust_store)
    except Exception as exc:
        reason = (
            exc.reason_code
            if isinstance(exc, SemanticGraphTrustError)
            else "trust_root_unexpected_failure"
        )
        return {
            "schema_version": SCHEMA_VERSION,
            "status": "rejected",
            "reason_code": reason,
            "generation": (
                generation_dir.name
                if isinstance(generation_dir, Path)
                else None
            ),
            "build_id": None,
            "manifest_path": None,
            "manifest_sha256": None,
            "keychain_service": KEYCHAIN_SERVICE,
            "keychain_account": None,
            "storage_registration_sha256": None,
            "graph_snapshot_id": None,
            "logical_snapshot_sha256": None,
            "projection_sha256": None,
            "activation_policy_version": ACTIVATION_POLICY_VERSION,
            "allows_answer_activation": False,
        }


def recover_trust_manifest(
    generation_dir: Path,
    build_id: str,
    registration: dict,
    storage_state: dict,
    trust_store: TrustStore,
) -> dict:
    """Restore a missing manifest only when an existing root proves its bytes.

    This function never creates or updates a Keychain item.  A missing root is
    not recoverable from the mutually writable artifact tree.
    """
    expected = build_trust_manifest(
        generation_dir, build_id, registration, storage_state
    )
    digest = manifest_sha256(expected)
    observed = _require_root_sha256(
        trust_store.read_root(expected["generation"])
    )
    if not hmac.compare_digest(digest, observed):
        raise SemanticGraphTrustError("trust_root_mismatch")
    path = trust_manifest_path(generation_dir)
    if path.exists() or path.is_symlink():
        loaded, loaded_digest = load_trust_manifest(generation_dir)
        if loaded != expected or not hmac.compare_digest(digest, loaded_digest):
            raise SemanticGraphTrustError("trust_manifest_recovery_mismatch")
    else:
        validate_trust_manifest(expected, generation_dir, registration)
        write_trust_manifest(generation_dir, expected, registration)
    return validate_trust_root(generation_dir, registration, trust_store)


__all__ = [
    "ACTIVATION_POLICY_VERSION",
    "KEYCHAIN_SERVICE",
    "KeychainTrustStore",
    "MANIFEST_NAME",
    "SemanticGraphTrustError",
    "TRUST_REGISTRATION_FIELDS",
    "TrustStore",
    "build_trust_manifest",
    "canonical_json",
    "canonical_sha256",
    "inspect_trust_root",
    "load_trust_manifest",
    "manifest_bytes",
    "manifest_sha256",
    "publish_trust_root",
    "recover_trust_manifest",
    "trust_manifest_path",
    "validate_trust_manifest",
    "validate_trust_registration",
    "validate_trust_root",
]
