"""検索結果をもとに OpenAI またはローカル Ollama で回答を生成する."""

from __future__ import annotations

import os
import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Protocol

BACKEND = os.environ.get("RAG_BACKEND", "ollama").strip().lower()
MODEL = os.environ.get(
    "RAG_MODEL",
    "gemma4:12b" if BACKEND == "ollama" else "gpt-5.2",
)
OLLAMA_BASE_URL = os.environ.get("OLLAMA_BASE_URL", "http://127.0.0.1:11434")
MAX_CONTEXT_CHARS = 60000

SYSTEM = """あなたは社内共有ドライブの資料にもとづいて質問に答えるアシスタントです。

厳守事項:
1. 与えられた【資料】に書かれている内容のみを根拠にしてください。資料にない情報を
   推測や一般知識で補ってはいけません。
2. 根拠が資料から読み取れない場合、必ず「わかりません」とだけ答えてください。
   誤った回答は減点されますが、「わかりません」は減点されません。推測で答えないでください。
3. 回答は結論のみを簡潔に書いてください。前置き・説明・根拠の引用は不要です。
4. 質問文に単位・小数桁・丸め方・表記の指定がある場合は、必ずそれに従ってください。
5. 「すべて挙げてください」と指示された場合、資料から完全な一覧が確定できるときのみ
   列挙してください。抜け漏れの可能性があるなら「わかりません」と答えてください。
6. 社内用語・略称ではなく通常の表現で答えてください。ただし資料内で定義された
   タスクID・アクションID・マイルストーンID・列名・パラメータ名などの識別子は、
   資料上の表記どおりに書いてください。
7. 条件に該当するものが資料上存在しない場合は、該当するものがない旨を答えてください。
"""

USER_TEMPLATE = """【資料】
{context}

【質問】
{question}

上記の資料のみを根拠に、結論だけを簡潔に答えてください。
根拠が資料から確認できない場合は「わかりません」とだけ答えてください。"""


def build_context(chunks) -> str:
    parts, total = [], 0
    for c in chunks:
        block = f"--- {c.header()}\n{c.text}\n"
        if total + len(block) > MAX_CONTEXT_CHARS:
            break
        parts.append(block)
        total += len(block)
    return "\n".join(parts)


class AnswerClient(Protocol):
    backend: str
    model: str

    def check(self) -> None: ...

    def generate(self, messages: list[dict[str, str]]) -> str: ...


@dataclass
class OllamaAnswerClient:
    model: str = MODEL
    base_url: str = OLLAMA_BASE_URL
    timeout: float = 600.0
    backend: str = "ollama"

    def _request(self, path: str, payload=None, timeout: float | None = None):
        url = self.base_url.rstrip("/") + path
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            url, data=data, headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout or self.timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"ローカルOllamaへの接続に失敗しました: {url}: {exc}") from exc

    def check(self) -> None:
        response = self._request("/api/tags", timeout=30.0)
        names = {
            item.get("name") or item.get("model")
            for item in response.get("models", [])
        }
        requested = self.model if ":" in self.model else f"{self.model}:latest"
        if self.model not in names and requested not in names:
            raise RuntimeError(
                f"Ollamaモデルが見つかりません: {self.model}。"
                f" `ollama pull {self.model}` を実行してください。"
            )

    def generate(self, messages: list[dict[str, str]]) -> str:
        response = self._request("/api/chat", {
            "model": self.model,
            "messages": messages,
            "stream": False,
            # Reasoning-capable local models may spend the entire output budget
            # in ``message.thinking`` and return no final answer.  The task only
            # needs the concise grounded answer, so disable exposed thinking.
            "think": False,
            "keep_alive": "10m",
            "options": {
                "temperature": 0,
                "seed": 42,
                "num_ctx": 65536,
                "num_predict": 768,
            },
        })
        message = response.get("message") or {}
        return str(message.get("content") or "").strip()


@dataclass
class OpenAIAnswerClient:
    model: str = MODEL
    backend: str = "openai"

    def __post_init__(self) -> None:
        from openai import OpenAI

        self.client = OpenAI()

    def check(self) -> None:
        self.client.models.list()

    def generate(self, messages: list[dict[str, str]]) -> str:
        kwargs = dict(model=self.model, messages=messages)
        try:
            response = self.client.chat.completions.create(
                temperature=0, seed=42, **kwargs
            )
        except Exception:
            response = self.client.chat.completions.create(**kwargs)
        return (response.choices[0].message.content or "").strip()


def make_client(
    backend: str = BACKEND,
    model: str = MODEL,
    timeout: float = 180.0,
) -> AnswerClient:
    backend = backend.strip().lower()
    if backend == "ollama":
        return OllamaAnswerClient(model=model, timeout=timeout)
    if backend == "openai":
        return OpenAIAnswerClient(model=model)
    raise ValueError("RAG_BACKEND は ollama または openai を指定してください")


def answer_question(client: AnswerClient, question: str, chunks, glossary=None) -> str:
    q = glossary.expand(question) if glossary else question
    user = USER_TEMPLATE.format(context=build_context(chunks), question=q)
    text = client.generate([
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": user},
    ])
    # Source documents can contain presentation/export markup.  Keep answer
    # cells plain text without changing their semantic content.
    text = re.sub(r"</?[A-Za-z][^>]{0,200}>", "", text).strip()
    return text or "わかりません"
