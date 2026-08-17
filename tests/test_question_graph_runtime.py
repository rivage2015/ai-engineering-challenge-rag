"""Live question-graph runtime and answer-contract regressions.

These tests use only synthetic questions and source snippets.  They prove that
the graph is the input to retrieval and generation even when the strict gate
holds or the question-understanding backend fails; no answer, prediction, or
competition fixture is read.
"""

from __future__ import annotations

import copy
import contextlib
import io
import json
import sys
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
RAG = ROOT / "rag"
SCRIPTS = ROOT / "scripts"
for path in (RAG, SCRIPTS):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import question_graph_runtime as graph_runtime  # noqa: E402
import main as rag_main  # noqa: E402
from answer import (  # noqa: E402
    answer_question_with_graph,
    answer_question_with_graph_result,
    validate_graph_answer,
)
from glossary import Glossary  # noqa: E402
from tests.test_question_understanding_engine import (  # noqa: E402
    alternative_scope_fixture,
    compile_fixture,
    declare_scope_ambiguity,
    generic_list_fixture,
)


@dataclass(frozen=True)
class _Chunk:
    text: str

    def header(self) -> str:
        return "[synthetic/source.csv / row=1]"


class _SequenceAnswerClient:
    backend = "fixture"
    model = "fixture-model"

    def __init__(self, responses: list[object]) -> None:
        self.responses = list(responses)
        self.calls: list[list[dict[str, str]]] = []

    def check(self) -> None:
        return None

    def generate(self, messages: list[dict[str, str]]) -> str:
        self.calls.append(copy.deepcopy(messages))
        if not self.responses:
            raise AssertionError("unexpected extra answer-generation call")
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return str(response)


@dataclass(frozen=True)
class _IndexedChunk:
    path: str
    location: str
    kind: str
    text: str

    def header(self) -> str:
        return f"[{self.path} / {self.location}]"


@dataclass(frozen=True)
class _ProvenanceChunk:
    path: str
    project: str
    text: str
    source: str = "forbidden_source_marker"
    answer: str = "forbidden_answer_marker"
    prediction: str = "forbidden_prediction_marker"
    gold: str = "forbidden_gold_marker"

    def header(self) -> str:
        return f"[{self.path} / synthetic]"


class _EmptyGlossary:
    def __len__(self) -> int:
        return 0

    def aliases_in(self, text: str) -> tuple[str, ...]:
        del text
        return ()


def _prompt_graph(messages: list[dict[str, str]]) -> dict[str, object]:
    user = messages[1]["content"]
    prefix = "【質問理解グラフ】\n"
    suffix = "\n\n【資料】"
    if not user.startswith(prefix) or suffix not in user:
        raise AssertionError("graph prompt markers are absent")
    return json.loads(user[len(prefix) : user.index(suffix)])


class QuestionGraphRuntimeTest(unittest.TestCase):
    def _ready_plan(self, suffix: str = "runtime"):
        question, draft = generic_list_fixture(suffix)
        qur = compile_fixture(question, draft)
        self.assertEqual("ready_for_retrieval", qur["final_status"])
        with patch.object(
            graph_runtime,
            "build_question_understanding",
            return_value=qur,
        ) as understand:
            plan = graph_runtime.build_graph_plan(
                question["question_id"], question["original_question"]
            )
        understand.assert_called_once()
        return question, qur, plan

    def test_graph_is_built_once_and_query_contains_typed_contract(self) -> None:
        question, qur, plan = self._ready_plan("query_contract")
        requested = qur["question_intent_contract"]["requested"]
        scope = requested["scope"]
        output = requested["requested_outputs"][0]

        self.assertEqual("pass", plan.strict_status)
        self.assertFalse(plan.fallback_used)
        self.assertEqual(1, len(plan.retrieval_queries))
        retrieval = plan.retrieval_queries[0]
        self.assertEqual(question["original_question"], retrieval.query_text)
        self.assertIn(scope["container"], retrieval.query_text)
        self.assertIn(scope["location"], retrieval.query_text)
        self.assertIn(scope["filters"][0]["field"], retrieval.query_text)
        self.assertIn(scope["filters"][0]["value"], retrieval.query_text)
        self.assertNotIn(output["return_field"], retrieval.required_terms)
        for internal_token in (
            "operator=eq",
            "operator=filter",
            "operator=project",
            "cardinality=all",
            "container=list",
            "exact_normalized",
        ):
            self.assertNotIn(internal_token, retrieval.query_text)
        self.assertEqual("authoritative_enumeration", retrieval.coverage_requirement)
        self.assertTrue(
            {
                scope["container"],
                scope["location"],
                scope["filters"][0]["field"],
                scope["filters"][0]["value"],
            }.issubset(set(retrieval.required_terms))
        )

    def test_clarification_candidates_remain_graph_aware_for_retrieval_and_generation(
        self,
    ) -> None:
        question, draft, left, right = alternative_scope_fixture(
            "runtime_clarification"
        )
        qur = compile_fixture(
            question,
            declare_scope_ambiguity(draft, (left, right)),
        )
        self.assertEqual("clarification_required", qur["final_status"])
        with patch.object(
            graph_runtime,
            "build_question_understanding",
            return_value=qur,
        ) as understand:
            plan = graph_runtime.build_graph_plan(
                question["question_id"], question["original_question"]
            )
        understand.assert_called_once()

        self.assertEqual("hold", plan.strict_status)
        self.assertTrue(plan.advisory_usable)
        self.assertFalse(plan.fallback_used)
        self.assertEqual(2, len(plan.retrieval_queries))
        self.assertIn(left, plan.retrieval_queries[0].required_terms)
        self.assertIn(right, plan.retrieval_queries[1].required_terms)
        self.assertTrue(
            all(item.query_text == question["original_question"] for item in plan.retrieval_queries)
        )

        client = _SequenceAnswerClient(["opaque_1"])
        answer = answer_question_with_graph(
            client,
            question["original_question"],
            [_Chunk("TaskID: opaque_1")],
            plan,
        )
        self.assertEqual("opaque_1", answer)
        self.assertEqual(1, len(client.calls))
        graph = _prompt_graph(client.calls[0])
        self.assertEqual("hold", graph["strict_status"])
        self.assertEqual(2, len(graph["branches"]))
        graph_json = json.dumps(graph, ensure_ascii=False, sort_keys=True)
        self.assertIn(left, graph_json)
        self.assertIn(right, graph_json)
        self.assertIn("requested_outputs", graph_json)

    def test_failed_understanding_uses_typed_graph_fallback_not_raw_only(self) -> None:
        question = "未知の対象を検査してください。"
        with patch.object(
            graph_runtime,
            "build_question_understanding",
            side_effect=RuntimeError("synthetic compiler failure"),
        ) as understand:
            plan = graph_runtime.build_graph_plan("q_failed_runtime", question)
        understand.assert_called_once()

        self.assertEqual("fail", plan.strict_status)
        self.assertEqual("failed", plan.qur_final_status)
        self.assertTrue(plan.fallback_used)
        self.assertTrue(plan.advisory_usable)
        self.assertEqual(1, len(plan.branch_intents))
        branch = plan.branch_intents[0]
        self.assertEqual(
            graph_runtime.UNKNOWN_BRANCH_STATUS,
            branch["status"],
        )
        intent = branch["intent"]
        self.assertEqual(
            "unknown", intent["operation_graph"]["nodes"][0]["operator"]
        )
        self.assertEqual(
            "unknown", intent["requested_outputs"][0]["return_field"]
        )
        retrieval = plan.retrieval_queries[0]
        self.assertEqual(question, retrieval.query_text)
        self.assertNotIn("precision=unspecified", retrieval.query_text)
        self.assertEqual((), retrieval.required_terms)
        self.assertEqual((), retrieval.optional_terms)

        client = _SequenceAnswerClient(["わかりません"])
        answer = answer_question_with_graph(
            client,
            question,
            [_Chunk("根拠なし")],
            plan,
        )
        self.assertEqual("わかりません", answer)
        prompt_graph = _prompt_graph(client.calls[0])
        self.assertTrue(prompt_graph["fallback_used"])
        self.assertIn("branches", prompt_graph)
        self.assertIn(
            "unknown",
            json.dumps(prompt_graph["branches"], ensure_ascii=False),
        )

    def test_answer_generator_receives_scope_operations_outputs_and_forbidden_contract(
        self,
    ) -> None:
        question, qur, plan = self._ready_plan("generator_contract")
        client = _SequenceAnswerClient(["opaque_2"])
        answer = answer_question_with_graph(
            client,
            question["original_question"],
            [_Chunk("TaskID: opaque_2")],
            plan,
        )
        self.assertEqual("opaque_2", answer)
        self.assertEqual(1, len(client.calls))
        graph = _prompt_graph(client.calls[0])
        rendered = json.dumps(graph, ensure_ascii=False, sort_keys=True)
        requested = qur["question_intent_contract"]["requested"]
        self.assertIn(requested["scope"]["container"], rendered)
        self.assertIn(requested["scope"]["location"], rendered)
        self.assertIn(requested["scope"]["filters"][0]["field"], rendered)
        self.assertIn(requested["scope"]["filters"][0]["value"], rendered)
        self.assertIn('"operator": "filter"', rendered)
        self.assertIn('"operator": "project"', rendered)
        self.assertIn('"return_field": "identifier"', rendered)
        self.assertIn('"cardinality": {"mode": "all"}', rendered)
        self.assertIn('"forbidden"', rendered)

    def test_shape_mismatch_gets_one_repair_then_safe_fallback(self) -> None:
        graph_plan = {
            "question_id": "q_count_shape",
            "strict_status": "hold",
            "compact_contract": {
                "requested_outputs": [
                    {
                        "output_id": "task_count",
                        "source_operation_ref": "op_count",
                        "return_field": "count",
                        "cardinality": {"mode": "single", "expected_count": 1},
                        "answer_shape": {
                            "container": "scalar",
                            "value_type": "integer",
                            "unit": None,
                            "precision": "exact",
                        },
                        "display_precision": None,
                    }
                ]
            },
        }
        client = _SequenceAnswerClient(["T01、T02", "T01、T02"])
        answer = answer_question_with_graph(
            client,
            "該当するタスクIDはいくつありますか。",
            [_Chunk("TaskID: T01\nTaskID: T02")],
            graph_plan,
        )
        self.assertEqual("わかりません", answer)
        self.assertEqual(2, len(client.calls))
        repair = client.calls[1][-1]["content"]
        self.assertIn("scalar_must_not_be_a_list", repair)
        self.assertIn("integer_scalar_required", repair)

    def test_shape_repair_succeeds_once_and_never_calls_a_third_time(self) -> None:
        graph_plan = {
            "question_id": "q_count_repair",
            "strict_status": "pass",
            "compact_contract": {
                "requested_outputs": [
                    {
                        "return_field": "count",
                        "cardinality": {"mode": "single", "expected_count": 1},
                        "answer_shape": {
                            "container": "scalar",
                            "value_type": "integer",
                            "unit": None,
                            "precision": "exact",
                        },
                    }
                ]
            },
        }
        client = _SequenceAnswerClient(["T01、T02", "2", "unexpected-third"])
        answer = answer_question_with_graph(
            client,
            "該当するタスクはいくつありますか。",
            [_Chunk("該当数: 2")],
            graph_plan,
        )
        self.assertEqual("2", answer)
        self.assertEqual(2, len(client.calls))

    def test_plan_identity_changes_when_graph_semantics_change(self) -> None:
        question, draft = generic_list_fixture("runtime_signature")
        first_qur = compile_fixture(question, draft)
        changed_qur = copy.deepcopy(first_qur)
        changed_qur["question_intent_contract"]["requested"]["scope"]["filters"][0][
            "value"
        ] += "-changed"

        # A tampered QUR is intentionally invalid and must be replaced by the
        # typed failure graph, so it cannot reuse the first graph identity.
        with patch.object(
            graph_runtime,
            "build_question_understanding",
            side_effect=[first_qur, changed_qur],
        ) as understand:
            first = graph_runtime.build_graph_plan(
                question["question_id"], question["original_question"]
            )
            changed = graph_runtime.build_graph_plan(
                question["question_id"], question["original_question"]
            )
        self.assertEqual(2, understand.call_count)
        self.assertNotEqual(first.qur_sha256, changed.qur_sha256)
        self.assertNotEqual(first.fallback_used, changed.fallback_used)
        self.assertEqual(graph_runtime.GRAPH_PLAN_VERSION, first.as_dict()["graph_plan_version"])

    def test_main_default_builds_every_graph_once_before_graph_only_retrieval(
        self,
    ) -> None:
        ready_question, ready_draft = generic_list_fixture("main_ready")
        ready_qur = compile_fixture(ready_question, ready_draft)
        ambiguous_question, ambiguous_draft, left, right = alternative_scope_fixture(
            "main_hold"
        )
        ambiguous_qur = compile_fixture(
            ambiguous_question,
            declare_scope_ambiguity(ambiguous_draft, (left, right)),
        )
        self.assertEqual("clarification_required", ambiguous_qur["final_status"])

        def make_plan(question: dict[str, object], qur: dict[str, object]):
            with patch.object(
                graph_runtime,
                "build_question_understanding",
                return_value=qur,
            ):
                return graph_runtime.build_graph_plan(
                    question["question_id"], question["original_question"]
                )

        plans = {
            ready_question["question_id"]: make_plan(ready_question, ready_qur),
            ambiguous_question["question_id"]: make_plan(
                ambiguous_question, ambiguous_qur
            ),
        }
        questions = [
            (ready_question["question_id"], ready_question["original_question"]),
            (
                ambiguous_question["question_id"],
                ambiguous_question["original_question"],
            ),
        ]
        question_ids_by_text = {question: index for index, question in questions}
        events: list[str] = []

        def graph_builder(index: str, question: str, **kwargs: object):
            self.assertIn("cache_dir", kwargs)
            self.assertIs(kwargs.get("fast_advisory"), True)
            events.append(f"graph:{index}")
            return plans[index]

        class RecordingIndex:
            def __init__(self, chunks: list[object]) -> None:
                del chunks

            def search(self, query: str, extra_terms=(), top_k: int = 12):
                del top_k
                first_line = query.splitlines()[0]
                index = question_ids_by_text[first_line]
                mode = "boosted" if extra_terms else "raw"
                events.append(f"search:{index}:{mode}")
                return [
                    _IndexedChunk(
                        path=f"synthetic/{index}.csv",
                        location="row=1",
                        kind="table_row",
                        text=f"value for {index}",
                    )
                ]

        with tempfile.TemporaryDirectory() as raw_directory:
            directory = Path(raw_directory)
            docs = directory / "docs"
            docs.mkdir()
            with (
                patch.object(rag_main, "DOCS", docs),
                patch.object(rag_main, "OUT", directory / "out"),
                patch.object(
                    rag_main,
                    "QUESTION_GRAPH_CACHE",
                    directory / "out" / "question-graph-cache",
                ),
                patch.object(rag_main, "build_glossary", return_value=_EmptyGlossary()),
                patch.object(rag_main, "build_or_load_chunks", return_value=[object()]),
                patch.object(rag_main, "Index", RecordingIndex),
                patch.object(rag_main, "load_questions", return_value=questions),
                patch.object(
                    graph_runtime,
                    "build_graph_plan",
                    side_effect=graph_builder,
                ) as build,
                patch.object(
                    sys,
                    "argv",
                    ["main.py", "--dry-run", "--limit", "2"],
                ),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                exit_code = rag_main.main()

        self.assertEqual(0, exit_code)
        self.assertEqual(2, build.call_count)
        self.assertEqual(
            [
                f"graph:{ready_question['question_id']}",
                f"graph:{ambiguous_question['question_id']}",
            ],
            events[:2],
        )
        searches = [event for event in events if event.startswith("search:")]
        self.assertEqual(
            2,
            sum(
                event.startswith(f"search:{ready_question['question_id']}:")
                for event in searches
            ),
        )
        self.assertEqual(
            4,
            sum(
                event.startswith(f"search:{ambiguous_question['question_id']}:")
                for event in searches
            ),
        )
        self.assertIn(f"search:{ready_question['question_id']}:raw", searches)
        self.assertIn(f"search:{ready_question['question_id']}:boosted", searches)
        for index, _ in questions:
            graph_position = events.index(f"graph:{index}")
            self.assertTrue(
                all(
                    graph_position < position
                    for position, event in enumerate(events)
                    if event.startswith(f"search:{index}:")
                )
            )

    def test_checkpoint_signature_includes_graph_version_and_qur_identity(self) -> None:
        questions = [("q_signature", "synthetic question")]
        args = SimpleNamespace(
            valid=False,
            backend="ollama",
            model="fixture",
            top_k=12,
            structured_candidate=True,
            legacy_answer_path=False,
            graph_plan_version="0.1",
            retrieval_mode="baseline",
        )
        first_plans = {"q_signature": SimpleNamespace(qur_sha256="a" * 64)}
        first = rag_main._checkpoint_signature(args, questions, first_plans)

        changed_plans = {"q_signature": SimpleNamespace(qur_sha256="b" * 64)}
        changed_qur = rag_main._checkpoint_signature(args, questions, changed_plans)
        self.assertNotEqual(first, changed_qur)

        args.graph_plan_version = "0.2"
        changed_version = rag_main._checkpoint_signature(args, questions, first_plans)
        self.assertNotEqual(first, changed_version)

        args.graph_plan_version = "0.1"
        args.legacy_answer_path = True
        legacy = rag_main._checkpoint_signature(args, questions, None)
        self.assertNotEqual(first, legacy)

    def test_extended_graph_is_compiled_before_the_slow_intent_model(self) -> None:
        question = (
            "銀河物流のplan.xlsxにおいて、"
            "バッファとして使用した工数の合計は何時間ですか。"
        )
        with patch.object(
            graph_runtime,
            "build_question_understanding",
            side_effect=AssertionError("the deterministic graph must run first"),
        ) as understand:
            plan = graph_runtime.build_graph_plan("q_extended", question)

        understand.assert_not_called()
        self.assertEqual("pass", plan.strict_status)
        self.assertEqual(("extended_graph_certified",), plan.strict_reasons)
        self.assertEqual("exhaustive", plan.retrieval_queries[0].coverage_requirement)
        operations = plan.compact_contract["branches"][0]["operations"]
        self.assertEqual("sum", operations[-1]["operator"])
        output = plan.compact_contract["common_requested_outputs"][0]
        self.assertEqual("scalar", output["answer_shape"]["container"])
        self.assertEqual("時間", output["answer_shape"]["unit"])

    def test_extended_terminal_contract_preserves_dynamic_shape(self) -> None:
        regression = graph_runtime.build_graph_plan(
            "q_regression",
            "銀河物流のmodel.xlsxにて算出された回帰係数を使って"
            "id=7を予測した場合の予測値はいくらになりますか。"
            "小数第3位まで求めてください。",
        )
        regression_output = regression.compact_contract[
            "common_requested_outputs"
        ][0]
        self.assertEqual(
            {"mode": "decimal_places", "digits": 3},
            regression_output["display_precision"],
        )
        self.assertEqual((), validate_graph_answer("12.345", regression))
        self.assertIn(
            "decimal_places_required:3",
            validate_graph_answer("12.3", regression),
        )

        parameters = graph_runtime.build_graph_plan(
            "q_parameters",
            "銀河物流の分析コードにおいて、今回の学習で勾配ブースティング法の"
            "モデルに実際に渡される n_estimators、learning_rate、random_state はそれぞれ"
            "いくつですか。設定ファイルに明示されていない値がある場合も、実行時にコード上で"
            "適用される値を含めて答えてください。",
        )
        valid = "n_estimators: 100、learning_rate: 0.1、random_state: 42"
        self.assertEqual((), validate_graph_answer(valid, parameters))
        self.assertIn(
            "key_value_required:random_state",
            validate_graph_answer(
                "n_estimators: 100、learning_rate: 0.1", parameters
            ),
        )

    def test_interaction_feature_columns_may_contain_spaces(self) -> None:
        plan = graph_runtime.build_graph_plan(
            "q_interaction_columns",
            "銀河物流の分析出力 metrics.json の "
            "feature_selection.selected_columns に含まれている列のうち、"
            "分析コードで生成された数値交互作用特徴量の列名をすべて答えてください。",
        )
        output = plan.compact_contract["common_requested_outputs"][0]
        self.assertEqual("list", output["answer_shape"]["container"])
        self.assertEqual("string", output["answer_shape"]["value_type"])
        self.assertEqual(
            (),
            validate_graph_answer(
                "ALPHA__x__BETA CODE、BETA CODE__x__GAMMA", plan
            ),
        )

    def test_advisory_shape_enforces_only_explicitly_proven_fields(self) -> None:
        output = {
            "return_field": "count",
            "cardinality": {"mode": "single", "expected_count": 1},
            "answer_shape": {
                "container": "scalar",
                "value_type": "integer",
                "unit": "日",
                "precision": "unspecified",
            },
            "display_precision": {"mode": "decimal_places", "digits": 2},
            "inference_basis": {
                "enforceable": {
                    "return_field": False,
                    "cardinality": False,
                    "container": False,
                    "value_type": False,
                    "unit": False,
                    "display_precision": False,
                }
            },
        }
        plan = {"compact_contract": {"common_requested_outputs": [output]}}
        self.assertEqual((), validate_graph_answer("opaque answer", plan))
        output["inference_basis"]["enforceable"] = {
            key: True
            for key in output["inference_basis"]["enforceable"]
        }
        violations = validate_graph_answer("opaque answer", plan)
        self.assertIn("integer_scalar_required", violations)
        self.assertIn("unit_required:日", violations)
        self.assertIn("decimal_places_required:2", violations)

    def test_alias_prompt_is_question_gated_source_scoped_and_ambiguity_safe(
        self,
    ) -> None:
        source_canonical = "架空星雲企画株式会社"
        other_canonical = "架空月面企画株式会社"
        glossary = Glossary()
        glossary.add("NEB", source_canonical, primary=True)
        glossary.add("DUO", source_canonical)
        glossary.add("DUO", other_canonical)
        glossary.add("LUN", other_canonical, primary=True)
        chunks = [
            _ProvenanceChunk(
                path=f"vault/{source_canonical}/facts.txt",
                project="opaque-project",
                text="対象案件の記録。",
            )
        ]
        unknown_plan = {
            "compact_contract": {
                "requested_outputs": [
                    {
                        "return_field": "unknown",
                        "cardinality": {"mode": "unknown", "expected_count": None},
                        "answer_shape": {
                            "container": "unknown",
                            "value_type": "unknown",
                            "unit": None,
                            "precision": "unspecified",
                        },
                    }
                ]
            }
        }

        client = _SequenceAnswerClient([source_canonical])
        answer = answer_question_with_graph(
            client,
            "対象案件を主略称で答えてください。",
            chunks,
            unknown_plan,
            glossary,
        )
        self.assertEqual("NEB", answer)
        prompt = client.calls[0][1]["content"]
        marker = "【明示出力表記候補（取得資料のpath/projectと用語集由来）】\n"
        self.assertIn(marker, prompt)
        section = prompt.split(marker, 1)[1].split("\n\n", 1)[0]
        candidates = json.loads(section.splitlines()[-1])
        self.assertEqual([source_canonical], [item["canonical"] for item in candidates])
        by_alias = {
            item["alias"]: item
            for item in candidates[0]["alias_candidates"]
        }
        self.assertEqual("primary", by_alias["NEB"]["role"])
        self.assertTrue(by_alias["DUO"]["ambiguous"])
        self.assertEqual(
            {source_canonical, other_canonical},
            set(by_alias["DUO"]["canonical_candidates"]),
        )
        for forbidden in (
            "forbidden_source_marker",
            "forbidden_answer_marker",
            "forbidden_prediction_marker",
            "forbidden_gold_marker",
        ):
            self.assertNotIn(forbidden, prompt)
        self.assertIn(
            "明示した場合は、その指定を通常表現より優先",
            client.calls[0][0]["content"],
        )

        generic_client = _SequenceAnswerClient([source_canonical])
        generic_answer = answer_question_with_graph(
            generic_client,
            "対象案件を略称で答えてください。",
            chunks,
            unknown_plan,
            glossary,
        )
        self.assertEqual(source_canonical, generic_answer)
        self.assertIn(marker, generic_client.calls[0][1]["content"])

        formal_client = _SequenceAnswerClient([source_canonical])
        formal_answer = answer_question_with_graph(
            formal_client,
            "対象案件を正式名称で答えてください。",
            chunks,
            unknown_plan,
            glossary,
        )
        self.assertEqual(source_canonical, formal_answer)
        self.assertNotIn(marker, formal_client.calls[0][1]["content"])

    def test_primary_alias_postprocessing_refuses_every_ambiguous_case(self) -> None:
        canonical = "架空深海計画株式会社"
        other = "架空深空計画株式会社"
        source_chunk = _ProvenanceChunk(
            path=f"vault/{canonical}/facts.txt",
            project="opaque-project",
            text="opaque",
        )
        question = "対象案件を主略称で答えてください。"

        ambiguous_alias = Glossary()
        ambiguous_alias.add("DUP", canonical, primary=True)
        ambiguous_alias.add("DUP", other, primary=True)
        self.assertEqual(
            canonical,
            ambiguous_alias.render_primary_aliases(
                question, canonical, [source_chunk]
            ),
        )

        multiple_primary = Glossary()
        multiple_primary.add("SEA", canonical, primary=True)
        multiple_primary.add("DEEP", canonical, primary=True)
        self.assertEqual(
            canonical,
            multiple_primary.render_primary_aliases(
                question, canonical, [source_chunk]
            ),
        )

        not_source_scoped = Glossary()
        not_source_scoped.add("SEA", canonical, primary=True)
        unrelated_chunk = _ProvenanceChunk(
            path="vault/unrelated/facts.txt",
            project="opaque-project",
            text=canonical,
        )
        self.assertEqual(
            canonical,
            not_source_scoped.render_primary_aliases(
                question, canonical, [unrelated_chunk]
            ),
        )

    def test_primary_alias_postprocessing_is_shape_checked_after_replacement(
        self,
    ) -> None:
        canonical = "架空境界企画株式会社"
        glossary = Glossary()
        glossary.add("BAD ALIAS EXTRA", canonical, primary=True)
        plan = {
            "compact_contract": {
                "requested_outputs": [
                    {
                        "return_field": "identifier",
                        "cardinality": {"mode": "single", "expected_count": 1},
                        "answer_shape": {
                            "container": "scalar",
                            "value_type": "identifier",
                            "unit": None,
                            "precision": "exact",
                        },
                    }
                ]
            }
        }
        client = _SequenceAnswerClient([canonical, canonical])
        result = answer_question_with_graph_result(
            client,
            "対象案件を主略称で答えてください。",
            [
                _ProvenanceChunk(
                    path=f"vault/{canonical}/facts.txt",
                    project="opaque-project",
                    text="opaque",
                )
            ],
            plan,
            glossary,
        )
        self.assertEqual(2, result.attempts)
        self.assertEqual("fail", result.validation_status)
        self.assertEqual("わかりません", result.answer)
        self.assertIn("identifier_scalar_required", result.violations)
        self.assertEqual("BAD ALIAS EXTRA", client.calls[1][-2]["content"])

    def test_question_parallel_facets_are_explicit_and_multiline_safe(self) -> None:
        plan = {
            "compact_contract": {
                "requested_outputs": [
                    {
                        "return_field": "unknown",
                        "cardinality": {"mode": "unknown", "expected_count": None},
                        "answer_shape": {
                            "container": "unknown",
                            "value_type": "unknown",
                            "unit": None,
                            "precision": "unspecified",
                        },
                    }
                ]
            }
        }
        questions = (
            "変更前と変更後を答えてください。",
            "抽出条件と集計内容を答えてください。",
            "dtypeとユニーク数を答えてください。",
            "AとBを答えてください。",
        )
        for question in questions:
            with self.subTest(question=question):
                client = _SequenceAnswerClient(["opaque-left\nopaque-right"])
                answer = answer_question_with_graph(
                    client,
                    question,
                    [_Chunk("opaque source")],
                    plan,
                )
                self.assertEqual("opaque-left\nopaque-right", answer)
                self.assertEqual(1, len(client.calls))
                prompt = client.calls[0][1]["content"]
                self.assertIn("【質問由来の省略禁止facet】", prompt)
                self.assertIn(question.rstrip("。"), prompt)
                self.assertIn(
                    "要求された要素を省略することではありません",
                    client.calls[0][0]["content"],
                )

    def test_unknown_and_composite_contracts_do_not_force_single_line(self) -> None:
        unknown_plan = {
            "compact_contract": {
                "requested_outputs": [
                    {
                        "return_field": "unknown",
                        "cardinality": {"mode": "unknown", "expected_count": None},
                        "answer_shape": {
                            "container": "unknown",
                            "value_type": "unknown",
                            "unit": None,
                            "precision": "unspecified",
                        },
                    }
                ]
            }
        }
        multiline = "opaque-left\nopaque-right"
        self.assertEqual((), validate_graph_answer(multiline, unknown_plan))

        composite_plan = {
            "compact_contract": {
                "requested_outputs": [
                    {
                        "return_field": "identifier",
                        "answer_shape": {
                            "container": "scalar",
                            "value_type": "identifier",
                            "unit": None,
                            "precision": "exact",
                        },
                    },
                    {
                        "return_field": "identifier",
                        "answer_shape": {
                            "container": "scalar",
                            "value_type": "identifier",
                            "unit": None,
                            "precision": "exact",
                        },
                    },
                ]
            }
        }
        self.assertEqual((), validate_graph_answer(multiline, composite_plan))

        key_value_plan = {
            "compact_contract": {
                "requested_outputs": [
                    {
                        "return_field": "value",
                        "required_keys": ["left", "right"],
                        "answer_shape": {
                            "container": "key_value",
                            "value_type": "number",
                            "unit": None,
                            "precision": "exact",
                        },
                    }
                ]
            }
        }
        self.assertEqual(
            (), validate_graph_answer("left: 1\nright: 2", key_value_plan)
        )

        scalar_plan = {
            "compact_contract": {
                "requested_outputs": [
                    {
                        "return_field": "identifier",
                        "answer_shape": {
                            "container": "scalar",
                            "value_type": "identifier",
                            "unit": None,
                            "precision": "exact",
                        },
                    }
                ]
            }
        }
        self.assertIn(
            "single_line_required",
            validate_graph_answer(multiline, scalar_plan),
        )

    def test_identifier_shape_accepts_one_internal_space_but_rejects_prose(
        self,
    ) -> None:
        list_plan = {
            "compact_contract": {
                "requested_outputs": [
                    {
                        "return_field": "identifier",
                        "cardinality": {"mode": "all", "expected_count": None},
                        "answer_shape": {
                            "container": "list",
                            "value_type": "identifier",
                            "unit": None,
                            "precision": "exact",
                        },
                    }
                ]
            }
        }
        for answer in (
            "ZIP CODE",
            "ZIP CODE、AI-05",
            "ALPHA__x__ZIP CODE、BETA__x__GAMMA",
            "ZIP CODE;AI-05",
            "ＺＩＰ　ＣＯＤＥ、ＡＩ－０５",
        ):
            with self.subTest(answer=answer):
                self.assertEqual((), validate_graph_answer(answer, list_plan))

        for answer in (
            "ZIP  CODE",
            "ZIP CODE is explanatory prose",
            "---、AI-05",
            "AI-05。",
        ):
            with self.subTest(answer=answer):
                self.assertIn(
                    "identifier_list_items_required",
                    validate_graph_answer(answer, list_plan),
                )

        scalar_plan = copy.deepcopy(list_plan)
        scalar_output = scalar_plan["compact_contract"]["requested_outputs"][0]
        scalar_output["cardinality"] = {"mode": "single", "expected_count": 1}
        scalar_output["answer_shape"]["container"] = "scalar"
        self.assertEqual((), validate_graph_answer("ZIP CODE", scalar_plan))
        self.assertIn(
            "identifier_scalar_required",
            validate_graph_answer("ZIP CODE is prose", scalar_plan),
        )

    def test_identifier_list_separator_metamorphs_preserve_digit_suffix_ids(
        self,
    ) -> None:
        identifier_plan = {
            "compact_contract": {
                "requested_outputs": [
                    {
                        "return_field": "identifier",
                        "cardinality": {"mode": "all", "expected_count": 3},
                        "answer_shape": {
                            "container": "list",
                            "value_type": "identifier",
                            "unit": None,
                            "precision": "exact",
                        },
                    }
                ]
            }
        }
        for answer in (
            "AI-05、AI-09、AI-08",
            "AI-05;AI-09;AI-08",
            "AI-05, AI-09, AI-08",
            "AI-05,AI-09,AI-08",
        ):
            with self.subTest(answer=answer):
                self.assertEqual((), validate_graph_answer(answer, identifier_plan))

        numeric_plan = copy.deepcopy(identifier_plan)
        numeric_output = numeric_plan["compact_contract"]["requested_outputs"][0]
        numeric_output["cardinality"] = {"mode": "single", "expected_count": 1}
        numeric_output["answer_shape"]["value_type"] = "number"
        self.assertEqual((), validate_graph_answer("1,234", numeric_plan))
        self.assertIn(
            "identifier_list_items_required",
            validate_graph_answer("AI-05, 1,234", identifier_plan),
        )


if __name__ == "__main__":
    unittest.main()
