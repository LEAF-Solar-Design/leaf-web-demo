"""Exercise the project reader's membership gate against a disposable revision store."""
from contextlib import closing
import json
import sqlite3
from types import SimpleNamespace
from uuid import UUID

import httpx
import pytest

from test_integration_element_save import recipe, rig, save
import deps
from leaf_platform import project_lifecycle
import platform_link
from tools_fallback import author_tool


@pytest.mark.parametrize("ask", [
    "Recover the missing receipt for my inspection recipe save.",
    "Confirm whether my table edit was saved after the connection dropped.",
    "Retry my recipe save after the response timed out.",
])
def test_recovery_respects_current_project_read_access(save, tmp_path, monkeypatch, ask):
    path = tmp_path / "access.sqlite"
    element = {**recipe(), "id": "inspection-register"}
    desired = {**element, "title": "West roof"}
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("CREATE TABLE grants (project TEXT, binding TEXT, role TEXT)")
        db.execute("INSERT INTO grants VALUES (?,?,?)", (save.auth.project_id, save.auth.binding_id, "reader"))
        db.execute("CREATE TABLE versions (revision INTEGER PRIMARY KEY, body TEXT)")
        db.execute("INSERT INTO versions VALUES (1,?)", (json.dumps(element),))
    def require_role(org, project, binding, *, write):
        assert str(org) == save.auth.tenant_id and not write
        with closing(sqlite3.connect(path)) as db:
            role = db.execute("SELECT role FROM grants WHERE project=? AND binding=?",
                              (str(project), str(binding))).fetchone()
        if not role:
            raise project_lifecycle.LifecycleForbidden("project read access revoked")
        return role[0]
    monkeypatch.setattr(project_lifecycle, "require_project_role", require_role)
    reads, writes = [], []
    def reader(authority, identity, rev):
        context = deps.TenantContext(authority.tenant_id, org_id=authority.tenant_id,
                                     subject=authority.subject, authority_resolved=True)
        binding = SimpleNamespace(platform_tenant_id=UUID(authority.tenant_id), binding_id=UUID(authority.binding_id))
        platform_link.require_project_access(context, authority.project_id, write=False, binding=binding)
        reads.append((identity, rev))
        with closing(sqlite3.connect(path)) as db:
            body, = db.execute("SELECT body FROM versions WHERE revision=?", (rev,)).fetchone()
        return {"element": json.loads(body), "revision": rev}

    def peer(request):
        assert request.headers["If-Match"] == "1" and request.method == "PUT"
        writes.append(json.loads(request.content))
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("INSERT INTO versions VALUES (2,?)", (request.content.decode(),))
        raise httpx.ReadTimeout("reply lost after commit")
    save.svc.transport = httpx.MockTransport(peer)
    scope = save.auth.tenant_id, save.auth.session_id, save.auth.turn_id
    assert save.svc.edit_inspection_recipe("Retitle the inspection table as West roof.",
        {"element": element, "revision": 1}, *scope)["error"]["code"] == "outcome_unknown"
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("DELETE FROM grants")
    args = {"recipe": desired, "expectedRevision": 1}
    assert author_tool(ask)[0]["engine_op"] == "recipe_save_recovery"
    denied = save.svc.recover_recipe_save(ask, args, *scope, read_revision=reader)
    assert denied["error"]["code"] == "denied" and "result" not in denied
    assert reads == [] and writes == [desired]
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("INSERT INTO grants VALUES (?,?,?)", (save.auth.project_id, save.auth.binding_id, "reader"))
    assert save.svc.recover_recipe_save(ask, args, *scope, read_revision=reader) == {
        "result": {"element": desired, "revision": 2}}
    assert reads == [(element["id"], 2)] and writes == [desired]
    with closing(sqlite3.connect(path)) as db:
        assert [(r, json.loads(b)) for r, b in db.execute("SELECT * FROM versions ORDER BY revision")] == [
            (1, element), (2, desired)]
