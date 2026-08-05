#!/usr/bin/env python3
"""Static regression checks for the production image build workflow."""

from pathlib import Path
import re


WORKFLOW = (
    Path(__file__).resolve().parents[1]
    / ".github"
    / "workflows"
    / "build-platform-images.yml"
)
ROOT = WORKFLOW.parents[2]
DEPLOY_DOC = (ROOT / "deploy" / "README.md").read_text(encoding="utf-8")


def main() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")

    # One tag is derived once and passed to every image build and later job.
    assert 'image_tag="prod-$current_short"' in text
    assert 'image_tag="sha-$current_sha"' in text
    assert 'echo "value=$image_tag"' in text
    assert "IMAGE_TAG: ${{ needs.prepare.outputs.tag }}" in text
    assert "TAG: ${{ needs.prepare.outputs.tag }}" in text
    assert (
        "tags: ${{ env.ECR_REGISTRY }}/${{ env.IMAGE_NAME }}:${{ env.IMAGE_TAG }}"
    ) in text

    # A trusted main workflow may build an exact reviewed source without
    # granting draft branches their own AWS OIDC subject.
    assert "source_sha:" in text
    assert "source_sha: ${{ steps.source.outputs.sha }}" in text
    assert "source_mode: ${{ steps.source.outputs.mode }}" in text
    assert "ref: ${{ inputs.source_sha || github.sha }}" in text
    assert "ref: ${{ needs.prepare.outputs.source_sha }}" in text
    assert "LEAF_SOURCE_SHA=${{ needs.prepare.outputs.source_sha }}" in text
    assert "AUTOFILL_SOLVER_REVISION={0}" in text
    assert "autofill_solver=./autofill-solver" in text
    assert "repository: LEAF-Solar-Design/autofill-solver" in text
    assert "AUTOFILL_SOLVER_DEPLOY_KEY is required" in text
    assert "ssh-key: ${{ secrets.AUTOFILL_SOLVER_DEPLOY_KEY }}" in text
    assert "pull-requests: read" in text
    assert "source_sha must be a full 40-character lowercase hexadecimal commit" in text
    source_start = text.index("      - name: Require exact source to be reviewed")
    solver_start = text.index("      - name: Resolve canonical solver provenance")
    source_body = text[source_start:solver_start]
    assert '--arg repo "$GITHUB_REPOSITORY"' in source_body
    # TWO PR-validating arms live in this step now: the draft-PR dispatch
    # mode and the speculative mode (which validates the exact open PR by
    # number). Both must pin the same-repository, non-fork, exact-head jq
    # conditions; only the draft arm requires .draft.
    for required_check in (
        '.state == "open"',
        '.base.ref == "main"',
        ".head.repo.full_name == $repo",
        ".head.repo.fork == false",
        "(.head.sha | ascii_downcase) == $sha",
    ):
        assert source_body.count(required_check) == 2, required_check
    assert source_body.count(".draft == true") == 1
    assert "exact head of an open same-repository draft PR" in source_body
    # The speculative arm builds the merge preview only after proving it
    # still merges the dispatched head, and can never promote.
    assert "A speculative run can never promote." in source_body
    assert (
        'fetch --no-tags --depth=2 origin "refs/pull/$SPECULATIVE_PR_NUMBER/merge"'
        in source_body
    )
    # The preview fetch carries a scoped auth header (the checkout persisted
    # no credentials because later prepare steps execute preview-authored
    # scripts); nothing may flip the checkout itself to persisted
    # credentials.
    assert "http.https://github.com/.extraheader" in source_body
    prepare_block = text.split("\n  prepare:\n", 1)[1].split("\n  test:\n", 1)[0]
    assert "persist-credentials: true" not in prepare_block
    assert '"$PREVIEW_SHA^2"' in source_body
    assert 'git checkout --quiet "$PREVIEW_SHA"' in source_body
    assert "Only a source commit on main may request production promotion" in text
    assert "needs.prepare.outputs.source_mode == 'main'" in text
    # SIX jobs hold the ECR push credential: warm, build, verify, and the
    # speculative lane's speculate (pushes spec-<tree> images),
    # speculate-manifest (reads live digests to mint the tree-bound
    # manifest), and adopt (aliases the release tag onto verified digests).
    # Each provably needs registry access. Note what that means and what
    # this count does NOT buy: six jobs can reach ECR with push rights, and
    # no assertion in this file can prevent them from using them (see the
    # limitation block below). Raising this number should mean a new job
    # that provably needs registry access, not a convenience.
    # Counted by parsed key and value, not by exact text: `id-token : write`
    # or a quoted key grants OIDC while leaving a literal count short,
    # and a comment mentioning the grant inflates it (sol-critic round 8,
    # findings 3 and 6). The structural line list is built below, so this
    # count is taken once it exists.
    oidc_grants = None  # set immediately after the lexical gate

    # ------------------------------------------------------------------ #
    # Lexical gate. This contract is enforced by TEXT extraction (stdlib
    # only, no YAML parser), so a construct that changes what YAML PARSES
    # while leaving the asserted literals intact would silently void every
    # assertion below it. Rounds 3 through 6 of sol-critic on PR #445 each
    # found another one (bare-dash step markers, anchors, exotic anchor
    # names, tag-fronted anchors, adjacent-value aliases, and finally an
    # anchor on the line AFTER its key), so the position-by-position bans
    # they produced are replaced here by one rule that does not enumerate:
    #
    #   * every line is classified as structural YAML or block-scalar
    #     content. Inside a block scalar (run: |, if: >-) YAML parses
    #     nothing, so shell operators and expression text are free;
    #   * on a structural line, expression spans (${{ ... }}) and quoted
    #     scalars are masked out, since both are opaque text to YAML;
    #   * what remains is pure structure, and there the anchor, alias, tag,
    #     and merge characters may not appear AT ALL. No position analysis,
    #     so no position left to hide in.
    #
    # Document markers are banned for the same reason (a second document
    # could carry its own copy of the asserted literals), and every step
    # item must use the canonical "- " marker so the step splitter used
    # throughout this file provably sees every step.
    # ------------------------------------------------------------------ #
    def _mask_opaque(line: str) -> str:
        # ${{ ... }} expressions and quoted scalars are text to YAML: an
        # anchor inside one is not a node. Masking them keeps GitHub
        # expression operators (&&, ||, !) and quoted content from reading
        # as YAML syntax, while an anchor placed OUTSIDE them stays visible.
        # The double-quote pattern honours backslash escapes so an escaped
        # quote inside a legitimate scalar cannot end the mask early
        # (sol-critic round 7 false positive). Masking is LENGTH-PRESERVING
        # so offsets still index the original line.
        def fill(match: "re.Match[str]") -> str:
            return "x" * (match.end() - match.start())

        masked = re.sub(r"\$\{\{.*?\}\}", fill, line)
        masked = re.sub(r"'(?:[^'])*'", fill, masked)
        masked = re.sub(r'"(?:\\.|[^"\\])*"', fill, masked)
        return masked

    def _comment_cut(line: str) -> str:
        # The line with any trailing comment removed, quotes intact. A `#`
        # inside a quoted scalar or an expression is not a comment.
        cut = _mask_opaque(line).find("#")
        return line if cut < 0 else line[:cut]

    def _uncommented(line: str) -> str:
        # Comment-free AND masked: for scans that must not see YAML
        # indicator characters that are really scalar text.
        return _mask_opaque(_comment_cut(line))

    # A block scalar header may carry an explicit indentation indicator and
    # a chomping indicator in EITHER order (|2-, >-2, |+, ...), and a
    # trailing comment. Content is always indented deeper than the header
    # line, whatever the indicator says, so the indicator does not change
    # the scan below — but an unrecognised header used to leave shell text
    # classified as structure and redden the run (round-7 false positive).
    # The header is matched on the COMMENT-STRIPPED line, so a `# run: |`
    # inline comment cannot spoof one (round-7 finding 1).
    block_header = re.compile(r"(?::|^\s*-)\s*[|>](?:[-+][0-9]?|[0-9][-+]?)?\s*$")
    structural = []
    block_scalar_indent = None
    for line in text.splitlines():
        stripped = line.strip()
        indent = len(line) - len(line.lstrip(" "))
        if block_scalar_indent is not None:
            if not stripped or indent > block_scalar_indent:
                continue
            block_scalar_indent = None
        assert stripped != "-", "bare sequence-dash line: %r" % line
        if re.match(r"^      -", line):
            assert re.match(r"^      - \S", line), (
                "non-canonical step marker: %r" % line)
        assert not re.match(r"^\s*(---|\.\.\.)(\s|$)", line), (
            "YAML document markers are banned in this workflow: %r" % line)
        if not stripped.startswith("#"):
            structural.append(line)
        if block_header.search(_uncommented(line)):
            block_scalar_indent = indent

    # &, *, and ! are YAML indicators ONLY at the start of a node. Inside a
    # plain scalar they are ordinary text, so `run: true && echo ok` and
    # `name: Build & verify` are perfectly legal and must not redden the
    # gate (sol-critic round 8 false positives). The scan therefore looks
    # at node-START positions only: after `key:`, after `- `, after a flow
    # delimiter, or the first character of a continuation line.
    node_start = re.compile(r"(?:^\s*|:\s|-\s|[\[{,]\s*)([&*!])")
    for line in structural:
        masked = _uncommented(line)
        found = node_start.search(masked)
        assert not found, (
            "YAML anchor/alias/tag at a node start is banned in this "
            "workflow: %r in %r" % (found.group(1) if found else "", line))
        assert "<<" not in masked, (
            "YAML merge keys are banned in this workflow: %r" % line)
        # Explicit-key syntax (`? key`) is another way to spell a mapping
        # key that no literal key scan would see.
        assert not re.match(r"^\s*\?\s", masked), (
            "explicit-key syntax is banned in this workflow: %r" % line)

    # Keys may be quoted (`"if":` parses as `if`), so every key-level check
    # in this file reads normalised keys rather than raw text: a quoted key
    # otherwise slips past a literal scan while GitHub honours it
    # (sol-critic round 7, findings 2 and 3).
    def _key_of(line: str):
        match = re.match(
            r'^\s*(?:-\s+)?("(?:\\.|[^"\\])*"|\'[^\']*\'|[^\s:#][^:#]*?)\s*:(?:\s|$)',
            _comment_cut(line),
        )
        if not match:
            return None
        key = match.group(1).strip()
        if len(key) >= 2 and key[0] == key[-1] and key[0] in "\"'":
            key = key[1:-1]
        return key

    def _keys_in(block: str):
        return [k for k in (_key_of(l) for l in block.splitlines()) if k]

    def _value_of(line: str):
        # The comment-stripped value text after `key:`, unquoted. Used where
        # the VALUE carries the invariant (a step condition, a permission
        # grant): `if: true # if: matrix.image == ...` presents the real key
        # with a neutered value while a substring scan stays green
        # (sol-critic round 8, finding 2).
        raw = _comment_cut(line)
        match = re.match(r'^\s*(?:-\s+)?(?:"(?:\\.|[^"\\])*"|\'[^\']*\'|[^\s:#][^:#]*?)\s*:\s*(.*)$', raw)
        if not match:
            return None
        value = match.group(1).strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        return value

    # A YAML escape inside a quoted key decodes to a different key than the
    # literal text shows (`"\u0069f"` is `if`), which no text-level
    # normalisation can follow. Keys in this workflow are plain (sol-critic
    # round 8, finding 5).
    for line in structural:
        raw_key = re.match(r'^\s*(?:-\s+)?("(?:\\.|[^"\\])*"|\'[^\']*\')\s*:', line)
        assert not raw_key or "\\" not in raw_key.group(1), (
            "escape sequences in a mapping key are banned: %r" % line)

    # SIX jobs hold the ECR push credential (see the note above the
    # deferred assignment for the roster and for why this is parsed, not
    # text-matched).
    oidc_grants = [
        l for l in structural
        if _key_of(l) == "id-token" and _value_of(l) == "write"
    ]
    assert len(oidc_grants) == 6, oidc_grants

    # The two image-building job blocks. Everything the warm/build contract
    # asserts below is bound INSIDE these slices: per sol-critic round 1 on
    # PR #445, a global positional search accepted the key steps relocated
    # into `prepare` and a five-image list surviving only in a comment.
    warm_block = text.split("\n  warm:\n", 1)[1].split("\n  build:\n", 1)[0]
    build_block = text.split("\n  build:\n", 1)[1].split("\n  verify:\n", 1)[0]

    # The private key is scoped to the canonical-worker lane of BOTH image
    # jobs: `build`, and `warm` since operator decision D1 (2026-08-05) let
    # the deploy key into the warm lane so canonical-worker stops being the
    # only cold post-gate build. In each lane it is used only for a
    # fail-closed presence check and the one exact solver checkout. It is
    # never passed to Docker as an argument or inherited by the whole job.
    secret_ref = "secrets.AUTOFILL_SOLVER_DEPLOY_KEY"
    assert text.count(secret_ref) == 6
    assert text.count("ssh-key: ${{ secrets.AUTOFILL_SOLVER_DEPLOY_KEY }}") == 3
    assert "AUTOFILL_SOLVER_DEPLOY_KEY=" not in text
    # Five mentions per lane (env key + secret ref, presence test, error
    # message, ssh-key) and none anywhere else: a new reference to the key
    # in any other step or comment must consciously bump this count.
    assert text.count("AUTOFILL_SOLVER_DEPLOY_KEY") == 15
    require_name = "      - name: Require the read-only canonical solver deploy key"
    checkout_name = "      - name: Check out the exact canonical solver source"
    # Exactly one presence-check and one solver-checkout step per image job,
    # and none anywhere else in the workflow: with the global total pinned to
    # 2, a key step relocated into any other job cannot go unnoticed.
    assert text.count(require_name) == 3
    assert text.count(checkout_name) == 3
    for lane, block in (("warm", warm_block), ("build", build_block)):
        assert block.count(require_name) == 1, lane
        assert block.count(checkout_name) == 1, lane
        assert block.index(require_name) < block.index(checkout_name), lane
        # Bind to the individual STEP slices, not the span between step
        # names: per sol-critic round 2 on PR #445, a span swallows any
        # intervening step, so the key env or the ssh-key input could move
        # into an inserted unrelated step while the span counts stayed
        # green. Steps are split on the step-list marker at its exact
        # indentation, so each slice below is one step and nothing else.
        steps = re.split(r"\n      - ", block)
        require_steps = [
            s
            for s in steps
            if s.startswith("name: Require the read-only canonical solver deploy key")
        ]
        checkout_steps = [
            s
            for s in steps
            if s.startswith("name: Check out the exact canonical solver source")
        ]
        assert len(require_steps) == 1, lane
        assert len(checkout_steps) == 1, lane
        require_step = require_steps[0]
        checkout_step = checkout_steps[0]
        # The condition is pinned by parsed VALUE: `if: true # if: matrix...`
        # keeps the literal while presenting the key and checking out the
        # solver on every matrix entry (sol-critic round 8, finding 2).
        for step in (require_step, checkout_step):
            conditions = [
                _value_of(l) for l in step.splitlines() if _key_of(l) == "if"
            ]
            assert conditions == ["matrix.image == 'canonical-worker'"], (
                lane, conditions)
        # The presence check carries the key as its own step env and nothing
        # more; the solver checkout is the pinned repository with the key as
        # its ssh-key input and no persisted credentials.
        assert (
            "AUTOFILL_SOLVER_DEPLOY_KEY: ${{ secrets.AUTOFILL_SOLVER_DEPLOY_KEY }}"
            in require_step
        ), lane
        assert require_step.count(secret_ref) == 1, lane
        assert "uses: actions/checkout@v4" in checkout_step, lane
        assert "repository: LEAF-Solar-Design/autofill-solver" in checkout_step, lane
        assert (
            "ssh-key: ${{ secrets.AUTOFILL_SOLVER_DEPLOY_KEY }}" in checkout_step
        ), lane
        assert "persist-credentials: false" in checkout_step, lane
        assert checkout_step.count(secret_ref) == 1, lane
        # One reference in each of those two steps and two in the whole
        # block: the key provably reaches no other step in this job. With
        # the file total pinned to 4, it reaches no other job either.
        assert block.count(secret_ref) == 2, lane

    # The BUILD job cannot publish an untested image: it waits on the full
    # gate, run against the exact commit `prepare` resolved. Branch
    # protection is unavailable on this repository's plan, so this
    # workflow-internal dependency is what enforces that for build, and must
    # not be loosened. It says nothing about the warm job; see the
    # limitation block below.
    assert "uses: ./.github/workflows/test-gate.yml" in text
    assert "needs: [prepare, test, adopt]" in text

    # BOTH image matrices (warm and build) carry all five images and do not
    # cancel siblings after one failure. Warm covering canonical-worker is
    # operator decision D1 (2026-08-05); silently dropping an image from
    # either matrix reopens a cold post-gate build. The regex is anchored to
    # the exact matrix mapping line and counted per job block, so a
    # five-image list surviving only in a comment cannot satisfy it (sol-
    # critic round 1 on PR #445). A failed build matrix entry still blocks
    # the verification job.
    # Membership, not line order: reordering the same five images is a
    # legitimate edit (sol-critic round 11 false positive), dropping one is
    # not. Exactly one matrix line per block, so a second cannot hide
    # beside the pinned one.
    for lane, block in (("warm", warm_block), ("build", build_block)):
        matrices = [
            l for l in block.splitlines() if re.match(r"^\s*image: \[", l)
        ]
        assert len(matrices) == 1, lane
        members = [m.strip() for m in matrices[0].split("[", 1)[1].rstrip("]").split(",")]
        assert sorted(members) == [
            "app", "broker", "canonical-worker", "harness", "web",
        ], (lane, members)
    assert "fail-fast: false" in warm_block
    assert "fail-fast: false" in build_block
    assert "needs: [prepare, adopt, build]" in text
    verify_guard = (
        "    if: >-\n"
        "      ${{ !cancelled() && !inputs.promote && !inputs.speculative &&\n"
        "          needs.prepare.result == 'success' &&\n"
        "          (needs.build.result == 'success' ||\n"
        "           (needs.build.result == 'skipped' && needs.adopt.outputs.adopted == 'true')) }}\n"
    )
    assert text.count(verify_guard) == 1

    # ECR tags are immutable. Current and previous commits have distinct cache
    # tags, fixed buildcache is forbidden, and a rerun never overwrites cache.
    assert 'current_cache_tag="buildcache-$current_short"' in text
    assert 'previous_cache_tag="buildcache-$previous_short"' in text
    assert '"$previous_cache_tag" == "$current_cache_tag"' in text
    assert "Current and previous cache tags must differ" in text
    assert "cache-to: ${{ steps.cache.outputs.to }}" in text
    assert "cache-from: ${{ steps.cache.outputs.from }}" in text
    assert re.search(r":buildcache(?:[\s,]|$)", text) is None
    assert "cache $CURRENT_CACHE_TAG already exists" in text
    assert "immutable tag will not be overwritten" in text
    assert "skipping cache publication" in text

    # Only push events may select the immediate prior commit. Dispatches and
    # missing predecessor cache manifests leave cache-from empty.
    assert '"$GITHUB_EVENT_NAME" == "push"' in text
    assert "BEFORE_SHA: ${{ github.event.before }}" in text
    assert 'cache_from=""' in text
    assert 'echo "from=$cache_from"' in text
    assert 'if [[ -n "$PREVIOUS_CACHE_TAG" ]]' in text
    assert "has no predecessor cache; building without cache input" in text

    # Cache growth has an explicit bounded-retention infrastructure contract.
    assert "expire buildcache-* tags" in text
    assert "after 14 days" in text

    # Handoff depends on the five-image manifest and an accepted staging
    # execution receipt. The historical tag-only production dispatch is gone.
    assert re.search(r"handoff:\s*\n\s+needs: \[prepare\]", text)
    verify_start = text.index("  verify:")
    verify_body = text[verify_start : text.index("  handoff:", verify_start)]
    assert "for image in app broker canonical-worker harness web; do" in verify_body
    assert "aws ecr batch-get-image" in verify_body
    assert '--image-ids "imageTag=$TAG"' in verify_body
    assert "prod-[0-9a-f]{7,40}|sha-[0-9a-f]{40}" in verify_body
    assert 'if [[ -z "$digest" || "$digest" == "None" ]]' in verify_body
    assert "platform_release_manifest.py generate" in verify_body
    assert "digest-web-dist --root dist" in verify_body
    assert "--web-artifact-sha256" in verify_body
    assert (
        "staging-supply-set-${{ needs.prepare.outputs.source_sha }}-attempt-${{ github.run_attempt }}"
        in verify_body
    )
    assert (
        "web-dist-${{ needs.prepare.outputs.source_sha }}-attempt-${{ github.run_attempt }}"
        in verify_body
    )

    handoff_body = text[text.index("  handoff:") :]
    # The handoff guard is a CONJUNCTION, pinned whole: a dispatch that is
    # not a promote, or a promote of a commit that is not on main, must not
    # mint a production handoff candidate. Asserting the operands separately
    # left `||` in place of either `&&` passing (sol-critic round 6), and
    # asserting the block merely EXISTS somewhere let the live guard be
    # weakened while a decoy copy sat in a later block scalar (round 7), so
    # the block is pinned once in the file AND at the job's own `if:` key.
    handoff_guard = (
        "    if: >-\n"
        "      github.event_name == 'workflow_dispatch' &&\n"
        "      inputs.promote &&\n"
        "      needs.prepare.outputs.source_mode == 'main'\n"
    )
    assert text.count(handoff_guard) == 1
    assert handoff_body.index(handoff_guard) < handoff_body.index("    steps:")
    assert [k for k in _keys_in(handoff_body) if k == "if"] == ["if"]
    assert "inputs.promote" in handoff_body
    assert "RELEASE_RUN_ID: ${{ inputs.release_workflow_run_id }}" in handoff_body
    assert "RELEASE_RUN_ATTEMPT: ${{ inputs.release_run_attempt }}" in handoff_body
    assert (
        "ACCEPTANCE_RUN_ATTEMPT: ${{ inputs.staging_acceptance_run_attempt }}"
        in handoff_body
    )
    assert "verify-workflow-run" in handoff_body
    assert "verify-artifact" in handoff_body
    assert '--workflow-path "$RELEASE_WORKFLOW_PATH"' in handoff_body
    assert '--workflow-path "$ACCEPTANCE_WORKFLOW_PATH"' in handoff_body
    assert '--event push --branch main --head-sha "$SOURCE_SHA"' in handoff_body
    assert "--event workflow_dispatch --branch main" in handoff_body
    assert "staging-supply-set-$SOURCE_SHA-attempt-$RELEASE_RUN_ATTEMPT" in handoff_body
    assert "actions/artifacts/$RELEASE_ARTIFACT_ID/zip" in handoff_body
    assert "actions/artifacts/$ACCEPTANCE_ARTIFACT_ID/zip" in handoff_body
    assert "ACCEPTANCE_RECEIPT_RUN_ID=${BASH_REMATCH[1]}" in handoff_body
    assert '--release-run-proof "$RUNNER_TEMP/release-run-proof.json"' in handoff_body
    assert '--expected-receipt-run-id "$ACCEPTANCE_RECEIPT_RUN_ID"' in handoff_body
    assert "/compare/$ACCEPTANCE_HEAD_SHA...main" in handoff_body
    assert "staging-authored-execute-" in text
    assert "platform_release_manifest.py verify-staging" in handoff_body
    assert "git fetch --no-tags origin main" in handoff_body
    assert "--main-ref origin/main" in handoff_body
    assert "production-handoff-candidate-" in handoff_body
    assert "-attempt-${{ github.run_attempt }}" in handoff_body
    assert "gh workflow run deploy-service-production.yml" not in text
    assert "aws ecr put-image" not in handoff_body
    assert "docker/build-push-action" not in handoff_body
    # FOUR build-lane jobs now carry the promote guard (test, warm, build,
    # verify): a promote run reuses already-published digests and must never
    # rebuild, so the cache warmer is skipped on that path exactly like the
    # jobs it feeds.
    # test and warm carry the promote guard AND the speculative guard: a
    # speculative dispatch builds nothing deployable, so it must not run
    # the gate (its PR's standalone gate run mints the proof) or the layer
    # warmer. build/verify/adopt/speculate guards are pinned whole below.
    assert text.count("if: ${{ !inputs.promote && !inputs.speculative }}") == 2
    assert "Production handoff requires the exact release source_sha input" in text
    assert "Production handoff requires the successful release workflow run ID" in text
    assert "Production handoff requires the exact release run attempt" in text
    assert (
        "Production handoff requires the exact staging acceptance run attempt" in text
    )
    assert "leaf.staging-supply-set.v1" in DEPLOY_DOC
    assert "leaf.production-handoff-candidate.v1" in DEPLOY_DOC
    assert "four OCI" in DEPLOY_DOC
    assert "Vercel deployment ID" in DEPLOY_DOC
    assert "staging web image digest alone is never production web proof" in DEPLOY_DOC

    # Every build source consumes the same full application revision. The
    # canonical worker also seals its separate solver revision into the image.
    for image in ("app", "broker", "canonical-worker", "harness", "web"):
        dockerfile = (ROOT / "deploy" / f"Dockerfile.{image}").read_text(
            encoding="utf-8"
        )
        assert "ARG LEAF_SOURCE_SHA" in dockerfile
        assert "LEAF_SOURCE_SHA=${LEAF_SOURCE_SHA}" in dockerfile
    canonical = (ROOT / "deploy" / "Dockerfile.canonical-worker").read_text(
        encoding="utf-8"
    )
    assert "ARG AUTOFILL_SOLVER_REVISION" in canonical
    assert "/opt/leaf/autofill-solver/.leaf-source-revision" in canonical
    assert "/app/.leaf-source-revision" in canonical

    # ------------------------------------------------------------------ #
    # The warm/build split: layers are prepared in parallel with the test
    # gate, and the DECLARED intent is that no image reaches ECR before the
    # gate is green. This repository has no branch protection, so
    # `build: needs: [prepare, test]` is what enforces that for the build
    # job. It does not constrain warm, which holds the same push role; the
    # limitation block below says what that leaves unenforced. The
    # assertions here pin warm's declared no-publish configuration, which
    # catches drift in what the job is written to do, not what its
    # credentials would permit.
    # ------------------------------------------------------------------ #
    assert "needs: prepare" in warm_block
    assert "continue-on-error: true" in warm_block, (
        "a warm failure must degrade to a cold build, never redden the run")

    # The no-publish contract binds to the actual build step, not the job
    # text: a `push: false` in a comment or an unrelated field must not
    # satisfy it. Steps are split on the step-list marker at its exact
    # indentation, so the slice below is one step's uses:/with: mapping and
    # nothing else.
    warm_steps = re.split(r"\n      - ", warm_block)
    warm_builds = [s for s in warm_steps if "uses: docker/build-push-action" in s]
    assert len(warm_builds) == 1, "warm holds exactly one build-push-action step"
    warm_build_step = warm_builds[0]

    def _with_mapping(step: str) -> str:
        # The action only reads inputs from the step's with: mapping. A
        # push: false parked under env: (or any other step key) satisfies an
        # indentation-only regex while the action input is gone, so the
        # assertion below must scan the with: body and nothing else.
        lines = step.splitlines()
        try:
            start = next(i for i, l in enumerate(lines) if l == "        with:")
        except StopIteration:
            raise AssertionError("a build-push-action step carries no with: mapping")
        body = []
        for line in lines[start + 1:]:
            if line.strip() and not line.startswith("          "):
                break
            body.append(line)
        return "\n".join(body)

    warm_with = _with_mapping(warm_build_step)
    assert re.search(r"^          push: false$", warm_with, re.M), (
        "the warm build step's with: mapping must set the literal input push: false")
    # Key-level, not substring: `"outputs":` parses as the outputs input
    # while evading a literal `outputs:` scan (sol-critic round 7).
    for banned in ("tags", "outputs", "provenance", "sbom", "attests"):
        assert banned not in _keys_in(warm_build_step), (
            "the warm build step must not carry the %r input: every "
            "publication channel of build-push-action stays closed in the "
            "warm lane" % banned)
    # Value-level, not substring: a comment mentioning push: true inside the
    # step is harmless text (sol-critic round 12 false positive), while the
    # real input is already pinned to false by the with:-mapping assertion
    # above. This catches a second push key sneaking in beside it.
    assert [
        _value_of(l) for l in warm_build_step.splitlines() if _key_of(l) == "push"
    ] == ["false"]

    # A warmed layer only matches the gated build if both builds hash the
    # same inputs. Any drift between the two steps' context, Dockerfile,
    # build-args, or extra build-contexts makes warm burn runners while the
    # post-gate build silently goes cold again — the exact regression this
    # job exists to prevent — so the cache-key-bearing inputs must stay
    # byte-identical.
    gated_builds = [
        s
        for s in re.split(r"\n      - ", build_block)
        if "uses: docker/build-push-action" in s
    ]
    assert len(gated_builds) == 1, "build holds exactly one build-push-action step"
    build_with = _with_mapping(gated_builds[0])

    def _with_input(body: str, key: str) -> str:
        lines = body.splitlines()
        try:
            start = next(
                i for i, l in enumerate(lines) if l.startswith("          " + key)
            )
        except StopIteration:
            raise AssertionError("a build step's with: mapping lacks %r" % key)
        taken = [lines[start]]
        for line in lines[start + 1:]:
            if line.strip() and not line.startswith("            "):
                break
            taken.append(line)
        return "\n".join(taken)

    for key in ("context:", "file:", "build-args:", "build-contexts:"):
        assert _with_input(warm_with, key) == _with_input(build_with, key), (
            "warm and build must carry a byte-identical %s input, or the "
            "warmed cache never matches the gated build" % key)

    # Warm may only use these actions. Adding one is a conscious act in a
    # job that holds the solver deploy key, so it should cost a test edit.
    allowed_uses = {
        "actions/checkout@v4",
        "docker/setup-buildx-action@v3",
        "aws-actions/configure-aws-credentials@v6.1.0",
        "aws-actions/amazon-ecr-login@v2",
        "docker/build-push-action@v6",
    }
    warm_uses = [
        _value_of(l) for l in warm_block.splitlines() if _key_of(l) == "uses"
    ]
    assert set(warm_uses) <= allowed_uses, sorted(set(warm_uses) - allowed_uses)
    assert "tags" not in _keys_in(warm_block), (
        "a cache warmer publishes no image tag")

    # ------------------------------------------------------------------ #
    # WHAT THIS TEST DOES NOT PROVE, stated plainly so nobody mistakes a
    # green run for a guarantee (sol-critic rounds 8 to 10 on PR #445).
    #
    # Warm holds the same ECR push role the gated build does, because it
    # reads and writes the registry layer cache. Any shell it runs can
    # therefore publish an image before the gate is green. Three attempts
    # to forbid that by inspection all failed: a command denylist was
    # beaten by `docker buildx build --push`, then by `docker image push`,
    # `podman push`, and `buildctl ... push=true`; and a structural rule
    # tight enough to try (an exact list of permitted shell steps) still
    # admitted `ctr images push`, `cosign copy`, and any helper script or
    # make target, while rejecting five ordinary maintenance edits —
    # a third diagnostic step, `aws ecr describe-images`, `docker
    # version`, and renaming or reordering the permitted steps.
    #
    # A text check cannot prove the absence of a publishing command in
    # arbitrary shell. State the consequence without softening it: there
    # is currently NO enforceable boundary preventing the warm job from
    # publishing a release image before the gate is green. The gate edge
    # asserted below constrains the BUILD job only; it says nothing about
    # warm, which holds the same push role. The boundary would come from
    # narrowing AWS_ECR_PUSH_ROLE so warm can write buildcache-* and
    # nothing else — infrastructure rather than workflow, filed as a
    # follow-up rather than faked here.
    #
    # What the warm assertions below DO buy: they pin the job's declared
    # configuration, so drift in what it is written to do is caught.
    # ------------------------------------------------------------------ #

    # The gate edge, pinned as an EFFECTIVE job key at its exact
    # indentation. A bare substring search was satisfied by
    # `needs: [prepare] # needs: [prepare, test]` (sol-critic round 6),
    # which drops the gate dependency while reading as intact. `adopt`
    # joined the list for ORDERING (its verdict decides whether this matrix
    # runs at all); the gate dependency is the same edge it always was, and
    # the job guard below additionally requires the gate's success by
    # explicit result check, so a skipped-but-satisfied adopt cannot smuggle
    # a build past a failed gate.
    assert re.search(r"(?m)^    needs: \[prepare, test, adopt\]$", build_block), (
        "the gate edge is what stops the BUILD job publishing an untested "
        "image; nothing else in this repository does")
    assert len(re.findall(r"(?m)^    needs:", build_block)) == 1, (
        "the build job declares exactly one needs: list")
    build_guard = (
        "    if: >-\n"
        "      ${{ !cancelled() && !inputs.promote && !inputs.speculative &&\n"
        "          needs.prepare.result == 'success' &&\n"
        "          needs.test.result == 'success' &&\n"
        "          (needs.adopt.result == 'skipped' || needs.adopt.outputs.adopted != 'true') }}\n"
    )
    assert text.count(build_guard) == 1
    assert build_guard in build_block
    assert "push: true" in build_block

    # The warmed cache is what makes the post-gate build cheap; without this
    # preference the warm job burns five runners and saves nothing.
    assert "current_warm" in build_block
    assert 'cache_from="type=registry,ref=$cache_repo:$CURRENT_CACHE_TAG"' in build_block

    # Both writers race on the same immutable tag by design, and ECR refuses
    # the loser with ImageTagAlreadyExistsException. The registry cache
    # exporter must therefore tolerate export errors in BOTH jobs, or a lost
    # race fails the gated build. Bound to the EFFECTIVE cache_to assignment,
    # not to any occurrence: a shell comment carrying the option satisfied a
    # plain count while the real export lost it (sol-critic round 6).
    for lane, block in (("warm", warm_block), ("build", build_block)):
        live = [l for l in block.splitlines() if not l.strip().startswith("#")]
        exports = [l for l in live if 'cache_to="type=registry' in l]
        assert len(exports) == 1, lane
        assert exports[0].rstrip().endswith('ignore-error=true"'), lane
        assert len([l for l in live if "ignore-error=true" in l]) == 1, lane
        # And nothing rewrites the variable afterwards: a later
        # `cache_to="${cache_to%,*}"` strips the option back off while every
        # assertion above still passes (sol-critic round 7, finding 4). The
        # only assignments allowed are the empty initialiser and the pinned
        # export line.
        # Direct assignments: the empty initialiser, then the pinned
        # export, optionally hardened with `readonly` or `declare`. This
        # catches DRIFT in how the variable is set. It is not a proof that
        # nothing later rewrites it — shell offers too many spellings, and
        # `declare cache_to="${cache_to%,*}"` slipped past the previous
        # attempt (sol-critic round 11). The named rewrites below are the
        # ones seen in review, not an exhaustive set. Reads are
        # unrestricted, so a debug echo does not redden the gate.
        writes = [
            l for l in live
            if re.match(r"^\s*(?:readonly\s+|declare\s+)?cache_to=", _comment_cut(l))
        ]
        assert len(writes) == 2, (lane, writes)
        # The same prefixes the regex accepts are accepted here, or
        # `declare cache_to=""` would match as a write and then fail the
        # literal comparison (sol-critic round 12).
        assert re.sub(r"^(?:readonly|declare)\s+", "", writes[0].strip()) == (
            'cache_to=""'), lane
        assert writes[1] == exports[0], lane
        for rewrite in (r"\bunset\s+cache_to\b", r"\bexport\s+cache_to\b",
                        r"\bprintf\s+-v\s+cache_to\b", r"\bread\s+cache_to\b"):
            assert not re.search(rewrite, "\n".join(_comment_cut(l) for l in live)), (
                lane, rewrite)

    # ------------------------------------------------------------------ #
    # Tree-bound gate verdict (operator decision D3, 2026-08-05): the called
    # gate may skip its shards when a verified proof already binds the exact
    # tree, so the build job must in turn refuse to push any image unless the
    # gate's proven tree equals the tree it checked out — before the push
    # step, with no way to pass on an empty output. And the called workflow's
    # reuse probe reads cross-run gate-proof artifacts through the Actions
    # API, whose permissions are capped by THIS caller's grant: dropping
    # actions:read here silently disables the skip (every build runs the full
    # gate and the probe 403s), so the grant is pinned.
    # ------------------------------------------------------------------ #
    test_block = text.split("\n  test:\n", 1)[1].split("\n  warm:\n", 1)[0]
    assert "uses: ./.github/workflows/test-gate.yml" in test_block
    assert "actions: read" in test_block
    assert "PROVEN_TREE: ${{ needs.test.outputs.proven_tree }}" in build_block
    assert '[[ "$PROVEN_TREE" =~ ^[0-9a-f]{40}$ ]]' in build_block
    assert 'built_tree="$(git rev-parse ' in build_block
    assert build_block.count("refusing to push images") == 2, (
        "both refusal arms — no proven tree at all, and a foreign tree — "
        "must stay fail-closed")
    # A step that carries an `if:` can be switched off without being
    # removed, so neither the tree binding nor the push may carry one at
    # all (sol-critic round 6: `if: ${{ false }}` on the binding step left
    # the push enabled while every literal stayed green). The two solver
    # steps are the only conditional steps in this job, and their condition
    # is pinned to the canonical-worker matrix entry above.
    for step in re.split(r"\n      - ", build_block):
        conditional = "if" in _keys_in(step)
        if "Require the green gate verdict to bind" in step:
            assert not conditional, "the tree binding must not be conditional"
        if "uses: docker/build-push-action" in step:
            assert not conditional, "the image push must not be conditional"
    bind_at = build_block.index("Require the green gate verdict to bind")
    push_at = build_block.index("uses: docker/build-push-action")
    assert bind_at < push_at, "the tree binding must precede the push step"

    # ------------------------------------------------------------------ #
    # Speculative PR builds (lane L5, operator decision D3, 2026-08-05).
    # Three jobs joined the workflow: `speculate` (matrix build of the merge
    # preview, pushed under the non-deployable spec-<tree> namespace),
    # `speculate-manifest` (the tree-redefined partial-push invariant: a
    # spec-supply-set artifact exists only when all five digests verify),
    # and `adopt` (the merge-run half: alias the release tag onto verified
    # speculative digests, else degrade to the full build). The pins below
    # hold the seams that keep untested content unreachable by any deploy:
    # the spec namespace never overlaps sha-/prod-, adoption requires the
    # gate's proven tree, and every adopt failure lands on adopted=false.
    # ------------------------------------------------------------------ #
    speculate_block = text.split("\n  speculate:\n", 1)[1].split(
        "\n  speculate-manifest:\n", 1
    )[0]
    manifest_block = text.split("\n  speculate-manifest:\n", 1)[1].split(
        "\n  adopt:\n", 1
    )[0]
    adopt_block = text.split("\n  adopt:\n", 1)[1].split("\n  handoff:\n", 1)[0]

    # Speculative dispatches must never queue inside the release concurrency
    # group (they are dispatched on the main ref), and only they may cancel
    # a predecessor.
    assert (
        "group: build-platform-images-${{ inputs.speculative && "
        "format('speculative-pr-{0}', inputs.speculative_pr_number) || github.ref }}"
    ) in text
    assert "cancel-in-progress: ${{ inputs.speculative || false }}" in text

    # The spec tag IS the tree, derived in prepare, and the derivation
    # exports the tree for the manifest and adopt jobs.
    assert 'image_tag="spec-$current_tree"' in text
    assert "current_tree=\"$(git rev-parse 'HEAD^{tree}')\"" in text
    assert 'echo "tree=$current_tree"' in text
    assert "source_tree: ${{ steps.tag.outputs.tree }}" in text

    # speculate runs ONLY as a speculative dispatch, carries the same
    # five-image matrix as warm/build, and never cancels siblings early.
    assert (
        "if: ${{ github.event_name == 'workflow_dispatch' && inputs.speculative }}"
        in speculate_block
    )
    assert "fail-fast: false" in speculate_block
    speculate_matrices = [
        l for l in speculate_block.splitlines() if re.match(r"^\s*image: \[", l)
    ]
    assert len(speculate_matrices) == 1
    speculate_members = [
        m.strip()
        for m in speculate_matrices[0].split("[", 1)[1].rstrip("]").split(",")
    ]
    assert sorted(speculate_members) == [
        "app", "broker", "canonical-worker", "harness", "web",
    ], speculate_members

    # The speculative build must hash the same inputs as the gated build, or
    # an adopted image would not realize the tree the gate proved. Same
    # byte-identity contract warm already carries.
    speculate_builds = [
        s
        for s in re.split(r"\n      - ", speculate_block)
        if "uses: docker/build-push-action" in s
    ]
    assert len(speculate_builds) == 1, "speculate holds exactly one build step"
    speculate_with = _with_mapping(speculate_builds[0])
    for key in ("context:", "file:", "build-args:", "build-contexts:"):
        assert _with_input(speculate_with, key) == _with_input(build_with, key), (
            "speculate and build must carry a byte-identical %s input" % key
        )
    assert re.search(r"^          push: true$", speculate_with, re.M)
    assert (
        "tags: ${{ env.ECR_REGISTRY }}/${{ env.IMAGE_NAME }}:${{ env.IMAGE_TAG }}"
        in speculate_with
    )
    # Import-only cache: no export, so no immutable-tag export race exists
    # in this lane at all. Live lines only — the shell comment explaining
    # the posture is allowed to name the option.
    speculate_live = [
        l for l in speculate_block.splitlines() if not l.strip().startswith("#")
    ]
    assert not [l for l in speculate_live if "cache_to" in l], (
        "the speculative lane must not export layer cache"
    )
    assert "cache-to" not in _keys_in(speculate_builds[0])
    # The push step and the solver steps are conditional on the
    # existence-skip: a tree already pushed is adopted as-is and the solver
    # key is never fetched for it. Pinned by parsed value, as elsewhere.
    speculate_condition = (
        "matrix.image == 'canonical-worker' && "
        "steps.exists.outputs.present != 'true'"
    )
    for step_name in (
        "name: Require the read-only canonical solver deploy key",
        "name: Check out the exact canonical solver source",
    ):
        steps = [
            s
            for s in re.split(r"\n      - ", speculate_block)
            if s.startswith(step_name)
        ]
        assert len(steps) == 1, step_name
        conditions = [
            _value_of(l) for l in steps[0].splitlines() if _key_of(l) == "if"
        ]
        assert conditions == [speculate_condition], (step_name, conditions)
    assert [
        _value_of(l)
        for l in speculate_builds[0].splitlines()
        if _key_of(l) == "if"
    ] == ["steps.exists.outputs.present != 'true'"]
    assert speculate_block.count(secret_ref) == 2
    # Both preview-consuming jobs re-verify the checkout is the exact
    # preview prepare resolved; a moved preview aborts rather than builds
    # the wrong bytes.
    assert speculate_block.count("Require the same merge preview prepare resolved") == 1
    assert manifest_block.count("Require the same merge preview prepare resolved") == 1

    # The partial-push invariant over tree identity: no artifact without all
    # five digests, and the mint is gated on the SAME step output the upload
    # is. if-no-files-found stays error so an empty upload cannot pass.
    assert "complete=false" in manifest_block
    assert "the speculative set is incomplete and no manifest will be minted" in (
        manifest_block
    )
    assert "generate-speculative" in manifest_block
    mint_or_upload = [
        s
        for s in re.split(r"\n      - ", manifest_block)
        if s.startswith("name: Mint the tree-bound speculative supply set")
        or "uses: actions/upload-artifact" in s
    ]
    assert len(mint_or_upload) == 2
    for step in mint_or_upload:
        assert [
            _value_of(l) for l in step.splitlines() if _key_of(l) == "if"
        ] == ["steps.digests.outputs.complete == 'true'"]
    assert (
        "name: spec-supply-set-${{ needs.prepare.outputs.source_tree }}"
        in manifest_block
    )
    assert "if-no-files-found: error" in manifest_block

    # adopt: absorbed like the gate-reuse probe (a defect costs the
    # optimization, never the push run), reads artifacts with an explicit
    # read-only Actions grant, and follows the probe's provenance
    # discipline: same-repo origin from workflow_run metadata, this
    # workflow's bare path, a main-ref workflow_dispatch run. Content
    # verifies via verify-speculative BEFORE any tag is written, only the
    # release prod- namespace may be aliased, and every alias re-verifies
    # before adopted=true is declared.
    assert "if: ${{ !inputs.promote && github.event_name == 'push' }}" in adopt_block
    assert "continue-on-error: true" in adopt_block
    assert "actions: read" in adopt_block
    assert "PROVEN_TREE: ${{ needs.test.outputs.proven_tree }}" in adopt_block
    assert ".workflow_run.head_repository_id == $repo_id" in adopt_block
    assert 'path="${path%%@*}"' in adopt_block
    assert '.github/workflows/build-platform-images.yml" ]] || continue' in adopt_block
    assert '"workflow_dispatch" ]] || continue' in adopt_block
    assert '"main" ]] || continue' in adopt_block
    assert "verify-speculative" in adopt_block
    assert "--expect-tree" in adopt_block
    assert adopt_block.index("verify-speculative") < adopt_block.index(
        "aws ecr put-image"
    ), "no tag may be written before the manifest verifies"
    assert adopt_block.count("aws ecr put-image") == 1
    assert "refusing to alias non-release tag" in adopt_block
    assert "re-verification failed" in adopt_block
    assert 'echo "adopted=$1"' in adopt_block
    # The decide step carries no if: at all — like the probe, it absorbs
    # failures internally; a conditional here could leave stale outputs
    # deciding whether the build matrix runs.
    decide_steps = [
        s
        for s in re.split(r"\n      - ", adopt_block)
        if s.startswith("name: Adopt a verified speculative supply set")
    ]
    assert len(decide_steps) == 1
    assert "if" not in _keys_in(decide_steps[0])

    # The staging relay accepts exactly the two supply-set schemas; the
    # deployable fields are the same shape in both.
    relay_text = (WORKFLOW.parent / "dispatch-staging-deploys.yml").read_text(
        encoding="utf-8"
    )
    assert (
        "leaf.staging-supply-set.v1|leaf.staging-supply-set.v2) ;;" in relay_text
    )
    assert relay_text.count("is not an accepted staging supply-set schema") == 1

    # The dispatcher is deliberately secretless and same-repo-gated: the
    # real build runs on the MAIN ref (main's workflow text, main's OIDC
    # subject). If a secrets.* reference ever appears here, the PR-editable
    # pull_request surface has grown a credential, which is exactly what
    # the dispatch indirection exists to prevent.
    dispatcher_text = (WORKFLOW.parent / "speculate-platform-images.yml").read_text(
        encoding="utf-8"
    )
    assert "secrets." not in dispatcher_text
    assert "pull_request:" in dispatcher_text
    assert "actions: write" in dispatcher_text
    assert (
        "github.event.pull_request.head.repo.full_name == github.repository"
        in dispatcher_text
    )
    assert "github.event.pull_request.head.repo.fork == false" in dispatcher_text
    assert "--ref main" in dispatcher_text
    assert '-f "speculative=true"' in dispatcher_text

    # Cache growth for the speculative namespace has the same explicit
    # bounded-retention infrastructure contract buildcache-* carries.
    assert "spec-* tags" in text
    assert "leaf.staging-supply-set.v2" in DEPLOY_DOC
    assert "spec-<tree>" in DEPLOY_DOC

    print("build-platform-images workflow invariants: PASS")


def test_build_platform_images_workflow_invariants() -> None:
    # Pytest entry point: the gate runner counts collected tests, and a bare
    # main() collects as zero.
    main()


if __name__ == "__main__":
    main()
