# Release note: 2026-08-29 pre-deploy scan closeout

The 2026-08-23 leaf-platform pre-deploy scan's open findings all merged today:

- **D1** (#815): fastapi 0.109.1 -> 0.141.1, starlette 0.35.1 -> 1.6.0, httpx -> 0.28.1.
  Removes 7 known starlette CVEs (3 HIGH) from the production app and broker images,
  confirmed absent in the post-merge cve-harvest of merge `395756b4`. The bump also
  surfaced and fixed a silently-blinded O2/O3 route walk (fastapi 0.110's
  `_IncludedRouter` reshape); all enumeration tests now walk via
  `server/tests/route_flatten.py` with non-vacuity floors.
- **D3 phase 2** (#816 + #817): the cve-harvest job now BLOCKS on HIGH/CRITICAL with an
  expiring allowlist (`.github/cve-allowlist/platform-images.trivyignore`, machine-checked
  by `.github/scripts/validate_cve_allowlist.py`).
- **D5** (#814): HEALTHCHECKs for `Dockerfile.instant-execution` and `e2b.Dockerfile`.
- **IAM follow-up** (leaf-automation-aws-terraform#1325): pull-only ECR role for the
  scanner; the workflow repoint follows its production apply.

This commit is docs-only on purpose: its push build no-ops, so a dispatched
`force_rebuild_all` build of this exact commit produces a clean, non-adopted image set
whose baked `LEAF_SOURCE_SHA` is an on-main commit — the identity shape the staging
authored-CAD acceptance requires before the production handoff. (Adopted/reused images
bake preview revisions, which the acceptance's on-main ancestry check correctly refuses;
see docs/SPECULATIVE-PR-BUILDS.md "Identity and the supply set".)

## Anchor moved forward (2026-08-30)

The promote candidate advanced past the original anchor: #818 ("re-pin three stale
operator gate pins to the live contract") merged behind it and its relay moved the
staging app service forward, making the original anchor a guarded non-forward move.
This commit is the second docs-only anchor, cut from the head that contains #818, for
the same force_rebuild_all identity purpose described above.
