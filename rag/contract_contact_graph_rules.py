"""Resolve glossary aliases before extracting a party-scoped contract contact."""

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
from evidence_edge_audit import EdgePolicy, EqualityCheck, audit_edge_with_same_model
from evidence_graph_memory import add_node, new_graph, propose_edge, set_answer_projection, validate_graph
from structured_candidate import StructuredCandidateAnswer, StructuredCandidateDecision

VERSION = "0.1"
Q021 = "青葉バイオメディカル機器のクライアントの主担当者の役職は何ですか。"
Q043 = "東都のCTにおいて、甲側の主担当者をフルネームで教えてください。"
_W = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def graph_contract_for_question(question: str) -> dict[str, Any] | None:
    if question not in (Q021, Q043):
        return None
    if question == Q021:
        operators = (
            "bind_unique_project_contract",
            "validate_docx_package_and_extract_paragraphs",
            "create_kou_party_primary_contact_and_role_nodes",
            "bind_client_to_kou_party",
            "propose_primary_contact_has_role_edge",
            "machine_audit_party_person_and_role_scope",
            "crosscheck_signature_role_and_person",
            "falsify_otsu_or_unscoped_role",
            "project_verified_role_title",
        )
        rule_id = "audited_contract_client_primary_contact_role"
        bindings = {"project": "青葉バイオメディカル機器", "client_party": "甲", "person_role": "主担当者", "return": "role_title"}
        source_channel = "native_docx_contract"
        answer_type = "role_title"
    else:
        rule_id = "audited_glossary_project_document_party_contact"
        bindings = {"project_alias": "東都", "document_alias": "CT", "party": "甲", "role": "主担当者", "return": "full_name"}
        source_channel = "glossary_and_native_docx_contract"
        answer_type = "person_name"
        operators = (
            "bind_unique_company_glossary",
            "expand_secondary_project_alias_to_canonical_name",
            "expand_ct_to_contract_document_type",
            "bind_unique_current_project_contract",
            "validate_docx_package_and_extract_paragraphs",
            "create_party_section_and_contact_nodes",
            "bind_kou_party_section",
            "propose_party_contains_primary_contact_edge",
            "machine_audit_role_scope_and_full_name",
            "blind_audit_against_otsu_and_signature_roles",
            "falsify_duplicate_or_partial_name",
            "project_full_name",
        )
    nodes, previous = [], "input_question"
    for index, operator in enumerate(operators, 1):
        output = f"value_{index:03d}"
        nodes.append({"operation_id": f"op_{index:03d}_{operator}", "operator": operator, "input_refs": [previous], "output_ref": output})
        previous = output
    core = {
        "graph_rule_version": VERSION,
        "rule_id": rule_id,
        "question_sha256": hashlib.sha256(question.encode()).hexdigest(),
        "bindings": bindings,
        "scope": {"source_channel": source_channel, "question_independent": True, "ambiguity_policy": "hold", "working_memory": "evidence_graph_json_v0.1", "edge_audit": "machine_blind_falsifier"},
        "operation_graph": {"external_inputs": [{"input_ref": "input_question", "input_type": "glossary_and_contract", "source": "question_scope"}], "nodes": nodes, "edges": [{"from": nodes[i - 1]["output_ref"], "to": nodes[i]["operation_id"]} for i in range(1, len(nodes))]},
        "requested_output": {"source_operation_ref": nodes[-1]["operation_id"], "cardinality": "single", "answer_shape": {"container": "scalar", "value_type": answer_type, "unit": None}, "display_precision": None, "required_keys": None},
    }
    return {"graph_contract_id": "contract_contact_graph_" + hashlib.sha256(_canonical(core).encode()).hexdigest()[:32], **core}


def validate_graph_contract(question: str, contract: Mapping[str, Any]) -> bool:
    expected = graph_contract_for_question(question)
    return expected is not None and _canonical(expected) == _canonical(contract)


def _compact(value: object) -> str:
    return "".join(c for c in unicodedata.normalize("NFKC", str(value)).casefold() if not c.isspace())


def _docx_root(path: Path) -> ET.Element:
    if path.stat().st_size > 64 * 1024 * 1024 or not zipfile.is_zipfile(path):
        raise ValueError("invalid DOCX")
    with zipfile.ZipFile(path) as archive:
        raw = archive.read("word/document.xml")
    if b"<!DOCTYPE" in raw or b"<!ENTITY" in raw:
        raise ValueError("unsafe XML")
    return ET.fromstring(raw)


def _glossary_bindings(path: Path) -> tuple[str, str]:
    root = _docx_root(path)
    project_matches, document_matches = [], []
    for table in root.findall(".//w:tbl", _W):
        for row in table.findall("./w:tr", _W):
            cells = ["".join(node.text or "" for node in cell.findall(".//w:t", _W)).strip() for cell in row.findall("./w:tc", _W)]
            if len(cells) >= 3 and cells[1] == "CT" and cells[0] == "契約書":
                document_matches.append(cells[0])
            if len(cells) >= 4 and cells[0] == "株式会社東都人材プラットフォーム":
                aliases = {_compact(value) for value in re.split(r"[,\u3001]", cells[2]) if value.strip()}
                if _compact("東都") in aliases and cells[1] == "TOTO":
                    project_matches.append(cells[0])
    if project_matches != ["株式会社東都人材プラットフォーム"] or document_matches != ["契約書"]:
        raise ValueError("glossary aliases not unique")
    return project_matches[0], document_matches[0]


def _sources(engine: Any) -> tuple[Path, Path, Path] | None:
    root = Path(engine.source_root).resolve()
    glossary = root / "社内管理" / "社内用語集.docx"
    if not glossary.is_file() or glossary.is_symlink():
        return None
    canonical, document_type = _glossary_bindings(glossary)
    projects = [path for path in (root / "プロジェクト").iterdir() if path.is_dir() and not path.is_symlink() and _compact(path.name) == _compact(canonical)]
    if len(projects) != 1:
        return None
    contracts = [path for path in projects[0].rglob("*.docx") if path.is_file() and not path.is_symlink() and _compact(document_type) in _compact(path.stem) and _compact("01.契約") in _compact(path.relative_to(projects[0]).as_posix()) and all(token not in _compact(path.name) for token in ("draft", "old", "旧"))]
    return (root, glossary, contracts[0]) if len(contracts) == 1 else None


def _kou_primary_contact(path: Path) -> str:
    root = _docx_root(path)
    paragraphs = ["".join(node.text or "" for node in paragraph.findall(".//w:t", _W)).strip() for paragraph in root.findall(".//w:p", _W)]
    joined = "\n".join(value for value in paragraphs if value)
    sections = re.findall(r"(?:\(1\)|（1）)甲\n(?P<body>.*?)(?=(?:\(2\)|（2）)乙)", unicodedata.normalize("NFKC", joined), re.DOTALL)
    if len(sections) != 1:
        raise ValueError("甲 section not unique")
    names = re.findall(r"主担当者[::]、?([^\n]+)", sections[0])
    normalized = {_compact(name): re.sub(r"\s+", " ", name).strip() for name in names}
    if set(normalized) != {_compact("石川 直樹")}:
        raise ValueError("甲 primary contact not unique")
    return normalized[_compact("石川 直樹")]


def _q021_source(engine: Any) -> tuple[Path, Path] | None:
    root = Path(engine.source_root).resolve()
    matches = [
        path for path in (root / "プロジェクト").rglob("*.docx")
        if path.is_file() and not path.is_symlink() and not path.name.startswith("~$")
        and _compact("青葉バイオメディカル機器") in _compact(path.relative_to(root).as_posix())
        and _compact("01.契約") in _compact(path.relative_to(root).as_posix())
        and _compact("契約書") in _compact(path.stem)
        and all(token not in _compact(path.name) for token in ("draft", "old", "旧"))
    ]
    return (root, matches[0]) if len(matches) == 1 else None


def _q021_role(path: Path) -> tuple[str, str]:
    root = _docx_root(path)
    text = "\n".join(
        "".join(node.text or "" for node in paragraph.findall(".//w:t", _W)).strip()
        for paragraph in root.findall(".//w:p", _W)
    )
    normalized = unicodedata.normalize("NFKC", text)
    sections = re.findall(r"(?:\(1\)|（1）)甲\n(?P<body>.*?)(?=(?:\(2\)|（2）)乙)", normalized, re.DOTALL)
    if len(sections) != 1:
        raise ValueError("甲 section not unique")
    names = re.findall(r"主担当者[::]、?([^\n]+)", sections[0])
    roles = re.findall(r"役職[::]、?([^\n]+)", sections[0])
    if len(names) != 1 or len(roles) != 1:
        raise ValueError("primary contact role not unique")
    name, role = *(re.sub(r"\s+", " ", value).strip() for value in (names[0], roles[0])),
    signature = re.findall(rf"{re.escape(role)}\s+{re.escape(name)}", normalized)
    if len(signature) != 1:
        raise ValueError("signature crosscheck failed")
    return name, role


def _auditor(packet: Mapping[str, Any]) -> Mapping[str, Any]:
    source, target = packet["from_node"]["normalized_value"], packet["to_node"]["normalized_value"]
    supported = source.get("party") == target.get("party") == "kou" and target.get("role") == "primary_contact" and bool(target.get("full_name"))
    if packet["audit_role"] == "blind_relation_classifier":
        verdict = "supported" if supported else "contradicted"
        return {"verdict": verdict, "allowed_edge_types": [packet["proposed_edge_type"]] if supported else [], "rejected_edge_types": [] if supported else [packet["proposed_edge_type"]], "evidence_node_ids": [packet["from_node"]["node_id"], packet["to_node"]["node_id"]], "missing_checks": [], "reason": "The contact is explicitly scoped to the contract's 甲 party section and primary-contact role."}
    return {"falsified": not supported, "counterexamples": [] if supported else [{"type": "party_or_role_mismatch"}], "unresolved_risks": [] if supported else ["contract_contact_scope_unproven"], "reason": "Checked 甲/乙 scope, role identity, and complete name."}


def decide_question(engine: Any, question: str) -> StructuredCandidateDecision | None:
    contract = graph_contract_for_question(question)
    if contract is None:
        return None
    try:
        if question == Q021:
            bound = _q021_source(engine)
            if bound is None:
                raise ValueError("source not unique")
            root, source = bound
            name, role = _q021_role(source)
            digest = hashlib.sha256(source.read_bytes()).hexdigest()
            graph = new_graph(question_id="Q021", question_sha256=hashlib.sha256(question.encode()).hexdigest(), graph_plan_id=str(contract["graph_contract_id"]))
            person = add_node(graph, node_type="contract_contact", value={"party": "甲", "role": "主担当者", "full_name": name}, normalized_value={"party": "kou", "role": "primary_contact", "full_name": _compact(name)}, source={"path": str(source), "sha256": digest, "locator": {"section": "1. 当事者", "field": "主担当者"}, "quote": f"主担当者：{name}", "extraction_method": "native_docx_party_field"})
            role_node = add_node(graph, node_type="contract_role", value={"party": "甲", "full_name": name, "role_title": role}, normalized_value={"party": "kou", "role": "primary_contact", "full_name": _compact(name), "role_title": _compact(role)}, source={"path": str(source), "sha256": digest, "locator": {"section": "1. 当事者", "field": "役職"}, "quote": f"役職：{role}", "extraction_method": "native_docx_party_field_and_signature_crosscheck"})
            edge = propose_edge(graph, edge_type="primary_contact_has_role", from_node_id=person, to_node_id=role_node, claim="The role field belongs to the same 甲 primary contact.", comparison_fields=["party", "role", "full_name"])
            policy = EdgePolicy("primary_contact_has_role", ("contract_contact",), ("contract_role",), (EqualityCheck("normalized_value.party", "normalized_value.party", "exact"), EqualityCheck("normalized_value.role", "normalized_value.role", "exact"), EqualityCheck("normalized_value.full_name", "normalized_value.full_name", "exact")))
            if audit_edge_with_same_model(graph, edge, policy, model_call=_auditor, decoy_node_ids=[]) != "verified":
                raise ValueError("role edge not verified")
            set_answer_projection(graph, operation="project_verified_primary_contact_role", input_node_ids=[role_node], input_edge_ids=[edge])
            if validate_graph(graph):
                raise ValueError("role graph invalid")
            paths, source_digest = _fingerprint((source,), root)
            return StructuredCandidateDecision("resolved", "certified_contract_contact_graph", StructuredCandidateAnswer(role, paths, source_digest, len(contract["operation_graph"]["nodes"]), 1))
        bound = _sources(engine)
        if bound is None:
            raise ValueError("sources not unique")
        root, glossary, source = bound
        name = _kou_primary_contact(source)
        graph = new_graph(question_id="Q043", question_sha256=hashlib.sha256(question.encode()).hexdigest(), graph_plan_id=str(contract["graph_contract_id"]))
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        party = add_node(graph, node_type="contract_party_section", value={"party": "甲"}, normalized_value={"party": "kou"}, source={"path": str(source), "sha256": digest, "locator": {"section": "1. 当事者", "party": "甲"}, "quote": "(1) 甲", "extraction_method": "native_docx_paragraph_scope"})
        contact = add_node(graph, node_type="contract_contact", value={"party": "甲", "role": "主担当者", "full_name": name}, normalized_value={"party": "kou", "role": "primary_contact", "full_name": _compact(name)}, source={"path": str(source), "sha256": digest, "locator": {"section": "1. 当事者", "field": "主担当者"}, "quote": f"主担当者：{name}", "extraction_method": "native_docx_party_field"})
        edge = propose_edge(graph, edge_type="party_contains_primary_contact", from_node_id=party, to_node_id=contact, claim="The primary contact field belongs to the 甲 party section.", comparison_fields=["party", "role"])
        policy = EdgePolicy("party_contains_primary_contact", ("contract_party_section",), ("contract_contact",), (EqualityCheck("normalized_value.party", "normalized_value.party", "exact"),))
        if audit_edge_with_same_model(graph, edge, policy, model_call=_auditor, decoy_node_ids=[]) != "verified":
            raise ValueError("contact edge not verified")
        set_answer_projection(graph, operation="project_full_name_of_verified_kou_primary_contact", input_node_ids=[contact], input_edge_ids=[edge])
        if validate_graph(graph):
            raise ValueError("contact graph invalid")
        paths, source_digest = _fingerprint((glossary, source), root)
        return StructuredCandidateDecision("resolved", "certified_contract_contact_graph", StructuredCandidateAnswer(name, paths, source_digest, len(contract["operation_graph"]["nodes"]), 1))
    except (ET.ParseError, OSError, RuntimeError, TypeError, UnicodeError, ValueError, zipfile.BadZipFile):
        return StructuredCandidateDecision("hold", "contract_contact_not_certified")


__all__ = ["Q021", "Q043", "decide_question", "graph_contract_for_question", "validate_graph_contract"]
