"""検索結果をもとに OpenAI またはローカル Ollama で回答を生成する."""

from __future__ import annotations

import os
import json
import math
import re
import unicodedata
import urllib.error
import urllib.request
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

BACKEND = os.environ.get("RAG_BACKEND", "ollama").strip().lower()
MODEL = os.environ.get(
    "RAG_MODEL",
    "qwen3.5:9b" if BACKEND == "ollama" else "gpt-5.2",
)
OLLAMA_BASE_URL = os.environ.get("OLLAMA_BASE_URL", "http://127.0.0.1:11434")
MAX_CONTEXT_CHARS = 60000
MAX_GRAPH_PLAN_CHARS = 12000
OLLAMA_CONTEXT_TIERS = (8192, 16384, 32768, 65536)
OLLAMA_NUM_PREDICT = 768
OLLAMA_CONTEXT_SAFETY_TOKENS = 512

SYSTEM = """あなたは社内共有ドライブの資料にもとづいて質問に答えるアシスタントです。

厳守事項:
1. 与えられた【資料】に書かれている内容のみを根拠にしてください。資料にない情報を
   推測や一般知識で補ってはいけません。
2. 根拠が資料から読み取れない場合、必ず「わかりません」とだけ答えてください。
   誤った回答は減点されますが、「わかりません」は減点されません。推測で答えないでください。
3. 回答は結論のみを簡潔に書いてください。前置き・説明・根拠の引用は不要です。
4. 質問文に単位・小数桁・丸め方・表記の指定がある場合は、必ずそれに従ってください。
5. 「すべて挙げてください」と指示された場合、資料から完全な一覧が確定できるときのみ
   列挙してください。抜け漏れの可能性があるなら「わかりません」と答えてください。
6. 質問が出力表記を指定していない場合は、通常の表現で答えてください。
   ただし、質問が「主略称」「略称」「正式名称」「フルネーム」「ID」などを
   明示した場合は、その指定を通常表現より優先してください。資料内で定義された
   タスクID・アクションID・マイルストーンID・列名・パラメータ名などの識別子は、
   資料上の表記どおりに書いてください。
7. 条件に該当するものが資料上存在しない場合は、該当するものがない旨を答えてください。
8. 質問が「AとB」「AおよびB」のように複数の要素を求める場合は、
   すべての要素を回答に含めてください。「簡潔に」とは根拠説明を省くことであり、
   要求された要素を省略することではありません。
"""

USER_TEMPLATE = """【資料】
{context}

{output_rendering_guidance}{facet_guidance}【質問】
{question}

上記の資料のみを根拠に、結論だけを簡潔に答えてください。
根拠が資料から確認できない場合は「わかりません」とだけ答えてください。"""

GRAPH_SYSTEM = SYSTEM + """

質問理解グラフを使うときの追加規則:
9. 「質問理解グラフ」は、質問の対象・範囲・条件・演算順序・出力形式を表す
   解釈契約です。回答値の根拠ではありません。回答値は必ず「資料」から求めてください。
10. グラフに書かれたフィルタ、論理接続、集計、丸め、単位、件数と一覧の区別を省略しないでください。
11. 候補分岐が複数ある場合は、資料によって一意に決まる分岐だけを使います。
    一意に決まらない場合は推測しません。
12. グラフと資料が食い違って見える場合、グラフに合わせて資料値を作ってはいけません。
13. 後続演算の入力になる中間値は計算のために使い、terminal_requested_outputsに指定された
    最終出力だけを回答してください。
14. 「明示出力表記候補」は、質問で要求された表記に変換するためだけに使います。
    それ自体を回答対象の根拠にしてはいけません。同じ略称に正式名称候補が複数あるときは、
    資料で一意に決められない候補を推測で選びません。
"""

GRAPH_USER_TEMPLATE = """【質問理解グラフ】
{graph_plan}

【資料】
{context}

{output_rendering_guidance}{facet_guidance}【質問】
{question}

質問理解グラフの演算順序と出力契約に従い、資料の根拠だけで結論を一つ作ってください。
説明や根拠の引用は付けず、指定された形式の回答だけを出力してください。"""

GRAPH_REPAIR_TEMPLATE = """直前の回答は、質問理解グラフの出力契約に次の点で合いません:
{violations}

資料の根拠と回答内容は変えず、件数か一覧か、単位、小数桁、単一行などの形式だけを修正してください。
根拠のない値を追加せず、修正後の回答だけを出力してください。"""

_FACET_CHARACTER = r"0-9A-Za-z_一-鿿々〆ヵヶぁ-んァ-ヴー"
_PARALLEL_FACET = re.compile(
    rf"(?:および|及び|ならびに|並びに|"
    rf"(?<=[{_FACET_CHARACTER}])\s*と\s*"
    rf"(?!(?:して|する|した|いう|なる|なり|ころ|き|"
    rf"を|が|に|は|へ|で|の|も))(?=[{_FACET_CHARACTER}])|"
    rf"(?<=\w)\s+(?:and|&)\s+(?=\w))",
    flags=re.IGNORECASE,
)


def _question_facet_clauses(question: str) -> tuple[str, ...]:
    """Return question-only clauses containing an explicit parallel request."""

    if not isinstance(question, str):
        return ()
    clauses: list[str] = []
    for raw_clause in re.split(r"[。！？!?\n]+", question):
        clause = raw_clause.strip()
        if not clause or _PARALLEL_FACET.search(clause) is None:
            continue
        if clause not in clauses:
            clauses.append(clause)
    return tuple(clauses)


def _facet_guidance(question: str) -> str:
    clauses = _question_facet_clauses(question)
    if not clauses:
        return ""
    rendered = json.dumps(clauses, ensure_ascii=False, separators=(",", ":"))
    return (
        "【質問由来の省略禁止facet】\n"
        "次の並列要求は質問文から抽出した回答形式であり、回答値の根拠ではありません。"
        "各要素を省略せず回答してください。\n"
        f"{rendered}\n\n"
    )


def _output_rendering_candidates(
    question: str,
    chunks: Sequence[object],
    glossary: object | None,
) -> list[Mapping[str, Any]]:
    if glossary is None:
        return []
    provider = getattr(glossary, "output_alias_candidates", None)
    if not callable(provider):
        return []
    candidates = provider(question, chunks)
    if not isinstance(candidates, Sequence) or isinstance(
        candidates, (str, bytes, bytearray)
    ):
        return []
    return [candidate for candidate in candidates if isinstance(candidate, Mapping)]


def _output_rendering_guidance(
    candidates: Sequence[Mapping[str, Any]],
) -> str:
    if not candidates:
        return ""
    rendered = json.dumps(
        list(candidates),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return (
        "【明示出力表記候補（取得資料のpath/projectと用語集由来）】\n"
        "この対応表は、質問で明示された表記への変換にだけ使い、"
        "回答値の根拠としては使わないでください。"
        "ambiguous=trueの略称はcanonical_candidatesの全候補を保持し、"
        "資料で一意に決められない場合は選びません。\n"
        f"{rendered}\n\n"
    )


def _expand_question(question: str, glossary: object | None) -> str:
    expand = getattr(glossary, "expand", None)
    return expand(question) if callable(expand) else question


def _render_primary_aliases(
    answer: str,
    question: str,
    chunks: Sequence[object],
    glossary: object | None,
) -> str:
    renderer = getattr(glossary, "render_primary_aliases", None)
    if not callable(renderer):
        return answer
    rendered = renderer(question, answer, chunks)
    return rendered if isinstance(rendered, str) else answer


def build_context(chunks) -> str:
    parts, total = [], 0
    for c in chunks:
        block = f"--- {c.header()}\n{c.text}\n"
        if total + len(block) > MAX_CONTEXT_CHARS:
            break
        parts.append(block)
        total += len(block)
    return "\n".join(parts)


class AnswerClient(Protocol):
    backend: str
    model: str

    def check(self) -> None: ...

    def generate(self, messages: list[dict[str, str]]) -> str: ...


@dataclass(frozen=True)
class GraphAnswerResult:
    """Answer text plus the deterministic output-contract audit result."""

    answer: str
    validation_status: str
    violations: tuple[str, ...]
    attempts: int


@dataclass
class OllamaAnswerClient:
    model: str = MODEL
    base_url: str = OLLAMA_BASE_URL
    timeout: float = 600.0
    backend: str = "ollama"

    @staticmethod
    def _estimated_prompt_tokens(messages: Sequence[Mapping[str, str]]) -> int:
        """Conservatively estimate tokens without depending on a model tokenizer.

        ASCII prose averages several characters per token, while Japanese text
        is commonly much closer to one character per token.  The estimate is
        intentionally high so dynamic context selection does not silently cut
        evidence that fitted in the former fixed 65K window.
        """

        text = "\n".join(str(message.get("content") or "") for message in messages)
        ascii_chars = sum(ord(character) < 128 for character in text)
        non_ascii_chars = len(text) - ascii_chars
        message_overhead = 16 * len(messages)
        return math.ceil(ascii_chars / 4) + non_ascii_chars + message_overhead

    @classmethod
    def _context_length(cls, messages: Sequence[Mapping[str, str]]) -> int:
        required = (
            cls._estimated_prompt_tokens(messages)
            + OLLAMA_NUM_PREDICT
            + OLLAMA_CONTEXT_SAFETY_TOKENS
        )
        for tier in OLLAMA_CONTEXT_TIERS:
            if required <= tier:
                return tier
        # Preserve the previous maximum rather than allocating beyond the
        # locally validated 65K ceiling. Ollama will apply its normal prompt
        # handling if a hostile or exceptionally large caller exceeds it.
        return OLLAMA_CONTEXT_TIERS[-1]

    def _request(self, path: str, payload=None, timeout: float | None = None):
        url = self.base_url.rstrip("/") + path
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            url, data=data, headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout or self.timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"ローカルOllamaへの接続に失敗しました: {url}: {exc}") from exc

    def check(self) -> None:
        response = self._request("/api/tags", timeout=30.0)
        names = {
            item.get("name") or item.get("model")
            for item in response.get("models", [])
        }
        requested = self.model if ":" in self.model else f"{self.model}:latest"
        if self.model not in names and requested not in names:
            raise RuntimeError(
                f"Ollamaモデルが見つかりません: {self.model}。"
                f" `ollama pull {self.model}` を実行してください。"
            )

    def generate(self, messages: list[dict[str, str]]) -> str:
        num_ctx = self._context_length(messages)
        response = self._request("/api/chat", {
            "model": self.model,
            "messages": messages,
            "stream": False,
            # Reasoning-capable local models may spend the entire output budget
            # in ``message.thinking`` and return no final answer.  The task only
            # needs the concise grounded answer, so disable exposed thinking.
            "think": False,
            "keep_alive": "10m",
            "options": {
                "temperature": 0,
                "seed": 42,
                "num_ctx": num_ctx,
                "num_predict": OLLAMA_NUM_PREDICT,
            },
        })
        message = response.get("message") or {}
        return str(message.get("content") or "").strip()


@dataclass
class OpenAIAnswerClient:
    model: str = MODEL
    backend: str = "openai"

    def __post_init__(self) -> None:
        from openai import OpenAI

        self.client = OpenAI()

    def check(self) -> None:
        self.client.models.list()

    def generate(self, messages: list[dict[str, str]]) -> str:
        kwargs = dict(model=self.model, messages=messages)
        try:
            response = self.client.chat.completions.create(
                temperature=0, seed=42, **kwargs
            )
        except Exception:
            response = self.client.chat.completions.create(**kwargs)
        return (response.choices[0].message.content or "").strip()


def make_client(
    backend: str = BACKEND,
    model: str = MODEL,
    timeout: float = 180.0,
) -> AnswerClient:
    backend = backend.strip().lower()
    if backend == "ollama":
        return OllamaAnswerClient(model=model, timeout=timeout)
    if backend == "openai":
        return OpenAIAnswerClient(model=model)
    raise ValueError("RAG_BACKEND は ollama または openai を指定してください")


def _get(plan: object, key: str, default: Any = None) -> Any:
    """Read one GraphPlan field from either a mapping or a dataclass-like object."""

    if isinstance(plan, Mapping):
        return plan.get(key, default)
    return getattr(plan, key, default)


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _bounded_value(value: Any, *, depth: int = 0) -> Any:
    """Copy JSON-like graph data into a small, prompt-safe deterministic value."""

    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value if len(value) <= 500 else value[:497] + "..."
    if depth >= 6:
        return "[depth-limited]"
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for index, key in enumerate(sorted(value, key=lambda item: str(item))):
            if index >= 40:
                result["_truncated_keys"] = len(value) - 40
                break
            result[str(key)] = _bounded_value(value[key], depth=depth + 1)
        return result
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        result = [_bounded_value(item, depth=depth + 1) for item in value[:24]]
        if len(value) > 24:
            result.append({"_truncated_items": len(value) - 24})
        return result
    return str(value)[:500]


def _selected(value: Any, keys: Sequence[str]) -> dict[str, Any]:
    source = _mapping(value)
    return {
        key: _bounded_value(source[key])
        for key in keys
        if key in source and source[key] is not None
    }


def _compact_output(value: Any) -> dict[str, Any]:
    source = _mapping(value)
    result = _selected(
        source,
        (
            "output_id",
            "source_operation_ref",
            "return_field",
            "display_precision",
            "required_keys",
            "inference_basis",
        ),
    )
    if isinstance(source.get("cardinality"), Mapping):
        result["cardinality"] = _selected(
            source["cardinality"], ("mode", "expected_count")
        )
    if isinstance(source.get("answer_shape"), Mapping):
        result["answer_shape"] = _selected(
            source["answer_shape"],
            ("container", "value_type", "unit", "precision"),
        )
    return result


def _compact_requested(value: Any) -> dict[str, Any]:
    requested = _mapping(value)
    target = _mapping(requested.get("target"))
    scope = _mapping(requested.get("scope"))
    graph = _mapping(requested.get("operation_graph"))
    if not graph and isinstance(requested.get("operations"), Sequence):
        graph = {"nodes": requested["operations"]}
    result: dict[str, Any] = {}
    if target:
        result["target"] = _selected(
            target, ("surface", "canonical_type", "instance")
        )
    if scope:
        result["scope"] = _selected(
            scope,
            (
                "location",
                "container",
                "time_or_version",
                "source",
                "match_mode",
                "filters",
            ),
        )
    if graph:
        compact_graph: dict[str, Any] = {}
        external_inputs = graph.get("external_inputs")
        if isinstance(external_inputs, Sequence) and not isinstance(
            external_inputs, (str, bytes, bytearray)
        ):
            compact_graph["external_inputs"] = [
                _selected(item, ("input_ref", "input_type", "source"))
                for item in external_inputs[:16]
                if isinstance(item, Mapping)
            ]
        nodes = graph.get("nodes") or graph.get("operations")
        if isinstance(nodes, Sequence) and not isinstance(
            nodes, (str, bytes, bytearray)
        ):
            node_keys = (
                "operation_id",
                "operator",
                "input_refs",
                "output_ref",
                "predicate",
                "fields",
                "field",
                "candidate_set_ref",
                "distance",
                "tie_policy",
                "sort_order",
                "calculation_precision",
            )
            compact_graph["nodes"] = [
                _selected(node, node_keys)
                for node in nodes[:24]
                if isinstance(node, Mapping)
            ]
        edges = graph.get("edges")
        if isinstance(edges, Sequence) and not isinstance(
            edges, (str, bytes, bytearray)
        ):
            compact_graph["edges"] = [
                _selected(edge, ("from", "to"))
                for edge in edges[:24]
                if isinstance(edge, Mapping)
            ]
        if compact_graph:
            result["operation_graph"] = compact_graph
    outputs = requested.get("requested_outputs")
    if isinstance(outputs, Sequence) and not isinstance(
        outputs, (str, bytes, bytearray)
    ):
        result["requested_outputs"] = [
            _compact_output(output)
            for output in outputs[:16]
            if isinstance(output, Mapping)
        ]
    if isinstance(requested.get("derived_summary"), Mapping):
        result["derived_summary"] = _selected(
            requested["derived_summary"],
            ("operation", "return_fields", "cardinality"),
        )
    return result


def _requested_from_contract(value: Any) -> Mapping[str, Any]:
    contract = _mapping(value)
    requested = contract.get("requested")
    if isinstance(requested, Mapping):
        return requested
    if any(
        key in contract
        for key in ("target", "scope", "operation_graph", "requested_outputs")
    ):
        return contract
    return {}


def _branch_values(plan: object) -> list[Any]:
    branches = _get(plan, "branch_intents")
    if branches is None:
        branches = _get(plan, "candidate_query_paths")
    if isinstance(branches, Sequence) and not isinstance(
        branches, (str, bytes, bytearray)
    ):
        return list(branches)
    return []


def _intent_from_branch(value: Any) -> Mapping[str, Any]:
    branch = _mapping(value)
    intent = branch.get("candidate_intent") or branch.get("intent")
    return intent if isinstance(intent, Mapping) else branch


def _terminal_outputs_for_intent(
    intent: Mapping[str, Any],
) -> list[Mapping[str, Any]]:
    outputs = intent.get("requested_outputs")
    graph = _mapping(intent.get("operation_graph"))
    nodes = graph.get("nodes") or graph.get("operations")
    if not isinstance(outputs, Sequence) or isinstance(
        outputs, (str, bytes, bytearray)
    ):
        return []
    values = [value for value in outputs if isinstance(value, Mapping)]
    if not isinstance(nodes, Sequence) or isinstance(nodes, (str, bytes, bytearray)):
        return values
    node_values = [node for node in nodes if isinstance(node, Mapping)]
    nodes_by_id = {
        str(node["operation_id"]): node
        for node in node_values
        if node.get("operation_id") is not None
    }
    consumed_refs = {
        str(reference)
        for node in node_values
        for reference in (node.get("input_refs") or [])
    }
    terminal = []
    for output in values:
        node = nodes_by_id.get(str(output.get("source_operation_ref")))
        output_ref = node.get("output_ref") if node is not None else None
        if output_ref is None or str(output_ref) not in consumed_refs:
            terminal.append(output)
    # Match the deterministic structured renderer: when the graph proves a
    # terminal output, upstream aggregates are proof intermediates.  If graph
    # references are incomplete, preserve every declared output instead.
    return terminal or values


def _terminalize_outputs(
    plan: object,
    outputs: Sequence[Mapping[str, Any]],
) -> list[Mapping[str, Any]]:
    branch_terminals = [
        _terminal_outputs_for_intent(_intent_from_branch(branch))
        for branch in _branch_values(plan)
    ]
    branch_terminals = [values for values in branch_terminals if values]
    if not branch_terminals:
        return list(outputs)
    signatures = [
        [
            json.dumps(
                _compact_output(output),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            for output in values
        ]
        for values in branch_terminals
    ]
    if any(signature != signatures[0] for signature in signatures[1:]):
        return list(outputs)
    terminal_ids = {
        output.get("output_id")
        for output in branch_terminals[0]
        if output.get("output_id") is not None
    }
    if terminal_ids:
        filtered = [output for output in outputs if output.get("output_id") in terminal_ids]
        if filtered:
            return filtered
    return list(branch_terminals[0])


def _compact_branch(value: Any) -> dict[str, Any]:
    branch = _mapping(value)
    result = _selected(branch, ("branch_id", "status", "reason_codes"))
    intent = _intent_from_branch(branch)
    compact_intent = _compact_requested(intent)
    if compact_intent:
        result["intent"] = compact_intent
    return result


def _compact_authoritative_contract(value: Any) -> dict[str, Any]:
    """Compact either a QIC or GraphPlan.compact_contract representation."""

    contract = _mapping(value)
    requested = _requested_from_contract(contract)
    if requested:
        result = {"requested": _compact_requested(requested)}
        for key in ("not_requested", "ambiguity", "forbidden"):
            if key in contract:
                result[key] = _bounded_value(contract[key])
        return result

    result = _selected(
        contract,
        (
            "contract_version",
            "question_id",
            "mode",
            "strict_status",
            "strict_reasons",
            "render_policy",
        ),
    )
    common_outputs = contract.get("common_requested_outputs")
    if isinstance(common_outputs, Sequence) and not isinstance(
        common_outputs, (str, bytes, bytearray)
    ):
        result["common_requested_outputs"] = [
            _compact_output(output)
            for output in common_outputs[:16]
            if isinstance(output, Mapping)
        ]
    raw_branches = contract.get("branches")
    if isinstance(raw_branches, Sequence) and not isinstance(
        raw_branches, (str, bytes, bytearray)
    ):
        result["branches"] = [
            _compact_branch(branch)
            for branch in raw_branches[:8]
            if isinstance(branch, Mapping)
        ]
    for key in ("not_requested", "ambiguity", "forbidden"):
        if key in contract:
            result[key] = _bounded_value(contract[key])
    return result


def _compact_retrieval_query(value: Any) -> dict[str, Any]:
    return {
        key: _bounded_value(field)
        for key in (
            "branch_id",
            "query_text",
            "coverage_requirement",
            "required_terms",
            "optional_terms",
        )
        if (field := _get(value, key)) is not None
    }


def _compact_graph_plan(plan: object) -> dict[str, Any]:
    """Return the graph facts useful to answer generation, without graph bulk."""

    if plan is None:
        raise ValueError("graph_plan is required")
    metadata_keys = (
        "question_id",
        "qur_id",
        "qic_id",
        "qur_final_status",
        "strict_status",
        "strict_reasons",
        "advisory_usable",
        "fallback_used",
        "qur_sha256",
    )
    result = {
        key: _bounded_value(value)
        for key in metadata_keys
        if (value := _get(plan, key)) is not None
    }

    contract_value = _get(plan, "compact_contract")
    if contract_value is None:
        contract_value = _get(plan, "question_intent_contract")
    if contract_value is None:
        contract_value = _get(plan, "contract")
    compact_contract = _compact_authoritative_contract(contract_value)
    if not compact_contract:
        compact_contract = _compact_authoritative_contract(_get(plan, "requested"))
    if compact_contract:
        result["contract"] = compact_contract

    retrieval_queries = _get(plan, "retrieval_queries")
    if isinstance(retrieval_queries, Sequence) and not isinstance(
        retrieval_queries, (str, bytes, bytearray)
    ):
        result["retrieval_queries"] = [
            _compact_retrieval_query(value) for value in retrieval_queries[:8]
        ]

    branches = [_compact_branch(value) for value in _branch_values(plan)[:8]]
    branches = [branch for branch in branches if branch]
    if branches:
        result["branches"] = branches

    outputs = _output_contracts(plan)
    if outputs:
        result["terminal_requested_outputs"] = [
            _compact_output(output) for output in outputs
        ]
    return result


def _render_graph_plan(plan: object) -> str:
    compact = _compact_graph_plan(plan)
    rendered = json.dumps(
        compact,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    if len(rendered) <= MAX_GRAPH_PLAN_CHARS:
        return rendered

    # Preserve the authoritative contract and output shape first.  Branch detail
    # is useful, but it must not crowd the source context out of the prompt.
    reduced = {
        key: value
        for key, value in compact.items()
        if key not in {"branches", "ambiguity", "not_requested", "retrieval_queries"}
    }
    reduced_contract = dict(_mapping(reduced.get("contract")))
    if reduced_contract:
        for key in ("ambiguity", "forbidden", "not_requested"):
            reduced_contract.pop(key, None)
        if isinstance(reduced_contract.get("branches"), list):
            reduced_contract["branches"] = reduced_contract["branches"][:2]
        reduced["contract"] = reduced_contract
    reduced["graph_plan_truncated"] = True
    rendered = json.dumps(
        reduced,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    if len(rendered) <= MAX_GRAPH_PLAN_CHARS:
        return rendered

    contract = _mapping(reduced.get("contract"))
    requested_contract = _mapping(contract.get("requested"))
    if requested_contract:
        minimal_contract = {
            key: requested_contract[key]
            for key in ("target", "scope", "derived_summary", "requested_outputs")
            if key in requested_contract
        }
    else:
        minimal_contract = {
            key: contract[key]
            for key in (
                "contract_version",
                "mode",
                "strict_status",
                "strict_reasons",
                "common_requested_outputs",
                "render_policy",
            )
            if key in contract
        }
        if isinstance(contract.get("branches"), list) and contract["branches"]:
            minimal_contract["branches"] = contract["branches"][:1]
    minimal = {
        "graph_plan_truncated": True,
        "status": _selected(
            reduced,
            ("qur_final_status", "strict_status", "strict_reasons", "advisory_usable"),
        ),
        "contract": minimal_contract,
        "terminal_requested_outputs": reduced.get(
            "terminal_requested_outputs", []
        ),
    }
    rendered = json.dumps(
        minimal,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    if len(rendered) <= MAX_GRAPH_PLAN_CHARS:
        return rendered
    # Emergency form remains valid JSON.  The answer-shape list is already
    # bounded, but a hostile or malformed caller could still supply very long
    # unit strings; keep only the first compact output in that case.
    emergency = {
        "graph_plan_truncated": True,
        "status": minimal["status"],
        "terminal_requested_outputs": list(
            minimal["terminal_requested_outputs"]
        )[:1],
    }
    return json.dumps(
        emergency,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _output_contracts(plan: object) -> list[Mapping[str, Any]]:
    """Find the authoritative output contracts across GraphPlan representations."""

    raw_values: list[Mapping[str, Any]] = []
    candidates = [
        _get(plan, "compact_contract"),
        _get(plan, "question_intent_contract"),
        _get(plan, "contract"),
        _get(plan, "requested"),
    ]
    for candidate in candidates:
        compact_contract = _mapping(candidate)
        common_outputs = compact_contract.get("common_requested_outputs")
        if isinstance(common_outputs, Sequence) and not isinstance(
            common_outputs, (str, bytes, bytearray)
        ):
            values = [item for item in common_outputs if isinstance(item, Mapping)]
            if values:
                raw_values = values
                break
        requested = _requested_from_contract(candidate)
        outputs = requested.get("requested_outputs") if requested else None
        if isinstance(outputs, Sequence) and not isinstance(
            outputs, (str, bytes, bytearray)
        ):
            values = [item for item in outputs if isinstance(item, Mapping)]
            if values:
                raw_values = values
                break

    if raw_values:
        return _terminalize_outputs(plan, raw_values)

    standalone = _get(plan, "output_shape")
    if isinstance(standalone, Mapping):
        if "answer_shape" in standalone or "return_field" in standalone:
            return _terminalize_outputs(plan, [standalone])
        return _terminalize_outputs(plan, [{"answer_shape": standalone}])
    if isinstance(standalone, Sequence) and not isinstance(
        standalone, (str, bytes, bytearray)
    ):
        values = []
        for item in standalone:
            if not isinstance(item, Mapping):
                continue
            values.append(
                item
                if "answer_shape" in item or "return_field" in item
                else {"answer_shape": item}
            )
        if values:
            return _terminalize_outputs(plan, values)

    unique: dict[str, Mapping[str, Any]] = {}
    for branch in _branch_values(plan):
        for output in _terminal_outputs_for_intent(_intent_from_branch(branch)):
            if not isinstance(output, Mapping):
                continue
            key = json.dumps(
                _compact_output(output),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            unique[key] = output
    return list(unique.values())


_UNKNOWN_ANSWER = re.compile(r"(?:わかりません|わからない|不明)[。．.]*\Z")
_INTEGER_ANSWER = re.compile(r"[+-]?(?:\d{1,3}(?:,\d{3})*|\d+)(?:\s*[^\d\s、,]+)?\Z")
_NUMBER_ANSWER = re.compile(
    r"(?:約\s*)?[+-]?(?:\d{1,3}(?:,\d{3})*|\d+)(?:\.\d+)?(?:\s*[^\d\s、,]+)?\Z"
)
_IDENTIFIER_COMPONENT = r"(?=[\w./:+-]*\w)[\w./:+-]+"
_IDENTIFIER_ANSWER = re.compile(
    rf"{_IDENTIFIER_COMPONENT}(?: {_IDENTIFIER_COMPONENT})?",
    flags=re.UNICODE,
)


def _list_items(text: str) -> list[str]:
    """Split answer-only list syntax without breaking numeric group commas."""

    # An ASCII comma is a separator whenever at least one adjacent character
    # is non-numeric.  The older both-sides guard failed on identifiers ending
    # in digits (for example ``AI-05, AI-09``), while this still preserves a
    # grouped number such as ``1,234`` as one item.
    return [
        value.strip()
        for value in re.split(r"、|;|(?<!\d),|,(?!\d)", text)
        if value.strip()
    ]


def _is_identifier_answer(text: str) -> bool:
    """Accept compact source identifiers, including one internal space."""

    # Source-defined identifiers can be multiword column labels such as
    # ``ZIP CODE``.  Keep the allowance narrow: at most two non-empty compact
    # components, one literal internal space, and at least one word character
    # in each component.  This excludes headings, sentences, repeated spaces,
    # and punctuation-only values while retaining the existing punctuation set.
    return _IDENTIFIER_ANSWER.fullmatch(text) is not None


def _shape_violations(text: str, outputs: Sequence[Mapping[str, Any]]) -> list[str]:
    """Check only deterministic surface constraints; never judge answer truth here."""

    answer = normalize_answer(text)
    if _UNKNOWN_ANSWER.fullmatch(answer):
        return []
    violations: list[str] = []
    if re.match(r"\s*(?:[-*#>]\s+|回答\s*[:：])", answer):
        violations.append("answer_only_no_heading")

    # A multi-output answer is a composite by definition.  Do not flatten it
    # before examining the individual contracts; doing so can erase requested
    # before/after or facet pairs.  Likewise, an explicit unknown container is
    # not evidence for a single-line shape.
    if len(outputs) != 1:
        return list(dict.fromkeys(violations))
    output = outputs[0]
    shape = _mapping(output.get("answer_shape"))
    cardinality = _mapping(output.get("cardinality"))
    inference_basis = _mapping(output.get("inference_basis"))
    enforceable = _mapping(inference_basis.get("enforceable"))

    def enforced(key: str) -> bool:
        # Strict QIC/extended contracts predate advisory inference flags and
        # remain fully enforceable.  Generic advisory outputs opt each surface
        # constraint in explicitly.
        return not enforceable or enforceable.get(key) is True

    container = shape.get("container") if enforced("container") else "unknown"
    value_type = shape.get("value_type") if enforced("value_type") else "unknown"
    return_field = output.get("return_field") if enforced("return_field") else "unknown"
    if not enforced("cardinality"):
        cardinality = {}
    normalized = unicodedata.normalize("NFKC", answer).strip()

    if "\n" in answer and container in {"scalar", "list", "yes_no"}:
        violations.append("single_line_required")

    if container == "scalar" and "、" in normalized:
        violations.append("scalar_must_not_be_a_list")
    if container == "list":
        expected_count = cardinality.get("expected_count")
        items = _list_items(normalized)
        if isinstance(expected_count, int) and expected_count > 0:
            if len(items) != expected_count:
                violations.append(f"list_expected_count_{expected_count}")
        if value_type == "identifier" and any(
            not _is_identifier_answer(item) for item in items
        ):
            violations.append("identifier_list_items_required")

    if container == "scalar" and (value_type == "integer" or return_field == "count"):
        if _INTEGER_ANSWER.fullmatch(normalized) is None:
            violations.append("integer_scalar_required")
    elif container == "scalar" and value_type == "number":
        if _NUMBER_ANSWER.fullmatch(normalized) is None:
            violations.append("numeric_scalar_required")
    elif container == "scalar" and value_type == "boolean":
        if normalized.casefold() not in {
            "true",
            "false",
            "yes",
            "no",
            "はい",
            "いいえ",
            "該当する",
            "該当しない",
        }:
            violations.append("boolean_scalar_required")
    elif container == "scalar" and value_type == "identifier":
        if not _is_identifier_answer(normalized):
            violations.append("identifier_scalar_required")
    elif container == "key_value":
        required_keys = output.get("required_keys")
        if isinstance(required_keys, Sequence) and not isinstance(
            required_keys, (str, bytes, bytearray)
        ):
            for key in required_keys:
                if not isinstance(key, str) or not key:
                    continue
                match = re.search(
                    rf"(?<![\w]){re.escape(key)}\s*[:=：]\s*"
                    rf"([+-]?\d+(?:\.\d+)?)",
                    normalized,
                    flags=re.UNICODE,
                )
                if match is None:
                    violations.append(f"key_value_required:{key}")

    unit = shape.get("unit") if enforced("unit") else None
    if isinstance(unit, str) and unit and unit not in answer:
        violations.append(f"unit_required:{unit[:40]}")
    display_precision = (
        _mapping(output.get("display_precision"))
        if enforced("display_precision")
        else {}
    )
    if display_precision.get("mode") == "decimal_places" and isinstance(
        display_precision.get("digits"), int
    ):
        digits = display_precision["digits"]
        numeric = re.search(r"[+-]?\d[\d,]*(?:\.(\d+))?", normalized)
        actual_digits = len(numeric.group(1) or "") if numeric else None
        if actual_digits != digits:
            violations.append(f"decimal_places_required:{digits}")
    return list(dict.fromkeys(violations))


def answer_question(client: AnswerClient, question: str, chunks, glossary=None) -> str:
    chunk_values = tuple(chunks)
    q = _expand_question(question, glossary)
    rendering_candidates = _output_rendering_candidates(
        question, chunk_values, glossary
    )
    user = USER_TEMPLATE.format(
        context=build_context(chunk_values),
        output_rendering_guidance=_output_rendering_guidance(
            rendering_candidates
        ),
        facet_guidance=_facet_guidance(question),
        question=q,
    )
    text = client.generate([
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": user},
    ])
    normalized = normalize_answer(text)
    return _render_primary_aliases(
        normalized, question, chunk_values, glossary
    )


def validate_graph_answer(answer: str, graph_plan: object) -> tuple[str, ...]:
    """Return deterministic terminal-output contract violations.

    This public boundary is shared by deterministic structured execution and
    LLM generation so neither route can bypass the graph's answer shape.
    """

    normalized = normalize_answer(answer)
    return tuple(_shape_violations(normalized, _output_contracts(graph_plan)))


def answer_question_with_graph(
    client: AnswerClient,
    question: str,
    chunks,
    graph_plan: object,
    glossary=None,
) -> str:
    return answer_question_with_graph_result(
        client,
        question,
        chunks,
        graph_plan,
        glossary,
    ).answer


def answer_question_with_graph_result(
    client: AnswerClient,
    question: str,
    chunks,
    graph_plan: object,
    glossary=None,
) -> GraphAnswerResult:
    """Answer with a graph-derived intent contract and one bounded format repair.

    The graph constrains interpretation and output shape.  It is deliberately
    presented separately from retrieved source text so it can never serve as
    evidence for an answer value.
    """

    chunk_values = tuple(chunks)
    q = _expand_question(question, glossary)
    outputs = _output_contracts(graph_plan)
    rendering_candidates = _output_rendering_candidates(
        question, chunk_values, glossary
    )
    user = GRAPH_USER_TEMPLATE.format(
        graph_plan=_render_graph_plan(graph_plan),
        context=build_context(chunk_values),
        output_rendering_guidance=_output_rendering_guidance(
            rendering_candidates
        ),
        facet_guidance=_facet_guidance(question),
        question=q,
    )
    messages = [
        {"role": "system", "content": GRAPH_SYSTEM},
        {"role": "user", "content": user},
    ]
    initial_raw = client.generate(messages)
    initial = _render_primary_aliases(
        normalize_answer(initial_raw), question, chunk_values, glossary
    )
    violations = _shape_violations(initial, outputs)
    if not violations:
        return GraphAnswerResult(initial, "pass", (), 1)

    repair_messages = [
        *messages,
        {"role": "assistant", "content": initial},
        {
            "role": "user",
            "content": GRAPH_REPAIR_TEMPLATE.format(
                violations="\n".join(f"- {value}" for value in violations)
            ),
        },
    ]
    try:
        repaired_raw = client.generate(repair_messages)
    except Exception:
        return GraphAnswerResult(
            "わかりません",
            "fail",
            tuple(violations),
            2,
        )
    if not str(repaired_raw).strip():
        return GraphAnswerResult(
            "わかりません",
            "fail",
            tuple(violations),
            2,
        )
    repaired = _render_primary_aliases(
        normalize_answer(repaired_raw), question, chunk_values, glossary
    )
    remaining = _shape_violations(repaired, outputs)
    if remaining:
        return GraphAnswerResult(
            "わかりません",
            "fail",
            tuple(remaining),
            2,
        )
    return GraphAnswerResult(repaired, "repaired", (), 2)


def normalize_answer(text: str) -> str:
    """Apply question-independent plain-text normalization to one answer."""
    # Source documents can contain presentation/export markup.  Keep answer
    # cells plain text without changing their semantic content.
    text = re.sub(r"</?[A-Za-z][^>]{0,200}>", "", text).strip()
    if re.fullmatch(r"(?:わかりません|わからない|不明)[。．.]*", text):
        text = "わかりません"
    return text or "わかりません"
