from __future__ import annotations

import hashlib
import io
import platform
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import local_image_ocr as reader  # noqa: E402

try:
    from PIL import Image, ImageOps
except ImportError:  # pragma: no cover - the packaged runtime does not need Pillow
    Image = None
    ImageOps = None


@unittest.skipUnless(
    platform.system() == "Darwin" and Image is not None,
    "the integration test requires macOS and the test-only Pillow dependency",
)
class LocalImageCanonicalizerIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.build_temporary = tempfile.TemporaryDirectory()
        cls.build_dir = Path(cls.build_temporary.name) / "build"
        cls.build_dir.mkdir(mode=0o700)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.build_temporary.cleanup()

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def _asymmetric_rgb_image():
        image = Image.new("RGB", (60, 40), "white")
        colors = {
            "top_left": (250, 10, 10),
            "top_right": (10, 10, 250),
            "bottom_left": (10, 250, 10),
            "bottom_right": (250, 250, 10),
        }
        for x in range(image.width):
            for y in range(image.height):
                if x < image.width // 2 and y < image.height // 2:
                    color = colors["top_left"]
                elif y < image.height // 2:
                    color = colors["top_right"]
                elif x < image.width // 2:
                    color = colors["bottom_left"]
                else:
                    color = colors["bottom_right"]
                image.putpixel((x, y), color)
        image.putpixel((4, 7), (0, 0, 0))
        return image

    @staticmethod
    def _corner_labels(image) -> list[str]:
        labels = []
        for x, y in (
            (2, 2),
            (image.width - 3, 2),
            (2, image.height - 3),
            (image.width - 3, image.height - 3),
        ):
            red, green, blue = image.convert("RGB").getpixel((x, y))
            if red > 180 and green < 80 and blue < 80:
                labels.append("red")
            elif blue > 180 and red < 80 and green < 80:
                labels.append("blue")
            elif green > 180 and red < 80 and blue < 80:
                labels.append("green")
            elif red > 180 and green > 180 and blue < 80:
                labels.append("yellow")
            else:
                labels.append("other")
        return labels

    def test_exif_orientations_1_through_8_are_baked_into_pixels(self) -> None:
        source_image = self._asymmetric_rgb_image()
        for orientation in range(1, 9):
            with self.subTest(orientation=orientation):
                source_path = self.root / f"orientation-{orientation}.jpg"
                exif = Image.Exif()
                exif[274] = orientation
                source_image.save(
                    source_path,
                    format="JPEG",
                    quality=100,
                    subsampling=0,
                    exif=exif,
                )
                original = source_path.read_bytes()
                source_dimensions = reader.inspect_image_bytes(original)["dimensions"]

                canonical, metadata = reader.canonicalize_image_bytes(
                    original,
                    source_dimensions,
                    self.build_dir,
                    timeout=120,
                )

                expected = ImageOps.exif_transpose(Image.open(source_path)).convert("RGB")
                actual_file = Image.open(io.BytesIO(canonical))
                actual = actual_file.convert("RGB")
                self.assertEqual(actual.size, expected.size)
                self.assertEqual(
                    self._corner_labels(actual),
                    self._corner_labels(expected),
                )
                self.assertEqual(metadata["source_orientation"], orientation)
                self.assertEqual(metadata["canonical_orientation"], 1)
                self.assertEqual(actual_file.getexif().get(274, 1), 1)
                self.assertIn("srgb", actual_file.info)
                self.assertEqual(source_path.read_bytes(), original)
                self.assertEqual(
                    metadata["source_sha256"], hashlib.sha256(original).hexdigest()
                )
                self.assertEqual(
                    metadata["canonical_sha256"],
                    hashlib.sha256(canonical).hexdigest(),
                )

    def test_transparent_pixels_are_flattened_on_white(self) -> None:
        source_path = self.root / "transparent.png"
        image = Image.new("RGBA", (12, 8), (0, 0, 0, 0))
        image.putpixel((6, 4), (0, 0, 0, 255))
        image.save(source_path, format="PNG")
        original = source_path.read_bytes()

        canonical, metadata = reader.canonicalize_image_bytes(
            original,
            reader.inspect_image_bytes(original)["dimensions"],
            self.build_dir,
            timeout=120,
        )

        actual = Image.open(io.BytesIO(canonical)).convert("RGBA")
        self.assertEqual(actual.getpixel((0, 0)), (255, 255, 255, 255))
        self.assertEqual(actual.getpixel((6, 4)), (0, 0, 0, 255))
        self.assertEqual(metadata["alpha_policy"], "flattened_on_white")

    def test_undefined_exif_orientation_is_safely_treated_as_up(self) -> None:
        source_path = self.root / "invalid-orientation.jpg"
        exif = Image.Exif()
        exif[274] = 9
        self._asymmetric_rgb_image().save(source_path, format="JPEG", exif=exif)
        raw = source_path.read_bytes()

        canonical, metadata = reader.canonicalize_image_bytes(
            raw,
            reader.inspect_image_bytes(raw)["dimensions"],
            self.build_dir,
            timeout=120,
        )

        self.assertEqual(metadata["source_orientation"], 1)
        self.assertEqual(metadata["canonical_orientation"], 1)
        self.assertEqual(Image.open(io.BytesIO(canonical)).size, (60, 40))

    def test_truncated_exif_segment_is_rejected_before_decode(self) -> None:
        corrupt = b"\xff\xd8\xff\xe1\xff\xffExif\x00\x00MM\x00*"
        with self.assertRaisesRegex(ValueError, "invalid JPEG segment"):
            reader.inspect_image_bytes(corrupt)


if __name__ == "__main__":
    unittest.main()
