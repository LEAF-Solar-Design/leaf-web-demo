"""Internal dispatch reads must name the durable, materialized generation."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
os.environ.setdefault("LEAF_CUSTOMIZATION_STAGE_WORKER_DISABLED", "1")
os.environ.setdefault("LEAF_GUEST_PURGE_DISABLED", "1")
import customization_service as service
from customization_models import ChangeSetNotFoundError
from routers import author


@pytest.fixture
def catalog(tmp_path, monkeypatch):
    monkeypatch.setenv("LEAF_APP_DISPATCH_SECRET", "test-dispatch")
    monkeypatch.setenv("LEAF_AUTH_LIVE", "1")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    source = tmp_path / "source"
    source.mkdir()

    def git(cwd, *args):
        return subprocess.run(["git", "-c", "core.autocrlf=false", *args], cwd=cwd,
                              check=True, text=True, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, timeout=10).stdout.strip()

    git(source, "init", "-b", "main")
    git(source, "config", "user.name", "Fixture")
    git(source, "config", "user.email", "fixture@example.invalid")
    registry = b'{"tools": []}\n'
    (source / "registry.json").write_bytes(registry)
    git(source, "add", "registry.json")
    git(source, "commit", "-m", "accepted catalog")
    commit = git(source, "rev-parse", "HEAD")
    bare = tmp_path / "bare"
    bare.mkdir()
    git(bare, "clone", "--bare", str(source), "tenant-a.git")
    monkeypatch.setenv("LEAF_TENANT_GIT_DIR", str(bare))
    monkeypatch.setenv("LEAF_EFFECTIVE_TENANTS_DIR", str(tmp_path / "effective"))
    # Exercise the existing reader/materializer, substituting only the store.
    monkeypatch.setattr(service, "customization_store_mode", lambda: "postgres")
    monkeypatch.setattr(service, "database_path", lambda: tmp_path / "unused.db")
    digest = hashlib.sha256(registry).hexdigest()
    pin = SimpleNamespace(catalog_commit=commit, catalog_digest=digest)
    calls = []

    def effective(*, tenant_id):
        calls.append(tenant_id)
        if tenant_id != "tenant-a":
            raise ChangeSetNotFoundError("no effective catalog")
        return pin

    monkeypatch.setattr(service.CustomizationService, "configured", lambda: SimpleNamespace(
        store=SimpleNamespace(get_effective_catalog=effective)))
    app = FastAPI()
    app.include_router(author.router)
    with TestClient(app) as client:
        yield SimpleNamespace(client=client, commit=commit, digest=digest, calls=calls,
                              target=tmp_path / "effective" / "tenant-a" / commit)


def request(catalog, tenant="tenant-a", secret="test-dispatch"):
    headers = {}
    if tenant is not None:
        headers["X-Tenant-Id"] = tenant
    if secret is not None:
        headers["X-Dispatch-Secret"] = secret
    return catalog.client.get("/internal/customization/effective-catalog", headers=headers)


def test_reads_and_materializes_existing_authority(catalog):
    assert not catalog.target.exists()
    response = request(catalog)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {"tenant_id": "tenant-a", "catalog_commit": catalog.commit,
                               "catalog_digest": catalog.digest}
    assert (catalog.target / "registry.json").is_file()
    assert set(catalog.calls) == {"tenant-a"}
    assert str(catalog.target) not in response.text


@pytest.mark.parametrize("tenant,secret", [("tenant-a", None), ("tenant-a", "wrong"),
                                          (None, "test-dispatch"), ("../tenant-a", "test-dispatch")])
def test_denied_before_authority_read(catalog, tenant, secret):
    response = request(catalog, tenant, secret)
    assert response.status_code == 403
    assert not catalog.calls and not catalog.target.exists()


def test_other_tenant_does_not_fall_back(catalog):
    response = request(catalog, tenant="tenant-b")
    assert response.status_code == 503
    assert catalog.calls == ["tenant-b"]
    assert catalog.commit not in response.text


def test_unsupported_authority_fails_closed(catalog, monkeypatch):
    def unavailable(tenant):
        raise service.CustomizationServiceError("customization_shared_sqlite_unsupported", 503)
    monkeypatch.setattr(service, "effective_catalog_pin", unavailable)
    response = request(catalog)
    assert response.status_code == 503
    assert response.json()["reason_code"] == "customization_shared_sqlite_unsupported"
    assert not catalog.target.exists()


def test_auth_off_cannot_use_dispatch(catalog, monkeypatch):
    monkeypatch.setenv("LEAF_AUTH_LIVE", "0")
    assert request(catalog).status_code == 503
    assert not catalog.calls


@pytest.mark.parametrize("kind", ["missing", "wrong-tenant", "registry", "head"])
def test_materialized_generation_must_match_pin(catalog, monkeypatch, kind):
    actual = service.effective_catalog_dir("tenant-a")
    if kind == "missing":
        actual = None
    elif kind == "wrong-tenant":
        actual = actual.parent.parent / "tenant-b" / catalog.commit
    elif kind == "registry":
        (actual / "registry.json").write_text('{"tools":["tampered"]}')
    else:
        git_dir = Path((actual / ".git").read_text().strip().removeprefix("gitdir: "))
        (git_dir / "HEAD").write_text("b" * 40 + "\n")
    monkeypatch.setattr(service, "effective_catalog_dir", lambda tenant: actual)
    response = request(catalog)
    assert response.status_code == 503
    assert "catalog_commit" not in response.json()
    assert str(catalog.target) not in response.text


def test_pointer_race_retries_only_boundedly(catalog, monkeypatch):
    target = service.effective_catalog_dir("tenant-a")
    pin = {"catalog_commit": catalog.commit, "effective_catalog_digest": catalog.digest}
    reads = []
    def changing(tenant):
        reads.append(tenant)
        return pin if len(reads) % 2 else {**pin, "catalog_commit": "b" * 40}
    monkeypatch.setattr(service, "effective_catalog_pin", changing)
    monkeypatch.setattr(service, "effective_catalog_dir", lambda tenant: target)
    assert request(catalog).status_code == 503
    assert len(reads) == 4


def test_one_pointer_race_can_settle(catalog, monkeypatch):
    target = service.effective_catalog_dir("tenant-a")
    pin = {"catalog_commit": catalog.commit, "effective_catalog_digest": catalog.digest}
    reads = iter([pin, {**pin, "catalog_commit": "b" * 40}, pin, pin])
    monkeypatch.setattr(service, "effective_catalog_pin", lambda tenant: next(reads))
    monkeypatch.setattr(service, "effective_catalog_dir", lambda tenant: target)
    assert request(catalog).json() == {"tenant_id": "tenant-a", "catalog_commit": catalog.commit,
                                     "catalog_digest": catalog.digest}
