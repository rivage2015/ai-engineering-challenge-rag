from __future__ import annotations

import hashlib
import os
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import local_image_ocr as reader  # noqa: E402


NAKANO_SOURCE_PAGE = (
    "https://commons.wikimedia.org/wiki/"
    "File:A_station_sign_at_Nakano_Station_Tokyo.jpg"
)
NAKANO_LICENSE = "CC0 1.0"
NAKANO_SHA256 = "c688ac7d69f6ef24618f7540c35daff1fbcac4dce4ec366e01b35ae198435cd9"
NAKANO_KEY_SPANS = (
    "JB",
    "07",
    "T 01",
    "Nakano",
    "中野",
    "나카노",
    "損していませんか。",
    "アキュアメンバーズなら",
    "貯まったポイントで",
    "ステキな賞品や飲料を",
    "GETできるのに。",
    "検索",
    "乃木坂46",
)
PHASE1_REQUIRED_SPANS = frozenset({"JB", "Nakano", "中野"})
PADDLE_REQUIRED_SPANS = frozenset({
    "JB",
    "07",
    "Nakano",
    "中野",
    "アキュアメンバーズなら",
    "貯まったポイントで",
    "ステキな賞品や飲料を",
    "検索",
    "乃木坂46",
})


class LocalImageOCRRealPhotoRegressionTests(unittest.TestCase):
    """Optional local regression; the CC0 source image itself stays out of Git."""

    def test_nakano_exif6_photo_phase2_floor(self) -> None:
        raw_path = os.environ.get("AIEC_NAKANO_OCR_FIXTURE")
        if not raw_path:
            self.skipTest("set AIEC_NAKANO_OCR_FIXTURE to the hash-locked CC0 JPEG")
        path = Path(raw_path)
        self.assertFalse(path.is_symlink())
        self.assertTrue(path.is_file())
        original = path.read_bytes()
        self.assertEqual(hashlib.sha256(original).hexdigest(), NAKANO_SHA256)

        observation = reader.extract(
            path,
            timeout=120,
            canonicalize=True,
            allow_vlm=False,
        )

        read_text = "\n".join(line["text"] for line in observation["read_lines"])
        found = {span for span in NAKANO_KEY_SPANS if span in read_text}
        high_text = "\n".join(
            line["text"] for line in observation["consensus_lines"]
        )
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(observation["orientation"], 6)
        self.assertEqual(observation["canonicalization"]["status"], "completed")
        self.assertEqual(observation["ocr_input_orientation"], 1)
        self.assertTrue(PHASE1_REQUIRED_SPANS.issubset(found))
        if observation["engines"]["paddleocr"]["status"] == "completed":
            self.assertTrue(PADDLE_REQUIRED_SPANS.issubset(found))
            self.assertGreaterEqual(len(found), 9)
            self.assertGreaterEqual(
                observation["engines"]["paddleocr"]["line_count"], 9
            )
            self.assertTrue(observation["independent_engines"])
        self.assertNotIn("0/", high_text)
        self.assertIn("tesseract_psm11", observation["engines"])
        self.assertEqual(
            observation["engines"]["tesseract_psm11"]["independence_group"],
            "tesseract",
        )
        self.assertFalse(observation["vlm_allowed"])
        self.assertFalse(observation["external_network_used"])
        self.assertFalse(observation["downloads_performed"])


if __name__ == "__main__":
    unittest.main()
