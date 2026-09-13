"""Pure observed-step candidates, NOT a complete or authorized workflow graph.

No runtime caller yet. Caller must bind scope and eligibility to validated sources
before integration. Source order alone does not establish business chronology.
"""
import re
import unicodedata

MAX_RECORDS = 10000
MAX_TEXT_CHARACTERS = 1000000
MAX_ROW = 1048576
CELL = re.compile(r'([A-Za-z]{1,3})([1-9][0-9]{0,6})\Z')
HEADING = re.compile(r'^(?:[^:\n]+:\s*)?([0-9]+)\s*[.、)]\s*(\S.*)$', re.DOTALL)


def _hold(reason):
    return {'status': 'hold', 'reason': reason,
            'coverage': 'unknown', 'order_kind': 'source_order',
            'steps': [], 'selected_evidence_ids': []}


def _text(value):
    return isinstance(value, str) and bool(value.strip())


def _cell(value):
    match = CELL.fullmatch(value) if isinstance(value, str) else None
    if not match:
        raise ValueError('invalid_cell_locator')
    column = match[1].upper()
    number = 0
    for char in column:
        number = number * 26 + ord(char) - ord('A') + 1
    row = int(match[2])
    if number > 16384 or row > MAX_ROW:
        raise ValueError('invalid_cell_locator')
    return column, row


def select_observed_steps(records, *, document_id, relative_path, sheet_name,
                          start_row, end_row, content_header_id, eligible_evidence_ids):
    """Return all observed paragraphs of explicit numbered steps in one scope.

The content header's role and section bounds are supplied by the caller, not
inferred here. Conditional sentences remain raw quotations, not control edges.
"""
    if any(not _text(value) for value in (document_id, relative_path, sheet_name, content_header_id)):
        return _hold('invalid_scope')
    if (type(start_row) is not int or type(end_row) is not int
            or not 1 <= start_row <= end_row <= MAX_ROW):
        return _hold('invalid_row_bounds')
    if (not isinstance(eligible_evidence_ids, (set, frozenset))
            or len(eligible_evidence_ids) > MAX_RECORDS
            or any(not _text(eid) for eid in eligible_evidence_ids)):
        return _hold('invalid_eligibility_input')
    if type(records) is not list or len(records) > MAX_RECORDS:
        return _hold('invalid_record_count_or_type')
    ids = set()
    target = []
    total_characters = 0
    for record in records:
        if (type(record) is not dict or not _text(record.get('evidence_id'))
                or not _text(record.get('document_id')) or not _text(record.get('relative_path'))
                or type(record.get('locator')) is not dict or not isinstance(record.get('text'), str)):
            return _hold('invalid_record')
        eid = record['evidence_id']
        if eid in ids:
            return _hold('duplicate_evidence_id')
        ids.add(eid)
        total_characters += len(record['text'])
        if total_characters > MAX_TEXT_CHARACTERS:
            return _hold('text_character_budget_exceeded')
        if (record['document_id'], record['relative_path'], record['locator'].get('sheet_name')) == (
                document_id, relative_path, sheet_name):
            target.append(record)

    cells, rows = {}, {}
    try:
        for record in target:
            locator = record['locator']
            if 'row_index' in locator and (type(locator['row_index']) is not int
                    or not 1 <= locator['row_index'] <= MAX_ROW):
                return _hold('invalid_row_locator')
            if 'cell' in locator:
                position = _cell(locator['cell'])
                if 'row_index' in locator and locator['row_index'] != position[1]:
                    return _hold('inconsistent_cell_row_locator')
                if position in cells:
                    return _hold('duplicate_cell_locator')
                cells[position] = record
            elif 'row_index' in locator:
                row = locator['row_index']
                if row in rows:
                    return _hold('duplicate_row_locator')
                rows[row] = record
    except ValueError as exc:
        return _hold(str(exc))

    headers = [(position, record) for position, record in cells.items()
               if record['evidence_id'] == content_header_id]
    if len(headers) != 1 or not _text(headers[0][1]['text']) or headers[0][0][1] >= start_row:
        return _hold('invalid_content_header')
    column = headers[0][0][0]
    headings = []
    for row, record in sorted(rows.items()):
        if not start_row <= row <= end_row:
            continue
        match = HEADING.fullmatch(unicodedata.normalize('NFKC', record['text']).strip())
        if match:
            # Bound digits before int(), including adversarial long numeric text.
            if len(match[1]) > 3:
                return _hold('invalid_step_numbering')
            headings.append((row, int(match[1]), record))
    if not headings:
        return _hold('numbered_steps_not_found')
    if len(headings) > 100 or [h[1] for h in headings] != list(range(1, len(headings) + 1)):
        return _hold('invalid_step_numbering')

    selected = [content_header_id]
    steps = []
    for index, (row, ordinal, record) in enumerate(headings):
        stop = headings[index + 1][0] if index + 1 < len(headings) else end_row + 1
        paragraphs = [{'evidence_id': body['evidence_id'], 'cell': f'{col}{body_row}', 'text': body['text']}
                      for (col, body_row), body in sorted(cells.items(), key=lambda item: item[0][1])
                      if col == column and row <= body_row < stop and _text(body['text'])]
        if not paragraphs:
            return _hold('step_body_missing')
        selected.append(record['evidence_id'])
        selected.extend(p['evidence_id'] for p in paragraphs)
        steps.append({'ordinal': ordinal, 'heading_row': row,
                      'heading_evidence_id': record['evidence_id'], 'heading_text': record['text'],
                      'paragraphs': paragraphs})
    if not set(selected) <= eligible_evidence_ids:
        return _hold('selected_evidence_not_eligible')
    try:
        input_sha256 = _selection_input_hash(records, {
            'document_id': document_id, 'relative_path': relative_path,
            'sheet_name': sheet_name, 'start_row': start_row, 'end_row': end_row,
            'content_header_id': content_header_id,
            'eligible_evidence_ids': sorted(eligible_evidence_ids)})
    except (TypeError, ValueError, RecursionError, OverflowError):
        return _hold('source_input_not_bounded_json')
    return {'status': 'candidate', 'reason': 'observed_numbered_steps',
            'coverage': 'unknown', 'order_kind': 'source_order',
            'source_input_sha256': input_sha256,
            'header_evidence_id': content_header_id,
            'steps': steps, 'selected_evidence_ids': selected}


def _candidate_json(value):
    """Bound plain JSON structure and compare numeric types without coercion."""
    import json

    pending = [(value, 0)]
    count = 0
    while pending:
        item, depth = pending.pop()
        count += 1
        if depth > 32 or count > 100000:
            raise ValueError('candidate_structure_budget')
        if type(item) is dict:
            if len(item) > 100000 or any(type(key) is not str for key in item):
                raise ValueError('candidate_keys')
            pending.extend((child, depth + 1) for child in item.values())
        elif type(item) is list:
            if len(item) > 100000:
                raise ValueError('candidate_structure_budget')
            pending.extend((child, depth + 1) for child in item)
        elif type(item) not in (str, int, float, bool, type(None)):
            raise ValueError('candidate_non_json_type')
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True,
                         separators=(',', ':'), allow_nan=False)
    if len(encoded) > 4000000:
        raise ValueError('candidate_character_budget')
    return encoded


def _selection_input_hash(records, scope):
    import hashlib

    # Record enumeration order is not source order; IDs are already unique.
    encoded = _candidate_json({'binding_version': 1, 'scope': scope,
        'records': sorted(records, key=lambda record: record['evidence_id'])})
    return hashlib.sha256(encoded.encode('utf-8')).hexdigest()


def validate_observed_steps(candidate, records, **scope):
    """Reconstruct from external source inputs, not the candidate's claims.

    A pass means exact reconstruction only. Source authority, Graph binding,
    generation and business completeness remain the integrating caller's duty.
    """
    result = {'status': 'fail', 'reason': 'candidate_invalid', 'coverage': 'unknown'}
    try:
        if type(candidate) is not dict:
            return result
        supplied = _candidate_json(candidate)
        expected = select_observed_steps(records, **scope)
        if expected['status'] != 'candidate':
            return {**result, 'reason': 'source_selection_unavailable'}
        if supplied != _candidate_json(expected):
            return {**result, 'reason': 'selection_reconstruction_mismatch'}
    except (TypeError, ValueError, RecursionError, OverflowError):
        return result
    return {'status': 'pass', 'reason': 'selection_reconstruction_match', 'coverage': 'unknown'}
