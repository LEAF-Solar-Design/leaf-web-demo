import json

import pytest

from deployment_identity import deployment_identity


REVISION = "f" * 40
DIGEST = "sha256:" + "a" * 64


def identity(**overrides):
    value = {
        "schema": "leaf.deployment-identity.v1",
        "environment": "staging",
        "source_revision": REVISION,
        "services": {
            name: {"image_digest": DIGEST, "source_revision": REVISION}
            for name in ("app", "broker", "canonical-worker", "harness", "web")
        },
    }
    value.update(overrides)
    return {"LEAF_DEPLOYMENT_IDENTITY": json.dumps(value)}


def test_deployment_identity_returns_only_validated_runtime_receipt():
    result = deployment_identity(identity())
    assert result["source_revision"] == REVISION
    assert set(result["services"]) == {"app", "broker", "canonical-worker", "harness", "web"}


@pytest.mark.parametrize("mutate", [
    lambda value: value.pop("LEAF_DEPLOYMENT_IDENTITY"),
    lambda value: value.update(LEAF_DEPLOYMENT_IDENTITY="{}"),
    lambda value: value.update(LEAF_DEPLOYMENT_IDENTITY=json.dumps({
        "schema": "leaf.deployment-identity.v1", "environment": "staging",
        "source_revision": "f" * 39, "services": {},
    })),
])
def test_deployment_identity_fails_closed_on_missing_or_invalid_runtime_receipt(mutate):
    env = identity()
    mutate(env)
    with pytest.raises(ValueError):
        deployment_identity(env)


def test_deployment_identity_rejects_a_mixed_service_revision():
    value = json.loads(identity()["LEAF_DEPLOYMENT_IDENTITY"])
    value["services"]["web"]["source_revision"] = "e" * 40
    with pytest.raises(ValueError, match="mixed"):
        deployment_identity({"LEAF_DEPLOYMENT_IDENTITY": json.dumps(value)})
