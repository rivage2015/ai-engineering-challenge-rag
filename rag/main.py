"""RAG パイプライン本体.

  python3 main.py              # test 100問に回答し predictions.csv と zip を作る
  python3 main.py --valid      # 正解付き30問に回答（ローカル採点用）
  python3 main.py --rebuild    # 抽出をやり直す（既定はキャッシュを再利用）
  python3 main.py --dry-run    # グラフ構築と検索だけ実行（回答LLMは呼ばない）
  python3 main.py --valid --start 0 --limit 1  # 1問だけローカル動作確認
  python3 main.py --valid --dry-run --retrieval-mode layer1-lexical
  python3 main.py --valid --dry-run --retrieval-mode layer1-hybrid
  python3 main.py --legacy-answer-path  # 旧経路とのA/B比較のみ
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
QUESTION_GRAPH_CACHE = OUT / "question-graph-cache"

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


def _stable_texts(values) -> tuple[str, ...]:
    """結果順を変えずに空文字と重複を除く."""
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        rendered = str(value).strip()
        if not rendered or rendered in seen:
            continue
        seen.add(rendered)
        result.append(rendered)
    return tuple(result)


def _chunk_identity(chunk) -> tuple[str, str, str, str]:
    """Index実装をまたいで安定する検索チャンク識別子."""
    text = str(getattr(chunk, "text", ""))
    return (
        str(getattr(chunk, "path", "")),
        str(getattr(chunk, "location", "")),
        str(getattr(chunk, "kind", "")),
        hashlib.sha256(text.encode("utf-8")).hexdigest(),
    )


def _search_graph_plan(index, plan, glossary, top_k: int):
    """Search every typed branch, then merge hits with stable round-robin dedupe.

    Searching per branch prevents one interpretation from consuming every
    retrieval slot.  The final budget is at least the number of branches so a
    clarification candidate never becomes an empty branch merely because a
    different candidate ranked first.
    """
    branch_hits: list[list] = []
    trace: list[dict[str, object]] = []
    for query in plan.retrieval_queries:
        aliases = _stable_texts(glossary.aliases_in(query.query_text))
        raw_hits = index.search(query.query_text, aliases, top_k=top_k)
        boost_terms = _stable_texts([
            *aliases,
            *query.required_terms,
            *query.optional_terms,
        ])
        boosted_hits = (
            index.search(query.query_text, boost_terms, top_k=top_k)
            if boost_terms != aliases
            else raw_hits
        )
        hits = []
        branch_seen: set[tuple[str, str, str, str]] = set()
        for rank in range(max(len(raw_hits), len(boosted_hits))):
            for candidates in (raw_hits, boosted_hits):
                if rank >= len(candidates):
                    continue
                candidate = candidates[rank]
                identity = _chunk_identity(candidate)
                if identity in branch_seen:
                    continue
                branch_seen.add(identity)
                hits.append(candidate)
                if len(hits) >= top_k:
                    break
            if len(hits) >= top_k:
                break
        branch_hits.append(hits)
        trace.append({
            **query.as_dict(),
            "hit_count": len(hits),
            "raw_hit_count": len(raw_hits),
            "boosted_hit_count": len(boosted_hits),
            "hits": [f"{chunk.path} / {chunk.location}" for chunk in hits],
        })

    merged: list = []
    seen: set[tuple[str, str, str, str]] = set()
    budget = max(top_k, len(branch_hits))
    max_rank = max((len(hits) for hits in branch_hits), default=0)
    for rank in range(max_rank):
        for hits in branch_hits:
            if rank >= len(hits):
                continue
            chunk = hits[rank]
            identity = _chunk_identity(chunk)
            if identity in seen:
                continue
            seen.add(identity)
            merged.append(chunk)
            if len(merged) >= budget:
                return merged, trace
    return merged, trace


def _graph_audit_record(plan, retrieval_trace, output_validation):
    if plan is None:
        return None
    return {
        "graph_plan_version": plan.as_dict()["graph_plan_version"],
        "qur_id": plan.qur_id,
        "qic_id": plan.qic_id,
        "qur_final_status": plan.qur_final_status,
        "strict_status": plan.strict_status,
        "strict_reasons": list(plan.strict_reasons),
        "advisory_usable": plan.advisory_usable,
        "fallback_used": plan.fallback_used,
        "qur_sha256": plan.qur_sha256,
        "branch_queries": retrieval_trace or [
            query.as_dict() for query in plan.retrieval_queries
        ],
        "output_validation": output_validation,
    }


def _write_run_log(
    args,
    questions,
    retrieved,
    results,
    model,
    t_start,
    routes=None,
    structured_sources=None,
    graph_plans=None,
    graph_retrieval=None,
    output_validations=None,
    structured_decisions=None,
) -> Path:
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
            "structured_candidate": getattr(
                args,
                "structured_candidate_enabled",
                getattr(args, "structured_candidate", False),
            ),
            "answer_path": (
                "legacy"
                if getattr(args, "legacy_answer_path", False)
                else "question-graph"
            ),
            "graph_plan_version": getattr(args, "graph_plan_version", None),
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
                "回答経路": (routes or {}).get(idx, "checkpoint"),
                "question_graph": _graph_audit_record(
                    (graph_plans or {}).get(idx),
                    (graph_retrieval or {}).get(idx),
                    (output_validations or {}).get(idx),
                ),
                "structured_candidate_decision": (
                    (structured_decisions or {}).get(idx)
                ),
                "参照資料": (
                    list((structured_sources or {}).get(idx, ()))
                    if (routes or {}).get(idx) == "structured-candidate"
                    else [f"{c.path} / {c.location}" for c in retrieved.get(idx, [])]
                ),
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


def _checkpoint_signature(args, questions, graph_plans=None) -> str:
    payload = json.dumps({
        "mode": "valid" if args.valid else "test",
        "questions": questions,
        "backend": args.backend,
        "model": args.model,
        "top_k": args.top_k,
        "structured_candidate": getattr(
            args,
            "structured_candidate_enabled",
            getattr(args, "structured_candidate", False),
        ),
        "answer_path": (
            "legacy"
            if getattr(args, "legacy_answer_path", False)
            else "question-graph"
        ),
        "graph_plan_version": getattr(args, "graph_plan_version", None),
        "qur_sha256": [
            [idx, graph_plans[idx].qur_sha256]
            for idx, _ in questions
            if graph_plans is not None and idx in graph_plans
        ],
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
        "--legacy-answer-path",
        action="store_true",
        help=(
            "A/B comparison only: bypass question-graph planning and use the "
            "former raw-question retrieval/answer path"
        ),
    )
    ap.add_argument(
        "--restart-question-graphs",
        action="store_true",
        help="ignore cached question-understanding runs and rebuild every graph",
    )
    ap.add_argument(
        "--structured-candidate",
        action="store_true",
        help=(
            "legacy compatibility switch; deterministic structured execution "
            "is always enabled on the default question-graph path"
        ),
    )
    ap.add_argument(
        "--output-tag",
        help="append a safe label to prediction/ZIP filenames without overwriting prior outputs",
    )
    ap.add_argument(
        "--retrieval-mode",
        choices=["baseline", "layer1-lexical", "layer1-hybrid"],
        default="baseline",
        help="baseline remains the default; Layer-1 modes use the audited indexes without rebuilding them",
    )
    ap.add_argument("--layer1-base", type=Path, default=ROOT / "artifacts" / "layer1-v1")
    args = ap.parse_args()

    from answer import (
        BACKEND,
        MODEL,
        answer_question,
        answer_question_with_graph_result,
        make_client,
        normalize_answer,
        validate_graph_answer,
    )

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
    if args.output_tag is not None and not all(
        character.isalnum() or character in {"-", "_"}
        for character in args.output_tag
    ):
        ap.error("--output-tag は英数字・ハイフン・アンダースコアだけを使用してください")

    args.graph_plan_version = None
    build_graph_plan = None
    if not args.legacy_answer_path:
        from question_graph_runtime import GRAPH_PLAN_VERSION, build_graph_plan

        args.graph_plan_version = GRAPH_PLAN_VERSION
    args.structured_candidate_enabled = (
        not args.legacy_answer_path or args.structured_candidate
    )

    if not DOCS.exists():
        print(f"共有ドライブフォルダが見つかりません: {DOCS}")
        return 1

    OUT.mkdir(exist_ok=True)
    print("用語集を読み込み中…")
    glossary = build_glossary(DOCS)
    print(f"  用語 {len(glossary)} 件")
    structured_engine = None
    if args.structured_candidate_enabled:
        from structured_candidate import StructuredCandidateEngine

        try:
            structured_engine = StructuredCandidateEngine(DOCS, glossary)
        except (OSError, ValueError) as exc:
            print(f"構造化候補経路を初期化できませんでした: {exc}")
            return 1
        fallback_label = "グラフ回答" if not args.legacy_answer_path else "従来回答"
        print(f"  構造化候補経路: 有効（未解決時は{fallback_label}へ継続）")

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
    t_start = time.time()

    # The production path compiles every question exactly once before either
    # retrieval or answer-checkpoint reuse.  strict_status is retained for
    # audit, but aggressive mode still executes advisory/typed-fallback
    # branches instead of reverting to the old unstructured path.
    graph_plans: dict[str, object] = {}
    if not args.legacy_answer_path:
        QUESTION_GRAPH_CACHE.mkdir(exist_ok=True)
        print("質問理解グラフを構築中…（strict holdも候補分岐として実行）")
        for position, (idx, question) in enumerate(questions, 1):
            try:
                plan = build_graph_plan(
                    idx,
                    question,
                    cache_dir=QUESTION_GRAPH_CACHE,
                    timeout=args.answer_timeout,
                    restart=args.restart_question_graphs,
                    fast_advisory=True,
                )
            except Exception as exc:
                # Never hide a graph runtime failure by silently taking the
                # raw-question route.  The runtime normally converts failures
                # into typed fallback plans; reaching here is terminal.
                print(
                    f"  ! グラフ構築失敗 [{idx}] "
                    f"{type(exc).__name__}: {str(exc)[:200]}"
                )
                return 1
            graph_plans[idx] = plan
            print(
                f"  graph {position}/{len(questions)} [{idx}] "
                f"qur={plan.qur_final_status} strict={plan.strict_status} "
                f"branches={len(plan.retrieval_queries)}",
                flush=True,
            )

    graph_retrieval: dict[str, list[dict[str, object]]] = {}
    try:
        if args.legacy_answer_path:
            retrieved = {
                idx: index.search(q, glossary.aliases_in(q), top_k=args.top_k)
                for idx, q in questions
            }
        else:
            retrieved = {}
            for idx, _ in questions:
                hits, trace = _search_graph_plan(
                    index,
                    graph_plans[idx],
                    glossary,
                    args.top_k,
                )
                retrieved[idx] = hits
                graph_retrieval[idx] = trace
    except Exception as exc:
        print(f"検索に失敗しました: {type(exc).__name__}: {exc}")
        return 1

    if args.dry_run:
        for idx, q in questions[:5]:
            print(f"\n[{idx}] {q}")
            if not args.legacy_answer_path:
                plan = graph_plans[idx]
                print(
                    f"    graph: qur={plan.qur_final_status} "
                    f"strict={plan.strict_status} fallback={plan.fallback_used}"
                )
                for query in graph_retrieval[idx]:
                    first_line = str(query["query_text"]).splitlines()[0]
                    print(
                        f"    branch {query['branch_id']}: "
                        f"{first_line[:100]} (hits={query['hit_count']})"
                    )
            for c in retrieved[idx][:5]:
                print("   ", c.path, "/", c.location)
        if args.legacy_answer_path:
            print("\n--dry-run のため LLM は呼び出していません。")
        else:
            print(
                "\n--dry-run のため回答LLMは呼び出していません。"
                "質問グラフの構築は実行済みです。"
            )
        return 0

    signature = _checkpoint_signature(args, questions, graph_plans)
    results: dict[str, str] = (
        {} if args.restart_answers else _load_answer_checkpoint(signature)
    )
    if results:
        results = {key: normalize_answer(value) for key, value in results.items()}
        print(f"途中保存から再開: {len(results)}/{len(questions)} 問")

    done = [len(results)]
    checkpoint_lock = Lock()
    routes: dict[str, str] = {}
    structured_sources: dict[str, tuple[str, ...]] = {}
    structured_decisions: dict[str, dict[str, object]] = {}
    output_validations: dict[str, dict[str, object]] = {}

    # Resolve deterministic branches before requiring an answer backend.  The
    # result is cached in memory so each structured decision is attempted once
    # and graph-only deterministic answers remain usable even when no
    # generative backend is available.
    prepared_decisions: dict[str, tuple[object, dict[str, object], tuple[str, ...]]] = {}
    if structured_engine is not None:
        print("グラフ済み構造化候補を実行中…")
        for idx, q in questions:
            if idx in results:
                continue
            decision = None
            decision_audit: dict[str, object]
            structured_violations: tuple[str, ...] = ()
            try:
                if args.legacy_answer_path:
                    decision = structured_engine.decide(idx, q)
                else:
                    decision = structured_engine.decide_from_graph(
                        idx, q, graph_plans[idx]
                    )
                decision_audit = {
                    "status": decision.status,
                    "reason": decision.reason,
                }
                if (
                    not args.legacy_answer_path
                    and decision.status == "resolved"
                    and decision.result is not None
                ):
                    structured_violations = validate_graph_answer(
                        decision.result.answer,
                        graph_plans[idx],
                    )
                    if structured_violations:
                        decision_audit = {
                            "status": "rejected_output_contract",
                            "reason": decision.reason,
                            "violations": list(structured_violations),
                        }
            except Exception as exc:
                # Structured execution is an optimisation; graph-aware
                # retrieval/generation remains the aggressive fallback.
                decision_audit = {
                    "status": "error",
                    "reason": f"{type(exc).__name__}: {str(exc)[:200]}",
                }
            prepared_decisions[idx] = (
                decision,
                decision_audit,
                structured_violations,
            )
            print(
                f"  structured [{idx}] status={decision_audit['status']}",
                flush=True,
            )

    def structured_resolved(idx: str) -> bool:
        prepared = prepared_decisions.get(idx)
        if prepared is None:
            return False
        decision, _, violations = prepared
        return bool(
            decision is not None
            and decision.status == "resolved"
            and decision.result is not None
            and not violations
        )

    needs_generation = any(
        idx not in results and not structured_resolved(idx)
        for idx, _ in questions
    )
    client = None
    if needs_generation:
        client = make_client(args.backend, args.model, args.answer_timeout)

        # 未回答があるときだけ回答バックエンドを確認する。
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
    def work(item):
        idx, q = item
        if idx in results:
            return
        decision = None
        decision_audit = None
        structured_violations: tuple[str, ...] = ()
        if idx in prepared_decisions:
            decision, decision_audit, structured_violations = prepared_decisions[idx]
        if (
            decision is not None
            and decision.status == "resolved"
            and decision.result
            and not structured_violations
        ):
            a = decision.result.answer
            route = "structured-candidate"
            sources = decision.result.source_paths
            validation = {
                "validation_status": (
                    "not_applicable" if args.legacy_answer_path else "pass"
                ),
                "reason": "deterministic_structured_candidate",
                "violations": [],
                "attempts": 0,
            }
        else:
            try:
                if args.legacy_answer_path:
                    a = answer_question(client, q, retrieved[idx], glossary)
                    validation = {
                        "validation_status": "not_applicable",
                        "reason": "legacy_answer_path",
                        "violations": [],
                        "attempts": 1,
                    }
                else:
                    graph_result = answer_question_with_graph_result(
                        client,
                        q,
                        retrieved[idx],
                        graph_plans[idx],
                        glossary,
                    )
                    a = graph_result.answer
                    validation = {
                        "validation_status": graph_result.validation_status,
                        "violations": list(graph_result.violations),
                        "attempts": graph_result.attempts,
                    }
            except Exception as e:
                print(f"  ! [{idx}] {type(e).__name__}: {e}")
                a = "わかりません"
                validation = {
                    "validation_status": "generation_error",
                    "error_type": type(e).__name__,
                    "violations": [],
                    "attempts": 1,
                }
            route = (
                args.retrieval_mode
                if args.legacy_answer_path
                else f"question-graph-{args.retrieval_mode}"
            )
            sources = ()
        answer = a.replace("\n", " ").strip() or "わかりません"
        with checkpoint_lock:
            results[idx] = answer
            routes[idx] = route
            if decision_audit is not None:
                structured_decisions[idx] = decision_audit
            output_validations[idx] = validation
            if sources:
                structured_sources[idx] = sources
            done[0] += 1
            _save_answer_checkpoint(signature, results)
            print(
                f"  {done[0]}/{len(questions)} [{idx}] {route}: {answer[:50]}",
                flush=True,
            )

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        list(ex.map(work, questions))

    _write_run_log(
        args,
        questions,
        retrieved,
        results,
        args.model,
        t_start,
        routes,
        structured_sources,
        graph_plans,
        graph_retrieval,
        output_validations,
        structured_decisions,
    )

    preview = args.limit is not None
    suffix = f"_{args.output_tag}" if args.output_tag else ""
    csv_path = OUT / (
        f"predictions_preview{suffix}.csv" if preview else f"predictions{suffix}.csv"
    )
    with csv_path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        for idx, _ in questions:
            w.writerow([idx, results.get(idx, "わかりません")])

    zip_path = None
    if not preview:
        base_name = "submission_valid" if args.valid else "submission"
        zip_path = OUT / f"{base_name}{suffix}.zip"
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
            z.write(csv_path, "predictions.csv")

    unknown = sum(1 for v in results.values() if v.startswith("わかりません"))
    print(f"\n完成: {zip_path or csv_path}")
    print(f"  「わかりません」 {unknown}/{len(questions)} 件")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
