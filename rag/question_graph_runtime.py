"""Runtime adapter from validated question-understanding records to RAG inputs.

The Phase-2 compiler deliberately separates its strict retrieval decision from
the candidate graph it has been able to validate structurally.  This module
keeps that distinction explicit:

* ``strict_status`` reports whether the compiler would permit retrieval;
* schema/semantic-valid candidate branches remain available as advisory
  retrieval queries in aggressive graph mode;
* a terminal compiler failure becomes one typed ``unknown`` branch instead of
  silently falling back to an unstructured question-only answer path.

Only the question and question-understanding runtime options enter this API.
It has no parameter through which source contents, gold answers, predictions,
or past answers can enter question understanding.
"""

from __future__ import annotations

import copy
import hashlib
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping


HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from build_question_understanding import (  # noqa: E402
    DEFAULT_MAX_BRANCHES,
    build_failed_understanding,
    build_question_understanding,
    derive_supported_intent_draft,
    validate_understanding_run,
)


GRAPH_PLAN_VERSION = "0.4"
UNKNOWN_BRANCH_STATUS = "runtime_fallback"


@dataclass(frozen=True)
class BranchRetrievalQuery:
    """One branch-local query derived from a typed candidate intent."""

    branch_id: str
    query_text: str
    coverage_requirement: str
    required_terms: tuple[str, ...]
    optional_terms: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "branch_id": self.branch_id,
            "query_text": self.query_text,
            "coverage_requirement": self.coverage_requirement,
            "required_terms": list(self.required_terms),
            "optional_terms": list(self.optional_terms),
        }


@dataclass(frozen=True)
class GraphPlan:
    """Validated graph projection consumed by retrieval and answer generation.

    ``strict_status`` is an audit result, not an aggressive-mode execution
    switch.  A caller using aggressive graph mode may consume advisory
    branches when this field is ``hold``; it must still retain the reasons in
    its run log and apply the compact answer contract after generation.
    """

    question_id: str | None
    original_question: str
    qur_id: str | None
    qic_id: str | None
    qur_final_status: str
    strict_status: str
    strict_reasons: tuple[str, ...]
    advisory_usable: bool
    fallback_used: bool
    retrieval_queries: tuple[BranchRetrievalQuery, ...]
    branch_intents: tuple[dict[str, Any], ...]
    compact_contract: dict[str, Any]
    qur_sha256: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "graph_plan_version": GRAPH_PLAN_VERSION,
            "question_id": self.question_id,
            "original_question": self.original_question,
            "qur_id": self.qur_id,
            "qic_id": self.qic_id,
            "qur_final_status": self.qur_final_status,
            "strict_status": self.strict_status,
            "strict_reasons": list(self.strict_reasons),
            "advisory_usable": self.advisory_usable,
            "fallback_used": self.fallback_used,
            "retrieval_queries": [item.as_dict() for item in self.retrieval_queries],
            "branch_intents": copy.deepcopy(list(self.branch_intents)),
            "compact_contract": copy.deepcopy(self.compact_contract),
            "qur_sha256": self.qur_sha256,
        }


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _identifier(prefix: str, value: Any, length: int = 24) -> str:
    return f"{prefix}_{_sha256_json(value)[:length]}"


def _unique_text(values: list[str]) -> tuple[str, ...]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        rendered = str(value).strip()
        if not rendered or rendered.casefold() == "unknown" or rendered in seen:
            continue
        seen.add(rendered)
        result.append(rendered)
    return tuple(result)


def _render_value(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        value = value.strip()
        return value if value and value.casefold() != "unknown" else None
    if isinstance(value, (bool, int, float)):
        return _canonical_json(value)
    if isinstance(value, (list, dict)):
        return _canonical_json(value)
    return str(value).strip() or None


def _unknown_intent(question_id: str | None, question: str) -> dict[str, Any]:
    """Build the closed typed branch used only when no trusted branch exists."""

    token = _sha256_json({"question_id": question_id, "question": question})
    graph_core = {
        "external_inputs": [
            {
                "input_ref": "input_unknown",
                "input_type": "unknown",
                "source": "question",
                "source_ref": f"question:{token[:24]}",
                "description": "Unresolved question input",
            }
        ],
        "nodes": [
            {
                "operation_id": "op_unknown",
                "operator": "unknown",
                "input_refs": ["input_unknown"],
                "output_ref": "value_unknown",
            }
        ],
        "edges": [],
        "scope_inheritance": {
            "default": "inherit_previous_output",
            "reset_requires": "explicit_instruction",
        },
    }
    return {
        "target": {"surface": None, "canonical_type": None, "instance": None},
        "scope": {
            "container": None,
            "location": None,
            "time_or_version": None,
            "filters": [],
            "source": "unknown",
            "match_mode": "unknown",
        },
        "operation_graph": {
            "operation_graph_id": _identifier("graph", graph_core),
            **graph_core,
        },
        "requested_outputs": [
            {
                "output_id": "output_unknown",
                "source_operation_ref": "op_unknown",
                "return_field": "unknown",
                "cardinality": {"mode": "unknown", "expected_count": None},
                "answer_shape": {
                    "container": "unknown",
                    "value_type": "unknown",
                    "unit": None,
                    "precision": "unspecified",
                },
                "display_precision": None,
            }
        ],
        "derived_summary": {
            "operation": "unknown",
            "return_fields": ["unknown"],
            "cardinality": "unknown",
        },
    }


def _extended_intent(contract: Mapping[str, Any]) -> dict[str, Any]:
    """Project a validated extended-rule graph into the runtime intent shape."""

    bindings = contract.get("bindings") or {}
    scope = contract.get("scope") or {}
    graph = contract["operation_graph"]
    requested = contract["requested_output"]
    shape = requested["answer_shape"]
    return_field = (
        bindings.get("target")
        or bindings.get("measure")
        or bindings.get("id_field")
        or contract["rule_id"]
    )
    external_inputs = [
        {
            "input_ref": "input_question",
            "input_type": "record_set",
            "source": "scope",
            "source_ref": "question:extended-graph",
            "description": "Records selected by the validated extended graph scope",
        }
    ]
    graph_core = {
        "external_inputs": external_inputs,
        "nodes": copy.deepcopy(graph["nodes"]),
        "edges": copy.deepcopy(graph["edges"]),
        "scope_inheritance": {
            "default": "inherit_previous_output",
            "reset_requires": "explicit_instruction",
        },
    }
    output = {
        "output_id": "output_extended",
        "source_operation_ref": requested["source_operation_ref"],
        "return_field": str(return_field),
        "cardinality": {
            "mode": requested["cardinality"],
            "expected_count": None,
        },
        "answer_shape": {
            "container": shape["container"],
            "value_type": shape["value_type"],
            "unit": shape.get("unit"),
            "precision": (
                "rounded"
                if isinstance(requested.get("display_precision"), Mapping)
                else "unspecified"
            ),
        },
        "display_precision": copy.deepcopy(requested.get("display_precision")),
    }
    if isinstance(requested.get("required_keys"), list):
        output["required_keys"] = copy.deepcopy(requested["required_keys"])
    return {
        "target": {
            "surface": bindings.get("target"),
            "canonical_type": (
                "identifier" if shape["value_type"] == "identifier" else "value"
            ),
            "instance": None,
        },
        "scope": {
            "container": scope.get("container"),
            "location": scope.get("location"),
            "time_or_version": None,
            "filters": [],
            "source": "explicit" if scope.get("location") or scope.get("container") else "unknown",
            "match_mode": "exact_normalized",
        },
        "operation_graph": {
            "operation_graph_id": _identifier("graph", graph_core),
            **graph_core,
        },
        "requested_outputs": [output],
        "derived_summary": {
            "operation": graph["nodes"][-1]["operator"],
            "return_fields": [str(return_field)],
            "cardinality": requested["cardinality"],
        },
        "extended_graph_contract": copy.deepcopy(dict(contract)),
    }


def _strict_result(qur: Mapping[str, Any]) -> tuple[str, tuple[str, ...]]:
    final_status = str(qur.get("final_status") or "failed")
    if final_status == "ready_for_retrieval":
        strict_status = "pass"
    elif final_status in {"clarification_required", "abstained"}:
        strict_status = "hold"
    else:
        strict_status = "fail"

    reasons: list[str] = []
    gate = qur.get("intent_gate")
    if isinstance(gate, Mapping):
        reason_codes = gate.get("reason_codes")
        if isinstance(reason_codes, list):
            reasons.extend(str(item) for item in reason_codes if item)
    errors = qur.get("errors")
    if isinstance(errors, list):
        for error in errors:
            if isinstance(error, Mapping) and error.get("code"):
                reasons.append(str(error["code"]))
    if strict_status != "pass" and not reasons:
        reasons.append(final_status or "question_understanding_not_ready")
    return strict_status, _unique_text(reasons)


def _coverage_requirement(intent: Mapping[str, Any]) -> str:
    outputs = intent.get("requested_outputs")
    nodes = (intent.get("operation_graph") or {}).get("nodes")
    output_records = outputs if isinstance(outputs, list) else []
    node_records = nodes if isinstance(nodes, list) else []
    operators = {
        str(node.get("operator"))
        for node in node_records
        if isinstance(node, Mapping)
    }
    cardinalities = {
        str((output.get("cardinality") or {}).get("mode"))
        for output in output_records
        if isinstance(output, Mapping)
    }
    return_fields = {
        str(output.get("return_field"))
        for output in output_records
        if isinstance(output, Mapping)
    }
    if operators & {
        "calculate",
        "sum",
        "mean",
        "min",
        "max",
        "absolute_distance",
        "argmin_all",
        "argmax_all",
        "group",
    }:
        return "exhaustive"
    if "count" in operators or "count" in return_fields:
        return "authoritative_aggregate"
    if "all" in cardinalities:
        return "authoritative_enumeration"
    return "none"


def _retrieval_query(
    original_question: str,
    branch_id: str,
    intent: Mapping[str, Any],
) -> BranchRetrievalQuery:
    """Build a retrieval query from question-bound literals only.

    Internal graph vocabulary such as ``project``, ``identifier`` or
    ``exact_normalized`` belongs in the answer contract, not in BM25 or
    embedding input.  Mixing that DSL into search was measured to displace
    relevant source files with code/configuration files.  The query therefore
    keeps the original question and only boosts literal values that are
    actually present in it.
    """

    required: list[str] = []

    def add_literal(value: Any) -> None:
        rendered = _render_value(value)
        if rendered is None:
            return
        if rendered in original_question:
            required.append(rendered)

    target = intent.get("target") or {}
    if isinstance(target, Mapping):
        for key in ("surface", "instance"):
            add_literal(target.get(key))

    scope = intent.get("scope") or {}
    if isinstance(scope, Mapping):
        for key in ("container", "location", "time_or_version"):
            add_literal(scope.get(key))
        filters = scope.get("filters")
        if isinstance(filters, list):
            for predicate in filters:
                if not isinstance(predicate, Mapping):
                    continue
                add_literal(predicate.get("field"))
                add_literal(predicate.get("value"))

    graph = intent.get("operation_graph") or {}
    nodes = graph.get("nodes") if isinstance(graph, Mapping) else []
    if isinstance(nodes, list):
        for node in nodes:
            if not isinstance(node, Mapping):
                continue
            predicate = node.get("predicate")
            if isinstance(predicate, Mapping):
                add_literal(predicate.get("field"))
                add_literal(predicate.get("value"))
            fields = node.get("fields")
            if isinstance(fields, list):
                for field in fields:
                    add_literal(field)
            add_literal(node.get("field"))

    outputs = intent.get("requested_outputs")
    if isinstance(outputs, list):
        for output in outputs:
            if not isinstance(output, Mapping):
                continue
            add_literal(output.get("return_field"))

    literal_terms = _unique_text(required)

    return BranchRetrievalQuery(
        branch_id=branch_id,
        query_text=original_question,
        coverage_requirement=_coverage_requirement(intent),
        required_terms=literal_terms,
        optional_terms=(),
    )


def _compact_operation(node: Mapping[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {
        "operation_id": node.get("operation_id"),
        "operator": node.get("operator"),
    }
    for key in (
        "predicate",
        "fields",
        "calculation_precision",
        "candidate_set_ref",
        "distance",
        "field",
        "tie_policy",
        "sort_order",
    ):
        if key in node:
            result[key] = copy.deepcopy(node[key])
    return result


def _branch_contract(branch: Mapping[str, Any]) -> dict[str, Any]:
    intent = branch["intent"]
    graph = intent.get("operation_graph") or {}
    nodes = graph.get("nodes") if isinstance(graph, Mapping) else []
    node_records = [
        node for node in (nodes if isinstance(nodes, list) else [])
        if isinstance(node, Mapping)
    ]
    consumed_refs = {
        str(reference)
        for node in node_records
        for reference in (
            list(node.get("input_refs") or [])
            + ([node["candidate_set_ref"]] if node.get("candidate_set_ref") else [])
        )
    }
    output_ref_by_operation = {
        str(node.get("operation_id")): str(node.get("output_ref"))
        for node in node_records
        if node.get("operation_id") and node.get("output_ref")
    }
    requested_outputs = intent.get("requested_outputs") or []
    terminal_outputs = [
        output
        for output in requested_outputs
        if isinstance(output, Mapping)
        and output_ref_by_operation.get(str(output.get("source_operation_ref")))
        not in consumed_refs
    ]
    if not terminal_outputs:
        terminal_outputs = [
            output for output in requested_outputs if isinstance(output, Mapping)
        ]
    return {
        "branch_id": branch["branch_id"],
        "target": copy.deepcopy(intent.get("target")),
        "scope": copy.deepcopy(intent.get("scope")),
        "operations": [
            _compact_operation(node)
            for node in node_records
        ],
        "requested_outputs": copy.deepcopy(terminal_outputs),
    }


def _compact_contract(
    question_id: str | None,
    strict_status: str,
    strict_reasons: tuple[str, ...],
    branches: tuple[dict[str, Any], ...],
    qic: Mapping[str, Any],
) -> dict[str, Any]:
    branch_contracts = [_branch_contract(branch) for branch in branches]
    requested_outputs = [item["requested_outputs"] for item in branch_contracts]
    common_outputs: list[dict[str, Any]] | None = None
    if requested_outputs and all(
        _canonical_json(value) == _canonical_json(requested_outputs[0])
        for value in requested_outputs[1:]
    ):
        common_outputs = copy.deepcopy(requested_outputs[0])

    forbidden: list[dict[str, Any]] = []
    forbidden_groups = qic.get("forbidden")
    if isinstance(forbidden_groups, Mapping):
        for category in ("global", "query", "evidence"):
            rules = forbidden_groups.get(category)
            if not isinstance(rules, list):
                continue
            for rule in rules:
                if not isinstance(rule, Mapping):
                    continue
                stages = rule.get("applies_to")
                if not isinstance(stages, list) or not {
                    "generation",
                    "validation",
                }.intersection(str(stage) for stage in stages):
                    continue
                forbidden.append(
                    {
                        "rule_id": rule.get("rule_id"),
                        "prohibition": rule.get("prohibition"),
                        "on_violation": rule.get("on_violation"),
                    }
                )

    not_requested: list[dict[str, Any]] = []
    raw_not_requested = qic.get("not_requested")
    if isinstance(raw_not_requested, list):
        for item in raw_not_requested:
            if isinstance(item, Mapping):
                not_requested.append(
                    {
                        "item": item.get("item"),
                        "handling": item.get("handling"),
                    }
                )

    return {
        "contract_version": f"question-graph-answer-contract-{GRAPH_PLAN_VERSION}",
        "question_id": question_id,
        "mode": "aggressive_graph",
        "strict_status": strict_status,
        "strict_reasons": list(strict_reasons),
        "common_requested_outputs": common_outputs,
        "branches": branch_contracts,
        "not_requested": not_requested,
        "forbidden": forbidden,
        "render_policy": {
            "answer_only_requested_outputs": True,
            "preserve_output_order": True,
            "do_not_merge_incompatible_branches": True,
            "abstain_text": "わかりません",
        },
    }


def _validated_qur(
    question_input: dict[str, Any],
    *,
    client: Any,
    cache_dir: Path | None,
    timeout: float,
    retry_limit: int,
    max_branches: int,
    restart: bool,
) -> tuple[dict[str, Any], tuple[str, ...]]:
    """Build a QUR, replacing an invalid runtime result with a valid failure QUR."""

    replacement_reasons: list[str] = []
    try:
        qur = build_question_understanding(
            question_input,
            client=client,
            cache_dir=cache_dir,
            timeout=timeout,
            retry_limit=retry_limit,
            max_branches=max_branches,
            restart=restart,
        )
    except Exception as exc:  # The returned plan must still have a typed branch.
        replacement_reasons.append("question_understanding_runtime_error")
        qur = build_failed_understanding(
            question_input,
            exc,
            timeout=timeout,
            retry_limit=retry_limit,
            max_branches=max_branches,
        )

    errors: list[str]
    try:
        errors = list(validate_understanding_run(qur))
    except Exception:
        errors = ["question_understanding_validator_error"]
    if (
        qur.get("question_id") != question_input["question_id"]
        or qur.get("original_question") != question_input["original_question"]
    ):
        errors.append("question_understanding_identity_mismatch")
    if errors:
        replacement_reasons.append("question_understanding_validation_failed")
        qur = build_failed_understanding(
            question_input,
            RuntimeError("question_understanding_validation_failed"),
            timeout=timeout,
            retry_limit=retry_limit,
            max_branches=max_branches,
        )
        fallback_errors = validate_understanding_run(qur)
        if fallback_errors:
            raise RuntimeError(
                "internal failure QuestionUnderstandingRun is invalid: "
                + "; ".join(fallback_errors[:8])
            )
    return qur, _unique_text(replacement_reasons)


def build_graph_plan(
    question_id: str | None,
    question: str,
    *,
    client: Any = None,
    cache_dir: Path | None = None,
    timeout: float = 180.0,
    retry_limit: int = 1,
    max_branches: int = DEFAULT_MAX_BRANCHES,
    restart: bool = False,
    fast_advisory: bool = False,
) -> GraphPlan:
    """Build one validated question graph and its branch-local RAG contract.

    ``client`` must implement the ``StructuredIntentClient`` protocol used by
    :mod:`build_question_understanding`.  With ``client=None`` the existing
    deterministic supported lane is tried first, followed by the configured
    local structured-intent backend.  ``cache_dir`` is passed only to that
    question-only compiler.  Production callers can set
    ``fast_advisory=True`` to compile unsupported questions with the
    deterministic lexical graph instead of waiting on the experimental model.
    """

    if question_id is not None and (not isinstance(question_id, str) or not question_id):
        raise ValueError("question_id must be a non-empty string or None")
    if not isinstance(question, str) or not question:
        raise ValueError("question must be a non-empty string")
    if timeout <= 0:
        raise ValueError("timeout must be positive")
    if retry_limit not in {0, 1}:
        raise ValueError("retry_limit must be 0 or 1")
    if max_branches < 1:
        raise ValueError("max_branches must be positive")
    if not isinstance(fast_advisory, bool):
        raise ValueError("fast_advisory must be boolean")
    if cache_dir is not None:
        cache_dir = Path(cache_dir)

    question_input = {
        "question_id": question_id,
        "original_question": question,
    }

    # Certified extended grammars are already complete question graphs.  Build
    # them before invoking the slower structured-intent model so deterministic
    # questions never spend minutes waiting for a redundant model proposal.
    from score_candidate_rules import (
        graph_contract_for_question,
        validate_graph_contract,
    )

    extended_contract = graph_contract_for_question(question)
    if extended_contract is not None:
        if not validate_graph_contract(question, extended_contract):
            raise RuntimeError("extended graph contract failed deterministic validation")
        extended_intent = _extended_intent(extended_contract)
        branch = {
            "branch_id": _identifier(
                "branch_extended", extended_contract["graph_contract_id"]
            ),
            "status": "resolved",
            "intent": extended_intent,
        }
        branches = (branch,)
        strict_reasons = ("extended_graph_certified",)
        retrieval_queries = (
            _retrieval_query(question, str(branch["branch_id"]), extended_intent),
        )
        return GraphPlan(
            question_id=question_id,
            original_question=question,
            qur_id=_identifier("qur", extended_contract),
            qic_id=_identifier("qic", extended_intent),
            qur_final_status="ready_for_retrieval",
            strict_status="pass",
            strict_reasons=strict_reasons,
            advisory_usable=True,
            fallback_used=False,
            retrieval_queries=retrieval_queries,
            branch_intents=branches,
            compact_contract=_compact_contract(
                question_id,
                "pass",
                strict_reasons,
                branches,
                {},
            ),
            qur_sha256=_sha256_json(extended_contract),
        )

    if (
        fast_advisory
        and client is None
        and derive_supported_intent_draft(question_input) is None
    ):
        from generic_question_graph import compile_advisory_intent

        advisory = compile_advisory_intent(question_id, question)
        intent = copy.deepcopy(advisory["intent"])
        branch = {
            "branch_id": _identifier(
                "branch_advisory", advisory["advisory_intent_id"]
            ),
            "status": "advisory",
            "intent": intent,
        }
        branches = (branch,)
        strict_reasons = (
            "question_equivalence_unproven",
            "generic_advisory_graph",
        )
        retrieval_queries = (
            _retrieval_query(question, str(branch["branch_id"]), intent),
        )
        return GraphPlan(
            question_id=question_id,
            original_question=question,
            qur_id=None,
            qic_id=None,
            qur_final_status="advisory_compiled",
            strict_status="hold",
            strict_reasons=strict_reasons,
            advisory_usable=True,
            fallback_used=False,
            retrieval_queries=retrieval_queries,
            branch_intents=branches,
            compact_contract=_compact_contract(
                question_id,
                "hold",
                strict_reasons,
                branches,
                {},
            ),
            qur_sha256=_sha256_json(advisory),
        )

    qur, replacement_reasons = _validated_qur(
        question_input,
        client=client,
        cache_dir=cache_dir,
        timeout=timeout,
        retry_limit=retry_limit,
        max_branches=max_branches,
        restart=restart,
    )
    strict_status, strict_reasons = _strict_result(qur)
    strict_reasons = _unique_text([*strict_reasons, *replacement_reasons])

    qic = qur["question_intent_contract"]
    raw_branches = qur.get("candidate_query_paths")
    fallback_used = qur.get("final_status") == "failed"
    branches: list[dict[str, Any]] = []
    if not fallback_used and isinstance(raw_branches, list):
        for branch in raw_branches:
            if not isinstance(branch, Mapping):
                continue
            intent = branch.get("candidate_intent")
            branch_id = branch.get("branch_id")
            if not isinstance(intent, Mapping) or not isinstance(branch_id, str):
                continue
            branches.append(
                {
                    "branch_id": branch_id,
                    "status": str(branch.get("status") or "pending"),
                    "intent": copy.deepcopy(dict(intent)),
                }
            )

    if not branches:
        fallback_used = True
        requested = qic.get("requested") if isinstance(qic, Mapping) else None
        if not isinstance(requested, Mapping):
            requested = _unknown_intent(question_id, question)
        fallback_core = {
            "question_id": question_id,
            "question": question,
            "intent": requested,
        }
        branches = [
            {
                "branch_id": _identifier("branch_fallback", fallback_core),
                "status": UNKNOWN_BRANCH_STATUS,
                "intent": copy.deepcopy(dict(requested)),
            }
        ]

    branch_tuple = tuple(branches)
    retrieval_queries = tuple(
        _retrieval_query(
            question,
            str(branch["branch_id"]),
            branch["intent"],
        )
        for branch in branch_tuple
    )
    contract = _compact_contract(
        question_id,
        strict_status,
        strict_reasons,
        branch_tuple,
        qic,
    )
    return GraphPlan(
        question_id=question_id,
        original_question=question,
        qur_id=str(qur.get("question_understanding_run_id"))
        if qur.get("question_understanding_run_id")
        else None,
        qic_id=str(qic.get("question_intent_contract_id"))
        if isinstance(qic, Mapping) and qic.get("question_intent_contract_id")
        else None,
        qur_final_status=str(qur.get("final_status") or "failed"),
        strict_status=strict_status,
        strict_reasons=strict_reasons,
        advisory_usable=bool(retrieval_queries),
        fallback_used=fallback_used,
        retrieval_queries=retrieval_queries,
        branch_intents=tuple(copy.deepcopy(branch) for branch in branch_tuple),
        compact_contract=contract,
        qur_sha256=_sha256_json(qur),
    )


__all__ = [
    "GRAPH_PLAN_VERSION",
    "BranchRetrievalQuery",
    "GraphPlan",
    "build_graph_plan",
]
