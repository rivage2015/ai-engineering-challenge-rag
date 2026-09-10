"""Bounded read-only F11a packet generation; stdout only, not audit approval.

No product/test imports, discovery, network, model, subprocess, or file writes.
All selected paths are explicit below or finite expansions of literal lists.
Read limits: 250 files, 2 MiB/file, 24 MiB total, 30 s checked between reads;
output <= 2 MiB. These are generator guards, not OS/RSS guarantees. The caller
saves returned artifact/source_packet through apply_patch in NEW versioned files.
Historical manifests supply provenance only, never current path discovery or
current authority. Current eleven product pins are independent literals here.

v3 preparation: original 19 run entries + collateral4 + version-clarification1.
Currentness and recorded outcome are separate. Frozen literal parser controls
cover docstrings, concatenation, subtests and malformed progress. No generation
or control execution is authorized until root reviews this entire source.
"""
from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import sys
import time

ROOT = Path(__file__).resolve().parents[3]
R = "design/local-memory-v1-hardening/runs/"
TASK = "lms-v1-f11a-notebook-metadata-2026-09-09"
MAX_FILES, MAX_FILE, MAX_TOTAL, MAX_OUTPUT = 250, 2097152, 25165824, 2097152
START = time.monotonic()

PRODUCTS = (
    ("PROBE", "probe", "scripts/probe_intermediate_records.py", "1320cda390ab671dc2df82678ed2237e12aa4828b29666e3fb3d5b8a734f456d"),
    ("MANAGED", "managed", "scripts/build_intermediate_records.py", "8d9dd31a7acba7d6b8f3e5a841b86565ea3e6e85d932fa08f77e3f3641ae59dd"),
    ("INATIVE", "intermediate-native", "scripts/validate_intermediate_records.py", "91573a6216a1cf1814c36452e36752c08af46ef6dd393020830f7d82df87ae31"),
    ("ISTREAM", "intermediate-stream", "scripts/validate_intermediate_records_streaming.py", "e7f5a53dc9822f8cccd1f0fa60c3c6873605575b4297e4c888b350dbfe56928a"),
    ("SBUILD", "search-builder", "scripts/build_search_units.py", "55f0284acd6462260ba4962fa77246fb78090aed9b42554146e49723728c20df"),
    ("SNATIVE", "search-native", "scripts/validate_search_units.py", "c1bb29855c8b390c4af8334e9641af0ad069279cc7ea86faf25fddd85c7f500b"),
    ("SSTREAM", "search-stream", "scripts/validate_search_units_streaming.py", "54851bb4500a8eb82a9dada65c5bec7cbcdce212fd70852f430b3f703ac24059"),
    ("ESCHEMA", "evidence-schema", "schemas/evidence.schema.json", "030fea7251578e6a1c57934b595a457294ab7281c38e93779a097d3dca0a4899"),
    ("SSCHEMA", "search-schema", "schemas/search-unit.schema.json", "b0bc29b9e9252bd35ac553ca428e679b8e664d8a353ff6d40d56e90c4375e776"),
    ("AVALIDATOR", "adaptive-validator", "distribution/macos-local-memory/engine/validate_adaptive_semantic_graph.py", "c2587b685d06a1e8d1ec006bb2577be47168a0c14b072a22b3a90878c30563e3"),
    ("INDEX", "index", "distribution/macos-local-memory/engine/build_local_semantic_index.py", "c70f36d98012cca29877af72e4d345c30a555e1fef24e34ebc057b4f823e7229"),
)
IMAGE_IDS = {"PROBE", "INATIVE", "ISTREAM", "SBUILD", "SNATIVE", "SSTREAM"}
FIXED = {
    "COLLATERAL_RUNNER": "design/local-memory-v1-hardening/runs/f11a-collateral-run.v1.py",
    "COLLATERAL_PREFLIGHT": "design/local-memory-v1-hardening/runs/f11a-collateral-preflight.v1.md",
    "COLLATERAL_F05_ROUTE": "design/local-memory-v1-hardening/runs/f05a-root-run.v2.py",
    "COLLATERAL_F18_ROUTE": "design/local-memory-v1-hardening/runs/f18-test-run.v2.py",
    "FOCUSED_TEST": "tests/test_immutable_lineage_validation.py",
    "LINEAGE_TEST_RUN001": "tests/test_semantic_lineage_relations.py",
    "MIGRATION_TEST": "distribution/macos-local-memory/tests/test_reader_generation_migration.py",
    "SECURITY_TEST": "tests/test_security_graph_partition.py",
    "SECURITY_BUILDER": "distribution/macos-local-memory/engine/content_security_gate.py",
    "SECURITY_VALIDATOR": "distribution/macos-local-memory/engine/validate_content_security_gate.py",
    "ANSWER_READER": "distribution/macos-local-memory/engine/answer_local_memory.py",
    "LINEAGE_VERSION_SCOPE": "design/local-memory-v1-hardening/runs/f11a-lineage-version-scope.v1.md",
    "LINEAGE_VERSION_GOLD": "design/local-memory-v1-hardening/runs/f11a-lineage-version-gold.v1.py",
    "LINEAGE_VERSION_RUNNER": "design/local-memory-v1-hardening/runs/f11a-lineage-version-run.v1.py",
    "LINEAGE_DIAGNOSIS": "design/local-memory-v1-hardening/runs/f11a-lineage-collateral-diagnosis.v1.md",
    "REFRESH_PLAN": "design/local-memory-v1-hardening/runs/f11a-artifact-refresh-plan.v1.md",
    "PARSER_CONTROLS": "design/local-memory-v1-hardening/runs/f11a-artifact-parser-controls.v1.json",
    "PARSER_SUBTEST_CONTROLS": "design/local-memory-v1-hardening/runs/f11a-artifact-parser-subtest-controls.v1.json",
    "DRAFT_ARTIFACT_V1": "design/local-memory-v1-hardening/runs/f11a-graph-artifact.v1.json",
    "DRAFT_PACKET_V1": "design/local-memory-v1-hardening/runs/f11a-source-packet.v1.json",
    "DRAFT_HANDOFF_V1": "design/local-memory-v1-hardening/runs/f11a-artifact-handoff.v1.md",
    "GENERATION_RESULT_V1": "design/local-memory-v1-hardening/runs/f11a-artifact-generation-result.v1.json",
    "GENERATOR_V2": "design/local-memory-v1-hardening/runs/f11a-final-artifact-generator.v2.py",
    "CONTRACT": R + "f11a-task-contract.v1.md",
    "ADDENDUM": R + "f11a-task-contract-addendum.v1.md",
    "CONTRACT_GATE": R + "f11a-contract-gate.v1.md",
    "IMPLEMENTATION_GATE": R + "f11a-implementation-gate.v1.md",
    "REGRESSION_GATE": R + "f11a-regression-repair-gate.v1.md",
    "IMAGE_GATE": R + "f11a-image-repair-gate.v1.md",
    "IMAGE_DESIGN": R + "f11a-image-repair-design.v1.md",
    "ROOT_RED_ASSESSMENT": R + "f11a-root-red-assessment.v1.md",
    "EXECUTOR_RED_ASSESSMENT": R + "f11a-executor-red-assessment.v1.md",
    "APP_DIAGNOSIS": R + "f11a-app-regression-diagnosis.v1.md",
    "IMAGE_DIAGNOSIS": R + "f11a-image-regression-diagnosis.v1.md",
    "INITIAL_BEFORE_MANIFEST": R + "f11a-executor-before-manifest.v1.json",
    "INITIAL_MANIFEST": R + "f11a-executor-manifest.v1.json",
    "INITIAL_COHERENT": R + "f11a-executor-coherent.v1.json",
    "INITIAL_SUMMARY": R + "f11a-executor-summary.v1.md",
    "INITIAL_FREEZE": R + "f11a-executor-freeze-result.v1.json",
    "INDEX_PACKET": R + "f11a-index-regression-coherent.v1.json",
    "INDEX_SUMMARY": R + "f11a-index-regression-repair.v1.md",
    "IMAGE_PACKET": R + "f11a-image-repair-coherent.v1.json",
    "IMAGE_SUMMARY": R + "f11a-image-repair-summary.v1.md",
    "IMAGE_MECHANICAL": R + "f11a-image-repair-mechanical-check.v1.json",
    "IMAGE_RUN_SNAPSHOT": R + "f11a-image-repair-coherent-before-runs.v1.json",
    "METADATA_TEST": "tests/test_notebook_metadata_binding.py",
    "METADATA_GOLD1": R + "f11a-executor-gold-test.v1.py",
    "METADATA_GOLD2": R + "f11a-executor-gold-test.v2.py",
    "METADATA_GOLD_DELTA": R + "f11a-executor-gold-additive.v2.diff",
    "METADATA_METHODS": R + "f11a-executor-gold-methods.v2.json",
    "ROOT_BOUNDARY_TEST": "tests/test_notebook_metadata_boundaries.py",
    "APP_TEST": "tests/test_notebook_metadata_application.py",
    "APP_GOLD_ORIGINAL": R + "f11a-root-app-gold.v1.py",
    "APP_GOLD_CORRECTION": R + "f11a-root-app-gold-correction.v1.md",
    "INDEX_TEST": R + "f11a-app-regression-gold.v1.py",
    "IMAGE_TEST": R + "f11a-image-regression-gold.v1.py",
    "IMAGE_ADDITIONAL_TEST": R + "f11a-image-additional-gold.v1.py",
    "LEGACY_IMAGE_TEST": "tests/test_local_embedded_visual_pipeline.py",
    "INITIAL_RUNNER": R + "f11a-executor-run.v1.py",
    "POST_RUNNER": R + "f11a-executor-run.v2.py",
    "FINAL_METADATA_RUNNER": R + "f11a-final-metadata-run.v1.py",
    "ROOT_BOUNDARY_RUNNER": R + "f11a-root-run.v1.py",
    "APP_RUNNER": R + "f11a-root-app-run.v1.py",
    "REGRESSION_RUNNER": R + "f11a-regression-run.v1.py",
    "LEGACY_IMAGE_RUNNER": R + "f11a-root-image-run.v1.py",
    "ADDITIONAL_RUNNER": R + "f11a-image-additional-run.v1.py",
    "GUARD": R + "f04a-executor-run.v1.py",
    "FIXTURE_BUDGET": R + "f05b-fixture-budget.v1.py",
    "SUPERVISOR": "scripts/run_local_memory_hardening_tests.py",
    "HARNESS": "distribution/macos-local-memory/tests/test_versioned_safe_index_e2e.py",
    "BOOTSTRAP": "distribution/macos-local-memory/app/bootstrap.py",
    "READER": "distribution/macos-local-memory/engine/build_adaptive_semantic_graph.py",
    "ADAPTER": "scripts/adapt_layer1_to_local_memory.py",
    "CHUNKING": "scripts/evidence_text_chunking.py",
    "INTEGRITY": "scripts/intermediate_build_integrity.py",
    "LEXICAL": "scripts/lexical_search_common.py",
    "OCR": "scripts/local_image_ocr.py",
    "VLM": "scripts/local_visual_observation.py",
    "DOCUMENT_SCHEMA": "schemas/document.schema.json",
    "RELATION_SCHEMA": "schemas/relation.schema.json",
    "PACKAGE_BUILD_READ_ONLY": "distribution/macos-local-memory/build/build_package.sh",
    "GENERATOR": R + "f11a-final-artifact-generator.v3.py",
    "GENERATOR_V1": R + "f11a-final-artifact-generator.v1.py",
    "GENERATION_ERROR_V1": R + "f11a-artifact-generation-attempt-001.log",
}
PINS = {
    "COLLATERAL_RUNNER": "5a5c78e853aa3b611c67d482d24be82330ec9c3154a88135bb8b6538dd2716d1",
    "COLLATERAL_PREFLIGHT": "9296f68a5bb939f855e042566993cb95a37fcf1ed8b8dda2b38938ade704c9d4",
    "COLLATERAL_F05_ROUTE": "e13a9c6d264a70a47bb35e84c037f97a49ea79513b512936bdd290edef78bfae",
    "COLLATERAL_F18_ROUTE": "16bacec1eca35d00f99ac6f03b4971e79a128ededd98d1e96abae0829c0e1e38",
    "FOCUSED_TEST": "e6f051e4dbf1aea266b7c7cb3f62511c9f499a5d66d11a4f051b223b408a2e32",
    "LINEAGE_TEST_RUN001": "26f1624c0dcdddacd86f8cf21253247db9f3f330d764c9b9e4cad80f8e41f4d0",
    "MIGRATION_TEST": "c0b4774ecbc9e45fa17b0a983c9f9f23c1725001972ecd3026c5f5cf03fe8c0c",
    "SECURITY_TEST": "ae9d957baacdd46b1225f8e6f10e4909fad153e412e6a622e4aa1bd25cb5be6d",
    "SECURITY_BUILDER": "3d657ee201022b43429ac765f18c4c3067b96c3962d0178097978ecffe90c2ad",
    "SECURITY_VALIDATOR": "8c64cf146c076321e08b163a101dcf0fa12808bba0756966fc48abec1d0a0201",
    "ANSWER_READER": "33f2b25e9d434e00b216be162d3e60dd8d322409cb108d07e26e455f4a1f34b2",
    "LINEAGE_VERSION_SCOPE": "0cf546851da53863f01a848324dbcdbe4e4c8cf8b27cff0a8ff11787a3961647",
    "LINEAGE_VERSION_GOLD": "f06011075ae61d6d079e84d8506ca4582e8c8952058538b217e3d1bb8ac309de",
    "LINEAGE_VERSION_RUNNER": "2d38fdaeb4305debd86afdad95e9f1360a34b0dd09694e718266cd37e009281b",
    "LINEAGE_DIAGNOSIS": "a0462aa11cfb77a0dc0fc52f07d113ef9e6deeaae249664e176b8d9e29a1a490",
    "REFRESH_PLAN": "65ed85dd242a46cfd51c4e20069b3d0e7b7e8e6c38b7335d5e00788a2253bc5b",
    "PARSER_CONTROLS": "3ae2d7707f7e21025004d28509e39658a6c280a3cdc849953bf284c73a571c98",
    "PARSER_SUBTEST_CONTROLS": "a1672a1ab84ed31347ea67460772db47f9eadb949cb713695650866314a1b08b",
    "DRAFT_ARTIFACT_V1": "b0b63af872e7b669f791941bb8938e1a813f81e38c37c4f4aeece58f1f4b1843",
    "DRAFT_PACKET_V1": "b5d9bd12bd109432d9fa61c3b40589115610349bfd9a9ab7977f381efda0ba45",
    "DRAFT_HANDOFF_V1": "f93151fd6223732e49f78d00054fcbcaed929cf6a7fb7779d19ce2f63fedb3b8",
    "GENERATION_RESULT_V1": "3eb3a6741355b9ab2ed548d36625902e272707c6af8cdbfa8b80f3b90c857fd3",
    "GENERATOR_V2": "19e9e45ca189f89a3aa28c3e3c6b5059e272605bc30ebb1ce253ab5a0fbe4f2e",
    "GENERATOR_V1": "2ce23cf4863b9a5539eed9415ccec849fd69f5e678050af23572b16501d7669c",
    "GENERATION_ERROR_V1": "65536dcf7a8f0af75617ae996dcee8521de88ecd113b57179d1f4cecda80bd5e",
    "COLLATERAL_FOCUSED_START": "42b3706b236bb7651a3fc771d08202ccc3f4cac5fe306aaab3b0234a95ab5e2a",
    "COLLATERAL_FOCUSED_RESULT": "ad4dfcd19329771bd662ac320a3a5547407a54fd2d960f71ba6c9b8ed9eadb8c",
    "COLLATERAL_FOCUSED_LOG": "e78b6061f8dfad7da0680af18163e6976157f9f5c0eda650637664d187c03808",
    "COLLATERAL_LINEAGE_START": "32f57e4fa487e19ca417859ff63b15d13dcec692d4789fc76560d3a08cc29b3d",
    "COLLATERAL_LINEAGE_RESULT": "d2fd145407cde83f5ae43a43c6e57e7c3bb23774e86c3dbd08285204edeb0b42",
    "COLLATERAL_LINEAGE_LOG": "f76a721c2110cf1f388479f2edcc6c1337c9f8fcb14773fa56b9d54149f01cd2",
    "COLLATERAL_MIGRATION_START": "208366a36707508d056702c24c95c4bd3cdd02e4e826c7406c6bc8137f1d3dab",
    "COLLATERAL_MIGRATION_RESULT": "3f637d5071183c7240c876fed02bc8af332e19e139fb0bbd02b56f2ecd7b5b2a",
    "COLLATERAL_MIGRATION_LOG": "8baf4e5f75850857836a95743f093978776d15542b98591cca6b898c6a0583b2",
    "COLLATERAL_SECURITY_START": "5c905c304329f0d8dfc6ac85397c25163cd49b0268af51728dc04a747c7b8ee6",
    "COLLATERAL_SECURITY_RESULT": "da02fd6c3aa05d6f56f4e061eb22609e22f62e19136aa5f46921634a16e5452e",
    "COLLATERAL_SECURITY_LOG": "f65ab176f9aba9feb14f60a47825a91a538519f7d5ed7d763012397df20280a3",
    "LINEAGE_VERSION_START": "9971ce106789d18d388b1a55810eeff502189f767474a8fe0f6a4d5aa648e14d",
    "LINEAGE_VERSION_RESULT": "2efea799367558707d1e7c3053ab4e0a84f07291e73589ee371fd5b2b6232e4a",
    "LINEAGE_VERSION_LOG": "a992412ab9e1b465477deb919a694405e3eca039d0484d5732891d6d653cc3a8",
    "CONTRACT": "20dfd742490462e101ce952e661c6d23a5a40ce0b89b3cf921204b973fc66d15",
    "ADDENDUM": "2542f29b2776127a5d919639c69505d31b4efdf46968d9ff80a0af7d930f6f8f",
    "IMAGE_GATE": "0b9e6252a549c29989f6209e5a2258f3a5edbfe5c10098fe807f919bca475d4b",
    "IMAGE_DESIGN": "b63e7fdc3161aed6cb61466770ecfe720018f8872df429a357dbb9817b9afc48",
    "METADATA_TEST": "ab2256c31b04bc8e8ec9a3c5c294d1e1d98071e38e9969c62bee3718c95bc41e",
    "METADATA_GOLD1": "0b80382980a130cc52a58108ed5d89df5f0bc4249af39a2432d650078a966daa",
    "METADATA_GOLD2": "ab2256c31b04bc8e8ec9a3c5c294d1e1d98071e38e9969c62bee3718c95bc41e",
    "APP_TEST": "b254860584650f376400a50d3890b9cc6074633427a565787b91aae11965496a",
    "INDEX_TEST": "55dc6665d9fbfc9bd6721898ea7be73606560f8a7d0e2fc61e9a05839c222296",
    "IMAGE_TEST": "75573e3ba7ea4a464b20feaf09b6a9e4d4a9133ceabe519399785f9638046913",
    "LEGACY_IMAGE_TEST": "892658aa919cad1dd35a7fdc19aba9c8b0ef1b931e83f3137fc6112b5a150508",
}
# source_id, directory, observed method count, claim role (not outcome authority).
RUNS = (
    ("RED_INITIAL", "f11a-executor-red-initial-001", 9, "historical_true_semantic_RED"),
    ("RED_ROOT", "f11a-root-red-001", 4, "historical_true_semantic_RED"),
    ("OLD_INITIAL", "f11a-executor-green-initial-001", 9, "historical_before_image_correction"),
    ("OLD_POST", "f11a-executor-post-green-001", 21, "historical_before_image_correction"),
    ("OLD_RESIDUAL", "f11a-executor-residual-green-001", 3, "historical_residual_witness_not_defense"),
    ("OLD_ROOT", "f11a-root-green-001", 4, "historical_before_corrections"),
    ("OLD_APP_ERROR", "f11a-root-app-001", 4, "historical_setup_regression_not_defense"),
    ("OLD_IMAGE_ERROR", "f11a-root-images-001", 3, "historical_setup_regression_not_defense"),
    ("RED_INDEX", "f11a-regression-app-001", 9, "historical_true_semantic_RED"),
    ("RED_IMAGE", "f11a-regression-image-001", 3, "historical_baseline_mixed_failure_negatives_not_reached"),
    ("GREEN_INDEX", "f11a-regression-app-002", 9, "current_index_only_pure_check"),
    ("OLD_APP_GREEN", "f11a-root-app-002", 4, "historical_after_index_before_image_correction"),
    ("GREEN_IMAGE", "f11a-regression-image-002", 3, "current_image_semantic_controls"),
    ("GREEN_LEGACY_IMAGE", "f11a-root-images-002", 3, "current_legacy_mocked_image_controls"),
    ("FINAL_INITIAL", "f11a-final-initial-001", 9, "current_metadata_acceptance_candidate"),
    ("FINAL_POST", "f11a-final-post-001", 21, "current_metadata_acceptance_candidate"),
    ("FINAL_RESIDUAL", "f11a-final-residual-001", 3, "current_residual_witness_not_defense"),
    ("FINAL_APP", "f11a-root-app-003", 4, "current_app_gate_migration_rebuild_static_package_controls"),
    ("ADDITIONAL_IMAGE", "f11a-image-additional-001", 6, "current_additional_visual_boundary_controls"),
    ("COLLATERAL_FOCUSED", "f11a-collateral-focused-001", 3, "selected_immutable_lineage_controls"),
    ("COLLATERAL_LINEAGE", "f11a-collateral-lineage-001", 3, "failed_legacy_fixture_not_reclassified_as_expected_failure"),
    ("COLLATERAL_MIGRATION", "f11a-collateral-migration-001", 3, "selected_migration_controls"),
    ("COLLATERAL_SECURITY", "f11a-collateral-security-001", 1, "selected_safe_fanin_control"),
    ("LINEAGE_VERSION", "f11a-lineage-version-001", 2, "current_literal_positive_and_historical_exact_rejection"),
)

# Epoch is source provenance/context, never a substitute for observed outcome.
# Run receipts do not contain product hashes. These epoch labels rely on saved
# phase packets and root's freeze context, not a cryptographic run-time binding.
RUN_EPOCH = {
    "RED_INITIAL": "pre_implementation", "RED_ROOT": "pre_implementation",
    "OLD_INITIAL": "initial_implementation_before_image",
    "OLD_POST": "initial_implementation_before_image",
    "OLD_RESIDUAL": "initial_implementation_before_image",
    "OLD_ROOT": "initial_implementation_before_corrections",
    "OLD_APP_ERROR": "initial_implementation_before_corrections",
    "OLD_IMAGE_ERROR": "initial_implementation_before_image",
    "RED_INDEX": "initial_implementation_before_index",
    "RED_IMAGE": "after_index_before_image",
    "GREEN_INDEX": "current_index_only_subset",
    "OLD_APP_GREEN": "after_index_before_image",
    "GREEN_IMAGE": "current_eleven_product_freeze",
    "GREEN_LEGACY_IMAGE": "current_eleven_product_freeze",
    "FINAL_INITIAL": "current_eleven_product_freeze",
    "FINAL_POST": "current_eleven_product_freeze",
    "FINAL_RESIDUAL": "current_eleven_product_freeze",
    "FINAL_APP": "current_eleven_product_freeze",
    "ADDITIONAL_IMAGE": "current_eleven_product_freeze",
    "COLLATERAL_FOCUSED": "current_eleven_product_freeze",
    "COLLATERAL_LINEAGE": "current_eleven_product_freeze",
    "COLLATERAL_MIGRATION": "current_eleven_product_freeze",
    "COLLATERAL_SECURITY": "current_eleven_product_freeze",
    "LINEAGE_VERSION": "current_eleven_product_freeze",
}
# Immutable observed footer counts, not expectedFailure annotations, predictions
# made before execution, or retroactive semantic approval of old ERROR results.
FAILED_RECEIPTS = {
    "RED_INITIAL": (8, 0), "RED_ROOT": (10, 0),
    "OLD_APP_ERROR": (0, 3), "OLD_IMAGE_ERROR": (0, 1),
    "RED_INDEX": (8, 0), "RED_IMAGE": (2, 7), "COLLATERAL_LINEAGE": (0, 1),
}
COLLATERAL_METHODS = {
    "focused": (
        "test_first_creation_and_repeat_read_only_validation",
        "test_source_and_reader_failures_preserve_lineage",
        "test_projector_rejects_failed_revalidation_even_with_saved_pass",
    ),
    "lineage": (
        "test_unsharded_table_row_promotes_exact_stable_fan_in",
        "test_native_section_contains_requires_real_heading_evidence",
        "test_full_validator_publishes_only_after_pass",
    ),
    "migration": (
        "test_existing_step6_config_requires_reader_migration_without_mutation",
        "test_current_generation_matches_code_processing_and_schema_bytes",
        "test_builder_adapter_processing_or_schema_byte_change_requires_migration",
    ),
    "security": ("test_partial_exclusion_holds_mixed_fan_in_atomically",),
}
LINEAGE_VERSION_METHODS = (
    "test_current_version_preserves_complete_original_fan_in_assertions",
    "test_historical_version_is_rejected_with_exact_provenance_error",
)


def require(condition, message):
    if not condition:
        raise ValueError(message)


def strict_json(raw):
    def object_pairs(pairs):
        value = {}
        for key, item in pairs:
            require(key not in value, "duplicate JSON key")
            value[key] = item
        return value
    def constant(value):
        raise ValueError("nonfinite JSON constant: " + value)
    return json.loads(raw, object_pairs_hook=object_pairs, parse_constant=constant)


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")


def patch_text(text, patch, inverse=False):
    source, lines, output = text.splitlines(keepends=True), patch.splitlines(keepends=True), []
    cursor, index = 0, 2
    while index < len(lines):
        match = re.match(r"@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@", lines[index])
        require(match is not None, "malformed isolated patch")
        start = int(match.group(2 if inverse else 1)) - 1
        require(start >= cursor, "overlapping patch hunks")
        output.extend(source[cursor:start])
        cursor, index = start, index + 1
        while index < len(lines) and not lines[index].startswith("@@ "):
            line = lines[index]
            index += 1
            prefix, body = line[0], line[1:]
            if inverse:
                prefix = {"+": "-", "-": "+"}.get(prefix, prefix)
            require(prefix in {" ", "+", "-"}, "unexpected patch line")
            if prefix in {" ", "-"}:
                require(cursor < len(source) and source[cursor] == body, "patch input differs")
                cursor += 1
            if prefix in {" ", "+"}:
                output.append(body)
    return "".join(output + source[cursor:])


def literal_assignment(tree, name):
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
            return ast.literal_eval(node.value)
    raise ValueError("missing literal assignment: " + name)


def parse_run_log(text, expected_methods=None):
    """Parse only bounded unittest progress/terminal metadata; not traceback truth."""
    footer = re.search(
        r"^Ran (\d+) tests? in [0-9.]+s\r?\n\r?\n"
        r"(OK|FAILED)(?: \(([^\r\n]*)\))?\r?\n?\Z", text, re.MULTILINE)
    require(footer is not None, "invalid terminal footer")
    counts = {"failures": 0, "errors": 0, "skips": 0,
              "expected_failures": 0, "unexpected_successes": 0}
    labels = {"failures": "failures", "errors": "errors", "skipped": "skips",
              "expected failures": "expected_failures",
              "unexpected successes": "unexpected_successes"}
    seen = set()
    if footer.group(3):
        for item in footer.group(3).split(", "):
            match = re.fullmatch(r"([a-z ]+)=([1-9][0-9]*)", item)
            require(match is not None and match.group(1) in labels,
                    "invalid terminal detail")
            label = labels[match.group(1)]
            require(label not in seen, "duplicate terminal detail")
            seen.add(label)
            counts[label] = int(match.group(2))
    progress = text[:footer.start()].split("\n====", 1)[0].split("\n----", 1)[0]
    budget = re.findall(r"^F05b explicit fixture budget: ([^\r\n]+)\r?$",
                        progress, re.MULTILINE)
    require(len(budget) <= 1, "duplicate fixture footer")
    if budget:
        require(isinstance(strict_json(budget[0]), dict), "invalid fixture footer")
        progress = re.sub(r"^F05b explicit fixture budget: [^\r\n]+\r?\n?",
                          "", progress, flags=re.MULTILINE)
    header = re.compile(r"(?<![A-Za-z0-9_])(test_[A-Za-z0-9_]+) \(([A-Za-z0-9_.]+)\)")
    # Python 3.14 prints indented subtest progress after its parent. Keep those
    # events distinct from methods, require a real preceding matching parent,
    # and never remove unrecognized indented text as if it were a subtest.
    clean, parent = [], None
    for line in progress.splitlines(keepends=True):
        subtest = re.fullmatch(
            r"  (test_[A-Za-z0-9_]+) \(([A-Za-z0-9_.]+)\)"
            r" \([^\r\n]*\) \.\.\. (FAIL|ERROR)\r?\n?", line)
        if subtest:
            require(parent == subtest.group(1, 2), "orphan or mismatched subtest progress")
            continue
        matches = list(header.finditer(line))
        if matches:
            parent = matches[-1].group(1, 2)
        clean.append(line)
    progress = "".join(clean)
    headers = list(header.finditer(progress))
    require(headers and not progress[:headers[0].start()].strip(),
            "missing or prefixed method progress")
    methods = [item.group(1) for item in headers]
    require(len(methods) == len(set(methods)), "duplicate progress method")
    outcomes = []
    for index, item in enumerate(headers):
        tail = progress[item.end():headers[index + 1].start()
                        if index + 1 < len(headers) else len(progress)]
        plain = re.fullmatch(r" \.\.\.(?: (ok|FAIL|ERROR))?\s*", tail)
        docstring = re.fullmatch(r"\r?\n[^\r\n]+ \.\.\. (ok|FAIL|ERROR)\s*", tail)
        require(plain is not None or docstring is not None, "invalid method progress tail")
        outcomes.append((plain if plain is not None else docstring).group(1))
    if expected_methods is not None:
        require(methods == list(expected_methods), "selected method mismatch")
    require(len(methods) == int(footer.group(1)), "method/footer count mismatch")
    if footer.group(2) == "OK":
        require(not any(counts.values()) and all(value == "ok" for value in outcomes),
                "OK progress inconsistent")
    else:
        require(counts["failures"] + counts["errors"] + counts["unexpected_successes"] > 0,
                "FAILED footer lacks failure/error")
    return {"methods": methods, "outcomes": outcomes, "tests": int(footer.group(1)),
            "terminal": footer.group(2), **counts}


def check_parser_controls(raw):
    checks, names = [], set()
    for source_id in ("PARSER_CONTROLS", "PARSER_SUBTEST_CONTROLS"):
        gold = strict_json(raw[source_id])
        require(gold.get("schema_version") == "1.0", "parser gold schema mismatch")
        for case in gold["cases"]:
            require(case["name"] not in names, "duplicate parser control name")
            names.add(case["name"])
            if "error" in case:
                try:
                    parse_run_log(case["log"], case["methods"])
                except ValueError as exc:
                    require(str(exc) == case["error"], "literal parser error mismatch: " + case["name"])
                else:
                    raise ValueError("parser negative accepted: " + case["name"])
            else:
                require(parse_run_log(case["log"], case["methods"]) == case["expected"],
                        "literal parser result mismatch: " + case["name"])
            checks.append({"name": case["name"], "basis": source_id,
                           "kind": "negative" if "error" in case else "positive"})
    require(len(checks) == 16, "parser gold selection changed")
    return checks


def main():
    specs = dict(FIXED)
    roles = {key: "selected_dependency_or_evidence" for key in specs}
    deltas = []
    for source_id, short, path, digest in PRODUCTS:
        specs[source_id], roles[source_id], PINS[source_id] = path, "current_product", digest
        if source_id == "INDEX":
            specs["INDEX_BEFORE"] = R + "f11a-app-regression-before-index.v1.py"
            specs["INDEX_AFTER"] = R + "f11a-index-regression-after.v1.py"
            specs["INDEX_DELTA"] = R + "f11a-index-regression-delta.v1.patch"
            deltas.append(("INDEX", "INDEX_BEFORE", "INDEX_DELTA", "integration_correction1_index"))
            continue
        extension = "json" if source_id in {"ESCHEMA", "SSCHEMA"} else "py"
        specs[source_id + "_ORIGINAL"] = R + "f11a-executor-before-" + short + ".v1." + extension
        specs[source_id + "_INITIAL_DELTA"] = R + "f11a-executor-" + short + "-delta.v1.diff"
        initial_after = source_id
        if source_id in IMAGE_IDS:
            initial_after = source_id + "_IMAGE_BEFORE"
            for stage in ("before", "after"):
                specs[source_id + "_IMAGE_" + stage.upper()] = R + "f11a-image-repair-" + stage + "-" + short + ".v1.py"
            specs[source_id + "_IMAGE_DELTA"] = R + "f11a-image-repair-" + short + "-delta.v1.diff"
            deltas.append((source_id, initial_after, source_id + "_IMAGE_DELTA", "integration_correction1_image"))
        deltas.append((initial_after, source_id + "_ORIGINAL", source_id + "_INITIAL_DELTA", "initial_implementation_historical_target"))
    deltas.append(("METADATA_GOLD2", "METADATA_GOLD1", "METADATA_GOLD_DELTA", "preimplementation_additive_gold"))
    for run_id, directory, _, _ in RUNS:
        for suffix, filename in (("RESULT", "result.json"), ("LOG", "unittest.log"), ("START", "started.json")):
            specs[run_id + "_" + suffix] = R + directory + "/" + filename
            roles[run_id + "_" + suffix] = "run_receipt_or_log"
    require(len(specs) <= MAX_FILES and len(set(specs.values())) == len(specs), "source count/duplicate path")
    raw, sources, trees, total = {}, [], {}, 0
    for source_id, relative in specs.items():
        require(time.monotonic() - START < 30, "generator time bound")
        parts = PurePosixPath(relative)
        require(not parts.is_absolute() and ".." not in parts.parts, "path escapes scope")
        path = ROOT / relative
        require(not path.is_symlink() and path.resolve(strict=True).is_relative_to(ROOT), "source path escape/symlink")
        with path.open("rb") as handle:
            value = handle.read(MAX_FILE + 1)
        total += len(value)
        require(len(value) <= MAX_FILE and total <= MAX_TOTAL, "generator byte bound")
        digest = hashlib.sha256(value).hexdigest()
        require(source_id not in PINS or digest == PINS[source_id], "frozen source drift: " + source_id)
        raw[source_id] = value
        if path.suffix == ".py":
            trees[source_id] = ast.parse(value.decode("utf-8"), filename=relative)
        elif path.suffix == ".json":
            strict_json(value)
        sources.append({"id": source_id, "path": str(path), "relative_path": relative,
                        "sha256": digest, "bytes": len(value),
                        "role": roles.get(source_id, "historical_snapshot_or_delta")})
    source_by_id = {item["id"]: item for item in sources}
    for source_id, short, _, _ in PRODUCTS:
        if source_id in IMAGE_IDS:
            require(raw[source_id] == raw[source_id + "_IMAGE_AFTER"], "current/image after mismatch")
    require(raw["INDEX"] == raw["INDEX_AFTER"], "current index after mismatch")
    initial_before = strict_json(raw["INITIAL_BEFORE_MANIFEST"])
    by_path = {item["relative_path"]: item for item in sources}
    for item in initial_before["sources"]:
        require(item["before_path"] in by_path, "unselected historical before")
        require(by_path[item["before_path"]]["sha256"] == item["before_sha256"], "historical before hash mismatch")
    delta_checks = []
    for target, before, patch, phase in deltas:
        a, b, p = (raw[key].decode("utf-8") for key in (before, target, patch))
        require(patch_text(a, p) == b and patch_text(b, p, True) == a, "delta forward/inverse mismatch: " + patch)
        delta_checks.append({"target_id": target, "before_id": before, "delta_id": patch,
                             "phase": phase, "forward_exact": True, "inverse_exact": True})
    require(raw["METADATA_TEST"] == raw["METADATA_GOLD2"], "permanent gold changed")
    # Drop only the predeclared additive class; compare the entire remaining AST.
    current_tree = trees["METADATA_TEST"]
    retained = ast.Module(body=[n for n in current_tree.body if not (
        isinstance(n, ast.ClassDef) and n.name == "NotebookMetadataPostAPITests")], type_ignores=[])
    require(ast.dump(retained, include_attributes=False) == ast.dump(trees["METADATA_GOLD1"], include_attributes=False), "initial complete gold AST changed")
    selections = {
        "initial": literal_assignment(trees["INITIAL_RUNNER"], "METHODS"),
        **literal_assignment(trees["POST_RUNNER"], "METHODS"),
        "app": literal_assignment(trees["APP_RUNNER"], "METHODS"),
        "index": literal_assignment(trees["INDEX_TEST"], "METHOD_NAMES"),
        "image": literal_assignment(trees["IMAGE_TEST"], "METHODS"),
        "legacy_image": literal_assignment(trees["LEGACY_IMAGE_RUNNER"], "METHODS"),
        "additional_image": literal_assignment(trees["IMAGE_ADDITIONAL_TEST"], "METHODS"),
    }
    require([len(selections[k]) for k in ("initial", "post", "residual", "app", "index", "image", "legacy_image", "additional_image")] == [9,21,3,4,9,3,3,6], "selected method counts changed")
    require(not set(selections["initial"]) & set(selections["post"]) and not set(selections["post"]) & set(selections["residual"]), "acceptance/residual overlap")
    collateral_targets = literal_assignment(trees["COLLATERAL_RUNNER"], "TARGETS")
    test_ids = {"focused": "FOCUSED_TEST", "lineage": "LINEAGE_TEST_RUN001",
                "migration": "MIGRATION_TEST", "security": "SECURITY_TEST"}
    require(set(collateral_targets) == set(COLLATERAL_METHODS), "collateral scope changed")
    for mode, methods in COLLATERAL_METHODS.items():
        relative, class_name, digest, selected = collateral_targets[mode]
        source_id = test_ids[mode]
        require(relative == specs[source_id] and digest == PINS[source_id],
                "collateral target path/hash mismatch")
        require(tuple(selected) == methods, "collateral literal method selection changed")
        classes = [node for node in trees[source_id].body
                   if isinstance(node, ast.ClassDef) and node.name == class_name]
        require(len(classes) == 1, "collateral class absent/duplicate")
        present = [node.name for node in classes[0].body if isinstance(node, ast.FunctionDef)]
        require(all(present.count(method) == 1 for method in methods), "collateral method missing")
        selections["collateral_" + mode] = methods
    require(literal_assignment(trees["LINEAGE_VERSION_RUNNER"], "METHODS") == LINEAGE_VERSION_METHODS,
            "lineage version runner methods changed")
    require(literal_assignment(trees["LINEAGE_VERSION_GOLD"], "EXPECTED_SOURCE") == PINS["LINEAGE_TEST_RUN001"],
            "lineage original source binding changed")
    selections["lineage_version"] = LINEAGE_VERSION_METHODS
    parser_checks = check_parser_controls(raw)
    expected_run_selection = {
        "FINAL_INITIAL": "initial", "FINAL_POST": "post", "FINAL_RESIDUAL": "residual",
        "FINAL_APP": "app", "GREEN_INDEX": "index", "GREEN_IMAGE": "image",
        "GREEN_LEGACY_IMAGE": "legacy_image", "ADDITIONAL_IMAGE": "additional_image",
        "COLLATERAL_FOCUSED": "collateral_focused", "COLLATERAL_LINEAGE": "collateral_lineage",
        "COLLATERAL_MIGRATION": "collateral_migration", "COLLATERAL_SECURITY": "collateral_security",
        "LINEAGE_VERSION": "lineage_version",
    }
    require(len(RUNS) == 24 and set(RUN_EPOCH) == {r[0] for r in RUNS}, "run epoch coverage changed")
    run_checks = []
    for run_id, directory, count, claim_role in RUNS:
        result, started = strict_json(raw[run_id + "_RESULT"]), strict_json(raw[run_id + "_START"])
        log = raw[run_id + "_LOG"]
        text = log.decode("utf-8")
        expected_methods = selections.get(expected_run_selection.get(run_id))
        parsed = parse_run_log(text, expected_methods)
        require(parsed["tests"] == count == result["tests_reported"], "run/footer count mismatch")
        require(result["log_sha256"] == hashlib.sha256(log).hexdigest() and result["log_bytes"] == len(log),
                "log integrity mismatch")
        require(result["log_path"] == str(ROOT / R / directory / "unittest.log"), "result log path mismatch")
        require(result["command"] == started["command"] and result["started_at"] == started["started_at"],
                "start/result mismatch")
        require(result["timeout_seconds"] == started["timeout_seconds"] == 30
                and result["max_log_bytes"] == started["max_log_bytes"] == 1048576,
                "unexpected run bounds")
        failures, errors = FAILED_RECEIPTS.get(run_id, (0, 0))
        expected_status, expected_exit, expected_terminal = (
            ("failed", 1, "FAILED") if failures or errors else ("passed", 0, "OK"))
        require((result["status"], result["exit_code"], parsed["terminal"])
                == (expected_status, expected_exit, expected_terminal), "recorded run outcome differs")
        require((parsed["failures"], parsed["errors"]) == (failures, errors),
                "recorded failure/error count differs")
        require(parsed["skips"] == parsed["expected_failures"] == parsed["unexpected_successes"] == 0
                and result["skipped_reported"] == result["expected_failures_reported"] == 0,
                "skip/expected failure is not an observed PASS")
        if run_id == "COLLATERAL_LINEAGE":
            require(parsed["outcomes"] == ["ERROR", "ok", "ok"], "old lineage outcomes changed")
            exact_error = ("ValueError: lineage_search_unit_provenance_invalid:"
                           "su_d59a0eeffdd8f7a32a06d66ff2aeb9fe\n")
            require(text.count(exact_error) == 1, "old lineage exact error changed")
        basis = [run_id + "_START", run_id + "_RESULT", run_id + "_LOG"]
        run_checks.append({
            "id": run_id, "run_directory": R + directory, "source_epoch": RUN_EPOCH[run_id],
            "claim_role": claim_role, "basis": basis, "status": result["status"],
            "exit_code": result["exit_code"], **parsed,
            "receipt_expectation_policy": "matches saved observation; does not relabel original failure as expectedFailure",
            "input_binding": "receipt has no product hashes; current selected hashes and saved phase context are separate evidence",
            "elapsed_seconds": result["elapsed_seconds"], "log_sha256": result["log_sha256"],
            "limits": {"seconds": 30, "log_bytes": 1048576},
            "fixture_footer": next((line for line in text.splitlines()
                                    if line.startswith("F05b explicit fixture budget:")), None),
        })
    original_ids = [item[0] for item in RUNS[:19]]
    collateral_ids = ["COLLATERAL_FOCUSED", "COLLATERAL_LINEAGE",
                      "COLLATERAL_MIGRATION", "COLLATERAL_SECURITY"]
    by_run = {run["id"]: run for run in run_checks}
    require(sum(by_run[key]["outcomes"].count("ok") for key in collateral_ids) == 9
            and sum(by_run[key]["outcomes"].count("ERROR") for key in collateral_ids) == 1,
            "collateral observed method outcomes changed")
    run_groups = {
        "original_draft_runs_retained": {"run_ids": original_ids, "runs": 19},
        "collateral": {"run_ids": collateral_ids, "runs": 4, "methods": 10,
                       "method_passes": 9, "method_errors": 1, "batch_passes": 3, "batch_failures": 1},
        "lineage_version_clarification": {"run_ids": ["LINEAGE_VERSION"], "runs": 1,
                                          "methods": 2, "method_passes": 2},
        "current_metadata_acceptance_candidate": {"run_ids": ["FINAL_INITIAL", "FINAL_POST"], "methods": 30},
        "residual_witnesses_not_defenses": {"run_ids": ["FINAL_RESIDUAL"], "methods": 3},
    }
    ast_index = []
    selected_python = {p[0] for p in PRODUCTS} | {"METADATA_TEST", "APP_TEST", "IMAGE_TEST", "IMAGE_ADDITIONAL_TEST", "INDEX_TEST", "FOCUSED_TEST", "LINEAGE_TEST_RUN001", "MIGRATION_TEST", "SECURITY_TEST", "LINEAGE_VERSION_GOLD"}
    names = {"read_notebook_bytes", "parse_notebook_bytes", "validate_notebook_state", "classify_notebook_evidence", "notebook_evidence_state", "notebook_document_binding", "notebook_binding_report", "_visual_origin_errors", "_validate_notebook_visual_text", "notebook_parent_lookup", "notebook_search_unit_contract_errors", "consume", "add_direct_text", "validate", "validate_report", "main", "_is_explicit_verified_structural", "_validate_attested_smartart_connection", "derive_native_structural_relations", "process_file"}
    for source_id in selected_python & trees.keys():
        for node in ast.walk(trees[source_id]):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and (node.name in names or node.name.startswith("test_")):
                ast_index.append({"source_id": source_id, "name": node.name, "line": node.lineno,
                                  "end_line": node.end_lineno, "ast_sha256": hashlib.sha256(ast.dump(node, include_attributes=False).encode()).hexdigest()})
    ast_index.sort(key=lambda item: (item["source_id"], item["line"]))
    artifact = graph(sources)
    known = set(specs) | {n["id"] for n in artifact["nodes"]} | {e["id"] for e in artifact["edges"]}
    for item in artifact["nodes"] + artifact["edges"]:
        require(bool(item["basis"]) and set(item["basis"]) <= set(specs), "invalid node/edge basis")
    for edge in artifact["edges"]:
        require(edge["source"] in known and edge["target"] in known, "invalid edge endpoint")
    for path in artifact["required_paths"]:
        require(set(path["node_ids"] + path["edge_ids"] + path["basis"]) <= known, "invalid required path ref")
    packet = {"schema_version":"1.0", "task_id":TASK, "status":"draft_pending_root_verification_not_audit_dispatch",
              "source_policy":"explicit selected paths; historical manifests do not define current sources",
              "files":sources, "runs":run_checks, "run_groups":run_groups, "isolated_deltas":delta_checks, "method_selections":selections,
              "parser_controls":parser_checks,
              "current_product_ids":[p[0] for p in PRODUCTS], "ast_index":ast_index,
              "mechanical_checks":{"pins_verified":len(PINS), "files":len(sources), "read_bytes":total,
                                   "all_delta_forward_inverse_exact":True, "initial_gold_complete_ast_unchanged":True,
                                   "permanent_gold2_byte_exact":True, "run_hash_footer_method_checks":True, "literal_parser_controls":len(parser_checks),
                                   "currentness_and_outcome_separate":True, "old_lineage_error_retained":True},
              "limits":{"files":MAX_FILES,"bytes_per_file":MAX_FILE,"total_read_bytes":MAX_TOTAL,"seconds":30,"output_bytes":MAX_OUTPUT},
              "not_claimed":["formal audit PASS", "all dependencies transitively discovered", "new product/test execution", "whole collateral/lineage suite acceptance", "whole V1/F11 completion"]}
    output = encoded({"artifact": artifact, "source_packet": packet})
    require(len(output) <= MAX_OUTPUT, "generator output bound")
    sys.stdout.buffer.write(output)


def graph(sources):
    def node(identifier, text, basis):
        return {"id":identifier, "text":text, "status":"proposed_for_independent_audit", "basis":basis}
    nodes = [
        node("N_GOAL", "F11a certifies only the declared textual Notebook cell/source/saved-output metadata slice. Body correspondence, complete membership, execution/freshness and F11b downstream presentation remain excluded and open.", ["CONTRACT","ADDENDUM","IMAGE_DESIGN"]),
        node("N_PARSE", "Bounded original-byte snapshot supplies both Document digest/length and parsed metadata; preparse byte/token/depth/number limits and strict malformed-JSON/count handling do not execute Notebook code. Missing counts are not fabricated as null.", ["PROBE","MANAGED","METADATA_TEST","FINAL_INITIAL_LOG","FINAL_POST_LOG"]),
        node("N_BIND", "Native/schema-stream/structural-stream intermediate validation checks canonical role/location/ordinal/state against same-read original metadata. Rootless, limits, incomplete and zero-checked states remain explicitly unverified; known malformed metadata fails.", ["PROBE","INATIVE","ISTREAM","ESCHEMA","METADATA_TEST","FINAL_POST_LOG"]),
        node("N_COPY", "Direct Search units copy exact metadata, and both Search validators bind it to referenced Evidence including same-ID alteration and omission. Search alone is not original-byte or body/membership attestation.", ["SBUILD","SNATIVE","SSTREAM","SSCHEMA","FINAL_INITIAL_LOG","FINAL_POST_LOG","FINAL_RESIDUAL_LOG"]),
        node("N_REPORT_GATE", "PASS/UNVERIFIED/FAIL and counts remain distinct. Actual adaptive run_tool observes intermediate exit2 before Search/adapter; tested failed generation leaves prior CONFIG and generation bytes intact. Existing counts wrapper returns only on PASS.", ["INATIVE","ISTREAM","READER","BOOTSTRAP","APP_TEST","FINAL_APP_LOG","FINAL_POST_LOG"]),
        node("N_VISUAL", "Only producer-shaped provisional visual children with real same-document image parent and strict origin/location contracts are excluded from Notebook metadata counts. Deferred image lookup, unlocated transcript, attachment/dataURI and missing/cross-document parent checks have bounded synthetic controls; visual text stays provisional.", ["PROBE","SBUILD","INATIVE","ISTREAM","SNATIVE","SSTREAM","IMAGE_DESIGN","IMAGE_TEST","IMAGE_ADDITIONAL_TEST","GREEN_IMAGE_LOG","ADDITIONAL_IMAGE_LOG"]),
        node("N_IDENTITY", "The managed0.12.0 identity is recognized at adaptive and projector native/SmartArt pins without removing old versions or loosening support/source comparisons. The original app3 setup errors and independent index semantic RED are retained; current index9 and final app4 pass.", ["MANAGED","AVALIDATOR","INDEX","INDEX_DELTA","INDEX_TEST","RED_INDEX_LOG","GREEN_INDEX_LOG","FINAL_APP_LOG"]),
        node("N_COMPAT", "App4 and separately selected migration3 exercise prior-generation preservation, explicit rebuild and processing/schema identity. Static package checks cover already shipped Probe/schema paths; checkout stream parity is not proof of a built app or the missing packaged streaming Search validator.", ["APP_TEST","FINAL_APP_LOG","MIGRATION_TEST","COLLATERAL_MIGRATION_RESULT","COLLATERAL_MIGRATION_LOG","PACKAGE_BUILD_READ_ONLY","BOOTSTRAP","CONTRACT"]),
        node("N_EVIDENCE", "The original19 run records remain; four collateral runs add9 method PASS and1 ERROR, and a separate lineage-version run adds2 PASS. Current metadata30/app4/image3/legacy-image3/additional-image6 and current-index-only9 stay distinct. Residual3 are not defenses. Neither the old ERROR nor source epochs are rewritten; receipts do not themselves pin product bytes at run time.", ["METADATA_METHODS","FINAL_INITIAL_LOG","FINAL_POST_LOG","FINAL_APP_LOG","GREEN_IMAGE_LOG","GREEN_LEGACY_IMAGE_LOG","ADDITIONAL_IMAGE_LOG","GREEN_INDEX_LOG","COLLATERAL_FOCUSED_RESULT","COLLATERAL_LINEAGE_RESULT","COLLATERAL_MIGRATION_RESULT","COLLATERAL_SECURITY_RESULT","LINEAGE_VERSION_RESULT","INITIAL_MANIFEST","INDEX_PACKET","IMAGE_PACKET","DRAFT_PACKET_V1"]),
        node("N_RESIDUAL", "Three passing residual witnesses show coherent body replacement, nonzero record removal and Search-only coherent metadata forgery remain possible under the declared slice. They are not defended attacks or three extra acceptance tests; F11b/body/membership work remains mandatory.", ["CONTRACT","METADATA_TEST","FINAL_RESIDUAL_LOG","IMAGE_DESIGN"]),
        node("N_PENDING", "This revised draft awaits root complete-generator/source/gold/receipt verification and separate-context formal audit. Full lineage and SmartArt pipeline are not accepted by the selected controls. Mechanical JSON/AST/hash/parser checks do not establish semantic correctness or self-approval.", ["CONTRACT","IMPLEMENTATION_GATE","REGRESSION_GATE","IMAGE_GATE","GENERATOR","REFRESH_PLAN","COLLATERAL_PREFLIGHT","LINEAGE_VERSION_SCOPE"]),
        node("N_COLLATERAL", "Focused3 and security1 pass the selected immutable-lineage and safe-fan-in guards under the frozen current phase, with bounded synthetic inputs and mocked embedding. Migration3 is separate. These controls do not certify Notebook metadata propagation into final answers or the entire related suites.", ["FOCUSED_TEST","SECURITY_TEST","COLLATERAL_FOCUSED_RESULT","COLLATERAL_FOCUSED_LOG","COLLATERAL_SECURITY_RESULT","COLLATERAL_SECURITY_LOG","SECURITY_BUILDER","SECURITY_VALIDATOR","ANSWER_READER","INDEX","READER","AVALIDATOR","COLLATERAL_RUNNER","COLLATERAL_F05_ROUTE","COLLATERAL_F18_ROUTE","GUARD","FIXTURE_BUDGET","SUPERVISOR"]),
        node("N_VERSION_SCOPE", "Root clarified current Search0.7.0 exact acceptance versus preserved historical native structural tuples without editing any product or original test. The original lineage batch remains2 PASS+1 ERROR. A separately named literal0.7 positive executes the complete original fan-in assertions; untouched0.6 exact-error rejection is a second PASS. This resolves the selected fixture interpretation only, not whole-lineage acceptance or a new product correction.", ["CONTRACT","LINEAGE_DIAGNOSIS","LINEAGE_VERSION_SCOPE","LINEAGE_TEST_RUN001","LINEAGE_VERSION_GOLD","LINEAGE_VERSION_RUNNER","COLLATERAL_LINEAGE_RESULT","COLLATERAL_LINEAGE_LOG","LINEAGE_VERSION_RESULT","LINEAGE_VERSION_LOG","AVALIDATOR","SBUILD"]),
    ]
    edges = [
        {"id":"E_PARSE_BIND","source":"N_PARSE","target":"N_BIND","relation":"provides same-read source facts and digest authority","basis":["PROBE","MANAGED","INATIVE","ISTREAM"]},
        {"id":"E_BIND_COPY","source":"N_BIND","target":"N_COPY","relation":"original attestation requires this intermediate generation before Evidence-only Search validation","basis":["CONTRACT","READER","INATIVE","ISTREAM","SNATIVE","SSTREAM"]},
        {"id":"E_BIND_GATE","source":"N_BIND","target":"N_REPORT_GATE","relation":"unverified metadata yields exit2 and stops subsequent stages","basis":["ISTREAM","READER","APP_TEST","FINAL_APP_LOG"]},
        {"id":"E_VISUAL_BIND","source":"N_VISUAL","target":"N_BIND","relation":"separates valid provisional visual children from saved textual metadata counts without a label-only exemption","basis":["PROBE","IMAGE_TEST","IMAGE_ADDITIONAL_TEST","GREEN_IMAGE_LOG","ADDITIONAL_IMAGE_LOG"]},
        {"id":"E_IDENTITY_GATE","source":"N_IDENTITY","target":"N_REPORT_GATE","relation":"restores exact current-version structural projection so real app gates are reached","basis":["INDEX","AVALIDATOR","INDEX_TEST","FINAL_APP_LOG"]},
        {"id":"E_GATE_COMPAT","source":"N_REPORT_GATE","target":"N_COMPAT","relation":"failed or incompatible new processing preserves existing publication","basis":["BOOTSTRAP","APP_TEST","FINAL_APP_LOG"]},
        {"id":"E_EVIDENCE_RESIDUAL","source":"N_EVIDENCE","target":"N_RESIDUAL","relation":"separates tested limitations from defended obligations","basis":["METADATA_TEST","METADATA_METHODS","FINAL_RESIDUAL_LOG"]},
        {"id":"E_SCOPE_PENDING","source":"N_GOAL","target":"N_PENDING","relation":"limits proposed acceptance and reserves approval to orchestrator/auditor","basis":["CONTRACT","REGRESSION_GATE","IMAGE_GATE"]},
    ]
    edges.extend([
        {"id":"E_COLLATERAL_COMPAT","source":"N_COLLATERAL","target":"N_COMPAT","relation":"bounds integration compatibility evidence to the selected immutable-lineage, safe-fanin and migration controls","basis":["FOCUSED_TEST","SECURITY_TEST","MIGRATION_TEST","COLLATERAL_FOCUSED_LOG","COLLATERAL_SECURITY_LOG","COLLATERAL_MIGRATION_LOG"]},
        {"id":"E_VERSION_IDENTITY","source":"N_VERSION_SCOPE","target":"N_IDENTITY","relation":"distinguishes the exact current Search pin from historical native producer compatibility without loosening either gate","basis":["CONTRACT","LINEAGE_VERSION_SCOPE","LINEAGE_DIAGNOSIS","AVALIDATOR","SBUILD","LINEAGE_VERSION_GOLD","LINEAGE_VERSION_LOG"]},
    ])
    required = [
        ("G1_G4_SOURCE", ["N_PARSE","N_BIND"], ["E_PARSE_BIND"], ["CONTRACT","METADATA_TEST","FINAL_INITIAL_LOG","FINAL_POST_LOG"]),
        ("G2_G3_BIND_COPY", ["N_BIND","N_COPY"], ["E_BIND_COPY"], ["METADATA_TEST","FINAL_POST_LOG"]),
        ("G5_REPORT_GATE", ["N_BIND","N_REPORT_GATE"], ["E_BIND_GATE"], ["APP_TEST","FINAL_APP_LOG"]),
        ("G6_COMPAT", ["N_IDENTITY","N_COMPAT"], ["E_IDENTITY_GATE","E_GATE_COMPAT"], ["INDEX_TEST","GREEN_INDEX_LOG","APP_TEST","FINAL_APP_LOG","MIGRATION_TEST","COLLATERAL_MIGRATION_RESULT","COLLATERAL_MIGRATION_LOG"]),
        ("COLLATERAL_AND_VERSION", ["N_COLLATERAL","N_COMPAT","N_VERSION_SCOPE","N_IDENTITY"], ["E_COLLATERAL_COMPAT","E_VERSION_IDENTITY"], ["COLLATERAL_PREFLIGHT","LINEAGE_DIAGNOSIS","LINEAGE_VERSION_SCOPE","COLLATERAL_FOCUSED_RESULT","COLLATERAL_SECURITY_RESULT","COLLATERAL_LINEAGE_RESULT","LINEAGE_VERSION_RESULT","LINEAGE_VERSION_LOG"]),
        ("IMAGE_CORRECTION", ["N_VISUAL","N_BIND"], ["E_VISUAL_BIND"], ["IMAGE_GATE","IMAGE_TEST","IMAGE_ADDITIONAL_TEST","GREEN_IMAGE_LOG","ADDITIONAL_IMAGE_LOG"]),
        ("PROVENANCE_AND_SCOPE", ["N_EVIDENCE","N_RESIDUAL","N_GOAL","N_PENDING"], ["E_EVIDENCE_RESIDUAL","E_SCOPE_PENDING"], ["INITIAL_MANIFEST","INDEX_PACKET","IMAGE_PACKET","GENERATOR"]),
    ]
    return {"schema_version":"1.0","task_id":TASK,"artifact_version":2,
            "status":"draft_pending_root_verification_before_formal_audit","audit_repair_round":0,"integration_correction_round":1,
            "task_contract":{"source_ids":["CONTRACT","ADDENDUM","CONTRACT_GATE","IMPLEMENTATION_GATE","REGRESSION_GATE","IMAGE_GATE","IMAGE_DESIGN","LINEAGE_VERSION_SCOPE"],"maximum_repair_cycles":2,"role_separation":"same_model_separate_context"},
            "goal":"Independently review the closed F11a metadata-only implementation and its integration correction, retaining every declared residual and required follow-on.",
            "nodes":nodes,"edges":edges,"sources":sources,
            "required_paths":[{"id":key,"node_ids":ns,"edge_ids":es,"basis":bs,"status":"submitted_for_review_not_self_approved"} for key,ns,es,bs in required],
            "scope_limits":["metadata-only; no raw body/pointer or complete membership attestation","no freshness/execution history inference","visual lineage shape is not source-image membership or model correctness","F11b app/shard/index/retrieval/answer propagation remains mandatory open","all-format/real-model/GUI/package/RSS guarantees excluded","selected collateral9PASS+1ERROR and separate version2PASS are not whole-suite acceptance; full SmartArt pipeline remains unexecuted under the16KiBsource bound"],
            "proposed_output":{"action":"root_verify_then_dispatch_separate_audit","acceptance_requested":"F11a declared slice only, contingent on formal audit and parent deterministic checks","self_approval":False,"whole_F11_or_V1_complete":False}}


if __name__ == "__main__":
    main()
