"""Saved supplier quantity comparisons through the estimate HTTP route."""
from copy import deepcopy
from dataclasses import replace
from hashlib import sha256
import json
from uuid import UUID

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

import deps
import solar_project_context as project
from routers import project_estimates as routes
from test_sip_r6_read import estimate_lane, lane  # noqa: F401


@pytest.mark.parametrize(
    "before_increment, after_increment, floor, expected_delta",
    [(2, 3, None, 25), (2, 3, "1000000.00", 0), (None, 3, None, 50)],
    ids=["different-prices", "equal-prices", "legacy-to-supplier"],
)
def test_compare_saved_supplier_quantities(
    estimate_lane, monkeypatch, before_increment, after_increment, floor, expected_delta,
):
    store = estimate_lane
    line_id = "strings.modules"
    quantity = int(next(row["quantity"] for row in store.saved["result"]["estimate"]["lines"]
                        if row["line_id"] == line_id))
    assert quantity > 0
    versions = {}
    for index, increment in enumerate((before_increment, after_increment)):
        version_id = UUID(int=1000101 + index)
        store.successor_id = version_id
        store.request_id = UUID(int=1000201 + index)
        store.receipt = {"drawing_version": {"version_id": str(version_id)}}
        card = deepcopy(store.card)
        rate = card["rates"][0]
        rate["rate"] = "0.25"
        if increment is not None:
            rate.update(increment=str(quantity * increment),
                        increment_semantics="ceil_quantity_before_pricing")
        if floor is not None:
            rate.update(minimum_amount=floor, minimum_semantics="per_line_floor_after_rounding")
        store.save_estimate(card)
        raw = store.publications[-1][-1]["_restored_intake"]
        binding = replace(store.successor.binding, input_version_id=version_id,
                          version=replace(store.successor.binding.version, version_id=version_id))
        versions[version_id] = replace(store.successor, intake=json.loads(raw), intake_bytes=raw,
                                       intake_sha256=sha256(raw).hexdigest(), binding=binding)

    def resolve(tenant, project_id, version_id, **kwargs):
        if project_id == store.project and kwargs.get("drawing_id") == store.drawing and version_id in versions:
            return versions[version_id]
        return store.resolve_original(tenant, project_id, version_id, **kwargs)

    monkeypatch.setattr(project, "resolve_context", resolve)
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[deps.require_active_tenant] = lambda: store.tenant
    base = f"/api/projects/{store.project}/drawings/{store.drawing}/versions"
    before_id, after_id = versions
    measured = str(quantity)
    unit = store.card["rates"][0]["unit"]
    before_billed = None if before_increment is None else str(quantity * before_increment)
    after_billed = str(quantity * after_increment)
    delta = quantity * expected_delta
    with TestClient(app) as client:
        forward = client.get(f"{base}/{before_id}/estimates/compare",
                             params={"compare_version_id": str(after_id)})
        reverse = client.get(f"{base}/{after_id}/estimates/compare",
                             params={"compare_version_id": str(before_id)})

    cases = ((forward, before_billed, after_billed, delta),
             (reverse, after_billed, before_billed, -delta))
    for response, old_billed, new_billed, cents in cases:
        assert response.status_code == 200 and response.headers["cache-control"] == "no-store", response.text
        result = response.json()["result"]
        assert result["subtotal_delta_cents"] == str(cents)
        assert result["billing_quantities"] == [{
            "line_id": line_id, "unit": unit, "measured_quantity": measured,
            "before_billed_quantity": old_billed, "after_billed_quantity": new_billed,
        }]
        if cents == 0:
            assert result["lines"] == []
