"""Civil HTTP authorization, bounded transport and direct-engine parity."""
import json
import sys
from pathlib import Path

import pytest

SERVER = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVER))
sys.path.insert(0, str(SERVER / "tests"))

import solar_civil_operations as civil
import solar_frames_piles as fp
import solar_physical_head as ph
import solar_physical_state as ps
from routers import drawings, solar_terrain as route
from test_solar_terrain_route import (
    backend, client, twin, graph, post, get, asgi, assert_refused, forbidden, URL)
from test_solar_civil_operations import body, call, snapshot
from test_solar_frames_piles import (
    landxml_head, head_id, state_of, SQUARE_M, TINY, TERRAIN_SHA,
    MEASURED_LX_M_GENERATE, MEASURED_LX_M_PILES)
from test_solar_landxml_route import body_of
from test_solar_physical_state import DRAWING, PROJECT, TENANT


def ready(backend):
    base = landxml_head(backend)
    call(backend, body(base=base, boundary=SQUARE_M))
    call(backend, body(fp.PILING, head_id(backend)))


def checkout_matrix(backend, client, monkeypatch, lease, proof, status):
    import checkout_capability
    import store
    ready(backend)
    if lease != "none":
        client.app.include_router(drawings.router)
        acquired = client.post("/api/drawings/" + DRAWING + "/checkout",
                               json={"holder": "civil holder"}, headers={"X-Tenant-Id": TENANT})
        assert acquired.status_code == 200
        if proof == "valid":
            proof = acquired.json()["checkout_capability"]
        if lease == "expired":
            manifest = store.load_manifest(backend, TENANT, DRAWING)
            manifest["checkout"]["expires"] = "2000-01-01T00:00:00+00:00"
            store.save_manifest(backend, TENANT, DRAWING, manifest)
    if proof == "unavailable":
        def unavailable(*args, **kwargs):
            raise checkout_capability.CapabilityUnavailable("unavailable")
        monkeypatch.setattr(checkout_capability, "verify", unavailable)
    manifest = store.load_manifest(backend, TENANT, DRAWING)
    headers = {"X-Tenant-Id": TENANT}
    if proof is not None:
        headers["X-Checkout-Capability"] = proof
    for operation in civil.OPERATIONS:
        before = snapshot(backend)
        request = body(operation, head_id(backend), **({"boundary": SQUARE_M} if operation == fp.GENERATE else {}))
        response = client.post(URL + "/operations", json=request, headers=headers)
        assert response.status_code == status
        assert store.load_manifest(backend, TENANT, DRAWING) == manifest
        if status == 200:
            assert response.json()["error"] is None
        else:
            assert_refused(response, status,
                           "TERRAIN_CHECKOUT_UNAVAILABLE" if status == 503 else "TERRAIN_CHECKOUT_DENIED",
                           status == 503)
            assert snapshot(backend) == before


def test_civ_11_active_lease_absent_proof(backend, client, monkeypatch):
    checkout_matrix(backend, client, monkeypatch, "active", None, 403)


def test_civ_12_active_lease_valid_proof(backend, client, monkeypatch):
    import deps
    tenant = deps.TenantContext(TENANT, tier="demo", subject="auth0|civil-holder")
    client.app.dependency_overrides[deps.require_active_tenant] = lambda: tenant
    original = route._lock_authorization
    seen = []
    def record(drawing, context, backend_, capability):
        seen.append(context)
        return original(drawing, context, backend_, capability)
    monkeypatch.setattr(route, "_lock_authorization", record)
    checkout_matrix(backend, client, monkeypatch, "active", "valid", 200)
    assert len(seen) == 5 and all(context is tenant for context in seen)


def test_civ_13_no_lease(backend, client):
    ready(backend)
    for proof in (None, "", "   ", "garbage"):
        for operation in civil.OPERATIONS:
            headers = {"X-Tenant-Id": TENANT}
            if proof is not None:
                headers["X-Checkout-Capability"] = proof
            request = body(operation, head_id(backend), **({"boundary": SQUARE_M} if operation == fp.GENERATE else {}))
            response = client.post(URL + "/operations", json=request, headers=headers)
            assert response.status_code == 200 and response.json()["error"] is None


def test_civ_14_expired_lease(backend, client, monkeypatch):
    checkout_matrix(backend, client, monkeypatch, "expired", None, 200)


def test_civ_15_invalid_active_proof(backend, client, monkeypatch):
    # Each proof is sent for all five operations against the same active checkout.
    import store
    checkout_matrix(backend, client, monkeypatch, "active", "", 403)
    manifest = store.load_manifest(backend, TENANT, DRAWING)
    before = snapshot(backend)
    for proof in ("   ", "garbage"):
        for operation in civil.OPERATIONS:
            response = client.post(URL + "/operations", json=body(operation, head_id(backend)),
                                   headers={"X-Tenant-Id": TENANT, "X-Checkout-Capability": proof})
            assert_refused(response, 403, "TERRAIN_CHECKOUT_DENIED")
            assert snapshot(backend) == before
            assert store.load_manifest(backend, TENANT, DRAWING) == manifest


def test_civ_16_verification_unavailable(backend, client, monkeypatch):
    checkout_matrix(backend, client, monkeypatch, "active", "unavailable", 503)


def test_civ_17_exact_frame_retry(backend, client):
    request = body()
    first = body_of(post(client, request))
    before = snapshot(backend)
    again = body_of(post(client, request))
    assert again["outcome"] == "retry" and again["summary"] is None and again["created"] is False
    assert again["head"] == first["head"] and again["preview"] == first["preview"]
    assert snapshot(backend) == before


def test_civ_18_stale_and_racing_writer(backend, client, monkeypatch):
    post(client, body())
    before = snapshot(backend)
    assert_refused(post(client, body(boundary=SQUARE_M)), 409, "FRAMES_PILES_STALE_BASE", True)
    assert snapshot(backend) == before
    base = head_id(backend)
    real_publish = ph.publish_physical_state
    winner = []
    def racing(backend_, tenant, drawing, document, **kw):
        rival = ps.physical_document(TINY, drawing_units="m", source_sha256=TERRAIN_SHA,
                                     capability="frame-generate", parent=base)
        winner.append(real_publish(backend_, tenant, drawing, rival)["head"])
        return real_publish(backend_, tenant, drawing, document, **kw)
    monkeypatch.setattr(ph, "publish_physical_state", racing)
    assert_refused(post(client, body(base=base)), 409, "PHYSICAL_HEAD_CONFLICT", True)
    assert ph.physical_head(backend, TENANT, DRAWING, project_id=PROJECT) == winner[0]
    assert state_of(backend)["frames"] == TINY["frames"]


def test_civ_21_legacy_body_edges(client, monkeypatch):
    seen = []
    def record(*args, **kwargs):
        seen.append(kwargs)
        return {"legacy": True}
    for name in ("render_mesh", "check_tracker_slope", "clear_tracker_slope"):
        monkeypatch.setattr(route.adapter, name, record)
    compact = json.dumps({"operation": "mesh", "expected_head": "a" * 64}).encode()
    for operation in ("mesh", "slope", "slope-clear"):
        compact = json.dumps({"operation": operation, "expected_head": "a" * 64}).encode()
        at_limit = compact + b" " * (8192 - len(compact))
        assert post(client, content=at_limit).status_code == 200
        count = len(seen)
        with monkeypatch.context() as patch:
            patch.setattr(route, "_backend", forbidden)
            assert_refused(post(client, content=at_limit + b" "), 413, "TERRAIN_BODY_TOO_LARGE")
        assert len(seen) == count
    assert_refused(post(client, {"operation": "piles", "expected_head": "a" * 64}),
                   400, "TERRAIN_OPERATION_INVALID")
    for request in (dict(body(), base=None), dict(body(), limits={}), {"operation": fp.GENERATE}):
        assert_refused(post(client, request), 400, "TERRAIN_BODY_INVALID")
    for content in ('{"operation":"frame-collision-detect","expected_head":null,"expected_head":null}',
                    '{"operation":"frame-generate","expected_head":null,"boundary":[],"drawing_units":"m",'
                    '"preset":{"Name":"x","Nested":{"x":1,"x":2}}}'):
        assert_refused(post(client, content=content), 400, "TERRAIN_BODY_INVALID")


def test_civ_22_civil_declared_bounds(client, monkeypatch):
    seen = []
    def record(*args):
        seen.append(args[3])
        return {"transport": True}
    monkeypatch.setattr(route, "_operate", record)
    monkeypatch.setattr(route, "_backend", forbidden)
    cap = civil.MAX_CIVIL_BODY_BYTES
    compact = json.dumps(body()).encode()
    padded = compact + b" " * (cap - len(compact))
    headers = [(b"content-type", b"application/json"), (b"content-length", str(cap).encode())]
    status, result, pulls = asgi(client, [("at-cap", padded)], headers)
    assert status == 200 and pulls == ["at-cap"] and len(seen) == 1
    for length in (str(cap + 1).encode(), ("000000000000" + str(cap + 1)).encode(), b"9" * 100):
        status, result, pulls = asgi(client, [("unread", padded)],
                                    [(b"content-type", b"application/json"), (b"content-length", length)])
        assert (status, result["error"]["reason_code"], pulls) == (413, "TERRAIN_BODY_TOO_LARGE", [])
    # A valid civil declaration cannot exempt a small legacy body's declared size.
    legacy = json.dumps({"operation": "mesh", "expected_head": "a" * 64}).encode()
    status, result, pulls = asgi(client, [("legacy", legacy)], headers)
    assert status == 413 and result["error"]["reason_code"] == "TERRAIN_BODY_TOO_LARGE"
    assert len(seen) == 1


def test_civ_23_civil_streamed_bounds(client, monkeypatch):
    from types import SimpleNamespace
    seen = []
    parsed_lengths = []
    real_loads = route.json.loads
    def parse(value, *args, **kwargs):
        parsed_lengths.append(len(value))
        return real_loads(value, *args, **kwargs)
    # Scope the spy to the route, leaving the ASGI helper's response decoder alone.
    monkeypatch.setattr(route, "json", SimpleNamespace(loads=parse))
    def record(*args):
        seen.append(args[3])
        return {"transport": True}
    monkeypatch.setattr(route, "_operate", record)
    monkeypatch.setattr(route, "_backend", forbidden)
    compact = json.dumps(body()).encode()
    cap = civil.MAX_CIVIL_BODY_BYTES
    padded = compact + b" " * (cap - len(compact))
    for length in (None, b"0", b"invalid", str(cap).encode()):
        headers = [(b"content-type", b"application/json")]
        if length is not None:
            headers.append((b"content-length", length))
        status, result, pulls = asgi(client, [("first", padded[:100]), ("rest", padded[100:])], headers)
        assert status == 200 and pulls == ["first", "rest"]
        assert parsed_lengths == [cap]
        parsed_lengths.clear()
        status, result, pulls = asgi(client, [("first", padded), ("overflow", b" "), ("unread", b" ")], headers)
        assert (status, result["error"]["reason_code"], pulls) == (
            413, "TERRAIN_BODY_TOO_LARGE", ["first", "overflow"])
        assert parsed_lengths == []
    status, result, pulls = asgi(client, [("unread", padded + b" ")],
                                [(b"content-type", b"application/json"),
                                 (b"content-length", str(cap + 1).encode())])
    assert (status, result["error"]["reason_code"], pulls) == (413, "TERRAIN_BODY_TOO_LARGE", [])
    assert parsed_lengths == []
    assert len(seen) == 4


def test_civ_25_scoped_reopen(backend, client):
    before = snapshot(backend)
    assert body_of(get(client, query="?view=civil")) == {
        "schema": civil.VIEW_SCHEMA, "stored": False, "head": None, "preview": None,
        "standing": None, "grade_pads": 0}
    assert snapshot(backend) == before
    landxml_head(backend)
    for operation in (fp.GENERATE, fp.PILING, civil.GRADE):
        response = post(client, body(operation, head_id(backend),
                                    **({"boundary": SQUARE_M} if operation == fp.GENERATE else {})))
        assert response.status_code == 200
        result = body_of(response)
        before = snapshot(backend)
        view = body_of(get(client, query="?view=civil&project_id=" + PROJECT))
        assert set(view) == {"schema", "stored", "head", "preview", "standing", "grade_pads"}
        assert view["schema"] == civil.VIEW_SCHEMA and view["stored"] is True
        assert view["head"] == result["head"] and view["preview"] == result["preview"]
        assert view["standing"] == result["standing"]
        assert view["grade_pads"] == (1 if operation == civil.GRADE else 0)
        assert snapshot(backend) == before
    assert_refused(get(client, query="?view=civil", tenant="foreign"), 404, "FRAMES_PILES_DRAWING_NOT_FOUND")


def test_civ_27_http_matches_direct_engine(backend, twin, client):
    assert landxml_head(backend) == landxml_head(twin)
    transactions = {fp.GENERATE: fp.generate_frames, fp.PILING: fp.generate_piles,
                    fp.COLLISION: fp.detect_collisions, fp.RANGE: fp.check_pile_lengths}
    for operation in (fp.GENERATE, fp.PILING, fp.COLLISION, fp.RANGE, civil.GRADE):
        request = body(operation, head_id(backend), **({"boundary": SQUARE_M} if operation == fp.GENERATE else {}))
        if operation == civil.GRADE:
            _, document = ph.load_physical_head(twin, TENANT, DRAWING, project_id=PROJECT)
            expected_pad = civil.bo.grade_multi(
                document["state"]["grid"], [request["boundary"]], document["units"]["meters_per_unit"],
                mode=request.get("mode", "Auto"), value_du=request.get("value_du"),
                runtime=civil.bo.RUNTIME_NET8)["pads"][0]
        else:
            kwargs = {key: value for key, value in request.items()
                      if key not in ("operation", "expected_head")}
            expected = transactions[operation](twin, TENANT, DRAWING, base=request["expected_head"],
                                                project_id=PROJECT, **kwargs)
        response = post(client, request, query="?project_id=" + PROJECT)
        assert response.status_code == 200
        if operation == civil.GRADE:
            assert state_of(backend)["grade_pads"][-1] == expected_pad
            continue
        assert body_of(response) == expected
        if operation == fp.GENERATE:
            assert expected["summary"] == MEASURED_LX_M_GENERATE
        elif operation == fp.PILING:
            assert expected["summary"] == MEASURED_LX_M_PILES
        assert head_id(backend) == head_id(twin)
