from __future__ import annotations

import copy
import hashlib
import json
import sys
import time
import unittest
from datetime import datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch


REPOSITORY = Path(__file__).resolve().parents[1]
SCRIPTS = REPOSITORY / "scripts"
sys.path.insert(0, str(SCRIPTS))

import build_question_understanding as engine
import question_language_registry as language_registry
import validate_query_graph_records as query_validator


STAMP = "2026-08-16T00:00:00+00:00"


def explicit_mention(
    question: str,
    surface: str,
    kind: str,
    occurrence: int = 0,
) -> dict[str, object]:
    start = -1
    cursor = 0
    for _ in range(occurrence + 1):
        start = question.find(surface, cursor)
        if start < 0:
            raise AssertionError(f"fixture surface is absent: {surface!r}")
        cursor = start + len(surface)
    return {
        "surface": surface,
        "start": start,
        "end": start + len(surface),
        "kind": kind,
    }


def question_input(question: str, question_id: str = "q_fixture") -> dict[str, object]:
    return {"question_id": question_id, "original_question": question}


def list_identifier_draft(
    question: str,
    *,
    location: str,
    container: str,
    filter_field: str,
    filter_value: str,
    identifier_field: str,
) -> dict[str, object]:
    mentions = [
        explicit_mention(question, location, "scope_location"),
        explicit_mention(question, container, "scope_container"),
        explicit_mention(question, filter_field, "filter_field"),
        explicit_mention(question, filter_value, "filter_value"),
        explicit_mention(question, "一致", "operator"),
        explicit_mention(question, identifier_field, "target_surface"),
        explicit_mention(question, identifier_field, "return_field"),
        explicit_mention(question, "すべて", "cardinality"),
    ]
    return {
        "requested": {
            "target": {
                "surface": identifier_field,
                "canonical_type": "task",
                "instance": None,
            },
            "scope": {
                "container": container,
                "location": location,
                "time_or_version": None,
                "filters": [
                    {"field": filter_field, "operator": "eq", "value": filter_value}
                ],
                "source": "explicit",
                "match_mode": "exact_normalized",
            },
            "operation_graph": {
                "external_inputs": [
                    {
                        "input_type": "record_set",
                        "source": "scope",
                        "description": "question-scoped records",
                    }
                ],
                "operations": [
                    {
                        "operator": "filter",
                        "input_refs": [{"kind": "external", "index": 0}],
                        "predicate": {
                            "field": filter_field,
                            "operator": "eq",
                            "value": filter_value,
                        },
                    },
                    {
                        "operator": "project",
                        "input_refs": [{"kind": "operation", "index": 0}],
                        "fields": [identifier_field],
                    },
                ],
            },
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
        },
        "not_requested": [],
        "ambiguities": [],
        "explicit_mentions": mentions,
    }


def compound_mean_nearest_draft(
    question: str,
    *,
    location: str,
    container: str,
    equality_field: str,
    equality_value: str,
    threshold_field: str,
    threshold: int,
    metric_field: str,
    identifier_field: str,
) -> dict[str, object]:
    threshold_surface = str(threshold)
    mentions = [
        explicit_mention(question, location, "scope_location"),
        explicit_mention(question, container, "scope_container"),
        explicit_mention(question, "データ", "target_surface"),
        explicit_mention(question, equality_field, "filter_field"),
        explicit_mention(question, equality_value, "filter_value"),
        explicit_mention(question, threshold_field, "filter_field"),
        explicit_mention(question, threshold_surface, "filter_value"),
        explicit_mention(question, "より大きい", "operator"),
        explicit_mention(question, metric_field, "return_field"),
        explicit_mention(question, identifier_field, "return_field"),
        explicit_mention(question, "すべて", "cardinality"),
    ]
    requested = {
        "target": {
            "surface": "データ",
            "canonical_type": "record",
            "instance": None,
        },
        "scope": {
            "container": container,
            "location": location,
            "time_or_version": None,
            "filters": [
                {"field": equality_field, "operator": "eq", "value": equality_value},
                {"field": threshold_field, "operator": "gt", "value": threshold},
            ],
            "source": "explicit",
            "match_mode": "exact_normalized",
        },
        "operation_graph": {
            "external_inputs": [
                {
                    "input_type": "record_set",
                    "source": "scope",
                    "description": "question-scoped records",
                }
            ],
            "operations": [
                {
                    "operator": "filter",
                    "input_refs": [{"kind": "external", "index": 0}],
                    "predicate": {
                        "field": equality_field,
                        "operator": "eq",
                        "value": equality_value,
                    },
                },
                {
                    "operator": "filter",
                    "input_refs": [{"kind": "operation", "index": 0}],
                    "predicate": {
                        "field": threshold_field,
                        "operator": "gt",
                        "value": threshold,
                    },
                },
                {
                    "operator": "project",
                    "input_refs": [{"kind": "operation", "index": 1}],
                    "fields": [metric_field],
                },
                {
                    "operator": "mean",
                    "input_refs": [{"kind": "operation", "index": 2}],
                    "calculation_precision": "exact_unrounded",
                },
                {
                    "operator": "argmin_all",
                    "input_refs": [
                        {"kind": "operation", "index": 1},
                        {"kind": "operation", "index": 3},
                    ],
                    "candidate_set_ref": {"kind": "operation", "index": 1},
                    "distance": "absolute",
                    "field": metric_field,
                    "tie_policy": "all",
                },
                {
                    "operator": "project",
                    "input_refs": [{"kind": "operation", "index": 4}],
                    "fields": [identifier_field],
                },
            ],
        },
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
    }
    return {
        "requested": requested,
        "not_requested": [],
        "ambiguities": [],
        "explicit_mentions": mentions,
    }


def generic_list_fixture(suffix: str = "a") -> tuple[dict[str, object], dict[str, object]]:
    location = f"組織ZX-{suffix}"
    container = f"plan_{suffix}.xlsx"
    filter_field = f"PhaseField{suffix}"
    filter_value = f"Stage-{suffix}"
    identifier_field = f"TaskID{suffix}"
    question = (
        f"{location}の{container}において、{filter_field}が{filter_value}に"
        f"一致する{identifier_field}をすべて挙げてください。"
    )
    return (
        question_input(question, f"q_list_{suffix}"),
        list_identifier_draft(
            question,
            location=location,
            container=container,
            filter_field=filter_field,
            filter_value=filter_value,
            identifier_field=identifier_field,
        ),
    )


def cardinality_variant_fixture(
    suffix: str,
    *,
    surface: str,
    mode: str,
    expected_count: int | None,
    container: str,
) -> tuple[dict[str, object], dict[str, object]]:
    question, draft = generic_list_fixture(suffix)
    mention = next(
        item
        for item in draft["explicit_mentions"]
        if item["surface"] == "すべて"
    )
    original = question["original_question"]
    question["original_question"] = (
        original[: mention["start"]] + surface + original[mention["end"] :]
    )
    mention["surface"] = surface
    mention["end"] = mention["start"] + len(surface)
    output = draft["requested"]["requested_outputs"][0]
    output["cardinality"] = {
        "mode": mode,
        "expected_count": expected_count,
    }
    output["answer_shape"]["container"] = container
    return question, draft


def generic_compound_fixture(
    suffix: str = "a",
) -> tuple[dict[str, object], dict[str, object]]:
    location = f"組織ZY-{suffix}"
    container = f"dataset_{suffix}.csv"
    equality_field = f"CategoryField{suffix}"
    equality_value = f"Segment-{suffix}"
    threshold_field = f"AmountField{suffix}"
    threshold = 31_415
    metric_field = f"MetricField{suffix}"
    identifier_field = f"RowID{suffix}"
    question = (
        f"{location}の{container}において、{equality_field}が{equality_value}であり、かつ"
        f"{threshold_field}が{threshold}より大きいデータを抽出し、{metric_field}の平均値を"
        f"計算してください。その平均値に最も近い{metric_field}の{identifier_field}を"
        "すべて答えてください。"
    )
    return (
        question_input(question, f"q_compound_{suffix}"),
        compound_mean_nearest_draft(
            question,
            location=location,
            container=container,
            equality_field=equality_field,
            equality_value=equality_value,
            threshold_field=threshold_field,
            threshold=threshold,
            metric_field=metric_field,
            identifier_field=identifier_field,
        ),
    )


def unknown_intent_draft() -> dict[str, object]:
    return {
        "requested": {
            "target": {
                "surface": None,
                "canonical_type": None,
                "instance": None,
            },
            "scope": {
                "container": None,
                "location": None,
                "time_or_version": None,
                "filters": [],
                "source": "unknown",
                "match_mode": "unknown",
            },
            "operation_graph": {
                "external_inputs": [
                    {
                        "input_type": "unknown",
                        "source": "question",
                        "description": "model-authored unsupported input",
                    }
                ],
                "operations": [
                    {
                        "operator": "unknown",
                        "input_refs": [{"kind": "external", "index": 0}],
                    }
                ],
            },
            "requested_outputs": [
                {
                    "source_operation_index": 0,
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
        },
        "not_requested": [],
        "ambiguities": [],
        "explicit_mentions": [],
    }


def alternative_scope_fixture(
    suffix: str = "scope",
) -> tuple[
    dict[str, object],
    dict[str, object],
    str,
    str,
]:
    left = f"組織North-{suffix}"
    right = f"組織South-{suffix}"
    container = f"scope_{suffix}.xlsx"
    filter_field = f"PhaseField{suffix}"
    filter_value = f"Stage-{suffix}"
    identifier_field = f"TaskID{suffix}"
    question = (
        f"{left}または{right}の{container}において、"
        f"{filter_field}が{filter_value}に一致する{identifier_field}を"
        "すべて挙げてください。"
    )
    draft = list_identifier_draft(
        question,
        location=left,
        container=container,
        filter_field=filter_field,
        filter_value=filter_value,
        identifier_field=identifier_field,
    )
    draft["explicit_mentions"].append(
        explicit_mention(question, right, "scope_location")
    )
    return question_input(question, f"q_alt_scope_{suffix}"), draft, left, right


def declare_scope_ambiguity(
    draft: dict[str, object],
    locations: tuple[str, str],
) -> dict[str, object]:
    declared = copy.deepcopy(draft)
    declared["requested"]["scope"]["location"] = None
    base = declared["requested"]
    candidates = []
    for location in locations:
        candidate = copy.deepcopy(base)
        candidate["scope"]["location"] = location
        candidates.append(
            {
                "candidate_requested": candidate,
                "confidence": "medium",
                "basis": f"model-authored basis for {location}",
            }
        )
    declared["ambiguities"] = [
        {
            "field": "scope",
            "field_path": "/requested/scope",
            "issue": "model-authored scope issue",
            "candidates": candidates,
            "impact": "high",
            "resolution": ["retrieve_parallel"],
        }
    ]
    return declared


def cartesian_ambiguity_fixture(
    suffix: str = "cartesian",
) -> tuple[dict[str, object], dict[str, object]]:
    left_scope = f"組織North-{suffix}"
    right_scope = f"組織South-{suffix}"
    left_target = f"TaskIDAlpha-{suffix}"
    right_target = f"TaskIDBeta-{suffix}"
    container = f"matrix_{suffix}.xlsx"
    filter_field = f"PhaseField{suffix}"
    filter_value = f"Stage-{suffix}"
    identifier_field = f"TaskID{suffix}"
    question = (
        f"{left_scope}または{right_scope}の{container}において、"
        f"{left_target}または{right_target}を対象とし、"
        f"{filter_field}が{filter_value}に一致する{identifier_field}を"
        "すべて挙げてください。"
    )
    draft = list_identifier_draft(
        question,
        location=left_scope,
        container=container,
        filter_field=filter_field,
        filter_value=filter_value,
        identifier_field=identifier_field,
    )
    draft["requested"]["scope"]["location"] = None
    draft["explicit_mentions"].extend(
        [
            explicit_mention(question, right_scope, "scope_location"),
            explicit_mention(question, left_target, "target_surface"),
            explicit_mention(question, right_target, "target_surface"),
        ]
    )
    base = draft["requested"]

    def candidates(component: str, values: tuple[str, str]) -> list[dict[str, object]]:
        result = []
        for value in values:
            candidate = copy.deepcopy(base)
            if component == "target":
                candidate["target"]["surface"] = value
                candidate["target"]["canonical_type"] = "task"
            else:
                candidate["scope"]["location"] = value
            result.append(
                {
                    "candidate_requested": candidate,
                    "confidence": "medium",
                    "basis": f"model-authored {component} basis",
                }
            )
        return result

    draft["ambiguities"] = [
        {
            "field": "target",
            "field_path": "/requested/target",
            "issue": "model-authored target issue",
            "candidates": candidates("target", (left_target, right_target)),
            "impact": "high",
            "resolution": ["retrieve_parallel"],
        },
        {
            "field": "scope",
            "field_path": "/requested/scope",
            "issue": "model-authored scope issue",
            "candidates": candidates("scope", (left_scope, right_scope)),
            "impact": "high",
            "resolution": ["resolve_from_evidence"],
        },
    ]
    return question_input(question, f"q_cartesian_{suffix}"), draft


def filter_value_alternative_fixture(
    suffix: str = "filter_or",
) -> tuple[dict[str, object], dict[str, object]]:
    location = f"組織FV-{suffix}"
    container = f"filter_{suffix}.csv"
    filter_field = f"PhaseField{suffix}"
    left_value = f"StageAlpha-{suffix}"
    right_value = f"StageBeta-{suffix}"
    identifier_field = f"TaskID{suffix}"
    question = (
        f"{location}の{container}において、{filter_field}が"
        f"{left_value}または{right_value}の{identifier_field}を"
        "すべて挙げてください。"
    )
    predicate = {
        "field": filter_field,
        "operator": "in",
        "value": [left_value, right_value],
    }
    return (
        question_input(question, f"q_filter_or_{suffix}"),
        {
            "requested": {
                "target": {
                    "surface": identifier_field,
                    "canonical_type": "task",
                    "instance": None,
                },
                "scope": {
                    "container": container,
                    "location": location,
                    "time_or_version": None,
                    "filters": [copy.deepcopy(predicate)],
                    "source": "explicit",
                    "match_mode": "exact_normalized",
                },
                "operation_graph": {
                    "external_inputs": [
                        {
                            "input_type": "record_set",
                            "source": "scope",
                            "description": "model-authored input",
                        }
                    ],
                    "operations": [
                        {
                            "operator": "filter",
                            "input_refs": [{"kind": "external", "index": 0}],
                            "predicate": copy.deepcopy(predicate),
                        },
                        {
                            "operator": "project",
                            "input_refs": [{"kind": "operation", "index": 0}],
                            "fields": [identifier_field],
                        },
                    ],
                },
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
            },
            "not_requested": [],
            "ambiguities": [],
            "explicit_mentions": [
                explicit_mention(question, location, "scope_location"),
                explicit_mention(question, container, "scope_container"),
                explicit_mention(question, filter_field, "filter_field"),
                explicit_mention(question, left_value, "filter_value"),
                explicit_mention(question, right_value, "filter_value"),
                explicit_mention(question, identifier_field, "target_surface"),
                explicit_mention(question, identifier_field, "return_field"),
                explicit_mention(question, "すべて", "cardinality"),
            ],
        },
    )


def operation_alternative_fixture(
    suffix: str = "operation_or",
) -> tuple[dict[str, object], dict[str, object]]:
    location = f"組織OP-{suffix}"
    container = f"operation_{suffix}.csv"
    metric_field = f"MetricField{suffix}"
    question = (
        f"{location}の{container}において、データの{metric_field}の"
        "合計値または平均値を計算し、その値を1つ答えてください。"
    )
    base_requested = {
        "target": {
            "surface": "データ",
            "canonical_type": "record",
            "instance": None,
        },
        "scope": {
            "container": container,
            "location": location,
            "time_or_version": None,
            "filters": [],
            "source": "explicit",
            "match_mode": "exact_normalized",
        },
        "operation_graph": {
            "external_inputs": [
                {
                    "input_type": "record_set",
                    "source": "scope",
                    "description": "model-authored input",
                }
            ],
            "operations": [
                {
                    "operator": "project",
                    "input_refs": [{"kind": "external", "index": 0}],
                    "fields": [metric_field],
                },
                {
                    "operator": "calculate",
                    "input_refs": [{"kind": "operation", "index": 0}],
                },
                {
                    "operator": "retrieve",
                    "input_refs": [{"kind": "operation", "index": 1}],
                },
            ],
        },
        "requested_outputs": [
            {
                "source_operation_index": 2,
                "return_field": "value",
                "cardinality": {"mode": "single", "expected_count": 1},
                "answer_shape": {
                    "container": "scalar",
                    "value_type": "number",
                    "unit": None,
                    "precision": "unspecified",
                },
                "display_precision": None,
            }
        ],
    }
    candidates = []
    for operator in ("sum", "mean"):
        candidate = copy.deepcopy(base_requested)
        candidate_operation = candidate["operation_graph"]["operations"][1]
        candidate_operation["operator"] = operator
        if operator == "mean":
            candidate_operation["calculation_precision"] = "exact_unrounded"
        candidates.append(
            {
                "candidate_requested": candidate,
                "confidence": "medium",
                "basis": f"model-authored {operator} basis",
            }
        )
    return (
        question_input(question, f"q_operation_or_{suffix}"),
        {
            "requested": base_requested,
            "not_requested": [],
            "ambiguities": [
                {
                    "field": "operation",
                    "field_path": "/requested/operation_graph",
                    "issue": "model-authored operation issue",
                    "candidates": candidates,
                    "impact": "high",
                    "resolution": ["retrieve_parallel"],
                }
            ],
            "explicit_mentions": [
                explicit_mention(question, location, "scope_location"),
                explicit_mention(question, container, "scope_container"),
                explicit_mention(question, "データ", "target_surface"),
                explicit_mention(question, metric_field, "return_field"),
                explicit_mention(question, "合計値", "operation"),
                explicit_mention(question, "平均", "operation"),
                explicit_mention(question, "計算", "operation"),
                explicit_mention(question, "答えて", "operation"),
                explicit_mention(question, "1つ", "cardinality"),
            ],
        },
    )


def output_alternative_fixture(
    suffix: str = "output_or",
) -> tuple[dict[str, object], dict[str, object]]:
    location = f"組織OUT-{suffix}"
    container = f"output_{suffix}.csv"
    filter_field = f"PhaseField{suffix}"
    filter_value = f"Stage-{suffix}"
    question = (
        f"{location}の{container}において、{filter_field}が{filter_value}に"
        "一致するTaskについて、identifierまたはnameをすべて"
        "挙げてください。"
    )
    predicate = {"field": filter_field, "operator": "eq", "value": filter_value}
    base_requested = {
        "target": {"surface": "Task", "canonical_type": "task", "instance": None},
        "scope": {
            "container": container,
            "location": location,
            "time_or_version": None,
            "filters": [copy.deepcopy(predicate)],
            "source": "explicit",
            "match_mode": "exact_normalized",
        },
        "operation_graph": {
            "external_inputs": [
                {
                    "input_type": "record_set",
                    "source": "scope",
                    "description": "model-authored input",
                }
            ],
            "operations": [
                {
                    "operator": "filter",
                    "input_refs": [{"kind": "external", "index": 0}],
                    "predicate": copy.deepcopy(predicate),
                },
                {
                    "operator": "project",
                    "input_refs": [{"kind": "operation", "index": 0}],
                    "fields": ["Task"],
                },
            ],
        },
        "requested_outputs": [
            {
                "source_operation_index": 1,
                "return_field": "unknown",
                "cardinality": {"mode": "all", "expected_count": None},
                "answer_shape": {
                    "container": "list",
                    "value_type": "unknown",
                    "unit": None,
                    "precision": "unspecified",
                },
                "display_precision": None,
            }
        ],
    }
    candidates = []
    for surface, return_field, value_type in (
        ("identifier", "identifier", "identifier"),
        ("name", "name", "string"),
    ):
        candidate = copy.deepcopy(base_requested)
        candidate["operation_graph"]["operations"][1]["fields"] = [surface]
        output = candidate["requested_outputs"][0]
        output["return_field"] = return_field
        output["answer_shape"]["value_type"] = value_type
        candidates.append(
            {
                "candidate_requested": candidate,
                "confidence": "medium",
                "basis": f"model-authored {return_field} basis",
            }
        )
    return (
        question_input(question, f"q_output_or_{suffix}"),
        {
            "requested": base_requested,
            "not_requested": [],
            "ambiguities": [
                {
                    "field": "return_field",
                    "field_path": "/requested/requested_outputs",
                    "issue": "model-authored output issue",
                    "candidates": candidates,
                    "impact": "high",
                    "resolution": ["retrieve_parallel"],
                }
            ],
            "explicit_mentions": [
                explicit_mention(question, location, "scope_location"),
                explicit_mention(question, container, "scope_container"),
                explicit_mention(question, filter_field, "filter_field"),
                explicit_mention(question, filter_value, "filter_value"),
                explicit_mention(question, "一致", "operator"),
                explicit_mention(question, "Task", "target_surface"),
                explicit_mention(question, "identifier", "return_field"),
                explicit_mention(question, "name", "return_field"),
                explicit_mention(question, "すべて", "cardinality"),
            ],
        },
    )


def compile_fixture(
    question: dict[str, object],
    draft: dict[str, object],
    **kwargs: object,
) -> dict[str, object]:
    return engine.compile_intent_draft(
        question,
        draft,
        generated_at=STAMP,
        started_at=STAMP,
        completed_at=STAMP,
        **kwargs,
    )


def operator_signature(run: dict[str, object]) -> list[str]:
    graph = run["question_intent_contract"]["requested"]["operation_graph"]
    return [node["operator"] for node in graph["nodes"]]


def replace_identifier_references(value: object, before: str, after: str) -> object:
    if isinstance(value, dict):
        return {
            key: replace_identifier_references(child, before, after)
            for key, child in value.items()
        }
    if isinstance(value, list):
        return [
            replace_identifier_references(child, before, after) for child in value
        ]
    return after if value == before else value


def changed_identifier(identifier: str) -> str:
    replacement = "0" if identifier[-1] != "0" else "1"
    return identifier[:-1] + replacement


def generated_ids(value: object, path: str = "root") -> dict[str, str]:
    result: dict[str, str] = {}
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{path}.{key}"
            if key.endswith("_id") and key not in {
                "question_id",
                "parent_question_id",
            }:
                result[child_path] = child
            result.update(generated_ids(child, child_path))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            result.update(generated_ids(child, f"{path}[{index}]"))
    return result


class SequenceClient:
    backend_mode = "local_sequential"

    def __init__(self, responses: list[object]) -> None:
        self.responses = list(responses)
        self.generate_calls = 0

    def check(self) -> dict[str, object]:
        return {
            "requested": "fixture-model",
            "resolved": "fixture-model",
            "digest": "a" * 32,
        }

    def generate_json(
        self,
        messages: list[dict[str, str]],
        schema: dict[str, object],
        timeout: float,
    ) -> object:
        self.generate_calls += 1
        if not self.responses:
            raise RuntimeError("fixture response sequence exhausted")
        return self.responses.pop(0)


class SlowFailureClient:
    backend_mode = "local_sequential"

    def __init__(self, fail_on: str) -> None:
        self.fail_on = fail_on

    def check(self) -> dict[str, object]:
        if self.fail_on == "check":
            time.sleep(0.025)
            raise TimeoutError("fixture backend timeout")
        return {
            "requested": "fixture-model",
            "resolved": "fixture-model",
            "digest": "c" * 32,
        }

    def generate_json(
        self,
        messages: list[dict[str, str]],
        schema: dict[str, object],
        timeout: float,
    ) -> object:
        time.sleep(0.025)
        raise TimeoutError("fixture generation timeout")


class SharedSemanticRegistryTest(unittest.TestCase):
    def test_registry_digest_and_consumers_match_the_shared_definition(self) -> None:
        payload = language_registry.registry_payload()
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        expected_digest = hashlib.sha256(encoded).hexdigest()

        self.assertEqual(language_registry.registry_digest(), expected_digest)
        self.assertEqual(
            language_registry.LANGUAGE_REGISTRY_SHA256,
            expected_digest,
        )
        self.assertEqual(
            language_registry.registry_metadata(),
            {
                "name": language_registry.REGISTRY_NAME,
                "version": language_registry.REGISTRY_VERSION,
                "sha256": expected_digest,
            },
        )
        self.assertEqual(
            query_validator.REGISTRY_VERSION,
            language_registry.REGISTRY_VERSION,
        )
        self.assertEqual(
            query_validator.LANGUAGE_REGISTRY_SHA256,
            expected_digest,
        )

        self.assertIs(
            engine.CANONICAL_TARGET_TYPE_LEXEMES,
            language_registry.CANONICAL_TARGET_TYPE_LEXEMES,
        )
        self.assertIs(
            engine.OPERATOR_MENTION_MAP,
            language_registry.OPERATOR_MENTION_MAP,
        )
        self.assertEqual(
            query_validator.CANONICAL_TARGET_TYPE_LEXEMES,
            language_registry.CANONICAL_TARGET_TYPE_LEXEMES,
        )
        self.assertEqual(
            query_validator.EXPLICIT_OPERATOR_SURFACES,
            language_registry.OPERATOR_MENTION_MAP,
        )
        self.assertEqual(
            query_validator.QUESTION_OPERATION_KEYWORDS,
            language_registry.OPERATION_KEYWORDS,
        )

    def test_registry_contains_no_representative_fixture_literals(self) -> None:
        leaves: set[str] = set()

        def visit(value: object) -> None:
            if isinstance(value, dict):
                for key, child in value.items():
                    leaves.add(str(key).casefold())
                    visit(child)
            elif isinstance(value, list):
                for child in value:
                    visit(child)
            elif isinstance(value, str):
                leaves.add(value.casefold())

        visit(language_registry.registry_payload()["definitions"])
        representative_literals = {
            "AYM",
            "PL",
            "探索的分析・仮説整理",
            "青葉バイオメディカル機器",
            "train.csv",
            "EducationField",
            "Marketing",
            "MonthlyIncome",
        }
        self.assertTrue(
            {item.casefold() for item in representative_literals}.isdisjoint(leaves)
        )

    def test_existing_three_supported_grammars_keep_their_graphs(self) -> None:
        standard_question, _ = generic_list_fixture("registry_standard")
        suffix_text = (
            "組織Registryのfuture_registry.xlsxにおいて、"
            "量子試験ステータスに一致するプロジェクトIDを"
            "すべて列挙してください。"
        )
        suffix_question = question_input(suffix_text, "q_registry_suffix")
        compound_question, _ = generic_compound_fixture("registry_compound")
        fixtures = (
            (standard_question, ["filter", "project"]),
            (suffix_question, ["filter", "project"]),
            (
                compound_question,
                [
                    "filter",
                    "filter",
                    "project",
                    "mean",
                    "argmin_all",
                    "project",
                ],
            ),
        )

        for question, expected_operators in fixtures:
            with self.subTest(question_id=question["question_id"]):
                draft = engine.derive_supported_intent_draft(question)
                self.assertIsNotNone(draft)
                self.assertEqual(engine.validate_intent_draft(draft), [])
                run = compile_fixture(question, draft)
                self.assertEqual(run["final_status"], "ready_for_retrieval")
                self.assertEqual(operator_signature(run), expected_operators)
                self.assertEqual(engine.validate_understanding_run(run), [])

    def test_registry_preserves_known_kinds_targets_operations_and_cardinality(
        self,
    ) -> None:
        self.assertEqual(
            {
                surface: language_registry.OPERATOR_MENTION_MAP[surface]
                for surface in ("一致", "一致しない", "より大きい", "以上")
            },
            {
                "一致": "eq",
                "一致しない": "ne",
                "より大きい": "gt",
                "以上": "gte",
            },
        )
        self.assertEqual(
            {
                operation: tuple(language_registry.OPERATION_KEYWORDS[operation])
                for operation in ("count", "mean", "argmin_all")
            },
            {
                "count": ("件数", "何件", "数え", "カウント", "count"),
                "mean": ("平均", "mean", "average"),
                "argmin_all": ("最も近", "最小", "nearest", "argmin"),
            },
        )
        self.assertIn("全て", language_registry.ALL_CARDINALITY_SURFACES)
        self.assertIn("複数", language_registry.MULTIPLE_CARDINALITY_SURFACES)
        self.assertIn("1件", language_registry.SINGLE_CARDINALITY_SURFACES)
        for left, right in (
            ("Age", "年齢"),
            ("Amount", "金額"),
            ("Distance", "距離"),
            ("Income", "収入"),
            ("Score", "スコア"),
        ):
            with self.subTest(metric_alias=(left, right)):
                self.assertIn(
                    (left, right),
                    language_registry.SUPPORTED_METRIC_DESCRIPTOR_ALIASES,
                )
                self.assertIn(
                    (right, left),
                    language_registry.SUPPORTED_METRIC_DESCRIPTOR_ALIASES,
                )

        for surface, canonical_type in (
            ("TaskIDRegistry", "task"),
            ("PrimaryRowID", "row"),
            ("従業員ID", "person"),
            ("データ", "record"),
        ):
            with self.subTest(target_surface=surface):
                self.assertEqual(engine._target_type_matches(surface), {canonical_type})
                self.assertEqual(
                    query_validator._target_type_matches(surface),
                    {canonical_type},
                )

        question, canonical_draft = generic_list_fixture("registry_kind")
        wrong_kinds = copy.deepcopy(canonical_draft)
        next(
            mention
            for mention in wrong_kinds["explicit_mentions"]
            if mention["surface"] == "一致"
        )["kind"] = "operation"
        next(
            mention
            for mention in wrong_kinds["explicit_mentions"]
            if mention["surface"] == "すべて"
        )["kind"] = "answer_shape"
        recovered = compile_fixture(question, wrong_kinds)
        source_refs = {
            source["span"]["text"]: source["source_ref"]
            for source in recovered["query_context_graph"]["sources"]
            if source["span"] is not None
            and source["span"]["text"] in {"一致", "すべて"}
        }
        self.assertEqual(recovered["final_status"], "ready_for_retrieval")
        self.assertIn(":kind:operator", source_refs["一致"])
        self.assertIn(":kind:cardinality", source_refs["すべて"])
        self.assertEqual(engine.validate_understanding_run(recovered), [])

    def test_registry_is_immutable_and_unknowns_fail_closed(self) -> None:
        with self.assertRaises(TypeError):
            language_registry.OPERATOR_MENTION_MAP["独自一致"] = "eq"
        with self.assertRaises(TypeError):
            language_registry.CANONICAL_TARGET_TYPE_LEXEMES["mystery"] = (
                "mysterycode",
            )
        with self.assertRaises(AttributeError):
            language_registry.ALL_CARDINALITY_SURFACES.add("無制限")

        payload = language_registry.registry_payload()
        payload["definitions"]["operator_mention_map"]["一致"] = "ne"
        tampered_digest = engine.sha256_json(payload)
        self.assertNotEqual(
            tampered_digest,
            language_registry.LANGUAGE_REGISTRY_SHA256,
        )
        self.assertEqual(language_registry.OPERATOR_MENTION_MAP["一致"], "eq")
        self.assertEqual(
            language_registry.registry_digest(),
            language_registry.LANGUAGE_REGISTRY_SHA256,
        )

        self.assertIsNone(language_registry.OPERATOR_MENTION_MAP.get("独自一致"))
        self.assertNotIn("無制限", language_registry.ALL_CARDINALITY_SURFACES)
        self.assertNotIn("独自演算", language_registry.OPERATION_KEYWORDS)
        self.assertEqual(engine._target_type_matches("MysteryCode"), set())
        self.assertEqual(query_validator._target_type_matches("MysteryCode"), set())

        unsupported = question_input(
            "組織Registryのunknown.csvにおいて、FieldXがValueXに一致する"
            "MysteryCodeをすべて挙げてください。",
            "q_registry_unknown_target",
        )
        self.assertIsNone(engine.derive_supported_intent_draft(unsupported))
        run = compile_fixture(unsupported, unknown_intent_draft())
        self.assertNotEqual(run["final_status"], "ready_for_retrieval")
        self.assertEqual(engine.validate_understanding_run(run), [])


class CompilerContractTest(unittest.TestCase):
    def test_compiler_only_valid_single_branch(self) -> None:
        question, draft = generic_list_fixture()
        run = compile_fixture(question, draft)

        self.assertEqual(run["final_status"], "ready_for_retrieval")
        self.assertEqual(run["branching"]["strategy"], "single")
        self.assertEqual(len(run["candidate_query_paths"]), 1)
        branch = run["candidate_query_paths"][0]
        self.assertEqual(branch["selected_candidates"], [])
        self.assertEqual(branch["intent_diffs"], [])
        self.assertEqual(branch["assumptions"], [])
        self.assertEqual(branch["status"], "pending")
        self.assertEqual(run["intent_gate"]["status"], "pass")
        self.assertEqual(run["intent_gate"]["action"], "retrieve")
        self.assertEqual(engine.validate_understanding_run(run), [])

    def test_full_cartesian_candidates_have_only_declared_diffs(self) -> None:
        question, draft = cartesian_ambiguity_fixture("cartesian")
        run = compile_fixture(question, draft, max_branches=4)

        self.assertEqual(run["branching"]["strategy"], "full_cartesian")
        self.assertEqual(len(run["candidate_query_paths"]), 4)
        selected_sets = set()
        diff_ids: list[str] = []
        assumption_ids: list[str] = []
        for branch in run["candidate_query_paths"]:
            self.assertEqual(len(branch["selected_candidates"]), 2)
            self.assertEqual(len(branch["intent_diffs"]), 2)
            self.assertEqual(
                {item["field_path"] for item in branch["intent_diffs"]},
                {"/requested/target", "/requested/scope"},
            )
            selected_sets.add(
                tuple(sorted(item["candidate_ref"] for item in branch["selected_candidates"]))
            )
            diff_ids.extend(item["intent_diff_id"] for item in branch["intent_diffs"])
            assumption_ids.extend(
                item["assumption_id"] for item in branch["assumptions"]
            )
        self.assertEqual(len(selected_sets), 4)
        self.assertEqual(len(diff_ids), len(set(diff_ids)))
        self.assertEqual(len(assumption_ids), len(set(assumption_ids)))
        self.assertEqual(engine.validate_understanding_run(run), [])

    def test_candidate_change_outside_declared_field_is_rejected(self) -> None:
        question, draft = generic_list_fixture("bad_diff")
        base = draft["requested"]
        candidates = []
        for suffix in ("one", "two"):
            candidate = copy.deepcopy(base)
            candidate["scope"]["location"] = f"Scope-{suffix}"
            candidate["requested_outputs"][0]["cardinality"] = {
                "mode": "multiple",
                "expected_count": None,
            }
            candidates.append(
                {
                    "candidate_requested": candidate,
                    "confidence": "medium",
                    "basis": suffix,
                }
            )
        draft["ambiguities"] = [
            {
                "field": "scope",
                "field_path": "/requested/scope",
                "issue": "scope only",
                "candidates": candidates,
                "impact": "high",
                "resolution": ["retrieve_parallel"],
            }
        ]

        with self.assertRaises(engine.CompilationError) as raised:
            compile_fixture(question, draft)
        self.assertEqual(raised.exception.code, "ambiguity_diff_outside_field")

    def test_branch_limit_is_fail_closed_without_truncation(self) -> None:
        question, draft = cartesian_ambiguity_fixture("limit")

        run = compile_fixture(question, draft, max_branches=3)
        self.assertEqual(run["final_status"], "abstained")
        self.assertEqual(run["candidate_query_paths"], [])
        self.assertEqual(run["intent_gate"]["action"], "abstain")
        self.assertIn("branch_limit_exceeded", run["intent_gate"]["reason_codes"])
        self.assertEqual(
            [item["reason_code"] for item in run["branching"]["excluded_combinations"]],
            ["branch_limit_exceeded"],
        )

    def test_unique_surface_recovers_schema_valid_mismatched_offsets(self) -> None:
        question, draft = generic_list_fixture("span_recovery")
        canonical = compile_fixture(question, draft)
        surface = next(
            mention["surface"]
            for mention in draft["explicit_mentions"]
            if mention["kind"] == "filter_value"
        )
        self.assertEqual(question["original_question"].count(surface), 1)

        shifted = copy.deepcopy(draft)
        mention = next(
            item
            for item in shifted["explicit_mentions"]
            if item["kind"] == "filter_value"
        )
        mention["start"] = 0
        mention["end"] = len(surface)
        self.assertNotEqual(
            question["original_question"][mention["start"] : mention["end"]],
            surface,
        )

        recovered = compile_fixture(question, shifted)
        expected_start = question["original_question"].find(surface)
        expected_span = {
            "start": expected_start,
            "end": expected_start + len(surface),
            "text": surface,
        }
        recovered_sources = [
            source
            for source in recovered["query_context_graph"]["sources"]
            if source["span"] == expected_span
        ]

        self.assertEqual(recovered["final_status"], "ready_for_retrieval")
        self.assertEqual(len(recovered_sources), 1)
        canonical_qic = canonical["question_intent_contract"]
        recovered_qic = recovered["question_intent_contract"]
        self.assertNotEqual(
            recovered_qic["provenance"]["intent_input_sha256"],
            canonical_qic["provenance"]["intent_input_sha256"],
        )
        self.assertNotEqual(
            recovered_qic["question_intent_contract_id"],
            canonical_qic["question_intent_contract_id"],
        )
        self.assertNotEqual(
            recovered["question_understanding_run_id"],
            canonical["question_understanding_run_id"],
        )
        self.assertEqual(
            generated_ids(recovered["query_context_graph"]),
            generated_ids(canonical["query_context_graph"]),
        )
        self.assertEqual(
            generated_ids(recovered_qic["requested"]),
            generated_ids(canonical_qic["requested"]),
        )
        self.assertEqual(engine.validate_understanding_run(recovered), [])

    def test_absent_repeated_or_out_of_range_surfaces_fail_closed(self) -> None:
        question, draft = generic_list_fixture("span_closed")
        surface = next(
            mention["surface"]
            for mention in draft["explicit_mentions"]
            if mention["kind"] == "filter_value"
        )

        absent = copy.deepcopy(draft)
        absent_mention = next(
            item
            for item in absent["explicit_mentions"]
            if item["kind"] == "filter_value"
        )
        absent_mention["surface"] = surface.swapcase()

        repeated_question = copy.deepcopy(question)
        repeated_question["original_question"] += f"注記:{surface}。"
        repeated = copy.deepcopy(draft)
        repeated_mention = next(
            item
            for item in repeated["explicit_mentions"]
            if item["kind"] == "filter_value"
        )
        repeated_mention["start"] = 0
        repeated_mention["end"] = len(surface)

        out_of_range = copy.deepcopy(draft)
        out_of_range_mention = next(
            item
            for item in out_of_range["explicit_mentions"]
            if item["kind"] == "filter_value"
        )
        out_of_range_mention["start"] = len(question["original_question"]) + 1
        out_of_range_mention["end"] = (
            out_of_range_mention["start"] + len(surface)
        )

        cases = (
            ("absent_exact_surface", question, absent, "explicit_span_mismatch"),
            (
                "repeated_ambiguous_surface",
                repeated_question,
                repeated,
                "explicit_span_mismatch",
            ),
            (
                "structurally_out_of_range",
                question,
                out_of_range,
                "invalid_explicit_span",
            ),
        )
        for label, question_case, draft_case, expected_code in cases:
            with self.subTest(case=label):
                failed = engine.build_question_understanding(
                    question_case,
                    draft=draft_case,
                    generated_at=STAMP,
                )
                self.assertEqual(failed["final_status"], "failed")
                self.assertEqual(failed["errors"][0]["code"], expected_code)
                self.assertEqual(engine.validate_understanding_run(failed), [])

    def test_repeated_surface_uses_an_exact_declared_occurrence(self) -> None:
        question, draft = generic_list_fixture("span_occurrence")
        mention = next(
            item
            for item in draft["explicit_mentions"]
            if item["kind"] == "filter_value"
        )
        surface = mention["surface"]
        expected_span = {
            "start": mention["start"],
            "end": mention["end"],
            "text": surface,
        }
        question["original_question"] += f"注記:{surface}。"
        self.assertEqual(question["original_question"].count(surface), 2)

        run = compile_fixture(question, draft)
        matching_sources = [
            source
            for source in run["query_context_graph"]["sources"]
            if source["span"] == expected_span
        ]

        self.assertEqual(run["final_status"], "clarification_required")
        self.assertIn(
            "question_equivalence_unproven",
            run["intent_gate"]["reason_codes"],
        )
        self.assertEqual(len(matching_sources), 1)
        self.assertEqual(engine.validate_understanding_run(run), [])

    def test_explicit_operator_and_cardinality_cannot_be_weakened(self) -> None:
        question, base_draft = generic_list_fixture("strict")

        wrong_operator = copy.deepcopy(base_draft)
        wrong_operator["requested"]["scope"]["filters"][0]["operator"] = "gte"
        wrong_operator["requested"]["operation_graph"]["operations"][0][
            "predicate"
        ]["operator"] = "gte"
        operator_run = compile_fixture(question, wrong_operator)
        self.assertEqual(operator_run["final_status"], "abstained")
        self.assertIn("explicit_conflict", operator_run["intent_gate"]["reason_codes"])

        wrong_cardinality = copy.deepcopy(base_draft)
        wrong_cardinality["requested"]["requested_outputs"][0]["cardinality"] = {
            "mode": "multiple",
            "expected_count": None,
        }
        cardinality_run = compile_fixture(question, wrong_cardinality)
        self.assertEqual(cardinality_run["final_status"], "abstained")
        self.assertIn(
            "forbidden_violation", cardinality_run["intent_gate"]["reason_codes"]
        )

    def test_deterministic_identity_excludes_timestamps(self) -> None:
        question, draft = generic_list_fixture("identity")
        first = compile_fixture(question, draft)
        later = engine.compile_intent_draft(
            question,
            draft,
            generated_at="2026-08-16T01:00:00+00:00",
            started_at="2026-08-16T01:00:00+00:00",
            completed_at="2026-08-16T01:00:00+00:00",
        )
        self.assertEqual(
            first["question_understanding_run_id"],
            later["question_understanding_run_id"],
        )
        self.assertEqual(
            first["question_intent_contract"]["question_intent_contract_id"],
            later["question_intent_contract"]["question_intent_contract_id"],
        )
        self.assertEqual(
            first["query_context_graph"]["graph_id"],
            later["query_context_graph"]["graph_id"],
        )
        self.assertEqual(
            [item["branch_id"] for item in first["candidate_query_paths"]],
            [item["branch_id"] for item in later["candidate_query_paths"]],
        )


class StrictSemanticRegressionTest(unittest.TestCase):
    def test_unsupported_greeting_cannot_become_ready(self) -> None:
        run = compile_fixture(
            question_input("こんにちは。", "q_unsupported_greeting"),
            unknown_intent_draft(),
        )

        self.assertNotEqual(run["final_status"], "ready_for_retrieval")
        self.assertEqual(run["final_status"], "clarification_required")
        self.assertEqual(run["intent_gate"]["status"], "indeterminate")
        self.assertEqual(run["intent_gate"]["action"], "clarify")

    def test_strong_tokens_canonicalize_wrong_model_kinds(self) -> None:
        operator_question, operator_draft = generic_list_fixture("mention_operator")
        cases = [
            (
                "operator",
                operator_question,
                operator_draft,
                "一致",
                "operation",
                "operator",
            )
        ]
        for label, surface, mode, expected_count, container in (
            ("all", "すべて", "all", None, "list"),
            ("multiple", "複数", "multiple", None, "list"),
            ("single", "1つ", "single", 1, "scalar"),
        ):
            question, draft = cardinality_variant_fixture(
                f"mention_{label}",
                surface=surface,
                mode=mode,
                expected_count=expected_count,
                container=container,
            )
            cases.append(
                (
                    label,
                    question,
                    draft,
                    surface,
                    "answer_shape",
                    "cardinality",
                )
            )

        for label, question, canonical_draft, surface, wrong_kind, expected_kind in cases:
            canonical = compile_fixture(question, canonical_draft)
            model_draft = copy.deepcopy(canonical_draft)
            mention = next(
                item
                for item in model_draft["explicit_mentions"]
                if item["surface"] == surface
            )
            mention["kind"] = wrong_kind
            client = SequenceClient([model_draft])

            with self.subTest(token_kind=label):
                recovered = engine.build_question_understanding(
                    question,
                    client=client,
                    retry_limit=1,
                    timeout=1,
                    generated_at=STAMP,
                )
                matching_sources = [
                    source
                    for source in recovered["query_context_graph"]["sources"]
                    if source["span"] is not None
                    and source["span"]["text"] == surface
                ]
                self.assertEqual(client.generate_calls, 1)
                expected_status = (
                    "ready_for_retrieval"
                    if label in {"operator", "all"}
                    else "clarification_required"
                )
                self.assertEqual(recovered["final_status"], expected_status)
                if expected_status == "clarification_required":
                    self.assertIn(
                        "question_equivalence_unproven",
                        recovered["intent_gate"]["reason_codes"],
                    )
                self.assertEqual(engine.validate_understanding_run(recovered), [])
                recovered_qic = recovered["question_intent_contract"]
                canonical_qic = canonical["question_intent_contract"]
                self.assertNotEqual(
                    recovered_qic["provenance"]["intent_input_sha256"],
                    canonical_qic["provenance"]["intent_input_sha256"],
                )
                self.assertNotEqual(
                    recovered_qic["question_intent_contract_id"],
                    canonical_qic["question_intent_contract_id"],
                )
                self.assertEqual(
                    generated_ids(recovered["query_context_graph"]),
                    generated_ids(canonical["query_context_graph"]),
                )
                self.assertEqual(
                    generated_ids(recovered_qic["requested"]),
                    generated_ids(canonical_qic["requested"]),
                )
                self.assertEqual(len(matching_sources), 1)
                source = matching_sources[0]
                self.assertIn(f":kind:{expected_kind}", source["source_ref"])
                edge = next(
                    item
                    for item in recovered["query_context_graph"]["edges"]
                    if item["source_ref"] == source["source_id"]
                )
                node = next(
                    item
                    for item in recovered["query_context_graph"]["nodes"]
                    if item["node_id"] == edge["from_ref"]
                )
                self.assertEqual(
                    node["node_type"],
                    "operation" if expected_kind == "operator" else "requested_output",
                )

    def test_strong_kind_canonicalization_does_not_weaken_requested_meaning(self) -> None:
        operator_question, operator_draft = generic_list_fixture("weaken_operator")
        next(
            item
            for item in operator_draft["explicit_mentions"]
            if item["surface"] == "一致"
        )["kind"] = "operation"
        operator_draft["requested"]["scope"]["filters"][0]["operator"] = "gte"
        operator_draft["requested"]["operation_graph"]["operations"][0][
            "predicate"
        ]["operator"] = "gte"

        cardinality_question, cardinality_draft = generic_list_fixture(
            "weaken_cardinality"
        )
        next(
            item
            for item in cardinality_draft["explicit_mentions"]
            if item["surface"] == "すべて"
        )["kind"] = "answer_shape"
        output = cardinality_draft["requested"]["requested_outputs"][0]
        output["cardinality"] = {"mode": "single", "expected_count": 1}
        output["answer_shape"]["container"] = "scalar"

        for label, question, draft in (
            ("operator", operator_question, operator_draft),
            ("cardinality", cardinality_question, cardinality_draft),
        ):
            client = SequenceClient([draft])
            with self.subTest(semantic_kind=label):
                run = engine.build_question_understanding(
                    question,
                    client=client,
                    retry_limit=1,
                    timeout=1,
                    generated_at=STAMP,
                )
                self.assertEqual(client.generate_calls, 1)
                self.assertNotEqual(run["final_status"], "ready_for_retrieval")
                self.assertEqual(run["final_status"], "abstained")
                self.assertIn("explicit_conflict", run["intent_gate"]["reason_codes"])
                self.assertEqual(engine.validate_understanding_run(run), [])

    def test_forged_final_qcg_cannot_relabel_strong_tokens(self) -> None:
        operator_question, operator_draft = generic_list_fixture("forged_operator")
        multiple_question, multiple_draft = cardinality_variant_fixture(
            "forged_multiple",
            surface="複数",
            mode="multiple",
            expected_count=None,
            container="list",
        )
        single_question, single_draft = cardinality_variant_fixture(
            "forged_single",
            surface="1件",
            mode="single",
            expected_count=1,
            container="scalar",
        )

        def relabel_with_coherent_ids(
            run: dict[str, object], surface: str, wrong_kind: str
        ) -> dict[str, object]:
            tampered = copy.deepcopy(run)
            graph = tampered["query_context_graph"]
            source = next(
                item
                for item in graph["sources"]
                if item["span"] is not None and item["span"]["text"] == surface
            )
            edge = next(
                item
                for item in graph["edges"]
                if item["source_ref"] == source["source_id"]
            )
            mention_node = next(
                item
                for item in graph["nodes"]
                if item["node_id"] == edge["from_ref"]
            )
            slot_node = next(
                item
                for item in graph["nodes"]
                if item["node_id"] == edge["to_ref"]
            )

            source["source_ref"] = (
                source["source_ref"].rsplit(":kind:", 1)[0]
                + f":kind:{wrong_kind}"
            )
            source_core = {
                key: value for key, value in source.items() if key != "source_id"
            }
            source["source_id"] = engine._identifier("source", source_core)

            mention_node["node_type"] = engine.NODE_TYPES_BY_MENTION[wrong_kind]
            mention_core = {
                key: value
                for key, value in mention_node.items()
                if key != "node_id"
            }
            mention_node["node_id"] = engine._identifier("node", mention_core)

            slot_node["node_type"] = engine.NODE_TYPES_BY_MENTION[wrong_kind]
            slot_node["canonical_value"] = wrong_kind
            slot_core = {
                key: value for key, value in slot_node.items() if key != "node_id"
            }
            slot_node["node_id"] = engine._identifier("node", slot_core)

            edge["from_ref"] = mention_node["node_id"]
            edge["to_ref"] = slot_node["node_id"]
            edge["source_ref"] = source["source_id"]
            edge_core = {
                key: value for key, value in edge.items() if key != "edge_id"
            }
            edge["edge_id"] = engine._identifier("edge", edge_core)

            graph["sources"].sort(key=lambda item: item["source_id"])
            graph["nodes"].sort(key=lambda item: item["node_id"])
            graph["edges"].sort(key=lambda item: item["edge_id"])
            graph_core = {
                key: value for key, value in graph.items() if key != "graph_id"
            }
            graph["graph_id"] = engine._identifier("qcg", graph_core)

            identity = {
                "question_intent_contract_id": tampered[
                    "question_intent_contract"
                ]["question_intent_contract_id"],
                "query_context_graph_id": graph["graph_id"],
                "branching": tampered["branching"],
                "candidate_query_paths": tampered["candidate_query_paths"],
                "intent_gate": tampered["intent_gate"],
            }
            tampered["question_understanding_run_id"] = engine._identifier(
                "qur", identity, 32
            )
            return tampered

        cases = (
            (
                "operator",
                operator_question,
                operator_draft,
                "一致",
                "operation",
                "labels an explicit comparison token with the wrong kind",
            ),
            (
                "multiple",
                multiple_question,
                multiple_draft,
                "複数",
                "answer_shape",
                "labels an explicit cardinality token with the wrong kind",
            ),
            (
                "single",
                single_question,
                single_draft,
                "1件",
                "answer_shape",
                "labels an explicit cardinality token with the wrong kind",
            ),
        )
        for label, question, draft, surface, wrong_kind, expected_error in cases:
            with self.subTest(token_kind=label):
                valid = compile_fixture(question, draft)
                tampered = relabel_with_coherent_ids(valid, surface, wrong_kind)
                errors = engine.validate_understanding_run(tampered)
                self.assertTrue(
                    any(expected_error in error for error in errors),
                    errors,
                )
                self.assertFalse(
                    any("deterministic content ID" in error for error in errors),
                    errors,
                )

    def test_unknown_semantic_role_swaps_are_not_canonicalized(self) -> None:
        swaps = (
            ("filter_field", "scope_location"),
            ("filter_value", "filter_field"),
            ("scope_location", "filter_value"),
        )
        for source_kind, wrong_kind in swaps:
            question, draft = generic_list_fixture(f"unknown_{source_kind}")
            next(
                item
                for item in draft["explicit_mentions"]
                if item["kind"] == source_kind
            )["kind"] = wrong_kind
            with self.subTest(source_kind=source_kind, wrong_kind=wrong_kind):
                failed = engine.build_question_understanding(
                    question,
                    draft=draft,
                    generated_at=STAMP,
                )
                self.assertEqual(failed["final_status"], "failed")
                self.assertEqual(
                    failed["errors"][0]["code"], "unbound_intent_literal"
                )
                self.assertEqual(engine.validate_understanding_run(failed), [])

    def test_duplicate_after_strong_kind_canonicalization_is_rejected(self) -> None:
        question, draft = generic_list_fixture("canonical_duplicate")
        operator_mention = next(
            item
            for item in draft["explicit_mentions"]
            if item["surface"] == "一致"
        )
        draft["explicit_mentions"].append(
            {**operator_mention, "kind": "operation"}
        )

        failed = engine.build_question_understanding(
            question,
            draft=draft,
            generated_at=STAMP,
        )

        self.assertEqual(failed["final_status"], "failed")
        self.assertEqual(failed["errors"][0]["code"], "duplicate_explicit_mention")
        self.assertEqual(engine.validate_understanding_run(failed), [])

    def test_one_question_span_cannot_fill_incompatible_roles(self) -> None:
        question, draft = generic_list_fixture("role_conflict")
        return_mention = next(
            item
            for item in draft["explicit_mentions"]
            if item["kind"] == "return_field"
        )
        draft["explicit_mentions"].append(
            {
                **return_mention,
                "start": 0,
                "end": len(return_mention["surface"]),
                "kind": "filter_value",
            }
        )

        with self.assertRaises(engine.CompilationError) as raised:
            compile_fixture(question, draft)
        self.assertEqual(
            (raised.exception.code, raised.exception.stage),
            ("explicit_span_role_conflict", "context"),
        )

    def test_repeated_predicates_require_repeated_source_expressions(self) -> None:
        question, draft = generic_list_fixture("predicate_repeat")
        requested = draft["requested"]
        predicate = copy.deepcopy(requested["scope"]["filters"][0])
        requested["scope"]["filters"].append(predicate)
        first_filter, project = requested["operation_graph"]["operations"]
        repeated_filter = copy.deepcopy(first_filter)
        repeated_filter["input_refs"] = [{"kind": "operation", "index": 0}]
        project["input_refs"] = [{"kind": "operation", "index": 1}]
        requested["operation_graph"]["operations"] = [
            first_filter,
            repeated_filter,
            project,
        ]
        requested["requested_outputs"][0]["source_operation_index"] = 2

        run = compile_fixture(question, draft)

        self.assertEqual(run["final_status"], "abstained")
        result = next(
            item
            for item in run["forbidden_check_results"]
            if item["validator_id"] == "operator_preserved"
        )
        self.assertEqual(result["status"], "violation")

    def test_repeated_outputs_require_repeated_source_expressions(self) -> None:
        question, draft = generic_list_fixture("output_repeat")
        draft["requested"]["requested_outputs"].append(
            copy.deepcopy(draft["requested"]["requested_outputs"][0])
        )

        run = compile_fixture(question, draft)

        self.assertEqual(run["final_status"], "abstained")
        result = next(
            item
            for item in run["forbidden_check_results"]
            if item["validator_id"] == "output_contract_match"
        )
        self.assertEqual(result["status"], "violation")

    def test_equal_priority_scope_alternatives_require_declared_branches(self) -> None:
        question, undeclared, left, right = alternative_scope_fixture("priority")
        unbranched = compile_fixture(question, undeclared)
        self.assertEqual(unbranched["final_status"], "abstained")
        self.assertEqual(unbranched["intent_gate"]["action"], "abstain")
        self.assertEqual(
            set(unbranched["intent_gate"]["reason_codes"]),
            {"explicit_conflict", "forbidden_violation"},
        )
        self.assertEqual(engine.validate_understanding_run(unbranched), [])

        declared = declare_scope_ambiguity(undeclared, (left, right))
        branched = compile_fixture(question, declared)
        self.assertEqual(branched["final_status"], "clarification_required")
        self.assertEqual(
            (branched["intent_gate"]["status"], branched["intent_gate"]["action"]),
            ("indeterminate", "clarify"),
        )
        self.assertIn(
            "question_equivalence_unproven",
            branched["intent_gate"]["reason_codes"],
        )
        self.assertEqual(branched["branching"]["strategy"], "full_cartesian")
        self.assertEqual(len(branched["candidate_query_paths"]), 2)
        self.assertEqual(engine.validate_understanding_run(branched), [])

    def test_question_absent_candidate_values_are_rejected(self) -> None:
        question, draft = generic_list_fixture("absent_candidate")
        base = draft["requested"]
        candidates = []
        for location in ("Invented-North", "Invented-South"):
            candidate = copy.deepcopy(base)
            candidate["scope"]["location"] = location
            candidates.append(
                {
                    "candidate_requested": candidate,
                    "confidence": "medium",
                    "basis": "model-authored unsupported candidate",
                }
            )
        draft["ambiguities"] = [
            {
                "field": "scope",
                "field_path": "/requested/scope",
                "issue": "model-authored unsupported scope",
                "candidates": candidates,
                "impact": "high",
                "resolution": ["retrieve_parallel"],
            }
        ]

        with self.assertRaises(engine.CompilationError) as raised:
            compile_fixture(question, draft)
        self.assertEqual(
            (raised.exception.code, raised.exception.stage),
            ("unbound_candidate_literal", "candidate_paths"),
        )

    def test_canonical_target_type_cannot_be_injected(self) -> None:
        question, draft = generic_list_fixture("ontology")
        draft["requested"]["target"]["canonical_type"] = "person"

        with self.assertRaises(engine.CompilationError) as raised:
            compile_fixture(question, draft)

        self.assertEqual(
            (raised.exception.code, raised.exception.stage),
            ("invalid_intent_draft", "decompose"),
        )

    def test_explicit_scope_and_file_omissions_cannot_be_ready(self) -> None:
        question, base_draft = generic_list_fixture("scope_omission")
        omissions = (
            ("location", "scope_location"),
            ("container", "scope_container"),
        )
        for scope_field, mention_kind in omissions:
            draft = copy.deepcopy(base_draft)
            draft["requested"]["scope"][scope_field] = None
            draft["explicit_mentions"] = [
                mention
                for mention in draft["explicit_mentions"]
                if mention["kind"] != mention_kind
            ]
            with self.subTest(scope_field=scope_field):
                run = compile_fixture(question, draft)
                self.assertEqual(run["final_status"], "abstained")
                self.assertIn("explicit_conflict", run["intent_gate"]["reason_codes"])
                self.assertEqual(engine.validate_understanding_run(run), [])

    def test_scope_and_operation_alternative_omissions_cannot_be_ready(self) -> None:
        scope_question, scope_draft, _left, right = alternative_scope_fixture(
            "raw_omission"
        )
        scope_draft["explicit_mentions"] = [
            mention
            for mention in scope_draft["explicit_mentions"]
            if mention["surface"] != right
        ]

        operation_question, declared_operation = operation_alternative_fixture(
            "raw_omission"
        )
        mean_candidate = next(
            candidate["candidate_requested"]
            for candidate in declared_operation["ambiguities"][0]["candidates"]
            if candidate["candidate_requested"]["operation_graph"]["operations"][1][
                "operator"
            ]
            == "mean"
        )
        operation_draft = {
            "requested": copy.deepcopy(mean_candidate),
            "not_requested": [],
            "ambiguities": [],
            "explicit_mentions": copy.deepcopy(
                declared_operation["explicit_mentions"]
            ),
        }

        for label, question, draft in (
            ("scope", scope_question, scope_draft),
            ("operation", operation_question, operation_draft),
        ):
            with self.subTest(alternative_kind=label):
                run = compile_fixture(question, draft)
                self.assertEqual(run["final_status"], "abstained")
                self.assertEqual(run["intent_gate"]["action"], "abstain")
                self.assertEqual(engine.validate_understanding_run(run), [])

    def test_filter_field_and_value_roles_cannot_be_swapped(self) -> None:
        question, draft = generic_list_fixture("direction")
        scope_predicate = draft["requested"]["scope"]["filters"][0]
        graph_predicate = draft["requested"]["operation_graph"]["operations"][0][
            "predicate"
        ]
        for predicate in (scope_predicate, graph_predicate):
            predicate["field"], predicate["value"] = (
                predicate["value"],
                predicate["field"],
            )
        for mention in draft["explicit_mentions"]:
            if mention["kind"] == "filter_field":
                mention["kind"] = "filter_value"
            elif mention["kind"] == "filter_value":
                mention["kind"] = "filter_field"

        run = compile_fixture(question, draft)

        self.assertEqual(run["final_status"], "abstained")
        self.assertIn("explicit_conflict", run["intent_gate"]["reason_codes"])
        self.assertEqual(engine.validate_understanding_run(run), [])

    def test_literal_equality_has_deterministic_match_mode(self) -> None:
        question, draft = generic_list_fixture("matchmode")
        ready = compile_fixture(question, draft)
        self.assertEqual(ready["final_status"], "ready_for_retrieval")
        self.assertEqual(
            ready["question_intent_contract"]["requested"]["scope"]["match_mode"],
            "exact_normalized",
        )

        exact = copy.deepcopy(draft)
        exact["requested"]["scope"]["match_mode"] = "exact"
        rejected = compile_fixture(question, exact)
        self.assertEqual(rejected["final_status"], "abstained")
        self.assertEqual(engine.validate_understanding_run(rejected), [])

    def test_filter_value_or_compiles_to_one_deterministic_in_path(self) -> None:
        question, draft = filter_value_alternative_fixture("single_path")
        run = compile_fixture(question, draft)

        self.assertEqual(run["final_status"], "clarification_required")
        self.assertIn(
            "question_equivalence_unproven",
            run["intent_gate"]["reason_codes"],
        )
        self.assertEqual(run["branching"]["strategy"], "single")
        self.assertEqual(len(run["candidate_query_paths"]), 1)
        self.assertEqual(run["question_intent_contract"]["ambiguity"], [])
        requested = run["question_intent_contract"]["requested"]
        self.assertEqual(requested["scope"]["filters"][0]["operator"], "in")
        filter_node = next(
            node
            for node in requested["operation_graph"]["nodes"]
            if node["operator"] == "filter"
        )
        self.assertEqual(filter_node["predicate"]["operator"], "in")
        self.assertEqual(engine.validate_understanding_run(run), [])

    def test_scope_operation_and_output_or_have_full_branches(self) -> None:
        scope_question, scope_draft, left, right = alternative_scope_fixture(
            "complete_scope"
        )
        fixtures = (
            (
                "scope",
                scope_question,
                declare_scope_ambiguity(scope_draft, (left, right)),
                lambda path: path["candidate_intent"]["scope"]["location"],
                {left, right},
            ),
            (
                "operation",
                *operation_alternative_fixture("complete_operation"),
                lambda path: next(
                    node["operator"]
                    for node in path["candidate_intent"]["operation_graph"]["nodes"]
                    if node["operator"] in {"sum", "mean"}
                ),
                {"sum", "mean"},
            ),
            (
                "output",
                *output_alternative_fixture("complete_output"),
                lambda path: path["candidate_intent"]["requested_outputs"][0][
                    "return_field"
                ],
                {"identifier", "name"},
            ),
        )
        for label, question, draft, value_of, expected_values in fixtures:
            with self.subTest(alternative_kind=label):
                run = compile_fixture(question, draft)
                self.assertEqual(run["final_status"], "clarification_required")
                self.assertEqual(
                    (run["intent_gate"]["status"], run["intent_gate"]["action"]),
                    ("indeterminate", "clarify"),
                )
                self.assertIn(
                    "question_equivalence_unproven",
                    run["intent_gate"]["reason_codes"],
                )
                self.assertEqual(run["branching"]["strategy"], "full_cartesian")
                self.assertEqual(len(run["candidate_query_paths"]), 2)
                self.assertEqual(
                    {value_of(path) for path in run["candidate_query_paths"]},
                    expected_values,
                )
                self.assertTrue(
                    all(path["intent_diffs"] for path in run["candidate_query_paths"])
                )
                diff_ids = [
                    diff["intent_diff_id"]
                    for path in run["candidate_query_paths"]
                    for diff in path["intent_diffs"]
                ]
                self.assertEqual(len(diff_ids), len(set(diff_ids)))
                self.assertEqual(
                    len(
                        {
                            path["branch_id"]
                            for path in run["candidate_query_paths"]
                        }
                    ),
                    2,
                )
                self.assertEqual(engine.validate_understanding_run(run), [])

    def test_ambiguity_cannot_hide_uncompiled_additional_requests(self) -> None:
        base_question, base_draft, left, right = alternative_scope_fixture(
            "additional_request"
        )
        declared = declare_scope_ambiguity(base_draft, (left, right))
        additions = {
            "priority": "さらに重要度も答えてください。",
            "department": "さらに担当部署も含めてください。",
            "audit_class": "さらに監査区分も返してください。",
        }

        for label, addition in additions.items():
            with self.subTest(additional_request=label):
                question = copy.deepcopy(base_question)
                question["question_id"] = f"q_ambiguity_additional_{label}"
                question["original_question"] += addition

                first = compile_fixture(question, copy.deepcopy(declared))
                later = engine.compile_intent_draft(
                    question,
                    copy.deepcopy(declared),
                    generated_at="2026-08-16T03:00:00+00:00",
                    started_at="2026-08-16T03:00:00+00:00",
                    completed_at="2026-08-16T03:00:00+00:00",
                )

                self.assertEqual(first["final_status"], "clarification_required")
                self.assertEqual(
                    (first["intent_gate"]["status"], first["intent_gate"]["action"]),
                    ("indeterminate", "clarify"),
                )
                self.assertIn(
                    "question_equivalence_unproven",
                    first["intent_gate"]["reason_codes"],
                )
                self.assertEqual(first["branching"]["strategy"], "full_cartesian")
                self.assertEqual(len(first["candidate_query_paths"]), 2)
                for path in first["candidate_query_paths"]:
                    self.assertEqual(len(path["selected_candidates"]), 1)
                    self.assertEqual(len(path["intent_diffs"]), 1)
                    self.assertEqual(
                        path["intent_diffs"][0]["field_path"],
                        "/requested/scope",
                    )
                    self.assertEqual(len(path["assumptions"]), 1)
                self.assertEqual(generated_ids(first), generated_ids(later))
                self.assertEqual(engine.validate_understanding_run(first), [])
                self.assertEqual(engine.validate_understanding_run(later), [])

    def test_ready_status_cannot_use_constrained_branching(self) -> None:
        question, draft, left, right = alternative_scope_fixture("constrained")
        run = compile_fixture(
            question,
            declare_scope_ambiguity(draft, (left, right)),
        )
        self.assertEqual(run["final_status"], "clarification_required")
        tampered = copy.deepcopy(run)
        tampered["final_status"] = "ready_for_retrieval"
        tampered["intent_gate"]["status"] = "pass"
        tampered["intent_gate"]["action"] = "retrieve"
        tampered["intent_gate"]["reason_codes"] = []
        for branch_result in tampered["intent_gate"]["branch_results"]:
            branch_result["status"] = "pass"
            branch_result["reason_codes"] = []
            explicit_check = next(
                check
                for check in branch_result["checks"]
                if check["check_id"] == "explicit_consistency"
            )
            explicit_check["status"] = "pass"
            explicit_check["detail"] = (
                "Explicit spans, operators, scope, and output constraints are consistent."
            )
        tampered["branching"]["strategy"] = "constrained"

        errors = engine.validate_understanding_run(tampered)

        self.assertTrue(
            any("ready_for_retrieval cannot use constrained branching" in error for error in errors),
            errors,
        )

    def test_gate_statuses_must_match_deterministic_recomputation(self) -> None:
        question, draft = generic_list_fixture("gate_recompute")
        tampered = compile_fixture(question, draft)
        tampered["final_status"] = "abstained"
        tampered["intent_gate"]["status"] = "fail"
        tampered["intent_gate"]["action"] = "abstain"
        tampered["intent_gate"]["reason_codes"] = ["explicit_conflict"]
        branch_result = tampered["intent_gate"]["branch_results"][0]
        branch_result["status"] = "fail"
        branch_result["reason_codes"] = ["explicit_conflict"]
        explicit_check = next(
            item
            for item in branch_result["checks"]
            if item["check_id"] == "explicit_consistency"
        )
        explicit_check["status"] = "fail"

        errors = engine.validate_understanding_run(tampered)

        self.assertTrue(
            any(
                "explicit_consistency" in error
                and "recomputed" in error
                for error in errors
            ),
            errors,
        )

    def test_forbidden_statuses_must_match_deterministic_recomputation(self) -> None:
        question, draft = generic_list_fixture("forbidden_recompute")
        tampered = compile_fixture(question, draft)
        result = tampered["forbidden_check_results"][0]
        result["status"] = "violation"
        result["action_taken"] = "abstain"

        errors = engine.validate_understanding_run(tampered)

        self.assertTrue(
            any(
                "status must be 'pass'" in error
                and "recomputed" in error
                for error in errors
            ),
            errors,
        )

    def test_failure_stage_is_reported_exactly(self) -> None:
        question, draft = generic_list_fixture("failure_stage")
        location_mention = next(
            item
            for item in draft["explicit_mentions"]
            if item["kind"] == "scope_location"
        )
        location_mention["kind"] = "filter_value"

        failed = engine.build_question_understanding(
            question,
            draft=draft,
            generated_at=STAMP,
        )

        self.assertEqual(failed["final_status"], "failed")
        self.assertEqual(
            failed["stage_statuses"],
            {
                "decompose": "completed",
                "context": "failed",
                "candidate_paths": "skipped",
                "intent_gate": "skipped",
                "validation": "skipped",
            },
        )
        self.assertEqual(failed["errors"][0]["stage"], "context")
        self.assertEqual(failed["errors"][0]["code"], "unbound_intent_literal")
        self.assertEqual(engine.validate_understanding_run(failed), [])

        wrong_error_stage = copy.deepcopy(failed)
        wrong_error_stage["errors"][0]["stage"] = "decompose"
        errors = engine.validate_understanding_run(wrong_error_stage)
        self.assertTrue(
            any(
                "cannot be reported at stage 'decompose'" in error
                for error in errors
            ),
            errors,
        )

    def test_compiler_provenance_runtime_and_timestamps_cannot_be_forged(self) -> None:
        question, draft = generic_list_fixture("runtime_tamper")
        run = compile_fixture(question, draft)
        tampered_cases: list[tuple[str, dict[str, object], str]] = []

        runner = copy.deepcopy(run)
        runner["provenance"]["runner"] = "forged-runner"
        tampered_cases.append(("runner", runner, "embedded analyzer and runner"))

        deterministic = copy.deepcopy(run)
        deterministic["question_intent_contract"]["provenance"][
            "deterministic"
        ] = False
        tampered_cases.append(
            ("deterministic", deterministic, "deterministic must be true iff")
        )

        backend = copy.deepcopy(run)
        backend["runtime_metadata"]["backend"] = "api_bounded_parallel"
        tampered_cases.append(
            ("backend", backend, "compiler-only runs require backend")
        )

        model = copy.deepcopy(run)
        model["runtime_metadata"]["models"].append(
            {"role": "intent", "name": "forged-model", "digest": "b" * 32}
        )
        tampered_cases.append(
            ("model", model, "deterministic must be true iff")
        )

        timestamp = copy.deepcopy(run)
        timestamp["question_intent_contract"]["provenance"][
            "generated_at"
        ] = "2026-08-16T00:00:01+00:00"
        tampered_cases.append(
            ("timestamp", timestamp, "generated_at must be identical")
        )

        for label, tampered, expected_error in tampered_cases:
            with self.subTest(field=label):
                errors = engine.validate_understanding_run(tampered)
                self.assertTrue(
                    any(expected_error in error for error in errors),
                    errors,
                )

    def test_failure_error_code_stage_count_and_id_cannot_be_forged(self) -> None:
        question, draft = generic_list_fixture("failure_tamper")
        location_mention = next(
            mention
            for mention in draft["explicit_mentions"]
            if mention["kind"] == "scope_location"
        )
        location_mention["kind"] = "filter_value"
        failed = engine.build_question_understanding(
            question,
            draft=draft,
            generated_at=STAMP,
        )
        self.assertEqual(engine.validate_understanding_run(failed), [])

        wrong_code = copy.deepcopy(failed)
        wrong_code["errors"][0]["code"] = "explicit_span_mismatch"
        wrong_code["errors"][0]["message"] = (
            "Question understanding terminated safely at context; "
            "reason_code=explicit_span_mismatch."
        )

        wrong_stage = copy.deepcopy(failed)
        wrong_stage["errors"][0]["stage"] = "decompose"
        wrong_stage["errors"][0]["message"] = (
            "Question understanding terminated safely at decompose; "
            "reason_code=unbound_intent_literal."
        )

        wrong_count = copy.deepcopy(failed)
        wrong_count["errors"].append(copy.deepcopy(wrong_count["errors"][0]))

        wrong_id = copy.deepcopy(failed)
        wrong_id["question_understanding_run_id"] = changed_identifier(
            wrong_id["question_understanding_run_id"]
        )

        cases = (
            ("code", wrong_code, "deterministic content ID"),
            ("stage", wrong_stage, "cannot be reported at stage"),
            ("count", wrong_count, "requires exactly one error"),
            ("id", wrong_id, "deterministic content ID"),
        )
        for label, tampered, expected_error in cases:
            with self.subTest(field=label):
                errors = engine.validate_understanding_run(tampered)
                self.assertTrue(
                    any(expected_error in error for error in errors),
                    errors,
                )

    def test_qcg_mention_node_values_must_match_question_spans(self) -> None:
        question, draft = generic_list_fixture("qcg_span")
        run = compile_fixture(question, draft)
        edge = run["query_context_graph"]["edges"][0]
        node_id = edge["from_ref"]

        for field in ("surface", "canonical_value"):
            tampered = copy.deepcopy(run)
            node = next(
                item
                for item in tampered["query_context_graph"]["nodes"]
                if item["node_id"] == node_id
            )
            node[field] = "forged-node-value"
            with self.subTest(field=field):
                errors = engine.validate_understanding_run(tampered)
                self.assertTrue(
                    any(
                        "mention surface/canonical_value must exactly match" in error
                        for error in errors
                    ),
                    errors,
                )

    def test_model_authored_descriptive_fields_are_normalized(self) -> None:
        question, draft = generic_list_fixture("normalized_text")
        draft["requested"]["operation_graph"]["external_inputs"][0][
            "description"
        ] = "model-authored external description"
        run = compile_fixture(question, draft)
        external_input = run["question_intent_contract"]["requested"][
            "operation_graph"
        ]["external_inputs"][0]
        self.assertEqual(
            external_input["description"],
            "Compiler-declared scope record_set input.",
        )

        excluded_question, excluded_draft = generic_list_fixture("normalized_reason")
        excluded_question["original_question"] += "説明は不要です。"
        excluded_draft["not_requested"] = [
            {
                "item": "説明",
                "reason": "model-authored exclusion reason",
                "confidence": "high",
                "handling": "omit",
            }
        ]
        excluded_draft["explicit_mentions"].append(
            explicit_mention(
                excluded_question["original_question"],
                "説明",
                "not_requested",
            )
        )
        excluded_run = compile_fixture(excluded_question, excluded_draft)
        self.assertEqual(
            excluded_run["question_intent_contract"]["not_requested"][0]["reason"],
            "Explicitly excluded by an exact question span.",
        )

        ambiguous_question, ambiguous_draft, left, right = alternative_scope_fixture(
            "normalized_ambiguity"
        )
        ambiguous_run = compile_fixture(
            ambiguous_question,
            declare_scope_ambiguity(ambiguous_draft, (left, right)),
        )
        ambiguity = ambiguous_run["question_intent_contract"]["ambiguity"][0]
        self.assertEqual(ambiguity["issue"], "Unresolved scope interpretation.")
        self.assertEqual(
            {candidate["basis"] for candidate in ambiguity["candidates"]},
            {"Candidate scope interpretation grounded in exact question context."},
        )
        self.assertEqual(
            {item["statement"] for item in ambiguous_run["candidate_query_paths"][0]["assumptions"]},
            {"Candidate scope interpretation grounded in exact question context."},
        )

    def test_every_generated_id_is_stable_across_repeated_compilation(self) -> None:
        question, draft = cartesian_ambiguity_fixture("repeat")
        first = compile_fixture(question, draft, max_branches=4)
        later = engine.compile_intent_draft(
            question,
            draft,
            max_branches=4,
            generated_at="2026-08-16T02:00:00+00:00",
            started_at="2026-08-16T02:00:00+00:00",
            completed_at="2026-08-16T02:00:00+00:00",
        )

        first_ids = generated_ids(first)
        self.assertTrue(first_ids)
        self.assertEqual(first_ids, generated_ids(later))
        self.assertTrue(
            any(path.endswith(".ambiguity_id") for path in first_ids), first_ids
        )
        self.assertTrue(
            any(path.endswith(".candidate_id") for path in first_ids), first_ids
        )
        self.assertTrue(
            any(path.endswith(".intent_diff_id") for path in first_ids), first_ids
        )
        self.assertTrue(
            any(path.endswith(".assumption_id") for path in first_ids), first_ids
        )

    def test_content_dependent_ids_cannot_be_forged(self) -> None:
        question, draft = cartesian_ambiguity_fixture("forged_ids")
        run = compile_fixture(question, draft, max_branches=4)
        contract = run["question_intent_contract"]
        ambiguity = contract["ambiguity"][0]
        branch = run["candidate_query_paths"][0]
        identifiers = {
            "qur": run["question_understanding_run_id"],
            "qic": contract["question_intent_contract_id"],
            "qcg": run["query_context_graph"]["graph_id"],
            "graph": contract["requested"]["operation_graph"][
                "operation_graph_id"
            ],
            "ambiguity": ambiguity["ambiguity_id"],
            "candidate": ambiguity["candidates"][0]["candidate_id"],
            "branch": branch["branch_id"],
            "diff": branch["intent_diffs"][0]["intent_diff_id"],
            "assumption": branch["assumptions"][0]["assumption_id"],
        }
        self.assertEqual(engine.validate_understanding_run(run), [])

        for label, identifier in identifiers.items():
            tampered = replace_identifier_references(
                run,
                identifier,
                changed_identifier(identifier),
            )
            with self.subTest(identifier_kind=label):
                errors = engine.validate_understanding_run(tampered)
                self.assertTrue(errors, f"forged {label} identifier was accepted")


class ContextAndGateTest(unittest.TestCase):
    def test_context_priority_rejects_lower_conflict_but_keeps_equal_priority(self) -> None:
        sources = [
            {
                "source_id": "source_question",
                "source_type": "question_explicit",
                "source_ref": "question:q",
                "content_sha256": None,
                "span": {"start": 0, "end": 1, "text": "A"},
            },
            {
                "source_id": "source_question_peer",
                "source_type": "question_explicit",
                "source_ref": "question:q:peer",
                "content_sha256": None,
                "span": {"start": 0, "end": 1, "text": "C"},
            },
            {
                "source_id": "source_conversation",
                "source_type": "conversation_explicit",
                "source_ref": "conversation:user",
                "content_sha256": None,
                "span": None,
            },
            {
                "source_id": "source_semantic",
                "source_type": "semantic_candidate",
                "source_ref": "catalog:semantic",
                "content_sha256": None,
                "span": None,
            },
        ]
        nodes = [
            {
                "node_id": "node_a",
                "node_type": "entity",
                "surface": "A",
                "canonical_value": "entity_a",
            },
            {
                "node_id": "node_b",
                "node_type": "entity",
                "surface": "B",
                "canonical_value": "entity_b",
            },
            {
                "node_id": "node_c",
                "node_type": "entity",
                "surface": "C",
                "canonical_value": "entity_c",
            },
            {
                "node_id": "node_scope",
                "node_type": "scope",
                "surface": None,
                "canonical_value": "scope_slot",
            },
        ]

        def edge(
            edge_id: str,
            from_ref: str,
            source_ref: str,
            source_type: str,
        ) -> dict[str, object]:
            return {
                "edge_id": edge_id,
                "from_ref": from_ref,
                "to_ref": "node_scope",
                "relation": "resolves_scope",
                "source_type": source_type,
                "source_ref": source_ref,
                "support_level": "high",
                "match_kind": "exact_value",
            }

        graph = engine.resolve_context_candidates(
            graph_id="qcg_fixture",
            sources=sources,
            nodes=nodes,
            candidate_edges=[
                edge("edge_a", "node_a", "source_question", "question_explicit"),
                edge(
                    "edge_c",
                    "node_c",
                    "source_question_peer",
                    "question_explicit",
                ),
                edge(
                    "edge_b",
                    "node_b",
                    "source_conversation",
                    "conversation_explicit",
                ),
                edge(
                    "edge_semantic_same",
                    "node_a",
                    "source_semantic",
                    "semantic_candidate",
                ),
            ],
        )
        self.assertEqual(
            {item["edge_id"] for item in graph["edges"]},
            {"edge_a", "edge_c", "edge_semantic_same"},
        )
        self.assertEqual(
            [item["edge"]["edge_id"] for item in graph["rejected_context"]],
            ["edge_b"],
        )
        self.assertEqual(
            graph["rejected_context"][0]["reason_code"],
            "lower_priority_conflict",
        )

    def test_intent_gate_ready_clarify_and_abstain(self) -> None:
        question, base_draft = generic_list_fixture("gates")
        ready = compile_fixture(question, base_draft)
        self.assertEqual(
            (ready["final_status"], ready["intent_gate"]["action"]),
            ("ready_for_retrieval", "retrieve"),
        )

        clarify = compile_fixture(
            question_input("こんにちは。", "q_gate_clarify"),
            unknown_intent_draft(),
        )
        self.assertEqual(
            (clarify["final_status"], clarify["intent_gate"]["action"]),
            ("clarification_required", "clarify"),
        )
        self.assertIn("target_unresolved", clarify["intent_gate"]["reason_codes"])

        conflicting = copy.deepcopy(base_draft)
        conflicting["requested"]["requested_outputs"][0]["cardinality"] = {
            "mode": "multiple",
            "expected_count": None,
        }
        abstained = compile_fixture(question, conflicting)
        self.assertEqual(
            (abstained["final_status"], abstained["intent_gate"]["action"]),
            ("abstained", "abstain"),
        )


class DeterministicSupportedLaneTest(unittest.TestCase):
    def build_without_model(
        self,
        question: dict[str, object],
        *,
        generated_at: str = STAMP,
    ) -> dict[str, object]:
        with patch.object(
            engine,
            "OllamaStructuredIntentClient",
            side_effect=AssertionError("deterministic lane called Ollama"),
        ):
            return engine.build_question_understanding(
                question,
                retry_limit=0,
                timeout=0.01,
                generated_at=generated_at,
            )

    def assert_falls_back_safely(self, question: dict[str, object]) -> None:
        self.assertIsNone(engine.derive_supported_intent_draft(question))
        client = SequenceClient([{"invalid": True}])
        run = engine.build_question_understanding(
            question,
            client=client,
            retry_limit=0,
            timeout=1,
            generated_at=STAMP,
        )
        self.assertEqual(client.generate_calls, 1)
        self.assertEqual(run["final_status"], "failed")
        self.assertEqual(engine.validate_understanding_run(run), [])

    def assert_deterministic_runtime(self, run: dict[str, object]) -> None:
        self.assertTrue(
            run["question_intent_contract"]["provenance"]["deterministic"]
        )
        self.assertEqual(run["runtime_metadata"]["backend"], "local_sequential")
        self.assertEqual(
            run["runtime_metadata"]["models"],
            [
                {
                    "role": "validation",
                    "name": "question-understanding-compiler:0.1",
                    "digest": None,
                }
            ],
        )

    def assert_manual_compound_semantics(self, run: dict[str, object]) -> None:
        requested = run["question_intent_contract"]["requested"]
        graph = requested["operation_graph"]

        self.assertEqual(run["final_status"], "ready_for_retrieval")
        self.assertEqual(
            operator_signature(run),
            ["filter", "filter", "project", "mean", "argmin_all", "project"],
        )
        self.assertEqual(requested["target"]["canonical_type"], "record")
        self.assertEqual(requested["scope"]["location"], "青葉バイオメディカル機器")
        self.assertEqual(requested["scope"]["container"], "train.csv")
        self.assertEqual(
            requested["scope"]["filters"],
            [
                {
                    "field": "EducationField",
                    "operator": "eq",
                    "value": "Marketing",
                },
                {
                    "field": "MonthlyIncome",
                    "operator": "gt",
                    "value": 10_000,
                },
            ],
        )
        self.assertEqual(graph["nodes"][2]["fields"], ["Age"])
        self.assertEqual(graph["nodes"][4]["field"], "Age")
        self.assertEqual(graph["nodes"][5]["fields"], ["id"])
        self.assertEqual(
            graph["nodes"][4]["candidate_set_ref"],
            graph["nodes"][1]["output_ref"],
        )
        self.assertEqual(graph["nodes"][4]["tie_policy"], "all")
        self.assertEqual(
            [
                (output["return_field"], output["cardinality"]["mode"])
                for output in requested["requested_outputs"]
            ],
            [("value", "single"), ("identifier", "all")],
        )
        self.assert_deterministic_runtime(run)
        self.assertEqual(engine.validate_understanding_run(run), [])

    def test_manual_list_uses_deterministic_lane_without_ollama(self) -> None:
        question_text = (
            "AYMのPLにおいて、フェーズが探索的分析・仮説整理に一致する"
            "タスクIDをすべて挙げてください。"
        )
        question = question_input(question_text, "q_lane_manual_list")
        draft = engine.derive_supported_intent_draft(question)
        self.assertIsNotNone(draft)
        self.assertEqual(engine.validate_intent_draft(draft), [])

        run = self.build_without_model(question)
        requested = run["question_intent_contract"]["requested"]

        self.assertEqual(run["final_status"], "ready_for_retrieval")
        self.assertEqual(operator_signature(run), ["filter", "project"])
        self.assertEqual(requested["scope"]["location"], "AYM")
        self.assertEqual(requested["scope"]["container"], "PL")
        self.assertEqual(
            requested["scope"]["filters"],
            [
                {
                    "field": "フェーズ",
                    "operator": "eq",
                    "value": "探索的分析・仮説整理",
                }
            ],
        )
        self.assertEqual(requested["target"]["canonical_type"], "task")
        self.assertEqual(
            (
                requested["requested_outputs"][0]["return_field"],
                requested["requested_outputs"][0]["cardinality"]["mode"],
            ),
            ("identifier", "all"),
        )
        self.assert_deterministic_runtime(run)
        self.assertEqual(engine.validate_understanding_run(run), [])

    def test_manual_suffix_list_uses_deterministic_lane_without_ollama(self) -> None:
        question_text = (
            "組織Suffixのplan_suffix.xlsxにおいて、"
            "探索的分析フェーズに一致するタスクIDを"
            "すべて挙げてください。"
        )
        question = question_input(question_text, "q_lane_suffix_list")
        draft = engine.derive_supported_intent_draft(question)
        self.assertIsNotNone(draft)
        self.assertEqual(engine.validate_intent_draft(draft), [])

        run = self.build_without_model(question)
        requested = run["question_intent_contract"]["requested"]

        self.assertEqual(run["final_status"], "ready_for_retrieval")
        self.assertEqual(operator_signature(run), ["filter", "project"])
        self.assertEqual(
            requested["scope"],
            {
                "container": "plan_suffix.xlsx",
                "location": "組織Suffix",
                "time_or_version": None,
                "filters": [
                    {
                        "field": "フェーズ",
                        "operator": "eq",
                        "value": "探索的分析",
                    }
                ],
                "source": "explicit",
                "match_mode": "exact_normalized",
            },
        )
        self.assertEqual(requested["target"]["canonical_type"], "task")
        self.assertEqual(
            (
                requested["requested_outputs"][0]["return_field"],
                requested["requested_outputs"][0]["cardinality"]["mode"],
            ),
            ("identifier", "all"),
        )
        self.assert_deterministic_runtime(run)
        self.assertEqual(engine.validate_understanding_run(run), [])

        forbidden_keys = {
            "answer_plan",
            "answerability_gate",
            "final_answer",
            "retrieval_hits",
            "retrieved_evidence_bundles",
            "primary_query_path",
        }

        def visit(value: object) -> None:
            if isinstance(value, dict):
                self.assertTrue(forbidden_keys.isdisjoint(value))
                for child in value.values():
                    visit(child)
            elif isinstance(value, list):
                for child in value:
                    visit(child)

        visit(run)
        self.assertFalse(run["provenance"]["answer_data_used"])
        self.assertFalse(run["provenance"]["past_answers_used"])

    def test_generic_opaque_list_variants_preserve_lane_semantics_and_ids(self) -> None:
        runs = []
        for suffix in ("p7", "q9"):
            question, _ = generic_list_fixture(f"lane_{suffix}")
            draft = engine.derive_supported_intent_draft(question)
            self.assertIsNotNone(draft)
            self.assertEqual(engine.validate_intent_draft(draft), [])
            run = self.build_without_model(question)
            self.assertEqual(run["final_status"], "ready_for_retrieval")
            self.assertEqual(operator_signature(run), ["filter", "project"])
            self.assertEqual(
                run["question_intent_contract"]["requested"]["scope"]["filters"][0][
                    "operator"
                ],
                "eq",
            )
            self.assert_deterministic_runtime(run)
            self.assertEqual(engine.validate_understanding_run(run), [])
            runs.append(run)

        self.assertNotEqual(
            runs[0]["question_intent_contract"]["question_intent_contract_id"],
            runs[1]["question_intent_contract"]["question_intent_contract_id"],
        )
        self.assertEqual(operator_signature(runs[0]), operator_signature(runs[1]))

        repeated_question, _ = generic_list_fixture("lane_repeat")
        first = self.build_without_model(repeated_question)
        later = self.build_without_model(
            repeated_question,
            generated_at="2026-08-16T03:00:00+00:00",
        )
        self.assertEqual(generated_ids(first), generated_ids(later))

    def test_manual_compound_uses_exact_deterministic_dag(self) -> None:
        question_text = (
            "青葉バイオメディカル機器のtrain.csvにおいて、EducationFieldがMarketingであり、かつ"
            "MonthlyIncomeが10000より大きいデータを抽出し、Ageの平均値を計算して"
            "ください。その平均値に最も近い年齢のidをすべて答えてください。"
        )
        question = question_input(question_text, "q_lane_manual_compound")
        draft = engine.derive_supported_intent_draft(question)
        self.assertIsNotNone(draft)
        self.assertEqual(engine.validate_intent_draft(draft), [])

        run = self.build_without_model(question)
        self.assert_manual_compound_semantics(run)

    def test_short_conjunction_compound_uses_same_deterministic_dag(self) -> None:
        question_text = (
            "青葉バイオメディカル機器のtrain.csvにおいて、EducationFieldがMarketingかつ"
            "MonthlyIncomeが10000より大きいデータを抽出し、Ageの平均値を計算して"
            "ください。その平均値に最も近い年齢のidをすべて答えてください。"
        )
        question = question_input(question_text, "q_lane_short_conjunction")
        draft = engine.derive_supported_intent_draft(question)
        self.assertIsNotNone(draft)
        self.assertEqual(engine.validate_intent_draft(draft), [])

        run = self.build_without_model(question)
        self.assert_manual_compound_semantics(run)

    def test_connector_tokens_do_not_enter_supported_list_lane(self) -> None:
        cases = {
            "value_aruiwa": (
                "組織Aのa.csvにおいて、FieldAがAlphaあるいはBetaに一致する"
                "TaskIDをすべて挙げてください。"
            ),
            "scope_wakashikuwa": (
                "組織A若しくは組織Bのa.csvにおいて、FieldAがValueAに一致する"
                "TaskIDをすべて挙げてください。"
            ),
            "field_oyobi": (
                "組織Aのa.csvにおいて、FieldAおよびFieldBがValueAに一致する"
                "TaskIDをすべて挙げてください。"
            ),
            "value_kanji_oyobi": (
                "組織Aのa.csvにおいて、FieldAがAlpha及びBetaに一致する"
                "TaskIDをすべて挙げてください。"
            ),
            "compact_ka": (
                "組織Aのa.csvにおいて、FieldAがAlphaかBetaに一致する"
                "TaskIDをすべて挙げてください。"
            ),
            "scope_ka": (
                "組織Aか組織Bのa.csvにおいて、FieldAがValueAに一致する"
                "TaskIDをすべて挙げてください。"
            ),
            "compact_to": (
                "組織Aのa.csvにおいて、FieldAがAlphaとBetaに一致する"
                "TaskIDをすべて挙げてください。"
            ),
            "ascii_slash": (
                "組織Aのa.csvにおいて、FieldAがAlpha/Betaに一致する"
                "TaskIDをすべて挙げてください。"
            ),
            "fullwidth_slash": (
                "組織Aのa.csvにおいて、FieldAがAlpha／Betaに一致する"
                "TaskIDをすべて挙げてください。"
            ),
            "word_or": (
                "組織Aのa.csvにおいて、FieldAがAlpha or Betaに一致する"
                "TaskIDをすべて挙げてください。"
            ),
            "word_and": (
                "組織Aのa.csvにおいて、FieldAがAlpha AND Betaに一致する"
                "TaskIDをすべて挙げてください。"
            ),
        }
        for label, text in cases.items():
            with self.subTest(label=label):
                self.assert_falls_back_safely(
                    question_input(text, f"q_lane_connector_{label}")
                )

    def test_ascii_connector_substrings_do_not_false_positive(self) -> None:
        question = question_input(
            "Corporationのreports/Anderson.csvにおいて、"
            "AndersonFieldがCorporationに一致する"
            "TaskIDをすべて挙げてください。",
            "q_lane_connector_substrings",
        )
        draft = engine.derive_supported_intent_draft(question)
        self.assertIsNotNone(draft)
        self.assertEqual(engine.validate_intent_draft(draft), [])

        run = self.build_without_model(question)
        requested = run["question_intent_contract"]["requested"]
        self.assertEqual(run["final_status"], "ready_for_retrieval")
        self.assertEqual(requested["scope"]["location"], "Corporation")
        self.assertEqual(requested["scope"]["container"], "reports/Anderson.csv")
        self.assertEqual(
            requested["scope"]["filters"],
            [
                {
                    "field": "AndersonField",
                    "operator": "eq",
                    "value": "Corporation",
                }
            ],
        )
        self.assert_deterministic_runtime(run)
        self.assertEqual(engine.validate_understanding_run(run), [])

    def test_omitted_output_and_exclusion_requests_cannot_be_ready(self) -> None:
        output_question_text = (
            "組織Aのa.csvにおいて、FieldAがValueAに一致する"
            "TaskIDをすべて挙げ、その件数も答えてください。"
        )
        output_question = question_input(
            output_question_text,
            "q_lane_omitted_additional_output",
        )
        output_draft = list_identifier_draft(
            output_question_text,
            location="組織A",
            container="a.csv",
            filter_field="FieldA",
            filter_value="ValueA",
            identifier_field="TaskID",
        )

        exclusion_question_text = (
            "組織Aのa.csvにおいて、FieldAがValueAに一致する"
            "TaskIDをすべて挙げてください。説明は不要です。"
        )
        exclusion_question = question_input(
            exclusion_question_text,
            "q_lane_omitted_exclusion",
        )
        exclusion_draft = list_identifier_draft(
            exclusion_question_text,
            location="組織A",
            container="a.csv",
            filter_field="FieldA",
            filter_value="ValueA",
            identifier_field="TaskID",
        )

        for label, question, draft in (
            ("additional_output", output_question, output_draft),
            ("explicit_exclusion", exclusion_question, exclusion_draft),
        ):
            with self.subTest(label=label):
                self.assertIsNone(engine.derive_supported_intent_draft(question))
                run = engine.build_question_understanding(
                    question,
                    draft=draft,
                    generated_at=STAMP,
                )
                self.assertNotEqual(run["final_status"], "ready_for_retrieval")
                self.assertEqual(engine.validate_understanding_run(run), [])

    def test_double_negative_cannot_generate_not_requested_omission(self) -> None:
        endings = {
            "exclude_not": "説明は除外しないでください。",
            "not_unneeded": "説明は不要ではないです。",
            "not_not_include": "説明は含めないわけではないです。",
        }
        prefix = (
            "組織Aのa.csvにおいて、FieldAがValueAに一致する"
            "TaskIDをすべて挙げてください。"
        )
        for label, ending in endings.items():
            with self.subTest(label=label):
                question_text = prefix + ending
                question = question_input(
                    question_text,
                    f"q_lane_double_negative_{label}",
                )
                draft = list_identifier_draft(
                    question_text,
                    location="組織A",
                    container="a.csv",
                    filter_field="FieldA",
                    filter_value="ValueA",
                    identifier_field="TaskID",
                )
                draft["not_requested"] = [
                    {
                        "item": "説明",
                        "reason": "model-authored double-negative omission",
                        "confidence": "high",
                        "handling": "omit",
                    }
                ]
                draft["explicit_mentions"].append(
                    explicit_mention(question_text, "説明", "not_requested")
                )

                run = engine.build_question_understanding(
                    question,
                    draft=draft,
                    generated_at=STAMP,
                )
                self.assertEqual(run["final_status"], "failed")
                self.assertEqual(
                    run["errors"][0]["code"],
                    "not_requested_without_negation",
                )
                self.assertEqual(engine.validate_understanding_run(run), [])

    def test_negative_equality_and_conflicting_cardinality_cannot_be_ready(
        self,
    ) -> None:
        negative_question_text = (
            "組織Aのa.csvにおいて、FieldAがValueAに一致しない"
            "TaskIDをすべて挙げてください。"
        )
        negative_question = question_input(
            negative_question_text,
            "q_lane_negative_equality",
        )
        negative_draft = list_identifier_draft(
            negative_question_text,
            location="組織A",
            container="a.csv",
            filter_field="FieldA",
            filter_value="ValueA",
            identifier_field="TaskID",
        )

        cardinality_question_text = (
            "組織Aのa.csvにおいて、FieldAがValueAに一致する"
            "TaskIDをすべてではなく1つ挙げてください。"
        )
        cardinality_question = question_input(
            cardinality_question_text,
            "q_lane_conflicting_cardinality",
        )
        cardinality_draft = list_identifier_draft(
            cardinality_question_text,
            location="組織A",
            container="a.csv",
            filter_field="FieldA",
            filter_value="ValueA",
            identifier_field="TaskID",
        )

        for label, question, draft in (
            ("negative_equality", negative_question, negative_draft),
            ("conflicting_cardinality", cardinality_question, cardinality_draft),
        ):
            with self.subTest(label=label):
                self.assertIsNone(engine.derive_supported_intent_draft(question))
                run = engine.build_question_understanding(
                    question,
                    draft=draft,
                    generated_at=STAMP,
                )
                self.assertNotEqual(run["final_status"], "ready_for_retrieval")
                self.assertEqual(engine.validate_understanding_run(run), [])

    def test_middle_dot_is_supported_only_inside_filter_value(self) -> None:
        negative_cases = {
            "location": (
                "組織・Aのa.csvにおいて、FieldAがValueAに一致する"
                "TaskIDをすべて挙げてください。"
            ),
            "container": (
                "組織Aのa・b.csvにおいて、FieldAがValueAに一致する"
                "TaskIDをすべて挙げてください。"
            ),
            "field": (
                "組織Aのa.csvにおいて、Field・AがValueAに一致する"
                "TaskIDをすべて挙げてください。"
            ),
            "identifier": (
                "組織Aのa.csvにおいて、FieldAがValueAに一致する"
                "Task・IDをすべて挙げてください。"
            ),
        }
        for label, text in negative_cases.items():
            with self.subTest(label=label):
                self.assert_falls_back_safely(
                    question_input(text, f"q_lane_middle_dot_{label}")
                )

        supported = question_input(
            "AYMのPLにおいて、フェーズが探索的分析・仮説整理に一致する"
            "タスクIDをすべて挙げてください。",
            "q_lane_middle_dot_value",
        )
        draft = engine.derive_supported_intent_draft(supported)
        self.assertIsNotNone(draft)
        run = self.build_without_model(supported)
        self.assertEqual(run["final_status"], "ready_for_retrieval")
        self.assertEqual(
            run["question_intent_contract"]["requested"]["scope"]["filters"][0][
                "value"
            ],
            "探索的分析・仮説整理",
        )
        self.assertEqual(engine.validate_understanding_run(run), [])

    def test_intent_origin_and_input_hash_distinguish_compilation_paths(self) -> None:
        question, _ = generic_list_fixture("lane_origin")
        derived = engine.derive_supported_intent_draft(question)
        self.assertIsNotNone(derived)
        expected_hash = engine.sha256_json(derived)

        supported_run = self.build_without_model(question)
        supplied_run = engine.build_question_understanding(
            question,
            draft=copy.deepcopy(derived),
            generated_at=STAMP,
        )
        model_client = SequenceClient([copy.deepcopy(derived)])
        model_run = engine.build_question_understanding(
            question,
            client=model_client,
            retry_limit=0,
            timeout=1,
            generated_at=STAMP,
        )

        runs = (
            ("supported_lane", supported_run, True, ["validation"]),
            ("supplied_draft", supplied_run, True, ["validation"]),
            ("structured_model", model_run, False, ["intent", "validation"]),
        )
        for expected_origin, run, deterministic, model_roles in runs:
            with self.subTest(origin=expected_origin):
                provenance = run["question_intent_contract"]["provenance"]
                self.assertEqual(run["final_status"], "ready_for_retrieval")
                self.assertEqual(provenance["intent_origin"], expected_origin)
                self.assertEqual(provenance["intent_input_sha256"], expected_hash)
                self.assertEqual(provenance["deterministic"], deterministic)
                self.assertEqual(
                    [item["role"] for item in run["runtime_metadata"]["models"]],
                    model_roles,
                )
                self.assertEqual(engine.validate_understanding_run(run), [])

        fallback_hashes = set()
        for invalid_payload in ({"invalid": True}, {"requested": {}}):
            client = SequenceClient([invalid_payload])
            failed = engine.build_question_understanding(
                question,
                client=client,
                retry_limit=0,
                timeout=1,
                generated_at=STAMP,
            )
            provenance = failed["question_intent_contract"]["provenance"]
            self.assertEqual(failed["final_status"], "failed")
            self.assertEqual(provenance["intent_origin"], "compiler_fallback")
            self.assertRegex(provenance["intent_input_sha256"], r"^[0-9a-f]{64}$")
            self.assertFalse(provenance["deterministic"])
            self.assertEqual(
                [item["role"] for item in failed["runtime_metadata"]["models"]],
                ["intent", "validation"],
            )
            self.assertEqual(engine.validate_understanding_run(failed), [])
            fallback_hashes.add(provenance["intent_input_sha256"])
        self.assertEqual(len(fallback_hashes), 1)

        origin_tamper = copy.deepcopy(supported_run)
        origin_tamper["question_intent_contract"]["provenance"][
            "intent_origin"
        ] = "supplied_draft"
        hash_tamper = copy.deepcopy(supported_run)
        original_hash = hash_tamper["question_intent_contract"]["provenance"][
            "intent_input_sha256"
        ]
        hash_tamper["question_intent_contract"]["provenance"][
            "intent_input_sha256"
        ] = ("0" if original_hash[0] != "0" else "1") + original_hash[1:]
        id_tamper = copy.deepcopy(supported_run)
        qic = id_tamper["question_intent_contract"]
        qic["question_intent_contract_id"] = changed_identifier(
            qic["question_intent_contract_id"]
        )
        deterministic_tamper = copy.deepcopy(model_run)
        deterministic_tamper["question_intent_contract"]["provenance"][
            "deterministic"
        ] = True
        runtime_tamper = copy.deepcopy(supported_run)
        runtime_tamper["runtime_metadata"]["models"].insert(
            0,
            {"role": "intent", "name": "forged-model", "digest": "b" * 32},
        )

        for label, tampered in (
            ("origin", origin_tamper),
            ("hash", hash_tamper),
            ("id", id_tamper),
            ("deterministic", deterministic_tamper),
            ("runtime", runtime_tamper),
        ):
            with self.subTest(tamper=label):
                self.assertTrue(engine.validate_understanding_run(tampered))

    def test_unsupported_or_ambiguous_questions_fall_back_safely(self) -> None:
        alternative_question, _draft, _left, _right = alternative_scope_fixture(
            "lane_fallback"
        )
        fallback_questions = [alternative_question]
        fallback_texts = (
            (
                "組織Aのa.csvにおいて、組織Bのb.csvにおいて、"
                "FieldAがValueAに一致するTaskIDをすべて挙げてください。"
            ),
            (
                "組織Aのa.csvにおいて、FieldAがValueAに一致し、"
                "FieldBがValueBに一致するTaskIDをすべて挙げてください。"
            ),
            (
                "組織Aのa.csvにおいて、FieldAがValueAに一致する"
                "MysteryCodeをすべて挙げてください。"
            ),
            (
                "組織Aのa.csvにおいて、FieldAがValueAに一致する"
                "TaskIDを挙げないでください。"
            ),
            (
                "組織Aのa.csvにおいて、FieldAのValueAに一致する"
                "TaskIDをすべて挙げてください。"
            ),
            (
                "組織Aのa.csvにおいて、探索的分析ステップに一致する"
                "タスクIDをすべて挙げてください。"
            ),
            (
                "組織Aのa.csvにおいて、FieldAがValueAに一致する"
                "TaskIDをすべて挙げ、TaskIDを再掲してください。"
            ),
            (
                "青葉バイオメディカル機器のtrain.csvにおいて、"
                "EducationFieldがMarketingまたはMonthlyIncomeが10000より大きい"
                "データを抽出し、Ageの平均値を計算してください。"
                "その平均値に最も近い年齢のidをすべて答えてください。"
            ),
            (
                "青葉バイオメディカル機器のtrain.csvにおいて、"
                "EducationFieldがMarketingかつかつMonthlyIncomeが10000より大きい"
                "データを抽出し、Ageの平均値を計算してください。"
                "その平均値に最も近い年齢のidをすべて答えてください。"
            ),
            (
                "青葉バイオメディカル機器のtrain.csvにおいて、"
                "EducationFieldがMarketingかつMonthlyIncomeが10000より大きい"
                "データを抽出し、Ageの平均値を計算してください。"
                "その平均値に最も近いWeightのidをすべて答えてください。"
            ),
        )
        fallback_questions.extend(
            question_input(text, f"q_lane_fallback_{index}")
            for index, text in enumerate(fallback_texts)
        )

        for question in fallback_questions:
            with self.subTest(question_id=question["question_id"]):
                self.assert_falls_back_safely(question)

    def test_explicit_client_and_draft_keep_existing_paths(self) -> None:
        question, draft = generic_list_fixture("lane_explicit")
        client = SequenceClient([copy.deepcopy(draft)])
        model_run = engine.build_question_understanding(
            question,
            client=client,
            retry_limit=0,
            timeout=1,
            generated_at=STAMP,
        )
        self.assertEqual(client.generate_calls, 1)
        self.assertEqual(model_run["final_status"], "ready_for_retrieval")
        self.assertFalse(
            model_run["question_intent_contract"]["provenance"]["deterministic"]
        )
        self.assertEqual(
            [
                model["role"]
                for model in model_run["runtime_metadata"]["models"]
            ],
            ["intent", "validation"],
        )

        weakened = copy.deepcopy(draft)
        weakened["requested"]["scope"]["filters"][0]["operator"] = "gte"
        weakened["requested"]["operation_graph"]["operations"][0]["predicate"][
            "operator"
        ] = "gte"
        draft_run = engine.build_question_understanding(
            question,
            draft=weakened,
            generated_at=STAMP,
        )
        self.assertEqual(draft_run["final_status"], "abstained")
        self.assertEqual(engine.validate_understanding_run(draft_run), [])


class LeakageAndModelBoundaryTest(unittest.TestCase):
    def test_answer_and_validation_fields_are_rejected_at_input_boundary(self) -> None:
        question, draft = generic_list_fixture("leakage")
        for field in ("answer", "past_answer", "validation_answer", "final_answer"):
            candidate = {**question, field: "leak-canary-secret"}
            with self.subTest(field=field):
                self.assertTrue(engine.validate_question_input(candidate))

        draft_with_answer = copy.deepcopy(draft)
        draft_with_answer["final_answer"] = "leak-canary-secret"
        self.assertTrue(engine.validate_intent_draft(draft_with_answer))

        draft_with_source_ref = copy.deepcopy(draft)
        draft_with_source_ref["requested"]["operation_graph"]["external_inputs"][0][
            "source_ref"
        ] = "leak-canary-secret"
        self.assertTrue(engine.validate_intent_draft(draft_with_source_ref))

    def test_compiled_record_contains_no_answer_layer_artifacts(self) -> None:
        question, draft = generic_list_fixture("boundary")
        run = compile_fixture(question, draft)
        forbidden_keys = {
            "answer_plan",
            "answerability_gate",
            "final_answer",
            "retrieval_hits",
            "retrieved_evidence_bundles",
            "primary_query_path",
        }

        def visit(value: object) -> None:
            if isinstance(value, dict):
                self.assertTrue(forbidden_keys.isdisjoint(value))
                for child in value.values():
                    visit(child)
            elif isinstance(value, list):
                for child in value:
                    visit(child)

        visit(run)
        self.assertFalse(run["provenance"]["answer_data_used"])
        self.assertFalse(run["provenance"]["past_answers_used"])

    def test_strict_json_rejects_duplicate_nonfinite_and_extra_payload(self) -> None:
        for payload in (
            '{"a": 1, "a": 2}',
            '{"a": NaN}',
            '{"a": 1}{"b": 2}',
        ):
            with self.subTest(payload=payload):
                with self.assertRaises((ValueError, json.JSONDecodeError)):
                    engine.load_strict_json(payload)

    def test_invalid_model_output_retries_once_then_succeeds(self) -> None:
        question, draft = generic_list_fixture("retry")
        client = SequenceClient([{"invalid": True}, copy.deepcopy(draft)])
        run = engine.build_question_understanding(
            question,
            client=client,
            retry_limit=1,
            timeout=1,
        )
        self.assertEqual(client.generate_calls, 2)
        self.assertEqual(run["final_status"], "ready_for_retrieval")

    def test_retry_exhaustion_returns_terminal_failure(self) -> None:
        question, _ = generic_list_fixture("retry_failure")
        client = SequenceClient([{"invalid": True}, {"still_invalid": True}])
        run = engine.build_question_understanding(
            question,
            client=client,
            retry_limit=1,
            timeout=1,
        )
        self.assertEqual(client.generate_calls, 2)
        self.assertEqual(run["final_status"], "failed")
        self.assertEqual(run["candidate_query_paths"], [])
        self.assertTrue(run["errors"])
        self.assertEqual(engine.validate_understanding_run(run), [])

    def test_slow_timeout_failures_have_coherent_runtime_interval(self) -> None:
        question, _ = generic_list_fixture("slow_failure")

        for fail_on in ("check", "generate"):
            with self.subTest(fail_on=fail_on):
                with patch.object(
                    engine.time,
                    "monotonic",
                    side_effect=(100.0, 400.0),
                ):
                    run = engine.build_question_understanding(
                        question,
                        client=SlowFailureClient(fail_on),
                        retry_limit=0,
                        timeout=0.01,
                    )

                self.assertEqual(run["final_status"], "failed")
                self.assertEqual(run["errors"][0]["stage"], "runtime")
                self.assertEqual(engine.validate_understanding_run(run), [])

                runtime = run["runtime_metadata"]
                started_at = datetime.fromisoformat(runtime["started_at"])
                completed_at = datetime.fromisoformat(runtime["completed_at"])
                interval_ms = (completed_at - started_at).total_seconds() * 1000
                self.assertGreater(interval_ms, 0)
                self.assertLess(runtime["duration_ms"], 1_000)
                self.assertLessEqual(
                    abs(runtime["duration_ms"] - interval_ms),
                    1,
                )


class QuestionOnlyFixtureTest(unittest.TestCase):
    def test_manual_filtered_identifier_question_compiles_without_answer_data(self) -> None:
        question = (
            "AYMのPLにおいて、フェーズが探索的分析・仮説整理に一致する"
            "タスクIDをすべて挙げてください。"
        )
        draft = list_identifier_draft(
            question,
            location="AYM",
            container="PL",
            filter_field="フェーズ",
            filter_value="探索的分析・仮説整理",
            identifier_field="タスクID",
        )
        run = compile_fixture(question_input(question, "q_manual_list"), draft)
        self.assertEqual(run["final_status"], "ready_for_retrieval")
        self.assertEqual(operator_signature(run), ["filter", "project"])
        output = run["question_intent_contract"]["requested"]["requested_outputs"][0]
        self.assertEqual(output["return_field"], "identifier")
        self.assertEqual(output["cardinality"]["mode"], "all")
        source_ref = run["question_intent_contract"]["requested"]["operation_graph"][
            "external_inputs"
        ][0]["source_ref"]
        self.assertTrue(source_ref.startswith("question:"))

    def test_manual_compound_question_preserves_exact_operation_dag(self) -> None:
        question = (
            "青葉バイオメディカル機器のtrain.csvにおいて、EducationFieldがMarketingであり、かつ"
            "MonthlyIncomeが10000より大きいデータを抽出し、Ageの平均値を計算して"
            "ください。その平均値に最も近い年齢のidをすべて答えてください。"
        )
        draft = compound_mean_nearest_draft(
            question,
            location="青葉バイオメディカル機器",
            container="train.csv",
            equality_field="EducationField",
            equality_value="Marketing",
            threshold_field="MonthlyIncome",
            threshold=10_000,
            metric_field="Age",
            identifier_field="id",
        )
        run = compile_fixture(question_input(question, "q_manual_compound"), draft)
        self.assertEqual(run["final_status"], "ready_for_retrieval")
        self.assertEqual(
            operator_signature(run),
            ["filter", "filter", "project", "mean", "argmin_all", "project"],
        )
        requested = run["question_intent_contract"]["requested"]
        filters = requested["scope"]["filters"]
        self.assertEqual([item["operator"] for item in filters], ["eq", "gt"])
        graph = requested["operation_graph"]
        mean = graph["nodes"][3]
        nearest = graph["nodes"][4]
        self.assertEqual(mean["calculation_precision"], "exact_unrounded")
        self.assertEqual(nearest["candidate_set_ref"], graph["nodes"][1]["output_ref"])
        self.assertEqual(nearest["tie_policy"], "all")
        self.assertEqual(
            [(item["return_field"], item["cardinality"]["mode"]) for item in requested["requested_outputs"]],
            [("value", "single"), ("identifier", "all")],
        )

    def test_opaque_token_substitution_changes_data_not_graph_semantics(self) -> None:
        first_question, first_draft = generic_compound_fixture("alpha")
        second_question, second_draft = generic_compound_fixture("omega")
        first = compile_fixture(first_question, first_draft)
        second = compile_fixture(second_question, second_draft)

        self.assertEqual(operator_signature(first), operator_signature(second))
        first_requested = first["question_intent_contract"]["requested"]
        second_requested = second["question_intent_contract"]["requested"]
        self.assertEqual(
            [item["operator"] for item in first_requested["scope"]["filters"]],
            [item["operator"] for item in second_requested["scope"]["filters"]],
        )
        self.assertNotEqual(
            first_requested["scope"]["location"],
            second_requested["scope"]["location"],
        )
        self.assertNotEqual(
            first["question_intent_contract"]["question_intent_contract_id"],
            second["question_intent_contract"]["question_intent_contract_id"],
        )


if __name__ == "__main__":
    unittest.main()
