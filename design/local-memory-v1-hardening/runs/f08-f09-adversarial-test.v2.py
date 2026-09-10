from __future__ import annotations

import io
import sys
import unittest
import zipfile
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPOSITORY / "scripts"))
import probe_intermediate_records as probe


class IncrementalXmlGateAudit(unittest.TestCase):
    def test_continuation_chunk_boundaries_and_shared_archive(self):
        configurations = (
            ("utf-8", b"\xef\xbb\xbf", "あ"),
            ("utf-16-le", b"\xff\xfe", "😀"),
            ("utf-16-be", b"\xfe\xff", "😀"),
            ("utf-32-le", b"\xff\xfe\x00\x00", "😀"),
            ("utf-32-be", b"\x00\x00\xfe\xff", "😀"),
        )
        for encoding, bom, character in configurations:
            for offset in range(8):
                with self.subTest(encoding=encoding, offset=offset):
                    value = "<p>" + "!" * offset + character * 46000 + "</p>"
                    raw = bom + value.encode(encoding)
                    self.assertLess(len(raw), 1024 * 1024)
                    probe.validate_xml_bytes(raw)
                    archive = io.BytesIO()
                    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_STORED) as output:
                        output.writestr("payload.xml", raw)
                    probe.validate_ooxml_archive(archive, required_members=frozenset({"payload.xml"}))

    def test_late_invalid_units_and_declarations_remain_rejected(self):
        configurations = (
            ("utf-8", b"\xef\xbb\xbf", b"\xe3\x81"),
            ("utf-16-le", b"\xff\xfe", b"\x00\xd8"),
            ("utf-16-be", b"\xfe\xff", b"\xd8\x00"),
            ("utf-32-le", b"\xff\xfe\x00\x00", b"\x00\x00\x11\x00"),
            ("utf-32-be", b"\x00\x00\xfe\xff", b"\x00\x11\x00\x00"),
        )
        for encoding, bom, invalid in configurations:
            with self.subTest(encoding=encoding):
                prefix = bom + ("<p>" + "!" * 140000).encode(encoding)
                self.assertLess(len(prefix) + len(invalid), 1024 * 1024)
                with self.assertRaisesRegex(ValueError, "xml_encoding_invalid"):
                    probe.validate_xml_bytes(prefix + invalid)
                dtd = bom + (" " * 140000 + '<!DOCTYPE p [<!ENTITY x "blocked">]><p>&x;</p>').encode(encoding)
                with self.assertRaisesRegex(ValueError, "xml_unsafe"):
                    probe.validate_xml_bytes(dtd)


if __name__ == "__main__":
    unittest.main()
