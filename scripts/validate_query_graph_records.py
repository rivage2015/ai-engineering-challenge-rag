#!/usr/bin/env python3
"""Validate question-intent and completed query-run JSON records."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import os
import re
import stat
import unicodedata
from collections import Counter
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable

from question_language_registry import (
    ALLOWED_OPERATION_OPTIONS,
    ALL_CARDINALITY_SURFACES as EXPLICIT_ALL_CARDINALITY_SURFACES,
    ALTERNATIVE_CONNECTORS,
    APPROXIMATE_PRECISION_KEYWORDS,
    CALCULATION_OPERATORS,
    CALCULATION_PRECISION_KEYWORDS,
    CANONICAL_TARGET_TYPE_LEXEMES,
    DIRECT_OPERATIONS,
    DISTANCE_KEYWORDS,
    EXACT_PRECISION_KEYWORDS,
    JAPANESE_DIGITS,
    LANGUAGE_REGISTRY_SHA256,
    MULTIPLE_CARDINALITY_SURFACES as EXPLICIT_MULTIPLE_CARDINALITY_SURFACES,
    OPERATION_KEYWORDS as QUESTION_OPERATION_KEYWORDS,
    OPERATION_OPTION_KEYS,
    OPERATOR_MENTION_MAP as EXPLICIT_OPERATOR_SURFACES,
    RAW_EXCLUSION_REVERSALS as _RAW_EXCLUSION_REVERSALS,
    RAW_REQUIRED_OPERATION_KEYWORDS,
    REGISTRY_VERSION,
    SINGLE_CARDINALITY_SURFACES as EXPLICIT_SINGLE_CARDINALITY_SURFACES,
    SORT_ORDER_KEYWORDS,
    SUPPORTED_FILTER_SUFFIXES,
    SUPPORTED_LANE_NEGATIVE_MARKERS as _SUPPORTED_LANE_NEGATIVE_MARKERS,
    SUPPORTED_METRIC_DESCRIPTOR_ALIASES as _METRIC_DESCRIPTOR_ALIASES,
)


REPOSITORY = Path(__file__).resolve().parents[1]
SCHEMA_PATHS = {
    "question_intent_contract": REPOSITORY / "schemas" / "question-intent-contract.schema.json",
    "question_understanding_run": REPOSITORY / "schemas" / "question-understanding-run.schema.json",
    "query_run": REPOSITORY / "schemas" / "query-run.schema.json",
}
MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_RECORDS = 100_000
MAX_JSON_DEPTH = 64
FORBIDDEN_SOURCE_NAMES = {
    "questions_valid.csv",
    "questions_test.csv",
    "predictions.csv",
    "submission.zip",
}
VALIDATOR_IDS_BY_CATEGORY = {
    "global": {
        "claims_supported_by_evidence",
        "unresolved_never_promoted",
        "causality_requires_source_relation",
        "evidence_is_read_only",
        "answer_sources_are_excluded",
    },
    "query": {
        "operator_preserved",
        "hard_scope_not_expanded",
        "output_contract_match",
    },
    "evidence": {
        "estimated_not_exact",
        "unit_requires_evidence",
        "compatible_evidence_only",
        "provenance_required",
    },
}
KNOWN_VALIDATOR_IDS = set().union(*VALIDATOR_IDS_BY_CATEGORY.values())
VALIDATOR_IMPLEMENTATION_VERSION = "0.1"
RULE_VERSION = "v0.2"
VALIDATOR_STAGES_BY_ID = {
    "claims_supported_by_evidence": {"generation", "validation"},
    "unresolved_never_promoted": {"generation", "validation"},
    "causality_requires_source_relation": {"generation", "validation"},
    "evidence_is_read_only": {"retrieval", "generation", "validation"},
    "answer_sources_are_excluded": {"retrieval", "validation"},
    "operator_preserved": {"intent", "validation"},
    "hard_scope_not_expanded": {"intent", "retrieval", "validation"},
    "output_contract_match": {"intent", "generation", "validation"},
    "estimated_not_exact": {"retrieval", "generation", "validation"},
    "unit_requires_evidence": {"retrieval", "generation", "validation"},
    "compatible_evidence_only": {"retrieval", "generation", "validation"},
    "provenance_required": {"retrieval", "generation", "validation"},
}
INTENT_GATE_CHECK_IDS = {
    "operation_graph_compilable",
    "target_resolved",
    "requested_outputs_resolved",
    "scope_resolved",
    "explicit_consistency",
    "pre_retrieval_type_safety",
    "forbidden_precheck",
    "ambiguity_branched",
}
INTENT_GATE_CHECK_ORDER = (
    "operation_graph_compilable",
    "target_resolved",
    "requested_outputs_resolved",
    "scope_resolved",
    "explicit_consistency",
    "pre_retrieval_type_safety",
    "forbidden_precheck",
    "ambiguity_branched",
)
ANSWERABILITY_GATE_CHECK_IDS = {
    "intent_resolved",
    "primary_path_resolved",
    "evidence_path_complete",
    "evidence_compatible",
    "proof_satisfied",
    "forbidden_clear",
}
QUERY_ONLY_VALIDATOR_IDS = {
    "operator_preserved",
    "hard_scope_not_expanded",
    "output_contract_match",
}
INDEX_KINDS_BY_RETRIEVAL_CHANNEL = {
    "lexical": {"lexical"},
    "semantic": {"semantic"},
    "hybrid": {"lexical", "semantic"},
    "relation_traversal": {"relation"},
    "structured": {"structured"},
}
STAGE_ORDER = (
    "intent",
    "context",
    "candidate_paths",
    "intent_gate",
    "retrieval",
    "candidate_evaluation",
    "proof",
    "answerability",
    "answer_planning",
    "generation",
    "output_validation",
)
QUESTION_UNDERSTANDING_STAGE_ORDER = (
    "decompose",
    "context",
    "candidate_paths",
    "intent_gate",
    "validation",
)
FIELD_PRIMARY_PATH = {
    "target": "/requested/target",
    "scope": "/requested/scope",
    "operation": "/requested/operation_graph",
    "return_field": "/requested/requested_outputs",
    "answer_shape": "/requested/requested_outputs",
}
CONTEXT_SOURCE_PRIORITY = {
    "question_explicit": 1,
    "conversation_explicit": 2,
    "source_local": 3,
    "source_metadata": 4,
    "semantic_candidate": 5,
}
EXPLICIT_CARDINALITY_SURFACES = (
    EXPLICIT_ALL_CARDINALITY_SURFACES
    | EXPLICIT_MULTIPLE_CARDINALITY_SURFACES
    | EXPLICIT_SINGLE_CARDINALITY_SURFACES
)
EXPLICIT_KIND_SURFACE_OVERLAP = (
    EXPLICIT_CARDINALITY_SURFACES & set(EXPLICIT_OPERATOR_SURFACES)
)
_PREDICATE_VALUE_ALTERNATIVE_CONNECTORS = ALTERNATIVE_CONNECTORS[:3]
RAW_RELATION_PATTERN = re.compile(
    r"(?:が|は)[^、。\n]{1,96}?"
    r"(?:に一致|であり|より大きい|超える|以上|"
    r"より小さい|未満|以下|等しくない|を含む|"
    r"から始まる|で終わる|の間|範囲内|[=!<>]=?)",
    flags=re.IGNORECASE,
)
RAW_FILE_PATTERN = re.compile(
    r"(?<![\w./-])"
    r"[A-Za-z0-9_./-]+\.(?:csv|tsv|xlsx?|jsonl?|parquet|pdf|docx?)"
    r"(?![A-Za-z0-9_.-])",
    flags=re.IGNORECASE,
)
RAW_IDENTIFIER_OUTPUT_PATTERN = re.compile(
    r"(?P<identifier>(?:[A-Za-z][A-Za-z0-9_.-]*[Ii][Dd][A-Za-z0-9_.-]*|"
    r"(?<![A-Za-z0-9_])[Ii][Dd](?![A-Za-z0-9_])|"
    r"(?:タスク|行|従業員|社員|プロジェクト|イベント|"
    r"文書|レコード|組織)[Ii][Dd]))"
    r"を(?:すべて|全て|全部)?(?:挙げ|答え|教え|列挙)",
    flags=re.IGNORECASE,
)
_SUPPORTED_FILTER_SUFFIX_ALTERNATION = "|".join(
    re.escape(value)
    for value in sorted(SUPPORTED_FILTER_SUFFIXES, key=lambda value: (-len(value), value))
)
_SUPPORTED_SUFFIX_RELATION_PATTERN = re.compile(
    r"(?P<value>[^,、。\n]{1,96}?)"
    rf"(?P<field>{_SUPPORTED_FILTER_SUFFIX_ALTERNATION})"
    r"に(?P<operator>一致)(?:する)?"
)
_SUPPORTED_IDENTIFIER_TOKEN = (
    r"(?:[A-Za-z][A-Za-z0-9_.-]*[Ii][Dd][A-Za-z0-9_.-]*|"
    r"(?<![A-Za-z0-9_])[Ii][Dd](?![A-Za-z0-9_])|"
    r"(?:タスク|行|従業員|社員|プロジェクト|イベント|"
    r"文書|レコード|組織)[Ii][Dd])"
)
_SUPPORTED_SCOPE_PREFIX = (
    r"(?P<location>[^,、。\n]{1,96}?)の"
    r"(?P<container>[^,、。\n]{1,96}?)において[ \t]*、[ \t]*"
)
_SUPPORTED_LIST_OUTPUT = (
    rf"(?P<identifier>{_SUPPORTED_IDENTIFIER_TOKEN})を"
    r"(?P<cardinality>すべて|全て|全部)"
    r"(?:挙げて|答えて|教えて|列挙して)ください[.。]?"
)
_SUPPORTED_LIST_STANDARD_PATTERN = re.compile(
    rf"\A{_SUPPORTED_SCOPE_PREFIX}"
    r"(?P<field>[^,、。\n]{1,64}?)が"
    r"(?P<value>[^,、。\n]{1,96}?)に(?P<operator>一致)する"
    rf"{_SUPPORTED_LIST_OUTPUT}\Z"
)
_SUPPORTED_LIST_SUFFIX_PATTERN = re.compile(
    rf"\A{_SUPPORTED_SCOPE_PREFIX}"
    r"(?P<value>[^,、。\n]{1,96}?)"
    rf"(?P<field>{_SUPPORTED_FILTER_SUFFIX_ALTERNATION})"
    r"に(?P<operator>一致)する"
    rf"{_SUPPORTED_LIST_OUTPUT}\Z"
)
_SUPPORTED_NUMBER_TOKEN = r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?"
_SUPPORTED_COMPOUND_PATTERN = re.compile(
    rf"\A{_SUPPORTED_SCOPE_PREFIX}"
    r"(?P<equality_field>[^,、。\n]{1,64}?)が"
    r"(?P<equality_value>[^,、。\n]{1,96}?)"
    r"(?:であり[,、]?)?かつ"
    r"(?P<threshold_field>[^,、。\n]{1,64}?)が"
    rf"(?P<threshold>{_SUPPORTED_NUMBER_TOKEN})(?P<gt_operator>より大きい)"
    r"(?P<target>データ)を抽出し[,、]"
    r"(?P<metric>[^,、。\n]{1,64}?)の平均値を計算してください。"
    r"その平均値に最も近い"
    r"(?P<nearest_descriptor>[^,、。\n]{1,64}?)の"
    rf"{_SUPPORTED_LIST_OUTPUT}\Z"
)

def _capture_has_inline_choice(value: str) -> bool:
    return re.search(
        r"(?<=[^\s,、。\n])(?:か(?!つ)|と)(?=[^\s,、。\n])",
        value,
    ) is not None


def _capture_has_clear_connector(
    value: str,
    *,
    allow_middle_dot: bool = False,
    allow_path_slash: bool = False,
) -> bool:
    return (
        any(connector in value for connector in ALTERNATIVE_CONNECTORS)
        or any(_raw_surface_occurs(value, token) for token in ("or", "and"))
        or _capture_has_inline_choice(value)
        or (not allow_middle_dot and "・" in value)
        or (not allow_path_slash and any(token in value for token in ("/", "／")))
    )


def _metric_descriptor_is_supported(metric: str, descriptor: str) -> bool:
    return metric == descriptor or (metric, descriptor) in _METRIC_DESCRIPTOR_ALIASES


def _supported_match_is_unique(question: str, match: re.Match[str]) -> bool:
    if any(connector in question for connector in ALTERNATIVE_CONNECTORS):
        return False
    if any(_raw_surface_occurs(question, token) for token in ("or", "and")):
        return False
    folded = question.casefold()
    if any(marker.casefold() in folded for marker in _SUPPORTED_LANE_NEGATIVE_MARKERS):
        return False
    captures = {
        group: value
        for group, value in match.groupdict().items()
        if isinstance(value, str)
    }
    if any(
        not value or value != value.strip() or "\t" in value
        for value in captures.values()
    ):
        return False
    if any(
        _capture_has_clear_connector(
            value,
            allow_middle_dot=group in {"value", "equality_value"},
            allow_path_slash=group == "container",
        )
        for group, value in captures.items()
    ):
        return False
    if _raw_scope_pairs(question) != [
        ([match.group("location")], match.group("container"))
    ]:
        return False
    outputs = list(RAW_IDENTIFIER_OUTPUT_PATTERN.finditer(question))
    return (
        len(outputs) == 1
        and outputs[0].span("identifier") == match.span("identifier")
        and outputs[0].group("identifier") == match.group("identifier")
    )


def _supported_list_question_match(question: str) -> re.Match[str] | None:
    matches = [
        match
        for pattern in (
            _SUPPORTED_LIST_STANDARD_PATTERN,
            _SUPPORTED_LIST_SUFFIX_PATTERN,
        )
        if (match := pattern.fullmatch(question)) is not None
    ]
    if len(matches) != 1:
        return None
    match = matches[0]
    if not _supported_match_is_unique(question, match):
        return None
    if question.count("一致") != 1:
        return None
    field = match.group("field")
    value = match.group("value")
    if "が" in field or "が" in value:
        return None
    direct_relations = list(RAW_RELATION_PATTERN.finditer(question))
    suffix_relations = list(_SUPPORTED_SUFFIX_RELATION_PATTERN.finditer(question))
    if match.re is _SUPPORTED_LIST_SUFFIX_PATTERN:
        if field not in SUPPORTED_FILTER_SUFFIXES or direct_relations:
            return None
        if len(suffix_relations) != 1:
            return None
        if (
            suffix_relations[0].span("field") != match.span("field")
            or suffix_relations[0].span("value") != match.span("value")
        ):
            return None
    elif len(direct_relations) != 1 or suffix_relations:
        return None
    if len(_target_type_matches(match.group("identifier"))) != 1:
        return None
    return match


def _supported_compound_question_match(question: str) -> re.Match[str] | None:
    """Return only the exact, non-absorbing supported compound grammar."""

    match = _SUPPORTED_COMPOUND_PATTERN.fullmatch(question)
    if match is None or not _supported_match_is_unique(question, match):
        return None
    semantic_groups = (
        "equality_field",
        "equality_value",
        "threshold_field",
        "metric",
        "nearest_descriptor",
    )
    grammar_tokens = ("が", "かつ", "であり", "より大きい", "に一致", "抽出")
    if any(
        token in match.group(group)
        for group in semantic_groups
        for token in grammar_tokens
    ):
        return None
    if question.count("より大きい") != 1 or question.count("最も近い") != 1:
        return None
    if not _metric_descriptor_is_supported(
        match.group("metric"), match.group("nearest_descriptor")
    ):
        return None
    if _target_type_matches(match.group("target")) != {"record"}:
        return None
    return match


def _supported_question_fullmatch(question: str) -> bool:
    return (
        _supported_list_question_match(question) is not None
        or _supported_compound_question_match(question) is not None
    )


def _requested_semantic_fingerprint(requested: dict[str, Any]) -> dict[str, Any]:
    graph = requested["operation_graph"]
    external_index = {
        item["input_ref"]: index
        for index, item in enumerate(graph["external_inputs"])
    }
    operation_index = {
        item["operation_id"]: index for index, item in enumerate(graph["nodes"])
    }
    output_index = {
        item["output_ref"]: index for index, item in enumerate(graph["nodes"])
    }

    def normalize_ref(reference: str) -> tuple[str, int] | tuple[str, str]:
        if reference in external_index:
            return ("external", external_index[reference])
        if reference in output_index:
            return ("operation", output_index[reference])
        return ("dangling", reference)

    nodes: list[dict[str, Any]] = []
    for item in graph["nodes"]:
        normalized = {
            "operator": item["operator"],
            "input_refs": [normalize_ref(value) for value in item["input_refs"]],
        }
        for key in (
            "predicate",
            "fields",
            "calculation_precision",
            "distance",
            "field",
            "tie_policy",
            "sort_order",
        ):
            if key in item:
                normalized[key] = copy.deepcopy(item[key])
        if "candidate_set_ref" in item:
            normalized["candidate_set_ref"] = normalize_ref(
                item["candidate_set_ref"]
            )
        nodes.append(normalized)

    outputs = []
    for item in requested["requested_outputs"]:
        outputs.append(
            {
                "source_operation_index": operation_index.get(
                    item["source_operation_ref"], -1
                ),
                "return_field": item["return_field"],
                "cardinality": copy.deepcopy(item["cardinality"]),
                "answer_shape": copy.deepcopy(item["answer_shape"]),
                "display_precision": copy.deepcopy(item["display_precision"]),
            }
        )

    return {
        "target": copy.deepcopy(requested["target"]),
        "scope": copy.deepcopy(requested["scope"]),
        "external_inputs": [
            {
                "input_type": item["input_type"],
                "source": item["source"],
                "description": item["description"],
                "has_source_ref": isinstance(item.get("source_ref"), str),
            }
            for item in graph["external_inputs"]
        ],
        "nodes": nodes,
        "edges": sorted(
            (operation_index.get(item["from"], -1), operation_index.get(item["to"], -1))
            for item in graph["edges"]
        ),
        "scope_inheritance": copy.deepcopy(graph["scope_inheritance"]),
        "requested_outputs": outputs,
        "derived_summary": copy.deepcopy(requested["derived_summary"]),
    }


def _supported_expected_fingerprint(question: str) -> dict[str, Any] | None:
    list_match = _supported_list_question_match(question)
    compound_match = _supported_compound_question_match(question)
    if (list_match is None) == (compound_match is None):
        return None

    external_inputs = [
        {
            "input_type": "record_set",
            "source": "scope",
            "description": "Compiler-declared scope record_set input.",
            "has_source_ref": True,
        }
    ]
    scope_inheritance = {
        "default": "inherit_previous_output",
        "reset_requires": "explicit_instruction",
    }
    if list_match is not None:
        identifier = list_match.group("identifier")
        target_types = _target_type_matches(identifier)
        if len(target_types) != 1:
            return None
        predicate = {
            "field": list_match.group("field"),
            "operator": "eq",
            "value": list_match.group("value"),
        }
        return {
            "target": {
                "surface": identifier,
                "canonical_type": next(iter(target_types)),
                "instance": None,
            },
            "scope": {
                "container": list_match.group("container"),
                "location": list_match.group("location"),
                "time_or_version": None,
                "filters": [copy.deepcopy(predicate)],
                "source": "explicit",
                "match_mode": "exact_normalized",
            },
            "external_inputs": external_inputs,
            "nodes": [
                {
                    "operator": "filter",
                    "input_refs": [("external", 0)],
                    "predicate": copy.deepcopy(predicate),
                },
                {
                    "operator": "project",
                    "input_refs": [("operation", 0)],
                    "fields": [identifier],
                },
            ],
            "edges": [(0, 1)],
            "scope_inheritance": scope_inheritance,
            "requested_outputs": [
                {
                    "source_operation_index": 1,
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
        }

    assert compound_match is not None
    try:
        threshold = _loads_strict_json(compound_match.group("threshold"))
    except (json.JSONDecodeError, ValueError):
        return None
    if isinstance(threshold, bool) or not isinstance(threshold, (int, float)):
        return None
    equality_predicate = {
        "field": compound_match.group("equality_field"),
        "operator": "eq",
        "value": compound_match.group("equality_value"),
    }
    threshold_predicate = {
        "field": compound_match.group("threshold_field"),
        "operator": "gt",
        "value": threshold,
    }
    metric = compound_match.group("metric")
    identifier = compound_match.group("identifier")
    return {
        "target": {
            "surface": compound_match.group("target"),
            "canonical_type": "record",
            "instance": None,
        },
        "scope": {
            "container": compound_match.group("container"),
            "location": compound_match.group("location"),
            "time_or_version": None,
            "filters": [
                copy.deepcopy(equality_predicate),
                copy.deepcopy(threshold_predicate),
            ],
            "source": "explicit",
            "match_mode": "exact_normalized",
        },
        "external_inputs": external_inputs,
        "nodes": [
            {
                "operator": "filter",
                "input_refs": [("external", 0)],
                "predicate": copy.deepcopy(equality_predicate),
            },
            {
                "operator": "filter",
                "input_refs": [("operation", 0)],
                "predicate": copy.deepcopy(threshold_predicate),
            },
            {
                "operator": "project",
                "input_refs": [("operation", 1)],
                "fields": [metric],
            },
            {
                "operator": "mean",
                "input_refs": [("operation", 2)],
                "calculation_precision": "exact_unrounded",
            },
            {
                "operator": "argmin_all",
                "input_refs": [("operation", 1), ("operation", 3)],
                "candidate_set_ref": ("operation", 1),
                "distance": "absolute",
                "field": metric,
                "tie_policy": "all",
            },
            {
                "operator": "project",
                "input_refs": [("operation", 4)],
                "fields": [identifier],
            },
        ],
        "edges": [(0, 1), (1, 2), (1, 4), (2, 3), (3, 4), (4, 5)],
        "scope_inheritance": scope_inheritance,
        "requested_outputs": [
            {
                "source_operation_index": 3,
                "return_field": "value",
                "cardinality": {"mode": "single", "expected_count": 1},
                "answer_shape": {
                    "container": "scalar",
                    "value_type": "number",
                    "unit": None,
                    "precision": "exact",
                },
                "display_precision": None,
            },
            {
                "source_operation_index": 5,
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
        ],
        "derived_summary": {
            "operation": "calculate",
            "return_fields": ["value", "identifier"],
            "cardinality": "mixed",
        },
    }


def _supported_contract_semantics_equal(
    question: str, requested: dict[str, Any]
) -> bool:
    expected = _supported_expected_fingerprint(question)
    return expected is not None and _requested_semantic_fingerprint(requested) == expected
QUESTION_UNDERSTANDING_FORBIDDEN_KEYS = {
    "answer_plan",
    "answerability_gate",
    "candidate_evaluations",
    "evidence_id",
    "evidence_ids",
    "final_answer",
    "primary_query_path",
    "proof_obligation",
    "retrieval_hits",
    "retrieval_plan",
    "retrieval_runs",
    "retrieved_evidence_bundles",
    "source_evidence_ids",
}

ALLOWED_MULTI_KIND_QUESTION_SPANS = {
    frozenset({"target_surface", "return_field"}),
}
SINGLE_VALUE_CONTEXT_SLOT_KINDS = {
    "target_surface",
    "target_instance",
    "scope_container",
    "scope_location",
    "scope_time_or_version",
}
QUESTION_MENTION_NODE_TYPES = {
    "target_surface": "entity",
    "target_instance": "entity",
    "scope_container": "scope",
    "scope_location": "scope",
    "scope_time_or_version": "scope",
    "filter_field": "operation",
    "filter_value": "value",
    "operator": "operation",
    "operation": "operation",
    "return_field": "requested_output",
    "answer_shape": "requested_output",
    "unit": "value",
    "precision": "requested_output",
    "cardinality": "requested_output",
    "not_requested": "unknown",
}
QUESTION_MENTION_KINDS_BY_AMBIGUITY_FIELD = {
    "target": {"target_surface", "target_instance"},
    "scope": {
        "scope_container",
        "scope_location",
        "scope_time_or_version",
        "filter_field",
        "filter_value",
        "operator",
    },
    "operation": {
        "operation",
        "operator",
        "filter_field",
        "filter_value",
        "return_field",
    },
    "return_field": {"return_field", "cardinality"},
    "answer_shape": {"answer_shape", "unit", "precision", "cardinality"},
}
COMPILER_TARGET_TYPES = {
    None,
    "record",
    "row",
    "task",
    "document",
    "file",
    "table",
    "chart",
    "person",
    "organization",
    "project",
    "event",
    "metric",
    "status",
    "procedure",
    "claim",
    "value",
    "identifier",
    "field",
    "dataset",
    "source",
}
COMPILER_FAILURE_STAGES: dict[str, frozenset[str]] = {
    "ambiguity_diff_outside_field": frozenset({"candidate_paths"}),
    "ambiguity_field_path_mismatch": frozenset({"candidate_paths"}),
    "ambiguity_primary_field_unchanged": frozenset({"candidate_paths"}),
    "backend_unavailable": frozenset({"runtime"}),
    "canonical_target_type_mismatch": frozenset({"context", "candidate_paths"}),
    "candidate_set_not_an_input": frozenset({"decompose", "candidate_paths"}),
    "compiled_contract_invalid": frozenset({"validation"}),
    "compiled_run_invalid": frozenset({"validation"}),
    "context_node_id_mismatch": frozenset({"context"}),
    "context_source_id_mismatch": frozenset({"context"}),
    "context_source_type_mismatch": frozenset({"context"}),
    "dangling_context_node": frozenset({"context"}),
    "dangling_context_source": frozenset({"context"}),
    "dangling_external_input": frozenset({"decompose", "candidate_paths"}),
    "dangling_requested_output": frozenset({"decompose", "candidate_paths"}),
    "duplicate_ambiguity_candidate": frozenset({"candidate_paths"}),
    "duplicate_ambiguity_field_path": frozenset({"candidate_paths"}),
    "duplicate_candidate_branch": frozenset({"candidate_paths"}),
    "duplicate_context_node": frozenset({"context"}),
    "duplicate_context_source": frozenset({"context"}),
    "duplicate_explicit_mention": frozenset({"context"}),
    "duplicate_not_requested": frozenset({"context"}),
    "duplicate_operation_input": frozenset({"decompose", "candidate_paths"}),
    "empty_ambiguity_candidate": frozenset({"candidate_paths"}),
    "explicit_mention_kind_mismatch": frozenset({"context"}),
    "explicit_span_mismatch": frozenset({"context"}),
    "explicit_span_role_conflict": frozenset({"context"}),
    "forward_operation_reference": frozenset({"decompose", "candidate_paths"}),
    "incompatible_ambiguity_candidates": frozenset({"candidate_paths"}),
    "invalid_backend_mode": frozenset({"runtime"}),
    "invalid_branch_limit": frozenset({"runtime"}),
    "invalid_explicit_span": frozenset({"context"}),
    "invalid_intent_draft": frozenset({"decompose"}),
    "invalid_max_concurrency": frozenset({"runtime"}),
    "invalid_model_json": frozenset({"decompose"}),
    "invalid_model_metadata": frozenset({"runtime"}),
    "invalid_question_input": frozenset({"decompose"}),
    "invalid_retry_limit": frozenset({"runtime"}),
    "local_parallelism_forbidden": frozenset({"runtime"}),
    "model_output_too_large": frozenset({"decompose"}),
    "not_requested_without_negation": frozenset({"context"}),
    "runtime_error": frozenset({"runtime"}),
    "unbound_candidate_literal": frozenset({"candidate_paths"}),
    "unbound_intent_literal": frozenset({"context"}),
    "unbound_not_requested": frozenset({"context"}),
}
COMPILER_FAILURE_CODES = frozenset(COMPILER_FAILURE_STAGES)


def _raw_surface_occurs(question: str, surface: str) -> bool:
    """Match Japanese literals as substrings and ASCII words at token boundaries."""

    if surface.isascii() and surface.replace("_", "").isalnum():
        return re.search(
            rf"(?<![A-Za-z0-9_]){re.escape(surface)}(?![A-Za-z0-9_])",
            question,
            flags=re.IGNORECASE,
        ) is not None
    return surface.casefold() in question.casefold()


def _raw_surface_count(question: str, surface: str) -> int:
    if surface.isascii() and surface.replace("_", "").isalnum():
        return len(
            re.findall(
                rf"(?<![A-Za-z0-9_]){re.escape(surface)}(?![A-Za-z0-9_])",
                question,
                flags=re.IGNORECASE,
            )
        )
    return question.casefold().count(surface.casefold())


def _safe_question_token(question_id: object, original_question: str) -> str:
    if isinstance(question_id, str) and question_id:
        cleaned = "".join(
            character.lower()
            if character.isascii() and character.isalnum()
            else "_"
            for character in question_id
        ).strip("_")
        if cleaned and cleaned[0].isalpha():
            return cleaned[:48]
    return "q_" + hashlib.sha256(original_question.encode("utf-8")).hexdigest()[:16]


def _reject_duplicate_object_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON object key: {key!r}")
        result[key] = value
    return result


def _reject_non_finite_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number is forbidden: {value}")


def _parse_finite_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError(f"non-finite JSON number is forbidden: {value}")
    return parsed


def _check_json_depth(value: object, max_depth: int = MAX_JSON_DEPTH) -> None:
    pending: list[tuple[object, int]] = [(value, 1)]
    while pending:
        current, depth = pending.pop()
        if depth > max_depth:
            raise ValueError(
                f"JSON nesting exceeds the configured depth {max_depth}"
            )
        if isinstance(current, dict):
            pending.extend((child, depth + 1) for child in current.values())
        elif isinstance(current, list):
            pending.extend((child, depth + 1) for child in current)


def _loads_strict_json(value: str) -> object:
    try:
        parsed = json.loads(
            value,
            object_pairs_hook=_reject_duplicate_object_keys,
            parse_constant=_reject_non_finite_constant,
            parse_float=_parse_finite_float,
        )
        _check_json_depth(parsed)
        return parsed
    except (RecursionError, OverflowError) as exc:
        raise ValueError(
            f"JSON parser resource limit exceeded: {type(exc).__name__}"
        ) from exc


def _parse_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _value_matches_type(value: object, value_type: str) -> bool:
    if value_type in {"string", "identifier"}:
        return isinstance(value, str)
    if value_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if value_type == "number":
        return (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and (not isinstance(value, float) or math.isfinite(value))
        )
    if value_type == "boolean":
        return isinstance(value, bool)
    return False


def _claim_value_matches_shape(value: object, answer_shape: dict[str, Any]) -> bool:
    container = answer_shape["container"]
    value_type = answer_shape["value_type"]
    if container == "scalar":
        return _value_matches_type(value, value_type)
    if container == "list":
        return isinstance(value, list) and all(
            _value_matches_type(item, value_type) for item in value
        )
    if container == "key_value":
        return isinstance(value, dict) and all(
            _value_matches_type(item, value_type) for item in value.values()
        )
    if container == "table":
        return isinstance(value, list) and all(
            isinstance(row, dict)
            and all(
                _value_matches_type(cell, value_type) for cell in row.values()
            )
            for row in value
        )
    if container == "prose":
        return isinstance(value, str) and _value_matches_type(value, value_type)
    if container == "yes_no":
        return isinstance(value, bool) and _value_matches_type(value, value_type)
    return False


def _duplicates(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    duplicates: set[str] = set()
    for value in values:
        if value in seen:
            duplicates.add(value)
        seen.add(value)
    return sorted(duplicates)


def _same_json(left: object, right: object) -> bool:
    return json.dumps(
        left, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ) == json.dumps(
        right, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def _canonical_identifier_key(value: object) -> str:
    if isinstance(value, str):
        value = unicodedata.normalize("NFC", value)
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _deterministic_identifier(
    prefix: str, value: object, length: int = 20
) -> str:
    digest = hashlib.sha256(
        _canonical_identifier_key(value).encode("utf-8")
    ).hexdigest()
    return f"{prefix}_{digest[:length]}"


def _non_finite_paths(value: object, path: str = "root") -> list[str]:
    paths: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            paths.extend(_non_finite_paths(child, f"{path}.{key}"))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            paths.extend(_non_finite_paths(child, f"{path}[{index}]"))
    elif isinstance(value, float) and not math.isfinite(value):
        paths.append(path)
    return paths


def _source_ref_is_forbidden(value: object) -> bool:
    if not isinstance(value, str):
        return False
    parts = [part for part in value.replace("\\", "/").split("/") if part]
    if "質問回答" in parts:
        return True
    for part in parts:
        plain = part.split("#", 1)[0].split("?", 1)[0].casefold()
        if plain in FORBIDDEN_SOURCE_NAMES:
            return True
    return False


def _load_schema(path: Path) -> dict[str, Any]:
    try:
        schema = _loads_strict_json(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError(f"cannot load schema {path}: {exc}") from exc
    if not isinstance(schema, dict):
        raise ValueError(f"schema root must be an object: {path}")
    if schema.get("$schema") != "https://json-schema.org/draft/2020-12/schema":
        raise ValueError(f"schema must use Draft 2020-12: {path}")
    if not isinstance(schema.get("$id"), str):
        raise ValueError(f"schema must have an absolute $id: {path}")
    return schema


@lru_cache(maxsize=3)
def _compiled_validator(record_type: str) -> Any:
    try:
        import jsonschema
        from referencing import Registry, Resource
    except ImportError as exc:
        raise ValueError(
            "jsonschema and referencing are required for query-graph validation"
        ) from exc
    target_path = SCHEMA_PATHS.get(record_type)
    if target_path is None:
        raise ValueError(f"unknown record_type: {record_type!r}")
    target_schema = _load_schema(target_path)
    resources: list[tuple[str, Any]] = []
    schemas: list[dict[str, Any]] = []
    for path in SCHEMA_PATHS.values():
        if not path.is_file():
            continue
        schema = target_schema if path == target_path else _load_schema(path)
        jsonschema.Draft202012Validator.check_schema(schema)
        schemas.append(schema)
        resources.append((schema["$id"], Resource.from_contents(schema)))
    if target_schema not in schemas:
        jsonschema.Draft202012Validator.check_schema(target_schema)
        resources.append((target_schema["$id"], Resource.from_contents(target_schema)))
    registry = Registry().with_resources(resources)
    return jsonschema.Draft202012Validator(
        target_schema,
        registry=registry,
        format_checker=jsonschema.FormatChecker(),
    )


def _schema_errors(record: dict[str, Any], validator: Any) -> list[str]:
    errors: list[str] = []
    for error in sorted(
        validator.iter_errors(record),
        key=lambda item: (
            tuple(str(component) for component in item.absolute_path),
            item.message,
        ),
    ):
        location = ".".join(str(component) for component in error.absolute_path) or "root"
        errors.append(f"schema {location}: {error.message}")
    return errors


def _has_cycle(operation_ids: set[str], edges: set[tuple[str, str]]) -> bool:
    outgoing = {operation_id: set() for operation_id in operation_ids}
    indegree = {operation_id: 0 for operation_id in operation_ids}
    for source, target in edges:
        if source not in operation_ids or target not in operation_ids:
            continue
        if target not in outgoing[source]:
            outgoing[source].add(target)
            indegree[target] += 1
    ready = [operation_id for operation_id, degree in indegree.items() if degree == 0]
    visited = 0
    while ready:
        operation_id = ready.pop()
        visited += 1
        for target in outgoing[operation_id]:
            indegree[target] -= 1
            if indegree[target] == 0:
                ready.append(target)
    return visited != len(operation_ids)


def _derived_operation(
    nodes_by_id: dict[str, dict[str, Any]],
    edges: set[tuple[str, str]],
    outputs: list[dict[str, Any]],
) -> str:
    requested_operations = {
        output["source_operation_ref"] for output in outputs
    }
    reverse_edges: dict[str, set[str]] = {operation_id: set() for operation_id in nodes_by_id}
    for source, target in edges:
        if source in nodes_by_id and target in nodes_by_id:
            reverse_edges[target].add(source)
    relevant = set(requested_operations)
    stack = list(requested_operations)
    while stack:
        operation_id = stack.pop()
        for parent in reverse_edges.get(operation_id, ()):
            if parent not in relevant:
                relevant.add(parent)
                stack.append(parent)
    relevant_operators = {
        nodes_by_id[operation_id]["operator"]
        for operation_id in relevant
        if operation_id in nodes_by_id
    }
    if relevant_operators & CALCULATION_OPERATORS:
        return "calculate"
    terminal_operators = {
        nodes_by_id[operation_id]["operator"]
        for operation_id in requested_operations
        if operation_id in nodes_by_id
    }
    normalized: set[str] = set()
    for operator in terminal_operators:
        if operator in DIRECT_OPERATIONS:
            normalized.add(operator)
        elif operator == "boolean_test":
            normalized.add("verify")
        elif operator == "unknown":
            normalized.add("unknown")
    if len(normalized) == 1:
        return next(iter(normalized))
    if not normalized and outputs:
        modes = {output["cardinality"]["mode"] for output in outputs}
        if modes <= {"multiple", "all"}:
            return "list"
        if modes == {"single"}:
            return "retrieve"
    return "unknown"


def _validate_question_intent_contract(record: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if record["provenance"]["rule_version"] != RULE_VERSION:
        errors.append(f"provenance.rule_version must be {RULE_VERSION!r}")
    requested = record["requested"]
    graph = requested["operation_graph"]
    nodes = graph["nodes"]
    external_inputs = graph["external_inputs"]
    outputs = requested["requested_outputs"]

    operation_ids = [node["operation_id"] for node in nodes]
    output_refs = [node["output_ref"] for node in nodes]
    external_refs = [item["input_ref"] for item in external_inputs]
    requested_output_ids = [output["output_id"] for output in outputs]
    for label, values in (
        ("operation_id", operation_ids),
        ("output_ref", output_refs),
        ("external input_ref", external_refs),
        ("output_id", requested_output_ids),
    ):
        duplicates = _duplicates(values)
        if duplicates:
            errors.append(f"duplicate {label}: {duplicates}")
    overlap = sorted(set(output_refs) & set(external_refs))
    if overlap:
        errors.append(f"operation output_ref collides with external input_ref: {overlap}")

    operation_id_set = set(operation_ids)
    available_value_refs = set(output_refs) | set(external_refs)
    nodes_by_id = {node["operation_id"]: node for node in nodes}
    producer_by_ref = {node["output_ref"]: node["operation_id"] for node in nodes}
    implicit_edges: set[tuple[str, str]] = set()
    for node in nodes:
        operation_id = node["operation_id"]
        for input_ref in node["input_refs"]:
            if input_ref not in available_value_refs:
                errors.append(f"{operation_id}: dangling input_ref {input_ref!r}")
            producer = producer_by_ref.get(input_ref)
            if producer is not None:
                implicit_edges.add((producer, operation_id))
        candidate_set_ref = node.get("candidate_set_ref")
        if candidate_set_ref is not None:
            if candidate_set_ref not in available_value_refs:
                errors.append(
                    f"{operation_id}: dangling candidate_set_ref {candidate_set_ref!r}"
                )
            if candidate_set_ref not in node["input_refs"]:
                errors.append(
                    f"{operation_id}: candidate_set_ref must also appear in input_refs"
                )

    edge_pairs_list = [(edge["from"], edge["to"]) for edge in graph["edges"]]
    duplicate_edges = _duplicates(f"{source}->{target}" for source, target in edge_pairs_list)
    if duplicate_edges:
        errors.append(f"duplicate operation edge: {duplicate_edges}")
    edge_pairs = set(edge_pairs_list)
    for source, target in edge_pairs:
        if source not in operation_id_set:
            errors.append(f"operation edge has dangling from reference {source!r}")
        if target not in operation_id_set:
            errors.append(f"operation edge has dangling to reference {target!r}")
    missing_edges = sorted(implicit_edges - edge_pairs)
    extra_edges = sorted(edge_pairs - implicit_edges)
    if missing_edges:
        errors.append(f"operation edges missing input dependencies: {missing_edges}")
    if extra_edges:
        errors.append(f"operation edges not represented by input_refs: {extra_edges}")
    if _has_cycle(operation_id_set, edge_pairs | implicit_edges):
        errors.append("operation_graph must be acyclic")

    for output in outputs:
        source_operation_ref = output["source_operation_ref"]
        if source_operation_ref not in operation_id_set:
            errors.append(
                f"requested output {output['output_id']!r} has dangling "
                f"source_operation_ref {source_operation_ref!r}"
            )
    for external_input in external_inputs:
        if _source_ref_is_forbidden(external_input.get("source_ref")):
            errors.append(
                f"external input {external_input['input_ref']!r} uses a forbidden source_ref"
            )
    graph_filter_predicates = [
        node["predicate"]
        for node in nodes
        if node["operator"] == "filter" and "predicate" in node
    ]
    for index, predicate in enumerate(requested["scope"]["filters"]):
        if not any(_same_json(predicate, candidate) for candidate in graph_filter_predicates):
            errors.append(
                f"scope.filters[{index}] predicate is not preserved by an operation_graph "
                "filter node"
            )

    rules: list[dict[str, Any]] = []
    validator_ids_by_category: dict[str, set[str]] = {
        category: set() for category in VALIDATOR_IDS_BY_CATEGORY
    }
    for category, category_rules in record["forbidden"].items():
        for rule in category_rules:
            rules.append(rule)
            if rule["category"] != category:
                errors.append(
                    f"rule {rule['rule_id']!r} category does not match forbidden.{category}"
                )
            validator_id = rule["check"]["validator_id"]
            if validator_id not in KNOWN_VALIDATOR_IDS:
                errors.append(
                    f"rule {rule['rule_id']!r} uses unknown validator_id {validator_id!r}"
                )
            elif validator_id not in VALIDATOR_IDS_BY_CATEGORY[category]:
                errors.append(
                    f"rule {rule['rule_id']!r} uses validator_id {validator_id!r} "
                    f"in the wrong category"
                )
            else:
                validator_ids_by_category[category].add(validator_id)
                expected_stages = VALIDATOR_STAGES_BY_ID[validator_id]
                actual_stages = set(rule["applies_to"])
                if actual_stages != expected_stages:
                    errors.append(
                        f"rule {rule['rule_id']!r} applies_to must be "
                        f"{sorted(expected_stages)} for validator_id {validator_id!r}"
                    )
            if rule["check"]["params"] != {}:
                errors.append(
                    f"rule {rule['rule_id']!r} validator params must be an empty object"
                )
            if _source_ref_is_forbidden(rule.get("basis_ref")):
                errors.append(f"rule {rule['rule_id']!r} uses a forbidden basis_ref")
    duplicate_rule_ids = _duplicates(rule["rule_id"] for rule in rules)
    if duplicate_rule_ids:
        errors.append(f"duplicate rule_id: {duplicate_rule_ids}")
    for category, required_validator_ids in VALIDATOR_IDS_BY_CATEGORY.items():
        missing = sorted(required_validator_ids - validator_ids_by_category[category])
        if missing:
            errors.append(
                f"forbidden.{category} lacks required validator coverage: {missing}"
            )

    derived = requested["derived_summary"]
    expected_return_fields = list(dict.fromkeys(
        output["return_field"] for output in outputs
    ))
    if derived["return_fields"] != expected_return_fields:
        errors.append(
            "derived_summary.return_fields is inconsistent with requested_outputs: "
            f"expected {expected_return_fields}"
        )
    cardinalities = {output["cardinality"]["mode"] for output in outputs}
    expected_cardinality = next(iter(cardinalities)) if len(cardinalities) == 1 else "mixed"
    if derived["cardinality"] != expected_cardinality:
        errors.append(
            "derived_summary.cardinality is inconsistent with requested_outputs: "
            f"expected {expected_cardinality!r}"
        )
    expected_operation = _derived_operation(nodes_by_id, edge_pairs, outputs)
    if derived["operation"] != expected_operation:
        errors.append(
            "derived_summary.operation is inconsistent with operation_graph: "
            f"expected {expected_operation!r}"
        )
    return errors


def _add_dangling_refs(
    errors: list[str], label: str, values: Iterable[str], allowed: set[str]
) -> None:
    dangling = sorted(set(values) - allowed)
    if dangling:
        errors.append(f"{label} has dangling references: {dangling}")


def _validate_stage_artifacts(record: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    statuses = record["stage_statuses"]

    failed_index = next(
        (
            index
            for index, stage in enumerate(STAGE_ORDER)
            if statuses[stage] == "failed"
        ),
        None,
    )
    if failed_index is not None:
        for stage in STAGE_ORDER[failed_index + 1 :]:
            if statuses[stage] != "skipped":
                errors.append(
                    f"stage {stage!r} must be skipped after an earlier failed stage"
                )

    if statuses["intent"] == "skipped":
        errors.append(
            "intent stage cannot be skipped when a question_intent_contract is embedded"
        )

    artifact_rules = {
        "context": (
            record["query_context_graph"] is not None,
            "query_context_graph",
        ),
        "candidate_paths": (
            bool(record["candidate_query_paths"]),
            "candidate_query_paths",
        ),
        "intent_gate": (record["intent_gate"] is not None, "intent_gate"),
        "candidate_evaluation": (
            bool(record["candidate_evaluations"]),
            "candidate_evaluations",
        ),
        "proof": (record["proof_obligation"] is not None, "proof_obligation"),
        "answerability": (
            record["answerability_gate"] is not None,
            "answerability_gate",
        ),
        "answer_planning": (record["answer_plan"] is not None, "answer_plan"),
        "output_validation": (
            record["output_validation"] is not None,
            "output_validation",
        ),
    }
    for stage, (artifact_present, artifact_name) in artifact_rules.items():
        status = statuses[stage]
        if status == "completed" and not artifact_present:
            errors.append(
                f"completed stage {stage!r} requires {artifact_name} artifact"
            )
        if status == "skipped" and artifact_present:
            errors.append(
                f"skipped stage {stage!r} must not retain {artifact_name} artifact"
            )

    retrieval_artifacts_present = any(
        record[field]
        for field in (
            "retrieval_runs",
            "retrieval_hits",
            "retrieved_evidence_bundles",
        )
    )
    if statuses["retrieval"] == "completed" and not record["retrieval_runs"]:
        errors.append("completed stage 'retrieval' requires a retrieval_run artifact")
    if statuses["retrieval"] == "skipped" and retrieval_artifacts_present:
        errors.append("skipped stage 'retrieval' must not retain retrieval artifacts")

    if statuses["candidate_evaluation"] == "skipped" and record["primary_query_path"] is not None:
        errors.append(
            "skipped stage 'candidate_evaluation' must not retain primary_query_path"
        )

    final_answer = record["final_answer"]
    if statuses["generation"] == "completed" and (
        not isinstance(final_answer, str) or not final_answer
    ):
        errors.append("completed stage 'generation' requires a non-empty final_answer")
    return errors


def _validate_gate_check_registry(
    gate_name: str,
    gate: dict[str, Any] | None,
    expected_check_ids: set[str],
) -> list[str]:
    if gate is None:
        return []
    errors: list[str] = []
    check_ids = [check["check_id"] for check in gate["checks"]]
    if not check_ids:
        errors.append(f"{gate_name}.checks must not be empty")
        return errors
    duplicate_check_ids = _duplicates(check_ids)
    if duplicate_check_ids:
        errors.append(
            f"{gate_name}.checks has duplicate check_id values: "
            f"{duplicate_check_ids}"
        )
    unknown_check_ids = sorted(set(check_ids) - expected_check_ids)
    if unknown_check_ids:
        errors.append(
            f"{gate_name}.checks uses unknown check_id values: {unknown_check_ids}"
        )
    if gate["status"] == "pass" and set(check_ids) != expected_check_ids:
        errors.append(
            f"passing {gate_name} requires the complete registered check set"
        )
    return errors


def _validate_query_run(record: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    errors.extend(_validate_stage_artifacts(record))
    errors.extend(
        _validate_gate_check_registry(
            "intent_gate", record["intent_gate"], INTENT_GATE_CHECK_IDS
        )
    )
    errors.extend(
        _validate_gate_check_registry(
            "answerability_gate",
            record["answerability_gate"],
            ANSWERABILITY_GATE_CHECK_IDS,
        )
    )
    contract = record["question_intent_contract"]
    contract_errors = _validate_question_intent_contract(contract)
    errors.extend(
        f"question_intent_contract: {error}" for error in contract_errors
    )
    if record["question_id"] != contract["question_id"]:
        errors.append("query_run question_id does not match question_intent_contract")
    if record["original_question"] != contract["original_question"]:
        errors.append("query_run original_question does not match question_intent_contract")
    runtime_rule_version = record["runtime_metadata"]["rule_version"]
    contract_rule_version = contract["provenance"]["rule_version"]
    if runtime_rule_version != RULE_VERSION:
        errors.append(f"runtime_metadata.rule_version must be {RULE_VERSION!r}")
    if runtime_rule_version != contract_rule_version:
        errors.append(
            "runtime_metadata.rule_version does not match the embedded "
            "question_intent_contract"
        )

    operation_nodes = contract["requested"]["operation_graph"]["nodes"]
    operation_ids = {node["operation_id"] for node in operation_nodes}
    operation_nodes_by_id = {
        node["operation_id"]: node for node in operation_nodes
    }
    requested_outputs = contract["requested"]["requested_outputs"]
    requested_output_ids = {output["output_id"] for output in requested_outputs}
    requested_outputs_by_id = {
        output["output_id"]: output for output in requested_outputs
    }
    operation_output_refs = {node["output_ref"] for node in operation_nodes}
    rules = [
        rule
        for category_rules in contract["forbidden"].values()
        for rule in category_rules
    ]
    rules_by_id = {rule["rule_id"]: rule for rule in rules}
    rule_ids = set(rules_by_id)
    requested_return_fields = {
        output["return_field"] for output in requested_outputs
    }
    requested_scope_filters = contract["requested"]["scope"]["filters"]
    requested_target_type = contract["requested"]["target"]["canonical_type"]
    has_all_output = any(
        output["cardinality"]["mode"] == "all"
        for output in requested_outputs
    )
    has_count_output = any(
        output["return_field"] == "count"
        or operation_nodes_by_id.get(output["source_operation_ref"], {}).get(
            "operator"
        )
        == "count"
        for output in requested_outputs
    )

    candidate_paths = record["candidate_query_paths"]
    branch_ids_list = [path["branch_id"] for path in candidate_paths]
    duplicate_branch_ids = _duplicates(branch_ids_list)
    if duplicate_branch_ids:
        errors.append(f"duplicate branch_id: {duplicate_branch_ids}")
    branch_ids = set(branch_ids_list)
    candidate_paths_by_branch = {
        path["branch_id"]: path for path in candidate_paths
    }
    for path in candidate_paths:
        if path["status"] == "pending":
            errors.append(
                f"completed query_run must not retain pending branch {path['branch_id']!r}"
            )
        if path["parent_question_id"] != record["question_id"]:
            errors.append(
                f"branch {path['branch_id']!r} parent_question_id differs from query_run"
            )
        if not _same_json(path["candidate_intent"], contract["requested"]):
            errors.append(
                f"branch {path['branch_id']!r} candidate_intent differs from the "
                "question_intent_contract requested intent"
            )

    retrieval_run_ids = [item["retrieval_run_id"] for item in record["retrieval_runs"]]
    duplicate_retrieval_run_ids = _duplicates(retrieval_run_ids)
    if duplicate_retrieval_run_ids:
        errors.append(f"duplicate retrieval_run_id: {duplicate_retrieval_run_ids}")
    completed_channels_by_branch: dict[str, set[str]] = {}
    retrieval_started_times: list[datetime] = []
    retrieval_completed_times: list[datetime] = []
    for retrieval_run in record["retrieval_runs"]:
        branch_id = retrieval_run["branch_id"]
        if branch_id not in branch_ids:
            errors.append(
                f"retrieval_run {retrieval_run['retrieval_run_id']!r} has dangling branch_id "
                f"{branch_id!r}"
            )
        if retrieval_run["plan"]["branch_id"] != branch_id:
            errors.append(
                f"retrieval_run {retrieval_run['retrieval_run_id']!r} plan.branch_id mismatch"
            )
        plan = retrieval_run["plan"]
        if (
            requested_target_type is not None
            and requested_target_type not in plan["target_types"]
        ):
            errors.append(
                f"retrieval_run {retrieval_run['retrieval_run_id']!r} target_types "
                "does not include the requested canonical target type"
            )
        if set(plan["return_fields"]) != requested_return_fields:
            errors.append(
                f"retrieval_run {retrieval_run['retrieval_run_id']!r} return_fields "
                "differ from the requested output fields"
            )
        if not _same_json(plan["scope_filters"], requested_scope_filters):
            errors.append(
                f"retrieval_run {retrieval_run['retrieval_run_id']!r} scope_filters "
                "differ from the requested scope"
            )
        coverage_requirement = plan["coverage_requirement"]
        if has_all_output:
            if coverage_requirement not in {
                "exhaustive",
                "authoritative_enumeration",
            }:
                errors.append(
                    f"retrieval_run {retrieval_run['retrieval_run_id']!r} must plan "
                    "exhaustive or authoritative enumeration coverage for an all output"
                )
            if plan["scan_mode"] != "exhaustive":
                errors.append(
                    f"retrieval_run {retrieval_run['retrieval_run_id']!r} must use "
                    "scan_mode=exhaustive for an all output"
                )
        elif has_count_output:
            if coverage_requirement not in {
                "exhaustive",
                "authoritative_aggregate",
                "authoritative_enumeration",
            }:
                errors.append(
                    f"retrieval_run {retrieval_run['retrieval_run_id']!r} has "
                    "insufficient coverage for a count output"
                )
            if (
                coverage_requirement != "authoritative_aggregate"
                and plan["scan_mode"] != "exhaustive"
            ):
                errors.append(
                    f"retrieval_run {retrieval_run['retrieval_run_id']!r} must use "
                    "scan_mode=exhaustive for enumerated count coverage"
                )
        run_status = retrieval_run["status"]
        started_at = retrieval_run["started_at"]
        completed_at = retrieval_run["completed_at"]
        run_error = retrieval_run["error"]
        if run_status == "completed":
            if started_at is None or completed_at is None:
                errors.append(
                    f"completed retrieval_run {retrieval_run['retrieval_run_id']!r} "
                    "requires started_at and completed_at"
                )
            if run_error is not None:
                errors.append(
                    f"completed retrieval_run {retrieval_run['retrieval_run_id']!r} "
                    "must not retain an error"
                )
        elif run_status == "failed":
            if completed_at is None:
                errors.append(
                    f"failed retrieval_run {retrieval_run['retrieval_run_id']!r} "
                    "requires completed_at"
                )
            if run_error is None:
                errors.append(
                    f"failed retrieval_run {retrieval_run['retrieval_run_id']!r} "
                    "requires an error"
                )
        else:
            if started_at is not None or completed_at is not None or run_error is not None:
                errors.append(
                    f"skipped retrieval_run {retrieval_run['retrieval_run_id']!r} "
                    "must not retain timestamps or an error"
                )
        parsed_started_at = (
            _parse_datetime(started_at) if started_at is not None else None
        )
        parsed_completed_at = (
            _parse_datetime(completed_at) if completed_at is not None else None
        )
        if parsed_started_at is not None:
            retrieval_started_times.append(parsed_started_at)
        if parsed_completed_at is not None:
            retrieval_completed_times.append(parsed_completed_at)
        if (
            parsed_started_at is not None
            and parsed_completed_at is not None
            and parsed_started_at > parsed_completed_at
        ):
            errors.append(
                f"retrieval_run {retrieval_run['retrieval_run_id']!r} started_at "
                "must not be later than completed_at"
            )
        if retrieval_run["status"] == "completed":
            completed_channels_by_branch.setdefault(branch_id, set()).update(
                plan["channels"]
            )

    runtime_metadata = record["runtime_metadata"]
    runtime_started_at = _parse_datetime(runtime_metadata["started_at"])
    runtime_completed_at = _parse_datetime(runtime_metadata["completed_at"])
    if runtime_started_at > runtime_completed_at:
        errors.append(
            "runtime_metadata.started_at must not be later than completed_at"
        )
    if any(started < runtime_started_at for started in retrieval_started_times):
        errors.append(
            "runtime_metadata.started_at must cover every retrieval_run start"
        )
    if any(completed > runtime_completed_at for completed in retrieval_completed_times):
        errors.append(
            "runtime_metadata.completed_at must cover every retrieval_run completion"
        )
    runtime_interval_ms = (
        runtime_completed_at - runtime_started_at
    ).total_seconds() * 1000
    if abs(runtime_metadata["duration_ms"] - runtime_interval_ms) > 1:
        errors.append(
            "runtime_metadata.duration_ms is inconsistent with its timestamp interval"
        )
    used_channels = set().union(*completed_channels_by_branch.values()) if completed_channels_by_branch else set()
    required_index_kinds = set().union(
        *(INDEX_KINDS_BY_RETRIEVAL_CHANNEL[channel] for channel in used_channels)
    ) if used_channels else set()
    runtime_index_kinds = {
        index["kind"] for index in runtime_metadata["indexes"]
    }
    missing_index_kinds = sorted(required_index_kinds - runtime_index_kinds)
    if missing_index_kinds:
        errors.append(
            f"runtime_metadata.indexes lacks kinds required by completed retrieval "
            f"channels: {missing_index_kinds}"
        )

    hit_evidence_ids: set[str] = set()
    hit_evidence_ids_by_branch: dict[str, set[str]] = {}
    hit_document_ids_by_branch_and_evidence: dict[
        str, dict[str, set[str]]
    ] = {}
    hit_routes_by_branch_evidence_document: dict[
        tuple[str, str, str], set[tuple[str, str]]
    ] = {}
    hit_search_unit_ids: set[str] = set()
    hit_search_unit_ids_by_branch: dict[str, set[str]] = {}
    document_ids: set[str] = set()
    for index, hit in enumerate(record["retrieval_hits"]):
        if hit["branch_id"] not in branch_ids:
            errors.append(
                f"retrieval_hits[{index}] has dangling branch_id {hit['branch_id']!r}"
            )
        if hit["channel"] not in completed_channels_by_branch.get(
            hit["branch_id"], set()
        ):
            errors.append(
                f"retrieval_hits[{index}].channel is not enabled by a completed "
                "retrieval run for the same branch"
            )
        branch_id = hit["branch_id"]
        hit_evidence_ids.update(hit["source_evidence_ids"])
        hit_evidence_ids_by_branch.setdefault(branch_id, set()).update(
            hit["source_evidence_ids"]
        )
        documents_by_evidence = hit_document_ids_by_branch_and_evidence.setdefault(
            branch_id, {}
        )
        for evidence_id in hit["source_evidence_ids"]:
            documents_by_evidence.setdefault(evidence_id, set()).add(
                hit["document_id"]
            )
            hit_routes_by_branch_evidence_document.setdefault(
                (branch_id, evidence_id, hit["document_id"]), set()
            ).add((hit["search_unit_id"], hit["channel"]))
        hit_search_unit_ids.add(hit["search_unit_id"])
        hit_search_unit_ids_by_branch.setdefault(branch_id, set()).add(
            hit["search_unit_id"]
        )
        document_ids.add(hit["document_id"])

    bundles = record["retrieved_evidence_bundles"]
    duplicate_bundle_branches = _duplicates(bundle["query_branch_id"] for bundle in bundles)
    if duplicate_bundle_branches:
        errors.append(
            f"duplicate retrieved evidence bundle branch: {duplicate_bundle_branches}"
        )
    bundle_evidence_ids: set[str] = set()
    bundle_evidence_ids_by_branch: dict[str, set[str]] = {}
    evidence_nodes_by_branch: dict[str, dict[str, dict[str, Any]]] = {}
    excluded_evidence_ids_by_branch: dict[str, set[str]] = {}
    for bundle_index, bundle in enumerate(bundles):
        branch_id = bundle["query_branch_id"]
        if branch_id not in branch_ids:
            errors.append(
                f"retrieved_evidence_bundles[{bundle_index}] has dangling branch_id "
                f"{branch_id!r}"
            )
        node_ids_list = [node["evidence_id"] for node in bundle["evidence_nodes"]]
        duplicate_node_ids = _duplicates(node_ids_list)
        if duplicate_node_ids:
            errors.append(
                f"retrieved_evidence_bundles[{bundle_index}] has duplicate evidence_id: "
                f"{duplicate_node_ids}"
            )
        node_ids = set(node_ids_list)
        bundle_evidence_ids.update(node_ids)
        bundle_evidence_ids_by_branch.setdefault(branch_id, set()).update(node_ids)
        branch_nodes = evidence_nodes_by_branch.setdefault(branch_id, {})
        branch_nodes.update(
            {node["evidence_id"]: node for node in bundle["evidence_nodes"]}
        )
        for node_index, node in enumerate(bundle["evidence_nodes"]):
            document_ids.add(node["document_id"])
            if node["evidence_id"] not in hit_evidence_ids_by_branch.get(
                branch_id, set()
            ):
                errors.append(
                    f"retrieved_evidence_bundles[{bundle_index}].evidence_nodes"
                    f"[{node_index}].evidence_id was not supplied by a retrieval hit "
                    "from the same branch"
                )
            matching_document_ids = hit_document_ids_by_branch_and_evidence.get(
                branch_id, {}
            ).get(node["evidence_id"], set())
            if node["document_id"] not in matching_document_ids:
                errors.append(
                    f"retrieved_evidence_bundles[{bundle_index}].evidence_nodes"
                    f"[{node_index}].document_id does not match a retrieval hit "
                    "for that Evidence ID in the same branch"
                )
            matching_routes = hit_routes_by_branch_evidence_document.get(
                (branch_id, node["evidence_id"], node["document_id"]), set()
            )
            if not node["search_unit_ids"]:
                errors.append(
                    f"retrieved_evidence_bundles[{bundle_index}].evidence_nodes"
                    f"[{node_index}].search_unit_ids must not be empty"
                )
            unmatched_search_unit_ids = sorted(
                search_unit_id
                for search_unit_id in node["search_unit_ids"]
                if not any(
                    route_search_unit_id == search_unit_id
                    and route_channel in node["discovered_by"]
                    for route_search_unit_id, route_channel in matching_routes
                )
            )
            if unmatched_search_unit_ids:
                errors.append(
                    f"retrieved_evidence_bundles[{bundle_index}].evidence_nodes"
                    f"[{node_index}] has search_unit_ids without a matching "
                    f"same-hit tuple: {unmatched_search_unit_ids}"
                )
            unmatched_channels = sorted(
                channel
                for channel in node["discovered_by"]
                if not any(
                    route_channel == channel
                    and route_search_unit_id in node["search_unit_ids"]
                    for route_search_unit_id, route_channel in matching_routes
                )
            )
            if unmatched_channels:
                errors.append(
                    f"retrieved_evidence_bundles[{bundle_index}].evidence_nodes"
                    f"[{node_index}].discovered_by has channels without a matching "
                    f"same-hit tuple: {unmatched_channels}"
                )
            _add_dangling_refs(
                errors,
                f"retrieved_evidence_bundles[{bundle_index}].evidence_nodes[{node_index}].search_unit_ids",
                node["search_unit_ids"],
                hit_search_unit_ids_by_branch.get(branch_id, set()),
            )
        for edge_index, edge in enumerate(bundle["evidence_edges"]):
            _add_dangling_refs(
                errors,
                f"retrieved_evidence_bundles[{bundle_index}].evidence_edges[{edge_index}]",
                (edge["from"], edge["to"]),
                node_ids,
            )
        for field in ("conflicts", "rejected_evidence"):
            for issue_index, issue in enumerate(bundle[field]):
                excluded_evidence_ids_by_branch.setdefault(branch_id, set()).update(
                    issue["evidence_ids"]
                )
                _add_dangling_refs(
                    errors,
                    f"retrieved_evidence_bundles[{bundle_index}].{field}[{issue_index}].evidence_ids",
                    issue["evidence_ids"],
                    node_ids,
                )
    evidence_ids = hit_evidence_ids | bundle_evidence_ids

    admissible_evidence_ids_by_branch: dict[str, set[str]] = {}
    exact_evidence_ids_by_branch: dict[str, set[str]] = {}
    for branch_id, nodes_by_id in evidence_nodes_by_branch.items():
        excluded_ids = excluded_evidence_ids_by_branch.get(branch_id, set())
        admissible_evidence_ids_by_branch[branch_id] = {
            evidence_id
            for evidence_id, node in nodes_by_id.items()
            if node["role"] == "supporting"
            and node["target_match"] == "matched"
            and node["scope_match"] == "matched"
            and node["exactness"] != "unresolved"
            and evidence_id not in excluded_ids
        }
        exact_evidence_ids_by_branch[branch_id] = {
            evidence_id
            for evidence_id, node in nodes_by_id.items()
            if node["exactness"] == "exact"
        }

    for path in candidate_paths:
        branch_id = path["branch_id"]
        _add_dangling_refs(
            errors,
            f"branch {branch_id!r} evidence_ids",
            path["evidence_ids"],
            bundle_evidence_ids_by_branch.get(branch_id, set()),
        )
    evaluation_branch_ids = [item["branch_id"] for item in record["candidate_evaluations"]]
    duplicate_evaluations = _duplicates(evaluation_branch_ids)
    if duplicate_evaluations:
        errors.append(f"duplicate candidate evaluation branch_id: {duplicate_evaluations}")
    for evaluation in record["candidate_evaluations"]:
        branch_id = evaluation["branch_id"]
        if branch_id not in branch_ids:
            errors.append(
                f"candidate evaluation has dangling branch_id {branch_id!r}"
            )
        _add_dangling_refs(
            errors,
            f"candidate evaluation {branch_id!r} evidence_ids",
            evaluation["evidence_ids"],
            bundle_evidence_ids_by_branch.get(branch_id, set()),
        )
        if (
            evaluation["status"] == "equivalent_for_answer"
            and evaluation["equivalence_class_id"] is None
        ):
            errors.append(
                f"candidate evaluation {branch_id!r} requires a non-null "
                "equivalence_class_id for equivalent_for_answer status"
            )
        if (
            evaluation["status"] != "equivalent_for_answer"
            and evaluation["equivalence_class_id"] is not None
        ):
            errors.append(
                f"candidate evaluation {branch_id!r} must not retain an "
                "equivalence_class_id outside equivalent_for_answer status"
            )
    evaluation_by_branch = {
        evaluation["branch_id"]: evaluation
        for evaluation in record["candidate_evaluations"]
    }
    if record["stage_statuses"]["candidate_evaluation"] == "completed":
        evaluated_branch_ids = set(evaluation_branch_ids)
        if evaluated_branch_ids != branch_ids:
            errors.append(
                "completed candidate_evaluation stage requires exactly one "
                "evaluation for every candidate branch"
            )

    primary = record["primary_query_path"]
    primary_branch_id = primary["branch_id"] if primary is not None else None
    primary_bundle_evidence_ids = (
        bundle_evidence_ids_by_branch.get(primary_branch_id, set())
        if primary_branch_id is not None
        else set()
    )
    primary_admissible_evidence_ids = (
        admissible_evidence_ids_by_branch.get(primary_branch_id, set())
        if primary_branch_id is not None
        else set()
    )
    primary_exact_evidence_ids = (
        exact_evidence_ids_by_branch.get(primary_branch_id, set())
        if primary_branch_id is not None
        else set()
    )
    if primary is not None:
        _add_dangling_refs(
            errors,
            "primary_query_path branch references",
            [primary["branch_id"], *primary["equivalent_branch_ids"]],
            branch_ids,
        )
        _add_dangling_refs(
            errors,
            "primary_query_path.evidence_ids",
            primary["evidence_ids"],
            primary_bundle_evidence_ids,
        )
        primary_evaluation = evaluation_by_branch.get(primary["branch_id"])
        if primary_evaluation is None or primary_evaluation["status"] not in {
            "resolved", "equivalent_for_answer"
        }:
            errors.append(
                "primary_query_path branch requires a resolved or equivalent_for_answer "
                "candidate evaluation"
            )
        elif primary_evaluation["disqualifiers"]:
            errors.append(
                "primary_query_path candidate evaluation must not have disqualifiers"
            )
        primary_candidate = candidate_paths_by_branch.get(primary["branch_id"])
        if primary_candidate is None:
            errors.append("primary_query_path requires a corresponding candidate path")
        elif (
            primary_candidate["status"] != "completed"
            or primary_candidate["error"] is not None
        ):
            errors.append(
                "primary_query_path candidate must be completed without an error"
            )
        equivalent_branch_ids = set(primary["equivalent_branch_ids"])
        if primary["branch_id"] in equivalent_branch_ids:
            errors.append(
                "primary_query_path.equivalent_branch_ids must not contain its own branch"
            )
        answerable_evaluations = {
            branch_id: evaluation
            for branch_id, evaluation in evaluation_by_branch.items()
            if evaluation["status"] in {"resolved", "equivalent_for_answer"}
        }
        expected_answerable_branch_ids = {
            primary["branch_id"],
            *equivalent_branch_ids,
        }
        if set(answerable_evaluations) != expected_answerable_branch_ids:
            errors.append(
                "primary_query_path must name exactly every resolved/equivalent branch"
            )
        if equivalent_branch_ids:
            equivalence_class_ids = {
                evaluation["equivalence_class_id"]
                for branch_id, evaluation in answerable_evaluations.items()
                if branch_id in expected_answerable_branch_ids
            }
            if (
                any(
                    evaluation["status"] != "equivalent_for_answer"
                    or evaluation["disqualifiers"]
                    for branch_id, evaluation in answerable_evaluations.items()
                    if branch_id in expected_answerable_branch_ids
                )
                or None in equivalence_class_ids
                or len(equivalence_class_ids) != 1
            ):
                errors.append(
                    "equivalent primary branches require undisqualified "
                    "equivalent_for_answer evaluations in one non-null class"
                )
            if not primary["required_qualifiers"]:
                errors.append(
                    "equivalent primary branches require explicit required_qualifiers"
                )
        elif primary_evaluation is not None and primary_evaluation["status"] != "resolved":
            errors.append(
                "a non-equivalent primary branch requires status=resolved"
            )
        answerable_context_refs: set[str] = set()
        if record["query_context_graph"] is not None:
            context_items = list(record["query_context_graph"]["edges"])
            for edge in context_items:
                answerable_context_refs.update((edge["from"], edge["to"]))
        for branch_id, evaluation in answerable_evaluations.items():
            branch_candidate = candidate_paths_by_branch.get(branch_id)
            if branch_candidate is None or (
                branch_candidate["status"] != "completed"
                or branch_candidate["error"] is not None
            ):
                errors.append(
                    f"answerable branch {branch_id!r} must be completed without an error"
                )
            else:
                candidate_evidence_ids = set(branch_candidate["evidence_ids"])
                if not candidate_evidence_ids:
                    errors.append(
                        f"answerable branch {branch_id!r} requires candidate path "
                        "evidence"
                    )
                _add_dangling_refs(
                    errors,
                    f"answerable candidate path {branch_id!r} evidence_ids",
                    branch_candidate["evidence_ids"],
                    admissible_evidence_ids_by_branch.get(branch_id, set()),
                )
                evaluation_evidence_ids = set(evaluation["evidence_ids"])
                if not evaluation_evidence_ids.issubset(candidate_evidence_ids):
                    errors.append(
                        f"answerable evaluation {branch_id!r} evidence_ids must be "
                        "contained in its candidate path evidence_ids"
                    )
            if evaluation["disqualifiers"]:
                errors.append(
                    f"answerable branch {branch_id!r} must not have disqualifiers"
                )
            if not evaluation["evidence_ids"]:
                errors.append(
                    f"answerable branch {branch_id!r} requires evaluation evidence"
                )
            _add_dangling_refs(
                errors,
                f"answerable evaluation {branch_id!r} evidence_ids",
                evaluation["evidence_ids"],
                admissible_evidence_ids_by_branch.get(branch_id, set()),
            )
            if not evaluation["signals"]:
                errors.append(
                    f"answerable branch {branch_id!r} requires evaluation signals"
                )
            signal_names = {signal["name"] for signal in evaluation["signals"]}
            missing_signal_names = {
                "evidence_support",
                "provenance_quality",
            } - signal_names
            if missing_signal_names:
                errors.append(
                    f"answerable branch {branch_id!r} lacks required signals: "
                    f"{sorted(missing_signal_names)}"
                )
            signal_refs_allowed = (
                set(evaluation["evidence_ids"])
                | answerable_context_refs
            )
            for signal_index, signal in enumerate(evaluation["signals"]):
                if not signal["basis_refs"]:
                    errors.append(
                        f"answerable branch {branch_id!r} signal[{signal_index}] "
                        "requires basis_refs"
                    )
                _add_dangling_refs(
                    errors,
                    f"answerable branch {branch_id!r} signal[{signal_index}].basis_refs",
                    signal["basis_refs"],
                    signal_refs_allowed,
                )

    proof = record["proof_obligation"]
    proof_requirement_ids: set[str] = set()
    if proof is not None:
        operation_graph_id = contract["requested"]["operation_graph"]["operation_graph_id"]
        if proof["operation_graph_ref"] != operation_graph_id:
            errors.append(
                "proof_obligation.operation_graph_ref differs from the embedded "
                "question_intent_contract operation_graph_id"
            )
        requirements = proof["requirements"]
        proof_requirement_ids = {
            requirement["requirement_id"] for requirement in requirements
        }
        duplicate_requirement_ids = _duplicates(
            requirement["requirement_id"] for requirement in requirements
        )
        if duplicate_requirement_ids:
            errors.append(f"duplicate proof requirement_id: {duplicate_requirement_ids}")
        allowed_proof_output_refs = requested_output_ids | operation_output_refs
        for requirement in requirements:
            if requirement["operation_ref"] not in operation_ids:
                errors.append(
                    f"proof requirement {requirement['requirement_id']!r} has dangling "
                    f"operation_ref {requirement['operation_ref']!r}"
                )
            if requirement["output_ref"] not in allowed_proof_output_refs:
                errors.append(
                    f"proof requirement {requirement['requirement_id']!r} has dangling "
                    f"output_ref {requirement['output_ref']!r}"
                )
            _add_dangling_refs(
                errors,
                f"proof requirement {requirement['requirement_id']!r} evidence_ids",
                requirement["evidence_ids"],
                primary_bundle_evidence_ids,
            )
        _add_dangling_refs(
            errors,
            "proof coverage evidence_ids",
            proof["coverage"]["evidence_ids"],
            primary_bundle_evidence_ids,
        )
        required_statuses = [
            requirement["status"] for requirement in requirements if requirement["required"]
        ]
        if "unsatisfied" in required_statuses:
            expected_proof_status = "unsatisfied"
        elif "indeterminate" in required_statuses:
            expected_proof_status = "indeterminate"
        else:
            expected_proof_status = "satisfied"
        if proof["overall"]["status"] != expected_proof_status:
            errors.append(
                "proof_obligation.overall.status is inconsistent with required requirements: "
                f"expected {expected_proof_status!r}"
            )

    context_graph = record["query_context_graph"]
    context_refs: set[str] = set()
    if context_graph is not None:
        context_edges = list(context_graph["edges"])
        context_edges.extend(item["edge"] for item in context_graph["rejected_context"])
        for index, edge in enumerate(context_edges):
            context_refs.update((edge["from"], edge["to"]))
            if _source_ref_is_forbidden(edge["source_ref"]):
                errors.append(f"query context edge {index} uses a forbidden source_ref")

    answer_plan = record["answer_plan"]
    claim_ids: set[str] = set()
    claims_by_id: dict[str, dict[str, Any]] = {}
    if answer_plan is not None:
        claims = answer_plan["allowed_claims"]
        claim_id_list = [claim["claim_id"] for claim in claims]
        duplicate_claim_ids = _duplicates(claim_id_list)
        if duplicate_claim_ids:
            errors.append(f"duplicate claim_id: {duplicate_claim_ids}")
        claim_ids = set(claim_id_list)
        claims_by_id = {claim["claim_id"]: claim for claim in claims}
        for claim in claims:
            _add_dangling_refs(
                errors,
                f"claim {claim['claim_id']!r} evidence_ids",
                claim["evidence_ids"],
                primary_bundle_evidence_ids,
            )
        output_plan_ids = [plan["output_id"] for plan in answer_plan["output_plans"]]
        duplicate_output_plans = _duplicates(output_plan_ids)
        if duplicate_output_plans:
            errors.append(f"duplicate answer output plan output_id: {duplicate_output_plans}")
        _add_dangling_refs(
            errors,
            "answer_plan output_id",
            output_plan_ids,
            requested_output_ids,
        )
        for plan in answer_plan["output_plans"]:
            expected_output = next(
                (
                    output
                    for output in requested_outputs
                    if output["output_id"] == plan["output_id"]
                ),
                None,
            )
            if expected_output is not None and not _same_json(
                plan["answer_shape"], expected_output["answer_shape"]
            ):
                errors.append(
                    f"answer output plan {plan['output_id']!r} answer_shape differs "
                    "from requested output"
                )
            if expected_output is not None:
                source_operator = operation_nodes_by_id.get(
                    expected_output["source_operation_ref"], {}
                ).get("operator")
                expected_answer_mode = (
                    "grounded_generation"
                    if expected_output["answer_shape"]["container"] == "prose"
                    or source_operator in {"explain", "procedure"}
                    else "deterministic"
                )
                if plan["answer_mode"] != expected_answer_mode:
                    errors.append(
                        f"answer output plan {plan['output_id']!r} answer_mode must "
                        f"be {expected_answer_mode!r} for its requested shape/operator"
                    )
            _add_dangling_refs(
                errors,
                f"answer output plan {plan['output_id']!r} allowed_claim_ids",
                plan["allowed_claim_ids"],
                claim_ids,
            )
            if (
                expected_output is not None
                and expected_output["answer_shape"]["precision"] == "exact"
            ):
                non_exact_claim_ids = sorted(
                    claim_id
                    for claim_id in plan["allowed_claim_ids"]
                    if claim_id in claims_by_id
                    and claims_by_id[claim_id]["exactness"] != "exact"
                )
                if non_exact_claim_ids:
                    errors.append(
                        f"answer output plan {plan['output_id']!r} requires exact claims: "
                        f"{non_exact_claim_ids}"
                    )
        _add_dangling_refs(
            errors,
            "answer_plan.forbidden_rule_ids",
            answer_plan["forbidden_rule_ids"],
            rule_ids,
        )
        if primary is not None:
            duplicate_primary_qualifiers = _duplicates(
                primary["required_qualifiers"]
            )
            duplicate_plan_qualifiers = _duplicates(
                answer_plan["required_qualifiers"]
            )
            if duplicate_primary_qualifiers or duplicate_plan_qualifiers:
                errors.append("required_qualifiers must not contain duplicates")
            if set(primary["required_qualifiers"]) != set(
                answer_plan["required_qualifiers"]
            ):
                errors.append(
                    "answer_plan.required_qualifiers must inherit every primary "
                    "required qualifier exactly"
                )

    stage_statuses = record["stage_statuses"]
    stage_status_key = {
        "intent": "intent",
        "retrieval": "retrieval",
        "generation": "generation",
        "validation": "output_validation",
    }
    result_pairs: set[tuple[str, str]] = set()
    result_statuses: list[str] = []
    result_actions: list[str] = []
    violated_rule_ids: set[str] = set()
    known_subject_refs = (
        branch_ids
        | operation_ids
        | operation_output_refs
        | requested_output_ids
        | rule_ids
        | set(retrieval_run_ids)
        | evidence_ids
        | hit_search_unit_ids
        | document_ids
        | claim_ids
        | proof_requirement_ids
        | context_refs
        | {
            contract["question_intent_contract_id"],
            contract["requested"]["operation_graph"]["operation_graph_id"],
            record["query_run_id"],
        }
        | {
            value
            for value in (record["question_id"],)
            if isinstance(value, str)
        }
        | {
            external_input["input_ref"]
            for external_input in contract["requested"]["operation_graph"]["external_inputs"]
        }
        | {
            evaluation["equivalence_class_id"]
            for evaluation in record["candidate_evaluations"]
            if evaluation["equivalence_class_id"] is not None
        }
    )
    subject_refs_by_stage = {
        "intent": (
            operation_ids
            | requested_output_ids
            | {
                contract["question_intent_contract_id"],
                contract["requested"]["operation_graph"]["operation_graph_id"],
            }
        ),
        "retrieval": (
            branch_ids
            | set(retrieval_run_ids)
            | evidence_ids
            | hit_search_unit_ids
            | document_ids
        ),
        "generation": claim_ids
        | requested_output_ids
        | {record["query_run_id"]},
        "validation": claim_ids
        | operation_ids
        | requested_output_ids
        | set(retrieval_run_ids)
        | rule_ids
        | {
            record["query_run_id"],
            contract["requested"]["operation_graph"]["operation_graph_id"],
        },
    }
    completed_retrieval_run_ids = {
        retrieval_run["retrieval_run_id"]
        for retrieval_run in record["retrieval_runs"]
        if retrieval_run["status"] == "completed"
    }
    graph_id = contract["requested"]["operation_graph"]["operation_graph_id"]
    claim_evidence_ids = {
        evidence_id
        for claim in claims_by_id.values()
        for evidence_id in claim["evidence_ids"]
    }

    def expected_validator_subjects(validator_id: str, stage: str) -> set[str]:
        if validator_id == "operator_preserved":
            return set(operation_ids)
        if validator_id == "hard_scope_not_expanded":
            if stage == "intent":
                return {graph_id}
            if stage == "retrieval":
                return set(completed_retrieval_run_ids)
            return {graph_id, *completed_retrieval_run_ids}
        if validator_id == "output_contract_match":
            subjects = set(requested_output_ids)
            if stage in {"generation", "validation"}:
                subjects.update(claim_ids)
            return subjects
        if validator_id == "answer_sources_are_excluded":
            return set(document_ids) if stage == "retrieval" else set(claim_ids)
        if validator_id in {
            "claims_supported_by_evidence",
            "unresolved_never_promoted",
            "causality_requires_source_relation",
        }:
            return set(claim_ids)
        if validator_id in {
            "evidence_is_read_only",
            "estimated_not_exact",
            "unit_requires_evidence",
            "compatible_evidence_only",
            "provenance_required",
        }:
            return (
                set(bundle_evidence_ids)
                if stage == "retrieval"
                else set(claim_ids)
            )
        return set()

    def expected_validator_evidence(validator_id: str, stage: str) -> set[str]:
        if validator_id in QUERY_ONLY_VALIDATOR_IDS:
            return set()
        if stage == "retrieval":
            return set(bundle_evidence_ids)
        return set(claim_evidence_ids)

    for index, result in enumerate(record["forbidden_check_results"]):
        pair = (result["rule_id"], result["stage"])
        if pair in result_pairs:
            errors.append(f"duplicate forbidden_check_result for {pair}")
        result_pairs.add(pair)
        rule = rules_by_id.get(result["rule_id"])
        if rule is None:
            errors.append(
                f"forbidden_check_results[{index}] has dangling rule_id {result['rule_id']!r}"
            )
        else:
            rule_validator_id = rule["check"]["validator_id"]
            expected_rule_stages = VALIDATOR_STAGES_BY_ID.get(
                rule_validator_id, set()
            )
            if result["stage"] not in expected_rule_stages:
                errors.append(
                    f"forbidden_check_results[{index}] stage is not registered for "
                    "its rule validator"
                )
            if result["validator_id"] != rule_validator_id:
                errors.append(
                    f"forbidden_check_results[{index}] validator_id differs from its rule"
                )
            expected_action = rule["on_violation"]
            if result["status"] == "pass" and result["action_taken"] != "none":
                errors.append(
                    f"forbidden_check_results[{index}] pass status requires action_taken=none"
                )
            if result["status"] == "violation":
                violated_rule_ids.add(result["rule_id"])
                if result["action_taken"] != expected_action:
                    errors.append(
                        f"forbidden_check_results[{index}] violation action differs from rule"
                    )
            if result["status"] == "error" and result["action_taken"] not in {
                "reject", "abstain"
            }:
                errors.append(
                    f"forbidden_check_results[{index}] error must fail closed"
                )
        if result["validator_id"] not in KNOWN_VALIDATOR_IDS:
            errors.append(
                f"forbidden_check_results[{index}] uses unknown validator_id "
                f"{result['validator_id']!r}"
            )
        elif result["stage"] not in VALIDATOR_STAGES_BY_ID[result["validator_id"]]:
            errors.append(
                f"forbidden_check_results[{index}] stage is not registered for "
                f"validator_id {result['validator_id']!r}"
            )
        if result["validator_version"] != VALIDATOR_IMPLEMENTATION_VERSION:
            errors.append(
                f"forbidden_check_results[{index}] validator_version must be "
                f"{VALIDATOR_IMPLEMENTATION_VERSION!r}"
            )
        _add_dangling_refs(
            errors,
            f"forbidden_check_results[{index}].subject_refs",
            result["subject_refs"],
            known_subject_refs,
        )
        if (
            result["validator_id"] not in KNOWN_VALIDATOR_IDS
            and not set(result["subject_refs"])
            & subject_refs_by_stage[result["stage"]]
        ):
            errors.append(
                f"forbidden_check_results[{index}].subject_refs lacks a subject "
                f"from the {result['stage']!r} stage domain"
            )
        if result["validator_id"] in KNOWN_VALIDATOR_IDS:
            expected_subject_refs = expected_validator_subjects(
                result["validator_id"], result["stage"]
            )
            if set(result["subject_refs"]) != expected_subject_refs:
                errors.append(
                    f"forbidden_check_results[{index}].subject_refs must cover "
                    f"exactly the registered subjects for validator_id "
                    f"{result['validator_id']!r}: {sorted(expected_subject_refs)}"
                )
        _add_dangling_refs(
            errors,
            f"forbidden_check_results[{index}].evidence_ids",
            result["evidence_ids"],
            evidence_ids,
        )
        if result["validator_id"] in KNOWN_VALIDATOR_IDS:
            expected_evidence_ids = expected_validator_evidence(
                result["validator_id"], result["stage"]
            )
            if set(result["evidence_ids"]) != expected_evidence_ids:
                errors.append(
                    f"forbidden_check_results[{index}].evidence_ids must cover "
                    f"exactly the registered Evidence for validator_id "
                    f"{result['validator_id']!r}: {sorted(expected_evidence_ids)}"
                )
        result_statuses.append(result["status"])
        result_actions.append(result["action_taken"])

    for rule in rules:
        validator_id = rule["check"]["validator_id"]
        for stage in VALIDATOR_STAGES_BY_ID.get(validator_id, set()):
            executed = stage_statuses[stage_status_key[stage]] != "skipped"
            pair = (rule["rule_id"], stage)
            if executed and pair not in result_pairs:
                errors.append(
                    f"missing forbidden_check_result for rule {rule['rule_id']!r} "
                    f"at stage {stage!r}"
                )
            if not executed and pair in result_pairs:
                errors.append(
                    f"forbidden_check_result exists for skipped stage {stage!r} "
                    f"and rule {rule['rule_id']!r}"
                )

    output_validation = record["output_validation"]
    if output_validation is not None:
        violation_ids = output_validation["checks"]["forbidden_violations"]
        _add_dangling_refs(
            errors,
            "output_validation forbidden_violations",
            violation_ids,
            rule_ids,
        )
        unconfirmed = sorted(set(violation_ids) - violated_rule_ids)
        if unconfirmed:
            errors.append(
                f"output_validation names forbidden violations without violation results: {unconfirmed}"
            )
        if output_validation["status"] == "pass" and output_validation["action"] != "accept":
            errors.append("output_validation pass status requires action=accept")
        if output_validation["status"] == "pass":
            scalar_checks = {
                key: value
                for key, value in output_validation["checks"].items()
                if key != "forbidden_violations"
            }
            if any(value != "pass" for value in scalar_checks.values()):
                errors.append(
                    "output_validation pass status requires every individual check to pass"
                )
            if violation_ids:
                errors.append(
                    "output_validation pass status requires forbidden_violations to be empty"
                )
        if output_validation["action"] == "accept" and output_validation["status"] != "pass":
            errors.append("output_validation action=accept requires pass status")
        if output_validation["action"] == "regenerate":
            errors.append("completed query_run must not end with action=regenerate")

    answerability = record["answerability_gate"]
    if answerability is not None:
        if answerability["status"] == "pass" and answerability["action"] != "answer":
            errors.append("answerability_gate pass status requires action=answer")
        if answerability["status"] == "pass" and any(
            check["status"] != "pass" for check in answerability["checks"]
        ):
            errors.append(
                "answerability_gate pass status requires every individual check to pass"
            )
        if answerability["status"] == "pass" and answerability["reason_codes"]:
            errors.append("answerability_gate pass status requires empty reason_codes")
        if answerability["action"] == "answer" and answerability["status"] != "pass":
            errors.append("answerability_gate action=answer requires pass status")

    intent_gate = record["intent_gate"]
    if intent_gate is not None:
        if intent_gate["status"] == "pass" and intent_gate["action"] != "retrieve":
            errors.append("intent_gate pass status requires action=retrieve")
        if intent_gate["status"] == "pass" and any(
            check["status"] != "pass" for check in intent_gate["checks"]
        ):
            errors.append("intent_gate pass status requires every individual check to pass")
        if intent_gate["status"] == "pass" and intent_gate["reason_codes"]:
            errors.append("intent_gate pass status requires empty reason_codes")
        if intent_gate["action"] == "retrieve" and intent_gate["status"] != "pass":
            errors.append("intent_gate action=retrieve requires pass status")

    final_status = record["final_status"]
    stage_values = list(stage_statuses.values())
    if final_status == "accepted":
        if any(status != "completed" for status in stage_values):
            errors.append("accepted query_run requires every stage to be completed")

        requested = contract["requested"]
        requested_graph = requested["operation_graph"]
        if requested["target"]["canonical_type"] is None:
            errors.append(
                "accepted query_run requires requested.target.canonical_type"
            )
        if requested["scope"]["source"] == "unknown":
            errors.append("accepted query_run cannot use unknown requested.scope.source")
        if requested["scope"]["match_mode"] == "unknown":
            errors.append(
                "accepted query_run cannot use unknown requested.scope.match_mode"
            )
        for external_input in requested_graph["external_inputs"]:
            if external_input["input_type"] == "unknown":
                errors.append(
                    f"accepted query_run cannot use unknown input_type for "
                    f"{external_input['input_ref']!r}"
                )
            if external_input["source"] == "unknown":
                errors.append(
                    f"accepted query_run cannot use unknown input source for "
                    f"{external_input['input_ref']!r}"
                )
        for node in requested_graph["nodes"]:
            if node["operator"] == "unknown":
                errors.append(
                    f"accepted query_run cannot use unknown operator for "
                    f"{node['operation_id']!r}"
                )
        for output in requested_outputs:
            if output["return_field"] == "unknown":
                errors.append(
                    f"accepted query_run cannot use unknown return_field for "
                    f"{output['output_id']!r}"
                )
            if output["cardinality"]["mode"] == "unknown":
                errors.append(
                    f"accepted query_run cannot use unknown cardinality for "
                    f"{output['output_id']!r}"
                )
            if output["answer_shape"]["container"] == "unknown":
                errors.append(
                    f"accepted query_run cannot use unknown answer container for "
                    f"{output['output_id']!r}"
                )
            if output["answer_shape"]["value_type"] == "unknown":
                errors.append(
                    f"accepted query_run cannot use unknown answer value_type for "
                    f"{output['output_id']!r}"
                )
        derived_summary = requested["derived_summary"]
        if derived_summary["operation"] == "unknown":
            errors.append(
                "accepted query_run cannot use unknown derived_summary.operation"
            )
        if derived_summary["cardinality"] == "unknown":
            errors.append(
                "accepted query_run cannot use unknown derived_summary.cardinality"
            )
        if "unknown" in derived_summary["return_fields"]:
            errors.append(
                "accepted query_run cannot use unknown derived_summary.return_fields"
            )
        for ambiguity_index, ambiguity in enumerate(contract["ambiguity"]):
            if ambiguity["impact"] == "high":
                errors.append(
                    f"accepted query_run cannot retain high-impact ambiguity at "
                    f"ambiguity[{ambiguity_index}]"
                )
            unresolved_resolutions = {
                "retrieve_parallel",
                "resolve_from_evidence",
                "answer_with_qualification",
                "abstain",
            } & set(ambiguity["resolution"])
            if unresolved_resolutions:
                errors.append(
                    f"accepted query_run cannot retain unresolved ambiguity "
                    f"resolutions at ambiguity[{ambiguity_index}]: "
                    f"{sorted(unresolved_resolutions)}"
                )
        ambiguous_branches = sorted(
            evaluation["branch_id"]
            for evaluation in record["candidate_evaluations"]
            if evaluation["status"] == "ambiguous"
        )
        if ambiguous_branches:
            errors.append(
                f"accepted query_run cannot retain ambiguous candidate evaluations: "
                f"{ambiguous_branches}"
            )

        if record["intent_gate"] is None or record["intent_gate"]["status"] != "pass":
            errors.append("accepted query_run requires a passing intent_gate")
        if primary is None:
            errors.append("accepted query_run requires primary_query_path")
        else:
            completed_retrieval_branches = {
                retrieval_run["branch_id"]
                for retrieval_run in record["retrieval_runs"]
                if retrieval_run["status"] == "completed"
            }
            bundle_branches = {
                bundle["query_branch_id"]
                for bundle in record["retrieved_evidence_bundles"]
            }
            if primary["branch_id"] not in completed_retrieval_branches:
                errors.append(
                    "accepted primary branch requires a completed retrieval_run"
                )
            if primary["branch_id"] not in bundle_branches:
                errors.append(
                    "accepted primary branch requires a retrieved Evidence bundle"
                )
            if not primary["evidence_ids"]:
                errors.append(
                    "accepted primary_query_path requires evidence_ids"
                )
            _add_dangling_refs(
                errors,
                "accepted primary_query_path.evidence_ids",
                primary["evidence_ids"],
                primary_admissible_evidence_ids,
            )
            primary_candidate = candidate_paths_by_branch.get(primary["branch_id"])
            primary_evaluation = evaluation_by_branch.get(primary["branch_id"])
            primary_evidence_ids = set(primary["evidence_ids"])
            if (
                primary_candidate is not None
                and not primary_evidence_ids.issubset(
                    set(primary_candidate["evidence_ids"])
                )
            ):
                errors.append(
                    "accepted primary_query_path.evidence_ids must be contained "
                    "in the primary candidate path evidence_ids"
                )
            if (
                primary_evaluation is not None
                and not primary_evidence_ids.issubset(
                    set(primary_evaluation["evidence_ids"])
                )
            ):
                errors.append(
                    "accepted primary_query_path.evidence_ids must be contained "
                    "in the primary candidate evaluation evidence_ids"
                )
        if proof is None or proof["overall"]["status"] != "satisfied":
            errors.append("accepted query_run requires satisfied proof_obligation")
        else:
            required_by_output: dict[str, list[dict[str, Any]]] = {
                output_id: [] for output_id in requested_output_ids
            }
            for requirement in proof["requirements"]:
                if requirement["required"] and requirement["output_ref"] in required_by_output:
                    required_by_output[requirement["output_ref"]].append(requirement)
                if requirement["required"] and not requirement["evidence_ids"]:
                    errors.append(
                        f"accepted required proof {requirement['requirement_id']!r} "
                        "requires evidence"
                    )
                _add_dangling_refs(
                    errors,
                    f"accepted proof requirement {requirement['requirement_id']!r} "
                    "evidence_ids",
                    requirement["evidence_ids"],
                    primary_admissible_evidence_ids,
                )
            for output_id, requirements in required_by_output.items():
                if not requirements:
                    errors.append(
                        f"accepted proof lacks a required requirement for output {output_id!r}"
                    )
                    continue
                expected_operation_ref = requested_outputs_by_id[output_id][
                    "source_operation_ref"
                ]
                if any(
                    requirement["operation_ref"] != expected_operation_ref
                    for requirement in requirements
                ):
                    errors.append(
                        f"accepted proof operation_ref does not match requested output "
                        f"{output_id!r}"
                    )
            coverage = proof["coverage"]
            _add_dangling_refs(
                errors,
                "accepted proof coverage evidence_ids",
                coverage["evidence_ids"],
                primary_admissible_evidence_ids,
            )
            all_outputs = [
                output
                for output in requested_outputs
                if output["cardinality"]["mode"] == "all"
            ]
            count_outputs = [
                output
                for output in requested_outputs
                if output["return_field"] == "count"
                or operation_nodes_by_id.get(
                    output["source_operation_ref"], {}
                ).get("operator")
                == "count"
            ]
            if all_outputs and coverage["method"] not in {
                "full_scan",
                "authoritative_enumeration",
            }:
                errors.append(
                    "accepted cardinality=all output requires full_scan or "
                    "authoritative_enumeration coverage"
                )
            if count_outputs and coverage["method"] not in {
                "full_scan",
                "authoritative_aggregate",
                "authoritative_enumeration",
            }:
                errors.append(
                    "accepted count output requires authoritative exhaustive coverage"
                )
            if all_outputs or count_outputs:
                if not coverage["evidence_ids"]:
                    errors.append(
                        "accepted all/count output requires evidence-backed coverage"
                    )
                scanned_count = coverage["scanned_count"]
                matched_count = coverage["matched_count"]
                scan_counts_required = bool(all_outputs) or coverage["method"] in {
                    "full_scan",
                    "authoritative_enumeration",
                }
                if scan_counts_required and not coverage["exhaustive"]:
                    errors.append(
                        "accepted enumerated all/count output requires exhaustive "
                        "proof coverage"
                    )
                if scan_counts_required and (
                    scanned_count is None or matched_count is None
                ):
                    errors.append(
                        "accepted enumerated all/count output requires non-null "
                        "coverage counts"
                    )
                if (
                    coverage["method"] == "authoritative_aggregate"
                    and matched_count is None
                ):
                    errors.append(
                        "authoritative_aggregate count coverage requires matched_count"
                    )
                if (
                    scanned_count is not None
                    and matched_count is not None
                    and matched_count > scanned_count
                ):
                    errors.append(
                        "proof coverage matched_count cannot exceed scanned_count"
                    )
                if matched_count is not None:
                    for output in all_outputs:
                        expected_count = output["cardinality"]["expected_count"]
                        if (
                            expected_count is not None
                            and expected_count != matched_count
                        ):
                            errors.append(
                                f"proof coverage matched_count differs from expected_count "
                                f"for output {output['output_id']!r}"
                            )
            if primary_branch_id is not None:
                primary_completed_plans = [
                    retrieval_run["plan"]
                    for retrieval_run in record["retrieval_runs"]
                    if retrieval_run["branch_id"] == primary_branch_id
                    and retrieval_run["status"] == "completed"
                ]
                coverage_method = coverage["method"]
                matching_plan = any(
                    (
                        coverage_method == "full_scan"
                        and plan["coverage_requirement"] == "exhaustive"
                        and plan["scan_mode"] == "exhaustive"
                    )
                    or (
                        coverage_method == "authoritative_enumeration"
                        and plan["coverage_requirement"]
                        == "authoritative_enumeration"
                        and plan["scan_mode"] == "exhaustive"
                    )
                    or (
                        coverage_method == "authoritative_aggregate"
                        and plan["coverage_requirement"]
                        == "authoritative_aggregate"
                    )
                    or (
                        coverage_method == "none"
                        and plan["coverage_requirement"] == "none"
                    )
                    for plan in primary_completed_plans
                )
                if not matching_plan:
                    errors.append(
                        "accepted proof coverage method is inconsistent with the "
                        "primary branch retrieval plan"
                    )
        if answerability is None or answerability["status"] != "pass" or answerability["action"] != "answer":
            errors.append("accepted query_run requires answerability pass/answer")
        if answer_plan is None:
            errors.append("accepted query_run requires answer_plan")
        else:
            if (
                set(plan["output_id"] for plan in answer_plan["output_plans"])
                != requested_output_ids
            ):
                errors.append(
                    "accepted query_run must plan every requested output exactly once"
                )
            if set(answer_plan["forbidden_rule_ids"]) != rule_ids:
                errors.append(
                    "accepted answer_plan.forbidden_rule_ids must contain every "
                    "contract rule exactly"
                )
            used_claim_ids: set[str] = set()
            for plan in answer_plan["output_plans"]:
                plan_claim_ids = plan["allowed_claim_ids"]
                used_claim_ids.update(plan_claim_ids)
                if not plan_claim_ids:
                    errors.append(
                        f"accepted output plan {plan['output_id']!r} requires at "
                        "least one allowed claim"
                    )
                requested_output = requested_outputs_by_id.get(plan["output_id"])
                if requested_output is not None:
                    requested_shape = requested_output["answer_shape"]
                    requested_unit = requested_shape["unit"]
                    shape_mismatches = sorted(
                        claim_id
                        for claim_id in plan_claim_ids
                        if claim_id in claims_by_id
                        and not _claim_value_matches_shape(
                            claims_by_id[claim_id]["value"], requested_shape
                        )
                    )
                    if shape_mismatches:
                        errors.append(
                            f"accepted output plan {plan['output_id']!r} has "
                            f"claims whose values do not match answer_shape: "
                            f"{shape_mismatches}"
                        )
                    mismatched_units = sorted(
                        claim_id
                        for claim_id in plan_claim_ids
                        if claim_id in claims_by_id
                        and claims_by_id[claim_id]["unit"] != requested_unit
                    )
                    if mismatched_units:
                        errors.append(
                            f"accepted output plan {plan['output_id']!r} has "
                            f"claims with an added or mismatched unit: "
                            f"{mismatched_units}"
                        )
                    matched_count = (
                        proof["coverage"]["matched_count"]
                        if proof is not None
                        else None
                    )
                    cardinality_mode = requested_output["cardinality"]["mode"]
                    if (
                        cardinality_mode in {"all", "multiple"}
                        and requested_output["return_field"] == "identifier"
                    ):
                        identifier_items = [
                            item
                            for claim_id in plan_claim_ids
                            if claim_id in claims_by_id
                            and isinstance(claims_by_id[claim_id]["value"], list)
                            for item in claims_by_id[claim_id]["value"]
                        ]
                        canonical_identifier_keys = [
                            _canonical_identifier_key(item)
                            for item in identifier_items
                        ]
                        if len(canonical_identifier_keys) != len(
                            set(canonical_identifier_keys)
                        ):
                            errors.append(
                                f"accepted {cardinality_mode} identifier output "
                                f"{plan['output_id']!r} contains duplicate Claim "
                                "list items"
                            )
                    if (
                        cardinality_mode in {"all", "multiple"}
                        and matched_count is not None
                    ):
                        list_item_count = sum(
                            len(claims_by_id[claim_id]["value"])
                            for claim_id in plan_claim_ids
                            if claim_id in claims_by_id
                            and isinstance(claims_by_id[claim_id]["value"], list)
                        )
                        if list_item_count != matched_count:
                            errors.append(
                                f"accepted {cardinality_mode} output "
                                f"{plan['output_id']!r} Claim "
                                "list item count differs from coverage.matched_count"
                            )
                    is_count_output = (
                        requested_output["return_field"] == "count"
                        or operation_nodes_by_id.get(
                            requested_output["source_operation_ref"], {}
                        ).get("operator")
                        == "count"
                    )
                    if is_count_output and matched_count is not None:
                        mismatched_count_claims = sorted(
                            claim_id
                            for claim_id in plan_claim_ids
                            if claim_id in claims_by_id
                            and claims_by_id[claim_id]["value"] != matched_count
                        )
                        if mismatched_count_claims:
                            errors.append(
                                f"accepted count output {plan['output_id']!r} has "
                                "Claim values that differ from coverage.matched_count: "
                                f"{mismatched_count_claims}"
                            )
            unused_claim_ids = sorted(claim_ids - used_claim_ids)
            if unused_claim_ids:
                errors.append(
                    f"accepted answer_plan has unused allowed claims: "
                    f"{unused_claim_ids}"
                )
            for claim in answer_plan["allowed_claims"]:
                if claim["exactness"] == "unresolved":
                    errors.append(
                        "accepted query_run must not allow unresolved claims"
                    )
                _add_dangling_refs(
                    errors,
                    f"accepted claim {claim['claim_id']!r} evidence_ids",
                    claim["evidence_ids"],
                    primary_admissible_evidence_ids,
                )
                if claim["exactness"] == "exact":
                    _add_dangling_refs(
                        errors,
                        f"exact claim {claim['claim_id']!r} evidence_ids",
                        claim["evidence_ids"],
                        primary_exact_evidence_ids
                        & primary_admissible_evidence_ids,
                    )
        if output_validation is None or output_validation["status"] != "pass" or output_validation["action"] != "accept":
            errors.append("accepted query_run requires output validation pass/accept")
        if not isinstance(record["final_answer"], str) or not record["final_answer"]:
            errors.append("accepted query_run requires a non-empty final_answer")
        if record["errors"]:
            errors.append("accepted query_run must not contain errors")
        if any(status != "pass" for status in result_statuses):
            errors.append("accepted query_run requires every forbidden check to pass")
    elif final_status == "abstained":
        if output_validation is not None and output_validation["action"] == "accept":
            errors.append("abstained query_run must not accept output")
        abstain_signal = (
            (record["intent_gate"] is not None and record["intent_gate"]["action"] in {"clarify", "abstain"})
            or (proof is not None and proof["overall"]["status"] != "satisfied")
            or (answerability is not None and answerability["action"] == "abstain")
            or (output_validation is not None and output_validation["action"] == "abstain")
            or any(action == "abstain" for action in result_actions)
        )
        if not abstain_signal:
            errors.append("abstained query_run lacks an abstention reason")
    else:
        if "failed" not in stage_values:
            errors.append("failed query_run requires at least one failed stage")
        if not record["errors"]:
            errors.append("failed query_run requires an error record")
        if output_validation is not None and output_validation["action"] == "accept":
            errors.append("failed query_run must not accept output")
    return errors


def _question_understanding_reserved_key_paths(
    value: object, path: str = "root"
) -> list[str]:
    paths: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{path}.{key}"
            if key in QUESTION_UNDERSTANDING_FORBIDDEN_KEYS:
                paths.append(child_path)
            paths.extend(_question_understanding_reserved_key_paths(child, child_path))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            paths.extend(
                _question_understanding_reserved_key_paths(
                    child, f"{path}[{index}]"
                )
            )
    return paths


def _decode_json_pointer(path: str) -> list[str]:
    if not path.startswith("/"):
        raise ValueError("JSON Pointer must start with '/'")
    result: list[str] = []
    for raw_part in path[1:].split("/"):
        part = ""
        index = 0
        while index < len(raw_part):
            if raw_part[index] != "~":
                part += raw_part[index]
                index += 1
                continue
            if index + 1 >= len(raw_part) or raw_part[index + 1] not in {"0", "1"}:
                raise ValueError(f"invalid JSON Pointer escape in {path!r}")
            part += "~" if raw_part[index + 1] == "0" else "/"
            index += 2
        result.append(part)
    return result


def _json_pointer_get(root: object, path: str) -> object:
    current = root
    for part in _decode_json_pointer(path):
        if isinstance(current, dict):
            if part not in current:
                raise KeyError(part)
            current = current[part]
        elif isinstance(current, list):
            if not part.isdigit() or (len(part) > 1 and part.startswith("0")):
                raise KeyError(part)
            index = int(part)
            if index >= len(current):
                raise IndexError(index)
            current = current[index]
        else:
            raise KeyError(part)
    return current


def _json_pointer_replace(root: object, path: str, value: object) -> None:
    parts = _decode_json_pointer(path)
    if not parts:
        raise ValueError("root replacement is forbidden")
    current = root
    for part in parts[:-1]:
        if isinstance(current, dict):
            if part not in current:
                raise KeyError(part)
            current = current[part]
        elif isinstance(current, list):
            if not part.isdigit() or (len(part) > 1 and part.startswith("0")):
                raise KeyError(part)
            index = int(part)
            if index >= len(current):
                raise IndexError(index)
            current = current[index]
        else:
            raise KeyError(part)
    final = parts[-1]
    replacement = copy.deepcopy(value)
    if isinstance(current, dict):
        if final not in current:
            raise KeyError(final)
        current[final] = replacement
    elif isinstance(current, list):
        if not final.isdigit() or (len(final) > 1 and final.startswith("0")):
            raise KeyError(final)
        index = int(final)
        if index >= len(current):
            raise IndexError(index)
        current[index] = replacement
    else:
        raise KeyError(final)


def _pointer_is_within(path: str, container_path: str) -> bool:
    path_parts = _decode_json_pointer(path)
    container_parts = _decode_json_pointer(container_path)
    return path_parts[: len(container_parts)] == container_parts


def _pointers_overlap(left: str, right: str) -> bool:
    left_parts = _decode_json_pointer(left)
    right_parts = _decode_json_pointer(right)
    shorter = min(len(left_parts), len(right_parts))
    return left_parts[:shorter] == right_parts[:shorter]


def _recompute_requested_summary(requested: dict[str, Any]) -> dict[str, Any]:
    nodes = requested["operation_graph"]["nodes"]
    nodes_by_id = {node["operation_id"]: node for node in nodes}
    edges = {
        (edge["from"], edge["to"])
        for edge in requested["operation_graph"]["edges"]
    }
    outputs = requested["requested_outputs"]
    return_fields = list(dict.fromkeys(output["return_field"] for output in outputs))
    cardinalities = {output["cardinality"]["mode"] for output in outputs}
    cardinality = next(iter(cardinalities)) if len(cardinalities) == 1 else "mixed"
    return {
        "operation": _derived_operation(nodes_by_id, edges, outputs),
        "return_fields": return_fields,
        "cardinality": cardinality,
    }


def _validate_question_context_graph(
    record: dict[str, Any],
) -> tuple[list[str], set[str], dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    errors: list[str] = []
    if EXPLICIT_KIND_SURFACE_OVERLAP:
        errors.append(
            "question-explicit cardinality and operator dictionaries overlap: "
            f"{sorted(EXPLICIT_KIND_SURFACE_OVERLAP)!r}"
        )
    graph = record["query_context_graph"]
    if graph is None:
        return errors, set(), {}, {}

    sources = graph["sources"]
    nodes = graph["nodes"]
    accepted_edges = graph["edges"]
    rejected = graph["rejected_context"]
    rejected_edges = [item["edge"] for item in rejected]

    source_ids = [source["source_id"] for source in sources]
    node_ids = [node["node_id"] for node in nodes]
    edge_ids = [edge["edge_id"] for edge in [*accepted_edges, *rejected_edges]]
    for label, values in (
        ("context source_id", source_ids),
        ("context node_id", node_ids),
        ("context edge_id", edge_ids),
    ):
        duplicates = _duplicates(values)
        if duplicates:
            errors.append(f"duplicate {label}: {duplicates}")
    cross_kind_duplicates = _duplicates([*source_ids, *node_ids, *edge_ids])
    if cross_kind_duplicates:
        errors.append(
            "context source, node, and edge identifiers must be disjoint: "
            f"{cross_kind_duplicates}"
        )

    sources_by_id = {source["source_id"]: source for source in sources}
    nodes_by_id = {node["node_id"]: node for node in nodes}
    all_edges_by_id = {
        edge["edge_id"]: edge for edge in [*accepted_edges, *rejected_edges]
    }
    original_question = record["original_question"]
    question_hash = hashlib.sha256(original_question.encode("utf-8")).hexdigest()
    for index, source in enumerate(sources):
        source_label = f"query_context_graph.sources[{index}]"
        if _source_ref_is_forbidden(source["source_ref"]):
            errors.append(f"{source_label}.source_ref uses a forbidden answer source")
        if source["content_sha256"] is None:
            errors.append(f"{source_label}.content_sha256 must be recorded")
        span = source["span"]
        if source["source_type"] == "question_explicit":
            if source["content_sha256"] != question_hash:
                errors.append(
                    f"{source_label}.content_sha256 must match original_question"
                )
            if span is None:
                errors.append(f"{source_label}.span is required for question_explicit")
        if span is not None:
            if span["start"] >= span["end"]:
                errors.append(f"{source_label}.span start must be before end")
            if source["source_type"] == "question_explicit":
                if span["end"] > len(original_question):
                    errors.append(f"{source_label}.span exceeds original_question")
                elif original_question[span["start"] : span["end"]] != span["text"]:
                    errors.append(
                        f"{source_label}.span text does not match original_question"
                    )

    node_id_set = set(node_ids)
    source_id_set = set(source_ids)
    all_edge_id_set = set(edge_ids)
    for edge_kind, edges in (("edges", accepted_edges), ("rejected_context", rejected_edges)):
        for index, edge in enumerate(edges):
            prefix = f"query_context_graph.{edge_kind}[{index}]"
            if edge["from_ref"] not in node_id_set:
                errors.append(f"{prefix}.from_ref is dangling: {edge['from_ref']!r}")
            if edge["to_ref"] not in node_id_set:
                errors.append(f"{prefix}.to_ref is dangling: {edge['to_ref']!r}")
            source = sources_by_id.get(edge["source_ref"])
            if source is None:
                errors.append(f"{prefix}.source_ref is dangling: {edge['source_ref']!r}")
            elif edge["source_type"] != source["source_type"]:
                errors.append(f"{prefix}.source_type differs from its source_ref")

    question_mentions: list[dict[str, Any]] = []
    seen_question_mentions: set[tuple[int, int, str, str]] = set()
    for edge_kind, edges in (("edges", accepted_edges), ("rejected_context", rejected_edges)):
        for index, edge in enumerate(edges):
            prefix = f"query_context_graph.{edge_kind}[{index}]"
            source = sources_by_id.get(edge["source_ref"])
            if source is None or source["source_type"] != "question_explicit":
                continue
            from_node = nodes_by_id.get(edge["from_ref"])
            to_node = nodes_by_id.get(edge["to_ref"])
            span = source["span"]
            if from_node is None or to_node is None or span is None:
                continue
            if edge["relation"] != "specifies":
                errors.append(f"{prefix} question-explicit relation must be 'specifies'")
            if edge["support_level"] != "high":
                errors.append(f"{prefix} question-explicit support_level must be 'high'")
            if edge["match_kind"] != "exact_value":
                errors.append(f"{prefix} question-explicit match_kind must be 'exact_value'")
            slot_kind = to_node.get("canonical_value")
            expected_node_type = QUESTION_MENTION_NODE_TYPES.get(slot_kind)
            if expected_node_type is None:
                errors.append(f"{prefix} has an unsupported question-explicit slot kind")
                continue
            if to_node.get("surface") is not None:
                errors.append(f"{prefix} question-explicit slot node surface must be null")
            if to_node.get("node_type") != expected_node_type:
                errors.append(
                    f"{prefix} slot node_type does not match slot kind {slot_kind!r}"
                )
            if from_node.get("node_type") != expected_node_type:
                errors.append(
                    f"{prefix} mention node_type does not match slot kind {slot_kind!r}"
                )
            if (
                from_node.get("surface") != span["text"]
                or from_node.get("canonical_value") != span["text"]
            ):
                errors.append(
                    f"{prefix} mention surface/canonical_value must exactly match "
                    "its question source span"
                )
            identity = (span["start"], span["end"], span["text"], slot_kind)
            if identity in seen_question_mentions:
                errors.append(f"{prefix} duplicates a question-explicit mention")
            seen_question_mentions.add(identity)
            if (
                span["text"] in EXPLICIT_CARDINALITY_SURFACES
                and slot_kind != "cardinality"
            ):
                errors.append(
                    f"{prefix} labels an explicit cardinality token with the wrong kind"
                )
            if span["text"] in EXPLICIT_OPERATOR_SURFACES and slot_kind != "operator":
                errors.append(
                    f"{prefix} labels an explicit comparison token with the wrong kind"
                )
            if edge_kind == "edges":
                question_mentions.append(
                    {
                        "start": span["start"],
                        "end": span["end"],
                        "surface": span["text"],
                        "kind": slot_kind,
                    }
                )

    kinds_by_question_span: dict[tuple[int, int, str], set[str]] = {}
    for mention in question_mentions:
        kinds_by_question_span.setdefault(
            (mention["start"], mention["end"], mention["surface"]), set()
        ).add(mention["kind"])
    for span_identity, kinds in kinds_by_question_span.items():
        if (
            len(kinds) > 1
            and frozenset(kinds) not in ALLOWED_MULTI_KIND_QUESTION_SPANS
        ):
            errors.append(
                "question-explicit span has incompatible semantic roles: "
                f"span={span_identity!r}, kinds={sorted(kinds)!r}"
            )

    accepted_by_id = {edge["edge_id"]: edge for edge in accepted_edges}
    for index, item in enumerate(rejected):
        edge = item["edge"]
        conflict_refs = item["conflicts_with_edge_refs"]
        dangling = sorted(set(conflict_refs) - all_edge_id_set)
        if dangling:
            errors.append(
                f"rejected_context[{index}].conflicts_with_edge_refs has dangling "
                f"references: {dangling}"
            )
        if edge["edge_id"] in conflict_refs:
            errors.append(f"rejected_context[{index}] cannot conflict with itself")
        reason = item["reason_code"]
        if reason in {
            "lower_priority_conflict",
            "explicit_conflict",
            "same_priority_conflict",
        } and not conflict_refs:
            errors.append(f"rejected_context[{index}] conflict reason requires refs")
        conflict_edges = [
            accepted_by_id[ref] for ref in conflict_refs if ref in accepted_by_id
        ]
        material_conflicts = [
            conflict
            for conflict in conflict_edges
            if conflict["to_ref"] == edge["to_ref"]
            and conflict["relation"] == edge["relation"]
            and _canonical_identifier_key(
                nodes_by_id.get(conflict["from_ref"], {}).get("canonical_value")
            )
            != _canonical_identifier_key(
                nodes_by_id.get(edge["from_ref"], {}).get("canonical_value")
            )
        ]
        if reason in {
            "lower_priority_conflict",
            "explicit_conflict",
            "same_priority_conflict",
        } and len(material_conflicts) != len(conflict_refs):
            errors.append(
                f"rejected_context[{index}] conflict refs must name accepted "
                "edges for the same slot/relation with a different value"
            )
        rejected_priority = CONTEXT_SOURCE_PRIORITY[edge["source_type"]]
        if reason == "lower_priority_conflict" and not any(
            CONTEXT_SOURCE_PRIORITY[conflict["source_type"]] < rejected_priority
            for conflict in material_conflicts
        ):
            errors.append(
                f"rejected_context[{index}] lacks a higher-priority accepted conflict"
            )
        if reason == "explicit_conflict" and not any(
            conflict["source_type"] == "question_explicit"
            for conflict in material_conflicts
        ):
            errors.append(
                f"rejected_context[{index}] lacks a question_explicit accepted conflict"
            )
        if reason == "same_priority_conflict" and not any(
            CONTEXT_SOURCE_PRIORITY[conflict["source_type"]] == rejected_priority
            for conflict in material_conflicts
        ):
            errors.append(
                f"rejected_context[{index}] lacks a same-priority accepted conflict"
            )

    accepted_groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for edge in accepted_edges:
        accepted_groups.setdefault((edge["to_ref"], edge["relation"]), []).append(edge)
    for group, edges in accepted_groups.items():
        values = {
            _canonical_identifier_key(
                nodes_by_id.get(edge["from_ref"], {}).get("canonical_value")
            )
            for edge in edges
        }
        priorities = {
            CONTEXT_SOURCE_PRIORITY[edge["source_type"]] for edge in edges
        }
        if len(values) > 1 and len(priorities) > 1:
            errors.append(
                "accepted context cannot retain a lower-priority conflicting edge "
                f"for slot/relation {group!r}"
            )

    # Undeclared same-priority alternatives are unresolved intent rather than
    # malformed QCG structure.  _deterministic_intent_audit detects them from
    # the exact question mentions and forces hard_scope_not_expanded to fail.

    known_refs = {
        graph["graph_id"],
        *source_id_set,
        *node_id_set,
        *all_edge_id_set,
    }
    return errors, known_refs, nodes_by_id, all_edges_by_id


def _question_explicit_mentions(
    record: dict[str, Any], nodes_by_id: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    graph = record["query_context_graph"]
    if graph is None:
        return []
    sources_by_id = {
        source["source_id"]: source for source in graph["sources"]
    }
    mentions: list[dict[str, Any]] = []
    for edge in graph["edges"]:
        source = sources_by_id.get(edge["source_ref"])
        from_node = nodes_by_id.get(edge["from_ref"])
        to_node = nodes_by_id.get(edge["to_ref"])
        if (
            source is None
            or source["source_type"] != "question_explicit"
            or source["span"] is None
            or edge["relation"] != "specifies"
            or from_node is None
            or to_node is None
            or not isinstance(to_node["canonical_value"], str)
            or not isinstance(from_node["surface"], str)
        ):
            continue
        mentions.append(
            {
                "surface": from_node["surface"],
                "start": source["span"]["start"],
                "end": source["span"]["end"],
                "kind": to_node["canonical_value"],
                "source_ref": source["source_id"],
            }
        )
    return mentions


def _normalized_question_surface(value: object) -> str | None:
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return None


def _target_type_matches(value: str) -> set[str]:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    compact = re.sub(r"[\s_\-./:：]+", "", normalized)
    matches: list[tuple[int, str]] = []
    for canonical_type, lexemes in CANONICAL_TARGET_TYPE_LEXEMES.items():
        for lexeme in lexemes:
            normalized_lexeme = unicodedata.normalize("NFKC", lexeme).casefold()
            compact_lexeme = re.sub(r"[\s_\-./:：]+", "", normalized_lexeme)
            if not compact_lexeme:
                continue
            if compact_lexeme.endswith("id") and len(compact_lexeme) > 2:
                matched = compact.startswith(compact_lexeme) or compact.endswith(
                    compact_lexeme
                )
            elif all(character.isascii() for character in compact_lexeme):
                matched = _raw_surface_occurs(normalized, normalized_lexeme)
            else:
                matched = (
                    compact == compact_lexeme
                    if len(compact_lexeme) == 1
                    else compact_lexeme in compact
                )
            if matched:
                matches.append((len(compact_lexeme), canonical_type))
    if not matches:
        return set()
    longest = max(length for length, _canonical_type in matches)
    return {
        canonical_type
        for length, canonical_type in matches
        if length == longest
    }


def _expected_canonical_target_type(
    target: dict[str, Any],
    mentions: dict[str, list[dict[str, Any]]],
) -> str | None:
    values = [
        value
        for value in (target.get("surface"), target.get("instance"))
        if isinstance(value, str) and value
    ]
    inferred_from_alternative_mentions = not values
    if inferred_from_alternative_mentions:
        values = [
            mention["surface"]
            for kind in ("target_surface", "target_instance")
            for mention in mentions.get(kind, [])
        ]
    inferred: set[str] = set()
    for value in values:
        matches = _target_type_matches(value)
        if inferred_from_alternative_mentions and not matches:
            return None
        inferred.update(matches)
    return next(iter(inferred)) if len(inferred) == 1 else None


def _raw_scope_pairs(question: str) -> list[tuple[list[str], str]]:
    pairs: list[tuple[list[str], str]] = []
    for match in re.finditer(r"(?:^|[、。])([^、。\n]{1,96}?)において", question):
        expression = match.group(1).lstrip("かつ")
        if "の" not in expression:
            continue
        location_expression, container = expression.rsplit("の", 1)
        locations = [location_expression]
        for connector in ALTERNATIVE_CONNECTORS:
            if connector in location_expression:
                locations = [
                    item for item in location_expression.split(connector) if item
                ]
                break
        if locations and container:
            pairs.append((locations, container))
    return pairs


def _raw_operation_occurrences(
    question: str,
) -> dict[str, list[tuple[int, int]]]:
    occurrences: dict[str, list[tuple[int, int]]] = {}
    for operator, keywords in RAW_REQUIRED_OPERATION_KEYWORDS.items():
        spans: set[tuple[int, int]] = set()
        for keyword in keywords:
            if keyword.isascii() and keyword.replace(" ", "").isalnum():
                pattern = re.compile(
                    rf"(?<![A-Za-z0-9_]){re.escape(keyword)}(?![A-Za-z0-9_])",
                    flags=re.IGNORECASE,
                )
            else:
                pattern = re.compile(re.escape(keyword), flags=re.IGNORECASE)
            spans.update(
                (match.start(), match.end()) for match in pattern.finditer(question)
            )
        if spans:
            occurrences[operator] = sorted(spans)
    return occurrences


def _recursive_scalar_values(value: object) -> set[str]:
    result: set[str] = set()
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
        else:
            normalized = _normalized_question_surface(item)
            if normalized is not None:
                result.add(normalized)
    return result


def _intent_operators(requested: dict[str, Any]) -> set[str]:
    return {
        operation["operator"]
        for operation in requested["operation_graph"]["nodes"]
    }


def _intent_project_fields(requested: dict[str, Any]) -> set[str]:
    return {
        field
        for operation in requested["operation_graph"]["nodes"]
        for field in operation.get("fields", [])
    }


def _question_mentions_by_kind(
    mentions: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {}
    for mention in mentions:
        result.setdefault(mention["kind"], []).append(mention)
    return result


def _raw_question_operator_expectations(question: str) -> set[str]:
    expected: set[str] = set()
    symbolic = question
    for token, operator in (
        (">=", "gte"),
        ("<=", "lte"),
        ("!=", "ne"),
        ("==", "eq"),
    ):
        if token in symbolic:
            expected.add(operator)
            symbolic = symbolic.replace(token, " " * len(token))
    for token, operator in ((">", "gt"), ("<", "lt"), ("=", "eq")):
        if token in symbolic:
            expected.add(operator)
    lexical_question = symbolic
    precision_pattern = re.compile(
        r"(?:小数点|小数|有効数字)\s*(?:以下|以上)?\s*"
        r"(?:\d+|[零〇一二三四五六七八九十]+)\s*桁",
        flags=re.IGNORECASE,
    )
    lexical_question = precision_pattern.sub(
        lambda match: " " * len(match.group(0)), lexical_question
    )
    lexical_surfaces = sorted(
        {
            surface
            for surface in EXPLICIT_OPERATOR_SURFACES
            if surface not in {">=", "<=", "!=", "==", ">", "<", "="}
        },
        key=lambda value: (-len(value), value),
    )
    for surface in lexical_surfaces:
        operator = EXPLICIT_OPERATOR_SURFACES[surface]
        if _raw_surface_occurs(lexical_question, surface):
            expected.add(operator)
            lexical_question = re.sub(
                re.escape(surface),
                " " * len(surface),
                lexical_question,
                flags=re.IGNORECASE,
            )
    return expected


def _raw_cardinality_modes(question: str) -> dict[str, list[tuple[int, int]]]:
    surfaces_by_mode = {
        "all": EXPLICIT_ALL_CARDINALITY_SURFACES,
        "multiple": EXPLICIT_MULTIPLE_CARDINALITY_SURFACES,
        "single": EXPLICIT_SINGLE_CARDINALITY_SURFACES,
    }
    result: dict[str, list[tuple[int, int]]] = {}
    for mode, surfaces in surfaces_by_mode.items():
        spans: set[tuple[int, int]] = set()
        for surface in surfaces:
            if surface and all(
                character.isascii()
                and (character.isalnum() or character == "_")
                for character in surface
            ):
                pattern = re.compile(
                    rf"(?<![A-Za-z0-9_]){re.escape(surface)}(?![A-Za-z0-9_])",
                    flags=re.IGNORECASE,
                )
            else:
                pattern = re.compile(re.escape(surface), flags=re.IGNORECASE)
            spans.update(match.span() for match in pattern.finditer(question))
        if spans:
            result[mode] = sorted(spans)
    return result


def _raw_cardinality_conflicts(question: str) -> bool:
    if len(_raw_cardinality_modes(question)) < 2:
        return False
    patterns = (
        re.compile(r"ではなく|でなく|ではない|じゃなく"),
        re.compile(
            r"(?<![A-Za-z0-9_])rather\s+than(?![A-Za-z0-9_])",
            re.IGNORECASE,
        ),
        re.compile(
            r"(?<![A-Za-z0-9_])not(?![A-Za-z0-9_]).{0,24}"
            r"(?<![A-Za-z0-9_])but(?![A-Za-z0-9_])",
            re.IGNORECASE,
        ),
    )
    return any(pattern.search(question) is not None for pattern in patterns)


_RAW_EXCLUSION_PATTERN = re.compile(
    r"(?P<item>[^,、。\n]{1,64}?)(?:は|を)"
    r"(?:不要(?:です|だ)?|除外(?:して|する)?|除いて|"
    r"省いて|含めない|求めない|答えない)",
    flags=re.IGNORECASE,
)


def _raw_exclusion_items(question: str) -> set[str]:
    folded = question.casefold()
    if any(value.casefold() in folded for value in _RAW_EXCLUSION_REVERSALS):
        return set()
    return {
        match.group("item").strip()
        for match in _RAW_EXCLUSION_PATTERN.finditer(question)
        if match.group("item").strip()
    }


def _question_value_is_bound(
    value: object,
    kind: str,
    mentions: dict[str, list[dict[str, Any]]],
) -> bool:
    if isinstance(value, list):
        return bool(value) and all(
            _question_value_is_bound(item, kind, mentions) for item in value
        )
    normalized = _normalized_question_surface(value)
    return normalized is not None and any(
        _normalized_question_surface(mention["surface"]) == normalized
        for mention in mentions.get(kind, [])
    )


def _question_field_is_bound(
    field: str, mentions: dict[str, list[dict[str, Any]]]
) -> bool:
    return any(
        _question_value_is_bound(field, kind, mentions)
        for kind in (
            "target_surface",
            "target_instance",
            "filter_field",
            "return_field",
        )
    )


def _question_equality_relation_is_bound(
    question: str,
    predicate: dict[str, Any],
    mentions: dict[str, list[dict[str, Any]]],
) -> bool:
    return (
        predicate["operator"] == "eq"
        and not isinstance(predicate["value"], list)
        and _question_predicate_relation_occurrences(
            question, predicate, mentions
        )
        > 0
    )


def _question_predicate_is_bound(
    question: str,
    predicate: dict[str, Any],
    mentions: dict[str, list[dict[str, Any]]],
) -> tuple[bool, bool, bool]:
    field_bound = _question_value_is_bound(
        predicate["field"], "filter_field", mentions
    )
    value_bound = (
        predicate["value"] is None
        and predicate["operator"] in {"is_null", "is_not_null"}
    ) or _question_value_is_bound(predicate["value"], "filter_value", mentions)
    operator_bound = (
        _question_predicate_relation_occurrences(question, predicate, mentions) > 0
    )
    return field_bound, value_bound, operator_bound


def _question_predicate_relation_occurrences(
    question: str,
    predicate: dict[str, Any],
    mentions: dict[str, list[dict[str, Any]]],
) -> int:
    fields = [
        mention
        for mention in mentions.get("filter_field", [])
        if _normalized_question_surface(mention["surface"])
        == _normalized_question_surface(predicate["field"])
    ]
    scalar_values = (
        predicate["value"]
        if isinstance(predicate["value"], list)
        else [predicate["value"]]
    )
    values = [
        mention
        for mention in mentions.get("filter_value", [])
        if any(
            _normalized_question_surface(mention["surface"])
            == _normalized_question_surface(value)
            for value in scalar_values
        )
    ]
    operators = [
        mention
        for mention in mentions.get("operator", [])
        if EXPLICIT_OPERATOR_SURFACES.get(mention["surface"])
        == predicate["operator"]
    ]
    relations: set[tuple[int, int, int, int]] = set()

    def scalar_relation(
        field_mention: dict[str, Any], value_mention: dict[str, Any]
    ) -> bool:
        if field_mention["end"] > value_mention["start"]:
            return False
        if value_mention["end"] - field_mention["start"] > 96:
            return False
        between = question[field_mention["end"] : value_mention["start"]]
        after = question[value_mention["end"] : value_mention["end"] + 32]
        has_directional_particle = len(between) <= 24 and any(
            token in between
            for token in ("が", "は", "=", "==", "!=", ">", "<", "：", ":")
        )
        if not has_directional_particle:
            return False
        compound_match = _supported_compound_question_match(question)
        if predicate["operator"] == "eq" and (
            "であり" in after or "で、" in after or "だけ" in after
            or (
                after.startswith("かつ")
                and compound_match is not None
                and _normalized_question_surface(
                    compound_match.group("equality_field")
                )
                == _normalized_question_surface(predicate["field"])
                and _normalized_question_surface(
                    compound_match.group("equality_value")
                )
                == _normalized_question_surface(predicate["value"])
            )
        ):
            return True
        for operator_mention in operators:
            surface = operator_mention["surface"]
            is_symbol = surface in {"=", "==", "!=", ">", ">=", "<", "<="}
            if is_symbol and (
                field_mention["end"] <= operator_mention["start"]
                and operator_mention["end"] <= value_mention["start"]
            ):
                return True
            if (
                value_mention["end"] <= operator_mention["start"]
                and operator_mention["start"] - value_mention["end"] <= 16
            ):
                return True
        return False

    if predicate["value"] is None and predicate["operator"] in {
        "is_null",
        "is_not_null",
    }:
        for field_mention in fields:
            for operator_mention in operators:
                if (
                    field_mention["end"] <= operator_mention["start"]
                    and operator_mention["start"] - field_mention["end"] <= 32
                ):
                    relations.add(
                        (
                            field_mention["start"],
                            field_mention["end"],
                            operator_mention["start"],
                            operator_mention["end"],
                        )
                    )
        return len(relations)

    if isinstance(predicate["value"], list) and predicate["value"]:
        value_occurrences = {
            _normalized_question_surface(mention["surface"]): mention
            for mention in values
        }
        normalized_values = [
            _normalized_question_surface(value) for value in predicate["value"]
        ]
        if any(value not in value_occurrences for value in normalized_values):
            return 0
        ordered_values = sorted(
            (value_occurrences[value] for value in normalized_values),
            key=lambda item: (item["start"], item["end"]),
        )
        for field_mention in fields:
            if field_mention["end"] > ordered_values[0]["start"]:
                continue
            between = question[
                field_mention["end"] : ordered_values[0]["start"]
            ]
            alternatives = question[
                ordered_values[0]["end"] : ordered_values[-1]["start"]
            ]
            if (
                len(between) <= 24
                and any(
                    token in between for token in ("が", "は", "：", ":", "=")
                )
                and predicate["operator"] in {"in", "not_in"}
                and any(
                    connector in alternatives
                    for connector in _PREDICATE_VALUE_ALTERNATIVE_CONNECTORS
                )
            ):
                relations.add(
                    (
                        field_mention["start"],
                        field_mention["end"],
                        ordered_values[0]["start"],
                        ordered_values[-1]["end"],
                    )
                )
        return len(relations)

    if predicate["operator"] == "eq":
        # Supported lane v0.1 also recognizes the bounded Japanese suffix
        # grammar ``value<field-suffix>に一致``.  It is intentionally
        # separate from the general field -> value rule: only the finite field
        # ontology, exact adjacency, exact particle, and lexical EQ operator
        # can authorize the reversed surface order.
        for field_mention in fields:
            if field_mention["surface"] not in SUPPORTED_FILTER_SUFFIXES:
                continue
            for value_mention in values:
                if value_mention["end"] != field_mention["start"]:
                    continue
                for operator_mention in operators:
                    if (
                        operator_mention["surface"] == "一致"
                        and field_mention["end"] <= operator_mention["start"]
                        and question[
                            field_mention["end"] : operator_mention["start"]
                        ]
                        == "に"
                    ):
                        relations.add(
                            (
                                field_mention["start"],
                                field_mention["end"],
                                value_mention["start"],
                                value_mention["end"],
                            )
                        )

    for field_mention in fields:
        for value_mention in values:
            if scalar_relation(field_mention, value_mention):
                relations.add(
                    (
                        field_mention["start"],
                        field_mention["end"],
                        value_mention["start"],
                        value_mention["end"],
                    )
                )
    return len(relations)


def _question_operation_phrase_is_bound(
    operator: str,
    question: str,
    mentions: dict[str, list[dict[str, Any]]],
) -> bool:
    keywords = QUESTION_OPERATION_KEYWORDS.get(operator, ())
    explicit = [mention["surface"] for mention in mentions.get("operation", [])]
    return any(
        operator.casefold() == surface.casefold()
        or any(_raw_surface_occurs(surface, keyword) for keyword in keywords)
        for surface in explicit
    ) or any(_raw_surface_occurs(question, keyword) for keyword in keywords)


def _question_operation_phrase_count(
    operator: str,
    question: str,
    mentions: dict[str, list[dict[str, Any]]],
) -> int:
    keywords = QUESTION_OPERATION_KEYWORDS.get(operator, ())
    explicit_count = sum(
        1
        for mention in mentions.get("operation", [])
        if operator.casefold() == mention["surface"].casefold()
        or any(
            _raw_surface_occurs(mention["surface"], keyword)
            for keyword in keywords
        )
    )
    lexical_count = max(
        (_raw_surface_count(question, keyword) for keyword in keywords),
        default=0,
    )
    return max(explicit_count, lexical_count)


def _question_operation_options_are_bound(
    operation: dict[str, Any],
    question: str,
    mentions: dict[str, list[dict[str, Any]]],
) -> bool:
    operator = operation["operator"]
    present_options = OPERATION_OPTION_KEYS & set(operation)
    if not present_options <= ALLOWED_OPERATION_OPTIONS.get(operator, set()):
        return False
    if operator == "sort":
        sort_order = operation.get("sort_order")
        if sort_order not in SORT_ORDER_KEYWORDS or not any(
            _raw_surface_occurs(question, keyword)
            for keyword in SORT_ORDER_KEYWORDS.get(sort_order, ())
        ):
            return False
    calculation_precision = operation.get("calculation_precision")
    if calculation_precision not in {None, "unknown"}:
        if operator == "mean" and calculation_precision == "exact_unrounded":
            pass
        elif calculation_precision not in CALCULATION_PRECISION_KEYWORDS or not any(
            _raw_surface_occurs(question, keyword)
            for keyword in CALCULATION_PRECISION_KEYWORDS.get(
                calculation_precision, ()
            )
        ):
            return False
    if operator in {"argmin_all", "argmax_all"}:
        if operation.get("tie_policy") != "all":
            return False
        distance = operation.get("distance")
        if distance not in DISTANCE_KEYWORDS or not any(
            _raw_surface_occurs(question, keyword)
            for keyword in DISTANCE_KEYWORDS.get(distance, ())
        ):
            return False
        if not _question_field_is_bound(operation.get("field", ""), mentions):
            return False
    return True


def _question_precision_mode_is_bound(
    mode: str, mentions: dict[str, list[dict[str, Any]]]
) -> bool:
    if mode == "unspecified":
        return True
    keywords = (
        APPROXIMATE_PRECISION_KEYWORDS
        if mode == "approximate"
        else EXACT_PRECISION_KEYWORDS
    )
    return any(
        any(_raw_surface_occurs(mention["surface"], keyword) for keyword in keywords)
        for mention in mentions.get("precision", [])
    )


def _question_display_precision_is_bound(
    display_precision: dict[str, Any] | None,
    mentions: dict[str, list[dict[str, Any]]],
) -> bool:
    if display_precision is None:
        return True
    mode_keywords = (
        ("小数", "小数点", "decimal")
        if display_precision["mode"] == "decimal_places"
        else ("有効数字", "significant")
    )
    digits = display_precision["digits"]
    digit_tokens = (str(digits), *JAPANESE_DIGITS.get(digits, ()))
    return any(
        any(
            _raw_surface_occurs(mention["surface"], keyword)
            for keyword in mode_keywords
        )
        and any(
            _raw_surface_occurs(mention["surface"], token)
            for token in digit_tokens
        )
        for mention in mentions.get("precision", [])
    )


def _question_scope_match_mode_is_bound(
    question: str,
    scope: dict[str, Any],
    has_scope_binding: bool,
) -> bool:
    semantic_tokens = ("類似", "似た", "関連", "意味的", "semantic", "similar")
    exact_tokens = ("完全一致", "文字列一致", "exact")
    range_tokens = ("範囲内", "の間", "range", "between")
    tokens_by_mode = {
        "semantic_candidate": semantic_tokens,
        "exact": exact_tokens,
        "range": range_tokens,
    }
    raw_modes = {
        mode
        for mode, tokens in tokens_by_mode.items()
        if any(_raw_surface_occurs(question, token) for token in tokens)
    }
    if len(raw_modes) > 1:
        return False
    folded = question.casefold()
    for tokens in tokens_by_mode.values():
        for token in tokens:
            token_folded = token.casefold()
            start = 0
            while (index := folded.find(token_folded, start)) >= 0:
                before = folded[max(0, index - 8) : index]
                after = folded[
                    index + len(token_folded) : index + len(token_folded) + 16
                ]
                if (
                    any(
                        marker in after
                        for marker in ("ではなく", "でなく", "ではない", "じゃない")
                    )
                    or re.search(r"(?<![a-z0-9_])not\s*$", before) is not None
                    or re.match(r"\s+(?:is\s+)?not(?![a-z0-9_])", after)
                    is not None
                ):
                    return False
                start = index + max(1, len(token_folded))
    if "semantic_candidate" in raw_modes:
        expected = "semantic_candidate"
    elif "exact" in raw_modes:
        expected = "exact"
    elif "range" in raw_modes:
        expected = "range"
    elif has_scope_binding:
        expected = "exact_normalized"
    else:
        expected = "unknown"
    return scope["match_mode"] == expected


def _question_return_field_is_bound(
    return_field: str, mentions: dict[str, list[dict[str, Any]]]
) -> bool:
    surfaces = [mention["surface"] for mention in mentions.get("return_field", [])]
    if not surfaces:
        return False
    hints = {
        "count": ("件数", "数", "count"),
        "identifier": ("id", "identifier", "識別", "コード", "番号"),
        "name": ("名前", "名称", "name"),
        "status": ("状態", "status"),
        "description": ("説明", "概要", "description"),
        "reason": ("理由", "なぜ", "reason"),
        "procedure": ("手順", "方法", "procedure"),
        "comparison_result": ("比較", "差", "comparison"),
        "boolean": ("かどうか", "真偽", "boolean"),
    }
    if return_field in {"value", "unknown"}:
        return return_field == "unknown" or bool(surfaces)
    return any(
        any(hint.casefold() in surface.casefold() for hint in hints[return_field])
        for surface in surfaces
    )


def _question_cardinality_is_bound(
    mode: str,
    expected_count: int | None,
    source_operator: str | None,
    mentions: dict[str, list[dict[str, Any]]],
) -> bool:
    surfaces = {mention["surface"] for mention in mentions.get("cardinality", [])}
    if mode == "unknown":
        return True
    if mode == "all":
        bound = bool(surfaces & EXPLICIT_ALL_CARDINALITY_SURFACES)
    elif mode == "multiple":
        bound = bool(surfaces & EXPLICIT_MULTIPLE_CARDINALITY_SURFACES)
    else:
        bound = bool(surfaces & EXPLICIT_SINGLE_CARDINALITY_SURFACES) or (
            source_operator
            in {
                "count",
                "calculate",
                "sum",
                "mean",
                "min",
                "max",
                "absolute_distance",
                "verify",
                "boolean_test",
            }
        )
    if expected_count is None or (
        mode == "single" and expected_count == 1 and bound
    ):
        return bound
    return bound and _question_value_is_bound(
        expected_count, "cardinality", mentions
    )


def _requested_intent_audit(
    question: str,
    requested: dict[str, Any],
    explicit_mentions: list[dict[str, Any]],
) -> dict[str, bool]:
    mentions = _question_mentions_by_kind(explicit_mentions)
    target = requested["target"]
    scope = requested["scope"]
    hard_scope_pass = True
    for value, kind in (
        (target["surface"], "target_surface"),
        (target["instance"], "target_instance"),
        (scope["container"], "scope_container"),
        (scope["location"], "scope_location"),
        (scope["time_or_version"], "scope_time_or_version"),
    ):
        if value is not None and not _question_value_is_bound(value, kind, mentions):
            hard_scope_pass = False
    if target["canonical_type"] != _expected_canonical_target_type(
        target, mentions
    ):
        hard_scope_pass = False
    has_scope_binding = any(
        mentions.get(kind)
        for kind in {
            "scope_container",
            "scope_location",
            "scope_time_or_version",
            "filter_field",
            "filter_value",
            "operator",
        }
    )
    if scope["source"] == "explicit" and not has_scope_binding:
        hard_scope_pass = False
    if not _question_scope_match_mode_is_bound(
        question, scope, has_scope_binding
    ):
        hard_scope_pass = False

    operator_pass = True
    for predicate in scope["filters"]:
        field_bound, value_bound, operator_bound = _question_predicate_is_bound(
            question, predicate, mentions
        )
        hard_scope_pass = hard_scope_pass and field_bound and value_bound
        operator_pass = operator_pass and operator_bound

    predicate_counts = Counter(
        _canonical_identifier_key(predicate) for predicate in scope["filters"]
    )
    predicates_by_key = {
        _canonical_identifier_key(predicate): predicate
        for predicate in scope["filters"]
    }
    for predicate_key, count in predicate_counts.items():
        if count > _question_predicate_relation_occurrences(
            question, predicates_by_key[predicate_key], mentions
        ):
            operator_pass = False

    graph = requested["operation_graph"]
    graph_filter_predicates = [
        node["predicate"]
        for node in graph["nodes"]
        if node["operator"] == "filter" and "predicate" in node
    ]
    if Counter(
        _canonical_identifier_key(predicate) for predicate in scope["filters"]
    ) != Counter(
        _canonical_identifier_key(predicate)
        for predicate in graph_filter_predicates
    ):
        operator_pass = False
    actual_comparison_operators = {
        predicate["operator"] for predicate in graph_filter_predicates
    }
    expected_comparison_operators = _raw_question_operator_expectations(question)
    expected_comparison_operators.update(
        predicate["operator"]
        for predicate in scope["filters"]
        if predicate["operator"] in {"eq", "in", "not_in"}
        and _question_predicate_relation_occurrences(
            question, predicate, mentions
        )
        > 0
    )
    deterministic_set_predicate = any(
        predicate["operator"] in {"in", "not_in"}
        and _question_predicate_relation_occurrences(
            question, predicate, mentions
        )
        > 0
        for predicate in scope["filters"]
    )
    deterministic_equality_predicate = any(
        predicate["operator"] == "eq"
        and _question_predicate_relation_occurrences(
            question, predicate, mentions
        )
        > 0
        for predicate in scope["filters"]
    )
    if deterministic_set_predicate and not deterministic_equality_predicate:
        # ``FieldがAまたはBに一致`` is one deterministic IN relation;
        # the trailing lexical ``一致`` must not create a second EQ predicate.
        expected_comparison_operators.discard("eq")
    if expected_comparison_operators != actual_comparison_operators:
        operator_pass = False

    outputs_by_operation: dict[str, list[dict[str, Any]]] = {}
    for output in requested["requested_outputs"]:
        outputs_by_operation.setdefault(output["source_operation_ref"], []).append(
            output
        )

    operation_ids = {
        operation["operation_id"] for operation in graph["nodes"]
    }
    reverse_dependencies: dict[str, set[str]] = {
        operation_id: set() for operation_id in operation_ids
    }
    for edge in graph["edges"]:
        reverse_dependencies.setdefault(edge["to"], set()).add(edge["from"])
    relevant_operation_ids = set(outputs_by_operation)
    relevance_stack = list(relevant_operation_ids)
    while relevance_stack:
        operation_id = relevance_stack.pop()
        for parent in reverse_dependencies.get(operation_id, set()):
            if parent not in relevant_operation_ids:
                relevant_operation_ids.add(parent)
                relevance_stack.append(parent)
    if relevant_operation_ids != operation_ids:
        operator_pass = False

    project_field_counts: Counter[str] = Counter()
    semantic_operator_counts: Counter[str] = Counter()
    for operation in graph["nodes"]:
        operator = operation["operator"]
        if operator in QUESTION_OPERATION_KEYWORDS:
            semantic_operator_counts[operator] += 1
        bound = operator == "unknown"
        if operator == "filter" and "predicate" in operation:
            bound = all(
                _question_predicate_is_bound(question, operation["predicate"], mentions)
            )
        elif operator == "project":
            project_field_counts.update(
                _normalized_question_surface(field) or ""
                for field in operation.get("fields", [])
            )
            bound = bool(operation.get("fields")) and all(
                _question_field_is_bound(field, mentions)
                for field in operation.get("fields", [])
            )
        elif operator == "list":
            bound = bool(
                {item["surface"] for item in mentions.get("cardinality", [])}
                & (
                    EXPLICIT_ALL_CARDINALITY_SURFACES
                    | EXPLICIT_MULTIPLE_CARDINALITY_SURFACES
                )
            ) and bool(mentions.get("return_field") or mentions.get("target_surface"))
        elif operator == "retrieve":
            bound = bool(mentions.get("return_field") or mentions.get("target_surface"))
        elif operator in {"argmin_all", "argmax_all"}:
            bound = _question_operation_phrase_is_bound(
                operator, question, mentions
            ) and _question_field_is_bound(operation.get("field", ""), mentions)
        elif operator in QUESTION_OPERATION_KEYWORDS:
            bound = _question_operation_phrase_is_bound(
                operator, question, mentions
            ) and bool(
                mentions.get("return_field")
                or mentions.get("target_surface")
                or outputs_by_operation.get(operation["operation_id"])
            )
        bound = bound and _question_operation_options_are_bound(
            operation, question, mentions
        )
        operator_pass = operator_pass and bound

    bound_field_spans: dict[str, set[tuple[int, int]]] = {}
    for kind in (
        "target_surface",
        "target_instance",
        "filter_field",
        "return_field",
    ):
        for mention in mentions.get(kind, []):
            surface = _normalized_question_surface(mention["surface"]) or ""
            bound_field_spans.setdefault(surface, set()).add(
                (mention["start"], mention["end"])
            )
    bound_field_counts = Counter(
        {surface: len(spans) for surface, spans in bound_field_spans.items()}
    )
    if any(
        count > bound_field_counts.get(field, 0)
        for field, count in project_field_counts.items()
    ):
        operator_pass = False
    for operator, count in semantic_operator_counts.items():
        if count > _question_operation_phrase_count(operator, question, mentions):
            operator_pass = False

    outputs = requested["requested_outputs"]
    output_pass = True
    all_is_explicit = any(
        _raw_surface_occurs(question, surface)
        for surface in EXPLICIT_ALL_CARDINALITY_SURFACES
    )
    has_all_output = any(
        output["cardinality"]["mode"] == "all" for output in outputs
    )
    if all_is_explicit != has_all_output:
        output_pass = False
    operations_by_id = {
        operation["operation_id"]: operation for operation in graph["nodes"]
    }
    known_output_count = sum(
        output["return_field"] != "unknown" for output in outputs
    )
    if known_output_count > len(mentions.get("return_field", [])):
        output_pass = False
    output_semantic_counts: Counter[str] = Counter()
    for output in outputs:
        source_operation = operations_by_id.get(output["source_operation_ref"])
        source_operator = source_operation["operator"] if source_operation else None
        output_semantic_counts[
            _canonical_identifier_key(
                {
                    "source_operation_ref": output["source_operation_ref"],
                    "return_field": output["return_field"],
                    "cardinality": output["cardinality"],
                    "answer_shape": output["answer_shape"],
                    "display_precision": output["display_precision"],
                }
            )
        ] += 1
        if output["return_field"] != "unknown" and not _question_return_field_is_bound(
            output["return_field"], mentions
        ):
            output_pass = False
        cardinality = output["cardinality"]
        if not _question_cardinality_is_bound(
            cardinality["mode"],
            cardinality["expected_count"],
            source_operator,
            mentions,
        ):
            output_pass = False
        shape = output["answer_shape"]
        container_bound = (
            shape["container"] == "unknown"
            or shape["container"] == "list"
            and cardinality["mode"] in {"all", "multiple"}
            or shape["container"] == "scalar"
            and cardinality["mode"] == "single"
            or shape["container"] == "yes_no"
            and output["return_field"] == "boolean"
            or shape["container"] == "prose"
            and output["return_field"] in {"description", "reason", "procedure"}
            or _question_value_is_bound(shape["container"], "answer_shape", mentions)
        )
        if not container_bound:
            output_pass = False
        compatible_value_types = {
            "count": {"integer", "number"},
            "identifier": {"identifier", "string"},
            "name": {"string"},
            "status": {"string"},
            "description": {"string"},
            "reason": {"string"},
            "procedure": {"string"},
            "boolean": {"boolean"},
        }
        value_type_bound = (
            shape["value_type"] == "unknown"
            or output["return_field"] == "value"
            and shape["value_type"] in {"integer", "number", "string"}
            or output["return_field"] == "comparison_result"
            and shape["value_type"] in {"number", "string", "boolean"}
            or shape["value_type"]
            in compatible_value_types.get(output["return_field"], set())
        )
        if not value_type_bound:
            output_pass = False
        if shape["unit"] is not None and not _question_value_is_bound(
            shape["unit"], "unit", mentions
        ):
            output_pass = False
        precision_bound = (
            shape["precision"] == "unspecified"
            or shape["precision"] == "exact"
            and (
                output["return_field"] in {"count", "identifier", "boolean"}
                or source_operation is not None
                and source_operation.get("calculation_precision")
                in {"exact", "exact_unrounded"}
                or output["display_precision"] is not None
                or _question_precision_mode_is_bound("exact", mentions)
            )
            or shape["precision"] == "approximate"
            and _question_precision_mode_is_bound("approximate", mentions)
        )
        if not precision_bound:
            output_pass = False
        if not _question_display_precision_is_bound(
            output["display_precision"], mentions
        ):
            output_pass = False

    if any(count > 1 for count in output_semantic_counts.values()):
        output_pass = False

    return {
        "operator_preserved": operator_pass,
        "hard_scope_not_expanded": hard_scope_pass,
        "output_contract_match": output_pass,
    }


def _filter_alternative_is_deterministic(
    question: str,
    connector_start: int,
    connector_end: int,
    requested_intents: list[dict[str, Any]],
    mentions: dict[str, list[dict[str, Any]]],
) -> bool:
    for requested in requested_intents:
        for predicate in requested["scope"]["filters"]:
            if predicate["operator"] not in {"in", "not_in"} or not isinstance(
                predicate["value"], list
            ):
                continue
            value_mentions = [
                mention
                for mention in mentions.get("filter_value", [])
                if any(
                    _normalized_question_surface(mention["surface"])
                    == _normalized_question_surface(value)
                    for value in predicate["value"]
                )
            ]
            if not value_mentions:
                continue
            if (
                min(item["start"] for item in value_mentions) < connector_start
                and max(item["end"] for item in value_mentions) > connector_end
                and _question_predicate_relation_occurrences(
                    question, predicate, mentions
                )
                > 0
            ):
                return True
    return False


def _alternative_contract_errors(
    question: str,
    explicit_mentions: list[dict[str, Any]],
    qic: dict[str, Any],
    requested_intents: list[dict[str, Any]],
    branches_complete: bool,
) -> tuple[list[str], set[str]]:
    errors: list[str] = []
    alternative_operations: set[str] = set()
    mentions = _question_mentions_by_kind(explicit_mentions)
    operation_occurrences = _raw_operation_occurrences(question)
    field_by_kind = {
        "target_surface": "target",
        "target_instance": "target",
        "scope_container": "scope",
        "scope_location": "scope",
        "scope_time_or_version": "scope",
        "operation": "operation",
        "return_field": "return_field",
        "answer_shape": "answer_shape",
    }
    for connector in ALTERNATIVE_CONNECTORS:
        for connector_match in re.finditer(re.escape(connector), question):
            start, end = connector_match.span()
            if _filter_alternative_is_deterministic(
                question, start, end, requested_intents, mentions
            ):
                continue

            left_operations = [
                (start - occurrence_end, operator, occurrence_start, occurrence_end)
                for operator, occurrences in operation_occurrences.items()
                for occurrence_start, occurrence_end in occurrences
                if occurrence_end <= start and start - occurrence_end <= 12
            ]
            right_operations = [
                (occurrence_start - end, operator, occurrence_start, occurrence_end)
                for operator, occurrences in operation_occurrences.items()
                for occurrence_start, occurrence_end in occurrences
                if occurrence_start >= end and occurrence_start - end <= 12
            ]
            if left_operations and right_operations:
                left = min(left_operations)
                right = min(right_operations)
                required_operators = {left[1], right[1]}
                alternative_operations.update(required_operators)
                operation_mentions = mentions.get("operation", [])
                spans_present = all(
                    any(
                        mention["start"] == occurrence_start
                        and mention["end"] == occurrence_end
                        for mention in operation_mentions
                    )
                    for occurrence_start, occurrence_end in (
                        (left[2], left[3]),
                        (right[2], right[3]),
                    )
                )
                operation_ambiguities = [
                    ambiguity
                    for ambiguity in qic["ambiguity"]
                    if ambiguity["field"] == "operation"
                ]
                candidate_operator_sets = [
                    {
                        node["operator"]
                        for node in candidate["value"]["nodes"]
                    }
                    for ambiguity in operation_ambiguities
                    for candidate in ambiguity["candidates"]
                    if isinstance(candidate["value"], dict)
                    and isinstance(candidate["value"].get("nodes"), list)
                ]
                covered = all(
                    any(operator in values for values in candidate_operator_sets)
                    for operator in required_operators
                )
                if not (
                    spans_present
                    and len(operation_ambiguities) == 1
                    and covered
                    and branches_complete
                ):
                    errors.append("/requested/operation_graph")
                continue

            left_mentions = [
                mention
                for mention in explicit_mentions
                if mention["end"] <= start and start - mention["end"] <= 48
            ]
            right_mentions = [
                mention
                for mention in explicit_mentions
                if mention["start"] >= end and mention["start"] - end <= 48
            ]
            left_mention = (
                min(left_mentions, key=lambda item: start - item["end"])
                if left_mentions
                else None
            )
            right_mention = (
                min(right_mentions, key=lambda item: item["start"] - end)
                if right_mentions
                else None
            )
            if (
                left_mention is None
                or right_mention is None
                or left_mention["kind"] != right_mention["kind"]
                or left_mention["kind"] not in field_by_kind
            ):
                errors.append("/requested/scope")
                continue
            field = field_by_kind[left_mention["kind"]]
            values = {
                _normalized_question_surface(left_mention["surface"]),
                _normalized_question_surface(right_mention["surface"]),
            }
            matching_ambiguities = [
                ambiguity
                for ambiguity in qic["ambiguity"]
                if ambiguity["field"] == field
            ]
            candidate_values = {
                scalar
                for ambiguity in matching_ambiguities
                for candidate in ambiguity["candidates"]
                for scalar in _recursive_scalar_values(candidate["value"])
            }
            if not (
                len(matching_ambiguities) == 1
                and values <= candidate_values
                and branches_complete
            ):
                errors.append(FIELD_PRIMARY_PATH[field])
    return sorted(set(errors)), alternative_operations


def _raw_question_contract_errors(
    question: str,
    explicit_mentions: list[dict[str, Any]],
    qic: dict[str, Any],
    candidate_paths: list[dict[str, Any]],
) -> dict[str, list[str]]:
    requested_intents = [
        path["candidate_intent"] for path in candidate_paths
    ] or [qic["requested"]]
    mentions = _question_mentions_by_kind(explicit_mentions)
    hard_scope_errors: list[str] = []
    operator_errors: list[str] = []
    output_errors: list[str] = []

    scope_pairs = _raw_scope_pairs(question)
    file_tokens = {match.group(0) for match in RAW_FILE_PATTERN.finditer(question)}
    expected_containers = file_tokens | {
        container for _locations, container in scope_pairs
    }
    expected_locations = {
        location for locations, _container in scope_pairs for location in locations
    }
    container_mentions = {
        mention["surface"] for mention in mentions.get("scope_container", [])
    }
    location_mentions = {
        mention["surface"] for mention in mentions.get("scope_location", [])
    }
    if not expected_containers <= container_mentions:
        hard_scope_errors.append("/requested/scope/container")
    if not expected_locations <= location_mentions:
        hard_scope_errors.append("/requested/scope/location")
    for locations, container in scope_pairs:
        if any(
            intent["scope"]["container"] != container
            or intent["scope"]["location"] not in locations
            for intent in requested_intents
        ):
            hard_scope_errors.append("/requested/scope")
        if not set(locations) <= {
            intent["scope"]["location"] for intent in requested_intents
        }:
            hard_scope_errors.append("/requested/scope/location")
    for token in file_tokens:
        if any(
            intent["scope"]["container"] != token
            for intent in requested_intents
        ):
            hard_scope_errors.append("/requested/scope/container")

    raw_relation_spans = {
        match.span() for match in RAW_RELATION_PATTERN.finditer(question)
    }
    raw_relation_spans.update(
        match.span()
        for match in _SUPPORTED_SUFFIX_RELATION_PATTERN.finditer(question)
    )
    raw_relation_spans.update(
        match.span()
        for match in re.finditer(
            r"(?:が|は)[^、。\n]{1,48}?(?:"
            + "|".join(re.escape(value) for value in ALTERNATIVE_CONNECTORS)
            + r")[^、。\n]{1,48}",
            question,
        )
    )
    merged_relation_spans: list[list[int]] = []
    for start, end in sorted(raw_relation_spans):
        if merged_relation_spans and start < merged_relation_spans[-1][1]:
            merged_relation_spans[-1][1] = max(
                merged_relation_spans[-1][1], end
            )
        else:
            merged_relation_spans.append([start, end])
    raw_relation_count = len(merged_relation_spans)
    if _supported_compound_question_match(question) is not None:
        # The punctuation-free ``FがVかつGがNより大きい`` form is one
        # overlapping generic-regex match.  The dedicated full grammar proves
        # two ordered predicates without treating OR or malformed conjunctions
        # as equivalent.
        raw_relation_count = max(raw_relation_count, 2)
    compound_match = _SUPPORTED_COMPOUND_PATTERN.fullmatch(question)
    if compound_match is not None and not _metric_descriptor_is_supported(
        compound_match.group("metric"),
        compound_match.group("nearest_descriptor"),
    ):
        operator_errors.append("/requested/operation_graph")
    if raw_relation_count and any(
        len(intent["scope"]["filters"]) < raw_relation_count
        for intent in requested_intents
    ):
        operator_errors.append("/requested/operation_graph")

    identifier_tokens = {
        match.group("identifier")
        for match in RAW_IDENTIFIER_OUTPUT_PATTERN.finditer(question)
    }
    return_mentions = {
        mention["surface"] for mention in mentions.get("return_field", [])
    }
    if not identifier_tokens <= return_mentions:
        output_errors.append("/requested/requested_outputs")
    for token in identifier_tokens:
        if any(
            token not in _intent_project_fields(intent)
            and intent["target"]["surface"] != token
            for intent in requested_intents
        ):
            output_errors.append("/requested/requested_outputs")

    if _raw_cardinality_conflicts(question):
        output_errors.append("/requested/requested_outputs")

    raw_exclusions = _raw_exclusion_items(question)
    compiled_exclusions = {item["item"] for item in qic["not_requested"]}
    if not raw_exclusions <= compiled_exclusions:
        output_errors.append("/not_requested")

    alternative_errors, alternative_operations = _alternative_contract_errors(
        question,
        explicit_mentions,
        qic,
        requested_intents,
        bool(candidate_paths),
    )
    operator_occurrences = _raw_operation_occurrences(question)
    for operator in set(operator_occurrences) - alternative_operations:
        if any(
            operator not in _intent_operators(intent)
            for intent in requested_intents
        ):
            operator_errors.append("/requested/operation_graph")
    for path in alternative_errors:
        if path == "/requested/operation_graph":
            operator_errors.append(path)
        elif path == "/requested/requested_outputs":
            output_errors.append(path)
        else:
            hard_scope_errors.append(path)
    return {
        "operator_preserved": sorted(set(operator_errors)),
        "hard_scope_not_expanded": sorted(set(hard_scope_errors)),
        "output_contract_match": sorted(set(output_errors)),
    }


def _deterministic_intent_audit(
    record: dict[str, Any], nodes_by_id: dict[str, dict[str, Any]]
) -> dict[str, bool]:
    mentions = _question_explicit_mentions(record, nodes_by_id)
    requested_intents = [record["question_intent_contract"]["requested"]]
    requested_intents.extend(
        path["candidate_intent"] for path in record["candidate_query_paths"]
    )
    audits = [
        _requested_intent_audit(record["original_question"], requested, mentions)
        for requested in requested_intents
    ]
    combined = {
        validator_id: all(audit[validator_id] for audit in audits)
        for validator_id in (
            "operator_preserved",
            "hard_scope_not_expanded",
            "output_contract_match",
        )
    }
    raw_errors = (
        _raw_question_contract_errors(
            record["original_question"],
            mentions,
            record["question_intent_contract"],
            record["candidate_query_paths"],
        )
        if record["final_status"] != "failed"
        else {
            validator_id: []
            for validator_id in (
                "operator_preserved",
                "hard_scope_not_expanded",
                "output_contract_match",
            )
        }
    )
    for validator_id, paths in raw_errors.items():
        if paths:
            combined[validator_id] = False
    if _has_unbranched_singleton_context(record, nodes_by_id):
        combined["hard_scope_not_expanded"] = False
    return combined


def _pre_retrieval_type_status(requested: dict[str, Any]) -> str:
    """Mirror the compiler's coarse, retrieval-free operation type inference."""

    graph = requested["operation_graph"]
    external_inputs = graph["external_inputs"]
    if any(
        item["input_type"] == "unknown" or item["source"] == "unknown"
        for item in external_inputs
    ) or any(node["operator"] == "unknown" for node in graph["nodes"]):
        return "indeterminate"
    if any(item["source"] == "constant" for item in external_inputs):
        return "fail"
    if any(
        item["source"] == "scope"
        and (
            requested["scope"]["source"] == "unknown"
            or requested["scope"]["match_mode"] == "unknown"
        )
        for item in external_inputs
    ):
        return "fail"

    value_types = {
        item["input_ref"]: item["input_type"] for item in external_inputs
    }
    used_external_refs: set[str] = set()
    set_types = {"record_set", "value_set"}

    for operation in graph["nodes"]:
        input_refs = operation["input_refs"]
        if any(reference not in value_types for reference in input_refs):
            return "fail"
        input_types = [value_types[reference] for reference in input_refs]
        used_external_refs.update(
            reference for reference in input_refs if reference.startswith("input_")
        )
        operator = operation["operator"]
        output_type: str | None = None
        if operator == "filter":
            if len(input_types) == 1 and input_types[0] in set_types:
                output_type = input_types[0]
        elif operator == "project":
            if len(input_types) == 1 and input_types[0] == "record_set":
                output_type = "value_set"
        elif operator == "count":
            if len(input_types) == 1 and input_types[0] in {
                "record_set",
                "value_set",
                "document",
                "claim",
            }:
                output_type = "scalar"
        elif operator == "list":
            if len(input_types) == 1 and input_types[0] in set_types:
                output_type = input_types[0]
        elif operator == "retrieve":
            if len(input_types) == 1:
                output_type = input_types[0]
        elif operator == "compare":
            if len(input_types) == 2 and all(
                value_type in {"scalar", "value_set"}
                for value_type in input_types
            ):
                output_type = "scalar"
        elif operator == "calculate":
            if input_types and all(
                value_type in {"scalar", "value_set"}
                for value_type in input_types
            ):
                output_type = "scalar"
        elif operator in {"sum", "mean", "min", "max"}:
            if len(input_types) == 1 and input_types[0] == "value_set":
                output_type = "scalar"
        elif operator == "absolute_distance":
            if len(input_types) == 2 and all(
                value_type in {"scalar", "value_set"}
                for value_type in input_types
            ):
                output_type = "scalar"
        elif operator in {"argmin_all", "argmax_all"}:
            candidate_ref = operation.get("candidate_set_ref")
            if (
                len(input_types) >= 2
                and candidate_ref in input_refs
                and value_types.get(candidate_ref) == "record_set"
                and all(
                    value_type in {"record_set", "scalar", "value_set"}
                    for value_type in input_types
                )
            ):
                output_type = "record_set"
        elif operator in {"sort", "deduplicate", "group"}:
            if len(input_types) == 1 and input_types[0] in set_types:
                output_type = input_types[0]
        elif operator in {"explain", "procedure", "verify", "boolean_test"}:
            if len(input_types) == 1:
                output_type = "scalar"
        if output_type is None:
            return "fail"
        value_types[operation["output_ref"]] = output_type

    external_refs = {item["input_ref"] for item in external_inputs}
    if used_external_refs != set(value_types) & external_refs:
        return "fail"
    return "pass"


def _has_unbranched_singleton_context(
    record: dict[str, Any], nodes_by_id: dict[str, dict[str, Any]]
) -> bool:
    mentions = _question_explicit_mentions(record, nodes_by_id)
    by_kind = _question_mentions_by_kind(mentions)
    bindings = {
        "target_surface": ("target", "surface"),
        "target_instance": ("target", "instance"),
        "scope_container": ("scope", "container"),
        "scope_location": ("scope", "location"),
        "scope_time_or_version": ("scope", "time_or_version"),
    }
    ambiguities = record["question_intent_contract"]["ambiguity"]
    for kind, (field, component) in bindings.items():
        explicit_values = {
            _normalized_question_surface(mention["surface"])
            for mention in by_kind.get(kind, [])
        }
        explicit_values.discard(None)
        if len(explicit_values) <= 1:
            continue
        matching = [
            ambiguity for ambiguity in ambiguities if ambiguity["field"] == field
        ]
        candidate_values = {
            _normalized_question_surface(candidate["value"].get(component))
            for ambiguity in matching
            for candidate in ambiguity["candidates"]
            if isinstance(candidate["value"], dict)
        }
        candidate_values.discard(None)
        if len(matching) != 1 or not explicit_values <= candidate_values:
            return True
    return False


def _expected_candidate_basis_refs(
    field: str,
    compiled_requested: dict[str, Any],
    mentions: list[dict[str, Any]],
    fallback_ref: str,
) -> list[str]:
    primary_component = {
        "target": "target",
        "scope": "scope",
        "operation": "operation_graph",
        "return_field": "requested_outputs",
        "answer_shape": "requested_outputs",
    }[field]
    scalar_values: set[str] = set()
    stack: list[Any] = [compiled_requested[primary_component]]
    while stack:
        value = stack.pop()
        if isinstance(value, dict):
            stack.extend(value.values())
        elif isinstance(value, list):
            stack.extend(value)
        else:
            normalized = _normalized_question_surface(value)
            if normalized is not None:
                scalar_values.add(normalized)

    relevant = [
        mention
        for mention in mentions
        if mention["kind"] in QUESTION_MENTION_KINDS_BY_AMBIGUITY_FIELD[field]
    ]
    exact_refs = {
        mention["source_ref"]
        for mention in relevant
        if _normalized_question_surface(mention["surface"]) in scalar_values
    }
    semantic_refs: set[str] = set()
    if field == "target":
        semantic_refs.update(
            mention["source_ref"]
            for mention in relevant
            if mention["kind"] in {"target_surface", "target_instance"}
        )
    elif field in {"return_field", "answer_shape"}:
        semantic_refs.update(mention["source_ref"] for mention in relevant)
    elif field in {"scope", "operation"}:
        comparison_operators = {
            predicate["operator"]
            for predicate in compiled_requested["scope"]["filters"]
        }
        comparison_operators.update(
            operation["predicate"]["operator"]
            for operation in compiled_requested["operation_graph"]["nodes"]
            if operation["operator"] == "filter" and "predicate" in operation
        )
        operation_names = {
            operation["operator"]
            for operation in compiled_requested["operation_graph"]["nodes"]
        }
        for mention in relevant:
            if (
                mention["kind"] == "operator"
                and EXPLICIT_OPERATOR_SURFACES.get(mention["surface"])
                in comparison_operators
            ):
                semantic_refs.add(mention["source_ref"])
            elif mention["kind"] == "operation" and any(
                operator.casefold() == mention["surface"].casefold()
                or any(
                    _raw_surface_occurs(mention["surface"], keyword)
                    for keyword in QUESTION_OPERATION_KEYWORDS.get(operator, ())
                )
                for operator in operation_names
            ):
                semantic_refs.add(mention["source_ref"])
    return sorted(exact_refs | semantic_refs) or [fallback_ref]


def _validate_compiler_controlled_text(
    record: dict[str, Any], nodes_by_id: dict[str, dict[str, Any]]
) -> list[str]:
    """Close free-text channels when the record claims compiler provenance."""

    contract = record["question_intent_contract"]
    if (
        record["provenance"]["runner"] != "question-understanding-compiler"
        or contract["provenance"]["analyzer"]
        != "question-understanding-compiler"
    ):
        return []

    errors: list[str] = []
    mentions = _question_explicit_mentions(record, nodes_by_id)
    explicit_not_requested = {
        mention["surface"]
        for mention in mentions
        if mention["kind"] == "not_requested"
    }
    raw_exclusions = _raw_exclusion_items(record["original_question"])
    for index, item in enumerate(contract["not_requested"]):
        if item["item"] not in explicit_not_requested:
            errors.append(
                f"question_intent_contract.not_requested[{index}].item must be "
                "bound to an exact not_requested question span"
            )
        if item["item"] not in raw_exclusions:
            errors.append(
                f"question_intent_contract.not_requested[{index}].item is not "
                "a positive raw exclusion request"
            )
        if item["reason"] != "Explicitly excluded by an exact question span.":
            errors.append(
                f"question_intent_contract.not_requested[{index}].reason must be "
                "compiler-controlled"
            )

    for ambiguity_index, ambiguity in enumerate(contract["ambiguity"]):
        expected_issue = f"Unresolved {ambiguity['field']} interpretation."
        if ambiguity["issue"] != expected_issue:
            errors.append(
                f"question_intent_contract.ambiguity[{ambiguity_index}].issue "
                "must be compiler-controlled"
            )
        expected_basis = (
            f"Candidate {ambiguity['field']} interpretation grounded in exact "
            "question context."
        )
        for candidate_index, candidate in enumerate(ambiguity["candidates"]):
            if candidate["basis"] != expected_basis:
                errors.append(
                    "question_intent_contract.ambiguity"
                    f"[{ambiguity_index}].candidates[{candidate_index}].basis "
                    "must be compiler-controlled"
                )

    for category, rules in contract["forbidden"].items():
        for rule_index, rule in enumerate(rules):
            validator_id = rule["check"]["validator_id"]
            if (
                rule["rule_id"] != f"rule_{validator_id}"
                or rule["category"] != category
                or rule["prohibition"] != f"Enforce {validator_id}"
                or rule["basis"] != "IG-GE v0.2 invariant"
                or rule["basis_ref"] is not None
                or rule["on_violation"] != "abstain"
            ):
                errors.append(
                    f"question_intent_contract.forbidden.{category}[{rule_index}] "
                    "must use compiler-controlled rule text and action"
                )

    requested_intents = [contract["requested"]]
    requested_intents.extend(
        path["candidate_intent"] for path in record["candidate_query_paths"]
    )
    expected_question_ref = "question:" + _safe_question_token(
        record["question_id"], record["original_question"]
    )
    for intent_index, requested in enumerate(requested_intents):
        if requested["target"]["canonical_type"] not in COMPILER_TARGET_TYPES:
            errors.append(
                f"requested_intents[{intent_index}].target.canonical_type is not "
                "a compiler-supported type"
            )
        for input_index, external_input in enumerate(
            requested["operation_graph"]["external_inputs"]
        ):
            if external_input["source_ref"] != expected_question_ref:
                errors.append(
                    f"requested_intents[{intent_index}].operation_graph.external_inputs"
                    f"[{input_index}].source_ref must identify only the raw question"
                )
            expected_description = (
                f"Compiler-declared {external_input['source']} "
                f"{external_input['input_type']} input."
            )
            if external_input["description"] == expected_description:
                continue
            if (
                record["final_status"] == "failed"
                and external_input["description"] == "Uncompiled question input"
            ):
                continue
            errors.append(
                f"requested_intents[{intent_index}].operation_graph.external_inputs"
                f"[{input_index}].description must be compiler-controlled"
            )
    for error_index, error in enumerate(record["errors"]):
        if error["code"] not in COMPILER_FAILURE_CODES:
            errors.append(
                f"errors[{error_index}].code is not a registered compiler reason code"
            )
        expected_message = (
            f"Question understanding terminated safely at {error['stage']}; "
            f"reason_code={error['code']}."
        )
        if error["message"] != expected_message:
            errors.append(
                f"errors[{error_index}].message must be compiler-controlled"
            )
    return errors


def _validate_compiler_identifiers(record: dict[str, Any]) -> list[str]:
    """Recompute every compiler-owned content identifier from the final record."""

    contract = record["question_intent_contract"]
    if (
        record["provenance"]["runner"] != "question-understanding-compiler"
        or contract["provenance"]["analyzer"]
        != "question-understanding-compiler"
    ):
        return []

    errors: list[str] = []

    requested_intents = [contract["requested"]]
    requested_intents.extend(
        path["candidate_intent"] for path in record["candidate_query_paths"]
    )
    for intent_index, requested in enumerate(requested_intents):
        graph = requested["operation_graph"]
        graph_core = {
            key: copy.deepcopy(value)
            for key, value in graph.items()
            if key != "operation_graph_id"
        }
        expected_graph_id = _deterministic_identifier("graph", graph_core)
        if graph["operation_graph_id"] != expected_graph_id:
            errors.append(
                f"requested_intents[{intent_index}].operation_graph_id is not "
                "the deterministic content ID"
            )
        is_failure_fallback = record["final_status"] == "failed" and all(
            node["operation_id"] == "op_unknown" for node in graph["nodes"]
        )
        if not is_failure_fallback:
            for input_index, external_input in enumerate(graph["external_inputs"]):
                if external_input["input_ref"] != f"input_{input_index:03d}":
                    errors.append(
                        f"requested_intents[{intent_index}].external_inputs"
                        f"[{input_index}].input_ref is not positional"
                    )
            for operation_index, operation in enumerate(graph["nodes"]):
                if operation["operation_id"] != (
                    f"op_{operation_index:03d}_{operation['operator']}"
                ):
                    errors.append(
                        f"requested_intents[{intent_index}].operation_graph.nodes"
                        f"[{operation_index}].operation_id is not positional"
                    )
                if operation["output_ref"] != f"value_{operation_index:03d}":
                    errors.append(
                        f"requested_intents[{intent_index}].operation_graph.nodes"
                        f"[{operation_index}].output_ref is not positional"
                    )
            for output_index, output in enumerate(requested["requested_outputs"]):
                if output["output_id"] != (
                    f"output_{output_index:03d}_{output['return_field']}"
                ):
                    errors.append(
                        f"requested_intents[{intent_index}].requested_outputs"
                        f"[{output_index}].output_id is not positional"
                    )

    graph = record["query_context_graph"]
    if graph is not None:
        all_graph_edges = [
            *graph["edges"],
            *(item["edge"] for item in graph["rejected_context"]),
        ]
        source_uses: dict[str, list[dict[str, Any]]] = {}
        from_node_uses: Counter[str] = Counter()
        to_node_uses: Counter[str] = Counter()
        for edge in all_graph_edges:
            source_uses.setdefault(edge["source_ref"], []).append(edge)
            from_node_uses[edge["from_ref"]] += 1
            to_node_uses[edge["to_ref"]] += 1
        question_token = _safe_question_token(
            record["question_id"], record["original_question"]
        )
        full_question_sources = []
        for source_index, source in enumerate(graph["sources"]):
            if source["source_type"] != "question_explicit":
                errors.append(
                    f"query_context_graph.sources[{source_index}] compiler QCG "
                    "cannot contain a non-question source"
                )
                continue
            source_edges = source_uses.get(source["source_id"], [])
            span = source["span"]
            if span is None:
                continue
            if (
                span["start"] == 0
                and span["end"] == len(record["original_question"])
                and span["text"] == record["original_question"]
                and not source_edges
            ):
                full_question_sources.append(source)
                expected_ref = f"question:{question_token}"
            elif len(source_edges) == 1:
                to_node = next(
                    (
                        node
                        for node in graph["nodes"]
                        if node["node_id"] == source_edges[0]["to_ref"]
                    ),
                    None,
                )
                slot_kind = (
                    to_node.get("canonical_value")
                    if isinstance(to_node, dict)
                    else None
                )
                expected_ref = (
                    f"question:{question_token}:span:{span['start']}-{span['end']}:"
                    f"kind:{slot_kind}"
                )
            else:
                errors.append(
                    f"query_context_graph.sources[{source_index}] must be either the "
                    "single whole-question source or support exactly one mention edge"
                )
                continue
            if source["source_ref"] != expected_ref:
                errors.append(
                    f"query_context_graph.sources[{source_index}].source_ref is not "
                    "compiler-derived from its question span and role"
                )
        if len(full_question_sources) != 1:
            errors.append(
                "compiler QCG must contain exactly one unreferenced whole-question source"
            )

        question_nodes = [
            node for node in graph["nodes"] if node["node_type"] == "question"
        ]
        if len(question_nodes) != 1 or any(
            node["surface"] != record["original_question"]
            or node["canonical_value"] != record["original_question"]
            or from_node_uses[node["node_id"]]
            or to_node_uses[node["node_id"]]
            for node in question_nodes
        ):
            errors.append(
                "compiler QCG must contain exactly one unreferenced exact question node"
            )
        slot_kind_counts: Counter[str] = Counter()
        for node_index, node in enumerate(graph["nodes"]):
            if node["node_type"] == "question":
                continue
            is_mention = from_node_uses[node["node_id"]] > 0
            is_slot = to_node_uses[node["node_id"]] > 0
            if is_mention == is_slot:
                errors.append(
                    f"query_context_graph.nodes[{node_index}] must be used exclusively "
                    "as a mention or a slot"
                )
            if is_slot and isinstance(node["canonical_value"], str):
                slot_kind_counts[node["canonical_value"]] += 1
        duplicate_slot_kinds = sorted(
            kind for kind, count in slot_kind_counts.items() if count > 1
        )
        if duplicate_slot_kinds:
            errors.append(
                "compiler QCG must contain at most one slot node per role: "
                f"{duplicate_slot_kinds}"
            )

        sorted_sources = sorted(graph["sources"], key=lambda item: item["source_id"])
        sorted_nodes = sorted(graph["nodes"], key=lambda item: item["node_id"])
        sorted_edges = sorted(graph["edges"], key=lambda item: item["edge_id"])
        sorted_rejected = sorted(
            graph["rejected_context"], key=lambda item: item["edge"]["edge_id"]
        )
        if graph["sources"] != sorted_sources:
            errors.append("query_context_graph.sources must use compiler canonical order")
        if graph["nodes"] != sorted_nodes:
            errors.append("query_context_graph.nodes must use compiler canonical order")
        if graph["edges"] != sorted_edges:
            errors.append("query_context_graph.edges must use compiler canonical order")
        if graph["rejected_context"] != sorted_rejected:
            errors.append(
                "query_context_graph.rejected_context must use compiler canonical order"
            )
        for source_index, source in enumerate(graph["sources"]):
            source_core = {
                key: copy.deepcopy(value)
                for key, value in source.items()
                if key != "source_id"
            }
            if source["source_id"] != _deterministic_identifier(
                "source", source_core
            ):
                errors.append(
                    f"query_context_graph.sources[{source_index}].source_id is not "
                    "the deterministic content ID"
                )
        for node_index, node in enumerate(graph["nodes"]):
            node_core = {
                key: copy.deepcopy(value)
                for key, value in node.items()
                if key != "node_id"
            }
            if node["node_id"] != _deterministic_identifier("node", node_core):
                errors.append(
                    f"query_context_graph.nodes[{node_index}].node_id is not the "
                    "deterministic content ID"
                )
        for edge_index, edge in enumerate(all_graph_edges):
            edge_core = {
                key: copy.deepcopy(value)
                for key, value in edge.items()
                if key != "edge_id"
            }
            if edge["edge_id"] != _deterministic_identifier("edge", edge_core):
                errors.append(
                    f"query_context_graph context edge[{edge_index}].edge_id is not "
                    "the deterministic content ID"
                )
        qcg_core = {
            "sources": sorted_sources,
            "nodes": sorted_nodes,
            "edges": sorted_edges,
            "rejected_context": sorted_rejected,
        }
        if graph["graph_id"] != _deterministic_identifier("qcg", qcg_core):
            errors.append(
                "query_context_graph.graph_id is not the deterministic content ID"
            )

    for ambiguity_index, ambiguity in enumerate(contract["ambiguity"]):
        ambiguity_core = {
            key: copy.deepcopy(ambiguity[key])
            for key in ("field", "field_path", "issue", "impact", "resolution")
        }
        if ambiguity["ambiguity_id"] != _deterministic_identifier(
            "ambiguity", ambiguity_core
        ):
            errors.append(
                f"ambiguity[{ambiguity_index}].ambiguity_id is not the "
                "deterministic content ID"
            )
        for candidate_index, candidate in enumerate(ambiguity["candidates"]):
            candidate_core = {
                "ambiguity_ref": ambiguity["ambiguity_id"],
                "value": copy.deepcopy(candidate["value"]),
                "confidence": candidate["confidence"],
                "basis": candidate["basis"],
                "basis_refs": copy.deepcopy(candidate["basis_refs"]),
            }
            if candidate["candidate_id"] != _deterministic_identifier(
                "candidate", candidate_core
            ):
                errors.append(
                    f"ambiguity[{ambiguity_index}].candidates[{candidate_index}]"
                    ".candidate_id is not the deterministic content ID"
                )

    for exclusion_index, exclusion in enumerate(
        record["branching"]["excluded_combinations"]
    ):
        exclusion_core = {
            key: copy.deepcopy(value)
            for key, value in exclusion.items()
            if key != "exclusion_id"
        }
        if exclusion["exclusion_id"] != _deterministic_identifier(
            "exclusion", exclusion_core
        ):
            errors.append(
                f"excluded_combinations[{exclusion_index}].exclusion_id is not "
                "the deterministic content ID"
            )

    for path_index, path in enumerate(record["candidate_query_paths"]):
        branch_core = {
            "question_id": record["question_id"],
            "original_question": record["original_question"],
            "selected_candidates": copy.deepcopy(path["selected_candidates"]),
            "candidate_intent": copy.deepcopy(path["candidate_intent"]),
        }
        if path["branch_id"] != _deterministic_identifier("branch", branch_core):
            errors.append(
                f"candidate_query_paths[{path_index}].branch_id is not the "
                "deterministic content ID"
            )
        for diff_index, diff in enumerate(path["intent_diffs"]):
            diff_core = {
                key: copy.deepcopy(value)
                for key, value in diff.items()
                if key != "intent_diff_id"
            }
            identity = {
                "selected_candidates": copy.deepcopy(path["selected_candidates"]),
                "diff": diff_core,
            }
            if diff["intent_diff_id"] != _deterministic_identifier("diff", identity):
                errors.append(
                    f"candidate_query_paths[{path_index}].intent_diffs"
                    f"[{diff_index}].intent_diff_id is not the deterministic content ID"
                )
        for assumption_index, assumption in enumerate(path["assumptions"]):
            assumption_core = {
                key: copy.deepcopy(value)
                for key, value in assumption.items()
                if key != "assumption_id"
            }
            identity = {
                "selected_candidates": copy.deepcopy(path["selected_candidates"]),
                "index": assumption_index,
                "assumption": assumption_core,
            }
            if assumption["assumption_id"] != _deterministic_identifier(
                "assumption", identity
            ):
                errors.append(
                    f"candidate_query_paths[{path_index}].assumptions"
                    f"[{assumption_index}].assumption_id is not the deterministic "
                    "content ID"
                )

    qic_core = {
        key: copy.deepcopy(contract[key])
        for key in (
            "question_id",
            "original_question",
            "requested",
            "not_requested",
            "forbidden",
            "ambiguity",
        )
    }
    qic_core["intent_origin"] = contract["provenance"]["intent_origin"]
    qic_core["intent_input_sha256"] = contract["provenance"][
        "intent_input_sha256"
    ]
    if contract["question_intent_contract_id"] != _deterministic_identifier(
        "qic", qic_core, 32
    ):
        errors.append(
            "question_intent_contract_id is not the deterministic content ID"
        )

    qcg_id = graph["graph_id"] if graph is not None else None
    qur_core = {
        "question_intent_contract_id": contract["question_intent_contract_id"],
        "query_context_graph_id": qcg_id,
        "branching": copy.deepcopy(record["branching"]),
        "candidate_query_paths": copy.deepcopy(record["candidate_query_paths"]),
        "intent_gate": copy.deepcopy(record["intent_gate"]),
    }
    if record["final_status"] == "failed":
        qur_core["errors"] = copy.deepcopy(record["errors"])
    if record["question_understanding_run_id"] != _deterministic_identifier(
        "qur", qur_core, 32
    ):
        errors.append(
            "question_understanding_run_id is not the deterministic content ID"
        )
    return errors


def _validate_question_understanding_run(record: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    reserved_paths = _question_understanding_reserved_key_paths(record)
    if reserved_paths:
        errors.append(
            "question_understanding_run contains post-intent answer, Evidence, "
            f"Primary, or Retrieval fields: {reserved_paths}"
        )

    contract = record["question_intent_contract"]
    if (
        record["provenance"]["runner"] != "question-understanding-compiler"
        or record["provenance"]["runner_version"] != "0.1"
        or contract["provenance"]["analyzer"]
        != "question-understanding-compiler"
        or contract["provenance"]["analyzer_version"] != "0.1"
    ):
        errors.append(
            "question_understanding_run embedded analyzer and runner provenance "
            "must be question-understanding-compiler/0.1"
        )
    errors.extend(
        f"question_intent_contract: {error}"
        for error in _validate_question_intent_contract(contract)
    )
    if record["question_id"] != contract["question_id"]:
        errors.append(
            "question_understanding_run question_id does not match "
            "question_intent_contract"
        )
    if record["original_question"] != contract["original_question"]:
        errors.append(
            "question_understanding_run original_question does not match "
            "question_intent_contract"
        )
    if record["runtime_metadata"]["rule_version"] != contract["provenance"]["rule_version"]:
        errors.append(
            "runtime_metadata.rule_version does not match question_intent_contract"
        )

    context_errors, context_refs, context_nodes_by_id, _ = (
        _validate_question_context_graph(record)
    )
    errors.extend(context_errors)
    explicit_question_mentions = _question_explicit_mentions(
        record, context_nodes_by_id
    )
    raw_contract_errors = (
        _raw_question_contract_errors(
            record["original_question"],
            explicit_question_mentions,
            contract,
            record["candidate_query_paths"],
        )
        if record["final_status"] != "failed"
        else {
            "operator_preserved": [],
            "hard_scope_not_expanded": [],
            "output_contract_match": [],
        }
    )
    deterministic_audit = _deterministic_intent_audit(
        record, context_nodes_by_id
    )
    errors.extend(_validate_compiler_controlled_text(record, context_nodes_by_id))
    errors.extend(_validate_compiler_identifiers(record))

    ambiguities = contract["ambiguity"]
    ambiguity_ids = [item["ambiguity_id"] for item in ambiguities]
    duplicate_ambiguity_ids = _duplicates(ambiguity_ids)
    if duplicate_ambiguity_ids:
        errors.append(f"duplicate ambiguity_id: {duplicate_ambiguity_ids}")
    ambiguity_paths = [item["field_path"] for item in ambiguities]
    duplicate_ambiguity_paths = _duplicates(ambiguity_paths)
    if duplicate_ambiguity_paths:
        errors.append(
            "each ambiguity field_path must be unique: "
            f"{duplicate_ambiguity_paths}"
        )
    ambiguity_by_id = {item["ambiguity_id"]: item for item in ambiguities}
    allowed_ambiguity_prefixes = {
        "target": "/requested/target",
        "operation": "/requested/operation_graph",
        "return_field": "/requested/requested_outputs",
        "scope": "/requested/scope",
        "answer_shape": "/requested/requested_outputs",
    }
    allowed_diff_components = {
        "target": {"target"},
        "scope": {"scope", "operation_graph"},
        "operation": {"operation_graph", "requested_outputs"},
        "return_field": {"requested_outputs", "operation_graph"},
        "answer_shape": {"requested_outputs"},
    }

    candidate_ids_list: list[str] = []
    candidate_by_id: dict[str, dict[str, Any]] = {}
    candidate_owner: dict[str, str] = {}
    for ambiguity_index, ambiguity in enumerate(ambiguities):
        ambiguity_id = ambiguity["ambiguity_id"]
        field_path = ambiguity["field_path"]
        try:
            _json_pointer_get(contract, field_path)
        except (KeyError, IndexError, ValueError) as exc:
            errors.append(
                f"ambiguity {ambiguity_id!r} field_path does not resolve: {exc}"
            )
        expected_prefix = allowed_ambiguity_prefixes[ambiguity["field"]]
        if not _pointer_is_within(field_path, expected_prefix):
            errors.append(
                f"ambiguity {ambiguity_id!r} field_path is outside its "
                f"{ambiguity['field']!r} domain"
            )
        candidate_value_keys: list[str] = []
        for candidate_index, candidate in enumerate(ambiguity["candidates"]):
            candidate_id = candidate["candidate_id"]
            candidate_ids_list.append(candidate_id)
            candidate_by_id[candidate_id] = candidate
            candidate_owner[candidate_id] = ambiguity_id
            candidate_value_keys.append(_canonical_identifier_key(candidate["value"]))
            _add_dangling_refs(
                errors,
                f"ambiguity[{ambiguity_index}].candidates[{candidate_index}].basis_refs",
                candidate["basis_refs"],
                context_refs,
            )
        duplicate_values = _duplicates(candidate_value_keys)
        if duplicate_values:
            errors.append(
                f"ambiguity {ambiguity_id!r} has duplicate candidate values"
            )
    duplicate_candidate_ids = _duplicates(candidate_ids_list)
    if duplicate_candidate_ids:
        errors.append(f"duplicate candidate_id: {duplicate_candidate_ids}")
    ambiguity_id_set = set(ambiguity_ids)
    candidate_id_set = set(candidate_ids_list)
    identity_overlap = sorted(ambiguity_id_set & candidate_id_set)
    if identity_overlap:
        errors.append(
            "ambiguity_id and candidate_id namespaces must be disjoint: "
            f"{identity_overlap}"
        )

    branching = record["branching"]
    source_ambiguity_refs = branching["source_ambiguity_refs"]
    _add_dangling_refs(
        errors,
        "branching.source_ambiguity_refs",
        source_ambiguity_refs,
        ambiguity_id_set,
    )
    if set(source_ambiguity_refs) != ambiguity_id_set:
        errors.append(
            "branching.source_ambiguity_refs must contain exactly every ambiguity"
        )
    if not source_ambiguity_refs and branching["strategy"] != "single":
        errors.append("branching without ambiguity refs requires strategy='single'")
    if source_ambiguity_refs and branching["strategy"] == "single":
        errors.append("branching with ambiguity refs cannot use strategy='single'")
    non_limit_exclusions = [
        item
        for item in branching["excluded_combinations"]
        if item["reason_code"] != "branch_limit_exceeded"
    ]
    if branching["strategy"] in {"single", "full_cartesian"} and non_limit_exclusions:
        errors.append(
            f"branching strategy {branching['strategy']!r} cannot semantically "
            "exclude combinations"
        )

    candidate_ids_by_ambiguity = {
        ambiguity_id: [
            candidate["candidate_id"]
            for candidate in ambiguity_by_id[ambiguity_id]["candidates"]
        ]
        for ambiguity_id in source_ambiguity_refs
        if ambiguity_id in ambiguity_by_id
    }
    combination_space_is_valid = (
        len(candidate_ids_by_ambiguity) == len(source_ambiguity_refs)
    )
    expected_combination_count = (
        math.prod(
            len(candidate_ids_by_ambiguity[ambiguity_id])
            for ambiguity_id in source_ambiguity_refs
        )
        if combination_space_is_valid
        else 0
    )

    exclusion_ids: list[str] = []
    excluded_combinations: set[tuple[str, ...]] = set()
    for exclusion_index, exclusion in enumerate(branching["excluded_combinations"]):
        exclusion_ids.append(exclusion["exclusion_id"])
        _add_dangling_refs(
            errors,
            f"excluded_combinations[{exclusion_index}].candidate_refs",
            exclusion["candidate_refs"],
            candidate_id_set,
        )
        _add_dangling_refs(
            errors,
            f"excluded_combinations[{exclusion_index}].basis_refs",
            exclusion["basis_refs"],
            context_refs,
        )
        if exclusion["reason_code"] == "branch_limit_exceeded":
            if set(exclusion["candidate_refs"]) != candidate_id_set:
                errors.append(
                    f"excluded_combinations[{exclusion_index}] branch-limit "
                    "sentinel must name every candidate"
                )
            continue
        by_ambiguity: dict[str, str] = {}
        for candidate_ref in exclusion["candidate_refs"]:
            owner = candidate_owner.get(candidate_ref)
            if owner is None:
                continue
            if owner in by_ambiguity:
                errors.append(
                    f"excluded_combinations[{exclusion_index}] selects multiple "
                    f"candidates for ambiguity {owner!r}"
                )
            by_ambiguity[owner] = candidate_ref
        if set(by_ambiguity) != set(source_ambiguity_refs):
            errors.append(
                f"excluded_combinations[{exclusion_index}] must select one "
                "candidate for every source ambiguity"
            )
        else:
            combination = tuple(
                by_ambiguity[ambiguity_id]
                for ambiguity_id in source_ambiguity_refs
            )
            if combination in excluded_combinations:
                errors.append(
                    f"duplicate excluded candidate combination: {combination}"
                )
            excluded_combinations.add(combination)
    duplicate_exclusion_ids = _duplicates(exclusion_ids)
    if duplicate_exclusion_ids:
        errors.append(f"duplicate exclusion_id: {duplicate_exclusion_ids}")

    candidate_paths = record["candidate_query_paths"]
    branch_ids_list = [path["branch_id"] for path in candidate_paths]
    duplicate_branch_ids = _duplicates(branch_ids_list)
    if duplicate_branch_ids:
        errors.append(f"duplicate branch_id: {duplicate_branch_ids}")
    branch_ids = set(branch_ids_list)
    candidate_path_by_branch = {
        path["branch_id"]: path for path in candidate_paths
    }
    intent_diff_ids: list[str] = []
    assumption_ids: list[str] = []
    actual_combinations: set[tuple[str, ...]] = set()
    candidate_operation_ids: set[str] = set()
    candidate_graph_ids: set[str] = set()
    candidate_output_ids: set[str] = set()
    candidate_external_input_ids: set[str] = set()
    candidate_contract_errors_by_branch: dict[str, list[str]] = {}
    isolated_candidate_intents: dict[str, dict[str, Any]] = {}

    for path_index, path in enumerate(candidate_paths):
        branch_id = path["branch_id"]
        if path["parent_question_id"] != record["question_id"]:
            errors.append(
                f"candidate_query_paths[{path_index}].parent_question_id differs "
                "from question_understanding_run"
            )
        selected_pairs = [
            (selected["ambiguity_ref"], selected["candidate_ref"])
            for selected in path["selected_candidates"]
        ]
        selected_ambiguities = [ambiguity_ref for ambiguity_ref, _ in selected_pairs]
        duplicate_selected_ambiguities = _duplicates(selected_ambiguities)
        if duplicate_selected_ambiguities:
            errors.append(
                f"branch {branch_id!r} selects multiple candidates for "
                f"ambiguities: {duplicate_selected_ambiguities}"
            )
        selected_by_ambiguity: dict[str, str] = {}
        for selected_index, (ambiguity_ref, candidate_ref) in enumerate(selected_pairs):
            if ambiguity_ref not in ambiguity_id_set:
                errors.append(
                    f"branch {branch_id!r} selected_candidates[{selected_index}] "
                    f"has dangling ambiguity_ref {ambiguity_ref!r}"
                )
            if candidate_ref not in candidate_id_set:
                errors.append(
                    f"branch {branch_id!r} selected_candidates[{selected_index}] "
                    f"has dangling candidate_ref {candidate_ref!r}"
                )
            elif candidate_owner[candidate_ref] != ambiguity_ref:
                errors.append(
                    f"branch {branch_id!r} candidate_ref {candidate_ref!r} does "
                    f"not belong to ambiguity_ref {ambiguity_ref!r}"
                )
            selected_by_ambiguity[ambiguity_ref] = candidate_ref
        if set(selected_by_ambiguity) != set(source_ambiguity_refs):
            errors.append(
                f"branch {branch_id!r} must select exactly one candidate for "
                "every source ambiguity"
            )
        else:
            combination = tuple(
                selected_by_ambiguity[ambiguity_id]
                for ambiguity_id in source_ambiguity_refs
            )
            if combination in excluded_combinations:
                errors.append(
                    f"branch {branch_id!r} uses an explicitly excluded candidate "
                    "combination"
                )
            if combination in actual_combinations:
                errors.append(f"duplicate candidate branch combination: {combination}")
            actual_combinations.add(combination)

        reconstructed_root: dict[str, Any] = {
            "requested": copy.deepcopy(contract["requested"])
        }
        branch_diff_paths: list[str] = []
        for diff_index, diff in enumerate(path["intent_diffs"]):
            intent_diff_ids.append(diff["intent_diff_id"])
            branch_diff_paths.append(diff["field_path"])
            owner = candidate_owner.get(diff["candidate_ref"])
            if diff["ambiguity_ref"] not in ambiguity_id_set:
                errors.append(
                    f"branch {branch_id!r} intent_diffs[{diff_index}] has dangling "
                    f"ambiguity_ref {diff['ambiguity_ref']!r}"
                )
            if owner is None:
                errors.append(
                    f"branch {branch_id!r} intent_diffs[{diff_index}] has dangling "
                    f"candidate_ref {diff['candidate_ref']!r}"
                )
            elif owner != diff["ambiguity_ref"]:
                errors.append(
                    f"branch {branch_id!r} intent_diffs[{diff_index}] candidate/"
                    "ambiguity ownership mismatch"
                )
            if (
                diff["ambiguity_ref"], diff["candidate_ref"]
            ) not in set(selected_pairs):
                errors.append(
                    f"branch {branch_id!r} intent_diffs[{diff_index}] is not "
                    "authorized by selected_candidates"
                )
            ambiguity = ambiguity_by_id.get(diff["ambiguity_ref"])
            if ambiguity is not None:
                diff_parts = _decode_json_pointer(diff["field_path"])
                component = diff_parts[1] if len(diff_parts) > 1 else None
                if component not in allowed_diff_components[ambiguity["field"]]:
                    errors.append(
                        f"branch {branch_id!r} intent_diffs[{diff_index}].field_path "
                        "is outside the allowed components for its ambiguity field"
                    )
            try:
                current_value = _json_pointer_get(
                    reconstructed_root, diff["field_path"]
                )
            except (KeyError, IndexError, ValueError) as exc:
                errors.append(
                    f"branch {branch_id!r} intent_diffs[{diff_index}].field_path "
                    f"does not resolve: {exc}"
                )
                continue
            if not _same_json(current_value, diff["before"]):
                errors.append(
                    f"branch {branch_id!r} intent_diffs[{diff_index}].before "
                    "does not match the current sequential intent state"
                )
            if _same_json(diff["before"], diff["after"]):
                errors.append(
                    f"branch {branch_id!r} intent_diffs[{diff_index}] is a no-op"
                )
            try:
                _json_pointer_replace(
                    reconstructed_root, diff["field_path"], diff["after"]
                )
            except (KeyError, IndexError, ValueError) as exc:
                errors.append(
                    f"branch {branch_id!r} intent_diffs[{diff_index}] cannot be "
                    f"applied: {exc}"
                )
        for left_index, left_path in enumerate(branch_diff_paths):
            for right_path in branch_diff_paths[left_index + 1 :]:
                if _pointers_overlap(left_path, right_path):
                    errors.append(
                        f"branch {branch_id!r} intent diff paths overlap: "
                        f"{left_path!r}, {right_path!r}"
                    )

        reconstructed = reconstructed_root["requested"]
        reconstructed["derived_summary"] = _recompute_requested_summary(reconstructed)
        if not _same_json(reconstructed, path["candidate_intent"]):
            errors.append(
                f"branch {branch_id!r} candidate_intent does not equal the base "
                "requested intent plus its authorized intent_diffs"
            )

        diffs_by_selection: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for diff in path["intent_diffs"]:
            key = (diff["ambiguity_ref"], diff["candidate_ref"])
            diffs_by_selection.setdefault(key, []).append(diff)
        for ambiguity_ref, candidate_ref in selected_pairs:
            ambiguity = ambiguity_by_id.get(ambiguity_ref)
            candidate = candidate_by_id.get(candidate_ref)
            if ambiguity is None or candidate is None:
                continue
            primary_diffs = [
                diff
                for diff in diffs_by_selection.get(
                    (ambiguity_ref, candidate_ref), []
                )
                if diff["field_path"] == ambiguity["field_path"]
            ]
            if len(primary_diffs) != 1:
                errors.append(
                    f"branch {branch_id!r} selected candidate {candidate_ref!r} "
                    "requires exactly one intent diff at its ambiguity field_path"
                )
            elif not _same_json(primary_diffs[0]["after"], candidate["value"]):
                errors.append(
                    f"branch {branch_id!r} selected candidate {candidate_ref!r} "
                    "intent diff does not match its declared candidate value"
                )
            isolated_root = {"requested": copy.deepcopy(contract["requested"])}
            isolated_ok = True
            for diff in diffs_by_selection.get(
                (ambiguity_ref, candidate_ref), []
            ):
                try:
                    _json_pointer_replace(
                        isolated_root, diff["field_path"], diff["after"]
                    )
                except (KeyError, IndexError, ValueError):
                    isolated_ok = False
                    break
            if isolated_ok:
                isolated = isolated_root["requested"]
                isolated["derived_summary"] = _recompute_requested_summary(
                    isolated
                )
                previous = isolated_candidate_intents.get(candidate_ref)
                if previous is not None and not _same_json(previous, isolated):
                    errors.append(
                        f"candidate {candidate_ref!r} reconstructs differently "
                        "across Cartesian branches"
                    )
                else:
                    isolated_candidate_intents[candidate_ref] = isolated

        expected_assumptions = sorted(
            _canonical_identifier_key(
                {
                    "statement": candidate_by_id[candidate_ref]["basis"],
                    "basis_refs": sorted(
                        candidate_by_id[candidate_ref]["basis_refs"]
                    ),
                    "impact": ambiguity_by_id[ambiguity_ref]["impact"],
                }
            )
            for ambiguity_ref, candidate_ref in selected_pairs
            if ambiguity_ref in ambiguity_by_id and candidate_ref in candidate_by_id
        )
        actual_assumptions = sorted(
            _canonical_identifier_key(
                {
                    "statement": assumption["statement"],
                    "basis_refs": sorted(assumption["basis_refs"]),
                    "impact": assumption["impact"],
                }
            )
            for assumption in path["assumptions"]
        )
        if actual_assumptions != expected_assumptions:
            errors.append(
                f"branch {branch_id!r} assumptions must correspond exactly to "
                "its selected ambiguity candidates"
            )
        candidate_contract = copy.deepcopy(contract)
        candidate_contract["requested"] = copy.deepcopy(path["candidate_intent"])
        candidate_contract_errors = _validate_question_intent_contract(
            candidate_contract
        )
        candidate_contract_errors_by_branch[branch_id] = candidate_contract_errors
        errors.extend(
            f"branch {branch_id!r} candidate_intent: {error}"
            for error in candidate_contract_errors
        )

        candidate_graph = path["candidate_intent"]["operation_graph"]
        candidate_graph_ids.add(candidate_graph["operation_graph_id"])
        candidate_operation_ids.update(
            node["operation_id"] for node in candidate_graph["nodes"]
        )
        candidate_external_input_ids.update(
            item["input_ref"] for item in candidate_graph["external_inputs"]
        )
        candidate_output_ids.update(
            output["output_id"]
            for output in path["candidate_intent"]["requested_outputs"]
        )
        for assumption_index, assumption in enumerate(path["assumptions"]):
            assumption_ids.append(assumption["assumption_id"])
            _add_dangling_refs(
                errors,
                f"branch {branch_id!r} assumptions[{assumption_index}].basis_refs",
                assumption["basis_refs"],
                context_refs,
            )

    duplicate_diff_ids = _duplicates(intent_diff_ids)
    if duplicate_diff_ids:
        errors.append(f"duplicate intent_diff_id: {duplicate_diff_ids}")
    duplicate_assumption_ids = _duplicates(assumption_ids)
    if duplicate_assumption_ids:
        errors.append(f"duplicate assumption_id: {duplicate_assumption_ids}")

    if record["query_context_graph"] is not None:
        explicit_mentions = _question_explicit_mentions(
            record, context_nodes_by_id
        )
        expected_question_ref = "question:" + _safe_question_token(
            record["question_id"], record["original_question"]
        )
        fallback_source = next(
            (
                source["source_id"]
                for source in record["query_context_graph"]["sources"]
                if source["source_ref"] == expected_question_ref
            ),
            None,
        )
        if fallback_source is not None:
            for candidate_ref, isolated_intent in isolated_candidate_intents.items():
                ambiguity_ref = candidate_owner.get(candidate_ref)
                ambiguity = ambiguity_by_id.get(ambiguity_ref or "")
                candidate = candidate_by_id.get(candidate_ref)
                if ambiguity is None or candidate is None:
                    continue
                expected_basis_refs = _expected_candidate_basis_refs(
                    ambiguity["field"],
                    isolated_intent,
                    explicit_mentions,
                    fallback_source,
                )
                if candidate["basis_refs"] != expected_basis_refs:
                    errors.append(
                        f"candidate {candidate_ref!r} basis_refs must equal its "
                        "exact material question support"
                    )

    logical_branch_limit = branching["logical_branch_limit"]
    branch_limit_exceeded = (
        logical_branch_limit is not None
        and expected_combination_count > logical_branch_limit
    )
    if logical_branch_limit is not None and len(candidate_paths) > logical_branch_limit:
        errors.append("candidate_query_paths exceeds branching.logical_branch_limit")
    gate = record["intent_gate"]
    gate_reason_codes = set(gate["reason_codes"]) if gate is not None else set()
    branch_limit_exclusions = [
        exclusion
        for exclusion in branching["excluded_combinations"]
        if exclusion["reason_code"] == "branch_limit_exceeded"
    ]
    candidate_paths_completed = (
        record["stage_statuses"]["candidate_paths"] == "completed"
    )
    if branch_limit_exceeded and candidate_paths_completed:
        if len(branch_limit_exclusions) != 1:
            errors.append(
                "an exceeded logical_branch_limit requires exactly one "
                "branch_limit_exceeded exclusion sentinel"
            )
        if candidate_paths:
            errors.append(
                "an exceeded logical_branch_limit must fail closed without "
                "emitting a truncated candidate branch subset"
            )
        if record["final_status"] == "ready_for_retrieval":
            errors.append(
                "ready_for_retrieval cannot silently truncate a Cartesian branch set"
            )
        if "branch_limit_exceeded" not in gate_reason_codes:
            errors.append(
                "an exceeded logical_branch_limit requires gate reason "
                "branch_limit_exceeded"
            )
    elif candidate_paths_completed:
        if branch_limit_exclusions:
            errors.append(
                "branch_limit_exceeded exclusion is forbidden when the declared "
                "Cartesian space does not exceed logical_branch_limit"
            )
        if (
            combination_space_is_valid
            and len(actual_combinations)
            != expected_combination_count - len(excluded_combinations)
        ):
            errors.append(
                "candidate_query_paths does not cover the declared Cartesian branch "
                "space exactly"
            )
    elif branch_limit_exclusions:
        errors.append(
            "branch_limit_exceeded exclusion is forbidden before candidate-path "
            "processing completes"
        )
    if (
        candidate_paths_completed
        and branching["strategy"] == "constrained"
        and not excluded_combinations
    ):
        errors.append("constrained branching requires an excluded combination")
    if record["final_status"] == "ready_for_retrieval":
        if branching["strategy"] == "constrained":
            errors.append(
                "ready_for_retrieval cannot use constrained branching until "
                "typed exclusion proofs are implemented"
            )
        unresolved_high = sorted(
            ambiguity["ambiguity_id"]
            for ambiguity in ambiguities
            if ambiguity["impact"] == "high"
            and ambiguity["ambiguity_id"] not in set(source_ambiguity_refs)
        )
        if unresolved_high:
            errors.append(
                "ready_for_retrieval retains unbranched high-impact ambiguity: "
                f"{unresolved_high}"
            )
        abstaining_ambiguities = sorted(
            ambiguity["ambiguity_id"]
            for ambiguity in ambiguities
            if "abstain" in ambiguity["resolution"]
        )
        if abstaining_ambiguities:
            errors.append(
                "ready_for_retrieval cannot retain an ambiguity whose resolution "
                f"requires abstention: {abstaining_ambiguities}"
            )

    base_graph = contract["requested"]["operation_graph"]
    base_operation_ids = {
        node["operation_id"] for node in base_graph["nodes"]
    }
    base_output_ids = {
        output["output_id"]
        for output in contract["requested"]["requested_outputs"]
    }
    base_external_input_ids = {
        item["input_ref"] for item in base_graph["external_inputs"]
    }
    known_refs = (
        context_refs
        | ambiguity_id_set
        | candidate_id_set
        | branch_ids
        | set(intent_diff_ids)
        | set(assumption_ids)
        | set(exclusion_ids)
        | base_operation_ids
        | base_output_ids
        | base_external_input_ids
        | candidate_operation_ids
        | candidate_output_ids
        | candidate_external_input_ids
        | candidate_graph_ids
        | {
            record["question_understanding_run_id"],
            contract["question_intent_contract_id"],
            base_graph["operation_graph_id"],
        }
        | {
            value
            for value in (record["question_id"],)
            if isinstance(value, str)
        }
    )

    rules = [
        rule
        for category_rules in contract["forbidden"].values()
        for rule in category_rules
    ]
    rules_by_id = {rule["rule_id"]: rule for rule in rules}
    intent_rules = [rule for rule in rules if "intent" in rule["applies_to"]]
    intent_rule_ids = {rule["rule_id"] for rule in intent_rules}
    results = record["forbidden_check_results"]
    result_rule_ids = [result["rule_id"] for result in results]
    duplicate_result_rules = _duplicates(result_rule_ids)
    if duplicate_result_rules:
        errors.append(
            "duplicate intent forbidden_check_result rule_id: "
            f"{duplicate_result_rules}"
        )
    intent_stage_executed = record["stage_statuses"]["intent_gate"] == "completed"
    if intent_stage_executed and set(result_rule_ids) != intent_rule_ids:
        errors.append(
            "intent forbidden_check_results must cover exactly every intent-stage "
            f"rule; expected={sorted(intent_rule_ids)}"
        )
    if not intent_stage_executed and results:
        errors.append(
            "intent forbidden_check_results must be empty when intent_gate was not completed"
        )

    effective_operation_ids = candidate_operation_ids or base_operation_ids
    effective_graph_ids = candidate_graph_ids or {base_graph["operation_graph_id"]}
    effective_output_ids = candidate_output_ids or base_output_ids
    expected_subjects_by_validator = {
        "operator_preserved": branch_ids | effective_operation_ids,
        "hard_scope_not_expanded": branch_ids | effective_graph_ids,
        "output_contract_match": branch_ids | effective_output_ids,
    }
    forbidden_all_pass = bool(results)
    for result_index, result in enumerate(results):
        rule = rules_by_id.get(result["rule_id"])
        if rule is None:
            errors.append(
                f"forbidden_check_results[{result_index}] has dangling rule_id "
                f"{result['rule_id']!r}"
            )
        else:
            expected_validator = rule["check"]["validator_id"]
            if "intent" not in rule["applies_to"]:
                errors.append(
                    f"forbidden_check_results[{result_index}] refers to a non-intent rule"
                )
            if result["validator_id"] != expected_validator:
                errors.append(
                    f"forbidden_check_results[{result_index}].validator_id differs "
                    "from its rule"
                )
            expected_status = (
                "pass"
                if deterministic_audit.get(result["validator_id"], False)
                else "violation"
            )
            if result["status"] != expected_status:
                errors.append(
                    f"forbidden_check_results[{result_index}].status must be "
                    f"{expected_status!r} when recomputed from exact question/QCG "
                    "bindings"
                )
            if result["status"] == "pass" and result["action_taken"] != "none":
                errors.append(
                    f"forbidden_check_results[{result_index}] pass requires "
                    "action_taken='none'"
                )
            if (
                result["status"] == "violation"
                and result["action_taken"] != rule["on_violation"]
            ):
                errors.append(
                    f"forbidden_check_results[{result_index}] violation action "
                    "differs from its rule"
                )
            if result["status"] == "error" and result["action_taken"] not in {
                "reject",
                "abstain",
            }:
                errors.append(
                    f"forbidden_check_results[{result_index}] error must fail closed"
                )
        if result["validator_version"] != VALIDATOR_IMPLEMENTATION_VERSION:
            errors.append(
                f"forbidden_check_results[{result_index}].validator_version must "
                f"be {VALIDATOR_IMPLEMENTATION_VERSION!r}"
            )
        expected_subjects = expected_subjects_by_validator[result["validator_id"]]
        if set(result["subject_refs"]) != expected_subjects:
            errors.append(
                f"forbidden_check_results[{result_index}].subject_refs must cover "
                f"exactly {sorted(expected_subjects)} for validator_id "
                f"{result['validator_id']!r}"
            )
        audit_passed = deterministic_audit.get(result["validator_id"], False)
        expected_message = (
            "Every base and candidate intent has exact question bindings."
            if audit_passed
            else "At least one raw question constraint is absent from the "
            "compiled intent."
            if raw_contract_errors.get(result["validator_id"])
            else "At least one base or candidate intent contains unbound semantics."
        )
        if result["details"]["message"] != expected_message:
            errors.append(
                f"forbidden_check_results[{result_index}].details.message must be "
                "the deterministic audit message"
            )
        if audit_passed:
            expected_pass_paths = {
                "operator_preserved": ["/requested/operation_graph"],
                "hard_scope_not_expanded": [
                    "/requested/target",
                    "/requested/scope",
                ],
                "output_contract_match": ["/requested/requested_outputs"],
            }[result["validator_id"]]
            if result["details"]["field_paths"] != expected_pass_paths:
                errors.append(
                    f"forbidden_check_results[{result_index}].details.field_paths "
                    "must be the deterministic pass paths"
                )
        for field_path in result["details"]["field_paths"]:
            try:
                _json_pointer_get(contract, field_path)
            except (KeyError, IndexError, ValueError) as exc:
                errors.append(
                    f"forbidden_check_results[{result_index}] field_path does not "
                    f"resolve: {exc}"
                )
        forbidden_all_pass = forbidden_all_pass and result["status"] == "pass"

    if gate is not None:
        gate_branch_ids_list = [item["branch_id"] for item in gate["branch_results"]]
        duplicate_gate_branches = _duplicates(gate_branch_ids_list)
        if duplicate_gate_branches:
            errors.append(
                f"duplicate intent_gate branch result: {duplicate_gate_branches}"
            )
        if set(gate_branch_ids_list) != branch_ids:
            errors.append(
                "intent_gate.branch_results must cover exactly every candidate branch"
            )
        if gate_branch_ids_list != branch_ids_list:
            errors.append(
                "intent_gate.branch_results must follow candidate branch order"
            )
        branch_statuses: list[str] = []
        branch_reason_union: set[str] = set()
        for branch_index, branch_result in enumerate(gate["branch_results"]):
            branch_id = branch_result["branch_id"]
            checks = branch_result["checks"]
            path = candidate_path_by_branch.get(branch_id)
            expected_check_statuses: dict[str, str] = {}
            if path is not None:
                candidate_intent = path["candidate_intent"]
                candidate_graph = candidate_intent["operation_graph"]
                candidate_outputs = candidate_intent["requested_outputs"]
                candidate_compiles = not candidate_contract_errors_by_branch.get(
                    branch_id, ["candidate contract was not validated"]
                )
                target_resolved = (
                    candidate_intent["target"]["canonical_type"] is not None
                )
                outputs_resolved = all(
                    output["return_field"] != "unknown"
                    and output["cardinality"]["mode"] != "unknown"
                    and output["answer_shape"]["container"] != "unknown"
                    and output["answer_shape"]["value_type"] != "unknown"
                    for output in candidate_outputs
                )
                scope_resolved = (
                    candidate_intent["scope"]["source"] != "unknown"
                    and candidate_intent["scope"]["match_mode"] != "unknown"
                )
                type_status = _pre_retrieval_type_status(candidate_intent)
                explicit_consistent = all(deterministic_audit.values())
                question_equivalence_proven = (
                    _supported_contract_semantics_equal(
                        record["original_question"], contract["requested"]
                    )
                )
                explicit_status = (
                    "fail"
                    if not explicit_consistent
                    else "pass"
                    if question_equivalence_proven
                    else "indeterminate"
                )
                selected_ambiguity_refs = {
                    selected["ambiguity_ref"]
                    for selected in path["selected_candidates"]
                }
                ambiguity_branched = (
                    selected_ambiguity_refs == ambiguity_id_set
                    and not any(
                        "abstain" in ambiguity["resolution"]
                        for ambiguity in ambiguities
                    )
                )
                expected_check_statuses = {
                    "operation_graph_compilable": (
                        "pass" if candidate_compiles else "fail"
                    ),
                    "target_resolved": (
                        "pass" if target_resolved else "indeterminate"
                    ),
                    "requested_outputs_resolved": (
                        "pass" if outputs_resolved else "indeterminate"
                    ),
                    "scope_resolved": (
                        "pass" if scope_resolved else "indeterminate"
                    ),
                    "explicit_consistency": (
                        explicit_status
                    ),
                    "pre_retrieval_type_safety": (
                        type_status
                    ),
                    "forbidden_precheck": (
                        "pass"
                        if candidate_compiles and forbidden_all_pass
                        else "fail"
                    ),
                    "ambiguity_branched": (
                        "pass" if ambiguity_branched else "fail"
                    ),
                }
                requires_abstention = any(
                    "abstain" in ambiguity["resolution"]
                    for ambiguity in ambiguities
                )
                expected_check_subjects = {
                    "operation_graph_compilable": {
                        candidate_graph["operation_graph_id"]
                    },
                    "target_resolved": {branch_id},
                    "requested_outputs_resolved": {
                        output["output_id"] for output in candidate_outputs
                    },
                    "scope_resolved": {branch_id},
                    "explicit_consistency": {branch_id},
                    "pre_retrieval_type_safety": {
                        candidate_graph["operation_graph_id"]
                    },
                    "forbidden_precheck": {branch_id},
                    "ambiguity_branched": {branch_id},
                }
                expected_check_details = {
                    "operation_graph_compilable": (
                        "The operation graph compiles as a validated DAG."
                        if candidate_compiles
                        else "The operation graph or its references are invalid."
                    ),
                    "target_resolved": (
                        "Target type is resolved."
                        if target_resolved
                        else "Target type is unknown."
                    ),
                    "requested_outputs_resolved": (
                        "Requested output types and cardinalities are resolved."
                        if outputs_resolved
                        else "At least one requested output remains unknown."
                    ),
                    "scope_resolved": (
                        "Scope source and match mode are resolved."
                        if scope_resolved
                        else "Scope source or match mode is unknown."
                    ),
                    "explicit_consistency": (
                        "Explicit spans, operators, scope, and output constraints "
                        "are consistent."
                        if explicit_status == "pass"
                        else "The question grammar cannot prove the proposed intent is complete."
                        if explicit_status == "indeterminate"
                        else "The compiled intent contradicts an explicit question span."
                    ),
                    "pre_retrieval_type_safety": (
                        "All pre-retrieval operation and input types are known."
                        if type_status == "pass"
                        else "An operation or input type remains unknown."
                        if type_status == "indeterminate"
                        else "A known operation arity or value type is incompatible."
                    ),
                    "forbidden_precheck": (
                        "All deterministic intent-stage forbidden checks pass."
                        if candidate_compiles and forbidden_all_pass
                        else "A deterministic intent-stage forbidden check failed."
                    ),
                    "ambiguity_branched": (
                        "A recorded ambiguity explicitly requires abstention."
                        if requires_abstention
                        else "Every recorded ambiguity has one selected candidate in "
                        "this logical branch."
                    ),
                }
            else:
                expected_check_subjects = {}
                expected_check_details = {}
            check_ids = [check["check_id"] for check in checks]
            duplicate_check_ids = _duplicates(check_ids)
            if duplicate_check_ids:
                errors.append(
                    f"intent_gate branch {branch_id!r} has duplicate checks: "
                    f"{duplicate_check_ids}"
                )
            if set(check_ids) != INTENT_GATE_CHECK_IDS:
                errors.append(
                    f"intent_gate branch {branch_id!r} must contain exactly the "
                    "registered eight checks"
                )
            if tuple(check_ids) != INTENT_GATE_CHECK_ORDER:
                errors.append(
                    f"intent_gate branch {branch_id!r} checks must use compiler order"
                )
            for check_index, check in enumerate(checks):
                _add_dangling_refs(
                    errors,
                    f"intent_gate.branch_results[{branch_index}].checks[{check_index}]"
                    ".subject_refs",
                    check["subject_refs"],
                    known_refs,
                )
                expected_status = expected_check_statuses.get(check["check_id"])
                if expected_status is not None and check["status"] != expected_status:
                    errors.append(
                        f"intent_gate branch {branch_id!r} check "
                        f"{check['check_id']!r} must be {expected_status!r} when "
                        "recomputed from its candidate intent and exact bindings"
                    )
                expected_subjects = expected_check_subjects.get(check["check_id"])
                if (
                    expected_subjects is not None
                    and set(check["subject_refs"]) != expected_subjects
                ):
                    errors.append(
                        f"intent_gate branch {branch_id!r} check "
                        f"{check['check_id']!r} subject_refs must match its "
                        "recomputed subjects"
                    )
                expected_detail = expected_check_details.get(check["check_id"])
                if expected_detail is not None and check["detail"] != expected_detail:
                    errors.append(
                        f"intent_gate branch {branch_id!r} check "
                        f"{check['check_id']!r} detail must be compiler-controlled"
                    )
            check_statuses = [check["status"] for check in checks]
            expected_branch_status = (
                "fail"
                if "fail" in check_statuses
                else "indeterminate"
                if "indeterminate" in check_statuses
                else "pass"
            )
            if branch_result["status"] != expected_branch_status:
                errors.append(
                    f"intent_gate branch {branch_id!r} status must aggregate its checks"
                )
            if expected_branch_status != "pass" and not branch_result["reason_codes"]:
                errors.append(
                    f"intent_gate branch {branch_id!r} non-pass status requires a reason"
                )
            reason_by_check = {
                "operation_graph_compilable": "operation_graph_uncompilable",
                "target_resolved": "target_unresolved",
                "requested_outputs_resolved": "requested_outputs_unresolved",
                "scope_resolved": "scope_unresolved",
                "explicit_consistency": "explicit_conflict",
                "pre_retrieval_type_safety": "pre_retrieval_type_error",
                "forbidden_precheck": "forbidden_violation",
                "ambiguity_branched": "ambiguity_unbranched",
            }
            expected_branch_reasons = {
                (
                    "question_equivalence_unproven"
                    if check_id == "explicit_consistency"
                    and status == "indeterminate"
                    else reason_by_check[check_id]
                )
                for check_id, status in expected_check_statuses.items()
                if status != "pass"
            }
            if path is not None and set(branch_result["reason_codes"]) != (
                expected_branch_reasons
            ):
                errors.append(
                    f"intent_gate branch {branch_id!r} reason_codes must match "
                    "its recomputed non-pass checks exactly"
                )
            branch_statuses.append(branch_result["status"])
            branch_reason_union.update(branch_result["reason_codes"])

        if branch_statuses:
            expected_gate_status = (
                "fail"
                if "fail" in branch_statuses
                else "indeterminate"
                if "indeterminate" in branch_statuses
                else "pass"
            )
            if gate["status"] != expected_gate_status:
                errors.append("intent_gate status must aggregate every branch status")
        elif gate["status"] == "pass":
            errors.append("intent_gate cannot pass without a candidate branch")
        if branch_statuses and branch_reason_union != set(gate["reason_codes"]):
            errors.append(
                "intent_gate.reason_codes must equal the union of all branch "
                "reason codes"
            )
        if gate["status"] != "pass" and not gate["reason_codes"]:
            errors.append("a non-pass intent_gate requires at least one reason code")
        if not candidate_paths and not (
            set(gate["reason_codes"])
            & {"no_candidate_path", "question_underspecified", "branch_limit_exceeded"}
        ):
            errors.append(
                "an empty candidate branch set requires a fail-closed gate reason"
            )

    if record["final_status"] == "ready_for_retrieval":
        if gate is None or gate["status"] != "pass" or gate["action"] != "retrieve":
            errors.append("ready_for_retrieval requires intent_gate pass/retrieve")
        if not forbidden_all_pass:
            errors.append(
                "ready_for_retrieval requires every intent forbidden check to pass"
            )

    runtime = record["runtime_metadata"]
    started_at = _parse_datetime(runtime["started_at"])
    completed_at = _parse_datetime(runtime["completed_at"])
    run_generated_at = _parse_datetime(record["provenance"]["generated_at"])
    contract_generated_at = _parse_datetime(contract["provenance"]["generated_at"])
    if contract_generated_at != run_generated_at:
        errors.append(
            "question_intent_contract and question_understanding_run generated_at "
            "must be identical"
        )
    if not started_at <= run_generated_at <= completed_at:
        errors.append(
            "compiler generated_at must fall within the runtime timestamp interval"
        )
    if started_at > completed_at:
        errors.append("runtime_metadata.started_at must not be later than completed_at")
    interval_ms = (completed_at - started_at).total_seconds() * 1000
    if abs(runtime["duration_ms"] - interval_ms) > 1:
        errors.append(
            "runtime_metadata.duration_ms is inconsistent with its timestamp interval"
        )
    if (
        runtime["backend"] == "local_sequential"
        and runtime["parallel_config"]["max_concurrency"] != 1
    ):
        errors.append("local_sequential backend requires max_concurrency=1")
    duplicate_model_roles = _duplicates(model["role"] for model in runtime["models"])
    if duplicate_model_roles:
        errors.append(f"duplicate runtime model role: {duplicate_model_roles}")
    intent_models = [model for model in runtime["models"] if model["role"] == "intent"]
    validation_models = [
        model for model in runtime["models"] if model["role"] == "validation"
    ]
    if validation_models != [
        {
            "role": "validation",
            "name": "question-understanding-compiler:0.1",
            "digest": None,
        }
    ]:
        errors.append(
            "runtime_metadata.models must contain exactly the compiler validation model"
        )
    if len(intent_models) > 1:
        errors.append("runtime_metadata.models can contain at most one intent model")
    deterministic = contract["provenance"]["deterministic"]
    if deterministic != (not intent_models):
        errors.append(
            "question_intent_contract provenance.deterministic must be true iff "
            "no intent model was used"
        )
    intent_origin = contract["provenance"]["intent_origin"]
    if intent_origin == "supported_lane":
        if intent_models or not deterministic:
            errors.append(
                "supported_lane intent origin requires compiler-only deterministic runtime"
            )
        if not _supported_contract_semantics_equal(
            record["original_question"], contract["requested"]
        ):
            errors.append(
                "supported_lane intent origin requires exact supported question semantics"
            )
        if record["final_status"] == "failed":
            errors.append("a failed run cannot claim supported_lane intent origin")
    elif intent_origin == "supplied_draft":
        if intent_models or not deterministic:
            errors.append(
                "supplied_draft intent origin requires compiler-only deterministic runtime"
            )
        if record["final_status"] == "failed":
            errors.append("a failed run must use compiler_fallback intent origin")
    elif intent_origin == "structured_model":
        if len(intent_models) != 1 or deterministic:
            errors.append(
                "structured_model intent origin requires exactly one intent model"
            )
        if record["final_status"] == "failed":
            errors.append("a failed run must use compiler_fallback intent origin")
    elif intent_origin == "compiler_fallback":
        if record["final_status"] != "failed":
            errors.append("compiler_fallback intent origin is allowed only for failed runs")
    if not intent_models and runtime["backend"] != "local_sequential":
        errors.append("compiler-only runs require backend='local_sequential'")
    if runtime["backend"] == "api_bounded_parallel" and not intent_models:
        errors.append("api_bounded_parallel requires exactly one intent model")

    failed_stages = [
        stage
        for stage in QUESTION_UNDERSTANDING_STAGE_ORDER
        if record["stage_statuses"][stage] == "failed"
    ]
    if record["final_status"] == "failed":
        if len(record["errors"]) != 1:
            errors.append(
                "failed question_understanding_run requires exactly one error"
            )
        if len(failed_stages) != 1:
            errors.append("failed question_understanding_run requires one failed stage")
        if len(record["errors"]) == 1:
            error = record["errors"][0]
            allowed_stages = COMPILER_FAILURE_STAGES.get(error["code"], frozenset())
            if error["stage"] not in allowed_stages:
                errors.append(
                    f"compiler failure code {error['code']!r} cannot be reported at "
                    f"stage {error['stage']!r}"
                )
            expected_failed_stage = (
                "decompose" if error["stage"] == "runtime" else error["stage"]
            )
            if len(failed_stages) == 1 and failed_stages[0] != expected_failed_stage:
                errors.append(
                    "failed stage must exactly correspond to the registered error "
                    "stage policy"
                )
    elif failed_stages:
        errors.append("a non-failed question_understanding_run cannot have a failed stage")
    elif record["errors"]:
        errors.append("a non-failed question_understanding_run cannot contain errors")

    return errors


def validate_record(record: object) -> list[str]:
    if not isinstance(record, dict):
        return ["root must be an object"]
    non_finite = _non_finite_paths(record)
    if non_finite:
        return [f"non-finite number is forbidden at {path}" for path in non_finite]
    record_type = record.get("record_type")
    if record_type not in SCHEMA_PATHS:
        return [f"unknown record_type: {record_type!r}"]
    try:
        validator = _compiled_validator(record_type)
        errors = _schema_errors(record, validator)
        if errors:
            return errors
        if record_type == "question_intent_contract":
            errors.extend(_validate_question_intent_contract(record))
        elif record_type == "question_understanding_run":
            errors.extend(_validate_question_understanding_run(record))
        else:
            errors.extend(_validate_query_run(record))
        return errors
    except Exception as exc:
        return [f"validator error ({type(exc).__name__}): {exc}"]


def _records(path: Path) -> Iterable[tuple[int, object]]:
    suffix = path.suffix.casefold()
    if suffix == ".json":
        try:
            yield 1, _loads_strict_json(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise ValueError(f"invalid JSON: {exc}") from exc
        return
    if suffix != ".jsonl":
        raise ValueError("input suffix must be .json or .jsonl")
    try:
        with path.open(encoding="utf-8") as handle:
            record_number = 0
            for line_number, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                record_number += 1
                if record_number > MAX_RECORDS:
                    raise ValueError(f"record count exceeds {MAX_RECORDS}")
                try:
                    yield record_number, _loads_strict_json(line)
                except (json.JSONDecodeError, ValueError) as exc:
                    raise ValueError(f"line {line_number}: invalid JSON: {exc}") from exc
    except (OSError, UnicodeDecodeError) as exc:
        raise ValueError(f"cannot read JSONL: {exc}") from exc


def validate_path(path: Path | str) -> dict[str, Any]:
    input_path = Path(path)
    try:
        metadata = os.lstat(input_path)
    except OSError as exc:
        raise ValueError(f"cannot stat input: {exc}") from exc
    if stat.S_ISLNK(metadata.st_mode):
        raise ValueError("input must not be a symbolic link")
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError("input must be a regular file")
    if metadata.st_size > MAX_FILE_BYTES:
        raise ValueError(f"input exceeds {MAX_FILE_BYTES} bytes")

    errors: list[str] = []
    counts: dict[str, int] = {}
    records = 0
    for record_number, record in _records(input_path):
        records += 1
        if records > MAX_RECORDS:
            raise ValueError(f"record count exceeds {MAX_RECORDS}")
        record_type = record.get("record_type") if isinstance(record, dict) else None
        if isinstance(record_type, str):
            counts[record_type] = counts.get(record_type, 0) + 1
        errors.extend(
            f"record {record_number}: {error}" for error in validate_record(record)
        )
    if records == 0:
        raise ValueError("input contains no records")
    if errors:
        raise ValueError("validation failed:\n- " + "\n- ".join(errors))
    return {"records": records, "counts_by_type": dict(sorted(counts.items()))}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    args = parser.parse_args()
    try:
        result = validate_path(args.input)
    except ValueError as exc:
        for line in str(exc).splitlines():
            print(f"ERROR: {line}")
        return 1
    print(json.dumps({"status": "ok", **result}, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
