"""Saved recipe edits on selected tables use independent guarded revisions."""
from copy import deepcopy
import json

import httpx
import pytest

from test_integration_element_save import recipe, rig, save
from tools_fallback import author_tool


ASKS = [
    "Apply my saved recipe edits to two tables and identify any revision conflicts.",
    "Update both selected tables with the saved recipe changes and report which saves succeeded.",
    "Use the saved recipe settings on both inspection tables and flag stale revisions.",
]


@pytest.mark.parametrize("ask,edits,stale", [
    (ASKS[0], {"row_limit": 7}, set()),
    (ASKS[1], {"title": "Roof observations"}, {"punch-list"}),
    (ASKS[2], {"sort": [{"field": "amount", "direction": "desc"}]}, {"rack-check", "punch-list"}),
])
def test_batch_recipe_author(save, ask, edits, stale):
    package, _, _ = author_tool(ask)
    assert package["engine_op"] == "batch_recipe_edit"
    originals = [
        {**recipe(), "id": "rack-check", "title": "Rack inspection"},
        {**recipe(), "id": "punch-list", "title": "Roof inspection"},
    ]
    history = {row["id"]: [] for row in originals}
    requests = []
    attempted = {}
    scope = f"/api/v1/projects/{save.auth.project_id}/integrations/elements"

    def peer(request):
        row = json.loads(request.content)
        versions = history[row["id"]]
        expected = request.headers.get("If-Match")
        requests.append((request.method, request.url.path, expected))
        attempted[row["id"]] = row
        if request.method == "PUT" and str(len(versions)) != expected:
            return httpx.Response(409)
        versions.append(row)
        return httpx.Response(200, json={"revision": len(versions), "element": row})

    save.svc.transport = httpx.MockTransport(peer)
    snapshots = [save.save({"recipe": row})["result"] for row in originals]
    snapshots[1] = save.save({"recipe": originals[1], "expectedRevision": 1})["result"]
    for row, snapshot in zip(originals, snapshots):
        if row["id"] in stale:
            newer = {**row, "title": "Other editor's title"}
            latest = save.save({"recipe": newer, "expectedRevision": snapshot["revision"]})
            assert latest["result"]["revision"] == snapshot["revision"] + 1
    before = deepcopy(history)
    requests.clear()
    attempted.clear()
    context = save.auth.tenant_id, save.auth.session_id, save.auth.turn_id
    result = save.svc.edit_table_recipes(ask, snapshots, edits, *context)
    assert result["status"] == ("saved" if not stale else "failed" if len(stale) == 2 else "partial")
    assert requests == [("PUT", f"{scope}/rack-check", "1"), ("PUT", f"{scope}/punch-list", "2")]
    assert len(result["tables"]) == 2
    for row, snapshot, item in zip(originals, snapshots, result["tables"]):
        row_id = row["id"]
        assert (item["id"], item["expectedRevision"]) == (row_id, snapshot["revision"])
        changed = deepcopy(row)
        if "title" in edits:
            changed["title"] = edits["title"]
        elif "row_limit" in edits:
            changed["props"]["row_limit"] = edits["row_limit"]
        else:
            changed["query"]["sort"] = edits["sort"]
        assert attempted[row_id] == changed
        if row_id in stale:
            assert item["outcome"]["error"]["code"] == "conflict"
            assert history[row_id] == before[row_id]
        else:
            assert item["outcome"]["result"] == {"revision": snapshot["revision"] + 1, "element": changed}
            assert history[row_id] == before[row_id] + [changed]


def test_invalid_batch_cannot_mutate_first_table(save):
    snapshots = [{"element": {**recipe(), "id": name}, "revision": 1}
                 for name in ("east", "west")]
    ctx = save.auth.tenant_id, save.auth.session_id, save.auth.turn_id
    for records, edits in [
        (snapshots[:1], {"row_limit": 5}),
        ([snapshots[0], {**snapshots[1], "revision": 0}], {"row_limit": 5}),
        ([snapshots[0], snapshots[0]], {"row_limit": 5}),
        (snapshots, {"id": "elsewhere"}),
        (snapshots, {"sort": [{"field": "unknown", "direction": "asc"}]}),
    ]:
        assert save.svc.edit_table_recipes(ASKS[0], records, edits, *ctx)["error"]["code"] == "invalid_input"
    assert not save.requests and not save.gates
