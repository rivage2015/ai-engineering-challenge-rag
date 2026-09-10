"""Reconstruct only the three authorized root before texts; stdout JSON only."""
import difflib
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def main():
    records = []
    for relative, expected in (
        ("distribution/macos-local-memory/app/bootstrap.py", "df720a40bb0f8f6123cb0f171b829ca1f4f26761f1e24878fa415b5ff70919a3"),
        ("tests/test_unmarked_version_candidates.py", "b4a9176f3f7bb723d701a548418be8647a5336dc7b88dd0707d03cfa70eb5b94"),
        ("distribution/macos-local-memory/README.md", "03e4918d7db84b3455d8790b4be8d3af1608071a9cb7e8c9a3df60bb36a4e028"),
    ):
        path = ROOT / relative
        after = path.read_text()
        if relative.endswith("bootstrap.py"):
            new = '                "validate", "--graph", str(paths / "document-version-graph.json"),\n                "--inventory", str(paths / "path-source-inventory.jsonl"),\n                "--decisions", str(DOCUMENT_VERSION_DECISIONS),\n'
            old = new.replace('                "--decisions", str(DOCUMENT_VERSION_DECISIONS),\n', "")
        elif relative.endswith("test_unmarked_version_candidates.py"):
            new = '        self.assertEqual("0.1.4", result["resolver_version"])\n'
            old = new.replace("0.1.4", "0.1.3")
        else:
            paragraphs = after.split("\n\n")
            matches = [p for p in paragraphs if p.startswith("版判定器の`validate`は、")]
            assert len(matches) == 1
            new, old = matches[0] + "\n\n", ""
        assert after.count(new) == 1
        before = after.replace(new, old, 1)
        assert sha(before) == expected, relative
        records.append({"path": str(path), "before_sha256": expected, "after_sha256": sha(after),
                        "isolated_unified_diff": "".join(difflib.unified_diff(before.splitlines(True), after.splitlines(True), fromfile="before/" + relative, tofile="after/" + relative))})
    print(json.dumps({"task_id": "lms-v1-00-hardening-2026-09-09-f05a", "status": "root_product_hunks_ready_executor_pending_not_accepted", "deltas": records}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
