#!/usr/bin/env python3
"""Pure Step 5 gate for promoting one independently audited graph answer.

The gate performs no I/O and never runs a model.  Callers inject the existing
candidate, independent-audit, and trust-root validators.  Every non-PROMOTE
path returns a deep copy of the already audited legacy answer.
"""

from __future__ import annotations

import copy
import hashlib
import json
import unicodedata
from datetime import date
from typing import Any, Callable, Optional


SCHEMA_VERSION = "0.1"
RECORD_TYPE = "cross_document_semantic_graph_answer_promotion"
PROMOTER = "local-memory-semantic-graph-answer-promotion"
PROMOTER_VERSION = "0.1.0"
KEYCHAIN_SERVICE = "jp.rivage.local-memory-search.semantic-graph-root.v1"
ACTIVATION_POLICY_VERSION = "semantic-graph-answer-promotion-v0.1"
TRUST_CONFIG_KEY = "cross_document_semantic_graph_trust"

SUPPORTED_OPERATIONS = frozenset({
    "owner",
    "assignment_change",
    "version_change",
})

AUDIT_CHECK_FIELDS = frozenset({
    "candidate_contract",
    "question_classification",
    "registered_storage_integrity",
    "independent_graph_reconstruction",
    "candidate_semantics",
})

PROMOTION_FIELDS = frozenset({
    "schema_version",
    "record_type",
    "promoter",
    "promoter_version",
    "status",
    "decision",
    "reason_code",
    "diagnostic_code",
    "source_answer",
    "operation",
    "question_sha256",
    "question_reference_date",
    "candidate_sha256",
    "edge_audit_sha256",
    "registration_sha256",
    "graph_snapshot_id",
    "trust_binding",
    "initial_config_sha256",
    "latest_config_sha256",
    "final_config_sha256",
    "legacy_answer_sha256",
    "selected_answer_sha256",
    "projected_answer",
    "evidence_ids",
    "source_references",
    "checks",
    "used_for_answers",
})

PROMOTION_CHECK_FIELDS = (
    "feature_enabled",
    "activation_lease",
    "latest_config_binding",
    "candidate_accepted",
    "audit_passed",
    "candidate_audit_binding",
    "registered_storage_integrity",
    "reference_date_binding",
    "trust_root_binding",
    "final_config_binding",
    "answer_projection",
    "evidence_projection",
)

ANSWER_FIELDS = frozenset({
    "answer_status",
    "answer_mode",
    "answer",
    "evidence_ids",
    "basis_summary",
    "uncertainties",
    "non_answer_reason",
    "diagnostic_evidence_ids",
    "needed_information",
    "follow_up_question",
    "reconsideration_condition",
    "verification_reminder",
})

SOURCE_REFERENCE_FIELDS = frozenset({
    "edge_id",
    "evidence_id",
    "document_id",
    "path",
    "source_sha256",
    "locator",
    "observed_text_sha256",
    "quote",
})

TRUST_BINDING_FIELDS = frozenset({
    "generation",
    "build_id",
    "manifest_sha256",
    "keychain_service",
    "keychain_account",
    "activation_policy_version",
    "storage_registration_sha256",
    "graph_snapshot_id",
    "logical_snapshot_sha256",
    "projection_sha256",
})
MAX_ANSWER_EVIDENCE_IDS = 10
MAX_EVIDENCE_COVER_SEARCH_STATES = 100_000

CandidateValidator = Callable[[object, dict[str, Any], str, Optional[str]], bool]
AuditValidator = Callable[
    [object, dict[str, Any], dict[str, Any], str, Optional[str]], bool
]
TrustValidator = Callable[
    [dict[str, Any], dict[str, Any], dict[str, Any]], object
]
LatestConfigValidator = Callable[
    [object, object, dict[str, Any]], bool
]
FinalConfigLoader = Callable[[], object]
AnswerValidator = Callable[[dict[str, Any], set[str], str, bool], object]


def canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def canonical_sha256(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def question_sha256(question: str) -> str:
    normalized = unicodedata.normalize("NFC", question).strip()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def deterministic_candidate_semantics(candidate: dict[str, Any]) -> dict[str, Any]:
    """Match the deterministic projection used by the Step 4 auditor."""
    trace = candidate.get("trace")
    if not isinstance(trace, dict):
        raise ValueError("candidate_trace_invalid")
    projected = copy.deepcopy(candidate)
    projected["trace"] = {
        key: copy.deepcopy(value)
        for key, value in trace.items()
        if key not in {"elapsed_ms", "peak_rss_bytes"}
    }
    return projected


def _safe_sha256(value: object) -> str | None:
    try:
        return canonical_sha256(value)
    except (TypeError, ValueError):
        return None


def _exception_diagnostic(exc: Exception) -> str:
    reason = getattr(exc, "reason_code", None)
    if isinstance(reason, str) and reason.strip():
        return reason
    return type(exc).__name__


def _strict_reference_date(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("reference_date_invalid")
    parsed = date.fromisoformat(value)
    if parsed.isoformat() != value:
        raise ValueError("reference_date_invalid")
    return value


def _checks() -> dict[str, str]:
    return {key: "NOT_APPLICABLE" for key in PROMOTION_CHECK_FIELDS}


def _record(
    *,
    promoted: bool,
    reason_code: str | None,
    diagnostic_code: str | None,
    question_hash: str,
    reference_date: str | None,
    legacy_answer: dict[str, Any],
    candidate: object,
    audit: object,
    registration: object,
    operation: str | None,
    graph_snapshot_id: str | None,
    projected_answer: dict[str, Any],
    evidence_ids: list[str],
    source_references: list[dict[str, Any]],
    checks: dict[str, str],
    trust_binding: object = None,
    initial_config: object = None,
    latest_config: object = None,
    final_config: object = None,
) -> dict[str, Any]:
    selected = projected_answer if promoted else legacy_answer
    record = {
        "schema_version": SCHEMA_VERSION,
        "record_type": RECORD_TYPE,
        "promoter": PROMOTER,
        "promoter_version": PROMOTER_VERSION,
        "status": "promoted" if promoted else "fallback",
        "decision": "PROMOTE" if promoted else "FALLBACK",
        "reason_code": reason_code,
        "diagnostic_code": diagnostic_code,
        "source_answer": "semantic_graph" if promoted else "legacy",
        "operation": operation if operation in SUPPORTED_OPERATIONS else None,
        "question_sha256": question_hash,
        "question_reference_date": reference_date,
        "candidate_sha256": _safe_sha256(candidate),
        "edge_audit_sha256": _safe_sha256(audit),
        "registration_sha256": _safe_sha256(registration),
        "graph_snapshot_id": graph_snapshot_id,
        "trust_binding": (
            copy.deepcopy(trust_binding) if promoted else {}
        ),
        "initial_config_sha256": (
            _safe_sha256(initial_config) if promoted else None
        ),
        "latest_config_sha256": (
            _safe_sha256(latest_config) if promoted else None
        ),
        "final_config_sha256": (
            _safe_sha256(final_config) if promoted else None
        ),
        "legacy_answer_sha256": _safe_sha256(legacy_answer),
        "selected_answer_sha256": _safe_sha256(selected),
        "projected_answer": copy.deepcopy(projected_answer) if promoted else {},
        "evidence_ids": list(evidence_ids) if promoted else [],
        "source_references": (
            copy.deepcopy(source_references) if promoted else []
        ),
        "checks": dict(checks),
        "used_for_answers": promoted,
    }
    if set(record) != PROMOTION_FIELDS:
        raise AssertionError("promotion_record_fields_invalid")
    return record


def _fallback(
    *,
    legacy_answer: dict[str, Any],
    question_hash: str,
    reference_date: str | None,
    candidate: object,
    audit: object,
    registration: object,
    checks: dict[str, str],
    reason_code: str,
    diagnostic_code: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    selected = copy.deepcopy(legacy_answer)
    operation = candidate.get("operation") if isinstance(candidate, dict) else None
    graph_snapshot_id = (
        registration.get("graph_snapshot_id")
        if isinstance(registration, dict)
        and isinstance(registration.get("graph_snapshot_id"), str)
        else None
    )
    return selected, _record(
        promoted=False,
        reason_code=reason_code,
        diagnostic_code=diagnostic_code,
        question_hash=question_hash,
        reference_date=reference_date,
        legacy_answer=legacy_answer,
        candidate=candidate,
        audit=audit,
        registration=registration,
        operation=operation,
        graph_snapshot_id=graph_snapshot_id,
        projected_answer={},
        evidence_ids=[],
        source_references=[],
        checks=checks,
    )


def _validate_promoted_answer(answer: dict[str, Any], evidence_ids: list[str]) -> None:
    if set(answer) != ANSWER_FIELDS:
        raise ValueError("promoted_answer_fields_invalid")
    if (
        answer.get("answer_status") != "answered"
        or answer.get("answer_mode") != "grounded"
        or not isinstance(answer.get("answer"), str)
        or not answer["answer"].strip()
        or answer.get("evidence_ids") != evidence_ids
        or not 1 <= len(evidence_ids) <= 10
        or len(evidence_ids) != len(set(evidence_ids))
        or answer.get("uncertainties") != []
        or answer.get("non_answer_reason")
        != {"code": "none", "explanation": ""}
        or answer.get("diagnostic_evidence_ids") != []
        or answer.get("needed_information") != []
        or answer.get("follow_up_question") != ""
        or answer.get("reconsideration_condition") != ""
        or answer.get("verification_reminder") != ""
        or not isinstance(answer.get("basis_summary"), str)
        or not answer["basis_summary"].strip()
    ):
        raise ValueError("promoted_answer_contract_invalid")


def _validate_trust_binding(
    value: object,
    registration: dict[str, Any],
    candidate: dict[str, Any],
    audit: dict[str, Any],
    latest_config: object,
) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != TRUST_BINDING_FIELDS:
        raise ValueError("trust_receipt_fields_invalid")
    locator = (
        latest_config.get(TRUST_CONFIG_KEY)
        if isinstance(latest_config, dict)
        else None
    )
    if not isinstance(locator, dict) or any(
        locator.get(key) != value[key] for key in TRUST_BINDING_FIELDS
    ):
        raise ValueError("trust_receipt_config_binding_invalid")
    generation = value.get("generation")
    build_id = value.get("build_id")
    sha_fields = (
        "manifest_sha256",
        "storage_registration_sha256",
        "logical_snapshot_sha256",
        "projection_sha256",
    )
    if (
        not isinstance(generation, str)
        or not generation.startswith("generation-")
        or len(generation) != 43
        or any(character not in "0123456789abcdef" for character in generation[11:])
        or not isinstance(build_id, str)
        or len(build_id) != 32
        or any(character not in "0123456789abcdef" for character in build_id)
        or any(
            not isinstance(value.get(key), str)
            or len(value[key]) != 64
            or any(
                character not in "0123456789abcdef"
                for character in value[key]
            )
            for key in sha_fields
        )
        or value.get("keychain_service") != KEYCHAIN_SERVICE
        or value.get("keychain_account") != generation
        or value.get("activation_policy_version")
        != ACTIVATION_POLICY_VERSION
        or value.get("storage_registration_sha256")
        != canonical_sha256(registration)
        or value.get("graph_snapshot_id")
        != registration.get("graph_snapshot_id")
        or value.get("logical_snapshot_sha256")
        != registration.get("logical_snapshot_sha256")
    ):
        raise ValueError("trust_receipt_binding_invalid")
    candidate_attestation = candidate.get("runtime_attestation")
    audit_attestation = audit.get("audit_attestation")
    if not isinstance(candidate_attestation, dict) or not isinstance(
        audit_attestation, dict
    ):
        raise ValueError("trust_receipt_attestation_invalid")
    expected = {
        "generation": generation,
        "graph_snapshot_id": value["graph_snapshot_id"],
        "logical_snapshot_sha256": value["logical_snapshot_sha256"],
        "projection_sha256": value["projection_sha256"],
    }
    if (
        candidate_attestation.get("build_id") != build_id
        or any(
            candidate_attestation.get(key) != expected_value
            or audit_attestation.get(key) != expected_value
            for key, expected_value in expected.items()
        )
    ):
        raise ValueError("trust_receipt_attestation_invalid")
    return copy.deepcopy(value)


def _bounded_evidence_cover(
    used_edge_ids: list[str],
    source_references: list[dict[str, Any]],
) -> list[str]:
    """Choose at most ten Evidence IDs that cover every used Edge.

    A deterministic greedy pass handles the normal case.  If it exceeds the
    answer schema budget, a bounded exact search prevents a poor per-Edge
    choice from rejecting a cover that actually fits.  Search exhaustion is a
    fail-closed non-promotion, never permission to omit an Edge.
    """
    used_edges = frozenset(used_edge_ids)
    coverage: dict[str, set[str]] = {}
    for reference in source_references:
        coverage.setdefault(reference["evidence_id"], set()).add(
            reference["edge_id"]
        )
    by_edge = {
        edge_id: tuple(sorted(
            evidence_id
            for evidence_id, covered in coverage.items()
            if edge_id in covered
        ))
        for edge_id in used_edge_ids
    }

    uncovered = set(used_edges)
    greedy: list[str] = []
    while uncovered and len(greedy) < MAX_ANSWER_EVIDENCE_IDS:
        evidence_id = min(
            coverage,
            key=lambda candidate: (
                -len(coverage[candidate] & uncovered),
                candidate,
            ),
        )
        newly_covered = coverage[evidence_id] & uncovered
        if not newly_covered:
            break
        greedy.append(evidence_id)
        uncovered -= newly_covered
    if not uncovered:
        return greedy

    search_states = 0
    failed: set[tuple[frozenset[str], int]] = set()

    def search(
        remaining_edges: frozenset[str],
        remaining_slots: int,
        chosen: tuple[str, ...],
    ) -> tuple[str, ...] | None:
        nonlocal search_states
        search_states += 1
        if search_states > MAX_EVIDENCE_COVER_SEARCH_STATES:
            raise ValueError("promotion_evidence_cover_search_exhausted")
        if not remaining_edges:
            return tuple(sorted(chosen))
        if remaining_slots == 0:
            return None
        max_new_coverage = max(
            len(edges & remaining_edges) for edges in coverage.values()
        )
        if max_new_coverage == 0 or (
            len(remaining_edges) + max_new_coverage - 1
        ) // max_new_coverage > remaining_slots:
            return None
        memo_key = (remaining_edges, remaining_slots)
        if memo_key in failed:
            return None
        constrained_edge = min(
            remaining_edges,
            key=lambda edge_id: (
                len([
                    evidence_id
                    for evidence_id in by_edge[edge_id]
                    if evidence_id not in chosen
                ]),
                used_edge_ids.index(edge_id),
            ),
        )
        candidates = sorted(
            (
                evidence_id
                for evidence_id in by_edge[constrained_edge]
                if evidence_id not in chosen
            ),
            key=lambda evidence_id: (
                -len(coverage[evidence_id] & remaining_edges),
                evidence_id,
            ),
        )
        for evidence_id in candidates:
            result = search(
                frozenset(remaining_edges - coverage[evidence_id]),
                remaining_slots - 1,
                chosen + (evidence_id,),
            )
            if result is not None:
                return result
        failed.add(memo_key)
        return None

    result = search(used_edges, MAX_ANSWER_EVIDENCE_IDS, ())
    if result is None:
        raise ValueError("promotion_evidence_budget_exceeded")
    return list(result)


def _validate_promotion_bindings(
    question: str,
    reference_date: str | None,
    candidate: dict[str, Any],
    audit: dict[str, Any],
    registration: dict[str, Any],
) -> tuple[list[str], list[dict[str, Any]]]:
    operation = candidate.get("operation")
    trace = candidate.get("trace")
    runtime_attestation = candidate.get("runtime_attestation")
    audit_attestation = audit.get("audit_attestation")
    checks = audit.get("checks")
    registration_snapshot = registration.get("graph_snapshot_id")
    candidate_hash = canonical_sha256(candidate)
    registration_hash = canonical_sha256(registration)
    semantics_hash = canonical_sha256(
        deterministic_candidate_semantics(candidate)
    )
    if (
        operation not in SUPPORTED_OPERATIONS
        or candidate.get("status") != "accepted"
        or candidate.get("decision") != "ACCEPTED"
        or candidate.get("reason_code") is not None
        or candidate.get("diagnostic_code") is not None
        or candidate.get("used_for_answers") is not False
        or not isinstance(candidate.get("answer_text"), str)
        or not candidate["answer_text"].strip()
        or not isinstance(candidate.get("asserted_facts"), list)
        or not candidate["asserted_facts"]
        or not isinstance(trace, dict)
        or trace.get("question_reference_date") != reference_date
        or trace.get("graph_snapshot_id") != registration_snapshot
        or trace.get("question_hash") != question_sha256(question)
        or trace.get("database_opened") is not True
        or trace.get("outbound_network_attempt_count") != 0
        or not isinstance(trace.get("used_semantic_edge_ids"), list)
        or not trace["used_semantic_edge_ids"]
        or trace.get("used_edge_statuses") != ["verified"]
        or not isinstance(runtime_attestation, dict)
        or runtime_attestation.get("read_only") is not True
        or runtime_attestation.get("graph_snapshot_id")
        != registration_snapshot
        or runtime_attestation.get("outbound_network_attempt_count") != 0
    ):
        raise ValueError("candidate_activation_binding_invalid")
    if (
        audit.get("status") != "passed"
        or audit.get("verdict") != "PASS"
        or audit.get("reason_code") is not None
        or audit.get("diagnostic_code") is not None
        or audit.get("operation") != operation
        or audit.get("candidate_sha256") != candidate_hash
        or audit.get("registration_sha256") != registration_hash
        or audit.get("question_sha256") != question_sha256(question)
        or audit.get("question_reference_date") != reference_date
        or audit.get("graph_snapshot_id") != registration_snapshot
        or audit.get("reconstructed_semantics_sha256") != semantics_hash
        or audit.get("used_for_answers") is not False
        or audit.get("allows_answer_activation") is not False
        or not isinstance(checks, dict)
        or set(checks) != AUDIT_CHECK_FIELDS
        or any(value != "PASS" for value in checks.values())
        or not isinstance(audit_attestation, dict)
        or audit_attestation.get("read_only") is not True
        or audit_attestation.get("database_opened") is not True
        or audit_attestation.get("graph_snapshot_id")
        != registration_snapshot
        or audit_attestation.get("outbound_network_attempt_count") != 0
    ):
        raise ValueError("independent_audit_activation_binding_invalid")
    references = trace.get("resolved_source_references")
    if not isinstance(references, list) or not references:
        raise ValueError("promotion_evidence_missing")
    source_references: list[dict[str, Any]] = []
    used_edge_ids = trace["used_semantic_edge_ids"]
    if (
        len(used_edge_ids) != len(set(used_edge_ids))
        or any(not isinstance(edge_id, str) or not edge_id for edge_id in used_edge_ids)
    ):
        raise ValueError("promotion_used_edges_invalid")
    used_edges = set(used_edge_ids)
    referenced_edges: set[str] = set()
    evidence_identity: dict[str, tuple[object, ...]] = {}
    for reference in references:
        if not isinstance(reference, dict) or set(reference) != SOURCE_REFERENCE_FIELDS:
            raise ValueError("promotion_source_reference_invalid")
        edge_id = reference.get("edge_id")
        evidence_id = reference.get("evidence_id")
        quote = reference.get("quote")
        if (
            not isinstance(edge_id, str)
            or edge_id not in used_edges
            or not isinstance(evidence_id, str)
            or not evidence_id.strip()
            or not isinstance(reference.get("document_id"), str)
            or not reference["document_id"].strip()
            or not isinstance(reference.get("path"), str)
            or not reference["path"].strip()
            or not isinstance(reference.get("source_sha256"), str)
            or len(reference["source_sha256"]) != 64
            or any(
                character not in "0123456789abcdef"
                for character in reference["source_sha256"]
            )
            or not isinstance(reference.get("locator"), dict)
            or not isinstance(quote, str)
            or not quote.strip()
            or hashlib.sha256(quote.encode("utf-8")).hexdigest()
            != reference.get("observed_text_sha256")
        ):
            raise ValueError("promotion_source_reference_invalid")
        identity = (
            reference["document_id"],
            reference["path"],
            reference["source_sha256"],
            canonical_json(reference["locator"]),
            reference["observed_text_sha256"],
            quote,
        )
        prior_identity = evidence_identity.setdefault(evidence_id, identity)
        if prior_identity != identity:
            raise ValueError("promotion_evidence_identity_conflict")
        referenced_edges.add(edge_id)
        source_references.append(copy.deepcopy(reference))
    if referenced_edges != used_edges:
        raise ValueError("promotion_edge_evidence_incomplete")
    # The answer contract caps citations at ten. Preserve every validated
    # source reference in the audit record and find a deterministic bounded
    # Evidence cover for the user-facing answer. No Edge is silently dropped.
    evidence_ids = _bounded_evidence_cover(used_edge_ids, source_references)
    if not 1 <= len(evidence_ids) <= MAX_ANSWER_EVIDENCE_IDS:
        raise ValueError("promotion_evidence_budget_exceeded")
    return evidence_ids, source_references


def promote_answer(
    *,
    legacy_answer: dict[str, Any],
    question: str,
    reference_date: str | None,
    candidate: object,
    audit: object,
    registration: object,
    feature_enabled: object,
    activation_available: object,
    initial_config: object,
    latest_config: object,
    candidate_validator: CandidateValidator,
    audit_validator: AuditValidator,
    trust_validator: TrustValidator,
    latest_config_validator: LatestConfigValidator,
    final_config_loader: FinalConfigLoader,
    answer_validator: AnswerValidator,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return ``(selected_answer, promotion_record)`` without mutating inputs."""
    legacy = copy.deepcopy(legacy_answer)
    frozen_candidate = copy.deepcopy(candidate)
    frozen_audit = copy.deepcopy(audit)
    frozen_registration = copy.deepcopy(registration)
    checks = _checks()
    question_hash = question_sha256(question) if isinstance(question, str) else ""
    try:
        reference = _strict_reference_date(reference_date)
    except (TypeError, ValueError):
        checks["reference_date_binding"] = "FAIL"
        return _fallback(
            legacy_answer=legacy,
            question_hash=question_hash,
            reference_date=None,
            candidate=frozen_candidate,
            audit=frozen_audit,
            registration=frozen_registration,
            checks=checks,
            reason_code="reference_date_binding_invalid",
        )
    if feature_enabled is not True:
        checks["feature_enabled"] = "FAIL"
        return _fallback(
            legacy_answer=legacy,
            question_hash=question_hash,
            reference_date=reference,
            candidate=frozen_candidate,
            audit=frozen_audit,
            registration=frozen_registration,
            checks=checks,
            reason_code="feature_disabled",
        )
    checks["feature_enabled"] = "PASS"
    if activation_available is not True:
        checks["activation_lease"] = "FAIL"
        return _fallback(
            legacy_answer=legacy,
            question_hash=question_hash,
            reference_date=reference,
            candidate=frozen_candidate,
            audit=frozen_audit,
            registration=frozen_registration,
            checks=checks,
            reason_code="promotion_activation_busy",
        )
    checks["activation_lease"] = "PASS"
    if not isinstance(question, str) or not question.strip():
        checks["reference_date_binding"] = "FAIL"
        return _fallback(
            legacy_answer=legacy,
            question_hash=question_hash,
            reference_date=reference,
            candidate=frozen_candidate,
            audit=frozen_audit,
            registration=frozen_registration,
            checks=checks,
            reason_code="question_binding_invalid",
        )
    if not isinstance(frozen_registration, dict):
        checks["registered_storage_integrity"] = "FAIL"
        return _fallback(
            legacy_answer=legacy,
            question_hash=question_hash,
            reference_date=reference,
            candidate=frozen_candidate,
            audit=frozen_audit,
            registration=frozen_registration,
            checks=checks,
            reason_code="registered_storage_invalid",
        )
    if not isinstance(frozen_candidate, dict):
        checks["candidate_accepted"] = "FAIL"
        return _fallback(
            legacy_answer=legacy,
            question_hash=question_hash,
            reference_date=reference,
            candidate=frozen_candidate,
            audit=frozen_audit,
            registration=frozen_registration,
            checks=checks,
            reason_code="candidate_absent",
        )
    try:
        candidate_valid = candidate_validator(
            copy.deepcopy(frozen_candidate),
            copy.deepcopy(frozen_registration),
            question,
            reference,
        ) is True
    except Exception as exc:
        candidate_valid = False
        candidate_diagnostic = type(exc).__name__
    else:
        candidate_diagnostic = None
    if not candidate_valid:
        checks["candidate_accepted"] = "FAIL"
        return _fallback(
            legacy_answer=legacy,
            question_hash=question_hash,
            reference_date=reference,
            candidate=frozen_candidate,
            audit=frozen_audit,
            registration=frozen_registration,
            checks=checks,
            reason_code="candidate_contract_invalid",
            diagnostic_code=candidate_diagnostic,
        )
    if (
        frozen_candidate.get("status") != "accepted"
        or frozen_candidate.get("decision") != "ACCEPTED"
        or frozen_candidate.get("operation") not in SUPPORTED_OPERATIONS
    ):
        checks["candidate_accepted"] = "FAIL"
        return _fallback(
            legacy_answer=legacy,
            question_hash=question_hash,
            reference_date=reference,
            candidate=frozen_candidate,
            audit=frozen_audit,
            registration=frozen_registration,
            checks=checks,
            reason_code="candidate_not_accepted",
        )
    checks["candidate_accepted"] = "PASS"
    if not isinstance(frozen_audit, dict):
        checks["audit_passed"] = "FAIL"
        return _fallback(
            legacy_answer=legacy,
            question_hash=question_hash,
            reference_date=reference,
            candidate=frozen_candidate,
            audit=frozen_audit,
            registration=frozen_registration,
            checks=checks,
            reason_code="independent_edge_audit_absent",
        )
    trace = frozen_candidate.get("trace")
    if (
        not isinstance(trace, dict)
        or trace.get("question_reference_date") != reference
        or frozen_audit.get("question_reference_date") != reference
    ):
        checks["reference_date_binding"] = "FAIL"
        return _fallback(
            legacy_answer=legacy,
            question_hash=question_hash,
            reference_date=reference,
            candidate=frozen_candidate,
            audit=frozen_audit,
            registration=frozen_registration,
            checks=checks,
            reason_code="reference_date_binding_invalid",
        )
    checks["reference_date_binding"] = "PASS"
    try:
        audit_valid = audit_validator(
            copy.deepcopy(frozen_audit),
            copy.deepcopy(frozen_candidate),
            copy.deepcopy(frozen_registration),
            question,
            reference,
        ) is True
    except Exception as exc:
        audit_valid = False
        audit_diagnostic = type(exc).__name__
    else:
        audit_diagnostic = None
    if not audit_valid:
        checks["audit_passed"] = "FAIL"
        return _fallback(
            legacy_answer=legacy,
            question_hash=question_hash,
            reference_date=reference,
            candidate=frozen_candidate,
            audit=frozen_audit,
            registration=frozen_registration,
            checks=checks,
            reason_code="independent_edge_audit_contract_invalid",
            diagnostic_code=audit_diagnostic,
        )
    if frozen_audit.get("status") != "passed" or frozen_audit.get("verdict") != "PASS":
        checks["audit_passed"] = "FAIL"
        return _fallback(
            legacy_answer=legacy,
            question_hash=question_hash,
            reference_date=reference,
            candidate=frozen_candidate,
            audit=frozen_audit,
            registration=frozen_registration,
            checks=checks,
            reason_code="independent_edge_audit_rejected",
        )
    checks["audit_passed"] = "PASS"
    try:
        evidence_ids, source_references = _validate_promotion_bindings(
            question,
            reference,
            frozen_candidate,
            frozen_audit,
            frozen_registration,
        )
    except (KeyError, TypeError, ValueError) as exc:
        checks["candidate_audit_binding"] = "FAIL"
        checks["evidence_projection"] = "FAIL"
        diagnostic = str(exc)
        return _fallback(
            legacy_answer=legacy,
            question_hash=question_hash,
            reference_date=reference,
            candidate=frozen_candidate,
            audit=frozen_audit,
            registration=frozen_registration,
            checks=checks,
            reason_code={
                "promotion_evidence_budget_exceeded": (
                    "answer_evidence_budget_exceeded"
                ),
                "promotion_evidence_cover_search_exhausted": (
                    "answer_evidence_cover_search_exhausted"
                ),
            }.get(diagnostic, "candidate_audit_binding_invalid"),
            diagnostic_code=diagnostic,
        )
    checks["candidate_audit_binding"] = "PASS"
    checks["evidence_projection"] = "PASS"
    try:
        latest_config_valid = latest_config_validator(
            copy.deepcopy(initial_config),
            copy.deepcopy(latest_config),
            copy.deepcopy(frozen_registration),
        ) is True
    except Exception as exc:
        latest_config_valid = False
        latest_config_diagnostic = type(exc).__name__
    else:
        latest_config_diagnostic = None
    if not latest_config_valid:
        checks["latest_config_binding"] = "FAIL"
        return _fallback(
            legacy_answer=legacy,
            question_hash=question_hash,
            reference_date=reference,
            candidate=frozen_candidate,
            audit=frozen_audit,
            registration=frozen_registration,
            checks=checks,
            reason_code="latest_config_binding_invalid",
            diagnostic_code=latest_config_diagnostic,
        )
    checks["latest_config_binding"] = "PASS"
    try:
        trust_result = trust_validator(
            copy.deepcopy(frozen_registration),
            copy.deepcopy(frozen_candidate),
            copy.deepcopy(frozen_audit),
        )
        if isinstance(trust_result, dict):
            trust_binding = _validate_trust_binding(
                trust_result,
                frozen_registration,
                frozen_candidate,
                frozen_audit,
                latest_config,
            )
            trust_valid = True
        else:
            trust_binding = {}
            trust_valid = False
    except Exception as exc:
        trust_valid = False
        trust_diagnostic = _exception_diagnostic(exc)
    else:
        trust_diagnostic = None
    if not trust_valid:
        checks["registered_storage_integrity"] = "FAIL"
        checks["trust_root_binding"] = "FAIL"
        return _fallback(
            legacy_answer=legacy,
            question_hash=question_hash,
            reference_date=reference,
            candidate=frozen_candidate,
            audit=frozen_audit,
            registration=frozen_registration,
            checks=checks,
            reason_code="trust_root_binding_invalid",
            diagnostic_code=trust_diagnostic,
        )
    checks["registered_storage_integrity"] = "PASS"
    checks["trust_root_binding"] = "PASS"
    try:
        final_config = final_config_loader()
        final_config_valid = (
            latest_config_validator(
                copy.deepcopy(initial_config),
                copy.deepcopy(final_config),
                copy.deepcopy(frozen_registration),
            )
            is True
            and canonical_sha256(final_config)
            == canonical_sha256(latest_config)
        )
    except Exception as exc:
        final_config_valid = False
        final_config_diagnostic = _exception_diagnostic(exc)
    else:
        final_config_diagnostic = None
    if not final_config_valid:
        checks["final_config_binding"] = "FAIL"
        return _fallback(
            legacy_answer=legacy,
            question_hash=question_hash,
            reference_date=reference,
            candidate=frozen_candidate,
            audit=frozen_audit,
            registration=frozen_registration,
            checks=checks,
            reason_code="final_config_binding_invalid",
            diagnostic_code=final_config_diagnostic,
        )
    checks["final_config_binding"] = "PASS"
    projected_answer = {
        "answer_status": "answered",
        "answer_mode": "grounded",
        "answer": frozen_candidate["answer_text"],
        "evidence_ids": evidence_ids,
        "basis_summary": (
            "保存済み意味グラフの必要Edgeと根拠を独立再構築し、"
            "候補回答と一致しました。"
        ),
        "uncertainties": [],
        "non_answer_reason": {"code": "none", "explanation": ""},
        "diagnostic_evidence_ids": [],
        "needed_information": [],
        "follow_up_question": "",
        "reconsideration_condition": "",
        "verification_reminder": "",
    }
    try:
        _validate_promoted_answer(projected_answer, evidence_ids)
        validation_result = answer_validator(
            copy.deepcopy(projected_answer),
            set(evidence_ids),
            "grounded",
            False,
        )
        if validation_result is False:
            raise ValueError("injected_answer_validator_rejected")
    except Exception as exc:
        checks["answer_projection"] = "FAIL"
        return _fallback(
            legacy_answer=legacy,
            question_hash=question_hash,
            reference_date=reference,
            candidate=frozen_candidate,
            audit=frozen_audit,
            registration=frozen_registration,
            checks=checks,
            reason_code="answer_projection_invalid",
            diagnostic_code=str(exc),
        )
    checks["answer_projection"] = "PASS"
    selected = copy.deepcopy(projected_answer)
    promotion = _record(
        promoted=True,
        reason_code=None,
        diagnostic_code=None,
        question_hash=question_hash,
        reference_date=reference,
        legacy_answer=legacy,
        candidate=frozen_candidate,
        audit=frozen_audit,
        registration=frozen_registration,
        operation=frozen_candidate["operation"],
        graph_snapshot_id=frozen_registration.get("graph_snapshot_id"),
        projected_answer=projected_answer,
        evidence_ids=evidence_ids,
        source_references=source_references,
        checks=checks,
        trust_binding=trust_binding,
        initial_config=initial_config,
        latest_config=latest_config,
        final_config=final_config,
    )
    return selected, promotion


__all__ = [
    "ANSWER_FIELDS",
    "AUDIT_CHECK_FIELDS",
    "PROMOTION_CHECK_FIELDS",
    "PROMOTION_FIELDS",
    "RECORD_TYPE",
    "SUPPORTED_OPERATIONS",
    "TRUST_BINDING_FIELDS",
    "canonical_sha256",
    "deterministic_candidate_semantics",
    "promote_answer",
    "question_sha256",
]
