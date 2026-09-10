# F11a contract addendum v1 — before implementation

Applies to `lms-v1-f11a-notebook-metadata-2026-09-09` and immutable `f11a-task-contract.v1.md`. Original contract is preserved. These are preimplementation clarification corrections, not formal product audit repairs.

1. Add allowed report reason `notebook_extraction_incomplete` whenever an applicable Document extraction status is partial or failed, even if all presented textual metadata matches. Do not falsely label successfully parsed text `notebook_state_unparsed` just because a separate visual extraction was incomplete. All applicable reasons are collected sorted unique; malformed known metadata still fails before a report. Missing facts caused by parse/raw fallback may additionally use notebook_state_unparsed.
2. Runner's prohibition of `actual import` means importing real user documents into indices, NOT Python importing reviewed production modules. Production functions/modules may be imported and executed under the frozen synthetic, no-external-I/O guard. No real corpus, published generation, model/network/GUI calls or external executable launches are authorized.

No other scope, acceptance limit, ownership or edit gate changes. Root's separate four boundary tests remain preimplementation gold; product editing waits for the explicit RED gate.
