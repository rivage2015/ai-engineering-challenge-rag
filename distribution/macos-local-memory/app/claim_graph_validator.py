#!/usr/bin/env python3
"""Build and deterministically validate a small claim graph before LLM audit."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from datetime import date
from decimal import Decimal, InvalidOperation


CURRENT_MARKERS = ("現在", "今は", "いまは", "現在は")
PAST_MARKERS = ("過去", "以前", "かつて", "住んでいました", "住んでいた", "当時")
ALL_MARKERS = ("すべて", "全て", "全部", "全件", "漏れなく", "すべて挙げ")
PERSON_MARKERS = ("人物", "本人", "パイロット", "質問者", "担当者", "誰")
NAME_MARKERS = ("名前", "氏名", "パイロットネーム", "氏名は", "名前は")
PROVISIONAL_MARKER = "[暫定読取]"
COUNT_MARKERS = ("何回", "回数", "何枠", "枠数", "何件", "件数", "総数", "合計")
EXPLICIT_PERIOD = re.compile(r"20\d{2}\s*年\s*(?:1[0-2]|0?[1-9])\s*月")
SAVED_VALUE_ANNOTATION = re.compile(
    r"\s+\[保存値[^\]]*[:：]\s*"
    r"[-+]?(?:\d+(?:\.\d+)?|\.\d+)\s*\]\s*$"
)
FORMULA_OPERATOR_CHARS = frozenset("=+-*/^&%(),:<>!")


def normalize(value: object) -> str:
    return "".join(
        char for char in unicodedata.normalize("NFKC", str(value)).casefold()
        if char.isalnum() or "ぁ" <= char <= "龥"
    )


def stable_hash(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def infer_time_scope(query: str, label: str) -> str:
    combined = query + " " + label
    if EXPLICIT_PERIOD.search(unicodedata.normalize("NFKC", combined)):
        return "specified_period"
    if any(marker in combined for marker in ("過去", "以前", "かつて", "住んでいた")):
        return "past"
    if any(marker in combined for marker in CURRENT_MARKERS):
        return "current"
    return "unspecified"


def _quoted_formula_end(value: str, start: int, quote: str) -> int:
    index = start + 1
    while index < len(value):
        if value[index] != quote:
            index += 1
            continue
        if index + 1 < len(value) and value[index + 1] == quote:
            index += 2
            continue
        return index + 1
    return len(value)


def _structured_reference_end(value: str, start: int) -> int:
    depth = 1
    index = start + 1
    while index < len(value):
        if value[index] in {'"', "'"}:
            index = _quoted_formula_end(value, index, value[index])
            continue
        if value[index] == "[":
            depth += 1
        elif value[index] == "]":
            depth -= 1
            if depth == 0:
                return index + 1
        index += 1
    return len(value)


def formula_projection(value: object) -> str | None:
    """Canonicalize formula syntax without erasing operand whitespace."""
    if not isinstance(value, str):
        return None
    formula = SAVED_VALUE_ANNOTATION.sub("", value.strip())
    if not formula or unicodedata.normalize("NFKC", formula[0]) != "=":
        return None
    tokens: list[tuple[str, str]] = []
    index = 0
    while index < len(formula):
        char = formula[index]
        if char in {'"', "'"}:
            end = _quoted_formula_end(formula, index, char)
            tokens.append(("protected", formula[index:end]))
            index = end
            continue
        if char == "[":
            end = _structured_reference_end(formula, index)
            tokens.append(("protected", formula[index:end]))
            index = end
            continue
        for normalized_char in unicodedata.normalize("NFKC", char).casefold():
            kind = "space" if normalized_char.isspace() else "char"
            if kind != "space" or not tokens or tokens[-1][0] != "space":
                tokens.append((kind, " " if kind == "space" else normalized_char))
        index += 1

    canonical: list[str] = []
    for token_index, (kind, token) in enumerate(tokens):
        if kind != "space":
            canonical.append(token)
            continue
        previous = tokens[token_index - 1] if token_index else None
        following = (
            tokens[token_index + 1]
            if token_index + 1 < len(tokens) else None
        )
        if previous is None or following is None:
            continue
        if (
            previous[0] == "char" and previous[1] in FORMULA_OPERATOR_CHARS
        ) or (
            following[0] == "char" and following[1] in FORMULA_OPERATOR_CHARS
        ):
            continue
        canonical.append(" ")
    return "".join(canonical)


def decimal_projection(value: object) -> tuple[bool, Decimal | None]:
    if not isinstance(value, str):
        return False, None
    try:
        parsed = Decimal(unicodedata.normalize("NFKC", value).strip())
    except (InvalidOperation, ValueError):
        return False, None
    return True, parsed if parsed.is_finite() else None


def normalized_value_text(value: object) -> str:
    if not isinstance(value, str):
        return ""
    normalized = "".join(
        char
        for char in unicodedata.normalize("NFKC", value)
        if unicodedata.category(char) != "Cf"
    )
    return re.sub(
        r"\s+", " ", normalized.strip()
    ).casefold()


ANSWER_NUMERIC_TOKEN = re.compile(
    r"(?<![0-9A-Za-z_.,])[-+]?"
    r"(?:(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?|\.\d+)"
    r"(?![0-9A-Za-z_.,])"
)
ANSWER_DATE_TOKEN = re.compile(
    r"(?<!\d)(?P<year>\d{4})\s*(?:年\s*|[-/.]\s*)"
    r"(?P<month>\d{1,2})\s*(?:月\s*|[-/.]\s*)"
    r"(?P<day>\d{1,2})\s*日?(?!\d)"
)
ANSWER_FORMULA_TOKEN = re.compile(r"=[^。.!！?？\n]+")


def _verified_scalar_occurrences(
    answer: object, value: object,
) -> list[tuple[int, int]]:
    """Locate semantically complete occurrences of one verified scalar."""
    if not isinstance(answer, str) or not isinstance(value, str):
        return []
    answer_text = normalized_value_text(answer)
    expected_formula = formula_projection(value)
    if expected_formula is not None:
        matches = []
        for match in ANSWER_FORMULA_TOKEN.finditer(answer_text):
            candidate = re.sub(
                r"\s*(?:」|』|”|\")?\s*"
                r"(?:です|でした|である|となります)\s*$",
                "",
                match.group(0),
            ).strip()
            candidate = candidate.rstrip(" 」』”\"")
            if formula_projection(candidate) == expected_formula:
                matches.append(match.span())
        return matches
    try:
        expected_date = date.fromisoformat(
            unicodedata.normalize("NFKC", value).strip()
        )
    except ValueError:
        expected_date = None
    if expected_date is not None:
        matches = []
        for match in ANSWER_DATE_TOKEN.finditer(answer_text):
            try:
                observed_date = date(
                    int(match.group("year")),
                    int(match.group("month")),
                    int(match.group("day")),
                )
            except ValueError:
                continue
            if observed_date == expected_date:
                matches.append(match.span())
        return matches
    expected_is_decimal, expected_decimal = decimal_projection(value)
    if expected_is_decimal:
        if expected_decimal is None:
            return []
        matches = []
        for match in ANSWER_NUMERIC_TOKEN.finditer(answer_text):
            try:
                observed = Decimal(match.group(0).replace(",", ""))
            except InvalidOperation:
                continue
            if observed.is_finite() and observed == expected_decimal:
                matches.append(match.span())
        return matches

    value_text = normalized_value_text(value)
    if not value_text:
        return []
    start = 0
    matches = []
    allowed_prefix_particles = frozenset("はがをにへとでの:：")
    allowed_prefixes = (
        "ただし", "しかし", "一方", "なお", "また", "そして",
    )
    allowed_suffixes = (
        "さん", "氏", "様", "は", "が", "を", "に", "へ", "と", "の",
        "です", "でした", "である", "では", "じゃ", "でない",
        "となります",
    )
    while True:
        position = answer_text.find(value_text, start)
        if position < 0:
            return matches
        before = answer_text[position - 1] if position else ""
        after = answer_text[position + len(value_text):]
        before_ok = (
            not before
            or (not before.isalnum() and before not in "_-" )
            or before in allowed_prefix_particles
            or any(
                answer_text[:position].endswith(prefix)
                for prefix in allowed_prefixes
            )
        )
        next_char = after[:1]
        after_ok = (
            not next_char
            or (not next_char.isalnum() and next_char not in "_-" )
            or any(after.startswith(suffix) for suffix in allowed_suffixes)
        )
        if before_ok and after_ok:
            matches.append((position, position + len(value_text)))
        start = position + max(1, len(value_text))


def verified_scalar_is_in_answer(answer: object, value: object) -> bool:
    """Require a complete verified scalar, not a substring of another value."""
    return bool(_verified_scalar_occurrences(answer, value))


def record_lookup_value_is_negated(
    answer: object,
    value: object,
    *,
    allow_current_contrast: bool = False,
) -> bool:
    """Detect negated complete occurrences, allowing an explicit time contrast."""
    if not isinstance(answer, str) or not isinstance(value, str):
        return False
    answer_text = normalized_value_text(answer)
    occurrences = _verified_scalar_occurrences(answer, value)
    if not occurrences:
        return False
    negated_contexts = []
    affirmative_count = 0
    for position, end in occurrences:
        before = answer_text[max(0, position - 32):position]
        after = answer_text[end:end + 40]
        after_clause = re.split(r"[。.!！?？\n]", after, maxsplit=1)[0]
        negated = bool(re.search(
            r"(?:not|isn['’]t|wasn['’]t|aren['’]t|weren['’]t)\s*$",
            before,
        ) or re.search(
            r"(?:ではありませんでした|ではございません|ではありません|"
            r"ではなかった|ではない|じゃありません|じゃない|"
            r"ではなく|でなく|でない|以外|"
            r"is\s+not|was\s+not|isn['’]t|wasn['’]t|not\b)",
            after_clause,
        ))
        if negated:
            negated_contexts.append(before + after_clause)
        else:
            affirmative_count += 1
    if not negated_contexts:
        return False
    if allow_current_contrast and affirmative_count:
        current_markers = tuple(
            normalized_value_text(marker) for marker in CURRENT_MARKERS
        ) + ("currently", "now")
        if all(
            any(marker and marker in context for marker in current_markers)
            for context in negated_contexts
        ):
            return False
    return True


ANSWER_ALTERNATIVE_SURFACE = re.compile(
    r"(?:または|もしくは|あるいは|正しくは|実際は|本当は|"
    r"(?<![0-9a-z])or(?![0-9a-z])|rather\s+than|instead|"
    r"(?<![0-9a-z])but(?![0-9a-z])|だが|しかし)",
    re.IGNORECASE,
)


def numeric_answer_has_conflicting_alternative(
    answer: object, value: object,
) -> bool:
    """Reject an explicitly alternative numeric value in the same clause."""
    expected_is_decimal, expected = decimal_projection(value)
    if not isinstance(answer, str) or not expected_is_decimal or expected is None:
        return False
    for clause in re.split(r"[。!！?？\n]", normalized_value_text(answer)):
        if (
            not ANSWER_ALTERNATIVE_SURFACE.search(clause)
            or not verified_scalar_is_in_answer(clause, str(value))
        ):
            continue
        observed = []
        for match in ANSWER_NUMERIC_TOKEN.finditer(clause):
            try:
                parsed = Decimal(match.group(0).replace(",", ""))
            except InvalidOperation:
                continue
            if parsed.is_finite():
                observed.append(parsed)
        if any(parsed != expected for parsed in observed):
            return True
    return False


def record_lookup_value_matches(observed: object, expected: object) -> bool:
    """Match formulas, then finite decimals, then punctuation-preserving text."""
    observed_formula = formula_projection(observed)
    expected_formula = formula_projection(expected)
    if observed_formula is not None or expected_formula is not None:
        return (
            observed_formula is not None
            and expected_formula is not None
            and observed_formula == expected_formula
        )
    observed_is_decimal, observed_decimal = decimal_projection(observed)
    expected_is_decimal, expected_decimal = decimal_projection(expected)
    if observed_is_decimal or expected_is_decimal:
        return (
            observed_is_decimal
            and expected_is_decimal
            and observed_decimal is not None
            and expected_decimal is not None
            and observed_decimal == expected_decimal
        )
    observed_identity = normalized_value_text(observed)
    return bool(observed_identity) and observed_identity == normalized_value_text(
        expected
    )


def record_lookup_field_bindings(record: dict) -> dict[str, dict]:
    """Return verified record-lookup branches keyed by question-plan item ID."""
    artifact = record.get("question_evidence_graph")
    validation = record.get("question_evidence_graph_validation")
    if not isinstance(artifact, dict) or not isinstance(validation, dict):
        return {}
    if artifact.get("status") != "ready" or validation.get("status") != "pass":
        return {}
    intent = artifact.get("intent")
    if not isinstance(intent, dict) or intent.get("operation") != "record_lookup":
        return {}
    body = {
        key: value for key, value in artifact.items()
        if key not in {"artifact_hash", "artifact_id"}
    }
    expected_hash = stable_hash(body)
    if (
        artifact.get("artifact_hash") != expected_hash
        or artifact.get("artifact_id") != f"qeg_{expected_hash[:24]}"
    ):
        return {}
    bindings: dict[str, dict] = {}
    branches = artifact.get("branches")
    if not isinstance(branches, list):
        return {}
    for branch in branches:
        if not isinstance(branch, dict):
            return {}
        item_id = str(branch.get("item_id", "")).strip()
        if not item_id or item_id in bindings:
            return {}
        bindings[item_id] = branch
    return bindings


def expected_record_lookup_answer(record: dict) -> str | None:
    """Rebuild the deterministic projection emitted by the answer engine."""
    answer = record.get("answer")
    plan = record.get("question_plan")
    field_runs = record.get("field_runs")
    if (
        not isinstance(answer, dict)
        or answer.get("answer_status") != "answered"
        or not isinstance(plan, dict)
        or not isinstance(plan.get("items"), list)
        or not isinstance(field_runs, list)
    ):
        return None
    item_by_id = {
        str(item.get("item_id", "")): item
        for item in plan["items"]
        if isinstance(item, dict) and str(item.get("item_id", "")).strip()
    }
    question_graph = record.get("question_evidence_graph")
    graph_intent = (
        question_graph.get("intent")
        if isinstance(question_graph, dict) else None
    )
    graph_operation = (
        graph_intent.get("operation")
        if isinstance(graph_intent, dict) else None
    )
    for item in item_by_id.values():
        label = item.get("label")
        if (
            not isinstance(label, str)
            or not label.strip()
            or len(label) > 80
            or any(
                unicodedata.category(char) in {"Cc", "Zl", "Zp"}
                for char in label
            )
        ):
            return None
        if graph_operation == "aggregate_count" and label not in {
            "回数", "稼働回数",
        }:
            return None
    supported = []
    unresolved = []
    for row in field_runs:
        audit = row.get("audit") if isinstance(row, dict) else None
        if not isinstance(audit, dict):
            return None
        item_id = str(audit.get("item_id", ""))
        item = item_by_id.get(item_id)
        if item is None:
            return None
        if audit.get("verdict") == "supported":
            supported.append((item, audit))
        else:
            unresolved.append((item, audit))
    confirmed_lines = [
        f"- {item.get('label', '')}: {audit.get('supported_value', '')}"
        for item, audit in supported
    ]
    unresolved_lines = [
        f"- {item.get('label', '')}: 確認できませんでした（{audit.get('defect', '')}）"
        for item, audit in unresolved
    ]
    parts = ["確認できた内容:\n" + "\n".join(confirmed_lines)]
    if unresolved_lines:
        parts.append("確認できなかった項目:\n" + "\n".join(unresolved_lines))
    return "\n\n".join(parts)


def infer_entity_type(
    query: str,
    label: str,
    record_lookup_binding: dict | None = None,
) -> str:
    if record_lookup_binding is not None:
        is_decimal, decimal = decimal_projection(record_lookup_binding.get("value"))
        return (
            "numeric_value"
            if is_decimal and decimal is not None
            else "text_value"
        )
    combined = query + " " + label
    if any(marker in combined for marker in COUNT_MARKERS):
        return "numeric_count"
    if "氏名" in combined or (
        any(marker in combined for marker in NAME_MARKERS)
        and any(marker in combined for marker in PERSON_MARKERS)
    ):
        return "person_name"
    if "場所" in combined or "どこ" in combined or "居住地" in combined:
        return "place"
    if "誰" in combined or "同居者" in combined:
        return "person_or_relation"
    return "text_value"


def build_question_contract(
    query: str,
    plan: dict,
    *,
    record_lookup_bindings: dict[str, dict] | None = None,
) -> dict:
    record_lookup_bindings = record_lookup_bindings or {}
    items = []
    for item in plan.get("items", []):
        field_id = str(item.get("item_id", ""))
        label = str(item.get("label", ""))
        binding = record_lookup_bindings.get(field_id)
        contract_item = {
            "field_id": field_id,
            "label": label,
            "required_claim": str(item.get("required_claim", "")),
            "required": bool(item.get("required", False)),
            "entity_type": infer_entity_type(query, label, binding),
            "time_scope": infer_time_scope(query, label),
            "require_all": any(marker in query for marker in ALL_MARKERS),
        }
        items.append(contract_item)
    body = {"query": query, "items": items}
    return {"contract_version": "1.0", "contract_hash": stable_hash(body), **body}


def split_values(value: str) -> list[str]:
    parts = [part.strip(" \t-・") for part in re.split(r"[、,，;/／\n]+", value) if part.strip(" \t-・")]
    return parts or ([value.strip()] if value.strip() else [])


def non_provisional_text(value: str) -> str:
    """Return only text that is not labelled as a provisional reading.

    A direct provisional Evidence packet starts with the marker and is wholly
    provisional. A derived image packet can contain multiple OCR lines; in
    that form only each marker-prefixed line is provisional, while independently
    accepted lines in the same packet remain eligible support.
    """
    lines = value.splitlines() or [value]
    first_nonempty = next((line for line in lines if line.strip()), "")
    if first_nonempty.lstrip().startswith(PROVISIONAL_MARKER):
        return ""
    accepted = []
    for line in lines:
        prefix, marker, _provisional = line.partition(PROVISIONAL_MARKER)
        accepted.append(prefix if marker else line)
    return "\n".join(accepted)


def packet_has_provisional_reading(value: str) -> bool:
    """Return whether a packet contains any explicitly provisional OCR line.

    SearchUnits keep high and provisional image readings in separate packets.
    A supported claim therefore never needs a provisional packet in its support
    set. Keeping those packets diagnostic also prevents relation laundering.
    """
    return any(
        line.lstrip().startswith(PROVISIONAL_MARKER)
        for line in value.splitlines()
    )


def numeric_value(value: object) -> Decimal | None:
    match = re.search(r"[-+]?(?:\d+(?:\.\d+)?|\.\d+)", unicodedata.normalize("NFKC", str(value)))
    if not match:
        return None
    try:
        parsed = Decimal(match.group(0))
    except InvalidOperation:
        return None
    return parsed if parsed.is_finite() else None


def validate_question_graph_binding(
    record: dict,
    contract: dict,
    graph: dict,
    packet_map: dict[str, str],
    failures: list[dict],
    warnings: list[dict],
) -> None:
    """Bind numeric count claims to the verified pre-answer aggregate graph."""
    count_fields = {
        item.get("field_id") for item in contract.get("items", [])
        if item.get("entity_type") == "numeric_count"
    }
    if not count_fields:
        return
    artifact = record.get("question_evidence_graph")
    validation = record.get("question_evidence_graph_validation")
    if not isinstance(artifact, dict) or not isinstance(validation, dict):
        failures.append({
            "code": "question_graph_missing",
            "detail": "回数主張に必要な回答前Question Evidence Graphがありません。",
        })
        return
    if artifact.get("status") == "unsupported" and validation.get("status") == "not_applicable":
        failures.append({
            "code": "structured_aggregate_graph_required",
            "detail": "回数主張に必要な構造化集計Graphがありません。",
        })
        return
    if artifact.get("status") != "ready" or validation.get("status") != "pass":
        failures.append({
            "code": "question_graph_not_verified",
            "detail": f"集計Graph未検証: {artifact.get('reason', '')}",
        })
        return
    body = {key: value for key, value in artifact.items() if key not in {"artifact_hash", "artifact_id"}}
    expected_hash = stable_hash(body)
    if artifact.get("artifact_hash") != expected_hash or artifact.get("artifact_id") != f"qeg_{expected_hash[:24]}":
        failures.append({"code": "question_graph_hash_mismatch", "detail": "集計Graphのhashが一致しません。"})
        return
    selection = artifact.get("selection") or {}
    stored_binding = artifact.get("stored_graph_binding")
    record_index = record.get("index") if isinstance(record.get("index"), dict) else {}
    record_graph_sha256 = record_index.get("graph_sha256")
    if stored_binding is not None or record_graph_sha256 is not None:
        if not isinstance(stored_binding, dict):
            failures.append({
                "code": "stored_graph_binding_missing",
                "detail": "回数主張が保存済みGraphのTraversalへ接続されていません。",
            })
            return
        binding_body = {
            key: value for key, value in stored_binding.items()
            if key != "traversal_sha256"
        }
        if stored_binding.get("traversal_sha256") != stable_hash(binding_body):
            failures.append({
                "code": "stored_graph_traversal_hash_mismatch",
                "detail": "保存済みGraph Traversalのhashが一致しません。",
            })
        if stored_binding.get("graph_sha256") != record_graph_sha256:
            failures.append({
                "code": "stored_graph_snapshot_mismatch",
                "detail": "質問Graphと回答記録が異なる保存済みGraphを参照しています。",
            })
        if not stored_binding.get("traversed_relation_ids"):
            failures.append({
                "code": "stored_graph_relations_missing",
                "detail": "回数主張へ到達する保存済みRelation IDがありません。",
            })
    saved_value = numeric_value(selection.get("saved_value"))
    recomputed_value = numeric_value(selection.get("recomputed_value"))
    if saved_value is None or recomputed_value is None or saved_value != recomputed_value:
        failures.append({
            "code": "question_graph_saved_recomputed_mismatch",
            "detail": "保存値と行範囲の再集計値が一致しません。",
        })
    selected_value = numeric_value(selection.get("value"))
    if (
        selected_value is None
        or recomputed_value is None
        or selected_value != recomputed_value
    ):
        failures.append({
            "code": "question_graph_selection_recomputed_mismatch",
            "detail": "回答用の選択値と行範囲の再集計値が一致しません。",
        })
    validation_ids = set(selection.get("validation_evidence_ids", []))
    missing_ids = sorted(validation_ids - set(packet_map))
    if missing_ids:
        failures.append({
            "code": "question_graph_evidence_missing",
            "detail": f"集計GraphのEvidenceが不足: {missing_ids[:6]}",
        })
    expected_value = selected_value
    mandatory_ids = set(selection.get("mandatory_aggregation_evidence_ids", []))
    for claim in graph.get("claims", []):
        if claim.get("field_id") not in count_fields:
            continue
        claim_id = claim.get("claim_id", "")
        actual_value = numeric_value(claim.get("value"))
        if expected_value is None or actual_value != expected_value:
            failures.append({
                "code": "question_graph_value_mismatch",
                "claim_id": claim_id,
                "detail": "回数主張が機械的に再集計した値と一致しません。",
            })
        if not (set(claim.get("evidence_ids", [])) & mandatory_ids):
            failures.append({
                "code": "question_graph_evidence_escape",
                "claim_id": claim_id,
                "detail": "回数主張が検証済みの合計Evidenceを参照していません。",
            })


def validate_record_lookup_binding(
    graph: dict,
    bindings: dict[str, dict],
    failures: list[dict],
) -> None:
    """Bind each record claim to its verified branch scalar and Evidence path."""
    if not bindings:
        return
    for claim in graph.get("claims", []):
        field_id = str(claim.get("field_id", ""))
        binding = bindings.get(field_id)
        if binding is None:
            continue
        claim_id = str(claim.get("claim_id", ""))
        expected_value = binding.get("value", "")
        actual_value = claim.get("value", "")
        if not record_lookup_value_matches(actual_value, expected_value):
            failures.append({
                "code": "record_lookup_value_mismatch",
                "claim_id": claim_id,
                "detail": "主張値が検証済みのレコード項目値と一致しません。",
            })
        selected_ids = {
            str(evidence_id)
            for evidence_id in binding.get("selected_evidence_ids", [])
        }
        if not (set(claim.get("evidence_ids", [])) & selected_ids):
            failures.append({
                "code": "record_lookup_evidence_escape",
                "claim_id": claim_id,
                "detail": "主張が検証済みのレコード項目Evidenceを参照していません。",
            })


def build_claim_graph(record: dict, packets: list[dict], contract: dict | None = None) -> dict:
    contract = contract or build_question_contract(
        record.get("query", ""),
        record.get("question_plan", {}),
        record_lookup_bindings=record_lookup_field_bindings(record),
    )
    packet_ids = {str(packet.get("evidence_id", "")) for packet in packets}
    nodes = [{"node_id": "Q1", "node_type": "question", "value": record.get("query", "")}]
    edges = []
    claims = []
    item_by_id = {item["field_id"]: item for item in contract["items"]}
    for item in contract["items"]:
        nodes.append({"node_id": item["field_id"], "node_type": "field", "value": item["required_claim"]})
        edges.append({"edge_id": f"R_{item['field_id']}", "source": "Q1", "predicate": "requires", "target": item["field_id"]})
    for packet in packets:
        evidence_id = str(packet.get("evidence_id", ""))
        nodes.append({"node_id": evidence_id, "node_type": "evidence", "value": packet.get("text", "")})
    for row in record.get("field_runs", []):
        audit = row.get("audit", {})
        if audit.get("verdict") != "supported":
            continue
        field_id = str(audit.get("item_id", ""))
        value = str(audit.get("supported_value", "")).strip()
        evidence_ids = list(dict.fromkeys(str(value) for value in audit.get("supporting_packet_ids", [])))
        claim_id = f"C{len(claims) + 1}"
        value_id = f"V{len(claims) + 1}"
        contract_item = item_by_id.get(field_id, {})
        claim = {
            "claim_id": claim_id,
            "field_id": field_id,
            "predicate": contract_item.get("label", ""),
            "value": value,
            "value_parts": split_values(value),
            "entity_type": contract_item.get("entity_type", "text_value"),
            "time_scope": contract_item.get("time_scope", "unspecified"),
            "evidence_ids": evidence_ids,
        }
        claims.append(claim)
        nodes.append({
            "node_id": claim_id,
            "node_type": "claim",
            "field_id": field_id,
            "predicate": claim["predicate"],
        })
        nodes.append({"node_id": value_id, "node_type": "value", "value": value})
        edges.append({
            "edge_id": f"A_{claim_id}", "source": field_id,
            "predicate": "answered_by", "target": claim_id,
        })
        edges.append({
            "edge_id": f"V_{claim_id}", "source": claim_id,
            "predicate": "has_value", "target": value_id,
        })
        for evidence_id in evidence_ids:
            if evidence_id in packet_ids:
                edges.append({"edge_id": f"S_{claim_id}_{evidence_id}", "source": evidence_id, "predicate": "supports", "target": claim_id})
    body = {"contract_hash": contract["contract_hash"], "nodes": nodes, "edges": edges, "claims": claims}
    return {"artifact_version": "1.1", "artifact_hash": stable_hash(body), **body}


def _name_relation_is_explicit(value: str, text: str) -> bool:
    value_key = normalize(value)
    if not value_key:
        return False
    text_key = normalize(text)
    position = text_key.find(value_key)
    if position < 0:
        return False
    prefix = text_key[max(0, position - 60):position]
    return any(normalize(marker) in prefix for marker in NAME_MARKERS)


def _time_relation_conflicts(time_scope: str, value: str, text: str) -> bool:
    if time_scope == "unspecified":
        return False
    value_key = normalize(value)
    text_key = normalize(text)
    position = text_key.find(value_key)
    if position < 0:
        return False
    before = text_key[:position]
    if time_scope == "past":
        last_current = max((before.rfind(normalize(marker)) for marker in CURRENT_MARKERS), default=-1)
        last_past = max((before.rfind(normalize(marker)) for marker in PAST_MARKERS), default=-1)
        return last_current > last_past
    if time_scope == "current":
        window = text_key[max(0, position - 80):position + len(value_key) + 80]
        return any(normalize(marker) in window for marker in PAST_MARKERS) and not any(
            normalize(marker) in window for marker in CURRENT_MARKERS
        )
    return False


def validate_claim_graph(record: dict, packets: list[dict], contract: dict, graph: dict) -> dict:
    failures = []
    warnings = []
    packet_map = {str(packet.get("evidence_id", "")): str(packet.get("text", "")) for packet in packets}
    lookup_bindings = record_lookup_field_bindings(record)
    question_graph = record.get("question_evidence_graph")
    graph_intent = (
        question_graph.get("intent")
        if isinstance(question_graph, dict) else None
    )
    graph_operation = (
        graph_intent.get("operation")
        if isinstance(graph_intent, dict) else None
    )
    if lookup_bindings or graph_operation == "aggregate_count":
        expected_answer = expected_record_lookup_answer(record)
        observed_answer = record.get("answer", {}).get("answer", "")
        if expected_answer is None or observed_answer != expected_answer:
            failures.append({
                "code": (
                    "record_lookup_answer_projection_mismatch"
                    if lookup_bindings
                    else "aggregate_answer_projection_mismatch"
                ),
                "detail": "回答が検証済み分岐の機械投影と一致しません。",
            })
    expected_contract = build_question_contract(contract.get("query", ""), {"items": [
        {
            "item_id": item.get("field_id", ""), "label": item.get("label", ""),
            "required_claim": item.get("required_claim", ""), "required": item.get("required", False),
        }
        for item in contract.get("items", [])
    ]}, record_lookup_bindings=lookup_bindings)
    if contract.get("contract_hash") != expected_contract.get("contract_hash"):
        failures.append({"code": "contract_hash_mismatch", "detail": "質問契約が作成後に変更されています。"})
    graph_body = {key: graph.get(key) for key in ("contract_hash", "nodes", "edges", "claims")}
    if graph.get("artifact_hash") != stable_hash(graph_body):
        failures.append({"code": "artifact_hash_mismatch", "detail": "主張グラフが作成後に変更されています。"})
    if graph.get("contract_hash") != contract.get("contract_hash"):
        failures.append({"code": "artifact_contract_mismatch", "detail": "主張グラフが別の質問契約を参照しています。"})

    nodes = graph.get("nodes", [])
    edges = graph.get("edges", [])
    node_ids = [node.get("node_id") for node in nodes if isinstance(node, dict)]
    if len(node_ids) != len(set(node_ids)) or any(not node_id for node_id in node_ids):
        failures.append({"code": "graph_node_ids_invalid", "detail": "Node IDが空または重複しています。"})
    known_nodes = set(node_ids)
    edge_ids = [edge.get("edge_id") for edge in edges if isinstance(edge, dict)]
    if len(edge_ids) != len(set(edge_ids)) or any(not edge_id for edge_id in edge_ids):
        failures.append({"code": "graph_edge_ids_invalid", "detail": "Edge IDが空または重複しています。"})
    for edge in edges:
        if not isinstance(edge, dict):
            failures.append({"code": "graph_edge_invalid", "detail": "Edgeがobjectではありません。"})
            continue
        if edge.get("source") not in known_nodes or edge.get("target") not in known_nodes:
            failures.append({
                "code": "graph_edge_endpoint_missing",
                "detail": f"Edge端点がありません: {edge.get('edge_id', '')}",
            })
    claim_node_ids = {
        node.get("node_id") for node in nodes
        if isinstance(node, dict) and node.get("node_type") == "claim"
    }
    missing_claim_nodes = [
        claim.get("claim_id") for claim in graph.get("claims", [])
        if claim.get("claim_id") not in claim_node_ids
    ]
    if missing_claim_nodes:
        failures.append({
            "code": "claim_node_missing",
            "detail": f"主張を表すNodeがありません: {missing_claim_nodes}",
        })

    contract_items = {
        item.get("field_id"): item for item in contract.get("items", [])
    }
    field_ids = set(contract_items)
    raw_answer_text = record.get("answer", {}).get("answer", "")
    answer_text = normalize(raw_answer_text)
    for claim in graph.get("claims", []):
        claim_id = claim.get("claim_id", "")
        is_record_lookup_claim = claim.get("field_id") in lookup_bindings
        if claim.get("field_id") not in field_ids:
            failures.append({"code": "unknown_field_id", "claim_id": claim_id, "detail": "質問契約にない項目です。"})
        evidence_ids = claim.get("evidence_ids", [])
        missing_ids = [evidence_id for evidence_id in evidence_ids if evidence_id not in packet_map]
        if not evidence_ids or missing_ids:
            failures.append({"code": "invalid_evidence_reference", "claim_id": claim_id, "detail": f"不明なEvidence ID: {missing_ids}"})
            continue
        provisional_support_ids = [
            evidence_id for evidence_id in evidence_ids
            if packet_has_provisional_reading(packet_map[evidence_id])
        ]
        if provisional_support_ids:
            failures.append({
                "code": "provisional_evidence_only",
                "claim_id": claim_id,
                "detail": (
                    "Provisional OCR packets cannot be used as confirmed support; "
                    f"keep them diagnostic: {provisional_support_ids}"
                ),
            })
        cited_text = "\n".join(packet_map[evidence_id] for evidence_id in evidence_ids)
        non_provisional_cited_text = "\n".join(
            non_provisional_text(packet_map[evidence_id]) for evidence_id in evidence_ids
        )
        for value_part in claim.get("value_parts", []):
            if (
                not is_record_lookup_claim
                and normalize(value_part) not in normalize(cited_text)
            ):
                failures.append({"code": "value_not_in_evidence", "claim_id": claim_id, "detail": f"原文にない値: {value_part}"})
            if _time_relation_conflicts(
                str(claim.get("time_scope", "unspecified")),
                value_part,
                non_provisional_cited_text,
            ):
                failures.append({"code": "time_scope_conflict", "claim_id": claim_id, "detail": f"質問の時制と一致しない値: {value_part}"})
        strict_scalar_projection = (
            is_record_lookup_claim
            or claim.get("entity_type") in {"numeric_count", "numeric_value"}
        )
        if strict_scalar_projection and not verified_scalar_is_in_answer(
            raw_answer_text, str(claim.get("value", "")),
        ):
            failures.append({"code": "value_not_in_answer", "claim_id": claim_id, "detail": "検証済み値が最終回答へ投影されていません。"})
        elif (
            not strict_scalar_projection
            and normalize(claim.get("value", "")) not in answer_text
        ):
            failures.append({"code": "value_not_in_answer", "claim_id": claim_id, "detail": "検証済み値が最終回答へ投影されていません。"})
        elif strict_scalar_projection and record_lookup_value_is_negated(
            raw_answer_text,
            str(claim.get("value", "")),
            allow_current_contrast=bool(
                isinstance(record.get("question_plan"), dict)
                and isinstance(
                    record.get("question_plan", {}).get("temporal_scope"), dict,
                )
            ),
        ):
            failures.append({
                "code": (
                    "record_lookup_value_negated_in_answer"
                    if is_record_lookup_claim
                    else "numeric_value_negated_in_answer"
                ),
                "claim_id": claim_id,
                "detail": "検証済み値が最終回答内で否定されています。",
            })
        if strict_scalar_projection and numeric_answer_has_conflicting_alternative(
            raw_answer_text, str(claim.get("value", "")),
        ):
            failures.append({
                "code": "verified_value_conflict_in_answer",
                "claim_id": claim_id,
                "detail": "検証済み数値と競合する代替値が最終回答にあります。",
            })
        if claim.get("entity_type") == "person_name" and not _name_relation_is_explicit(
            str(claim.get("value", "")), non_provisional_cited_text
        ):
            failures.append({"code": "person_name_relation_missing", "claim_id": claim_id, "detail": "人物名と値を結ぶ明示的な記述がありません。"})

    supported_fields = {claim.get("field_id") for claim in graph.get("claims", [])}
    answer = record.get("answer", {})
    if answer.get("answer_mode") == "grounded":
        missing_required = [
            item.get("field_id") for item in contract.get("items", [])
            if item.get("required") and item.get("field_id") not in supported_fields
        ]
        if missing_required:
            failures.append({"code": "required_field_missing", "detail": f"必須項目が未充足です: {missing_required}"})
    for item in contract.get("items", []):
        if item.get("require_all") and item.get("field_id") in supported_fields:
            warnings.append({"code": "coverage_requires_semantic_audit", "field_id": item.get("field_id"), "detail": "全件性は独立監査役がEvidenceの列挙範囲を再確認します。"})

    validate_record_lookup_binding(
        graph, lookup_bindings, failures,
    )
    validate_question_graph_binding(record, contract, graph, packet_map, failures, warnings)

    status = "blocked" if failures else "pass"
    return {
        "validator_version": "1.4",
        "status": status,
        "failures": failures,
        "warnings": warnings,
        "checked_claim_ids": [claim.get("claim_id") for claim in graph.get("claims", [])],
    }


def build_and_validate(record: dict, packets: list[dict]) -> tuple[dict, dict, dict]:
    contract = build_question_contract(
        record.get("query", ""),
        record.get("question_plan", {}),
        record_lookup_bindings=record_lookup_field_bindings(record),
    )
    graph = build_claim_graph(record, packets, contract)
    report = validate_claim_graph(record, packets, contract, graph)
    return contract, graph, report
