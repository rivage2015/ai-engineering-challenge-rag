#!/usr/bin/env python3
"""Shared local Ollama embedding client without API keys."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any


DEFAULT_BASE_URL = "http://127.0.0.1:11434"
DEFAULT_MODEL = "embeddinggemma"


def request_json(base_url: str, path: str, payload: dict[str, Any] | None, timeout: float) -> dict[str, Any]:
    url = base_url.rstrip("/") + path
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            detail = exc.read().decode("utf-8", errors="replace").strip()
        except OSError:
            detail = ""
        suffix = f": {detail[:2000]}" if detail else ""
        raise RuntimeError(
            f"local Ollama request failed: {url}: HTTP {exc.code}{suffix}"
        ) from exc
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"local Ollama request failed: {url}: {exc}") from exc


def model_info(base_url: str, model: str, timeout: float = 30.0) -> dict[str, str]:
    response = request_json(base_url, "/api/tags", None, timeout)
    requested = model if ":" in model else f"{model}:latest"
    for item in response.get("models", []):
        if item.get("name") in {model, requested} or item.get("model") in {model, requested}:
            if not item.get("digest"):
                raise ValueError(f"Ollama model has no digest: {model}")
            return {
                "requested": model,
                "resolved": item.get("name") or item.get("model") or requested,
                "digest": item["digest"],
            }
    raise ValueError(f"Ollama model is not installed: {model}")


def embed_texts(
    base_url: str,
    model: str,
    texts: list[str],
    timeout: float = 300.0,
) -> list[list[float]]:
    if not texts or any(not text.strip() for text in texts):
        raise ValueError("embedding input must contain non-empty text")
    response = request_json(
        base_url,
        "/api/embed",
        {"model": model, "input": texts, "truncate": True, "keep_alive": "10m"},
        timeout,
    )
    embeddings = response.get("embeddings")
    if not isinstance(embeddings, list) or len(embeddings) != len(texts):
        raise RuntimeError("Ollama returned an invalid embedding count")
    if not embeddings or not embeddings[0]:
        raise RuntimeError("Ollama returned empty embeddings")
    dimensions = len(embeddings[0])
    if any(not isinstance(vector, list) or len(vector) != dimensions for vector in embeddings):
        raise RuntimeError("Ollama returned inconsistent embedding dimensions")
    return embeddings
