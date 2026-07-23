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


def main() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")

    # One tag is derived once and passed to every image build and later job.
    assert 'value=prod-$(git rev-parse --short HEAD)' in text
    assert "IMAGE_TAG: ${{ needs.prepare.outputs.tag }}" in text
    assert "TAG: ${{ needs.prepare.outputs.tag }}" in text
    assert (
        "tags: ${{ env.ECR_REGISTRY }}/${{ env.IMAGE_NAME }}:"
        "${{ env.IMAGE_TAG }}"
    ) in text

    # The matrix isolates all three images and does not cancel siblings after
    # one failure. A failed matrix entry still blocks the verification job.
    assert re.search(r"image:\s*\[app, broker, harness\]", text)
    assert "fail-fast: false" in text
    assert "needs: [prepare, build]" in text

    # Each matrix entry reads and publishes only its own ECR cache tag.
    cache_ref = (
        "${{ env.ECR_REGISTRY }}/${{ env.IMAGE_NAME }}:buildcache"
    )
    assert f"cache-from: type=registry,ref={cache_ref}" in text
    assert (
        f"cache-to: type=registry,ref={cache_ref},mode=max,"
        "image-manifest=true,oci-mediatypes=true"
    ) in text
    assert text.count(":buildcache") == 2

    # Promotion depends on the final all-three ECR existence check, not merely
    # on one successful image push.
    assert re.search(r"promote:\s*\n\s+needs: \[prepare, verify\]", text)
    verify_start = text.index("  verify:")
    verify_body = text[verify_start : text.index("  promote:", verify_start)]
    assert "for image in app broker harness; do" in verify_body
    assert "aws ecr batch-get-image" in verify_body
    assert '--image-ids "imageTag=$TAG"' in verify_body
    assert 'if [[ -z "$digest" || "$digest" == "None" ]]' in verify_body

    print("build-platform-images workflow invariants: PASS")


if __name__ == "__main__":
    main()
