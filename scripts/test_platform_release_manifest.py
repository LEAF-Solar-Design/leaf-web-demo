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
    validate_manifest,
    verify_staging_receipt,
    web_dist_digest,
)


DIGESTS = {
    name: "sha256:" + format(index, "064x")
    for index, name in enumerate(SERVICES, start=1)
}
SOLVER_REVISION = "b" * 40
SOLVER_HASH = "c" * 64
WEB_HASH = "d" * 64


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

    handoff = verify_staging_receipt(_manifest(source), _receipt(source), repo, "main")

    assert handoff["source_revision"] == source
    assert handoff["schema"] == "leaf.production-handoff-candidate.v1"
    assert "services" not in handoff
    assert handoff["proof"] == {
        "source_is_ancestor_of_main": True,
        "staging_digests_equal_release": True,
    }
    worker = handoff["staging_supply_set_services"]["canonical-worker"]
    assert worker["image_digest"] == DIGESTS["canonical-worker"]


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
        verify_staging_receipt(manifest, receipt, repo, "main")
