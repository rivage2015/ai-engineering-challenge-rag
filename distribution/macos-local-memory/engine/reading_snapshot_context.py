"""Bind frozen, partial reading coverage through index, generation and audit.

Coverage describes extraction, not answer evidence or source-currentness.  The
ordinary index path is unchanged.  No model, source extraction or network here.
"""
from __future__ import annotations

import hashlib
import importlib
import json
from pathlib import Path
import sys


KEY = "reading_snapshot"
GUIDANCE = """読取範囲メタデータは回答の根拠ではありません。追加読取待ちの画像や失敗箇所の内容を推測しないでください。
本文・表のEvidenceが直接支持する個別の事実は回答できます。未読画像があることだけを理由に無関係な質問まで拒否しないでください。
関連範囲が未確認なら、全件性・唯一性・存在しないこと・手順の完了を断定せず、確認できた範囲と追加確認が必要な場所を区別してください。
固定データは読取時点の資料です。現在も最新版であるとの保証にしないでください。"""


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def tools_module(name):
    here = Path(__file__).resolve()
    for directory in (here.parent / "layer1/scripts", here.parents[3] / "scripts"):
        if (directory / (name + ".py")).is_file():
            if str(directory) not in sys.path:
                sys.path.insert(0, str(directory))
            return importlib.import_module(name)
    raise ValueError("snapshot_validation_tools_missing")


def _body(envelope, path):
    documents = envelope["payload"]["records"]["documents"]
    return {
        "version": "1", "snapshot_id": envelope["snapshot_id"],
        "payload_sha256": envelope["payload_sha256"],
        "path": str(path.resolve(strict=True)),
        "file_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "source_freshness": "extraction_time_only",
        "complete_source_coverage": False,
        # All selected documents, not just retrievable graph nodes.
        "documents": [{"document_id": doc["document_id"],
                       "source": doc["source"], "extraction": doc["extraction"]}
                      for doc in documents],
    }


def build_context(semantic_directory, projected_documents):
    intermediate = Path(semantic_directory) / "layer1-intermediate"
    state = json.loads((intermediate / "build-state.json").read_text())
    if state.get("extractor") != "reading-snapshot-importer":
        if any(KEY in doc.get("extraction_metadata", {}) for doc in projected_documents):
            raise ValueError("snapshot_document_without_import")
        return None
    envelope = tools_module("materialize_reading_snapshot").validate_materialized_snapshot(intermediate)
    body = _body(envelope, intermediate / "reading-snapshot.json")
    context = {**body, "contract_sha256": digest(body)}
    _check_documents(context, projected_documents, require_all=True)
    return context


def _check_documents(context, documents, *, require_all=False):
    expected = {doc["document_id"]: doc for doc in context["documents"]}
    seen = set()
    for document in documents:
        identity = document.get("document_id")
        if identity not in expected or identity in seen:
            raise ValueError("snapshot_index_document_set_mismatch")
        seen.add(identity)
        item = expected[identity]
        source = document.get("source", {})
        if any(source.get(key) != item["source"].get(key)
               for key in ("relative_path", "sha256", "size_bytes")):
            raise ValueError("snapshot_index_source_mismatch")
        extraction = document.get("extraction_metadata", {})
        if extraction.get("source_extraction") != item["extraction"]:
            raise ValueError("snapshot_index_extraction_mismatch")
        binding = extraction.get(KEY, {})
        if (not isinstance(binding, dict)
                or binding.get("snapshot_id") != context["snapshot_id"]):
            raise ValueError("snapshot_index_binding_missing")
    if require_all and seen != set(expected):
        raise ValueError("snapshot_index_document_coverage_missing")


def validate_context(metadata, documents):
    context = metadata.get(KEY)
    marked = any(KEY in doc.get("extraction_metadata", {}) for doc in documents)
    if context is None:
        if marked:
            raise ValueError("snapshot_index_coverage_missing")
        return None
    if not isinstance(context, dict):
        raise ValueError("snapshot_index_context_invalid")
    body = {key: value for key, value in context.items() if key != "contract_sha256"}
    if digest(body) != context.get("contract_sha256"):
        raise ValueError("snapshot_index_context_hash_mismatch")
    path = Path(context.get("path", ""))
    envelope = tools_module("freeze_reading_snapshot").load_snapshot(path)
    if _body(envelope, path) != body:
        raise ValueError("snapshot_index_context_content_mismatch")
    _check_documents(context, documents)
    return context


def model_scope(context):
    if not context:
        return None
    documents = []
    for doc in context["documents"]:
        extraction = doc["extraction"]
        pending = extraction.get("visual_coverage", {}).get("pending", [])
        documents.append({
            "document_id": doc["document_id"], "path": doc["source"]["relative_path"],
            "reading_policy": extraction.get("reading_policy", "full"),
            "extraction_status": extraction["status"],
            "warning_count": len(extraction.get("warnings", [])),
            "error_count": len(extraction.get("errors", [])),
            "unread_locations": [{"kind": p["kind"], "location": p["location"]} for p in pending],
        })
    result = {"snapshot_id": context["snapshot_id"], "contract_sha256": context["contract_sha256"],
              "complete_source_coverage": False, "source_freshness": "extraction_time_only",
              "documents": documents}
    if len(_json(result)) > 12000:
        raise ValueError("snapshot_scope_exceeds_context_budget")
    return result


def scope_text(context):
    scope = model_scope(context)
    if scope is None:
        return ""
    return "\n[READING SCOPE: metadata, not Evidence]\n" + _json(scope) + "\n"


def notice(context):
    if not context:
        return ""
    docs = context["documents"]
    pending = sum(len(doc["extraction"].get("visual_coverage", {}).get("pending", [])) for doc in docs)
    failed = sum(doc["extraction"]["status"] in ("failed", "deferred") for doc in docs)
    return (f"保存JSONの本文・表を検索しました（{len(docs)}資料）。追加読取待ち{pending}箇所、"
            f"読取失敗・保留{failed}資料。画像内容や資料全体の網羅性は未確認です。"
            "読取時点の固定データであり、現在の最新版を保証するものではありません。")
