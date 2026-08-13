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
    } <= required


def test_manifests_keep_standard_services_selector_and_drop_legacy_sandbox():
    """The runtime tool sandbox contract is provider-based and conditional.

    ``LEAF_SANDBOX`` is the legacy tool_loader flag; terraform #561 strips the
    vestigial ``LEAF_SANDBOX="1"`` from the staging task definitions (production
    never carried it), so a manifest that still listed it would fail the
    deploy-time required-config check after that rollout. The armed contract --
    authored execution enabled requires ``LEAF_TOOL_SANDBOX_PROVIDER=e2b`` -- is
    CONDITIONAL, which a static lower-bound list cannot express; it is enforced
    instead by ``broker.validate_runtime_safety`` at boot, the deployed-posture
    gate in ``broker._execute`` per request, the harness's
    ``assertAuthoredSandboxBoundary``, and the terraform render script's
    ``_assert_target_posture``. The provider variable is therefore not
    unconditionally required either: a dark posture legitimately omits it.

    The harness still needs ``LEAF_STANDARD_SERVICES_ENV``. Without that
    selector, staging service URLs are interpreted under production endpoint
    rules and an otherwise valid task can fail during boot.
    """
    for service in ("broker", "harness"):
        manifest = json.loads(
            (ROOT / "deploy" / f"required-config.{service}.json").read_text(
                encoding="utf-8"
            )
        )
        required = set(manifest["required"]["environment"])
        assert "LEAF_SANDBOX" not in required, service
        assert "LEAF_TOOL_SANDBOX_PROVIDER" not in required, service

        if service == "harness":
            assert "LEAF_STANDARD_SERVICES_ENV" in required


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
    ``SESSIONS_DB`` keeps real readers -- ``checkpoints.py`` and
    ``session_policy.py`` still resolve it for their SQLite modes -- so making it
    durable stays a legitimate change, and no test forbids it. Those two are no
    longer selector-free, though: they answer to ``LEAF_SESSION_ANNEX_STORE``,
    which the next test requires. The rule that would actually be worth enforcing
    (require the path whenever a mode touches the legacy store) is conditional,
    which this flat manifest cannot express, so it belongs in the app's startup
    validation.
    """
    manifest = json.loads(
        (ROOT / "deploy" / "required-config.app.json").read_text(encoding="utf-8")
    )

    assert "LEAF_SESSIONS_STORE" in set(manifest["required"]["environment"])


def test_app_manifest_requires_an_explicit_session_annex_authority():
    """The annex selector has the same defect shape, and the same fix.

    ``session_annex.store_mode`` defaults to ``legacy`` exactly as
    ``session_store._store_mode`` does, and under every annex mode but
    ``postgres`` the ``session_checkpoints`` and ``session_policies`` tables live
    in the SQLite file at ``SESSIONS_DB`` -- task-local on staging, where
    ``SESSIONS_DB`` is unset. So an omitted selector silently picks an ephemeral
    authority, which is the criterion the test above states.

    On the ORDINARY path this adds no new safety property, only an earlier
    failure. ``platform_link.validate_session_annex_authority`` already refuses to
    start a task whose sessions authority is ``postgres`` while its annex is
    anything else, and staging arms that unconditionally through
    ``LEAF_PLATFORM_POSTGRES_REQUIRED=1``. The manifest entry converts that
    task-start crash into a deploy-time refusal, before a rollout churns.

    On ONE path it is the only guard there is, and that is the reason to keep it.
    A build deploy may pass an explicit ``configuration_task_definition`` naming
    any ACTIVE revision, and the manifest check diffs the source manifest against
    THAT baseline. Selecting a revision from before the cutover therefore now
    fails here. It should: read from live on 2026-08-13, no revision anywhere
    carries ``LEAF_SESSIONS_STORE=postgres`` without the annex selector -- they
    arrived together in the P4A typed cutover, with ``:580`` still
    ``dual_write_shadow`` and no annex, and ``:590`` onward both ``postgres``. So
    a baseline missing the annex selector is a PRE-CUTOVER baseline carrying
    ``dual_write_shadow``, and deploying current source onto it silently
    reinstates the ephemeral-sessions defect. The startup gate cannot catch that
    one, because it only fires when the sessions authority is ``postgres``, and
    that baseline's is not. This entry is what refuses it.

    So the refusal is the feature. If a future reader hits it, the fix is to
    select a post-cutover baseline, never to drop this entry.

    Why it was absent until now, so the gap does not read as an oversight: the
    terraform workflow's "Verify configuration baseline satisfies source manifest"
    step diffs this manifest against a baseline cloned from the PREVIOUSLY LIVE
    task definition, which by construction cannot carry a brand-new variable.
    Requiring it before it existed on the live baseline would have failed every
    build deploy. leaf-automation-aws-terraform PR #534 opened the delta rail
    that could deliver it; both staging colors now carry
    ``LEAF_SESSION_ANNEX_STORE=postgres`` (``leaf-platform-app:600`` and
    ``leaf-platform-app-alt:81``, read 2026-08-13), so the entry is now
    non-breaking on either family.
    """
    manifest = json.loads(
        (ROOT / "deploy" / "required-config.app.json").read_text(encoding="utf-8")
    )

    assert "LEAF_SESSION_ANNEX_STORE" in set(manifest["required"]["environment"])


def test_web_image_writes_source_identity_health_file():
    dockerfile = (ROOT / "deploy" / "Dockerfile.web").read_text(encoding="utf-8")
    assert "ARG LEAF_SOURCE_SHA=unknown" in dockerfile
    assert '"service":"leaf-platform-web"' in dockerfile
    assert '"component":"frontend"' in dockerfile
    assert 'source_sha":"%s' in dockerfile
