#!/usr/bin/env python3
"""Shared, data-independent language registry for question compilation.

This module contains only finite lexical and operation-language definitions.
It must not contain complete questions, source-specific names, answers, or
retrieval data.  The JSON-safe payload and SHA-256 API make every registry
revision explicit before the builder and validator share more parser logic.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from types import MappingProxyType
from typing import Any


REGISTRY_NAME = "question-language-registry"
REGISTRY_VERSION = "0.1"


# A target type is usable only when an exact target wording contains one of
# these finite lexemes.  Longer-match and ambiguity handling remain compiler
# responsibilities.
CANONICAL_TARGET_TYPE_LEXEMES: Mapping[str, tuple[str, ...]] = MappingProxyType({
    "record": ("recordid", "record", "レコードid", "レコード", "データ"),
    "row": ("rowid", "row", "行id", "行番号", "行"),
    "task": ("taskid", "task", "タスクid", "タスク"),
    "document": (
        "documentid",
        "document",
        "docid",
        "ドキュメントid",
        "ドキュメント",
        "文書id",
        "文書",
    ),
    "file": ("fileid", "file", "ファイルid", "ファイル"),
    "table": ("tableid", "table", "テーブルid", "テーブル", "表"),
    "chart": ("chartid", "chart", "graph", "チャート", "グラフ", "図表"),
    "person": (
        "employeeid",
        "employee",
        "personid",
        "person",
        "worker",
        "staff",
        "従業員id",
        "従業員",
        "社員id",
        "社員",
        "人物",
    ),
    "organization": (
        "organizationid",
        "organization",
        "orgid",
        "組織id",
        "組織",
        "会社",
        "法人",
        "団体",
    ),
    "project": ("projectid", "project", "プロジェクトid", "プロジェクト"),
    "event": ("eventid", "event", "イベントid", "イベント"),
    "metric": ("metricid", "metric", "measure", "メトリック", "指標"),
    "status": ("status", "ステータス", "状態"),
    "procedure": ("procedure", "プロセス", "手順"),
    "claim": ("claim", "主張"),
    "value": ("value", "値"),
    "identifier": ("identifier", "識別子", "id"),
    "field": ("field", "column", "フィールド", "カラム", "列", "項目"),
    "dataset": ("dataset", "データセット"),
    "source": ("source", "ソース", "出典"),
})

CALCULATION_OPERATORS = frozenset({
    "calculate",
    "sum",
    "mean",
    "min",
    "max",
    "absolute_distance",
    "argmin_all",
    "argmax_all",
})

DIRECT_OPERATIONS = frozenset({
    "count",
    "list",
    "retrieve",
    "compare",
    "explain",
    "procedure",
    "verify",
})

OPERATOR_MENTION_MAP: Mapping[str, str] = MappingProxyType({
    "=": "eq",
    "==": "eq",
    "等しい": "eq",
    "同じ": "eq",
    "一致": "eq",
    "!=": "ne",
    "等しくない": "ne",
    "一致していない": "ne",
    "一致しません": "ne",
    "一致しない": "ne",
    "一致せず": "ne",
    "以外": "ne",
    ">": "gt",
    "より大きい": "gt",
    "超える": "gt",
    ">=": "gte",
    "以上": "gte",
    "<": "lt",
    "より小さい": "lt",
    "未満": "lt",
    "<=": "lte",
    "以下": "lte",
    "含む": "contains",
    "始まる": "starts_with",
    "終わる": "ends_with",
    "の間": "between",
    "範囲内": "between",
    "いずれか": "in",
    "いずれでもない": "not_in",
    "正規表現": "matches",
    "null": "is_null",
    "NULL": "is_null",
    "空値": "is_null",
    "nullでない": "is_not_null",
    "NULLでない": "is_not_null",
})

ALL_CARDINALITY_SURFACES = frozenset({"すべて", "全て", "全部", "all", "ALL"})
MULTIPLE_CARDINALITY_SURFACES = frozenset({"複数", "いくつか", "multiple"})
SINGLE_CARDINALITY_SURFACES = frozenset({"1つ", "一つ", "ひとつ", "1件", "single"})

OPERATION_KEYWORDS: Mapping[str, tuple[str, ...]] = MappingProxyType({
    "count": ("件数", "何件", "数え", "カウント", "count"),
    "list": ("一覧", "列挙", "挙げ", "list"),
    "retrieve": ("取得", "教え", "答え", "retrieve"),
    "compare": ("比較", "比べ", "compare"),
    "calculate": ("計算", "算出", "calculate"),
    "sum": ("合計", "総和", "sum"),
    "mean": ("平均", "mean", "average"),
    "min": ("最小値", "最低値", "minimum"),
    "max": ("最大値", "最高値", "maximum"),
    "absolute_distance": ("絶対差", "距離", "差分", "difference", "distance"),
    "argmin_all": ("最も近", "最小", "nearest", "argmin"),
    "argmax_all": ("最も遠", "最大", "farthest", "argmax"),
    "sort": (
        "昇順",
        "降順",
        "小さい順",
        "大きい順",
        "高い順",
        "低い順",
        "順番",
        "順に",
        "並べ",
        "sort",
    ),
    "deduplicate": ("重複", "一意", "deduplicate"),
    "group": ("ごと", "別々", "グループ", "group", "by"),
    "explain": ("説明", "なぜ", "理由", "explain"),
    "procedure": ("手順", "方法", "procedure"),
    "verify": ("確認", "検証", "verify"),
    "boolean_test": ("かどうか", "真偽", "boolean"),
})

# These are the current structural option signatures.  Value-flow type rules
# remain in the compiler until the typed Clause IR owns them.
OPERATION_OPTION_KEYS = frozenset({
    "predicate",
    "fields",
    "calculation_precision",
    "candidate_set_ref",
    "distance",
    "field",
    "tie_policy",
    "sort_order",
})

ALLOWED_OPERATION_OPTIONS: Mapping[str, frozenset[str]] = MappingProxyType({
    "filter": frozenset({"predicate"}),
    "project": frozenset({"fields"}),
    "calculate": frozenset({"calculation_precision"}),
    "sum": frozenset({"calculation_precision"}),
    "mean": frozenset({"calculation_precision"}),
    "min": frozenset({"calculation_precision"}),
    "max": frozenset({"calculation_precision"}),
    "absolute_distance": frozenset({"calculation_precision"}),
    "argmin_all": frozenset({"candidate_set_ref", "distance", "field", "tie_policy"}),
    "argmax_all": frozenset({"candidate_set_ref", "distance", "field", "tie_policy"}),
    "sort": frozenset({"sort_order"}),
})

CALCULATION_PRECISION_KEYWORDS: Mapping[str, tuple[str, ...]] = MappingProxyType({
    "exact_unrounded": ("丸めない", "丸めず", "非丸め", "unrounded"),
    "exact": ("正確", "厳密", "exact"),
    "rounded": ("丸め", "四捨五入", "rounded"),
})

DISTANCE_KEYWORDS: Mapping[str, tuple[str, ...]] = MappingProxyType({
    "absolute": ("絶対", "距離", "最も近", "最も遠", "absolute"),
    "squared": ("二乗", "平方", "squared"),
    "custom": ("独自", "カスタム", "custom"),
})

SORT_ORDER_KEYWORDS: Mapping[str, tuple[str, ...]] = MappingProxyType({
    "ascending": ("昇順", "小さい順", "低い順", "ascending", "asc"),
    "descending": ("降順", "大きい順", "高い順", "descending", "desc"),
})

APPROXIMATE_PRECISION_KEYWORDS = (
    "約",
    "およそ",
    "概算",
    "近似",
    "approximate",
    "approximately",
)

EXACT_PRECISION_KEYWORDS = ("正確", "厳密", "exact")

JAPANESE_DIGITS: Mapping[int, tuple[str, ...]] = MappingProxyType({
    0: ("零", "〇"),
    1: ("一",),
    2: ("二",),
    3: ("三",),
    4: ("四",),
    5: ("五",),
    6: ("六",),
    7: ("七",),
    8: ("八",),
    9: ("九",),
    10: ("十",),
})

ALTERNATIVE_CONNECTORS = (
    "または",
    "もしくは",
    "又は",
    "あるいは",
    "若しくは",
    "および",
    "及び",
)

RAW_REQUIRED_OPERATION_KEYWORDS: Mapping[str, tuple[str, ...]] = MappingProxyType({
    "count": ("何件", "件数", "count"),
    "sum": ("合計値", "合計", "総和", "sum"),
    "mean": ("平均値", "平均", "mean", "average"),
    "min": ("最小値", "最低値", "minimum"),
    "max": ("最大値", "最高値", "maximum"),
    "absolute_distance": ("絶対差", "距離", "absolute distance"),
    "argmin_all": ("最も近", "nearest", "argmin"),
    "argmax_all": ("最も遠", "farthest", "argmax"),
    "sort": ("昇順", "降順", "小さい順", "大きい順", "sort"),
    "deduplicate": ("重複を除", "一意", "deduplicate"),
    "group": ("グループ", "group by"),
    "procedure": ("手順", "procedure"),
    "verify": ("検証", "verify"),
})

SUPPORTED_FILTER_SUFFIXES = (
    "フェーズ",
    "ステータス",
    "状態",
    "カテゴリ",
    "区分",
    "種別",
    "段階",
)

# Closed, source-independent bilingual metric concepts.  These aliases bind
# two explicit question spans; they never rename a Catalog field or infer a
# value from source data.
_METRIC_DESCRIPTOR_CONCEPTS = (
    ("Age", "年齢"),
    ("Amount", "金額"),
    ("Count", "件数"),
    ("Distance", "距離"),
    ("Duration", "期間"),
    ("Height", "身長"),
    ("Income", "収入"),
    ("Price", "価格"),
    ("Salary", "給与"),
    ("Score", "スコア"),
    ("Weight", "体重"),
)
SUPPORTED_METRIC_DESCRIPTOR_ALIASES = frozenset(
    (left, right)
    for pair in _METRIC_DESCRIPTOR_CONCEPTS
    for left, right in (pair, (pair[1], pair[0]))
)

SUPPORTED_LANE_NEGATIVE_MARKERS = (
    "不要",
    "除外",
    "除いて",
    "除く",
    "含めない",
    "含めず",
    "求めない",
    "求めていない",
    "答えない",
    "答えなくて",
    "省いて",
    "ではなく",
    "do not",
    "don't",
    "exclude",
    "without",
)

RAW_EXCLUSION_REVERSALS = (
    "除外しない",
    "除外しません",
    "除外しなく",
    "除かない",
    "除かず",
    "省かない",
    "省かず",
    "不要ではない",
    "不要でない",
    "不要とは限らない",
    "含めないわけではない",
    "求めないわけではない",
    "答えないわけではない",
    "ないわけではない",
    "なくはない",
    "ないことはない",
)


_REGISTRY_DEFINITIONS: Mapping[str, Any] = MappingProxyType({
    "canonical_target_type_lexemes": CANONICAL_TARGET_TYPE_LEXEMES,
    "calculation_operators": CALCULATION_OPERATORS,
    "direct_operations": DIRECT_OPERATIONS,
    "operator_mention_map": OPERATOR_MENTION_MAP,
    "cardinality_surfaces": {
        "all": ALL_CARDINALITY_SURFACES,
        "multiple": MULTIPLE_CARDINALITY_SURFACES,
        "single": SINGLE_CARDINALITY_SURFACES,
    },
    "operation_keywords": OPERATION_KEYWORDS,
    "operation_option_keys": OPERATION_OPTION_KEYS,
    "allowed_operation_options": ALLOWED_OPERATION_OPTIONS,
    "calculation_precision_keywords": CALCULATION_PRECISION_KEYWORDS,
    "distance_keywords": DISTANCE_KEYWORDS,
    "sort_order_keywords": SORT_ORDER_KEYWORDS,
    "approximate_precision_keywords": APPROXIMATE_PRECISION_KEYWORDS,
    "exact_precision_keywords": EXACT_PRECISION_KEYWORDS,
    "japanese_digits": JAPANESE_DIGITS,
    "alternative_connectors": ALTERNATIVE_CONNECTORS,
    "raw_required_operation_keywords": RAW_REQUIRED_OPERATION_KEYWORDS,
    "supported_filter_suffixes": SUPPORTED_FILTER_SUFFIXES,
    "supported_metric_descriptor_aliases": SUPPORTED_METRIC_DESCRIPTOR_ALIASES,
    "supported_lane_negative_markers": SUPPORTED_LANE_NEGATIVE_MARKERS,
    "raw_exclusion_reversals": RAW_EXCLUSION_REVERSALS,
})


def _json_safe(value: Any) -> Any:
    """Return a deterministic JSON-safe copy of finite registry values."""

    if isinstance(value, Mapping):
        return {
            str(key): _json_safe(value[key])
            for key in sorted(value, key=lambda item: str(item))
        }
    if isinstance(value, (set, frozenset)):
        converted = [_json_safe(item) for item in value]
        return sorted(
            converted,
            key=lambda item: json.dumps(
                item, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ),
        )
    if isinstance(value, (tuple, list)):
        return [_json_safe(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"registry value is not JSON-safe: {type(value).__name__}")


def registry_payload() -> dict[str, Any]:
    """Return the canonical, mutation-safe registry payload."""

    return {
        "name": REGISTRY_NAME,
        "version": REGISTRY_VERSION,
        "definitions": _json_safe(_REGISTRY_DEFINITIONS),
    }


def registry_digest() -> str:
    """Return the lowercase SHA-256 of the canonical registry payload."""

    encoded = json.dumps(
        registry_payload(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


LANGUAGE_REGISTRY_SHA256 = registry_digest()


def registry_metadata() -> dict[str, str]:
    """Return compact provenance metadata for future Clause IR records."""

    return {
        "name": REGISTRY_NAME,
        "version": REGISTRY_VERSION,
        "sha256": LANGUAGE_REGISTRY_SHA256,
    }


__all__ = [
    "ALLOWED_OPERATION_OPTIONS",
    "ALL_CARDINALITY_SURFACES",
    "ALTERNATIVE_CONNECTORS",
    "APPROXIMATE_PRECISION_KEYWORDS",
    "CALCULATION_OPERATORS",
    "CALCULATION_PRECISION_KEYWORDS",
    "CANONICAL_TARGET_TYPE_LEXEMES",
    "DIRECT_OPERATIONS",
    "DISTANCE_KEYWORDS",
    "EXACT_PRECISION_KEYWORDS",
    "JAPANESE_DIGITS",
    "LANGUAGE_REGISTRY_SHA256",
    "MULTIPLE_CARDINALITY_SURFACES",
    "OPERATION_KEYWORDS",
    "OPERATION_OPTION_KEYS",
    "OPERATOR_MENTION_MAP",
    "RAW_EXCLUSION_REVERSALS",
    "RAW_REQUIRED_OPERATION_KEYWORDS",
    "REGISTRY_NAME",
    "REGISTRY_VERSION",
    "SINGLE_CARDINALITY_SURFACES",
    "SORT_ORDER_KEYWORDS",
    "SUPPORTED_FILTER_SUFFIXES",
    "SUPPORTED_LANE_NEGATIVE_MARKERS",
    "SUPPORTED_METRIC_DESCRIPTOR_ALIASES",
    "registry_digest",
    "registry_metadata",
    "registry_payload",
]
