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
    assert "pull-requests: read" in text
    assert "source_sha must be a full 40-character lowercase hexadecimal commit" in text
    assert '.state == "open"' in text
    assert ".draft == true" in text
    assert '.base.ref == "main"' in text
    assert "(.head.sha | ascii_downcase) == $sha" in text
    assert "Build source must be on main or the exact head of an open draft PR" in text
    assert "Only a source commit on main may request production promotion" in text
    assert "needs.prepare.outputs.source_mode == 'main'" in text
    assert text.count("id-token: write") == 2

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
    assert text.count("if: ${{ !inputs.promote }}") == 3
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

    print("build-platform-images workflow invariants: PASS")


def test_build_platform_images_workflow_invariants() -> None:
    # Pytest entry point: the gate runner counts collected tests, and a bare
    # main() collects as zero.
    main()


if __name__ == "__main__":
    main()
