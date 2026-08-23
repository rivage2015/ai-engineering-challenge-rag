from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import run_ocr_poc_paddle as paddle_runner


def make_model_root(parent: Path, name: str, *, weights: bytes) -> Path:
    root = parent / name
    root.mkdir(parents=True)
    (root / "inference.json").write_bytes(b"config")
    (root / "inference.pdiparams").write_bytes(weights)
    return root


class ArrayLike:
    def __init__(self, value):
        self.value = value

    def tolist(self):
        return self.value


class PaddlePoCRunnerTest(unittest.TestCase):
    def test_result_mapping_preserves_engine_order_and_raw_values(self) -> None:
        result = {
            "rec_texts": ["二番目", "  first  "],
            "rec_scores": ArrayLike([0.625, 1.0]),
            "rec_boxes": ArrayLike([[50, 10, 150, 30], [0, 0, 25, 10]]),
        }
        lines = paddle_runner.paddle_result_to_lines(
            result, width_px=200, height_px=100
        )
        self.assertEqual(["二番目", "  first  "], [line["raw_text"] for line in lines])
        self.assertEqual([0.625, 1.0], [line["confidence"] for line in lines])
        self.assertEqual([250, 100, 500, 200], lines[0]["bbox"])
        self.assertEqual([0, 0, 125, 100], lines[1]["bbox"])
        self.assertEqual([1, 2], [line["sequence"] for line in lines])

    def test_result_mapping_rejects_misaligned_output(self) -> None:
        result = {
            "rec_texts": ["A", "B"],
            "rec_scores": [0.9],
            "rec_boxes": [[0, 0, 10, 10], [10, 0, 20, 10]],
        }
        with self.assertRaisesRegex(ValueError, "output length mismatch"):
            paddle_runner.paddle_result_to_lines(
                result, width_px=20, height_px=10
            )

    def test_model_preflight_never_downloads_implicitly(self) -> None:
        with tempfile.TemporaryDirectory(prefix="aiec-paddle-cache-") as temporary:
            cache = Path(temporary)
            with self.assertRaisesRegex(ValueError, "download is required"):
                paddle_runner.require_models_or_download_permission(
                    cache, allow_model_download=False
                )
            self.assertEqual(
                (None, None),
                paddle_runner.require_models_or_download_permission(
                    cache, allow_model_download=True
                ),
            )

    def test_model_identity_is_portable_across_cache_roots(self) -> None:
        with tempfile.TemporaryDirectory(prefix="aiec-paddle-model-a-") as first_tmp:
            with tempfile.TemporaryDirectory(prefix="aiec-paddle-model-b-") as second_tmp:
                first_parent = Path(first_tmp) / "cache-a"
                second_parent = Path(second_tmp) / "cache-b"
                first_det = make_model_root(
                    first_parent, paddle_runner.DETECTION_MODEL, weights=b"same det"
                )
                first_rec = make_model_root(
                    first_parent, paddle_runner.RECOGNITION_MODEL, weights=b"same rec"
                )
                second_det = make_model_root(
                    second_parent, paddle_runner.DETECTION_MODEL, weights=b"same det"
                )
                second_rec = make_model_root(
                    second_parent, paddle_runner.RECOGNITION_MODEL, weights=b"same rec"
                )

                first_model = paddle_runner.directory_fingerprint(first_det)
                second_model = paddle_runner.directory_fingerprint(second_det)
                self.assertEqual(first_model, second_model)
                self.assertNotIn("directory", first_model)

                first_config = paddle_runner.stable_pipeline_config(
                    {
                        "SubModules": {
                            "TextDetection": {
                                "model_name": paddle_runner.DETECTION_MODEL,
                                "model_dir": str(first_det),
                            },
                            "TextRecognition": {
                                "model_name": paddle_runner.RECOGNITION_MODEL,
                                "model_dir": str(first_rec),
                            },
                        }
                    },
                    model_paths={
                        first_det: paddle_runner.DETECTION_MODEL,
                        first_rec: paddle_runner.RECOGNITION_MODEL,
                    },
                )
                second_config = paddle_runner.stable_pipeline_config(
                    {
                        "SubModules": {
                            "TextDetection": {
                                "model_name": paddle_runner.DETECTION_MODEL,
                                "model_dir": str(second_det),
                            },
                            "TextRecognition": {
                                "model_name": paddle_runner.RECOGNITION_MODEL,
                                "model_dir": str(second_rec),
                            },
                        }
                    },
                    model_paths={
                        second_det: paddle_runner.DETECTION_MODEL,
                        second_rec: paddle_runner.RECOGNITION_MODEL,
                    },
                )
                self.assertEqual(first_config, second_config)
                first_identity = paddle_runner.contract.sha256_json(
                    {"config": first_config, "model": first_model}
                )
                second_identity = paddle_runner.contract.sha256_json(
                    {"config": second_config, "model": second_model}
                )
                self.assertEqual(first_identity, second_identity)

                (second_det / "inference.pdiparams").write_bytes(b"changed weights")
                changed_model = paddle_runner.directory_fingerprint(second_det)
                self.assertNotEqual(first_model, changed_model)

    def test_config_rejects_unapproved_name_path_mismatch_and_duplicate_path(self) -> None:
        with tempfile.TemporaryDirectory(prefix="aiec-paddle-model-map-") as temporary:
            parent = Path(temporary)
            detection = make_model_root(
                parent, paddle_runner.DETECTION_MODEL, weights=b"det"
            )
            recognition = make_model_root(
                parent, paddle_runner.RECOGNITION_MODEL, weights=b"rec"
            )
            approved = {
                detection: paddle_runner.DETECTION_MODEL,
                recognition: paddle_runner.RECOGNITION_MODEL,
            }

            with self.assertRaisesRegex(ValueError, "unapproved PaddleOCR model_name"):
                paddle_runner.stable_pipeline_config(
                    {
                        "model_name": "unknown-model",
                        "model_dir": "/definitely/not/approved",
                    },
                    model_paths=approved,
                )

            with self.assertRaisesRegex(ValueError, "unapproved PaddleOCR model_dir"):
                paddle_runner.stable_pipeline_config(
                    {
                        "model_name": paddle_runner.DETECTION_MODEL,
                        "model_dir": "/definitely/not/approved",
                    },
                    model_paths=approved,
                )

            with self.assertRaisesRegex(ValueError, "model_dir/model_name mismatch"):
                paddle_runner.stable_pipeline_config(
                    {
                        "model_name": paddle_runner.DETECTION_MODEL,
                        "model_dir": str(recognition),
                    },
                    model_paths=approved,
                )

            alias_parent = parent / "alias-parent"
            alias_parent.mkdir()
            duplicate_detection = alias_parent / ".." / detection.name
            with self.assertRaisesRegex(ValueError, "duplicate resolved PaddleOCR model path"):
                paddle_runner.stable_pipeline_config(
                    {},
                    model_paths={
                        detection: paddle_runner.DETECTION_MODEL,
                        duplicate_detection: paddle_runner.RECOGNITION_MODEL,
                    },
                )


if __name__ == "__main__":
    unittest.main()
