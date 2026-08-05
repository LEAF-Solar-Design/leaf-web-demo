#!/usr/bin/env python3
"""Static regression checks for the instant execution staging image workflow."""

from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "build-instant-execution-image.yml"


def main() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")

    assert "ref: ${{ inputs.source_sha || github.sha }}" in text
    assert "source_sha: ${{ steps.source.outputs.sha }}" in text
    assert "ref: ${{ needs.prepare.outputs.source_sha }}" in text
    assert "LEAF_SOURCE_SHA=${{ needs.prepare.outputs.source_sha }}" in text
    assert "source_sha must be a full 40-character lowercase hexadecimal commit" in text
    assert "Checkout did not resolve to source_sha" in text
    assert "GH_TOKEN: ${{ github.token }}" in text
    assert 'https://api.github.com/repos/$GITHUB_REPOSITORY/compare/$current_sha...main' in text
    assert "merge_base_commit.sha" in text
    assert "status" in text
    assert "fetch --no-tags origin main" not in text
    assert "http.https://github.com/.extraheader" not in text

    assert 'image_tag="sha-$current_sha"' in text
    assert "IMAGE_TAG: ${{ needs.prepare.outputs.image_tag }}" in text
    assert ":latest" not in text.lower()
    assert not re.search(r"image_tag=\"(?:prod|release)-", text)
    assert "leaf-platform-instant-execution" in text
    assert "for image in app broker canonical-worker harness web" not in text
    assert "staging-supply-set" not in text

    test_start = text.index("  test:")
    build_start = text.index("  build-scan-push:")
    test_body = text[test_start:build_start]
    build_body = text[build_start:]
    assert "python scripts/test_build_instant_execution_image_workflow.py" in test_body
    assert "python scripts/test_instant_execution_container_contract.py" in test_body
    assert "needs: [prepare, test]" in build_body
    assert build_body.index("Scan image before push") < build_body.index("Push scanned immutable image")
    assert "aquasecurity/trivy-action@v0.36.0" in build_body
    assert "aquasecurity/trivy-action@0." not in build_body
    assert "push: false" in build_body
    assert 'docker push "$IMAGE_URI"' in build_body

    assert "id-token: write" in build_body
    assert "secrets.AWS_INSTANT_EXECUTION_ECR_PUSH_ROLE" in build_body
    assert "AWS_ECR_PUSH_ROLE" not in text
    assert "aws ecs" not in text.lower()
    assert "terraform" not in text.lower()
    assert "workflow run" not in text.lower()

    assert 'current_cache_tag="buildcache-$current_short"' in text
    assert 'previous_cache_tag="buildcache-$previous_short"' in text
    assert '"$previous_cache_tag" == "$current_cache_tag"' in text
    assert "immutable tag will not be overwritten" in text
    assert "expire buildcache-* tags after 14 days" in text

    # The cache probes must surface stderr and fail the job on a nonzero exit
    # (AccessDenied-class) rather than reading a probe failure as an absent tag.
    assert "2>/dev/null" not in build_body
    assert "a probe failure is not an absent tag" in build_body
    assert "refusing to skip cache publication on a failed probe" in build_body
    assert "Unable to verify" not in build_body

    print("instant execution image workflow invariants: PASS")


def test_build_instant_execution_image_workflow_invariants() -> None:
    main()


if __name__ == "__main__":
    main()
