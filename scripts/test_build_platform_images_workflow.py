#!/usr/bin/env python3
"""Regression checks for the production image build workflow.

Mostly static text invariants, plus stronger bindings for the docs-noop
gate and its staging relay: their structure is asserted against the
PARSED workflow YAML, textual assertions run against comment-stripped
executable bash (a guard that drifts into a comment stops counting),
and the decide script is extracted from the parsed YAML and EXECUTED
against a real git history, including the rename vector that rename
detection would disguise as a docs-only diff.
"""

from pathlib import Path
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile

import yaml  # gate venv + prepare job: installed from scripts/requirements-ci.txt


class _StrictLoader(yaml.SafeLoader):
    """SafeLoader that refuses duplicate mapping keys.

    yaml.safe_load silently keeps the LAST duplicate key, so a hostile
    file could show these assertions a healthy final value while an
    earlier duplicate carries the payload (whichever one another parser
    honors). Ambiguity fails loud here instead.
    """

    def construct_mapping(self, node, deep=False):
        seen = set()
        for key_node, _ in node.value:
            key = self.construct_object(key_node, deep=True)
            if key in seen:
                raise ValueError(
                    f"duplicate YAML mapping key {key!r} at {key_node.start_mark}"
                )
            seen.add(key)
        return super().construct_mapping(node, deep)


def _strict_yaml(text: str):
    return yaml.load(text, Loader=_StrictLoader)


_BASH_COMMENT = re.compile(r"(?:^|(?<=\s))#.*$")


def _executable_bash(script: str) -> str:
    """Strip bash comments (full-line and trailing) from a run scalar.

    Text assertions run against this so they bind to executable lines
    only: a guard that drifts into a comment stops counting. The hash
    survives when glued to non-space (e.g. ${#array[@]}, "## heading").
    Known tradeoff: a quoted value containing a whitespace-preceded hash
    ("status # data") would be truncated; none of the checked scripts
    carries one, and a false trip here fails loud, never silently green.
    """
    lines = []
    for line in script.splitlines():
        code = _BASH_COMMENT.sub("", line).rstrip()
        if code.strip():
            lines.append(code)
    return "\n".join(lines)


WORKFLOW = (
    Path(__file__).resolve().parents[1]
    / ".github"
    / "workflows"
    / "build-platform-images.yml"
)
ROOT = WORKFLOW.parents[2]
DEPLOY_DOC = (ROOT / "deploy" / "README.md").read_text(encoding="utf-8")

# The warm-skip hint script, pinned BYTE-EXACT (sol-critic round 3 on
# PR #449): substring pins over "live" lines were spoofed by TRAILING
# comments carrying the pinned text while the code beside them was
# rewritten, and no substring set proves the absence of a rewrite. Any
# edit to the hint script is therefore a conscious edit of this copy
# too. The same idiom already binds warm's and build's cache-bearing
# inputs byte-identical.
HINT_SCRIPT = """\
          # Deliberately NOT errexit (set +e overrides the runner's `bash -e`
          # wrapper): every failure below must degrade to expected=false —
          # warm runs, the pre-hint behaviour — never redden the build.
          set -uo pipefail
          set +e
          expected="false"
          tree="$(git rev-parse 'HEAD^{tree}' 2>/dev/null)"
          if [[ "$tree" =~ ^[0-9a-f]{40}$ ]]; then
            repo_id="$(gh api "repos/$GITHUB_REPOSITORY" --jq .id 2>/dev/null)"
            listing="$(gh api "repos/$GITHUB_REPOSITORY/actions/artifacts?name=gate-proof-$tree&per_page=30" 2>/dev/null)"
            if [[ -n "$repo_id" && -n "$listing" ]]; then
              # Same-repo provenance comes from the artifact's workflow_run
              # metadata, exactly as in the gate's probe: a fork's
              # pull_request run can upload an artifact with any name. The
              # probe's per-candidate minting-workflow check is skipped here
              # on purpose — it costs one API call per candidate, and a
              # same-repo actor gaming a warm SKIP is outside the threat
              # model (they hold push and could edit this workflow).
              hits="$(jq -r --argjson repo_id "$repo_id" '
                [.artifacts[]
                 | select(.expired | not)
                 | select(.workflow_run.head_repository_id == $repo_id)]
                | length' <<<"$listing" 2>/dev/null)"
              if [[ "$hits" =~ ^[1-9][0-9]*$ ]]; then
                expected="true"
              fi
            fi
          fi
          echo "expected=$expected" >> "$GITHUB_OUTPUT"
          if [[ "$expected" == "true" ]]; then
            echo "::notice::a same-repo gate proof already names tree $tree; the warm job is skipped (the gate's own probe still verifies the proof before any shard skip)"
          else
            echo "::notice::no same-repo gate proof names tree ${tree:-<unknown>}; the warm job runs beside the gate"
          fi
          exit 0
"""


def main() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")

    # Newline discipline at the BYTE level (sol-critic round 4 on PR #449;
    # the regression class from round 1): read_text() and splitlines()
    # erase CR bytes, so a wholesale CRLF rewrite of this workflow would
    # sail through every text assertion below, including the byte-exact
    # hint pin. Pin the bytes before trusting the text.
    assert b"\r" not in WORKFLOW.read_bytes(), (
        "build-platform-images.yml must be LF-only")

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
    # SIX jobs hold an ECR credential: warm (the buildcache-scoped role,
    # which reaches only the *-buildcache repositories), build, verify, and
    # the speculative lane's speculate (pushes spec-<tree> images),
    # speculate-manifest (reads live digests to mint the tree-bound
    # manifest), and adopt (aliases the release tag onto verified digests)
    # on the release role. Each provably needs registry access; the role
    # split is pinned by the role-to-assume assertions below. Raising this
    # number should mean a new job that provably needs registry access,
    # not a convenience.
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
        "           (needs.build.result == 'skipped' && needs.adopt.result == 'success' &&\n"
        "            needs.adopt.outputs.adopted == 'true')) }}\n"
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
    # test carries the promote guard AND the speculative guard (a
    # speculative dispatch must not run the gate: its PR's standalone gate
    # run mints the proof). build and verify moved to compound whole-pinned
    # guards (see build_guard/verify_guard), and warm's guard is compound —
    # promote, speculative, and prepare's gate-reuse hint — pinned by exact
    # parsed value below rather than counted here.
    assert text.count("if: ${{ !inputs.promote }}") == 0
    assert text.count("if: ${{ !inputs.promote && !inputs.speculative }}") == 1
    # Warm's guard, whole and at the job's own if: key. Warm is also skipped
    # when a same-repo gate proof already names this exact tree: the gate leg
    # then reuses the verdict in ~30s, the build job starts before any warm
    # leg could publish its cache, and warm burns five runners for nothing
    # (measured: zero of nine warm legs contributed across the first two
    # reuse-path runs, 30978164812 and 30983842725). `!= 'true'` keeps the
    # hint fail-open: an empty, missing, or failed hint output runs warm.
    warm_job_header = warm_block[: warm_block.index("    steps:")]
    warm_guards = [
        _value_of(l) for l in warm_job_header.splitlines() if _key_of(l) == "if"
    ]
    assert warm_guards == [
        "${{ !inputs.promote && !inputs.speculative && "
        "needs.prepare.outputs.gate_reuse_expected != 'true' }}"
    ], warm_guards
    # The hint is an ECONOMIC signal with a deliberately narrow blast
    # radius: it may gate warm and NOTHING else. Skipping the GATE stays the
    # sole business of the called gate's verified reuse probe and fan-in
    # re-verification, and the build job's own tree binding is what refuses
    # an unproven push — a hint defect must never widen past a colder
    # build. Exactly two occurrences: the prepare output that mints it and
    # the warm guard that consumes it.
    assert text.count("gate_reuse_expected") == 2
    prepare_block = text.split("\n  prepare:\n", 1)[1].split("\n  test:\n", 1)[0]
    assert prepare_block.count("gate_reuse_expected") == 1
    assert warm_job_header.count("gate_reuse_expected") == 1
    # BOUND, not merely counted (sol-critic round 1 on PR #449): a
    # hard-coded `gate_reuse_expected: true` in prepare's outputs kept every
    # assertion above green while skipping warm on every non-promote push.
    # The output must carry exactly the hint step's expression, that step
    # must exist in prepare under the referenced id and key its listing on
    # this exact tree's proof-artifact name, and prepare must hold the
    # actions:read grant the listing depends on. A job-level permissions
    # block REPLACES the workflow-level set, so the two standing grants are
    # pinned beside it: dropping either breaks the draft-PR source check or
    # the checkout, and dropping actions:read 403s the hint into a
    # permanent expected=false — warm silently runs on every reuse path
    # again.
    prepare_header = prepare_block[: prepare_block.index("    steps:")]
    hint_outputs = [
        _value_of(l) for l in prepare_header.splitlines()
        if _key_of(l) == "gate_reuse_expected"
    ]
    assert hint_outputs == ["${{ steps.reuse-hint.outputs.expected }}"], hint_outputs
    # No structural line ANYWHERE in this workflow may declare `defaults`
    # or `shell`: jobs.<id>.defaults.run.shell (or the workflow-scoped
    # form) redefines the interpreter of every run step in scope and can
    # selectively skip a pinned script by matching its content — a
    # `bash -c 'grep -Fq <hint marker> "$1" || bash -e "$1"' _ {0}` on
    # prepare passed every step-level pin while the hint emitted nothing
    # (sol-critic round 6 on PR #449); a step-level shell: is already
    # excluded by the hint step's exact key set below. This workflow uses
    # neither key anywhere; introducing one is a conscious contract edit.
    for line in structural:
        assert _key_of(line) not in ("defaults", "shell"), (
            "defaults/shell are banned in this workflow: %r" % line)

    # The hint STEP is located on STRUCTURAL lines only, using the same
    # block-scalar classifier as the lexical gate above: raw-text slicing
    # let a `- run: |` outer step swallow an apparent id:, env:, and a
    # canonical-looking inner script as shell TEXT while its real first
    # lines emitted expected=true (sol-critic round 4 on PR #449, finding
    # 1). A floating id: in another step's env (round 2) and trailing-
    # comment pin carriers (round 3) are the same family: only what YAML
    # PARSES as the step's own keys and the run block's own content may
    # satisfy a pin.
    annotated = []
    scalar_indent = None
    for line in prepare_block.splitlines():
        stripped = line.strip()
        indent = len(line) - len(line.lstrip(" "))
        if scalar_indent is not None:
            if not stripped or indent > scalar_indent:
                annotated.append((line, False))
                continue
            scalar_indent = None
        annotated.append((line, True))
        if block_header.search(_uncommented(line)):
            scalar_indent = indent
    step_starts = [
        i for i, (l, s) in enumerate(annotated) if s and l.startswith("      - ")
    ]
    # prepare's step LIST is pinned, first key and value per step, in
    # order: a step inserted ahead of the hint can poison the environment
    # of every later step (round 7 on PR #449 exported
    # BASH_ENV=/tmp/... via GITHUB_ENV, so the next bash step exited 0
    # before the byte-pinned script ran — empty output, warm silently
    # runs on every reuse path). Adding a step to this job is a conscious
    # contract edit.
    #
    # WHAT THIS PIN DOES NOT PROVE, plainly, in the tradition of the
    # limitation block above the gate-edge assertions: a text contract
    # cannot prove the hint step will EXECUTE its pinned script in an
    # unpoisoned environment — an edit to an EXISTING step's free-text
    # run block can still export BASH_ENV, rewrite PATH, or shadow
    # gh/jq, and no enumerable ban closes arbitrary shell. Every defeat
    # of that kind leaves the hint's output empty or false, and the warm
    # guard's `!= 'true'` polarity maps exactly that to RUNNING warm —
    # the pre-#449 behaviour, slower and never wrong. The enforceable
    # boundary, pinned throughout this section, is blast radius: the
    # hint's output reaches the warm guard and nothing else, so no
    # defeat of the hint can redden a build, skip a gate, or publish an
    # image.
    prepare_step_heads = []
    for n, start in enumerate(step_starts):
        first = annotated[start][0]
        prepare_step_heads.append((_key_of(first), _value_of(first)))
    assert prepare_step_heads == [
        ("uses", "actions/checkout@v4"),
        ("name", "Require exact source to be reviewed"),
        ("name", "Resolve canonical solver provenance"),
        ("name", "Validate image workflow invariants"),
        ("name", "Derive immutable image tag"),
        ("name", "Probe for an expected gate reuse (warm-skip hint)"),
    ], prepare_step_heads
    hint_ranges = []
    for n, start in enumerate(step_starts):
        end = step_starts[n + 1] if n + 1 < len(step_starts) else len(annotated)
        if any(
            s and re.match(r"^        id: reuse-hint\s*$", l)
            for l, s in annotated[start:end]
        ):
            hint_ranges.append((start, end))
    assert len(hint_ranges) == 1, "prepare must hold exactly one reuse-hint step"
    hint_seg = annotated[hint_ranges[0][0]:hint_ranges[0][1]]
    hint_structural = [l for l, s in hint_seg if s]
    # The step's structural key set is EXACT and ordered: pinning the run
    # block, the condition, and the token still permitted an extra key,
    # and `shell: bash -c true {0}` before the run block returns success
    # without ever executing the pinned script — empty output, warm runs
    # on every reuse path again (sol-critic round 5 on PR #449). Any new
    # key on this step (shell, with, working-directory, continue-on-error,
    # a second env entry, ...) is a conscious contract edit.
    hint_keys = [k for k in (_key_of(l) for l in hint_structural) if k]
    assert hint_keys == ["name", "id", "env", "GH_TOKEN", "run"], hint_keys
    # Exactly one run key, spelled exactly as the one plain literal header
    # (a folded, chomped, quoted, or comment-carrying header is a
    # different spelling and fails), and the run block must END the step:
    # nothing structural may follow it, so no second header or stray key
    # can hide behind the canonical content.
    run_keys = [l for l, s in hint_seg if s and _key_of(l) == "run"]
    assert run_keys == ["        run: |"], run_keys
    run_at = next(
        i for i, (l, s) in enumerate(hint_seg) if s and l == "        run: |"
    )
    tail = hint_seg[run_at + 1:]
    assert all(not s for l, s in tail), "the run block must end the hint step"
    assert "\n".join(l for l, s in tail).rstrip("\n") == HINT_SCRIPT.rstrip("\n"), (
        "the warm-skip hint script must equal its canonical pinned copy in "
        "this file; edit both together, consciously")
    # And the step must actually RUN, with the workflow token: an if: on
    # the step (or an emptied GH_TOKEN) silently restores permanent
    # expected=false - warm runs on every reuse path again with nothing
    # red anywhere (round 3, finding 2). Pinned on structural lines.
    assert "if" not in [k for k in (_key_of(l) for l in hint_structural) if k], (
        "the reuse hint must not be conditional")
    hint_tokens = [
        _value_of(l) for l in hint_structural if _key_of(l) == "GH_TOKEN"
    ]
    assert hint_tokens == ["${{ github.token }}"], hint_tokens
    # The grant backing the listing must sit in prepare's own job-level
    # permissions MAPPING, sliced as a mapping: an `actions: read` parked
    # under a job env satisfied the previous header-wide scan while the
    # real grant vanished and the hint 403'd into a permanent
    # expected=false (round 2, finding 3). A job-level block REPLACES the
    # workflow-level set, so the two standing grants are pinned with it.
    header_lines = prepare_header.splitlines()
    assert header_lines.count("    permissions:") == 1, (
        "prepare declares exactly one job-level permissions block")
    perm_start = header_lines.index("    permissions:")
    perm_lines = []
    for line in header_lines[perm_start + 1:]:
        if line.strip() and not line.startswith("      "):
            break
        perm_lines.append(line)
    prepare_perms = sorted(
        (_key_of(l), _value_of(l)) for l in perm_lines if _key_of(l)
    )
    assert prepare_perms == [
        ("actions", "read"), ("contents", "read"), ("pull-requests", "read"),
    ], prepare_perms
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

    # The warm job's ONLY credential is the buildcache-scoped role, bound to
    # the role-to-assume VALUE of each lane's credentials step rather than
    # raw text: a comment carrying the expected line while the live step
    # assumed something else satisfied the substring form (sol-critic
    # round 1 on this chip's PR). _key_of/_value_of are comment-cut, so a
    # commented copy contributes nothing here.
    def _assumed_roles(block: str):
        return [
            _value_of(l) for l in block.splitlines()
            if _key_of(l) == "role-to-assume"
        ]

    release_role = "${{ secrets.AWS_ECR_PUSH_ROLE }}"
    cache_role = "${{ secrets.AWS_ECR_BUILDCACHE_PUSH_ROLE }}"
    assert _assumed_roles(warm_block) == [cache_role], (
        "the warm job's single credentials step must assume the "
        "buildcache-scoped role and nothing else")
    assert _assumed_roles(build_block) == [release_role]
    role_verify_block = text.split("\n  verify:\n", 1)[1].split(
        "\n  speculate:\n", 1)[0]
    assert _assumed_roles(role_verify_block) == [release_role]
    assert _assumed_roles(text) == [
        cache_role,
        release_role,  # build
        release_role,  # verify
        release_role,  # speculate: pushes real spec-* images by design
        release_role,  # speculate-manifest: reads live digests
        release_role,  # adopt: aliases the release tag onto digests
    ], ("exactly six role assumptions in the workflow, in job order "
        "warm, build, verify, speculate, speculate-manifest, adopt")
    # Substring bans stay as belt-and-braces: comment-inclusive is fine for
    # a NEGATIVE. (Sound: AWS_ECR_BUILDCACHE_PUSH_ROLE does not contain the
    # substring AWS_ECR_PUSH_ROLE.)
    assert "AWS_ECR_PUSH_ROLE" not in warm_block
    assert "AWS_ECR_BUILDCACHE_PUSH_ROLE" not in build_block

    # ALL cache traffic lives in the dedicated *-buildcache repositories,
    # asserted on comment-stripped executable shell lines with the exact
    # permitted assignment. _comment_cut on every surviving line as well: a
    # LIVE release probe with the expected text in a TRAILING comment
    # defeated the whole-line scan while remaining valid shell (sol-critic
    # rounds 1 and 2 on this chip's PR).
    for lane, block in (("warm", warm_block), ("build", build_block)):
        live = [
            _comment_cut(l) for l in block.splitlines()
            if not l.strip().startswith("#")
        ]
        assignments = [l.strip() for l in live if "cache_repo=" in l]
        assert assignments == [
            'cache_repo="$ECR_REGISTRY/$IMAGE_NAME-buildcache"'
        ], lane
        probes = [l for l in live if "--repository-name" in l]
        assert len(probes) == 3, lane
        for probe in probes:
            assert '--repository-name "$IMAGE_NAME-buildcache"' in probe, (
                lane, probe)
        assert not re.search(
            r'--repository-name "\$IMAGE_NAME"(?!-)', "\n".join(live)
        ), lane
    # The speculative lane: its cache IMPORT reads the *-buildcache
    # repository, while its image-existence probe reads the RELEASE
    # repository (it asks whether the spec image tag itself is present).
    cache_speculate_block = text.split("\n  speculate:\n", 1)[1].split(
        "\n  speculate-manifest:\n", 1)[0]
    speculate_live = [
        _comment_cut(l) for l in cache_speculate_block.splitlines()
        if not l.strip().startswith("#")
    ]
    assert [l.strip() for l in speculate_live if "cache_repo=" in l] == [
        'cache_repo="$ECR_REGISTRY/$IMAGE_NAME-buildcache"'
    ]
    speculate_probes = [l for l in speculate_live if "--repository-name" in l]
    assert len(speculate_probes) == 2
    assert sorted(
        '"$IMAGE_NAME-buildcache"' in p for p in speculate_probes
    ) == [False, True], speculate_probes
    tag_values = [
        _value_of(l) for l in build_block.splitlines() if _key_of(l) == "tags"
    ]
    assert tag_values == [
        "${{ env.ECR_REGISTRY }}/${{ env.IMAGE_NAME }}:${{ env.IMAGE_TAG }}"
    ], "the release push tag targets the release repository, never a cache"
    assert "-buildcache:${{" not in text

    # ------------------------------------------------------------------ #
    # WHAT THIS TEST DOES NOT PROVE, stated plainly so nobody mistakes a
    # green run for a guarantee (sol-critic rounds 8 to 10 on PR #445).
    #
    # A text check cannot prove the absence of a publishing command in
    # arbitrary shell (the attempts and their failures are recorded in this
    # block's git history). Since leaf-automation-aws-terraform's
    # leaf_platform_buildcache.tf landed, the boundary for WARM is IAM
    # rather than inspection: the warm job's configured credential reaches
    # only the five *-buildcache repositories, so no shell step in warm can
    # write a repository the deploy rail reads. ECR has no tag-level IAM
    # condition (aws/containers-roadmap#230), which is why the split is
    # per-repository. The assertions above pin the wiring that keeps that
    # true IN THIS FILE: warm assumes only the cache-scoped role, and cache
    # references point only at cache repositories.
    #
    # Still true and deliberately unfaked:
    #  * the gate edge asserted below constrains the BUILD job only;
    #  * the speculative lane (speculate, speculate-manifest, adopt) holds
    #    the RELEASE role by design -- it pushes real spec-* images from PR
    #    merge previews; its fence is the tag namespace no deploy accepts
    #    plus adopt's tree-bound proof, not IAM;
    #  * the gated build IMPORTS the cache warm wrote, so an attacker with
    #    code execution inside warm could still poison layers that flow
    #    through the gate into a release image (inherent to remote layer
    #    cache reuse, and present before warm existed via the predecessor
    #    cache);
    #  * the same-subject escape (code in warm minting a fresh job token
    #    and assuming the release role directly) is closed by the
    #    ecr-release GitHub environment: every release-role job declares
    #    environment: ecr-release (asserted on the parsed workflow below),
    #    warm never does, and the release role's IAM trust accepts only the
    #    environment-qualified subject once the 2026-08-05 transition in
    #    leaf-automation-aws-terraform's leaf_iam.tf completes. Until the
    #    ref-based subject is removed there, the escape is merely narrowed,
    #    not closed -- the trust policy, not this file, is the boundary.
    #
    # What the warm assertions above and below DO buy: they pin the job's
    # declared configuration, so drift in what it is written to do is
    # caught.
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
        "          (needs.adopt.result == 'skipped' ||\n"
        "           (needs.adopt.result == 'success' && needs.adopt.outputs.adopted != 'true')) }}\n"
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

    check_docs_noop_filter(text)

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
    assert 'image_tag="spec-$current_tree-$current_spec_short"' in text
    assert "current_tree=\"$(git rev-parse 'HEAD^{tree}')\"" in text
    assert 'echo "tree=$current_tree"' in text
    assert "source_tree: ${{ steps.tag.outputs.tree }}" in text

    # speculate runs ONLY as a speculative dispatch, carries the same
    # five-image matrix as warm/build, and never cancels siblings early.
    speculate_guards = [
        _value_of(l)
        for l in speculate_block.splitlines()
        if _key_of(l) == "if" and l.startswith("    if")
    ]
    assert speculate_guards == [
        "${{ github.event_name == 'workflow_dispatch' && inputs.speculative }}"
    ], speculate_guards
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
    manifest_guard = (
        "    if: >-\n"
        "      ${{ !cancelled() && github.event_name == 'workflow_dispatch' &&\n"
        "          inputs.speculative && needs.prepare.result == 'success' }}\n"
    )
    assert text.count(manifest_guard) == 1
    assert manifest_guard in manifest_block
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
    adopt_guards = [
        _value_of(l)
        for l in adopt_block.splitlines()
        if _key_of(l) == "if" and l.startswith("    if")
    ]
    assert adopt_guards == [
        "${{ !inputs.promote && github.event_name == 'push' }}"
    ], adopt_guards
    # NO JOB-level continue-on-error: before the first tag write the EXIT
    # trap (and the individually absorbed SETUP steps — see the parsed
    # step pins below) degrade every failure to adopted=false, and after
    # it adoption is committed — a deliberate exit 1 must fail the run,
    # because the fallback rebuild would collide with the half-aliased
    # immutable release tag. The build guard above refuses to run when
    # adopt FAILED for the same reason.
    adopt_header = adopt_block[: adopt_block.index("    steps:")]
    assert "continue-on-error" not in _keys_in(adopt_header)
    assert "trap on_exit EXIT" in adopt_block
    assert "COMMITTED=false" in adopt_block
    assert adopt_block.count("RERUN THIS RUN") == 2
    assert "positively absent" in adopt_block
    assert [
        _value_of(l)
        for l in adopt_block.splitlines()
        if _key_of(l) == "actions"
    ] == ["read"]
    assert "PROVEN_TREE: ${{ needs.test.outputs.proven_tree }}" in adopt_block
    assert ".workflow_run.head_repository_id == $repo_id" in adopt_block
    assert 'path="${path%%@*}"' in adopt_block
    assert '.github/workflows/build-platform-images.yml" ]] || continue' in adopt_block
    assert '"workflow_dispatch" ]] || continue' in adopt_block
    assert '"main" ]] || continue' in adopt_block
    assert "verify-speculative" in adopt_block
    assert "--expect-tree" in adopt_block
    # The spec tag comes from the verified manifest (tree + baking preview,
    # sol-critic round 1 finding 1), never re-derived from the tree alone.
    assert "spec_tag=\"$(jq -r '.spec_tag' \"$verified\")\"" in adopt_block
    assert "spec-[0-9a-f]{40}-[0-9a-f]{12}" in adopt_block
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

    # Effective parsed values (sol-critic round 2 on PR #450, finding 3):
    # the text pins above bind format; these bind what GitHub actually
    # evaluates — a guard relocated into a name: scalar or a permission
    # hidden behind write-all fails here.
    wf_doc = _strict_yaml(text)
    wf_jobs = wf_doc["jobs"]

    def _folded(value) -> str:
        return " ".join(str(value).split())

    assert wf_jobs["speculate"]["if"] == (
        "${{ github.event_name == 'workflow_dispatch' && inputs.speculative }}"
    )
    assert wf_jobs["adopt"]["if"] == (
        "${{ !inputs.promote && github.event_name == 'push' }}"
    )
    assert _folded(wf_jobs["speculate-manifest"]["if"]) == (
        "${{ !cancelled() && github.event_name == 'workflow_dispatch' && "
        "inputs.speculative && needs.prepare.result == 'success' }}"
    )
    assert _folded(wf_jobs["build"]["if"]) == (
        "${{ !cancelled() && !inputs.promote && !inputs.speculative && "
        "needs.prepare.result == 'success' && "
        "needs.test.result == 'success' && "
        "(needs.adopt.result == 'skipped' || "
        "(needs.adopt.result == 'success' && needs.adopt.outputs.adopted != 'true')) }}"
    )
    assert _folded(wf_jobs["verify"]["if"]) == (
        "${{ !cancelled() && !inputs.promote && !inputs.speculative && "
        "needs.prepare.result == 'success' && "
        "(needs.build.result == 'success' || "
        "(needs.build.result == 'skipped' && needs.adopt.result == 'success' && "
        "needs.adopt.outputs.adopted == 'true')) }}"
    )
    assert wf_jobs["build"]["needs"] == ["prepare", "test", "adopt"]
    assert wf_jobs["verify"]["needs"] == ["prepare", "adopt", "build"]
    assert wf_jobs["adopt"]["needs"] == ["prepare", "test"]

    # The release role is reachable only through the ecr-release GitHub
    # environment (main-only deployment branch policy): every job that
    # assumes AWS_ECR_PUSH_ROLE declares it as a plain scalar, so its OIDC
    # sub presents repo:...:environment:ecr-release. warm deliberately does
    # NOT declare it (nor does any other job): warm's ref-based subject must
    # stop matching the release role's trust once the leaf_iam.tf transition
    # removes the ref-based subject. Parsed, not text-matched, so a
    # commented copy or a {name: ...} mapping variant cannot satisfy it.
    release_env_jobs = {"build", "verify", "speculate", "speculate-manifest", "adopt"}
    for job_name in release_env_jobs:
        assert wf_jobs[job_name].get("environment") == "ecr-release", job_name
    for job_name, job in wf_jobs.items():
        if job_name not in release_env_jobs:
            assert "environment" not in job, (
                "%s must not declare a GitHub environment" % job_name)

    # The fail-fast main-ref guard in prepare. workflow_dispatch can target
    # any branch or tag; on a non-main ref the five release-role jobs would
    # otherwise only die at the ecr-release environment's main-only
    # deployment branch policy AFTER burning the full gate (sol-critic on
    # PR #454, round 1). Bound to the comment-stripped executable line so a
    # commented copy cannot satisfy it.
    env_prepare_block = text.split("\n  prepare:\n", 1)[1].split(
        "\n  test:\n", 1)[0]
    env_prepare_live = [
        _comment_cut(l) for l in env_prepare_block.splitlines()
        if not l.strip().startswith("#")
    ]
    guard_lines = [
        l for l in env_prepare_live
        if 'if [ "$GITHUB_REF" != "refs/heads/main" ]; then' in l
    ]
    assert len(guard_lines) == 1, (
        "prepare must fail fast exactly once when not on refs/heads/main")
    assert any(
        "may only run on refs/heads/main" in l for l in env_prepare_live
    ), "the main-ref guard must fail with an actionable error message"
    assert wf_jobs["adopt"]["permissions"] == {
        "id-token": "write",
        "contents": "read",
        "actions": "read",
    }
    assert "continue-on-error" not in wf_jobs["adopt"]
    # adopt's SETUP steps are individually absorbed (a pre-write failure
    # must degrade through decide, not veto the fallback build); the decide
    # step is the deliberate exception — its only nonzero exit is the
    # post-write committed path, which must fail the run.
    adopt_steps = wf_jobs["adopt"]["steps"]
    assert len(adopt_steps) == 5
    assert adopt_steps[-1].get("id") == "decide"
    for step in adopt_steps[:-1]:
        assert step.get("continue-on-error") is True, step
    assert "continue-on-error" not in adopt_steps[-1]
    assert "if" not in adopt_steps[-1]

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
    # the dispatch indirection exists to prevent. Bound to the PARSED
    # document (strict loader, duplicate keys refused), so a trigger,
    # permission, or guard relocated into a scalar cannot satisfy it; the
    # dispatch command lines are bound to the step's comment-stripped
    # EXECUTABLE bash.
    dispatcher_text = (WORKFLOW.parent / "speculate-platform-images.yml").read_text(
        encoding="utf-8"
    )
    # ABSENCE over the raw text: a comment mentioning secrets fails too,
    # which is the safe direction.
    assert "secrets." not in dispatcher_text
    dsp_doc = _strict_yaml(dispatcher_text)
    # YAML parses the `on:` key as boolean True.
    assert dsp_doc[True] == {"pull_request": {"branches": ["main"]}}
    assert dsp_doc["permissions"] == {"contents": "read", "actions": "write"}
    assert set(dsp_doc["jobs"]) == {"dispatch"}
    dispatch_job = dsp_doc["jobs"]["dispatch"]
    assert "permissions" not in dispatch_job, (
        "a job-level permissions block would REPLACE the workflow-level set"
    )
    assert _folded(dispatch_job["if"]) == (
        "github.event.pull_request.head.repo.full_name == github.repository && "
        "github.event.pull_request.head.repo.fork == false"
    )
    dispatch_steps = dispatch_job["steps"]
    assert len(dispatch_steps) == 2
    # Step 0 feeds the docs-only decision: an absorbed, credential-free
    # preview checkout. Absorption matters (a conflicted preview must not
    # redden the PR) and so does the credential posture (nothing later in
    # this job may find a persisted token).
    checkout_step = dispatch_steps[0]
    assert checkout_step["uses"] == "actions/checkout@v4"
    assert checkout_step["continue-on-error"] is True
    assert checkout_step["with"]["persist-credentials"] is False
    assert checkout_step["with"]["fetch-depth"] == 2
    assert checkout_step["with"]["ref"] == (
        "refs/pull/${{ github.event.pull_request.number }}/merge"
    )
    assert set(dispatch_steps[1]) <= {"name", "env", "run"}, (
        "the dispatch step runs plain bash with github.token only"
    )
    dispatch_script = _executable_bash(dispatch_steps[1]["run"])
    assert "gh workflow run build-platform-images.yml" in dispatch_script
    assert "--ref main" in dispatch_script
    assert '-f "speculative=true"' in dispatch_script
    assert '-f "source_sha=$HEAD_SHA"' in dispatch_script
    assert '-f "speculative_pr_number=$PR_NUMBER"' in dispatch_script
    # Docs-only skip, fail-open, with the SHAPE bound rather than merely
    # ordered (sol-critic round 1 on PR #452): exactly one DISPATCH=true
    # (the fail-open initializer) and one DISPATCH=false (inside the
    # literal-"skip" arm); the skip EXIT is pinned as a block so deleting
    # it cannot pass; and the filter executes from the TRUSTED first
    # parent via git show, never from the PR-controlled preview checkout —
    # this step's env carries GH_TOKEN with actions:write, so a
    # preview-sourced filter would hand a same-repo PR that token before
    # merge. The preview supplies only the FILE LIST (data, not code).
    assert dispatch_script.count("DISPATCH=true") == 1
    assert dispatch_script.count("DISPATCH=false") == 1
    assert "git diff --no-renames --name-only HEAD^1 HEAD" in dispatch_script
    assert (
        "git show 'HEAD^1:scripts/docs_noop_filter.py'" in dispatch_script
    )
    assert 'python3 "$RUNNER_TEMP/docs_noop_filter.py"' in dispatch_script
    assert "python3 scripts/docs_noop_filter.py" not in dispatch_script, (
        "the filter must never execute from the PR-controlled checkout"
    )
    assert '[ "$VERDICT" = "skip" ]' in dispatch_script
    assert dispatch_script.index('[ "$VERDICT" = "skip" ]') < (
        dispatch_script.index("DISPATCH=false")
    )
    skip_exit = re.search(
        r'if \[ "\$DISPATCH" != "true" \]; then\s*\n\s*exit 0\s*\n\s*fi',
        dispatch_script,
    )
    assert skip_exit, "the docs-only skip must exit before the dispatch"
    assert skip_exit.start() > dispatch_script.index("DISPATCH=true")
    assert skip_exit.end() < dispatch_script.index(
        "gh workflow run build-platform-images.yml"
    )
    assert dispatch_script.count("exit 0") == 1, (
        "exactly one exit 0: the docs-only skip; every other early exit is "
        "a validation error"
    )

    # Cache growth for the speculative namespace has the same explicit
    # bounded-retention infrastructure contract buildcache-* carries.
    assert "spec-* tags" in text
    assert "leaf.staging-supply-set.v2" in DEPLOY_DOC
    assert "spec-<tree>" in DEPLOY_DOC

    print("build-platform-images workflow invariants: PASS")


def check_docs_noop_filter(text: str) -> None:
    # ------------------------------------------------------------------ #
    # Docs-only no-op gate. The decision must NOT be a native paths /
    # paths-ignore filter: GitHub evaluates those over a truncated
    # changed-file window (its docs cite 300 files), so a large mixed
    # push could hide a code file past the window and silently skip a
    # build — fail-closed in exactly the direction this repo cannot
    # afford. The decision lives in the `noop` job (real git diff, no
    # window) and scripts/docs_noop_filter.py (imported and exercised
    # below — the shipped logic, not a re-implementation).
    # ------------------------------------------------------------------ #
    assert "paths-ignore" not in text, (
        "native path filtering is fail-closed on truncated diffs; the "
        "noop job owns the docs-only decision")

    # Structural invariants read from the PARSED workflow, so a guard that
    # drifts into a comment or another step key stops satisfying them.
    wf = _strict_yaml(text)
    on_block = wf.get("on") or wf.get(True)  # YAML 1.1 reads bare `on` as a bool
    assert on_block["push"] == {"branches": ["main"]}, (
        "the push trigger must carry branches only — no native path filter")

    jobs = wf["jobs"]
    noop_job = jobs["noop"]
    assert jobs["prepare"]["needs"] == "noop"
    assert jobs["prepare"]["if"] == "needs.noop.outputs.build == 'true'"
    assert noop_job["outputs"]["build"] == "${{ steps.decide.outputs.build }}"

    # No always() anywhere downstream of the gate: it would run a train job
    # even after the noop skip (or a failed upstream) shut its needs off.
    # test-gate.yml legitimately uses always() for its fan-in; when noop
    # skips, that called workflow never starts, so the scope is exactly the
    # build workflow and the staging relay.
    relay_text = (WORKFLOW.parent / "dispatch-staging-deploys.yml").read_text(
        encoding="utf-8"
    )
    for label, parsed in (
        ("build-platform-images.yml", wf),
        ("dispatch-staging-deploys.yml", _strict_yaml(relay_text)),
    ):
        for job_name, job in parsed["jobs"].items():
            conditions = [str(job.get("if", ""))]
            conditions += [str(s.get("if", "")) for s in job.get("steps", [])]
            for condition in conditions:
                assert "always(" not in condition, (
                    f"{label} job {job_name}: always() bypasses the "
                    "docs-noop gate and failure propagation"
                )

    # The decide step, extracted from the parsed YAML, then stripped to
    # executable bash. Every assertion from here down binds to text that
    # actually runs; the rehearsal below additionally executes the raw
    # scalar verbatim.
    decide = next(s for s in noop_job["steps"] if s.get("id") == "decide")
    decide_src = decide["run"]
    decide_code = _executable_bash(decide_src)
    assert decide["env"]["EVENT_NAME"] == "${{ github.event_name }}"
    assert decide["env"]["BEFORE_SHA"] == "${{ github.event.before }}"
    assert "git diff --no-renames --name-only" in decide_code, (
        "rename detection reports only a rename's destination, so a file "
        "moved out of a build-input tree would classify as docs-only; the "
        "decide diff must disable it"
    )
    assert "scripts/docs_noop_filter.py" in decide_code

    # Fail-open arms: every abnormal path must land on build=true before
    # the docs-only verdict is even consulted.
    for arm in (
        '[ "$EVENT_NAME" = "push" ] || build',
        '[[ "$BEFORE_SHA" =~ ^[0-9a-f]{40}$ ]] || build',
        '[[ "$BEFORE_SHA" =~ ^0{40}$ ]] && build',
        '|| build "before-sha unfetchable"',
        '|| build "diff failed"',
        "|| VERDICT=build",
        '[ "$VERDICT" = "skip" ] || build',
    ):
        assert arm in decide_code, f"missing fail-open arm: {arm}"

    # The relay tells a deliberate no-op from a broken run by this marker;
    # its name must mirror the supply-set naming (sha + attempt), and it is
    # published only on an actual skip.
    marker_uploads = [
        s
        for s in noop_job["steps"]
        if str(s.get("uses", "")).startswith("actions/upload-artifact")
    ]
    assert len(marker_uploads) == 1
    assert (
        marker_uploads[0]["with"]["name"]
        == "docs-noop-${{ github.sha }}-attempt-${{ github.run_attempt }}"
    )
    assert marker_uploads[0]["if"] == "steps.decide.outputs.build == 'false'"

    # Both handoff artifact listings stay pinned above GitHub's default
    # page size (30): today's builds publish ~11 artifacts, and an unpinned
    # listing would silently drop the newest artifact past a growth spurt.
    # Counted over executable text only, so a comment cannot stand in for
    # the pin.
    handoff_code = "\n".join(
        _executable_bash(s.get("run", "")) for s in jobs["handoff"]["steps"]
    )
    assert handoff_code.count("artifacts?per_page=100") == 2

    # Decision vectors run against the SHIPPED module.
    scripts_dir = str(Path(__file__).resolve().parent)
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    import docs_noop_filter as dnf

    # The motivating shapes: root README (6e73747) and docs/ trees skip.
    assert dnf.decide(["README.md"]) == "skip"
    assert dnf.decide(["docs/GATE-TREE-REUSE.md", "RUN.md"]) == "skip"
    assert dnf.decide(["docs/img/staging-train.png"]) == "skip"
    # A synthetic MIXED diff builds: one code file beside any docs.
    assert dnf.decide(["README.md", "web/src/App.jsx"]) == "build"
    # NESTED markdown is an image input (Dockerfile.app/broker COPY
    # server/ and contract/ wholesale; .dockerignore keeps markdown), so
    # it builds — the ignore set is docs/** plus ROOT *.md only.
    assert dnf.decide(["server/README.md"]) == "build"
    assert dnf.decide(["contract/AUTH.md"]) == "build"
    assert dnf.decide(["web/README.md"]) == "build"
    # Code, workflow, dependency, and root non-markdown changes build.
    assert dnf.decide(["server/app.py"]) == "build"
    assert dnf.decide([".github/workflows/build-platform-images.yml"]) == "build"
    assert dnf.decide(["docker-compose.yml"]) == "build"
    # Unknown / empty / garbage diffs fail open to building.
    assert dnf.decide([]) == "build"
    assert dnf.decide(["", "  "]) == "build"
    assert dnf.decide(['"docs/\\303\\251.md"']) == "build"

    # The relay is a FROZEN three-step surface: tip check, manifest read,
    # guarded dispatch. Adding a step, importing an action, or reordering
    # must break this harness and force a deliberate co-review. Guards are
    # compared with EXACT equality, never substring: a guard weakened to
    # `X == 'true' || true` still contains the healthy text and must fail.
    relay_wf = _strict_yaml(relay_text)
    # Workflow shape is frozen top to bottom (PyYAML reads the bare `on`
    # key as True). A new trigger, job, or permission grant must land here.
    assert set(relay_wf) == {"name", True, "permissions", "concurrency", "jobs"}
    assert relay_wf["name"] == "Dispatch staging deploys"
    assert relay_wf[True] == {
        "workflow_run": {
            "workflows": ["Build platform images"],
            "types": ["completed"],
        }
    }, "the relay trigger is frozen: broadening it re-reviews here"
    assert relay_wf["concurrency"] == {
        "group": (
            "dispatch-staging-deploys-"
            "${{ github.event.workflow_run.head_sha }}"
        ),
        "cancel-in-progress": False,
    }
    # CAPABILITY WALL: dispatching the infra repo requires the PAT, and the
    # workflow's own token is pinned read-only, so an assembled command in
    # an unguarded script has nothing to dispatch WITH. The token-denial
    # scan below is only a tripwire on top of this.
    assert relay_wf["permissions"] == {"actions": "read", "contents": "read"}
    assert set(relay_wf["jobs"]) == {"dispatch"}
    dispatch_job = relay_wf["jobs"]["dispatch"]
    assert set(dispatch_job) == {"if", "runs-on", "timeout-minutes", "env", "steps"}
    # Pinned VALUES, not just keys: on a self-hosted runner the PAT-backed
    # step would execute on infrastructure outside GitHub's ephemeral VMs.
    assert dispatch_job["runs-on"] == "ubuntu-latest"
    assert dispatch_job["timeout-minutes"] == 5
    assert dispatch_job["env"] == {
        "INFRA_REPO": "LEAF-Solar-Design/leaf-automation-aws-terraform",
        "DEPLOY_WORKFLOW": "deploy-leaf-platform-staging.yml",
        "BUILD_RUN_ID": "${{ github.event.workflow_run.id }}",
        "BUILD_HEAD_SHA": "${{ github.event.workflow_run.head_sha }}",
    }
    assert dispatch_job["if"] == (
        "github.event.workflow_run.conclusion == 'success' && "
        "github.event.workflow_run.event == 'push' && "
        "github.event.workflow_run.head_branch == 'main'"
    )
    relay_steps = dispatch_job["steps"]
    assert [s.get("id") for s in relay_steps] == ["tip", "manifest", None]
    tip_step, manifest_step, dispatch_step = relay_steps
    # Step KEY SETS are exact: no uses:, no shell:, no working-directory:,
    # no continue-on-error: may appear on any step without breaking this.
    assert set(tip_step) == {"name", "id", "env", "run"}
    assert set(manifest_step) == {"name", "id", "if", "env", "run"}
    assert set(dispatch_step) == {"name", "if", "env", "run"}
    # Step ENV dicts are exact: the unguarded steps hold only the
    # read-scoped workflow token, never the infra-repo PAT.
    assert tip_step["env"] == {"GH_TOKEN": "${{ github.token }}"}
    assert manifest_step["env"] == {
        "GH_TOKEN": "${{ github.token }}",
        "BUILD_RUN_ATTEMPT": "${{ github.event.workflow_run.run_attempt }}",
    }
    assert dispatch_step["env"] == {
        "GH_TOKEN": "${{ secrets.TERRAFORM_REPO_TOKEN }}",
        "IMAGE_TAG": "${{ steps.manifest.outputs.image_tag }}",
    }
    assert manifest_step["if"] == "steps.tip.outputs.current == 'true'"
    assert dispatch_step["if"] == (
        "steps.tip.outputs.current == 'true' && "
        "steps.manifest.outputs.deploy == 'true'"
    ), "without this exact guard a docs-only run dispatches an empty tag"

    # The PAT appears EXACTLY once in the whole parsed workflow, at the
    # guarded dispatch step's GH_TOKEN. Walking parsed values (not raw
    # text) keeps comments out of the count in both directions.
    def _walk_strings(node, path=""):
        if isinstance(node, dict):
            for key, value in node.items():
                yield from _walk_strings(value, f"{path}.{key}")
        elif isinstance(node, list):
            for index, value in enumerate(node):
                yield from _walk_strings(value, f"{path}[{index}]")
        elif isinstance(node, str):
            yield path, node

    secret_refs = [
        (path, value)
        for path, value in _walk_strings(relay_wf)
        if "secrets." in value
    ]
    assert len(secret_refs) == 1, (
        f"exactly one secret reference may exist in the relay: {secret_refs}"
    )
    assert secret_refs[0][1] == "${{ secrets.TERRAFORM_REPO_TOKEN }}"
    assert ".steps[2].env.GH_TOKEN" in secret_refs[0][0]

    tip_code = _executable_bash(tip_step["run"])
    manifest_code = _executable_bash(manifest_step["run"])
    dispatch_code = _executable_bash(dispatch_step["run"])

    # ALL THREE scripts are content-frozen: any edit to their executable
    # text (comments excluded) must update this hash in the same PR, so
    # neither an assembled dispatch in an unguarded step nor an extra
    # PAT-backed command under the guarded step's healthy-looking guard
    # can land silently. The property checks around this stay for
    # readable failures; the hash is the contract, and the capability
    # wall means the unguarded scripts hold no credential able to
    # dispatch even if edited.
    frozen = hashlib.sha256(
        "\n===\n".join((tip_code, manifest_code, dispatch_code)).encode("utf-8")
    ).hexdigest()
    # Hash updated for lane L5 (PR #450): the manifest step's schema pin
    # widened to accept leaf.staging-supply-set.v1 OR .v2 — same deployable
    # fields, no new dispatch capability, no new secret surface.
    assert frozen == (
        "a4fd3f7b49df61be1a15b307c1b8425d08a6d47ad0410cfcedb38225fc4ff22a"
    ), (
        "relay step scripts changed: review the diff for dispatch "
        "capability, then update this hash in the same PR"
    )

    assert (
        'NOOP_NAME="docs-noop-$BUILD_HEAD_SHA-attempt-$BUILD_RUN_ATTEMPT"'
        in manifest_code
    )
    assert manifest_code.count('echo "deploy=false"') == 1
    assert manifest_code.count('echo "deploy=true"') == 1
    assert manifest_code.count("artifacts?per_page=100") == 1
    assert "no $NOOP_NAME marker present" in manifest_code, (
        "manifest-and-marker both absent must stay a hard error: a "
        "successful build without a supply set is a partial run")

    # Only the guarded third step may know how to dispatch anything. This
    # token scan is a heuristic tripwire on top of the capability wall
    # above (read-only permissions plus the PAT pinned to the guarded
    # step): an assembled command in tip or manifest trips this, and even
    # one that slips past has no credential able to dispatch.
    for step_name, code in (("tip", tip_code), ("manifest", manifest_code)):
        for token in (
            "gh workflow run",
            "/dispatches",
            "curl",
            "wget",
            "-X POST",
            "--method",
        ):
            assert token not in code, (
                f"relay {step_name} step must not carry a dispatch "
                f"path: {token}"
            )

    assert "gh workflow run" in dispatch_code
    assert '--repo "$INFRA_REPO"' in dispatch_code
    for dispatch_input in (
        '"service=$SERVICE"',
        '"expected_task_definition=auto-live"',
        '"image_tag=$IMAGE_TAG"',
        '"app_deploy_intent=forward"',
    ):
        assert dispatch_input in dispatch_code

    # Finally, run the extracted decide script for real.
    _rehearse_decide_script(decide_src, dnf)


def _rehearse_decide_script(decide_src: str, dnf) -> None:
    """Execute the workflow's ACTUAL decide script (extracted from the
    parsed YAML, never re-typed here) against a real git history.

    A docs-only push must skip; a code push must build; and the rename
    vector server/app.py -> docs/app.py — which rename detection would
    disguise as a docs-only diff — must build. The abnormal arms
    (foreign event, branch creation, unfetchable before-sha) must fail
    open to building.
    """
    bash = shutil.which("bash")
    git = shutil.which("git")
    assert bash and git, "the decide rehearsal needs bash and git on PATH"

    with tempfile.TemporaryDirectory() as tmp_name:
        tmp = Path(tmp_name)

        def run(cmd, cwd, env=None):
            proc = subprocess.run(
                cmd, cwd=str(cwd), env=env, text=True, capture_output=True
            )
            assert proc.returncode == 0, (
                f"{cmd} rc={proc.returncode}\n{proc.stdout}\n{proc.stderr}"
            )
            return proc

        # A local origin the script's before-sha fetch can really hit.
        origin = tmp / "origin"
        origin.mkdir()
        run([git, "init", "-q", "-b", "main"], origin)
        for key, value in (
            ("user.name", "rehearsal"),
            ("user.email", "rehearsal@invalid"),
            ("commit.gpgsign", "false"),
            ("core.autocrlf", "false"),
            ("uploadpack.allowAnySHA1InWant", "true"),
        ):
            run([git, "config", key, value], origin)

        def commit(message):
            run([git, "add", "-A"], origin)
            run([git, "commit", "-q", "--no-verify", "-m", message], origin)
            return run([git, "rev-parse", "HEAD"], origin).stdout.strip()

        (origin / "server").mkdir()
        (origin / "docs").mkdir()
        (origin / "server" / "app.py").write_text("print('v1')\n", encoding="utf-8")
        (origin / "README.md").write_text("v1\n", encoding="utf-8")
        (origin / "docs" / "guide.md").write_text("v1\n", encoding="utf-8")
        base = commit("base")
        (origin / "README.md").write_text("v2\n", encoding="utf-8")
        (origin / "docs" / "guide.md").write_text("v2\n", encoding="utf-8")
        docs_head = commit("docs-only")
        (origin / "server" / "app.py").write_text("print('v2')\n", encoding="utf-8")
        code_head = commit("code")
        run([git, "mv", "server/app.py", "docs/app.py"], origin)
        rename_head = commit("rename server/app.py -> docs/app.py")

        # The workflow-checkout equivalent, with the SHIPPED filter beside it.
        work = tmp / "work"
        run([git, "clone", "-q", str(origin), str(work)], tmp)
        (work / "scripts").mkdir()
        shutil.copyfile(
            Path(__file__).resolve().parent / "docs_noop_filter.py",
            work / "scripts" / "docs_noop_filter.py",
        )

        # Rename output at the unit level: --no-renames lists BOTH sides and
        # the shipped filter builds; detection-on lists only the destination
        # and would skip — the exact miss --no-renames exists to prevent.
        both_sides = run(
            [git, "diff", "--no-renames", "--name-only", code_head, rename_head],
            work,
        ).stdout.split()
        assert sorted(both_sides) == ["docs/app.py", "server/app.py"]
        assert dnf.decide(both_sides) == "build"
        destination_only = run(
            [git, "diff", "--find-renames", "--name-only", code_head, rename_head],
            work,
        ).stdout.split()
        assert destination_only == ["docs/app.py"]
        assert dnf.decide(destination_only) == "skip", (
            "if this stops skipping, the filter grew rename awareness and "
            "the --no-renames rationale needs revisiting"
        )

        # The script invokes python3; pin that name to THIS interpreter
        # (Windows has no python3, and the Store stub that answers to it
        # is a trap).
        bindir = tmp / "bin"
        bindir.mkdir()
        shim = bindir / "python3"
        shim.write_text(
            '#!/bin/sh\nexec "%s" "$@"\n'
            % str(Path(sys.executable)).replace("\\", "/"),
            encoding="utf-8",
            newline="\n",
        )
        shim.chmod(0o755)

        script = tmp / "decide.sh"
        script.write_text(decide_src, encoding="utf-8", newline="\n")
        out_path = tmp / "github-output.txt"

        def decide_run(event, before, head):
            out_path.write_text("", encoding="utf-8")
            env = dict(os.environ)
            env["PATH"] = str(bindir) + os.pathsep + env.get("PATH", "")
            env["EVENT_NAME"] = event
            env["BEFORE_SHA"] = before
            env["GITHUB_SHA"] = head
            env["GITHUB_OUTPUT"] = str(out_path).replace("\\", "/")
            proc = subprocess.run(
                [bash, str(script)], cwd=str(work), env=env,
                text=True, capture_output=True,
            )
            assert proc.returncode == 0, (
                f"decide script rc={proc.returncode}\n{proc.stdout}\n{proc.stderr}"
            )
            verdicts = re.findall(
                r"^build=(true|false)$",
                out_path.read_text(encoding="utf-8"),
                re.M,
            )
            assert len(verdicts) == 1, f"expected one verdict, got {verdicts!r}"
            return verdicts[0]

        assert decide_run("push", base, docs_head) == "false"
        assert decide_run("push", docs_head, code_head) == "true"
        assert decide_run("push", code_head, rename_head) == "true", (
            "the rename vector must build: with rename detection on, the "
            "diff arrives as docs/app.py alone and the train silently skips"
        )
        assert decide_run("workflow_dispatch", base, docs_head) == "true"
        assert decide_run("push", "0" * 40, docs_head) == "true"
        assert decide_run("push", "deadbeef" * 5, docs_head) == "true"


def test_build_platform_images_workflow_invariants() -> None:
    # Pytest entry point: the gate runner counts collected tests, and a bare
    # main() collects as zero.
    main()


if __name__ == "__main__":
    main()
