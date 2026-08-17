"""Fail-closed resolution of a best-model parameter from analysis artifacts.

The rule is deliberately independent from retrieval and answer generation.  A
question supplies only a project scope and an exact parameter identifier.  The
answer is resolved only when one current final report and one complete set of
machine-readable analysis artifacts agree on a unique champion and on the
selected model parameters.  Source-specific names, parameter names, values,
question IDs, reference answers, and predictions are not part of the grammar.
"""

from __future__ import annotations

import ast
import csv
import hashlib
import io
import json
import re
import unicodedata
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN
from pathlib import Path
from typing import Any, Mapping, Sequence

from structured_candidate import (
    StructuredCandidateAnswer,
    StructuredCandidateDecision,
    _candidate_values,
    _location_matches,
)


ANALYSIS_ARTIFACT_RULE_VERSION = "0.1"

BEST_MODEL_PARAMETER = re.compile(
    r"^(?P<location>[^\r\n/\\]{1,160}?)の"
    r"(?P<report>最終報告(?:書)?)"
    r"(?:にて|において)、?"
    r"(?:(?:最良モデル|最も精度が高いモデル)としているモデル|最良モデル)の"
    r"パラメータである"
    r"(?P<parameter>[A-Za-z_][A-Za-z0-9_]*)"
    r"は(?:いくら|何)に設定されていますか[。．]?$"
)

_ARCHIVE_ENGLISH = re.compile(
    r"(?:^|[._\-\s])(?:old|draft|copy|backup|bak|archive|archived|obsolete|tmp)"
    r"(?:[._\-\s]*[0-9]+)?(?:$|[._\-\s])",
    flags=re.IGNORECASE,
)
_ARCHIVE_JAPANESE = (
    "旧",
    "過去",
    "草案",
    "ドラフト",
    "コピー",
    "写し",
    "バックアップ",
    "アーカイブ",
    "改訂前",
)

_MINIMIZE_METRICS = frozenset(
    {
        "rmse",
        "mae",
        "mse",
        "mape",
        "smape",
        "log_loss",
        "brier_score",
        "brier_score_loss",
        "error",
        "loss",
    }
)
_MAXIMIZE_METRICS = frozenset(
    {
        "r2",
        "accuracy",
        "f1",
        "f1_macro",
        "precision",
        "recall",
        "auc",
        "auc_roc",
        "roc_auc",
        "average_precision",
    }
)
_DOMAIN_FIELDS = ("task_type", "primary_metric", "split_strategy", "test_size")
_REQUIRED_LEADERBOARD_FIELDS = frozenset(
    {
        "status",
        "model_type",
        "transform_target",
        "task_type",
        "primary_metric",
        "primary_value",
        "split_strategy",
        "test_size",
    }
)
_MAX_JSON_BYTES = 32 * 1024 * 1024
_MAX_CSV_BYTES = 64 * 1024 * 1024
_MAX_CODE_BYTES = 16 * 1024 * 1024
_MAX_LEADERBOARD_ROWS = 1_000_000


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _strict_json_equal(left: Any, right: Any) -> bool:
    """Compare decoded JSON values without Python's numeric type coercion."""
    if type(left) is not type(right):
        return False
    if isinstance(left, Mapping):
        return left.keys() == right.keys() and all(
            _strict_json_equal(left[key], right[key]) for key in left
        )
    if isinstance(left, list):
        return len(left) == len(right) and all(
            _strict_json_equal(left_item, right_item)
            for left_item, right_item in zip(left, right)
        )
    return left == right


def _normalized(value: object) -> str:
    return unicodedata.normalize("NFKC", str(value)).casefold().strip()


def _metric_key(value: object) -> str:
    normalized = _normalized(value)
    return re.sub(r"[\s.\-/]+", "_", normalized).strip("_")


def _is_archived_component(value: str) -> bool:
    normalized = unicodedata.normalize("NFKC", value).casefold().strip()
    return bool(_ARCHIVE_ENGLISH.search(normalized)) or any(
        marker in normalized for marker in _ARCHIVE_JAPANESE
    )


def _safe_root(engine: Any) -> Path | None:
    try:
        raw = Path(engine.source_root)
        if not raw.is_dir() or raw.is_symlink():
            return None
        return raw.resolve()
    except (AttributeError, OSError, RuntimeError, TypeError):
        return None


def _has_symlink_component(path: Path, root: Path) -> bool:
    current = path
    while True:
        if current.is_symlink():
            return True
        if current == root:
            return False
        if root not in current.parents:
            return True
        current = current.parent


def _project_roots(engine: Any, location: str) -> tuple[Path, ...]:
    root = _safe_root(engine)
    if root is None:
        return ()
    candidates = _candidate_values(location, getattr(engine, "glossary", None))
    possible = [root]
    try:
        possible.extend(path for path in root.rglob("*") if path.is_dir())
    except OSError:
        return ()
    matches: list[Path] = []
    for path in possible:
        if _has_symlink_component(path, root):
            continue
        if not _location_matches((path.name,), candidates):
            continue
        try:
            children = {child.name for child in path.iterdir() if child.is_dir()}
        except OSError:
            continue
        if not any("分析" in _normalized(value) for value in children):
            continue
        if not any("報告" in _normalized(value) for value in children):
            continue
        matches.append(path.resolve())
    return tuple(sorted(set(matches), key=lambda item: item.as_posix()))


def _safe_project_files(project: Path) -> tuple[Path, ...]:
    files: list[Path] = []
    try:
        for path in project.rglob("*"):
            if (
                not path.is_file()
                or path.name.startswith(("~$", "."))
                or _has_symlink_component(path, project)
            ):
                continue
            relative = path.relative_to(project)
            if any(_is_archived_component(part) for part in relative.parts):
                continue
            files.append(path)
    except OSError:
        return ()
    return tuple(sorted(files, key=lambda item: item.as_posix()))


def _current_final_reports(project: Path, files: Sequence[Path]) -> tuple[Path, ...]:
    matches: list[Path] = []
    for path in files:
        if path.suffix.casefold() not in {".pdf", ".pptx"}:
            continue
        relative = path.relative_to(project)
        if not any("報告" in _normalized(part) for part in relative.parts[:-1]):
            continue
        stem = _normalized(path.stem)
        if "最終" not in stem or "報告" not in stem:
            continue
        try:
            if path.stat().st_size <= 0:
                continue
        except OSError:
            continue
        matches.append(path)
    return tuple(matches)


def _artifact_matches(
    project: Path,
    files: Sequence[Path],
    *,
    name: str,
    required_parts: Sequence[str],
    forbidden_parts: Sequence[str] = (),
) -> tuple[Path, ...]:
    matches: list[Path] = []
    for path in files:
        if path.name != name:
            continue
        parts = tuple(_normalized(part) for part in path.relative_to(project).parts[:-1])
        if not all(any(_normalized(required) == part for part in parts) for required in required_parts):
            continue
        if any(any(_normalized(forbidden) == part for part in parts) for forbidden in forbidden_parts):
            continue
        matches.append(path)
    return tuple(matches)


def _artifact_set(project: Path, files: Sequence[Path]) -> dict[str, Path] | None:
    definitions = {
        "leaderboard": {
            "name": "leaderboard.csv",
            "required_parts": ("analysis_outputs", "experiments"),
        },
        "metrics": {
            "name": "metrics.json",
            "required_parts": ("analysis_outputs",),
            "forbidden_parts": ("analysis_project",),
        },
        "run_summary": {
            "name": "run_summary.json",
            "required_parts": ("analysis_outputs",),
            "forbidden_parts": ("analysis_project",),
        },
        "project_config": {
            "name": "project_config.json",
            "required_parts": ("analysis_project", "configs"),
        },
        "modeling": {
            "name": "modeling.py",
            "required_parts": ("analysis_project", "src"),
        },
    }
    result: dict[str, Path] = {}
    for role, definition in definitions.items():
        matches = _artifact_matches(project, files, **definition)
        if len(matches) != 1:
            return None
        result[role] = matches[0]
    return result


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant is forbidden: {value}")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _read_json_object(path: Path) -> dict[str, Any]:
    raw = path.read_bytes()
    if not raw or len(raw) > _MAX_JSON_BYTES or b"\x00" in raw:
        raise ValueError("JSON source size or content is invalid")
    value = json.loads(
        raw.decode("utf-8-sig"),
        parse_float=Decimal,
        parse_constant=_reject_constant,
        object_pairs_hook=_unique_object,
    )
    if not isinstance(value, dict):
        raise ValueError("JSON source is not an object")
    return value


def _read_leaderboard(path: Path) -> tuple[dict[str, str], ...]:
    raw = path.read_bytes()
    if not raw or len(raw) > _MAX_CSV_BYTES or b"\x00" in raw:
        raise ValueError("leaderboard size or content is invalid")
    text: str | None = None
    for encoding in ("utf-8-sig", "utf-8", "cp932"):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        raise ValueError("leaderboard encoding is unsupported")
    reader = csv.DictReader(io.StringIO(text, newline=""))
    fields = reader.fieldnames
    if (
        not fields
        or len(fields) != len(set(fields))
        or not _REQUIRED_LEADERBOARD_FIELDS.issubset(fields)
    ):
        raise ValueError("leaderboard header is incomplete or ambiguous")
    rows: list[dict[str, str]] = []
    for row in reader:
        if None in row or any(value is None for value in row.values()):
            raise ValueError("leaderboard row has inconsistent arity")
        rows.append({key: str(value).strip() for key, value in row.items()})
        if len(rows) > _MAX_LEADERBOARD_ROWS:
            raise ValueError("leaderboard row limit exceeded")
    if not rows:
        raise ValueError("leaderboard is empty")
    return tuple(rows)


def _finite_decimal(value: object) -> Decimal:
    if isinstance(value, bool):
        raise ValueError("boolean is not a metric")
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("metric is not numeric") from exc
    if not result.is_finite():
        raise ValueError("metric is not finite")
    return result


def _metric_direction(metric: str) -> str | None:
    key = _metric_key(metric)
    if key in _MINIMIZE_METRICS:
        return "minimize"
    if key in _MAXIMIZE_METRICS:
        return "maximize"
    return None


def _domain_value(field: str, value: str) -> object:
    if field == "test_size":
        return _finite_decimal(value)
    if field == "primary_metric":
        return _metric_key(value)
    return _normalized(value)


def _unique_champion(rows: Sequence[Mapping[str, str]]) -> tuple[Mapping[str, str], str] | None:
    eligible = [row for row in rows if _normalized(row.get("status", "")) == "ok"]
    if not eligible:
        return None
    domains: set[tuple[object, ...]] = set()
    scores: list[Decimal] = []
    for row in eligible:
        if any(not str(row.get(field, "")).strip() for field in _DOMAIN_FIELDS):
            return None
        if not str(row.get("model_type", "")).strip() or not str(
            row.get("transform_target", "")
        ).strip():
            return None
        try:
            domains.add(tuple(_domain_value(field, row[field]) for field in _DOMAIN_FIELDS))
            scores.append(_finite_decimal(row["primary_value"]))
        except (KeyError, ValueError):
            return None
    if len(domains) != 1:
        return None
    metric = str(eligible[0]["primary_metric"])
    direction = _metric_direction(metric)
    if direction is None:
        return None
    best = min(scores) if direction == "minimize" else max(scores)
    winners = [row for row, score in zip(eligible, scores) if score == best]
    if len(winners) != 1:
        return None
    return winners[0], direction


def _matches_declared_decimal(actual: object, declared: str) -> bool:
    try:
        expected = _finite_decimal(declared)
        observed = _finite_decimal(actual)
    except ValueError:
        return False
    exponent = expected.as_tuple().exponent
    quantum = Decimal(1).scaleb(exponent) if exponent < 0 else Decimal(1)
    try:
        return observed.quantize(quantum, rounding=ROUND_HALF_EVEN) == expected
    except InvalidOperation:
        return False


def _same_text(left: object, right: object) -> bool:
    return isinstance(left, str) and isinstance(right, str) and _normalized(left) == _normalized(right)


def _selected_run_matches(
    champion: Mapping[str, str],
    metrics: Mapping[str, Any],
    config: Mapping[str, Any],
    summary: Mapping[str, Any],
) -> bool:
    model_type = champion.get("model_type")
    transform = champion.get("transform_target")
    if not model_type or not transform:
        return False
    for source in (metrics, config, summary):
        if not _same_text(source.get("model_type"), model_type):
            return False
        if not _same_text(source.get("transform_target"), transform):
            return False
    for field in ("task_type", "split_strategy"):
        declared = champion.get(field)
        if not declared or not _same_text(metrics.get(field), declared):
            return False
    metric = _metric_key(champion.get("primary_metric", ""))
    if not metric or metric not in metrics:
        return False
    if not _matches_declared_decimal(metrics[metric], champion.get("primary_value", "")):
        return False
    secondary_metric = str(champion.get("secondary_metric", "")).strip()
    secondary_value = str(champion.get("secondary_value", "")).strip()
    if bool(secondary_metric) != bool(secondary_value):
        return False
    if secondary_metric:
        secondary_key = _metric_key(secondary_metric)
        if secondary_key not in metrics or not _matches_declared_decimal(
            metrics[secondary_key], secondary_value
        ):
            return False
    return True


def _consensus_parameter(
    parameter: str,
    config: Mapping[str, Any],
    metrics: Mapping[str, Any],
    summary: Mapping[str, Any],
) -> int | None:
    parameter_sets = [source.get("model_params") for source in (config, metrics, summary)]
    if not all(isinstance(value, Mapping) for value in parameter_sets):
        return None
    first = parameter_sets[0]
    # Recursive equality supports Decimal values produced by the strict JSON
    # loader while preserving int/Decimal/bool distinctions.
    if any(not _strict_json_equal(first, value) for value in parameter_sets[1:]):
        return None
    if any(parameter not in value for value in parameter_sets):
        return None
    values = [value[parameter] for value in parameter_sets]
    if any(type(value) is not int for value in values):
        return None
    return values[0]


def _call_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _camel_to_snake(value: str) -> str:
    first = re.sub(r"(.)([A-Z][a-z]+)", r"\1_\2", value)
    return re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", first).casefold()


def _constructor_identity(name: str) -> tuple[str, str | None]:
    for suffix, task in (("Regressor", "regression"), ("Classifier", "classification")):
        if name.endswith(suffix):
            return _camel_to_snake(name[: -len(suffix)]), task
    return _camel_to_snake(name), None


def _exact_parameter_source(expression: ast.AST, parameter: str) -> bool:
    if isinstance(expression, ast.Subscript):
        return (
            isinstance(expression.value, ast.Name)
            and expression.value.id == "model_params"
            and isinstance(expression.slice, ast.Constant)
            and expression.slice.value == parameter
        )
    if isinstance(expression, ast.Call):
        if (
            isinstance(expression.func, ast.Attribute)
            and expression.func.attr == "get"
            and isinstance(expression.func.value, ast.Name)
            and expression.func.value.id == "model_params"
            and expression.args
            and isinstance(expression.args[0], ast.Constant)
            and expression.args[0].value == parameter
        ):
            return True
        wrapper = _call_name(expression.func)
        if wrapper in {"int", "to_int"}:
            return bool(expression.args) and _exact_parameter_source(expression.args[0], parameter)
    return False


def _assigned_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
        return node.targets[0].id
    if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
        return node.target.id
    return None


def _assignment_value(node: ast.AST) -> ast.AST | None:
    if isinstance(node, ast.Assign):
        return node.value
    if isinstance(node, ast.AnnAssign):
        return node.value
    return None


def _condition_is_false_for_value(test: ast.AST, name: str, value: int) -> bool:
    if not isinstance(test, ast.Compare) or not isinstance(test.left, ast.Name) or test.left.id != name:
        return False
    if len(test.ops) != 1 or len(test.comparators) != 1:
        return False
    comparator = test.comparators[0]
    try:
        right = ast.literal_eval(comparator)
    except Exception:
        return False
    operator = test.ops[0]
    if isinstance(operator, ast.In):
        try:
            return value not in right
        except TypeError:
            return False
    if isinstance(operator, (ast.Eq, ast.Is)):
        return value != right
    if isinstance(operator, (ast.NotEq, ast.IsNot)):
        return value == right
    return False


def _enclosing_function(tree: ast.AST, target: ast.AST) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if any(child is target for child in ast.walk(node)):
            return node
    return None


def _parent_map(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    return {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}


def _ignored_assignment(
    assignment: ast.AST,
    name: str,
    value: int,
    parents: Mapping[ast.AST, ast.AST],
) -> bool:
    current = assignment
    while current in parents:
        parent = parents[current]
        if isinstance(parent, ast.If):
            if current in parent.body and _condition_is_false_for_value(parent.test, name, value):
                return True
            return False
        current = parent
    return False


def _name_flows_from_parameter(
    function: ast.FunctionDef | ast.AsyncFunctionDef,
    name: str,
    parameter: str,
    parameter_value: int,
    call: ast.Call,
    parents: Mapping[ast.AST, ast.AST],
) -> bool:
    assignments = [
        node
        for node in ast.walk(function)
        if _assigned_name(node) == name and getattr(node, "lineno", 0) < getattr(call, "lineno", 0)
    ]
    assignments.sort(key=lambda item: (getattr(item, "lineno", 0), getattr(item, "col_offset", 0)))
    source_seen = False
    for assignment in assignments:
        expression = _assignment_value(assignment)
        if expression is not None and _exact_parameter_source(expression, parameter):
            source_seen = True
            continue
        if source_seen and not _ignored_assignment(
            assignment, name, parameter_value, parents
        ):
            return False
    return source_seen


def _modeling_propagates_parameter(
    path: Path,
    model_type: str,
    task_type: str,
    parameter: str,
    parameter_value: int,
) -> bool:
    raw = path.read_bytes()
    if not raw or len(raw) > _MAX_CODE_BYTES or b"\x00" in raw:
        return False
    try:
        tree = ast.parse(raw.decode("utf-8-sig"))
    except (SyntaxError, UnicodeDecodeError):
        return False
    expected_model = _metric_key(model_type)
    expected_task = _normalized(task_type)
    candidates: list[ast.Call] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _call_name(node.func)
        if name is None:
            continue
        model, task = _constructor_identity(name)
        if model == expected_model and task == expected_task:
            candidates.append(node)
    if len(candidates) != 1:
        return False
    call = candidates[0]
    keywords = [keyword for keyword in call.keywords if keyword.arg == parameter]
    if len(keywords) != 1:
        return False
    expression = keywords[0].value
    if _exact_parameter_source(expression, parameter):
        return True
    if not isinstance(expression, ast.Name):
        return False
    function = _enclosing_function(tree, call)
    if function is None:
        return False
    return _name_flows_from_parameter(
        function,
        expression.id,
        parameter,
        parameter_value,
        call,
        _parent_map(tree),
    )


def graph_contract_for_question(question: str) -> dict[str, Any] | None:
    """Compile one complete best-model-parameter question into a typed graph."""

    if not isinstance(question, str):
        return None
    match = BEST_MODEL_PARAMETER.fullmatch(question)
    if match is None:
        return None
    bindings = {
        "location": match["location"],
        "parameter": match["parameter"],
        "report": match["report"],
    }
    operators = (
        "retrieve",
        "select_unique_current_final_report",
        "bind_unique_analysis_artifacts",
        "filter_comparable_ok_trials",
        "resolve_metric_direction",
        "select_unique_champion",
        "verify_selected_run",
        "verify_model_params_consensus",
        "project_nested_parameter_exact",
        "verify_constructor_parameter_flow",
    )
    nodes: list[dict[str, Any]] = []
    previous = "input_question"
    for index, operator in enumerate(operators, 1):
        output_ref = f"value_{index:03d}"
        nodes.append(
            {
                "operation_id": f"op_{index:03d}_{operator}",
                "operator": operator,
                "input_refs": [previous],
                "output_ref": output_ref,
            }
        )
        previous = output_ref
    core = {
        "graph_rule_version": ANALYSIS_ARTIFACT_RULE_VERSION,
        "rule_id": "analysis_best_model_parameter",
        "question_sha256": hashlib.sha256(question.encode("utf-8")).hexdigest(),
        "bindings": bindings,
        "scope": {
            "location": bindings["location"],
            "container": "06.報告書/*最終報告*.{pdf,pptx}",
            "document_kind": bindings["report"],
            "version_state": "current_unique",
            "excluded_version_states": ["old", "draft", "copy", "backup", "archive"],
            "artifact_roles": {
                "leaderboard": "analysis_outputs/experiments/leaderboard.csv",
                "metrics": "analysis_outputs/metrics.json",
                "run_summary": "analysis_outputs/run_summary.json",
                "project_config": "analysis_project/configs/project_config.json",
                "modeling": "analysis_project/src/modeling.py",
            },
            "comparison_domain": list(_DOMAIN_FIELDS),
            "selection": "primary_metric_direction_unique_extremum",
            "nested_path": ["model_params", bindings["parameter"]],
            "code_evidence": "python_ast_constructor_keyword_flow",
        },
        "operation_graph": {
            "external_inputs": [
                {
                    "input_ref": "input_question",
                    "input_type": "source_records",
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
                "value_type": "integer",
                "unit": None,
            },
            "display_precision": None,
            "required_keys": [bindings["parameter"]],
        },
    }
    return {
        "graph_contract_id": "analysis_artifact_"
        + hashlib.sha256(_canonical_json(core).encode("utf-8")).hexdigest()[:32],
        **core,
    }


def validate_graph_contract(question: str, contract: Mapping[str, Any]) -> bool:
    """Recompile the graph so caller-supplied source and output claims are rejected."""

    if not isinstance(contract, Mapping):
        return False
    expected = graph_contract_for_question(question)
    if expected is None:
        return False
    try:
        return _canonical_json(expected) == _canonical_json(contract)
    except (TypeError, ValueError):
        return False


def _source_digest(paths: Sequence[Path]) -> str:
    digest = hashlib.sha256()
    for path in sorted(set(paths), key=lambda item: item.as_posix()):
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
    return digest.hexdigest()


def _resolved(
    answer: int,
    paths: Sequence[Path],
    root: Path,
    operation_count: int,
) -> StructuredCandidateDecision:
    relative = tuple(
        unicodedata.normalize("NFC", path.relative_to(root).as_posix())
        for path in sorted(set(paths), key=lambda item: item.as_posix())
    )
    return StructuredCandidateDecision(
        "resolved",
        "certified_analysis_artifact_parameter",
        StructuredCandidateAnswer(
            answer=str(answer),
            source_paths=relative,
            source_sha256=_source_digest(paths),
            operation_count=operation_count,
            output_count=1,
        ),
    )


def decide_question(engine: Any, question: str) -> StructuredCandidateDecision | None:
    """Resolve a matched question or hold when any source invariant is unproven."""

    contract = graph_contract_for_question(question)
    if contract is None:
        return None
    match = BEST_MODEL_PARAMETER.fullmatch(question)
    assert match is not None
    root = _safe_root(engine)
    if root is None:
        return StructuredCandidateDecision("hold", "analysis_source_root_invalid")
    try:
        projects = _project_roots(engine, match["location"])
        if len(projects) != 1:
            return StructuredCandidateDecision("hold", "analysis_project_not_unique")
        project = projects[0]
        files = _safe_project_files(project)
        reports = _current_final_reports(project, files)
        if len(reports) != 1:
            return StructuredCandidateDecision("hold", "current_final_report_not_unique")
        artifacts = _artifact_set(project, files)
        if artifacts is None:
            return StructuredCandidateDecision("hold", "analysis_artifacts_not_unique")

        rows = _read_leaderboard(artifacts["leaderboard"])
        selection = _unique_champion(rows)
        if selection is None:
            return StructuredCandidateDecision("hold", "analysis_champion_not_unique")
        champion, _ = selection
        metrics = _read_json_object(artifacts["metrics"])
        config = _read_json_object(artifacts["project_config"])
        summary = _read_json_object(artifacts["run_summary"])
        if not _selected_run_matches(champion, metrics, config, summary):
            return StructuredCandidateDecision("hold", "selected_run_conflict")
        value = _consensus_parameter(match["parameter"], config, metrics, summary)
        if value is None:
            return StructuredCandidateDecision("hold", "model_parameter_not_exact_integer")
        if not _modeling_propagates_parameter(
            artifacts["modeling"],
            str(metrics["model_type"]),
            str(metrics["task_type"]),
            match["parameter"],
            value,
        ):
            return StructuredCandidateDecision("hold", "model_parameter_not_propagated")
        source_paths = [reports[0], *artifacts.values()]
        return _resolved(
            value,
            source_paths,
            root,
            len(contract["operation_graph"]["nodes"]),
        )
    except (
        OSError,
        UnicodeDecodeError,
        ValueError,
        csv.Error,
        json.JSONDecodeError,
    ):
        return StructuredCandidateDecision("hold", "analysis_source_invalid")


__all__ = [
    "ANALYSIS_ARTIFACT_RULE_VERSION",
    "BEST_MODEL_PARAMETER",
    "decide_question",
    "graph_contract_for_question",
    "validate_graph_contract",
]
