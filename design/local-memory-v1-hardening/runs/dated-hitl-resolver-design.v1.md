# Dated Human gate — resolver design v1, not implementation

2026-09-09. Executor read-only design under codex-graph-engineering-adapter / graph-engineering-agentic-audit. `runs/` means `design/local-memory-v1-hardening/runs/`; other paths are repository-relative. User-priority scope is `runs/dated-hitl-scope.v1.md`, SHA `67a6dd83073af3f6fc58e17e657504bacd0059d1aa2f1d7909eec870c1826a49`.

F11a remains paused at generator v3 **unexecuted**, without acceptance. This work continues the existing F02/F03/F04/F06/F13/F19 residuals; it is not a new name for resetting correction/audit budgets. No product, existing test/gold, F11a artifact or run record was changed. No module import, test/generator execution, source-document read, network, model or GUI operation occurred. This new document is the only write.

## 1. Bounded outcome and limit

Recommend first closing this resolver-only invariant:

> Within the explicitly supplied inventory and current filename-family mechanism, an unresolved family containing a supported year/full-date signal cannot gain an automatic active member from a date, a current/final/approved filename marker, or a numeric version. Calendar-valid dates only propose membership. Pending members all remain held; no older member is substituted.

This is **candidate discovery plus removal of automatic selection**, not complete dated-document approval. Existing `record_decision` represents one hash-bound selected path, not the three independent questions “same work?”, “applicable now?”, and “may this content be ingested/used for answers?”. Keeping that distinction is mandatory. A narrow resolver patch must not be released or described as the user's complete Human gate while legacy one-click selection can still stand in for those approvals.

The root's original five-method run is a real saved baseline: 3 assertion FAIL (compact grouping, Japanese grouping, dated current-marker autoaccept), 2 PASS (different procedures, year-only hold), 0 ERROR/SKIP/expectedFailure. `dated-hitl-red-001` finished at 2026-09-09 09:39:23 UTC, elapsed 0.051879 seconds, 3,724-byte log, bounds 30 seconds/1 MiB. These are read results, not this turn's execution.

## 2. Current code facts

| Boundary | Current source and finding |
| --- | --- |
| Filename family | `engine/document_version_resolver.py:37,91–113`: only a numeric-boundary `20xx` year is removed; `_family_component` also strips ver/version, copies and lifecycle markers. Compact eight-digit dates retain all digits; Japanese dates lose only their year, leaving `年M月D日`; separated dates leave month/day. Parent components and extension remain part of the key. |
| Candidate scope | Same file `149–178`: observed inventory file, string hash and one of `.csv/.doc/.docx/.ods/.pdf/.ppt/.pptx/.tsv/.xls/.xlsx`. Signals are read from path metadata only; no source bytes are read. `.txt/.md/.ipynb/.html` and other Reader-readable formats are not currently version candidates. |
| Auto-selection leak | Same file `205–279`: unmarked family holds first. A unique eligible current marker can return an active path at line245 even when years exist, unless an earlier year/version conflict holds it. Only the later all-year branch prohibits year-only supersession. A family where only some members have years may also reach numeric-version selection at line274. |
| Human selection today | Same file `282–336,663–696`: candidate-set hash + selected path/source hash allows `resolution_basis=human`; all other members become historical. There is no independent/same-work relationship, use-scope, revocation or policy-aware consent field. Actor/time are metadata, not authenticated proof of those extra decisions. |
| Bound set today | Same file `188–202`: ordered candidate paths, source hashes, mtime/birthtime and five signal fields enter the set hash; size does not. `resolve_group` sorts paths by UTF-8 bytes before hashing. Changing an included peer or adding a discovered same-family peer makes a saved decision stale. This is not proof that undiscovered/unsupported peers do not exist. |
| Reconstruction | Same file `511–539,556–660`: attestation independently rebuilds groups/counts/projection from explicit inventory and explicit decision snapshots, compares exact canonical JSON and resolver/policy identity. Graph-carried paths are diagnostic only. Do not weaken this to comparing just graph hashes. |
| Reader effect | `engine/build_adaptive_semantic_graph.py:192–240`: historical and needs-review members are removed from selection. Ungrouped inventory items remain eligible. Therefore “not grouped” is not “Human approved”, and singleton/unknown-family coverage remains a real follow-on. |
| Generation and app | `app/bootstrap.py:365–367,690–750,3754–3777`: resolver file identity belongs to the generation contract; the app captures an immutable decision snapshot, builds and validates the version graph, then runs Reader. A valid graph with held groups still receives resolver validation PASS; the Reader filters those members rather than treating the whole graph as invalid. |
| Existing approval UI | `app/local_memory_server.py:648–706,2693–2724`: radio selection asks which one to use now and requests rebuild. It does not first distinguish same-work revisions from independent annual records or separately authorize answer use. The saved snapshot is not a fresh scan for newly added peers at answer time. |

No app/answer-time freshness guarantee is inferred from the immutable-generation controls. Preserving an old index file after a failed rebuild is not permission to answer from an older unapproved version.

## 3. Minimal candidate-only date grammar

Recommend one private pure helper in the existing resolver, with no new module or package dependency:

`_temporal_component(value: str) -> tuple[str, tuple[int, ...]]`

Return (a normalized component with **only accepted temporal tokens** replaced by spaces, sorted unique signaled years). `_family_component` continues its existing version/copy/lifecycle/punctuation processing after this step. `candidate` unions signaled years across the basename stem and each parent component. Both consumers must use the same helper; independent regex substitutions would again let graph membership and signals drift.

The proposed JSON candidate shape stays unchanged for this first slice: `explicit_years` includes years from calendar-valid full-date tokens as well as existing standalone years. Do not invent `explicit_dates`, precision or confidence fields in only the producer without updating every hash/projection/reconstruction contract. Exact date labels and relation/use approval metadata belong in a separately frozen schema/UI contract. This compatibility choice does not make an observed year authoritative.

| Accepted full-date form after existing NFKC | Literal examples | Exact proposed parsing rule |
| --- | --- | --- |
| Compact `YYYYMMDD` | `20240101`, `20250202`, `２０２４０２２９` | Exactly 8 ASCII digits after normalization; no adjacent decimal digit; year2000–2099; valid Gregorian calendar date. |
| Japanese `YYYY年M月D日` | `2024年1月1日`, `2025年02月02日` | Year4 digits, month/day1–2 digits, all three literal units including 日; no adjacent decimal digit; valid calendar date. |
| Separated `YYYY-MM-DD`, `YYYY_MM_DD`, `YYYY.MM.DD` | `2024-1-1`, `2025_02_02`, `2024.02.29` | One selected separator repeated between all components; month/day1–2 digits; no mixed separators; valid calendar date. |
| Existing standalone year | `2024`, `2025年`, filename or directory | Preserve the current numeric-boundary `20xx` behavior outside full-date-shaped spans. A year is a candidate hint only; 年/年度 labels are not silently converted into a same-work assertion. |

Use fixed-width/bounded regex alternatives to locate complete-date-shaped spans, then `datetime.date(year, month, day)` solely for calendar validation. No Notebook/document execution, clock-relative “latest”, locale parser, model inference, dateutil or filesystem mtime-as-authority. Retain the existing supported year domain2000–2099 in this slice rather than silently broadening all historical/future year grouping; pre2000/post2099 support is an explicit follow-on, not invalid calendar data globally.

Order matters: recognize **maximal complete-date-shaped spans before standalone years**. The lexical recognizer should capture both separator positions independently; mixed forms such as `2024-01_01` are a recognized span but fail the same-separator acceptance check. On a recognized invalid full date such as `2024-02-30`, `2024年13月1日` or `20230229`, preserve the entire normalized token. Do not erase its year as a fallback, drop the offending day/month, clamp it, or reinterpret it as a valid date. The helper must exclude the interiors of these recognized invalid spans from the standalone-year substitution. Outside those spans, existing year behavior remains. Invalid/malformed data is not evidence of an approved or safe ungrouped document; uncertain-family gating remains mandatory downstream.

Two qualifications keep this rule precise: (a) temporal-helper preservation does not disable the existing later punctuation normalization of a family key; all invalid date digits must remain and the raw candidate path stays unchanged; (b) full-date recognition must not take over spans already identified by the existing `VERSION_TOKEN`. Within explicit ver/version spans preserve the prior standalone-year detection instead. In particular, `ver2024.2.30` must not lose its pre-existing year caution because it looks like an invalid calendar date and thereby gain numeric-only automatic selection.

Never combine tokens across `/` path-component boundaries: `2024/01/01/file.xlsx` is a hierarchy, not a filename date token. Existing year/lifecycle parent normalization stays; only actual accepted date tokens in a single component gain new normalization. Generic cross-directory merging, basename rename, similarity, and cross-extension grouping remain out of this patch. A date-only canonical stem may be empty; any resulting group is still only a candidate family and must remain held without appropriate Human authority.

### False positives and unsupported forms

Calendar validity cannot prove a token is a document date: an invoice/product identifier `ID20240101` can also look like a valid date. Retain this as a possible candidate/hold, not replacement evidence; show the original filename to the Human. A nine-digit identifier such as `202401011` must not be partially matched. Fullwidth decimal normalization is deliberate; other numeral scripts/era names are not guessed.

Month-only `2024-01`, fiscal-year `2024年度`, quarters, ranges, era names, two-digit years, DD-MM-YYYY, timezones and execution timestamps are not fully parsed by this grammar. An embedded valid day part or a standalone year may still supply the explicitly limited existing hint, but no timestamp/fiscal applicability semantics follow. The exact version string `ver2024.1.2` is also ambiguous; do not remove existing year-based caution or allow automatic adoption merely by relabeling it as a numeric version. Unknown syntax requires additional discovery/approval work; do not report “no date” as “not versioned/approved”.

This is linear scanning over supplied path strings with constant-size calendar validation per token; it introduces no source reads, network, full-document cache or new production size cap. Existing inventory/snapshot bounds are not expanded. Before running any new fixture batch, root should freeze a finite synthetic path-record fixture total (recommend ≤16 KiB, no huge inputs) and existing 30-second/1 MiB-log guards. That is a test resource cap, not a new product truncation rule.

## 4. Exact automatic-selection policy proposal

Preserve existing failure reasons where possible; place a dated-family stop immediately before **each positive automatic return**, rather than turning prior conflicts into acceptance or rewriting all established reason strings.

1. Keep unmarked-member hold and all current-marker conflicts. Keep year-only tie/draft/historical/year-order holds; in particular the frozen root positive expects `year_order_does_not_establish_supersession` for ordinary year-only peers.
2. Before the current-marker branch can return `unique_explicit_current_marker`, if **any member** has temporal years, return `(None, "dated_family_requires_human_review", all_candidate_paths)`.
3. Before numeric-version comparison can return a selected path, enforce the same guard for any temporal member. A dated member mixed with a signal-free member already holds under F03; a dated member mixed with an undated current/version peer must not fall through to automatic selection.
4. Never choose “the previous version” as a fallback. `resolve_group` retains every candidate with `needs_human_review`, null selected/basis, and no `active_version` edge when held. Candidate-order permutations must produce the same resolved graph after existing sorting.
5. Non-dated existing unique-current and numeric-version-only controls remain unchanged. Historical/draft conflicts and F04's higher-numeric-version conflict remain held. This scope is not authority to weaken those gates.

Suggested resolver identity: `0.1.4 → 0.1.5`, with graph `SCHEMA_VERSION` unchanged at1.0 if candidate/group shape is unchanged. Update the exact policy declaration, for example `automatic_rule="mixed_signal_hold_else_current_conflicts_hold_else_dated_family_hold_else_unique_current_or_comparable_latest_version"`, and add explicit `date_tokens_are_candidate_only: true` / `dated_family_requires_human_review: true`. Root must freeze final literal policy before code/test changes. Resolver/policy identity mismatches must still be rejected by `attest`.

### Human authority is a separate remaining gate, not solved by renaming a reason

The above closed invariant is conditional on **no reusable existing Human selection**. `resolve_group` currently consumes a matching old decision before `automatic_selection`; changing automatic logic alone does not invalidate it. A year-only family's key/set hash can remain identical under0.1.5, so an old one-click selection could still be reused on rebuild. Resolver version drift causes a generation migration, but is not by itself consent invalidation when the same decision is rebuilt into a new graph.

Therefore the complete dated-family gate must require a new explicit approval authority: same-work relation or independent records, applicable member(s)/time scope, and permission to ingest/use answers, bound to original content, **complete discovered set**, discovery-policy version and approval schema/version. A legacy single-path decision cannot be silently promoted to it. Recommended safe integration policy is to require reapproval for dated families at that authority boundary; until that contract/UI is fixed, keep the resolver correction labelled candidate/automatic-hold only and do not treat existing `resolution_basis=human` as the new use approval.

Independent annual records may both remain relevant; choosing one must not mark all others superseded merely because they share a normalized family. Current `resolve_group` has only one active path and historical-all-others, so relation classification and multi-record permissible-use policy need a separate schema/UI implementation. Persisting, revoking and refreshing that authority across read/index/retrieval/answer, asking one question at a time, and refusing prior-index fallback while approval is pending remain mandatory. They are not optional omissions from the user task and must be closed before end-to-end acceptance.

## 5. Reconstruction, generations and exact implementation footprint

The **only proposed first-slice product edit** is `distribution/macos-local-memory/engine/document_version_resolver.py`. Its full pre-edit hash is `14ecce2c01d68bfbfaa27d3022bfc6076a97465a331eb03443af0e6d50cc006f`. No before-byte snapshot was created in this design-only turn; preserve one with apply_patch and verify it against this hash immediately before any root-authorized edit.

Affected resolver functions: temporal constants/new private helper; `_family_component`; `candidate`; the two automatic positive branches; `validation_policy`; resolver version/docstring. `has_version_signal`, candidate JSON fields, `candidate_set_hash`, `graph_projection`, `_validation_components`, `attest`, `validate`, authority modes, CLI arguments and snapshot read limits should retain their interfaces. Reconstruction must independently invoke the changed candidate/family/policy functions from caller snapshots; never copy proposed group membership or Human status out of the submitted graph. No graph-derived path may create approval authority.

New date-family keys can merge former separate groups and will change their group IDs; do not remap old approvals by basename/date similarity. Calendar signals can change set hashes even when content bytes do not. Unchanged year-only old decisions remain the explicit legacy-approval caveat above, not an overlooked freshness guarantee. Existing graph files, decision snapshots and generations remain immutable; do not “migrate” their hashes in place.

Reader, adaptive Validator and index projector already consume reconstructed dispositions through the explicit F05b attestation path. They need regression coverage, not an inferred edit for this minimal parser/policy change. Bootstrap fingerprints the resolver file; changed bytes should require a new generation. Do not assert that this automatically disables all old-answer paths without fresh app/answer tests. The existing package build copies engine code; a helper inside resolver needs no protected build-script edit. F11a's frozen eleven products remain untouched; any later dependency/audit refresh must say that a resolver dependency changed, not relabel historical F11a app results as a new run.

## 6. Literal tests to freeze before correction

Retain `tests/test_dated_document_human_gate.py` and its original five-method RED unchanged. Add independent literal tests (separately named new file/gold, no SUT-generated expected results) covering:

- Accepted compact/Japanese/separated representations and NFKC fullwidth controls; same semantic stem/folder/extension groups; different procedure/folder words/extensions do not. Specify exact family keys and explicit-year lists, not only equality between two SUT outputs.
- Leap2024-02-29 accepted; 2023-02-29, month0/13, day0/32, malformed/mixed separator, eight-/nine-digit boundaries and invalid-token year fallback rejected/preserved as specified. Keep all original filenames in candidate objects. No actual source files are needed.
- Dated current marker (newer, equal, older), undated current plus dated peer, full-date plus numeric-version and any candidate-order permutation all select none absent proper authority. Exact group counts/dispositions/edges; no older-file substitution. Pure undated current/numeric controls retain their original outcomes.
- Independent annual records stay in the candidate graph and are not automatically labelled historical; a separate future relation decision must be able to distinguish “independent” without deleting records. Do not falsely claim the current single-selection decision schema already supports this.
- F05a forged date signal, missing candidate, forged active edge, policy/version mismatch, added peer and stale snapshot attacks: graph hash recomputation alone must not pass independent reconstruction. Verify `no_decisions`/explicit/snapshot mode controls and strict duplicate JSON/numeric-type rules remain.
- Reader selection contains unrelated control documents but no held dated-family members. A rebuilt held generation and an older index/approval authority are distinct; real app tests for “pending → no old fallback” are required in the later integration gate, not inferred from these pure tests.

Existing tests with intentional expectation impact: `test_document_version_resolver.py:78` version literal; its year+current positive at81–88 changes to hold; year-only conflict/draft reasons should stay preserved. `tests/test_unmarked_version_candidates.py:284,287` version/policy literals change. `tests/test_year_only_supersession.py:174–184` is an explicitly labelled current-marker residual witness of the old bug; preserve its original gold/result and replace its use with new desired-contract evidence rather than quietly editing it and claiming the old witness still passes. The other ten F02a controls should remain meaningful. F03 residual path shapes, F04 numeric conflicts and all F05a/F05b attacks must not be weakened to get a green batch.

Whether/how to update executable existing compatibility assertions is a future root-owned exact delta decision. No existing gold/test alteration is authorized by this document. All attempted runs must use fresh IDs; unexpected failures stop for diagnosis, not an automatic broader repair.

## 7. Fresh source / test / receipt pins

These are read-only current hashes, not evidence that the proposed patch ran. `engine/`, `app/`, and `build/` in the dependency table are under `distribution/macos-local-memory/`.

| Exact dependency path | SHA-256 | Role for first slice |
| --- | --- | --- |
| `engine/document_version_resolver.py` | `14ecce2c01d68bfbfaa27d3022bfc6076a97465a331eb03443af0e6d50cc006f` | Sole proposed product target, pending root gate. |
| `engine/build_adaptive_semantic_graph.py` | `5d2883e2a053776935d71b2486180b177078fe71b5d7a0a02bf8741e8e041c0a` | Read-only selection consumer. |
| `engine/validate_adaptive_semantic_graph.py` | `c2587b685d06a1e8d1ec006bb2577be47168a0c14b072a22b3a90878c30563e3` | Frozen F11a, attestation consumer. |
| `engine/build_local_semantic_index.py` | `c70f36d98012cca29877af72e4d345c30a555e1fef24e34ebc057b4f823e7229` | Frozen F11a, projector revalidation. |
| `app/bootstrap.py` | `e6248aae9ffa89e3cd6a43839af7f41e4d5941482f8f00a48f466de5524b526b` | Snapshot capture, version gate and generation fingerprint. |
| `app/local_memory_server.py` | `3acb859916ccb9fb1a1c26cc0ebfa511bea2d8114fa83103ace40183fa899ee6` | Existing one-choice UI; wider approval follow-on, not first-slice edit. |
| `build/build_package.sh` | `10989f5f941c1e567a7a8c7fe82f2d5d5d88e66345d4503a1600c7c9827bd02f` | Protected existing user change; do not edit. |

| Test / receipt path | SHA-256 |
| --- | --- |
| `tests/test_dated_document_human_gate.py` | `ee62aaf42632bca032c80a40a54893a817cd78f72c5d95e1c1078ba96a69586a` |
| `distribution/macos-local-memory/tests/test_document_version_resolver.py` | `594089cdee5e0298c5c732e7da369417e14de7fea6282594acc43a55c6a4a126` |
| `distribution/macos-local-memory/tests/test_versioned_safe_index_e2e.py` | `bd967cb2e4d22aa3985d59514a72e22e42e9e9d03841dbc6b80164a69c9f97ff` |
| `tests/test_year_only_supersession.py` | `70a1226cb30daa6692533d2c6f1fe270fe301a8f759c0db56a142acbc945b8c0` |
| `tests/test_unmarked_version_candidates.py` | `7bf83e2823742cc1449fdd69890d780e8c8b8daf69911905ce6cb20af2ad1f29` |
| `tests/test_version_graph_reconstruction.py` | `33dc00f8c0cbe2bbf095c9236da32f74ea3ccc87a97f995b05005f999a8028ba` |
| `tests/test_decision_snapshot_attestation.py` | `ef223b71064f8c0094c906fa19855cc7a62f91054ecb405cb9404eb3e7f8787a` |
| `tests/test_decision_snapshot_e2e.py` | `14ad1a5a53f9e5251f0947e20e33980eb5d62ec284a5283cef17d2d22d2ab27c` |
| `tests/test_decision_snapshot_controls.py` | `ff28bd5b93c8410f7928b2cb38ca981d1096abc44cf9373f970d64b2b587b7e6` |
| `tests/test_version_graph_validation_e2e.py` | `eb160ba4140aa4c8360f3d7c8bb626d9c1c7edc432d69b81b6104e8ffbc26c69` |
| `runs/dated-hitl-red-001/started.json` | `0b9d6f54f4631472fd2d4c39001e0f1248cf32e1d65ab2fccd86f530039fee81` |
| `runs/dated-hitl-red-001/result.json` | `a8d05723d302d21b19c6008f73848bf4a50ce141794b447a20ee05bbc52ac252` |
| `runs/dated-hitl-red-001/unittest.log` | `52c1111409180893248821190933a970c3bb3a7342e803f483d00ab76bc792ee` |

Before any correction root must fix the exact private-helper grammar, policy/version literals, original/new test ownership, before snapshots, bounded runner/gold, and the staged separation from the mandatory relation/use/freshness/UI gate. Rollback for a future resolver patch is its isolated inverse against verified before bytes, without restoring or overwriting any user decision/source/generation. Today's rollback is simply to stop using this new design note; nothing else changed.
