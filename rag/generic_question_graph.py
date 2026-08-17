"""Question-only lexical advisory graph compiler.

This module is the broad, fail-soft companion to the strict Phase-2 question
compiler.  It never claims that a lexical candidate is authoritative.  Its
job is to retain explicit question atoms in a typed, deterministic graph so
retrieval and generation do not fall back to the raw question alone.

Only the question and the shared question-language registry enter the public
API.  Source files, source contents, answers, predictions, and question IDs do
not affect semantic compilation or generated identities.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any


HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from question_language_registry import (  # noqa: E402
    ALL_CARDINALITY_SURFACES,
    CALCULATION_OPERATORS,
    CALCULATION_PRECISION_KEYWORDS,
    CANONICAL_TARGET_TYPE_LEXEMES,
    DIRECT_OPERATIONS,
    JAPANESE_DIGITS,
    MULTIPLE_CARDINALITY_SURFACES,
    OPERATION_KEYWORDS,
    OPERATOR_MENTION_MAP,
    SINGLE_CARDINALITY_SURFACES,
    SORT_ORDER_KEYWORDS,
    registry_metadata,
)


COMPILER_NAME = "generic-question-graph"
COMPILER_VERSION = "0.1"
SCHEMA_VERSION = "0.1"
MAX_QUESTION_CODEPOINTS = 100_000
MAX_ATOMS = 2_048

FILE_EXTENSIONS = (
    "csv",
    "tsv",
    "xlsx",
    "xls",
    "json",
    "jsonl",
    "parquet",
    "pdf",
    "docx",
    "doc",
    "pptx",
    "ppt",
    "txt",
    "md",
    "py",
    "ipynb",
)
_FILE_PATTERN = re.compile(
    r"(?P<token>[\w./-]{1,240}\.(?:"
    + "|".join(FILE_EXTENSIONS)
    + r"))(?![A-Za-z0-9_.-])",
    flags=re.IGNORECASE,
)
_CONNECTED_FILE_TOKEN_PATTERN = re.compile(
    r"(?:^|から|と)(?P<token>[\w./-]+?\.(?:"
    + "|".join(FILE_EXTENSIONS)
    + r"))(?=から|と|$)",
    flags=re.IGNORECASE,
)
_SCOPE_PATTERN = re.compile(
    r"(?:^|[\u3001。\n])(?P<body>[^\u3001。\n]{1,240}?)(?:において|にて)",
    flags=re.IGNORECASE,
)
_DATE_PATTERN = re.compile(
    r"(?<![0-9])(?:20[0-9]{2}[-/][01]?[0-9][-/][0-3]?[0-9]|"
    r"20[0-9]{2}年(?:1[0-2]|[1-9])月(?:3[01]|[12]?[0-9])日)(?![0-9])"
)
_DECIMAL_PATTERN = re.compile(
    r"小数第\s*(?P<digits>[0-9０-９零〇一二三四五六七八九十]+)\s*位"
)
_EXTREMUM_PATTERN = re.compile(
    r"最も(?P<direction>高い|大きい|多い|低い|小さい)"
)
_REQUESTED_DIFFERENCE_PATTERN = re.compile(
    r"差(?!分)\s*(?:は|を)\s*(?:いくら|何(?:円|%|％)?|計算|算出|求め)"
)
_RATIO_PATTERN = re.compile(r"何\s*倍")
_UNRESOLVED_PATTERN = re.compile(
    r"[A-Za-z_][A-Za-z0-9_.:/+-]*|"
    r"[0-9０-９]+(?:\.[0-9０-９]+)?|"
    r"[^　\s\u3001。，,.!?！？:：;；()（）\[\]{}<>「」『』]+"
)

UNIT_SURFACES: Mapping[str, str] = {
    "ページ": "ページ",
    "時間": "時間",
    "％": "%",
    "%": "%",
    "円": "円",
    "日": "日",
    "歳": "歳",
    "年": "年",
    "週": "週",
}

SPECIAL_CARDINALITY_SURFACES: Mapping[str, tuple[str, str | None]] = {
    # いくつ can ask for a value ("Accuracyはいくつ") rather than a row
    # count.  Only the explicit people/record-count forms compile ``count``.
    "いくつ": ("single", None),
    "何人": ("count", None),
    "何日": ("single", "日"),
    # A page question asks for one page-number value; it does not ask for the
    # number of matching records.  Keep the explicit unit, but do not compile
    # a count operation from it.
    "何ページ": ("single", "ページ"),
    "第何週": ("single", "週"),
    "何件": ("count", None),
}

ROUNDING_SURFACES: Mapping[str, str] = {
    "四捨五入": "half_up",
    "整数": "integer",
    "切り上げ": "ceiling",
    "切り捨て": "floor",
}

PER_ITEM_SURFACES: Mapping[str, str] = {
    "それぞれ": "per_item",
    "各々": "per_item",
    "各": "per_item",
}

_SPECIFIC_CALCULATIONS = frozenset(
    {
        "sum",
        "mean",
        "min",
        "max",
        "absolute_distance",
        "argmin_all",
        "argmax_all",
    }
)
_TERMINAL_RETURN_FIELDS: Mapping[str, str] = {
    "count": "count",
    "list": "unknown",
    "sum": "value",
    "mean": "value",
    "min": "value",
    "max": "value",
    "calculate": "value",
    "absolute_distance": "value",
    "argmin_all": "unknown",
    "argmax_all": "unknown",
    "compare": "comparison_result",
    "explain": "reason",
    "procedure": "procedure",
    "verify": "boolean",
    "boolean_test": "boolean",
    "retrieve": "unknown",
}


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _identifier(prefix: str, value: Any, length: int = 24) -> str:
    return f"{prefix}_{_sha256(value)[:length]}"


def _normalized(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold().strip()


def _surface_pattern(surface: str) -> re.Pattern[str]:
    escaped = re.escape(surface)
    if surface and all(
        character.isascii()
        and (character.isalnum() or character in {"_", "-", "."})
        for character in surface
    ):
        return re.compile(
            rf"(?<![A-Za-z0-9_]){escaped}(?![A-Za-z0-9_])",
            flags=re.IGNORECASE,
        )
    return re.compile(escaped, flags=re.IGNORECASE)


def _candidate(
    question: str,
    *,
    kind: str,
    start: int,
    end: int,
    canonical: Any,
    registry_ref: str | None,
    details: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    surface = question[start:end]
    core: dict[str, Any] = {
        "kind": kind,
        "surface": surface,
        "normalized": _normalized(surface),
        "start": start,
        "end": end,
        "canonical": canonical,
        "registry_ref": registry_ref,
        "status": "explicit_lexical_candidate",
    }
    if details:
        core["details"] = dict(details)
    return {
        "atom_id": _identifier("qatom", core, 20),
        **core,
    }


def _longest_nonoverlapping(
    candidates: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Apply deterministic left-to-right longest match at each start."""

    pending = sorted(
        candidates,
        key=lambda item: (
            item["start"],
            -(item["end"] - item["start"]),
            item["kind"],
            _canonical_json(item.get("canonical")),
        ),
    )
    result: list[dict[str, Any]] = []
    cursor = -1
    index = 0
    while index < len(pending):
        while index < len(pending) and pending[index]["start"] < cursor:
            index += 1
        if index >= len(pending):
            break
        start = pending[index]["start"]
        same_start: list[dict[str, Any]] = []
        while index < len(pending) and pending[index]["start"] == start:
            same_start.append(pending[index])
            index += 1
        selected = same_start[0]
        result.append(selected)
        cursor = selected["end"]
    return result


def _scan_mapping(
    question: str,
    *,
    kind: str,
    surfaces: Mapping[str, Any],
    registry_ref: str | None,
) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    for surface, canonical in surfaces.items():
        for match in _surface_pattern(surface).finditer(question):
            found.append(
                _candidate(
                    question,
                    kind=kind,
                    start=match.start(),
                    end=match.end(),
                    canonical=canonical,
                    registry_ref=registry_ref,
                )
            )
    return _longest_nonoverlapping(found)


def _scan_operations(question: str) -> list[dict[str, Any]]:
    surfaces = {
        surface: operator
        for operator, values in OPERATION_KEYWORDS.items()
        for surface in values
    }
    atoms = _scan_mapping(
        question,
        kind="operation",
        surfaces=surfaces,
        registry_ref="operation_keywords",
    )
    for match in _EXTREMUM_PATTERN.finditer(question):
        direction = match.group("direction")
        suffix = question[match.end() : match.end() + 40]
        requests_metric_value = bool(
            re.match(
                r"\s*(?:カウント数|件数|人数|回数|値|スコア|金額|給与|割合|率|点)"
                r"\s*(?:は|を)\s*(?:いくつ|いくら)",
                suffix,
            )
        )
        atoms.append(
            _candidate(
                question,
                kind="operation",
                start=match.start(),
                end=match.end(),
                canonical=(
                    "argmin_all"
                    if direction in {"低い", "小さい"}
                    else "argmax_all"
                ),
                registry_ref="operation_keywords:composed_extremum",
                details={
                    "composition": ["最も", direction],
                    "extremum_role": "candidate_selection",
                    "role": "semantic_operation",
                },
            )
        )
        if requests_metric_value:
            atoms.append(
                _candidate(
                    question,
                    kind="operation",
                    start=match.start(),
                    end=match.end(),
                    canonical=(
                        "min" if direction in {"低い", "小さい"} else "max"
                    ),
                    registry_ref="operation_keywords:composed_extremum_value",
                    details={
                        "composition": ["extremum_candidate", "metric_value"],
                        "extremum_role": "requested_metric_value",
                        "role": "semantic_operation",
                    },
                )
            )
    for match in _REQUESTED_DIFFERENCE_PATTERN.finditer(question):
        atoms.append(
            _candidate(
                question,
                kind="operation",
                start=match.start(),
                end=match.end(),
                canonical="absolute_distance",
                registry_ref="operation_keywords:composed_requested_difference",
                details={
                    "composition": ["差", "numeric_request"],
                    "role": "semantic_operation",
                },
            )
        )
    for match in _RATIO_PATTERN.finditer(question):
        atoms.append(
            _candidate(
                question,
                kind="operation",
                start=match.start(),
                end=match.end(),
                canonical="calculate",
                registry_ref="operation_keywords:composed_ratio",
                details={
                    "composition": ["何", "倍"],
                    "calculation_kind": "ratio",
                    "role": "semantic_operation",
                },
            )
        )
    atoms = _dedupe_atoms(atoms)
    has_boolean_request = any(
        atom["canonical"] == "boolean_test" for atom in atoms
    )
    for atom in atoms:
        if atom["canonical"] == "retrieve" and atom["normalized"] in {
            _normalized("教え"),
            _normalized("答え"),
        }:
            atom.setdefault("details", {})["role"] = "speech_act"
        elif atom["canonical"] == "verify" and not has_boolean_request:
            # "notebookを確認して、Xを教えて" and supplemental source
            # instructions such as "社内管理を確認してください" are inspection
            # steps, not requests for a yes/no answer.  Only an explicit
            # boolean-test construction promotes verify to a semantic node.
            atom.setdefault("details", {})["role"] = "supporting_inspection"
        elif (
            atom["canonical"] == "explain"
            and atom["normalized"] == _normalized("説明")
            and question[atom["end"] : atom["end"] + 1] == "性"
        ):
            atom.setdefault("details", {})["role"] = "descriptor_mention"
        elif (
            atom["canonical"] == "absolute_distance"
            and atom["normalized"] == _normalized("差分")
            and not re.match(
                r"\s*(?:を)?\s*(?:計算|算出|求め)",
                question[atom["end"] : atom["end"] + 16],
            )
        ):
            atom.setdefault("details", {})["role"] = "descriptor_mention"
        elif (
            atom["canonical"] == "mean"
            and re.match(
                r"\s*(?:給与|年収|収入|賃金|金額|価格|単価|工数|時間|年齢|"
                r"スコア|率|件数|回数|人数|点)",
                question[atom["end"] : atom["end"] + 12],
            )
        ):
            atom.setdefault("details", {})["role"] = "metric_mention"
        elif (
            atom["canonical"] == "count"
            and atom["normalized"] in {
                _normalized("件数"),
                _normalized("カウント"),
            }
            and re.match(
                r"\s*(?:数\s*)?(?:が|は|の|推移|分布|グラフ|ヒストグラム|"
                r"系列|傾向)",
                question[atom["end"] : atom["end"] + 8],
            )
        ):
            atom.setdefault("details", {})["role"] = "metric_mention"
        elif (
            atom["canonical"] == "calculate"
            and re.match(
                r"\s*(?:された|済み|してある)",
                question[atom["end"] : atom["end"] + 12],
            )
        ):
            atom.setdefault("details", {})["role"] = "precomputed_mention"
        else:
            atom.setdefault("details", {})["role"] = "semantic_operation"
    return atoms


def _scan_operators(question: str) -> list[dict[str, Any]]:
    return _scan_mapping(
        question,
        kind="comparison_operator",
        surfaces=OPERATOR_MENTION_MAP,
        registry_ref="operator_mention_map",
    )


def _scan_cardinality(question: str) -> list[dict[str, Any]]:
    registry_surfaces: dict[str, tuple[str, str | None]] = {}
    registry_surfaces.update(
        {surface: ("all", None) for surface in ALL_CARDINALITY_SURFACES}
    )
    registry_surfaces.update(
        {
            surface: ("multiple", None)
            for surface in MULTIPLE_CARDINALITY_SURFACES
        }
    )
    registry_surfaces.update(
        {surface: ("single", None) for surface in SINGLE_CARDINALITY_SURFACES}
    )
    registry_surfaces.update(SPECIAL_CARDINALITY_SURFACES)
    found: list[dict[str, Any]] = []
    for surface, (mode, implied_unit) in registry_surfaces.items():
        for match in _surface_pattern(surface).finditer(question):
            contextual_mode = mode
            contextual_reason: str | None = None
            if surface == "いくつ" and re.match(
                r"\s*(?:あり|ある|存在し|い(?:ます|る))",
                question[match.end() : match.end() + 16],
            ):
                contextual_mode = "count"
                contextual_reason = "existence_count_construction"
            reference = (
                "cardinality_surfaces"
                if surface not in SPECIAL_CARDINALITY_SURFACES
                else "generic_count_surfaces"
            )
            found.append(
                _candidate(
                    question,
                    kind="cardinality",
                    start=match.start(),
                    end=match.end(),
                    canonical=contextual_mode,
                    registry_ref=reference,
                    details={
                        "implied_unit": implied_unit,
                        "contextual_reason": contextual_reason,
                    },
                )
            )
    return _longest_nonoverlapping(found)


def _scan_units(question: str) -> list[dict[str, Any]]:
    return _scan_mapping(
        question,
        kind="unit",
        surfaces=UNIT_SURFACES,
        registry_ref="generic_output_units",
    )


def _scan_targets(question: str) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    for target_type, lexemes in CANONICAL_TARGET_TYPE_LEXEMES.items():
        for lexeme in lexemes:
            # Single Japanese codepoints such as 行, 表, 値, and 列 are useful
            # to the strict grammar only with surrounding syntax.  A generic
            # substring scan would otherwise fabricate targets from words
            # such as 実行時 and 銀行, so retain them as raw atoms instead.
            if len(_normalized(lexeme)) == 1:
                continue
            for match in _surface_pattern(lexeme).finditer(question):
                found.append(
                    _candidate(
                        question,
                        kind="target",
                        start=match.start(),
                        end=match.end(),
                        canonical=target_type,
                        registry_ref="canonical_target_type_lexemes",
                    )
                )
    return _longest_nonoverlapping(found)


def _japanese_integer(token: str) -> int | None:
    normalized = unicodedata.normalize("NFKC", token)
    if normalized.isdecimal():
        try:
            return int(normalized)
        except ValueError:
            return None
    reverse = {
        surface: value
        for value, surfaces in JAPANESE_DIGITS.items()
        for surface in surfaces
    }
    if normalized in reverse:
        return reverse[normalized]
    if "十" not in normalized or normalized.count("十") != 1:
        return None
    left, right = normalized.split("十")
    tens = 1 if not left else reverse.get(left)
    ones = 0 if not right else reverse.get(right)
    if tens is None or ones is None or not 1 <= tens <= 9 or not 0 <= ones <= 9:
        return None
    return tens * 10 + ones


def _scan_precision(question: str) -> list[dict[str, Any]]:
    atoms: list[dict[str, Any]] = []
    for match in _DECIMAL_PATTERN.finditer(question):
        digits = _japanese_integer(match.group("digits"))
        atoms.append(
            _candidate(
                question,
                kind="display_precision",
                start=match.start(),
                end=match.end(),
                canonical={"mode": "decimal_places", "digits": digits},
                registry_ref="japanese_digits",
                details={"parsed": digits is not None},
            )
        )
    for precision, surfaces in CALCULATION_PRECISION_KEYWORDS.items():
        for surface in surfaces:
            for match in _surface_pattern(surface).finditer(question):
                atoms.append(
                    _candidate(
                        question,
                        kind="calculation_precision",
                        start=match.start(),
                        end=match.end(),
                        canonical=precision,
                        registry_ref="calculation_precision_keywords",
                    )
                )
    atoms.extend(
        _scan_mapping(
            question,
            kind="rounding",
            surfaces=ROUNDING_SURFACES,
            registry_ref="generic_rounding_surfaces",
        )
    )
    return sorted(atoms, key=lambda item: (item["start"], item["end"], item["kind"]))


def _scan_sort_order(question: str) -> list[dict[str, Any]]:
    surfaces = {
        surface: order
        for order, values in SORT_ORDER_KEYWORDS.items()
        for surface in values
    }
    return _scan_mapping(
        question,
        kind="sort_order",
        surfaces=surfaces,
        registry_ref="sort_order_keywords",
    )


def _scan_files(question: str) -> list[dict[str, Any]]:
    atoms: list[dict[str, Any]] = []
    for match in _FILE_PATTERN.finditer(question):
        token = match.group("token")
        start = match.start("token")
        # Unicode ``\w`` can include the preceding Japanese possessive scope.
        # Keep the final component as the file candidate and let the scope
        # scanner preserve the preceding literal separately.
        if "の" in token:
            prefix, token = token.rsplit("の", 1)
            start += len(prefix) + 1
        if not token or "." not in token:
            continue
        # ``\w`` deliberately supports Japanese file names, but also consumes
        # Japanese connectors.  Split only when both sides are extension-
        # terminated file tokens, so ``A.pptxからB.pptx`` and ``A.pdfとB.pdf``
        # become two literal candidates without splitting ordinary names.
        connected_parts = list(_CONNECTED_FILE_TOKEN_PATTERN.finditer(token))
        parts = (
            [(part.group("token"), part.start("token")) for part in connected_parts]
            if connected_parts
            else [(token, 0)]
        )
        for part_token, relative_start in parts:
            part_start = start + relative_start
            atoms.append(
                _candidate(
                    question,
                    kind="file_name",
                    start=part_start,
                    end=part_start + len(part_token),
                    canonical=unicodedata.normalize("NFC", part_token),
                    registry_ref="generic_file_extensions",
                )
            )
    return _longest_nonoverlapping(atoms)


def _scan_scope(
    question: str,
    file_atoms: Sequence[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    atoms: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    for match in _SCOPE_PATTERN.finditer(question):
        body = match.group("body").strip()
        leading = len(match.group("body")) - len(match.group("body").lstrip())
        body_start = match.start("body") + leading
        if "の" not in body:
            continue
        location, container = body.rsplit("の", 1)
        location = location.strip()
        container = container.strip()
        if not location or not container:
            continue
        location_start = body_start
        container_start = body_start + body.rfind("の") + 1
        location_atom = _candidate(
            question,
            kind="scope_location",
            start=location_start,
            end=location_start + len(location),
            canonical=location,
            registry_ref="generic_scope_literal",
        )
        container_atom = _candidate(
            question,
            kind="scope_container",
            start=container_start,
            end=container_start + len(container),
            canonical=container,
            registry_ref="generic_scope_literal",
        )
        atoms.extend((location_atom, container_atom))
        candidates.append(
            {
                "location": location,
                "container": container,
                "basis_atom_ids": [
                    location_atom["atom_id"],
                    container_atom["atom_id"],
                ],
                "status": "literal_candidate",
            }
        )

    for file_atom in file_atoms:
        if any(
            candidate["container"] == file_atom["canonical"]
            for candidate in candidates
        ):
            continue
        candidates.append(
            {
                "location": None,
                "container": file_atom["canonical"],
                "basis_atom_ids": [file_atom["atom_id"]],
                "status": "literal_candidate",
            }
        )
    for match in _DATE_PATTERN.finditer(question):
        atoms.append(
            _candidate(
                question,
                kind="scope_time",
                start=match.start(),
                end=match.end(),
                canonical=unicodedata.normalize("NFKC", match.group(0)),
                registry_ref="generic_date_literal",
            )
        )
    return atoms, candidates


def _dedupe_atoms(atoms: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    unique: dict[tuple[Any, ...], dict[str, Any]] = {}
    for atom in atoms:
        key = (
            atom["kind"],
            atom["start"],
            atom["end"],
            _canonical_json(atom.get("canonical")),
        )
        unique[key] = atom
    return sorted(
        unique.values(),
        key=lambda item: (item["start"], item["end"], item["kind"]),
    )[:MAX_ATOMS]


def _unresolved_atoms(
    question: str,
    recognized: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    covered = [False] * len(question)
    for atom in recognized:
        for index in range(max(0, atom["start"]), min(len(question), atom["end"])):
            covered[index] = True
    atoms: list[dict[str, Any]] = []
    for match in _UNRESOLVED_PATTERN.finditer(question):
        start, end = match.span()
        cursor = start
        while cursor < end:
            while cursor < end and covered[cursor]:
                cursor += 1
            segment_start = cursor
            while cursor < end and not covered[cursor]:
                cursor += 1
            if segment_start == cursor:
                continue
            surface = question[segment_start:cursor].strip()
            if not surface:
                continue
            adjusted_start = segment_start + len(
                question[segment_start:cursor]
            ) - len(question[segment_start:cursor].lstrip())
            adjusted_end = adjusted_start + len(surface)
            atoms.append(
                _candidate(
                    question,
                    kind="unresolved",
                    start=adjusted_start,
                    end=adjusted_end,
                    canonical=unicodedata.normalize("NFKC", surface),
                    registry_ref=None,
                    details={"reason": "no_registered_lexical_type"},
                )
            )
    return _dedupe_atoms(atoms)


def _node(
    index: int,
    operator: str,
    input_refs: Sequence[str],
    basis: Sequence[dict[str, Any]],
    **options: Any,
) -> dict[str, Any]:
    core = {
        "index": index,
        "operator": operator,
        "input_refs": list(input_refs),
        "basis_atom_ids": [atom["atom_id"] for atom in basis],
        "options": options,
    }
    operation_id = f"op_{index:03d}_{operator}"
    return {
        "operation_id": operation_id,
        "operator": operator,
        "input_refs": list(input_refs),
        "output_ref": f"value_{index:03d}",
        "advisory": True,
        "inference_basis": {
            "kind": "explicit_lexical" if basis else "runtime_scaffold",
            "atom_ids": [atom["atom_id"] for atom in basis],
        },
        **options,
        "node_sha256": _sha256(core),
    }


def _cardinality_decision(
    atoms: Sequence[dict[str, Any]],
) -> tuple[str, int | None, list[dict[str, Any]], bool]:
    modes = {str(atom["canonical"]) for atom in atoms}
    if len(modes) != 1:
        return "unknown", None, list(atoms), False
    mode = next(iter(modes))
    if mode == "count":
        return "single", 1, list(atoms), True
    if mode == "single":
        if len(atoms) != 1:
            return "unknown", None, list(atoms), False
        return "single", 1, list(atoms), True
    if mode in {"all", "multiple"}:
        return mode, None, list(atoms), True
    return "unknown", None, list(atoms), False


def _output_unit_atoms(
    question: str,
    unit_atoms: Sequence[dict[str, Any]],
    cardinality_atoms: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Select only units explicitly attached to the requested answer.

    All unit mentions remain in ``lexical_atoms``.  This narrower set controls
    the enforceable answer contract, preventing dates, filter thresholds, and
    source page/section mentions from leaking into the output shape.
    """

    if re.search(r"何\s*年\s*何\s*月|何年何月", question):
        # A compound date is not a scalar value carrying only the 年 unit.
        return []
    selected: list[dict[str, Any]] = []
    implied_spans = [
        (atom["start"], atom["end"], atom.get("details", {}).get("implied_unit"))
        for atom in cardinality_atoms
        if atom.get("details", {}).get("implied_unit")
    ]
    for atom in unit_atoms:
        start = int(atom["start"])
        end = int(atom["end"])
        unit = str(atom["canonical"])
        overlaps_implied = any(
            implied == unit and span_start <= start and end <= span_end
            for span_start, span_end, implied in implied_spans
        )
        prefix = question[max(0, start - 8) : start]
        suffix = question[end : min(len(question), end + 16)]
        wh_attached = bool(re.search(r"(?:第\s*)?何\s*$", prefix))
        named_page_value = unit == "ページ" and bool(
            re.match(r"\s*(?:番号|数)", suffix)
        )
        explicit_unit_format = bool(
            re.match(
                r"\s*単位\s*(?:で|に|まで|として)?\s*(?:答|回答|示|出力|算出|計算|切)",
                suffix,
            )
        )
        direct_answer_format = bool(
            re.match(r"\s*(?:で|として)\s*(?:答|回答|示|出力)", suffix)
        )
        if (
            overlaps_implied
            or wh_attached
            or named_page_value
            or explicit_unit_format
            or direct_answer_format
        ):
            selected.append(atom)
    return _dedupe_atoms(selected)


def _operation_graph(
    question: str,
    operation_atoms: Sequence[dict[str, Any]],
    cardinality_atoms: Sequence[dict[str, Any]],
    precision_atoms: Sequence[dict[str, Any]],
    sort_atoms: Sequence[dict[str, Any]],
) -> tuple[dict[str, Any], str, list[dict[str, Any]]]:
    count_explicit = any(atom["canonical"] == "count" for atom in cardinality_atoms)
    list_explicit = any(
        atom["canonical"] in {"all", "multiple"} for atom in cardinality_atoms
    ) and not count_explicit
    semantic_atoms = [
        atom
        for atom in operation_atoms
        if atom.get("details", {}).get("role") == "semantic_operation"
        and atom["canonical"] != "retrieve"
    ]
    specific_atoms = [
        atom
        for atom in semantic_atoms
        if atom["canonical"] in _SPECIFIC_CALCULATIONS
    ]
    semantic_atoms = [
        atom
        for atom in semantic_atoms
        if atom["canonical"] != "calculate"
        or not any(
            specific["end"] <= atom["start"]
            and atom["start"] - specific["end"] <= 8
            for specific in specific_atoms
        )
    ]
    if count_explicit and not any(atom["canonical"] == "count" for atom in semantic_atoms):
        semantic_atoms.append(
            min(
                (atom for atom in cardinality_atoms if atom["canonical"] == "count"),
                key=lambda item: item["start"],
            )
        )

    # Registry synonyms and later noun-like references can repeat the same
    # canonical operator (for example 理由を説明, or 平均を計算し、その平均に
    # 最も近い…).  Preserve every lexical atom in detected_operation_sequence,
    # while collapsing only adjacent equal semantic steps in the executable
    # advisory graph.  Different intervening operations remain distinct.
    ordered_semantic_atoms = sorted(
        semantic_atoms,
        key=lambda item: (
            item["start"],
            item["end"],
            0
            if item["canonical"] in {"argmin_all", "argmax_all"}
            else 1,
            item["atom_id"],
        ),
    )
    semantic_atoms = []
    for atom in ordered_semantic_atoms:
        if semantic_atoms and semantic_atoms[-1]["canonical"] == atom["canonical"]:
            continue
        semantic_atoms.append(atom)

    nodes: list[dict[str, Any]] = []
    retrieve_basis = [
        atom
        for atom in operation_atoms
        if atom["canonical"] == "retrieve"
        and atom.get("details", {}).get("role") == "semantic_operation"
    ]
    nodes.append(_node(0, "retrieve", ["input_question_scope"], retrieve_basis))
    source_ref = nodes[0]["output_ref"]
    previous_ref = source_ref

    for atom in semantic_atoms:
        operator = str(atom["canonical"])
        if operator == "count" and count_explicit and nodes[-1]["operator"] == "count":
            continue
        options: dict[str, Any] = {}
        inputs = [previous_ref]
        if operator in {"argmin_all", "argmax_all"} and previous_ref != source_ref:
            inputs = [source_ref, previous_ref]
            options["candidate_set_ref"] = source_ref
            options["tie_policy"] = "all"
        matching_precision = [
            item
            for item in precision_atoms
            if item["kind"] == "calculation_precision"
        ]
        if operator in CALCULATION_OPERATORS and len(
            {item["canonical"] for item in matching_precision}
        ) == 1:
            options["calculation_precision"] = matching_precision[0]["canonical"]
        if operator == "sort" and len({item["canonical"] for item in sort_atoms}) == 1:
            options["sort_order"] = sort_atoms[0]["canonical"]
        node = _node(len(nodes), operator, inputs, [atom], **options)
        nodes.append(node)
        # list/count are output branches, not typed transforms.  Keeping the
        # working value ref lets a later explicit calculation branch from the
        # same retrieved candidate set (e.g. "all IDs, and their total").
        if operator not in {"list", "count"}:
            previous_ref = node["output_ref"]

    if nodes[-1]["operator"] in {"argmin_all", "argmax_all"}:
        extremum_basis_ids = set(nodes[-1]["inference_basis"]["atom_ids"])
        extremum_atoms = [
            atom for atom in operation_atoms if atom["atom_id"] in extremum_basis_ids
        ]
        extremum_end = max(
            (int(atom["end"]) for atom in extremum_atoms),
            default=len(question),
        )
        projection_basis = [
            atom
            for atom in operation_atoms
            if atom["canonical"] == "retrieve"
            and atom.get("details", {}).get("role") == "speech_act"
            and int(atom["start"]) > extremum_end
        ]
        tail = question[extremum_end:]
        if projection_basis and re.search(r"(?:とき|場合|際)の", tail):
            # The extremum selects a candidate/parameter, while the requested
            # answer is a value projected from that selection (for example,
            # the F1 score at the threshold that maximises F1).  A terminal
            # retrieve keeps argmax as an intermediate instead of falsely
            # declaring the selected candidate itself as the answer.
            projection = min(
                projection_basis,
                key=lambda atom: (atom["start"], atom["end"], atom["atom_id"]),
            )
            node = _node(len(nodes), "retrieve", [previous_ref], [projection])
            nodes.append(node)
            previous_ref = node["output_ref"]

    terminal_basis: list[dict[str, Any]] = []
    present_operators = {node["operator"] for node in nodes}
    if count_explicit and "count" not in present_operators:
        terminal = "count"
        terminal_basis = [atom for atom in cardinality_atoms if atom["canonical"] == "count"]
    elif list_explicit and "list" not in present_operators:
        terminal = "list"
        terminal_basis = [
            atom
            for atom in cardinality_atoms
            if atom["canonical"] in {"all", "multiple"}
        ]
    else:
        terminal = nodes[-1]["operator"]
        basis_ids = set(nodes[-1]["inference_basis"]["atom_ids"])
        terminal_basis = [
            atom for atom in operation_atoms if atom["atom_id"] in basis_ids
        ]

    if nodes[-1]["operator"] != terminal:
        node = _node(len(nodes), terminal, [previous_ref], terminal_basis)
        nodes.append(node)
    terminal_node = nodes[-1]
    producer_by_output = {item["output_ref"]: item for item in nodes}
    edges: list[dict[str, str]] = []
    for node in nodes:
        for input_ref in node["input_refs"]:
            producer = producer_by_output.get(input_ref)
            if producer is None:
                continue
            edge = {"from": producer["operation_id"], "to": node["operation_id"]}
            if edge not in edges:
                edges.append(edge)
    graph_core = {
        "external_inputs": [
            {
                "input_ref": "input_question_scope",
                "input_type": "unknown",
                "source": "question",
                "source_ref": "question:lexical_scope",
                "description": "Question-only advisory retrieval scope",
            }
        ],
        "nodes": nodes,
        "edges": edges,
        "scope_inheritance": {
            "default": "inherit_previous_output",
            "reset_requires": "explicit_instruction",
        },
    }
    return {
        "operation_graph_id": _identifier("graph_advisory", graph_core),
        **graph_core,
        "detected_operation_sequence": [
            {
                "operator": atom["canonical"],
                "surface": atom["surface"],
                "span": {"start": atom["start"], "end": atom["end"]},
                "atom_id": atom["atom_id"],
                "role": atom.get("details", {}).get("role"),
            }
            for atom in operation_atoms
        ],
    }, terminal, terminal_basis


def _target_contract(target_atoms: Sequence[dict[str, Any]]) -> dict[str, Any]:
    types = {str(atom["canonical"]) for atom in target_atoms}
    surfaces = {str(atom["surface"]) for atom in target_atoms}
    return {
        "surface": next(iter(surfaces)) if len(surfaces) == 1 else None,
        "canonical_type": next(iter(types)) if len(types) == 1 else None,
        "instance": None,
        "status": "explicit_candidate" if target_atoms else "unknown",
        "candidates": [
            {
                "surface": atom["surface"],
                "canonical_type": atom["canonical"],
                "atom_id": atom["atom_id"],
            }
            for atom in target_atoms
        ],
    }


def _scope_contract(
    scope_candidates: Sequence[dict[str, Any]],
    scope_atoms: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    containers = {
        candidate["container"]
        for candidate in scope_candidates
        if candidate.get("container")
    }
    locations = {
        candidate["location"]
        for candidate in scope_candidates
        if candidate.get("location")
    }
    times = {
        atom["canonical"] for atom in scope_atoms if atom["kind"] == "scope_time"
    }
    resolved_literal = len(containers) == 1 and len(locations) <= 1
    return {
        "container": next(iter(containers)) if len(containers) == 1 else None,
        "location": next(iter(locations)) if len(locations) == 1 else None,
        "time_or_version": next(iter(times)) if len(times) == 1 else None,
        "filters": [],
        "source": "explicit" if resolved_literal else "unknown",
        "match_mode": "exact" if resolved_literal else "unknown",
        "status": "literal_candidate" if scope_candidates else "unknown",
        "literal_candidates": list(scope_candidates),
        "operator_candidates": [],
    }


def _requested_output(
    output_index: int,
    terminal: str,
    terminal_node: Mapping[str, Any],
    cardinality_atoms: Sequence[dict[str, Any]],
    target: Mapping[str, Any],
    unit_atoms: Sequence[dict[str, Any]],
    precision_atoms: Sequence[dict[str, Any]],
    per_item_atoms: Sequence[dict[str, Any]],
    terminal_basis: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    mode, expected_count, cardinal_basis, cardinal_enforceable = _cardinality_decision(
        cardinality_atoms
    )
    count_explicit = any(atom["canonical"] == "count" for atom in cardinality_atoms)
    explicit_id = any(
        "id" in _normalized(candidate["surface"])
        or "識別子" in candidate["surface"]
        for candidate in target.get("candidates", [])
    )
    units = {str(atom["canonical"]) for atom in unit_atoms}
    implied_units = {
        str(atom.get("details", {}).get("implied_unit"))
        for atom in cardinality_atoms
        if atom.get("details", {}).get("implied_unit")
    }
    all_units = units | implied_units
    unit = next(iter(all_units)) if len(all_units) == 1 else None

    display_candidates = [
        atom
        for atom in precision_atoms
        if atom["kind"] == "display_precision"
        and atom.get("details", {}).get("parsed") is True
    ]
    display_values = {
        _canonical_json(atom["canonical"]): atom["canonical"]
        for atom in display_candidates
    }
    display_precision = (
        next(iter(display_values.values())) if len(display_values) == 1 else None
    )

    numeric_terminal = terminal in {
        "calculate",
        "sum",
        "mean",
        "min",
        "max",
        "absolute_distance",
    }

    if count_explicit:
        container = "scalar"
        value_type = "integer"
        return_field = "count"
    elif numeric_terminal and not per_item_atoms:
        container = "scalar"
        value_type = "number"
        return_field = "value"
        mode = "single"
        expected_count = 1
        cardinal_basis = list(terminal_basis)
        cardinal_enforceable = bool(terminal_basis)
    elif mode in {"all", "multiple"}:
        container = "list"
        value_type = "identifier" if explicit_id else "unknown"
        return_field = "identifier" if explicit_id else "unknown"
    elif mode == "single":
        container = "scalar"
        value_type = "identifier" if explicit_id else "unknown"
        return_field = "identifier" if explicit_id else _TERMINAL_RETURN_FIELDS.get(
            terminal, "unknown"
        )
    else:
        container = (
            "prose"
            if terminal in {"explain", "procedure"}
            else "yes_no"
            if terminal in {"verify", "boolean_test"}
            else "unknown"
        )
        if explicit_id and terminal in {"list", "argmin_all", "argmax_all", "retrieve"}:
            value_type = "identifier"
            return_field = "identifier"
        elif terminal in {"verify", "boolean_test"}:
            value_type = "boolean"
            return_field = "boolean"
        elif numeric_terminal and (unit is not None or display_precision):
            value_type = "number"
            return_field = "value"
        else:
            value_type = "unknown"
            return_field = _TERMINAL_RETURN_FIELDS.get(terminal, "unknown")

    conflicts: list[str] = []
    if display_precision is not None and any(
        atom["canonical"] == "single"
        and atom["normalized"] == _normalized("いくつ")
        for atom in cardinality_atoms
    ):
        # ``Xはいくつ`` is a generic scalar cue, not an integer/count type.
        # Coupling it to a decimal contract would make an advisory guess
        # authoritative, so demote the whole shape and retain both raw atoms.
        conflicts.append("generic_single_conflicts_with_precise_numeric_shape")
    if display_precision is not None and (
        value_type in {"boolean", "identifier"}
        or (
            value_type == "integer"
            and display_precision.get("mode") == "decimal_places"
            and int(display_precision.get("digits") or 0) > 0
        )
    ):
        conflicts.append("display_precision_conflicts_with_value_type")
    if unit is not None and value_type in {"boolean", "identifier"}:
        conflicts.append("unit_conflicts_with_value_type")
    if conflicts:
        preserve_explicit_display = set(conflicts) == {
            "generic_single_conflicts_with_precise_numeric_shape"
        }
        return_field = "unknown"
        mode = "unknown"
        expected_count = None
        container = "unknown"
        value_type = "unknown"
        unit = None
        if not preserve_explicit_display:
            display_precision = None
        cardinal_enforceable = False

    inference_basis = {
        "terminal_operation": [atom["atom_id"] for atom in terminal_basis],
        "cardinality": [atom["atom_id"] for atom in cardinal_basis],
        "target": [candidate["atom_id"] for candidate in target.get("candidates", [])],
        "unit": [atom["atom_id"] for atom in unit_atoms],
        "display_precision": [atom["atom_id"] for atom in display_candidates],
        "per_item": [atom["atom_id"] for atom in per_item_atoms],
        "conflicts": conflicts,
        "enforceable": {
            "return_field": return_field != "unknown",
            "cardinality": cardinal_enforceable,
            "container": container != "unknown",
            "value_type": value_type != "unknown",
            "unit": unit is not None,
            "display_precision": display_precision is not None,
        },
    }
    return {
        "output_id": f"output_advisory_{output_index:03d}",
        "source_operation_ref": terminal_node["operation_id"],
        "return_field": return_field,
        "cardinality": {"mode": mode, "expected_count": expected_count},
        "answer_shape": {
            "container": container,
            "value_type": value_type,
            "unit": unit,
            "precision": "unspecified",
        },
        "display_precision": display_precision,
        "inference_basis": inference_basis,
    }


def _coverage(question: str, recognized: Sequence[dict[str, Any]]) -> dict[str, Any]:
    covered = [False] * len(question)
    for atom in recognized:
        if atom["kind"] == "unresolved":
            continue
        for index in range(atom["start"], atom["end"]):
            if 0 <= index < len(covered):
                covered[index] = True
    meaningful = [
        index
        for index, character in enumerate(question)
        if not character.isspace() and character not in "、。，,.!?！？:：;；()（）[]{}<>「」『』"
    ]
    recognized_count = sum(1 for index in meaningful if covered[index])
    return {
        "status": "advisory_partial",
        "total_codepoints": len(question),
        "meaningful_codepoints": len(meaningful),
        "recognized_codepoints": recognized_count,
        "recognized_ratio": (
            round(recognized_count / len(meaningful), 6) if meaningful else 1.0
        ),
    }


def compile_advisory_intent(
    question_id: str | None,
    question: str,
) -> dict[str, Any]:
    """Compile one question into a deterministic, non-authoritative graph.

    ``question_id`` is retained only for audit correlation.  It is excluded
    from every semantic decision and content-derived identifier.
    """

    if question_id is not None and (
        not isinstance(question_id, str) or not question_id.strip()
    ):
        raise ValueError("question_id must be a non-empty string or None")
    if not isinstance(question, str) or not question.strip():
        raise ValueError("question must be non-empty text")
    if len(question) > MAX_QUESTION_CODEPOINTS:
        raise ValueError("question exceeds the advisory compiler size limit")

    operation_atoms = _scan_operations(question)
    operator_atoms = _scan_operators(question)
    cardinality_atoms = _scan_cardinality(question)
    unit_atoms = _scan_units(question)
    target_atoms = _scan_targets(question)
    precision_atoms = _scan_precision(question)
    sort_atoms = _scan_sort_order(question)
    file_atoms = _scan_files(question)
    scope_atoms, scope_candidates = _scan_scope(question, file_atoms)
    per_item_atoms = _scan_mapping(
        question,
        kind="distribution",
        surfaces=PER_ITEM_SURFACES,
        registry_ref="generic_distribution_surfaces",
    )

    recognized = _dedupe_atoms(
        [
            *operation_atoms,
            *operator_atoms,
            *cardinality_atoms,
            *unit_atoms,
            *target_atoms,
            *precision_atoms,
            *sort_atoms,
            *file_atoms,
            *scope_atoms,
            *per_item_atoms,
        ]
    )
    unresolved = _unresolved_atoms(question, recognized)
    lexical_atoms = _dedupe_atoms([*recognized, *unresolved])

    target = _target_contract(target_atoms)
    scope = _scope_contract(scope_candidates, scope_atoms)
    scope["operator_candidates"] = [
        {
            "operator": atom["canonical"],
            "surface": atom["surface"],
            "atom_id": atom["atom_id"],
            "binding_status": "unbound",
        }
        for atom in operator_atoms
    ]
    operation_graph, terminal, terminal_basis = _operation_graph(
        question,
        operation_atoms,
        cardinality_atoms,
        precision_atoms,
        sort_atoms,
    )
    output_unit_atoms = _output_unit_atoms(question, unit_atoms, cardinality_atoms)
    consumed_refs = {
        input_ref
        for node in operation_graph["nodes"]
        for input_ref in node["input_refs"]
    }
    terminal_nodes = [
        node
        for node in operation_graph["nodes"]
        if node["output_ref"] not in consumed_refs
    ]
    operation_atom_by_id = {atom["atom_id"]: atom for atom in operation_atoms}
    requested_outputs: list[dict[str, Any]] = []
    for output_index, node in enumerate(terminal_nodes):
        node_terminal = str(node["operator"])
        node_basis = [
            operation_atom_by_id[atom_id]
            for atom_id in node["inference_basis"]["atom_ids"]
            if atom_id in operation_atom_by_id
        ]
        if node["operation_id"] == operation_graph["nodes"][-1]["operation_id"]:
            node_basis = list(terminal_basis) or node_basis
        if node_terminal == "list":
            node_cardinality = [
                atom
                for atom in cardinality_atoms
                if atom["canonical"] in {"all", "multiple", "single"}
            ]
        elif node_terminal == "count":
            node_cardinality = [
                atom for atom in cardinality_atoms if atom["canonical"] == "count"
            ]
        elif node_terminal in {
            "calculate",
            "sum",
            "mean",
            "min",
            "max",
            "absolute_distance",
        }:
            node_cardinality = []
        else:
            node_cardinality = list(cardinality_atoms)
        is_final_output = output_index == len(terminal_nodes) - 1
        requested_outputs.append(
            _requested_output(
                output_index,
                node_terminal,
                node,
                node_cardinality,
                target,
                output_unit_atoms if is_final_output else [],
                precision_atoms if is_final_output else [],
                per_item_atoms if is_final_output else [],
                node_basis,
            )
        )
    requested_output = requested_outputs[-1]
    derived_operation = terminal if terminal in (
        set(CALCULATION_OPERATORS) | set(DIRECT_OPERATIONS)
    ) else "unknown"
    intent = {
        "target": target,
        "scope": scope,
        "operation_graph": operation_graph,
        "requested_outputs": requested_outputs,
        "derived_summary": {
            "operation": (
                derived_operation if len(requested_outputs) == 1 else "multi_output"
            ),
            "terminal_operations": [node["operator"] for node in terminal_nodes],
            "return_fields": [
                output["return_field"] for output in requested_outputs
            ],
            "cardinality": (
                requested_output["cardinality"]["mode"]
                if len(requested_outputs) == 1
                else "mixed"
            ),
        },
        "advisory": {
            "status": "candidate_only",
            "authoritative": False,
            "semantic_gaps": [atom["atom_id"] for atom in unresolved],
        },
    }
    identity_core = {
        "compiler": {"name": COMPILER_NAME, "version": COMPILER_VERSION},
        "registry": registry_metadata(),
        "original_question": question,
        "intent": intent,
        "lexical_atoms": lexical_atoms,
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "record_type": "advisory_question_intent",
        "advisory_intent_id": _identifier("qai", identity_core, 32),
        "question_id": question_id,
        "original_question": question,
        "advisory_status": "candidate_only",
        "intent": intent,
        "lexical_atoms": lexical_atoms,
        "unresolved_atoms": unresolved,
        "coverage": _coverage(question, lexical_atoms),
        "registry": registry_metadata(),
        "compiler": {"name": COMPILER_NAME, "version": COMPILER_VERSION},
        "provenance": {
            "deterministic": True,
            "question_only": True,
            "source_data_used": False,
            "answer_data_used": False,
            "prediction_data_used": False,
            "past_answers_used": False,
            "question_id_affects_semantics": False,
        },
    }


__all__ = [
    "COMPILER_NAME",
    "COMPILER_VERSION",
    "SCHEMA_VERSION",
    "compile_advisory_intent",
]
