"""Immutable F11 gap observations; green means gap reproduced, not repaired.

Preflight: only two stdlib-only production modules are imported. Source is the
adjacent fixed 1.4 KiB synthetic Notebook. Probe has visuals suppressed and does
not call build fingerprints, models, subprocesses, package discovery or GUI.
Every child socket and subprocess launch is denied by an audit hook. The only
write is the reviewed parent's bounded supervisor creating a new owned run dir.
"""
from __future__ import annotations

import copy
import hashlib
import json
import sys
import unittest
from pathlib import Path


HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
FIXTURE = HERE / "f11-next-fixture.v1.ipynb"
RUN_AT = "2026-09-09T00:00:00+00:00"
sys.path.insert(0, str(ROOT / "scripts"))


def forbid_external(event, args):
    if event.startswith("socket.") or event in {"subprocess.Popen", "os.system", "os.posix_spawn"}:
        raise AssertionError(f"F11 preflight forbids {event}")


class F11CurrentGapObservations(unittest.TestCase):
    def setUp(self):
        import probe_intermediate_records as records

        self.before = FIXTURE.read_bytes()
        self.assertLessEqual(len(self.before), 1024 * 1024)
        self.source = json.loads(self.before)
        self.reader = records.Probe(
            HERE, RUN_AT, None, diagnostic=False,
            visual_observation_mode="suppressed",
        )
        self.reader.extract(FIXTURE)
        self.addCleanup(lambda: self.assertEqual(self.before, FIXTURE.read_bytes()))

    def test_saved_counts_present_in_source_are_absent_from_reader(self):
        self.assertEqual([c["execution_count"] for c in self.source["cells"][:3]], [7, 2, None])
        self.assertEqual(self.source["cells"][0]["outputs"][0]["execution_count"], 6)
        self.assertEqual(self.reader.documents[0]["extraction"]["status"], "success")
        self.assertEqual(len(self.reader.evidence), 6)
        for evidence in self.reader.evidence:
            native = evidence.get("native_properties", {})
            self.assertNotIn("execution_count", native)
            self.assertNotIn("notebook_execution", native)
            self.assertNotIn("execution_policy", native)
        print("OBSERVED reader emitted 6 text evidence records; saved counts and per-record execution status absent")

    def test_saved_output_with_empty_source_keeps_locator_without_cell_record(self):
        records = [r for r in self.reader.evidence if r["location"]["notebook_cell_index"] == 3]
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["content"]["raw_text"], "saved value C")
        self.assertEqual(records[0]["location"]["locator_text"], "cell=3;output=1")
        self.assertEqual(records[0]["native_properties"], {"output_type": "display_data"})
        print("OBSERVED empty-source cell still has saved output, with no parent cell Evidence to inherit a count")

    def test_search_unit_drops_in_memory_candidate_execution_metadata(self):
        import build_search_units

        derived = []
        deriver = build_search_units.DocumentDeriver(
            self.reader.documents[0]["document_id"], RUN_AT, derived.append, 1000,
        )
        for original in self.reader.evidence:
            record = copy.deepcopy(original)
            record.setdefault("native_properties", {})["notebook_execution"] = {
                "reader_execution": "not_executed",
                "probe_sentinel": "candidate-metadata-only",
            }
            deriver.consume(record)
        deriver.finish()
        self.assertEqual(len(derived), 6)
        self.assertEqual([r["locator"]["notebook_cell_index"] for r in derived], [1, 1, 2, 2, 3, 4])
        for unit in derived:
            self.assertNotIn("notebook_execution", unit.get("context", {}))
            self.assertNotIn("candidate-metadata-only", json.dumps(unit))
        self.assertEqual(len({r["source_evidence_ids"][0] for r in derived}), 6)
        print("OBSERVED direct SearchUnit derivation retains source IDs/locators but drops candidate native metadata")


if __name__ == "__main__":
    if "--supervise" in sys.argv:
        from run_local_memory_hardening_tests import run_bounded

        target = HERE / "f11-next-observation-run.v1"
        result = run_bounded(
            [sys.executable, "-B", str(Path(__file__).resolve()), "-v"],
            target, cwd=ROOT, timeout_seconds=30, max_log_bytes=1024 * 1024,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        raise SystemExit(0 if result["status"] == "passed" else 1)
    sys.addaudithook(forbid_external)
    print("F11 OBSERVATION ONLY: green tests reproduce known gaps, not product acceptance")
    print("source_sha256=" + hashlib.sha256(FIXTURE.read_bytes()).hexdigest())
    unittest.main(verbosity=2)
