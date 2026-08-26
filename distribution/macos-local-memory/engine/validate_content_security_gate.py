#!/usr/bin/env python3
"""Validate Content Security Gate lineage, partitioning, and fail-closed policy."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def fail(condition: bool, message: str) -> None:
    if condition:
        raise ValueError(message)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence", required=True)
    parser.add_argument("--documents", required=True)
    parser.add_argument("--gate-dir", required=True)
    args = parser.parse_args()

    evidence_path = Path(args.evidence).resolve(strict=True)
    documents_path = Path(args.documents).resolve(strict=True)
    gate_dir = Path(args.gate_dir).resolve(strict=True)
    state_path = gate_dir / "content-security-state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    fail(state.get("schema_version") != "0.1", "state_schema_version")
    fail(state.get("policy_version") != "0.1.0", "state_policy_version")
    fail(state.get("classifier") != "deterministic_content_security_gate", "state_classifier")
    fail(state.get("question_independent") is not True, "state_question_independent")
    fail(state.get("llm_used_for_classification") is not False, "state_llm_flag")
    fail(state.get("all_source_content_trust") != "untrusted", "state_trust")
    fail(state.get("execution_policy") != "never_execute", "state_execution_policy")
    fail(state.get("quarantine_index_allowed") is not False, "state_quarantine_index")
    fail(state["source_evidence"]["sha256"] != sha256_file(evidence_path), "source_evidence_sha")
    fail(state["source_documents"]["sha256"] != sha256_file(documents_path), "source_documents_sha")

    source = load_jsonl(evidence_path)
    documents = load_jsonl(documents_path)
    classifications = load_jsonl(gate_dir / "content-security-classifications.jsonl")
    document_results = load_jsonl(gate_dir / "content-security-documents.jsonl")
    streams = {
        "answer_eligible": load_jsonl(gate_dir / "safe-answer-evidence.jsonl"),
        "prompt_library_only": load_jsonl(gate_dir / "prompt-library-evidence.jsonl"),
        "quarantine": load_jsonl(gate_dir / "quarantine-evidence.jsonl"),
    }

    source_ids = [item["evidence_id"] for item in source]
    classification_ids = [item["evidence_id"] for item in classifications]
    fail(len(source_ids) != len(set(source_ids)), "duplicate_source_evidence")
    fail(classification_ids != source_ids, "classification_order_or_coverage")
    fail(len(document_results) != len({item["document_id"] for item in document_results}), "duplicate_document_result")
    extracted_document_ids = {item["document_id"] for item in source}
    fail({item["document_id"] for item in document_results} != extracted_document_ids, "document_result_coverage")
    source_by_id = {item["evidence_id"]: item for item in source}
    doc_disposition = {item["document_id"]: item["disposition"] for item in document_results}

    stream_ids: dict[str, set[str]] = {}
    for disposition, records in streams.items():
        ids = [item["evidence_id"] for item in records]
        fail(len(ids) != len(set(ids)), f"duplicate_stream_id:{disposition}")
        stream_ids[disposition] = set(ids)
        for item in records:
            fail(source_by_id.get(item["evidence_id"]) != item, f"stream_record_changed:{item['evidence_id']}")
            fail(doc_disposition[item["document_id"]] != disposition, f"stream_wrong_disposition:{item['evidence_id']}")
    fail(bool(stream_ids["answer_eligible"] & stream_ids["prompt_library_only"]), "safe_prompt_overlap")
    fail(bool(stream_ids["answer_eligible"] & stream_ids["quarantine"]), "safe_quarantine_overlap")
    fail(bool(stream_ids["prompt_library_only"] & stream_ids["quarantine"]), "prompt_quarantine_overlap")
    fail(set.union(*stream_ids.values()) != set(source_ids), "stream_partition_incomplete")

    for source_record, classification in zip(source, classifications, strict=True):
        fail(classification.get("trust") != "untrusted", f"classification_trust:{classification['evidence_id']}")
        fail(classification.get("execution_policy") != "never_execute", f"classification_execution:{classification['evidence_id']}")
        expected_hash = hashlib.sha256(str(source_record.get("observed_text", "")).encode("utf-8")).hexdigest()
        fail(classification.get("observed_text_sha256") != expected_hash, f"classification_text_hash:{classification['evidence_id']}")
        fail(classification.get("document_disposition") != doc_disposition[classification["document_id"]], f"classification_document_disposition:{classification['evidence_id']}")
        if classification.get("content_role") in {"ai_instruction", "prompt_injection", "unknown_or_mixed"}:
            fail(classification["evidence_id"] in stream_ids["answer_eligible"], f"unsafe_evidence_in_safe_stream:{classification['evidence_id']}")

    for name, expected in state["outputs"].items():
        path = gate_dir / name
        fail(sha256_file(path) != expected["sha256"], f"output_sha:{name}")
        fail(path.stat().st_size != expected["size_bytes"], f"output_size:{name}")

    counts = state["counts"]
    fail(counts["classifications"] != len(classifications), "count_classifications")
    fail(counts["documents"] != len(document_results), "count_documents")
    fail(counts["safe_answer_evidence"] != len(streams["answer_eligible"]), "count_safe")
    fail(counts["prompt_library_evidence"] != len(streams["prompt_library_only"]), "count_prompt")
    fail(counts["quarantine_evidence"] != len(streams["quarantine"]), "count_quarantine")
    fail(counts["document_dispositions"] != dict(sorted(Counter(doc_disposition.values()).items())), "count_document_dispositions")
    fail(state["source_evidence"]["count"] != len(source), "source_evidence_count")
    fail(state["source_documents"]["count"] != len(documents), "source_document_count")
    print(json.dumps({"status": "PASS", "counts": counts}, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
