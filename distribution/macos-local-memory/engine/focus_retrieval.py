"""Bounded literal-term rescue, applied only to already permitted evidence.

This is candidate selection, not semantic validation or a new answerer. The
caller must apply its existing source/version/security gates before calling and
must audit supplemented evidence exactly like ordinary retrieval evidence.
"""
from __future__ import annotations

import re
import unicodedata


VERSION = "focus-retrieval-v2"
MAX_QUERY_CHARACTERS = 4096
MAX_TERMS = 12
MAX_TERM_CHARACTERS = 40
MAX_SUPPLEMENTAL = 3
MAX_SCAN_CANDIDATES = 50_000
# Match the existing context packer's whole-evidence limit. Never truncate text.
MAX_EVIDENCE_CHARACTERS = 1800

_SEPARATORS = re.compile(
    r"に関する|について|における|に対する|から|まで|[のをがはでともに]"
)
_WORDS = re.compile(r"[一-鿿々〆ヵヶぁ-ゖ]+|[ァ-ヺー]+|[a-z][a-z0-9_-]*")
_KANJI = re.compile(r"[一-鿿々〆ヵヶ]")
_HIRAGANA = re.compile(r"[ぁ-ゖ]")
_ACTION_SUFFIXES = tuple(sorted((
    "受け渡し", "引き渡し", "取り扱い", "持ち込み", "申し込み",
    "貸出", "返却", "配り", "配布", "配達", "配送", "販売", "購入",
    "回収", "交換", "保管", "管理", "処分", "持込", "申込",
), key=len, reverse=True))
_STOP_TERMS = frozenset((
    "注意", "注意点", "注意事項", "留意", "留意点", "条件", "適用", "場合",
    "資料", "所在", "場所", "記載", "記載場所", "出典", "根拠", "情報", "内容",
    "曜日", "時間", "時刻", "開始", "終了", "日付", "案内", "案内文", "確認",
    "質問", "回答", "方法", "手順", "事項", "対象", "範囲", "必要", "禁止",
    "制限", "規則", "ルール", "知りたい", "教えて", "説明", "最初", "一番",
    "全体", "具体的", "関連", "最新", "現在", "所", "何", "時", "事",
    "what", "where", "when", "how", "which", "please", "tell", "me", "about",
    "the", "a", "an", "and", "or", "is", "are", "of", "for", "to", "in", "on",
    "at", "with", "do", "does", "information", "document", "documents", "location",
    "conditions", "precautions", "rules", "schedule", "time", "hours", "source", "sources",
    *_ACTION_SUFFIXES,
))
_JAPANESE_STOP_COMPONENTS = tuple(sorted(
    (word for word in _STOP_TERMS if re.fullmatch(r"[一-鿿々〆ヵヶぁ-ゖァ-ヺー]+", word)),
    key=lambda word: (-len(word), word),
))


def _is_request_term(word: str) -> bool:
    """Also reject compounds made entirely from existing request words.

    開始時刻 is a requested attribute, not a new subject merely because the
    source expresses its value without that exact heading. Do not strip such
    components from genuine subject words or add domain-specific vocabulary.
    """
    if word in _STOP_TERMS:
        return True
    if len(word) > MAX_TERM_CHARACTERS:
        return False
    reachable = {0}
    for start in range(len(word)):
        if start in reachable:
            for component in _JAPANESE_STOP_COMPONENTS:
                if word.startswith(component, start):
                    reachable.add(start + len(component))
    return len(word) in reachable


def _normalize(value: str) -> str:
    return "".join(character for character in unicodedata.normalize("NFKC", value).casefold()
                   if character.isalnum())


def _dedup_key(value: str) -> str:
    """Normalize presentation aliases without discarding numeric meaning.

    The broader term-matching normalizer is deliberately not suitable here:
    removing every symbol would collapse -5 into 5, or 1.5 into 15. Retain
    symbols except a leading non-numeric heading colon and final Japanese full
    stop, so existing cell/row presentation aliases do not consume all slots.
    """
    text = re.sub(r"\s+", "", unicodedata.normalize("NFKC", value).casefold())
    text = re.sub(r"^([^:。！？、\d]{1,40}):(?=[^\d])", r"\1", text, count=1)
    return text.rstrip("。")


def _focus_terms(query: str) -> list[str]:
    """Extract explicit short words, not arbitrary kanji character n-grams.

    This intentionally is not a full Japanese morphological analyzer. Mixed
    kanji/hiragana fragments are rejected unless a recognized action suffix
    exposes the object, e.g. a one-character object followed by 貸出.
    """
    normalized_query = unicodedata.normalize("NFKC", query).casefold()
    terms: list[str] = []
    for fragment in _SEPARATORS.split(normalized_query):
        for match in _WORDS.finditer(fragment):
            word = match.group()
            for suffix in _ACTION_SUFFIXES:
                if word.endswith(suffix) and len(word) > len(suffix):
                    word = word[:-len(suffix)]
                    break
            if not word or _is_request_term(word) or _HIRAGANA.search(word):
                continue
            term = _normalize(word)
            if not term or len(term) > MAX_TERM_CHARACTERS:
                continue
            # A one-character kanji is allowed only as the explicit extracted
            # word/action stem. Never split a multi-kanji word into characters.
            if len(term) == 1 and not _KANJI.fullmatch(term):
                continue
            if term not in terms:
                terms.append(term)
                if len(terms) >= MAX_TERMS:
                    return terms
    return terms


def supplement(
    query: str,
    ranked_candidates: list[dict],
    normal_results: list[dict],
    max_supplemental: int = 3,
) -> list[dict]:
    """Keep ordinary results and interleave at most three missing-term hits.

    All eligible sheets compete equally. Literal matching does not resolve
    scope, contradictory instructions, applicability, dates, or source trust.
    Such decisions remain with the existing graph/answer audits and the user.
    No data is read outside the caller-supplied candidates.
    """
    if not isinstance(query, str) or len(query) > MAX_QUERY_CHARACTERS:
        return normal_results
    limit = min(MAX_SUPPLEMENTAL, max(0, max_supplemental))
    if not limit or not ranked_candidates:
        return normal_results
    terms = _focus_terms(query)
    if not terms:
        return normal_results

    normal_keys = {_dedup_key(row["text"]) for row in normal_results
                   if isinstance(row.get("text"), str)}
    # An un-packable ordinary hit cannot establish that Gemma will receive the
    # term. Keep that hit untouched, but permit a shorter whole-source rescue.
    packable_normal_texts = {_normalize(row["text"]) for row in normal_results
                            if isinstance(row.get("text"), str) and row["text"].strip()
                            and len(row["text"]) <= MAX_EVIDENCE_CHARACTERS}
    missing = [term for term in terms
               if not any(term in text for text in packable_normal_texts)]
    if not missing:
        return normal_results

    # Deduplication keeps cell/row aliases from consuming all rescue slots or
    # inflating frequency. Preserve the first candidate's original rank/locator.
    candidates: list[tuple[dict, str]] = []
    seen_texts: set[str] = set()
    seen_ids: set[str] = set()
    for row in ranked_candidates[:MAX_SCAN_CANDIDATES]:
        source_text = row.get("text")
        evidence_id = row.get("evidence_id")
        if (not isinstance(source_text, str) or not source_text.strip()
                or len(source_text) > MAX_EVIDENCE_CHARACTERS
                or not isinstance(evidence_id, str) or not evidence_id):
            continue
        normalized_text = _normalize(source_text)
        duplicate_key = _dedup_key(source_text)
        if not normalized_text or duplicate_key in seen_texts or evidence_id in seen_ids:
            continue
        seen_texts.add(duplicate_key)
        seen_ids.add(evidence_id)
        candidates.append((row, normalized_text))

    frequencies = {term: sum(term in text for _, text in candidates) for term in missing}
    missing = [term for term in missing if frequencies[term] and not (
        len(candidates) >= 8 and frequencies[term] / len(candidates) > 0.6
    )]
    if not missing:
        return normal_results

    normal_ids = {row.get("evidence_id") for row in normal_results}
    additions: list[tuple[float, int, dict]] = []
    for rank, (row, text) in enumerate(candidates):
        if _dedup_key(row["text"]) in normal_keys or row["evidence_id"] in normal_ids:
            continue
        matched = [term for term in missing if term in text]
        if matched:
            # Prefer coverage of rare object terms, then retain the existing
            # ranking. Never prefer a sheet name, newer date, or presumed answer.
            score = sum(1 / frequencies[term] for term in matched)
            addition = dict(row)
            addition["retrieval_source"] = "focus_term_supplement"
            addition["focus_terms"] = matched
            additions.append((-score, rank, addition))
    if not additions:
        return normal_results
    additions.sort(key=lambda entry: (entry[0], entry[1]))
    rescued = [entry[2] for entry in additions[:limit]]
    return normal_results[:1] + rescued + normal_results[1:]
