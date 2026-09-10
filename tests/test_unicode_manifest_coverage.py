import importlib.util
import subprocess
import sys
import tempfile
import unicodedata
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BUILDER = (
    ROOT
    / "distribution"
    / "macos-local-memory"
    / "engine"
    / "build_adaptive_semantic_graph.py"
)
VALIDATOR = (
    ROOT
    / "distribution"
    / "macos-local-memory"
    / "engine"
    / "validate_adaptive_semantic_graph.py"
)


def load_builder():
    spec = importlib.util.spec_from_file_location("unicode_manifest_builder", BUILDER)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def load_validator():
    spec = importlib.util.spec_from_file_location("unicode_manifest_validator", VALIDATOR)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class UnicodeManifestCoverageTests(unittest.TestCase):
    def test_nfd_manifest_matches_layer1_nfc_state(self):
        builder = load_builder()
        nfc = "資料/受付ガイド.pdf"
        nfd = unicodedata.normalize("NFD", nfc)
        self.assertNotEqual(nfd, nfc)
        self.assertEqual(builder.canonical_manifest_input_paths([nfd]), [nfc])

    def test_order_and_cardinality_are_not_weakened(self):
        builder = load_builder()
        values = ["b.txt", "a.txt", unicodedata.normalize("NFD", "資料.txt")]
        canonical = builder.canonical_manifest_input_paths(values)
        self.assertEqual(canonical, ["b.txt", "a.txt", "資料.txt"])
        self.assertEqual(len(canonical), len(values))

    def test_validator_uses_the_same_nfc_boundary(self):
        validator = load_validator()
        nfc = ["資料/受付ガイド.pdf", "資料/業務手順.xlsx"]
        nfd = [unicodedata.normalize("NFD", value) for value in nfc]
        self.assertEqual(validator.canonical_layer1_input_paths(nfd), nfc)

    def test_validator_maps_layer1_path_to_original_inventory_record(self):
        validator = load_validator()
        nfc = "資料/受付ガイド.pdf"
        nfd = unicodedata.normalize("NFD", nfc)
        record = {"relative_path": nfd, "sha256": "a" * 64}
        indexed = validator.inventory_files_by_layer1_path({nfd: record})
        self.assertIs(indexed[nfc], record)

    def test_validator_rejects_normalization_collision(self):
        validator = load_validator()
        nfc = "受付ガイド.txt"
        nfd = unicodedata.normalize("NFD", nfc)
        self.assertNotEqual(nfd, nfc)
        with self.assertRaisesRegex(
            ValueError, "inventory_path_normalization_collision"
        ):
            validator.inventory_files_by_layer1_path(
                {nfc: {"relative_path": nfc}, nfd: {"relative_path": nfd}}
            )

    def test_nfd_source_passes_real_builder_and_validator_boundary(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve()
            source = base / "source"
            source.mkdir()
            nfd_name = unicodedata.normalize("NFD", "受付ガイド.txt")
            (source / nfd_name).write_text("受付では、お客様をお迎えします。", encoding="utf-8")
            path_output = base / "path"
            semantic_output = base / "semantic"
            subprocess.run(
                [
                    sys.executable,
                    str(
                        ROOT
                        / "distribution/macos-local-memory/engine/build_path_graph.py"
                    ),
                    str(source),
                    "--output-dir",
                    str(path_output),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            subprocess.run(
                [
                    sys.executable,
                    str(BUILDER),
                    "--inventory",
                    str(path_output / "path-source-inventory.jsonl"),
                    "--source-root",
                    str(source),
                    "--output-dir",
                    str(semantic_output),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            completed = subprocess.run(
                [
                    sys.executable,
                    str(VALIDATOR),
                    "--output-dir",
                    str(semantic_output),
                    "--source-root",
                    str(source),
                    "--inventory",
                    str(path_output / "path-source-inventory.jsonl"),
                    "--initialize-lineage",
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertIn('"status": "PASS"', completed.stdout)


if __name__ == "__main__":
    unittest.main()
