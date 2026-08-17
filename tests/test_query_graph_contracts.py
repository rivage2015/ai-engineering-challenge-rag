from __future__ import annotations

import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import jsonschema


REPOSITORY = Path(__file__).resolve().parents[1]
SCRIPTS = REPOSITORY / "scripts"
sys.path.insert(0, str(SCRIPTS))

import validate_intermediate_records as intermediate_validator
import validate_intermediate_records_streaming as streaming_intermediate_validator
import validate_query_graph_records as query_validator


STAMP = "2026-08-16T00:00:00+00:00"
FORBIDDEN_VALIDATORS = {
    "global": (
        "claims_supported_by_evidence",
        "unresolved_never_promoted",
        "causality_requires_source_relation",
        "evidence_is_read_only",
        "answer_sources_are_excluded",
    ),
    "query": (
        "operator_preserved",
        "hard_scope_not_expanded",
        "output_contract_match",
    ),
    "evidence": (
        "estimated_not_exact",
        "unit_requires_evidence",
        "compatible_evidence_only",
        "provenance_required",
    ),
}
VALIDATOR_STAGES = {
    "claims_supported_by_evidence": ("generation", "validation"),
    "unresolved_never_promoted": ("generation", "validation"),
    "causality_requires_source_relation": ("generation", "validation"),
    "evidence_is_read_only": ("retrieval", "generation", "validation"),
    "answer_sources_are_excluded": ("retrieval", "validation"),
    "operator_preserved": ("intent", "validation"),
    "hard_scope_not_expanded": ("intent", "retrieval", "validation"),
    "output_contract_match": ("intent", "generation", "validation"),
    "estimated_not_exact": ("retrieval", "generation", "validation"),
    "unit_requires_evidence": ("retrieval", "generation", "validation"),
    "compatible_evidence_only": ("retrieval", "generation", "validation"),
    "provenance_required": ("retrieval", "generation", "validation"),
}
INTENT_GATE_CHECK_IDS = (
    "operation_graph_compilable",
    "target_resolved",
    "requested_outputs_resolved",
    "scope_resolved",
    "explicit_consistency",
    "pre_retrieval_type_safety",
    "forbidden_precheck",
    "ambiguity_branched",
)
ANSWERABILITY_GATE_CHECK_IDS = (
    "intent_resolved",
    "primary_path_resolved",
    "evidence_path_complete",
    "evidence_compatible",
    "proof_satisfied",
    "forbidden_clear",
)


def write_jsonl(path: Path, records: list[dict[str, object]]) -> None:
    path.write_text(
        "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records),
        encoding="utf-8",
    )


def forbidden_rules() -> dict[str, list[dict[str, object]]]:
    result: dict[str, list[dict[str, object]]] = {}
    for category, validator_ids in FORBIDDEN_VALIDATORS.items():
        result[category] = [
            {
                "rule_id": f"rule_{validator_id}",
                "category": category,
                "prohibition": f"Enforce {validator_id}",
                "basis": "IG-GE v0.2 invariant",
                "basis_ref": None,
                "applies_to": list(VALIDATOR_STAGES[validator_id]),
                "check": {"validator_id": validator_id, "params": {}},
                "on_violation": "abstain",
            }
            for validator_id in validator_ids
        ]
    return result


def simple_contract() -> dict[str, object]:
    return {
        "schema_version": "0.1",
        "record_type": "question_intent_contract",
        "question_intent_contract_id": "qic_" + "1" * 32,
        "question_id": "q_fixture_simple",
        "original_question": "対象範囲に含まれる識別子をすべて挙げてください。",
        "requested": {
            "target": {
                "surface": "識別子",
                "canonical_type": "record",
                "instance": None,
            },
            "scope": {
                "container": "source.csv",
                "location": None,
                "time_or_version": None,
                "filters": [],
                "source": "explicit",
                "match_mode": "exact_normalized",
            },
            "operation_graph": {
                "operation_graph_id": "graph_main",
                "external_inputs": [
                    {
                        "input_ref": "source_set",
                        "input_type": "record_set",
                        "source": "scope",
                        "source_ref": "share/project/source.csv",
                        "description": "Question-scoped source rows",
                    }
                ],
                "nodes": [
                    {
                        "operation_id": "op_list",
                        "operator": "list",
                        "input_refs": ["source_set"],
                        "output_ref": "identifier_values",
                    }
                ],
                "edges": [],
                "scope_inheritance": {
                    "default": "inherit_previous_output",
                    "reset_requires": "explicit_instruction",
                },
            },
            "requested_outputs": [
                {
                    "output_id": "identifiers",
                    "source_operation_ref": "op_list",
                    "return_field": "identifier",
                    "cardinality": {"mode": "all", "expected_count": None},
                    "answer_shape": {
                        "container": "list",
                        "value_type": "identifier",
                        "unit": None,
                        "precision": "exact",
                    },
                    "display_precision": None,
                }
            ],
            "derived_summary": {
                "operation": "list",
                "return_fields": ["identifier"],
                "cardinality": "all",
            },
        },
        "not_requested": [],
        "forbidden": forbidden_rules(),
        "ambiguity": [],
        "provenance": {
            "analyzer": "deterministic_fixture",
            "analyzer_version": "0.1",
            "rule_version": "v0.2",
            "generated_at": STAMP,
            "deterministic": True,
            "intent_origin": "supplied_draft",
            "intent_input_sha256": "a" * 64,
            "question_independent": False,
            "answer_data_used": False,
            "past_answers_used": False,
        },
    }


def compound_contract() -> dict[str, object]:
    record = simple_contract()
    record["question_intent_contract_id"] = "qic_" + "2" * 32
    record["question_id"] = "q_fixture_compound"
    record["original_question"] = (
        "条件に一致する行の値の平均と、その平均に最も近い行の識別子をすべて答えてください。"
    )
    requested = record["requested"]
    requested["target"] = {
        "surface": "条件に一致する行",
        "canonical_type": "record",
        "instance": None,
    }
    requested["scope"] = {
        "container": "source.csv",
        "location": None,
        "time_or_version": None,
        "filters": [
            {"field": "category", "operator": "eq", "value": "target"},
            {"field": "amount", "operator": "gt", "value": 100},
        ],
        "source": "explicit",
        "match_mode": "exact_normalized",
    }
    requested["operation_graph"] = {
        "operation_graph_id": "graph_main",
        "external_inputs": [
            {
                "input_ref": "source_set",
                "input_type": "record_set",
                "source": "scope",
                "source_ref": "share/project/source.csv",
                "description": "Question-scoped source rows",
            }
        ],
        "nodes": [
            {
                "operation_id": "op_filter_category",
                "operator": "filter",
                "input_refs": ["source_set"],
                "predicate": {"field": "category", "operator": "eq", "value": "target"},
                "output_ref": "category_set",
            },
            {
                "operation_id": "op_filter_amount",
                "operator": "filter",
                "input_refs": ["category_set"],
                "predicate": {"field": "amount", "operator": "gt", "value": 100},
                "output_ref": "filtered_set",
            },
            {
                "operation_id": "op_project_value",
                "operator": "project",
                "input_refs": ["filtered_set"],
                "fields": ["value"],
                "output_ref": "numeric_values",
            },
            {
                "operation_id": "op_mean",
                "operator": "mean",
                "input_refs": ["numeric_values"],
                "calculation_precision": "exact_unrounded",
                "output_ref": "mean_value",
            },
            {
                "operation_id": "op_nearest",
                "operator": "argmin_all",
                "input_refs": ["filtered_set", "mean_value"],
                "candidate_set_ref": "filtered_set",
                "distance": "absolute",
                "field": "value",
                "tie_policy": "all",
                "output_ref": "nearest_rows",
            },
            {
                "operation_id": "op_project_id",
                "operator": "project",
                "input_refs": ["nearest_rows"],
                "fields": ["id"],
                "output_ref": "nearest_ids",
            },
        ],
        "edges": [
            {"from": "op_filter_category", "to": "op_filter_amount"},
            {"from": "op_filter_amount", "to": "op_project_value"},
            {"from": "op_project_value", "to": "op_mean"},
            {"from": "op_filter_amount", "to": "op_nearest"},
            {"from": "op_mean", "to": "op_nearest"},
            {"from": "op_nearest", "to": "op_project_id"},
        ],
        "scope_inheritance": {
            "default": "inherit_previous_output",
            "reset_requires": "explicit_instruction",
        },
    }
    requested["requested_outputs"] = [
        {
            "output_id": "mean_output",
            "source_operation_ref": "op_mean",
            "return_field": "value",
            "cardinality": {"mode": "single", "expected_count": 1},
            "answer_shape": {
                "container": "scalar",
                "value_type": "number",
                "unit": None,
                "precision": "exact",
            },
            "display_precision": {"mode": "decimal_places", "digits": 2},
        },
        {
            "output_id": "nearest_identifiers",
            "source_operation_ref": "op_project_id",
            "return_field": "identifier",
            "cardinality": {"mode": "all", "expected_count": None},
            "answer_shape": {
                "container": "list",
                "value_type": "identifier",
                "unit": None,
                "precision": "exact",
            },
            "display_precision": None,
        },
    ]
    requested["derived_summary"] = {
        "operation": "calculate",
        "return_fields": ["value", "identifier"],
        "cardinality": "mixed",
    }
    return record


def populate_forbidden_check_results(record: dict[str, object]) -> None:
    contract = record["question_intent_contract"]
    requested = contract["requested"]
    operation_ids = sorted(
        node["operation_id"] for node in requested["operation_graph"]["nodes"]
    )
    output_ids = sorted(
        output["output_id"] for output in requested["requested_outputs"]
    )
    graph_id = requested["operation_graph"]["operation_graph_id"]
    completed_retrieval_run_ids = sorted(
        retrieval_run["retrieval_run_id"]
        for retrieval_run in record["retrieval_runs"]
        if retrieval_run["status"] == "completed"
    )
    document_ids = sorted(
        {
            hit["document_id"]
            for hit in record["retrieval_hits"]
        }
        | {
            node["document_id"]
            for bundle in record["retrieved_evidence_bundles"]
            for node in bundle["evidence_nodes"]
        }
    )
    bundle_evidence_ids = sorted(
        {
            node["evidence_id"]
            for bundle in record["retrieved_evidence_bundles"]
            for node in bundle["evidence_nodes"]
        }
    )
    claims = (
        record["answer_plan"]["allowed_claims"]
        if record["answer_plan"] is not None
        else []
    )
    claim_ids = sorted(claim["claim_id"] for claim in claims)
    claim_evidence_ids = sorted(
        {
            evidence_id
            for claim in claims
            for evidence_id in claim["evidence_ids"]
        }
    )
    claim_subject_validators = {
        "claims_supported_by_evidence",
        "unresolved_never_promoted",
        "causality_requires_source_relation",
    }
    evidence_subject_validators = {
        "evidence_is_read_only",
        "estimated_not_exact",
        "unit_requires_evidence",
        "compatible_evidence_only",
        "provenance_required",
    }
    query_only_validators = {
        "operator_preserved",
        "hard_scope_not_expanded",
        "output_contract_match",
    }

    def subjects_for(validator_id: str, stage: str) -> list[str]:
        if validator_id == "operator_preserved":
            return operation_ids
        if validator_id == "hard_scope_not_expanded":
            if stage == "intent":
                return [graph_id]
            if stage == "retrieval":
                return completed_retrieval_run_ids
            return [graph_id, *completed_retrieval_run_ids]
        if validator_id == "output_contract_match":
            if stage == "intent":
                return output_ids
            return [*output_ids, *claim_ids]
        if validator_id == "answer_sources_are_excluded":
            return document_ids if stage == "retrieval" else claim_ids
        if validator_id in claim_subject_validators:
            return claim_ids
        if validator_id in evidence_subject_validators:
            return bundle_evidence_ids if stage == "retrieval" else claim_ids
        raise AssertionError(f"unmapped validator fixture: {validator_id}")

    stage_status_key = {
        "intent": "intent",
        "retrieval": "retrieval",
        "generation": "generation",
        "validation": "output_validation",
    }
    results = []
    for rules in contract["forbidden"].values():
        for rule in rules:
            validator_id = rule["check"]["validator_id"]
            for stage in rule["applies_to"]:
                if record["stage_statuses"][stage_status_key[stage]] == "skipped":
                    continue
                if validator_id in query_only_validators:
                    evidence_ids = []
                elif stage == "retrieval":
                    evidence_ids = bundle_evidence_ids
                else:
                    evidence_ids = claim_evidence_ids
                results.append(
                    {
                        "rule_id": rule["rule_id"],
                        "stage": stage,
                        "validator_id": validator_id,
                        "validator_version": "0.1",
                        "status": "pass",
                        "subject_refs": subjects_for(validator_id, stage),
                        "evidence_ids": evidence_ids,
                        "details": {},
                        "action_taken": "none",
                    }
                )
    record["forbidden_check_results"] = results


def accepted_query_run() -> dict[str, object]:
    contract = simple_contract()
    question_id = contract["question_id"]
    branch_id = "branch_main"
    evidence_id = "ev_" + "3" * 32
    document_id = "doc_" + "4" * 32
    search_unit_id = "su_" + "5" * 32
    query_run_id = "qr_" + "6" * 32
    record = {
        "schema_version": "0.1",
        "record_type": "query_run",
        "query_run_id": query_run_id,
        "question_id": question_id,
        "original_question": contract["original_question"],
        "question_intent_contract": contract,
        "stage_statuses": {
            "intent": "completed",
            "context": "completed",
            "candidate_paths": "completed",
            "intent_gate": "completed",
            "retrieval": "completed",
            "candidate_evaluation": "completed",
            "proof": "completed",
            "answerability": "completed",
            "answer_planning": "completed",
            "generation": "completed",
            "output_validation": "completed",
        },
        "query_context_graph": {"edges": [], "rejected_context": []},
        "candidate_query_paths": [
            {
                "branch_id": branch_id,
                "parent_question_id": question_id,
                "candidate_intent": copy.deepcopy(contract["requested"]),
                "assumptions": [],
                "status": "completed",
                "evidence_ids": [evidence_id],
                "result": "resolved",
                "error": None,
            }
        ],
        "intent_gate": {
            "status": "pass",
            "checks": [
                {"check_id": check_id, "status": "pass", "detail": None}
                for check_id in INTENT_GATE_CHECK_IDS
            ],
            "action": "retrieve",
            "reason_codes": [],
        },
        "retrieval_runs": [
            {
                "retrieval_run_id": "retrieval_main",
                "branch_id": branch_id,
                "plan": {
                    "branch_id": branch_id,
                    "target_types": ["record"],
                    "return_fields": ["identifier"],
                    "scope_filters": [],
                    "channels": ["structured"],
                    "coverage_requirement": "authoritative_enumeration",
                    "scan_mode": "exhaustive",
                    "deprioritize": [],
                    "exclude": [],
                },
                "status": "completed",
                "started_at": STAMP,
                "completed_at": STAMP,
                "error": None,
            }
        ],
        "retrieval_hits": [
            {
                "branch_id": branch_id,
                "channel": "structured",
                "rank": 1,
                "score": None,
                "search_unit_id": search_unit_id,
                "document_id": document_id,
                "source_evidence_ids": [evidence_id],
                "locator_text": "row 1",
                "evidence_text": "id_1",
            }
        ],
        "retrieved_evidence_bundles": [
            {
                "query_branch_id": branch_id,
                "evidence_nodes": [
                    {
                        "evidence_id": evidence_id,
                        "discovered_by": ["structured"],
                        "role": "supporting",
                        "target_match": "matched",
                        "scope_match": "matched",
                        "exactness": "exact",
                        "document_id": document_id,
                        "search_unit_ids": [search_unit_id],
                        "locator_text": "row 1",
                    }
                ],
                "evidence_edges": [],
                "conflicts": [],
                "rejected_evidence": [],
            }
        ],
        "candidate_evaluations": [
            {
                "branch_id": branch_id,
                "status": "resolved",
                "disqualifiers": [],
                "signals": [
                    {
                        "name": "evidence_support",
                        "level": "strong",
                        "basis_refs": [evidence_id],
                    },
                    {
                        "name": "provenance_quality",
                        "level": "strong",
                        "basis_refs": [evidence_id],
                    },
                ],
                "evidence_ids": [evidence_id],
                "equivalence_class_id": None,
                "rationale": "One supported path remains.",
            }
        ],
        "primary_query_path": {
            "branch_id": branch_id,
            "equivalent_branch_ids": [],
            "evidence_ids": [evidence_id],
            "required_qualifiers": [],
            "rationale": "One supported path remains.",
        },
        "proof_obligation": {
            "operation_graph_ref": "graph_main",
            "requirements": [
                {
                    "requirement_id": "proof_complete_list",
                    "operation_ref": "op_list",
                    "output_ref": "identifiers",
                    "description": "The requested list is complete within scope.",
                    "required": True,
                    "status": "satisfied",
                    "evidence_ids": [evidence_id],
                }
            ],
            "coverage": {
                "method": "authoritative_enumeration",
                "scanned_count": 1,
                "matched_count": 1,
                "exhaustive": True,
                "evidence_ids": [evidence_id],
            },
            "overall": {"status": "satisfied"},
        },
        "forbidden_check_results": [],
        "answerability_gate": {
            "status": "pass",
            "checks": [
                {"check_id": check_id, "status": "pass", "detail": None}
                for check_id in ANSWERABILITY_GATE_CHECK_IDS
            ],
            "reason_codes": [],
            "action": "answer",
        },
        "answer_plan": {
            "output_plans": [
                {
                    "output_id": "identifiers",
                    "answer_mode": "deterministic",
                    "answer_shape": copy.deepcopy(
                        contract["requested"]["requested_outputs"][0]["answer_shape"]
                    ),
                    "allowed_claim_ids": ["claim_identifiers"],
                }
            ],
            "allowed_claims": [
                {
                    "claim_id": "claim_identifiers",
                    "value": ["id_1"],
                    "unit": None,
                    "exactness": "exact",
                    "evidence_ids": [evidence_id],
                }
            ],
            "required_qualifiers": [],
            "forbidden_rule_ids": [
                rule["rule_id"]
                for rules in contract["forbidden"].values()
                for rule in rules
            ],
        },
        "output_validation": {
            "checks": {
                "operation_match": "pass",
                "target_match": "pass",
                "requested_outputs_match": "pass",
                "scope_match": "pass",
                "allowed_claims_only": "pass",
                "exactness_match": "pass",
                "forbidden_violations": [],
            },
            "status": "pass",
            "action": "accept",
        },
        "final_answer": "id_1",
        "final_status": "accepted",
        "runtime_metadata": {
            "rule_version": "v0.2",
            "started_at": STAMP,
            "completed_at": STAMP,
            "duration_ms": 1,
            "models": [],
            "indexes": [{"kind": "structured", "sha256": "7" * 64}],
            "backend": "local_sequential",
            "parallel_config": {
                "max_concurrency": 1,
                "timeout_ms": None,
                "retry_limit": 0,
            },
        },
        "errors": [],
        "provenance": {
            "runner": "query_graph_fixture",
            "runner_version": "0.1",
            "generated_at": STAMP,
            "question_independent": False,
            "answer_data_used": False,
            "past_answers_used": False,
        },
    }
    populate_forbidden_check_results(record)
    return record


def abstained_query_run() -> dict[str, object]:
    record = accepted_query_run()
    record["query_run_id"] = "qr_" + "8" * 32
    record["stage_statuses"]["answer_planning"] = "skipped"
    record["stage_statuses"]["generation"] = "skipped"
    record["stage_statuses"]["output_validation"] = "skipped"
    record["retrieval_hits"] = []
    record["retrieved_evidence_bundles"][0]["evidence_nodes"] = []
    record["candidate_query_paths"][0]["evidence_ids"] = []
    record["candidate_query_paths"][0]["result"] = "unsupported"
    record["candidate_evaluations"][0].update(
        {
            "status": "unsupported",
            "disqualifiers": ["evidence_path_missing"],
            "signals": [],
            "evidence_ids": [],
            "rationale": "No supporting Evidence was retrieved.",
        }
    )
    record["primary_query_path"] = None
    requirement = record["proof_obligation"]["requirements"][0]
    requirement["status"] = "unsatisfied"
    requirement["evidence_ids"] = []
    record["proof_obligation"]["coverage"] = {
        "method": "none",
        "scanned_count": None,
        "matched_count": None,
        "exhaustive": False,
        "evidence_ids": [],
    }
    record["proof_obligation"]["overall"]["status"] = "unsatisfied"
    record["answerability_gate"] = {
        "status": "fail",
        "checks": [
            {"check_id": "proof_satisfied", "status": "fail", "detail": None}
        ],
        "reason_codes": ["no_supporting_evidence"],
        "action": "abstain",
    }
    record["answer_plan"] = None
    record["output_validation"] = None
    record["final_answer"] = "わかりません"
    record["final_status"] = "abstained"
    populate_forbidden_check_results(record)
    return record


def accepted_count_query_run() -> dict[str, object]:
    record = accepted_query_run()
    requested = record["question_intent_contract"]["requested"]
    requested["operation_graph"]["nodes"] = [
        {
            "operation_id": "op_count",
            "operator": "count",
            "input_refs": ["source_set"],
            "output_ref": "count_value",
        }
    ]
    requested["operation_graph"]["edges"] = []
    requested["requested_outputs"] = [
        {
            "output_id": "count_result",
            "source_operation_ref": "op_count",
            "return_field": "count",
            "cardinality": {"mode": "single", "expected_count": 1},
            "answer_shape": {
                "container": "scalar",
                "value_type": "integer",
                "unit": None,
                "precision": "exact",
            },
            "display_precision": None,
        }
    ]
    requested["derived_summary"] = {
        "operation": "count",
        "return_fields": ["count"],
        "cardinality": "single",
    }
    record["candidate_query_paths"][0]["candidate_intent"] = copy.deepcopy(requested)
    plan = record["retrieval_runs"][0]["plan"]
    plan["return_fields"] = ["count"]
    plan["coverage_requirement"] = "authoritative_aggregate"
    plan["scan_mode"] = "top_k"
    proof = record["proof_obligation"]
    proof["requirements"][0].update(
        {
            "operation_ref": "op_count",
            "output_ref": "count_result",
            "description": "An authoritative aggregate supplies the scoped count.",
        }
    )
    proof["coverage"].update(
        {
            "method": "authoritative_aggregate",
            "scanned_count": None,
            "matched_count": 3,
            "exhaustive": False,
        }
    )
    answer_plan = record["answer_plan"]
    answer_plan["output_plans"] = [
        {
            "output_id": "count_result",
            "answer_mode": "deterministic",
            "answer_shape": copy.deepcopy(
                requested["requested_outputs"][0]["answer_shape"]
            ),
            "allowed_claim_ids": ["claim_count"],
        }
    ]
    answer_plan["allowed_claims"] = [
        {
            "claim_id": "claim_count",
            "value": 3,
            "unit": None,
            "exactness": "exact",
            "evidence_ids": copy.deepcopy(proof["coverage"]["evidence_ids"]),
        }
    ]
    record["final_answer"] = "3"
    populate_forbidden_check_results(record)
    return record


def add_secondary_branch(record: dict[str, object]) -> str:
    branch_id = "branch_secondary"
    evidence_id = "ev_" + "b" * 32
    search_unit_id = "su_" + "c" * 32
    document_id = "doc_" + "d" * 32

    candidate = copy.deepcopy(record["candidate_query_paths"][0])
    candidate.update(
        {
            "branch_id": branch_id,
            "evidence_ids": [evidence_id],
            "result": "disqualified",
        }
    )
    record["candidate_query_paths"].append(candidate)

    retrieval_run = copy.deepcopy(record["retrieval_runs"][0])
    retrieval_run["retrieval_run_id"] = "retrieval_secondary"
    retrieval_run["branch_id"] = branch_id
    retrieval_run["plan"]["branch_id"] = branch_id
    record["retrieval_runs"].append(retrieval_run)

    hit = copy.deepcopy(record["retrieval_hits"][0])
    hit.update(
        {
            "branch_id": branch_id,
            "search_unit_id": search_unit_id,
            "document_id": document_id,
            "source_evidence_ids": [evidence_id],
        }
    )
    record["retrieval_hits"].append(hit)

    bundle = copy.deepcopy(record["retrieved_evidence_bundles"][0])
    bundle["query_branch_id"] = branch_id
    bundle["evidence_nodes"][0].update(
        {
            "evidence_id": evidence_id,
            "document_id": document_id,
            "search_unit_ids": [search_unit_id],
        }
    )
    record["retrieved_evidence_bundles"].append(bundle)

    evaluation = copy.deepcopy(record["candidate_evaluations"][0])
    evaluation.update(
        {
            "branch_id": branch_id,
            "status": "disqualified",
            "disqualifiers": ["explicit_conflict"],
            "evidence_ids": [evidence_id],
            "rationale": "The secondary branch conflicts with the explicit question.",
        }
    )
    for signal in evaluation["signals"]:
        signal["basis_refs"] = [evidence_id]
    record["candidate_evaluations"].append(evaluation)
    return evidence_id


def intermediate_records(
    *,
    relative_path: str = "share/project/source.txt",
    raw_text: str = "question_id final_answer are source text here",
) -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
    source_path = Path(relative_path)
    source = {
        "relative_path": relative_path,
        "file_name": source_path.name,
        "extension": source_path.suffix.removeprefix(".").casefold(),
        "sha256": "a" * 64,
        "size_bytes": 1,
    }
    document_id = intermediate_validator.stable_id(
        "doc",
        {"relative_path": relative_path, "source_sha256": source["sha256"]},
    )
    document = {
        "schema_version": "0.1",
        "record_type": "document",
        "document_id": document_id,
        "source": source,
        "extraction": {
            "status": "success",
            "parser": "fixture_parser",
            "parser_version": "0.1",
            "extracted_at": STAMP,
            "warnings": [],
            "errors": [],
        },
    }
    content = {
        "raw_text": raw_text,
        "normalized_text": intermediate_validator.normalize_text(raw_text),
        "value_type": "text",
        "sha256": intermediate_validator.digest_value({"raw_text": raw_text}),
        "is_truncated": False,
    }
    evidence_id = intermediate_validator.stable_id(
        "ev",
        {
            "document_id": document_id,
            "evidence_type": "paragraph",
            "location": {},
            "content_sha256": content["sha256"],
        },
    )
    evidence = {
        "schema_version": "0.1",
        "record_type": "evidence",
        "evidence_id": evidence_id,
        "document_id": document_id,
        "evidence_type": "paragraph",
        "location": {},
        "content": content,
        "native_properties": {},
        "annotations": [],
        "provenance": {
            "extraction_method": "fixture",
            "extractor": "fixture_parser",
            "extractor_version": "0.1",
            "extracted_at": STAMP,
            "deterministic": True,
            "confidence": 1.0,
            "warnings": [],
        },
    }
    from_ref = {"record_type": "document", "record_id": document_id}
    to_ref = {"record_type": "evidence", "record_id": evidence_id}
    provenance = {
        "generated_by": "fixture",
        "generator_version": "0.1",
        "generated_at": STAMP,
        "deterministic": True,
        "confidence": 1.0,
    }
    relation_id = intermediate_validator.stable_id(
        "rel",
        {
            "class": "structural",
            "type": "contains",
            "from": from_ref,
            "to": to_ref,
            "generator": provenance["generated_by"],
            "generator_version": provenance["generator_version"],
        },
    )
    relation = {
        "schema_version": "0.1",
        "record_type": "relation",
        "relation_id": relation_id,
        "relation_class": "structural",
        "relation_type": "contains",
        "from_ref": from_ref,
        "to_ref": to_ref,
        "properties": {},
        "supporting_evidence_ids": [evidence_id],
        "provenance": provenance,
        "status": "verified",
    }
    return document, evidence, relation


def write_intermediate(
    directory: Path,
    document: dict[str, object],
    evidence: dict[str, object],
    relation: dict[str, object],
) -> None:
    write_jsonl(directory / "documents.jsonl", [document])
    write_jsonl(directory / "evidence.jsonl", [evidence])
    write_jsonl(directory / "relations.jsonl", [relation])


class QuestionIntentContractTest(unittest.TestCase):
    def assert_invalid(self, record: object, message: str | None = None) -> None:
        errors = query_validator.validate_record(record)
        self.assertTrue(errors, "record unexpectedly passed validation")
        if message is not None:
            self.assertIn(message, "\n".join(errors))

    def test_valid_simple_contract(self) -> None:
        self.assertEqual(query_validator.validate_record(simple_contract()), [])

    def test_valid_compound_dag_contract(self) -> None:
        self.assertEqual(query_validator.validate_record(compound_contract()), [])

    def test_unknown_root_property_is_rejected(self) -> None:
        record = simple_contract()
        record["query_context_graph"] = {}
        self.assert_invalid(record, "Additional properties are not allowed")

    def test_unknown_enum_is_rejected(self) -> None:
        record = simple_contract()
        record["requested"]["scope"]["source"] = "model_guess"
        self.assert_invalid(record, "is not one of")

    def test_non_finite_number_is_rejected(self) -> None:
        record = compound_contract()
        record["requested"]["scope"]["filters"][1]["value"] = float("nan")
        self.assert_invalid(record, "non-finite")

    def test_duplicate_operation_id_is_rejected(self) -> None:
        record = simple_contract()
        duplicate = copy.deepcopy(record["requested"]["operation_graph"]["nodes"][0])
        duplicate["output_ref"] = "other_values"
        record["requested"]["operation_graph"]["nodes"].append(duplicate)
        self.assert_invalid(record, "duplicate operation_id")

    def test_dangling_operation_reference_is_rejected(self) -> None:
        record = simple_contract()
        record["requested"]["operation_graph"]["nodes"][0]["input_refs"] = ["missing_set"]
        self.assert_invalid(record, "dangling input_ref")

    def test_cycle_is_rejected(self) -> None:
        record = compound_contract()
        graph = record["requested"]["operation_graph"]
        graph["nodes"][0]["input_refs"] = ["nearest_ids"]
        graph["edges"].append({"from": "op_project_id", "to": "op_filter_category"})
        self.assert_invalid(record, "must be acyclic")

    def test_edge_and_input_dependency_mismatch_is_rejected(self) -> None:
        record = compound_contract()
        record["requested"]["operation_graph"]["edges"].pop()
        self.assert_invalid(record, "edges missing input dependencies")

    def test_explicit_scope_predicate_must_match_filter_operation(self) -> None:
        record = compound_contract()
        record["requested"]["operation_graph"]["nodes"][1]["predicate"]["operator"] = "gte"
        self.assert_invalid(record)

    def test_dangling_requested_output_reference_is_rejected(self) -> None:
        record = simple_contract()
        record["requested"]["requested_outputs"][0]["source_operation_ref"] = "op_missing"
        self.assert_invalid(record, "dangling source_operation_ref")

    def test_inconsistent_derived_summary_is_rejected(self) -> None:
        record = simple_contract()
        record["requested"]["derived_summary"]["operation"] = "retrieve"
        self.assert_invalid(record, "derived_summary.operation is inconsistent")

    def test_missing_required_forbidden_validator_is_rejected(self) -> None:
        record = simple_contract()
        record["forbidden"]["global"].pop()
        self.assert_invalid(record, "lacks required validator coverage")

    def test_unknown_forbidden_validator_is_rejected(self) -> None:
        record = simple_contract()
        record["forbidden"]["global"][0]["check"]["validator_id"] = "unknown_validator"
        self.assert_invalid(record, "unknown validator_id")

    def test_forbidden_validator_params_are_closed(self) -> None:
        record = simple_contract()
        record["forbidden"]["global"][0]["check"]["params"] = {"free_form": True}
        self.assert_invalid(record, "validator params must be an empty object")

    def test_answer_source_reference_is_rejected(self) -> None:
        record = simple_contract()
        external = record["requested"]["operation_graph"]["external_inputs"][0]
        external["source_ref"] = "share/質問回答/questions_valid.csv"
        self.assert_invalid(record, "forbidden source_ref")


class QueryRunContractTest(unittest.TestCase):
    def assert_invalid(self, record: object, message: str | None = None) -> None:
        errors = query_validator.validate_record(record)
        self.assertTrue(errors, "record unexpectedly passed validation")
        if message is not None:
            self.assertIn(message, "\n".join(errors))

    def test_valid_accepted_query_run(self) -> None:
        self.assertEqual(query_validator.validate_record(accepted_query_run()), [])

    def test_valid_equivalent_answer_branch_set(self) -> None:
        record = accepted_query_run()
        add_secondary_branch(record)
        equivalence_class_id = "equivalence_same_answer"
        record["candidate_query_paths"][1]["result"] = "resolved"
        record["candidate_evaluations"][0].update(
            {
                "status": "equivalent_for_answer",
                "equivalence_class_id": equivalence_class_id,
            }
        )
        record["candidate_evaluations"][1].update(
            {
                "status": "equivalent_for_answer",
                "disqualifiers": [],
                "equivalence_class_id": equivalence_class_id,
                "rationale": "This branch produces the same required answer.",
            }
        )
        record["primary_query_path"]["equivalent_branch_ids"] = [
            "branch_secondary"
        ]
        required_qualifier = "Either equivalent branch yields the same required output."
        record["primary_query_path"]["required_qualifiers"] = [required_qualifier]
        record["answer_plan"]["required_qualifiers"] = [required_qualifier]
        populate_forbidden_check_results(record)
        self.assertEqual(query_validator.validate_record(record), [])

    def test_valid_abstained_query_run(self) -> None:
        self.assertEqual(query_validator.validate_record(abstained_query_run()), [])

    def test_run_and_embedded_contract_question_must_match(self) -> None:
        for field, value in (
            ("question_id", "q_other"),
            ("original_question", "A different question"),
        ):
            record = accepted_query_run()
            record[field] = value
            with self.subTest(field=field):
                self.assert_invalid(record, "does not match question_intent_contract")

    def test_accepted_status_requires_passing_gate_proof_and_output(self) -> None:
        mutations = {
            "gate": lambda record: record["answerability_gate"].update(
                {
                    "status": "fail",
                    "reason_codes": ["no_supporting_evidence"],
                    "action": "abstain",
                }
            ),
            "proof": lambda record: record["proof_obligation"]["overall"].update(
                {"status": "unsatisfied"}
            ),
            "output": lambda record: record["output_validation"].update(
                {"status": "fail", "action": "abstain"}
            ),
        }
        for name, mutate in mutations.items():
            record = accepted_query_run()
            mutate(record)
            with self.subTest(inconsistency=name):
                self.assert_invalid(record)

    def test_proof_must_reference_the_contract_operation_graph(self) -> None:
        record = accepted_query_run()
        record["proof_obligation"]["operation_graph_ref"] = "graph_missing"
        self.assert_invalid(record, "operation_graph_ref")

    def test_abstained_status_cannot_keep_an_answerable_path(self) -> None:
        record = accepted_query_run()
        record["final_status"] = "abstained"
        record["final_answer"] = "わかりません"
        self.assert_invalid(record)

    def test_claim_evidence_rule_and_branch_references_must_resolve(self) -> None:
        mutations = {
            "claim": lambda record: record["answer_plan"]["output_plans"][0].update(
                {"allowed_claim_ids": ["claim_missing"]}
            ),
            "evidence": lambda record: record["answer_plan"]["allowed_claims"][0].update(
                {"evidence_ids": ["ev_" + "9" * 32]}
            ),
            "rule": lambda record: record["answer_plan"].update(
                {"forbidden_rule_ids": ["rule_missing"]}
            ),
            "branch": lambda record: record["retrieval_runs"][0].update(
                {"branch_id": "branch_missing"}
            ),
        }
        for name, mutate in mutations.items():
            record = accepted_query_run()
            mutate(record)
            with self.subTest(reference=name):
                self.assert_invalid(record, "dangling")

    def test_forbidden_check_rule_reference_must_resolve(self) -> None:
        record = accepted_query_run()
        record["forbidden_check_results"][0]["rule_id"] = "rule_missing"
        self.assert_invalid(record, "dangling")

    def test_forbidden_check_validator_version_is_fixed(self) -> None:
        record = accepted_query_run()
        record["forbidden_check_results"][0]["validator_version"] = "9.9"
        self.assert_invalid(record, "validator_version")

    def test_forbidden_check_subject_references_must_resolve(self) -> None:
        record = accepted_query_run()
        record["forbidden_check_results"][0]["subject_refs"] = ["branch_missing"]
        self.assert_invalid(record, "subject_refs has dangling references")

    def test_claim_cannot_use_evidence_seen_only_in_a_raw_retrieval_hit(self) -> None:
        record = accepted_query_run()
        raw_hit_evidence_id = "ev_" + "a" * 32
        record["retrieval_hits"][0]["source_evidence_ids"].append(raw_hit_evidence_id)
        record["answer_plan"]["allowed_claims"][0]["evidence_ids"] = [
            raw_hit_evidence_id
        ]
        self.assert_invalid(record)

    def test_every_requested_output_requires_a_matching_proof(self) -> None:
        record = accepted_query_run()
        record["proof_obligation"]["requirements"][0]["output_ref"] = "identifier_values"
        self.assert_invalid(record)

    def test_proof_operation_must_produce_its_requested_output(self) -> None:
        record = accepted_query_run()
        record["question_intent_contract"]["requested"]["operation_graph"]["nodes"].append(
            {
                "operation_id": "op_other",
                "operator": "retrieve",
                "input_refs": ["source_set"],
                "output_ref": "other_value",
            }
        )
        record["proof_obligation"]["requirements"][0]["operation_ref"] = "op_other"
        self.assert_invalid(record)

    def test_primary_path_requires_a_resolved_candidate_evaluation(self) -> None:
        record = accepted_query_run()
        record["candidate_evaluations"][0]["status"] = "ambiguous"
        self.assert_invalid(record)

    def test_completed_query_run_cannot_retain_a_pending_candidate_path(self) -> None:
        record = accepted_query_run()
        record["candidate_query_paths"][0]["status"] = "pending"
        self.assert_invalid(record)

    def test_primary_resolved_candidate_path_requires_evidence(self) -> None:
        record = accepted_query_run()
        self.assertEqual(record["candidate_query_paths"][0]["result"], "resolved")
        record["candidate_query_paths"][0]["evidence_ids"] = []
        self.assert_invalid(record)

    def test_output_validation_pass_rejects_a_failed_individual_check(self) -> None:
        record = accepted_query_run()
        record["output_validation"]["checks"]["operation_match"] = "fail"
        self.assert_invalid(record)

    def test_accepted_claim_must_not_be_unresolved(self) -> None:
        record = accepted_query_run()
        record["answer_plan"]["allowed_claims"][0]["exactness"] = "unresolved"
        self.assert_invalid(record)

    def test_answer_plan_shape_must_match_requested_output(self) -> None:
        record = accepted_query_run()
        record["answer_plan"]["output_plans"][0]["answer_shape"]["container"] = "scalar"
        self.assert_invalid(record)

    def test_list_all_requires_exhaustive_evidence_backed_proof(self) -> None:
        def no_coverage(record: dict[str, object]) -> None:
            record["proof_obligation"]["coverage"]["method"] = "none"

        def not_exhaustive(record: dict[str, object]) -> None:
            record["proof_obligation"]["coverage"]["exhaustive"] = False

        def proof_without_evidence(record: dict[str, object]) -> None:
            record["proof_obligation"]["requirements"][0]["evidence_ids"] = []

        for name, mutate in (
            ("coverage_none", no_coverage),
            ("not_exhaustive", not_exhaustive),
            ("proof_without_evidence", proof_without_evidence),
        ):
            record = accepted_query_run()
            mutate(record)
            with self.subTest(case=name):
                self.assert_invalid(record)

    def test_count_accepts_an_evidence_backed_authoritative_aggregate(self) -> None:
        record = accepted_count_query_run()
        coverage = record["proof_obligation"]["coverage"]
        self.assertEqual(coverage["method"], "authoritative_aggregate")
        self.assertIsNone(coverage["scanned_count"])
        self.assertEqual(coverage["matched_count"], 3)
        self.assertFalse(coverage["exhaustive"])
        self.assertTrue(coverage["evidence_ids"])
        self.assertEqual(query_validator.validate_record(record), [])

    def test_candidate_intent_cannot_rewrite_scope_operator_or_output(self) -> None:
        mutations = {
            "scope": lambda intent: intent["scope"].update(
                {"container": "different-source.csv"}
            ),
            "operator": lambda intent: intent["operation_graph"]["nodes"][0].update(
                {"operator": "retrieve"}
            ),
            "output": lambda intent: intent["requested_outputs"][0].update(
                {"return_field": "name"}
            ),
        }
        for name, mutate in mutations.items():
            record = accepted_query_run()
            mutate(record["candidate_query_paths"][0]["candidate_intent"])
            with self.subTest(field=name):
                self.assert_invalid(record)

    def test_primary_path_cannot_use_another_branch_evidence_bundle(self) -> None:
        record = accepted_query_run()
        secondary_evidence_id = add_secondary_branch(record)
        record["primary_query_path"]["evidence_ids"] = [secondary_evidence_id]
        self.assert_invalid(record)

    def test_estimated_evidence_cannot_support_an_exact_claim(self) -> None:
        record = accepted_query_run()
        record["retrieved_evidence_bundles"][0]["evidence_nodes"][0][
            "exactness"
        ] = "estimated"
        self.assert_invalid(record)

    def test_accepted_answer_plan_must_include_every_forbidden_rule(self) -> None:
        record = accepted_query_run()
        record["answer_plan"]["forbidden_rule_ids"].pop()
        self.assert_invalid(record)

    def test_accepted_answer_plan_rejects_unused_or_empty_claim_bindings(self) -> None:
        unused = accepted_query_run()
        extra_claim = copy.deepcopy(unused["answer_plan"]["allowed_claims"][0])
        extra_claim["claim_id"] = "claim_unused"
        unused["answer_plan"]["allowed_claims"].append(extra_claim)
        self.assert_invalid(unused)

        empty = accepted_query_run()
        empty["answer_plan"]["output_plans"][0]["allowed_claim_ids"] = []
        self.assert_invalid(empty)

    def test_claim_unit_must_match_the_requested_nonnull_unit(self) -> None:
        record = accepted_query_run()
        requested_output = record["question_intent_contract"]["requested"][
            "requested_outputs"
        ][0]
        requested_output["answer_shape"]["unit"] = "件"
        record["candidate_query_paths"][0]["candidate_intent"] = copy.deepcopy(
            record["question_intent_contract"]["requested"]
        )
        record["answer_plan"]["output_plans"][0]["answer_shape"]["unit"] = "件"
        record["answer_plan"]["allowed_claims"][0]["unit"] = "%"
        self.assert_invalid(record)

    def test_bundle_evidence_must_match_same_branch_hit_provenance(self) -> None:
        mutations = {
            "unknown_evidence": lambda node: node.update(
                {"evidence_id": "ev_" + "f" * 32}
            ),
            "document_mismatch": lambda node: node.update(
                {"document_id": "doc_" + "f" * 32}
            ),
        }
        for name, mutate in mutations.items():
            record = accepted_query_run()
            node = record["retrieved_evidence_bundles"][0]["evidence_nodes"][0]
            mutate(node)
            with self.subTest(case=name):
                self.assert_invalid(record)

    def test_bundle_provenance_fields_must_come_from_the_same_retrieval_hit(self) -> None:
        record = accepted_query_run()
        second_hit = copy.deepcopy(record["retrieval_hits"][0])
        second_hit.update(
            {
                "rank": 2,
                "search_unit_id": "su_" + "a" * 32,
                "document_id": "doc_" + "b" * 32,
                "source_evidence_ids": ["ev_" + "c" * 32],
                "locator_text": "row 2",
                "evidence_text": "id_2",
            }
        )
        record["retrieval_hits"].append(second_hit)
        record["retrieved_evidence_bundles"][0]["evidence_nodes"][0][
            "search_unit_ids"
        ] = [second_hit["search_unit_id"]]
        self.assert_invalid(record)

    def test_bundle_discovery_channel_must_be_backed_by_its_retrieval_hit(self) -> None:
        record = accepted_query_run()
        record["retrieved_evidence_bundles"][0]["evidence_nodes"][0][
            "discovered_by"
        ] = ["semantic"]
        self.assert_invalid(record)

    def test_retrieval_plan_and_hits_must_match_the_branch_contract(self) -> None:
        def target_mismatch(record: dict[str, object]) -> None:
            record["retrieval_runs"][0]["plan"]["target_types"] = ["other"]

        def return_field_mismatch(record: dict[str, object]) -> None:
            record["retrieval_runs"][0]["plan"]["return_fields"] = ["name"]

        def scope_mismatch(record: dict[str, object]) -> None:
            record["retrieval_runs"][0]["plan"]["scope_filters"] = [
                {"field": "unrequested", "operator": "eq", "value": True}
            ]

        def channel_mismatch(record: dict[str, object]) -> None:
            record["retrieval_hits"][0]["channel"] = "lexical"

        for name, mutate in (
            ("target", target_mismatch),
            ("return_field", return_field_mismatch),
            ("scope", scope_mismatch),
            ("channel", channel_mismatch),
        ):
            record = accepted_query_run()
            mutate(record)
            with self.subTest(case=name):
                self.assert_invalid(record)

    def test_primary_path_requires_a_successful_undisqualified_branch(self) -> None:
        failed = accepted_query_run()
        failed["candidate_query_paths"][0]["status"] = "failed"
        failed["candidate_query_paths"][0]["error"] = "branch failed"
        self.assert_invalid(failed)

        disqualified = accepted_query_run()
        disqualified["candidate_evaluations"][0]["disqualifiers"] = ["type_mismatch"]
        self.assert_invalid(disqualified)

    def test_every_branch_requires_evaluation_and_no_ambiguity_may_remain(self) -> None:
        missing = accepted_query_run()
        missing["candidate_evaluations"] = []
        self.assert_invalid(missing)

        ambiguous = accepted_query_run()
        add_secondary_branch(ambiguous)
        secondary = ambiguous["candidate_evaluations"][1]
        secondary.update(
            {
                "status": "ambiguous",
                "disqualifiers": [],
                "rationale": "The secondary interpretation remains plausible.",
            }
        )
        self.assert_invalid(ambiguous)

    def test_acceptance_requires_one_resolved_or_equivalent_branch_set(self) -> None:
        two_resolved = accepted_query_run()
        add_secondary_branch(two_resolved)
        two_resolved["candidate_query_paths"][1]["result"] = "resolved"
        two_resolved["candidate_evaluations"][1].update(
            {
                "status": "resolved",
                "disqualifiers": [],
                "rationale": "A second independently resolved interpretation remains.",
            }
        )

        disqualified_equivalent = accepted_query_run()
        add_secondary_branch(disqualified_equivalent)
        disqualified_equivalent["primary_query_path"]["equivalent_branch_ids"] = [
            "branch_secondary"
        ]
        self.assert_invalid(disqualified_equivalent)

        self_equivalent = accepted_query_run()
        self_equivalent["primary_query_path"]["equivalent_branch_ids"] = [
            "branch_main"
        ]
        for name, record in (
            ("two_resolved", two_resolved),
            ("disqualified_equivalent", disqualified_equivalent),
            ("self_equivalent", self_equivalent),
        ):
            with self.subTest(case=name):
                self.assert_invalid(record)

    def test_primary_evaluation_requires_grounded_resolution_signals(self) -> None:
        empty = accepted_query_run()
        empty["candidate_evaluations"][0]["evidence_ids"] = []
        empty["candidate_evaluations"][0]["signals"] = []

        dangling_basis = accepted_query_run()
        dangling_basis["candidate_evaluations"][0]["signals"][0]["basis_refs"] = [
            "missing_ref"
        ]
        for name, record in (
            ("empty", empty),
            ("dangling_basis", dangling_basis),
        ):
            with self.subTest(case=name):
                self.assert_invalid(record)

    def test_answer_with_qualification_ambiguity_must_bind_a_qualifier(self) -> None:
        record = accepted_query_run()
        record["question_intent_contract"]["ambiguity"].append(
            {
                "field": "scope",
                "issue": "The question leaves two medium-impact scopes possible.",
                "candidates": [
                    {"value": "scope_a", "confidence": "medium", "basis": "question"},
                    {"value": "scope_b", "confidence": "medium", "basis": "question"},
                ],
                "impact": "medium",
                "resolution": ["answer_with_qualification"],
            }
        )
        self.assert_invalid(record)

    def test_answer_plan_must_inherit_primary_required_qualifiers_exactly(self) -> None:
        missing = accepted_query_run()
        missing["primary_query_path"]["required_qualifiers"] = [
            "source.csvの明示範囲に限定"
        ]

        extra = accepted_query_run()
        extra["answer_plan"]["required_qualifiers"] = ["未要求の限定"]
        for name, record in (("missing", missing), ("extra", extra)):
            with self.subTest(case=name):
                self.assert_invalid(record)

    def test_forbidden_subjects_cannot_all_collapse_to_the_contract_id(self) -> None:
        record = accepted_query_run()
        contract_id = record["question_intent_contract"]["question_intent_contract_id"]
        for result in record["forbidden_check_results"]:
            result["subject_refs"] = [contract_id]
        self.assert_invalid(record)

    def test_forbidden_results_require_validator_specific_subject_and_evidence(self) -> None:
        generic_subjects = {
            "intent": ["graph_main"],
            "retrieval": ["branch_main"],
            "generation": ["claim_identifiers"],
            "validation": ["qr_" + "6" * 32],
        }
        record = accepted_query_run()
        for result in record["forbidden_check_results"]:
            result["subject_refs"] = generic_subjects[result["stage"]]
            result["evidence_ids"] = []

        evidence_validator = accepted_query_run()
        result = next(
            result
            for result in evidence_validator["forbidden_check_results"]
            if result["validator_id"] == "estimated_not_exact"
            and result["stage"] == "retrieval"
        )
        result["subject_refs"] = ["branch_main"]
        result["evidence_ids"] = []
        for name, invalid_record in (
            ("all_generic", record),
            ("evidence_validator", evidence_validator),
        ):
            with self.subTest(case=name):
                self.assert_invalid(invalid_record)

    def test_list_all_matched_count_must_equal_the_claim_list_length(self) -> None:
        record = accepted_query_run()
        record["proof_obligation"]["coverage"]["matched_count"] = 2
        self.assert_invalid(record)

    def test_identifier_all_or_multiple_claim_lists_cannot_contain_duplicates(
        self,
    ) -> None:
        for cardinality in ("all", "multiple"):
            record = accepted_query_run()
            requested = record["question_intent_contract"]["requested"]
            requested["requested_outputs"][0]["cardinality"] = {
                "mode": cardinality,
                "expected_count": None,
            }
            requested["derived_summary"]["cardinality"] = cardinality
            record["candidate_query_paths"][0]["candidate_intent"] = copy.deepcopy(
                requested
            )
            record["answer_plan"]["allowed_claims"][0]["value"] = [
                "id_1",
                "id_1",
            ]
            record["proof_obligation"]["coverage"].update(
                {"scanned_count": 2, "matched_count": 2}
            )
            record["final_answer"] = "id_1, id_1"
            populate_forbidden_check_results(record)
            with self.subTest(cardinality=cardinality):
                self.assert_invalid(record)

    def test_non_identifier_list_may_preserve_duplicate_values(self) -> None:
        record = accepted_query_run()
        contract = record["question_intent_contract"]
        record["question_id"] = contract["question_id"] = (
            "q_fixture_duplicate_values"
        )
        record["original_question"] = contract["original_question"] = (
            "重複を含めて値をすべて列挙してください。"
        )
        record["candidate_query_paths"][0]["parent_question_id"] = record[
            "question_id"
        ]
        requested = contract["requested"]
        requested["target"]["surface"] = "値"
        requested_output = requested["requested_outputs"][0]
        requested_output["return_field"] = "value"
        requested_output["answer_shape"]["value_type"] = "string"
        requested["derived_summary"]["return_fields"] = ["value"]
        record["candidate_query_paths"][0]["candidate_intent"] = copy.deepcopy(
            requested
        )
        record["retrieval_runs"][0]["plan"]["return_fields"] = ["value"]
        record["answer_plan"]["output_plans"][0]["answer_shape"] = copy.deepcopy(
            requested_output["answer_shape"]
        )
        record["answer_plan"]["allowed_claims"][0]["value"] = ["same", "same"]
        record["proof_obligation"]["coverage"].update(
            {"scanned_count": 2, "matched_count": 2}
        )
        record["retrieval_hits"][0]["evidence_text"] = "same, same"
        record["final_answer"] = "same, same"
        populate_forbidden_check_results(record)
        self.assertEqual(query_validator.validate_record(record), [])

    def test_retrieve_parallel_ambiguity_must_be_resolved_before_acceptance(self) -> None:
        record = accepted_query_run()
        record["question_intent_contract"]["ambiguity"].append(
            {
                "field": "target",
                "issue": "Two target interpretations require parallel retrieval.",
                "candidates": [
                    {"value": "target_a", "confidence": "medium", "basis": "question"},
                    {"value": "target_b", "confidence": "medium", "basis": "question"},
                ],
                "impact": "medium",
                "resolution": ["retrieve_parallel"],
            }
        )
        self.assert_invalid(record)

    def test_accepted_runtime_requires_index_identity_and_monotonic_time(self) -> None:
        no_index = accepted_query_run()
        no_index["runtime_metadata"]["indexes"] = []
        self.assert_invalid(no_index)

        reversed_time = accepted_query_run()
        reversed_time["runtime_metadata"]["started_at"] = "2026-08-16T00:00:01+00:00"
        reversed_time["runtime_metadata"]["completed_at"] = "2026-08-16T00:00:00+00:00"
        self.assert_invalid(reversed_time)

    def test_completed_retrieval_timestamps_must_be_complete_and_monotonic(self) -> None:
        def missing_start(record: dict[str, object]) -> None:
            record["retrieval_runs"][0]["started_at"] = None

        def reversed_time(record: dict[str, object]) -> None:
            record["retrieval_runs"][0]["started_at"] = (
                "2026-08-16T00:00:01+00:00"
            )

        def outside_runtime(record: dict[str, object]) -> None:
            record["retrieval_runs"][0]["started_at"] = (
                "2026-08-15T23:59:59+00:00"
            )
            record["retrieval_runs"][0]["completed_at"] = (
                "2026-08-15T23:59:59+00:00"
            )

        for name, mutate in (
            ("missing_start", missing_start),
            ("reversed", reversed_time),
            ("outside_runtime", outside_runtime),
        ):
            record = accepted_query_run()
            mutate(record)
            with self.subTest(case=name):
                self.assert_invalid(record)

    def test_passing_gates_require_the_registered_check_set(self) -> None:
        for gate_name, mutation in (
            ("intent_empty", lambda checks: checks.clear()),
            ("intent_incomplete", lambda checks: checks.pop()),
            (
                "intent_unknown",
                lambda checks: checks.append(
                    {"check_id": "unknown_check", "status": "pass", "detail": None}
                ),
            ),
            (
                "answerability_empty",
                lambda checks: checks.clear(),
            ),
            ("answerability_incomplete", lambda checks: checks.pop()),
        ):
            record = accepted_query_run()
            target = (
                "answerability_gate"
                if gate_name.startswith("answerability")
                else "intent_gate"
            )
            mutation(record[target]["checks"])
            with self.subTest(case=gate_name):
                self.assert_invalid(record)

    def test_null_requested_unit_cannot_gain_a_claim_unit(self) -> None:
        record = accepted_query_run()
        requested_unit = record["question_intent_contract"]["requested"][
            "requested_outputs"
        ][0]["answer_shape"]["unit"]
        self.assertIsNone(requested_unit)
        record["answer_plan"]["allowed_claims"][0]["unit"] = "kg"
        self.assert_invalid(record)

    def test_structured_list_output_requires_deterministic_answer_mode(self) -> None:
        record = accepted_query_run()
        record["answer_plan"]["output_plans"][0][
            "answer_mode"
        ] = "grounded_generation"
        self.assert_invalid(record)

    def test_claim_value_must_match_the_requested_value_type(self) -> None:
        record = accepted_query_run()
        record["answer_plan"]["allowed_claims"][0]["value"] = [123]
        self.assert_invalid(record)

    def test_failed_stage_cannot_be_followed_by_completed_stages(self) -> None:
        record = accepted_query_run()
        record["final_status"] = "failed"
        record["final_answer"] = None
        record["stage_statuses"]["retrieval"] = "failed"
        record["answerability_gate"].update(
            {
                "status": "fail",
                "reason_codes": ["no_supporting_evidence"],
                "action": "abstain",
            }
        )
        record["output_validation"].update({"status": "fail", "action": "abstain"})
        record["errors"] = [
            {"stage": "retrieval", "code": "retrieval_failed", "message": "failed"}
        ]
        self.assert_invalid(record)

    def test_skipped_stage_cannot_retain_its_artifact(self) -> None:
        record = abstained_query_run()
        record["answer_plan"] = copy.deepcopy(accepted_query_run()["answer_plan"])
        self.assert_invalid(record)

    def test_query_run_rule_version_must_match_embedded_contract(self) -> None:
        record = accepted_query_run()
        record["runtime_metadata"]["rule_version"] = "v9.9"
        self.assert_invalid(record)

    def test_required_validator_cannot_move_to_a_weaker_stage(self) -> None:
        record = accepted_query_run()
        rule = next(
            rule
            for rule in record["question_intent_contract"]["forbidden"]["evidence"]
            if rule["check"]["validator_id"] == "estimated_not_exact"
        )
        rule["applies_to"] = ["intent"]
        record["forbidden_check_results"] = [
            result
            for result in record["forbidden_check_results"]
            if result["rule_id"] != rule["rule_id"]
        ]
        record["forbidden_check_results"].append(
            {
                "rule_id": rule["rule_id"],
                "stage": "intent",
                "validator_id": "estimated_not_exact",
                "validator_version": "0.1",
                "status": "pass",
                "subject_refs": ["branch_main"],
                "evidence_ids": [],
                "details": {},
                "action_taken": "none",
            }
        )
        self.assert_invalid(record)

    def test_high_impact_abstain_ambiguity_blocks_acceptance(self) -> None:
        record = accepted_query_run()
        record["question_intent_contract"]["ambiguity"].append(
            {
                "field": "scope",
                "issue": "Two scopes remain possible.",
                "candidates": [
                    {"value": "scope_a", "confidence": "medium", "basis": "question"},
                    {"value": "scope_b", "confidence": "medium", "basis": "question"},
                ],
                "impact": "high",
                "resolution": ["abstain"],
            }
        )
        self.assert_invalid(record)

    def test_primary_evidence_must_be_admissible(self) -> None:
        def add_issue(record: dict[str, object], field: str) -> None:
            evidence_id = record["primary_query_path"]["evidence_ids"][0]
            record["retrieved_evidence_bundles"][0][field].append(
                {"evidence_ids": [evidence_id], "reason": "not admissible"}
            )

        mutations = {
            "rejected_role": lambda record: record["retrieved_evidence_bundles"][0][
                "evidence_nodes"
            ][0].update({"role": "rejected"}),
            "scope_mismatch": lambda record: record["retrieved_evidence_bundles"][0][
                "evidence_nodes"
            ][0].update({"scope_match": "mismatched"}),
            "target_unknown": lambda record: record["retrieved_evidence_bundles"][0][
                "evidence_nodes"
            ][0].update({"target_match": "unknown"}),
            "estimated_for_exact_claim": lambda record: record[
                "retrieved_evidence_bundles"
            ][0]["evidence_nodes"][0].update({"exactness": "estimated"}),
            "conflict": lambda record: add_issue(record, "conflicts"),
            "rejected_evidence": lambda record: add_issue(record, "rejected_evidence"),
        }
        for name, mutate in mutations.items():
            record = accepted_query_run()
            mutate(record)
            with self.subTest(case=name):
                self.assert_invalid(record)

    def test_accepted_contract_cannot_leave_required_semantics_unknown(self) -> None:
        def unknown_return_field(record: dict[str, object]) -> None:
            output = record["question_intent_contract"]["requested"]["requested_outputs"][0]
            output["return_field"] = "unknown"
            record["question_intent_contract"]["requested"]["derived_summary"][
                "return_fields"
            ] = ["unknown"]

        def unknown_cardinality(record: dict[str, object]) -> None:
            output = record["question_intent_contract"]["requested"]["requested_outputs"][0]
            output["cardinality"] = {"mode": "unknown", "expected_count": None}
            record["question_intent_contract"]["requested"]["derived_summary"][
                "cardinality"
            ] = "unknown"

        def unknown_shape(record: dict[str, object]) -> None:
            output = record["question_intent_contract"]["requested"]["requested_outputs"][0]
            output["answer_shape"].update(
                {"container": "unknown", "value_type": "unknown"}
            )

        def unknown_operator(record: dict[str, object]) -> None:
            requested = record["question_intent_contract"]["requested"]
            requested["operation_graph"]["nodes"][0]["operator"] = "unknown"
            requested["derived_summary"]["operation"] = "unknown"

        for name, mutate in (
            ("return_field", unknown_return_field),
            ("cardinality", unknown_cardinality),
            ("answer_shape", unknown_shape),
            ("operator", unknown_operator),
        ):
            record = accepted_query_run()
            mutate(record)
            requested = record["question_intent_contract"]["requested"]
            record["candidate_query_paths"][0]["candidate_intent"] = copy.deepcopy(
                requested
            )
            record["answer_plan"]["output_plans"][0]["answer_shape"] = copy.deepcopy(
                requested["requested_outputs"][0]["answer_shape"]
            )
            with self.subTest(field=name):
                self.assert_invalid(record)


class IntermediateBoundaryTest(unittest.TestCase):
    VALIDATORS = (
        intermediate_validator.validate,
        streaming_intermediate_validator.validate,
    )

    def _validate_with_each(
        self,
        document: dict[str, object],
        evidence: dict[str, object],
        relation: dict[str, object],
    ) -> list[dict[str, int]]:
        results = []
        with tempfile.TemporaryDirectory(prefix="aiec-query-boundary-") as temporary:
            directory = Path(temporary)
            write_intermediate(directory, document, evidence, relation)
            for validate in self.VALIDATORS:
                with self.subTest(validator=validate.__module__):
                    results.append(validate(directory))
        return results

    def test_raw_source_text_may_contain_question_and_answer_words(self) -> None:
        records = intermediate_records(raw_text="question_intent_contract final_answer")
        results = self._validate_with_each(*records)
        self.assertEqual(
            results,
            [
                {"document": 1, "evidence": 1, "relation": 1},
                {"document": 1, "evidence": 1, "relation": 1},
            ],
        )

    def test_question_pair_in_evidence_metadata_is_rejected(self) -> None:
        def inject_native_pair(evidence: dict[str, object]) -> None:
            evidence["native_properties"] = {
                "question_id": "q_embedded",
                "original_question": "embedded query-layer question",
            }

        def inject_nested_column_pair(evidence: dict[str, object]) -> None:
            evidence["native_properties"] = {
                "columns": {
                    "question_id": "q_embedded",
                    "original_question": "embedded query-layer question",
                }
            }

        def inject_annotation_pair(evidence: dict[str, object]) -> None:
            evidence["annotations"] = [
                {
                    "key": "question_id",
                    "value": "q_embedded",
                    "generated_by": "fixture",
                    "confidence": 1.0,
                },
                {
                    "key": "original_question",
                    "value": "embedded query-layer question",
                    "generated_by": "fixture",
                    "confidence": 1.0,
                },
            ]

        for label, mutate in (
            ("direct_same_container", inject_native_pair),
            ("nested_column_pair", inject_nested_column_pair),
            ("annotation_pair", inject_annotation_pair),
        ):
            document, evidence, relation = intermediate_records()
            mutate(evidence)
            with tempfile.TemporaryDirectory(
                prefix="aiec-question-pair-boundary-"
            ) as temporary:
                directory = Path(temporary)
                write_intermediate(directory, document, evidence, relation)
                for validate in self.VALIDATORS:
                    with self.subTest(label=label, validator=validate.__module__):
                        with self.assertRaisesRegex(
                            ValueError,
                            "question-layer data is forbidden",
                        ):
                            validate(directory)

        for field in ("question_id", "original_question"):
            for container in ("native_properties", "annotations"):
                document, evidence, relation = intermediate_records()
                if container == "native_properties":
                    evidence[container] = {field: "single query-layer field"}
                else:
                    evidence[container] = [
                        {
                            "key": field,
                            "value": "single query-layer field",
                            "generated_by": "fixture",
                            "confidence": 1.0,
                        }
                    ]
                with tempfile.TemporaryDirectory(
                    prefix="aiec-single-question-field-boundary-"
                ) as temporary:
                    directory = Path(temporary)
                    write_intermediate(directory, document, evidence, relation)
                    for validate in self.VALIDATORS:
                        with self.subTest(
                            field=field,
                            container=container,
                            validator=validate.__module__,
                        ):
                            with self.assertRaisesRegex(
                                ValueError,
                                "question-layer data is forbidden",
                            ):
                                validate(directory)

    def test_question_pair_raw_content_and_single_column_names_are_allowed(
        self,
    ) -> None:
        raw_records = intermediate_records(
            raw_text=(
                '{"question_id":"source column value",'
                '"original_question":"literal source content"}'
            )
        )
        self.assertEqual(
            self._validate_with_each(*raw_records),
            [
                {"document": 1, "evidence": 1, "relation": 1},
                {"document": 1, "evidence": 1, "relation": 1},
            ],
        )

        document, evidence, relation = intermediate_records()
        raw_value = {
            "question_id": "source column value",
            "original_question": "literal source content",
        }
        content = {
            "raw_value": raw_value,
            "normalized_value": copy.deepcopy(raw_value),
            "value_type": "mixed",
            "sha256": intermediate_validator.digest_value({"raw_value": raw_value}),
            "is_truncated": False,
        }
        evidence["content"] = content
        evidence_id = intermediate_validator.stable_id(
            "ev",
            {
                "document_id": evidence["document_id"],
                "evidence_type": evidence["evidence_type"],
                "location": evidence["location"],
                "content_sha256": content["sha256"],
            },
        )
        evidence["evidence_id"] = evidence_id
        relation["to_ref"]["record_id"] = evidence_id
        relation["supporting_evidence_ids"] = [evidence_id]
        relation["relation_id"] = intermediate_validator.stable_id(
            "rel",
            {
                "class": relation["relation_class"],
                "type": relation["relation_type"],
                "from": relation["from_ref"],
                "to": relation["to_ref"],
                "generator": relation["provenance"]["generated_by"],
                "generator_version": relation["provenance"]["generator_version"],
            },
        )
        self.assertEqual(
            self._validate_with_each(document, evidence, relation),
            [
                {"document": 1, "evidence": 1, "relation": 1},
                {"document": 1, "evidence": 1, "relation": 1},
            ],
        )

        for column_name in ("question_id", "original_question"):
            with self.subTest(column_name=column_name):
                document, evidence, relation = intermediate_records()
                evidence["native_properties"] = {
                    "columns": {column_name: "literal source column value"}
                }
                self.assertEqual(
                    self._validate_with_each(document, evidence, relation),
                    [
                        {"document": 1, "evidence": 1, "relation": 1},
                        {"document": 1, "evidence": 1, "relation": 1},
                    ],
                )

    def test_root_query_layer_property_is_rejected(self) -> None:
        document, evidence, relation = intermediate_records()
        evidence["question_intent_contract"] = {}
        with tempfile.TemporaryDirectory(prefix="aiec-query-boundary-") as temporary:
            directory = Path(temporary)
            write_intermediate(directory, document, evidence, relation)
            for validate in self.VALIDATORS:
                with self.subTest(validator=validate.__module__):
                    with self.assertRaisesRegex(ValueError, "unexpected fields"):
                        validate(directory)

    def test_query_key_in_free_form_evidence_or_relation_data_is_rejected(self) -> None:
        for target, field in (("evidence", "native_properties"), ("relation", "properties")):
            document, evidence, relation = intermediate_records()
            selected = evidence if target == "evidence" else relation
            selected[field] = {"nested": {"question_intent_contract": {}}}
            with tempfile.TemporaryDirectory(prefix="aiec-query-boundary-") as temporary:
                directory = Path(temporary)
                write_intermediate(directory, document, evidence, relation)
                for validate in self.VALIDATORS:
                    with self.subTest(target=target, validator=validate.__module__):
                        with self.assertRaisesRegex(ValueError, "question-layer data is forbidden"):
                            validate(directory)

    def test_query_key_in_evidence_annotation_key_is_rejected(self) -> None:
        document, evidence, relation = intermediate_records()
        evidence["annotations"] = [
            {
                "key": "question_intent_contract",
                "value": "must not be embedded",
                "generated_by": "fixture",
                "confidence": 1.0,
            }
        ]
        with tempfile.TemporaryDirectory(prefix="aiec-query-boundary-") as temporary:
            directory = Path(temporary)
            write_intermediate(directory, document, evidence, relation)
            for validate in self.VALIDATORS:
                with self.subTest(validator=validate.__module__):
                    with self.assertRaisesRegex(ValueError, "question-layer data is forbidden"):
                        validate(directory)

    def test_query_artifact_in_evidence_style_native_properties_is_rejected(self) -> None:
        document, evidence, relation = intermediate_records()
        evidence["style"] = {
            "native_properties": {"nested": {"answer_plan": {}}}
        }
        with tempfile.TemporaryDirectory(prefix="aiec-query-boundary-") as temporary:
            directory = Path(temporary)
            write_intermediate(directory, document, evidence, relation)
            for validate in self.VALIDATORS:
                with self.subTest(validator=validate.__module__):
                    with self.assertRaisesRegex(ValueError, "question-layer data is forbidden"):
                        validate(directory)

    def test_direct_question_contract_signature_in_evidence_is_rejected(self) -> None:
        document, evidence, relation = intermediate_records()
        evidence["native_properties"] = {
            "schema_version": "0.1",
            "record_type": "question_intent_contract",
            "question_intent_contract_id": "qic_" + "e" * 32,
        }
        with tempfile.TemporaryDirectory(prefix="aiec-query-boundary-") as temporary:
            directory = Path(temporary)
            write_intermediate(directory, document, evidence, relation)
            for validate in self.VALIDATORS:
                with self.subTest(validator=validate.__module__):
                    with self.assertRaisesRegex(ValueError, "question-layer data is forbidden"):
                        validate(directory)

    def test_reserved_word_as_ordinary_native_value_is_allowed(self) -> None:
        document, evidence, relation = intermediate_records()
        evidence["native_properties"] = {"column_name": "final_answer"}
        results = self._validate_with_each(document, evidence, relation)
        self.assertEqual(
            results,
            [
                {"document": 1, "evidence": 1, "relation": 1},
                {"document": 1, "evidence": 1, "relation": 1},
            ],
        )

    def test_published_schema_is_enforced_in_both_intermediate_validators(self) -> None:
        document, evidence, relation = intermediate_records()
        evidence["style"] = {"unsupported_style_field": True}
        with tempfile.TemporaryDirectory(prefix="aiec-query-boundary-") as temporary:
            directory = Path(temporary)
            write_intermediate(directory, document, evidence, relation)
            for validate in self.VALIDATORS:
                with self.subTest(validator=validate.__module__):
                    with self.assertRaisesRegex(ValueError, "schema"):
                        validate(directory)

    def test_query_artifact_in_closed_metadata_containers_is_rejected(self) -> None:
        def inject_document(
            document: dict[str, object],
            evidence: dict[str, object],
            relation: dict[str, object],
        ) -> None:
            document["classification"] = {"question_intent_contract": {}}

        def inject_evidence_style(
            document: dict[str, object],
            evidence: dict[str, object],
            relation: dict[str, object],
        ) -> None:
            evidence["style"] = {"answer_plan": {}}

        def inject_relation_provenance(
            document: dict[str, object],
            evidence: dict[str, object],
            relation: dict[str, object],
        ) -> None:
            relation["provenance"]["answer_plan"] = {}

        for name, mutate in (
            ("document_classification", inject_document),
            ("evidence_style", inject_evidence_style),
            ("relation_provenance", inject_relation_provenance),
        ):
            records = intermediate_records()
            mutate(*records)
            with tempfile.TemporaryDirectory(
                prefix="aiec-query-boundary-"
            ) as temporary:
                directory = Path(temporary)
                write_intermediate(directory, *records)
                for validate in self.VALIDATORS:
                    with self.subTest(case=name, validator=validate.__module__):
                        with self.assertRaisesRegex(
                            ValueError,
                            "question-layer data is forbidden|schema",
                        ):
                            validate(directory)

    def test_legitimate_reserved_leaf_key_as_native_column_is_allowed(self) -> None:
        document, evidence, relation = intermediate_records()
        evidence["native_properties"] = {
            "columns": {"final_answer": "literal source column value"}
        }
        results = self._validate_with_each(document, evidence, relation)
        self.assertEqual(
            results,
            [
                {"document": 1, "evidence": 1, "relation": 1},
                {"document": 1, "evidence": 1, "relation": 1},
            ],
        )

    def test_duplicate_keys_and_non_finite_json_are_rejected_by_both_intermediate_validators(
        self,
    ) -> None:
        for case in ("duplicate_key", "non_finite"):
            document, evidence, relation = intermediate_records()
            with tempfile.TemporaryDirectory(
                prefix="aiec-query-boundary-"
            ) as temporary:
                directory = Path(temporary)
                write_intermediate(directory, document, evidence, relation)
                evidence_path = directory / "evidence.jsonl"
                if case == "duplicate_key":
                    payload = json.dumps(evidence, ensure_ascii=False)
                    key = '"record_type": "evidence"'
                    self.assertEqual(payload.count(key), 1)
                    payload = payload.replace(key, f"{key}, {key}", 1)
                    evidence_path.write_text(payload + "\n", encoding="utf-8")
                    expected_error = "duplicate"
                else:
                    evidence["native_properties"] = {"score": float("nan")}
                    write_jsonl(evidence_path, [evidence])
                    expected_error = "non-finite"
                for validate in self.VALIDATORS:
                    with self.subTest(case=case, validator=validate.__module__):
                        with self.assertRaisesRegex(ValueError, expected_error):
                            validate(directory)

    def test_intermediate_strict_loaders_control_json_resource_failures(self) -> None:
        cases = {
            "depth": ("[" * 70 + "0" + "]" * 70, "nesting exceeds"),
            "recursion": ("[" * 2_000 + "0" + "]" * 2_000, "resource limit"),
            "overflow": ('{"overflow":1e999}', "non-finite"),
        }
        for label, (payload, expected_error) in cases.items():
            document, evidence, relation = intermediate_records()
            with tempfile.TemporaryDirectory(
                prefix="aiec-intermediate-strict-json-"
            ) as temporary:
                directory = Path(temporary)
                write_intermediate(directory, document, evidence, relation)
                (directory / "evidence.jsonl").write_text(
                    payload + "\n",
                    encoding="utf-8",
                )
                for validate in self.VALIDATORS:
                    with self.subTest(label=label, validator=validate.__module__):
                        with self.assertRaisesRegex(ValueError, expected_error):
                            validate(directory)

    def test_query_shaped_raw_value_is_treated_as_source_payload(self) -> None:
        document, evidence, relation = intermediate_records()
        raw_value = {
            "question_intent_contract": {
                "question_intent_contract_id": "qic_" + "e" * 32,
                "requested": {"target": "literal source payload"},
            },
            "answer_plan": {"allowed_claims": []},
        }
        content = {
            "raw_value": raw_value,
            "normalized_value": copy.deepcopy(raw_value),
            "value_type": "mixed",
            "sha256": intermediate_validator.digest_value({"raw_value": raw_value}),
            "is_truncated": False,
        }
        evidence["content"] = content
        evidence_id = intermediate_validator.stable_id(
            "ev",
            {
                "document_id": evidence["document_id"],
                "evidence_type": evidence["evidence_type"],
                "location": evidence["location"],
                "content_sha256": content["sha256"],
            },
        )
        evidence["evidence_id"] = evidence_id
        relation["to_ref"]["record_id"] = evidence_id
        relation["supporting_evidence_ids"] = [evidence_id]
        relation["relation_id"] = intermediate_validator.stable_id(
            "rel",
            {
                "class": relation["relation_class"],
                "type": relation["relation_type"],
                "from": relation["from_ref"],
                "to": relation["to_ref"],
                "generator": relation["provenance"]["generated_by"],
                "generator_version": relation["provenance"]["generator_version"],
            },
        )
        results = self._validate_with_each(document, evidence, relation)
        self.assertEqual(
            results,
            [
                {"document": 1, "evidence": 1, "relation": 1},
                {"document": 1, "evidence": 1, "relation": 1},
            ],
        )

    def test_answer_or_submission_source_is_rejected(self) -> None:
        document, evidence, relation = intermediate_records(
            relative_path="share/質問回答/questions_valid.csv"
        )
        with tempfile.TemporaryDirectory(prefix="aiec-query-boundary-") as temporary:
            directory = Path(temporary)
            write_intermediate(directory, document, evidence, relation)
            for validate in self.VALIDATORS:
                with self.subTest(validator=validate.__module__):
                    with self.assertRaisesRegex(ValueError, "source is forbidden"):
                        validate(directory)


class PublishedBoundarySchemaTest(unittest.TestCase):
    def test_question_records_are_not_evidence_or_relation_records(self) -> None:
        for record in (simple_contract(), accepted_query_run()):
            for schema_name in ("evidence.schema.json", "relation.schema.json"):
                schema = json.loads(
                    (REPOSITORY / "schemas" / schema_name).read_text(encoding="utf-8")
                )
                validator = jsonschema.Draft202012Validator(
                    schema, format_checker=jsonschema.FormatChecker()
                )
                with self.subTest(record_type=record["record_type"], schema=schema_name):
                    self.assertTrue(list(validator.iter_errors(record)))


class QueryGraphCliTest(unittest.TestCase):
    def test_cli_accepts_valid_jsonl_and_rejects_invalid_jsonl(self) -> None:
        with tempfile.TemporaryDirectory(prefix="aiec-query-cli-") as temporary:
            directory = Path(temporary)
            valid_path = directory / "valid.jsonl"
            write_jsonl(valid_path, [simple_contract(), accepted_query_run()])
            valid = subprocess.run(
                [sys.executable, str(SCRIPTS / "validate_query_graph_records.py"), str(valid_path)],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(valid.returncode, 0, valid.stdout + valid.stderr)
            result = json.loads(valid.stdout)
            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["records"], 2)
            self.assertEqual(
                result["counts_by_type"],
                {"query_run": 1, "question_intent_contract": 1},
            )

            invalid = simple_contract()
            invalid["unexpected"] = True
            invalid_path = directory / "invalid.jsonl"
            write_jsonl(invalid_path, [invalid])
            rejected = subprocess.run(
                [sys.executable, str(SCRIPTS / "validate_query_graph_records.py"), str(invalid_path)],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(rejected.returncode, 1, rejected.stdout + rejected.stderr)
            self.assertIn("ERROR: validation failed", rejected.stdout)

    def test_cli_rejects_duplicate_json_object_keys(self) -> None:
        with tempfile.TemporaryDirectory(prefix="aiec-query-cli-") as temporary:
            path = Path(temporary) / "duplicate.json"
            payload = json.dumps(simple_contract(), ensure_ascii=False)
            key = '"record_type": "question_intent_contract"'
            self.assertEqual(payload.count(key), 1)
            payload = payload.replace(key, f"{key}, {key}", 1)
            path.write_text(payload, encoding="utf-8")
            completed = subprocess.run(
                [sys.executable, str(SCRIPTS / "validate_query_graph_records.py"), str(path)],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(completed.returncode, 1, completed.stdout + completed.stderr)
            self.assertIn("duplicate", completed.stdout.casefold())

    def test_cli_strict_loader_controls_depth_recursion_and_overflow(self) -> None:
        cases = {
            "depth": ("[" * 70 + "0" + "]" * 70, "nesting exceeds"),
            "recursion": ("[" * 2_000 + "0" + "]" * 2_000, "resource limit"),
            "overflow": ('{"overflow":1e999}', "non-finite"),
        }
        with tempfile.TemporaryDirectory(prefix="aiec-query-cli-limits-") as temporary:
            directory = Path(temporary)
            for label, (payload, expected_error) in cases.items():
                with self.subTest(label=label):
                    path = directory / f"{label}.json"
                    path.write_text(payload, encoding="utf-8")
                    completed = subprocess.run(
                        [
                            sys.executable,
                            str(SCRIPTS / "validate_query_graph_records.py"),
                            str(path),
                        ],
                        check=False,
                        capture_output=True,
                        text=True,
                    )
                    combined = completed.stdout + completed.stderr
                    self.assertEqual(completed.returncode, 1, combined)
                    self.assertIn("ERROR:", completed.stdout)
                    self.assertIn(expected_error, combined.casefold())
                    self.assertNotIn("Traceback", combined)


if __name__ == "__main__":
    unittest.main()
