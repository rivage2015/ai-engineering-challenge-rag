# F03a candidate completeness: bounded next contract proposal

Task: `lms-v1-00-hardening-2026-09-09-f03a-next`. Owner: `root/f02_contract_review`. State: proposal and reproduced BEFORE behavior, **not implementation authorization or product acceptance**. Source snapshot is `f03a-next-preflight.v1.json`. Resolver is 0.1.2 / `9a7443862c015ed768007ef5a7b21bd3bc197c9bb7a3d7ed2f7b39b33374359d`. F02a/F18 are accepted prior work, not to be reimplemented. Parent has agreed to the narrow semantic boundary below; a new immutable implementation task and fresh hashes must precede changes.

## 1. One invariant

Within the **existing exact normalized family key**, when at least one valid document has an existing year/version/lifecycle signal, every valid same-key document must participate in its candidate set, including signal-free peers. A mixed marked/unmarked family is identity-uncertain and held for Human review, never automatically resolved by a current marker, year, numeric version, timestamp, file size or content-hash equality.

This is candidate discovery plus conservative mixed-group holding. It does not decide whether the documents are revisions, independent annual records, or unrelated documents. `candidate_for` remains a candidate relation, not verified sameness. Retain originals. Held unknown is not the same as independent annual retention: this slice excludes the mixed family from the answer index until a currently supported Human active-choice decision; it does not implement a choice to use both independently.

## 2. Exact scope and proposed minimal change

1. Keep the present eligibility gate unchanged: inventory `kind=file`, `read_status=observed`, string `relative_path`, suffix in `DOCUMENT_SUFFIXES`, string `sha256`. Do not add readers, suffixes, content access, or new validity claims.
2. Separate valid candidate extraction from signal presence. `candidate(record)` may return the existing candidate shape for eligible signal-free records, with the five signal arrays empty. Add a small `has_version_signal(candidate)` helper for `explicit_years`, `explicit_versions`, `current_markers`, `historical_markers`, `draft_markers`. Do not add a serialized candidate field or change the candidate-set hash shape.
3. `build()` groups all valid candidates by unchanged `family_key()`. Emit a group only when its size is at least two and at least one member has a signal. All-unmarked families and singleton families stay ungrouped under the version policy. This preserves normal Reader availability; it is not proof of independence, freshness, or correct identity.
4. At the start of `automatic_selection()` (before all current/year/version branches), any signal-free member yields `(None, "unmarked_candidate_requires_human_review", all candidate paths)`. The build-level filter prevents all-unmarked groups. All-marked branch order and reasons remain exactly as accepted by F02a/F04a.
5. Keep `resolve_group()` ordering and Human logic unchanged: a valid complete-set/source-bound decision wins; a stale decision remains `stale_human_decision` with `candidate_set_changed` and no automatic fallback rescue. A Human may select a marked or an unmarked member after the full set is visible. A Human choice of one active file is not certification that all peers are revisions; richer choices remain F06/H2.
6. Preserve `family_key`, sorted candidate order, `candidate_set_hash`, graph shape, `group_id` algorithm and `record_decision` format. Bump resolver to 0.1.3 and describe the mixed-group guard in policy metadata/docstring accurately. Suggested policy fields: `candidate_rule="same_family_valid_documents_with_at_least_one_version_signal"`, `mixed_signal_action="needs_human_review"`; existing `year_order_establishes_supersession=false` stays. Final field spellings must be frozen in the implementation contract, not invented during repair.

No changes to Reader builder, adaptive Validator, projector, bootstrap, server, source inventory or production CONFIG are required for this slice's runtime routing. The existing Reader policy already excludes `needs_human_review` candidates. Its omission-as-ungrouped behavior explains the current leak but should not be changed globally here.

## 3. Exact BEFORE observations

`f03a-next-baseline-001/result.json` and `unittest.log` were produced by the reviewed H0 supervisor running `f03a-next-probe.v1.py`. Four observation methods, zero skips, 30 observations, Python 3.14.6. Worker 0.005285 s; supervisor 0.076144 s; cumulative synthetic inventory/decision bytes 26,054; resolver bytes 21,959; log 20,458 bytes. Log SHA-256 `39ce1c65eec0feda10ba0cc84966aed27e1d638fb667903938390f4e47d4f75a`.

The tests assert existing defect behavior. Their successful execution is **not a GREEN acceptance run** and does not replace a future frozen failing F03a test suite.

| Case | Actual 0.1.2 BEFORE | Proposed F03a expectation / boundary |
|---|---|---|
| `業務内容2024.xlsx` + `業務内容.xlsx` | `[candidate, None]`; group 0 | Same-key mixed group 1; both held, no active edge |
| `記録/実績2024.csv` + `記録/実績.csv` | group 0 | Both held; the word 実績 does not classify annual vs revision |
| `手順_ver1.csv` + `手順.csv` | group 0 | Both held, no numeric fallback |
| `現行/手順.csv` + `手順.csv` | group 0 | Both held even with unique current marker |
| Draft or historical marker + same-key unmarked peer | group 0 | Both held; no marker-derived identity claim |
| `2024/業務内容.xlsx` + `業務内容.xlsx` | same key; group 0 | Mixed group held; exact current normalization retained |
| `2024年/業務内容.xlsx` + `業務内容.xlsx` | different keys (`年` parent remains); group 0 | Residual, unchanged; no additional directory stripping |
| XLSX + PDF with same stem | suffix makes keys different; group 0 | Residual extension-change relation; do not merge formats |
| `業務内容2024.xlsx` + `業務手順.xlsx` | stem makes keys different; group 0 | Residual meaningful rename; no semantic matching |
| `部署A/業務内容2024.xlsx` + `部署B/業務内容.xlsx` | parent makes keys different; group 0 | Preserve separation; same name is not identity |
| `業務内容.xlsx` + `業務内容コピー.xlsx` (also `_` variant) | same key, both candidates None, group 0 | All-unmarked group stays absent; ordinary Reader eligibility retained, not verified-independent |
| Human selection on ver1/ver2, then add `業務内容.xlsx` | inventory hash changes but candidate-set hash unchanged; old Human active persists, unmarked omitted | Three candidates; same group ID, changed set hash, stale Human decision, all held |
| Omitted unmarked content changes | group/hash/Human choice unchanged | Once included, any member source change changes the set and stales the full-set Human choice |
| Rename ver2 to same-key unmarked name after Human choice | remaining marked singleton discarded; group 0 and old decision not evaluated | Two candidates remain grouped; old set hash changes; stale Human choice held |
| Move peer to different parent, rename to different stem, or change suffix | old group disappears; decision not explicitly invalidated | Concrete residual: no blanket move/rename/suffix invalidation guarantee |
| Add marked ver3 after Human choice | set changes, `stale_human_decision` | Existing behavior preserved |
| Add different-family/suffix/parent peer | targeted set and Human choice unchanged | Preserve exact-family boundary; cross-family identity remains unresolved |

Additional control observations retain F02a year hold, F04a single-numeric conflict hold, and successful year-free numeric selection.

One separate **counterfactual direct `resolve_group`** call supplied a manually constructed unmarked candidate without modifying source or `build()`. Current `automatic_selection` resolved `現行/手順.csv` and made `手順.csv` historical. This proves that discovery-only inclusion is insufficient. It is explicitly not actual current build output.

## 4. Candidate-set and Human invalidation boundary

The hash currently includes sorted path, source hash, mtime, birthtime and the five signal arrays; it does not include file size or content meaning. Keep that format, including existing metadata-triggered invalidation. A group ID depends on the unchanged family key, not inventory order.

- Adding a same-key unmarked member to a marked pair must alter the set hash and invalidate any previous choice. Addition while a two-member mixed group already exists is also covered.
- Changing an included unmarked member's hash, path or bound metadata must invalidate a previously valid full-set choice, whether the selected member changed or another member changed.
- Renaming marked to unmarked **within the same family**, with another marked member remaining, must preserve group presence and stale the old choice. Reordering records alone must not change the group/hash/projection.
- A genuinely different key does not alter the targeted group's set. Cross-key historical identity, explicit source-root/generation binding, all-marked-to-all-unmarked renames, removal to a singleton, stale decisions for omitted groups, same-root rescan completeness and freshness at question time remain open. A scan/build-time local candidate hash is not a global Human-decision lease.

## 5. Frozen acceptance to establish before implementation

Create a dedicated new pure test file (suggested `tests/test_unmarked_version_candidates.py`) only after parent assigns ownership. Fix exact expected results before editing product code, run it RED with the unchanged resolver, and preserve all failures and hashes. Suggested acceptance methods:

1. Eligible unmarked candidate retains existing fields with all five arrays empty; unsupported suffix/directory/non-observed/missing-or-nonstring SHA/invalid path shape remain rejected. Do not broaden malformed-record validation.
2. Year-name, neutral/annual-word, year-directory, version, current, historical and draft mixed cases each yield exactly one group with all expected candidate paths, all `needs_human_review`, null selection/basis, specified reason and all-path conflicts; no active edge.
3. Mixed current+two-marked peers stays held, including a marked numeric/year conflict. This only tests the new mixed-group priority, not a broader F04 signal solver.
4. All-marked year hold/F04a conflict/current control/year-free numeric success remain unchanged; do not count existing known current/year/multi-token residual witnesses as improvement acceptance.
5. All-unmarked copy/neutral-name variants remain ungrouped; mixed family plus unrelated all-unmarked or other-family source must not absorb or hold unrelated sources.
6. Different suffix, meaningful parent/stem, and `2024年` parent boundaries stay unchanged. Keep separate residual witnesses rather than disguising these as F03 solved.
7. Full group/nodes/edges/hash invariant under all permutations of a three-member mixed fixture; graph timestamps/source inventory hashes are not compared as order-invariant values.
8. Full-set Human decision selecting marked or unmarked member resolves exactly that member. Same set is reusable; changed member content and later same-key unmarked addition stale the choice, with no marker fallback.
9. Same-family marked-to-unmarked rename keeps group present and invalidates old Human choice. Cross-family moves/renames/suffix changes and dropping all signals are explicit residual witnesses.
10. Existing resolver/Reader test file: add synthetic mixed-family Reader-policy and small CSV Reader checks. Only unrelated source reaches manifest; held paths have no Evidence/index entry; original hashes unchanged. Do not alter existing successful numeric-answer fixtures or F18 explicit initialization flags.
11. Existing app E2E: add one mixed-family held path using mock model/IO harness and one all-held failure case. Preserve prior public CONFIG/index hashes and its proven normal answer; do not publish new held inputs. Distinguish rejected new generation from stale review-publication F06 residual.
12. Metadata version and documented candidate policy match behavior; no source/input/CONFIG/network/model/process mutations by worker; focused suite and related accepted regressions pass within 30 s / 1 MiB each, zero required skips.

The existing resolver test asserts version 0.1.2 explicitly and will need a deliberate metadata expectation update only if 0.1.3 is adopted. `candidate()` call search found the product build loop and the frozen pure year tests; all-marked year tests should remain compatible. No production caller requires a candidate-shape change. Existing all-marked gold fixtures must not be rewritten to hide failures.

## 6. Downstream consequences and remaining Human work

Reader `apply_document_version_policy` maps graph disposition to eligibility; absent paths are currently `version_ungrouped`. New mixed-group membership will intentionally reduce eligibility only for that matching family. This is a temporary conservative loss of answers, not successful support of annual comparisons. Avoid claiming improved answer quality solely from more holds; prove the unrelated and normal numeric answers still work.

HITL UI currently lists only held groups and offers one required active radio choice. It has no independent/both/none/keep-held correction path; automatic groups are not shown. F03a improves visibility of mixed groups but does not make that UI a complete Human classification flow. Record-before-rebuild and review publication before successful index publication remain F06/F19; no UI, lease, generation or source-root changes here.

F04 multi-token/year/status conflict semantics, F05 inventory-independent candidate/graph recomputation, F02 independent annual retention, F06/F19 Human correction and generation safety, F13 latest-source freshness, extension migrations and semantic identities remain unresolved. The current graph Validator can still accept self-consistent omitted-candidate graphs; F03a producer completeness is not adversarial validator completeness.

## 7. Safety, rollout, rollback and handoff

Preflight and executable sources were reviewed before the one baseline run. Worker is in-memory after source/stdlib preload; only IO boundaries and clock are replaced. The H0 supervisor owns one new bounded log directory. No real files, Reader, model, network, HTTP, UI, CONFIG, public index, `record_decision`, or `validate` calls were executed. The Python guard is not OS isolation.

Next bounded phase: parent freezes this design with fresh source hashes and assigns resolver + new dedicated test, plus narrowly approved existing tests. Executor first writes immutable implementation authorization and RED evidence, then implements the small extraction/group/mixed guard change. Parent arranges a separate-context audit and deterministic source/delta/schema checks. No self-approval; maximum two repair rounds; retain historical failed records.

No product rollback is needed now. Future rollback is the exact isolated resolver/test delta against the implementation's saved before hashes, preserving prior F02a/F04a/F18 changes and all user edits. Do not restore a whole git checkout or reuse a now-stale published generation as proof of freshness. Schema migration, reindex/review publication and release deployment are not authorized here. No commit, push, original-data access or approval bypass.
