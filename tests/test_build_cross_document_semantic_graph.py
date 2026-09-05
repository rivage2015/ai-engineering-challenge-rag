from __future__ import annotations

import hashlib
import importlib.util
import inspect
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from typing import Any, Iterable


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
BUILDER_PATH = REPOSITORY_ROOT / "scripts" / "build_cross_document_semantic_graph.py"
DEFAULT_ADAPTER_ROOT = Path(
    "/private/tmp/cross-format-kg-v0.1-baseline/layer1-adapter"
)
ADAPTER_ROOT = Path(os.environ.get("CROSS_FORMAT_KG_LAYER1_ADAPTER_DIR", DEFAULT_ADAPTER_ROOT))
EXPECTED_GRAPH = (
    REPOSITORY_ROOT
    / "evaluation"
    / "cross-format-kg-v0.1"
    / "gold"
    / "expected-graph.jsonl"
)


def _load_builder() -> Any:
    spec = importlib.util.spec_from_file_location(
        "test_target_build_cross_document_semantic_graph", BUILDER_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


builder = _load_builder()


def _jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _write_jsonl(path: Path, records: Iterable[dict[str, Any]]) -> None:
    path.write_text(
        "".join(
            json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
            for record in records
        ),
        encoding="utf-8",
    )


def _metadata(connection: sqlite3.Connection) -> dict[str, Any]:
    return {
        key: json.loads(value)
        for key, value in connection.execute("SELECT key, value FROM metadata")
    }


def _edge_tuples(connection: sqlite3.Connection) -> set[tuple[Any, ...]]:
    return {
        (
            from_type,
            from_key,
            relation_type,
            to_type,
            to_key,
            relation_class,
            status,
            basis_kind,
            properties_json,
        )
        for (
            from_type,
            from_key,
            relation_type,
            to_type,
            to_key,
            relation_class,
            status,
            basis_kind,
            properties_json,
        ) in connection.execute(
            """
            SELECT source.node_type, source.canonical_key, edge.relation_type,
                   target.node_type, target.canonical_key, edge.relation_class,
                   edge.status, edge.basis_kind, edge.properties_json
            FROM edges AS edge
            JOIN nodes AS source ON source.node_id = edge.from_node_id
            JOIN nodes AS target ON target.node_id = edge.to_node_id
            """
        )
    }


def _evidence_view(
    evidence_id: str,
    text: str,
    *,
    page_number: int,
    ordinal: int,
    x: float | None = None,
    y: float | None = None,
    coordinate_space: str = "page",
    unit: str = "pt",
    quality_disposition: str = "eligible_native",
) -> Any:
    geometry = None
    if x is not None and y is not None:
        geometry = {
            "coordinate_space": coordinate_space,
            "coordinate_origin": "top_left",
            "unit": unit,
            "x": x,
            "y": y,
            "width": 80.0,
            "height": 10.0,
        }
    return builder.EvidenceView(
        evidence_id=evidence_id,
        document_id="doc_pdf",
        relative_path="sample/register.pdf",
        source_sha256="a" * 64,
        evidence_type="text_block",
        location={"page_number": page_number},
        observed_text=text,
        observed_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        text=text,
        ordinal=ordinal,
        geometry=geometry,
        quality_disposition=quality_disposition,
    )


class CrossDocumentSemanticGraphBuilderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.documents = ADAPTER_ROOT / "semantic-documents.jsonl"
        cls.evidence = ADAPTER_ROOT / "safe-answer-evidence.jsonl"

    def _require_real_inputs(self) -> None:
        if not self.documents.is_file() or not self.evidence.is_file():
            self.skipTest(
                "real Layer 1 adapter outputs are unavailable; set "
                "CROSS_FORMAT_KG_LAYER1_ADAPTER_DIR"
            )

    def _build(self, directory: Path, documents: Path | None = None, evidence: Path | None = None):
        output = directory / "graph.sqlite3"
        state_path = directory / "state.json"
        state = builder.build(
            documents or self.documents,
            evidence or self.evidence,
            output,
            state_path,
        )
        return state, output, state_path

    def test_real_safe_adapter_outputs_contain_all_fourteen_gold_tuples_and_hashes(self) -> None:
        self._require_real_inputs()
        with tempfile.TemporaryDirectory() as raw_directory:
            state, database_path, _ = self._build(Path(raw_directory))
            with closing(sqlite3.connect(database_path)) as connection:
                actual = _edge_tuples(connection)

                # The builder has already completed before the evaluator opens gold.
                expected_records = _jsonl(EXPECTED_GRAPH)
                expected = {
                    (
                        record["from"]["node_type"],
                        record["from"]["canonical_key"],
                        record["relation_type"],
                        record["to"]["node_type"],
                        record["to"]["canonical_key"],
                        record["relation_class"],
                        record["expected_status"],
                        record["basis_kind"],
                        builder.canonical_json(record["properties"]),
                    )
                    for record in expected_records
                }
                self.assertEqual(14, len(expected))
                self.assertEqual(set(), expected - actual)
                self.assertEqual(
                    {"verified"},
                    {row[0] for row in connection.execute("SELECT DISTINCT status FROM nodes")},
                )
                self.assertEqual(
                    {"verified"},
                    {row[0] for row in connection.execute("SELECT DISTINCT status FROM edges")},
                )

                evidence_hashes: list[str] = []
                for row in connection.execute(
                    """
                    SELECT evidence_id, document_id, relative_path, source_sha256,
                           locator_json, observed_text, observed_sha256, record_sha256
                    FROM source_evidence
                    """
                ):
                    (
                        evidence_id,
                        document_id,
                        relative_path,
                        source_sha256,
                        locator_json,
                        observed_text,
                        observed_sha256,
                        record_sha256,
                    ) = row
                    self.assertEqual(
                        hashlib.sha256(observed_text.encode("utf-8")).hexdigest(),
                        observed_sha256,
                    )
                    payload = {
                        "evidence_id": evidence_id,
                        "document_id": document_id,
                        "relative_path": relative_path,
                        "source_sha256": source_sha256,
                        "locator": json.loads(locator_json),
                        "observed_text": observed_text,
                        "observed_sha256": observed_sha256,
                    }
                    self.assertEqual(builder.sha256_json(payload), record_sha256)
                    evidence_hashes.append(record_sha256)

                node_hashes: list[str] = []
                for row in connection.execute(
                    """
                    SELECT node_id, node_type, canonical_key, status,
                           properties_json, record_sha256
                    FROM nodes
                    """
                ):
                    node_id, node_type, canonical_key, status_value, properties_json, record_hash = row
                    payload = {
                        "node_id": node_id,
                        "node_type": node_type,
                        "canonical_key": canonical_key,
                        "status": status_value,
                        "properties": json.loads(properties_json),
                    }
                    self.assertEqual(builder.sha256_json(payload), record_hash)
                    node_hashes.append(record_hash)

                edge_hashes: list[str] = []
                for row in connection.execute(
                    """
                    SELECT edge_id, from_node_id, relation_type, to_node_id,
                           relation_class, status, basis_kind, basis_rule,
                           properties_json, record_sha256
                    FROM edges
                    """
                ):
                    (
                        edge_id,
                        from_node_id,
                        relation_type,
                        to_node_id,
                        relation_class,
                        status_value,
                        basis_kind,
                        basis_rule,
                        properties_json,
                        record_hash,
                    ) = row
                    supporting = [
                        item[0]
                        for item in connection.execute(
                            "SELECT evidence_id FROM edge_evidence WHERE edge_id = ? ORDER BY evidence_id",
                            (edge_id,),
                        )
                    ]
                    identity = {
                        "from_node_id": from_node_id,
                        "relation_type": relation_type,
                        "to_node_id": to_node_id,
                        "relation_class": relation_class,
                        "status": status_value,
                        "basis_kind": basis_kind,
                        "basis_rule": basis_rule,
                        "properties": json.loads(properties_json),
                        "supporting_evidence_ids": supporting,
                    }
                    self.assertEqual(
                        "edge_" + builder.sha256_json(identity)[:32],
                        edge_id,
                    )
                    payload = {"edge_id": edge_id, **identity}
                    self.assertEqual(builder.sha256_json(payload), record_hash)
                    edge_hashes.append(record_hash)

                logical_payload = {
                    "evidence_record_sha256": sorted(evidence_hashes),
                    "node_record_sha256": sorted(node_hashes),
                    "edge_record_sha256": sorted(edge_hashes),
                }
                logical_hash = builder.sha256_json(logical_payload)
                metadata = _metadata(connection)
                self.assertEqual(logical_hash, metadata["logical_snapshot_sha256"])
                self.assertEqual("xkgs_" + logical_hash[:32], metadata["graph_snapshot_id"])
                self.assertEqual(metadata["graph_snapshot_id"], state["graph_snapshot_id"])

    def test_source_rename_and_observed_value_change_are_reflected_without_rules_changes(self) -> None:
        self._require_real_inputs()
        with tempfile.TemporaryDirectory() as raw_directory:
            root = Path(raw_directory)
            baseline_root = root / "baseline"
            baseline_root.mkdir()
            baseline_state, baseline_database, _ = self._build(baseline_root)
            with closing(sqlite3.connect(baseline_database)) as connection:
                employee_key, person_key, document_id = connection.execute(
                    """
                    SELECT source.canonical_key, target.canonical_key, evidence.document_id
                    FROM edges AS edge
                    JOIN nodes AS source ON source.node_id = edge.from_node_id
                    JOIN nodes AS target ON target.node_id = edge.to_node_id
                    JOIN edge_evidence AS support ON support.edge_id = edge.edge_id
                    JOIN source_evidence AS evidence ON evidence.evidence_id = support.evidence_id
                    WHERE edge.relation_type = 'IDENTIFIES_PERSON'
                    ORDER BY source.canonical_key, evidence.evidence_id
                    LIMIT 1
                    """
                ).fetchone()

            changed_root = root / "changed"
            changed_root.mkdir()
            documents_path = changed_root / "semantic-documents.jsonl"
            evidence_path = changed_root / "safe-answer-evidence.jsonl"
            document_records = _jsonl(self.documents)
            evidence_records = _jsonl(self.evidence)
            selected_document = next(
                record for record in document_records if record["document_id"] == document_id
            )
            old_relative_path = selected_document["source"]["relative_path"]
            new_relative_path = "renamed-source/identity-register.pdf"
            new_person_key = person_key + "（更新）"
            new_source_hash = hashlib.sha256(
                (selected_document["source"]["sha256"] + new_relative_path + new_person_key).encode("utf-8")
            ).hexdigest()
            selected_document["source"]["relative_path"] = new_relative_path
            selected_document["source"]["sha256"] = new_source_hash
            selected_document["source"].pop("absolute_path", None)
            changed_values = 0
            for record in evidence_records:
                if record["document_id"] != document_id:
                    continue
                record["source"]["relative_path"] = new_relative_path
                record["source"]["sha256"] = new_source_hash
                if person_key in record["observed_text"]:
                    record["observed_text"] = record["observed_text"].replace(
                        person_key, new_person_key
                    )
                    changed_values += 1
            self.assertGreaterEqual(changed_values, 1)
            self.assertNotEqual(old_relative_path, new_relative_path)
            _write_jsonl(documents_path, document_records)
            _write_jsonl(evidence_path, evidence_records)

            changed_state, changed_database, _ = self._build(
                changed_root, documents_path, evidence_path
            )
            with closing(sqlite3.connect(changed_database)) as connection:
                targets = {
                    row[0]
                    for row in connection.execute(
                        """
                        SELECT target.canonical_key
                        FROM edges AS edge
                        JOIN nodes AS source ON source.node_id = edge.from_node_id
                        JOIN nodes AS target ON target.node_id = edge.to_node_id
                        WHERE edge.relation_type = 'IDENTIFIES_PERSON'
                          AND source.canonical_key = ?
                        """,
                        (employee_key,),
                    )
                }
                self.assertIn(new_person_key, targets)
                self.assertNotIn(person_key, targets)
                paths = {
                    row[0]
                    for row in connection.execute(
                        "SELECT DISTINCT relative_path FROM source_evidence WHERE document_id = ?",
                        (document_id,),
                    )
                }
                self.assertEqual({new_relative_path}, paths)
            self.assertNotEqual(
                baseline_state["graph_snapshot_id"], changed_state["graph_snapshot_id"]
            )

    def test_pdf_identity_table_uses_order_fallback_when_geometry_is_unavailable(self) -> None:
        self._require_real_inputs()
        with tempfile.TemporaryDirectory() as raw_directory:
            root = Path(raw_directory)
            coordinate_root = root / "coordinate"
            coordinate_root.mkdir()
            coordinate_state, coordinate_database, _ = self._build(coordinate_root)
            with closing(sqlite3.connect(coordinate_database)) as connection:
                coordinate_identities = {
                    row[:2]
                    for row in connection.execute(
                        """
                        SELECT source.canonical_key, target.canonical_key
                        FROM edges AS edge
                        JOIN nodes AS source ON source.node_id = edge.from_node_id
                        JOIN nodes AS target ON target.node_id = edge.to_node_id
                        WHERE edge.relation_type = 'IDENTIFIES_PERSON'
                        """
                    )
                }

            fallback_root = root / "fallback"
            fallback_root.mkdir()
            documents_path = fallback_root / "semantic-documents.jsonl"
            evidence_path = fallback_root / "safe-answer-evidence.jsonl"
            document_records = _jsonl(self.documents)
            pdf_document_ids = {
                record["document_id"]
                for record in document_records
                if record["source"].get("file_type") == "pdf"
                or record["source"]["relative_path"].casefold().endswith(".pdf")
            }
            self.assertTrue(pdf_document_ids)
            evidence_records = _jsonl(self.evidence)
            removed = 0
            for record in evidence_records:
                if record["document_id"] in pdf_document_ids and "geometry" in record:
                    del record["geometry"]
                    removed += 1
            self.assertGreater(removed, 0)
            _write_jsonl(documents_path, document_records)
            _write_jsonl(evidence_path, evidence_records)
            fallback_state, fallback_database, _ = self._build(
                fallback_root, documents_path, evidence_path
            )
            with closing(sqlite3.connect(fallback_database)) as connection:
                fallback_identities = {
                    row[:2]
                    for row in connection.execute(
                        """
                        SELECT source.canonical_key, target.canonical_key
                        FROM edges AS edge
                        JOIN nodes AS source ON source.node_id = edge.from_node_id
                        JOIN nodes AS target ON target.node_id = edge.to_node_id
                        WHERE edge.relation_type = 'IDENTIFIES_PERSON'
                        """
                    )
                }
            self.assertEqual(coordinate_identities, fallback_identities)
            self.assertEqual(2, coordinate_state["counts"]["pdf_coordinate_rows"])
            self.assertEqual(2, fallback_state["counts"]["pdf_order_fallback"])

    def test_quality_gate_excludes_provisional_marker_and_invalid_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            root = Path(raw_directory)
            documents_path = root / "semantic-documents.jsonl"
            evidence_path = root / "safe-answer-evidence.jsonl"
            source = {
                "relative_path": "sample/quality.txt",
                "sha256": "a" * 64,
                "extension": "txt",
            }
            specifications = [
                (
                    "native",
                    "text_block",
                    "native_parser",
                    "別表記: SAFE_NATIVE は Project ID: P-NATIVEの別表記",
                    {},
                ),
                (
                    "high",
                    "ocr_line",
                    "dual_local_ocr_consensus",
                    "別表記: SAFE_HIGH は Project ID: P-HIGHの別表記",
                    {"quality_tier": "high"},
                ),
                (
                    "provisional",
                    "text_block",
                    "local_vlm_unlocated_transcript_provisional",
                    "[暫定読取] 別表記: BAD_PROVISIONAL は Project ID: P-BAD-PROVISIONALの別表記",
                    {
                        "quality_tier": "provisional",
                        "provisional_marker": "[暫定読取]",
                    },
                ),
                (
                    "marker",
                    "paragraph",
                    "native_parser",
                    "別表記: BAD_MARKER [暫定読取] は Project ID: P-BAD-MARKERの別表記",
                    {},
                ),
                (
                    "invalid",
                    "paragraph",
                    "native_parser",
                    "別表記: BAD_INVALID は Project ID: P-BAD-INVALIDの別表記",
                    {"quality_tier": "trusted"},
                ),
                (
                    "unknown_visual",
                    "text_block",
                    "local_vlm_unknown",
                    "別表記: BAD_UNKNOWN は Project ID: P-BAD-UNKNOWNの別表記",
                    {},
                ),
                (
                    "promoted_visual",
                    "text_block",
                    "local_vlm_unlocated_transcript_provisional",
                    "別表記: BAD_PROMOTED は Project ID: P-BAD-PROMOTEDの別表記",
                    {"quality_tier": "high"},
                ),
                (
                    "native_high",
                    "paragraph",
                    "native_parser",
                    "別表記: BAD_NATIVE_HIGH は Project ID: P-BAD-NATIVE-HIGHの別表記",
                    {"quality_tier": "high"},
                ),
            ]
            evidence_records = []
            for ordinal, (
                suffix,
                source_record_type,
                extraction_method,
                observed_text,
                quality,
            ) in enumerate(specifications, 1):
                evidence_records.append({
                    "evidence_id": f"ev_{suffix}",
                    "document_id": "doc_quality",
                    "source": source,
                    "locator": {"paragraph_index": ordinal},
                    "observed_text": observed_text,
                    "ordinal": ordinal,
                    "adapter": {
                        "execution_policy": "never_execute",
                        "source_record_type": source_record_type,
                    },
                    "status": "observed",
                    "extraction_method": extraction_method,
                    **quality,
                })
            _write_jsonl(
                documents_path,
                [{
                    "document_id": "doc_quality",
                    "source": source,
                    "evidence_ids": [
                        record["evidence_id"] for record in evidence_records
                    ],
                    "status": "extracted",
                }],
            )
            _write_jsonl(evidence_path, evidence_records)

            state, database_path, _ = self._build(
                root, documents_path, evidence_path
            )
            with closing(sqlite3.connect(database_path)) as connection:
                aliases = {
                    row[0]
                    for row in connection.execute(
                        """
                        SELECT target.canonical_key
                        FROM edges AS edge
                        JOIN nodes AS target ON target.node_id = edge.to_node_id
                        WHERE edge.relation_type = 'HAS_ALIAS'
                        """
                    )
                }
                self.assertEqual({"SAFE_NATIVE", "SAFE_HIGH"}, aliases)
                self.assertEqual(
                    len(evidence_records),
                    connection.execute(
                        "SELECT COUNT(*) FROM source_evidence"
                    ).fetchone()[0],
                )
                supporting_ids = {
                    row[0]
                    for row in connection.execute(
                        "SELECT DISTINCT evidence_id FROM edge_evidence"
                    )
                }
                self.assertEqual({"ev_native", "ev_high"}, supporting_ids)

            self.assertEqual(1, state["counts"]["quality_gate_eligible_native"])
            self.assertEqual(1, state["counts"]["quality_gate_eligible_high"])
            self.assertEqual(1, state["counts"]["quality_gate_excluded_provisional"])
            self.assertEqual(1, state["counts"]["quality_gate_excluded_marker"])
            self.assertEqual(
                4,
                state["counts"]["quality_gate_excluded_invalid_quality"],
            )
            self.assertEqual(6, state["counts"]["quality_gate_excluded_total"])

    def test_pdf_coordinate_rows_never_cross_page_or_coordinate_frame(self) -> None:
        valid = [
            _evidence_view("ev_header_id", "社員ID", page_number=1, ordinal=1, x=10, y=10),
            _evidence_view("ev_header_name", "氏名", page_number=1, ordinal=2, x=110, y=10),
            _evidence_view("ev_value_id", "EMP-001", page_number=1, ordinal=3, x=10, y=30),
            _evidence_view("ev_value_name", "山田 太郎", page_number=1, ordinal=4, x=110, y=30),
        ]
        rows = builder._coordinate_identity_rows(valid)
        self.assertEqual(1, len(rows))
        self.assertEqual("EMP-001", rows[0]["employee_id"].value)
        self.assertEqual("山田 太郎", rows[0]["person_name"].value)

        crossed_page = [
            valid[0],
            valid[1],
            _evidence_view("ev_page2_id", "EMP-002", page_number=2, ordinal=3, x=10, y=30),
            _evidence_view("ev_page2_name", "佐藤 花子", page_number=2, ordinal=4, x=110, y=30),
        ]
        self.assertEqual([], builder._coordinate_identity_rows(crossed_page))

        for field, value in (("coordinate_space", "image"), ("unit", "px")):
            changed = []
            for item in valid:
                if item.evidence_id.startswith("ev_value"):
                    kwargs = {
                        "coordinate_space": "page",
                        "unit": "pt",
                    }
                    kwargs[field] = value
                    changed.append(_evidence_view(
                        item.evidence_id + "_changed",
                        item.text,
                        page_number=1,
                        ordinal=item.ordinal,
                        x=float(item.geometry["x"]),
                        y=float(item.geometry["y"]),
                        **kwargs,
                    ))
                else:
                    changed.append(item)
            with self.subTest(field=field):
                self.assertEqual([], builder._coordinate_identity_rows(changed))

    def test_pdf_order_fallback_never_crosses_pages(self) -> None:
        headers = [
            _evidence_view("ev_order_header_id", "社員ID", page_number=1, ordinal=1),
            _evidence_view("ev_order_header_name", "氏名", page_number=1, ordinal=2),
        ]
        values = [
            _evidence_view("ev_order_value_id", "EMP-003", page_number=2, ordinal=3),
            _evidence_view("ev_order_value_name", "高橋 次郎", page_number=2, ordinal=4),
        ]
        self.assertEqual([], builder._ordered_identity_rows([*headers, *values]))

        same_page_values = [
            _evidence_view("ev_order_page1_id", "EMP-003", page_number=1, ordinal=3),
            _evidence_view("ev_order_page1_name", "高橋 次郎", page_number=1, ordinal=4),
        ]
        rows = builder._ordered_identity_rows([*headers, *same_page_values])
        self.assertEqual(1, len(rows))
        self.assertEqual("EMP-003", rows[0]["employee_id"].value)
        self.assertEqual("高橋 次郎", rows[0]["person_name"].value)

    def test_pdfkit_whitespace_identity_rows_bind_complete_status_suffix(self) -> None:
        inactive = _evidence_view(
            "ev_pdfkit_inactive",
            "EmployeeID Name Status\nE1  Alice Smith  Not Approved",
            page_number=1,
            ordinal=1,
        )
        rows = builder._ordered_identity_rows([inactive])
        self.assertEqual(1, len(rows))
        self.assertEqual("Alice Smith", rows[0]["person_name"].value)
        self.assertEqual("Not Approved", rows[0]["status"].value)

        facts = builder.DocumentFacts(
            document_id="doc_pdf",
            relative_path="sample/register.pdf",
            extension="pdf",
        )
        facts.add("document_status", "Approved", ["ev_status"])
        graph = builder.GraphAccumulator()
        builder._add_employee_identities(
            graph, {"doc_pdf": facts}, [inactive], {}
        )
        self.assertFalse(any(
            edge["relation_type"] == "IDENTIFIES_PERSON"
            for edge in graph.edges.values()
        ))

        active = _evidence_view(
            "ev_pdfkit_active",
            "EmployeeID Name Status\nE2\tAlice Smith\tApproved",
            page_number=1,
            ordinal=1,
        )
        active_rows = builder._ordered_identity_rows([active])
        self.assertEqual(1, len(active_rows))
        self.assertEqual("Alice Smith", active_rows[0]["person_name"].value)
        self.assertEqual("Approved", active_rows[0]["status"].value)

        single_token_columns = _evidence_view(
            "ev_pdfkit_role",
            "EmployeeID Name Role Status\nE7  Alice  Lead  Not Approved",
            page_number=1,
            ordinal=1,
        )
        role_rows = builder._ordered_identity_rows([single_token_columns])
        self.assertEqual(1, len(role_rows))
        self.assertEqual("Alice", role_rows[0]["person_name"].value)
        self.assertEqual("Lead", role_rows[0]["role"].value)
        self.assertEqual("Not Approved", role_rows[0]["status"].value)

    def test_pdfkit_whitespace_identity_rows_hold_ambiguous_free_text(self) -> None:
        cases = {
            "unknown_multiword_status": (
                "EmployeeID Name Status\nE3 Alice Smith Pending Review"
            ),
            "multiword_role_and_name": (
                "EmployeeID Name Role Status\n"
                "E4 Alice Smith Senior Manager Approved"
            ),
            "multiword_name_without_status_anchor": (
                "EmployeeID Name\nE5 Alice Smith"
            ),
            "status_not_at_row_end": (
                "EmployeeID Status Name\nE6 Approved Alice Smith"
            ),
            "known_suffix_with_unknown_modifier": (
                "EmployeeID Name Status\n"
                "E8 Alice Smith Conditionally Approved"
            ),
            "known_multiword_status_without_boundaries": (
                "EmployeeID Name Status\nE1 Alice Smith Not Approved"
            ),
        }
        for label, text in cases.items():
            with self.subTest(case=label):
                item = _evidence_view(
                    f"ev_{label}", text, page_number=1, ordinal=1
                )
                self.assertEqual(
                    [], builder._ordered_identity_rows([item])
                )

    def test_pdf_native_and_high_ocr_identity_conflict_is_excluded(self) -> None:
        facts = builder.DocumentFacts(
            document_id="doc_pdf",
            relative_path="sample/register.pdf",
            extension="pdf",
        )
        facts.add("document_status", "Approved", ["ev_status"])
        native = _evidence_view(
            "ev_native_page",
            "社員ID\n氏名\nEMP-001\n山田 太郎",
            page_number=1,
            ordinal=1,
        )
        high = [
            _evidence_view(
                "ev_high_header_id", "社員ID", page_number=1, ordinal=2,
                x=10, y=10, quality_disposition="eligible_high",
            ),
            _evidence_view(
                "ev_high_header_name", "氏名", page_number=1, ordinal=3,
                x=110, y=10, quality_disposition="eligible_high",
            ),
            _evidence_view(
                "ev_high_value_id", "EMP-001", page_number=1, ordinal=4,
                x=10, y=30, quality_disposition="eligible_high",
            ),
            _evidence_view(
                "ev_high_value_name", "山田 大郎", page_number=1, ordinal=5,
                x=110, y=30, quality_disposition="eligible_high",
            ),
        ]
        graph = builder.GraphAccumulator()
        diagnostics: dict[str, int] = {}
        builder._add_employee_identities(
            graph,
            {"doc_pdf": facts},
            [native, *high],
            diagnostics,
        )
        self.assertFalse(any(
            edge["relation_type"] == "IDENTIFIES_PERSON"
            for edge in graph.edges.values()
        ))
        self.assertEqual(2, diagnostics["pdf_identity_conflicts_excluded"])

    def test_pdf_native_and_high_identity_status_must_agree(self) -> None:
        facts = builder.DocumentFacts(
            document_id="doc_pdf",
            relative_path="sample/register.pdf",
            extension="pdf",
        )
        facts.add("document_status", "Approved", ["ev_status"])

        def high_row(*, include_status: bool) -> list[Any]:
            cells = [
                _evidence_view(
                    "ev_high_status_header_id", "EmployeeID",
                    page_number=1, ordinal=2, x=10, y=10,
                    quality_disposition="eligible_high",
                ),
                _evidence_view(
                    "ev_high_status_header_name", "Name",
                    page_number=1, ordinal=3, x=110, y=10,
                    quality_disposition="eligible_high",
                ),
            ]
            if include_status:
                cells.append(_evidence_view(
                    "ev_high_status_header_status", "Status",
                    page_number=1, ordinal=4, x=210, y=10,
                    quality_disposition="eligible_high",
                ))
            cells.extend([
                _evidence_view(
                    "ev_high_status_value_id", "E1",
                    page_number=1, ordinal=5, x=10, y=30,
                    quality_disposition="eligible_high",
                ),
                _evidence_view(
                    "ev_high_status_value_name", "Alice",
                    page_number=1, ordinal=6, x=110, y=30,
                    quality_disposition="eligible_high",
                ),
            ])
            if include_status:
                cells.append(_evidence_view(
                    "ev_high_status_value_status", "Active",
                    page_number=1, ordinal=7, x=210, y=30,
                    quality_disposition="eligible_high",
                ))
            return cells

        cases = {
            "active_inactive_conflict": (
                "EmployeeID Name Status\nE1  Alice  Inactive",
                True,
            ),
            "missing_native_status": (
                "EmployeeID Name\nE1 Alice",
                True,
            ),
            "missing_high_status": (
                "EmployeeID Name Status\nE1  Alice  Active",
                False,
            ),
        }
        for label, (native_text, high_has_status) in cases.items():
            native = _evidence_view(
                f"ev_native_{label}", native_text,
                page_number=1, ordinal=1,
            )
            graph = builder.GraphAccumulator()
            diagnostics: dict[str, int] = {}
            builder._add_employee_identities(
                graph,
                {"doc_pdf": facts},
                [native, *high_row(include_status=high_has_status)],
                diagnostics,
            )
            with self.subTest(case=label):
                self.assertFalse(any(
                    edge["relation_type"] == "IDENTIFIES_PERSON"
                    for edge in graph.edges.values()
                ))
                self.assertEqual(
                    2,
                    diagnostics["pdf_identity_status_conflicts_excluded"],
                )

    def test_cli_contract_is_question_independent_and_rejects_non_safe_input_name(self) -> None:
        self.assertEqual(
            ["documents_path", "evidence_path", "output_path", "state_path"],
            list(inspect.signature(builder.build).parameters),
        )
        with tempfile.TemporaryDirectory() as raw_directory:
            root = Path(raw_directory)
            documents = root / "semantic-documents.jsonl"
            documents.write_text("{}\n", encoding="utf-8")
            wrongly_named = root / "semantic-evidence.jsonl"
            wrongly_named.write_text("{}\n", encoding="utf-8")
            with self.assertRaisesRegex(
                ValueError, "evidence_input_must_be_safe_answer_evidence_jsonl"
            ):
                builder.build(
                    documents,
                    wrongly_named,
                    root / "graph.sqlite3",
                    root / "state.json",
                )

            source = {"relative_path": "sample/document.txt", "sha256": "a" * 64}
            _write_jsonl(
                documents,
                [{
                    "document_id": "doc_sample",
                    "source": source,
                    "evidence_ids": ["ev_sample"],
                    "status": "extracted",
                }],
            )
            unsafe_evidence = root / "safe-answer-evidence.jsonl"
            _write_jsonl(
                unsafe_evidence,
                [{
                    "evidence_id": "ev_sample",
                    "document_id": "doc_sample",
                    "source": source,
                    "locator": {"paragraph_index": 1},
                    "observed_text": "sample",
                    "adapter": {
                        "execution_policy": "execute",
                        "source_record_type": "paragraph",
                    },
                    "status": "observed",
                }],
            )
            with self.assertRaisesRegex(ValueError, "evidence_execution_policy_invalid"):
                builder.build(
                    documents,
                    unsafe_evidence,
                    root / "unsafe.sqlite3",
                    root / "unsafe-state.json",
                )


if __name__ == "__main__":
    unittest.main()
