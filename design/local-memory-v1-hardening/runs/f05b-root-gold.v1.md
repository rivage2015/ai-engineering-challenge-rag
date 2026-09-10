# F05b root app RED gold, frozen before first run

2026-09-09 11:21 JST. Contract `e136a468b3ae2a16f094b3189788f7e86a4c5bd089458a27f7d963193af8fd1b`; product5before hashes rechecked and match contract. Test `tests/test_decision_snapshot_e2e.py` SHA-256 `14ad1a5a53f9e5251f0947e20e33980eb5d62ec284a5283cef17d2d22d2ab27c`; runner `f05b-root-run.v1.py` SHA-256 `7dae3a260d07b88e344c367a28889796cfd5afaebdf417449432b7c9c0f241ae`.

Frozen methods and desired outcomes:

1. test_new_app_generation_materializes_empty_decision_snapshot: fixed generation-local file exists, exact empty bytes, shared file still absent.
2. test_reader_rejects_omitted_authority_before_source_entry: ValueError/version_decision_authority_required; source-entry sentinel never reached.
3. test_validator_rejects_omitted_authority: ValueError/version_decision_authority_required on otherwise valid current generation.
4. test_reader_rejects_resealed_active_after_initial_gate: genuine initial resolver PASS then coherent mixed-group false-active forgery; Reader rejects before source binding, previous published CONFIG/index and source bytes unchanged.
5. test_validator_rejects_coherent_eligible_contact_omission: producer-only selector omits Contact coherently, selector restored before real Validator; reject and keep previous publication. A missing keyword/TypeError is not RED.
6. test_projector_rejects_omitted_authority_before_index_changes: otherwise legitimate versioned input with graph but no authority rejects before embedding/index modification; prior sentinel remains.

Initial current-source failures must be assertion-level unsafe acceptance/missing snapshot, not unrelated environment/API errors. Gold is not yet a result. Keep failed run logs; corrections must be additive records with reasons and new hashes, never conceal the first attempt. Source fixtures are tiny synthetic CSV/text, no real models/network; runner reuses fully reviewed F04a guard and 30-second/1MiB supervisor. Root-generated attack graph explicitly checked <=64KiB. This six-method seed is not all required post-API acceptance coverage.
