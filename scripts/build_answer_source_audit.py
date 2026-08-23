#!/usr/bin/env python3
"""Build a conservative source audit for unresolved competition answers."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import sqlite3
import sys
import tempfile
import unicodedata
from decimal import Decimal, ROUND_HALF_UP
from datetime import date
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from lexical_search_common import canonical_json, digest_file
from search_lexical_index import search
from build_pdf_page_observations import validate_observation as validate_pdf_observation

GAPS = {"semantic_evidence_reasoning", "multi_document_reasoning"}
BUILDER_VERSION = "0.1.0"


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def normalized(value: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKC", value) if not c.isspace())


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def load_predictions(path: Path) -> dict[str, str]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.reader(handle))
    if not rows:
        raise ValueError("predictions must not be empty")
    data_rows = rows[1:] if rows[0] == ["index", "answer"] else rows
    values = {row[0]: row[1] for row in data_rows if len(row) == 2}
    if len(values) != len(data_rows):
        raise ValueError("predictions contain malformed or duplicate rows")
    return values


def source_index(state: dict[str, Any]) -> dict[str, dict[str, str]]:
    values: dict[str, dict[str, str]] = {}
    for path, entry in state.get("entries", {}).items():
        document_id = entry.get("document_id")
        source_sha = entry.get("source_sha256")
        if not document_id or not source_sha:
            raise ValueError(f"incomplete intermediate entry: {path}")
        if document_id in values:
            raise ValueError(f"duplicate document_id: {document_id}")
        values[document_id] = {"source_path": path, "source_sha256": source_sha}
    return values


def classify(candidate: dict[str, Any], current_answer: str) -> tuple[str, dict[str, Any]]:
    strict = candidate.get("strict_status") == "pass"
    resolved = candidate.get("decision_status") == "resolved"
    derived = candidate.get("candidate_answer")
    source_paths = list(candidate.get("source_paths") or [])
    complete = bool(strict and resolved and isinstance(derived, str) and source_paths)
    if complete:
        same = normalized(derived) == normalized(current_answer)
        return ("verified" if same else "contradicted"), {
            "method": "strict_graph_contract",
            "source_derived_answer": derived,
            "verified_source_paths": source_paths,
            "reasons": ["strict_graph_contract_and_output_validation_pass" if same else "source_derived_answer_differs_from_current_answer"],
            "proof_complete": True,
        }
    return "unverified", {
        "method": "retrieval_observation_only",
        "source_derived_answer": None,
        "verified_source_paths": [],
        "reasons": ["retrieval_is_candidate_evidence_not_a_complete_answer_proof"],
        "proof_complete": False,
    }


ROLE_QUESTION = re.compile(r"^(?P<org>.+)のクライアントの主担当者の役職は何ですか。$")
NAME_QUESTION = re.compile(r"^(?P<org>.+)のCTにおいて、甲側の主担当者をフルネームで教えてください。$")
SCOPE_COUNT_QUESTION = re.compile(r"^(?P<org>.+)の提案書において、スコープ対象外としている項目はいくつありますか。$")
UNFINISHED_IDS_QUESTION = re.compile(r"^(?P<org>.+)の最終報告資料内で未完事項として挙げられているIDをすべて抽出してください。$")
UNMET_KPI_QUESTION = re.compile(r"^(?P<org>.+)の最終報告において、設定されたKPIとして未達成とされている項目を挙げてください。$")
NEXT_F1_ACCURACY_QUESTION = re.compile(r"^(?P<org>.+)の(?P<filename>.+_最終報告\.pptx)において、F1スコアにて(?P<model>[A-Za-z0-9_]+)に次ぐ順位のモデルの Accuracy はいくつですか。$")
DEATH_RATE_RATIO_QUESTION = re.compile(r"^(?P<org>.+)の糖尿病統計情報調査結果において、死亡率が最も高い都道府県の死亡率は、(?P<rank>[0-9]+)番目に低い都道府県の死亡率の何倍ですか。小数第2位まで求めてください。$")
ESTIMATE_FINAL_DIFFERENCE_QUESTION = re.compile(r"^(?P<org>.+)案件において、提案時の税込み見込み金額と最終請求金額の差額はいくらですか。$")
ACTH_DECREASE_QUESTION = re.compile(r"^(?P<org>.+)の案件において、案件終了後のACTHが(?P<hours>[0-9]+)時間(?P<minutes>[0-9]+)分だった場合の税込請求額は提案書内で記載の見込税込金額と比べて何円の減額になりますか。$")
OVER_HOURS_SETTLEMENT_QUESTION = re.compile(r"^(?P<org>.+)の契約条件において、ACTHが(?P<threshold>[0-9]+)時間を超えた場合の精算方法に関する規定内容を答えてください。$")
TM_RATE_CHANGE_QUESTION = re.compile(r"^TM案件において、RATEが変更されたのは何年何月1日からと想定されますか。$")
CONTRACT_OVERLAP_QUESTION = re.compile(r"^(?P<from>20\d\d-\d\d-\d\d) から (?P<to>20\d\d-\d\d-\d\d) の間に契約期間が重なっている案件の中で、契約期間が (?P<days>[0-9]+)日 を超えている案件を、主略称ですべて挙げてください。$")
HOURLY_DECREASE_QUESTION = re.compile(r"^(?P<alias>[A-Z0-9_-]+)において、見込金額（税込）と確定金額（税込）の差を、ESTHとACTHの差で割った1時間あたりの減少金額を計算してください。$")
MAX_TM_HOURS_GAP_QUESTION = re.compile(r"^事後精算案件のうち、提案時の見積工数と最終報告で報告されている実績工数の乖離が最も大きい案件を主略称で挙げてください。$")
RATE_AND_HOURS_VARIANCE_QUESTION = re.compile(r"^(?P<alias>[A-Z0-9_-]+)の契約条件において、契約単価が現状よりも(?P<rate_delta>[0-9,]+)円高く、実績工数が(?P<hours_delta>[0-9.]+)時間少なかった場合、税込請求金額は、実際の税込請求金額と比べていくら変動しますか。$")


def _contract_document(org: str, sources: dict[str, dict[str, str]]) -> tuple[str, dict[str, str]] | None:
    org_norm = normalized(org)
    matches = [
        (document_id, source) for document_id, source in sources.items()
        if source["source_path"].endswith("/01.契約/契約書.docx")
        and org_norm in normalized(source["source_path"])
    ]
    return matches[0] if len(matches) == 1 else None


def deterministic_contract_field(question: str, sources: dict[str, dict[str, str]], index_dir: Path) -> tuple[str, list[str]] | None:
    role_match = ROLE_QUESTION.fullmatch(question)
    name_match = NAME_QUESTION.fullmatch(question)
    match = role_match or name_match
    if match is None:
        return None
    bound = _contract_document(match.group("org"), sources)
    if bound is None:
        return None
    document_id, source = bound
    database = index_dir / "lexical-index.sqlite3"
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        texts = [row[0] for row in connection.execute(
            "SELECT search_text FROM documents WHERE document_id = ? ORDER BY search_unit_id", (document_id,)
        )]
    finally:
        connection.close()
    values: set[str] = set()
    if role_match:
        pattern = re.compile(r"主担当者[：:]\s*[^\n]+?\s+役職[：:]\s*([^\n]+)")
    else:
        pattern = re.compile(r"主担当者[：:]\s*([^\n]+?)\s+役職[：:]")
    for text in texts:
        for value in pattern.findall(text):
            cleaned = " ".join(value.split()).strip("。 ")
            if cleaned:
                values.add(cleaned)
    if len(values) != 1:
        return None
    return next(iter(values)), [source["source_path"]]


def _unique_document(org: str, sources: dict[str, dict[str, str]], suffix: str) -> tuple[str, dict[str, str]] | None:
    org_norm = normalized(org)
    matches = [
        (document_id, source) for document_id, source in sources.items()
        if source["source_path"].endswith(suffix) and org_norm in normalized(source["source_path"])
    ]
    return matches[0] if len(matches) == 1 else None


def _document_rows(document_id: str, index_dir: Path) -> list[tuple[str, dict[str, Any], str]]:
    connection = sqlite3.connect(f"file:{index_dir / 'lexical-index.sqlite3'}?mode=ro", uri=True)
    try:
        return [(unit_type, json.loads(locator), text) for unit_type, locator, text in connection.execute(
            "SELECT unit_type, locator_json, search_text FROM documents WHERE document_id = ? ORDER BY search_unit_id", (document_id,)
        )]
    finally:
        connection.close()


def deterministic_structured_source(question: str, sources: dict[str, dict[str, str]], index_dir: Path, pdf_observations: list[dict[str, Any]] | None = None) -> tuple[str, list[str]] | None:
    scope = SCOPE_COUNT_QUESTION.fullmatch(question)
    unfinished = UNFINISHED_IDS_QUESTION.fullmatch(question)
    unmet = UNMET_KPI_QUESTION.fullmatch(question)
    next_accuracy = NEXT_F1_ACCURACY_QUESTION.fullmatch(question)
    death_ratio = DEATH_RATE_RATIO_QUESTION.fullmatch(question)
    estimate_final = ESTIMATE_FINAL_DIFFERENCE_QUESTION.fullmatch(question)
    acth_decrease = ACTH_DECREASE_QUESTION.fullmatch(question)
    over_hours = OVER_HOURS_SETTLEMENT_QUESTION.fullmatch(question)
    tm_rate_change = TM_RATE_CHANGE_QUESTION.fullmatch(question)
    contract_overlap = CONTRACT_OVERLAP_QUESTION.fullmatch(question)
    hourly_decrease = HOURLY_DECREASE_QUESTION.fullmatch(question)
    max_tm_gap = MAX_TM_HOURS_GAP_QUESTION.fullmatch(question)
    rate_hours_variance = RATE_AND_HOURS_VARIANCE_QUESTION.fullmatch(question)
    if scope:
        bound = _unique_document(scope.group("org"), sources, "/00.提案/提案書.pptx")
        if bound is None: return None
        document_id, source = bound
        candidates = [text for unit, locator, text in _document_rows(document_id, index_dir) if unit == "text_chunk" and locator.get("locator_text") == "speaker-notes" and "スコープ対象外" in text]
        if len(candidates) != 1: return None
        items = [line.strip() for line in candidates[0].splitlines() if line.strip().startswith("✖")]
        if not items or len(items) != len(set(items)): return None
        return str(len(items)), [source["source_path"]]
    if unfinished:
        org = unfinished.group("org")
        matches = [(doc, source) for doc, source in sources.items() if normalized(org) in normalized(source["source_path"]) and "/06.報告書/" in source["source_path"] and source["source_path"].endswith("_最終報告.pptx")]
        if len(matches) != 1: return None
        document_id, source = matches[0]
        candidates = [text for unit, _, text in _document_rows(document_id, index_dir) if unit == "slide_text" and "要アクション（未完事項）" in text]
        if len(candidates) != 1: return None
        ids = re.findall(r"(?m)^(AI-\d+):", candidates[0])
        if not ids or len(ids) != len(set(ids)): return None
        return "、".join(ids), [source["source_path"]]
    if unmet:
        org = unmet.group("org")
        matches = [(doc, source) for doc, source in sources.items() if normalized(org) in normalized(source["source_path"]) and "/06.報告書/" in source["source_path"] and source["source_path"].endswith("_最終報告.pptx")]
        if len(matches) != 1: return None
        document_id, source = matches[0]
        rows = [(locator, text) for unit, locator, text in _document_rows(document_id, index_dir) if unit == "table_row" and "KPI分類:" in text and "評価:" in text]
        if not rows: return None
        slide_numbers = {locator.get("slide_number") for locator, _ in rows}
        row_indexes = sorted(locator.get("row_index") for locator, _ in rows)
        if len(slide_numbers) != 1 or None in row_indexes or row_indexes != list(range(row_indexes[0], row_indexes[-1] + 1)): return None
        assessments = []
        categories = []
        for _, text in rows:
            category = re.search(r"(?m)^KPI分類:\s*(.+)$", text)
            assessment = re.search(r"(?m)^評価:\s*(.+)$", text)
            if not category or not assessment: return None
            categories.append(category.group(1).strip()); assessments.append(assessment.group(1).strip())
        unmet_categories = [category for category, assessment in zip(categories, assessments) if normalized(assessment) != normalized("達成")]
        return ("、".join(unmet_categories) if unmet_categories else "該当するものはありません。"), [source["source_path"]]
    if next_accuracy:
        org = next_accuracy.group("org")
        filename = next_accuracy.group("filename")
        matches = [(doc, source) for doc, source in sources.items() if normalized(org) in normalized(source["source_path"]) and source["source_path"].endswith("/06.報告書/" + filename)]
        if len(matches) != 1: return None
        document_id, source = matches[0]
        parsed = []
        for unit, locator, text in _document_rows(document_id, index_dir):
            if unit != "table_row" or not all(label in text for label in ("Rank:", "モデル種別:", "F1 (macro):", "Accuracy:")): continue
            rank = re.search(r"(?m)^Rank:\s*(\d+)$", text)
            model = re.search(r"(?m)^モデル種別:\s*(\S+)$", text)
            f1 = re.search(r"(?m)^F1 \(macro\):\s*([0-9.]+)$", text)
            accuracy = re.search(r"(?m)^Accuracy:\s*([0-9.]+)$", text)
            if not all((rank, model, f1, accuracy)): return None
            parsed.append((int(rank.group(1)), model.group(1), f1.group(1), accuracy.group(1), locator.get("slide_number")))
        parsed.sort()
        if len(parsed) < 2 or [row[0] for row in parsed] != list(range(1, len(parsed) + 1)) or len({row[4] for row in parsed}) != 1: return None
        target_ranks = [row[0] for row in parsed if row[1] == next_accuracy.group("model")]
        if len(target_ranks) != 1 or target_ranks[0] == len(parsed): return None
        following = parsed[target_ranks[0]]
        return following[3], [source["source_path"]]
    if death_ratio:
        org = death_ratio.group("org")
        matches = [(doc, source) for doc, source in sources.items() if normalized(org) in normalized(source["source_path"]) and source["source_path"].endswith("/00.提案/糖尿病統計情報.docx")]
        if len(matches) != 1: return None
        document_id, source = matches[0]
        rows = []
        for unit, locator, text in _document_rows(document_id, index_dir):
            if unit != "table_row" or locator.get("table_index") != 3 or not all(label in text for label in ("順位:", "死亡率が高い都道府県", "死亡率が低い都道府県")): continue
            rank = re.search(r"(?m)^順位:\s*(\d+)位$", text)
            rates = re.findall(r"(?m)^死亡率（%）:\s*([0-9.]+)$", text)
            if not rank or len(rates) != 2: return None
            rows.append((int(rank.group(1)), Decimal(rates[0]), Decimal(rates[1])))
        rows.sort()
        if not rows or [row[0] for row in rows] != list(range(1, len(rows) + 1)): return None
        requested = int(death_ratio.group("rank"))
        if requested < 1 or requested > len(rows): return None
        highest = rows[0][1]
        requested_lowest = rows[requested - 1][2]
        result = (highest / requested_lowest).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        return format(result, ".2f"), [source["source_path"]]
    if estimate_final:
        org = estimate_final.group("org")
        proposal = _unique_document(org, sources, "/00.提案/提案書.pptx")
        reports = [(doc, source) for doc, source in sources.items() if normalized(org) in normalized(source["source_path"]) and "/06.報告書/" in source["source_path"] and "最終報告" in source["source_path"]]
        if proposal is None or len(reports) != 1: return None
        proposal_doc, proposal_source = proposal
        report_doc, report_source = reports[0]
        proposal_values = []
        for _, _, text in _document_rows(proposal_doc, index_dir):
            match = re.search(r"見込金額（税込）\s*\n?\s*[¥￥]?([0-9,]+)", text)
            if match: proposal_values.append(int(match.group(1).replace(",", "")))
        proposal_values = sorted(set(proposal_values))
        final_values = []
        for unit, _, text in _document_rows(report_doc, index_dir):
            if unit != "page_text" or "請求情報" not in text: continue
            match = re.search(r"税込金額\s+([0-9,]+)円", text)
            if match: final_values.append(int(match.group(1).replace(",", "")))
        final_values = sorted(set(final_values))
        if len(proposal_values) != 1 or len(final_values) != 1: return None
        difference = abs(final_values[0] - proposal_values[0])
        return f"{difference:,}円", [proposal_source["source_path"], report_source["source_path"]]
    if acth_decrease:
        org = acth_decrease.group("org")
        proposal = _unique_document(org, sources, "/00.提案/提案書.pptx")
        contract = _unique_document(org, sources, "/01.契約/契約書.docx")
        if proposal is None or contract is None: return None
        proposal_doc, proposal_source = proposal
        contract_doc, contract_source = contract
        proposal_values = []
        for _, _, text in _document_rows(proposal_doc, index_dir):
            match = re.search(r"見込金額（税込）\s*\n?\s*[¥￥]?([0-9,]+)", text)
            if match: proposal_values.append(Decimal(match.group(1).replace(",", "")))
        contract_text = "\n".join(text for _, _, text in _document_rows(contract_doc, index_dir))
        rates = sorted(set(re.findall(r"時間単価は([0-9,]+)円（消費税別）", contract_text)))
        taxes = sorted(set(re.findall(r"消費税([0-9,]+)円、税込([0-9,]+)円", contract_text)))
        if len(set(proposal_values)) != 1 or len(rates) != 1 or len(taxes) != 1: return None
        if "作業時間の計上単位は30分" not in contract_text or "30分未満の端数は30分単位に切り上げ" not in contract_text: return None
        tax_amount, tax_inclusive = (Decimal(value.replace(",", "")) for value in taxes[0])
        pre_tax_estimate = tax_inclusive - tax_amount
        tax_rate = tax_amount / pre_tax_estimate
        hours = Decimal(acth_decrease.group("hours"))
        minutes = int(acth_decrease.group("minutes"))
        if not 0 <= minutes < 60: return None
        rounded_hours = hours + (Decimal("0") if minutes == 0 else Decimal("0.5") if minutes <= 30 else Decimal("1"))
        actual = rounded_hours * Decimal(rates[0].replace(",", "")) * (Decimal("1") + tax_rate)
        decrease = next(iter(set(proposal_values))) - actual
        if decrease < 0 or decrease != decrease.to_integral_value(): return None
        return f"{int(decrease):,}円", [proposal_source["source_path"], contract_source["source_path"]]
    if over_hours:
        contract = _unique_document(over_hours.group("org"), sources, "/01.契約/契約書.docx")
        if contract is None: return None
        document_id, source = contract
        contract_text = "\n".join(text for _, _, text in _document_rows(document_id, index_dir))
        rates = sorted(set(re.findall(r"時間単価は([0-9,]+)円（消費税別）", contract_text)))
        required = (
            "実績工数に基づく事後精算（月次精算）",
            "最終請求額は、実績工数に時間単価を乗じ、これに消費税を加算した金額",
            "実績工数がこれを上回りまたは下回る場合でも",
        )
        if len(rates) != 1 or not all(value in contract_text for value in required): return None
        rate = int(rates[0].replace(",", ""))
        threshold = int(over_hours.group("threshold"))
        return f"{threshold}時間を超えても、当該月の実績工数に時間単価{rate:,}円を乗じ、消費税を加算した金額を月次精算する。", [source["source_path"]]
    if tm_rate_change:
        observations = []
        for document_id, source in sources.items():
            path = source["source_path"]
            if "/01.契約/" not in path or not path.endswith(".docx") or "draft" in path: continue
            text = "\n".join(value for _, _, value in _document_rows(document_id, index_dir))
            if "time_and_materials" not in text and "Time & Materials" not in text: continue
            rate_values = set(re.findall(r"時間単価(?:は|[:：])\s*([0-9,]+)円", text))
            rate_values.update(re.findall(r"項目:\s*時間単価\s*\n内容:\s*([0-9,]+)円/時間", text))
            rate_values.update(re.findall(r"時間単価は、1時間当たり([0-9,]+)円", text))
            start = (
                re.search(r"締結日兼効力発生日は、(20\d\d)-(\d\d)-(\d\d)", text)
                or re.search(r"締結日および効力発生日は、(20\d\d)-(\d\d)-(\d\d)", text)
                or re.search(r"本契約の締結日および効力発生日は、(?:いずれも)?(20\d\d)-(\d\d)-(\d\d)", text)
                or re.search(r"契約締結日および効力発生日は(20\d\d)-(\d\d)-(\d\d)", text)
                or re.search(r"本契約の有効期間は、(20\d\d)-(\d\d)-(\d\d)から", text)
            )
            if len(rate_values) != 1 or start is None: return None
            observations.append((date(*(int(part) for part in start.groups())), int(next(iter(rate_values)).replace(",", "")), path))
        if len(observations) < 2: return None
        rates = sorted({rate for _, rate, _ in observations})
        if len(rates) != 2: return None
        old = [item for item in observations if item[1] == rates[0]]
        new = [item for item in observations if item[1] == rates[1]]
        if not old or not new or max(item[0] for item in old) >= min(item[0] for item in new): return None
        last_old = max(item[0] for item in old)
        first_new = min(item[0] for item in new)
        next_month_year = last_old.year + (1 if last_old.month == 12 else 0)
        next_month = 1 if last_old.month == 12 else last_old.month + 1
        if (first_new.year, first_new.month) != (next_month_year, next_month): return None
        return f"{first_new.year}年{first_new.month}月1日", sorted(item[2] for item in observations)
    if contract_overlap:
        glossary_docs = [(doc, source) for doc, source in sources.items() if source["source_path"] == "社内管理/社内用語集.docx"]
        if len(glossary_docs) != 1: return None
        glossary_doc, glossary_source = glossary_docs[0]
        aliases = []
        for unit, locator, text in _document_rows(glossary_doc, index_dir):
            if unit != "table_row" or locator.get("table_index") != 9 or "案件名:" not in text or "主略称:" not in text: continue
            canonical = re.search(r"(?m)^案件名:\s*(.+)$", text)
            alias = re.search(r"(?m)^主略称:\s*(\S+)$", text)
            if not canonical or not alias: return None
            aliases.append((locator.get("row_index"), canonical.group(1).strip(), alias.group(1).strip()))
        if not aliases or any(row is None for row, _, _ in aliases): return None
        start_window = date.fromisoformat(contract_overlap.group("from")); end_window = date.fromisoformat(contract_overlap.group("to"))
        threshold = int(contract_overlap.group("days")); selected = []
        contract_paths = []
        for row, canonical, alias in sorted(aliases):
            matches = [(doc, source) for doc, source in sources.items() if normalized(canonical) in normalized(source["source_path"]) and "/01.契約/" in source["source_path"] and source["source_path"].endswith(".docx") and "draft" not in source["source_path"]]
            if len(matches) != 1: continue
            document_id, source = matches[0]
            text = "\n".join(value for _, _, value in _document_rows(document_id, index_dir))
            period = re.search(r"(?:有効期間|契約期間|期間)は、?\s*(20\d\d-\d\d-\d\d)\s*から\s*(20\d\d-\d\d-\d\d)\s*まで", text)
            if period:
                start, end = (date.fromisoformat(value) for value in period.groups())
            else:
                period = re.search(r"有効期間は、(20\d\d-\d\d-\d\d)から起算して([0-9]+)週間", text)
                if not period: return None
                start = date.fromisoformat(period.group(1))
                from datetime import timedelta
                end = start + timedelta(weeks=int(period.group(2)))
            contract_paths.append(source["source_path"])
            if start <= end_window and end >= start_window and (end - start).days > threshold:
                selected.append(alias)
        if not selected: return None
        return "、".join(selected), [glossary_source["source_path"], *sorted(contract_paths)]
    if hourly_decrease:
        glossary_docs = [(doc, source) for doc, source in sources.items() if source["source_path"] == "社内管理/社内用語集.docx"]
        if len(glossary_docs) != 1: return None
        glossary_doc, glossary_source = glossary_docs[0]
        canonicals = []
        for unit, locator, text in _document_rows(glossary_doc, index_dir):
            if unit != "table_row" or locator.get("table_index") != 9: continue
            canonical = re.search(r"(?m)^案件名:\s*(.+)$", text); alias = re.search(r"(?m)^主略称:\s*(\S+)$", text)
            if canonical and alias and alias.group(1).strip() == hourly_decrease.group("alias"):
                canonicals.append(canonical.group(1).strip())
        if len(canonicals) != 1: return None
        canonical = canonicals[0]
        contracts = [(doc, source) for doc, source in sources.items() if normalized(canonical) in normalized(source["source_path"]) and "/01.契約/" in source["source_path"] and source["source_path"].endswith(".docx") and "draft" not in source["source_path"]]
        reports = [(doc, source) for doc, source in sources.items() if normalized(canonical) in normalized(source["source_path"]) and "/06.報告書/" in source["source_path"] and source["source_path"].endswith("_最終報告.pptx")]
        if len(contracts) != 1 or len(reports) != 1: return None
        contract_doc, contract_source = contracts[0]; report_doc, report_source = reports[0]
        contract_text = "\n".join(value for _, _, value in _document_rows(contract_doc, index_dir))
        report_text = "\n".join(value for _, _, value in _document_rows(report_doc, index_dir))
        esth = sorted(set(re.findall(r"想定総工数[：:]\s*([0-9.]+)時間", contract_text)))
        estimate = sorted(set(re.findall(r"想定金額（税込）[：:]\s*([0-9,]+)円", contract_text)))
        acth = sorted(set(re.findall(r"実績工数[：:]\s*([0-9.]+)\s*時間", report_text)))
        final = sorted(set(re.findall(r"税込金額[：:]\s*([0-9,]+)\s*円", report_text)))
        if not all(len(values) == 1 for values in (esth, estimate, acth, final)): return None
        hours_delta = Decimal(esth[0]) - Decimal(acth[0])
        amount_delta = Decimal(estimate[0].replace(",", "")) - Decimal(final[0].replace(",", ""))
        if hours_delta <= 0 or amount_delta <= 0: return None
        result = amount_delta / hours_delta
        if result != result.to_integral_value(): return None
        return f"{int(result):,}円", [glossary_source["source_path"], contract_source["source_path"], report_source["source_path"]]
    if max_tm_gap:
        if pdf_observations is None:
            return None
        glossary_docs = [(doc, source) for doc, source in sources.items() if source["source_path"] == "社内管理/社内用語集.docx"]
        if len(glossary_docs) != 1:
            return None
        glossary_doc, glossary_source = glossary_docs[0]
        aliases: list[tuple[str, str]] = []
        for unit, locator, text in _document_rows(glossary_doc, index_dir):
            if unit != "table_row" or locator.get("table_index") != 9:
                continue
            canonical = re.search(r"(?m)^案件名:\s*(.+)$", text)
            alias = re.search(r"(?m)^主略称:\s*(\S+)$", text)
            if canonical and alias:
                aliases.append((canonical.group(1).strip(), alias.group(1).strip()))
        compared: list[tuple[Decimal, str, list[str]]] = []
        for canonical, alias in aliases:
            contracts = [(doc, source) for doc, source in sources.items()
                         if normalized(canonical) in normalized(source["source_path"])
                         and "/01.契約/" in source["source_path"]
                         and source["source_path"].endswith(".docx")
                         and "draft" not in source["source_path"]]
            if len(contracts) != 1:
                continue
            contract_doc, contract_source = contracts[0]
            contract_text = "\n".join(value for _, _, value in _document_rows(contract_doc, index_dir))
            if "time_and_materials" not in contract_text and "Time & Materials" not in contract_text:
                continue
            estimates = set(re.findall(r"想定(?:総)?工数(?:は|[：:])?\s*(?:\n内容[：:]\s*)?([0-9]+(?:\.[0-9]+)?)\s*時間", contract_text))
            contract_rates = set(re.findall(r"時間単価(?:は|[：:])?\s*([0-9,]+)円", contract_text))
            contract_rates.update(re.findall(r"項目:\s*時間単価\s*\n内容:\s*([0-9,]+)円/時間", contract_text))
            if len(estimates) != 1 or len(contract_rates) != 1:
                return None
            reports = [(doc, source) for doc, source in sources.items()
                       if normalized(canonical) in normalized(source["source_path"])
                       and "/06.報告書/" in source["source_path"]
                       and "最終報告" in source["source_path"]
                       and "_old" not in source["source_path"]]
            if len(reports) != 1:
                return None
            report_doc, report_source = reports[0]
            report_text = "\n".join(value for _, _, value in _document_rows(report_doc, index_dir))
            actuals = set(re.findall(r"実績工数\s*(?:[：:]\s*|\n(?:値|金額 / 数値)?[：:]?\s*)([0-9]+(?:\.[0-9]+)?)\s*時間", report_text))
            if not actuals and report_source["source_path"].endswith(".pdf"):
                independent_readings: list[str] = []
                native_exact = False
                for observation in pdf_observations:
                    if observation.get("source", {}).get("relative_path") != report_source["source_path"]:
                        continue
                    if observation.get("source", {}).get("sha256") != report_source["source_sha256"]:
                        return None
                    native_raw = " ".join(word.get("raw_text", "") for word in (observation.get("native") or {}).get("words", []))
                    native_values = set(re.findall(r"実績工数\s*[：:]?\s*([0-9]+(?:\.[0-9]+)?)\s*時間", native_raw))
                    if len(native_values) == 1:
                        independent_readings.append(next(iter(native_values)))
                        native_exact = observation.get("status") == "observed"
                    for run in (observation.get("ocr") or {}).get("raw_runs", []):
                        if run.get("status") != "completed":
                            continue
                        joined = " ".join(line.get("raw_text", "") for line in run.get("lines", []))
                        run_values = set(re.findall(r"実績工数\s*[：:]?\s*([0-9]+(?:\.[0-9]+)?)\s*時間", joined))
                        delta_values = set()
                        for estimate_value, actual_value in re.findall(r"想定([0-9]+(?:\.[0-9]+)?)hに対し[^\n]{0,80}?([0-9]+(?:\.[0-9]+)?)時間", joined):
                            if estimate_value != next(iter(estimates)):
                                return None
                            delta_values.add(actual_value)
                        run_values.update(delta_values)
                        rate = Decimal(next(iter(contract_rates)).replace(",", ""))
                        hour_values = set(re.findall(r"([0-9]+(?:\.[0-9]+)?)\s*時間", joined))
                        money_values = set(re.findall(r"([0-9][0-9,]+)\s*JPY", joined))
                        invoice_bound = {
                            hour for hour in hour_values
                            if any(Decimal(hour) * rate == Decimal(money.replace(",", "")) for money in money_values)
                        }
                        run_values.update(invoice_bound)
                        if len(run_values) == 1:
                            independent_readings.append(next(iter(run_values)))
                if (not native_exact and len(independent_readings) < 2) or len(set(independent_readings)) != 1:
                    return None
                actuals = set(independent_readings)
            if len(actuals) != 1:
                return None
            estimate = Decimal(next(iter(estimates)))
            actual = Decimal(next(iter(actuals)))
            compared.append((abs(actual - estimate), alias, [contract_source["source_path"], report_source["source_path"]]))
        if len(compared) < 2:
            return None
        compared.sort(key=lambda item: (-item[0], item[1]))
        if compared[0][0] == compared[1][0]:
            return None
        paths = [glossary_source["source_path"]]
        for _, _, source_paths in compared:
            paths.extend(source_paths)
        return compared[0][1], list(dict.fromkeys(paths))
    if rate_hours_variance:
        glossary_docs = [(doc, source) for doc, source in sources.items() if source["source_path"] == "社内管理/社内用語集.docx"]
        if len(glossary_docs) != 1:
            return None
        glossary_doc, glossary_source = glossary_docs[0]
        canonicals = []
        for unit, locator, text in _document_rows(glossary_doc, index_dir):
            if unit != "table_row" or locator.get("table_index") != 9:
                continue
            canonical = re.search(r"(?m)^案件名:\s*(.+)$", text)
            alias = re.search(r"(?m)^主略称:\s*(\S+)$", text)
            if canonical and alias and alias.group(1).strip() == rate_hours_variance.group("alias"):
                canonicals.append(canonical.group(1).strip())
        if len(canonicals) != 1:
            return None
        canonical = canonicals[0]
        contracts = [(doc, source) for doc, source in sources.items() if normalized(canonical) in normalized(source["source_path"]) and source["source_path"].endswith("/01.契約/契約書.docx")]
        reports = [(doc, source) for doc, source in sources.items() if normalized(canonical) in normalized(source["source_path"]) and "/06.報告書/" in source["source_path"] and source["source_path"].endswith("_最終報告.pptx")]
        if len(contracts) != 1 or len(reports) != 1:
            return None
        contract_doc, contract_source = contracts[0]
        report_doc, report_source = reports[0]
        contract_text = "\n".join(value for _, _, value in _document_rows(contract_doc, index_dir))
        report_text = "\n".join(value for _, _, value in _document_rows(report_doc, index_dir))
        rates = set(re.findall(r"時間単価(?:は|[：:])?\s*[¥￥]?([0-9,]+)", contract_text))
        rates.update(re.findall(r"項目:\s*時間単価\s*\n内容:\s*([0-9,]+)円/時間", contract_text))
        tax_rates = set(re.findall(r"消費税率(?:は|[：:])?\s*([0-9.]+)%", contract_text))
        tax_rates.update(re.findall(r"項目:\s*消費税率\s*\n内容:\s*([0-9.]+)%", contract_text))
        actual_hours = set(re.findall(r"実績工数\s*(?:[：:]\s*|\n\s*)([0-9]+(?:\.[0-9]+)?)時間", report_text))
        actual_pretax = set(re.findall(r"税抜金額\s*\n?\s*[¥￥]?([0-9,]+)", report_text))
        if not all(len(values) == 1 for values in (rates, tax_rates, actual_hours, actual_pretax)):
            return None
        rate = Decimal(next(iter(rates)).replace(",", ""))
        tax_multiplier = Decimal("1") + Decimal(next(iter(tax_rates))) / Decimal("100")
        hours = Decimal(next(iter(actual_hours)))
        old_pretax = Decimal(next(iter(actual_pretax)).replace(",", ""))
        if old_pretax != rate * hours:
            return None
        new_rate = rate + Decimal(rate_hours_variance.group("rate_delta").replace(",", ""))
        new_hours = hours - Decimal(rate_hours_variance.group("hours_delta"))
        if new_hours <= 0:
            return None
        delta = (new_rate * new_hours - old_pretax) * tax_multiplier
        if delta != delta.to_integral_value():
            return None
        direction = "増加" if delta >= 0 else "減少"
        return f"{abs(int(delta)):,}円{direction}します。", [glossary_source["source_path"], contract_source["source_path"], report_source["source_path"]]
    return None


def build(args: argparse.Namespace) -> list[dict[str, Any]]:
    matrix = load_jsonl(args.matrix)
    submission = json.loads(args.submission_log.read_text(encoding="utf-8"))
    candidates = {str(item["index"]): item for item in submission["candidates"]}
    predictions = load_predictions(args.predictions)
    intermediate_state_path = args.intermediate / "build-state.json"
    lexical_state_path = args.lexical_index / "lexical-index-state.json"
    intermediate_state = json.loads(intermediate_state_path.read_text(encoding="utf-8"))
    sources = source_index(intermediate_state)
    pdf_observations = load_jsonl(args.pdf_observations) if args.pdf_observations is not None else None
    if pdf_observations is not None:
        for position, observation in enumerate(pdf_observations, 1):
            errors = validate_pdf_observation(observation)
            if errors:
                raise ValueError(f"invalid PDF observation at line {position}: {errors[0]}")
    provenance = {
        "builder": "answer-source-audit-builder", "builder_version": BUILDER_VERSION,
        "matrix_sha256": digest_file(args.matrix), "submission_log_sha256": digest_file(args.submission_log),
        "predictions_sha256": digest_file(args.predictions), "lexical_state_sha256": digest_file(lexical_state_path),
        "intermediate_state_sha256": digest_file(intermediate_state_path), "question_only_retrieval": True,
        "pdf_observations_sha256": digest_file(args.pdf_observations) if args.pdf_observations is not None else None,
        "gold_used": False, "public_score_used": False, "past_answer_used_as_evidence": False,
    }
    records = []
    for row in matrix:
        gap = row.get("capabilities", {}).get("primary_gap")
        qid = str(row.get("question_id"))
        candidate = candidates.get(qid)
        if gap not in GAPS or not candidate or candidate.get("strict_status") == "pass":
            continue
        question = row["question"]
        answer = predictions[qid]
        found = search(args.lexical_index, question, args.top_k, snippet_chars=args.snippet_chars)
        results = []
        answer_norm = normalized(answer)
        occurrences = 0
        for item in found["results"]:
            source = sources.get(item["document_id"])
            if source is None:
                raise ValueError(f"retrieval document not bound to source: {item['document_id']}")
            contains = bool(answer_norm and answer_norm != normalized("わかりません") and answer_norm in normalized(item["text"]))
            occurrences += int(contains)
            results.append({
                "rank": item["rank"], "score": item["score"], "search_unit_id": item["search_unit_id"],
                "document_id": item["document_id"], **source, "unit_type": item["unit_type"],
                "locator": item["locator"], "source_evidence_ids": item["source_evidence_ids"],
                "text": item["text"], "text_sha256": sha256_text(item["text"]),
                "contains_current_answer_exact": contains,
            })
        deterministic = deterministic_contract_field(question, sources, args.lexical_index)
        deterministic_method = "deterministic_contract_field"
        if deterministic is None:
            deterministic = deterministic_structured_source(question, sources, args.lexical_index, pdf_observations)
            deterministic_method = "deterministic_structured_source"
        if deterministic is not None:
            derived, verified_paths = deterministic
            same = normalized(derived) == normalized(answer)
            status = "verified" if same else "contradicted"
            verification = {
                "method": deterministic_method, "source_derived_answer": derived,
                "verified_source_paths": verified_paths,
                "reasons": [("unique_current_contract_and_unique_labeled_field" if deterministic_method == "deterministic_contract_field" else "unique_source_and_complete_structured_extraction") if same else "unique_source_field_differs_from_current_answer"],
                "proof_complete": True,
            }
        else:
            status, verification = classify(candidate, answer)
        core = {"question_id": qid, "question_sha256": sha256_text(question), "current_answer_sha256": sha256_text(answer), "audit_status": status}
        record = {
            "schema_version": "0.1", "record_type": "answer_source_audit",
            "audit_id": "asa_" + sha256_text(canonical_json(core))[:24], "question_id": qid,
            "question": question, "question_sha256": core["question_sha256"], "capability_gap": gap,
            "current_answer": answer, "current_answer_sha256": core["current_answer_sha256"],
            "audit_status": status, "verification": verification,
            "retrieval": {"query_sha256": sha256_text(question), "answer_used_in_query": False,
                          "result_count": len(results), "answer_exact_occurrences": occurrences, "results": results},
            "provenance": provenance,
        }
        records.append(record)
    return sorted(records, key=lambda value: int(value["question_id"]))


def atomic_write(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            for record in records:
                handle.write(canonical_json(record) + "\n")
            handle.flush(); os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True); raise


def render_report(records: list[dict[str, Any]]) -> str:
    ordered = sorted(records, key=lambda value: (
        {"contradicted": 0, "unverified": 1, "verified": 2}[value["audit_status"]],
        value["current_answer"] != "わかりません",
        int(value["question_id"]),
    ))
    counts = {status: sum(record["audit_status"] == status for record in records) for status in ("verified", "contradicted", "unverified")}
    lines = [
        "# 未完成回答・原本監査レポート", "",
        f"- 対象: {len(records)}問（意味条件判断と複数資料横断）",
        f"- verified: {counts['verified']}問", f"- contradicted: {counts['contradicted']}問", f"- unverified: {counts['unverified']}問", "",
        "> 検索一致だけではverifiedにしない。原本を一意に固定し、回答を再導出できたものだけを確定する。", "",
    ]
    for record in ordered:
        verification = record["verification"]
        lines.extend([
            f"## Q{record['question_id']} [{record['audit_status']}]", "",
            f"- 質問: {record['question']}", f"- 現回答: `{record['current_answer']}`",
            f"- 原本再導出: `{verification['source_derived_answer']}`" if verification["source_derived_answer"] is not None else "- 原本再導出: 未完了",
            f"- 能力分類: `{record['capability_gap']}`",
        ])
        if verification["verified_source_paths"]:
            lines.append("- 確定資料: " + " / ".join(f"`{path}`" for path in verification["verified_source_paths"]))
        else:
            candidates = []
            for result in record["retrieval"]["results"][:3]:
                if result["source_path"] not in candidates: candidates.append(result["source_path"])
            lines.append("- 次に確認する資料候補: " + (" / ".join(f"`{path}`" for path in candidates) or "なし"))
        lines.append("")
    return "\n".join(lines)


def atomic_write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(value); handle.flush(); os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True); raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--matrix", type=Path, required=True); parser.add_argument("--submission-log", type=Path, required=True)
    parser.add_argument("--predictions", type=Path, required=True); parser.add_argument("--intermediate", type=Path, required=True)
    parser.add_argument("--lexical-index", type=Path, required=True); parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--pdf-observations", type=Path)
    parser.add_argument("--top-k", type=int, default=10); parser.add_argument("--snippet-chars", type=int, default=1600)
    args = parser.parse_args()
    try:
        records = build(args); atomic_write(args.output, records)
        if args.report is not None: atomic_write_text(args.report, render_report(records))
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr); return 1
    print(canonical_json({"records": len(records), "output": str(args.output), "statuses": {key: sum(r["audit_status"] == key for r in records) for key in ("verified", "contradicted", "unverified")}}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
