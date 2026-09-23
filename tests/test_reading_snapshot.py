"""Fixed extraction JSON preserves evidence without re-reading source files."""
from __future__ import annotations

import base64
import contextlib
import copy
import hashlib
import io
import json
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest import mock


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import build_intermediate_records as builder
import freeze_reading_snapshot as snapshots
import local_image_ocr as image_reader
import local_visual_observation as visual_reader
import validate_intermediate_records as validator


RUN_AT = "2031-04-01T00:00:00+00:00"
PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR4"
    "2mP8/x8AAusB9Y9ZK7sAAAAASUVORK5CYII="
)


def digest(value):
    return hashlib.sha256(validator.canonical_json(value).encode("utf-8")).hexdigest()


def fingerprint(policy="text_first_v1"):
    payload = {"reading_policy": policy, "fixture": "reading-snapshot"}
    return {"version": "1", "payload": payload, "sha256": digest(payload)}


class ReadingSnapshotTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="lms-reading-snapshot-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.source = self.base / "source"
        self.source.mkdir()
        (self.source / "rules.txt").write_text(
            "架空施設のご案内\n火曜日の15時に実施。祝日は休止します。\n",
            encoding="utf-8",
        )
        (self.source / "unread.png").write_bytes(PNG_BYTES)
        self.intermediate = self.base / "intermediate"
        self.output = self.base / "reading.json"
        self.model_guards = [self.enterContext(mock.patch.object(
            owner, name, side_effect=AssertionError("image/model reading is forbidden")
        )) for owner, name in (
            (image_reader, "extract"),
            (visual_reader, "observe_path"),
            (visual_reader, "_ollama_json"),
            (visual_reader, "_run_isolated_task"),
        )]
        argv = [
            "build_intermediate_records.py", "--root", str(self.source),
            "--out", str(self.intermediate), "--run-at", RUN_AT,
            "--reading-policy", "text_first_v1",
        ]
        with (
            mock.patch.object(sys, "argv", argv),
            mock.patch.object(builder, "processing_fingerprint", return_value=fingerprint()),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            builder.main()

    def tearDown(self):
        for guarded in self.model_guards:
            guarded.assert_not_called()

    def records(self):
        return {
            kind: validator.read_jsonl(self.intermediate / f"{kind}.jsonl")
            for kind in ("documents", "evidence", "relations")
        }

    def write_mutated(self, envelope, *, rehash=True, name="mutated.json"):
        changed = copy.deepcopy(envelope)
        if rehash:
            changed["payload_sha256"] = digest(changed["payload"])
            changed["snapshot_id"] = "reading_" + changed["payload_sha256"]
        path = self.base / name
        path.write_text(json.dumps(changed, ensure_ascii=False), encoding="utf-8")
        return path

    def rewrite_state(self, edit):
        path = self.intermediate / "build-state.json"
        value = json.loads(path.read_text(encoding="utf-8"))
        edit(value)
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")

    def test_freeze_and_reload_preserve_every_record_and_pending_visual(self):
        original = self.records()
        envelope = snapshots.freeze(self.intermediate, self.output)
        self.assertEqual(set(envelope), {
            "schema_version", "record_type", "snapshot_id", "payload_sha256", "payload",
        })
        self.assertEqual(envelope["schema_version"], "1")
        self.assertEqual(envelope["record_type"], "reading_snapshot")
        self.assertEqual(envelope["payload_sha256"], digest(envelope["payload"]))
        self.assertEqual(envelope["snapshot_id"], "reading_" + envelope["payload_sha256"])
        self.assertEqual(envelope["payload"]["records"], original)
        self.assertEqual(snapshots.load_snapshot(self.output), envelope)
        self.assertEqual(envelope["payload"]["usage"], {
            "answer_ready": False, "source_freshness": "extraction_time_only",
            "complete_source_coverage": False,
        })
        pending = [
            item for document in original["documents"]
            for item in document["extraction"]["visual_coverage"]["pending"]
        ]
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["kind"], "standalone_image")
        self.assertTrue(original["relations"])
        self.assertEqual(stat.S_IMODE(self.output.stat().st_mode), 0o400)

    def test_provenance_preserves_original_hashes_and_source_identities(self):
        envelope = snapshots.freeze(self.intermediate, self.output)
        provenance = envelope["payload"]["provenance"]
        state = json.loads((self.intermediate / "build-state.json").read_text())
        self.assertEqual(provenance["processing_fingerprint"], state["processing_fingerprint"])
        self.assertEqual(provenance["run_at"], state["run_at"])
        self.assertEqual(provenance["extractor"], state["extractor"])
        self.assertEqual(provenance["aggregate_hashes"], {
            kind: hashlib.sha256((self.intermediate / f"{kind}.jsonl").read_bytes()).hexdigest()
            for kind in ("documents", "evidence", "relations")
        })
        manifest = {item["document_id"]: item for item in provenance["input_manifest"]}
        for document in self.records()["documents"]:
            source = document["source"]
            self.assertEqual(manifest[document["document_id"]], {
                "relative_path": source["relative_path"],
                "document_id": document["document_id"],
                "source_sha256": source["sha256"], "size_bytes": source["size_bytes"],
                "status": document["extraction"]["status"],
            })

    def test_export_and_load_work_with_original_source_unavailable(self):
        self.source.rename(self.base / "source-not-available")
        with mock.patch.object(builder.Probe, "extract", side_effect=AssertionError("no re-extraction")) as extract:
            frozen = snapshots.freeze(self.intermediate, self.output)
            self.assertEqual(snapshots.load_snapshot(self.output), frozen)
        extract.assert_not_called()

    def test_checksum_tampering_is_rejected(self):
        envelope = snapshots.freeze(self.intermediate, self.output)
        envelope["payload"]["provenance"]["run_at"] = "2032-04-01T00:00:00+00:00"
        with self.assertRaises(ValueError):
            snapshots.load_snapshot(self.write_mutated(envelope, rehash=False))

    def test_rehash_does_not_bypass_record_hash_or_reference_validation(self):
        envelope = snapshots.freeze(self.intermediate, self.output)
        for label in ("text", "relation", "document", "coverage"):
            with self.subTest(label=label):
                changed = copy.deepcopy(envelope)
                records = changed["payload"]["records"]
                if label == "text":
                    next(item for item in records["evidence"] if "raw_text" in item["content"])["content"]["raw_text"] = "別の内容"
                elif label == "relation":
                    records["relations"][0]["to_ref"]["record_id"] = "ev_" + "0" * 32
                elif label == "document":
                    records["documents"][0]["document_id"] = "doc_" + "0" * 32
                else:
                    document = next(item for item in records["documents"] if item["extraction"]["visual_coverage"]["pending"])
                    document["extraction"]["visual_coverage"] = {"status": "none_pending", "pending": []}
                with self.assertRaises(ValueError):
                    snapshots.load_snapshot(self.write_mutated(changed, name=f"{label}.json"))

    def test_rehash_does_not_bypass_manifest_binding_or_reading_policy(self):
        envelope = snapshots.freeze(self.intermediate, self.output)
        for label in ("source_sha256", "status", "size_bytes", "policy"):
            with self.subTest(label=label):
                changed = copy.deepcopy(envelope)
                provenance = changed["payload"]["provenance"]
                if label == "policy":
                    provenance["processing_fingerprint"] = fingerprint("full")
                else:
                    provenance["input_manifest"][0][label] = {
                        "source_sha256": "0" * 64, "status": "failed", "size_bytes": 1,
                    }[label]
                with self.assertRaises(ValueError):
                    snapshots.load_snapshot(self.write_mutated(changed, name=f"{label}.json"))

    def test_cannot_promote_to_answer_ready_even_with_fresh_payload_hash(self):
        envelope = snapshots.freeze(self.intermediate, self.output)
        for key, value in (("answer_ready", True), ("complete_source_coverage", True), ("source_freshness", "current")):
            with self.subTest(key=key):
                changed = copy.deepcopy(envelope)
                changed["payload"]["usage"][key] = value
                with self.assertRaises(ValueError):
                    snapshots.load_snapshot(self.write_mutated(changed, name=f"{key}.json"))

    def test_unknown_envelope_field_and_duplicate_key_are_rejected(self):
        envelope = snapshots.freeze(self.intermediate, self.output)
        changed = copy.deepcopy(envelope)
        changed["answer_ready"] = True
        with self.assertRaises(ValueError):
            snapshots.load_snapshot(self.write_mutated(changed))
        duplicate = self.base / "duplicate.json"
        duplicate.write_text('{"schema_version":"1",' + self.output.read_text()[1:], encoding="utf-8")
        with self.assertRaises(ValueError):
            snapshots.load_snapshot(duplicate)

    def test_output_existing_file_is_never_overwritten(self):
        self.output.write_text("keep this unrelated file", encoding="utf-8")
        before = self.output.read_bytes()
        with self.assertRaises((ValueError, OSError)):
            snapshots.freeze(self.intermediate, self.output)
        self.assertEqual(self.output.read_bytes(), before)

    def test_output_existing_empty_file_is_also_not_overwritten(self):
        self.output.touch()
        with self.assertRaises((ValueError, OSError)):
            snapshots.freeze(self.intermediate, self.output)
        self.assertEqual(self.output.read_bytes(), b"")

    def test_output_symlink_is_rejected_without_changing_target(self):
        target = self.base / "target.txt"
        target.write_text("untouched", encoding="utf-8")
        self.output.symlink_to(target)
        with self.assertRaises((ValueError, OSError)):
            snapshots.freeze(self.intermediate, self.output)
        self.assertTrue(self.output.is_symlink())
        self.assertEqual(target.read_text(), "untouched")

    def test_unmanaged_input_is_rejected(self):
        self.rewrite_state(lambda state: state.update(extractor="another-extractor"))
        with self.assertRaises(ValueError):
            snapshots.freeze(self.intermediate, self.output)
        self.assertFalse(self.output.exists())

    def test_in_progress_input_is_rejected(self):
        self.rewrite_state(lambda state: state.update(build_status="in_progress"))
        with self.assertRaises(ValueError):
            snapshots.freeze(self.intermediate, self.output)
        self.assertFalse(self.output.exists())

    def test_aggregate_byte_tampering_is_rejected(self):
        with (self.intermediate / "evidence.jsonl").open("ab") as handle:
            handle.write(b"\n")
        with self.assertRaises(ValueError):
            snapshots.freeze(self.intermediate, self.output)
        self.assertFalse(self.output.exists())

    def test_inconsistent_policy_is_rejected_even_when_build_hashes_match(self):
        def edit(state):
            state["processing_fingerprint"] = fingerprint("full")
            for entry in state["entries"].values():
                entry["processing_fingerprint_sha256"] = fingerprint("full")["sha256"]
        self.rewrite_state(edit)
        with self.assertRaises(ValueError):
            snapshots.freeze(self.intermediate, self.output)
        self.assertFalse(self.output.exists())


if __name__ == "__main__":
    unittest.main()
