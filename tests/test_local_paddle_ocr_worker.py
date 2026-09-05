from __future__ import annotations

import json
import socket
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
RUNTIME_LOCK = (
    ROOT / "distribution" / "macos-local-memory"
    / "paddleocr-requirements.lock.txt"
)

import local_paddle_ocr as worker  # noqa: E402


class ArrayLike:
    def __init__(self, value):
        self.value = value

    def tolist(self):
        return self.value


def create_model_tree(root: Path) -> tuple[Path, dict[str, Path]]:
    official = root / "official_models"
    paths = {}
    for name in (worker.DETECTION_MODEL, worker.RECOGNITION_MODEL):
        path = official / name
        path.mkdir(parents=True)
        (path / "inference.json").write_bytes(b"config")
        paths[name] = path.resolve()
    return root, paths


def approved_metadata() -> dict[str, dict[str, object]]:
    return {
        name: {
            "name": name,
            "algorithm": "sha256(relative_path\\0size\\0file_sha256\\n)",
            **contract,
        }
        for name, contract in worker.MODEL_CONTRACTS.items()
    }


class LocalPaddleOCRWorkerTests(unittest.TestCase):
    def test_result_mapping_preserves_raw_order_and_normalizes_bbox(self) -> None:
        result = {
            "rec_texts": ["二番目", "", "  first  "],
            "rec_scores": ArrayLike([0.625, 0.0, 1.0]),
            "rec_boxes": ArrayLike([
                [50, 10, 150, 30],
                [25, 10, 45, 20],
                [0, 0, 25, 10],
            ]),
        }
        lines = worker.paddle_result_to_lines(
            result,
            width_px=200,
            height_px=100,
        )
        self.assertEqual(["二番目", "  first  "], [line["raw_text"] for line in lines])
        self.assertEqual([250, 100, 500, 200], lines[0]["bbox"])
        self.assertEqual([0, 0, 125, 100], lines[1]["bbox"])
        self.assertEqual([1, 2], [line["sequence"] for line in lines])
        self.assertEqual([1, 3], [line["source_sequence"] for line in lines])

    def test_runtime_lock_rejects_tampering(self) -> None:
        with tempfile.TemporaryDirectory(prefix="aiec-paddle-lock-") as temporary:
            tampered = Path(temporary) / "runtime.lock"
            tampered.write_text("paddleocr==3.7.1\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "lock hash"):
                worker.verify_runtime_lock(tampered)

    def test_missing_and_tampered_models_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory(prefix="aiec-paddle-models-") as temporary:
            root = Path(temporary)
            (root / "official_models").mkdir()
            with self.assertRaisesRegex(ValueError, "PP-OCRv6_medium_det directory"):
                worker.verify_models(root)

            _, paths = create_model_tree(root)
            with self.assertRaisesRegex(ValueError, "approved model manifest"):
                worker.verify_models(root)
            (paths[worker.DETECTION_MODEL] / "inference.json").write_bytes(b"tampered")
            with self.assertRaisesRegex(ValueError, "approved model manifest"):
                worker.verify_models(root)

    def test_network_guard_denies_ip_but_allows_unix_socket(self) -> None:
        original = socket.socket
        with worker.offline_socket_guard():
            with self.assertRaises(worker.OfflineNetworkError):
                socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            with self.assertRaises(worker.OfflineNetworkError):
                socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
            unix_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            unix_socket.close()
        self.assertIs(socket.socket, original)

    def test_path_validation_rejects_symlinks_existing_output_and_model_output(self) -> None:
        with tempfile.TemporaryDirectory(prefix="aiec-paddle-paths-") as temporary:
            root = Path(temporary)
            model_root, _ = create_model_tree(root / "models")
            source = root / "input.png"
            source.write_bytes(b"not-empty")
            output = root / "result.json"
            worker.validate_paths(source, output, model_root)

            output.write_text("occupied", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "must not already exist"):
                worker.validate_paths(source, output, model_root)

            alias = root / "input-alias.png"
            alias.symlink_to(source)
            with self.assertRaisesRegex(ValueError, "non-symlink file"):
                worker.validate_paths(alias, root / "other.json", model_root)

            inside_model = model_root / "worker-result.json"
            with self.assertRaisesRegex(ValueError, "outside the model root"):
                worker.validate_paths(source, inside_model, model_root)

    def test_worker_uses_exact_local_models_and_writes_atomic_bounded_json(self) -> None:
        with tempfile.TemporaryDirectory(prefix="aiec-paddle-worker-") as temporary:
            root = Path(temporary)
            model_root, model_paths = create_model_tree(root / "models")
            source = root / "input.png"
            source.write_bytes(b"image-bytes")
            output = root / "result.json"
            captured: dict[str, object] = {}

            class FakeImageValue:
                size = (200, 100)

                def __enter__(self):
                    return self

                def __exit__(self, *_args):
                    return None

                def load(self):
                    return None

                def convert(self, mode):
                    captured["image_mode"] = mode
                    return self

            image_module = types.ModuleType("PIL.Image")
            image_module.open = lambda stream: (
                captured.setdefault("input_bytes", stream.read()),
                FakeImageValue(),
            )[1]
            pil_module = types.ModuleType("PIL")
            pil_module.Image = image_module
            numpy_module = types.ModuleType("numpy")
            numpy_module.asarray = lambda value: (
                captured.setdefault("array_input", value),
                "rgb-array",
            )[1]

            class Model:
                def __init__(self, name, path):
                    self.model_name = name
                    self.model_dir = str(path)

            class Inner:
                device = "cpu"
                engine = "paddle_static"
                text_det_model = Model(worker.DETECTION_MODEL, model_paths[worker.DETECTION_MODEL])
                text_rec_model = Model(worker.RECOGNITION_MODEL, model_paths[worker.RECOGNITION_MODEL])

            class FakePaddleOCR:
                def __init__(self, **kwargs):
                    captured["kwargs"] = kwargs
                    self.paddlex_pipeline = Inner()

                def predict(self, array):
                    captured["predict_input"] = array
                    return [{
                        "rec_texts": ["中野"],
                        "rec_scores": [0.99],
                        "rec_boxes": [[20, 10, 120, 30]],
                    }]

            paddle_module = types.ModuleType("paddleocr")
            paddle_module.PaddleOCR = FakePaddleOCR
            package_versions = lambda name: worker.PACKAGE_VERSIONS[name]
            fake_modules = {
                "paddleocr": paddle_module,
                "PIL": pil_module,
                "PIL.Image": image_module,
                "numpy": numpy_module,
            }
            with (
                mock.patch.object(worker, "_require_python_312"),
                mock.patch.object(
                    worker,
                    "verify_models",
                    return_value=(model_paths, approved_metadata()),
                ),
                mock.patch.object(worker.metadata, "version", side_effect=package_versions),
                mock.patch.object(
                    worker,
                    "verify_runtime_lock",
                    return_value={
                        "sha256": worker.RUNTIME_LOCK_SHA256,
                        "package_count": 72,
                        "fully_matched": True,
                    },
                ),
                mock.patch.dict(sys.modules, fake_modules),
            ):
                code = worker.main([
                    "--input", str(source),
                    "--output", str(output),
                    "--model-root", str(model_root),
                    "--runtime-lock", str(RUNTIME_LOCK),
                ])

            self.assertEqual(code, 0)
            result = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["lines"][0]["raw_text"], "中野")
            self.assertEqual(result["engine"]["independence_group"], "paddleocr")
            self.assertFalse(result["external_network_used"])
            self.assertFalse(result["downloads_performed"])
            self.assertEqual(output.stat().st_mode & 0o777, 0o600)
            self.assertEqual(captured["input_bytes"], b"image-bytes")
            self.assertEqual(source.read_bytes(), b"image-bytes")
            self.assertEqual(captured["image_mode"], "RGB")
            self.assertEqual(captured["predict_input"], "rgb-array")
            kwargs = captured["kwargs"]
            self.assertEqual(kwargs["device"], "cpu")
            self.assertEqual(kwargs["engine"], "paddle_static")
            self.assertFalse(kwargs["use_doc_orientation_classify"])
            self.assertFalse(kwargs["use_doc_unwarping"])
            self.assertFalse(kwargs["use_textline_orientation"])
            self.assertNotIn("allow_model_download", kwargs)

    def test_network_attempt_is_captured_as_failed_json(self) -> None:
        with tempfile.TemporaryDirectory(prefix="aiec-paddle-network-") as temporary:
            root = Path(temporary)
            model_root, model_paths = create_model_tree(root / "models")
            source = root / "input.png"
            source.write_bytes(b"image-bytes")
            output = root / "failed.json"

            class NetworkPaddleOCR:
                def __init__(self, **_kwargs):
                    socket.socket(socket.AF_INET, socket.SOCK_STREAM)

            paddle_module = types.ModuleType("paddleocr")
            paddle_module.PaddleOCR = NetworkPaddleOCR
            with (
                mock.patch.object(worker, "_require_python_312"),
                mock.patch.object(
                    worker,
                    "verify_models",
                    return_value=(model_paths, approved_metadata()),
                ),
                mock.patch.object(
                    worker.metadata,
                    "version",
                    side_effect=lambda name: worker.PACKAGE_VERSIONS[name],
                ),
                mock.patch.object(
                    worker,
                    "verify_runtime_lock",
                    return_value={
                        "sha256": worker.RUNTIME_LOCK_SHA256,
                        "package_count": 72,
                        "fully_matched": True,
                    },
                ),
                mock.patch.dict(sys.modules, {"paddleocr": paddle_module}),
            ):
                code = worker.main([
                    "--input", str(source),
                    "--output", str(output),
                    "--model-root", str(model_root),
                    "--runtime-lock", str(RUNTIME_LOCK),
                ])

            self.assertEqual(code, 1)
            result = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["error"]["type"], "OfflineNetworkError")
            self.assertEqual(result["lines"], [])
            self.assertFalse(result["downloads_performed"])


if __name__ == "__main__":
    unittest.main()
