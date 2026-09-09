# Restore useful staging surface receipt verification

Parent: Leaf Studio integration of the parked combined-app release branch.

- Scope: new scripts/platform_surface_receipt.py, its tests, the existing staging relay invocation and the relay fixture.
- Preserve the current supply and consumer contract envelopes. Do not modify Terraform or shared canonical hashes.
- Restore the parked validators with the actual producer's primary/alt colors, numeric identities, priority arrays and jq compact-sorted hash including LF.
- Verify positive deployed/skipped receipt formats, mismatched hashes, missing/extra files, routed drift, CLI execution and relay wiring. Baseline manifest suite: 89 passed.
- No cloud calls, publication or paid CI in this local slice. Rollback before publication is to omit this isolated branch. After integration, revert only this change through the normal release process.

The relay invokes the helper after downloading the exact Terraform result artifact. jq must be on PATH, as it already is for the relay. The helper validates both files and exits nonzero on receipt mismatch. This is local source recovery, not deployed acceptance.
