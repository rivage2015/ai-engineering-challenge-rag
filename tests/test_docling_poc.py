import copy
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from PIL import Image


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "run_docling_poc", ROOT / "scripts" / "run_docling_poc.py"
)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64


def fixture(origin_kind, asset_id, fixture_suffix, materialized_path, page_number):
    return {
        "schema_version": "0.1",
        "record_type": "ocr_poc_fixture",
        "fixture_id": "ocrfx_" + fixture_suffix * 24,
        "asset_ref": {
            "asset_id": asset_id,
            "materialized_path": materialized_path,
            "image_sha256": SHA_A if origin_kind == "pdf_page" else SHA_B,
            "dimensions": {"width_px": 100, "height_px": 200},
            "source_relative_path": "source/input.pdf" if page_number else "source/input.docx",
            "source_sha256": SHA_C,
            "origin_kind": origin_kind,
            "page_number": page_number,
        },
        "crop": {
            "bbox": [0, 0, 1000, 1000],
            "purpose": "table_cell",
            "writing_mode": "horizontal",
        },
        "strata": {
            "document_family": "scan_pdf" if page_number else "office_embedded",
            "difficulty": "medium",
            "routes": ["ocr_text", "table_structure"],
        },
        "reference": {
            "status": "verified",
            "raw_text": "cell",
            "important_spans": ["cell"],
            "verification_method": "human_visual_transcription",
            "reviewer_count": 1,
            "notes": ["synthetic contract fixture"],
        },
        "hashes": {"signature_sha256": fixture_suffix * 64},
        "provenance": {
            "question_independent": True,
            "question_data_used": False,
            "answer_data_used": False,
            "prediction_data_used": False,
        },
    }


def sample():
    value = {
        "role": "image_only_pdf_page",
        "fixture_refs": [
            {"fixture_id": "ocrfx_" + "1" * 24, "signature_sha256": "1" * 64}
        ],
        "materialized_path": "artifacts/image.png",
        "image_sha256": SHA_A,
        "dimensions": {"width_px": 100, "height_px": 200},
        "origin_kind": "pdf_page",
        "source_relative_path": "source/input.pdf",
        "source_sha256": SHA_C,
        "page_number": 1,
    }
    value["sample_id"] = "docsrc_" + MODULE.sha256_json(value)[:24]
    return value


def configuration():
    versions = {
        "docling": "2.115.0",
        "docling_core": "2.91.0",
        "docling_ibm_models": "3.14.0",
        "docling_parse": "7.13.0",
        "torch": "2.10.0",
        "torchvision": "0.25.0",
        "tesseract": "tesseract 5.5.2",
    }
    model = {
        "repo_id": "docling-project/docling-layout-heron",
        "revision": "main",
        "relative_path": "docling-project--docling-layout-heron",
        "file_count": 1,
        "size_bytes": 1,
        "sha256": SHA_A,
    }
    table = dict(model)
    table.update(
        {
            "repo_id": "docling-project/docling-models",
            "revision": "v2.3.0",
            "relative_path": "docling-project--docling-models/model_artifacts/tableformer/accurate",
            "sha256": SHA_B,
        }
    )
    return {
        "runner_version": "0.2",
        "python_version": "3.12.13",
        "platform": "macOS-arm64",
        "num_threads": 4,
        "package_versions": versions,
        "package_fingerprint_sha256": "0" * 64,
        "pipeline": {
            "input_format": "image",
            "pipeline": "standard_pdf_pipeline",
            "remote_services": False,
            "external_plugins": False,
            "ocr": {
                "enabled": True,
                "engine": "tesseract_cli",
                "languages": ["jpn", "eng"],
                "force_full_page": True,
            },
            "layout": {
                "enabled": True,
                "model": "docling-project/docling-layout-heron",
                "revision": "main",
                "device_requested": "mps",
            },
            "table_structure": {
                "enabled": True,
                "model": "docling-project/docling-models",
                "revision": "v2.3.0",
                "mode": "accurate",
                "cell_matching": True,
                "device_requested": "mps",
                "device_effective": "cpu",
                "mps_forced_cpu": True,
                "fallback_reason": "Docling forces TableFormer to CPU on MPS.",
            },
        },
        "models": {"layout": model, "tableformer": table},
    }


def valid_fixture(repository_root, materialized_path="artifacts/input.png"):
    image_path = repository_root / materialized_path
    image_path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (100, 200), "white").save(image_path)
    record = {
        "schema_version": "0.1",
        "record_type": "ocr_poc_fixture",
        "fixture_id": "ocrfx_" + "0" * 24,
        "asset_ref": {
            "asset_id": "asset_" + "a" * 32,
            "materialized_path": materialized_path,
            "image_sha256": MODULE.sha256_file(image_path),
            "dimensions": {"width_px": 100, "height_px": 200},
            "source_relative_path": "source/input.pdf",
            "source_sha256": SHA_C,
            "origin_kind": "pdf_page",
            "page_number": 1,
        },
        "crop": {
            "bbox": [0, 0, 1000, 1000],
            "purpose": "table_cell",
            "writing_mode": "horizontal",
        },
        "strata": {
            "document_family": "scan_pdf",
            "difficulty": "medium",
            "routes": ["ocr_text", "table_structure"],
        },
        "reference": {
            "status": "verified",
            "raw_text": "cell",
            "important_spans": ["cell"],
            "verification_method": "human_visual_transcription",
            "reviewer_count": 1,
            "notes": ["synthetic contract fixture"],
        },
        "hashes": {"signature_sha256": "0" * 64},
        "provenance": {
            "created_at": "2026-08-17T00:00:00+00:00",
            "selection_method": "human-stratified-region-v0.1",
            "question_independent": True,
            "question_data_used": False,
            "answer_data_used": False,
            "prediction_data_used": False,
            "source_data_used": True,
        },
    }
    signature = MODULE.ocr_contract.expected_fixture_signature(record)
    record["hashes"]["signature_sha256"] = signature
    record["fixture_id"] = MODULE.ocr_contract.expected_fixture_id(signature)
    return record


def configuration_with_ocr_engine(engine):
    value = configuration()
    ocr = MODULE._ocr_pipeline_configuration(engine)
    if engine == "tesseract_cli":
        version = "tesseract 5.5.2"
        runtime = version
    else:
        version = "1.0.0"
        runtime = "macOS Vision 26.5.1 (arm64)"
        value["package_versions"].pop("tesseract")
        value["package_versions"]["ocrmac"] = version
    fingerprint_payload = {
        "engine": engine,
        "version": version,
        "runtime": runtime,
        "artifacts_sha256": SHA_C,
        "config_sha256": MODULE.sha256_json(ocr),
    }
    value["pipeline"]["ocr"] = ocr
    value["ocr_engine_fingerprint"] = {
        **fingerprint_payload,
        "fingerprint_sha256": MODULE.sha256_json(fingerprint_payload),
    }
    value["package_fingerprint_sha256"] = MODULE.sha256_json(
        MODULE.package_fingerprint_payload(
            versions=value["package_versions"],
            python_version=value["python_version"],
            platform_value=value["platform"],
            num_threads=value["num_threads"],
            ocr_engine_fingerprint=value["ocr_engine_fingerprint"],
        )
    )
    return value


class FakeDocument:
    def __init__(self):
        bbox = SimpleNamespace(l=1, t=2, r=30, b=40, coord_origin="TOPLEFT")
        prov = SimpleNamespace(page_no=1, bbox=bbox, charspan=(0, 4))
        text = SimpleNamespace(
            label="text", self_ref="#/texts/0", text="cell", prov=[prov]
        )
        cell = SimpleNamespace(
            start_row_offset_idx=0,
            end_row_offset_idx=1,
            start_col_offset_idx=0,
            end_col_offset_idx=1,
            row_span=1,
            col_span=1,
            text="cell",
            column_header=True,
            row_header=False,
            row_section=False,
            bbox=bbox,
        )
        data = SimpleNamespace(num_rows=1, num_cols=1, table_cells=[cell])
        table = SimpleNamespace(
            label="table", self_ref="#/tables/0", text=None, prov=[prov], data=data
        )
        self.name = "synthetic"
        self.tables = [table]
        self.pages = {
            1: SimpleNamespace(
                page_no=1, size=SimpleNamespace(width=100.0, height=200.0)
            )
        }
        self._items = [(text, 1), (table, 1)]

    def export_to_markdown(self):
        return "cell\n\n| cell |"

    def export_to_dict(self, **kwargs):
        return {"schema_name": "DoclingDocument", "name": "synthetic"}

    def iterate_items(self, with_groups=False):
        return iter(self._items)


class DoclingPoCTest(unittest.TestCase):
    def test_selects_two_question_independent_structural_roles(self):
        records = [
            fixture("pdf_page", "asset_pdf", "1", "artifacts/pdf.png", 3),
            fixture(
                "office_embedded_image",
                "asset_office",
                "2",
                "artifacts/table.png",
                None,
            ),
        ]
        selected = MODULE.select_structural_samples(records)
        self.assertEqual(
            [value["role"] for value in selected],
            ["image_only_pdf_page", "office_embedded_table_image"],
        )
        self.assertEqual(len({value["sample_id"] for value in selected}), 2)

    def test_manifest_rejects_symlink_bad_signature_and_forbidden_asset(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            value = valid_fixture(root)
            target = root / "manifest-target.jsonl"
            target.write_text(json.dumps(value) + "\n", encoding="utf-8")
            symlink = root / "manifest.jsonl"
            symlink.symlink_to(target)
            with self.assertRaisesRegex(ValueError, "non-symlink"):
                MODULE.load_verified_manifest(symlink, root)

            bad_signature = copy.deepcopy(value)
            bad_signature["hashes"]["signature_sha256"] = "f" * 64
            invalid = root / "invalid-manifest.jsonl"
            invalid.write_text(json.dumps(bad_signature) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "invalid OCR fixture"):
                MODULE.load_verified_manifest(invalid, root)

            forbidden = valid_fixture(root, "artifacts/questions.png")
            forbidden_path = root / "forbidden-manifest.jsonl"
            forbidden_path.write_text(json.dumps(forbidden) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "prohibited"):
                MODULE.load_verified_manifest(forbidden_path, root)

    def test_tree_fingerprint_changes_with_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            model = root / "model"
            model.mkdir()
            item = model / "weights.bin"
            item.write_bytes(b"abc")
            first = MODULE.fingerprint_tree(
                model, relative_to=root, repo_id="repo/model", revision="main"
            )
            item.write_bytes(b"abd")
            second = MODULE.fingerprint_tree(
                model, relative_to=root, repo_id="repo/model", revision="main"
            )
            self.assertNotEqual(first["sha256"], second["sha256"])

    def test_document_summary_preserves_table_cells_bbox_and_provenance(self):
        summary = MODULE.summarize_document(FakeDocument())
        self.assertEqual(summary["item_counts"]["total"], 2)
        self.assertEqual(summary["item_counts"]["table"], 1)
        self.assertEqual(summary["tables"][0]["cells"][0]["text"], "cell")
        self.assertEqual(
            summary["tables"][0]["cells"][0]["bbox"]["coord_origin"],
            "TOPLEFT",
        )
        self.assertEqual(
            summary["tables"][0]["provenance"][0]["page_number"], 1
        )

    def test_closed_record_roundtrip_and_extra_property_rejection(self):
        record = MODULE.finalize_record(
            sample=sample(),
            configuration=configuration_with_ocr_engine("tesseract_cli"),
            status="completed",
            document=MODULE.summarize_document(FakeDocument()),
            total_ms=12.5,
            stages=[{"stage": "layout", "elapsed_seconds": 0.01, "count": 1}],
            warnings=[],
            errors=[],
        )
        self.assertEqual(MODULE.validate_record(record), [])
        mutated = copy.deepcopy(record)
        mutated["unexpected"] = True
        self.assertTrue(MODULE.validate_record(mutated))

    def test_record_integrity_detects_timing_input_and_package_mutation(self):
        record = MODULE.finalize_record(
            sample=sample(),
            configuration=configuration_with_ocr_engine("tesseract_cli"),
            status="completed",
            document=MODULE.summarize_document(FakeDocument()),
            total_ms=12.5,
            stages=[],
            warnings=[],
            errors=[],
        )
        mutations = []
        timing = copy.deepcopy(record)
        timing["timing"]["total_ms"] += 1
        mutations.append(timing)
        materialized = copy.deepcopy(record)
        materialized["input"]["materialized_path"] = "artifacts/other.png"
        mutations.append(materialized)
        package = copy.deepcopy(record)
        package["configuration"]["package_versions"]["docling"] = "9.9.9"
        mutations.append(package)
        for mutated in mutations:
            self.assertTrue(MODULE.validate_record(mutated))

    def test_run_id_is_stable_while_full_record_integrity_tracks_timing(self):
        common = {
            "sample": sample(),
            "configuration": configuration_with_ocr_engine("tesseract_cli"),
            "status": "completed",
            "document": MODULE.summarize_document(FakeDocument()),
            "stages": [],
            "warnings": [],
            "errors": [],
        }
        first = MODULE.finalize_record(total_ms=10.0, **common)
        second = MODULE.finalize_record(total_ms=20.0, **common)
        self.assertEqual(first["run_id"], second["run_id"])
        self.assertNotEqual(
            first["hashes"]["record_integrity_sha256"],
            second["hashes"]["record_integrity_sha256"],
        )

    def test_completed_empty_document_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "non-empty|minimum"):
            MODULE.finalize_record(
                sample=sample(),
                configuration=configuration_with_ocr_engine("tesseract_cli"),
                status="completed",
                document=MODULE.empty_document(),
                total_ms=1.0,
                stages=[],
                warnings=[],
                errors=[],
            )

    def test_unknown_docling_status_is_not_silently_partial(self):
        with self.assertRaisesRegex(ValueError, "unknown Docling"):
            MODULE._status("future_status")

    def test_ocrmac_closed_contract_and_exact_configuration(self):
        config = configuration_with_ocr_engine("ocrmac")
        record = MODULE.finalize_record(
            sample=sample(),
            configuration=config,
            status="completed",
            document=MODULE.summarize_document(FakeDocument()),
            total_ms=10.0,
            stages=[],
            warnings=[],
            errors=[],
        )
        self.assertEqual(MODULE.validate_record(record), [])
        self.assertEqual(
            config["pipeline"]["ocr"],
            {
                "enabled": True,
                "engine": "ocrmac",
                "languages": ["ja-JP", "en-US"],
                "force_full_page": True,
                "recognition": "accurate",
                "framework": "vision",
            },
        )
        missing_recognition = copy.deepcopy(record)
        missing_recognition["configuration"]["pipeline"]["ocr"].pop(
            "recognition"
        )
        self.assertTrue(MODULE.validate_record(missing_recognition))

    def test_engine_specific_contract_rejects_cross_engine_fields(self):
        config = configuration_with_ocr_engine("tesseract_cli")
        record = MODULE.finalize_record(
            sample=sample(),
            configuration=config,
            status="completed",
            document=MODULE.summarize_document(FakeDocument()),
            total_ms=10.0,
            stages=[],
            warnings=[],
            errors=[],
        )
        bad = copy.deepcopy(record)
        bad["configuration"]["pipeline"]["ocr"]["framework"] = "vision"
        self.assertTrue(MODULE.validate_record(bad))

    def test_ocr_fingerprint_binds_engine_version_and_configuration(self):
        common = {
            "sample": sample(),
            "status": "completed",
            "document": MODULE.summarize_document(FakeDocument()),
            "total_ms": 10.0,
            "stages": [],
            "warnings": [],
            "errors": [],
        }
        tesseract = MODULE.finalize_record(
            configuration=configuration_with_ocr_engine("tesseract_cli"),
            **common,
        )
        ocrmac = MODULE.finalize_record(
            configuration=configuration_with_ocr_engine("ocrmac"), **common
        )
        self.assertNotEqual(tesseract["run_id"], ocrmac["run_id"])
        self.assertNotEqual(
            tesseract["configuration"]["ocr_engine_fingerprint"][
                "fingerprint_sha256"
            ],
            ocrmac["configuration"]["ocr_engine_fingerprint"][
                "fingerprint_sha256"
            ],
        )


if __name__ == "__main__":
    unittest.main()
