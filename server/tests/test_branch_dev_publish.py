"""Reviewed Branch packages publish atomically without activating a catalog pin."""

import errno
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import zipfile

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from branch_tools import dev_publish
from branch_tools.dev_publish import DevPublishError, publish_branch_package
import customization_service
from customization_authority import AuthorityError, PublishRequest
from customization_models import ChangeSetConflictError, ChangeSetNotFoundError, ChangeState
from customization_postgres_store import PostgresCustomizationStore
from customization_service import CustomizationService, CustomizationServiceError
from customization_store import SQLiteCustomizationStore
import deps
from routers import tools as tools_router
import tool_envelopes
from test_customization_branch_target import FIXTURE, NAME, _change, _request, lane
from test_customization_runtime import staged_change


VERSION = "1.2.0"
REPO = Path(__file__).resolve().parents[2]


def package():
    return {p.relative_to(FIXTURE).as_posix(): p.read_bytes()
            for p in FIXTURE.rglob("*") if p.is_file()}


def snapshot(directory):
    return {p.relative_to(directory).as_posix(): (p.read_bytes(), p.stat().st_mtime_ns)
            for p in directory.rglob("*") if p.is_file()}


def test_zip_bytes_match_staging_script(tmp_path):
    root = tmp_path / "new" / "dev"
    result = publish_branch_package(package(), NAME, root)
    assert result.created and result.path == root / NAME / VERSION
    assert (result.path / "digest.txt").read_bytes() == (result.package_digest + "\n").encode()
    assert json.loads((result.path / "review.json").read_bytes()) == {"status": "reviewed"}
    path = REPO / "scripts/branch_tools/stage_dev_package.py"
    spec = importlib.util.spec_from_file_location("_stage_dev_parity", path)
    module = importlib.util.module_from_spec(spec)
    exec(compile(path.read_bytes(), str(path), "exec"), module.__dict__)
    staged_root = tmp_path / "script"
    assert module.main(["--package", str(FIXTURE), "--out", str(staged_root)]) == 0
    assert (result.path / "package.zip").read_bytes() == (staged_root / NAME / VERSION / "package.zip").read_bytes()
    with zipfile.ZipFile(result.path / "package.zip") as archive:
        assert archive.namelist() == [f"{NAME}/{path}" for path in sorted(package())]
        for entry in archive.infolist():
            assert entry.date_time == (1980, 1, 1, 0, 0, 0)
            assert entry.compress_type == zipfile.ZIP_DEFLATED
            assert entry.create_system == 3 and entry.external_attr == 0o100644 << 16
    assert list(result.path.parent.iterdir()) == [result.path]


def test_same_digest_is_a_read_only_noop(tmp_path, monkeypatch):
    first = publish_branch_package(package(), NAME, tmp_path)
    before = snapshot(tmp_path)

    def forbidden(*args, **kwargs):
        pytest.fail("idempotent publication attempted a write")

    monkeypatch.setattr(Path, "mkdir", forbidden)
    monkeypatch.setattr(Path, "write_text", forbidden)
    monkeypatch.setattr(dev_publish.tempfile, "mkdtemp", forbidden)
    second = publish_branch_package(dict(reversed(list(package().items()))), NAME, tmp_path)
    assert not second.created
    assert (second.name, second.version, second.package_digest, second.path) == (
        first.name, first.version, first.package_digest, first.path,
    )
    assert snapshot(tmp_path) == before


def test_conflicting_digest_does_not_modify_existing_version(tmp_path):
    publish_branch_package(package(), NAME, tmp_path)
    before = snapshot(tmp_path)
    files = package()
    files["main.csx"] += b"\n// a distinct valid package\n"
    with pytest.raises(DevPublishError) as caught:
        publish_branch_package(files, NAME, tmp_path)
    assert caught.value.code == "branch_version_conflict"
    assert snapshot(tmp_path) == before


@pytest.mark.parametrize("invalid", ["manifest", "script", "files", "bytes"])
def test_invalid_package_never_creates_root(tmp_path, invalid):
    files = package()
    if invalid == "manifest":
        del files["tool.json"]
    elif invalid == "script":
        files["main.csx"] = b'#r "untrusted.dll"\n'
    elif invalid == "files":
        files.update({f"lib/f{i}.csx": b"" for i in range(65)})
    else:
        files["main.csx"] = b" " * (4 * 1024 * 1024 + 1)
    root = tmp_path / "dev"
    with pytest.raises(DevPublishError) as caught:
        publish_branch_package(files, NAME, root)
    assert caught.value.code == "branch_package_invalid" and caught.value.failures
    assert not root.exists()


@pytest.mark.parametrize("location", [
    "root", "ancestor", "name", "version", "package.zip", "digest.txt", "review.json", "nested", "dangling",
])
def test_symlinks_are_refused_without_writing(tmp_path, location):
    root = tmp_path / "dev"
    outside = tmp_path / "outside"
    outside.mkdir()
    if location == "root":
        root.symlink_to(outside, target_is_directory=True)
    elif location == "ancestor":
        (tmp_path / "link").symlink_to(outside, target_is_directory=True)
        root = tmp_path / "link" / "dev"
    elif location == "name":
        root.mkdir()
        (root / NAME).symlink_to(outside, target_is_directory=True)
    elif location in {"version", "dangling"}:
        (root / NAME).mkdir(parents=True)
        (root / NAME / VERSION).symlink_to(
            outside if location == "version" else outside / "missing", target_is_directory=True,
        )
    else:
        published = publish_branch_package(package(), NAME, root)
        link = published.path / location
        if link.exists():
            link.unlink()
        target = outside / "file"
        target.write_bytes(b"outside must not change")
        link.symlink_to(target)
    before = snapshot(outside)
    with pytest.raises(DevPublishError) as caught:
        publish_branch_package(package(), NAME, root)
    assert caught.value.code == "branch_dev_catalog_unavailable"
    assert snapshot(outside) == before


@pytest.mark.parametrize("failure", ["zip", "digest", "review", "rename"])
def test_io_failures_remove_temporary_directory(tmp_path, monkeypatch, failure):
    original_write = Path.write_text

    def fail(*args, **kwargs):
        raise OSError("synthetic I/O failure")

    def write(path, *args, **kwargs):
        if path.name == {"digest": "digest.txt", "review": "review.json"}.get(failure):
            fail()
        return original_write(path, *args, **kwargs)

    if failure == "zip":
        monkeypatch.setattr(dev_publish.zipfile.ZipFile, "writestr", fail)
    elif failure == "rename":
        monkeypatch.setattr(Path, "rename", fail)
    else:
        monkeypatch.setattr(Path, "write_text", write)
    with pytest.raises(OSError):
        publish_branch_package(package(), NAME, tmp_path)
    assert list((tmp_path / NAME).iterdir()) == []


@pytest.mark.parametrize("conflict", [False, True])
def test_concurrent_atomic_install_rechecks_digest(tmp_path, monkeypatch, conflict):
    winner_root = tmp_path / "winner"
    files = package()
    if conflict:
        files["main.csx"] += b"\n// winner has different bytes\n"
    winner = publish_branch_package(files, NAME, winner_root)
    root = tmp_path / "dev"
    rename = Path.rename

    def race(temporary, destination):
        # Another publisher atomically installs a complete directory first.
        rename(winner.path, destination)
        raise OSError(errno.EEXIST, "another publisher won")

    monkeypatch.setattr(Path, "rename", race)
    if conflict:
        with pytest.raises(DevPublishError, match="branch_version_conflict"):
            publish_branch_package(package(), NAME, root)
    else:
        assert not publish_branch_package(package(), NAME, root).created
    assert [p.name for p in (root / NAME).iterdir()] == [VERSION]


@pytest.fixture
def publishing_lane(lane, tmp_path, monkeypatch):
    # Stage with S5b's genuine bare Git fixture, then use the real signed,
    # independently issued confirmation rather than a mock approver.
    response = lane.service.stage(**_request(lane))
    lane.change = _change(lane, response["receipt"])
    monkeypatch.setattr(lane.service, "_authority", CustomizationService._authority.__get__(lane.service))
    monkeypatch.setenv("LEAF_CUSTOMIZATION_CONFIRMATION_KEY", "branch-publication-test-key")
    monkeypatch.setenv("LEAF_CUSTOMIZATION_INTERNAL_APPROVER_SUBJECT", "auth0|reviewer")
    monkeypatch.setenv("LEAF_CUSTOMIZATION_INTERNAL_TENANTS", "tenant-a")
    monkeypatch.setenv("LEAF_BRANCH_DEV_TOOLS", "1")
    lane.root = tmp_path / "dev-catalog"
    monkeypatch.setenv("LEAF_BRANCH_DEV_TOOLS_DIR", str(lane.root))
    lane.confirmation_id = lane.service.confirm(
        tenant_id="tenant-a", change_set_id=lane.change.change_set_id,
    )["confirmation_id"]
    lane.request = PublishRequest(
        lane.change.change_set_id, lane.change.staged_commit, lane.change.catalog_digest,
        lane.change.desired_platform_release, lane.change.workspace_contract_digest,
    )

    def publish(key="publish-a", **kwargs):
        return lane.service.publish(
            tenant=lane.tenant, request=lane.request,
            confirmation_id=lane.confirmation_id, idempotency_key=key, **kwargs,
        )

    def forbidden(*args, **kwargs):
        pytest.fail("branch publication reached the harness or signed catalog")

    lane.publish = publish
    monkeypatch.setattr(lane.service, "_harness_publish", forbidden)
    monkeypatch.setattr(tool_envelopes, "published_catalog", forbidden)
    return lane


def durable(lane):
    return lane.store.get_change_set(tenant_id="tenant-a", change_set_id=lane.change.change_set_id)


def test_approved_publish_is_listed_and_preserves_effective_catalog(publishing_lane):
    lane = publishing_lane
    # A pre-existing, unrelated Studio pointer must remain byte-for-byte equal.
    prior = staged_change(lane.store, suffix="studio")
    with lane.store._transaction() as conn:
        conn.execute(
            "INSERT INTO effective_catalogs (tenant_id, change_set_id, catalog_commit, catalog_digest, "
            "effective_platform_release, workspace_contract_digest) VALUES (?, ?, ?, ?, ?, ?)",
            (prior.tenant_id, prior.change_set_id, prior.staged_commit, prior.catalog_digest,
             prior.desired_platform_release, prior.workspace_contract_digest),
        )
    before = lane.store.get_effective_catalog(tenant_id="tenant-a")
    result = lane.publish()
    assert result == {
        "contract": customization_service.CONTRACT, "tenant_id": "tenant-a",
        "change_set_id": lane.change.change_set_id, "target": "branch", "name": NAME,
        "version": VERSION, "package_digest": dev_publish.check_branch_package(lane.files, NAME).package_digest,
        "dev_catalog": "/api/tool-envelopes/dev",
    }
    assert durable(lane).state is ChangeState.PUBLISHED
    assert lane.store.get_effective_catalog(tenant_id="tenant-a") == before
    assert customization_service._git(lane.bare, "rev-parse", "refs/heads/main") == lane.base
    app = FastAPI()
    app.include_router(tools_router.router)
    app.dependency_overrides[deps.require_active_tenant] = lambda: "tenant-a"
    client = TestClient(app)
    response = client.get(result["dev_catalog"])
    assert response.status_code == 200
    entry, = response.json()["tools"]
    assert entry["name"] == NAME and entry["version"] == VERSION
    assert entry["review"] == "reviewed" and entry["package_digest"] == result["package_digest"]
    assert client.get(f"{result['dev_catalog']}/{NAME}/{VERSION}/package").content == (
        lane.root / NAME / VERSION / "package.zip"
    ).read_bytes()
    app.dependency_overrides[deps.require_active_tenant] = lambda: "tenant-b"
    assert client.get(result["dev_catalog"]).status_code == 404


def test_branch_publish_does_not_create_an_effective_catalog(publishing_lane):
    lane = publishing_lane
    lane.publish()
    published = durable(lane)
    assert lane.store.publish_branch(
        tenant_id="tenant-a", change_set_id=published.change_set_id,
        expected_version=published.version - 1, idempotency_key="publish-a",
        approver_subject="auth0|reviewer",
    ) == published
    with pytest.raises(ChangeSetNotFoundError):
        lane.store.get_effective_catalog(tenant_id="tenant-a")


def test_published_replay_reads_disk_without_writes(publishing_lane, monkeypatch):
    lane = publishing_lane
    first = lane.publish()
    before = snapshot(lane.root)
    change = durable(lane)

    def forbidden(*args, **kwargs):
        pytest.fail("published replay attempted publication")

    monkeypatch.setattr(customization_service, "publish_branch_package", forbidden)
    monkeypatch.setattr(lane.store, "prepare_publish", forbidden)
    assert lane.publish() == first
    assert lane.publish(key="another-key") == first
    assert durable(lane) == change and snapshot(lane.root) == before
    (lane.root / NAME / VERSION / "digest.txt").write_text("0" * 64 + "\n")
    with pytest.raises(CustomizationServiceError, match="publish_not_available"):
        lane.publish()


def test_conflict_supersedes_change_and_frees_publish_slot(publishing_lane):
    lane = publishing_lane
    files = package()
    files["main.csx"] += b"\n// different package at the same version\n"
    publish_branch_package(files, NAME, lane.root)
    before = snapshot(lane.root)
    with pytest.raises(CustomizationServiceError) as caught:
        lane.publish()
    assert (caught.value.code, caught.value.status_code) == ("branch_version_conflict", 409)
    assert durable(lane).state is ChangeState.SUPERSEDED
    assert lane.store.get_confirmation(confirmation_id=lane.confirmation_id)["consumed"]
    assert snapshot(lane.root) == before
    with lane.store._connection() as conn:
        row = conn.execute(
            "SELECT reason_code FROM customization_audit_events WHERE tenant_id = ? AND change_set_id = ? "
            "AND next_state = ?", ("tenant-a", lane.change.change_set_id, "superseded"),
        ).fetchone()
        assert row["reason_code"] == "branch_branch_version_conflict"
        assert conn.execute(
            "SELECT 1 FROM customization_change_sets WHERE tenant_id = ? AND state = ?",
            ("tenant-a", "publishing"),
        ).fetchone() is None


@pytest.mark.parametrize("refusal", ["external", "flag-off", "flag-true", "flag-missing", "dir-missing", "dir-empty"])
def test_refusal_leaves_staged_and_confirmation_unconsumed(publishing_lane, monkeypatch, refusal):
    lane = publishing_lane
    expected = ("branch_dev_catalog_unavailable", 503)
    if refusal == "external":
        monkeypatch.setenv("LEAF_CUSTOMIZATION_INTERNAL_TENANTS", "tenant-b")
        expected = ("branch_publish_internal_only", 403)
    elif refusal == "flag-off":
        monkeypatch.setenv("LEAF_BRANCH_DEV_TOOLS", "0")
    elif refusal == "flag-true":
        monkeypatch.setenv("LEAF_BRANCH_DEV_TOOLS", "true")
    elif refusal == "flag-missing":
        monkeypatch.delenv("LEAF_BRANCH_DEV_TOOLS")
    elif refusal == "dir-missing":
        monkeypatch.delenv("LEAF_BRANCH_DEV_TOOLS_DIR")
    else:
        monkeypatch.setenv("LEAF_BRANCH_DEV_TOOLS_DIR", "  ")
    with pytest.raises(CustomizationServiceError) as caught:
        lane.publish()
    assert (caught.value.code, caught.value.status_code) == expected
    assert durable(lane) == lane.change
    assert not lane.store.get_confirmation(confirmation_id=lane.confirmation_id)["consumed"]
    assert not lane.root.exists()


def test_publication_request_waits_for_review_even_under_auto_policy(publishing_lane, monkeypatch):
    lane = publishing_lane
    monkeypatch.setattr(lane.service, "_publication_policy_state", lambda _: (True, False))
    # Spend the fixture's approval without publishing, to prove auto policy
    # cannot mint "reviewed" status for a Branch package.
    record = lane.store.get_confirmation(confirmation_id=lane.confirmation_id)
    lane.store.consume_confirmation(confirmation_id=lane.confirmation_id, signature=record["signature"])
    waiting = lane.service.request_publication(tenant=lane.tenant, change_set_id=lane.change.change_set_id)
    assert waiting == {
        "contract": customization_service.CONTRACT, "change_set_id": lane.change.change_set_id,
        "status": "awaiting_approval", "target": "branch", "name": NAME, "version": VERSION,
    }
    assert durable(lane).state is ChangeState.STAGED and not lane.root.exists()
    lane.service.confirm(tenant_id="tenant-a", change_set_id=lane.change.change_set_id)
    published = lane.service.request_publication(tenant=lane.tenant, change_set_id=lane.change.change_set_id)
    assert published == dict(waiting, status="published")
    before = snapshot(lane.root)
    assert lane.service.request_publication(tenant=lane.tenant, change_set_id=lane.change.change_set_id) == published
    assert snapshot(lane.root) == before


def test_recovery_after_entry_written_but_store_commit_failed(publishing_lane, monkeypatch):
    lane = publishing_lane
    publish = lane.store.publish_branch

    def fail(**kwargs):
        raise OSError("lost store commit")

    monkeypatch.setattr(lane.store, "publish_branch", fail)
    with pytest.raises(CustomizationServiceError, match="record_fields_write_failed"):
        lane.publish()
    assert durable(lane).state is ChangeState.PUBLISHING
    before = snapshot(lane.root)
    monkeypatch.setattr(lane.store, "publish_branch", publish)
    assert lane.publish()["target"] == "branch"
    assert durable(lane).state is ChangeState.PUBLISHED and snapshot(lane.root) == before


def test_invalid_package_supersedes_prepared_publication(publishing_lane, monkeypatch):
    lane = publishing_lane
    monkeypatch.setattr(lane.service, "_branch_package_files", lambda _: ({"main.csx": b""}, NAME))
    with pytest.raises(CustomizationServiceError) as caught:
        lane.publish()
    assert (caught.value.code, caught.value.status_code) == ("branch_package_invalid", 422)
    assert caught.value.failures and durable(lane).state is ChangeState.SUPERSEDED
    assert not lane.root.exists()


def test_signed_confirmation_is_required_and_bound_to_change(publishing_lane):
    lane = publishing_lane
    with pytest.raises(AuthorityError):
        lane.service.publish(
            tenant=lane.tenant, request=lane.request,
            confirmation_id="unissued-confirmation", idempotency_key="publish-a",
        )
    assert durable(lane) == lane.change
    assert not lane.store.get_confirmation(confirmation_id=lane.confirmation_id)["consumed"]
    # A caller from another tenant cannot select this tenant's change.
    with pytest.raises(CustomizationServiceError):
        lane.service.publish(
            tenant=deps.TenantContext("tenant-b", tier="hosted_pro", subject="auth0|author"),
            request=lane.request, confirmation_id=lane.confirmation_id, idempotency_key="publish-a",
        )
    assert durable(lane) == lane.change


@pytest.mark.parametrize("invalid", ["state", "version", "approver"])
def test_store_branch_transition_checks_prepared_authority(publishing_lane, invalid):
    lane = publishing_lane
    change = lane.change
    if invalid != "state":
        confirmation, signature = lane.service._authority().verify_publish_confirmation(
            tenant_id="tenant-a", request=lane.request, confirmation_id=lane.confirmation_id,
        )
        change = lane.store.prepare_publish(
            tenant_id="tenant-a", change_set_id=change.change_set_id,
            confirmation_id=lane.confirmation_id, confirmation_signature=signature,
            approver_subject=confirmation.approver_subject, idempotency_key="prepare-a",
        )
    with pytest.raises(ChangeSetConflictError):
        lane.store.publish_branch(
            tenant_id="tenant-a", change_set_id=change.change_set_id,
            expected_version=change.version + (1 if invalid == "version" else 0),
            idempotency_key="finish-a", approver_subject="wrong" if invalid == "approver" else "auth0|reviewer",
        )
    assert durable(lane) == change
    assert PostgresCustomizationStore.publish_branch is SQLiteCustomizationStore.publish_branch


def test_studio_default_still_publishes_through_harness(tmp_path, monkeypatch):
    store = SQLiteCustomizationStore(tmp_path / "studio.db")
    change = staged_change(store)
    service = CustomizationService(store)
    monkeypatch.setenv("LEAF_CUSTOMIZATION_CONFIRMATION_KEY", "studio-test-key")
    monkeypatch.setenv("LEAF_CUSTOMIZATION_INTERNAL_APPROVER_SUBJECT", "auth0|reviewer")
    monkeypatch.setattr(customization_service, "enabled", lambda *_: True)
    monkeypatch.setattr(customization_service, "_binding", lambda _: SimpleNamespace(role="owner"))
    monkeypatch.setattr(deps, "auth_live", lambda: True)
    monkeypatch.setattr(service, "_verify_catalog", lambda *_: None)
    calls = []

    def harness_publish(row):
        calls.append(row.change_set_id)
        return row.staged_commit

    monkeypatch.setattr(service, "_harness_publish", harness_publish)
    approval = service.confirm(tenant_id="tenant-a", change_set_id=change.change_set_id)
    request = PublishRequest(change.change_set_id, change.staged_commit, change.catalog_digest,
                             change.desired_platform_release, change.workspace_contract_digest)
    result = service.publish(tenant="tenant-a", request=request,
                             confirmation_id=approval["confirmation_id"], idempotency_key="studio-publish")
    assert calls == [change.change_set_id] and "target" not in result
    assert store.get_effective_catalog(tenant_id="tenant-a").change_set_id == change.change_set_id
