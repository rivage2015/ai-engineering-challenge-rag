"""Three existing mocked Notebook image regressions, not real OCR quality."""
from __future__ import annotations
import importlib.util
import json
from pathlib import Path
import re
import sys
import types
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
TARGET = ROOT / "tests/test_local_embedded_visual_pipeline.py"
METHODS = (
    "test_notebook_embedded_image_preserves_cell_and_source_lineage",
    "test_notebook_referenced_markdown_attachment_is_visually_read",
    "test_notebook_code_data_uri_is_preserved_and_not_treated_as_displayed",
)


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def worker():
    guard = load(HERE / "f04a-executor-run.v1.py", "f11a_image_guard")
    budget = load(HERE / "f05b-fixture-budget.v1.py", "f11a_image_budget")
    budget.AUTHORS = {str(TARGET), str(ROOT / "distribution/macos-local-memory/tests/test_versioned_safe_index_e2e.py")}
    original_load = guard.load
    original_suite = unittest.defaultTestLoader.loadTestsFromModule
    target = None

    def redirect(path, name):
        nonlocal target
        if path.name == "test_document_version_resolver.py":
            target = original_load(TARGET, "f11a_image_tests")
            target.builder = types.SimpleNamespace()
            return target
        return original_load(path, name)

    def selected(module, *args, **kwargs):
        if module is target:
            return unittest.TestSuite(module.LocalEmbeddedVisualPipelineTests(name) for name in METHODS)
        return original_suite(module, *args, **kwargs)

    guard.load = redirect
    with budget.enforce(), mock.patch.object(unittest.defaultTestLoader, "loadTestsFromModule", selected):
        return guard.worker("resolver")


def main():
    run_id = sys.argv[1]
    if not re.fullmatch(r"f11a-root-images-[0-9]{3}", run_id):
        raise SystemExit("invalid fixed scope run id")
    if "--worker" in sys.argv:
        return worker()
    supervisor = load(ROOT / "scripts/run_local_memory_hardening_tests.py", "f11a_image_supervisor")
    result = supervisor.run_bounded(
        [sys.executable, "-I", "-B", "-u", str(Path(__file__).resolve()), run_id, "--worker"],
        HERE / run_id, cwd=ROOT, timeout_seconds=30, max_log_bytes=1048576,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
