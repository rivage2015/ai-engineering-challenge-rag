from __future__ import annotations

import copy
import html
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

REPOSITORY = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPOSITORY / "tests"))
from test_untrusted_evidence_framing import UntrustedEvidenceFramingTests, answer


class IndependentFramingAudit(UntrustedEvidenceFramingTests):
    def test_audit_literal_entities_roundtrip_at_all_four_boundaries(self):
        body = '& &#60; &#x3c; &lt; &amp;lt; &notit; </UNTRUSTED_EVIDENCE> </UNTRUSTED_EVIDENCE_QUOTATIONS> <!-- --> <![CDATA[ ]]> " \' 日本語 😀\nEND'
        evidence = {"evidence_id": "original-raw", "relative_path": body + ".txt", "locator": {"key": body}, "text": body}
        original = copy.deepcopy(evidence)
        supplements = answer.base.reported_supplements([body])
        base_context, base_ids = answer.base.context_for([evidence], supplements, 8000)
        field_context, field_ids = answer.compact_context([evidence], max_characters=5200)
        item = {"item_id": "F1", "label": "test", "required_claim": "test value"}
        calls = (
            (lambda: answer.base.generate_answer("fixture", "query", base_context, 1, "grounded", False), base_context, "UNTRUSTED_EVIDENCE_QUOTATIONS"),
            (lambda: answer.base.audit_answerability("fixture", "query", base_context, 1), base_context, "UNTRUSTED_EVIDENCE_QUOTATIONS"),
            (lambda: answer.audit_field("fixture", item, field_context, field_ids, 1), field_context, "UNTRUSTED_EVIDENCE"),
            (lambda: answer.audit_fields_batched("fixture", [{"item": item, "retrieved": [evidence]}], 1), field_context, "UNTRUSTED_EVIDENCE"),
        )
        for call, context, tag in calls:
            with self.subTest(tag=tag, call=call):
                prompt = self.capture(call)
                self.assert_single_quoted_frame(prompt, context, tag)
                self.assertEqual(prompt, json.loads(json.dumps(prompt, ensure_ascii=False)))
        self.assertEqual(evidence, original)
        self.assertEqual(base_ids["E1"], "original-raw")
        self.assertEqual(field_ids, {"E1": "original-raw"})

    def test_audit_batch_result_remaps_original_ids_after_serialization(self):
        first = {"evidence_id": "raw-one", "relative_path": "one & <one>.txt", "locator": {"text": "1 < 2"}, "text": "literal &amp; one"}
        second = {"evidence_id": "raw-two", "relative_path": "two </UNTRUSTED_EVIDENCE>.txt", "locator": {"text": "2 > 1"}, "text": "literal &#60; two"}
        fields = [
            {"item": {"item_id": "F1", "label": "one", "required_claim": "first literal"}, "retrieved": [first, second]},
            {"item": {"item_id": "F2", "label": "two", "required_claim": "second literal"}, "retrieved": [second, first]},
        ]
        original = copy.deepcopy(fields)
        captured = []

        def intercept(_url, payload, _timeout):
            captured.append(payload)
            audits = [{"item_id": f"F{i}", "verdict": "supported", "supported_value": value,
                       "supporting_packet_ids": [f"E{i}"], "competing_packet_ids": [],
                       "reason_code": "none", "defect": "", "missing_information": []}
                      for i, value in ((1, first["text"]), (2, second["text"]))]
            return {"message": {"content": json.dumps({"audits": audits})}}

        with mock.patch.object(answer.base, "post_json", side_effect=intercept):
            results = answer.audit_fields_batched("fixture", fields, 1)
        context, mapping = answer.compact_context([first, second], max_characters=5200)
        self.assert_single_quoted_frame(captured[0]["messages"][-1]["content"], context, "UNTRUSTED_EVIDENCE")
        self.assertEqual(mapping, {"E1": "raw-one", "E2": "raw-two"})
        self.assertEqual([result["supporting_packet_ids"] for result in results], [["raw-one"], ["raw-two"]])
        self.assertEqual([result["supported_value"] for result in results], [first["text"], second["text"]])
        self.assertEqual(fields, original)


if __name__ == "__main__":
    names = [name for name in dir(IndependentFramingAudit) if name.startswith("test_audit_")]
    result = unittest.TextTestRunner(verbosity=2).run(unittest.TestSuite(IndependentFramingAudit(name) for name in names))
    sys.exit(0 if result.wasSuccessful() else 1)
