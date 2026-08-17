"""社内用語集の自動検出と、質問文の略称展開.

共有ドライブ内の docx を走査し、「社内用語 / 略称」列を持つ表を
用語辞書として取り込む。特定のファイル名・案件名には依存しない。
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# 用語表のヘッダに現れうる列名（この語を含む列を、その役割の列とみなす）
COL_ALIAS = ("社内用語", "略称", "主略称", "略号")
COL_CANON = ("正式名称", "案件名", "正式")
COL_OTHER = ("別名", "別名候補")

# 1文字のASCII略称は誤検出が多いため使わない
MIN_ASCII_ALIAS = 2
MIN_JA_ALIAS = 2

_ALIAS_OUTPUT_REQUEST = re.compile(
    r"(?:主略称|案件略称|略称)\s*(?:だけ|のみ)?\s*"
    r"(?:で|を|として|にて|は)"
)
_PRIMARY_ALIAS_OUTPUT_REQUEST = re.compile(
    r"主略称\s*(?:だけ|のみ)?\s*(?:で|を|として|にて|は)"
)
_ALIAS_OUTPUT_NEGATION = re.compile(
    r"(?:主略称|案件略称|略称)\s*"
    r"(?:ではなく|でなく|を使わず|を使わない|"
    r"を用いず|以外(?:で|の|を))"
)


def _normalized(value: object) -> str:
    return unicodedata.normalize("NFKC", str(value)).casefold().strip()


def requests_alias_output(question: str) -> bool:
    """Return whether the question explicitly requests an alias rendering."""

    if not isinstance(question, str):
        return False
    normalized = unicodedata.normalize("NFKC", question)
    if _ALIAS_OUTPUT_NEGATION.search(normalized):
        return False
    return _ALIAS_OUTPUT_REQUEST.search(normalized) is not None


def requests_primary_alias_output(question: str) -> bool:
    """Return whether the question explicitly asks for the primary alias."""

    if not isinstance(question, str):
        return False
    normalized = unicodedata.normalize("NFKC", question)
    if _ALIAS_OUTPUT_NEGATION.search(normalized):
        return False
    return _PRIMARY_ALIAS_OUTPUT_REQUEST.search(normalized) is not None


def _chunk_source_strings(
    chunks: Iterable[object],
    *,
    key: str,
) -> tuple[str, ...]:
    values: list[str] = []
    for chunk in chunks:
        if isinstance(chunk, Mapping):
            value = chunk.get(key)
        else:
            value = getattr(chunk, key, None)
        if value is None:
            continue
        rendered = str(value).strip()
        if rendered and rendered not in values:
            values.append(rendered)
    return tuple(values)


@dataclass
class Glossary:
    """alias -> 正式名称候補（複数ありうる）の辞書."""

    entries: dict = field(default_factory=dict)
    primary_entries: dict = field(default_factory=dict)

    def add(self, alias: str, canonical: str, *, primary: bool = False) -> None:
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
        if primary:
            primary_values = self.primary_entries.setdefault(alias, [])
            if canonical not in primary_values:
                primary_values.append(canonical)

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

    def output_alias_candidates(
        self,
        question: str,
        chunks: Iterable[object],
    ) -> list[dict[str, Any]]:
        """Build source-scoped canonical-to-alias rendering candidates.

        The mapping is emitted only when the question explicitly requests an
        alias.  Canonical keys must occur in a retrieved chunk's ``path`` or
        ``project`` metadata; answer text and chunk body text are never used.
        If one alias maps to several canonicals, every registered candidate is
        retained so downstream generation cannot silently choose one.
        """

        if not requests_alias_output(question):
            return []
        chunk_values = tuple(chunks)
        normalized_project_sources = tuple(
            _normalized(value)
            for value in _chunk_source_strings(chunk_values, key="project")
        )
        normalized_path_sources = tuple(
            _normalized(value)
            for value in _chunk_source_strings(chunk_values, key="path")
        )
        if not normalized_project_sources and not normalized_path_sources:
            return []

        all_canonicals = {
            canonical
            for values in self.entries.values()
            for canonical in values
        }

        def source_scoped(sources: tuple[str, ...]) -> set[str]:
            return {
                canonical
                for canonical in all_canonicals
                if any(
                    _normalized(canonical) in source
                    for source in sources
                )
            }

        # Prefer the narrow project identity.  Only if no registered canonical
        # matches it do we inspect paths, which keeps compatibility with custom
        # chunks while preventing path components such as ``スケジュール.xlsx``
        # from becoming unrelated output aliases (for example PL).
        canonicals = sorted(
            source_scoped(normalized_project_sources)
            or source_scoped(normalized_path_sources),
            key=lambda value: (_normalized(value), value),
        )
        result: list[dict[str, Any]] = []
        for canonical in canonicals:
            alias_candidates: list[dict[str, Any]] = []
            for alias in sorted(
                self.entries,
                key=lambda value: (
                    canonical not in self.primary_entries.get(value, []),
                    _normalized(value),
                    value,
                ),
            ):
                canonical_candidates = list(self.entries[alias])
                if canonical not in canonical_candidates:
                    continue
                is_primary = canonical in self.primary_entries.get(alias, [])
                alias_candidates.append(
                    {
                        "alias": alias,
                        "role": "primary" if is_primary else "declared_or_alternative",
                        "canonical_candidates": canonical_candidates,
                        "ambiguous": len(canonical_candidates) > 1,
                    }
                )
            if alias_candidates:
                result.append(
                    {
                        "canonical": canonical,
                        "alias_candidates": alias_candidates,
                    }
                )
        return result

    def render_primary_aliases(
        self,
        question: str,
        answer: str,
        chunks: Iterable[object],
    ) -> str:
        """Deterministically render exact source canonicals as primary aliases.

        Replacement is deliberately narrower than prompt guidance: the
        question must request ``主略称``, the canonical must be present in
        retrieved ``path``/``project`` metadata and literally occur in the
        generated answer, and the canonical must have exactly one unambiguous
        primary alias.  Any ambiguity leaves the generated text untouched.
        """

        if not requests_primary_alias_output(question) or not isinstance(answer, str):
            return answer
        replacements: list[tuple[str, str]] = []
        for record in self.output_alias_candidates(question, chunks):
            canonical = record.get("canonical")
            if not isinstance(canonical, str) or canonical not in answer:
                continue
            primary = [
                candidate
                for candidate in record.get("alias_candidates", [])
                if isinstance(candidate, Mapping)
                and candidate.get("role") == "primary"
            ]
            if len(primary) != 1:
                continue
            candidate = primary[0]
            alias = candidate.get("alias")
            canonical_candidates = candidate.get("canonical_candidates")
            if (
                not isinstance(alias, str)
                or not isinstance(canonical_candidates, list)
                or canonical_candidates != [canonical]
            ):
                continue
            replacements.append((canonical, alias))

        rendered = answer
        # Longest first prevents a shorter canonical from rewriting a prefix of
        # a more specific one before its exact match is considered.
        for canonical, alias in sorted(
            replacements,
            key=lambda item: (-len(item[0]), _normalized(item[0]), item[0]),
        ):
            rendered = rendered.replace(canonical, alias)
        return rendered


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
            primary_alias_column = "主略称" in header[i_alias]
            for row in rows[1:]:
                if len(row) <= max(i_alias, i_canon):
                    continue
                canonical = row[i_canon]
                for alias in _split_alternatives(row[i_alias]):
                    g.add(alias, canonical, primary=primary_alias_column)
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
