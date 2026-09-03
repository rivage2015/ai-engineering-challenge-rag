from __future__ import annotations

import copy
import importlib.util
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / "distribution" / "macos-local-memory" / "engine"


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


answer = load_module(
    "answer_local_memory_v2_temporal_plan_test",
    ENGINE / "answer_local_memory_v2.py",
)


class TemporalQuestionPlanTests(unittest.TestCase):
    QUERY = "5年前は誰が受付業務を担当していましたか？"
    REFERENCE_DATE = "2026-09-03"

    def test_fast_plan_compiles_typed_temporal_assignment_contract(self) -> None:
        plan = answer.try_fast_plan(self.QUERY)

        self.assertIsNotNone(plan)
        compiled = answer.sanitize_plan(plan, self.QUERY, self.REFERENCE_DATE)

        self.assertEqual(compiled["items"][0]["label"], "担当者")
        self.assertEqual(compiled["operation"], "record_lookup")
        self.assertEqual(compiled["target"], "受付業務")
        self.assertEqual(compiled["relation"], "responsible_for")
        self.assertEqual(
            compiled["items"][0]["required_claim"],
            "2021-09-03時点の受付業務の担当者",
        )
        self.assertIn("受付業務", compiled["items"][0]["retrieval_query"])
        self.assertIn("2021-09-03", compiled["items"][0]["retrieval_query"])
        self.assertEqual(
            compiled["temporal_scope"],
            {
                "expression": "5年前",
                "reference_date": "2026-09-03",
                "as_of": "2021-09-03",
                "precision": "day",
                "boundary": "inclusive",
                "resolution_rule": "calendar_year_offset_clamp",
                "timezone": "Asia/Tokyo",
            },
        )

    def test_kanji_years_and_leap_day_use_the_explicit_clamp_rule(self) -> None:
        query = "五年前は誰が受付業務を担当していましたか？"
        compiled = answer.sanitize_plan(
            answer.try_fast_plan(query), query, "2024-02-29",
        )

        self.assertEqual(compiled["temporal_scope"]["expression"], "五年前")
        self.assertEqual(compiled["temporal_scope"]["as_of"], "2019-02-28")

    def test_deictic_target_is_not_promoted_to_a_resolved_target(self) -> None:
        query = "5年前は誰がこの業務を担当していましたか？"
        compiled = answer.sanitize_plan(
            answer.try_fast_plan(query), query, self.REFERENCE_DATE,
        )

        self.assertNotIn("target", compiled)
        self.assertEqual(compiled["temporal_scope"]["as_of"], "2021-09-03")

    def test_common_japanese_assignment_word_orders_resolve_the_exact_target(self) -> None:
        queries = (
            "今から5年前は誰が受付業務を担当していましたか？",
            "受付業務は5年前、誰が担当していましたか？",
            "受付業務について、5年前は誰が担当していましたか？",
            "受付業務では5年前、誰が担当していましたか？",
            "受付業務の場合、5年前は誰が担当していましたか？",
            "受付業務において5年前、誰が担当していましたか？",
            "5年前に受付業務を担当していたのは誰ですか？",
            "受付業務を5年前に担当していたのは誰ですか？",
            "5年前の時点で受付業務の担当者は誰ですか？",
            "5年前の「受付業務」の担当者は誰ですか？",
            "今から5年前の受付業務の担当者は誰ですか？",
            "今からちょうど5年前は誰が受付業務を担当していましたか？",
            "5年前の受付業務は誰の担当でしたか？",
            "5年前、受付業務の担当は誰でしたか？",
            "5年前における受付業務の担当者は誰ですか？",
            "5年前には誰が受付業務を担当していましたか？",
            "5年前の時点では誰が受付業務を担当していましたか？",
            "5年前、受付業務を担当していたのは誰ですか？",
            "5年前時点の受付業務の担当者は誰ですか？",
            "受付業務の5年前の担当者は誰ですか？",
            "ちょうど5年前は誰が受付業務を担当していましたか？",
            "まさに5年前は誰が受付業務を担当していましたか？",
        )
        for query in queries:
            with self.subTest(query=query):
                compiled = answer.sanitize_plan(
                    answer.try_fast_plan(query), query, self.REFERENCE_DATE,
                )
                self.assertEqual(compiled["target"], "受付業務")

    def test_planner_cannot_shorten_an_explicit_assignment_target(self) -> None:
        query = "5年前は誰がProject Atlasを担当していましたか？"
        plan = answer.try_fast_plan(query)
        plan["target"] = "Project"

        with self.assertRaisesRegex(ValueError, "plan_target_not_grounded"):
            answer.sanitize_plan(plan, query, self.REFERENCE_DATE)

    def test_target_name_characters_are_not_stripped_as_particles(self) -> None:
        targets = (
            "はな業務", "のぞみ業務", "しなの",
            "月末処理業務", "年度更新業務", "春風業務",
            "2026年度予算編成業務",
        )
        for target in targets:
            query = f"5年前は誰が{target}を担当していましたか？"
            with self.subTest(target=target):
                compiled = answer.sanitize_plan(
                    answer.try_fast_plan(query), query, self.REFERENCE_DATE,
                )
                self.assertEqual(compiled["target"], target)
                target_first_query = (
                    f"5年前は{target}を誰が担当していましたか？"
                )
                target_first = answer.sanitize_plan(
                    answer.try_fast_plan(target_first_query),
                    target_first_query,
                    self.REFERENCE_DATE,
                )
                self.assertEqual(target_first["target"], target)

    def test_approximate_range_and_mixed_anchor_questions_fail_closed(self) -> None:
        queries = (
            "2020年の5年前は誰が受付業務を担当していましたか？",
            "5年前から現在まで誰が受付業務を担当していましたか？",
            "約5年前は誰が受付業務を担当していましたか？",
            "ほぼ5年前は誰が受付業務を担当していましたか？",
            "5年前後は誰が受付業務を担当していましたか？",
            "5年前あたりは誰が受付業務を担当していましたか？",
            "5年ほど前は誰が受付業務を担当していましたか？",
            "5年くらい前は誰が受付業務を担当していましたか？",
            "5年半前は誰が受付業務を担当していましたか？",
            "5.5年前は誰が受付業務を担当していましたか？",
            "-5年前は誰が受付業務を担当していましたか？",
            "過去5年間で誰が受付業務を担当していましたか？",
            "昨年は誰が受付業務を担当していましたか？",
            "昨年度は誰が受付業務を担当していましたか？",
            "5か月前は誰が受付業務を担当していましたか？",
            "2021-01-01時点では誰が受付業務を担当していましたか？",
            "2021/01/01時点では誰が受付業務を担当していましたか？",
            "当時は誰が受付業務を担当していましたか？",
            "現在は誰が受付業務を担当していますか？",
            "5年前と現在は誰が受付業務を担当していますか？",
            "5年前の1月1日時点では誰が受付業務を担当していましたか？",
            "5年前の1月1日頃は誰が受付業務を担当していましたか？",
            "5年前の春頃は誰が受付業務を担当していましたか？",
            "5年前の1月から誰が受付業務を担当していましたか？",
            "5年前の1月まで誰が受付業務を担当していましたか？",
            "5年前の1月中は誰が受付業務を担当していましたか？",
            "5年前の1月1日付では誰が受付業務を担当していましたか？",
            "5年前の第1四半期中は誰が受付業務を担当していましたか？",
            "5年前の年末頃は誰が受付業務を担当していましたか？",
            "五年前の一月一日は誰が受付業務を担当していましたか？",
            "5年前の十二月三十一日は誰が受付業務を担当していましたか？",
            "5年前の第一四半期は誰が受付業務を担当していましたか？",
            "5年前の1月末は誰が受付業務を担当していましたか？",
            "5年前の1月上旬は誰が受付業務を担当していましたか？",
            "5年前の年度末は誰が受付業務を担当していましたか？",
            "5年前の上半期は誰が受付業務を担当していましたか？",
            "5年前の元日は誰が受付業務を担当していましたか？",
            "5年前の9月3日午前は誰が受付業務を担当していましたか？",
            "少なくとも5年前は誰が受付業務を担当していましたか？",
            "5年以上前は誰が受付業務を担当していましたか？",
            "5年前の翌日は誰が受付業務を担当していましたか？",
            "5年前の前月は誰が受付業務を担当していましたか？",
            "5年前の翌日の担当者は誰ですか？",
            "5年前の1月1日の担当者は誰ですか？",
            "5年前のQ1の担当者は誰ですか？",
            "5年前の前月の担当者は誰ですか？",
            "5年前の9/1時点では誰が受付業務を担当していましたか？",
            "5年前の9-1時点では誰が受付業務を担当していましたか？",
            "5年前の9.1時点では誰が受付業務を担当していましたか？",
            "5年前の4Qは誰が受付業務を担当していましたか？",
            "5年前のQ1は誰が受付業務を担当していましたか？",
            "5年前の2021-09は誰が受付業務を担当していましたか？",
            "入社の5年前は誰が受付業務を担当していましたか？",
            "プロジェクト開始の5年前は誰が受付業務を担当していましたか？",
            "事故発生時点から5年前は誰が受付業務を担当していましたか？",
            "入社時点を基準に5年前は誰が受付業務を担当していましたか？",
            "入社日から数えて5年前は誰が受付業務を担当していましたか？",
            "プロジェクト開始を起点に5年前は誰が受付業務を担当していましたか？",
            "5年前は誰が受付業務を担当していませんでしたか？",
            "5年前に受付業務の担当者ではなかったのは誰ですか？",
            "5年前は誰が受付業務を担当していたわけではありませんか？",
            "5年前に受付業務を担当したことがないのは誰ですか？",
            "5年前は誰が受付業務の担当をしていませんでしたか？",
            "5年前に受付業務を担当していたとは限らないのは誰ですか？",
            "昨日、5年前は誰が受付業務を担当していましたか？",
            "基準日は昨日。5年前は誰が受付業務を担当していましたか？",
            "5年前に受付業務を担当し始めたのは誰ですか？",
            "5年前に受付業務の担当者が交代した後の担当者は誰ですか？",
            "5年前の受付業務の担当者以外は誰ですか？",
            "5年前に受付業務を担当していた人以外は誰ですか？",
            "5年前に受付業務を担当していた人を除くと誰ですか？",
            "5年前の受付業務の担当者は何人でしたか？",
            "5年前の受付業務の担当者のメールアドレスは何ですか？",
            "5年前の受付業務の担当者名と役職は？",
            "5年前の受付業務の担当者を全員教えてください。",
            "5年前の受付業務の担当者は誰の上司でしたか？",
            "5年前の受付業務の担当者は誰と一緒に働いていましたか？",
            "5年前の受付業務の担当者を誰が評価しましたか？",
            "5年前の受付業務の担当者に誰が報告していましたか？",
            "5年前の受付業務の担当者を誰が決めましたか？",
            "5年前の受付業務の担当者候補は誰ですか？",
            "数年前は誰が受付業務を担当していましたか？",
            "半年前は誰が受付業務を担当していましたか？",
            "百年前は誰が受付業務を担当していましたか？",
            "二百年前は誰が受付業務を担当していましたか？",
            "5年まえは誰が受付業務を担当していましたか？",
            "五年まえは誰が受付業務を担当していましたか？",
            "5ねん前は誰が受付業務を担当していましたか？",
            "五ねんまえは誰が受付業務を担当していましたか？",
            "昨日は誰が受付業務を担当していましたか？",
            "一昨日は誰が受付業務を担当していましたか？",
            "先日は誰が受付業務を担当していましたか？",
            "今月は誰が受付業務を担当していますか？",
            "前任の受付業務の担当者は誰ですか？",
            "東京支社の昨年の受付業務の担当者は誰ですか？",
            "東京支社の2021年の受付業務の担当者は誰ですか？",
            "東京支社の令和3年の受付業務の担当者は誰ですか？",
            "東京支社の9月の受付業務の担当者は誰ですか？",
            "東京支社の開始時の受付業務の担当者は誰ですか？",
            "東京支社の退任時の受付業務の担当者は誰ですか？",
            "東京支社の引き継ぎ前の受付業務の担当者は誰ですか？",
            "東京支社のQ1の受付業務の担当者は誰ですか？",
            "東京支社の上期の受付業務の担当者は誰ですか？",
            "東京支社の春の受付業務の担当者は誰ですか？",
            "東京支社の冬の受付業務の担当者は誰ですか？",
            "東京支社の4月1日の受付業務の担当者は誰ですか？",
            "東京支社の第1週の受付業務の担当者は誰ですか？",
            "東京支社の第1四半期の受付業務の担当者は誰ですか？",
            "東京支社の2021年度末の受付業務の担当者は誰ですか？",
            "東京支社の期首の受付業務の担当者は誰ですか？",
            "東京支社の期末の受付業務の担当者は誰ですか？",
        )
        for query in queries:
            with self.subTest(query=query), self.assertRaisesRegex(
                ValueError, "plan_temporal_context_unsupported",
            ):
                answer.sanitize_plan(
                    answer.try_fast_plan(query), query, self.REFERENCE_DATE,
                )

    def test_symbolic_or_conjoined_official_target_name_is_preserved(self) -> None:
        targets = (
            "Research and Development", "Sales & Marketing", "R&D", "A/B Test",
            "調査および開発業務", "企画・開発業務", "プラス+プロジェクト",
        )
        for target in targets:
            query = f"5年前は誰が{target}を担当していましたか？"
            with self.subTest(target=target):
                compiled = answer.sanitize_plan(
                    answer.try_fast_plan(query), query, self.REFERENCE_DATE,
                )
                self.assertEqual(compiled["target"], target)

    def test_llm_supplied_date_math_must_match_the_deterministic_scope(self) -> None:
        plan = answer.try_fast_plan(self.QUERY)
        plan["temporal_scope"] = {
            "expression": "5年前",
            "as_of": "2022-09-03",
        }

        with self.assertRaisesRegex(ValueError, "plan_temporal_as_of_mismatch"):
            answer.sanitize_plan(plan, self.QUERY, self.REFERENCE_DATE)

    def test_temporal_plan_requires_one_explicit_run_reference_date(self) -> None:
        with self.assertRaisesRegex(ValueError, "plan_reference_date_required"):
            answer.sanitize_plan(answer.try_fast_plan(self.QUERY), self.QUERY)

    def test_ungrounded_target_and_future_scope_fail_closed(self) -> None:
        target_plan = answer.try_fast_plan(self.QUERY)
        target_plan["target"] = "配送業務"
        with self.assertRaisesRegex(ValueError, "plan_target_not_grounded"):
            answer.sanitize_plan(target_plan, self.QUERY, self.REFERENCE_DATE)

        future_queries = (
            "5年後は誰が受付業務を担当しますか？",
            "5年あとには誰が受付業務を担当しますか？",
            "明日は誰が受付業務を担当しますか？",
            "来週は誰が受付業務を担当しますか？",
            "来月は誰が受付業務を担当しますか？",
            "来年は誰が受付業務を担当しますか？",
            "再来年は誰が受付業務を担当しますか？",
            "将来は誰が受付業務を担当しますか？",
            "今後は誰が受付業務を担当しますか？",
        )
        for future_query in future_queries:
            with self.subTest(query=future_query), self.assertRaisesRegex(
                ValueError, "plan_temporal_scope_future_not_supported",
            ):
                answer.sanitize_plan(
                    answer.try_fast_plan(future_query),
                    future_query,
                    self.REFERENCE_DATE,
                )

    def test_plain_owner_questions_use_a_positive_full_clause_allowlist(self) -> None:
        supported = (
            "誰が受付業務を担当していますか？",
            "受付業務を担当しているのは誰ですか？",
            "受付業務の担当者は誰ですか？",
            "受付業務は誰の担当ですか？",
            "受付業務の責任者はどなたですか？",
            "受付業務の主担当は誰ですか？",
            "受付業務の担当者氏名は誰ですか？",
            "受付業務の担当社員は誰ですか？",
            "受付業務の受け持ちは誰ですか？",
        )
        for query in supported:
            with self.subTest(query=query):
                compiled = answer.sanitize_plan(
                    answer.try_fast_plan(query), query, self.REFERENCE_DATE,
                )
                self.assertNotIn("temporal_scope", compiled)

        for query in (
            "Who is the Owner of Project Atlas?",
            "Project Atlas's Owner is who?",
            "Project AtlasのOwnerは誰ですか？",
        ):
            with self.subTest(query=query):
                compiled = answer.sanitize_plan(
                    answer.make_plan(query, ("Owner",)),
                    query,
                    self.REFERENCE_DATE,
                )
                self.assertNotIn("temporal_scope", compiled)

        unsupported = (
            "誰が受付業務を担当していませんでしたか？",
            "受付業務を担当していなかったのは誰ですか？",
            "受付業務の担当を開始したのは誰ですか？",
            "受付業務の担当者以外は誰ですか？",
            "受付業務の担当者候補は誰ですか？",
        )
        for query in unsupported:
            with self.subTest(query=query), self.assertRaisesRegex(
                ValueError, "plan_assignment_context_unsupported",
            ):
                answer.sanitize_plan(
                    answer.try_fast_plan(query), query, self.REFERENCE_DATE,
                )

    def test_existing_non_temporal_plan_remains_compatible(self) -> None:
        original = answer.make_plan("居住地は？", ("居住地",))
        compiled = answer.sanitize_plan(
            copy.deepcopy(original), "居住地は？", self.REFERENCE_DATE,
        )

        self.assertEqual(compiled["items"], original["items"])
        self.assertNotIn("operation", compiled)
        self.assertNotIn("temporal_scope", compiled)
        answer.validate_plan(compiled)

    def test_non_assignment_time_words_keep_existing_plans_compatible(self) -> None:
        cases = (
            ("昨年の出勤回数は？", answer.make_count_plan("昨年の出勤回数は？")),
            ("今の居住地は？", answer.make_plan("今の居住地は？", ("居住地",))),
            ("2026年の出勤回数は？", answer.make_count_plan("2026年の出勤回数は？")),
            ("担当回数は何回ですか？", answer.make_count_plan("担当回数は何回ですか？")),
            (
                "受付業務を担当した回数は何回ですか？",
                answer.make_count_plan("受付業務を担当した回数は何回ですか？"),
            ),
            (
                "担当業務の件数は何件ですか？",
                answer.make_count_plan("担当業務の件数は何件ですか？"),
            ),
        )
        for query, plan in cases:
            with self.subTest(query=query):
                compiled = answer.sanitize_plan(
                    copy.deepcopy(plan), query, self.REFERENCE_DATE,
                )
                self.assertNotIn("temporal_scope", compiled)


if __name__ == "__main__":
    unittest.main()
