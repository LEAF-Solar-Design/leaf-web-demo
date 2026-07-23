"""Static checks for staged PostgreSQL container wiring.

These checks do not need a Docker daemon or a live database. They protect the
legacy defaults and process trust boundaries that must hold before an operator
can run the separate migration and cutover stages.
"""
from pathlib import Path
import re


REPO_ROOT = Path(__file__).resolve().parents[2]


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
