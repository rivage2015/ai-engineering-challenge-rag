"""Recheck the two existing methods after adding the missing package root binding.

This bypasses unrelated model-touching class setup, not test assertions or
Reader behavior. HTTP, runtime metadata and subprocess creation are forbidden;
visual observation is suppressed. This is not the full Layer1 class suite.
"""
from __future__ import annotations

import contextlib
import http.client
import importlib.util
import subprocess
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
    stack.enter_context(mock.patch.object(subprocess, "Popen", side_effect=AssertionError("subprocess forbidden")))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
sys.exit(0 if result.wasSuccessful() else 1)
