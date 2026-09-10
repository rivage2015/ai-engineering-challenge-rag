# Additive post-API gold: literal late substitution

Original v1 26 methods and v2 27 methods stay immutable. This v3 adds exactly one method before its first run, with no product change: test_late_input_substitution_cannot_replace_verified_snapshot_payload. Total 28 methods. An explicitly injected test seam replaces all three synthetic input files only after the attestation has read them, while the reconstruction still receives its original parsed inputs. Fixed oracle: PASS payload must equal the pre-substitution verified payload exactly, including the original raw inventory hash and Guide.csv Human selection; all on-disk input bytes must now differ. Product must not repair the synthetic replacements. The existing single-open test independently forbids a second input open.

This is a post-API control, not an original semantic RED or an audit result. The full v3 test snapshot is f05b-executor-gold-test.v3.py; prior gold and every previous failure log remain unchanged.
