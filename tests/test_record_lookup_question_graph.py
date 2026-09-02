from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / "distribution" / "macos-local-memory" / "engine"
APP = ROOT / "distribution" / "macos-local-memory" / "app"


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


qeg = load_module(
    "record_lookup_question_graph_qeg",
    ENGINE / "question_evidence_graph.py",
)
claim_validator = load_module(
    "record_lookup_claim_graph_validator",
    APP / "claim_graph_validator.py",
)


QUESTION = (
    "For the finalized Project Atlas record, give the Owner, "
    "Review Date, Unit Cost, Seats, and Budget."
)
PLAN = {
    "items": [
        {
            "item_id": "F1",
            "label": "Owner",
            "required_claim": "Owner for Project Atlas",
            "retrieval_query": "Project Atlas Owner",
            "required": True,
        },
        {
            "item_id": "F2",
            "label": "Review Date",
            "required_claim": "Review Date for Project Atlas",
            "retrieval_query": "Project Atlas Review Date",
            "required": True,
        },
        {
            "item_id": "F3",
            "label": "Unit Cost",
            "required_claim": "Unit Cost for Project Atlas",
            "retrieval_query": "Project Atlas Unit Cost",
            "required": True,
        },
        {
            "item_id": "F4",
            "label": "Seats",
            "required_claim": "Seat count for Project Atlas",
            "retrieval_query": "Project Atlas Seats",
            "required": True,
        },
        {
            "item_id": "F5",
            "label": "Budget Calculation",
            "required_claim": "Budget for Project Atlas",
            "retrieval_query": "Project Atlas Budget",
            "required": True,
        },
    ],
    "answer_shape": "Owner / Review Date / Unit Cost / Seats / Budget",
}


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def record(evidence_id: str, locator: dict, text: str) -> dict:
    return {
        "evidence_id": evidence_id,
        "document_id": "doc_projects",
        "relative_path": "project-plan.xlsx",
        "locator": locator,
        "text": text,
    }


FIELD_LAYOUT = (
    ("A", "Project"),
    ("B", "Owner"),
    ("C", "Review Date"),
    ("D", "Unit Cost"),
    ("E", "Seat Count"),
    ("F", "Budget"),
    ("G", "Status"),
    ("H", "Description"),
)


def evidence_fixture(*, second_approved: bool = False) -> list[dict]:
    records = [
        record(f"ev_h_{column.lower()}", {"sheet_name": "Project Plan", "cell": f"{column}1"}, label)
        for column, label in FIELD_LAYOUT
    ]
    row_values = {
        "A": "Project Atlas",
        "B": "Maya Chen",
        "C": "2026-09-15",
        "D": "1250",
        "E": "8",
        "F": "10000",
        "G": "Approved",
        "H": "Regional rollout",
    }
    draft_values = {
        **row_values,
        "B": "Legacy Owner",
        "C": "2026-08-01",
        "F": "9000",
        "G": "Draft",
        "H": "Legacy rollout",
    }
    for row_index, values, suffix in (
        (2, row_values, "approved"),
        (3, draft_values, "draft"),
    ):
        records.extend(
            record(
                f"ev_v_{column.lower()}_{suffix}",
                {"sheet_name": "Project Plan", "cell": f"{column}{row_index}"},
                value,
            )
            for column, value in values.items()
        )
        row_text = "\n".join(
            f"{label}: {values[column]}" for column, label in FIELD_LAYOUT
        )
        records.append(record(
            f"ev_row_{suffix}",
            {"sheet_name": "Project Plan", "row_index": row_index},
            row_text,
        ))

    if second_approved:
        values = {**row_values, "B": "Other Owner"}
        records.extend(
            record(
                f"ev_v_{column.lower()}_approved_2",
                {"sheet_name": "Project Plan", "cell": f"{column}4"},
                value,
            )
            for column, value in values.items()
        )
        records.append(record(
            "ev_row_approved_2",
            {"sheet_name": "Project Plan", "row_index": 4},
            "\n".join(f"{label}: {values[column]}" for column, label in FIELD_LAYOUT),
        ))

    records.append(record(
        "ev_unrelated_note",
        {"sheet_name": "Notes", "cell": "A9"},
        "Unrelated planning note",
    ))
    return records


def source_graph(records: list[dict]) -> dict:
    nodes = [{
        "node_id": "doc_projects",
        "node_type": "document",
        "status": "observed",
        "record_sha256": digest("node:doc_projects"),
    }]
    nodes.extend({
        "node_id": item["evidence_id"],
        "node_type": "evidence",
        "status": "observed",
        "record_sha256": digest(f"node:{item['evidence_id']}"),
    } for item in records)

    def edge(
        relation_id: str,
        source: str,
        relation_type: str,
        target: str,
        relation_class: str,
    ) -> dict:
        return {
            "relation_id": relation_id,
            "from_node_id": source,
            "relation_type": relation_type,
            "to_node_id": target,
            "relation_class": relation_class,
            "basis_kind": "explicit",
            "basis_rule": "native structured provenance",
            "status": "verified",
            "record_sha256": digest(f"edge:{relation_id}"),
        }

    raw_ids = [
        item["evidence_id"]
        for item in records
        if item["evidence_id"].startswith(("ev_h_", "ev_v_", "ev_unrelated_"))
    ]
    edges = [
        edge(
            f"rel_contains_{evidence_id}",
            "doc_projects",
            "contains",
            evidence_id,
            "structural",
        )
        for evidence_id in raw_ids
    ]
    for item in records:
        if not item["evidence_id"].startswith("ev_row_"):
            continue
        row_index = item["locator"]["row_index"]
        suffix = item["evidence_id"].removeprefix("ev_row_")
        for column, _ in FIELD_LAYOUT:
            for target_id in (
                f"ev_h_{column.lower()}",
                f"ev_v_{column.lower()}_{suffix}",
            ):
                edges.append(edge(
                    f"rel_{item['evidence_id']}_from_{target_id}",
                    item["evidence_id"],
                    "derived_from",
                    target_id,
                    "lineage",
                ))

    return {
        "graph_schema_version": "0.1",
        "graph_sha256": "d" * 64,
        "partition_sha256": "e" * 64,
        "eligible_evidence_set_sha256": "f" * 64,
        "eligible_evidence_ids": [item["evidence_id"] for item in records],
        "nodes": nodes,
        "edges": edges,
    }


class RecordLookupQuestionGraphTests(unittest.TestCase):
    def test_claim_validation_uses_record_lookup_for_total_budget_surface(self) -> None:
        records = evidence_fixture()
        graph = source_graph(records)
        question = "承認済みの Project Atlas の合計予算を教えて。"
        plan = {
            "items": [PLAN["items"][4]],
            "answer_shape": "Budget",
        }
        artifact = qeg.build_question_evidence_graph(
            question,
            records,
            source_graph=graph,
            question_plan=plan,
        )
        graph_validation = qeg.validate_question_evidence_graph(
            question,
            records,
            artifact,
            source_graph=graph,
            question_plan=plan,
        )
        branch = artifact["branches"][0]
        answer_record = {
            "query": question,
            "question_plan": plan,
            "field_runs": [{
                "item": plan["items"][0],
                "audit": {
                    "item_id": "F5",
                    "verdict": "supported",
                    "supported_value": "10000",
                    "supporting_packet_ids": ["ev_row_approved"],
                    "competing_packet_ids": [],
                    "reason_code": "none",
                    "defect": "",
                    "missing_information": [],
                },
            }],
            "answer": {
                "answer_status": "answered",
                "answer_mode": "grounded",
                "answer": "確認できた内容:\n- Budget: 10000",
                "evidence_ids": ["ev_row_approved"],
            },
            "question_evidence_graph": artifact,
            "question_evidence_graph_validation": graph_validation,
        }
        packet_ids = set(branch["validation_evidence_ids"])
        packets = [
            {"evidence_id": item["evidence_id"], "text": item["text"]}
            for item in records
            if item["evidence_id"] in packet_ids
        ]

        contract, _, report = claim_validator.build_and_validate(
            answer_record,
            packets,
        )

        self.assertEqual(report["status"], "pass", report)
        self.assertEqual(contract["items"][0]["entity_type"], "numeric_value")
        self.assertEqual(
            claim_validator.record_lookup_field_bindings(answer_record)["F5"]["field_name"],
            "budget",
        )

        wrong_value = copy.deepcopy(answer_record)
        wrong_value["field_runs"][0]["audit"]["supported_value"] = "100"
        wrong_value["answer"]["answer"] = "確認できた内容:\n- Budget: 100"
        _, _, wrong_report = claim_validator.build_and_validate(
            wrong_value,
            packets,
        )
        self.assertEqual(wrong_report["status"], "blocked")
        self.assertIn(
            "record_lookup_value_mismatch",
            {failure["code"] for failure in wrong_report["failures"]},
        )

        decimal_equivalent = copy.deepcopy(answer_record)
        decimal_equivalent["field_runs"][0]["audit"]["supported_value"] = "10000.0"
        decimal_equivalent["answer"]["answer"] = (
            "確認できた内容:\n- Budget: 10000.0"
        )
        _, _, decimal_report = claim_validator.build_and_validate(
            decimal_equivalent,
            packets,
        )
        self.assertEqual(decimal_report["status"], "pass", decimal_report)

        formula_records = evidence_fixture()
        for item in formula_records:
            if item["evidence_id"] == "ev_v_f_approved":
                item["text"] = "=D3*E3"
            elif item["evidence_id"] == "ev_row_approved":
                item["text"] = item["text"].replace(
                    "Budget: 10000",
                    "Budget: =D3*E3 [保存値・ファイル保存時・未再計算: 10000]",
                )
        formula_graph = source_graph(formula_records)
        formula_artifact = qeg.build_question_evidence_graph(
            question,
            formula_records,
            source_graph=formula_graph,
            question_plan=plan,
        )
        formula_validation = qeg.validate_question_evidence_graph(
            question,
            formula_records,
            formula_artifact,
            source_graph=formula_graph,
            question_plan=plan,
        )
        formula_branch = formula_artifact["branches"][0]
        formula_record = copy.deepcopy(answer_record)
        formula_record["field_runs"][0]["audit"]["supported_value"] = "= d3 * e3"
        formula_record["answer"]["answer"] = "確認できた内容:\n- Budget: = d3 * e3"
        formula_record["question_evidence_graph"] = formula_artifact
        formula_record["question_evidence_graph_validation"] = formula_validation
        formula_packet_ids = set(formula_branch["validation_evidence_ids"])
        formula_packets = [
            {"evidence_id": item["evidence_id"], "text": item["text"]}
            for item in formula_records
            if item["evidence_id"] in formula_packet_ids
        ]

        formula_contract, _, formula_report = claim_validator.build_and_validate(
            formula_record,
            formula_packets,
        )

        self.assertEqual(formula_report["status"], "pass", formula_report)
        self.assertEqual(formula_contract["items"][0]["entity_type"], "text_value")
        self.assertTrue(claim_validator.record_lookup_value_matches(
            "= d3 * e3",
            "=D3*E3 [保存値・ファイル保存時・未再計算: 10000]",
        ))
        self.assertFalse(claim_validator.record_lookup_value_matches(
            "AB-CD", "ABCD",
        ))

    def test_claim_formula_projection_preserves_semantic_operand_spaces(self) -> None:
        self.assertTrue(claim_validator.record_lookup_value_matches(
            "= d3 * e3", "=D3*E3",
        ))
        self.assertFalse(claim_validator.record_lookup_value_matches(
            '="East Division"', '="EastDivision"',
        ))
        self.assertFalse(claim_validator.record_lookup_value_matches(
            "='East Division'!A1", "='EastDivision'!A1",
        ))
        self.assertFalse(claim_validator.record_lookup_value_matches(
            "=Table1[East Division]", "=Table1[EastDivision]",
        ))
        self.assertFalse(claim_validator.record_lookup_value_matches(
            "=A1 B1", "=A1B1",
        ))
        self.assertFalse(claim_validator.record_lookup_value_matches(
            '="North ""Region"" Team"', '="North ""Region""Team"',
        ))
        self.assertFalse(claim_validator.record_lookup_value_matches(
            "='North ''Region'' Team'!A1", "='North ''Region''Team'!A1",
        ))
        self.assertTrue(claim_validator.record_lookup_value_matches(
            "= SUM( A1 , B1 )", "=sum(a1,b1)",
        ))
        self.assertTrue(claim_validator.record_lookup_value_matches(
            "=A1   B1", "=a1 B1",
        ))
        self.assertFalse(claim_validator.record_lookup_value_matches(
            '="North"', '="north"',
        ))
        self.assertFalse(claim_validator.record_lookup_value_matches(
            "=Table1[North]", "=table1[north]",
        ))
        self.assertFalse(claim_validator.record_lookup_value_matches(
            "=Table1[保存値:列]", "=Table1",
        ))
        self.assertFalse(claim_validator.record_lookup_value_matches(
            "=Table1[保存値:10000]", "=Table1",
        ))

    def test_complete_graph_resolves_each_requested_field_and_validates(self) -> None:
        records = evidence_fixture()
        graph = source_graph(records)

        artifact = qeg.build_question_evidence_graph(
            QUESTION,
            records,
            source_graph=graph,
            question_plan=PLAN,
        )

        self.assertEqual((artifact["status"], artifact["intent"]["operation"]), ("ready", "record_lookup"))
        self.assertEqual(
            {branch["item_id"]: branch["value"] for branch in artifact["branches"]},
            {
                "F1": "Maya Chen",
                "F2": "2026-09-15",
                "F3": "1250",
                "F4": "8",
                "F5": "10000",
            },
        )
        self.assertEqual(
            artifact["selected_evidence_ids"],
            list(dict.fromkeys(
                evidence_id
                for branch in artifact["branches"]
                for evidence_id in branch["selected_evidence_ids"]
            )),
        )
        self.assertTrue(all(branch["stored_graph_binding"] for branch in artifact["branches"]))
        self.assertTrue(all(
            set(branch["validation_evidence_ids"])
            <= set(branch["stored_graph_binding"]["required_evidence_ids"])
            for branch in artifact["branches"]
        ))
        validation = qeg.validate_question_evidence_graph(
            QUESTION,
            records,
            artifact,
            source_graph=graph,
            question_plan=PLAN,
        )
        self.assertEqual(validation["status"], "pass", validation)

    def test_required_value_lineage_edge_ablation_holds(self) -> None:
        records = evidence_fixture()
        graph = source_graph(records)
        graph["edges"] = [
            edge for edge in graph["edges"]
            if edge["relation_id"]
            != "rel_ev_row_approved_from_ev_v_b_approved"
        ]

        artifact = qeg.build_question_evidence_graph(
            QUESTION,
            records,
            source_graph=graph,
            question_plan=PLAN,
        )

        self.assertEqual((artifact["status"], artifact["reason"]), ("hold", "stored_graph_lineage_failed"))

    def test_unrelated_edge_ablation_keeps_the_same_answer_paths(self) -> None:
        records = evidence_fixture()
        complete_graph = source_graph(records)
        ablated_graph = copy.deepcopy(complete_graph)
        ablated_graph["edges"] = [
            edge for edge in ablated_graph["edges"]
            if edge["relation_id"] != "rel_contains_ev_unrelated_note"
        ]

        complete = qeg.build_question_evidence_graph(
            QUESTION,
            records,
            source_graph=complete_graph,
            question_plan=PLAN,
        )
        ablated = qeg.build_question_evidence_graph(
            QUESTION,
            records,
            source_graph=ablated_graph,
            question_plan=PLAN,
        )

        self.assertEqual((complete["status"], ablated["status"]), ("ready", "ready"))
        self.assertEqual(
            [(branch["value"], branch["primary_path"]) for branch in complete["branches"]],
            [(branch["value"], branch["primary_path"]) for branch in ablated["branches"]],
        )
        self.assertEqual(complete["selected_evidence_ids"], ablated["selected_evidence_ids"])

    def test_nearest_header_and_annotated_formula_bind_to_raw_formula_cell(self) -> None:
        records = evidence_fixture()
        for item in records:
            locator = item["locator"]
            if locator.get("sheet_name") != "Project Plan":
                continue
            if "row_index" in locator:
                locator["row_index"] += 1
            cell = locator.get("cell")
            if isinstance(cell, str):
                match = re.fullmatch(r"([A-Z]+)(\d+)", cell)
                assert match is not None
                locator["cell"] = f"{match.group(1)}{int(match.group(2)) + 1}"
            if item["evidence_id"] == "ev_v_f_approved":
                item["text"] = "=D3*E3"
            elif item["evidence_id"] == "ev_row_approved":
                item["text"] = item["text"].replace(
                    "Budget: 10000",
                    "Budget: =D3*E3 [保存値・ファイル保存時・未再計算: 10000]",
                )
        graph = source_graph(records)

        artifact = qeg.build_question_evidence_graph(
            QUESTION,
            records,
            source_graph=graph,
            question_plan=PLAN,
        )

        self.assertEqual(artifact["status"], "ready", artifact)
        budget = next(
            branch for branch in artifact["branches"]
            if branch["item_id"] == "F5"
        )
        self.assertEqual(
            budget["value"],
            "=D3*E3",
        )
        self.assertEqual(
            budget["source_value"],
            "=D3*E3 [保存値・ファイル保存時・未再計算: 10000]",
        )
        self.assertIn("ev_v_f_approved", budget["selected_evidence_ids"])

    def test_formula_projection_preserves_semantic_whitespace(self) -> None:
        records = evidence_fixture()
        for item in records:
            if item["evidence_id"] == "ev_v_f_approved":
                item["text"] = '=IF(A1="EastDivision",1,0)'
            elif item["evidence_id"] == "ev_row_approved":
                item["text"] = item["text"].replace(
                    "Budget: 10000",
                    'Budget: =IF(A1="East Division",1,0)',
                )
        graph = source_graph(records)

        artifact = qeg.build_question_evidence_graph(
            QUESTION,
            records,
            source_graph=graph,
            question_plan=PLAN,
        )

        self.assertEqual(
            (artifact["status"], artifact["reason"]),
            ("hold", "stored_graph_lineage_failed"),
        )
        self.assertTrue(qeg._raw_value_matches("= d3 * e3", "=D3*E3"))
        self.assertFalse(qeg._raw_value_matches("=A1 B1", "=A1B1"))
        self.assertFalse(qeg._raw_value_matches(
            "='East Division'!A1", "='EastDivision'!A1"
        ))
        self.assertFalse(qeg._raw_value_matches(
            "=Table[Sales Region]", "=Table[SalesRegion]"
        ))
        self.assertFalse(qeg._raw_value_matches('="ABC"', '="abc"'))
        self.assertTrue(qeg._raw_value_matches(
            '= IF(A1="North ""Region""", 1, 0)',
            '=if(A1="North ""Region""",1,0)',
        ))
        self.assertTrue(qeg._raw_value_matches(
            "= 'North ''Region''' ! A1",
            "='North ''Region'''!a1",
        ))
        self.assertFalse(qeg._raw_value_matches(
            "=Table1[保存値]", "=Table1"
        ))
        self.assertTrue(qeg._raw_value_matches(
            "=E3*F3 [保存値・ファイル保存時・未再計算: 48000]",
            "=e3 * f3",
        ))

    def test_punctuation_difference_cannot_bind_a_row_value_to_a_raw_cell(self) -> None:
        records = evidence_fixture()
        for item in records:
            if item["evidence_id"] == "ev_row_approved":
                item["text"] = item["text"].replace(
                    "Owner: Maya Chen", "Owner: F-1",
                )
            elif item["evidence_id"] == "ev_v_b_approved":
                item["text"] = json.dumps("F1")
        graph = source_graph(records)

        artifact = qeg.build_question_evidence_graph(
            QUESTION,
            records,
            source_graph=graph,
            question_plan=PLAN,
        )

        self.assertEqual(
            (artifact["status"], artifact["reason"]),
            ("hold", "stored_graph_lineage_failed"),
        )

    def test_json_string_literal_raw_cells_bind_to_plain_row_values(self) -> None:
        records = evidence_fixture()
        for item in records:
            if item["evidence_id"].startswith(("ev_h_", "ev_v_")):
                item["text"] = json.dumps(item["text"], ensure_ascii=False)
        graph = source_graph(records)

        artifact = qeg.build_question_evidence_graph(
            QUESTION,
            records,
            source_graph=graph,
            question_plan=PLAN,
        )

        self.assertEqual(artifact["status"], "ready", artifact)
        self.assertEqual(
            [branch["value"] for branch in artifact["branches"]],
            ["Maya Chen", "2026-09-15", "1250", "8", "10000"],
        )

    def test_final_status_aliases_are_consistent_with_final_question_surfaces(self) -> None:
        owner_plan = {
            "items": [PLAN["items"][0]],
            "answer_shape": "Owner",
        }
        for status, question in (
            (
                "Finalized",
                "For the finalized Project Atlas record, give the Owner.",
            ),
            (
                "Final",
                "Finalの Project Atlas レコードの Owner を教えて。",
            ),
            (
                "Approved",
                "Approvedを示す Project Atlas レコードの Owner を教えて。",
            ),
            (
                "承認済み",
                "承認済みの Project Atlas レコードの Owner を教えて。",
            ),
            (
                "最終",
                "最終の Project Atlas レコードの Owner を教えて。",
            ),
            (
                "最終版",
                "最終版の Project Atlas レコードの Owner を教えて。",
            ),
            (
                "最終確定",
                "最終確定の Project Atlas レコードの Owner を教えて。",
            ),
        ):
            with self.subTest(status=status):
                records = evidence_fixture()
                for item in records:
                    if item["evidence_id"] == "ev_row_approved":
                        item["text"] = item["text"].replace(
                            "Status: Approved", f"Status: {status}",
                        )
                    elif item["evidence_id"] == "ev_v_g_approved":
                        item["text"] = status
                graph = source_graph(records)

                artifact = qeg.build_question_evidence_graph(
                    question,
                    records,
                    source_graph=graph,
                    question_plan=owner_plan,
                )

                self.assertEqual(artifact["status"], "ready", artifact)
                self.assertEqual(artifact["selection"]["status"], status)

    def test_latin_final_surface_before_japanese_particle_rejects_draft_only(self) -> None:
        owner_plan = {
            "items": [PLAN["items"][0]],
            "answer_shape": "Owner",
        }
        records = [
            item for item in evidence_fixture()
            if not item["evidence_id"].endswith("_approved")
        ]
        graph = source_graph(records)
        for surface in ("Final", "Finalized", "Approved"):
            with self.subTest(surface=surface):
                artifact = qeg.build_question_evidence_graph(
                    f"{surface}の Project Atlas レコードの Owner を教えて。",
                    records,
                    source_graph=graph,
                    question_plan=owner_plan,
                )

                self.assertEqual(
                    (artifact["status"], artifact["reason"]),
                    ("hold", "record_lookup_final_status_not_found"),
                )

    def test_negated_final_surface_holds_without_selecting_any_record(self) -> None:
        records = evidence_fixture()
        graph = source_graph(records)
        owner_plan = {
            "items": [PLAN["items"][0]],
            "answer_shape": "Owner",
        }
        for question in (
            (
                "For the Project Atlas record that is not finalized, "
                "give the Owner."
            ),
            "承認済みではない Project Atlas レコードの Owner を教えて。",
            "最終版ではない Project Atlas レコードの Owner を教えて。",
            "The Project Atlas record isn't finalized; give the Owner.",
            "承認済みではありません Project Atlas レコードの Owner を教えて。",
            "最終版ではございません Project Atlas レコードの Owner を教えて。",
        ):
            with self.subTest(question=question):
                artifact = qeg.build_question_evidence_graph(
                    question,
                    records,
                    source_graph=graph,
                    question_plan=owner_plan,
                )

                self.assertEqual(
                    (artifact["status"], artifact["reason"]),
                    ("hold", "record_lookup_negated_final_not_supported"),
                )
                self.assertIsNone(artifact["selection"])
                self.assertEqual(artifact["selected_evidence_ids"], [])

    def test_duplicate_field_items_hold_instead_of_sharing_one_status_scope(self) -> None:
        records = evidence_fixture()
        graph = source_graph(records)
        duplicate_field_plan = {
            "items": [
                {
                    **PLAN["items"][0],
                    "item_id": "F_APPROVED",
                    "label": "Approved Owner",
                },
                {
                    **PLAN["items"][0],
                    "item_id": "F_DRAFT",
                    "label": "Draft Owner",
                },
            ],
            "answer_shape": "Approved Owner / Draft Owner",
        }

        artifact = qeg.build_question_evidence_graph(
            (
                "Give the Approved and Draft Owner for each East Division "
                "Onboarding record."
            ),
            records,
            source_graph=graph,
            question_plan=duplicate_field_plan,
        )

        self.assertEqual(
            (artifact["status"], artifact["reason"]),
            ("hold", "record_lookup_field_duplicate"),
        )
        self.assertIsNone(artifact["selection"])

    def test_conflicting_final_and_nonfinal_scopes_hold(self) -> None:
        records = evidence_fixture()
        graph = source_graph(records)
        plan = {
            "items": [PLAN["items"][0], PLAN["items"][4]],
            "answer_shape": "Owner / Budget",
        }

        artifact = qeg.build_question_evidence_graph(
            (
                "Give the Approved Owner and Draft Budget for East Division "
                "Onboarding."
            ),
            records,
            source_graph=graph,
            question_plan=plan,
        )

        self.assertEqual(
            (artifact["status"], artifact["reason"]),
            ("hold", "record_lookup_status_scope_conflicting"),
        )
        self.assertIsNone(artifact["selection"])

    def test_nonfinal_status_scopes_are_not_projected_onto_approved_row(self) -> None:
        owner_plan = {
            "items": [PLAN["items"][0]],
            "answer_shape": "Owner",
        }
        records = [
            item for item in evidence_fixture()
            if not item["evidence_id"].endswith("_draft")
        ]
        graph = source_graph(records)
        for surface in (
            "Draft", "Old", "Superseded", "ドラフト", "下書き", "旧版", "廃止済み",
        ):
            with self.subTest(surface=surface):
                artifact = qeg.build_question_evidence_graph(
                    (
                        f"Give the {surface} Owner for East Division "
                        "Onboarding."
                    ),
                    records,
                    source_graph=graph,
                    question_plan=owner_plan,
                )

                self.assertEqual(
                    (artifact["status"], artifact["reason"]),
                    ("hold", "record_lookup_status_scope_not_supported"),
                )
                self.assertIsNone(artifact["selection"])

    def test_plan_item_not_mentioned_in_the_question_holds(self) -> None:
        records = evidence_fixture()
        graph = source_graph(records)
        question = "For the finalized Project Atlas record, give the Owner."

        artifact = qeg.build_question_evidence_graph(
            question,
            records,
            source_graph=graph,
            question_plan=PLAN,
        )

        self.assertEqual(
            (artifact["status"], artifact["reason"]),
            ("hold", "record_lookup_plan_not_grounded"),
        )

    def test_known_and_unknown_plan_items_hold_instead_of_bypassing_graph(self) -> None:
        records = evidence_fixture()
        graph = source_graph(records)
        question = (
            "For the finalized Project Atlas record, give the Owner "
            "and Description."
        )
        mixed_plan = {
            "items": [
                PLAN["items"][0],
                {
                    "item_id": "F2",
                    "label": "Description",
                    "required_claim": "Description for Project Atlas",
                    "retrieval_query": f"{question} Description",
                    "required": True,
                },
            ],
            "answer_shape": "Owner / Description",
        }

        artifact = qeg.build_question_evidence_graph(
            question,
            records,
            source_graph=graph,
            question_plan=mixed_plan,
        )

        self.assertEqual(
            (artifact["status"], artifact["intent"]["operation"], artifact["reason"]),
            ("hold", "record_lookup", "record_lookup_field_not_supported"),
        )

    def test_question_field_omitted_from_plan_holds_before_answering(self) -> None:
        records = evidence_fixture()
        graph = source_graph(records)
        cases = (
            (
                "unknown_question_field",
                (
                    "For the finalized Project Atlas record, give "
                    "the Owner and Description."
                ),
                {
                    "items": [PLAN["items"][0]],
                    "answer_shape": "Owner",
                },
            ),
            (
                "unknown_field_only_in_claim_metadata",
                "For the finalized Project Atlas record, give the Owner.",
                {
                    "items": [{
                        **PLAN["items"][0],
                        "required_claim": "Owner and Description",
                        "retrieval_query": "Project Atlas Owner Description",
                    }],
                    "answer_shape": "Owner",
                },
            ),
            (
                "known_question_field",
                (
                    "For the finalized Project Atlas record, give "
                    "the Owner and Budget."
                ),
                {
                    "items": [PLAN["items"][0]],
                    "answer_shape": "Owner",
                },
            ),
        )
        for name, question, plan in cases:
            with self.subTest(name=name):
                artifact = qeg.build_question_evidence_graph(
                    question,
                    records,
                    source_graph=graph,
                    question_plan=plan,
                )

                self.assertEqual(
                    (artifact["status"], artifact["reason"]),
                    ("hold", "record_lookup_question_field_not_planned"),
                )
                self.assertIsNone(artifact["selection"])

    def test_planner_paraphrase_uses_one_known_field_mention(self) -> None:
        records = evidence_fixture()
        graph = source_graph(records)
        question = "For the finalized Project Atlas record, give the Owner."
        paraphrased_plan = {
            "items": [{
                "item_id": "F1",
                "label": "Project owner",
                "required_claim": "Owner for Project Atlas",
                "retrieval_query": "Project Atlas owner",
                "required": True,
            }],
            "answer_shape": "Project owner",
        }

        artifact = qeg.build_question_evidence_graph(
            question,
            records,
            source_graph=graph,
            question_plan=paraphrased_plan,
        )

        self.assertEqual(artifact["status"], "ready", artifact)
        self.assertEqual(artifact["branches"][0]["value"], "Maya Chen")

    def test_latin_alias_substrings_do_not_activate_record_lookup(self) -> None:
        records = evidence_fixture()
        graph = source_graph(records)
        for phrase in ("Ownership structure", "Homeowner policy", "budgetary note"):
            with self.subTest(phrase=phrase):
                generic_plan = {
                    "items": [{
                        "item_id": "F1",
                        "label": phrase,
                        "required_claim": f"{phrase} for Project Atlas",
                        "retrieval_query": f"Project Atlas {phrase}",
                        "required": True,
                    }],
                    "answer_shape": phrase,
                }
                artifact = qeg.build_question_evidence_graph(
                    f"For the finalized Project Atlas record, give the {phrase}.",
                    records,
                    source_graph=graph,
                    question_plan=generic_plan,
                )

                self.assertEqual(
                    (artifact["status"], artifact["intent"]["operation"]),
                    ("unsupported", "unknown"),
                )

    def test_known_record_plan_precedes_count_surface(self) -> None:
        records = evidence_fixture()
        graph = source_graph(records)
        budget_plan = {
            "items": [PLAN["items"][4]],
            "answer_shape": "Budget",
        }

        artifact = qeg.build_question_evidence_graph(
            "承認済みの Project Atlas の合計予算を教えて。",
            records,
            source_graph=graph,
            question_plan=budget_plan,
        )

        self.assertEqual(
            (artifact["status"], artifact["intent"]["operation"]),
            ("ready", "record_lookup"),
        )
        self.assertEqual(artifact["branches"][0]["value"], "10000")

        pure_count = qeg.build_question_evidence_graph(
            "Project Atlas の件数は何件ですか。",
            records,
            source_graph=graph,
        )
        self.assertEqual(
            pure_count["intent"]["operation"],
            "aggregate_count",
        )

        owner_plan = {
            "items": [PLAN["items"][0]],
            "answer_shape": "integer",
        }
        for surface in (
            "何回", "回数", "何枠", "枠数", "何件", "件数", "総数",
        ):
            with self.subTest(strong_count_surface=surface):
                strong_count = qeg.build_question_evidence_graph(
                    (
                        "承認済みの Project Atlas レコードの "
                        f"Owner の{surface}を教えて。"
                    ),
                    records,
                    source_graph=graph,
                    question_plan=owner_plan,
                )
                self.assertEqual(
                    strong_count["intent"]["operation"],
                    "aggregate_count",
                )

    def test_question_field_cannot_be_lost_by_unrecognized_planner_paraphrase(self) -> None:
        records = evidence_fixture()
        graph = source_graph(records)
        question = "For the finalized Project Atlas record, give the Owner."
        unresolved_plan = {
            "items": [{
                "item_id": "F1",
                "label": "Responsible person",
                "required_claim": "Owner for Project Atlas",
                "retrieval_query": "Project Atlas Owner",
                "required": True,
            }],
            "answer_shape": "Responsible person",
        }

        artifact = qeg.build_question_evidence_graph(
            question,
            records,
            source_graph=graph,
            question_plan=unresolved_plan,
        )

        self.assertEqual(
            (artifact["status"], artifact["intent"]["operation"], artifact["reason"]),
            ("hold", "record_lookup", "record_lookup_field_not_supported"),
        )

    def test_one_plan_item_with_two_known_fields_holds_as_ambiguous(self) -> None:
        records = evidence_fixture()
        graph = source_graph(records)
        question = (
            "For the finalized Project Atlas record, give the Owner "
            "and Budget."
        )
        ambiguous_plan = {
            "items": [{
                "item_id": "F1",
                "label": "Owner and Budget",
                "required_claim": "Owner and Budget for Project Atlas",
                "retrieval_query": question,
                "required": True,
            }],
            "answer_shape": "Owner / Budget",
        }

        artifact = qeg.build_question_evidence_graph(
            question,
            records,
            source_graph=graph,
            question_plan=ambiguous_plan,
        )

        self.assertEqual(
            (artifact["status"], artifact["intent"]["operation"], artifact["reason"]),
            ("hold", "record_lookup", "record_lookup_field_ambiguous"),
        )

    def test_malformed_known_field_plan_cannot_bypass_graph(self) -> None:
        records = evidence_fixture()
        graph = source_graph(records)
        question = "For the finalized Project Atlas record, give the Owner."
        duplicate_id_plan = {
            "items": [
                {
                    "item_id": "F1",
                    "label": "Owner",
                    "required_claim": "Owner for Project Atlas",
                    "retrieval_query": "Project Atlas Owner",
                    "required": True,
                },
                {
                    "item_id": "F1",
                    "label": "Owner",
                    "required_claim": "Owner for Project Atlas",
                    "retrieval_query": "Project Atlas Owner",
                    "required": True,
                },
            ],
            "answer_shape": "Owner",
        }

        artifact = qeg.build_question_evidence_graph(
            question,
            records,
            source_graph=graph,
            question_plan=duplicate_id_plan,
        )

        self.assertEqual(
            (artifact["status"], artifact["intent"]["operation"], artifact["reason"]),
            ("hold", "record_lookup", "record_lookup_plan_invalid"),
        )

    def test_all_unknown_plan_items_keep_generic_not_applicable(self) -> None:
        records = evidence_fixture()
        graph = source_graph(records)
        question = "Describe Project Atlas."
        generic_plan = {
            "items": [{
                "item_id": "F1",
                "label": "Description",
                "required_claim": "Description for Project Atlas",
                "retrieval_query": "Project Atlas Description",
                "required": True,
            }],
            "answer_shape": "Description",
        }

        artifact = qeg.build_question_evidence_graph(
            question,
            records,
            source_graph=graph,
            question_plan=generic_plan,
        )

        self.assertEqual(
            (artifact["status"], artifact["intent"]["operation"]),
            ("unsupported", "unknown"),
        )

    def test_two_final_rows_for_the_same_subject_hold_as_ambiguous(self) -> None:
        records = evidence_fixture(second_approved=True)
        graph = source_graph(records)

        artifact = qeg.build_question_evidence_graph(
            QUESTION,
            records,
            source_graph=graph,
            question_plan=PLAN,
        )

        self.assertEqual((artifact["status"], artifact["reason"]), ("hold", "record_lookup_candidate_ambiguous"))

    def test_record_lookup_requires_the_same_plan_and_stored_binding(self) -> None:
        records = evidence_fixture()
        graph = source_graph(records)
        artifact = qeg.build_question_evidence_graph(
            QUESTION,
            records,
            source_graph=graph,
            question_plan=PLAN,
        )

        missing_plan = qeg.validate_question_evidence_graph(
            QUESTION,
            records,
            artifact,
            source_graph=graph,
        )
        self.assertEqual(missing_plan["status"], "blocked")

        tampered = copy.deepcopy(artifact)
        tampered["stored_graph_binding"] = None
        body = {
            key: value for key, value in tampered.items()
            if key not in {"artifact_hash", "artifact_id"}
        }
        artifact_hash = qeg.stable_hash(body)
        tampered["artifact_hash"] = artifact_hash
        tampered["artifact_id"] = f"qeg_{artifact_hash[:24]}"
        validation = qeg.validate_question_evidence_graph(
            QUESTION,
            records,
            tampered,
            source_graph=graph,
            question_plan=PLAN,
        )
        self.assertEqual(validation["status"], "blocked")
        self.assertIn(
            "stored_graph_binding_missing",
            {failure["code"] for failure in validation["failures"]},
        )


if __name__ == "__main__":
    unittest.main()
