"""Dated consent literal gold. Root review required before bounded execution.

Seed tests use only existing APIs: a missing future API is never semantic RED.
Post-API tests are separately selected after an explicit implementation gate.
All source/candidate/revision fixtures are synthetic; no document or store IO.
"""
from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "dated_consent_resolver", ROOT / "distribution/macos-local-memory/engine/document_version_resolver.py")
resolver = importlib.util.module_from_spec(spec)
spec.loader.exec_module(resolver)

KEY = "\0手順\0.csv"
ACTOR = "local-ui-human"
DECIDED_AT = "2026-09-09T12:00:00+00:00"
SET_FIELDS = (
    "relative_path", "source_sha256", "mtime_ns", "birthtime_ns", "explicit_years",
    "explicit_versions", "current_markers", "historical_markers", "draft_markers",
)
CANDIDATES = [
    {"relative_path": "手順2024.csv", "source_sha256": "a" * 64,
     "size_bytes": 10, "mtime_ns": 2, "birthtime_ns": 1,
     "explicit_years": [2024], "explicit_versions": [], "current_markers": [],
     "historical_markers": [], "draft_markers": []},
    {"relative_path": "現行_手順2025.csv", "source_sha256": "b" * 64,
     "size_bytes": 20, "mtime_ns": 4, "birthtime_ns": 3,
     "explicit_years": [2025], "explicit_versions": [], "current_markers": ["現行"],
     "historical_markers": [], "draft_markers": []},
]


def literal_hash(value):
    # Independent stdlib fixture construction, never the production hash helper.
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def set_hash(candidates):
    return literal_hash([{key: value[key] for key in SET_FIELDS}
                         for value in sorted(candidates, key=lambda item: item["relative_path"].encode("utf-8"))])


GROUP_ID = "version_set_" + literal_hash({"family_key": KEY})[:32]
SET_HASH = set_hash(CANDIDATES)


def revision():
    return {
        "generation": "generation-" + "1" * 32,
        "source_scope_sha256": "c" * 64,
        "graph_sha256": "d" * 64,
        "graph_file_sha256": "e" * 64,
        "inventory_sha256": "f" * 64,
        "candidate_set_sha256": SET_HASH,
        "decisions_sha256": None,
        "resolver_version": "0.1.6",
    }


def legacy(candidates=None):
    candidates = CANDIDATES if candidates is None else candidates
    return {
        "group_id": GROUP_ID,
        "candidate_set_sha256": set_hash(candidates),
        "selected_relative_path": candidates[1]["relative_path"],
        "selected_source_sha256": candidates[1]["source_sha256"],
        "decided_by": ACTOR,
        "decided_at": DECIDED_AT,
    }


def consent():
    return {
        "decision_schema_version": "2.0",
        **legacy(),
        "relation": "same_work_revisions",
        "current_applicability_confirmed": True,
        "allow_ingest_index_answer": True,
        "reviewed_revision": revision(),
    }


def submission(record=None):
    record = consent() if record is None else record
    return {key: copy.deepcopy(record[key]) for key in (
        "relation", "selected_relative_path", "selected_source_sha256",
        "current_applicability_confirmed", "allow_ingest_index_answer",
    )}


def resolve(record, candidates=None):
    return resolver.resolve_group(KEY, copy.deepcopy(CANDIDATES if candidates is None else candidates), record)


class HoldsMixin:
    def assert_held(self, record, reason=None):
        group = resolve(record)
        self.assertEqual(group["status"], "needs_human_review")
        self.assertIsNone(group["selected_relative_path"])
        self.assertIsNone(group["resolution_basis"])
        self.assertEqual([item["disposition"] for item in group["candidates"]],
                         ["needs_human_review", "needs_human_review"])
        self.assertEqual({item["relative_path"] for item in group["candidates"]},
                         {"手順2024.csv", "現行_手順2025.csv"})
        _, edges = resolver.graph_projection([group])
        self.assertFalse(any(edge["edge_type"] == "active_version" for edge in edges))
        if reason is not None:
            self.assertEqual(group["reason_code"], reason)
        return group


class DatedConsentSeedTests(HoldsMixin, unittest.TestCase):
    def test_matching_legacy_dated_record_is_not_new_consent(self):
        self.assert_held(legacy(), "dated_consent_required")

    def test_version_tag_alone_cannot_upgrade_legacy(self):
        record = legacy()
        record["decision_schema_version"] = "2.0"
        self.assert_held(record, "dated_consent_invalid")

    def test_missing_relation_is_not_inferred_from_same_family(self):
        record = consent()
        del record["relation"]
        self.assert_held(record, "dated_consent_invalid")

    def test_current_confirmation_is_explicit_not_truthy_or_defaulted(self):
        for value in (None, False, 1, "true"):
            with self.subTest(value=value):
                record = consent()
                if value is None:
                    del record["current_applicability_confirmed"]
                else:
                    record["current_applicability_confirmed"] = value
                self.assert_held(record)

    def test_use_permission_is_explicit_not_truthy_or_defaulted(self):
        for value in (None, False, 1, "true"):
            with self.subTest(value=value):
                record = consent()
                if value is None:
                    del record["allow_ingest_index_answer"]
                else:
                    record["allow_ingest_index_answer"] = value
                self.assert_held(record)

    def test_missing_or_stale_displayed_candidate_revision_is_held(self):
        missing = consent()
        del missing["reviewed_revision"]
        stale = consent()
        stale["reviewed_revision"]["candidate_set_sha256"] = "0" * 64
        for label, record in (("missing", missing), ("stale", stale)):
            with self.subTest(label=label):
                self.assert_held(record)

    def test_independent_annual_relation_cannot_force_other_record_obsolete(self):
        record = consent()
        record["relation"] = "independent_records"
        record["current_applicability_confirmed"] = False
        record["allow_ingest_index_answer"] = False
        # Deliberately keep the old selected path: it must not override relation.
        self.assert_held(record, "dated_consent_invalid")

    def test_defer_cannot_reuse_an_old_selected_path(self):
        record = consent()
        record["relation"] = "defer"
        record["current_applicability_confirmed"] = False
        record["allow_ingest_index_answer"] = False
        self.assert_held(record, "dated_consent_invalid")

    def test_old_writer_needs_display_proof_before_decision_store_access(self):
        # This is the real existing five-argument writer, with pure IO seams.
        graph = {"groups": [{"group_id": GROUP_ID, "candidate_set_sha256": SET_HASH,
                             "candidates": copy.deepcopy(CANDIDATES)}]}
        graph["graph_sha256"] = literal_hash(graph)
        graph_text = json.dumps(graph, ensure_ascii=False)

        class GraphText:
            def read_text(self, **kwargs):
                return graph_text

        events = []

        def read_store(path):
            events.append("read_decision_store")
            return {}

        def write_store(path, value):
            events.append("write_decision_store")

        failure = None
        with patch.object(resolver, "load_decisions", read_store), patch.object(resolver, "atomic_json", write_store):
            try:
                resolver.record_decision(GraphText(), object(), GROUP_ID,
                                         "現行_手順2025.csv", ACTOR)
            except ValueError as error:
                failure = str(error)
        self.assertEqual(events, [], "dated writer reached store without displayed proof")
        self.assertEqual(failure, "dated_consent_submission_required")

    def test_undated_legacy_selection_remains_a_compatibility_control(self):
        candidates = copy.deepcopy(CANDIDATES)
        candidates[0].update(relative_path="旧版/手順.csv", explicit_years=[], historical_markers=["旧版"])
        candidates[1].update(relative_path="現行/手順.csv", explicit_years=[])
        group = resolve(legacy(candidates), candidates)
        self.assertEqual(group["status"], "resolved")
        self.assertEqual(group["selected_relative_path"], "現行/手順.csv")
        self.assertEqual(group["resolution_basis"], "human")


class DatedConsentPostApiTests(HoldsMixin, unittest.TestCase):
    def check_record(self, record, status, reason, selected=None):
        self.assertEqual(resolver.validate_dated_consent(KEY, copy.deepcopy(CANDIDATES), record),
                         {"status": status, "reason_code": reason, "selected_relative_path": selected})

    def prepare(self, *, record=None, current=None, displayed=None, candidates=None, submitted=None):
        return resolver.prepare_dated_consent(
            KEY, copy.deepcopy(CANDIDATES if candidates is None else candidates),
            submission(record) if submitted is None else submitted,
            displayed_revision=revision() if displayed is None else displayed,
            current_revision=revision() if current is None else current,
            actor=ACTOR, decided_at=DECIDED_AT)

    def test_literal_complete_record_allows_only_chosen_source(self):
        self.check_record(consent(), "ALLOW_SELECTION", "human_confirmed_same_work_current_and_use",
                          "現行_手順2025.csv")
        group = resolve(consent())
        self.assertEqual(group["reason_code"], "human_confirmed_same_work_current_and_use")
        self.assertEqual(group["resolution_basis"], "human")
        self.assertEqual([(item["relative_path"], item["disposition"]) for item in group["candidates"]],
                         [("手順2024.csv", "historical"), ("現行_手順2025.csv", "active")])

    def test_independent_and_defer_keep_every_candidate_unselected(self):
        for relation, reason in (("independent_records", "independent_records_require_separate_use_review"),
                                 ("defer", "human_deferred")):
            with self.subTest(relation=relation):
                record = consent()
                record.update(relation=relation, selected_relative_path=None, selected_source_sha256=None,
                              current_applicability_confirmed=False, allow_ingest_index_answer=False)
                self.check_record(record, "HOLD", reason)
                self.assert_held(record, reason)

    def test_false_current_or_use_flag_is_an_explicit_hold(self):
        for field, reason in (("current_applicability_confirmed", "dated_consent_current_not_confirmed"),
                              ("allow_ingest_index_answer", "dated_consent_use_not_approved")):
            with self.subTest(field=field):
                record = consent()
                record[field] = False
                self.check_record(record, "HOLD", reason)
                self.assert_held(record, reason)

    def test_prepare_honest_submission_returns_exact_literal_without_mutation(self):
        submitted, displayed, current, candidates = submission(), revision(), revision(), copy.deepcopy(CANDIDATES)
        before = copy.deepcopy((submitted, displayed, current, candidates))
        self.assertEqual(self.prepare(submitted=submitted, displayed=displayed, current=current, candidates=candidates),
                         consent())
        self.assertEqual((submitted, displayed, current, candidates), before)

    def test_prepare_changed_graph_raw_graph_or_inventory_is_stale(self):
        for field in ("graph_sha256", "graph_file_sha256", "inventory_sha256"):
            with self.subTest(field=field):
                current = revision()
                current[field] = "0" * 64
                with self.assertRaisesRegex(ValueError, "^dated_consent_display_stale$"):
                    self.prepare(current=current)

    def test_prepare_changed_generation_or_scope_is_stale(self):
        for field, value in (("generation", "generation-" + "2" * 32), ("source_scope_sha256", "0" * 64)):
            with self.subTest(field=field):
                current = revision()
                current[field] = value
                with self.assertRaisesRegex(ValueError, "^dated_consent_display_stale$"):
                    self.prepare(current=current)

    def test_prepare_store_absent_and_present_are_distinct_revisions(self):
        current = revision()
        # Hash of a present empty decision store must not equal absent/null.
        current["decisions_sha256"] = literal_hash({"schema_version": "2.0", "decisions": []})
        with self.assertRaisesRegex(ValueError, "^dated_consent_store_changed$"):
            self.prepare(current=current)
        displayed = copy.deepcopy(current)
        expected = consent()
        expected["reviewed_revision"] = displayed
        self.assertEqual(self.prepare(current=current, displayed=displayed), expected)

    def test_prepare_stale_selected_source_same_path_is_rejected(self):
        submitted = submission()
        submitted["selected_source_sha256"] = "a" * 64
        with self.assertRaisesRegex(ValueError, "^dated_consent_source_changed$"):
            self.prepare(submitted=submitted)

    def test_prepare_changed_nonselected_source_invalidates_complete_set(self):
        candidates = copy.deepcopy(CANDIDATES)
        candidates[0]["source_sha256"] = "c" * 64
        current = revision()
        current["candidate_set_sha256"] = set_hash(candidates)
        with self.assertRaisesRegex(ValueError, "^dated_consent_display_stale$"):
            self.prepare(candidates=candidates, current=current)

    def test_prepare_matching_but_fabricated_set_revision_is_rejected(self):
        current = revision()
        current["candidate_set_sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "^dated_consent_source_changed$"):
            self.prepare(current=current, displayed=copy.deepcopy(current))

    def test_prepare_current_policy_mismatch_is_not_silently_migrated(self):
        current = revision()
        current["resolver_version"] = "0.1.5"
        with self.assertRaisesRegex(ValueError, "^dated_consent_policy_changed$"):
            self.prepare(current=current, displayed=copy.deepcopy(current))
        displayed = revision()
        displayed["resolver_version"] = "0.1.5"
        with self.assertRaisesRegex(ValueError, "^dated_consent_display_stale$"):
            self.prepare(displayed=displayed)

    def test_prepare_missing_or_unknown_revision_fields_do_not_create_authority(self):
        missing = revision()
        del missing["graph_file_sha256"]
        forged = revision()
        forged["graph_path"] = "/must-not-open/attacker-selected-review.json"
        wrong_type = revision()
        wrong_type["decisions_sha256"] = False
        for label, current in (("missing", missing), ("unknown-path", forged), ("wrong-type", wrong_type)):
            with self.subTest(label=label):
                with self.assertRaisesRegex(ValueError, "^dated_consent_invalid$"):
                    self.prepare(current=current, displayed=copy.deepcopy(current))

    def test_validator_binds_group_set_selected_source_reviewed_set_and_policy(self):
        for field in ("group_id", "candidate_set_sha256", "selected_source_sha256", "reviewed_set", "policy"):
            with self.subTest(field=field):
                record = consent()
                reason = "stale_human_decision"
                if field == "group_id":
                    record[field] = "version_set_" + "0" * 32
                elif field == "reviewed_set":
                    record["reviewed_revision"]["candidate_set_sha256"] = "0" * 64
                elif field == "policy":
                    record["reviewed_revision"]["resolver_version"] = "0.1.5"
                    reason = "dated_consent_policy_changed"
                else:
                    record[field] = "0" * 64
                self.check_record(record, "HOLD", reason)
                self.assert_held(record, reason)

    def test_validator_strict_schema_keys_and_boolean_types(self):
        for field, value in (("decision_schema_version", "9.0"), ("relation", "same-ish"),
                             ("current_applicability_confirmed", 1), ("allow_ingest_index_answer", "true"),
                             ("untrusted_graph_path", "/must-not-open/review.json")):
            with self.subTest(field=field):
                record = consent()
                record[field] = value
                self.check_record(record, "HOLD", "dated_consent_invalid")

    def test_existing_strict_decision_json_parser_still_rejects_bad_input(self):
        for raw in (b'{"decisions":[{"group_id":"x","group_id":"y"}]}',
                    b'{"decisions":[{"group_id":"x"},{"group_id":"x"}]}',
                    b'{"decisions":[],"x":NaN}', b'{"decisions":[],"x":1e309}'):
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError):
                    resolver._validation_decisions(raw)

    def test_prepare_independent_or_defer_has_no_selected_or_use_permission(self):
        for relation in ("independent_records", "defer"):
            with self.subTest(relation=relation):
                expected = consent()
                expected.update(relation=relation, selected_relative_path=None, selected_source_sha256=None,
                                current_applicability_confirmed=False, allow_ingest_index_answer=False)
                self.assertEqual(self.prepare(record=expected), expected)
                self.assert_held(expected)

    def test_prepare_explicit_use_denial_is_recorded_but_not_selected(self):
        expected = consent()
        expected["allow_ingest_index_answer"] = False
        self.assertEqual(self.prepare(record=expected), expected)
        self.assert_held(expected, "dated_consent_use_not_approved")
