#!/usr/bin/env python3
"""Regression checks for the production image build workflow.

Mostly static text invariants, plus stronger bindings for the docs-noop
gate and its staging relay: their structure is asserted against the
PARSED workflow YAML, textual assertions run against comment-stripped
executable bash (a guard that drifts into a comment stops counting),
and the decide script is extracted from the parsed YAML and EXECUTED
against a real git history, including the rename vector that rename
detection would disguise as a docs-only diff.

The speculative dispatcher's step bash is additionally bound as
STATEMENT-shaped pins (whole-line assignments, command-position
invocations over logical lines, filename-occurrence arithmetic), and an
in-file decoy battery proves each pin catches the executable-decoy
vectors from sol-critic round 2 on PR #452 while ordinary maintenance
edits still pass.
"""

from pathlib import Path
import hashlib
import json
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


def _folded(value) -> str:
    return " ".join(str(value).split())


_PER_COMMIT_ARGS = ("LEAF_SOURCE_SHA", "AUTOFILL_SOLVER_REVISION")
# A RUN "consumes" a per-commit ARG only through an actual shell expansion
# ($NAME or ${NAME...}) that the shell would perform (sol-critic rounds 1-2
# on PR #458: a bare name, a comment mention, a single-quoted or
# backslash-escaped dollar, and exec-form RUN all reach the shell literal
# or bypass it entirely, so none of them consumes).
_PER_COMMIT_REF = re.compile(
    r"\$\{?(?:%s)\b" % "|".join(_PER_COMMIT_ARGS)
)
_PER_COMMIT_DECL = re.compile(
    r"ARG\s+(?:%s)\b" % "|".join(_PER_COMMIT_ARGS)
)


def _consumes_per_commit_arg(run_body: str) -> bool:
    """True only where the shell would actually expand a per-commit ARG.

    Exec-form RUN (a JSON array, with or without leading --flags such as
    --mount) invokes no shell, so nothing expands -- with ONE exception: an
    explicit shell invocation, `["/bin/sh", "-c", "<script>"]` (or /bin/bash),
    whose third element IS a shell script the shell expands exactly as a
    shell-form RUN would, so that element is parsed out and scanned. In shell
    form, a
    $NAME / ${NAME} reference expands except inside single quotes or with
    a backslash-escaped dollar; double quotes (including apostrophes
    nested inside them) do expand. Known scope boundary, named here on
    purpose: heredoc RUN bodies, $$ self-escapes, and BuildKit's own
    expansion inside --flag values are not modeled — no checked
    Dockerfile uses them, and a false trip fails loud, never silently
    green.
    """
    body = run_body.strip()
    # RUN flags (--mount/--network/--security[=value]) precede either
    # form; skip them before deciding exec vs shell.
    while body.startswith("--"):
        parts = body.split(None, 1)
        if len(parts) < 2:
            return False
        body = parts[1].lstrip()
    if body.startswith("["):
        # Exec form. Almost all exec-form RUNs invoke no shell and expand
        # nothing -- but `["/bin/sh", "-c", "<script>"]` and its /bin/bash
        # twin DO run a shell over their third element, so parse the argv and
        # fall through to the expansion scan on that script. Anything else in
        # exec form still consumes nothing.
        try:
            argv = json.loads(body)
        except (ValueError, TypeError):
            return False
        if (
            isinstance(argv, list)
            and len(argv) == 3
            and argv[0] in ("/bin/sh", "/bin/bash")
            and argv[1] == "-c"
            and isinstance(argv[2], str)
        ):
            body = argv[2]
        else:
            return False
    in_single = in_double = False
    i = 0
    while i < len(body):
        ch = body[i]
        if ch == "\\" and not in_single:
            i += 2
            continue
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif ch == "$" and not in_single and _PER_COMMIT_REF.match(body, i):
            return True
        i += 1
    return False


def _runs_after_per_commit_arg(dockerfile: str) -> list:
    """RUN instructions that follow a per-commit ARG in the same stage
    without consuming one.

    LEAF_SOURCE_SHA is a new commit sha on every build, and a changed
    in-scope ARG is a buildx cache miss for every instruction after it.
    Declared at the top of a stage it silently disables cross-commit layer
    caching for the whole file (run 30983842725: harness imported its
    predecessor cache and hit 0 layers; apt + npm ci reran on every merge).
    The contract: every RUN below the per-commit ARG declarations must
    reference one of them; cacheable RUNs stay above. ARG goes out of
    scope at the end of its stage, so tracking resets on FROM.
    """
    offending = []
    arg_seen = False
    logical = []
    buf = ""
    for raw in dockerfile.splitlines():
        line = raw.rstrip()
        # The Dockerfile parser removes comment lines even inside a continued
        # instruction, so a comment can never satisfy (or break) a RUN check.
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if not buf:
            buf = line
        else:
            buf += "\n" + line
        if buf.endswith("\\"):
            continue
        logical.append(buf)
        buf = ""
    for inst in logical:
        head = inst.split(None, 1)[0].upper()
        if head == "FROM":
            arg_seen = False
        elif head == "ARG" and _PER_COMMIT_DECL.match(inst):
            arg_seen = True
        elif head == "RUN" and arg_seen and not _consumes_per_commit_arg(
            # Comments cannot consume: the fold above drops Dockerfile
            # comment lines, and _executable_bash drops trailing shell
            # comments, so only an expansion in executable text counts.
            _executable_bash(inst[len("RUN"):])
        ):
            offending.append(inst.splitlines()[0])
    return offending


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
            echo "::notice::a same-repo gate proof already names tree $tree; warm legs with a fresh cache chain exit early (the gate's own probe still verifies the proof before any shard skip)"
          else
            echo "::notice::no same-repo gate proof names tree ${tree:-<unknown>}; the warm job runs beside the gate"
          fi
          exit 0
"""

# The warm chain-keeper script, pinned BYTE-EXACT for the same reason as
# HINT_SCRIPT above: no substring set proves the absence of a rewrite.
# Sol-critic round 1 on this PR showed the concrete defeat — inverting the
# probe polarity (`"$found" != "1"`) satisfied every targeted pin while
# recreating the exact chain-starvation defect this step exists to close
# (a missing predecessor or failed probe would SKIP warm instead of
# running it). Any edit to the chain script is a conscious edit of this
# copy too. Raw string: the script carries backslash continuations.
CHAIN_SCRIPT = r"""
          # Deliberately NOT errexit (set +e overrides the runner's `bash -e`
          # wrapper): every failure below must land on skip=false — warm
          # runs and republishes the chain; a redundant warm costs about a
          # runner-minute, while a false skip starves every later cache
          # consumer (warm, gated build, and speculative imports alike).
          set -uo pipefail
          set +e
          skip="false"
          if [[ "$GATE_REUSE_EXPECTED" == "true" && -n "$PREVIOUS_CACHE_TAG" ]]; then
            found="$(aws ecr batch-get-image \
              --repository-name "$IMAGE_NAME-buildcache" \
              --image-ids "imageTag=$PREVIOUS_CACHE_TAG" \
              --query 'length(images)' \
              --output text 2>/dev/null)"
            if [[ "$found" == "1" ]]; then
              skip="true"
            fi
          fi
          echo "skip=$skip" >> "$GITHUB_OUTPUT"
          if [[ "$skip" == "true" ]]; then
            echo "::notice::$IMAGE_NAME: gate reuse is expected and the predecessor cache $PREVIOUS_CACHE_TAG exists; this warm leg exits early"
          else
            echo "::notice::$IMAGE_NAME: warming (gate reuse expected: ${GATE_REUSE_EXPECTED:-false}; predecessor cache tag: ${PREVIOUS_CACHE_TAG:-<none>})"
          fi
          exit 0
""".lstrip("\n")

# The nearest-ancestor fallback's EXECUTABLE lines, one canonical copy for
# all three cache-selection lanes (warm, build, speculate — their comments
# differ, their code must not). Binds the walk's exact flags
# (--first-parent --skip=1 --max-count=15: the checkouts fetch 20 commits
# for precisely this), the single batch-get-image probe (deliberately not
# an ECR listing: neither workflow role is granted one, so a
# describe-images variant would AccessDenied and silently run cold —
# sol-critic round 1 on this PR), the nearest-first selection order, and
# the fail-open tail. Raw string: backslash continuations again.
FALLBACK_SCRIPT = r"""
          if [[ -z "$cache_from" ]]; then
            ids=()
            for sha in $(git rev-list --first-parent --skip=1 --max-count=15 HEAD 2>/dev/null); do
              short="$(git rev-parse --short "$sha" 2>/dev/null)" || continue
              if [[ "buildcache-$short" == "${CURRENT_CACHE_TAG:-}" ]]; then
                continue
              fi
              ids+=("imageTag=buildcache-$short")
            done
            if [[ "${#ids[@]}" -gt 0 ]]; then
              found_tags="$(aws ecr batch-get-image \
                --repository-name "$IMAGE_NAME-buildcache" \
                --image-ids "${ids[@]}" \
                --query 'images[].imageId.imageTag' \
                --output text 2>/dev/null | tr '\t\n' '  ' || true)"
              for id in "${ids[@]}"; do
                tag="${id#imageTag=}"
                if [[ " $found_tags " == *" $tag "* ]]; then
                  cache_from="type=registry,ref=$cache_repo:$tag"
                  echo "::notice::$IMAGE_NAME importing the nearest ancestor cache $tag"
                  break
                fi
              done
            fi
            if [[ -z "$cache_from" ]]; then
              echo "::notice::$IMAGE_NAME has no predecessor cache; building without cache input"
            fi
          fi
""".lstrip("\n")

# The warm/build cache-select script, pinned BYTE-EXACT (sol-critic
# round 3 on this PR): a contains-once count over the parsed run value
# still accepted the canonical fallback bytes wrapped dead inside
# `if false; then ... fi`. As HINT_SCRIPT's history records, no
# substring set proves the absence of a rewrite — only whole-script
# equality does. One copy serves both lanes (their byte-identity is
# separately asserted). Raw string: backslash continuations.
WARM_BUILD_CACHE_SCRIPT = r"""
          set -euo pipefail
          cache_repo="$ECR_REGISTRY/$IMAGE_NAME-buildcache"
          cache_from=""
          cache_to=""

          # THIS commit's cache first. The `warm` job builds the same tree in
          # parallel with the test gate and publishes exactly this tag, so by
          # the time the gate is green the layers already exist and this build
          # is a near-total cache hit rather than a cold rebuild. The fallback
          # keeps the previous behaviour whenever warm was skipped, failed, or
          # simply has not finished — it is never a hard dependency, so a warm
          # failure costs seconds, not a red build.
          current_warm="$(aws ecr batch-get-image \
            --repository-name "$IMAGE_NAME-buildcache" \
            --image-ids "imageTag=$CURRENT_CACHE_TAG" \
            --query 'length(images)' \
            --output text 2>/dev/null || true)"
          if [[ "$current_warm" == "1" ]]; then
            cache_from="type=registry,ref=$cache_repo:$CURRENT_CACHE_TAG"
            echo "::notice::$IMAGE_NAME reusing the warmed cache $CURRENT_CACHE_TAG"
          elif [[ -n "$PREVIOUS_CACHE_TAG" ]]; then
            found="$(aws ecr batch-get-image \
              --repository-name "$IMAGE_NAME-buildcache" \
              --image-ids "imageTag=$PREVIOUS_CACHE_TAG" \
              --query 'length(images)' \
              --output text 2>/dev/null || true)"
            if [[ "$found" == "1" ]]; then
              cache_from="type=registry,ref=$cache_repo:$PREVIOUS_CACHE_TAG"
            fi
          fi

          # Nearest-ancestor fallback (chain keeper, 2026-08-05): under the
          # adoption path a fully green merge run skips both cache writers,
          # so the exact predecessor tag can be several commits stale. The
          # cache keys are commit-stable below the per-commit ARGs (#458),
          # so a near-ancestor cache still hits the heavy toolchain layers.
          # Candidates come from the checkout's own first-parent history
          # (fetch-depth: 20 above exists for exactly this walk) and are
          # probed in ONE batch-get-image call — deliberately not an ECR
          # listing, which neither workflow role is granted
          # (leaf-automation-aws-terraform pins both to BatchGetImage plus
          # push actions). Every failure lands on the uncached build.
          if [[ -z "$cache_from" ]]; then
            ids=()
            for sha in $(git rev-list --first-parent --skip=1 --max-count=15 HEAD 2>/dev/null); do
              short="$(git rev-parse --short "$sha" 2>/dev/null)" || continue
              if [[ "buildcache-$short" == "${CURRENT_CACHE_TAG:-}" ]]; then
                continue
              fi
              ids+=("imageTag=buildcache-$short")
            done
            if [[ "${#ids[@]}" -gt 0 ]]; then
              found_tags="$(aws ecr batch-get-image \
                --repository-name "$IMAGE_NAME-buildcache" \
                --image-ids "${ids[@]}" \
                --query 'images[].imageId.imageTag' \
                --output text 2>/dev/null | tr '\t\n' '  ' || true)"
              for id in "${ids[@]}"; do
                tag="${id#imageTag=}"
                if [[ " $found_tags " == *" $tag "* ]]; then
                  cache_from="type=registry,ref=$cache_repo:$tag"
                  echo "::notice::$IMAGE_NAME importing the nearest ancestor cache $tag"
                  break
                fi
              done
            fi
            if [[ -z "$cache_from" ]]; then
              echo "::notice::$IMAGE_NAME has no predecessor cache; building without cache input"
            fi
          fi

          if current_exists="$(aws ecr batch-get-image \
              --repository-name "$IMAGE_NAME-buildcache" \
              --image-ids "imageTag=$CURRENT_CACHE_TAG" \
              --query 'length(images)' \
              --output text 2>/dev/null)"; then
            if [[ "$current_exists" == "1" ]]; then
              echo "::notice::$IMAGE_NAME cache $CURRENT_CACHE_TAG already exists; immutable tag will not be overwritten"
            else
              # ignore-error: the existence check above cannot exclude the
              # other writer of this immutable tag finishing in between (warm
              # and the gated build race by design, and ECR refuses the second
              # manifest with ImageTagAlreadyExistsException). Losing that race
              # must cost only the cache export, never the build.
              cache_to="type=registry,ref=$cache_repo:$CURRENT_CACHE_TAG,mode=max,image-manifest=true,oci-mediatypes=true,ignore-error=true"
            fi
          else
            echo "::warning::Unable to verify $CURRENT_CACHE_TAG; skipping cache publication"
          fi

          echo "from=$cache_from" >> "$GITHUB_OUTPUT"
          echo "to=$cache_to" >> "$GITHUB_OUTPUT"
""".lstrip("\n")

# The speculative lane's cache-select script, pinned BYTE-EXACT for the
# same reason. Import-only by construction: the canonical copy itself
# is the proof that no cache_to/export path exists in this lane.
# Raw string: backslash continuations.
SPECULATE_CACHE_SCRIPT = r"""
          # Import the cache of the main tip this preview merged onto and
          # export nothing: speculative trees churn too fast to be worth
          # polluting the immutable buildcache-* namespace, and no export
          # means no cache_to race to tolerate.
          set -euo pipefail
          cache_repo="$ECR_REGISTRY/$IMAGE_NAME-buildcache"
          cache_from=""
          if [[ -n "$PREVIOUS_CACHE_TAG" ]]; then
            found="$(aws ecr batch-get-image \
              --repository-name "$IMAGE_NAME-buildcache" \
              --image-ids "imageTag=$PREVIOUS_CACHE_TAG" \
              --query 'length(images)' \
              --output text 2>/dev/null || true)"
            if [[ "$found" == "1" ]]; then
              cache_from="type=registry,ref=$cache_repo:$PREVIOUS_CACHE_TAG"
            fi
          fi
          # Nearest-ancestor fallback (chain keeper, 2026-08-05): the
          # merged-onto main tip's exact tag can be several commits stale
          # when adopted merges published nothing, and the commit-stable
          # keys (#458) make a near-ancestor cache nearly as good. The
          # preview's first-parent chain IS main's history, so the same
          # walk warm/build use works here (fetch-depth: 20 above), probed
          # in ONE batch-get-image call — deliberately not an ECR listing,
          # which neither workflow role is granted. IMPORT stays the only
          # direction in this lane; every failure lands on the uncached
          # build.
          if [[ -z "$cache_from" ]]; then
            ids=()
            for sha in $(git rev-list --first-parent --skip=1 --max-count=15 HEAD 2>/dev/null); do
              short="$(git rev-parse --short "$sha" 2>/dev/null)" || continue
              if [[ "buildcache-$short" == "${CURRENT_CACHE_TAG:-}" ]]; then
                continue
              fi
              ids+=("imageTag=buildcache-$short")
            done
            if [[ "${#ids[@]}" -gt 0 ]]; then
              found_tags="$(aws ecr batch-get-image \
                --repository-name "$IMAGE_NAME-buildcache" \
                --image-ids "${ids[@]}" \
                --query 'images[].imageId.imageTag' \
                --output text 2>/dev/null | tr '\t\n' '  ' || true)"
              for id in "${ids[@]}"; do
                tag="${id#imageTag=}"
                if [[ " $found_tags " == *" $tag "* ]]; then
                  cache_from="type=registry,ref=$cache_repo:$tag"
                  echo "::notice::$IMAGE_NAME importing the nearest ancestor cache $tag"
                  break
                fi
              done
            fi
            if [[ -z "$cache_from" ]]; then
              echo "::notice::$IMAGE_NAME has no predecessor cache; building without cache input"
            fi
          fi
          echo "from=$cache_from" >> "$GITHUB_OUTPUT"
""".lstrip("\n")


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
        "${{ env.ECR_REGISTRY }}/${{ env.IMAGE_NAME }}:${{ env.IMAGE_TAG }}"
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
    # on the release role. Since the 2026-08-05 adopt/verify fold, adopt
    # also writes the ADOPTED path's supply set with that same credential:
    # its grant set is exactly the union of the two pre-fold jobs
    # (id-token:write + contents:read from both, actions:read from adopt's
    # artifact listing), and the verify half adds only ECR READS the
    # release role already had — no job gained a permission or a role it
    # did not hold before the fold. Each provably needs registry access;
    # the role split is pinned by the role-to-assume assertions below.
    # Raising this number should mean a new job that provably needs
    # registry access, not a convenience.
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
        # In warm the pair is additionally gated on the chain-keeper skip
        # (2026-08-05): a leg that exits early never fetches the deploy
        # key — the same posture speculate's existence-skip holds.
        expected_solver_condition = (
            "matrix.image == 'canonical-worker' && "
            "steps.chain.outputs.skip != 'true'"
            if lane == "warm"
            else (
                "matrix.image == 'canonical-worker' && "
                "steps.resume.outputs.skip != 'true'"
            )
        )
        for step in (require_step, checkout_step):
            conditions = [
                _value_of(l) for l in step.splitlines() if _key_of(l) == "if"
            ]
            assert conditions == [expected_solver_condition], (
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
    # Since the 2026-08-05 adopt/verify fold the verify JOB owns only the
    # full-build path; the adopted path's supply set is written inside
    # adopt (byte-identity pinned below). The two writers are mutually
    # exclusive: build runs only when adopted != 'true', and this guard
    # requires build's success.
    verify_guard = (
        "    if: >-\n"
        "      ${{ !cancelled() && !inputs.promote && !inputs.speculative &&\n"
        "          needs.prepare.result == 'success' &&\n"
        "          needs.build.result == 'success' }}\n"
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
    # The handoff checkout persists no credentials and the repository is
    # private, so the pre-verify-staging main fetch must carry the scoped
    # auth-header idiom on the workflow's own token. A bare
    # `git fetch --no-tags origin main` fails on auth the first time a
    # promote is dispatched (it has never fired: no promote has run), and
    # the job-level cross-repo PAT stays out of the git header.
    assert "git fetch --no-tags origin main" not in handoff_body
    assert "http.https://github.com/.extraheader" in handoff_body
    assert "fetch --no-tags origin main" in handoff_body
    assert "x-access-token:$MAIN_FETCH_TOKEN" in handoff_body
    assert "MAIN_FETCH_TOKEN: ${{ github.token }}" in handoff_body
    assert "--main-ref origin/main" in handoff_body
    assert "production-handoff-candidate-" in handoff_body
    assert "-attempt-${{ github.run_attempt }}" in handoff_body
    assert "gh workflow run deploy-service-production.yml" not in text
    assert "aws ecr put-image" not in handoff_body
    assert "docker/build-push-action" not in handoff_body
    # test carries the promote guard, the speculative guard (a speculative
    # dispatch must not run the gate: its PR's standalone gate run mints
    # the proof), AND the docs-noop build gate: since the 2026-08-05 fold
    # prepare SUCCEEDS on a docs-only push (it publishes the marker), so
    # without the build check test's implicit success() would run the full
    # gate on every docs push. build and verify moved to compound
    # whole-pinned guards (see build_guard/verify_guard), and warm's guard
    # is compound — promote, speculative, the docs gate, and prepare's
    # gate-reuse hint — pinned by exact parsed value below rather than
    # counted here.
    assert text.count("if: ${{ !inputs.promote }}") == 0
    assert text.count("if: ${{ !inputs.promote && !inputs.speculative }}") == 0
    # TWO carriers of this exact guard: the test job and, since the
    # 2026-08-05 chain-keeper change, the warm job — warm always schedules
    # and each leg decides IN-JOB whether to exit early (prepare holds no
    # AWS credential, so only a warm leg can probe ECR for the predecessor
    # tag). The docs-gate clause stays polar CLOSED on purpose: == 'true'
    # skips warm on an empty build output, because on a docs-only push warm
    # would otherwise burn five runners on a tree nothing ships.
    assert text.count(
        "if: ${{ !inputs.promote && !inputs.speculative && "
        "needs.prepare.outputs.build == 'true' }}"
    ) == 2
    warm_job_header = warm_block[: warm_block.index("    steps:")]
    warm_guards = [
        _value_of(l) for l in warm_job_header.splitlines() if _key_of(l) == "if"
    ]
    assert warm_guards == [
        "${{ !inputs.promote && !inputs.speculative && "
        "needs.prepare.outputs.build == 'true' }}"
    ], warm_guards
    # The hint is an ECONOMIC signal with a deliberately narrow blast
    # radius: it may feed warm's per-leg chain-keeper decision and NOTHING
    # else. Skipping the GATE stays the sole business of the called gate's
    # verified reuse probe and fan-in re-verification, and the build job's
    # own tree binding is what refuses an unproven push — a hint defect
    # must never widen past a colder build. Exactly two occurrences: the
    # prepare output that mints it and the chain step env that consumes it.
    # The warm job HEADER no longer reads it: a header-level skip cannot
    # see ECR, and skipping on the hint alone starved the cache chain on
    # every adopted merge (nothing published a buildcache tag across the
    # four adopted merges through run 31006482947).
    assert text.count("gate_reuse_expected") == 2
    prepare_block = text.split("\n  prepare:\n", 1)[1].split("\n  test:\n", 1)[0]
    assert prepare_block.count("gate_reuse_expected") == 1
    assert warm_job_header.count("gate_reuse_expected") == 0
    assert warm_block.count("gate_reuse_expected") == 1
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
        ("name", "Decide whether this push touched any build input"),
        ("name", "Write the docs-noop marker for the staging relay"),
        ("uses", "actions/upload-artifact@v4"),
        ("name", "Require exact source to be reviewed"),
        ("name", "Resolve canonical solver provenance"),
        ("name", "Validate image workflow invariants"),
        ("name", "Derive immutable image tag"),
        ("name", "Probe for an expected gate reuse (warm-skip hint)"),
    ], prepare_step_heads
    # The folded docs-noop gate's step conditions, pinned per step by
    # parsed value (Codex trap, 2026-08-05 fold: folded job-level if:
    # logic can silently change skip/failure semantics). The checkout and
    # the decide step run unconditionally; the marker pair runs exactly on
    # the docs-only verdict; every later step runs exactly on its
    # negation, so a docs-only push ends prepare green with only the
    # marker published and every downstream job gated off.
    step_ifs = []
    for n, start in enumerate(step_starts):
        end = step_starts[n + 1] if n + 1 < len(step_starts) else len(annotated)
        seg_structural = [l for l, s in annotated[start:end] if s]
        step_ifs.append(
            [_value_of(l) for l in seg_structural if _key_of(l) == "if"]
        )
    docs_gate = "steps.decide.outputs.build == 'true'"
    docs_skip = "steps.decide.outputs.build == 'false'"
    assert step_ifs == [
        [], [], [docs_skip], [docs_skip],
        [docs_gate], [docs_gate], [docs_gate], [docs_gate], [docs_gate],
    ], step_ifs
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
    assert hint_keys == ["name", "id", "if", "env", "GH_TOKEN", "run"], hint_keys
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
    # And the step must actually RUN, with the workflow token: a foreign
    # if: on the step (or an emptied GH_TOKEN) silently restores permanent
    # expected=false - warm runs on every reuse path again with nothing
    # red anywhere (round 3, finding 2). Since the 2026-08-05 fold the
    # step carries exactly ONE permitted condition — the docs-noop gate,
    # already pinned per step above — and no other: when build == 'false'
    # the warm job itself is gated off, so this condition can never starve
    # a live warm; any OTHER condition could. Pinned by exact value.
    hint_conditions = [
        _value_of(l) for l in hint_structural if _key_of(l) == "if"
    ]
    assert hint_conditions == [docs_gate], hint_conditions
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
        # Cross-commit layer-cache contract: cacheable RUNs stay above the
        # per-commit ARG declarations (see _runs_after_per_commit_arg).
        offending = _runs_after_per_commit_arg(dockerfile)
        assert offending == [], (
            f"Dockerfile.{image}: RUN below a per-commit ARG without "
            f"consuming it re-runs on every merge: {offending}"
        )

    # The consumption scanner is itself pinned: every evasion class from
    # the PR #458 reviews (bare name, comment mention, single-quoted or
    # backslash-escaped dollar, exec-form RUN) must offend, and the real
    # expansion forms the five Dockerfiles use must stay clean. A checker
    # edit that silently widens or narrows the rule fails here first.
    probe_header = "FROM x AS y\nARG LEAF_SOURCE_SHA=unknown\n"
    for probe_run, must_offend in (
        ("RUN echo LEAF_SOURCE_SHA", True),
        ("RUN apt-get update  # uses $LEAF_SOURCE_SHA", True),
        ("RUN echo '$LEAF_SOURCE_SHA'", True),
        ("RUN echo \\$LEAF_SOURCE_SHA", True),
        ('RUN ["echo", "$LEAF_SOURCE_SHA"]', True),
        ('RUN --mount=type=cache,target=/tmp ["echo", "$LEAF_SOURCE_SHA"]', True),
        ('RUN --network=none printf "%s" "$LEAF_SOURCE_SHA"', False),
        ('RUN printf "%s" "$LEAF_SOURCE_SHA" > /tmp/sha', False),
        ("RUN test -n $LEAF_SOURCE_SHA", False),
        ("RUN seal ${LEAF_SOURCE_SHA}", False),
        ("RUN python -c \"attest('${LEAF_SOURCE_SHA}')\"", False),
        # The one exec form that DOES run a shell: `["/bin/sh","-c","<script>"]`
        # (and /bin/bash). Its third element is scanned like a shell-form body,
        # so it consumes when the script references the ARG and offends when it
        # does not (canonical-worker's survival guard is the real user of this).
        # `-lc` is not `-c`, so it is not treated as a shell invocation.
        (
            'RUN ["/bin/sh", "-c", "test -f /a && test -n \\"${LEAF_SOURCE_SHA}\\""]',
            False,
        ),
        ('RUN ["/bin/sh", "-c", "echo no-arg-here"]', True),
        ('RUN ["/bin/bash", "-c", "seal ${LEAF_SOURCE_SHA}"]', False),
        ('RUN ["/bin/sh", "-lc", "echo ${LEAF_SOURCE_SHA}"]', True),
    ):
        offended = bool(_runs_after_per_commit_arg(probe_header + probe_run + "\n"))
        assert offended == must_offend, (
            f"consumption scanner drift on: {probe_run!r} "
            f"(offended={offended}, expected {must_offend})"
        )

    # Dockerfile.instant-execution is built by the sibling staging workflow
    # (build-instant-execution-image.yml), not this one, but it consumes the
    # same per-commit LEAF_SOURCE_SHA and so shares the layer-cache contract.
    # This file owns the scanner and is the PR-gated one of the two
    # (run-all-gates), so the invariant binds the staging image here. Its one
    # RUN below the ARG is the sha-format gate, which consumes it.
    instant = (ROOT / "deploy" / "Dockerfile.instant-execution").read_text(
        encoding="utf-8"
    )
    assert "ARG LEAF_SOURCE_SHA" in instant
    assert "LEAF_SOURCE_SHA=${LEAF_SOURCE_SHA}" in instant
    offending = _runs_after_per_commit_arg(instant)
    assert offending == [], (
        "Dockerfile.instant-execution: RUN below a per-commit ARG without "
        f"consuming it re-runs on every merge: {offending}"
    )

    # The broker's toolchain block (apt libstdc++6/git + node binary + e2b
    # node_modules) reads nothing from the source tree, so it must sit ABOVE
    # the per-commit source COPYs: an instruction below them re-executes on
    # every merge regardless of the ARG placement the scanner enforces
    # (run 31006198764: apt 6.1s + node 0.7s re-ran after the source COPYs).
    # Comment lines are stripped first so a mention in prose can neither
    # satisfy nor break the ordering pin.
    broker_exec = "\n".join(
        line
        for line in (ROOT / "deploy" / "Dockerfile.broker")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    )
    first_source_copy = broker_exec.index("COPY server/  /app/server/")
    for toolchain_marker in (
        "apt-get install -y --no-install-recommends libstdc++6 git",
        "COPY --from=node:20-slim /usr/local/bin/node",
        "COPY --from=e2bdeps /helper/node_modules",
        "COPY harness/scripts/e2b-tool-exec.mjs",
    ):
        assert broker_exec.index(toolchain_marker) < first_source_copy, (
            f"Dockerfile.broker: {toolchain_marker!r} is below the per-commit "
            "source COPYs and re-runs on every merge"
        )

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

    # The chain-keeper step (2026-08-05): warm always schedules; each leg
    # exits early ONLY when prepare's reuse hint fired AND that image's
    # exact predecessor buildcache tag exists in its own cache repository.
    # Pinned: the decide step is unconditional (a foreign if: could leave
    # a stale/empty skip output gating the build steps), fail-open (set
    # +e, skip="false" initialiser, exit 0 — every probe failure warms),
    # reads the hint through its own step env, and its skip output gates
    # warm's OWN later steps and nothing else — a chain defect can cost a
    # redundant warm or a colder gated build, never redden a run or reach
    # another job.
    chain_steps = [
        s for s in warm_steps
        if s.startswith("name: Decide whether the cache chain needs this warm build")
    ]
    assert len(chain_steps) == 1, "warm holds exactly one chain-keeper step"
    chain_step = chain_steps[0]
    assert [
        _value_of(l) for l in chain_step.splitlines() if _key_of(l) == "id"
    ] == ["chain"]
    assert "if" not in _keys_in(chain_step), (
        "the chain decide step itself must always run")
    assert [
        _value_of(l) for l in chain_step.splitlines()
        if _key_of(l) == "GATE_REUSE_EXPECTED"
    ] == ["${{ needs.prepare.outputs.gate_reuse_expected }}"]
    chain_code = "\n".join(
        _comment_cut(l) for l in chain_step.splitlines()
        if not l.strip().startswith("#")
    )
    assert "set +e" in chain_code
    assert 'skip="false"' in chain_code
    assert chain_code.rstrip().endswith("exit 0"), (
        "the chain decide step must degrade internally, never redden warm")
    # BYTE-EXACT script pin (sol-critic round 1 on this PR): the targeted
    # substring pins above allowed a polarity inversion ("$found" != "1")
    # that skipped warm exactly when the chain was starving — the defect
    # this step exists to close. One plain run header, nothing structural
    # after it, and the content equals the canonical copy.
    chain_lines = chain_step.splitlines()
    chain_run_keys = [l for l in chain_lines if _key_of(l) == "run"]
    assert chain_run_keys == ["        run: |"], chain_run_keys
    chain_run_at = chain_lines.index("        run: |")
    chain_tail = chain_lines[chain_run_at + 1:]
    for line in chain_tail:
        assert not line.strip() or line.startswith("          "), (
            "the run block must end the chain step: %r" % line)
    assert "\n".join(chain_tail).rstrip("\n") == CHAIN_SCRIPT.rstrip("\n"), (
        "the chain-keeper script must equal its canonical pinned copy in "
        "this file; edit both together, consciously")
    # Every warm step after the decide carries exactly the skip condition
    # (the solver pair additionally carries the canonical-worker arm,
    # pinned with build's above), and the output reaches nothing outside
    # the warm job.
    chain_skip = "steps.chain.outputs.skip != 'true'"
    for step_prefix in (
        "name: Set up Docker Buildx",
        "name: Select immutable cache references",
    ):
        gated = [s for s in warm_steps if s.startswith(step_prefix)]
        assert len(gated) == 1, step_prefix
        assert [
            _value_of(l) for l in gated[0].splitlines() if _key_of(l) == "if"
        ] == [chain_skip], step_prefix
    assert [
        _value_of(l) for l in warm_build_step.splitlines() if _key_of(l) == "if"
    ] == [chain_skip]
    assert text.count("steps.chain.") == warm_block.count("steps.chain."), (
        "the chain-keeper's skip output must gate warm's own steps and "
        "nothing else")

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
        # build: the exact release-output resume probe, the current-warm
        # probe, the predecessor probe, the
        # nearest-ancestor fallback batch probe (chain keeper, 2026-08-05;
        # batch-get-image, never an ECR listing — the roles are not granted
        # one), and the existence check before export. warm adds the
        # chain-keeper decide step's predecessor probe.
        probes = [l for l in live if "--repository-name" in l]
        assert len(probes) == {"warm": 5, "build": 5}[lane], (lane, probes)
        release_probes = [
            p for p in probes
            if re.search(r'--repository-name "\$IMAGE_NAME"(?!-)', p)
        ]
        assert len(release_probes) == {"warm": 0, "build": 1}[lane], (
            lane, release_probes)
        for probe in probes:
            if probe not in release_probes:
                assert '--repository-name "$IMAGE_NAME-buildcache"' in probe, (
                    lane, probe)
        # The fallback's executable lines equal the one canonical copy
        # (sol-critic round 1 on this PR: probe counts alone bound neither
        # the walk's flags nor the batch probe, so dropping fetch-depth or
        # swapping in an ungranted listing call passed unnoticed).
        assert "\n".join(live).count(FALLBACK_SCRIPT.rstrip("\n")) == 1, (
            "the %s lane must carry exactly the canonical nearest-ancestor "
            "fallback; edit FALLBACK_SCRIPT and every lane together, "
            "consciously" % lane)
        assert len(re.findall(
            r'--repository-name "\$IMAGE_NAME"(?!-)', "\n".join(live)
        )) == {"warm": 0, "build": 1}[lane], lane
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
    # One release-repository existence probe; the predecessor import probe
    # and the nearest-ancestor fallback batch probe both read the cache
    # repository.
    assert len(speculate_probes) == 3
    assert sorted(
        '"$IMAGE_NAME-buildcache"' in p for p in speculate_probes
    ) == [False, True, True], speculate_probes
    # Same canonical fallback bytes as warm/build (their comments differ,
    # their code must not), and no lane anywhere may reach for an ECR
    # listing: neither workflow role is granted one, so a describe-images
    # variant would AccessDenied and silently run every build cold
    # (sol-critic round 1 on this PR).
    assert "\n".join(speculate_live).count(FALLBACK_SCRIPT.rstrip("\n")) == 1, (
        "the speculative lane must carry exactly the canonical "
        "nearest-ancestor fallback")
    assert "describe-images" not in text
    assert "list-images" not in text
    tag_values = [
        _value_of(l) for l in build_block.splitlines() if _key_of(l) == "tags"
    ]
    assert tag_values == ["|"], (
        "the release push tags are one block scalar: release tag, baked "
        "identity witness, and the app-only src- stamp")
    tags_lines = build_block[build_block.index("tags: |"):].splitlines()[1:5]
    assert tags_lines[0].strip() == (
        "${{ env.ECR_REGISTRY }}/${{ env.IMAGE_NAME }}:${{ env.IMAGE_TAG }}"
    ), "the release push tag targets the release repository, never a cache"
    assert tags_lines[1].strip() == (
        "${{ env.ECR_REGISTRY }}/${{ env.IMAGE_NAME }}:sha-${{ "
        "needs.prepare.outputs.source_sha }}"
    ), "every full-build digest carries the sha-<40> blue/green identity witness"
    assert tags_lines[2].strip() == (
        "${{ matrix.image == 'canonical-worker' && "
        "format('{0}/{1}:sha-{2}-solver-{3}', env.ECR_REGISTRY, "
        "env.IMAGE_NAME, needs.prepare.outputs.source_sha, "
        "needs.prepare.outputs.solver_revision) || '' }}"
    ), "canonical-worker also carries the compound app and solver deploy tag"
    assert tags_lines[3].strip() == (
        "${{ matrix.image == 'app' && startsWith(env.IMAGE_TAG, 'prod-') && "
        "format('{0}/{1}:src-{2}', env.ECR_REGISTRY, env.IMAGE_NAME, "
        "needs.prepare.outputs.source_sha) || '' }}"
    ), "the src- identity stamp is app-only and rides release prod-* builds only"
    assert "-buildcache:${{" not in text

    # ------------------------------------------------------------------ #
    # The src-<full-commit> identity namespace: a CI build-identity stamp
    # for the staging migration source diff, NEVER a release or review
    # tag. It is minted in exactly two places (the build push's
    # conditional second tag above, and adopt's post-verify stamp), and
    # nowhere else — the warm, speculate, and handoff arms never touch it.
    assert text.count("src-{2}") == 1
    assert text.count('src_tag="src-$SOURCE_SHA"') == 1
    # Adopt stamps only AFTER every release alias re-verified, only with a
    # full-commit identity, and only best-effort: a failed stamp warns and
    # costs the fast path, it never reddens an adoption that already
    # committed.
    adopt_stamp = text.index('src_tag="src-$SOURCE_SHA"')
    assert text.index("re-verification failed after aliasing") < adopt_stamp
    stamp_block = text[
        text.index('if [[ "$SOURCE_SHA" =~ ^[0-9a-f]{40}$ ]]; then'):
        text.index('spec_run_id="$(jq -r')
    ]
    assert 'src_tag="src-$SOURCE_SHA"' in stamp_block
    assert "exit" not in stamp_block, (
        "the src- stamp is best-effort; it must never exit the decide step")
    assert "::warning::adopt: could not stamp leaf-platform-app:$src_tag" in text
    assert "already names a different digest" in stamp_block
    assert 'SOURCE_SHA: ${{ needs.prepare.outputs.source_sha }}' in text

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
    # The tree binding stays unconditional. The push has one permitted
    # condition: the exact-output resume guard may skip a complete immutable
    # image. Any other condition could bypass the gate (sol-critic round 6:
    # `if: ${{ false }}` on the binding step left the push enabled while
    # every literal stayed green).
    for step in re.split(r"\n      - ", build_block):
        conditional = "if" in _keys_in(step)
        if "Require the green gate verdict to bind" in step:
            assert not conditional, "the tree binding must not be conditional"
        if "uses: docker/build-push-action" in step:
            conditions = [
                _value_of(l) for l in step.splitlines() if _key_of(l) == "if"
            ]
            assert conditions == ["steps.resume.outputs.skip != 'true'"], (
                "the image push may only be skipped by the exact-output guard"
            )
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
    # group (they are dispatched on the main ref). Cancellation is
    # newest-wins for speculative runs AND for main push runs (merge-burst
    # coalescing: the latest-main-only relay discards a superseded build's
    # images anyway, so completing them only burns runners and queues the
    # winning run behind them). Non-push dispatches (promote, draft-PR
    # builds) still cancel nothing.
    assert (
        "group: build-platform-images-${{ inputs.speculative && "
        "format('speculative-pr-{0}', inputs.speculative_pr_number) || github.ref }}"
    ) in text
    assert (
        "cancel-in-progress: "
        "${{ inputs.speculative || github.event_name == 'push' }}"
    ) in text

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
        "${{ !inputs.promote && github.event_name == 'push' && "
        "needs.prepare.outputs.build == 'true' }}"
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
    # Exactly two writers: the release-tag alias loop and the post-verify app
    # src- identity stamp. An adopted digest keeps its spec-* baked-identity
    # witness. It must never receive sha-* because that namespace asserts the
    # tag's commit was baked into the image, which is false for adoption.
    assert adopt_block.count("aws ecr put-image") == 2
    assert 'deploy_tag="sha-$SOURCE_SHA"' not in adopt_block
    assert 'deploy_tag="sha-$SOURCE_SHA-solver-$SOLVER_SHA"' not in adopt_block
    assert 're-verification failed for exact deploy tag' not in adopt_block
    assert adopt_block.index('--image-tag "$PROD_TAG"') < adopt_block.index(
        '--image-tag "$src_tag"'
    ), "the src- stamp never precedes the release alias loop"
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

    assert wf_jobs["speculate"]["if"] == (
        "${{ github.event_name == 'workflow_dispatch' && inputs.speculative }}"
    )
    assert wf_jobs["adopt"]["if"] == (
        "${{ !inputs.promote && github.event_name == 'push' && "
        "needs.prepare.outputs.build == 'true' }}"
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
        "needs.build.result == 'success' }}"
    )
    assert wf_jobs["build"]["needs"] == ["prepare", "test", "adopt"]
    assert wf_jobs["verify"]["needs"] == ["prepare", "adopt", "build"]
    assert wf_jobs["adopt"]["needs"] == ["prepare", "test"]

    # A cancelled full-build matrix may be resumed at the image boundary,
    # but never at an individual tag boundary. This guard is parsed from the
    # build job itself so a decoy in another job cannot satisfy the contract.
    build_steps = wf_jobs["build"]["steps"]
    resume_steps = [
        s for s in build_steps
        if s.get("name") == "Inspect exact full-build outputs"
    ]
    assert len(resume_steps) == 1
    resume_step = resume_steps[0]
    assert resume_step["id"] == "resume"
    assert resume_step["env"] == {
        "SOURCE_SHA": "${{ needs.prepare.outputs.source_sha }}",
        "SOLVER_REVISION": "${{ needs.prepare.outputs.solver_revision }}",
    }
    resume_run = _executable_bash(resume_step["run"])
    assert resume_run.count("aws ecr batch-get-image") == 1
    assert "describe-images" not in resume_run
    assert "list-images" not in resume_run
    assert 'required_tags=("$IMAGE_TAG")' in resume_run
    assert 'add_required_tag "sha-$SOURCE_SHA"' in resume_run
    assert 'add_required_tag "sha-$SOURCE_SHA-solver-$SOLVER_REVISION"' in resume_run
    assert 'add_required_tag "src-$SOURCE_SHA"' in resume_run
    assert '--repository-name "$IMAGE_NAME"' in resume_run
    assert '--image-ids "${image_ids[@]}"' in resume_run
    assert 'select(.failureCode != "ImageNotFound")' in resume_run
    assert 'if [ "$present" = "0" ]; then' in resume_run
    assert 'echo "skip=false" >> "$GITHUB_OUTPUT"' in resume_run
    assert 'if [ "$present" != "${#required_tags[@]}" ]; then' in resume_run
    assert "refusing an immutable partial overwrite" in resume_run
    # Load-bearing: without this exact accumulator a complete response with
    # different tag digests leaves an empty array. Bash then counts the one
    # blank printf line as one unique digest and can incorrectly skip.
    def require_digest_accumulator(script: str) -> None:
        assert script.count('digests+=("$digest")') == 1

    require_digest_accumulator(resume_run)
    assert "sort -u | wc -l" in resume_run
    assert 'if [ "$unique_digests" != "1" ]; then' in resume_run
    assert "resolve to different digests" in resume_run
    assert 'echo "skip=true" >> "$GITHUB_OUTPUT"' in resume_run

    # Mutation guard for the accumulator itself. Keep this next to the
    # executable contract so removing the append cannot leave a green suite
    # while all of the later comparison tokens remain present.
    without_digest_accumulator = resume_run.replace(
        'digests+=("$digest")', "", 1
    )
    try:
        require_digest_accumulator(without_digest_accumulator)
    except AssertionError:
        pass
    else:
        raise AssertionError("digest-accumulator mutation escaped the contract")
    for forbidden_write in (
        "aws ecr put-image", "docker push", "docker build", "buildx build",
    ):
        assert forbidden_write not in resume_run

    step_names = [str(s.get("name", s.get("uses", ""))) for s in build_steps]
    resume_at = step_names.index("Inspect exact full-build outputs")
    assert step_names.index("Login to ECR") < resume_at
    assert resume_at < step_names.index(
        "Require the read-only canonical solver deploy key")
    assert resume_at < step_names.index("Select immutable cache references")
    build_push_at = next(
        i for i, s in enumerate(build_steps)
        if str(s.get("name", "")).startswith("Build and push")
    )
    assert resume_at < build_push_at
    cache_step = next(
        s for s in build_steps
        if s.get("name") == "Select immutable cache references"
    )
    assert cache_step["if"] == "steps.resume.outputs.skip != 'true'"
    assert build_steps[build_push_at]["if"] == (
        "steps.resume.outputs.skip != 'true'"
    )

    # src- identity stamp, bound to PARSED EXECUTABLE values (the raw-text
    # pins earlier are convenience; these are the load-bearing ones — a
    # commented decoy or a value smuggled into a name: scalar fails here).
    # The build push's tags are the parsed with.tags value, line-exact.
    src_push_steps = [
        s
        for s in wf_jobs["build"]["steps"]
        if str(s.get("name", "")).startswith("Build and push")
    ]
    assert len(src_push_steps) == 1
    src_tag_lines = [
        l.strip()
        for l in src_push_steps[0]["with"]["tags"].splitlines()
        if l.strip()
    ]
    assert src_tag_lines == [
        "${{ env.ECR_REGISTRY }}/${{ env.IMAGE_NAME }}:${{ env.IMAGE_TAG }}",
        "${{ env.ECR_REGISTRY }}/${{ env.IMAGE_NAME }}:sha-${{ "
        "needs.prepare.outputs.source_sha }}",
        "${{ matrix.image == 'canonical-worker' && "
        "format('{0}/{1}:sha-{2}-solver-{3}', env.ECR_REGISTRY, "
        "env.IMAGE_NAME, needs.prepare.outputs.source_sha, "
        "needs.prepare.outputs.solver_revision) || '' }}",
        "${{ matrix.image == 'app' && startsWith(env.IMAGE_TAG, 'prod-') && "
        "format('{0}/{1}:src-{2}', env.ECR_REGISTRY, env.IMAGE_NAME, "
        "needs.prepare.outputs.source_sha) || '' }}",
    ], src_tag_lines
    # The adopt stamp is checked on the decide step's parsed run scalar by
    # a dedicated checker with its own decoy battery below.
    src_decide_steps = [
        s
        for s in wf_jobs["adopt"]["steps"]
        if str(s.get("name", "")).startswith(
            "Adopt a verified speculative supply set"
        )
    ]
    assert len(src_decide_steps) == 1
    check_adopt_src_stamp(src_decide_steps[0]["run"])
    check_adopt_src_stamp_battery(src_decide_steps[0]["run"])

    # The cache fallback's ancestor walk reads real history: the three
    # image-building jobs' checkouts must fetch 20 commits (the walk's
    # --max-count is 15). actions/checkout defaults to depth 1, which
    # would make every walk silently empty while the fallback pins stayed
    # green (sol-critic round 1 on this PR). Parsed, not text-matched.
    for job_name in ("warm", "build", "speculate"):
        first_step = wf_jobs[job_name]["steps"][0]
        assert str(first_step.get("uses", "")).startswith(
            "actions/checkout@"), job_name
        assert first_step["with"].get("fetch-depth") == 20, (
            "%s's checkout must declare fetch-depth: 20 for the "
            "nearest-ancestor walk" % job_name)

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
    # The merged adopt job's grant set is exactly the UNION of the two
    # pre-fold jobs (2026-08-05 adopt/verify fold): id-token:write +
    # contents:read were held by both, actions:read by adopt's artifact
    # listing alone. The verify half added no grant and no role — its ECR
    # digest reads ride the release role adopt already assumed for
    # aliasing. verify keeps the smaller pre-fold pair: it never lists
    # artifacts, so it must not hold actions:read.
    assert wf_jobs["adopt"]["permissions"] == {
        "id-token": "write",
        "contents": "read",
        "actions": "read",
    }
    assert wf_jobs["verify"]["permissions"] == {
        "id-token": "write",
        "contents": "read",
    }
    assert "continue-on-error" not in wf_jobs["adopt"]
    # adopt's SETUP steps are individually absorbed (a pre-write failure
    # must degrade through decide, not veto the fallback build); the decide
    # step is the deliberate exception — its only nonzero exit is the
    # post-write committed path, which must fail the run. The steps AFTER
    # decide are the adopted-path verify half: each runs exactly on
    # adopted == 'true', and none is absorbed — any failure there is
    # post-commit by construction (adopted=true means every alias
    # re-verified), so it must redden the run; a rerun resumes
    # idempotently through decide's existing-alias arm.
    adopt_steps = wf_jobs["adopt"]["steps"]
    assert len(adopt_steps) == 10
    assert adopt_steps[4].get("id") == "decide"
    for step in adopt_steps[:4]:
        assert step.get("continue-on-error") is True, step
    assert "continue-on-error" not in adopt_steps[4]
    assert "if" not in adopt_steps[4]
    for step in adopt_steps[5:]:
        assert step.get("if") == "steps.decide.outputs.adopted == 'true'", step
        assert "continue-on-error" not in step, step

    # The two supply-set writers are byte-identical where bytes matter,
    # the same idiom that keeps warm's and build's cache-bearing inputs
    # aligned: drift between them would make the adopted path attest a
    # different artifact than the full-build path for the same tag. The
    # env SOURCES differ by construction — verify reads the adopt job's
    # outputs across the needs edge, adopt reads its own decide step —
    # and are pinned exactly.
    verify_steps = wf_jobs["verify"]["steps"]

    def _sole_named(steps, name):
        found = [s for s in steps if s.get("name") == name]
        assert len(found) == 1, name
        return found[0]

    web_step_name = "Build and hash the exact web deployment artifact"
    write_step_name = "Write the immutable five-service staging supply set"
    for step_name in (web_step_name, write_step_name):
        adopt_copy = _sole_named(adopt_steps, step_name)
        verify_copy = _sole_named(verify_steps, step_name)
        assert adopt_copy["run"] == verify_copy["run"], step_name
        for job_name, job in wf_jobs.items():
            if job_name in ("adopt", "verify"):
                continue
            for step in job.get("steps", []):
                assert step.get("name") != step_name, (job_name, step_name)
    adopt_web = _sole_named(adopt_steps, web_step_name)
    verify_web = _sole_named(verify_steps, web_step_name)
    assert adopt_web["env"] == verify_web["env"]
    assert adopt_web.get("working-directory") == "web"
    assert verify_web.get("working-directory") == "web"
    assert adopt_web.get("id") == "web-artifact" == verify_web.get("id")
    adopt_write = _sole_named(adopt_steps, write_step_name)
    verify_write = _sole_named(verify_steps, write_step_name)
    assert verify_write["env"] == {
        "ADOPTED": "${{ needs.adopt.outputs.adopted }}",
        "ADOPTED_BUILT_FROM": "${{ needs.adopt.outputs.built_from }}",
        "ADOPTED_PR_NUMBER": "${{ needs.adopt.outputs.pr_number }}",
        "ADOPTED_SPEC_RUN_ID": "${{ needs.adopt.outputs.spec_run_id }}",
    }
    assert adopt_write["env"] == {
        "ADOPTED": "${{ steps.decide.outputs.adopted }}",
        "ADOPTED_BUILT_FROM": "${{ steps.decide.outputs.built_from }}",
        "ADOPTED_PR_NUMBER": "${{ steps.decide.outputs.pr_number }}",
        "ADOPTED_SPEC_RUN_ID": "${{ steps.decide.outputs.spec_run_id }}",
    }
    # Both writers' scripts read $TAG, so adopt must alias PROD_TAG and
    # TAG to the same prepare output the verify job uses.
    assert wf_jobs["adopt"]["env"]["TAG"] == wf_jobs["verify"]["env"]["TAG"]
    assert wf_jobs["adopt"]["env"]["TAG"] == wf_jobs["adopt"]["env"]["PROD_TAG"]
    # Upload pairs: identical with: mappings, so either writer publishes
    # the same artifact names with if-no-files-found: error — an adopted
    # run can never conclude green without a supply set, and if both
    # writers ever ran (they cannot: build gates verify off when adopted)
    # upload-artifact would refuse the duplicate names loudly.
    adopt_uploads = [
        s for s in adopt_steps
        if str(s.get("uses", "")).startswith("actions/upload-artifact")
    ]
    verify_uploads = [
        s for s in verify_steps
        if str(s.get("uses", "")).startswith("actions/upload-artifact")
    ]
    assert len(adopt_uploads) == 2
    assert len(verify_uploads) == 2
    for adopt_upload, verify_upload in zip(adopt_uploads, verify_uploads):
        assert adopt_upload["with"] == verify_upload["with"], adopt_upload
    adopt_nodes = [
        s for s in adopt_steps
        if str(s.get("uses", "")).startswith("actions/setup-node")
    ]
    verify_nodes = [
        s for s in verify_steps
        if str(s.get("uses", "")).startswith("actions/setup-node")
    ]
    assert len(adopt_nodes) == 1
    assert len(verify_nodes) == 1
    assert adopt_nodes[0]["with"] == verify_nodes[0]["with"]

    # warm's and build's cache-select scripts are byte-identical, the same
    # idiom that binds their build-push inputs: drift would let one lane's
    # fallback rot while the other lane's pins stayed green.
    cache_step_name = "Select immutable cache references"
    warm_cache_run = _sole_named(wf_jobs["warm"]["steps"], cache_step_name)["run"]
    build_cache_run = _sole_named(wf_jobs["build"]["steps"], cache_step_name)["run"]
    assert warm_cache_run == build_cache_run, (
        "warm and build must carry byte-identical cache-select scripts")
    # The cache-select scripts bind WHOLE and BYTE-EXACT to the parsed
    # run value of each named step. The escalation history on this PR:
    # whole-job live-line counts admitted an env-value decoy at matching
    # columns (sol-critic round 2), and a contains-once count over the
    # parsed run value still admitted the canonical bytes wrapped dead
    # inside `if false; then ... fi` (round 3). Only equality of the
    # entire executable content proves the fallback executes — the same
    # conclusion HINT_SCRIPT's history reached. The parser strips the
    # scalar's ten-column base indentation, so the canonical copies are
    # dedented before comparing.
    def _dedent_run(script):
        return "\n".join(
            l[10:] for l in script.rstrip("\n").splitlines()
        )

    assert warm_cache_run.rstrip("\n") == _dedent_run(WARM_BUILD_CACHE_SCRIPT), (
        "the warm/build cache-select script must equal its canonical "
        "pinned copy in this file; edit both together, consciously")
    speculate_cache_run = _sole_named(
        wf_jobs["speculate"]["steps"],
        "Select the merged-onto main tip's cache, import-only")["run"]
    assert speculate_cache_run.rstrip("\n") == _dedent_run(SPECULATE_CACHE_SCRIPT), (
        "the speculative cache-select script must equal its canonical "
        "pinned copy in this file; edit both together, consciously")
    # Self-consistency of the canonical copies: both must carry the
    # canonical fallback exactly once, so FALLBACK_SCRIPT cannot rot into
    # a decoy of its own while the whole-script pins stay green.
    assert WARM_BUILD_CACHE_SCRIPT.count(FALLBACK_SCRIPT.rstrip("\n")) == 1
    assert SPECULATE_CACHE_SCRIPT.count(FALLBACK_SCRIPT.rstrip("\n")) == 1
    # Identity binding, name -> id -> consumer (sol-critic round 4 on
    # this PR): the name-pinned step must BE the `id: cache` step, that
    # id must be unique in its job, its condition must be exactly the
    # lane's legitimate gate (the resume guard in build), and the lane's build-push
    # step must consume exactly steps.cache's outputs — otherwise a
    # canonical-script decoy under the pinned NAME could sit unreferenced
    # while a renamed hollow step feeds the consumers empty outputs.
    cache_expr_from = "${{ steps.cache.outputs.from }}"
    cache_expr_to = "${{ steps.cache.outputs.to }}"
    for job_name, step_name, expected_if in (
        ("warm", cache_step_name, "steps.chain.outputs.skip != 'true'"),
        ("build", cache_step_name, "steps.resume.outputs.skip != 'true'"),
        ("speculate", "Select the merged-onto main tip's cache, import-only",
         "steps.exists.outputs.present != 'true'"),
    ):
        cache_step_parsed = _sole_named(wf_jobs[job_name]["steps"], step_name)
        assert cache_step_parsed.get("id") == "cache", (
            "%s's cache-select step must carry id: cache — the id the "
            "lane's build-push step consumes" % job_name)
        same_id = [
            s for s in wf_jobs[job_name]["steps"] if s.get("id") == "cache"
        ]
        assert len(same_id) == 1, job_name
        assert cache_step_parsed.get("if") == expected_if, (
            job_name, cache_step_parsed.get("if"))
        build_push_steps = [
            s for s in wf_jobs[job_name]["steps"]
            if str(s.get("uses", "")).startswith("docker/build-push-action")
        ]
        assert len(build_push_steps) == 1, job_name
        push_with = build_push_steps[0]["with"]
        assert push_with.get("cache-from") == cache_expr_from, job_name
        if job_name == "speculate":
            assert "cache-to" not in push_with
        else:
            assert push_with.get("cache-to") == cache_expr_to, job_name

    # The staging relay accepts exactly the two supply-set schemas; the
    # deployable fields are the same shape in both.
    relay_text = (WORKFLOW.parent / "dispatch-staging-deploys.yml").read_text(
        encoding="utf-8"
    )
    assert (
        "leaf.staging-supply-set.v1|leaf.staging-supply-set.v2) ;;" in relay_text
    )
    assert relay_text.count("is not an accepted staging supply-set schema") == 1

    # The speculative dispatcher's whole contract lives in
    # check_speculative_dispatcher, callable on mutated text so the decoy
    # battery can prove each pin catches the vector it names.
    dispatcher_path = WORKFLOW.parent / "speculate-platform-images.yml"
    check_speculative_dispatcher(dispatcher_path.read_text(encoding="utf-8"))
    check_speculative_dispatcher_battery(dispatcher_path)

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
    # afford. The decision lives in prepare's decide step (real git diff,
    # no window; folded from the former standalone noop job on 2026-08-05
    # — the separate job cost ~8s of work plus ~12s of runner scheduling
    # ahead of every real build, run 30992431627) and
    # scripts/docs_noop_filter.py (imported and exercised below — the
    # shipped logic, not a re-implementation).
    # ------------------------------------------------------------------ #
    assert "paths-ignore" not in text, (
        "native path filtering is fail-closed on truncated diffs; the "
        "decide step in prepare owns the docs-only decision")

    # Structural invariants read from the PARSED workflow, so a guard that
    # drifts into a comment or another step key stops satisfying them.
    wf = _strict_yaml(text)
    on_block = wf.get("on") or wf.get(True)  # YAML 1.1 reads bare `on` as a bool
    assert on_block["push"] == {"branches": ["main"]}, (
        "the push trigger must carry branches only — no native path filter")

    jobs = wf["jobs"]
    # The decide step lives INSIDE prepare since the 2026-08-05 fold; a
    # reintroduced standalone noop job would mean two docs-only deciders
    # (and would silently stop gating anything, since every guard reads
    # prepare's output).
    assert "noop" not in jobs
    prepare_job = jobs["prepare"]
    assert "needs" not in prepare_job, "prepare is the graph's root job"
    assert "if" not in prepare_job
    assert prepare_job["outputs"]["build"] == "${{ steps.decide.outputs.build }}"
    # prepare SUCCEEDS on a docs-only push (it must: it publishes the
    # marker artifact), so the docs gate is enforced by the three guards
    # below plus build/verify's needs-result checks — a docs-only push
    # runs NOTHING downstream. Pinned as parsed values; the folded
    # step-level conditions inside prepare are pinned in main().
    assert jobs["test"]["if"] == (
        "${{ !inputs.promote && !inputs.speculative && "
        "needs.prepare.outputs.build == 'true' }}"
    )
    # Since the 2026-08-05 chain-keeper change warm carries the same guard
    # as test: the gate-reuse skip moved into warm's per-leg decide step,
    # which can probe ECR for the predecessor buildcache tag before
    # skipping (a header-level skip cannot, and starved the cache chain on
    # adopted merges).
    assert jobs["warm"]["if"] == (
        "${{ !inputs.promote && !inputs.speculative && "
        "needs.prepare.outputs.build == 'true' }}"
    )
    assert jobs["adopt"]["if"] == (
        "${{ !inputs.promote && github.event_name == 'push' && "
        "needs.prepare.outputs.build == 'true' }}"
    )

    # No always() anywhere downstream of the gate: it would run a train job
    # even after the docs-noop skip (or a failed upstream) shut its needs
    # off. test-gate.yml legitimately uses always() for its fan-in; on a
    # docs-only push the test guard above gates that called workflow off,
    # so the scope is exactly the build workflow and the staging relay.
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
    decide = next(s for s in prepare_job["steps"] if s.get("id") == "decide")
    decide_src = decide["run"]
    decide_code = _executable_bash(decide_src)
    assert decide["env"]["EVENT_NAME"] == "${{ github.event_name }}"
    assert decide["env"]["BEFORE_SHA"] == "${{ github.event.before }}"
    assert decide["env"]["GH_TOKEN"] == "${{ github.token }}"
    # Credential reconciliation (2026-08-05 fold): prepare's checkout
    # persists NO credentials — later prepare steps execute
    # preview-authored scripts on speculative dispatches — while the
    # before-sha fetch needs auth, so the fetch carries its own scoped,
    # non-persisted header (the merge-preview fetch idiom). Without it an
    # unauthenticated fetch would fail this gate open forever, silently
    # rebuilding every docs push; with it, an auth failure still lands on
    # the fail-open arm. The decide step also executes no checkout
    # content on non-push events (the event arm exits first), so a
    # preview tree never runs in this token-bearing step.
    assert "http.https://github.com/.extraheader" in decide_code
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
        for s in prepare_job["steps"]
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

    # The main fetch's auth binding, counted over executable text and the
    # parsed step env so a comment cannot stand in for either half: the
    # header is built from the workflow's own token, the step env supplies
    # it, and the job-level cross-repo PAT never enters the git header.
    assert "x-access-token:$MAIN_FETCH_TOKEN" in handoff_code
    assert "x-access-token:$GH_TOKEN" not in handoff_code
    consume_step = next(
        s
        for s in jobs["handoff"]["steps"]
        if str(s.get("name", "")).startswith("Consume the release manifest")
    )
    assert consume_step["env"]["MAIN_FETCH_TOKEN"] == "${{ github.token }}"

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
    # 5 -> 105 on 2026-08-07: the relay now watches both deploys to a terminal
    # state instead of dispatching and exiting, and a deploy runs ~8-14 min
    # plus time queued behind the shared staging lock. Must stay above the
    # step's own 95-minute deadline; check_staging_relay_convergence asserts
    # that relationship rather than the literal.
    assert dispatch_job["timeout-minutes"] == 105
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
    # HOME_TOKEN added 2026-08-07. The per-service tip re-check reads THIS
    # repo, which the infra PAT is not scoped for; github.token is, and the
    # workflow's permissions block above still pins it read-only. This is a
    # read the step already had the right to make, not new capability — the
    # secret-reference count below remains the wall that matters.
    assert dispatch_step["env"] == {
        "GH_TOKEN": "${{ secrets.TERRAFORM_REPO_TOKEN }}",
        "HOME_TOKEN": "${{ github.token }}",
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
    # Hash updated 2026-08-07: the dispatch step now deploys one service at a
    # time and watches each run to a terminal state, instead of firing both
    # deploys two seconds apart and exiting. Reviewed for dispatch capability:
    # still exactly one `gh workflow run` site with the same four inputs, one
    # added read of THIS repo's tip on the workflow's own read-only token, and
    # no new secret reference (the count assertion above is unchanged).
    assert frozen == (
        "39e47e1a24a7770d2d6ca1a28f21a706040eaab2f6e135562d56865e110deb72"
    ), (
        "relay step scripts changed: review the diff for dispatch "
        "capability, then update this hash in the same PR"
    )

    # The convergence contract itself: a dispatched service is always
    # accounted for, or the relay goes red. Batteries prove each pin catches
    # the regression vector it names.
    check_staging_relay_convergence(relay_text)
    check_staging_relay_convergence_battery(
        WORKFLOW.parent / "dispatch-staging-deploys.yml")

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
            # The decide script's credentialed-fetch header targets
            # https://github.com/ only; against this local file-path
            # origin the config is inert, so the rehearsal exercises the
            # same code path without needing a real token value.
            env["GH_TOKEN"] = "rehearsal-token"
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


def check_speculative_dispatcher(dispatcher_text: str) -> None:
    # The dispatcher is deliberately secretless and same-repo-gated: the
    # real build runs on the MAIN ref (main's workflow text, main's OIDC
    # subject). If a secrets.* reference ever appears here, the PR-editable
    # pull_request surface has grown a credential, which is exactly what
    # the dispatch indirection exists to prevent. Bound to the PARSED
    # document (strict loader, duplicate keys refused), so a trigger,
    # permission, or guard relocated into a scalar cannot satisfy it; the
    # dispatch command lines are bound to the step's comment-stripped
    # EXECUTABLE bash.
    #
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
    # this job may find a persisted token). With step 0 pinned to `uses:`
    # and the step count pinned, step 1's `run` is this job's ONLY bash.
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
    # parent via git show, never from the PR-controlled preview checkout,
    # because this step's env carries GH_TOKEN with actions:write and a
    # preview-sourced filter would hand a same-repo PR that token before
    # merge. The preview supplies only the FILE LIST (data, not code).
    #
    # Round 2 of that review (merged past by operator direction, hardened
    # here) proved substring pins alone are satisfied by executable
    # DECOYS: a `:` no-op quoting the git-show token, a quoted no-op
    # carrying the $RUNNER_TEMP invocation, `python3 ./scripts/...` with
    # a `./` the banned literal missed, and `: "DISPATCH=false"`. The
    # load-bearing lines are therefore bound as STATEMENTS: whole-line
    # assignments, the trusted-copy extraction and the filter invocation
    # in the command position of their logical lines, and filename
    # arithmetic accounting for every occurrence of the filter's name.
    assert dispatch_script.count("DISPATCH=true") == 1
    assert dispatch_script.count("DISPATCH=false") == 1
    assert re.search(r"^[ \t]*DISPATCH=true[ \t]*$", dispatch_script, re.M), (
        "DISPATCH=true must be a whole-line assignment statement, not a "
        "decoy inside quotes or a `:` no-op"
    )
    assert re.search(
        r'if \[ "\$VERDICT" = "skip" \]; then\n'
        r"(?:[ \t]*echo [^\n]*\n)*"
        r"[ \t]*DISPATCH=false\n"
        r"[ \t]*fi",
        dispatch_script,
    ), (
        "DISPATCH=false must be a whole-line assignment inside the "
        '[ "$VERDICT" = "skip" ] arm; `: "DISPATCH=false"` preserves '
        "every count and ordering while leaving DISPATCH true at runtime"
    )
    # Multi-line statements are pinned over LOGICAL lines (backslash
    # continuations joined) so each must sit in the command position of
    # its line, where a quoted decoy cannot.
    dispatch_logical = re.sub(r"[ \t]*\\\n[ \t]*", " ", dispatch_script)
    assert re.search(
        r"^[ \t]*if git rev-parse --verify --quiet 'HEAD\^1' >/dev/null 2>&1 "
        r"&& git show 'HEAD\^1:scripts/docs_noop_filter\.py' "
        r'> "\$RUNNER_TEMP/docs_noop_filter\.py" 2>/dev/null; then[ \t]*$',
        dispatch_logical,
        re.M,
    ), (
        "the trusted-copy extraction must run git show in command "
        "position, redirected to the $RUNNER_TEMP copy"
    )
    assert re.search(
        r"^[ \t]*if FILES=\$\(git diff --no-renames --name-only "
        r"HEAD\^1 HEAD 2>/dev/null\); then[ \t]*$",
        dispatch_logical,
        re.M,
    )
    assert re.search(
        r"^[ \t]*if VERDICT=\$\(printf '%s\\n' \"\$FILES\" \| "
        r'python3 "\$RUNNER_TEMP/docs_noop_filter\.py" 2>/dev/null\); then[ \t]*$',
        dispatch_logical,
        re.M,
    ), (
        "the filter must execute the $RUNNER_TEMP copy in the command "
        "position of the VERDICT capture, fed the file list on stdin"
    )
    # Filename arithmetic: the filter's name may appear ONLY inside the
    # two trusted tokens, the first-parent git-show source (once) and the
    # $RUNNER_TEMP copy it writes then executes (twice). 1 + 2 accounts
    # for all 3 bare-name occurrences, so ANY other spelling (a bare
    # `scripts/` path, the `./` variant, a decoy quoting a trusted token)
    # changes a count and fails.
    assert dispatch_script.count("docs_noop_filter.py") == 3
    assert dispatch_script.count("'HEAD^1:scripts/docs_noop_filter.py'") == 1
    assert dispatch_script.count('"$RUNNER_TEMP/docs_noop_filter.py"') == 2
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
    # LIMITATION, stated rather than chased (the PR #445 review lesson:
    # before tightening a lint again, ask whether the property is
    # enforceable at that layer at all). These are text checks over
    # comment-stripped bash, not a bash parser. They bind the statement
    # shape of the current trusted-copy implementation, so single-token
    # rewrites fail a shape pin or the arithmetic; they CANNOT exclude
    # deliberate obfuscation that never spells a pinned token: variable
    # indirection (f="scripts/..."; python3 "$f"), globs
    # (python3 scripts/*_filter.py), eval / source / sh -c, string
    # splicing ("docs_noop_filter"".py"), or code smuggled inside a
    # multi-line quoted string. The boundaries that hold against a
    # determined editor are the ones asserted from the parsed document
    # above (a dispatcher whose only credential is github.token, the
    # same-repo non-fork job gate, the real build running MAIN's workflow
    # text via workflow_dispatch) plus review of any diff that touches
    # this workflow; the statement pins exist to catch accidental drift
    # and casual smuggling, not to certify the absence of hostile bash.


def _executable_shell(run_text: str) -> str:
    """The run block's executable lines only: full-line comments and blank
    lines dropped. A pin against this text cannot be satisfied by a
    commented decoy, and a live line commented out stops satisfying it."""
    return "\n".join(
        raw
        for raw in run_text.splitlines()
        if raw.strip() and not raw.strip().startswith("#")
    )


def check_adopt_src_stamp(decide_run: str) -> None:
    """The adopted app image's src-<full-commit> identity stamp.

    Invariants, bound to executable shell lines:
    - exactly one live assignment line `src_tag="src-$SOURCE_SHA"` (line-
      exact, so a quoted no-op decoy `: '...'` does not count);
    - the stamp sits AFTER the release-alias re-verify loop and before the
      spec_run_id output block;
    - the stamp block is best-effort: no exit, no finish, no COMMITTED
      mutation — a failure may only warn;
    - it touches only leaf-platform-app and writes only "$src_tag";
    - the full-commit guard on SOURCE_SHA is present and exact.
    """
    text = _executable_shell(decide_run)
    lines = [l.strip() for l in text.splitlines()]
    assert lines.count('src_tag="src-$SOURCE_SHA"') == 1, (
        "exactly one executable src_tag assignment line")
    guard = 'if [[ "$SOURCE_SHA" =~ ^[0-9a-f]{40}$ ]]; then'
    assert lines.count(guard) == 1, "full-commit guard on SOURCE_SHA"
    reverify_at = text.index("re-verification failed after aliasing")
    stamp_at = text.index('src_tag="src-$SOURCE_SHA"')
    assert reverify_at < stamp_at, (
        "the stamp must follow the release-alias re-verify loop")
    start = text.index(guard)
    end = text.index('spec_run_id="$(jq -r', start)
    assert start < stamp_at < end, "stamp block bounds"
    block = text[start:end]
    for banned in ("exit", "finish ", "COMMITTED="):
        assert banned not in block, (
            f"src- stamp block is best-effort and must not contain {banned!r}")
    assert block.count("aws ecr put-image") == 1
    assert '--image-tag "$src_tag"' in block
    assert block.count("--repository-name leaf-platform-app") == 2, (
        "the stamp probes and writes leaf-platform-app only")
    assert "--repository-name leaf-platform-web" not in block
    assert "::warning::adopt: could not stamp leaf-platform-app:$src_tag" in block
    assert "already names a different digest" in block
    assert text.index('--image-tag "$PROD_TAG"') < stamp_at, (
        "the release alias loop writes before any stamp")


def check_adopt_src_stamp_battery(decide_run: str) -> None:
    """Decoy mutation battery for check_adopt_src_stamp: each negative is
    an executable decoy that must be CAUGHT; each positive control is an
    ordinary maintenance edit that must still PASS."""
    original = decide_run

    def mutate(text: str, old: str, new: str) -> str:
        assert old in text, f"battery fixture drifted: {old!r} not in decide run"
        return text.replace(old, new)

    assignment = 'src_tag="src-$SOURCE_SHA"'
    guard = 'if [[ "$SOURCE_SHA" =~ ^[0-9a-f]{40}$ ]]; then'
    warn_stamp = (
        'echo "::warning::adopt: could not stamp leaf-platform-app:$src_tag'
    )

    negatives = [
        (
            "assignment commented out (decoy text keeps raw pins green)",
            mutate(original, assignment, "true # " + assignment),
        ),
        (
            "quoted no-op replacing the live assignment",
            mutate(original, assignment, ": '" + assignment + "'"),
        ),
        (
            "exit smuggled into the best-effort block",
            mutate(original, warn_stamp, "exit 1\n            " + warn_stamp),
        ),
        (
            "stamp retargeted at the web repository",
            mutate(
                original,
                "--repository-name leaf-platform-app \\\n    --image-ids \"imageTag=$src_tag\"",
                "--repository-name leaf-platform-web \\\n    --image-ids \"imageTag=$src_tag\"",
            ),
        ),
        (
            "full-commit guard loosened to a short prefix",
            mutate(original, guard, guard.replace("{40}", "{7,40}")),
        ),
    ]
    for name, mutant in negatives:
        assert mutant != original
        try:
            check_adopt_src_stamp(mutant)
        except AssertionError:
            continue
        raise AssertionError(f"adopt src- stamp battery: {name} was NOT caught")

    positives = [
        ("unmodified decide step", original),
        (
            "added full-line comment inside the stamp block",
            mutate(
                original,
                "  " + assignment,
                "  # identity stamp is best-effort by design\n"
                "  " + assignment,
            ),
        ),
        (
            "added innocuous notice in the stamp block",
            mutate(
                original,
                warn_stamp,
                'echo "stamp is best-effort"\n            ' + warn_stamp,
            ),
        ),
    ]
    for name, control in positives:
        try:
            check_adopt_src_stamp(control)
        except AssertionError as exc:
            raise AssertionError(
                f"adopt src- stamp battery: control {name!r} must pass "
                f"but tripped: {exc}"
            )

    print("adopt src- stamp decoy battery: PASS")


def check_speculative_dispatcher_battery(dispatcher_path: Path) -> None:
    """Decoy mutation battery for check_speculative_dispatcher.

    Negatives are the executable decoys from sol-critic round 2 on PR
    #452 plus close variants; each must be CAUGHT. Positive controls are
    ordinary maintenance edits; each must still PASS (the PR #445
    round-10 tell: a rule can be simultaneously incomplete and hostile
    to normal edits).
    """
    original = dispatcher_path.read_text(encoding="utf-8")

    def mutate(text: str, old: str, new: str) -> str:
        assert old in text, f"battery fixture drifted: {old!r} not in workflow"
        return text.replace(old, new)

    initializer = "          DISPATCH=true\n"
    invocation = 'python3 "$RUNNER_TEMP/docs_noop_filter.py" 2>/dev/null); then'
    skip_assignment = "                  DISPATCH=false\n"
    git_show_decoy = "          : \"git show 'HEAD^1:scripts/docs_noop_filter.py'\"\n"
    runner_temp_decoy = "          : 'python3 \"$RUNNER_TEMP/docs_noop_filter.py\"'\n"

    negatives = [
        (
            "no-op decoy quoting the git-show token",
            mutate(original, initializer, initializer + git_show_decoy),
        ),
        (
            "no-op decoy quoting the $RUNNER_TEMP invocation",
            mutate(original, initializer, initializer + runner_temp_decoy),
        ),
        (
            "./-prefixed preview execution replacing the trusted copy",
            mutate(
                original,
                invocation,
                "python3 ./scripts/docs_noop_filter.py 2>/dev/null); then",
            ),
        ),
        (
            "quoted no-op replacing the DISPATCH=false assignment",
            mutate(
                original, skip_assignment, '                  : "DISPATCH=false"\n'
            ),
        ),
        (
            "quoted no-op replacing the DISPATCH=true initializer",
            mutate(original, initializer, '          : "DISPATCH=true"\n'),
        ),
    ]
    # The round-2 composite: both no-op decoys keep the old substring pins
    # satisfied while the filter actually executes from the PR-controlled
    # checkout and the skip arm assigns nothing.
    composite = mutate(
        original, initializer, initializer + git_show_decoy + runner_temp_decoy
    )
    composite = mutate(
        composite,
        invocation,
        "python3 ./scripts/docs_noop_filter.py 2>/dev/null); then",
    )
    composite = mutate(
        composite, skip_assignment, '                  : "DISPATCH=false"\n'
    )
    negatives.append(("round-2 composite decoy scenario", composite))

    for name, mutant in negatives:
        assert mutant != original
        try:
            check_speculative_dispatcher(mutant)
        except AssertionError:
            continue
        raise AssertionError(f"dispatcher decoy battery: {name} was NOT caught")

    dispatched_echo = (
        '            echo "Dispatched a speculative build of PR'
        ' #$PR_NUMBER at $HEAD_SHA."\n'
    )
    positives = [
        ("unmodified workflow", original),
        (
            "added full-line bash comment",
            mutate(
                original,
                initializer,
                "          # fail-open: every abnormal path dispatches\n"
                + initializer,
            ),
        ),
        (
            "added innocuous echo in the success arm",
            mutate(
                original,
                dispatched_echo,
                dispatched_echo + '            echo "speculation is best-effort"\n',
            ),
        ),
    ]
    for name, control in positives:
        try:
            check_speculative_dispatcher(control)
        except AssertionError as exc:
            raise AssertionError(
                f"dispatcher decoy battery: control {name!r} must pass "
                f"but tripped: {exc}"
            )

    print("speculative dispatcher decoy battery: PASS")


def check_staging_relay_convergence(text: str) -> None:
    """The relay may never end green with a dispatched service unaccounted for.

    WHY: until 2026-08-07 this relay dispatched web and app about two
    seconds apart and exited without reading either outcome. The infra repo
    funnels every staging mutation through ONE concurrency group
    (leaf-platform-staging-ecs-mutation, shared by 39 workflows) and GitHub
    keeps at most ONE pending run per group, cancelling whichever run was
    previously pending. So whenever the lock was already busy the second
    dispatch evicted the first, and staging deployed one service instead of
    two while the relay reported success.

    Four merges lost a service that way between 2026-08-05 and 2026-08-06:
    infra runs 30994453570, 31103023296, 31132184537 and 31132566801, each
    cancelled with ZERO jobs, i.e. killed while still queued. The most
    recent pair is the clearest reading — run 31132183543 (web) succeeded
    while 31132184537 (app) died queued, then 31132566801 (web) died queued
    while 31132568205 (app) succeeded — leaving leaf-platform-web-alt on the
    PREVIOUS merge's tree with its own built image sitting unused in ECR.

    These pins are about the RELAY'S OBLIGATION, not about the lock: the
    shared group is legitimate and stays. What changed is that the relay now
    deploys one service at a time and watches each run to a terminal state.
    """
    wf = _strict_yaml(text)
    job = wf["jobs"]["dispatch"]
    code = _executable_bash(job["steps"][-1]["run"])

    # Both services stay covered, and from a single dispatch site: a second
    # one is a deploy that the watch below would not be looking at.
    assert "for SERVICE in web app; do" in code, (
        "the relay must still deploy both staging services in one pass")
    assert code.count("gh workflow run") == 1, (
        "one dispatch site only: another would fire a deploy nobody watches")
    dispatch_at = code.index("gh workflow run")

    # THE HEADLINE INVARIANT, asserted first so a regression says so: a
    # dispatch is followed by resolving the run it created and polling that
    # run to a terminal state. Dispatching and exiting is the original bug.
    assert 'gh run view "$RUN_ID"' in code, (
        "a dispatched deploy must be watched to a terminal state, not assumed")
    assert code.index('gh run view "$RUN_ID"') > dispatch_at

    # Identity must be POSITIVE. sol-critic returned RED on PR #497 against an
    # earlier version that bound "the one new run above the high-water mark":
    # GitHub creates and lists dispatched runs asynchronously and sibling lanes
    # dispatch the same workflow a few times an hour, so a sibling's run can
    # become visible before ours. The relay would then watch THEIR run, report
    # their success as our service's, and dispatch the next service — evicting
    # its own still-queued run. Green relay, undeployed service: the very bug
    # this file exists to prevent, re-entering through the resolver.
    assert re.search(r'RUN_ID=\$\(find_run_named "\$BEFORE" "\$WANT"', code), (
        "the watched run must be identified BY NAME; inferring it from "
        "'a new run appeared' can bind a sibling lane's run")
    assert code.count("RUN_ID=$(") == 1, (
        "exactly one place may bind RUN_ID")
    assert "displayTitle == $want" in code, (
        "run resolution must compare the run name, not just the run id")

    # The tip is re-read INSIDE the per-service loop, before each dispatch.
    # Deploying serially widens the window in which main can move, and
    # dispatching an older tag behind a newer relay would roll staging
    # BACKWARDS — the exact thing latest-main-only staging exists to stop.
    assert "for SERVICE in web app" in code
    assert "branches/main" in code, (
        "each dispatch must be preceded by a fresh tip-of-main read")
    assert code.index("for SERVICE in web app") < code.index("branches/main") < dispatch_at, (
        "the tip re-check must sit inside the loop and before each dispatch")

    # The tip read is the FIRST statement of every dispatch attempt and is
    # NEVER gated. sol-critic RED round 2 on PR #497 broke the tempting
    # alternative: gating it once a service had landed (to avoid leaving a
    # split) lets relay A hold a queued app deploy across newer relay B's
    # whole web-then-app sequence, each eviction retried, so A's OLDER app
    # lands on top of B's and both relays finish green. Deploying web before
    # app orders services within one relay, not two relays against each other.
    # A visible split is survivable; a silent backwards deploy is not.
    loop_body = code[code.index("while :; do"):].splitlines()[1:]
    first_stmt = next(ln.strip() for ln in loop_body if ln.strip())
    assert first_stmt.startswith("MAIN_SHA="), (
        "every dispatch attempt must re-read the tip FIRST and ungated; "
        f"found {first_stmt!r}")

    # And standing down after something went live must NAME the split rather
    # than report a plain success.
    assert "STAGING IS SPLIT" in code, (
        "standing down mid-release must announce the split it leaves behind")
    assert re.search(r'if \[ "\$DEPLOYED_ANY" = "true" \]', code), (
        "the split warning must be conditioned on a service already being live")

    # The loop advances to the next service ONLY from inside the success arm.
    # Pinned as "a break lives in that arm" rather than "break is the next
    # line", so ordinary edits inside the arm stay legal.
    success_arm = re.search(
        r'if \[ "\$CONCLUSION" = "success" \]; then\n(.*?)\n\s*fi\b', code, re.S)
    assert success_arm, "the watch needs an explicit success arm"
    assert re.search(r"^\s*break\b", success_arm.group(1), re.M), (
        "the loop may only advance past a service whose deploy concluded success")
    assert "landed (run $RUN_ID)" in success_arm.group(1), (
        "the success arm must say which run landed the deploy")
    assert re.search(r"^\s*DEPLOYED_ANY=true\b", success_arm.group(1), re.M), (
        "a landed deploy must record that this release is now partly live, so "
        "the stand-down above cannot abandon it half-deployed")

    # Every announced error FAILS the job. An `::error::` followed by
    # anything other than `exit 1` is the silent-success bug returning.
    lines = code.splitlines()
    error_lines = [i for i, line in enumerate(lines) if "::error::" in line]
    assert len(error_lines) >= 3, (
        "the unresolved-run, watch-budget and non-success paths each need a "
        f"loud error; found {len(error_lines)}")
    for i in error_lines:
        following = [ln.strip() for ln in lines[i + 1:i + 3] if ln.strip()]
        assert following and following[0] == "exit 1", (
            f"::error:: on line {i + 1} is not followed by `exit 1`; "
            f"found {following[:1]}")

    # The only clean early exit is standing down for a newer commit.
    assert code.count("exit 0") == 1, (
        "the relay may exit 0 early only when a newer commit owns the deploy")
    assert "standing down" in code[:code.index("exit 0")][-500:], (
        "the single exit 0 must be the latest-main-only stand-down")

    # Retry covers QUEUE EVICTION ONLY. Zero started jobs proves the run
    # never reached AWS; re-dispatching a run that started and then failed
    # would blind-redeploy over a real failure.
    assert re.search(
        r'\[ "\$CONCLUSION" = "cancelled" \]\s*&&\s*\[ "\$JOBS" -eq 0 \]', code), (
        "the retry must require BOTH a cancelled conclusion and zero started jobs")
    assert code.count("continue") == 1, (
        "exactly one retry path may re-enter the dispatch loop")
    assert re.search(r'\[ "\$ATTEMPT" -lt "\$EVICTION_RETRIES" \]', code), (
        "eviction retries must be bounded")

    # The job must outlive the step's own deadline, so a stuck deploy is
    # reported as a NAMED half-deployed service instead of a bare job timeout.
    deadline = re.search(
        r"DEADLINE=\$\(\(\s*\$\(date \+%s\)\s*\+\s*(\d+)\s*\*\s*60", code)
    assert deadline, "the watch loop needs an explicit wall-clock deadline"
    assert job["timeout-minutes"] > int(deadline.group(1)), (
        f"timeout-minutes {job['timeout-minutes']} must exceed the step's own "
        f"{deadline.group(1)}-minute deadline")

    print("staging relay convergence invariants: PASS")


def check_staging_relay_convergence_battery(relay_path: Path) -> None:
    """Decoy battery: each negative reintroduces a real regression vector.

    Same shape as the speculative-dispatcher battery — negatives must be
    CAUGHT, positive controls must still PASS, so the pins above cannot be
    simultaneously incomplete and hostile to ordinary maintenance edits.
    """
    original = relay_path.read_text(encoding="utf-8")

    def mutate(text: str, old: str, new: str) -> str:
        assert old in text, f"battery fixture drifted: {old!r} not in relay"
        return text.replace(old, new)

    negatives = [
        (
            "non-success reported as a clean exit (the original silent bug)",
            mutate(original,
                   "\n              exit 1\n            done\n",
                   "\n              exit 0\n            done\n"),
        ),
        (
            "retrying a deploy that actually started and failed",
            mutate(original,
                   '[ "$CONCLUSION" = "cancelled" ] && [ "$JOBS" -eq 0 ] \\',
                   '[ "$CONCLUSION" = "cancelled" ] \\'),
        ),
        (
            "tip re-check weakened so an older tag can roll staging back",
            mutate(original,
                   '"repos/$GITHUB_REPOSITORY/branches/main" --jq \'.commit.sha\')',
                   '"repos/$GITHUB_REPOSITORY" --jq \'.default_branch\')'),
        ),
        (
            "a second dispatch nobody watches",
            mutate(original,
                   '              echo "Dispatched $SERVICE deploy of $IMAGE_TAG',
                   '              gh workflow run "$DEPLOY_WORKFLOW" --repo "$INFRA_REPO"\n'
                   '              echo "Dispatched $SERVICE deploy of $IMAGE_TAG'),
        ),
        (
            "job timeout cut below the step's own watch deadline",
            mutate(original, "timeout-minutes: 105", "timeout-minutes: 5"),
        ),
        (
            "one service quietly dropped from the loop",
            mutate(original, "for SERVICE in web app; do", "for SERVICE in web; do"),
        ),
        (
            "watch removed, back to dispatch-and-hope",
            mutate(original, 'gh run view "$RUN_ID"', 'true "$RUN_ID"'),
        ),
        (
            "run bound by 'newest new run' instead of by name (sol-critic RED)",
            mutate(original,
                   'RUN_ID=$(find_run_named "$BEFORE" "$WANT" || true)',
                   'RUN_ID=$(newest_run_since "$BEFORE" || true)'),
        ),
        (
            "tip re-check gated, letting a stale tag land on a newer deploy",
            mutate(original,
                   '              MAIN_SHA=$(GH_TOKEN="$HOME_TOKEN" gh api \\',
                   '              if [ "$DEPLOYED_ANY" = "false" ]; then\n'
                   '              MAIN_SHA=$(GH_TOKEN="$HOME_TOKEN" gh api \\'),
        ),
        (
            "mid-release stand-down reported as an ordinary success",
            mutate(original, "STAGING IS SPLIT: main moved", "main moved"),
        ),
        (
            "landed deploy no longer records that the release is partly live",
            mutate(original, "                DEPLOYED_ANY=true\n", ""),
        ),
        (
            "loop advances without the deploy having landed",
            mutate(original,
                   'landed (run $RUN_ID)."\n                break\n',
                   'landed (run $RUN_ID)."\n'),
        ),
    ]
    for name, mutant in negatives:
        assert mutant != original
        try:
            check_staging_relay_convergence(mutant)
        except AssertionError:
            continue
        raise AssertionError(f"relay convergence battery: {name} was NOT caught")

    landed_echo = '                echo "$SERVICE deploy of $IMAGE_TAG landed (run $RUN_ID)."\n'
    positives = [
        ("unmodified workflow", original),
        (
            "added full-line bash comment",
            mutate(original,
                   "          set -euo pipefail\n",
                   "          set -euo pipefail\n          # maintenance note\n"),
        ),
        (
            "added innocuous echo after a landed deploy",
            mutate(original, landed_echo,
                   landed_echo + '                echo "next service follows."\n'),
        ),
        (
            "watch deadline retuned while staying under the job timeout",
            mutate(original, "+ 95 * 60 ))", "+ 80 * 60 ))"),
        ),
    ]
    for name, control in positives:
        try:
            check_staging_relay_convergence(control)
        except AssertionError as exc:
            raise AssertionError(
                f"relay convergence battery: control {name!r} must pass "
                f"but tripped: {exc}")

    print("staging relay convergence decoy battery: PASS")


def test_build_platform_images_workflow_invariants() -> None:
    # Pytest entry point: the gate runner counts collected tests, and a bare
    # main() collects as zero.
    main()


def test_staging_relay_cannot_leave_a_service_undeployed() -> None:
    # Named separately from the mega-test so a scoreboard failure says which
    # invariant broke: this one is "a dispatched deploy is always accounted
    # for", the recurring defect diagnosed on 2026-08-07.
    relay = WORKFLOW.parent / "dispatch-staging-deploys.yml"
    check_staging_relay_convergence(relay.read_text(encoding="utf-8"))
    check_staging_relay_convergence_battery(relay)


if __name__ == "__main__":
    main()
