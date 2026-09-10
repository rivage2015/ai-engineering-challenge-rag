# F01 / F07 — audit repair round 1

The v1 audit found a real check/use gap introduced by the initial repair: SQLite metadata reread `adaptive-reader-state.json` after validation and a model probe. A transient swap followed by restoration could publish an unvalidated binding while the app still reported its Reader generation current. This was not a passing condition; the original audit and execution records remain unchanged.

The new permanent fault-injection test failed before this repair. The fix now carries the Validator's detached result through lineage attestation, security attestation and graph projection to SQLite metadata. The Validator constructs only the explicit resolved path and hashes verified during that invocation; legacy returns explicit `None`. The late producer-state reread is removed. The strict lineage artifact schema and F18 mutation behavior are unchanged. Parent explicitly expanded ownership to the Validator's return report, so the issue now owns five source/test files.

All suites were bounded to 30 seconds and 1 MiB output. Results: focused 15 PASS; existing lineage 8 plus Reader migration 7 PASS; replay of the auditor's two v1 counterexamples PASS; selected Python 3.9 compatibility cases 4 PASS. No skips. The guards allow no actual model, HTTP, GUI, Keychain, external source or app-production-state actions. Full queries run with Python 3.14.6, not the unsupported Python 3.9 query path.

The source hashes, raw-log hashes, exact result paths, compatibility inspection, unresolved limits and replay instructions are recorded in `F01-003-executor.v2.json`. Re-executing a run must use a new run ID: existing evidence directories are immutable. `F01-003-regression-wrapper.py` preserves the otherwise ad-hoc guarded lineage/migration runner for audit reproduction.

Awaiting separate re-audit; executor does not approve this repair. No commit or release. Rollback must target only this issue's reviewed changes and retain other agents' work and the user's dirty files.
