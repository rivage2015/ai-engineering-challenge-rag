from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPOSITORY / "tests"))
from test_generic_structured_reader_hardening import (
    GenericStructuredReaderHardeningTests,
    adapt_layer1_to_local_memory,
    build_search_units,
    probe,
    validate_intermediate_records,
    validate_intermediate_records_streaming,
    validate_search_units,
    validate_search_units_streaming,
)


class IndependentCounterexamples(GenericStructuredReaderHardeningTests):
    def test_audit_valid_utf8_bom_xml_crosses_gate_sample_boundary(self):
        value = "<p>" + "あ" * 800 + "<b>中</b>後</p>"
        path = self.source("utf8-bom-boundary.xml", value.encode("utf-8-sig"))
        self.assertEqual(probe.read_text(path)[0], value)
        reader = self.extract(path)
        text = [item["content"]["raw_text"] for item in reader.evidence if item["evidence_type"] == "field"]
        self.assertEqual(text, ["あ" * 800, "中", "後"])
        self.assertEqual(reader.documents[0]["extraction"]["status"], "success")

    def test_audit_valid_utf16_xml_surrogate_crosses_gate_sample_boundary(self):
        value = "<r>!" + "😀" * 600 + "<b>中</b>後</r>"
        path = self.source("utf16-boundary.xml", value.encode("utf-16"))
        self.assertEqual(probe.read_text(path)[0], value)
        reader = self.extract(path)
        text = [item["content"]["raw_text"] for item in reader.evidence if item["evidence_type"] == "field"]
        self.assertEqual(text, ["!" + "😀" * 600, "中", "後"])
        self.assertEqual(reader.documents[0]["extraction"]["status"], "success")

    def test_audit_pretty_xml_through_adapter_and_validators(self):
        value = '<r xmlns:n="https://example.invalid/ns">\n  <n:p id="A">start<n:b/>middle<n:c>nested</n:c>end</n:p>\n  <n:p id="B">second</n:p>\n</r>'
        path = self.source("pretty.xml", value)
        self.source("duplicate.json", '{"outer":[{"ok":1},{"x":1,"x":2}]}')
        original_hash = probe.digest_file(path)
        intermediate, state = self.run_build()
        validate_intermediate_records.validate(intermediate, self.root)
        validate_intermediate_records_streaming.validate(intermediate, self.root)
        search = self.base / "search"
        build_search_units.build(intermediate, search, 1200)
        validate_search_units.validate(search, [intermediate])
        validate_search_units_streaming.validate(search, [intermediate])
        adapter_output = self.base / "adapter"
        adapt_layer1_to_local_memory.adapt(intermediate, self.root.resolve(), adapter_output, search)
        raw = [json.loads(line) for line in (intermediate / "evidence.jsonl").read_text(encoding="utf-8").splitlines()]
        fields = [item for item in raw if item["evidence_type"] == "field" and "/text()" in item["location"]["locator_text"]]
        self.assertEqual([item["content"]["raw_text"] for item in fields], ["\n  ", "start", "middle", "nested", "end", "\n  ", "second", "\n"])
        self.assertEqual(len({item["location"]["locator_text"] for item in fields}), len(fields))
        semantic = [json.loads(line) for line in (adapter_output / "semantic-evidence.jsonl").read_text(encoding="utf-8").splitlines()]
        semantic_by_id = {item["evidence_id"]: item for item in semantic}
        self.assertTrue(all(semantic_by_id[item["evidence_id"]]["observed_text"] == item["content"]["raw_text"] for item in fields))
        self.assertEqual(state["entries"]["duplicate.json"]["shards"]["evidence"]["record_count"], 0)
        self.assertEqual(state["entries"]["pretty.xml"]["status"], "success")
        self.assertEqual(probe.digest_file(path), original_hash)

    def test_audit_duplicates_deep_and_encoded_fail_before_evidence(self):
        duplicate = '{"日本":1,"\\u65e5\\u672c":2}'
        for encoding in ("utf-8-sig", "utf-16", "cp932"):
            for depth in (0, 1, 64):
                with self.subTest(encoding=encoding, depth=depth):
                    value = '{"nested":' * depth + duplicate + '}' * depth
                    path = self.source(f"dup-{encoding}-{depth}.json", value.encode(encoding))
                    with self.assertRaisesRegex(ValueError, "duplicate JSON object key"):
                        self.extract(path)


if __name__ == "__main__":
    names = [name for name in dir(IndependentCounterexamples) if name.startswith("test_audit_")]
    suite = unittest.TestSuite(IndependentCounterexamples(name) for name in names)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
