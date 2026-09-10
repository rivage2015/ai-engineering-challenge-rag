"""Synthetic answer-time decision revision checks."""
from __future__ import annotations

import hashlib
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
PATH = ROOT / "distribution/macos-local-memory/app/bootstrap.py"
SPEC = importlib.util.spec_from_file_location("answer_revision_bootstrap", PATH)
bootstrap = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bootstrap)


class AnswerDecisionRevisionGateTests(unittest.TestCase):
    def fixture(self, snapshot: bytes, live: bytes | None):
        temporary = tempfile.TemporaryDirectory()
        root = Path(temporary.name)
        generation_name = "generation-" + "1" * 32
        path = root / "generations" / generation_name / "01-path"
        path.mkdir(parents=True)
        (path / "document-version-decisions.snapshot.json").write_bytes(snapshot)
        live_path = root / "live.json"
        if live is not None:
            live_path.write_bytes(live)
        config = {"workspace": str(root), "active_generation": generation_name}
        return temporary, root, config, live_path

    def check(self, snapshot: bytes, live: bytes | None, *, with_identity=False):
        temporary, root, config, live_path = self.fixture(snapshot, live)
        try:
            with (
                mock.patch.object(bootstrap, "load_config_snapshot", return_value=(True, config)),
                mock.patch.object(bootstrap, "DOCUMENT_VERSION_DECISIONS", live_path),
                mock.patch.object(bootstrap, "SUPPORT", root),
                mock.patch.object(
                    bootstrap, "ENGINE",
                    ROOT / "distribution/macos-local-memory/engine",
                ),
            ):
                return (
                    bootstrap.active_answer_revision_identity()
                    if with_identity
                    else bootstrap.active_decision_revision_current()
                )
        finally:
            temporary.cleanup()

    def test_absent_live_store_matches_only_canonical_empty_snapshot(self):
        self.assertEqual(
            self.check(bootstrap.EMPTY_DECISION_SNAPSHOT, None),
            (True, "current"),
        )
        self.assertEqual(
            self.check(b'{"schema_version":"2.0","decisions":[],"inactive_decisions":[]}\n', None)[0],
            False,
        )

    def test_present_live_store_requires_exact_raw_snapshot(self):
        raw = b'{"schema_version":"2.0","decisions":[],"inactive_decisions":[]}\n'
        self.assertEqual(self.check(raw, raw), (True, "current"))
        self.assertEqual(self.check(raw, raw + b" ")[0], False)

    def test_answer_identity_binds_generation_decision_and_complete_config(self):
        temporary, root, config, live_path = self.fixture(
            bootstrap.EMPTY_DECISION_SNAPSHOT, None
        )
        try:
            with (
                mock.patch.object(
                    bootstrap, "load_config_snapshot", return_value=(True, config)
                ),
                mock.patch.object(
                    bootstrap, "DOCUMENT_VERSION_DECISIONS", live_path
                ),
                mock.patch.object(bootstrap, "SUPPORT", root),
                mock.patch.object(
                    bootstrap,
                    "ENGINE",
                    ROOT / "distribution/macos-local-memory/engine",
                ),
            ):
                current, reason, identity = (
                    bootstrap.active_answer_revision_identity()
                )
                self.assertTrue(current)
                self.assertEqual(reason, "current")
                self.assertEqual(
                    identity["generation"], "generation-" + "1" * 32
                )
                self.assertEqual(
                    identity["decision_snapshot_sha256"],
                    hashlib.sha256(
                        bootstrap.EMPTY_DECISION_SNAPSHOT
                    ).hexdigest(),
                )
                self.assertTrue(
                    bootstrap.answer_config_matches_revision(config, identity)
                )
                self.assertFalse(
                    bootstrap.answer_config_matches_revision(
                        {**config, "answer_model": "changed"}, identity
                    )
                )
        finally:
            temporary.cleanup()


if __name__ == "__main__":
    unittest.main()
