#!/usr/bin/env python3
"""Exact, question-path-sized chunks for long extracted text."""

from __future__ import annotations

from typing import NamedTuple


# answer_local_memory_v2.compact_context currently exposes at most 1,800
# characters from one Evidence packet. Keep extraction chunks below that
# boundary so a lexical hit cannot point to text that the answering/audit
# context then silently removes.
MAX_QUESTION_EVIDENCE_CHARS = 1_600


class TextChunk(NamedTuple):
    start: int
    end: int
    text: str


def exact_text_chunks(
    value: str,
    *,
    max_chars: int = MAX_QUESTION_EVIDENCE_CHARS,
) -> list[TextChunk]:
    """Split text exactly, preferring a nearby newline without losing bytes.

    Offsets are zero-based Python character offsets. Joining every returned
    ``text`` reproduces ``value`` exactly, including whitespace and newlines.
    """
    if not isinstance(value, str):
        raise TypeError("text chunk input must be a string")
    if not isinstance(max_chars, int) or isinstance(max_chars, bool) or max_chars < 256:
        raise ValueError("max_chars must be an integer of at least 256")
    chunks: list[TextChunk] = []
    start = 0
    while start < len(value):
        hard_end = min(start + max_chars, len(value))
        end = hard_end
        if hard_end < len(value):
            # ``str.rfind`` uses an exclusive stop.  Do not include
            # ``hard_end`` itself: consuming a newline at that index would
            # make this chunk ``max_chars + 1`` characters long.
            newline = value.rfind("\n", start + max_chars // 2, hard_end)
            if newline >= 0:
                end = newline + 1
        if end <= start:
            raise RuntimeError("text chunker made no progress")
        chunks.append(TextChunk(start, end, value[start:end]))
        start = end
    if "".join(chunk.text for chunk in chunks) != value:
        raise RuntimeError("text chunk reconstruction failed")
    if any(not chunk.text or len(chunk.text) > max_chars for chunk in chunks):
        raise RuntimeError("text chunk size contract failed")
    return chunks
