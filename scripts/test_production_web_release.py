from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
import shutil
import stat
import sys
import uuid
import zipfile

import pytest


SCRIPTS = Path(__file__).resolve().parent
ROOT = SCRIPTS.parent
sys.path.insert(0, str(SCRIPTS))

from platform_release_manifest import web_dist_digest  # noqa: E402
from forge_native_production_handoff import build_handoff_bundle  # noqa: E402
from production_web_release import (  # noqa: E402
    PROJECT_ID,
    ReleaseError,
    deployment_receipt,
    extract_artifact,
    prepare,
    prepare_native,
    validate_native_bundle,
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
NATIVE_TREE = "b" * 40
NATIVE_TX = "d10-1234567890abcdef"
NATIVE_REPOSITORY = {"id": 46, "full_name": "LEAF-Solar-Design/leaf-web-demo"}
NATIVE_DELIVERY_BUCKET = "leaf-native-staging-delivery-807034087062"
NATIVE_CONTRACT_BUCKET = "leaf-developer-platform-artifacts-807034087062-us-east-1"
NATIVE_RELEASE_BUCKET = "leaf-studio-release-artifacts-807034087062-us-east-1"
NATIVE_STAGING_LISTENER = (
    "arn:aws:elasticloadbalancing:us-east-1:807034087062:listener/app/"
    "leaf-automation-staging-api/d5cae470e8fcdbae/96cc5c0ab89ab4d3"
)


def _native_staging_route(name: str) -> dict:
    if name not in {"app", "web"}:
        return {"kind": "unrouted"}
    priorities = {"44", "50", "51"} if name == "app" else {"60"}
    target = (
        "arn:aws:elasticloadbalancing:us-east-1:807034087062:targetgroup/"
        f"leaf-stg-platform-{name}/12339c8d6d4846c8"
    )
    return {
        "kind": "alb",
        "listener": NATIVE_STAGING_LISTENER,
        "rules": {
            priority: {
                "arn": NATIVE_STAGING_LISTENER.replace(
                    ":listener/", ":listener-rule/"
                )
                + f"/{priority}/rule",
                "actions": [{"Type": "forward", "TargetGroupArn": target}],
                "conditions": [
                    {
                        "Field": "host-header",
                        "HostHeaderConfig": {
                            "Values": ["platform-staging.leafdesign.ai"]
                        },
                    }
                ],
            }
            for priority in priorities
        },
    }


def _native_identity(project: str, number: int) -> dict:
    return {
        "project_arn": f"arn:aws:codebuild:us-east-1:807034087062:project/{project}",
        "build_arn": (
            f"arn:aws:codebuild:us-east-1:807034087062:build/{project}:"
            f"{number:08x}-1234-5678-9abc-1234567890ab"
        ),
        "build_number": number,
    }


def _native_ref(
    seed: str, transaction: str, *, revision: bool = False, name: str | None = None
) -> dict:
    digest = hashlib.sha256(seed.encode()).hexdigest()
    value = {
        "bucket": NATIVE_DELIVERY_BUCKET,
        "key": f"delivery/v1/{transaction}/{digest}/{name or seed + '.json'}",
        "version_id": f"version-{seed}",
        "sha256": digest,
    }
    if revision:
        value["revision"] = 1
    return value


def _web_entries_digest(entries: dict[str, bytes]) -> str:
    digest = hashlib.sha256(b"leaf.web-dist.v1\0")
    for name in sorted(entries):
        relative = name.removeprefix("dist/").encode()
        content = entries[name]
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def _native_web(source: str) -> tuple[bytes, str]:
    engine = {
        "engine.js": b"export default async function init() {}\n",
        "engine_bg.wasm": b"\x00asm\x01\x00\x00\x00",
    }
    provenance = {
        "contract": "leaf.cad-engine-stage.v1",
        "files": {
            name: {"sha256": hashlib.sha256(content).hexdigest(), "bytes": len(content)}
            for name, content in engine.items()
        },
    }
    entries = {
        "dist/assets/index-good.js": b"console.log('leaf')\n",
        "dist/build-config.json": json.dumps(
            {"schema": "leaf.web-build-config.v1", "vite_cad_edit": "1"}
        ).encode(),
        "dist/engine/PROVENANCE.json": json.dumps(provenance).encode(),
        "dist/engine/engine.js": engine["engine.js"],
        "dist/engine/engine_bg.wasm": engine["engine_bg.wasm"],
        "dist/health.json": (
            json.dumps(
                {
                    "ok": True,
                    "service": "leaf-platform-web",
                    "component": "frontend",
                    "source_sha": source,
                }
            )
            + "\n"
        ).encode(),
        "dist/index.html": b'<script type="module" src="/assets/index-good.js"></script>\n',
        "dist/vercel.json": (
            json.dumps(
                {"rewrites": [{"source": "/:path*", "destination": "/index.html"}]}
            )
            + "\n"
        ).encode(),
    }
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_STORED) as archive:
        for name in sorted(entries):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_STORED
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | 0o644) << 16
            archive.writestr(info, entries[name])
    return stream.getvalue(), _web_entries_digest(entries)


def _native_bundle(
    *, source: str = SOURCE, transaction: str = NATIVE_TX
) -> dict[str, bytes]:
    producer = _native_identity("leaf-studio-native-release", 42)
    gate_identity = _native_identity("leaf-studio-native-gate", 41)
    gate_build_id = gate_identity["build_arn"].rsplit(":", 1)[-1]
    gate_archive = {
        "bucket": NATIVE_RELEASE_BUCKET,
        "key": f"gate/{gate_build_id}/evidence.zip",
        "version_id": "version-gate",
        "sha256": "7" * 64,
    }
    contract_sha = "4" * 64
    producer_contract = {
        "bucket": NATIVE_CONTRACT_BUCKET,
        "key": f"trusted/native-producer/{contract_sha}/contract.json",
        "version_id": "version-contract",
        "sha256": contract_sha,
    }
    web_archive, web_artifact_sha = _native_web(source)
    native = {
        "schema": "leaf.native-release.v1",
        "provider": "aws.codebuild",
        "source_revision": source,
        "source_tree": NATIVE_TREE,
        "producer": producer,
        "services": {
            name: {
                "repository": f"leaf-platform-{name}",
                "image_digest": DIGESTS[name],
                "source_revision": source,
                "native_build_number": producer["build_number"],
            }
            for name in DIGESTS
        },
        "solver": {"revision": "c" * 40, "source_sha256": "d" * 64},
        "web": {
            "member": "web-dist.zip",
            "artifact_sha256": web_artifact_sha,
            "archive_sha256": hashlib.sha256(web_archive).hexdigest(),
        },
        "gate": {
            "producer": gate_identity,
            "source_revision": source,
            "source_tree": NATIVE_TREE,
            "archive": gate_archive,
            "proof_sha256": "1" * 64,
        },
    }
    semantic = {
        "schema": "leaf.native-staging-semantic.v1",
        "transaction_id": transaction,
        "settled": True,
        "services": {
            name: {
                "image": DIGESTS[name],
                "config": f"{index + 5:064x}",
                "tags": [{"key": "leaf:source", "value": source}],
                "route": _native_staging_route(name),
                "evidence": _native_ref(
                    "readback", transaction, revision=True, name="readback.json"
                ),
            }
            for index, name in enumerate(DIGESTS)
        },
        "activity": None,
        "source_revision": source,
        "source_tree": NATIVE_TREE,
        "repository": NATIVE_REPOSITORY,
        "branch": "main",
    }
    semantic_bytes = (
        json.dumps(semantic, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()
    semantic_ref = _native_ref(
        "semantic-live", transaction, revision=True, name="semantic-live.json"
    )
    semantic_ref["sha256"] = hashlib.sha256(semantic_bytes).hexdigest()
    semantic_ref["key"] = (
        f"delivery/v1/{transaction}/{semantic_ref['sha256']}/semantic-live.json"
    )
    binding = {
        "schema": "leaf.native-staging-live-binding.v1",
        "repository": NATIVE_REPOSITORY,
        "branch": "main",
        "transaction_id": transaction,
        "source_revision": source,
        "source_tree": NATIVE_TREE,
        "semantic_receipt": {
            key: semantic_ref[key]
            for key in ("bucket", "key", "version_id", "sha256")
        },
        "revision": 9,
    }
    transaction_readback = {
        "schema": "leaf.native-staging-transaction.v1",
        "transaction_id": transaction,
        "state": "LIVE",
        "repository": NATIVE_REPOSITORY,
        "branch": "main",
        "commit": source,
        "tree": NATIVE_TREE,
        "semantic_receipt": semantic_ref,
        "producer_contract": producer_contract,
        "revision": 12,
    }
    branch_authority = {
        "schema": "leaf.native-staging-authority-binding.v1",
        "transaction_id": transaction,
        "grant": _native_ref("standing-grant", transaction),
        "proof": _native_ref("trusted-ci", transaction),
        "repository_id": NATIVE_REPOSITORY["id"],
        "branch": "main",
        "source_revision": source,
        "source_tree": NATIVE_TREE,
        "target": "staging",
        "scope": "canonical-native-five-service-staging",
        "expires_at": "2027-10-03T00:00:00Z",
    }
    authority_bytes = _canonical_json(branch_authority)
    authority_ref = _native_ref(
        "authority", transaction, revision=True, name="authority-tx.json"
    )
    authority_ref["sha256"] = hashlib.sha256(authority_bytes).hexdigest()
    authority_ref["key"] = (
        f"delivery/v1/{transaction}/{authority_ref['sha256']}/authority-tx.json"
    )
    transaction_readback["authority"] = {
        **authority_ref,
        "transaction_id": transaction,
    }
    release_stream = io.BytesIO()
    with zipfile.ZipFile(
        release_stream, "w", compression=zipfile.ZIP_STORED
    ) as release_archive:
        release_archive.writestr(
            "staging-supply-set.json",
            json.dumps(native, sort_keys=True, separators=(",", ":")) + "\n",
        )
        release_archive.writestr("web-dist.zip", web_archive)
    release_bytes = release_stream.getvalue()
    release_build_id = producer["build_arn"].rsplit(":", 1)[-1]
    release_object = {
        "bucket": NATIVE_RELEASE_BUCKET,
        "key": f"release/{release_build_id}/evidence.zip",
        "version_id": "version-release",
        "sha256": hashlib.sha256(release_bytes).hexdigest(),
    }
    return build_handoff_bundle(
        release_object=release_object,
        release_archive=release_bytes,
        producer_readback={**producer, "status": "SUCCEEDED"},
        gate_readback={**gate_identity, "status": "SUCCEEDED"},
        semantic_live=semantic,
        live_binding=binding,
        transaction=transaction_readback,
        producer_contract=producer_contract,
        branch_authority=branch_authority,
        forge_head={
            "repository": NATIVE_REPOSITORY,
            "branch": "main",
            "commit": source,
            "tree": NATIVE_TREE,
        },
    )


def _canonical_json(value: dict) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


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
        "schema": "leaf.production-web-approval.v2",
        "project_id": PROJECT_ID,
        "deployment_id": "dpl_" + "B" * 24,
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
        "approval_mode": "independent",
        "author_separated": True,
        "timely_at_promotion": True,
    }


def _self_authorized(digest: str) -> dict:
    """The single-administrator approval the deploy workflow records.

    Same proof shape, but the approver IS the dispatcher, so separation is
    honestly false and the recorded permission must be admin.
    """
    approval = _approval(digest)
    approval["approval_mode"] = "administrator-self-authorization"
    approval["author_separated"] = False
    approval["permission"] = "admin"
    return approval


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


def test_native_consumer_accepts_closed_provider_bound_bundle():
    handoff = validate_native_bundle(_native_bundle(), source=SOURCE)

    assert handoff["schema"] == "leaf.production-handoff-candidate.v2"
    assert handoff["provider"] == "forge-native"
    assert handoff["release"]["transaction_id"] == NATIVE_TX
    assert set(handoff["staging_supply_set_services"]) == set(DIGESTS)


@pytest.mark.parametrize(
    "failure",
    [
        "provider",
        "source",
        "archive",
        "producer identity",
        "gate identity",
        "release artifact",
        "producer status",
        "repository",
        "environment",
        "route",
        "source tag",
        "proof",
        "web content",
        "semantic receipt",
        "incomplete bundle",
    ],
)
def test_native_consumer_rejects_forged_or_incomplete_evidence(failure: str):
    bundle = _native_bundle()
    source = SOURCE
    if failure == "source":
        source = "f" * 40
    elif failure == "archive":
        bundle["web-dist.zip"] += b"substituted"
    elif failure == "semantic receipt":
        semantic = json.loads(bundle["semantic-live.json"])
        semantic["source_revision"] = "f" * 40
        bundle["semantic-live.json"] = _canonical_json(semantic)
    elif failure == "route":
        _rewrite_semantic(
            bundle,
            lambda semantic: semantic["services"]["web"]["route"]["rules"][
                "60"
            ]["actions"][0].update(
                TargetGroupArn=(
                    "arn:aws:elasticloadbalancing:us-east-1:807034087062:"
                    "targetgroup/leaf-production-web/0123456789abcdef"
                )
            ),
        )
    elif failure == "source tag":
        _rewrite_semantic(
            bundle,
            lambda semantic: semantic["services"]["app"].update(
                tags=[{"key": "leaf:source", "value": "f" * 40}]
            ),
        )
    elif failure == "web content":
        _rewrite_web_content(bundle)
    elif failure == "incomplete bundle":
        bundle.pop("semantic-live.json")
    else:
        handoff = json.loads(bundle["production-handoff-candidate.json"])
        if failure == "provider":
            handoff["provider"] = "github-actions"
        elif failure == "producer identity":
            handoff["release"]["producer"]["build_number"] += 1
        elif failure == "gate identity":
            handoff["release"]["gate"]["producer"]["build_number"] += 1
        elif failure == "release artifact":
            handoff["release"]["artifact"]["key"] = "release/other/evidence.zip"
        elif failure == "producer status":
            handoff["release"]["producer_readback"]["status"] = "FAILED"
        elif failure == "repository":
            forged = {"id": 99, "full_name": "other-org/other-repo"}
            handoff["staging_acceptance"]["repository"] = forged
            handoff["staging_acceptance"]["canonical_head"]["repository"] = forged
            handoff["staging_acceptance"]["transaction_readback"]["repository"] = forged
        elif failure == "environment":
            handoff["staging_acceptance"]["environment"] = "production"
        elif failure == "proof":
            handoff["proof"]["producer_and_gate_succeeded"] = False
        bundle["production-handoff-candidate.json"] = _canonical_json(handoff)

    with pytest.raises(ReleaseError):
        validate_native_bundle(bundle, source=source)


def _rewrite_semantic(bundle: dict[str, bytes], mutate) -> None:
    semantic = json.loads(bundle["semantic-live.json"])
    mutate(semantic)
    semantic_bytes = _canonical_json(semantic)
    digest = hashlib.sha256(semantic_bytes).hexdigest()
    handoff = json.loads(bundle["production-handoff-candidate.json"])
    reference = handoff["staging_acceptance"]["semantic_receipt"]
    reference["sha256"] = digest
    reference["key"] = f"delivery/v1/{NATIVE_TX}/{digest}/semantic-live.json"
    handoff["staging_acceptance"]["transaction_readback"]["semantic_receipt"] = reference
    bundle["semantic-live.json"] = semantic_bytes
    bundle["production-handoff-candidate.json"] = _canonical_json(handoff)


def _rewrite_web_content(bundle: dict[str, bytes]) -> None:
    with zipfile.ZipFile(io.BytesIO(bundle["web-dist.zip"])) as archive:
        entries = {name: archive.read(name) for name in archive.namelist()}
    entries["dist/assets/index-good.js"] = b"console.log('forged')\n"
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_STORED) as archive:
        for name in sorted(entries):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_STORED
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | 0o644) << 16
            archive.writestr(info, entries[name])
    web = stream.getvalue()
    archive_sha = hashlib.sha256(web).hexdigest()
    manifest = json.loads(bundle["staging-supply-set.json"])
    manifest["web"]["archive_sha256"] = archive_sha
    manifest_bytes = _canonical_json(manifest)
    handoff = json.loads(bundle["production-handoff-candidate.json"])
    handoff["release"]["manifest_sha256"] = hashlib.sha256(manifest_bytes).hexdigest()
    handoff["web"]["archive_sha256"] = archive_sha
    handoff["staging_acceptance"]["web"]["archive_sha256"] = archive_sha
    bundle["staging-supply-set.json"] = manifest_bytes
    bundle["web-dist.zip"] = web
    bundle["production-handoff-candidate.json"] = _canonical_json(handoff)


def test_native_consumer_rejects_replayed_candidate_evidence():
    current = _native_bundle()
    replay = _native_bundle(transaction="d10-fedcba0987654321")
    current["production-handoff-candidate.json"] = replay[
        "production-handoff-candidate.json"
    ]

    with pytest.raises(ReleaseError):
        validate_native_bundle(current, source=SOURCE)


def test_prepare_native_reuses_engine_complete_bytes_without_workflow_ids():
    bundle = _native_bundle()
    root = ROOT / f".native-web-test-{uuid.uuid4().hex}"
    root.mkdir()
    try:
        bundle_root = root / "bundle"
        bundle_root.mkdir()
        for name, value in bundle.items():
            (bundle_root / name).write_bytes(value)
        output = root / "deploy" / ".vercel" / "output"

        prepared = prepare_native(bundle_root, output, source=SOURCE)

        assert prepared["schema"] == "leaf.production-web-prepared.v2"
        assert prepared["provider"] == "forge-native"
        assert prepared["transaction_id"] == NATIVE_TX
        assert prepared["build_performed"] is False
        assert "release_workflow_run_id" not in prepared
        assert (output / "static" / "engine" / "engine.js").is_file()
        assert (output / "static" / "engine" / "engine_bg.wasm").is_file()
        assert (output / "static" / "index.html").is_file()
    finally:
        shutil.rmtree(root)


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
    assert receipt["stable_url"] == "https://leaf-platform-web.vercel.app"
    assert receipt["production_hosts"] == ["app.leafdesign.ai", "platform.leafdesign.ai"]
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
        ("deployment_id", "dpl_" + "C" * 24),
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


def test_receipt_accepts_an_administrator_self_authorized_approval(tmp_path: Path):
    """Single-approver releases must succeed end to end.

    `author_separated` was once hardcoded true and asserted true, so an honest
    self-authorized proof could not pass the verifier at all. It is now a fact
    about the mode, not an invariant.
    """
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
        _self_authorized(digest),
        handoff_run_id="654322",
        handoff_attempt="4",
        workflow_run_id="765432",
        workflow_attempt="5",
        workflow_head_sha="f" * 40,
    )
    assert receipt


@pytest.mark.parametrize(
    "mutate",
    [
        # A mode outside the closed set is refused, never read as independent.
        {"approval_mode": "single-approver"},
        {"approval_mode": ""},
        # Contradictory evidence: claims separation while naming self-auth.
        {"approval_mode": "administrator-self-authorization", "author_separated": True},
        # ...and the reverse.
        {"approval_mode": "independent", "author_separated": False},
        # Self-authorization on anything less than live admin stays refused,
        # re-asserted here so a forged proof cannot self-authorize on write.
        {
            "approval_mode": "administrator-self-authorization",
            "author_separated": False,
            "permission": "write",
        },
        {
            "approval_mode": "administrator-self-authorization",
            "author_separated": False,
            "permission": "maintain",
        },
    ],
)
def test_receipt_rejects_contradictory_or_underprivileged_approval_modes(
    tmp_path: Path, mutate: dict
):
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
    approval = _approval(digest)
    approval.update(mutate)
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
        "timeout-minutes: 60",
        "actions: read",
        "issues: read",
        "collaborators/$OPERATOR/permission",
        "collaborators/$APPROVER/permission",
        "approve-vercel-production:",
        "approve-vercel-production-promotion:",
        "Production approval required (independent approver, or a repository administrator self-authorizing)",
        "Exact deployment approval required before public alias promotion",
        # The two-person rule is now two named modes. These pins keep the
        # administrator requirement and the anti-laundering check in the
        # workflow: dropping either would silently let a non-admin, or a rerun
        # by a different actor, self-authorize a production deploy.
        "administrator-self-authorization",
        "Self-authorization requires live repository admin permission.",
        "Self-authorization requires the dispatcher and rerun actor to be the same administrator.",
        "Self-authorization requires live repository admin permission at promotion.",
        "PROMOTION_APPROVAL_COMMENT_ID",
        'DEPLOYMENT_ID=$(jq -er \'.id\' "$RUN_DIR/deployment.json")',
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
