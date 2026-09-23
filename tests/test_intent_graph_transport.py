"""Exercise the isolated intent transport without models, sockets or live data."""

import copy
import importlib.util
import json
from contextlib import ExitStack
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest import mock

from test_dated_hitl_http_e2e import APP, load_server


class IntentGraphTransportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = load_server()
        spec = importlib.util.spec_from_file_location(
            "intent_transport_final_audit", APP / "final_answer_audit.py")
        cls.audit = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.audit)

    def setUp(self):
        self.revision = {"generation": "synthetic-generation", "source_sha256": "abc"}
        self.contract = self.server.intent_contract.make_contract(
            "会議室の名前は？", "予約するときに使う正式名称を知りたい。",
            "資料で確認できる正式名称", self.revision)
        helper = self.server.intent_contract.graph_helper()
        plan = helper.plan_from_contract(self.contract)
        self.record = {
            "query": self.contract["question"],
            "question_reference_date": "2026-09-23",
            "confirmed_intent": copy.deepcopy(self.contract),
            "intent_requirement_graph": helper.compile_contract(self.contract),
            "question_plan": plan,
            "field_runs": [{"item": copy.deepcopy(plan["items"][0]),
                            "audit": {"verdict": "supported", "supported_value": "青空"}}],
            "answer": {"answer": "青空です。", "answer_mode": "grounded"},
        }
        self.audited = copy.deepcopy(self.record)
        self.audited["independent_final_audit"] = {"verdict": "verified"}

    def isolated_pipeline(self, stack, directory, records):
        """Mock process boundaries only; request/response binding remains real."""
        m = self.server
        config = {"index_path": str(Path(directory) / "unused-synthetic.sqlite3"),
                  "answer_model": "synthetic-gemma", "audit_model": "synthetic-gemma",
                  "sequential_model_loading": False}
        stack.enter_context(mock.patch.object(m.bootstrap, "SUPPORT", Path(directory)))
        stack.enter_context(mock.patch.object(m.bootstrap, "load_config_snapshot",
                                               return_value=(True, config)))
        stack.enter_context(mock.patch.object(m.bootstrap, "answer_config_matches_revision",
                                               return_value=True))
        started = stack.enter_context(mock.patch.object(m.bootstrap, "start_ollama"))
        stack.enter_context(mock.patch.object(m, "_search_stage"))
        stack.enter_context(mock.patch.object(m, "_attach_search_request"))
        stack.enter_context(mock.patch.object(m, "unload_ollama_model"))
        candidates = stack.enter_context(mock.patch.object(
            m, "run_semantic_graph_candidate", return_value=(None, {"enabled": False})))
        stack.enter_context(mock.patch.object(m, "run_semantic_graph_edge_audit",
                                               return_value=(None, {"enabled": False})))
        stack.enter_context(mock.patch.object(m, "apply_semantic_graph_answer_promotion",
                                               return_value={"enabled": False}))
        subprocess_call = stack.enter_context(mock.patch.object(
            m.subprocess, "run", side_effect=[
                SimpleNamespace(stdout=json.dumps(record, ensure_ascii=False))
                for record in records]))
        return started, subprocess_call, candidates

    def test_exact_contract_uses_stdin_original_question_remains_cli_query(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            started, process, _ = self.isolated_pipeline(
                stack, directory, [self.record, self.audited])
            result = self.server.answer_query(
                self.contract["question"], expected_active_revision=self.revision,
                confirmed_intent=self.contract)
            started.assert_called_once()
            self.assertEqual(process.call_count, 2)
            generated = process.call_args_list[0]
            self.assertEqual(generated.args[0][2], self.contract["question"])
            self.assertIn("--intent-contract-stdin", generated.args[0])
            self.assertNotIn(self.contract["goal"], generated.args[0])
            self.assertEqual(json.loads(generated.kwargs["input"]), self.contract)
            self.assertTrue(generated.kwargs["text"])
            audited = process.call_args_list[1]
            self.assertIn("--record", audited.args[0])
            self.assertNotIn("input", audited.kwargs)
            self.assertEqual(result["confirmed_intent"], self.contract)
            self.assertEqual(result["intent_requirement_graph"],
                             self.record["intent_requirement_graph"])
            saved = json.loads((Path(directory) / "logs/audited-answers.jsonl").read_text())
            self.assertEqual(saved["confirmed_intent"], self.contract)
            temporary_record = Path(audited.args[0][audited.args[0].index("--record") + 1])
            self.assertFalse(temporary_record.exists())

    def test_wrong_question_expiry_and_revision_stop_before_model_start(self):
        cases = [
            ("別の質問ですか？", copy.deepcopy(self.contract)),
            (self.contract["question"], {**self.contract, "expires_at": time.time() - 1}),
            (self.contract["question"], {**self.contract, "revision": {"generation": "other"}}),
        ]
        for query, contract in cases:
            with self.subTest(query=query, contract=contract):
                with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
                    started, process, candidates = self.isolated_pipeline(stack, directory, [])
                    with self.assertRaises(ValueError):
                        self.server.answer_query(query, expected_active_revision=self.revision,
                                                 confirmed_intent=contract)
                    started.assert_not_called()
                    process.assert_not_called()
                    candidates.assert_not_called()
                    self.assertFalse((Path(directory) / "logs").exists())

    def test_mismatched_engine_record_stops_before_final_audit(self):
        mutations = [
            lambda r: r.update(query="別の質問"),
            lambda r: r["confirmed_intent"].update(goal="別の目的"),
            lambda r: r["field_runs"][0]["item"].update(intent_requirement_id="R9"),
            lambda r: r["question_plan"]["items"].clear(),
        ]
        for index, mutate in enumerate(mutations):
            with self.subTest(mutation=index):
                bad_record = copy.deepcopy(self.record)
                mutate(bad_record)
                with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
                    _, process, candidates = self.isolated_pipeline(stack, directory, [bad_record])
                    with self.assertRaises(ValueError):
                        self.server.answer_query(self.contract["question"],
                            expected_active_revision=self.revision, confirmed_intent=self.contract)
                    self.assertEqual(process.call_count, 1)
                    candidates.assert_not_called()
                    self.assertFalse((Path(directory) / "logs").exists())

    def test_mismatched_final_audit_record_stops_before_downstream_processing(self):
        bad_record = copy.deepcopy(self.audited)
        bad_record["field_runs"][0]["item"]["intent_requirement_id"] = "R9"
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            _, process, candidates = self.isolated_pipeline(
                stack, directory, [self.record, bad_record])
            with self.assertRaises(ValueError):
                self.server.answer_query(self.contract["question"],
                    expected_active_revision=self.revision, confirmed_intent=self.contract)
            self.assertEqual(process.call_count, 2)
            candidates.assert_not_called()
            self.assertFalse((Path(directory) / "logs").exists())
            command = process.call_args_list[1].args[0]
            self.assertFalse(Path(command[command.index("--record") + 1]).exists())

    def test_incomplete_final_audit_cannot_be_promoted_by_intent_binding(self):
        incomplete = copy.deepcopy(self.audited)
        incomplete["independent_final_audit"] = {
            "status": "incomplete", "verdict": "rejected", "reason_code": "synthetic_failure"}
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            _, process, candidates = self.isolated_pipeline(
                stack, directory, [self.record, incomplete])
            result = self.server.answer_query(self.contract["question"],
                expected_active_revision=self.revision, confirmed_intent=self.contract)
            self.assertEqual(process.call_count, 2)
            candidates.assert_not_called()
            self.assertTrue(self.server.final_audit_incomplete(result))
            self.assertEqual(result["independent_final_audit"]["verdict"], "rejected")
            self.assertEqual(result["pipeline_performance"]["downstream_skipped_reason"],
                             "final_audit_incomplete")

    def test_final_fact_audit_receives_original_intent_in_one_http_call(self):
        raw = {"message": {"content": json.dumps({"verdict": "verified",
                    "reason": "名称が原文にある", "unsupported_claims": []}, ensure_ascii=False)},
               "done": True, "done_reason": "stop", "prompt_eval_count": 2000,
               "eval_count": 60}
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps(raw).encode()
        with mock.patch.object(self.audit.LOCAL_HTTP_OPENER, "open",
                               return_value=response) as opened:
            result, performance = self.audit.audit(
                "synthetic-gemma", self.contract["question"], self.record["answer"],
                [{"text": "会議室の正式名称は青空です。"}], 30,
                graph_context={"intent_requirement_graph": self.record["intent_requirement_graph"]})
        opened.assert_called_once()
        payload = json.loads(opened.call_args.args[0].data)
        prompt = payload["messages"][1]["content"]
        self.assertIn(self.contract["question"], prompt)
        self.assertIn(self.contract["goal"], prompt)
        self.assertIn(self.contract["requirements"][0], prompt)
        self.assertIn(self.record["intent_requirement_graph"]["contract_sha256"], prompt)
        self.assertIn("資料の事実ではありません", prompt)
        self.assertIn("verifiedは事実支持だけ", prompt)
        self.assertIn("それって本当？", prompt)
        self.assertIn("Evidenceの原文・出典と照合", prompt)
        self.assertEqual(prompt.count("それって本当？"), 1)
        self.assertEqual(result["verdict"], "verified")
        self.assertEqual(performance["context_usage"]["status"], "observed")

    def test_truth_recheck_survives_workflow_prompt_override(self):
        with mock.patch.object(self.audit, "workflow_group_audit_prompt",
                               return_value="WORKFLOW_AUDIT_PROMPT"), \
             mock.patch.object(self.audit, "workflow_group_audit_schema",
                               return_value=self.audit.SCHEMA), \
             mock.patch.object(self.audit.LOCAL_HTTP_OPENER, "open",
                               side_effect=RuntimeError("stop before model")) as opened:
            with self.assertRaises(self.audit.audit_guard.AuditResponseError):
                self.audit.audit(
                    "synthetic-gemma", self.contract["question"], self.record["answer"],
                    [{"text": "会議室の正式名称は青空です。"}], 30,
                    graph_context={"workflow_groups": {"groups": ["synthetic"]}})
        opened.assert_called_once()
        payload = json.loads(opened.call_args.args[0].data)
        prompt = payload["messages"][1]["content"]
        self.assertIn("WORKFLOW_AUDIT_PROMPT", prompt)
        self.assertIn("それって本当？", prompt)
        self.assertEqual(prompt.count("それって本当？"), 1)


if __name__ == "__main__":
    unittest.main()
