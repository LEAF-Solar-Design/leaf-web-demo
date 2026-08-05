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
    assert text.count("id-token: write") == 3

    # The private key is scoped to the canonical-worker lane. It is used only
    # for a fail-closed presence check and the one exact solver checkout. It is
    # never passed to Docker as an argument or inherited by the whole job.
    secret_ref = "secrets.AUTOFILL_SOLVER_DEPLOY_KEY"
    assert text.count(secret_ref) == 2
    require_start = text.index(
        "      - name: Require the read-only canonical solver deploy key"
    )
    checkout_start = text.index(
        "      - name: Check out the exact canonical solver source"
    )
    buildx_start = text.index("      - name: Set up Docker Buildx", checkout_start)
    require_body = text[require_start:checkout_start]
    checkout_body = text[checkout_start:buildx_start]
    assert "if: matrix.image == 'canonical-worker'" in require_body
    assert "if: matrix.image == 'canonical-worker'" in checkout_body
    assert "persist-credentials: false" in checkout_body
    assert text.count("ssh-key: ${{ secrets.AUTOFILL_SOLVER_DEPLOY_KEY }}") == 1
    assert "AUTOFILL_SOLVER_DEPLOY_KEY=" not in text
    assert "AUTOFILL_SOLVER_DEPLOY_KEY:" not in text[:require_start]
    assert "AUTOFILL_SOLVER_DEPLOY_KEY" not in text[buildx_start:]

    # An untested image can never reach ECR: the build job waits on the full
    # gate, run against the exact commit `prepare` resolved. Branch protection
    # is unavailable on this repository's plan, so this workflow-internal
    # dependency is the only enforceable gate and must not be loosened.
    assert "uses: ./.github/workflows/test-gate.yml" in text
    assert "needs: [prepare, test]" in text

    # The matrix isolates all five images and does not cancel siblings after
    # one failure. A failed matrix entry still blocks the verification job.
    assert re.search(r"image:\s*\[app, broker, canonical-worker, harness, web\]", text)
    assert "fail-fast: false" in text
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
    warm_block = text.split("\n  warm:\n", 1)[1].split("\n  build:\n", 1)[0]
    build_block = text.split("\n  build:\n", 1)[1].split("\n  verify:\n", 1)[0]

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
            raise AssertionError("the warm build step carries no with: mapping")
        body = []
        for line in lines[start + 1:]:
            if line.strip() and not line.startswith("          "):
                break
            body.append(line)
        return "\n".join(body)

    warm_with = _with_mapping(warm_build_step)
    assert re.search(r"^          push: false$", warm_with, re.M), (
        "the warm build step's with: mapping must set the literal input push: false")
    for banned in ("push: true", "tags:", "outputs:", "provenance:", "sbom:", "attests:"):
        assert banned not in warm_build_step, (
            "the warm build step must not carry %r: every publication channel "
            "of build-push-action stays closed in the warm lane" % banned)
    # And no publish path outside that step either.
    assert "docker push" not in warm_block
    assert "aws ecr put-image" not in warm_block
    assert "push: true" not in warm_block
    assert "tags:" not in warm_block, "a cache warmer publishes no image tag"

    assert "needs: [prepare, test]" in build_block, (
        "the gate edge is this repository's only enforcement that an untested "
        "image never reaches ECR")
    assert "push: true" in build_block

    # The warmed cache is what makes the post-gate build cheap; without this
    # preference the warm job burns five runners and saves nothing.
    assert "current_warm" in build_block
    assert 'cache_from="type=registry,ref=$cache_repo:$CURRENT_CACHE_TAG"' in build_block

    # Both writers race on the same immutable tag by design, and ECR refuses
    # the loser with ImageTagAlreadyExistsException. The registry cache
    # exporter must therefore tolerate export errors in BOTH jobs, or a lost
    # race fails the gated build.
    assert warm_block.count("ignore-error=true") == 1
    assert build_block.count("ignore-error=true") == 1

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
    bind_at = build_block.index("Require the green gate verdict to bind")
    push_at = build_block.index("uses: docker/build-push-action")
    assert bind_at < push_at, "the tree binding must precede the push step"

    check_docs_noop_filter(text)

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
    wf = yaml.safe_load(text)
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
        ("dispatch-staging-deploys.yml", yaml.safe_load(relay_text)),
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
    relay_wf = yaml.safe_load(relay_text)
    # Workflow shape is frozen top to bottom (PyYAML reads the bare `on`
    # key as True). A new trigger, job, or permission grant must land here.
    assert set(relay_wf) == {"name", True, "permissions", "concurrency", "jobs"}
    # CAPABILITY WALL: dispatching the infra repo requires the PAT, and the
    # workflow's own token is pinned read-only, so an assembled command in
    # an unguarded script has nothing to dispatch WITH. The token-denial
    # scan below is only a tripwire on top of this.
    assert relay_wf["permissions"] == {"actions": "read", "contents": "read"}
    assert set(relay_wf["jobs"]) == {"dispatch"}
    dispatch_job = relay_wf["jobs"]["dispatch"]
    assert set(dispatch_job) == {"if", "runs-on", "timeout-minutes", "env", "steps"}
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

    # The UNGUARDED scripts are content-frozen: any edit to their
    # executable text (comments excluded) must update this hash in the
    # same PR, so the change is visible to review — an assembled dispatch
    # smuggled into tip or manifest cannot land silently. The guarded
    # dispatch script stays property-checked (its exact guard above is
    # the contract), and the capability wall means the frozen scripts
    # hold no credential able to dispatch even if edited.
    frozen = hashlib.sha256(
        (tip_code + "\n===\n" + manifest_code).encode("utf-8")
    ).hexdigest()
    assert frozen == (
        "ce04a9766bfdf5af80a33fc61c7002fbaa35f735e55ecf6816d0fd5f99e88872"
    ), (
        "tip/manifest relay scripts changed: review the diff for dispatch "
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
