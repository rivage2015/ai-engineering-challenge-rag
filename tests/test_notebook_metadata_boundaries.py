"""Independent literal boundary tests for F11a; synthetic originals only."""
from __future__ import annotations

import copy
from pathlib import Path
import tempfile
import types
import unittest

import probe_intermediate_records as records
import build_search_units as search
import validate_intermediate_records as native
import validate_intermediate_records_streaming as streaming

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "design/local-memory-v1-hardening/runs/f11-next-fixture.v1.ipynb"
builder = types.SimpleNamespace()  # reviewed guard's dispatcher seam; unused here


def literal_states():
    # Independent hand-written facts, not the producer's metadata helper.
    common = {"version": "1.0", "reader_execution": "not_executed"}
    first = dict(common, cell_index=1, cell_type="code", content_origin="cell_source",
                 source_json_pointer="/cells/0", cell_execution_count={"present": True, "value": 7})
    first_output = dict(common, cell_index=1, cell_type="code", content_origin="saved_output",
                        source_json_pointer="/cells/0/outputs/0", cell_execution_count={"present": True, "value": 7},
                        output_index=1, output_type="execute_result", output_execution_count={"present": True, "value": 6},
                        output_freshness="unverified")
    second = dict(common, cell_index=2, cell_type="code", content_origin="cell_source",
                  source_json_pointer="/cells/1", cell_execution_count={"present": True, "value": 2})
    second_output = dict(common, cell_index=2, cell_type="code", content_origin="saved_output",
                         source_json_pointer="/cells/1/outputs/0", cell_execution_count={"present": True, "value": 2},
                         output_index=1, output_type="stream", output_execution_count={"present": False},
                         output_freshness="unverified")
    empty_source_output = dict(common, cell_index=3, cell_type="code", content_origin="saved_output",
                              source_json_pointer="/cells/2/outputs/0", cell_execution_count={"present": True, "value": None},
                              output_index=1, output_type="display_data", output_execution_count={"present": False},
                              output_freshness="unverified")
    markdown = dict(common, cell_index=4, cell_type="markdown", content_origin="cell_source",
                    source_json_pointer="/cells/3", cell_execution_count={"present": False})
    return [first, first_output, second, second_output, empty_source_output, markdown]


class NotebookMetadataBoundaryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="f11a-root-boundary-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.source = self.base / "source"
        self.source.mkdir()
        self.raw = FIXTURE.read_bytes()
        self.assertLess(len(self.raw), 16384)
        self.path = self.source / "saved.ipynb"
        self.path.write_text(self.raw.decode("utf-8"), encoding="utf-8")
        self.reader = records.Probe(self.source, "2026-09-09T00:00:00+00:00", None,
                                    diagnostic=False, visual_observation_mode="suppressed")
        self.reader.extract(self.path)
        self.assertEqual(len(self.reader.evidence), 6)
        self.assertEqual([e["location"]["locator_text"] for e in self.reader.evidence],
                         ["cell=1", "cell=1;output=1", "cell=2", "cell=2;output=1", "cell=3;output=1", "cell=4"])
        for evidence, state in zip(self.reader.evidence, literal_states()):
            evidence.setdefault("native_properties", {})["notebook_state"] = state
        self.addCleanup(lambda: self.assertEqual(self.path.read_bytes(), self.raw))
        self.addCleanup(lambda: self.assertEqual(FIXTURE.read_bytes(), self.raw))

    def calls(self):
        return (("native", native.validate, {}), ("stream", streaming.validate, {}),
                ("structural-stream", streaming.validate, {"published_schema": False}))

    def good_output(self):
        output = self.base / "good"
        self.reader.write(output)
        for label, validate, kwargs in self.calls():
            with self.subTest(control=label):
                self.assertEqual(validate(output, self.source, **kwargs), {"document": 1, "evidence": 6, "relation": 6})
        return output

    def test_missing_metadata_rejected_with_originals(self):
        self.good_output()
        for evidence in self.reader.evidence:
            evidence["native_properties"].pop("notebook_state")
        output = self.base / "missing"
        self.reader.write(output)
        for label, validate, kwargs in self.calls():
            with self.subTest(boundary=label):
                with self.assertRaises(ValueError):
                    validate(output, self.source, **kwargs)

    def test_well_typed_false_count_rejected_without_id_change(self):
        self.good_output()
        before = copy.deepcopy(self.reader.evidence)
        self.reader.evidence[1]["native_properties"]["notebook_state"]["output_execution_count"]["value"] = 5
        self.assertEqual([e["evidence_id"] for e in before], [e["evidence_id"] for e in self.reader.evidence])
        self.assertEqual([e["content"] for e in before], [e["content"] for e in self.reader.evidence])
        output = self.base / "false-count"
        self.reader.write(output)
        for label, validate, kwargs in self.calls():
            with self.subTest(boundary=label):
                with self.assertRaises(ValueError):
                    validate(output, self.source, **kwargs)

    def test_counts_only_wrapper_cannot_pass_without_originals(self):
        output = self.good_output()
        for label, validate, kwargs in self.calls():
            with self.subTest(boundary=label):
                with self.assertRaises(ValueError):
                    validate(output, **kwargs)

    def test_direct_search_preserves_all_six_literal_states(self):
        derived = []
        deriver = search.DocumentDeriver(self.reader.documents[0]["document_id"],
                                         "2026-09-09T00:00:00+00:00", derived.append, 1000)
        for evidence in self.reader.evidence:
            deriver.consume(evidence)
        deriver.finish()
        self.assertEqual(len(derived), 6)
        self.assertEqual([u.get("context", {}).get("notebook_state") for u in derived], literal_states())


if __name__ == "__main__":
    unittest.main(verbosity=2)
