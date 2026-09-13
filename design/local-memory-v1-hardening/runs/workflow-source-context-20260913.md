# Workflow source-context integration (2023 test baseline)

## Before

Real gemma4:12b execution took 110.236 seconds and returned insufficient evidence. The question graph reported `question_operation_not_supported`; retrieved sources included unrelated operation-experience worksheets and introductory PDF pages. This was a retrieval/coverage failure, not proof that the reception instructions were absent.

## Change

The answer engine now has a bounded source-context route for an explicit year and uniquely named Japanese worksheet when a workflow is requested. It follows validated stored provenance paths, supplies source rows beginning at the unique first numbered step, and retains cell-level safe content when a mixed row contains credentials. It does not infer chronological or causal business edges and does not mark the existing question graph as supported. The record reports this separately as `workflow_source_context`, with coverage unknown.

Credential detection decodes JSON string observations both before retrieval ranking and at the model context boundary. Required evidence omitted from the context still fails closed, including retry and batch paths.

Actual read-only context check: eight reception locations selected; eight supplied; 1,981 characters. No original workbook, published index, application bundle, or version decision modified.

## Verification

72 relevant unit tests passed after source-scope guards, prompt instructions and workflow-specific citation/output budgets.

Live rerun 2 (176.067 seconds): selected all eight target packets but produced only headings, rejected caution content by heading name, and misread source location as a storage-location question. Not accepted.

Live rerun 3 (177.601 seconds): produced greeting, reservation question, both branches and cautions, but independent final processing demoted the answer to insufficient. Exact-value validation correctly rejected altered labels, a typo, and uncited later branch actions. Its final `accepted` status approved an insufficient answer, NOT the desired workflow answer. Never report this as success.

Further corrections: workflow citation capacity now scales to supplied packets (up to 24), instead of four; exact source lines are requested without invented labels; planner distinguishes citation locations from physical storage; workflow generation budget expanded while ordinary question budgets remain unchanged. Fourth live run uses sequential auditing and is pending. Installed app remains unchanged.

## Limits

Fourth live generation finished in 176.524 seconds in sequential mode. Source-text validation passed workflow (C1) and cautions (C2), but source-location (C3) failed and was demoted. Gemma final audit repeatedly returned verified with a nonempty unsupported_claims array; no failure was promoted. A diagnostic capture showed unsupported_claims=[C3]. Increasing final output budget did not resolve it. Granite rejected the request with HTTP 400 (cause not established).

Qwen3.5:9b final audit completed with an accepted, answered, qualified result. The workflow field remained confirmed with eight Evidence IDs and concrete greeting/reservation/branch/handoff text. The standalone caution field and location field remained unresolved. This is a partial workflow-answer milestone, not full acceptance. Detailed local-only records are in the ignored `.tmp/workflow-audited-20260913.json` and `.md`. Installed model configuration and app were NOT changed. No processes started by this test remain running; Ollama itself is left running.

Next: fix source metadata projection and standalone caution audit, then test the same production audit mode and package/app/UI path. Do not ship this as fully accepted or claim generic workflow GraphRAG is implemented. Final-audit prompt now explicitly rejects contradictory verdict/schema combinations and allows a larger workflow output budget; mechanical gates unchanged. HTTP-inclusive regression group passed 31 tests after sandbox retry. Workflow/source-context regression group passed 72 tests; additional final/source regression group passed 50 (overlapping groups, do not sum).

This is bounded source-order context, not a completed workflow/branch graph. Ambiguous worksheets raise a confirmation-required error but no new confirmation UI is implemented here. No explicit year falls back to the pre-existing route. Extraction coverage remains unknown; 2023 is not asserted to be the latest operating policy. Installed app update and UI acceptance are not done.

## Rollback

Remove only the workflow_source_context function, its main-loop integration and record metadata if rollback is needed; preserve credential-boundary protection. Do not reset unrelated worktree changes. No commit or push.
