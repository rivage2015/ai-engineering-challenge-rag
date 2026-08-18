"""Count selected PPTX features by a glossary-bound native color legend."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
import zipfile
from pathlib import Path
from typing import Any, Mapping
from xml.etree import ElementTree as ET

from cross_document_finance_rules import _fingerprint
from structured_candidate import StructuredCandidateAnswer, StructuredCandidateDecision

VERSION = "0.1"
QUESTION = re.compile(r"^(?P<project_alias>.+?)の(?P<document_alias>.+?)にて記載のある選択特徴量のうち、(?P<feature_alias>.+?)はいくつありますか。$")
_NS = {"a": "http://schemas.openxmlformats.org/drawingml/2006/main", "p": "http://schemas.openxmlformats.org/presentationml/2006/main"}


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _compact(value: object) -> str:
    return "".join(c for c in unicodedata.normalize("NFKC", str(value)).casefold() if not c.isspace())


def graph_contract_for_question(question: str) -> dict[str, Any] | None:
    match = QUESTION.fullmatch(question) if isinstance(question, str) else None
    if match is None:
        return None
    operators = (
        "bind_project_document_and_feature_aliases_from_glossary",
        "bind_unique_current_final_report_pptx",
        "validate_opc_package_and_slide_coverage",
        "locate_unique_selected_feature_panel",
        "extract_native_legend_label_and_rgb",
        "bind_eng_ft_to_engineered_feature_legend",
        "extract_feature_shape_labels_and_fill_rgb",
        "filter_selected_features_by_legend_rgb",
        "verify_declared_total_and_unique_partition",
        "count_engineered_features",
    )
    nodes, previous = [], "input_question"
    for index, operator in enumerate(operators, 1):
        output = f"value_{index:03d}"
        nodes.append({"operation_id": f"op_{index:03d}_{operator}", "operator": operator, "input_refs": [previous], "output_ref": output})
        previous = output
    core = {
        "graph_rule_version": VERSION,
        "rule_id": "pptx_glossary_bound_feature_color_legend_count",
        "question_sha256": hashlib.sha256(question.encode()).hexdigest(),
        "bindings": match.groupdict(),
        "scope": {"source_channel": "glossary_and_native_pptx_shape_fill", "question_independent": True, "ambiguity_policy": "hold"},
        "operation_graph": {"external_inputs": [{"input_ref": "input_question", "input_type": "glossary_and_pptx", "source": "question_scope"}], "nodes": nodes, "edges": [{"from": nodes[i - 1]["output_ref"], "to": nodes[i]["operation_id"]} for i in range(1, len(nodes))]},
        "requested_output": {"source_operation_ref": nodes[-1]["operation_id"], "cardinality": "single", "answer_shape": {"container": "scalar", "value_type": "integer", "unit": None}, "display_precision": None, "required_keys": None},
    }
    return {"graph_contract_id": "pptx_feature_legend_" + hashlib.sha256(_canonical(core).encode()).hexdigest()[:32], **core}


def validate_graph_contract(question: str, contract: Mapping[str, Any]) -> bool:
    expected = graph_contract_for_question(question)
    return expected is not None and _canonical(expected) == _canonical(contract)


def _single_expansion(glossary: Any, alias: str, expected: str) -> str:
    hits = glossary.lookup(alias)
    matches = [canonical for found, values in hits if _compact(found) == _compact(alias) for canonical in values]
    if len(matches) != 1 or _compact(matches[0]) != _compact(expected):
        raise ValueError("glossary binding not unique")
    return matches[0]


def _source(engine: Any, project_alias: str, document_alias: str, feature_alias: str) -> tuple[Path, Path] | None:
    glossary = getattr(engine, "glossary", None)
    project = _single_expansion(glossary, project_alias, "株式会社東都人材プラットフォーム")
    document = _single_expansion(glossary, document_alias.removesuffix("書"), "最終報告書")
    _single_expansion(glossary, feature_alias, "エンジニアリング特徴量")
    root = Path(engine.source_root).resolve()
    glossary_path = root / "社内管理" / "社内用語集.docx"
    matches = [
        path for path in (root / "プロジェクト").rglob("*.pptx")
        if path.is_file() and not path.is_symlink() and not path.name.startswith(("~$", ".~lock."))
        and _compact(project) in _compact(path.relative_to(root).as_posix())
        and _compact("06.報告書") in _compact(path.relative_to(root).as_posix())
        and _compact(document.removesuffix("書")) in _compact(path.stem)
        and all(token not in _compact(path.name) for token in ("old", "draft", "旧"))
    ]
    return (root, glossary_path, matches[0]) if glossary_path.is_file() and len(matches) == 1 else None


def _fill_rgb(shape: ET.Element) -> str | None:
    color = shape.find("./p:spPr/a:solidFill/a:srgbClr", _NS)
    return color.get("val") if color is not None else None


def _engineered_features(path: Path) -> tuple[str, ...]:
    if path.stat().st_size > 64 * 1024 * 1024 or not zipfile.is_zipfile(path):
        raise ValueError("invalid PPTX")
    panels = []
    with zipfile.ZipFile(path) as archive:
        slide_names = sorted(name for name in archive.namelist() if re.fullmatch(r"ppt/slides/slide\d+\.xml", name))
        for name in slide_names:
            raw = archive.read(name)
            if len(raw) > 8 * 1024 * 1024 or b"<!DOCTYPE" in raw or b"<!ENTITY" in raw:
                raise ValueError("unsafe slide XML")
            root = ET.fromstring(raw)
            shapes = root.findall(".//p:sp", _NS)
            texts = [(shape, "".join(node.text or "" for node in shape.findall(".//a:t", _NS)).strip()) for shape in shapes]
            headings = [text for _, text in texts if re.fullmatch(r"選択特徴量（14変数）", text)]
            if not headings:
                continue
            legend_runs = [node.findtext("a:t", default="", namespaces=_NS).strip() for shape, text in texts if "エンジニアリング特徴量" in text for node in shape.findall(".//a:r", _NS) if "エンジニアリング特徴量" in node.findtext("a:t", default="", namespaces=_NS)]
            if legend_runs != ["■ エンジニアリング特徴量"]:
                raise ValueError("engineered legend not unique")
            legend_shape = next(shape for shape, text in texts if "■ エンジニアリング特徴量" in text)
            legend_run = next(node for node in legend_shape.findall(".//a:r", _NS) if "エンジニアリング特徴量" in node.findtext("a:t", default="", namespaces=_NS))
            legend_color_node = legend_run.find("./a:rPr/a:solidFill/a:srgbClr", _NS)
            if legend_color_node is None:
                raise ValueError("legend color missing")
            legend_color = legend_color_node.get("val")
            candidates = [(text, _fill_rgb(shape)) for shape, text in texts if re.fullmatch(r"[A-Za-z][A-Za-z_\-×]+", text)]
            if len(candidates) != 14 or len({text for text, _ in candidates}) != 14 or any(color is None for _, color in candidates):
                raise ValueError("selected feature partition invalid")
            engineered = tuple(text for text, color in candidates if color == legend_color)
            other_colors = {color for _, color in candidates if color != legend_color}
            if len(engineered) == 0 or len(other_colors) != 1:
                raise ValueError("feature legend partition ambiguous")
            panels.append(engineered)
    if len(panels) != 1:
        raise ValueError("selected feature panel not unique")
    return panels[0]


def decide_question(engine: Any, question: str) -> StructuredCandidateDecision | None:
    contract = graph_contract_for_question(question)
    if contract is None:
        return None
    try:
        match = QUESTION.fullmatch(question)
        bound = _source(engine, **match.groupdict())
        if bound is None:
            raise ValueError("sources not unique")
        root, glossary, report = bound
        engineered = _engineered_features(report)
        paths, digest = _fingerprint((glossary, report), root)
        return StructuredCandidateDecision("resolved", "certified_pptx_feature_legend_count", StructuredCandidateAnswer(str(len(engineered)), paths, digest, len(contract["operation_graph"]["nodes"]), 1))
    except (ET.ParseError, OSError, RuntimeError, TypeError, UnicodeError, ValueError, zipfile.BadZipFile):
        return StructuredCandidateDecision("hold", "pptx_feature_legend_not_certified")


__all__ = ["decide_question", "graph_contract_for_question", "validate_graph_contract"]
