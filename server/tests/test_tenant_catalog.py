"""Tenant catalog isolation, immutable publication, revocation, and rollout gates."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from uuid import UUID

from fastapi.testclient import TestClient
import pytest

from branch_tools import tenant_catalog as catalog
import deps
import tool_envelopes

TENANT = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
OTHER = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
BASE = "/api/tool-envelopes/tenant"
NAME = "solar-tool"
VERSION = "1.2.3"
PACKAGE = b"PK\x05\x06" + b"\x00" * 18
SUFFIXES = ("", "/revocations", f"/{NAME}/{VERSION}/envelope", f"/{NAME}/{VERSION}/package")


def content_digest(package):
    line = f"payload.txt\t{len(package)}\t{hashlib.sha256(package).hexdigest()}\n"
    return hashlib.sha256(line.encode()).hexdigest()


def envelope(tenant=TENANT, package=PACKAGE, name=NAME, version=VERSION):
    return {"schema": "leaf.tool-envelope.v2", "audience_tenant": tenant,
            "package": {"name": name, "version": version,
                        "package_digest": content_digest(package)},
            "tiers": ["hosted_pro"], "signed_at": "2026-11-22T12:00:00Z",
            "key_id": "b" * 16, "alg": "ECDSA_P256_SHA256", "signature": "c" * 140}


def publish(root, tenant=TENANT, package=PACKAGE, value=None, name=NAME, version=VERSION):
    raw = json.dumps(envelope(tenant, package, name, version) if value is None else value).encode()
    entry = catalog.write_version(root, tenant, name, version, package, raw, content_digest(package))
    return entry, raw


def revocations(digest=None):
    entries = [] if digest is None else [{"package_digest": digest, "name": NAME, "version": VERSION,
                                         "reason": "withdrawn", "revoked_at": "2026-11-22T12:00:00Z"}]
    return json.dumps({"schema": "leaf.tool-revocations.v1", "issued_at": "2026-11-22T12:00:00Z",
                       "key_id": "b" * 16, "alg": "ECDSA_P256_SHA256", "entries": entries,
                       "signature": "c" * 140}).encode()


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
    return TestClient(app)


def test_tenants_have_independent_versions(client, tmp_path, monkeypatch):
    first, raw = publish(tmp_path)
    other, other_raw = publish(tmp_path, OTHER, b"other package")
    assert first.path != other.path
    assert first.package_digest == content_digest(PACKAGE)
    assert first.package_digest != hashlib.sha256(PACKAGE).hexdigest()
    for tenant, package, expected_raw in ((TENANT, PACKAGE, raw), (OTHER, b"other package", other_raw)):
        monkeypatch.setitem(client.app.dependency_overrides, deps.require_active_tenant, lambda: UUID(tenant))
        response = client.get(BASE)
        assert response.status_code == 200
        assert response.json() == {"tools": [{"name": NAME, "version": VERSION,
                                            "package_digest": content_digest(package),
                                            "signed_at": "2026-11-22T12:00:00Z"}]}
        assert client.get(BASE + f"/{NAME}/{VERSION}/envelope").content == expected_raw
        assert client.get(BASE + f"/{NAME}/{VERSION}/package").content == package


def test_other_tenant_is_hidden_including_crafted_names(client, tmp_path, monkeypatch):
    publish(tmp_path, OTHER)
    assert client.get(BASE).json() == {"tools": []}
    for name in (NAME, f"..%2F{OTHER}%2F{NAME}", f"..%5C{OTHER}%5C{NAME}"):
        for resource in ("envelope", "package"):
            response = client.get(BASE + f"/{name}/{VERSION}/{resource}")
            assert response.status_code == 404
            assert response.json() == {"detail": "tool_not_published"}
            assert response.headers["cache-control"] == "no-store"

    def forbidden(*args, **kwargs):
        pytest.fail("invalid identity reached the filesystem")

    monkeypatch.setattr(Path, "open", forbidden)
    for resource in ("envelope", "package"):
        assert client.get(BASE + f"/..%2F{OTHER}/{VERSION}/{resource}").status_code == 404


def test_duplicate_write_preserves_original(tmp_path):
    entry, raw = publish(tmp_path)
    with pytest.raises(catalog.TenantCatalogError, match="^version_exists$"):
        publish(tmp_path, package=b"replacement")
    assert (entry.path / "package.zip").read_bytes() == PACKAGE
    assert (entry.path / "envelope.json").read_bytes() == raw
    assert (entry.path / "digest.txt").read_text().strip() == entry.package_digest
    assert not list((tmp_path / TENANT).glob(".publish-*"))


def test_empty_existing_version_is_immutable(tmp_path):
    directory = tmp_path / TENANT / NAME / VERSION
    directory.mkdir(parents=True)
    with pytest.raises(catalog.TenantCatalogError, match="^version_exists$"):
        publish(tmp_path)
    assert list(directory.iterdir()) == []


def test_failed_writes_do_not_expose_partial_state(tmp_path, monkeypatch):
    old = revocations()
    catalog.write_revocations(tmp_path, TENANT, old)

    def failed(*args):
        raise OSError("disk failure")

    monkeypatch.setattr(catalog, "_write", failed)
    with pytest.raises(OSError, match="disk failure"):
        publish(tmp_path)
    assert not (tmp_path / TENANT / NAME / VERSION).exists()
    assert catalog.list_versions(tmp_path, TENANT) == []
    with pytest.raises(OSError, match="disk failure"):
        catalog.write_revocations(tmp_path, TENANT, revocations("a" * 64))
    assert catalog.revocations_bytes(tmp_path, TENANT) == old
    assert not list((tmp_path / TENANT).glob(".publish-*"))
    assert not list((tmp_path / TENANT).glob(".revocations-*"))


def test_concurrent_writers_leave_one_complete_version(tmp_path):
    def write(package):
        try:
            return publish(tmp_path, package=package)[0]
        except catalog.TenantCatalogError as exc:
            assert exc.code == "version_exists"
            return None

    packages = [b"first", b"second"]
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(write, packages))
    winner = next(entry for entry in results if entry is not None)
    assert sum(entry is not None for entry in results) == 1
    package = (winner.path / "package.zip").read_bytes()
    assert content_digest(package) == winner.package_digest
    assert (winner.path / "package.sha256").read_text().strip() == hashlib.sha256(package).hexdigest()
    assert len(catalog.list_versions(tmp_path, TENANT)) == 1


def test_revocations_are_tenant_local_and_immediate(client, tmp_path, monkeypatch):
    entry, _ = publish(tmp_path)
    publish(tmp_path, OTHER)
    assert len(client.get(BASE).json()["tools"]) == 1
    raw = revocations(entry.package_digest)
    catalog.write_revocations(tmp_path, TENANT, raw)
    assert client.get(BASE).json() == {"tools": []}
    assert client.get(BASE + "/revocations").content == raw
    for resource in ("envelope", "package"):
        response = client.get(BASE + f"/{NAME}/{VERSION}/{resource}")
        assert response.status_code == 404
        assert response.json() == {"detail": "tool_not_published"}
    monkeypatch.setitem(client.app.dependency_overrides, deps.require_active_tenant, lambda: UUID(OTHER))
    assert len(client.get(BASE).json()["tools"]) == 1
    assert client.get(BASE + f"/{NAME}/{VERSION}/package").content == PACKAGE
    assert client.get(BASE + "/revocations").json() == {"detail": "no_revocation_list"}
    catalog.write_revocations(tmp_path, TENANT, revocations())
    assert len(catalog.list_versions(tmp_path, TENANT)) == 1


@pytest.mark.parametrize("setting,value", [
    ("LEAF_PLUGIN_BUILDER", None), ("LEAF_PLUGIN_BUILDER", "0"),
    ("LEAF_PLUGIN_BUILDER", "true"), ("LEAF_TENANT_TOOLS_DIR", None),
    ("LEAF_TENANT_TOOLS_DIR", ""), ("LEAF_PLUGIN_BUILDER_TENANTS", OTHER),
])
def test_closed_gate_all_routes(client, tmp_path, monkeypatch, setting, value):
    publish(tmp_path)
    catalog.write_revocations(tmp_path, TENANT, revocations())
    if value is None:
        monkeypatch.delenv(setting)
    else:
        monkeypatch.setenv(setting, value)
    for suffix in SUFFIXES:
        response = client.get(BASE + suffix)
        assert response.status_code == 404
        assert response.json() == {"detail": "tool_not_published"}
        assert response.headers["cache-control"] == "no-store"


def test_internal_tenant_is_invited(client, tmp_path, monkeypatch):
    publish(tmp_path)
    monkeypatch.delenv("LEAF_PLUGIN_BUILDER_TENANTS")
    monkeypatch.setenv("LEAF_CUSTOMIZATION_INTERNAL_TENANTS", f" {TENANT}, ")
    assert len(client.get(BASE).json()["tools"]) == 1


def test_tenant_routes_precede_published_catch_all(client, tmp_path, monkeypatch):
    from routers import tools

    def forbidden(*args, **kwargs):
        pytest.fail("tenant request reached published catalog")

    monkeypatch.setattr(tool_envelopes, "published_catalog", forbidden)
    publish(tmp_path)
    catalog.write_revocations(tmp_path, TENANT, revocations())
    assert client.get(BASE).status_code == 200
    routes = [route.endpoint for route in tools.router.routes]
    for handler in (tools.tenant_tools, tools.tenant_revocations, tools.tenant_envelope, tools.tenant_package):
        assert routes.index(handler) < routes.index(tools.published_envelope)
        assert routes.index(handler) < routes.index(tools.published_package)
    for suffix in SUFFIXES:
        response = client.get(BASE + suffix)
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"


def test_routes_require_active_tenant(client, monkeypatch):
    monkeypatch.setenv("LEAF_AUTH_LIVE", "1")
    monkeypatch.setattr(client.app, "dependency_overrides", {})
    for suffix in SUFFIXES:
        assert client.get(BASE + suffix).status_code in (401, 403)


@pytest.mark.parametrize("tenant", ["../escape", TENANT.upper(), TENANT.replace("-", ""), "invalid"])
def test_invalid_tenant_never_joins_root(tmp_path, tenant):
    with pytest.raises(catalog.TenantCatalogError, match="^invalid_tenant$"):
        publish(tmp_path, tenant)
    assert catalog.list_versions(tmp_path, tenant) == []
    assert catalog.envelope_bytes(tmp_path, tenant, NAME, VERSION) is None
    assert catalog.package_path(tmp_path, tenant, NAME, VERSION) is None
    assert catalog.revocations_bytes(tmp_path, tenant) is None
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("name,version", [("../escape", VERSION), ("Bad_Name", VERSION), (NAME, "1.2")])
def test_invalid_identity_never_writes(tmp_path, name, version):
    with pytest.raises(catalog.TenantCatalogError, match="^invalid_identity$"):
        publish(tmp_path, name=name, version=version)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("target", ["package", "envelope", "revocations"])
def test_write_limits(tmp_path, target):
    with pytest.raises(catalog.TenantCatalogError, match="^size_limit$"):
        if target == "revocations":
            catalog.write_revocations(tmp_path, TENANT, b" " * (catalog.REVOCATIONS_LIMIT + 1))
        else:
            catalog.write_version(tmp_path, TENANT, NAME, VERSION,
                                  b" " * (catalog.PACKAGE_LIMIT + 1) if target == "package" else PACKAGE,
                                  b" " * (catalog.ENVELOPE_LIMIT + 1) if target == "envelope" else b"{}",
                                  content_digest(PACKAGE))
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("mutation", ["audience", "schema", "extra", "time", "tiers", "digest", "duplicate"])
def test_invalid_envelope_hidden_on_every_read(tmp_path, mutation):
    value = deepcopy(envelope())
    if mutation == "audience":
        value["audience_tenant"] = OTHER
    elif mutation == "schema":
        value["schema"] = "unknown"
    elif mutation == "extra":
        value["extra"] = True
    elif mutation == "time":
        value["signed_at"] = "2026-02-30T12:00:00Z"
    elif mutation == "tiers":
        value["tiers"] = ["pro", "pro"]
    elif mutation == "digest":
        value["package"]["package_digest"] = "f" * 64
    entry, raw = publish(tmp_path, value=value)
    if mutation == "duplicate":
        (entry.path / "envelope.json").write_bytes(raw[:-1] + b',"audience_tenant":"' + OTHER.encode() + b'"}')
    assert catalog.list_versions(tmp_path, TENANT) == []
    assert catalog.envelope_bytes(tmp_path, TENANT, NAME, VERSION) is None
    assert catalog.package_path(tmp_path, TENANT, NAME, VERSION) is None


def test_v1_envelope_remains_valid(tmp_path):
    value = envelope()
    value["schema"] = "leaf.tool-envelope.v1"
    del value["audience_tenant"]
    publish(tmp_path, value=value)
    assert len(catalog.list_versions(tmp_path, TENANT)) == 1


def test_invalid_revocations_fail_closed(client, tmp_path):
    publish(tmp_path)
    catalog.write_revocations(tmp_path, TENANT, b"{")
    assert client.get(BASE).json() == {"tools": []}
    for suffix in SUFFIXES[1:]:
        assert client.get(BASE + suffix).status_code == 404


@pytest.mark.parametrize("component", ["root", "tenant", "name", "version", "package.zip", "envelope.json", "digest.txt", "package.sha256", "revocations.json"])
def test_symlinks_are_never_followed(tmp_path, component):
    root = tmp_path / "catalog"
    entry, _ = publish(root)
    catalog.write_revocations(root, TENANT, revocations())
    original = {"root": root, "tenant": root / TENANT, "name": entry.path.parent,
                "version": entry.path}.get(component, entry.path / component)
    if component == "revocations.json":
        original = root / TENANT / component
    moved = tmp_path / "moved"
    original.rename(moved)
    try:
        original.symlink_to(moved, target_is_directory=moved.is_dir())
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable for this account")
    assert catalog.list_versions(root, TENANT) == []
    assert catalog.envelope_bytes(root, TENANT, NAME, VERSION) is None
    assert catalog.package_path(root, TENANT, NAME, VERSION) is None
    if component in ("root", "tenant", "name", "version"):
        with pytest.raises(catalog.TenantCatalogError, match="^unsafe_path$"):
            publish(root)
    if component in ("root", "tenant", "revocations.json"):
        with pytest.raises(catalog.TenantCatalogError, match="^unsafe_path$"):
            catalog.write_revocations(root, TENANT, revocations())


@pytest.mark.parametrize("dimension,count,expected", [("names", 201, 200), ("versions", 51, 50)])
def test_enumeration_is_bounded(tmp_path, dimension, count, expected):
    for index in range(count):
        name = f"tool-{index}" if dimension == "names" else NAME
        version = f"1.0.{index}" if dimension == "versions" else VERSION
        directory = tmp_path / TENANT / name / version
        directory.mkdir(parents=True)
        value = envelope(name=name, version=version)
        (directory / "envelope.json").write_text(json.dumps(value))
        (directory / "package.zip").write_bytes(PACKAGE)
        (directory / "digest.txt").write_text(value["package"]["package_digest"])
        (directory / "package.sha256").write_text(hashlib.sha256(PACKAGE).hexdigest())
    entries = catalog.list_versions(tmp_path, TENANT)
    assert len(entries) == expected
    assert entries == sorted(entries, key=lambda entry: (entry["name"], entry["version"]))


@pytest.mark.parametrize("component", ["package.zip", "package.sha256", "digest.txt"])
def test_tampered_package_or_digest_is_hidden(tmp_path, component):
    entry, _ = publish(tmp_path)
    (entry.path / component).write_bytes(b"f" * 64)
    assert catalog.list_versions(tmp_path, TENANT) == []
    assert catalog.envelope_bytes(tmp_path, TENANT, NAME, VERSION) is None
    assert catalog.package_path(tmp_path, TENANT, NAME, VERSION) is None
