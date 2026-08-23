#!/usr/bin/env python3
"""Build the gold-free 100-question capability comparison baseline.

The fourth-run answer text is used only to distinguish answered rows from
explicit abstentions.  The v16 audit answer text is not emitted and is used
only to bind an audit-state digest.  Neither input is passed to question
understanding, structured execution, capability tagging, or validation.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import re
import sys
import tempfile
import unicodedata
from collections import Counter
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

import jsonschema


ROOT = Path(__file__).resolve().parents[1]
RAG = ROOT / "rag"
if str(RAG) not in sys.path:
    sys.path.insert(0, str(RAG))

from answer import validate_graph_answer  # noqa: E402
from glossary import build_glossary  # noqa: E402
from question_graph_runtime import (  # noqa: E402
    GRAPH_PLAN_VERSION,
    build_graph_plan,
)
from score_candidate_rules import (  # noqa: E402
    GRAPH_RULE_VERSION,
    graph_contract_for_question,
    validate_graph_contract,
)
from structured_candidate import (  # noqa: E402
    CANDIDATE_VERSION,
    StructuredCandidateEngine,
)


BUILDER_VERSION = "0.1"
SCHEMA_VERSION = "0.1"
SCHEMA_PATH = ROOT / "schemas" / "question-capability-matrix.schema.json"
OUTPUT_NAMES = {
    "jsonl": "question-capability-matrix.jsonl",
    "csv": "question-capability-matrix.csv",
    "summary": "coverage-summary.json",
    "markdown": "question-capability-matrix.md",
}
FORBIDDEN_INPUT_MARKERS = (
    "questions_valid",
    "questions-valid",
    "gold",
    "prediction",
)
ALLOWED_AUDIT_STATES = frozenset(
    {"fresh_source_supported", "source_recomputed", "old_source_supported"}
)
KNOWN_EXTENSIONS = frozenset(
    {
        ".csv",
        ".docx",
        ".emf",
        ".ipynb",
        ".json",
        ".md",
        ".pdf",
        ".png",
        ".pptx",
        ".py",
        ".tsv",
        ".xlsx",
    }
)
CAPABILITY_TAG_ORDER = (
    "multi_document_join",
    "version_or_state_diff",
    "office_native_structure",
    "style_or_annotation",
    "page_or_layout",
    "pdf_layout_or_table",
    "graph_value_recovery",
    "structured_data",
    "deterministic_calculation",
    "notebook_or_code_execution",
    "semantic_text_extraction",
    "spatial_relation",
    "entity_or_alias_resolution",
    "temporal_filtering",
    "table_structure",
    "complete_enumeration",
)
COMPONENT_NAMES = (
    "native_parser",
    "apple_vision",
    "paddleocr",
    "ndlocr_lite",
    "tesseract",
    "docling",
)


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_text(value: str) -> str:
    return sha256_bytes(value.encode("utf-8"))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def load_json(path: Path, role: str) -> Any:
    try:
        return json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
        )
    except json.JSONDecodeError as exc:
        raise ValueError(f"{role} is not valid JSON: {exc}") from exc


def _normalized_path(path: Path) -> str:
    return unicodedata.normalize("NFKC", path.as_posix()).casefold()


def reported_path(path: Path) -> str:
    """Return one NFC, repository-relative path when the target is in ROOT."""

    resolved = path.resolve()
    try:
        rendered = resolved.relative_to(ROOT.resolve()).as_posix()
    except ValueError:
        rendered = resolved.as_posix()
    return unicodedata.normalize("NFC", rendered)


def reject_forbidden_input(path: Path, role: str) -> Path:
    normalized = _normalized_path(path)
    marker = next(
        (value for value in FORBIDDEN_INPUT_MARKERS if value in normalized),
        None,
    )
    if marker is not None:
        raise ValueError(
            f"{role} input is forbidden because its path contains {marker!r}"
        )
    if path.is_symlink():
        raise ValueError(f"{role} input must not be a symlink")
    if not path.is_file():
        raise ValueError(f"{role} input is not a regular file: {path}")
    return path.resolve()


def validate_source_root(path: Path) -> Path:
    if path.is_symlink():
        raise ValueError("source root must not be a symlink")
    if not path.is_dir():
        raise ValueError(f"source root is not a directory: {path}")
    root = path.resolve()
    for child in root.rglob("*"):
        if child.is_symlink():
            raise ValueError(
                "source root contains a symlink: "
                + unicodedata.normalize("NFC", child.relative_to(root).as_posix())
            )
    return root


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def read_questions(
    path: Path,
    *,
    expected_question_count: int = 100,
) -> list[dict[str, str]]:
    path = reject_forbidden_input(path, "questions")
    if unicodedata.normalize("NFKC", path.name).casefold() != "questions_test.csv":
        raise ValueError("questions input must be named exactly questions_test.csv")
    with path.open(encoding="utf-8-sig", newline="") as handle:
        raw_rows = list(csv.reader(handle))
    if not raw_rows or raw_rows[0] != ["index", "question"]:
        raise ValueError(
            "questions input must contain exactly index,question; answer/gold columns are forbidden"
        )
    questions: list[dict[str, str]] = []
    for row_number, row in enumerate(raw_rows[1:], 2):
        if len(row) != 2 or not row[0] or not row[1].strip():
            raise ValueError(f"invalid question row {row_number}")
        if re.fullmatch(r"[0-9]+", row[0]) is None:
            raise ValueError(f"question index must be decimal at row {row_number}")
        questions.append({"index": row[0], "question": row[1]})
    if len(questions) != expected_question_count:
        raise ValueError(
            f"questions input must contain exactly {expected_question_count} rows"
        )
    indices = [item["index"] for item in questions]
    if len(indices) != len(set(indices)):
        raise ValueError("question indices must be unique")
    expected_indices = [str(index) for index in range(expected_question_count)]
    if indices != expected_indices:
        raise ValueError(
            f"question indices must be ordered exactly 0..{expected_question_count - 1}"
        )
    return questions


def _as_mapping(value: Any, role: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{role} must be an object")
    return value


def _as_nonempty_text(value: Any, role: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{role} must be a non-empty string")
    return value


def load_fourth_run(
    path: Path,
    questions: Sequence[Mapping[str, str]],
) -> tuple[dict[str, Mapping[str, Any]], Mapping[str, Any]]:
    path = reject_forbidden_input(path, "fourth run log")
    record = _as_mapping(load_json(path, "fourth run log"), "fourth run log")
    if record.get("モード") != "test":
        raise ValueError("fourth run log must be a test run")
    if record.get("質問数") != len(questions):
        raise ValueError("fourth run log question count mismatch")
    params = _as_mapping(record.get("パラメータ"), "fourth run parameters")
    if params.get("answer_path") != "question-graph":
        raise ValueError("fourth run log must use the question-graph answer path")
    if params.get("structured_candidate") is not True:
        raise ValueError("fourth run log must enable structured candidates")
    answers = record.get("回答")
    if not isinstance(answers, list) or len(answers) != len(questions):
        raise ValueError("fourth run log must have one answer row per question")
    by_id: dict[str, Mapping[str, Any]] = {}
    for position, (question, raw) in enumerate(zip(questions, answers), 1):
        row = _as_mapping(raw, f"fourth run answer {position}")
        index = _as_nonempty_text(row.get("index"), f"fourth run index {position}")
        text = _as_nonempty_text(row.get("質問"), f"fourth run question {position}")
        _as_nonempty_text(row.get("回答"), f"fourth run answer text {position}")
        if index != question["index"] or text != question["question"]:
            raise ValueError(f"fourth run row {position} does not match questions input")
        if index in by_id:
            raise ValueError(f"duplicate fourth run question index: {index}")
        by_id[index] = row
    return by_id, record


def load_v16_source_audit(
    path: Path,
    questions: Sequence[Mapping[str, str]],
    questions_sha256: str,
) -> tuple[dict[str, Mapping[str, Any]], Mapping[str, Any]]:
    path = reject_forbidden_input(path, "v16 source audit")
    record = _as_mapping(load_json(path, "v16 source audit"), "v16 source audit")
    if record.get("schema_version") != "audited-test-hybrid-0.4":
        raise ValueError("v16 source audit schema_version is not audited-test-hybrid-0.4")
    if record.get("question_count") != len(questions):
        raise ValueError("v16 source audit question count mismatch")
    if record.get("questions_sha256") != questions_sha256:
        raise ValueError("v16 source audit questions SHA-256 mismatch")
    policy = _as_nonempty_text(
        record.get("selection_policy"), "v16 source audit selection_policy"
    ).casefold()
    for required in ("no valid", "gold", "prior score"):
        if required not in policy:
            raise ValueError(
                "v16 source audit does not declare the required gold-free selection policy"
            )
    question_by_id = {item["index"]: item["question"] for item in questions}
    deltas = record.get("audited_deltas")
    if not isinstance(deltas, list):
        raise ValueError("v16 source audit audited_deltas must be an array")
    by_id: dict[str, Mapping[str, Any]] = {}
    for position, raw in enumerate(deltas, 1):
        delta = _as_mapping(raw, f"v16 audit delta {position}")
        index = _as_nonempty_text(delta.get("index"), f"v16 audit index {position}")
        question = _as_nonempty_text(
            delta.get("question"), f"v16 audit question {position}"
        )
        selection = _as_nonempty_text(
            delta.get("selection"), f"v16 audit selection {position}"
        )
        if index not in question_by_id or question_by_id[index] != question:
            raise ValueError(f"v16 audit delta {position} does not match questions input")
        if selection not in ALLOWED_AUDIT_STATES:
            raise ValueError(f"unsupported v16 audit selection: {selection}")
        if index in by_id:
            raise ValueError(f"duplicate v16 audit question index: {index}")
        for field in ("fresh_answer", "old_answer", "selected_answer", "rationale"):
            _as_nonempty_text(delta.get(field), f"v16 audit {field} {position}")
        by_id[index] = delta
    return by_id, record


def source_inventory(source_root: Path) -> dict[str, Any]:
    records: list[dict[str, Any]] = []
    seen_paths: set[str] = set()
    for path in source_root.rglob("*"):
        if not path.is_file():
            continue
        relative = unicodedata.normalize("NFC", path.relative_to(source_root).as_posix())
        if relative in seen_paths:
            raise ValueError(
                "source root contains duplicate NFC-normalized paths: " + relative
            )
        seen_paths.add(relative)
        records.append(
            {
                "path": relative,
                "size_bytes": path.stat().st_size,
                "sha256": sha256_file(path),
            }
        )
    records.sort(key=lambda item: item["path"])
    return {
        "file_count": len(records),
        "sha256": sha256_text(canonical_json(records)),
    }


def classify_baseline(row: Mapping[str, Any]) -> dict[str, Any]:
    answer = _as_nonempty_text(row.get("回答"), "fourth run answer")
    route = _as_nonempty_text(row.get("回答経路"), "fourth run answer route")
    raw_decision = row.get("structured_candidate_decision")
    decision = raw_decision if isinstance(raw_decision, Mapping) else {}
    structured_status = decision.get("status")
    structured_reason = decision.get("reason")
    graph = row.get("question_graph")
    graph = graph if isinstance(graph, Mapping) else {}
    validation = graph.get("output_validation")
    validation = validation if isinstance(validation, Mapping) else {}
    raw_validation_status = validation.get("validation_status")
    if raw_validation_status == "pass":
        validation_status = "pass"
    elif raw_validation_status in {"fail", "failed", "rejected"}:
        validation_status = "failed"
    elif raw_validation_status == "not_applicable":
        validation_status = "not_applicable"
    else:
        validation_status = "unknown"
    if (
        route == "structured-candidate"
        and structured_status == "resolved"
        and validation_status == "pass"
    ):
        state = "machine_certified"
    elif answer.strip().startswith("わかりません"):
        state = "abstained"
    else:
        state = "answered_unverified"
    return {
        "state": state,
        "route": route,
        "structured_status": (
            str(structured_status) if isinstance(structured_status, str) else None
        ),
        "structured_reason": (
            str(structured_reason) if isinstance(structured_reason, str) else None
        ),
        "output_validation_status": validation_status,
        "answer_sha256": sha256_text(answer),
    }


def classify_source_audit(delta: Mapping[str, Any] | None) -> dict[str, Any]:
    if delta is None:
        return {
            "state": "not_manually_audited",
            "audited": False,
            "entry_sha256": None,
        }
    return {
        "state": str(delta["selection"]),
        "audited": True,
        "entry_sha256": sha256_text(canonical_json(delta)),
    }


def _empty_current(error: str) -> dict[str, Any]:
    return {
        "state": "error",
        "graph_plan_version": GRAPH_PLAN_VERSION,
        "graph_strict_status": "error",
        "graph_strict_reasons": ["graph_execution_error"],
        "advisory_usable": False,
        "fallback_used": False,
        "branch_count": 0,
        "candidate_version": CANDIDATE_VERSION,
        "decision_status": "not_run",
        "decision_reason": "graph_execution_error",
        "graph_rule_version": None,
        "rule_id": None,
        "graph_contract_id": None,
        "contract_rebuild_valid": None,
        "output_validation_status": "not_run",
        "violations": [],
        "answer_sha256": None,
        "source_paths": [],
        "source_sha256": None,
        "operation_count": None,
        "error": error[:20000],
    }


def execute_current(
    question_id: str,
    question: str,
    engine: Any,
    *,
    graph_builder: Callable[..., Any] = build_graph_plan,
    answer_validator: Callable[[str, Any], Sequence[str]] = validate_graph_answer,
    contract_builder: Callable[[str], Mapping[str, Any] | None] = graph_contract_for_question,
    contract_validator: Callable[[str, Mapping[str, Any]], bool] = validate_graph_contract,
) -> dict[str, Any]:
    try:
        plan = graph_builder(question_id, question, fast_advisory=True)
        decision = engine.decide_from_graph(question_id, question, plan)
        contract = contract_builder(question)
        contract_valid = (
            bool(contract_validator(question, contract)) if contract is not None else None
        )
        rule_version = (
            str(contract.get("graph_rule_version"))
            if isinstance(contract, Mapping) and contract.get("graph_rule_version")
            else None
        )
        rule_id = (
            str(contract.get("rule_id"))
            if isinstance(contract, Mapping) and contract.get("rule_id")
            else None
        )
        contract_id = (
            str(contract.get("graph_contract_id"))
            if isinstance(contract, Mapping) and contract.get("graph_contract_id")
            else None
        )
        graph_reasons = tuple(str(value) for value in plan.strict_reasons)
        base = {
            "graph_plan_version": GRAPH_PLAN_VERSION,
            "graph_strict_status": str(plan.strict_status),
            "graph_strict_reasons": list(dict.fromkeys(graph_reasons)),
            "advisory_usable": bool(plan.advisory_usable),
            "fallback_used": bool(plan.fallback_used),
            "branch_count": len(plan.retrieval_queries),
            "candidate_version": CANDIDATE_VERSION,
            "decision_status": str(decision.status),
            "decision_reason": str(decision.reason),
            "graph_rule_version": rule_version,
            "rule_id": rule_id,
            "graph_contract_id": contract_id,
            "contract_rebuild_valid": contract_valid,
        }
        result = decision.result
        if decision.status == "resolved" and result is not None:
            violations = tuple(str(value) for value in answer_validator(result.answer, plan))
            if contract is not None and contract_valid is not True:
                violations = (*violations, "graph_contract_rebuild_failed")
            if not violations:
                return {
                    "state": "certified",
                    **base,
                    "output_validation_status": "pass",
                    "violations": [],
                    "answer_sha256": sha256_text(result.answer),
                    "source_paths": sorted(
                        {
                            unicodedata.normalize("NFC", str(value))
                            for value in result.source_paths
                        }
                    ),
                    "source_sha256": str(result.source_sha256),
                    "operation_count": int(result.operation_count),
                    "error": None,
                }
            return {
                "state": "error",
                **base,
                "output_validation_status": "failed",
                "violations": list(dict.fromkeys(violations)),
                "answer_sha256": None,
                "source_paths": [],
                "source_sha256": None,
                "operation_count": None,
                "error": "output contract validation failed",
            }
        if decision.status == "error":
            return {
                "state": "error",
                **base,
                "output_validation_status": "not_run",
                "violations": [],
                "answer_sha256": None,
                "source_paths": [],
                "source_sha256": None,
                "operation_count": None,
                "error": f"structured candidate error: {decision.reason}"[:20000],
            }
        if plan.advisory_usable and plan.retrieval_queries and not plan.fallback_used:
            return {
                "state": "unproven",
                **base,
                "output_validation_status": "not_run",
                "violations": [],
                "answer_sha256": None,
                "source_paths": [],
                "source_sha256": None,
                "operation_count": None,
                "error": None,
            }
        return {
            "state": "error",
            **base,
            "output_validation_status": "not_run",
            "violations": [],
            "answer_sha256": None,
            "source_paths": [],
            "source_sha256": None,
            "operation_count": None,
            "error": "graph plan has no executable advisory branch",
        }
    except Exception as exc:
        return _empty_current(f"{type(exc).__name__}: {str(exc)[:19000]}")


def source_locators_from_baseline(row: Mapping[str, Any]) -> tuple[str, ...]:
    raw = row.get("参照資料")
    if not isinstance(raw, list):
        return ()
    return tuple(
        unicodedata.normalize("NFC", value)
        for value in raw
        if isinstance(value, str) and value.strip()
    )


def _extensions_in_text(value: str) -> set[str]:
    return {
        f".{match.group(1)}"
        for match in re.finditer(
            r"\.([a-z0-9]{1,16})(?=$|[^a-z0-9])", value.casefold()
        )
        if f".{match.group(1)}" in KNOWN_EXTENSIONS
    }


def source_extensions(question: str, locators: Iterable[str]) -> list[str]:
    observed = _extensions_in_text(question)
    for locator in locators:
        observed.update(_extensions_in_text(locator))
    return sorted(observed)


def _tagging_extensions(question: str, locators: Sequence[str]) -> set[str]:
    """Prefer question-declared formats; accept only a dominant locator hint.

    Fourth-run locators are retrieval candidates, not certified evidence.  A
    noisy top-k union must therefore not turn every question into an Office or
    PDF question.  When no extension is explicit, use a locator extension only
    if it accounts for at least 60% of extension-bearing locators.
    """

    explicit = _extensions_in_text(question)
    if explicit:
        return explicit
    observed: list[str] = []
    for locator in locators:
        extensions = sorted(_extensions_in_text(locator))
        if len(extensions) == 1:
            observed.append(extensions[0])
    if not observed:
        return set()
    counts = Counter(observed)
    extension, count = counts.most_common(1)[0]
    if count / len(observed) >= 0.60:
        return {extension}
    return set()


def derive_capabilities(
    question: str,
    locators: Sequence[str],
    current_state: str,
) -> dict[str, Any]:
    normalized = unicodedata.normalize("NFKC", question).casefold()
    extensions = source_extensions(question, locators)
    routing_extensions = _tagging_extensions(question, locators)
    explicit_extensions = _extensions_in_text(question)
    tags: set[str] = set()

    office_extensions = {".docx", ".pptx", ".xlsx"}
    structured_extensions = {".csv", ".json", ".tsv", ".xlsx"}
    if office_extensions.intersection(routing_extensions):
        tags.add("office_native_structure")
    if structured_extensions.intersection(routing_extensions) or re.search(
        r"(?:分析データ|顧客データ|train(?:\.xlsx|\.csv)?|行数|カラム|セル|"
        r"シート|特徴量|係数|相関|metrics\.json)",
        normalized,
    ):
        tags.add("structured_data")
    if ".pdf" in routing_extensions or "pdf" in normalized:
        tags.add("pdf_layout_or_table")
    if re.search(
        r"(?:ハイライト|太字|下線|イタリック|赤字|コメント|色|bold|red)",
        normalized,
    ):
        tags.add("style_or_annotation")
    if re.search(r"(?:ページ|右側|向かい|座席|配置|レイアウト|物理スライド)", normalized):
        tags.add("page_or_layout")
    if re.search(
        r"(?:グラフ|ヒストグラム|可視化|折れ線|y軸|x軸|目盛り|プロット)",
        normalized,
    ):
        tags.add("graph_value_recovery")
        tags.add("page_or_layout")
    if re.search(
        r"(?:\.ipynb|分析コード|実装設定|設定ファイル|コード上|実行時|one-hot encoding)",
        normalized,
    ):
        tags.add("notebook_or_code_execution")
    if re.search(r"(?:右側|左側|向かい|隣|座席|位置関係)", normalized):
        tags.add("spatial_relation")

    version_or_state = bool(
        re.search(
            r"(?:old|旧版|最新版|更新|修正|変更|差分|未着手から完了|時点|最初の.+最後の)",
            normalized,
        )
    )
    if version_or_state:
        tags.add("version_or_state_diff")
    artifact_terms = {
        term
        for term in (
            "提案書",
            "契約書",
            "中間報告",
            "最終報告",
            "会議録",
            "スケジュール",
            "社内管理",
            "分析コード",
            "train.xlsx",
        )
        if term.casefold() in normalized
    }
    explicit_cross_scope = bool(
        re.search(
            r"(?:各案件|全案件|すべての案件|完了案件|案件のうち|pp・契約書・plan・fr|"
            r"提案時.+(?:最終|確定)|(?:中間|最終)報告.+(?:最終|分析出力)|"
            r"会議録.+報告|m0?1.+m0?2|m0?2.+m0?3)",
            normalized,
        )
    )
    if version_or_state or len(artifact_terms) >= 2 or explicit_cross_scope:
        tags.add("multi_document_join")
    if re.search(
        r"(?:計算|算出|合計|差額|差を|何倍|割合|上昇率|平均|相関|最大|最小|"
        r"カウント|件数|行数|工数|金額|小数第|四捨五入|切り上げ|f1スコア|accuracy)",
        normalized,
    ):
        tags.add("deterministic_calculation")
    if re.search(
        r"(?:抽出|挙げ|答えて|教えて|記載|内容|規定|役割|項目|条件|変更|未達成)",
        normalized,
    ):
        tags.add("semantic_text_extraction")
    if re.search(r"(?:主略称|案件略称|社内用語|フルネーム|内線番号|ext)", normalized):
        tags.add("entity_or_alias_resolution")
    if re.search(
        r"(?:20\d{2}[-年./]|契約期間|支払月|第\d+週|開始日|終了日|何年何月|スケジュール)",
        normalized,
    ):
        tags.add("temporal_filtering")
    if re.search(
        r"(?:表|sheet\d*|シート|セル|ピボット|行数|行の|行を|各行|列名|列の|"
        r"タスクid|アクションid|マイルストーンid)",
        normalized,
    ):
        tags.add("table_structure")
    if re.search(r"(?:すべて|全て|全部|合計|いくつ|何人|何件|列挙)", normalized):
        tags.add("complete_enumeration")
    if not tags:
        tags.add("semantic_text_extraction")

    if current_state == "certified":
        primary_gap = "none_currently_certified"
    elif current_state == "error":
        primary_gap = "current_execution_error"
    elif "spatial_relation" in tags:
        primary_gap = "spatial_grounding"
    elif "graph_value_recovery" in tags:
        primary_gap = "chart_to_table"
    elif "notebook_or_code_execution" in tags:
        primary_gap = "code_execution_semantics"
    elif "multi_document_join" in tags:
        primary_gap = "multi_document_reasoning"
    elif "style_or_annotation" in tags:
        primary_gap = "office_structure_extraction"
    elif "pdf_layout_or_table" in tags and (
        ".pdf" in explicit_extensions or "page_or_layout" in tags
    ):
        primary_gap = "pdf_layout_understanding"
    elif "structured_data" in tags:
        primary_gap = "structured_deterministic_execution"
    elif "office_native_structure" in tags and "page_or_layout" in tags:
        primary_gap = "office_structure_extraction"
    else:
        primary_gap = "semantic_evidence_reasoning"
    ordered_tags = [tag for tag in CAPABILITY_TAG_ORDER if tag in tags]
    return {
        "tags": ordered_tags,
        "primary_gap": primary_gap,
        "source_extensions": extensions,
        "derivation": "generic_question_text_and_source_locators_v0.1",
    }


def component_statuses(
    capabilities: Mapping[str, Any],
    current_state: str,
) -> dict[str, str]:
    tags = set(capabilities["tags"])
    if current_state == "certified":
        return {
            "native_parser": "certified_current",
            "apple_vision": "not_primary",
            "paddleocr": "not_primary",
            "ndlocr_lite": "not_primary",
            "tesseract": "not_primary",
            "docling": "not_primary",
        }
    result = {name: "not_primary" for name in COMPONENT_NAMES}
    native_relevant = tags.intersection(
        {
            "multi_document_join",
            "version_or_state_diff",
            "office_native_structure",
            "structured_data",
            "deterministic_calculation",
            "notebook_or_code_execution",
            "semantic_text_extraction",
            "entity_or_alias_resolution",
            "temporal_filtering",
            "table_structure",
        }
    )
    if native_relevant:
        result["native_parser"] = "applicable_not_e2e_tested"
    visual_relevant = tags.intersection(
        {
            "style_or_annotation",
            "page_or_layout",
            "pdf_layout_or_table",
            "graph_value_recovery",
            "spatial_relation",
        }
    )
    if visual_relevant:
        for name in ("apple_vision", "paddleocr", "ndlocr_lite", "tesseract"):
            result[name] = "applicable_not_e2e_tested"
    docling_relevant = tags.intersection(
        {
            "office_native_structure",
            "page_or_layout",
            "pdf_layout_or_table",
            "graph_value_recovery",
            "table_structure",
        }
    )
    if docling_relevant:
        result["docling"] = "applicable_not_e2e_tested"
    return result


def load_row_validator() -> jsonschema.Draft202012Validator:
    schema = load_json(SCHEMA_PATH, "question capability matrix schema")
    jsonschema.Draft202012Validator.check_schema(schema)
    return jsonschema.Draft202012Validator(schema)


def validate_rows(rows: Sequence[Mapping[str, Any]]) -> None:
    validator = load_row_validator()
    seen_ids: set[str] = set()
    for position, row in enumerate(rows, 1):
        errors = sorted(validator.iter_errors(row), key=lambda item: list(item.path))
        if errors:
            rendered = "; ".join(
                f"{'/'.join(str(part) for part in error.path) or '<root>'}: {error.message}"
                for error in errors[:8]
            )
            raise ValueError(f"matrix row {position} fails schema: {rendered}")
        question_id = str(row["question_id"])
        if question_id in seen_ids:
            raise ValueError(f"duplicate matrix question_id: {question_id}")
        seen_ids.add(question_id)


def build_rows(
    questions: Sequence[Mapping[str, str]],
    fourth_rows: Mapping[str, Mapping[str, Any]],
    audit_rows: Mapping[str, Mapping[str, Any]],
    engine: Any,
    provenance: Mapping[str, Any],
    *,
    current_executor: Callable[[str, str, Any], dict[str, Any]] = execute_current,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for question in questions:
        question_id = question["index"]
        question_text = question["question"]
        baseline_row = fourth_rows[question_id]
        baseline = classify_baseline(baseline_row)
        source_audit = classify_source_audit(audit_rows.get(question_id))
        current = current_executor(question_id, question_text, engine)
        locators = (
            *source_locators_from_baseline(baseline_row),
            *current["source_paths"],
        )
        capabilities = derive_capabilities(question_text, locators, current["state"])
        components = component_statuses(capabilities, current["state"])
        question_digest = sha256_text(question_text)
        row_core = {
            "question_id": question_id,
            "question_sha256": question_digest,
            "baseline_state": baseline["state"],
            "audit_state": source_audit["state"],
            "current_state": current["state"],
            "current_answer_sha256": current["answer_sha256"],
            "current_rule_id": current["rule_id"],
            "provenance": provenance,
        }
        rows.append(
            {
                "schema_version": SCHEMA_VERSION,
                "record_type": "question_capability_matrix_row",
                "matrix_row_id": "qcm_" + sha256_text(canonical_json(row_core))[:24],
                "question_id": question_id,
                "question": question_text,
                "question_sha256": question_digest,
                "baseline": baseline,
                "v16_source_audit": source_audit,
                "current": current,
                "capabilities": capabilities,
                "components": components,
                "provenance": dict(provenance),
            }
        )
    validate_rows(rows)
    return rows


def _counts(values: Iterable[str]) -> dict[str, int]:
    return dict(sorted(Counter(values).items()))


def build_summary(
    rows: Sequence[Mapping[str, Any]],
    *,
    input_files: Mapping[str, Mapping[str, Any]],
    source_root: Path,
    inventory: Mapping[str, Any],
    artifact_payloads: Mapping[str, bytes],
) -> dict[str, Any]:
    current_ids: dict[str, list[str]] = {}
    for state in ("certified", "unproven", "error"):
        current_ids[state] = [
            str(row["question_id"])
            for row in rows
            if row["current"]["state"] == state
        ]
    component_counts = {
        component: _counts(row["components"][component] for row in rows)
        for component in COMPONENT_NAMES
    }
    build_core = {
        "builder_version": BUILDER_VERSION,
        "schema_version": SCHEMA_VERSION,
        "input_sha256": {
            role: value["sha256"] for role, value in sorted(input_files.items())
        },
        "source_inventory_sha256": inventory["sha256"],
        "question_count": len(rows),
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "record_type": "question_capability_coverage_summary",
        "builder": "build_question_capability_matrix.py",
        "builder_version": BUILDER_VERSION,
        "build_signature_sha256": sha256_text(canonical_json(build_core)),
        "versions": {
            "graph_plan": GRAPH_PLAN_VERSION,
            "structured_candidate": CANDIDATE_VERSION,
            "extended_graph_rule": GRAPH_RULE_VERSION,
            "capability_derivation": "generic_question_text_and_source_locators_v0.1",
        },
        "inputs": {
            **{role: dict(value) for role, value in sorted(input_files.items())},
            "source_root": {
                "path": reported_path(source_root),
                "file_count": inventory["file_count"],
                "inventory_sha256": inventory["sha256"],
            },
        },
        "provenance_policy": {
            "gold_used": False,
            "validation_answers_used": False,
            "prediction_files_used": False,
            "past_answers_used_as_evidence": False,
            "fourth_run_answer_text_usage": "answered_vs_abstained_classification_and_hash_only",
            "v16_answer_text_usage": "audit_entry_hash_only",
            "capability_tags_use": "question_text_and_source_locators_only",
            "question_id_specific_capability_overrides_used": False,
        },
        "question_count": len(rows),
        "counts": {
            "baseline": _counts(row["baseline"]["state"] for row in rows),
            "v16_source_audit": _counts(
                row["v16_source_audit"]["state"] for row in rows
            ),
            "current": _counts(row["current"]["state"] for row in rows),
            "graph_strict": _counts(
                row["current"]["graph_strict_status"] for row in rows
            ),
            "primary_gap": _counts(
                row["capabilities"]["primary_gap"] for row in rows
            ),
            "capability_tags": {
                tag: sum(tag in row["capabilities"]["tags"] for row in rows)
                for tag in CAPABILITY_TAG_ORDER
            },
            "components": component_counts,
        },
        "question_ids_by_current_state": current_ids,
        "artifacts": {
            role: {
                "path": OUTPUT_NAMES[role],
                "sha256": sha256_bytes(payload),
            }
            for role, payload in sorted(artifact_payloads.items())
        },
        "limitations": [
            "Current certified means source/graph/output-contract certified, not leaderboard-confirmed correct.",
            "Answered-unverified and unproven rows must not be counted as solved.",
            "OCR and Docling component applicability is not end-to-end question accuracy.",
            "Capability tags are generic lexical/source-locator routing aids, not answer-derived labels.",
        ],
    }


def render_jsonl(rows: Sequence[Mapping[str, Any]]) -> bytes:
    text = "".join(canonical_json(row) + "\n" for row in rows)
    return text.encode("utf-8")


def render_csv(rows: Sequence[Mapping[str, Any]]) -> bytes:
    stream = io.StringIO(newline="")
    fields = [
        "question_id",
        "question",
        "baseline_state",
        "v16_source_audit_state",
        "current_state",
        "graph_strict_status",
        "rule_id",
        "primary_gap",
        "capability_tags",
        "source_extensions",
        *COMPONENT_NAMES,
    ]
    writer = csv.DictWriter(stream, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow(
            {
                "question_id": row["question_id"],
                "question": row["question"],
                "baseline_state": row["baseline"]["state"],
                "v16_source_audit_state": row["v16_source_audit"]["state"],
                "current_state": row["current"]["state"],
                "graph_strict_status": row["current"]["graph_strict_status"],
                "rule_id": row["current"]["rule_id"] or "",
                "primary_gap": row["capabilities"]["primary_gap"],
                "capability_tags": ";".join(row["capabilities"]["tags"]),
                "source_extensions": ";".join(
                    row["capabilities"]["source_extensions"]
                ),
                **row["components"],
            }
        )
    return stream.getvalue().encode("utf-8")


def _md(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\n", "<br>")


def render_markdown(rows: Sequence[Mapping[str, Any]]) -> bytes:
    baseline_counts = _counts(row["baseline"]["state"] for row in rows)
    current_counts = _counts(row["current"]["state"] for row in rows)
    lines = [
        "# 100問 capability matrix v0.1",
        "",
        "正解・questions_valid・gold・predictionsを使わず、第4回ログ、v16 source audit、原本、現行グラフ／構造化回答器だけを比較した。",
        "",
        f"- questions: {len(rows)}",
        f"- baseline: `{canonical_json(baseline_counts)}`",
        f"- current: `{canonical_json(current_counts)}`",
        "- `certified`: 原本、演算グラフ、出力契約を機械検証済み。leaderboard正解確認ではない。",
        "- OCR/Doclingの`applicable_not_e2e_tested`は部品候補であり、その問題を解けたという意味ではない。",
        "",
        "| ID | question | baseline | v16 audit | current | strict | rule | primary gap | tags | native | Apple | Paddle | NDLOCR | Tesseract | Docling |",
        "|---:|---|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for row in rows:
        values = [
            row["question_id"],
            row["question"],
            row["baseline"]["state"],
            row["v16_source_audit"]["state"],
            row["current"]["state"],
            row["current"]["graph_strict_status"],
            row["current"]["rule_id"] or "-",
            row["capabilities"]["primary_gap"],
            ", ".join(row["capabilities"]["tags"]),
            row["components"]["native_parser"],
            row["components"]["apple_vision"],
            row["components"]["paddleocr"],
            row["components"]["ndlocr_lite"],
            row["components"]["tesseract"],
            row["components"]["docling"],
        ]
        lines.append("| " + " | ".join(_md(value) for value in values) + " |")
    lines.append("")
    return "\n".join(lines).encode("utf-8")


def _atomic_write(path: Path, payload: bytes, *, overwrite: bool) -> None:
    if (path.exists() or path.is_symlink()) and not overwrite:
        raise ValueError(f"refusing to overwrite output: {path}")
    descriptor, name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def write_artifacts(
    output_dir: Path,
    rows: Sequence[Mapping[str, Any]],
    *,
    input_files: Mapping[str, Mapping[str, Any]],
    source_root: Path,
    inventory: Mapping[str, Any],
    overwrite: bool,
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    payloads = {
        "jsonl": render_jsonl(rows),
        "csv": render_csv(rows),
        "markdown": render_markdown(rows),
    }
    summary = build_summary(
        rows,
        input_files=input_files,
        source_root=source_root,
        inventory=inventory,
        artifact_payloads=payloads,
    )
    payloads["summary"] = (
        json.dumps(
            summary,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")
    paths = {role: output_dir / name for role, name in OUTPUT_NAMES.items()}
    if not overwrite:
        existing = [str(path) for path in paths.values() if path.exists() or path.is_symlink()]
        if existing:
            raise ValueError("refusing to overwrite outputs: " + ", ".join(existing))
    for role in ("jsonl", "csv", "markdown", "summary"):
        _atomic_write(paths[role], payloads[role], overwrite=overwrite)
    return summary


def build_matrix(
    questions_path: Path,
    fourth_run_log_path: Path,
    v16_source_audit_path: Path,
    source_root_path: Path,
    output_dir: Path,
    *,
    overwrite: bool = False,
    expected_question_count: int = 100,
    engine_factory: Callable[[Path, Any], Any] = StructuredCandidateEngine,
    current_executor: Callable[[str, str, Any], dict[str, Any]] = execute_current,
) -> dict[str, Any]:
    questions_path = reject_forbidden_input(questions_path, "questions")
    fourth_run_log_path = reject_forbidden_input(
        fourth_run_log_path, "fourth run log"
    )
    v16_source_audit_path = reject_forbidden_input(
        v16_source_audit_path, "v16 source audit"
    )
    input_paths = [questions_path, fourth_run_log_path, v16_source_audit_path]
    if len(set(input_paths)) != len(input_paths):
        raise ValueError("input file roles must reference distinct files")
    source_root = validate_source_root(source_root_path)
    if output_dir.is_symlink():
        raise ValueError("output directory must not be a symlink")
    output_dir_resolved = output_dir.resolve()
    if output_dir_resolved == source_root or _is_within(output_dir_resolved, source_root):
        raise ValueError("output directory must not be inside the source root")

    questions = read_questions(
        questions_path,
        expected_question_count=expected_question_count,
    )
    questions_digest = sha256_file(questions_path)
    fourth_rows, fourth_run = load_fourth_run(fourth_run_log_path, questions)
    audit_rows, audit = load_v16_source_audit(
        v16_source_audit_path,
        questions,
        questions_digest,
    )
    inventory = source_inventory(source_root)
    glossary = build_glossary(source_root)
    engine = engine_factory(source_root, glossary)
    input_files = {
        "questions_test": {
            "path": reported_path(questions_path),
            "sha256": questions_digest,
        },
        "fourth_run_log": {
            "path": reported_path(fourth_run_log_path),
            "sha256": sha256_file(fourth_run_log_path),
            "model": fourth_run.get("モデル"),
            "executed_at": fourth_run.get("実行日時"),
        },
        "v16_source_audit": {
            "path": reported_path(v16_source_audit_path),
            "sha256": sha256_file(v16_source_audit_path),
            "schema_version": audit.get("schema_version"),
            "selection_policy": audit.get("selection_policy"),
        },
    }
    row_provenance = {
        "builder": "build_question_capability_matrix.py",
        "builder_version": BUILDER_VERSION,
        "questions_sha256": questions_digest,
        "fourth_run_log_sha256": input_files["fourth_run_log"]["sha256"],
        "v16_source_audit_sha256": input_files["v16_source_audit"]["sha256"],
        "source_inventory_sha256": inventory["sha256"],
        "gold_used": False,
        "validation_answers_used": False,
        "prediction_files_used": False,
        "past_answers_used_as_evidence": False,
        "question_id_specific_capability_overrides_used": False,
    }
    rows = build_rows(
        questions,
        fourth_rows,
        audit_rows,
        engine,
        row_provenance,
        current_executor=current_executor,
    )
    return write_artifacts(
        output_dir_resolved,
        rows,
        input_files=input_files,
        source_root=source_root,
        inventory=inventory,
        overwrite=overwrite,
    )


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("--questions", required=True, type=Path)
    value.add_argument("--fourth-run-log", required=True, type=Path)
    value.add_argument("--v16-source-audit", required=True, type=Path)
    value.add_argument("--source-root", required=True, type=Path)
    value.add_argument("--output-dir", required=True, type=Path)
    value.add_argument("--overwrite", action="store_true")
    return value


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        summary = build_matrix(
            args.questions,
            args.fourth_run_log,
            args.v16_source_audit,
            args.source_root,
            args.output_dir,
            overwrite=args.overwrite,
        )
    except (OSError, UnicodeDecodeError, ValueError, jsonschema.SchemaError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    current = summary["counts"]["current"]
    print(
        "OK: "
        f"questions={summary['question_count']} "
        f"certified={current.get('certified', 0)} "
        f"unproven={current.get('unproven', 0)} "
        f"error={current.get('error', 0)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
