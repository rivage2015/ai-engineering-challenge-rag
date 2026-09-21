"""Bounded, provenance-bound worksheet reading; never a business-order graph.

The caller must first select the permitted effective document editions and
provide their matching validated evidence graph. This module neither resolves
versions nor calls a model. Section roles are lexical reading aids, not claims
that a task is complete or that neighboring rows imply a workflow edge.
"""
from __future__ import annotations

import copy
import importlib.util
import json
import re
import unicodedata
from pathlib import Path


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(filename))
    if spec is None or spec.loader is None:
        raise ImportError(filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_graph = _load("workflow_reading_source_graph", "question_evidence_graph.py")
_base = _load("workflow_reading_base", "answer_local_memory.py")
WORKFLOW_QUERY = re.compile(r"流れ|業務フロー|ワークフロー|手順|一連の対応|workflow|procedure", re.I)
_SUFFIX = re.compile(r"(?:スクリプト|スク|マニュアル|手順書|業務手順|業務フロー|手順|workflow|procedure)\s*$", re.I)
_NUMBERED = re.compile(r"^(?:第\s*\d{1,3}\s*[章節]|\d{1,3}\s*[.．、)）]|[①-⑳])\s*\S")
_COLUMN = re.compile(r"^(?:担当|担当者|内容|手順|セリフ|発話|参考(?:欄)?|注意|備考|条件|スクリプト(?:他|例)?|項目|シーン)$")
_ROLES = (
    ("preparation", re.compile(r"^(?:事前準備|準備|開始前|開始時|業務開始|作業開始|ログイン|接続)(?:$|時|前|後|の|について|[（(：:])")),
    ("ending", re.compile(r"^(?:終了|業務終了|作業終了|終了後|ログアウト|切断|後片付け)(?:$|時|前|後|の|について|[（(：:])")),
    ("notes", re.compile(r"^(?:注意事項|注意点|注意|例外|補足|備考|トラブル対応)(?:$|時|の|について|[（(：:])")),
    ("main", re.compile(r"^(?:基本手順|作業手順|対応手順|仕事の手順|実施手順|業務内容|主作業|作業中|実施|スクリプト)(?:$|時|中|の|について|[（(：:])")),
)
_REFERENCE = re.compile(r"(?:操作キー|ショートカット|機能一覧|ボタン一覧|モーション|操作パネル)(?:\s*[：:]?\s*)$")


def _raw(record):
    return _graph._decode_json_string_literal(record["text"])


def _normal(value):
    return _graph.normalize(unicodedata.normalize("NFKC", value))


def _surface(value):
    value = re.sub(r"\s*[（(](?:英語|日本語|english|japanese)[)）]\s*$", "", value, flags=re.I)
    return _SUFFIX.sub("", value).strip()


def _language(value):
    if re.search(r"英語|english", value, re.I):
        return "en"
    if re.search(r"日本語|japanese", value, re.I):
        return "ja"
    return None


def _position(record):
    locator = record["locator"]
    if "row_index" in locator and (type(locator["row_index"]) is not int
            or not 1 <= locator["row_index"] <= 1048576):
        raise ValueError("workflow_row_locator_invalid")
    if "cell" in locator:
        pos = _graph._spreadsheet_cell_position(locator)
        if pos is None:
            raise ValueError("workflow_cell_locator_invalid")
        column, row = pos
        number = 0
        for char in column:
            number = number * 26 + ord(char) - ord("A") + 1
        if not 1 <= number <= 16384 or not 1 <= row <= 1048576:
            raise ValueError("workflow_cell_locator_invalid")
        if "row_index" in locator and locator["row_index"] != row:
            raise ValueError("workflow_cell_row_mismatch")
        return row, number
    if "row_index" in locator:
        return locator["row_index"], 0
    return None


def _values(record):
    """Inspect row labels without changing any emitted raw evidence."""
    value = _raw(record).strip()
    if "cell" in record["locator"]:
        return [value] if value else []
    return [re.sub(r"^[^:\n：]{1,80}[:：]\s*", "", line).strip()
            for line in value.splitlines() if line.strip()]


def _heading(values, query_surface):
    if not values:
        return None
    text = unicodedata.normalize("NFKC", values[0]).strip()
    if not text or len(text) > 64 or re.search(r"[。！？!?、,]", text):
        # A numbered Japanese heading may itself use the enumeration comma.
        if not _NUMBERED.match(text) or len(text) > 64 or re.search(r"[。！？!?]", text):
            return None
    if "\n" in text and not _NUMBERED.match(text):
        return None
    boxed = text.startswith(("【", "[", "■", "●", "◆", "#"))
    text = text.strip("【】[]■●◆# ")
    surface = _normal(_surface(text))
    for role, pattern in _ROLES:
        if pattern.search(text):
            return role, values[0]
    if _SUFFIX.search(text) and len(surface) >= 2 and surface in query_surface:
        return "scope", values[0]
    if _NUMBERED.match(text):
        return "main", values[0]
    if _REFERENCE.search(text):
        return "reference", values[0]
    notes = re.fullmatch(r"(.*?)(?:ルール|注意事項|注意点)", text)
    if notes:
        prefix = _normal(notes[1].strip("：: "))
        return ("notes" if not prefix or prefix in query_surface else "other"), values[0]
    if _SUFFIX.search(text) and len(surface) >= 2:
        return "other", values[0]
    if boxed:
        return "other", values[0]
    return None


def _unsafe(record):
    value = _raw(record)
    if not value.strip():
        return "empty_text"
    if "[暫定読取]" in value:
        return "provisional_text"
    if _graph.SENSITIVE_VALUE_SURFACE.search(value + "\n" + "\n".join(_values(record))):
        return "sensitive_value"
    if any(pattern.search(value) for pattern in _base.INSTRUCTION_LIKE_PATTERNS):
        return "instruction_like_text"
    if record.get("retrieval_eligible") is False or record.get("status", "observed") not in {"observed", "verified"}:
        return "ineligible_record"
    return None


def _requested_role(query):
    """Honor an explicit 'only preparation/ending/notes' scope, not a guess."""
    roles = []
    for role, surface in (("preparation", r"準備|開始前|ログイン"),
                          ("ending", r"終了後|ログアウト|後片付け"),
                          ("notes", r"注意事項|注意点|例外")):
        if re.search(r"(?:" + surface + r")(?:の)?(?:手順|作業|対応)?(?:だけ|のみ)", query):
            roles.append(role)
    return roles


def _row_cell_sources(row, cells, records_by_id, traversal, table):
    """Validate a lossless row-to-originals projection, without parsing labels.

    The trusted graph is supplied by the caller's index validator. We still
    require each explicit lineage binding, same source scope, all body cells,
    and byte-for-byte forward reconstruction. A preceding cell is only an
    uncertain column-label candidate, never an actor or a condition assertion.
    """
    edges = [edge for edge in traversal["edge_by_id"].values()
             if edge.get("from_node_id") == row["evidence_id"]
             and edge.get("relation_type") == "derived_from"]
    if not edges:
        return (None, None) if not cells else (None, "workflow_row_lineage_missing")
    sources, relation_ids = {}, []
    row_number = _position(row)[0]
    for edge in edges:
        if (edge.get("status") != "verified" or edge.get("basis_kind") != "explicit"
                or edge.get("relation_class") != "lineage"):
            return None, "workflow_row_lineage_invalid"
        eid = edge.get("to_node_id")
        source = records_by_id.get(eid)
        if source is None or eid in sources:
            return None, "workflow_row_source_missing_or_duplicate"
        if (source["document_id"] != row["document_id"]
                or source["relative_path"] != row["relative_path"]
                or source["locator"].get("sheet_name") != row["locator"].get("sheet_name")):
            return None, "workflow_row_source_scope_mismatch"
        position = _position(source)
        if (position is None or not position[1] or position[0] > row_number
                or any(item.get("from_node_id") == eid and item.get("relation_type") == "derived_from"
                       for item in traversal["edge_by_id"].values())):
            return None, "workflow_row_source_not_original_cell"
        if eid not in traversal["paths"]:
            return None, "workflow_source_path_missing"
        if traversal["node_by_id"][eid].get("status") not in {"observed", "verified"}:
            return None, "workflow_source_not_eligible"
        problem = _unsafe(source)
        if problem:
            return {"excluded_evidence": [{"evidence_id": eid, "reason": problem}]}, "workflow_source_incomplete_or_unsafe"
        sources[eid] = source
        relation_ids.append(edge["relation_id"])
    body = sorted((source for source in sources.values() if _position(source)[0] == row_number),
                  key=_position)
    headers = sorted((source for source in sources.values() if _position(source)[0] < row_number),
                     key=_position)
    if not body or {r["evidence_id"] for r in body} != {r["evidence_id"] for r in cells}:
        return None, "workflow_row_cell_coverage_incomplete"
    if headers:
        header_rows = {_position(source)[0] for source in headers}
        if len(header_rows) != 1:
            return None, "workflow_row_header_scope_ambiguous"
        expected_headers = {r["evidence_id"] for r in table
                            if "cell" in r["locator"] and _position(r)[0] in header_rows
                            and _raw(r)}
        if expected_headers != {r["evidence_id"] for r in headers}:
            return None, "workflow_row_header_coverage_incomplete"
    by_column = {_position(source)[1]: source for source in headers}
    expected_lines, normalized_lines = [], []
    header_bindings = {}
    for source in body:
        column = _position(source)[1]
        header = by_column.get(column)
        label = _raw(header) if header is not None else _graph._spreadsheet_cell_position(source["locator"])[0]
        expected_lines.append(f"{label}: {_raw(source)}")
        # The existing SearchUnit builder's display_value() strips cell
        # boundaries before joining. Reproduce that known projection solely
        # for comparison; emitted originals keep all whitespace unchanged.
        normalized_lines.append(f"{label.strip()}: {_raw(source).strip()}")
        if header is not None:
            header_bindings[source["evidence_id"]] = [header["evidence_id"]]
    if _raw(row) not in {"\n".join(expected_lines), "\n".join(normalized_lines)}:
        # Formula/cache annotations, container headings, inner whitespace, or
        # any otherwise unrepresented information are not silently discarded.
        return None, "workflow_row_originals_not_lossless"
    return {"body": body, "headers": headers, "header_bindings": header_bindings,
            "relation_ids": sorted(relation_ids)}, None


def collect_workflow(query, records, source_graph, *, max_chars=12000, max_records=80, allowed_paths=None):
    """Return ``packets`` and a bounded selection ``trace``.

    ``allowed_paths`` can restrict candidate selection to caller-confirmed
    editions without changing the full evidence universe used for graph
    validation. It must not be computed only from top-k content hits.

    A named worksheet or explicit manual title establishes the subject. Explicit
    preparation/main/ending/notes headings establish chapter boundaries. A
    different manual/boxed subject closes the current scope. No section is
    selected merely because it follows the first numbered step. All columns in
    selected rows are retained as original cells when their complete lineage
    and exact row reconstruction are validated. Genuine row-only evidence is
    retained unchanged. Budget or unsafe/missing evidence blocks the bundle
    rather than silently returning a partial workflow.
    """
    trace = {"status": "not_applicable", "reason": "not_workflow_question",
             "used": False, "coverage": "unknown", "order_kind": "source_order",
             "source_order_only": True, "selected_evidence_ids": [],
             "omitted_evidence_ids": [], "excluded_evidence": [],
             "sections": [], "cell_coverage": {}, "packet_characters": 0,
             "row_cell_decomposition": {}, "header_candidate_evidence_ids": []}

    def finish(status, reason):
        trace.update(status=status, reason=reason)
        if status != "ready":
            trace["omitted_evidence_ids"] = list(trace["selected_evidence_ids"])
        return {"packets": [], "trace": trace}

    if not isinstance(query, str) or not WORKFLOW_QUERY.search(query):
        return finish("not_applicable", "not_workflow_question")
    if (type(max_chars) is not int or max_chars < 1
            or type(max_records) is not int or max_records < 1):
        return finish("blocked", "workflow_budget_invalid")
    if not isinstance(records, list) or len(records) > 100000:
        return finish("blocked", "workflow_records_invalid")
    if allowed_paths is not None and (not isinstance(allowed_paths, (set, frozenset, list, tuple))
            or any(not isinstance(path, str) or not path for path in allowed_paths)):
        return finish("blocked", "workflow_allowed_paths_invalid")
    allowed_paths = None if allowed_paths is None else set(allowed_paths)
    trace["version_scope_filter_applied"] = allowed_paths is not None
    ids = set()
    tables = {}
    for record in records:
        if (not isinstance(record, dict) or not isinstance(record.get("locator"), dict)
                or any(not isinstance(record.get(key), str) or not record[key]
                       for key in ("evidence_id", "document_id", "relative_path"))
                or not isinstance(record.get("text"), str)):
            return finish("blocked", "workflow_record_invalid")
        if record["evidence_id"] in ids:
            return finish("blocked", "workflow_duplicate_evidence_id")
        ids.add(record["evidence_id"])
        if allowed_paths is not None and record["relative_path"] not in allowed_paths:
            continue
        sheet = record["locator"].get("sheet_name")
        if isinstance(sheet, str) and sheet.strip():
            tables.setdefault((record["document_id"], record["relative_path"], sheet), []).append(record)
    query_surface = _normal(query)
    candidates = []
    for key, table in sorted(tables.items()):
        surface = _normal(_surface(key[2]))
        sheet_match = len(surface) >= 2 and surface in query_surface
        title_matches = []
        for record in table:
            values = _values(record)
            if (len(values) == 1 and len(values[0]) <= 64 and _SUFFIX.search(values[0])
                    and len(_normal(_surface(values[0]))) >= 2
                    and _normal(_surface(values[0])) in query_surface):
                title_matches.append(record["evidence_id"])
        if sheet_match or title_matches:
            candidates.append((key, table, sheet_match, title_matches))
    requested_language = _language(query)
    if requested_language:
        explicit = [entry for entry in candidates if _language(entry[0][2]) == requested_language]
        candidates = explicit or [entry for entry in candidates if _language(entry[0][2]) is None]
    elif any(_language(entry[0][2]) is None for entry in candidates):
        # A named original and its explicitly labelled translation are not
        # interchangeable scopes. Prefer the unqualified title actually asked
        # for; keep the language decision visible, not a global English ban.
        untranslated = [entry for entry in candidates if _language(entry[0][2]) is None]
        trace["language_selection"] = "unqualified_subject_title"
        trace["language_alternatives"] = [entry[0][2] for entry in candidates if _language(entry[0][2])]
        candidates = untranslated
    if any(entry[2] for entry in candidates):
        trace["scope_selection"] = "explicit_subject_sheet_before_title_mentions"
        trace["title_mention_alternatives"] = [entry[0][2] for entry in candidates if not entry[2]]
        candidates = [entry for entry in candidates if entry[2]]
    trace["candidate_scopes"] = [dict(zip(("document_id", "relative_path", "sheet_name"), entry[0]))
                                 for entry in candidates]
    if not candidates:
        return finish("not_applicable", "workflow_section_not_found")
    if len(candidates) != 1:
        return finish("needs_confirmation", "workflow_scope_ambiguous")
    key, table, sheet_match, title_matches = candidates[0]
    trace["scope"] = dict(zip(("document_id", "relative_path", "sheet_name"), key))
    traversal, error = _graph._prepare_stored_graph_traversal(source_graph, records)
    if error:
        trace["graph_error"] = error.get("code")
        return finish("blocked", "workflow_source_graph_invalid")
    rows = {}
    positions = set()
    try:
        for record in table:
            position = _position(record)
            if position is None:
                continue  # A sheet metadata object is not workflow text.
            if position in positions:
                return finish("blocked", "workflow_duplicate_locator")
            positions.add(position)
            rows.setdefault(position[0], []).append((position[1], record))
    except ValueError as exc:
        return finish("blocked", str(exc))
    if not rows:
        return finish("needs_confirmation", "workflow_chapter_structure_missing")
    requested_roles = _requested_role(query)
    if len(requested_roles) > 1:
        return finish("needs_confirmation", "workflow_requested_sections_ambiguous")
    trace["requested_section_roles"] = requested_roles
    headings, context_rows = [], []
    active = bool(sheet_match)
    in_reference = False
    for number, entries in sorted(rows.items()):
        cells = [record for column, record in sorted(entries) if column]
        row_records = [record for column, record in entries if not column]
        values = [value for record in (cells or row_records) for value in _values(record)]
        if values and all(_COLUMN.fullmatch(value) for value in values):
            if active:
                context_rows.append(number)
            continue
        heading = _heading(values, query_surface)
        if heading:
            role, text = heading
            if role == "scope":
                active = True
                in_reference = False
                context_rows.append(number)
            elif role == "other":
                active = False
                in_reference = False
            elif role == "reference":
                in_reference = True
            elif not (role == "main" and _NUMBERED.match(unicodedata.normalize("NFKC", text))):
                in_reference = False
            headings.append({"start_row": number, "role": role, "heading": text,
                             "in_scope": active and not in_reference,
                             "heading_evidence_ids": [record["evidence_id"]
                                                      for _, record in sorted(entries)]})
    if sum(heading["role"] == "scope" for heading in headings) > 1:
        return finish("needs_confirmation", "workflow_repeated_scope_heading")
    selected_rows = set()
    sections = []
    last_row = max(rows)
    for index, heading in enumerate(headings):
        end = headings[index + 1]["start_row"] - 1 if index + 1 < len(headings) else last_row
        if not heading["in_scope"] or heading["role"] in {"other", "scope", "reference"}:
            continue
        if requested_roles and heading["role"] not in requested_roles:
            continue
        section = {**heading, "end_row": end}
        section.pop("in_scope")
        sections.append(section)
        selected_rows.update(number for number in rows if heading["start_row"] <= number <= end)
    if not sections:
        return finish("needs_confirmation", "workflow_chapter_structure_missing")
    # A lone short heading cannot establish an entire sheet's business scope.
    if len(sections) == 1 and not requested_roles and not any(h["role"] == "scope" for h in headings):
        return finish("needs_confirmation", "workflow_chapter_structure_ambiguous")
    selected_rows.update(number for number in context_rows
                         if number <= max(selected_rows) and (sheet_match or any(
                             h["role"] == "scope" and h["start_row"] <= number for h in headings)))
    trace["sections"] = sections
    scoped_records = [record for number in sorted(selected_rows) for _, record in sorted(rows[number])]
    trace["candidate_evidence_ids"] = [record["evidence_id"] for record in scoped_records]
    trace["source_locators"] = {record["evidence_id"]: copy.deepcopy(record["locator"])
                                for record in scoped_records}
    trace["selected_evidence_ids"] = list(trace["candidate_evidence_ids"])
    for record in scoped_records:
        eid = record["evidence_id"]
        if eid not in traversal["paths"]:
            return finish("blocked", "workflow_source_path_missing")
        if traversal["node_by_id"][eid].get("status") not in {"observed", "verified"}:
            return finish("blocked", "workflow_source_not_eligible")
        problem = _unsafe(record)
        if problem:
            trace["excluded_evidence"].append({"evidence_id": eid, "reason": problem})
    if trace["excluded_evidence"]:
        return finish("blocked", "workflow_source_incomplete_or_unsafe")
    selected_by_id, header_bindings, borrowed_headers = {}, {}, set()
    records_by_id = {record["evidence_id"]: record for record in records}
    for number in sorted(selected_rows):
        entries = sorted(rows[number])
        row = next((record for column, record in entries if not column), None)
        cells = [record for column, record in entries if column]
        if row is not None:
            decomposition, error = _row_cell_sources(row, cells, records_by_id, traversal, table)
            if error:
                if decomposition:
                    trace["excluded_evidence"].extend(decomposition.get("excluded_evidence", []))
                return finish("blocked", error)
            if decomposition is None:
                selected_by_id[row["evidence_id"]] = row
            else:
                trace["row_cell_decomposition"][row["evidence_id"]] = {
                    "cell_evidence_ids": [r["evidence_id"] for r in decomposition["body"]],
                    "header_candidate_evidence_ids": [r["evidence_id"] for r in decomposition["headers"]],
                    "lineage_relation_ids": decomposition["relation_ids"],
                }
                for header in decomposition["headers"]:
                    eid = header["evidence_id"]
                    selected_by_id[eid] = header
                    borrowed_headers.add(eid)
                    trace["source_locators"][eid] = copy.deepcopy(header["locator"])
                header_bindings.update(decomposition["header_bindings"])
        for cell in cells:
            selected_by_id[cell["evidence_id"]] = cell
            trace["cell_coverage"][cell["evidence_id"]] = cell["evidence_id"]
    selected = sorted(selected_by_id.values(), key=_position)
    trace["header_candidate_evidence_ids"] = [r["evidence_id"] for r in selected
                                               if r["evidence_id"] in borrowed_headers]
    trace["selected_evidence_ids"] = [record["evidence_id"] for record in selected]
    for record in selected:
        eid = record["evidence_id"]
        if eid not in traversal["paths"]:
            return finish("blocked", "workflow_source_path_missing")
        if traversal["node_by_id"][eid].get("status") not in {"observed", "verified"}:
            return finish("blocked", "workflow_source_not_eligible")
    # Preflight size estimate, not the actual model payload: source path/sheet
    # is emitted once rather than per cell. Runtime alias/reading-aid formatting
    # is checked exactly by the caller, with the same 12,000-character cap and
    # delivery of every selected original required before any model call.
    trace["packet_characters"] = len(json.dumps(trace["scope"], ensure_ascii=False)) + 80 + sum(
        len(record["text"]) + len(json.dumps({k: v for k, v in record["locator"].items()
                                            if k != "sheet_name"}, ensure_ascii=False)) + 100
        for record in selected)
    if len(selected) > max_records or trace["packet_characters"] > max_chars:
        return finish("blocked", "workflow_context_outside_budget")
    trace.update(status="ready", reason="bounded_source_sections", used=True,
                 stored_graph_binding=True, selected_row_count=len(selected_rows))
    for section in trace["sections"]:
        section["evidence_ids"] = [record["evidence_id"] for record in selected
                                   if section["start_row"] <= _position(record)[0] <= section["end_row"]]
    packets = [{**copy.deepcopy(record), "score": 1.0, "rerank_score": 1.0,
                "document_support_bonus": 0.0, "semantic_score": 0.0,
                "lexical_score": 0.0, "token_score": 0.0,
                "retrieval_source": "workflow_reading_section"} for record in selected]
    for packet in packets:
        eid = packet["evidence_id"]
        if eid in header_bindings:
            packet["workflow_header_candidate_evidence_ids"] = header_bindings[eid]
        if eid in borrowed_headers:
            packet["workflow_borrowed_header_candidate"] = True
    return {"packets": packets, "trace": trace}
