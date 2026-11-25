"""An inspection recipe that changes back to its old content still has a new revision."""
from copy import deepcopy
import json

import httpx
import pytest

from test_integration_element_save import recipe, rig, save


@pytest.mark.parametrize("ask,expected", [
    ("Retitle this inspection recipe as Roof defects.", ("title", "Roof defects")),
    ("Display 6 inspection rows per page.", ("rows", 6)),
    ("Sort my inspection rows by amount ascending.", ("sort", [{"field": "amount", "direction": "asc"}])),
])
def test_return_to_original_content_preserves_stale_guard(save, ask, expected):
    original = {**recipe(), "id": "inspection-table", "title": "Inspection table"}
    changed = {**original, "title": "Temporary alternate title"}
    history = []
    requests = []
    def peer(request):
        revision = request.headers.get("If-Match")
        requests.append((request.method, revision))
        if (revision is None and history) or (revision is not None and str(len(history)) != revision):
            return httpx.Response(409)
        element = json.loads(request.content)
        history.append(element)
        return httpx.Response(200, json={"revision": len(history), "element": element})

    save.svc.transport = httpx.MockTransport(peer)
    stale = save.save({"recipe": original})["result"]
    save.save({"recipe": changed, "expectedRevision": 1})
    current = save.save({"recipe": original, "expectedRevision": 2})["result"]
    assert stale["element"] == current["element"] and (stale["revision"], current["revision"]) == (1, 3)
    scope = save.auth.tenant_id, save.auth.session_id, save.auth.turn_id
    rejected = save.svc.edit_inspection_recipe(ask, stale, *scope)
    assert rejected["error"]["code"] == "conflict" and "result" not in rejected
    assert requests[-1] == ("PUT", "1") and history == [original, changed, original]
    accepted = save.svc.edit_inspection_recipe(ask, current, *scope)
    wanted = deepcopy(original)
    field, value = expected
    if field == "title":
        wanted["title"] = value
    elif field == "rows":
        wanted["props"]["row_limit"] = value
    else:
        wanted["query"]["sort"] = value
    assert accepted["result"] == {"element": wanted, "revision": 4}
    assert requests[-1] == ("PUT", "3") and history == [original, changed, original, wanted]
