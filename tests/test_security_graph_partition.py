from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import sqlite3
import sys
import tempfile
import unittest
from collections import defaultdict
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

try:
    from openpyxl import Workbook
except ImportError:  # pragma: no cover - exercised only on minimal runtimes
    Workbook = None


ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / "distribution" / "macos-local-memory" / "engine"
SCRIPTS = ROOT / "scripts"
RUN_AT = "2026-09-01T00:00:00+00:00"


def load_module(name: str, path: Path):
    specification = importlib.util.spec_from_file_location(name, path)
    if specification is None or specification.loader is None:
        raise ImportError(f"cannot load {path}")
    module = importlib.util.module_from_spec(specification)
    sys.modules[name] = module
    specification.loader.exec_module(module)
    return module


adaptive_builder = load_module(
    "security_partition_adaptive_builder",
    ENGINE / "build_adaptive_semantic_graph.py",
)
semantic_validator = load_module(
    "security_partition_semantic_validator",
    ENGINE / "validate_adaptive_semantic_graph.py",
)
security_builder = load_module(
    "security_partition_gate_builder",
    ENGINE / "content_security_gate.py",
)
security_validator = load_module(
    "security_partition_gate_validator",
    ENGINE / "validate_content_security_gate.py",
)
index_builder = load_module(
    "security_partition_index_builder",
    ENGINE / "build_local_semantic_index.py",
)
answer_engine = load_module(
    "security_partition_answer_engine",
    ENGINE / "answer_local_memory.py",
)


def canonical(value: object) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )


def read_jsonl(path: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def insert_indexed_evidence(
    connection: sqlite3.Connection,
    records: list[dict],
) -> None:
    for record in records:
        observed_text = str(record["observed_text"])
        connection.execute(
            "INSERT INTO evidence VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                record["evidence_id"],
                record["document_id"],
                record["source"]["relative_path"],
                canonical(record["locator"]),
                observed_text,
                observed_text,
                0,
                hashlib.sha256(observed_text.encode("utf-8")).hexdigest(),
            ),
        )
    connection.commit()


class SecurityGraphPartitionTests(unittest.TestCase):
    @unittest.skipUnless(
        Workbook is not None,
        "openpyxl is required to create a genuine multi-cell fan-in fixture",
    )
    def test_partial_exclusion_holds_mixed_fan_in_atomically(self) -> None:
        """Only complete safe fan-ins may reach the answer graph.

        This deliberately uses prompt-library material rather than a hard
        injection.  The XLSX document therefore retains safe cells while one
        cell and its derived row are excluded.  A real CSV travels through the
        same adaptive path and supplies a dependency-free, fully safe row.

        CSV extraction intentionally represents one complete row as one
        Evidence item, so it cannot by itself create a multi-source fan-in.
        The smallest honest mixed fan-in consequently needs one native XLSX
        row whose SearchUnit cites multiple independently gated cells.
        """
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source_root = base / "source"
            semantic_dir = base / "semantic"
            security_dir = base / "security"
            source_root.mkdir()
            security_dir.mkdir()
            csv_path = source_root / "safe-row.csv"
            csv_path.write_text(
                "Item,Value\n稼働回数,13\n",
                encoding="utf-8",
            )
            workbook_path = source_root / "mixed-security.xlsx"
            workbook = Workbook()
            sheet = workbook.active
            sheet.title = "Mixed"
            sheet.append(["Item", "Value"])
            sheet.append([
                "AI設定",
                "あなたはAIアシスタントです。最初に結果を出力してください。",
            ])
            sheet.append(["確認値", "安全な値"])
            workbook.save(workbook_path)
            inventory_path = base / "path-source-inventory.jsonl"
            inventories = [
                {
                    "kind": "file",
                    "relative_path": path.name,
                    "read_status": "observed",
                    "size_bytes": path.stat().st_size,
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                }
                for path in sorted((csv_path, workbook_path))
            ]
            inventory_path.write_text(
                "".join(canonical(item) + "\n" for item in inventories),
                encoding="utf-8",
            )

            adaptive_builder.build(
                source_root, inventory_path, semantic_dir, SCRIPTS,
            )
            semantic_report = semantic_validator.validate(
                semantic_dir, source_root, inventory_path,
            )
            self.assertEqual(semantic_report["status"], "PASS")

            security_state = security_builder.build(
                semantic_dir / "semantic-evidence.jsonl",
                semantic_dir / "semantic-documents.jsonl",
                security_dir,
                created_at=RUN_AT,
            )
            security_report = security_validator.validate(
                semantic_dir / "semantic-evidence.jsonl",
                semantic_dir / "semantic-documents.jsonl",
                security_dir,
            )
            self.assertEqual(security_report["status"], "PASS")
            self.assertGreater(security_state["counts"]["safe_answer_evidence"], 0)
            self.assertGreater(security_state["counts"]["prompt_library_evidence"], 0)
            self.assertEqual(security_state["counts"]["quarantine_evidence"], 0)
            self.assertEqual(
                security_state["counts"]["partially_excluded_documents"], 1,
            )

            documents = read_jsonl(semantic_dir / "semantic-documents.jsonl")
            safe_evidence = read_jsonl(
                security_dir / "safe-answer-evidence.jsonl"
            )
            exclusions = read_jsonl(
                security_dir / "content-security-exclusions.jsonl"
            )
            structural_relations = read_jsonl(
                semantic_dir / "layer1-intermediate" / "relations.jsonl"
            )
            lineage_relations = read_jsonl(
                semantic_dir / "semantic-lineage-relations.jsonl"
            )
            all_relations = [*structural_relations, *lineage_relations]
            safe_ids = {record["evidence_id"] for record in safe_evidence}
            excluded_ids = {record["evidence_id"] for record in exclusions}
            self.assertTrue(safe_ids)
            self.assertTrue(excluded_ids)
            self.assertFalse(safe_ids & excluded_ids)

            lineage_by_derived: dict[str, list[dict]] = defaultdict(list)
            for relation in lineage_relations:
                lineage_by_derived[
                    relation["from_ref"]["record_id"]
                ].append(relation)

            mixed_fan_ins = {
                derived_id: relations
                for derived_id, relations in lineage_by_derived.items()
                if (
                    {
                        relation["to_ref"]["record_id"]
                        for relation in relations
                    }
                    & safe_ids
                )
                and (
                    {
                        relation["to_ref"]["record_id"]
                        for relation in relations
                    }
                    & excluded_ids
                )
            }
            fully_safe_fan_ins = {
                derived_id: relations
                for derived_id, relations in lineage_by_derived.items()
                if derived_id in safe_ids
                and all(
                    relation["to_ref"]["record_id"] in safe_ids
                    for relation in relations
                )
            }
            self.assertTrue(mixed_fan_ins, "fixture must contain a mixed fan-in")
            self.assertTrue(
                fully_safe_fan_ins,
                "fixture must retain a completely safe fan-in",
            )

            expected_promoted_lineage_ids = {
                relation["relation_id"]
                for relations in fully_safe_fan_ins.values()
                for relation in relations
            }
            mixed_lineage_ids = {
                relation["relation_id"]
                for relations in mixed_fan_ins.values()
                for relation in relations
            }

            connection = sqlite3.connect(":memory:")
            try:
                index_builder.initialize(connection)
                insert_indexed_evidence(connection, safe_evidence)
                graph_report = index_builder.project_verified_structural_graph(
                    connection,
                    documents,
                    safe_evidence,
                    all_relations,
                    lineage_context={
                        "output_dir": semantic_dir,
                        "source_root": source_root,
                        "inventory": inventory_path,
                    },
                    security_context={"gate_dir": security_dir},
                )
                projected_edges = connection.execute(
                    "SELECT relation_id, from_node_id, to_node_id, relation_class "
                    "FROM graph_edges"
                ).fetchall()
                projected_lineage_ids = {
                    relation_id
                    for relation_id, _from_id, _to_id, relation_class
                    in projected_edges
                    if relation_class == "lineage"
                }
                self.assertEqual(
                    projected_lineage_ids, expected_promoted_lineage_ids,
                )
                self.assertFalse(projected_lineage_ids & mixed_lineage_ids)

                projected_endpoint_ids = {
                    endpoint
                    for _relation_id, from_id, to_id, _relation_class
                    in projected_edges
                    for endpoint in (from_id, to_id)
                }
                projected_node_ids = {
                    row[0]
                    for row in connection.execute("SELECT node_id FROM graph_nodes")
                }
                self.assertFalse(projected_endpoint_ids & excluded_ids)
                self.assertFalse(projected_node_ids & excluded_ids)

                partition = graph_report["security_partition"]
                held_by_derived = {
                    item["evidence_id"]: item
                    for item in partition["held_derived_evidence"]
                }
                for derived_id, relations in mixed_fan_ins.items():
                    self.assertIn(derived_id, held_by_derived)
                    held = held_by_derived[derived_id]
                    expected_excluded_sources = {
                        relation["to_ref"]["record_id"]
                        for relation in relations
                    } & excluded_ids
                    self.assertTrue(held["reason_codes"])
                    self.assertEqual(
                        set(held["excluded_source_evidence_ids"]),
                        expected_excluded_sources,
                    )
                self.assertFalse(
                    set(fully_safe_fan_ins) & set(held_by_derived)
                )
                self.assertEqual(
                    set(partition["promoted_relation_ids"]),
                    {row[0] for row in projected_edges},
                )
                self.assertEqual(
                    connection.execute("PRAGMA foreign_key_check").fetchall(),
                    [],
                )
                self.assertEqual(
                    connection.execute("PRAGMA integrity_check").fetchone()[0],
                    "ok",
                )
            finally:
                connection.close()

            output_path = base / "safe-answer-index.sqlite3"
            argv = [
                str(ENGINE / "build_local_semantic_index.py"),
                "--evidence", str(security_dir / "safe-answer-evidence.jsonl"),
                "--documents", str(semantic_dir / "semantic-documents.jsonl"),
                "--security-state", str(
                    security_dir / "content-security-state.json"
                ),
                "--source-root", str(source_root),
                "--source-inventory", str(inventory_path),
                "--index-purpose", "safe_answer",
                "--output", str(output_path),
            ]
            with mock.patch.object(sys, "argv", argv), mock.patch.object(
                index_builder,
                "embed",
                side_effect=lambda _model, texts, _timeout: [
                    [0.25, 0.75] for _text in texts
                ],
            ), redirect_stdout(io.StringIO()):
                self.assertEqual(index_builder.main(), 0)

            indexed = sqlite3.connect(output_path)
            try:
                metadata = {
                    key: json.loads(value)
                    for key, value in indexed.execute(
                        "SELECT key, value FROM metadata"
                    )
                }
                self.assertEqual(
                    metadata["graph_status"], "validated_safe_partition"
                )
                self.assertTrue(metadata["graph_retrieval_enabled"])
                self.assertTrue(metadata["answer_generation_allowed"])
                self.assertEqual(
                    metadata["graph_security_partition_sha256"],
                    metadata["graph_security_partition"]["partition_sha256"],
                )
                self.assertEqual(
                    metadata["graph_unresolved_evidence_count"],
                    len(set(held_by_derived) & safe_ids),
                )
                self.assertEqual(
                    indexed.execute("PRAGMA integrity_check").fetchone()[0],
                    "ok",
                )
            finally:
                indexed.close()

            retrievable_records, answer_policy = (
                answer_engine.load_answer_evidence_records(output_path)
            )
            self.assertEqual(
                {item["evidence_id"] for item in retrievable_records},
                safe_ids - set(held_by_derived),
            )
            self.assertEqual(
                answer_policy["metadata"]["graph_held_derived_evidence_count"],
                len(held_by_derived),
            )
            self.assertEqual(
                answer_policy["metadata"][
                    "graph_nonindexed_held_derived_evidence_count"
                ],
                len(set(held_by_derived) - safe_ids),
            )


if __name__ == "__main__":
    unittest.main()
