from __future__ import annotations

import json
from pathlib import Path
import sys
import zipfile

import pytest


SCRIPTS = Path(__file__).resolve().parent
ROOT = SCRIPTS.parent
sys.path.insert(0, str(SCRIPTS))

from platform_release_manifest import web_dist_digest  # noqa: E402
from production_web_release import (  # noqa: E402
    PROJECT_ID,
    ReleaseError,
    deployment_receipt,
    extract_artifact,
    prepare,
)


SOURCE = "a" * 40
RELEASE_RUN_ID = "123456"
RELEASE_ATTEMPT = "2"
DIGESTS = {
    name: f"sha256:{index:064x}"
    for index, name in enumerate(
        ("app", "broker", "canonical-worker", "harness", "web"), start=1
    )
}


def _dist(root: Path) -> tuple[Path, str]:
    dist = root / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "assets" / "index-good.js").write_text(
        "console.log('leaf')\n", encoding="utf-8"
    )
    (dist / "index.html").write_text(
        '<script type="module" src="/assets/index-good.js"></script>\n',
        encoding="utf-8",
    )
    (dist / "health.json").write_text(
        json.dumps(
            {
                "ok": True,
                "service": "leaf-platform-web",
                "component": "frontend",
                "source_sha": SOURCE,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (dist / "vercel.json").write_text(
        json.dumps({"rewrites": [{"source": "/:path*", "destination": "/index.html"}]})
        + "\n",
        encoding="utf-8",
    )
    return dist, web_dist_digest(dist)


def _approval(digest: str) -> dict:
    return {
        "schema": "leaf.production-web-approval.v1",
        "project_id": PROJECT_ID,
        "source_revision": SOURCE,
        "release_workflow_run_id": 123456,
        "release_workflow_run_attempt": 2,
        "handoff_workflow_run_id": 654322,
        "handoff_workflow_run_attempt": 4,
        "web_artifact_sha256": digest,
        "workflow_head_sha": "f" * 40,
        "deployment_workflow_run_id": 765432,
        "deployment_workflow_run_attempt": 5,
        "issue_number": 42,
        "comment_id": 987654,
        "approver_login": "qualified-reviewer",
        "approver_id": 12345,
        "permission": "write",
        "created_at": "2026-07-28T12:00:00Z",
        "validated_at": "2026-07-28T12:05:00Z",
        "approval_payload_sha256": "e" * 64,
        "exact_body_verified": True,
        "author_separated": True,
        "timely_at_promotion": True,
    }


def _handoff(web_hash: str) -> dict:
    services = {
        name: {
            "repository": f"leaf-platform-{name}",
            "image_digest": DIGESTS[name],
            "source_revision": SOURCE,
        }
        for name in DIGESTS
    }
    services["web"]["artifact_sha256"] = web_hash
    services["canonical-worker"]["provenance"] = {
        "application_source_revision": SOURCE,
        "solver_source_revision": "b" * 40,
        "solver_source_sha256": "c" * 64,
    }
    return {
        "schema": "leaf.production-handoff-candidate.v1",
        "source_revision": SOURCE,
        "staging_supply_set_manifest_sha256": "d" * 64,
        "release": {
            "workflow_run_id": int(RELEASE_RUN_ID),
            "workflow_run_attempt": int(RELEASE_ATTEMPT),
            "workflow_id": 99,
            "workflow_path": ".github/workflows/build-platform-images.yml",
            "event": "push",
            "head_branch": "main",
            "head_sha": SOURCE,
        },
        "staging_acceptance": {
            "run_id": "accept-1",
            "workflow_run_id": 654321,
            "workflow_run_attempt": 3,
            "workflow_id": 88,
            "workflow_path": ".github/workflows/accept-leaf-platform-staging-authored-cad.yml",
            "event": "workflow_dispatch",
            "head_branch": "main",
            "head_sha": "e" * 40,
            "source_revision": SOURCE,
            "images": DIGESTS,
        },
        "staging_supply_set_services": services,
        "proof": {
            "source_is_ancestor_of_main": True,
            "staging_digests_equal_release": True,
        },
    }


def _inspect(deployment_id: str, url: str) -> dict:
    return {
        "id": deployment_id,
        "name": "leaf-platform-web",
        "url": url,
        "target": "production",
        "readyState": "READY",
        "projectId": PROJECT_ID,
    }


def test_prepare_reuses_exact_web_bytes_and_builds_static_output(tmp_path: Path):
    dist, digest = _dist(tmp_path)
    output = tmp_path / ".vercel" / "output"
    proof = prepare(
        _handoff(digest),
        dist,
        output,
        source=SOURCE,
        release_run_id=RELEASE_RUN_ID,
        release_attempt=RELEASE_ATTEMPT,
        expected_web_sha256=digest,
    )

    assert proof["web_artifact_sha256"] == digest
    assert proof["entry_asset"] == "assets/index-good.js"
    assert proof["build_performed"] is False
    assert (output / "static" / "index.html").read_bytes() == (
        dist / "index.html"
    ).read_bytes()
    assert not (output / "static" / "vercel.json").exists()
    config = json.loads((output / "config.json").read_text(encoding="utf-8"))
    assert config["version"] == 3
    assert config["routes"][1]["src"] == "/api(?:/.*)?"
    assert config["routes"][1]["status"] == 404
    assert config["routes"][-1] == {"src": "/.*", "dest": "/index.html"}


@pytest.mark.parametrize("failure", ["hash", "source", "release", "acceptance"])
def test_prepare_rejects_unbound_or_mixed_evidence(tmp_path: Path, failure: str):
    dist, digest = _dist(tmp_path)
    handoff = _handoff(digest)
    expected = digest
    if failure == "hash":
        expected = "f" * 64
    elif failure == "source":
        handoff["staging_supply_set_services"]["app"]["source_revision"] = "f" * 40
    elif failure == "release":
        handoff["release"]["workflow_run_attempt"] = 3
    else:
        handoff["staging_acceptance"]["images"]["web"] = "sha256:" + "f" * 64

    with pytest.raises(ReleaseError):
        prepare(
            handoff,
            dist,
            tmp_path / "output",
            source=SOURCE,
            release_run_id=RELEASE_RUN_ID,
            release_attempt=RELEASE_ATTEMPT,
            expected_web_sha256=expected,
        )


def test_extract_rejects_path_traversal_and_preserves_destination_absence(
    tmp_path: Path,
):
    archive = tmp_path / "bad.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("../escape.txt", "bad")
    destination = tmp_path / "extract"
    with pytest.raises(ReleaseError):
        extract_artifact(archive, destination)
    assert not (tmp_path / "escape.txt").exists()


def test_receipt_binds_new_stable_deployment_and_all_workflow_attempts(tmp_path: Path):
    dist, digest = _dist(tmp_path)
    prepared = prepare(
        _handoff(digest),
        dist,
        tmp_path / "output",
        source=SOURCE,
        release_run_id=RELEASE_RUN_ID,
        release_attempt=RELEASE_ATTEMPT,
        expected_web_sha256=digest,
    )
    baseline = _inspect("dpl_" + "A" * 24, "leaf-old.vercel.app")
    deployed = _inspect("dpl_" + "B" * 24, "leaf-new.vercel.app")

    receipt = deployment_receipt(
        prepared,
        baseline,
        deployed,
        deployed,
        _approval(digest),
        handoff_run_id="654322",
        handoff_attempt="4",
        workflow_run_id="765432",
        workflow_attempt="5",
        workflow_head_sha="f" * 40,
    )

    assert receipt["schema"] == "leaf.production-web-deployment.v1"
    assert receipt["deployment_id"] == deployed["id"]
    assert receipt["baseline_deployment_id"] == baseline["id"]
    assert receipt["web_artifact_sha256"] == digest
    assert receipt["build_performed"] is False
    assert receipt["secret_values_observed"] is False
    assert receipt["approval"]["comment_id"] == 987654


def test_receipt_rejects_stable_alias_or_project_mismatch(tmp_path: Path):
    dist, digest = _dist(tmp_path)
    prepared = prepare(
        _handoff(digest),
        dist,
        tmp_path / "output",
        source=SOURCE,
        release_run_id=RELEASE_RUN_ID,
        release_attempt=RELEASE_ATTEMPT,
        expected_web_sha256=digest,
    )
    baseline = _inspect("dpl_" + "A" * 24, "leaf-old.vercel.app")
    deployed = _inspect("dpl_" + "B" * 24, "leaf-new.vercel.app")
    wrong = _inspect("dpl_" + "C" * 24, "leaf-wrong.vercel.app")
    wrong["projectId"] = "prj_wrong"

    with pytest.raises(ReleaseError):
        deployment_receipt(
            prepared,
            baseline,
            deployed,
            wrong,
            _approval(digest),
            handoff_run_id="654322",
            handoff_attempt="4",
            workflow_run_id="765432",
            workflow_attempt="5",
            workflow_head_sha="f" * 40,
        )


def test_receipt_rejects_replayed_or_unbound_approval(tmp_path: Path):
    dist, digest = _dist(tmp_path)
    prepared = prepare(
        _handoff(digest),
        dist,
        tmp_path / "output",
        source=SOURCE,
        release_run_id=RELEASE_RUN_ID,
        release_attempt=RELEASE_ATTEMPT,
        expected_web_sha256=digest,
    )
    baseline = _inspect("dpl_" + "A" * 24, "leaf-old.vercel.app")
    deployed = _inspect("dpl_" + "B" * 24, "leaf-new.vercel.app")
    for field, value in (
        ("deployment_workflow_run_id", 765433),
        ("timely_at_promotion", False),
        ("permission", "read"),
    ):
        approval = _approval(digest)
        approval[field] = value
        with pytest.raises(ReleaseError):
            deployment_receipt(
                prepared,
                baseline,
                deployed,
                deployed,
                approval,
                handoff_run_id="654322",
                handoff_attempt="4",
                workflow_run_id="765432",
                workflow_attempt="5",
                workflow_head_sha="f" * 40,
            )


def test_workflow_is_protected_prebuilt_two_phase_and_receipted():
    workflow = (
        ROOT / ".github" / "workflows" / "deploy-platform-web-production.yml"
    ).read_text(encoding="utf-8")
    for expected in (
        "refs/heads/main",
        "actions: read",
        "issues: read",
        "collaborators/$OPERATOR/permission",
        "collaborators/$APPROVER/permission",
        "approve-vercel-production:",
        "Independent production approval required",
        '[ "$APPROVER" != "$ACTOR" ]',
        '[ "$APPROVER" != "$TRIGGERING_ACTOR" ]',
        "age > 86400",
        "VERCEL_AUTOMATION_BYPASS_SECRET",
        "x-vercel-protection-bypass:",
        "production-handoff-candidate-$SOURCE_SHA-attempt-$HANDOFF_RUN_ATTEMPT",
        "web-dist-$SOURCE_SHA-attempt-$RELEASE_RUN_ATTEMPT",
        "vercel deploy --prebuilt --prod --skip-domain",
        "Verify the immutable candidate before promotion",
        "vercel promote",
        "Verify the stable production alias",
        "vercel rollback",
        "https://api.vercel.com/v13/deployments/",
        ".projectId == $project",
        "scripts/production_web_release.py receipt",
        "production-web-deployment-${{ inputs.source_sha }}-run-${{ github.run_id }}-attempt-${{ github.run_attempt }}",
    ):
        assert expected in workflow
    assert "environment: vercel-production" not in workflow
    job_env = workflow.split("    steps:", 1)[0]
    assert "runner.temp" not in job_env
    for forbidden in ("npm run build", "aws ", "ecs ", "ecr ", "secretsmanager"):
        assert forbidden not in workflow.lower()
    assert workflow.count("actions/upload-artifact@v4") == 1
    assert ".project.id" not in workflow
    assert 'RECEIPT_SCHEMA = "leaf.production-web-deployment.v1"' in (
        ROOT / "scripts" / "production_web_release.py"
    ).read_text(encoding="utf-8")
