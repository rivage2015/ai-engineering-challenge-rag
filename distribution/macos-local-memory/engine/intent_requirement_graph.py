"""Compile a reviewed request without reinterpreting or dropping its requirements.

This is a request graph, not a semantic graph extracted from source documents.
Its edges describe the explicit structure of the reviewed contract only.  It
does not infer an actor, dates, domain concepts, or evidence relationships.
The app remains responsible for signature, expiry, and live revision checks.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata


SCHEMA_VERSION = "1.0-intent-requirements"
ANSWER_SHAPE = "承認済みの各要求に原文根拠に基づいて回答し、未確認の要求は区別する。"


def _canonical(value: object) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError, RecursionError) as exc:
        raise ValueError("intent_contract_json_invalid") from exc


def _text(value: object, maximum: int, field: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"intent_contract_{field}_invalid")
    # Ordinary whitespace is retained verbatim in the graph.  Non-whitespace
    # controls (including bidi overrides and isolated surrogates) are unsafe.
    if any(unicodedata.category(char) in {"Cc", "Cf", "Cs"}
           and not char.isspace() for char in value):
        raise ValueError(f"intent_contract_{field}_control_character")
    return value


def _contract_core(contract: object) -> dict:
    if not isinstance(contract, dict) or type(contract.get("version")) is not int \
            or contract["version"] != 1:
        raise ValueError("intent_contract_version_invalid")
    question = _text(contract.get("question"), 2000, "question")
    goal = _text(contract.get("goal"), 3000, "goal")
    requirements = contract.get("requirements")
    if not isinstance(requirements, list) or not 1 <= len(requirements) <= 8:
        raise ValueError("intent_contract_requirements_invalid")
    requirements = [_text(item, 300, "requirement") for item in requirements]
    revision = contract.get("revision")
    if not isinstance(revision, dict):
        raise ValueError("intent_contract_revision_invalid")
    core = {"question": question, "goal": goal, "requirements": requirements,
            "revision": revision}
    # Copy the JSON-compatible input so a caller cannot mutate graph provenance
    # indirectly.  Expiry is intentionally not part of the content identity.
    return json.loads(_canonical(core))


def compile_contract(contract: dict) -> dict:
    """Return the deterministic graph of the exact reviewed question and goal."""
    core = _contract_core(contract)
    nodes = [
        {"node_id": "Q1", "node_type": "question", "text": core["question"],
         "source_pointer": "/question"},
        {"node_id": "G1", "node_type": "goal", "text": core["goal"],
         "source_pointer": "/goal"},
    ]
    edges = [{"from": "Q1", "to": "G1", "type": "expresses_goal",
              "basis": "explicit_user_contract", "source_pointer": "/goal"}]
    for index, requirement in enumerate(core["requirements"]):
        requirement_id = f"R{index + 1}"
        pointer = f"/requirements/{index}"
        nodes.append({"node_id": requirement_id, "node_type": "requirement",
                      "text": requirement, "source_pointer": pointer})
        edges.extend([
            {"from": "Q1", "to": requirement_id, "type": "requires",
             "basis": "explicit_user_contract", "source_pointer": pointer},
            {"from": "G1", "to": requirement_id, "type": "scopes",
             "basis": "explicit_user_contract", "source_pointer": pointer},
        ])
    return {
        "schema_version": SCHEMA_VERSION,
        "contract_sha256": hashlib.sha256(_canonical(core).encode("utf-8")).hexdigest(),
        **core,
        "nodes": nodes,
        "edges": edges,
    }


def plan_from_contract(contract: dict) -> dict:
    """Make all reviewed requirements mandatory, without an LLM planning call.

    Display labels may be shortened; the requirement itself is never truncated.
    Whitespace normalization only adapts multiline user text to legacy fields.
    The full original question and goal remain in ``intent_graph`` and must also
    be delivered as context by the caller; generic requirement text is not a
    substitute for that context.
    """
    graph = compile_contract(contract)
    items = []
    for index, requirement in enumerate(graph["requirements"], 1):
        normalized = " ".join(requirement.split())
        items.append({
            "item_id": f"F{index}",
            "intent_requirement_id": f"R{index}",
            "label": normalized[:80],
            "required_claim": normalized,
            "retrieval_query": normalized,
            "required": True,
        })
    scope = '\n'.join((graph['question'], graph['goal'], *graph['requirements']))
    return {"items": items, "answer_shape": ANSWER_SHAPE, "intent_graph": graph,
            "partial_answer_allowed": not any(phrase in scope for phrase in
                ('一つに確定', '一つに特定', '一意に確定'))}


def validate_plan_binding(plan: dict) -> None:
    """Reject changed requirements or a graph whose provenance no longer binds.

    This detects divergence from the embedded contract, not malicious changes to
    an entire unsigned artifact.  The app must independently bind the graph's
    hash to its verified contract.  Engine metadata outside these three owned
    fields is allowed; items and graph are compared canonically and in full.
    """
    if not isinstance(plan, dict) or not isinstance(plan.get("intent_graph"), dict):
        raise ValueError("intent_plan_graph_invalid")
    graph = plan["intent_graph"]
    expected = plan_from_contract({
        "version": 1,
        "question": graph.get("question"),
        "goal": graph.get("goal"),
        "requirements": graph.get("requirements"),
        "revision": graph.get("revision"),
    })
    for key in ("intent_graph", "items", "answer_shape", "partial_answer_allowed"):
        if _canonical(plan.get(key)) != _canonical(expected[key]):
            raise ValueError(f"intent_plan_{key}_mismatch")


def build_work_mapping(plan: dict, field_aliases: dict) -> dict:
    """Describe candidate lookup work without replacing a reviewed requirement.

    A lexical field mention is only a routing hint: it can be negated, refer to
    another subject, or be part of a larger request. Each requirement therefore
    keeps a mandatory whole-requirement job. No lookup result is sufficient to
    mark that job complete. Existing QEG validators must still decide whether
    any proposed lookup is applicable, temporally valid, and source-grounded.
    This function schedules no model calls and changes no execution route.
    """
    validate_plan_binding(plan)
    if not isinstance(field_aliases, dict) or not field_aliases:
        raise ValueError('intent_work_aliases_invalid')
    aliases = {}
    for field, values in sorted(field_aliases.items()):
        if (not isinstance(field, str) or not field
                or not isinstance(values, (list, tuple)) or not values
                or any(not isinstance(v, str) or not v.strip() for v in values)):
            raise ValueError('intent_work_aliases_invalid')
        aliases[field] = list(values)
    graph = plan['intent_graph']
    jobs, links = [], []
    for item, original in zip(plan['items'], graph['requirements']):
        requirement_id = item['intent_requirement_id']
        whole_id = f'{requirement_id}:whole'
        jobs.append({
            'work_id': whole_id, 'kind': 'whole_requirement',
            'item_id': item['item_id'], 'requirement_id': requirement_id,
            'text': original, 'required': True,
            'status': 'requires_semantic_matching',
        })
        links.append({'from': requirement_id, 'to': whole_id,
                      'type': 'requires_full_coverage'})
        normalized = unicodedata.normalize('NFKC', original).casefold()
        for field, values in aliases.items():
            hits = []
            for alias in values:
                key = unicodedata.normalize('NFKC', alias).casefold()
                if key.isascii():
                    tokens = re.findall(r'[0-9a-z]+', key)
                    pattern = r'[^0-9a-z]+'.join(map(re.escape, tokens))
                    matched = bool(tokens) and re.search(
                        rf'(?<![0-9a-z]){pattern}(?![0-9a-z])', normalized)
                else:
                    matched = key in normalized
                if matched:
                    hits.append(alias)
            if not hits:
                continue
            work_id = f'{requirement_id}:field:{field}'
            jobs.append({
                'work_id': work_id, 'kind': 'record_field_candidate',
                'item_id': item['item_id'], 'requirement_id': requirement_id,
                'field_name': field, 'matched_aliases': hits,
                'source_pointer': graph['nodes'][int(requirement_id[1:]) + 1]['source_pointer'],
                'source_text': original,
                'status': 'candidate_not_semantically_validated',
                'satisfies_requirement': False,
            })
            links.append({'from': whole_id, 'to': work_id,
                          'type': 'has_lexical_lookup_candidate'})
    body = {
        'schema_version': '1.0-intent-work-mapping',
        'contract_sha256': graph['contract_sha256'],
        'alias_catalog_sha256': hashlib.sha256(_canonical(aliases).encode()).hexdigest(),
        # Keep the full question/goal, revision, and uniqueness guard rather
        # than projecting only the field name and accidentally losing scope.
        'scope': {'question': graph['question'], 'goal': graph['goal'],
                  'revision': graph['revision'],
                  'partial_answer_allowed': plan['partial_answer_allowed']},
        'jobs': jobs, 'links': links,
        'execution_status': 'mapping_only_not_dispatched',
        'requirements_checked': False,
    }
    body['mapping_sha256'] = hashlib.sha256(_canonical(body).encode()).hexdigest()
    return json.loads(_canonical(body))


def validate_work_mapping(plan: dict, mapping: dict, field_aliases: dict) -> None:
    """Rebuild, not merely trust a supplied hash or a claimed success flag."""
    if _canonical(mapping) != _canonical(build_work_mapping(plan, field_aliases)):
        raise ValueError('intent_work_mapping_mismatch')


def dispatch_lookup_candidates(plan: dict, records: list, source_graph: dict,
                               reference_date: str, graph_api) -> dict:
    """Run the existing deterministic QEG as an auxiliary candidate lookup.

    No query rewriting, field merging, semantic adoption, or model calls. All
    fields must be planned together to preserve QEG's unplanned-field guard.
    Duplicate fields across requirements deliberately remain duplicate: the
    existing QEG may hold them rather than merge different subjects silently.
    """
    mapping = build_work_mapping(plan, graph_api.RECORD_LOOKUP_FIELD_ALIASES)
    candidates = [j for j in mapping['jobs'] if j['kind'] == 'record_field_candidate']
    result = {'schema_version': '1.0-intent-lookup-dispatch',
              'mapping_sha256': mapping['mapping_sha256'],
              'reference_date': reference_date, 'status': 'not_applicable',
              'reason': 'no_explicit_field_candidate', 'work_links': [],
              'selected_by_item': {i['item_id']: [] for i in plan['items']},
              'requirements_checked': False, 'relations_adopted': False}
    if not candidates:
        return result
    items = []
    for number, job in enumerate(candidates, 1):
        work_item_id = f'W{number}'
        items.append({'item_id': work_item_id, 'label': job['field_name'],
                      'field_name': job['field_name'], 'required': True,
                      'required_claim': job['source_text'],
                      'retrieval_query': plan['intent_graph']['question']})
        result['work_links'].append({'work_item_id': work_item_id,
            'work_id': job['work_id'], 'item_id': job['item_id'],
            'requirement_id': job['requirement_id']})
    lookup_plan = {'items': items, 'answer_shape': 'candidate evidence only',
                   'partial_answer_allowed': plan['partial_answer_allowed']}
    for key in ('operation', 'target', 'relation', 'temporal_scope'):
        if key in plan:
            lookup_plan[key] = json.loads(_canonical(plan[key]))
    question = plan['intent_graph']['question']
    artifact = graph_api.build_question_evidence_graph(question, records,
        source_graph=source_graph, question_plan=lookup_plan, reference_date=reference_date)
    validation = graph_api.validate_question_evidence_graph(question, records, artifact,
        source_graph=source_graph, question_plan=lookup_plan, reference_date=reference_date)
    result.update(lookup_plan=lookup_plan, graph=artifact, validation=validation)
    if artifact.get('intent', {}).get('operation') != 'record_lookup':
        result.update(status='not_applicable', reason='not_record_lookup')
        return result
    if artifact.get('status') != 'ready' or validation.get('status') != 'pass':
        result.update(status='held', reason=str(artifact.get('reason') or 'lookup_validation_failed'))
        return result
    branches = artifact.get('branches', [])
    if [b.get('item_id') for b in branches] != [i['item_id'] for i in items]:
        raise ValueError('intent_lookup_branch_binding_mismatch')
    for link, branch in zip(result['work_links'], branches):
        ids = branch.get('selected_evidence_ids')
        if not isinstance(ids, list) or not ids or any(not isinstance(i, str) for i in ids):
            raise ValueError('intent_lookup_selection_invalid')
        target = result['selected_by_item'][link['item_id']]
        target.extend(i for i in ids if i not in target)
    if len({i for ids in result['selected_by_item'].values() for i in ids}) > 12:
        result.update(status='scope_limited', reason='candidate_block_budget_exceeded')
        result['selected_by_item'] = {i['item_id']: [] for i in plan['items']}
    else:
        result.update(status='candidate_ready', reason='source_bound_lookup_not_requirement_coverage')
    return result


def validate_lookup_dispatch(plan: dict, dispatch: dict, records: list,
                             source_graph: dict, reference_date: str, graph_api) -> None:
    expected = dispatch_lookup_candidates(plan, records, source_graph, reference_date, graph_api)
    if _canonical(expected) != _canonical(dispatch):
        raise ValueError('intent_lookup_dispatch_mismatch')
