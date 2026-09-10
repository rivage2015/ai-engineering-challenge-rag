"""F11a synthetic application gates; no real process/model/public state."""
from __future__ import annotations
import contextlib
import copy
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import types
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "distribution/macos-local-memory"
SPEC = importlib.util.spec_from_file_location("f11a_app_harness", PACKAGE / "tests/test_versioned_safe_index_e2e.py")
HARNESS = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = HARNESS
SPEC.loader.exec_module(HARNESS)
builder = types.SimpleNamespace()


class NotebookApplicationTests(HARNESS.VersionedSafeIndexE2E):
    # Runner selects only these four methods, never inherited E2E methods.
    def byte_map(self, directory):
        return {str(p.relative_to(directory)): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in directory.rglob("*") if p.is_file()}

    def test_unverified_notebook_stops_actual_bridge_and_preserves_public_generation(self):
        self.seed()
        self.bootstrap.build_index()
        before_config = self.bootstrap.CONFIG.read_bytes()
        config = self.bootstrap.load_json(self.bootstrap.CONFIG)
        old_generation = Path(config["index_path"]).parent
        before_map = self.byte_map(old_generation)
        marker = self.bootstrap.reader_generation_contract_status(config)
        self.assertEqual(marker["state"], "current")
        path = self.source / "notebook.ipynb"
        payload = json.dumps({"cells": [{"cell_type": "code", "source": "inert text",
                                         "outputs": []}]})
        path.write_text(payload, encoding="utf-8")
        bridge = self.module(PACKAGE / "engine/build_adaptive_semantic_graph.py")
        fresh = HARNESS.load_module(PACKAGE / "engine/build_adaptive_semantic_graph.py")
        stages, returns, stopped = [], [], []

        def dispatch(command, **kwargs):
            module = self.module(Path(command[1]))
            captured = io.StringIO()
            with mock.patch.object(sys, "argv", command[1:]), contextlib.redirect_stdout(captured):
                try:
                    code = module.main()
                    code = 0 if code is None else code
                except SystemExit as exc:
                    code = exc.code
            returns.append((Path(command[1]).name, code, captured.getvalue()))
            return subprocess.CompletedProcess(command, code, captured.getvalue(), "")

        def checked_stage(label, command, tools, log):
            stages.append(label)
            # Real run_tool handles exit code/logging; only OS launch is stubbed.
            with mock.patch.object(fresh.subprocess, "run", side_effect=dispatch):
                try:
                    return fresh.run_tool(label, command, tools, log)
                except RuntimeError:
                    semantic = Path(log).parent
                    stopped.append({
                        "stage": label,
                        "validation_receipt_exists": (semantic / "layer1-validation-state.json").exists(),
                        "search_exists": (semantic / "layer1-search").exists(),
                    })
                    raise

        bridge.run_tool = checked_stage
        try:
            with self.assertRaises((RuntimeError, SystemExit)) as raised:
                self.bootstrap.build_index()
            self.assertIn("adaptive_reader_stage_failed:intermediate_validation:exit_2", str(raised.exception))
            self.assertEqual(stages, ["intermediate", "intermediate_validation"])
            self.assertEqual(returns[-1][0:2], ("validate_intermediate_records_streaming.py", 2))
            self.assertEqual(json.loads(returns[-1][2])["status"], "UNVERIFIED")
            self.assertEqual(self.bootstrap.CONFIG.read_bytes(), before_config)
            self.assertEqual(self.byte_map(old_generation), before_map)
            generations = Path(config["workspace"]) / "generations"
            newer = [p for p in generations.iterdir() if p != old_generation]
            self.assertEqual(newer, [])
            self.assertEqual(stopped, [{"stage": "intermediate_validation",
                                       "validation_receipt_exists": False,
                                       "search_exists": False}])
            self.assertEqual(path.read_text(encoding="utf-8"), payload)
        finally:
            bridge.run_tool = lambda _label, command, _tools, _log: self.run_cli(command)

    def test_changed_reader_contract_holds_old_generation_without_mutating_it(self):
        self.seed()
        self.bootstrap.build_index()
        config = self.bootstrap.load_json(self.bootstrap.CONFIG)
        generation = Path(config["index_path"]).parent
        before = self.byte_map(generation)
        config_bytes = self.bootstrap.CONFIG.read_bytes()
        current = self.bootstrap._current_reader_resource_contract()
        changed = copy.deepcopy(current)
        changed["processing_code"]["probe_intermediate_records.py"]["sha256"] = "f" * 64
        with mock.patch.object(self.bootstrap, "_current_reader_resource_contract", return_value=changed):
            status = self.bootstrap.reader_generation_contract_status(config)
        self.assertEqual(status["state"], "reader_migration_required")
        self.assertEqual(self.byte_map(generation), before)
        self.assertEqual(self.bootstrap.CONFIG.read_bytes(), config_bytes)

    def test_explicit_rebuild_creates_new_generation_and_keeps_previous_bytes(self):
        self.seed()
        self.bootstrap.build_index()
        old_config = self.bootstrap.load_json(self.bootstrap.CONFIG)
        old_generation = Path(old_config["index_path"]).parent
        before = self.byte_map(old_generation)
        self.bootstrap.build_index()
        current = self.bootstrap.load_json(self.bootstrap.CONFIG)
        self.assertNotEqual(current["index_path"], old_config["index_path"])
        self.assertEqual(self.byte_map(old_generation), before)
        self.assertEqual(self.bootstrap.reader_generation_contract_status(current)["state"], "current")

    def test_probe_and_schemas_are_in_existing_package_copy_contract(self):
        # Read-only static copy-list check, not an actual packaged app build.
        # Runner supplies this one read-only source snapshot before the guard.
        package = self.package_copy_text
        for name in ("probe_intermediate_records.py", "build_intermediate_records.py",
                     "validate_intermediate_records.py", "validate_intermediate_records_streaming.py",
                     "build_search_units.py", "validate_search_units.py"):
            self.assertIn('"$ROOT/scripts/' + name + '"', package)
        for name in ("evidence.schema.json", "search-unit.schema.json"):
            self.assertIn('"$ROOT/schemas/' + name + '"', package)
        self.assertIn("probe_intermediate_records.py", self.bootstrap.READER_PROCESSING_CODE_FILES)
        self.assertIn("probe_intermediate_records.py", self.module(ROOT / "scripts/build_intermediate_records.py").PROCESSING_CODE_FILES)


if __name__ == "__main__":
    unittest.main(verbosity=2)
