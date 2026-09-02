from __future__ import annotations

import array
import hashlib
import importlib.util
import io
import json
import sqlite3
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / "distribution" / "macos-local-memory" / "engine"
APP = ROOT / "distribution" / "macos-local-memory" / "app"


def load_module(name: str, path: Path):
    specification = importlib.util.spec_from_file_location(name, path)
    if specification is None or specification.loader is None:
        raise ImportError(f"cannot load {path}")
    module = importlib.util.module_from_spec(specification)
    sys.modules[name] = module
    specification.loader.exec_module(module)
    return module


index_builder = load_module(
    "answer_graph_policy_index_builder",
    ENGINE / "build_local_semantic_index.py",
)
answer_v1 = load_module(
    "answer_graph_policy_v1",
    ENGINE / "answer_local_memory.py",
)
answer_v2 = load_module(
    "answer_graph_policy_v2",
    ENGINE / "answer_local_memory_v2.py",
)
final_audit = load_module(
    "answer_graph_policy_final_audit",
    APP / "final_answer_audit.py",
)


DOC_ID = "doc_11111111111111111111111111111111"
SAFE_ID = "ev_22222222222222222222222222222222"
HELD_ID = "ev_33333333333333333333333333333333"


def packed(values: list[float]) -> bytes:
    return array.array("f", values).tobytes()


def create_ready_index(path: Path) -> dict:
    connection = sqlite3.connect(path)
    index_builder.initialize(connection)
    evidence = [
        (SAFE_ID, "安全な根拠", [0.0, 1.0]),
        (HELD_ID, "除外元を含む派生値", [1.0, 0.0]),
    ]
    locator = {"sheet_name": "集計表", "cell": "A1"}
    relative_path = "fixture.xlsx"
    for evidence_id, text, vector in evidence:
        observed_sha256 = hashlib.sha256(text.encode("utf-8")).hexdigest()
        embedding_text = (
            f"ファイル: {relative_path}\n"
            f"場所: {json.dumps(locator, ensure_ascii=False, sort_keys=True)}\n"
            f"内容:\n{text}"
        )
        connection.execute(
            "INSERT INTO evidence VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                evidence_id,
                DOC_ID,
                relative_path,
                json.dumps(locator),
                text,
                embedding_text,
                0,
                observed_sha256,
            ),
        )
        connection.execute(
            "INSERT INTO embeddings VALUES (?, ?, ?)",
            (evidence_id, len(vector), packed(vector)),
        )

    partition = {
        "schema_version": "0.1",
        "partitioner": "content-security-graph-partitioner",
        "partitioner_version": "0.1.0",
        "status": "pass",
        "question_independent": True,
        "security_policy_version": "test",
        "security_state_sha256": "a" * 64,
        "safe_answer_evidence_sha256": "b" * 64,
        "document_source_set_sha256": "c" * 64,
        "evidence_source_set_sha256": "d" * 64,
        "source_relation_set_sha256": "e" * 64,
        "projected_relation_set_sha256": "f" * 64,
        "promoted_relation_ids": [],
        "held_relations": [],
        "held_derived_evidence": [{
            "evidence_id": HELD_ID,
            "reason_codes": ["excluded_lineage_source"],
            "excluded_source_evidence_ids": [
                "ev_44444444444444444444444444444444"
            ],
        }],
        "counts": {
            "source_relations": 0,
            "promoted_relations": 0,
            "held_relations": 0,
            "safe_evidence": 2,
            "held_derived_evidence": 1,
        },
    }
    partition["partition_sha256"] = answer_v1.record_sha256(partition)

    document_node = {
        "node_id": DOC_ID,
        "node_type": "document",
        "payload": {
            "record_type": "document",
            "record_id": DOC_ID,
            "source_record": {"status": "extracted"},
        },
        "status": "observed",
    }
    document_node["record_sha256"] = answer_v1.record_sha256(document_node)
    nodes = [document_node]
    for evidence_id, text, _vector in evidence:
        observed_sha256 = hashlib.sha256(text.encode("utf-8")).hexdigest()
        status = "unresolved" if evidence_id == HELD_ID else "observed"
        payload = {
            "record_type": "evidence",
            "record_id": evidence_id,
            "source_record": {
                "document_id": DOC_ID,
                "locator": locator,
                "source": {"relative_path": relative_path},
            },
            "observed_sha256": observed_sha256,
        }
        if status == "unresolved":
            payload["security_graph_hold"] = {
                "reason_codes": ["excluded_lineage_source"],
                "excluded_source_evidence_ids": [
                    "ev_44444444444444444444444444444444"
                ],
                "partition_sha256": partition["partition_sha256"],
            }
        node = {
            "node_id": evidence_id,
            "node_type": "evidence",
            "payload": payload,
            "status": status,
        }
        node["record_sha256"] = answer_v1.record_sha256(node)
        nodes.append(node)

    for node in nodes:
        connection.execute(
            "INSERT INTO graph_nodes VALUES (?, ?, ?, ?, ?)",
            (
                node["node_id"],
                node["node_type"],
                answer_v1.canonical_json(node["payload"]),
                node["status"],
                node["record_sha256"],
            ),
        )
    graph = {
        "graph_schema_version": "0.1",
        "nodes": nodes,
        "edges": [],
    }
    eligible_rows = [{
        "evidence_id": SAFE_ID,
        "status": "observed",
        "record_sha256": next(
            node["record_sha256"] for node in nodes if node["node_id"] == SAFE_ID
        ),
    }]
    metadata = {
        "schema_version": "0.3",
        "model": "embedding-test",
        "embedding_dimension": 2,
        "evidence_sha256": "b" * 64,
        "content_security_state_sha256": "a" * 64,
        "content_security_gate": True,
        "content_security_execution_policy": "never_execute",
        "index_purpose": "safe_answer",
        "answer_generation_allowed": True,
        "graph_schema_version": "0.1",
        "graph_status": "validated_safe_partition",
        "graph_retrieval_enabled": True,
        "graph_node_count": 3,
        "graph_edge_count": 0,
        "graph_document_node_count": 1,
        "graph_evidence_node_count": 2,
        "graph_sha256": answer_v1.record_sha256(graph),
        "graph_security_partition": partition,
        "graph_security_partition_sha256": partition["partition_sha256"],
        "graph_source_relation_input_count": 0,
        "graph_retrievable_evidence_count": 1,
        "graph_unresolved_evidence_count": 1,
        "graph_held_derived_evidence_count": 1,
        "graph_nonindexed_held_derived_evidence_count": 0,
        "graph_retrievable_evidence_set_sha256": answer_v1.record_sha256(
            eligible_rows
        ),
        "embedding_space_probe_version": (
            answer_v1.EMBEDDING_SPACE_PROBE_VERSION
        ),
        "embedding_space_probe_text_sha256": hashlib.sha256(
            answer_v1.EMBEDDING_SPACE_PROBE_TEXT.encode("utf-8")
        ).hexdigest(),
        "embedding_space_probe_dimension": 2,
        "embedding_space_probe_vector_f32_sha256": hashlib.sha256(
            packed([1.0, 0.0])
        ).hexdigest(),
        "graph_embeddings_sha256": answer_v1.record_sha256({
            "model": "embedding-test",
            "probe": {
                "version": answer_v1.EMBEDDING_SPACE_PROBE_VERSION,
                "text_sha256": hashlib.sha256(
                    answer_v1.EMBEDDING_SPACE_PROBE_TEXT.encode("utf-8")
                ).hexdigest(),
                "dimension": 2,
                "vector_f32_sha256": hashlib.sha256(
                    packed([1.0, 0.0])
                ).hexdigest(),
            },
            "records": [
                {
                    "evidence_id": evidence_id,
                    "dimension": len(vector),
                    "vector_f32_sha256": hashlib.sha256(packed(vector)).hexdigest(),
                }
                for evidence_id, _text, vector in evidence
            ],
        }),
    }
    connection.executemany(
        "INSERT INTO metadata(key, value) VALUES (?, ?)",
        [
            (key, json.dumps(value, ensure_ascii=False, sort_keys=True))
            for key, value in metadata.items()
        ],
    )
    connection.commit()
    connection.close()
    return metadata


class AnswerGraphRetrievalPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.index_path = Path(self.temporary.name) / "answer.sqlite3"
        self.metadata = create_ready_index(self.index_path)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_v1_and_v2_retrieval_exclude_unresolved_evidence(self) -> None:
        with mock.patch.object(answer_v1, "embed_query", return_value=[1.0, 0.0]):
            _metadata, v1_results = answer_v1.retrieve(
                self.index_path, "質問", 5, 1
            )
        with mock.patch.object(
            answer_v2.base, "embed_query", return_value=[1.0, 0.0]
        ):
            _metadata, v2_results = answer_v2.retrieve_hybrid(
                self.index_path, "質問", 5, 1
            )
        self.assertEqual([row["evidence_id"] for row in v1_results], [SAFE_ID])
        self.assertEqual([row["evidence_id"] for row in v2_results], [SAFE_ID])

        records, by_id = answer_v2.load_index_evidence_records(self.index_path)
        self.assertEqual([row["evidence_id"] for row in records], [SAFE_ID])
        self.assertEqual(set(by_id), {SAFE_ID})

    def test_final_audit_can_load_only_retrievable_evidence(self) -> None:
        self.assertEqual(
            [row["evidence_id"] for row in final_audit.graph_evidence(self.index_path)],
            [SAFE_ID],
        )

    def test_final_audit_rejects_verified_with_unsupported_claims(self) -> None:
        raw = {
            "message": {
                "content": json.dumps({
                    "verdict": "verified",
                    "reason": "問題なし",
                    "unsupported_claims": ["13回という主張は未支持"],
                }, ensure_ascii=False),
            },
        }
        response = io.BytesIO(
            json.dumps(raw, ensure_ascii=False).encode("utf-8")
        )
        with mock.patch.object(
            final_audit.urllib.request,
            "urlopen",
            return_value=response,
        ):
            with self.assertRaisesRegex(
                ValueError, "verified_with_unsupported_claim"
            ):
                final_audit.audit(
                    "gemma4:12b",
                    "何回？",
                    {"answer": "13回です"},
                    [],
                    1,
                )

    def test_final_audit_requires_unsupported_claims_field(self) -> None:
        raw = {
            "message": {
                "content": json.dumps({
                    "verdict": "verified",
                    "reason": "問題なし",
                }, ensure_ascii=False),
            },
        }
        response = io.BytesIO(
            json.dumps(raw, ensure_ascii=False).encode("utf-8")
        )
        with mock.patch.object(
            final_audit.urllib.request,
            "urlopen",
            return_value=response,
        ):
            with self.assertRaisesRegex(
                ValueError, "audit_unsupported_claims_invalid"
            ):
                final_audit.audit(
                    "gemma4:12b",
                    "何回？",
                    {"answer": "13回です"},
                    [],
                    1,
                )
        self.assertEqual(
            [
                row["evidence_id"]
                for row in final_audit.evidence(
                    self.index_path, [HELD_ID, SAFE_ID]
                )
            ],
            [SAFE_ID],
        )

    def test_schema_only_index_is_refused_before_embedding(self) -> None:
        connection = sqlite3.connect(self.index_path)
        connection.execute(
            "UPDATE metadata SET value = ? WHERE key = 'graph_status'",
            (json.dumps("schema_only"),),
        )
        connection.commit()
        connection.close()
        with mock.patch.object(answer_v1, "embed_query") as embed_query:
            with self.assertRaisesRegex(ValueError, "graph_projection_required"):
                answer_v1.retrieve(self.index_path, "質問", 5, 1)
        embed_query.assert_not_called()

    def test_graph_node_tamper_is_refused(self) -> None:
        connection = sqlite3.connect(self.index_path)
        connection.execute(
            "UPDATE graph_nodes SET status = 'observed' WHERE node_id = ?",
            (HELD_ID,),
        )
        connection.commit()
        connection.close()
        with self.assertRaisesRegex(ValueError, "graph_node_record_hash_mismatch"):
            answer_v1.load_answer_evidence_records(self.index_path)

    def test_evidence_path_or_locator_tamper_is_refused(self) -> None:
        connection = sqlite3.connect(self.index_path)
        connection.execute(
            "UPDATE evidence SET relative_path = ? WHERE evidence_id = ?",
            ("forged.xlsx", SAFE_ID),
        )
        connection.commit()
        connection.close()
        with self.assertRaisesRegex(
            ValueError, "graph_evidence_payload_binding_mismatch"
        ):
            answer_v1.load_answer_evidence_records(self.index_path)

    def test_embedding_vector_tamper_is_refused(self) -> None:
        connection = sqlite3.connect(self.index_path)
        connection.execute(
            "UPDATE embeddings SET vector_f32 = ? WHERE evidence_id = ?",
            (packed([0.5, 0.5]), SAFE_ID),
        )
        connection.commit()
        connection.close()
        with self.assertRaisesRegex(
            ValueError, "graph_embeddings_sha256_mismatch"
        ):
            answer_v1.load_answer_evidence_records(self.index_path)

    def test_embedding_model_tamper_is_refused(self) -> None:
        connection = sqlite3.connect(self.index_path)
        connection.execute(
            "UPDATE metadata SET value = ? WHERE key = 'model'",
            (json.dumps("different-embedding-model"),),
        )
        connection.commit()
        connection.close()
        with self.assertRaisesRegex(
            ValueError, "graph_embeddings_sha256_mismatch"
        ):
            answer_v1.load_answer_evidence_records(self.index_path)

    def test_retagged_embedding_space_is_refused_before_query(self) -> None:
        with mock.patch.object(
            answer_v1, "embed_query", return_value=[0.0, 1.0]
        ):
            with self.assertRaisesRegex(
                ValueError, "embedding_space_changed_rebuild_required"
            ):
                answer_v1.retrieve(self.index_path, "質問", 5, 1)

    def test_question_graph_cannot_reintroduce_a_held_id(self) -> None:
        artifact = {
            "status": "ready",
            "selected_evidence_ids": [HELD_ID],
        }
        with self.assertRaisesRegex(
            ValueError, "question_graph_selected_evidence_not_retrievable"
        ):
            answer_v2.augment_with_question_graph(
                [], {}, artifact, {"status": "pass"}
            )

    def test_cache_key_is_bound_to_graph_and_partition(self) -> None:
        original = answer_v2.answer_cache_key(
            "質問", self.metadata, "gemma4:12b", 5, "sequential"
        )
        changed = dict(self.metadata)
        changed["graph_sha256"] = "9" * 64
        self.assertNotEqual(
            original,
            answer_v2.answer_cache_key(
                "質問", changed, "gemma4:12b", 5, "sequential"
            ),
        )

    def test_cached_record_cannot_reintroduce_unresolved_evidence(self) -> None:
        record = {
            "index": {
                field: self.metadata[field]
                for field in (
                    "evidence_sha256",
                    "graph_sha256",
                    "graph_security_partition_sha256",
                    "graph_retrievable_evidence_set_sha256",
                    "graph_embeddings_sha256",
                )
            },
            "answer": {
                "evidence_ids": [HELD_ID],
                "diagnostic_evidence_ids": [],
            },
            "retrieved": [],
            "question_evidence_graph": {},
        }
        self.assertFalse(
            answer_v2.cached_record_matches_answer_graph(
                record, self.metadata, self.index_path
            )
        )

    def test_structurally_valid_generic_cache_is_not_replayed(self) -> None:
        query = "内容は？"
        records, by_id = answer_v2.load_index_evidence_records(self.index_path)
        artifact = answer_v2.question_graph.build_question_evidence_graph(
            query, records
        )
        graph_validation = (
            answer_v2.question_graph.validate_question_evidence_graph(
                query, records, artifact
            )
        )
        plan = {
            "items": [{
                "item_id": "F1",
                "label": "内容",
                "required_claim": "記載内容",
                "retrieval_query": "内容",
                "required": True,
            }],
            "answer_shape": "内容",
        }
        audit = {
            "item_id": "F1",
            "verdict": "supported",
            "supported_value": "安全な根拠",
            "supporting_packet_ids": [SAFE_ID],
            "competing_packet_ids": [],
            "reason_code": "none",
            "defect": "",
            "missing_information": [],
        }
        answer = answer_v2.generate_projected_answer(
            "", query, plan, [audit], by_id, 0
        )
        record = {
            "schema_version": "0.3-field-audit",
            "query": query,
            "question_plan": plan,
            "question_evidence_graph": artifact,
            "question_evidence_graph_validation": graph_validation,
            "field_runs": [{
                "item": plan["items"][0],
                "retrieved_evidence_ids": [SAFE_ID],
                "question_graph_branch_id": artifact.get("artifact_id"),
                "graph_augmented_evidence_ids": [],
                "graph_primary_evidence_ids": artifact.get(
                    "selected_evidence_ids", []
                ),
                "audit": audit,
            }],
            "answer": answer,
            "retrieved": [{
                "evidence_id": SAFE_ID,
                "document_id": by_id[SAFE_ID]["document_id"],
                "relative_path": by_id[SAFE_ID]["relative_path"],
                "locator": by_id[SAFE_ID]["locator"],
            }],
            "index": {
                field: self.metadata[field]
                for field in (
                    "evidence_sha256",
                    "graph_sha256",
                    "graph_security_partition_sha256",
                    "graph_retrievable_evidence_set_sha256",
                    "graph_embeddings_sha256",
                )
            },
        }
        self.assertFalse(
            answer_v2.cached_record_matches_answer_graph(
                record, self.metadata, self.index_path, query
            )
        )

    def test_final_audit_rejects_record_that_cites_unresolved_evidence(self) -> None:
        answer = {
            "answer_status": "answered",
            "answer_mode": "grounded",
            "answer": "除外対象の値",
            "evidence_ids": [HELD_ID],
            "basis_summary": "",
            "uncertainties": [],
            "non_answer_reason": {"code": "none", "explanation": ""},
            "diagnostic_evidence_ids": [],
            "needed_information": [],
            "follow_up_question": "",
            "reconsideration_condition": "",
            "verification_reminder": "",
        }
        record = {
            "query": "質問",
            "answer": answer,
            "question_evidence_graph": {},
            "index": {
                field: self.metadata[field]
                for field in (
                    "evidence_sha256",
                    "graph_sha256",
                    "graph_security_partition_sha256",
                    "graph_retrievable_evidence_set_sha256",
                    "graph_embeddings_sha256",
                )
            },
            "models": {},
            "performance": {},
        }
        record_path = Path(self.temporary.name) / "record.json"
        record_path.write_text(
            json.dumps(record, ensure_ascii=False), encoding="utf-8"
        )
        argv = [
            str(APP / "final_answer_audit.py"),
            "--record", str(record_path),
            "--index", str(self.index_path),
        ]
        claim_validation = {
            "status": "pass",
            "failures": [],
            "warnings": [],
        }
        with mock.patch.object(sys, "argv", argv), mock.patch.object(
            final_audit.question_graph,
            "validate_question_evidence_graph",
            return_value={"status": "pass", "failures": []},
        ), mock.patch.object(
            final_audit.claim_validator,
            "build_and_validate",
            return_value=({}, {}, claim_validation),
        ), mock.patch.object(final_audit, "audit") as llm_audit, redirect_stdout(
            io.StringIO()
        ) as output:
            self.assertEqual(final_audit.main(), 0)
        llm_audit.assert_not_called()
        audited = json.loads(output.getvalue())
        self.assertEqual(audited["answer_graph_validation"]["status"], "blocked")
        self.assertEqual(
            audited["performance"]["independent_final_audit"]["skip_reason"],
            "answer_graph_validation_blocked",
        )
        self.assertEqual(audited["answer"]["answer_status"], "insufficient")
        self.assertEqual(audited["answer"]["diagnostic_evidence_ids"], [])
        self.assertEqual(audited["orchestration_decision"]["status"], "rejected")
        self.assertFalse(
            audited["orchestration_decision"]["checks"]["answer_graph"]
        )


if __name__ == "__main__":
    unittest.main()
