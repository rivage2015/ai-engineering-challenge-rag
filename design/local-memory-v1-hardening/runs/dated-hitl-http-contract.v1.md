# Dated Human Review HTTP Contract v1

Scope: the localhost HTTP handler only, using synthetic metadata and temporary decision files. No real user document, production index, Ollama inference, packaged app, commit, or push is in scope.

Acceptance checks:

1. A same-work, current-confirmed, use-approved submission carrying a valid CSRF token and one-time review ticket is re-attested, persisted through the resolver CAS API, and starts exactly one index rebuild request.
2. Reusing the consumed ticket returns HTTP 409, does not start another rebuild, and does not change the saved decision bytes.
3. A wrong CSRF token returns HTTP 403 before any decision is stored or rebuild requested.
4. `/ask` returns HTTP 409 without invoking answer generation when the live decision revision does not match the active generation.
5. If the decision revision changes while an answer is being generated, `/ask` returns HTTP 409 and does not disclose the generated answer text. The check binds the starting generation, generation path, decision snapshot, and complete CONFIG identity; two independently `current` results are insufficient when those identities differ.

Limits: a PASS establishes handler routing and fail-closed behavior for these bounded synthetic cases. It does not certify browser rendering, real documents, model quality, app packaging, or all concurrent/fault conditions.

Composition note: the HTTP positive control must prove that submission requests `validate_source=True`. The bootstrap review context also compares the generation review artifacts' hashes before and after its validator/reconstruction reads and fails closed on drift. Existing Path Graph validator tests remain the evidence for individual live-source mutation classes; the HTTP test does not replace that suite.
