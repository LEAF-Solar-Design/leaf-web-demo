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
WEB_ARCHIVE = b"deterministic web archive fixture"


def _identity(project: str, number: int) -> dict:
    return {
        "project_arn": f"arn:aws:codebuild:us-east-1:807034087062:project/{project}",
        "build_arn": (
            f"arn:aws:codebuild:us-east-1:807034087062:build/{project}:"
            f"{number:08x}-1234-5678-9abc-1234567890ab"
        ),
        "build_number": number,
    }


def _ref(seed: str, *, revision: bool = False) -> dict:
    value = {
        "bucket": "leaf-native-staging-delivery-807034087062",
        "key": f"delivery/v1/{TX}/{seed}.json",
        "version_id": f"version-{seed}",
        "sha256": hashlib.sha256(seed.encode()).hexdigest(),
    }
    if revision:
        value["revision"] = 1
    return value


def _evidence() -> dict:
    producer = _identity("leaf-forge-native-publisher", 42)
    gate_identity = _identity("leaf-forge-native-gate", 41)
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
            "artifact_sha256": "e" * 64,
            "archive_sha256": hashlib.sha256(WEB_ARCHIVE).hexdigest(),
        },
        "gate": {
            "producer": gate_identity,
            "source_revision": SOURCE,
            "source_tree": TREE,
            "archive": _ref("gate-archive"),
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
                "tags": {"leaf:source": SOURCE},
                "route": {"target": "staging"},
                "evidence": _ref(f"{name}-readback", revision=True),
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
    semantic_ref = _ref("semantic-live")
    semantic_ref["sha256"] = hashlib.sha256(semantic_bytes).hexdigest()
    binding = {
        "schema": "leaf.native-staging-live-binding.v1",
        "repository": REPOSITORY,
        "branch": "main",
        "transaction_id": TX,
        "source_revision": SOURCE,
        "source_tree": TREE,
        "semantic_receipt": semantic_ref,
        "revision": 9,
    }
    transaction = {
        "schema": "leaf.native-staging-transaction.v1",
        "transaction_id": TX,
        "state": "LIVE",
        "commit": SOURCE,
        "tree": TREE,
        "semantic_receipt": semantic_ref,
        "revision": 12,
    }
    release_stream = io.BytesIO()
    with zipfile.ZipFile(release_stream, "w", compression=zipfile.ZIP_STORED) as bundle:
        bundle.writestr(
            "staging-supply-set.json",
            json.dumps(native, sort_keys=True, separators=(",", ":")) + "\n",
        )
        bundle.writestr("web-dist.zip", WEB_ARCHIVE)
    release_archive = release_stream.getvalue()
    release_object = _ref("native-release")
    release_object["sha256"] = hashlib.sha256(release_archive).hexdigest()
    return {
        "release_object": release_object,
        "release_archive": release_archive,
        "producer_readback": {**producer, "status": "SUCCEEDED"},
        "gate_readback": {**gate_identity, "status": "SUCCEEDED"},
        "semantic_live": semantic,
        "live_binding": binding,
        "transaction": transaction,
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
        "live_binding"
    ]["semantic_receipt"]
    assert first["staging_supply_set_services"]["web"]["artifact_sha256"] == "e" * 64
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
            "changed semantic bytes",
            lambda value: None,
        ),
        ("changed release archive", lambda value: value.update(release_archive=b"substituted")),
        (
            "unknown field",
            lambda value: value.update(release_archive=_polluted_archive(value)),
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
    return result
