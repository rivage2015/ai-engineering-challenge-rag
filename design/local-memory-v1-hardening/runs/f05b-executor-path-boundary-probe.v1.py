"""Preaudit static generation-path gold; no ancestor locking/race claim."""
import importlib.util
from pathlib import Path
import types
import unittest

ROOT = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location("f05b_path_fixture", ROOT / "distribution/macos-local-memory/tests/test_versioned_safe_index_e2e.py")
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)
builder = types.SimpleNamespace()


class StaticSnapshotPathTests(unittest.TestCase):
    def setUp(self):
        self.h = fixture.VersionedSafeIndexE2E()
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)

    def assert_static_link_rejected(self, generation_link):
        target_generation = self.h.base / ("generation-" + "1" * 32)
        target_paths = target_generation / "01-path"
        target_paths.mkdir(parents=True)
        supplied_generation = self.h.base / ("generation-" + "2" * 32)
        if generation_link:
            supplied_generation.symlink_to(target_generation, target_is_directory=True)
        else:
            supplied_generation.mkdir()
            (supplied_generation / "01-path").symlink_to(target_paths, target_is_directory=True)
        path = supplied_generation / "01-path"
        rejected = False
        try:
            self.h.bootstrap.capture_decision_snapshot(path, 128)
        except ValueError:
            rejected = True
        self.assertTrue(rejected, "static generation/path symlink must be rejected")
        self.assertFalse((target_paths / "document-version-decisions.snapshot.json").exists(), "capture must not write through a directory symlink")
        self.assertFalse(self.h.bootstrap.DOCUMENT_VERSION_DECISIONS.exists())

    def test_capture_rejects_static_path_directory_symlink(self):
        self.assert_static_link_rejected(False)

    def test_capture_rejects_static_generation_directory_symlink(self):
        self.assert_static_link_rejected(True)
