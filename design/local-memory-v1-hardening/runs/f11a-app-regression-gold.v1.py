"""Frozen synthetic projector identity gold; prepared, NOT EXECUTED.

Use only after root reads this source and approves a bounded runner. The exact
selection is ProjectorProducerIdentityTests(method) for method in METHOD_NAMES,
in the literal order below. Proposed runner: reuse f04a-executor-run.v1.py's
resolver worker, redirect only test_document_version_resolver.py to this file,
and replace module discovery with that fixed suite, as in the reviewed F11a v2
runner. Keep f05b-fixture-budget.v1.py, supervisor 30 s / 1 MiB, -I -B, and all
network/process/source-read guards. No direct unittest.main entry point.

All fixtures are small in-memory literals (four Evidence records, one relation,
one raw connection); these tests write no files, create no databases, and call
no builder/CLI/model. The guard harness may create its ordinary tiny temporary
CONFIG fixture. Product import occurs only when the approved runner loads this
module after its guards. This uses the reviewed spec loader and guard.builder
compatibility seam; it does not instantiate the application harness itself.

Expected original 95b44b32 projector outcome by static inspection: 9 methods,
4 semantic assertion FAIL (current native, current SmartArt classification,
current support positive, current support tamper error specificity), 5 PASS,
0 ERROR / SKIP. This is a prediction, not a recorded run. Missing functions or
TypeError are deliberately not converted into RED. These graph-side checks do
not claim original-source attestation; the unchanged root app gold remains the
actual source-bound pipeline regression.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import types
import unittest


ROOT = Path(__file__).resolve().parents[3]
PRODUCT = ROOT / "distribution/macos-local-memory/engine/build_local_semantic_index.py"
SPEC = importlib.util.spec_from_file_location("f11a_identity_index_product", PRODUCT)
if SPEC is None or SPEC.loader is None:
    raise ImportError(f"cannot load {PRODUCT}")
index_builder = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = index_builder
SPEC.loader.exec_module(index_builder)

# The existing bounded guard assigns builder.run_tool; no production builder
# is loaded here, and no test invokes this unused compatibility placeholder.
builder = types.SimpleNamespace()

METHOD_NAMES = (
    "test_native_current_012_exact_types_allowed",
    "test_native_historical_versions_preserved",
    "test_native_unknown_versions_and_fabricated_producers_rejected",
    "test_smartart_current_012_exact_producer_allowed",
    "test_smartart_historical_versions_and_support_preserved",
    "test_smartart_current_012_support_is_verified",
    "test_smartart_unknown_versions_and_fabricated_producers_rejected",
    "test_smartart_current_012_mismatched_support_rejected",
    "test_smartart_historical_mismatched_support_rejected",
)

DOC = "doc_" + "a" * 32
DIAGRAM = "ev_" + "1" * 32
SOURCE = "ev_" + "2" * 32
TARGET = "ev_" + "3" * 32
SUPPORT = "ev_" + "4" * 32
MEMBER = "ppt/diagrams/data1.xml"
PREFIX = "slide=1;diagram=1"


def identify(value):
    """Independent test-side identity encoding; never used as expected output."""
    identity = {
        "class": value["relation_class"], "type": value["relation_type"],
        "from": value["from_ref"], "to": value["to_ref"],
        "generator": value["provenance"]["generated_by"],
        "generator_version": value["provenance"]["generator_version"],
    }
    raw = json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    value["relation_id"] = "rel_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]
    return value


def native(version, relation_type="contains"):
    return identify({
        "schema_version": "0.1", "record_type": "relation",
        "relation_class": "structural", "relation_type": relation_type,
        "from_ref": {"record_type": "document", "record_id": DOC},
        "to_ref": {"record_type": "evidence", "record_id": SOURCE},
        "properties": {}, "supporting_evidence_ids": [],
        "provenance": {
            "generated_by": "intermediate-record-extractor",
            "generator_version": version,
            "generated_at": "2026-09-09T00:00:00+00:00",
            "deterministic": True, "confidence": 1.0,
            "rule_or_model": "native containment", "warnings": [],
        },
        "status": "verified",
    })


def smartart(version):
    value = native(version, "diagram_connection")
    value["from_ref"] = {"record_type": "evidence", "record_id": SOURCE}
    value["to_ref"] = {"record_type": "evidence", "record_id": TARGET}
    value["provenance"]["rule_or_model"] = "native SmartArt srcId/destId connection"
    value["supporting_evidence_ids"] = [DIAGRAM, SUPPORT]
    value["properties"] = {
        "raw_connections": [{
            "modelId": "conn-1", "srcId": "start", "destId": "end", "type": "parOf",
        }],
        "source_member": MEMBER, "slide_number": 1,
        "semantic_interpretation_performed": False,
    }
    return identify(value)


def smartart_evidence():
    def record(evidence_id, record_type, ordinal, text, locator):
        return {
            "evidence_id": evidence_id, "document_id": DOC, "ordinal": ordinal,
            "observed_text": text, "adapter": {"source_record_type": record_type},
            "extraction_method": "native_parser", "locator": locator,
        }

    return {
        DIAGRAM: record(DIAGRAM, "shape", 1, "SmartArt（ファイル内の明示構造）", {
            "slide_number": 1, "source_member": MEMBER,
            "object_index": 1, "locator_text": PREFIX,
        }),
        SOURCE: record(SOURCE, "text_block", 1, "開始", {
            "slide_number": 1, "source_member": MEMBER, "object_index": 1,
            "object_id": "start", "locator_text": PREFIX + ";point=start",
        }),
        TARGET: record(TARGET, "text_block", 2, "終了", {
            "slide_number": 1, "source_member": MEMBER, "object_index": 2,
            "object_id": "end", "locator_text": PREFIX + ";point=end",
        }),
        SUPPORT: record(SUPPORT, "text_block", 1,
                        "SmartArtの明示接続: 開始 -> 終了 (原形式type=parOf)", {
            "slide_number": 1, "source_member": MEMBER,
            "object_index": 1, "locator_text": PREFIX + ";connection=1",
        }),
    }


def support_outcome(relation, evidence):
    # ValueError is the existing documented rejection path; unexpected API or
    # harness errors remain unittest ERROR, never semantic RED evidence.
    try:
        result = index_builder._validate_attested_smartart_connection(
            relation["relation_id"], relation, evidence,
        )
    except ValueError as error:
        return "rejected", str(error).split(":", 1)[0]
    return "accepted", result


class ProjectorProducerIdentityTests(unittest.TestCase):
    def test_native_current_012_exact_types_allowed(self):
        for relation_type in ("contains", "section_contains"):
            with self.subTest(relation_type=relation_type):
                value = native("0.12.0", relation_type)
                self.assertIs(True, index_builder._is_explicit_verified_structural(value))

    def test_native_historical_versions_preserved(self):
        for version in ("0.7.0", "0.8.0", "0.10.1", "0.11.0"):
            for relation_type in ("contains", "section_contains"):
                with self.subTest(version=version, relation_type=relation_type):
                    self.assertIs(True, index_builder._is_explicit_verified_structural(
                        native(version, relation_type),
                    ))

    def test_native_unknown_versions_and_fabricated_producers_rejected(self):
        cases = [("version=" + version, native(version)) for version in
                 ("0.9.0", "0.13.0", "999.0.0", "0.12.0-forged", "")]
        for key, value in (
            ("generated_by", "invented-producer"),
            ("rule_or_model", "self-asserted containment"),
            ("deterministic", False),
        ):
            changed = native("0.12.0")
            changed["provenance"][key] = value
            cases.append((key, identify(changed)))
        for key, value in (
            ("relation_type", "diagram_connection"),
            ("relation_class", "semantic"), ("status", "proposed"),
        ):
            changed = native("0.12.0")
            changed[key] = value
            cases.append((key, identify(changed)))
        for label, value in cases:
            with self.subTest(case=label):
                self.assertIs(False, index_builder._is_explicit_verified_structural(value))

    def test_smartart_current_012_exact_producer_allowed(self):
        self.assertIs(True, index_builder._is_explicit_verified_structural(smartart("0.12.0")))

    def test_smartart_historical_versions_and_support_preserved(self):
        for version in ("0.10.1", "0.11.0"):
            with self.subTest(version=version):
                value = smartart(version)
                self.assertIs(True, index_builder._is_explicit_verified_structural(value))
                self.assertEqual(("accepted", None), support_outcome(value, smartart_evidence()))

    def test_smartart_current_012_support_is_verified(self):
        self.assertEqual(("accepted", None), support_outcome(smartart("0.12.0"), smartart_evidence()))

    def test_smartart_unknown_versions_and_fabricated_producers_rejected(self):
        cases = [("version=" + version, smartart(version)) for version in
                 ("0.7.0", "0.8.0", "0.9.0", "0.13.0", "999.0.0", "0.12.0-forged", "")]
        for key, value in (
            ("generated_by", "invented-producer"),
            ("rule_or_model", "inferred diagram relation"),
            ("deterministic", False),
        ):
            changed = smartart("0.12.0")
            changed["provenance"][key] = value
            cases.append((key, identify(changed)))
        for label, value in cases:
            with self.subTest(case=label):
                self.assertIs(False, index_builder._is_explicit_verified_structural(value))
                self.assertEqual(
                    ("rejected", "graph_smartart_relation_contract_invalid"),
                    support_outcome(value, smartart_evidence()),
                )

    def assert_support_tampering_rejected(self, version):
        # Each mutation is independent, with a literal error oracle. A blanket
        # version rejection cannot masquerade as validating Evidence support.
        relation = smartart(version)
        evidence = smartart_evidence()
        wrong_text = deepcopy(evidence)
        wrong_text[SUPPORT]["observed_text"] = "unrelated support"
        wrong_raw = deepcopy(relation)
        wrong_raw["properties"]["raw_connections"][0]["srcId"] = "forged-start"
        wrong_locator = deepcopy(evidence)
        wrong_locator[SUPPORT]["locator"]["locator_text"] = PREFIX + ";connection=2"
        wrong_document = deepcopy(evidence)
        wrong_document[SUPPORT]["document_id"] = "doc_" + "b" * 32
        for label, candidate, candidate_evidence in (
            ("support text", relation, wrong_text),
            ("raw srcId", wrong_raw, evidence),
            ("support locator", relation, wrong_locator),
            ("support document", relation, wrong_document),
        ):
            with self.subTest(version=version, case=label):
                self.assertEqual(
                    ("rejected", "graph_smartart_support_binding_invalid"),
                    support_outcome(candidate, candidate_evidence),
                )

    def test_smartart_current_012_mismatched_support_rejected(self):
        self.assert_support_tampering_rejected("0.12.0")

    def test_smartart_historical_mismatched_support_rejected(self):
        for version in ("0.10.1", "0.11.0"):
            self.assert_support_tampering_rejected(version)
