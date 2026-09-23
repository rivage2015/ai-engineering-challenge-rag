"""Trial overlays modify only fresh temporary bundle copies, never runtime data.

No launcher, model, network, Keychain, or installed application is executed.
"""
from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "distribution/macos-local-memory"
SPEC = importlib.util.spec_from_file_location(
    "trial_bundle_subject", SOURCE / "build/prepare_trial_bundle.py"
)
SUBJECT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SUBJECT)

PROFILE = "LocalMemorySearch-Trial-20260922-123456-1234"
VERSION = "2026.09.22"
PORT = 8766
OVERLAY_FILES = (
    "bootstrap.py",
    "launch.sh",
    "local_memory_server.py",
    "semantic_graph_trust.py",
    "semantic_graph_answer_promotion.py",
    "engine/layer1/scripts/local_image_ocr.py",
)


def constant(path: Path, name: str):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign):
            if any(isinstance(target, ast.Name) and target.id == name
                   for target in node.targets):
                return ast.literal_eval(node.value)
    raise AssertionError(f"constant not found: {name}")


class TrialBundleTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="trial-bundle-test-")
        self.addCleanup(temporary.cleanup)
        self.stage = Path(temporary.name).resolve()
        self.resources = (self.stage / "Local Memory Search 試用版.app"
                          / "Contents/Resources")
        self.resources.mkdir(parents=True)
        self.originals = {}
        for name in OVERLAY_FILES:
            source = (ROOT / "scripts/local_image_ocr.py"
                      if name.startswith("engine/") else SOURCE / "app" / name)
            target = self.resources / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            self.originals[source] = source.read_bytes()
        shutil.copy2(SOURCE / "app/source_selection.py", self.resources)
        self.before = self.snapshot()

    def snapshot(self):
        return {path.relative_to(self.stage).as_posix(): path.read_bytes()
                for path in self.stage.rglob("*") if path.is_file()}

    def prepare(self, **changes):
        arguments = {"resources": self.resources, "profile": PROFILE,
                     "port": PORT, "version": VERSION}
        arguments.update(changes)
        return SUBJECT.prepare(**arguments)

    def assert_sources_unchanged(self):
        for source, content in self.originals.items():
            self.assertEqual(content, source.read_bytes(), str(source))

    def test_overlay_is_confined_to_expected_copies_and_records_hashes(self):
        record = self.prepare()
        self.assertEqual(set(OVERLAY_FILES), set(record["overlay"]))
        base = self.resources.relative_to(self.stage)
        for name, hashes in record["overlay"].items():
            original = self.before[(base / name).as_posix()]
            modified = (self.resources / name).read_bytes()
            self.assertNotEqual(original, modified)
            self.assertEqual(hashlib.sha256(original).hexdigest(), hashes["source_sha256"])
            self.assertEqual(hashlib.sha256(modified).hexdigest(), hashes["trial_sha256"])
        self.assert_sources_unchanged()
        self.assertEqual(self.before[(base / "source_selection.py").as_posix()],
                         (self.resources / "source_selection.py").read_bytes())
        self.assertEqual(record, json.loads((self.stage / "trial-build.json").read_text()))
        self.assertFalse(record["source_config_index_copied"])
        self.assertEqual("trial_not_v1_acceptance", record["status"])
        self.assertFalse(any(path.name in {"config.json", "state.json"}
                             or path.suffix in {".sqlite3", ".jsonl"}
                             for path in self.stage.rglob("*")))

    def test_support_cache_temp_and_port_are_isolated(self):
        self.prepare()
        self.assertEqual(PROFILE, constant(self.resources / "bootstrap.py", "APP_NAME"))
        bootstrap = (self.resources / "bootstrap.py").read_text()
        launcher = (self.resources / "launch.sh").read_text()
        server = (self.resources / "local_memory_server.py").read_text()
        ocr = (self.resources / "engine/layer1/scripts/local_image_ocr.py").read_text()
        self.assertIn(f'"port": {PORT},', bootstrap)
        self.assertIn(f'Library/Application Support/{PROFILE}/config.json', launcher)
        self.assertIn(f'Library/Caches/{PROFILE}', launcher)
        self.assertIn('export TMPDIR="$CACHE_DIR/tmp"', launcher)
        self.assertIn('mkdir -p "$LOG_DIR" "$CACHE_DIR" "$TMPDIR"', launcher)
        self.assertIn(f'.get("port",{PORT})', launcher)
        self.assertIn(f'default={PORT})', server)
        self.assertIn(f'Application Support/{PROFILE}', server)
        self.assertIn(f'/ "{PROFILE}"', ocr)
        self.assertNotIn('/ "LocalMemorySearch"', ocr)
        self.assertIn("Ollamaは共用", server)
        self.assertIn("V1.00の全体受入はまだ完了していません", server)
        self.assertIn('OLLAMA = "http://127.0.0.1:11434"', bootstrap)

    def test_both_keychain_contracts_use_same_separate_namespace(self):
        record = self.prepare()
        values = [constant(self.resources / name, "KEYCHAIN_SERVICE") for name in
                  ("semantic_graph_trust.py", "semantic_graph_answer_promotion.py")]
        self.assertEqual([record["keychain_service"]] * 2, values)
        self.assertNotEqual("jp.rivage.local-memory-search.semantic-graph-root.v1", values[0])
        self.assertIn(PROFILE.lower(), values[0])

    def test_all_changed_python_copies_compile_without_bytecode(self):
        self.prepare()
        for name in OVERLAY_FILES:
            if name.endswith(".py"):
                path = self.resources / name
                compile(path.read_text(), str(path), "exec")
        self.assertEqual([], list(self.stage.rglob("__pycache__")))

    def test_rerun_refused_without_overwriting_prepared_files(self):
        self.prepare()
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, "trial_bundle_already_prepared"):
            self.prepare()
        self.assertEqual(before, self.snapshot())

    def test_invalid_profiles_are_rejected_before_writes(self):
        for profile in ("LocalMemorySearch", "LocalMemorySearch-Trial-20260922",
                        PROFILE + "/../x", PROFILE + '"', "../" + PROFILE):
            with self.subTest(profile=profile):
                with self.assertRaisesRegex(ValueError, "invalid_trial_profile"):
                    self.prepare(profile=profile)
                self.assertEqual(self.before, self.snapshot())

    def test_dangling_manifest_link_is_rejected_before_writes(self):
        destination = self.stage / "not-created.json"
        (self.stage / "trial-build.json").symlink_to(destination)
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, "trial_bundle_already_prepared"):
            self.prepare()
        self.assertEqual(before, self.snapshot())
        self.assertFalse(destination.exists())

    def test_existing_guide_is_not_overwritten(self):
        (self.stage / "試用版の使い方.txt").write_text("既存の案内")
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, "trial_bundle_already_prepared"):
            self.prepare()
        self.assertEqual(before, self.snapshot())

    def test_invalid_and_shared_ports_are_rejected_before_writes(self):
        for port in (True, False, "8766", 1023, 65536, 8765, 11434):
            with self.subTest(port=port):
                with self.assertRaisesRegex(ValueError, "invalid_trial_port"):
                    self.prepare(port=port)
                self.assertEqual(self.before, self.snapshot())

    def test_invalid_versions_are_rejected_before_writes(self):
        for version in ("2026-09-22", "2026.9.22", VERSION + "\n", VERSION + '"'):
            with self.subTest(version=version):
                with self.assertRaisesRegex(ValueError, "invalid_trial_version"):
                    self.prepare(version=version)
                self.assertEqual(self.before, self.snapshot())

    def test_non_trial_app_and_source_tree_are_rejected(self):
        for path in (SOURCE / "app", self.stage / "Resources",
                     self.stage / "Ordinary.app/Contents/Resources"):
            with self.subTest(path=path):
                with self.assertRaisesRegex(ValueError, "new_trial_bundle_required"):
                    self.prepare(resources=path)
                self.assertEqual(self.before, self.snapshot())

    def test_resource_directory_symlink_is_rejected(self):
        link = self.stage / "Other 試用版.app/Contents/Resources"
        link.parent.mkdir(parents=True)
        link.symlink_to(self.resources, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "new_trial_bundle_required"):
            self.prepare(resources=link)
        self.assert_sources_unchanged()
        self.assertFalse((self.stage / "trial-build.json").exists())

    def test_symlinked_overlay_file_is_rejected_before_writes(self):
        path = self.resources / "semantic_graph_trust.py"
        path.unlink()
        path.symlink_to(SOURCE / "app/semantic_graph_trust.py")
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, "symlink_in_bundle"):
            self.prepare()
        self.assertEqual(before, self.snapshot())
        self.assert_sources_unchanged()

    def test_late_anchor_drift_is_rejected_before_any_bundle_write(self):
        path = self.resources / "engine/layer1/scripts/local_image_ocr.py"
        path.write_text(path.read_text().replace('/ "LocalMemorySearch"', '/ "Changed"'))
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, "trial_overlay_anchor_changed"):
            self.prepare()
        self.assertEqual(before, self.snapshot())
        self.assert_sources_unchanged()

    def test_duplicate_anchor_is_rejected_before_any_bundle_write(self):
        path = self.resources / "bootstrap.py"
        path.write_text(path.read_text() + '\nAPP_NAME = "LocalMemorySearch"\n')
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, "trial_overlay_anchor_changed"):
            self.prepare()
        self.assertEqual(before, self.snapshot())

    def test_syntax_drift_is_rejected_before_any_bundle_write(self):
        path = self.resources / "semantic_graph_answer_promotion.py"
        path.write_text(path.read_text() + "\nif invalid syntax\n")
        before = self.snapshot()
        with self.assertRaises(SyntaxError):
            self.prepare()
        self.assertEqual(before, self.snapshot())


if __name__ == "__main__":
    unittest.main()
