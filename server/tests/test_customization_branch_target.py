"""Branch authoring validates receipt-bound package blobs in the tenant repo."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
import requests

import customization_service
import customization_stage_worker
import deps
from customization_models import ChangeState
from customization_service import CustomizationService, CustomizationServiceError
from customization_store import SQLiteCustomizationStore
from routers import author as author_router


NAME = "panel-gap-check"
PACKAGE = f"branch-tools/{NAME}"
WORKSPACE = "d" * 64
DESCRIPTION = "Check panel gaps with a Branch tool"
FIXTURE = Path(__file__).parent / "fixtures" / "branch_tools" / "reference-package" / NAME


def _git(bare, *args, data=None):
    return subprocess.run(
        ["git", "-c", "user.name=Branch Test", "-c", "user.email=branch@example.test",
         "--git-dir", str(bare), *args], input=data, check=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    ).stdout.strip().decode()


def _commit(bare, files, parent=None, modes=None):
    """Write a genuine Git tree without relying on a mutable server checkout."""
    root = {}
    for path, data in files.items():
        node = root
        parts = path.split("/")
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        oid = _git(bare, "hash-object", "-w", "--stdin", data=data)
        node[parts[-1]] = ((modes or {}).get(path, "100644"), oid)

    def tree(node):
        entries = []
        for name, item in sorted(node.items()):
            mode, kind, oid = ("040000", "tree", tree(item)) if isinstance(item, dict) else (item[0], "blob", item[1])
            entries.append(f"{mode} {kind} {oid}\t{name}\0".encode())
        return _git(bare, "mktree", "-z", data=b"".join(entries))

    args = ["commit-tree", tree(root)]
    if parent:
        args += ["-p", parent]
    return _git(bare, *args, data=b"branch test\n")


@pytest.fixture
def lane(tmp_path, monkeypatch):
    bare = tmp_path / "tenant-a.git"
    subprocess.run(["git", "init", "--bare", str(bare)], check=True, capture_output=True)
    store = SQLiteCustomizationStore(tmp_path / "customization.db")
    store.initialize()
    service = CustomizationService(store)
    tenant = deps.TenantContext("tenant-a", tier="hosted_pro", subject="auth0|author")
    state = SimpleNamespace(
        bare=bare, store=store, service=service, tenant=tenant,
        base_files={"registry.json": b'{"tools":[]}'},
        files={p.relative_to(FIXTURE).as_posix(): p.read_bytes()
               for p in FIXTURE.rglob("*") if p.is_file()},
        name=NAME, wires=[], bodies=[], extra_files={}, modes={}, receipt_edit=None,
    )

    def seed(package=False):
        if package:
            state.base_files.update({f"{PACKAGE}/{path}": data for path, data in state.files.items()})
        state.base = _commit(bare, state.base_files)
        _git(bare, "update-ref", "refs/heads/main", state.base)

    state.seed = seed
    seed()

    def resolve(tenant_id):
        assert tenant_id == "tenant-a"
        return bare

    monkeypatch.delenv("LEAF_TENANT_MCP_BROKER_URL", raising=False)
    monkeypatch.setenv("LEAF_AUTHOR_HARNESS_URL", "http://harness.internal:8150")
    monkeypatch.setenv("LEAF_HARNESS_SECRET", "test-secret")
    monkeypatch.setattr(customization_service, "enabled", lambda *_: True)
    monkeypatch.setattr(customization_service, "_bare_repo", resolve)
    monkeypatch.setattr(customization_service, "_ensure_bare_repo", resolve)
    monkeypatch.setattr(customization_service, "_binding", lambda _: SimpleNamespace(subject=tenant.subject, role="owner"))
    monkeypatch.setattr(service, "_authority", lambda: SimpleNamespace(authorize_stage=lambda **_: None))
    monkeypatch.setattr(service, "_release", lambda: SimpleNamespace(
        release_id="release-a", workspace_contract_sha256=WORKSPACE,
    ))
    monkeypatch.setattr(customization_service.entitlements, "resolve_tier", lambda _: "hosted_pro")
    monkeypatch.setattr(customization_service.entitlements, "entitlements_for", lambda *_: {"build": True})
    monkeypatch.setattr(customization_service.author_quota, "enforce", lambda *_, **__: None)
    monkeypatch.setattr(deps, "active_stage_author_subject", lambda *_: tenant.subject)
    monkeypatch.setattr(deps, "auth_live", lambda: True)

    def post(url, **kwargs):
        assert url == "http://harness.internal:8150/author/stage"
        wire = kwargs["json"]
        state.wires.append(wire)
        package_path = f"branch-tools/{state.name}"
        files = dict(state.base_files)
        files.update({f"{package_path}/{path}": data for path, data in state.files.items()})
        files.update(state.extra_files)
        parent = wire["expectedBaseSha"]
        if "revisionBaseCommit" in wire:
            # The fixture's published tree is main plus exactly this package.
            tree = _git(bare, "rev-parse", wire["revisionBaseCommit"] + "^{tree}")
            parent = _git(bare, "commit-tree", tree, "-p", parent, data=b"revision base\n")
            if state.edit_seed:
                parent = state.edit_seed(parent)
        staged = _commit(bare, files, parent, state.modes)
        receipt = {
            "contract": "leaf.customization.v1", "tenant_id": "tenant-a",
            "change_set_id": wire["changeSetId"], "state": "staged",
            "base_commit": wire["expectedBaseSha"], "staged_commit": staged,
            "catalog_digest": hashlib.sha256(state.base_files["registry.json"]).hexdigest(),
            "platform_release": wire["platformRelease"],
            "workspace_contract_digest": wire["workspaceContractDigest"],
            "idempotency_key": wire["idempotencyKey"],
            "target": "branch", "package_path": package_path,
            "package_files": sorted(path for path in files if path.startswith(package_path + "/")),
        }
        if state.receipt_edit:
            state.receipt_edit(receipt)
        body = {"receipt": receipt, "branch_package": json.loads(state.files["tool.json"])}
        state.bodies.append(body)
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: body)

    monkeypatch.setattr(requests, "post", post)
    state.edit_seed = None
    return state


def _request(lane, *, key="request-a", **kwargs):
    return dict(tenant=lane.tenant, description=DESCRIPTION, mode="build",
                idempotency_key=key, target="branch", **kwargs)


def _change(lane, job):
    return lane.store.get_change_set(tenant_id="tenant-a", change_set_id=job["change_set_id"])


def _finish(lane, job, path):
    if path == "worker":
        assert customization_stage_worker.run_once("branch-worker", service=lane.service)
    else:
        change = _change(lane, job)
        if path == "callback":
            body = lane.service.dispatch_stage(change)
            lane.service.record_staged_callback(tenant_id="tenant-a", receipt=body["receipt"])
        else:
            lane.service.execute_stage(change)


@pytest.mark.parametrize("path", ["sync", "worker", "callback"])
def test_good_package_reaches_staged_with_branch_target(lane, path):
    if path == "sync":
        response = lane.service.stage(**_request(lane))
        job = {"change_set_id": response["receipt"]["change_set_id"]}
        assert response["target"] == "branch" and response["package_path"] == PACKAGE
    else:
        job = lane.service.enqueue_stage(**_request(lane))
        assert job["target"] == "branch"
        _finish(lane, job, path)
    change = _change(lane, job)
    assert change.state is ChangeState.STAGED and change.tool_target == "branch"
    status = lane.service.stage_status(tenant=lane.tenant, change_set_id=change.change_set_id)
    assert status["status"] == "staged" and status["target"] == "branch"
    assert status["package_path"] == PACKAGE
    assert status["receipt"] == lane.bodies[-1]["receipt"]
    assert status["result"]["branch_package"]["name"] == NAME
    assert lane.wires[-1]["target"] == "branch"
    calls = len(lane.wires)
    replay = lane.service.stage(**_request(lane))
    assert replay["receipt"] == status["receipt"] and len(lane.wires) == calls


@pytest.mark.parametrize("path", ["sync", "worker", "callback"])
def test_bad_package_fails_with_bounded_diagnostics(lane, path):
    # S1's V17 negative: a forbidden assembly reference in the good fixture.
    lane.files["main.csx"] = b'#r "untrusted.dll"\n'
    job = lane.service.enqueue_stage(**_request(lane))
    if path == "worker":
        _finish(lane, job, path)
    else:
        with pytest.raises(CustomizationServiceError) as caught:
            _finish(lane, job, path)
        assert caught.value.code == "branch_package_invalid"
        response = author_router._customization_error(caught.value)
        assert response.status_code == 422
        failures = json.loads(response.body)["failures"]
        assert any(f["rule"] == "V17" for f in failures)
        assert 1 <= len(failures) <= 50
        assert all(set(f) == {"rule", "path", "message"} and len(f["message"]) <= 300 for f in failures)
    change = _change(lane, job)
    assert change.state is ChangeState.FAILED
    assert change.stage_error_code == "branch_package_invalid"
    assert change.stage_error_retryable is False
    assert "V17" in change.stage_error_message and len(change.stage_error_message) <= 2000
    status = lane.service.stage_status_change(change)
    assert status["status"] == "failed" and status["error"]["message"] == change.stage_error_message


@pytest.mark.parametrize(("version", "accepted"), [("1.2.0", False), ("1.1.9", False), ("1.2.1", True), ("1.10.0", True), ("2.0.0", True)])
def test_branch_revision_requires_strict_semver_increase(lane, version, accepted):
    lane.seed(package=True)
    manifest = json.loads(lane.files["tool.json"])
    manifest["version"] = version
    lane.files["tool.json"] = json.dumps(manifest).encode()
    # Equal-version revisions still have a nonempty diff.
    lane.files["main.csx"] += b"\n// revised\n"
    request = _request(lane, target_tool_name=NAME)
    if accepted:
        response = lane.service.stage(**request)
        change = _change(lane, response["receipt"])
        assert change.change_kind == "revise" and change.target_tool_name == NAME
    else:
        with pytest.raises(CustomizationServiceError) as caught:
            lane.service.stage(**request)
        assert caught.value.code == "branch_version_not_greater" and caught.value.status_code == 409


@pytest.mark.parametrize(("scenario", "code", "status"), [
    ("exists", "branch_tool_exists", 409),
    ("missing", "revision_target_missing", 422),
    ("mismatch", "branch_target_mismatch", 422),
    ("manifest-mismatch", "branch_target_mismatch", 422),
])
def test_branch_target_identity(lane, scenario, code, status):
    kwargs = {}
    if scenario == "exists":
        lane.seed(package=True)
        lane.files["main.csx"] += b"\n// modified\n"
    elif scenario == "missing":
        kwargs["target_tool_name"] = NAME
    elif scenario == "manifest-mismatch":
        lane.seed(package=True)
        kwargs["target_tool_name"] = NAME
        manifest = json.loads(lane.files["tool.json"])
        manifest["name"] = "another-tool"
        lane.files["tool.json"] = json.dumps(manifest).encode()
    else:
        kwargs["target_tool_name"] = "another-tool"
        lane.base_files["branch-tools/another-tool/tool.json"] = lane.files["tool.json"]
        lane.seed()
    with pytest.raises(CustomizationServiceError) as caught:
        lane.service.stage(**_request(lane, **kwargs))
    assert (caught.value.code, caught.value.status_code) == (code, status)


def _stored_branch(lane, version="1.2.0", *, tenant="tenant-a", published=True, manifest_edit=None):
    manifest = dict(json.loads(lane.files["tool.json"]), version=version)
    manifest.update(manifest_edit or {})
    package = dict(lane.files)
    package["tool.json"] = json.dumps(manifest).encode()
    files = dict(lane.base_files)
    files.update({f"{PACKAGE}/{path}": data for path, data in package.items()})
    commit = _commit(lane.bare, files, lane.base)
    row = lane.store.create_change_set(
        tenant_id=tenant, idempotency_key=f"create-{version}", base_commit=lane.base,
        desired_platform_release="release-a", workspace_contract_digest=WORKSPACE,
        author_subject=lane.tenant.subject, tool_target="branch",
    )

    def transition(state):
        return lane.store.transition(
            tenant_id=tenant, change_set_id=row.change_set_id, expected_version=row.version,
            next_state=state, idempotency_key=f"{state.value}-{version}",
            approver_subject="reviewer" if state is ChangeState.APPROVED else None,
        )

    row = transition(ChangeState.STAGING)
    row = lane.store.record_staged(
        tenant_id=tenant, change_set_id=row.change_set_id, expected_version=row.version,
        idempotency_key=f"stage-{version}", staged_commit=commit,
        catalog_digest=hashlib.sha256(files["registry.json"]).hexdigest(),
        platform_release="release-a", workspace_contract_digest=WORKSPACE,
    )
    if published:
        for state in (ChangeState.AWAITING_APPROVAL, ChangeState.APPROVED,
                      ChangeState.PUBLISHING, ChangeState.PUBLISHED):
            row = transition(state)
    return row


@pytest.mark.parametrize("multiple", [False, True])
def test_branch_revision_uses_latest_published_tree(lane, multiple):
    if multiple:
        _stored_branch(lane, "1.0.0")
    latest = _stored_branch(lane, "1.1.0")
    response = lane.service.stage(**_request(lane, target_tool_name=NAME))
    change = _change(lane, response["receipt"])
    assert change.state is ChangeState.STAGED and change.change_kind == "revise"
    assert lane.wires[-1]["revisionBaseCommit"] == latest.staged_commit
    seed = _git(lane.bare, "rev-parse", change.staged_commit + "^")
    assert _git(lane.bare, "rev-parse", seed + "^") == lane.base
    assert _git(lane.bare, "rev-parse", f"{seed}:{PACKAGE}") == _git(lane.bare, "rev-parse", f"{latest.staged_commit}:{PACKAGE}")
    assert _git(lane.bare, "rev-parse", "refs/heads/main") == lane.base


@pytest.mark.parametrize("limit", [True, 0, 501, 1.0])
def test_published_branch_lookup_requires_bounded_integer(lane, limit):
    with pytest.raises(ValueError):
        lane.store.list_published_branch_change_sets(tenant_id="tenant-a", limit=limit)


@pytest.mark.parametrize("method", ["stage", "enqueue_stage"])
@pytest.mark.parametrize("source", ["missing", "unpublished", "other-tenant", "invalid-latest"])
def test_revision_admission_refuses_before_reservation_or_charge(lane, monkeypatch, method, source):
    if source == "unpublished":
        _stored_branch(lane, published=False)
    elif source == "other-tenant":
        _stored_branch(lane, tenant="tenant-b")
    elif source == "invalid-latest":
        _stored_branch(lane, "1.0.0")
        _stored_branch(lane, manifest_edit={"entry": "wrong.csx"})
    with lane.store._connection() as conn:
        before = conn.execute("SELECT count(*) FROM customization_change_sets").fetchone()[0]
    charges = []
    monkeypatch.setattr(customization_service.author_quota, "enforce", lambda *a, **kw: charges.append(a))
    with pytest.raises(CustomizationServiceError) as caught:
        getattr(lane.service, method)(**_request(lane, target_tool_name=NAME))
    assert (caught.value.code, caught.value.status_code) == ("revision_target_missing", 422)
    with lane.store._connection() as conn:
        assert conn.execute("SELECT count(*) FROM customization_change_sets").fetchone()[0] == before
    assert not charges and not lane.wires


@pytest.mark.parametrize(("seed", "code", "status"), [
    ("wrong-tree", "branch_revise_target_missing", 404),
    ("outside", "branch_revise_target_missing", 404),
    ("no-seed", "branch_revise_target_missing", 404),
    ("equal-version", "branch_version_not_greater", 409),
])
def test_published_revision_policy(lane, seed, code, status):
    published = _stored_branch(lane)
    if seed == "wrong-tree":
        files = dict(lane.base_files)
        files.update({f"{PACKAGE}/{path}": data for path, data in lane.files.items()})
        files[f"{PACKAGE}/main.csx"] += b"\n// different seed\n"
        lane.edit_seed = lambda _: _commit(lane.bare, files, lane.base)
    elif seed == "outside":
        files = dict(lane.base_files)
        for path in lane.files:
            key = f"{PACKAGE}/{path}"
            files[key] = customization_service._git_blob(lane.bare, f"{published.staged_commit}:{key}")
        files[f" {PACKAGE}/unrelated"] = b"seed only"
        lane.edit_seed = lambda _: _commit(lane.bare, files, lane.base)
    elif seed == "no-seed":
        lane.edit_seed = lambda _: lane.base
    with pytest.raises(CustomizationServiceError) as caught:
        lane.service.stage(**_request(lane, target_tool_name=NAME))
    assert (caught.value.code, caught.value.status_code) == (code, status)


@pytest.mark.parametrize("scenario", ["outside", "registry", "symlink", "file-list", "digest"])
def test_branch_policy_rejects_unbound_paths_and_catalog(lane, scenario):
    expected = "invalid_staged_paths"
    if scenario == "outside":
        lane.extra_files["outside.txt"] = b"unrelated"
    elif scenario == "registry":
        lane.extra_files["registry.json"] = b'{"tools": [], "changed":true}'
        lane.receipt_edit = lambda r: r.update(catalog_digest=hashlib.sha256(lane.extra_files["registry.json"]).hexdigest())
    elif scenario == "symlink":
        lane.modes[f"{PACKAGE}/main.csx"] = "120000"
    elif scenario == "file-list":
        lane.receipt_edit = lambda r: r["package_files"].pop()
    else:
        lane.receipt_edit = lambda r: r.update(catalog_digest="0" * 64)
        expected = "catalog_digest_mismatch"
    with pytest.raises(CustomizationServiceError) as caught:
        lane.service.stage(**_request(lane))
    assert caught.value.code == expected


@pytest.mark.parametrize("edit", [
    lambda r: r.update(target="studio"),
    lambda r: r.update(tenant_id="tenant-b"),
    lambda r: r.update(package_path="branch-tools/../escape"),
    lambda r: r.update(package_files=[]),
    lambda r: r["package_files"].append(r["package_files"][0]),
    lambda r: r.update(package_files=["outside/main.csx"]),
    lambda r: r.update(package_files=[{}]),
    lambda r: r.update(extra="unexpected"),
])
def test_branch_receipt_is_closed_and_bounded(lane, edit):
    lane.receipt_edit = edit
    with pytest.raises(CustomizationServiceError) as caught:
        lane.service.stage(**_request(lane))
    assert caught.value.code == "invalid_staged_receipt"


def test_studio_fingerprint_and_wire_stay_unchanged_and_branch_keys_are_refused(lane):
    request = _request(lane)
    request.pop("target")
    job = lane.service.enqueue_stage(**request)
    change = _change(lane, job)
    assert change.tool_target is None
    assert change.request_fingerprint == hashlib.sha256(DESCRIPTION.encode()).hexdigest()
    assert "target" not in job and "package_path" not in job
    assert lane.service.enqueue_stage(**dict(request, target="studio")) == job
    with pytest.raises(CustomizationServiceError) as caught:
        lane.service.execute_stage(change)
    assert caught.value.code == "invalid_staged_receipt"
    assert "target" not in lane.wires[-1]
    receipt = dict(lane.bodies[-1]["receipt"])
    for key in ("target", "package_path", "package_files"):
        receipt.pop(key)
    assert lane.service._validate_receipt(receipt, change) == receipt


@pytest.mark.parametrize("first_branch", [True, False])
def test_target_is_part_of_idempotent_reservation(lane, first_branch):
    request = _request(lane)
    request["target"] = "branch" if first_branch else "studio"
    job = lane.service.enqueue_stage(**request)
    assert lane.service.enqueue_stage(**request) == job
    request["target"] = "studio" if first_branch else "branch"
    with pytest.raises(CustomizationServiceError) as caught:
        lane.service.enqueue_stage(**request)
    assert caught.value.code == "idempotency_replay"


@pytest.mark.parametrize("method", ["stage", "enqueue_stage"])
def test_service_refuses_unknown_target(lane, method):
    request = _request(lane)
    request["target"] = "other"
    with pytest.raises(CustomizationServiceError) as caught:
        getattr(lane.service, method)(**request)
    assert (caught.value.code, caught.value.status_code) == ("invalid_target", 422)


@pytest.mark.parametrize("route", ["/api/author", "/api/author/stage"])
@pytest.mark.parametrize("target", [None, "studio", "branch"])
def test_http_routes_forward_only_branch_target(lane, monkeypatch, route, target):
    calls = []

    def enqueue(**kwargs):
        calls.append(kwargs)
        return {"contract": "leaf.customization-stage-job.v1", "status": "queued"}

    monkeypatch.setattr(author_router.CustomizationService, "configured", classmethod(lambda _: lane.service))
    monkeypatch.setattr(lane.service, "enqueue_stage", enqueue)
    monkeypatch.setattr(author_router, "customization_enabled", lambda *_: True)
    monkeypatch.setattr(author_router, "_customization_gate", lambda *_: None)
    monkeypatch.setattr(author_router, "_emit_author_event", lambda *_: None)
    monkeypatch.setattr(deps, "stage_author_identity", lambda tenant, *_: tenant)
    app = FastAPI()
    app.include_router(author_router.router)
    app.dependency_overrides[deps.require_tenant] = lambda: lane.tenant
    body = {"description": DESCRIPTION, "mode": "build"}
    if target is not None:
        body["target"] = target
    if route.endswith("/stage"):
        body["idempotency_key"] = "route-request"
    response = TestClient(app).post(route, json=body, headers={"Idempotency-Key": "route-request"})
    assert response.status_code == 202
    assert len(calls) == 1
    if target == "branch":
        assert calls[0]["target"] == "branch"
    else:
        assert "target" not in calls[0]


def test_legacy_branch_target_is_unavailable(lane):
    response = author_router._legacy_author(author_router.AuthorRequest(
        description=DESCRIPTION, target="branch",
    ), lane.tenant)
    assert response.status_code == 422
    assert json.loads(response.body)["reason_code"] == "branch_target_unavailable"


def test_branch_recovery_uses_target_fingerprint(lane):
    job = lane.service.enqueue_stage(**_request(lane))
    recovered = lane.service.recover_stage(tenant=lane.tenant, change_set_id=job["change_set_id"])
    assert recovered["status"] == "found"
    assert recovered["job"]["target"] == "branch"


def test_failures_are_bounded_before_http_and_persistence():
    error = CustomizationServiceError("branch_package_invalid", 422, failures=[
        {"rule": "V17", "path": "main.csx", "message": "x" * 400}
        for _ in range(100)
    ])
    assert len(error.failures) == 50
    assert all(len(f["message"]) == 300 for f in error.failures)
