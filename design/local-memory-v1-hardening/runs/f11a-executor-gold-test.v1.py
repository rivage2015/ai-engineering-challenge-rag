"""Literal F11a metadata-only contract; execute only in the reviewed guard.

The historical fixture is fictional inert text. Expected state objects below
are hand-written, never produced by the implementation being checked. Source
text correspondence/full membership/freshness are intentionally not certified.
"""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import probe_intermediate_records as records
import build_search_units as search_builder
import validate_intermediate_records as native_validator
import validate_intermediate_records_streaming as stream_validator


RUN_AT = "2026-09-09T00:00:00+00:00"
FIXTURE = ROOT / "design/local-memory-v1-hardening/runs/f11-next-fixture.v1.ipynb"
FIXTURE_SHA256 = "123ff664e24f2cea7c3caac841e139f8574e7b2f9667701dd16e63f5086d80ad"
EXPECTED_COUNTS = {"document": 1, "evidence": 6, "relation": 6}
GOLD_STATES = [
    {
        "version": "1.0", "cell_index": 1, "cell_type": "code",
        "content_origin": "cell_source", "source_json_pointer": "/cells/0",
        "reader_execution": "not_executed",
        "cell_execution_count": {"present": True, "value": 7},
    },
    {
        "version": "1.0", "cell_index": 1, "cell_type": "code",
        "content_origin": "saved_output", "source_json_pointer": "/cells/0/outputs/0",
        "reader_execution": "not_executed",
        "cell_execution_count": {"present": True, "value": 7},
        "output_index": 1, "output_type": "execute_result",
        "output_execution_count": {"present": True, "value": 6},
        "output_freshness": "unverified",
    },
    {
        "version": "1.0", "cell_index": 2, "cell_type": "code",
        "content_origin": "cell_source", "source_json_pointer": "/cells/1",
        "reader_execution": "not_executed",
        "cell_execution_count": {"present": True, "value": 2},
    },
    {
        "version": "1.0", "cell_index": 2, "cell_type": "code",
        "content_origin": "saved_output", "source_json_pointer": "/cells/1/outputs/0",
        "reader_execution": "not_executed",
        "cell_execution_count": {"present": True, "value": 2},
        "output_index": 1, "output_type": "stream",
        "output_execution_count": {"present": False},
        "output_freshness": "unverified",
    },
    {
        "version": "1.0", "cell_index": 3, "cell_type": "code",
        "content_origin": "saved_output", "source_json_pointer": "/cells/2/outputs/0",
        "reader_execution": "not_executed",
        "cell_execution_count": {"present": True, "value": None},
        "output_index": 1, "output_type": "display_data",
        "output_execution_count": {"present": False},
        "output_freshness": "unverified",
    },
    {
        "version": "1.0", "cell_index": 4, "cell_type": "markdown",
        "content_origin": "cell_source", "source_json_pointer": "/cells/3",
        "reader_execution": "not_executed",
        "cell_execution_count": {"present": False},
    },
]
GOLD_TEXTS = [
    "raise RuntimeError('F11 source must never execute')\n",
    "saved value A", "print('saved value B')\n", "saved value B\n",
    "saved value C", "F11 synthetic note",
]
GOLD_LOCATIONS = [
    {"notebook_cell_index": 1, "locator_text": "cell=1"},
    {"notebook_cell_index": 1, "object_index": 1, "locator_text": "cell=1;output=1"},
    {"notebook_cell_index": 2, "locator_text": "cell=2"},
    {"notebook_cell_index": 2, "object_index": 1, "locator_text": "cell=2;output=1"},
    {"notebook_cell_index": 3, "object_index": 1, "locator_text": "cell=3;output=1"},
    {"notebook_cell_index": 4, "locator_text": "cell=4"},
]
GOLD_REPORT = {
    "status": "PASS", "counts": {"document": 1, "evidence": 6, "relation": 6},
    "notebook_metadata_binding": {
        "status": "verified", "documents": 1, "checked_evidence": 6,
        "unchecked_evidence": 0, "reason_codes": [],
        "scope": {"raw_text_binding": "not_verified",
                  "complete_membership": "not_verified",
                  "output_freshness": "not_verified"},
    },
}


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


class NotebookMetadataBindingTests(unittest.TestCase):
    """Initial existing-API semantic RED batch (G1/G2/G3/G6)."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="f11a-metadata-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.source = self.base / "source"
        self.source.mkdir()
        self.fixture_bytes = FIXTURE.read_bytes()
        self.assertEqual(hashlib.sha256(self.fixture_bytes).hexdigest(), FIXTURE_SHA256)
        self.assertLessEqual(len(self.fixture_bytes), 16384)
        self.path = self.source / "saved.ipynb"
        self.path.write_bytes(self.fixture_bytes)
        self.probe = records.Probe(
            self.source, RUN_AT, None, diagnostic=False,
            visual_observation_mode="suppressed",
        )
        self.probe.extract(self.path)
        self.addCleanup(lambda: self.assertEqual(self.path.read_bytes(), self.fixture_bytes))
        self.addCleanup(lambda: self.assertEqual(FIXTURE.read_bytes(), self.fixture_bytes))

    def install_literal_states(self):
        self.assertEqual(len(self.probe.evidence), 6)
        for evidence, expected in zip(self.probe.evidence, GOLD_STATES):
            evidence.setdefault("native_properties", {})["notebook_state"] = copy.deepcopy(expected)

    def validate_existing(self, mode, directory):
        if mode == "native":
            return native_validator.validate(directory, self.source)
        return stream_validator.validate(
            directory, self.source, published_schema=(mode == "schema"),
        )

    def assert_metadata_rejected(self, mode, mutation):
        self.install_literal_states()
        directory = self.base / "intermediate"
        self.probe.write(directory)
        self.assertEqual(self.validate_existing(mode, directory), EXPECTED_COUNTS)
        before_ids = [item["evidence_id"] for item in self.probe.evidence]
        before_content = copy.deepcopy([item["content"] for item in self.probe.evidence])
        state = self.probe.evidence[0]["native_properties"]
        if mutation == "missing":
            del state["notebook_state"]
        else:
            # Still schema-valid; counts do not participate in Evidence IDs.
            state["notebook_state"]["cell_execution_count"]["value"] = 17
        self.probe.write(directory)
        self.assertEqual([item["evidence_id"] for item in self.probe.evidence], before_ids)
        self.assertEqual([item["content"] for item in self.probe.evidence], before_content)
        with self.assertRaisesRegex(ValueError, "notebook"):
            self.validate_existing(mode, directory)

    def test_g1_probe_emits_literal_six_states_and_preserves_source(self):
        self.assertEqual(len(self.probe.documents), 1)
        self.assertEqual(len(self.probe.evidence), 6)
        self.assertEqual(len(self.probe.relations), 6)
        self.assertEqual([item["content"]["raw_text"] for item in self.probe.evidence], GOLD_TEXTS)
        self.assertEqual([item["location"] for item in self.probe.evidence], GOLD_LOCATIONS)
        self.assertEqual([item["ordinal"] for item in self.probe.evidence], [1, 1, 2, 1, 1, 4])
        self.assertEqual(self.probe.documents[0]["source"]["sha256"], FIXTURE_SHA256)
        expected_content_hashes = [hashlib.sha256(canonical({"raw_text": text}).encode("utf-8")).hexdigest() for text in GOLD_TEXTS]
        self.assertEqual([item["content"]["sha256"] for item in self.probe.evidence], expected_content_hashes)
        actual = [item.get("native_properties", {}).get("notebook_state") for item in self.probe.evidence]
        self.assertEqual(canonical(actual), canonical(GOLD_STATES))

    def test_g2_missing_state_rejected_native(self):
        self.assert_metadata_rejected("native", "missing")

    def test_g2_missing_state_rejected_stream_schema(self):
        self.assert_metadata_rejected("schema", "missing")

    def test_g2_missing_state_rejected_stream_structural(self):
        self.assert_metadata_rejected("structural", "missing")

    def test_g2_shape_valid_wrong_count_rejected_native(self):
        self.assert_metadata_rejected("native", "wrong_count")

    def test_g2_shape_valid_wrong_count_rejected_stream_schema(self):
        self.assert_metadata_rejected("schema", "wrong_count")

    def test_g2_shape_valid_wrong_count_rejected_stream_structural(self):
        self.assert_metadata_rejected("structural", "wrong_count")

    def test_g3_direct_search_copies_all_six_literal_states(self):
        self.install_literal_states()
        before = copy.deepcopy(self.probe.evidence)
        units = []
        deriver = search_builder.DocumentDeriver(
            self.probe.documents[0]["document_id"], RUN_AT, units.append, 1200,
        )
        for evidence in self.probe.evidence:
            deriver.consume(evidence)
        self.assertEqual(deriver.finish(), {"notebook_cell": 3, "text_chunk": 3})
        self.assertEqual(len(units), 6)
        self.assertEqual([item["text"]["search_text"] for item in units], [text.strip() for text in GOLD_TEXTS])
        self.assertEqual([item["locator"] for item in units], GOLD_LOCATIONS)
        self.assertEqual([item["source_evidence_ids"] for item in units], [[item["evidence_id"]] for item in before])
        self.assertEqual(self.probe.evidence, before)
        actual = [item.get("context", {}).get("notebook_state") for item in units]
        self.assertEqual(canonical(actual), canonical(GOLD_STATES))

    def test_g6_non_notebook_retains_exact_old_counts(self):
        path = self.source / "plain.txt"
        path.write_text("fictional plain text\n", encoding="utf-8")
        probe = records.Probe(self.source, RUN_AT, None, diagnostic=False,
                              visual_observation_mode="suppressed")
        probe.extract(path)
        directory = self.base / "plain-intermediate"
        probe.write(directory)
        expected = {"document": 1, "evidence": 1, "relation": 1}
        self.assertEqual(native_validator.validate(directory), expected)
        self.assertEqual(stream_validator.validate(directory), expected)
        self.assertEqual(stream_validator.validate(directory, published_schema=False), expected)


if __name__ == "__main__":
    unittest.main(verbosity=2)
