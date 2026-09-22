"""W4h S1 provider import and member source discovery HTTP contracts."""
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

import deps
from routers import ios_ship as router
from routers import ios_ship_provider as provider_router


ORG = "8f964070-76e9-485f-b960-009a9f318872"
PROJECT = "b8c20a0e-21d5-4082-9ecb-b76852662e52"
BASE = f"/api/projects/{PROJECT}/ios"
ENTRY = {"catalog_key": "bakery-stock", "repository": "LEAF-Solar-Design/bakery-inventory",
         "source_revision": "source-1", "source_sha256": "a" * 64,
         "bundle_identifier": "ai.leafautomation.bakerystock", "marketing_version": "1.0",
         "build_number": "1", "producer_receipt_digest": "b" * 64}
APPROVAL = {"revision": "r1", **{key: ENTRY[key] for key in (
    "source_revision", "source_sha256", "bundle_identifier", "marketing_version", "build_number")}}


class StoreError(ValueError):
    def __init__(self, code, message="refused", setup_action=None):
        super().__init__(message)
        self.code = code
        self.setup_action = setup_action


class CatalogConflict(StoreError):
    def __init__(self):
        super().__init__("catalog_conflict")


class ProjectUnavailable(StoreError):
    def __init__(self):
        super().__init__("project_unavailable")


class LaunchConflict(StoreError):
    pass


class FakeStore:
    def __init__(self):
        self.approvals = []
        self.registrations = []
        self.error = None
        self.latest = {"execution_id": "execution-1", "revision": "r2", "status": "running"}

    def project_accessible(self, org, project, subject):
        return org == ORG and project == PROJECT and subject in {"owner", "editor"}

    def project_org(self, project):
        return ORG if project == PROJECT else None

    def resolve_ship_owner(self, org, project, subject):
        assert org == ORG and project == PROJECT
        return "owner-binding" if subject == "owner" else None

    def register_source_catalog_entry(self, org, project, entry):
        self.registrations.append((org, project, entry))
        if self.error:
            raise self.error
        return {"catalog_id": "catalog-1", **entry}

    def list_source_catalog(self, org, project):
        assert (org, project) == (ORG, PROJECT)
        return [ENTRY]

    def list_revision_approvals(self, org, project):
        assert (org, project) == (ORG, PROJECT)
        return [{"approval_id": "approval-1", **APPROVAL}]

    def approve_catalog_revision(self, org, project, **fields):
        assert (org, project) == (ORG, PROJECT)
        self.approvals.append(fields)
        if self.error:
            raise self.error
        return {"approval_id": "approval-1", "approved": True, **fields}

    def latest_execution_for_project(self, org, tenant, project):
        assert (org, tenant, project) == (ORG, "tenant-1", PROJECT)
        return self.latest


def _client(subject="owner"):
    app = FastAPI()
    app.include_router(router.router)
    tenant = SimpleNamespace(tenant_id="tenant-1", org_id=ORG if subject else None, subject=subject)
    app.dependency_overrides[deps.require_tenant] = lambda: tenant
    return TestClient(app, raise_server_exceptions=False)


def _code(response):
    return response.json()["error"]["error_code"]


# S1 row8
def test_provider_catalog_requires_auth_and_exact_body(monkeypatch):
    store = FakeStore()
    monkeypatch.setattr(router, "_store", lambda: store)
    monkeypatch.setattr(provider_router, "_CONFIG", SimpleNamespace(
        provider_id="provider-1", read_token=lambda: "test-provider-bearer"))
    client = _client()
    url = "/internal/v1/ios-ship/source-catalog"
    body = {"org_id": ORG, "project_id": PROJECT, **ENTRY}
    headers = {"Authorization": "Bearer test-provider-bearer", "X-Leaf-Ios-Ship-Provider": "provider-1"}
    assert client.post(url, json=body).status_code == 401
    assert client.post(url, json=body, headers={"Authorization": headers["Authorization"]}).status_code == 401
    assert client.post(url, json=body, headers={
        **headers, "X-Leaf-Ios-Ship-Provider": "wrong-provider"}).status_code == 401
    assert store.registrations == []
    registration_count = len(store.registrations)
    response = client.post(url, json=body, headers={
        **headers, "Authorization": "Bearer wrong-provider-bearer"})
    assert response.status_code == 401
    assert response.json() == {"ok": False, "error": {
        "error_code": "provider_unauthorized", "message": "provider authentication failed",
        "retryable": False}}
    assert len(store.registrations) == registration_count
    response = client.post(url, json=body, headers=headers)
    assert response.status_code == 200
    assert response.json() == {"ok": True, "entry": {"catalog_id": "catalog-1", **ENTRY}}
    assert store.registrations == [(ORG, PROJECT, ENTRY)]
    assert client.post(url, json={**body, "extra": "no"}, headers=headers).status_code == 400
    assert client.post(url, json=ENTRY, headers=headers).status_code == 400
    for error, status in ((CatalogConflict(), 409), (ProjectUnavailable(), 404)):
        store.error = error
        response = client.post(url, json=body, headers=headers)
        assert response.status_code == status and _code(response) == error.code
    monkeypatch.setattr(provider_router, "_CONFIG", None)
    assert client.post(url, json=body, headers=headers).status_code == 503


# S1 row9
def test_sources_allow_members_and_auth_off_but_hide_foreign_projects(monkeypatch):
    store = FakeStore()
    monkeypatch.setattr(router, "_store", lambda: store)
    router.set_provider_catalog(None)
    for subject in ("owner", "editor", None):
        response = _client(subject).get(BASE + "/sources")
        assert response.status_code == 200
        assert response.json() == {"ok": True, "sources": [ENTRY],
                                   "approvals": [{"approval_id": "approval-1", **APPROVAL}],
                                   "sync": {"status": "provider_unavailable" if subject else "auth_off",
                                            "registered": 0, "conflicts": [], "unpinned": [], "refused": []},
                                   "can_approve": subject == "owner"}
    response = _client("outsider").get(BASE + "/sources", headers={"X-Org-Id": ORG})
    assert response.status_code == 404 and _code(response) == "project_unavailable"


# S1 row10
def test_only_owner_can_approve_and_secret_body_never_reaches_store(monkeypatch):
    store = FakeStore()
    events = []
    monkeypatch.setattr(router, "_store", lambda: store)
    monkeypatch.setattr(router, "_ship_event", lambda *args: events.append(args))
    response = _client().post(BASE + "/approvals", json=APPROVAL)
    assert response.status_code == 200
    assert response.json()["approval"]["approved_by"] == "owner-binding"
    assert store.approvals == [{**APPROVAL, "approved_by": "owner-binding"}]
    for subject, status, code in (("editor", 403, "approval_forbidden"),
                                  ("outsider", 404, "project_unavailable"),
                                  (None, 403, "approval_forbidden")):
        response = _client(subject).post(BASE + "/approvals", json=APPROVAL)
        assert response.status_code == status and _code(response) == code
    assert _client().post(BASE + "/approvals", json={**APPROVAL, "extra": "no"}).status_code in {400, 422}
    for code in ("catalog_entry_missing", "approval_tuple_mismatch"):
        store.error = StoreError(code, "source_sha256", "import-ios-source" if code == "catalog_entry_missing" else None)
        response = _client().post(BASE + "/approvals", json=APPROVAL)
        assert response.status_code == 409 and _code(response) == code
        if code == "catalog_entry_missing":
            assert response.json()["error"]["setup_action"] == "import-ios-source"
        else:
            assert response.json()["error"]["message"] == "source_sha256"
    store.approvals.clear()

    def untouched():
        raise AssertionError("secret body must be refused before store lookup")

    monkeypatch.setattr(router, "_store", untouched)
    response = _client().post(BASE + "/approvals", json={**APPROVAL, "source_revision": "-----BEGIN material"})
    assert response.status_code == 400 and _code(response) == "secret_shaped_field"
    assert store.approvals == []
    assert any(event[2] == "approval.recorded" for event in events)
    assert any(event[2] == "approval.refused" for event in events)


# S1 row11
def test_latest_execution_returns_row_or_null_for_member(monkeypatch):
    store = FakeStore()
    monkeypatch.setattr(router, "_store", lambda: store)
    response = _client("editor").get(BASE + "/executions/latest")
    assert response.status_code == 200
    assert response.json() == {"ok": True, "execution": store.latest}
    store.latest = None
    response = _client().get(BASE + "/executions/latest")
    assert response.status_code == 200 and response.json() == {"ok": True, "execution": None}
    response = _client("outsider").get(BASE + "/executions/latest")
    assert response.status_code == 404 and _code(response) == "project_unavailable"


# S1 row14
def test_consumed_approval_returns_conflict_and_refused_event(monkeypatch):
    store = FakeStore()
    store.error = LaunchConflict(
        "approval_consumed",
        "that revision's approval was consumed by a launch; approve a new revision",
        setup_action="approve-new-revision")
    events = []
    monkeypatch.setattr(router, "_store", lambda: store)
    monkeypatch.setattr(router, "_ship_event", lambda *args: events.append(args))
    response = _client().post(BASE + "/approvals", json=APPROVAL)
    assert response.status_code == 409 and _code(response) == "approval_consumed"
    assert response.json()["error"]["setup_action"] == "approve-new-revision"
    assert events == [("tenant-1", "account", "approval.refused", "approval", "approval_consumed")]


def _b4a_projection():
    return {"schema": "leaf.ios-ship-source-catalog.v1", "project_id": PROJECT,
            "catalog_key": "exzachly", "status": "ok", "sources": [{
                "source_revision": "c76380846278cdfa4ffcb71b031dce33c7f139f0",
                "source_sha256": "41f1cd4e5bee84238973ea785c23e49888edd5154f385254c7781813bc6065c6",
                "producer_receipt_digest": "ce8186d075b4ca8877560442dba99777558e1385f5b5ae52256f8102fed193b3",
                "bundle_identifier": "com.exzachly.app", "marketing_version": "0.2.0",
                "build_number": "12", "repository": "https://github.com/Evan-Haug/ExZachly.git"}],
            "unpinned": [], "refused": []}


class B4aStore(FakeStore):
    def __init__(self):
        super().__init__()
        self.rows = []
        self.errors = {}

    def register_source_catalog_entry(self, org, project, entry):
        self.registrations.append((org, project, entry))
        error = self.errors.get(entry["source_revision"])
        if error:
            raise error
        for row in self.rows:
            if row["source_revision"] == entry["source_revision"]:
                if row != entry:
                    raise CatalogConflict()
                return row
        self.rows.append(dict(entry))
        return entry

    def list_source_catalog(self, org, project):
        assert (org, project) == (ORG, PROJECT)
        return list(self.rows)


def _b4a_setup(monkeypatch):
    project = "c6dbda41-f9bf-4f0f-984c-45f3199f4ca1"
    monkeypatch.setitem(globals(), "PROJECT", project)
    monkeypatch.setitem(globals(), "BASE", f"/api/projects/{project}/ios")
    store = B4aStore()
    projection = _b4a_projection()
    calls, events = [], []

    def catalog(project):
        calls.append(project)
        return projection

    monkeypatch.setattr(router, "_store", lambda: store)
    monkeypatch.setattr(router, "_PROVIDER_CATALOG", catalog)
    monkeypatch.setattr(router, "_ship_event", lambda *args: events.append(args))
    return store, projection, calls, events


def test_b4a_member_reconciles_exact_tuple(monkeypatch):
    store, projection, calls, events = _b4a_setup(monkeypatch)
    response = _client().get(BASE + "/sources")
    entry = {"catalog_key": projection["catalog_key"], **projection["sources"][0]}
    assert response.status_code == 200
    assert store.registrations == [(ORG, PROJECT, entry)]
    assert calls == [PROJECT]
    assert response.json()["sources"] == [entry]
    assert response.json()["sync"] == {"status": "ok", "registered": 1, "conflicts": [],
                                       "unpinned": [], "refused": []}
    assert response.json()["can_approve"] is True
    assert events == [("tenant-1", "account", "catalog.reconciled", "ok", None)]
    assert _client("editor").get(BASE + "/sources").json()["can_approve"] is False


def test_b4a_exact_replay_counts_without_duplicate(monkeypatch):
    store, projection, calls, events = _b4a_setup(monkeypatch)
    client = _client()
    client.get(BASE + "/sources")
    body = client.get(BASE + "/sources").json()
    assert len(store.registrations) == 2
    assert len(body["sources"]) == 1
    assert body["sync"]["registered"] == 1 and body["sync"]["conflicts"] == []


def test_b4a_tuple_conflict_preserves_stored_row(monkeypatch):
    store, projection, calls, events = _b4a_setup(monkeypatch)
    client = _client()
    client.get(BASE + "/sources")
    projection["sources"][0]["build_number"] = "13"
    body = client.get(BASE + "/sources").json()
    assert body["sync"]["registered"] == 0
    assert body["sync"]["conflicts"] == [projection["sources"][0]["source_revision"]]
    assert body["sources"][0]["build_number"] == "12"


def test_b4a_foreign_member_never_contacts_provider(monkeypatch):
    store, projection, calls, events = _b4a_setup(monkeypatch)
    response = _client("outsider").get(BASE + "/sources")
    assert response.status_code == 404 and _code(response) == "project_unavailable"
    assert calls == [] and store.registrations == []


def test_b4a_scope_mismatch_preserves_stored_sources(monkeypatch):
    store, projection, calls, events = _b4a_setup(monkeypatch)
    store.rows = [ENTRY]
    projection["project_id"] = "5ec5345a-0d85-4c3f-80e2-8ab99ae25c32"
    response = _client().get(BASE + "/sources")
    assert response.status_code == 200
    assert response.json()["sync"]["status"] == "unavailable"
    assert response.json()["sources"] == [ENTRY]
    assert store.registrations == [] and events == []


def test_b4a_provider_failure_keeps_stored_sources(monkeypatch):
    store, projection, calls, events = _b4a_setup(monkeypatch)
    store.rows = [ENTRY]

    def unavailable(project):
        raise RuntimeError("private provider failure")

    monkeypatch.setattr(router, "_PROVIDER_CATALOG", unavailable)
    response = _client().get(BASE + "/sources")
    assert response.status_code == 200
    assert response.json()["sync"]["status"] == "unavailable"
    assert response.json()["sources"] == [ENTRY]
    assert store.registrations == []
    assert "private" not in response.text


def test_b4a_missing_provider_keeps_stored_sources(monkeypatch):
    store, projection, calls, events = _b4a_setup(monkeypatch)
    store.rows = [ENTRY]
    router.set_provider_catalog(None)
    response = _client().get(BASE + "/sources")
    assert response.status_code == 200
    assert response.json()["sync"] == {"status": "provider_unavailable", "registered": 0,
                                       "conflicts": [], "unpinned": [], "refused": []}
    assert response.json()["sources"] == [ENTRY]
    assert calls == [] and store.registrations == []


def test_b4a_auth_off_never_contacts_provider(monkeypatch):
    store, projection, calls, events = _b4a_setup(monkeypatch)
    body = _client(None).get(BASE + "/sources").json()
    assert body["sync"]["status"] == "auth_off"
    assert body["can_approve"] is False
    assert calls == [] and store.registrations == []


def test_b4a_registration_project_loss_returns_404(monkeypatch):
    store, projection, calls, events = _b4a_setup(monkeypatch)
    store.errors[projection["sources"][0]["source_revision"]] = ProjectUnavailable()
    response = _client().get(BASE + "/sources")
    assert response.status_code == 404 and _code(response) == "project_unavailable"


def test_b4a_conflict_continues_other_revisions(monkeypatch):
    store, projection, calls, events = _b4a_setup(monkeypatch)
    revision = projection["sources"][0]["source_revision"]
    store.errors[revision] = CatalogConflict()
    projection["sources"].append({**projection["sources"][0], "source_revision": "a" * 40})
    projection["unpinned"] = ["b" * 40]
    projection["refused"] = [{"source_revision": "c" * 40, "reason": "invalid_metadata"}]
    body = _client().get(BASE + "/sources").json()
    assert len(store.registrations) == 2
    assert body["sync"] == {"status": "ok", "registered": 1, "conflicts": [revision],
                            "unpinned": projection["unpinned"], "refused": projection["refused"]}


def test_b4a_store_outage_stops_without_conflict(monkeypatch):
    store, projection, calls, events = _b4a_setup(monkeypatch)
    store.errors[projection["sources"][0]["source_revision"]] = RuntimeError("store offline")
    projection["sources"].append({**projection["sources"][0], "source_revision": "a" * 40})
    response = _client().get(BASE + "/sources")
    assert response.status_code == 200
    assert len(store.registrations) == 1
    assert response.json()["sync"]["status"] == "unavailable"
    assert response.json()["sync"]["conflicts"] == []
