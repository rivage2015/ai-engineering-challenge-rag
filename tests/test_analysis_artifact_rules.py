from __future__ import annotations

import copy
import csv
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
RAG = ROOT / "rag"
if str(RAG) not in sys.path:
    sys.path.insert(0, str(RAG))

from analysis_artifact_rules import (  # noqa: E402
    decide_question,
    graph_contract_for_question,
    validate_graph_contract,
)


PROJECT = "株式会社青潮モビリティサービス"
QUESTION = (
    f"{PROJECT}の最終報告にて最良モデルとしているモデルの"
    "パラメータであるmax_depthはいくらに設定されていますか。"
)

LEADERBOARD_FIELDS = (
    "trial_index",
    "status",
    "model_type",
    "transform_target",
    "task_type",
    "primary_metric",
    "primary_value",
    "secondary_metric",
    "secondary_value",
    "split_strategy",
    "test_size",
)


def engine_for(root: Path) -> SimpleNamespace:
    return SimpleNamespace(
        source_root=root.resolve(),
        glossary=SimpleNamespace(entries={}),
    )


def question_for(project: str, parameter: str) -> str:
    return (
        f"{project}の最終報告にて最良モデルとしているモデルの"
        f"パラメータである{parameter}はいくらに設定されていますか。"
    )


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def write_leaderboard(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=LEADERBOARD_FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def base_rows(
    *,
    primary_metric: str = "rmse",
    primary_values: tuple[str, str] = ("46.98383404", "57.96890778"),
    secondary_metric: str = "r2",
    secondary_values: tuple[str, str] = ("0.84516646", "0.76430065"),
) -> list[dict[str, str]]:
    return [
        {
            "trial_index": "4",
            "status": "ok",
            "model_type": "hist_gradient_boosting",
            "transform_target": "log1p",
            "task_type": "regression",
            "primary_metric": primary_metric,
            "primary_value": primary_values[0],
            "secondary_metric": secondary_metric,
            "secondary_value": secondary_values[0],
            "split_strategy": "time_ordered",
            "test_size": "0.2",
        },
        {
            "trial_index": "9",
            "status": "ok",
            "model_type": "random_forest",
            "transform_target": "none",
            "task_type": "regression",
            "primary_metric": primary_metric,
            "primary_value": primary_values[1],
            "secondary_metric": secondary_metric,
            "secondary_value": secondary_values[1],
            "split_strategy": "time_ordered",
            "test_size": "0.2",
        },
    ]


def modeling_source(parameter: str, *, direct_key: str | None = None) -> str:
    source_key = parameter if direct_key is None else direct_key
    return f'''from sklearn.ensemble import HistGradientBoostingRegressor

def to_int(value, default):
    return int(value) if value is not None else default

def build_pipeline(model_params, task_type):
    model_key = "hist_gradient_boosting"
    {parameter} = model_params.get("{source_key}")
    if {parameter} in ("", "None", "null"):
        {parameter} = None
    if task_type == "regression":
        if model_key == "hist_gradient_boosting":
            return HistGradientBoostingRegressor(
                {parameter}=to_int(model_params.get("{source_key}"), 6)
            )
'''


def write_project(
    root: Path,
    *,
    project_name: str = PROJECT,
    parameter: str = "max_depth",
    parameter_value: object = 6,
    rows: list[dict[str, str]] | None = None,
    metric_value: object = 46.9838340445052,
    secondary_value: object = 0.8451664555487036,
    modeling: str | None = None,
) -> dict[str, Path]:
    project = root / "共有ドライブ" / "プロジェクト" / project_name
    report = project / "06.報告書" / f"{project_name}_最終報告.pdf"
    leaderboard = project / "04.分析" / "analysis_outputs" / "experiments" / "leaderboard.csv"
    metrics = project / "04.分析" / "analysis_outputs" / "metrics.json"
    summary = project / "04.分析" / "analysis_outputs" / "run_summary.json"
    config = project / "04.分析" / "analysis_project" / "configs" / "project_config.json"
    modeling_path = project / "04.分析" / "analysis_project" / "src" / "modeling.py"

    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_bytes(b"%PDF-1.7\nopaque image report\n")
    write_leaderboard(leaderboard, rows or base_rows())
    params = {parameter: parameter_value, "learning_rate": 0.05, "max_iter": 400}
    selected = {
        "model_type": "hist_gradient_boosting",
        "transform_target": "log1p",
        "task_type": "regression",
        "split_strategy": "time_ordered",
        "model_params": params,
    }
    write_json(
        metrics,
        {
            **selected,
            "rmse": metric_value,
            "r2": secondary_value,
        },
    )
    write_json(summary, selected)
    write_json(config, selected)
    modeling_path.parent.mkdir(parents=True, exist_ok=True)
    modeling_path.write_text(
        modeling if modeling is not None else modeling_source(parameter),
        encoding="utf-8",
    )
    return {
        "project": project,
        "report": report,
        "leaderboard": leaderboard,
        "metrics": metrics,
        "summary": summary,
        "config": config,
        "modeling": modeling_path,
    }


def json_value(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


class AnalysisArtifactGraphContractTest(unittest.TestCase):
    def test_full_grammar_builds_graphplan_compatible_contract(self) -> None:
        contract = graph_contract_for_question(QUESTION)
        self.assertIsNotNone(contract)
        assert contract is not None
        self.assertEqual(contract["rule_id"], "analysis_best_model_parameter")
        self.assertEqual(
            contract["bindings"],
            {"location": PROJECT, "parameter": "max_depth", "report": "最終報告"},
        )
        self.assertEqual(
            [node["operator"] for node in contract["operation_graph"]["nodes"]],
            [
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
            ],
        )
        self.assertEqual(
            contract["requested_output"]["answer_shape"],
            {"container": "scalar", "value_type": "integer", "unit": None},
        )
        self.assertTrue(validate_graph_contract(QUESTION, contract))

        tampered = copy.deepcopy(contract)
        tampered["scope"]["nested_path"][-1] = "caller_override"
        self.assertFalse(validate_graph_contract(QUESTION, tampered))
        unserializable = copy.deepcopy(contract)
        unserializable["scope"]["unexpected"] = object()
        self.assertFalse(validate_graph_contract(QUESTION, unserializable))

    def test_full_match_rejects_suffix_and_invalid_identifier(self) -> None:
        self.assertIsNone(graph_contract_for_question(QUESTION + "追記"))
        self.assertIsNone(
            graph_contract_for_question(
                QUESTION.replace("max_depth", "model_params.max_depth")
            )
        )
        self.assertIsNone(graph_contract_for_question("最良モデルを教えてください。"))


class AnalysisArtifactDecisionTest(unittest.TestCase):
    def test_q5_resolves_exact_scalar_integer(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            paths = write_project(root)
            for role in ("config", "metrics", "summary"):
                source = json_value(paths[role])
                self.assertIs(type(source["model_params"]["max_depth"]), int)

            decision = decide_question(engine_for(root), QUESTION)

            self.assertEqual(decision.status, "resolved")
            self.assertEqual(decision.reason, "certified_analysis_artifact_parameter")
            assert decision.result is not None
            self.assertEqual(decision.result.answer, "6")
            self.assertEqual(decision.result.operation_count, 10)
            self.assertEqual(decision.result.output_count, 1)
            self.assertEqual(len(decision.result.source_paths), 6)
            self.assertTrue(
                all(path.relative_to(root).as_posix() in decision.result.source_paths for path in paths.values() if path.name != paths["project"].name)
            )

    def test_opaque_project_parameter_and_value_metamorphose(self) -> None:
        project = "合同会社紫苑交通解析"
        parameter = "leaf_limit"
        question = question_for(project, parameter)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            write_project(
                root,
                project_name=project,
                parameter=parameter,
                parameter_value=17,
            )

            contract = graph_contract_for_question(question)
            decision = decide_question(engine_for(root), question)

            self.assertIsNotNone(contract)
            assert contract is not None
            self.assertEqual(contract["scope"]["nested_path"], ["model_params", parameter])
            self.assertTrue(validate_graph_contract(question, contract))
            self.assertEqual(decision.status, "resolved")
            assert decision.result is not None
            self.assertEqual(decision.result.answer, "17")

    def test_maximize_metric_selects_unique_champion(self) -> None:
        rows = base_rows(
            primary_metric="r2",
            primary_values=("0.84516646", "0.76430065"),
            secondary_metric="rmse",
            secondary_values=("46.98383404", "57.96890778"),
        )
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            write_project(
                root,
                rows=rows,
                metric_value=46.9838340445052,
                secondary_value=0.8451664555487036,
            )
            metrics_path = next(root.rglob("metrics.json"))
            metrics = json_value(metrics_path)
            metrics["r2"] = metrics.pop("r2")
            metrics["rmse"] = metrics.pop("rmse")
            write_json(metrics_path, metrics)

            decision = decide_question(engine_for(root), QUESTION)

            self.assertEqual(decision.status, "resolved")
            assert decision.result is not None
            self.assertEqual(decision.result.answer, "6")

    def test_archived_reports_and_distractor_parameter_are_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            paths = write_project(root)
            archived = paths["project"] / "06.報告書" / "old" / "最終報告_copy.pdf"
            archived.parent.mkdir(parents=True, exist_ok=True)
            archived.write_bytes(b"%PDF archived")
            distractor = paths["project"] / "04.分析" / "analysis_project" / "src" / "limits.py"
            distractor.write_text("max_depth = 64\n", encoding="utf-8")

            decision = decide_question(engine_for(root), QUESTION)

            self.assertEqual(decision.status, "resolved")
            assert decision.result is not None
            self.assertEqual(decision.result.answer, "6")

    def test_duplicate_current_report_holds(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            paths = write_project(root)
            duplicate = paths["project"] / "06.報告書" / "最終報告_v2.pptx"
            duplicate.write_bytes(b"opaque pptx")

            decision = decide_question(engine_for(root), QUESTION)

            self.assertEqual(decision.status, "hold")
            self.assertEqual(decision.reason, "current_final_report_not_unique")

    def test_missing_or_duplicate_artifact_holds(self) -> None:
        cases = ("metrics", "summary", "config", "modeling", "leaderboard")
        for role in cases:
            with self.subTest(role=role), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                paths = write_project(root)
                paths[role].unlink()
                decision = decide_question(engine_for(root), QUESTION)
                self.assertEqual(decision.status, "hold")
                self.assertEqual(decision.reason, "analysis_artifacts_not_unique")

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            paths = write_project(root)
            duplicate = paths["project"] / "04.分析" / "shadow" / "analysis_outputs" / "metrics.json"
            duplicate.parent.mkdir(parents=True, exist_ok=True)
            duplicate.write_bytes(paths["metrics"].read_bytes())
            decision = decide_question(engine_for(root), QUESTION)
            self.assertEqual(decision.status, "hold")
            self.assertEqual(decision.reason, "analysis_artifacts_not_unique")

    def test_unknown_metric_tie_and_mixed_domain_hold(self) -> None:
        cases: dict[str, list[dict[str, str]]] = {}
        unknown = base_rows(primary_metric="opaque_score")
        cases["unknown"] = unknown
        tied = base_rows(primary_values=("46.0", "46.0"))
        cases["tie"] = tied
        mixed = base_rows()
        mixed[1]["split_strategy"] = "random_holdout"
        cases["mixed"] = mixed
        for label, rows in cases.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                write_project(root, rows=rows)
                decision = decide_question(engine_for(root), QUESTION)
                self.assertEqual(decision.status, "hold")
                self.assertEqual(decision.reason, "analysis_champion_not_unique")

    def test_selected_run_model_transform_and_metric_conflicts_hold(self) -> None:
        mutations = {
            "model_type": "random_forest",
            "transform_target": "none",
            "rmse": 46.0,
            "task_type": "classification",
            "split_strategy": "random_holdout",
            "r2": 0.5,
        }
        for field, value in mutations.items():
            with self.subTest(field=field), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                paths = write_project(root)
                metrics = json_value(paths["metrics"])
                metrics[field] = value
                write_json(paths["metrics"], metrics)
                decision = decide_question(engine_for(root), QUESTION)
                self.assertEqual(decision.status, "hold")
                self.assertEqual(decision.reason, "selected_run_conflict")

    def test_model_parameter_consensus_and_integer_shape_fail_closed(self) -> None:
        mutations = {
            "conflict": lambda value: 7,
            "missing": lambda value: None,
            "string": lambda value: "6",
            "number": lambda value: 6.0,
            "object": lambda value: {"value": 6},
            "boolean": lambda value: True,
        }
        for label, mutate in mutations.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                paths = write_project(root)
                config = json_value(paths["config"])
                if label == "missing":
                    del config["model_params"]["max_depth"]
                    for role in ("metrics", "summary"):
                        source = json_value(paths[role])
                        del source["model_params"]["max_depth"]
                        write_json(paths[role], source)
                elif label == "conflict":
                    config["model_params"]["max_depth"] = mutate(6)
                else:
                    replacement = mutate(6)
                    config["model_params"]["max_depth"] = replacement
                    for role in ("metrics", "summary"):
                        source = json_value(paths[role])
                        source["model_params"]["max_depth"] = replacement
                        write_json(paths[role], source)
                write_json(paths["config"], config)

                decision = decide_question(engine_for(root), QUESTION)

                self.assertEqual(decision.status, "hold")
                self.assertEqual(decision.reason, "model_parameter_not_exact_integer")

    def test_cross_source_float_and_boolean_parameter_values_hold(self) -> None:
        cases = (
            ("float", 6, 6.0),
            ("boolean", 1, True),
        )
        for label, baseline, replacement in cases:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                paths = write_project(root, parameter_value=baseline)
                metrics = json_value(paths["metrics"])
                metrics["model_params"]["max_depth"] = replacement
                write_json(paths["metrics"], metrics)

                decision = decide_question(engine_for(root), QUESTION)

                self.assertEqual(decision.status, "hold")
                self.assertEqual(decision.reason, "model_parameter_not_exact_integer")

    def test_modeling_ast_requires_exact_constructor_parameter_flow(self) -> None:
        invalid_sources = {
            "literal": '''def build_pipeline(model_params, task_type):
    return HistGradientBoostingRegressor(max_depth=6)
''',
            "other_key": modeling_source("max_depth", direct_key="tree_depth"),
            "wrong_constructor": '''def build_pipeline(model_params, task_type):
    return RandomForestRegressor(max_depth=model_params.get("max_depth"))
''',
            "duplicate_constructor": modeling_source("max_depth")
            + '\nother = HistGradientBoostingRegressor(max_depth=model_params.get("max_depth"))\n',
            "overwritten": '''def build_pipeline(model_params, task_type):
    depth = model_params.get("max_depth")
    depth = 6
    return HistGradientBoostingRegressor(max_depth=depth)
''',
        }
        for label, source in invalid_sources.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                paths = write_project(root, modeling=source)
                for role in ("config", "metrics", "summary"):
                    artifact = json_value(paths[role])
                    self.assertIs(
                        type(artifact["model_params"]["max_depth"]), int
                    )
                decision = decide_question(engine_for(root), QUESTION)
                self.assertEqual(decision.status, "hold")
                self.assertEqual(decision.reason, "model_parameter_not_propagated")

    def test_corrupt_and_duplicate_key_json_hold(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            paths = write_project(root)
            paths["metrics"].write_text(
                '{"model_type":"hist_gradient_boosting","model_type":"other"}',
                encoding="utf-8",
            )
            decision = decide_question(engine_for(root), QUESTION)
            self.assertEqual(decision.status, "hold")
            self.assertEqual(decision.reason, "analysis_source_invalid")

    def test_unsupported_question_returns_none_without_source_access(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            decision = decide_question(
                engine_for(Path(temp)),
                f"{PROJECT}の最良モデルを教えてください。",
            )
        self.assertIsNone(decision)


if __name__ == "__main__":
    unittest.main()
