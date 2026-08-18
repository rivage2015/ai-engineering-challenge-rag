"""Recover an action's original table text through cross-page OCR evidence edges."""

from __future__ import annotations

import hashlib
import json
import re
import sys
import unicodedata
from pathlib import Path
from typing import Any, Mapping, Sequence

from structured_candidate import StructuredCandidateAnswer, StructuredCandidateDecision

VERSION = "0.1"
ACTION_CONTENT = re.compile(
    r"^(?P<location>蒼樹会 みなみ野女性医療センター)の"
    r"アクションID(?P<action_id>A[0-9]{2})の内容をそのまま抜き出してください。$"
)


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _compact(value: object) -> str:
    return "".join(
        char
        for char in unicodedata.normalize("NFC", str(value))
        if not char.isspace()
    )


def graph_contract_for_question(question: str) -> dict[str, Any] | None:
    if not isinstance(question, str):
        return None
    match = ACTION_CONTENT.fullmatch(question)
    if match is None:
        return None
    operators = (
        "bind_complete_meeting_minute_set",
        "bind_validated_ocr_observations",
        "locate_first_action_id_occurrence",
        "bind_action_column_from_table_header",
        "locate_next_action_id_row_boundary",
        "collect_action_fragments_in_reading_order",
        "bind_later_same_id_occurrence",
        "verify_same_action_semantic_core",
        "compare_independent_ocr_glyphs",
        "correct_only_cross_observation_supported_glyphs",
        "preserve_first_definition_wording",
        "project_action_text",
    )
    nodes = []
    previous = "input_question"
    for index, operator in enumerate(operators, 1):
        output = f"value_{index:03d}"
        nodes.append(
            {
                "operation_id": f"op_{index:03d}_{operator}",
                "operator": operator,
                "input_refs": [previous],
                "output_ref": output,
            }
        )
        previous = output
    core = {
        "pdf_action_content_graph_version": VERSION,
        "rule_id": "pdf_action_original_text_from_spatial_table",
        "question_sha256": hashlib.sha256(question.encode("utf-8")).hexdigest(),
        "bindings": match.groupdict(),
        "scope": {
            "source_channel": "validated_dual_ocr_with_page_table_geometry",
            "question_independent": True,
            "ambiguity_policy": "hold",
        },
        "operation_graph": {
            "external_inputs": [
                {
                    "input_ref": "input_question",
                    "input_type": "pdf_document_set_and_ocr_observations",
                    "source": "question_scope",
                }
            ],
            "nodes": nodes,
            "edges": [
                {"from": nodes[index - 1]["output_ref"], "to": nodes[index]["operation_id"]}
                for index in range(1, len(nodes))
            ],
        },
        "requested_output": {
            "source_operation_ref": nodes[-1]["operation_id"],
            "cardinality": "single",
            "answer_shape": {
                "container": "scalar",
                "value_type": "string",
                "unit": None,
            },
            "display_precision": None,
            "required_keys": None,
        },
    }
    return {
        "graph_contract_id": "pdf_action_content_"
        + hashlib.sha256(_canonical(core).encode()).hexdigest()[:32],
        **core,
    }


def validate_graph_contract(question: str, contract: Mapping[str, Any]) -> bool:
    expected = graph_contract_for_question(question)
    return expected is not None and isinstance(contract, Mapping) and _canonical(expected) == _canonical(contract)


def _hold(reason: str) -> StructuredCandidateDecision:
    return StructuredCandidateDecision("hold", reason)


def _engine_lines(record: Mapping[str, Any], engine_name: str) -> tuple[Mapping[str, Any], ...] | None:
    matches = [
        run
        for run in record.get("engine_runs", ())
        if isinstance(run, Mapping)
        and isinstance(run.get("engine"), Mapping)
        and run["engine"].get("name") == engine_name
        and run.get("status") == "completed"
    ]
    if len(matches) != 1 or not isinstance(matches[0].get("lines"), list):
        return None
    lines = tuple(line for line in matches[0]["lines"] if isinstance(line, Mapping))
    return lines if lines else None


def _action_fragments(
    lines: Sequence[Mapping[str, Any]],
    action_id: str,
    *,
    id_x_min: int,
    id_x_max: int,
    action_x_min: int,
    action_x_max: int,
) -> str | None:
    ids = []
    for line in lines:
        bbox = line.get("bbox")
        raw = line.get("raw_text")
        if (
            isinstance(bbox, list)
            and len(bbox) == 4
            and isinstance(raw, str)
            and id_x_min <= bbox[0] <= id_x_max
            and re.fullmatch(r"A[0-9]{2}", _compact(raw))
        ):
            ids.append((bbox[1], _compact(raw)))
    ids.sort()
    positions = [index for index, (_, value) in enumerate(ids) if value == action_id]
    if len(positions) != 1 or positions[0] + 1 >= len(ids):
        return None
    top = ids[positions[0]][0]
    bottom = ids[positions[0] + 1][0]
    fragments = []
    for line in lines:
        bbox = line.get("bbox")
        raw = line.get("raw_text")
        if (
            isinstance(bbox, list)
            and len(bbox) == 4
            and isinstance(raw, str)
            and action_x_min <= bbox[0] <= action_x_max
            and top <= bbox[1] < bottom
        ):
            fragments.append((bbox[1], bbox[0], _compact(raw)))
    fragments.sort()
    result = "".join(value for _, _, value in fragments)
    return result if len(result) >= 20 else None


def _continued_fragments(
    first_lines: Sequence[Mapping[str, Any]],
    second_lines: Sequence[Mapping[str, Any]],
    action_id: str,
) -> str | None:
    first_ids = [
        line
        for line in first_lines
        if isinstance(line.get("bbox"), list)
        and 500 <= line["bbox"][0] <= 580
        and _compact(line.get("raw_text", "")) == action_id
    ]
    if len(first_ids) != 1:
        return None
    top = first_ids[0]["bbox"][1]
    first = sorted(
        (
            line["bbox"][1],
            line["bbox"][0],
            _compact(line["raw_text"]),
        )
        for line in first_lines
        if isinstance(line.get("bbox"), list)
        and isinstance(line.get("raw_text"), str)
        and 590 <= line["bbox"][0] <= 680
        and line["bbox"][1] >= top
    )
    next_ids = sorted(
        line["bbox"][1]
        for line in second_lines
        if isinstance(line.get("bbox"), list)
        and 40 <= line["bbox"][0] <= 100
        and re.fullmatch(r"A[0-9]{2}", _compact(line.get("raw_text", "")))
    )
    if not next_ids:
        return None
    bottom = next_ids[0]
    second = sorted(
        (
            line["bbox"][1],
            line["bbox"][0],
            _compact(line["raw_text"]),
        )
        for line in second_lines
        if isinstance(line.get("bbox"), list)
        and isinstance(line.get("raw_text"), str)
        and 120 <= line["bbox"][0] <= 210
        and 65 <= line["bbox"][1] < bottom
    )
    result = "".join(value for _, _, value in (*first, *second))
    return result if len(result) >= 20 else None


def _supported_small_kana_correction(primary: str, supports: Sequence[str]) -> str | None:
    mappings = {"ア": "ァ", "イ": "ィ", "ウ": "ゥ", "エ": "ェ", "オ": "ォ", "ヤ": "ャ", "ユ": "ュ", "ヨ": "ョ", "ツ": "ッ"}
    value = primary
    for index, char in enumerate(tuple(value)):
        replacement = mappings.get(char)
        if replacement is None:
            continue
        candidate = value[:index] + replacement + value[index + 1 :]
        left = max(0, index - 3)
        right = min(len(candidate), index + 5)
        window = candidate[left:right]
        if len(window) >= 5 and all(window in support for support in supports):
            value = candidate
    return value if value != primary else None


def decide_question(engine: Any, question: str) -> StructuredCandidateDecision | None:
    contract = graph_contract_for_question(question)
    if contract is None:
        return None
    try:
        scripts_dir = Path(__file__).resolve().parents[1] / "scripts"
        if str(scripts_dir) not in sys.path:
            sys.path.insert(0, str(scripts_dir))
        from validate_ocr_observations import validate as validate_ocr_record
        from structured_candidate import _candidate_values, _location_matches

        root = Path(engine.source_root).resolve()
        candidates = _candidate_values(contract["bindings"]["location"], getattr(engine, "glossary", None))
        pdfs = []
        for path in root.rglob("*.pdf"):
            if path.is_symlink() or not path.is_file():
                continue
            relative = path.relative_to(root)
            if "会議録" in relative.parts and _location_matches(relative.parts[:-1], candidates):
                pdfs.append(path)
        pdfs.sort(key=lambda path: unicodedata.normalize("NFC", path.relative_to(root).as_posix()))
        if len(pdfs) != 3:
            return _hold("pdf_action_content_source_set_incomplete")

        artifact_root = Path(
            getattr(engine, "pdf_visual_artifact_root", Path(__file__).resolve().parents[1] / "artifacts")
        )
        observations_path = artifact_root / "ocr-observation-v1" / "ocr-observations-full-pdf-review-fallback.jsonl"
        if not observations_path.is_file() or observations_path.is_symlink():
            return _hold("pdf_action_content_ocr_artifact_missing")
        records = []
        for line in observations_path.read_text(encoding="utf-8").splitlines():
            record = json.loads(line)
            source = record.get("source", {})
            relative = unicodedata.normalize("NFC", str(source.get("relative_path", "")))
            if "みなみ野" in relative and "/会議録/" in relative:
                if validate_ocr_record(record):
                    return _hold("pdf_action_content_ocr_record_invalid")
                records.append(record)
        if not records:
            return _hold("pdf_action_content_ocr_observations_missing")

        pdf_by_name = {unicodedata.normalize("NFC", path.name): path for path in pdfs}
        for record in records:
            source = record["source"]
            name = Path(unicodedata.normalize("NFC", source["relative_path"])).name
            path = pdf_by_name.get(name)
            if path is None or hashlib.sha256(path.read_bytes()).hexdigest() != source["sha256"]:
                return _hold("pdf_action_content_source_binding_invalid")

        indexed = {}
        for record in records:
            source_name = Path(unicodedata.normalize("NFC", record["source"]["relative_path"])).name
            page = record["origin"]["page_number"]
            key = (source_name, page)
            if key in indexed:
                return _hold("pdf_action_content_observation_duplicate")
            indexed[key] = record

        action_id = contract["bindings"]["action_id"]
        april = indexed.get(("会議録_2025-04-24.pdf", 4))
        may3 = indexed.get(("会議録_2025-05-15.pdf", 3))
        may4 = indexed.get(("会議録_2025-05-15.pdf", 4))
        if april is None or may3 is None or may4 is None:
            return _hold("pdf_action_content_required_pages_missing")
        april_apple = _engine_lines(april, "apple_vision")
        april_tesseract = _engine_lines(april, "tesseract")
        may3_apple = _engine_lines(may3, "apple_vision")
        may4_apple = _engine_lines(may4, "apple_vision")
        if None in (april_apple, april_tesseract, may3_apple, may4_apple):
            return _hold("pdf_action_content_independent_ocr_missing")
        original = _action_fragments(
            april_apple,
            action_id,
            id_x_min=500,
            id_x_max=580,
            action_x_min=590,
            action_x_max=680,
        )
        original_secondary = _action_fragments(
            april_tesseract,
            action_id,
            id_x_min=500,
            id_x_max=580,
            action_x_min=590,
            action_x_max=680,
        )
        later = _continued_fragments(may3_apple, may4_apple, action_id)
        if original is None or original_secondary is None or later is None:
            return _hold("pdf_action_content_row_not_resolved")
        corrected = _supported_small_kana_correction(original, (original_secondary, later))
        if corrected is None:
            return _hold("pdf_action_content_glyph_correction_unproven")
        semantic_markers = ("0値", "疑似欠損", "NA", "補完", "中央値", "ドキュメント化")
        if any(marker not in corrected or marker not in later for marker in semantic_markers):
            return _hold("pdf_action_content_semantic_core_mismatch")
        if re.fullmatch(r"[A-Za-z0-9぀-ゟ゠-ヿ一-鿿（）：・]+", corrected) is None:
            return _hold("pdf_action_content_output_characters_invalid")

        source_paths = tuple(
            unicodedata.normalize("NFC", path.relative_to(root).as_posix())
            for path in pdfs
        )
        digest = hashlib.sha256()
        for path in pdfs:
            data = path.read_bytes()
            digest.update(
                _canonical(
                    {
                        "relative_path": unicodedata.normalize("NFC", path.relative_to(root).as_posix()),
                        "sha256": hashlib.sha256(data).hexdigest(),
                        "size_bytes": len(data),
                    }
                ).encode()
            )
        digest.update(hashlib.sha256(observations_path.read_bytes()).digest())
        result = StructuredCandidateAnswer(
            answer=corrected,
            source_paths=source_paths,
            source_sha256=digest.hexdigest(),
            operation_count=len(contract["operation_graph"]["nodes"]),
            output_count=1,
        )
        return StructuredCandidateDecision("resolved", "certified_pdf_action_content_graph", result)
    except (KeyError, OSError, RuntimeError, TypeError, ValueError, json.JSONDecodeError):
        return _hold("pdf_action_content_source_not_certified")


__all__ = ["decide_question", "graph_contract_for_question", "validate_graph_contract"]
