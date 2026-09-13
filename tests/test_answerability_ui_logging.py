"""Exercise provisional answers and diagnostic IDs through the localhost UI."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest import mock

import test_dated_hitl_http_e2e as http_tests


class AnswerabilityUiLoggingTests(unittest.TestCase):
    setUpClass = classmethod(http_tests.DatedHitlHttpE2ETests.setUpClass.__func__)
    post = http_tests.DatedHitlHttpE2ETests.post

    def setUp(self):
        http_tests.DatedHitlHttpE2ETests.setUp(self)
        self.temporary = tempfile.TemporaryDirectory()
        self.support = Path(self.temporary.name)
        self.support_patch = mock.patch.object(self.server_module.bootstrap, "SUPPORT", self.support)
        self.support_patch.start()

    def tearDown(self):
        http_tests.DatedHitlHttpE2ETests.tearDown(self)
        self.support_patch.stop()
        self.temporary.cleanup()

    def search(self, record=None, side_effect=None):
        module = self.server_module
        contract = module.intent_contract.make_contract(
            "福徳神社の創建年と訪問有無は？", "創建年と訪問有無の確認", "創建年\n訪問有無", {"generation": "one"})
        payload, signature = module.intent_contract.seal(contract, module.intent_contract.SIGNING_KEY)
        response = mock.MagicMock()
        answer_text = record["answer"]["answer"] if record else ""
        response.__enter__.return_value.read.return_value = json.dumps({"response": json.dumps({"items": [
            {"index": index, "covered": True, "quote": answer_text} for index in range(2)
        ]})}).encode()
        with (
            mock.patch.object(module, "state", return_value={"phase": "ready"}),
            mock.patch.object(module.bootstrap, "active_answer_revision_identity",
                              return_value=(True, "current", contract["revision"])),
            mock.patch.object(module.bootstrap, "load_json", return_value={"audit_model": "test", "sequential_model_loading": False}),
            mock.patch.object(module.LOCAL_HTTP_OPENER, "open", return_value=response),
            mock.patch.object(module, "answer_query", return_value=record, side_effect=side_effect),
            mock.patch.object(module, "semantic_graph_candidate_notice", return_value=""),
            mock.patch.object(module, "security_exclusion_notice", return_value=""),
        ):
            return self.post("/local-search-answer", {
                module.UI_CSRF_FIELD: self.httpd.ui_csrf_token,
                "query": contract["question"], "intent_action": "confirm",
                "intent_payload": payload, "intent_signature": signature,
            })

    def events(self, wait_for=None):
        deadline = time.monotonic() + 1
        while True:
            with self.server_module.SEARCH_LOG_LOCK:
                events = [json.loads(line) for line in (self.support / "logs/request-events.jsonl").read_text().splitlines()]
            if wait_for is None or any(item["event"] == wait_for for item in events):
                return events
            if time.monotonic() >= deadline:
                self.fail("Request did not record its terminal event: " + wait_for)
            time.sleep(0.01)

    def test_http_error_has_matching_request_id_and_bounded_redacted_traceback(self):
        def fail(*_args, **_kwargs):
            self.server_module._search_stage("final_answer_audit")
            raise RuntimeError("api_key=private-key " + self.httpd.ui_csrf_token + " " + "x" * 20000)

        status, body = self.search(side_effect=fail)
        self.assertEqual(status, 500)
        events = self.events()
        failure = events[-1]
        self.assertEqual(failure["event"], "request_failed")
        self.assertEqual(failure["stage"], "final_answer_audit")
        self.assertEqual(failure["exception"]["type"], "RuntimeError")
        self.assertIn("Traceback", failure["exception"]["traceback"])
        self.assertLessEqual(len(failure["exception"]["traceback"]), 16000)
        self.assertLessEqual(len(failure["exception"]["message"]), 2000)
        self.assertIn(failure["request_id"], body)
        self.assertIn("処理上の問題", body)
        self.assertNotIn("RuntimeError", body)
        raw = json.dumps(events, ensure_ascii=False)
        self.assertNotIn("private-key", raw)
        self.assertNotIn(self.httpd.ui_csrf_token, raw)
        self.assertNotIn("intent_payload", raw)
        self.assertEqual((self.support / "logs/request-events.jsonl").stat().st_mode & 0o777, 0o600)

    def test_verified_partial_answer_survives_and_coverage_cannot_claim_completion(self):
        text = "資料に福徳神社の記載があります。訪問有無は未確認です。"
        record = {
            "answer": {"answer": text, "answer_mode": "qualified", "answer_status": "answered"},
            "independent_final_audit": {"verdict": "verified"},
            "answerability_policy": {"version": "provisional-v1", "applied": True,
                "confirmed_field_ids": ["name"], "unresolved_field_ids": ["visit"],
                "reference_only": False, "observations": []},
        }
        status, body = self.search(record)
        self.assertEqual(status, 200)
        self.assertIn(text, body)
        self.assertIn("確認できた範囲を表示しています", body)
        self.assertIn("回答は未完了です", body)
        self.assertNotIn("合意した内容を確認できました", body)
        final = self.events("request_completed")[-1]
        intent = json.loads((self.support / "logs/intent-answers.jsonl").read_text().splitlines()[-1])
        self.assertEqual(final["request_id"], intent["request_id"])
        self.assertIn(final["request_id"], body)
        self.assertEqual(final["result"]["policy_version"], "provisional-v1")
        self.assertFalse(intent["coverage"]["complete"])

    def test_failed_subprocess_keeps_stderr_without_response_payload_or_credentials(self):
        module = self.server_module
        error = subprocess.CalledProcessError(1, ["worker.py"],
            output="private model response", stderr="RuntimeError: audit unavailable; Authorization: Bearer private-token")
        module._log_search_event({"request_id": "test-id", "request_started_at": "2026-09-12T10:00:00+09:00",
            "question": "質問", "stage": "final_answer_audit"}, "request_failed", exc=error)
        event = self.events()[-1]
        self.assertEqual(event["exception"]["subprocess_returncode"], 1)
        self.assertIn("audit unavailable", event["exception"]["subprocess_stderr"])
        self.assertNotIn("private-token", json.dumps(event))
        self.assertNotIn("private model response", json.dumps(event))

    def test_reference_only_reading_is_visible_but_does_not_complete_the_question(self):
        text = "読み取り結果には『870年頃？（平安時代）』とあります。年号は未確認です。"
        raw_reason = "The shrine was established in 860AD. This unsupported explanation is diagnostic only."
        record = {
            "answer": {"answer": text, "answer_mode": "qualified", "answer_status": "insufficient"},
            "independent_final_audit": {"verdict": "verified", "reason": raw_reason},
            "answerability_policy": {"version": "provisional-v1", "applied": True,
                "confirmed_field_ids": [], "unresolved_field_ids": ["year", "visit"],
                "reference_only": True, "observations": [{"field_id": "year",
                    "kind": "provisional_reading", "quote": "870年頃？（平安時代）",
                    "path": "ガイド.pdf", "locator": {"page": 3}, "evidence_id": "evidence-1"}]},
        }
        status, body = self.search(record)
        self.assertEqual(status, 200)
        self.assertIn(text, body)
        self.assertIn("暫定の読み取りを含みます", body)
        self.assertIn("質問への確定回答はまだ得られていません", body)
        self.assertIn("回答は未完了です", body)
        self.assertIn("独立監査: 確認済み", body)
        self.assertNotIn("860AD", body)
        self.assertNotIn(raw_reason, body)
        event = self.events("request_completed")[-1]
        self.assertFalse(event["complete"])
        self.assertEqual(event["result"]["audit_reason"], raw_reason)
        self.assertIn(event["request_id"], body)

    def test_independent_qualified_verdict_is_not_labeled_confirmed(self):
        record = {"answer": {"answer": "保留", "answer_mode": "qualified"},
                  "independent_final_audit": {"verdict": "qualified"},
                  "answerability_policy": {"applied": True, "confirmed_field_ids": ["x"]}}
        self.assertEqual(self.server_module.answerability_notice(record), "")
        status, body = self.search(record)
        self.assertEqual(status, 200)
        self.assertIn("回答は未完了です", body)
        self.assertNotIn("確認できた範囲を表示しています", body)

    def test_access_log_keeps_request_and_adds_timestamp_and_request_id(self):
        self.log_patch.stop()
        handler = object.__new__(self.server_module.Handler)
        handler.path = "/local-search-answer"
        handler.search_request_id = "test-request-id"
        handler.log_message('"%s" %s -', "POST /local-search-answer HTTP/1.1", "500")
        line = (self.support / "logs/server.log").read_text()
        self.assertRegex(line, r'^\[\d{4}-\d{2}-\d{2}T')
        self.assertIn('"POST /local-search-answer HTTP/1.1" 500 -', line)
        self.assertIn("request_id=test-request-id", line)


if __name__ == "__main__":
    unittest.main()
