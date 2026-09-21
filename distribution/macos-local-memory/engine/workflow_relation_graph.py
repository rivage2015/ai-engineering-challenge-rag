"""Bounded, source-grounded candidate relations for local workflow questions.

No model, storage, network, or answer generation lives here. Quotation checks
prove textual provenance, not the truth of an extracted semantic relation.
"""

import copy
import json
import re
from collections import Counter


GRAPH_VERSION = "workflow-relation-v1"
NODE_KINDS = ("actor", "condition", "action", "speech", "caution")
EDGE_ENDPOINTS = {
    "performs": ({"actor"}, {"action"}),
    "when": ({"condition"}, {"action"}),
    "next": ({"action"}, {"action"}),
    "says": ({"actor", "action"}, {"speech"}),
    "warns": ({"caution"}, {"action"}),
}
MAX_NODES = 48
MAX_EDGES = 64
MAX_QUOTE = 1800
_ID = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,31}\Z")
_PACKET = re.compile(r"E[1-9][0-9]*\Z")


def _string_schema(**extra):
    return {"type": "string", "minLength": 1, **extra}


SOURCE_GRAPH_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["nodes", "edges", "unresolved_packet_ids"],
    "properties": {
        "nodes": {
            "type": "array", "maxItems": MAX_NODES,
            "items": {
                "type": "object", "additionalProperties": False,
                "required": ["id", "kind", "packet_id", "quote"],
                "properties": {
                    "id": _string_schema(pattern="^[A-Za-z][A-Za-z0-9_-]{0,31}$", maxLength=32),
                    "kind": {"type": "string", "enum": list(NODE_KINDS)},
                    "packet_id": _string_schema(pattern="^E[1-9][0-9]*$"),
                    "quote": _string_schema(maxLength=MAX_QUOTE),
                },
            },
        },
        "edges": {
            "type": "array", "maxItems": MAX_EDGES,
            "items": {
                "type": "object", "additionalProperties": False,
                "required": ["id", "source", "target", "kind", "support_packet_id", "support_quote"],
                "properties": {
                    "id": _string_schema(pattern="^[A-Za-z][A-Za-z0-9_-]{0,31}$", maxLength=32),
                    "source": _string_schema(pattern="^[A-Za-z][A-Za-z0-9_-]{0,31}$", maxLength=32),
                    "target": _string_schema(pattern="^[A-Za-z][A-Za-z0-9_-]{0,31}$", maxLength=32),
                    "kind": {"type": "string", "enum": list(EDGE_ENDPOINTS)},
                    "support_packet_id": _string_schema(pattern="^E[1-9][0-9]*$"),
                    "support_quote": _string_schema(maxLength=MAX_QUOTE),
                },
            },
        },
        "unresolved_packet_ids": {
            "type": "array", "uniqueItems": True,
            "items": _string_schema(pattern="^E[1-9][0-9]*$"),
        },
    },
}


# These patterns identify requested answer roles, never answer values. A plan
# cannot make a role required when the user's own question did not request it.
_REQUIREMENTS = (
    ("actions", "対応する行動", r"手順|対応|行動|流れ|フロー|ワークフロー|一連", "action", None),
    ("speech", "声がけのセリフ", r"セリフ|台詞|せりふ|声[がか掛]け|挨拶|あいさつ", "speech", "says"),
    ("conditions", "条件ごとの行動", r"条件|分岐|場合", "condition", "when"),
    ("actors", "担当と行動", r"誰|担当|引[き]?継[ぎぐ]|引継", "actor", "performs"),
    ("order", "行動の順番", r"順番|手順|流れ|フロー|ワークフロー|一連|その後|次に", "action", "next"),
    ("cautions", "行動に関する注意", r"注意|禁止|気を[つ付]け", "caution", "warns"),
)


def build_question_graph(query, plan):
    """Build unknown relation requirements, with exact question-quote anchors."""
    if not isinstance(query, str):
        raise ValueError("question_must_be_text")
    graph = {"schema_version": GRAPH_VERSION, "status": "not_applicable",
             "query": query, "nodes": [], "edges": [], "requirements": []}
    if not query.strip():
        return graph
    graph["nodes"].append({"id": "Q_request", "kind": "request", "text": query,
                           "provenance": {"kind": "query", "quote": query}})
    plan_items = plan.get("items", []) if isinstance(plan, dict) else []
    if not isinstance(plan_items, list):
        plan_items = []
    for kind, label, pattern, variable_kind, relation in _REQUIREMENTS:
        match = re.search(pattern, query)
        if not match:
            continue
        requirement_id = "Q_" + kind
        query_quote = match.group()
        provenance = {"kind": "query", "quote": query_quote,
                      "span": [match.start(), match.end()]}
        item_ids = [item["item_id"] for item in plan_items
                    if isinstance(item, dict) and isinstance(item.get("item_id"), str)
                    and re.search(pattern, str(item.get("required_claim", "")))]
        graph["requirements"].append({"id": requirement_id, "kind": kind,
                                       "label": label, "query_quote": query_quote,
                                       "item_ids": list(dict.fromkeys(item_ids))})
        graph["nodes"].append({"id": requirement_id, "kind": "requirement",
                               "requirement_kind": kind, "label": label,
                               "answer_status": "unknown", "provenance": provenance})
        graph["edges"].append({"id": "QE_asks_" + kind, "source": "Q_request",
                               "target": requirement_id, "kind": "asks_for",
                               "status": "requested_unknown", "provenance": provenance})
        if relation:
            source_kind, target_kind = {
                "says": ("actor", "speech"), "when": ("condition", "action"),
                "performs": ("actor", "action"), "next": ("action", "action"),
                "warns": ("caution", "action"),
            }[relation]
            source_id, target_id = requirement_id + "_from", requirement_id + "_to"
            for node_id, node_kind in ((source_id, source_kind), (target_id, target_kind)):
                graph["nodes"].append({"id": node_id, "kind": node_kind,
                                       "answer_status": "unknown", "provenance": provenance})
            graph["edges"].append({"id": "QE_" + relation, "source": source_id,
                                   "target": target_id, "kind": relation,
                                   "requirement_id": requirement_id,
                                   "status": "requested_unknown", "provenance": provenance})
        else:
            graph["nodes"].append({"id": requirement_id + "_value", "kind": variable_kind,
                                   "answer_status": "unknown", "provenance": provenance})
    if graph["requirements"]:
        graph["status"] = "ready"
    return graph


def _source_text(record):
    text = record.get("text") if isinstance(record, dict) else None
    if not isinstance(text, str) or not text.strip():
        return None
    try:
        decoded = json.loads(text)
    except (ValueError, TypeError):
        return text
    return decoded if isinstance(decoded, str) else text


def _scope(record):
    if not isinstance(record, dict) or not isinstance(record.get("locator"), dict):
        return None
    doc, path = record.get("document_id"), record.get("relative_path")
    sheet = record["locator"].get("sheet_name")
    if not isinstance(doc, str) or not doc or not isinstance(path, str) or not path:
        return None
    if sheet is not None and (not isinstance(sheet, str) or not sheet):
        return None
    return doc, path, sheet


def _provenance(record):
    return {"document_id": record["document_id"], "relative_path": record["relative_path"],
            "locator": copy.deepcopy(record["locator"])}


def _valid_id(value):
    return isinstance(value, str) and bool(_ID.fullmatch(value))


def _valid_quote(quote, text):
    return (isinstance(quote, str) and 0 < len(quote) <= MAX_QUOTE
            and bool(quote.strip()) and isinstance(text, str) and quote in text)


def normalize_source_graph(payload, packet_sources):
    """Reject ungrounded relations without promoting quoted candidates to truth.

    The support quote must include both endpoint quotes in the same source
    scope. That still does not prove the semantic label/direction is correct.
    Caller must keep raw evidence alongside this candidate graph.
    """
    if not isinstance(packet_sources, dict):
        raise ValueError("packet_sources_must_be_mapping")
    packet_ids = [key for key in packet_sources if isinstance(key, str) and _PACKET.fullmatch(key)]
    result = {"schema_version": GRAPH_VERSION, "status": "invalid",
              "semantic_validation": "not_performed", "nodes": [], "edges": [],
              "unresolved_packet_ids": packet_ids.copy(), "issues": []}

    def issue(code, identifier=""):
        result["issues"].append({"code": code, "id": identifier if isinstance(identifier, str) else ""})

    if (not isinstance(payload, dict)
            or set(payload) != {"nodes", "edges", "unresolved_packet_ids"}
            or not all(isinstance(payload.get(key), list)
                       for key in ("nodes", "edges", "unresolved_packet_ids"))):
        issue("invalid_graph_envelope")
        return result
    if len(payload["nodes"]) > MAX_NODES or len(payload["edges"]) > MAX_EDGES:
        issue("graph_budget_exceeded")
        return result
    records = {key: record for key, record in packet_sources.items()
               if key in packet_ids and _scope(record) is not None and _source_text(record) is not None
               and isinstance(record.get("evidence_id"), str) and bool(record["evidence_id"])}
    for key in packet_ids:
        if key not in records:
            issue("invalid_packet_source", key)
    all_ids = Counter(item.get("id") for item in payload["nodes"] + payload["edges"]
                      if isinstance(item, dict) and isinstance(item.get("id"), str))
    node_keys = {"id", "kind", "packet_id", "quote"}
    by_id = {}
    for node in payload["nodes"]:
        identifier = node.get("id", "") if isinstance(node, dict) else ""
        if not isinstance(node, dict) or set(node) != node_keys or not _valid_id(identifier):
            issue("invalid_node", identifier)
            continue
        if all_ids[identifier] != 1:
            issue("duplicate_graph_id", identifier)
            continue
        record = records.get(node["packet_id"]) if isinstance(node["packet_id"], str) else None
        if node["kind"] not in NODE_KINDS or record is None:
            issue("invalid_node_kind_or_packet", identifier)
            continue
        if not _valid_quote(node["quote"], _source_text(record)):
            issue("node_quote_not_grounded", identifier)
            continue
        accepted = {**node, "status": "grounded_candidate", "evidence_id": record["evidence_id"],
                    "provenance": _provenance(record)}
        by_id[identifier] = accepted
        result["nodes"].append(accepted)
    edge_keys = {"id", "source", "target", "kind", "support_packet_id", "support_quote"}
    for edge in payload["edges"]:
        identifier = edge.get("id", "") if isinstance(edge, dict) else ""
        if not isinstance(edge, dict) or set(edge) != edge_keys or not _valid_id(identifier):
            issue("invalid_edge", identifier)
            continue
        if all_ids[identifier] != 1:
            issue("duplicate_graph_id", identifier)
            continue
        source = by_id.get(edge["source"]) if isinstance(edge["source"], str) else None
        target = by_id.get(edge["target"]) if isinstance(edge["target"], str) else None
        kind = edge["kind"]
        if not source or not target or source["id"] == target["id"]:
            issue("invalid_edge_endpoints", identifier)
            continue
        if (not isinstance(kind, str) or kind not in EDGE_ENDPOINTS
                or source["kind"] not in EDGE_ENDPOINTS[kind][0]
                or target["kind"] not in EDGE_ENDPOINTS[kind][1]):
            issue("edge_endpoint_kind_mismatch", identifier)
            continue
        record = records.get(edge["support_packet_id"]) if isinstance(edge["support_packet_id"], str) else None
        if record is None:
            issue("invalid_edge_support_packet", identifier)
            continue
        if not (_scope(record) == _scope(records[source["packet_id"]])
                == _scope(records[target["packet_id"]])):
            issue("cross_scope_edge", identifier)
            continue
        quote = edge["support_quote"]
        if (not _valid_quote(quote, _source_text(record))
                or source["quote"] not in quote or target["quote"] not in quote):
            issue("edge_quote_not_grounded", identifier)
            continue
        result["edges"].append({**edge, "status": "grounded_candidate",
                                "evidence_id": record["evidence_id"], "provenance": _provenance(record)})
    explicit_unresolved = set()
    for packet_id in payload["unresolved_packet_ids"]:
        if not isinstance(packet_id, str) or packet_id not in packet_ids:
            issue("unknown_unresolved_packet")
        else:
            explicit_unresolved.add(packet_id)
    represented = {node["packet_id"] for node in result["nodes"]}
    represented.update(edge["support_packet_id"] for edge in result["edges"])
    result["unresolved_packet_ids"] = [key for key in packet_ids
                                        if key not in represented or key in explicit_unresolved]
    result["status"] = ("partial" if result["issues"] or result["unresolved_packet_ids"]
                        else "grounded_candidates") if result["nodes"] else "empty"
    return result


def build_relation_matches(question_graph, source_graph):
    """Map requested roles/types to candidates; no semantic match is certified."""
    result = {"status": "candidate_mapping", "verified": False,
              "matches": [], "unmatched_requirement_ids": []}
    requirement_types = {
        "actions": ("action", None), "speech": ("speech", "says"),
        "conditions": ("condition", "when"), "actors": ("actor", "performs"),
        "order": ("action", "next"), "cautions": ("caution", "warns"),
    }
    for requirement in question_graph.get("requirements", []):
        node_kind, edge_kind = requirement_types.get(requirement.get("kind"), (None, None))
        nodes = [node["id"] for node in source_graph.get("nodes", [])
                 if node.get("kind") == node_kind and node.get("status") == "grounded_candidate"]
        edges = [edge["id"] for edge in source_graph.get("edges", [])
                 if edge.get("kind") == edge_kind and edge.get("status") == "grounded_candidate"] if edge_kind else []
        matched = bool(edges) if edge_kind else bool(nodes)
        result["matches"].append({"requirement_id": requirement["id"], "kind": requirement["kind"],
                                  "node_ids": nodes, "edge_ids": edges,
                                  "status": "candidate" if matched else "unmatched"})
        if not matched:
            result["unmatched_requirement_ids"].append(requirement["id"])
    return result


def render_graph_context(question_graph, source_graph, matches):
    """Return complete compact JSON; the caller must enforce its input budget.

    Do not drop quotes or relations to meet a budget here: silent truncation
    would falsely make the delivered graph appear complete.
    """
    # Full file/locator provenance remains in the trace and the adjacent raw
    # SOURCE/EVIDENCE bundle. Packet IDs bind this compact view to that bundle.
    question_view = {
        "query": question_graph.get("query", ""),
        "requirements": question_graph.get("requirements", []),
        "nodes": [{key: value for key, value in node.items() if key not in {"provenance", "text"}}
                  for node in question_graph.get("nodes", [])],
        "edges": [{key: value for key, value in edge.items() if key != "provenance"}
                  for edge in question_graph.get("edges", [])],
    }
    source_view = {
        "status": source_graph.get("status", "invalid"),
        "semantic_validation": "not_performed",
        "nodes": [{key: value for key, value in node.items() if key not in {"provenance", "evidence_id"}}
                  for node in source_graph.get("nodes", [])],
        "edges": [{key: value for key, value in edge.items() if key not in {"provenance", "evidence_id"}}
                  for edge in source_graph.get("edges", [])],
        "unresolved_packet_ids": source_graph.get("unresolved_packet_ids", []),
        "issues": source_graph.get("issues", []),
    }
    return json.dumps({
        "notice": "Question relations are unknown requirements. Source relations and matches are unverified candidates; raw evidence is authoritative. Source order is not workflow order.",
        "question_graph": question_view, "source_graph": source_view, "relation_matches": matches,
    }, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
