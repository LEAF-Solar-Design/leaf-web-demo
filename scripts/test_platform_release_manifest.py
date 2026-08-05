from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

import pytest

from platform_release_manifest import (
    ContractError,
    SERVICES,
    build_manifest,
    build_speculative_manifest,
    validate_manifest,
    validate_speculative_manifest,
    verify_artifact,
    verify_staging_receipt,
    verify_workflow_run,
    web_dist_digest,
)


DIGESTS = {
    name: "sha256:" + format(index, "064x")
    for index, name in enumerate(SERVICES, start=1)
}
SOLVER_REVISION = "b" * 40
SOLVER_HASH = "c" * 64
WEB_HASH = "d" * 64
WORKFLOW_PATH = ".github/workflows/build-platform-images.yml"
RUN_ID = "123456"
RUN_ATTEMPT = "2"


def _manifest(source: str) -> dict:
    return build_manifest(
        source,
        "prod-abcdef1",
        DIGESTS,
        SOLVER_REVISION,
        SOLVER_HASH,
        WEB_HASH,
    )


def _receipt(source: str, **overrides) -> dict:
    value = {
        "schema": "leaf.deployed-authored-cad-acceptance.v1",
        "environment": "staging",
        "mode": "execute",
        "ok": True,
        "run_id": "release-proof-1",
        "source_revision": source,
        "secrets_recorded": False,
        "images": DIGESTS,
    }
    value.update(overrides)
    return value


def _workflow() -> dict:
    return {"id": 71, "name": "Untrusted display name", "path": WORKFLOW_PATH}


def _workflow_run(source: str, **overrides) -> dict:
    value = {
        "id": int(RUN_ID),
        "run_attempt": int(RUN_ATTEMPT),
        "workflow_id": 71,
        "name": "Spoofable display name",
        "path": WORKFLOW_PATH,
        "event": "push",
        "head_branch": "main",
        "head_sha": source,
        "status": "completed",
        "conclusion": "success",
    }
    value.update(overrides)
    return value


def _acceptance_proof(source: str) -> dict:
    return {
        "workflow_id": 81,
        "workflow_path": (
            ".github/workflows/accept-leaf-platform-staging-authored-cad.yml"
        ),
        "run_id": 654321,
        "run_attempt": 3,
        "event": "workflow_dispatch",
        "head_branch": "main",
        "head_sha": source,
    }


def _release_proof(source: str) -> dict:
    return {
        "workflow_id": 71,
        "workflow_path": WORKFLOW_PATH,
        "run_id": int(RUN_ID),
        "run_attempt": int(RUN_ATTEMPT),
        "event": "push",
        "head_branch": "main",
        "head_sha": source,
    }


def _git(repo: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *arguments],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _repository(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "Release Test")
    _git(repo, "config", "user.email", "release-test@example.invalid")
    (repo / "source.txt").write_text("release\n", encoding="utf-8")
    _git(repo, "add", "source.txt")
    _git(repo, "commit", "-m", "release")
    return repo, _git(repo, "rev-parse", "HEAD")


def test_cli_generates_exact_five_service_manifest_with_composite_provenance(
    tmp_path: Path,
):
    output = tmp_path / "release.json"
    command = [
        sys.executable,
        str(Path(__file__).with_name("platform_release_manifest.py")),
        "generate",
        "--source-revision",
        "a" * 40,
        "--build-tag",
        "prod-abcdef1",
        "--solver-revision",
        SOLVER_REVISION,
        "--solver-source-sha256",
        SOLVER_HASH,
        "--web-artifact-sha256",
        WEB_HASH,
        "--output",
        str(output),
    ]
    for name, digest in DIGESTS.items():
        command.extend(("--image", f"{name}={digest}"))

    subprocess.run(command, check=True)
    manifest = json.loads(output.read_text(encoding="utf-8"))

    assert manifest["source_revision"] == "a" * 40
    assert manifest["schema"] == "leaf.staging-supply-set.v1"
    assert tuple(manifest["services"]) == tuple(sorted(SERVICES))
    assert manifest["services"]["canonical-worker"]["provenance"] == {
        "application_source_revision": "a" * 40,
        "solver_source_revision": SOLVER_REVISION,
        "solver_source_sha256": SOLVER_HASH,
    }
    assert manifest["services"]["web"]["artifact_sha256"] == WEB_HASH


def test_web_dist_digest_is_deterministic_and_content_addressed(tmp_path: Path):
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<main>Leaf</main>\n", encoding="utf-8")
    (dist / "assets" / "app.js").write_bytes(b"console.log('leaf')\n")

    first = web_dist_digest(dist)
    second = web_dist_digest(dist)
    (dist / "assets" / "app.js").write_bytes(b"console.log('changed')\n")

    assert first == second
    assert len(first) == 64
    assert web_dist_digest(dist) != first


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value["services"].pop("web"),
        lambda value: value["services"]["app"].update(image_digest="prod-latest"),
        lambda value: value["services"]["broker"].update(source_revision="d" * 40),
        lambda value: value["services"]["canonical-worker"]["provenance"].pop(
            "solver_source_sha256"
        ),
    ],
)
def test_manifest_rejects_incomplete_mutable_or_mixed_release(mutate):
    manifest = _manifest("a" * 40)
    mutate(manifest)

    with pytest.raises(ContractError):
        validate_manifest(manifest)


def test_staging_handoff_proves_ancestry_and_exact_digest_equality(tmp_path: Path):
    repo, source = _repository(tmp_path)

    handoff = verify_staging_receipt(
        _manifest(source),
        _receipt(source),
        repo,
        "main",
        _release_proof(source),
        _acceptance_proof(source),
        "release-proof-1",
    )

    assert handoff["source_revision"] == source
    assert handoff["schema"] == "leaf.production-handoff-candidate.v1"
    assert "services" not in handoff
    assert handoff["proof"] == {
        "source_is_ancestor_of_main": True,
        "staging_digests_equal_release": True,
    }
    worker = handoff["staging_supply_set_services"]["canonical-worker"]
    assert worker["image_digest"] == DIGESTS["canonical-worker"]
    assert handoff["staging_acceptance"]["workflow_run_id"] == 654321
    assert handoff["staging_acceptance"]["workflow_run_attempt"] == 3
    assert handoff["release"]["workflow_run_id"] == int(RUN_ID)
    assert handoff["release"]["workflow_run_attempt"] == int(RUN_ATTEMPT)


@pytest.mark.parametrize("failure", ["digest", "mode", "ancestry"])
def test_staging_handoff_fails_closed_on_unaccepted_or_unrelated_evidence(
    tmp_path: Path, failure: str
):
    repo, source = _repository(tmp_path)
    manifest = _manifest(source)
    receipt = _receipt(source)
    if failure == "digest":
        receipt["images"] = {**DIGESTS, "web": "sha256:" + "f" * 64}
    elif failure == "mode":
        receipt["mode"] = "preflight"
    else:
        _git(repo, "checkout", "--orphan", "unrelated")
        (repo / "source.txt").write_text("unrelated\n", encoding="utf-8")
        _git(repo, "add", "source.txt")
        _git(repo, "commit", "-m", "unrelated")
        unrelated = _git(repo, "rev-parse", "HEAD")
        manifest = _manifest(unrelated)
        receipt = _receipt(unrelated)
        _git(repo, "checkout", "main")

    with pytest.raises(ContractError):
        verify_staging_receipt(
            manifest,
            receipt,
            repo,
            "main",
            _release_proof(source),
            _acceptance_proof(source),
            "release-proof-1",
        )


def test_workflow_run_uses_canonical_id_and_path_not_display_name():
    source = "a" * 40
    proof = verify_workflow_run(
        _workflow_run(source),
        _workflow(),
        run_id=RUN_ID,
        run_attempt=RUN_ATTEMPT,
        workflow_path=WORKFLOW_PATH,
        event="push",
        branch="main",
        head_sha=source,
    )

    assert proof["workflow_id"] == 71
    assert proof["workflow_path"] == WORKFLOW_PATH


@pytest.mark.parametrize(
    ("run_override", "workflow_override", "attempt"),
    [
        ({"path": ".github/workflows/spoof.yml"}, {}, RUN_ATTEMPT),
        ({"workflow_id": 72}, {}, RUN_ATTEMPT),
        ({"head_sha": "b" * 40}, {}, RUN_ATTEMPT),
        ({"head_branch": "feature/spoof"}, {}, RUN_ATTEMPT),
        ({"event": "workflow_dispatch"}, {}, RUN_ATTEMPT),
        ({"id": 123457}, {}, RUN_ATTEMPT),
        ({}, {"path": ".github/workflows/spoof.yml"}, RUN_ATTEMPT),
        ({}, {}, "3"),
    ],
    ids=(
        "spoofed-run-path",
        "wrong-workflow-id",
        "wrong-head",
        "wrong-branch",
        "wrong-event",
        "wrong-run-id",
        "spoofed-workflow-path",
        "wrong-attempt",
    ),
)
def test_workflow_run_rejects_spoofed_or_wrong_provenance(
    run_override: dict, workflow_override: dict, attempt: str
):
    source = "a" * 40
    with pytest.raises(ContractError):
        verify_workflow_run(
            _workflow_run(source, **run_override),
            {**_workflow(), **workflow_override},
            run_id=RUN_ID,
            run_attempt=attempt,
            workflow_path=WORKFLOW_PATH,
            event="push",
            branch="main",
            head_sha=source,
        )


def test_staging_handoff_rejects_wrong_internal_receipt_run_id(tmp_path: Path):
    repo, source = _repository(tmp_path)

    with pytest.raises(ContractError):
        verify_staging_receipt(
            _manifest(source),
            _receipt(source, run_id="swapped-proof-2"),
            repo,
            "main",
            _release_proof(source),
            _acceptance_proof(source),
            "release-proof-1",
        )


def test_artifact_rejects_rerun_name_collision():
    source = "a" * 40
    artifact = {
        "id": 91,
        "name": "staging-supply-set-a-attempt-2",
        "expired": False,
        "workflow_run": {"id": int(RUN_ID), "head_sha": source},
    }

    with pytest.raises(ContractError):
        verify_artifact(
            {"artifacts": [artifact, {**artifact, "id": 92}]},
            artifact_name=artifact["name"],
            run_id=RUN_ID,
            head_sha=source,
        )


TREE = "e" * 40
PREVIEW_SHA = "f" * 40


def _speculative_kwargs() -> dict:
    return {
        "source_tree": TREE,
        "built_from_revision": PREVIEW_SHA,
        "pr_number": 87654321,
        "workflow_run_id": 30983842725,
    }


def _v2_manifest(source: str) -> dict:
    return build_manifest(
        source,
        "prod-abcdef1",
        DIGESTS,
        SOLVER_REVISION,
        SOLVER_HASH,
        WEB_HASH,
        speculative=_speculative_kwargs(),
    )


def _speculative_manifest() -> dict:
    return build_speculative_manifest(
        TREE,
        PREVIEW_SHA,
        "87654321",
        "30983842725",
        DIGESTS,
        SOLVER_REVISION,
        SOLVER_HASH,
    )


def test_v2_manifest_round_trips_tree_provenance():
    manifest = _v2_manifest("a" * 40)

    validate_manifest(manifest)

    assert manifest["schema"] == "leaf.staging-supply-set.v2"
    assert manifest["source_tree"] == TREE
    assert manifest["speculative"] == {
        "built_from_revision": PREVIEW_SHA,
        "pr_number": 87654321,
        "workflow_run_id": 30983842725,
    }
    # The deployable identity fields are byte-for-byte the v1 shape: the
    # staging relay and every digest consumer read the same keys either way.
    assert manifest["build_tag"] == "prod-abcdef1"
    assert manifest["services"] == _manifest("a" * 40)["services"]


def test_v2_manifest_feeds_production_handoff_unchanged(tmp_path: Path):
    # An adopted supply set must still promote: the audit chain (manifest ->
    # receipt -> handoff candidate) accepts v2 without loosening v1.
    repo, source = _repository(tmp_path)

    handoff = verify_staging_receipt(
        _v2_manifest(source),
        _receipt(source),
        repo,
        "main",
        _release_proof(source),
        _acceptance_proof(source),
        "release-proof-1",
    )

    assert handoff["schema"] == "leaf.production-handoff-candidate.v1"
    assert handoff["proof"]["staging_digests_equal_release"] is True


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value.update(source_tree="not-a-tree"),
        lambda value: value.pop("source_tree"),
        lambda value: value.pop("speculative"),
        lambda value: value["speculative"].pop("built_from_revision"),
        lambda value: value["speculative"].update(built_from_revision="short"),
        lambda value: value["speculative"].update(pr_number="87654321"),
        lambda value: value["speculative"].update(extra="field"),
        lambda value: value.update(schema="leaf.staging-supply-set.v3"),
    ],
)
def test_v2_manifest_rejects_broken_tree_provenance(mutate):
    manifest = _v2_manifest("a" * 40)
    mutate(manifest)

    with pytest.raises(ContractError):
        validate_manifest(manifest)


def test_v1_manifest_rejects_v2_only_fields():
    manifest = _manifest("a" * 40)
    manifest["source_tree"] = TREE

    with pytest.raises(ContractError):
        validate_manifest(manifest)


def test_speculative_manifest_round_trips_and_binds_its_tag():
    manifest = _speculative_manifest()

    validate_speculative_manifest(manifest, expect_tree=TREE)

    assert manifest["schema"] == "leaf.speculative-supply-set.v1"
    assert manifest["spec_tag"] == f"spec-{TREE}-{PREVIEW_SHA[:12]}"
    assert set(manifest["services"]) == set(SERVICES)
    assert all(
        set(entry) == {"repository", "image_digest"}
        for entry in manifest["services"].values()
    )


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value.update(spec_tag="spec-" + "0" * 40 + "-" + "0" * 12),
        lambda value: value.update(spec_tag="spec-" + "e" * 40),
        lambda value: value.update(source_tree="0" * 40),
        lambda value: value["services"].pop("harness"),
        lambda value: value["services"]["app"].update(image_digest="prod-latest"),
        lambda value: value["services"]["web"].update(extra="field"),
        lambda value: value["solver"].pop("solver_source_sha256"),
        lambda value: value.update(built_from_revision="short"),
        lambda value: value.update(schema="leaf.speculative-supply-set.v2"),
    ],
)
def test_speculative_manifest_rejects_tampered_content(mutate):
    manifest = _speculative_manifest()
    mutate(manifest)

    with pytest.raises(ContractError):
        validate_speculative_manifest(manifest, expect_tree=TREE)


def test_speculative_manifest_rejects_foreign_expected_tree():
    with pytest.raises(ContractError):
        validate_speculative_manifest(_speculative_manifest(), expect_tree="0" * 40)


def test_generate_cli_requires_the_whole_speculative_argument_set(tmp_path: Path):
    output = tmp_path / "release.json"
    command = [
        sys.executable,
        str(Path(__file__).with_name("platform_release_manifest.py")),
        "generate",
        "--source-revision",
        "a" * 40,
        "--build-tag",
        "prod-abcdef1",
        "--solver-revision",
        SOLVER_REVISION,
        "--solver-source-sha256",
        SOLVER_HASH,
        "--web-artifact-sha256",
        WEB_HASH,
        "--output",
        str(output),
    ]
    for name, digest in DIGESTS.items():
        command.extend(("--image", f"{name}={digest}"))

    partial = command + ["--source-tree", TREE]
    result = subprocess.run(partial, capture_output=True, text=True)
    assert result.returncode != 0
    assert not output.exists()

    complete = command + [
        "--source-tree",
        TREE,
        "--speculative-built-from",
        PREVIEW_SHA,
        "--speculative-pr-number",
        "87654321",
        "--speculative-run-id",
        "30983842725",
    ]
    subprocess.run(complete, check=True)
    manifest = json.loads(output.read_text(encoding="utf-8"))
    assert manifest["schema"] == "leaf.staging-supply-set.v2"
    assert manifest["source_tree"] == TREE


def test_verify_speculative_cli_round_trip(tmp_path: Path):
    script = str(Path(__file__).with_name("platform_release_manifest.py"))
    manifest_path = tmp_path / "spec-supply-set.json"
    generate = [
        sys.executable,
        script,
        "generate-speculative",
        "--source-tree",
        TREE,
        "--built-from-revision",
        PREVIEW_SHA,
        "--pr-number",
        "87654321",
        "--workflow-run-id",
        "30983842725",
        "--solver-revision",
        SOLVER_REVISION,
        "--solver-source-sha256",
        SOLVER_HASH,
        "--output",
        str(manifest_path),
    ]
    for name, digest in DIGESTS.items():
        generate.extend(("--image", f"{name}={digest}"))
    subprocess.run(generate, check=True)

    verified = tmp_path / "verified.json"
    subprocess.run(
        [
            sys.executable,
            script,
            "verify-speculative",
            "--manifest",
            str(manifest_path),
            "--expect-tree",
            TREE,
            "--output",
            str(verified),
        ],
        check=True,
    )
    assert json.loads(verified.read_text(encoding="utf-8"))["spec_tag"] == (
        "spec-" + TREE + "-" + PREVIEW_SHA[:12]
    )

    mismatched = subprocess.run(
        [
            sys.executable,
            script,
            "verify-speculative",
            "--manifest",
            str(manifest_path),
            "--expect-tree",
            "0" * 40,
            "--output",
            str(tmp_path / "rejected.json"),
        ],
        capture_output=True,
        text=True,
    )
    assert mismatched.returncode != 0
    assert not (tmp_path / "rejected.json").exists()
