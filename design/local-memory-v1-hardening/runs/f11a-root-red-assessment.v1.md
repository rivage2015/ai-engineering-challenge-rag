# F11a root semantic RED assessment

Executed at 2026-09-09 13:34:39–40 JST, before any F11a product edits. `f11a-root-red-001/result.json`: four methods, ten assertion failures, zero errors/skips/expected failures; 0.410952 seconds supervisor elapsed. Log6756 bytes SHAee851105417f221f4cc3966bdfb3ea7e611b6734c2e6655bdfb08d7b3f6cadc1. Root read the full log.

Nine failures are `ValueError not raised`: missing state, well-typed false output count 5 instead of original6, and counts-only validation without caller originals, each through native/schema-stream/structural-stream. Each method first validated the fixed independent literal good metadata successfully in all three modes. Source and content/ID preservation assertions passed. The tenth failure is actual direct Search derivation returning six missing state objects instead of six literal states.

No proposed API/import/TypeError or malformed fixture accounts for these failures. These are semantic REDs against the new frozen contract, unlike the previous green gap observations. Four methods/ten assertions are not ten independent vulnerabilities. Product not fixed or audited yet.

Gold SHA53c693f4280569f6801c91f1679ce3bf7d93d59fba57c59f440040067cc8214e and runner SHA22bfdccf9bc20beed35f0ffffcca6e7bc891f8be29cf167c9063865246ca8dd3 remain immutable. Before product ownership is activated, finish executor gold/runner preparation, read it and freeze its hashes; verify all ten before snapshots match current sources. Independent contract gate is pending. No original/public index/commit/push changes.
