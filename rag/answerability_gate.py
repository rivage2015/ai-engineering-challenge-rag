"""Question-independent answerability gate for fail-closed graph execution."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

UNKNOWN_ANSWER = "わかりません"


@dataclass(frozen=True)
class AnswerabilityDecision:
    status: str
    action: str
    reason_codes: tuple[str, ...]
    details: Mapping[str, Any]


def evaluate_answerability(
    *,
    required_conditions: Mapping[str, bool],
    interpretations: Mapping[str, object],
    selected_candidates: Sequence[object],
) -> AnswerabilityDecision:
    """Answer only when conditions, interpretation and selection are unique.

    The gate never chooses between interpretations and never converts ranges to
    point values.  Callers retain their source-derived alternatives in details.
    """

    missing = tuple(sorted(key for key, value in required_conditions.items() if value is not True))
    normalized_interpretations = {
        str(key): repr(value) for key, value in sorted(interpretations.items())
    }
    interpretation_values = set(normalized_interpretations.values())
    candidate_values = tuple(repr(value) for value in selected_candidates)
    reasons = []
    if missing:
        reasons.append("extraction_unresolved")
    if len(interpretation_values) != 1:
        reasons.append("intent_ambiguous")
    if len(set(candidate_values)) != 1 or len(candidate_values) != 1:
        reasons.append("conflicting_evidence")
    action = "answer" if not reasons else "abstain"
    return AnswerabilityDecision(
        status="pass" if action == "answer" else "indeterminate",
        action=action,
        reason_codes=tuple(reasons),
        details={
            "missing_conditions": missing,
            "interpretations": normalized_interpretations,
            "selected_candidates": candidate_values,
        },
    )


__all__ = ["AnswerabilityDecision", "UNKNOWN_ANSWER", "evaluate_answerability"]
