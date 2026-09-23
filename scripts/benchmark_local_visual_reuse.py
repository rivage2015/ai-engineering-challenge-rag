#!/usr/bin/env python3
"""Bounded local-only comparison; never changes sources or app indexes.

Runs one image twice with reuse disabled and twice with reuse enabled, at most
three model inferences with the unchanged per-call deadline. Checks app state
before each observation; this guard is not a lock against concurrent app use.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import local_visual_observation as visual


def require_idle(states: list[Path]) -> None:
    for path in states:
        state = json.loads(path.read_text(encoding="utf-8"))
        if state.get("phase") not in {"ready", "ready_with_limits", "error", "not_started"}:
            raise RuntimeError("App is not known to be idle; benchmark not started.")


def signature(value: object) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


class InferenceBudget:
    """Refuse a fourth worker even if a would-be hit turns into a miss."""

    def __init__(self, worker) -> None:
        self.worker = worker
        self.calls = 0

    def __call__(self, *args, **kwargs):
        if self.calls >= 3:
            raise RuntimeError("Three-inference benchmark budget exhausted; no retry.")
        self.calls += 1
        return self.worker(*args, **kwargs)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path, help="New private JSON report; no overwrite")
    parser.add_argument("--idle-state", required=True, action="append", type=Path)
    args = parser.parse_args()
    require_idle(args.idle_state)
    image_bytes = visual.read_checked_image_bytes(args.image)
    input_sha256 = hashlib.sha256(image_bytes).hexdigest()
    fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    report: dict[str, object] = {
        "status": "in_progress", "mode": "real_local_inference",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "input_sha256": input_sha256,
        "reader_version": visual.VISUAL_OBSERVATION_VERSION,
        "deadline_seconds_per_observation": visual.MAX_TIMEOUT_SECONDS,
        "maximum_model_calls": 3, "arms": [],
        "scope": "one image repeated within one build; not whole-folder or answer accuracy",
    }
    all_results = []
    original_worker = visual._run_isolated_task
    budget = InferenceBudget(original_worker)
    visual._run_isolated_task = budget
    try:
        for enabled in (False, True):
            arm: dict[str, object] = {"reuse_enabled": enabled, "observations": []}
            report["arms"].append(arm)
            with visual.visual_observation_session(enabled=enabled) as memo:
                results = []
                for ordinal in (1, 2):
                    require_idle(args.idle_state)
                    print(json.dumps({"phase": "observing", "reuse_enabled": enabled,
                                      "ordinal": ordinal}), flush=True)
                    start = time.monotonic()
                    result = visual.observe_image(image_bytes, expected_input_sha256=input_sha256)
                    elapsed = time.monotonic() - start
                    results.append(result)
                    observation = {
                        "ordinal": ordinal, "elapsed_seconds": round(elapsed, 6),
                        "status": result["status"], "model_digest": result["model_digest"],
                        "observation_sha256": signature(result["observation"]),
                        "result_sha256": signature(result),
                        "model_output_sha256": result["model_output_sha256"],
                    }
                    arm["observations"].append(observation)
                    print(json.dumps(observation), flush=True)
                arm["memo"] = memo.stats()
                arm["exact_repeat_equal"] = results[0] == results[1]
                arm["observation_repeat_equal"] = results[0]["observation"] == results[1]["observation"]
                all_results.extend(results)
        if len({r["model_digest"] for r in all_results}) != 1:
            raise RuntimeError("Model changed between arms; comparison is invalid.")
        cached = report["arms"][1]
        if not cached["exact_repeat_equal"] or cached["memo"]["hits"] != 1:
            raise RuntimeError("Expected exact-result reuse was not observed.")
        normal_total = sum(r["elapsed_seconds"] for r in report["arms"][0]["observations"])
        reuse_total = sum(r["elapsed_seconds"] for r in cached["observations"])
        report.update(
            status="completed", normal_pair_seconds=normal_total, reuse_pair_seconds=reuse_total,
            pair_reduction_percent=round(100 * (1 - reuse_total / normal_total), 2),
            all_observations_equal=all(r["observation"] == all_results[0]["observation"] for r in all_results),
        )
    except Exception as exc:
        report.update(status="failed", error_type=type(exc).__name__)
        print(json.dumps({"status": "failed", "error_type": type(exc).__name__}), flush=True)
    finally:
        visual._run_isolated_task = original_worker
        report["actual_worker_calls"] = budget.calls
        report["finished_at"] = datetime.now(timezone.utc).isoformat()
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(report, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
        print(json.dumps({"report": str(args.output), "status": report["status"]}), flush=True)
    return 0 if report["status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
