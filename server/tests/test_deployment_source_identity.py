import json
from pathlib import Path

from fastapi.testclient import TestClient

import app as app_module
from deployment_identity import deployment_identity


ROOT = Path(__file__).resolve().parents[2]


def test_health_reports_image_source_sha(monkeypatch):
    source_sha = "a" * 40
    monkeypatch.setenv("LEAF_SOURCE_SHA", source_sha)

    response = TestClient(app_module.app).get("/api/health")

    assert response.status_code == 200
    assert response.json()["source_sha"] == source_sha


def test_health_does_not_resolve_a_tenant_catalog(monkeypatch):
    def reject_tenant_resolution(*_args, **_kwargs):
        raise AssertionError("process health must not resolve a tenant catalog")

    monkeypatch.setattr(app_module.deps, "all_tools", reject_tenant_resolution)
    monkeypatch.setattr(
        app_module.deps, "load_tenant_repo_tools", reject_tenant_resolution
    )

    response = TestClient(app_module.app).get("/api/health")

    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert response.json()["n_tools"] == len(app_module.deps.shared_tools())


def test_required_deployment_manifests_have_frozen_shape():
    for service in ("app", "broker", "harness", "web"):
        manifest = json.loads(
            (ROOT / "deploy" / f"required-config.{service}.json").read_text(
                encoding="utf-8"
            )
        )
        assert manifest["schemaVersion"] == 1
        assert manifest["container"] == f"leaf-platform-{service}"
        assert set(manifest["required"]) == {"environment", "mountPaths", "secrets"}
        assert all(
            isinstance(value, list)
            and all(isinstance(item, str) and item for item in value)
            for value in manifest["required"].values()
        )


def test_required_deployment_manifests_include_runtime_auth_secrets():
    expected = {
        "app": {"LEAF_BROKER_SECRET", "LEAF_OPS_SECRET"},
        "broker": {"LEAF_BROKER_SECRET"},
        "harness": {"LEAF_BROKER_SECRET"},
    }

    for service, required_secrets in expected.items():
        manifest = json.loads(
            (ROOT / "deploy" / f"required-config.{service}.json").read_text(
                encoding="utf-8"
            )
        )
        assert required_secrets <= set(manifest["required"]["secrets"])


def test_broker_manifest_requires_explicit_production_posture():
    """The broker's fail-closed guards all key off LEAF_RUNTIME_ENV.

    ``_production_runtime`` gates ``validate_runtime_safety`` and the authored-
    execution refusal in ``_execute``, and ``_authored_execution_enabled``
    DEFAULTS TO ON when the posture is absent. A broker deployed without this
    variable therefore runs every guard in the permissive branch and loads
    tenant-authored Python in the credential-holding process. The harness and
    app manifests already require it; the broker must too, so a deploy cannot
    omit the posture silently.
    """
    manifest = json.loads(
        (ROOT / "deploy" / "required-config.broker.json").read_text(encoding="utf-8")
    )
    required = set(manifest["required"]["environment"])
    assert {
        "LEAF_AUTHORED_EXECUTION",
        "LEAF_RUNTIME_ENV",
        "LEAF_SANDBOX",
    } <= required


def test_credential_holding_services_require_runtime_posture():
    """LEAF_RUNTIME_ENV must be a required env on every service that enforces it."""
    for service in ("app", "broker", "harness"):
        manifest = json.loads(
            (ROOT / "deploy" / f"required-config.{service}.json").read_text(
                encoding="utf-8"
            )
        )
        assert "LEAF_RUNTIME_ENV" in set(manifest["required"]["environment"]), (
            f"{service} manifest omits LEAF_RUNTIME_ENV"
        )


def test_app_manifest_requires_durable_runtime_and_build_identity():
    manifest = json.loads(
        (ROOT / "deploy" / "required-config.app.json").read_text(encoding="utf-8")
    )
    required = set(manifest["required"]["environment"])
    assert {
        "LEAF_AGENT_AUDIT",
        "LEAF_AGENT_LEDGER",
        "LEAF_AGENT_STATE_DIR",
        "LEAF_AGENT_TENANTS_FILE",
        "LEAF_BUILD_REVISION_REQUIRED",
        "LEAF_GUEST_CAP_STORE",
        "LEAF_GUEST_STORE_DIR",
        "LEAF_JOBS_STORE",
        "LEAF_UPLOAD_IMPORT_MUTATIONS_ENABLED",
    } <= required
    broker_manifest = json.loads(
        (ROOT / "deploy" / "required-config.broker.json").read_text(
            encoding="utf-8"
        )
    )
    assert "LEAF_UPLOAD_IMPORT_MUTATIONS_ENABLED" not in set(
        broker_manifest["required"]["environment"]
    )
    assert {
        "LEAF_GUEST_CAP_HMAC_SECRET",
        "LEAF_GUEST_SECRET",
    } <= set(manifest["required"]["secrets"])


def test_app_manifest_requires_the_broker_store_authority():
    """The app reads LEAF_BROKER_STORE from its OWN process env.

    The app serves /api/ops/* and /api/usage itself and never proxies them to
    the broker, so the broker carrying the variable does nothing for them. All
    three readers default to "legacy" when it is absent:

      routers/ops_metrics.py::_requires_postgres  -> 503 on the whole ops
        read-API.
      routers/usage.py::_aggregate_usage          -> BILLING-VISIBLE. Legacy
        mode aggregates broker_ledger.jsonl, which is per-container while the
        broker writes runs to PostgreSQL, so tenant-facing today/total usage and
        the quota `remaining` come from an authority nothing writes any more.
      routers/ops.py::_disabled_set               -> disabled-tenant lookups
        fall back to the legacy broker_tenants.json file.

    The manifest declared it for the broker but not the app, so by the declared
    contract it was broker-only while app-served code depended on it. Production
    ran that way after the PostgreSQL cutover and answered 503 on
    /api/ops/metrics under a valid ops secret. Pin it here so a deploy cannot
    call the app "correct" while it is in legacy mode.
    """
    app_required = set(
        json.loads(
            (ROOT / "deploy" / "required-config.app.json").read_text(encoding="utf-8")
        )["required"]["environment"]
    )
    broker_required = set(
        json.loads(
            (ROOT / "deploy" / "required-config.broker.json").read_text(
                encoding="utf-8"
            )
        )["required"]["environment"]
    )

    assert "LEAF_BROKER_STORE" in app_required
    assert "LEAF_BROKER_STORE" in broker_required


def test_baseline_manifests_exclude_later_authored_activation_config():
    manifests = {
        service: json.loads(
            (ROOT / "deploy" / f"required-config.{service}.json").read_text(
                encoding="utf-8"
            )
        )["required"]
        for service in ("app", "broker", "harness")
    }

    assert {"LEAF_TOOL_SANDBOX_PROVIDER"}.isdisjoint(
        manifests["broker"]["environment"]
    )
    assert {"E2B_API_KEY"}.isdisjoint(manifests["broker"]["secrets"])
    assert {
        "LEAF_AUTHOR_SANDBOX_PROVIDER",
        "LEAF_TOOL_SANDBOX_PROVIDER",
    }.isdisjoint(manifests["harness"]["environment"])
    assert {"DATABASE_URL", "E2B_API_KEY"}.isdisjoint(
        manifests["harness"]["secrets"]
    )


def test_configuration_baseline_validation_precedes_identity_injection():
    manifest = json.loads(
        (ROOT / "deploy" / "required-config.app.json").read_text(encoding="utf-8")
    )["required"]
    generated = {"LEAF_DEPLOYMENT_ENVIRONMENT", "LEAF_DEPLOYMENT_IDENTITY"}

    # The controller validates these names against the existing task definition
    # before it creates and injects the deployment receipt.
    assert generated.isdisjoint(manifest["environment"])

    receipt = {
        "schema": "leaf.deployment-identity.v1",
        "environment": "staging",
        "source_revision": "a" * 40,
        "services": {
            name: {
                "image_digest": "sha256:" + "b" * 64,
                "source_revision": "a" * 40,
            }
            for name in ("app", "broker", "canonical-worker", "harness", "web")
        },
    }
    runtime = {
        "LEAF_RUNTIME_ENV": "staging",
        "LEAF_DEPLOYMENT_IDENTITY": json.dumps(receipt),
    }

    assert deployment_identity(runtime) == receipt


def test_app_manifest_requires_an_explicit_session_authority():
    """An absent LEAF_SESSIONS_STORE silently selects an EPHEMERAL authority.

    ``session_store._store_mode`` defaults to ``legacy``, and ``legacy`` reads
    and writes SQLite alone at ``SESSIONS_DB``, which itself defaults to the
    task-local ``server/sessions.db``. A deploy that omits the selector
    therefore lands an app whose session authority dies with the ECS task, and
    nothing in the manifest notices. Absence is also not readable from the task
    definition alone: ``deploy/Dockerfile.app`` bakes a value into the image, so
    a task definition that sets nothing still runs an unstated mode.

    Same shape as ``test_broker_manifest_requires_explicit_production_posture``
    above: a variable whose ABSENCE picks the unsafe branch has to be required,
    so the deployment states its authority instead of inheriting one.

    This requires the SELECTOR, not ``SESSIONS_DB``, and that is deliberate:
    requiring the path is the obvious-looking fix and does not work. Under a
    legacy-touching mode, pointing it at a FRESH durable path leaves an empty
    SQLite against a populated PostgreSQL, so every existing
    ``(tenant_id, drawing_id)`` fails ``_shadow_equal`` permanently instead of
    only until the next task replacement, and no reverse repair exists because
    ``scripts/reconcile_sessions_authority.py`` copies SQLite to PostgreSQL only.

    That is an argument against requiring the path HERE, not against the path.
    ``SESSIONS_DB`` keeps real readers under every mode -- ``checkpoints.py`` and
    ``session_policy.py`` resolve it independently of the selector -- so making it
    durable stays a legitimate change, and no test forbids it. The rule that
    would actually be worth enforcing (require the path whenever the mode touches
    the legacy store) is conditional, which this flat manifest cannot express, so
    it belongs in the app's startup validation.
    """
    manifest = json.loads(
        (ROOT / "deploy" / "required-config.app.json").read_text(encoding="utf-8")
    )

    assert "LEAF_SESSIONS_STORE" in set(manifest["required"]["environment"])


def test_web_image_writes_source_identity_health_file():
    dockerfile = (ROOT / "deploy" / "Dockerfile.web").read_text(encoding="utf-8")
    assert "ARG LEAF_SOURCE_SHA=unknown" in dockerfile
    assert '"service":"leaf-platform-web"' in dockerfile
    assert '"component":"frontend"' in dockerfile
    assert 'source_sha":"%s' in dockerfile
