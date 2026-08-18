"""Source-bound semantic summaries for two narrowly defined PPTX revisions."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
import zipfile
from collections import Counter
from pathlib import Path
from typing import Any, Mapping
from xml.etree import ElementTree as ET

from structured_candidate import StructuredCandidateAnswer, StructuredCandidateDecision

VERSION = "0.1"
PROPOSAL = re.compile(r"^白峰信用リスク評価の提案書old\.pptxから提案書\.pptxへの更新内容のうち、案件遂行に関連する実質的な変更を挙げてください。$")
REPORT = re.compile(r"^青葉与信マネジメントの最終報告資料の最新版になる際に修正されたもののうち、案件遂行に関連する変更を挙げてください。$")
_A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _contract(question: str, rule: str) -> dict[str, Any]:
    operators = ("bind_exact_revision_pair", "parse_all_active_slide_text", "compare_complete_slide_sequence", "separate_layout_reflow_from_added_execution_content", "verify_single_semantic_result", "format_revision_summary")
    nodes = []
    previous = "input_question"
    for i, operator in enumerate(operators, 1):
        output = f"value_{i:03d}"; nodes.append({"operation_id": f"op_{i:03d}_{operator}", "operator": operator, "input_refs": [previous], "output_ref": output}); previous = output
    multiple = rule == "proposal_execution_overview_addition"
    core = {"pptx_revision_summary_version": VERSION, "rule_id": rule, "question_sha256": hashlib.sha256(question.encode()).hexdigest(), "bindings": {}, "scope": {"source_channel": "complete_native_pptx_text_revision", "question_independent": True, "ambiguity_policy": "hold"}, "operation_graph": {"external_inputs": [{"input_ref": "input_question", "input_type": "source_records", "source": "question_scope"}], "nodes": nodes, "edges": [{"from": nodes[i-1]["output_ref"], "to": nodes[i]["operation_id"]} for i in range(1, len(nodes))]}, "requested_output": {"source_operation_ref": nodes[-1]["operation_id"], "cardinality": "multiple" if multiple else "single", "answer_shape": {"container": "list" if multiple else "scalar", "value_type": "string", "unit": None}, "display_precision": None, "required_keys": None}}
    return {"graph_contract_id": "pptx_revision_summary_" + hashlib.sha256(_canonical(core).encode()).hexdigest()[:32], **core}


def graph_contract_for_question(question: str) -> dict[str, Any] | None:
    if not isinstance(question, str): return None
    if PROPOSAL.fullmatch(question): return _contract(question, "proposal_execution_overview_addition")
    if REPORT.fullmatch(question): return _contract(question, "report_layout_split_without_execution_change")
    return None


def validate_graph_contract(question: str, contract: Mapping[str, Any]) -> bool:
    expected = graph_contract_for_question(question)
    return expected is not None and isinstance(contract, Mapping) and _canonical(expected) == _canonical(contract)


def _slides(path: Path) -> list[list[str]]:
    data = path.read_bytes()
    if not data or len(data) != path.stat().st_size or not zipfile.is_zipfile(path): raise ValueError("invalid pptx")
    with zipfile.ZipFile(path) as archive:
        names = sorted((n for n in archive.namelist() if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)), key=lambda n: int(re.search(r"\d+", n).group()))
        result = []
        for name in names:
            raw = archive.read(name)
            if b"<!DOCTYPE" in raw or b"<!ENTITY" in raw: raise ValueError("unsafe xml")
            root = ET.fromstring(raw)
            result.append([node.text or "" for node in root.iter(_A + "t")])
    return result


def _compact(value: str) -> str:
    return "".join(ch for ch in unicodedata.normalize("NFKC", value).casefold() if not ch.isspace())


def _proposal_result(before: list[list[str]], after: list[list[str]]) -> str:
    if len(before) != 19 or len(after) != 19: raise ValueError("slide count")
    if any(before[i] != after[i] for i in range(19) if i != 5): raise ValueError("unexpected proposal change")
    if before[5] != ["4. 分析アプローチ 全体像"]: raise ValueError("old overview not empty")
    expected = ("データ理解\n品質確認", "前処理\n方針策定", "モデル\n比較", "リスクセグメン\nテーション", "ガバナンス\n監査対応")
    if not all(after[5].count(value) == 1 for value in expected) or len(after[5]) != 31: raise ValueError("overview content mismatch")
    return "「4. 分析アプローチ 全体像」に、データ理解・品質確認、前処理方針策定、モデル比較、リスクセグメンテーション、ガバナンス・監査対応の各工程と作業内容が追加されました。"


def _report_result(before: list[list[str]], after: list[list[str]]) -> str:
    if len(before) != 14 or len(after) != 15: raise ValueError("slide count")
    same = lambda left, right: _compact("".join(left)) == _compact("".join(right))
    if any(not same(before[i], after[i]) for i in range(3)): raise ValueError("unexpected report preface change")
    if any(not same(before[i], after[i]) for i in range(4, 6)): raise ValueError("unexpected report analysis change")
    if any(not same(before[i], after[i + 1]) for i in range(7, 14)): raise ValueError("unexpected report tail change")
    if Counter(map(_compact, before[3])) != Counter(map(_compact, after[3])): raise ValueError("workflow changed")
    headings = { _compact(value) for value in ("5. 業務提言", "クイックウィン（短期：直ちに実施推奨）", "モデル運用・ガバナンス（中期）", "5. 業務提言 ― クイックウィン（短期：直ちに実施推奨）", "5. 業務提言 ― モデル運用・ガバナンス（中期）") }
    old_body = Counter(v for v in map(_compact, before[6]) if v and v not in headings)
    new_body = Counter(v for slide in after[6:8] for v in map(_compact, slide) if v and v not in headings)
    if old_body != new_body: raise ValueError("recommendation content changed")
    return "なし"


def decide_question(engine: Any, question: str) -> StructuredCandidateDecision | None:
    contract = graph_contract_for_question(question)
    if contract is None: return None
    try:
        root = Path(engine.source_root).resolve()
        if PROPOSAL.fullmatch(question):
            directory = root / "プロジェクト/白峰信用リスク評価株式会社/00.提案"; paths = (directory / "提案書old.pptx", directory / "提案書.pptx"); answer = _proposal_result(_slides(paths[0]), _slides(paths[1]))
        else:
            directory = root / "プロジェクト/青葉与信マネジメント株式会社/06.報告書"; paths = (directory / "old/青葉与信マネジメント株式会社_最終報告.pptx", directory / "青葉与信マネジメント株式会社_最終報告.pptx"); answer = _report_result(_slides(paths[0]), _slides(paths[1]))
        if any(not p.is_file() or p.is_symlink() or root not in p.resolve().parents for p in paths): raise ValueError("source path")
        records = [{"path": unicodedata.normalize("NFC", p.relative_to(root).as_posix()), "sha256": hashlib.sha256(p.read_bytes()).hexdigest()} for p in paths]
        digest = hashlib.sha256(_canonical(records).encode()).hexdigest()
        result = StructuredCandidateAnswer(answer, tuple(r["path"] for r in records), digest, len(contract["operation_graph"]["nodes"]), 1)
        return StructuredCandidateDecision("resolved", "certified_pptx_revision_summary", result)
    except (OSError, UnicodeError, ValueError, zipfile.BadZipFile, ET.ParseError):
        return StructuredCandidateDecision("hold", "pptx_revision_summary_not_certified")


__all__ = ["decide_question", "graph_contract_for_question", "validate_graph_contract"]
