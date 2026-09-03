from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[1]
SCRIPTS = REPOSITORY / "scripts"
DATASET = REPOSITORY / "evaluation" / "cross-format-kg-v0.1"
sys.path.insert(0, str(SCRIPTS))

import validate_cross_format_kg_fixture as validator


class CrossFormatKgFixtureContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="aiec-cross-format-kg-")
        self.work = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def copy_contract(self) -> Path:
        target = self.work / "cross-format-kg-v0.1"
        shutil.copytree(
            DATASET,
            target,
            ignore=shutil.ignore_patterns("corpus", validator.CORPUS_MANIFEST),
        )
        return target

    def copy_dataset_with_corpus(self) -> Path:
        target = self.work / "cross-format-kg-v0.1"
        shutil.copytree(DATASET, target)
        return target

    def write_json(self, path: Path, value: dict[str, object]) -> None:
        path.write_text(
            json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )

    def read_jsonl(self, path: Path) -> list[dict[str, object]]:
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def write_jsonl(self, path: Path, records: list[dict[str, object]]) -> None:
        path.write_text(
            "".join(
                json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
                for record in records
            ),
            encoding="utf-8",
        )

    def materialize_dummy_corpus(self, dataset: Path) -> None:
        for index, relative in enumerate(validator.REQUIRED_SOURCE_PATHS, start=1):
            path = dataset / validator.CORPUS_ROOT / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(f"synthetic-source-{index}:{relative}".encode("utf-8"))

    def test_repository_contract_passes_static_validation(self) -> None:
        result = validator.validate_contract(DATASET)
        self.assertEqual("OK", result["status"])
        self.assertEqual(5, result["declared_source_count"])
        self.assertGreaterEqual(result["accepted_case_count"], 1)
        self.assertGreaterEqual(result["hold_case_count"], 1)

    def test_repository_generated_corpus_passes_content_validation(self) -> None:
        result = validator.validate_corpus(DATASET)
        self.assertEqual("OK", result["status"])
        self.assertEqual("PASS", result["corpus_content_validation"])
        self.assertEqual(5, result["corpus_file_count"])
        self.assertTrue(
            all(item["content_contract"] == "PASS" for item in result["files"])
        )
        xlsx = next(item for item in result["files"] if item["format"] == "xlsx")
        self.assertEqual("Assignment History", xlsx["sheet"])
        self.assertEqual("A1:H3", xlsx["validated_range"])
        self.assertEqual(["F2", "G2", "F3"], xlsx["typed_date_cells"])
        self.assertEqual(["G3"], xlsx["blank_date_cells"])

    def test_repository_frozen_manifest_matches_corpus(self) -> None:
        result = validator.validate_manifest(
            DATASET, DATASET / validator.CORPUS_MANIFEST
        )
        self.assertEqual("OK", result["status"])
        self.assertEqual(5, result["file_count"])
        self.assertRegex(result["source_set_sha256"], r"^[0-9a-f]{64}$")

    def test_corpus_content_requires_declared_exact_phrase(self) -> None:
        dataset = self.copy_dataset_with_corpus()
        spec_path = dataset / validator.FIXTURE_SPEC
        spec = json.loads(spec_path.read_text(encoding="utf-8"))
        spec["corpus_contract"]["required_files"][0][
            "must_contain_exact"
        ].append("THIS PHRASE IS NOT IN THE DOCX")
        self.write_json(spec_path, spec)
        with self.assertRaisesRegex(
            validator.FixtureContractError, "missing required exact phrases"
        ):
            validator.validate_corpus(dataset)

    def test_corpus_content_rejects_forbidden_exact_phrase(self) -> None:
        dataset = self.copy_dataset_with_corpus()
        spec_path = dataset / validator.FIXTURE_SPEC
        spec = json.loads(spec_path.read_text(encoding="utf-8"))
        spec["corpus_contract"]["required_files"][0][
            "must_not_contain_exact"
        ].append("ORION-27")
        self.write_json(spec_path, spec)
        with self.assertRaisesRegex(
            validator.FixtureContractError, "forbidden exact phrases were present"
        ):
            validator.validate_corpus(dataset)

    def test_xlsx_dates_must_be_typed_not_iso_text(self) -> None:
        from openpyxl import load_workbook

        dataset = self.copy_dataset_with_corpus()
        workbook_path = (
            dataset / validator.CORPUS_ROOT / validator.REQUIRED_SOURCE_PATHS[1]
        )
        workbook = load_workbook(workbook_path)
        workbook["Assignment History"]["F2"] = "2021-04-01"
        workbook.save(workbook_path)
        workbook.close()
        with self.assertRaisesRegex(
            validator.FixtureContractError, "must be an XLSX typed date"
        ):
            validator.validate_corpus(dataset)

    def test_corpus_content_rejects_empty_artifact(self) -> None:
        dataset = self.copy_dataset_with_corpus()
        source = dataset / validator.CORPUS_ROOT / validator.REQUIRED_SOURCE_PATHS[4]
        source.write_bytes(b"")
        with self.assertRaisesRegex(validator.FixtureContractError, "file is empty"):
            validator.validate_corpus(dataset)

    def test_build_inputs_must_be_exactly_corpus_glob(self) -> None:
        dataset = self.copy_contract()
        spec_path = dataset / validator.FIXTURE_SPEC
        spec = json.loads(spec_path.read_text(encoding="utf-8"))
        spec["input_boundary"]["build_and_index_inputs"].append("gold/**")
        self.write_json(spec_path, spec)
        with self.assertRaisesRegex(validator.FixtureContractError, "exactly corpus"):
            validator.validate_contract(dataset)

    def test_gold_reference_must_name_a_declared_corpus_file(self) -> None:
        dataset = self.copy_contract()
        gold_path = dataset / validator.EXPECTED_GRAPH
        records = self.read_jsonl(gold_path)
        records[0]["source_references"][0]["path"] = "project-orion/undeclared.docx"
        self.write_jsonl(gold_path, records)
        with self.assertRaisesRegex(validator.FixtureContractError, "undeclared corpus path"):
            validator.validate_contract(dataset)

    def test_accepted_case_requires_two_documents_and_required_edges(self) -> None:
        dataset = self.copy_contract()
        qa_path = dataset / validator.QA_CASES
        records = self.read_jsonl(qa_path)
        accepted = next(item for item in records if item["expected"]["decision"] == "ACCEPTED")
        accepted["graph_requirements"]["minimum_distinct_visited_documents"] = 1
        self.write_jsonl(qa_path, records)
        with self.assertRaisesRegex(validator.FixtureContractError, "at least two"):
            validator.validate_contract(dataset)

    def test_accepted_case_ablation_must_hold(self) -> None:
        dataset = self.copy_contract()
        qa_path = dataset / validator.QA_CASES
        records = self.read_jsonl(qa_path)
        accepted = next(item for item in records if item["expected"]["decision"] == "ACCEPTED")
        accepted["graph_requirements"]["edge_ablation"]["expected_decision"] = "ACCEPTED"
        self.write_jsonl(qa_path, records)
        with self.assertRaisesRegex(validator.FixtureContractError, "ablation must produce HOLD"):
            validator.validate_contract(dataset)

    def test_reference_time_hold_case_is_required(self) -> None:
        dataset = self.copy_contract()
        qa_path = dataset / validator.QA_CASES
        records = [
            item for item in self.read_jsonl(qa_path)
            if item["expected"]["decision"] != "HOLD"
        ]
        self.write_jsonl(qa_path, records)
        with self.assertRaisesRegex(validator.FixtureContractError, "at least one HOLD"):
            validator.validate_contract(dataset)

    def test_manifest_round_trip_and_tamper_detection(self) -> None:
        dataset = self.copy_contract()
        self.materialize_dummy_corpus(dataset)
        manifest = dataset / "corpus-manifest.json"
        written = validator.write_manifest(dataset, manifest)
        result = validator.validate_manifest(dataset, manifest)
        self.assertEqual(5, result["file_count"])
        self.assertEqual(written["source_set_sha256"], result["source_set_sha256"])

        source = dataset / validator.CORPUS_ROOT / validator.REQUIRED_SOURCE_PATHS[0]
        source.write_bytes(source.read_bytes() + b"tampered")
        with self.assertRaisesRegex(validator.FixtureContractError, "does not match"):
            validator.validate_manifest(dataset, manifest)

    def test_manifest_rejects_extra_corpus_files(self) -> None:
        dataset = self.copy_contract()
        self.materialize_dummy_corpus(dataset)
        extra = dataset / validator.CORPUS_ROOT / "project-orion" / "preview.inspect.ndjson"
        extra.write_text("{}\n", encoding="utf-8")
        with self.assertRaisesRegex(validator.FixtureContractError, "extra="):
            validator.build_manifest_record(dataset)

    def test_manifest_must_remain_outside_corpus(self) -> None:
        dataset = self.copy_contract()
        self.materialize_dummy_corpus(dataset)
        manifest = dataset / validator.CORPUS_ROOT / "corpus-manifest.json"
        with self.assertRaisesRegex(validator.FixtureContractError, "outside corpus"):
            validator.write_manifest(dataset, manifest)


if __name__ == "__main__":
    unittest.main()
