# F05b executor Phase A freeze and conditional Phase B release

2026-09-09. This is preimplementation evidence, not test PASS or audit acceptance.

Original 26-method gold v1 remains immutable. Additive v2 adds only `test_snapshot_symlink_directory_and_dangling_link_fail_read_only`: each nonregular supplied snapshot must yield FAIL, preserve target lstat and original bytes. New test file and exact snapshot SHA-256: `306a8930892750f05c68c6c82880fcdd67789c5fd36a1a41ae61ae4a111411ab`. Total 27 methods; all original method names/oracles are retained. Unit tests have not yet run; absence of attest API is not semantic RED. Root's independent six-method genuine assertion RED is in f05b-root-red-assessment.v1.md.

All five immutable f05b-executor-before-{resolver,reader,validator,projector,bootstrap}.v1.py snapshots match the task-contract before hashes exactly. Before bytes are preserved for later isolated forward/reverse deltas. No product or existing test has been edited for F05b at this freeze. Parent's conditional Phase B release now applies. Executor owns exactly the five contract files and listed compatibility test deltas; root-owned app gold is untouched. Phase B is implementation/verification only, never self-approval.

Runner f05b-executor-run.v1.py is bounded to 30 seconds and 1 MiB log, inherits reviewed synthetic fixture guards, and retains exclusive run directories. Test fixtures remain <=8 KiB decisions, <=64 KiB graph, <=16 records / 16 KiB inventory and <=1 MiB cumulative explicitly written bytes. No production/shared user data, network, real models, GUI, commit, push, broad suite, or further agents.
