"""Snapshot importer uses the existing Reader bridge without source extraction."""
from __future__ import annotations

import contextlib
import copy
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
ENGINE = ROOT / "distribution" / "macos-local-memory" / "engine"
sys.path.insert(0, str(SCRIPTS))
import adapt_layer1_to_local_memory as adapter
import build_intermediate_records as builder
import freeze_reading_snapshot as freezing
import materialize_reading_snapshot as importer
from test_text_first_reader import PNG_BYTES, write_office_fixture


def load_engine(name):
    spec = importlib.util.spec_from_file_location("snapshot_test_" + name, ENGINE / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


bridge = load_engine("build_adaptive_semantic_graph")
lineage = load_engine("validate_adaptive_semantic_graph")


class SnapshotReaderConnectionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="snapshot-reader-connection-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.root = self.base / "source"
        self.root.mkdir()
        (self.root / "rules.txt").write_text("架空の規程\n火曜日15時。祝日は休み。", encoding="utf-8")
        (self.root / "unread.png").write_bytes(PNG_BYTES)
        write_office_fixture(self.root / "table.xlsx")
        self.records = self.base / "original-records"
        payload = {"reading_policy": "text_first_v1", "fixture": "snapshot-reader-connection"}
        fingerprint = {"version": "1", "payload": payload,
                       "sha256": hashlib.sha256(builder.canonical_json(payload).encode()).hexdigest()}
        with (
            mock.patch.object(sys, "argv", ["builder", "--root", str(self.root), "--out", str(self.records),
                                           "--reading-policy", "text_first_v1", "--run-at", "2031-04-01T00:00:00+00:00"]),
            mock.patch.object(builder, "processing_fingerprint", return_value=fingerprint),
            mock.patch.dict(sys.modules, {"openpyxl": None}),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            builder.main()
        self.snapshot = self.base / "reading.json"
        self.envelope = freezing.freeze(self.records, self.snapshot)
        self.output = self.base / "semantic"
        self.inventory = self.base / "inventory.jsonl"
        inventory = [{
            "kind": "file", "relative_path": path.name, "read_status": "observed",
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "size_bytes": path.stat().st_size,
        } for path in sorted(self.root.iterdir())]
        self.inventory.write_text("".join(json.dumps(item) + "\n" for item in inventory))

    def build(self):
        original = bridge.run_tool
        def no_reader(label, command, tools_dir, log_path):
            self.assertNotEqual(Path(command[1]).name, "build_intermediate_records.py")
            return original(label, command, tools_dir, log_path)
        with mock.patch.object(bridge, "run_tool", side_effect=no_reader):
            return bridge.build(self.root, self.inventory, self.output, SCRIPTS, reading_snapshot_path=self.snapshot)

    def test_end_to_end_search_units_adapter_and_independent_lineage(self):
        state = self.build()
        report = lineage.validate(self.output, self.root, self.inventory, initialize_lineage=True)
        self.assertEqual(report["status"], "PASS")
        self.assertEqual(state["status"], "complete_with_limits")
        self.assertFalse(state["llm_used_for_extraction"])
        materialized = self.output / "layer1-intermediate"
        self.assertEqual(importer.validate_materialized_snapshot(materialized), self.envelope)
        imported_state = json.loads((materialized / "build-state.json").read_text())
        self.assertEqual(imported_state["extractor"], "reading-snapshot-importer")
        self.assertEqual(imported_state["source_provenance"], self.envelope["payload"]["provenance"])
        self.assertNotEqual(imported_state["processing_fingerprint"], imported_state["source_provenance"]["processing_fingerprint"])
        expected = {item["document_id"]: item["extraction"] for item in self.envelope["payload"]["records"]["documents"]}
        docs = [json.loads(line) for line in (self.output / "semantic-documents.jsonl").read_text().splitlines()]
        self.assertEqual({doc["document_id"] for doc in docs}, set(expected))
        for doc in docs:
            self.assertEqual(doc["extraction_metadata"]["source_extraction"], expected[doc["document_id"]])
            self.assertEqual(doc["extraction_metadata"]["reading_snapshot"], state["reading_snapshot"])
        units = [json.loads(line) for line in (self.output / "layer1-search" / "search_units.jsonl").read_text().splitlines()]
        self.assertTrue(any(unit["unit_type"] == "table_row" for unit in units))
        self.assertTrue((self.output / "semantic-lineage-relations.jsonl").is_file())

    def test_direct_text_first_without_snapshot_remains_rejected(self):
        with self.assertRaisesRegex(ValueError, "R2b required"):
            adapter.adapt(self.records, self.root.resolve(), self.base / "direct")

    def test_selected_inputs_must_match_snapshot_exactly(self):
        with self.assertRaisesRegex(ValueError, "selected input manifest"):
            importer.materialize(self.snapshot, self.base / "subset", self.root, ["rules.txt"])

    def test_current_original_change_is_not_treated_as_fresh(self):
        (self.root / "rules.txt").write_text("Changed since extraction", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "source changed"):
            self.build()

    def test_materialized_record_change_is_rejected_even_if_state_hash_rewritten(self):
        out = self.base / "materialized"
        importer.materialize(self.snapshot, out, self.root)
        path = out / "evidence.jsonl"
        raw = path.read_bytes() + b"\n"
        path.write_bytes(raw)
        state_path = out / "build-state.json"
        state = json.loads(state_path.read_text())
        state["aggregates"]["evidence"]["sha256"] = hashlib.sha256(raw).hexdigest()
        state["aggregates"]["evidence"]["size_bytes"] = len(raw)
        state_path.write_text(json.dumps(state))
        with self.assertRaisesRegex(ValueError, "differs from frozen input"):
            importer.validate_materialized_snapshot(out)

    def test_original_producer_cannot_be_forged_in_import_state(self):
        out = self.base / "materialized"
        importer.materialize(self.snapshot, out, self.root)
        path = out / "build-state.json"
        state = json.loads(path.read_text())
        state["source_provenance"]["extractor_version"] = "forged"
        path.write_text(json.dumps(state))
        with self.assertRaises(ValueError):
            importer.validate_materialized_snapshot(out)

    def test_structural_reconstruction_rechecks_frozen_evidence(self):
        out = self.base / "materialized"
        state = importer.materialize(self.snapshot, out, self.root)
        records = self.envelope["payload"]["records"]
        reconstructed = lineage.derive_native_structural_relations(records["documents"], records["evidence"], state)
        self.assertEqual({r["relation_id"]: r for r in reconstructed},
                         {r["relation_id"]: r for r in records["relations"]})
        changed = copy.deepcopy(records["evidence"])
        changed[0]["ordinal"] = 9999
        with self.assertRaisesRegex(ValueError, "snapshot_evidence_records_mismatch"):
            lineage.derive_native_structural_relations(records["documents"], changed, state)

    def test_coverage_loss_rejected_even_when_producer_output_hashes_updated(self):
        state = self.build()
        path = self.output / "semantic-documents.jsonl"
        original = [json.loads(line) for line in path.read_text().splitlines()]
        adapter_state_path = self.output / "layer1-adapter" / "layer1-adapter-state.json"
        original_adapter_state = json.loads(adapter_state_path.read_text())
        for field in ("source_extraction", "reading_snapshot"):
            with self.subTest(field=field):
                documents = copy.deepcopy(original)
                documents[0]["extraction_metadata"].pop(field)
                raw = "".join(json.dumps(doc, ensure_ascii=False, sort_keys=True) + "\n" for doc in documents).encode()
                path.write_bytes(raw)
                (self.output / "layer1-adapter" / path.name).write_bytes(raw)
                adapter_state = copy.deepcopy(original_adapter_state)
                adapter_state["outputs"]["documents"]["sha256"] = hashlib.sha256(raw).hexdigest()
                adapter_raw = json.dumps(adapter_state).encode()
                adapter_state_path.write_bytes(adapter_raw)
                changed_state = copy.deepcopy(state)
                changed_state["outputs"]["documents"]["sha256"] = hashlib.sha256(raw).hexdigest()
                changed_state["stages"]["adapter"]["sha256"] = hashlib.sha256(adapter_raw).hexdigest()
                (self.output / "adaptive-reader-state.json").write_text(json.dumps(changed_state))
                with self.assertRaisesRegex(ValueError, "document_snapshot_"):
                    lineage.validate(self.output, self.root, self.inventory, initialize_lineage=True)

    def test_materializer_refuses_existing_output_and_symlink(self):
        out = self.base / "materialized"
        importer.materialize(self.snapshot, out, self.root)
        before = (out / "build-state.json").read_bytes()
        with self.assertRaisesRegex(ValueError, "empty directory"):
            importer.materialize(self.snapshot, out, self.root)
        self.assertEqual((out / "build-state.json").read_bytes(), before)
        linked = self.base / "linked-output"
        linked.symlink_to(out, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "symlink"):
            importer.materialize(self.snapshot, linked, self.root)

    def test_json_selects_one_book_without_reading_neighbouring_pdf(self):
        manifest_path = self.base / "one-book-manifest.json"
        manifest_path.write_text(json.dumps({
            "schema_version": "0.1", "source_root": str(self.root), "paths": ["table.xlsx"],
        }))
        one_book_records = self.base / "one-book-records"
        fingerprint = self.envelope["payload"]["provenance"]["processing_fingerprint"]
        with (
            mock.patch.object(sys, "argv", ["builder", "--root", str(self.root), "--out", str(one_book_records),
                                           "--reading-policy", "text_first_v1", "--input-manifest", str(manifest_path)]),
            mock.patch.object(builder, "processing_fingerprint", return_value=fingerprint),
            mock.patch.dict(sys.modules, {"openpyxl": None}),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            builder.main()
        self.snapshot = self.base / "one-book.json"
        freezing.freeze(one_book_records, self.snapshot)
        neighbouring_pdf = self.root / "outside.pdf"
        neighbouring_pdf.write_bytes(b"%PDF-1.7\nSynthetic unselected content, not a parsed fixture.")
        with self.inventory.open("a") as handle:
            handle.write(json.dumps({
                "kind": "file", "relative_path": neighbouring_pdf.name, "read_status": "observed",
                "sha256": hashlib.sha256(neighbouring_pdf.read_bytes()).hexdigest(),
                "size_bytes": neighbouring_pdf.stat().st_size,
            }) + "\n")
        inventory_before = self.inventory.read_bytes()
        state = self.build()
        report = lineage.validate(self.output, self.root, self.inventory, initialize_lineage=True)
        self.assertEqual(report["status"], "PASS")
        self.assertEqual(state["selected_file_count"], 1)
        self.assertEqual(state["selection_counts"]["outside_snapshot_count"], 3)
        manifest = json.loads((self.output / "layer1-input-manifest.json").read_text())
        self.assertEqual(manifest["paths"], ["table.xlsx"])
        self.assertEqual(self.inventory.read_bytes(), inventory_before)
        default = bridge.select_attested_inventory(self.inventory)
        self.assertEqual(len(default["selected"]), 4)
        self.assertNotIn("outside_snapshot_count", default["selection_counts"])

    def test_snapshot_selection_rejects_missing_or_hash_changed_inventory(self):
        original = self.inventory.read_text()
        records = [json.loads(line) for line in original.splitlines()]
        for change in ("missing", "hash"):
            with self.subTest(change=change):
                altered = copy.deepcopy(records)
                if change == "missing":
                    altered = altered[1:]
                else:
                    altered[0]["sha256"] = "0" * 64
                self.inventory.write_text("".join(json.dumps(item) + "\n" for item in altered))
                with self.assertRaisesRegex(ValueError, "reading_snapshot_(source_missing_or_held|inventory_source_mismatch)"):
                    bridge.select_attested_inventory(self.inventory, reading_snapshot_path=self.snapshot)

    def test_snapshot_cannot_revive_version_held_source(self):
        records = [json.loads(line) for line in self.inventory.read_text().splitlines()]
        candidate = copy.deepcopy(next(item for item in records if item["relative_path"] == "table.xlsx"))
        candidate["relative_path"] = "table_ver1.xlsx"
        records.append(candidate)
        self.inventory.write_text("".join(json.dumps(item) + "\n" for item in records))
        resolver = load_engine("document_version_resolver")
        graph_path = self.base / "document-version-graph.json"
        resolver.build(self.inventory, graph_path)
        selection = bridge.select_attested_inventory(
            self.inventory, graph_path, version_authority_mode="no_decisions",
        )
        self.assertEqual(selection["selection_counts"]["version_needs_human_review"], 2)
        with self.assertRaisesRegex(ValueError, "reading_snapshot_source_missing_or_held"):
            bridge.select_attested_inventory(
                self.inventory, graph_path, version_authority_mode="no_decisions",
                reading_snapshot_path=self.snapshot,
            )


if __name__ == "__main__":
    unittest.main()
