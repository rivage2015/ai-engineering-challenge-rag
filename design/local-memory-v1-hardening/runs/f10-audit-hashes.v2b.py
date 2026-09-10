"""Correct v2 diagnostic context selection; retain the failed v2 helper unchanged."""
from pathlib import Path

original = Path(__file__).with_name("f10-audit-hashes.v2.py")
source = original.read_text()
old = 'before_repair = transform(base, prior["parser_patch_snapshot"], False)'
new = '''prefix, marker, rest = base.partition("    def extract_xlsx_ooxml(")
assert marker
fallback, next_marker, suffix = rest.partition("    def extract_pptx(")
assert next_marker
before_repair = prefix + marker + transform(fallback, prior["parser_patch_snapshot"], False) + next_marker + suffix'''
assert source.count(old) == 1
exec(compile(source.replace(old, new), str(original), "exec"), {"__file__": str(original), "__name__": "__main__"})
