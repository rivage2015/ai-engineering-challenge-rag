# F11a source-binding preflight v1

2026-09-09 08:11 JST. Investigation only; no implementation or product acceptance.

The previous proposed Notebook state representation is not yet a supported product field. This preflight checks why simply copying native metadata would not make it source-attested. Preserve the existing F11-next proposal, fixture and observations unchanged.

## Fixed observations before running

- On the fixed 1.4 KiB synthetic Notebook, both intermediate validators should accept current output without saved execution facts (known missing contract, not a fix).
- The same validators currently allow arbitrary native properties. A deliberately false candidate notebook_state object can therefore be attached without changing Evidence ID/content hash, even with source_root supplied. Test both schema-enabled entry points and the streaming published_schema=False path. A green observation means the missing binding was reproduced, not that the false metadata is safe or currently consumed by answers.
- Check source bytes, original Evidence IDs and content hashes are unchanged. Do not run a notebook, model, GUI, network request, installed Office reader or a managed production build.

## Execution limits and ownership

Root owns only new f11a-binding-* investigation files, not product files. Reuse the reviewed f04a-executor-run.v1.py guard and its synthetic F01 dispatcher. One supervisor owns one Python worker with 30-second wall and 1 MiB log limits. Worker writes only inside its owned temporary fixture root; the only source is the fixed f11-next-fixture.v1.ipynb, copied after a 16 KiB check. No existing result is overwritten. Use the local 3.9 venv because it already supplies jsonschema. This is not an OS sandbox or a total RSS/disk guarantee.

## Plan corrections established by code inspection

- validate_intermediate_records.validate currently reads source bytes to hash them but does not parse the Notebook to compare per-cell facts. Streaming validates source size/hash separately, also without per-cell reconstruction. Reuse the exact hashed bytes in the new source-binding helper; a separate unbound reread would introduce a false attestation boundary.
- Both validators currently return integer count dictionaries. Any no-source-root diagnostic must be an explicit backwards-compatible contract or a separate report, not an unannounced change in return type consumed by builders/tests.
- Probe 0.7.1 and managed extractor 0.11.0 are separate identities. A managed bump also affects the pinned NATIVE_STRUCTURAL_PRODUCERS whitelist, not merely SearchUnit builder pins. Old generations must not silently acquire a new state guarantee.
- The proposed nonnegative count restriction is still a project proposal; no installed nbformat schema was found in the two inspected Python environments. Do not call it verified official nbformat validation. Resolve the exact supported JSON shape before implementation, including boolean versus integer and missing values.
- Complete source binding must check role/type/locator and metadata against the same source snapshot. Content hash excludes native_properties and is not proof of metadata fidelity.

No product rollback needed. Keep observations even if they fail; implementation requires a new immutable contract with before hashes, exact expected objects, both validator paths and downstream compatibility tests. F11 whole remains open.
