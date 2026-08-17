from __future__ import annotations

import csv
import hashlib
import json
import sys
import tempfile
import unittest
import zipfile
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import build_incremental_submission as builder  # noqa: E402


class FakeEngine:
    def __init__(self, decisions: dict[str, object], events: list[str]) -> None:
        self.decisions = decisions
        self.events = events

    def decide_from_graph(
        self,
        question_id: str,
        question: str,
        plan: object,
    ) -> object:
        del question, plan
        self.events.append(f"decide:{question_id}")
        return self.decisions[question_id]


def fake_plan(status: str = "pass") -> object:
    return SimpleNamespace(
        strict_status=status,
        strict_reasons=(
            ("extended_graph_certified",)
            if status == "pass"
            else ("generic_advisory_graph",)
        ),
        qur_sha256=(status[0] * 64),
    )


def fake_result(answer: str, marker: str) -> object:
    return SimpleNamespace(
        answer=answer,
        source_paths=(f"opaque/{marker}.csv",),
        source_sha256=marker[0] * 64,
        operation_count=2,
        output_count=1,
    )


def resolved(answer: str, marker: str) -> object:
    return SimpleNamespace(
        status="resolved",
        reason="certified_graph",
        result=fake_result(answer, marker),
    )


def hold() -> object:
    return SimpleNamespace(
        status="hold",
        reason="graph_not_structured",
        result=None,
    )


class IncrementalSubmissionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="aiec-overlay-")
        self.root = Path(self.temporary.name)
        self.source_root = self.root / "opaque-source"
        self.source_root.mkdir()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def write_questions(
        self,
        rows: list[tuple[str, str]],
        *,
        name: str = "questions_test.csv",
        header: tuple[str, ...] = ("index", "question"),
    ) -> Path:
        path = self.root / name
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(header)
            writer.writerows(rows)
        return path

    def write_base(
        self,
        rows: list[tuple[str, str]],
        *,
        name: str = "base.csv",
    ) -> Path:
        path = self.root / name
        with path.open("w", encoding="utf-8", newline="") as handle:
            csv.writer(handle).writerows(rows)
        return path

    def output_paths(self, tag: str) -> tuple[Path, Path, Path]:
        directory = self.root / tag
        return (
            directory / "predictions.csv",
            directory / "submission.zip",
            directory / "overlay-log.json",
        )

    def run_with_mocks(
        self,
        questions: Path,
        base: Path,
        outputs: tuple[Path, Path, Path],
        *,
        plans: dict[str, object],
        decisions: dict[str, object],
        violations: dict[str, tuple[str, ...]] | None = None,
        events: list[str] | None = None,
        replace_exact: tuple[str, ...] = (),
        track_base_read: bool = False,
    ) -> dict[str, object]:
        observed = events if events is not None else []
        violations = violations or {}
        engine = FakeEngine(decisions, observed)

        def build(question_id: str, question: str, **kwargs: object) -> object:
            del question
            self.assertEqual({"fast_advisory": True}, kwargs)
            observed.append(f"build:{question_id}")
            return plans[question_id]

        def validate(answer: str, plan: object) -> tuple[str, ...]:
            del plan
            observed.append(f"validate:{answer}")
            return violations.get(answer, ())

        original_read_predictions = builder._read_predictions

        def read_base(path: Path, indices: object) -> object:
            observed.append("read_base")
            return original_read_predictions(path, indices)

        output_csv, output_zip, log_path = outputs
        with ExitStack() as stack:
            stack.enter_context(
                patch.object(builder, "build_glossary", return_value=object())
            )
            stack.enter_context(
                patch.object(
                    builder,
                    "StructuredCandidateEngine",
                    return_value=engine,
                )
            )
            stack.enter_context(patch.object(builder, "build_graph_plan", side_effect=build))
            stack.enter_context(
                patch.object(builder, "validate_graph_answer", side_effect=validate)
            )
            if track_base_read:
                stack.enter_context(
                    patch.object(builder, "_read_predictions", side_effect=read_base)
                )
            return builder.build_submission(
                questions,
                base,
                self.source_root,
                output_csv,
                output_zip,
                log_path,
                replace_exact=replace_exact,
            )

    def test_only_exact_unknown_is_replaced_and_hold_invalid_are_rejected(
        self,
    ) -> None:
        questions = self.write_questions(
            [(str(index), f"opaque question {index}") for index in range(5)]
        )
        base = self.write_base(
            [
                ("0", "わかりません"),
                ("1", "OPAQUE-BASE-CONCRETE"),
                ("2", "わかりません"),
                ("3", "わかりません"),
                ("4", "わかりません"),
            ]
        )
        answers = {str(index): f"OPAQUE-CANDIDATE-{index}" for index in range(4)}
        plans = {
            "0": fake_plan("pass"),
            "1": fake_plan("pass"),
            "2": fake_plan("hold"),
            "3": fake_plan("pass"),
            "4": fake_plan("pass"),
        }
        decisions = {
            "0": resolved(answers["0"], "alpha"),
            "1": resolved(answers["1"], "beta"),
            "2": resolved(answers["2"], "gamma"),
            "3": resolved(answers["3"], "delta"),
            "4": hold(),
        }
        events: list[str] = []
        outputs = self.output_paths("primary")
        log = self.run_with_mocks(
            questions,
            base,
            outputs,
            plans=plans,
            decisions=decisions,
            violations={answers["3"]: ("identifier_list_items_required",)},
            events=events,
            track_base_read=True,
        )

        with outputs[0].open(encoding="utf-8", newline="") as handle:
            rows = list(csv.reader(handle))
        self.assertEqual(
            [
                ["0", answers["0"]],
                ["1", "OPAQUE-BASE-CONCRETE"],
                ["2", "わかりません"],
                ["3", "わかりません"],
                ["4", "わかりません"],
            ],
            rows,
        )
        self.assertEqual("read_base", events[-1])
        self.assertEqual(1, log["adopted_count"])
        self.assertEqual(1, log["changed_count"])
        records = {record["index"]: record for record in log["candidates"]}
        self.assertTrue(records["0"]["adopted"])
        self.assertEqual(
            "base_answer_not_in_replace_exact", records["1"]["adoption_reason"]
        )
        self.assertIn(
            "graph_strict_status_not_pass",
            records["2"]["candidate_rejection_reasons"],
        )
        self.assertIn(
            "output_contract_violations",
            records["3"]["candidate_rejection_reasons"],
        )
        self.assertEqual("fail", records["3"]["output_validation_status"])
        self.assertIn(
            "decision_not_resolved",
            records["4"]["candidate_rejection_reasons"],
        )
        self.assertEqual("not_run", records["4"]["output_validation_status"])
        self.assertEqual(["opaque/alpha.csv"], records["0"]["source_paths"])
        self.assertEqual("a" * 64, records["0"]["source_sha256"])
        self.assertEqual(
            hashlib.sha256(outputs[0].read_bytes()).hexdigest(),
            log["outputs"]["csv_sha256"],
        )
        self.assertEqual(
            hashlib.sha256(outputs[1].read_bytes()).hexdigest(),
            log["outputs"]["zip_sha256"],
        )

    def test_every_candidate_finishes_before_base_predictions_are_read(self) -> None:
        questions = self.write_questions(
            [("a", "opaque alpha"), ("b", "opaque beta")]
        )
        base = self.write_base(
            [("a", "わかりません"), ("b", "わかりません")]
        )
        events: list[str] = []
        self.run_with_mocks(
            questions,
            base,
            self.output_paths("ordering"),
            plans={"a": fake_plan(), "b": fake_plan()},
            decisions={
                "a": resolved("OPAQUE-A", "alpha"),
                "b": resolved("OPAQUE-B", "beta"),
            },
            events=events,
            track_base_read=True,
        )
        self.assertEqual(
            [
                "build:a",
                "decide:a",
                "validate:OPAQUE-A",
                "build:b",
                "decide:b",
                "validate:OPAQUE-B",
                "read_base",
            ],
            events,
        )

    def test_explicit_replace_exact_adds_to_default_without_normalization(self) -> None:
        questions = self.write_questions([("x", "opaque x")])
        base = self.write_base([("x", "不明")])
        common = {
            "plans": {"x": fake_plan()},
            "decisions": {"x": resolved("OPAQUE-X", "xray")},
        }
        self.run_with_mocks(
            questions,
            base,
            self.output_paths("default-replace"),
            **common,
        )
        explicit_outputs = self.output_paths("explicit-replace")
        log = self.run_with_mocks(
            questions,
            base,
            explicit_outputs,
            replace_exact=("不明",),
            **common,
        )
        with (self.root / "default-replace" / "predictions.csv").open(
            encoding="utf-8", newline=""
        ) as handle:
            self.assertEqual([["x", "不明"]], list(csv.reader(handle)))
        with explicit_outputs[0].open(encoding="utf-8", newline="") as handle:
            self.assertEqual([["x", "OPAQUE-X"]], list(csv.reader(handle)))
        self.assertEqual(["わかりません", "不明"], log["replace_exact"])

    def test_zip_has_one_root_member_fixed_metadata_and_repeatable_bytes(self) -> None:
        questions = self.write_questions([("z", "opaque z")])
        base_csv = self.write_base([("z", "わかりません")])
        base_zip = self.root / "base.zip"
        with zipfile.ZipFile(base_zip, "w") as archive:
            archive.writestr("predictions.csv", base_csv.read_bytes())
        common = {
            "plans": {"z": fake_plan()},
            "decisions": {"z": resolved("OPAQUE-Z", "zulu")},
        }
        first = self.output_paths("deterministic-1")
        second = self.output_paths("deterministic-2")
        first_log = self.run_with_mocks(
            questions, base_zip, first, **common
        )
        self.run_with_mocks(questions, base_zip, second, **common)

        self.assertEqual(first[0].read_bytes(), second[0].read_bytes())
        self.assertEqual(first[1].read_bytes(), second[1].read_bytes())
        with zipfile.ZipFile(first[1]) as archive:
            self.assertEqual(["predictions.csv"], archive.namelist())
            info = archive.getinfo("predictions.csv")
            self.assertEqual(builder.FIXED_ZIP_TIMESTAMP, info.date_time)
            self.assertEqual(first[0].read_bytes(), archive.read(info))
        self.assertEqual("zip", first_log["inputs"]["base_predictions_format"])
        self.assertEqual(
            hashlib.sha256(base_zip.read_bytes()).hexdigest(),
            first_log["inputs"]["base_predictions_file_sha256"],
        )
        self.assertEqual(
            hashlib.sha256(base_csv.read_bytes()).hexdigest(),
            first_log["inputs"]["base_predictions_payload_sha256"],
        )

    def test_base_index_order_is_strict_and_checked_after_candidates(self) -> None:
        questions = self.write_questions(
            [("first", "opaque first"), ("second", "opaque second")]
        )
        base = self.write_base(
            [("second", "わかりません"), ("first", "わかりません")]
        )
        events: list[str] = []
        outputs = self.output_paths("bad-order")
        with self.assertRaisesRegex(ValueError, "indices/order"):
            self.run_with_mocks(
                questions,
                base,
                outputs,
                plans={"first": fake_plan(), "second": fake_plan()},
                decisions={
                    "first": resolved("OPAQUE-FIRST", "first"),
                    "second": resolved("OPAQUE-SECOND", "second"),
                },
                events=events,
                track_base_read=True,
            )
        self.assertEqual("read_base", events[-1])
        self.assertTrue(all(not path.exists() for path in outputs))

    def test_gold_valid_paths_and_extra_question_columns_are_forbidden(self) -> None:
        for name in ("questions_valid.csv", "questions_gold.csv"):
            path = self.write_questions([("x", "opaque")], name=name)
            with self.subTest(name=name), self.assertRaisesRegex(
                ValueError, "gold/valid"
            ):
                builder._read_questions(path)
        answer_column = self.write_questions(
            [("x", "opaque", "forbidden")],  # type: ignore[list-item]
            name="questions_with_answer.csv",
            header=("index", "question", "answer"),
        )
        with self.assertRaisesRegex(ValueError, "exactly index,question"):
            builder._read_questions(answer_column)

    def test_existing_output_is_never_overwritten(self) -> None:
        questions = self.write_questions([("x", "opaque")])
        base = self.write_base([("x", "わかりません")])
        outputs = self.output_paths("existing")
        outputs[0].parent.mkdir(parents=True)
        outputs[0].write_bytes(b"sentinel")
        with patch.object(builder, "build_graph_plan") as graph:
            with self.assertRaisesRegex(ValueError, "refusing to overwrite"):
                builder.build_submission(
                    questions,
                    base,
                    self.source_root,
                    *outputs,
                )
        graph.assert_not_called()
        self.assertEqual(b"sentinel", outputs[0].read_bytes())
        self.assertFalse(outputs[1].exists())
        self.assertFalse(outputs[2].exists())

    def test_written_log_matches_returned_audit_record(self) -> None:
        questions = self.write_questions([("x", "opaque x")])
        base = self.write_base([("x", "わかりません")])
        outputs = self.output_paths("log")
        returned = self.run_with_mocks(
            questions,
            base,
            outputs,
            plans={"x": fake_plan()},
            decisions={"x": resolved("OPAQUE-X", "xray")},
        )
        stored = json.loads(outputs[2].read_text(encoding="utf-8"))
        self.assertEqual(returned, stored)
        payload_hash = stored["outputs"].pop("log_payload_sha256")
        self.assertEqual(
            hashlib.sha256(builder._canonical_json(stored)).hexdigest(),
            payload_hash,
        )


if __name__ == "__main__":
    unittest.main()
