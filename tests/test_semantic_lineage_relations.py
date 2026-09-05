from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / "distribution" / "macos-local-memory" / "engine"
VALIDATOR_PATH = ENGINE / "validate_adaptive_semantic_graph.py"
BUILDER_PATH = ENGINE / "build_adaptive_semantic_graph.py"
INDEX_PATH = ENGINE / "build_local_semantic_index.py"


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


validator = load_module("semantic_lineage_validator", VALIDATOR_PATH)
adaptive_builder = load_module("semantic_lineage_adaptive_builder", BUILDER_PATH)
index_builder = load_module("semantic_lineage_index_builder", INDEX_PATH)


DOC_ID = "doc_" + "d" * 32
OTHER_DOC_ID = "doc_" + "e" * 32
SOURCE_A = "ev_" + "1" * 32
SOURCE_B = "ev_" + "2" * 32
BINARY_SOURCE = "ev_" + "3" * 32
RUN_AT = "2026-09-01T00:00:00+00:00"


def canonical(value: object) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )


def text_sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def search_unit(
    source_ids: list[str],
    *,
    document_id: str = DOC_ID,
    unit_type: str = "table_row",
    text: str = "項目: 稼働回数 | 値: 13",
) -> dict:
    value = {
        "schema_version": "0.1",
        "record_type": "search_unit",
        "document_id": document_id,
        "unit_type": unit_type,
        "source_evidence_ids": source_ids,
        "locator": {"sheet_name": "集計表", "row_index": 33},
        "text": {
            "search_text": text,
            "sha256": text_sha(text),
            "char_count": len(text),
        },
        "provenance": {
            "builder": "search-unit-builder",
            "builder_version": "0.6.0",
            "generated_at": RUN_AT,
            "deterministic": True,
        },
    }
    value["search_unit_id"] = validator.stable_id("su", {
        "document_id": document_id,
        "unit_type": unit_type,
        "source_evidence_ids": source_ids,
        "locator": value["locator"],
        "text_sha256": value["text"]["sha256"],
        "builder": value["provenance"]["builder"],
        "builder_version": value["provenance"]["builder_version"],
    })
    return value


def layer_evidence(
    evidence_id: str,
    *,
    document_id: str = DOC_ID,
    binary: bool = False,
) -> dict:
    content = (
        {"content_ref": "sample.png", "sha256": "a" * 64}
        if binary else {"raw_text": evidence_id}
    )
    return {
        "evidence_id": evidence_id,
        "document_id": document_id,
        "evidence_type": "image" if binary else "table_cell",
        "content": content,
    }


def semantic_source(
    evidence_id: str,
    *,
    document_id: str = DOC_ID,
    observed_text: str | None = None,
) -> dict:
    return {
        "schema_version": "0.1",
        "evidence_id": evidence_id,
        "document_id": document_id,
        "ordinal": 1,
        "locator": {"sheet_name": "集計表", "cell": "A1"},
        "observed_text": observed_text or evidence_id,
        "source": {"relative_path": "dawn.xlsx", "sha256": "a" * 64},
        "extraction_method": "fixture",
        "status": "observed",
        "adapter": {
            "name": validator.ADAPTER_NAME,
            "version": validator.ADAPTER_VERSION,
            "source_record_type": "table_cell",
            "text_projection": "raw_text",
            "execution_policy": "never_execute",
        },
    }


def derived_projection(unit: dict, *, observed_text: str | None = None) -> dict:
    evidence_id = validator._expected_search_unit_projection_id(unit)
    return {
        "schema_version": "0.1",
        "evidence_id": evidence_id,
        "document_id": unit["document_id"],
        "ordinal": 3,
        "locator": unit["locator"],
        "observed_text": observed_text or unit["text"]["search_text"],
        "source": {"relative_path": "dawn.xlsx", "sha256": "a" * 64},
        "extraction_method": "verified_search_unit_projection",
        "status": "observed",
        "adapter": {
            "name": validator.ADAPTER_NAME,
            "version": validator.ADAPTER_VERSION,
            "source_record_type": "search_unit",
            "source_search_unit_id": unit["search_unit_id"],
            "source_evidence_ids": unit["source_evidence_ids"],
            "unit_type": unit["unit_type"],
            "text_projection": "search_unit_text",
            "execution_policy": "never_execute",
        },
    }


class SemanticLineageRelationTests(unittest.TestCase):
    def test_unsharded_table_row_promotes_exact_stable_fan_in(self) -> None:
        unit = search_unit([SOURCE_A, SOURCE_B])
        derived = derived_projection(unit)
        semantic = [semantic_source(SOURCE_B), derived, semantic_source(SOURCE_A)]
        layer = [layer_evidence(SOURCE_A), layer_evidence(SOURCE_B)]

        relations, coverage = validator.derive_verified_lineage_relations(
            [unit], semantic, layer,
        )

        self.assertEqual(len(relations), 2)
        self.assertEqual(coverage["verified_derived_count"], 1)
        self.assertEqual(coverage["verified_relation_count"], 2)
        self.assertEqual(coverage["held_derived_count"], 0)
        self.assertEqual(
            {item["to_ref"]["record_id"] for item in relations},
            {SOURCE_A, SOURCE_B},
        )
        for relation in relations:
            self.assertEqual(relation["relation_class"], "lineage")
            self.assertEqual(relation["relation_type"], "derived_from")
            self.assertEqual(
                relation["from_ref"],
                {"record_type": "evidence", "record_id": derived["evidence_id"]},
            )
            self.assertEqual(
                relation["supporting_evidence_ids"],
                [relation["to_ref"]["record_id"]],
            )
            self.assertEqual(relation["status"], "verified")
            self.assertEqual(
                relation["provenance"],
                {
                    "generated_by": validator.LINEAGE_VALIDATOR,
                    "generator_version": validator.LINEAGE_VALIDATOR_VERSION,
                    "generated_at": RUN_AT,
                    "deterministic": True,
                    "confidence": 1.0,
                    "rule_or_model": validator.LINEAGE_RULE,
                    "warnings": [],
                },
            )
            self.assertEqual(
                relation["relation_id"],
                validator._stable_lineage_relation_id(
                    relation["from_ref"], relation["to_ref"],
                ),
            )
            self.assertEqual(
                relation["properties"]["fan_in_sha256"],
                validator.canonical_sha256([SOURCE_A, SOURCE_B]),
            )
        reordered, reordered_coverage = validator.derive_verified_lineage_relations(
            [copy.deepcopy(unit)], list(reversed(semantic)), list(reversed(layer)),
        )
        self.assertEqual((relations, coverage), (reordered, reordered_coverage))

    def test_tampered_binding_missing_and_cross_document_fail_closed(self) -> None:
        unit = search_unit([SOURCE_A])
        derived = derived_projection(unit)
        tampered = copy.deepcopy(derived)
        tampered["adapter"]["source_evidence_ids"] = [SOURCE_B]
        with self.assertRaisesRegex(
            ValueError, "lineage_derived_projection_binding_invalid",
        ):
            validator.derive_verified_lineage_relations(
                [unit], [semantic_source(SOURCE_A), tampered],
                [layer_evidence(SOURCE_A)],
            )

        with self.assertRaisesRegex(
            ValueError, "lineage_source_unexplained_missing",
        ):
            validator.derive_verified_lineage_relations(
                [unit], [derived], [],
            )

        with self.assertRaisesRegex(
            ValueError, "lineage_source_cross_document",
        ):
            validator.derive_verified_lineage_relations(
                [unit], [semantic_source(SOURCE_A), derived],
                [layer_evidence(SOURCE_A, document_id=OTHER_DOC_ID)],
            )

    def test_binary_source_holds_the_whole_fan_in(self) -> None:
        unit = search_unit(
            [BINARY_SOURCE, SOURCE_A], unit_type="image_text_packet",
        )
        derived = derived_projection(unit)
        relations, coverage = validator.derive_verified_lineage_relations(
            [unit], [semantic_source(SOURCE_A), derived],
            [
                layer_evidence(BINARY_SOURCE, binary=True),
                layer_evidence(SOURCE_A),
            ],
        )

        self.assertEqual(relations, [])
        self.assertEqual(coverage["verified_derived_count"], 0)
        self.assertEqual(coverage["held_derived_count"], 1)
        self.assertEqual(coverage["held_source_reference_count"], 2)
        self.assertEqual(
            coverage["held"][0]["reasons"],
            ["non_projected_binary_source"],
        )
        self.assertEqual(
            coverage["held"][0]["unresolved_source_evidence_ids"],
            [BINARY_SOURCE],
        )

    def test_derived_or_source_shards_hold_until_projection_anchor_exists(self) -> None:
        unit = search_unit([SOURCE_A])
        long_derived = derived_projection(unit, observed_text="x" * 4_000)
        derived_shards = validator.expected_question_shards(long_derived)
        relations, coverage = validator.derive_verified_lineage_relations(
            [unit], [semantic_source(SOURCE_A), *derived_shards],
            [layer_evidence(SOURCE_A)],
        )
        self.assertEqual(relations, [])
        self.assertEqual(
            coverage["held"][0]["reasons"], ["requires_projection_anchor"],
        )

        source_projection = semantic_source(
            SOURCE_A, observed_text="y" * 4_000,
        )
        source_shards = validator.expected_question_shards(source_projection)
        relations, coverage = validator.derive_verified_lineage_relations(
            [unit], [*source_shards, derived_projection(unit)],
            [layer_evidence(SOURCE_A)],
        )
        self.assertEqual(relations, [])
        self.assertEqual(
            coverage["held"][0]["reasons"], ["requires_projection_anchor"],
        )
        self.assertEqual(
            coverage["held"][0]["unresolved_source_evidence_ids"], [SOURCE_A],
        )

    def test_cycle_guard_rejects_self_loop_and_multi_node_cycle(self) -> None:
        def edge(source: str, target: str) -> dict:
            return {
                "from_ref": {"record_type": "evidence", "record_id": source},
                "to_ref": {"record_type": "evidence", "record_id": target},
            }

        with self.assertRaisesRegex(ValueError, "lineage_self_loop"):
            validator._assert_acyclic_lineage([edge(SOURCE_A, SOURCE_A)])
        with self.assertRaisesRegex(ValueError, "lineage_cycle"):
            validator._assert_acyclic_lineage([
                edge(SOURCE_A, SOURCE_B), edge(SOURCE_B, SOURCE_A),
            ])

    def test_native_section_contains_requires_real_heading_evidence(self) -> None:
        heading_id = "ev_" + "a" * 32
        table_id = "ev_" + "b" * 32
        provenance = {
            "extraction_method": "ooxml_stdlib_docx_fallback",
            "extractor": "intermediate-record-extractor",
            "extractor_version": "0.10.1",
            "extracted_at": RUN_AT,
            "deterministic": True,
            "confidence": 1.0,
            "warnings": [],
        }
        document = {
            "document_id": DOC_ID,
            "extraction": {"extracted_at": RUN_AT},
        }
        heading = {
            "evidence_id": heading_id,
            "document_id": DOC_ID,
            "evidence_type": "heading",
            "content": {"raw_text": "稼働集計"},
            "provenance": provenance,
            "native_properties": {},
        }
        table = {
            "evidence_id": table_id,
            "document_id": DOC_ID,
            "evidence_type": "table",
            "content": {"raw_value": {"rows": 2, "columns": 2}},
            "provenance": provenance,
            "native_properties": {
                "preceding_heading_evidence_id": heading_id,
                "preceding_heading_text": "稼働集計",
            },
        }
        state = {
            "extractor": "intermediate-record-extractor",
            "extractor_version": "0.10.1",
            "run_at": RUN_AT,
        }

        relations = validator.derive_native_structural_relations(
            [document], [heading, table], state,
        )
        section = [
            item for item in relations
            if item["relation_type"] == "section_contains"
        ]
        self.assertEqual(1, len(section))
        self.assertEqual(
            {
                "record_type": "evidence",
                "record_id": heading_id,
            },
            section[0]["from_ref"],
        )
        self.assertEqual(
            {"record_type": "evidence", "record_id": table_id},
            section[0]["to_ref"],
        )

        mislabeled = copy.deepcopy(heading)
        mislabeled["evidence_type"] = "paragraph"
        with self.assertRaisesRegex(
            ValueError, "native_structural_heading_binding_mismatch",
        ):
            validator.derive_native_structural_relations(
                [document], [mislabeled, table], state,
            )

    def test_full_validator_publishes_only_after_pass(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source_root = base / "source"
            output = base / "semantic"
            source_root.mkdir()
            source_path = source_root / "sample.csv"
            source_path.write_text(
                "Item,Value\nalpha,13\nbeta,21\n", encoding="utf-8",
            )
            inventory_path = base / "path-source-inventory.jsonl"
            inventory_record = {
                "kind": "file",
                "relative_path": source_path.name,
                "read_status": "observed",
                "size_bytes": source_path.stat().st_size,
                "sha256": hashlib.sha256(source_path.read_bytes()).hexdigest(),
            }
            inventory_path.write_text(
                canonical(inventory_record) + "\n", encoding="utf-8",
            )
            adaptive_builder.build(
                source_root, inventory_path, output, ROOT / "scripts",
            )

            report = validator.validate(output, source_root, inventory_path)

            relations_path = output / validator.LINEAGE_RELATIONS_FILE
            state_path = output / validator.LINEAGE_VALIDATION_FILE
            self.assertTrue(relations_path.is_file())
            self.assertTrue(state_path.is_file())
            relations = [
                json.loads(line)
                for line in relations_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            state = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertGreater(len(relations), 0)
            self.assertEqual(report["lineage_relations"], len(relations))
            self.assertEqual(state["status"], "pass")
            self.assertEqual(state["validator"], validator.LINEAGE_VALIDATOR)
            self.assertEqual(state["output"]["count"], len(relations))
            self.assertEqual(
                state["output"]["verified_relation_ids"],
                sorted(item["relation_id"] for item in relations),
            )
            self.assertEqual(
                state["output"]["sha256"],
                hashlib.sha256(relations_path.read_bytes()).hexdigest(),
            )
            self.assertEqual(
                state["output"]["relation_source_set_sha256"],
                validator.record_source_set_sha256(relations, "relation_id"),
            )
            documents = validator.read_jsonl(output / "semantic-documents.jsonl")
            evidence = validator.read_jsonl(output / "semantic-evidence.jsonl")
            self.assertEqual(
                state["inputs"]["document_source_set_sha256"],
                validator.record_source_set_sha256(documents, "document_id"),
            )
            self.assertEqual(
                state["inputs"]["evidence_source_set_sha256"],
                validator.record_source_set_sha256(evidence, "evidence_id"),
            )
            layer_evidence_records = validator.read_jsonl(
                output / "layer1-intermediate" / "evidence.jsonl"
            )
            layer_relation_records = validator.read_jsonl(
                output / "layer1-intermediate" / "relations.jsonl"
            )
            search_units = validator.read_jsonl(
                output / "layer1-search" / "search_units.jsonl"
            )
            self.assertEqual(
                state["inputs"]["layer_evidence_source_set_sha256"],
                validator.record_source_set_sha256(
                    layer_evidence_records, "evidence_id",
                ),
            )
            self.assertEqual(
                state["inputs"]["layer_relation_source_set_sha256"],
                validator.record_source_set_sha256(
                    layer_relation_records, "relation_id",
                ),
            )
            self.assertEqual(
                state["inputs"]["search_unit_source_set_sha256"],
                validator.record_source_set_sha256(search_units, "search_unit_id"),
            )

            structural_relations = validator.read_jsonl(
                output / "layer1-intermediate" / "relations.jsonl"
            )
            omitted_binary_relation = copy.deepcopy(
                next(
                    item for item in structural_relations
                    if item["from_ref"]["record_type"] == "document"
                    and item["to_ref"]["record_type"] == "evidence"
                )
            )
            omitted_binary_relation["to_ref"] = {
                "record_type": "evidence",
                "record_id": BINARY_SOURCE,
            }
            omitted_binary_relation["relation_id"] = (
                index_builder._stable_relation_id(omitted_binary_relation)
            )
            with self.assertRaisesRegex(
                ValueError, "graph_structural_attested_relations_mismatch"
            ):
                index_builder._attest_lineage_context(
                    documents,
                    evidence,
                    [*structural_relations, omitted_binary_relation, *relations],
                    {
                        "output_dir": output,
                        "source_root": source_root,
                        "inventory": inventory_path,
                    },
                )
            connection = sqlite3.connect(":memory:")
            try:
                index_builder.initialize(connection)
                for record in evidence:
                    observed_text = record["observed_text"]
                    connection.execute(
                        "INSERT INTO evidence VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            record["evidence_id"], record["document_id"],
                            record["source"]["relative_path"],
                            index_builder.canonical_json(record["locator"]),
                            observed_text, observed_text, 0,
                            hashlib.sha256(observed_text.encode("utf-8")).hexdigest(),
                        ),
                    )
                graph_report = index_builder.project_verified_structural_graph(
                    connection,
                    documents,
                    evidence,
                    [*structural_relations, *relations],
                    lineage_context={
                        "output_dir": output,
                        "source_root": source_root,
                        "inventory": inventory_path,
                    },
                )
                self.assertEqual(
                    graph_report["edge_count"],
                    len(structural_relations) + len(relations),
                )
                self.assertEqual(graph_report["isolated_node_count"], 0)
                self.assertEqual(
                    connection.execute("PRAGMA foreign_key_check").fetchall(), [],
                )

                forged_relations = copy.deepcopy(relations)
                forged_relations[0]["properties"]["source_search_unit_sha256"] = (
                    "0" * 64
                )
                with self.assertRaisesRegex(
                    ValueError, "graph_lineage_attested_relations_mismatch",
                ):
                    index_builder.project_verified_structural_graph(
                        connection,
                        documents,
                        evidence,
                        [*structural_relations, *forged_relations],
                        lineage_context={
                            "output_dir": output,
                            "source_root": source_root,
                            "inventory": inventory_path,
                        },
                    )

                forged_structural = copy.deepcopy(structural_relations)
                structural_index = next(
                    index
                    for index, item in enumerate(forged_structural)
                    if item["from_ref"]["record_type"] == "evidence"
                )
                forged_structural[structural_index]["relation_type"] = (
                    "section_contains"
                )
                forged_structural[structural_index]["relation_id"] = (
                    index_builder._stable_relation_id(
                        forged_structural[structural_index]
                    )
                )
                with self.assertRaisesRegex(
                    ValueError, "graph_structural_attested_relations_mismatch",
                ):
                    index_builder.project_verified_structural_graph(
                        connection,
                        documents,
                        evidence,
                        [*forged_structural, *relations],
                        lineage_context={
                            "output_dir": output,
                            "source_root": source_root,
                            "inventory": inventory_path,
                        },
                    )
            finally:
                connection.close()

            evidence[0]["observed_text"] += " tampered"
            (output / "semantic-evidence.jsonl").write_text(
                "".join(canonical(item) + "\n" for item in evidence),
                encoding="utf-8",
            )
            with self.assertRaises(ValueError):
                validator.validate(output, source_root, inventory_path)
            self.assertFalse(relations_path.exists())
            self.assertFalse(state_path.exists())

    def test_validator_rejects_mixed_search_run_timestamps(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source_root = base / "source"
            output = base / "semantic"
            source_root.mkdir()
            source_path = source_root / "sample.csv"
            source_path.write_text("Item,Value\nalpha,13\n", encoding="utf-8")
            inventory_path = base / "path-source-inventory.jsonl"
            inventory_path.write_text(
                canonical({
                    "kind": "file",
                    "relative_path": source_path.name,
                    "read_status": "observed",
                    "size_bytes": source_path.stat().st_size,
                    "sha256": hashlib.sha256(source_path.read_bytes()).hexdigest(),
                }) + "\n",
                encoding="utf-8",
            )
            adaptive_builder.build(
                source_root, inventory_path, output, ROOT / "scripts",
            )

            search_units_path = output / "layer1-search" / "search_units.jsonl"
            search_units = validator.read_jsonl(search_units_path)
            self.assertTrue(validator.is_rfc3339_timestamp(RUN_AT))
            self.assertFalse(validator.is_rfc3339_timestamp("not-a-time"))
            search_units[0]["provenance"]["generated_at"] = RUN_AT
            search_units_path.write_text(
                "".join(canonical(item) + "\n" for item in search_units),
                encoding="utf-8",
            )
            adapter_state_path = (
                output / "layer1-adapter" / "layer1-adapter-state.json"
            )
            adapter_state = json.loads(adapter_state_path.read_text(encoding="utf-8"))
            adapter_state["search_unit_projection"]["search_units_sha256"] = (
                validator.sha256_file(search_units_path)
            )
            adapter_state_path.write_text(
                canonical(adapter_state) + "\n", encoding="utf-8",
            )
            reader_state_path = output / "adaptive-reader-state.json"
            reader_state = json.loads(reader_state_path.read_text(encoding="utf-8"))
            reader_state["stages"]["adapter"]["sha256"] = validator.sha256_file(
                adapter_state_path
            )
            reader_state_path.write_text(
                canonical(reader_state) + "\n", encoding="utf-8",
            )

            with self.assertRaisesRegex(
                ValueError, "lineage_search_unit_run_mismatch"
            ):
                validator.validate(output, source_root, inventory_path)


if __name__ == "__main__":
    unittest.main()
