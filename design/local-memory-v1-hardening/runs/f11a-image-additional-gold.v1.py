"""Source-only independent F11a image repair gold; not run or accepted yet.

Select exactly METHODS on NotebookVisualAdditionalGold, never inherited tests.
Proposed runner: already installed ROOT/rag/.venv/bin/python -I -B -u, one fresh
f11a-image-additional-NNN run, reviewed F04 worker + F05b budget + supervisor,
30 seconds, 1 MiB captured log, 1 MiB cumulative explicit test Path writes,
16 KiB cumulative source fixtures per case. Add every BUDGET_AUTHORS path to
the wrapper's in-memory budget.AUTHORS; do not edit the frozen budget source.
No runner is created or authorized by this file. Root must read this full source
and the wrapper before execution. Retain every failure/skip/import-error log.

Only owned tmp contains source, Intermediate, Search or deferred image spool.
OCR and both VLM transports are mocks; active Paddle session lookup is null.
Producer extraction, image projection/spooling, Search construction and all
validators remain real. No new classifier API is called directly: an absent
planned API cannot masquerade as a semantic RED. Six bounded methods are not
all-format, raw-body/membership, model-quality, RSS or packaged-app acceptance.

Expected metadata, locator shapes and counts are literal. Fixture image hashes
and stable identity links are not expected metadata derived from the classifier.
The old gold (75573e3b...) and image helper (892658aa...) remain immutable.
Adapter / Agentic Audit: separate-context preparatory tests, not a formal report.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import types
from unittest import mock


HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
OLD_GOLD = HERE / "f11a-image-regression-gold.v1.py"
SPEC = importlib.util.spec_from_file_location("f11a_image_additional_base", OLD_GOLD)
BASE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = BASE
SPEC.loader.exec_module(BASE)
HELPER = BASE.HELPER

METHODS = (
    "test_deferred_first_image_child_survives_second_image",
    "test_actual_unlocated_transcript_keeps_literal_child_shape",
    "test_attachment_encoded_prefix_keeps_native_metadata_separate",
    "test_markdown_data_uri_prefix_keeps_existing_text_transform",
    "test_missing_and_cross_document_visual_parent_are_rejected",
    "test_non_notebook_origin_contract_keeps_exact_existing_behavior",
)
BUDGET_AUTHORS = (
    str(Path(__file__).resolve()), str(OLD_GOLD),
    str(ROOT / "tests/test_local_embedded_visual_pipeline.py"),
    str(ROOT / "distribution/macos-local-memory/tests/test_versioned_safe_index_e2e.py"),
)
ENCODED_PNG = base64.b64encode(HELPER.PNG_BYTES).decode("ascii")
PNG_SHA256 = hashlib.sha256(HELPER.PNG_BYTES).hexdigest()
VISUAL_METHOD = "local_vlm_visual_observation_provisional"
TRANSCRIPT_METHOD = "local_vlm_unlocated_transcript_provisional"
MARKDOWN_STATE = {
    "version": "1.0", "cell_index": 1, "cell_type": "markdown",
    "content_origin": "cell_source", "source_json_pointer": "/cells/0",
    "reader_execution": "not_executed",
    "cell_execution_count": {"present": False},
}


class NotebookVisualAdditionalGold(BASE.NotebookVisualBoundaryGold):
    def _code_cells(self, image_count=1):
        self.assertIn(image_count, (1, 2))
        return [{
            "cell_type": "code", "execution_count": None, "metadata": {},
            "source": ["# inert additional image fixture"],
            "outputs": [{"output_type": "display_data", "metadata": {},
                         "data": {"image/png": ENCODED_PNG}}
                        for _ in range(image_count)],
        }]

    def _extract_cells(self, name, cells, *, deferred=False, transcript=False):
        source = self.source_root / name
        raw = json.dumps({"cells": cells, "metadata": {}, "nbformat": 4,
                          "nbformat_minor": 5}).encode("utf-8")
        self.assertLess(len(raw), 4096)
        source.write_bytes(raw)

        def ocr(path):
            value = HELPER.high_ocr_observation(path, "literal mock image OCR")
            if transcript:
                value["unlocated_transcript"] = {
                    "text": "A=12\nB=done", "location_status": "unlocated",
                    "quality_tier": "provisional", "provisional_marker": "[暫定読取]",
                    "transcript_type": "whole_image_faithful_transcript",
                    "question_independent": True, "model": "gemma4:12b",
                    "model_digest": "a" * 64,
                    # Existing OCR protocol constant is a mock-input prerequisite,
                    # not the oracle for emitted Notebook state or child locator.
                    "prompt_sha256": HELPER.local_image_ocr.UNLOCATED_TRANSCRIPT_PROMPT_SHA256,
                    "runner": "ollama_loopback_chat", "host": "127.0.0.1",
                    "temperature": 0, "num_predict": 4096,
                }
            return value

        def observed_path(path, **kwargs):
            self.assertLessEqual(set(kwargs), {"expected_input_sha256", "timeout"})
            return HELPER.provisional_visual_observation(
                path, expected_input_sha256=kwargs.get("expected_input_sha256"))

        def observed_bytes(raw_image, **kwargs):
            self.assertEqual(raw_image, HELPER.PNG_BYTES)
            self.assertLessEqual(set(kwargs), {"expected_input_sha256", "timeout"})
            self.assertEqual(kwargs.get("expected_input_sha256"), PNG_SHA256)
            # Only the transport result is mocked. The bytes here came from the
            # real producer's private checked spool; no new image path is opened.
            fixture = types.SimpleNamespace(read_bytes=lambda: raw_image)
            return HELPER.provisional_visual_observation(
                fixture, expected_input_sha256=kwargs["expected_input_sha256"])

        probe = HELPER.records.Probe(
            self.source_root, HELPER.RUN_AT, None, diagnostic=False,
            visual_observation_mode="deferred_per_document" if deferred else "immediate")
        with (
            mock.patch.object(HELPER.local_image_ocr, "extract", side_effect=ocr) as read,
            mock.patch.object(HELPER.local_image_ocr, "active_paddle_session", return_value=None),
            mock.patch.object(HELPER.local_visual_observation, "observe_path",
                              side_effect=observed_path) as path_observe,
            mock.patch.object(HELPER.local_visual_observation, "observe_image",
                              side_effect=observed_bytes) as bytes_observe,
        ):
            probe.extract(source)
        self.assertEqual(source.read_bytes(), raw)
        return probe, source, (read.call_count, path_observe.call_count, bytes_observe.call_count)

    def _reports(self, intermediate, *, documents, records, native):
        expected = {
            "status": "UNVERIFIED",
            "counts": {"document": documents, "evidence": records, "relation": records},
            "notebook_metadata_binding": {
                "status": "unverified", "documents": documents,
                "checked_evidence": native, "unchecked_evidence": 0,
                "reason_codes": ["notebook_extraction_incomplete"], "scope": BASE.SCOPE,
            },
        }
        # No subTest around the positive baseline: a failure prevents subsequent
        # negatives from being mistaken for reached defensive controls.
        for function, options in (
            (BASE.NATIVE.validate_report, {}),
            (BASE.STREAM.validate_report, {"published_schema": True}),
            (BASE.STREAM.validate_report, {"published_schema": False}),
        ):
            self.assertEqual(function(intermediate, self.source_root, **options), expected)

    def _search_result(self, intermediate, search, expected_counts):
        expected = {"records": sum(expected_counts.values()), "counts_by_type": expected_counts}
        for validator in (BASE.SEARCH_NATIVE.validate, BASE.SEARCH_STREAM.validate):
            self.assertEqual(validator(search, intermediate), expected)
        return [json.loads(line) for line in (search / "search_units.jsonl").read_text(
            encoding="utf-8").splitlines() if line]

    def _bundle(self, probe, label, *, records, images, visual_chunks):
        intermediate = self._materialize(probe, label)
        self._reports(intermediate, documents=1, records=records, native=1)
        search = self.work / (label + "-search")
        HELPER.search_units.build(intermediate, search, 500)
        units = self._search_result(intermediate, search, {
            "notebook_cell": 1, "image_text_packet": images, "text_chunk": visual_chunks})
        for unit in units:
            if unit["unit_type"] == "text_chunk":
                self.assertEqual(unit.get("context"), {
                    "container_kind": "notebook_embedded_image",
                    "quality_tier": "provisional", "provisional_marker": "[暫定読取]"})
        return intermediate, search, units

    def _source_and_visual(self, probe, expected_state, parent_locator):
        cell = self._only(probe.evidence, lambda item: item["evidence_type"] == "notebook_cell")
        self.assertEqual(cell["native_properties"]["notebook_state"], expected_state)
        image = self._only(probe.evidence, lambda item: item["evidence_type"] == "image")
        self.assertEqual(image["location"], parent_locator)
        visual = self._visual(probe)
        self.assertNotIn("notebook_state", visual["native_properties"])
        self.assertEqual(visual["ordinal"], 2)  # one located OCR row, not object_index
        self.assertEqual(visual["location"], {
            "notebook_cell_index": 1, "image_object_index": 1, "object_index": 1,
            "locator_text": parent_locator["locator_text"] + ";visual_observation=whole_image"})
        self.assertEqual(visual["parent_evidence_id"], image["evidence_id"])
        self.assertEqual(visual["native_properties"]["visual_origin"], image["native_properties"]["visual_origin"])
        return cell, image, visual

    def test_deferred_first_image_child_survives_second_image(self):
        probe, source, calls = self._extract_cells(
            "deferred.ipynb", self._code_cells(2), deferred=True)
        before = source.read_bytes()
        self.assertEqual(calls, (2, 0, 2))
        self.assertEqual([item["evidence_type"] for item in probe.evidence], [
            "notebook_cell", "image", "ocr_line", "image", "ocr_line", "text_block", "text_block"])
        images = [item for item in probe.evidence if item["evidence_type"] == "image"]
        visuals = [item for item in probe.evidence if item.get("provenance", {}).get(
            "extraction_method") == VISUAL_METHOD]
        self.assertEqual(len(visuals), 2)
        self.assertEqual(probe.evidence[0]["native_properties"]["notebook_state"], BASE.SOURCE_STATE)
        for number, image, visual in zip((1, 2), images, visuals):
            locator = f"cell=1;output={number};output-image={number}"
            self.assertEqual(image["location"], {
                "notebook_cell_index": 1, "object_index": number, "locator_text": locator})
            self.assertEqual(visual["location"], {
                "notebook_cell_index": 1, "image_object_index": number, "object_index": 1,
                "locator_text": locator + ";visual_observation=whole_image"})
            self.assertEqual(visual["parent_evidence_id"], image["evidence_id"])
            self.assertEqual(visual["ordinal"], 2)
            self.assertNotIn("notebook_state", visual["native_properties"])
        self.assertGreater(probe.evidence.index(visuals[0]), probe.evidence.index(images[1]))
        _, _, units = self._bundle(probe, "deferred", records=7, images=2, visual_chunks=2)
        self.assertEqual({tuple(unit["source_evidence_ids"]) for unit in units
                          if unit["unit_type"] == "text_chunk"},
                         {(visual["evidence_id"],) for visual in visuals})
        self.assertEqual(source.read_bytes(), before)

    def test_actual_unlocated_transcript_keeps_literal_child_shape(self):
        probe, source, calls = self._extract_cells(
            "transcript.ipynb", self._code_cells(), transcript=True)
        before = source.read_bytes()
        self.assertEqual(calls, (1, 1, 0))
        self._source_and_visual(probe, BASE.SOURCE_STATE, {
            "notebook_cell_index": 1, "object_index": 1,
            "locator_text": "cell=1;output=1;output-image=1"})
        transcript = self._only(probe.evidence, lambda item: item.get("provenance", {}).get(
            "extraction_method") == TRANSCRIPT_METHOD)
        self.assertEqual(transcript["content"]["raw_text"], "[暫定読取]\nA=12\nB=done")
        self.assertEqual(transcript["ordinal"], 2)
        self.assertNotIn("geometry", transcript)
        self.assertNotIn("notebook_state", transcript["native_properties"])
        self.assertEqual(transcript["location"], {
            "notebook_cell_index": 1, "image_object_index": 1, "object_index": 2,
            "locator_text": "cell=1;output=1;output-image=1;location_status=unlocated;"
                            "source=image;chunk=1/1;characters=1-11"})
        keys = ("location_status", "transcript_type", "transcript_chunk_index",
                "transcript_chunk_count", "character_start", "character_end", "character_offset_basis")
        self.assertEqual({key: transcript["native_properties"][key] for key in keys}, {
            "location_status": "unlocated", "transcript_type": "whole_image_faithful_transcript",
            "transcript_chunk_index": 1, "transcript_chunk_count": 1,
            "character_start": 0, "character_end": 11, "character_offset_basis": "zero_based_half_open"})
        _, _, units = self._bundle(probe, "transcript", records=5, images=1, visual_chunks=2)
        unit = self._only(units, lambda value: value["source_evidence_ids"] == [transcript["evidence_id"]])
        self.assertEqual(unit["text"]["search_text"], "[暫定読取] A=12\n[暫定読取] B=done")
        self.assertEqual(unit["locator"], transcript["location"])
        self.assertEqual(source.read_bytes(), before)

    def test_attachment_encoded_prefix_keeps_native_metadata_separate(self):
        text = "![chart](attachment:chart%20space.png)"
        probe, source, calls = self._extract_cells("attachment-prefix.ipynb", [{
            "cell_type": "markdown", "metadata": {}, "source": [text],
            "attachments": {"chart space.png": {"image/png": ENCODED_PNG},
                            "unused.png": {"image/png": ENCODED_PNG}},
        }])
        before = source.read_bytes()
        self.assertEqual(calls, (1, 1, 0))
        cell, image, _ = self._source_and_visual(probe, MARKDOWN_STATE, {
            "notebook_cell_index": 1, "object_index": 1,
            "locator_text": "cell=1;attachment-image=1;attachment=chart%20space.png"})
        self.assertEqual(cell["content"]["raw_text"], text)
        self.assertEqual(image["native_properties"]["attachment_name"], "chart space.png")
        self._bundle(probe, "attachment", records=4, images=1, visual_chunks=1)
        self.assertEqual(source.read_bytes(), before)

    def test_markdown_data_uri_prefix_keeps_existing_text_transform(self):
        text = "![chart](data:image/png;base64," + ENCODED_PNG + ")"
        probe, source, calls = self._extract_cells("data-uri-prefix.ipynb", [{
            "cell_type": "markdown", "metadata": {}, "source": [text],
        }])
        before = source.read_bytes()
        self.assertEqual(calls, (1, 1, 0))
        cell, _, _ = self._source_and_visual(probe, MARKDOWN_STATE, {
            "notebook_cell_index": 1, "object_index": 1,
            "locator_text": "cell=1;source-image=1"})
        self.assertEqual(cell["content"]["raw_text"],
                         "![chart]([embedded image sha256=" + PNG_SHA256 + "])")
        self._bundle(probe, "data-uri", records=4, images=1, visual_chunks=1)
        self.assertEqual(source.read_bytes(), before)

    def _materialize_multiple(self, packet, label):
        # Tiny explicit two-document fixture. Correct per-document evidence
        # shards let real Search build select each document once; no false
        # duplicate/missing-state link is used as the parent-rejection oracle.
        output = self.work / label
        output.mkdir()
        for name in ("documents", "evidence", "relations"):
            HELPER.write_jsonl(output / (name + ".jsonl"), getattr(packet, name))
        entries = {}
        for number, document in enumerate(packet.documents, 1):
            relative = document["source"]["relative_path"]
            values = [item for item in packet.evidence if item["document_id"] == document["document_id"]]
            shard = output / f"evidence-{number}.jsonl"
            HELPER.write_jsonl(shard, values)
            raw = shard.read_bytes()
            entries[relative] = {
                "document_id": document["document_id"], "relative_path": relative,
                "source_sha256": document["source"]["sha256"],
                "status": document["extraction"]["status"],
                "shards": {"evidence": {"relative_path": shard.name,
                    "sha256": hashlib.sha256(raw).hexdigest(), "size_bytes": len(raw),
                    "record_count": len(values)}},
            }
        state = {
            "state_version": "1", "build_status": "complete",
            "source_root": str(self.source_root.resolve()), "extractor": packet.extractor,
            "extractor_version": packet.extractor_version, "run_at": packet.run_at,
            "input_paths": list(entries), "entries": entries,
            "totals": {"documents": 2, "evidence": 8, "relations": 8},
        }
        self.assertEqual((len(packet.documents), len(packet.evidence), len(packet.relations)), (2, 8, 8))
        (output / "build-state.json").write_text(json.dumps(state), encoding="utf-8")
        return output

    def test_missing_and_cross_document_visual_parent_are_rejected(self):
        first, first_source, first_calls = self._extract_cells("first-parent.ipynb", self._code_cells())
        second, second_source, second_calls = self._extract_cells("other-parent.ipynb", self._code_cells())
        self.assertEqual((first_calls, second_calls), ((1, 1, 0), (1, 1, 0)))
        source_before = {path: path.read_bytes() for path in (first_source, second_source)}
        packet = self._copy_packet(first)
        for name in ("documents", "evidence", "relations"):
            getattr(packet, name).extend(copy.deepcopy(getattr(second, name)))
        visual_id = self._visual(first)["evidence_id"]
        other_image = self._only(second.evidence, lambda item: item["evidence_type"] == "image")
        self.assertNotEqual(other_image["document_id"], first.documents[0]["document_id"])
        baseline = self._materialize_multiple(packet, "parents-baseline")
        self._reports(baseline, documents=2, records=8, native=2)
        search = self.work / "parents-baseline-search"
        HELPER.search_units.build(baseline, search, 500)
        self._search_result(baseline, search, {"notebook_cell": 2, "image_text_packet": 2, "text_chunk": 2})
        for label in ("missing", "cross-document"):
            with self.subTest(parent=label):
                forged = self._copy_packet(packet)
                target = next(item for item in forged.evidence if item["evidence_id"] == visual_id)
                original = copy.deepcopy(target)
                if label == "missing":
                    target.pop("parent_evidence_id")
                else:
                    target["parent_evidence_id"] = other_image["evidence_id"]
                self.assertEqual({k: v for k, v in target.items() if k != "parent_evidence_id"},
                                 {k: v for k, v in original.items() if k != "parent_evidence_id"})
                intermediate = self._materialize_multiple(forged, "parent-" + label)
                self._reject_reports(intermediate)
                rebound = self._search_with_rebound_input_hash(search, intermediate, "parent-search-" + label)
                for validator in (BASE.SEARCH_NATIVE.validate, BASE.SEARCH_STREAM.validate):
                    with self.assertRaisesRegex(ValueError, "(?i)notebook|visual|parent|document"):
                        validator(rebound, intermediate)
                with self.assertRaisesRegex(ValueError, "(?i)notebook|visual|parent|document"):
                    HELPER.search_units.build(intermediate, self.work / ("parent-build-" + label), 500)
        self.assertEqual({path: path.read_bytes() for path in source_before}, source_before)

    def test_non_notebook_origin_contract_keeps_exact_existing_behavior(self):
        with (
            mock.patch.object(HELPER.local_image_ocr, "extract", side_effect=lambda path:
                              HELPER.high_ocr_observation(path, "literal Office image OCR")),
            mock.patch.object(HELPER.local_image_ocr, "active_paddle_session", return_value=None),
            mock.patch.object(HELPER.local_visual_observation, "observe_path",
                              side_effect=HELPER.provisional_visual_observation),
        ):
            probe, source = self._extract_office_helper()
        before = source.read_bytes()
        self._assert_pipeline(probe, source, expected_kind="office_embedded_image",
                              expected_locator={"source_member": "word/media/image1.png", "object_index": 3})
        intermediate = self.work / "intermediate-office_embedded_image"
        expected = {
            "status": "PASS", "counts": {"document": 1, "evidence": 3, "relation": 3},
            "notebook_metadata_binding": {"status": "not_applicable", "documents": 0,
                "checked_evidence": 0, "unchecked_evidence": 0, "reason_codes": [], "scope": BASE.SCOPE},
        }
        for validator, options in (
            (BASE.NATIVE.validate_report, {}),
            (BASE.STREAM.validate_report, {"published_schema": True}),
            (BASE.STREAM.validate_report, {"published_schema": False}),
        ):
            self.assertEqual(validator(intermediate, self.source_root, **options), expected)
        self._search_result(intermediate, self.work / "search-office_embedded_image",
                            {"image_text_packet": 1, "text_chunk": 1})
        parent = self._only(probe.evidence, lambda item: item["evidence_type"] == "image")
        visual = self._visual(probe)
        origin_errors = BASE.SEARCH_NATIVE._visual_origin_errors
        self.assertEqual(origin_errors(parent, [visual], "office_embedded_image", probe.documents[0]), [])
        for label, expected_errors in (
            ("offline", ["parent image materialization is not offline"]),
            ("child-origin", ["child visual Evidence origin differs from parent image"]),
            ("embedded-digest", ["parent embedded image digest binding is invalid"]),
        ):
            with self.subTest(origin=label):
                changed_parent, changed_child = copy.deepcopy(parent), copy.deepcopy(visual)
                if label == "offline":
                    changed_parent["native_properties"]["visual_origin"]["materialization"]["external_network_used"] = True
                    changed_child["native_properties"]["visual_origin"] = copy.deepcopy(changed_parent["native_properties"]["visual_origin"])
                elif label == "child-origin":
                    changed_child["native_properties"]["visual_origin"]["source_sha256"] = "0" * 64
                else:
                    changed_parent["native_properties"]["embedded_sha256"] = "0" * 64
                self.assertEqual(origin_errors(changed_parent, [changed_child], "office_embedded_image",
                                               probe.documents[0]), expected_errors)
        self.assertEqual(source.read_bytes(), before)


if __name__ == "__main__":
    raise SystemExit("Root must review a confined wrapper selecting exactly METHODS; no discovery/direct run.")
