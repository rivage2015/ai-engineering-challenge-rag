# Dated-HITL selected-byte check v2 failure (preserved)

This is a second artifact preparation error, not a product test or audit result.

- Command: `/opt/homebrew/opt/python@3.14/bin/python3.14 -I -B design/local-memory-v1-hardening/runs/dated-hitl-executor-byte-check.v2.py`
- Verifier SHA256: `042d58d2dfe104eda7e48eabb99c4468a759c54559713cc0673c54cc271a2e89`
- Exit code: `1`.
- Failure: `AssertionError` at line 62, `assert delta.encode() == blobs[RUNS + "dated-hitl-executor-resolver-delta.v1.patch"]`.
- All 11 selected input SHA256 checks and the before/current/after and gold byte equality checks preceding this assertion completed. The later AST checks did not run.
- Diagnosis: the system `diff -u` and Python `difflib.unified_diff` choose different equivalent context/hunks for repeated blank lines and the repeated `return None` list. The artifact used the actual system diff; byte identity between different diff renderers was an invalid verifier assumption.
- Read-only diagnostic printed the two representations' difference. Examples: the helper hunk is `-86,10 +93,74` versus `-88,8 +95,72`; the dated hold insertion hunk is `-258,6 +339,10` versus `-256,6 +337,10`.
- Corrective scope: a new v3 verifier applies the saved patch forward and in reverse in memory and compares complete literal before/after bytes, rejecting malformed hunk counts or context. Preserve v1, v2, both failures and the original patch. No product or test bytes change.
