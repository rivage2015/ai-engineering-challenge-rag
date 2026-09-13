# 2023 baseline workflow progress

## Scope

User approved the 2023 workbook as an update-test baseline, not current operational authority. Original documents and installed application were not modified. Existing unrelated worktree changes were preserved.

## Implemented

`workflow_step_selection.py` now includes content on the numbered heading's own row. The next heading remains exclusive. Three regression tests cover correct ownership, eligibility rejection, and a step whose only body is on its heading row.

Before modification all three new tests failed. After modification 40 tests passed:

`PYTHONPATH=tests python3 -B -m unittest test_workflow_step_selection test_workflow_step_selection_multiline test_workflow_index_binding test_workflow_reconstruction test_workflow_reconstruction_binding -q`

These are synthetic unit tests, not an application acceptance test.

## Read-only source-layout verification

The indexed 2023 reception sheet has a numbered heading at A7 and its question at C7. Conditional labels occur in other columns; operational cautions occur in reference columns. Reference data also contains authentication material. Do not copy whole reference columns or raw row records into model prompts, logs, fixtures or reports. Do not weaken the existing eligibility policy to make workflow selection succeed.

## Remaining / resume here

1. Inspect existing eligibility exclusions using IDs and reasons only, without printing raw contents. Determine whether workflow scope can be safely validated despite excluded authentication cells; retain an explicit coverage hold for potentially missing business information.
2. Design source-bound selection of conditional labels and cautions across columns. Preserve original locators and distinguish source order from business chronology. No blanket reference-column inclusion.
3. Apply candidate-size limits to a validated scoped selection while retaining global graph validation; the current index-binding helper has a global 10,000-record limit.
4. Connect the helper to confirmed intent and answer generation with revision checks. The helper still has no production caller.
5. Verify the actual 2023 question through the application, including citations, conditions and cautions. Package/app update and UI acceptance are not done.

## Rollback

Revert only the comparison in workflow_step_selection.py from `row <= body_row < stop` to `row < body_row < stop` and the three added tests if needed. Do not reset the dirty worktree. No commit or push performed.
