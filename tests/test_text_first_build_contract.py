"""Keep the opt-in reader experiment isolated from existing answer generations."""
from __future__ import annotations

import contextlib
import copy
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import adapt_layer1_to_local_memory as adapter
import build_intermediate_records as builder
import validate_intermediate_records_streaming as streaming
import validate_intermediate_records as validator


def fixture_fingerprint(policy="full"):
    payload = {"reading_policy": policy, "fixture": "text-first-contract"}
    return {"version": "1", "payload": payload,
            "sha256": hashlib.sha256(builder.canonical_json(payload).encode()).hexdigest()}


class TextFirstBuildContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="text-first-contract-")
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.root = self.base / "source"
        self.root.mkdir()
        (self.root / "sample.txt").write_text("Open on Tuesday. Closed on holidays.\n", encoding="utf-8")
        self.output = self.base / "records"

    def build(self, policy="full", *extra, fingerprint_policy=None):
        argv = ["builder", "--root", str(self.root), "--out", str(self.output),
                "--reading-policy", policy, *extra]
        captured = io.StringIO()
        with (mock.patch.object(sys, "argv", argv),
              mock.patch.object(builder, "processing_fingerprint",
                                return_value=fixture_fingerprint(fingerprint_policy or policy)),
              contextlib.redirect_stdout(captured)):
            builder.main()
        return json.loads(captured.getvalue())

    def documents(self):
        return [json.loads(x) for x in (self.output / "documents.jsonl").read_text().splitlines()]

    def test_default_still_adapts(self):
        self.assertEqual(self.build()["reading_policy"], "full")
        self.assertNotIn("reading_policy", self.documents()[0]["extraction"])
        adapter.adapt(self.output, self.root.resolve(), self.base / "adapted")

    def test_text_first_no_images_still_not_answer_ready(self):
        result = self.build("text_first_v1")
        self.assertEqual(result["build_status"], "complete")
        extraction = self.documents()[0]["extraction"]
        self.assertEqual(extraction["visual_coverage"], {"status": "none_pending", "pending": []})
        streaming.validate(self.output, self.root.resolve(), published_schema=False)
        with self.assertRaisesRegex(ValueError, "R2b required"):
            adapter.adapt(self.output, self.root.resolve(), self.base / "adapted")
        self.assertFalse((self.base / "adapted" / "semantic-evidence.jsonl").exists())

    def test_document_contract_alone_blocks_adapter(self):
        self.build("text_first_v1", fingerprint_policy="full")
        with self.assertRaisesRegex(ValueError, "R2b required"):
            adapter.adapt(self.output, self.root.resolve(), self.base / "adapted")

    def test_fingerprint_contract_alone_blocks_adapter(self):
        self.build("full", fingerprint_policy="text_first_v1")
        with self.assertRaisesRegex(ValueError, "R2b required"):
            adapter.adapt(self.output, self.root.resolve(), self.base / "adapted")

    def test_unknown_policy_in_fingerprint_is_not_full(self):
        self.build("full", fingerprint_policy="unrecognized_policy")
        with self.assertRaisesRegex(ValueError, "R2b required"):
            adapter.adapt(self.output, self.root.resolve(), self.base / "adapted")

    def test_same_policy_reuses_but_changed_policy_reextracts(self):
        self.build()
        self.assertEqual(self.build("full", "--resume")["skipped_now"], 1)
        changed = self.build("text_first_v1", "--resume")
        self.assertEqual(changed["processed_now"], 1)
        self.assertEqual(changed["skipped_now"], 0)
        self.assertEqual(self.documents()[0]["extraction"]["reading_policy"], "text_first_v1")
        self.assertEqual(self.build("text_first_v1", "--resume")["skipped_now"], 1)
        restored = self.build("full", "--resume")
        self.assertEqual(restored["processed_now"], 1)
        self.assertNotIn("reading_policy", self.documents()[0]["extraction"])

    def test_partial_resume_cannot_publish_old_aggregate(self):
        (self.root / "second.txt").write_text("Second source", encoding="utf-8")
        self.build()
        result = self.build("text_first_v1", "--resume", "--max-files", "1")
        self.assertEqual(result["build_status"], "in_progress")
        with self.assertRaisesRegex(ValueError, "terminal state"):
            adapter.adapt(self.output, self.root.resolve(), self.base / "adapted")
        self.assertEqual(self.build("text_first_v1", "--resume")["build_status"], "complete")

    def test_unknown_cli_policy_rejected_without_output(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            self.build("typo")
        self.assertFalse(self.output.exists())

    def test_fingerprint_binds_policy_and_skips_unused_vision_probes(self):
        with (mock.patch.object(builder, "_code_identity", return_value={}),
              mock.patch.object(builder, "_reader_distribution_identities", return_value={}),
              mock.patch.object(builder, "_pdfkit_jxa_backend_identity", return_value={}),
              mock.patch.object(builder, "_fixed_ocr_runtime_identity", return_value={}) as ocr,
              mock.patch.object(builder, "_paddle_runtime_identity", return_value={}) as paddle,
              mock.patch.object(builder, "_local_vlm_identities", return_value=[]) as vlm):
            full = builder.processing_fingerprint()
            ocr.reset_mock(); paddle.reset_mock(); vlm.reset_mock()
            native = builder.processing_fingerprint("text_first_v1")
            self.assertNotEqual(full["sha256"], native["sha256"])
            self.assertEqual(native["payload"]["reading_policy"], "text_first_v1")
            ocr.assert_not_called(); paddle.assert_not_called(); vlm.assert_not_called()
        with self.assertRaisesRegex(ValueError, "reading_policy"):
            builder.processing_fingerprint("unknown")

    def test_failed_shard_drops_discarded_pending_refs_not_failure(self):
        def failing_extract(probe, path):
            doc = probe.add_document(path, "synthetic-failing-reader")
            doc["extraction"]["visual_coverage"] = {
                "status": "pending", "pending": [{"evidence_id": "ev_" + "a" * 64}],
            }
            raise RuntimeError("native parsing failed after earlier records")
        with mock.patch.object(builder.Probe, "extract", failing_extract):
            result = self.build("text_first_v1")
        self.assertEqual(result["build_status"], "complete_with_failures")
        doc = self.documents()[0]
        self.assertEqual(doc["extraction"]["status"], "failed")
        self.assertIn("native parsing failed", doc["extraction"]["errors"][0])
        self.assertEqual(doc["extraction"]["visual_coverage"]["pending"], [])
        streaming.validate(self.output, self.root.resolve(), published_schema=False)


class VisualCoverageContractTests(unittest.TestCase):
    def setUp(self):
        self.evidence = {
            "evidence_id": "ev_" + "e" * 64, "document_id": "doc_" + "d" * 64,
            "evidence_type": "image", "location": {"sheet_name": "sample", "object_index": 1},
            "native_properties": {"embedded_sha256": "b" * 64},
        }
        self.document = {
            "document_id": self.evidence["document_id"],
            "source": {"sha256": "a" * 64, "extension": "xlsx"},
            "extraction": {"status": "partial", "reading_policy": "text_first_v1",
                           "visual_coverage": {"status": "pending", "pending": [{
                               "evidence_id": self.evidence["evidence_id"],
                               "source_sha256": "a" * 64, "image_sha256": "b" * 64,
                               "location": copy.deepcopy(self.evidence["location"]),
                               "kind": "embedded_image", "reason": "deferred_by_reading_policy",
                           }]}},
        }

    def errors(self):
        return (validator.visual_coverage_shape_errors(self.document, "sample")
                + validator.visual_coverage_binding_errors(self.document, [self.evidence], "sample"))

    def test_valid_binding(self):
        self.assertEqual(self.errors(), [])

    def test_unread_reference_cannot_be_omitted(self):
        self.document["extraction"]["visual_coverage"] = {"status": "none_pending", "pending": []}
        self.assertTrue(self.errors())

    def test_shape_and_binding_mutations_rejected(self):
        original = copy.deepcopy(self.document)
        changes = [
            lambda d: d["extraction"].update(reading_policy="unknown"),
            lambda d: d["extraction"].pop("reading_policy"),
            lambda d: d["extraction"].pop("visual_coverage"),
            lambda d: d["extraction"].update(status="success"),
            lambda d: d["extraction"]["visual_coverage"].update(status="none_pending"),
            lambda d: d["extraction"]["visual_coverage"].update(pending=None),
            lambda d: d["extraction"]["visual_coverage"].update(extra=True),
        ]
        for change in changes:
            with self.subTest(change=change):
                self.document = copy.deepcopy(original)
                change(self.document)
                self.assertTrue(self.errors())
        for key, value in [("source_sha256", "c" * 64), ("image_sha256", "c" * 64),
                           ("evidence_id", "ev_" + "f" * 64), ("kind", "pdf_page"),
                           ("kind", "unknown"), ("location", {"object_index": True}),
                           ("location", {"sheet_name": "other", "object_index": 1}),
                           ("reason", "read_successfully")]:
            with self.subTest(key=key, value=value):
                self.document = copy.deepcopy(original)
                self.document["extraction"]["visual_coverage"]["pending"][0][key] = value
                self.assertTrue(self.errors())

    def test_duplicate_and_foreign_references_rejected(self):
        pending = self.document["extraction"]["visual_coverage"]["pending"]
        pending.append(copy.deepcopy(pending[0]))
        self.assertTrue(self.errors())
        pending.pop()
        self.evidence["document_id"] = "doc_" + "f" * 64
        self.assertTrue(self.errors())

    def test_pdf_must_not_claim_rendered_image(self):
        self.document["source"]["extension"] = "pdf"
        item = self.document["extraction"]["visual_coverage"]["pending"][0]
        item.update(kind="pdf_page", location={"page_number": 1})
        item.pop("image_sha256")
        self.evidence.update(evidence_type="page", location={"page_number": 1}, native_properties={})
        self.assertEqual(self.errors(), [])
        item["image_sha256"] = "b" * 64
        self.assertTrue(self.errors())


if __name__ == "__main__":
    unittest.main()
