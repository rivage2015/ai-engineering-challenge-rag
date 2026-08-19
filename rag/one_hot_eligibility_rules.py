"""Resolve threshold-based One-Hot eligibility without conflating later encoders."""

from __future__ import annotations

import csv
import hashlib
import json
import re
import unicodedata
import zipfile
from pathlib import Path
from typing import Any, Mapping, Sequence
from xml.etree import ElementTree as ET

from cross_document_finance_rules import _fingerprint
from structured_candidate import StructuredCandidateAnswer, StructuredCandidateDecision

VERSION = "0.1"
QUESTION = "恒一会のPPで言及されている One-Hot Encoding のカテゴリ数閾値を実装設定から確認したうえで、その条件により One-Hot Encoding の対象となるカテゴリ列をすべて答えてください。"
_NS = {
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "p": "http://schemas.openxmlformats.org/presentationml/2006/main",
}


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _compact(value: object) -> str:
    return "".join(char for char in unicodedata.normalize("NFKC", str(value)).casefold() if not char.isspace())


def graph_contract_for_question(question: str) -> dict[str, Any] | None:
    if question != QUESTION:
        return None
    operators = (
        "bind_project_and_pp_aliases_from_glossary",
        "bind_unique_current_proposal_pptx",
        "extract_one_hot_threshold_relation_from_native_table",
        "bind_unique_runtime_config",
        "verify_one_hot_limit_and_exclusion_boundary",
        "verify_config_limit_flows_to_feature_selection",
        "verify_selected_categorical_columns_flow_to_one_hot_transformer",
        "bind_declared_training_csv_and_target",
        "infer_categorical_columns_from_complete_csv",
        "count_unique_non_null_values_per_categorical_column",
        "filter_below_limit_and_project_threshold_with_columns",
    )
    nodes = []
    previous = "input_question"
    for index, operator in enumerate(operators, 1):
        output = f"value_{index:03d}"
        nodes.append({"operation_id": f"op_{index:03d}_{operator}", "operator": operator, "input_refs": [previous], "output_ref": output})
        previous = output
    core = {
        "graph_rule_version": VERSION,
        "rule_id": "proposal_one_hot_threshold_eligible_categorical_columns",
        "question_sha256": hashlib.sha256(question.encode()).hexdigest(),
        "bindings": {"eligibility_stage": "pre_target_encoding_feature_selection"},
        "scope": {"source_channel": "glossary_pptx_config_code_and_csv", "question_independent": True, "ambiguity_policy": "hold", "exclusion": "later_target_encoding_execution_is_not_the_requested_threshold_condition"},
        "operation_graph": {"external_inputs": [{"input_ref": "input_question", "input_type": "proposal_config_code_csv", "source": "question_scope"}], "nodes": nodes, "edges": [{"from": nodes[i - 1]["output_ref"], "to": nodes[i]["operation_id"]} for i in range(1, len(nodes))]},
        "requested_output": {"source_operation_ref": nodes[-1]["operation_id"], "cardinality": "multiple", "answer_shape": {"container": "list", "value_type": "string", "unit": None}, "display_precision": None, "required_keys": None},
    }
    return {"graph_contract_id": "one_hot_eligibility_" + hashlib.sha256(_canonical(core).encode()).hexdigest()[:32], **core}


def validate_graph_contract(question: str, contract: Mapping[str, Any]) -> bool:
    expected = graph_contract_for_question(question)
    return expected is not None and _canonical(expected) == _canonical(contract)


def _sources(engine: Any) -> tuple[Path, ...]:
    root = Path(engine.source_root).resolve()
    glossary = root / "社内管理" / "社内用語集.docx"
    if not root.is_dir() or root.is_symlink() or not glossary.is_file() or glossary.is_symlink():
        raise ValueError("source root or glossary invalid")
    lookup = getattr(engine, "glossary", None).lookup
    if lookup("恒一会") != [("恒一会", ["医療法人社団 恒一会 かえで総合病院"])] or lookup("PP") != [("PP", ["提案書"])]:
        raise ValueError("glossary bindings changed")
    projects = [path for path in (root / "プロジェクト").iterdir() if path.is_dir() and not path.is_symlink() and _compact(path.name) == _compact("医療法人社団 恒一会 かえで総合病院")]
    if len(projects) != 1:
        raise ValueError("project not unique")
    project = projects[0]
    proposals = [path for path in project.rglob("*.pptx") if path.is_file() and not path.is_symlink() and _compact(path.stem) == _compact("提案書") and "00.提案" in unicodedata.normalize("NFC", path.relative_to(project).as_posix())]
    if len(proposals) != 1:
        raise ValueError("proposal not unique")
    analysis_dirs = [path for path in project.iterdir() if path.is_dir() and unicodedata.normalize("NFC", path.name) == "04.分析"]
    if len(analysis_dirs) != 1:
        raise ValueError("analysis directory not unique")
    base = analysis_dirs[0] / "analysis_project"
    files = (base / "configs/project_config.json", base / "scripts/run_train.py", base / "src/features.py", base / "src/modeling.py", base / "data/train.csv")
    if any(not path.is_file() or path.is_symlink() or root not in path.resolve().parents for path in files):
        raise ValueError("implementation sources invalid")
    return (root, glossary, proposals[0], *files)


def _proposal_relation(path: Path) -> None:
    if not zipfile.is_zipfile(path) or path.stat().st_size > 64 * 1024 * 1024:
        raise ValueError("proposal invalid")
    found = []
    with zipfile.ZipFile(path) as archive:
        slides = [name for name in archive.namelist() if re.fullmatch(r"ppt/slides/slide\d+\.xml", name)]
        for name in slides:
            root = ET.fromstring(archive.read(name))
            # This slide uses aligned text boxes rather than an OOXML table.
            # Preserve the authored shape order and require the semantic row to
            # occur as one consecutive triple; do not infer it from proximity.
            texts = [
                "".join(node.text or "" for node in shape.findall(".//a:t", _NS)).strip()
                for shape in root.findall(".//p:sp", _NS)
            ]
            texts = [text for text in texts if text]
            for index in range(len(texts) - 2):
                if texts[index : index + 2] == ["カテゴリ変換", "One-Hot Encoding"]:
                    found.append((int(re.search(r"\d+", name).group()), texts[index + 2]))
    if found != [(9, "閾値未満のカテゴリ数の場合に適用")]:
        raise ValueError("proposal One-Hot relation not unique")


def _implementation_contract(config_path: Path, run_train: Path, features: Path, modeling: Path) -> tuple[int, str, tuple[str, ...]]:
    config = json.loads(config_path.read_text(encoding="utf-8"))
    plan = config.get("feature_plan")
    if not isinstance(plan, dict) or plan.get("categorical_encoding") != "one_hot" or plan.get("exclude_high_cardinality_at_or_above_limit") is not True:
        raise ValueError("One-Hot feature plan invalid")
    limit = plan.get("categorical_unique_limit")
    if not isinstance(limit, int) or limit < 2 or config.get("categorical_unique_limit_override") != limit:
        raise ValueError("categorical limit inconsistent")
    data_csv, target = config.get("data_csv"), config.get("target_column")
    identifiers = plan.get("identifier_exact_names")
    if data_csv != "data/train.csv" or not isinstance(target, str) or not isinstance(identifiers, list) or not all(isinstance(value, str) for value in identifiers):
        raise ValueError("data or identifier bindings invalid")
    run = re.sub(r"\s+", "", run_train.read_text(encoding="utf-8"))
    feature_code = re.sub(r"\s+", "", features.read_text(encoding="utf-8"))
    model_code = re.sub(r"\s+", "", modeling.read_text(encoding="utf-8"))
    required_run = ('categorical_unique_limit_override=cfg.get("categorical_unique_limit_override")', 'feature_selection_kwargs["categorical_unique_limit"]=int(categorical_unique_limit_override)', 'select_feature_columns(X,**feature_selection_kwargs)')
    required_feature = (
        "unique_count=int(series.dropna().nunique())",
        "ifunique_count>=categorical_unique_limit:",
        '"reason":"high_cardinality_categorical"',
        "categorical_cols=[cforcinX.columnsifcnotinnumeric_cols]",
        '("onehot",OneHotEncoder(handle_unknown="ignore",sparse_output=sparse_output))',
    )
    required_model = ("preprocessor=build_preprocessor(X,sparse_output=model_key!=\"hist_gradient_boosting\")",)
    if any(token not in run for token in required_run) or any(token not in feature_code for token in required_feature) or any(token not in model_code for token in required_model):
        raise ValueError("implementation data flow changed")
    return limit, target, tuple(identifiers)


def _eligible_columns(csv_path: Path, target: str, identifiers: Sequence[str], limit: int) -> tuple[str, ...]:
    with csv_path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None or target not in reader.fieldnames:
            raise ValueError("CSV schema invalid")
        values = {column: set() for column in reader.fieldnames}
        for index, row in enumerate(reader, 1):
            if index > 1_000_000 or set(row) != set(reader.fieldnames):
                raise ValueError("CSV row invalid")
            for column, value in row.items():
                if value != "":
                    values[column].add(value)
    eligible = []
    for column in reader.fieldnames:
        if column == target or column.casefold() in {value.casefold() for value in identifiers}:
            continue
        tokens = values[column]
        numeric = all(re.fullmatch(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?", token) for token in tokens)
        if not numeric and 0 < len(tokens) < limit:
            eligible.append(column)
    if not eligible:
        raise ValueError("no eligible categorical columns")
    return tuple(eligible)


def decide_question(engine: Any, question: str) -> StructuredCandidateDecision | None:
    contract = graph_contract_for_question(question)
    if contract is None:
        return None
    try:
        root, glossary, proposal, config, run_train, features, modeling, csv_path = _sources(engine)
        _proposal_relation(proposal)
        limit, target, identifiers = _implementation_contract(config, run_train, features, modeling)
        eligible = _eligible_columns(csv_path, target, identifiers, limit)
        paths, digest = _fingerprint((glossary, proposal, config, run_train, features, modeling, csv_path), root)
        answer = f"カテゴリ数{limit}未満がOne-Hot Encodingの対象で、該当するカテゴリ列は{'、'.join(eligible)}です。"
        result = StructuredCandidateAnswer(answer, paths, digest, len(contract["operation_graph"]["nodes"]), len(eligible))
        return StructuredCandidateDecision("resolved", "certified_one_hot_threshold_eligibility", result)
    except (csv.Error, ET.ParseError, json.JSONDecodeError, OSError, RuntimeError, TypeError, UnicodeError, ValueError, zipfile.BadZipFile):
        return StructuredCandidateDecision("hold", "one_hot_eligibility_not_certified")


__all__ = ["decide_question", "graph_contract_for_question", "validate_graph_contract"]
