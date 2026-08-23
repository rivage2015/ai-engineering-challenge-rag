from __future__ import annotations

import copy
import contextlib
import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from PIL import Image


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import ocr_poc_contract as ocr_contract
import run_pp_doclayout_poc as module


REVISION = "1" * 40
SHA_A = "a" * 64
SHA_B = "b" * 64
STAMP = "2026-08-17T00:00:00+00:00"


def _write_png(path: Path, size: tuple[int, int]) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, "white").save(path)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _fixture(
    *,
    repository: Path,
    asset_id: str,
    image_relative_path: str,
    image_sha256: str,
    dimensions: tuple[int, int],
    origin_kind: str,
    page_number: int | None,
    purpose: str,
    source_suffix: str,
) -> dict[str, object]:
    document_family = "scan_pdf" if origin_kind == "pdf_page" else "office_embedded"
    record: dict[str, object] = {
        "schema_version": "0.1",
        "record_type": "ocr_poc_fixture",
        "fixture_id": "ocrfx_" + "0" * 24,
        "asset_ref": {
            "asset_id": asset_id,
            "materialized_path": image_relative_path,
            "image_sha256": image_sha256,
            "dimensions": {
                "width_px": dimensions[0],
                "height_px": dimensions[1],
            },
            "source_relative_path": f"opaque/{source_suffix}",
            "source_sha256": SHA_A if origin_kind == "pdf_page" else SHA_B,
            "origin_kind": origin_kind,
            "page_number": page_number,
        },
        "crop": {
            "bbox": [100, 100, 200, 100],
            "purpose": purpose,
            "writing_mode": "horizontal",
        },
        "strata": {
            "document_family": document_family,
            "difficulty": "hard" if purpose == "table_cell" else "medium",
            "routes": ["ocr_text", "table_structure"],
        },
        "reference": {
            "status": "verified",
            "raw_text": purpose,
            "important_spans": [purpose],
            "verification_method": "human_visual_transcription",
            "reviewer_count": 1,
            "notes": ["synthetic structural fixture"],
        },
        "hashes": {"signature_sha256": "0" * 64},
        "provenance": {
            "created_at": STAMP,
            "selection_method": "human-stratified-region-v0.1",
            "question_independent": True,
            "question_data_used": False,
            "answer_data_used": False,
            "prediction_data_used": False,
            "source_data_used": True,
        },
    }
    signature = ocr_contract.expected_fixture_signature(record)
    record["hashes"]["signature_sha256"] = signature
    record["fixture_id"] = ocr_contract.expected_fixture_id(signature)
    return record


def _write_manifest(path: Path, records: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(module.canonical_json(record) + "\n" for record in records),
        encoding="utf-8",
    )


def _model(repository: Path) -> tuple[Path, str]:
    model_dir = repository / "artifacts" / "pp-doclayout-poc" / "model"
    model_dir.mkdir(parents=True)
    (model_dir / "config.json").write_text(
        json.dumps({"model_type": "pp_doclayout_v3"}), encoding="utf-8"
    )
    (model_dir / "preprocessor_config.json").write_text(
        json.dumps({"image_processor_type": "PPDocLayoutV3ImageProcessor"}),
        encoding="utf-8",
    )
    weights = model_dir / "model.safetensors"
    weights.write_bytes(b"synthetic safetensors payload")
    return model_dir, hashlib.sha256(weights.read_bytes()).hexdigest()


def _packages() -> dict[str, str]:
    return {
        "torch": "2.13.0",
        "torchvision": "0.28.0",
        "transformers": "5.14.0",
        "safetensors": "0.8.0",
        "numpy": "2.5.1",
        "opencv_python": "4.13.0.92",
        "pillow": "12.3.0",
        "jsonschema": "4.26.0",
        "huggingface_hub": "1.24.0",
    }


def _raw_prediction() -> dict[str, object]:
    return {
        "scores": [0.91, 0.82],
        "labels": [4, 2],
        "boxes": [[10.0, 20.0, 90.0, 70.0], [100.0, 40.0, 180.0, 95.0]],
        "polygon_points": [
            [10.0, 20.0, 90.0, 20.0, 90.0, 70.0, 10.0, 70.0],
            [[100.0, 40.0], [180.0, 40.0], [180.0, 95.0], [100.0, 95.0]],
        ],
        "order_seq": [17, 3],
    }


class FakeLoader:
    calls: list[tuple[str, str, dict[str, object]]] = []

    @classmethod
    def from_pretrained(cls, path: str, **kwargs: object) -> object:
        cls.calls.append((cls.__name__, path, dict(kwargs)))
        if cls.__name__ == "FakeModelLoader":
            return FakeModel()
        return object()


class FakeProcessorLoader(FakeLoader):
    pass


class FakeModelLoader(FakeLoader):
    pass


class FakeModel:
    def __init__(self) -> None:
        self.device = None
        self.eval_called = False

    def to(self, device: str) -> "FakeModel":
        self.device = device
        return self

    def eval(self) -> "FakeModel":
        self.eval_called = True
        return self


class FakeBackend:
    def __init__(self, fail_role: str | None = None) -> None:
        self.fail_role = fail_role

    def predict(
        self,
        image_path: Path,
        *,
        threshold: float,
        role: str,
        dimensions: dict[str, int],
    ) -> dict[str, object]:
        del image_path, threshold
        if role == self.fail_role:
            raise RuntimeError("synthetic inference failure")
        return module.normalize_prediction(
            _raw_prediction(),
            id2label={2: "Table", 4: "Text"},
            dimensions=dimensions,
        )


class FakeMPSLikeTensor:
    def __init__(self, value: object, *, device: str, floating: bool, dtype: str) -> None:
        self.value = value
        self.device = device
        self.floating = floating
        self.dtype = dtype

    def detach(self) -> "FakeMPSLikeTensor":
        return self

    def is_floating_point(self) -> bool:
        return self.floating

    def to(self, *, device: str, dtype: str) -> "FakeMPSLikeTensor":
        return FakeMPSLikeTensor(
            self.value,
            device=device,
            floating=self.floating,
            dtype=dtype,
        )


class FakeModelOutput(dict):
    def __init__(self, **values: object) -> None:
        super().__init__(values)
        for key, value in values.items():
            setattr(self, key, value)


class FakeTensorBatch(dict):
    def to(self, device: str) -> "FakeTensorBatch":
        return FakeTensorBatch(
            {
                key: FakeMPSLikeTensor(
                    value.value,
                    device=device,
                    floating=value.floating,
                    dtype=value.dtype,
                )
                for key, value in self.items()
            }
        )


def _fake_tensors(value: object) -> list[FakeMPSLikeTensor]:
    if isinstance(value, FakeMPSLikeTensor):
        return [value]
    if isinstance(value, dict):
        return [tensor for item in value.values() for tensor in _fake_tensors(item)]
    if isinstance(value, (list, tuple)):
        return [tensor for item in value for tensor in _fake_tensors(item)]
    return []


class FakeMPSModel:
    def __init__(self) -> None:
        self.config = SimpleNamespace(id2label={2: "Table", 4: "Text"})
        self.forward_input_devices: list[str] = []

    def __call__(self, **inputs: object) -> FakeModelOutput:
        self.forward_input_devices = [
            tensor.device for value in inputs.values() for tensor in _fake_tensors(value)
        ]
        return FakeModelOutput(
            logits=FakeMPSLikeTensor(
                [[0.1, 0.9]], device="mps", floating=True, dtype="float64"
            ),
            pred_boxes=FakeMPSLikeTensor(
                [[0.1, 0.2, 0.3, 0.4]],
                device="mps",
                floating=True,
                dtype="float16",
            ),
            auxiliary_outputs=[
                {
                    "labels": FakeMPSLikeTensor(
                        [4], device="mps", floating=False, dtype="int32"
                    )
                }
            ],
        )


class FakeMPSProcessor:
    def __init__(self) -> None:
        self.postprocess_tensors: list[FakeMPSLikeTensor] = []
        self.target_sizes: FakeMPSLikeTensor | None = None

    def __call__(self, *, images: Image.Image, return_tensors: str) -> FakeTensorBatch:
        del images
        if return_tensors != "pt":
            raise AssertionError("expected PyTorch tensors")
        return FakeTensorBatch(
            pixel_values=FakeMPSLikeTensor(
                [[1.0]], device="cpu", floating=True, dtype="float32"
            )
        )

    def post_process_object_detection(
        self,
        outputs: FakeModelOutput,
        *,
        threshold: float,
        target_sizes: FakeMPSLikeTensor,
    ) -> list[dict[str, object]]:
        del threshold
        self.postprocess_tensors = _fake_tensors(outputs)
        self.target_sizes = target_sizes
        if not self.postprocess_tensors:
            raise AssertionError("expected model-output tensors")
        if any(tensor.device != "cpu" for tensor in self.postprocess_tensors):
            raise AssertionError("MPS tensor reached post-processing")
        for tensor in self.postprocess_tensors:
            expected = "float32" if tensor.floating else "int64"
            if tensor.dtype != expected:
                raise AssertionError(f"unexpected post-processing dtype: {tensor.dtype}")
        if target_sizes.device != "cpu" or target_sizes.dtype != "int64":
            raise AssertionError("target_sizes must be an explicit CPU int64 tensor")
        return [_raw_prediction()]


class FakeTorchAPI:
    float32 = "float32"
    int64 = "int64"

    @staticmethod
    def is_tensor(value: object) -> bool:
        return isinstance(value, FakeMPSLikeTensor)

    @staticmethod
    def inference_mode() -> contextlib.AbstractContextManager[None]:
        return contextlib.nullcontext()

    @staticmethod
    def tensor(value: object, *, device: str, dtype: str) -> FakeMPSLikeTensor:
        return FakeMPSLikeTensor(value, device=device, floating=False, dtype=dtype)


class PPDocLayoutPoCTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="aiec-pp-doclayout-")
        self.repository = Path(self.temporary.name)
        complex_rel = "artifacts/images/complex.png"
        clean_rel = "artifacts/images/clean.png"
        complex_sha = _write_png(self.repository / complex_rel, (200, 120))
        clean_sha = _write_png(self.repository / clean_rel, (220, 130))
        self.records = [
            _fixture(
                repository=self.repository,
                asset_id="asset_" + "1" * 32,
                image_relative_path=complex_rel,
                image_sha256=complex_sha,
                dimensions=(200, 120),
                origin_kind="pdf_page",
                page_number=3,
                purpose="table_header",
                source_suffix="complex.pdf",
            ),
            _fixture(
                repository=self.repository,
                asset_id="asset_" + "1" * 32,
                image_relative_path=complex_rel,
                image_sha256=complex_sha,
                dimensions=(200, 120),
                origin_kind="pdf_page",
                page_number=3,
                purpose="table_cell",
                source_suffix="complex.pdf",
            ),
            _fixture(
                repository=self.repository,
                asset_id="asset_" + "2" * 32,
                image_relative_path=clean_rel,
                image_sha256=clean_sha,
                dimensions=(220, 130),
                origin_kind="office_embedded_image",
                page_number=None,
                purpose="table_header",
                source_suffix="clean.docx",
            ),
            _fixture(
                repository=self.repository,
                asset_id="asset_" + "2" * 32,
                image_relative_path=clean_rel,
                image_sha256=clean_sha,
                dimensions=(220, 130),
                origin_kind="office_embedded_image",
                page_number=None,
                purpose="table_cell",
                source_suffix="clean.docx",
            ),
        ]
        self.manifest = self.repository / "artifacts" / "manifest.verified.jsonl"
        _write_manifest(self.manifest, self.records)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _loaded_and_samples(self):
        loaded = module.load_verified_manifest(self.manifest, self.repository)
        samples = module.select_poc_samples(
            loaded,
            manifest_path=self.manifest,
            repository_root=self.repository,
        )
        return loaded, samples

    def _configuration(self) -> dict[str, object]:
        model_dir, weight_sha = _model(self.repository)
        fingerprint = module.fingerprint_local_model(
            model_dir,
            repository_root=self.repository,
            revision=REVISION,
            expected_weight_sha256=weight_sha,
        )
        return module.build_configuration(
            model=fingerprint,
            packages=_packages(),
            device_requested="mps",
            device_effective="mps",
            threshold=0.5,
            python_version="3.12.13",
            platform_value="macOS-arm64",
        )

    def test_manifest_selects_complex_and_clean_pages_without_question_data(self) -> None:
        loaded, samples = self._loaded_and_samples()
        self.assertEqual(4, len(loaded))
        self.assertEqual(
            ["complex_pdf_page", "clean_table_page"],
            [sample["role"] for sample in samples],
        )
        self.assertEqual(
            ["asset_" + "1" * 32, "asset_" + "2" * 32],
            [sample["asset_id"] for sample in samples],
        )
        self.assertEqual([2, 2], [len(sample["fixture_refs"]) for sample in samples])
        self.assertTrue(all(sample["sample_id"].startswith("ppsrc_") for sample in samples))

    def test_manifest_rejects_symlink_tampering_and_forbidden_name(self) -> None:
        linked = self.repository / "artifacts" / "linked-manifest.jsonl"
        linked.symlink_to(self.manifest)
        with self.assertRaisesRegex(ValueError, "non-symlink"):
            module.load_verified_manifest(linked, self.repository)

        tampered = copy.deepcopy(self.records)
        tampered[0]["hashes"]["signature_sha256"] = "f" * 64
        bad = self.repository / "artifacts" / "bad-manifest.jsonl"
        _write_manifest(bad, tampered)
        with self.assertRaisesRegex(ValueError, "invalid OCR fixture"):
            module.load_verified_manifest(bad, self.repository)

        forbidden = self.repository / "artifacts" / "gold-manifest.jsonl"
        _write_manifest(forbidden, self.records)
        with self.assertRaisesRegex(ValueError, "prohibited"):
            module.load_verified_manifest(forbidden, self.repository)

    def test_manifest_rejects_image_hash_mismatch_and_duplicate_json_key(self) -> None:
        image = self.repository / self.records[0]["asset_ref"]["materialized_path"]
        image.write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "invalid OCR fixture"):
            module.load_verified_manifest(self.manifest, self.repository)

        duplicate = self.repository / "artifacts" / "duplicate-manifest.jsonl"
        duplicate.write_text('{"record_type":"x","record_type":"y"}\n', encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "duplicate JSON key"):
            module.load_verified_manifest(duplicate, self.repository)

    def test_model_fingerprint_is_local_pinned_and_weight_bound(self) -> None:
        model_dir, weight_sha = _model(self.repository)
        fingerprint = module.fingerprint_local_model(
            model_dir,
            repository_root=self.repository,
            revision=REVISION,
            expected_weight_sha256=weight_sha,
        )
        self.assertEqual(REVISION, fingerprint["revision"])
        self.assertEqual(weight_sha, fingerprint["weights_sha256"])
        self.assertEqual(3, fingerprint["file_count"])
        self.assertEqual(
            ["config.json", "model.safetensors", "preprocessor_config.json"],
            [item["relative_path"] for item in fingerprint["files"]],
        )
        with self.assertRaisesRegex(ValueError, "weight hash mismatch"):
            module.fingerprint_local_model(
                model_dir,
                repository_root=self.repository,
                revision=REVISION,
                expected_weight_sha256="f" * 64,
            )
        (model_dir / "config.json").unlink()
        with self.assertRaisesRegex(ValueError, "required model file"):
            module.fingerprint_local_model(
                model_dir,
                repository_root=self.repository,
                revision=REVISION,
                expected_weight_sha256=weight_sha,
            )

    def test_offline_environment_and_local_loader_forbid_implicit_download(self) -> None:
        with self.assertRaisesRegex(ValueError, "HF_HUB_OFFLINE"):
            module.require_offline_environment({})
        offline = {
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
            "HF_DATASETS_OFFLINE": "1",
        }
        module.require_offline_environment(offline)

        FakeLoader.calls.clear()
        api = SimpleNamespace(
            AutoImageProcessor=FakeProcessorLoader,
            AutoModelForObjectDetection=FakeModelLoader,
        )
        torch_api = SimpleNamespace(
            backends=SimpleNamespace(
                mps=SimpleNamespace(is_available=lambda: True)
            )
        )
        model_dir, _ = _model(self.repository)
        processor, model = module.load_local_components(
            model_dir,
            device="mps",
            environ=offline,
            transformers_api=api,
            torch_api=torch_api,
        )
        self.assertIsNotNone(processor)
        self.assertEqual("mps", model.device)
        self.assertTrue(model.eval_called)
        self.assertEqual(2, len(FakeLoader.calls))
        for _, path, kwargs in FakeLoader.calls:
            self.assertEqual(str(model_dir.resolve()), path)
            self.assertIs(True, kwargs["local_files_only"])
            self.assertIs(False, kwargs["trust_remote_code"])
            self.assertNotIn("revision", kwargs)

    def test_prediction_preserves_raw_bbox_polygon_and_reading_order(self) -> None:
        prediction = module.normalize_prediction(
            _raw_prediction(),
            id2label={2: "Table", 4: "Text"},
            dimensions={"width_px": 220, "height_px": 130},
        )
        self.assertEqual([1, 2], prediction["raw_output"]["result_order"])
        self.assertEqual(
            _raw_prediction()["order_seq"],
            prediction["raw_output"]["order_seq"],
        )
        self.assertEqual([1, 2], [item["order"] for item in prediction["detections"]])
        self.assertEqual(17, prediction["detections"][0]["raw"]["order_seq"])
        self.assertEqual("Text", prediction["detections"][0]["label"])
        self.assertEqual(
            _raw_prediction()["polygon_points"],
            prediction["raw_output"]["polygon_points"],
        )
        self.assertEqual(
            [[10.0, 20.0], [90.0, 20.0], [90.0, 70.0], [10.0, 70.0]],
            prediction["detections"][0]["polygon_points"],
        )

    def test_mps_forward_moves_all_outputs_to_cpu_before_postprocess(self) -> None:
        processor = FakeMPSProcessor()
        model = FakeMPSModel()
        backend = module.TransformersPPDocLayoutBackend(
            processor=processor,
            model=model,
            torch_api=FakeTorchAPI,
            device="mps",
        )
        image_path = self.repository / self.records[0]["asset_ref"]["materialized_path"]
        prediction = backend.predict(
            image_path,
            threshold=0.5,
            role="complex_pdf_page",
            dimensions={"width_px": 200, "height_px": 120},
        )
        self.assertEqual(["mps"], model.forward_input_devices)
        self.assertTrue(processor.postprocess_tensors)
        self.assertTrue(
            all(tensor.device == "cpu" for tensor in processor.postprocess_tensors)
        )
        self.assertIsNotNone(processor.target_sizes)
        self.assertEqual("cpu", processor.target_sizes.device)
        self.assertEqual("int64", processor.target_sizes.dtype)
        self.assertEqual([17, 3], prediction["raw_output"]["order_seq"])

    def test_closed_record_integrity_and_semantic_alignment(self) -> None:
        _, samples = self._loaded_and_samples()
        configuration = self._configuration()
        prediction = module.normalize_prediction(
            _raw_prediction(),
            id2label={2: "Table", 4: "Text"},
            dimensions=samples[0]["dimensions"],
        )
        record = module.finalize_record(
            sample=samples[0],
            configuration=configuration,
            prediction=prediction,
            setup_ms=1.0,
            inference_ms=2.0,
            generated_at=STAMP,
        )
        self.assertEqual([], module.validate_record(record))
        self.assertTrue(record["run_id"].startswith("ppdlpoc_"))
        self.assertEqual("mps", record["configuration"]["inference_device"])
        self.assertEqual("cpu", record["configuration"]["postprocess_device"])
        self.assertEqual("mps", record["provenance"]["inference_device"])
        self.assertEqual("cpu", record["provenance"]["postprocess_device"])

        extra = copy.deepcopy(record)
        extra["unexpected"] = True
        self.assertTrue(module.validate_record(extra))

        tampered = copy.deepcopy(record)
        tampered["detections"][0]["order"] = 2
        self.assertTrue(any("order" in error for error in module.validate_record(tampered)))

        integrity = copy.deepcopy(record)
        integrity["timing"]["inference_ms"] = 999.0
        self.assertTrue(
            any("record_integrity_sha256" in error for error in module.validate_record(integrity))
        )

    def test_mock_run_records_each_failure_without_connecting_mainline(self) -> None:
        _, samples = self._loaded_and_samples()
        configuration = self._configuration()
        records = module.run_samples(
            samples,
            repository_root=self.repository,
            backend=FakeBackend(fail_role="clean_table_page"),
            configuration=configuration,
            generated_at=STAMP,
        )
        self.assertEqual(["completed", "failed"], [item["status"] for item in records])
        self.assertEqual([], module.validate_record(records[0]))
        self.assertEqual([], module.validate_record(records[1]))
        self.assertFalse(records[0]["provenance"]["evidence_connected"])
        self.assertFalse(records[0]["provenance"]["search_unit_connected"])
        self.assertIn("synthetic inference failure", records[1]["error"]["message"])


if __name__ == "__main__":
    unittest.main()
