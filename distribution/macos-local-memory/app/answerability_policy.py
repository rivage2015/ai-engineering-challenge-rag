"""Conservative, deterministic projection of ordinary answers and observations.

This module never promotes an OCR reading or a name match into a fact.  The
caller must validate the original record's IDs/contracts before calling it,
then rebuild the claim graph and audit the exact projected answer afterwards.
"""

from __future__ import annotations

from copy import deepcopy
from pathlib import PurePosixPath
import re
import unicodedata


POLICY_VERSION = "provisional-v1"
PROVISIONAL_MARKER = "[暫定読取]"
DEPENDENCY_KEYS = ("depends_on", "depends_on_item_ids", "dependency_ids")
WHOLE_RESULT_MARKERS = (
    "全件", "すべて", "全て", "全部", "漏れなく", "合計", "総数", "件数",
    "回数", "何回", "何件", "何枠", "比較", "順位", "ランキング", "平均",
    "差額", "割合", "何番", "順番", "順序", "手順",
)


def _label(item: dict) -> str:
    return str(item.get("label", ""))


def is_metadata_label(label: str) -> bool:
    return bool(re.search(
        r"ファイル名|資料名|(?:資料|ファイル|文書|出典).*(?:名称|記載場所|パス|場所)",
        label,
    ))


def _explicit_source_label(label: str) -> bool:
    """Source provenance, as distinct from a referenced target's file name."""
    return bool(re.search(
        r"出典|記載場所|記載(?:されている|された|のある|がある).*資料.*(?:名称|場所)|根拠となる資料",
        label,
    ))


def _source_key(value: str) -> str:
    # Preserve punctuation and path components.  Only restore the index's
    # Unicode/case spelling; never invent a suffix, directory or link target.
    return unicodedata.normalize("NFC", value).casefold()


def _text_key(value: str) -> str:
    return "".join(char for char in unicodedata.normalize("NFKC", value).casefold()
                   if char.isalnum() or "ぁ" <= char <= "龥")


def _provisional(packet: dict) -> bool:
    return any(line.lstrip().startswith(PROVISIONAL_MARKER)
               for line in str(packet.get("text", "")).splitlines())


def _field_ids(row: dict) -> list[str]:
    audit = row.get("audit", {})
    return list(dict.fromkeys(
        audit.get("supporting_packet_ids", [])
        + audit.get("competing_packet_ids", [])
        + row.get("retrieved_evidence_ids", [])
    ))


def _eligible(record: dict) -> bool:
    plan = record.get("question_plan")
    rows = record.get("field_runs")
    if not isinstance(plan, dict) or not isinstance(rows, list) or not rows:
        return False
    if plan.get("partial_answer_allowed") is False:
        return False
    operation = (record.get("question_evidence_graph") or {}).get("intent", {}).get("operation")
    if operation not in {None, "unknown"}:
        return False
    # Ignore the standard instruction footer ("資料にない手順...補わず").
    question = str(record.get("query", "")).split("回答に必要な内容：", 1)[0]
    labels = " ".join(_label(item) for item in plan.get("items", []))
    return not any(marker in question + labels for marker in WHOLE_RESULT_MARKERS)


def _metadata_binding(item: dict, audit: dict, packet_map: dict) -> dict | None:
    if not is_metadata_label(_label(item)):
        return None
    value = str(audit.get("supported_value", "")).strip()
    if not value:
        return None
    for evidence_id in audit.get("supporting_packet_ids", []):
        packet = packet_map[evidence_id]
        path = str(packet.get("path", ""))
        if not path:
            continue
        for source_field, candidate in (("path", path), ("basename", PurePosixPath(path).name)):
            if _source_key(value) == _source_key(candidate):
                return {
                    "field_id": str(item["item_id"]), "evidence_id": evidence_id,
                    "source_field": source_field, "value": candidate,
                    "path": path, "locator": deepcopy(packet.get("locator", {})),
                }
    # A source-provenance question can use the indexed source directly even
    # when the model appended prose/page notation.  A target file-name question
    # still requires a literal name/path match above.  Multiple source paths
    # remain unresolved here rather than selecting an arbitrary document.
    support = [packet_map[eid] for eid in audit.get("supporting_packet_ids", [])]
    paths = {str(packet.get("path", "")) for packet in support}
    if _explicit_source_label(_label(item)) and len(paths) == 1 and "" not in paths:
        packet = next((packet for packet in support if not _provisional(packet)), support[0])
        path = str(packet["path"])
        return {
            "field_id": str(item["item_id"]), "evidence_id": str(packet["evidence_id"]),
            "source_field": "path", "value": path,
            "path": path, "locator": deepcopy(packet.get("locator", {})),
        }
    return None


def _name_only_relation(item: dict, value: str, cited: list[dict]) -> bool:
    label = _label(item)
    relation = bool(re.search(r"含まれ|含む|行きます|行く|訪問|訪れ|行程|対象.*(?:か|否)", label))
    if not relation or not value:
        return False
    texts = [str(packet.get("text", "")).strip() for packet in cited]
    return bool(texts) and all(text.strip("（）()【】「」『』 \n") == value for text in texts)


def _unresolve(row: dict, reason: str) -> None:
    audit = row["audit"]
    audit.update({
        "verdict": "insufficient", "supported_value": "",
        "supporting_packet_ids": [], "reason_code": "unsupported_relation",
        "defect": reason, "missing_information": [reason],
    })


def _observation(field_id: str, packet: dict, kind: str) -> dict:
    return {
        "field_id": field_id, "kind": kind,
        "evidence_id": str(packet["evidence_id"]),
        "quote": str(packet.get("text", "")),
        "path": str(packet.get("path", "")),
        "locator": deepcopy(packet.get("locator", {})),
    }


def _reading_display_key(quote: str) -> str:
    # This is only a duplicate-display key.  The selected quote is not changed.
    # Keep question marks, ranges, negation and numbers significant.
    value = unicodedata.normalize("NFKC", quote)
    value = re.sub(r"^\s*\[暫定読取\]\s*(?:【資料】\s*)?", "", value)
    return re.sub(r"\s+", "", value)


def _deduplicate_observations(observations: list[dict]) -> list[dict]:
    result = []
    seen = {}
    for observation in observations:
        # Same wording on different pages/documents is independent evidence.
        locator = observation["locator"]
        page = locator.get("page_number") if isinstance(locator, dict) else None
        if observation["kind"] != "provisional_reading" or page is None:
            result.append(observation)
            continue
        key = (observation["field_id"], observation["path"], page,
               _reading_display_key(observation["quote"]))
        if key not in seen:
            seen[key] = len(result)
            result.append(observation)
        elif len(observation["quote"]) < len(result[seen[key]]["quote"]):
            result[seen[key]] = observation
    return result


def format_locator(locator: object) -> str:
    if not isinstance(locator, dict):
        return str(locator)
    names = {"page_number": "ページ", "object_index": "オブジェクト", "paragraph_index": "段落"}
    return "、".join(f"{names.get(key, key)} {value}" for key, value in sorted(locator.items()))


def render_answer(record: dict) -> str:
    """One canonical projection; validation compares this with the displayed text."""
    policy = record["answerability_policy"]
    item_map = {str(item["item_id"]): item for item in record["question_plan"]["items"]}
    lines = []
    metadata_fields = {entry["field_id"] for entry in policy["metadata_claims"]}
    for row in record["field_runs"]:
        audit = row["audit"]
        if audit.get("verdict") != "supported":
            continue
        field_id = str(audit["item_id"])
        confirmation = "確認済み（記載の出典）" if field_id in metadata_fields else "確認済み"
        lines.append(f"{confirmation} — {_label(item_map[field_id])}: {audit['supported_value']}")
    grouped_observations = {}
    for observation in policy["observations"]:
        key = (observation["kind"], observation["evidence_id"])
        grouped_observations.setdefault(key, []).append(observation)
    for observation_group in grouped_observations.values():
        observation = observation_group[0]
        kind = observation["kind"]
        lead = {
            "provisional_reading": "暫定の読み取り（内容は未確認）",
            "document_title": "本文中の資料タイトル・リンク表記（参照先ファイルの実在は未確認）",
            "related_excerpt": "関連する記載（質問への回答は未確認）",
        }[kind]
        labels = "、".join(dict.fromkeys(_label(item_map[entry["field_id"]]) for entry in observation_group))
        lines.append(f"{lead} — {labels}:\n「{observation['quote']}」")
        location = format_locator(observation["locator"])
        lines.append(f"出典: {observation['path']}" + (f"（{location}）" if location else ""))
    for row in record["field_runs"]:
        audit = row["audit"]
        if audit.get("verdict") == "supported":
            continue
        field_id = str(audit["item_id"])
        lines.append(f"未確認 — {_label(item_map[field_id])}: {audit.get('defect') or '直接支持する根拠を確認できませんでした。'}")
    return "\n\n".join(lines) if lines else "わかりません"


def prepare_record(record: dict, packets: list[dict], excluded_field_ids=()) -> dict:
    """Return a fresh record; preserve structured operations and invalid IDs."""
    result = deepcopy(record)
    if not _eligible(record):
        return result
    packet_map = {str(packet.get("evidence_id", "")): packet for packet in packets}
    if len(packet_map) != len(packets) or "" in packet_map:
        return result
    item_map = {str(item["item_id"]): item for item in result["question_plan"].get("items", [])}
    if any(str(row.get("audit", {}).get("item_id", "")) not in item_map
           or any(evidence_id not in packet_map for evidence_id in _field_ids(row))
           for row in result["field_runs"]):
        return result
    excluded = set(map(str, excluded_field_ids))
    observations, metadata = [], []
    for row in result["field_runs"]:
        audit = row["audit"]
        # Retry starts from the original audit, never from a promoted decision.
        if "original_audit" in row:
            row["audit"] = deepcopy(row["original_audit"])
            audit = row["audit"]
        else:
            row["original_audit"] = deepcopy(audit)
        field_id = str(audit["item_id"])
        item = item_map[field_id]
        support = [packet_map[evidence_id] for evidence_id in audit.get("supporting_packet_ids", [])]
        if field_id in excluded:
            _unresolve(row, "最終監査で支持を確認できなかったため、この項目の回答を保留しました。")
            continue
        binding = _metadata_binding(item, audit, packet_map) if audit.get("verdict") == "supported" else None
        if binding:
            metadata.append(binding)
            audit["supported_value"] = binding["value"]
            audit["supporting_packet_ids"] = [binding["evidence_id"]]
            continue
        value = str(audit.get("supported_value", "")).strip()
        if audit.get("verdict") == "supported" and is_metadata_label(_label(item)):
            # A literal title in the body is not proof of a file name/existence.
            titles = [packet for packet in support if value and value in str(packet.get("text", ""))]
            for packet in titles[:1]:
                observations.append(_observation(field_id, packet, "provisional_reading" if _provisional(packet) else "document_title"))
            _unresolve(row, "本文中の表記は確認できますが、求められた資料の実在するファイル名・場所は未確認です。" if titles else "資料の名称・場所を出典情報と照合できませんでした。")
        elif audit.get("verdict") == "supported" and _name_only_relation(item, value, support):
            observations.append(_observation(field_id, support[0], "related_excerpt"))
            _unresolve(row, "名称の記載だけでは、質問で求められた関係や行程への包含を確認できません。")
        elif audit.get("verdict") == "supported" and any(_provisional(packet) for packet in support):
            provisional = [packet for packet in support if _provisional(packet)]
            for packet in provisional[:2]:
                observations.append(_observation(field_id, packet, "provisional_reading"))
            confirmed = [packet for packet in support if not _provisional(packet)]
            if value and any(value in str(packet.get("text", "")) for packet in confirmed):
                audit["supporting_packet_ids"] = [packet["evidence_id"] for packet in confirmed]
            else:
                _unresolve(row, "暫定の読み取りが得られましたが、内容を確定する根拠は未確認です。")
        elif audit.get("verdict") == "supported" and any(
            _text_key(part) not in _text_key("\n".join(str(packet.get("text", "")) for packet in support))
            for part in re.split(r"[、,，;/／\n]+", value) if part.strip()
        ):
            _unresolve(row, "回答候補の値を引用された原文で確認できないため、この項目は未確認です。")
        if audit.get("verdict") != "supported" and not any(obs["field_id"] == field_id for obs in observations):
            # Show only a bounded, explicitly non-answer excerpt from the field's
            # own retrieval.  No numeric value or relation is inferred from it.
            candidates = [packet_map[eid] for eid in _field_ids(row)]
            candidates = [packet for packet in candidates if packet.get("text") and len(str(packet["text"])) <= 6000]
            if candidates:
                observations.append(_observation(field_id, candidates[0], "provisional_reading" if _provisional(candidates[0]) else "related_excerpt"))

    # Explicit dependencies are propagated to a fixed point (bounded by fields).
    for _ in result["field_runs"]:
        unresolved = {str(row["audit"]["item_id"]) for row in result["field_runs"] if row["audit"].get("verdict") != "supported"}
        changed = False
        for row in result["field_runs"]:
            field_id = str(row["audit"]["item_id"])
            item = item_map[field_id]
            dependencies = set()
            for key in DEPENDENCY_KEYS:
                value = item.get(key, [])
                dependencies.update(map(str, value if isinstance(value, list) else [value]))
            if row["audit"].get("verdict") == "supported" and dependencies & unresolved:
                _unresolve(row, "この項目が依存する別の項目が未確認のため、結論を保留しました。")
                metadata = [entry for entry in metadata if entry["field_id"] != field_id]
                changed = True
        if not changed:
            break
    observations = _deduplicate_observations(observations)
    supported = [row["audit"] for row in result["field_runs"] if row["audit"].get("verdict") == "supported"]
    unresolved = [row["audit"] for row in result["field_runs"] if row["audit"].get("verdict") != "supported"]
    if not observations and not metadata and not excluded and all(
        row["audit"] == original["audit"]
        for row, original in zip(result["field_runs"], record["field_runs"])
    ):
        return deepcopy(record)
    result["answerability_policy"] = {
        "version": POLICY_VERSION, "applied": True,
        "confirmed_field_ids": [str(audit["item_id"]) for audit in supported],
        "unresolved_field_ids": [str(audit["item_id"]) for audit in unresolved],
        "reference_only": not supported and bool(observations),
        "observations": observations, "metadata_claims": metadata,
        "excluded_field_ids": sorted(excluded),
    }
    answer = result["answer"]
    incomplete = bool(unresolved or observations)
    answer.update({
        "answer_status": "answered" if supported else "insufficient",
        "answer_mode": "qualified" if incomplete and (supported or observations) else ("grounded" if supported else "insufficient"),
        "evidence_ids": list(dict.fromkeys(eid for audit in supported for eid in audit["supporting_packet_ids"])),
        "diagnostic_evidence_ids": list(dict.fromkeys(obs["evidence_id"] for obs in observations)),
        "basis_summary": "確認済みの項目と、原文を保持した参考情報・未確認の項目を分けて表示しました。",
        "uncertainties": [audit.get("defect", "") for audit in unresolved],
        "non_answer_reason": {"code": "unsupported_relation" if not supported else "none", "explanation": "質問への確定回答は未確認です。" if not supported else ""},
        "needed_information": [audit.get("defect", "") for audit in unresolved],
        "follow_up_question": "未確認の項目を直接支持する資料を追加しますか？" if not supported else "", "reconsideration_condition": "未確認の項目を直接支持する資料が確認された後。" if incomplete else "",
        "verification_reminder": "暫定の読み取り・関連記載は、質問への確定回答ではありません。" if observations else "",
    })
    answer["answer"] = render_answer(result)
    return result


def validate_projection(record: dict, packets: list[dict]) -> list[dict]:
    """Verify metadata, literal observations, ID bindings and final projection."""
    policy = record.get("answerability_policy")
    if not isinstance(policy, dict) or not policy.get("applied"):
        return []
    failures = []
    def fail(detail: str) -> None:
        failures.append({"code": "answerability_projection_invalid", "detail": detail})
    if policy.get("version") != POLICY_VERSION or not _eligible(record):
        fail("回答方針の版または適用対象が一致しません。")
        return failures
    packet_map = {str(packet.get("evidence_id", "")): packet for packet in packets}
    rows = {str(row["audit"]["item_id"]): row for row in record["field_runs"]}
    items = {str(item["item_id"]): item for item in record["question_plan"]["items"]}
    try:
        for entry in policy["metadata_claims"]:
            packet = packet_map[entry["evidence_id"]]
            row = rows[entry["field_id"]]
            path = str(packet.get("path", ""))
            expected = path if entry["source_field"] == "path" else PurePosixPath(path).name
            original_binding = _metadata_binding(
                items[entry["field_id"]], row.get("original_audit", row["audit"]), packet_map,
            )
            if (entry["source_field"] not in {"path", "basename"}
                or entry != original_binding
                or not path or entry["value"] != expected
                or entry["path"] != path or entry["locator"] != packet.get("locator", {})
                or not is_metadata_label(_label(items[entry["field_id"]]))
                or row["audit"].get("verdict") != "supported"
                or row["audit"].get("supported_value") != expected
                or row["audit"].get("supporting_packet_ids") != [entry["evidence_id"]]
                or entry["evidence_id"] not in _field_ids({**row, "audit": row.get("original_audit", row["audit"])})):
                fail("出典の値・項目・Evidenceの対応が一致しません。")
        for entry in policy["observations"]:
            packet = packet_map[entry["evidence_id"]]
            row = rows[entry["field_id"]]
            if (entry["quote"] != str(packet.get("text", ""))
                or entry["path"] != str(packet.get("path", ""))
                or entry["locator"] != packet.get("locator", {})
                or entry["kind"] not in {"provisional_reading", "related_excerpt", "document_title"}
                or (_provisional(packet) and entry["kind"] != "provisional_reading")
                or (entry["kind"] == "provisional_reading" and not _provisional(packet))
                or (entry["kind"] == "document_title" and not is_metadata_label(_label(items[entry["field_id"]])))
                or entry["evidence_id"] not in _field_ids({**row, "audit": row.get("original_audit", row["audit"])})):
                fail("参考情報の原文・出典・項目との対応が一致しません。")
        confirmed = [str(row["audit"]["item_id"]) for row in record["field_runs"] if row["audit"].get("verdict") == "supported"]
        unresolved = [str(row["audit"]["item_id"]) for row in record["field_runs"] if row["audit"].get("verdict") != "supported"]
        confirmed_ids = list(dict.fromkeys(
            evidence_id for row in record["field_runs"]
            if row["audit"].get("verdict") == "supported"
            for evidence_id in row["audit"].get("supporting_packet_ids", [])
        ))
        observation_ids = list(dict.fromkeys(entry["evidence_id"] for entry in policy["observations"]))
        if record["answer"].get("evidence_ids") != confirmed_ids or record["answer"].get("diagnostic_evidence_ids") != observation_ids:
            fail("確定根拠と参考情報のEvidence一覧が各項目への対応と一致しません。")
        if policy["confirmed_field_ids"] != confirmed or policy["unresolved_field_ids"] != unresolved:
            fail("確認済み・未確認の項目一覧が一致しません。")
        if policy["reference_only"] != (not confirmed and bool(policy["observations"])):
            fail("参考情報のみの状態が一致しません。")
        if policy["reference_only"] and (record["answer"].get("answer_status") != "insufficient" or record["answer"].get("evidence_ids")):
            fail("参考情報だけの回答を完了として扱えません。")
        if (unresolved or policy["observations"]) and record["answer"].get("answer_mode") == "grounded":
            fail("未確認または暫定情報がある回答を全項目確認済みとして扱えません。")
        if record["answer"].get("answer") != render_answer(record):
            fail("表示する文章が検証済みの項目・引用からの投影と一致しません。")
    except (KeyError, TypeError, ValueError):
        fail("回答方針の出典参照または構造が不正です。")
    return failures
