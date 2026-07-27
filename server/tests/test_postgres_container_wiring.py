"""Static checks for staged PostgreSQL container wiring.

These checks do not need a Docker daemon or a live database. They protect the
legacy defaults and process trust boundaries that must hold before an operator
can run the separate migration and cutover stages.
"""
from pathlib import Path
import json
import re

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
CUSTOMIZATION_POSTGRES_GATE_FRAGMENTS = (
    "- 'platform/authority-inventory.json'",
    "- 'platform/db.py'",
    "- 'platform/migrations/0020_customization_authority.sql'",
    "- 'scripts/reconcile_customization_authority.py'",
    "- 'server/customization_models.py'",
    "- 'server/customization_postgres_store.py'",
    "- 'server/customization_service.py'",
    "- 'server/customization_store.py'",
    "- 'server/platform_link.py'",
    "- 'server/tests/test_customization_postgres_contract.py'",
    "- 'server/tests/test_customization_postgres_integration.py'",
    "- 'server/tests/test_customization_runtime.py'",
    (
        "PG_CUSTOMIZATION_TEST_URL: "
        "postgresql://postgres:postgres@127.0.0.1:5432/leaf_test"
    ),
    'ln -s "$GITHUB_WORKSPACE/scripts"',
    'PYTHONPATH="$RUNNER_TEMP/leaf-customization-pythonpath"',
    '"$GITHUB_WORKSPACE/server/tests/test_customization_postgres_integration.py"',
)


def _read(path: str) -> str:
    return (REPO_ROOT / path).read_text(encoding="utf-8")


def _service(compose: str, name: str) -> str:
    match = re.search(
        rf"^  {re.escape(name)}:\s*\n(.*?)(?=^  [a-zA-Z0-9_-]+:\s*\n|\Z)",
        compose,
        flags=re.MULTILINE | re.DOTALL,
    )
    assert match is not None, f"missing compose service {name}"
    return match.group(1)


def _required_environment(path: str) -> set[str]:
    manifest = json.loads(_read(path))
    return set(manifest["required"]["environment"])


def _assert_customization_postgres_gate(workflow: str) -> None:
    missing = [
        fragment
        for fragment in CUSTOMIZATION_POSTGRES_GATE_FRAGMENTS
        if fragment not in workflow
    ]
    assert not missing, f"customization PostgreSQL gate omitted: {missing}"


def test_required_config_manifests_fail_closed_for_postgres_authority():
    app_environment = _required_environment("deploy/required-config.app.json")
    broker_environment = _required_environment("deploy/required-config.broker.json")

    assert {
        "LEAF_BLOB_STORE",
        "LEAF_DRAWING_MUTATIONS_FENCE_FILE",
        "LEAF_PLATFORM_POSTGRES_REQUIRED",
        "LEAF_UPLOAD_IMPORT_MUTATIONS_ENABLED",
    }.issubset(app_environment)
    assert {
        "LEAF_BLOB_STORE",
        "LEAF_BROKER_STORE",
        "LEAF_DRAWING_MUTATIONS_FENCE_FILE",
    }.issubset(broker_environment)
    assert "LEAF_UPLOAD_IMPORT_MUTATIONS_ENABLED" not in broker_environment


def test_upload_import_boundary_has_a_real_postgres_pr_gate():
    workflow = _read(".github/workflows/upload-authority-postgres.yml")

    for protected_path in (
        "platform/api.py",
        # The shared cutover fence IS part of this boundary: without it listed,
        # a change to the fence alone would skip the gate entirely.
        "platform/mutation_fence.py",
        "platform/tests/test_drawing_import.py",
        "server/routers/uploads.py",
        "server/write_loop.py",
        "deploy/required-config.app.json",
    ):
        assert f"- '{protected_path}'" in workflow
    assert "working-directory: server" in workflow
    assert "python -m pytest --import-mode=importlib -q" in workflow
    assert "../platform/tests/test_drawing_import.py" in workflow


def test_customization_authority_has_a_real_postgres_pr_gate():
    _assert_customization_postgres_gate(
        _read(".github/workflows/upload-authority-postgres.yml")
    )


@pytest.mark.parametrize(
    "required_fragment",
    CUSTOMIZATION_POSTGRES_GATE_FRAGMENTS,
)
def test_customization_postgres_gate_rejects_each_omission(
    required_fragment: str,
):
    workflow = _read(".github/workflows/upload-authority-postgres.yml")
    assert required_fragment in workflow

    with pytest.raises(
        AssertionError,
        match="customization PostgreSQL gate omitted",
    ):
        _assert_customization_postgres_gate(
            workflow.replace(required_fragment, "", 1)
        )


def test_broker_image_contains_pg_runtime_without_crossing_secret_boundary():
    dockerfile = _read("deploy/Dockerfile.broker")
    dockerignore = _read(".dockerignore")

    assert "COPY platform/requirements.txt /app/platform/requirements.txt" in dockerfile
    assert "-r /app/platform/requirements.txt" in dockerfile
    assert "COPY platform/ /app/platform/" in dockerfile
    assert "LEAF_BROKER_STORE=legacy" in dockerfile
    assert "USER 10001:10001" in dockerfile

    # The broker may copy the one E2B helper, but never the harness manifest,
    # SDK dependency tree, grant files, or an APS credential.
    assert "COPY harness/package" not in dockerfile
    assert "COPY harness/src" not in dockerfile
    assert "COPY .aps" not in dockerfile
    assert "COPY .secrets" not in dockerfile
    assert "**/.env.local" in dockerignore
    assert ".secrets/" in dockerignore


def test_app_and_harness_images_are_ready_but_keep_legacy_defaults():
    app = _read("deploy/Dockerfile.app")
    harness = _read("deploy/Dockerfile.harness")
    harness_serve = _read("harness/scripts/serve.ts")
    package = _read("harness/package.json")

    assert "COPY platform/requirements.txt" in app
    assert "-r /app/platform/requirements.txt" in app
    assert "COPY platform/" in app
    assert "LEAF_JOBS_STORE=legacy" in app
    assert "LEAF_SESSIONS_STORE=legacy" in app
    assert "LEAF_AGENT_STORE=legacy" in app
    assert "LEAF_GUEST_CAP_STORE=memory" in app
    assert "LEAF_DRAWING_STORE=legacy" in app
    assert "LEAF_UPLOAD_STORE=legacy" in app
    assert "LEAF_CALLBACK_REPLAY_STORE=legacy" in _read("deploy/Dockerfile.broker")
    assert "LEAF_JOBS_STORE=legacy" in _read("deploy/Dockerfile.broker")
    assert "LEAF_DRAWING_STORE=legacy" in _read("deploy/Dockerfile.broker")

    assert '"pg"' in package
    assert "RUN npm ci" in harness
    assert "COPY harness/src/" in harness
    assert "LEAF_HARNESS_SESSION_STORE=file" in harness
    assert "LEAF_HARNESS_AUTHORING_MODE=disabled" in harness
    assert "information_schema.columns" in harness_serve
    assert "FROM pg_constraint" in harness_serve
    assert "FROM pg_indexes" in harness_serve
    assert "harness_tenant_repo_leases" in harness_serve
    assert "USER 10002:10002" in harness


def test_harness_bootstraps_tls_before_using_debian_package_sources():
    harness = _read("deploy/Dockerfile.harness")

    trust_copy = (
        "COPY --from=trust-store /etc/ssl/certs/ca-certificates.crt "
        "/etc/ssl/certs/ca-certificates.crt"
    )
    rewrite_marker = "s|http://deb.debian.org|https://deb.debian.org|g"
    assert "FROM node:22-bookworm AS trust-store" in harness
    assert trust_copy in harness
    assert rewrite_marker in harness
    assert "s|http://security.debian.org|https://security.debian.org|g" in harness
    assert (
        harness.index(trust_copy)
        < harness.index(rewrite_marker)
        < harness.index("apt-get update")
    )
    assert "apt-get install -y --no-install-recommends git ca-certificates" in harness


def test_base_compose_uses_explicit_legacy_defaults_and_separate_connections():
    compose = _read("docker-compose.yml")
    app = _service(compose, "app")
    broker = _service(compose, "broker")
    harness = _service(compose, "harness")

    assert "LEAF_SESSIONS_STORE: ${LEAF_SESSIONS_STORE:-legacy}" in app
    assert "LEAF_JOBS_STORE: ${LEAF_JOBS_STORE:-legacy}" in app
    assert "LEAF_AGENT_STORE: ${LEAF_AGENT_STORE:-legacy}" in app
    assert "LEAF_GUEST_CAP_STORE: ${LEAF_GUEST_CAP_STORE:-memory}" in app
    assert "LEAF_DRAWING_STORE: ${LEAF_DRAWING_STORE:-legacy}" in app
    assert "LEAF_UPLOAD_STORE: ${LEAF_UPLOAD_STORE:-legacy}" in app
    assert "LEAF_GUEST_CAP_HMAC_SECRET: ${LEAF_GUEST_CAP_HMAC_SECRET:-}" in app
    assert "DATABASE_URL: ${DATABASE_URL:-}" in app

    assert "LEAF_BROKER_STORE: ${LEAF_BROKER_STORE:-legacy}" in broker
    assert "LEAF_CALLBACK_REPLAY_STORE: ${LEAF_CALLBACK_REPLAY_STORE:-legacy}" in broker
    assert "LEAF_JOBS_STORE: ${LEAF_JOBS_STORE:-legacy}" in broker
    assert "LEAF_DRAWING_STORE: ${LEAF_DRAWING_STORE:-legacy}" in broker
    assert "DATABASE_URL: ${LEAF_BROKER_DATABASE_URL:-}" in broker
    assert "LEAF_GRANTS_DIR" not in broker
    assert "LEAF_GRANT_FILE" not in broker

    assert (
        "LEAF_HARNESS_SESSION_STORE: "
        "${LEAF_HARNESS_SESSION_STORE:-file}"
    ) in harness
    assert "LEAF_HARNESS_DATABASE_URL: ${LEAF_HARNESS_DATABASE_URL:-}" in harness
    assert "LEAF_HARNESS_AUTHORING_MODE: ${LEAF_HARNESS_AUTHORING_MODE:-disabled}" in harness


def test_opt_in_overlay_runs_migration_before_services_without_startup_ddl():
    overlay = _read("docker-compose.canonical.yml")
    migrate = _service(overlay, "migrate")

    assert "db.apply_migration(); db.assert_schema_current()" in migrate
    for name in ("app", "broker", "harness"):
        service = _service(overlay, name)
        assert "migrate:" in service
        assert "condition: service_completed_successfully" in service

    assert "LEAF_BROKER_STORE: ${LEAF_BROKER_STORE:-legacy}" in _service(
        overlay, "broker"
    )
    assert "LEAF_CALLBACK_REPLAY_STORE: ${LEAF_CALLBACK_REPLAY_STORE:-legacy}" in _service(
        overlay, "broker"
    )
    assert "LEAF_JOBS_STORE: ${LEAF_JOBS_STORE:-legacy}" in _service(
        overlay, "broker"
    )
    assert "LEAF_DRAWING_STORE: ${LEAF_DRAWING_STORE:-legacy}" in _service(
        overlay, "broker"
    )
    assert "LEAF_HARNESS_SESSION_STORE: ${LEAF_HARNESS_SESSION_STORE:-file}" in _service(
        overlay, "harness"
    )
    assert "LEAF_HARNESS_AUTHORING_MODE: ${LEAF_HARNESS_AUTHORING_MODE:-disabled}" in _service(
        overlay, "harness"
    )
    assert "LEAF_SESSIONS_STORE: ${LEAF_SESSIONS_STORE:-legacy}" in _service(
        overlay, "app"
    )
    assert "LEAF_JOBS_STORE: ${LEAF_JOBS_STORE:-legacy}" in _service(
        overlay, "app"
    )
    assert "LEAF_AGENT_STORE: ${LEAF_AGENT_STORE:-legacy}" in _service(
        overlay, "app"
    )
    assert "LEAF_GUEST_CAP_STORE: ${LEAF_GUEST_CAP_STORE:-memory}" in _service(
        overlay, "app"
    )
    assert "LEAF_DRAWING_STORE: ${LEAF_DRAWING_STORE:-legacy}" in _service(
        overlay, "app"
    )
    assert "LEAF_UPLOAD_STORE: ${LEAF_UPLOAD_STORE:-legacy}" in _service(
        overlay, "app"
    )

    for path in (
        "deploy/Dockerfile.app",
        "deploy/Dockerfile.broker",
        "deploy/Dockerfile.harness",
    ):
        dockerfile = _read(path)
        assert "apply_migration" not in dockerfile
        assert "CREATE TABLE" not in dockerfile
