"""チャンク化と BM25 検索.

日本語の形態素解析器に依存せず、ASCII 単語 + 文字バイグラムで索引を張る。
外部サービスを使わないので、この部分はオフラインで完結する。
"""

from __future__ import annotations

import json
import math
import re
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

CHUNK_CHARS = 1400
CHUNK_OVERLAP = 200

K1 = 1.5
B = 0.75

_ASCII_RE = re.compile(r"[0-9A-Za-z_][0-9A-Za-z_.\-]*")
_JA_RE = re.compile(r"[぀-ヿ㐀-鿿豈-﫿]+")


def tokenize(text: str) -> list:
    """ASCII 語 + 日本語文字バイグラムに分解する."""
    text = unicodedata.normalize("NFKC", text).lower()
    toks = [m.group(0) for m in _ASCII_RE.finditer(text)]
    for m in _JA_RE.finditer(text):
        s = m.group(0)
        if len(s) == 1:
            toks.append(s)
        else:
            toks += [s[i:i + 2] for i in range(len(s) - 1)]
    return toks


@dataclass
class Chunk:
    cid: int
    path: str
    project: str
    filename: str
    kind: str
    location: str
    text: str

    def header(self) -> str:
        loc = f" / {self.location}" if self.location else ""
        return f"[{self.path}{loc}]"

    def is_stub(self) -> bool:
        """未処理資産の目印だけを持つチャンクか（本文を持たない）."""
        t = self.text
        return (
            "※OCR/VLM未実施" in t
            or t.startswith("[テキスト層なし")
            or t.startswith("[画像ファイル")
        ) and len(t) < 400

    def index_text(self) -> str:
        """BM25 の対象。案件名を含むフルパスは入れない（ノイズになるため）."""
        parent = self.path.split("/")[-2] if "/" in self.path else ""
        return f"{self.filename} {parent} {self.location}\n{self.text}"


def make_chunks(sections) -> list:
    """Section を検索単位のチャンクに割る."""
    chunks = []
    for sec in sections:
        text = sec.text
        if len(text) <= CHUNK_CHARS:
            pieces = [text]
        else:
            pieces, start = [], 0
            step = CHUNK_CHARS - CHUNK_OVERLAP
            while start < len(text):
                pieces.append(text[start:start + CHUNK_CHARS])
                start += step
        for piece in pieces:
            if piece.strip():
                chunks.append(Chunk(
                    cid=len(chunks), path=sec.path, project=sec.project,
                    filename=sec.filename, kind=sec.kind,
                    location=sec.location, text=piece,
                ))
    return chunks


class BM25:
    def __init__(self, docs):
        self.docs = docs
        self.tf = []
        self.len = []
        df = Counter()
        for d in docs:
            toks = tokenize(d)
            c = Counter(toks)
            self.tf.append(c)
            self.len.append(len(toks))
            df.update(c.keys())
        n = max(len(docs), 1)
        self.avg = sum(self.len) / n
        self.idf = {t: math.log(1 + (n - v + 0.5) / (v + 0.5)) for t, v in df.items()}
        self.postings = defaultdict(list)
        for i, c in enumerate(self.tf):
            for t in c:
                self.postings[t].append(i)

    def scores(self, query: str) -> dict:
        qt = Counter(tokenize(query))
        out: dict = defaultdict(float)
        for t, qn in qt.items():
            idf = self.idf.get(t)
            if idf is None:
                continue
            for i in self.postings[t]:
                f = self.tf[i][t]
                denom = f + K1 * (1 - B + B * self.len[i] / (self.avg or 1))
                out[i] += idf * (f * (K1 + 1)) / (denom or 1) * min(qn, 3)
        return out


class Index:
    """チャンク集合 + BM25 + 案件名によるブースト."""

    def __init__(self, chunks):
        self.chunks = chunks
        self.bm25 = BM25([c.index_text() for c in chunks])
        self.projects = sorted({c.project for c in chunks if c.project})

    def _target_projects(self, query: str, extra_terms) -> set:
        """質問文（および用語展開後の正式名称）から対象案件を推定する."""
        hay = query + " " + " ".join(extra_terms)
        hay = unicodedata.normalize("NFKC", hay)
        hit = set()
        for p in self.projects:
            name = unicodedata.normalize("NFKC", p)
            core = re.sub(r"(株式会社|医療法人社団|有限会社)", "", name).strip()
            for token in [name] + core.split():
                if len(token) >= 3 and token in hay:
                    hit.add(p)
                    break
        return hit

    def _strip_project_names(self, text: str, targets) -> str:
        """案件の絞り込みに使った固有名詞はクエリから外す（BM25を汚すため）."""
        out = unicodedata.normalize("NFKC", text)
        for p in targets:
            name = unicodedata.normalize("NFKC", p)
            for token in [name] + re.sub(
                r"(株式会社|医療法人社団|有限会社)", " ", name
            ).split():
                if len(token) >= 3:
                    out = out.replace(token, " ")
        return out

    def search_with_scores(self, query: str, extra_terms=(), top_k: int = 12) -> list:
        """再ランキング後のスコアとチャンクを返す.

        回答経路と同じ検索処理を診断レポートから再利用できるようにする。
        """
        targets = self._target_projects(query, extra_terms)
        terms = [t for t in extra_terms if t not in targets]
        expanded = self._strip_project_names(
            query + "\n" + "\n".join(terms), targets
        )
        raw = self.bm25.scores(expanded)
        qn = unicodedata.normalize("NFKC", query).lower()
        fname_tokens = set(
            m.group(0).lower()
            for m in re.finditer(r"[0-9A-Za-z_][0-9A-Za-z_.\-]{2,}", qn)
        )
        ranked = []
        for i, s in raw.items():
            c = self.chunks[i]
            fn = unicodedata.normalize("NFKC", c.filename).lower()
            named = any(t in fn for t in fname_tokens if len(t) >= 4)
            if named:
                s *= 2.5        # 質問がファイル名を名指ししている場合
            elif c.is_stub():
                # 未処理資産のスタブは本文を持たない。名指しされていない限り、
                # 本文チャンクの検索枠を奪わないよう減点する。
                s *= 0.3
            if targets:
                if c.project in targets:
                    s *= 3.0
                elif c.project:
                    s *= 0.25   # 別案件は大きく減点
                else:
                    s *= 1.2    # 社内管理などの共通資料は少し優遇
            ranked.append((s, i))
        ranked.sort(reverse=True)
        return [(score, self.chunks[i]) for score, i in ranked[:top_k]]

    def search(self, query: str, extra_terms=(), top_k: int = 12) -> list:
        return [
            chunk
            for _, chunk in self.search_with_scores(query, extra_terms, top_k)
        ]


def save_chunks(chunks, path: Path) -> None:
    with path.open("w", encoding="utf-8") as fh:
        for c in chunks:
            fh.write(json.dumps(c.__dict__, ensure_ascii=False) + "\n")


def load_chunks(path: Path) -> list:
    out = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            out.append(Chunk(**json.loads(line)))
    return out
