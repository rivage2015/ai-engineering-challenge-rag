"""Gap observations only: an OK result does not certify Notebook metadata."""
from __future__ import annotations

import copy
from pathlib import Path
import tempfile
import types
import unittest

import probe_intermediate_records as records
import validate_intermediate_records as memory_validator
import validate_intermediate_records_streaming as stream_validator

HERE = Path(__file__).resolve().parent
FIXTURE = HERE / "f11-next-fixture.v1.ipynb"
# The reused guarded dispatcher assigns run_tool; these tests never call it.
builder = types.SimpleNamespace()


class F11BindingGapObservations(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="f11a-binding-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.source = self.base / "source"
        self.source.mkdir()
        self.fixture_bytes = FIXTURE.read_bytes()
        self.assertLessEqual(len(self.fixture_bytes), 16384)
        self.path = self.source / "saved.ipynb"
        self.path.write_bytes(self.fixture_bytes)
        self.reader = records.Probe(
            self.source, "2026-09-09T00:00:00+00:00", None,
            diagnostic=False, visual_observation_mode="suppressed",
        )
        self.reader.extract(self.path)
        self.addCleanup(lambda: self.assertEqual(self.path.read_bytes(), self.fixture_bytes))
        self.addCleanup(lambda: self.assertEqual(FIXTURE.read_bytes(), self.fixture_bytes))

    def assert_current_validators_accept(self, name):
        output = self.base / name
        self.reader.write(output)
        expected = {"document": 1, "evidence": 6, "relation": 6}
        self.assertEqual(memory_validator.validate(output, self.source), expected)
        self.assertEqual(stream_validator.validate(output, self.source), expected)
        self.assertEqual(
            stream_validator.validate(output, self.source, published_schema=False), expected,
        )

    def test_missing_state_is_currently_accepted_with_source_root(self):
        self.assertTrue(all("notebook_state" not in e.get("native_properties", {}) for e in self.reader.evidence))
        self.assert_current_validators_accept("missing")

    def test_false_candidate_metadata_does_not_change_ids_or_trigger_source_check(self):
        before = copy.deepcopy(self.reader.evidence)
        for evidence in self.reader.evidence:
            evidence.setdefault("native_properties", {})["notebook_state"] = {
                "version": "1.0", "cell_index": 999,
                "cell_type": "invented", "content_origin": "saved_output",
                "source_json_pointer": "/cells/998/outputs/998",
                "reader_execution": "executed",
                "cell_execution_count": {"present": True, "value": 999},
                "output_freshness": "verified",
            }
        self.assertEqual([e["evidence_id"] for e in before], [e["evidence_id"] for e in self.reader.evidence])
        self.assertEqual([e["content"] for e in before], [e["content"] for e in self.reader.evidence])
        self.assert_current_validators_accept("false-state")


if __name__ == "__main__":
    unittest.main(verbosity=2)
