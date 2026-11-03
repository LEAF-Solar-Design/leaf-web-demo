"""Tenant isolation for TEMPLATE-authored tools (PR #131, security finding F3).

The hazard this suite pins down: /api/author's template path used to persist every
tool to a FLAT ``server/authored/<name>.py`` and dedup the in-memory store on the
tool NAME alone. Tool names are derived from the description, so two tenants asking
for the same thing get the same name — and then:

  * tenant B's write OVERWROTE tenant A's file, while A's catalog entry still
    pointed at that path, so A's runs executed B's code under A's identity; and
  * the name-only dedup EVICTED A's catalog entry, letting B shadow A's tool.

The fix scopes both halves: the body is written under a per-tenant subdirectory
(``authored/<sha256(tenant_id)[:32]>/<name>.py``) and the store dedups on
(tenant_id, name), with ``deps.all_tools()`` only folding entries the requesting
tenant owns.

These tests drive the REAL /api/author route in-process and then execute through the
REAL loader, so they fail if either half is loosened. `test_dynamic_loader.py` covers
the single-tenant "the FILE is the tool" contract; this file covers what happens when
a SECOND tenant shows up.
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import pytest


@pytest.fixture
def authored_view_lane(monkeypatch, tmp_path):
    """Independent published Git + durable pin lane; never the template fixture.

    Membership and session storage are explicit platform boundary doubles. Git,
    pin storage, catalog provenance folding and the HTTP route are real.
    """
    import json
    import subprocess
    from types import SimpleNamespace
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    import customization_service as catalogs
    from customization_models import ChangeState
    from customization_store import SQLiteCustomizationStore
    import deps
    import platform_link
    import session_store
    from routers.tools import router

    tenant = "11111111-1111-4111-8111-111111111111"
    project = "22222222-2222-4222-8222-222222222222"
    session = {"session_id": "view-session", "tenant_id": f"project:{tenant}:{project}",
               "org_id": tenant, "project_id": project}
    sessions = {session["session_id"]: session}
    membership = {"allowed": True}
    def access(actor, requested, *, write, **_kwargs):
        if str(actor) != tenant or requested != project:
            raise LookupError("private project")
        if not membership["allowed"]:
            raise platform_link.ProjectSessionForbidden("private membership")
        assert write is False
        return tenant
    monkeypatch.setattr(platform_link, "require_project_access", access)
    monkeypatch.setattr(session_store, "get_session", sessions.get)
    monkeypatch.setenv("LEAF_CUSTOMIZATION_DB", str(tmp_path / "catalog.db"))
    monkeypatch.setenv("LEAF_CUSTOMIZATION_STORE", "sqlite")
    monkeypatch.setenv("LEAF_TENANT_GIT_DIR", str(tmp_path / "bare"))
    monkeypatch.setenv("LEAF_EFFECTIVE_TENANTS_DIR", str(tmp_path / "effective"))
    monkeypatch.setenv("LEAF_CUSTOMIZATION_R5_MODE", "off")
    monkeypatch.setenv("LEAF_CUSTOMIZATION_R6_MODE", "off")
    monkeypatch.setattr(deps, "_AUTHORED", [])
    for attr in ("ENGINE_REGISTRY", "CATALOG_TOOLS_STORE", "WRITE_TOOLS_STORE", "AUTHORED_STORE"):
        path = tmp_path / f"{attr}.json"
        path.write_text('{"tools": []}', encoding="utf-8")
        monkeypatch.setattr(deps, attr, path)
    monkeypatch.setattr(deps.solar_tools, "registry_records", lambda: [])
    repo = tmp_path / "working"
    repo.mkdir()
    bare = tmp_path / "bare" / f"{tenant}.git"
    bare.parent.mkdir()
    def git(*args):
        return subprocess.check_output(["git", "-C", str(repo), *args], stderr=subprocess.PIPE).decode().strip()
    git("init")
    git("config", "user.email", "view-fixture@example.invalid")
    git("config", "user.name", "View fixture")
    subprocess.check_call(["git", "init", "--bare", str(bare)], stdout=subprocess.DEVNULL)
    git("remote", "add", "origin", str(bare))
    store = SQLiteCustomizationStore(tmp_path / "catalog.db")
    store.initialize()
    serial = [0]
    html = '<!doctype html><meta charset="utf-8"><h1>Arbitrary café view</h1><script>document.title="view"</script>\n'
    tool = {"name": "fixture-view", "kind": "view", "entry": "tools/fixture-view/nested/custom.htm",
            "version": "1.0.0", "description": "Registered HTML", "capabilities": [],
            "params": {"type": "object", "properties": {}}}
    def publish(*, body=None, changes=None, symlink=False):
        serial[0] += 1
        row = {**tool, **(changes or {})}
        path = repo / tool["entry"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.unlink(missing_ok=True)
        if symlink:
            path.symlink_to("../../private.html")
        else:
            path.write_bytes(html.encode() if body is None else body)
        raw = json.dumps({"tools": [row]}, ensure_ascii=False).encode()
        (repo / "registry.json").write_bytes(raw)
        git("add", ".")
        git("commit", "--allow-empty", "-m", f"published view {serial[0]}")
        commit = git("rev-parse", "HEAD")
        git("push", "origin", "HEAD:refs/heads/main")
        change = store.create_change_set(tenant_id=tenant, idempotency_key=f"create-{serial[0]}",
            base_commit=commit, desired_platform_release="platform@sha256:abc",
            workspace_contract_digest="d" * 64, author_subject="fixture-author")
        def advance(state):
            nonlocal change
            change = store.transition(tenant_id=tenant, change_set_id=change.change_set_id,
                next_state=state, expected_version=change.version,
                idempotency_key=f"{serial[0]}-{state.value}", approver_subject="fixture-approver")
        advance(ChangeState.STAGING)
        change = store.record_staged(tenant_id=tenant, change_set_id=change.change_set_id,
            expected_version=change.version, idempotency_key=f"staged-{serial[0]}", staged_commit=commit,
            catalog_digest=hashlib.sha256(raw).hexdigest(), platform_release="platform@sha256:abc",
            workspace_contract_digest="d" * 64)
        for state in (ChangeState.AWAITING_APPROVAL, ChangeState.APPROVED, ChangeState.PUBLISHING):
            advance(state)
        store.publish(tenant_id=tenant, change_set_id=change.change_set_id, expected_version=change.version,
                      idempotency_key=f"publish-{serial[0]}")
        return {"project_id": project, "session_id": session["session_id"], "catalog_commit": commit,
                "effective_catalog_digest": hashlib.sha256(raw).hexdigest(),
                "tool_manifest_sha256": deps.catalog_tool_digest(row), "catalog_digest": deps.catalog_tool_digest(row)}
    query = publish()
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[deps.require_tenant] = lambda: tenant
    client = TestClient(app)
    def get(params=None, actor=tenant):
        app.dependency_overrides[deps.require_tenant] = lambda: actor
        return client.get("/api/tools/fixture-view/view-source", params=query if params is None else params)
    return SimpleNamespace(get=get, query=query, publish=publish, html=html, session=session,
                           sessions=sessions, membership=membership, store=store, tenant=tenant, bare=bare)


def test_authored_view_pinned_source(authored_view_lane, monkeypatch):
    import tool_loader
    import broker_client
    def forbidden(*_args, **_kwargs):
        pytest.fail("authored view must not execute Python or broker work")
    monkeypatch.setattr(tool_loader, "run_tool_dynamic", forbidden)
    monkeypatch.setattr(broker_client, "run_via_broker", forbidden)
    response = authored_view_lane.get()
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["html"] == authored_view_lane.html
    assert body["sha256"] == hashlib.sha256(authored_view_lane.html.encode()).hexdigest()
    assert body["size_bytes"] == len(authored_view_lane.html.encode())
    assert body["media_type"] == "text/html"
    assert body["entry"] == "tools/fixture-view/nested/custom.htm"
    assert response.headers["cache-control"] == "no-store"
    assert all(body[key] == value for key, value in authored_view_lane.query.items())


def test_authored_view_ignores_materialized_source(authored_view_lane):
    import customization_service as catalogs
    lane = authored_view_lane
    assert lane.get().status_code == 200
    cached = catalogs.effective_catalog_dir(lane.tenant)
    (cached / "tools/fixture-view/nested/custom.htm").write_text("<h1>Wrong checkout bytes</h1>", encoding="utf-8")
    response = lane.get()
    assert response.status_code == 200, response.text
    assert response.json()["html"] == lane.html


def test_authored_view_requires_winning_tenant_provenance(authored_view_lane, monkeypatch):
    import deps
    lane = authored_view_lane
    winners = deps.effective_tools_with_provenance(lane.tenant)
    monkeypatch.setattr(deps, "effective_tools_with_provenance", lambda _tenant: [
        (tool, deps.TOOL_SOURCE_AUTHORED if tool.get("name") == "fixture-view" else source)
        for tool, source in winners])
    response = lane.get()
    assert response.status_code == 409, response.text
    assert "html" not in response.json()


@pytest.mark.parametrize("case", ["tenant", "project", "session", "legacy", "missing_org", "membership", "stale", "manifest", "registry"])
def test_authored_view_authority_refusals(authored_view_lane, case):
    lane = authored_view_lane
    query = dict(lane.query)
    actor = lane.tenant
    if case == "tenant": actor = "33333333-3333-4333-8333-333333333333"
    if case == "project": query["project_id"] = "44444444-4444-4444-8444-444444444444"
    if case == "session": query["session_id"] = "missing"
    if case == "legacy":
        lane.session.pop("org_id"); lane.session.pop("project_id"); lane.session["tenant_id"] = lane.tenant
    if case == "missing_org": lane.session.pop("org_id")
    if case == "membership": lane.membership["allowed"] = False
    if case == "stale": query["catalog_commit"] = "0" * 40
    if case == "manifest": query.update(tool_manifest_sha256="sha256:" + "0" * 64, catalog_digest="sha256:" + "0" * 64)
    if case == "registry":
        import sqlite3
        with sqlite3.connect(lane.store.database_path) as conn:
            conn.execute("UPDATE effective_catalogs SET catalog_digest = ?", ("0" * 64,))
        query["effective_catalog_digest"] = "0" * 64
    response = lane.get(query, actor)
    assert response.status_code in {403, 404, 409, 422}, response.text
    assert "html" not in response.json()
    assert str(lane.bare) not in response.text


@pytest.mark.parametrize("case", ["script", "traversal", "symlink", "oversize", "utf8"])
def test_authored_view_source_refusals(authored_view_lane, case, monkeypatch):
    import customization_service as catalogs
    lane = authored_view_lane
    kwargs = {}
    if case == "script": kwargs["changes"] = {"kind": "script"}
    if case == "traversal": kwargs["changes"] = {"entry": "tools/../private.html"}
    if case == "symlink": kwargs["symlink"] = True
    if case == "oversize": kwargs["body"] = b"x" * (1024 * 1024 + 1)
    if case == "utf8": kwargs["body"] = b"\xff"
    query = lane.publish(**kwargs)
    source_oid = catalogs._git(lane.bare, "rev-parse", f"{query['catalog_commit']}:tools/fixture-view/nested/custom.htm")
    original = catalogs._git_blob
    def bounded(bare, identity):
        # Registry reads may use the normal catalog helper. The view source
        # cannot be read at all after an oversize tree stat.
        if case == "oversize" and identity == source_oid:
            pytest.fail("oversize source blob was read")
        return original(bare, identity)
    monkeypatch.setattr(catalogs, "_git_blob", bounded)
    response = lane.get(query)
    assert response.status_code in {422, 503}, response.text
    assert "html" not in response.json()


@pytest.mark.parametrize("change", ["pin", "session", "membership"])
def test_authored_view_rechecks_after_read(authored_view_lane, monkeypatch, change):
    import customization_service as catalogs
    lane = authored_view_lane
    original = catalogs._git_blob
    def read(bare, identity):
        raw = original(bare, identity)
        if ":" not in identity and raw == lane.html.encode():
            if change == "pin":
                monkeypatch.setattr(catalogs, "effective_catalog_pin", lambda _tenant: None)
            elif change == "session": lane.sessions.clear()
            else: lane.membership["allowed"] = False
        return raw
    monkeypatch.setattr(catalogs, "_git_blob", read)
    response = lane.get()
    assert response.status_code in {403, 404, 409}, response.text

SERVER_DIR = Path(__file__).resolve().parent.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

AUTHORED_DIR = SERVER_DIR / "authored"

# Distinct ids that no real lane uses, so their hashed directories cannot collide
# with an operator's authored tools and are safe to delete in teardown.
TENANT_A = "iso-probe-tenant-a"
TENANT_B = "iso-probe-tenant-b"
TENANT_C = "iso-probe-tenant-c"          # authors nothing; must see neither A nor B
DESCRIPTION = "list all layer names in the drawing"

_SENTINEL = ('def run(intake, params):\n'
             '    return ({{"who": "{who}", "layers": [], "count": 0}}, None)\n')


def authored_tenant_dir(tenant_id: str) -> Path:
    """Mirror of routers/author.py._legacy_author's per-tenant directory derivation."""
    return AUTHORED_DIR / hashlib.sha256(tenant_id.encode("utf-8")).hexdigest()[:32]


@pytest.fixture
def authored_lane(monkeypatch, tmp_path):
    """A clean authored store + a real (but disposable) per-tenant file lane.

    The STORE is redirected to tmp so the operator's authored_tools.json is untouched.
    The FILES deliberately land in the real server/authored/ tree — that is the path
    the loader resolves against — under probe-tenant directories.

    TEARDOWN IS OWNERSHIP-SCOPED BY NAME, deliberately not a blanket rmtree of those
    hashed directories, and not "everything that appeared since the snapshot" either:
    both would delete a body this test never created if a real tenant ever used a probe
    id, or if any other process wrote into one of these directories while the test ran.
    Authoring is deterministic, so the ONLY file this fixture can produce in a probe
    directory is `<name derived from DESCRIPTION>.py`. Remove exactly that, then drop
    the directory only if this fixture created it and left it empty.
    """
    import deps
    monkeypatch.delenv("LEAF_AUTHOR_HARNESS_URL", raising=False)  # force the template path
    monkeypatch.delenv("LEAF_AUTH_LIVE", raising=False)           # X-Tenant-Id header stub
    monkeypatch.delenv("LEAF_TENANTS_DIR", raising=False)         # no tenant-repo fold
    monkeypatch.delenv("LEAF_TENANT_REPO", raising=False)
    monkeypatch.setattr(deps, "AUTHORED_STORE", tmp_path / "authored.json")
    monkeypatch.setattr(deps, "_AUTHORED", [])

    import tools_fallback
    # the one filename authoring can produce here, derived exactly as the router does
    owned = f"{tools_fallback.author_tool(DESCRIPTION)[0]['name']}.py"
    probe_dirs = {tid: authored_tenant_dir(tid) for tid in (TENANT_A, TENANT_B, TENANT_C)}
    we_created = {tid: not d.exists() for tid, d in probe_dirs.items()}
    # STALENESS GUARD: clear that filename up front too. A run killed mid-test leaves a
    # body behind, and the persistence assertions below would then pass on residue even
    # if the author route had stopped writing files at all.
    for d in probe_dirs.values():
        (d / owned).unlink(missing_ok=True)

    from fastapi.testclient import TestClient
    from app import app
    try:
        yield TestClient(app)
    finally:
        for tid, d in probe_dirs.items():
            if not d.exists():
                continue
            (d / owned).unlink(missing_ok=True)          # only the file we can have made
            if we_created[tid] and not any(d.iterdir()):  # ours, and nothing else is in it
                d.rmdir()


def _author_as(client, tenant_id: str) -> dict:
    r = client.post("/api/author", json={"description": DESCRIPTION},
                    headers={"X-Tenant-Id": tenant_id})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["source"] == "template", body["source"]
    return body["tool"]


# --------------------------------------------------------------------------- #
# F3 (a): two tenants, one tool name -> two physically distinct bodies
# --------------------------------------------------------------------------- #
def test_same_named_authored_tools_get_per_tenant_files(authored_lane):
    a = _author_as(authored_lane, TENANT_A)
    b = _author_as(authored_lane, TENANT_B)

    # The collision this suite exists to cover must actually occur, or every
    # assertion below would pass vacuously.
    assert a["name"] == b["name"], (a["name"], b["name"])
    name = a["name"]

    fa = authored_tenant_dir(TENANT_A) / f"{name}.py"
    fb = authored_tenant_dir(TENANT_B) / f"{name}.py"
    assert fa.exists() and fb.exists()
    assert fa != fb
    assert a["entry"] != b["entry"]
    assert a["entry"] == f"authored/{fa.parent.name}/{fa.name}", a["entry"]
    assert b["entry"] == f"authored/{fb.parent.name}/{fb.name}", b["entry"]
    # neither tenant's body may also land at the shared flat path
    assert not (AUTHORED_DIR / f"{name}.py").exists()

    # B authoring second must NOT have evicted A's catalog entry (name-only dedup).
    import deps
    owners = {t.get("tenant_id") for t in deps._AUTHORED if t["name"] == name}
    assert owners == {TENANT_A, TENANT_B}, owners


# --------------------------------------------------------------------------- #
# F3 (b): the catalog only surfaces the requesting tenant's own authored tool
# --------------------------------------------------------------------------- #
def test_catalog_scopes_authored_tools_to_their_owner(authored_lane):
    import deps
    a = _author_as(authored_lane, TENANT_A)
    b = _author_as(authored_lane, TENANT_B)
    name = a["name"]

    def entry_for(tenant_id):
        return next((t["entry"] for t in deps.all_tools(tenant_id) if t["name"] == name), None)

    # each owner sees its OWN body, never the other's
    assert entry_for(TENANT_A) == a["entry"]
    assert entry_for(TENANT_B) == b["entry"]
    # a tenant that authored nothing sees neither
    assert entry_for(TENANT_C) is None
    assert name not in {t["name"] for t in deps.all_tools(TENANT_C)}

    # and over the real catalog route, not just the fold
    r = authored_lane.get("/api/tools", headers={"X-Tenant-Id": TENANT_C})
    assert r.status_code == 200, r.text
    assert name not in {t["name"] for t in r.json()["tools"]}


# --------------------------------------------------------------------------- #
# F3 (c): execution follows the caller's own file, so B cannot hijack A's run
# --------------------------------------------------------------------------- #
def test_each_tenant_executes_its_own_authored_body(authored_lane):
    import deps
    from tool_loader import resolve_local_file, run_tool_dynamic

    a = _author_as(authored_lane, TENANT_A)
    b = _author_as(authored_lane, TENANT_B)
    name = a["name"]

    fa = authored_tenant_dir(TENANT_A) / f"{name}.py"
    fb = authored_tenant_dir(TENANT_B) / f"{name}.py"
    # Give each tenant a body that names itself. Under the old flat layout these two
    # writes were the SAME file, so the second would decide both tenants' results.
    fa.write_text(_SENTINEL.format(who=TENANT_A), encoding="utf-8")
    fb.write_text(_SENTINEL.format(who=TENANT_B), encoding="utf-8")

    tool_a = next(t for t in deps.all_tools(TENANT_A) if t["name"] == name)
    tool_b = next(t for t in deps.all_tools(TENANT_B) if t["name"] == name)

    assert resolve_local_file(tool_a, TENANT_A) == fa.resolve()
    assert resolve_local_file(tool_b, TENANT_B) == fb.resolve()

    env_a = run_tool_dynamic(tool_a, {}, {}, aps_live=False, da=None, tenant_id=TENANT_A)
    env_b = run_tool_dynamic(tool_b, {}, {}, aps_live=False, da=None, tenant_id=TENANT_B)
    assert env_a["ok"] is True and env_a["result"].get("who") == TENANT_A, env_a["result"]
    assert env_b["ok"] is True and env_b["result"].get("who") == TENANT_B, env_b["result"]


# --------------------------------------------------------------------------- #
# F3 (d): a MISSING per-tenant body must not fall back onto a shared file
#
# The per-tenant directory only isolates tenants while resolution insists on it.
# resolve_local_file's basename fallback roots (authored/<name>, builtins/<name>)
# threw the directory away, so a tenant whose own body was gone — never re-authored
# after the pre-#131 upgrade, or a wiped volume) silently resolved onto a LEGACY
# FLAT authored/<name>.py that some OTHER tenant had written, and executed it under
# this tenant's identity. A missing body must be a miss, not someone else's code.
# --------------------------------------------------------------------------- #
def test_missing_tenant_body_does_not_fall_back_to_a_shared_file(authored_lane):
    from tool_loader import AUTHORED_DIR as LOADER_AUTHORED_DIR, resolve_local_file

    a = _author_as(authored_lane, TENANT_A)
    name = a["name"]
    fa = authored_tenant_dir(TENANT_A) / f"{name}.py"
    assert fa.exists()
    assert resolve_local_file(a, TENANT_A) == fa.resolve()   # the healthy case first

    # tenant A's OWN body disappears, while its catalog entry survives...
    fa.unlink()
    # ...and a legacy flat file of the same name, from before the per-tenant layout,
    # is still sitting in the shared directory.
    flat = LOADER_AUTHORED_DIR / f"{name}.py"
    assert not flat.exists(), f"unexpected pre-existing flat body: {flat}"
    flat.write_text(_SENTINEL.format(who="SOME-OTHER-TENANT"), encoding="utf-8")
    try:
        assert resolve_local_file(a, TENANT_A) is None, (
            "a missing per-tenant body fell back onto a shared authored file"
        )
    finally:
        flat.unlink(missing_ok=True)

    # the same must hold for the builtins/ basename root, which would otherwise
    # launder a tenant tool onto a trusted-looking builtin path.
    builtin_named = dict(a, entry=f"authored/{authored_tenant_dir(TENANT_A).name}/list_layers.py")
    assert resolve_local_file(builtin_named, TENANT_A) is None

    # nor may a `script` stand in for the missing entry. In a tenant-repo registry the
    # tenant writes `script` itself, so a stand-in could name a body this package does
    # not own, including another tenant's.
    assert resolve_local_file(dict(a, script="builtins/list_layers.py"), TENANT_A) is None
    other = authored_tenant_dir(TENANT_B).name
    assert resolve_local_file(dict(a, script=f"authored/{other}/{name}.py"), TENANT_A) is None


# --------------------------------------------------------------------------- #
# F3 (e): a DANGLING package must not be reclassified as trusted
#
# broker.py's production containment gate denies only UNtrusted tools, so what
# "trusted" means decides what runs. A package that declares a local Python body
# which does not resolve is dangling, not APS-only: while resolution still fell back,
# it landed on a builtin and was judged untrusted there (its identity is absent from
# the platform registry). Exact-or-miss must not flip that to trusted, or a dangling
# tenant package walks through the gate that exists to stop it.
# --------------------------------------------------------------------------- #
def test_a_dangling_local_entry_is_not_trusted(authored_lane):
    from tool_loader import is_trusted_builtin_tool, resolve_local_file

    a = _author_as(authored_lane, TENANT_A)
    (authored_tenant_dir(TENANT_A) / f"{a['name']}.py").unlink()   # body gone, entry stays
    assert resolve_local_file(a, TENANT_A) is None
    assert is_trusted_builtin_tool(a, TENANT_A) is False, (
        "a dangling authored package was classified trusted, which skips the "
        "production containment gate in broker.py"
    )

    # same for a tenant-repo package whose body is gone...
    dangling_repo = {"name": "probe-repo-tool", "engine_op": "count_by_layer",
                     "entry": "tools/probe-missing/tool.py"}
    assert is_trusted_builtin_tool(dangling_repo, TENANT_A) is False

    # ...while a package that never claimed local Python is still trusted, because
    # nothing of its runs in this process.
    assert is_trusted_builtin_tool({"name": "probe-aps", "kind": "appbundle"}, TENANT_A) is True
