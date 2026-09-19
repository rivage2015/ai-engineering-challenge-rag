"""Offline regression tests: incomplete final audits never publish a candidate."""

from __future__ import annotations

import json
import unittest
from unittest import mock

from test_answerability_integration import audit, fixture, run_main


VERIFIED = {"verdict": "verified", "reason": "原文と一致", "unsupported_claims": []}


def model_response(result=None, **metadata):
    return {
        "message": {"content": json.dumps(VERIFIED if result is None else result, ensure_ascii=False)},
        "done": True, "done_reason": "stop", "prompt_eval_count": 6000, "eval_count": 100,
        **metadata,
    }


def call_audit(raw=None, *, transport_error=None, query="案内資料の表示色は何ですか？"):
    record, packets = fixture()
    body = json.dumps(model_response() if raw is None else raw).encode()
    response = mock.MagicMock()
    response.__enter__.return_value.read.return_value = body
    with mock.patch.object(audit.LOCAL_HTTP_OPENER, "open", return_value=response,
                           side_effect=transport_error) as request:
        result, performance = audit.audit_fail_closed(
            "local-test-model", query, record["answer"], packets, 300)
    return result, performance, request


class NormalFinalAuditCapacityTests(unittest.TestCase):
    def assert_incomplete(self, result, performance, reason):
        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(result["verdict"], "rejected")
        self.assertEqual(result["reason_code"], reason)
        self.assertEqual(result["unsupported_claims"], [])
        self.assertTrue(performance["failed"])
        self.assertEqual(performance["failure_reason"], reason)
        self.assertEqual(performance["context_usage"]["status"], "incomplete")

    def test_complete_response_requests_8192_and_records_capacity(self):
        result, performance, request = call_audit()
        self.assertEqual(result, VERIFIED)
        self.assertNotIn("failed", performance)
        payload = json.loads(request.call_args.args[0].data)
        self.assertEqual(payload["options"], {"temperature": 0, "num_ctx": 8192, "num_predict": 320})
        self.assertFalse(payload["think"])
        self.assertFalse(payload["stream"])
        self.assertEqual(request.call_args.kwargs["timeout"], 300)
        usage = performance["context_usage"]
        self.assertEqual(usage["requested_context_tokens"], 8192)
        self.assertEqual((usage["input_tokens"], usage["output_tokens"], usage["total_tokens"]), (6000, 100, 6100))
        self.assertEqual(usage["done_reason"], "stop")
        self.assertIs(usage["done"], True)
        self.assertEqual(usage["status"], "observed")

    def test_workflow_word_in_normal_query_keeps_existing_output_limit(self):
        _, _, request = call_audit(query="受付の手順を教えてください。")
        self.assertEqual(json.loads(request.call_args.args[0].data)["options"]["num_predict"], 1000)

    def test_length_is_rejected_before_broken_json_parsing(self):
        raw = model_response(done_reason="length", prompt_eval_count=8155, eval_count=37)
        raw["message"]["content"] = '{"verdict":"verified","reason":"'
        result, performance, request = call_audit(raw)
        self.assert_incomplete(result, performance, "audit_response_truncated")
        self.assertEqual(performance["context_usage"]["total_tokens"], 8192)
        self.assertEqual(performance["context_usage"]["done_reason"], "length")
        self.assertEqual(request.call_count, 1)

    def test_length_is_rejected_even_when_json_claims_verified(self):
        result, performance, _ = call_audit(model_response(done_reason="length"))
        self.assert_incomplete(result, performance, "audit_response_truncated")

    def test_capacity_boundary_is_rejected_even_when_reported_stop(self):
        result, performance, _ = call_audit(model_response(prompt_eval_count=8092, eval_count=100))
        self.assert_incomplete(result, performance, "audit_context_exhausted")

    def test_missing_or_invalid_usage_never_defaults_to_zero(self):
        for key in ("prompt_eval_count", "eval_count"):
            for value in (None, True, "100", -1):
                with self.subTest(key=key, value=value):
                    raw = model_response(**{key: value})
                    result, performance, _ = call_audit(raw)
                    self.assert_incomplete(result, performance, "audit_usage_missing")
            raw = model_response()
            del raw[key]
            result, performance, _ = call_audit(raw)
            self.assert_incomplete(result, performance, "audit_usage_missing")

    def test_impossible_usage_is_not_accepted(self):
        for metadata in ({"prompt_eval_count": 0}, {"eval_count": 0}, {"eval_count": 321}):
            with self.subTest(metadata=metadata):
                result, performance, _ = call_audit(model_response(**metadata))
                self.assert_incomplete(result, performance, "audit_usage_invalid")

    def test_done_and_stop_are_required(self):
        for metadata in ({"done": False}, {"done": None}, {"done": "true"},
                         {"done_reason": None}, {"done_reason": "unexpected-secret"}):
            with self.subTest(metadata=metadata):
                result, performance, _ = call_audit(model_response(**metadata))
                self.assert_incomplete(result, performance, "audit_response_not_finished")
                self.assertNotIn("unexpected-secret", json.dumps((result, performance)))

    def test_invalid_inner_json_and_duplicate_keys_are_not_repaired(self):
        for content in ('{"verdict":"verified"',
                        '{"verdict":"rejected","verdict":"verified","reason":"ok","unsupported_claims":[]}'):
            with self.subTest(content=content):
                raw = model_response()
                raw["message"]["content"] = content
                result, performance, _ = call_audit(raw)
                self.assert_incomplete(result, performance, "audit_response_invalid_json")

    def test_response_envelope_types_are_checked(self):
        for raw in ([], "text", model_response(message=[]), model_response(message={"content": {}})):
            with self.subTest(raw=raw):
                result, performance, _ = call_audit(raw)
                self.assert_incomplete(result, performance, "audit_response_type_invalid")

    def test_required_fields_types_lengths_and_extra_fields_are_checked(self):
        cases = [[], "verified", 1, True,
                 {"verdict": "verified", "unsupported_claims": []},
                 {**VERIFIED, "reason": 5}, {**VERIFIED, "reason": "x" * 241},
                 {**VERIFIED, "verdict": "PASS"},
                 {**VERIFIED, "unsupported_claims": [5]},
                 {**VERIFIED, "unsupported_claims": ["x" * 181]},
                 {**VERIFIED, "unsupported_claims": ["x"] * 7},
                 {**VERIFIED, "unsupported_claims": "none"},
                 {**VERIFIED, "extra": "invented"}]
        for value in cases:
            with self.subTest(value=value):
                result, performance, _ = call_audit(model_response(value))
                self.assert_incomplete(result, performance, "audit_response_schema_invalid")

    def test_semantic_contract_failure_retains_observed_usage(self):
        result, performance, _ = call_audit(model_response({**VERIFIED, "unsupported_claims": ["表示色"]}))
        self.assert_incomplete(result, performance, "verified_with_unsupported_claim")
        self.assertEqual(performance["context_usage"]["input_tokens"], 6000)

    def test_transport_failure_is_sanitized_without_retry(self):
        result, performance, request = call_audit(transport_error=TimeoutError("secret source text"))
        self.assert_incomplete(result, performance, "audit_transport_error")
        self.assertIsNone(performance["context_usage"]["input_tokens"])
        self.assertEqual(performance["context_usage"]["requested_context_tokens"], 8192)
        self.assertNotIn("secret source text", json.dumps((result, performance)))
        self.assertEqual(request.call_count, 1)

    def test_invalid_http_json_is_incomplete(self):
        record, packets = fixture()
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = b"not json"
        with mock.patch.object(audit.LOCAL_HTTP_OPENER, "open", return_value=response):
            result, performance = audit.audit_fail_closed("local-test", record["query"], record["answer"], packets, 300)
        self.assert_incomplete(result, performance, "audit_transport_invalid_json")

    def test_main_initial_failure_hides_candidate_and_does_not_reaudit(self):
        record, packets = fixture()
        result, calls = run_main(record, packets, [TimeoutError("not source evidence")])
        self.assertEqual(calls.call_count, 1)
        self.assertEqual(result["independent_final_audit"]["status"], "incomplete")
        self.assertFalse(result["orchestration_decision"]["checks"]["independent_audit"])
        self.assertEqual(result["orchestration_decision"]["status"], "rejected")
        self.assertEqual(result["answer"]["answer"], "わかりません")
        self.assertEqual(result["answer"]["evidence_ids"], [])
        self.assertIn("監査が未完了", result["answer"]["basis_summary"])
        self.assertEqual(result["answer"]["non_answer_reason"]["code"], "machine_validation_failure")

    def test_main_reaudit_failure_does_not_publish_repaired_candidate(self):
        record, packets = fixture()
        initial = ({"verdict": "qualified", "reason": "暫定引用の扱いを再確認", "unsupported_claims": ["870年頃？（平安時代）"]},
                   {"wall_seconds": 0.01})
        failure = audit.audit_guard.AuditResponseError("audit_response_truncated", {
            "requested_context_tokens": 8192, "requested_output_tokens": 320,
            "input_tokens": 8155, "output_tokens": 37, "total_tokens": 8192,
            "done": True, "done_reason": "length", "status": "incomplete"})
        result, calls = run_main(record, packets, [initial, failure])
        self.assertEqual(calls.call_count, 2)
        self.assertEqual(result["answerability_reaudit"]["excluded_field_ids"], ["F2"])
        self.assertEqual(result["independent_final_audit"]["status"], "incomplete")
        self.assertEqual(result["independent_final_audit"]["reason_code"], "audit_response_truncated")
        self.assertEqual(result["answer"]["answer"], "わかりません")
        self.assertEqual(result["orchestration_decision"]["status"], "rejected")
        metrics = result["performance"]["independent_final_audit"]
        self.assertEqual(metrics["attempts"], 2)
        self.assertEqual(metrics["context_usage"]["output_tokens"], 37)
        self.assertEqual(metrics["first_attempt"], {"wall_seconds": 0.01})


if __name__ == "__main__":
    unittest.main()
