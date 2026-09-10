# F11a executor initial semantic RED assessment v1

Root authorized only the frozen nine-method runner after reading its code, reused guards/supervisor and literal tests. Executed `rag/.venv/bin/python -I -B design/local-memory-v1-hardening/runs/f11a-executor-run.v1.py f11a-executor-red-initial-001` on 2026-09-09 04:38:57 UTC. No product edit occurred before or during this run.

Result: **9 methods, 8 semantic assertion failures, 1 positive control, 0 errors/skips/expected failures**. Supervisor 0.369937 seconds, unittest 0.132 seconds, log 7623 bytes. Full log was read. The six intermediate mutation failures are `ValueError not raised` after their literal good-state controls returned exact expected counts. Probe's six-state assertion and Search's six-state copy assertion each received six null/missing state values. Their earlier source bytes/content digest/location/ordinal/text/count controls passed. Non-Notebook old-count compatibility passed.

No missing API/import/TypeError, malformed fixture, outer checksum failure or expected-failure decorator accounts for RED. This result demonstrates the declared current gap; it is not a fix, product PASS, audit PASS or whole-source-body attestation.

Explicit fixture budget: 9642 cumulative bytes / 10 writes, 9 source fixtures, largest source total 1090 bytes. The reused budget prints its historical F05b label; its authored-path set was explicitly limited to this new test and the reviewed harness. Producer artifact writes are confined to temp but are not counted as explicit test fixtures. No external model/network/process was used by the child.

Immutable files:

- Test and full source gold: `tests/test_notebook_metadata_binding.py`, `f11a-executor-gold-test.v1.py`, SHA `0b80382980a130cc52a58108ed5d89df5f0bc4249af39a2432d650078a966daa`.
- Runner: `f11a-executor-run.v1.py`, SHA `1a15a28550c30527ef52cf01e87860a83da663342d100c362f9cb9499643ef4a`.
- Result: `f11a-executor-red-initial-001/result.json`, SHA `91d2f09202a1328fa702a9950813a3d0e4ae7c9dbec13f9af6ccc27653b1f0a7`.
- Log: `f11a-executor-red-initial-001/unittest.log`, SHA `c2fbfa10d2f6ba4815a6ce7c07064ce01a939e64e47c73fd89ec4989afd42f6d`.
- Ten `f11a-executor-before-*.v1.*` files were saved with apply_patch; their SHA values equal all current ten product sources before and after this run (inventory is in the fresh preflight section 9). Root independently compared their exact bytes.

Remaining preimplementation gate: freeze the additional G1–G6 post-API fixture/expected behavior/method gold; root must then grant the separate bounded product edit permission. Do not alter these original nine methods or their supporting constants/helpers. Append new tests and a new gold version, preserving this record. Formal product repair count remains 0; F11b/body/membership remain open.
