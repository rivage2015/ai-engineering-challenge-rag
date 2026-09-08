#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "engine" / "document_version_resolver.py"
SPEC = importlib.util.spec_from_file_location("document_version_resolver", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
resolver = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(resolver)
BUILDER_PATH = Path(__file__).resolve().parents[1] / "engine" / "build_adaptive_semantic_graph.py"
BUILDER_SPEC = importlib.util.spec_from_file_location("adaptive_builder_for_versions", BUILDER_PATH)
assert BUILDER_SPEC is not None and BUILDER_SPEC.loader is not None
builder = importlib.util.module_from_spec(BUILDER_SPEC)
BUILDER_SPEC.loader.exec_module(builder)
VALIDATOR_PATH = Path(__file__).resolve().parents[1] / "engine" / "validate_adaptive_semantic_graph.py"
VALIDATOR_SPEC = importlib.util.spec_from_file_location("adaptive_validator_for_versions", VALIDATOR_PATH)
assert VALIDATOR_SPEC is not None and VALIDATOR_SPEC.loader is not None
validator = importlib.util.module_from_spec(VALIDATOR_SPEC)
VALIDATOR_SPEC.loader.exec_module(validator)


def inventory_record(path: str, digest: str) -> dict:
    return {
        "relative_path": path,
        "kind": "file",
        "size_bytes": 100,
        "mtime_ns": 2_000_000_000,
        "birthtime_ns": 1_000_000_000,
        "sha256": digest * 64,
        "read_status": "observed",
    }


def write_inventory(path: Path, records: list[dict]) -> None:
    path.write_text(
        "".join(resolver.canonical_json(record) + "\n" for record in records),
        encoding="utf-8",
    )


class DocumentVersionResolverTests(unittest.TestCase):
    def build(self, records: list[dict], decisions: Path | None = None):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        inventory = root / "inventory.jsonl"
        graph = root / "graph.json"
        write_inventory(inventory, records)
        result = resolver.build(inventory, graph, decisions)
        return root, inventory, graph, result

    def test_unique_latest_explicit_year_is_selected(self) -> None:
        _, inventory, graph, result = self.build([
            inventory_record("DAWN/業務内容2024.xlsx", "a"),
            inventory_record("DAWN/業務内容2025.xlsx", "b"),
        ])
        self.assertEqual({"groups": 1, "resolved": 1, "needs_human_review": 0}, result["counts"])
        group = result["groups"][0]
        self.assertEqual("DAWN/業務内容2025.xlsx", group["selected_relative_path"])
        self.assertEqual("unique_latest_explicit_year", group["reason_code"])
        self.assertEqual(
            ["historical", "active"],
            [item["disposition"] for item in group["candidates"]],
        )
        self.assertEqual("PASS", resolver.validate(graph, inventory)["status"])

    def test_year_directories_and_lifecycle_directories_share_a_family(self) -> None:
        _, _, _, result = self.build([
            inventory_record("DAWN/旧資料/2024年/業務内容2024.xlsx", "a"),
            inventory_record("DAWN/実際に使うもの/2025年/業務内容2025.xlsx", "b"),
        ])
        self.assertEqual(1, result["counts"]["groups"])
        self.assertEqual("resolved", result["groups"][0]["status"])

    def test_status_conflict_requires_human_review(self) -> None:
        _, _, _, result = self.build([
            inventory_record("DAWN/現行/業務内容2024.xlsx", "a"),
            inventory_record("DAWN/業務内容2025.xlsx", "b"),
        ])
        group = result["groups"][0]
        self.assertEqual("needs_human_review", group["status"])
        self.assertEqual("current_marker_conflicts_with_latest_year", group["reason_code"])
        self.assertTrue(all(item["disposition"] == "needs_human_review" for item in group["candidates"]))

    def test_latest_draft_requires_human_review(self) -> None:
        _, _, _, result = self.build([
            inventory_record("DAWN/業務内容2024.xlsx", "a"),
            inventory_record("DAWN/業務内容2025_下書き.xlsx", "b"),
        ])
        self.assertEqual("needs_human_review", result["groups"][0]["status"])
        self.assertEqual("latest_year_is_draft", result["groups"][0]["reason_code"])

    def test_unique_current_folder_can_resolve_without_dates(self) -> None:
        _, _, _, result = self.build([
            inventory_record("DAWN/とりあえず倉庫/業務内容.xlsx", "a"),
            inventory_record("DAWN/使うファイル/業務内容.xlsx", "b"),
        ])
        group = result["groups"][0]
        self.assertEqual("resolved", group["status"])
        self.assertEqual("unique_explicit_current_marker", group["reason_code"])
        self.assertEqual("DAWN/使うファイル/業務内容.xlsx", group["selected_relative_path"])

    def test_explicit_versions_are_compared_when_year_is_absent(self) -> None:
        _, _, _, result = self.build([
            inventory_record("DAWN/業務内容_ver1.2.xlsx", "a"),
            inventory_record("DAWN/業務内容_ver1.10.xlsx", "b"),
        ])
        group = result["groups"][0]
        self.assertEqual("resolved", group["status"])
        self.assertEqual("unique_latest_explicit_version", group["reason_code"])
        self.assertEqual("DAWN/業務内容_ver1.10.xlsx", group["selected_relative_path"])

    def test_human_decision_is_hash_bound_and_reused(self) -> None:
        root, inventory, graph, initial = self.build([
            inventory_record("DAWN/現行/業務内容2024.xlsx", "a"),
            inventory_record("DAWN/業務内容2025.xlsx", "b"),
        ])
        decisions = root / "decisions.json"
        group = initial["groups"][0]
        resolver.record_decision(
            graph, decisions, group["group_id"], "DAWN/業務内容2025.xlsx", "user"
        )
        rebuilt_path = root / "rebuilt.json"
        rebuilt = resolver.build(inventory, rebuilt_path, decisions)
        resolved = rebuilt["groups"][0]
        self.assertEqual("resolved", resolved["status"])
        self.assertEqual("human", resolved["resolution_basis"])
        self.assertEqual("DAWN/業務内容2025.xlsx", resolved["selected_relative_path"])

        changed_records = [
            inventory_record("DAWN/現行/業務内容2024.xlsx", "a"),
            inventory_record("DAWN/業務内容2025.xlsx", "c"),
        ]
        write_inventory(inventory, changed_records)
        stale_path = root / "stale.json"
        stale = resolver.build(inventory, stale_path, decisions)["groups"][0]
        self.assertEqual("needs_human_review", stale["status"])
        self.assertEqual("stale_human_decision", stale["reason_code"])

    def test_graph_hash_tampering_fails_validation(self) -> None:
        _, inventory, graph, _ = self.build([
            inventory_record("DAWN/業務内容2024.xlsx", "a"),
            inventory_record("DAWN/業務内容2025.xlsx", "b"),
        ])
        value = json.loads(graph.read_text(encoding="utf-8"))
        value["groups"][0]["status"] = "needs_human_review"
        graph.write_text(json.dumps(value), encoding="utf-8")
        report = resolver.validate(graph, inventory)
        self.assertEqual("FAIL", report["status"])
        self.assertIn("graph_hash_mismatch", report["errors"])

    def test_answer_policy_keeps_active_and_holds_other_candidates(self) -> None:
        _, _, graph, result = self.build([
            inventory_record("DAWN/業務内容2024.xlsx", "a"),
            inventory_record("DAWN/業務内容2025.xlsx", "b"),
        ])
        selected = [
            inventory_record("DAWN/業務内容2024.xlsx", "a"),
            inventory_record("DAWN/業務内容2025.xlsx", "b"),
            inventory_record("DAWN/連絡先.txt", "c"),
        ]
        eligible, counts, binding = builder.apply_document_version_policy(selected, graph)
        self.assertEqual(
            ["DAWN/業務内容2025.xlsx", "DAWN/連絡先.txt"],
            [item["relative_path"] for item in eligible],
        )
        self.assertEqual(1, counts["version_historical"])
        self.assertEqual(1, counts["version_active"])
        self.assertEqual(1, counts["version_ungrouped"])
        self.assertEqual(result["graph_sha256"], binding["graph_sha256"])

    def test_tampered_graph_cannot_record_human_decision(self) -> None:
        root, _, graph, result = self.build([
            inventory_record("DAWN/現行/業務内容2024.xlsx", "a"),
            inventory_record("DAWN/業務内容2025.xlsx", "b"),
        ])
        value = json.loads(graph.read_text(encoding="utf-8"))
        value["groups"][0]["candidates"][0]["relative_path"] = "すり替え.xlsx"
        graph.write_text(json.dumps(value), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "decision_graph_hash_mismatch"):
            resolver.record_decision(
                graph, root / "decisions.json", result["groups"][0]["group_id"],
                "DAWN/業務内容2025.xlsx", "user",
            )

    def test_reader_end_to_end_indexes_only_active_version(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        source = root / "source"
        source.mkdir()
        files = {
            "業務内容2024.csv": "task,owner\nold,alice\n",
            "業務内容2025.csv": "task,owner\ncurrent,bob\n",
            "連絡先.txt": "contact: front desk\n",
        }
        records = []
        for relative, content in files.items():
            path = source / relative
            path.write_text(content, encoding="utf-8")
            metadata = path.stat()
            records.append({
                "relative_path": relative,
                "kind": "file",
                "size_bytes": metadata.st_size,
                "mtime_ns": metadata.st_mtime_ns,
                "birthtime_ns": getattr(metadata, "st_birthtime_ns", None),
                "sha256": resolver.sha256_file(path),
                "read_status": "observed",
            })
        inventory = root / "inventory.jsonl"
        write_inventory(inventory, records)
        graph = root / "document-version-graph.json"
        resolver.build(inventory, graph)
        output = root / "semantic"
        tools = builder.default_tools_dir()
        result = builder.build(source, inventory, output, tools, graph)
        manifest = json.loads((output / "layer1-input-manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(
            ["業務内容2025.csv", "連絡先.txt"],
            sorted(manifest["paths"]),
        )
        self.assertEqual(1, result["limitations"]["historical_version_files_held"])
        self.assertEqual(
            "PASS",
            validator.validate(output, source, inventory, graph)["status"],
        )


if __name__ == "__main__":
    unittest.main()
