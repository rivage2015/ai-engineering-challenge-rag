"""Current fan-in positive plus untouched historical-version rejection.

Only the original TEST helper is substituted. No product constant is patched.
The complete original positive method, including ordering and ID assertions,
is executed verbatim; the original failing test and its log remain unchanged.
"""
import hashlib
import importlib.util
from pathlib import Path
import sys
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[3]
SOURCE = ROOT / "tests/test_semantic_lineage_relations.py"
EXPECTED_SOURCE = "26f1624c0dcdddacd86f8cf21253247db9f3f330d764c9b9e4cad80f8e41f4d0"
if hashlib.sha256(SOURCE.read_bytes()).hexdigest() != EXPECTED_SOURCE:
    raise AssertionError("original lineage test changed")
spec = importlib.util.spec_from_file_location("f11a_original_lineage", SOURCE)
original = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = original
spec.loader.exec_module(original)
historical_fixture = original.search_unit


def current_fixture(*args, **kwargs):
    unit = historical_fixture(*args, **kwargs)
    unit["provenance"]["builder_version"] = "0.7.0"
    identity = {
        "document_id": unit["document_id"],
        "unit_type": unit["unit_type"],
        "source_evidence_ids": unit["source_evidence_ids"],
        "locator": unit["locator"],
        "text_sha256": unit["text"]["sha256"],
        "builder": "search-unit-builder",
        "builder_version": "0.7.0",
    }
    unit["search_unit_id"] = original.validator.stable_id("su", identity)
    return unit


class LineageVersionGold(unittest.TestCase):
    def test_current_version_preserves_complete_original_fan_in_assertions(self):
        self.assertEqual(current_fixture([original.SOURCE_A])["provenance"]["builder_version"], "0.7.0")
        case = original.SemanticLineageRelationTests(
            "test_unsharded_table_row_promotes_exact_stable_fan_in")
        with mock.patch.object(original, "search_unit", current_fixture):
            case.test_unsharded_table_row_promotes_exact_stable_fan_in()
        self.assertIs(original.search_unit, historical_fixture)

    def test_historical_version_is_rejected_with_exact_provenance_error(self):
        unit = historical_fixture([original.SOURCE_A, original.SOURCE_B])
        self.assertEqual(unit["provenance"]["builder_version"], "0.6.0")
        derived = original.derived_projection(unit)
        semantic = [original.semantic_source(original.SOURCE_B), derived,
                    original.semantic_source(original.SOURCE_A)]
        layer = [original.layer_evidence(original.SOURCE_A),
                 original.layer_evidence(original.SOURCE_B)]
        with self.assertRaises(ValueError) as raised:
            original.validator.derive_verified_lineage_relations([unit], semantic, layer)
        self.assertEqual(str(raised.exception),
                         "lineage_search_unit_provenance_invalid:" + unit["search_unit_id"])
