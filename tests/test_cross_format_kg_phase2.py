from __future__ import annotations

import ast
import copy
import json
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[1]
SCRIPTS = REPOSITORY / "scripts"
ENGINE = REPOSITORY / "distribution" / "macos-local-memory" / "engine"
DATASET = REPOSITORY / "evaluation" / "cross-format-kg-v0.1"
BUILDER = SCRIPTS / "build_cross_document_semantic_graph.py"
ANSWERER = SCRIPTS / "query_cross_document_semantic_graph.py"
SHADOW_VALIDATOR = SCRIPTS / "validate_cross_document_semantic_graph.py"
RUNTIME_PYTHON = (
    REPOSITORY / "rag" / ".venv" / "bin" / "python"
    if (REPOSITORY / "rag" / ".venv" / "bin" / "python").is_file()
    else Path(sys.executable)
)
sys.path.insert(0, str(SCRIPTS))

import evaluate_cross_format_kg_phase2 as evaluator


def run_checked(argv: list[str]) -> None:
    completed = subprocess.run(
        argv,
        cwd=REPOSITORY,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode:
        raise AssertionError(
            f"command failed ({completed.returncode}): {' '.join(argv)}\n"
            f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
        )


def jsonl(path: Path) -> list[dict[str, object]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


class CrossFormatKgPhase2Test(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temporary = tempfile.TemporaryDirectory(prefix="aiec-cross-format-phase2-")
        cls.work = Path(cls.temporary.name)
        layer1 = cls.work / "layer1"
        intermediate = layer1 / "intermediate"
        search = layer1 / "search"
        cls.safe_phase1 = cls.work / "safe-phase1"
        corpus = DATASET / "corpus"

        # Build the real Layer 1 safe stream from corpus only.  Gold and QA are
        # deliberately absent from all four extraction/security commands.
        run_checked([
            str(RUNTIME_PYTHON),
            str(SCRIPTS / "build_intermediate_records.py"),
            "--root",
            str(corpus),
            "--out",
            str(intermediate),
            "--run-at",
            "2026-08-27T00:00:00+00:00",
        ])
        run_checked([
            str(RUNTIME_PYTHON),
            str(SCRIPTS / "build_search_units.py"),
            "--intermediate",
            str(intermediate),
            "--out",
            str(search),
        ])
        run_checked([
            str(RUNTIME_PYTHON),
            str(SCRIPTS / "adapt_layer1_to_local_memory.py"),
            "--intermediate",
            str(intermediate),
            "--search-output",
            str(search),
            "--source-root",
            str(corpus),
            "--out",
            str(cls.safe_phase1),
        ])
        run_checked([
            str(RUNTIME_PYTHON),
            str(ENGINE / "content_security_gate.py"),
            "--evidence",
            str(cls.safe_phase1 / "semantic-evidence.jsonl"),
            "--documents",
            str(cls.safe_phase1 / "semantic-documents.jsonl"),
            "--output-dir",
            str(cls.safe_phase1),
        ])

        cls.output = cls.work / "phase2-output"
        cls.report = evaluator.run_evaluation(
            dataset=DATASET,
            phase1_dir=cls.safe_phase1,
            output=cls.output,
            builder=BUILDER,
            answerer=ANSWERER,
            python=Path(sys.executable),
        )
        cls.results = jsonl(cls.output / "phase2-results.jsonl")
        cls.snapshot = evaluator.GraphSnapshot.load(
            cls.output / "semantic-graph.sqlite3"
        )
        cls.qa_cases = {
            item["qa_case_id"]: item
            for item in jsonl(DATASET / "gold" / "qa-cases.jsonl")
        }

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temporary.cleanup()

    def test_real_safe_phase1_e2e_runs_five_normal_and_twenty_nine_ablations(self) -> None:
        self.assertEqual("PASS", self.report["decision"])
        self.assertEqual(14, self.report["gold_edge_count"])
        self.assertEqual(14, self.report["matched_gold_edge_count"])
        self.assertEqual(5, self.report["normal_case_count"])
        self.assertEqual(4, self.report["accepted_case_count"])
        self.assertEqual(1, self.report["hold_case_count"])
        self.assertEqual(29, self.report["required_edge_ablation_count"])
        self.assertEqual(1, self.report["unused_edge_negative_control_count"])
        self.assertEqual(30, self.report["runtime_ablation_snapshot_count"])
        self.assertEqual([], self.report["ablation_trace_disabled_edge_ids"])
        self.assertTrue(self.report["source_graph_unchanged_after_ablations"])
        self.assertEqual(
            self.report["graph_file_sha256"],
            self.report["source_graph_file_sha256_after_ablations"],
        )
        self.assertEqual(
            "independent_hash_valid_sqlite_snapshot",
            self.report["ablation_strategy"],
        )
        self.assertEqual(35, self.report["result_record_count"])
        self.assertEqual(0, self.report["measured_outbound_network_attempt_count"])
        self.assertEqual(0, self.report["reported_outbound_network_attempt_count"])

        normal = [item for item in self.results if item["run_kind"] == "normal"]
        ablated = [
            item
            for item in self.results
            if item["run_kind"] == "required_edge_ablation"
        ]
        controls = [
            item
            for item in self.results
            if item["run_kind"] == "unused_edge_ablation_control"
        ]
        self.assertEqual(5, len(normal))
        self.assertEqual(29, len(ablated))
        self.assertEqual(1, len(controls))
        self.assertEqual(4, sum(item["decision"] == "ACCEPTED" for item in normal))
        self.assertEqual(1, sum(item["decision"] == "HOLD" for item in normal))
        self.assertTrue(all(item["decision"] == "HOLD" for item in ablated))
        self.assertTrue(
            all(
                not item["answer"]["asserted_facts"]
                and not item["answer"]["asserted_relations"]
                for item in ablated
            )
        )
        self.assertEqual("ACCEPTED", controls[0]["decision"])
        self.assertTrue(controls[0]["semantic_answer_projection_unchanged"])
        self.assertTrue(
            all(item["answer"]["trace"]["disabled_edge_ids"] == [] for item in self.results)
        )

    def test_real_five_document_graph_passes_shadow_validation(self) -> None:
        candidate = self.work / "04-semantic-graph-shadow.building"
        candidate.mkdir()
        database = candidate / "semantic-graph.sqlite3"
        builder_state = candidate / "semantic-graph-state.json"
        shutil.copy2(self.output / database.name, database)
        shutil.copy2(self.output / builder_state.name, builder_state)
        validation = candidate / "semantic-graph-validation.json"
        run_checked([
            str(Path(sys.executable)),
            str(SHADOW_VALIDATOR),
            "--database",
            str(database),
            "--state",
            str(builder_state),
            "--documents",
            str(self.safe_phase1 / "semantic-documents.jsonl"),
            "--source-evidence",
            str(self.safe_phase1 / "semantic-evidence.jsonl"),
            "--evidence",
            str(self.safe_phase1 / "safe-answer-evidence.jsonl"),
            "--security-state",
            str(self.safe_phase1 / "content-security-state.json"),
            "--security-gate-dir",
            str(self.safe_phase1),
            "--security-validator",
            str(ENGINE / "validate_content_security_gate.py"),
            "--generation-dir",
            str(self.work),
            "--output",
            str(validation),
        ])
        state = json.loads(validation.read_text(encoding="utf-8"))
        self.assertEqual("complete", state["status"])
        self.assertEqual(5, state["counts"]["documents"])
        self.assertEqual(144, state["counts"]["source_evidence"])
        self.assertEqual(13, state["counts"]["nodes"])
        self.assertEqual(16, state["counts"]["edges"])

    def test_answerer_inputs_contain_question_only(self) -> None:
        question_files = sorted((self.output / "answerer-io").glob("question-*.jsonl"))
        self.assertEqual(35, len(question_files))
        for path in question_files:
            records = jsonl(path)
            self.assertEqual(1, len(records))
            self.assertEqual({"question"}, set(records[0]))

    def test_ablation_uses_independent_hash_valid_sqlite_snapshots(self) -> None:
        ablation_records = [
            item for item in self.results if item["run_kind"] != "normal"
        ]
        self.assertEqual(30, len(ablation_records))
        self.assertEqual(
            evaluator.sha256_file(self.output / "semantic-graph.sqlite3"),
            self.report["graph_file_sha256"],
        )
        for item in ablation_records:
            graph_path = self.output / item["ablation_graph"]
            snapshot = evaluator.GraphSnapshot.load(graph_path)
            self.assertEqual(
                item["ablation_graph_snapshot_id"], snapshot.graph_snapshot_id
            )
            self.assertEqual(
                item["ablation_graph_file_sha256"], evaluator.sha256_file(graph_path)
            )
            self.assertNotIn(item["removed_runtime_edge_id"], snapshot.edges)
            self.assertEqual(len(self.snapshot.edges) - 1, len(snapshot.edges))
            self.assertEqual([], item["answer"]["trace"]["disabled_edge_ids"])
            self.assertEqual(
                snapshot.graph_snapshot_id,
                item["answer"]["trace"]["graph_snapshot_id"],
            )

    def test_unused_edge_negative_control_preserves_semantic_answer(self) -> None:
        control = next(
            item
            for item in self.results
            if item["run_kind"] == "unused_edge_ablation_control"
        )
        normal = next(
            item
            for item in self.results
            if item["run_kind"] == "normal"
            and item["qa_case_id"] == control["qa_case_id"]
        )
        self.assertEqual(
            evaluator.semantic_answer_projection(normal["answer"]),
            evaluator.semantic_answer_projection(control["answer"]),
        )

    def test_evaluator_never_uses_answerer_disable_flag(self) -> None:
        source = (SCRIPTS / "evaluate_cross_format_kg_phase2.py").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("--disable-edge-id", source)

    def test_answerer_rejects_qa_case_id_and_expected_fields(self) -> None:
        input_path = self.work / "leaking-question.jsonl"
        output_path = self.work / "leaking-answer.jsonl"
        input_path.write_text(
            json.dumps(
                {
                    "question": "Project Orionの主担当は誰ですか。",
                    "qa_case_id": "must-not-be-visible",
                    "expected": {"decision": "ACCEPTED"},
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        completed = subprocess.run(
            [
                str(sys.executable),
                str(ANSWERER),
                "--graph",
                str(self.output / "semantic-graph.sqlite3"),
                "--questions",
                str(input_path),
                "--out",
                str(output_path),
            ],
            cwd=REPOSITORY,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(2, completed.returncode)
        self.assertIn("accepts exactly one field", completed.stderr)
        self.assertFalse(output_path.exists())

    def test_independent_trace_audit_rejects_edge_hash_tamper(self) -> None:
        record = next(
            item
            for item in self.results
            if item["run_kind"] == "normal" and item["decision"] == "ACCEPTED"
        )
        answer = copy.deepcopy(record["answer"])
        answer["trace"]["visited_edge_hashes"][0] = "0" * 64
        qa_case = self.qa_cases[record["qa_case_id"]]
        with self.assertRaisesRegex(evaluator.EvaluationError, "Edge hashes mismatch"):
            evaluator.validate_answer_trace(
                self.snapshot, answer, qa_case["question"]
            )

    def test_independent_trace_audit_rejects_missing_endpoint(self) -> None:
        record = next(
            item
            for item in self.results
            if item["run_kind"] == "normal" and item["decision"] == "ACCEPTED"
        )
        answer = copy.deepcopy(record["answer"])
        used_edge_id = answer["trace"]["used_semantic_edge_ids"][0]
        endpoint = self.snapshot.edges[used_edge_id].from_node_id
        answer["trace"]["visited_node_ids"].remove(endpoint)
        answer["trace"]["visited_node_hashes"].remove(
            self.snapshot.nodes[endpoint].record_sha256
        )
        qa_case = self.qa_cases[record["qa_case_id"]]
        with self.assertRaisesRegex(evaluator.EvaluationError, "endpoints were not visited"):
            evaluator.validate_answer_trace(
                self.snapshot, answer, qa_case["question"]
            )

    def test_independent_trace_audit_rejects_untraversed_proof_edge(self) -> None:
        record = next(
            item
            for item in self.results
            if item["run_kind"] == "normal" and item["decision"] == "ACCEPTED"
        )
        answer = copy.deepcopy(record["answer"])
        unused_edge_id = next(
            edge_id
            for edge_id in self.snapshot.edges
            if edge_id not in answer["trace"]["used_semantic_edge_ids"]
        )
        answer["asserted_facts"][0]["proof_edge_ids"] = [unused_edge_id]
        qa_case = self.qa_cases[record["qa_case_id"]]
        with self.assertRaisesRegex(evaluator.EvaluationError, "untraversed proof Edge"):
            evaluator.validate_answer_trace(
                self.snapshot, answer, qa_case["question"]
            )

    def test_independent_trace_audit_rejects_semantically_wrong_proof_edge(self) -> None:
        record = next(
            item
            for item in self.results
            if item["run_kind"] == "normal"
            and item["qa_case_id"] == "xkg_qa_owner_at_2022_08_01"
        )
        answer = copy.deepcopy(record["answer"])
        alias_edge_id = next(
            edge_id
            for edge_id in answer["trace"]["used_semantic_edge_ids"]
            if self.snapshot.edges[edge_id].relation_type == "HAS_ALIAS"
        )
        reference_fact = next(
            item for item in answer["asserted_facts"] if item["field"] == "reference_time"
        )
        reference_fact["proof_edge_ids"] = [alias_edge_id]
        qa_case = self.qa_cases[record["qa_case_id"]]
        with self.assertRaisesRegex(
            evaluator.EvaluationError, "not supported by its proof Edges"
        ):
            evaluator.validate_answer_trace(
                self.snapshot, answer, qa_case["question"]
            )

    def test_gold_matching_rejects_unresolved_exact_phrase(self) -> None:
        gold = jsonl(DATASET / "gold" / "expected-graph.jsonl")
        gold[0]["source_references"][0]["selector"]["value"] = (
            "phrase that is absent from all supporting Evidence"
        )
        with self.assertRaisesRegex(
            evaluator.EvaluationError, "not resolved by supporting Evidence"
        ):
            evaluator.match_gold_edges(self.snapshot, gold)

    def test_network_guard_blocks_and_counts_socket_attempt(self) -> None:
        root = self.work / "network-guard-negative"
        root.mkdir()
        script = root / "attempt.py"
        script.write_text(
            textwrap.dedent(
                """
                import socket
                socket.create_connection(("example.com", 443))
                """
            ),
            encoding="utf-8",
        )
        guard = evaluator.NetworkGuard(root)
        with self.assertRaisesRegex(evaluator.EvaluationError, "network attempt blocked"):
            guard.run([str(sys.executable), str(script)], cwd=root)

    def test_builder_and_answerer_have_no_network_or_process_launch_imports(self) -> None:
        forbidden_modules = {
            "socket",
            "requests",
            "http.client",
            "urllib.request",
            "subprocess",
            "asyncio.subprocess",
        }
        forbidden_calls = {
            "os.system",
            "os.popen",
            "subprocess.run",
            "subprocess.call",
            "subprocess.Popen",
            "urllib.request.urlopen",
            "socket.create_connection",
        }
        for path in (BUILDER, ANSWERER):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            imported: set[str] = set()
            calls: set[str] = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imported.update(alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imported.add(node.module)
                elif isinstance(node, ast.Call):
                    current = node.func
                    parts: list[str] = []
                    while isinstance(current, ast.Attribute):
                        parts.append(current.attr)
                        current = current.value
                    if isinstance(current, ast.Name):
                        parts.append(current.id)
                        calls.add(".".join(reversed(parts)))
            self.assertFalse(
                imported & forbidden_modules,
                f"{path.name} imports network/process module(s): "
                f"{sorted(imported & forbidden_modules)}",
            )
            self.assertFalse(
                calls & forbidden_calls,
                f"{path.name} launches process/network call(s): "
                f"{sorted(calls & forbidden_calls)}",
            )

    def test_output_directory_is_immutable_by_default(self) -> None:
        with self.assertRaisesRegex(evaluator.EvaluationError, "refusing to overwrite"):
            evaluator.run_evaluation(
                dataset=DATASET,
                phase1_dir=self.safe_phase1,
                output=self.output,
                builder=BUILDER,
                answerer=ANSWERER,
                python=Path(sys.executable),
            )

    def test_source_places_freeze_before_gold_load(self) -> None:
        source = (SCRIPTS / "evaluate_cross_format_kg_phase2.py").read_text(
            encoding="utf-8"
        )
        boundary = source.index("build = build_and_freeze(")
        gold_read = source.index(
            'gold_edges = _read_jsonl(dataset / "gold" / "expected-graph.jsonl")'
        )
        self.assertLess(boundary, gold_read)


if __name__ == "__main__":
    unittest.main()
