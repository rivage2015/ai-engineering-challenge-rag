# F11 next-slice read-only preflight v1

Scope: inspect the existing Reader; add only `design/local-memory-v1-hardening/runs/f11-next-*` evidence and proposed contracts. No parser/schema/product test edits. No product approval.

Inspected before execution: `Probe.__init__`, `Probe.extract`, `Probe.extract_notebook`, `add_document`, `add_evidence`, `DocumentDeriver.__init__/consume/add_direct_text/finish`, and the complete bounded supervisor. Direct imports of the two Reader/SearchUnit modules have no model/package-metadata calls. The fixture has only text, no attachments, images, paths, URLs or executable side effects other than a literal RuntimeError line that must remain source text. The Reader is called with `visual_observation_mode="suppressed"` and diagnostic=False.

Run command: `python3 -B design/local-memory-v1-hardening/runs/f11-next-observe.v1.py --supervise`.

Limits: 30 seconds wall time, 1 MiB combined child log, one static adjacent Notebook below 1 MiB, 3 deterministic observation tests, no parallel model. `run_bounded` writes only its new owned `f11-next-observation-run.v1` directory. Child has a stdlib audit hook rejecting socket/subprocess/system/spawn events; this is a supplemental guard, not an OS sandbox. No temporary source directories, published CONFIG, real inputs, credentials or external tools.

Expected observation: 6 Reader text records, status success, original text and cell/output locators preserved, saved execution counts not propagated, and an in-memory candidate metadata sentinel lost by direct SearchUnit derivation. A PASS means these current gaps reproduced. It does not attest a fix, complete Notebook support, source-to-index E2E, or output freshness. Empty code source with saved output must be observed because metadata cannot rely on a parent source Evidence existing.

Source protection: test setup records fixture bytes and verifies identical bytes at cleanup. Product files remain read-only. A new run directory exists check rejects overwriting old evidence. Rollback for this read-only study is to leave the evidence excluded from any candidate patch; no product rollback is needed. Never reset a shared dirty worktree.
