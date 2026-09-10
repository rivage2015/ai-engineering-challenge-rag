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


def dated_consent(group: dict, selected_path: str, actor: str = "synthetic-human") -> dict:
    selected = next(
        item for item in group["candidates"]
        if item["relative_path"] == selected_path
    )
    return {
        "decision_schema_version": "2.0",
        "group_id": group["group_id"],
        "candidate_set_sha256": group["candidate_set_sha256"],
        "relation": "same_work_revisions",
        "selected_relative_path": selected_path,
        "selected_source_sha256": selected["source_sha256"],
        "current_applicability_confirmed": True,
        "allow_ingest_index_answer": True,
        "reviewed_revision": {
            "generation": "generation-" + "1" * 32,
            "source_scope_sha256": "2" * 64,
            "graph_sha256": "3" * 64,
            "graph_file_sha256": "4" * 64,
            "inventory_sha256": "5" * 64,
            "candidate_set_sha256": group["candidate_set_sha256"],
            "decisions_sha256": None,
            "resolver_version": resolver.RESOLVER_VERSION,
        },
        "decided_by": actor,
        "decided_at": "2026-09-09T12:00:00+00:00",
    }


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

    def test_year_order_does_not_establish_supersession(self) -> None:
        _, inventory, graph, result = self.build([
            inventory_record("DAWN/業務内容2024.xlsx", "a"),
            inventory_record("DAWN/業務内容2025.xlsx", "b"),
        ])
        self.assertEqual({"groups": 1, "resolved": 0, "needs_human_review": 1}, result["counts"])
        group = result["groups"][0]
        self.assertEqual("needs_human_review", group["status"])
        self.assertIsNone(group["selected_relative_path"])
        self.assertIsNone(group["resolution_basis"])
        self.assertEqual("year_order_does_not_establish_supersession", group["reason_code"])
        self.assertEqual(
            {"DAWN/業務内容2024.xlsx", "DAWN/業務内容2025.xlsx"},
            set(group["conflicts"]),
        )
        self.assertEqual(
            ["needs_human_review", "needs_human_review"],
            [item["disposition"] for item in group["candidates"]],
        )
        self.assertFalse(any(edge["edge_type"] == "active_version" for edge in result["edges"]))
        self.assertEqual("0.1.6", result["resolver_version"])
        self.assertIs(False, result["policy"]["year_order_establishes_supersession"])
        self.assertEqual("PASS", resolver.validate(graph, inventory)["status"])

    def test_year_directories_and_lifecycle_directories_share_a_family(self) -> None:
        _, _, _, result = self.build([
            inventory_record("DAWN/旧資料/2024年/業務内容2024.xlsx", "a"),
            inventory_record("DAWN/実際に使うもの/2025年/業務内容2025.xlsx", "b"),
        ])
        self.assertEqual(1, result["counts"]["groups"])
        self.assertEqual("needs_human_review", result["groups"][0]["status"])
        self.assertEqual(
            "dated_family_requires_human_review",
            result["groups"][0]["reason_code"],
        )

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

    def test_latest_historical_requires_human_review(self) -> None:
        _, _, _, result = self.build([
            inventory_record("DAWN/業務内容2024.xlsx", "a"),
            inventory_record("DAWN/旧資料/業務内容2025.xlsx", "b"),
        ])
        group = result["groups"][0]
        self.assertEqual("needs_human_review", group["status"])
        self.assertEqual("latest_year_marked_historical", group["reason_code"])
        self.assertIsNone(group["selected_relative_path"])
        self.assertFalse(any(edge["edge_type"] == "active_version" for edge in result["edges"]))

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

    def test_current_marker_conflicts_with_higher_numeric_version(self) -> None:
        for current, other in (
            ("現行/業務_ver1.xlsx", "業務_ver2.xlsx"),
            ("現行/業務_ver1.2.xlsx", "業務_ver1.10.xlsx"),
            ("現行/業務_ver1.2.xlsx", "業務_ver1.10_下書き.xlsx"),
            ("現行/業務_ver1.2.xlsx", "旧版/業務_ver1.10.xlsx"),
        ):
            with self.subTest(current=current, other=other):
                records = [inventory_record(current, "a"), inventory_record(other, "b")]
                contact = inventory_record("連絡先.txt", "c")
                _, inventory, graph, result = self.build([*records, contact])
                group = result["groups"][0]
                self.assertEqual("needs_human_review", group["status"])
                self.assertIsNone(group["selected_relative_path"])
                self.assertIsNone(group["resolution_basis"])
                self.assertEqual("current_marker_conflicts_with_latest_version", group["reason_code"])
                self.assertEqual({current, other}, set(group["conflicts"]))
                self.assertTrue(all(item["disposition"] == "needs_human_review" for item in group["candidates"]))
                self.assertFalse(any(edge["edge_type"] == "active_version" for edge in result["edges"]))
                selection = builder.select_attested_inventory(inventory, graph, version_authority_mode="no_decisions")
                eligible, counts = selection["selected"], selection["selection_counts"]
                self.assertEqual([contact], eligible)
                self.assertEqual(2, counts["version_needs_human_review"])

    def test_current_marker_newest_or_equal_version_remains_selected(self) -> None:
        for other_version in ("1.2", "1.10"):
            with self.subTest(other_version=other_version):
                _, _, _, result = self.build([
                    inventory_record("現行/業務_ver1.10.xlsx", "a"),
                    inventory_record(f"業務_ver{other_version}.xlsx", "b"),
                ])
                group = result["groups"][0]
                self.assertEqual("resolved", group["status"])
                self.assertEqual("現行/業務_ver1.10.xlsx", group["selected_relative_path"])
                self.assertEqual("unique_explicit_current_marker", group["reason_code"])

    def test_current_version_conflict_is_invariant_to_candidate_order(self) -> None:
        records = [
            inventory_record("現行/業務_ver1.2.xlsx", "a"),
            inventory_record("業務_ver1.10.xlsx", "b"),
        ]
        _, _, _, forward = self.build(records)
        _, _, _, reverse = self.build(list(reversed(records)))
        self.assertEqual("needs_human_review", forward["groups"][0]["status"])
        self.assertEqual(forward["groups"], reverse["groups"])
        self.assertEqual(forward["nodes"], reverse["nodes"])
        self.assertEqual(forward["edges"], reverse["edges"])

    def test_multiple_current_markers_still_require_review(self) -> None:
        _, _, _, result = self.build([
            inventory_record("現行/業務_ver1.xlsx", "a"),
            inventory_record("承認済み/業務_ver2.xlsx", "b"),
        ])
        group = result["groups"][0]
        self.assertEqual("needs_human_review", group["status"])
        self.assertIsNone(group["selected_relative_path"])
        self.assertEqual("multiple_current_markers", group["reason_code"])

    def test_human_version_choice_precedes_conflict_and_stale_choice_stays_held(self) -> None:
        for changed_index in (0, 1):
            with self.subTest(changed_index=changed_index):
                records = [
                    inventory_record("現行/業務_ver1.xlsx", "a"),
                    inventory_record("業務_ver2.xlsx", "b"),
                ]
                root, inventory, graph, initial = self.build(records)
                self.assertEqual("needs_human_review", initial["groups"][0]["status"])
                decisions = root / "decisions.json"
                resolver.record_decision(
                    graph, decisions, initial["groups"][0]["group_id"],
                    "現行/業務_ver1.xlsx", "synthetic-human",
                )
                chosen = resolver.build(inventory, root / "chosen.json", decisions)["groups"][0]
                self.assertEqual("resolved", chosen["status"])
                self.assertEqual("human", chosen["resolution_basis"])
                self.assertEqual("現行/業務_ver1.xlsx", chosen["selected_relative_path"])
                records[changed_index]["sha256"] = "c" * 64
                write_inventory(inventory, records)
                stale = resolver.build(inventory, root / "stale.json", decisions)["groups"][0]
                self.assertEqual("needs_human_review", stale["status"])
                self.assertEqual("stale_human_decision", stale["reason_code"])
                self.assertIsNone(stale["selected_relative_path"])
                self.assertTrue(all(item["disposition"] == "needs_human_review" for item in stale["candidates"]))

    def test_human_decision_is_hash_bound_and_reused(self) -> None:
        root, inventory, graph, initial = self.build([
            inventory_record("DAWN/現行/業務内容2024.xlsx", "a"),
            inventory_record("DAWN/業務内容2025.xlsx", "b"),
        ])
        decisions = root / "decisions.json"
        group = initial["groups"][0]
        resolver.record_dated_consent_cas(
            decisions, dated_consent(group, "DAWN/業務内容2025.xlsx", "user"), None
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
            inventory_record("DAWN/業務内容_ver1.xlsx", "a"),
            inventory_record("DAWN/業務内容_ver2.xlsx", "b"),
        ])
        value = json.loads(graph.read_text(encoding="utf-8"))
        value["groups"][0]["status"] = "needs_human_review"
        graph.write_text(json.dumps(value), encoding="utf-8")
        report = resolver.validate(graph, inventory)
        self.assertEqual("FAIL", report["status"])
        self.assertIn("graph_hash_mismatch", report["errors"])

    def test_answer_policy_keeps_active_and_holds_other_candidates(self) -> None:
        selected = [
            inventory_record("DAWN/業務内容_ver1.xlsx", "a"),
            inventory_record("DAWN/業務内容_ver2.xlsx", "b"),
            inventory_record("DAWN/連絡先.txt", "c"),
        ]
        _, inventory, graph, result = self.build(selected)
        selection = builder.select_attested_inventory(inventory, graph, version_authority_mode="no_decisions")
        eligible, counts, binding = selection["selected"], selection["selection_counts"], selection["document_version_graph"]
        self.assertEqual(
            ["DAWN/業務内容_ver2.xlsx", "DAWN/連絡先.txt"],
            [item["relative_path"] for item in eligible],
        )
        self.assertEqual(1, counts["version_historical"])
        self.assertEqual(1, counts["version_active"])
        self.assertEqual(1, counts["version_ungrouped"])
        self.assertEqual(result["graph_sha256"], binding["graph_sha256"])

    def test_answer_policy_holds_year_candidates_and_keeps_unrelated_source(self) -> None:
        records = [
            inventory_record("実績2024.csv", "a"),
            inventory_record("実績2025.csv", "b"),
        ]
        contact = inventory_record("連絡先.txt", "c")
        _, inventory, graph, result = self.build([*records, contact])
        selection = builder.select_attested_inventory(inventory, graph, version_authority_mode="no_decisions")
        eligible, counts, binding = selection["selected"], selection["selection_counts"], selection["document_version_graph"]
        self.assertEqual([contact], eligible)
        self.assertEqual(2, counts["version_needs_human_review"])
        self.assertEqual(0, counts.get("version_historical", 0))
        self.assertEqual(0, counts.get("version_active", 0))
        self.assertEqual(1, counts["version_ungrouped"])
        self.assertEqual(result["graph_sha256"], binding["graph_sha256"])
        self.assertFalse(any(edge["edge_type"] == "active_version" for edge in result["edges"]))

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
            "業務内容_ver1.csv": "task,owner\nold,alice\n",
            "業務内容_ver2.csv": "task,owner\ncurrent,bob\n",
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
        result = builder.build(source, inventory, output, tools, graph, version_authority_mode="no_decisions")
        manifest = json.loads((output / "layer1-input-manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(
            ["業務内容_ver2.csv", "連絡先.txt"],
            sorted(manifest["paths"]),
        )
        self.assertEqual(1, result["limitations"]["historical_version_files_held"])
        self.assertEqual(
            "PASS",
            validator.validate(
                output, source, inventory, graph, initialize_lineage=True, version_authority_mode="no_decisions",
            )["status"],
        )

    def test_reader_holds_year_candidates_and_keeps_unrelated_source(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        source = root / "source"
        source.mkdir()
        files = {
            "実績2024.csv": "period,value\n2024,10\n",
            "実績2025.csv": "period,value\n2025,20\n",
            "連絡先.txt": "contact: front desk\n",
        }
        self.assertLessEqual(sum(len(text.encode("utf-8")) for text in files.values()), 1048576)
        records = []
        original_hashes = {}
        for relative, content in files.items():
            path = source / relative
            path.write_text(content, encoding="utf-8")
            metadata = path.stat()
            original_hashes[relative] = resolver.sha256_file(path)
            records.append({
                "relative_path": relative, "kind": "file", "size_bytes": metadata.st_size,
                "mtime_ns": metadata.st_mtime_ns,
                "birthtime_ns": getattr(metadata, "st_birthtime_ns", None),
                "sha256": original_hashes[relative], "read_status": "observed",
            })
        inventory = root / "inventory.jsonl"
        write_inventory(inventory, records)
        graph = root / "document-version-graph.json"
        version = resolver.build(inventory, graph)
        self.assertEqual("needs_human_review", version["groups"][0]["status"])
        self.assertFalse(any(edge["edge_type"] == "active_version" for edge in version["edges"]))
        output = root / "semantic"
        result = builder.build(source, inventory, output, builder.default_tools_dir(), graph, version_authority_mode="no_decisions")
        manifest = json.loads((output / "layer1-input-manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(["連絡先.txt"], manifest["paths"])
        self.assertEqual(0, result["limitations"]["historical_version_files_held"])
        self.assertEqual(2, result["limitations"]["version_files_needing_human_review"])
        self.assertEqual("PASS", validator.validate(
            output, source, inventory, graph, initialize_lineage=True, version_authority_mode="no_decisions",
        )["status"])
        self.assertEqual(original_hashes, {relative: resolver.sha256_file(source / relative) for relative in files})

    def test_reader_holds_current_version_conflict_and_keeps_unrelated_source(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        source = root / "source"
        (source / "現行").mkdir(parents=True)
        files = {
            "現行/業務_ver1.csv": "task,owner\nold,alice\n",
            "業務_ver2.csv": "task,owner\nnew,bob\n",
            "連絡先.txt": "contact: front desk\n",
        }
        records = []
        original_hashes = {}
        for relative, content in files.items():
            path = source / relative
            path.write_text(content, encoding="utf-8")
            metadata = path.stat()
            original_hashes[relative] = resolver.sha256_file(path)
            records.append({
                "relative_path": relative, "kind": "file",
                "size_bytes": metadata.st_size, "mtime_ns": metadata.st_mtime_ns,
                "birthtime_ns": getattr(metadata, "st_birthtime_ns", None),
                "sha256": original_hashes[relative], "read_status": "observed",
            })
        inventory = root / "inventory.jsonl"
        write_inventory(inventory, records)
        graph = root / "document-version-graph.json"
        resolver.build(inventory, graph)
        output = root / "semantic"
        result = builder.build(source, inventory, output, builder.default_tools_dir(), graph, version_authority_mode="no_decisions")
        manifest = json.loads((output / "layer1-input-manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(["連絡先.txt"], manifest["paths"])
        self.assertEqual(2, result["limitations"]["version_files_needing_human_review"])
        self.assertEqual("PASS", validator.validate(
            output, source, inventory, graph, initialize_lineage=True, version_authority_mode="no_decisions",
        )["status"])
        self.assertEqual(original_hashes, {relative: resolver.sha256_file(source / relative) for relative in files})


if __name__ == "__main__":
    unittest.main()
