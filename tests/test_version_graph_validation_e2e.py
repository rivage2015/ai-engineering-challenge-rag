"""F05a immediate app-gate attacks with synthetic sources and stub inference.

The test sentinel stops immediately after the real resolver validation CLI,
whether accepted or rejected. A later Reader error cannot masquerade as rejection.
This is not post-gate tamper, real-model, immutable-generation or release coverage.
"""
from __future__ import annotations

import contextlib
import copy
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import types
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "distribution/macos-local-memory"
ENGINE = PACKAGE / "engine"
spec = importlib.util.spec_from_file_location(
    "f05a_app_fixture", PACKAGE / "tests/test_versioned_safe_index_e2e.py",
)
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)
builder = types.SimpleNamespace()


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


class GateOutcome(RuntimeError):
    def __init__(self, result):
        self.result = result
        super().__init__("synthetic resolver gate stopped: " + result["status"])


class VersionGraphApplicationGateTests(unittest.TestCase):
    def setUp(self):
        self.h = fixture.VersionedSafeIndexE2E()
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)
        self.h.seed()
        self.h.bootstrap.build_index()
        config = self.h.bootstrap.load_json(self.h.bootstrap.CONFIG)
        self.prior_index = Path(config["index_path"])
        self.prior_graph = Path(config["path_graph_path"]) / "document-version-graph.json"
        self.resolver = self.h.module(ENGINE / "document_version_resolver.py")
        self.injected_bytes = 0

    def originals(self):
        return {str(p.relative_to(self.h.source)): p.read_bytes()
                for p in self.h.source.rglob("*") if p.is_file()}

    def seal(self, path, value):
        value["graph_sha256"] = digest({k: v for k, v in value.items() if k != "graph_sha256"})
        text = json.dumps(value, ensure_ascii=False)
        self.assertLessEqual(len(text.encode()), 65536)
        self.injected_bytes += len(text.encode())
        self.assertLessEqual(self.injected_bytes, 1048576)
        path.write_text(text, encoding="utf-8")

    def assert_gate_rejects(self, mutate):
        config_before = self.h.bootstrap.CONFIG.read_bytes()
        index_before = self.prior_index.read_bytes()
        review_before = self.h.bootstrap.DOCUMENT_VERSION_REVIEW.read_bytes()
        sources_before = self.originals()
        command_start, model_start = len(self.h.commands), len(self.h.model_requests)
        gate_inputs = []

        def intercept(command, log=None):
            if Path(command[1]).name != "document_version_resolver.py" or command[2] != "validate":
                return self.h.run_cli(command, log)
            self.h.commands.append(list(command))
            graph_path = Path(command[command.index("--graph") + 1])
            inventory = Path(command[command.index("--inventory") + 1])
            inventory_before = inventory.read_bytes()
            value = json.loads(graph_path.read_bytes())
            mutate(graph_path, value)
            graph_bytes = graph_path.read_bytes()
            decision_path = Path(command[command.index("--decisions") + 1])
            decision_bytes = decision_path.read_bytes() if decision_path.exists() else None
            output = io.StringIO()
            with mock.patch.object(sys, "argv", command[1:]), contextlib.redirect_stdout(output):
                code = self.resolver.main()
            result = json.loads(output.getvalue())
            self.assertIn(result["status"], {"PASS", "FAIL"})
            self.assertEqual(0 if result["status"] == "PASS" else 1, code)
            self.assertEqual(inventory_before, inventory.read_bytes())
            self.assertEqual(graph_bytes, graph_path.read_bytes())
            self.assertEqual(decision_bytes, decision_path.read_bytes() if decision_path.exists() else None)
            gate_inputs.append(result)
            raise GateOutcome(result)

        with mock.patch.object(self.h.bootstrap, "run", side_effect=intercept):
            with self.assertRaises(GateOutcome) as stop:
                self.h.bootstrap.build_index()
        self.assertEqual(1, len(gate_inputs))
        self.assertEqual("FAIL", stop.exception.result["status"], stop.exception.result)
        self.assertTrue(stop.exception.result["errors"])
        self.assertEqual(config_before, self.h.bootstrap.CONFIG.read_bytes())
        self.assertEqual(index_before, self.prior_index.read_bytes())
        self.assertEqual(review_before, self.h.bootstrap.DOCUMENT_VERSION_REVIEW.read_bytes())
        self.assertEqual(sources_before, self.originals())
        self.assertEqual(model_start, len(self.h.model_requests))
        forbidden = {"build_adaptive_semantic_graph.py", "validate_adaptive_semantic_graph.py", "build_local_semantic_index.py"}
        self.assertFalse(any(Path(c[1]).name in forbidden for c in self.h.commands[command_start:]))

    def test_empty_resealed_graph_fails_before_reader_and_keeps_prior_generation(self):
        def mutate(path, value):
            value.update(groups=[], nodes=[], edges=[], counts={"groups": 0, "resolved": 0, "needs_human_review": 0})
            self.seal(path, value)
        self.assert_gate_rejects(mutate)

    def test_fabricated_active_choice_fails_before_reader_and_keeps_prior_generation(self):
        # Same-key unmarked peer makes the actual new group held under F03a.
        (self.h.source / "業務内容_ver1.csv").rename(self.h.source / "業務内容.csv")
        def mutate(path, value):
            group = value["groups"][0]
            self.assertEqual("unmarked_candidate_requires_human_review", group["reason_code"])
            group.update(status="resolved", selected_relative_path="業務内容.csv", resolution_basis="automatic",
                         reason_code="fabricated_policy", conflicts=[])
            for item in group["candidates"]:
                item["disposition"] = "active" if item["relative_path"] == "業務内容.csv" else "historical"
            value["nodes"], value["edges"] = self.resolver.graph_projection(value["groups"])
            value["counts"] = {"groups": 1, "resolved": 1, "needs_human_review": 0}
            self.seal(path, value)
        self.assert_gate_rejects(mutate)

    def test_absent_inventory_candidate_fails_before_reader_and_keeps_prior_generation(self):
        def mutate(path, value):
            group = value["groups"][0]
            item = next(c for c in group["candidates"] if c["disposition"] == "historical")
            item.update(relative_path="NotInInventory_ver1.csv", source_sha256="0" * 64)
            group["candidate_set_sha256"] = self.resolver.candidate_set_hash(group["candidates"])
            value["nodes"], value["edges"] = self.resolver.graph_projection(value["groups"])
            self.seal(path, value)
        self.assert_gate_rejects(mutate)

    def test_changed_explicit_decision_snapshot_fails_before_reader(self):
        prior = json.loads(self.prior_graph.read_bytes())
        self.resolver.record_decision(
            self.prior_graph, self.h.bootstrap.DOCUMENT_VERSION_DECISIONS,
            prior["groups"][0]["group_id"], "業務内容_ver2.csv", "synthetic-human",
        )
        def mutate(graph_path, value):
            self.assertEqual("human", value["groups"][0]["resolution_basis"])
            path = graph_path.parent / "document-version-decisions.snapshot.json"
            text = json.dumps({"schema_version": "1.0", "decisions": []})
            self.assertLessEqual(len(text.encode()), 8192)
            path.write_text(text, encoding="utf-8")
        self.assert_gate_rejects(mutate)


if __name__ == "__main__":
    unittest.main(verbosity=2)
