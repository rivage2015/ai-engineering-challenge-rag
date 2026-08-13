#!/usr/bin/env python3
"""Shared deterministic tokenization and hashing for lexical search."""

from __future__ import annotations

import hashlib
import json
import unicodedata
from collections import Counter
from pathlib import Path
from typing import Any


TOKENIZER = "unicode-script-runs-char-ngram"
TOKENIZER_VERSION = "0.1.0"


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def script_group(character: str) -> str | None:
    codepoint = ord(character)
    if character.isascii() and character.isalnum():
        return "ascii"
    if (
        0x3040 <= codepoint <= 0x30FF
        or 0x31F0 <= codepoint <= 0x31FF
        or 0x3400 <= codepoint <= 0x4DBF
        or 0x4E00 <= codepoint <= 0x9FFF
        or 0xF900 <= codepoint <= 0xFAFF
    ):
        return "japanese"
    category = unicodedata.category(character)
    if category.startswith(("L", "N")):
        return "other_word"
    return None


def script_runs(text: str) -> list[tuple[str, str]]:
    normalized = unicodedata.normalize("NFKC", text).casefold()
    runs: list[tuple[str, str]] = []
    current_group: str | None = None
    current: list[str] = []
    for character in normalized:
        group = script_group(character)
        if group is None:
            if current:
                runs.append((current_group or "other_word", "".join(current)))
                current = []
            current_group = None
        elif group == current_group:
            current.append(character)
        else:
            if current:
                runs.append((current_group or "other_word", "".join(current)))
            current_group = group
            current = [character]
    if current:
        runs.append((current_group or "other_word", "".join(current)))
    return runs


def tokenize(text: str) -> list[str]:
    tokens: list[str] = []
    for group, run in script_runs(text):
        if group == "ascii":
            tokens.append(f"w:{run}")
        elif group == "japanese":
            if len(run) == 1:
                tokens.append(f"j1:{run}")
            else:
                tokens.extend(f"j2:{run[index:index + 2]}" for index in range(len(run) - 1))
                if len(run) >= 3:
                    tokens.extend(f"j3:{run[index:index + 3]}" for index in range(len(run) - 2))
        else:
            tokens.append(f"w:{run}")
    return tokens


def term_frequencies(text: str) -> Counter[str]:
    return Counter(tokenize(text))
