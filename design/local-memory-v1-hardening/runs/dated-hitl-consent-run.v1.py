"""Root-reviewed-only pure consent batches; 30 s / 1 MiB durable logs.

No execution is authorized by saving this file. The parent supplies the exact
reviewed source hash; red additionally requires the immutable 0.1.5 baseline.
This guard is not an OS sandbox, production limit or RSS guarantee.
"""
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import signal
import sys
import unittest

ROOT = Path(__file__).resolve().parents[3]
RUNS = Path(__file__).resolve().parent
TEST = ROOT / "tests/test_dated_consent_records.py"
GOLD = RUNS / "dated-hitl-consent-gold.v1.py"
TEST_SHA = "f85d05c018dabac206e5f7308b57b6742e96a7ad0214689828d224881179086d"
BEFORE_SHA = "11218cc2b5170ba59a69203bcc549a50dc6eb6f88e487d191fb55a76048973a8"
SUPERVISOR_SHA = "6b3bde90b6ed649a82325f6b5ad9661756bef7b6e0303fa74c2d045fb26eb4a9"
SEED_METHODS = (
    "test_matching_legacy_dated_record_is_not_new_consent",
    "test_version_tag_alone_cannot_upgrade_legacy",
    "test_missing_relation_is_not_inferred_from_same_family",
    "test_current_confirmation_is_explicit_not_truthy_or_defaulted",
    "test_use_permission_is_explicit_not_truthy_or_defaulted",
    "test_missing_or_stale_displayed_candidate_revision_is_held",
    "test_independent_annual_relation_cannot_force_other_record_obsolete",
    "test_defer_cannot_reuse_an_old_selected_path",
    "test_old_writer_needs_display_proof_before_decision_store_access",
    "test_undated_legacy_selection_remains_a_compatibility_control",
)
POST_METHODS = (
    "test_literal_complete_record_allows_only_chosen_source",
    "test_independent_and_defer_keep_every_candidate_unselected",
    "test_false_current_or_use_flag_is_an_explicit_hold",
    "test_prepare_honest_submission_returns_exact_literal_without_mutation",
    "test_prepare_changed_graph_raw_graph_or_inventory_is_stale",
    "test_prepare_changed_generation_or_scope_is_stale",
    "test_prepare_store_absent_and_present_are_distinct_revisions",
    "test_prepare_stale_selected_source_same_path_is_rejected",
    "test_prepare_changed_nonselected_source_invalidates_complete_set",
    "test_prepare_matching_but_fabricated_set_revision_is_rejected",
    "test_prepare_current_policy_mismatch_is_not_silently_migrated",
    "test_prepare_missing_or_unknown_revision_fields_do_not_create_authority",
    "test_validator_binds_group_set_selected_source_reviewed_set_and_policy",
    "test_validator_strict_schema_keys_and_boolean_types",
    "test_existing_strict_decision_json_parser_still_rejects_bad_input",
    "test_prepare_independent_or_defer_has_no_selected_or_use_permission",
    "test_prepare_explicit_use_denial_is_recorded_but_not_selected",
)


def checked_source(path, expected):
    with path.open("rb") as handle:
        raw = handle.read(1048577)
    if len(raw) > 1048576 or hashlib.sha256(raw).hexdigest() != expected:
        raise AssertionError("reviewed source drift or size: " + path.name)
    return raw


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class PureResult(unittest.TextTestResult):
    def _exc_info_to_string(self, error, test):
        # Do not open source files for traceback formatting under the IO guard.
        return error[0].__name__ + ": " + str(error[1]) + "\n"


def worker(mode, expected_resolver):
    raw = checked_source(TEST, TEST_SHA)
    if raw != checked_source(GOLD, TEST_SHA):
        raise AssertionError("test/gold byte mismatch")
    resolver_path = ROOT / "distribution/macos-local-memory/engine/document_version_resolver.py"
    checked_source(resolver_path, expected_resolver)
    target = load(TEST, "dated_consent_fixed_test")
    if mode == "post":
        if not all(callable(getattr(target.resolver, name, None)) for name in (
            "validate_dated_consent", "prepare_dated_consent")):
            raise RuntimeError("post-API gate missing: infrastructure error, not semantic RED")
        class_name, expected_methods = "DatedConsentPostApiTests", POST_METHODS
    else:
        class_name, expected_methods = "DatedConsentSeedTests", SEED_METHODS
    cls = getattr(target, class_name)
    methods = unittest.defaultTestLoader.getTestCaseNames(cls)
    if methods != sorted(expected_methods):
        raise AssertionError("literal method list drift")
    fixture_bytes = len(json.dumps(target.CANDIDATES, ensure_ascii=False).encode())
    fixture_bytes += len(json.dumps(target.consent(), ensure_ascii=False).encode())
    if len(target.CANDIDATES) != 2 or fixture_bytes > 8192:
        raise AssertionError("fixed tiny fixture definition exceeded")
    print(json.dumps({"mode": mode, "resolver_sha256": expected_resolver,
                      "test_sha256": TEST_SHA, "methods": methods,
                      "fixture_definition_bytes": fixture_bytes,
                      "original_document_reads": False, "actual_store_io": False,
                      "post_api_not_baseline_red": mode == "post"}), flush=True)

    def deny(event, args):
        if event == "open" or event.startswith(("socket.", "subprocess.", "ctypes.")):
            raise AssertionError("pure runtime IO forbidden: " + event)
        if event in {"os.system", "os.fork", "os.forkpty", "os.posix_spawn", "os.exec",
                     "os.mkdir", "os.remove", "os.rename", "os.rmdir", "os.symlink",
                     "os.link", "os.truncate", "os.listdir", "os.scandir"}:
            raise AssertionError("pure runtime filesystem/process forbidden: " + event)

    def deadline(signum, frame):
        raise TimeoutError("pure consent 30-second deadline")

    signal.signal(signal.SIGALRM, deadline)
    signal.alarm(30)
    sys.addaudithook(deny)
    suite = unittest.TestSuite(cls(method) for method in methods)
    result = unittest.TextTestRunner(verbosity=2, resultclass=PureResult).run(suite)
    signal.alarm(0)
    return 0 if result.wasSuccessful() else 1


def main():
    if len(sys.argv) not in (4, 5):
        raise SystemExit("mode, fresh run ID and reviewed resolver SHA required")
    mode, run_id, expected = sys.argv[1:4]
    if mode not in {"red", "seed", "post"}:
        raise SystemExit("invalid fixed batch mode")
    if not re.fullmatch(r"dated-hitl-consent-[a-z0-9-]+-[0-9]{3}", run_id):
        raise SystemExit("invalid fresh run ID")
    if not re.fullmatch(r"[0-9a-f]{64}", expected) or (mode == "red" and expected != BEFORE_SHA):
        raise SystemExit("reviewed source hash required; red cannot move baseline")
    if len(sys.argv) == 5:
        if sys.argv[4] != "--worker":
            raise SystemExit("invalid worker flag")
        return worker(mode, expected)
    supervisor_path = ROOT / "scripts/run_local_memory_hardening_tests.py"
    checked_source(supervisor_path, SUPERVISOR_SHA)
    supervisor = load(supervisor_path, "dated_consent_supervisor")
    result = supervisor.run_bounded(
        [sys.executable, "-I", "-B", "-u", str(Path(__file__).resolve()),
         mode, run_id, expected, "--worker"],
        RUNS / run_id, cwd=ROOT, timeout_seconds=30, max_log_bytes=1048576)
    print(json.dumps(result, indent=2))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
