# Dated-HITL selected-byte check v1 failure (preserved)

This is an artifact preparation error, not a product test or audit result.

- Command: `/opt/homebrew/opt/python@3.14/bin/python3.14 -I -B design/local-memory-v1-hardening/runs/dated-hitl-executor-byte-check.v1.py`
- Verifier SHA256: `955775ed496c7b410ebc7ab6595c0b3090e380a33707cd93ddf03184aacb3fd3`
- Exit code: `1`.
- Failure: `FileNotFoundError: [Errno 2] No such file or directory: '/Users/takashifukutomi/Documents/ChatGPT/AIエンジニアリングチャレンジ/tests/test_document_version_resolver.py'` at line 46, before the AST comparisons.
- Diagnosis: the selected source path was mistyped. `rg --files` locates the existing test at `distribution/macos-local-memory/tests/test_document_version_resolver.py`.
- Corrective scope: new verifier v2 changes only that explicit source path. Preserve v1. No product or test bytes changed; no product test rerun is caused by this artifact error.
- Prior supervised RED/GREEN receipts are unaffected. This file does not turn the failed check into a successful one.
