# Speculative PR image builds: when a merge adopts instead of rebuilding

Lane L5 of the build-speed program (operator decision D3, 2026-08-05): image
identity binds to the git TREE hash, the same principle PR #446 applied to
the test gate's verdict (see `GATE-TREE-REUSE.md`). Where #446 lets the merge
run skip re-PROVING bytes a PR already proved, this lane lets it skip
re-BUILDING bytes a PR already built.

## Why

The post-gate build leg of a push-to-main run costs ~100-140s of wall even
fully warmed (the harness image sets the wall). A clean squash-merge lands a
commit whose tree is byte-identical to the PR's merge preview, so those
images were already buildable minutes earlier, while the PR sat in review.
Building them speculatively moves the build off the merge-to-live critical
path; the merge run only has to verify identity and alias tags — seconds,
not minutes.

## Mechanism

1. **Dispatch.** Every update of a same-repository PR targeting main runs
   `speculate-platform-images.yml` (pull_request trigger, deliberately
   secretless). It dispatches `build-platform-images.yml` on the MAIN ref
   with `speculative=true`, the PR head as `source_sha`, and the PR number.
   Two load-bearing consequences of the indirection: the build runs MAIN's
   workflow text (a PR cannot edit the build that holds the push role and
   the solver deploy key), and it authenticates under the main-ref OIDC
   subject — the ECR push role's trust policy admits exactly that one
   subject, so a pull_request-event build could never assume it. Dispatch
   failures soft-fail: speculation is an optimization.
2. **Build the preview.** `prepare` validates the dispatched head against
   the open PR via the pulls API (same-repo, non-fork, exact head), fetches
   `refs/pull/<n>/merge`, proves the preview still merges that head, and
   checks it out. The five-image matrix (`speculate`) builds that tree with
   inputs byte-identical to the gated build's and pushes each image as
   `spec-<tree>-<preview12>` — a namespace no deploy workflow accepts (the
   staging deploy validates tags against `sha-*`/`prod-*` only), so
   untested content stays unreachable however long it sits in ECR. The tag
   carries the preview commit alongside the tree because two previews can
   realize the SAME tree while baking different `LEAF_SOURCE_SHA` values: a
   tree-only tag would let a later run mint provenance for images an
   earlier run built, and a crashed run could leave a mixed-bake set. So
   only a rerun of the SAME preview skips by existence; a new preview of an
   identical tree rebuilds under its own tag. Cache is import-only (the
   merged-onto main tip's `buildcache-*`); nothing is exported.
3. **Mint the invariant.** `speculate-manifest` re-reads all five digests
   from the registry and uploads a `spec-supply-set-<tree>` artifact ONLY
   when every one is present. This is the partial-push invariant redefined
   over tree identity: no complete artifact, no adoption, ever.
4. **Adopt at merge.** The push run's `adopt` job (between the gate and the
   build matrix) requires three trees to agree: its own checkout's, the
   gate's `proven_tree`, and the speculative manifest's. Artifact provenance
   follows the gate-reuse probe's discipline — same-repo origin from the
   artifact's `workflow_run` metadata, this workflow's bare path, a main-ref
   `workflow_dispatch` run; the file's self-reported content is never
   trusted for provenance. Content then verifies (`verify-speculative`
   schema + exact tree) and every digest must equal what the manifest's own
   immutable spec tag carries live. Only then does adopt alias
   `prod-<shortsha>` onto those digests (`ecr put-image` with three
   attempts per image, idempotent: an existing release tag must already
   equal the speculative digest, anything else stands down), re-verifies
   all five, and declares `adopted=true`. The build matrix skips, and
   since the 2026-08-05 tail-compression fold the SAME `adopt` job then
   stamps and uploads the supply set in place (byte-identical steps to
   the `verify` job's, pinned by the contract test) instead of handing
   off to a fresh `verify` runner; `verify` still owns the full-build
   path. A failure in that half is post-commit by construction, so it
   reddens the run and a rerun resumes idempotently through adopt.
5. **Degrade before the first write, fail closed after it.** Up to the
   moment the first release tag is written, every anomaly in adopt — no
   artifact, foreign tree, missing digest, even an unexpected script death
   (an EXIT trap catches those) — lands on `adopted=false` and the full
   build runs exactly as before. Once a `prod-*` tag carries a speculative
   digest, adoption is COMMITTED: a fallback rebuild would only collide
   with the immutable tag, so a persistent failure past that point fails
   the run with a rerun-to-resume instruction (the alias loop accepts
   equal digests, so a rerun finishes the job). An ATTEMPTED write with
   unconfirmed state counts as committed too — degrading is safe only when
   a successful probe positively proves the tag absent, because ECR may
   apply a put whose response was lost. The build matrix refuses to run
   after a FAILED adopt for the same reason. The fail-closed
   direction of identity is unchanged: "no deployable tag without the
   gate's proven tree".

## Identity and the supply set

Adopted images bake the merge PREVIEW commit's `LEAF_SOURCE_SHA` (the tree
is identical; the commit envelope differs). That is the D3 trade, stated
plainly: live health endpoints report the preview commit until the next
non-adopted deploy. The deploy path only verifies live `source_sha` in
`mode=build` deploys, which the relay never uses, so nothing red arises. The
authoritative record is the supply-set manifest: adopted runs stamp
`leaf.staging-supply-set.v2` — the v1 deployable fields unchanged (the relay
reads `build_tag`/`source_revision` identically), plus `source_tree` and the
speculative run's provenance. v1 stays accepted everywhere (relay,
production handoff): a rerun of a pre-v2 build run regenerates its artifact
from the old workflow text and must keep dispatching.

The deterministic web `dist/` artifact is unaffected: whichever supply-set
writer runs (`adopt` on the adopted path, `verify` on the full-build path)
rebuilds and hashes it from the merge checkout, so the production web path
carries the merge commit's identity on both build paths. That rebuild is
deliberately NOT replaced by the speculative run's output: `dist/` bakes
the merge commit's `LEAF_SOURCE_SHA` (at minimum `health.json`), which the
preview run cannot know, so no earlier run can produce the byte-identical
artifact the manifest's `web_artifact_sha256` attests — the fresh
`npm ci` + Vite build is what makes the hash provable, and the production
handoff audit chain consumes exactly that hash.

## Retention and cost

`spec-<tree>` tags carry the same bounded-retention infrastructure contract
as `buildcache-*` (expire after 14 days or retain a bounded latest-N per
repository); the terraform-side lifecycle rule is the follow-up that
enforces it. Speculative artifacts expire in 30 days. Cost per PR update is
five image builds on warmed cache; within one PR, newer dispatches cancel
older ones (per-PR concurrency group, separate from the release group so a
speculative run can never queue ahead of a real merge build).

## Threat model

Same-repo actors with push access are outside the threat model, as
`GATE-TREE-REUSE.md` records: with no branch protection they can already
edit the gate and this workflow on main. What this design deliberately
preserves: PR-authored workflow text never runs with a credential (the
dispatcher is secretless, pinned by the contract test), fork PRs are
excluded at the dispatcher AND re-validated against the pulls API in
`prepare`, and adoption trusts only artifact metadata plus the live
registry, never file contents.
