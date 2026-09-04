# Tree-bound gate proof: when a main-push build may skip the 8-shard gate

Operator decision D3 (2026-08-05): the test gate's green verdict binds to the
git TREE hash (`git rev-parse HEAD^{tree}`), not the commit SHA. Shipped in
PR #446; precedent leaf_website #200 (promotion proof bound to HEAD).

## Why

A pull_request gate runs on the PR's merge preview. The merge that lands on
main mints a NEW commit whose tree is byte-identical whenever main did not
move in between, so re-running the full 8-shard gate (~190s of shard wall) on
the push-to-main build proves nothing the PR run did not already prove.
Binding the verdict to the tree lets the build recognize "these exact bytes
already passed" while ANY skew — main moved under the PR, a hand-edited
commit, a direct push like the one that added this file — changes the tree
and runs the full gate.

## Mechanism

1. **Mint.** The fan-in (`gate` / `run-all-gates` in `test-gate.yml`), after
   `--verify-shard-results` PROVES the shard set AND the shard matrix job
   succeeded, emits a `leaf-gate-proof` document (tree, head SHA, catalog
   fingerprint) via `run-all-gates.py --emit-proof` and uploads it as the
   `gate-proof-<tree>` artifact (30-day retention). Emission REFUSES on a
   dirty or non-git checkout rather than fabricate; a refusal costs the next
   identical-tree build its skip, never the verdict.
2. **Probe.** When `build-platform-images.yml` calls the gate with an exact
   ref, the `gate-reuse-probe` job searches `gate-proof-<tree>` artifacts and
   accepts a candidate only when the artifact's own `workflow_run` metadata
   says it was minted by a run of THIS repository (fork uploads rejected)
   from one of the two gate workflows, and the content verifies via
   `run-all-gates.py --verify-gate-proof` (schema, exact tree, catalog
   fingerprint recomputed from the probing checkout). Every failure degrades
   to `reuse=false`; the probe cannot redden a build.
3. **Skip.** On a verified hit the shard matrix is skipped and the fan-in
   re-downloads and re-verifies the SAME proof against its own checkout — the
   verdict never rests on a string that crossed jobs. On any miss the full
   gate runs and mints this tree's proof.
4. **Pre-push binding.** The build job refuses to push any image unless the
   gate's exported `proven_tree` equals `git rev-parse HEAD^{tree}` of its
   own checkout. `build: needs: [prepare, test]` remains the only pre-ECR
   gate (no branch protection on this plan); the skip path is fail-closed at
   every seam listed above.

## Registry-outage transitivity (schema 2, 2026-09-04)

`harness-audit-high` (the npm-audit suite) can report `UNAVAILABLE` instead of
PASS/FAIL when npmjs.org's advisory endpoint is down (see `run_npm_audit_suite`
in `run-all-gates.py`). This changes what a proof means:

* A proof's `audit_suites` block records every `kind="npm-audit"` suite's
  status, and only a PASS entry also carries the CURRENT git blob sha of that
  suite's `package-lock.json` (`lockfile_blob_sha`) — never an UNAVAILABLE
  entry, so an outage-time proof can never itself become a future ancestor.
* **Probe and re-verify both refuse an UNAVAILABLE proof.** `--verify-gate-
  proof` (called by both the probe job above and the fan-in's own re-verify
  step) refuses any proof whose `audit_suites` carries an UNAVAILABLE status —
  no workflow change was needed for this, since both already call the same
  function.
* **The fan-in itself never prints the bare PROVEN line while any suite is
  UNAVAILABLE.** It prints `NOT PROVEN BY AUDIT: <suite> unavailable (...)`
  instead, and exits 0 ONLY when the CURRENT lockfile blob sha equals the one
  recorded in the newest proof under `--prior-proofs-dir` whose own
  `audit_suites` entry for that suite is PASS — "the exact same bytes already
  passed audit on some earlier tree." No prior-proofs directory, no matching
  entry, or a differing blob sha all FAIL the fan-in with the same distinct
  line. The "Download recent gate-proof artifacts" step in `test-gate.yml`
  populates that directory (bounded, same-repo/gate-workflow provenance
  allowlist as the probe job, best-effort — any failure there just leaves the
  directory empty, which fails closed, never redly).

This keeps the merge honest by TRANSITIVITY (same lockfile bytes, previously
proven clean), never by assuming an outage is harmless.

## Trust boundary

The proof file's self-reported `source` block is informational only.
Provenance always comes from the GitHub artifact listing (`head_repository_id`,
minting run `path`), because a fork's pull_request run can upload an artifact
with any name and content. Same-repo actors with push access are outside the
threat model: with no branch protection they can already edit the gate itself.

## Receipts (first live cycle, 2026-08-05)

* Mint: PR #446 gate run 30977874583 minted `gate-proof-c4951361…`.
* Skip: merge b2e63b3 build run 30978164812 — probe VERIFIED the proof,
  shards skipped, fan-in re-verified, gate leg 28s vs ~220s, build job logged
  `gate verdict bound to tree c4951361… == build tree`, all five images
  pushed and verified.
* Skew: the direct push that added this file changed the tree with no PR
  gate, so its build ran the full 8-shard gate (see that run's
  `gate-reuse-probe` log: "no verified gate proof binds tree …").
* Literal skew: a PR whose gate ran against an older main and merged after
  main advanced takes the full gate the same way — its merge tree no longer
  matches the gated preview tree, so no proof binds it.
