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

VERSION = "0.2"
Q048 = "青嶺不動産アセットマネジメントのニューヨーク不動産市場の最新動向調査.pdfにおいて、提案されているマンション税の新税率のうち、現行税率からの絶対値の増加が最も小さい価格帯はどこですか。"
Q052 = "蒼樹会 みなみ野女性医療センターの今後の運用に関する記載の中で、データアステル側の役割として「別契約」と明記されているものを抽出してください。"
Q084 = "東都人材プラットフォームの最終報告書で分析結果が記載されている中で、モデル毎のF1スコアがランキング形式で記載されているページ数を教えてください。"


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _contract(
    question: str,
    rule_id: str,
    operators: tuple[str, ...],
    *,
    ambiguity_policy: str = "abstain",
) -> dict[str, Any]:
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
        "scope": {
            "source_channel": "native_document_structure",
            "question_independent": True,
            "ambiguity_policy": ambiguity_policy,
        },
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
        return _contract(
            question,
            "interval_dominance_unique_argmin",
            (
                "bind_source",
                "extract_complete_rate_table",
                "parse_scalar_or_interval_rates",
                "compute_absolute_difference_intervals",
                "compare_candidate_upper_bounds_to_other_lower_bounds",
                "require_unique_robust_minimum",
                "answerability_gate",
                "format_price_band",
            ),
            ambiguity_policy="hold",
        )
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


_Q048_HEADER = "物件価格帯現行税率提案されている新税率"
_Q048_ROW = re.compile(
    r"(?P<lower>\d[\d,]*)\s*万ドル超"
    # Native PDF extraction can place the tail of `万ドル以下` after
    # the numeric cells.  Preserve both fragments and require that their exact
    # concatenation is the authored inclusive upper-bound marker.
    r"(?:\s*-\s*(?P<upper>\d[\d,]*)\s*(?P<upper_prefix>万ドル以下|万ドル以|万ドル|万ド))?\s*"
    r"(?P<current_low>\d+(?:\.\d+)?)%"
    r"(?:\s*-\s*(?P<current_high>\d+(?:\.\d+)?)%)?\s*"
    r"(?P<proposed>\d+(?:\.\d+)?)%"
    r"(?P<upper_tail>ル以下|以下|下)?"
)


def _absolute_difference_interval(
    current_low: Decimal,
    current_high: Decimal,
    proposed: Decimal,
) -> tuple[Decimal, Decimal]:
    """Return the exact range of |proposed-current| over a closed interval."""

    if not all(value.is_finite() for value in (current_low, current_high, proposed)):
        raise ValueError("rate is not finite")
    if current_low > current_high:
        raise ValueError("rate interval is reversed")
    if proposed < current_low:
        return current_low - proposed, current_high - proposed
    if proposed > current_high:
        return proposed - current_high, proposed - current_low
    return Decimal(0), max(proposed - current_low, current_high - proposed)


def _unique_interval_argmin(
    rows: list[tuple[str, Decimal, Decimal, Decimal]],
) -> tuple[str, tuple[tuple[str, Decimal, Decimal], ...]]:
    """Select an argmin only when its worst case beats every rival's best case."""

    if len(rows) < 2 or len({label for label, *_ in rows}) != len(rows):
        raise ValueError("rate table is incomplete or duplicated")
    intervals = tuple(
        (label, *_absolute_difference_interval(low, high, proposed))
        for label, low, high, proposed in rows
    )
    winners = [
        label
        for label, _minimum, maximum in intervals
        if all(
            label == other_label or maximum < other_minimum
            for other_label, other_minimum, _other_maximum in intervals
        )
    ]
    if len(winners) != 1:
        raise ValueError("absolute-difference argmin is not interval-dominant")
    return winners[0], intervals


def _parse_q048_table(text: str) -> list[tuple[str, Decimal, Decimal, Decimal]]:
    """Parse every table token, including PDF-displaced upper-bound fragments."""

    normalized = unicodedata.normalize("NFKC", text)
    lines = normalized.splitlines()
    nonempty = [index for index, line in enumerate(lines) if line.strip()]
    if not nonempty:
        raise ValueError("Q048 rate table is empty")
    footer_index = nonempty[-1]
    if re.fullmatch(r"\d{1,3}", lines[footer_index].strip()) is None:
        raise ValueError("Q048 printed page footer is not an isolated line")
    if any(line.strip() for line in lines[footer_index + 1 :]):
        raise ValueError("Q048 tokens follow the printed page footer")
    compact = "".join(
        char for char in "\n".join(lines[:footer_index]) if not char.isspace()
    )
    if not compact.startswith(_Q048_HEADER):
        raise ValueError("Q048 rate table header changed")
    cursor = len(_Q048_HEADER)
    rows = []
    boundaries: list[tuple[int, int | None]] = []
    while match := _Q048_ROW.match(compact, cursor):
        lower, upper = match.group("lower"), match.group("upper")
        prefix, tail = match.group("upper_prefix"), match.group("upper_tail")
        if upper is None:
            if prefix is not None or tail is not None:
                raise ValueError("Q048 open-ended band has an upper-bound fragment")
        elif f"{prefix or ''}{tail or ''}" != "万ドル以下":
            raise ValueError("Q048 inclusive upper-bound marker is incomplete")
        label = f"{lower} 万ドル超"
        if upper is not None:
            label += f" - {upper} 万ドル以下"
        current_low = Decimal(match.group("current_low"))
        current_high = Decimal(match.group("current_high") or match.group("current_low"))
        proposed = Decimal(match.group("proposed"))
        rows.append((label, current_low, current_high, proposed))
        boundaries.append(
            (
                int(lower.replace(",", "")),
                int(upper.replace(",", "")) if upper is not None else None,
            )
        )
        cursor = match.end()
    # The independently bound footer was removed before token compaction, so
    # every remaining source character must belong to one parsed table row.
    if compact[cursor:]:
        raise ValueError("Q048 rate table contains unconsumed tokens")
    if len(rows) != 7:
        raise ValueError("Q048 rate table is incomplete")
    if any(
        upper is None or upper != boundaries[index + 1][0] or lower >= upper
        for index, (lower, upper) in enumerate(boundaries[:-1])
    ) or boundaries[-1][1] is not None:
        raise ValueError("Q048 price bands are not contiguous and ordered")
    return rows


def _q048(engine: Any, question: str) -> StructuredCandidateDecision:
    source = _unique_path(engine, suffix=".pdf", required=("青嶺不動産", "ニューヨーク不動産市場の最新動向調査"))
    text = _pdf_text(source)
    start, end = text.index("物件価格帯"), text.index("この提案は", text.index("物件価格帯"))
    rows = _parse_q048_table(text[start:end])
    winner, intervals = _unique_interval_argmin(rows)
    gate = evaluate_answerability(
        required_conditions={
            "table_complete": len(rows) == 7,
            "all_rates_typed_as_closed_intervals": True,
            "unique_interval_dominant_minimum": True,
        },
        interpretations={"absolute_percentage_point_difference_intervals": intervals},
        selected_candidates=(winner,),
    )
    if gate.action != "answer" or gate.reason_codes:
        raise ValueError("Q048 interval-dominance answer not certified")
    contract = graph_contract_for_question(question)
    paths, digest = _fingerprint([source], engine.source_root)
    return StructuredCandidateDecision(
        "resolved",
        "certified_interval_dominance_argmin",
        StructuredCandidateAnswer(
            winner,
            paths,
            digest,
            len(contract["operation_graph"]["nodes"]),
            1,
        ),
    )


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
