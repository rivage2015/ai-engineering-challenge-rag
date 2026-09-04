from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[1]
DATASET = REPOSITORY / "evaluation" / "cross-format-kg-v0.1"
RUNNER = REPOSITORY / "scripts" / "evaluate_cross_format_kg_anti_hardcoding.py"
RUNTIME_PYTHON = (
    REPOSITORY / "rag" / ".venv" / "bin" / "python"
    if (REPOSITORY / "rag" / ".venv" / "bin" / "python").is_file()
    else Path(sys.executable)
)


class CrossFormatKgAntiHardcodingTests(unittest.TestCase):
    def test_variant_contract_covers_every_required_mutation(self) -> None:
        variant = json.loads(
            (DATASET / "anti-hardcoding-variant.json").read_text(
                encoding="utf-8"
            )
        )
        renamed = variant["renamed_files"]
        replacements = dict(variant["replacements"])

        self.assertEqual(5, len(renamed))
        self.assertEqual(set(variant["source_file_order"]), set(renamed))
        self.assertNotEqual(
            list(variant["source_file_order"]),
            sorted(variant["source_file_order"]),
        )
        self.assertTrue(all(old != new for old, new in renamed.items()))
        self.assertEqual("AURORA-42", replacements["ORION-27"])
        self.assertEqual("WS-DR-09", replacements["WS-MIG-04"])
        self.assertEqual("EMP-731", replacements["EMP-104"])
        self.assertEqual("EMP-842", replacements["EMP-208"])
        self.assertEqual("高藤未来", replacements["佐藤未来"])
        self.assertEqual("佐橋蓮", replacements["高橋蓮"])
        self.assertEqual("2026-03-31", replacements["2023-03-31"])
        self.assertEqual("2026-04-01", replacements["2023-04-01"])
        self.assertEqual(
            {item[0] for item in variant["replacements"]},
            set(variant["forbidden_old_values"]),
        )
        self.assertIn(
            "災害復旧フェーズへの移行に伴う運営体制の再編",
            variant["required_new_graph_values"],
        )

        cases = [
            json.loads(line)
            for line in (
                DATASET / "gold" / "anti-hardcoding-qa-cases.jsonl"
            ).read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        original_questions = {
            json.loads(line)["question"]
            for line in (DATASET / "gold" / "qa-cases.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
            if line.strip()
        }
        self.assertEqual(5, len(cases))
        self.assertTrue(
            all(case["question"] not in original_questions for case in cases)
        )

    def test_full_mutation_gate_rebuilds_and_answers_without_old_value_leak(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "anti-hardcoding-output"
            completed = subprocess.run(
                [
                    str(RUNTIME_PYTHON),
                    str(RUNNER),
                    "--dataset",
                    str(DATASET),
                    "--out",
                    str(output),
                    "--python",
                    str(RUNTIME_PYTHON),
                ],
                cwd=REPOSITORY,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=180,
            )
            self.assertEqual(
                0,
                completed.returncode,
                msg=f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}",
            )
            report = json.loads(
                (output / "anti-hardcoding-report.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual("PASS", report["decision"])
            self.assertTrue(report["required_before_answer_promotion"])
            self.assertEqual(5, report["normal_case_count"])
            self.assertEqual(4, report["accepted_case_count"])
            self.assertEqual(1, report["hold_case_count"])
            self.assertEqual(0, report["old_value_leak_count"])
            self.assertEqual(0, report["production_literal_leak_count"])
            self.assertEqual(
                0, report["measured_outbound_network_attempt_count"]
            )
            self.assertTrue(all(report["mutations"].values()))

            results = [
                json.loads(line)
                for line in (
                    output / "anti-hardcoding-results.jsonl"
                ).read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            self.assertEqual(5, len(results))
            self.assertTrue(all(result["passed"] for result in results))
            rendered = json.dumps(results, ensure_ascii=False, sort_keys=True)
            for forbidden in json.loads(
                (DATASET / "anti-hardcoding-variant.json").read_text(
                    encoding="utf-8"
                )
            )["forbidden_old_values"]:
                self.assertNotIn(forbidden, rendered)


if __name__ == "__main__":
    unittest.main()
