# Root app gold correction before first execution

Independent static reviewer found the initial assertion expecting one failed generation to remain contradicted existing bootstrap finally cleanup. Preserve initial root test as f11a-root-app-gold.v1.py (e8887c8e...). No test has run with either form; this is a mistaken test oracle, not a product failure or formal audit repair.

Correction: observe absence of validation PASS receipt and Search output at the actual failing run_tool boundary before cleanup; after build_index unwinds, require no new generation and unchanged old generation/config. Do not change or weaken production cleanup. All other test assertions and production source ownership remain unchanged. Root runner still selects exactly four methods. Freeze corrected test before its first execution and retain both versions.
