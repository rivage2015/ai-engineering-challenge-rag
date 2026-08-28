#!/usr/bin/env python3
"""Run a repeatable local answer -> unload/reuse -> audit benchmark."""

from __future__ import annotations

import argparse
import importlib.util
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / "engine" / "answer_local_memory_v2.py"
FINAL_AUDIT = ROOT / "app" / "final_answer_audit.py"
SERVER_PATH = ROOT / "app" / "local_memory_server.py"


def load_server():
    sys.path.insert(0, str(ROOT / "app"))
    spec = importlib.util.spec_from_file_location("benchmark_local_memory_server", SERVER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_json(command: list[str], timeout: int) -> tuple[dict, float]:
    started = time.perf_counter()
    process = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
    if process.returncode:
        detail = process.stderr.strip() or process.stdout.strip() or f"exit {process.returncode}"
        raise RuntimeError(detail)
    return json.loads(process.stdout), round(time.perf_counter() - started, 3)


def evaluate(case: dict, record: dict) -> dict:
    answer = record.get("answer", {})
    text = str(answer.get("answer", ""))
    required_terms = [str(value) for value in case.get("required_terms", [])]
    missing_terms = [value for value in required_terms if value not in text]
    actual_mode = str(answer.get("answer_mode", ""))
    expected_mode = str(case.get("expected_final_mode", ""))
    actual_verdict = str(record.get("independent_final_audit", {}).get("verdict", ""))
    expected_verdict = str(case.get("expected_audit_verdict", ""))
    return {
        "passed": (
            not missing_terms
            and actual_mode == expected_mode
            and (not expected_verdict or actual_verdict == expected_verdict)
        ),
        "missing_terms": missing_terms,
        "expected_final_mode": expected_mode,
        "actual_final_mode": actual_mode,
        "expected_audit_verdict": expected_verdict,
        "actual_audit_verdict": actual_verdict,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--index", required=True)
    parser.add_argument("--cases", default=str(Path(__file__).parent / "fixtures" / "le-fruitier-audit-cases.json"))
    parser.add_argument("--output", required=True)
    parser.add_argument("--answer-model", default="gemma4:12b")
    parser.add_argument("--audit-model", default="gemma4:12b")
    parser.add_argument("--case-id", action="append", help="run only the named case; repeatable")
    args = parser.parse_args()

    index = Path(args.index).resolve(strict=True)
    cases = json.loads(Path(args.cases).read_text(encoding="utf-8"))
    if args.case_id:
        selected = set(args.case_id)
        cases = [case for case in cases if case.get("id") in selected]
        missing = selected - {case.get("id") for case in cases}
        if missing:
            raise SystemExit("unknown case id: " + ", ".join(sorted(missing)))
    server = load_server()
    results = []
    for case in cases:
        try:
            primary, answer_seconds = run_json([
                sys.executable, str(ENGINE), case["query"], "--index", str(index),
                "--model", args.answer_model, "--audit-mode", "batched", "--fast-plan",
                "--no-cache", "--json",
            ], 900)
            same_model_reused = args.answer_model == args.audit_model
            answer_unload = (
                {"requested": False, "succeeded": False, "seconds": 0.0, "error": "", "reason": "same_model_reused"}
                if same_model_reused else server.unload_ollama_model(args.answer_model)
            )
            with tempfile.NamedTemporaryFile("w", suffix=".json", encoding="utf-8", delete=False) as handle:
                json.dump(primary, handle, ensure_ascii=False)
                record_path = Path(handle.name)
            try:
                audited, audit_seconds = run_json([
                    sys.executable, str(FINAL_AUDIT), "--record", str(record_path),
                    "--index", str(index), "--model", args.audit_model,
                ], 600)
            finally:
                audit_unload = server.unload_ollama_model(args.audit_model)
                record_path.unlink(missing_ok=True)
        except Exception as exc:
            server.unload_ollama_model(args.answer_model)
            server.unload_ollama_model(args.audit_model)
            results.append({
                "id": case["id"], "query": case["query"],
                "evaluation": {"passed": False, "error": f"{type(exc).__name__}: {exc}"},
            })
            print(f"{case['id']}: ERROR ({type(exc).__name__}: {exc})", flush=True)
            continue
        results.append({
            "id": case["id"],
            "query": case["query"],
            "evaluation": evaluate(case, audited),
            "answer": audited.get("answer", {}),
            "audit": audited.get("independent_final_audit", {}),
            "timing": {
                "same_model_reused_across_separate_contexts": same_model_reused,
                "answer_seconds": answer_seconds,
                "answer_unload": answer_unload,
                "audit_seconds": audit_seconds,
                "audit_unload": audit_unload,
                "total_seconds": round(answer_seconds + audit_seconds + answer_unload["seconds"] + audit_unload["seconds"], 3),
            },
            "audit_performance": audited.get("performance", {}).get("independent_final_audit", {}),
        })
        print(f"{case['id']}: {'PASS' if results[-1]['evaluation']['passed'] else 'FAIL'} ({results[-1]['timing']['total_seconds']}s)", flush=True)

    summary = {
        "case_count": len(results),
        "passed": sum(1 for item in results if item["evaluation"]["passed"]),
        "failed": sum(1 for item in results if not item["evaluation"]["passed"]),
        "total_seconds": round(sum(item.get("timing", {}).get("total_seconds", 0.0) for item in results), 3),
        "results": results,
    }
    Path(args.output).write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0 if summary["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
