import pytest

import platform_link


def _enable_required_upload_authorities(monkeypatch):
    monkeypatch.setenv("LEAF_BLOB_STORE", "filesystem")
    monkeypatch.setenv("LEAF_DRAWING_STORE", "postgres")
    monkeypatch.setenv("LEAF_UPLOAD_STORE", "postgres")
    monkeypatch.setenv(
        "LEAF_DRAWING_MUTATIONS_FENCE_FILE", "/data/state/drawing-mutations")


def test_postgres_requirement_defaults_off(monkeypatch):
    monkeypatch.delenv("LEAF_PLATFORM_POSTGRES_REQUIRED", raising=False)
    for name in platform_link._AUTHORITY_SELECTORS:
        monkeypatch.delenv(name, raising=False)
    assert platform_link.postgres_required() is False
    assert platform_link.postgres_startup_required() is False
    assert platform_link.validate_postgres_startup() is None


@pytest.mark.parametrize("value", [None, "0", "false", "off"])
def test_production_app_requires_master_postgres_gate(monkeypatch, value):
    monkeypatch.setenv("LEAF_RUNTIME_ENV", "production")
    if value is None:
        monkeypatch.delenv("LEAF_PLATFORM_POSTGRES_REQUIRED", raising=False)
    else:
        monkeypatch.setenv("LEAF_PLATFORM_POSTGRES_REQUIRED", value)
    with pytest.raises(
        RuntimeError, match="LEAF_PLATFORM_POSTGRES_REQUIRED=1"
    ):
        platform_link.validate_postgres_startup()


def test_required_postgres_rejects_auth_off_before_database_access(monkeypatch):
    monkeypatch.setenv("LEAF_PLATFORM_POSTGRES_REQUIRED", "1")
    _enable_required_upload_authorities(monkeypatch)
    monkeypatch.setenv("LEAF_AUTH_LIVE", "0")
    monkeypatch.setenv("DATABASE_URL", "postgresql://leaf@db.internal/leaf")
    monkeypatch.setattr(
        platform_link, "_load_platform",
        lambda: (_ for _ in ()).throw(AssertionError("database was accessed")))
    with pytest.raises(RuntimeError, match="LEAF_AUTH_LIVE=1"):
        platform_link.validate_postgres_startup()


def test_required_postgres_rejects_missing_environment_url(monkeypatch):
    monkeypatch.setenv("LEAF_PLATFORM_POSTGRES_REQUIRED", "true")
    _enable_required_upload_authorities(monkeypatch)
    monkeypatch.setenv("LEAF_AUTH_LIVE", "1")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(RuntimeError, match="DATABASE_URL in the environment"):
        platform_link.validate_postgres_startup()


def test_required_postgres_checks_current_schema(monkeypatch):
    class FakeDb:
        @staticmethod
        def assert_schema_current():
            return {"ok": True, "migration_count": 10}

    monkeypatch.setenv("LEAF_PLATFORM_POSTGRES_REQUIRED", "yes")
    _enable_required_upload_authorities(monkeypatch)
    monkeypatch.setenv("LEAF_AUTH_LIVE", "true")
    monkeypatch.setenv("DATABASE_URL", "postgresql://leaf@db.internal/leaf")
    monkeypatch.setattr(platform_link, "_load_platform", lambda: (object(), FakeDb(), object()))
    assert platform_link.validate_postgres_startup() == {
        "ok": True, "migration_count": 10}


def test_selected_postgres_authority_requires_url_and_schema_without_live_auth(
        monkeypatch):
    class FakeDb:
        @staticmethod
        def assert_schema_current():
            return {"ok": True, "migration_count": 17}

    monkeypatch.delenv("LEAF_PLATFORM_POSTGRES_REQUIRED", raising=False)
    monkeypatch.setenv("LEAF_AUTH_LIVE", "0")
    monkeypatch.setenv("LEAF_JOBS_STORE", "postgres")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(RuntimeError, match="DATABASE_URL in the environment"):
        platform_link.validate_postgres_startup()

    monkeypatch.setenv("DATABASE_URL", "postgresql://leaf@db.internal/leaf")
    monkeypatch.setattr(platform_link, "_load_platform", lambda: (object(), FakeDb(), object()))
    assert platform_link.validate_postgres_startup() == {
        "ok": True, "migration_count": 17}


def test_invalid_authority_selector_fails_startup(monkeypatch):
    monkeypatch.delenv("LEAF_PLATFORM_POSTGRES_REQUIRED", raising=False)
    monkeypatch.setenv("LEAF_UPLOAD_STORE", "typo")
    with pytest.raises(RuntimeError, match="LEAF_UPLOAD_STORE"):
        platform_link.postgres_startup_required()


def test_required_platform_rejects_legacy_upload_authority(monkeypatch):
    monkeypatch.setenv("LEAF_PLATFORM_POSTGRES_REQUIRED", "1")
    monkeypatch.setenv("LEAF_AUTH_LIVE", "1")
    monkeypatch.setenv("DATABASE_URL", "postgresql://leaf@db.internal/leaf")
    monkeypatch.setenv("LEAF_BLOB_STORE", "filesystem")
    monkeypatch.setenv("LEAF_DRAWING_STORE", "postgres")
    monkeypatch.setenv("LEAF_UPLOAD_STORE", "legacy")

    with pytest.raises(
        RuntimeError,
        match="LEAF_PLATFORM_POSTGRES_REQUIRED requires LEAF_UPLOAD_STORE=postgres",
    ):
        platform_link.validate_postgres_startup()


def test_required_platform_rejects_aps_oss_app_blob_store(monkeypatch):
    monkeypatch.setenv("LEAF_PLATFORM_POSTGRES_REQUIRED", "1")
    monkeypatch.setenv("LEAF_AUTH_LIVE", "1")
    monkeypatch.setenv("DATABASE_URL", "postgresql://leaf@db.internal/leaf")
    monkeypatch.setenv("LEAF_DRAWING_STORE", "postgres")
    monkeypatch.setenv("LEAF_UPLOAD_STORE", "postgres")
    monkeypatch.setenv("LEAF_BLOB_STORE", "aps_oss")

    with pytest.raises(
        RuntimeError,
        match="LEAF_PLATFORM_POSTGRES_REQUIRED requires LEAF_BLOB_STORE=filesystem",
    ):
        platform_link.validate_postgres_startup()


def test_required_platform_rejects_missing_shared_mutation_fence(monkeypatch):
    monkeypatch.setenv("LEAF_PLATFORM_POSTGRES_REQUIRED", "1")
    _enable_required_upload_authorities(monkeypatch)
    monkeypatch.delenv("LEAF_DRAWING_MUTATIONS_FENCE_FILE", raising=False)

    with pytest.raises(RuntimeError, match="LEAF_DRAWING_MUTATIONS_FENCE_FILE"):
        platform_link.validate_postgres_startup()
