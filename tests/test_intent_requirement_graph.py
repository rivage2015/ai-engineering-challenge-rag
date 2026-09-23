"""Pure tests: no model, files, source index, or installed application used."""

import copy
import importlib.util
from pathlib import Path
import unittest


MODULE_PATH = Path(__file__).resolve().parents[1] / (
    "distribution/macos-local-memory/engine/intent_requirement_graph.py")
SPEC = importlib.util.spec_from_file_location("intent_requirement_graph", MODULE_PATH)
graph = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(graph)


class IntentRequirementGraphTests(unittest.TestCase):
    def contract(self, requirements=None):
        return {"version": 1, "question": "開店までの手順は？",
                "goal": "準備から開店までの流れを知りたい。",
                "requirements": requirements or ["手順と明示された条件"],
                "revision": {"generation": "test-generation", "sha256": "abc"},
                "expires_at": 12345}

    def test_all_eight_requirements_bind_without_dropping(self):
        contract = self.contract([f"必要な内容 {n}" for n in range(1, 9)])
        plan = graph.plan_from_contract(contract)
        graph.validate_plan_binding(plan)
        self.assertEqual(len(plan["items"]), 8)
        self.assertEqual([item["item_id"] for item in plan["items"]],
                         [f"F{n}" for n in range(1, 9)])
        self.assertEqual([item["intent_requirement_id"] for item in plan["items"]],
                         [f"R{n}" for n in range(1, 9)])
        self.assertTrue(all(item["required"] is True for item in plan["items"]))
        self.assertEqual([item["required_claim"] for item in plan["items"]],
                         contract["requirements"])
        self.assertEqual(len(plan["intent_graph"]["nodes"]), 10)
        self.assertEqual(len(plan["intent_graph"]["edges"]), 17)

    def test_generic_request_adds_no_actor_or_source_semantics(self):
        compiled = graph.compile_contract(self.contract())
        self.assertEqual([node["node_type"] for node in compiled["nodes"]],
                         ["question", "goal", "requirement"])
        self.assertEqual({edge["type"] for edge in compiled["edges"]},
                         {"expresses_goal", "requires", "scopes"})
        self.assertTrue(all(edge["basis"] == "explicit_user_contract"
                            for edge in compiled["edges"]))
        self.assertNotIn("actor", compiled)
        self.assertNotIn("relations_adopted", compiled)
        self.assertNotIn("evidence", compiled)

    def test_original_multiline_text_is_preserved(self):
        contract = self.contract(["条件\n\t例外も\r\n説明する"])
        contract["question"] = "手順は？\nいつ開始する？"
        plan = graph.plan_from_contract(contract)
        self.assertEqual(plan["intent_graph"]["question"], contract["question"])
        self.assertEqual(plan["intent_graph"]["nodes"][2]["text"],
                         contract["requirements"][0])
        self.assertEqual(plan["intent_graph"]["nodes"][2]["source_pointer"],
                         "/requirements/0")
        self.assertEqual(plan["items"][0]["required_claim"], "条件 例外も 説明する")
        graph.validate_plan_binding(plan)

    def test_only_display_label_is_truncated(self):
        requirement = "手順" * 150
        plan = graph.plan_from_contract(self.contract([requirement]))
        self.assertEqual(len(plan["items"][0]["label"]), 80)
        self.assertEqual(plan["items"][0]["required_claim"], requirement)
        self.assertEqual(plan["items"][0]["retrieval_query"], requirement)
        self.assertEqual(plan["intent_graph"]["requirements"], [requirement])

    def test_hash_includes_goal_revision_and_requirements_but_not_expiry(self):
        original = self.contract(["順序", "例外"])
        initial = graph.compile_contract(original)["contract_sha256"]
        changed = copy.deepcopy(original)
        changed["expires_at"] += 999
        self.assertEqual(graph.compile_contract(changed)["contract_sha256"], initial)
        for field, value in [("question", "開始時刻は？"), ("goal", "終了まで知りたい"),
                             ("revision", {"generation": "next"}),
                             ("requirements", ["例外", "順序"])]:
            with self.subTest(field=field):
                changed = copy.deepcopy(original)
                changed[field] = value
                self.assertNotEqual(graph.compile_contract(changed)["contract_sha256"],
                                    initial)

    def test_duplicate_text_still_has_distinct_requirement_identity(self):
        plan = graph.plan_from_contract(self.contract(["条件", "条件"]))
        self.assertEqual([item["intent_requirement_id"] for item in plan["items"]],
                         ["R1", "R2"])
        graph.validate_plan_binding(plan)

    def test_input_mutation_cannot_change_graph_provenance(self):
        contract = self.contract()
        compiled = graph.compile_contract(contract)
        contract["revision"]["generation"] = "changed"
        contract["requirements"][0] = "changed"
        self.assertEqual(compiled["revision"]["generation"], "test-generation")
        self.assertEqual(compiled["requirements"], ["手順と明示された条件"])

    def test_bound_plan_rejects_mutation_drop_reorder_or_unbound_metadata(self):
        original = graph.plan_from_contract(self.contract(["順序", "条件", "例外"]))
        mutations = [
            lambda p: p["items"].pop(),
            lambda p: p["items"].reverse(),
            lambda p: p["items"][0].update(required=False),
            lambda p: p["items"][0].update(required_claim="変更した内容"),
            lambda p: p["items"][0].update(intent_requirement_id="R9"),
            lambda p: p["items"][0].update(required=1),
            lambda p: p["intent_graph"].update(contract_sha256="0" * 64),
            lambda p: p["intent_graph"]["edges"].pop(),
            lambda p: p["intent_graph"]["nodes"][0].update(text="別の質問"),
            lambda p: p["intent_graph"].update(schema_version="future"),
            lambda p: p.update(answer_shape="都合のよい回答形式"),
        ]
        for index, mutation in enumerate(mutations):
            with self.subTest(index=index):
                plan = copy.deepcopy(original)
                mutation(plan)
                with self.assertRaises(ValueError):
                    graph.validate_plan_binding(plan)

    def test_engine_metadata_does_not_change_owned_contract_fields(self):
        plan = graph.plan_from_contract(self.contract())
        plan["partial_answer_allowed"] = True
        graph.validate_plan_binding(plan)

    def test_explicit_unique_answer_constraint_is_not_lost(self):
        value = self.contract()
        value['goal'] = '対象を一つに確定してください。'
        plan = graph.plan_from_contract(value)
        self.assertFalse(plan['partial_answer_allowed'])
        graph.validate_plan_binding(plan)
        plan['partial_answer_allowed'] = True
        with self.assertRaises(ValueError):
            graph.validate_plan_binding(plan)

    def test_invalid_contracts_fail_closed(self):
        bad_fields = [
            ("version", True), ("version", 2), ("version", "1"),
            ("question", ""), ("question", " \n\t"), ("question", "x" * 2001),
            ("goal", "x" * 3001), ("goal", None),
            ("requirements", []), ("requirements", ["x"] * 9),
            ("requirements", ["x" * 301]), ("requirements", [1]),
            ("requirements", "条件"), ("requirements", ["\n\t"]),
            ("revision", []), ("revision", {"bad": float("nan")}),
            ("question", "手順\x00は？"), ("goal", "\u202e反転"),
            ("requirements", ["\ud800"]),
        ]
        for field, value in bad_fields:
            with self.subTest(field=field, value=repr(value)):
                contract = self.contract()
                contract[field] = value
                with self.assertRaises(ValueError):
                    graph.compile_contract(contract)
        for value in (None, [], "contract"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                graph.compile_contract(value)


if __name__ == "__main__":
    unittest.main()
