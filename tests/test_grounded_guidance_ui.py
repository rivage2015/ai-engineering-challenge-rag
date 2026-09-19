"""Display the separately verified guidance without changing the extractive record."""
from __future__ import annotations

import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import test_dated_hitl_http_e2e as http_tests


class GroundedGuidanceUiTests(unittest.TestCase):
    setUpClass = classmethod(http_tests.DatedHitlHttpE2ETests.setUpClass.__func__)
    post = http_tests.DatedHitlHttpE2ETests.post

    def setUp(self):
        http_tests.DatedHitlHttpE2ETests.setUp(self)
        self.temporary = tempfile.TemporaryDirectory()
        self.support = Path(self.temporary.name)
        self.support_patch = mock.patch.object(self.server_module.bootstrap, "SUPPORT", self.support)
        self.support_patch.start()
        self.record = {
            "answer": {"answer": "RAW_EXTRACTED_TEMPLATE ○○", "answer_mode": "qualified"},
            "independent_final_audit": {"verdict": "verified"},
            "claim_graph": {"unchanged": "original exact quotation"},
        }
        self.contract = self.server_module.intent_contract.make_contract(
            "展示の曜日と時刻、お客様への案内を教えて", "曜日・時刻・案内例を知りたい",
            "曜日と時刻を含む案内例", {"generation": "one"})
        self.view = {
            "answer": "資料をもとにした案内例：展示は火曜日の10時開始です。",
            "coverage": {"complete": True, "items": [{"requirement": "曜日と時刻を含む案内例", "covered": True}]},
            "sources": [{"relative_path": "sample.txt", "locator": {"line": 2},
                         "evidence_id": "e1", "quote": "展示 火曜 10:00"}],
        }

    def tearDown(self):
        http_tests.DatedHitlHttpE2ETests.tearDown(self)
        self.support_patch.stop()
        self.temporary.cleanup()

    def search(self, *, incomplete=False, revision_changed=False, binding_changed=False, fallback_complete=False):
        m = self.server_module
        payload, signature = m.intent_contract.seal(self.contract, m.intent_contract.SIGNING_KEY)
        def prepare(record, *_):
            record["grounded_guidance"] = {"status": "incomplete" if incomplete else "verified"}
            return None if incomplete else copy.deepcopy(self.view)
        with (
            mock.patch.object(m, "state", return_value={"phase": "ready"}),
            mock.patch.object(m.bootstrap, "active_answer_revision_identity", side_effect=[
                (True, "current", self.contract["revision"]),
                (True, "current", {"generation": "changed"} if revision_changed else self.contract["revision"])]),
            mock.patch.object(m, "answer_query", return_value=self.record),
            mock.patch.object(m, "prepare_grounded_guidance", side_effect=prepare),
            mock.patch.object(m.grounded_guidance, "verified_view", return_value=None if binding_changed else self.view),
            mock.patch.object(m, "audit_intent_coverage", return_value={"complete": fallback_complete, "items": []}) as old_coverage,
            mock.patch.object(m, "semantic_graph_candidate_notice", return_value=""),
            mock.patch.object(m, "security_exclusion_notice", return_value=""),
        ):
            result = self.post("/local-search-answer", {
                m.UI_CSRF_FIELD: self.httpd.ui_csrf_token, "query": self.contract["question"],
                "intent_action": "confirm", "intent_payload": payload, "intent_signature": signature})
            self.assertEqual(old_coverage.call_count, 1 if incomplete else 0)
            return result

    def test_verified_sidecar_is_the_display_and_log_retains_original(self):
        original = copy.deepcopy(self.record)
        status, body = self.search()
        self.assertEqual(status, 200)
        self.assertIn(self.view["answer"], body)
        self.assertNotIn("RAW_EXTRACTED_TEMPLATE", body)
        self.assertIn("合意した内容を確認できました", body)
        self.assertIn("案内例の根拠", body)
        self.assertIn("展示 火曜 10:00", body)
        self.assertIn("別コンテキスト監査", body)
        self.assertEqual(self.record["answer"], original["answer"])
        self.assertEqual(self.record["claim_graph"], original["claim_graph"])
        path = self.support / "logs/guidance-answers.jsonl"
        log = json.loads(path.read_text().splitlines()[-1])
        self.assertEqual(log["extracted_answer"], original["answer"])
        self.assertEqual(log["displayed_answer"], self.view["answer"])
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_incomplete_guidance_preserves_raw_verified_facts_but_not_completion(self):
        status, body = self.search(incomplete=True, fallback_complete=True)
        self.assertEqual(status, 200)
        self.assertIn("案内例の作成・点検は完了していません", body)
        self.assertIn("RAW_EXTRACTED_TEMPLATE", body)
        self.assertNotIn(self.view["answer"], body)
        self.assertNotIn("合意した内容を確認できました", body)

    def test_revision_change_withholds_guidance(self):
        with mock.patch.object(self.server_module, "home", side_effect=lambda text, *_: text.encode()):
            status, body = self.search(revision_changed=True)
        self.assertEqual(status, 409)
        self.assertNotIn(self.view["answer"], body)
        self.assertFalse((self.support / "logs/guidance-answers.jsonl").exists())

    def test_display_binding_change_withholds_guidance(self):
        status, body = self.search(binding_changed=True)
        self.assertEqual(status, 409)
        self.assertIn("案内例の確認状態が変わりました", body)
        self.assertNotIn(self.view["answer"], body)

    def test_unrelated_query_does_not_load_config_or_call_model(self):
        m = self.server_module
        with (mock.patch.object(m.grounded_guidance, "eligible", return_value=False),
              mock.patch.object(m.bootstrap, "load_config_snapshot") as config,
              mock.patch.object(m.grounded_guidance, "compose_guidance") as compose):
            self.assertIsNone(m.prepare_grounded_guidance(self.record, self.contract, self.contract["revision"]))
        config.assert_not_called()
        compose.assert_not_called()

    def test_composition_exception_is_incomplete_not_pipeline_error(self):
        m = self.server_module
        with (mock.patch.object(m.grounded_guidance, "eligible", return_value=True),
              mock.patch.object(m.bootstrap, "load_config_snapshot", return_value=(True, {
                  "index_path": str(self.support / "index"), "answer_model": "local-test",
                  "sequential_model_loading": False})),
              mock.patch.object(m.bootstrap, "answer_config_matches_revision", return_value=True),
              mock.patch.object(m.grounded_guidance, "compose_guidance", side_effect=TimeoutError("bounded"))):
            self.assertIsNone(m.prepare_grounded_guidance(self.record, self.contract, self.contract["revision"]))
        self.assertEqual(self.record["grounded_guidance"]["status"], "incomplete")
        self.assertEqual(self.record["answer"]["answer"], "RAW_EXTRACTED_TEMPLATE ○○")

    def test_failed_view_binding_cannot_keep_verified_label(self):
        m = self.server_module
        with (mock.patch.object(m.grounded_guidance, "eligible", return_value=True),
              mock.patch.object(m.bootstrap, "load_config_snapshot", return_value=(True, {
                  "index_path": str(self.support / "index"), "answer_model": "local-test",
                  "sequential_model_loading": False})),
              mock.patch.object(m.bootstrap, "answer_config_matches_revision", return_value=True),
              mock.patch.object(m.grounded_guidance, "compose_guidance", return_value={"status": "verified"}),
              mock.patch.object(m.grounded_guidance, "verified_view", return_value=None)):
            self.assertIsNone(m.prepare_grounded_guidance(self.record, self.contract, self.contract["revision"]))
        self.assertEqual(self.record["grounded_guidance"]["status"], "incomplete")
        self.assertEqual(self.record["grounded_guidance"]["reason"], "guidance_view_binding_failed")


if __name__ == "__main__":
    unittest.main()
