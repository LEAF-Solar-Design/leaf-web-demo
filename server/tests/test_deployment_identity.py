import json

import pytest

from deployment_identity import deployment_identity


REVISION = "f" * 40
DIGEST = "sha256:" + "a" * 64


def identity(environment="staging", **overrides):
    value = {
        "schema": "leaf.deployment-identity.v1",
        "environment": environment,
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
    assert result["environment"] == "staging"
    assert result["source_revision"] == REVISION
    assert set(result["services"]) == {"app", "broker", "canonical-worker", "harness", "web"}


def test_deployment_identity_accepts_production_only_with_exact_runtime_binding():
    env = identity(environment="production")
    env["LEAF_RUNTIME_ENV"] = "production"
    env["LEAF_DEPLOYMENT_ENVIRONMENT"] = "production"

    result = deployment_identity(env)

    assert result["environment"] == "production"
    assert result["source_revision"] == REVISION


@pytest.mark.parametrize(
    ("runtime_environment", "configured_environment", "identity_environment"),
    [
        ("production", None, "production"),
        ("production", "staging", "staging"),
        ("production", "production", "staging"),
        ("staging", "staging", "production"),
        ("staging", "production", "production"),
        ("preview", "production", "production"),
    ],
)
def test_deployment_identity_rejects_runtime_environment_mismatch(
    runtime_environment, configured_environment, identity_environment,
):
    env = identity(environment=identity_environment)
    env["LEAF_RUNTIME_ENV"] = runtime_environment
    if configured_environment is not None:
        env["LEAF_DEPLOYMENT_ENVIRONMENT"] = configured_environment

    with pytest.raises(ValueError, match="environment"):
        deployment_identity(env)


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
