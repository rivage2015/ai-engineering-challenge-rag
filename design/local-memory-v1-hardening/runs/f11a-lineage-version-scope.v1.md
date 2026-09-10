# F11a lineage version clarification — root scope

Adopt the diagnosis a0462aa11cfb77a0dc0fc52f07d113ef9e6deeaae249664e176b8d9e29a1a490: current Search version is exactly 0.7.0; preserved historical native structural tuples do not authorize historical Search 0.6.0 as current. No product gate change is authorized or made. This is a test-fixture interpretation correction, not a newly demonstrated product defect or reset of the F11a repair budget.

Add only f11a-lineage-version-gold.v1.py and f11a-lineage-version-run.v1.py. Execute the original complete positive fan-in method using a test-helper-only literal 0.7.0 fixture and coherent ID, plus an untouched literal 0.6.0 exact-error control. Do not patch SUT constants, add skips, weaken original assertions, or alter tests/test_semantic_lineage_relations.py. Preserve collateral-lineage-001 as 2 PASS + 1 ERROR; never relabel its ERROR as an expected result retroactively.

Root reviewed the new source and existing F04 guard before execution. Fixed two-method selection, owned temporary fixtures only, no models/network/GUI/process dispatch within worker, existing explicit-write budget, 30 seconds and 1 MiB log. These Python guards are not an OS sandbox or RSS limit. New run f11a-lineage-version-001; any unexpected failure stops this scope for diagnosis. Reversal is to stop using these separately named additions, leaving original tests/product/history intact.

This does not repair or certify the entire existing lineage suite, whose other methods share the historical helper. It also does not close F11b or whole-V1 acceptance. Independent packet/audit remain required.
