"""Bind workflow candidates to the existing validated index read path.

Application-owned scope and revision reader are trust inputs, never model input.
This API is not yet wired into answer generation or the UI.
"""
import hashlib
import importlib.util
import json
from pathlib import Path
import re


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


base = _load('workflow_index_base', 'answer_local_memory.py')
graph = _load('workflow_index_graph', 'question_evidence_graph.py')
steps = _load('workflow_index_steps', 'workflow_step_selection.py')


def _hold(reason):
    return {'status':'hold', 'reason':reason, 'coverage':'unknown'}


def _current(reader, expected):
    current, _, revision = reader()
    return current is True and steps._candidate_json(revision) == steps._candidate_json(expected)


def _potential_document_evidence(source_graph, document_id):
    # Conservative hold detection, not proof of ownership: include non-verified
    # structural links too so a held node is not silently erased from a workflow.
    transitions = {}
    for edge in source_graph['edges']:
        if edge.get('relation_type') == 'contains' and edge.get('relation_class') == 'structural':
            source, target = edge['from_node_id'], edge['to_node_id']
        elif edge.get('relation_type') == 'derived_from' and edge.get('relation_class') == 'lineage':
            source, target = edge['to_node_id'], edge['from_node_id']
        else:
            continue
        transitions.setdefault(source,set()).add(target)
    seen, pending = {document_id}, [document_id]
    while pending:
        for target in transitions.get(pending.pop(),set()):
            if target not in seen:
                seen.add(target)
                pending.append(target)
    return {n['node_id'] for n in source_graph['nodes']
            if n['node_type']=='evidence' and n['node_id'] in seen}


def load_bound_workflow(index_path, *, scope, expected_revision, read_revision):
    """Load and bind from a checked snapshot; never accept caller Evidence."""
    try:
        scope = json.loads(steps._candidate_json(scope))
        expected_revision = json.loads(steps._candidate_json(expected_revision))
        revision_fields = {'generation','generation_path','decision_snapshot_sha256','config_sha256'}
        if (type(expected_revision) is not dict or set(expected_revision)!=revision_fields
                or any(type(v) is not str for v in expected_revision.values())
                or not re.fullmatch(r'generation-[0-9a-f]{32}',expected_revision['generation'])
                or any(not re.fullmatch(r'[0-9a-f]{64}',expected_revision[k])
                       for k in ['decision_snapshot_sha256','config_sha256'])):
            return _hold('invalid_expected_revision')
        if type(scope) is not dict or set(scope)!={'document_id','relative_path','sheet_name',
                'start_row','end_row','content_header_id'}:
            return _hold('invalid_scope_fields')
        generation = Path(expected_revision['generation_path'])
        if not generation.is_absolute() or generation.name != expected_revision['generation']:
            return _hold('invalid_generation_path')
        index = Path(index_path).resolve()
        if not index.is_relative_to(generation.resolve()) or index == generation.resolve():
            return _hold('index_outside_expected_generation')
        if not _current(read_revision,expected_revision):
            return _hold('revision_changed_before_read')
        records, policy = base.load_answer_evidence_records(index)
        source_graph = policy['source_graph']
        eligible = policy['eligible_evidence_ids']
        if (type(records) is not list or len(records)>steps.MAX_RECORDS
                or not isinstance(eligible,(set,frozenset))
                or {r['evidence_id'] for r in records} != set(eligible)):
            return _hold('eligible_record_mismatch')
        for record in records:
            if hashlib.sha256(record['text'].encode('utf-8')).hexdigest()!=record['observed_sha256']:
                return _hold('observed_text_hash_mismatch')
        traversal, error = graph._prepare_stored_graph_traversal(source_graph,records)
        if error:
            return _hold('stored_graph_traversal_invalid')
        if _potential_document_evidence(source_graph,scope['document_id']) - set(eligible):
            return _hold('document_has_ineligible_evidence')
        selection = steps.select_observed_steps(records,**scope,eligible_evidence_ids=eligible)
        if selection['status']!='candidate':
            return _hold('workflow_selection_unavailable')
        selected = selection['selected_evidence_ids']
        if any(eid not in traversal['paths'] for eid in selected):
            return _hold('required_document_path_missing')
        stored_binding = graph._stored_graph_binding(traversal,selected)
        if not _current(read_revision,expected_revision):
            return _hold('revision_changed_after_read')
        # Copy trust inputs so later caller mutation cannot change this result.
        return json.loads(steps._candidate_json({'status':'candidate','coverage':'unknown',
            'scope':scope,'revision':expected_revision,'index_path':str(index),
            'selection':selection,'stored_graph_binding':stored_binding}))
    except (OSError, ValueError, TypeError, KeyError, AttributeError, RecursionError,
            base.sqlite3.Error):
        return _hold('validated_workflow_load_failed')


def validate_bound_workflow(candidate, index_path, *, scope, expected_revision, read_revision):
    rebuilt = load_bound_workflow(index_path,scope=scope,
        expected_revision=expected_revision,read_revision=read_revision)
    try:
        matched = (rebuilt['status']=='candidate'
            and steps._candidate_json(candidate)==steps._candidate_json(rebuilt))
    except (ValueError,TypeError,RecursionError,OverflowError):
        matched = False
    return {'status':'pass' if matched else 'fail',
        'reason':'bound_workflow_matches' if matched else 'bound_workflow_mismatch',
        'coverage':'unknown'}
