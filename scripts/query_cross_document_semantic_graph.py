#!/usr/bin/env python3
"""Answer bounded cross-document questions by traversing a frozen SQLite graph.

The answerer receives only a question and a question-independent graph snapshot.
It never reads fixture specifications, evaluation gold, or source files.  Every
asserted fact carries the semantic Edge IDs used to derive it, so a later
evaluator can recompute the proof instead of trusting prose.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import resource
import sqlite3
import sys
import time
import unicodedata
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote


SCHEMA_VERSION = "0.1"
ANSWERER = "cross-document-semantic-graph-query"
ANSWERER_VERSION = "0.1.0"
GRAPH_SNAPSHOT_PREFIX = "xkgs_"
RUN_PREFIX = "xkgr_"
EXPECTED_BUILDER = "cross-document-semantic-graph-builder"
NODE_TYPES = {
    "Project",
    "ProjectAlias",
    "Work",
    "WorkName",
    "Employee",
    "Person",
    "Claim",
    "Reason",
}
RELATION_ENDPOINT_TYPES = {
    "HAS_ALIAS": ("Project", "ProjectAlias"),
    "CONTAINS_WORK": ("Project", "Work"),
    "HAS_NAME": ("Work", "WorkName"),
    "ASSIGNED_TO": ("Work", "Employee"),
    "IDENTIFIES_PERSON": ("Employee", "Person"),
    "HAS_CLAIM": ("Work", "Claim"),
    "HAS_CURRENT_CLAIM": ("Work", "Claim"),
    "CLAIMS_ASSIGNEE": ("Claim", "Employee"),
    "SUPERSEDES": ("Claim", "Claim"),
    "CONTRADICTS": ("Claim", "Claim"),
    "HAS_CHANGE_REASON": ("Claim", "Reason"),
}
DATE_JA = re.compile(
    r"(?<!\d)(?P<year>[12]\d{3})\s*年\s*"
    r"(?P<month>1[0-2]|0?[1-9])\s*月\s*"
    r"(?P<day>3[01]|[12]\d|0?[1-9])\s*日"
)
DATE_ISO = re.compile(
    r"(?<!\d)(?P<year>[12]\d{3})[-/.]"
    r"(?P<month>1[0-2]|0?[1-9])[-/.]"
    r"(?P<day>3[01]|[12]\d|0?[1-9])(?!\d)"
)
ABSOLUTE_DATE_TEXT = (
    r"(?:[12]\d{3}\s*年\s*(?:1[0-2]|0?[1-9])\s*月\s*"
    r"(?:3[01]|[12]\d|0?[1-9])\s*日|"
    r"[12]\d{3}[-/.](?:1[0-2]|0?[1-9])[-/.]"
    r"(?:3[01]|[12]\d|0?[1-9]))"
)
ABSOLUTE_DATE_APPROX_OR_RANGE = re.compile(
    rf"(?:約|およそ|だいたい|大体|おおむね|概ね|ほぼ|ざっと|"
    rf"多分|たぶん|おそらく|恐らく|少なくとも|少なくても|"
    rf"最低でも|最大でも|遅くとも|早くとも|多くとも)\s*"
    rf"{ABSOLUTE_DATE_TEXT}"
    rf"|{ABSOLUTE_DATE_TEXT}\s*"
    rf"(?:頃|ごろ|前後|くらい|ぐらい|ほど|程度|あたり|辺り|付近|位|"
    rf"から|以降|まで|以前|より昔|より(?:も|少し|も少し)?(?:後|前)|"
    rf"(?:の\s*)?(?:直前|直後)|を中心に|"
    rf"かそれ(?:以前|以降|より前|より後)|"
    rf"ではない|じゃない|以外|を除く|かもしれない|"
    rf"(?:〜|～|~|-|−|—|–|/|／)\s*"
    rf"(?:[0-9〇零一二三四五六七八九十]+)(?:\s*日)?)"
)
ABSOLUTE_DATE_CALENDAR_DETAIL = re.compile(
    rf"{ABSOLUTE_DATE_TEXT}\s*(?:の\s*)?"
    rf"(?:前日|翌日|[0-9〇零一二三四五六七八九十]+\s*日\s*(?:前|後)|"
    r"初め|初頭|前半|後半|終わり|年央|上旬|中旬|下旬|"
    r"ゴールデンウィーク|午前|午後|正午|朝|昼|夕方|夜|深夜)"
)
RELATIVE_YEARS_AGO = re.compile(
    r"(?<![0-9.+\-−〇零一二三四五六七八九十])"
    r"(?P<years>[0-9〇零一二三四五六七八九十]+)\s*年前"
)
RELATIVE_YEAR_COUNT = r"(?:[0-9]+|[〇零一二三四五六七八九十]+)"
RELATIVE_YEAR_APPROX_OR_RANGE = re.compile(
    rf"(?:約|およそ|だいたい|大体|おおむね|概ね|ほぼ|ざっと|"
    rf"多分|たぶん|おそらく|恐らく|少なくとも|少なくても|"
    rf"最低でも|最大でも|遅くとも|早くとも|多くとも)\s*"
    rf"{RELATIVE_YEAR_COUNT}\s*年前"
    rf"|{RELATIVE_YEAR_COUNT}\s*年前\s*"
    rf"(?:から|以降|まで|以前|より後|より前|より昔|ごろ|頃|"
    rf"くらい|ぐらい|ほど|程度|あたり|辺り|付近|位|"
    rf"以上|以下|以内|未満|超|"
    rf"より昔|より(?:も|少し|も少し)?(?:前|後)|"
    rf"(?:の\s*)?(?:直前|直後)|を中心に|"
    rf"かそれ(?:以前|以降|より前|より後)|"
    rf"ではない|じゃない|以外|を除く|かもしれない)"
    rf"|{RELATIVE_YEAR_COUNT}\s*年前後"
    rf"|{RELATIVE_YEAR_COUNT}\s*年\s*"
    rf"(?:以上|以下|未満|超|より|ほど|くらい|ぐらい|程度)\s*前"
    rf"|{RELATIVE_YEAR_COUNT}(?:\s*年)?\s*"
    rf"(?:〜|～|~|-|−|—|–|/|／|、|,|・|か|から|ないし|"
    rf"又は|または|あるいは|もしくは)\s*"
    rf"{RELATIVE_YEAR_COUNT}\s*年前"
)
RELATIVE_YEAR_LIKE = re.compile(
    rf"{RELATIVE_YEAR_COUNT}\s*年[^。?？!！\n]{{0,12}}?前"
)
RELATIVE_YEAR_CALENDAR_DETAIL = re.compile(
    rf"{RELATIVE_YEAR_COUNT}\s*年前\s*(?:の\s*)?"
    r"(?:時点(?:で|の)?\s*)?(?:は\s*)?(?:、|,)?\s*"
    rf"(?:{RELATIVE_YEAR_COUNT}\s*月"
    rf"(?:\s*{RELATIVE_YEAR_COUNT}\s*日)?|"
    rf"{RELATIVE_YEAR_COUNT}\s*日|"
    rf"{RELATIVE_YEAR_COUNT}\s*"
    rf"(?:[/.-]\s*{RELATIVE_YEAR_COUNT}){{1,2}}|"
    r"(?:[Qq][1-4]|[1-4][Qq])|"
    rf"第?\s*{RELATIVE_YEAR_COUNT}\s*週|"
    rf"年度(?:初|末|上期|下期)?|"
    rf"第?\s*{RELATIVE_YEAR_COUNT}\s*四半期|"
    r"上期|下期|上半期|下半期|月初|月末|年初|年末|年末年始|"
    r"初め|初頭|前半|後半|終わり|年央|上旬|中旬|下旬|"
    r"ゴールデンウィーク|元日|正月|同日|同月|同時期|今日|"
    r"(?:月|火|水|木|金|土|日)曜日|"
    r"午前|午後|正午|朝|昼|夕方|夜|深夜|春|夏|秋|冬)"
)
ALLOWED_RUN_DATE_ANCHOR = re.compile(
    rf"(?:今|現在|現時点)\s*"
    rf"(?:から\s*(?:(?:数えて|遡って|さかのぼって)\s*)?|"
    rf"(?:を)?(?:基準|起点)(?:に|として)\s*)"
    rf"(?:ちょうど|まさに)?\s*"
    rf"(?P<relative>{RELATIVE_YEAR_COUNT}\s*年前)"
)
OTHER_TEMPORAL_CONTEXT = re.compile(
    r"(?:一昨日|昨日|今日|明日|明後日|先日|当日|前日|翌日|"
    r"先々週|先週|今週|来週|再来週|先々月|先月|今月|来月|再来月|"
    r"一昨年度|昨年度|今年度|来年度|再来年度|"
    r"一昨年|昨年|去年|今年|来年|再来年|"
    r"現在|現時点|当時|その時|将来|今後|昔|過去)"
    rf"|(?:{RELATIVE_YEAR_COUNT}|数|半)\s*"
    r"(?:か月|ヶ月|ヵ月|ケ月|月|週間?|日間?|時間|分|秒)\s*"
    r"(?:前|まえ|後|あと)"
    r"|(?:数|半)\s*年(?:間)?"
    r"|(?<!\d)[12]\d{3}\s*年"
    rf"|(?:令和|平成|昭和)\s*(?:元|{RELATIVE_YEAR_COUNT})\s*年"
)
EVENT_RELATIVE_YEAR = re.compile(
    rf"(?:開始|終了|交代|交替|変更|異動|退任|着任|入社|退社|"
    rf"就任|離任|引き継ぎ|引継ぎ)(?:した|する)?(?:日|時点)?\s*"
    rf"(?:の|から|より)?\s*(?:(?:数えて|遡って|さかのぼって)\s*)?"
    rf"{RELATIVE_YEAR_COUNT}\s*年前"
)
ARBITRARY_RELATIVE_YEAR_ANCHOR = re.compile(
    rf"(?:(?:から|より)\s*(?:(?:数えて|遡って|さかのぼって)\s*)?|"
    rf"(?:を)?(?:基準|起点)(?:日|時点)?(?:に|として|から|より|の)\s*)"
    rf"{RELATIVE_YEAR_COUNT}\s*年前"
)
RELATIVE_ALLOWED_PREFIX = re.compile(
    r"(?:(?:今|現在|現時点)\s*"
    r"(?:から\s*(?:(?:数えて|遡って|さかのぼって)\s*)?|"
    r"(?:を)?(?:基準|起点)(?:に|として)\s*))?"
    r"(?:ちょうど|まさに)?\s*$"
)
ABSOLUTE_ALLOWED_PREFIX = re.compile(r"(?:ちょうど|まさに)?\s*$")
TEMPORAL_LEFT_BOUNDARY = re.compile(
    r"(?:^|[、,。?？!！:：;；（(\[「『]|(?:は|で|に|を))\s*$"
)
EXACT_TIME_ALLOWED_SUFFIX = re.compile(
    r"^\s*(?:"
    r"(?:の\s*)?時点\s*(?:で(?:は|の)?|は|に(?:おける)?)|"
    r"(?:現在|付)\s*(?:で(?:は|の)?|は|に)|"
    r"を\s*(?:基準|起点)\s*(?:に|として)|"
    r"で(?:は|の)?|は|に|の"
    r")?\s*(?:、|,)?\s*"
    r"(?=(?:誰|どなた|主担当|担当者?|受け持))"
)
OWNER_TIME_QUESTION_TAIL = re.compile(
    r"^\s*(?:"
    r"(?:誰|どなた)が\s*(?:この業務(?:を|の)?\s*)?"
    r"(?:主担当|担当者?|担当|受け持ち)"
    r"(?:でした|だった|です|なのか|していました|しています|していた|している)?"
    r"(?:か|でしょうか)?|"
    r"(?:主担当|担当者?|担当|受け持ち)(?:は|が)?\s*(?:誰|どなた)"
    r"(?:でした|だった|です|なのか)?(?:か|でしょうか)?"
    r")\s*[。.?？!！]*\s*$"
)
PROJECT_TO_WORK_BRIDGE = re.compile(
    r"^\s*(?:の|で|における|内の)\s*[「『\"（(\[]*\s*$"
)
WORK_TO_TIME_BRIDGE = re.compile(
    r"^\s*[」』\"）)\]]*\s*(?:は|で|について|の)?\s*(?:、|,)?\s*$"
)
ASSIGNMENT_CHANGE_QUESTION_TAIL = re.compile(
    r"^\s*[」』\"）)\]]*\s*(?:"
    r"で\s*(?:、|,)?\s*主担当が(?:切り替わった|交代した)日と\s*"
    r"(?:、|,)?\s*(?:(?:変更前\s*(?:・|と)\s*変更後)|"
    r"(?:前任\s*(?:・|と)\s*後任))(?:の担当者)?を"
    r"(?:答えて|教えて)ください|"
    r"の\s*主担当がいつ(?:変わった|変更された)か\s*(?:、|,)?\s*"
    r"変更前後の担当者を(?:答えて|教えて)ください"
    r")\s*[。.?？!！]*\s*$"
)
VERSION_CHANGE_QUESTION_TAIL = re.compile(
    r"^\s*[」』\"）)\]]*\s*(?:"
    r"について\s*(?:、|,)?\s*承認済みの担当変更理由と\s*"
    r"(?:、|,)?\s*旧案から何が変わったかを(?:答えて|教えて)ください|"
    r"で\s*(?:、|,)?\s*旧版から何が変わったのか\s*"
    r"(?:、|,)?\s*担当変更の背景も(?:答えて|教えて)ください"
    r")\s*[。.?？!！]*\s*$"
)
UNCONSUMED_TEMPORAL_SIGNAL = re.compile(
    rf"{ABSOLUTE_DATE_TEXT}|{RELATIVE_YEAR_LIKE.pattern}|"
    r"(?:約|およそ|だいたい|大体|おおむね|概ね|ほぼ|ざっと|"
    r"多分|たぶん|おそらく|恐らく|もしかすると|たしか|確か|"
    r"推定|推測|確証|記憶違い|仮|想定|可能性|予想|少なくとも|"
    r"少なくても|最低でも|最大でも|遅くとも|早くとも|多くとも)|"
    r"(?:かもしれない|かもしれません|かどうか|だったかも|"
    r"だった(?:と思う|はず)|らしい|"
    r"と思われる|ではない|ではなく|じゃない|以外|除く|"
    r"または|又は|もしくは|あるいは|ないし|その前|もっと前|"
    r"それ以前|それ以降|それより前|それより後|その後|後日|以後|"
    r"継続|引き続き|そのまま|別の日|別日)|"
    r"(?:頃|ごろ|時期|どこか|前後|くらい|ぐらい|ほど|程度|"
    r"あたり|辺り|付近|近辺|近く|周辺|近傍|中心|境に|直前|直後|"
    r"ちょっと|やや|若干|少し)|"
    r"(?:前年|翌年|次年|前月|翌月|次月|前週|翌週|次週|"
    r"前日|翌日|前々日|翌々日|次の日|前の日|前営業日|翌営業日|"
    r"同日|その日|期首|期末|誕生日)|"
    r"(?:初め|初頭|前半|後半|終わり|年央|上旬|中旬|下旬|"
    r"ゴールデンウィーク|元日|正月|年末年始|月初|月末|年初|年末|"
    r"上期|下期|上半期|下半期|四半期|"
    r"午前|午後|正午|朝|昼|夕方|夜|深夜|春|夏|秋|冬)|"
    r"(?:一昨日|昨日|今日|明日|明後日|先日|当日|"
    r"先々週|先週|今週|来週|再来週|先々月|先月|今月|来月|再来月|"
    r"一昨年度|昨年度|今年度|来年度|再来年度|"
    r"一昨年|昨年|去年|今年|来年|再来年|"
    r"現在|現時点|当時|その時|将来|今後|昔|過去)|"
    rf"(?:{RELATIVE_YEAR_COUNT}|数|半)\s*"
    r"(?:年|ねん|か月|ヶ月|ヵ月|ケ月|月|週間?|日間?|日|時間|分|秒)|"
    rf"(?:令和|平成|昭和)\s*(?:元|{RELATIVE_YEAR_COUNT})\s*年|"
    r"(?:開始|終了|交代|交替|変更|異動|退任|着任|入社|退社|"
    r"就任|離任|引き継ぎ|引継ぎ)(?:した|する)?(?:日|時点)?\s*"
    r"(?:から|より|の)|"
    r"(?:基準|起点)(?:日|時点)?|時点|(?:の)?ときから"
)
QUESTION_TIME_SCOPE_SIGNAL = re.compile(
    rf"(?:前年|翌年|次年|前月|翌月|次月|前週|翌週|次週|"
    rf"前日|翌日|前々日|翌々日|前営業日|翌営業日|同日|その日|"
    rf"春|夏|秋|冬|上期|下期|上半期|下半期|"
    rf"今期|前期|当期|次期|今四半期|期首|期末|年初|年末|月末|年度末|"
    rf"上旬|中旬|下旬|初日|最終日|午前|午後|正午|朝|昼|夕方|夜|深夜|"
    rf"未明|夕刻|ゴールデンウィーク|盆|連休|"
    rf"(?:月|火|水|木|金|土|日)曜日|[Qq][1-4]|"
    rf"[12]\d{{3}}\s*[Qq][1-4]|"
    rf"[12]\d{{3}}[-/.](?:1[0-2]|0?[1-9])(?![-/.]\d)|"
    rf"(?<!\d)(?:1[0-2]|0?[1-9])[/.-](?:3[01]|[12]\d|0?[1-9])(?!\d)|"
    rf"第?\s*{RELATIVE_YEAR_COUNT}\s*四半期|"
    rf"{RELATIVE_YEAR_COUNT}\s*(?:月|年度|四半期|週))\s*"
    r"(?:に|の|は|で|頃|ごろ|時点)?"
)


class GraphContractError(ValueError):
    """Raised when a graph snapshot is corrupt or outside this contract."""


class ResolutionError(ValueError):
    """Raised when a question cannot be resolved through a unique graph path."""

    def __init__(self, reason_code: str, message: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code
        self.message = message


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def sha256_value(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _is_sha256(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _object(value: str, label: str) -> dict[str, Any]:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise GraphContractError(f"{label}: invalid JSON") from exc
    if not isinstance(parsed, dict):
        raise GraphContractError(f"{label}: object required")
    return parsed


def _readonly_connection(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise GraphContractError(f"graph SQLite is missing: {path}")
    uri = f"file:{quote(str(path.resolve()))}?mode=ro&immutable=1"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


@dataclass(frozen=True)
class Node:
    node_id: str
    node_type: str
    canonical_key: str
    status: str
    properties: dict[str, Any]
    record_sha256: str


@dataclass(frozen=True)
class Edge:
    edge_id: str
    from_node_id: str
    relation_type: str
    to_node_id: str
    relation_class: str
    status: str
    basis_kind: str
    basis_rule: str
    properties: dict[str, Any]
    supporting_evidence_ids: tuple[str, ...]
    record_sha256: str


@dataclass(frozen=True)
class SourceEvidence:
    evidence_id: str
    document_id: str
    relative_path: str
    source_sha256: str
    locator: dict[str, Any]
    observed_text: str
    observed_sha256: str
    record_sha256: str


class GraphSnapshot:
    def __init__(
        self,
        graph_snapshot_id: str,
        nodes: dict[str, Node],
        edges: dict[str, Edge],
        evidence: dict[str, SourceEvidence],
    ) -> None:
        self.graph_snapshot_id = graph_snapshot_id
        self.nodes = nodes
        self.edges = edges
        self.evidence = evidence

    @classmethod
    def load(cls, path: Path) -> "GraphSnapshot":
        connection = _readonly_connection(path)
        try:
            required_tables = {
                "metadata", "source_evidence", "nodes", "edges", "edge_evidence",
            }
            actual_tables = {
                row["name"]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            missing = sorted(required_tables - actual_tables)
            if missing:
                raise GraphContractError(f"graph SQLite missing tables: {missing}")

            metadata: dict[str, Any] = {}
            for row in connection.execute("SELECT key, value FROM metadata"):
                try:
                    metadata[row["key"]] = json.loads(row["value"])
                except json.JSONDecodeError as exc:
                    raise GraphContractError(
                        f"metadata value is not canonical JSON: {row['key']}"
                    ) from exc
            required_metadata = {
                "schema_version": SCHEMA_VERSION,
                "builder": EXPECTED_BUILDER,
                "status": "complete",
                "question_independent": True,
                "external_network_used": False,
            }
            for key, expected in required_metadata.items():
                if metadata.get(key) != expected:
                    raise GraphContractError(
                        f"metadata {key} must equal {expected!r}"
                    )
            if not isinstance(metadata.get("builder_version"), str):
                raise GraphContractError("metadata builder_version is missing")
            for key in ("documents_input_sha256", "evidence_input_sha256"):
                if not _is_sha256(metadata.get(key)):
                    raise GraphContractError(f"metadata {key} is invalid")
            stored_snapshot_id = metadata.get("graph_snapshot_id")
            if not isinstance(stored_snapshot_id, str) or not stored_snapshot_id.startswith(
                GRAPH_SNAPSHOT_PREFIX
            ):
                raise GraphContractError("graph_snapshot_id is missing or invalid")

            evidence: dict[str, SourceEvidence] = {}
            for row in connection.execute(
                "SELECT evidence_id, document_id, relative_path, source_sha256, "
                "locator_json, observed_text, observed_sha256, record_sha256 "
                "FROM source_evidence "
                "ORDER BY evidence_id"
            ):
                evidence_id = row["evidence_id"]
                if evidence_id in evidence:
                    raise GraphContractError(f"duplicate source Evidence: {evidence_id}")
                observed_text = row["observed_text"]
                observed_sha256 = row["observed_sha256"]
                if sha256_text(observed_text) != observed_sha256:
                    raise GraphContractError(
                        f"source Evidence text hash mismatch: {evidence_id}"
                    )
                locator = _object(row["locator_json"], f"{evidence_id}.locator")
                evidence_payload = {
                    "evidence_id": evidence_id,
                    "document_id": row["document_id"],
                    "relative_path": row["relative_path"],
                    "source_sha256": row["source_sha256"],
                    "locator": locator,
                    "observed_text": observed_text,
                    "observed_sha256": observed_sha256,
                }
                if sha256_value(evidence_payload) != row["record_sha256"]:
                    raise GraphContractError(
                        f"source Evidence record hash mismatch: {evidence_id}"
                    )
                evidence[evidence_id] = SourceEvidence(
                    evidence_id=evidence_id,
                    document_id=row["document_id"],
                    relative_path=row["relative_path"],
                    source_sha256=row["source_sha256"],
                    locator=locator,
                    observed_text=observed_text,
                    observed_sha256=observed_sha256,
                    record_sha256=row["record_sha256"],
                )

            nodes: dict[str, Node] = {}
            for row in connection.execute(
                "SELECT node_id, node_type, canonical_key, status, properties_json, "
                "record_sha256 FROM nodes ORDER BY node_id"
            ):
                node_id = row["node_id"]
                properties = _object(row["properties_json"], f"{node_id}.properties")
                payload = {
                    "node_id": node_id,
                    "node_type": row["node_type"],
                    "canonical_key": row["canonical_key"],
                    "status": row["status"],
                    "properties": properties,
                }
                if sha256_value(payload) != row["record_sha256"]:
                    raise GraphContractError(f"Node hash mismatch: {node_id}")
                if node_id in nodes:
                    raise GraphContractError(f"duplicate Node: {node_id}")
                if row["status"] != "verified":
                    raise GraphContractError(
                        f"answer graph contains non-verified Node: {node_id}"
                    )
                if row["node_type"] not in NODE_TYPES:
                    raise GraphContractError(
                        f"answer graph contains unknown Node type: {node_id}"
                    )
                if not isinstance(row["canonical_key"], str) or not row[
                    "canonical_key"
                ].strip():
                    raise GraphContractError(f"Node canonical key is empty: {node_id}")
                nodes[node_id] = Node(
                    node_id=node_id,
                    node_type=row["node_type"],
                    canonical_key=row["canonical_key"],
                    status=row["status"],
                    properties=properties,
                    record_sha256=row["record_sha256"],
                )

            support: dict[str, list[str]] = {}
            seen_support_pairs: set[tuple[str, str]] = set()
            for row in connection.execute(
                "SELECT edge_id, evidence_id FROM edge_evidence "
                "ORDER BY edge_id, evidence_id"
            ):
                if row["evidence_id"] not in evidence:
                    raise GraphContractError(
                        f"Edge references unknown Evidence: {row['edge_id']}"
                    )
                pair = (row["edge_id"], row["evidence_id"])
                if pair in seen_support_pairs:
                    raise GraphContractError(
                        f"duplicate Edge/Evidence support pair: {pair}"
                    )
                seen_support_pairs.add(pair)
                support.setdefault(row["edge_id"], []).append(row["evidence_id"])

            edges: dict[str, Edge] = {}
            for row in connection.execute(
                "SELECT edge_id, from_node_id, relation_type, to_node_id, "
                "relation_class, status, basis_kind, basis_rule, properties_json, "
                "record_sha256 FROM edges ORDER BY edge_id"
            ):
                edge_id = row["edge_id"]
                supporting_ids = tuple(sorted(support.get(edge_id, [])))
                properties = _object(row["properties_json"], f"{edge_id}.properties")
                payload = {
                    "edge_id": edge_id,
                    "from_node_id": row["from_node_id"],
                    "relation_type": row["relation_type"],
                    "to_node_id": row["to_node_id"],
                    "relation_class": row["relation_class"],
                    "status": row["status"],
                    "basis_kind": row["basis_kind"],
                    "basis_rule": row["basis_rule"],
                    "properties": properties,
                    "supporting_evidence_ids": list(supporting_ids),
                }
                if sha256_value(payload) != row["record_sha256"]:
                    raise GraphContractError(f"Edge hash mismatch: {edge_id}")
                if edge_id in edges:
                    raise GraphContractError(f"duplicate Edge: {edge_id}")
                if row["from_node_id"] not in nodes or row["to_node_id"] not in nodes:
                    raise GraphContractError(f"Edge endpoint is unknown: {edge_id}")
                expected_endpoint_types = RELATION_ENDPOINT_TYPES.get(
                    row["relation_type"]
                )
                actual_endpoint_types = (
                    nodes[row["from_node_id"]].node_type,
                    nodes[row["to_node_id"]].node_type,
                )
                if expected_endpoint_types is None:
                    raise GraphContractError(
                        f"answer graph contains unknown relation type: {edge_id}"
                    )
                if actual_endpoint_types != expected_endpoint_types:
                    raise GraphContractError(
                        "relation endpoint type mismatch: "
                        f"{edge_id}: {actual_endpoint_types} != "
                        f"{expected_endpoint_types}"
                    )
                if not supporting_ids:
                    raise GraphContractError(f"Edge has no supporting Evidence: {edge_id}")
                if row["relation_class"] != "semantic" or row["status"] != "verified":
                    raise GraphContractError(
                        f"answer graph contains non-verified semantic Edge: {edge_id}"
                    )
                edges[edge_id] = Edge(
                    edge_id=edge_id,
                    from_node_id=row["from_node_id"],
                    relation_type=row["relation_type"],
                    to_node_id=row["to_node_id"],
                    relation_class=row["relation_class"],
                    status=row["status"],
                    basis_kind=row["basis_kind"],
                    basis_rule=row["basis_rule"],
                    properties=properties,
                    supporting_evidence_ids=supporting_ids,
                    record_sha256=row["record_sha256"],
                )

            unknown_support_edges = sorted(set(support) - set(edges))
            if unknown_support_edges:
                raise GraphContractError(
                    f"Evidence support references unknown Edge: {unknown_support_edges}"
                )

            snapshot_payload = {
                "evidence_record_sha256": sorted(
                    item.record_sha256 for item in evidence.values()
                ),
                "node_record_sha256": sorted(
                    node.record_sha256 for node in nodes.values()
                ),
                "edge_record_sha256": sorted(
                    edge.record_sha256 for edge in edges.values()
                ),
            }
            logical_snapshot_sha256 = sha256_value(snapshot_payload)
            computed_snapshot_id = GRAPH_SNAPSHOT_PREFIX + logical_snapshot_sha256[:32]
            if computed_snapshot_id != stored_snapshot_id:
                raise GraphContractError(
                    "logical graph snapshot hash does not match metadata"
                )
            if metadata.get("logical_snapshot_sha256") != logical_snapshot_sha256:
                raise GraphContractError("logical_snapshot_sha256 does not match graph")
            expected_counts = {
                "document_count": len({item.document_id for item in evidence.values()}),
                "source_evidence_count": len(evidence),
                "node_count": len(nodes),
                "edge_count": len(edges),
            }
            for key, expected in expected_counts.items():
                value = metadata.get(key)
                if type(value) is not int or value != expected:
                    raise GraphContractError(
                        f"metadata {key} does not match graph: {value!r} != {expected}"
                    )
            return cls(stored_snapshot_id, nodes, edges, evidence)
        finally:
            connection.close()

    def active_edges(self, disabled_edge_ids: set[str]) -> list[Edge]:
        unknown = sorted(disabled_edge_ids - set(self.edges))
        if unknown:
            raise GraphContractError(f"disabled Edge is unknown: {unknown}")
        return [
            edge
            for edge in self.edges.values()
            if edge.edge_id not in disabled_edge_ids
        ]


class Traversal:
    def __init__(
        self,
        snapshot: GraphSnapshot,
        disabled_edge_ids: Iterable[str] = (),
    ) -> None:
        self.snapshot = snapshot
        self.disabled_edge_ids = set(disabled_edge_ids)
        self.edges = snapshot.active_edges(self.disabled_edge_ids)
        self.used_edge_ids: list[str] = []
        self.visited_node_ids: set[str] = set()

    def candidates(
        self,
        relation_type: str,
        *,
        from_node_id: str | None = None,
        to_node_id: str | None = None,
    ) -> list[Edge]:
        return sorted(
            [
                edge
                for edge in self.edges
                if edge.relation_type == relation_type
                and (from_node_id is None or edge.from_node_id == from_node_id)
                and (to_node_id is None or edge.to_node_id == to_node_id)
            ],
            key=lambda edge: edge.edge_id,
        )

    def one(
        self,
        relation_type: str,
        *,
        from_node_id: str | None = None,
        to_node_id: str | None = None,
        reason_code: str,
    ) -> Edge:
        candidates = self.candidates(
            relation_type, from_node_id=from_node_id, to_node_id=to_node_id
        )
        if len(candidates) != 1:
            raise ResolutionError(
                reason_code,
                f"{relation_type} must resolve to one verified Edge; got {len(candidates)}",
            )
        self.use(candidates[0])
        return candidates[0]

    def use(self, edge: Edge) -> Edge:
        if edge.edge_id not in self.used_edge_ids:
            self.used_edge_ids.append(edge.edge_id)
        self.visited_node_ids.update((edge.from_node_id, edge.to_node_id))
        return edge

    def node(self, node_id: str) -> Node:
        try:
            return self.snapshot.nodes[node_id]
        except KeyError as exc:
            raise GraphContractError(f"unknown Node: {node_id}") from exc


def _normalize_surface(value: str) -> str:
    return unicodedata.normalize("NFC", value).strip()


def _normalize_for_match(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold().strip()


def _strict_reference_date(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("reference_date must be strict ISO YYYY-MM-DD")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(
            "reference_date must be strict ISO YYYY-MM-DD"
        ) from exc
    if parsed.isoformat() != value:
        raise ValueError("reference_date must be strict ISO YYYY-MM-DD")
    return value


def _japanese_integer(value: str) -> int | None:
    normalized = unicodedata.normalize("NFKC", value)
    if normalized.isdecimal():
        return int(normalized)
    digits = {
        "一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
        "六": 6, "七": 7, "八": 8, "九": 9, "〇": 0, "零": 0,
    }
    if "十" in normalized:
        if normalized.count("十") != 1:
            return None
        tens_text, ones_text = normalized.split("十")
        if len(tens_text) > 1 or len(ones_text) > 1:
            return None
        tens = digits.get(tens_text, 1) if tens_text else 1
        ones = digits.get(ones_text, 0) if ones_text else 0
        if tens is None or ones is None or tens == 0:
            return None
        return tens * 10 + ones
    if not normalized or any(character not in digits for character in normalized):
        return None
    return int("".join(str(digits[character]) for character in normalized))


def _subtract_calendar_years(reference: date, years: int) -> date:
    target_year = reference.year - years
    if target_year < 1:
        raise ResolutionError(
            "reference_time_invalid", "relative date is outside the supported range"
        )
    try:
        return reference.replace(year=target_year)
    except ValueError as exc:
        if reference.month == 2 and reference.day == 29:
            return date(target_year, 2, 28)
        raise ResolutionError(
            "reference_time_invalid", "relative date could not be resolved"
        ) from exc


def _has_unconsumed_temporal_context(
    question: str,
    start: int,
    end: int,
    *,
    relative: bool,
    project_surfaces: Iterable[str],
    work_surfaces: Iterable[str],
) -> bool:
    """Reject a point date when surrounding temporal language is left over.

    The bounded owner query supports exactly one calendar day.  Consume only
    the small grammar that preserves that meaning (for example, ``今から``
    before ``5年前`` and ``時点で`` after a date).  Any remaining date,
    range, modifier, event anchor, negation, or uncertainty signal makes the
    reference time ambiguous and therefore fail-closed.
    """

    left = question[:start]
    right = question[end:]
    prefix_pattern = RELATIVE_ALLOWED_PREFIX if relative else ABSOLUTE_ALLOWED_PREFIX
    prefix_match = prefix_pattern.search(left)
    if prefix_match is None:
        return True
    if TEMPORAL_LEFT_BOUNDARY.search(left[:prefix_match.start()]) is None:
        return True
    subject_boundary = prefix_match.start()
    subject_matches: list[tuple[int, int, int, int]] = []
    for work_surface in work_surfaces:
        normalized_work = _normalize_for_match(work_surface)
        if not normalized_work:
            continue
        work_start = question.rfind(normalized_work, 0, subject_boundary)
        if work_start < 0:
            continue
        work_end = work_start + len(normalized_work)
        if WORK_TO_TIME_BRIDGE.fullmatch(
            question[work_end:subject_boundary]
        ) is None:
            continue
        for project_surface in project_surfaces:
            normalized_project = _normalize_for_match(project_surface)
            if not normalized_project:
                continue
            project_start = question.rfind(normalized_project, 0, work_start)
            if project_start < 0:
                continue
            project_end = project_start + len(normalized_project)
            if question[:project_start].strip():
                continue
            if PROJECT_TO_WORK_BRIDGE.fullmatch(
                question[project_end:work_start]
            ) is None:
                continue
            subject_matches.append(
                (project_start, project_end, work_start, work_end)
            )
    if len(set(subject_matches)) != 1:
        return True
    suffix_match = EXACT_TIME_ALLOWED_SUFFIX.match(right)
    if suffix_match is None:
        return True
    if OWNER_TIME_QUESTION_TAIL.fullmatch(right[suffix_match.end():]) is None:
        return True
    remainder = left[:prefix_match.start()] + " " + right[suffix_match.end():]
    return UNCONSUMED_TEMPORAL_SIGNAL.search(remainder) is not None


def _parse_question_date(
    question: str,
    reference_date: str | None = None,
    *,
    project_surfaces: Iterable[str] = (),
    work_surfaces: Iterable[str] = (),
) -> str | None:
    question = _normalize_for_match(question)
    if (
        ABSOLUTE_DATE_APPROX_OR_RANGE.search(question)
        or ABSOLUTE_DATE_CALENDAR_DETAIL.search(question)
    ):
        raise ResolutionError(
            "reference_time_ambiguous",
            "approximate, ranged, or bounded absolute dates are unsupported",
        )
    explicit_matches: list[tuple[re.Match[str], str]] = []
    for pattern in (DATE_JA, DATE_ISO):
        for match in pattern.finditer(question):
            try:
                value = date(
                    int(match.group("year")),
                    int(match.group("month")),
                    int(match.group("day")),
                ).isoformat()
            except ValueError as exc:
                raise ResolutionError(
                    "reference_time_invalid", "question contains an invalid date"
                ) from exc
            explicit_matches.append((match, value))
    relative_matches = list(RELATIVE_YEARS_AGO.finditer(question))
    if len(explicit_matches) > 1 or (explicit_matches and relative_matches):
        raise ResolutionError(
            "reference_time_ambiguous", "question contains multiple reference dates"
        )
    if explicit_matches:
        explicit_match, explicit_value = explicit_matches[0]
        if _has_unconsumed_temporal_context(
            question,
            explicit_match.start(),
            explicit_match.end(),
            relative=False,
            project_surfaces=project_surfaces,
            work_surfaces=work_surfaces,
        ):
            raise ResolutionError(
                "reference_time_ambiguous",
                "non-exact or mixed absolute dates are unsupported",
            )
        return explicit_value
    if relative_matches:
        guarded_question = ALLOWED_RUN_DATE_ANCHOR.sub(
            lambda match: match.group("relative"), question
        )
        temporal_remainder = RELATIVE_YEARS_AGO.sub(" ", guarded_question)
        if (
            RELATIVE_YEAR_APPROX_OR_RANGE.search(question)
            or RELATIVE_YEAR_CALENDAR_DETAIL.search(question)
            or EVENT_RELATIVE_YEAR.search(question)
            or ARBITRARY_RELATIVE_YEAR_ANCHOR.search(guarded_question)
            or OTHER_TEMPORAL_CONTEXT.search(temporal_remainder)
        ):
            raise ResolutionError(
                "reference_time_ambiguous",
                "non-exact or mixed relative dates are unsupported",
            )
        if len(relative_matches) != 1:
            raise ResolutionError(
                "reference_time_ambiguous",
                "question contains multiple reference dates",
            )
        relative_match = relative_matches[0]
        if _has_unconsumed_temporal_context(
            question,
            relative_match.start(),
            relative_match.end(),
            relative=True,
            project_surfaces=project_surfaces,
            work_surfaces=work_surfaces,
        ):
            raise ResolutionError(
                "reference_time_ambiguous",
                "non-exact or mixed relative dates are unsupported",
            )
        relative_years: set[int] = set()
        for match in relative_matches:
            years = _japanese_integer(match.group("years"))
            if years is None or not 1 <= years <= 99:
                raise ResolutionError(
                    "reference_time_invalid",
                    "relative year offset is outside the supported range",
                )
            relative_years.add(years)
        if len(relative_years) != 1:
            raise ResolutionError(
                "reference_time_ambiguous",
                "question contains multiple reference dates",
            )
        if reference_date is None:
            raise ResolutionError(
                "reference_time_required",
                "相対日付を解決する実行基準日を確認できません。",
            )
        reference = date.fromisoformat(reference_date)
        return _subtract_calendar_years(
            reference, next(iter(relative_years))
        ).isoformat()
    return None


def _question_has_reference_time(question: str) -> bool:
    normalized = _normalize_for_match(question)
    return bool(
        DATE_JA.search(normalized)
        or DATE_ISO.search(normalized)
        or RELATIVE_YEAR_LIKE.search(normalized)
        or OTHER_TEMPORAL_CONTEXT.search(normalized)
        or QUESTION_TIME_SCOPE_SIGNAL.search(normalized)
    )


def _date_property(properties: dict[str, Any], key: str) -> date | None:
    value = properties.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ResolutionError("graph_date_invalid", f"{key} must be an ISO date")
    if re.fullmatch(r"[12]\d{3}-\d{2}-\d{2}", value) is None:
        raise ResolutionError(
            "graph_date_invalid", f"{key} must use YYYY-MM-DD"
        )
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ResolutionError("graph_date_invalid", f"{key} is not an ISO date") from exc


def _inclusive_property(properties: dict[str, Any], key: str) -> bool:
    if key not in properties:
        raise ResolutionError("assignment_period_incomplete", f"{key} is missing")
    value = properties[key]
    if not isinstance(value, bool):
        raise ResolutionError("graph_date_invalid", f"{key} must be a boolean")
    return value


def _effective_start(edge: Edge) -> date | None:
    value = _date_property(edge.properties, "valid_from")
    if value is None:
        return None
    if not _inclusive_property(edge.properties, "valid_from_inclusive"):
        return value + timedelta(days=1)
    return value


def _effective_end(edge: Edge) -> date | None:
    value = _date_property(edge.properties, "valid_to")
    if value is None:
        return None
    if not _inclusive_property(edge.properties, "valid_to_inclusive"):
        return value - timedelta(days=1)
    return value


def _operation(question: str) -> str:
    normalized = _normalize_for_match(question)
    old_version_signal = any(
        token in normalized for token in ("旧案", "旧版", "前版")
    )
    version_comparison_signal = any(
        token in normalized
        for token in ("変わ", "変更", "違", "差分", "背景", "経緯", "理由")
    )
    if re.search(r"変更.{0,4}理由", normalized) or (
        old_version_signal and version_comparison_signal
    ):
        return "version_change"
    if (
        "切り替" in normalized
        or "交代" in normalized
        or "変更日" in normalized
        or (
            "いつ" in normalized
            and ("変わ" in normalized or "変更" in normalized)
            and ("担当" in normalized or "前後" in normalized)
        )
        or (
        "変更前" in normalized and "変更後" in normalized
        )
        or ("前任" in normalized and "後任" in normalized)
    ):
        return "assignment_change"
    if ("担当" in normalized or "受け持" in normalized) and any(
        token in normalized for token in ("誰", "どなた", "教えて")
    ):
        return "owner"
    raise ResolutionError(
        "question_operation_unsupported",
        "question is outside the bounded semantic graph operations",
    )


def _resolve_subject(
    traversal: Traversal, question: str
) -> tuple[Node, Node, list[Edge]]:
    project_candidates: list[tuple[Node, Edge | None]] = []
    comparable_question = _normalize_for_match(question)
    for edge in traversal.candidates("HAS_ALIAS"):
        alias = traversal.node(edge.to_node_id)
        if alias.canonical_key and _normalize_for_match(alias.canonical_key) in comparable_question:
            project_candidates.append((traversal.node(edge.from_node_id), edge))
    for node in traversal.snapshot.nodes.values():
        if (
            node.node_type == "Project"
            and _normalize_for_match(node.canonical_key) in comparable_question
        ):
            project_candidates.append((node, None))
    distinct_projects = {node.node_id for node, _edge in project_candidates}
    if len(distinct_projects) != 1:
        raise ResolutionError(
            "project_identity_not_unique",
            f"question must identify one Project; got {len(distinct_projects)}",
        )
    project_id = next(iter(distinct_projects))
    project = traversal.node(project_id)
    project_edges = [
        edge for node, edge in project_candidates
        if node.node_id == project_id and edge is not None
    ]
    if project_edges:
        traversal.use(sorted(project_edges, key=lambda edge: edge.edge_id)[0])

    work_candidates: list[tuple[Node, Edge | None]] = []
    for edge in traversal.candidates("HAS_NAME"):
        work_name = traversal.node(edge.to_node_id)
        if (
            work_name.canonical_key
            and _normalize_for_match(work_name.canonical_key) in comparable_question
        ):
            work_candidates.append((traversal.node(edge.from_node_id), edge))
    for node in traversal.snapshot.nodes.values():
        if (
            node.node_type == "Work"
            and _normalize_for_match(node.canonical_key) in comparable_question
        ):
            work_candidates.append((node, None))
    distinct_works = {node.node_id for node, _edge in work_candidates}
    if len(distinct_works) != 1:
        raise ResolutionError(
            "work_identity_not_unique",
            f"question must identify one Work; got {len(distinct_works)}",
        )
    work_id = next(iter(distinct_works))
    work = traversal.node(work_id)
    work_name_edges = [
        edge for node, edge in work_candidates
        if node.node_id == work_id and edge is not None
    ]
    if work_name_edges:
        traversal.use(sorted(work_name_edges, key=lambda edge: edge.edge_id)[0])

    traversal.one(
        "CONTAINS_WORK",
        from_node_id=project.node_id,
        to_node_id=work.node_id,
        reason_code="project_work_path_missing",
    )
    selected_edges = [
        traversal.snapshot.edges[edge_id] for edge_id in traversal.used_edge_ids
    ]
    return project, work, selected_edges


def _subject_surfaces(
    traversal: Traversal,
    project: Node,
    work: Node,
) -> tuple[set[str], set[str]]:
    project_surfaces = {project.canonical_key}
    project_surfaces.update(
        traversal.node(edge.to_node_id).canonical_key
        for edge in traversal.candidates(
            "HAS_ALIAS", from_node_id=project.node_id
        )
    )
    work_surfaces = {work.canonical_key}
    work_surfaces.update(
        traversal.node(edge.to_node_id).canonical_key
        for edge in traversal.candidates(
            "HAS_NAME", from_node_id=work.node_id
        )
    )
    return project_surfaces, work_surfaces


def _non_owner_question_form_supported(
    question: str,
    operation: str,
    project_surfaces: Iterable[str],
    work_surfaces: Iterable[str],
) -> bool:
    normalized = _normalize_for_match(question)
    tail_pattern = {
        "assignment_change": ASSIGNMENT_CHANGE_QUESTION_TAIL,
        "version_change": VERSION_CHANGE_QUESTION_TAIL,
    }.get(operation)
    if tail_pattern is None:
        return False
    matching_layouts: set[tuple[int, int, int, int]] = set()
    for project_surface in project_surfaces:
        normalized_project = _normalize_for_match(project_surface)
        if not normalized_project or not normalized.startswith(normalized_project):
            continue
        project_start = 0
        project_end = len(normalized_project)
        for work_surface in work_surfaces:
            normalized_work = _normalize_for_match(work_surface)
            if not normalized_work:
                continue
            work_start = normalized.find(normalized_work, project_end)
            if work_start < 0:
                continue
            work_end = work_start + len(normalized_work)
            if PROJECT_TO_WORK_BRIDGE.fullmatch(
                normalized[project_end:work_start]
            ) is None:
                continue
            if tail_pattern.fullmatch(normalized[work_end:]) is None:
                continue
            matching_layouts.add(
                (project_start, project_end, work_start, work_end)
            )
    return len(matching_layouts) == 1


def _role_for_question(question: str, assignment_edges: list[Edge]) -> str:
    roles = {
        str(edge.properties.get("role"))
        for edge in assignment_edges
        if isinstance(edge.properties.get("role"), str)
        and str(edge.properties.get("role")).strip()
    }
    mentioned = sorted(role for role in roles if role in question)
    if len(mentioned) == 1:
        return mentioned[0]
    if not mentioned and len(roles) == 1:
        return next(iter(roles))
    raise ResolutionError(
        "assignment_role_not_unique",
        f"question must identify one assignment role; got {sorted(roles)}",
    )


def _validate_assignment_edges(assignment_edges: list[Edge]) -> None:
    if not assignment_edges:
        raise ResolutionError(
            "assignment_path_missing", "no verified assignment Edge was found"
        )
    approved_statuses = {
        "active",
        "approved",
        "current",
        "effective",
        "final",
        "signed",
        "有効",
        "承認済み",
        "署名済み/承認済み",
    }
    for edge in assignment_edges:
        role = edge.properties.get("role")
        source_status = edge.properties.get("source_status")
        if not isinstance(role, str) or not role.strip():
            raise ResolutionError(
                "assignment_semantics_inconsistent", "assignment role is missing"
            )
        if (
            not isinstance(source_status, str)
            or _normalize_for_match(source_status) not in approved_statuses
        ):
            raise ResolutionError(
                "assignment_semantics_inconsistent",
                "assignment source status is not final/approved",
            )
        start = _effective_start(edge)
        if start is None:
            raise ResolutionError(
                "assignment_period_incomplete", "an assignment is missing valid_from"
            )
        end = _effective_end(edge)
        if end is not None and end < start:
            raise ResolutionError(
                "assignment_semantics_inconsistent",
                "assignment effective end precedes its start",
            )


def _person_for_employee(
    traversal: Traversal, employee_node_id: str
) -> tuple[Node, Edge]:
    identity = traversal.one(
        "IDENTIFIES_PERSON",
        from_node_id=employee_node_id,
        reason_code="employee_person_identity_missing",
    )
    person = traversal.node(identity.to_node_id)
    if person.node_type != "Person":
        raise ResolutionError(
            "employee_person_identity_invalid",
            "IDENTIFIES_PERSON did not resolve to a Person Node",
        )
    return person, identity


def _fact(field: str, value: str, *proof_edges: Edge) -> dict[str, Any]:
    return {
        "field": field,
        "value": value,
        "proof_edge_ids": sorted({edge.edge_id for edge in proof_edges}),
    }


def _answer_owner(
    traversal: Traversal,
    question: str,
    project: Node,
    work: Node,
    reference_date: str | None = None,
) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]]]:
    assignments = traversal.candidates("ASSIGNED_TO", from_node_id=work.node_id)
    _validate_assignment_edges(assignments)
    role = _role_for_question(question, assignments)
    assignments = [edge for edge in assignments if edge.properties.get("role") == role]
    project_surfaces, work_surfaces = _subject_surfaces(
        traversal, project, work
    )
    reference_time = _parse_question_date(
        question,
        reference_date,
        project_surfaces=project_surfaces,
        work_surfaces=work_surfaces,
    )
    if reference_time is None:
        for edge in assignments:
            traversal.use(edge)
        if len(assignments) >= 2:
            raise ResolutionError(
                "reference_time_required",
                "複数の担当期間があるため、基準日を指定してください。",
            )
        raise ResolutionError(
            "reference_time_required", "担当者を決める基準日を指定してください。"
        )

    target = date.fromisoformat(reference_time)
    active: list[Edge] = []
    for edge in assignments:
        start = _effective_start(edge)
        end = _effective_end(edge)
        if start is None:
            raise ResolutionError(
                "assignment_period_incomplete",
                "a competing assignment is missing valid_from",
            )
        if start <= target and (end is None or target <= end):
            active.append(edge)
    if len(active) != 1:
        raise ResolutionError(
            "assignment_at_time_not_unique",
            f"reference time resolves to {len(active)} assignments",
        )
    assignment = traversal.use(active[0])
    employee = traversal.node(assignment.to_node_id)
    person, identity = _person_for_employee(traversal, employee.node_id)
    facts = [
        _fact("reference_time", reference_time, assignment),
        _fact("role", role, assignment),
        _fact("assignee_id", employee.canonical_key, assignment),
        _fact("assignee_name", person.canonical_key, assignment, identity),
    ]
    answer = (
        f"{reference_time}時点の{role}は{person.canonical_key}"
        f"（社員ID: {employee.canonical_key}）です。"
    )
    return answer, facts, []


def _answer_assignment_change(
    traversal: Traversal,
    question: str,
    work: Node,
) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]]]:
    assignments = traversal.candidates("ASSIGNED_TO", from_node_id=work.node_id)
    _validate_assignment_edges(assignments)
    role = _role_for_question(question, assignments)
    assignments = [edge for edge in assignments if edge.properties.get("role") == role]
    ordered = sorted(
        assignments,
        key=lambda edge: (_effective_start(edge), edge.edge_id),
    )
    changes: list[tuple[Edge, Edge]] = []
    for previous, current in zip(ordered, ordered[1:]):
        previous_end = _effective_end(previous)
        current_start = _effective_start(current)
        if current_start is None:
            raise ResolutionError(
                "assignment_period_incomplete", "an assignment is missing valid_from"
            )
        if previous_end is None:
            raise ResolutionError(
                "assignment_period_incomplete",
                "a preceding assignment is missing valid_to",
            )
        if (
            previous.to_node_id != current.to_node_id
            and previous_end + timedelta(days=1) == current_start
        ):
            changes.append((previous, current))
    if len(changes) != 1:
        raise ResolutionError(
            "assignment_change_not_unique",
            f"question resolves to {len(changes)} contiguous assignment changes",
        )
    previous, current = changes[0]
    traversal.use(previous)
    traversal.use(current)
    previous_employee = traversal.node(previous.to_node_id)
    current_employee = traversal.node(current.to_node_id)
    previous_person, previous_identity = _person_for_employee(
        traversal, previous_employee.node_id
    )
    current_person, current_identity = _person_for_employee(
        traversal, current_employee.node_id
    )
    previous_end = _effective_end(previous)
    current_start = _effective_start(current)
    assert previous_end is not None and current_start is not None
    facts = [
        _fact("change_effective_date", current_start.isoformat(), current),
        _fact("previous_valid_to", previous_end.isoformat(), previous),
        _fact("from_assignee_id", previous_employee.canonical_key, previous),
        _fact(
            "from_assignee_name",
            previous_person.canonical_key,
            previous,
            previous_identity,
        ),
        _fact("to_assignee_id", current_employee.canonical_key, current),
        _fact(
            "to_assignee_name",
            current_person.canonical_key,
            current,
            current_identity,
        ),
    ]
    answer = (
        f"{role}は{current_start.isoformat()}に"
        f"{previous_person.canonical_key}（{previous_employee.canonical_key}）から"
        f"{current_person.canonical_key}（{current_employee.canonical_key}）へ"
        f"切り替わりました。変更前の有効期限は{previous_end.isoformat()}です。"
    )
    return answer, facts, []


def _required_string_property(
    properties: dict[str, Any], key: str, context: str
) -> str:
    value = properties.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ResolutionError(
            "claim_semantics_inconsistent", f"{context}.{key} is missing"
        )
    return value


def _required_claim_date(
    properties: dict[str, Any], key: str, context: str
) -> str:
    value = _required_string_property(properties, key, context)
    if re.fullmatch(r"[12]\d{3}-\d{2}-\d{2}", value) is None:
        raise ResolutionError(
            "claim_semantics_inconsistent",
            f"{context}.{key} must use YYYY-MM-DD",
        )
    try:
        date.fromisoformat(value)
    except ValueError as exc:
        raise ResolutionError(
            "claim_semantics_inconsistent", f"{context}.{key} is invalid"
        ) from exc
    return value


def _validate_claim_semantics(
    *,
    work: Node,
    current_claim: Node,
    old_claim: Node,
    current_link: Edge,
    old_link: Edge,
    current_assignment: Edge,
    old_assignment: Edge,
    contradiction: Edge,
) -> tuple[str, str, str]:
    current_records = (
        ("current_claim", current_claim.properties),
        ("current_link", current_link.properties),
        ("current_assignment", current_assignment.properties),
    )
    old_records = (
        ("old_claim", old_claim.properties),
        ("old_link", old_link.properties),
        ("old_assignment", old_assignment.properties),
    )
    for context, properties in current_records:
        if properties.get("current") is not True:
            raise ResolutionError(
                "claim_semantics_inconsistent", f"{context}.current must be true"
            )
        if properties.get("claim_status") != "APPROVED":
            raise ResolutionError(
                "claim_semantics_inconsistent",
                f"{context}.claim_status must be APPROVED",
            )
    for context, properties in old_records:
        if properties.get("current") is not False:
            raise ResolutionError(
                "claim_semantics_inconsistent", f"{context}.current must be false"
            )
        if properties.get("claim_status") != "DRAFT":
            raise ResolutionError(
                "claim_semantics_inconsistent",
                f"{context}.claim_status must be DRAFT",
            )

    current_dates = {
        _required_claim_date(properties, "effective_from", context)
        for context, properties in current_records
    }
    old_dates = {
        _required_claim_date(properties, "effective_from", context)
        for context, properties in old_records
    }
    if len(current_dates) != 1 or old_dates != current_dates:
        raise ResolutionError(
            "claim_semantics_inconsistent",
            "old/current effective_from values must agree",
        )

    current_role = _required_string_property(
        current_assignment.properties, "role", "current_assignment"
    )
    old_role = _required_string_property(
        old_assignment.properties, "role", "old_assignment"
    )
    claim_roles = {
        _required_string_property(current_claim.properties, "role", "current_claim"),
        _required_string_property(old_claim.properties, "role", "old_claim"),
        current_role,
        old_role,
    }
    if len(claim_roles) != 1:
        raise ResolutionError(
            "claim_semantics_inconsistent", "old/current roles must agree"
        )

    project_ids = {
        _required_string_property(current_claim.properties, "project_id", "current_claim"),
        _required_string_property(old_claim.properties, "project_id", "old_claim"),
    }
    work_ids = {
        _required_string_property(current_claim.properties, "work_id", "current_claim"),
        _required_string_property(old_claim.properties, "work_id", "old_claim"),
    }
    if len(project_ids) != 1 or work_ids != {work.canonical_key}:
        raise ResolutionError(
            "claim_semantics_inconsistent",
            "old/current Claims must identify the selected Project and Work",
        )
    versions = {
        _required_string_property(current_claim.properties, "version", "current_claim"),
        _required_string_property(old_claim.properties, "version", "old_claim"),
    }
    if len(versions) != 2:
        raise ResolutionError(
            "claim_semantics_inconsistent", "old/current Claim versions must differ"
        )
    if old_assignment.to_node_id == current_assignment.to_node_id:
        raise ResolutionError(
            "claim_semantics_inconsistent", "old/current assignees must differ"
        )
    dimensions = contradiction.properties.get("comparison_dimensions")
    required_dimensions = {"work", "role", "effective_from", "assignee"}
    if (
        not isinstance(dimensions, list)
        or any(not isinstance(value, str) for value in dimensions)
        or not required_dimensions.issubset(set(dimensions))
    ):
        raise ResolutionError(
            "claim_semantics_inconsistent",
            "CONTRADICTS comparison dimensions are incomplete",
        )
    return next(iter(current_dates)), "DRAFT", "APPROVED"


def _answer_version_change(
    traversal: Traversal,
    work: Node,
) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]]]:
    current_link = traversal.one(
        "HAS_CURRENT_CLAIM",
        from_node_id=work.node_id,
        reason_code="current_claim_missing",
    )
    current_claim = traversal.node(current_link.to_node_id)
    supersedes = traversal.one(
        "SUPERSEDES",
        from_node_id=current_claim.node_id,
        reason_code="superseded_claim_missing",
    )
    old_claim = traversal.node(supersedes.to_node_id)
    old_link = traversal.one(
        "HAS_CLAIM",
        from_node_id=work.node_id,
        to_node_id=old_claim.node_id,
        reason_code="old_claim_work_link_missing",
    )
    contradiction = traversal.one(
        "CONTRADICTS",
        from_node_id=old_claim.node_id,
        to_node_id=current_claim.node_id,
        reason_code="claim_comparison_missing",
    )
    old_assignment = traversal.one(
        "CLAIMS_ASSIGNEE",
        from_node_id=old_claim.node_id,
        reason_code="old_claim_assignee_missing",
    )
    current_assignment = traversal.one(
        "CLAIMS_ASSIGNEE",
        from_node_id=current_claim.node_id,
        reason_code="current_claim_assignee_missing",
    )
    old_employee = traversal.node(old_assignment.to_node_id)
    current_employee = traversal.node(current_assignment.to_node_id)
    effective_from, old_status, current_status = _validate_claim_semantics(
        work=work,
        current_claim=current_claim,
        old_claim=old_claim,
        current_link=current_link,
        old_link=old_link,
        current_assignment=current_assignment,
        old_assignment=old_assignment,
        contradiction=contradiction,
    )
    old_person, old_identity = _person_for_employee(traversal, old_employee.node_id)
    current_person, current_identity = _person_for_employee(
        traversal, current_employee.node_id
    )
    reason_edge = traversal.one(
        "HAS_CHANGE_REASON",
        from_node_id=current_claim.node_id,
        reason_code="change_reason_missing",
    )
    reason = traversal.node(reason_edge.to_node_id)

    facts = [
        _fact("effective_from", effective_from, current_link, current_assignment),
        _fact("old_plan_status", old_status, old_link, old_assignment),
        _fact("old_plan_assignee_id", old_employee.canonical_key, old_assignment),
        _fact(
            "old_plan_assignee_name",
            old_person.canonical_key,
            old_assignment,
            old_identity,
        ),
        _fact("current_plan_status", current_status, current_link, current_assignment),
        _fact(
            "current_plan_assignee_id",
            current_employee.canonical_key,
            current_assignment,
        ),
        _fact(
            "current_plan_assignee_name",
            current_person.canonical_key,
            current_assignment,
            current_identity,
        ),
        _fact("change_reason", reason.canonical_key, reason_edge),
    ]
    relations = [
        {
            "from": current_claim.canonical_key,
            "relation": "SUPERSEDES",
            "to": old_claim.canonical_key,
            "proof_edge_ids": [supersedes.edge_id],
        },
        {
            "from": old_claim.canonical_key,
            "relation": "CONTRADICTS",
            "to": current_claim.canonical_key,
            "proof_edge_ids": [contradiction.edge_id],
        },
    ]
    answer = (
        f"承認済み計画では、{effective_from}から担当を"
        f"{old_person.canonical_key}（{old_employee.canonical_key}）から"
        f"{current_person.canonical_key}（{current_employee.canonical_key}）へ変更しています。"
        f"変更理由は「{reason.canonical_key}」です。"
    )
    return answer, facts, relations


def _peak_rss_bytes() -> int:
    value = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    return value if sys.platform == "darwin" else value * 1024


def _trace(
    traversal: Traversal,
    question_hash: str,
    decision: str,
    elapsed_ms: float,
    reference_date: str | None,
) -> dict[str, Any]:
    used_edges = [
        traversal.snapshot.edges[edge_id] for edge_id in traversal.used_edge_ids
    ]
    source_references: list[dict[str, Any]] = []
    for edge in used_edges:
        for evidence_id in edge.supporting_evidence_ids:
            evidence = traversal.snapshot.evidence[evidence_id]
            source_references.append({
                "edge_id": edge.edge_id,
                "evidence_id": evidence_id,
                "document_id": evidence.document_id,
                "path": evidence.relative_path,
                "source_sha256": evidence.source_sha256,
                "locator": evidence.locator,
                "observed_text_sha256": evidence.observed_sha256,
                "quote": evidence.observed_text,
            })
    source_references.sort(
        key=lambda item: (item["edge_id"], item["evidence_id"])
    )
    run_identity = {
        "graph_snapshot_id": traversal.snapshot.graph_snapshot_id,
        "question_hash": question_hash,
        "disabled_edge_ids": sorted(traversal.disabled_edge_ids),
    }
    if reference_date is not None:
        run_identity["question_reference_date"] = reference_date
    return {
        "run_id": RUN_PREFIX + sha256_value(run_identity)[:32],
        "graph_snapshot_id": traversal.snapshot.graph_snapshot_id,
        "question_hash": question_hash,
        "question_reference_date": reference_date,
        "visited_node_ids": sorted(traversal.visited_node_ids),
        "visited_node_hashes": sorted(
            traversal.snapshot.nodes[node_id].record_sha256
            for node_id in traversal.visited_node_ids
        ),
        "visited_edge_ids": list(traversal.used_edge_ids),
        "visited_edge_hashes": [edge.record_sha256 for edge in used_edges],
        "used_semantic_edge_ids": list(traversal.used_edge_ids),
        "used_semantic_edge_count": len(used_edges),
        "used_edge_statuses": sorted({edge.status for edge in used_edges}),
        "visited_document_paths": sorted(
            {item["path"] for item in source_references}
        ),
        "resolved_source_references": source_references,
        "disabled_edge_ids": sorted(traversal.disabled_edge_ids),
        "decision": decision,
        "elapsed_ms": round(elapsed_ms, 3),
        "peak_rss_bytes": _peak_rss_bytes(),
        "outbound_network_attempt_count": 0,
    }


def answer_question(
    snapshot: GraphSnapshot,
    question: str,
    *,
    disabled_edge_ids: Iterable[str] = (),
    reference_date: str | None = None,
) -> dict[str, Any]:
    if not isinstance(question, str) or not question.strip():
        raise ValueError("question must be a non-empty string")
    question = _normalize_surface(question)
    reference_date = _strict_reference_date(reference_date)
    question_hash = sha256_text(question)
    traversal = Traversal(snapshot, disabled_edge_ids)
    started = time.perf_counter()
    decision = "HOLD"
    reason_code: str | None = None
    must_request_concept: str | None = None
    answer_text = "必要な検証済みグラフ経路が足りないため回答できません。"
    asserted_facts: list[dict[str, Any]] = []
    asserted_relations: list[dict[str, Any]] = []
    operation = "unresolved"
    try:
        operation = _operation(question)
        if operation != "owner" and _question_has_reference_time(question):
            raise ResolutionError(
                "temporal_context_unsupported",
                "this graph operation does not support a question-time filter",
            )
        project, work, _identity_edges = _resolve_subject(traversal, question)
        if operation in {"assignment_change", "version_change"}:
            project_surfaces, work_surfaces = _subject_surfaces(
                traversal, project, work
            )
            if not _non_owner_question_form_supported(
                question,
                operation,
                project_surfaces,
                work_surfaces,
            ):
                raise ResolutionError(
                    "temporal_context_unsupported",
                    "question is outside the exact non-temporal operation grammar",
                )
        if operation == "owner":
            answer_text, asserted_facts, asserted_relations = _answer_owner(
                traversal, question, project, work, reference_date
            )
        elif operation == "assignment_change":
            answer_text, asserted_facts, asserted_relations = (
                _answer_assignment_change(traversal, question, work)
            )
        elif operation == "version_change":
            answer_text, asserted_facts, asserted_relations = _answer_version_change(
                traversal, work
            )
        else:  # protected by _operation
            raise ResolutionError(
                "question_operation_unsupported", "unsupported graph operation"
            )
        decision = "ACCEPTED"
    except ResolutionError as exc:
        reason_code = exc.reason_code
        asserted_facts = []
        asserted_relations = []
        if exc.reason_code == "reference_time_required":
            must_request_concept = "基準日"
            answer_text = exc.message
        else:
            answer_text = "必要な検証済みグラフ経路が足りないため回答できません。"

    elapsed_ms = (time.perf_counter() - started) * 1000
    trace = _trace(
        traversal, question_hash, decision, elapsed_ms, reference_date
    )
    result = {
        "schema_version": SCHEMA_VERSION,
        "record_type": "cross_document_semantic_graph_answer",
        "answerer": ANSWERER,
        "answerer_version": ANSWERER_VERSION,
        "question_hash": question_hash,
        "operation": operation,
        "decision": decision,
        "reason_code": reason_code,
        "must_request_concept": must_request_concept,
        "answer_text": answer_text,
        "asserted_facts": asserted_facts,
        "asserted_relations": asserted_relations,
        "trace": trace,
    }
    return result


def _read_questions(path: Path) -> list[str]:
    questions: list[str] = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"invalid question JSONL at line {line_number}"
                ) from exc
            if not isinstance(record, dict) or set(record) != {"question"}:
                raise ValueError(
                    "answerer accepts exactly one field per input record: question"
                )
            question = record["question"]
            if not isinstance(question, str) or not question.strip():
                raise ValueError(f"question line {line_number} is empty")
            questions.append(question)
    if not questions:
        raise ValueError("question file is empty")
    return questions


def _write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise ValueError(f"refusing to overwrite answer output: {path}")
    temporary = path.with_name(f".{path.name}.building")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            for record in records:
                handle.write(canonical_json(record) + "\n")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--graph", required=True, type=Path)
    parser.add_argument("--questions", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--disable-edge-id", action="append", default=[])
    parser.add_argument("--reference-date")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        snapshot = GraphSnapshot.load(args.graph)
        questions = _read_questions(args.questions)
        answers = [
            answer_question(
                snapshot,
                question,
                disabled_edge_ids=args.disable_edge_id,
                reference_date=args.reference_date,
            )
            for question in questions
        ]
        _write_jsonl(args.out, answers)
    except (GraphContractError, OSError, sqlite3.Error, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print(canonical_json({
        "answer_count": len(answers),
        "graph_snapshot_id": snapshot.graph_snapshot_id,
        "output": str(args.out.resolve()),
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
