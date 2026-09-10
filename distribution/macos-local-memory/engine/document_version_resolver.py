#!/usr/bin/env python3
"""Resolve document-version families with fail-closed human review.

This stage consumes the source-bound Path Graph inventory.  It never reads or
modifies source documents.  Signal-free peers are included within existing
marked filename families; mixed marked/unmarked families require human review.
Explicit path status markers and numeric versions may select one active
candidate only within undated, all-marked families under a deterministic policy.
Calendar-valid dates and date-like 202/203 numeric runs propose families, never
prove replacement or approve use. Ambiguity is preserved as
``needs_human_review`` and a human decision is reusable only while the complete
candidate set and selected source hash remain unchanged.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import hmac
import json
import math
import os
import re
import stat
import tempfile
import unicodedata
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable


SCHEMA_VERSION = "1.0"
RESOLVER_VERSION = "0.1.6"
DECISION_SCHEMA_VERSION = "2.0"
MAX_DECISION_SNAPSHOT_BYTES = 67_108_864
DOCUMENT_SUFFIXES = {
    ".csv", ".doc", ".docx", ".ods", ".pdf", ".ppt", ".pptx",
    ".tsv", ".xls", ".xlsx",
}
YEAR = re.compile(r"(?<!\d)(20\d{2})(?!\d)")
# Match the whole numeric compound before calendar validation. Invalid/unknown
# compounds must not fall back to stripping only their leading four-digit year.
DATE_LIKE = re.compile(
    r"(?<!\d)(?:202|203)[0-9]*"
    r"(?:年度|年[0-9]+月(?:[0-9]+日?)?|(?:[-_.][0-9]+)+)?(?!\d)"
)
VERSION_TOKEN = re.compile(r"(?i)(?:ver(?:sion)?[._ -]*\d+(?:[._-]\d+)*)")
VERSION_VALUE = re.compile(r"(?i)ver(?:sion)?[._ -]*(\d+(?:[._-]\d+)*)")
COPY_TOKEN = re.compile(r"(?:の)?コピー(?:\s*\(\d+\))?|\bcopy(?:\s*\(\d+\))?\b", re.I)
CURRENT_MARKERS = (
    "実際に使うもの", "実際に使う", "現在使用", "現行", "最新版", "最終版", "承認済み",
    "使うファイル", "採用版", "approved", "final", "current",
)
HISTORICAL_MARKERS = (
    "とりあえず倉庫", "旧資料", "旧版", "過去資料", "廃止", "archive", "archived",
)
DRAFT_MARKERS = (
    "下書き", "編集中", "未承認", "たたき台", "draft", "unapproved",
)


def canonical_json(value: object) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def sha256_json(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory_descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        temporary.unlink(missing_ok=True)


def normalize(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold()


def _calendar_year(token: str) -> int | None:
    """Return a valid calendar candidate's year, not its document authority.

    The token is already NFKC-normalized and starts with 202 or 203. Bound all
    integer conversions by an exact format; unknown lengths remain untouched.
    A fiscal-year label does not specify a period boundary or replacement.
    """
    if re.fullmatch(r"[0-9]{4}(?:年度)?", token):
        return int(token[:4])
    if re.fullmatch(r"[0-9]{8}", token):
        parts = (token[:4], token[4:6], token[6:8])
    else:
        separated = re.fullmatch(
            r"([0-9]{4})([-_.])([0-9]{1,2})(?:\2([0-9]{1,2}))?", token
        )
        japanese = re.fullmatch(r"([0-9]{4})年([0-9]{1,2})月([0-9]{1,2})日", token)
        if separated:
            parts = (separated[1], separated[3], separated[4] or "1")
        elif japanese:
            parts = japanese.groups()
        else:
            return None
    try:
        return date(*(int(part) for part in parts)).year
    except ValueError:
        return None


def _temporal_component(value: str) -> tuple[str, list[int]]:
    """Normalize only validated date candidates; preserve unknown numeric text.

    Legacy year signals outside 202/203 compounds, including explicit numeric
    version labels, retain their earlier caution. They are not date approvals.
    """
    value = normalize(value)
    pieces: list[str] = []
    years: set[int] = set()
    version_spans = iter(VERSION_TOKEN.finditer(value))
    version = next(version_spans, None)
    end = 0

    def legacy(part: str) -> str:
        years.update(int(year) for year in YEAR.findall(part))
        return YEAR.sub(" ", part)

    for match in DATE_LIKE.finditer(value):
        pieces.append(legacy(value[end:match.start()]))
        while version is not None and version.end() <= match.start():
            version = next(version_spans, None)
        token = match.group()
        if version is not None and version.start() < match.end():
            pieces.append(legacy(token))
        else:
            year = _calendar_year(token)
            if year is None:
                pieces.append(token)
            else:
                years.add(year)
                pieces.append(" ")
        end = match.end()
    pieces.append(legacy(value[end:]))
    return "".join(pieces), sorted(years)


def _family_component(value: str) -> str:
    value, _ = _temporal_component(value)
    value = VERSION_TOKEN.sub(" ", value)
    value = COPY_TOKEN.sub(" ", value)
    for marker in (*CURRENT_MARKERS, *HISTORICAL_MARKERS, *DRAFT_MARKERS):
        value = value.replace(normalize(marker), " ")
    return re.sub(r"[\s_\-:：()（）\[\]【】]+", "", value)


def family_key(relative_path: str) -> str:
    """Derive a conservative family key from directory and basename.

    Valid date candidates, explicit years, version labels, copy suffixes, and
    lifecycle markers are removed. Unknown/invalid date-like digits and other
    words remain. A shared key proposes peers, not a replacement relationship.
    """
    path = Path(relative_path)
    stem = _family_component(path.stem)
    parent = "/".join(
        _family_component(part) for part in path.parent.parts
        if _family_component(part)
    )
    return f"{parent}\0{stem}\0{path.suffix.casefold()}"


def markers(relative_path: str, values: Iterable[str]) -> list[str]:
    comparable = normalize(relative_path)
    return [marker for marker in values if normalize(marker) in comparable]


def load_inventory(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError(f"inventory_record_not_object:{line_number}")
            records.append(value)
    return records


def load_decisions(path: Path | None) -> dict[str, dict[str, Any]]:
    if path is None or not path.exists():
        return {}
    value = json.loads(path.read_text(encoding="utf-8"))
    records = value.get("decisions") if isinstance(value, dict) else None
    if not isinstance(records, list):
        raise ValueError("decision_file_invalid")
    decisions: dict[str, dict[str, Any]] = {}
    for record in records:
        if not isinstance(record, dict) or not isinstance(record.get("group_id"), str):
            raise ValueError("decision_record_invalid")
        decisions[record["group_id"]] = record
    return decisions


def candidate(record: dict[str, Any]) -> dict[str, Any] | None:
    relative = record.get("relative_path")
    if (
        record.get("kind") != "file"
        or record.get("read_status") != "observed"
        or not isinstance(relative, str)
        or Path(relative).suffix.casefold() not in DOCUMENT_SUFFIXES
        or not isinstance(record.get("sha256"), str)
    ):
        return None
    _, years = _temporal_component(relative)
    versions = [
        tuple(int(part) for part in re.split(r"[._-]", value))
        for value in VERSION_VALUE.findall(normalize(relative))
    ]
    current = markers(relative, CURRENT_MARKERS)
    historical = markers(relative, HISTORICAL_MARKERS)
    drafts = markers(relative, DRAFT_MARKERS)
    return {
        "relative_path": relative,
        "source_sha256": record["sha256"],
        "size_bytes": record.get("size_bytes"),
        "mtime_ns": record.get("mtime_ns"),
        "birthtime_ns": record.get("birthtime_ns"),
        "explicit_years": years,
        "explicit_versions": [list(value) for value in versions],
        "current_markers": current,
        "historical_markers": historical,
        "draft_markers": drafts,
    }


def has_version_signal(item: dict[str, Any]) -> bool:
    return any(item[name] for name in (
        "explicit_years", "explicit_versions", "current_markers",
        "historical_markers", "draft_markers",
    )) or bool(DATE_LIKE.search(normalize(item["relative_path"])))


def _has_temporal_signal(item: dict[str, Any]) -> bool:
    return bool(item["explicit_years"]) or bool(
        DATE_LIKE.search(normalize(item["relative_path"]))
    )


def candidate_set_hash(candidates: list[dict[str, Any]]) -> str:
    return sha256_json([
        {
            "relative_path": item["relative_path"],
            "source_sha256": item["source_sha256"],
            "mtime_ns": item["mtime_ns"],
            "birthtime_ns": item["birthtime_ns"],
            "explicit_years": item["explicit_years"],
            "explicit_versions": item["explicit_versions"],
            "current_markers": item["current_markers"],
            "historical_markers": item["historical_markers"],
            "draft_markers": item["draft_markers"],
        }
        for item in candidates
    ])


_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_GROUP_ID = re.compile(r"^version_set_[0-9a-f]{32}$")
_GENERATION = re.compile(r"^generation-[0-9a-f]{32}$")
_REVISION_KEYS = {
    "generation", "source_scope_sha256", "graph_sha256", "graph_file_sha256",
    "inventory_sha256", "candidate_set_sha256", "decisions_sha256",
    "resolver_version",
}
_CONSENT_KEYS = {
    "decision_schema_version", "group_id", "candidate_set_sha256", "relation",
    "selected_relative_path", "selected_source_sha256",
    "current_applicability_confirmed", "allow_ingest_index_answer",
    "reviewed_revision", "decided_by", "decided_at",
}
_SUBMISSION_KEYS = {
    "relation", "selected_relative_path", "selected_source_sha256",
    "current_applicability_confirmed", "allow_ingest_index_answer",
}


def _strict_revision(value: object) -> bool:
    if not isinstance(value, dict) or set(value) != _REVISION_KEYS:
        return False
    return (
        isinstance(value["generation"], str)
        and _GENERATION.fullmatch(value["generation"]) is not None
        and all(isinstance(value[field], str) and _HEX64.fullmatch(value[field])
                for field in ("source_scope_sha256", "graph_sha256",
                              "graph_file_sha256", "inventory_sha256",
                              "candidate_set_sha256"))
        and (value["decisions_sha256"] is None
             or isinstance(value["decisions_sha256"], str)
             and _HEX64.fullmatch(value["decisions_sha256"]) is not None)
        and isinstance(value["resolver_version"], str)
    )


def _strict_consent_shape(value: object) -> bool:
    if not isinstance(value, dict) or set(value) != _CONSENT_KEYS:
        return False
    if (value["decision_schema_version"] != DECISION_SCHEMA_VERSION
            or not isinstance(value["group_id"], str)
            or _GROUP_ID.fullmatch(value["group_id"]) is None
            or not isinstance(value["candidate_set_sha256"], str)
            or _HEX64.fullmatch(value["candidate_set_sha256"]) is None
            or value["relation"] not in {
                "same_work_revisions", "independent_records", "defer"
            }
            or type(value["current_applicability_confirmed"]) is not bool
            or type(value["allow_ingest_index_answer"]) is not bool
            or not isinstance(value["decided_by"], str) or not value["decided_by"]
            or not isinstance(value["decided_at"], str) or not value["decided_at"]
            or not _strict_revision(value["reviewed_revision"])):
        return False
    if value["relation"] == "same_work_revisions":
        return (isinstance(value["selected_relative_path"], str)
                and isinstance(value["selected_source_sha256"], str)
                and _HEX64.fullmatch(value["selected_source_sha256"]) is not None)
    return (value["selected_relative_path"] is None
            and value["selected_source_sha256"] is None
            and value["current_applicability_confirmed"] is False
            and value["allow_ingest_index_answer"] is False)


def validate_dated_consent(
    key: str, candidates: list[dict[str, Any]], decision: object,
) -> dict[str, Any]:
    """Validate bounded consent; never infer Human authority from old fields."""
    hold = lambda reason: {
        "status": "HOLD", "reason_code": reason, "selected_relative_path": None,
    }
    if not isinstance(decision, dict) or "decision_schema_version" not in decision:
        return hold("dated_consent_required")
    if not _strict_consent_shape(decision):
        return hold("dated_consent_invalid")
    ordered = sorted(candidates, key=lambda item: item["relative_path"].encode("utf-8"))
    expected_group = "version_set_" + sha256_json({"family_key": key})[:32]
    expected_set = candidate_set_hash(ordered)
    if decision["reviewed_revision"]["resolver_version"] != RESOLVER_VERSION:
        return hold("dated_consent_policy_changed")
    if (decision["group_id"] != expected_group
            or decision["candidate_set_sha256"] != expected_set
            or decision["reviewed_revision"]["candidate_set_sha256"] != expected_set):
        return hold("stale_human_decision")
    relation = decision["relation"]
    if relation == "independent_records":
        return hold("independent_records_require_separate_use_review")
    if relation == "defer":
        return hold("human_deferred")
    selected = next((item for item in ordered
                     if item["relative_path"] == decision["selected_relative_path"]), None)
    if (selected is None
            or decision["selected_source_sha256"] != selected["source_sha256"]):
        return hold("stale_human_decision")
    if decision["current_applicability_confirmed"] is not True:
        return hold("dated_consent_current_not_confirmed")
    if decision["allow_ingest_index_answer"] is not True:
        return hold("dated_consent_use_not_approved")
    return {
        "status": "ALLOW_SELECTION",
        "reason_code": "human_confirmed_same_work_current_and_use",
        "selected_relative_path": selected["relative_path"],
    }


def prepare_dated_consent(
    key: str, candidates: list[dict[str, Any]], submission: object, *,
    displayed_revision: object, current_revision: object,
    actor: str, decided_at: str,
) -> dict[str, Any]:
    """Prepare a consent record only when the reviewed revision is still current."""
    if (not isinstance(submission, dict) or set(submission) != _SUBMISSION_KEYS
            or submission.get("relation") not in {
                "same_work_revisions", "independent_records", "defer"
            }
            or type(submission.get("current_applicability_confirmed")) is not bool
            or type(submission.get("allow_ingest_index_answer")) is not bool
            or not isinstance(actor, str) or not actor
            or not isinstance(decided_at, str) or not decided_at
            or not _strict_revision(displayed_revision)
            or not _strict_revision(current_revision)):
        raise ValueError("dated_consent_invalid")
    if current_revision["resolver_version"] != RESOLVER_VERSION:
        raise ValueError("dated_consent_policy_changed")
    if displayed_revision["decisions_sha256"] != current_revision["decisions_sha256"]:
        raise ValueError("dated_consent_store_changed")
    if displayed_revision != current_revision:
        raise ValueError("dated_consent_display_stale")
    ordered = sorted(candidates, key=lambda item: item["relative_path"].encode("utf-8"))
    expected_set = candidate_set_hash(ordered)
    if current_revision["candidate_set_sha256"] != expected_set:
        raise ValueError("dated_consent_source_changed")
    selected_path = submission["selected_relative_path"]
    selected_sha = submission["selected_source_sha256"]
    relation = submission["relation"]
    if relation == "same_work_revisions":
        selected = next((item for item in ordered
                         if item["relative_path"] == selected_path), None)
        if (not isinstance(selected_path, str) or selected is None
                or not isinstance(selected_sha, str)
                or selected_sha != selected["source_sha256"]):
            raise ValueError("dated_consent_source_changed")
    elif (selected_path is not None or selected_sha is not None
          or submission["current_applicability_confirmed"] is not False
          or submission["allow_ingest_index_answer"] is not False):
        raise ValueError("dated_consent_invalid")
    record = {
        "decision_schema_version": DECISION_SCHEMA_VERSION,
        "group_id": "version_set_" + sha256_json({"family_key": key})[:32],
        "candidate_set_sha256": expected_set,
        **submission,
        "reviewed_revision": dict(displayed_revision),
        "decided_by": actor,
        "decided_at": decided_at,
    }
    if not _strict_consent_shape(record):
        raise ValueError("dated_consent_invalid")
    return record


def automatic_selection(candidates: list[dict[str, Any]]) -> tuple[str | None, str, list[str]]:
    if any(not has_version_signal(item) for item in candidates):
        return None, "unmarked_candidate_requires_human_review", [
            item["relative_path"] for item in candidates
        ]
    current = [
        item for item in candidates
        if item["current_markers"]
        and not item["draft_markers"]
        and not item["historical_markers"]
    ]
    if len(current) > 1:
        return None, "multiple_current_markers", [item["relative_path"] for item in current]
    if current:
        proposed = current[0]
        dated = [item for item in candidates if item["explicit_years"]]
        if proposed["explicit_years"] and dated:
            maximum_year = max(max(item["explicit_years"]) for item in dated)
            if max(proposed["explicit_years"]) < maximum_year:
                return None, "current_marker_conflicts_with_latest_year", [
                    proposed["relative_path"],
                    *[
                        item["relative_path"] for item in dated
                        if maximum_year in item["explicit_years"]
                    ],
                ]
        if len(proposed["explicit_versions"]) == 1:
            proposed_version = tuple(proposed["explicit_versions"][0])
            higher_versions = [
                item for item in candidates
                if len(item["explicit_versions"]) == 1
                and tuple(item["explicit_versions"][0]) > proposed_version
            ]
            if higher_versions:
                # Draft/historical labels do not settle a conflicting version
                # number. Keep both signals for a human to assess.
                return None, "current_marker_conflicts_with_latest_version", [
                    proposed["relative_path"],
                    *[item["relative_path"] for item in higher_versions],
                ]
        if any(_has_temporal_signal(item) for item in candidates):
            return None, "dated_family_requires_human_review", [
                item["relative_path"] for item in candidates
            ]
        return proposed["relative_path"], "unique_explicit_current_marker", []
    if all(item["explicit_years"] for item in candidates):
        maximum_year = max(max(item["explicit_years"]) for item in candidates)
        newest = [item for item in candidates if maximum_year in item["explicit_years"]]
        if len(newest) != 1:
            return None, "latest_year_not_unique", [item["relative_path"] for item in newest]
        proposed = newest[0]
        if proposed["draft_markers"]:
            return None, "latest_year_is_draft", [proposed["relative_path"]]
        if proposed["historical_markers"]:
            return None, "latest_year_marked_historical", [proposed["relative_path"]]
        # A later year may identify an independent reporting period. Preserve
        # the unresolved relationship, including mixed year/version signals.
        return None, "year_order_does_not_establish_supersession", [
            item["relative_path"] for item in candidates
        ]
    if any(_has_temporal_signal(item) for item in candidates):
        return None, "dated_family_requires_human_review", [
            item["relative_path"] for item in candidates
        ]
    if all(item["explicit_versions"] for item in candidates):
        maximum_version = max(
            max(tuple(value) for value in item["explicit_versions"])
            for item in candidates
        )
        newest = [
            item for item in candidates
            if maximum_version in {tuple(value) for value in item["explicit_versions"]}
        ]
        if len(newest) == 1 and not (
            newest[0]["draft_markers"] or newest[0]["historical_markers"]
        ):
            return newest[0]["relative_path"], "unique_latest_explicit_version", []
        return None, "latest_version_not_unique_or_not_active", [
            item["relative_path"] for item in newest
        ]
    return None, "insufficient_comparable_version_signals", [
        item["relative_path"] for item in candidates
    ]


def resolve_group(
    key: str,
    candidates: list[dict[str, Any]],
    decision: dict[str, Any] | None,
) -> dict[str, Any]:
    candidates.sort(key=lambda item: item["relative_path"].encode("utf-8"))
    set_hash = candidate_set_hash(candidates)
    group_id = "version_set_" + sha256_json({"family_key": key})[:32]
    selected: str | None = None
    resolution_basis = "automatic"
    reason_code = ""
    conflicts: list[str] = []
    stale_decision_reason: str | None = None
    dated_family = any(_has_temporal_signal(item) for item in candidates)
    dated_decision_evaluated = decision is not None and dated_family
    if dated_decision_evaluated:
        consent = validate_dated_consent(key, candidates, decision)
        reason_code = consent["reason_code"]
        if consent["status"] == "ALLOW_SELECTION":
            selected = consent["selected_relative_path"]
            resolution_basis = "human"
    elif decision is not None:
        proposed = decision.get("selected_relative_path")
        selected_candidate = next(
            (item for item in candidates if item["relative_path"] == proposed), None
        )
        if decision.get("candidate_set_sha256") != set_hash:
            stale_decision_reason = "candidate_set_changed"
        elif selected_candidate is None:
            stale_decision_reason = "selected_candidate_missing"
        elif decision.get("selected_source_sha256") != selected_candidate["source_sha256"]:
            stale_decision_reason = "selected_source_changed"
        else:
            selected = proposed
            resolution_basis = "human"
            reason_code = "human_confirmed_active"
    if selected is None and not dated_decision_evaluated:
        selected, reason_code, conflicts = automatic_selection(candidates)
        if stale_decision_reason is not None:
            selected = None
            reason_code = "stale_human_decision"
            conflicts = [stale_decision_reason]
    status = "resolved" if selected is not None else "needs_human_review"
    return {
        "group_id": group_id,
        "family_key_sha256": sha256_json(key),
        "candidate_set_sha256": set_hash,
        "status": status,
        "selected_relative_path": selected,
        "resolution_basis": resolution_basis if selected else None,
        "reason_code": reason_code,
        "conflicts": conflicts,
        "candidates": [
            {
                **item,
                "disposition": (
                    "active" if item["relative_path"] == selected
                    else "historical" if selected else "needs_human_review"
                ),
            }
            for item in candidates
        ],
    }


def graph_projection(groups: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    for group in groups:
        set_node = group["group_id"]
        nodes.append({
            "node_id": set_node,
            "node_type": "document_version_set",
            "status": group["status"],
            "candidate_set_sha256": group["candidate_set_sha256"],
        })
        document_ids: dict[str, str] = {}
        for item in group["candidates"]:
            document_id = "document_version_" + sha256_json({
                "relative_path": item["relative_path"],
                "source_sha256": item["source_sha256"],
            })[:32]
            document_ids[item["relative_path"]] = document_id
            nodes.append({
                "node_id": document_id,
                "node_type": "document_version",
                "status": item["disposition"],
                "relative_path": item["relative_path"],
                "source_sha256": item["source_sha256"],
                "explicit_years": item["explicit_years"],
                "explicit_versions": item["explicit_versions"],
            })
            edges.append({
                "edge_id": "edge_" + sha256_json([document_id, "candidate_for", set_node])[:32],
                "from_node_id": document_id,
                "edge_type": "candidate_for",
                "to_node_id": set_node,
                "basis": "normalized_filename_family",
                "status": "candidate",
            })
        if group["selected_relative_path"]:
            selected_id = document_ids[group["selected_relative_path"]]
            edges.append({
                "edge_id": "edge_" + sha256_json([set_node, "active_version", selected_id])[:32],
                "from_node_id": set_node,
                "edge_type": "active_version",
                "to_node_id": selected_id,
                "basis": group["reason_code"],
                "status": "verified" if group["resolution_basis"] == "human" else "policy_resolved",
            })
    nodes.sort(key=lambda item: item["node_id"])
    edges.sort(key=lambda item: item["edge_id"])
    return nodes, edges


def validation_policy() -> dict[str, Any]:
    return {
        "creation_time_is_authoritative": False,
        "modification_time_is_authoritative": False,
        "candidate_rule": "same_family_valid_documents_with_at_least_one_version_signal",
        "mixed_signal_action": "needs_human_review",
        "automatic_rule": "mixed_signal_hold_else_current_conflicts_hold_else_dated_family_hold_else_unique_current_or_comparable_latest_version",
        "date_candidate_prefixes": ["202", "203"],
        "date_likeness_is_authoritative": False,
        "calendar_validity_establishes_supersession": False,
        "year_order_establishes_supersession": False,
        "ambiguous_action": "needs_human_review",
        "historical_sources_retained": True,
    }


def build(
    inventory_path: Path,
    output_path: Path,
    decisions_path: Path | None = None,
) -> dict[str, Any]:
    records = load_inventory(inventory_path)
    decisions = load_decisions(decisions_path)
    grouped: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        item = candidate(record)
        if item is None:
            continue
        grouped.setdefault(family_key(item["relative_path"]), []).append(item)
    groups = []
    for key, candidates in sorted(grouped.items()):
        if len(candidates) < 2 or not any(has_version_signal(item) for item in candidates):
            continue
        group_id = "version_set_" + sha256_json({"family_key": key})[:32]
        groups.append(resolve_group(key, candidates, decisions.get(group_id)))
    nodes, edges = graph_projection(groups)
    core = {
        "schema_version": SCHEMA_VERSION,
        "resolver": "document-version-resolver",
        "resolver_version": RESOLVER_VERSION,
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "source": {
            "inventory_path": str(inventory_path.resolve()),
            "inventory_sha256": sha256_file(inventory_path),
            "decisions_path": str(decisions_path.resolve()) if decisions_path else None,
            "decisions_sha256": sha256_file(decisions_path) if decisions_path and decisions_path.exists() else None,
        },
        "policy": validation_policy(),
        "counts": {
            "groups": len(groups),
            "resolved": sum(item["status"] == "resolved" for item in groups),
            "needs_human_review": sum(item["status"] == "needs_human_review" for item in groups),
        },
        "groups": groups,
        "nodes": nodes,
        "edges": edges,
    }
    result = {**core, "graph_sha256": sha256_json(core)}
    atomic_json(output_path, result)
    return result


def _validation_json(raw: bytes) -> Any:
    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate_json_key")
            result[key] = value
        return result

    def invalid_constant(_value: str) -> None:
        raise ValueError("nonfinite_json_constant")

    def finite_float(value: str) -> float:
        parsed = float(value)
        if not math.isfinite(parsed):
            raise ValueError("nonfinite_json_number")
        return parsed

    return json.loads(
        raw.decode("utf-8"), object_pairs_hook=unique_object,
        parse_constant=invalid_constant, parse_float=finite_float,
    )


def _validation_inventory(raw: bytes) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    paths: set[str] = set()
    # Match text-mode universal newlines without splitting Unicode characters
    # such as U+2028 that may legally occur inside JSON strings.
    text = raw.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
    for line in text.split("\n"):
        if not line.strip():
            continue
        record = _validation_json(line.encode("utf-8"))
        if not isinstance(record, dict):
            raise ValueError("inventory_record_not_object")
        relative = record.get("relative_path")
        if isinstance(relative, str):
            if relative in paths:
                raise ValueError("duplicate_inventory_relative_path")
            paths.add(relative)
        records.append(record)
    return records


def _validation_decisions(raw: bytes | None) -> dict[str, dict[str, Any]]:
    if raw is None:
        return {}
    value = _validation_json(raw)
    records = value.get("decisions") if isinstance(value, dict) else None
    if not isinstance(records, list):
        raise ValueError("decision_file_invalid")
    result: dict[str, dict[str, Any]] = {}
    for record in records:
        if not isinstance(record, dict) or not isinstance(record.get("group_id"), str):
            raise ValueError("decision_record_invalid")
        group_id = record["group_id"]
        if group_id in result:
            raise ValueError("duplicate_decision_group_id")
        result[group_id] = record
    return result


def _validation_components(
    records: list[dict[str, Any]], decisions: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Reconstruct from caller snapshots; submitted graph fields are absent."""
    grouped: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        item = candidate(record)
        if item is not None:
            grouped.setdefault(family_key(item["relative_path"]), []).append(item)
    groups = []
    for key, candidates in sorted(grouped.items()):
        if len(candidates) < 2 or not any(has_version_signal(item) for item in candidates):
            continue
        group_id = "version_set_" + sha256_json({"family_key": key})[:32]
        groups.append(resolve_group(key, candidates, decisions.get(group_id)))
    nodes, edges = graph_projection(groups)
    return {
        "schema_version": SCHEMA_VERSION,
        "resolver": "document-version-resolver",
        "resolver_version": RESOLVER_VERSION,
        "policy": validation_policy(),
        "groups": groups, "nodes": nodes, "edges": edges,
        "counts": {
            "groups": len(groups),
            "resolved": sum(item["status"] == "resolved" for item in groups),
            "needs_human_review": sum(item["status"] == "needs_human_review" for item in groups),
        },
    }


def read_decision_snapshot(path: Path, limit: int) -> bytes:
    """Read one bounded regular-file snapshot, without following its final link."""
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError("decision_snapshot_not_regular_file")
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            raw = handle.read(limit + 1)
        if len(raw) > limit:
            raise ValueError("decision_snapshot_too_large")
        return raw
    finally:
        os.close(descriptor)


def _decision_store_snapshot(path: Path, limit: int) -> tuple[bool, bytes | None]:
    if type(limit) is not int or not 1 <= limit <= MAX_DECISION_SNAPSHOT_BYTES:
        raise ValueError("decision_snapshot_limit_invalid")
    if path.is_symlink():
        raise ValueError("decision_snapshot_source_not_regular_file")
    try:
        return True, read_decision_snapshot(path, limit)
    except FileNotFoundError:
        if path.is_symlink():
            raise ValueError("decision_snapshot_source_not_regular_file")
        return False, None


def decision_store_revision(
    path: Path, limit: int = MAX_DECISION_SNAPSHOT_BYTES,
) -> dict[str, Any]:
    """Return a raw-byte revision; absence is deliberately not an empty file."""
    exists, raw = _decision_store_snapshot(path, limit)
    return {
        "exists": exists,
        "sha256": hashlib.sha256(raw).hexdigest() if raw is not None else None,
        "byte_count": len(raw) if raw is not None else 0,
    }


def _decision_store_envelope(raw: bytes | None) -> dict[str, Any]:
    if raw is None:
        return {"schema_version": "2.0", "decisions": [], "inactive_decisions": []}
    value = _validation_json(raw)
    if not isinstance(value, dict) or value.get("schema_version") not in {"1.0", "2.0"}:
        raise ValueError("decision_file_invalid")
    expected_keys = (
        {"schema_version", "decisions"}
        if value["schema_version"] == "1.0"
        else {"schema_version", "decisions", "inactive_decisions"}
    )
    if set(value) != expected_keys or not isinstance(value.get("decisions"), list):
        raise ValueError("decision_file_invalid")
    _validation_decisions(raw)
    inactive = value.get("inactive_decisions", [])
    if not isinstance(inactive, list) or any(not isinstance(item, dict) for item in inactive):
        raise ValueError("decision_file_invalid")
    return {
        "schema_version": "2.0",
        "decisions": list(value["decisions"]),
        "inactive_decisions": list(inactive),
    }


def _decision_store_lock_path(path: Path) -> Path:
    return path.with_name("." + path.name + ".lock")


class _DecisionStoreLease:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.descriptor: int | None = None

    def __enter__(self) -> "_DecisionStoreLease":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        self.descriptor = os.open(_decision_store_lock_path(self.path), flags, 0o600)
        os.fchmod(self.descriptor, 0o600)
        metadata = os.fstat(self.descriptor)
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid()
                or metadata.st_nlink != 1):
            os.close(self.descriptor)
            self.descriptor = None
            raise ValueError("decision_store_lock_invalid")
        fcntl.flock(self.descriptor, fcntl.LOCK_EX)
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        if self.descriptor is not None:
            fcntl.flock(self.descriptor, fcntl.LOCK_UN)
            os.close(self.descriptor)
            self.descriptor = None


def record_dated_consent_cas(
    decisions_path: Path,
    record: dict[str, Any],
    expected_sha256: str | None,
    limit: int = MAX_DECISION_SNAPSHOT_BYTES,
) -> dict[str, Any]:
    """Persist one complete dated consent iff the displayed store is current."""
    if (not _strict_consent_shape(record)
            or record["reviewed_revision"]["resolver_version"] != RESOLVER_VERSION):
        raise ValueError("dated_consent_invalid")
    if expected_sha256 is not None and (
        not isinstance(expected_sha256, str) or _HEX64.fullmatch(expected_sha256) is None
    ):
        raise ValueError("dated_consent_invalid")
    with _DecisionStoreLease(decisions_path):
        _exists, raw = _decision_store_snapshot(decisions_path, limit)
        current_sha256 = hashlib.sha256(raw).hexdigest() if raw is not None else None
        if not hmac.compare_digest(current_sha256 or "", expected_sha256 or ""):
            raise ValueError("dated_consent_store_changed")
        envelope = _decision_store_envelope(raw)
        active: dict[str, dict[str, Any]] = {
            item["group_id"]: item for item in envelope["decisions"]
        }
        previous = active.get(record["group_id"])
        if previous is not None:
            envelope["inactive_decisions"].append(previous)
        active[record["group_id"]] = dict(record)
        envelope["decisions"] = [active[key] for key in sorted(active)]
        atomic_json(decisions_path, envelope)
    return dict(record)


def attest(
    graph_path: Path, inventory_path: Path, *, decision_mode: str,
    decisions_path: Path | None = None, expected_decisions_sha256: str | None = None,
) -> dict[str, Any]:
    """Validate against explicit input snapshots, without discovering authority.

    Stored source paths are diagnostic metadata only. In particular, historical
    graph validation must not infer today's decision path from those strings.
    """
    errors: list[str] = []
    try:
        if decision_mode not in ("no_decisions", "explicit_decisions", "snapshot"):
            raise ValueError("decision_authority_mode_invalid")
        if decision_mode == "no_decisions":
            if decisions_path is not None or expected_decisions_sha256 is not None:
                raise ValueError("decision_authority_inputs_forbidden")
        else:
            if not isinstance(decisions_path, (str, Path)) or not str(decisions_path):
                raise ValueError("decision_authority_path_required")
            decisions_path = Path(decisions_path)
            if decision_mode == "snapshot":
                if not isinstance(expected_decisions_sha256, str) or re.fullmatch(r"[0-9a-f]{64}", expected_decisions_sha256) is None:
                    raise ValueError("decision_snapshot_expected_hash_required")
            elif expected_decisions_sha256 is not None:
                raise ValueError("decision_authority_hash_forbidden")
        graph_path, inventory_path = Path(graph_path), Path(inventory_path)
        graph_raw = graph_path.read_bytes()
        inventory_raw = inventory_path.read_bytes()
        decision_raw = None
        if decision_mode == "snapshot":
            decision_raw = read_decision_snapshot(decisions_path, MAX_DECISION_SNAPSHOT_BYTES)
            if hashlib.sha256(decision_raw).hexdigest() != expected_decisions_sha256:
                raise ValueError("decision_snapshot_hash_mismatch")
        elif decisions_path is not None:
            try:
                decision_raw = decisions_path.read_bytes()
            except FileNotFoundError:
                pass
        graph = _validation_json(graph_raw)
        records = _validation_inventory(inventory_raw)
        decisions = _validation_decisions(decision_raw)
        expected = _validation_components(records, decisions)
        required = {*expected, "created_at", "source", "graph_sha256"}
        if not isinstance(graph, dict) or set(graph) != required:
            raise ValueError("version_graph_fields_invalid")
        source = graph["source"]
        if not isinstance(source, dict) or set(source) != {
            "inventory_path", "inventory_sha256", "decisions_path", "decisions_sha256",
        }:
            raise ValueError("version_graph_source_invalid")
        if (
            not isinstance(source["inventory_path"], str)
            or not isinstance(source["inventory_sha256"], str)
            or not (source["decisions_path"] is None or isinstance(source["decisions_path"], str))
            or not (source["decisions_sha256"] is None or isinstance(source["decisions_sha256"], str))
            or not isinstance(graph["created_at"], str) or not graph["created_at"]
        ):
            raise ValueError("version_graph_metadata_invalid")
        core = {key: value for key, value in graph.items() if key != "graph_sha256"}
        if graph["graph_sha256"] != sha256_json(core):
            errors.append("graph_hash_mismatch")
        if source["inventory_sha256"] != hashlib.sha256(inventory_raw).hexdigest():
            errors.append("inventory_changed")
        decision_hash = hashlib.sha256(decision_raw).hexdigest() if decision_raw is not None else None
        if source["decisions_sha256"] is not None and decisions_path is None:
            errors.append("decision_authority_required")
        elif source["decisions_sha256"] != decision_hash:
            errors.append("decisions_changed")
        for key, value in expected.items():
            # Canonical serialization distinguishes booleans, ints and floats,
            # and retains exact list ordering and all extra/missing fields.
            if canonical_json(graph[key]) != canonical_json(value):
                errors.append(f"version_graph_{key}_mismatch")
    except (OSError, ValueError, TypeError, KeyError, RecursionError) as exc:
        # Invalid input is a failure; test-guard AssertionError and interrupts
        # intentionally propagate instead of being disguised as data failures.
        errors.append(f"version_validation_input_invalid:{type(exc).__name__}:{exc}")
    if errors:
        return {"status": "FAIL", "errors": errors}
    authority = {"mode": decision_mode}
    if decision_mode != "no_decisions":
        authority.update(path=str(decisions_path), sha256=decision_hash,
                         byte_count=len(decision_raw) if decision_raw is not None else 0)
    return {
        "status": "PASS", "errors": [],
        "inventory": {"sha256": hashlib.sha256(inventory_raw).hexdigest(), "records": records},
        "version": {
            "groups": expected["groups"],
            "dispositions": {item["relative_path"]: item["disposition"]
                             for group in expected["groups"] for item in group["candidates"]},
        },
        "document_version_graph": {
            "path": str(graph_path), "sha256": hashlib.sha256(graph_raw).hexdigest(),
            "graph_sha256": graph["graph_sha256"], "decision_authority": authority,
        },
    }


def validate(
    graph_path: Path, inventory_path: Path, decisions_path: Path | None = None,
) -> dict[str, Any]:
    report = attest(graph_path, inventory_path,
                    decision_mode="no_decisions" if decisions_path is None else "explicit_decisions",
                    decisions_path=decisions_path)
    return {"status": report["status"], "errors": report["errors"]}


def record_decision(
    graph_path: Path,
    decisions_path: Path,
    group_id: str,
    selected_relative_path: str,
    actor: str,
) -> dict[str, Any]:
    graph = json.loads(graph_path.read_text(encoding="utf-8"))
    core = {key: value for key, value in graph.items() if key != "graph_sha256"}
    if graph.get("graph_sha256") != sha256_json(core):
        raise ValueError("decision_graph_hash_mismatch")
    group = next((item for item in graph.get("groups", []) if item.get("group_id") == group_id), None)
    if group is None:
        raise ValueError("decision_group_missing")
    selected = next(
        (item for item in group["candidates"] if item["relative_path"] == selected_relative_path), None
    )
    if selected is None:
        raise ValueError("decision_candidate_missing")
    if any(_has_temporal_signal(item) for item in group["candidates"]):
        # The legacy command records only a chosen path.  A dated family needs
        # the independently captured relationship, current-applicability, use
        # permission, and reviewed-revision fields from prepare_dated_consent.
        # Refuse before reading or writing the decision store.
        raise ValueError("dated_consent_submission_required")
    new_record = {
        "group_id": group_id,
        "candidate_set_sha256": group["candidate_set_sha256"],
        "selected_relative_path": selected_relative_path,
        "selected_source_sha256": selected["source_sha256"],
        "decided_by": actor,
        "decided_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    with _DecisionStoreLease(decisions_path):
        _exists, raw = _decision_store_snapshot(
            decisions_path, MAX_DECISION_SNAPSHOT_BYTES
        )
        envelope = _decision_store_envelope(raw)
        existing = {
            item["group_id"]: item for item in envelope["decisions"]
        }
        previous = existing.get(group_id)
        if previous is not None and envelope["schema_version"] == "2.0":
            envelope["inactive_decisions"].append(previous)
        existing[group_id] = new_record
        envelope["decisions"] = [existing[key] for key in sorted(existing)]
        # The shared writer now always emits the strict envelope. Legacy
        # undated records remain accepted as active compatibility records.
        atomic_json(decisions_path, envelope)
    return new_record


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    build_parser = subparsers.add_parser("build")
    build_parser.add_argument("--inventory", required=True, type=Path)
    build_parser.add_argument("--output", required=True, type=Path)
    build_parser.add_argument("--decisions", type=Path)
    validate_parser = subparsers.add_parser("validate")
    validate_parser.add_argument("--graph", required=True, type=Path)
    validate_parser.add_argument("--inventory", required=True, type=Path)
    validate_parser.add_argument("--decisions", type=Path)
    validate_parser.add_argument("--decision-mode", choices=("no_decisions", "explicit_decisions", "snapshot"))
    validate_parser.add_argument("--decisions-sha256")
    decide_parser = subparsers.add_parser("decide")
    decide_parser.add_argument("--graph", required=True, type=Path)
    decide_parser.add_argument("--decisions", required=True, type=Path)
    decide_parser.add_argument("--group-id", required=True)
    decide_parser.add_argument("--select", required=True)
    decide_parser.add_argument("--actor", default="human")
    args = parser.parse_args()
    if args.command == "build":
        result = build(args.inventory.resolve(strict=True), args.output, args.decisions)
        print(json.dumps(result["counts"], ensure_ascii=False, indent=2))
    elif args.command == "validate":
        if args.decision_mode is None and args.decisions_sha256 is None:
            result = validate(args.graph, args.inventory, args.decisions)
        else:
            report = attest(args.graph, args.inventory, decision_mode=args.decision_mode,
                            decisions_path=args.decisions, expected_decisions_sha256=args.decisions_sha256)
            result = {"status": report["status"], "errors": report["errors"]}
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["status"] == "PASS" else 1
    else:
        result = record_decision(
            args.graph.resolve(strict=True), args.decisions, args.group_id,
            args.select, args.actor,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
