"""Literal dated-family candidate controls; no source documents or product IO.

The separate legacy-decision class is a residual witness, not end-to-end consent.
Run only through the bounded dated-hitl executor runner after static review.
"""
from __future__ import annotations

import copy
import importlib.util
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "dated_temporal_resolver", ROOT / "distribution/macos-local-memory/engine/document_version_resolver.py")
resolver = importlib.util.module_from_spec(spec)
spec.loader.exec_module(resolver)


def item(path, token="a"):
    return resolver.candidate({
        "kind": "file", "read_status": "observed", "relative_path": path,
        "sha256": token * 64, "size_bytes": 10, "mtime_ns": 2, "birthtime_ns": 1,
    })


def group(paths, decision=None):
    candidates = [item(path, chr(97 + index)) for index, path in enumerate(paths)]
    keys = {resolver.family_key(path) for path in paths}
    if len(keys) != 1:
        raise AssertionError("literal fixture must form one candidate family")
    return resolver.resolve_group(next(iter(keys)), candidates, decision)


class DatedTemporalCandidateTests(unittest.TestCase):
    def assert_token(self, token, year):
        path = "DAWN/受付手順_" + token + ".xlsx"
        self.assertEqual(resolver.family_key(path), "dawn\0受付手順\0.xlsx")
        self.assertEqual(item(path)["explicit_years"], [year])
        self.assertEqual(item(path)["relative_path"], path)

    def assert_hold(self, paths):
        value = group(paths)
        self.assertEqual(value["status"], "needs_human_review")
        self.assertIsNone(value["selected_relative_path"])
        self.assertIsNone(value["resolution_basis"])
        self.assertEqual({c["relative_path"] for c in value["candidates"]}, set(paths))
        self.assertEqual([c["disposition"] for c in value["candidates"]],
                         ["needs_human_review"] * len(paths))
        nodes, edges = resolver.graph_projection([value])
        self.assertEqual(sum(e["edge_type"] == "candidate_for" for e in edges), len(paths))
        self.assertFalse(any(e["edge_type"] == "active_version" for e in edges))
        return value

    def test_compact_202_and_203_calendar_days(self):
        for token, year in (("20240101", 2024), ("20301231", 2030), ("20390228", 2039)):
            with self.subTest(token=token):
                self.assert_token(token, year)

    def test_fullwidth_calendar_candidates(self):
        for token in ("２０２４０２２９", "２０２４－０２－２９", "２０２４年度"):
            with self.subTest(token=token):
                self.assert_token(token, 2024)

    def test_japanese_full_days(self):
        for token, year in (("2024年1月1日", 2024), ("2030年02月28日", 2030)):
            with self.subTest(token=token):
                self.assert_token(token, year)

    def test_separated_days(self):
        for token in ("2024-2-29", "2024_02_29", "2024.02.29"):
            with self.subTest(token=token):
                self.assert_token(token, 2024)

    def test_month_precision(self):
        for token, year in (("2024-01", 2024), ("2030-12", 2030), ("2024_09", 2024)):
            with self.subTest(token=token):
                self.assert_token(token, year)

    def test_fiscal_year_is_candidate_not_replacement(self):
        self.assert_token("2024年度", 2024)
        self.assert_token("2030年度", 2030)
        self.assert_hold(["記録/実績2024年度.csv", "記録/現行_実績2030年度.csv"])

    def test_invalid_calendar_preserves_digits_without_partial_year(self):
        for token, stem in (("20251340", "20251340"), ("20230229", "20230229"),
                            ("2025-13-40", "20251340"), ("2024-00", "202400"),
                            ("2024年2月30日", "2024年2月30日"), ("2024-01_02", "20240102")):
            with self.subTest(token=token):
                path = "手順_" + token + ".csv"
                self.assertEqual(resolver.family_key(path), "\0手順" + stem + "\0.csv")
                self.assertEqual(item(path)["explicit_years"], [])
                self.assertTrue(resolver.has_version_signal(item(path)))

    def test_unknown_202_203_digit_runs_are_not_normalized_away(self):
        for token in ("202", "203", "20251", "203012345678", "202401011"):
            with self.subTest(token=token):
                path = "ID" + token + ".csv"
                self.assertEqual(resolver.family_key(path), "\0id" + token + "\0.csv")
                self.assertEqual(item(path)["explicit_years"], [])
                self.assertTrue(resolver.has_version_signal(item(path)))

    def test_other_numeric_identifiers_are_preserved(self):
        for token in ("120240101", "20400101", "12345678"):
            with self.subTest(token=token):
                path = "ID" + token + ".csv"
                self.assertEqual(resolver.family_key(path), "\0id" + token + "\0.csv")
                self.assertFalse(resolver.has_version_signal(item(path)))

    def test_dated_current_and_undated_current_both_hold(self):
        for paths in (["手順20240101.csv", "現行_手順20300101.csv"],
                      ["手順20240101.csv", "現行_手順.csv"],
                      ["手順20251340.csv", "現行_手順20251340.csv"]):
            with self.subTest(paths=paths):
                value = self.assert_hold(paths)
                self.assertEqual(value["reason_code"], "dated_family_requires_human_review")

    def test_mixed_dated_numeric_versions_never_fall_through(self):
        value = self.assert_hold(["手順20240101_ver1.csv", "手順_ver2.csv"])
        self.assertEqual(value["reason_code"], "dated_family_requires_human_review")

    def test_unrelated_labels_directories_extensions_stay_separate(self):
        base = resolver.family_key("DAWN/受付20240101.csv")
        for path in ("DAWN/配膳20300101.csv", "別業務/受付20300101.csv", "DAWN/受付20300101.xlsx"):
            with self.subTest(path=path):
                self.assertNotEqual(base, resolver.family_key(path))
        self.assertIsNone(item("DAWN/受付20300101.txt"))

    def test_existing_year_only_reason_and_version_cautions_remain(self):
        value = self.assert_hold(["実績2024.csv", "実績2025.csv"])
        self.assertEqual(value["reason_code"], "year_order_does_not_establish_supersession")
        self.assertEqual(item("手順_ver2024.2.30.csv")["explicit_years"], [2024])
        self.assertEqual(resolver.family_key("2024年/業務内容.xlsx"), "年\0業務内容\0.xlsx")

    def test_undated_current_and_numeric_controls_remain(self):
        current = group(["旧版/手順.csv", "現行/手順.csv"])
        self.assertEqual(current["selected_relative_path"], "現行/手順.csv")
        self.assertEqual(current["reason_code"], "unique_explicit_current_marker")
        numeric = group(["手順_ver1.2.csv", "手順_ver1.10.csv"])
        self.assertEqual(numeric["selected_relative_path"], "手順_ver1.10.csv")
        self.assertEqual(numeric["reason_code"], "unique_latest_explicit_version")
        conflict = self.assert_hold(["現行/手順_ver1.csv", "手順_ver2.csv"])
        self.assertEqual(conflict["reason_code"], "current_marker_conflicts_with_latest_version")

    def test_reconstruction_includes_complete_dated_family_and_policy(self):
        records = [{"kind": "file", "read_status": "observed", "relative_path": path,
                    "sha256": token * 64, "size_bytes": 10, "mtime_ns": 2, "birthtime_ns": 1}
                   for path, token in (("手順20240101.csv", "a"), ("現行_手順20300101.csv", "b"))]
        value = resolver._validation_components(records, {})
        self.assertEqual(value["counts"], {"groups": 1, "resolved": 0, "needs_human_review": 1})
        self.assertEqual(value["resolver_version"], "0.1.5")
        self.assertIs(value["policy"]["date_likeness_is_authoritative"], False)
        self.assertEqual(value["groups"][0]["reason_code"], "dated_family_requires_human_review")
        self.assertEqual(len(value["groups"][0]["candidates"]), 2)

    def test_order_and_peer_change_keep_hold_and_change_bound_set(self):
        paths = ["手順20240101.csv", "現行_手順20300101.csv"]
        candidates = [item(paths[0], "a"), item(paths[1], "b")]
        key = "\0手順\0.csv"
        first = resolver.resolve_group(key, copy.deepcopy(candidates), None)
        reverse = resolver.resolve_group(key, list(reversed(copy.deepcopy(candidates))), None)
        self.assertEqual(first, reverse)
        changed = copy.deepcopy(candidates)
        changed[0]["source_sha256"] = "c" * 64
        altered = resolver.resolve_group(key, changed, None)
        self.assertNotEqual(first["candidate_set_sha256"], altered["candidate_set_sha256"])
        self.assertIsNone(altered["selected_relative_path"])


class DatedLegacyDecisionResidualTests(unittest.TestCase):
    def test_matching_legacy_selection_still_is_not_new_use_approval(self):
        paths = ["実績2024.csv", "現行_実績2025.csv"]
        initial = group(paths)
        decision = {"candidate_set_sha256": initial["candidate_set_sha256"],
                    "selected_relative_path": paths[0], "selected_source_sha256": "a" * 64}
        selected = group(paths, decision)
        self.assertEqual(selected["resolution_basis"], "human")
        self.assertEqual(selected["selected_relative_path"], paths[0])
        self.assertNotIn("answer_use_approved", selected)
