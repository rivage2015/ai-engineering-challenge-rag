"""Fail-closed checks for a normal local final-audit response.

Usage metadata establishes that the response finished within the requested
capacity; it is not proof that the model read or understood every input token.
No model text is repaired, retried, or retained in failure diagnostics here.
"""

from __future__ import annotations

import json


NORMAL_CONTEXT_TOKENS = 8192


class AuditResponseError(ValueError):
    def __init__(self, code: str, diagnostics: dict):
        super().__init__(code)
        self.code = code
        self.diagnostics = diagnostics


def context_diagnostics(raw: object, context_tokens: int, output_tokens: int) -> dict:
    raw = raw if isinstance(raw, dict) else {}
    input_count = raw.get("prompt_eval_count")
    output_count = raw.get("eval_count")
    input_count = input_count if type(input_count) is int and input_count >= 0 else None
    output_count = output_count if type(output_count) is int and output_count >= 0 else None
    reason = raw.get("done_reason")
    # Metadata only. Arbitrary response strings must not leak into UI/logs.
    reason = reason if reason in ("stop", "length", "load", "unload") else None
    total = input_count + output_count if input_count is not None and output_count is not None else None
    return {
        "requested_context_tokens": context_tokens,
        "requested_output_tokens": output_tokens,
        "input_tokens": input_count,
        "output_tokens": output_count,
        "total_tokens": total,
        "done": raw.get("done") if type(raw.get("done")) is bool else None,
        "done_reason": reason,
        "status": "unverified",
    }


def validate_schema(value: object, schema: dict) -> bool:
    """Validate the small object/string/array contract used by this auditor."""
    kind = schema.get("type")
    if kind == "object":
        if not isinstance(value, dict):
            return False
        properties = schema.get("properties", {})
        if any(key not in value for key in schema.get("required", [])):
            return False
        if schema.get("additionalProperties") is False and set(value) - set(properties):
            return False
        return all(validate_schema(item, properties[key]) for key, item in value.items() if key in properties)
    if kind == "array":
        return (isinstance(value, list) and len(value) <= schema.get("maxItems", len(value))
                and all(validate_schema(item, schema["items"]) for item in value))
    if kind == "string":
        return (isinstance(value, str) and len(value) <= schema.get("maxLength", len(value))
                and ("enum" not in schema or value in schema["enum"]))
    return False


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_json_key")
        result[key] = value
    return result


def parse_normal_response(raw: object, schema: dict, context_tokens: int, output_tokens: int) -> tuple[dict, dict]:
    diagnostics = context_diagnostics(raw, context_tokens, output_tokens)

    def fail(code: str) -> None:
        diagnostics["status"] = "incomplete"
        raise AuditResponseError(code, diagnostics)

    if not isinstance(raw, dict):
        fail("audit_response_type_invalid")
    # Check termination before content parsing: even a valid-looking JSON
    # object from a capacity-limited response must never pass the audit.
    if raw.get("done_reason") == "length":
        fail("audit_response_truncated")
    if raw.get("done") is not True or raw.get("done_reason") != "stop":
        fail("audit_response_not_finished")
    if diagnostics["input_tokens"] is None or diagnostics["output_tokens"] is None:
        fail("audit_usage_missing")
    if diagnostics["input_tokens"] == 0 or diagnostics["output_tokens"] == 0:
        fail("audit_usage_invalid")
    if diagnostics["total_tokens"] >= context_tokens:
        fail("audit_context_exhausted")
    if diagnostics["output_tokens"] > output_tokens:
        fail("audit_usage_invalid")
    message = raw.get("message")
    if not isinstance(message, dict) or not isinstance(message.get("content"), str):
        fail("audit_response_type_invalid")
    try:
        result = json.loads(message["content"], object_pairs_hook=_unique_object)
    except (json.JSONDecodeError, ValueError):
        fail("audit_response_invalid_json")
    if not validate_schema(result, schema):
        fail("audit_response_schema_invalid")
    diagnostics["status"] = "observed"
    return result, diagnostics
