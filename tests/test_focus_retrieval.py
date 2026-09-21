from __future__ import annotations

import importlib.util
from pathlib import Path
import unittest


MODULE_PATH = (Path(__file__).resolve().parents[1] / "distribution" /
               "macos-local-memory" / "engine" / "focus_retrieval.py")
SPEC = importlib.util.spec_from_file_location("focus_retrieval_unit", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
focus = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(focus)


def evidence(identifier: str, text: str, sheet: str = "通常手順") -> dict:
    return {"evidence_id": identifier, "text": text, "relative_path": "synthetic.xlsx",
            "document_id": "synthetic_document", "locator": {"sheet_name": sheet},
            "score": 0.5}


class FocusRetrievalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.normal = [evidence("normal-1", "巡回は入口から始める。"),
                       evidence("normal-2", "巡回終了後は道具を戻す。")]

    def test_cross_sheet_action_stem_rescue_retains_normal_order_and_original(self):
        source = evidence("umbrella", "傘は貸す前に破損がないか確認する。", "連絡事項")
        before = source.copy()
        result = focus.supplement("巡回 傘貸出 注意事項 条件", self.normal + [source], self.normal)
        self.assertEqual([row["evidence_id"] for row in result],
                         ["normal-1", "umbrella", "normal-2"])
        self.assertEqual(result[1]["focus_terms"], ["傘"])
        self.assertEqual(result[1]["retrieval_source"], "focus_term_supplement")
        self.assertEqual(source, before)
        self.assertIsNot(result[1], source)
        self.assertIs(result[0], self.normal[0])
        for key in ("text", "locator", "evidence_id", "document_id", "relative_path", "score"):
            self.assertEqual(result[1][key], source[key])

    def test_existing_coverage_returns_exact_input_list(self):
        normal = [evidence("n", "貸出用の傘は事務所にあります。")]
        candidate = evidence("c", "傘は管理表に記録する。")
        self.assertIs(focus.supplement("傘貸出 注意事項", [candidate], normal), normal)

    def test_empty_candidates_or_no_match_leave_original_unchanged(self):
        self.assertIs(focus.supplement("鍵返却", [], self.normal), self.normal)
        self.assertIs(focus.supplement("鍵返却", self.normal, self.normal), self.normal)

    def test_general_request_words_do_not_rescue_irrelevant_text(self):
        candidate = evidence("irrelevant", "注意事項と曜日、時間、案内、資料の所在と適用条件。")
        self.assertIs(focus.supplement("注意事項 曜日 時間 案内 資料 所在 適用 条件",
                                     [candidate], self.normal), self.normal)

    def test_requested_start_attribute_does_not_rescue_another_subject(self):
        normal = [evidence("course", "講座は午前九時から始まります。")]
        unrelated = evidence("other", "見学会の開始時刻は午前十時です。")
        result = focus.supplement("講座 開始時刻", [*normal, unrelated], normal)
        self.assertIs(result, normal)
        self.assertEqual(focus._focus_terms("講座 開始時刻"), ["講座"])

    def test_only_compounds_entirely_made_of_request_words_are_excluded(self):
        for term in ("開始時刻", "開始時間", "終了時刻", "適用条件", "記載場所",
                     "対象範囲", "確認事項", "資料記載場所"):
            with self.subTest(term=term):
                self.assertEqual(focus._focus_terms(term), [])
        self.assertEqual(focus._focus_terms("講座開始時刻"), ["講座開始時刻"])
        self.assertEqual(focus._focus_terms("機器管理"), ["機器"])

    def test_does_not_make_arbitrary_single_kanji_from_compound(self):
        candidate = evidence("unrelated", "外壁の修理に使います。")
        self.assertIs(focus.supplement("壁面図の注意事項", [candidate], self.normal), self.normal)

    def test_explicit_single_kanji_and_multi_kanji_action_suffix(self):
        for query, term in [("傘 注意事項", "傘"), ("鍵受け渡しの注意点", "鍵"),
                            ("防具返却について", "防具")]:
            with self.subTest(query=query):
                source = evidence("s", term + "には番号が付いています。")
                result = focus.supplement(query, [source], self.normal)
                self.assertEqual(result[1]["focus_terms"], [term])

    def test_partial_match_is_only_a_candidate_not_a_supported_answer(self):
        source = evidence("s", "傘下の部署には別規則があります。")
        result = focus.supplement("傘貸出", [source], self.normal)
        self.assertEqual(result[1]["text"], source["text"])
        self.assertNotIn("supported", result[1])
        self.assertNotIn("answer", result[1])

    def test_uses_only_supplied_candidates_no_external_source(self):
        # Caller owns filtering: no omitted candidate can be manufactured here.
        permitted = evidence("permitted", "巡回は指定経路を使う。")
        result = focus.supplement("鍵返却", [permitted], self.normal)
        self.assertIs(result, self.normal)

    def test_conflicting_originals_remain_without_date_preference(self):
        older = evidence("older", "2024年4月：傘は入口で貸し出す。", "連絡")
        newer = evidence("newer", "2025年4月：傘は事務所で貸し出す。", "更新")
        result = focus.supplement("傘貸出", [older, newer], self.normal)
        self.assertEqual([row["evidence_id"] for row in result[1:3]], ["older", "newer"])
        self.assertEqual([row["text"] for row in result[1:3]], [older["text"], newer["text"]])

    def test_normalized_text_aliases_do_not_consume_multiple_slots(self):
        a = evidence("cell", "傘：貸出前に番号を確認。")
        b = evidence("row", "傘 貸出前に番号を確認")
        c = evidence("other", "傘は返却時に確認する。")
        result = focus.supplement("傘貸出", [a, b, c], self.normal)
        self.assertEqual([row["evidence_id"] for row in result],
                         ["normal-1", "cell", "other", "normal-2"])

    def test_duplicate_ids_not_reintroduced(self):
        source = evidence("normal-1", "傘の保管庫。")
        self.assertIs(focus.supplement("傘貸出", [source], self.normal), self.normal)

    def test_numeric_sign_decimal_and_time_differences_are_not_deduplicated(self):
        for left, right in (("-5度", "5度"), ("1.5度", "15度"),
                            ("12:30", "1230"), ("1、2", "12")):
            with self.subTest(left=left, right=right):
                a = evidence("a", f"冷媒の保管条件：{left}。")
                b = evidence("b", f"冷媒の保管条件：{right}。")
                result = focus.supplement("冷媒保管", [a, b], self.normal)
                self.assertEqual([row["evidence_id"] for row in result],
                                 ["normal-1", "a", "b", "normal-2"])
                self.assertEqual([row["text"] for row in result[1:3]],
                                 [a["text"], b["text"]])

    def test_dedup_key_does_not_collapse_meaningful_symbols(self):
        self.assertNotEqual(focus._dedup_key("温度 -5"), focus._dedup_key("温度 5"))
        self.assertNotEqual(focus._dedup_key("量 1.5"), focus._dedup_key("量 15"))
        self.assertNotEqual(focus._dedup_key("使用可？"), focus._dedup_key("使用可"))
        self.assertNotEqual(focus._dedup_key("A、BC"), focus._dedup_key("AB、C"))
        self.assertNotEqual(focus._dedup_key("AB。CD"), focus._dedup_key("ABCD"))
        self.assertEqual(focus._dedup_key("番号 Ａ１"), focus._dedup_key("番号A1"))

    def test_supplemental_limit_is_hard_clamped(self):
        sources = [evidence(str(index), f"傘の番号{index}。") for index in range(6)]
        self.assertEqual(len(focus.supplement("傘貸出", sources, self.normal, 100)), 5)
        self.assertEqual(len(focus.supplement("傘貸出", sources, self.normal, 1)), 3)
        for limit in (0, -1):
            self.assertIs(focus.supplement("傘貸出", sources, self.normal, limit), self.normal)

    def test_query_and_term_limits_are_bounded(self):
        source = evidence("s", "傘は番号で管理する。")
        query = "傘 " + "x" * focus.MAX_QUERY_CHARACTERS
        self.assertIs(focus.supplement(query, [source], self.normal), self.normal)
        terms = focus._focus_terms(" ".join(f"object{index}" for index in range(100)))
        self.assertEqual(len(terms), focus.MAX_TERMS)
        self.assertEqual(focus._focus_terms("x" * (focus.MAX_TERM_CHARACTERS + 1)), [])

    def test_overlong_evidence_is_not_silently_truncated(self):
        source = evidence("s", "傘" + "長" * focus.MAX_EVIDENCE_CHARACTERS)
        self.assertIs(focus.supplement("傘貸出", [source], self.normal), self.normal)
        self.assertEqual(len(source["text"]), focus.MAX_EVIDENCE_CHARACTERS + 1)

    def test_overlong_normal_hit_does_not_suppress_short_whole_source_rescue(self):
        long_source = evidence("long", "傘" + "長" * focus.MAX_EVIDENCE_CHARACTERS)
        short_source = evidence("short", "傘は返却時に番号を確認する。")
        normal = [long_source, *self.normal]
        result = focus.supplement("傘貸出", [*normal, short_source], normal)
        self.assertEqual([row["evidence_id"] for row in result],
                         ["long", "short", "normal-1", "normal-2"])
        self.assertIs(result[0], long_source)
        self.assertEqual(result[0]["text"], long_source["text"])
        self.assertEqual(result[1]["text"], short_source["text"])

    def test_frequent_context_words_do_not_dominate_supplement(self):
        sources = [evidence(str(index), f"設備の番号{index}を確認する。") for index in range(8)]
        self.assertIs(focus.supplement("設備 注意事項", sources, self.normal), self.normal)

    def test_rare_term_coverage_then_existing_rank(self):
        sources = [evidence("broad", "巡回の入口を確認する。"),
                   evidence("both", "鍵と防具は番号を確認する。"),
                   evidence("one", "鍵は事務所へ返す。")]
        result = focus.supplement("鍵返却 防具返却", sources, self.normal)
        self.assertEqual([row["evidence_id"] for row in result[1:3]], ["both", "one"])

    def test_no_normal_results_can_be_supplemented(self):
        source = evidence("s", "鍵は事務所へ返す。")
        result = focus.supplement("鍵返却", [source], [])
        self.assertEqual([row["evidence_id"] for row in result], ["s"])

    def test_nfkc_casefold_and_katakana_preserve_original_evidence(self):
        source = evidence("s", "ヘルメットの内側を確認する。")
        result = focus.supplement("ﾍﾙﾒｯﾄ 貸出", [source], self.normal)
        self.assertEqual(result[1]["focus_terms"], ["ヘルメット"])
        self.assertEqual(result[1]["text"], source["text"])


if __name__ == "__main__":
    unittest.main()
