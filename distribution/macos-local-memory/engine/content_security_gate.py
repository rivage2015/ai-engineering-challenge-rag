#!/usr/bin/env python3
"""Classify extracted Evidence before indexing.

All source text is untrusted data and is never executable.  The gate keeps
normal answer material, prompt/instruction material, and quarantined material
in physically separate JSONL streams.  Classification is deterministic and
does not call an LLM.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import tempfile
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path


SCHEMA_VERSION = "0.1"
POLICY_VERSION = "0.2.0"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def normalized(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).split()).casefold()


PATTERNS: dict[str, tuple[re.Pattern[str], ...]] = {
    "priority_override": (
        re.compile(r"ignore\s+(?:all\s+)?(?:previous|prior|above)\s+(?:instructions?|conversations?|messages?|rules?)", re.I),
        re.compile(r"(?:以前|過去|上記|これまで|前の)[^。\n]{0,24}(?:指示|命令|会話|ルール)[^。\n]{0,16}(?:無視|忘れ|従わない)"),
        re.compile(r"(?:system|developer)\s+(?:prompt|message)[^\n]{0,40}(?:ignore|override|reveal|show)", re.I),
        re.compile(r"(?:システム|開発者)(?:プロンプト|メッセージ)[^。\n]{0,30}(?:無視|上書き|開示|表示)"),
    ),
    "secret_exfiltration": (
        re.compile(r"(?:reveal|print|show|expose)\s+(?:the\s+)?(?:system prompt|hidden instructions?|developer message)", re.I),
        re.compile(r"(?:システムプロンプト|隠された指示|内部命令)[^。\n]{0,24}(?:開示|表示|出力|教えて)"),
    ),
    "ai_role_assignment": (
        re.compile(r"あなたは[^。\n]{0,80}(?:ai|assistant|chatgpt|gemma|gpt|エージェント|アシスタント)" , re.I),
        re.compile(r"(?:system\s*prompt|gpts?\s*instructions?|custom\s*instructions?)", re.I),
        re.compile(r"(?:システムプロンプト|カスタムインストラクション|カスタム指示)"),
    ),
    "prompt_template": (
        re.compile(r"(?:この|以下の|下記の)プロンプト[^。\n]{0,40}(?:コピー|コピペ|利用|入力|受け取)"),
        re.compile(r"(?:起動|行動|システム)プロンプト"),
        re.compile(r"(?:エージェント向け|プロジェクトルールファイル)"),
        re.compile(r"(?:そのまま|以下を)[^。\n]{0,30}(?:コピペ|コピーして利用)"),
        re.compile(r"(?:full\s*prompt|prompt\s*(?:template|collection|library|cheat\s*sheet))", re.I),
        re.compile(r"(?:プロンプト集|プロンプトテンプレート|チートシート)"),
    ),
    "output_control": (
        re.compile(r"(?:最初|まず)[^。\n]{0,30}(?:出力|回答|質問)"),
        re.compile(r"(?:json|yaml|xml)[^。\n]{0,20}(?:だけ|のみ)[^。\n]{0,12}(?:出力|返答)" , re.I),
        re.compile(r"(?:only\s+(?:output|respond)|respond\s+only|output\s+only)", re.I),
        re.compile(r"(?:出力|回答)(?:して|せよ|しなさい|してください)"),
        re.compile(r"(?:生成|作成|記述|列挙|要約)(?:して|せよ|しなさい|してください)"),
    ),
    "instruction_structure": (
        re.compile(r"(?:^|\n)\s*(?:step|ステップ)\s*1\b", re.I),
        re.compile(r"(?:^|\n)\s*#{1,4}\s*(?:instructions?|命令|指示|rules?|ルール)\b", re.I),
        re.compile(r"(?:絶対遵守|厳守事項|必ず守って)"),
    ),
}

PATH_PROMPT_RE = re.compile(
    r"(?:プロンプト|prompt|cheat.?sheet|custom.?instruction|カスタムインストラクション|(?:^|/)(?:claude|agents?|skill)\.md$)", re.I
)


def matches_for(text: str) -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for code, patterns in PATTERNS.items():
        values: list[str] = []
        for pattern in patterns:
            for match in pattern.finditer(text):
                snippet = " ".join(match.group(0).split())[:160]
                if snippet and snippet not in values:
                    values.append(snippet)
                if len(values) == 4:
                    break
            if len(values) == 4:
                break
        if values:
            found[code] = values
    return found


def classify_text(text: str, relative_path: str) -> tuple[str, str, int, dict[str, list[str]]]:
    signals = matches_for(unicodedata.normalize("NFKC", text))
    if PATH_PROMPT_RE.search(unicodedata.normalize("NFKC", relative_path)):
        signals["prompt_path"] = [relative_path]
    codes = set(signals)
    if codes & {"priority_override", "secret_exfiltration"}:
        return "prompt_injection", "quarantine", 10, signals
    if "prompt_path" in codes or "prompt_template" in codes:
        return "ai_instruction", "prompt_library_only", 7, signals
    if "ai_role_assignment" in codes and codes & {"output_control", "instruction_structure"}:
        return "ai_instruction", "prompt_library_only", 7, signals
    if "ai_role_assignment" in codes:
        # A role-like sentence by itself is evidence, not executable control.
        # It becomes prompt-library material only when an instruction signal
        # occurs in the same local window.
        return "human_instruction", "answer_eligible", 3, signals
    if "instruction_structure" in codes or "output_control" in codes:
        return "human_instruction", "answer_eligible", 2, signals
    return "normal_content", "answer_eligible", 0, signals


def classify_document_chunks(chunks: list[str], relative_path: str) -> tuple[str, str, int, dict[str, list[str]]]:
    """Summarize local scans without combining unrelated document sections.

    Individual chunks and adjacent pairs are scanned. This preserves split
    injection detection while preventing signals from distant paragraphs from
    being synthesized into a document-wide false positive.
    """
    rank = {"answer_eligible": 0, "prompt_library_only": 1, "quarantine": 2}
    scans = [classify_text(chunk, relative_path) for chunk in chunks]
    scans.extend(
        classify_text(f"{left}\n{right}", relative_path)
        for left, right in zip(chunks, chunks[1:])
    )
    if not scans:
        return classify_text("", relative_path)
    role, disposition, score, _ = max(scans, key=lambda item: (rank[item[1]], item[2]))
    combined_signals: dict[str, list[str]] = {}
    for _, _, _, signals in scans:
        for code, snippets in signals.items():
            values = combined_signals.setdefault(code, [])
            for snippet in snippets:
                if snippet not in values and len(values) < 4:
                    values.append(snippet)
    return role, disposition, score, combined_signals


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def jsonl_bytes(records: list[dict]) -> bytes:
    return ("".join(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n" for item in records)).encode("utf-8")


def build(
    evidence_path: Path,
    documents_path: Path,
    output_dir: Path,
    *,
    created_at: str | None = None,
) -> dict:
    """Build the deterministic security partition for independent replay."""
    evidence_path = evidence_path.resolve(strict=True)
    documents_path = documents_path.resolve(strict=True)
    output_dir = output_dir.resolve(strict=True)
    evidence = [json.loads(line) for line in evidence_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    documents = [json.loads(line) for line in documents_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    document_by_id = {item["document_id"]: item for item in documents}
    if len(document_by_id) != len(documents):
        raise SystemExit("duplicate_document_id")

    local: list[dict] = []
    by_document: dict[str, list[dict]] = defaultdict(list)
    for record in evidence:
        document_id = record["document_id"]
        document = document_by_id.get(document_id)
        if document is None:
            raise SystemExit(f"missing_document:{document_id}")
        relative_path = document["source"]["relative_path"]
        text = str(record.get("observed_text", ""))
        role, disposition, score, signals = classify_text(text, relative_path)
        item = {
            "schema_version": SCHEMA_VERSION,
            "policy_version": POLICY_VERSION,
            "evidence_id": record["evidence_id"],
            "document_id": document_id,
            "source": record["source"],
            "locator": record.get("locator", {}),
            "observed_text_sha256": sha256_text(text),
            "content_role": role,
            "local_disposition": disposition,
            "risk_score": score,
            "risk_signals": signals,
            "trust": "untrusted",
            "execution_policy": "never_execute",
            "classifier": "deterministic_content_security_gate",
        }
        local.append(item)
        by_document[document_id].append(item)

    document_results: list[dict] = []
    final_disposition: dict[str, str] = {}
    rank = {"answer_eligible": 0, "prompt_library_only": 1, "quarantine": 2}
    for document_id in sorted(by_document, key=lambda value: document_by_id[value]["source"]["relative_path"]):
        items = by_document[document_id]
        relative_path = document_by_id[document_id]["source"]["relative_path"]
        source_texts = [str(source_record.get("observed_text", "")) for source_record in evidence if source_record["document_id"] == document_id]

        # Escalate only the chunks participating in a risky adjacent window.
        # Never apply one chunk's result to every Evidence item in the file.
        for index in range(len(items) - 1):
            window_role, window_disposition, window_score, window_signals = classify_text(
                f"{source_texts[index]}\n{source_texts[index + 1]}", relative_path
            )
            local_window_rank = max(
                rank[items[index]["local_disposition"]],
                rank[items[index + 1]["local_disposition"]],
            )
            if rank[window_disposition] <= local_window_rank:
                continue
            for item in (items[index], items[index + 1]):
                if rank[window_disposition] > rank[item["local_disposition"]]:
                    item["content_role"] = window_role
                    item["effective_disposition"] = window_disposition
                    item["risk_score"] = max(item["risk_score"], window_score)
                    item["adjacent_window_risk_signals"] = window_signals
                else:
                    item.setdefault("effective_disposition", item["local_disposition"])
        for item in items:
            item.setdefault("effective_disposition", item["local_disposition"])

        document_role, document_scan_disposition, document_score, document_signals = classify_document_chunks(source_texts, relative_path)
        hard_injection_reasons = {
            code for code in document_signals
            if code in {"priority_override", "secret_exfiltration"}
        }
        if hard_injection_reasons:
            # High-confidence prompt injection taints derivative packets from
            # the same source (for example spreadsheet relations or PDF layout
            # packets), even when those packets do not repeat the attack text.
            # Lower-risk prompt-library material remains chunk-partitioned.
            inherited_signals = {
                code: document_signals[code] for code in sorted(hard_injection_reasons)
            }
            for item in items:
                item["content_role"] = "prompt_injection"
                item["effective_disposition"] = "quarantine"
                item["risk_score"] = max(item["risk_score"], document_score)
                item["inherited_document_risk_signals"] = inherited_signals
        disposition = max(
            [document_scan_disposition, *(item["effective_disposition"] for item in items)],
            key=rank.__getitem__,
        )
        final_disposition[document_id] = disposition
        roles = Counter(item["content_role"] for item in items)
        reasons = sorted({code for item in items for code in item["risk_signals"]} | set(document_signals))
        document_results.append({
            "schema_version": SCHEMA_VERSION,
            "policy_version": POLICY_VERSION,
            "document_id": document_id,
            "source": document_by_id[document_id]["source"],
            "disposition": disposition,
            "content_role_counts": dict(sorted(roles.items())),
            "risk_reasons": reasons,
            "document_scan_content_role": document_role,
            "document_scan_risk_signals": document_signals,
            "max_risk_score": max(document_score, *(item["risk_score"] for item in items)),
            "evidence_count": len(items),
            "evidence_dispositions": dict(sorted(Counter(item["effective_disposition"] for item in items).items())),
            "partially_excluded": any(item["effective_disposition"] == "answer_eligible" for item in items)
                and any(item["effective_disposition"] != "answer_eligible" for item in items),
            "trust": "untrusted",
            "execution_policy": "never_execute",
        })

    classifications = []
    streams: dict[str, list[dict]] = {key: [] for key in rank}
    if len(evidence) != len(local):
        raise SystemExit("classification_count_mismatch")
    # Keep Python 3.9 compatibility; the explicit length check above provides
    # the same invariant as ``zip(..., strict=True)``.
    for source_record, item in zip(evidence, local):
        item = dict(item)
        item["document_disposition"] = final_disposition[item["document_id"]]
        classifications.append(item)
        streams[item["effective_disposition"]].append(source_record)

    exclusions = [
        {
            "schema_version": SCHEMA_VERSION,
            "policy_version": POLICY_VERSION,
            "evidence_id": item["evidence_id"],
            "document_id": item["document_id"],
            "source": item["source"],
            "locator": item["locator"],
            "disposition": item["effective_disposition"],
            "content_role": item["content_role"],
            "risk_reasons": sorted(
                set(item["risk_signals"])
                | set(item.get("adjacent_window_risk_signals", {}))
                | set(item.get("inherited_document_risk_signals", {}))
            ),
        }
        for item in classifications
        if item["effective_disposition"] != "answer_eligible"
    ]

    outputs = {
        "content-security-classifications.jsonl": jsonl_bytes(classifications),
        "content-security-documents.jsonl": jsonl_bytes(document_results),
        "safe-answer-evidence.jsonl": jsonl_bytes(streams["answer_eligible"]),
        "prompt-library-evidence.jsonl": jsonl_bytes(streams["prompt_library_only"]),
        "quarantine-evidence.jsonl": jsonl_bytes(streams["quarantine"]),
        "content-security-exclusions.jsonl": jsonl_bytes(exclusions),
    }
    for name, data in outputs.items():
        atomic_write(output_dir / name, data)

    state = {
        "schema_version": SCHEMA_VERSION,
        "policy_version": POLICY_VERSION,
        "created_at": created_at or datetime.now().astimezone().isoformat(timespec="seconds"),
        "classifier": "deterministic_content_security_gate",
        "question_independent": True,
        "llm_used_for_classification": False,
        "all_source_content_trust": "untrusted",
        "execution_policy": "never_execute",
        "source_evidence": {"path": str(evidence_path), "sha256": sha256_file(evidence_path), "count": len(evidence)},
        "source_documents": {"path": str(documents_path), "sha256": sha256_file(documents_path), "count": len(documents)},
        "counts": {
            "classifications": len(classifications),
            "documents": len(document_results),
            "safe_answer_evidence": len(streams["answer_eligible"]),
            "prompt_library_evidence": len(streams["prompt_library_only"]),
            "quarantine_evidence": len(streams["quarantine"]),
            "excluded_evidence": len(exclusions),
            "partially_excluded_documents": sum(item["partially_excluded"] for item in document_results),
            "document_dispositions": dict(sorted(Counter(final_disposition.values()).items())),
        },
        "outputs": {
            name: {"sha256": hashlib.sha256(data).hexdigest(), "size_bytes": len(data)}
            for name, data in sorted(outputs.items())
        },
        "safe_answer_index_allowed": True,
        "prompt_library_requires_explicit_mode": True,
        "quarantine_index_allowed": False,
    }
    atomic_write(output_dir / "content-security-state.json", (json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8"))
    return state


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence", required=True)
    parser.add_argument("--documents", required=True)
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()
    state = build(
        Path(args.evidence), Path(args.documents), Path(args.output_dir),
    )
    print(json.dumps(state["counts"], ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
