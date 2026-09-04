#!/usr/bin/env python3
"""Localhost-only web UI for the packaged local-memory system."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import unicodedata
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import bootstrap


BUILD_LOCK = threading.Lock()
BASE = Path(__file__).resolve().parent
ENGINE = BASE / "engine"
OLLAMA_GENERATE = "http://127.0.0.1:11434/api/generate"
SEMANTIC_GRAPH_CANDIDATE_KEY = (
    "cross_document_semantic_graph_query_candidate"
)
SEMANTIC_GRAPH_EDGE_AUDIT_KEY = (
    "cross_document_semantic_graph_independent_edge_audit"
)
SEMANTIC_GRAPH_CANDIDATE_TIMEOUT_SECONDS = 30.0
SEMANTIC_GRAPH_EDGE_AUDIT_TIMEOUT_SECONDS = 30.0
SEMANTIC_GRAPH_RUN_PREFIX = "xkgr_"
SEMANTIC_GRAPH_REGISTRATION_FIELDS = frozenset({
    "schema_version",
    "status",
    "generation",
    "database_path",
    "database_sha256",
    "state_path",
    "state_sha256",
    "base_index_path",
    "base_index_sha256",
    "graph_snapshot_id",
    "logical_snapshot_sha256",
    "counts",
    "retrieval_enabled",
    "used_for_answers",
})
SEMANTIC_GRAPH_CANDIDATE_FIELDS = frozenset({
    "schema_version",
    "record_type",
    "adapter",
    "adapter_version",
    "status",
    "decision",
    "reason_code",
    "diagnostic_code",
    "operation",
    "answer_text",
    "asserted_facts",
    "asserted_relations",
    "trace",
    "runtime_attestation",
    "used_for_answers",
    "independent_edge_audit_status",
})
GENERATION_PATTERN = re.compile(r"generation-[0-9a-f]{32}")
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
SEMANTIC_GRAPH_OPERATIONS = frozenset({
    "owner", "assignment_change", "version_change",
})
SEMANTIC_GRAPH_OPERATION_FACT_FIELDS = {
    "owner": frozenset({
        "reference_time", "role", "assignee_id", "assignee_name",
    }),
    "assignment_change": frozenset({
        "change_effective_date", "previous_valid_to",
        "from_assignee_id", "from_assignee_name",
        "to_assignee_id", "to_assignee_name",
    }),
    "version_change": frozenset({
        "effective_from", "old_plan_status", "old_plan_assignee_id",
        "old_plan_assignee_name", "current_plan_status",
        "current_plan_assignee_id", "current_plan_assignee_name",
        "change_reason",
    }),
}
SEMANTIC_GRAPH_OPERATION_RELATION_TYPES = {
    "owner": frozenset(),
    "assignment_change": frozenset(),
    "version_change": frozenset({"SUPERSEDES", "CONTRADICTS"}),
}
SEMANTIC_GRAPH_ATTESTATION_FIELDS = frozenset({
    "adapter", "adapter_version", "read_only", "read_snapshot",
    "generation", "build_id", "index_sha256", "graph_snapshot_id",
    "logical_snapshot_sha256", "projection_sha256", "node_count",
    "edge_count", "edge_evidence_count", "eligible_evidence_count",
    "outbound_network_attempt_count",
})
SEMANTIC_GRAPH_EDGE_AUDIT_FIELDS = frozenset({
    "schema_version", "record_type", "auditor", "auditor_version",
    "status", "verdict", "reason_code", "diagnostic_code", "operation",
    "candidate_sha256", "registration_sha256", "question_sha256",
    "question_reference_date", "graph_snapshot_id",
    "reconstructed_semantics_sha256", "checks", "audit_attestation",
    "used_for_answers", "allows_answer_activation",
})
SEMANTIC_GRAPH_EDGE_AUDIT_CHECK_FIELDS = frozenset({
    "candidate_contract", "question_classification",
    "registered_storage_integrity", "independent_graph_reconstruction",
    "candidate_semantics",
})
SEMANTIC_GRAPH_EDGE_AUDIT_ATTESTATION_FIELDS = frozenset({
    "read_only", "read_snapshot", "database_opened", "generation",
    "index_sha256", "graph_snapshot_id", "logical_snapshot_sha256",
    "projection_sha256", "node_count", "edge_count",
    "edge_evidence_count", "eligible_evidence_count",
    "outbound_network_attempt_count",
})


STYLE = """
:root{font-family:-apple-system,BlinkMacSystemFont,'Hiragino Sans',sans-serif;color:#0b1f33;background:#f5f9fd}
*{box-sizing:border-box}body{margin:0}.wrap{max-width:980px;margin:0 auto;padding:54px 28px 90px}
.eyebrow{color:#2775b6;font-weight:700;letter-spacing:.12em;font-size:12px}.hero{font-size:42px;line-height:1.15;margin:12px 0;color:#071827}
.sub{color:#557086;font-size:16px;line-height:1.8;max-width:720px}.card{background:white;border:1px solid #dce9f4;border-radius:22px;padding:28px;margin-top:24px;box-shadow:0 12px 36px rgba(45,91,125,.08)}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:12px}.metric{padding:16px;background:#f6faff;border-radius:14px}.metric b{display:block;font-size:18px;margin-top:7px}
textarea{width:100%;min-height:120px;border:1px solid #bfd7ea;border-radius:14px;padding:15px;font:inherit;resize:vertical}button,.button{display:inline-block;border:0;border-radius:999px;background:#1c72b8;color:#fff;padding:12px 20px;font-weight:700;text-decoration:none;cursor:pointer;margin-top:12px}.secondary{background:#e8f3fb;color:#175787}
.answer{white-space:pre-wrap;line-height:1.8;background:#f8fbfe;border-left:4px solid #65aee4;padding:18px;border-radius:10px}.warn{color:#8b4d00;background:#fff6e8;padding:12px;border-radius:10px}.ok{color:#17603a}.bad{color:#9b2c2c}.small{font-size:13px;color:#667b8d}.progress{animation:pulse 1.4s infinite}@keyframes pulse{50%{opacity:.45}}
code{background:#edf5fb;padding:2px 6px;border-radius:5px}details{margin-top:16px}
"""


def page(body: str, refresh: int | None = None) -> bytes:
    meta = f'<meta http-equiv="refresh" content="{refresh}">' if refresh else ""
    value = f"""<!doctype html><html lang="ja"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">{meta}<title>Local Memory Search</title><style>{STYLE}</style></head><body><main class="wrap">{body}</main></body></html>"""
    return value.encode("utf-8")


def state() -> dict:
    return bootstrap.load_json(bootstrap.STATE, {"phase": "not_started", "message": "まだ索引は作成されていません。", "error": ""})


def security_exclusion_notice() -> str:
    """Render transparent, non-sensitive information about gated Evidence."""
    config = bootstrap.load_json(bootstrap.CONFIG)
    workspace = Path(config.get("workspace", bootstrap.SUPPORT / "data"))
    security = Path(config.get("security_path", workspace / "03-security"))
    state_path = security / "content-security-state.json"
    exclusions_path = security / "content-security-exclusions.jsonl"
    if not state_path.is_file() or not exclusions_path.is_file():
        return ""
    try:
        security_state = json.loads(state_path.read_text(encoding="utf-8"))
        exclusions = [
            json.loads(line) for line in exclusions_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    except (OSError, ValueError, TypeError):
        return '<p class="warn">安全性判定の詳細を読み込めませんでした。</p>'
    expected = int(security_state.get("counts", {}).get("excluded_evidence", 0))
    if expected != len(exclusions):
        return '<p class="warn">安全性判定の件数が一致しないため、除外一覧を表示できません。</p>'
    if not exclusions:
        return ""
    grouped: dict[tuple[str, str], int] = {}
    for item in exclusions:
        relative_path = str(item.get("source", {}).get("relative_path", "(不明)"))
        key = (relative_path, str(item.get("disposition", "unknown")))
        grouped[key] = grouped.get(key, 0) + 1
    rows = "".join(
        f"<li>{html.escape(path)} — {html.escape(disposition)} ({count}箇所)</li>"
        for (path, disposition), count in sorted(grouped.items())
    )
    return (
        f'<details class="card"><summary>安全のため {len(exclusions)} 箇所の証拠を回答索引から除外しました</summary>'
        f'<p class="small">判定に応じて、プロンプ資料の該当箇所、または高確度の攻撃文を含む資料を除外しています。</p><ul>{rows}</ul></details>'
    )


def home(message: str = "") -> bytes:
    diagnosis = bootstrap.diagnose()
    current = state()
    ready = diagnosis["index_ready"] and current.get("phase") in {"ready", "ready_with_limits"}
    models = " / ".join(diagnosis["models"]) or "未確認"
    notices = "".join(f'<p class="warn">{html.escape(item)}</p>' for item in diagnosis["warnings"])
    transient = f'<p class="ok">{html.escape(message)}</p>' if message else ""
    setup = ""
    if current["phase"] == "building":
        setup = '<p class="progress">索引を作成中です。ファイル数と初回モデル取得により時間がかかります。この画面は自動更新します。</p>'
    elif current["phase"] == "error":
        setup = f'<p class="bad">{html.escape(current["message"])}<br><span class="small">{html.escape(current.get("error", ""))}</span></p><form method="post" action="/build"><button>再実行</button></form>'
    elif not ready:
        setup = '<p>初回だけ、ローカルモデルの確認と索引作成を行います。このボタンで、不足モデルがある場合の公式Ollama経由の取得を開始します。ファイルは外部へ送信しません。</p><form method="post" action="/build"><button>初回セットアップを開始</button></form>'
    elif current["phase"] == "ready_with_limits":
        limitations = html.escape(json.dumps(current.get("reader_limitations", {}), ensure_ascii=False, sort_keys=True))
        setup = f'<p class="warn">{html.escape(current["message"])}<br><span class="small">{limitations}</span></p>'
    else:
        setup = '<p class="ok">準備完了。曖昧な記憶のまま質問できます。</p>'
    ask = "" if not ready else """
    <section class="card"><div class="eyebrow">ASK YOUR MEMORY</div><h2>パソコンの中に質問する</h2>
    <form method="post" action="/ask"><textarea name="query" required placeholder="例：あの頃、AIの講演で何を話したっけ？"></textarea><br><button>根拠を探して答える</button></form></section>
    """
    refresh = 4 if current["phase"] == "building" else None
    return page(f"""
    <div class="eyebrow">PRIVATE / LOCAL / EVIDENCE-BASED</div><h1 class="hero">あなたのMacを、<br>曖昧な記憶から探す。</h1>
    <p class="sub">Word・Excel・PowerPoint・PDF・テキストなどの所在と内容をローカルで索引化。回答は根拠と別モデルの監査を通し、判断できない場合は理由付きで「わかりません」と停止します。</p>
    {transient}{notices}<section class="card"><div class="eyebrow">SYSTEM STATUS</div><h2>現在の状態</h2><div class="grid">
    <div class="metric">メモリ<b>{diagnosis['memory_gb'] or '?'} GB</b></div><div class="metric">空き容量<b>{diagnosis['free_gb']} GB</b></div>
    <div class="metric">チップ<b>{html.escape(diagnosis['architecture'])}</b></div><div class="metric">Ollama<b>{'起動中' if diagnosis['ollama_online'] else '停止中/未導入'}</b></div></div>
    <p class="small">検索対象: {html.escape(diagnosis['source_root'] or '未選択')}<br>モデル: {html.escape(models)}</p>{setup}</section>{ask}
    {security_exclusion_notice()}
    <section class="card"><details><summary>プライバシーと制限</summary><p class="small">質問・回答・索引は <code>~/Library/Application Support/LocalMemorySearch</code> に保存されます。通常利用中のAI処理は127.0.0.1のOllamaのみです。初回のOllama導入・モデル取得にはインターネットが必要です。画像はまずローカルOCRで位置付き文字を取り出し、位置付き結果が空の場合だけGemmaの座標なし文字起こしを暫定情報として残します。音声・動画は未対応です。</p></details></section>
    """, refresh=refresh)


def build_worker() -> None:
    if not BUILD_LOCK.acquire(blocking=False):
        return
    try:
        bootstrap.build_index()
    except Exception:
        pass
    finally:
        BUILD_LOCK.release()


def unload_ollama_model(model: str, timeout: int = 60) -> dict:
    """Ask local Ollama to release one model and report the switching cost."""
    started = time.perf_counter()
    request = urllib.request.Request(
        OLLAMA_GENERATE,
        data=json.dumps({"model": model, "keep_alive": 0, "stream": False}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            response.read()
        return {"requested": True, "succeeded": True, "seconds": round(time.perf_counter() - started, 3), "error": ""}
    except Exception as exc:
        return {
            "requested": True,
            "succeeded": False,
            "seconds": round(time.perf_counter() - started, 3),
            "error": f"{type(exc).__name__}: {exc}",
        }


def semantic_graph_candidate_eligibility(
    config: dict,
    index: Path,
) -> tuple[bool, str]:
    """Allow the Step 3 observer only on its validated Step 2 index copy."""
    if (
        config.get(bootstrap.CROSS_DOCUMENT_QUERY_CANDIDATE_FLAG, True)
        is not True
    ):
        return False, "feature_disabled"
    if config.get(bootstrap.CROSS_DOCUMENT_STORAGE_FLAG, True) is not True:
        return False, "semantic_storage_disabled"
    registration = config.get(bootstrap.CROSS_DOCUMENT_STORAGE_CONFIG_KEY)
    if not isinstance(registration, dict):
        return False, "validated_storage_registration_missing"
    if set(registration) != SEMANTIC_GRAPH_REGISTRATION_FIELDS:
        return False, "validated_storage_registration_fields_invalid"
    if (
        registration.get("schema_version") != "0.1"
        or registration.get("status") != "validated_storage_only"
    ):
        return False, "validated_storage_status_missing"
    if (
        registration.get("retrieval_enabled") is not False
        or registration.get("used_for_answers") is not False
    ):
        return False, "step2_storage_boundary_invalid"
    active_generation = config.get("active_generation")
    if (
        not isinstance(active_generation, str)
        or GENERATION_PATTERN.fullmatch(active_generation) is None
        or registration.get("generation") != active_generation
    ):
        return False, "storage_generation_mismatch"
    workspace = Path(config.get("workspace", bootstrap.SUPPORT / "data"))
    generation = workspace / "generations" / active_generation
    expected_index = (
        generation
        / bootstrap.CROSS_DOCUMENT_STORAGE_DIR
        / "safe-answer-index.sqlite3"
    )
    expected_state = (
        expected_index.parent
        / bootstrap.CROSS_DOCUMENT_STORAGE_RUN_STATE
    )
    expected_base = generation / "safe-answer-index.sqlite3"
    required_paths = {
        "database_path": expected_index,
        "state_path": expected_state,
        "base_index_path": expected_base,
    }
    if any(
        not isinstance(registration.get(key), str)
        or Path(registration[key]) != expected
        for key, expected in required_paths.items()
    ):
        return False, "storage_registered_path_mismatch"
    if any(
        not isinstance(registration.get(key), str)
        or SHA256_PATTERN.fullmatch(registration[key]) is None
        for key in (
            "database_sha256", "state_sha256", "base_index_sha256",
            "logical_snapshot_sha256",
        )
    ):
        return False, "storage_registered_hash_invalid"
    logical_sha256 = registration["logical_snapshot_sha256"]
    if registration.get("graph_snapshot_id") != (
        "xkgs_" + logical_sha256[:32]
    ):
        return False, "storage_graph_snapshot_binding_invalid"
    counts = registration.get("counts")
    if (
        not isinstance(counts, dict)
        or set(counts) != {"nodes", "edges", "edge_evidence"}
        or any(type(value) is not int or value < 1 for value in counts.values())
    ):
        return False, "storage_registered_counts_invalid"
    registered_index = Path(registration["database_path"])
    if index != registered_index or index != expected_index:
        return False, "storage_index_pointer_mismatch"
    return True, "validated_storage_candidate_enabled"


def _empty_candidate_trace(
    decision: str,
    reference_date: str | None = None,
) -> dict:
    return {
        "graph_snapshot_id": None,
        "question_reference_date": reference_date,
        "visited_node_ids": [],
        "visited_node_hashes": [],
        "visited_edge_ids": [],
        "visited_edge_hashes": [],
        "used_semantic_edge_ids": [],
        "used_semantic_edge_count": 0,
        "used_edge_statuses": [],
        "visited_document_paths": [],
        "resolved_source_references": [],
        "disabled_edge_ids": [],
        "decision": decision,
        "outbound_network_attempt_count": 0,
        "database_opened": False,
    }


def _held_candidate(
    diagnostic_code: str,
    reference_date: str | None = None,
) -> dict:
    """Return safe observer telemetry without changing the audited answer."""
    return {
        "schema_version": "0.1",
        "record_type": SEMANTIC_GRAPH_CANDIDATE_KEY,
        "adapter": "cross-document-semantic-graph-runtime",
        "adapter_version": "0.1.0",
        "status": "held",
        "decision": "HOLD",
        "reason_code": "semantic_graph_candidate_observer_failed",
        "diagnostic_code": diagnostic_code,
        "operation": None,
        "answer_text": "",
        "asserted_facts": [],
        "asserted_relations": [],
        "trace": _empty_candidate_trace("HOLD", reference_date),
        "runtime_attestation": None,
        "used_for_answers": False,
        "independent_edge_audit_status": "not_implemented_step4",
    }


def _strict_candidate_json(payload: str) -> object:
    def object_pairs(pairs: list[tuple[str, object]]) -> dict:
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("semantic_candidate_duplicate_json_key")
            result[key] = value
        return result

    def reject_constant(_value: str) -> None:
        raise ValueError("semantic_candidate_non_finite_json_number")

    return json.loads(
        payload,
        object_pairs_hook=object_pairs,
        parse_constant=reject_constant,
    )


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _canonical_sha256(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _deterministic_candidate_semantics(candidate: dict) -> dict:
    """Project only deterministic Step 3 fields for Step 4 hash equality."""
    trace = candidate.get("trace")
    if not isinstance(trace, dict):
        raise ValueError("semantic_candidate_trace_invalid")
    deterministic_trace = {
        key: value
        for key, value in trace.items()
        if key not in {"elapsed_ms", "peak_rss_bytes"}
    }
    return {
        key: deterministic_trace if key == "trace" else candidate[key]
        for key in SEMANTIC_GRAPH_CANDIDATE_FIELDS
    }


def _strict_candidate_reference_date(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("semantic_candidate_reference_date_invalid")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(
            "semantic_candidate_reference_date_invalid"
        ) from exc
    if parsed.isoformat() != value:
        raise ValueError("semantic_candidate_reference_date_invalid")
    return value


def _record_reference_date(record: object) -> tuple[bool, str | None]:
    """Read an optional legacy anchor while rejecting malformed/mutated values."""
    if not isinstance(record, dict):
        return False, None
    if "question_reference_date" not in record:
        return True, None
    try:
        return True, _strict_candidate_reference_date(
            record["question_reference_date"]
        )
    except ValueError:
        return False, None


def _candidate_result_is_safe(
    candidate: object,
    registration: dict,
    query: str,
    reference_date: str | None = None,
) -> bool:
    if not isinstance(query, str) or not query.strip():
        return False
    if not isinstance(candidate, dict) or set(candidate) != (
        SEMANTIC_GRAPH_CANDIDATE_FIELDS
    ):
        return False
    status = candidate.get("status")
    decision = candidate.get("decision")
    expected_decisions = {
        "accepted": "ACCEPTED",
        "held": "HOLD",
        "not_applicable": "NOT_APPLICABLE",
    }
    trace = candidate.get("trace")
    if (
        expected_decisions.get(status) != decision
        or candidate.get("schema_version") != "0.1"
        or candidate.get("record_type") != SEMANTIC_GRAPH_CANDIDATE_KEY
        or candidate.get("adapter")
        != "cross-document-semantic-graph-runtime"
        or candidate.get("adapter_version") != "0.1.0"
        or candidate.get("used_for_answers") is not False
        or candidate.get("independent_edge_audit_status")
        != "not_implemented_step4"
        or not isinstance(candidate.get("answer_text"), str)
        or not isinstance(candidate.get("asserted_facts"), list)
        or not isinstance(candidate.get("asserted_relations"), list)
        or not isinstance(trace, dict)
    ):
        return False
    used_edge_count = trace.get("used_semantic_edge_count")
    database_opened = trace.get("database_opened")
    if (
        type(used_edge_count) is not int
        or used_edge_count < 0
        or type(database_opened) is not bool
        or "question_reference_date" not in trace
        or trace.get("question_reference_date") != reference_date
    ):
        return False
    list_fields = (
        "visited_node_ids", "visited_node_hashes",
        "visited_edge_ids", "visited_edge_hashes",
        "used_semantic_edge_ids", "used_edge_statuses",
        "visited_document_paths", "resolved_source_references",
        "disabled_edge_ids",
    )
    if any(not isinstance(trace.get(key), list) for key in list_fields):
        return False
    used_edge_ids = trace["used_semantic_edge_ids"]
    if (
        len(used_edge_ids) != used_edge_count
        or len(used_edge_ids) != len(set(used_edge_ids))
        or any(
            not isinstance(value, str) or not value.strip()
            for value in used_edge_ids
        )
        or used_edge_count > registration["counts"]["edges"]
        or trace.get("visited_edge_ids") != used_edge_ids
        or len(trace["visited_edge_hashes"]) != len(used_edge_ids)
        or any(
            SHA256_PATTERN.fullmatch(value) is None
            for value in trace["visited_edge_hashes"]
            if isinstance(value, str)
        )
        or any(
            not isinstance(value, str)
            for value in trace["visited_edge_hashes"]
        )
        or len(trace["visited_node_ids"])
        != len(trace["visited_node_hashes"])
        or len(trace["visited_node_ids"])
        != len(set(trace["visited_node_ids"]))
        or any(
            not isinstance(value, str) or not value.strip()
            for value in trace["visited_node_ids"]
        )
        or len(trace["visited_node_ids"])
        > registration["counts"]["nodes"]
        or any(
            not isinstance(value, str)
            or SHA256_PATTERN.fullmatch(value) is None
            for value in trace["visited_node_hashes"]
        )
        or trace.get("used_edge_statuses")
        != (["verified"] if used_edge_ids else [])
        or trace.get("decision") != decision
        or trace.get("outbound_network_attempt_count") != 0
        or any(
            not isinstance(reference, dict)
            for reference in trace["resolved_source_references"]
        )
    ):
        return False

    reference_fields = {
        "edge_id", "evidence_id", "document_id", "path", "source_sha256",
        "locator", "observed_text_sha256", "quote",
    }
    reference_pairs: set[tuple[str, str]] = set()
    referenced_edges: set[str] = set()
    reference_paths: set[str] = set()
    referenced_evidence: set[str] = set()
    for reference in trace["resolved_source_references"]:
        if set(reference) != reference_fields:
            return False
        edge_id = reference["edge_id"]
        evidence_id = reference["evidence_id"]
        document_id = reference["document_id"]
        path = reference["path"]
        quote = reference["quote"]
        pair = (edge_id, evidence_id)
        if (
            not isinstance(edge_id, str)
            or edge_id not in used_edge_ids
            or not isinstance(evidence_id, str)
            or not evidence_id.strip()
            or not isinstance(document_id, str)
            or not document_id.strip()
            or not isinstance(path, str)
            or not path.strip()
            or path.startswith("/")
            or ".." in Path(path).parts
            or not isinstance(reference["locator"], dict)
            or not isinstance(quote, str)
            or not quote.strip()
            or not isinstance(reference["source_sha256"], str)
            or SHA256_PATTERN.fullmatch(reference["source_sha256"]) is None
            or not isinstance(reference["observed_text_sha256"], str)
            or SHA256_PATTERN.fullmatch(
                reference["observed_text_sha256"]
            ) is None
            or hashlib.sha256(quote.encode("utf-8")).hexdigest()
            != reference["observed_text_sha256"]
            or pair in reference_pairs
        ):
            return False
        reference_pairs.add(pair)
        referenced_edges.add(edge_id)
        referenced_evidence.add(evidence_id)
        reference_paths.add(path)
    if (
        referenced_edges != set(used_edge_ids)
        or trace["visited_document_paths"] != sorted(reference_paths)
        or len(trace["visited_document_paths"])
        != len(set(trace["visited_document_paths"]))
    ):
        return False

    fact_fields: set[str] = set()
    for item in candidate["asserted_facts"]:
        if not isinstance(item, dict) or set(item) != {
            "field", "value", "proof_edge_ids",
        }:
            return False
        field = item["field"]
        value = item["value"]
        proof = item["proof_edge_ids"]
        if (
            not isinstance(field, str)
            or not field.strip()
            or field in fact_fields
            or not isinstance(value, str)
            or not value.strip()
            or not isinstance(proof, list)
            or not proof
            or len(proof) != len(set(proof))
            or any(
                not isinstance(edge_id, str) or edge_id not in used_edge_ids
                for edge_id in proof
            )
        ):
            return False
        fact_fields.add(field)

    relation_tuples: set[tuple[str, str, str]] = set()
    for item in candidate["asserted_relations"]:
        if not isinstance(item, dict) or set(item) != {
            "from", "relation", "to", "proof_edge_ids",
        }:
            return False
        asserted_tuple = (item["from"], item["relation"], item["to"])
        proof = item["proof_edge_ids"]
        if (
            any(
                not isinstance(value, str) or not value.strip()
                for value in asserted_tuple
            )
            or asserted_tuple in relation_tuples
            or not isinstance(proof, list)
            or not proof
            or len(proof) != len(set(proof))
            or any(
                not isinstance(edge_id, str) or edge_id not in used_edge_ids
                for edge_id in proof
            )
        ):
            return False
        relation_tuples.add(asserted_tuple)
    operation = candidate.get("operation")
    if status == "accepted" and (
        used_edge_count < 1
        or database_opened is not True
        or not candidate["asserted_facts"]
        or operation not in SEMANTIC_GRAPH_OPERATIONS
        or fact_fields != SEMANTIC_GRAPH_OPERATION_FACT_FIELDS.get(operation)
        or {item[1] for item in relation_tuples}
        != SEMANTIC_GRAPH_OPERATION_RELATION_TYPES.get(operation)
        or candidate.get("reason_code") is not None
        or candidate.get("diagnostic_code") is not None
        or not candidate["answer_text"].strip()
        or not trace["resolved_source_references"]
    ):
        return False
    if status != "accepted" and (
        candidate["asserted_facts"] or candidate["asserted_relations"]
    ):
        return False
    if status == "not_applicable" and database_opened is not False:
        return False
    attestation = candidate.get("runtime_attestation")
    if database_opened:
        counts = registration["counts"]
        question_hash = hashlib.sha256(
            unicodedata.normalize("NFC", query).strip().encode("utf-8")
        ).hexdigest()
        run_identity = {
            "graph_snapshot_id": registration["graph_snapshot_id"],
            "question_hash": question_hash,
            "disabled_edge_ids": [],
            **(
                {"question_reference_date": reference_date}
                if reference_date is not None else {}
            ),
        }
        expected_run_id = SEMANTIC_GRAPH_RUN_PREFIX + hashlib.sha256(
            json.dumps(
                run_identity,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        ).hexdigest()[:32]
        expected_attestation = {
            "adapter": "cross-document-semantic-graph-runtime",
            "adapter_version": "0.1.0",
            "read_only": True,
            "read_snapshot": "single_sqlite_transaction",
            "generation": registration["generation"],
            "index_sha256": registration["database_sha256"],
            "graph_snapshot_id": registration["graph_snapshot_id"],
            "logical_snapshot_sha256": registration[
                "logical_snapshot_sha256"
            ],
            "node_count": counts["nodes"],
            "edge_count": counts["edges"],
            "edge_evidence_count": counts["edge_evidence"],
            "outbound_network_attempt_count": 0,
        }
        if (
            not isinstance(attestation, dict)
            or set(attestation) != SEMANTIC_GRAPH_ATTESTATION_FIELDS
            or any(
                attestation.get(key) != value
                for key, value in expected_attestation.items()
            )
            or not isinstance(attestation.get("build_id"), str)
            or not attestation["build_id"].strip()
            or not isinstance(attestation.get("eligible_evidence_count"), int)
            or isinstance(attestation["eligible_evidence_count"], bool)
            or attestation["eligible_evidence_count"] < 1
            or attestation["eligible_evidence_count"]
            < len(referenced_evidence)
            or not isinstance(attestation.get("projection_sha256"), str)
            or SHA256_PATTERN.fullmatch(
                attestation["projection_sha256"]
            ) is None
            or trace.get("graph_snapshot_id")
            != registration["graph_snapshot_id"]
            or trace.get("question_hash") != question_hash
            or trace.get("run_id") != expected_run_id
            or trace.get("disabled_edge_ids") != []
            or len(reference_pairs) > counts["edge_evidence"]
        ):
            return False
    elif attestation is not None or used_edge_ids:
        return False
    return True


def run_semantic_graph_candidate(
    query: str,
    config: dict,
    index: Path,
    reference_date: str | None = None,
) -> tuple[dict | None, dict]:
    """Run the observer after final audit in a bounded separate process."""
    enabled, reason = semantic_graph_candidate_eligibility(config, index)
    if not enabled:
        return None, {
            "enabled": False,
            "eligibility_reason": reason,
            "seconds": 0.0,
            "timed_out": False,
        }
    try:
        reference_date = _strict_candidate_reference_date(reference_date)
    except ValueError:
        candidate = _held_candidate(
            "semantic_candidate_reference_date_invalid"
        )
        return candidate, {
            "enabled": True,
            "eligibility_reason": reason,
            "seconds": 0.0,
            "timeout_seconds": SEMANTIC_GRAPH_CANDIDATE_TIMEOUT_SECONDS,
            "timed_out": False,
            "status": "held",
        }
    configured_timeout = config.get(
        "cross_document_semantic_graph_query_candidate_timeout_seconds",
        SEMANTIC_GRAPH_CANDIDATE_TIMEOUT_SECONDS,
    )
    timeout_seconds = (
        float(configured_timeout)
        if type(configured_timeout) in {int, float}
        and 1 <= configured_timeout <= 120
        else SEMANTIC_GRAPH_CANDIDATE_TIMEOUT_SECONDS
    )
    registration = config[bootstrap.CROSS_DOCUMENT_STORAGE_CONFIG_KEY]
    command = [
        sys.executable,
        str(ENGINE / "cross_document_semantic_graph_runtime.py"),
        query,
        "--index",
        str(index),
        "--registration-json",
        json.dumps(
            registration,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
    ]
    if reference_date is not None:
        command.extend(("--reference-date", reference_date))
    started = time.perf_counter()
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=True,
            close_fds=True,
        )
        candidate = _strict_candidate_json(completed.stdout)
        if not _candidate_result_is_safe(
            candidate, registration, query, reference_date
        ):
            candidate = _held_candidate(
                "semantic_candidate_result_contract_invalid",
                reference_date,
            )
        return candidate, {
            "enabled": True,
            "eligibility_reason": reason,
            "seconds": round(time.perf_counter() - started, 3),
            "timeout_seconds": timeout_seconds,
            "timed_out": False,
            "status": candidate["status"],
        }
    except subprocess.TimeoutExpired:
        candidate = _held_candidate(
            "semantic_candidate_timeout", reference_date
        )
        return candidate, {
            "enabled": True,
            "eligibility_reason": reason,
            "seconds": round(time.perf_counter() - started, 3),
            "timeout_seconds": timeout_seconds,
            "timed_out": True,
            "status": "held",
        }
    except Exception:
        candidate = _held_candidate(
            "semantic_candidate_runtime_failed", reference_date
        )
        return candidate, {
            "enabled": True,
            "eligibility_reason": reason,
            "seconds": round(time.perf_counter() - started, 3),
            "timeout_seconds": timeout_seconds,
            "timed_out": False,
            "status": "held",
        }


def _question_sha256(query: str) -> str:
    normalized = unicodedata.normalize("NFC", query).strip()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _empty_edge_audit_attestation() -> dict:
    return {
        "read_only": True,
        "read_snapshot": None,
        "database_opened": False,
        "generation": None,
        "index_sha256": None,
        "graph_snapshot_id": None,
        "logical_snapshot_sha256": None,
        "projection_sha256": None,
        "node_count": None,
        "edge_count": None,
        "edge_evidence_count": None,
        "eligible_evidence_count": None,
        "outbound_network_attempt_count": 0,
    }


def _rejected_edge_audit(
    diagnostic_code: str,
    candidate: object,
    registration: object,
    query: str,
    reference_date: str | None,
) -> dict:
    try:
        candidate_sha256 = _canonical_sha256(candidate)
    except (TypeError, ValueError):
        candidate_sha256 = None
    try:
        registration_sha256 = _canonical_sha256(registration)
    except (TypeError, ValueError):
        registration_sha256 = None
    operation = candidate.get("operation") if isinstance(candidate, dict) else None
    if operation not in SEMANTIC_GRAPH_OPERATIONS:
        operation = None
    return {
        "schema_version": "0.1",
        "record_type": SEMANTIC_GRAPH_EDGE_AUDIT_KEY,
        "auditor": "cross-document-semantic-graph-independent-edge-audit",
        "auditor_version": "0.1.0",
        "status": "rejected",
        "verdict": "REJECT",
        "reason_code": "independent_audit_observer_failed",
        "diagnostic_code": diagnostic_code,
        "operation": operation,
        "candidate_sha256": candidate_sha256,
        "registration_sha256": registration_sha256,
        "question_sha256": _question_sha256(query),
        "question_reference_date": reference_date,
        "graph_snapshot_id": None,
        "reconstructed_semantics_sha256": None,
        "checks": {
            "candidate_contract": (
                "PASS" if isinstance(candidate, dict) else "FAIL"
            ),
            "question_classification": "NOT_APPLICABLE",
            "registered_storage_integrity": "NOT_APPLICABLE",
            "independent_graph_reconstruction": "NOT_APPLICABLE",
            "candidate_semantics": "FAIL",
        },
        "audit_attestation": _empty_edge_audit_attestation(),
        "used_for_answers": False,
        "allows_answer_activation": False,
    }


def semantic_graph_edge_audit_eligibility(
    config: dict,
    index: Path,
    candidate: object,
) -> tuple[bool, str]:
    """Gate Step 4 independently from the Step 3 observer."""
    if (
        config.get(
            bootstrap.CROSS_DOCUMENT_INDEPENDENT_EDGE_AUDIT_FLAG,
            True,
        )
        is not True
    ):
        return False, "feature_disabled"
    if not isinstance(candidate, dict):
        return False, "candidate_absent"
    candidate_enabled, candidate_reason = semantic_graph_candidate_eligibility(
        config, index
    )
    if not candidate_enabled:
        return False, "candidate_" + candidate_reason
    return True, "validated_candidate_edge_audit_enabled"


def _edge_audit_result_is_safe(
    audit: object,
    candidate: dict,
    registration: dict,
    query: str,
    reference_date: str | None,
) -> bool:
    """Validate the independent auditor transport before recording it."""
    try:
        expected_candidate_sha256 = _canonical_sha256(candidate)
        expected_registration_sha256 = _canonical_sha256(registration)
        expected_semantics_sha256 = _canonical_sha256(
            _deterministic_candidate_semantics(candidate)
        )
    except (KeyError, TypeError, ValueError):
        return False
    if (
        not isinstance(audit, dict)
        or set(audit) != SEMANTIC_GRAPH_EDGE_AUDIT_FIELDS
        or audit.get("schema_version") != "0.1"
        or audit.get("record_type") != SEMANTIC_GRAPH_EDGE_AUDIT_KEY
        or audit.get("auditor")
        != "cross-document-semantic-graph-independent-edge-audit"
        or audit.get("auditor_version") != "0.1.0"
        or audit.get("used_for_answers") is not False
        or audit.get("allows_answer_activation") is not False
        or audit.get("candidate_sha256") != expected_candidate_sha256
        or audit.get("registration_sha256")
        != expected_registration_sha256
        or audit.get("question_sha256") != _question_sha256(query)
        or audit.get("question_reference_date") != reference_date
    ):
        return False
    status = audit.get("status")
    verdict = audit.get("verdict")
    if {"passed": "PASS", "rejected": "REJECT"}.get(status) != verdict:
        return False
    operation = audit.get("operation")
    if operation is not None and operation not in SEMANTIC_GRAPH_OPERATIONS:
        return False
    checks = audit.get("checks")
    if (
        not isinstance(checks, dict)
        or set(checks) != SEMANTIC_GRAPH_EDGE_AUDIT_CHECK_FIELDS
        or any(
            value not in {"PASS", "FAIL", "NOT_APPLICABLE"}
            for value in checks.values()
        )
    ):
        return False
    attestation = audit.get("audit_attestation")
    if (
        not isinstance(attestation, dict)
        or set(attestation)
        != SEMANTIC_GRAPH_EDGE_AUDIT_ATTESTATION_FIELDS
        or attestation.get("read_only") is not True
        or type(attestation.get("database_opened")) is not bool
        or type(attestation.get("outbound_network_attempt_count")) is not int
        or attestation["outbound_network_attempt_count"] < 0
        or (
            status == "passed"
            and attestation["outbound_network_attempt_count"] != 0
        )
    ):
        return False
    database_opened = attestation["database_opened"]
    graph_fields = (
        "generation", "index_sha256", "graph_snapshot_id",
        "logical_snapshot_sha256", "projection_sha256", "node_count",
        "edge_count", "edge_evidence_count", "eligible_evidence_count",
    )
    if database_opened and status == "passed":
        counts = registration["counts"]
        if (
            attestation.get("read_snapshot")
            != "single_sqlite_transaction"
            or attestation.get("generation") != registration["generation"]
            or attestation.get("index_sha256")
            != registration["database_sha256"]
            or attestation.get("graph_snapshot_id")
            != registration["graph_snapshot_id"]
            or attestation.get("logical_snapshot_sha256")
            != registration["logical_snapshot_sha256"]
            or not isinstance(attestation.get("projection_sha256"), str)
            or SHA256_PATTERN.fullmatch(attestation["projection_sha256"])
            is None
            or any(
                type(attestation.get(field)) is not int
                for field in (
                    "node_count",
                    "edge_count",
                    "edge_evidence_count",
                )
            )
            or attestation.get("node_count") != counts["nodes"]
            or attestation.get("edge_count") != counts["edges"]
            or attestation.get("edge_evidence_count")
            != counts["edge_evidence"]
            or type(attestation.get("eligible_evidence_count")) is not int
            or attestation["eligible_evidence_count"] < 1
            or audit.get("graph_snapshot_id")
            != registration["graph_snapshot_id"]
        ):
            return False
    elif database_opened:
        counts = registration["counts"]
        if (
            attestation.get("read_snapshot")
            not in {
                "connection_opened_no_transaction",
                "single_sqlite_transaction",
            }
            or attestation.get("generation") != registration["generation"]
            or attestation.get("index_sha256")
            != registration["database_sha256"]
            or (
                attestation.get("graph_snapshot_id") is not None
                and attestation["graph_snapshot_id"]
                != registration["graph_snapshot_id"]
            )
            or (
                attestation.get("logical_snapshot_sha256") is not None
                and attestation["logical_snapshot_sha256"]
                != registration["logical_snapshot_sha256"]
            )
            or (
                attestation.get("projection_sha256") is not None
                and (
                    not isinstance(
                        attestation["projection_sha256"], str
                    )
                    or SHA256_PATTERN.fullmatch(
                        attestation["projection_sha256"]
                    )
                    is None
                )
            )
            or any(
                attestation.get(field) is not None
                and (
                    type(attestation[field]) is not int
                    or attestation[field] < 0
                )
                for field in (
                    "node_count",
                    "edge_count",
                    "edge_evidence_count",
                    "eligible_evidence_count",
                )
            )
            or (
                audit.get("graph_snapshot_id") is not None
                and audit.get("graph_snapshot_id")
                != attestation.get("graph_snapshot_id")
            )
            or (
                attestation.get("node_count") is not None
                and attestation["node_count"] != counts["nodes"]
            )
            or (
                attestation.get("edge_count") is not None
                and attestation["edge_count"] != counts["edges"]
            )
            or (
                attestation.get("edge_evidence_count") is not None
                and attestation["edge_evidence_count"]
                != counts["edge_evidence"]
            )
        ):
            return False
    elif (
        attestation.get("read_snapshot") is not None
        or any(attestation.get(field) is not None for field in graph_fields)
        or audit.get("graph_snapshot_id") is not None
    ):
        return False
    reconstructed_sha256 = audit.get("reconstructed_semantics_sha256")
    if reconstructed_sha256 is not None and (
        not isinstance(reconstructed_sha256, str)
        or SHA256_PATTERN.fullmatch(reconstructed_sha256) is None
    ):
        return False
    if status == "passed":
        expected_checks = {
            "candidate_contract": "PASS",
            "question_classification": "PASS",
            "registered_storage_integrity": (
                "PASS" if database_opened else "NOT_APPLICABLE"
            ),
            "independent_graph_reconstruction": (
                "PASS"
            ),
            "candidate_semantics": "PASS",
        }
        if (
            checks != expected_checks
            or audit.get("reason_code") is not None
            or audit.get("diagnostic_code") is not None
            or reconstructed_sha256 != expected_semantics_sha256
            or operation != candidate.get("operation")
            or (
                candidate.get("status") == "not_applicable"
                and database_opened
            )
            or (
                candidate.get("status") != "not_applicable"
                and not database_opened
            )
        ):
            return False
        if database_opened:
            candidate_attestation = candidate.get("runtime_attestation")
            if (
                not isinstance(candidate_attestation, dict)
                or set(candidate_attestation)
                != SEMANTIC_GRAPH_ATTESTATION_FIELDS
                or attestation.get("projection_sha256")
                != candidate_attestation.get("projection_sha256")
                or attestation.get("eligible_evidence_count")
                != candidate_attestation.get("eligible_evidence_count")
            ):
                return False
    elif (
        not isinstance(audit.get("reason_code"), str)
        or not audit["reason_code"].strip()
        or not isinstance(audit.get("diagnostic_code"), str)
        or not audit["diagnostic_code"].strip()
        or "FAIL" not in checks.values()
    ):
        return False
    return True


def run_semantic_graph_edge_audit(
    query: str,
    config: dict,
    index: Path,
    candidate: dict | None,
    reference_date: str | None = None,
) -> tuple[dict | None, dict]:
    """Run Step 4 after Step 3 and keep it outside answer authority."""
    enabled, reason = semantic_graph_edge_audit_eligibility(
        config, index, candidate
    )
    if not enabled:
        return None, {
            "enabled": False,
            "attempted": False,
            "eligibility_reason": reason,
            "seconds": 0.0,
            "timed_out": False,
        }
    assert isinstance(candidate, dict)
    registration = config[bootstrap.CROSS_DOCUMENT_STORAGE_CONFIG_KEY]
    try:
        reference_date = _strict_candidate_reference_date(reference_date)
    except ValueError:
        audit = _rejected_edge_audit(
            "semantic_edge_audit_reference_date_invalid",
            candidate,
            registration,
            query,
            None,
        )
        return audit, {
            "enabled": True,
            "attempted": False,
            "eligibility_reason": reason,
            "seconds": 0.0,
            "timeout_seconds": SEMANTIC_GRAPH_EDGE_AUDIT_TIMEOUT_SECONDS,
            "timed_out": False,
            "status": "rejected",
        }
    configured_timeout = config.get(
        "cross_document_semantic_graph_independent_edge_audit_timeout_seconds",
        SEMANTIC_GRAPH_EDGE_AUDIT_TIMEOUT_SECONDS,
    )
    timeout_seconds = (
        float(configured_timeout)
        if type(configured_timeout) in {int, float}
        and 1 <= configured_timeout <= 120
        else SEMANTIC_GRAPH_EDGE_AUDIT_TIMEOUT_SECONDS
    )
    started = time.perf_counter()
    request_input: Path | None = None
    candidate_input: Path | None = None
    try:
        request_payload = {
            "schema_version": "0.1",
            "question": query,
            "index_path": str(index),
            "registration": registration,
            "question_reference_date": reference_date,
        }
        with tempfile.NamedTemporaryFile(
            "w",
            suffix=".json",
            encoding="utf-8",
            delete=False,
        ) as handle:
            request_input = Path(handle.name)
            os.fchmod(handle.fileno(), 0o600)
            handle.write(_canonical_json(request_payload))
        with tempfile.NamedTemporaryFile(
            "w",
            suffix=".json",
            encoding="utf-8",
            delete=False,
        ) as handle:
            candidate_input = Path(handle.name)
            os.fchmod(handle.fileno(), 0o600)
            handle.write(_canonical_json(candidate))
        command = [
            sys.executable,
            str(BASE / "cross_document_semantic_graph_edge_audit.py"),
            "--request-file",
            str(request_input),
            "--candidate-file",
            str(candidate_input),
        ]
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=True,
            close_fds=True,
        )
        try:
            audit = _strict_candidate_json(completed.stdout)
        except (TypeError, ValueError):
            audit = _rejected_edge_audit(
                "semantic_edge_audit_output_invalid",
                candidate,
                registration,
                query,
                reference_date,
            )
        else:
            if not _edge_audit_result_is_safe(
                audit, candidate, registration, query, reference_date
            ):
                audit = _rejected_edge_audit(
                    "semantic_edge_audit_result_contract_invalid",
                    candidate,
                    registration,
                    query,
                    reference_date,
                )
        return audit, {
            "enabled": True,
            "attempted": True,
            "eligibility_reason": reason,
            "seconds": round(time.perf_counter() - started, 3),
            "timeout_seconds": timeout_seconds,
            "timed_out": False,
            "status": audit["status"],
        }
    except subprocess.TimeoutExpired:
        audit = _rejected_edge_audit(
            "semantic_edge_audit_timeout",
            candidate,
            registration,
            query,
            reference_date,
        )
        return audit, {
            "enabled": True,
            "attempted": True,
            "eligibility_reason": reason,
            "seconds": round(time.perf_counter() - started, 3),
            "timeout_seconds": timeout_seconds,
            "timed_out": True,
            "status": "rejected",
        }
    except Exception:
        audit = _rejected_edge_audit(
            "semantic_edge_audit_runtime_failed",
            candidate,
            registration,
            query,
            reference_date,
        )
        return audit, {
            "enabled": True,
            "attempted": True,
            "eligibility_reason": reason,
            "seconds": round(time.perf_counter() - started, 3),
            "timeout_seconds": timeout_seconds,
            "timed_out": False,
            "status": "rejected",
        }
    finally:
        if request_input is not None:
            request_input.unlink(missing_ok=True)
        if candidate_input is not None:
            candidate_input.unlink(missing_ok=True)


def semantic_graph_candidate_notice(record: dict) -> str:
    """Render observer telemetry without exposing it as answer evidence."""
    candidate = record.get(SEMANTIC_GRAPH_CANDIDATE_KEY)
    if not isinstance(candidate, dict):
        return ""
    trace = candidate.get("trace")
    trace = trace if isinstance(trace, dict) else {}
    used_edge_count = trace.get("used_semantic_edge_count", 0)
    if (
        not isinstance(used_edge_count, int)
        or isinstance(used_edge_count, bool)
        or used_edge_count < 0
    ):
        used_edge_count = 0
    used_for_answers = candidate.get("used_for_answers")
    used_for_answers_label = (
        "false" if used_for_answers is False
        else "true" if used_for_answers is True
        else "unknown"
    )
    edge_audit = record.get(SEMANTIC_GRAPH_EDGE_AUDIT_KEY)
    edge_audit_status = (
        str(edge_audit.get("status", "unknown"))
        if isinstance(edge_audit, dict)
        else "not_run"
    )
    edge_audit_verdict = (
        str(edge_audit.get("verdict", "unknown"))
        if isinstance(edge_audit, dict)
        else "not_run"
    )
    answer_activation = (
        str(edge_audit.get("allows_answer_activation", "unknown")).lower()
        if isinstance(edge_audit, dict)
        else "false"
    )
    return (
        '<section class="card"><details><summary>意味グラフ候補の観測結果</summary>'
        '<p class="small">候補status: '
        + html.escape(str(candidate.get("status", "unknown")))
        + "<br>使用Edge数: "
        + str(used_edge_count)
        + "<br>used_for_answers: "
        + used_for_answers_label
        + "<br>candidate_pre_audit_marker: "
        + html.escape(str(candidate.get(
            "independent_edge_audit_status", "unknown"
        )))
        + "<br>independent_edge_audit: "
        + html.escape(edge_audit_status)
        + " / "
        + html.escape(edge_audit_verdict)
        + "<br>allows_answer_activation: "
        + html.escape(answer_activation)
        + "</p></details></section>"
    )


def answer_query(query: str) -> dict:
    pipeline_started = time.perf_counter()
    config = bootstrap.load_json(bootstrap.CONFIG)
    index = Path(config["index_path"])
    bootstrap.start_ollama()
    log = bootstrap.SUPPORT / "logs" / "answers.jsonl"
    cache = bootstrap.SUPPORT / "data" / "answer-cache-v2.jsonl"
    command = [
        sys.executable, str(ENGINE / "answer_local_memory_v2.py"), query,
        "--index", str(index), "--model", config["answer_model"],
        "--audit-mode", "batched", "--fast-plan", "--log", str(log),
        "--cache", str(cache), "--json",
    ]
    answer_started = time.perf_counter()
    generated = subprocess.run(command, capture_output=True, text=True, timeout=900, check=True)
    answer_seconds = time.perf_counter() - answer_started
    record = json.loads(generated.stdout)
    sequential = bool(config.get("sequential_model_loading", True))
    reuse_loaded_model = config["answer_model"] == config["audit_model"]
    answer_unload = (
        unload_ollama_model(config["answer_model"])
        if sequential and not reuse_loaded_model
        else {
            "requested": False, "succeeded": False, "seconds": 0.0,
            "error": "", "reason": "same_model_reused" if reuse_loaded_model else "sequential_loading_disabled",
        }
    )
    legacy_record = dict(record)
    legacy_record.pop(SEMANTIC_GRAPH_CANDIDATE_KEY, None)
    legacy_record.pop(SEMANTIC_GRAPH_EDGE_AUDIT_KEY, None)
    with tempfile.NamedTemporaryFile("w", suffix=".json", encoding="utf-8", delete=False) as handle:
        json.dump(legacy_record, handle, ensure_ascii=False)
        temporary = Path(handle.name)
    try:
        audit_started = time.perf_counter()
        audited = subprocess.run([
            sys.executable, str(BASE / "final_answer_audit.py"), "--record", str(temporary),
            "--index", str(index), "--model", config["audit_model"],
        ], capture_output=True, text=True, timeout=600, check=True)
        audit_seconds = time.perf_counter() - audit_started
        audited_record = json.loads(audited.stdout)
        audited_record.pop(SEMANTIC_GRAPH_CANDIDATE_KEY, None)
        audited_record.pop(SEMANTIC_GRAPH_EDGE_AUDIT_KEY, None)
        audit_unload = (
            unload_ollama_model(config["audit_model"])
            if sequential else {"requested": False, "succeeded": False, "seconds": 0.0, "error": ""}
        )
        candidate_started = time.perf_counter()
        legacy_reference_valid, legacy_reference_date = (
            _record_reference_date(legacy_record)
        )
        audited_reference_valid, audited_reference_date = (
            _record_reference_date(audited_record)
        )
        try:
            reference_binding_valid = (
                legacy_reference_valid
                and audited_reference_valid
                and audited_reference_date == legacy_reference_date
            )
            if reference_binding_valid:
                semantic_candidate, semantic_candidate_performance = (
                    run_semantic_graph_candidate(
                        query,
                        config,
                        index,
                        legacy_reference_date,
                    )
                )
            else:
                candidate_enabled, eligibility_reason = (
                    semantic_graph_candidate_eligibility(config, index)
                )
                semantic_candidate = (
                    _held_candidate(
                        "semantic_candidate_reference_date_binding_invalid"
                    )
                    if candidate_enabled else None
                )
                semantic_candidate_performance = {
                    "enabled": candidate_enabled,
                    "eligibility_reason": eligibility_reason,
                    "seconds": round(
                        time.perf_counter() - candidate_started, 3
                    ),
                    "timed_out": False,
                    **(
                        {"status": "held"}
                        if candidate_enabled else {}
                    ),
                }
        except Exception:
            semantic_candidate = _held_candidate(
                "semantic_candidate_observer_boundary_failed",
                legacy_reference_date,
            )
            semantic_candidate_performance = {
                "enabled": True,
                "eligibility_reason": "observer_boundary_failed",
                "seconds": round(time.perf_counter() - candidate_started, 3),
                "timed_out": False,
                "status": "held",
            }
        edge_audit_reference_date = (
            legacy_reference_date
            if reference_binding_valid
            else None
        )
        try:
            semantic_edge_audit, semantic_edge_audit_performance = (
                run_semantic_graph_edge_audit(
                    query,
                    config,
                    index,
                    semantic_candidate,
                    edge_audit_reference_date,
                )
            )
        except Exception:
            registration = config.get(
                bootstrap.CROSS_DOCUMENT_STORAGE_CONFIG_KEY,
                {},
            )
            edge_audit_enabled = (
                config.get(
                    bootstrap.CROSS_DOCUMENT_INDEPENDENT_EDGE_AUDIT_FLAG,
                    True,
                )
                is True
            )
            semantic_edge_audit = (
                _rejected_edge_audit(
                    "semantic_edge_audit_observer_boundary_failed",
                    semantic_candidate,
                    registration,
                    query,
                    edge_audit_reference_date,
                )
                if edge_audit_enabled and semantic_candidate is not None
                else None
            )
            semantic_edge_audit_performance = {
                "enabled": edge_audit_enabled and semantic_candidate is not None,
                "attempted": False,
                "eligibility_reason": (
                    "observer_boundary_failed"
                    if edge_audit_enabled else "feature_disabled"
                ),
                "seconds": 0.0,
                "timed_out": False,
                **(
                    {"status": "rejected"}
                    if edge_audit_enabled and semantic_candidate is not None
                    else {}
                ),
            }
        if semantic_candidate is not None:
            audited_record[SEMANTIC_GRAPH_CANDIDATE_KEY] = semantic_candidate
        if semantic_edge_audit is not None:
            audited_record[SEMANTIC_GRAPH_EDGE_AUDIT_KEY] = semantic_edge_audit
        audited_record["pipeline_performance"] = {
            "sequential_model_loading": sequential,
            "same_model_reused_across_separate_contexts": reuse_loaded_model,
            "answer_process_seconds": round(answer_seconds, 3),
            "answer_model_unload": answer_unload,
            "audit_process_seconds": round(audit_seconds, 3),
            "audit_model_unload": audit_unload,
            "semantic_graph_candidate": semantic_candidate_performance,
            "semantic_graph_independent_edge_audit": (
                semantic_edge_audit_performance
            ),
            "total_seconds": round(time.perf_counter() - pipeline_started, 3),
        }
        audited_log = bootstrap.SUPPORT / "logs" / "audited-answers.jsonl"
        audited_log.parent.mkdir(parents=True, exist_ok=True)
        with audited_log.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(audited_record, ensure_ascii=False) + "\n")
        return audited_record
    finally:
        if sequential and "audit_unload" not in locals():
            unload_ollama_model(config["audit_model"])
        temporary.unlink(missing_ok=True)


class Handler(BaseHTTPRequestHandler):
    server_version = "LocalMemorySearch/0.1"

    def send(self, content: bytes, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(content)

    def do_GET(self) -> None:
        if self.path != "/":
            self.send(page("<h1>404</h1>"), 404)
            return
        self.send(home())

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        form = urllib.parse.parse_qs(self.rfile.read(length).decode("utf-8"))
        if self.path == "/build":
            threading.Thread(target=build_worker, daemon=True).start()
            self.send(home("セットアップを開始しました。"))
            return
        if self.path == "/ask":
            if state().get("phase") not in {"ready", "ready_with_limits"}:
                self.send(home("索引の世代が完了していないため、質問を保留しました。"), 409)
                return
            query = str(form.get("query", [""])[0]).strip()
            if not query:
                self.send(home("質問を入力してください。"), 400)
                return
            try:
                record = answer_query(query)
                answer = record["answer"]
                audit = record.get("independent_final_audit", {})
                semantic_candidate = semantic_graph_candidate_notice(record)
                sources = "".join(
                    f"<li>{html.escape(item['relative_path'])} / {html.escape(json.dumps(item['locator'], ensure_ascii=False))}</li>"
                    for item in record.get("retrieved", [])[:8]
                )
                self.send(page(f"""
                <a class="button secondary" href="/">← 戻る</a><div class="eyebrow">AUDITED ANSWER</div><h1>{html.escape(query)}</h1>
                <section class="card"><div class="answer">{html.escape(str(answer.get('answer','')))}</div><p class="small">回答モード: {html.escape(str(answer.get('answer_mode','')))}<br>独立監査: {html.escape(str(audit.get('verdict','未実行')))} — {html.escape(str(audit.get('reason','')))}</p></section>
                <section class="card"><h2>参照候補</h2><ul>{sources or '<li>根拠候補なし</li>'}</ul><p class="small">候補のファイル名は回答の正しさを自動で保証するものではありません。監査不合格時は回答を停止します。</p></section>
                {semantic_candidate}
                {security_exclusion_notice()}
                """))
            except Exception as exc:
                self.send(page(f'<a class="button secondary" href="/">← 戻る</a><section class="card"><h1>回答を保留しました</h1><p class="bad">{html.escape(type(exc).__name__ + ": " + str(exc))}</p><p>ローカルモデル、索引、または監査の機械検証に失敗したため、推測で回答しません。</p></section>'), 500)
            return
        self.send(page("<h1>404</h1>"), 404)

    def log_message(self, format: str, *args) -> None:
        path = bootstrap.SUPPORT / "logs" / "server.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write((format % args) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    if args.host not in {"127.0.0.1", "localhost"}:
        raise SystemExit("remote binding is forbidden")
    bootstrap.recover_interrupted_build()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
