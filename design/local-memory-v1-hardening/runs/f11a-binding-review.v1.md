# F11a binding investigation: separate-context static review

2026-09-09 08:18 JST. Root records the read-only feedback from `/root/f18_independent_audit`. This is design preflight, not formal product audit PASS. Reviewer launched no tests and edited no files.

Reviewer checked all 15 recorded current source/log hashes and the two methods (three validator paths each). The central observation is supported: current source hash checks do not validate arbitrary candidate native Notebook metadata; no claim was made that the candidate field reaches answers. Root also repeated the 15-hash check successfully.

Correction to the descriptive size in the earlier proposal/preflight: the fixed `f11-next-fixture.v1.ipynb` is **1069 bytes, about 1.04 KiB**, not 1.4 KiB. Root verified with `wc -c`. Its SHA-256 and the enforced 16 KiB fixture cap are unchanged. Keep original observation records.

## Required decisions before a new implementation contract

1. **Do not assume existing Notebook byte/depth limits.** In-memory intermediate validation uses unbounded source `read_bytes`; streaming validation checks source size/hash; Probe uses ordinary `json.loads`. Declare actual byte/depth/JSON-type/duplicate-key constraints for the new same-snapshot parser and fail/partial behavior. Exceeding a bound or malformed JSON must not become state-attested success. Notebook full-format support remains outside this slice.
2. **Do not trust a record's declared extractor version to decide whether state is required.** Native metadata/provenance are not in Evidence content identity. Test removal of state with a downgrade to an old producer label, and a copied valid state from another cell/output. Require agreement with the expected generation identity. Define whether validation also proves text belongs to that cell/output; if it does not, do not describe the whole record as source-attested.
3. **New helper modules would add packaging/identity obligations.** `build_intermediate_records.py` and bootstrap maintain explicit processing-code lists; the protected build script uses explicit copies. Reusing a helper inside an existing packaged file avoids that new distribution-file change, but does not waive the needed source/hash/strict-schema/consumer checks. Do not silently edit the protected build script.

Count semantics, missing/null/boolean distinctions, no-source-root return compatibility and separate managed/Probe identities from the root preflight remain necessary. Nothing in this review implements a Reader guarantee. F11 stays open and needs its own RED → minimum fix → independent formal audit → regression loop.
