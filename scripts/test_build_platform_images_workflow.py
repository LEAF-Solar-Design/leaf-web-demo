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
import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import zipfile

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
    form, a $NAME / ${NAME} reference expands except inside single
    quotes or after a backslash-escaped dollar; double quotes (including
    apostrophes nested inside them) do expand.

    A `#` gets NO expansion decision here. _executable_bash has already
    removed real full-line and trailing shell comments, so a `#` reaching
    this scanner is ambiguous (a glued mid-word hash, or a comment inside
    a substitution or backtick the line stripper cannot see), and this
    returns False on it so the caller reports the RUN as offending: a
    LOUD false-offend on that class. See the `if "#" in body` guard.

    KNOWN FALSE-ACCEPT LIMITS (operator-accepted, PR #514 r9 -- do not
    re-file as bugs). This is a best-effort scanner, not a shell parser.
    It CAN silently mis-accept a per-commit ref that bash would not
    actually expand when the ref sits inside nested quoting or
    substitution, e.g. `"$(printf '%s' '${LEAF_SOURCE_SHA}')"` (single-
    quoted inside a command substitution inside double quotes), inside a
    heredoc body, or beside a `$$` self-escape; `_executable_bash` can
    likewise drop a `#` that is really inside a double-quoted value and
    skew the scan. Every such form needs adversarial RUN text: NO guarded
    Dockerfile has a `#`, a `$(...)`, a backtick, a heredoc or a `$$` in
    any post-ARG RUN (all six use simple `${REF}` forms), so the scanner
    is offense-free and correct on every real input. The residual is a
    SILENT cache-efficiency regression on contrived text only (one extra
    rebuild), never a correctness or security fault. Closing it fully
    needs a real shell parser or a fail-loud whitelist of allowed
    constructs; rounds 4-9 showed patch-per-corner does not terminate, so
    the residual is accepted rather than chased.
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
    # sol-critic #514 r8: FAIL LOUD on any `#`, do not classify it.
    # _executable_bash has already stripped real full-line and trailing
    # shell comments, so a `#` surviving here is ambiguous -- a glued
    # mid-word hash, or a comment INSIDE a substitution or backtick that
    # the line-based stripper cannot see (`$(# ...)`, `` `# ...` ``).
    # Rounds 4-7 grew a substitution/comment lexer to tell these apart
    # and every round surfaced the next corner (`;#`, `)#`, `${X}#`,
    # `$(x)#`, `$((1))#`, `\)#`, then r8's `$(#...)`), each an unbounded
    # false-ACCEPT: a non-consuming RUN read as consuming, placed below
    # the per-commit ARG, silently regressing layer caching -- the one
    # merge-blocking direction. Rather than model one more corner, refuse
    # the whole class: return False so the caller reports the RUN as
    # offending. A LOUD false-offend is the declared-acceptable direction
    # and this closes every silent false-accept by construction. No
    # guarded Dockerfile RUN contains a `#`, so it never trips.
    if "#" in body:
        return False
    # With `#` handled, a per-commit reference consumes wherever the
    # shell would expand it: everywhere except inside single quotes or
    # after a backslash-escaped `$`. Double quotes still expand
    # (apostrophes nested in them are literal); a backtick or `$(...)`
    # body expands too, so its inner reference is scanned like any other
    # text. This is deliberately smaller than the r4-r7 lexer: with the
    # `#` decision gone, no substitution/word-boundary state is needed.
    in_double = False
    i, n = 0, len(body)
    while i < n:
        ch = body[i]
        if in_double:
            if ch == "\\" and i + 1 < n and body[i + 1] in '$"`\\':
                i += 2
                continue
            if ch == '"':
                in_double = False
            elif ch == "$" and _PER_COMMIT_REF.match(body, i):
                return True
            i += 1
            continue
        if ch == "\\":
            i += 2
            continue
        if ch == "'":                       # single quotes: literal to next '
            close = body.find("'", i + 1)
            i = close + 1 if close != -1 else n
            continue
        if ch == '"':
            in_double = True
            i += 1
            continue
        if ch == "$" and _PER_COMMIT_REF.match(body, i):
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
          tags=("${CURRENT_CACHE_TAG:-}" "${PREVIOUS_CACHE_TAG:-}")
          for sha in $(git rev-list --first-parent --skip=1 --max-count=15 HEAD 2>/dev/null); do
            short="$(git rev-parse --short "$sha" 2>/dev/null)" || continue
            tags+=("buildcache-$short")
          done
          candidates=()
          seen=" "
          for tag in "${tags[@]}"; do
            if [[ -n "$tag" && "$seen" != *" $tag "* ]]; then
              seen="$seen$tag "
              candidates+=("$tag")
            fi
          done

          probe_ok="false"
          found=" "
          if [[ "${#candidates[@]}" -gt 0 ]]; then
            ids=()
            for tag in "${candidates[@]}"; do
              ids+=("imageTag=$tag")
            done
            for attempt in 1 2 3; do
              if raw="$(aws ecr batch-get-image \
                  --repository-name "$IMAGE_NAME-buildcache" \
                  --image-ids "${ids[@]}" \
                  --query 'images[].imageId.imageTag' \
                  --output text 2>/dev/null)"; then
                probe_ok="true"
                found=" $(printf '%s' "$raw" | tr '\t\n' '  ') "
                break
              fi
              if [[ "$attempt" -lt 3 ]]; then
                sleep 1
              fi
            done
          fi

          for tag in "${candidates[@]}"; do
            if [[ "$found" == *" $tag "* ]]; then
              cache_from="type=registry,ref=$cache_repo:$tag"
              if [[ "$tag" == "${CURRENT_CACHE_TAG:-}" ]]; then
                echo "::notice::$IMAGE_NAME reusing the warmed cache $CURRENT_CACHE_TAG"
              elif [[ "$tag" != "${PREVIOUS_CACHE_TAG:-}" ]]; then
                echo "::notice::$IMAGE_NAME importing the nearest ancestor cache $tag"
              fi
              break
            fi
          done
          if [[ -z "$cache_from" ]]; then
            echo "::notice::$IMAGE_NAME has no predecessor cache; building without cache input"
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

          # ONE probe, every candidate at once, best first:
          #
          #   1. THIS commit's own cache tag. The `warm` job builds the same tree
          #      beside the test gate and publishes exactly this tag, so by the
          #      time the gate is green the layers already exist and this build is
          #      a near-total cache hit rather than a cold rebuild. It is never a
          #      hard dependency: whenever warm was skipped, failed, or simply has
          #      not finished, the arms below carry it.
          #   2. The exact predecessor's tag.
          #   3. The nearest first-parent ancestor still published (chain keeper,
          #      2026-08-05). Under the adoption path a fully green merge run skips
          #      both cache writers, so the exact predecessor tag can be several
          #      commits stale; the cache keys are commit-stable below the
          #      per-commit ARGs (#458), so a near-ancestor cache still hits the
          #      heavy toolchain layers. Candidates come from the checkout's own
          #      first-parent history -- fetch-depth: 20 above exists for this walk.
          #
          # batch-get-image, deliberately never an ECR listing: neither workflow
          # role is granted one (leaf-automation-aws-terraform pins both to
          # BatchGetImage plus push actions), so an image-listing call would
          # AccessDenied and silently run every build cold.
          #
          # ONE call, where this step used to make four (2026-08-31). Each `aws`
          # invocation costs about 0.75s of process start even with the page cache
          # warm, and BatchGetImage accepts 100 image ids against our at-most 17,
          # so asking once is both cheaper and more consistent: every arm below
          # now reads the SAME response instead of four snapshots taken seconds
          # apart. Differentially tested against the four-call version across 15
          # scenarios (each candidate arm, none present, empty predecessor, no
          # ancestors, probe failure) -- identical from=/to=/notices in all 15.
          #
          # Every failure still lands on the uncached build: a probe that errors
          # leaves `found` empty, so no candidate matches and cache_from stays "".
          #
          # The probe carries a bounded retry, because it is now the ONLY probe.
          # The four separate calls this step used to make gave it accidental
          # redundancy against a transient ECR error, and collapsing them must not
          # quietly trade that away: three attempts a second apart, then the
          # uncached build. The selection that follows takes the first candidate
          # present and stops, and the space padding on both sides of the match is
          # what stops a tag matching as a substring of a longer one.
          tags=("${CURRENT_CACHE_TAG:-}" "${PREVIOUS_CACHE_TAG:-}")
          for sha in $(git rev-list --first-parent --skip=1 --max-count=15 HEAD 2>/dev/null); do
            short="$(git rev-parse --short "$sha" 2>/dev/null)" || continue
            tags+=("buildcache-$short")
          done
          candidates=()
          seen=" "
          for tag in "${tags[@]}"; do
            if [[ -n "$tag" && "$seen" != *" $tag "* ]]; then
              seen="$seen$tag "
              candidates+=("$tag")
            fi
          done

          probe_ok="false"
          found=" "
          if [[ "${#candidates[@]}" -gt 0 ]]; then
            ids=()
            for tag in "${candidates[@]}"; do
              ids+=("imageTag=$tag")
            done
            for attempt in 1 2 3; do
              if raw="$(aws ecr batch-get-image \
                  --repository-name "$IMAGE_NAME-buildcache" \
                  --image-ids "${ids[@]}" \
                  --query 'images[].imageId.imageTag' \
                  --output text 2>/dev/null)"; then
                probe_ok="true"
                found=" $(printf '%s' "$raw" | tr '\t\n' '  ') "
                break
              fi
              if [[ "$attempt" -lt 3 ]]; then
                sleep 1
              fi
            done
          fi

          for tag in "${candidates[@]}"; do
            if [[ "$found" == *" $tag "* ]]; then
              cache_from="type=registry,ref=$cache_repo:$tag"
              if [[ "$tag" == "${CURRENT_CACHE_TAG:-}" ]]; then
                echo "::notice::$IMAGE_NAME reusing the warmed cache $CURRENT_CACHE_TAG"
              elif [[ "$tag" != "${PREVIOUS_CACHE_TAG:-}" ]]; then
                echo "::notice::$IMAGE_NAME importing the nearest ancestor cache $tag"
              fi
              break
            fi
          done
          if [[ -z "$cache_from" ]]; then
            echo "::notice::$IMAGE_NAME has no predecessor cache; building without cache input"
          fi

          # The publication decision, read off that SAME response.
          #
          # ignore-error: the probe cannot exclude the other writer of this
          # immutable tag finishing in between (warm and the gated build race by
          # design, and ECR refuses the second manifest with
          # ImageTagAlreadyExistsException). Losing that race must cost only the
          # cache export, never the build. Taking the decision from the earlier
          # response means a racer landing mid-step is now discovered by ECR
          # rather than by a second probe: that costs one doomed export attempt
          # whose failure is already ignored, and nothing else.
          #
          # The probe_ok and non-empty guards are the fail-closed half. A probe
          # that never succeeded, or an empty CURRENT_CACHE_TAG, must land on "no
          # publication" and say so -- never on an export to an empty ref.
          if [[ "$probe_ok" == "true" && -n "${CURRENT_CACHE_TAG:-}" ]]; then
            if [[ "$found" == *" $CURRENT_CACHE_TAG "* ]]; then
              echo "::notice::$IMAGE_NAME cache $CURRENT_CACHE_TAG already exists; immutable tag will not be overwritten"
            else
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
          # Import the cache of the main tip this preview merged onto and export
          # nothing: speculative trees churn too fast to be worth polluting the
          # immutable buildcache-* namespace, and no export means no cache_to race
          # to tolerate. That is also why this lane never reads probe_ok -- the
          # probe block below is byte-identical to warm's and build's, and only
          # the publication half it feeds is absent here.
          set -euo pipefail
          cache_repo="$ECR_REGISTRY/$IMAGE_NAME-buildcache"
          cache_from=""

          # ONE probe, every candidate at once, best first: the merged-onto main
          # tip's exact tag, then the nearest first-parent ancestor still
          # published. That tip's tag can be several commits stale when adopted
          # merges published nothing, and the commit-stable keys (#458) make a
          # near-ancestor cache nearly as good. The preview's first-parent chain
          # IS main's history, so the same walk warm and build use works here
          # (fetch-depth: 20 above). This lane has no CURRENT_CACHE_TAG, so the
          # shared block's first candidate is simply empty and drops out.
          #
          # batch-get-image, deliberately never an ECR listing, which neither
          # workflow role is granted. IMPORT stays the only direction in this
          # lane, and every failure lands on the uncached build.
          #
          # The probe carries the same bounded retry warm and build use -- it is
          # the only probe here too -- and the selection takes the first candidate
          # present, space-padded on both sides so a tag cannot match as a
          # substring of a longer one.
          tags=("${CURRENT_CACHE_TAG:-}" "${PREVIOUS_CACHE_TAG:-}")
          for sha in $(git rev-list --first-parent --skip=1 --max-count=15 HEAD 2>/dev/null); do
            short="$(git rev-parse --short "$sha" 2>/dev/null)" || continue
            tags+=("buildcache-$short")
          done
          candidates=()
          seen=" "
          for tag in "${tags[@]}"; do
            if [[ -n "$tag" && "$seen" != *" $tag "* ]]; then
              seen="$seen$tag "
              candidates+=("$tag")
            fi
          done

          probe_ok="false"
          found=" "
          if [[ "${#candidates[@]}" -gt 0 ]]; then
            ids=()
            for tag in "${candidates[@]}"; do
              ids+=("imageTag=$tag")
            done
            for attempt in 1 2 3; do
              if raw="$(aws ecr batch-get-image \
                  --repository-name "$IMAGE_NAME-buildcache" \
                  --image-ids "${ids[@]}" \
                  --query 'images[].imageId.imageTag' \
                  --output text 2>/dev/null)"; then
                probe_ok="true"
                found=" $(printf '%s' "$raw" | tr '\t\n' '  ') "
                break
              fi
              if [[ "$attempt" -lt 3 ]]; then
                sleep 1
              fi
            done
          fi

          for tag in "${candidates[@]}"; do
            if [[ "$found" == *" $tag "* ]]; then
              cache_from="type=registry,ref=$cache_repo:$tag"
              if [[ "$tag" == "${CURRENT_CACHE_TAG:-}" ]]; then
                echo "::notice::$IMAGE_NAME reusing the warmed cache $CURRENT_CACHE_TAG"
              elif [[ "$tag" != "${PREVIOUS_CACHE_TAG:-}" ]]; then
                echo "::notice::$IMAGE_NAME importing the nearest ancestor cache $tag"
              fi
              break
            fi
          done
          if [[ -z "$cache_from" ]]; then
            echo "::notice::$IMAGE_NAME has no predecessor cache; building without cache input"
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
    # THREE PR-validating jq blocks live in this step now: the draft-PR
    # dispatch mode, the speculative mode (which validates the exact open
    # PR by number), and the speculative mode's merged-race arm — a PR
    # merged seconds before the dispatched guard read it ends the run
    # green instead of red (runs 33334277156, 33359992686). All must pin
    # the same-repository, non-fork, exact-head jq conditions; only the
    # draft arm requires .draft, only the two live arms require
    # .state == "open", and only the merged arm accepts .merged — the
    # sha comparison itself is never relaxed.
    for required_check, occurrences in (
        ('.state == "open"', 2),
        ('.base.ref == "main"', 3),
        (".head.repo.full_name == $repo", 3),
        (".head.repo.fork == false", 3),
        ("(.head.sha | ascii_downcase) == $sha", 3),
        (".merged == true", 1),
    ):
        assert source_body.count(required_check) == occurrences, required_check
    assert source_body.count(".draft == true") == 1
    assert "exact head of an open same-repository draft PR" in source_body
    # The merged-race arm is benign-exit only: it sets superseded (which
    # gates the later prepare steps and both speculative jobs off) and
    # stops; it never reaches the preview fetch. The wrong-head error
    # stays word-for-word, and the merged jq sits between the open check
    # and that error, so it can only fire once the open arm has refused.
    assert source_body.count('echo "superseded=true" >> "$GITHUB_OUTPUT"') == 1
    assert "Speculation superseded by merge" in source_body
    assert (
        "Speculative source must be the exact current head of the open "
        "same-repository PR." in source_body
    )
    merged_at = source_body.index(".merged == true")
    assert source_body.index('.state == "open"') < merged_at
    assert merged_at < source_body.index(
        "Speculative source must be the exact current head"
    )
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

    # SEVEN jobs hold the ECR push credential (see the note above the
    # deferred assignment for the roster and for why this is parsed, not
    # text-matched). The seventh is cve-harvest (D3, 2026-08-26): it reads
    # leaf-platform-* digests out of ECR to scan them and pushes nothing.
    oidc_grants = [
        l for l in structural
        if _key_of(l) == "id-token" and _value_of(l) == "write"
    ]
    assert len(oidc_grants) == 7, oidc_grants

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
                    "steps.surface.outputs.reuse != 'true' && "
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
    # The predecessor arm's non-empty guard. It used to be its own
    # `if [[ -n "$PREVIOUS_CACHE_TAG" ]]` wrapped around a dedicated probe;
    # with the four probes collapsed into one (2026-08-31) the same guard is
    # the candidate filter, which drops empty tags and de-duplicates. An
    # empty PREVIOUS_CACHE_TAG must never reach --image-ids as `imageTag=`.
    assert 'tags=("${CURRENT_CACHE_TAG:-}" "${PREVIOUS_CACHE_TAG:-}")' in text
    assert 'if [[ -n "$tag" && "$seen" != *" $tag "* ]]; then' in text
    assert "has no predecessor cache; building without cache input" in text

    # Cache growth has an explicit bounded-retention infrastructure contract.
    assert "expire buildcache-* tags" in text
    assert "after 14 days" in text

    # Handoff depends on the five-image manifest and an accepted staging
    # execution receipt. The historical tag-only production dispatch is gone.
    assert re.search(r"handoff:\s*\n\s+needs: \[prepare\]", text)
    verify_start = text.index("  verify:")
    verify_body = text[verify_start : text.index("\n  speculate:", verify_start)]
    assert "for image in app broker canonical-worker harness web; do" in verify_body
    assert "docker buildx imagetools inspect --raw" in verify_body
    assert '"$ECR_REGISTRY/$repository@$digest"' in verify_body
    assert "platform_release_manifest.py generate-v3" in verify_body
    assert "platform_release_manifest.py generate \\" not in verify_body
    assert "digest-web-dist --root dist" not in verify_body
    assert "--web-artifact-sha256" not in verify_body
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
    # Steps after the source step additionally gate on its merged-race
    # superseded output: past that arm there is nothing to tag, validate,
    # or hint for, and the tag step must not mint a prod-* value from the
    # PR-head checkout the preview never replaced.
    live_gate = (
        "steps.decide.outputs.build == 'true' && "
        "steps.source.outputs.superseded != 'true'"
    )
    assert step_ifs == [
        [], [], [docs_skip], [docs_skip],
        [docs_gate], [live_gate], [live_gate], [live_gate], [live_gate],
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
    # step carries exactly the TWO permitted conditions — the docs-noop
    # gate and the merged-race superseded gate, already pinned per step
    # above — and no other. Neither can starve a live warm: when
    # build == 'false' the warm job itself is gated off, and superseded
    # only ever sets on a speculative dispatch, where warm's
    # !inputs.speculative guard gates it off too; any OTHER condition
    # could. Pinned by exact value.
    hint_conditions = [
        _value_of(l) for l in hint_structural if _key_of(l) == "if"
    ]
    assert hint_conditions == [live_gate], hint_conditions
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
        # sol-critic #514 r4-r8: a `#` ANYWHERE in a post-ARG RUN now fails
        # loud (offends). The scanner no longer decides whether a `#` is a
        # plain comment, a comment inside a substitution/backtick, or mid-word
        # text: r4-r7 grew a lexer to tell those apart and each round surfaced
        # the next corner, culminating in r8's SILENT false-accept `$(#...)`
        # (bash comments the ref out inside the substitution, so it never
        # expands, yet the lexer read it as consuming). Any `#` is refused
        # instead. The six real guarded Dockerfiles contain no `#` in any RUN,
        # so none is affected. Genuine comment forms (r4/r5) still offend:
        ('RUN ["/bin/sh", "-c", "true;# ${LEAF_SOURCE_SHA:?expanded}"]', True),
        ('RUN echo hi  # ${LEAF_SOURCE_SHA}', True),
        ('RUN ["/bin/sh", "-c", "(true)# ${LEAF_SOURCE_SHA:?expanded}"]', True),
        ('RUN (true)# ${LEAF_SOURCE_SHA:?expanded}', True),
        # Mid-word `#` (r6/r7): bash WOULD expand the ref, but the scanner no
        # longer leans on that distinction -- a loud false-offend on a form no
        # Dockerfile uses beats modelling one more silent-false-accept corner.
        ('RUN echo ${PATH}# ${LEAF_SOURCE_SHA}', True),
        ('RUN {# ${LEAF_SOURCE_SHA}', True),
        ('RUN `printf x`# ${LEAF_SOURCE_SHA}', True),
        ('RUN echo $(printf x)# ${LEAF_SOURCE_SHA}', True),
        ('RUN echo $((1+1))# ${LEAF_SOURCE_SHA}', True),
        ('RUN echo \\)# ${LEAF_SOURCE_SHA}', True),
        # r8's false-ACCEPT `$(#...)` and its backtick twin `` `#...` ``: `#`
        # inside a command substitution or backtick is a comment, so bash does
        # NOT expand the ref (both print with LEAF_SOURCE_SHA unset). The r7
        # lexer read them as consuming; both must offend now.
        ('RUN ["/bin/sh", "-c", "echo $(# ${LEAF_SOURCE_SHA:?x}\\nprintf ok)"]', True),
        ('RUN echo `# ${LEAF_SOURCE_SHA}`', True),
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

    # THE THIRD-FLIP PIN. A CodeBuild runner may never coexist with a Docker Hub
    # base-image lookup. The fleet egresses through shared AWS NAT, so every
    # anonymous pull counts against Docker Hub's per-IP limit: #643 was reverted
    # by #648 on that, and #654 was reverted by #658 because pinning only the
    # BUILD's pulls is not enough -- the "Resolve one signed reusable surface"
    # step still resolved digests from `$base` and canary run 32046935487 took
    # the same 429 on the canonical-worker and harness legs. Both halves are
    # pinned here, and only in the dangerous direction: reverting `runs-on` to
    # ubuntu-latest must stay a one-line rollback, so every assertion below is
    # conditional on a CodeBuild runner actually being named.
    cache_prefix = "public-ecr/docker/library"
    codebuild_runners = text.count(
        "runs-on: codebuild-leaf-gha-runner-web-demo-"
        "${{ github.run_id }}-${{ github.run_attempt }}")

    base_case_arms = re.findall(
        r"^ +[\w-]+\) bases=\(([^)]*)\) ;;$", text, re.M)
    assert base_case_arms, (
        "the surface-resolve step's base-image case block vanished; this pin "
        "cannot see what it is meant to guard")
    declared_bases = {
        base for arm in base_case_arms for base in arm.split()}
    assert declared_bases == {
        "nginx:alpine",
        "node:20-slim",
        "node:22-bookworm",
        "node:22-slim",
        "python:3.12-slim",
        # Card F-3: Dockerfile.web's engine stage compiles the CAD wasm.
        "rust:1-slim",
    }, sorted(declared_bases)

    # One surface-resolve step per job that resolves base digests at all.
    resolve_steps = text.count("base_args=()")
    assert resolve_steps == 2, (
        "expected the build and speculate surface-resolve steps", resolve_steps)

    if codebuild_runners:
        # Both spellings, because #654's own line wrapped the target onto a
        # continuation line: a plain substring ban would have missed it.
        assert not re.search(
            r'imagetools inspect\s*\\?\s*"\$base"', text), (
            "a CodeBuild runner is named while base digests are still resolved "
            "from an unqualified Docker Hub reference: this is exactly the "
            "regression #658 reverted (run 32046935487, 429 on "
            "python:3.12-slim / node:22-bookworm)")
        assert text.count(
            f'"$ECR_REGISTRY/{cache_prefix}/$base"') == resolve_steps, (
            "every surface-resolve step must look base digests up through the "
            "pull-through cache while a CodeBuild runner is named")
        for image in sorted(declared_bases):
            assert (
                f"{image}=docker-image://${{{{ env.ECR_REGISTRY }}}}"
                f"/{cache_prefix}/{image}"
            ) in text, (
                "build-contexts must redirect %s through the pull-through "
                "cache while a CodeBuild runner is named" % image)

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
        release_role,  # cve-harvest: pulls the published digests to scan
    ], ("exactly seven role assumptions in the workflow, in job order "
        "warm, build, verify, speculate, speculate-manifest, adopt, "
        "cve-harvest")
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
        # warm: the CodeBuild page-cache prewarm (2026-08-31, pinned to a
        # dead loopback endpoint below, so it reaches no repository at all),
        # the chain-keeper decide step, and the cache-select step. build:
        # the prewarm, the signed-surface discovery and baked-source witness
        # probes, the exact release-output resume probe, and the cache-select
        # step. Cache-select is ONE probe per lane as of 2026-08-31: the
        # current-warm, predecessor, nearest-ancestor and pre-export
        # existence questions are four arms reading one batch-get-image
        # response, never an ECR listing (neither role is granted one).
        # A count that grows here is a probe fan-out and must be argued for.
        probes = [l for l in live if "--repository-name" in l]
        assert len(probes) == {"warm": 3, "build": 5}[lane], (lane, probes)
        # The prewarm is the ONLY thing in either lane allowed to redirect an
        # AWS endpoint, and there is exactly one of it. A live probe that grew
        # an --endpoint-url would be talking to something that is not ECR, and
        # a prewarm that lost one would be making a real call whose result
        # nothing checks.
        offline = [l for l in live if "--endpoint-url" in l]
        assert offline == ["            --endpoint-url http://127.0.0.1:1 \\"], (
            lane, offline)
        release_probes = [
            p for p in probes
            if re.search(r'--repository-name "\$IMAGE_NAME"(?!-)', p)
        ]
        assert len(release_probes) == {"warm": 0, "build": 3}[lane], (
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
        )) == {"warm": 0, "build": 3}[lane], lane
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
    # One release-repository existence probe; the predecessor import probe,
    # the nearest-ancestor fallback batch probe, and the CodeBuild page-cache
    # prewarm (2026-08-31) all name the cache repository, though the prewarm
    # reaches nothing: it is pinned to a dead loopback endpoint, asserted next.
    assert len(speculate_probes) == 3
    assert sorted(
        '"$IMAGE_NAME-buildcache"' in p for p in speculate_probes
    ) == [False, True, True], speculate_probes
    speculate_offline = [l for l in speculate_live if "--endpoint-url" in l]
    assert speculate_offline == [
        "            --endpoint-url http://127.0.0.1:1 \\"
    ], speculate_offline
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
        text.index('spec_run_id="$candidate_run"')
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
    # preference the warm job burns five runners and saves nothing. Since the
    # four probes collapsed into one (2026-08-31) the preference is no longer a
    # separate current-warm probe that simply runs first -- it is CANDIDATE
    # ORDER, so that is what is pinned here: this commit's tag seeds the list
    # ahead of the predecessor, exactly once, and the ancestor walk only ever
    # appends. FALLBACK_SCRIPT above already binds the selection loop itself
    # byte-exact, including its first-match `break`.
    assert 'tags=("${CURRENT_CACHE_TAG:-}" "${PREVIOUS_CACHE_TAG:-}")' in build_block
    assert build_block.count('tags=("${CURRENT_CACHE_TAG:-}"') == 1
    assert build_block.count('tags+=("buildcache-$short")') == 1
    assert (
        "::notice::$IMAGE_NAME reusing the warmed cache $CURRENT_CACHE_TAG"
        in build_block
    )

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
        # conditions: the exact-output resume guard or the signed exact-surface
        # reuse guard may skip a complete immutable image. Any other condition
        # could bypass the gate (sol-critic round 6:
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
            assert conditions == [
                "steps.surface.outputs.reuse != 'true' && "
                "steps.resume.outputs.skip != 'true'"
            ], (
                "the image push may only be skipped by an exact-output guard"
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
    # Bounded at cve-harvest, which sits between adopt and handoff since
    # D3 (2026-08-26). The roster is pinned deliberately: a new job must
    # declare itself here rather than silently widening a slice.
    adopt_block = text.split("\n  adopt:\n", 1)[1].split(
        "\n  cve-harvest:\n", 1
    )[0]

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
        "${{ github.event_name == 'workflow_dispatch' && inputs.speculative && "
        "needs.prepare.outputs.superseded != 'true' }}"
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

    # The harness apt cache key comes from the exact signed Debian channel
    # documents. Each real producer resolves both HTTPS resources itself,
    # validates one lowercase SHA256 for each, and passes the same values to
    # Docker. Build and speculate also bind them into the signed surface
    # fingerprint, so a channel update cannot reuse the prior signed image.
    resolver_name = "name: Resolve harness Debian InRelease digests"
    producer_blocks = (
        ("warm", warm_block),
        ("build", build_block),
        ("speculate", speculate_block),
    )
    expected_conditions = {
        "warm": "matrix.image == 'harness' && steps.chain.outputs.skip != 'true'",
        "build": "matrix.image == 'harness'",
        "speculate": "matrix.image == 'harness'",
    }
    security_url = (
        "https://deb.debian.org/debian-security/dists/"
        "bookworm-security/InRelease"
    )
    updates_url = (
        "https://deb.debian.org/debian/dists/bookworm-updates/InRelease"
    )
    for lane, block in producer_blocks:
        resolvers = [
            step
            for step in re.split(r"\n      - ", block)
            if step.startswith(resolver_name)
        ]
        assert len(resolvers) == 1, (lane, len(resolvers))
        resolver = _executable_bash(resolvers[0])
        assert resolver.count(security_url) == 1, lane
        assert resolver.count(updates_url) == 1, lane
        assert "curl --fail --silent --show-error --location" in resolver, lane
        assert "--proto '=https' --tlsv1.2" in resolver, lane
        assert "sha256sum \"$artifact\"" in resolver, lane
        assert '[[ "$digest" =~ ^[0-9a-f]{64}$ ]]' in resolver, lane
        assert 'echo "$output_name=$digest" >> "$GITHUB_OUTPUT"' in resolver, lane
        conditions = [
            _value_of(line)
            for line in resolvers[0].splitlines()
            if _key_of(line) == "if"
        ]
        assert conditions == [expected_conditions[lane]], (lane, conditions)

    harness_arg_outputs = {
        "HARNESS_DEBIAN_SECURITY_INRELEASE_SHA256": "security_sha256",
        "HARNESS_DEBIAN_UPDATES_INRELEASE_SHA256": "updates_sha256",
    }
    build_arg_input = _with_input(build_with, "build-args:")
    for argument, output in harness_arg_outputs.items():
        expression = (
            "${{ matrix.image == 'harness' && format('"
            f"{argument}={{0}}', steps.harness_debian.outputs.{output}) || '' }}}}"
        )
        assert expression in build_arg_input, argument
        assert text.count(expression) == 3, argument

    for lane, block, surface_name in (
        ("build", build_block, "name: Resolve one signed reusable surface"),
        ("speculate", speculate_block, "name: Describe the exact speculative surface"),
    ):
        surfaces = [
            step
            for step in re.split(r"\n      - ", block)
            if step.startswith(surface_name)
        ]
        assert len(surfaces) == 1, (lane, len(surfaces))
        surface = _executable_bash(surfaces[0])
        for argument, output in harness_arg_outputs.items():
            assert (
                f"{argument}: ${{{{ steps.harness_debian.outputs.{output} }}}}"
                in surface
            ), (lane, argument)
            assert f'[[ "${argument}" =~ ^[0-9a-f]{{64}}$ ]]' in surface, (
                lane,
                argument,
            )
            assert (
                f'build_args+=(--build-arg "{argument}=${argument}")' in surface
            ), (lane, argument)

    harness_dockerfile = (ROOT / "deploy" / "Dockerfile.harness").read_text(
        encoding="utf-8"
    )
    assert not re.search(r"^ADD\s+https://", harness_dockerfile, re.M)
    for argument in harness_arg_outputs:
        assert harness_dockerfile.count(f"ARG {argument}") == 1, argument
        assert (
            f'printf \'%s\\n\' "${argument}"' in harness_dockerfile
        ), argument
        assert (
            f'printf \'%s  %s\\n\' "${argument}"' in harness_dockerfile
        ), argument

    # The three python:3.12-slim images (app, broker, canonical-worker) carry
    # the harness contract against the TRIXIE channels: one producer-resolved
    # digest pair, shared across the three, passed to Docker and bound into
    # the signed surface fingerprint — so a Debian channel update both
    # invalidates their apt layers and refuses signed reuse of the pre-update
    # image (the libexpat1 CVE-2026-56408 defect class, applied preventively).
    trixie_resolver_name = "name: Resolve trixie Debian InRelease digests"
    trixie_members = (
        "matrix.image == 'app' || matrix.image == 'broker' || "
        "matrix.image == 'canonical-worker'"
    )
    trixie_expected_conditions = {
        "warm": f"({trixie_members}) && steps.chain.outputs.skip != 'true'",
        "build": trixie_members,
        "speculate": trixie_members,
    }
    trixie_security_url = (
        "https://deb.debian.org/debian-security/dists/"
        "trixie-security/InRelease"
    )
    trixie_updates_url = (
        "https://deb.debian.org/debian/dists/trixie-updates/InRelease"
    )
    for lane, block in producer_blocks:
        resolvers = [
            step
            for step in re.split(r"\n      - ", block)
            if step.startswith(trixie_resolver_name)
        ]
        assert len(resolvers) == 1, (lane, len(resolvers))
        resolver = _executable_bash(resolvers[0])
        assert resolver.count(trixie_security_url) == 1, lane
        assert resolver.count(trixie_updates_url) == 1, lane
        assert "curl --fail --silent --show-error --location" in resolver, lane
        assert "--proto '=https' --tlsv1.2" in resolver, lane
        assert "sha256sum \"$artifact\"" in resolver, lane
        assert '[[ "$digest" =~ ^[0-9a-f]{64}$ ]]' in resolver, lane
        assert 'echo "$output_name=$digest" >> "$GITHUB_OUTPUT"' in resolver, lane
        conditions = [
            _value_of(line)
            for line in resolvers[0].splitlines()
            if _key_of(line) == "if"
        ]
        assert conditions == [trixie_expected_conditions[lane]], (lane, conditions)

    trixie_arg_outputs = {
        "TRIXIE_DEBIAN_SECURITY_INRELEASE_SHA256": "security_sha256",
        "TRIXIE_DEBIAN_UPDATES_INRELEASE_SHA256": "updates_sha256",
    }
    for argument, output in trixie_arg_outputs.items():
        expression = (
            f"${{{{ ({trixie_members}) && format('"
            f"{argument}={{0}}', steps.trixie_debian.outputs.{output}) || '' }}}}"
        )
        assert expression in build_arg_input, argument
        assert text.count(expression) == 3, argument

    for lane, block, surface_name in (
        ("build", build_block, "name: Resolve one signed reusable surface"),
        ("speculate", speculate_block, "name: Describe the exact speculative surface"),
    ):
        surface = _executable_bash(next(
            step
            for step in re.split(r"\n      - ", block)
            if step.startswith(surface_name)
        ))
        for argument, output in trixie_arg_outputs.items():
            assert (
                f"{argument}: ${{{{ steps.trixie_debian.outputs.{output} }}}}"
                in surface
            ), (lane, argument)
            assert f'[[ "${argument}" =~ ^[0-9a-f]{{64}}$ ]]' in surface, (
                lane,
                argument,
            )
            assert (
                f'build_args+=(--build-arg "{argument}=${argument}")' in surface
            ), (lane, argument)

    for trixie_image in ("app", "broker", "canonical-worker"):
        trixie_dockerfile = (
            ROOT / "deploy" / f"Dockerfile.{trixie_image}"
        ).read_text(encoding="utf-8")
        assert not re.search(
            r"^ADD\s+https://", trixie_dockerfile, re.M
        ), trixie_image
        for argument in trixie_arg_outputs:
            assert trixie_dockerfile.count(f"ARG {argument}") == 1, (
                trixie_image, argument)
            assert (
                f'printf \'%s\\n\' "${argument}"' in trixie_dockerfile
            ), (trixie_image, argument)
            assert (
                f'printf \'%s  %s\\n\' "${argument}"' in trixie_dockerfile
            ), (trixie_image, argument)
        # The upgrade itself, on executable lines only: a mention in prose
        # can neither satisfy nor break it.
        trixie_executable = "\n".join(
            line
            for line in trixie_dockerfile.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        )
        assert "apt-get upgrade -y" in trixie_executable, trixie_image

    # The web libexpat refresh follows the same producer-bound cache contract,
    # but against the exact Alpine main APKINDEX exposed by nginx:alpine. Each
    # producer reads the versioned repository from that base, validates the
    # official HTTPS endpoint, hashes the current index bytes, and passes that
    # value to both Docker and the signed surface fingerprint.
    web_resolver_name = "name: Resolve web Alpine main APKINDEX digest"
    web_expected_conditions = {
        "warm": "matrix.image == 'web' && steps.chain.outputs.skip != 'true'",
        "build": "matrix.image == 'web'",
        "speculate": "matrix.image == 'web'",
    }
    for lane, block in producer_blocks:
        resolvers = [
            step
            for step in re.split(r"\n      - ", block)
            if step.startswith(web_resolver_name)
        ]
        assert len(resolvers) == 1, (lane, len(resolvers))
        resolver = _executable_bash(resolvers[0])
        assert (
            '--entrypoint cat "$NGINX_ALPINE_BASE" /etc/apk/repositories'
            in resolver
        ), lane
        assert (
            r"^https://dl-cdn\.alpinelinux\.org/alpine/"
            r"v[0-9]+\.[0-9]+/main/?$" in resolver
        ), lane
        assert '"${main_repo%/}/x86_64/APKINDEX.tar.gz"' in resolver, lane
        assert "curl --fail --silent --show-error --location" in resolver, lane
        assert "--proto '=https' --tlsv1.2" in resolver, lane
        assert 'sha256sum "$artifact"' in resolver, lane
        assert '[[ "$digest" =~ ^[0-9a-f]{64}$ ]]' in resolver, lane
        assert 'echo "main_sha256=$digest" >> "$GITHUB_OUTPUT"' in resolver, lane
        conditions = [
            _value_of(line)
            for line in resolvers[0].splitlines()
            if _key_of(line) == "if"
        ]
        assert conditions == [web_expected_conditions[lane]], (lane, conditions)

    web_argument = "WEB_ALPINE_MAIN_APKINDEX_SHA256"
    web_expression = (
        "${{ matrix.image == 'web' && format('"
        f"{web_argument}={{0}}', steps.web_alpine.outputs.main_sha256) || '' }}}}"
    )
    assert web_expression in build_arg_input
    assert text.count(web_expression) == 3
    for lane, block, surface_name in (
        ("build", build_block, "name: Resolve one signed reusable surface"),
        ("speculate", speculate_block, "name: Describe the exact speculative surface"),
    ):
        surface = _executable_bash(next(
            step
            for step in re.split(r"\n      - ", block)
            if step.startswith(surface_name)
        ))
        assert (
            f"{web_argument}: ${{{{ steps.web_alpine.outputs.main_sha256 }}}}"
            in surface
        ), lane
        assert f'[[ "${web_argument}" =~ ^[0-9a-f]{{64}}$ ]]' in surface, lane
        assert (
            f'build_args+=(--build-arg "{web_argument}=${web_argument}")'
            in surface
        ), lane

    web_dockerfile = (ROOT / "deploy" / "Dockerfile.web").read_text(
        encoding="utf-8"
    )
    assert web_dockerfile.count(f"ARG {web_argument}") == 1
    assert 'apk --repositories-file "$repositories_file" update' in web_dockerfile
    assert f'printf \'%s  %s\\n\' "${web_argument}" "$1"' in web_dockerfile
    assert 'apk --repositories-file "$repositories_file" upgrade libexpat' in (
        web_dockerfile
    )
    assert 'apk version -t "$installed_version" 2.8.4-r0' in web_dockerfile
    assert not re.search(r"^ADD\s+https://", web_dockerfile, re.M)

    # The speculative app leg must mint the same zstd compression as the
    # gated build leg: adoption aliases the speculative digest onto the
    # immutable sha-* tags, so a gzip spec image would keep every merge on
    # gzip no matter what the full-build leg does.
    assert re.search(r"^          push: \$\{\{ matrix\.image != 'app' \}\}$",
                     speculate_with, re.M)
    assert _with_input(speculate_with, "outputs:") == _with_input(
        build_with, "outputs:"
    ), "speculate and build must carry a byte-identical outputs: input"
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
    assert "Describe the exact speculative surface" in speculate_block
    assert "platform_release_manifest.py surface-fingerprint" in speculate_block
    assert 'echo "lookup_tag=surface-v1-$fingerprint"' in speculate_block
    assert "Materialize one producer-owned speculative v3 service entry" in (
        speculate_block
    )
    assert "platform_release_manifest.py surface-predicate" in speculate_block
    assert 'WORKFLOW_BLOB: ${{ needs.prepare.outputs.workflow_blob }}' in (
        speculate_block
    )
    assert 'build_disposition: "built"' in speculate_block
    assert "Upload one producer-owned speculative v3 service entry" in (
        speculate_block
    )
    assert "pack-web-dist \\\n            --root dist" in speculate_block
    assert "--output \"$RUNNER_TEMP/spec-web-dist.zip\"" in speculate_block
    assert "Upload the provider-bound speculative web deployment artifact" in (
        speculate_block
    )
    assert (
        "name: spec-web-dist-${{ needs.prepare.outputs.source_sha }}-attempt-"
        "${{ github.run_attempt }}"
        in speculate_block
    )
    assert "path: ${{ runner.temp }}/spec-web-dist.zip" in speculate_block
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

    # The partial-push invariant over tree identity: no provider-bound v3
    # artifact without all five live digests and all five current-run service
    # entries. The mint and upload use separate, monotonic completion outputs.
    manifest_guard = (
        "    if: >-\n"
        "      ${{ !cancelled() && github.event_name == 'workflow_dispatch' &&\n"
        "          inputs.speculative && needs.prepare.result == 'success' &&\n"
        "          needs.prepare.outputs.superseded != 'true' }}\n"
    )
    assert text.count(manifest_guard) == 1
    assert manifest_guard in manifest_block
    assert "complete=false" in manifest_block
    assert "the speculative set is incomplete and no manifest will be minted" in (
        manifest_block
    )
    assert "generate-v3" in manifest_block
    assert "generate-speculative" not in manifest_block
    assert "spec-surface-result-*" in manifest_block
    assert ".producer_source_revision == $source" in manifest_block
    assert ".producer_source_tree == $tree" in manifest_block
    assert ".producer_run_id == $run" in manifest_block
    assert ".producer_run_attempt == $attempt" in manifest_block
    assert '.build_disposition == "built"' in manifest_block
    mint_or_upload = [
        s
        for s in re.split(r"\n      - ", manifest_block)
        if s.startswith("name: Mint the provider-bound speculative v3 supply set")
        or "uses: actions/upload-artifact" in s
    ]
    assert len(mint_or_upload) == 2
    assert [
        _value_of(l) for l in mint_or_upload[0].splitlines() if _key_of(l) == "if"
    ] == ["steps.digests.outputs.complete == 'true'"]
    assert [
        _value_of(l) for l in mint_or_upload[1].splitlines() if _key_of(l) == "if"
    ] == ["steps.evidence.outputs.complete == 'true'"]
    assert (
        "name: spec-v3-supply-set-${{ needs.prepare.outputs.source_tree }}"
        in manifest_block
    )
    assert "path: ${{ runner.temp }}/spec-v3-supply-set.json" in manifest_block
    assert "if-no-files-found: error" in manifest_block

    # adopt: absorbed like the gate-reuse probe (a defect costs the
    # optimization, never the push run), reads artifacts with an explicit
    # read-only Actions grant, and follows the probe's provenance
    # discipline: same-repo origin from workflow_run metadata, this
    # workflow's bare path, a main-ref workflow_dispatch run. The provider ZIP
    # digest and closed archive are verified, then the speculative v3 is
    # re-enveloped under this main run BEFORE any tag is written. Only the
    # release prod- namespace may be aliased, and every alias re-verifies.
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
    assert (
        '[[ "$path" == ".github/workflows/build-platform-images.yml" ]] '
        '|| finish false "speculative provider workflow is foreign"'
    ) in adopt_block
    assert (
        '[[ "$(jq -r \'.event // empty\' <<<"$run_record")" == '
        '"workflow_dispatch" ]]'
    ) in adopt_block
    assert (
        '[[ "$(jq -r \'.head_branch // empty\' <<<"$run_record")" == "main" ]]'
    ) in adopt_block
    assert "re-envelope-speculative-v3" in adopt_block
    assert "verify-speculative" not in adopt_block
    assert "--expect-candidate-tree \"$tree\"" in adopt_block
    assert "--expect-workflow-blob \"$candidate_workflow_blob\"" in adopt_block
    assert '--release-source-revision "$SOURCE_SHA"' in adopt_block
    assert '--build-run-id "$GITHUB_RUN_ID"' in adopt_block
    assert 'artifact_name="spec-v3-supply-set-$tree"' in adopt_block
    assert 'actual_archive_digest="sha256:$(sha256sum' in adopt_block
    assert '[ "${#archive_paths[@]}" = "1" ]' in adopt_block
    assert 'candidate_source="$(jq -r' in adopt_block
    # The spec tag binds tree plus the speculative producer source carried by
    # the independently verified v3 service entries.
    assert 'spec_tag="spec-$tree-${candidate_source:0:12}"' in adopt_block
    assert "spec-[0-9a-f]{40}-[0-9a-f]{12}" in adopt_block
    assert adopt_block.index("re-envelope-speculative-v3") < adopt_block.index(
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
        "${{ github.event_name == 'workflow_dispatch' && inputs.speculative && "
        "needs.prepare.outputs.superseded != 'true' }}"
    )
    assert wf_jobs["adopt"]["if"] == (
        "${{ !inputs.promote && github.event_name == 'push' && "
        "needs.prepare.outputs.build == 'true' }}"
    )
    assert _folded(wf_jobs["speculate-manifest"]["if"]) == (
        "${{ !cancelled() && github.event_name == 'workflow_dispatch' && "
        "inputs.speculative && needs.prepare.result == 'success' && "
        "needs.prepare.outputs.superseded != 'true' }}"
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
    digest_aware_build_guard = (
        "steps.surface.outputs.reuse != 'true' && "
        "steps.resume.outputs.skip != 'true'"
    )
    assert cache_step["if"] == digest_aware_build_guard
    assert build_steps[build_push_at]["if"] == (
        digest_aware_build_guard
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
        "${{ env.DIGEST_AWARE_CONVERGENCE_ENABLED == 'true' && "
        "format('{0}/{1}:surface-v1-{2}', env.ECR_REGISTRY, "
        "env.IMAGE_NAME, steps.surface.outputs.fingerprint) || '' }}",
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
        steps = wf_jobs[job_name]["steps"]
        # Exactly one step may precede the checkout: the CodeBuild page-cache
        # prewarm (2026-08-31). It must stay a plain `run:` -- anything that
        # reads the repository cannot run before the tree exists, and an
        # `uses:` here would put a third-party action ahead of the source it
        # is meant to be building.
        if str(steps[0].get("name", "")) == "Prewarm the AWS CLI page cache":
            assert "uses" not in steps[0], job_name
            steps = steps[1:]
        first_step = steps[0]
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
    # cve-harvest joined the set with D3 (2026-08-26): it reaches the
    # release role only to PULL the published digests it scans.
    release_env_jobs = {"build", "verify", "speculate", "speculate-manifest",
                        "adopt", "cve-harvest"}
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

    # The full-build verifier consumes matrix-owned v3 entries and the exact
    # web artifact. The adopted path uses the main-run v3 envelope that the
    # decide step re-materialized from the speculative producer evidence.
    verify_steps = wf_jobs["verify"]["steps"]

    def _sole_named(steps, name):
        found = [s for s in steps if s.get("name") == name]
        assert len(found) == 1, name
        return found[0]

    write_step_name = "Write the immutable five-service staging supply set"
    adopt_web = _sole_named(
        adopt_steps, "Verify the provider-bound web source restamp"
    )

    # ---- D3 CVE harvest (2026-08-26) ---------------------------------
    # This job is the repo's only CVE gate on what actually deploys, and it
    # is deliberately toothless in phase 1. Pin the whole contract so a
    # later edit cannot leave a HALF-FLIPPED gate that looks armed and
    # blocks nothing.
    harvest = wf_jobs["cve-harvest"]
    harvest_steps = harvest["steps"]

    # It must scan what DEPLOYS, not what this run happened to build. A gate
    # keyed on the build job's own digest is bypassed three ways, all live:
    # an adopted merge skips the whole matrix, the resume arm skips
    # build-image when the exact tags exist, and the surface-reuse arm does
    # the same. Reading the supply-set artifact is what makes this
    # bypass-proof, so the download and the digest resolve are both pinned.
    assert harvest["needs"] == ["prepare", "verify", "adopt"]
    download = next(
        s for s in harvest_steps
        if str(s.get("uses", "")).startswith("actions/download-artifact@")
    )
    assert download["with"]["name"] == "${{ env.SUPPLY_SET }}"
    assert harvest["env"]["SUPPLY_SET"] == (
        "staging-supply-set-${{ needs.prepare.outputs.source_sha }}"
        "-attempt-${{ github.run_attempt }}"
    )
    resolve = next(s for s in harvest_steps if s.get("id") == "digests")
    assert "leaf.staging-supply-set.v3" in resolve["run"]

    # All five services, every one scanned, on the same pinned action the
    # instant-execution gate already uses. A sixth service added to the
    # release matrix without a scan step must fail here.
    scan_steps = [
        s for s in harvest_steps
        if str(s.get("uses", "")).startswith("aquasecurity/trivy-action@")
    ]
    assert [s["name"] for s in scan_steps] == [
        "Scan app",
        "Scan broker",
        "Scan canonical-worker",
        "Scan harness",
        "Scan web",
    ], [s.get("name") for s in scan_steps]
    assert {s["uses"] for s in scan_steps} == {
        "aquasecurity/trivy-action@v0.36.0"
    }
    for step in scan_steps:
        assert step["with"]["severity"] == "HIGH,CRITICAL", step["name"]
        assert step["with"]["ignore-unfixed"] is True, step["name"]

    # THE INVARIANT: the job blocks if and only if its scans block.
    # exit-code 0 everywhere plus continue-on-error is a coherent
    # report-only phase 1; exit-code 1 everywhere with no continue-on-error
    # is a coherent blocking phase 2. Every other combination is a gate that
    # lies, so flipping one without the other fails this test by
    # construction.
    exit_codes = {str(s["with"]["exit-code"]) for s in scan_steps}
    assert exit_codes in ({"0"}, {"1"}), exit_codes
    report_only = exit_codes == {"0"}
    assert harvest.get("continue-on-error", False) is report_only, (
        "cve-harvest: exit-code and continue-on-error disagree, so the gate "
        "either blocks nothing while looking armed, or reddens main on "
        "findings it was only meant to record"
    )

    assert "leaf.web-source-restamp.v1" in adopt_web["run"]
    assert "spec-candidate/restamped-web/dist" in adopt_web["run"]
    assert "steps.decide.outputs.built_from" in adopt_web["run"]
    adopt_write = _sole_named(adopt_steps, write_step_name)
    verify_write = _sole_named(verify_steps, write_step_name)
    assert verify_write.get("env") is None
    assert adopt_write.get("env") is None
    assert "platform_release_manifest.py generate-v3" in verify_write["run"]
    assert "platform_release_manifest.py generate \\" not in verify_write["run"]
    assert "surface-results" in verify_write["run"]
    assert "spec-candidate/main-v3-supply-set.json" in adopt_write["run"]
    assert "platform_release_manifest.py generate-v3" not in adopt_write["run"]
    assert ".services.web.artifact_sha256" in adopt_write["run"]
    assert ".new_artifact_sha256" in adopt_write["run"]
    assert "source-restamp receipt" in adopt_write["run"]
    assert "cp spec-candidate/main-v3-supply-set.json" in adopt_write["run"]
    decide_run = adopt_steps[4]["run"]
    assert "actions/runs/$candidate_run/artifacts?per_page=100" in decide_run
    assert "spec-web-dist-$candidate_source-attempt-$candidate_run_attempt" in decide_run
    assert "spec-candidate/web-provider.zip" in decide_run
    assert "restamp-web-artifact" in decide_run
    assert "--web-restamp-receipt spec-candidate/web-source-restamp.json" in decide_run
    assert "rebuilt web artifact does not match" not in decide_run
    # Adopt keeps its legacy tag aliases while the full-build verifier binds
    # immutable digests from the five v3 service entries.
    assert wf_jobs["adopt"]["env"]["TAG"] == wf_jobs["verify"]["env"]["TAG"]
    assert wf_jobs["adopt"]["env"]["TAG"] == wf_jobs["adopt"]["env"]["PROD_TAG"]
    # Both mutually exclusive paths still fail closed on missing artifacts.
    adopt_uploads = [
        s for s in adopt_steps
        if str(s.get("uses", "")).startswith("actions/upload-artifact")
    ]
    verify_uploads = [
        s for s in verify_steps
        if str(s.get("uses", "")).startswith("actions/upload-artifact")
    ]
    assert len(adopt_uploads) == 3
    assert len(verify_uploads) == 2
    assert adopt_uploads[0]["with"]["if-no-files-found"] == "error"
    assert verify_uploads[0]["with"]["if-no-files-found"] == "error"
    downloads = [
        s for s in verify_steps
        if str(s.get("uses", "")).startswith("actions/download-artifact")
    ]
    assert len(downloads) == 2
    assert downloads[0]["with"]["pattern"].startswith("surface-result-")
    assert downloads[1]["with"]["name"].startswith("surface-web-dist-")
    adopt_nodes = [
        s for s in adopt_steps
        if str(s.get("uses", "")).startswith("actions/setup-node")
    ]
    verify_nodes = [
        s for s in verify_steps
        if str(s.get("uses", "")).startswith("actions/setup-node")
    ]
    assert adopt_nodes == []
    assert verify_nodes == []

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
        (
            "build",
            cache_step_name,
            "steps.surface.outputs.reuse != 'true' && "
            "steps.resume.outputs.skip != 'true'",
        ),
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
    # The merged-race arm's benign exit travels as a first-class prepare
    # output; the speculate / speculate-manifest guards and the late
    # prepare step gates all read exactly this.
    assert prepare_job["outputs"]["superseded"] == (
        "${{ steps.source.outputs.superseded }}"
    )
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
    # Coalesced across commits (2026-08-24): the group used to be keyed by
    # head_sha, so every commit's relay ran in its own group and an
    # unbounded number could run concurrently, racing for the infra repo's
    # one shared ecs-mutation lock. Fixed group + cancel-in-progress mirrors
    # the same "merge-burst coalescing" pattern already pinned above for
    # build-platform-images.yml's push runs: a newer commit's relay cancels
    # an older one instead of letting both run, and it is safe because every
    # dispatch always targets the CANCELLING relay's own current tag.
    assert relay_wf["concurrency"] == {
        "group": "dispatch-staging-deploys",
        "cancel-in-progress": True,
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
    # BUILD_RUN_ATTEMPT moved from the manifest step to the JOB on 2026-08-07
    # with the convergence receipt. The manifest step names the build's own
    # supply set and marker with it, and the receipt publish step names the
    # RECEIPT with it; one definition keeps those names on the same key.
    assert dispatch_job["env"] == {
        "INFRA_REPO": "LEAF-Solar-Design/leaf-automation-aws-terraform",
        "DEPLOY_WORKFLOW": "deploy-leaf-platform-staging.yml",
        "BUILD_RUN_ID": "${{ github.event.workflow_run.id }}",
        "BUILD_HEAD_SHA": "${{ github.event.workflow_run.head_sha }}",
        "BUILD_RUN_ATTEMPT": "${{ github.event.workflow_run.run_attempt }}",
        "DIGEST_AWARE_CONVERGENCE_ENABLED": "true",
        "DIGEST_AWARE_CONSUMER_MARKER": "leaf.staging-digest-aware-consumer.v1",
        "CONSUMER_CONTRACT_WORKFLOW": "publish-leaf-platform-staging-consumer-contract.yml",
        "CONSUMER_CONTRACT_WORKFLOW_PATH": ".github/workflows/publish-leaf-platform-staging-consumer-contract.yml",
    }
    assert dispatch_job["if"] == (
        "github.event.workflow_run.conclusion == 'success' && "
        "github.event.workflow_run.event == 'push' && "
        "github.event.workflow_run.head_branch == 'main'"
    )
    relay_steps = dispatch_job["steps"]
    # Five steps: tip check, manifest read, provider contract read, guarded
    # dispatch, and receipt publish. The fifth is the only step with `uses:`.
    # reordering must break this harness and force a co-review.
    assert [s.get("id") for s in relay_steps] == [
        "tip", "manifest", "consumer_contract", "deploy", None]
    tip_step, manifest_step, contract_step, dispatch_step, receipt_step = relay_steps
    # Step KEY SETS are exact: no shell:, no working-directory:,
    # no continue-on-error: may appear on any step without breaking this.
    assert set(tip_step) == {"name", "id", "env", "run"}
    assert set(manifest_step) == {"name", "id", "if", "env", "run"}
    assert set(contract_step) == {"name", "id", "if", "env", "run"}
    assert set(dispatch_step) == {"name", "id", "if", "env", "run"}
    # THE RECEIPT STEP RUNS NO SCRIPT AND HOLDS NO TOKEN. It is a pure upload
    # of a file the guarded step already wrote, so it carries no `env:` at all
    # and cannot be the place a dispatch is smuggled in. Its `uses`, `if` and
    # `with` are pinned inside check_staging_relay_convergence, where the decoy
    # battery exercises them; a receipt published by a relay that stood down is
    # a lie every future waiter believes, so it belongs to the convergence
    # contract.
    assert set(receipt_step) == {"name", "if", "uses", "with"}
    # Step ENV dicts are exact: the unguarded steps hold only the
    # read-scoped workflow token, never the infra-repo PAT.
    assert tip_step["env"] == {"GH_TOKEN": "${{ github.token }}"}
    assert manifest_step["env"] == {"GH_TOKEN": "${{ github.token }}"}
    assert contract_step["env"] == {
        "GH_TOKEN": "${{ secrets.TERRAFORM_REPO_TOKEN }}"
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
        "SUPPLY_SCHEMA": "${{ steps.manifest.outputs.schema }}",
        "SUPPLY_SHA256": "${{ steps.manifest.outputs.artifact_sha256 }}",
        "SUPPLY_ARTIFACT_ID": "${{ steps.manifest.outputs.artifact_id }}",
        "SUPPLY_ARTIFACT_NAME": "${{ steps.manifest.outputs.artifact_name }}",
        "SUPPLY_EVIDENCE_B64": (
            "${{ steps.manifest.outputs.supply_evidence_b64 }}"
        ),
        # The ENVELOPE'S release identity, not the tip's. On the docs-only
        # reconcile these name the candidate build whose images are deployed,
        # and the convergence id is composed from them.
        "RELEASE_SOURCE": "${{ steps.manifest.outputs.release_source }}",
        "RELEASE_ATTEMPT": "${{ steps.manifest.outputs.release_attempt }}",
        "CONSUMER_CONTRACT_B64": (
            "${{ steps.consumer_contract.outputs.consumer_contract_b64 }}"
        ),
        "TF_CONTRACT_HEAD": "${{ steps.consumer_contract.outputs.terraform_head_sha }}",
        "TF_CONSUMER_BLOB": "${{ steps.consumer_contract.outputs.deploy_workflow_blob }}",
        "TF_CONTRACT_RUN_ID": "${{ steps.consumer_contract.outputs.producer_run_id }}",
    }
    assert manifest_step["if"] == "steps.tip.outputs.current == 'true'"
    assert _folded(contract_step["if"]) == (
        "steps.tip.outputs.current == 'true' && "
        "steps.manifest.outputs.deploy == 'true' && "
        "steps.manifest.outputs.schema == 'leaf.staging-supply-set.v3'"
    )
    assert dispatch_step["if"] == (
        "steps.tip.outputs.current == 'true' && "
        "steps.manifest.outputs.deploy == 'true'"
    ), "without this exact guard a docs-only run dispatches an empty tag"

    # The PAT appears only in the provider read and guarded dispatch steps.
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
    assert len(secret_refs) == 2, (
        f"exactly two scoped secret references may exist in the relay: {secret_refs}"
    )
    assert all(value == "${{ secrets.TERRAFORM_REPO_TOKEN }}" for _, value in secret_refs)
    assert {path.rsplit(".", 2)[-2] for path, _ in secret_refs} == {"env"}
    assert any(".steps[2].env.GH_TOKEN" in path for path, _ in secret_refs)
    assert any(".steps[3].env.GH_TOKEN" in path for path, _ in secret_refs)

    tip_code = _executable_bash(tip_step["run"])
    manifest_code = _executable_bash(manifest_step["run"])
    contract_code = _executable_bash(contract_step["run"])
    dispatch_code = _executable_bash(dispatch_step["run"])

    # ALL THREE scripts are content-frozen: any edit to their executable
    # text (comments excluded) must update this hash in the same PR, so
    # neither an assembled dispatch in an unguarded step nor an extra
    # PAT-backed command under the guarded step's healthy-looking guard
    # can land silently. The property checks around this stay for
    # readable failures; the hash is the contract, and the capability
    # wall means the unguarded scripts hold no credential able to
    # dispatch even if edited.
    # A COMMENT IS NOT ALWAYS INERT, so the freeze cannot assume it is.
    #
    # Bash removes backslash-newline BEFORE it processes comments, so a
    # full-line comment placed after a continued line is spliced INTO that
    # command and comments out the remainder of it. `_executable_bash` instead
    # drops comment lines and joins the continuation to the next CODE line, so
    # such an edit changes what bash actually runs while leaving the frozen
    # hash below byte-identical. sol-critic demonstrated exactly that on
    # PR #508: the mutated script failed under bash and still hashed to
    # 97ae6184…9214a. Refuse the construct in the frozen scripts outright,
    # which is stricter than modelling it and cannot itself drift.
    for step_name, raw in (("tip", tip_step["run"]),
                           ("manifest", manifest_step["run"]),
                           ("consumer_contract", contract_step["run"]),
                           ("dispatch", dispatch_step["run"])):
        raw_lines = raw.splitlines()
        for idx, line in enumerate(raw_lines[:-1]):
            if (line.rstrip().endswith("\\")
                    and raw_lines[idx + 1].lstrip().startswith("#")):
                raise AssertionError(
                    f"{step_name} step line {idx + 2}: a full-line comment "
                    "follows a line continuation. Bash splices it into the "
                    "continued command and comments out the rest, so this "
                    "changes behaviour while the frozen hash stays identical. "
                    "Put the comment above the statement instead.")

    frozen = hashlib.sha256(
        "\n===\n".join((tip_code, manifest_code, contract_code, dispatch_code)).encode("utf-8")
    ).hexdigest()
    # Hash updated 2026-08-07: the dispatch step now deploys one service at a
    # time and watches each run to a terminal state, instead of firing both
    # deploys two seconds apart and exiting. Reviewed for dispatch capability:
    # still exactly one `gh workflow run` site with the same four inputs, one
    # added read of THIS repo's tip on the workflow's own read-only token, and
    # no new secret reference (the count assertion above is unchanged).
    #
    # Hash updated 2026-08-07 (b): need-first ordering. The dispatch step reads
    # each service's newest successful infra deploy run name and, when the
    # ancestry compare says app trails web, deploys app first. Reviewed for
    # dispatch capability: STILL exactly one `gh workflow run` site with the
    # same four inputs; the added calls are two `gh run list` reads on the
    # infra repo the step already lists runs from, and one `compare` read of
    # THIS repo on the same read-only HOME_TOKEN the tip re-check already uses.
    # No new secret reference. The read may only REORDER — it never assigns
    # IMAGE_TAG and never shortens SERVICES, both pinned above.
    #
    # Hash updated 2026-08-07 (c): TEXT ONLY, and deliberately not exempted
    # from the freeze. Two operator-facing strings changed — the app-first
    # notice and the STAGING IS SPLIT warning — because both promised a
    # convergence the relay cannot guarantee (review rounds 2, 3 and 4 of
    # PR #506 were all RED for that same overclaim, in four different
    # places). Message text lives inside the executable script, so it moves
    # this hash even though no control flow changed. Reviewed for dispatch
    # capability: STILL exactly one `gh workflow run` site with the same four
    # inputs, still exactly one `secrets.` reference in the whole workflow, no
    # new command, no new read, no new token. Verified by recomputation, not
    # assumed: the only diff in the extracted script text is inside two echo
    # arguments.
    # Hash updated 2026-08-07 (second edit, same day): the dispatch step now
    # classifies a mid-release superseder from its own build receipt, fails
    # RED when it leaves staging split with no converger, and finishes the
    # release when the superseder is a docs-only no-op that would deploy
    # nothing. Reviewed for dispatch capability: still exactly ONE
    # `gh workflow run` site with the same four inputs (the count assertion
    # above is unchanged), no new secret reference, and the three added calls
    # are read-only `gh api` GETs against THIS repo — runs, run artifacts,
    # and one run — each explicitly re-bound to the workflow's own read-only
    # token with `GH_TOKEN="$HOME_TOKEN"`, matching the existing tip read, so
    # the infra-repo PAT gains no new command.
    # Hash updated again 2026-08-07 (third edit): the docs-noop arm now
    # re-reads the tip AFTER its classification poll and fails RED on a moved
    # tip, closing an interleaving an external review found and this repo
    # reproduced. Reviewed for dispatch capability: no change to the dispatch
    # itself, still ONE `gh workflow run` with the same four inputs, no new
    # secret reference; the one added call is a read-only GET of this repo's
    # own branch tip on `GH_TOKEN="$HOME_TOKEN"`, identical in form and token
    # to the tip read already at the top of the loop.
    # Hash updated again 2026-08-07 (fourth edit): sol-critic returned RED on
    # PR #508 against the third edit, and all three P1s reproduced. The
    # classifier now (a) fails CLOSED on every read instead of suppressing a
    # failed artifact listing with `|| true`, which let a docs-only superseder
    # classify as `yes` on the strength of its build concluding success, (b)
    # binds the docs-noop marker to the run's CURRENT attempt, since artifacts
    # are keyed by run id and an attempt-1 marker otherwise outlives a rerun
    # whose attempt 2 built real images, and (c) selects the superseding build
    # by workflow PATH rather than display name, which any other workflow can
    # also carry. Marker absence is additionally only trusted against a
    # listing proven whole. Reviewed for dispatch capability: UNCHANGED. Still
    # exactly ONE `gh workflow run` with the same four inputs, still exactly
    # one secret reference, and the classifier still makes the same THREE
    # read-only `gh api` GETs against THIS repo — the run list, one run
    # record, and that run's artifacts — on `GH_TOKEN="$HOME_TOKEN"`. No new
    # endpoint, no new repo, no new credential: only the handling of their
    # failures and the strictness of their matching changed.
    # Hash updated again 2026-08-07 (fifth edit): sol-critic round 2 on PR
    # #508. `yes` no longer rests on a green conclusion alone — it now
    # requires the superseder's exact attempt-bound supply-set artifact,
    # because a build can conclude success and publish nothing, in which case
    # the manifest step above fails LOUD on the missing supply set and that
    # relay converges nothing. Artifact completeness additionally requires the
    # response to carry a numeric `total_count` and an array of artifacts, and
    # both JSON parses now fail closed explicitly. Finally, a deploy dispatched
    # over a tip we had to clear re-reads the tip AFTER it lands and fails RED
    # if it moved, which closes the read-to-dispatch window for REPORTING
    # (preventing the stale write needs in-lock validation in the infra repo).
    # Reviewed for dispatch capability: still exactly ONE `gh workflow run`
    # with the same four inputs and still exactly one secret reference. One
    # call is added, a read-only GET of this repo's own branch tip on
    # `GH_TOKEN="$HOME_TOKEN"`, identical in form and token to the two tip
    # reads already present. No new endpoint, repo or credential.
    # Hash updated again 2026-08-07 (sixth edit): sol-critic round 3 on PR
    # #508. The `yes` arm now re-reads the tip after its classification poll
    # and fails RED if main moved, because `yes` was a claim about a commit
    # that could already have stopped being the tip: A polls deployable B,
    # docs-only C lands during the poll, B's relay then SKIPS on the moved tip
    # and C's relay skips on its own marker, so nothing converges and all
    # three runs are green. A supply set proves B COULD deploy, never that its
    # relay WILL. The supply-set match additionally rejects an EXPIRED
    # artifact, which is a name the superseder's manifest step can no longer
    # download. Reviewed for dispatch capability: still exactly ONE
    # `gh workflow run` with the same four inputs and still exactly one secret
    # reference. The one added call is a fourth read-only GET of this repo's
    # own branch tip on `GH_TOKEN="$HOME_TOKEN"`, identical in form and token
    # to the three already present. No new endpoint, repo or credential.
    # Hash updated again 2026-08-07 (seventh edit): sol-critic round 4 on PR
    # #508. The supply-set expiry test tightened from `.expired != true` to
    # `.expired == false`, because jq reads a MISSING field as null and
    # `null != true` is true, so the loose form accepted a partial artifact
    # object that never proved the artifact is still downloadable. Comment and
    # filter change only; the dispatch is untouched, still ONE
    # `gh workflow run` with the same four inputs, one secret reference, and
    # the same four read-only GETs on `GH_TOKEN="$HOME_TOKEN"`.
    #
    # Hash updated 2026-08-07 (d): PR #508 merged with #506's need-first
    # ordering. Both changes rewrite this step and each moved this hash alone,
    # so this value covers the COMBINED script: #506's ordering read plus
    # #508's superseder classification, its two stale-tip guards and its
    # post-landing check. Reviewed for dispatch capability on the MERGED text:
    # still exactly one `gh workflow run` site with the same four inputs,
    # still exactly one `secrets.` reference, and every call either side adds
    # is a read-only GET on the workflow's own `GH_TOKEN="$HOME_TOKEN"`. No new
    # endpoint, repo, command or credential arises from combining them. The
    # `yes`-arm warning was also reworded here to drop the convergence promise
    # #506 was RED'd for three rounds running.
    #
    # Hash updated 2026-08-07 (e): THE CONVERGENCE RECEIPT, the behaviour change
    # PR #508 deliberately deferred and named as this residual's in-repo fix. A
    # relay that lands both services now writes staging-converged.json and sets
    # `converged=true`; the fourth step uploads it under
    # staging-converged-<sha>-attempt-<n>. A relay standing down on a `yes`
    # classification no longer exits green on the superseder's supply set -- it
    # WAITS for that superseder's receipt, bounded by the same 95-minute
    # deadline, and fails RED if none appears. The classifier additionally
    # persists the attempt that earned `yes` to a file, because it runs inside a
    # command substitution and that number would otherwise die with the subshell.
    #
    # REVIEWED FOR DISPATCH CAPABILITY. Unchanged. Still exactly ONE executable
    # `gh workflow run` with the same four inputs and still exactly one secret
    # reference, at the guarded step's GH_TOKEN. One call is added: a read-only
    # GET of THIS repo's own artifact list, filtered by exact name, on
    # `GH_TOKEN="$HOME_TOKEN"` -- the workflow's own token, which the permissions
    # block still pins to actions: read / contents: read. It is a new ENDPOINT
    # (repository artifacts rather than one run's artifacts) but the same repo,
    # token and read scope the relay already exercises; the repo-level form is
    # required because the receipt is published by the superseder's RELAY run,
    # whose id this relay cannot name. Two local file writes are added
    # (superseder-attempt, staging-converged.json) in the runner workspace, plus
    # one `jq -n` that makes no network call. The new fourth step holds NO env
    # and NO token and runs first-party actions/upload-artifact@v4, already used
    # across build-platform-images.yml; artifact upload uses the runner's own
    # results service, so it needs no addition to the permissions block and the
    # read-only capability wall stands. The infra-repo PAT gains no new command,
    # no new endpoint and no new repo.
    # Hash updated 2026-08-07 (f): THE DOCS-ONLY RECONCILE (PR #519). The
    # manifest step's docs-only arm no longer sets deploy=false and exits; it
    # resolves the last tag built from main (the newest successful main build
    # supply set whose commit is an ANCESTOR of the tip, scanned newest-first
    # so the first hit is never-backwards) and sets deploy=true onto it, so the
    # existing guarded dispatch step converges BOTH staging services. The
    # manifest was refactored onto a shared fetch_supply_set/supply_set_tag
    # helper pair used by the ordinary path and the reconcile, so both deploy a
    # verified supply-set build_tag and neither reads live state (the tag
    # NEVER comes from a run-name read -- sol-critic RED on PR #506 round 1).
    # REVIEWED FOR DISPATCH CAPABILITY: the manifest step still holds only the
    # read-only workflow token (its env is unchanged and asserted above),
    # carries NO dispatch path (the token tripwire below still passes), and the
    # ONE `gh workflow run` site stays in the guarded deploy step with the same
    # four inputs. The reconcile adds only read-only `gh api` GETs on the
    # workflow's own GH_TOKEN: a workflow-runs list filtered by build workflow
    # PATH, per-candidate run-artifact listings and zip downloads (already made
    # by the ordinary path via the same helper), and a compare of each
    # candidate against the tip. No new secret reference, endpoint class, or
    # credential; deploy=false is gone and deploy=true now appears twice, both
    # asserted above.
    assert frozen == (
        # Hash updated 2026-08-07 (g): sol-critic RED round 2 on PR #519.
        # The reconcile scan now (1) fails CLOSED when a successful build run
        # has neither a supply set nor its docs-noop marker (an expired,
        # deleted, or partial-upload artifact on a DEPLOYABLE run), instead of
        # skipping past it to an older ancestor and risking a backwards
        # deploy, and (2) requires each candidate supply set's source_revision
        # to equal the candidate run's head sha, so an artifact naming an older
        # revision cannot smuggle an older tag past the ancestor check.
        # Reviewed for dispatch capability: UNCHANGED -- one added local
        # jq read of the already-fetched artifacts.json (the marker check),
        # no new gh call, no new secret, one `gh workflow run` still in the
        # deploy step.
        # Hash updated 2026-08-07 (h): sol-critic RED round 2 on PR #519,
        # findings 1 and 2. (1) The reconcile compare now FAILS CLOSED on an
        # unreadable ancestry compare instead of `|| true`-skipping to an
        # older tag. (2) The deploy step no longer finishes its own remaining
        # service on a moved tip: making docs-only commits reconcile made
        # them convergers too, so a docs-only superseder (`no`) is now handled
        # like a deployable one (`yes`) -- both wait for the superseder's
        # convergence receipt -- and only `unknown` stands down. The dead
        # NOOP_FINISH_FROM finish-on-older path and its landing check are
        # gone. #508's fail-red-on-split principle is unchanged (no receipt
        # within budget is still RED). Reviewed for dispatch capability: one
        # `gh workflow run` still in the deploy step, no new secret, and the
        # only added reads are the reconcile's existing gh api GETs.
        # Hash updated 2026-08-07 (i): sol-critic RED round 3 on PR #519. The
        # reconcile compare's `behind|identical|diverged` skip branch is split:
        # a NON-ancestor candidate carrying a valid, provenance-matched supply
        # set ('behind' or 'diverged') now FAILS CLOSED, because scanned
        # newest-first it is NEWER than any ancestor the reconcile could fall
        # back to and may be live, so skipping it to deploy an older ancestor
        # was a backwards deploy. Only 'identical' (the docs-only tip commit
        # itself, which carries no images) still continues. Control-flow + text
        # change inside the manifest script; no dispatch change: still exactly
        # ONE `gh workflow run` in the deploy step with the same four inputs, no
        # new secret reference, and no new gh call -- the compare read is
        # unchanged, only its non-ancestor outcome moved from skip to exit 1.
        # Hash updated 2026-08-07 (j): sol-critic RED round 4 on PR #519. The
        # 'identical' compare arm no longer `continue`s -- it is merged into the
        # 'ahead' arm and USED. Docs-only-ness is decided per-PUSH (the build
        # gate diffs github.event.before against the head), not per-commit, so a
        # same-head SIBLING build reached from a different base can publish the
        # tip's OWN live images; skipping it to an older ancestor was a backwards
        # deploy. Now only 'behind'/'diverged' and unreadable/unexpected compares
        # fail closed. Control-flow + text change inside the manifest script; no
        # dispatch change: still exactly ONE `gh workflow run` in the deploy step
        # with the same four inputs, no new secret reference, and no new gh call.
        # Hash updated for the dormant digest-aware v3 producer/consumer
        # handshake. The guarded step still has one workflow dispatch site;
        # v1/v2 retain their exact legacy inputs, while v3 can run only when
        # the hardcoded source flag is deliberately enabled with the
        # reviewed Terraform consumer marker present.
        # Hash updated for the closed producer-owned v2/v3 supply envelope.
        # The manifest step adds only read-only provider association checks,
        # archive hashing, and local canonical JSON construction. The guarded
        # deploy step retains one `gh workflow run` site and adds one protected
        # input to that existing call for v2/v3. V1 dispatch inputs, credentials,
        # service order, polling, rollback, selectors, and receipts are unchanged.
        # Hash updated after the R3 producer made the manifest lookup-tag
        # relation authoritative and the relay replaced duplicate v3 fields
        # with one closed supply envelope.
        # Hash updated for the provider consumer contract handshake. The relay
        # adds only read-only Actions artifact discovery and one closed,
        # unpadded base64url input on the existing strict-v3 dispatch. It adds
        # no dispatch site, credential, selector, or v1/v2 behavior.
        # Hash updated after two real relays completed both child receipts but
        # lost the receipt stage to disjoint Terraform pushes. A newer contract
        # is accepted only when it is terminal green, strictly descends from
        # the bound head, and changes none of the three consumer semantics
        # files. The guarded step gains one read-only compare and no new write.
        # Hash updated 2026-08-17 for the docs-only convergence-identity
        # deadlock. REVIEWED FOR DISPATCH CAPABILITY: no new dispatch site,
        # secret reference, endpoint class, credential, or gh call of any kind.
        # The manifest step writes two additional OUTPUTS (release_source,
        # release_attempt) from values fetch_supply_set had already fetched and
        # verified against the producer run record. The guarded deploy step
        # reads them from step env, validates them against the supply artifact
        # name it already carried, and substitutes them into the convergence id
        # and the surface-result release check that previously used the build
        # head. The single `gh workflow run` site and its input set are
        # unchanged.
        # Hash updated 2026-08-24 for ATOMIC TWO-LEG DISPATCH. The serial
        # dispatch/watch loop became: plan every service, take ONE tip read,
        # dispatch every leg, resolve every run, then watch every leg. The
        # bodies moved into plan_service / require_tip_current /
        # dispatch_service / resolve_service_run / watch_service and per-service
        # state moved into associative arrays; the statements inside them are
        # otherwise carried over unchanged. The run-name reader was widened from
        # `^prod-<sha>` to also read the v3 `<sha>-<attempt>-<service>` and the
        # sha-/src-<40> deploy-only forms, because v3 naming had silently
        # disabled it in production. The clean stand-down was TIGHTENED from
        # DEPLOYED_ANY to DISPATCHED_ANY.
        # REVIEWED FOR DISPATCH CAPABILITY: no new dispatch site (still exactly
        # one `gh workflow run`, asserted above), no new secret reference,
        # endpoint class, credential, or gh call of any kind. The dispatch input
        # set is byte-identical. The added reads are the same `gh run list` and
        # `gh api compare` the ordering read already made. The never-backwards
        # tip gate is unchanged in substance and still ungated.
        "d15214ad2adb4ef73ffe13943bb9ff11a06b0907a938ef317cfc56e8070b75dd"
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
    # DOCS-ONLY RECONCILE (PR #519): the docs-only arm no longer skips. It
    # resolves the last tag built from main and sets deploy=true onto it, so a
    # docs-only merge converges both staging services instead of stranding a
    # split until the next deployable merge. deploy=false is therefore GONE.
    # Both the ordinary same-build path and the reconcile path call the same
    # closed output writer.
    assert manifest_code.count('echo "deploy=false"') == 0, (
        "the docs-only arm must reconcile, not skip: a deploy=false skip is "
        "exactly how a staging split outlived a docs-only merge")
    assert manifest_code.count('echo "deploy=true"') == 1, (
        "deploy=true must be emitted only by the shared output writer")
    assert manifest_code.count("write_manifest_outputs") == 3, (
        "the shared writer must have one definition and exactly two callers")
    # Still ONE run-artifact listing endpoint, inside the shared
    # fetch_supply_set helper that both paths call; the reconcile's run scan
    # lists workflow RUNS, a different endpoint.
    assert manifest_code.count("artifacts?per_page=100") == 1
    # The reconcile is real, not merely named: it scans successful main build
    # runs, keeps only a supply set whose commit is an ANCESTOR of the tip
    # (never-backwards), and fails RED when none is reachable rather than
    # skipping. test_staging_relay_reconciles_a_docs_only_build EXECUTES it.
    assert (
        "reconciling both staging services onto the last tag built from main"
        in manifest_code), "the docs-only arm must announce the reconcile"
    assert 'case "$RELATION" in' in manifest_code, (
        "the reconcile must switch on the EXPLICIT ancestry-compare status: "
        "the last built tag it takes must have a commit that is an ancestor "
        "of the tip, so it can never deploy a non-ancestor image")
    assert "the ancestry compare of candidate run" in manifest_code, (
        "an unreadable ancestry compare must FAIL CLOSED, never skip to an "
        "older tag -- that fallback is the backwards deploy sol-critic caught")
    assert "has nothing to reconcile onto" in manifest_code, (
        "a docs-only build with no reachable supply set must fail loudly, "
        "not skip")
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
        '"image_tag=$SERVICE_TAG"',
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
    end = text.index('spec_run_id="$candidate_run"', start)
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


def _unquoted_shell_refs(script: str) -> set:
    """Every $NAME / ${NAME} the SHELL would expand.

    Both quote states are tracked, because they mask each other and getting
    that wrong is not a small error: a single apostrophe inside a double-quoted
    string ("this relay\'s budget") would otherwise flip the single-quote state
    and desynchronise the rest of the file, surfacing every embedded jq
    program\'s --arg variables as if they were shell references.

      * inside single quotes bash expands NOTHING and offers no escape, which
        is exactly where the embedded jq programs live, so their $name tokens
        are jq variables bound by --arg and must not be collected;
      * inside double quotes $NAME DOES expand, and a \' there is literal;
      * a backslash escape suppresses expansion outside single quotes.
    """
    names = set()
    in_single = False
    in_double = False
    index = 0
    length = len(script)
    while index < length:
        char = script[index]
        if char == "'" and not in_double:
            in_single = not in_single
            index += 1
            continue
        if char == '"' and not in_single:
            in_double = not in_double
            index += 1
            continue
        if char == chr(92) and not in_single:
            index += 2
            continue
        if char == "$" and not in_single:
            match = re.match(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)", script[index:])
            if match:
                names.add(match.group(1))
                index += match.end()
                continue
        index += 1
    return names


def _shell_assigned_names(script: str) -> set:
    """Every name the script could bind: assignment, local, or loop variable.

    Deliberately permissive -- the pin hunts for names bound NOWHERE, so a
    false ASSIGNMENT only weakens the check, while a false REFERENCE would
    make it fire on correct code.
    """
    assigned = set(re.findall(r"\b([A-Za-z_][A-Za-z0-9_]*)(?:\[[^]]*\])?=", script))
    for decl in re.findall(r"^\s*(?:local|declare|export)\s+(.+)$", script, re.M):
        for token in decl.split():
            assigned.add(token.split("=", 1)[0].lstrip("$-"))
    assigned |= set(
        re.findall(r"^\s*for\s+([A-Za-z_][A-Za-z0-9_]*)\s+in\b", script, re.M)
    )
    return assigned


def _strip_heredocs(script: str) -> str:
    """Remove quoted-heredoc bodies so quote tracking cannot desync on them.

    `python3 - <<'PY' ... PY` bodies are full of apostrophes that belong to
    another language entirely; counting them as shell quotes would scramble
    every span boundary after the first one.
    """
    out = []
    lines = script.splitlines(keepends=True)
    index = 0
    while index < len(lines):
        line = lines[index]
        match = re.search(r"<<-?'([A-Za-z_][A-Za-z0-9_]*)'", line)
        out.append(line)
        index += 1
        if match:
            delim = match.group(1)
            while index < len(lines) and lines[index].strip() != delim:
                index += 1
            if index < len(lines):
                out.append(lines[index])
                index += 1
    return "".join(out)


def _jq_program_vars(script: str) -> set:
    """Every $name appearing inside a single-quoted span, i.e. a jq program.

    The mirror image of _unquoted_shell_refs: what the SHELL will not expand is
    exactly what jq sees, and jq resolves those names from its own --arg
    bindings. A shell-side rename that reaches inside one of these spans does
    not fail loudly -- jq exits non-zero with "is not defined", which the
    surrounding `|| { echo ...; exit 1; }` reports as a validation failure of
    the ARTIFACT rather than of the filter.
    """
    body = _strip_heredocs(script)
    names = set()
    in_single = False
    in_double = False
    index = 0
    while index < len(body):
        char = body[index]
        if char == "'" and not in_double:
            in_single = not in_single
            index += 1
            continue
        if char == '"' and not in_single:
            in_double = not in_double
            index += 1
            continue
        if char == chr(92) and not in_single:
            index += 2
            continue
        if char == "$" and in_single:
            match = re.match(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)", body[index:])
            if match:
                names.add(match.group(1))
                index += match.end()
                continue
        index += 1
    return names


def _jq_bound_names(script: str) -> set:
    """Names bound into jq programs by --arg / --argjson / --slurpfile / --rawfile."""
    return set(
        re.findall(
            r"--(?:arg|argjson|slurpfile|rawfile)\s+([A-Za-z_][A-Za-z0-9_]*)", script
        )
    )


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
    # Selected by ID, not by position. The relay grew a fourth step that
    # carries `uses:` and no `run:`, so `steps[-1]` both crashes and, worse,
    # would silently start checking the wrong script if another step were ever
    # appended. `deploy` is the guarded step's identity.
    code = _executable_bash(_relay_deploy_step(job)["run"])

    # Both services stay covered, and from a single dispatch site: a second
    # one is a deploy that the watch below would not be looking at.
    #
    # NEED-FIRST ORDERING (2026-08-07) replaced the literal `web app` loop.
    # The order was hardcoded, so web won every race and app lost every race:
    # a relay superseded mid-release always dies before its SECOND service, so
    # under any merge rate faster than one full release app never advanced
    # while web advanced every time. Three consecutive relays (d9306c3,
    # 7056e2e, 48204c4) produced web deploys with no app deploy between them
    # and staging ran a whole release split for over two hours.
    #
    # SERVICES may take exactly the two full-set orderings and nothing else,
    # so the ordering read cannot drop a service from the list.
    #
    # BE PRECISE ABOUT WHAT THAT BUYS. It does NOT make a wrong read free.
    # Only the FIRST service is dispatched unconditionally; the second sits
    # behind another tip re-check, so ORDER DECIDES WHO GETS SKIPPED when main
    # moves. sol-critic RED round 2 on PR #506 against a comment claiming
    # otherwise: put app (~14 min) first on bad evidence, let main move at
    # t=10, and web is skipped where web-first would have landed web at t=8
    # and dispatched app before the move. The trade is deliberate — the fixed
    # order charged that cost to app every single time, forever — but it is a
    # trade, not an absence of cost. test_staging_relay_orders_the_starved_
    # service_first pins the skip path so it stays a known, announced outcome.
    assert set(re.findall(r'SERVICES="([^"]*)"', code)) == {"web app", "app web"}, (
        "SERVICES may only ever hold BOTH services; a single-service value "
        "would let the ordering read silently skip a deploy")
    assert "for SERVICE in $SERVICES; do" in code, (
        "the relay must still deploy both staging services in one pass")
    assert code.count("gh workflow run") == 1, (
        "one dispatch site only: another would fire a deploy nobody watches")
    dispatch_at = code.index("gh workflow run")

    # THE OTHER HALF OF THAT SAFETY: the ordering read may never choose a TAG.
    # sol-critic returned RED on PR #506 round 1 against a version that read
    # each service's live tag from infra run names and DEPLOYED to it. That is
    # unsound two ways: a deploy that flips traffic and then fails or is
    # cancelled leaves the new colour live while its run reads as
    # unsuccessful, so the read can name an older tag as live; and the
    # read/dispatch window is not atomic, so a sibling deploy can move a
    # service forward while main has not moved, defeating the tip check. Both
    # turn a tag decision into a silent backwards deploy. Reordering cannot.
    # Staging app/web are also blue/green over digest-pinned task definitions,
    # so "live" is a function of ALB rule weights and no run-name read is
    # authoritative about it.
    assert not re.search(r'^\s*IMAGE_TAG=', code, re.M), (
        "the dispatch script must never assign IMAGE_TAG: the tag comes from "
        "the verified supply-set manifest, and letting a live-state read pick "
        "one is the backwards-deploy vector sol-critic caught")
    assert re.search(r'^\s*behind\)?\s*$|"\$RELATION" = "behind"', code, re.M), (
        "ordering must key off the ancestry compare, not recency")
    assert re.search(
        r'\[ "\$RELATION" = "behind" \]; then\n\s*SERVICES="app web"', code), (
        "compare/<web>...<app> reporting 'behind' means APP trails, so app "
        "takes the first slot; inverting this restores the starvation")

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

    # ATOMIC DISPATCH: ONE tip read covers BOTH legs, and no leg is watched
    # until every leg is dispatched.
    #
    # This assertion used to require the tip read INSIDE the per-service loop,
    # which was correct for the serial design and is what the design was
    # changed away from on 2026-08-24. Serially, the second service sat behind
    # the first service's ENTIRE deploy (~8 min web, ~14 min app, plus lock
    # queue), and any merge inside that window cost it its dispatch: five
    # consecutive relays between 16:05Z and 16:38Z landed a web deploy with no
    # app deploy between any of them, and staging ran split across two source
    # commits for hours. The tip read is now taken ONCE, immediately before the
    # dispatch phase, so both legs provably carry the same tag from the same
    # observation and the exposure window is the seconds between two
    # `gh workflow run` calls rather than a whole deploy.
    #
    # The safety property is unchanged and still asserted: nothing is
    # dispatched without a fresh, ungated tip read preceding it.
    assert "branches/main" in code, (
        "each dispatch must be preceded by a fresh tip-of-main read")
    assert code.count("require_tip_current") == 3, (
        "exactly one definition and two call sites: once before the dispatch "
        "phase, and once again before an eviction re-dispatch, which happens "
        "minutes later and so needs its own read")
    # Positions are compared between CALL SITES. dispatch_at above is the
    # `gh workflow run` inside dispatch_service's definition, and a definition
    # says nothing about execution order.
    # rindex, not index: the eviction-retry path inside watch_service also
    # calls dispatch_service, and it is defined earlier in the file than the
    # phase driver at the bottom. The LAST occurrence of each is the phase loop.
    gate_call_at = code.index('require_tip_current "$SERVICES"')
    dispatch_loop_at = code.rindex('dispatch_service "$SERVICE"')
    watch_loop_at = code.rindex('watch_service "$SERVICE"')
    assert gate_call_at < dispatch_loop_at < watch_loop_at, (
        "every leg must be dispatched before any leg is watched; interleaving "
        "them is the abandoned-leg defect")
    assert code.count("branches/main") == 3, (
        "exactly three tip reads: require_tip_current's own read, its re-read "
        "guarding a stale converger classification, and the freeze check "
        "before the v3 convergence receipt. A FOURTH, per-leg read would mean "
        "the two legs no longer share one observation")

    # The tip read is the FIRST statement of the gate and is NEVER gated.
    # sol-critic RED round 2 on PR #497 broke the tempting alternative: gating
    # it once a service had landed (to avoid leaving a split) lets relay A hold
    # a queued app deploy across newer relay B's whole sequence, each eviction
    # retried, so A's OLDER app lands on top of B's and both relays finish
    # green. Ordering legs within one relay never orders two relays against
    # each other; the infra workflow's in-lock monotonic-forward guard does.
    # A visible split is survivable; a silent backwards deploy is not.
    gate_body = code[code.index("require_tip_current() {"):].splitlines()[1:]
    first_stmt = next(ln.strip() for ln in gate_body if ln.strip())
    assert first_stmt.startswith("local "), first_stmt
    first_action = next(
        ln.strip() for ln in gate_body if ln.strip() and not ln.strip().startswith("local ")
    )
    assert first_action.startswith("MAIN_SHA="), (
        "the tip gate must re-read the tip FIRST and ungated; "
        f"found {first_action!r}")

    # And standing down after something went out must NAME the split rather
    # than report a plain success.
    assert "STAGING IS SPLIT" in code, (
        "standing down mid-release must announce the split it leaves behind")
    assert re.search(r'if \[ "\$DISPATCHED_ANY" != "true" \]', code), (
        "the clean stand-down must be conditioned on nothing having been "
        "DISPATCHED, not merely nothing having been deployed: a dispatched "
        "deploy can still land after this relay exits, so a relay that has "
        "dispatched can no longer prove nothing of its release went out")

    # A KNOWINGLY UNCONVERGED SPLIT MUST NOT CONCLUDE SUCCESS.
    #
    # Observed 2026-08-07: relay run 31144164225 deployed web, was superseded
    # mid-release by docs-only 7056e2e8, stood down with an accurate
    # ::warning:: naming the split, and concluded SUCCESS. A warning on a
    # green run pages nobody and gates nothing, so staging served
    # prod-d9306c3 on leaf-platform-web-alt and prod-87294a9 on
    # leaf-platform-app-alt with no alarm and a clean relay history.
    #
    # Red is conditioned on the split being LEFT UNCONVERGED, not on standing
    # down: being superseded by a commit that does build images is normal and
    # its own relay converges staging, so that path stays green on purpose.
    assert code.count("CONVERGER=$(superseder_deploys") == 1, (
        "a mid-release stand-down must classify whether the superseding "
        "commit converges staging, from exactly one place")
    # ...and that call must be the ONLY thing that ever sets it. A second
    # assignment placed after it (`CONVERGER=yes`) leaves the call in place,
    # satisfies the count above, and silently overrides the classification.
    converger_writes = re.findall(r"^\s*CONVERGER=", code, re.M)
    assert len(converger_writes) == 1, (
        "CONVERGER may be assigned exactly once, by the classifier call; "
        f"found {len(converger_writes)} assignments, so one can override it")
    # THE STAND-DOWN MUST BE EARNED BY THE SUPERSEDER'S RECEIPT.
    #
    # Until PR #519 there were two mid-release arms: a docs-only superseder
    # (`no`) FINISHED this relay's own remaining service on its older tag,
    # because docs-only "dispatched nothing"; a deployable superseder (`yes`)
    # waited for that superseder's convergence receipt. PR #519 makes docs-only
    # commits RECONCILE (dispatch both services onto the last built tag), so a
    # docs-only superseder now dispatches too, and finishing here would race it
    # and could land this older tag last -- the #497/#508 backwards-deploy race,
    # reopened (sol-critic RED round 2 on PR #519). So `no` and `yes` collapse
    # into ONE converge-and-wait arm and only `unknown` is a bare stand-down.
    #
    # `unknown` (build failed / never appeared / outlived the watch) fails RED.
    unconverged = re.search(
        r'if \[ "\$CONVERGER" != "no" \] && \[ "\$CONVERGER" != "yes" \]; then\n'
        r'.*?echo "::error::STAGING IS SPLIT AND UNCONVERGED:.*?\n\s*exit 1\n',
        code, re.S)
    assert unconverged, (
        "an `unknown` superseder (its build failed, never appeared, or outlived "
        "the watch) must fail the relay RED; a ::warning:: on a green run is the "
        "reporting hole itself")

    # THE RELAY MUST NOT FINISH ITS OWN SERVICE ON A MOVED TIP. A docs-only
    # superseder now reconciles, so there is a competing deploy; the only safe
    # move is to wait for the superseder's receipt. So the whole moved-tip
    # handling carries NO dispatch -- the single `gh workflow run` sits after it.
    # The block ends where the tip gate does. Under the serial design this was
    # delimited by the loop's `BEFORE=$(latest_any_run_id)` watermark; the
    # moved-tip handling now lives in require_tip_current, so the boundary is
    # the start of the next function.
    classify_at = code.index('CONVERGER=$(superseder_deploys')
    after_classify = code[classify_at:]
    moved_tip_block = after_classify[:after_classify.index("dispatch_service() {")]
    assert "gh workflow run" not in moved_tip_block, (
        "a relay superseded mid-release must NOT dispatch its own remaining "
        "service on a moved tip: a docs-only superseder now reconciles, so "
        "finishing here races it and can land an older tag last")

    # THE CLASSIFICATION IS A POLL, so its input tip is a snapshot. The arm
    # re-reads the tip AFTER the poll and fails CLOSED if it moved: the
    # superseder we are about to trust may no longer be the tip, and its own
    # relay may then stand down and converge nothing.
    assert re.search(
        r'TIP_NOW=\$\(GH_TOKEN="\$HOME_TOKEN" gh api \\\n\s*'
        r'"repos/\$GITHUB_REPOSITORY/branches/main"', moved_tip_block), (
        "the converge-and-wait arm must re-read the tip AFTER the classification "
        "poll; acting on the pre-poll snapshot can trust a superseder that is no "
        "longer the tip")
    stale_guard = re.search(
        r'if \[ "\$TIP_NOW" != "\$MAIN_SHA" \]; then\n(.*?)\n\s*fi',
        moved_tip_block, re.S)
    assert stale_guard, "the re-read needs a guard that fails on a moved tip"
    assert ("::error::" in stale_guard.group(1)
            and "exit 1" in stale_guard.group(1)), (
        "a stale convergence finding must fail RED, not proceed and not exit 0")

    # THE STAND-DOWN EXITS 0 ONLY INSIDE THE RECEIPT WAIT. A supply set (or a
    # docs-noop marker) proves the superseder COULD converge, never that its
    # relay DID; only the receipt proves it. The clean exit must sit inside the
    # wait, and be the ONLY exit 0 in the moved-tip handling.
    receipt_gate = re.search(
        r'^([ ]*)if wait_for_receipt "\$MAIN_SHA" "\$SUP_ATTEMPT_SEEN"; then\n'
        r'(.*?)\n\1fi\b', code, re.S | re.M)
    assert receipt_gate, (
        "the superseded stand-down must be gated on waiting for that "
        "superseder's convergence receipt; exiting 0 on its supply set or "
        "marker alone is the one-hop displacement of the original incident")
    assert receipt_gate.start() > classify_at, (
        "the receipt wait must live in the mid-release stand-down path")
    assert re.search(r"^\s*exit 0\b", receipt_gate.group(2), re.M), (
        "the clean exit must sit INSIDE the receipt wait, so a relay that never "
        "saw the receipt cannot reach it")
    assert len(re.findall(r"^\s*exit 0\b", moved_tip_block, re.M)) == 1, (
        "the superseded arm may exit 0 exactly once, inside its receipt wait; a "
        "second one stands down without the proof the wait exists to get")
    # The announcement must say the split CLOSED, on proof.
    converged_notice = re.search(
        r'echo "::notice::([^"]*)"', receipt_gate.group(2))
    assert converged_notice and "CONVERGED" in converged_notice.group(1), (
        "the receipt-backed stand-down must announce that staging converged, "
        "naming the proof it stood down on")
    # A MISSING RECEIPT IS RED (the wait is bounded by the step's own deadline).
    no_receipt = re.search(
        r'echo "::error::STAGING IS SPLIT AND UNCONVERGED:[^"]*never published '
        r'a convergence receipt[^"]*"\n[ ]*exit 1\b', moved_tip_block)
    assert no_receipt, (
        "a superseded stand-down whose receipt never arrives must fail RED and "
        "say the receipt is what was missing")
    # The attempt the receipt is named for comes from the classifier's own read.
    assert 'SUP_ATTEMPT_SEEN=$(cat superseder-attempt' in code, (
        "the receipt must be named from the attempt the classifier actually "
        "read, not from a re-read a rerun could move")
    assert re.search(
        r'if \[ -z "\$SUP_ATTEMPT_SEEN" \]; then\n[ ]*echo "::error::[^"]*"\n'
        r'[ ]*exit 1\b', code), (
        "an unrecorded superseder attempt must fail RED explicitly, not fall "
        "through to a wait for an artifact that can never appear")
    # The WRITER of that attempt must exist too.
    assert re.search(
        r"printf '%s' \"\$SUP_ATTEMPT\" > superseder-attempt", code), (
        "the classifier must persist the attempt that earned its verdict; it "
        "runs in a command substitution, so the value dies with that subshell")
    # `wait_for_receipt` single-definition rule: bash resolves the LAST
    # definition at call time, so a second one silently wins.
    for fn in ("wait_for_receipt",):
        defs = (code.count(f"{fn}() {{")
                + len(re.findall(rf"^\s*function\s+{fn}\b", code, re.M)))
        assert defs == 1, (
            f"{fn} must be defined exactly once; bash resolves the LAST "
            f"definition at call time, so a second one silently wins "
            f"(found {defs})")

    # THE RECEIPT IS WRITTEN ONLY BY FALLING OUT OF THE SERVICE LOOP.
    #
    # Everything that stands down or fails exits before this point, so the
    # single `converged=true` write is what makes the receipt mean "both
    # services provably landed". A second write anywhere inside the loop would
    # publish that claim for a release that deployed one service.
    converged_writes = re.findall(r'^[ ]*echo "converged=true"', code, re.M)
    assert len(converged_writes) == 1, (
        "`converged=true` may be written exactly once; a second write inside "
        "the service loop publishes a receipt for a partial release "
        f"(found {len(converged_writes)})")
    loop_ends = [m.end() for m in re.finditer(r"^[ ]*done\b", code, re.M)]
    assert loop_ends, "the service loop must close"
    assert code.index('echo "converged=true"') > loop_ends[-1], (
        "the convergence receipt may only be recorded AFTER the service loop "
        "completes; recorded inside it, a relay that later fails or stands "
        "down has already claimed both services landed")
    assert re.search(r'staging-converged\.json', code), (
        "the guarded step must write the receipt payload the publish step "
        "uploads")

    # THE PUBLISH STEP ITSELF. Pinned here, inside the convergence contract, so
    # the decoy battery exercises it: a receipt published by a relay that stood
    # down is a lie every future waiter believes.
    receipt_steps = [s for s in job["steps"] if s.get("id") is None]
    assert len(receipt_steps) == 1, (
        f"exactly one relay step publishes the receipt; found "
        f"{len(receipt_steps)}")
    receipt_step = receipt_steps[0]
    assert receipt_step["uses"] == "actions/upload-artifact@v4", (
        "the receipt publisher is pinned to the first-party action version the "
        "build workflow already uses")
    assert receipt_step["if"] == "steps.deploy.outputs.converged == 'true'", (
        "the receipt may be published ONLY when the guarded step recorded "
        "convergence; any weaker condition lets a relay that stood down or "
        "skipped assert a convergence it never performed")
    assert receipt_step["with"] == {
        "name": (
            "staging-converged-"
            "${{ github.event.workflow_run.head_sha }}"
            "-attempt-${{ github.event.workflow_run.run_attempt }}"
        ),
        "path": "staging-converged.json",
        "if-no-files-found": "error",
    }, (
        "the receipt NAME is the protocol. It is keyed by the BUILD's sha and "
        "attempt, exactly like the supply set and the docs-noop marker, "
        "because that is the only key a superseded relay can compute from the "
        "build record its classifier already reads. Drifting it publishes a "
        "receipt nobody is waiting for and strands every waiter")

    # (The former docs-only `no` arm and deployable `yes` arm collapsed into
    # the single converge-and-wait stand-down pinned above: PR #519 made
    # docs-only commits reconcile, so both superseders converge and are
    # proven by the same receipt, and the tip re-read / stale-guard now lives
    # in that one arm. The classifier verdicts themselves are unchanged and
    # are pinned next.)

    # The classifier reads the superseding build's OWN receipt. Re-deriving
    # the docs-only verdict from a compare API would be a second copy of a
    # rule the build computes from the real push diff with rename detection
    # disabled, and the two would drift.
    assert "docs-noop-$SUP_SHA-attempt-" in code, (
        "the superseder must be classified by its docs-noop marker artifact")
    assert '"completed success") echo yes' in code, (
        "only a SUCCEEDED superseding build proves a supply set exists to "
        "converge staging")
    assert re.search(r"^\s*echo unknown\n\s*\}", code, re.M), (
        "exhausting the classifier budget must fall to 'unknown', which is "
        "the RED path; defaulting to 'yes' would restore the silent split")

    # EVERY READ IN THE CLASSIFIER FAILS CLOSED.
    #
    # sol-critic RED, reproduced: `|| true` on the artifact listing emptied
    # the result on any transient API failure, the marker check was then
    # skipped, and a DOCS-ONLY superseder classified as `yes` on the strength
    # of its build concluding success. That is precisely the green-on-a-split
    # outcome this guard exists to stop, reachable through one dropped
    # request. An unproven read must retry, never decide.
    classifier = re.search(
        r"^classify_superseder_once\(\) \{\n(.*?)\n\}$", code, re.S | re.M)
    assert classifier, "the per-poll classification must be its own function"
    once = classifier.group(1)
    assert "|| true" not in once, (
        "no read in the classifier may be suppressed with `|| true`: an "
        "unproven read must retry, never fall through to a decision")
    assert once.count("|| return 1") >= 4, (
        "the run list, the run record, the artifact listing and the "
        "completeness check must each fail closed with `return 1`; found "
        f"{once.count('|| return 1')}")

    # A NAME IS NOT AN IDENTITY. Selecting the superseding build by display
    # name lets any other workflow also called "Build platform images" supply
    # a colliding marker or conclusion, and adding one would not touch this
    # file or its frozen hash.
    assert "select(.path == $path)" in once, (
        "the superseding build must be selected by workflow PATH, not by its "
        "display name")
    assert "BUILD_WORKFLOW_PATH=" in code, "the pinned build path must be declared"

    # ABSENCE ONLY COUNTS AGAINST A LISTING PROVEN WHOLE. A truncated page
    # says nothing about the entries it did not return, and marker absence is
    # exactly what promotes the answer to `yes`.
    assert "(.total_count <= (.artifacts | length))" in once, (
        "artifact absence may only be trusted when the listing is proven "
        "complete")
    assert '(.total_count | type == "number")' in once, (
        "a response missing `total_count` proves nothing about completeness "
        "and must not be treated as whole")
    assert '(.artifacts | type == "array")' in once, (
        "the artifact listing must be proven to BE a listing")

    # THE MARKER IS BOUND TO THE RUN'S CURRENT ATTEMPT. Artifacts are keyed by
    # run id and NOT by attempt, so an attempt-1 docs marker outlives a rerun
    # whose attempt 2 built real images. A prefix match would call that rerun
    # `no` and finish this release on the older tag, landing it on top of the
    # images the rerun is about to deploy.
    assert "any(.artifacts[]; .name == $n)" in once, (
        "the marker must match by EXACT name, not by prefix across attempts")
    assert "docs-noop-$SUP_SHA-attempt-$SUP_ATTEMPT" in once, (
        "the marker name must be bound to the run's CURRENT attempt")

    # THE FORWARDED VERDICT IS PART OF THE CLASSIFICATION, so it needs the
    # same single-assignment rule as CONVERGER. `VERDICT=yes` placed just
    # inside `if VERDICT=$(classify_superseder_once ...)` preserves the jq
    # filter, the single CONVERGER assignment and every other pin, passes
    # `bash -n`, and turns an UNDECIDED poll into a green stand-down.
    assert 'if VERDICT=$(classify_superseder_once "$1"); then' in code, (
        "the polling wrapper must take its verdict straight from the "
        "classifier call")
    # EXACTLY ONE DEFINITION EACH. Bash resolves a function at CALL time, so a
    # second definition placed anywhere later silently wins. The executable
    # rehearsal lifts only the slice from BUILD_WORKFLOW_PATH through the end
    # of superseder_deploys, so a redefinition after that slice would answer
    # `yes` at runtime while every rehearsal case still passed.
    for fn in ("classify_superseder_once", "superseder_deploys"):
        # Count BOTH spellings: bash also accepts `function name { ... }`, and
        # counting only `name() {` let the alternate form slip past.
        defs = (code.count(f"{fn}() {{")
                + len(re.findall(rf"^\s*function\s+{fn}\b", code, re.M)))
        assert defs == 1, (
            f"{fn} must be defined exactly once; bash resolves the LAST "
            f"definition at call time, so a second one silently wins "
            f"(found {defs})")
    assert code.count("VERDICT=") == 1, (
        "VERDICT may be assigned exactly once, by the classifier call; a "
        "second assignment overrides an undecided result while leaving every "
        f"other pin intact (found {code.count('VERDICT=')})")

    # `yes` MUST BE EARNED BY THE SUPPLY SET, NOT BY A GREEN CONCLUSION.
    #
    # sol-critic round 2, reproduced: a build can conclude success and publish
    # NO artifacts (a partial run, a failed upload). The completeness check
    # passes on {"total_count":0,"artifacts":[]}, no marker is found, and the
    # old code answered `yes`. But the manifest step of THIS very workflow
    # treats a missing supply set with no marker as a hard failure, so that
    # superseder's relay deploys nothing and the split never converges, while
    # this relay exited green on its success. `yes` claims the superseder
    # converges staging, so it has to require the artifact that makes that
    # true.
    assert "staging-supply-set-$SUP_SHA-attempt-$SUP_ATTEMPT" in once, (
        "`yes` must require the superseder's exact attempt-bound supply-set "
        "artifact, not merely a successful conclusion")
    yes_arm = re.search(
        r'staging-supply-set-\$SUP_SHA-attempt-\$SUP_ATTEMPT"(.*?)\n\s*fi\n',
        once, re.S)
    assert yes_arm and "echo yes" in yes_arm.group(1), (
        "the `yes` answer must sit INSIDE the supply-set check")
    # An expired artifact is a name with nothing behind it; the superseder's
    # manifest step still has to DOWNLOAD this supply set.
    # `.expired == false`, not `!= true`: jq reads a MISSING field as null and
    # `null != true` is true, so the loose form would accept a partial artifact
    # object that never proved the artifact is still downloadable. This
    # classifier fails closed on an unproven response, and that includes an
    # artifact whose expiry it cannot read.
    assert "(.expired == false)" in once, (
        "an expired -- or unproven -- supply-set artifact must not earn `yes`")
    assert ".expired != true" not in once, (
        "`.expired != true` accepts a missing field; require `== false`")
    # The CONDITION itself must stay falsifiable. sol-critic showed an
    # always-true guard (`... >/dev/null || :`) passed every other pin while
    # removing the requirement entirely, so pin the condition, not just the
    # presence of the text inside it.
    supply_condition = re.search(
        r'if printf[^\n]*\n(?:[^\n]*\n)*?[^\n]*staging-supply-set[^\n]*\n'
        r'(?:[^\n]*\n)*?[^\n]*; then\n', once)
    assert supply_condition, "the supply-set requirement must gate `yes`"
    assert "||" not in supply_condition.group(0), (
        "the supply-set check must carry no `||` fallback: that makes the "
        "condition unconditionally true and silently drops the requirement")
    # A shell-level `||` is not the only way to make the guard vacuous: the jq
    # FILTER can be made a tautology from the inside. Pin the filter exactly.
    assert (
        "'any(.artifacts[]; (.name == $n) and (.expired == false))'"
        in supply_condition.group(0)), (
        "the supply-set jq filter must be exactly the membership test; a "
        "disjunction such as `any(...) or true` accepts a missing supply set")

    # (The NOOP_FINISH_FROM landing check is gone with the finish-on-moved-tip
    # path it guarded: since PR #519 this relay never dispatches over a moved
    # tip -- it waits for the superseder's receipt instead -- so there is no
    # dispatched-over-a-cleared-tip deploy left to re-check after landing.)

    # The loop advances to the next service ONLY from inside the success arm.
    # Pinned as "a break lives in that arm" rather than "break is the next
    # line", so ordinary edits inside the arm stay legal.
    # The close is anchored to the OPENING indentation so a nested `if` inside
    # the arm (the post-landing tip check) cannot truncate the match and let
    # `break` slip out of view.
    success_arm = re.search(
        r'^(\s*)if \[ "\$CONCLUSION" = "success" \]; then\n(.*?)\n\1fi\b',
        code, re.S | re.M)
    assert success_arm, "the watch needs an explicit success arm"
    # `break` under the serial loop, `return 0` now that the watch is a
    # function called once per already-dispatched leg. Either way the ONLY way
    # to stop watching a service is that its deploy concluded success; every
    # other path below exits non-zero.
    assert re.search(r"^\s*(break|return 0)\b", success_arm.group(2), re.M), (
        "the watch may only stop on a service whose deploy concluded success")
    assert "landed (run $RUN_ID)" in success_arm.group(2), (
        "the success arm must say which run landed the deploy")
    assert re.search(r"^\s*DEPLOYED_ANY=true\b", success_arm.group(2), re.M), (
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

    # Every clean early exit is a stand-down for a newer commit, and there
    # are exactly two: nothing was live yet, or the superseder converges.
    # Counting them pins the shape; requiring each to be a stand-down is what
    # keeps a future `exit 0` from swallowing a failure.
    exit_zeros = [i for i, ln in enumerate(code.splitlines())
                  if ln.strip() == "exit 0"]
    assert len(exit_zeros) == 2, (
        "the relay may exit 0 early only for the two stand-downs (nothing "
        f"live yet, or a converging superseder); found {len(exit_zeros)}")
    for i in exit_zeros:
        preceding = "\n".join(code.splitlines()[:i])[-700:]
        assert "standing down" in preceding.lower(), (
            f"the exit 0 on line {i + 1} is not a stand-down; every clean "
            "early exit must say why it is leaving the deploy to someone else")

    # Retry covers QUEUE EVICTION ONLY. Zero started jobs proves the run
    # never reached AWS; re-dispatching a run that started and then failed
    # would blind-redeploy over a real failure.
    assert re.search(
        r'\[ "\$CONCLUSION" = "cancelled" \]\s*&&\s*\[ "\$JOBS" -eq 0 \]', code), (
        "the retry must require BOTH a cancelled conclusion and zero started jobs")
    assert code.count("continue") == 1, (
        "exactly one retry path may re-enter the dispatch loop")
    assert re.search(
        r'\[ "\$\{ATTEMPT_OF\[\$SERVICE\]\}" -lt "\$EVICTION_RETRIES" \]', code), (
        "eviction retries must be bounded, per service: the attempt counter is "
        "per-leg now that both legs are in flight at once")

    # The job must outlive the step's own deadline, so a stuck deploy is
    # reported as a NAMED half-deployed service instead of a bare job timeout.
    deadline = re.search(
        r"DEADLINE=\$\(\(\s*\$\(date \+%s\)\s*\+\s*(\d+)\s*\*\s*60", code)
    assert deadline, "the watch loop needs an explicit wall-clock deadline"
    assert job["timeout-minutes"] > int(deadline.group(1)), (
        f"timeout-minutes {job['timeout-minutes']} must exceed the step's own "
        f"{deadline.group(1)}-minute deadline")

    # NO REFERENCE TO A VARIABLE NOTHING ASSIGNS. The step runs under
    # `set -euo pipefail`, so a reference to an unset name does not print an
    # empty string -- it ABORTS the step. That turns a deliberate, informative
    # failure path into a bare "unbound variable" with no diagnosis, on the
    # exact path an operator most needs to read.
    #
    # This is not hypothetical. Moving the dispatch and watch bodies into
    # functions renamed their working variables, and two error-message
    # interpolations were missed: `$conclusion` on the "deploy concluded X"
    # path and `$want` on the "no run named X appeared" path. Both are inside
    # error strings, so no rehearsal that takes the success path can reach
    # them, and `bash -n` cannot see them either. Only this check does.
    #
    # Single-quoted spans are skipped, because jq programs are embedded as
    # single-quoted heredoc-free strings and their `$name` tokens are JQ
    # variables bound by `--arg`, not shell variables. Bash gives single quotes
    # no escape, so tracking the quote state across the whole script is exact.
    referenced = _unquoted_shell_refs(code)
    assigned = _shell_assigned_names(code)
    # Names the runner or the step env supplies rather than the script itself.
    supplied = set(_relay_deploy_step(job).get("env", {})) | set(job.get("env", {})) | {
        "GITHUB_REPOSITORY", "GITHUB_OUTPUT", "GITHUB_RUN_ID", "GITHUB_ENV",
        "RUNNER_TEMP", "HOME", "PATH", "BASH_REMATCH", "LC_ALL",
    }
    unbound = sorted(n for n in referenced if n not in assigned and n not in supplied)
    assert unbound == [], (
        "the deploy step references shell variables nothing assigns, which "
        f"aborts under `set -u` instead of reporting: {unbound}")

    # ...AND NO jq PROGRAM MAY REFERENCE A VARIABLE NOTHING BINDS. This is the
    # mirror of the pin above and it exists because that one cannot see here:
    # it deliberately skips single-quoted spans, which is exactly where the jq
    # filters live.
    #
    # The failure this catches is quieter than the shell one. A shell-side
    # rename that reaches into a jq program does not abort the step -- jq exits
    # non-zero with "$NAME is not defined", and the surrounding
    # `|| { echo "...invalid or mismatched surface result"; exit 1; }` reports
    # it as a bad ARTIFACT rather than a bad filter. Every successful v3 deploy
    # would fail its surface-result validation while the error blamed the
    # producer. That is precisely what a bulk rename did to
    # `.convergence_id == $convergence` on this branch, and sol-critic caught
    # it after the shell pin had passed clean.
    jq_vars = _jq_program_vars(code)
    jq_bound = _jq_bound_names(code) | {"__loc__", "ENV"}
    unbound_jq = sorted(jq_vars - jq_bound)
    assert unbound_jq == [], (
        "a jq program in the deploy step references variables no --arg binds, "
        f"so jq fails and the error blames the artifact: {unbound_jq}")

    print("staging relay convergence invariants: PASS")


def _relay_deploy_step(job: dict) -> dict:
    """The relay's ONE guarded step, resolved by id and proven unique.

    The receipt step added on 2026-08-07 carries `uses:` and no `run:`, so
    positional selection (`steps[-1]`) no longer names the guarded script.
    Resolving by id also means a future reordering cannot quietly point these
    checks at a different script while every assertion below still passes.
    """
    matches = [s for s in job["steps"] if s.get("id") == "deploy"]
    assert len(matches) == 1, (
        f"exactly one relay step may carry id `deploy`; found {len(matches)}")
    assert "run" in matches[0], "the guarded deploy step must carry a script"
    return matches[0]


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
                   '            echo "Dispatched $SERVICE reconciliation of $SERVICE_TAG',
                   '            gh workflow run "$DEPLOY_WORKFLOW" --repo "$INFRA_REPO"\n'
                   '            echo "Dispatched $SERVICE reconciliation of $SERVICE_TAG'),
        ),
        (
            "job timeout cut below the step's own watch deadline",
            mutate(original, "timeout-minutes: 105", "timeout-minutes: 5"),
        ),
        (
            "one service quietly dropped from the loop",
            mutate(original, 'SERVICES="web app"', 'SERVICES="web"'),
        ),
        (
            "the ordering read promoted to choosing the deploy tag",
            mutate(original,
                   '              SERVICES="app web"\n',
                   '              SERVICES="app web"\n'
                   '              IMAGE_TAG="$WEB_TAG"\n'),
        ),
        (
            "ordering inverted, so the starved service stays starved",
            mutate(original,
                   '[ "$RELATION" = "behind" ]; then\n              SERVICES="app web"',
                   '[ "$RELATION" = "behind" ]; then\n              SERVICES="web app"'),
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
                   '            MAIN_SHA=$(GH_TOKEN="$HOME_TOKEN" gh api \\',
                   '            if [ "$DISPATCHED_ANY" = "false" ]; then\n'
                   '            MAIN_SHA=$(GH_TOKEN="$HOME_TOKEN" gh api \\'),
        ),
        (
            "mid-release stand-down reported as an ordinary success",
            mutate(original, "STAGING IS SPLIT AND UNCONVERGED: main moved",
                   "main moved"),
        ),
        (
            "landed deploy no longer records that the release is partly live",
            mutate(original, "DEPLOYED_ANY=true", "DEPLOYED_ANY=false"),
        ),
        (
            "loop advances without the deploy having landed",
            mutate(original,
                   '                fi\n                return 0\n              fi\n',
                   '                fi\n              fi\n'),
        ),
        # --- the 2026-08-07 reporting hole and its escape hatch ---
        (
            "an `unknown` superseder no longer fails red, so a split is green",
            mutate(original,
                   '            if [ "$CONVERGER" != "no" ] && [ "$CONVERGER" != "yes" ]; then\n',
                   "            if false; then\n"),
        ),
        (
            "classifier budget exhaustion defaults to 'converges' instead of red",
            mutate(original, "            echo unknown\n          }",
                   "            echo yes\n          }"),
        ),
        (
            "any completed superseding build counted as a converger",
            mutate(original,
                   '                "completed success") echo yes; return 0 ;;\n',
                   "                completed*) echo yes; return 0 ;;\n"),
        ),
        # --- sol-critic round 2 on PR #508 ---
        (
            "the supply-set requirement neutralised by an always-true guard, "
            "so a build that published NOTHING counts as a converger",
            mutate(original,
                   "'any(.artifacts[]; (.name == $n) and (.expired == false))' \\\n"
                   "                  >/dev/null; then\n",
                   "'any(.artifacts[]; (.name == $n) and (.expired == false))' \\\n"
                   "                  >/dev/null || :; then\n"),
        ),
        (
            "an EXPIRED supply set, which the superseder's relay cannot "
            "download, counted as a converger",
            mutate(original, " and (.expired == false)", ""),
        ),
        (
            "expiry test loosened so a partial artifact object with no "
            "`expired` field still earns `yes`",
            mutate(original, "(.expired == false)", "(.expired != true)"),
        ),
        (
            "the jq membership test made a tautology from the inside",
            mutate(original,
                   "'any(.artifacts[]; (.name == $n) and (.expired == false))'",
                   "'any(.artifacts[]; (.name == $n) and (.expired == false)) or true'"),
        ),
        (
            "the classification silently overridden by a second assignment",
            mutate(original, 'CONVERGER=$(superseder_deploys "$MAIN_SHA")',
                   'CONVERGER=$(superseder_deploys "$MAIN_SHA")\n'
                   "                CONVERGER=yes"),
        ),
        (
            "the classifier REDEFINED after the rehearsal's extracted slice, "
            "so bash resolves the override at call time",
            mutate(original, "          DEPLOYED_ANY=false\n",
                   "          classify_superseder_once() { echo yes; }\n"
                   "          DEPLOYED_ANY=false\n"),
        ),
        (
            "the same late override written in bash's ALTERNATE function "
            "syntax, which an exact-spelling count misses",
            mutate(original, "          DEPLOYED_ANY=false\n",
                   "          function superseder_deploys { echo yes; }\n"
                   "          DEPLOYED_ANY=false\n"),
        ),
        (
            "the forwarded verdict overridden inside the polling wrapper, "
            "turning an UNDECIDED poll into a green stand-down",
            mutate(original,
                   'if VERDICT=$(classify_superseder_once "$1"); then\n',
                   'if VERDICT=$(classify_superseder_once "$1"); then\n'
                   "                VERDICT=yes\n"),
        ),
        (
            "artifact listing trusted without proving it carries a count",
            mutate(original, '(.total_count | type == "number")', "true"),
        ),
        # --- sol-critic RED on PR #508: the classifier's own read paths ---
        (
            "a failed artifact listing suppressed, so absence is assumed and "
            "a docs-only superseder classifies as `yes`",
            mutate(original, "|| return 1", "|| true"),
        ),
        (
            "the superseding build selected by display name, which any other "
            "workflow can also carry",
            mutate(original, "select(.path == $path)",
                   'select(.name == "Build platform images")'),
        ),
        (
            "marker absence trusted against a truncated artifact page",
            mutate(original, "(.total_count <= (.artifacts | length))",
                   "(.total_count >= 0)"),
        ),
        (
            "marker matched across rerun attempts, so an attempt-1 docs "
            "marker outlives a rerun whose attempt 2 built real images",
            mutate(
                mutate(original, "any(.artifacts[]; .name == $n)",
                       "any(.artifacts[]; .name | startswith($n))"),
                "docs-noop-$SUP_SHA-attempt-$SUP_ATTEMPT",
                "docs-noop-$SUP_SHA-attempt-"),
        ),
        (
            "superseder classification skipped, split assumed benign",
            mutate(original, 'CONVERGER=$(superseder_deploys "$MAIN_SHA")',
                   "CONVERGER=yes"),
        ),
        (
            "marker check replaced by a re-derived diff that can drift",
            mutate(original, 'docs-noop-$SUP_SHA-attempt-', "noop-guess-"),
        ),
        (
            "acting on the pre-poll tip snapshot (sol@medium's interleaving)",
            mutate(original,
                   '            TIP_NOW=$(GH_TOKEN="$HOME_TOKEN" gh api \
',
                   '            TIP_NOW=$MAIN_SHA # $(GH_TOKEN="$HOME_TOKEN" gh api \
'),
        ),
        (
            "stale docs-noop finding proceeds instead of failing red",
            mutate(original,
                   'if [ "$TIP_NOW" != "$MAIN_SHA" ]; then',
                   'if [ "$TIP_NOW" = "$TIP_NOW" ] && false; then'),
        ),
        # --- THE CONVERGENCE RECEIPT (2026-08-07). Each negative below is the
        # receipt protocol defeated in one specific way, and each one reopens
        # the exact race the receipt exists to close.
        (
            "THE RESIDUAL ITSELF: the `yes` arm stands down on the supply set "
            "again, without ever waiting for the superseder's receipt",
            mutate(original,
                   'if wait_for_receipt "$MAIN_SHA" "$SUP_ATTEMPT_SEEN"; then',
                   "if true; then"),
        ),
        (
            "a receipt that never arrives swallowed as success, so a "
            "superseder that was itself superseded still exits this relay green",
            mutate(original,
                   "it may itself have been superseded and stood down. "
                   "Staging is NOT converged on $BUILD_HEAD_SHA; converge it "
                   'with a successful main build."\n            exit 1\n',
                   "it may itself have been superseded and stood down. "
                   "Staging is NOT converged on $BUILD_HEAD_SHA; converge it "
                   'with a successful main build."\n            exit 0\n'),
        ),
        (
            "`wait_for_receipt` REDEFINED later to return 0, so the wait is "
            "satisfied by no receipt at all",
            mutate(original, "          DEPLOYED_ANY=false\n",
                   "          wait_for_receipt() { return 0; }\n"
                   "          DEPLOYED_ANY=false\n"),
        ),
        (
            "the same override in bash's ALTERNATE function syntax",
            mutate(original, "          DEPLOYED_ANY=false\n",
                   "          function wait_for_receipt { return 0; }\n"
                   "          DEPLOYED_ANY=false\n"),
        ),
        (
            "the classifier stops recording the attempt, so the receipt can "
            "never be named and the protocol is silently dead",
            mutate(original,
                   "            printf '%s' \"$SUP_ATTEMPT\" > superseder-attempt\n",
                   ""),
        ),
        (
            "an unrecorded attempt falls through to the wait instead of "
            "failing red, so the relay burns its deadline and blames the receipt",
            mutate(original, 'if [ -z "$SUP_ATTEMPT_SEEN" ]; then',
                   "if false; then"),
        ),
        (
            "the receipt published unconditionally, so a relay that STOOD "
            "DOWN certifies a convergence it never performed",
            mutate(original,
                   "        if: steps.deploy.outputs.converged == 'true'\n",
                   "        if: always()\n"),
        ),
        (
            "convergence recorded INSIDE the service loop, so a release that "
            "lands web and then fails on app still publishes a receipt",
            mutate(original,
                   '                  DEPLOYED_ANY=true\n',
                   '                  DEPLOYED_ANY=true\n'
                   '                echo "converged=true" >> "$GITHUB_OUTPUT"\n'),
        ),
        (
            "the receipt NAME keyed by the relay run instead of the build, "
            "which no superseded relay can compute",
            mutate(original,
                   "staging-converged-${{ github.event.workflow_run.head_sha }}"
                   "-attempt-${{ github.event.workflow_run.run_attempt }}",
                   "staging-converged-${{ github.sha }}"
                   "-attempt-${{ github.run_attempt }}"),
        ),
        (
            "the publisher swapped for a different action, new supply chain "
            "under a workflow that holds the infra PAT",
            mutate(original, "uses: actions/upload-artifact@v4",
                   "uses: some-org/upload@main"),
        ),
        (
            "an empty upload tolerated, so the receipt name exists with no "
            "payload behind it",
            mutate(original, "if-no-files-found: error",
                   "if-no-files-found: ignore"),
        ),
    ]
    for name, mutant in negatives:
        assert mutant != original
        try:
            check_staging_relay_convergence(mutant)
        except AssertionError:
            continue
        raise AssertionError(f"relay convergence battery: {name} was NOT caught")

    landed_echo = '                  echo "$SERVICE deploy of $IMAGE_TAG landed (run $RUN_ID)."\n'
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


_FAKE_GH = r'''#!/usr/bin/env bash
# Fake `gh` for the relay dispatch rehearsal. Answers from the scenario in the
# environment and records every dispatch to $DISPATCH_LOG, so the assertion is
# on what the relay ACTUALLY deploys, not on what its text looks like.
set -uo pipefail

args=("$@")
joined="$*"

case "${args[0]}" in
  api)
    case "${args[1]}" in
      *"/branches/main"*)
        # Serve the built sha for the first $FAKE_TIP_OK_READS tip checks,
        # then a newer one, so a scenario can move main BETWEEN services.
        n=$(cat "$FAKE_TIP_FILE")
        if [ "$n" -gt 0 ]; then
          printf '%s' "$((n - 1))" > "$FAKE_TIP_FILE"
          printf '%s\n' "$FAKE_MAIN_SHA"
        else
          printf '%s\n' "moved01"
        fi
        ;;
      *"/compare/"*)      printf '%s\n' "$FAKE_RELATION" ;;
      *"/jobs"*)          printf '%s\n' "1" ;;
      # PR #508 follow-up: the `yes` arm now waits for the superseder's
      # convergence receipt at the repository artifact endpoint, filtered by
      # exact name. Echo a matching receipt so a converging-superseder scenario
      # resolves at once instead of spinning to the deadline (sleep is a
      # no-op). The name is read back out of the query so the relay's own local
      # re-match on `.name == $n` succeeds.
      *"/actions/artifacts?per_page=100&name="*)
        want="${args[1]##*name=}"
        printf '%s\n' '{"total_count":1,"artifacts":[{"name":"'"$want"'","expired":false}]}' ;;
      # PR #508's superseder classifier runs inside THIS step whenever a
      # scenario moves main after a service is already live. It polls to a
      # 95-minute deadline, and this stub no-ops `sleep`, so an unanswered
      # endpoint spins the rehearsal for the full budget instead of failing
      # it. Answer all three reads so the classification resolves at once.
      *"/actions/runs?head_sha="*)
        printf '%s\n' '{"workflow_runs":[{"id":77,"path":".github/workflows/build-platform-images.yml","name":"Build platform images"}]}' ;;
      *"/artifacts?per_page=100")
        printf '%s\n' '{"total_count":1,"artifacts":[{"name":"staging-supply-set-moved01-attempt-1","expired":false}]}' ;;
      *"/actions/runs/"*)
        printf '%s\n' '{"status":"completed","conclusion":"success","run_attempt":1}' ;;
      *) echo "fake gh: unhandled api ${args[1]}" >&2; exit 9 ;;
    esac
    ;;
  run)
    case "${args[1]}" in
      list)
        # The live-tag read is the only one asking for `conclusion`.
        if [[ "$joined" == *"conclusion"* ]]; then
          if [ "${FAKE_HISTORY_FAILS:-0}" = "1" ]; then exit 4; fi
          printf '%s\n' "$FAKE_HISTORY"
        elif [[ "$joined" == *"displayTitle"* ]]; then
          printf '%s\n' "$FAKE_PENDING"
        else
          printf '%s\n' '[{"databaseId":1000}]'
        fi
        ;;
      view) printf '%s\n' "completed success" ;;
      *) echo "fake gh: unhandled run ${args[1]}" >&2; exit 9 ;;
    esac
    ;;
  workflow) printf '%s\n' "$joined" >> "$DISPATCH_LOG" ;;
  *) echo "fake gh: unhandled ${args[0]}" >&2; exit 9 ;;
esac
'''


def _rehearse_relay_dispatch(*, image_tag="prod-9999999", web_title, app_title,
                             relation="ahead", main_sha="deadbee",
                             history_fails=False, tip_ok_reads=99):
    """Execute the relay's ACTUAL dispatch script (extracted from the parsed
    YAML, never re-typed here) against a fake `gh`, and return
    (returncode, stdout, [(service, image_tag)] in dispatch order).

    The static pins prove the script SAYS the right thing. This proves it DOES
    it: which services it deploys, in which order, onto which tag.
    """
    bash = shutil.which("bash")
    jq = shutil.which("jq")
    assert bash and jq, "the relay dispatch rehearsal needs bash and jq on PATH"

    relay = _strict_yaml(
        (WORKFLOW.parent / "dispatch-staging-deploys.yml").read_text(
            encoding="utf-8"))

    history = [
        {"databaseId": rid, "displayTitle": title, "conclusion": "success"}
        for rid, title in ((10, web_title), (11, app_title))
        if title
    ]
    pending = [
        {"databaseId": 2000 + i,
         "displayTitle": f"Deploy leaf-platform staging {svc} ({image_tag})"}
        for i, svc in enumerate(("web", "app"))
    ]

    with tempfile.TemporaryDirectory() as tmp_name:
        tmp = Path(tmp_name)
        bindir = tmp / "bin"
        bindir.mkdir()
        (bindir / "gh").write_text(_FAKE_GH, encoding="utf-8")
        (bindir / "gh").chmod(0o755)
        (bindir / "sleep").write_text(
            "#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
        (bindir / "sleep").chmod(0o755)

        log = tmp / "dispatches.log"
        log.write_text("", encoding="utf-8")
        tip_file = tmp / "tip"
        tip_file.write_text(str(tip_ok_reads), encoding="utf-8")
        script = tmp / "dispatch.sh"
        script.write_text(
            _relay_deploy_step(relay["jobs"]["dispatch"])["run"],
            encoding="utf-8")

        env = dict(os.environ)
        env.update(
            PATH=f"{bindir}{os.pathsep}{env['PATH']}",
            INFRA_REPO="LEAF-Solar-Design/leaf-automation-aws-terraform",
            DEPLOY_WORKFLOW="deploy-leaf-platform-staging.yml",
            GITHUB_REPOSITORY="LEAF-Solar-Design/leaf-web-demo",
            BUILD_HEAD_SHA="deadbee",
            # Job-level env in the real workflow; the receipt payload and the
            # publish step name are both keyed on the build attempt, and the
            # `converged=true` line writes to GITHUB_OUTPUT. Unset, `set -u`
            # would abort the script the moment the loop completes.
            BUILD_RUN_ATTEMPT="1",
            GITHUB_RUN_ID="424242",
            GITHUB_OUTPUT=str(tmp / "github_output"),
            IMAGE_TAG=image_tag,
            GH_TOKEN="fake-pat",
            HOME_TOKEN="fake-token",
            FAKE_MAIN_SHA=main_sha,
            FAKE_TIP_FILE=str(tip_file),
            FAKE_RELATION=relation,
            FAKE_HISTORY=json.dumps(history),
            FAKE_HISTORY_FAILS="1" if history_fails else "0",
            FAKE_PENDING=json.dumps(pending),
            DISPATCH_LOG=str(log),
        )
        # cwd=tmp so the relay's own file writes (superseder-attempt, the
        # staging-converged.json receipt payload) land in the throwaway dir
        # rather than leaking into wherever pytest was invoked.
        proc = subprocess.run(
            [bash, str(script)], env=env, text=True, capture_output=True,
            cwd=str(tmp))

        deployed = []
        for line in log.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            parts = line.split()
            svc = next(p for p in parts if p.startswith("service="))
            tag = next(p for p in parts if p.startswith("image_tag="))
            deployed.append((svc.split("=", 1)[1], tag.split("=", 1)[1]))
        return proc.returncode, proc.stdout, deployed


WEB_OLD = "Deploy leaf-platform staging web (prod-1111111)"
WEB_NEW = "Deploy leaf-platform staging web (prod-2222222)"
APP_OLD = "Deploy leaf-platform staging app (prod-1111111)"
APP_NEW = "Deploy leaf-platform staging app (prod-2222222)"
TAG = "prod-9999999"

# THE NAMES PRODUCTION ACTUALLY USES. The fixtures above are the LEGACY
# `prod-<sha>` run names. The v3 digest-aware path names its runs
# `<sha>-<attempt>-<service>` (see CONVERGENCE_ID at the dispatch site), and
# every leaf-platform staging deploy run on 2026-08-24 carried that name. The
# ordering read matched `^prod-` only, so it silently stopped deciding the
# moment v3 went live while these legacy fixtures kept it green -- a test that
# modelled a grammar production had left behind. Both grammars are exercised.
SHA_OLD = "1" * 40
SHA_NEW = "2" * 40
WEB_NEW_V3 = f"Deploy leaf-platform staging web ({SHA_NEW}-1-web)"
APP_OLD_V3 = f"Deploy leaf-platform staging app ({SHA_OLD}-1-app)"
WEB_OLD_V3 = f"Deploy leaf-platform staging web ({SHA_OLD}-1-web)"
APP_NEW_V3 = f"Deploy leaf-platform staging app ({SHA_NEW}-2-app)"


def test_staging_relay_orders_the_starved_service_first() -> None:
    """The abandoned service takes the first slot, and ONLY the order moves.

    THE BUG: `for SERVICE in web app` was hardcoded, so web won every race.
    A relay superseded mid-release always dies before its SECOND service, so
    at any merge rate faster than one full release app never advanced while
    web advanced every time. Observed 2026-08-07: three consecutive relays
    produced web deploys with no app deploy between them, and staging ran a
    whole release split for over two hours.

    Every case below runs the real extracted dispatch script and asserts on
    the commands it actually issued.
    """
    # app trails web -> app goes FIRST, and still onto THIS relay's tag.
    rc, out, deployed = _rehearse_relay_dispatch(
        web_title=WEB_NEW, app_title=APP_OLD, relation="behind")
    assert rc == 0
    assert deployed == [("app", TAG), ("web", TAG)], deployed
    assert "trails web" in out

    # web trails app -> the default order already puts web first.
    rc, _, deployed = _rehearse_relay_dispatch(
        web_title=WEB_OLD, app_title=APP_NEW, relation="ahead")
    assert deployed == [("web", TAG), ("app", TAG)], deployed

    # Level pegging -> unchanged behaviour, no compare needed.
    rc, _, deployed = _rehearse_relay_dispatch(
        web_title=WEB_NEW, app_title=APP_NEW)
    assert deployed == [("web", TAG), ("app", TAG)], deployed

    # THE SAME THREE CASES IN THE GRAMMAR PRODUCTION ACTUALLY USES. A reader
    # that understands only `prod-<sha>` returns nothing for these, the guard
    # never fires, and the order silently reverts to the fixed `web app` this
    # read exists to remove. That is not hypothetical: it was the live state of
    # staging all of 2026-08-24, with these very assertions green above it.
    rc, out, deployed = _rehearse_relay_dispatch(
        web_title=WEB_NEW_V3, app_title=APP_OLD_V3, relation="behind")
    assert rc == 0
    assert deployed == [("app", TAG), ("web", TAG)], (
        "a v3-named deploy history must still let app take the first slot; "
        f"got {deployed}")
    assert "trails web" in out, (
        "the ordering read must ANNOUNCE that it decided, so a silent revert "
        "to the fixed order is visible in the relay log")

    rc, _, deployed = _rehearse_relay_dispatch(
        web_title=WEB_OLD_V3, app_title=APP_NEW_V3, relation="ahead")
    assert deployed == [("web", TAG), ("app", TAG)], deployed

    rc, _, deployed = _rehearse_relay_dispatch(
        web_title=WEB_NEW_V3, app_title=APP_NEW_V3)
    assert deployed == [("web", TAG), ("app", TAG)], deployed

    # Mixed grammars, which is what an operator dispatch between two relay
    # deploys actually produces: one service on a v3 convergence id, the other
    # on the legacy alias. Both must still be readable.
    rc, out, deployed = _rehearse_relay_dispatch(
        web_title=WEB_NEW_V3,
        app_title=f"Deploy leaf-platform staging app (sha-{SHA_OLD})",
        relation="behind")
    assert deployed == [("app", TAG), ("web", TAG)], deployed

    # THE READ IS BEST EFFORT AND MAY ONLY REORDER. Every degraded input still
    # deploys BOTH services onto the manifest's tag; none may drop a service,
    # change the tag, or fail the release.
    for name, kwargs in (
        ("history query fails outright", dict(
            web_title=WEB_NEW, app_title=APP_OLD, history_fails=True)),
        ("compare unavailable", dict(
            web_title=WEB_NEW, app_title=APP_OLD, relation="")),
        ("tags unrelated", dict(
            web_title=WEB_NEW, app_title=APP_OLD, relation="diverged")),
        ("a recovery deploy names no immutable tag", dict(
            web_title="Deploy leaf-platform staging web (live baseline)",
            app_title=APP_OLD, relation="behind")),
        ("no successful deploy on record for a service", dict(
            web_title=WEB_NEW, app_title="", relation="behind")),
    ):
        rc, _, deployed = _rehearse_relay_dispatch(**kwargs)
        assert rc == 0, f"{name} must not fail the release, got rc={rc}"
        assert deployed == [("web", TAG), ("app", TAG)], (
            f"{name} must fall back to both services in the fixed order, "
            f"got {deployed}")

    # ORDERING NO LONGER DECIDES WHO GETS SKIPPED, because nothing is skipped
    # for want of a dispatch.
    #
    # This case used to assert the opposite, and asserting it was right at the
    # time: under the serial design only the FIRST service was dispatched
    # unconditionally, the second sat behind another tip re-check, and a single
    # tip movement therefore cost the second service its dispatch outright.
    # sol-critic RED round 2 on PR #506 forced that cost to be named rather
    # than denied.
    #
    # On 2026-08-24 that cost stopped being acceptable: the read that was meant
    # to spread it across both services had been dead since v3 naming went live
    # (see the v3 grammar cases below), so it fell on app every single time.
    # Five consecutive relays between 16:05Z and 16:38Z landed a web deploy and
    # dispatched no app deploy at all, and staging ran split across two source
    # commits for hours. The dispatch phase now takes ONE tip read and issues
    # BOTH dispatches under it, so a tip that moves after that read no longer
    # strands the second leg.
    rc, out, deployed = _rehearse_relay_dispatch(
        web_title=WEB_NEW, app_title=APP_OLD, relation="behind",
        tip_ok_reads=1)
    assert rc == 0
    assert deployed == [("app", TAG), ("web", TAG)], (
        "one tip read covers both legs, so a tip that moves after it must not "
        f"strand the second service; got {deployed}")

    # THE RESIDUAL WINDOW, PINNED RATHER THAN DENIED. The cost is reduced, not
    # removed: a tip that has ALREADY moved when the single read is taken still
    # stops the whole release, and a relay cancelled between its two
    # `gh workflow run` calls still leaves one leg undispatched. The first of
    # those is rehearsable and asserted immediately below; the second is a
    # cancellation of the relay process itself, which this in-process rehearsal
    # cannot stage, exactly as the classifier's 95-minute timeout is covered
    # statically for the same reason.

    # And the stand-down still wins over ordering: main moved, nothing goes out.
    rc, out, deployed = _rehearse_relay_dispatch(
        web_title=WEB_NEW, app_title=APP_OLD, relation="behind",
        main_sha="0ther")
    assert (rc, deployed) == (0, [])
    assert "standing down" in out

    print("staging relay need-first ordering rehearsal: PASS")


# REHEARSAL_BUDGET is the classifier's wall-clock deadline for one case.
# Cases that DECIDE return in well under a second; cases that must fall to
# `unknown` do so by exhausting this budget, so it is the per-case cost of
# every failed-read scenario. It started at 2s and that was too tight once
# this suite also ran #506's ordering rehearsal: a decisive case could
# spend its whole budget on jq startup and answer `unknown`, failing the
# test for a reason that had nothing to do with the classifier. 8s is far
# above the observed sub-second decide path and still bounds the suite.
_CLASSIFIER_HARNESS = """set -euo pipefail
GITHUB_REPOSITORY=owner/repo
HOME_TOKEN=token
POLL_SECONDS=0.05
_END=$(( $(date +%%s) + ${REHEARSAL_BUDGET:-8} ))
before_deadline() { [ "$(date +%%s)" -lt "$_END" ]; }
%(classifier)s
superseder_deploys "$REHEARSAL_SHA"
"""

# The stub ROUTES on the requested sha and run id rather than answering
# every call the same way. Without that, a classifier that hardcoded the
# sha or the run id would be served the right fixture anyway and every
# case would pass while the lookup was broken.
_CLASSIFIER_FAKE_GH = """#!/usr/bin/env bash
url="$2"
case "$url" in
  *"/actions/runs?head_sha="*)
    if [ "${FAIL_RUNS:-0}" = 1 ]; then exit 1; fi
    req="${url#*head_sha=}"; req="${req%%&*}"
    if [ "$req" != "$WANT_SHA" ]; then printf '{"workflow_runs":[]}'; exit 0; fi
    printf '%s' "$RUNS_JSON" ;;
  *"/artifacts?per_page=100")
    if [ "${FAIL_ARTIFACTS:-0}" = 1 ]; then exit 1; fi
    rest="${url#*/actions/runs/}"; req="${rest%%/*}"
    if [ "$req" != "$WANT_RUN" ]; then
      printf '{"total_count":0,"artifacts":[]}'; exit 0
    fi
    printf '%s' "$ARTIFACTS_JSON" ;;
  *"/actions/runs/"*)
    if [ "${FAIL_RECORD:-0}" = 1 ]; then exit 1; fi
    req="${url##*/actions/runs/}"
    if [ "$req" != "$WANT_RUN" ]; then
      printf '{"status":"queued","conclusion":null,"run_attempt":1}'; exit 0
    fi
    printf '%s' "$RECORD_JSON" ;;
  *) exit 9 ;;
esac
"""


def check_staging_relay_classifier_behaviour(text: str) -> None:
    """EXECUTE the real classifier against stubbed API responses.

    WHY THIS EXISTS. Every guard below was previously pinned only as
    text, and sol-critic kept finding edits that changed behaviour while
    satisfying every pin: an always-true jq filter, a second
    `CONVERGER=`/`VERDICT=` assignment, a redefinition of
    `classify_superseder_once`, and an early `|| { echo yes; return 0; }`
    that still left four `|| return 1` occurrences behind. Pinning text
    was not converging, because the property that matters is what the
    function ANSWERS, not what it says.

    So this drives the actual bash lifted from the parsed YAML. The
    invariant is one line: NOTHING may answer `yes` unless the
    superseding build really published an unexpired, attempt-bound
    supply set. A redefinition or an early `echo yes` fails here no
    matter where it is hidden.
    """
    bash = shutil.which("bash")
    jq_bin = shutil.which("jq")
    assert bash and jq_bin, "the classifier rehearsal needs bash and jq on PATH"

    wf = _strict_yaml(text)
    code = _relay_deploy_step(wf["jobs"]["dispatch"])["run"]
    start = code.index("BUILD_WORKFLOW_PATH=")
    end = re.search(r"^superseder_deploys\(\) \{\n.*?^\}$", code[start:],
                    re.S | re.M)
    assert end, "the classifier and its polling wrapper must be extractable"
    classifier = code[start:start + end.end()]

    sha = "abc123"
    other_sha = "def456"
    supply = f"staging-supply-set-{sha}-attempt-2"
    other_supply = f"staging-supply-set-{other_sha}-attempt-2"
    marker = f"docs-noop-{sha}-attempt-2"
    build_path = ".github/workflows/build-platform-images.yml"
    good_run = ('{"workflow_runs":[{"id":11,"path":"%s",'
                '"name":"Build platform images"}]}' % build_path)
    ok_record = '{"status":"completed","conclusion":"success","run_attempt":2}'

    def arts(*names_expired, total=None):
        items = ",".join(
            '{"name":"%s","expired":%s}' % (n, "true" if e else "false")
            for n, e in names_expired)
        count = len(names_expired) if total is None else total
        return '{"total_count":%d,"artifacts":[%s]}' % (count, items)

    # (name, env overrides, expected answer)
    cases = [
        ("run-list read fails", {"FAIL_RUNS": "1", "BUDGET": "2"}, "unknown"),
        ("run-record read fails", {"FAIL_RECORD": "1", "BUDGET": "2"}, "unknown"),
        ("artifact read fails", {"FAIL_ARTIFACTS": "1", "BUDGET": "2"}, "unknown"),
        ("no build run for the sha",
         {"RUNS_JSON": '{"workflow_runs":[]}', "BUDGET": "2"}, "unknown"),
        ("a same-NAMED run at a different path is not our build",
         {"RUNS_JSON": '{"workflow_runs":[{"id":11,"path":'
                       '".github/workflows/impostor.yml",'
                       '"name":"Build platform images"}]}', "BUDGET": "2"}, "unknown"),
        ("success but NO artifacts at all",
         {"ARTIFACTS_JSON": arts()}, "unknown"),
        ("success with an unexpired supply set",
         {"ARTIFACTS_JSON": arts((supply, False))}, "yes"),
        ("success with an EXPIRED supply set",
         {"ARTIFACTS_JSON": arts((supply, True))}, "unknown"),
        ("artifact listing truncated",
         {"ARTIFACTS_JSON": arts((supply, False), total=9), "BUDGET": "2"}, "unknown"),
        ("artifact listing carries no total_count",
         {"ARTIFACTS_JSON": '{"artifacts":[]}', "BUDGET": "2"}, "unknown"),
        ("docs-noop marker for the CURRENT attempt",
         {"ARTIFACTS_JSON": arts((marker, False))}, "no"),
        ("a PREVIOUS attempt's marker must not outlive a rerun that built",
         {"ARTIFACTS_JSON": arts((f"docs-noop-{sha}-attempt-1", False),
                                 (supply, False))}, "yes"),
        ("the build concluded failure",
         {"ARTIFACTS_JSON": arts((supply, False)),
          "RECORD_JSON": '{"status":"completed","conclusion":"failure",'
                         '"run_attempt":2}'}, "unknown"),
        # THE ATTEMPT MUST COME FROM THE RUN RECORD, NOT FROM THE FIXTURE.
        # Every case above happens to sit on run_attempt 2, so a classifier
        # that hardcoded `SUP_ATTEMPT=2` would satisfy all of them. These two
        # move the record's attempt to 3 so only a classifier that actually
        # reads it can tell them apart.
        ("attempt 3 in flight, only the PREVIOUS attempt's supply set",
         {"ARTIFACTS_JSON": arts((supply, False)),
          "RECORD_JSON": '{"status":"completed","conclusion":"success",'
                         '"run_attempt":3}'}, "unknown"),
        ("attempt 3 in flight with its OWN supply set",
         {"ARTIFACTS_JSON": arts((f"staging-supply-set-{sha}-attempt-3", False)),
          "RECORD_JSON": '{"status":"completed","conclusion":"success",'
                         '"run_attempt":3}'}, "yes"),
        ("attempt 3 in flight, only the PREVIOUS attempt's docs marker",
         {"ARTIFACTS_JSON": arts((marker, False)),
          "RECORD_JSON": '{"status":"completed","conclusion":"success",'
                         '"run_attempt":3}'}, "unknown"),
        # THE REQUESTED SHA MUST COME FROM THE ARGUMENT. Every case above asks
        # for the same sha, so a classifier that hardcoded it would be served
        # the right fixture regardless.
        ("a DIFFERENT sha is asked for, and only the other sha's supply set "
         "exists",
         {"SHA": other_sha, "WANT_SHA": other_sha,
          "ARTIFACTS_JSON": arts((supply, False))}, "unknown"),
        ("a DIFFERENT sha is asked for, with its OWN supply set",
         {"SHA": other_sha, "WANT_SHA": other_sha,
          "ARTIFACTS_JSON": arts((other_supply, False))}, "yes"),
        # THE RUN ID MUST COME FROM THE RUN LIST. Every case above resolves to
        # run 11, so a classifier that hardcoded the id would still be served.
        ("the build resolves to run 12 while only run 11 is served",
         {"RUNS_JSON": '{"workflow_runs":[{"id":12,"path":"%s",'
                       '"name":"Build platform images"}]}' % build_path,
          "WANT_RUN": "11", "BUDGET": "2"}, "unknown"),
        ("the build resolves to run 12 and run 12 is served",
         {"RUNS_JSON": '{"workflow_runs":[{"id":12,"path":"%s",'
                       '"name":"Build platform images"}]}' % build_path,
          "WANT_RUN": "12"}, "yes"),
    ]

    with tempfile.TemporaryDirectory() as tmp_name:
        tmp = Path(tmp_name)
        fake_dir = tmp / "bin"
        fake_dir.mkdir()
        gh = fake_dir / "gh"
        gh.write_text(_CLASSIFIER_FAKE_GH, encoding="utf-8", newline="\n")
        gh.chmod(0o755)
        script = tmp / "rehearse.sh"
        script.write_text(
            _CLASSIFIER_HARNESS % {"classifier": classifier},
            encoding="utf-8", newline="\n")

        for name, overrides, expected in cases:
            env = dict(os.environ)
            env["PATH"] = f"{fake_dir}{os.pathsep}{env.get('PATH', '')}"
            env["REHEARSAL_SHA"] = overrides.get("SHA", sha)
            env["WANT_SHA"] = overrides.get("WANT_SHA", sha)
            env["WANT_RUN"] = overrides.get("WANT_RUN", "11")
            env["RUNS_JSON"] = overrides.get("RUNS_JSON", good_run)
            env["RECORD_JSON"] = overrides.get("RECORD_JSON", ok_record)
            env["ARTIFACTS_JSON"] = overrides.get(
                "ARTIFACTS_JSON", arts((supply, False)))
            for key in ("FAIL_RUNS", "FAIL_RECORD", "FAIL_ARTIFACTS"):
                env[key] = overrides.get(key, "0")
            # Cases that must EXPIRE to answer `unknown` pay their budget
            # in wall clock, so they get a short one. Cases that must
            # DECIDE get a generous one: the decide path is sub-second,
            # and an expiry there would fail the test for a reason that
            # has nothing to do with the classifier.
            env["REHEARSAL_BUDGET"] = overrides.get("BUDGET", "10")
            # cwd=tmp so the classifier's `superseder-attempt` write (added with
            # the convergence receipt, so the `yes` arm can name the receipt it
            # waits for) lands in the throwaway dir rather than dropping an
            # untracked file into wherever pytest ran.
            proc = subprocess.run(
                [bash, str(script)], env=env, text=True, capture_output=True,
                cwd=str(tmp))
            assert proc.returncode == 0, (
                f"classifier rehearsal {name!r} crashed rc={proc.returncode}\n"
                f"{proc.stdout}\n{proc.stderr}")
            answer = proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else ""
            assert answer == expected, (
                f"classifier rehearsal {name!r}: expected {expected!r}, got "
                f"{answer!r}. Answering `yes` without a proven unexpired "
                f"supply set is the reporting hole itself.")

    print(f"staging relay classifier rehearsal ({len(cases)} cases): PASS")


_MANIFEST_FAKE_GH = r"""#!/usr/bin/env bash
# Fake `gh` for the relay MANIFEST reconcile rehearsal. Answers the reads the
# docs-only reconcile makes, from the scenario in the environment, so the
# assertion is on the tag the reconcile ACTUALLY resolves, not on its text.
set -uo pipefail
args=("$@")
url="${args[1]:-}"
case "${args[0]}" in
  api)
    case "$url" in
      *"/actions/runs?branch=main"*)
        # Newest-first successful main build run scan. Rows are already in
        # "id sha attempt" shape; the real --jq reduces to the same.
        printf '%s\n' "$FAKE_ROWS"
        ;;
      *"/compare/"*)
        rest="${url#*/compare/}"
        base="${rest%%...*}"
        if printf '%s' "$FAKE_COMPARE_FAILS" | jq -e --arg s "$base" 'index($s) != null' >/dev/null; then
          echo "fake gh: simulated compare failure for $base" >&2
          exit 4
        fi
        printf '%s\n' "$(printf '%s' "$FAKE_RELATIONS" | jq -r --arg s "$base" '.[$s] // "diverged"')"
        ;;
      *"/artifacts/"*"/zip")
        # The listing call made the exact one-file provider archive.
        cat .pending_supply_set.zip
        ;;
      *"/artifacts?per_page=100")
        rest="${url#*/actions/runs/}"
        run_id="${rest%%/*}"
        rm -f .pending_supply_set.json .pending_supply_set.zip
        # The build run answers the docs-noop marker UNLESS the scenario gave
        # it a supply set of its own, which is the ORDINARY (non-docs-only)
        # path. Every scenario that predates this branch names build-run
        # artifacts nowhere in FAKE_SUPPLY_SETS, so their answer is unchanged.
        if [ "$run_id" = "$BUILD_RUN_ID" ] \
          && ! printf '%s' "$FAKE_SUPPLY_SETS" | jq -e --arg id "$run_id" 'has($id)' >/dev/null; then
          printf '%s\n' '{"total_count":1,"artifacts":[{"name":"docs-noop-'"$BUILD_HEAD_SHA"'-attempt-'"$BUILD_RUN_ATTEMPT"'","id":1,"expired":false}]}'
        else
          entry=$(printf '%s' "$FAKE_SUPPLY_SETS" | jq -c --arg id "$run_id" '.[$id] // empty')
          marker=$(printf '%s' "$FAKE_MARKERS" | jq -c --arg id "$run_id" '.[$id] // empty')
          if [ -n "$entry" ]; then
            sha=$(printf '%s' "$entry" | jq -r '.sha')
            att=$(printf '%s' "$entry" | jq -r '.attempt')
            tag=$(printf '%s' "$entry" | jq -r '.tag')
            rev=$(printf '%s' "$entry" | jq -r '.rev // .sha')
            tree=$(printf '%s:tree' "$rev" | sha1sum | awk '{print $1}')
            digest="sha256:$(printf '%064d' 0 | tr '0' 'a')"
            jq -n --arg rev "$rev" --arg tree "$tree" --arg tag "$tag" \
              --arg digest "$digest" --argjson run "$run_id" \
              '{schema:"leaf.staging-supply-set.v2",source_revision:$rev,
                source_tree:$tree,build_tag:$tag,
                speculative:{built_from_revision:$rev,pr_number:1,workflow_run_id:$run},
                services:{
                  app:{repository:"leaf-platform-app",image_digest:$digest,source_revision:$rev},
                  broker:{repository:"leaf-platform-broker",image_digest:$digest,source_revision:$rev},
                  "canonical-worker":{repository:"leaf-platform-canonical-worker",image_digest:$digest,source_revision:$rev,
                    provenance:{application_source_revision:$rev,solver_source_revision:$rev,
                      solver_source_sha256:"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}},
                  harness:{repository:"leaf-platform-harness",image_digest:$digest,source_revision:$rev},
                  web:{repository:"leaf-platform-web",image_digest:$digest,source_revision:$rev,
                    artifact_sha256:"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"}
                }}' \
              > .pending_supply_set.json
            python -c 'import zipfile; z=zipfile.ZipFile(".pending_supply_set.zip","w",zipfile.ZIP_DEFLATED); z.write(".pending_supply_set.json","staging-supply-set.json"); z.close()'
            archive_digest=$(sha256sum .pending_supply_set.zip | awk '{print $1}')
            printf '%s\n' '{"total_count":1,"artifacts":[{"name":"staging-supply-set-'"$sha"'-attempt-'"$att"'","id":'"$run_id"',"expired":false,"digest":"sha256:'"$archive_digest"'","workflow_run":{"id":'"$run_id"',"head_sha":"'"$sha"'"}}]}'
          elif [ -n "$marker" ]; then
            msha=$(printf '%s' "$marker" | jq -r '.sha')
            matt=$(printf '%s' "$marker" | jq -r '.attempt')
            printf '%s\n' '{"total_count":1,"artifacts":[{"name":"docs-noop-'"$msha"'-attempt-'"$matt"'","id":1,"expired":false}]}'
          else
            printf '%s\n' '{"total_count":0,"artifacts":[]}'
          fi
        fi
        ;;
      *"/actions/runs/"*)
        run_id="${url##*/actions/runs/}"
        entry=$(printf '%s' "$FAKE_SUPPLY_SETS" | jq -c --arg id "$run_id" '.[$id] // empty')
        [ -n "$entry" ] || { echo "fake gh: no producer run $run_id" >&2; exit 9; }
        sha=$(printf '%s' "$entry" | jq -r '.sha')
        att=$(printf '%s' "$entry" | jq -r '.attempt')
        jq -n --argjson id "$run_id" --argjson att "$att" --arg sha "$sha" \
          '{id:$id,run_attempt:$att,event:"push",head_sha:$sha,
            path:".github/workflows/build-platform-images.yml",status:"completed",
            conclusion:"success",head_branch:"main",
            repository:{full_name:"LEAF-Solar-Design/leaf-web-demo"},
            head_repository:{full_name:"LEAF-Solar-Design/leaf-web-demo"}}'
        ;;
      *"/git/commits/"*)
        source="${url##*/git/commits/}"
        tree=$(printf '%s:tree' "$source" | sha1sum | awk '{print $1}')
        jq -n --arg source "$source" --arg tree "$tree" \
          '{sha:$source,tree:{sha:$tree}}'
        ;;
      *) echo "fake gh: unhandled api $url" >&2; exit 9 ;;
    esac
    ;;
  *) echo "fake gh: unhandled ${args[0]}" >&2; exit 9 ;;
esac
"""


def _rehearse_relay_manifest(*, rows, supply_sets, relations, markers=None,
                             compare_fails=None,
                             build_run_id="100", build_head_sha="docshead",
                             build_attempt="1"):
    """Execute the relay's ACTUAL manifest script (extracted from the parsed
    YAML) against a fake gh, and return (returncode, combined_output,
    {output-key: value}) parsed from the step's GITHUB_OUTPUT.
    """
    bash = shutil.which("bash")
    jq = shutil.which("jq")
    assert bash and jq, "the manifest reconcile rehearsal needs bash and jq on PATH"

    # The production envelope refuses symbolic or shortened source identities.
    # Keep the human-readable scenario labels at each call site, then map them
    # to deterministic full SHA-40 values before the real script sees them.
    sha_map = {}
    for entry in supply_sets.values():
        label = entry.get("rev", entry["sha"])
        suffix = entry["tag"].removeprefix("prod-")
        if re.fullmatch(r"[0-9a-f]{7,40}", suffix):
            sha_map[label] = suffix + hashlib.sha1(
                label.encode("utf-8")
            ).hexdigest()[len(suffix):]
            if "rev" not in entry:
                sha_map[entry["sha"]] = sha_map[label]

    def full_sha(value: str) -> str:
        if re.fullmatch(r"[0-9a-f]{40}", value):
            return value
        return sha_map.get(value, hashlib.sha1(value.encode("utf-8")).hexdigest())

    normalized_rows = []
    for row in rows:
        run_id, sha, attempt = row.split()
        normalized_rows.append(f"{run_id} {full_sha(sha)} {attempt}")
    normalized_supply_sets = json.loads(json.dumps(supply_sets))
    for entry in normalized_supply_sets.values():
        entry["sha"] = full_sha(entry["sha"])
        if "rev" in entry:
            entry["rev"] = full_sha(entry["rev"])
    normalized_relations = {
        full_sha(sha): relation for sha, relation in relations.items()
    }
    normalized_markers = json.loads(json.dumps(markers or {}))
    for entry in normalized_markers.values():
        entry["sha"] = full_sha(entry["sha"])
    normalized_compare_fails = [full_sha(sha) for sha in (compare_fails or [])]
    build_head_sha = full_sha(build_head_sha)

    relay = _strict_yaml(
        (WORKFLOW.parent / "dispatch-staging-deploys.yml").read_text(
            encoding="utf-8"))
    manifest = next(s for s in relay["jobs"]["dispatch"]["steps"]
                    if s.get("id") == "manifest")

    with tempfile.TemporaryDirectory() as tmp_name:
        tmp = Path(tmp_name)
        bindir = tmp / "bin"
        bindir.mkdir()
        (bindir / "gh").write_text(_MANIFEST_FAKE_GH, encoding="utf-8", newline="\n")
        (bindir / "gh").chmod(0o755)
        out = tmp / "github_output"
        out.write_text("", encoding="utf-8")
        script = tmp / "manifest.sh"
        script.write_text(manifest["run"], encoding="utf-8", newline="\n")

        env = dict(os.environ)
        env.update(
            PATH=f"{bindir}{os.pathsep}{env['PATH']}",
            GITHUB_REPOSITORY="LEAF-Solar-Design/leaf-web-demo",
            GITHUB_RUN_ID="500",
            GITHUB_RUN_ATTEMPT="1",
            INFRA_REPO="LEAF-Solar-Design/leaf-automation-aws-terraform",
            DEPLOY_WORKFLOW="deploy-leaf-platform-staging.yml",
            BUILD_RUN_ID=build_run_id,
            BUILD_HEAD_SHA=build_head_sha,
            BUILD_RUN_ATTEMPT=build_attempt,
            GH_TOKEN="fake-token",
            GITHUB_OUTPUT=str(out),
            FAKE_ROWS="\n".join(normalized_rows),
            FAKE_SUPPLY_SETS=json.dumps(normalized_supply_sets),
            FAKE_RELATIONS=json.dumps(normalized_relations),
            FAKE_MARKERS=json.dumps(normalized_markers),
            FAKE_COMPARE_FAILS=json.dumps(normalized_compare_fails),
        )
        proc = subprocess.run(
            [bash, str(script)], env=env, text=True, capture_output=True,
            cwd=str(tmp))
        outputs = {}
        for line in out.read_text(encoding="utf-8").splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                outputs[key] = value
        return proc.returncode, proc.stdout + proc.stderr, outputs


def test_staging_relay_reconciles_a_docs_only_build() -> None:
    """A docs-only merge converges staging instead of stranding a split.

    Runs the extracted manifest script against a fake gh: the docs-only build
    published a marker and no supply set, so the reconcile scans main's
    successful build runs newest-first and takes the newest tag whose commit is
    an ANCESTOR of the tip (or IS the tip -- a same-head sibling build), setting
    deploy=true onto it. Then feeds that tag to the real dispatch script and
    asserts BOTH staging services deploy onto it -- the split-closing behaviour
    end to end. This is the DOES half of the static pins in the invariants test.
    """
    # Rows newest-first: 100 is the docs-only build itself (skipped), 98 carries
    # the newest tag whose commit IS an ancestor of the tip (must win). No newer
    # non-ancestor sits above it, so the reconcile lands on 98.
    rows = ["100 docshead 1", "98 ancestor 1"]
    supply = {"98": {"sha": "ancestor", "attempt": "1", "tag": "prod-abc1234"}}
    relations = {"ancestor": "ahead"}

    rc, out, outputs = _rehearse_relay_manifest(
        rows=rows, supply_sets=supply, relations=relations)
    assert rc == 0, out
    assert outputs.get("deploy") == "true", (out, outputs)
    assert outputs.get("image_tag") == "prod-abc1234", (out, outputs)

    # END TO END: hand the resolved tag to the REAL dispatch script; both
    # services land on it through the single watched dispatch site.
    rc2, out2, deployed = _rehearse_relay_dispatch(
        image_tag="prod-abc1234", web_title=None, app_title=None,
        relation="ahead")
    assert rc2 == 0, out2
    assert deployed == [("web", "prod-abc1234"), ("app", "prod-abc1234")], deployed

    # IDENTICAL IS USED, NOT SKIPPED (sol-critic RED round 4 on PR #519). Because
    # docs-only-ness is decided per-PUSH (the build gate diffs github.event.before
    # against the head), a SIBLING push can reach the tip's SAME head sha from a
    # different base, build real images, and publish a provenance-matched supply
    # set for the tip itself (e.g. main reverts away from a deployed commit then
    # returns to it). Run 99 is that sibling: head sha == the tip, valid supply
    # set, compares 'identical'. Its tag is the tip's OWN live images, so it MUST
    # win over the older ancestor 98 -- skipping it (the old `identical) continue`)
    # would roll staging BACKWARDS onto prod-abc1234.
    rc_id, out_id, outputs_id = _rehearse_relay_manifest(
        rows=["100 docshead 1", "99 docshead 1", "98 ancestor 1"],
        supply_sets={"99": {"sha": "docshead", "attempt": "1",
                            "tag": "prod-cafe123"},
                     "98": {"sha": "ancestor", "attempt": "1",
                            "tag": "prod-abc1234"}},
        relations={"docshead": "identical", "ancestor": "ahead"})
    assert rc_id == 0, out_id
    assert outputs_id.get("deploy") == "true", (out_id, outputs_id)
    assert outputs_id.get("image_tag") == "prod-cafe123", (
        "a same-head sibling build of the tip's own images must win over the "
        "older ancestor", out_id, outputs_id)
    assert outputs_id.get("image_tag") != "prod-abc1234", (out_id, outputs_id)

    # NEVER-BACKWARDS (sol-critic RED round 3 on PR #519). A NEWER valid supply
    # set whose commit is NON-ancestral -- 'diverged' (main was rewritten under
    # a deployed commit) or 'behind' (a deployable merge landed after this
    # docs-only tip) -- MUST fail closed, never skip to the older ancestor. That
    # skip IS the backwards deploy: run 99's newer tag may be live, so shipping
    # run 98's older tag rolls both services back. The old code lumped
    # 'behind'/'diverged' with a clean skip and shipped prod-abc1234.
    for newer_relation in ("diverged", "behind"):
        rc_nb, out_nb, outputs_nb = _rehearse_relay_manifest(
            rows=["100 docshead 1", "99 newer 1", "98 ancestor 1"],
            supply_sets={"99": {"sha": "newer", "attempt": "1",
                                "tag": "prod-new9999"},
                         "98": {"sha": "ancestor", "attempt": "1",
                                "tag": "prod-abc1234"}},
            relations={"newer": newer_relation, "ancestor": "ahead"})
        assert rc_nb != 0, (newer_relation, out_nb)
        assert "cannot reconcile safely" in out_nb, (newer_relation, out_nb)
        assert "may be live" in out_nb, (newer_relation, out_nb)
        assert outputs_nb.get("deploy") != "true", (newer_relation, outputs_nb)
        assert outputs_nb.get("image_tag") != "prod-abc1234", (
            "must not fall back to the older ancestor tag",
            newer_relation, outputs_nb)
        assert outputs_nb.get("image_tag") != "prod-new9999", (
            "and must not deploy the unproven newer tag either",
            newer_relation, outputs_nb)

    # NEGATIVE: a docs-only build whose ONLY candidate is a non-ancestor with a
    # valid supply set fails RED at the divergence -- it may be live, so the
    # reconcile refuses rather than skip-and-strand or deploy backwards.
    rc3, out3, outputs3 = _rehearse_relay_manifest(
        rows=["100 docshead 1", "97 orphan 1"],
        supply_sets={"97": {"sha": "orphan", "attempt": "1",
                            "tag": "prod-dead999"}},
        relations={"orphan": "diverged"})
    assert rc3 != 0, out3
    assert "cannot reconcile safely" in out3, out3
    assert "may be live" in out3, out3
    assert outputs3.get("deploy") != "true", outputs3

    # And a scan that finds ONLY docs-noop markers above (no ancestor supply
    # set at all) still fails RED at the END rather than exiting green
    # undeployed -- skipping was how a split used to survive a docs-only merge.
    rc3b, out3b, outputs3b = _rehearse_relay_manifest(
        rows=["100 docshead 1", "99 docsonly99 1"],
        supply_sets={},
        relations={},
        markers={"99": {"sha": "docsonly99", "attempt": "1"}})
    assert rc3b != 0, out3b
    assert "has nothing to reconcile onto" in out3b, out3b
    assert outputs3b.get("deploy") != "true", outputs3b

    # A newer DOCS-ONLY run (marker present, no supply set) between the tip
    # and the deployable ancestor must be SKIPPED, not mistaken for a
    # rollback risk -- otherwise the reconcile could never see past the first
    # docs-only commit.
    rc4, out4, outputs4 = _rehearse_relay_manifest(
        rows=["100 docshead 1", "99 docsonly99 1", "98 ancestor 1"],
        supply_sets={"98": {"sha": "ancestor", "attempt": "1",
                            "tag": "prod-abc1234"}},
        relations={"ancestor": "ahead"},
        markers={"99": {"sha": "docsonly99", "attempt": "1"}})
    assert rc4 == 0, out4
    assert outputs4.get("image_tag") == "prod-abc1234", (out4, outputs4)

    # DEFECT 1 (sol-critic RED round 2): a newer DEPLOYABLE run whose supply
    # set is missing (deleted / partial upload, and NO docs-noop marker) must
    # FAIL CLOSED, never fall back to the older ancestor -- that fallback is a
    # backwards deploy. The old `|| continue` skipped it and shipped prod-abc.
    rc5, out5, outputs5 = _rehearse_relay_manifest(
        rows=["100 docshead 1", "99 gonerun 1", "98 ancestor 1"],
        supply_sets={"98": {"sha": "ancestor", "attempt": "1",
                            "tag": "prod-abc1234"}},
        relations={"ancestor": "ahead"})
    assert rc5 != 0, out5
    assert "cannot reconcile safely" in out5, out5
    assert "neither a supply set nor a docs-noop marker" in out5, out5
    assert outputs5.get("deploy") != "true", outputs5
    assert outputs5.get("image_tag") != "prod-abc1234", (
        "must not fall back to the older ancestor tag", outputs5)

    # DEFECT 2 (sol-critic RED round 2): a supply set whose internal
    # source_revision does not equal its run's head sha must FAIL CLOSED -- it
    # could name an older revision and tag than the run it hangs off.
    rc6, out6, outputs6 = _rehearse_relay_manifest(
        rows=["100 docshead 1", "99 run99head 1"],
        supply_sets={"99": {"sha": "run99head", "attempt": "1",
                            "tag": "prod-abc1234", "rev": "olderrev"}},
        relations={"olderrev": "ahead", "run99head": "ahead"})
    assert rc6 != 0, out6
    assert "not the run's head" in out6, out6
    assert outputs6.get("deploy") != "true", outputs6

    # DEFECT 1 (sol-critic RED round 2): a TRANSIENT ancestry-compare failure
    # on a candidate must FAIL CLOSED, never `|| true`-skip to an older
    # ancestor (that fallback is the backwards deploy). Newer candidate 99's
    # compare fails; the reconcile must not fall back to 98's older tag.
    rc7, out7, outputs7 = _rehearse_relay_manifest(
        rows=["100 docshead 1", "99 flakerun 1", "98 ancestor 1"],
        supply_sets={"99": {"sha": "flakerun", "attempt": "1",
                            "tag": "prod-new9999"},
                     "98": {"sha": "ancestor", "attempt": "1",
                            "tag": "prod-abc1234"}},
        relations={"ancestor": "ahead"},
        compare_fails=["flakerun"])
    assert rc7 != 0, out7
    assert "could not be read" in out7, out7
    assert outputs7.get("deploy") != "true", outputs7
    assert outputs7.get("image_tag") != "prod-abc1234", (
        "a failed compare must not fall back to the older ancestor tag",
        outputs7)


def _rehearse_relay_v3_identity_gate(*, release_source, release_attempt,
                                     artifact_name):
    """Run the REAL dispatch step's v3 gate and return (returncode, output).

    Everything the gate needs before the convergence-identity binding is
    supplied; nothing else is faked, so the script either refuses at that
    binding or gets past it. `gh` is a hard failure here on purpose: reaching
    a gh call at all means the gate ACCEPTED the identity, which the caller
    distinguishes by the message, never by the exit code alone.
    """
    bash = shutil.which("bash")
    assert bash, "the v3 identity gate rehearsal needs bash on PATH"
    relay = _strict_yaml(
        (WORKFLOW.parent / "dispatch-staging-deploys.yml").read_text(
            encoding="utf-8"))
    with tempfile.TemporaryDirectory() as tmp_name:
        tmp = Path(tmp_name)
        script = tmp / "dispatch.sh"
        script.write_text(
            _relay_deploy_step(relay["jobs"]["dispatch"])["run"],
            encoding="utf-8", newline="\n")
        # A refusing `gh` keeps this rehearsal OFFLINE. The gate under test runs
        # before any gh call, so the stub only bounds what happens after it.
        bindir = tmp / "bin"
        bindir.mkdir()
        (bindir / "gh").write_text(
            "#!/usr/bin/env bash\necho 'fake gh: not reachable in this "
            "rehearsal' >&2\nexit 9\n", encoding="utf-8", newline="\n")
        (bindir / "gh").chmod(0o755)
        env = dict(os.environ)
        env.update(
            PATH=f"{bindir}{os.pathsep}{env['PATH']}",
            INFRA_REPO="LEAF-Solar-Design/leaf-automation-aws-terraform",
            DEPLOY_WORKFLOW="deploy-leaf-platform-staging.yml",
            GITHUB_REPOSITORY="LEAF-Solar-Design/leaf-web-demo",
            GITHUB_RUN_ID="424242",
            GITHUB_OUTPUT=str(tmp / "github_output"),
            BUILD_HEAD_SHA="d" * 40,
            BUILD_RUN_ATTEMPT="1",
            IMAGE_TAG="surface-v1-" + "e" * 64,
            GH_TOKEN="fake-pat",
            HOME_TOKEN="fake-token",
            SUPPLY_SCHEMA="leaf.staging-supply-set.v3",
            SUPPLY_SHA256="f" * 64,
            SUPPLY_ARTIFACT_ID="7",
            SUPPLY_ARTIFACT_NAME=artifact_name,
            SUPPLY_EVIDENCE_B64="ZXZpZGVuY2U",
            RELEASE_SOURCE=release_source,
            RELEASE_ATTEMPT=release_attempt,
            DIGEST_AWARE_CONVERGENCE_ENABLED="true",
            CONSUMER_CONTRACT_B64="Y29udHJhY3Q",
            TF_CONTRACT_HEAD="a" * 40,
            TF_CONSUMER_BLOB="b" * 40,
            TF_CONTRACT_RUN_ID="99",
        )
        proc = subprocess.run(
            [bash, str(script)], env=env, text=True, capture_output=True,
            cwd=str(tmp))
        return proc.returncode, proc.stdout + proc.stderr


def test_relay_convergence_identity_names_the_images_not_the_docs_only_tip() -> None:
    """The convergence id must name the ENVELOPE's release, not the tip.

    THE DEADLOCK THIS PINS (observed 2026-08-17, leaf-automation runs
    32019452787 and 32026370399, step "Verify one closed supply envelope
    before provider credentials": "closed supply evidence refused: v3 routed
    convergence identity"). A docs-only main commit builds no images, so its
    build publishes a docs-noop marker and NO supply set, and the relay
    deliberately reconciles onto the newest ancestor's supply set. The relay
    nonetheless stamped CONVERGENCE_ID from its OWN docs-only head, while the
    consumer binds the id to the ENVELOPE's producer source. Those two can
    never agree on that path, so every docs-only merge refused three
    dispatches and then failed "Staging is NOT converged".

    The fix is relay-side and the consumer's guard is untouched: the deploy
    workflow still accepts exactly one id per envelope,
    `<envelope producer source>-<attempt>-<service>`, so the previous commit's
    image still cannot land under a new identity. What changed is only which
    identity this relay computes -- the one belonging to the images it is
    actually deploying.
    """
    docs_head = hashlib.sha1(b"docshead").hexdigest()

    # DOCS-ONLY RECONCILE. Tip is run 100 (marker only); the images come from
    # run 98, attempt 3. The release identity must follow run 98.
    rc, out, outputs = _rehearse_relay_manifest(
        rows=["100 docshead 1", "98 ancestor 3"],
        supply_sets={"98": {"sha": "ancestor", "attempt": "3",
                            "tag": "prod-abc1234"}},
        relations={"ancestor": "ahead"})
    assert rc == 0, out
    assert outputs.get("deploy") == "true", (out, outputs)
    release_source = outputs.get("release_source", "")
    release_attempt = outputs.get("release_attempt", "")
    assert re.fullmatch(r"[0-9a-f]{40}", release_source), (out, outputs)
    assert release_attempt == "3", (
        "the release attempt must be the CANDIDATE build's, not the tip's",
        out, outputs)
    assert release_source != docs_head, (
        "naming the docs-only tip here is the deadlock itself", out, outputs)
    assert release_source.startswith("abc1234"), (
        "the release source must be the commit behind the deployed tag "
        "prod-abc1234", out, outputs)
    # THE CONSUMER'S RELATION, RESTATED. The deploy workflow requires the
    # supply artifact name to be staging-supply-set-<source>-attempt-<attempt>
    # for the same source and attempt it requires in the convergence id, so a
    # relay whose release identity satisfies this cannot produce an id the
    # consumer refuses.
    assert outputs.get("artifact_name") == (
        f"staging-supply-set-{release_source}-attempt-{release_attempt}"
    ), (out, outputs)

    # ORDINARY PATH IS UNCHANGED. When the build publishes its own supply set,
    # the release identity IS the build head and attempt, so the id this relay
    # now computes is byte-identical to the one it computed before.
    # Spelled as a literal SHA-40 (the harness passes 40-hex through its label
    # mapping untouched) so "the release source IS the build head" is asserted
    # against one value, not against the harness's label arithmetic.
    own_head = "abc1234" + "0" * 33
    rc_ord, out_ord, outputs_ord = _rehearse_relay_manifest(
        rows=[f"100 {own_head} 1"],
        supply_sets={"100": {"sha": own_head, "attempt": "1",
                             "tag": "prod-abc1234"}},
        relations={},
        build_run_id="100",
        build_head_sha=own_head)
    assert rc_ord == 0, out_ord
    assert outputs_ord.get("deploy") == "true", (out_ord, outputs_ord)
    assert outputs_ord.get("release_source") == own_head, (
        "the ordinary path's release identity is the build head",
        out_ord, outputs_ord)
    assert outputs_ord.get("release_attempt") == "1", (out_ord, outputs_ord)

    # THE DISPATCH SIDE COMPOSES THE ID FROM THAT IDENTITY, and the surface
    # result it demands back is checked against the same revision. Both were
    # keyed on $BUILD_HEAD_SHA, which is two independent refusals of the same
    # legitimate deploy; neither spelling may come back.
    relay = _strict_yaml(
        (WORKFLOW.parent / "dispatch-staging-deploys.yml").read_text(
            encoding="utf-8"))
    code = _executable_bash(_relay_deploy_step(relay["jobs"]["dispatch"])["run"])
    assert 'CONVERGENCE_ID="$RELEASE_SOURCE-$RELEASE_ATTEMPT-$SERVICE"' in code
    assert 'CONVERGENCE_ID="$BUILD_HEAD_SHA-$BUILD_RUN_ATTEMPT-$SERVICE"' not in code
    assert 'jq -e --arg release "$RELEASE_SOURCE"' in code
    assert 'jq -e --arg release "$BUILD_HEAD_SHA"' not in code
    assert (
        '[ "$SUPPLY_ARTIFACT_NAME" = '
        '"staging-supply-set-$RELEASE_SOURCE-attempt-$RELEASE_ATTEMPT" ]'
    ) in code
    # The convergence RECEIPT stays keyed on the TIP: a superseded relay waits
    # on staging-converged-<tip sha>-attempt-<tip attempt>, which is computed
    # from the build record, not from the release identity.
    assert '--arg sha "$BUILD_HEAD_SHA"' in code

    # NEGATIVE, EXECUTED: a release identity that does NOT name the supply
    # artifact this relay carries must refuse BEFORE any dispatch. This is the
    # genuine mismatch case -- an unrelated or merely descendant sha -- and it
    # is exactly what the consumer's guard exists to stop, so the relay must
    # not be able to emit it either.
    good_name = f"staging-supply-set-{'1' * 40}-attempt-2"
    for label, source, attempt in (
        ("an unrelated sha", "2" * 40, "2"),
        ("the right sha at the wrong attempt", "1" * 40, "9"),
        ("a truncated sha", "1" * 12, "2"),
        ("a non-numeric attempt", "1" * 40, "one"),
        ("an empty release source", "", "2"),
    ):
        rc_bad, out_bad = _rehearse_relay_v3_identity_gate(
            release_source=source, release_attempt=attempt,
            artifact_name=good_name)
        assert rc_bad != 0, (label, out_bad)
        assert "::error::V3 relay" in out_bad, (label, out_bad)
        assert "convergence id" in out_bad, (label, out_bad)
        assert "Dispatched" not in out_bad, (
            "the relay must refuse BEFORE dispatching anything", label, out_bad)

    # POSITIVE, EXECUTED: the matching identity gets PAST the gate. Proven by
    # the docs-only notice, which is printed after the binding and before any
    # dispatch; the run then dies on the unfaked gh, which is expected.
    rc_ok, out_ok = _rehearse_relay_v3_identity_gate(
        release_source="1" * 40, release_attempt="2", artifact_name=good_name)
    assert "::error::V3 relay" not in out_ok, out_ok
    assert "Docs-only reconcile" in out_ok, (
        "a release identity that differs from the tip must be announced, "
        "not refused", out_ok)


def test_build_platform_images_workflow_invariants() -> None:
    # Pytest entry point: the gate runner counts collected tests, and a bare
    # main() collects as zero.
    main()


def test_unbound_shell_ref_pin_is_red_on_mutation() -> None:
    """The unbound-variable pin must actually catch a renamed interpolation."""
    good = 'X=1\necho "$X"\n'
    assert _unquoted_shell_refs(good) == {"X"}
    # A jq program's $name is bound by --arg, not by the shell, so it must NOT
    # be collected; otherwise the pin fires on every jq call in the step.
    jq_only = "jq -r --arg want \"$X\" '[.[] | select(.title == $want)]'\n"
    assert _unquoted_shell_refs(jq_only) == {"X"}
    # An apostrophe inside a DOUBLE-quoted string is literal to bash. Reading it
    # as a quote flips the scanner's state and every jq --arg name after it
    # leaks in as a false positive, which is how the first cut of this pin
    # failed.
    apostrophe = 'echo "this relay\'s budget"\njq -e \'select(.x == $n)\' f\n'
    assert _unquoted_shell_refs(apostrophe) == set()
    # The exact defect this pin exists for: an error message left holding the
    # pre-rename spelling.
    renamed = 'CONCLUSION=x\necho "concluded $conclusion"\n'
    refs = _unquoted_shell_refs(renamed)
    assert "conclusion" in refs and "CONCLUSION" not in refs
    assert "conclusion" not in _shell_assigned_names(renamed)
    assert "CONCLUSION" in _shell_assigned_names(renamed)
    # An assignment in a condition (`if VAR=$(...)`) still binds the name.
    assert "VERDICT" in _shell_assigned_names('if VERDICT=$(f); then :; fi\n')


def test_jq_var_pin_is_red_on_mutation() -> None:
    """The jq pin must catch a shell-side rename that reached into a filter."""
    good = "jq -e --arg convergence \"$CONVERGENCE_ID\" '.id == $convergence' f\n"
    assert _jq_program_vars(good) == {"convergence"}
    assert _jq_bound_names(good) == {"convergence"}
    # The exact defect: the filter renamed to the SHELL variable's spelling.
    broken = "jq -e --arg convergence \"$CONVERGENCE_ID\" '.id == $CONVERGENCE_ID' f\n"
    assert _jq_program_vars(broken) - _jq_bound_names(broken) == {"CONVERGENCE_ID"}
    # A quoted heredoc body is another language; its apostrophes must not be
    # read as shell quotes or every span boundary after it is wrong.
    heredoc = (
        "python3 - <<'PY'\n"
        "x = \"it's fine\"\n"
        "PY\n"
        "jq -e --arg a \"$A\" '.k == $a' f\n"
    )
    assert _jq_program_vars(heredoc) == {"a"}


def test_staging_relay_cannot_leave_a_service_undeployed() -> None:
    # Named separately from the mega-test so a scoreboard failure says which
    # invariant broke: this one is "a dispatched deploy is always accounted
    # for", the recurring defect diagnosed on 2026-08-07.
    relay = WORKFLOW.parent / "dispatch-staging-deploys.yml"
    check_staging_relay_convergence(relay.read_text(encoding="utf-8"))
    check_staging_relay_convergence_battery(relay)
    check_staging_relay_classifier_behaviour(relay.read_text(encoding="utf-8"))


def _supply_evidence_python() -> str:
    relay = _strict_yaml(
        (WORKFLOW.parent / "dispatch-staging-deploys.yml").read_text(
            encoding="utf-8"
        )
    )
    manifest = next(
        step
        for step in relay["jobs"]["dispatch"]["steps"]
        if step.get("id") == "manifest"
    )["run"]
    match = re.search(
        r"python - <<'SUPPLY_EVIDENCE_PY'\n(.*?)\n\s*SUPPLY_EVIDENCE_PY",
        manifest,
        re.S,
    )
    assert match, "the closed supply evidence producer must stay extractable"
    return match.group(1)


_SUPPLY_SOURCE = "aa7a7c9c5ad5021bea68b24843da3f197dd07ceb"
_SUPPLY_TREE = "2a291fd10d60f844850bc1efda5ed206eadeac60"
_SUPPLY_DIGESTS = {
    "app": "sha256:1d9f84c0d98b0e87b830f194d614d961c0b40c907f17ec917b10b4d0fa913d3b",
    "broker": "sha256:6cd7bceaba696e2a29ec677b4d3ca34e4f5f11bdf39241c04b7d7e3c7373affa",
    "canonical-worker": "sha256:ccbc68ff1bc16eded47fcf2664b6eec3715b182655f8b8d0b06217cc98d9f323",
    "harness": "sha256:d35c4107cb37d36f967007dfea5329cc7cf9bee867449c8fce76dea6c03a9a61",
    "web": "sha256:d664a928c224c5c27412b3c18c8b2db89a1db740c3f8f895fc134263e3231dd2",
}
_SUPPLY_REPOSITORIES = {
    "app": "leaf-platform-app",
    "broker": "leaf-platform-broker",
    "canonical-worker": "leaf-platform-canonical-worker",
    "harness": "leaf-platform-harness",
    "web": "leaf-platform-web",
}


def _real_v2_supply_manifest() -> dict:
    services = {
        name: {
            "image_digest": _SUPPLY_DIGESTS[name],
            "repository": _SUPPLY_REPOSITORIES[name],
            "source_revision": _SUPPLY_SOURCE,
        }
        for name in _SUPPLY_DIGESTS
    }
    services["canonical-worker"]["provenance"] = {
        "application_source_revision": _SUPPLY_SOURCE,
        "solver_source_revision": "3ae53e274a5c6be3edeab30054234d09fdd74b41",
        "solver_source_sha256": (
            "c50ab70db1802f36af2af1ac24f8177d347a1083b9caf4bc85009310addcd721"
        ),
    }
    services["web"]["artifact_sha256"] = (
        "0c330d1e7460eeb5f74c5777c75223831609ded68b199442b8687c2e5192f6af"
    )
    return {
        "build_tag": "prod-aa7a7c9",
        "schema": "leaf.staging-supply-set.v2",
        "services": services,
        "source_revision": _SUPPLY_SOURCE,
        "source_tree": _SUPPLY_TREE,
        "speculative": {
            "built_from_revision": "a67c8860594b428f5380b1587cb280d523e32e64",
            "pr_number": 591,
            "workflow_run_id": 31738039215,
        },
    }


def _run_supply_evidence(manifest, **overrides):
    env = dict(os.environ)
    env.update(
        GITHUB_REPOSITORY="LEAF-Solar-Design/leaf-web-demo",
        GITHUB_RUN_ID="31738400000",
        GITHUB_RUN_ATTEMPT="1",
        SUPPLY_ARTIFACT_ID="9196079750",
        SUPPLY_ARTIFACT_NAME=(
            "staging-supply-set-aa7a7c9c5ad5021bea68b24843da3f197dd07ceb-"
            "attempt-1"
        ),
        SUPPLY_ARTIFACT_PROVIDER_SHA256=(
            "d3787ed751ee6271b1a8bbb8bb8f4c8165d1d48ab4a0c76ff968cbb9ad3ad186"
        ),
        SUPPLY_PRODUCER_RUN_ID="31738360788",
        SUPPLY_PRODUCER_RUN_ATTEMPT="1",
        SUPPLY_PRODUCER_EVENT="push",
        SUPPLY_PRODUCER_SOURCE_REVISION=_SUPPLY_SOURCE,
        SUPPLY_PRODUCER_SOURCE_TREE=_SUPPLY_TREE,
        SUPPLY_DISPATCH_IMAGE_TAG="prod-aa7a7c9",
    )
    env.update({key: str(value) for key, value in overrides.items()})
    with tempfile.TemporaryDirectory() as tmp_name:
        tmp = Path(tmp_name)
        raw = manifest if isinstance(manifest, str) else json.dumps(manifest)
        (tmp / "staging-supply-set.json").write_text(
            raw, encoding="utf-8", newline="\n"
        )
        return subprocess.run(
            [sys.executable, "-c", _supply_evidence_python()],
            cwd=tmp,
            env=env,
            text=True,
            capture_output=True,
        )


def _decode_supply_evidence(encoded: str) -> dict:
    import base64

    padding = "=" * (-len(encoded) % 4)
    return json.loads(base64.urlsafe_b64decode(encoded + padding))


def test_relay_mints_closed_real_v2_supply_evidence() -> None:
    manifest = _real_v2_supply_manifest()
    proc = _run_supply_evidence(manifest)
    assert proc.returncode == 0, proc.stderr
    encoded = proc.stdout.strip()
    assert encoded and "=" not in encoded
    evidence = _decode_supply_evidence(encoded)
    assert set(evidence) == {
        "manifest", "producer", "relay", "schema", "supply_artifact"
    }
    assert evidence["schema"] == "leaf.staging-supply-dispatch-evidence.v1"
    assert evidence["producer"] == {
        "event": "push",
        "repository": "LEAF-Solar-Design/leaf-web-demo",
        "run_attempt": 1,
        "run_id": 31738360788,
        "source_revision": _SUPPLY_SOURCE,
        "source_tree": _SUPPLY_TREE,
        "workflow_path": ".github/workflows/build-platform-images.yml",
    }
    assert evidence["supply_artifact"] == {
        "id": 9196079750,
        "name": (
            "staging-supply-set-aa7a7c9c5ad5021bea68b24843da3f197dd07ceb-"
            "attempt-1"
        ),
        "provider_archive_sha256": (
            "d3787ed751ee6271b1a8bbb8bb8f4c8165d1d48ab4a0c76ff968cbb9ad3ad186"
        ),
    }
    assert evidence["manifest"]["dispatch_image_tag"] == "prod-aa7a7c9"
    raw_manifest = base64.urlsafe_b64decode(
        evidence["manifest"]["json_b64"]
        + "=" * (-len(evidence["manifest"]["json_b64"]) % 4)
    )
    assert json.loads(raw_manifest) == manifest
    assert evidence["manifest"]["sha256"] == hashlib.sha256(
        json.dumps(manifest).encode("utf-8")
    ).hexdigest()
    decoded = json.dumps(evidence, separators=(",", ":"), sort_keys=True)
    assert len(decoded.encode("utf-8")) <= 16_384
    assert not re.search(r"(?i)(secret|token|password|authorization)", decoded)


def test_relay_mints_closed_real_shape_v3_supply_evidence() -> None:
    services = {}
    for index, name in enumerate(_SUPPLY_DIGESTS, start=1):
        surface_fingerprint = format(index + 2, "064x")
        row = {
            "build_disposition": "built",
            "image_digest": _SUPPLY_DIGESTS[name],
            "immutable_lookup_tag": f"surface-v1-{surface_fingerprint}",
            "producer_run_attempt": 1,
            "producer_run_id": 31738360788,
            "producer_source_revision": _SUPPLY_SOURCE,
            "producer_source_tree": _SUPPLY_TREE,
            "producer_workflow_blob": "1" * 40,
            "producer_workflow_path": ".github/workflows/build-platform-images.yml",
            "provenance_digest": _SUPPLY_DIGESTS[name],
            "provenance_subject": (
                "807034087062.dkr.ecr.us-east-1.amazonaws.com/"
                f"{_SUPPLY_REPOSITORIES[name]}"
            ),
            "recipe_fingerprint": "2" * 64,
            "repository": _SUPPLY_REPOSITORIES[name],
            "surface_fingerprint": surface_fingerprint,
        }
        if name == "canonical-worker":
            row["solver_provenance"] = {
                "solver_source_revision": "3ae53e274a5c6be3edeab30054234d09fdd74b41",
                "solver_source_sha256": "4" * 64,
            }
        if name == "web":
            row["artifact_sha256"] = "5" * 64
        services[name] = row
    services["broker"].update(
        build_disposition="reused",
        producer_run_attempt=2,
        producer_run_id=31730000000,
        producer_source_revision="b" * 40,
        producer_source_tree="c" * 40,
    )
    manifest = {
        "build_run_attempt": 1,
        "build_run_id": 31738360788,
        "release_source_revision": _SUPPLY_SOURCE,
        "release_source_tree": _SUPPLY_TREE,
        "schema": "leaf.staging-supply-set.v3",
        "services": services,
    }
    proc = _run_supply_evidence(
        manifest, SUPPLY_DISPATCH_IMAGE_TAG=f"v3-{_SUPPLY_SOURCE[:12]}"
    )
    assert proc.returncode == 0, proc.stderr
    evidence = _decode_supply_evidence(proc.stdout.strip())
    assert evidence["manifest"]["schema"] == "leaf.staging-supply-set.v3"
    raw_manifest = base64.urlsafe_b64decode(
        evidence["manifest"]["json_b64"]
        + "=" * (-len(evidence["manifest"]["json_b64"]) % 4)
    )
    carried_manifest = json.loads(raw_manifest)
    assert all(
        row["immutable_lookup_tag"]
        == f"surface-v1-{row['surface_fingerprint']}"
        for row in carried_manifest["services"].values()
    )
    entries = carried_manifest["services"]
    assert entries["app"]["producer_run_id"] == 31738360788
    assert entries["broker"]["build_disposition"] == "reused"
    assert entries["broker"]["producer_run_id"] == 31730000000

    negatives = []
    wrong_outer = json.loads(json.dumps(manifest))
    wrong_outer["build_run_id"] = 31738360789
    negatives.append(("outer build run", wrong_outer))
    wrong_attempt = json.loads(json.dumps(manifest))
    wrong_attempt["build_run_attempt"] = 2
    negatives.append(("outer build attempt", wrong_attempt))
    wrong_built_run = json.loads(json.dumps(manifest))
    wrong_built_run["services"]["app"]["producer_run_id"] = 31738360789
    negatives.append(("built service run", wrong_built_run))
    wrong_built_source = json.loads(json.dumps(manifest))
    wrong_built_source["services"]["app"]["producer_source_revision"] = "d" * 40
    negatives.append(("built service source", wrong_built_source))
    malformed_reuse = json.loads(json.dumps(manifest))
    malformed_reuse["services"]["broker"]["producer_run_attempt"] = 0
    negatives.append(("reused service attempt", malformed_reuse))
    coordinated_wrong_tree = json.loads(json.dumps(manifest))
    coordinated_wrong_tree["release_source_tree"] = "d" * 40
    for row in coordinated_wrong_tree["services"].values():
        if row["build_disposition"] == "built":
            row["producer_source_tree"] = "d" * 40
    negatives.append(("coordinated outer and built tree", coordinated_wrong_tree))
    extra_service_key = json.loads(json.dumps(manifest))
    extra_service_key["services"]["harness"]["caller_authority"] = "forged"
    negatives.append(("extra v3 service key", extra_service_key))
    for label, lookup_tag in (
        ("prod lookup tag rebinding", "prod-aa7a7c9"),
        ("sha lookup tag rebinding", f"sha-{_SUPPLY_SOURCE}"),
        ("surface lookup tag rebinding", f"surface-v1-{'f' * 64}"),
    ):
        rebound = json.loads(json.dumps(manifest))
        rebound["services"]["app"]["immutable_lookup_tag"] = lookup_tag
        negatives.append((label, rebound))
    for name, candidate in negatives:
        rejected = _run_supply_evidence(
            candidate, SUPPLY_DISPATCH_IMAGE_TAG=f"v3-{_SUPPLY_SOURCE[:12]}"
        )
        assert rejected.returncode != 0, (name, rejected.stdout, rejected.stderr)
        assert not rejected.stdout.strip(), (name, rejected.stdout)


def test_relay_supply_evidence_fails_closed_on_rebinding_and_malformed_input() -> None:
    base = _real_v2_supply_manifest()
    cases = []

    short_source = json.loads(json.dumps(base))
    short_source["source_revision"] = _SUPPLY_SOURCE[:7]
    cases.append(("short-only source", short_source, {}))

    wrong_source = json.loads(json.dumps(base))
    wrong_source["source_revision"] = "b" * 40
    cases.append(("source/run mismatch", wrong_source, {}))

    wrong_tree = json.loads(json.dumps(base))
    wrong_tree["source_tree"] = "d" * 40
    cases.append(("source/tree mismatch", wrong_tree, {}))

    bad_digest = json.loads(json.dumps(base))
    bad_digest["services"]["app"]["image_digest"] = "prod-aa7a7c9"
    cases.append(("tag-only service evidence", bad_digest, {}))

    wrong_tag = json.loads(json.dumps(base))
    wrong_tag["build_tag"] = "prod-bbbbbbb"
    cases.append(("tag/source mismatch", wrong_tag, {}))

    foreign_service = json.loads(json.dumps(base))
    foreign_service["services"]["broker"]["repository"] = "foreign-broker"
    cases.append(("foreign service", foreign_service, {}))

    missing_service = json.loads(json.dumps(base))
    del missing_service["services"]["harness"]
    cases.append(("missing service", missing_service, {}))

    extra_key = json.loads(json.dumps(base))
    extra_key["caller_authority"] = "forged"
    cases.append(("extra manifest key", extra_key, {}))

    mismatch = json.loads(json.dumps(base))
    mismatch["schema"] = "leaf.staging-supply-set.v3"
    cases.append(("v2/v3 shape mismatch", mismatch, {}))

    cases.extend([
        ("foreign repository", base, {"GITHUB_REPOSITORY": "other/repo"}),
        ("wrong producer event", base, {"SUPPLY_PRODUCER_EVENT": "workflow_dispatch"}),
        ("wrong artifact", base, {"SUPPLY_ARTIFACT_ID": "not-an-id"}),
        ("wrong artifact name", base, {"SUPPLY_ARTIFACT_NAME": "prod-aa7a7c9"}),
        ("wrong archive digest", base, {"SUPPLY_ARTIFACT_PROVIDER_SHA256": "short"}),
        ("wrong run", base, {"SUPPLY_PRODUCER_RUN_ID": "0"}),
    ])
    for name, manifest, overrides in cases:
        proc = _run_supply_evidence(manifest, **overrides)
        assert proc.returncode != 0, (name, proc.stdout, proc.stderr)
        assert not proc.stdout.strip(), (name, proc.stdout)

    duplicate = json.dumps(base).replace(
        '"schema": "leaf.staging-supply-set.v2"',
        '"schema": "leaf.staging-supply-set.v2", '
        '"schema": "leaf.staging-supply-set.v2"',
        1,
    )
    proc = _run_supply_evidence(duplicate)
    assert proc.returncode != 0
    assert not proc.stdout.strip()


def test_relay_binds_provider_archive_and_dispatches_one_unchanged_envelope() -> None:
    relay = _strict_yaml(
        (WORKFLOW.parent / "dispatch-staging-deploys.yml").read_text(
            encoding="utf-8"
        )
    )
    job = relay["jobs"]["dispatch"]
    manifest_code = _executable_bash(
        next(step for step in job["steps"] if step.get("id") == "manifest")["run"]
    )
    dispatch_code = _executable_bash(_relay_deploy_step(job)["run"])
    for proof in (
        'artifact_count" -eq 1',
        '.workflow_run.id == $run',
        '.path == $path',
        '.event == "push"',
        'git/commits/$producer_source',
        '.tree.sha | test("^[0-9a-f]{40}$")',
        'sha256sum supply-set.zip',
        '"${artifact_digest#sha256:}"',
        'unzip -Z1 supply-set.zip',
        '.immutable_lookup_tag == ("surface-v1-" + .surface_fingerprint)',
    ):
        assert proof in manifest_code
    assert dispatch_code.count("gh workflow run") == 1
    assert dispatch_code.count(
        'dispatch_args+=(-f "supply_evidence_b64=$SUPPLY_EVIDENCE_B64")'
    ) == 1
    assert '-f "expected_image_digest=' not in dispatch_code
    assert '-f "component_producer_source_revision=' not in dispatch_code
    assert '-f "supply_set_artifact_id=' not in dispatch_code
    assert 'if [ "$SUPPLY_SCHEMA" != "leaf.staging-supply-set.v1" ]' in dispatch_code
    assert 'V2/v3 dispatch is missing its closed supply evidence envelope' in dispatch_code


def test_digest_aware_producer_is_source_controlled_and_dormant() -> None:
    parsed = _strict_yaml(WORKFLOW.read_text(encoding="utf-8"))
    assert parsed["env"]["DIGEST_AWARE_CONVERGENCE_ENABLED"] == "false"
    build = parsed["jobs"]["build"]
    assert build["permissions"]["attestations"] == "write"
    surface = next(
        step for step in build["steps"]
        if step.get("name") == "Resolve one signed reusable surface"
    )
    code = _executable_bash(surface["run"])
    dormant = code.index(
        '[ "$DIGEST_AWARE_CONVERGENCE_ENABLED" != "true" ]'
    )
    fingerprint = code.index("surface-fingerprint")
    first_registry_read = code.index("aws ecr batch-get-image")
    assert fingerprint < dormant < first_registry_read
    assert surface["env"]["FORCE_REBUILD_ALL"] == "${{ inputs.force_rebuild_all }}"
    forced = code.index('[ "${FORCE_REBUILD_ALL:-false}" = "true" ]')
    assert dormant < forced < first_registry_read
    assert "surface-v1-$fingerprint" in code
    assert "gh attestation verify" in code
    assert '--predicate-type "$SURFACE_PREDICATE_TYPE"' in code
    assert 'compare/$producer...$SOURCE_SHA' in code
    assert 'git fetch --no-tags --depth=1 origin "$producer"' in code
    assert "verify-surface-predicate" in code
    assert 'git rev-parse "$producer^{tree}"' in code
    assert 'git rev-parse "$producer:.github/workflows/build-platform-images.yml"' in code
    assert 'imageTag=sha-$producer' in code
    adopt = parsed["jobs"]["adopt"]
    decide = next(step for step in adopt["steps"] if step.get("id") == "decide")
    decide_code = _executable_bash(decide["run"])
    guard = decide_code.index(
        '[ "$DIGEST_AWARE_CONVERGENCE_ENABLED" = "true" ]'
    )
    first_adoption_read = decide_code.index('gh api "repos/$GITHUB_REPOSITORY"')
    assert guard < first_adoption_read
    assert "finish false" in decide_code[guard:first_adoption_read]


def test_digest_aware_build_attests_each_built_digest_and_never_restamps_reuse() -> None:
    parsed = _strict_yaml(WORKFLOW.read_text(encoding="utf-8"))
    steps = parsed["jobs"]["build"]["steps"]
    image = next(step for step in steps if step.get("id") == "build-image")
    assert "steps.surface.outputs.reuse != 'true'" in image["if"]
    assert any("surface-v1-" in tag for tag in image["with"]["tags"].splitlines())
    attestation = next(
        step for step in steps if step.get("name") == "Sign exact surface provenance"
    )
    assert attestation["uses"] == "actions/attest@v4"
    assert attestation["with"]["subject-digest"] == (
        "${{ steps.build-image.outputs.digest }}"
    )
    assert attestation["with"]["push-to-registry"] is True
    result = next(
        step for step in steps
        if step.get("name") == "Materialize one exact v3 service entry"
    )
    assert "if" not in result
    code = _executable_bash(result["run"])
    assert "if [ \"$REUSED\" = \"true\" ]" in code
    assert "disposition=reused" in code
    assert "disposition=built" in code
    assert "producer_source_revision" in code
    assert "release_source_revision" not in code


def test_digest_aware_fan_in_requires_all_five_exact_service_entries() -> None:
    parsed = _strict_yaml(WORKFLOW.read_text(encoding="utf-8"))
    verify = parsed["jobs"]["verify"]
    writer = next(
        step for step in verify["steps"]
        if step.get("name") == "Write the immutable five-service staging supply set"
    )
    code = _executable_bash(writer["run"])
    assert "for image in app broker canonical-worker harness web" in code
    assert 'if [ "${#matches[@]}" != "1" ]' in code
    assert code.count("--service-entry") == 5
    assert "generate-v3" in code
    assert "docker buildx imagetools inspect --raw" in code
    assert '"$ECR_REGISTRY/$repository@$digest"' in code
    assert "leaf.staging-supply-set.v1" not in code
    assert "platform_release_manifest.py generate \\" not in code


def test_v3_digest_existence_probe_accepts_oci_index_and_fails_closed() -> None:
    """Run the shipped digest probe against the exact OCI-index response shape.

    Build 31758917297 pushed an immutable Buildx digest and then proved the
    false-negative counterexample: BatchGetImage returned no leaf manifest for
    that digest. An authenticated registry inspect is the correct existence
    boundary because it accepts the index media type and addresses it by the
    exact immutable digest.
    """
    parsed = _strict_yaml(WORKFLOW.read_text(encoding="utf-8"))
    writer = next(
        step for step in parsed["jobs"]["verify"]["steps"]
        if step.get("name") == "Write the immutable five-service staging supply set"
    )
    shipped = writer["run"]
    match = re.search(
        r"# BEGIN V3_DIGEST_EXISTENCE_PROBE\n(?P<body>.*?)"
        r"\n\s*# END V3_DIGEST_EXISTENCE_PROBE",
        shipped,
        re.S,
    )
    assert match, "the executable immutable-digest probe must remain extractable"
    probe = "set -euo pipefail\n" + match.group("body") + "\n"
    assert "docker buildx imagetools inspect --raw" in probe
    assert "aws ecr batch-get-image" not in probe
    assert "aws ecr describe-images" not in probe

    bash = shutil.which("bash")
    assert bash, "the immutable-digest rehearsal needs bash on PATH"
    expected = "sha256:" + "b" * 64
    with tempfile.TemporaryDirectory() as tmp_name:
        tmp = Path(tmp_name)
        bindir = tmp / "bin"
        bindir.mkdir()
        docker = bindir / "docker"
        docker.write_text(
            "#!/bin/sh\n"
            "[ \"$1 $2 $3 $4\" = \"buildx imagetools inspect --raw\" ] || exit 2\n"
            "[ \"$5\" = \"$EXPECTED_ECR_REF\" ] || exit 1\n"
            "printf '%s\\n' "
            "'{\"mediaType\":\"application/vnd.oci.image.index.v1+json\"}'\n",
            encoding="utf-8",
            newline="\n",
        )
        docker.chmod(0o755)
        script = tmp / "probe.sh"
        script.write_text(probe, encoding="utf-8", newline="\n")

        def run(candidate_digest: str) -> subprocess.CompletedProcess[str]:
            env = dict(os.environ)
            env["PATH"] = str(bindir) + os.pathsep + env.get("PATH", "")
            env["ECR_REGISTRY"] = "807034087062.dkr.ecr.us-east-1.amazonaws.com"
            env["repository"] = "leaf-platform-app"
            env["digest"] = candidate_digest
            env["image"] = "app"
            env["EXPECTED_ECR_REF"] = (
                f"{env['ECR_REGISTRY']}/{env['repository']}@{expected}"
            )
            return subprocess.run(
                [bash, str(script)],
                text=True,
                capture_output=True,
                env=env,
            )

        assert run(expected).returncode == 0
        altered = "sha256:" + "a" * 64
        result = run(altered)
        assert result.returncode != 0


def test_selector_off_full_build_always_mints_v3_without_enabling_reuse() -> None:
    parsed = _strict_yaml(WORKFLOW.read_text(encoding="utf-8"))
    assert parsed["env"]["DIGEST_AWARE_CONVERGENCE_ENABLED"] == "false"
    build_steps = parsed["jobs"]["build"]["steps"]
    surface = next(step for step in build_steps if step.get("id") == "surface")
    surface_code = _executable_bash(surface["run"])
    fingerprint = surface_code.index("surface-fingerprint")
    dormant = surface_code.index(
        '[ "$DIGEST_AWARE_CONVERGENCE_ENABLED" != "true" ]'
    )
    registry_read = surface_code.index("aws ecr batch-get-image")
    assert fingerprint < dormant < registry_read
    build_image = next(step for step in build_steps if step.get("id") == "build-image")
    assert "steps.surface.outputs.reuse != 'true'" in build_image["if"]
    surface_tag = next(
        tag for tag in build_image["with"]["tags"].splitlines()
        if "surface-v1-" in tag
    )
    assert "env.DIGEST_AWARE_CONVERGENCE_ENABLED == 'true'" in surface_tag
    predicate = next(
        step for step in build_steps
        if step.get("name") == "Create exact surface provenance predicate"
    )
    assert predicate["if"] == "steps.surface.outputs.reuse != 'true'"
    attestation = next(
        step for step in build_steps if step.get("name") == "Sign exact surface provenance"
    )
    assert attestation["if"] == (
        "env.DIGEST_AWARE_CONVERGENCE_ENABLED == 'true' && "
        "steps.surface.outputs.reuse != 'true'"
    )
    materialize = next(
        step for step in build_steps
        if step.get("name") == "Materialize one exact v3 service entry"
    )
    upload = next(
        step for step in build_steps
        if step.get("name") == "Upload exact v3 service entry"
    )
    assert "if" not in materialize
    assert "if" not in upload
    assert materialize["env"]["LOOKUP_TAG"] == "${{ steps.surface.outputs.lookup_tag }}"
    verify_steps = parsed["jobs"]["verify"]["steps"]
    for name in (
        "Download exact v3 service entries",
        "Download exact v3 web deployment artifact",
    ):
        step = next(candidate for candidate in verify_steps if candidate.get("name") == name)
        assert "if" not in step
    writer = next(
        step for step in verify_steps
        if step.get("name") == "Write the immutable five-service staging supply set"
    )
    assert writer["run"].count("--service-entry") == 5
    assert "generate-v3" in writer["run"]
    assert "leaf.staging-supply-set.v1" not in writer["run"]


def test_digest_aware_relay_requires_consumer_marker_and_exact_surface_receipts() -> None:
    relay_path = WORKFLOW.parent / "dispatch-staging-deploys.yml"
    parsed = _strict_yaml(relay_path.read_text(encoding="utf-8"))
    job = parsed["jobs"]["dispatch"]
    assert job["env"]["DIGEST_AWARE_CONVERGENCE_ENABLED"] == "true"
    build = _strict_yaml(WORKFLOW.read_text(encoding="utf-8"))
    assert build["env"]["DIGEST_AWARE_CONVERGENCE_ENABLED"] == "false"
    assert job["env"]["DIGEST_AWARE_CONSUMER_MARKER"] == (
        "leaf.staging-digest-aware-consumer.v1"
    )
    assert job["env"]["CONSUMER_CONTRACT_WORKFLOW"] == (
        "publish-leaf-platform-staging-consumer-contract.yml"
    )
    contract_step = next(
        step for step in job["steps"]
        if step.get("name") == "Read the provider-associated Terraform consumer contract"
    )
    assert contract_step["env"]["GH_TOKEN"] == "${{ secrets.TERRAFORM_REPO_TOKEN }}"
    contract_code = _executable_bash(contract_step["run"])
    assert "actions/workflows/$CONSUMER_CONTRACT_WORKFLOW/runs" in contract_code
    assert "actions/runs/$RUN_ID/artifacts" in contract_code
    assert "actions/artifacts/$ARTIFACT_ID/zip" in contract_code
    assert "contents/" not in contract_code
    assert "branches/main" not in contract_code
    assert 'steps.manifest.outputs.schema == \'leaf.staging-supply-set.v3\'' in _folded(
        contract_step["if"]
    )
    deploy_step = _relay_deploy_step(job)
    assert deploy_step["env"]["CONSUMER_CONTRACT_B64"] == (
        "${{ steps.consumer_contract.outputs.consumer_contract_b64 }}"
    )
    assert deploy_step["env"]["TF_CONTRACT_HEAD"] == (
        "${{ steps.consumer_contract.outputs.terraform_head_sha }}"
    )
    assert deploy_step["env"]["TF_CONSUMER_BLOB"] == (
        "${{ steps.consumer_contract.outputs.deploy_workflow_blob }}"
    )
    code = _executable_bash(deploy_step["run"])
    assert 'repos/$INFRA_REPO/contents/' not in code
    assert 'repos/$INFRA_REPO/branches/main' not in code
    assert '-f "digest_aware_reconcile=true"' in code
    assert code.count(
        'dispatch_args+=(-f "supply_evidence_b64=$SUPPLY_EVIDENCE_B64")'
    ) == 1
    assert '-f "convergence_id=$CONVERGENCE_ID"' in code
    assert '-f "consumer_contract_b64=$CONSUMER_CONTRACT_B64"' in code
    assert 'repos/$INFRA_REPO/compare/' not in code
    assert 'actions/runs/$latest/artifacts?per_page=100' in code
    assert 'actions/artifacts/$artifact_id/zip' in code
    assert "consumer semantics changed" in code
    assert "producer workflow changed" in code
    assert "payload digest" in code
    for removed_field in (
        "expected_image_digest=",
        "component_producer_source_revision=",
        "component_producer_source_tree=",
        "surface_fingerprint=",
        "recipe_fingerprint=",
        "producer_workflow_path=",
        "producer_workflow_blob=",
        "producer_run_id=",
        "producer_run_attempt=",
        "provenance_subject=",
        "provenance_digest=",
        "release_source_revision=",
        "release_source_tree=",
        "supply_set_artifact_id=",
        "supply_set_artifact_name=",
        "supply_set_sha256=",
    ):
        assert f'-f "{removed_field}' not in code
    assert 'OUTCOME=$(jq -er \'.outcome\'' in code
    assert 'if [ "$OUTCOME" = "deployed" ]' in code
    assert "DEPLOYED_ANY=true" in code
    assert 'schema: "leaf.staging-converged.v2"' in code
    assert "candidate_supply_set: $supply[0]" in code
    assert 'full_fleet_identity_stamped: false' in code
    assert 'harness: "not_automatically_reconciled"' in code


def test_relay_accepts_only_byte_equivalent_newer_consumer_contract() -> None:
    relay = _strict_yaml(
        (WORKFLOW.parent / "dispatch-staging-deploys.yml").read_text(
            encoding="utf-8"
        )
    )
    code = _executable_bash(_relay_deploy_step(relay["jobs"]["dispatch"])["run"])
    match = re.search(
        r"python3 - <<'CONTRACT_SUCCESSOR_PY'\n(.*?)\n\s*CONTRACT_SUCCESSOR_PY",
        code,
        re.S,
    )
    assert match, "newer consumer-contract validator heredoc missing"
    validator = textwrap.dedent(match.group(1))
    consumer = {
        "contract_schema_path": "contract/leaf-platform-staging-consumer-contract.v1.schema.json",
        "contract_schema_blob": "b" * 40,
        "contract_version": 1,
        "deploy_workflow_path": ".github/workflows/deploy-leaf-platform-staging.yml",
        "deploy_workflow_blob": "c" * 40,
        "pins": {
            "deployment_environment": "aws-apply",
            "digest_aware_marker": "leaf.staging-digest-aware-consumer.v1",
            "mutation_group": "leaf-platform-staging-ecs-mutation",
        },
    }
    workflow_blob = "d" * 40
    bound = {
        "contract": {
            "consumer": consumer,
            "producer": {"workflow_blob": workflow_blob},
        }
    }
    encoded = base64.urlsafe_b64encode(_canonical_json(bound)).rstrip(b"=").decode()
    run_id = 31940000001
    attempt = 1
    head = "e" * 40

    def contract() -> dict:
        value = {
            "artifact": {"file": "consumer-contract.json", "name": "contract"},
            "consumer": json.loads(json.dumps(consumer)),
            "producer": {
                "repository": "LEAF-Solar-Design/leaf-automation-aws-terraform",
                "workflow_path": ".github/workflows/publish-leaf-platform-staging-consumer-contract.yml",
                "workflow_blob": workflow_blob,
                "run_id": run_id,
                "run_attempt": attempt,
                "event": "push",
                "branch": "main",
                "head_sha": head,
                "head_tree": "f" * 40,
            },
            "schema": "leaf.platform-staging-consumer-contract.v1",
            "version": 1,
        }
        value["payload_sha256"] = hashlib.sha256(_canonical_json(value)).hexdigest()
        return value

    def accepted(value: dict) -> bool:
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "newer-consumer-contract.json").write_bytes(
                _canonical_json(value)
            )
            env = os.environ.copy()
            env.update({
                "CONSUMER_CONTRACT_B64": encoded,
                "LATEST_CONTRACT_RUN_ID": str(run_id),
                "LATEST_CONTRACT_RUN_ATTEMPT": str(attempt),
                "LATEST_CONTRACT_HEAD": head,
            })
            return subprocess.run(
                [sys.executable, "-c", validator],
                cwd=tmp,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            ).returncode == 0

    assert accepted(contract())
    for mutate in (
        lambda value: value["consumer"].update(deploy_workflow_blob="0" * 40),
        lambda value: value["producer"].update(workflow_blob="0" * 40),
        lambda value: value["producer"].update(head_sha="0" * 40),
        lambda value: value.update(payload_sha256="0" * 64),
    ):
        rejected = contract()
        mutate(rejected)
        assert not accepted(rejected)


def _consumer_contract_validator_python() -> str:
    relay = _strict_yaml(
        (WORKFLOW.parent / "dispatch-staging-deploys.yml").read_text(
            encoding="utf-8"
        )
    )
    step = next(
        item for item in relay["jobs"]["dispatch"]["steps"]
        if item.get("name")
        == "Read the provider-associated Terraform consumer contract"
    )
    match = re.search(r"python3 - <<'PY'\n(.*?)\n\s*PY", step["run"], re.S)
    assert match, "consumer contract validator heredoc missing"
    return textwrap.dedent(match.group(1))


def _canonical_json(value) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")


def _run_consumer_contract_fixture(
    *, run_change=None, contract_change=None, artifact_change=None, duplicate=False
):
    run_id = 31770000001
    attempt = 2
    head = "a" * 40
    name = f"leaf-platform-staging-consumer-contract-run-{run_id}-attempt-{attempt}"
    contract = {
        "artifact": {"file": "consumer-contract.json", "name": name},
        "consumer": {
            "contract_schema_path": "contract/leaf-platform-staging-consumer-contract.v1.schema.json",
            "contract_schema_blob": "b" * 40,
            "contract_version": 1,
            "deploy_workflow_path": ".github/workflows/deploy-leaf-platform-staging.yml",
            "deploy_workflow_blob": "c" * 40,
            "pins": {
                "deployment_environment": "aws-apply",
                "digest_aware_marker": "leaf.staging-digest-aware-consumer.v1",
                "mutation_group": "leaf-platform-staging-ecs-mutation",
            },
        },
        "producer": {
            "repository": "LEAF-Solar-Design/leaf-automation-aws-terraform",
            "workflow_path": ".github/workflows/publish-leaf-platform-staging-consumer-contract.yml",
            "workflow_blob": "d" * 40,
            "run_id": run_id,
            "run_attempt": attempt,
            "event": "push",
            "branch": "main",
            "head_sha": head,
            "head_tree": "e" * 40,
        },
        "schema": "leaf.platform-staging-consumer-contract.v1",
        "version": 1,
    }
    if contract_change:
        contract_change(contract)
    unsigned = dict(contract)
    unsigned.pop("payload_sha256", None)
    contract["payload_sha256"] = hashlib.sha256(_canonical_json(unsigned)).hexdigest()
    raw_contract = _canonical_json(contract) + b"\n"

    run = {
        "id": run_id,
        "run_attempt": attempt,
        "repository": {"full_name": "LEAF-Solar-Design/leaf-automation-aws-terraform"},
        "path": ".github/workflows/publish-leaf-platform-staging-consumer-contract.yml",
        "event": "push",
        "head_branch": "main",
        "head_sha": head,
        "status": "completed",
        "conclusion": "success",
    }
    if run_change:
        run_change(run)

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        archive_path = root / "consumer-contract.zip"
        with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as bundle:
            bundle.writestr("consumer-contract.json", raw_contract)
            if duplicate:
                bundle.writestr("foreign.json", b"{}")
        archive_sha = hashlib.sha256(archive_path.read_bytes()).hexdigest()
        artifact = {
            "archive_download_url": "https://api.github.com/artifacts/9207000001/zip",
            "created_at": "2026-08-14T00:00:00Z",
            "digest": f"sha256:{archive_sha}",
            "expired": False,
            "expires_at": "2026-09-13T00:00:00Z",
            "id": 9207000001,
            "name": name,
            "node_id": "artifact-node",
            "size_in_bytes": len(archive_path.read_bytes()),
            "updated_at": "2026-08-14T00:00:01Z",
            "url": "https://api.github.com/artifacts/9207000001",
            "workflow_run": {
                "head_branch": "main",
                "head_repository_id": 1,
                "head_sha": head,
                "id": run_id,
                "repository_id": 2,
            },
        }
        if artifact_change:
            artifact_change(artifact)
        (root / "consumer-contract-run.json").write_text(
            json.dumps(run), encoding="utf-8"
        )
        (root / "consumer-contract-artifact.json").write_text(
            json.dumps(artifact), encoding="utf-8"
        )
        output = root / "github-output"
        completed = subprocess.run(
            [sys.executable, "-c", _consumer_contract_validator_python()],
            cwd=root,
            env={**os.environ, "GITHUB_OUTPUT": str(output)},
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        return completed.returncode, completed.stdout, (
            output.read_text(encoding="utf-8") if output.exists() else ""
        )


def test_provider_consumer_contract_validator_accepts_real_shape() -> None:
    rc, output, github_output = _run_consumer_contract_fixture()
    assert rc == 0, output
    assert "Validated one provider-associated Terraform consumer contract" in output
    assert "consumer_contract_b64=" in github_output
    assert "terraform_head_sha=" + "a" * 40 in github_output
    assert "deploy_workflow_blob=" + "c" * 40 in github_output
    encoded = next(
        line.split("=", 1)[1]
        for line in github_output.splitlines()
        if line.startswith("consumer_contract_b64=")
    )
    assert re.fullmatch(r"[A-Za-z0-9_-]+", encoded)
    assert "=" not in encoded
    envelope = json.loads(
        base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
    )
    assert envelope["schema"] == (
        "leaf.platform-staging-consumer-contract-dispatch.v1"
    )


def test_provider_consumer_contract_validator_fails_closed_on_rebinding() -> None:
    cases = (
        {"run_change": lambda run: run.update(path=".github/workflows/foreign.yml")},
        {"run_change": lambda run: run.update(conclusion="failure")},
        {"run_change": lambda run: run.update(head_sha="f" * 40)},
        {"artifact_change": lambda artifact: artifact.update(digest="sha256:" + "f" * 64)},
        {"artifact_change": lambda artifact: artifact.update(expired=True)},
        {"artifact_change": lambda artifact: artifact.update(name="foreign")},
        {"contract_change": lambda contract: contract.update(authority_digest="f" * 64)},
        {"contract_change": lambda contract: contract["consumer"]["pins"].update(digest_aware_marker="foreign")},
        {"duplicate": True},
    )
    for case in cases:
        rc, output, github_output = _run_consumer_contract_fixture(**case)
        assert rc != 0, (case, output)
        assert github_output == "", (case, github_output)


def test_digest_aware_relay_enables_only_v3_and_keeps_v1_v2_compatibility() -> None:
    relay = (WORKFLOW.parent / "dispatch-staging-deploys.yml").read_text(
        encoding="utf-8"
    )
    executable = _executable_bash(relay)
    assert "leaf.staging-supply-set.v1|leaf.staging-supply-set.v2" in executable
    assert "leaf.staging-supply-set.v3" in executable
    assert 'if [ "$SUPPLY_SCHEMA" = "leaf.staging-supply-set.v3" ]' in executable
    assert executable.count('-f "digest_aware_reconcile=true"') == 1
    assert executable.count(
        'dispatch_args+=(-f "supply_evidence_b64=$SUPPLY_EVIDENCE_B64")'
    ) == 1
    assert (
        "V3 supply set arrived while its source-controlled consumer handshake is dormant"
        in executable
    )
    assert 'schema: "leaf.staging-converged.v1"' in executable


if __name__ == "__main__":
    main()
