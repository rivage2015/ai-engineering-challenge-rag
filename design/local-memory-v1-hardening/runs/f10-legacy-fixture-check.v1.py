"""Run only two existing, synthetic XLSX methods without model-touching class setup.

This is a compatibility diagnostic, not the original class/integration suite.
The original setUpClass invokes a builder subprocess and runtime fingerprint
probes unrelated to these two self-contained methods, so it is explicitly
bypassed here. Reader methods and both test assertions are unchanged.
"""
from __future__ import annotations

import contextlib
import http.client
import importlib.util
import sys
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "scripts"))
spec = importlib.util.spec_from_file_location("f10_existing_layer1_tests", ROOT / "tests/test_layer1_pipeline.py")
existing = importlib.util.module_from_spec(spec)
spec.loader.exec_module(existing)
reader_module = existing.probe_intermediate_records
real_probe = reader_module.Probe


def suppressed_probe(*args, **kwargs):
    kwargs["visual_observation_mode"] = "suppressed"
    return real_probe(*args, **kwargs)


suite = unittest.TestSuite([
    existing.Layer1PipelineTest("test_ooxml_fallback_preserves_numeric_raw_lexeme"),
    existing.Layer1PipelineTest("test_ooxml_fallback_keeps_formula_and_saved_value_in_search_units"),
])
with contextlib.ExitStack() as stack:
    stack.enter_context(mock.patch.object(existing.Layer1PipelineTest, "setUpClass", return_value=None))
    stack.enter_context(mock.patch.object(existing.Layer1PipelineTest, "tearDownClass", return_value=None))
    stack.enter_context(mock.patch.object(reader_module, "Probe", side_effect=suppressed_probe))
    stack.enter_context(mock.patch.object(http.client.HTTPConnection, "connect", side_effect=AssertionError("network forbidden")))
    stack.enter_context(mock.patch.object(existing.build_intermediate_records, "_ollama_json", side_effect=AssertionError("runtime probe forbidden")))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
sys.exit(0 if result.wasSuccessful() else 1)
