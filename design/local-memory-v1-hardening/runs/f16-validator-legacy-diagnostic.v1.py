"""Observe old two-argument behavior, without treating absent new API as RED."""
import importlib.util
from pathlib import Path
import unittest
from unittest import mock

root = Path(__file__).resolve().parents[3]
path = root / "tests/test_path_graph_validator_binding.py"
spec = importlib.util.spec_from_file_location("path_binding_fixture", path)
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)
case = fixture.PathValidatorBindingTests
names = [
    "test_added_file_or_empty_directory_invalidates_old_inventory",
    "test_rehashed_forged_node_is_not_attested_by_valid_inventory",
    "test_valid_root_and_complete_inventory_are_accepted_without_mutation",
]
with mock.patch.object(case, "check", lambda self: fixture.validator.validate(self.graph_path, self.inventory_path)):
    result = unittest.TextTestRunner(verbosity=2).run(unittest.TestSuite(case(name) for name in names))
raise SystemExit(not result.wasSuccessful())
