"""Unexecuted fixed F11a collateral selection; root review required first.

Preserve F05a v2 -> F18 v2 -> F04 worker dispatch and its synthetic CLI stubs.
Add exact method selection and F05b explicit-write accounting only. Security's
one genuine XLSX is serialized unchanged into a <=16 KiB memory stream, then
written through the counted Path interface; no truncation or alternate fixture.
Original skipUnless remains: a missing openpyxl is a skip, never an acceptance.
One mode per fresh 30 s / 1 MiB-log run; no real models/network/GUI or install.
"""
from __future__ import annotations

from contextlib import ExitStack
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import re
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[3]
RUNS = ROOT / "design/local-memory-v1-hardening/runs"
TARGETS = {
    "focused": (
        "tests/test_immutable_lineage_validation.py", "ImmutableLineageValidationTests",
        "e6f051e4dbf1aea266b7c7cb3f62511c9f499a5d66d11a4f051b223b408a2e32",
        ("test_first_creation_and_repeat_read_only_validation",
         "test_source_and_reader_failures_preserve_lineage",
         "test_projector_rejects_failed_revalidation_even_with_saved_pass"),
    ),
    "lineage": (
        "tests/test_semantic_lineage_relations.py", "SemanticLineageRelationTests",
        "26f1624c0dcdddacd86f8cf21253247db9f3f330d764c9b9e4cad80f8e41f4d0",
        ("test_unsharded_table_row_promotes_exact_stable_fan_in",
         "test_native_section_contains_requires_real_heading_evidence",
         "test_full_validator_publishes_only_after_pass"),
    ),
    "migration": (
        "distribution/macos-local-memory/tests/test_reader_generation_migration.py",
        "ReaderGenerationMigrationTests",
        "c0b4774ecbc9e45fa17b0a983c9f9f23c1725001972ecd3026c5f5cf03fe8c0c",
        ("test_existing_step6_config_requires_reader_migration_without_mutation",
         "test_current_generation_matches_code_processing_and_schema_bytes",
         "test_builder_adapter_processing_or_schema_byte_change_requires_migration"),
    ),
    "security": (
        "tests/test_security_graph_partition.py", "SecurityGraphPartitionTests",
        "ae9d957baacdd46b1225f8e6f10e4909fad153e412e6a622e4aa1bd25cb5be6d",
        ("test_partial_exclusion_holds_mixed_fan_in_atomically",),
    ),
}


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class SourceZipBuffer(io.BytesIO):
    def write(self, value):
        if self.tell() + memoryview(value).nbytes > 16384:
            raise AssertionError("F11a security source XLSX exceeds 16 KiB")
        return super().write(value)


def worker(mode):
    relative, class_name, expected_sha256, methods = TARGETS[mode]
    target_path = ROOT / relative
    route = load(RUNS / "f05a-root-run.v2.py", "f11a_collateral_route")
    budget = load(RUNS / "f05b-fixture-budget.v1.py", "f11a_collateral_budget")
    budget.AUTHORS = {
        str(Path(__file__).resolve()), str(target_path),
        str(ROOT / "distribution/macos-local-memory/tests/test_versioned_safe_index_e2e.py"),
    }
    original_route_load = route.load
    target = None
    selections = 0
    with ExitStack() as patches:
        def register_guard(guard):
            original_guard_load = guard.load

            def observe_target(path, name):
                nonlocal target
                if Path(path) == target_path:
                    if hashlib.sha256(target_path.read_bytes()).hexdigest() != expected_sha256:
                        raise AssertionError("F11a collateral target source differs from reviewed hash")
                module = original_guard_load(path, name)
                if Path(path) == target_path:
                    if target is not None:
                        raise AssertionError("F11a collateral target loaded twice")
                    target = module
                    if mode == "security" and module.Workbook is not None:
                        original_save = module.Workbook.save

                        def bounded_save(workbook, filename):
                            destination = Path(filename)
                            confined = Path(tempfile.gettempdir()).resolve()
                            destination.resolve().relative_to(confined)
                            if (destination.name != "mixed-security.xlsx"
                                    or destination.parent.name != "source" or destination.exists()):
                                raise AssertionError("F11a unexpected security fixture save")
                            with SourceZipBuffer() as buffer:
                                result = original_save(workbook, buffer)
                                payload = buffer.getvalue()
                            destination.write_bytes(payload)
                            return result

                        patches.enter_context(mock.patch.object(module.Workbook, "save", bounded_save))
                return module

            guard.load = observe_target
            return guard

        def route_load(path, name):
            module = original_route_load(path, name)
            if Path(path).name == "f18-test-run.v2.py":
                original_collateral_load = module.load

                def collateral_load(inner_path, inner_name):
                    inner = original_collateral_load(inner_path, inner_name)
                    if Path(inner_path).name == "f04a-executor-run.v1.py":
                        return register_guard(inner)
                    return inner

                module.load = collateral_load
            return module

        def selected(module, *args, **kwargs):
            nonlocal selections
            if target is None or module is not target:
                raise AssertionError("F11a unexpected suite discovery")
            selections += 1
            if selections != 1:
                raise AssertionError("F11a collateral suite selected twice")
            cls = getattr(module, class_name)
            return unittest.TestSuite(cls(name) for name in methods)

        route.load = route_load
        patches.enter_context(budget.enforce())
        patches.enter_context(mock.patch.object(unittest.defaultTestLoader, "loadTestsFromModule", selected))
        result = route.worker(mode)
        if target is None or selections != 1:
            raise AssertionError("F11a fixed collateral selection was not reached")
        return result


def main():
    if len(sys.argv) not in (3, 4):
        raise SystemExit("mode and fresh run ID required")
    mode, run_id = sys.argv[1:3]
    if mode not in TARGETS or not re.fullmatch("f11a-collateral-" + mode + r"-[0-9]{3}", run_id):
        raise SystemExit("invalid fixed collateral scope")
    if len(sys.argv) == 4:
        if sys.argv[3] != "--worker":
            raise SystemExit("invalid worker option")
        return worker(mode)
    supervisor = load(ROOT / "scripts/run_local_memory_hardening_tests.py", "f11a_collateral_supervisor")
    result = supervisor.run_bounded(
        [sys.executable, "-I", "-B", "-u", str(Path(__file__).resolve()), mode, run_id, "--worker"],
        RUNS / run_id, cwd=ROOT, timeout_seconds=30, max_log_bytes=1048576,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
