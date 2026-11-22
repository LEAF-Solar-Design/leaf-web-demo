"""Published artifact transport uses the real app and never verifies signatures."""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

import deps
import tool_envelopes as catalog

BASE = "/api/tool-envelopes"
CACHE = "private, max-age=300"
ZIP = b"PK\x05\x06" + b"\x00" * 18
TENANT_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
TENANT_B = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"


def audience(tenants=None):
    return {"schema": "leaf.tool-audience.v1", "tenants": [TENANT_A] if tenants is None else tenants}


def envelope(name="solar-tool", version="1.2.3"):
    return {"schema": "leaf.tool-envelope.v1",
            "package": {"name": name, "version": version, "package_digest": "a" * 64},
            "tiers": ["hosted_pro"], "signed_at": "2026-10-29T12:00:00Z",
            "key_id": "b" * 16, "alg": "ECDSA_P256_SHA256", "signature": "c" * 140}


def publish(root, name="solar-tool", version="1.2.3", value=None):
    directory = root / name / version
    directory.mkdir(parents=True)
    raw = (json.dumps(envelope(name, version) if value is None else value, indent=2) + "\n").encode()
    (directory / "envelope.json").write_bytes(raw)
    (directory / "package.zip").write_bytes(ZIP)
    (directory / "audience.json").write_text(json.dumps(audience()))
    return raw


def revocations():
    return {"schema": "leaf.tool-revocations.v1", "issued_at": "2026-10-29T12:00:00Z",
            "key_id": "b" * 16, "alg": "ECDSA_P256_SHA256", "entries": [],
            "signature": "c" * 140}


@pytest.fixture
def client(tmp_path, monkeypatch):
    from app import app

    monkeypatch.setenv("LEAF_PUBLISHED_TOOLS_DIR", str(tmp_path))
    monkeypatch.setenv("LEAF_AUTH_LIVE", "1")
    monkeypatch.setattr(app, "dependency_overrides", {
        deps.require_active_tenant: lambda: UUID(TENANT_A),
    })
    catalog._CACHE.clear()
    # No lifespan is needed for this read-only router, matching neighboring tests.
    yield TestClient(app)
    catalog._CACHE.clear()


def test_list_is_sorted(client, tmp_path):
    for name, version in [("zebra-tool", "2.0.0"), ("alpha-tool", "2.0.0"), ("alpha-tool", "1.0.0")]:
        publish(tmp_path, name, version)
    response = client.get(BASE)
    assert response.status_code == 200
    assert response.json() == {"tools": [
        {"name": name, "version": version, "package_digest": "a" * 64,
         "tiers": ["hosted_pro"], "signed_at": "2026-10-29T12:00:00Z"}
        for name, version in [("alpha-tool", "1.0.0"), ("alpha-tool", "2.0.0"), ("zebra-tool", "2.0.0")]
    ]}


def test_empty_root(client):
    assert client.get(BASE).json() == {"tools": []}


def test_envelope_bytes(client, tmp_path):
    raw = publish(tmp_path)
    response = client.get(BASE + "/solar-tool/1.2.3/envelope")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    assert response.content == raw


def test_package_bytes(client, tmp_path):
    publish(tmp_path)
    response = client.get(BASE + "/solar-tool/1.2.3/package")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"
    assert response.content == ZIP


def test_unknown_version(client, tmp_path):
    publish(tmp_path)
    for resource in ("envelope", "package"):
        response = client.get(BASE + "/solar-tool/9.0.0/" + resource)
        assert response.status_code == 404
        assert response.json() == {"detail": "tool_not_published"}


def test_invalid_identity_never_accesses_catalog(client, tmp_path, monkeypatch):
    # This name would escape the root if joined without validation. Encoding the
    # slash prevents the HTTP client from normalizing away the traversal first.
    def forbidden(*args, **kwargs):
        pytest.fail("invalid identity accessed the filesystem/catalog")

    monkeypatch.setattr(catalog, "published_catalog", forbidden)
    monkeypatch.setattr(Path, "open", forbidden)
    for identity in ("..%2Fx/1.2.3", "Bad_Name/1.2.3", "solar-tool/1.2", "solar-tool/1.2.3-rc1"):
        for resource in ("envelope", "package"):
            response = client.get(BASE + "/" + identity + "/" + resource)
            assert response.status_code == 400, response.text
            assert response.json() == {"detail": "invalid_tool_identity"}


def test_skips_invalid_entries(client, tmp_path, caplog):
    publish(tmp_path, "good-tool")
    for name, case in [("wrong-schema", "schema"), ("extra-key", "extra"), ("wrong-name", "name"),
                       ("missing-zip", "missing"), ("large-json", "large"), ("large-zip", "zip"),
                       ("invalid-json", "json")]:
        value = envelope(name)
        if case == "schema":
            value["schema"] = "private-invalid-schema-marker"
        elif case == "extra":
            value["extra"] = True
        elif case == "name":
            value["package"]["name"] = "other-tool"
        publish(tmp_path, name, value=value)
        directory = tmp_path / name / "1.2.3"
        if case == "missing":
            (directory / "package.zip").unlink()
        elif case == "large":
            (directory / "envelope.json").write_bytes(b" " * (64 * 1024 + 1))
        elif case == "zip":
            with (directory / "package.zip").open("wb") as stream:
                stream.truncate(8 * 1024 * 1024 + 1)
        elif case == "json":
            (directory / "envelope.json").write_bytes(b"{")
    publish(tmp_path, "Bad_Name")
    publish(tmp_path, "bad-version", "1.2.3-rc1")
    assert [tool["name"] for tool in client.get(BASE).json()["tools"]] == ["good-tool"]
    warnings = [record for record in caplog.records if record.name == catalog.__name__]
    assert len(warnings) == 9
    assert all(record.levelname == "WARNING" for record in warnings)
    assert "private-invalid-schema-marker" not in caplog.text
    client.get(BASE)
    assert len([record for record in caplog.records if record.name == catalog.__name__]) == 9


def test_revocations_present(client, tmp_path):
    raw = (json.dumps(revocations(), indent=2) + "\n").encode()
    (tmp_path / "revocations.json").write_bytes(raw)
    response = client.get(BASE + "/revocations")
    assert response.status_code == 200
    assert response.content == raw
    assert response.headers["content-type"] == "application/json"


def test_revocations_absent(client):
    response = client.get(BASE + "/revocations")
    assert response.status_code == 404
    assert response.json() == {"detail": "no_revocation_list"}


def test_revocations_invalid(client, tmp_path):
    for raw in (b"{", b'{"schema":"wrong"}', b" " * (1024 * 1024 + 1)):
        (tmp_path / "revocations.json").write_bytes(raw)
        catalog._CACHE.clear()
        response = client.get(BASE + "/revocations")
        assert response.status_code == 404
        assert response.json() == {"detail": "no_revocation_list"}


def test_all_routes_require_auth(client, tmp_path, monkeypatch):
    publish(tmp_path)
    (tmp_path / "revocations.json").write_text(json.dumps(revocations()))
    monkeypatch.setattr(client.app, "dependency_overrides", {})
    for suffix in ("", "/revocations", "/solar-tool/1.2.3/envelope", "/solar-tool/1.2.3/package"):
        assert client.get(BASE + suffix).status_code in (401, 403)


def test_success_cache_control(client, tmp_path):
    publish(tmp_path)
    (tmp_path / "revocations.json").write_text(json.dumps(revocations()))
    for suffix in ("", "/revocations", "/solar-tool/1.2.3/envelope", "/solar-tool/1.2.3/package"):
        response = client.get(BASE + suffix)
        assert response.status_code == 200
        assert response.headers["cache-control"] == CACHE


def test_catalog_cache_is_bounded_and_reuses_root(client, tmp_path):
    first = catalog.load_catalog(tmp_path)
    assert first is catalog.load_catalog(str(tmp_path))
    publish(tmp_path)
    assert first.list(TENANT_A) == []  # Catalogs are loaded once per cached root.
    for number in range(5):
        root = tmp_path / str(number)
        root.mkdir()
        catalog.load_catalog(root)
    assert len(catalog._CACHE) == 4


def test_envelope_shape_validation(client, tmp_path):
    mutations = [
        ("tiers", []), ("tiers", ["pro", "pro"]), ("tiers", ["Bad"]),
        ("tiers", [{}]), ("signed_at", "2026-02-30T12:00:00Z"),
        ("signed_at", "2026-10-29T12:00:00+00:00"), ("key_id", "B" * 16),
        ("alg", "other"), ("signature", "c" * 139), ("signature", "C" * 140),
        ("package", {"name": "solar-tool", "version": "1.2.3", "package_digest": "A" * 64}),
    ]
    for key, value in mutations:
        body = deepcopy(envelope())
        body[key] = value
        assert not catalog._envelope(body, "solar-tool", "1.2.3")
    assert catalog._envelope(envelope(), "solar-tool", "1.2.3")


def test_tenant_audience_filters_all_reads(client, tmp_path, monkeypatch):
    raw = publish(tmp_path)
    loaded = catalog.load_catalog(tmp_path)
    assert len(loaded.list(TENANT_A)) == 1
    assert loaded.envelope_bytes("solar-tool", "1.2.3", TENANT_A) == raw
    assert loaded.package_path("solar-tool", "1.2.3", TENANT_A) == tmp_path / "solar-tool/1.2.3/package.zip"
    assert loaded.list(TENANT_B) == []
    assert loaded.envelope_bytes("solar-tool", "1.2.3", TENANT_B) is None
    assert loaded.package_path("solar-tool", "1.2.3", TENANT_B) is None
    assert len(client.get(BASE).json()["tools"]) == 1
    assert client.get(BASE + "/solar-tool/1.2.3/envelope").content == raw
    assert client.get(BASE + "/solar-tool/1.2.3/package").content == ZIP
    monkeypatch.setitem(client.app.dependency_overrides, deps.require_active_tenant, lambda: UUID(TENANT_B))
    response = client.get(BASE)
    assert response.status_code == 200
    assert response.json() == {"tools": []}
    assert response.headers["cache-control"] == CACHE
    for resource in ("envelope", "package"):
        hidden = client.get(BASE + "/solar-tool/1.2.3/" + resource)
        absent = client.get(BASE + "/absent-tool/1.2.3/" + resource)
        assert hidden.status_code == absent.status_code == 404
        assert hidden.content == absent.content
        assert hidden.json() == {"detail": "tool_not_published"}
        assert hidden.headers == absent.headers
        assert hidden.headers["cache-control"] == CACHE


@pytest.mark.parametrize("raw", [
    None,
    b"{",
    json.dumps({**audience(), "schema": "wrong"}).encode(),
    json.dumps({**audience(), "extra": True}).encode(),
    b'{"schema":"leaf.tool-audience.v1","tenants":"internal","tenants":"internal"}',
    json.dumps(audience()).encode() + b" " * 4096,
    json.dumps(audience(["not-a-uuid"])).encode(),
    json.dumps(audience([TENANT_A.upper()])).encode(),
    json.dumps(audience([TENANT_A.replace("-", "")])).encode(),
    json.dumps(audience([])).encode(),
    json.dumps(audience([TENANT_A, TENANT_A])).encode(),
    json.dumps(audience([123])).encode(),
    json.dumps(audience("all")).encode(),
    b'{"schema":"leaf.tool-audience.v1","tenants":[NaN]}',
    b'{"schema":"leaf.tool-audience.v1","tenants":[Infinity]}',
    b'{"schema":"leaf.tool-audience.v1","tenants":[-Infinity]}',
    b'{"schema":"leaf.tool-audience.v1","tenants":["\xff"]}',
])
def test_invalid_audience_is_hidden(client, tmp_path, monkeypatch, caplog, raw):
    publish(tmp_path)
    path = tmp_path / "solar-tool/1.2.3/audience.json"
    if raw is None:
        path.unlink()
    else:
        path.write_bytes(raw)
    loaded = catalog.load_catalog(tmp_path)
    for tenant in (TENANT_A, TENANT_B):
        assert loaded.list(tenant) == []
        assert loaded.envelope_bytes("solar-tool", "1.2.3", tenant) is None
        assert loaded.package_path("solar-tool", "1.2.3", tenant) is None
        monkeypatch.setitem(client.app.dependency_overrides, deps.require_active_tenant, lambda: UUID(tenant))
        assert client.get(BASE).json() == {"tools": []}
        for resource in ("envelope", "package"):
            hidden = client.get(BASE + "/solar-tool/1.2.3/" + resource)
            absent = client.get(BASE + "/absent-tool/1.2.3/" + resource)
            assert hidden.status_code == absent.status_code == 404
            assert hidden.content == absent.content
            assert hidden.headers == absent.headers
            assert hidden.headers["cache-control"] == CACHE
    warnings = [record for record in caplog.records if record.name == catalog.__name__]
    assert len(warnings) == 1
    assert warnings[0].levelname == "WARNING"


def test_internal_audience_follows_env_on_cached_catalog(client, tmp_path, monkeypatch):
    raw = publish(tmp_path)
    (tmp_path / "solar-tool/1.2.3/audience.json").write_text(json.dumps(audience("internal")))
    loaded = catalog.load_catalog(tmp_path)
    for allowed, hidden in ((TENANT_A, TENANT_B), (TENANT_B, TENANT_A)):
        monkeypatch.setenv("LEAF_CUSTOMIZATION_INTERNAL_TENANTS", " " + allowed + ", ")
        assert catalog.load_catalog(tmp_path) is loaded
        assert len(loaded.list(allowed)) == 1
        assert loaded.envelope_bytes("solar-tool", "1.2.3", allowed) == raw
        assert loaded.package_path("solar-tool", "1.2.3", allowed) is not None
        assert loaded.list(hidden) == []
        assert loaded.envelope_bytes("solar-tool", "1.2.3", hidden) is None
        assert loaded.package_path("solar-tool", "1.2.3", hidden) is None
        for tenant in (allowed, hidden):
            monkeypatch.setitem(client.app.dependency_overrides, deps.require_active_tenant, lambda: UUID(tenant))
            response = client.get(BASE)
            assert bool(response.json()["tools"]) == (tenant == allowed)
            assert response.headers["cache-control"] == CACHE
            for resource in ("envelope", "package"):
                response = client.get(BASE + "/solar-tool/1.2.3/" + resource)
                assert response.status_code == (200 if tenant == allowed else 404)
                assert response.headers["cache-control"] == CACHE
    monkeypatch.delenv("LEAF_CUSTOMIZATION_INTERNAL_TENANTS")
    assert loaded.list(TENANT_B) == []


def test_explicit_audience_is_shared_and_parsed_once(client, tmp_path):
    raw = publish(tmp_path)
    path = tmp_path / "solar-tool/1.2.3/audience.json"
    body = json.dumps(audience([TENANT_A, TENANT_B])).encode()
    path.write_bytes(body + b" " * (4096 - len(body)))
    loaded = catalog.load_catalog(tmp_path)
    path.unlink()
    for tenant in (TENANT_A, TENANT_B):
        assert len(loaded.list(tenant)) == 1
        assert loaded.envelope_bytes("solar-tool", "1.2.3", tenant) == raw
        assert loaded.package_path("solar-tool", "1.2.3", tenant) is not None


def test_unreadable_audience_is_hidden(client, tmp_path, monkeypatch, caplog):
    publish(tmp_path)
    read = catalog._read

    def unreadable(path, limit):
        if path.name == "audience.json":
            raise PermissionError("audience unavailable")
        return read(path, limit)

    monkeypatch.setattr(catalog, "_read", unreadable)
    loaded = catalog.load_catalog(tmp_path)
    for tenant in (TENANT_A, TENANT_B):
        assert loaded.list(tenant) == []
        assert loaded.envelope_bytes("solar-tool", "1.2.3", tenant) is None
        assert loaded.package_path("solar-tool", "1.2.3", tenant) is None
    assert len([record for record in caplog.records if record.name == catalog.__name__]) == 1


def test_revocations_are_global(client, tmp_path, monkeypatch):
    publish(tmp_path)
    value = revocations()
    value["entries"] = [{"package_digest": "a" * 64, "name": "solar-tool", "version": "1.2.3",
                         "reason": "withdrawn", "revoked_at": "2026-10-29T12:00:00Z"}]
    raw = json.dumps(value).encode()
    (tmp_path / "revocations.json").write_bytes(raw)
    for tenant in (TENANT_A, TENANT_B):
        monkeypatch.setitem(client.app.dependency_overrides, deps.require_active_tenant, lambda: UUID(tenant))
        response = client.get(BASE + "/revocations")
        assert response.status_code == 200
        assert response.content == raw
        assert response.headers["cache-control"] == CACHE
