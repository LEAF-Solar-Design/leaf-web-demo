import json

from fastapi.responses import JSONResponse

import deps
from routers import jobs as jobs_router
from routers import tools as tools_router


def _tool(description="original"):
    return {
        "name": "catalog-bound-tool",
        "description": description,
        "kind": "script",
        "capabilities": ["drawing.read"],
        "params": {"type": "object", "properties": {}},
        "default_params": {},
    }


def test_get_digest_then_catalog_mutation_denies_without_job_side_effect(monkeypatch):
    original = _tool()
    mutated = _tool("mutated after GET")
    monkeypatch.setattr(tools_router.deps, "all_tools", lambda _tenant: [original])
    catalog = tools_router.tools(tenant="tenant-a")
    issued = catalog["tools"][0]["catalog_digest"]
    assert issued.startswith("sha256:") and len(issued) == 71

    calls = []
    monkeypatch.setattr(jobs_router.deps, "find_tool", lambda _name, _tenant: mutated)
    monkeypatch.setattr(jobs_router.jobs, "submit_job", lambda *_args, **_kwargs: calls.append(True))
    response = jobs_router.run(
        jobs_router.RunRequest(
            tool=original["name"], params={}, catalog_digest=issued),
        tenant_id="tenant-a",
        x_org_id=None, x_project_id=None, idempotency_key=None, authorization=None,
    )

    assert isinstance(response, JSONResponse)
    assert response.status_code == 409
    body = json.loads(response.body)
    assert body["error"]["error_code"] == "BAD_PARAMS"
    assert calls == []


def test_run_requires_server_issued_catalog_digest_before_submission(monkeypatch):
    calls = []
    monkeypatch.setattr(jobs_router.deps, "find_tool", lambda _name, _tenant: _tool())
    monkeypatch.setattr(jobs_router.jobs, "submit_job", lambda *_args, **_kwargs: calls.append(True))

    response = jobs_router.run(
        jobs_router.RunRequest(tool="catalog-bound-tool", params={}),
        tenant_id="tenant-a",
        x_org_id=None, x_project_id=None, idempotency_key=None, authorization=None,
    )

    assert isinstance(response, JSONResponse)
    assert response.status_code == 409
    assert calls == []
