"""Additional F11a post-API gold batches; root must approve before each run.

Original nine-method runner/gold/RED remain immutable. Same F04a guard,
explicit-fixture budget and bounded supervisor, with fixed per-mode methods.
"""
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
TARGET = ROOT / "tests/test_notebook_metadata_binding.py"
METHODS = {
    "post": [
        "test_g1_exact_metadata_only_report_three_paths",
        "test_g1_equal_zero_null_raw_and_multiple_outputs",
        "test_g1_long_text_retains_one_direct_unit_and_whitespace_emits_none",
        "test_g2_wrong_pointer_role_locator_ordinal_and_bool_fail",
        "test_g2_parser_extension_and_state_omission_cannot_opt_out",
        "test_g2_unrelated_text_state_injection_rejected",
        "test_g3_same_id_context_mutations_rejected_by_both_search_validators",
        "test_g4_invalid_count_types_and_malformed_json_raise_controlled_errors",
        "test_g4_byte_token_depth_and_number_limits_precede_parse",
        "test_g4_source_mismatch_and_missing_file_are_failures",
        "test_g4_each_validation_uses_one_bounded_source_snapshot",
        "test_g4_probe_digest_and_facts_share_the_read_snapshot",
        "test_g4_validators_reject_b_facts_bound_to_an_a_snapshot",
        "test_g4_path_cache_cannot_reuse_a_previous_calls_metadata",
        "test_g5_missing_code_and_execute_result_counts_are_checked_but_unverified",
        "test_g5_rootless_reports_unverified_and_old_counts_wrapper_raises",
        "test_g5_rootless_known_malformed_state_is_not_hidden",
        "test_g5_partial_and_failed_records_never_return_pass",
        "test_g5_empty_notebook_is_zero_checked_unverified",
        "test_g5_cli_pass_unverified_and_fail_envelopes",
        "test_g6_versions_and_old_notebook_have_no_silent_upgrade"
    ],
    "residual": [
        "test_residual_metadata_only_allows_coherent_raw_text_replacement",
        "test_residual_nonzero_record_removal_does_not_claim_membership",
        "test_residual_search_only_coherent_state_forgery_is_not_original_attestation"
    ]
}

def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module

def worker(mode):
    guard = load(HERE / "f04a-executor-run.v1.py", "f11a_executor_guard_v2")
    budget = load(HERE / "f05b-fixture-budget.v1.py", "f11a_executor_budget_v2")
    budget.AUTHORS = {
        str(TARGET),
        str(ROOT / "distribution/macos-local-memory/tests/test_versioned_safe_index_e2e.py"),
    }
    original_load = guard.load
    original_suite = unittest.defaultTestLoader.loadTestsFromModule
    target = None
    def redirect(path, name):
        nonlocal target
        if path.name == "test_document_version_resolver.py":
            target = original_load(TARGET, "f11a_executor_post_tests")
            target.builder = types.SimpleNamespace()
            return target
        return original_load(path, name)
    def selected(module, *args, **kwargs):
        if module is target:
            return unittest.TestSuite(module.NotebookMetadataPostAPITests(name) for name in METHODS[mode])
        return original_suite(module, *args, **kwargs)
    guard.load = redirect
    with budget.enforce(), mock.patch.object(
        unittest.defaultTestLoader, "loadTestsFromModule", selected,
    ):
        return guard.worker("resolver")

def main():
    mode, run_id = sys.argv[1:3]
    if mode not in METHODS or not re.fullmatch(r"f11a-executor-(?:post|residual)-[a-z0-9-]+", run_id):
        raise SystemExit("invalid fixed F11a post-API scope")
    if "--worker" in sys.argv:
        return worker(mode)
    supervisor = load(ROOT / "scripts/run_local_memory_hardening_tests.py", "f11a_executor_supervisor_v2")
    result = supervisor.run_bounded(
        [sys.executable, "-I", "-B", "-u", str(Path(__file__).resolve()), mode, run_id, "--worker"],
        HERE / run_id, cwd=ROOT, timeout_seconds=30, max_log_bytes=1048576,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "passed" else 1

if __name__ == "__main__":
    raise SystemExit(main())
