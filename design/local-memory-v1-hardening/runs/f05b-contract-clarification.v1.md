# F05b preimplementation shape clarification

Root, 2026-09-09 11:21 JST. Addendum to frozen contract e136a468b3ae2a16f094b3189788f7e86a4c5bd089458a27f7d963193af8fd1b; no product edits yet.

For internal attest mode explicit_decisions, successful authority has exactly mode, path, sha256, byte_count: mode explicit_decisions; path the explicit caller path string; sha256 raw digest for present file or null only for genuine absence; byte_count raw byte length or zero for genuine absence. Public validate still returns status/errors only; app consumers cannot select explicit_decisions. Snapshot/no_decisions shapes unchanged.

Default-None Python parameters cannot distinguish explicit None from omission; combination validation follows their values. Strict rejection of explicit null keys applies to serialized lineage/security contexts where key presence is observable. Neither grants a versioned app an omitted mode.

Resolver absolute snapshot read bound uses module constant MAX_DECISION_SNAPSHOT_BYTES = 67_108_864, which pure synthetic capacity tests may patch to a smaller positive integer. App CONFIG validates its separate 1..67_108_864 range. This test seam is not an additional public API/configuration route.
