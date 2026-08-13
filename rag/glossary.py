"""社内用語集の自動検出と、質問文の略称展開.

共有ドライブ内の docx を走査し、「社内用語 / 略称」列を持つ表を
用語辞書として取り込む。特定のファイル名・案件名には依存しない。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

# 用語表のヘッダに現れうる列名（この語を含む列を、その役割の列とみなす）
COL_ALIAS = ("社内用語", "略称", "主略称", "略号")
COL_CANON = ("正式名称", "案件名", "正式")
COL_OTHER = ("別名", "別名候補")

# 1文字のASCII略称は誤検出が多いため使わない
MIN_ASCII_ALIAS = 2
MIN_JA_ALIAS = 2


@dataclass
class Glossary:
    """alias -> 正式名称候補（複数ありうる）の辞書."""

    entries: dict = field(default_factory=dict)

    def add(self, alias: str, canonical: str) -> None:
        alias = alias.strip()
        canonical = canonical.strip()
        if not alias or not canonical or alias == canonical:
            return
        if _is_ascii(alias):
            if len(alias) < MIN_ASCII_ALIAS:
                return
        elif len(alias) < MIN_JA_ALIAS:
            return
        vals = self.entries.setdefault(alias, [])
        if canonical not in vals:
            vals.append(canonical)

    def __len__(self) -> int:
        return len(self.entries)

    def lookup(self, text: str):
        """text 中に出現する略称を、長いものから順に返す."""
        hits = []
        for alias in sorted(self.entries, key=len, reverse=True):
            if _contains(text, alias):
                hits.append((alias, self.entries[alias]))
        return hits

    def expand(self, question: str) -> str:
        """質問文に用語展開の注記を付けて返す（元の文は壊さない）."""
        hits = self.lookup(question)
        if not hits:
            return question
        lines = [f"- {a} = " + " / ".join(c) for a, c in hits]
        return question + "\n\n[社内用語の展開]\n" + "\n".join(lines)

    def aliases_in(self, question: str):
        """質問文に出現した略称の展開先（検索語の拡張に使う）."""
        out = []
        for _, canons in self.lookup(question):
            for c in canons:
                if c not in out:
                    out.append(c)
        return out


def _is_ascii(s: str) -> bool:
    return all(ord(ch) < 128 for ch in s)


def _contains(text: str, alias: str) -> bool:
    """ASCII略称は語境界つき、日本語略称は単純な部分一致で判定."""
    if _is_ascii(alias):
        pattern = r"(?<![0-9A-Za-z_-])" + re.escape(alias) + r"(?![0-9A-Za-z_-])"
        return re.search(pattern, text) is not None
    return alias in text


def _split_alternatives(cell: str):
    return [p.strip() for p in re.split(r"[,、/／・]", cell) if p.strip()]


def _column_index(header, keys):
    for i, name in enumerate(header):
        if any(k in name for k in keys):
            return i
    return None


def _load_docx_tables(path: Path):
    try:
        from docx import Document
    except ImportError:
        return []
    try:
        doc = Document(str(path))
    except Exception:
        return []
    tables = []
    for t in doc.tables:
        rows = [[c.text.strip() for c in r.cells] for r in t.rows]
        if len(rows) >= 2:
            tables.append(rows)
    return tables


def build_glossary(share_root: Path) -> Glossary:
    """share_root 配下の docx から用語表を集めて辞書を作る."""
    g = Glossary()
    for path in sorted(share_root.rglob("*.docx")):
        if path.name.startswith("~$"):
            continue
        for rows in _load_docx_tables(path):
            header = rows[0]
            i_alias = _column_index(header, COL_ALIAS)
            i_canon = _column_index(header, COL_CANON)
            if i_alias is None or i_canon is None:
                continue
            i_other = _column_index(header, COL_OTHER)
            for row in rows[1:]:
                if len(row) <= max(i_alias, i_canon):
                    continue
                canonical = row[i_canon]
                for alias in _split_alternatives(row[i_alias]):
                    g.add(alias, canonical)
                if i_other is not None and len(row) > i_other:
                    for alias in _split_alternatives(row[i_other]):
                        g.add(alias, canonical)
    return g


if __name__ == "__main__":
    import sys

    root = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
    gl = build_glossary(root)
    print(f"用語数: {len(gl)}")
    for alias in list(sorted(gl.entries))[:20]:
        print(f"  {alias} -> {gl.entries[alias]}")
