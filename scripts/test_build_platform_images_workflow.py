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
    for required_check in (
        '.state == "open"',
        ".draft == true",
        '.base.ref == "main"',
        ".head.repo.full_name == $repo",
        ".head.repo.fork == false",
        "(.head.sha | ascii_downcase) == $sha",
    ):
        assert source_body.count(required_check) == 1
    assert "exact head of an open same-repository draft PR" in source_body
    assert "Only a source commit on main may request production promotion" in text
    assert "needs.prepare.outputs.source_mode == 'main'" in text
    # THREE jobs hold the ECR push credential, not two: `warm` joined build and
    # verify when layer warming moved beside the test gate. It genuinely needs
    # the role — it reads and writes the registry-backed layer cache — so the
    # honest statement is that a third job can reach ECR, and the thing that
    # keeps an untested IMAGE out is the assertion below that warm's build step
    # sets push:false and publishes no tag. Raising this number again should
    # mean a new job that provably needs registry access, not a convenience.
    # Counted by parsed key and value, not by exact text: `id-token : write`
    # or a quoted key grants OIDC while leaving a literal count at three,
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

    # THREE jobs hold the ECR push credential: warm, build, verify. See the
    # note above the deferred assignment for why this is parsed, not
    # text-matched.
    oidc_grants = [
        l for l in structural
        if _key_of(l) == "id-token" and _value_of(l) == "write"
    ]
    assert len(oidc_grants) == 3, oidc_grants

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
    assert text.count(secret_ref) == 4
    assert text.count("ssh-key: ${{ secrets.AUTOFILL_SOLVER_DEPLOY_KEY }}") == 2
    assert "AUTOFILL_SOLVER_DEPLOY_KEY=" not in text
    # Five mentions per lane (env key + secret ref, presence test, error
    # message, ssh-key) and none anywhere else: a new reference to the key
    # in any other step or comment must consciously bump this count.
    assert text.count("AUTOFILL_SOLVER_DEPLOY_KEY") == 10
    require_name = "      - name: Require the read-only canonical solver deploy key"
    checkout_name = "      - name: Check out the exact canonical solver source"
    # Exactly one presence-check and one solver-checkout step per image job,
    # and none anywhere else in the workflow: with the global total pinned to
    # 2, a key step relocated into any other job cannot go unnoticed.
    assert text.count(require_name) == 2
    assert text.count(checkout_name) == 2
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

    # An untested image can never reach ECR: the build job waits on the full
    # gate, run against the exact commit `prepare` resolved. Branch protection
    # is unavailable on this repository's plan, so this workflow-internal
    # dependency is the only enforceable gate and must not be loosened.
    assert "uses: ./.github/workflows/test-gate.yml" in text
    assert "needs: [prepare, test]" in text

    # BOTH image matrices (warm and build) carry all five images and do not
    # cancel siblings after one failure. Warm covering canonical-worker is
    # operator decision D1 (2026-08-05); silently dropping an image from
    # either matrix reopens a cold post-gate build. The regex is anchored to
    # the exact matrix mapping line and counted per job block, so a
    # five-image list surviving only in a comment cannot satisfy it (sol-
    # critic round 1 on PR #445). A failed build matrix entry still blocks
    # the verification job.
    five_image_matrix = (
        r"^        image: \[app, broker, canonical-worker, harness, web\]$"
    )
    assert len(re.findall(five_image_matrix, warm_block, re.M)) == 1
    assert len(re.findall(five_image_matrix, build_block, re.M)) == 1
    # And no OTHER image matrix line survives in either block: four-image
    # lists or duplicates fail here rather than hiding beside the pinned one.
    assert len(re.findall(r"^\s*image: \[", warm_block, re.M)) == 1
    assert len(re.findall(r"^\s*image: \[", build_block, re.M)) == 1
    assert "fail-fast: false" in warm_block
    assert "fail-fast: false" in build_block
    assert "needs: [prepare, build]" in text

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
    assert text.count("if: ${{ !inputs.promote }}") == 4
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
    # The warm/build split: layers may be prepared in parallel with the test
    # gate, but NOTHING may reach ECR as an image before the gate is green.
    # This repository has no branch protection, so `build: needs: [prepare,
    # test]` is the entire enforcement — a warm job that ever learns to push,
    # or a build job that stops needing `test`, silently removes it.
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
    assert "push: true" not in warm_build_step

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

    # And no publish path outside that step either. Enumerating publishing
    # COMMANDS is a losing game — `docker buildx build --push` beat the
    # first list (sol-critic round 8), then `docker image push`, `podman
    # push`, and `buildctl ... push=true` beat the second (round 9). So the
    # contract is structural instead: warm may only run the five pinned
    # actions, and it may contain exactly ONE shell step, the cache
    # selector. A publishing command needs somewhere to live, and in this
    # job there is nowhere: any added run step fails outright, whatever it
    # contains.
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
    warm_run_steps = [s for s in warm_steps if "run" in _keys_in(s)]
    assert [s.splitlines()[0] for s in warm_run_steps] == [
        "name: Require the read-only canonical solver deploy key",
        "name: Select immutable cache references",
    ], "warm carries exactly these two shell steps; a third is where a " \
       "pre-gate publish would live"

    # Those two steps talk to ECR only to READ tags. Comments are stripped
    # first, so a comment naming a banned tool is fine (round-9 false
    # positive); an actual invocation is not. Container tooling has no
    # business in either, which is why the ban is on the tool name rather
    # than on a verb list that keeps growing.
    warm_script = "\n".join(
        _comment_cut(l) for s in warm_run_steps for l in s.splitlines()
    )
    forbidden_tool = re.search(
        r"\b(docker|podman|nerdctl|buildctl|buildah|crane|skopeo|oras|regctl)\b",
        warm_script,
    )
    assert not forbidden_tool, (
        "the warm shell steps must not invoke container tooling: %r"
        % (forbidden_tool.group(1) if forbidden_tool else ""))
    for call in re.findall(r"aws ecr [a-z-]+", warm_script):
        assert call == "aws ecr batch-get-image", call
    assert "push: true" not in warm_block
    assert "tags" not in _keys_in(warm_block), (
        "a cache warmer publishes no image tag")

    # The gate edge, pinned as an EFFECTIVE job key at its exact
    # indentation. A bare substring search was satisfied by
    # `needs: [prepare] # needs: [prepare, test]` (sol-critic round 6),
    # which drops the gate dependency while reading as intact.
    assert re.search(r"(?m)^    needs: \[prepare, test\]$", build_block), (
        "the gate edge is this repository's only enforcement that an untested "
        "image never reaches ECR")
    assert len(re.findall(r"(?m)^    needs:", build_block)) == 1, (
        "the build job declares exactly one needs: list")
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
        # Every mention of the variable is accounted for, not just lines
        # that begin with an assignment: `unset cache_to`, `export
        # cache_to=...`, and `printf -v cache_to ...` all rewrite it after
        # the pinned line (sol-critic round 8, finding 4).
        mentions = [l for l in live if "cache_to" in _comment_cut(l)]
        assert len(mentions) == 3, (lane, mentions)
        assert mentions[0].strip() == 'cache_to=""', lane
        assert mentions[1] == exports[0], lane
        assert mentions[2].strip() == 'echo "to=$cache_to" >> "$GITHUB_OUTPUT"', lane

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

    print("build-platform-images workflow invariants: PASS")


def test_build_platform_images_workflow_invariants() -> None:
    # Pytest entry point: the gate runner counts collected tests, and a bare
    # main() collects as zero.
    main()


if __name__ == "__main__":
    main()
