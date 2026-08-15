"""RAG パイプライン本体.

  python3 main.py              # test 100問に回答し predictions.csv と zip を作る
  python3 main.py --valid      # 正解付き30問に回答（ローカル採点用）
  python3 main.py --rebuild    # 抽出をやり直す（既定はキャッシュを再利用）
  python3 main.py --dry-run    # LLM を呼ばず、検索結果だけ確認する
  python3 main.py --valid --start 0 --limit 1  # 1問だけローカル動作確認
  python3 main.py --valid --dry-run --retrieval-mode layer1-lexical
  python3 main.py --valid --dry-run --retrieval-mode layer1-hybrid
"""

from __future__ import annotations

import argparse
import csv
import datetime
import hashlib
import io
import json
import platform
import sys
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Lock

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
SHARE = ROOT / "share"
# 検索対象は共有ドライブのみ。質問回答フォルダは正解を含むため索引に入れない。
DOCS = SHARE / "共有ドライブ"
QUESTIONS = SHARE / "質問回答"
OUT = HERE / "out"
CACHE = HERE / "chunks.jsonl"
LOGS = HERE / "logs"
ASSETS = HERE / "assets"
ANSWER_CHECKPOINT = OUT / "answer-checkpoint.json"

sys.path.insert(0, str(HERE))

from glossary import build_glossary          # noqa: E402
from index import Index, load_chunks, make_chunks, save_chunks   # noqa: E402


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _layer1_state_hashes(args) -> dict[str, str]:
    if args.retrieval_mode == "baseline":
        return {}
    base = args.layer1_base.resolve()
    with_charts = (base / "chart-intermediate").is_dir()
    lexical = base / ("lexical-index-with-charts" if with_charts else "lexical-index")
    states = {"lexical": _sha256(lexical / "lexical-index-state.json")}
    if args.retrieval_mode == "layer1-hybrid":
        semantic = base / ("semantic-index-with-charts" if with_charts else "semantic-index")
        states["semantic"] = _sha256(semantic / "semantic-index-state.json")
    return states


def load_questions(valid: bool, start: int = 0, limit: int | None = None):
    name = "questions_valid.csv" if valid else "questions_test.csv"
    path = QUESTIONS / name
    rows = list(csv.reader(io.open(path, encoding="utf-8-sig")))[1:]
    questions = [(r[0], r[1]) for r in rows]
    questions = questions[start:]
    return questions[:limit] if limit is not None else questions


def build_or_load_chunks(rebuild: bool, glossary):
    if CACHE.exists() and not rebuild:
        print(f"キャッシュを読み込み: {CACHE.name}")
        return load_chunks(CACHE)
    from extract import extract_all, write_coverage

    print("共有ドライブを解析中… (数分かかります)")
    t0 = time.time()
    sections, coverage = extract_all(DOCS, glossary, assets_dir=ASSETS)
    chunks = make_chunks(sections)
    save_chunks(chunks, CACHE)
    LOGS.mkdir(exist_ok=True)
    write_coverage(coverage, LOGS / "coverage.md")
    pending = sum(1 for c in coverage if c.status in ("deferred", "partial", "failed"))
    print(f"  {len(chunks)} チャンク / {time.time() - t0:.0f}秒")
    print(f"  未処理・一部のみ: {pending} ファイル -> logs/coverage.md")
    return chunks


def _write_run_log(args, questions, retrieved, results, model, t_start) -> Path:
    """後から検証できるよう、実行条件と各問の参照資料・回答を残す."""
    LOGS.mkdir(exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    record = {
        "実行日時": datetime.datetime.now().isoformat(timespec="seconds"),
        "モード": "valid" if args.valid else "test",
        "モデル": model,
        "バックエンド": args.backend,
        "パラメータ": {
            "top_k": args.top_k, "workers": args.workers,
            "retrieval_mode": args.retrieval_mode,
            "retrieval_state_sha256": _layer1_state_hashes(args),
            "temperature": 0, "seed": 42,
        },
        "実行環境": {
            "python": platform.python_version(),
            "platform": platform.platform(),
        },
        "所要秒数": round(time.time() - t_start, 1),
        "質問数": len(questions),
        "わかりません件数": sum(
            1 for v in results.values() if v.startswith("わかりません")
        ),
        "回答": [
            {
                "index": idx,
                "質問": q,
                "回答": results.get(idx, ""),
                "参照資料": [
                    f"{c.path} / {c.location}" for c in retrieved.get(idx, [])
                ],
            }
            for idx, q in questions
        ],
    }
    path = LOGS / f"run_{stamp}.json"
    path.write_text(
        json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    hist = LOGS / "history.md"
    line = (
        f"| {record['実行日時']} | {record['モード']} | {model} | "
        f"{args.retrieval_mode}, top_k={args.top_k} | {record['質問数']}問 | "
        f"わかりません {record['わかりません件数']} | {path.name} |\n"
    )
    if not hist.exists():
        hist.write_text(
            "# 実行履歴\n\n"
            "| 日時 | モード | モデル | 検索設定 | 問数 | 棄権 | ログ |\n"
            "|---|---|---|---|---|---|---|\n", encoding="utf-8",
        )
    with hist.open("a", encoding="utf-8") as fh:
        fh.write(line)
    print(f"実行ログ: logs/{path.name}")
    return path


def _checkpoint_signature(args, questions) -> str:
    payload = json.dumps({
        "mode": "valid" if args.valid else "test",
        "questions": questions,
        "backend": args.backend,
        "model": args.model,
        "top_k": args.top_k,
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if args.retrieval_mode != "baseline":
        signature_data = json.loads(payload)
        signature_data.update({
            "retrieval_mode": args.retrieval_mode,
            "layer1_base": str(args.layer1_base.resolve()),
            "retrieval_state_sha256": _layer1_state_hashes(args),
        })
        payload = json.dumps(
            signature_data, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _load_answer_checkpoint(signature: str) -> dict[str, str]:
    if not ANSWER_CHECKPOINT.exists():
        return {}
    try:
        record = json.loads(ANSWER_CHECKPOINT.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if record.get("signature") != signature or not isinstance(record.get("answers"), dict):
        return {}
    return {str(key): str(value) for key, value in record["answers"].items()}


def _save_answer_checkpoint(signature: str, results: dict[str, str]) -> None:
    temporary = ANSWER_CHECKPOINT.with_suffix(".tmp")
    temporary.write_text(json.dumps({
        "signature": signature,
        "updated_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "answers": results,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(ANSWER_CHECKPOINT)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--valid", action="store_true")
    ap.add_argument("--rebuild", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--top-k", type=int, default=12)
    ap.add_argument("--workers", type=int)
    ap.add_argument("--start", type=int, default=0)
    ap.add_argument("--limit", type=int)
    ap.add_argument("--backend", choices=["ollama", "openai"])
    ap.add_argument("--model")
    ap.add_argument("--answer-timeout", type=float, default=180.0)
    ap.add_argument("--restart-answers", action="store_true")
    ap.add_argument(
        "--retrieval-mode",
        choices=["baseline", "layer1-lexical", "layer1-hybrid"],
        default="baseline",
        help="baseline remains the default; Layer-1 modes use the audited indexes without rebuilding them",
    )
    ap.add_argument("--layer1-base", type=Path, default=ROOT / "artifacts" / "layer1-v1")
    args = ap.parse_args()

    from answer import BACKEND, MODEL, answer_question, make_client, normalize_answer

    args.backend = args.backend or BACKEND
    args.model = args.model or MODEL
    if args.workers is None:
        args.workers = 1 if args.backend == "ollama" else 4
    if args.limit is not None and args.limit < 1:
        ap.error("--limit は1以上を指定してください")
    if args.start < 0:
        ap.error("--start は0以上を指定してください")
    if args.answer_timeout <= 0:
        ap.error("--answer-timeout は0より大きい値を指定してください")

    if not DOCS.exists():
        print(f"共有ドライブフォルダが見つかりません: {DOCS}")
        return 1

    OUT.mkdir(exist_ok=True)
    print("用語集を読み込み中…")
    glossary = build_glossary(DOCS)
    print(f"  用語 {len(glossary)} 件")

    if args.retrieval_mode == "baseline":
        chunks = build_or_load_chunks(args.rebuild, glossary)
        index = Index(chunks)
        chunk_count = len(chunks)
    else:
        if args.rebuild:
            ap.error("--rebuild はbaseline検索でのみ使用できます")
        from layer1_index import Layer1Index

        layer1_base = args.layer1_base.resolve()
        chart_intermediate = layer1_base / "chart-intermediate"
        intermediates = [layer1_base / "intermediate"]
        if chart_intermediate.is_dir():
            intermediates.append(chart_intermediate)
        lexical_name = "lexical-index-with-charts" if chart_intermediate.is_dir() else "lexical-index"
        semantic_name = "semantic-index-with-charts" if chart_intermediate.is_dir() else "semantic-index"
        try:
            index = Layer1Index(
                args.retrieval_mode,
                layer1_base / lexical_name,
                intermediates,
                layer1_base / semantic_name if args.retrieval_mode == "layer1-hybrid" else None,
            )
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            print(f"Layer 1索引を読み込めませんでした: {exc}")
            return 1
        chunk_count = index.record_count
    questions = load_questions(args.valid, args.start, args.limit)
    print(f"質問 {len(questions)} 件 / チャンク {chunk_count} 件 / retrieval={args.retrieval_mode}")

    try:
        retrieved = {
            idx: index.search(q, glossary.aliases_in(q), top_k=args.top_k)
            for idx, q in questions
        }
    except Exception as exc:
        print(f"検索に失敗しました: {type(exc).__name__}: {exc}")
        return 1

    if args.dry_run:
        for idx, q in questions[:5]:
            print(f"\n[{idx}] {q}")
            for c in retrieved[idx][:5]:
                print("   ", c.path, "/", c.location)
        print("\n--dry-run のため LLM は呼び出していません。")
        return 0

    t_start = time.time()
    client = make_client(args.backend, args.model, args.answer_timeout)

    # 全問走らせる前に、バックエンドとモデルを確認する。
    print(f"回答バックエンドを確認中… backend={args.backend} model={args.model}")
    try:
        client.check()
    except Exception as e:
        print()
        print("=" * 50)
        print("回答バックエンドを利用できませんでした。")
        print(f"  {type(e).__name__}: {str(e)[:200]}")
        print("=" * 50)
        return 1
    print(f"回答生成中… backend={args.backend} model={args.model}")
    signature = _checkpoint_signature(args, questions)
    results: dict[str, str] = (
        {} if args.restart_answers else _load_answer_checkpoint(signature)
    )
    if results:
        results = {key: normalize_answer(value) for key, value in results.items()}
        print(f"途中保存から再開: {len(results)}/{len(questions)} 問")
    done = [len(results)]
    checkpoint_lock = Lock()

    def work(item):
        idx, q = item
        if idx in results:
            return
        try:
            a = answer_question(client, q, retrieved[idx], glossary)
        except Exception as e:
            print(f"  ! [{idx}] {type(e).__name__}: {e}")
            a = "わかりません"
        answer = a.replace("\n", " ").strip() or "わかりません"
        with checkpoint_lock:
            results[idx] = answer
            done[0] += 1
            _save_answer_checkpoint(signature, results)
            print(f"  {done[0]}/{len(questions)} [{idx}] {answer[:50]}", flush=True)

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        list(ex.map(work, questions))

    _write_run_log(args, questions, retrieved, results, args.model, t_start)

    preview = args.limit is not None
    csv_path = OUT / ("predictions_preview.csv" if preview else "predictions.csv")
    with csv_path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        for idx, _ in questions:
            w.writerow([idx, results.get(idx, "わかりません")])

    zip_path = None
    if not preview:
        zip_path = OUT / ("submission_valid.zip" if args.valid else "submission.zip")
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
            z.write(csv_path, "predictions.csv")

    unknown = sum(1 for v in results.values() if v.startswith("わかりません"))
    print(f"\n完成: {zip_path or csv_path}")
    print(f"  「わかりません」 {unknown}/{len(questions)} 件")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
