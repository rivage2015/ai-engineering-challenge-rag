from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np


REPOSITORY = Path(__file__).resolve().parents[1]
SCRIPTS = REPOSITORY / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(REPOSITORY / "rag"))

import build_chart_intermediate
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
import validate_search_units_streaming
import validate_semantic_index
import layer1_index


RUN_AT = "2026-08-15T00:00:00+00:00"
MODEL = {
    "requested": "embeddinggemma",
    "resolved": "embeddinggemma:latest",
    "digest": "a" * 64,
}


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
