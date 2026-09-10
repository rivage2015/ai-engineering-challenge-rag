from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock


REPOSITORY = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY / "scripts"))

import build_intermediate_records as builder
import build_search_units
import adapt_layer1_to_local_memory
import probe_intermediate_records as probe
import validate_intermediate_records
import validate_intermediate_records_streaming
import validate_search_units
import validate_search_units_streaming


RUN_AT = "2026-09-09T00:00:00+00:00"


class GenericStructuredReaderHardeningTests(unittest.TestCase):
    """Small, deterministic F08/F09 fixtures; no models or personal sources."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="lms-f08-f09-")
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.root = self.base / "source"
        self.root.mkdir()
        # Both inference and the otherwise implicit fingerprint metadata call
        # are forbidden. The real build is exercised with a synthetic identity.
        self.network_guard = mock.patch.object(
            builder, "_ollama_json", side_effect=AssertionError("network forbidden")
        )
        self.network_guard.start()
        self.addCleanup(self.network_guard.stop)

    def source(self, name: str, value: str | bytes) -> Path:
        path = self.root / name
        raw = value.encode("utf-8") if isinstance(value, str) else value
        self.assertLessEqual(len(raw), 1024 * 1024)
        path.write_bytes(raw)
        return path

    def extract(self, path: Path, max_items: int | None = None) -> probe.Probe:
        reader = probe.Probe(
            self.root, RUN_AT, max_items, diagnostic=max_items is not None,
            visual_observation_mode="suppressed",
        )
        reader.extract(path)
        return reader

    def run_build(self) -> tuple[Path, dict]:
        output = self.base / "intermediate"
        actual_probe = probe.Probe

        def suppressed_probe(*args, **kwargs):
            kwargs["visual_observation_mode"] = "suppressed"
            return actual_probe(*args, **kwargs)

        fingerprint_payload = {"test": "generic-structured-reader-no-models"}
        fingerprint = {
            "version": builder.PROCESSING_FINGERPRINT_VERSION,
            "sha256": probe.digest_value(fingerprint_payload),
            "payload": fingerprint_payload,
        }
        with (
            mock.patch.object(sys, "argv", [
                "build_intermediate_records.py", "--root", str(self.root),
                "--out", str(output), "--run-at", RUN_AT,
            ]),
            mock.patch.object(builder, "Probe", side_effect=suppressed_probe),
            mock.patch.object(builder, "processing_fingerprint", return_value=fingerprint),
            mock.patch.object(builder, "discover_password_candidates", return_value=()),
            mock.patch.object(builder, "paddle_build_session", side_effect=contextlib.nullcontext),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            builder.main()
        return output, json.loads((output / "build-state.json").read_text(encoding="utf-8"))

    def test_xml_mixed_content_keeps_text_tail_order_and_parent_locator(self) -> None:
        reader = self.extract(self.source("mixed.xml", '<p>前<b>中<i>深</i>内後</b>後<empty/>終</p>'))
        fields = [item for item in reader.evidence if item["evidence_type"] == "field"]
        self.assertEqual([item["content"]["raw_text"] for item in fields], ["前", "中", "深", "内後", "後", "終"])
        self.assertEqual(
            [item["location"]["locator_text"] for item in fields],
            ["/p[1]/text()[1]", "/p[1]/b[1]/text()[1]", "/p[1]/b[1]/i[1]/text()[1]",
             "/p[1]/b[1]/text()[2]", "/p[1]/text()[2]", "/p[1]/text()[3]"],
        )
        by_path = {item["location"]["locator_text"]: item for item in reader.evidence}
        for item in fields:
            parent_path = item["location"]["locator_text"].rsplit("/text()", 1)[0]
            self.assertEqual(item["parent_evidence_id"], by_path[parent_path]["evidence_id"])
        self.assertEqual(reader.documents[0]["extraction"]["status"], "success")

    def test_xml_preserves_whitespace_text_nodes_and_cdata(self) -> None:
        reader = self.extract(self.source("space.xml", '<p xml:space="preserve"> <b><![CDATA[x < y]]></b> \n<c/> z </p>'))
        text_nodes = [item for item in reader.evidence if "/text()" in item["location"]["locator_text"]]
        self.assertEqual([item["content"]["raw_text"] for item in text_nodes], [" ", "x < y", " \n", " z "])

    def test_xml_namespace_attributes_and_repeated_elements_remain_distinct(self) -> None:
        reader = self.extract(self.source("names.xml", '<r xmlns:a="urn:a" xmlns:b="urn:b"><a:x a:id="1">one</a:x><b:x>two</b:x><a:x>three</a:x></r>'))
        fields = [item for item in reader.evidence if item["evidence_type"] == "field"]
        self.assertEqual([item["content"]["raw_text"] for item in fields], ["1", "one", "two", "three"])
        self.assertEqual(len({item["evidence_id"] for item in reader.evidence}), len(reader.evidence))
        self.assertIn("{urn:a}x", fields[0]["location"]["locator_text"])
        self.assertIn("{urn:b}x", fields[2]["location"]["locator_text"])

    def test_xml_diagnostic_leaf_limit_does_not_claim_complete(self) -> None:
        reader = self.extract(self.source("limited.xml", "<p>前<b>中</b>後</p>"), max_items=2)
        self.assertEqual(reader.documents[0]["extraction"]["status"], "partial")
        self.assertEqual(len([item for item in reader.evidence if item["evidence_type"] == "field"]), 2)

    def test_xml_rejects_dtd_and_entities_before_parser_in_wide_encodings(self) -> None:
        value = '<!DOCTYPE p [<!ENTITY secret "must-not-be-evidence">]><p>&secret;</p>'
        for index, encoding in enumerate(("utf-8", "utf-16", "utf-16-be", "utf-32", "utf-32-be")):
            with self.subTest(encoding=encoding):
                # Wide encodings without BOM use the XML declaration signature.
                declared = value if encoding in {"utf-8", "utf-16", "utf-32"} else '<?xml version="1.0" encoding="' + encoding + '"?>' + value
                path = self.source(f"unsafe-{index}.xml", declared.encode(encoding))
                with mock.patch.object(probe.ElementTree, "fromstring") as parse:
                    with self.assertRaisesRegex(ValueError, "xml_unsafe"):
                        self.extract(path)
                    parse.assert_not_called()

    def test_xml_rejects_external_dtd_before_parser(self) -> None:
        path = self.source("external.xml", '<!DOCTYPE p SYSTEM "file:///not-a-real-readable-target"><p>text</p>')
        with mock.patch.object(probe.ElementTree, "fromstring") as parse:
            with self.assertRaisesRegex(ValueError, "xml_unsafe"):
                self.extract(path)
            parse.assert_not_called()

    def test_xml_utf16_without_dtd_still_preserves_mixed_text(self) -> None:
        value = '<?xml version="1.0" encoding="utf-16"?><p>前<b>中</b>後</p>'
        reader = self.extract(self.source("utf16.xml", value.encode("utf-16")))
        self.assertEqual([item["content"]["raw_text"] for item in reader.evidence if item["evidence_type"] == "field"], ["前", "中", "後"])

    def test_xml_cp932_and_utf8_bom_decoding_remain_supported(self) -> None:
        for index, encoding in enumerate(("cp932", "utf-8-sig")):
            with self.subTest(encoding=encoding):
                reader = self.extract(self.source(f"encoded-{index}.xml", '<p>前<b>中</b>後</p>'.encode(encoding)))
                self.assertEqual([item["content"]["raw_text"] for item in reader.evidence if item["evidence_type"] == "field"], ["前", "中", "後"])

    def test_xml_gate_multibyte_and_surrogate_sample_boundaries(self) -> None:
        # Move the 2048-byte gate sample across all nearby character offsets.
        # UTF-32 exercises the gate only: generic UTF-32 decoding is not part
        # of the native Reader compatibility claim in this repair.
        encodings = (
            ("utf-8", b"\xef\xbb\xbf", "あ", True),
            ("utf-16-le", b"\xff\xfe", "😀", True),
            ("utf-16-be", b"\xfe\xff", "😀", True),
            ("utf-32-le", b"\xff\xfe\x00\x00", "😀", False),
            ("utf-32-be", b"\x00\x00\xfe\xff", "😀", False),
        )
        for encoding, bom, character, extract_native in encodings:
            for offset in range(8):
                with self.subTest(encoding=encoding, offset=offset):
                    leading = "!" * offset + character * 800
                    value = f"<p>{leading}<b>中</b>後</p>"
                    raw = bom + value.encode(encoding)
                    probe.validate_xml_bytes(raw)
                    if extract_native:
                        reader = self.extract(self.source(f"boundary-{encoding}-{offset}.xml", raw))
                        self.assertEqual([item["content"]["raw_text"] for item in reader.evidence if item["evidence_type"] == "field"], [leading, "中", "後"])
                        self.assertEqual(reader.documents[0]["extraction"]["status"], "success")

    def test_xml_gate_still_rejects_invalid_encoding_at_and_after_sample(self) -> None:
        encodings = (
            ("utf-8", b"\xef\xbb\xbf", b"\xff", b"\xe3\x81"),
            ("utf-16-le", b"\xff\xfe", b"\x00\xd8A\x00", b"\x00\xd8"),
            ("utf-16-be", b"\xfe\xff", b"\xd8\x00\x00A", b"\xd8\x00"),
            ("utf-32-le", b"\xff\xfe\x00\x00", b"\x00\x00\x11\x00", b"\x41\x00"),
            ("utf-32-be", b"\x00\x00\xfe\xff", b"\x00\x11\x00\x00", b"\x00\x00\x41"),
        )
        for encoding, bom, invalid, truncated in encodings:
            for padding in (0, 2039, 2048):
                with self.subTest(encoding=encoding, padding=padding):
                    prefix = bom + ("<p>" + "!" * padding).encode(encoding)
                    for ending in (invalid + "</p>".encode(encoding), truncated):
                        with self.assertRaisesRegex(ValueError, "xml_encoding_invalid"):
                            probe.validate_xml_bytes(prefix + ending)

    def test_xml_gate_retains_full_buffer_declaration_checks(self) -> None:
        for encoding, bom in (
            ("utf-8", b"\xef\xbb\xbf"),
            ("utf-16-le", b"\xff\xfe"),
            ("utf-16-be", b"\xfe\xff"),
            ("utf-32-le", b"\xff\xfe\x00\x00"),
            ("utf-32-be", b"\x00\x00\xfe\xff"),
        ):
            with self.subTest(encoding=encoding):
                unsafe = " " * 2050 + '<!DOCTYPE p [<!ENTITY x "blocked">]><p>&x;</p>'
                with self.assertRaisesRegex(ValueError, "xml_unsafe"):
                    probe.validate_xml_bytes(bom + unsafe.encode(encoding))
                # A declared incompatible encoding remains rejected, even
                # when the sampled body crosses a multibyte boundary.
                wrong = '<?xml version="1.0" encoding="cp932"?><p>' + "😀" * 800 + '</p>'
                with self.assertRaisesRegex(ValueError, "xml_encoding_invalid"):
                    probe.validate_xml_bytes(bom + wrong.encode(encoding))

    def test_shared_archive_xml_gate_keeps_safety_and_encoding_contract(self) -> None:
        for encoding in ("utf-8-sig", "utf-16"):
            for unsafe in (False, True):
                with self.subTest(encoding=encoding, unsafe=unsafe):
                    xml = '<p>!' + '😀' * 800 + '</p>'
                    if unsafe:
                        xml = '<!DOCTYPE p [<!ENTITY blocked "value">]>' + xml
                    archive = io.BytesIO()
                    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_STORED) as output:
                        output.writestr("payload.xml", xml.encode(encoding))
                    self.assertLess(len(archive.getvalue()), 1024 * 1024)
                    if unsafe:
                        with self.assertRaisesRegex(ValueError, "xml_unsafe"):
                            probe.validate_ooxml_archive(archive, required_members=frozenset({"payload.xml"}))
                    else:
                        probe.validate_ooxml_archive(archive, required_members=frozenset({"payload.xml"}))

    def test_optional_xml_gate_does_not_change_other_text_reads(self) -> None:
        for index, encoding in enumerate(("utf-8", "cp932", "utf-16")):
            with self.subTest(encoding=encoding):
                path = self.source(f"plain-{index}.txt", "日本語テキスト".encode(encoding))
                self.assertEqual(probe.read_text(path)[0], "日本語テキスト")
        path = self.source("marker.txt", "<!DOCTYPE is literal code here>")
        self.assertEqual(probe.read_text(path)[0], "<!DOCTYPE is literal code here>")

    def test_large_structured_input_is_explicitly_partial_without_native_parsing(self) -> None:
        for name, text in (("large.xml", '<!DOCTYPE p><p>data</p>'), ("large.json", '{"x":0,"x":1}')):
            with self.subTest(name=name):
                path = self.source(name, text)
                with mock.patch.object(probe, "MAX_DIRECT_TEXT_BYTES", 8):
                    reader = self.extract(path)
                self.assertEqual(reader.documents[0]["extraction"]["status"], "partial")
                self.assertEqual(reader.documents[0]["extraction"]["parser"], "bounded-text-stream")
                self.assertEqual("".join(item["content"]["raw_text"] for item in reader.evidence), text)

    def test_json_rejects_duplicate_keys_at_every_object_depth(self) -> None:
        cases = [
            '{"status":"old","status":"new"}',
            '{"safe":1,"nested":{"status":false,"status":true}}',
            '[{"safe":1},{"status":0,"status":null}]',
            '{"a":1,"\\u0061":2}',
            '{"a/b":1,"a/b":2}',
            '{"a":1,"a":1}',
        ]
        for index, value in enumerate(cases):
            with self.subTest(index=index):
                path = self.source(f"duplicate-{index}.json", value)
                with self.assertRaisesRegex(ValueError, "duplicate JSON object key"):
                    self.extract(path)

    def test_json_same_key_in_separate_objects_is_not_duplicate(self) -> None:
        reader = self.extract(self.source("valid.json", '{"left":{"x":0},"right":{"x":false},"array":[{"x":null},{"x":""}],"a/b~c":"value"}'))
        fields = [item for item in reader.evidence if item["evidence_type"] == "field"]
        self.assertEqual([item["location"]["locator_text"] for item in fields], ["/left/x", "/right/x", "/array/0/x", "/array/1/x", "/a~1b~0c"])
        self.assertEqual([item["content"].get("raw_text", item["content"].get("raw_value")) for item in fields], [0, False, None, "", "value"])
        self.assertEqual(reader.documents[0]["extraction"]["status"], "success")

    def test_failed_structured_inputs_have_no_search_units_but_valid_neighbor_survives(self) -> None:
        sources = [
            self.source("duplicate.json", '{"status":"old","status":"new"}'),
            self.source("nested.json", '{"nested":{"status":0,"status":1}}'),
            self.source("unsafe.xml", '<!DOCTYPE p [<!ENTITY s "secret">]><p>&s;</p>'),
            self.source("broken.json", '{"broken":'),
            self.source("broken.xml", '<p>broken<b></p>'),
            self.source("valid.xml", '<p>前<b>中</b>後</p>'),
            self.source("valid.json", '{"status":"現行"}'),
            self.source("boundary-utf8.xml", ("<p>" + "あ" * 800 + "<b>中</b>後</p>").encode("utf-8-sig")),
            self.source("boundary-utf16.xml", ("<r>!" + "😀" * 600 + "<b>中</b>後</r>").encode("utf-16")),
        ]
        original_hashes = {path: probe.digest_file(path) for path in sources}
        intermediate, state = self.run_build()
        self.assertEqual(state["build_status"], "complete_with_failures")
        failed_ids = set()
        for name in ("duplicate.json", "nested.json", "unsafe.xml", "broken.json", "broken.xml"):
            entry = state["entries"][name]
            self.assertEqual(entry["status"], "failed", name)
            self.assertEqual(entry["shards"]["evidence"]["record_count"], 0)
            self.assertEqual(entry["shards"]["relations"]["record_count"], 0)
            failed_ids.add(entry["document_id"])
        batch = validate_intermediate_records.validate(intermediate, self.root)
        streaming = validate_intermediate_records_streaming.validate(intermediate, self.root)
        self.assertEqual(batch["document"], len(sources))
        self.assertEqual(streaming["document"], len(sources))
        search = self.base / "search"
        build_search_units.build(intermediate, search, 1200)
        expected = validate_search_units.validate(search, [intermediate])
        self.assertEqual(validate_search_units_streaming.validate(search, [intermediate]), expected)
        units = [json.loads(line) for line in (search / "search_units.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertFalse(failed_ids.intersection(unit["document_id"] for unit in units))
        xml_units = [unit for unit in units if unit["document_id"] == state["entries"]["valid.xml"]["document_id"]]
        self.assertTrue(any(unit["locator"].get("locator_text") == "/p[1]/text()[2]" and "後" in unit["text"]["search_text"] for unit in xml_units))
        self.assertTrue(any("現行" in unit["text"]["search_text"] for unit in units))
        adapter_output = self.base / "adapter"
        adapted = adapt_layer1_to_local_memory.adapt(intermediate, self.root.resolve(), adapter_output, search)
        self.assertEqual(adapted["layer1_status_counts"], {"failed": 5, "success": 4})
        semantic_documents = [json.loads(line) for line in (adapter_output / "semantic-documents.jsonl").read_text(encoding="utf-8").splitlines()]
        semantic_evidence = [json.loads(line) for line in (adapter_output / "semantic-evidence.jsonl").read_text(encoding="utf-8").splitlines()]
        for document in semantic_documents:
            if document["document_id"] in failed_ids:
                self.assertEqual(document["classification"], "unresolved")
                self.assertEqual(document["status"], "extraction_failed")
                self.assertEqual(document["evidence_ids"], [])
                self.assertTrue(document["error"])
        self.assertFalse(failed_ids.intersection(item["document_id"] for item in semantic_evidence))
        self.assertTrue(any(item["observed_text"] == "後" for item in semantic_evidence))
        self.assertEqual({path: probe.digest_file(path) for path in sources}, original_hashes)


if __name__ == "__main__":
    unittest.main()
