#!/usr/bin/env python3
"""Run resumable Gemma 4 visual analysis stages strictly one at a time."""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import hashlib
import json
import mimetypes
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

LOCAL_HTTP_OPENER = urllib.request.build_opener(
    urllib.request.ProxyHandler({})
)

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from ollama_embedding_common import model_info  # noqa: E402

ORCHESTRATOR = "sequential-visual-orchestrator"
ORCHESTRATOR_VERSION = "0.1"
PROMPT_VERSIONS = {
    "transcription": "visual-transcription-v0.1",
    "visual_state": "visual-state-v0.3",
    "fusion": "visual-fusion-v0.3",
}
SUPPORTED_MIME_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp"}

TRANSCRIPTION_PROMPT = """あなたは画像内の文字と数値を忠実に観測する転記担当です。
画像の意味や答えを推測しないでください。誤字の修正や不鮮明な文字の補完も禁止です。
座標は画像左上を0,0、右下を1000,1000とする正規化座標[x,y,width,height]で返してください。
必ず次のJSONオブジェクトだけを返してください。
{
  "summary_text": "読み取れた文字全体。不明箇所は[不明]",
  "text_regions": [{"region_id":"t1","bbox":[0,0,1,1],"text":"","alternatives":[],"unreadable":false}],
  "table_candidates": [{"region_id":"table1","bbox":[0,0,1,1],"cells":[{"row":1,"column":1,"text":"","bbox":[0,0,1,1]}]}],
  "warnings": []
}
""".strip()

VISUAL_STATE_PROMPT = """あなたは画像の表示状態と視覚的な関係だけを観測する担当です。
文字の完全転記や質問への回答はしないでください。色、マーカー、太字、下線、配置、階層、
表、結合、グラフの軸・凡例・系列、空白に見える領域を観測します。空白を欠損値と断定しないでください。
座標は画像左上を0,0、右下を1000,1000とする正規化座標[x,y,width,height]で返してください。
グラフの場合は、各系列が対応する軸を判定し、見えるすべてのマーカーについて
x軸の値とy軸の概算値をdata_pointsへ記録してください。その後、系列ごとの最小・最大候補を
extrema_candidatesへ記録してください。値を厳密に読めない場合も、マーカー位置から概算し、
`estimated: true`とします。系列名が凡例にない場合は、色・マーカー・対応軸で識別します。
出力前に画像内の系列数を数え、`series`の要素数と一致することを確認してください。
例えば1本目の系列に点が多くても、2本目以降を省略してはいけません。
説明文よりも、全系列の`data_points`と`extrema_candidates`の完全性を優先します。
必ず次のJSONオブジェクトだけを返してください。
{
  "content_types": ["chart"],
  "visual_regions": [{"region_id":"v1","bbox":[0,0,1,1],"kind":"","styles":[],"description":""}],
  "relations": [{"relation_type":"","from_region":"v1","to_region":"v2","description":""}],
  "chart_observations": [{"chart_region":"v1","chart_type":"","axes":[],"legends":[],"series":[{"series_id":"","color":"","marker":"","axis_id":"","data_points":[{"x":null,"y":null,"estimated":true}]}],"extrema_candidates":[{"series_id":"","extremum":"minimum","x":null,"y":null,"estimated":true}]}],
  "blank_candidates": [],
  "warnings": []
}
""".strip()

FUSION_PROMPT = """あなたは独立した2つの画像観測を統合する担当です。
入力にない事実を推測せず、各項目に根拠参照を付けてください。
一方だけが示す事実はその旨を残し、矛盾は勝手に解消せずconflictsへ記録します。
グラフのdata_pointsとextrema_candidatesは省略せず、軸・系列と対応付けてevidence_itemsへ残してください。
必ず次のJSONオブジェクトだけを返してください。
{
  "document_summary": "",
  "evidence_items": [{"evidence_type":"","text":"","attributes":{},"source_refs":["transcription:t1"]}],
  "relations": [{"relation_type":"","from_item":0,"to_item":1,"source_refs":[]}],
  "conflicts": [{"description":"","source_refs":[]}],
  "unresolved": []
}
""".strip()


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def atomic_write_json(path: Path, value: Any) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def request_json(base_url: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
    url = base_url.rstrip("/") + "/api/chat"
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with LOCAL_HTTP_OPENER.open(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"local Ollama visual request failed: {url}: {exc}") from exc


def parse_model_json(response: dict[str, Any], stage: str) -> dict[str, Any]:
    content = str((response.get("message") or {}).get("content") or "").strip()
    content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content, flags=re.IGNORECASE)
    try:
        value = json.loads(content)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{stage} returned invalid JSON: {exc}: {content[:300]}") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"{stage} must return a JSON object")
    return value


def image_dimensions(path: Path) -> tuple[int | None, int | None]:
    try:
        from PIL import Image

        with Image.open(path) as image:
            return int(image.width), int(image.height)
    except Exception:
        return None, None


def validate_stage(stage: str, value: dict[str, Any]) -> list[str]:
    required = {
        "transcription": {"summary_text", "text_regions", "table_candidates"},
        "visual_state": {"content_types", "visual_regions", "relations", "chart_observations", "blank_candidates", "warnings"},
        "fusion": {"document_summary", "evidence_items", "relations", "conflicts", "unresolved"},
    }[stage]
    warnings = []
    missing = sorted(required - set(value))
    if missing:
        warnings.append(f"{stage} missing keys: {', '.join(missing)}")
    for key in required & set(value):
        if key not in {"summary_text", "document_summary"} and not isinstance(value[key], list):
            warnings.append(f"{stage}.{key} must be an array")
    return warnings


def run_image_stage(base_url: str, model: str, prompt: str, image_b64: str, timeout: float) -> dict[str, Any]:
    return request_json(base_url, {
        "model": model,
        "messages": [{"role": "user", "content": prompt, "images": [image_b64]}],
        "stream": False,
        "format": "json",
        "think": False,
        "keep_alive": "10m",
        "options": {"temperature": 0, "seed": 42, "num_ctx": 65536, "num_predict": 8192},
    }, timeout)


def run_fusion_stage(base_url: str, model: str, transcription: dict[str, Any], visual_state: dict[str, Any], timeout: float) -> dict[str, Any]:
    user = FUSION_PROMPT + "\n\n【転記観測】\n" + canonical_json(transcription)
    user += "\n\n【視覚状態観測】\n" + canonical_json(visual_state)
    return request_json(base_url, {
        "model": model,
        "messages": [{"role": "user", "content": user}],
        "stream": False,
        "format": "json",
        "think": False,
        "keep_alive": "10m",
        "options": {"temperature": 0, "seed": 42, "num_ctx": 65536, "num_predict": 8192},
    }, timeout)


def completed_stage(path: Path, signature: str) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if value.get("signature") != signature or value.get("status") != "completed":
        return None
    return value


def execute_stage(name: str, path: Path, signature: str, prompt_version: str, operation) -> dict[str, Any]:
    cached = completed_stage(path, signature)
    if cached is not None:
        print(f"  cache: {name}")
        return cached
    print(f"  run: {name}", flush=True)
    started = time.monotonic()
    output = operation()
    record = {
        "signature": signature,
        "status": "completed",
        "prompt_version": prompt_version,
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "output": output,
    }
    atomic_write_json(path, record)
    return record


def verify_outputs(transcription: dict[str, Any], visual_state: dict[str, Any], fusion: dict[str, Any]) -> dict[str, Any]:
    warnings = []
    warnings.extend(validate_stage("transcription", transcription))
    warnings.extend(validate_stage("visual_state", visual_state))
    warnings.extend(validate_stage("fusion", fusion))
    evidence = fusion.get("evidence_items") if isinstance(fusion.get("evidence_items"), list) else []
    conflicts = fusion.get("conflicts") if isinstance(fusion.get("conflicts"), list) else []
    unresolved = fusion.get("unresolved") if isinstance(fusion.get("unresolved"), list) else []
    checks = [
        {"name": "stage_contracts", "passed": not warnings, "detail": "; ".join(warnings)},
        {"name": "fusion_has_evidence", "passed": bool(evidence), "detail": f"evidence_items={len(evidence)}"},
        {"name": "fusion_has_no_conflicts", "passed": not conflicts, "detail": f"conflicts={len(conflicts)}"},
    ]
    if conflicts or unresolved or warnings or not evidence:
        status = "needs_retry"
    else:
        status = "verified"
    return {"status": status, "checks": checks, "warnings": warnings, "retry_count": 0}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--source-root", type=Path)
    parser.add_argument("--origin-json", type=Path)
    parser.add_argument("--model", default="gemma4:12b")
    parser.add_argument("--base-url", default="http://127.0.0.1:11434")
    parser.add_argument("--timeout", type=float, default=900.0)
    parser.add_argument("--restart", action="store_true")
    args = parser.parse_args()
    image_path = args.image.resolve()
    if not image_path.is_file():
        parser.error(f"image not found: {image_path}")
    mime_type = mimetypes.guess_type(image_path.name)[0] or ""
    if mime_type not in SUPPORTED_MIME_TYPES:
        parser.error(f"unsupported image MIME type: {mime_type}")
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    args.out.mkdir(parents=True, exist_ok=True)
    if args.restart:
        for name in ("transcription.json", "visual-state.json", "fusion.json", "analysis.json"):
            target = args.out / name
            if target.exists():
                target.unlink()

    image_bytes = image_path.read_bytes()
    image_sha256 = sha256_bytes(image_bytes)
    image_b64 = base64.b64encode(image_bytes).decode("ascii")
    model = model_info(args.base_url, args.model, timeout=min(args.timeout, 30.0))
    base_signature_input = {
        "image_sha256": image_sha256,
        "model_digest": model["digest"],
        "orchestrator_version": ORCHESTRATOR_VERSION,
    }
    transcription_signature = sha256_bytes(canonical_json({
        **base_signature_input,
        "stage": "transcription",
        "prompt_version": PROMPT_VERSIONS["transcription"],
    }).encode("utf-8"))
    visual_signature = sha256_bytes(canonical_json({
        **base_signature_input,
        "stage": "visual_state",
        "prompt_version": PROMPT_VERSIONS["visual_state"],
    }).encode("utf-8"))

    transcription_record = execute_stage(
        "transcription", args.out / "transcription.json", transcription_signature,
        PROMPT_VERSIONS["transcription"],
        lambda: parse_model_json(run_image_stage(
            args.base_url, args.model, TRANSCRIPTION_PROMPT, image_b64, args.timeout
        ), "transcription"),
    )
    visual_record = execute_stage(
        "visual_state", args.out / "visual-state.json", visual_signature,
        PROMPT_VERSIONS["visual_state"],
        lambda: parse_model_json(run_image_stage(
            args.base_url, args.model, VISUAL_STATE_PROMPT, image_b64, args.timeout
        ), "visual_state"),
    )
    fusion_signature = sha256_bytes(canonical_json({
        **base_signature_input,
        "stage": "fusion",
        "prompt_version": PROMPT_VERSIONS["fusion"],
        "transcription_sha256": sha256_bytes(canonical_json(transcription_record["output"]).encode("utf-8")),
        "visual_state_sha256": sha256_bytes(canonical_json(visual_record["output"]).encode("utf-8")),
    }).encode("utf-8"))
    fusion_record = execute_stage(
        "fusion", args.out / "fusion.json", fusion_signature,
        PROMPT_VERSIONS["fusion"],
        lambda: parse_model_json(run_fusion_stage(
            args.base_url, args.model,
            transcription_record["output"], visual_record["output"], args.timeout
        ), "fusion"),
    )
    verification = verify_outputs(
        transcription_record["output"], visual_record["output"], fusion_record["output"]
    )
    width, height = image_dimensions(image_path)
    if args.source_root:
        try:
            source_path = str(image_path.relative_to(args.source_root.resolve()))
        except ValueError:
            parser.error("--image must be inside --source-root")
    else:
        source_path = str(image_path)
    origin = {}
    if args.origin_json:
        origin = json.loads(args.origin_json.read_text(encoding="utf-8"))
    source = {
        "path": source_path,
        "mime_type": mime_type,
        "sha256": image_sha256,
        "bytes": len(image_bytes),
        "origin": origin,
    }
    if width and height:
        source.update({"width_px": width, "height_px": height})
    analysis = {
        "schema_version": "0.1",
        "record_type": "visual_analysis",
        "analysis_id": "va_" + sha256_bytes(canonical_json({
            "transcription": transcription_signature,
            "visual_state": visual_signature,
            "fusion": fusion_signature,
        }).encode("utf-8"))[:24],
        "source": source,
        "model": model,
        "transcription": {key: transcription_record[key] for key in ("status", "prompt_version", "elapsed_seconds", "output")},
        "visual_state": {key: visual_record[key] for key in ("status", "prompt_version", "elapsed_seconds", "output")},
        "fusion": {key: fusion_record[key] for key in ("status", "prompt_version", "elapsed_seconds", "output")},
        "verification": verification,
        "provenance": {
            "orchestrator": ORCHESTRATOR,
            "orchestrator_version": ORCHESTRATOR_VERSION,
            "prompt_versions": PROMPT_VERSIONS,
            "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "sequential": True,
        },
    }
    atomic_write_json(args.out / "analysis.json", analysis)
    print(f"analysis: {args.out / 'analysis.json'}")
    print(f"status: {verification['status']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
