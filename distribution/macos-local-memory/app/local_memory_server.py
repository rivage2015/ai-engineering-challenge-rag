#!/usr/bin/env python3
"""Localhost-only web UI for the packaged local-memory system."""

from __future__ import annotations

import argparse
import copy
import fcntl
import hashlib
import hmac
import html
import importlib.util
import json
import os
import re
import secrets
import stat
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.parse
import urllib.request
import unicodedata
import uuid
from contextlib import contextmanager
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import bootstrap
import semantic_graph_answer_promotion
import semantic_graph_trust
import intent_contract
import grounded_guidance
import audit_response_guard
import source_updates
import source_selection


BUILD_LOCK = threading.Lock()
ACTIVE_WORK_LOCK = threading.Lock()
ACTIVE_WORK_COUNT = 0
SOURCE_CHANGE_ACTIVE = False
SERVER_SHUTDOWN_REQUESTED = threading.Event()
BASE = Path(__file__).resolve().parent
ENGINE = BASE / "engine"
OLLAMA_GENERATE = "http://127.0.0.1:11434/api/generate"
LOCAL_HTTP_OPENER = urllib.request.build_opener(
    urllib.request.ProxyHandler({})
)
SERVER_PROTOCOL_VERSION = "local-memory-search-step5-v1"
SERVER_HEALTH_PATH = "/__local_memory_health"
SERVER_SHUTDOWN_PATH = "/__local_memory_shutdown"
SERVER_IDENTITY_FILENAME = "server-identity-v1.json"
SERVER_IDENTITY_LOCK_FILENAME = ".server-identity-v1.lock"
STARTUP_RECOVERY_RETRY_SECONDS = 0.25
STARTUP_RECOVERY_MAX_ACTIVE_RETRIES = 480
STARTUP_RECOVERY_ACTIVE_STATUSES = {
    "active_build",
    "active",
    "active_shadow",
    "active_semantic_storage",
}
UI_CSRF_FIELD = "_local_memory_csrf"
MAX_FORM_BYTES = 64 * 1024
# These forms carry both original text and a signed, Unicode-rich contract.
# Preserve the smaller bound on all existing operational endpoints.
MAX_INTENT_FORM_BYTES = 128 * 1024
REVIEW_TICKET_TTL_SECONDS = 30 * 60
MAX_REVIEW_TICKETS = 256
SEARCH_REQUEST_CONTEXT = threading.local()
SEARCH_LOG_LOCK = threading.Lock()


def _diagnostic_text(value: object, sensitive_values: tuple[str, ...] = (), limit: int = 16000) -> str:
    """Keep diagnostics bounded and omit credentials without recording locals."""
    rendered = str(value)
    for sensitive in sensitive_values:
        if sensitive:
            rendered = rendered.replace(sensitive, "[redacted]")
    rendered = re.sub(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]+=*", "Bearer [redacted]", rendered)
    rendered = re.sub(
        r"(?i)([\"']?(?:api[_-]?key|authorization|password|secret|(?:intent_|access_|refresh_)?token|intent_signature|intent_payload|_local_memory_csrf)[\"']?\s*[:=]\s*)(?:[\"'][^\"'\r\n]*[\"']|[^\s,;}]+)",
        r"\1[redacted]", rendered,
    )
    return rendered[:limit]


def _log_search_event(context: dict, event: str, *, exc: Exception | None = None,
                      record: dict | None = None, coverage: dict | None = None) -> bool:
    """Append only selected request fields; never serialize forms or config."""
    sensitive = tuple(context.get("_sensitive_values", ()))
    entry = {
        "timestamp": datetime.now().astimezone().isoformat(timespec="milliseconds"),
        "request_id": context["request_id"],
        "request_started_at": context["request_started_at"],
        "question": _diagnostic_text(context.get("question", ""), sensitive, 2000),
        "stage": context.get("stage", "request"),
        "event": event,
    }
    if exc is not None:
        entry["exception"] = {
            "type": type(exc).__name__,
            "message": _diagnostic_text(exc, sensitive, 2000),
            "traceback": _diagnostic_text("".join(
                traceback.TracebackException.from_exception(exc, capture_locals=False).format()
            ), sensitive),
        }
        if isinstance(exc, (subprocess.CalledProcessError, subprocess.TimeoutExpired)):
            stderr = exc.stderr or ""
            if isinstance(stderr, bytes):
                stderr = stderr.decode("utf-8", errors="replace")
            entry["exception"]["subprocess_stderr"] = _diagnostic_text(stderr, sensitive, 8000)
            if isinstance(exc, subprocess.CalledProcessError):
                entry["exception"]["subprocess_returncode"] = exc.returncode
    if isinstance(record, dict):
        policy = record.get("answerability_policy", {})
        answer = record.get("answer", {})
        audit = record.get("independent_final_audit", {})
        guidance = record.get("grounded_guidance", {})
        guidance = guidance if isinstance(guidance, dict) else {}
        policy = policy if isinstance(policy, dict) else {}
        answer = answer if isinstance(answer, dict) else {}
        audit = audit if isinstance(audit, dict) else {}
        entry["result"] = {
            "answer_mode": answer.get("answer_mode"),
            "answer_status": record.get("answer_status", answer.get("answer_status")),
            "audit_verdict": audit.get("verdict"),
            "guidance_status": guidance.get("status"),
            "guidance_reason": guidance.get("reason_code", guidance.get("reason")),
            "audit_reason": _diagnostic_text(audit.get("reason", ""), sensitive, 1000),
            "policy_version": policy.get("version"),
            "confirmed_field_ids": policy.get("confirmed_field_ids", []),
            "unresolved_field_ids": policy.get("unresolved_field_ids", []),
            "reference_only": policy.get("reference_only", False),
        }
    if isinstance(coverage, dict):
        entry["complete"] = coverage.get("complete") is True
    descriptor = None
    try:
        path = bootstrap.SUPPORT / "logs" / "request-events.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        metadata = os.fstat(descriptor)
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid()
                or metadata.st_nlink != 1):
            return False
        os.fchmod(descriptor, 0o600)
        payload = (json.dumps(entry, ensure_ascii=False) + "\n").encode("utf-8")
        with SEARCH_LOG_LOCK:
            while payload:
                written = os.write(descriptor, payload)
                if written <= 0:
                    return False
                payload = payload[written:]
        return True
    except (OSError, TypeError, ValueError):
        return False  # Diagnostic failure must not erase a valid answer.
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _search_stage(stage: str) -> None:
    context = getattr(SEARCH_REQUEST_CONTEXT, "value", None)
    if context is not None:
        context["stage"] = stage
        _log_search_event(context, "stage_started")


def _attach_search_request(record: dict) -> None:
    context = getattr(SEARCH_REQUEST_CONTEXT, "value", None)
    if context is not None:
        record["request_id"] = context["request_id"]
        record["request_started_at"] = context["request_started_at"]


def _server_build_id() -> str:
    """Fingerprint the loaded server's complete executable resource set."""
    paths = set(BASE.glob("*.py")) | set(BASE.glob("*.sh"))
    paths |= set(BASE.glob("*.js"))
    paths |= set(ENGINE.rglob("*.py"))
    paths |= set(ENGINE.rglob("*.json"))
    paths |= set(ENGINE.rglob("*.js"))
    paths |= set(ENGINE.rglob("*.swift"))
    runtime_contracts: list[tuple[str, Path]] = []
    for name in (
        "paddleocr-requirements.lock.txt",
        "paddleocr-model-manifest.json",
    ):
        packaged = BASE / name
        source_tree = BASE.parent / name
        selected = packaged if packaged.is_file() else source_tree
        if selected.is_file():
            runtime_contracts.append((f"runtime-contract/{name}", selected))
    bundle_contracts: list[tuple[str, Path]] = []
    contents = BASE.parent
    if contents.name == "Contents":
        for logical_name, path in (
            ("bundle/Info.plist", contents / "Info.plist"),
            ("bundle/MacOS/applet", contents / "MacOS" / "applet"),
            ("bundle/Scripts/main.scpt", BASE / "Scripts" / "main.scpt"),
        ):
            if not path.is_file():
                raise RuntimeError("server_bundle_contract_missing")
            bundle_contracts.append((logical_name, path))
    if not paths or len(runtime_contracts) != 2:
        raise RuntimeError("server_build_files_missing")
    digest = hashlib.sha256()
    resources = [
        (path.relative_to(BASE).as_posix(), path)
        for path in paths
    ] + runtime_contracts + bundle_contracts
    for relative_name, path in sorted(resources, key=lambda item: item[0]):
        if path.is_symlink() or not path.is_file():
            raise RuntimeError("server_build_file_invalid")
        relative = relative_name.encode("utf-8")
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        data = path.read_bytes()
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)
    return digest.hexdigest()


SERVER_BUILD_ID = _server_build_id()
SEMANTIC_GRAPH_CANDIDATE_KEY = (
    "cross_document_semantic_graph_query_candidate"
)
SEMANTIC_GRAPH_EDGE_AUDIT_KEY = (
    "cross_document_semantic_graph_independent_edge_audit"
)
SEMANTIC_GRAPH_ANSWER_PROMOTION_KEY = (
    "cross_document_semantic_graph_answer_promotion"
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


UI_SCRIPT = """(() => {
  let submitting = false;
  let disposed = false;
  let pollTimer;
  const pollUpdates = async () => {
    if (disposed) return;
    const card = document.getElementById("source-updates");
    if (!card || card.dataset.busy !== "true") return;
    try {
      const response = await fetch("/source-updates/status", {credentials: "same-origin", cache: "no-store"});
      if (!response.ok) throw new Error("status unavailable");
      const result = await response.json();
      if (disposed) return;
      card.outerHTML = result.html;
      if (result.busy) pollTimer = setTimeout(pollUpdates, 4000);
    } catch (_error) {
      if (disposed) return;
      card.dataset.busy = "false";
      const notice = document.createElement("p");
      notice.textContent = "\\u72b6\\u614b\\u3092\\u53d6\\u5f97\\u3067\\u304d\\u307e\\u305b\\u3093\\u3002\\u753b\\u9762\\u3092\\u958b\\u304d\\u76f4\\u3057\\u3066\\u78ba\\u8a8d\\u3057\\u3066\\u304f\\u3060\\u3055\\u3044\\u3002";
      card.appendChild(notice);
    }
  };
  pollTimer = setTimeout(pollUpdates, 1000);
  // Native POST + no-referrer can send Origin:null. Keep the server's strict
  // Origin/CSRF checks; use same-origin fetch for every owned POST form instead.
  // Delegation also covers source-update cards replaced by the polling above.
  document.addEventListener("submit", async (event) => {
    const form = event.target;
    if (!(form instanceof HTMLFormElement) || form.method.toLowerCase() !== "post"
        || !form.querySelector('input[name="_local_memory_csrf"]')) return;
    event.preventDefault();
    if (submitting) return;
    let status = form.querySelector(".progress, .local-submit-status");
    if (!status) {
      status = document.createElement("p");
      status.className = "progress local-submit-status";
      status.setAttribute("role", "status");
      form.appendChild(status);
    }
    const buttons = Array.from(form.querySelectorAll('button, input[type="submit"]'));
    const wasDisabled = buttons.map((button) => button.disabled);
    try {
      const submitter = event.submitter;
      const action = new URL(submitter?.hasAttribute("formaction")
        ? submitter.formAction : form.action, location.href);
      if (action.origin !== location.origin) throw new Error("invalid form origin");
      // Capture successful controls BEFORE disabling, including a named button.
      const fields = new FormData(form);
      if (submitter && submitter.name && !submitter.disabled) {
        fields.append(submitter.name, submitter.value);
      }
      const body = new URLSearchParams(fields);
      submitting = true;
      buttons.forEach((button) => { button.disabled = true; });
      status.hidden = false;
      status.className = "progress local-submit-status";
      status.textContent = form.dataset.localProgress || form.dataset.progress
        || (form.id === "local-search-form"
          ? "実行中です。根拠の検索と監査を行っています…" : "実行中です。処理結果を待っています…");
      const response = await fetch(action.href, {
        method: "POST",
        body,
        mode: "same-origin",
        credentials: "same-origin",
        headers: {"Content-Type": "application/x-www-form-urlencoded"},
      });
      if (!(response.headers.get("Content-Type") || "").toLowerCase().startsWith("text/html")) {
        throw new Error("unexpected response");
      }
      const result = await response.text();
      // POST-only result paths must never become refresh/bookmark destinations.
      // A build's 303 was followed by fetch; refresh always returns to GET /.
      history.replaceState(null, "", "/");
      disposed = true;
      clearTimeout(pollTimer);
      document.open();
      document.write(result);
      document.close();
    } catch (_error) {
      submitting = false;
      buttons.forEach((button, index) => { button.disabled = wasDisabled[index]; });
      status.hidden = false;
      status.className = "warn local-submit-status";
      status.textContent = "送信結果を確認できませんでした。再送する前に、ホーム画面で処理状態を確認してください。入力内容はこの画面に残っています。 ";
      const link = document.createElement("a");
      link.href = "/";
      link.textContent = "ホーム画面で確認する";
      status.appendChild(link);
    }
  });
})();
""".encode("utf-8")


def page(body: str, refresh: int | None = None) -> bytes:
    meta = f'<meta http-equiv="refresh" content="{refresh}">' if refresh else ""
    value = f"""<!doctype html><html lang="ja"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">{meta}<title>Local Memory Search</title><style>{STYLE}</style></head><body><main class="wrap">{body}</main><script src="/local-memory-ui.js"></script></body></html>"""
    return value.encode("utf-8")


def state() -> dict:
    return bootstrap.load_json(bootstrap.STATE, {"phase": "not_started", "message": "まだ索引は作成されていません。", "error": ""})


def intent_hidden(name, value):
    return f'<input type="hidden" name="{html.escape(name, quote=True)}" value="{html.escape(value, quote=True)}">'


def intent_form(action, csrf, contents):
    progress = '確認内容を準備しています…' if action != '/local-search-answer' else '実行中です。根拠の検索と監査を行っています…'
    return (f'<form id="local-search-form" data-progress="{progress}" method="post" action="{action}">'
            + intent_hidden(UI_CSRF_FIELD, csrf) + contents
            + '<p id="local-search-progress" class="progress" hidden></p></form>')


def intent_dialog(query, csrf):
    return page('<section class="card"><h1>まず、知りたい範囲を教えてください</h1>'
        + '<p>あなた：' + html.escape(query) + '</p><p>一つの答えですか？ それとも一連の流れですか？</p>'
        + intent_form('/intent-scope', csrf, intent_hidden('query', query)
            + '<label><input type="radio" name="scope" value="workflow" required>一連の手順・条件分岐を知りたい</label><br>'
            + '<label><input type="radio" name="scope" value="fact">一言・一つの事実を知りたい</label><br>'
            + '<label><input type="radio" name="scope" value="custom">その他：目的を自分で説明したい</label><br>'
            + '<button>回答の完成形を相談する</button>') + '</section>')


def intent_editor(query, scope, csrf, goal=None, requirements=None, notice=''):
    draft_goal, draft_requirements = intent_contract.draft_intent(query, scope)
    if goal is None:
        goal = draft_goal
    if requirements is None:
        requirements = draft_requirements
    return page('<section class="card"><h1>どんな回答なら役に立ちますか？</h1><p>あなた：' + html.escape(query) + '</p><p>内容は自由に修正できます。まだ資料検索は始めません。手順の場合は、どこからどこまで知りたいかも記入してください。</p>'
        + ('<p class="warn">' + html.escape(notice) + '</p>' if notice else '')
        + intent_form('/intent-preview', csrf, intent_hidden('query', query)
            + '<label>知りたいこと<textarea name="goal" required maxlength="3000">' + html.escape(goal) + '</textarea></label>'
            + '<label>回答に必要な内容（1行に1項目）<textarea name="requirements" required>' + html.escape(requirements) + '</textarea></label>'
            + '<button>この内容を確認する</button>') + '</section>')


def intent_preview(contract, csrf):
    payload, signature = intent_contract.seal(contract, intent_contract.SIGNING_KEY)
    items = ''.join('<li>' + html.escape(x) + '</li>' for x in contract['requirements'])
    # One form carries the signed, displayed contract. Editing returns to review.
    return page('<section class="card"><h1>この回答を目指して検索します</h1><p>' + html.escape(contract['goal'])
        + '</p><ul>' + items + '</ul><p>不足する項目は「確認できなかった内容」として表示します。資料の新旧確認は別途維持します。</p>'
        + intent_form('/local-search-answer', csrf, intent_hidden('query', contract['question'])
            + intent_hidden('intent_payload', payload) + intent_hidden('intent_signature', signature)
            + '<label><input type="radio" name="intent_action" value="confirm" required>この完成形で検索する</label><br>'
            + '<label><input type="radio" name="intent_action" value="edit">内容を修正する</label><br><button>進む</button>') + '</section>')


def audit_intent_coverage(contract, record):
    answer = str(record.get('answer', {}).get('answer', ''))
    verdict = {}
    config = bootstrap.load_json(bootstrap.CONFIG)
    context_tokens, output_tokens = audit_response_guard.NORMAL_CONTEXT_TOKENS, 1200
    diagnostics = audit_response_guard.context_diagnostics(None, context_tokens, output_tokens)
    failure_reason = None
    # The shared guard checks termination, capacity and JSON structure. The
    # contract checker validates the integer indices, boolean verdicts and
    # exact quotes, including duplicate and missing requirement indices.
    schema = {'type': 'object', 'required': ['items'], 'additionalProperties': False,
        'properties': {'items': {'type': 'array', 'maxItems': 8, 'items': {
            'type': 'object', 'required': ['index', 'covered', 'quote'],
            'properties': {'quote': {'type': 'string', 'maxLength': 3000},
                           'reason': {'type': 'string', 'maxLength': 300}}}}}}
    model_schema = copy.deepcopy(schema)
    model_schema['properties']['items']['items']['properties'].update(
        index={'type': 'integer'}, covered={'type': 'boolean'})
    try:
        if (record.get('independent_final_audit', {}).get('verdict') != 'verified'
                or record.get('answer', {}).get('answer_mode') == 'insufficient'):
            raise ValueError('Evidence audit did not pass; completeness cannot pass.')
        prompt = intent_contract.coverage_prompt(contract, answer)
        request = urllib.request.Request(OLLAMA_GENERATE, data=json.dumps({
            'model': config['audit_model'], 'prompt': prompt, 'stream': False,
            'format': model_schema, 'think': False,
            'options': {'temperature': 0, 'num_ctx': context_tokens, 'num_predict': output_tokens},
        }).encode(), headers={'Content-Type': 'application/json'}, method='POST')
        with LOCAL_HTTP_OPENER.open(request, timeout=120) as response:
            raw = json.loads(response.read())
        # /generate uses response instead of /chat's message.content. Preserve
        # its actual termination/usage fields; never invent missing metadata.
        guarded = dict(raw) if isinstance(raw, dict) else raw
        if isinstance(guarded, dict):
            guarded['message'] = {'content': guarded.get('response')}
        verdict, diagnostics = audit_response_guard.parse_normal_response(
            guarded, schema, context_tokens, output_tokens)
    except Exception as exc:
        failure_reason = (exc.code if isinstance(exc, audit_response_guard.AuditResponseError)
                          else 'coverage_audit_unavailable')
        if isinstance(exc, audit_response_guard.AuditResponseError):
            diagnostics = exc.diagnostics
        context = getattr(SEARCH_REQUEST_CONTEXT, "value", None)
        if context is not None:
            _log_search_event(context, "intent_coverage_unavailable", exc=exc)
        # An unavailable completeness check must never report completion.
    finally:
        if config.get('sequential_model_loading', True) and config.get('audit_model'):
            unload_ollama_model(config['audit_model'])
    coverage = intent_contract.check_coverage(contract, answer, verdict)
    if failure_reason:
        coverage.update(complete=False, status='unavailable', reason_code=failure_reason)
    policy = record.get('answerability_policy', {})
    if (record.get('independent_final_audit', {}).get('verdict') != 'verified'
            or record.get('answer', {}).get('answer_mode') == 'insufficient'
            or policy.get('reference_only')
            or policy.get('observations')
            or policy.get('unresolved_field_ids')
            or record.get('grounded_guidance', {}).get('status') == 'incomplete'):
        coverage.update(complete=False, status='blocked', reason_code='answer_not_fully_confirmed')
    coverage['diagnostics'] = diagnostics
    record['confirmed_intent'] = contract
    record['intent_coverage'] = coverage
    path = bootstrap.SUPPORT / 'logs' / 'intent-answers.jsonl'
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a', encoding='utf-8') as handle:
        handle.write(json.dumps({'contract': contract, 'coverage': coverage, 'answer': answer,
            'request_id': record.get('request_id'),
            'request_started_at': record.get('request_started_at'),
            'answerability_policy': policy}, ensure_ascii=False) + '\n')
    return coverage


def prepare_grounded_guidance(record: dict, contract: dict, revision: dict) -> dict | None:
    """Keep the extractive record intact; build a separately audited display view."""
    if not grounded_guidance.eligible(record, contract):
        return None
    config = {}
    try:
        _search_stage("grounded_guidance")
        exists, config = bootstrap.load_config_snapshot()
        if not exists or not bootstrap.answer_config_matches_revision(config, revision):
            raise ValueError("guidance_revision_changed_before_composition")
        record["grounded_guidance"] = grounded_guidance.compose_guidance(
            record, contract, Path(config["index_path"]), config["answer_model"], timeout=120,
        )
        view = grounded_guidance.verified_view(record, contract)
        if view is None and record["grounded_guidance"].get("status") == "verified":
            record["grounded_guidance"].update(status="incomplete", reason="guidance_view_binding_failed")
        return view
    except Exception as exc:
        # A composition failure must not hide the already checked source extraction.
        record["grounded_guidance"] = {
            "status": "incomplete", "reason": "guidance_processing_incomplete",
        }
        context = getattr(SEARCH_REQUEST_CONTEXT, "value", None)
        if context is not None:
            _log_search_event(context, "grounded_guidance_unavailable", exc=exc)
        return None
    finally:
        if config.get("sequential_model_loading", True) and config.get("answer_model"):
            unload_ollama_model(config["answer_model"])


def save_grounded_guidance(record: dict, contract: dict, view: dict | None, coverage: dict) -> None:
    """Log the new artifact without relabeling the old extraction/claim audit."""
    if "grounded_guidance" not in record:
        return
    entry = {
        "request_id": record.get("request_id"),
        "request_started_at": record.get("request_started_at"),
        "contract": contract, "coverage": coverage,
        "extracted_answer": record.get("answer"),
        "grounded_guidance": record["grounded_guidance"],
        "displayed_answer": view["answer"] if view else record.get("answer", {}).get("answer"),
    }
    path = bootstrap.SUPPORT / "logs" / "guidance-answers.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(descriptor, "a", encoding="utf-8") as handle:
        metadata = os.fstat(handle.fileno())
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid() or metadata.st_nlink != 1:
            raise ValueError("guidance_log_not_private_regular_file")
        os.fchmod(handle.fileno(), 0o600)
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")


def grounded_guidance_sources(view: dict) -> tuple[str, str, str]:
    rows = []
    for source in view["sources"]:
        rows.append(
            "<li>" + html.escape(str(source["relative_path"])) + " / "
            + html.escape(json.dumps(source["locator"], ensure_ascii=False))
            + "<br>原文：「" + html.escape(source["quote"]) + "」<br>Evidence: "
            + html.escape(source["evidence_id"]) + "</li>"
        )
    return ("案内例の根拠", "".join(rows),
            "案内例は以下の原文を組み合わせた表現です。原文の引用そのものではありません。")


def _log_startup_recovery_failure(exc: Exception) -> None:
    """Persist a bounded local diagnostic without changing startup outcome."""
    path = bootstrap.SUPPORT / "logs" / "startup-recovery.jsonl"
    descriptor = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        flags = (
            os.O_WRONLY
            | os.O_APPEND
            | os.O_CREAT
            | getattr(os, "O_NOFOLLOW", 0)
        )
        descriptor = os.open(path, flags, 0o600)
        os.fchmod(descriptor, 0o600)
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or metadata.st_nlink != 1
        ):
            return
        reason = getattr(exc, "reason_code", None)
        record = {
            "status": "startup_recovery_failed",
            "error_type": type(exc).__name__,
            "reason_code": (
                str(reason)[:256]
                if isinstance(reason, str) and reason
                else None
            ),
            "message": str(exc)[:512],
        }
        os.write(
            descriptor,
            (json.dumps(
                record,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ) + "\n").encode("utf-8"),
        )
    except (OSError, TypeError, ValueError):
        return
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _startup_recovery_outcome() -> str:
    """Wait out another process's build, then recover before serving work."""
    active_retries = 0
    while True:
        try:
            result = bootstrap.recover_interrupted_build()
            if not isinstance(result, dict):
                raise RuntimeError("startup_recovery_result_invalid")
        except Exception as exc:
            _log_startup_recovery_failure(exc)
            return "failed"
        if result.get("status") not in STARTUP_RECOVERY_ACTIVE_STATUSES:
            return "ready"
        active_retries += 1
        if active_retries >= STARTUP_RECOVERY_MAX_ACTIVE_RETRIES:
            timeout = RuntimeError("startup_recovery_active_timeout")
            timeout.reason_code = "startup_recovery_active_timeout"
            _log_startup_recovery_failure(timeout)
            return "failed"
        # Keep health in ``recovering`` and ACTIVE_WORK_COUNT above zero.  The
        # external builder owns the cross-process lease; when it exits or is
        # killed, the next iteration acquires that lease and repairs any dead
        # ``building`` state before this server becomes ready.
        time.sleep(STARTUP_RECOVERY_RETRY_SECONDS)


def server_health_payload(
    instance_id: str,
    startup_state: str = "ready",
) -> dict:
    """Return the fixed, side-effect-free launcher handshake."""
    if startup_state not in {"recovering", "ready", "failed"}:
        raise ValueError("server_startup_state_invalid")
    return {
        "service": "LocalMemorySearch",
        "protocol_version": SERVER_PROTOCOL_VERSION,
        "build_id": SERVER_BUILD_ID,
        "instance_id": instance_id,
        "graceful_restart": True,
        "startup_state": startup_state,
    }


def _bound_server_port(server: object) -> int | None:
    port = getattr(server, "server_port", None)
    if isinstance(port, int) and not isinstance(port, bool):
        return port if 1 <= port <= 65535 else None
    address = getattr(server, "server_address", None)
    if (
        isinstance(address, tuple)
        and len(address) >= 2
        and isinstance(address[1], int)
        and not isinstance(address[1], bool)
        and 1 <= address[1] <= 65535
    ):
        return address[1]
    return None


def _local_http_authorities(server: object) -> frozenset[str]:
    port = _bound_server_port(server)
    if port is None:
        return frozenset()
    suffix = "" if port == 80 else f":{port}"
    return frozenset({f"127.0.0.1{suffix}", f"localhost{suffix}"})


def _local_request_host_is_valid(server: object, headers: object) -> bool:
    host = getattr(headers, "get", lambda *_args: None)("Host")
    return (
        isinstance(host, str)
        and host.strip().lower() in _local_http_authorities(server)
    )


def _local_ui_post_is_authorized(
    server: object,
    headers: object,
    form: dict[str, list[str]],
) -> bool:
    expected = getattr(server, "ui_csrf_token", "")
    supplied_values = form.get(UI_CSRF_FIELD, [])
    token_is_current = (
        isinstance(expected, str)
        and bool(expected)
        and isinstance(supplied_values, list)
        and len(supplied_values) == 1
        and isinstance(supplied_values[0], str)
        and hmac.compare_digest(supplied_values[0], expected)
    )
    if token_is_current:
        return _local_ui_request_context_is_same_origin(server, headers)
    if (
        not isinstance(supplied_values, list)
        or len(supplied_values) != 1
        or not isinstance(supplied_values[0], str)
        or not supplied_values[0]
    ):
        return False
    # Chromium can restore a no-store page from its back/forward cache after
    # this local server has restarted.  The form token is then stale even
    # though the navigation is demonstrably from this exact loopback origin.
    # Do not relax cross-site requests or token-less requests: the exception
    # requires every browser Fetch Metadata signal for a same-origin HTML form.
    return _local_ui_post_is_strict_same_origin_navigation(server, headers)


def _local_ui_request_context_is_same_origin(
    server: object,
    headers: object,
) -> bool:
    authorities = _local_http_authorities(server)
    origin = getattr(headers, "get", lambda *_args: None)("Origin")
    if isinstance(origin, str) and origin:
        if origin.strip().lower() not in {
            f"http://{authority}" for authority in authorities
        }:
            return False
    fetch_site = getattr(headers, "get", lambda *_args: None)(
        "Sec-Fetch-Site"
    )
    if (
        isinstance(fetch_site, str)
        and fetch_site
        and fetch_site.lower() not in {"same-origin", "none"}
    ):
        return False
    return True


def _local_ui_post_is_strict_same_origin_navigation(
    server: object,
    headers: object,
) -> bool:
    authorities = _local_http_authorities(server)
    origins = {f"http://{authority}" for authority in authorities}
    get = getattr(headers, "get", lambda *_args: None)
    origin = get("Origin")
    referer = get("Referer")
    content_type = get("Content-Type")
    return (
        isinstance(origin, str)
        and origin.strip().lower() in origins
        and isinstance(referer, str)
        and any(
            referer.strip().lower().startswith(expected + "/")
            for expected in origins
        )
        and get("Sec-Fetch-Site") == "same-origin"
        and get("Sec-Fetch-Mode") == "navigate"
        and get("Sec-Fetch-Dest") == "document"
        and isinstance(content_type, str)
        and content_type.lower().split(";", 1)[0].strip()
        == "application/x-www-form-urlencoded"
    )


def _local_ui_post_has_stale_csrf(
    server: object,
    headers: object,
    form: dict[str, list[str]],
) -> bool:
    """Recognize an expired local form without accepting its request."""
    expected = getattr(server, "ui_csrf_token", "")
    supplied_values = form.get(UI_CSRF_FIELD, [])
    return (
        isinstance(expected, str)
        and bool(expected)
        and isinstance(supplied_values, list)
        and len(supplied_values) == 1
        and isinstance(supplied_values[0], str)
        and bool(supplied_values[0])
        and not hmac.compare_digest(supplied_values[0], expected)
        and _local_ui_request_context_is_same_origin(server, headers)
    )


def _begin_active_work() -> bool:
    global ACTIVE_WORK_COUNT
    with ACTIVE_WORK_LOCK:
        if SERVER_SHUTDOWN_REQUESTED.is_set() or SOURCE_CHANGE_ACTIVE:
            return False
        ACTIVE_WORK_COUNT += 1
        return True


def _end_active_work() -> None:
    global ACTIVE_WORK_COUNT
    with ACTIVE_WORK_LOCK:
        ACTIVE_WORK_COUNT = max(0, ACTIVE_WORK_COUNT - 1)


def _reserve_source_change() -> bool:
    """Do not switch scopes while an answer, build, or update is running."""
    global ACTIVE_WORK_COUNT, SOURCE_CHANGE_ACTIVE
    if not BUILD_LOCK.acquire(blocking=False):
        return False
    with ACTIVE_WORK_LOCK:
        if SERVER_SHUTDOWN_REQUESTED.is_set() or SOURCE_CHANGE_ACTIVE or ACTIVE_WORK_COUNT:
            BUILD_LOCK.release()
            return False
        SOURCE_CHANGE_ACTIVE = True
        ACTIVE_WORK_COUNT += 1
        return True


def _release_source_change() -> None:
    global ACTIVE_WORK_COUNT, SOURCE_CHANGE_ACTIVE
    with ACTIVE_WORK_LOCK:
        SOURCE_CHANGE_ACTIVE = False
        ACTIVE_WORK_COUNT = max(0, ACTIVE_WORK_COUNT - 1)
    BUILD_LOCK.release()


def _reserve_memory_reset() -> bool:
    """Start a new question session only when no answer or build is active."""
    global ACTIVE_WORK_COUNT
    if not BUILD_LOCK.acquire(blocking=False):
        return False
    with ACTIVE_WORK_LOCK:
        if SERVER_SHUTDOWN_REQUESTED.is_set() or SOURCE_CHANGE_ACTIVE or ACTIVE_WORK_COUNT:
            BUILD_LOCK.release()
            return False
        ACTIVE_WORK_COUNT += 1
        return True


def _release_memory_reset() -> None:
    _end_active_work()
    BUILD_LOCK.release()


def _reserve_server_shutdown() -> bool:
    with ACTIVE_WORK_LOCK:
        if (
            SERVER_SHUTDOWN_REQUESTED.is_set()
            or BUILD_LOCK.locked()
            or ACTIVE_WORK_COUNT > 0
        ):
            return False
        SERVER_SHUTDOWN_REQUESTED.set()
        return True


def _cancel_server_shutdown_reservation() -> None:
    """Undo a reservation only when no shutdown worker could be started."""
    with ACTIVE_WORK_LOCK:
        SERVER_SHUTDOWN_REQUESTED.clear()


@contextmanager
def _server_identity_lease(*, shared: bool = False):
    """Serialize identity publication/removal across server processes."""
    path = bootstrap.SUPPORT / SERVER_IDENTITY_LOCK_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    acquired = False
    try:
        os.fchmod(descriptor, 0o600)
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or metadata.st_nlink != 1
        ):
            raise RuntimeError("server_identity_lock_invalid")
        fcntl.flock(
            descriptor,
            fcntl.LOCK_SH if shared else fcntl.LOCK_EX,
        )
        acquired = True
        yield
    finally:
        if acquired:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _publish_server_identity(server: ThreadingHTTPServer, port: int) -> None:
    identity = {
        "schema_version": "0.1",
        "service": "LocalMemorySearch",
        "protocol_version": SERVER_PROTOCOL_VERSION,
        "build_id": SERVER_BUILD_ID,
        "instance_id": server.instance_id,
        "pid": os.getpid(),
        "uid": os.getuid(),
        "host": "127.0.0.1",
        "port": port,
        "server_script": str(Path(__file__).resolve()),
        "shutdown_token": server.shutdown_token,
    }
    path = bootstrap.SUPPORT / SERVER_IDENTITY_FILENAME
    with _server_identity_lease():
        bootstrap.atomic_json(path, identity)
        os.chmod(path, 0o600)


def _remove_server_identity(instance_id: str) -> None:
    """Remove only the identity file owned by this exact server instance."""
    path = bootstrap.SUPPORT / SERVER_IDENTITY_FILENAME
    try:
        with _server_identity_lease():
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(path, flags)
            try:
                metadata = os.fstat(descriptor)
                if (
                    not stat.S_ISREG(metadata.st_mode)
                    or stat.S_IMODE(metadata.st_mode) != 0o600
                    or metadata.st_uid != os.getuid()
                    or metadata.st_nlink != 1
                ):
                    return
                data = os.read(descriptor, 4097)
            finally:
                os.close(descriptor)
            if len(data) > 4096:
                return
            identity = json.loads(data.decode("utf-8"))
            current = os.lstat(path)
            if (
                current.st_dev != metadata.st_dev
                or current.st_ino != metadata.st_ino
                or identity.get("instance_id") != instance_id
                or identity.get("pid") != os.getpid()
            ):
                return
            path.unlink()
    except (OSError, UnicodeError, ValueError, TypeError, AttributeError):
        return


def semantic_graph_answer_path_status(
    diagnosis: dict,
    current: dict,
) -> dict:
    """Describe Step 5 without implying that per-question checks have passed."""
    phase_ready = current.get("phase") in {"ready", "ready_with_limits"}
    if diagnosis.get("reader_migration_required") is True:
        return {
            "state": "reader_migration_required",
            "label": (
                "reader_migration_required（Reader更新あり・再構築までは"
                "現在の索引で回答）"
            ),
            "css_class": "warn",
            "show_rebuild": True,
        }
    configured = diagnosis.get(
        "cross_document_semantic_graph_answer_promotion_configured"
    ) is True
    enabled = diagnosis.get(
        "cross_document_semantic_graph_answer_promotion_enabled"
    ) is True
    if not configured and diagnosis.get("index_ready") is True and phase_ready:
        return {
            "state": "migration_required",
            "label": "migration_required（再構築が必要・現在は従来経路）",
            "css_class": "warn",
            "show_rebuild": True,
        }
    if not enabled:
        return {
            "state": "off_explicit",
            "label": "明示停止（従来経路）",
            "css_class": "small",
            "show_rebuild": False,
        }

    registration = diagnosis.get("cross_document_semantic_graph_storage")
    trust = diagnosis.get("cross_document_semantic_graph_trust")
    index_path = diagnosis.get("index_path")
    storage_run = current.get("cross_document_semantic_graph_storage")
    storage_status = (
        storage_run.get("status") if isinstance(storage_run, dict) else None
    )
    if storage_status == "pending":
        return {
            "state": "preparing",
            "label": "準備中（完了までは従来経路）",
            "css_class": "warn",
            "show_rebuild": False,
        }
    if storage_status == "held":
        reason = storage_run.get("reason_code")
        reason_label = f" / {reason}" if isinstance(reason, str) else ""
        return {
            "state": "held",
            "label": f"準備を保留しました{reason_label}（従来経路）",
            "css_class": "bad",
            "show_rebuild": True,
        }
    activated = (
        diagnosis.get("cross_document_semantic_graph_storage_enabled") is True
        and isinstance(registration, dict)
        and registration.get("status") == "validated_storage_only"
        and isinstance(registration.get("database_path"), str)
        and bool(registration.get("database_path"))
        and registration.get("database_path") == index_path
        and isinstance(trust, dict)
        and bool(trust)
    )
    if activated:
        return {
            "state": "armed_per_query",
            "label": "使用可能（質問ごとに検証）",
            "css_class": "ok",
            "show_rebuild": False,
        }
    return {
        "state": "blocked_dependency",
        "label": "前段の保存または信頼確認が未完了（従来経路）",
        "css_class": "warn",
        "show_rebuild": (
            diagnosis.get("cross_document_semantic_graph_storage_enabled")
            is True
        ),
    }


def _semantic_graph_observer_pending(current: dict) -> bool:
    return any(
        isinstance(current.get(key), dict)
        and current[key].get("status") == "pending"
        for key in (
            "cross_document_semantic_graph_shadow",
            "cross_document_semantic_graph_storage",
        )
    )


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


def document_version_review_notice(csrf_field: str, ticket_issuer=None) -> str:
    """Render unresolved version families without exposing document content."""
    path = bootstrap.DOCUMENT_VERSION_REVIEW
    if not path.is_file() or path.is_symlink():
        return ""
    try:
        graph = json.loads(path.read_text(encoding="utf-8"))
        core = {key: value for key, value in graph.items() if key != "graph_sha256"}
        expected = hashlib.sha256(
            json.dumps(
                core, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
        ).hexdigest()
        if not hmac.compare_digest(str(graph.get("graph_sha256", "")), expected):
            raise ValueError("version graph integrity mismatch")
        groups = [
            item for item in graph.get("groups", [])
            if isinstance(item, dict) and item.get("status") == "needs_human_review"
        ]
    except (OSError, ValueError, TypeError):
        return '<p class="warn">資料版の確認候補を安全に読み込めませんでした。</p>'
    if not groups:
        return ""
    cards: list[str] = []
    for group in groups:
        group_id = group.get("group_id")
        candidates = group.get("candidates")
        if not isinstance(group_id, str) or not isinstance(candidates, list):
            continue
        choices: list[str] = []
        for item in candidates:
            if not isinstance(item, dict) or not isinstance(item.get("relative_path"), str):
                continue
            relative = item["relative_path"]
            years = ", ".join(str(value) for value in item.get("explicit_years", []))
            choices.append(
                '<label><input type="radio" name="selected_relative_path" '
                f'value="{html.escape(relative, quote=True)}" required> '
                f'{html.escape(relative)} <span class="small">({html.escape(years)})</span></label><br>'
            )
        if not choices:
            continue
        reason = html.escape(str(group.get("reason_code", "ambiguous")))
        ticket = ticket_issuer(group) if ticket_issuer is not None else ""
        # The trusted reconstructed context classifies all temporal signals,
        # including invalid/unknown 202/203-shaped tokens.  A ticket therefore
        # selects the full consent flow even when no valid year was extracted.
        dated = bool(ticket) or any(
            item.get("explicit_years")
            for item in candidates
            if isinstance(item, dict)
        )
        if dated and not ticket:
            candidate_list = "".join(
                "<li>" + html.escape(str(item.get("relative_path", "不明"))) + "</li>"
                for item in candidates if isinstance(item, dict)
            )
            cards.append(
                f'<p class="warn"><b>どれを現在使う資料にしますか？</b><br>'
                f'判定保留: {reason}<br>確認票を作れないため、選択は保留しました。</p>'
                f'<ul>{candidate_list}</ul>'
            )
            continue
        if dated:
            hidden = (
                f'<input type="hidden" name="review_ticket" value="{html.escape(ticket, quote=True)}">'
                '<input type="hidden" name="relation" value="same_work_revisions">'
            )
            cards.append(
                '<form method="post" action="/document-version-decision">'
                f'{csrf_field}{hidden}'
                f'<p><b>1. これらは同じ業務の改訂版ですか？</b><br>'
                f'<span class="small">判定保留: {reason}</span></p>'
                '<p><b>2. 現在有効な資料を1つ選んでください</b></p>'
                + "".join(choices)
                + '<p><label><input type="checkbox" name="current_confirmed" value="yes" required> '
                'この資料が現在の業務に有効だと確認しました</label></p>'
                '<p><b>3. この内容を取り込み、検索と回答に使ってよいですか？</b><br>'
                '<label><input type="checkbox" name="use_approved" value="yes" required> はい、使ってかまいません</label></p>'
                '<button>確認を保存して索引を再構築</button></form>'
                '<p class="small">同じ業務ではない場合や、今は判断できない場合は、回答に使わず保留します。</p>'
                '<form method="post" action="/document-version-decision">'
                f'{csrf_field}<input type="hidden" name="review_ticket" value="{html.escape(ticket, quote=True)}">'
                '<input type="hidden" name="relation" value="independent_records">'
                '<button class="secondary">別々の年次記録として保留</button></form>'
                '<form method="post" action="/document-version-decision">'
                f'{csrf_field}<input type="hidden" name="review_ticket" value="{html.escape(ticket, quote=True)}">'
                '<input type="hidden" name="relation" value="defer">'
                '<button class="secondary">今は判断しない</button></form>'
            )
            continue
        cards.append(
            '<form method="post" action="/document-version-decision">'
            f'{csrf_field}<input type="hidden" name="group_id" '
            f'value="{html.escape(group_id, quote=True)}">'
            f'<p><b>どれを現在使う資料にしますか？</b><br><span class="small">判定保留: {reason}</span></p>'
            + "".join(choices)
            + '<br><button>この資料を採用して索引を再構築</button></form>'
        )
    if not cards:
        return ""
    return (
        '<section class="card"><div class="eyebrow">HUMAN IN THE LOOP</div>'
        f'<h2>資料の新旧を確認してください（{len(cards)}件）</h2>'
        '<p class="small">作成日時・更新日時だけでは最新版と断定しません。選択は候補一式とファイル内容のハッシュに結び付け、変更されたら再確認します。</p>'
        + "".join(cards) + "</section>"
    )


def unread_document_notice(report: object) -> str:
    """Render reader diagnostics as escaped text, not trusted answer content."""
    if not isinstance(report, dict) or not isinstance(report.get("items"), list):
        return '<p class="warn">読めなかったファイル・場所の詳細記録はありません。</p>'
    cards = []
    labels = {"partial": "一部を読めませんでした。",
              "failed": "読み取りに失敗しました。",
              "deferred": "読み取りを完了できませんでした。"}
    for item in report["items"][:100]:
        if not isinstance(item, dict) or item.get("status") not in labels:
            continue
        cards.append(
            '<div class="warn"><b>' + labels[item["status"]] + '</b><br>'
            + 'ファイル：' + html.escape(str(item.get("file", "不明"))[:2000]) + '<br>'
            + '場所：' + html.escape(str(item.get("location") or "特定できませんでした")[:2000]) + '<br>'
            + 'Readerの記録：' + html.escape(str(item.get("reason") or "理由を特定できませんでした")[:4000])
            + '</div>'
        )
    omitted = report.get("omitted", 0)
    if type(omitted) is int and omitted > 0:
        cards.append(f'<p class="warn">ほか{omitted}件あります。表示は先頭100件です。</p>')
    if not cards:
        return '<p class="small">文書単位の読取失敗記録はありません。全領域を読めた保証ではありません。</p>'
    return '<section class="card"><h2>読めなかった資料</h2>' + ''.join(cards) + '</section>'


def source_update_card(view: dict | None, csrf_token: str = "") -> str:
    if view is None:
        return ""
    escape = lambda value: html.escape(str(value), quote=True)
    phase = view.get('phase', 'idle')
    busy = phase in {'scanning', 'adopting'}
    field = (f'<input type="hidden" name="{UI_CSRF_FIELD}" '
             f'value="{escape(csrf_token)}">')
    labels = {
        'idle': '更新候補はまだ確認していません。',
        'scanning': '実行中：PC内の資料の名前・場所・更新日時を調べています。本文は読みません。',
        'adopting': '実行中：確認した資料を取り込み、安全検査と索引構築へ渡しています。',
        'complete': '所在の確認が終わりました。候補の採用には人の確認が必要です。',
        'partial': '一部の場所・候補が未確認です。「更新なし」とは判断できません。',
        'applied': '取込処理が終了しました。版の確認や読取制限は、現在の状態で確認してください。',
        'error': '更新確認または取り込みを完了できませんでした。旧資料を自動で採用し直していません。',
    }
    body = f'<p class="{"progress" if busy else "warn"}">{labels.get(phase, labels["error"])}</p>'
    if busy:
        body += f'<p>経過：{escape(view.get("elapsed_seconds", 0))} 秒。この欄は自動更新します。</p>'
    error = view.get('error', '')
    if error:
        reason = {
            'model_downloads_disabled_missing:': '必要なローカルモデルが不足しています。勝手なダウンロードはしていません。',
            'update_existing_source_unreadable': '今の検索元に、安全に引き継げないファイルがあります。更新を停止しました。',
            'update_destination_collision': '同名の別資料があるため、上書きせず停止しました。',
        }.get(error, 'ファイル・設定の変更、読取制限、または構築失敗の可能性があります。現在の状態を確認し、必要な再構築・更新確認を行ってください。')
        body += f'<p>{reason}<br><code>{escape(error)}</code></p>'
    summary = view.get('summary', {})
    if summary:
        body += (f'<p class="small">確認時点：{escape(summary.get("finished_at", "確認中"))} / '
                 f'所在候補 {escape(summary.get("candidate_files", 0))} 件 / '
                 f'列挙エラー {escape(summary.get("errors", 0))} 件 / '
                 f'候補表示上限による未確認 {escape(summary.get("candidate_overflow", 0))} 件。</p>')
    if view.get('expired'):
        body += '<p class="warn">確認画面の期限が切れました。更新確認をやり直してください。</p>'
    candidates = view.get('candidates', [])
    if candidates and not busy:
        body += '<p class="warn">同系列かもしれない資料が見つかりました。未確認のまま、現在の索引を最新版とは扱わないでください。</p>'
    if phase in {'complete', 'partial'} and not view.get('expired'):
        for item in candidates:
            record = item['record']
            ticket = f'<input type="hidden" name="candidate_ticket" value="{escape(item["ticket"])}">'
            try:
                updated = datetime.fromtimestamp(record['mtime_ns'] / 1e9).astimezone().isoformat(timespec='seconds')
            except (ValueError, TypeError, OverflowError, KeyError):
                updated = '不明'
            body += (f'<details><summary>{escape(record["name"])}</summary>'
                f'<p>現在の資料：<code>{escape(item["indexed_path"])}</code><br>'
                f'更新候補：<code>{escape(record["path"])}</code><br>'
                f'更新日時：{escape(updated)} / {escape(record["size_bytes"])} bytes</p>'
                '<p>名前と日時からの候補です。同じ内容・最新版と自動判定したものではありません。本文は未読です。</p>'
                f'<form method="post" action="/source-updates/adopt">{field}{ticket}'
                '<label><input type="checkbox" name="confirmed" value="yes" required> '
                '同じ資料の採用版であることを確認しました。現在の資料に代えて、回答用に取り込みます。</label>'
                '<p>原本と旧コピーは保持します。他の資料は保持し、新しい索引を構築します。</p>'
                '<button>確認した資料を取り込む</button></form>'
                f'<form method="post" action="/source-updates/dismiss">{field}{ticket}'
                '<button class="secondary">別資料／今回は使わない</button></form></details>')
        if not candidates:
            body += '<p>今回の範囲では、未確認の同系列候補はありません。PC全体の最新版保証ではありません。</p>'
    if not busy:
        body += f'<form method="post" action="/source-updates/scan">{field}<button>更新確認</button></form>'
    if phase == 'applied':
        body += '<p><a href="/">現在の状態と質問画面を読み直す</a>。残りの候補は「更新確認」で再確認できます。</p>'
    body += ('<p class="small">システム・アプリ・Library・キャッシュ・認証情報名・ゴミ箱・'
             '他ユーザー領域・外付け・未取得クラウド資料・隠しファイル・アーカイブ内部は除外。'
             '候補は名前の類似によるため、名前が大きく変わった資料は見つからないことがあります。'
             '選んでいない資料の本文を一括で読み込むことはありません。</p>')
    return f'<section id="source-updates" class="card" data-busy="{str(busy).lower()}"><h2>資料の更新候補</h2>{body}</section>'


def start_source_update(server, action: str, ticket: str = "", *, confirmed: bool = False):
    service = getattr(server, 'source_updates', None)
    if service is None:
        raise ValueError('update_service_unavailable')
    if not BUILD_LOCK.acquire(blocking=False):
        raise ValueError('update_busy')
    if not _begin_active_work():
        BUILD_LOCK.release()
        raise ValueError('update_shutting_down')
    reserved = False
    try:
        if action == 'scan':
            service.reserve_scan()
            work = service.scan_reserved
        elif action == 'adopt':
            item = service.reserve_adoption(ticket, confirmed=confirmed)
            work = lambda: service.adopt_reserved(item)
        else:
            raise ValueError('update_action_invalid')
        reserved = True
        def run():
            try:
                work()
            except Exception as exc:
                service.fail(exc)
            finally:
                _end_active_work()
                BUILD_LOCK.release()
        threading.Thread(target=run, name='local-memory-source-update', daemon=True).start()
    except Exception as exc:
        try:
            if reserved:
                service.fail(exc)
        finally:
            _end_active_work()
            BUILD_LOCK.release()
        raise


def source_selection_card(view: dict | None, csrf_token: str = "") -> str:
    view = view or {"phase": "idle"}
    escape = lambda value: html.escape(str(value), quote=True)
    field = (f'<input type="hidden" name="{UI_CSRF_FIELD}" value="{escape(csrf_token)}">')
    phase = view.get("phase")
    body = ('<p>読み込むフォルダを選び、次の画面で対象を確認して開始します。'
            '原本は変更しません。</p>')
    if phase in {"picking", "building"} or SOURCE_CHANGE_ACTIVE:
        body += ('<p class="progress">実行中：' + (
            'Macのフォルダ選択画面で選んでください。' if phase == "picking"
            else '選択した資料の地図・索引を準備しています。')
            + f' 経過 {escape(view.get("elapsed_seconds", 0))} 秒。</p>')
    elif phase == "selected" and not view.get("expired"):
        body += (f'<p>読み込む場所：<code>{escape(view["path"])}</code></p>'
            '<p>下位フォルダの対応資料も対象です。これまでの検索対象に追加するのではなく、'
            'このフォルダへ切り替えます。旧索引は保持しますが、新しい対象の回答には使いません。</p>'
            '<p>作成時間は資料量・形式によって変わります。版の判断が必要なら画面で確認します。'
            '不足するモデルの自動ダウンロードは行いません。</p>'
            '<form method="post" action="/source-selection/build" data-local-progress="読み込みを開始しています…">'
            + field + f'<input type="hidden" name="selection_ticket" value="{escape(view["ticket"])}">'
            '<label><input type="checkbox" name="confirmed" value="yes" required>このフォルダを読み込み、検索対象にします。</label>'
            '<br><button>このフォルダを読み込む</button><p class="progress" hidden></p></form>'
            '<form method="post" action="/source-selection/cancel">' + field
            + '<button class="secondary">変更しない</button></form>')
    else:
        if phase == "cancelled":
            body += '<p>フォルダ選択を取り消しました。選択操作による設定の変更はありません。</p>'
        elif phase == "error":
            body += ('<p class="warn">フォルダ選択または読み込みを完了できませんでした。'
                     '現在の検索対象と準備状態を確認してください。資料不足という判定ではありません。</p>')
            reason = {
                "source_is_application_data": "アプリの保存領域と重なる場所は読み込めません。ホーム全体ではなく、資料のあるフォルダを選んでください。",
                "source_directory_changed": "選択後にフォルダが置き換わりました。もう一度選んでください。",
                "configuration_changed_before_publish": "選択後に設定が変わりました。もう一度選んでください。",
                "source_confirmation_invalid": "確認が未完了、期限切れ、または使用済みです。もう一度選んでください。",
                "source_configuration_missing": "初回設定が見つかりません。アプリを開き直して設定してください。",
                "build_already_running": "別の取り込みが実行中です。完了後にもう一度選んでください。",
                "TimeoutExpired": "フォルダ選択の待ち時間を超えました。もう一度選んでください。",
            }.get(view.get("error"), "")
            if reason:
                body += '<p>' + reason + '</p>'
        elif phase == "complete":
            body += '<p>取込処理が終了しました。準備状態・読取制限・版の確認は下の表示をご覧ください。</p>'
        if view.get("expired"):
            body += '<p class="warn">選択の確認期限が切れました。もう一度選んでください。</p>'
        body += ('<form method="post" action="/source-selection/pick" data-local-progress="Macのフォルダ選択画面を開いています…">'
                 + field + '<button class="secondary">読み込むフォルダを選ぶ</button>'
                 '<p class="progress" hidden></p></form>')
    return '<section id="source-selection" class="card"><h2>読み込む資料</h2>' + body + '</section>'


def start_selected_source_build(server, candidate: dict) -> None:
    """Caller owns the exclusive reservation; the worker releases it."""
    service = server.source_selection
    def run():
        try:
            bootstrap.apply_source_selection(
                Path(candidate["identity"]["path"]), candidate["config"], candidate["identity"],
            )
            service.complete()
        except Exception as exc:
            service.fail(exc)
        finally:
            # Old source update tickets must never be shown for the new scope.
            # A failed attempt may also invalidate them; a new scan is harmless.
            try:
                server.source_updates = source_updates.SourceUpdates(bootstrap)
            finally:
                _release_source_change()
    threading.Thread(target=run, name="local-memory-source-selection", daemon=True).start()


def home(message: str = "", csrf_token: str = "", review_ticket_issuer=None,
         source_update_state=None, source_selection_state=None) -> bytes:
    diagnosis = bootstrap.diagnose()
    current = state()
    ready = (diagnosis["index_ready"] and current.get("phase") in {"ready", "ready_with_limits"}
             and not SOURCE_CHANGE_ACTIVE)
    answer_path = semantic_graph_answer_path_status(diagnosis, current)
    models = " / ".join(diagnosis["models"]) or "未確認"
    notices = "".join(f'<p class="warn">{html.escape(item)}</p>' for item in diagnosis["warnings"])
    transient = f'<p class="ok">{html.escape(message)}</p>' if message else ""
    csrf_field = (
        f'<input type="hidden" name="{UI_CSRF_FIELD}" '
        f'value="{html.escape(csrf_token, quote=True)}">'
    )
    setup = ""
    if SOURCE_CHANGE_ACTIVE:
        setup = '<p class="progress">選択・取り込み処理中です。準備完了までお待ちください。新しいフォルダの取込操作ではモデルの自動取得は行いません。</p>'
    elif current["phase"] == "building":
        setup = '<p class="progress">索引を作成中です。ファイル数と初回モデル取得により時間がかかります。この画面は自動更新します。</p>'
    elif current["phase"] == "error":
        setup = f'<p class="bad">{html.escape(current["message"])}<br><span class="small">{html.escape(current.get("error", ""))}</span></p><p>次の再実行では、不足するモデルがあれば公式Ollama経由で取得します。資料は外部へ送信しません。</p><form method="post" action="/build">{csrf_field}<button>不足モデルの取得を許可して再実行</button></form>'
    elif not ready:
        setup = f'<p>初回だけ、ローカルモデルの確認と索引作成を行います。このボタンで、不足モデルがある場合の公式Ollama経由の取得を開始します。ファイルは外部へ送信しません。</p><form method="post" action="/build">{csrf_field}<button>初回セットアップを開始</button></form>'
    elif current["phase"] == "ready_with_limits":
        limitations = html.escape(json.dumps(current.get("reader_limitations", {}), ensure_ascii=False, sort_keys=True))
        setup = f'<p class="warn">{html.escape(current["message"])}<br><span class="small">{limitations}</span></p>'
        setup += unread_document_notice(current.get("unread_document_notices"))
    else:
        setup = '<p class="ok">準備完了。曖昧な記憶のまま質問できます。</p>'
    ask = "" if not ready else f"""
    <section class="card"><div class="eyebrow">ASK YOUR MEMORY</div><h2>パソコンの中に質問する</h2>
    <form id="local-search-form" method="post" action="/intent-dialog">{csrf_field}<textarea name="query" required maxlength="2000" placeholder="何を知りたいか、話しかけてください"></textarea><br><button>知りたいことを相談する</button><p id="local-search-progress" class="progress" hidden></p></form></section>
    """
    reset_form = f"""
    <section class="card"><h2>新しい質問を始める</h2>
    <p class="small">確認途中の質問をリセットします。読み込んだ資料と索引は残ります。</p>
    <form method="post" action="/memory-reset">{csrf_field}<button class="secondary">メモリをリセット</button></form></section>
    """
    rebuild_label = (
        "Step 7 Reader索引を再構築"
        if answer_path["state"] == "reader_migration_required"
        else "意味グラフ回答を有効化して再構築"
    )
    migration_action = (
        f'<form method="post" action="/build">{csrf_field}<button>'
        f'{html.escape(rebuild_label)}</button></form>'
        if answer_path["show_rebuild"]
        else ""
    )
    answer_path_notice = (
        f'<p class="{html.escape(str(answer_path["css_class"]))}">'
        '意味グラフ回答: '
        f'{html.escape(str(answer_path["label"]))}</p>{migration_action}'
    )
    refresh = (
        4
        if current.get("phase") == "building"
        or SOURCE_CHANGE_ACTIVE
        or _semantic_graph_observer_pending(current)
        else None
    )
    return page(f"""
    <div class="eyebrow">PRIVATE / LOCAL / EVIDENCE-BASED</div><h1 class="hero">あなたのMacを、<br>曖昧な記憶から探す。</h1>
    <p class="sub">Word・Excel・PowerPoint・PDF・テキストなどの所在と内容をローカルで索引化。回答は根拠と別モデルの監査を通し、判断できない場合は理由付きで「わかりません」と停止します。</p>
    {ask}
    {reset_form}
    {source_selection_card(source_selection_state, csrf_token)}
    {source_update_card(source_update_state, csrf_token) if not SOURCE_CHANGE_ACTIVE else ''}
    {transient}{notices}<section class="card"><div class="eyebrow">SYSTEM STATUS</div><h2>現在の状態</h2><div class="grid">
    <div class="metric">メモリ<b>{diagnosis['memory_gb'] or '?'} GB</b></div><div class="metric">空き容量<b>{diagnosis['free_gb']} GB</b></div>
    <div class="metric">チップ<b>{html.escape(diagnosis['architecture'])}</b></div><div class="metric">Ollama<b>{'起動中' if diagnosis['ollama_online'] else '停止中/未導入'}</b></div></div>
    <p class="small">検索対象: {html.escape(diagnosis['source_root'] or '未選択')}<br>モデル: {html.escape(models)}</p>{answer_path_notice}{setup}</section>
    {document_version_review_notice(csrf_field, review_ticket_issuer)}
    {security_exclusion_notice()}
    <section class="card"><details><summary>プライバシーと制限</summary><p class="small">質問・回答・索引は <code>~/Library/Application Support/LocalMemorySearch</code> に保存されます。通常利用中のAI処理は127.0.0.1のOllamaのみです。初回のOllama導入・モデル取得にはインターネットが必要です。画像、スキャンPDF、対応する埋め込み画像はローカルOCRで位置付き文字を読みます。Gemmaによる座標なし文字起こしと、図・表・写真の意味観測は <code>[暫定読取]</code> として検索にだけ使い、それ単独で確定回答や確定グラフを作りません。音声・動画は未対応です。</p></details></section>
    """, refresh=refresh)


def build_worker() -> None:
    if SERVER_SHUTDOWN_REQUESTED.is_set():
        return
    if not BUILD_LOCK.acquire(blocking=False):
        return
    if not _begin_active_work():
        BUILD_LOCK.release()
        return
    try:
        bootstrap.build_index()
    except Exception:
        pass
    finally:
        _end_active_work()
        BUILD_LOCK.release()


def review_ticket_issuer(server):
    """Create opaque, in-memory tickets bound to the active review revision."""
    try:
        context = bootstrap.current_document_version_review_context()
    except Exception:
        return lambda _group: ""
    dated_ids = set(context["dated_group_ids"])

    def issue(group: dict) -> str:
        group_id = group.get("group_id")
        if group_id not in dated_ids or group_id not in context["family_keys"]:
            return ""
        revision = {
            **context["base_revision"],
            "candidate_set_sha256": group.get("candidate_set_sha256"),
        }
        now = time.monotonic()
        with server.review_ticket_lock:
            server.review_tickets = {
                key: value for key, value in server.review_tickets.items()
                if value["expires_at"] > now
            }
            while len(server.review_tickets) >= MAX_REVIEW_TICKETS:
                oldest = min(server.review_tickets, key=lambda key: server.review_tickets[key]["expires_at"])
                del server.review_tickets[oldest]
            token = secrets.token_urlsafe(32)
            server.review_tickets[token] = {
                "expires_at": now + REVIEW_TICKET_TTL_SECONDS,
                "group_id": group_id,
                "family_key": context["family_keys"][group_id],
                "revision": revision,
            }
        return token

    return issue


def consume_review_ticket(server, token: str) -> dict:
    with server.review_ticket_lock:
        ticket = server.review_tickets.pop(token, None)
    if not isinstance(ticket, dict) or ticket.get("expires_at", 0) <= time.monotonic():
        raise ValueError("dated_consent_review_ticket_invalid")
    return ticket


def save_dated_review_submission(server, form: dict[str, list[str]]) -> bool:
    """Re-attest current source/revision, then save one consent with CAS."""
    token = str(form.get("review_ticket", [""])[0]).strip()
    ticket = consume_review_ticket(server, token)
    context = bootstrap.current_document_version_review_context(validate_source=True)
    group = next(
        (item for item in context["graph"].get("groups", [])
         if item.get("group_id") == ticket["group_id"]),
        None,
    )
    if group is None or context["family_keys"].get(ticket["group_id"]) != ticket["family_key"]:
        raise ValueError("dated_consent_display_stale")
    current_revision = {
        **context["base_revision"],
        "candidate_set_sha256": group.get("candidate_set_sha256"),
    }
    relation = str(form.get("relation", [""])[0]).strip()
    selected_path = str(form.get("selected_relative_path", [""])[0]).strip()
    selected = next(
        (item for item in group.get("candidates", [])
         if item.get("relative_path") == selected_path),
        None,
    )
    if relation == "same_work_revisions":
        selected_hash = selected.get("source_sha256") if isinstance(selected, dict) else None
        current_confirmed = form.get("current_confirmed", [""])[0] == "yes"
        use_approved = form.get("use_approved", [""])[0] == "yes"
    elif relation in {"independent_records", "defer"}:
        selected_path = None
        selected_hash = None
        current_confirmed = False
        use_approved = False
    else:
        raise ValueError("dated_consent_invalid")
    resolver = bootstrap._decision_resolver()
    record = resolver.prepare_dated_consent(
        ticket["family_key"],
        group["candidates"],
        {
            "relation": relation,
            "selected_relative_path": selected_path,
            "selected_source_sha256": selected_hash,
            "current_applicability_confirmed": current_confirmed,
            "allow_ingest_index_answer": use_approved,
        },
        displayed_revision=ticket["revision"],
        current_revision=current_revision,
        actor="local-ui-human",
        decided_at=datetime.now().astimezone().isoformat(timespec="seconds"),
    )
    resolver.record_dated_consent_cas(
        bootstrap.DOCUMENT_VERSION_DECISIONS,
        record,
        ticket["revision"]["decisions_sha256"],
        bootstrap.MAX_DECISION_SNAPSHOT_BYTES,
    )
    return (
        relation == "same_work_revisions"
        and current_confirmed
        and use_approved
    )


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
        with LOCAL_HTTP_OPENER.open(request, timeout=timeout) as response:
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


def _semantic_graph_latest_config_is_safe(
    initial: object,
    latest: object,
    registration: object,
    index: Path,
) -> bool:
    """Bind answer selection to the same enabled generation seen at start."""
    if (
        not isinstance(initial, dict)
        or not isinstance(latest, dict)
        or not isinstance(registration, dict)
    ):
        return False
    required_true_flags = (
        bootstrap.CROSS_DOCUMENT_STORAGE_FLAG,
        bootstrap.CROSS_DOCUMENT_QUERY_CANDIDATE_FLAG,
        bootstrap.CROSS_DOCUMENT_INDEPENDENT_EDGE_AUDIT_FLAG,
        bootstrap.CROSS_DOCUMENT_ANSWER_PROMOTION_FLAG,
    )
    if any(
        initial.get(key, False) is not True
        or latest.get(key, False) is not True
        for key in required_true_flags
    ):
        return False
    stable_fields = (
        "active_generation",
        "index_path",
        bootstrap.CROSS_DOCUMENT_STORAGE_CONFIG_KEY,
        bootstrap.CROSS_DOCUMENT_TRUST_CONFIG_KEY,
    )
    if any(initial.get(key) != latest.get(key) for key in stable_fields):
        return False
    if (
        initial.get(bootstrap.CROSS_DOCUMENT_STORAGE_CONFIG_KEY)
        != registration
        or latest.get(bootstrap.CROSS_DOCUMENT_STORAGE_CONFIG_KEY)
        != registration
        or initial.get("index_path") != str(index)
        or latest.get("index_path") != str(index)
        or registration.get("database_path") != str(index)
    ):
        return False
    return True


def _semantic_graph_trust_is_safe(
    config: dict,
    index: Path,
    registration: dict,
    candidate: dict,
    audit: dict,
) -> object:
    """Verify trust inputs and return a closed receipt for the audit log."""
    generation_name = config.get("active_generation")
    workspace_value = config.get("workspace", bootstrap.SUPPORT / "data")
    if (
        not isinstance(generation_name, str)
        or GENERATION_PATTERN.fullmatch(generation_name) is None
        or not isinstance(config.get(bootstrap.CROSS_DOCUMENT_TRUST_CONFIG_KEY), dict)
    ):
        return False
    generation = Path(workspace_value) / "generations" / generation_name
    expected_index = (
        generation
        / bootstrap.CROSS_DOCUMENT_STORAGE_DIR
        / "safe-answer-index.sqlite3"
    )
    if index != expected_index or Path(registration["database_path"]) != index:
        return False
    verified = semantic_graph_trust.validate_trust_root(
        generation,
        registration,
        semantic_graph_trust.KeychainTrustStore(),
    )
    semantic_graph_trust.validate_trust_registration(
        config[bootstrap.CROSS_DOCUMENT_TRUST_CONFIG_KEY],
        generation,
        registration,
        verified_root=verified,
    )
    candidate_attestation = candidate.get("runtime_attestation")
    audit_attestation = audit.get("audit_attestation")
    if not isinstance(candidate_attestation, dict) or not isinstance(
        audit_attestation, dict
    ):
        return False
    expected = {
        "generation": verified["generation"],
        "graph_snapshot_id": verified["graph_snapshot_id"],
        "logical_snapshot_sha256": verified["logical_snapshot_sha256"],
        "projection_sha256": verified["projection_sha256"],
    }
    if any(
        candidate_attestation.get(key) != value
        or audit_attestation.get(key) != value
        for key, value in expected.items()
    ):
        return False
    if candidate_attestation.get("build_id") != verified["build_id"]:
        return False
    return {
        key: verified[key]
        for key in semantic_graph_answer_promotion.TRUST_BINDING_FIELDS
    }


_ANSWER_VALIDATOR_MODULE = None


def _validate_promoted_answer_with_engine(
    answer: dict,
    allowed_ids: set[str],
    expected_mode: str | None,
    reminder_required: bool | None,
) -> None:
    """Reuse the production answer JSON validator without importing retrieval."""
    global _ANSWER_VALIDATOR_MODULE
    if _ANSWER_VALIDATOR_MODULE is None:
        path = ENGINE / "answer_local_memory.py"
        if path.is_symlink() or not path.is_file():
            raise ValueError("semantic_promotion_answer_validator_missing")
        module_name = "local_memory_semantic_promotion_answer_validator"
        specification = importlib.util.spec_from_file_location(module_name, path)
        if specification is None or specification.loader is None:
            raise ValueError("semantic_promotion_answer_validator_unavailable")
        module = importlib.util.module_from_spec(specification)
        sys.modules[module_name] = module
        try:
            specification.loader.exec_module(module)
        except Exception:
            sys.modules.pop(module_name, None)
            raise
        _ANSWER_VALIDATOR_MODULE = module
    _ANSWER_VALIDATOR_MODULE.validate_answer(
        answer,
        allowed_ids,
        expected_mode,
        reminder_required,
    )


def apply_semantic_graph_answer_promotion(
    query: str,
    initial_config: dict,
    index: Path,
    audited_record: dict,
    candidate: object,
    edge_audit: object,
    reference_date: str | None,
) -> dict:
    """Select the graph answer only after every Step 5 gate passes."""
    started = time.perf_counter()
    registration = initial_config.get(
        bootstrap.CROSS_DOCUMENT_STORAGE_CONFIG_KEY
    )
    legacy_answer = audited_record.get("answer")
    feature_enabled = initial_config.get(
        bootstrap.CROSS_DOCUMENT_ANSWER_PROMOTION_FLAG,
        False,
    ) is True

    def run_gate(
        latest_config: object,
        activation_available: bool,
    ) -> tuple[dict, dict]:
        return semantic_graph_answer_promotion.promote_answer(
            legacy_answer=legacy_answer,
            question=query,
            reference_date=reference_date,
            candidate=candidate,
            audit=edge_audit,
            registration=registration,
            feature_enabled=feature_enabled,
            activation_available=activation_available,
            initial_config=initial_config,
            latest_config=latest_config,
            candidate_validator=_candidate_result_is_safe,
            audit_validator=_edge_audit_result_is_safe,
            latest_config_validator=lambda first, latest, registered: (
                _semantic_graph_latest_config_is_safe(
                    first,
                    latest,
                    registered,
                    index,
                )
            ),
            trust_validator=lambda registered, accepted, passed: (
                _semantic_graph_trust_is_safe(
                    latest_config,
                    index,
                    registered,
                    accepted,
                    passed,
                )
            ),
            final_config_loader=lambda: bootstrap.load_json(bootstrap.CONFIG),
            answer_validator=_validate_promoted_answer_with_engine,
        )

    try:
        if feature_enabled:
            try:
                with bootstrap.config_read_lease(blocking=False):
                    try:
                        latest_config = bootstrap.load_json(bootstrap.CONFIG)
                    except Exception:
                        latest_config = None
                    selected, promotion = run_gate(latest_config, True)
                    # Keep the cross-process CONFIG read lease through the
                    # answer swap.  Every in-process and CLI CONFIG publisher
                    # uses the matching exclusive lease; concurrent questions
                    # may safely hold shared leases together.
                    if promotion.get("decision") == "PROMOTE":
                        audited_record[
                            "pre_semantic_graph_promotion_answer"
                        ] = copy.deepcopy(legacy_answer)
                        audited_record["answer"] = selected
            except BlockingIOError:
                selected, promotion = run_gate(None, False)
        else:
            selected, promotion = run_gate(None, False)
    except Exception as exc:
        # A defect in the promotion boundary must never discard the separately
        # audited legacy answer.  This record is intentionally small because
        # no unvalidated promotion payload may cross the boundary.
        def safe_hash(value: object) -> str | None:
            try:
                return _canonical_sha256(value)
            except (TypeError, ValueError):
                return None

        checks = {
            key: "NOT_APPLICABLE"
            for key in semantic_graph_answer_promotion.PROMOTION_CHECK_FIELDS
        }
        checks["feature_enabled"] = (
            "PASS"
            if initial_config.get(
                bootstrap.CROSS_DOCUMENT_ANSWER_PROMOTION_FLAG,
                False,
            )
            is True
            else "FAIL"
        )
        promotion = {
            "schema_version": semantic_graph_answer_promotion.SCHEMA_VERSION,
            "record_type": SEMANTIC_GRAPH_ANSWER_PROMOTION_KEY,
            "promoter": semantic_graph_answer_promotion.PROMOTER,
            "promoter_version": (
                semantic_graph_answer_promotion.PROMOTER_VERSION
            ),
            "status": "fallback",
            "decision": "FALLBACK",
            "reason_code": "promotion_boundary_failed",
            "diagnostic_code": type(exc).__name__,
            "source_answer": "legacy",
            "operation": None,
            "question_sha256": _question_sha256(query),
            "question_reference_date": None,
            "candidate_sha256": safe_hash(candidate),
            "edge_audit_sha256": safe_hash(edge_audit),
            "registration_sha256": safe_hash(registration),
            "graph_snapshot_id": (
                registration.get("graph_snapshot_id")
                if isinstance(registration, dict)
                else None
            ),
            "trust_binding": {},
            "initial_config_sha256": None,
            "latest_config_sha256": None,
            "final_config_sha256": None,
            "legacy_answer_sha256": safe_hash(legacy_answer),
            "selected_answer_sha256": safe_hash(legacy_answer),
            "projected_answer": {},
            "evidence_ids": [],
            "source_references": [],
            "checks": checks,
            "used_for_answers": False,
        }
        selected = copy.deepcopy(legacy_answer)
    audited_record[SEMANTIC_GRAPH_ANSWER_PROMOTION_KEY] = promotion
    return {
        "enabled": initial_config.get(
            bootstrap.CROSS_DOCUMENT_ANSWER_PROMOTION_FLAG,
            False,
        )
        is True,
        "attempted": promotion.get("decision") == "PROMOTE"
        or promotion.get("reason_code") not in {
            "feature_disabled",
            "candidate_absent",
            "candidate_not_accepted",
        },
        "seconds": round(time.perf_counter() - started, 3),
        "status": promotion.get("status", "fallback"),
        "decision": promotion.get("decision", "FALLBACK"),
        "reason_code": promotion.get("reason_code"),
    }


def semantic_graph_candidate_notice(record: dict) -> str:
    """Render candidate, audit, and final promotion telemetry."""
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
    promotion = record.get(SEMANTIC_GRAPH_ANSWER_PROMOTION_KEY)
    promotion_decision = (
        str(promotion.get("decision", "unknown"))
        if isinstance(promotion, dict)
        else "not_run"
    )
    answer_source = (
        str(promotion.get("source_answer", "unknown"))
        if isinstance(promotion, dict)
        else "legacy"
    )
    promotion_reason = (
        str(promotion.get("reason_code"))
        if isinstance(promotion, dict) and promotion.get("reason_code") is not None
        else "none"
    )
    promotion_diagnostic = (
        str(promotion.get("diagnostic_code"))
        if isinstance(promotion, dict)
        and promotion.get("diagnostic_code") is not None
        else "none"
    )
    promoted = (
        promotion.get("used_for_answers") is True
        if isinstance(promotion, dict)
        else False
    )
    return (
        '<section class="card"><details><summary>意味グラフ経路の検査結果</summary>'
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
        + "<br>promotion: "
        + html.escape(promotion_decision)
        + "<br>answer_source: "
        + html.escape(answer_source)
        + "<br>promotion_reason_code: "
        + html.escape(promotion_reason)
        + "<br>promotion_diagnostic_code: "
        + html.escape(promotion_diagnostic)
        + "<br>promotion_used_for_answers: "
        + ("true" if promoted else "false")
        + "</p></details></section>"
    )


def audit_verdict_notice(audit: dict) -> str:
    """Show fixed status text; the auditor's free-form reason is diagnostic data."""
    verdict = audit.get("verdict") if isinstance(audit, dict) else None
    return {
        "verified": "確認済み — 回答文と根拠の対応を確認しました。暫定情報や未確認の項目は、回答内の表示を確認してください。",
        "qualified": "保留 — 根拠との対応を確認できない記述が残っています。確定回答としては承認していません。",
        "rejected": "不合格 — 回答を支える根拠を確認できませんでした。",
    }.get(verdict, "未確認 — 監査結果を確認できませんでした。")


def answerability_notice(record: dict) -> str:
    """Distinguish answer usefulness, evidence certainty, and completeness."""
    policy = record.get("answerability_policy", {})
    snapshot_notice = reading_snapshot_notice(record)
    if not isinstance(policy, dict) or not policy.get("applied"):
        return snapshot_notice
    if record.get("independent_final_audit", {}).get("verdict") != "verified":
        return snapshot_notice  # Preserve reading limits even when audit is incomplete.
    observations = policy.get("observations", [])
    observations = observations if isinstance(observations, list) else []
    provisional = any(isinstance(item, dict) and item.get("kind") == "provisional_reading"
                      for item in observations)
    messages = []
    if policy.get("reference_only"):
        messages.append("関連する記述を参考情報として表示しています。質問への確定回答はまだ得られていません。")
    elif policy.get("confirmed_field_ids"):
        messages.append("確認できた範囲を表示しています。")
    if provisional:
        messages.append("暫定の読み取りを含みます。引用した文字列の内容は、確定した事実としては未確認です。")
    if policy.get("unresolved_field_ids"):
        messages.append("未確認の項目があります。回答内の未確認理由と出典を確認してください。")
    if not messages:
        return snapshot_notice
    return snapshot_notice + '<div class="warn" aria-label="根拠の状態">' + ''.join(
        '<p>' + html.escape(message) + '</p>' for message in messages
    ) + '</div>'


def reading_snapshot_notice(record: dict) -> str:
    snapshot = record.get("reading_snapshot")
    if not snapshot:
        return ""
    spec = importlib.util.spec_from_file_location("ui_reading_snapshot", ENGINE / "reading_snapshot_context.py")
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    return '<div class="warn" aria-label="保存JSONの読取範囲">' + html.escape(helper.notice(snapshot)) + '</div>'


SOURCE_REVIEW_REASONS = {
    "missing_evidence": "求められた情報を検索結果から確認できませんでした。",
    "unsupported_relation": "行動・条件・担当などの関係を確認できませんでした。",
    "conflicting_evidence": "記述の食い違いを解消できませんでした。",
    "intent_ambiguity": "質問の対象や範囲を確定できませんでした。",
    "retrieval_noise": "検索結果が質問に対応するか確認できませんでした。",
    "coverage_unknown": "求められた範囲を説明できるか確認できませんでした。",
}
SOURCE_REVIEW_BINDINGS = (
    "evidence_sha256", "graph_sha256", "graph_security_partition_sha256",
    "graph_retrievable_evidence_set_sha256", "graph_embeddings_sha256",
)


def source_review_requests(record: dict) -> dict:
    """Describe unresolved fields, never upgrade a refusal into an answer."""
    answer = record.get("answer", {})
    if final_audit_incomplete(record):
        return {"status": "processing_error", "items": []}
    promotion = record.get(SEMANTIC_GRAPH_ANSWER_PROMOTION_KEY, {})
    if (isinstance(promotion, dict) and promotion.get("decision") == "PROMOTE"
            and promotion.get("used_for_answers") is True
            and answer.get("answer_status") == "answered" and answer.get("answer_mode") == "grounded"):
        return {"status": "hidden", "items": []}  # Old failed fields are not the selected answer path.
    runs = record.get("field_runs", [])
    if (answer.get("non_answer_reason", {}).get("code") == "machine_validation_failure"
            or any(run.get("audit", {}).get("reason_code") == "machine_validation_failure"
                   for run in runs)):
        return {"status": "processing_error", "items": []}
    if record.get("registered_version_scope", {}).get("status") == "hold":
        return {"status": "unavailable", "items": []}
    # Final validation failures must not open a second, less protected source path.
    for key in ("deterministic_claim_validation", "workflow_retrieval_validation",
                "temporal_reference_validation", "graph_retrieval_trace"):
        check = record.get(key, {})
        if check.get("status") in {"fail", "failed", "blocked", "invalid"}:
            return {"status": "unavailable", "items": []}
    items = []
    for run in runs:
        audit = run.get("audit", {})
        if audit.get("verdict") not in {"insufficient", "ambiguous", "contradicted"}:
            continue
        reason = audit.get("reason_code")
        if reason not in SOURCE_REVIEW_REASONS:
            continue
        retrieved = run.get("retrieved_evidence_ids", [])
        retrieved = [eid for eid in retrieved if isinstance(eid, str)]
        selected = [eid for key in ("competing_packet_ids", "supporting_packet_ids")
                    for eid in audit.get(key, []) if isinstance(eid, str) and eid in retrieved]
        ids = list(dict.fromkeys(selected or retrieved))
        items.append({"label": str(run.get("item", {}).get("label", "未確認の項目"))[:300],
                      "reason": SOURCE_REVIEW_REASONS[reason],
                      "model_note": str(audit.get("defect", ""))[:600],
                      "selection": "model_reference" if selected else "search_candidates",
                      "evidence_ids": ids[:3], "omitted_sources": max(0, len(ids) - 3)})
    # A completed semantic audit may reject the whole draft without rewriting
    # each field. Show its diagnostic candidates, never the rejected draft.
    reason = answer.get("non_answer_reason", {}).get("code")
    if not items and answer.get("answer_status") == "insufficient" and reason in SOURCE_REVIEW_REASONS:
        retrieved = {item.get("evidence_id") for item in record.get("retrieved", [])
                     if isinstance(item, dict) and isinstance(item.get("evidence_id"), str)}
        ids = list(dict.fromkeys(eid for eid in answer.get("diagnostic_evidence_ids", [])
                                if isinstance(eid, str) and eid in retrieved))
        items.append({"label": "回答できなかった内容", "reason": SOURCE_REVIEW_REASONS[reason],
                      "model_note": "", "selection": "search_candidates",
                      "evidence_ids": ids[:3], "omitted_sources": max(0, len(ids) - 3)})
    return {"status": "requested" if items else "hidden", "items": items[:3],
            "omitted_items": max(0, len(items) - 3)}


def load_source_review_packets(index: Path, ids: list[str]) -> tuple[list[dict], dict]:
    """Reuse the normal read-only, graph-eligible source loader in both layouts."""
    for directory in (BASE / "engine", BASE.parent / "engine"):
        path = directory / "answer_local_memory.py"
        if path.is_file():
            spec = importlib.util.spec_from_file_location("source_review_answer_engine", path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module.load_answer_evidence_records(index, ids)
    raise ValueError("source_review_engine_unavailable")


def prepare_source_review(record: dict, expected_revision: dict) -> dict:
    view = source_review_requests(record)
    if view["status"] != "requested":
        return view
    try:
        exists, config = bootstrap.load_config_snapshot()
        if not exists or not bootstrap.answer_config_matches_revision(config, expected_revision):
            raise ValueError("source_review_revision_changed")
        # Never open a path supplied by a model/answer record.
        index = Path(config["index_path"])
        ids = list(dict.fromkeys(eid for item in view["items"] for eid in item["evidence_ids"]))
        packets, policy = load_source_review_packets(index, ids)
        metadata = policy["metadata"]
        stored = record.get("index", {})
        if any(not isinstance(stored.get(key), str) or not stored[key]
               or stored[key] != metadata.get(key) for key in SOURCE_REVIEW_BINDINGS):
            raise ValueError("source_review_index_changed")
        eligible = set(policy["eligible_evidence_ids"])
        by_id = {packet["evidence_id"]: packet for packet in packets
                 if packet["evidence_id"] in eligible}
        budget = 12000
        for item in view["items"]:
            item["sources"] = []
            for eid in item["evidence_ids"]:
                packet = by_id.get(eid)
                if packet is None or budget <= 0:
                    continue
                text = packet["text"]
                excerpt = text[:min(2400, budget)]
                budget -= len(excerpt)
                item["sources"].append({"text": excerpt, "truncated": len(excerpt) < len(text),
                    "path": packet["relative_path"], "locator": packet["locator"]})
            item["unavailable_sources"] = len(item["evidence_ids"]) - len(item["sources"])
        view["status"] = "ready"
        return view
    except Exception:
        # No fallback to generated quotations or unfiltered SQLite reads.
        return {"status": "unavailable", "items": []}


def source_review_location(locator: dict) -> str:
    names = {"sheet_name": "シート", "cell": "セル", "cell_range": "セル範囲",
             "row_index": "行", "page": "ページ", "page_number": "ページ",
             "slide": "スライド", "slide_number": "スライド"}
    parts = [f"{names[key]}：{value}" for key, value in locator.items() if key in names]
    return "／".join(parts) if parts else json.dumps(locator, ensure_ascii=False)


def source_review_notice(view: dict) -> str:
    status = view.get("status")
    if status == "hidden":
        return ""
    if status == "processing_error":
        return ('<section class="card"><h2>処理上の問題で回答を確認できませんでした</h2>'
                '<p>これは資料の文章が曖昧という判定ではありません。'
                '資料追加や書き直しを求める前に、診断ログで処理を確認する必要があります。</p></section>')
    if status != "ready":
        return ('<section class="card"><h2>確認用の原文を表示できませんでした</h2>'
                '<p>現在の資料・版・回答に利用できる根拠との対応を確認できないため、原文は表示していません。'
                '資料の書き方に問題があると判断したわけではありません。</p></section>')
    parts = ['<section class="card" aria-label="未確認部分の原文"><h2>判断できなかった部分を原文で確認</h2>'
             '<p>以下は検索した資料の記載で、確定した回答ではありません。'
             '資料の省略・曖昧さか、AIの読み落としかは、この表示だけでは決めつけません。</p>']
    for item in view["items"]:
        parts.append('<h3>' + html.escape(item["label"]) + '</h3><p>' + html.escape(item["reason"]) + '</p>')
        if item["model_note"]:
            parts.append('<p>モデルの判断メモ（原文ではなく、正しさは未確認）：<br>'
                         + html.escape(item["model_note"]) + '</p>')
        parts.append('<p>' + ('モデルが参照した箇所です。問題箇所と確定したものではありません。'
                             if item["selection"] == "model_reference" else
                             '問題の一文を特定できていないため、検索候補を表示しています。') + '</p>')
        for source in item["sources"]:
            locator = source_review_location(source["locator"])
            parts.append('<p>出典：' + html.escape(source["path"]) + ' / ' + html.escape(locator) + '</p>'
                         '<blockquote style="white-space:pre-wrap;overflow-wrap:anywhere">'
                         + html.escape(source["text"]) + '</blockquote>')
            if source["truncated"]:
                parts.append('<p>表示上限のため、ここまでの抜粋です。続きは原資料で確認してください。</p>')
        if not item["sources"]:
            parts.append('<p>表示できる該当原文を特定できませんでした。</p>')
        if item["omitted_sources"] or item["unavailable_sources"]:
            parts.append('<p>表示件数・文字数または安全確認の制限により、すべての候補は表示していません。</p>')
    if view.get("omitted_items"):
        parts.append('<p>未確認項目は先頭3件まで表示しています。</p>')
    parts.append('</section>')
    return ''.join(parts)


def answer_source_notice(record: dict) -> tuple[str, str, str]:
    """Render only the Evidence that belongs to the selected answer path."""
    promotion = record.get(SEMANTIC_GRAPH_ANSWER_PROMOTION_KEY)
    if (
        isinstance(promotion, dict)
        and promotion.get("decision") == "PROMOTE"
        and promotion.get("used_for_answers") is True
    ):
        references = promotion.get("source_references")
        references = references if isinstance(references, list) else []
        rows = []
        for item in references:
            if not isinstance(item, dict):
                continue
            path = html.escape(str(item.get("path", "(不明)")))
            locator = html.escape(
                json.dumps(item.get("locator", {}), ensure_ascii=False)
            )
            quote = html.escape(str(item.get("quote", "")))
            evidence_id = html.escape(str(item.get("evidence_id", "")))
            edge_id = html.escape(str(item.get("edge_id", "")))
            rows.append(
                f"<li>{path} / {locator}<br>「{quote}」"
                f"<br>Evidence: {evidence_id} / Edge: {edge_id}</li>"
            )
        return (
            "意味グラフで確認した根拠",
            "".join(rows) or "<li>根拠を表示できません</li>",
            "表示中の根拠は、回答に実際に使い、独立Edge監査で再構築したものです。",
        )
    rows = "".join(
        f"<li>{html.escape(str(item.get('relative_path', '(不明)')))} / "
        f"{html.escape(json.dumps(item.get('locator', {}), ensure_ascii=False))}</li>"
        for item in record.get("retrieved", [])[:8]
        if isinstance(item, dict)
    )
    return (
        "参照候補",
        rows or "<li>根拠候補なし</li>",
        "候補のファイル名は回答の正しさを自動で保証するものではありません。回答内の確認済み・暫定・未確認の区分と出典を確認してください。",
    )


def final_audit_incomplete(record: dict) -> bool:
    audit = record.get("independent_final_audit")
    performance = record.get("performance")
    performance = performance if isinstance(performance, dict) else {}
    audit_performance = performance.get("independent_final_audit")
    return (
        isinstance(audit, dict) and audit.get("status") == "incomplete"
    ) or (
        isinstance(audit_performance, dict)
        and audit_performance.get("failed") is True
    )


def save_audited_answer(record: dict) -> None:
    audited_log = bootstrap.SUPPORT / "logs" / "audited-answers.jsonl"
    _search_stage("save_audited_answer")
    audited_log.parent.mkdir(parents=True, exist_ok=True)
    with audited_log.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def answer_query(
    query: str,
    *,
    expected_active_revision: dict | None = None,
    confirmed_intent: dict | None = None,
) -> dict:
    pipeline_started = time.perf_counter()
    _search_stage("configuration")
    if expected_active_revision is None:
        # Non-HTTP callers retained for the bounded pipeline test harness.
        # Handler.do_POST always supplies the captured revision identity.
        config = bootstrap.load_json(bootstrap.CONFIG)
    else:
        config_exists, config = bootstrap.load_config_snapshot()
        if not config_exists:
            raise RuntimeError("answer_configuration_missing")
        if not bootstrap.answer_config_matches_revision(
            config, expected_active_revision
        ):
            raise RuntimeError("answer_revision_changed_before_query")
    if confirmed_intent is not None:
        intent_contract.graph_helper().compile_contract(confirmed_intent)
        if (confirmed_intent['question'] != query
                or confirmed_intent.get('expires_at', 0) < time.time()
                or (expected_active_revision is not None
                    and confirmed_intent['revision'] != expected_active_revision)):
            raise ValueError('intent_request_binding_mismatch')
    index = Path(config["index_path"])
    _search_stage("model_start")
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
    _search_stage("answer_generation")
    input_options = {}
    if confirmed_intent is not None:
        # Private stdin, not command-line arguments or a shared temporary file.
        command.append('--intent-contract-stdin')
        input_options['input'] = json.dumps(confirmed_intent, ensure_ascii=False)
    generated = subprocess.run(command, capture_output=True, text=True, timeout=900, check=True,
                               **input_options)
    answer_seconds = time.perf_counter() - answer_started
    record = json.loads(generated.stdout)
    if confirmed_intent is not None:
        intent_contract.validate_record_binding(confirmed_intent, record)
    _attach_search_request(record)
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
    legacy_record.pop(SEMANTIC_GRAPH_ANSWER_PROMOTION_KEY, None)
    legacy_record.pop("pre_semantic_graph_promotion_answer", None)
    with tempfile.NamedTemporaryFile("w", suffix=".json", encoding="utf-8", delete=False) as handle:
        json.dump(legacy_record, handle, ensure_ascii=False)
        temporary = Path(handle.name)
    try:
        audit_started = time.perf_counter()
        _search_stage("final_answer_audit")
        audited = subprocess.run([
            sys.executable, str(BASE / "final_answer_audit.py"), "--record", str(temporary),
            "--index", str(index), "--model", config["audit_model"],
        ], capture_output=True, text=True, timeout=600, check=True)
        audit_seconds = time.perf_counter() - audit_started
        audited_record = json.loads(audited.stdout)
        if confirmed_intent is not None:
            intent_contract.validate_record_binding(confirmed_intent, audited_record)
        _attach_search_request(audited_record)
        audited_record.pop(SEMANTIC_GRAPH_CANDIDATE_KEY, None)
        audited_record.pop(SEMANTIC_GRAPH_EDGE_AUDIT_KEY, None)
        audited_record.pop(SEMANTIC_GRAPH_ANSWER_PROMOTION_KEY, None)
        audited_record.pop("pre_semantic_graph_promotion_answer", None)
        audit_unload = (
            unload_ollama_model(config["audit_model"])
            if sequential else {"requested": False, "succeeded": False, "seconds": 0.0, "error": ""}
        )
        if final_audit_incomplete(audited_record):
            # An unfinished audit cannot be promoted by any downstream route.
            audited_record["pipeline_performance"] = {
                "sequential_model_loading": sequential,
                "same_model_reused_across_separate_contexts": reuse_loaded_model,
                "answer_process_seconds": round(answer_seconds, 3),
                "answer_model_unload": answer_unload,
                "audit_process_seconds": round(audit_seconds, 3),
                "audit_model_unload": audit_unload,
                "downstream_skipped_reason": "final_audit_incomplete",
                "total_seconds": round(time.perf_counter() - pipeline_started, 3),
            }
            save_audited_answer(audited_record)
            return audited_record
        candidate_started = time.perf_counter()
        _search_stage("semantic_graph_candidate")
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
            _search_stage("semantic_graph_edge_audit")
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
        _search_stage("semantic_graph_promotion")
        semantic_promotion_performance = apply_semantic_graph_answer_promotion(
            query,
            config,
            index,
            audited_record,
            semantic_candidate,
            semantic_edge_audit,
            edge_audit_reference_date,
        )
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
            "semantic_graph_answer_promotion": (
                semantic_promotion_performance
            ),
            "total_seconds": round(time.perf_counter() - pipeline_started, 3),
        }
        save_audited_answer(audited_record)
        return audited_record
    finally:
        if sequential and "audit_unload" not in locals():
            unload_ollama_model(config["audit_model"])
        temporary.unlink(missing_ok=True)


class Handler(BaseHTTPRequestHandler):
    server_version = "LocalMemorySearch/step5"

    def send_local_security_headers(self) -> None:
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; style-src 'unsafe-inline'; "
            "script-src 'self'; connect-src 'self'; "
            "form-action 'self'; base-uri 'none'; frame-ancestors 'none'",
        )
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")

    def send(self, content: bytes, status: int = 200) -> None:
        service = getattr(self.server, 'source_updates', None)
        if service is not None and b'id="source-updates"' not in content:
            view = service.snapshot()
            if view.get('candidates') or view.get('phase') in {'idle', 'scanning', 'partial', 'error', 'applied'}:
                notice = ('<p class="warn">資料の更新候補が未確認、または更新確認が未完了です。'
                          'この回答は現在の索引に基づき、最新版の保証ではありません。'
                          '<a href="/">資料の更新候補を確認する</a></p>').encode('utf-8')
                content = content.replace(b'<main class="wrap">', b'<main class="wrap">' + notice, 1)
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.send_local_security_headers()
        self.end_headers()
        self.wfile.write(content)

    def send_json(self, value: dict, status: int = 200) -> None:
        content = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.send_local_security_headers()
        self.end_headers()
        self.wfile.write(content)

    def redirect_home(self) -> None:
        self.send_response(303)
        self.send_header("Location", "/")
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        self.send_local_security_headers()
        self.end_headers()

    def send_javascript(self, content: bytes) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/javascript; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.send_local_security_headers()
        self.end_headers()
        self.wfile.write(content)

    def do_GET(self) -> None:
        if not _local_request_host_is_valid(self.server, self.headers):
            self.send_json({"status": "invalid_host"}, 421)
            return
        if self.path == SERVER_HEALTH_PATH:
            self.send_json(server_health_payload(
                self.server.instance_id,
                getattr(self.server, "startup_state", "ready"),
            ))
            return
        if self.path == "/local-memory-ui.js":
            self.send_javascript(UI_SCRIPT)
            return
        if self.path == "/source-updates/status":
            service = getattr(self.server, 'source_updates', None)
            if service is None or getattr(self.server, 'startup_state', 'ready') != 'ready':
                self.send_json({'status': 'update_unavailable'}, 503)
                return
            view = service.snapshot()
            self.send_json({'html': source_update_card(view, self.server.ui_csrf_token),
                            'busy': view.get('phase') in {'scanning', 'adopting'}})
            return
        if self.path != "/":
            self.send(page("<h1>404</h1>"), 404)
            return
        startup_state = getattr(self.server, "startup_state", "ready")
        if startup_state != "ready":
            message = (
                "起動時の復旧確認を実行中です。"
                if startup_state == "recovering"
                else "起動時の復旧確認に失敗しました。"
            )
            self.send(page(
                f'<section class="card"><h1>{message}</h1>'
                '<p>しばらく待ってから、もう一度アプリを開いてください。</p>'
                "</section>"
            ), 503)
            return
        self.send(home(
            csrf_token=self.server.ui_csrf_token,
            review_ticket_issuer=review_ticket_issuer(self.server),
            source_update_state=(self.server.source_updates.snapshot()
                                 if hasattr(self.server, 'source_updates') else None),
            source_selection_state=(self.server.source_selection.snapshot()
                                    if hasattr(self.server, 'source_selection') else None),
        ))

    def do_POST(self) -> None:
        if not _local_request_host_is_valid(self.server, self.headers):
            self.send_json({"status": "invalid_host"}, 421)
            return
        if self.path == SERVER_SHUTDOWN_PATH:
            supplied = self.headers.get("X-Local-Memory-Shutdown-Token", "")
            expected = getattr(self.server, "shutdown_token", "")
            if (
                not supplied
                or not expected
                or not hmac.compare_digest(supplied, expected)
            ):
                self.send_json({"status": "forbidden"}, 403)
                return
            if not _reserve_server_shutdown():
                self.send_json({"status": "busy"}, 409)
                return
            release_shutdown = threading.Event()

            def shutdown_after_response() -> None:
                release_shutdown.wait()
                self.server.shutdown()

            try:
                shutdown_worker = threading.Thread(
                    target=shutdown_after_response,
                    name="local-memory-graceful-shutdown",
                    daemon=True,
                )
                shutdown_worker.start()
            except Exception:
                _cancel_server_shutdown_reservation()
                self.send_json({"status": "shutdown_unavailable"}, 503)
                return
            try:
                self.send_json({"status": "shutting_down"}, 202)
            finally:
                release_shutdown.set()
            return
        if getattr(self.server, "startup_state", "ready") != "ready":
            self.send_json({"status": "server_starting"}, 503)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except (TypeError, ValueError):
            self.send_json({"status": "invalid_content_length"}, 400)
            return
        body_limit = (MAX_INTENT_FORM_BYTES
                      if self.path in {'/intent-preview', '/local-search-answer'}
                      else MAX_FORM_BYTES)
        if not 0 <= length <= body_limit:
            self.send_json({"status": "request_too_large"}, 413)
            return
        form = urllib.parse.parse_qs(self.rfile.read(length).decode("utf-8"))
        if not _local_ui_post_is_authorized(
            self.server,
            self.headers,
            form,
        ):
            if _local_ui_post_has_stale_csrf(
                self.server,
                self.headers,
                form,
            ):
                self.send(page(
                    '<a class="button secondary" href="/">← 最新の画面に戻る</a>'
                    '<section class="card"><h1>質問画面が更新されました</h1>'
                    '<p>アプリの更新・再起動・メモリのリセットにより、開いていた画面の'
                    '安全トークンが失効しました。質問はまだ検索に送られていません。</p>'
                    '<p>最新の画面に戻り、もう一度質問を入力してください。</p>'
                    '</section>'
                ), 403)
                return
            self.send_json({"status": "forbidden"}, 403)
            return
        if SOURCE_CHANGE_ACTIVE and self.path in {"/build", "/document-version-decision"}:
            self.send(page('<a href="/">← 戻る</a><section class="card"><h1>資料の切替処理中です</h1>'
                '<p>完了後に最新の画面で操作してください。今回は開始・保存していません。</p></section>'), 409)
            return
        if self.path == "/memory-reset":
            if not _reserve_memory_reset():
                self.send(page('<a href="/">← 戻る</a><section class="card"><h1>処理中です</h1>'
                    '<p>回答・取り込みが終わってから、メモリをリセットしてください。</p></section>'), 409)
                return
            try:
                intent_contract.SIGNING_KEY = secrets.token_urlsafe(32)
                self.server.ui_csrf_token = secrets.token_urlsafe(32)
            finally:
                _release_memory_reset()
            self.send(home("質問のメモリをリセットしました。新しい質問を入力できます。",
                           self.server.ui_csrf_token))
            return
        if self.path in {"/source-selection/pick", "/source-selection/build", "/source-selection/cancel"}:
            service = getattr(self.server, "source_selection", None)
            if service is None:
                self.send_json({"status": "source_selection_unavailable"}, 503)
                return
            if not _reserve_source_change():
                self.send(page('<a href="/">← 戻る</a><section class="card"><h1>処理中です</h1>'
                    '<p>回答・取り込み・更新確認が終わってから、フォルダを選んでください。</p></section>'), 409)
                return
            handed_off = False
            try:
                if self.path.endswith("/pick"):
                    service.pick()
                elif self.path.endswith("/cancel"):
                    service.cancel()
                else:
                    tickets = form.get("selection_ticket", [])
                    ticket = tickets[0] if len(tickets) == 1 else ""
                    candidate = service.consume(ticket, confirmed=form.get("confirmed") == ["yes"])
                    start_selected_source_build(self.server, candidate)
                    handed_off = True
            except Exception as exc:
                service.fail(exc)
            finally:
                if not handed_off:
                    _release_source_change()
            view = service.snapshot()
            if handed_off:
                # Refresh must GET the home/status page, never replay POST or
                # GET the POST-only build endpoint.
                self.redirect_home()
                return
            self.send(page('<a href="/">← 現在の状態と質問画面へ</a>'
                + source_selection_card(view, self.server.ui_csrf_token)),
                409 if view["phase"] == "error" else 200)
            return
        if self.path in {'/source-updates/scan', '/source-updates/adopt', '/source-updates/dismiss'}:
            service = getattr(self.server, 'source_updates', None)
            if service is None:
                self.send_json({'status': 'update_unavailable'}, 503)
                return
            action = self.path.rsplit('/', 1)[-1]
            ticket = form.get('candidate_ticket', [''])[0]
            try:
                if action == 'dismiss':
                    if not BUILD_LOCK.acquire(blocking=False):
                        raise ValueError('update_busy')
                    active = False
                    try:
                        active = _begin_active_work()
                        if not active:
                            raise ValueError('update_shutting_down')
                        service.dismiss(ticket)
                    finally:
                        if active:
                            _end_active_work()
                        BUILD_LOCK.release()
                else:
                    start_source_update(self.server, action, ticket,
                                        confirmed=form.get('confirmed') == ['yes'])
            except Exception:
                self.send(page('<a href="/">← 更新確認へ戻る</a><section class="card">'
                    '<h1>操作を開始できませんでした</h1><p>採用確認の不足、画面の期限切れ、'
                    '資料・設定の変更、または別の処理が実行中です。最新の画面で確認してください。</p></section>'), 409)
                return
            self.send(home('更新操作を受け付けました。', self.server.ui_csrf_token,
                           source_update_state=service.snapshot()))
            return
        if self.path == "/build":
            if SERVER_SHUTDOWN_REQUESTED.is_set():
                self.send_json({"status": "shutting_down"}, 503)
                return
            threading.Thread(target=build_worker, daemon=True).start()
            self.send(home(
                "セットアップを開始しました。",
                self.server.ui_csrf_token,
            ))
            return
        if self.path == "/document-version-decision":
            if not _begin_active_work():
                self.send(page('<p>資料の切替または終了処理中です。最新の画面で確認してください。</p>'), 409)
                return
            try:
                if state().get("phase") == "building":
                    self.send(home(
                        "索引作成中のため、資料版の選択を保留しました。",
                        self.server.ui_csrf_token,
                    ), 409)
                    return
                if str(form.get("review_ticket", [""])[0]).strip():
                    try:
                        should_rebuild = save_dated_review_submission(self.server, form)
                    except Exception:
                        self.send(home(
                            "資料または判断状態が表示後に変わったため、保存しませんでした。もう一度確認してください。",
                            self.server.ui_csrf_token,
                        ), 409)
                        return
                    if should_rebuild:
                        threading.Thread(target=build_worker, daemon=True).start()
                        message = "確認を保存し、索引の再構築を開始しました。"
                    else:
                        message = "判断を保留しました。これらの資料は回答に使いません。"
                    self.send(home(message, self.server.ui_csrf_token))
                    return
                group_id = str(form.get("group_id", [""])[0]).strip()
                selected = str(form.get("selected_relative_path", [""])[0]).strip()
                if not group_id or not selected:
                    self.send(home(
                        "現在使う資料を1つ選んでください。",
                        self.server.ui_csrf_token,
                    ), 400)
                    return
                process = subprocess.run(
                    [
                        sys.executable,
                        str(bootstrap.ENGINE / "document_version_resolver.py"),
                        "decide",
                        "--graph", str(bootstrap.DOCUMENT_VERSION_REVIEW),
                        "--decisions", str(bootstrap.DOCUMENT_VERSION_DECISIONS),
                        "--group-id", group_id,
                        "--select", selected,
                        "--actor", "local-ui-human",
                    ],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                )
                if process.returncode:
                    self.send(home(
                        "資料版の選択を記録できませんでした。候補を再確認してください。",
                        self.server.ui_csrf_token,
                    ), 409)
                    return
                threading.Thread(target=build_worker, daemon=True).start()
                self.send(home(
                    "選択を記録し、索引の再構築を開始しました。",
                    self.server.ui_csrf_token,
                ))
                return
            finally:
                _end_active_work()
        if self.path in {"/intent-dialog", "/intent-scope", "/intent-preview"}:
            query = str(form.get('query', [''])[0]).strip()
            csrf = self.server.ui_csrf_token
            if not 1 <= len(query) <= 2000:
                self.send(page('<p>質問は1〜2000文字で入力してください。</p><a href="/">戻る</a>'), 400)
                return
            if self.path == '/intent-dialog':
                self.send(intent_dialog(query, csrf))
                return
            if self.path == '/intent-scope':
                scope = form.get('scope', [''])[0]
                if scope not in {'workflow', 'fact', 'custom'}:
                    self.send(intent_dialog(query, csrf), 400)
                    return
                self.send(intent_editor(query, scope, csrf))
                return
            current, _, revision = bootstrap.active_answer_revision_identity()
            if not current or revision is None:
                self.send(intent_editor(query, 'custom', csrf,
                    goal=form.get('goal', [''])[0], requirements=form.get('requirements', [''])[0],
                    notice='資料の更新が完了してから、もう一度確認してください。入力内容は保持しています。'), 409)
                return
            try:
                contract = intent_contract.make_contract(query, form.get('goal', [''])[0], form.get('requirements', [''])[0], revision)
                self.send(intent_preview(contract, csrf))
            except ValueError as exc:
                self.send(intent_editor(query, 'custom', csrf,
                    goal=form.get('goal', [''])[0], requirements=form.get('requirements', [''])[0],
                    notice=str(exc) + ' 入力内容は保持しています。該当箇所だけ修正してください。'), 400)
            return
        if self.path in {"/ask", "/local-search-answer"}:
            if state().get("phase") not in {"ready", "ready_with_limits"}:
                self.send(home(
                    "索引の世代が完了していないため、質問を保留しました。",
                    self.server.ui_csrf_token,
                ), 409)
                return
            decision_current, _decision_reason, answer_revision = (
                bootstrap.active_answer_revision_identity()
            )
            if not decision_current or answer_revision is None:
                self.send(home(
                    "資料の判断が更新され、対応する索引がまだ完成していないため、回答を保留しました。",
                    self.server.ui_csrf_token,
                ), 409)
                return
            query = str(form.get("query", [""])[0]).strip()
            if not query:
                self.send(home(
                    "質問を入力してください。",
                    self.server.ui_csrf_token,
                ), 400)
                return
            try:
                contract = intent_contract.verify(
                    form.get('intent_payload', [''])[0], form.get('intent_signature', [''])[0],
                    intent_contract.SIGNING_KEY, answer_revision)
                if query != contract['question']:
                    raise ValueError('質問が確認時から変更されています。')
            except (ValueError, KeyError, TypeError):
                try:
                    previous = intent_contract.read_signed(form.get('intent_payload', [''])[0],
                        form.get('intent_signature', [''])[0], intent_contract.SIGNING_KEY)
                    self.send(intent_editor(previous['question'], 'custom', self.server.ui_csrf_token,
                        previous['goal'], '\n'.join(previous['requirements']),
                        notice='資料または確認内容の状態が変わりました。入力を残したので、もう一度完成形を確認してください。'), 409)
                    return
                except (ValueError, KeyError, TypeError):
                    pass
                self.send(intent_dialog(query, self.server.ui_csrf_token), 409)
                return
            if form.get('intent_action', [''])[0] == 'edit':
                self.send(intent_editor(query, 'custom', self.server.ui_csrf_token,
                    contract['goal'], '\n'.join(contract['requirements'])))
                return
            if form.get('intent_action', [''])[0] != 'confirm':
                self.send(intent_preview(contract, self.server.ui_csrf_token), 409)
                return
            if not _begin_active_work():
                self.send_json({"status": "source_changing" if SOURCE_CHANGE_ACTIVE else "shutting_down"}, 503)
                return
            request_context = {
                "request_id": str(uuid.uuid4()),
                "request_started_at": datetime.now().astimezone().isoformat(timespec="milliseconds"),
                "question": query,
                "stage": "answer_pipeline",
                "_sensitive_values": tuple(str(form.get(key, [""])[0]) for key in (
                    UI_CSRF_FIELD, "intent_payload", "intent_signature")),
            }
            self.search_request_id = request_context["request_id"]
            SEARCH_REQUEST_CONTEXT.value = request_context
            _log_search_event(request_context, "request_started")
            try:
                record = answer_query(
                    intent_contract.search_question(contract),
                    expected_active_revision=answer_revision,
                )
                _attach_search_request(record)
                incomplete = final_audit_incomplete(record)
                guidance_view = None
                if incomplete:
                    coverage = {"complete": False, "items": []}
                else:
                    guidance_view = prepare_grounded_guidance(record, contract, answer_revision)
                    if guidance_view is not None:
                        coverage = guidance_view["coverage"]
                        record["confirmed_intent"] = contract
                        record["intent_coverage"] = coverage
                    else:
                        _search_stage("intent_coverage")
                        coverage = audit_intent_coverage(contract, record)
                        if record.get("grounded_guidance", {}).get("status") == "incomplete":
                            coverage["complete"] = False
                source_review = ({"status": "hidden", "items": []} if incomplete or guidance_view is not None
                                 else prepare_source_review(record, answer_revision))
                if source_review["status"] != "hidden":
                    coverage["complete"] = False
                _search_stage("final_revision_check")
                decision_current, _decision_reason, final_revision = (
                    bootstrap.active_answer_revision_identity()
                )
                if (
                    not decision_current
                    or final_revision is None
                    or final_revision != answer_revision
                ):
                    _log_search_event(request_context, "answer_withheld_revision_changed", record=record, coverage=coverage)
                    self.send(home(
                        "回答作成中に資料の判断が変わったため、作成済みの回答を表示しませんでした。",
                        self.server.ui_csrf_token,
                    ), 409)
                    return
                if guidance_view is not None and grounded_guidance.verified_view(record, contract) != guidance_view:
                    _log_search_event(request_context, "guidance_withheld_binding_changed", record=record)
                    self.send(page('<section class="card"><h1>案内例の確認状態が変わりました</h1>'
                        '<p>確認済みの文章との一致を確かめられないため、案内例は表示していません。</p>'
                        '<a class="button secondary" href="/">← 戻る</a></section>'), 409)
                    return
                save_grounded_guidance(record, contract, guidance_view, coverage)
                _search_stage("response_rendering")
                if incomplete:
                    audit = record.get("independent_final_audit", {})
                    reason = audit.get("reason_code") if isinstance(audit, dict) else None
                    explanation = {
                        "audit_context_exhausted": "最終点検に必要な容量を使い切りました。",
                        "audit_response_truncated": "最終点検の応答が途中で終了しました。",
                        "audit_transport_error": "最終点検を行うローカルモデルとの通信を完了できませんでした。",
                    }.get(reason, "最終点検の応答が完全であることを確認できませんでした。")
                    self.send(page('<a class="button secondary" href="/">← 戻る</a>'
                        '<section class="card"><h1>回答の最終点検を完了できませんでした</h1>'
                        '<p>' + explanation + '</p><p>資料不足という判定ではありません。'
                        '未点検の回答は表示していません。診断ログに原因を記録しました。</p>'
                        '<p>要求ID: <code>' + html.escape(request_context['request_id']) + '</code></p></section>'))
                    _log_search_event(request_context, "request_incomplete", record=record, coverage=coverage)
                    return
                answer = record["answer"]
                if guidance_view is not None:
                    answer = {"answer": guidance_view["answer"], "answer_mode": "grounded_guidance"}
                elif source_review["status"] == "processing_error" and not (
                    record.get("independent_final_audit", {}).get("verdict") == "verified"
                    and record.get("answerability_policy", {}).get("applied") is True
                    and record.get("answerability_policy", {}).get("confirmed_field_ids")
                    and answer.get("non_answer_reason", {}).get("code") != "machine_validation_failure"
                ):
                    answer = {"answer": "検索・回答作成・検証の処理が正常に完了していないため、回答を保留しています。",
                              "answer_mode": "processing_error"}
                completion_title = intent_contract.coverage_heading(coverage)
                if source_review["status"] == "processing_error":
                    completion_title = '回答処理の確認が必要です'
                coverage_notice = '<section class="card"><h2>' + completion_title + '</h2><p>' + html.escape(contract['goal']) + '</p><ul>'
                for item in coverage['items']:
                    label = ('説明あり：' if item['covered'] else '判定できず：'
                             if item.get('status') == 'unavailable' else '確認不足：')
                    coverage_notice += '<li>' + label + html.escape(item['requirement']) + '</li>'
                coverage_notice += '</ul>'
                if coverage.get('status') == 'unavailable':
                    coverage_notice += '<p>充足確認の処理を完了できなかったため、資料や回答の不足とは断定していません。</p>'
                coverage_notice += '<p>以下は確認できた範囲です。完成形の確認は意味判断を含み、誤判定の可能性があります。</p></section>'
                payload, signature = intent_contract.seal(contract, intent_contract.SIGNING_KEY)
                revise_form = intent_form('/local-search-answer', self.server.ui_csrf_token,
                    intent_hidden('query', contract['question']) + intent_hidden('intent_payload', payload)
                    + intent_hidden('intent_signature', signature) + intent_hidden('intent_action', 'edit')
                    + '<button>完成形を修正する（入力内容を保持）</button>')
                audit = record.get("independent_final_audit", {})
                certainty_notice = answerability_notice(record)
                semantic_candidate = semantic_graph_candidate_notice(record)
                source_heading, sources, source_note = answer_source_notice(
                    record
                )
                if guidance_view is not None:
                    certainty_notice = ('<p class="small">確認した資料をもとに案内例を作成しました。'
                        '原文引用ではなく、別の点検で根拠・対象・条件・要求との対応を確認した表現です。</p>')
                    source_heading, sources, source_note = grounded_guidance_sources(guidance_view)
                elif record.get("grounded_guidance", {}).get("status") == "incomplete":
                    certainty_notice += ('<div class="warn"><p>案内例の作成・点検は完了していません。'
                        '以下には、元の資料から確認できた範囲を表示しています。</p></div>')
                promotion = record.get(SEMANTIC_GRAPH_ANSWER_PROMOTION_KEY)
                graph_promoted = (
                    isinstance(promotion, dict)
                    and promotion.get("decision") == "PROMOTE"
                    and promotion.get("used_for_answers") is True
                )
                graph_route = record.get("graph_route")
                question_graph_used = (
                    isinstance(graph_route, dict)
                    and graph_route.get("used") is True
                )
                answer_route = (
                    "意味グラフ"
                    if graph_promoted else
                    "質問グラフ（構造検索）"
                    if question_graph_used else
                    "従来検索"
                )
                edge_audit = record.get(SEMANTIC_GRAPH_EDGE_AUDIT_KEY, {})
                audit_label = (
                    "意味グラフ独立Edge監査: "
                    + audit_verdict_notice(edge_audit)
                    + "<br>従来回答の独立監査: "
                    + audit_verdict_notice(audit)
                    if graph_promoted
                    else "独立監査: "
                    + audit_verdict_notice(audit)
                )
                if guidance_view is not None:
                    audit_label = ("原文抽出の独立監査: " + audit_verdict_notice(audit)
                        + "<br>案内例の別コンテキスト監査: 確認済み（同じローカルモデル・別の点検役割）")
                self.send(page(f"""
                <a class="button secondary" href="/">← 戻る</a><div class="eyebrow">AUDITED ANSWER</div><h1>{html.escape(query)}</h1>
                {coverage_notice}
                {revise_form}
                <section class="card">{certainty_notice}<div class="answer">{html.escape(str(answer.get('answer','')))}</div><p class="small">回答モード: {html.escape(str(answer.get('answer_mode','')))}<br>回答経路: {html.escape(answer_route)}<br>{audit_label}<br>要求ID: {request_context['request_id']}</p></section>
                {intent_form('/memory-reset', self.server.ui_csrf_token,
                    '<button class="secondary">メモリをリセットして新しい質問へ</button>')}
                {source_review_notice(source_review)}
                <section class="card"><h2>{html.escape(source_heading)}</h2><ul>{sources}</ul><p class="small">{html.escape(source_note)}</p></section>
                {semantic_candidate}
                {security_exclusion_notice()}
                """))
                _log_search_event(request_context, "request_completed", record=record, coverage=coverage)
            except Exception as exc:
                logged = _log_search_event(request_context, "request_failed", exc=exc)
                if not isinstance(exc, (BrokenPipeError, ConnectionResetError)):
                    log_notice = ("詳しい原因は診断ログに記録しました。" if logged
                                  else "診断ログも保存できませんでした。要求IDを控えてください。")
                    try:
                        self.send(page(f'<a class="button secondary" href="/">← 戻る</a><section class="card"><h1>回答の処理を完了できませんでした</h1><p>検索、回答作成、または検証の途中で処理上の問題が発生しました。資料が不足しているかどうかは、このエラーだけでは判断できません。</p><p>{log_notice}</p><p>要求ID: <code>{request_context["request_id"]}</code></p></section>'), 500)
                    except (BrokenPipeError, ConnectionResetError) as send_exc:
                        request_context["stage"] = "error_response_delivery"
                        _log_search_event(request_context, "response_delivery_failed", exc=send_exc)
            finally:
                SEARCH_REQUEST_CONTEXT.value = None
                _end_active_work()
            return
        self.send(page("<h1>404</h1>"), 404)

    def log_message(self, format: str, *args) -> None:
        if self.path == SERVER_HEALTH_PATH:
            return
        path = bootstrap.SUPPORT / "logs" / "server.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            timestamp = datetime.now().astimezone().isoformat(timespec="milliseconds")
            request_id = getattr(self, "search_request_id", None)
            suffix = f" request_id={request_id}" if request_id else ""
            handle.write(f"[{timestamp}] " + (format % args) + suffix + "\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    if args.host not in {"127.0.0.1", "localhost"}:
        raise SystemExit("remote binding is forbidden")
    if not 1 <= args.port <= 65535:
        raise SystemExit("port must be between 1 and 65535")
    with ThreadingHTTPServer((args.host, args.port), Handler) as server:
        server.instance_id = secrets.token_hex(16)
        server.shutdown_token = secrets.token_urlsafe(32)
        server.ui_csrf_token = secrets.token_urlsafe(32)
        server.review_ticket_lock = threading.Lock()
        server.review_tickets = {}
        server.source_updates = source_updates.SourceUpdates(bootstrap)
        server.source_selection = source_selection.SourceSelection(bootstrap)
        server.startup_state = "recovering"
        if not _begin_active_work():
            raise RuntimeError("server_startup_shutdown_already_requested")
        startup_work_needs_release = True
        try:
            _publish_server_identity(server, args.port)

            def recover_before_requests() -> None:
                startup_outcome = _startup_recovery_outcome()
                _end_active_work()
                # Publish the terminal startup state only after recovery has
                # left the active-work set.  The launcher treats ``failed`` as
                # a safe point for authenticated shutdown; exposing it any
                # earlier can race with the shutdown reservation and strand
                # the failed child.
                server.startup_state = startup_outcome
                if startup_outcome == 'ready':
                    try:
                        start_source_update(server, 'scan')
                    except Exception as exc:
                        server.source_updates.fail(exc)

            recovery_thread = threading.Thread(
                target=recover_before_requests,
                name="local-memory-startup-recovery",
                daemon=True,
            )
            recovery_thread.start()
            startup_work_needs_release = False
            server.serve_forever()
        finally:
            if startup_work_needs_release:
                _end_active_work()
            _remove_server_identity(server.instance_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
