from __future__ import annotations

import importlib.util
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "hardening_test_runner", ROOT / "scripts/run_local_memory_hardening_tests.py",
)
assert SPEC is not None and SPEC.loader is not None
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)


class HardeningTestRunnerTests(unittest.TestCase):
    def run_script(self, script, **kwargs):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        output = Path(temporary.name) / "run"
        result = runner.run_bounded(
            [sys.executable, "-B", "-c", script], output,
            cwd=ROOT, timeout_seconds=kwargs.pop("timeout_seconds", 3), **kwargs,
        )
        self.assertEqual(result, json.loads((output / "result.json").read_text()))
        self.assertTrue((output / "started.json").is_file())
        return result, output

    def test_success_is_recorded_only_with_nonzero_test_count(self):
        result, _ = self.run_script("print('Ran 2 tests in 0.001s\\n\\nOK')")
        self.assertEqual("passed", result["status"])
        self.assertEqual(2, result["tests_reported"])

    def test_zero_tests_and_skips_do_not_pass(self):
        result, _ = self.run_script("print('Ran 0 tests in 0.000s\\n\\nOK')")
        self.assertEqual("no_tests", result["status"])
        result, _ = self.run_script("print('Ran 2 tests in 0.001s\\n\\nOK (skipped=1)')")
        self.assertEqual("completed_with_skips", result["status"])

    def test_nonzero_exit_does_not_pass(self):
        result, _ = self.run_script("import sys; print('Ran 2 tests in 0.001s'); sys.exit(1)")
        self.assertEqual("failed", result["status"])

    def test_timeout_is_bounded_and_persisted(self):
        result, _ = self.run_script("import time; time.sleep(20)", timeout_seconds=0.1)
        self.assertEqual("timeout", result["status"])
        self.assertLess(result["elapsed_seconds"], 3)

    def test_output_limit_keeps_log_bounded(self):
        result, output = self.run_script("print('x' * 200000)", max_log_bytes=1024)
        self.assertEqual("output_limit", result["status"])
        self.assertLessEqual((output / "unittest.log").stat().st_size, 1024)

    def test_existing_run_is_not_overwritten(self):
        result, output = self.run_script("print('Ran 1 test in 0.001s\\n\\nOK')")
        before = (output / "result.json").read_bytes()
        with self.assertRaises(FileExistsError):
            runner.run_bounded([sys.executable, "-c", "pass"], output, cwd=ROOT)
        self.assertEqual(before, (output / "result.json").read_bytes())

    def test_only_terminal_summary_can_supply_counts_and_outcome(self):
        cases = [
            ("Ran 1 test in 0.001s\n\nOK\nRan 0 tests in 0.000s\n\nOK", "no_tests"),
            ("logging skipped=0\nRan 2 tests in 0.001s\n\nOK (skipped=1)", "completed_with_skips"),
            ("Ran 2 tests in 0.001s\n\nFAILED (failures=1)", "failed"),
            ("Ran 2 tests in 0.001s\n\nOK\ntrailing unrecognized output", "no_tests"),
            ("Ran 2 tests in 0.001s\n\nOK (expected failures=1)", "completed_with_expected_failures"),
        ]
        for output, expected in cases:
            with self.subTest(expected=expected, output=output):
                result, _ = self.run_script(f"print({output!r})")
                self.assertEqual(expected, result["status"])

    def test_failed_unittest_with_exit_false_does_not_pass(self):
        script = (
            "import unittest\n"
            "class Failure(unittest.TestCase):\n"
            " def test_failure(self): self.fail('fixture failure')\n"
            "unittest.main(exit=False)\n"
        )
        result, _ = self.run_script(script)
        self.assertEqual(0, result["exit_code"])
        self.assertEqual("failed", result["status"])

    def test_repository_import_contract_is_explicit(self):
        result, _ = self.run_script(
            "import tests.test_answer_graph_retrieval_policy; "
            "print('Ran 1 test in 0.001s\\n\\nOK')",
        )
        self.assertEqual("passed", result["status"])
        self.assertEqual(str(ROOT), result["repository_import_root"])

    def test_signal_handlers_are_restored(self):
        previous = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGHUP)}
        self.run_script("print('Ran 1 test in 0.001s\\n\\nOK')")
        self.assertEqual(previous, {sig: signal.getsignal(sig) for sig in previous})

    def test_sigterm_cleans_own_child_and_records_interruption(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "interrupted-run"
            pid_file = root / "child.pid"
            child_script = (
                "import os,time; from pathlib import Path; "
                f"Path({str(pid_file)!r}).write_text(str(os.getpid())); time.sleep(20)"
            )
            supervisor_script = (
                "import sys; from pathlib import Path; "
                f"sys.path.insert(0, {str(ROOT / 'scripts')!r}); "
                "import run_local_memory_hardening_tests as runner; "
                f"runner.run_bounded([sys.executable,'-B','-c',{child_script!r}], "
                f"Path({str(output)!r}), cwd=Path({str(ROOT)!r}), timeout_seconds=10)"
            )
            worker = subprocess.Popen(
                [sys.executable, "-B", "-c", supervisor_script],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            child_pid = None
            try:
                deadline = time.monotonic() + 3
                while not pid_file.exists() and time.monotonic() < deadline:
                    self.assertIsNone(worker.poll())
                    time.sleep(0.01)
                self.assertTrue(pid_file.exists(), "synthetic child did not start")
                child_pid = int(pid_file.read_text())
                worker.send_signal(signal.SIGTERM)
                worker.wait(timeout=3)
                record = json.loads((output / "interrupted.json").read_text())
                self.assertEqual("interrupted", record["status"])
                self.assertEqual(signal.SIGTERM, record["signal"])
                self.assertFalse((output / "result.json").exists())
                with self.assertRaises(ProcessLookupError):
                    os.kill(child_pid, 0)
            finally:
                # Only PIDs created by this fixture are cleanup targets.
                if child_pid is not None:
                    try:
                        os.killpg(child_pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                if worker.poll() is None:
                    worker.kill()
                worker.wait(timeout=3)


if __name__ == "__main__":
    unittest.main()
