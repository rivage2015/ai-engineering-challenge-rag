#!/usr/bin/env python3
"""Localhost-only web UI for the packaged local-memory system."""

from __future__ import annotations

import argparse
import html
import json
import os
import subprocess
import sys
import tempfile
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import bootstrap


BUILD_LOCK = threading.Lock()
BASE = Path(__file__).resolve().parent
ENGINE = BASE / "engine"


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


def home(message: str = "") -> bytes:
    diagnosis = bootstrap.diagnose()
    current = state()
    ready = diagnosis["index_ready"]
    models = " / ".join(diagnosis["models"]) or "未確認"
    notices = "".join(f'<p class="warn">{html.escape(item)}</p>' for item in diagnosis["warnings"])
    transient = f'<p class="ok">{html.escape(message)}</p>' if message else ""
    setup = ""
    if current["phase"] == "building":
        setup = '<p class="progress">索引を作成中です。ファイル数と初回モデル取得により時間がかかります。この画面は自動更新します。</p>'
    elif current["phase"] == "error":
        setup = f'<p class="bad">{html.escape(current["message"])}<br><span class="small">{html.escape(current.get("error", ""))}</span></p><form method="post" action="/build"><button>再実行</button></form>'
    elif not ready:
        setup = '<p>初回だけ、ローカルモデルの確認と索引作成を行います。ファイルは外部へ送信しません。</p><form method="post" action="/build"><button>初回セットアップを開始</button></form>'
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
    <section class="card"><details><summary>プライバシーと制限</summary><p class="small">質問・回答・索引は <code>~/Library/Application Support/LocalMemorySearch</code> に保存されます。通常利用中のAI処理は127.0.0.1のOllamaのみです。初回のOllama導入・モデル取得にはインターネットが必要です。画像・音声・動画は現在、ファイル名とメタデータだけを索引化します。</p></details></section>
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


def answer_query(query: str) -> dict:
    config = bootstrap.load_json(bootstrap.CONFIG)
    index = Path(config["index_path"])
    log = bootstrap.SUPPORT / "logs" / "answers.jsonl"
    cache = bootstrap.SUPPORT / "data" / "answer-cache-v2.jsonl"
    command = [
        sys.executable, str(ENGINE / "answer_local_memory_v2.py"), query,
        "--index", str(index), "--model", config["answer_model"],
        "--audit-mode", "batched", "--fast-plan", "--log", str(log),
        "--cache", str(cache), "--json",
    ]
    generated = subprocess.run(command, capture_output=True, text=True, timeout=900, check=True)
    record = json.loads(generated.stdout)
    with tempfile.NamedTemporaryFile("w", suffix=".json", encoding="utf-8", delete=False) as handle:
        json.dump(record, handle, ensure_ascii=False)
        temporary = Path(handle.name)
    try:
        audited = subprocess.run([
            sys.executable, str(BASE / "final_answer_audit.py"), "--record", str(temporary),
            "--index", str(index), "--model", config["audit_model"],
        ], capture_output=True, text=True, timeout=600, check=True)
        audited_record = json.loads(audited.stdout)
        audited_log = bootstrap.SUPPORT / "logs" / "audited-answers.jsonl"
        audited_log.parent.mkdir(parents=True, exist_ok=True)
        with audited_log.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(audited_record, ensure_ascii=False) + "\n")
        return audited_record
    finally:
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
            query = str(form.get("query", [""])[0]).strip()
            if not query:
                self.send(home("質問を入力してください。"), 400)
                return
            try:
                record = answer_query(query)
                answer = record["answer"]
                audit = record.get("independent_final_audit", {})
                sources = "".join(
                    f"<li>{html.escape(item['relative_path'])} / {html.escape(json.dumps(item['locator'], ensure_ascii=False))}</li>"
                    for item in record.get("retrieved", [])[:8]
                )
                self.send(page(f"""
                <a class="button secondary" href="/">← 戻る</a><div class="eyebrow">AUDITED ANSWER</div><h1>{html.escape(query)}</h1>
                <section class="card"><div class="answer">{html.escape(str(answer.get('answer','')))}</div><p class="small">回答モード: {html.escape(str(answer.get('answer_mode','')))}<br>独立監査: {html.escape(str(audit.get('verdict','未実行')))} — {html.escape(str(audit.get('reason','')))}</p></section>
                <section class="card"><h2>参照候補</h2><ul>{sources or '<li>根拠候補なし</li>'}</ul><p class="small">候補のファイル名は回答の正しさを自動で保証するものではありません。監査不合格時は回答を停止します。</p></section>
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
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
