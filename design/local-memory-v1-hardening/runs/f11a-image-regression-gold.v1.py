"""Unexecuted independent gold for F11a's image/text classification boundary.

Run only after root reviews this complete source and a frozen wrapper. Select
exactly METHODS, never inherited tests. Reuse F04 confinement/F05b write budget;
include this file and the existing image-test helper in budget.AUTHORS. Use an
already installed jsonschema runtime for native/schema-stream checks: no install,
model, network, GUI or external process is authorized. One batch: 30 s, 1 MiB
captured log and explicit fixture writes; source fixtures remain below 16 KiB.

Three methods, with fixed subcases. Producer metadata is compared to literal
gold, not to its own state helper. Partial visual discovery remains UNVERIFIED
at the intermediate gate, although applicable Search/discovery construction must
work. Current-v1 positive failures are regressions, not passed negative controls.
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
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[3]
HELPER_PATH = ROOT / "tests/test_local_embedded_visual_pipeline.py"
SPEC = importlib.util.spec_from_file_location("f11a_image_gold_helper", HELPER_PATH)
HELPER = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = HELPER
SPEC.loader.exec_module(HELPER)

import validate_intermediate_records as NATIVE  # noqa: E402
import validate_intermediate_records_streaming as STREAM  # noqa: E402
import validate_search_units as SEARCH_NATIVE  # noqa: E402
import validate_search_units_streaming as SEARCH_STREAM  # noqa: E402

METHODS = (
    "test_producer_visual_text_keeps_provisional_pipeline_and_metadata_scope",
    "test_native_output_cannot_opt_out_using_visual_labels_or_parent",
    "test_visual_state_parent_and_origin_forgery_remain_rejected",
)
SOURCE_STATE = {
    "version": "1.0", "cell_index": 1, "cell_type": "code",
    "content_origin": "cell_source", "source_json_pointer": "/cells/0",
    "reader_execution": "not_executed",
    "cell_execution_count": {"present": True, "value": None},
}
OUTPUT_STATE = {
    "version": "1.0", "cell_index": 1, "cell_type": "code",
    "content_origin": "saved_output", "source_json_pointer": "/cells/0/outputs/0",
    "reader_execution": "not_executed",
    "cell_execution_count": {"present": True, "value": None},
    "output_index": 1, "output_type": "display_data",
    "output_execution_count": {"present": False},
    "output_freshness": "unverified",
}
SCOPE = {
    "raw_text_binding": "not_verified", "complete_membership": "not_verified",
    "output_freshness": "not_verified",
}


class NotebookVisualBoundaryGold(HELPER.LocalEmbeddedVisualPipelineTests):
    def _probe(self, *, saved_text=False):
        with (
            mock.patch.object(HELPER.local_image_ocr, "extract", side_effect=lambda path:
                              HELPER.high_ocr_observation(path, "Notebook画像の確定OCR")),
            mock.patch.object(HELPER.local_visual_observation, "observe_path",
                              side_effect=HELPER.provisional_visual_observation),
        ):
            if not saved_text:
                return self._extract_notebook()
            source = self.source_root / "native-output.ipynb"
            payload = {
                "cells": [{"cell_type": "code", "execution_count": None,
                           "metadata": {}, "source": ["# inert native/visual boundary"],
                           "outputs": [{"output_type": "display_data", "metadata": {},
                                        "data": {"text/plain": "literal saved output",
                                                 "image/png": base64.b64encode(
                                                     HELPER.PNG_BYTES).decode("ascii")}}]}],
                "metadata": {}, "nbformat": 4, "nbformat_minor": 5,
            }
            raw = json.dumps(payload).encode("utf-8")
            self.assertLess(len(raw), 4096)
            source.write_bytes(raw)
            probe = HELPER.records.Probe(self.source_root, HELPER.RUN_AT, None, diagnostic=False)
            probe.extract(source)
            return probe, source

    def _only(self, values, predicate):
        selected = [item for item in values if predicate(item)]
        self.assertEqual(len(selected), 1)
        return selected[0]

    def _visual(self, probe):
        return self._only(probe.evidence, lambda item: item.get("provenance", {}).get(
            "extraction_method") == "local_vlm_visual_observation_provisional")

    def _copy_packet(self, probe):
        return types.SimpleNamespace(**{
            name: copy.deepcopy(getattr(probe, name)) for name in (
                "documents", "evidence", "relations", "extractor", "extractor_version", "run_at")
        })

    def _materialize(self, probe, label):
        directory = self.work / label
        HELPER.materialize_intermediate(probe, self.source_root, directory)
        return directory

    def _check_partial_reports(self, intermediate, *, native_count):
        # Literal fixture topology: source + optional saved output + image/OCR/VLM.
        record_count = 4 if native_count == 1 else 5
        expected = {
            "status": "UNVERIFIED",
            "counts": {"document": 1, "evidence": record_count, "relation": record_count},
            "notebook_metadata_binding": {
                "status": "unverified", "documents": 1,
                "checked_evidence": native_count, "unchecked_evidence": 0,
                "reason_codes": ["notebook_extraction_incomplete"], "scope": SCOPE,
            },
        }
        for name, function, options in (
            ("native", NATIVE.validate_report, {}),
            ("schema_stream", STREAM.validate_report, {"published_schema": True}),
            ("structural_stream", STREAM.validate_report, {"published_schema": False}),
        ):
            with self.subTest(path=name):
                self.assertEqual(function(intermediate, self.source_root, **options), expected)
        for function, options in (
            (NATIVE.validate, {}),
            (STREAM.validate, {"published_schema": True}),
            (STREAM.validate, {"published_schema": False}),
        ):
            with self.assertRaisesRegex(ValueError, "notebook_metadata_binding_unverified"):
                function(intermediate, self.source_root, **options)

    def _check_search(self, intermediate, search, *, native_count):
        expected = {"records": native_count + 2,
                    "counts_by_type": {"image_text_packet": 1, "notebook_cell": 1,
                                       "text_chunk": native_count}}
        for function in (SEARCH_NATIVE.validate, SEARCH_STREAM.validate):
            self.assertEqual(function(search, intermediate), expected)

    def _reject_reports(self, intermediate):
        for name, function, options in (
            ("native", NATIVE.validate_report, {}),
            ("schema_stream", STREAM.validate_report, {"published_schema": True}),
            ("structural_stream", STREAM.validate_report, {"published_schema": False}),
        ):
            with self.subTest(validator=name):
                with self.assertRaisesRegex(ValueError, "(?i)notebook|visual|parent|origin"):
                    function(intermediate, self.source_root, **options)

    def _search_with_rebound_input_hash(self, baseline, intermediate, label):
        # Preserve literal units; reseal only the modified intermediate-state link
        # so stale outer state hashes cannot masquerade as semantic rejection.
        output = self.work / label
        output.mkdir()
        (output / "search_units.jsonl").write_bytes((baseline / "search_units.jsonl").read_bytes())
        state = json.loads((baseline / "search-build-state.json").read_text(encoding="utf-8"))
        digest = hashlib.sha256((intermediate / "build-state.json").read_bytes()).hexdigest()
        self.assertEqual(len(state["source"]["intermediate_states"]), 1)
        state["source"]["intermediate_states"][0]["sha256"] = digest
        state["source"]["intermediate_state_sha256"] = digest
        (output / "search-build-state.json").write_text(json.dumps(state), encoding="utf-8")
        return output

    def test_producer_visual_text_keeps_provisional_pipeline_and_metadata_scope(self):
        probe, source = self._probe()
        source_before = source.read_bytes()
        cell = self._only(probe.evidence, lambda item: item["evidence_type"] == "notebook_cell")
        self.assertEqual(cell["native_properties"]["notebook_state"], SOURCE_STATE)
        visual = self._visual(probe)
        self.assertEqual(visual["evidence_type"], "text_block")
        self.assertNotIn("notebook_state", visual["native_properties"])
        self.assertEqual(probe.documents[0]["extraction"]["status"], "partial")
        # Original regression oracle, unchanged: image lineage, provisional
        # Search/semantic text and verified-graph exclusion must all survive.
        self._assert_pipeline(probe, source, expected_kind="notebook_embedded_image",
                              expected_locator={"notebook_cell_index": 1, "object_index": 1,
                                                "locator_text": "cell=1;output=1;output-image=1"})
        intermediate = self.work / "intermediate-notebook_embedded_image"
        search = self.work / "search-notebook_embedded_image"
        self._check_partial_reports(intermediate, native_count=1)
        self._check_search(intermediate, search, native_count=1)
        self.assertEqual(source.read_bytes(), source_before)

    def test_native_output_cannot_opt_out_using_visual_labels_or_parent(self):
        probe, source = self._probe(saved_text=True)
        source_before = source.read_bytes()
        output = self._only(probe.evidence, lambda item: item.get("native_properties", {}).get(
            "notebook_state", {}).get("content_origin") == "saved_output")
        self.assertEqual(output["native_properties"]["notebook_state"], OUTPUT_STATE)
        self.assertEqual(output["content"]["raw_text"], "literal saved output")
        image = self._only(probe.evidence, lambda item: item["evidence_type"] == "image")
        cell = self._only(probe.evidence, lambda item: item["evidence_type"] == "notebook_cell")
        visual = self._visual(probe)
        baseline = self._materialize(probe, "native-baseline")
        self._check_partial_reports(baseline, native_count=2)
        search = self.work / "native-baseline-search"
        HELPER.search_units.build(baseline, search, 500)
        self._check_search(baseline, search, native_count=2)
        for label, parent in (("label_only", None), ("real_image_parent", image["evidence_id"]),
                              ("wrong_parent_type", cell["evidence_id"])):
            with self.subTest(forgery=label):
                forged = self._copy_packet(probe)
                target = next(item for item in forged.evidence if item["evidence_id"] == output["evidence_id"])
                target["provenance"] = copy.deepcopy(visual["provenance"])
                target["native_properties"] = copy.deepcopy(visual["native_properties"])
                if parent is not None:
                    target["parent_evidence_id"] = parent
                else:
                    target.pop("parent_evidence_id", None)
                # Native saved-output body/location remain exactly canonical;
                # neither a visual label nor an existing image parent is enough.
                self.assertEqual(target["location"], output["location"])
                self.assertEqual(target["content"], output["content"])
                self._reject_reports(self._materialize(forged, "native-" + label))
        self.assertEqual(source.read_bytes(), source_before)

    def test_visual_state_parent_and_origin_forgery_remain_rejected(self):
        probe, source = self._probe()
        source_before = source.read_bytes()
        visual = self._visual(probe)
        cell = self._only(probe.evidence, lambda item: item["evidence_type"] == "notebook_cell")
        baseline = self._materialize(probe, "visual-baseline")
        self._check_partial_reports(baseline, native_count=1)
        search = self.work / "visual-baseline-search"
        HELPER.search_units.build(baseline, search, 500)
        self._check_search(baseline, search, native_count=1)
        for label in ("state_injection", "wrong_parent_type", "wrong_origin_hash"):
            with self.subTest(forgery=label):
                forged = self._copy_packet(probe)
                target = next(item for item in forged.evidence if item["evidence_id"] == visual["evidence_id"])
                if label == "state_injection":
                    target["native_properties"]["notebook_state"] = copy.deepcopy(SOURCE_STATE)
                elif label == "wrong_parent_type":
                    target["parent_evidence_id"] = cell["evidence_id"]
                else:
                    target["native_properties"]["visual_origin"]["source_sha256"] = "0" * 64
                intermediate = self._materialize(forged, "visual-" + label)
                self._reject_reports(intermediate)
                rebound = self._search_with_rebound_input_hash(search, intermediate, "search-" + label)
                for validator in (SEARCH_NATIVE.validate, SEARCH_STREAM.validate):
                    with self.assertRaisesRegex(ValueError, "(?i)notebook|visual|parent|origin"):
                        validator(rebound, intermediate)
        self.assertEqual(source.read_bytes(), source_before)


if __name__ == "__main__":
    raise SystemExit("Use a reviewed confined wrapper selecting exactly METHODS; do not discover inherited tests.")
