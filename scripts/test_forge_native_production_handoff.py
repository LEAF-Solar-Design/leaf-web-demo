from __future__ import annotations

from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path
import sys
import zipfile

import pytest


SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

from forge_native_production_handoff import (  # noqa: E402
    HandoffError,
    build_handoff,
    build_handoff_bundle,
    canonical_handoff_bytes,
)


SOURCE = "a" * 40
TREE = "b" * 40
TX = "d10-1234567890abcdef"
REPOSITORY = {"id": 46, "full_name": "LEAF-Solar-Design/leaf-web-demo"}
SERVICES = ("app", "broker", "canonical-worker", "harness", "web")
DELIVERY_BUCKET = "leaf-native-staging-delivery-807034087062"
CONTRACT_BUCKET = "leaf-developer-platform-artifacts-807034087062-us-east-1"
RELEASE_BUCKET = "leaf-studio-release-artifacts-807034087062-us-east-1"
STAGING_LISTENER = (
    "arn:aws:elasticloadbalancing:us-east-1:807034087062:listener/app/"
    "leaf-automation-staging-api/d5cae470e8fcdbae/96cc5c0ab89ab4d3"
)


def _web_archive() -> bytes:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_STORED) as bundle:
        bundle.writestr("dist/assets/index-test.js", b"console.log('leaf')\n")
        bundle.writestr("dist/index.html", b"<script src='/assets/index-test.js'></script>\n")
    return stream.getvalue()


def _web_content_digest(archive: bytes) -> str:
    digest = hashlib.sha256(b"leaf.web-dist.v1\0")
    with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
        for name in bundle.namelist():
            relative = name.removeprefix("dist/").encode()
            payload = bundle.read(name)
            digest.update(len(relative).to_bytes(8, "big"))
            digest.update(relative)
            digest.update(len(payload).to_bytes(8, "big"))
            digest.update(payload)
    return digest.hexdigest()


WEB_ARCHIVE = _web_archive()
WEB_CONTENT_SHA = _web_content_digest(WEB_ARCHIVE)


def _staging_route(name: str) -> dict:
    if name not in {"app", "web"}:
        return {"kind": "unrouted"}
    priorities = {"44", "50", "51"} if name == "app" else {"60"}
    target = (
        "arn:aws:elasticloadbalancing:us-east-1:807034087062:targetgroup/"
        f"leaf-stg-platform-{name}/12339c8d6d4846c8"
    )
    return {
        "kind": "alb",
        "listener": STAGING_LISTENER,
        "rules": {
            priority: {
                "arn": STAGING_LISTENER.replace(":listener/", ":listener-rule/")
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


def _identity(project: str, number: int) -> dict:
    return {
        "project_arn": f"arn:aws:codebuild:us-east-1:807034087062:project/{project}",
        "build_arn": (
            f"arn:aws:codebuild:us-east-1:807034087062:build/{project}:"
            f"{number:08x}-1234-5678-9abc-1234567890ab"
        ),
        "build_number": number,
    }


def _ref(seed: str, *, revision: bool = False, name: str | None = None) -> dict:
    digest = hashlib.sha256(seed.encode()).hexdigest()
    value = {
        "bucket": DELIVERY_BUCKET,
        "key": f"delivery/v1/{TX}/{digest}/{name or seed + '.json'}",
        "version_id": f"version-{seed}",
        "sha256": digest,
    }
    if revision:
        value["revision"] = 1
    return value


def _evidence() -> dict:
    producer = _identity("leaf-studio-native-release", 42)
    gate_identity = _identity("leaf-studio-native-gate", 41)
    gate_build_id = gate_identity["build_arn"].rsplit(":", 1)[-1]
    gate_archive = {
        "bucket": RELEASE_BUCKET,
        "key": f"gate/{gate_build_id}/evidence.zip",
        "version_id": "version-gate",
        "sha256": "7" * 64,
    }
    contract_sha = "4" * 64
    producer_contract = {
        "bucket": CONTRACT_BUCKET,
        "key": f"trusted/native-producer/{contract_sha}/contract.json",
        "version_id": "version-contract",
        "sha256": contract_sha,
    }
    image_digests = {
        name: f"sha256:{index:064x}" for index, name in enumerate(SERVICES, start=1)
    }
    native = {
        "schema": "leaf.native-release.v1",
        "provider": "aws.codebuild",
        "source_revision": SOURCE,
        "source_tree": TREE,
        "producer": producer,
        "services": {
            name: {
                "repository": f"leaf-platform-{name}",
                "image_digest": image_digests[name],
                "source_revision": SOURCE,
                "native_build_number": producer["build_number"],
            }
            for name in SERVICES
        },
        "solver": {"revision": "c" * 40, "source_sha256": "d" * 64},
        "web": {
            "member": "web-dist.zip",
            "artifact_sha256": WEB_CONTENT_SHA,
            "archive_sha256": hashlib.sha256(WEB_ARCHIVE).hexdigest(),
        },
        "gate": {
            "producer": gate_identity,
            "source_revision": SOURCE,
            "source_tree": TREE,
            "archive": gate_archive,
            "proof_sha256": "1" * 64,
        },
    }
    semantic = {
        "schema": "leaf.native-staging-semantic.v1",
        "transaction_id": TX,
        "settled": True,
        "services": {
            name: {
                "image": image_digests[name],
                "config": f"{index + 5:064x}",
                "tags": [{"key": "leaf:source", "value": SOURCE}],
                "route": _staging_route(name),
                "evidence": _ref("readback", revision=True, name="readback.json"),
            }
            for index, name in enumerate(SERVICES)
        },
        "activity": None,
        "source_revision": SOURCE,
        "source_tree": TREE,
        "repository": REPOSITORY,
        "branch": "main",
    }
    semantic_bytes = (
        json.dumps(semantic, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()
    semantic_ref = _ref("semantic-live", revision=True, name="semantic-live.json")
    semantic_ref["sha256"] = hashlib.sha256(semantic_bytes).hexdigest()
    semantic_ref["key"] = f"delivery/v1/{TX}/{semantic_ref['sha256']}/semantic-live.json"
    binding = {
        "schema": "leaf.native-staging-live-binding.v1",
        "repository": REPOSITORY,
        "branch": "main",
        "transaction_id": TX,
        "source_revision": SOURCE,
        "source_tree": TREE,
        "semantic_receipt": {
            key: semantic_ref[key] for key in ("bucket", "key", "version_id", "sha256")
        },
        "revision": 9,
    }
    transaction = {
        "schema": "leaf.native-staging-transaction.v1",
        "transaction_id": TX,
        "state": "LIVE",
        "repository": REPOSITORY,
        "branch": "main",
        "commit": SOURCE,
        "tree": TREE,
        "semantic_receipt": semantic_ref,
        "producer_contract": producer_contract,
        "revision": 12,
    }
    branch_authority = {
        "schema": "leaf.native-staging-authority-binding.v1",
        "transaction_id": TX,
        "grant": _ref("standing-grant", name="standing-grant.json"),
        "proof": _ref("trusted-ci", name="trusted-ci.json"),
        "repository_id": REPOSITORY["id"],
        "branch": "main",
        "source_revision": SOURCE,
        "source_tree": TREE,
        "target": "staging",
        "scope": "canonical-native-five-service-staging",
        "expires_at": "2027-10-03T00:00:00Z",
    }
    authority_bytes = (
        json.dumps(branch_authority, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()
    authority_ref = _ref("authority", revision=True, name="authority-tx.json")
    authority_ref["sha256"] = hashlib.sha256(authority_bytes).hexdigest()
    authority_ref["key"] = f"delivery/v1/{TX}/{authority_ref['sha256']}/authority-tx.json"
    transaction["authority"] = {**authority_ref, "transaction_id": TX}
    release_stream = io.BytesIO()
    with zipfile.ZipFile(release_stream, "w", compression=zipfile.ZIP_STORED) as bundle:
        bundle.writestr(
            "staging-supply-set.json",
            json.dumps(native, sort_keys=True, separators=(",", ":")) + "\n",
        )
        bundle.writestr("web-dist.zip", WEB_ARCHIVE)
    release_archive = release_stream.getvalue()
    release_object = _ref("native-release", revision=True, name="native-release.zip")
    release_object["sha256"] = hashlib.sha256(release_archive).hexdigest()
    release_object["key"] = f"delivery/v1/{TX}/{release_object['sha256']}/native-release.zip"
    return {
        "release_object": release_object,
        "release_archive": release_archive,
        "producer_readback": {**producer, "status": "SUCCEEDED"},
        "gate_readback": {**gate_identity, "status": "SUCCEEDED"},
        "semantic_live": semantic,
        "live_binding": binding,
        "transaction": transaction,
        "producer_contract": producer_contract,
        "branch_authority": branch_authority,
        "forge_head": {
            "repository": REPOSITORY,
            "branch": "main",
            "commit": SOURCE,
            "tree": TREE,
        },
    }


def test_native_handoff_is_deterministic_and_preserves_provenance():
    evidence = _evidence()
    first = build_handoff(**evidence)
    second = build_handoff(**deepcopy(evidence))

    assert canonical_handoff_bytes(first) == canonical_handoff_bytes(second)
    assert first["schema"] == "leaf.production-handoff-candidate.v2"
    assert first["provider"] == "forge-native"
    assert first["release"]["transaction_id"] == TX
    assert first["staging_acceptance"]["semantic_receipt"] == evidence[
        "transaction"
    ]["semantic_receipt"]
    assert first["release"]["source_revision"] == SOURCE
    assert first["release"]["producer_readback"]["status"] == "SUCCEEDED"
    assert first["release"]["gate_readback"]["status"] == "SUCCEEDED"
    assert first["release"]["producer_contract"] == evidence["producer_contract"]
    assert first["staging_acceptance"]["environment"] == "staging"
    assert first["staging_acceptance"]["state"] == "LIVE"
    assert first["staging_acceptance"]["branch_authority"] == evidence["branch_authority"]
    assert first["staging_acceptance"]["transaction_readback"]["revision"] == 12
    assert first["staging_supply_set_services"]["web"]["artifact_sha256"] == WEB_CONTENT_SHA
    assert first["web"]["archive_sha256"] == hashlib.sha256(WEB_ARCHIVE).hexdigest()
    assert first["proof"] == {
        "source_is_canonical_main": True,
        "producer_and_gate_succeeded": True,
        "staging_state_is_live": True,
        "staging_digests_equal_release": True,
        "web_archive_bytes_equal_release": True,
    }
    assert "workflow" not in canonical_handoff_bytes(first).decode()


def test_native_handoff_bundle_is_closed_and_reuses_exact_web_bytes():
    evidence = _evidence()
    bundle = build_handoff_bundle(**evidence)

    assert set(bundle) == {
        "staging-supply-set.json",
        "web-dist.zip",
        "semantic-live.json",
        "production-handoff-candidate.json",
    }
    assert bundle["web-dist.zip"] == WEB_ARCHIVE
    assert json.loads(bundle["staging-supply-set.json"])["schema"] == (
        "leaf.native-release.v1"
    )
    assert json.loads(bundle["semantic-live.json"])["settled"] is True
    assert json.loads(bundle["production-handoff-candidate.json"])["provider"] == (
        "forge-native"
    )


@pytest.mark.parametrize(
    ("label", "mutate"),
    [
        ("failed producer", lambda value: value["producer_readback"].update(status="FAILED")),
        ("failed gate", lambda value: value["gate_readback"].update(status="FAILED")),
        ("mixed source", lambda value: value["semantic_live"].update(source_revision="9" * 40)),
        ("mixed tree", lambda value: value["forge_head"].update(tree="9" * 40)),
        (
            "wrong repository",
            lambda value: value["forge_head"].update(
                repository={"id": 99, "full_name": "other-org/other-repo"}
            ),
        ),
        (
            "wrong producer project",
            lambda value: value["producer_readback"].update(
                project_arn=_identity("other-native-release", 42)["project_arn"]
            ),
        ),
        (
            "wrong gate project",
            lambda value: value["gate_readback"].update(
                project_arn=_identity("other-native-gate", 41)["project_arn"]
            ),
        ),
        ("not LIVE", lambda value: value["transaction"].update(state="FAILED")),
        ("unsettled", lambda value: value["semantic_live"].update(settled=False)),
        (
            "changed image",
            lambda value: value["semantic_live"]["services"]["web"].update(
                image="sha256:" + "9" * 64
            ),
        ),
        (
            "mutable object",
            lambda value: value["release_object"].update(version_id="null"),
        ),
        (
            "wrong release bucket",
            lambda value: value["release_object"].update(bucket="other-bucket"),
        ),
        (
            "wrong release key",
            lambda value: value["release_object"].update(key="delivery/v1/other/native-release.zip"),
        ),
        (
            "production route",
            lambda value: value["semantic_live"]["services"]["web"]["route"][
                "rules"
            ]["60"]["actions"][0].update(
                TargetGroupArn=(
                    "arn:aws:elasticloadbalancing:us-east-1:807034087062:"
                    "targetgroup/leaf-production-web/0123456789abcdef"
                )
            ),
        ),
        (
            "wrong source tag",
            lambda value: value["semantic_live"]["services"]["app"].update(
                tags=[{"key": "leaf:source", "value": "9" * 40}]
            ),
        ),
        (
            "different branch authority",
            lambda value: value["branch_authority"].update(branch="feature/forged"),
        ),
        (
            "different producer contract",
            lambda value: value["transaction"].update(
                producer_contract={
                    **value["producer_contract"],
                    "version_id": "substituted-version",
                }
            ),
        ),
        (
            "changed semantic bytes",
            lambda value: None,
        ),
        ("changed release archive", lambda value: value.update(release_archive=b"substituted")),
        (
            "unknown field",
            lambda value: value.update(release_archive=_polluted_archive(value)),
        ),
        (
            "changed web content",
            lambda value: value.update(release_archive=_changed_web_archive(value)),
        ),
    ],
)
def test_native_handoff_refuses_unbound_or_mixed_evidence(label, mutate):
    evidence = _evidence()
    if label == "changed semantic bytes":
        evidence["semantic_live"]["activity"] = {"unexpected": True}
    else:
        mutate(evidence)
    with pytest.raises(HandoffError):
        build_handoff(**evidence)


def _polluted_archive(value):
    with zipfile.ZipFile(io.BytesIO(value["release_archive"])) as bundle:
        manifest = json.loads(bundle.read("staging-supply-set.json"))
        web = bundle.read("web-dist.zip")
    manifest["workflow_run_id"] = 123456
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_STORED) as bundle:
        bundle.writestr(
            "staging-supply-set.json",
            json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n",
        )
        bundle.writestr("web-dist.zip", web)
    result = stream.getvalue()
    value["release_object"]["sha256"] = hashlib.sha256(result).hexdigest()
    value["release_object"]["key"] = (
        f"delivery/v1/{TX}/{value['release_object']['sha256']}/native-release.zip"
    )
    return result


def _changed_web_archive(value):
    with zipfile.ZipFile(io.BytesIO(value["release_archive"])) as bundle:
        manifest = json.loads(bundle.read("staging-supply-set.json"))
    changed_stream = io.BytesIO()
    with zipfile.ZipFile(changed_stream, "w", compression=zipfile.ZIP_STORED) as bundle:
        bundle.writestr("dist/assets/index-test.js", b"console.log('substituted')\n")
        bundle.writestr("dist/index.html", b"<script src='/assets/index-test.js'></script>\n")
    changed = changed_stream.getvalue()
    manifest["web"]["archive_sha256"] = hashlib.sha256(changed).hexdigest()
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_STORED) as bundle:
        bundle.writestr(
            "staging-supply-set.json",
            json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n",
        )
        bundle.writestr("web-dist.zip", changed)
    result = stream.getvalue()
    value["release_object"]["sha256"] = hashlib.sha256(result).hexdigest()
    value["release_object"]["key"] = (
        f"delivery/v1/{TX}/{value['release_object']['sha256']}/native-release.zip"
    )
    return result
