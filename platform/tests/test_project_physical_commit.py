"""Real PostgreSQL lease, row-lock and canonical physical publication proofs."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import importlib.util
import os
from pathlib import Path
from threading import Barrier
from types import SimpleNamespace
from uuid import UUID

import pytest

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location(
    "_project_physical_pg_helpers", ROOT / "platform/tests/test_sip_r6_outputs.py")
helpers = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(helpers)
pg = helpers.pg

import solar_project_physical as physical
import solar_project_artifacts as artifacts
from routers import project_physical, project_drawings

pytestmark = pytest.mark.skipif(
    not (os.environ.get("DATABASE_URL") or (ROOT / "platform/.env.local").exists()),
    reason="PostgreSQL integration test requires DATABASE_URL")


@pytest.fixture
def world(make_org, monkeypatch):
    generator = helpers.world.__wrapped__(make_org, monkeypatch)
    fresh = next(generator)
    try:
        yield fresh
    finally:
        with pytest.raises(StopIteration):
            next(generator)


@pytest.fixture
def http(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    import envelopes
    from routers import jobs, capabilities
    principal = {"tenant": None}
    app = FastAPI()
    envelopes.install_error_handlers(app)
    app.middleware("http")(project_drawings.no_store_responses)
    app.include_router(project_drawings.router)
    app.include_router(project_physical.router)
    app.include_router(jobs.router)
    app.include_router(capabilities.router)
    app.dependency_overrides[pg.deps.require_tenant] = lambda: principal["tenant"]
    app.dependency_overrides[pg.deps.require_active_tenant] = lambda: principal["tenant"]
    monkeypatch.setattr(jobs.jobs, "submit_job", pg.blocked)
    monkeypatch.setattr(jobs.jobs, "wait_for_terminal", pg.blocked)
    with TestClient(app) as client:
        yield SimpleNamespace(client=client, principal=principal, route=jobs)


def _path(s, version):
    return f"/api/projects/{s.project}/drawings/{s.drawing}/versions/{version}/physical"


def _xml(elevation=10):
    points = "".join(f'<P id="{i}">{y} {x} {elevation+i}</P>'
                     for i, (x, y) in enumerate(((0, 0), (10, 0), (0, 10), (10, 10)), 1))
    return ('<LandXML xmlns="http://www.landxml.org/schema/LandXML-1.2" version="1.2">'
            '<Units><Metric linearUnit="meter" areaUnit="squareMeter" volumeUnit="cubicMeter"/></Units>'
            '<Surfaces><Surface name="S"><Definition surfType="TIN"><Pnts>' + points +
            '</Pnts></Definition></Surface></Surfaces></LandXML>').encode()


def _import(http, s, version, elevation=10):
    return http.client.post(_path(s, version) + "/imports/landxml?drawing_units=m&crs=none&target_cells=2",
        content=_xml(elevation), headers={**helpers._headers(s, "physical"), "Content-Type": "application/xml"})


def _count(s):
    with pg.db.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM drawing_versions WHERE org_id=%s AND project_id=%s",
                    (s.org, s.project))
        return cur.fetchone()["n"]


def _setup(http, world):
    s = world()
    version = helpers._seed(http, s)
    helpers._bind(http, s)
    return s, version


def test_lease_released_between_admission_and_commit(world, http, monkeypatch):
    s, version = _setup(http, world)
    before = _count(s), deepcopy(s.blobs.data), list(s.blobs.writes)
    original = physical.landxml.import_landxml_terrain
    def release(*args, **kwargs):
        result = original(*args, **kwargs)
        pg.project.release_project_checkout(helpers._principal(s), s.project, s.drawing,
            checkout_capability_token=helpers._headers(s, "release")["X-Checkout-Capability"])
        return result
    monkeypatch.setattr(physical.landxml, "import_landxml_terrain", release)
    helpers._refusal(_import(http, s, version), 409, "SIP_R1_CHECKOUT_REQUIRED")
    assert (_count(s), s.blobs.data, s.blobs.writes) == before


def test_lease_taken_over_between_admission_and_commit(world, http, monkeypatch):
    s, version = _setup(http, world)
    before = _count(s), deepcopy(s.blobs.data), list(s.blobs.writes)
    original = physical.landxml.import_landxml_terrain
    def takeover(*args, **kwargs):
        result = original(*args, **kwargs)
        with pg.db.cursor() as cur:
            # Model an expired renewal. The checkout guard requires every
            # change of lease timestamps to advance the retained fence, and
            # its state constraint requires expiry to follow acquisition.
            cur.execute("UPDATE project_drawing_checkouts SET "
                        "acquired_at=clock_timestamp()-interval '2 seconds', "
                        "expires_at=clock_timestamp()-interval '1 second', fence=fence+1 "
                        "WHERE org_id=%s AND project_id=%s AND drawing_id=%s", (s.org, s.project, s.drawing))
        lease = pg.db.run_transaction(lambda conn: pg.store_graph.acquire_checkout(
            s.org, s.project, s.drawing, actor_binding_id=s.other, holder="Second editor",
            ttl_s=60, expected_fence=None, conn=conn))
        assert lease.fence > s.lease.fence
        return result
    monkeypatch.setattr(physical.landxml, "import_landxml_terrain", takeover)
    response = _import(http, s, version)
    assert (response.status_code, response.json()["error"]["reason_code"]) in (
        (409, "SIP_R1_CHECKOUT_STALE"), (403, "SIP_R1_CHECKOUT_DENIED"))
    assert (_count(s), s.blobs.data, s.blobs.writes) == before


def test_concurrent_writers_same_head_one_version(world, http, monkeypatch):
    s, version = _setup(http, world)
    before_count, before_keys = _count(s), set(s.blobs.data)
    original = physical.landxml.import_landxml_terrain
    barrier = Barrier(2)
    candidates = {}
    def concurrent(*args, **kwargs):
        result = original(*args, **kwargs)
        candidates[args[3]] = (result["head"]["state"], result["source"])
        barrier.wait(timeout=20)
        return result
    monkeypatch.setattr(physical.landxml, "import_landxml_terrain", concurrent)
    # Distinct output bytes on one head distinguish a losing attempt from an
    # idempotent replay of the winner's identical content-addressed request.
    with ThreadPoolExecutor(max_workers=2) as pool:
        pending = [pool.submit(_import, http, s, version, elevation) for elevation in (10, 20)]
        responses = [future.result(timeout=30) for future in pending]
    assert sorted(response.status_code for response in responses) == [200, 409]
    winner = next(response for response in responses if response.status_code == 200)
    loser = next(response for response in responses if response.status_code == 409)
    helpers._refusal(loser, 409, "SIP_R1_STALE_VERSION")
    assert winner.json()["context"]["output_version_id"] and _count(s) == before_count + 1
    winner_id = winner.json()["result"]["head"]["state"]["artifact_id"]
    for state, source in candidates.values():
        for ref in (state, source):
            keys = {artifacts._key(ref, ref["artifact_id"]),
                    artifacts._key(ref, ref["content_sha256"], blob=True)}
            if state["artifact_id"] == winner_id:
                assert keys <= set(s.blobs.data) - before_keys
            else:
                assert not keys & set(s.blobs.data)


def test_commit_carries_admission_fence_and_ref(world, http, monkeypatch):
    s, version = _setup(http, world)
    captured = []
    original = physical.landxml.import_landxml_terrain
    def capture(*args, **kwargs):
        captured.append(kwargs["source"])
        return original(*args, **kwargs)
    monkeypatch.setattr(physical.landxml, "import_landxml_terrain", capture)
    response = _import(http, s, version)
    assert response.status_code == 200, response.text
    output = UUID(response.json()["context"]["output_version_id"])
    binding = pg.project.graph_store().resolve_version_binding(s.org, s.project, output, drawing_id=s.drawing)
    assert binding.version.provenance["checkout_fence"] == str(s.lease.fence)
    view = http.client.get(_path(s, output))
    assert view.status_code == 200, view.text
    assert view.json()["head"] == response.json()["result"]["head"]
    assert physical.check_ref(pg.project._read_intake(binding).intake[physical.REF_KEY])["index"] == 0
    before = _count(s), deepcopy(s.blobs.data), list(s.blobs.writes)
    assert physical.commit(captured[0].admission, captured[0]) == output
    assert (_count(s), s.blobs.data, s.blobs.writes) == before
