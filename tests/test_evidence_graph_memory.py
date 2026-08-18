from __future__ import annotations

import copy
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "rag"))

from evidence_edge_audit import (
    EdgePolicy,
    EqualityCheck,
    audit_edge,
    audit_edge_with_same_model,
    blind_audit_packet,
)
from evidence_graph_memory import (
    EvidenceGraphError,
    add_node,
    add_unresolved,
    load_graph,
    new_graph,
    propose_edge,
    refresh_integrity,
    save_graph,
    set_answer_projection,
    validate_graph,
)


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def source(name: str, quote: str) -> dict:
    return {
        "path": f"source/{name}.pdf",
        "sha256": digest(name),
        "locator": {"page": 1, "bbox": [10, 20, 30, 40]},
        "quote": quote,
        "extraction_method": "native_text",
    }


def supported(packet: dict) -> dict:
    return {
        "verdict": "supported",
        "allowed_edge_types": [packet["proposed_edge_type"]],
        "rejected_edge_types": [],
        "evidence_node_ids": [packet["from_node"]["node_id"], packet["to_node"]["node_id"]],
        "missing_checks": [],
        "reason": "Both evidence nodes independently expose the same scoped action ID.",
    }


def not_falsified(packet: dict) -> dict:
    return {
        "falsified": False,
        "counterexamples": [],
        "unresolved_risks": [],
        "reason": "No conflicting identity, scope, time, row, column, unit, or decoy was found.",
    }


class EvidenceGraphMemoryTests(unittest.TestCase):
    def graph(self):
        graph = new_graph(
            question_id="Q070",
            question_sha256=digest("question"),
            graph_plan_id="pdf_action_transition_plan",
        )
        left = add_node(
            graph,
            node_type="action_status",
            value={"action_id": "AI-05", "status": "Open"},
            normalized_value={"action_id": "AI-05", "status": "open"},
            source=source("report", "AI-05 Open"),
        )
        right = add_node(
            graph,
            node_type="action_status",
            value={"action_id": "AI-05", "status": "未完了"},
            normalized_value={"action_id": "AI-05", "status": "open"},
            source=source("minutes", "AI-05 未完了"),
        )
        edge = propose_edge(
            graph,
            edge_type="same_action_id",
            from_node_id=left,
            to_node_id=right,
            claim="The records refer to the same scoped action.",
            comparison_fields=["normalized_value.action_id"],
        )
        return graph, left, right, edge

    def policy(self):
        return EdgePolicy(
            edge_type="same_action_id",
            from_node_types=("action_status",),
            to_node_types=("action_status",),
            equality_checks=(
                EqualityCheck(
                    "normalized_value.action_id",
                    "normalized_value.action_id",
                    "nfc_compact",
                ),
            ),
        )

    def test_verified_graph_round_trips_as_persistent_working_memory(self):
        graph, left, right, edge = self.graph()
        self.assertEqual(
            audit_edge(
                graph,
                edge,
                self.policy(),
                blind_auditor=supported,
                falsifier=not_falsified,
            ),
            "verified",
        )
        set_answer_projection(
            graph,
            operation="report_open_minus_completed",
            input_node_ids=[left, right],
            input_edge_ids=[edge],
        )
        self.assertEqual(graph["state"], "ready")
        self.assertEqual(validate_graph(graph), [])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "Q070.evidence-graph.json"
            save_graph(graph, path)
            loaded = load_graph(path)
            self.assertEqual(loaded, graph)
            with self.assertRaisesRegex(EvidenceGraphError, "overwrite"):
                save_graph(graph, path)

    def test_tampered_node_is_rejected_even_if_outer_integrity_is_refreshed(self):
        graph, _, _, _ = self.graph()
        graph["nodes"][0]["source"]["quote"] = "tampered"
        refresh_integrity(graph)
        errors = validate_graph(graph)
        self.assertTrue(any("node ID mismatch" in error for error in errors))
        self.assertTrue(any("node content hash mismatch" in error for error in errors))

    def test_open_unresolved_item_blocks_answer_projection(self):
        graph, left, right, edge = self.graph()
        audit_edge(graph, edge, self.policy(), blind_auditor=supported, falsifier=not_falsified)
        add_unresolved(
            graph,
            kind="scope_completeness",
            description="A later meeting may contain another status change.",
            required_checks=["scan_complete_meeting_set"],
        )
        set_answer_projection(
            graph,
            operation="status_difference",
            input_node_ids=[left, right],
            input_edge_ids=[edge],
        )
        self.assertEqual(graph["state"], "blocked")
        self.assertEqual(graph["answer_projection"]["status"], "blocked")

    def test_blind_packet_omits_builder_reason_question_and_answer(self):
        graph, _, _, edge = self.graph()
        decoy = add_node(
            graph,
            node_type="action_status",
            value={"action_id": "AI-06", "status": "Open"},
            normalized_value={"action_id": "AI-06", "status": "open"},
            source=source("decoy", "AI-06 Open"),
        )
        packet = blind_audit_packet(graph, edge, self.policy(), decoy_node_ids=[decoy])
        encoded = json.dumps(packet, ensure_ascii=False)
        self.assertNotIn("basis", packet)
        self.assertNotIn("claim", encoded)
        self.assertNotIn("question", encoded)
        self.assertNotIn("answer_projection", encoded)
        self.assertEqual(packet["decoy_nodes"][0]["node_id"], decoy)

    def test_blind_ambiguity_is_not_promoted(self):
        graph, _, _, edge = self.graph()

        def ambiguous(packet):
            value = supported(packet)
            value["verdict"] = "ambiguous"
            value["missing_checks"] = ["organization_scope"]
            return value

        status = audit_edge(
            graph,
            edge,
            self.policy(),
            blind_auditor=ambiguous,
            falsifier=not_falsified,
        )
        self.assertEqual(status, "ambiguous")
        self.assertEqual(graph["edges"][0]["status"], "ambiguous")

    def test_falsifier_can_veto_a_supportive_blind_audit(self):
        graph, _, _, edge = self.graph()

        def falsified(packet):
            return {
                "falsified": True,
                "counterexamples": [{"type": "different_project_scope", "node_id": packet["to_node"]["node_id"]}],
                "unresolved_risks": [],
                "reason": "The matching ID belongs to a different project scope.",
            }

        status = audit_edge(
            graph,
            edge,
            self.policy(),
            blind_auditor=supported,
            falsifier=falsified,
        )
        self.assertEqual(status, "contradicted")

    def test_machine_mismatch_cannot_be_overridden_by_two_supportive_calls(self):
        graph = new_graph(
            question_id="Q070",
            question_sha256=digest("question"),
            graph_plan_id="pdf_action_transition_plan",
        )
        left = add_node(
            graph, node_type="action_status",
            value={"action_id": "AI-05"}, normalized_value={"action_id": "AI-05"},
            source=source("left", "AI-05"),
        )
        right = add_node(
            graph, node_type="action_status",
            value={"action_id": "AI-06"}, normalized_value={"action_id": "AI-06"},
            source=source("right", "AI-06"),
        )
        edge = propose_edge(
            graph, edge_type="same_action_id", from_node_id=left, to_node_id=right,
            claim="These might be the same action.",
            comparison_fields=["normalized_value.action_id"],
        )
        status = audit_edge(
            graph,
            edge,
            self.policy(),
            blind_auditor=supported,
            falsifier=not_falsified,
        )
        self.assertEqual(status, "contradicted")

    def test_auditor_cannot_cite_evidence_outside_blind_packet(self):
        graph, _, _, edge = self.graph()

        def hallucinated(packet):
            value = supported(packet)
            value["evidence_node_ids"].append("egn_" + "0" * 32)
            return value

        with self.assertRaisesRegex(EvidenceGraphError, "outside its packet"):
            audit_edge(
                graph,
                edge,
                self.policy(),
                blind_auditor=hallucinated,
                falsifier=not_falsified,
            )

    def test_same_edge_cannot_be_audited_twice(self):
        graph, _, _, edge = self.graph()
        audit_edge(graph, edge, self.policy(), blind_auditor=supported, falsifier=not_falsified)
        with self.assertRaisesRegex(EvidenceGraphError, "unaudited proposed"):
            audit_edge(graph, edge, self.policy(), blind_auditor=supported, falsifier=not_falsified)

    def test_one_model_is_invoked_in_two_isolated_roles(self):
        graph, _, _, edge = self.graph()
        packets = []

        def same_model(packet):
            packets.append(copy.deepcopy(packet))
            if packet["audit_role"] == "blind_relation_classifier":
                return supported(packet)
            if packet["audit_role"] == "relation_falsifier":
                return not_falsified(packet)
            self.fail("unexpected audit role")

        status = audit_edge_with_same_model(
            graph,
            edge,
            self.policy(),
            model_call=same_model,
        )
        self.assertEqual(status, "verified")
        self.assertEqual(
            [packet["audit_role"] for packet in packets],
            ["blind_relation_classifier", "relation_falsifier"],
        )
        self.assertNotEqual(packets[0]["packet_sha256"], packets[1]["packet_sha256"])
        for packet in packets:
            encoded = json.dumps(packet, ensure_ascii=False)
            self.assertNotIn("basis", encoded)
            self.assertNotIn("final_status", encoded)
            self.assertNotIn("response_sha256", encoded)


if __name__ == "__main__":
    unittest.main()
