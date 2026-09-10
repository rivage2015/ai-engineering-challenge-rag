# Dated-HITL resolver executor handoff v1

Status: bounded implementation and selected pure verification complete; parent review pending. Not formal audit PASS, not app/answer authorization closure. Same F02/F03/F04 residual continuation; no repair-budget reset. F11a generator v3 remains unexecuted and F11b remains mandatory follow-on.

## Ownership and actual change

Sole product change: `distribution/macos-local-memory/engine/document_version_resolver.py`, resolver `0.1.4 → 0.1.5`, schema `1.0` unchanged.

- Before SHA256 `14ecce2c01d68bfbfaa27d3022bfc6076a97465a331eb03443af0e6d50cc006f`.
- Current/after SHA256 `11218cc2b5170ba59a69203bcc549a50dc6eb6f88e487d191fb55a76048973a8`.
- Exact before and after: `dated-hitl-executor-before-resolver.v1.py`, `dated-hitl-executor-after-resolver.v1.py`.
- Unified delta: `dated-hitl-executor-resolver-delta.v1.patch`, SHA256 `2ccb23aa6a63413b76fa56ca13c74faf552a312d4dae1c82a7d9e5a0a5ed461d`.

At current source lines 38–44, 98–168: NFKC-normalized 202/203 numeric compounds are first retained as whole candidates. Only calendar-valid `YYYYMMDD`, consistently separated `YYYY-M[M]` / `YYYY-M[M]-D[D]` (separators `-`, `_`, `.`), `YYYY年M[M]月D[D]日`, bare year and `YYYY年度` are removed from normalized family text. Compact full-date and month/year checks do not infer authority or replacement. Original relative paths remain unchanged. Invalid dates such as `20251340`, non-leap `20230229`, invalid month/day and mixed separators keep all digits; unknown run lengths such as `202`, `203`, `20251`, `202401011` remain in family names. Those runs still provide a review signal for same-family peers. Other numeric identifiers are not stripped by this new grammar.

At lines 252–262, 282–364: both positive automatic paths now hold whenever a peer has a temporal signal. A current/approved-looking filename cannot authorize dated peers, nor can the numeric-version fallback do so. Existing earlier conflict/ambiguity reasons remain. Year-only groups retain the independent-period caution (`year_order_does_not_establish_supersession`); every unresolved member stays `needs_human_review`, with no active-version edge. Valid date-like IDs can still produce false-positive candidate families, which is intentional candidate-only behavior, not confirmation that they are dates.

At lines 473 onward: reconstruction's resolver version and policy declare date prefixes and `date_likeness_is_authoritative: false`, `calendar_validity_establishes_supersession: false`. Candidate JSON fields, candidate-set hash field set, public API/CLI signatures, source/decision snapshot readers, exact reconstruction and decision matching functions are unchanged. Twenty-one original function ASTs are unchanged; six changed and three new helper functions are listed in the byte-check result.

No Reader, Validator, projector, server, bootstrap, config, app bundle, shared decisions, original documents, model/network, existing test, Git, or protected build edits were performed by this executor in this scope. The new helper remains within an already bundled resolver module; no packaging-script modification is needed.

## Frozen test oracle and execution

NEW `tests/test_dated_temporal_candidates.py` and preimplementation `dated-hitl-executor-gold.v1.py` remain byte-identical, SHA256 `6a9a877771117441b33cdb94f5bc4f459b067bf682c538dbade00593189c7154`. They contain 16 desired-contract methods plus a separately selected one-method legacy residual witness. The preflight (`dated-hitl-executor-preflight.v1.md`, `eafb4514dcc9eb2fa4cb0b4cd44e98cfe12dcc7c5bf37fae57c15b6af689bd54`) was saved before the resolver edit.

All product batches used `/opt/homebrew/opt/python@3.14/bin/python3.14 -I -B`, fixed runner `dated-hitl-executor-run.v1.py` SHA256 `8932d3d909c903678af4ed5ad5f0abf6352b4487ba20739765b62e494e80350b`. The runner fixes source-test hashes and exact classes/method counts; after code imports it denies runtime file/directory, process and network IO and sets a 30-second alarm. Outer supervision is 30 seconds / 1 MiB complete log. This is not an OS sandbox or RSS guarantee. Fixtures are in-memory synthetic records, with no original-file reads or fixture writes. The supervisor was read fully before execution; its post-run observed SHA256 is `6b3bde90b6ed649a82325f6b5ad9661756bef7b6e0303fa74c2d045fb26eb4a9` (not a retroactive per-run pin).

Every directory below retains `started.json`, `result.json`, `unittest.log`. All five runs were completed under the log/time bound; none was retried/overwritten. No product run had ERROR, SKIP or expectedFailure.

| Run directory (under this runs directory) | Source | Literal result | result.json SHA256 | unittest.log SHA256 |
| --- | --- | --- | --- | --- |
| `dated-hitl-executor-red-temporal-001` | before `14ecce2c…` | 16 methods: 11 failing methods / 31 semantic assertion failures, 5 PASS | `0fe77c56596f65676f5dc239af73306e99d2781316c1b00eb00993ac675fda7f` | `c836a32b59776da5918fede5b64f88d99b6606addd8456bc932e74ccf3b00bf4` |
| `dated-hitl-executor-green-temporal-001` | current `11218cc2…` | 16 PASS | `b3c4b5374b818e2173b07ef42f229d6f9caced1f167a8d9e1726c50a5ed8c5f1` | `f1a41e2b393fae862a1e6daa77ca982fb2a4972ee8323141b664ee202897af4d` |
| `dated-hitl-executor-green-initial-001` | current | Original root date controls: 5 PASS | `2d9a150fcca45f72ae1ea39dc73274482b39e00004fedf048b521f234d38b7c4` | `6d525f66ab448507abf55909573f02f1ce496a37311409d7571ef7d6c38636a4` |
| `dated-hitl-executor-green-year-001` | current | Existing selected year class: 10 PASS | `2b0887641269d66d30965e8d29e448491b92a9b1a64b97d77f782807a0095f23` | `68458f6302f43dd3b28ee791495ff6926f686232acb62fdf7c6fc8b71138a22e` |
| `dated-hitl-executor-residual-legacy-001` | current | Separate residual witness: 1 PASS, not closure | `4a636946e6be9fdfd29a24f1e395ce8e6ff294799b729c4eefbdb2f92d6447dc` | `5f8b23226b4512aa8fa764fc78d9516cee79362f6fff31ec8841515b2c727407` |

The preserved original root `dated-hitl-red-001` remains historical (5 methods: 3 failures / 2 PASS on the old source). The new before run adds calendar and maximal-token coverage rather than replacing that evidence. Each full green log and the complete semantic-failure log were read. New date, fullwidth, valid/invalid, independent-name, annual, mixed version/date, current marker, projection, reconstruction, order and set-hash controls passed without changing frozen gold.

## Selected-byte evidence, failed verifier attempts and rollback

Read-only `dated-hitl-executor-byte-check.v3.py`, SHA256 `5e393dfa1b4cc77392f4fc7972a299d8ae39dc5013231307c56c7c7f21330217`, checked 11 explicit files / 189910 bytes, no product imports, 30 seconds, 1 MiB/file and 8 MiB aggregate. Its result `dated-hitl-executor-byte-check-result.v3.json`, SHA256 `c476d63409d155033712bd0aa167429f8e4ee5f714db0d50091a2cec6a20fe8e`, verifies current=after, original gold identity, original selected test hashes and exact in-memory forward/reverse application of the saved unified delta. This is mechanical evidence, not independent audit approval.

Earlier verifier attempts are preserved, not relabeled: v1 (`955775ed…`) failed on a mistyped existing-test path; v2 (`042d58d2…`) failed on invalid byte-comparison of system diff versus Python difflib renderings. See `dated-hitl-executor-byte-check-failure.v1.md` and `.v2.md`. v3 corrects only the artifact checks, not product/tests. These failures did not cause product test retries or product repair changes.

Rollback, if separately requested: require current resolver hash `11218cc2…`, then apply the exact inverse delta or the saved before bytes to this one product file only. Do not reset the repository or overwrite shared changes. Keep the new tests, original RED, all runs, snapshots and design history as evidence. No rollback was executed.

## Explicit residual and compatibility handoff

- A matching legacy decision still resolves a dated group and marks other members historical without the new read/index/answer-use approval schema, purpose, reason or trusted Human identity. The separate residual witness intentionally confirms this gap. Existing selected year controls also include legacy Human-selection compatibility; therefore 31 desired/collateral PASS must not be described as 31 independent consent tests.
- Approval schema, visible candidate explanation, keep-both/independent-period choice, fresh active-set decision checks across read/index/answer, app revalidation/update and downstream gating remain root-owned mandatory work. No end-to-end safety claim is made here.
- New grammar is bounded to 202/203 prefixes (2020–2039 when calendar-valid); existing standalone `20xx` year handling remains outside those compounds. Japanese month-only, era, English month names and arbitrary date syntaxes are not claimed parsed. `2024年` retains the historical `年` residue in directory-family keys; this F03 relationship limitation was deliberately preserved, not hidden by this correction.
- Existing `distribution/macos-local-memory/tests/test_document_version_resolver.py` still asserts resolver `0.1.4` and a dated-current automatic selection; `tests/test_unmarked_version_candidates.py` still pins `0.1.4` and its old policy literal. Their bytes remain unchanged and they were not run in full here. Those expectations need explicitly scoped additive compatibility handling by the parent, not silent edits to original assertions. Old `YearOnlySupersessionTests` class alone was selected; its separate old dated-current residual class was not relabeled current or rerun.
- A historical 0.1.4 graph is not claimed accepted by the new exact 0.1.5 reconstruction. Parent must verify normal rebuild/migration/app behavior before packaging or release. Old F05/F11 source-bound receipts remain historical and must not be labeled as runs of this resolver.

Adapter and Agentic Audit were used to preserve exclusive ownership, preimplementation literal gold, actual semantic RED, failed-attempt history, source-bound checks and the independent review boundary. No self-approval is asserted.
