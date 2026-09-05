from __future__ import annotations

import hashlib
import io
import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

import numpy as np


REPOSITORY = Path(__file__).resolve().parents[1]
SCRIPTS = REPOSITORY / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(REPOSITORY / "rag"))

import build_chart_intermediate
import build_intermediate_records
import build_layer1_deliverables
import build_lexical_index
import build_search_units
import build_semantic_index
import evaluate_lexical_retrieval
import probe_intermediate_records
import retrieval_trace_common
import search_semantic_index
import search_lexical_index
import validate_intermediate_records_streaming
import validate_layer1_deliverables
import validate_lexical_index
import validate_search_units
import validate_search_units_streaming
import validate_semantic_index
import layer1_index


RUN_AT = "2026-08-15T00:00:00+00:00"
MODEL = {
    "requested": "embeddinggemma",
    "resolved": "embeddinggemma:latest",
    "digest": "a" * 64,
}

OOXML_REQUIRED = frozenset({
    "[Content_Types].xml",
    "xl/workbook.xml",
    "xl/_rels/workbook.xml.rels",
})


def write_unsafe_ooxml(
    target: Path | io.BytesIO,
    *,
    required_members: frozenset[str],
    unsafe_member: str,
) -> None:
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name in sorted(required_members):
            value = (
                '<!DOCTYPE root [<!ENTITY unsafe "entity">]><root/>'
                if name == unsafe_member
                else "<root/>"
            )
            archive.writestr(name, value)
    if isinstance(target, io.BytesIO):
        target.seek(0)


def write_minimal_xlsx(
    path: Path,
    *,
    numeric_lexeme: str = "0.123456789012345678901234567890",
    unsafe_workbook_xml: bool = False,
    workbook_xml_bytes: bytes | None = None,
) -> None:
    workbook_prefix = '<!DOCTYPE workbook [<!ENTITY unsafe "entity">]>' if unsafe_workbook_xml else ""
    members = {
        "[Content_Types].xml": (
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>'
        ),
        "xl/workbook.xml": workbook_xml_bytes or (workbook_prefix + (
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            '<sheets><sheet name="Precise" sheetId="1" r:id="rId1"/></sheets></workbook>'
        )),
        "xl/_rels/workbook.xml.rels": (
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
            'Target="worksheets/sheet1.xml"/></Relationships>'
        ),
        "xl/styles.xml": (
            '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            '<cellXfs count="1"><xf numFmtId="0"/></cellXfs></styleSheet>'
        ),
        "xl/worksheets/sheet1.xml": (
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            f'<sheetData><row r="1"><c r="A1"><v>{numeric_lexeme}</v></c></row></sheetData>'
            '</worksheet>'
        ),
    }
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, value in members.items():
            archive.writestr(name, value)


def write_cached_formula_xlsx(path: Path) -> None:
    """Write two sheets with formula text and saved, not recalculated values."""
    spreadsheet = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    office_relationships = (
        "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    )
    package_relationships = (
        "http://schemas.openxmlformats.org/package/2006/relationships"
    )
    members = {
        "[Content_Types].xml": (
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
            '<Override PartName="/xl/worksheets/sheet2.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
            '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
            '</Types>'
        ),
        "_rels/.rels": (
            f'<Relationships xmlns="{package_relationships}">'
            f'<Relationship Id="rId1" Type="{office_relationships}/officeDocument" Target="xl/workbook.xml"/>'
            '</Relationships>'
        ),
        "xl/workbook.xml": (
            f'<workbook xmlns="{spreadsheet}" xmlns:r="{office_relationships}">'
            '<sheets>'
            '<sheet name="マスター" sheetId="1" r:id="rId1"/>'
            '<sheet name="集計表マスター" sheetId="2" r:id="rId2"/>'
            '</sheets></workbook>'
        ),
        "xl/_rels/workbook.xml.rels": (
            f'<Relationships xmlns="{package_relationships}">'
            f'<Relationship Id="rId1" Type="{office_relationships}/worksheet" Target="worksheets/sheet1.xml"/>'
            f'<Relationship Id="rId2" Type="{office_relationships}/worksheet" Target="worksheets/sheet2.xml"/>'
            f'<Relationship Id="rId3" Type="{office_relationships}/styles" Target="styles.xml"/>'
            '</Relationships>'
        ),
        "xl/styles.xml": (
            f'<styleSheet xmlns="{spreadsheet}">'
            '<fonts count="1"><font><sz val="11"/><name val="Arial"/></font></fonts>'
            '<fills count="2"><fill><patternFill patternType="none"/></fill>'
            '<fill><patternFill patternType="gray125"/></fill></fills>'
            '<borders count="1"><border/></borders>'
            '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
            '<cellXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/></cellXfs>'
            '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
            '</styleSheet>'
        ),
        "xl/worksheets/sheet1.xml": (
            f'<worksheet xmlns="{spreadsheet}"><dimension ref="B7:C9"/><sheetData>'
            '<row r="7"><c r="B7" t="inlineStr"><is><t>受付</t></is></c>'
            '<c r="C7" t="n"><f>集計表マスター!B33</f><v>13</v></c></row>'
            '<row r="8"><c r="B8" t="inlineStr"><is><t>配膳</t></is></c>'
            '<c r="C8" t="n"><f>集計表マスター!C33</f><v>0</v></c></row>'
            '<row r="9"><c r="B9" t="inlineStr"><is><t>卓上</t></is></c>'
            '<c r="C9" t="n"><f>集計表マスター!D33</f><v>0</v></c></row>'
            '</sheetData></worksheet>'
        ),
        "xl/worksheets/sheet2.xml": (
            f'<worksheet xmlns="{spreadsheet}"><dimension ref="B1:D33"/><sheetData>'
            '<row r="1"><c r="B1" t="inlineStr"><is><t>受付</t></is></c>'
            '<c r="C1" t="inlineStr"><is><t>配膳</t></is></c>'
            '<c r="D1" t="inlineStr"><is><t>卓上</t></is></c></row>'
            '<row r="33"><c r="B33" t="n"><f>SUM(B2:B32)</f><v>13</v></c>'
            '<c r="C33" t="n"><f>SUM(C2:C32)</f><v>0</v></c>'
            '<c r="D33" t="n"><f>SUM(D2:D32)</f><v>0</v></c></row>'
            '</sheetData></worksheet>'
        ),
    }
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, value in members.items():
            archive.writestr(name, value)


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")


def fake_embeddings(_base_url: str, _model: str, texts: list[str], _timeout: float) -> list[list[float]]:
    vectors: list[list[float]] = []
    for text in texts:
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        vector = np.asarray([digest[index] + 1 for index in range(8)], dtype=np.float32)
        vectors.append(vector.tolist())
    return vectors


def evaluation_report(method: str, case_id: str, relevant_id: str, rank: int) -> dict[str, object]:
    reciprocal = 1.0 / rank
    overall = {
        "case_count": 1,
        "mrr": reciprocal,
        "hit_at_1": float(rank <= 1),
        "hit_at_3": float(rank <= 3),
        "hit_at_5": float(rank <= 5),
        "hit_at_10": float(rank <= 10),
        "recall_at_1": float(rank <= 1),
        "recall_at_3": float(rank <= 3),
        "recall_at_5": float(rank <= 5),
        "recall_at_10": float(rank <= 10),
    }
    return {
        "retrieval_method": method,
        "inputs": {
            "evaluation_set_sha256": "b" * 64,
            "intermediate_states": [{"path": "/test/intermediate", "sha256": "e" * 64}],
        },
        "overall": overall,
        "field_value_weight": 0.0 if method == "BM25" else None,
        "parent_context_penalty": 0.0 if method == "BM25" else None,
        "semantic_weight": 0.25 if "RRF" in method else None,
        "adaptive_semantic": "RRF" in method,
        "cases": [{
            "eval_case_id": case_id,
            "category": "table_row",
            "query": "alphaの値",
            "ground_truth_status": "confirmed",
            "first_relevant_rank": rank,
            "reciprocal_rank": reciprocal,
            "relevant_search_unit_ids": [relevant_id],
            "retrieved_search_unit_ids": [relevant_id],
            "retrieved_results": [{
                "file": "sample.csv", "page": None, "sheet": None, "slide": None,
                "section": "row=2", "chunk_id": relevant_id,
                "evidence_text": "name:alpha / value:10",
            }],
        }],
    }


class Layer1PipelineTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temporary = tempfile.TemporaryDirectory(prefix="aiec-layer1-test-")
        cls.work = Path(cls.temporary.name)
        cls.native_root = cls.work / "native"
        cls.native_root.mkdir()
        (cls.native_root / "sample.csv").write_text(
            "name,value\nalpha,10\nbeta,20\n", encoding="utf-8"
        )
        cls.intermediate = cls.work / "intermediate"
        subprocess.run(
            [
                sys.executable, str(SCRIPTS / "build_intermediate_records.py"),
                "--root", str(cls.native_root), "--out", str(cls.intermediate),
                "--run-at", RUN_AT,
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        cls.search = cls.work / "search"
        build_search_units.build([cls.intermediate], cls.search, 1200)
        cls.lexical = cls.work / "lexical"
        build_lexical_index.build(cls.search, cls.lexical)

        cls.chart_root = cls.work / "chart-source"
        cls.chart_root.mkdir()
        cls.chart_image = cls.chart_root / "chart.png"
        cls.chart_image.write_bytes(b"not-a-rendered-image-but-a-stable-source")
        image_sha = hashlib.sha256(cls.chart_image.read_bytes()).hexdigest()
        cls.chart_table = cls.work / "chart-table.json"
        write_json(cls.chart_table, {
            "schema_version": "0.1",
            "record_type": "chart_table",
            "chart_table_id": "ct_0123456789abcdef",
            "source": {
                "chart_path": str(cls.chart_image),
                "chart_sha256": image_sha,
                "source_type": "native_data",
            },
            "chart_type": "line",
            "title": "day別件数",
            "axes": [
                {"axis_id": "x", "orientation": "x", "label": "day", "exactness": "exact"},
                {"axis_id": "y", "orientation": "y", "label": "件数", "exactness": "exact"},
            ],
            "x_values": [1, 2],
            "series": [{
                "series_id": "count",
                "label": "件数",
                "axis_id": "y",
                "points": [
                    {"x": 1, "y": 10, "status": "exact"},
                    {"x": 2, "y": 20, "status": "exact"},
                ],
            }],
            "completeness": {
                "status": "verified",
                "detected_series_count": 1,
                "output_series_count": 1,
                "checks": ["source values reproduced"],
                "warnings": [],
            },
            "provenance": {
                "question_independent": True,
                "method": "static_source_recovery",
                "code_sha256": "c" * 64,
                "data_paths": ["data.csv"],
                "data_sha256": ["d" * 64],
            },
        })
        cls.chart_intermediate = cls.work / "chart-intermediate"
        build_chart_intermediate.build(
            cls.chart_root, [cls.chart_table], cls.chart_intermediate, RUN_AT
        )
        cls.combined_search = cls.work / "combined-search"
        build_search_units.build(
            [cls.intermediate, cls.chart_intermediate], cls.combined_search, 1200
        )
        cls.combined_lexical = cls.work / "combined-lexical"
        build_lexical_index.build(cls.combined_search, cls.combined_lexical)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temporary.cleanup()

    def test_native_pipeline_is_traceable(self) -> None:
        self.assertEqual(
            validate_intermediate_records_streaming.validate(self.intermediate, self.native_root),
            {"document": 1, "evidence": 3, "relation": 3},
        )
        search_result = validate_search_units_streaming.validate(
            self.search, [self.intermediate]
        )
        self.assertEqual(search_result["records"], 2)
        lexical_result = validate_lexical_index.validate(self.lexical, self.search)
        self.assertEqual(lexical_result["documents"], 2)
        retrieval = search_lexical_index.search(
            self.lexical, "name alpha value 10", 2, field_value_weight=0.0,
            parent_context_penalty=0.0,
        )
        sources, _ = retrieval_trace_common.load_document_sources([self.intermediate])
        retrieval_trace_common.enrich_retrieval(retrieval, sources)
        self.assertEqual(retrieval["results"][0]["unit_type"], "table_row")
        self.assertIn("alpha", retrieval["results"][0]["text"])
        self.assertEqual(retrieval["results"][0]["file"], "sample.csv")
        self.assertEqual(
            retrieval["results"][0]["chunk_id"],
            retrieval["results"][0]["search_unit_id"],
        )

    def test_single_source_legacy_search_state_remains_verifiable(self) -> None:
        with tempfile.TemporaryDirectory(prefix="aiec-legacy-search-") as temporary:
            copied = Path(temporary) / "search"
            shutil.copytree(self.search, copied)
            state_path = copied / "search-build-state.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["source"].pop("intermediate_states")
            write_json(state_path, state)
            result = validate_search_units_streaming.validate(
                copied, [self.intermediate]
            )
        self.assertEqual(result["records"], 2)

    def test_search_validators_reject_unknown_builder_version(self) -> None:
        with tempfile.TemporaryDirectory(prefix="aiec-search-version-") as temporary:
            copied = Path(temporary) / "search"
            shutil.copytree(self.search, copied)
            state_path = copied / "search-build-state.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["builder_version"] = "99.0.0"
            write_json(state_path, state)

            for validator in (
                validate_search_units.validate,
                validate_search_units_streaming.validate,
            ):
                with self.subTest(validator=validator.__module__):
                    with self.assertRaisesRegex(
                        ValueError, "provenance|builder version|build state"
                    ):
                        validator(copied, [self.intermediate])

    def test_chart_units_append_without_changing_native_prefix(self) -> None:
        validate_intermediate_records_streaming.validate(
            self.chart_intermediate, self.chart_root
        )
        combined = validate_search_units_streaming.validate(
            self.combined_search, [self.intermediate, self.chart_intermediate]
        )
        self.assertEqual(combined["counts_by_type"]["chart_summary"], 1)
        self.assertEqual(combined["counts_by_type"]["chart_series"], 1)
        base_bytes = (self.search / "search_units.jsonl").read_bytes()
        combined_bytes = (self.combined_search / "search_units.jsonl").read_bytes()
        self.assertTrue(combined_bytes.startswith(base_bytes))
        retrieval = search_lexical_index.search(self.combined_lexical, "最大値", 1)
        self.assertEqual(retrieval["results"][0]["unit_type"], "chart_series")
        self.assertIn("20", retrieval["results"][0]["text"])

    def test_semantic_index_reuses_exact_base_prefix(self) -> None:
        semantic = self.work / "semantic"
        combined_semantic = self.work / "combined-semantic"
        with (
            mock.patch.object(build_semantic_index, "model_info", return_value=MODEL),
            mock.patch.object(build_semantic_index, "embed_texts", side_effect=fake_embeddings),
        ):
            base_state = build_semantic_index.build(
                self.search, semantic, "http://unused", "embeddinggemma", 2, 10.0
            )
            combined_state = build_semantic_index.build(
                self.combined_search, combined_semantic, "http://unused", "embeddinggemma",
                2, 10.0, base_index=semantic,
            )
        self.assertEqual(
            combined_state["build_statistics"]["reused_base_records"],
            base_state["matrix"]["record_count"],
        )
        self.assertEqual(
            combined_state["matrix"]["record_count"],
            base_state["matrix"]["record_count"] + 2,
        )
        validate_semantic_index.validate(semantic, self.search)
        validate_semantic_index.validate(combined_semantic, self.combined_search, semantic)
        with (
            mock.patch.object(search_semantic_index, "model_info", return_value=MODEL),
            mock.patch.object(search_semantic_index, "embed_texts", side_effect=fake_embeddings),
        ):
            filtered = search_semantic_index.search(
                combined_semantic, "最大値", 1, base_url="http://unused",
                unit_types=["chart_series"],
            )
        self.assertEqual(filtered["results"][0]["unit_type"], "chart_series")

    def test_semantic_embedding_bisects_rejected_http400_batch(self) -> None:
        calls: list[int] = []

        def reject_multi(_base_url: str, _model: str, texts: list[str], _timeout: float):
            calls.append(len(texts))
            if len(texts) > 1:
                raise RuntimeError("local Ollama request failed: HTTP 400: batch rejected")
            return fake_embeddings(_base_url, _model, texts, _timeout)

        with mock.patch.object(build_semantic_index, "embed_texts", side_effect=reject_multi):
            vectors = build_semantic_index.embed_with_http400_split(
                "http://unused", "embeddinggemma", ["one", "two", "three"], 10.0
            )
        self.assertEqual(len(vectors), 3)
        self.assertEqual(calls[0], 3)
        self.assertTrue(all(size >= 1 for size in calls))

    def test_semantic_cosine_scores_are_finite_and_reject_invalid_query(self) -> None:
        matrix = np.asarray([[1.0, 0.0], [0.0, 1.0]], dtype=np.float32)
        scores = search_semantic_index.cosine_scores(
            matrix, np.asarray([1.0, 1.0], dtype=np.float32), batch_rows=1
        )
        self.assertTrue(np.isfinite(scores).all())
        self.assertTrue(np.allclose(scores, np.asarray([2 ** -0.5, 2 ** -0.5])))
        with self.assertRaisesRegex(ValueError, "non-finite"):
            search_semantic_index.cosine_scores(
                matrix, np.asarray([np.inf, 1.0], dtype=np.float32)
            )

    def test_layer1_deliverables_are_paired_and_hashed(self) -> None:
        first_unit = json.loads(
            (self.search / "search_units.jsonl").read_text(encoding="utf-8").splitlines()[1]
        )
        reports: list[Path] = []
        for index, (method, rank) in enumerate((
            ("BM25", 3),
            ("cosine-local-embedding", 2),
            ("BM25-field-parent+local-semantic-RRF", 1),
        )):
            path = self.work / f"report-{index}.json"
            write_json(path, evaluation_report(
                method, "qe_0123456789abcdef", first_unit["search_unit_id"], rank
            ))
            reports.append(path)
        output = self.work / "deliverables"
        build_layer1_deliverables.build(
            self.native_root, self.intermediate, self.search, output, reports
        )
        result = validate_layer1_deliverables.validate(output)
        self.assertEqual(result["inventory_files"], 1)
        raw = json.loads((output / "native_text_raw.jsonl").read_text(encoding="utf-8").splitlines()[0])
        normalized = json.loads(
            (output / "native_text_normalized.jsonl").read_text(encoding="utf-8").splitlines()[0]
        )
        self.assertTrue("raw_text" in raw or "raw_value" in raw)
        self.assertTrue("normalized_text" in normalized or "normalized_value" in normalized)
        summary = (output / "text_retrieval_summary.md").read_text(encoding="utf-8")
        self.assertIn("BM25-field-parent+local-semantic-RRF", summary)

    def test_text_encoding_detection_does_not_misread_cp932_as_utf16(self) -> None:
        cp932_path = self.work / "cp932.txt"
        cp932_path.write_bytes("日本語テキスト".encode("cp932"))
        value, encoding = probe_intermediate_records.read_text(cp932_path)
        self.assertEqual(value, "日本語テキスト")
        self.assertEqual(encoding, "cp932")

        utf16_path = self.work / "utf16.txt"
        utf16_path.write_bytes("日本語テキスト".encode("utf-16"))
        value, encoding = probe_intermediate_records.read_text(utf16_path)
        self.assertEqual(value, "日本語テキスト")
        self.assertEqual(encoding, "utf-16")

    def test_large_text_like_file_routes_to_bounded_searchable_stream(self) -> None:
        source = self.work / "large.json"
        source.write_text('{"message":"後段の質問に渡す読取結果"}\n', encoding="utf-8")
        probe = probe_intermediate_records.Probe(
            self.work, RUN_AT, None, diagnostic=False
        )
        with (
            mock.patch.object(probe_intermediate_records, "MAX_DIRECT_TEXT_BYTES", 8),
            mock.patch.object(
                probe_intermediate_records,
                "read_text",
                side_effect=AssertionError("large-file route must not materialize the source"),
            ),
        ):
            probe.extract(source)

        self.assertEqual(len(probe.documents), 1)
        document = probe.documents[0]
        self.assertEqual(document["extraction"]["parser"], "bounded-text-stream")
        self.assertEqual(document["extraction"]["status"], "partial")
        self.assertTrue(probe.evidence)
        self.assertIn("後段の質問に渡す", probe.evidence[0]["content"]["raw_text"])
        self.assertEqual(
            probe.evidence[0]["provenance"]["extraction_method"],
            "bounded_streaming_text",
        )
        self.assertEqual(
            probe.evidence[0]["native_properties"]["source_structure_status"],
            "unresolved",
        )

    def test_large_stream_is_exactly_sharded_for_the_question_path(self) -> None:
        source = self.work / "large.txt"
        source_text = "先頭\n" + ("読取文字" * 1400) + "\n末尾の質問根拠"
        source.write_text(source_text, encoding="utf-8")
        probe = probe_intermediate_records.Probe(
            self.work, RUN_AT, None, diagnostic=False
        )
        with mock.patch.object(
            probe_intermediate_records, "MAX_DIRECT_TEXT_BYTES", 8
        ):
            probe.extract(source)

        chunks = [item["content"]["raw_text"] for item in probe.evidence]
        self.assertGreater(len(chunks), 1)
        self.assertEqual("".join(chunks), source_text)
        self.assertTrue(all(
            len(value) <= probe_intermediate_records.MAX_QUESTION_EVIDENCE_CHARS
            for value in chunks
        ))
        self.assertIn("末尾の質問根拠", chunks[-1])
        offsets = [
            (
                item["native_properties"]["character_start"],
                item["native_properties"]["character_end"],
            )
            for item in probe.evidence
        ]
        self.assertEqual(offsets[0][0], 0)
        self.assertEqual(offsets[-1][1], len(source_text))
        self.assertTrue(all(left[1] == right[0] for left, right in zip(offsets, offsets[1:])))

    def test_large_cp932_sniff_keeps_a_character_split_at_sample_boundary(self) -> None:
        prefix = b"A" * (probe_intermediate_records.TEXT_ENCODING_SNIFF_BYTES - 1)
        encoded = prefix + "後".encode("cp932") + b"B" * 16
        sample = encoded[:probe_intermediate_records.TEXT_ENCODING_SNIFF_BYTES]
        self.assertEqual(
            probe_intermediate_records.detect_text_encoding(
                sample, partial_sample=True
            ),
            "cp932",
        )

        source = self.work / "large-boundary.csv"
        source.write_bytes(encoded)
        probe = probe_intermediate_records.Probe(
            self.work, RUN_AT, None, diagnostic=False
        )
        with mock.patch.object(
            probe_intermediate_records,
            "MAX_DIRECT_TEXT_BYTES",
            probe_intermediate_records.TEXT_ENCODING_SNIFF_BYTES - 1,
        ):
            probe.extract(source)
        reconstructed = "".join(
            item["content"]["raw_text"] for item in probe.evidence
        )
        self.assertIn("後", reconstructed)
        self.assertNotIn("\ufffd", reconstructed)

    def test_large_cp932_with_ascii_prefix_is_validated_beyond_the_sniff_window(self) -> None:
        encoded = (
            b"A" * probe_intermediate_records.TEXT_ENCODING_SNIFF_BYTES
            + "後".encode("cp932")
            + b"B"
        )
        source = self.work / "large-ascii-prefix.csv"
        source.write_bytes(encoded)
        self.assertEqual(
            probe_intermediate_records.detect_text_file_encoding(source),
            "cp932",
        )

        probe = probe_intermediate_records.Probe(
            self.work, RUN_AT, None, diagnostic=False
        )
        with mock.patch.object(
            probe_intermediate_records,
            "MAX_DIRECT_TEXT_BYTES",
            probe_intermediate_records.TEXT_ENCODING_SNIFF_BYTES - 1,
        ):
            probe.extract(source)
        reconstructed = "".join(
            item["content"]["raw_text"] for item in probe.evidence
        )
        self.assertIn("後", reconstructed)
        self.assertNotIn("\ufffd", reconstructed)

    def test_ooxml_fallback_rejects_unsafe_xml_and_zip_bomb_ratio(self) -> None:
        with tempfile.TemporaryDirectory(prefix="aiec-ooxml-safety-") as temporary:
            root = Path(temporary)
            unsafe = root / "unsafe.xlsx"
            write_minimal_xlsx(unsafe, unsafe_workbook_xml=True)
            with self.assertRaisesRegex(ValueError, "ooxml_xml_unsafe"):
                probe_intermediate_records.validate_ooxml_archive(
                    unsafe, required_members=OOXML_REQUIRED
                )

            compressed_bomb = root / "compressed-bomb.xlsx"
            with zipfile.ZipFile(
                compressed_bomb, "w", compression=zipfile.ZIP_DEFLATED
            ) as archive:
                for name in OOXML_REQUIRED:
                    value = "A" * 1_000_000 if name == "xl/workbook.xml" else "<root/>"
                    archive.writestr(name, value)
            with self.assertRaisesRegex(ValueError, "ooxml_archive_resource_limit"):
                probe_intermediate_records.validate_ooxml_archive(
                    compressed_bomb, required_members=OOXML_REQUIRED
                )

    @unittest.skipUnless(
        importlib.util.find_spec("openpyxl"),
        "openpyxl is required for the native-reader safety-route test",
    )
    def test_openpyxl_route_runs_ooxml_safety_gate_before_parser(self) -> None:
        with tempfile.TemporaryDirectory(prefix="aiec-openpyxl-safety-") as temporary:
            root = Path(temporary)
            unsafe = root / "unsafe.xlsx"
            write_minimal_xlsx(unsafe, unsafe_workbook_xml=True)
            probe = probe_intermediate_records.Probe(
                root, RUN_AT, None, diagnostic=False
            )
            with self.assertRaisesRegex(ValueError, "ooxml_xml_unsafe"):
                probe.extract_xlsx(unsafe)

    @unittest.skipUnless(
        importlib.util.find_spec("docx"),
        "python-docx is required for the native-reader safety-route test",
    )
    def test_python_docx_route_gates_archive_before_parser(self) -> None:
        import docx

        with tempfile.TemporaryDirectory(prefix="aiec-docx-safety-") as temporary:
            root = Path(temporary)
            unsafe = root / "unsafe.docx"
            write_unsafe_ooxml(
                unsafe,
                required_members=probe_intermediate_records.DOCX_REQUIRED_OOXML_MEMBERS,
                unsafe_member="word/document.xml",
            )
            probe = probe_intermediate_records.Probe(
                root, RUN_AT, None, diagnostic=False
            )
            with mock.patch.object(docx, "Document") as parser:
                with self.assertRaisesRegex(ValueError, "ooxml_xml_unsafe"):
                    probe.extract_docx(unsafe)
                parser.assert_not_called()

            decrypted = io.BytesIO()
            write_unsafe_ooxml(
                decrypted,
                required_members=probe_intermediate_records.DOCX_REQUIRED_OOXML_MEMBERS,
                unsafe_member="word/document.xml",
            )
            decrypted.seek(7)
            with (
                mock.patch.object(
                    probe, "office_source", return_value=(decrypted, True)
                ),
                mock.patch.object(docx, "Document") as parser,
            ):
                with self.assertRaisesRegex(ValueError, "ooxml_xml_unsafe"):
                    probe.extract_docx(unsafe)
                parser.assert_not_called()
            self.assertEqual(decrypted.tell(), 0)

    @unittest.skipUnless(
        importlib.util.find_spec("pptx"),
        "python-pptx is required for the native-reader safety-route test",
    )
    def test_python_pptx_route_gates_archive_before_parser(self) -> None:
        import pptx

        with tempfile.TemporaryDirectory(prefix="aiec-pptx-safety-") as temporary:
            root = Path(temporary)
            unsafe = root / "unsafe.pptx"
            write_unsafe_ooxml(
                unsafe,
                required_members=probe_intermediate_records.PPTX_REQUIRED_OOXML_MEMBERS,
                unsafe_member="ppt/presentation.xml",
            )
            probe = probe_intermediate_records.Probe(
                root, RUN_AT, None, diagnostic=False
            )
            with mock.patch.object(pptx, "Presentation") as parser:
                with self.assertRaisesRegex(ValueError, "ooxml_xml_unsafe"):
                    probe.extract_pptx(unsafe)
                parser.assert_not_called()

            decrypted = io.BytesIO()
            write_unsafe_ooxml(
                decrypted,
                required_members=probe_intermediate_records.PPTX_REQUIRED_OOXML_MEMBERS,
                unsafe_member="ppt/presentation.xml",
            )
            decrypted.seek(7)
            with (
                mock.patch.object(
                    probe, "office_source", return_value=(decrypted, True)
                ),
                mock.patch.object(pptx, "Presentation") as parser,
            ):
                with self.assertRaisesRegex(ValueError, "ooxml_xml_unsafe"):
                    probe.extract_pptx(unsafe)
                parser.assert_not_called()
            self.assertEqual(decrypted.tell(), 0)

    def test_ooxml_fallback_rejects_utf16_dtd_before_parsing(self) -> None:
        unsafe_xml = (
            '<?xml version="1.0" encoding="UTF-16"?>'
            '<!DOCTYPE workbook [<!ENTITY unsafe "entity">]>'
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"/>'
        )
        with tempfile.TemporaryDirectory(prefix="aiec-ooxml-utf16-") as temporary:
            root = Path(temporary)
            for name, payload in (
                ("bom-le.xlsx", unsafe_xml.encode("utf-16")),
                (
                    "signature-be.xlsx",
                    unsafe_xml.replace("UTF-16", "UTF-16BE").encode("utf-16-be"),
                ),
            ):
                workbook = root / name
                write_minimal_xlsx(workbook, workbook_xml_bytes=payload)
                with self.subTest(name=name), self.assertRaisesRegex(
                    ValueError, "ooxml_xml_unsafe"
                ):
                    probe_intermediate_records.validate_ooxml_archive(
                        workbook, required_members=OOXML_REQUIRED
                    )

            safe_xml = (
                '<?xml version="1.0" encoding="UTF-16"?>'
                '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"/>'
            ).encode("utf-16")
            safe = root / "safe-utf16.xlsx"
            write_minimal_xlsx(safe, workbook_xml_bytes=safe_xml)
            probe_intermediate_records.validate_ooxml_archive(
                safe, required_members=OOXML_REQUIRED
            )

            mismatched = root / "mismatched-declaration.xlsx"
            write_minimal_xlsx(
                mismatched,
                workbook_xml_bytes=(
                    '<?xml version="1.0" encoding="UTF-8"?>'
                    '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"/>'
                ).encode("utf-16"),
            )
            with self.assertRaisesRegex(ValueError, "ooxml_xml_encoding_invalid"):
                probe_intermediate_records.validate_ooxml_archive(
                    mismatched, required_members=OOXML_REQUIRED
                )

    def test_ooxml_fallback_preserves_numeric_raw_lexeme(self) -> None:
        exact = "0.123456789012345678901234567890"
        with tempfile.TemporaryDirectory(prefix="aiec-ooxml-precision-") as temporary:
            root = Path(temporary)
            workbook = root / "precise.xlsx"
            write_minimal_xlsx(workbook, numeric_lexeme=exact)
            probe = probe_intermediate_records.Probe(
                root, RUN_AT, None, diagnostic=False
            )
            probe.extract_xlsx_ooxml(workbook)
            probe.finalize_document()
            cells = [
                item for item in probe.evidence
                if item.get("evidence_type") == "table_cell"
            ]
            self.assertEqual(len(cells), 1)
            self.assertEqual(cells[0]["content"]["raw_value"], exact)
            self.assertEqual(cells[0]["content"]["normalized_value"], exact)
            self.assertEqual(cells[0]["native_properties"]["raw_lexeme"], exact)

    def assert_cached_formulas_are_questionable(
        self,
        probe: probe_intermediate_records.Probe,
    ) -> None:
        expected = {
            ("マスター", "C7"): 13,
            ("マスター", "C8"): 0,
            ("マスター", "C9"): 0,
            ("集計表マスター", "B33"): 13,
            ("集計表マスター", "C33"): 0,
            ("集計表マスター", "D33"): 0,
        }
        cells = {
            (item["location"]["sheet_name"], item["location"]["cell"]): item
            for item in probe.evidence
            if item.get("evidence_type") == "table_cell"
            and (item["location"]["sheet_name"], item["location"]["cell"])
            in expected
        }
        formulas = {
            (item["location"]["sheet_name"], item["location"]["cell"]): item
            for item in probe.evidence
            if item.get("evidence_type") == "formula"
        }
        self.assertEqual(set(cells), set(expected))
        self.assertEqual(set(formulas), set(expected))
        for locator, saved_value in expected.items():
            with self.subTest(locator=locator):
                self.assertEqual(cells[locator]["content"]["raw_value"], saved_value)
                self.assertTrue(formulas[locator]["content"]["raw_text"].startswith("="))
                self.assertEqual(
                    formulas[locator]["native_properties"]["cached_value"],
                    saved_value,
                )
                self.assertTrue(
                    formulas[locator]["native_properties"]["cached_value_available"]
                )
                self.assertEqual(
                    formulas[locator]["native_properties"]["cached_value_status"],
                    "stored_in_file_not_recalculated",
                )

        units: list[dict[str, object]] = []
        deriver = build_search_units.DocumentDeriver(
            probe.documents[0]["document_id"], RUN_AT, units.append, 1200
        )
        for item in probe.evidence:
            deriver.consume(item)
        deriver.finish()

        exact_formula_units = {
            (item["locator"]["sheet_name"], item["locator"]["cell"]): item
            for item in units
            if item["unit_type"] == "text_chunk" and "cell" in item["locator"]
        }
        self.assertEqual(set(exact_formula_units), set(expected))
        for locator, saved_value in expected.items():
            text = exact_formula_units[locator]["text"]["search_text"]
            self.assertIn(f"保存値（ファイル保存時・未再計算）: {saved_value}", text)
            self.assertIn("式: =", text)
            self.assertEqual(
                set(exact_formula_units[locator]["source_evidence_ids"]),
                {
                    cells[locator]["evidence_id"],
                    formulas[locator]["evidence_id"],
                },
            )

        rows = {
            (item["locator"]["sheet_name"], item["locator"]["row_index"]): item
            for item in units if item["unit_type"] == "table_row"
        }
        for locator in (("マスター", 7), ("マスター", 8), ("マスター", 9)):
            text = rows[locator]["text"]["search_text"]
            self.assertIn(str(expected[(locator[0], f"C{locator[1]}")]), text)
            self.assertIn("保存値・ファイル保存時・未再計算", text)
            self.assertIn(": =", text)
        aggregate_text = rows[("集計表マスター", 33)]["text"]["search_text"]
        for value in (13, 0, 0):
            self.assertIn(str(value), aggregate_text)
        self.assertEqual(aggregate_text.count("保存値・ファイル保存時・未再計算"), 3)
        self.assertEqual(aggregate_text.count(": =SUM("), 3)

    def test_ooxml_fallback_keeps_formula_and_saved_value_in_search_units(self) -> None:
        with tempfile.TemporaryDirectory(prefix="aiec-ooxml-formula-cache-") as temporary:
            root = Path(temporary)
            workbook = root / "cached-formulas.xlsx"
            write_cached_formula_xlsx(workbook)
            before = hashlib.sha256(workbook.read_bytes()).hexdigest()
            probe = probe_intermediate_records.Probe(
                root, RUN_AT, None, diagnostic=False
            )
            probe.extract_xlsx_ooxml(workbook)
            probe.finalize_document()
            self.assert_cached_formulas_are_questionable(probe)
            self.assertEqual(hashlib.sha256(workbook.read_bytes()).hexdigest(), before)

    @unittest.skipUnless(
        importlib.util.find_spec("openpyxl"),
        "openpyxl is required for the native cached-formula route test",
    )
    def test_openpyxl_keeps_formula_and_saved_value_in_search_units(self) -> None:
        with tempfile.TemporaryDirectory(prefix="aiec-openpyxl-formula-cache-") as temporary:
            root = Path(temporary)
            workbook = root / "cached-formulas.xlsx"
            write_cached_formula_xlsx(workbook)
            before = hashlib.sha256(workbook.read_bytes()).hexdigest()
            probe = probe_intermediate_records.Probe(
                root, RUN_AT, None, diagnostic=False
            )
            probe.extract_xlsx(workbook)
            probe.finalize_document()
            self.assert_cached_formulas_are_questionable(probe)
            self.assertEqual(hashlib.sha256(workbook.read_bytes()).hexdigest(), before)

    def test_search_unit_schema_allows_exact_formula_cell_locator(self) -> None:
        schema = json.loads(
            (REPOSITORY / "schemas" / "search-unit.schema.json").read_text(
                encoding="utf-8"
            )
        )
        cell = schema["properties"]["locator"]["properties"]["cell"]
        self.assertEqual(cell["pattern"], "^[A-Z]{1,3}[1-9][0-9]*$")

    def test_cached_formula_search_units_pass_trace_validators(self) -> None:
        with tempfile.TemporaryDirectory(prefix="aiec-formula-trace-") as temporary:
            base = Path(temporary)
            root = base / "source"
            root.mkdir()
            write_cached_formula_xlsx(root / "cached-formulas.xlsx")
            intermediate = base / "intermediate"
            subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS / "build_intermediate_records.py"),
                    "--root", str(root),
                    "--out", str(intermediate),
                    "--run-at", RUN_AT,
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            search = base / "search"
            build_search_units.build([intermediate], search, 1200)
            validate_intermediate_records_streaming.validate(intermediate, root)
            expected = validate_search_units.validate(search, [intermediate])
            streamed = validate_search_units_streaming.validate(
                search, [intermediate]
            )
            self.assertEqual(streamed, expected)
            self.assertEqual(expected["counts_by_type"]["text_chunk"], 6)
            units = [
                json.loads(line)
                for line in (search / "search_units.jsonl").read_text(
                    encoding="utf-8"
                ).splitlines()
                if line.strip()
            ]
            formula_units = [
                item for item in units
                if item["unit_type"] == "text_chunk"
                and "cell" in item["locator"]
            ]
            self.assertEqual(len(formula_units), 6)
            self.assertTrue(all("未再計算" in item["text"]["search_text"] for item in formula_units))

    def test_failed_document_rolls_back_partial_evidence_and_relations(self) -> None:
        with tempfile.TemporaryDirectory(prefix="aiec-file-transaction-") as temporary:
            root = Path(temporary) / "source"
            root.mkdir()
            source = root / "late-failure.txt"
            source.write_text("source remains unchanged", encoding="utf-8")
            output = Path(temporary) / "intermediate"
            output.mkdir()

            def emit_then_fail(extractor, path: Path) -> None:
                document = extractor.add_document(path, "late-failure-fixture")
                evidence = extractor.add_evidence(
                    document["document_id"],
                    "paragraph",
                    {"paragraph_index": 1},
                    probe_intermediate_records.content(raw_text="partial-evidence-must-not-survive"),
                )
                extractor.contain_document(document["document_id"], evidence["evidence_id"])
                raise RuntimeError("synthetic late extraction failure")

            source_sha = probe_intermediate_records.digest_file(source)
            with mock.patch.object(
                build_intermediate_records.Probe, "extract", new=emit_then_fail
            ):
                entry, error = build_intermediate_records.process_file(
                    output, root, source, RUN_AT, source_sha, ()
                )

            self.assertIsInstance(error, RuntimeError)
            self.assertEqual(entry["status"], "failed")
            self.assertEqual(entry["shards"]["documents"]["record_count"], 1)
            self.assertEqual(entry["shards"]["evidence"]["record_count"], 0)
            self.assertEqual(entry["shards"]["relations"]["record_count"], 0)
            document_path = output / entry["shards"]["documents"]["relative_path"]
            evidence_path = output / entry["shards"]["evidence"]["relative_path"]
            relation_path = output / entry["shards"]["relations"]["relative_path"]
            document = json.loads(document_path.read_text(encoding="utf-8"))
            self.assertEqual(document["extraction"]["status"], "failed")
            self.assertIn("synthetic late extraction failure", document["extraction"]["errors"][0])
            self.assertEqual(evidence_path.read_bytes(), b"")
            self.assertEqual(relation_path.read_bytes(), b"")
            self.assertNotIn(
                b"partial-evidence-must-not-survive",
                document_path.read_bytes() + evidence_path.read_bytes() + relation_path.read_bytes(),
            )

    def test_evaluation_records_confirmed_ground_truth_and_full_trace(self) -> None:
        unit = json.loads(
            (self.search / "search_units.jsonl").read_text(encoding="utf-8").splitlines()[0]
        )
        identity = {
            "query": "alphaの値",
            "relevant_search_unit_ids": [unit["search_unit_id"]],
            "method": "human_reviewed",
            "generator": "human-retrieval-eval-finalizer",
            "generator_version": "0.1.0",
        }
        evaluation_set = self.work / "evaluation-set.jsonl"
        write_json(evaluation_set, {
            "schema_version": "0.1",
            "record_type": "retrieval_eval_case",
            "eval_case_id": evaluate_lexical_retrieval.stable_id("qe", identity),
            "query": identity["query"],
            "relevant_search_unit_ids": identity["relevant_search_unit_ids"],
            "category": "table_row",
            "review": {
                "reviewed": True,
                "method": "structural",
                "source_locations": ["sample.csv#row=2"],
            },
            "provenance": {
                "method": "human_reviewed",
                "generator": identity["generator"],
                "generator_version": identity["generator_version"],
                "deterministic": True,
            },
        })
        report = evaluate_lexical_retrieval.evaluate(
            self.lexical,
            evaluation_set,
            [1, 3],
            field_value_weight=0.0,
            parent_context_penalty=0.0,
            intermediates=[self.intermediate],
        )
        case = report["cases"][0]
        self.assertEqual(case["ground_truth_status"], "confirmed")
        self.assertEqual(case["retrieved_results"][0]["file"], "sample.csv")
        self.assertIn("evidence_text", case["retrieved_results"][0])

    def test_repeated_pdf_edges_are_removed_only_from_normalized_text(self) -> None:
        raw = "共通ヘッダー\n\n本文は保持する\n\n共通フッター"
        normalized, operations = build_layer1_deliverables.normalize_page_with_edges(
            raw,
            {"first": {"共通ヘッダー"}, "last": {"共通フッター"}},
        )
        self.assertEqual(normalized, "本文は保持する")
        self.assertEqual(
            operations,
            [
                {"operation": "remove_repeated_pdf_header", "text": "共通ヘッダー"},
                {"operation": "remove_repeated_pdf_footer", "text": "共通フッター"},
            ],
        )
        self.assertIn("共通ヘッダー", raw)
        first_page, first_operations = build_layer1_deliverables.normalize_page_with_edges(
            raw,
            {
                "first": {"共通ヘッダー"}, "last": {"共通フッター"},
                "first_keep_page": {"共通ヘッダー": 1},
                "last_keep_page": {"共通フッター": 1},
            },
            page_number=1,
        )
        self.assertIn("共通ヘッダー", first_page)
        self.assertEqual(first_operations, [])

    def test_answer_pipeline_adapter_uses_layer1_trace(self) -> None:
        index = layer1_index.Layer1Index(
            "layer1-lexical",
            self.combined_lexical,
            [self.intermediate, self.chart_intermediate],
        )
        chunks = index.search("最大値", top_k=2)
        self.assertTrue(chunks)
        self.assertEqual(chunks[0].kind, "chart_series")
        self.assertIn("最大値", chunks[0].text)
        index.projects = ["京橋信用ソリューションズ株式会社"]
        targets = index.target_projects("京橋信用ソリューションズ株式会社の最大値")
        self.assertEqual(targets, {"京橋信用ソリューションズ株式会社"})
        self.assertNotIn(
            "京橋信用ソリューションズ株式会社",
            index.strip_project_names("京橋信用ソリューションズ株式会社の最大値", targets),
        )

    def test_answer_pipeline_uses_best_observed_fixed_hybrid(self) -> None:
        index = object.__new__(layer1_index.Layer1Index)
        index.mode = "layer1-hybrid"
        index.lexical_index = Path("/unused/lexical")
        index.semantic_index = Path("/unused/semantic")
        index.document_sources = {}
        index.projects = []
        with mock.patch.object(
            layer1_index,
            "search_hybrid",
            return_value={"results": []},
        ) as hybrid:
            self.assertEqual(index.search("test query", top_k=1), [])
        self.assertIs(hybrid.call_args.kwargs["adaptive_semantic"], False)


if __name__ == "__main__":
    unittest.main()
