#!/usr/bin/env python3
"""回答実行ログと同じ検索経路を再現し、問題ごとの診断レポートを作る."""

from __future__ import annotations

import argparse
import csv
import json
import sys
import unicodedata
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RAG = ROOT / "rag"
sys.path.insert(0, str(RAG))

from glossary import build_glossary  # noqa: E402
from index import Index, load_chunks  # noqa: E402


def normalize_for_compare(value: str) -> str:
    return unicodedata.normalize("NFKC", value).strip()


def classify(
    expected: str,
    predicted: str,
    corpus_has_exact: bool,
    top_has_exact: bool,
) -> tuple[str, str]:
    """機械的に断定できる範囲だけ分類する.

    正解文字列が原文にそのまま現れない計算・比較・列挙問題は、
    前処理不足と断定せず目視確認に回す。
    """
    if normalize_for_compare(predicted) == normalize_for_compare(expected):
        return "成功", "正解とモデル回答が一致"
    if top_has_exact:
        return "回答生成不足", "正解文字列を含む根拠が検索上位にある"
    if corpus_has_exact:
        return "検索不足", "正解文字列は抽出済みだが検索上位にない"
    return (
        "要目視",
        "正解文字列の完全一致なし。前処理不足、または計算・比較・表記差を確認",
    )


def load_questions(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))
    required = {"index", "question", "answer"}
    if not rows or not required.issubset(rows[0]):
        raise ValueError(f"正解付きCSVに {sorted(required)} が必要です: {path}")
    return rows


def load_run(path: Path) -> tuple[dict[str, str], dict]:
    record = json.loads(path.read_text(encoding="utf-8"))
    answers = {
        str(item["index"]): str(item.get("回答", ""))
        for item in record.get("回答", [])
    }
    return answers, record


def load_reviews(path: Path | None) -> dict[str, dict[str, str]]:
    if path is None:
        return {}
    record = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(record, dict):
        raise ValueError(f"目視確認JSONは index をキーにしたオブジェクトです: {path}")
    return {str(key): value for key, value in record.items()}


def clipped(text: str, limit: int) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[:limit] + " …"


def build_records(args) -> tuple[list[dict], dict]:
    questions = load_questions(args.questions)
    predictions, run = load_run(args.run_log)
    reviews = load_reviews(args.reviews)
    chunks = load_chunks(args.chunks)
    index = Index(chunks)
    glossary = build_glossary(args.share_root)
    corpus_texts = [normalize_for_compare(chunk.text) for chunk in chunks]
    top_k = args.top_k or int(run.get("パラメータ", {}).get("top_k", 12))

    records = []
    for row in questions:
        question_id = str(row["index"])
        question = row["question"]
        expected = row["answer"]
        predicted = predictions.get(question_id, "")
        expected_norm = normalize_for_compare(expected)
        corpus_has_exact = any(expected_norm in text for text in corpus_texts)
        scored = index.search_with_scores(
            question, glossary.aliases_in(question), top_k=top_k
        )
        results = []
        for rank, (score, chunk) in enumerate(scored, 1):
            exact = expected_norm in normalize_for_compare(chunk.text)
            results.append({
                "rank": rank,
                "score": round(score, 6),
                "path": chunk.path,
                "location": chunk.location,
                "kind": chunk.kind,
                "chunk_id": chunk.cid,
                "contains_exact_answer": exact,
                "text": chunk.text,
            })
        top_has_exact = any(item["contains_exact_answer"] for item in results)
        category, reason = classify(
            expected, predicted, corpus_has_exact, top_has_exact
        )
        human_review = {
            "final_category": "",
            "evidence_location": "",
            "note": "",
        }
        human_review.update(reviews.get(question_id, {}))
        records.append({
            "index": question_id,
            "question": question,
            "expected_answer": expected,
            "model_answer": predicted,
            "automatic_category": category,
            "automatic_reason": reason,
            "corpus_contains_exact_answer": corpus_has_exact,
            "top_results_contain_exact_answer": top_has_exact,
            "retrieval_results": results,
            "human_review": human_review,
        })

    final_categories = Counter(
        r["human_review"].get("final_category") or r["automatic_category"]
        for r in records
    )
    summary = {
        "questions": len(records),
        "top_k": top_k,
        "categories": dict(Counter(r["automatic_category"] for r in records)),
        "final_categories": dict(final_categories),
        "run_log": str(args.run_log),
        "chunks": len(chunks),
        "warning": "自動分類は正解文字列の完全一致による一次判定。計算・比較・列挙は目視確認が必要。",
    }
    return records, summary


def write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for record in records:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def write_markdown(path: Path, records: list[dict], summary: dict, shown: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# 回答診断レポート",
        "",
        "> 自動分類は一次判定です。正解が計算・比較・列挙で得られる問題は、検索本文を目視して確定します。",
        "",
        "## 概要",
        "",
        f"- 問題数: {summary['questions']}",
        f"- 検索件数: 各問上位{summary['top_k']}件",
        f"- 検索チャンク数: {summary['chunks']}",
        "- 自動分類: " + " / ".join(
            f"{key} {value}問" for key, value in summary["categories"].items()
        ),
        "- 目視確認後: " + " / ".join(
            f"{key} {value}問" for key, value in summary["final_categories"].items()
        ),
        "",
    ]
    for record in records:
        lines += [
            f"## [{record['index']}] {record['question']}",
            "",
            f"- 正解: `{record['expected_answer']}`",
            f"- モデル回答: `{record['model_answer']}`",
            f"- 自動分類: **{record['automatic_category']}**",
            f"- 理由: {record['automatic_reason']}",
            f"- コーパス内に正解文字列: {'あり' if record['corpus_contains_exact_answer'] else 'なし'}",
            "",
            "### 検索結果",
            "",
        ]
        for item in record["retrieval_results"][:shown]:
            marker = " / 正解文字列あり" if item["contains_exact_answer"] else ""
            lines += [
                f"#### {item['rank']}位 score={item['score']}{marker}",
                "",
                f"`{item['path']} / {item['location']}`",
                "",
                "```text",
                clipped(item["text"], 1600),
                "```",
                "",
            ]
        review = record["human_review"]
        lines += [
            "### 目視確認",
            "",
            f"- 最終分類: {review.get('final_category', '')}",
            f"- 根拠位置: {review.get('evidence_location', '')}",
            f"- メモ: {review.get('note', '')}",
            "",
        ]
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--questions", type=Path, required=True)
    parser.add_argument("--run-log", type=Path, required=True)
    parser.add_argument("--chunks", type=Path, default=RAG / "chunks.jsonl")
    parser.add_argument("--share-root", type=Path, default=ROOT / "share" / "共有ドライブ")
    parser.add_argument("--out-jsonl", type=Path, required=True)
    parser.add_argument("--out-md", type=Path, required=True)
    parser.add_argument("--reviews", type=Path)
    parser.add_argument("--top-k", type=int)
    parser.add_argument("--show-top", type=int, default=5)
    args = parser.parse_args()
    if args.top_k is not None and args.top_k < 1:
        parser.error("--top-k は1以上です")
    if args.show_top < 1:
        parser.error("--show-top は1以上です")

    records, summary = build_records(args)
    write_jsonl(args.out_jsonl, records)
    write_markdown(args.out_md, records, summary, args.show_top)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"JSONL: {args.out_jsonl}")
    print(f"Markdown: {args.out_md}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
