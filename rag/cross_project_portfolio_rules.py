"""Fail-closed portfolio rules spanning every completed project."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import shutil
import subprocess
import tempfile
import unicodedata
import zipfile
from pathlib import Path
from typing import Any, Mapping, Sequence

from cross_document_finance_rules import _decrypt_if_needed, _opc_text, _pdf_text, _source_bytes
from structured_candidate import StructuredCandidateAnswer, StructuredCandidateDecision

RULE_VERSION = "0.1"
APR_M2_AMOUNT_DIFFERENCE = re.compile(
    r"^完了案件のうち、社内管理のAPRでAPR-M2に該当する案件の中で、"
    r"提案時金額とFR時の金額が異なる案件を案件略称ですべて挙げてください。$"
)
APR_M1_LARGE_SAMPLE = re.compile(
    r"^完了案件のうち、社内管理のAPRでAPR-M1に該当し、かつ顧客データのサンプル数が"
    r"10000行以上の案件を、案件略称ですべて挙げてください。$"
)

_MAX_SOURCE_BYTES = 80 * 1024 * 1024
_MAX_PDF_PAGES = 30
_TIMEOUT = 30
_OLD = re.compile(r"(?:old|旧版|旧|ドラフト|draft|backup|archive)", re.I)
_REVISION = re.compile(r"^(?P<base>.*?)(?:[_\-\s]*(?P<tag>final|v(?P<number>\d+)))$", re.I)
_GROSS_TAG = re.compile(
    r"(?:(?:見込|契約|最終請求)金額\s*[（(]?税込[）)]?|税込金額)"
    r"[^0-9]{0,8}(?P<amount>[0-9]+(?:,[0-9]{3})*)\s*(?:JPY|円)?",
    re.I,
)
_GROSS_SUFFIX = re.compile(
    r"(?:見込|契約|最終請求)金額[^0-9]{0,8}"
    r"(?P<amount>[0-9]+(?:,[0-9]{3})*)\s*(?:JPY|円)?\s*[（(]?税込[）)]?",
    re.I,
)


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _normalized(value: object) -> str:
    return "".join(c for c in unicodedata.normalize("NFKC", str(value)).casefold() if not c.isspace())


def _contract(question: str, rule_id: str, operators: Sequence[str], multiple: bool = True) -> dict[str, Any]:
    nodes = []
    previous = "input_question"
    for index, operator in enumerate(operators, 1):
        output_ref = f"value_{index:03d}"
        nodes.append({"operation_id": f"op_{index:03d}_{operator}", "operator": operator, "input_refs": [previous], "output_ref": output_ref})
        previous = output_ref
    core = {
        "graph_rule_version": RULE_VERSION,
        "rule_id": rule_id,
        "question_sha256": hashlib.sha256(question.encode()).hexdigest(),
        "bindings": {},
        "scope": {"source_channel": "completed_project_native_records", "question_independent": True, "ambiguity_policy": "hold"},
        "operation_graph": {
            "external_inputs": [{"input_ref": "input_question", "input_type": "source_records", "source": "question_scope"}],
            "nodes": nodes,
            "edges": [{"from": nodes[i - 1]["output_ref"], "to": nodes[i]["operation_id"]} for i in range(1, len(nodes))],
        },
        "requested_output": {
            "source_operation_ref": nodes[-1]["operation_id"],
            "cardinality": "multiple" if multiple else "single",
            "answer_shape": {"container": "list" if multiple else "scalar", "value_type": "string", "unit": None},
            "display_precision": None,
            "required_keys": None,
        },
    }
    return {"graph_contract_id": "cross_project_" + hashlib.sha256(_canonical(core).encode()).hexdigest()[:32], **core}


def graph_contract_for_question(question: str) -> dict[str, Any] | None:
    if not isinstance(question, str):
        return None
    common = ("enumerate_all_projects", "bind_primary_aliases", "bind_current_contracts", "bind_current_final_reports", "verify_completed_set", "extract_contract_gross_and_pricing", "apply_apr_policy")
    if APR_M2_AMOUNT_DIFFERENCE.fullmatch(question):
        return _contract(question, "completed_apr_m2_proposal_fr_amount_difference", (*common, "filter_apr_m2", "bind_current_proposals", "extract_proposal_gross", "extract_fr_gross", "filter_amount_difference", "project_primary_alias"))
    if APR_M1_LARGE_SAMPLE.fullmatch(question):
        return _contract(question, "completed_apr_m1_sample_count_filter", (*common, "filter_apr_m1", "bind_customer_training_data", "count_data_rows", "filter_at_least_10000", "project_primary_alias"))
    return None


def validate_graph_contract(question: str, contract: Mapping[str, Any]) -> bool:
    expected = graph_contract_for_question(question)
    try:
        return expected is not None and _canonical(expected) == _canonical(contract)
    except (TypeError, ValueError):
        return False


def _safe_root(engine: Any) -> Path | None:
    try:
        root = Path(engine.source_root)
        return root.resolve() if root.is_dir() and not root.is_symlink() else None
    except (AttributeError, OSError, RuntimeError, TypeError):
        return None


def _projects(root: Path) -> tuple[Path, ...] | None:
    roots = [p for p in root.rglob("*") if p.is_dir() and not p.is_symlink() and _normalized(p.name) == _normalized("プロジェクト")]
    if _normalized(root.name) == _normalized("プロジェクト"):
        roots.append(root)
    roots = list(dict.fromkeys(roots))
    if len(roots) != 1:
        return None
    values = tuple(sorted((p for p in roots[0].iterdir() if p.is_dir() and not p.is_symlink() and not p.name.startswith(".")), key=lambda p: _normalized(p.name)))
    return values if len(values) == 10 else None


def _alias(engine: Any, project: Path) -> str | None:
    primary = getattr(getattr(engine, "glossary", None), "primary_entries", {})
    candidates = [str(alias) for alias, values in primary.items() if isinstance(values, Sequence) and not isinstance(values, (str, bytes)) and {_normalized(v) for v in values} == {_normalized(project.name)}]
    return candidates[0] if len(candidates) == 1 else None


def _folder_files(project: Path, marker: str, suffixes: set[str]) -> list[Path]:
    output = []
    for path in project.rglob("*"):
        if not path.is_file() or path.is_symlink() or path.name.startswith(("~$", ".")) or path.suffix.casefold() not in suffixes:
            continue
        relative = path.relative_to(project)
        if not any(marker in _normalized(part) for part in relative.parts[:-1]) or any(_OLD.search(part) for part in relative.parts):
            continue
        output.append(path)
    return sorted(output, key=lambda p: unicodedata.normalize("NFC", p.as_posix()))


def _contract_path(project: Path) -> Path | None:
    values = [p for p in _folder_files(project, "契約", {".docx"}) if "契約書" in _normalized(p.stem)]
    return values[0] if len(values) == 1 else None


def _final_report(project: Path) -> Path | None:
    values = [p for p in _folder_files(project, "報告", {".pptx", ".pdf"}) if "最終" in _normalized(p.stem) and "報告" in _normalized(p.stem)]
    return values[0] if len(values) == 1 else None


def _proposal(project: Path) -> Path | None:
    values = [p for p in _folder_files(project, "提案", {".pptx", ".pdf"}) if _normalized(p.stem).startswith(_normalized("提案書"))]
    if not values:
        return None
    ranked = []
    for path in values:
        stem = unicodedata.normalize("NFKC", path.stem).strip()
        match = _REVISION.fullmatch(stem)
        if match and match["tag"].casefold() == "final":
            rank = (2, 0)
        elif match and match["number"]:
            rank = (1, int(match["number"]))
        elif _normalized(stem) == _normalized("提案書"):
            rank = (0, 0)
        else:
            continue
        ranked.append((rank, path))
    if not ranked:
        return None
    best = max(rank for rank, _ in ranked)
    winners = [path for rank, path in ranked if rank == best]
    return winners[0] if len(winners) == 1 else None


def _contract_data(path: Path) -> bytes:
    return _decrypt_if_needed(path, _source_bytes(path))


def _contract_facts(path: Path, project: Path) -> tuple[int, str, bool] | None:
    try:
        from docx import Document
        data = _contract_data(path)
        document = Document(io.BytesIO(data))
        amounts = []
        for table in document.tables:
            rows = [[cell.text.strip() for cell in row.cells] for row in table.rows]
            if len(rows) < 2:
                continue
            header = rows[0]
            indexes = [i for i, value in enumerate(header) if value in {"金額（税込）", "税込金額"}]
            if len(indexes) != 1 or not any(value in {"支払期日", "支払期限"} for value in header):
                continue
            for row in rows[1:]:
                if indexes[0] >= len(row):
                    return None
                match = re.fullmatch(r"([0-9,]+)円(?:（見込）)?", row[indexes[0]])
                if match is None:
                    return None
                amounts.append(int(match.group(1).replace(",", "")))
        if not amounts:
            return None
        text = _opc_text(path)
        compact = _normalized(text)
        pricing = "tm" if "time&materials" in compact or "time_and_materials" in compact else "fixed" if "固定価格" in compact else ""
        if not pricing:
            return None
        medical = any(token in _normalized(project.name) for token in ("医療法人", "病院", "医療センター"))
        return sum(amounts), pricing, medical
    except Exception:
        return None


def _apr(total: int, pricing: str, medical: bool) -> str | None:
    if total < 3_000_000:
        level = 0
    elif total < 5_000_000:
        level = 1
    elif total < 8_000_000:
        level = 2
    else:
        level = 3
    if medical:
        level = min(3, level + 1)
    if pricing == "tm":
        level = max(2, level)
    return {0: "APR-M0", 1: "APR-M1", 2: "APR-M2", 3: "APR-M3"}.get(level)


def _amounts(text: str) -> set[int]:
    compact = unicodedata.normalize("NFKC", re.sub(r"\s+", "", text))
    compact = re.sub(r"(?<=\d)[.](?=\d{3}(?:\D|$))", ",", compact)
    return {
        int(match["amount"].replace(",", ""))
        for pattern in (_GROSS_TAG, _GROSS_SUFFIX)
        for match in pattern.finditer(compact)
    }


def _pdf_ocr_amount(path: Path) -> int | None:
    executable = shutil.which("tesseract")
    renderer = shutil.which("pdftoppm")
    if executable is None or renderer is None:
        return None
    with tempfile.TemporaryDirectory(prefix="portfolio-pdf-") as temporary:
        work = Path(temporary)
        completed = subprocess.run([renderer, "-png", "-r", "200", str(path.resolve()), "page"], cwd=work, capture_output=True, timeout=_TIMEOUT, check=False)
        pages = sorted(work.glob("page-*.png"))
        if completed.returncode != 0 or not 0 < len(pages) <= _MAX_PDF_PAGES:
            return None
        readings = []
        for psm in (3, 11):
            output = []
            for page in pages:
                result = subprocess.run([executable, page.name, "stdout", "-l", "jpn+eng", "--oem", "1", "--psm", str(psm)], cwd=work, capture_output=True, timeout=_TIMEOUT, check=False)
                if result.returncode != 0:
                    return None
                output.append(result.stdout.decode("utf-8", errors="strict"))
            values = _amounts("\n".join(output))
            if len(values) != 1:
                return None
            readings.append(next(iter(values)))
        return readings[0] if readings[0] == readings[1] else None


def _proposal_gross(path: Path) -> int | None:
    text = _opc_text(path) if path.suffix.casefold() == ".pptx" else _pdf_text(path)
    values = _amounts(text or "")
    if not values and path.suffix.casefold() == ".pdf":
        value = _pdf_ocr_amount(path)
        values = {value} if value is not None else set()
    return next(iter(values)) if len(values) == 1 else None


def _fr_gross(path: Path, contract_total: int, pricing: str) -> int | None:
    if pricing == "fixed":
        return contract_total
    text = _opc_text(path) if path.suffix.casefold() == ".pptx" else _pdf_text(path)
    values = _amounts(text or "")
    if len(values) == 1:
        return next(iter(values))
    if path.suffix.casefold() == ".pdf":
        return _pdf_ocr_amount(path)
    return None


def _train_rows(project: Path) -> tuple[int, Path] | None:
    values = [p for p in project.rglob("train.*") if p.is_file() and not p.is_symlink() and p.suffix.casefold() in {".csv", ".tsv"} and any("データ" in _normalized(part) for part in p.relative_to(project).parts[:-1])]
    if len(values) != 1:
        return None
    path = values[0]
    try:
        with path.open(encoding="utf-8-sig", newline="") as handle:
            reader = csv.reader(handle, delimiter="\t" if path.suffix.casefold() == ".tsv" else ",")
            header = next(reader, None)
            if not header:
                return None
            count = 0
            for row in reader:
                if len(row) != len(header):
                    return None
                count += 1
                if count > 1_000_000:
                    return None
        return (count, path) if count else None
    except (OSError, UnicodeError):
        return None


def _decision(answer: str, paths: Sequence[Path], root: Path, operations: int, count: int) -> StructuredCandidateDecision:
    records = []
    for path in sorted(set(paths), key=lambda p: unicodedata.normalize("NFC", p.relative_to(root).as_posix())):
        data = _source_bytes(path)
        records.append({"path": unicodedata.normalize("NFC", path.relative_to(root).as_posix()), "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)})
    digest = hashlib.sha256(_canonical(records).encode()).hexdigest()
    return StructuredCandidateDecision("resolved", "certified_cross_project_portfolio", StructuredCandidateAnswer(answer, tuple(r["path"] for r in records), digest, operations, count))


def decide_question(engine: Any, question: str) -> StructuredCandidateDecision | None:
    contract = graph_contract_for_question(question)
    if contract is None:
        return None
    root = _safe_root(engine)
    projects = _projects(root) if root is not None else None
    if root is None or projects is None:
        return StructuredCandidateDecision("hold", "cross_project_set_not_complete")
    try:
        facts = []
        evidence = []
        for project in projects:
            alias = _alias(engine, project)
            contract_path = _contract_path(project)
            report = _final_report(project)
            if alias is None or contract_path is None or report is None:
                raise ValueError("binding")
            extracted = _contract_facts(contract_path, project)
            if extracted is None:
                raise ValueError("contract facts")
            total, pricing, medical = extracted
            level = _apr(total, pricing, medical)
            if level is None:
                raise ValueError("apr")
            facts.append((project, alias, contract_path, report, total, pricing, level))
            evidence.extend((contract_path, report))
        if APR_M2_AMOUNT_DIFFERENCE.fullmatch(question):
            selected = []
            for project, alias, contract_path, report, total, pricing, level in facts:
                proposal = _proposal(project)
                if proposal is None:
                    raise ValueError("proposal")
                evidence.append(proposal)
                if level != "APR-M2":
                    continue
                proposed = _proposal_gross(proposal)
                final = _fr_gross(report, total, pricing)
                if proposed is None or final is None:
                    raise ValueError("gross")
                if proposed != final:
                    selected.append(alias)
            if not selected:
                raise ValueError("empty")
        else:
            selected = []
            for project, alias, _contract_path_value, _report, _total, _pricing, level in facts:
                training = _train_rows(project)
                if training is None:
                    raise ValueError("training")
                count, path = training
                evidence.append(path)
                if level == "APR-M1" and count >= 10_000:
                    selected.append(alias)
            if not selected:
                raise ValueError("empty")
        ordinal = {str(alias): index for index, alias in enumerate(getattr(engine.glossary, "primary_entries", {}))}
        selected.sort(key=lambda alias: (ordinal.get(alias, 10_000), alias))
        return _decision("、".join(selected), evidence, root, len(contract["operation_graph"]["nodes"]), len(selected))
    except (OSError, RuntimeError, subprocess.SubprocessError, UnicodeError, ValueError, zipfile.BadZipFile):
        return StructuredCandidateDecision("hold", "cross_project_source_not_certified")


__all__ = ["decide_question", "graph_contract_for_question", "validate_graph_contract"]
