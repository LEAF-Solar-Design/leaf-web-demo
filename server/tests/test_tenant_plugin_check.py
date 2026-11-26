"""Best-effort, tenant-local AutoCAD check stamps and workspace status."""
from datetime import datetime, timedelta, timezone
import json
from uuid import UUID

from fastapi.testclient import TestClient
import pytest

from branch_tools import tenant_catalog as catalog
import deps


TENANT = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
OTHER = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
BASE = "/api/tool-envelopes/tenant"
NOW = datetime(2026, 11, 25, 12, 34, 56, 123456, tzinfo=timezone.utc)
STAMP = "2026-11-25T12:34:56Z"


@pytest.fixture(autouse=True)
def empty_cache():
    with catalog._PLUGIN_CHECK_LOCK:
        catalog._PLUGIN_CHECK_CACHE.clear()
    yield
    with catalog._PLUGIN_CHECK_LOCK:
        catalog._PLUGIN_CHECK_CACHE.clear()


@pytest.fixture
def client(tmp_path, monkeypatch):
    from app import app

    monkeypatch.setenv("LEAF_PLUGIN_BUILDER", "1")
    monkeypatch.setenv("LEAF_TENANT_TOOLS_DIR", str(tmp_path))
    monkeypatch.setenv("LEAF_PLUGIN_BUILDER_TENANTS", TENANT + ", " + OTHER)
    monkeypatch.delenv("LEAF_CUSTOMIZATION_INTERNAL_TENANTS", raising=False)
    monkeypatch.setattr(app, "dependency_overrides", {
        deps.require_active_tenant: lambda: UUID(TENANT),
    })
    monkeypatch.setattr(deps, "workspace_view", lambda tenant: {"name": "Leaf Automation"})
    return TestClient(app)


def test_first_record_is_seconds_precision_utc_and_not_a_tool(tmp_path):
    assert catalog.record_plugin_check(tmp_path, TENANT, NOW) is True
    path = tmp_path / TENANT / catalog.PLUGIN_CHECK_FILE
    assert json.loads(path.read_bytes()) == {"last_plugin_check_at": STAMP}
    assert catalog.plugin_check_at(tmp_path, TENANT) == STAMP
    assert catalog.list_versions(tmp_path, TENANT) == []
    assert sorted(p.name for p in path.parent.iterdir()) == [catalog.PLUGIN_CHECK_FILE]


def test_cache_throttles_without_file_access_and_later_rewrites(tmp_path, monkeypatch):
    assert catalog.record_plugin_check(tmp_path, TENANT, NOW) is True
    path = tmp_path / TENANT / catalog.PLUGIN_CHECK_FILE
    before = path.read_bytes(), path.stat().st_mtime_ns

    def forbidden(*args):
        pytest.fail("fresh cache must avoid filesystem access")

    with monkeypatch.context() as patch:
        patch.setattr(catalog, "_tenant", forbidden)
        assert catalog.record_plugin_check(tmp_path, TENANT, NOW + timedelta(seconds=10)) is False
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before
    assert catalog.record_plugin_check(tmp_path, TENANT, NOW + timedelta(seconds=301)) is True
    assert catalog.plugin_check_at(tmp_path, TENANT) == "2026-11-25T12:39:57Z"


def test_fresh_file_repopulates_empty_cache_without_writing(tmp_path, monkeypatch):
    assert catalog.record_plugin_check(tmp_path, TENANT, NOW) is True
    path = tmp_path / TENANT / catalog.PLUGIN_CHECK_FILE
    before = path.read_bytes(), path.stat().st_mtime_ns
    catalog._PLUGIN_CHECK_CACHE.clear()

    def forbidden(*args, **kwargs):
        pytest.fail("fresh file must not be rewritten")

    monkeypatch.setattr(catalog.tempfile, "mkdtemp", forbidden)
    assert catalog.record_plugin_check(tmp_path, TENANT, NOW + timedelta(seconds=10)) is False
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before
    assert catalog._PLUGIN_CHECK_CACHE[TENANT] == NOW.replace(microsecond=0)


@pytest.mark.parametrize("clear_cache", [False, True])
def test_future_stamp_is_rewritten(tmp_path, clear_cache):
    assert catalog.record_plugin_check(tmp_path, TENANT, NOW + timedelta(days=1)) is True
    if clear_cache:
        catalog._PLUGIN_CHECK_CACHE.clear()
    assert catalog.record_plugin_check(tmp_path, TENANT, NOW) is True
    assert catalog.plugin_check_at(tmp_path, TENANT) == STAMP


@pytest.mark.parametrize("raw", [
    b"{", b" " * (catalog.PLUGIN_CHECK_LIMIT + 1), b"[]", b"{}", b"null",
    b'{"last_plugin_check_at":null}', b'{"last_plugin_check_at":42}',
    b'{"last_plugin_check_at":"2026-11-25T12:34:56+00:00"}',
    b'{"last_plugin_check_at":"2026-11-25T12:34:56.123Z"}',
    b'{"last_plugin_check_at":"2026-1-25T12:34:56Z"}',
    b'{"last_plugin_check_at":"2026-02-30T12:34:56Z"}',
])
def test_invalid_stamp_is_ignored_and_repaired(tmp_path, raw):
    directory = tmp_path / TENANT
    directory.mkdir()
    (directory / catalog.PLUGIN_CHECK_FILE).write_bytes(raw)
    assert catalog.plugin_check_at(tmp_path, TENANT) is None
    assert catalog.record_plugin_check(tmp_path, TENANT, NOW) is True
    assert catalog.plugin_check_at(tmp_path, TENANT) == STAMP


@pytest.mark.parametrize("tenant", ["invalid", "../escape", "/escape", TENANT.upper(),
                                    TENANT.replace("-", "")])
def test_invalid_tenant_writes_nothing(tmp_path, tenant):
    assert catalog.plugin_check_at(tmp_path, tenant) is None
    assert catalog.record_plugin_check(tmp_path, tenant, NOW) is False
    assert list(tmp_path.iterdir()) == []
    assert not catalog._PLUGIN_CHECK_CACHE


def test_tenant_stamps_are_independent(tmp_path):
    assert catalog.plugin_check_at(tmp_path, OTHER) is None
    assert catalog.record_plugin_check(tmp_path, OTHER, NOW - timedelta(days=1)) is True
    other_path = tmp_path / OTHER / catalog.PLUGIN_CHECK_FILE
    before = other_path.read_bytes(), other_path.stat().st_mtime_ns
    assert catalog.record_plugin_check(tmp_path, TENANT, NOW) is True
    assert (other_path.read_bytes(), other_path.stat().st_mtime_ns) == before
    assert catalog.plugin_check_at(tmp_path, OTHER) == "2026-11-24T12:34:56Z"


def test_failed_replace_leaves_no_temporary_or_cached_stamp(tmp_path, monkeypatch):
    def fail(*args):
        raise OSError("disk failure")

    monkeypatch.setattr(catalog.os, "replace", fail)
    assert catalog.record_plugin_check(tmp_path, TENANT, NOW) is False
    assert catalog.plugin_check_at(tmp_path, TENANT) is None
    assert list((tmp_path / TENANT).iterdir()) == []
    assert not catalog._PLUGIN_CHECK_CACHE


def test_cache_is_bounded_and_evicts_oldest_write(tmp_path, monkeypatch):
    monkeypatch.setattr(catalog, "_PLUGIN_CHECK_CACHE_MAX", 3)
    tenants = [str(UUID(int=index + 1)) for index in range(5)]
    for tenant in tenants:
        assert catalog.record_plugin_check(tmp_path, tenant, NOW) is True
    assert list(catalog._PLUGIN_CHECK_CACHE) == tenants[-3:]
    assert catalog.record_plugin_check(tmp_path, tenants[2], NOW + timedelta(seconds=301)) is True
    assert list(catalog._PLUGIN_CHECK_CACHE) == [tenants[3], tenants[4], tenants[2]]


def test_list_stamps_and_status_displays_resolved_workspace(client, tmp_path):
    assert client.get(BASE).json() == {"tools": [], "workspace_id": TENANT}
    stamp = catalog.plugin_check_at(tmp_path, TENANT)
    assert stamp is not None
    response = client.get(BASE + "/status")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {
        "workspace_id": TENANT, "workspace_name": "Leaf Automation",
        "last_plugin_check_at": stamp,
    }
    assert not (tmp_path / OTHER).exists()


@pytest.mark.parametrize("workspace,expected", [({"name": "Leaf Automation"}, "Leaf Automation"),
                                               (None, None)])
def test_status_alone_does_not_stamp(client, tmp_path, monkeypatch, workspace, expected):
    monkeypatch.setattr(deps, "workspace_view", lambda tenant: workspace)
    response = client.get(BASE + "/status")
    assert response.status_code == 200
    assert response.json() == {
        "workspace_id": TENANT, "workspace_name": expected, "last_plugin_check_at": None,
    }
    assert list(tmp_path.iterdir()) == []
    assert not catalog._PLUGIN_CHECK_CACHE


def test_stamp_failure_does_not_change_list_response(client, monkeypatch):
    def fail(*args):
        raise RuntimeError("stamp unavailable")

    monkeypatch.setattr(catalog, "record_plugin_check", fail)
    response = client.get(BASE)
    assert response.status_code == 200
    assert response.json() == {"tools": [], "workspace_id": TENANT}
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("setting,value", [
    ("LEAF_PLUGIN_BUILDER", "0"), ("LEAF_TENANT_TOOLS_DIR", ""),
    ("LEAF_PLUGIN_BUILDER_TENANTS", OTHER),
])
def test_hidden_status_returns_tenant_missing(client, tmp_path, monkeypatch, setting, value):
    monkeypatch.setenv(setting, value)
    response = client.get(BASE + "/status")
    assert response.status_code == 404
    assert response.json() == {"detail": "tool_not_published"}
    assert response.headers["cache-control"] == "no-store"
    assert list(tmp_path.iterdir()) == []
