"""Actual managed per-document failure and continued text extraction.

Run only via the fixed guard runner; all files are owned tiny synthetic inputs.
No real password document, model, network or inference is used.
"""
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import build_intermediate_records as builder
import probe_intermediate_records as probe

RUN_AT = "2026-09-09T11:18:00+00:00"


class PasswordFailurePropagationTests(unittest.TestCase):
    def test_failed_document_has_no_evidence_and_next_text_succeeds(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "source"
            root.mkdir()
            output = base / "intermediate"
            output.mkdir()
            bad = root / "locked-or-damaged.docx"
            bad.write_bytes(b"synthetic non ZIP; not a real encrypted file")
            good = root / "normal.txt"
            good.write_text("受付手順の合成確認用テキスト", encoding="utf-8")
            original_probe = probe.Probe

            def suppressed(*args, **kwargs):
                kwargs["visual_observation_mode"] = "suppressed"
                return original_probe(*args, **kwargs)

            with mock.patch.object(builder, "Probe", side_effect=suppressed):
                failed, error = builder.process_file(
                    output, root, bad, RUN_AT, probe.digest_file(bad), ("not-for-log",))
                succeeded, normal_error = builder.process_file(
                    output, root, good, RUN_AT, probe.digest_file(good), ())
            self.assertEqual(failed["status"], "failed")
            self.assertIn("office_source_requires_human_review", str(error))
            evidence = output / failed["shards"]["evidence"]["relative_path"]
            self.assertEqual(evidence.read_text(), "")
            docs = output / failed["shards"]["documents"]["relative_path"]
            document = json.loads(docs.read_text())
            self.assertEqual(document["source"]["relative_path"], bad.name)
            self.assertIn("読めませんでした", document["extraction"]["errors"][0])
            self.assertNotIn("not-for-log", docs.read_text())
            self.assertIsNone(normal_error)
            self.assertEqual(succeeded["status"], "success")
            text = (output / succeeded["shards"]["evidence"]["relative_path"]).read_text()
            self.assertIn("受付手順", text)
            self.assertEqual(bad.read_bytes(), b"synthetic non ZIP; not a real encrypted file")

    def test_direct_probe_failure_is_explicit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "broken.xlsx"
            source.write_bytes(b"not an Office ZIP")
            reader = probe.Probe(root, RUN_AT, None, diagnostic=False,
                                 visual_observation_mode="suppressed")
            with self.assertRaisesRegex(ValueError, "office_source_requires_human_review"):
                reader.extract(source)
            self.assertFalse(reader.evidence)
