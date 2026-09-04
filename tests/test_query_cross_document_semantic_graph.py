from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Callable


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
BUILDER_PATH = REPOSITORY_ROOT / "scripts" / "build_cross_document_semantic_graph.py"
QUERY_PATH = REPOSITORY_ROOT / "scripts" / "query_cross_document_semantic_graph.py"
DEFAULT_ADAPTER_ROOT = Path(
    "/private/tmp/cross-format-kg-v0.1-baseline/layer1-adapter"
)
ADAPTER_ROOT = Path(
    os.environ.get("CROSS_FORMAT_KG_LAYER1_ADAPTER_DIR", DEFAULT_ADAPTER_ROOT)
)

OWNER_2022_QUESTION = (
    "Project Orionの「移行リハーサル統括」は、2022年8月1日時点で"
    "誰が主担当でしたか。"
)
OWNER_2023_QUESTION = (
    "Project Orionの「移行リハーサル統括」は、2023年5月1日時点で"
    "誰が主担当でしたか。"
)
RELATIVE_OWNER_QUESTION = (
    "Project Orionの「移行リハーサル統括」は、5年前に"
    "誰が主担当でしたか。"
)
ASSIGNMENT_CHANGE_QUESTION = (
    "Project Orionの「移行リハーサル統括」で、主担当が切り替わった日と、"
    "変更前・変更後の担当者を答えてください。"
)
VERSION_CHANGE_QUESTION = (
    "Project Orionの「移行リハーサル統括」について、承認済みの担当変更理由と、"
    "旧案から何が変わったかを答えてください。"
)


def _load_module(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


builder = _load_module("test_target_cross_document_builder", BUILDER_PATH)
query = _load_module("test_target_cross_document_query", QUERY_PATH)


def _metadata_value(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _edge_rows(
    connection: sqlite3.Connection, relation_type: str
) -> list[tuple[str, dict[str, Any]]]:
    return [
        (edge_id, json.loads(properties_json))
        for edge_id, properties_json in connection.execute(
            "SELECT edge_id, properties_json FROM edges "
            "WHERE relation_type = ? ORDER BY edge_id",
            (relation_type,),
        )
    ]


def _rehash_edge(connection: sqlite3.Connection, edge_id: str) -> None:
    row = connection.execute(
        "SELECT edge_id, from_node_id, relation_type, to_node_id, "
        "relation_class, status, basis_kind, basis_rule, properties_json "
        "FROM edges WHERE edge_id = ?",
        (edge_id,),
    ).fetchone()
    if row is None:
        raise AssertionError(f"missing Edge under test: {edge_id}")
    supporting = [
        item[0]
        for item in connection.execute(
            "SELECT evidence_id FROM edge_evidence "
            "WHERE edge_id = ? ORDER BY evidence_id",
            (edge_id,),
        )
    ]
    payload = {
        "edge_id": row[0],
        "from_node_id": row[1],
        "relation_type": row[2],
        "to_node_id": row[3],
        "relation_class": row[4],
        "status": row[5],
        "basis_kind": row[6],
        "basis_rule": row[7],
        "properties": json.loads(row[8]),
        "supporting_evidence_ids": supporting,
    }
    connection.execute(
        "UPDATE edges SET record_sha256 = ? WHERE edge_id = ?",
        (query.sha256_value(payload), edge_id),
    )


def _rehash_node(connection: sqlite3.Connection, node_id: str) -> None:
    row = connection.execute(
        "SELECT node_id, node_type, canonical_key, status, properties_json "
        "FROM nodes WHERE node_id = ?",
        (node_id,),
    ).fetchone()
    if row is None:
        raise AssertionError(f"missing Node under test: {node_id}")
    payload = {
        "node_id": row[0],
        "node_type": row[1],
        "canonical_key": row[2],
        "status": row[3],
        "properties": json.loads(row[4]),
    }
    connection.execute(
        "UPDATE nodes SET record_sha256 = ? WHERE node_id = ?",
        (query.sha256_value(payload), node_id),
    )


def _rehash_snapshot(connection: sqlite3.Connection) -> None:
    logical = {
        "evidence_record_sha256": sorted(
            row[0]
            for row in connection.execute(
                "SELECT record_sha256 FROM source_evidence"
            )
        ),
        "node_record_sha256": sorted(
            row[0] for row in connection.execute("SELECT record_sha256 FROM nodes")
        ),
        "edge_record_sha256": sorted(
            row[0] for row in connection.execute("SELECT record_sha256 FROM edges")
        ),
    }
    logical_sha256 = query.sha256_value(logical)
    connection.execute(
        "UPDATE metadata SET value = ? WHERE key = 'logical_snapshot_sha256'",
        (_metadata_value(logical_sha256),),
    )
    connection.execute(
        "UPDATE metadata SET value = ? WHERE key = 'graph_snapshot_id'",
        (_metadata_value("xkgs_" + logical_sha256[:32]),),
    )


def _replace_edge_properties(
    connection: sqlite3.Connection,
    relation_type: str,
    predicate: Callable[[dict[str, Any]], bool],
    mutation: Callable[[dict[str, Any]], None],
) -> str:
    matches = [
        (edge_id, properties)
        for edge_id, properties in _edge_rows(connection, relation_type)
        if predicate(properties)
    ]
    if len(matches) != 1:
        raise AssertionError(
            f"expected one {relation_type} Edge under test; got {len(matches)}"
        )
    edge_id, properties = matches[0]
    mutation(properties)
    connection.execute(
        "UPDATE edges SET properties_json = ? WHERE edge_id = ?",
        (_metadata_value(properties), edge_id),
    )
    _rehash_edge(connection, edge_id)
    return edge_id


def _facts(answer: dict[str, Any]) -> dict[str, str]:
    return {item["field"]: item["value"] for item in answer["asserted_facts"]}


class CrossDocumentSemanticGraphQueryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.documents = ADAPTER_ROOT / "semantic-documents.jsonl"
        cls.evidence = ADAPTER_ROOT / "safe-answer-evidence.jsonl"
        if not cls.documents.is_file() or not cls.evidence.is_file():
            raise unittest.SkipTest(
                "real safe Layer 1 outputs are unavailable; set "
                "CROSS_FORMAT_KG_LAYER1_ADAPTER_DIR"
            )
        cls._baseline_directory = tempfile.TemporaryDirectory()
        baseline_root = Path(cls._baseline_directory.name)
        cls.baseline_graph = baseline_root / "semantic-graph.sqlite3"
        builder.build(
            cls.documents,
            cls.evidence,
            cls.baseline_graph,
            baseline_root / "semantic-graph-state.json",
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls._baseline_directory.cleanup()

    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.root = Path(self._directory.name)

    def tearDown(self) -> None:
        self._directory.cleanup()

    def _graph_copy(self, name: str) -> Path:
        target = self.root / f"{name}.sqlite3"
        shutil.copy2(self.baseline_graph, target)
        return target

    def _answer(
        self,
        graph: Path,
        question_text: str,
        reference_date: str | None = None,
    ) -> dict[str, Any]:
        snapshot = query.GraphSnapshot.load(graph)
        return query.answer_question(
            snapshot,
            question_text,
            reference_date=reference_date,
        )

    def test_relative_year_uses_one_explicit_run_reference_date(self) -> None:
        missing_anchor = self._answer(
            self.baseline_graph, RELATIVE_OWNER_QUESTION
        )
        self.assertEqual("HOLD", missing_anchor["decision"])
        self.assertEqual(
            "reference_time_required", missing_anchor["reason_code"]
        )

        answer = self._answer(
            self.baseline_graph,
            RELATIVE_OWNER_QUESTION,
            reference_date="2027-08-01",
        )
        self.assertEqual("ACCEPTED", answer["decision"])
        self.assertEqual("2022-08-01", _facts(answer)["reference_time"])
        self.assertEqual("EMP-104", _facts(answer)["assignee_id"])
        self.assertEqual(
            "2027-08-01", answer["trace"]["question_reference_date"]
        )
        replay = self._answer(
            self.baseline_graph,
            RELATIVE_OWNER_QUESTION,
            reference_date="2027-08-01",
        )
        self.assertEqual(answer["trace"]["run_id"], replay["trace"]["run_id"])

        earlier_anchor = self._answer(
            self.baseline_graph,
            RELATIVE_OWNER_QUESTION,
            reference_date="2025-08-01",
        )
        self.assertEqual("HOLD", earlier_anchor["decision"])
        self.assertNotEqual(
            answer["trace"]["run_id"], earlier_anchor["trace"]["run_id"]
        )

        with self.assertRaisesRegex(ValueError, "strict ISO"):
            self._answer(
                self.baseline_graph,
                RELATIVE_OWNER_QUESTION,
                reference_date="2027-8-1",
            )

    def test_approximate_or_ranged_relative_year_never_becomes_one_day(self) -> None:
        for surface in (
            "約5年前",
            "5年前頃",
            "5〜6年前",
            "4、5年前",
            "5年前から",
            "少なくとも5年前",
            "5年前後",
            "5年〜6年前",
            "5年から6年前",
            "4年、5年前",
            "5年ないし6年前",
            "5年または6年前",
            "4・5年前",
            "5年前ほど",
            "大体5年前",
            "数年〜5年前",
            "去年または5年前",
            "5年前の4月",
            "5年前時点の4月",
            "5年前の4/1",
            "5年前のQ1",
            "5年前の春",
            "5年前の年末",
            "プロジェクト開始から5年前",
            "入社から数えて5年前",
            "移行開始日の5年前",
            "契約終了日の5年前",
            "基準日から5年前",
            "5年前よりも前",
            "5年前より少し前",
            "5年前の直前",
            "5年前の直後",
            "5年前付近",
            "5年前を中心に",
            "5年前かそれ以前",
            "5年前ではない",
            "5年前以外",
            "5年前を除く",
            "多分5年前",
            "5年前かもしれない",
            "5年前の初め",
            "5年前の初頭",
            "5年前の前半",
            "5年前の終わり",
            "5年前の年央",
            "5年前のゴールデンウィーク",
            "5年か6年前",
            "5年あるいは6年前",
            "5年もしくは6年前",
            "おおむね5年前",
            "ざっと5年前",
            "5年前の前年",
            "5年前の翌年",
            "5年前の前月",
            "5年前の翌月",
            "5年前の前週",
            "5年前の翌週",
            "5年前の次の日",
            "5年前の前の日",
            "5年前よりちょっと前",
            "5年前よりやや前",
            "5年前より若干前",
            "5年前の頃",
            "5年前の時期",
            "5年前のどこか",
            "5年前近辺",
            "5年前近く",
            "多分今から5年前",
            "ほぼちょうど5年前",
            "ちょうど5年前だったかもしれない",
            "4年と5年前",
            "4年、または5年前",
            "たしか5年前",
            "5年前かどうか",
            "5年前だったと思う",
            "5年前だったはず",
            "5年前かその前",
            "5年前もしくはもっと前",
            "5年前あるいはそれ以前",
            "5年前またはそれより前",
            "5年前の時点より前",
            "5年前時点以前",
            "5年前の翌々日",
            "5年前の前々日",
            "5年前の期首",
            "5年前の期末",
            "5年前の誕生日",
            "誕生日の5年前",
            "事故の5年前",
            "リリースの5年前",
            "推定では、5年前",
            "事故のときからは5年前",
            "締結時点では、5年前",
            "仮に、5年前",
            "想定では、5年前",
            "可能性としては、5年前",
            "ひょっとしたら、5年前",
            "もしかしたら、5年前",
            "暫定では、5年前",
            "目安として、5年前",
            "5年前周辺",
            "5年前近傍",
            "5年前を境に",
            "5年前だったかも",
            "5年前らしい",
        ):
            with self.subTest(surface=surface):
                answer = self._answer(
                    self.baseline_graph,
                    RELATIVE_OWNER_QUESTION.replace("5年前", surface),
                    reference_date="2027-08-01",
                )
                self.assertEqual("HOLD", answer["decision"])
                self.assertEqual(
                    "reference_time_ambiguous", answer["reason_code"]
                )
                self.assertEqual([], answer["asserted_facts"])

        for surface in (
            "ちょうど5年前",
            "今から5年前",
            "現在から数えて5年前",
            "現時点を基準に5年前",
        ):
            with self.subTest(exact_surface=surface):
                answer = self._answer(
                    self.baseline_graph,
                    RELATIVE_OWNER_QUESTION.replace("5年前", surface),
                    reference_date="2027-08-01",
                )
                self.assertEqual("ACCEPTED", answer["decision"])
                self.assertEqual(
                    "2022-08-01", _facts(answer)["reference_time"]
                )

    def test_owner_time_question_requires_one_identity_question(self) -> None:
        cases = (
            (
                "Project Orionの「移行リハーサル統括」は、5年前に"
                "誰がその後も主担当でしたか。",
                "2027-08-01",
            ),
            (
                "Project Orionの「移行リハーサル統括」は、5年前に"
                "誰か担当でしたか。",
                "2027-08-01",
            ),
            (
                "Project Orionの「移行リハーサル統括」は、5年前に"
                "どなたか担当でしたか。",
                "2027-08-01",
            ),
            (
                "Project Orionの「移行リハーサル統括」は、"
                "2022年8月1日に誰が後日も主担当でしたか。",
                None,
            ),
            (
                "Project Orionの「移行リハーサル統括」は、"
                "2022年8月1日に誰か担当でしたか。",
                None,
            ),
            (
                "Project Orionの「移行リハーサル統括」は、"
                "2022年8月1日にどなたか担当でしたか。",
                None,
            ),
        )
        for question_text, reference_date in cases:
            with self.subTest(question=question_text):
                answer = self._answer(
                    self.baseline_graph,
                    question_text,
                    reference_date=reference_date,
                )
                self.assertEqual("HOLD", answer["decision"])
                self.assertEqual(
                    "reference_time_ambiguous", answer["reason_code"]
                )
                self.assertEqual([], answer["asserted_facts"])

    def test_non_owner_operations_do_not_ignore_question_time(self) -> None:
        questions = (
            (
                "Project Orionの「移行リハーサル統括」で、5年前に"
                "主担当が交代した日と前任・後任を教えてください。"
            ),
            (
                "Project Orionの「移行リハーサル統括」で、5年前の"
                "旧版から何が変わったのか、担当変更の理由も教えてください。"
            ),
            (
                "Project Orionの「移行リハーサル統括」で、昨年に"
                "主担当が交代した日と前任・後任を教えてください。"
            ),
            (
                "Project Orionの「移行リハーサル統括」で、先月に"
                "主担当が交代した日と前任・後任を教えてください。"
            ),
            (
                "Project Orionの「移行リハーサル統括」で、数年前に"
                "主担当が交代した日と前任・後任を教えてください。"
            ),
            (
                "Project Orionの「移行リハーサル統括」で、5日前に"
                "主担当が交代した日と前任・後任を教えてください。"
            ),
            (
                "Project Orionの「移行リハーサル統括」で、2023年度に"
                "主担当が交代した日と前任・後任を教えてください。"
            ),
            (
                "Project Orionの「移行リハーサル統括」で、春に"
                "主担当が交代した日と前任・後任を教えてください。"
            ),
            (
                "Project Orionの「移行リハーサル統括」で、先月の旧版から"
                "何が変わったのか、担当変更の理由も教えてください。"
            ),
            (
                "Project Orionの「移行リハーサル統括」で、昨年の旧版から"
                "何が変わったのか、担当変更の理由も教えてください。"
            ),
            (
                "Project Orionの「移行リハーサル統括」で、今期に"
                "主担当が交代した日と前任・後任を教えてください。"
            ),
            (
                "Project Orionの「移行リハーサル統括」で、年末に"
                "主担当が交代した日と前任・後任を教えてください。"
            ),
            (
                "Project Orionの「移行リハーサル統括」で、Q1に"
                "主担当が交代した日と前任・後任を教えてください。"
            ),
            (
                "Project Orionの「移行リハーサル統括」で、2023-04に"
                "主担当が交代した日と前任・後任を教えてください。"
            ),
            (
                "Project Orionの「移行リハーサル統括」で、朝に"
                "主担当が交代した日と前任・後任を教えてください。"
            ),
            (
                "Project Orionの「移行リハーサル統括」で、4/1に"
                "主担当が交代した日と前任・後任を教えてください。"
            ),
            (
                "Project Orionの「移行リハーサル統括」で、月末に"
                "主担当が交代した日と前任・後任を教えてください。"
            ),
            (
                "Project Orionの「移行リハーサル統括」で、今四半期に"
                "主担当が交代した日と前任・後任を教えてください。"
            ),
            (
                "Project Orionの「移行リハーサル統括」で、"
                "ゴールデンウィークに主担当が交代した日と"
                "前任・後任を教えてください。"
            ),
            (
                "Project Orionの「移行リハーサル統括」で、未明に"
                "主担当が交代した日と前任・後任を教えてください。"
            ),
            (
                "Project Orionの「移行リハーサル統括」で、連休に"
                "主担当が交代した日と前任・後任を教えてください。"
            ),
            (
                "Project Orionの「移行リハーサル統括」で、月初に"
                "主担当が交代した日と前任・後任を教えてください。"
            ),
            (
                "Project Orionの「移行リハーサル統括」で、年度初に"
                "主担当が交代した日と前任・後任を教えてください。"
            ),
            (
                "Project Orionの「移行リハーサル統括」で、週末に"
                "主担当が交代した日と前任・後任を教えてください。"
            ),
            (
                "Project Orionの「移行リハーサル統括」で、開始当初に"
                "主担当が交代した日と前任・後任を教えてください。"
            ),
            (
                "Project Orionの「移行リハーサル統括」で、何年か前に"
                "主担当が交代した日と前任・後任を教えてください。"
            ),
            (
                "Project Orionの「移行リハーサル統括」で、ずっと前に"
                "主担当が交代した日と前任・後任を教えてください。"
            ),
            (
                "Project Orionの「移行リハーサル統括」で、締結時に"
                "主担当が交代した日と前任・後任を教えてください。"
            ),
        )
        for question_text in questions:
            with self.subTest(question=question_text):
                answer = self._answer(
                    self.baseline_graph,
                    question_text,
                    reference_date="2027-08-01",
                )
                self.assertEqual("HOLD", answer["decision"])
                self.assertEqual(
                    "temporal_context_unsupported", answer["reason_code"]
                )
                self.assertEqual([], answer["asserted_facts"])
                self.assertEqual([], answer["asserted_relations"])

    def test_approximate_or_bounded_absolute_date_is_held(self) -> None:
        for surface in (
            "約2022年8月1日",
            "2022年8月1日頃",
            "2022年8月1日前後",
            "2022年8月1日から",
            "2022年8月1日以降",
            "2022年8月1日まで",
            "2022年8月1日以前",
            "2022年8月1日または2022年8月1日",
            "多分2022年8月1日",
            "2022年8月1日かもしれない",
            "2022年8月1日付近",
            "2022年8月1日辺り",
            "2022年8月1日位",
            "2022年8月1日よりも前",
            "2022年8月1日より少し前",
            "2022年8月1日の直前",
            "2022年8月1日の直後",
            "2022年8月1日を中心に",
            "2022年8月1日ではない",
            "2022年8月1日以外",
            "2022年8月1日を除く",
            "2022年8月1日かそれ以前",
            "2022年8月1日の朝",
            "2022年8月1日〜5日",
            "2022年8月1日の前日",
            "2022年8月1日の翌日",
            "2022年8月1日の3日後",
            "おおむね2022年8月1日",
            "2022年8月1日と翌日",
            "2022年8月1日〜翌日",
            "2022年8月1日ではなく前日",
            "2023年4月1日の前日",
            "2023年3月31日の翌日",
            "2022年8月1日または翌営業日",
            "2022年8月1日か同日",
            "たしか2022年8月1日",
            "2022年8月1日かどうか",
            "2022年8月1日だったと思う",
            "2022年8月1日時点より前",
            "2022年8月1日の時点以降",
            "2022年8月1日の翌々日",
            "2022年8月1日の前々日",
            "2022年8月1日か別の日",
            "2022年8月1日または別日",
            "2022年8月1日周辺",
            "2022年8月1日近傍",
            "2022年8月1日を境に",
            "2022年8月1日12時",
            "2022-08-01T12:00:00+09:00",
            "推定では、2022年8月1日",
            "推定:2022年8月1日",
            "確証はないが、2022年8月1日",
            "記憶違いかもしれませんが、2022年8月1日",
            "締結時点では、2022年8月1日",
            "仮の日付は、2022年8月1日",
            "予想では、2022年8月1日",
            "ひょっとしたら、2022年8月1日",
            "もしかしたら、2022年8月1日",
            "暫定では、2022年8月1日",
            "目安として、2022年8月1日",
        ):
            with self.subTest(surface=surface):
                answer = self._answer(
                    self.baseline_graph,
                    OWNER_2022_QUESTION.replace("2022年8月1日", surface),
                )
                self.assertEqual("HOLD", answer["decision"])
                self.assertEqual(
                    "reference_time_ambiguous", answer["reason_code"]
                )
                self.assertEqual([], answer["asserted_facts"])

    def test_current_claim_semantic_tampering_holds_even_with_valid_hashes(self) -> None:
        """Record/snapshot hash consistency must not imply semantic validity."""

        mutations: dict[str, Callable[[sqlite3.Connection], None]] = {}

        def status_mutation(connection: sqlite3.Connection) -> None:
            for relation_type in ("HAS_CURRENT_CLAIM", "CLAIMS_ASSIGNEE"):
                _replace_edge_properties(
                    connection,
                    relation_type,
                    lambda value: value.get("current") is True,
                    lambda value: value.__setitem__("claim_status", "DRAFT"),
                )

        def current_mutation(connection: sqlite3.Connection) -> None:
            for relation_type in ("HAS_CURRENT_CLAIM", "CLAIMS_ASSIGNEE"):
                _replace_edge_properties(
                    connection,
                    relation_type,
                    lambda value: value.get("current") is True,
                    lambda value: value.__setitem__("current", False),
                )

        def effective_from_mutation(connection: sqlite3.Connection) -> None:
            for relation_type in ("HAS_CURRENT_CLAIM", "CLAIMS_ASSIGNEE"):
                _replace_edge_properties(
                    connection,
                    relation_type,
                    lambda value: value.get("current") is True,
                    lambda value: value.__setitem__("effective_from", "not-a-date"),
                )

        def role_mutation(connection: sqlite3.Connection) -> None:
            _replace_edge_properties(
                connection,
                "CLAIMS_ASSIGNEE",
                lambda value: value.get("current") is True,
                lambda value: value.__setitem__("role", "副担当"),
            )

        mutations["status"] = status_mutation
        mutations["current"] = current_mutation
        mutations["effective_from"] = effective_from_mutation
        mutations["role"] = role_mutation

        for name, mutate in mutations.items():
            with self.subTest(property=name):
                graph = self._graph_copy(f"claim-{name}")
                with sqlite3.connect(graph) as connection:
                    mutate(connection)
                    _rehash_snapshot(connection)
                    connection.commit()

                # Loading proves that every changed record and snapshot hash is valid.
                answer = self._answer(graph, VERSION_CHANGE_QUESTION)
                self.assertEqual("HOLD", answer["decision"])
                self.assertEqual([], answer["asserted_facts"])
                self.assertEqual([], answer["asserted_relations"])

    def test_relation_endpoint_type_tampering_is_rejected_with_valid_hashes(self) -> None:
        graph = self._graph_copy("bad-endpoint-type")
        with sqlite3.connect(graph) as connection:
            row = connection.execute(
                "SELECT target.node_id "
                "FROM edges AS edge "
                "JOIN nodes AS target ON target.node_id = edge.to_node_id "
                "WHERE edge.relation_type = 'ASSIGNED_TO' "
                "ORDER BY edge.edge_id LIMIT 1"
            ).fetchone()
            self.assertIsNotNone(row)
            node_id = row[0]
            connection.execute(
                "UPDATE nodes SET node_type = 'Project' WHERE node_id = ?",
                (node_id,),
            )
            _rehash_node(connection, node_id)
            _rehash_snapshot(connection)
            connection.commit()

        with self.assertRaises(query.GraphContractError):
            query.GraphSnapshot.load(graph)

    def test_fullwidth_date_and_japanese_paraphrases_select_expected_operation(self) -> None:
        cases = [
            (
                "fullwidth-date",
                "Project Orionの「移行リハーサル統括」は、"
                "２０２３年５月１日時点で誰が主担当でしたか。",
                "owner",
                {"reference_time": "2023-05-01", "assignee_id": "EMP-208"},
            ),
            (
                "casefold-project",
                "project orionの「移行リハーサル統括」は、"
                "2023年5月1日時点で誰が主担当でしたか。",
                "owner",
                {"reference_time": "2023-05-01", "assignee_id": "EMP-208"},
            ),
            (
                "handover-former-successor",
                "Project Orionの「移行リハーサル統括」で、"
                "主担当が交代した日と前任・後任を教えてください。",
                "assignment_change",
                {"change_effective_date": "2023-04-01"},
            ),
            (
                "when-changed",
                "Project Orionの「移行リハーサル統括」の"
                "主担当がいつ変わったか、変更前後の担当者を教えてください。",
                "assignment_change",
                {"change_effective_date": "2023-04-01"},
            ),
            (
                "old-version-background",
                "Project Orionの「移行リハーサル統括」で、"
                "旧版から何が変わったのか、担当変更の背景も教えてください。",
                "version_change",
                {"current_plan_status": "APPROVED"},
            ),
        ]

        for name, question_text, operation, expected_facts in cases:
            with self.subTest(paraphrase=name):
                answer = self._answer(self.baseline_graph, question_text)
                self.assertEqual("ACCEPTED", answer["decision"])
                self.assertEqual(operation, answer["operation"])
                facts = _facts(answer)
                for field, expected in expected_facts.items():
                    self.assertEqual(expected, facts.get(field))

    def test_evidence_path_and_locator_tampering_without_hash_update_is_rejected(self) -> None:
        mutations = {
            "path": (
                "UPDATE source_evidence SET relative_path = relative_path || '.tampered' "
                "WHERE evidence_id = (SELECT evidence_id FROM source_evidence "
                "ORDER BY evidence_id LIMIT 1)",
                (),
            ),
            "locator": (
                "UPDATE source_evidence SET locator_json = ? "
                "WHERE evidence_id = (SELECT evidence_id FROM source_evidence "
                "ORDER BY evidence_id LIMIT 1)",
                (_metadata_value({"tampered": True}),),
            ),
        }
        for name, (statement, parameters) in mutations.items():
            with self.subTest(field=name):
                graph = self._graph_copy(f"evidence-{name}")
                with sqlite3.connect(graph) as connection:
                    connection.execute(statement, parameters)
                    connection.commit()
                with self.assertRaisesRegex(
                    query.GraphContractError, "source Evidence record hash mismatch"
                ):
                    query.GraphSnapshot.load(graph)

    def test_missing_first_valid_from_holds_after_self_consistent_rehash(self) -> None:
        graph = self._graph_copy("missing-valid-from")
        with sqlite3.connect(graph) as connection:
            assignments = sorted(
                _edge_rows(connection, "ASSIGNED_TO"),
                key=lambda item: str(item[1].get("valid_from", "")),
            )
            self.assertGreaterEqual(len(assignments), 2)
            edge_id, properties = assignments[0]
            self.assertIn("valid_from", properties)
            del properties["valid_from"]
            connection.execute(
                "UPDATE edges SET properties_json = ? WHERE edge_id = ?",
                (_metadata_value(properties), edge_id),
            )
            _rehash_edge(connection, edge_id)
            _rehash_snapshot(connection)
            connection.commit()

        answer = self._answer(graph, ASSIGNMENT_CHANGE_QUESTION)
        self.assertEqual("HOLD", answer["decision"])
        self.assertEqual("assignment_period_incomplete", answer["reason_code"])
        self.assertEqual([], answer["asserted_facts"])

    def test_exclusive_boundaries_are_normalized_before_temporal_resolution(self) -> None:
        def old_exclusive_end(connection: sqlite3.Connection) -> None:
            assignments = _edge_rows(connection, "ASSIGNED_TO")
            old_id, old_properties = min(
                assignments, key=lambda item: str(item[1].get("valid_from", ""))
            )
            old_properties["valid_to"] = "2023-04-01"
            old_properties["valid_to_inclusive"] = False
            connection.execute(
                "UPDATE edges SET properties_json = ? WHERE edge_id = ?",
                (_metadata_value(old_properties), old_id),
            )
            _rehash_edge(connection, old_id)

        def new_exclusive_start(connection: sqlite3.Connection) -> None:
            assignments = _edge_rows(connection, "ASSIGNED_TO")
            new_id, new_properties = max(
                assignments, key=lambda item: str(item[1].get("valid_from", ""))
            )
            new_properties["valid_from"] = "2023-03-31"
            new_properties["valid_from_inclusive"] = False
            connection.execute(
                "UPDATE edges SET properties_json = ? WHERE edge_id = ?",
                (_metadata_value(new_properties), new_id),
            )
            _rehash_edge(connection, new_id)

        for name, mutate in (
            ("old-exclusive-end", old_exclusive_end),
            ("new-exclusive-start", new_exclusive_start),
        ):
            with self.subTest(boundary=name):
                graph = self._graph_copy(name)
                with sqlite3.connect(graph) as connection:
                    mutate(connection)
                    _rehash_snapshot(connection)
                    connection.commit()

                change = self._answer(graph, ASSIGNMENT_CHANGE_QUESTION)
                self.assertEqual("ACCEPTED", change["decision"])
                self.assertEqual(
                    "2023-04-01", _facts(change).get("change_effective_date")
                )
                self.assertEqual(
                    "2023-03-31", _facts(change).get("previous_valid_to")
                )
                before = self._answer(
                    graph,
                    OWNER_2022_QUESTION.replace("2022年8月1日", "2023年3月31日"),
                )
                after = self._answer(
                    graph,
                    OWNER_2023_QUESTION.replace("2023年5月1日", "2023年4月1日"),
                )
                self.assertEqual("EMP-104", _facts(before).get("assignee_id"))
                self.assertEqual("EMP-208", _facts(after).get("assignee_id"))


if __name__ == "__main__":
    unittest.main()
