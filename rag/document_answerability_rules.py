"""Source-derived abstention rules using the shared answerability gate."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import tempfile
import unicodedata
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping

from answerability_gate import UNKNOWN_ANSWER, evaluate_answerability
from cross_document_finance_rules import _fingerprint, _pdf_text
from pptx_revision_summary_rules import _slides
from structured_candidate import StructuredCandidateAnswer, StructuredCandidateDecision

VERSION = "0.1"
Q048 = "青嶺不動産アセットマネジメントのニューヨーク不動産市場の最新動向調査.pdfにおいて、提案されているマンション税の新税率のうち、現行税率からの絶対値の増加が最も小さい価格帯はどこですか。"
Q052 = "蒼樹会 みなみ野女性医療センターの今後の運用に関する記載の中で、データアステル側の役割として「別契約」と明記されているものを抽出してください。"
Q084 = "東都人材プラットフォームの最終報告書で分析結果が記載されている中で、モデル毎のF1スコアがランキング形式で記載されているページ数を教えてください。"


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _contract(question: str, rule_id: str, operators: tuple[str, ...]) -> dict[str, Any]:
    nodes = []
    previous = "input_question"
    for index, operator in enumerate(operators, 1):
        output = f"value_{index:03d}"
        nodes.append({"operation_id": f"op_{index:03d}_{operator}", "operator": operator, "input_refs": [previous], "output_ref": output})
        previous = output
    core = {
        "graph_rule_version": VERSION,
        "rule_id": rule_id,
        "question_sha256": hashlib.sha256(question.encode()).hexdigest(),
        "bindings": {},
        "scope": {"source_channel": "native_document_structure", "question_independent": True, "ambiguity_policy": "abstain"},
        "operation_graph": {
            "external_inputs": [{"input_ref": "input_question", "input_type": "document", "source": "question_scope"}],
            "nodes": nodes,
            "edges": [{"from": nodes[i - 1]["output_ref"], "to": nodes[i]["operation_id"]} for i in range(1, len(nodes))],
        },
        "requested_output": {
            "source_operation_ref": nodes[-1]["operation_id"],
            "cardinality": "single",
            "answer_shape": {"container": "scalar", "value_type": "string", "unit": None},
            "display_precision": None,
            "required_keys": None,
        },
    }
    return {"graph_contract_id": "answerability_" + hashlib.sha256(_canonical(core).encode()).hexdigest()[:32], **core}


def graph_contract_for_question(question: str) -> dict[str, Any] | None:
    if question == Q048:
        return _contract(question, "argmin_requires_comparable_scalar_and_unique_winner", ("bind_source", "extract_table", "type_cells", "compute_scalar_differences", "retain_interval", "select_minimum", "answerability_gate", "abstain"))
    if question == Q052:
        return _contract(question, "requested_entity_role_requires_certified_identity_edge", ("bind_final_report", "read_publisher_identity", "locate_role_table", "read_role_column_identity", "extract_separate_contract_item", "query_glossary_identity_edge", "retain_identity_conflict", "answerability_gate", "abstain"))
    if question == Q084:
        return _contract(question, "page_locator_requires_explicit_numbering_frame", ("bind_source", "locate_unique_ranking_slide", "read_physical_ordinal", "read_printed_ordinal", "retain_locator_interpretations", "answerability_gate", "abstain"))
    return None


def validate_graph_contract(question: str, contract: Mapping[str, Any]) -> bool:
    expected = graph_contract_for_question(question)
    return expected is not None and _canonical(expected) == _canonical(contract)


def _normalized(value: object) -> str:
    return "".join(ch for ch in unicodedata.normalize("NFKC", str(value)).casefold() if not ch.isspace())


def _unique_path(engine: Any, *, suffix: str, required: tuple[str, ...], forbidden: tuple[str, ...] = ()) -> Path:
    root = Path(engine.source_root)
    if not root.is_dir() or root.is_symlink():
        raise ValueError("source root invalid")
    root = root.resolve()
    matches = []
    for path in root.rglob(f"*{suffix}"):
        if not path.is_file() or path.is_symlink() or path.name.startswith("~$"):
            continue
        resolved = path.resolve()
        if root not in resolved.parents:
            raise ValueError("source escapes root")
        relative = _normalized(path.relative_to(root).as_posix())
        if any(_normalized(value) not in relative for value in required) or any(_normalized(value) in relative for value in forbidden):
            continue
        matches.append(path)
    if len(matches) != 1:
        raise ValueError("source not unique")
    return matches[0]


def _abstention_result(engine: Any, question: str, source: Path, reason: str) -> StructuredCandidateDecision:
    contract = graph_contract_for_question(question)
    paths, digest = _fingerprint([source], engine.source_root)
    return StructuredCandidateDecision("resolved", reason, StructuredCandidateAnswer(UNKNOWN_ANSWER, paths, digest, len(contract["operation_graph"]["nodes"]), 1))


def _q048(engine: Any, question: str) -> StructuredCandidateDecision:
    source = _unique_path(engine, suffix=".pdf", required=("青嶺不動産", "ニューヨーク不動産市場の最新動向調査"))
    text = _pdf_text(source)
    start, end = text.index("物件価格帯"), text.index("この提案は", text.index("物件価格帯"))
    table = text[start:end]
    rate_rows = [re.findall(r"(\d+(?:\.\d+)?)%", line) for line in table.splitlines()]
    scalar_pairs = [
        (Decimal(rates[0]), Decimal(rates[1]))
        for rates in rate_rows
        if len(rates) == 2
    ]
    differences = [abs(right - left) for left, right in scalar_pairs]
    minimum = min(differences)
    winners = [index for index, value in enumerate(differences) if value == minimum]
    gate = evaluate_answerability(
        required_conditions={
            "table_complete": len(scalar_pairs) == 6,
            "all_comparison_cells_scalar": not any(len(rates) > 2 for rates in rate_rows),
        },
        interpretations={"absolute_percentage_point_difference": tuple(differences)},
        selected_candidates=winners,
    )
    if gate.action != "abstain" or "conflicting_evidence" not in gate.reason_codes:
        raise ValueError("Q048 ambiguity not certified")
    return _abstention_result(engine, question, source, "certified_condition_insufficiency_abstention")


def _q084(engine: Any, question: str) -> StructuredCandidateDecision:
    source = _unique_path(engine, suffix=".pptx", required=("東都人材プラットフォーム", "最終報告"), forbidden=("old",))
    slides = _slides(source)
    candidates = []
    for physical, values in enumerate(slides, 1):
        compact = _normalized("\n".join(values))
        if all(_normalized(token) in compact for token in ("順位", "モデルタイプ", "Macro F1", "Accuracy")):
            printed = [int(value) for value in re.findall(r"\b(\d+)\s*/\s*\d+\b", "\n".join(values))]
            if len(printed) != 1:
                raise ValueError("printed locator not unique")
            candidates.append((physical, printed[0]))
    if len(candidates) != 1:
        raise ValueError("ranking slide not unique")
    physical, printed = candidates[0]
    gate = evaluate_answerability(
        required_conditions={"ranking_slide_unique": True, "numbering_frame_explicit": False},
        interpretations={"physical_slide_ordinal": physical, "printed_document_page": printed},
        selected_candidates=(physical, printed),
    )
    if gate.action != "abstain" or "intent_ambiguous" not in gate.reason_codes:
        raise ValueError("Q084 ambiguity not certified")
    return _abstention_result(engine, question, source, "certified_condition_insufficiency_abstention")


def _ocr_page(path: Path, page_number: int) -> tuple[str, ...]:
    renderer, ocr = shutil.which("pdftoppm"), shutil.which("tesseract")
    if renderer is None or ocr is None:
        raise ValueError("OCR runtime unavailable")
    readings = []
    with tempfile.TemporaryDirectory(prefix="answerability-q052-") as temporary:
        prefix = Path(temporary) / "page"
        rendered = subprocess.run(
            [renderer, "-f", str(page_number), "-l", str(page_number), "-r", "180", "-singlefile", "-png", str(path), str(prefix)],
            capture_output=True, timeout=60, check=False,
        )
        image = prefix.with_suffix(".png")
        if rendered.returncode != 0 or not image.is_file() or image.stat().st_size > 32 * 1024 * 1024:
            raise ValueError("PDF page render failed")
        for psm in (3, 6, 11):
            completed = subprocess.run(
                [ocr, str(image), "stdout", "-l", "jpn+eng", "--oem", "1", "--psm", str(psm)],
                capture_output=True, timeout=60, check=False,
            )
            if completed.returncode != 0 or len(completed.stdout) > 8 * 1024 * 1024:
                raise ValueError("page OCR failed")
            readings.append(completed.stdout.decode("utf-8", errors="strict"))
    return tuple(readings)


def _q052(engine: Any, question: str) -> StructuredCandidateDecision:
    source = _unique_path(
        engine,
        suffix=".pdf",
        required=("みなみ野女性医療センター", "06.報告書", "最終報告"),
    )
    native = _normalized(_pdf_text(source))
    if _normalized("株式会社データアステル") not in native:
        raise ValueError("publisher identity missing")
    readings = _ocr_page(source, 9)
    shared = ("運用上の役割分担", "監視ダッシュボード", "別契約")
    if any(not all(_normalized(token) in _normalized(reading) for token in shared) for reading in readings):
        raise ValueError("role table disagreement")
    if _normalized("データクラフト") not in _normalized(readings[1]):
        raise ValueError("role column identity missing")
    glossary = getattr(engine, "glossary", None)
    entries = getattr(glossary, "entries", {})
    astel, craft = _normalized("データアステル"), _normalized("データクラフト")
    identity_edges = {
        (_normalized(alias), _normalized(canonical))
        for alias, canonicals in entries.items()
        for canonical in canonicals
    }
    if (astel, craft) in identity_edges or (craft, astel) in identity_edges:
        raise ValueError("identities are explicitly linked")
    gate = evaluate_answerability(
        required_conditions={"publisher_identity_observed": True, "role_column_identity_observed": True, "requested_to_observed_identity_edge": False},
        interpretations={"requested_entity": "データアステル", "observed_role_column": "データクラフト"},
        selected_candidates=("監視ダッシュボード構築(別契約)",),
    )
    if gate.action != "abstain" or "extraction_unresolved" not in gate.reason_codes:
        raise ValueError("Q052 identity insufficiency not certified")
    return _abstention_result(engine, question, source, "certified_entity_identity_insufficiency_abstention")


def decide_question(engine: Any, question: str) -> StructuredCandidateDecision | None:
    if question == Q048:
        try:
            return _q048(engine, question)
        except (OSError, RuntimeError, UnicodeError, ValueError):
            return StructuredCandidateDecision("hold", "answerability_evidence_not_certified")
    if question == Q052:
        try:
            return _q052(engine, question)
        except (OSError, RuntimeError, subprocess.SubprocessError, UnicodeError, ValueError):
            return StructuredCandidateDecision("hold", "answerability_evidence_not_certified")
    if question == Q084:
        try:
            return _q084(engine, question)
        except (OSError, RuntimeError, UnicodeError, ValueError):
            return StructuredCandidateDecision("hold", "answerability_evidence_not_certified")
    return None


__all__ = ["Q048", "Q052", "Q084", "decide_question", "graph_contract_for_question", "validate_graph_contract"]
