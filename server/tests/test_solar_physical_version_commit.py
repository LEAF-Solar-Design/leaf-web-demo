"""V2 physical publications use the canonical version rail on both authorities.

Digests are derived from documents, including terrain kernel floats; no libm-dependent
digest is pinned here. PostgreSQL runs use isolated drawing identities.
"""
import copy
import json
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

SERVER = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVER))
sys.path.insert(0, str(SERVER / "tests"))

import write_loop
import store
import solar_physical_head as ph
import solar_physical_state as ps
import solar_physical_legacy as legacy
import solar_physical_source as sources
import solar_landxml_import as lx
import solar_ground_terrain_adapter as terrain
import solar_frames_piles as frames
import solar_civil_operations as civil
import solar_tracker_rows as trackers
import solar_local_graph as local
import solar_local_read as reads
from leaf_cloud_client import canonical_bytes
from solar_sizing_client import digest
from test_w1_design_graph import graph  # noqa: F401
from test_solar_ground_graph_codec import ground_base
import test_solar_frames_piles as frame_cases
import test_solar_landxml_import as xml_cases
import test_solar_tracker_rows as row_cases


@pytest.fixture
def postgres_authority(monkeypatch):
    if not os.environ.get("DATABASE_URL"):
        pytest.skip("PostgreSQL physical commit test requires explicit DATABASE_URL")
    db = store._db()
    db.apply_migration()
    monkeypatch.setenv("LEAF_DRAWING_STORE", "postgres")
    monkeypatch.setenv("LEAF_UPLOAD_STORE", "postgres")
    yield db
    db.reset_pool()
    store._platform_db = None


@pytest.fixture(params=["legacy", "postgres"])
def case(request, graph, tmp_path, monkeypatch):
    monkeypatch.setenv("LEAF_DRAWING_STORE", "legacy")
    if request.param == "postgres":
        request.getfixturevalue("postgres_authority")
    monkeypatch.setenv("LEAF_DRAWING_MUTATIONS_ENABLED", "1")
    monkeypatch.delenv("LEAF_DRAWING_MUTATIONS_FENCE_FILE", raising=False)
    monkeypatch.setenv(legacy.SWITCH_ENV, "1")
    store._physical_memo_clear()
    g = ground_base(graph)
    backend = store.FilesystemBackend(str(tmp_path / "drawings"))
    tenant, drawing = "physical-" + uuid4().hex[:16], "solar"
    payload = {"dwg": {}, "layers": [], "polylines": [], "inserts": [], "faces3d": [],
               "blockdefs": [], "geodata": None, "solar_design_graph": g,
               "solar_design_graph_sha256": digest(g)}
    path = tmp_path / "intake.json"
    path.write_bytes(canonical_bytes(payload))
    store.ingest_drawing(backend, tenant, str(path), drawing_id=drawing)
    fence = store.acquire_checkout_fence(backend, tenant, drawing, "physical-owner", 300)
    monkeypatch.setattr(write_loop, "backend_for_tenant", lambda *a, **k: backend)
    yield SimpleNamespace(backend=backend, tenant=tenant, drawing=drawing,
                          project=g["project"]["id"], holder="physical-owner", fence=fence, graph=g)
    store._physical_memo_clear()


def credentials(c):
    return dict(project_id=c.project, holder=c.holder, fence=c.fence)


def manifest(c):
    return store.load_manifest(c.backend, c.tenant, c.drawing)


def snapshot(c):
    return (manifest(c), {k: c.backend.get(k) for k in c.backend.drawing_object_keys(c.tenant, c.drawing)})


def head(c):
    return ph.load_physical_head(c.backend, c.tenant, c.drawing, project_id=c.project)


def document(parent=None, value=1, state=None):
    return ps.physical_document({"next_handle": value} if state is None else state,
                                drawing_units="m", source_sha256="a" * 64,
                                capability="fixture", parent=parent)


def publish(c, doc=None, **kwargs):
    return ph.publish_physical_state(c.backend, c.tenant, c.drawing,
                                     document() if doc is None else doc,
                                     **(credentials(c) | kwargs))


def ref(view):
    return local.physical_state_source(view)


def refused(code, fn, *args, **kwargs):
    with pytest.raises(ps.PhysicalStateError) as exc:
        fn(*args, **kwargs)
    assert exc.value.code == code


def imported(c):
    return lx.import_landxml_terrain(c.backend, c.tenant, c.drawing, xml_cases.REAL,
                                    drawing_units="m", crs="none", **credentials(c))


def generate(c, base):
    return frames.generate_frames(c.backend, c.tenant, c.drawing, base=base,
        boundary=frame_cases.SQUARE_M, preset=frame_cases.PRESET, drawing_units="m", **credentials(c))


def rows(c, base=None):
    return trackers.publish_manual_create(c.backend, c.tenant, c.drawing,
        rows=copy.deepcopy(row_cases.RAW), module_power_watts=450, expected_head=base, **credentials(c))


PRODUCERS = ["landxml", "mesh", "slope", "slope-clear", "frames", "collision",
             "piles", "range", "grade", "rows"]


@pytest.mark.parametrize("operation", PRODUCERS)
def test_each_producer_commits_one_version(case, monkeypatch, operation):
    c = case
    calls = []
    real_publish = ph.publish_physical_state
    def recording(*args, **kwargs):
        calls.append(kwargs)
        return real_publish(*args, **kwargs)
    monkeypatch.setattr(ph, "publish_physical_state", recording)
    if operation in ("landxml", "rows"):
        action = lambda: imported(c) if operation == "landxml" else rows(c)
    else:
        base = imported(c)["head"]["state"]["artifact_id"]
        if operation in ("slope", "slope-clear", "collision", "piles", "range"):
            base = generate(c, base)["head"]["state"]["artifact_id"]
        if operation == "collision":
            base = generate(c, base)["head"]["state"]["artifact_id"]
        if operation == "slope-clear":
            base = terrain.check_tracker_slope(c.backend, c.tenant, c.drawing,
                expected_head=base, **credentials(c))["head"]["state"]["artifact_id"]
        if operation == "range":
            base = frames.generate_piles(c.backend, c.tenant, c.drawing, base=base,
                preset=frame_cases.PRESET, pile_template=frame_cases.TEMPLATE,
                **credentials(c))["head"]["state"]["artifact_id"]
        actions = {
            "mesh": lambda: terrain.render_mesh(c.backend, c.tenant, c.drawing,
                expected_head=base, **credentials(c)),
            "slope": lambda: terrain.check_tracker_slope(c.backend, c.tenant, c.drawing,
                expected_head=base, **credentials(c)),
            "slope-clear": lambda: terrain.clear_tracker_slope(c.backend, c.tenant, c.drawing,
                expected_head=base, **credentials(c)),
            "frames": lambda: generate(c, base),
            "collision": lambda: frames.detect_collisions(c.backend, c.tenant, c.drawing,
                base=base, **credentials(c)),
            "piles": lambda: frames.generate_piles(c.backend, c.tenant, c.drawing, base=base,
                preset=frame_cases.PRESET, pile_template=frame_cases.TEMPLATE, **credentials(c)),
            "range": lambda: frames.check_pile_lengths(c.backend, c.tenant, c.drawing, base=base,
                preset=frame_cases.narrow(MinPileLengthM=100, MaxPileLengthM=101), **credentials(c)),
            "grade": lambda: civil.grade_pad(c.backend, c.tenant, c.drawing, base=base,
                boundary=frame_cases.SQUARE_M, **credentials(c)),
        }
        action = actions[operation]
    before = manifest(c)
    result = action()
    after = manifest(c)
    assert result["created"] is True
    assert len(after["versions"]) == len(before["versions"]) + 1
    assert result["drawing_version"] == {"version": after["head"], "parent_version": before["head"], "created": True}
    assert store.read_version_physical(c.backend, c.tenant, c.drawing, project_id=c.project) == (after["head"], ref(result["head"]))
    assert head(c)[0] == result["head"]
    assert calls and all(call["holder"] == c.holder and call["fence"] == c.fence for call in calls)


def test_noop_and_retry_write_neither_version_nor_entry(case):
    first = imported(case)
    before = manifest(case)
    tip = ph._Log(case.backend, case.tenant, case.drawing).last()
    again = imported(case)
    assert again["created"] is False and "drawing_version" not in again
    assert manifest(case) == before
    assert ph._Log(case.backend, case.tenant, case.drawing).last() == tip
    doc = head(case)[1]
    result = publish(case, doc)
    assert result["drawing_version"] == {"version": manifest(case)["head"],
        "parent_version": manifest(case)["head"], "created": False}
    assert result["head"] == first["head"] and manifest(case) == before
    assert ph._Log(case.backend, case.tenant, case.drawing).last() == tip


def test_undo_redo_restore_follow_version_reference(case):
    from routers.drawings import restore_drawing_version
    first = publish(case)
    base = first["head"]["state"]["artifact_id"]
    second = publish(case, document(base, 2))
    store.undo(case.backend, case.tenant, case.drawing, holder=case.holder, fence=case.fence)
    assert head(case)[0] == first["head"]
    store.redo(case.backend, case.tenant, case.drawing, holder=case.holder, fence=case.fence)
    assert head(case)[0] == second["head"]
    restored = restore_drawing_version(case.tenant, case.drawing, first["drawing_version"]["version"],
        actor="restore", backend=case.backend, holder=case.holder, fence=case.fence)
    assert restored.version > second["drawing_version"]["version"]
    assert head(case)[0] == first["head"]


def test_edit_after_undo_and_recorded_read_proof(case):
    first = publish(case)
    base = first["head"]["state"]["artifact_id"]
    second = publish(case, document(base, 2))
    store.undo(case.backend, case.tenant, case.drawing, holder=case.holder, fence=case.fence)
    # The proof follows the selected version, even though the log tip is newer.
    old_version = manifest(case)["head"]
    assert reads._recorded_physical_head(case.backend, case.tenant, case.drawing, case.project,
        {"head": first["head"]}, version=old_version)["head"] == first["head"]
    third = publish(case, document(base, 3))
    view, doc = head(case)
    assert view == third["head"] and view["parent"] == doc["parent"] == base
    entry = ph._Log(case.backend, case.tenant, case.drawing).entry(view["index"])
    assert entry["parent"] == second["head"]["state"]["artifact_id"] != base
    assert local._physical_state_entry(case.backend, case.tenant, case.drawing, case.project, ref(view))["document"] == doc
    assert reads._recorded_physical_head(case.backend, case.tenant, case.drawing, case.project,
        {"head": view}, version=manifest(case)["head"])["head"] == view
    bad = copy.deepcopy(view)
    bad["parent"] = entry["parent"]
    with pytest.raises(ValueError):
        reads._recorded_physical_head(case.backend, case.tenant, case.drawing, case.project, {"head": bad})


def test_none_read_proof_after_undo(case):
    publish(case)
    store.undo(case.backend, case.tenant, case.drawing, holder=case.holder, fence=case.fence)
    assert reads._recorded_physical_head(case.backend, case.tenant, case.drawing, case.project,
        {"head": None}, version=manifest(case)["head"]) is None


@pytest.mark.parametrize("branch", [False, True])
def test_terrain_builtin_terminal_proof_after_undo(case, branch):
    first = imported(case)
    base = first["head"]["state"]["artifact_id"]
    terrain.render_mesh(case.backend, case.tenant, case.drawing, expected_head=base, **credentials(case))
    store.undo(case.backend, case.tenant, case.drawing, holder=case.holder, fence=case.fence)
    if branch:
        generate(case, base)
    version = manifest(case)["head"]
    params = {"drawing_id": case.drawing}
    result = reads.run_local_graph_read(case.backend, case.tenant, "solar-terrain-read", params,
        drawing_id=case.drawing, source_version=version, job_id="terrain-proof", project_id=case.project)
    assert result["output"]["head"] == head(case)[0]
    proof = reads.graph_read_provenance(result, params, case.tenant, "terrain-proof",
        "solar-terrain-read", version, backend=case.backend)
    assert proof is not None


def test_intervening_graph_commit_conflicts_and_next_edit_crosses_orphan(case, monkeypatch):
    real = write_loop.commit_physical_version
    before = len(manifest(case)["versions"])
    def race(*args, **kwargs):
        version = manifest(case)["head"]
        payload = case.backend.get(store.drawing_version_key(case.tenant, case.drawing, version))
        write_loop._put_bytes_version(case.backend, case.tenant, case.drawing, payload, version,
            {"tool": "graph-race"}, holder=case.holder, fence=case.fence)
        return real(*args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(write_loop, "commit_physical_version", race)
        refused("PHYSICAL_HEAD_CONFLICT", publish, case)
    assert len(manifest(case)["versions"]) == before + 1  # Only the racing graph version.
    assert head(case)[0] is None
    result = publish(case, document(value=2))
    assert result["head"]["index"] == 1 and result["head"]["parent"] is None


def test_orphan_reused_after_store_failure(case, monkeypatch):
    def fail(*a, **k):
        raise OSError("private store detail")
    with monkeypatch.context() as patch:
        patch.setattr(write_loop, "commit_physical_version", fail)
        refused("PHYSICAL_HEAD_STORE_UNAVAILABLE", publish, case)
    log = ph._Log(case.backend, case.tenant, case.drawing)
    assert log.last() == 0 and head(case)[0] is None
    result = publish(case)
    assert result["created"] and result["head"]["index"] == 0
    assert ph._Log(case.backend, case.tenant, case.drawing).last() == 0
    assert len(manifest(case)["versions"]) == 2


@pytest.mark.parametrize("identical", [False, True])
def test_concurrent_requests(case, monkeypatch, identical):
    real = ps.store_physical_state
    barrier = threading.Barrier(2)
    def staged(*a, **k):
        result = real(*a, **k)
        barrier.wait(timeout=30)
        return result
    monkeypatch.setattr(ps, "store_physical_state", staged)
    def submit(value):
        try:
            return publish(case, document(value=value))
        except ph.PhysicalHeadError as exc:
            return exc.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(submit, [1, 1 if identical else 2]))
    created = [r for r in results if isinstance(r, dict) and r["created"]]
    assert len(created) == 1 and len(manifest(case)["versions"]) == 2
    if identical:
        retry = next(r for r in results if isinstance(r, dict) and not r["created"])
        version = created[0]["drawing_version"]["version"]
        assert retry["drawing_version"] == {"version": version, "parent_version": version, "created": False}
        assert retry["head"] == created[0]["head"]
        assert ph._Log(case.backend, case.tenant, case.drawing).last() == 0
    else:
        assert "PHYSICAL_HEAD_CONFLICT" in results


def test_conversion_carries_reference(case):
    result = rows(case)
    version = result["drawing_version"]["version"]
    receipt = local.run_local_graph_commit(case.backend, case.tenant, "solar-trackers-to-panel-groups",
        {"expected_rev": 0}, drawing_id=case.drawing, source_version=version,
        holder=case.holder, fence=case.fence, job_id="convert-physical", project_id=case.project)
    new_version = receipt["new_version"]["version"]
    # Local conversion publishes an intake version, not a licensed DWG graph bundle.
    context = local.resolve_graph_context(case.backend, case.tenant, case.drawing, new_version,
                                          project_id=case.project)
    assert context["graph"]["extra"]["physical_state"] == ref(result["head"])
    assert store.read_version_physical(case.backend, case.tenant, case.drawing)[1] == ref(result["head"])


@pytest.mark.parametrize("live", [False, True])
def test_missing_pair_never_reads_or_writes_state(case, live):
    if not live:
        store.release_checkout(case.backend, case.tenant, case.drawing, case.holder)
    before = snapshot(case)
    refused("PHYSICAL_HEAD_CHECKOUT_REQUIRED", publish, case, holder=None, fence=None)
    refused("PHYSICAL_HEAD_CHECKOUT_REQUIRED", publish, case,
            **({"holder": "another-owner"} if live else {}))
    assert snapshot(case) == before


@pytest.mark.parametrize("holder,fence", [
    ("", 1), (store.ANONYMOUS_HOLDER, 1), ("x" * 201, 1), ("owner", 0),
    ("owner", "1"), ("owner", True), (None, 1), ("owner", None), (12, 1),
])
def test_malformed_pair_refused_before_any_get(case, monkeypatch, holder, fence):
    def no_read(*a, **k):
        pytest.fail("malformed checkout reached a backend read")
    monkeypatch.setattr(case.backend, "get", no_read)
    monkeypatch.setattr(store, "load_manifest", no_read)
    refused("PHYSICAL_HEAD_CHECKOUT_REQUIRED", publish, case, holder=holder, fence=fence)


@pytest.mark.parametrize("scope", ["tenant", "project"])
def test_scope_isolation_writes_nothing(case, scope):
    before = snapshot(case)
    if scope == "tenant":
        refused("PHYSICAL_HEAD_ID_INVALID", ph.publish_physical_state,
            case.backend, "foreign-tenant", case.drawing, document(), **credentials(case))
    else:
        refused("PHYSICAL_HEAD_PROJECT_MISMATCH", publish, case, project_id="foreign-project")
    assert snapshot(case) == before


def test_adoption_freezes_before_append_and_preserves_native_state(case, monkeypatch):
    # A pre-V2 drawing retains its frames and piles on its first versioned import.
    with monkeypatch.context() as patch:
        patch.delenv(legacy.SWITCH_ENV)
        first = publish(case, document(state=copy.deepcopy(row_cases.NATIVE)))
    puts = []
    real = case.backend.put_if_absent_or_verify
    def record(key, data):
        puts.append(key)
        return real(key, data)
    monkeypatch.setattr(case.backend, "put_if_absent_or_verify", record)
    result = imported(case)
    freeze_key = legacy.freeze_key(case.tenant, case.drawing)
    assert puts.index(freeze_key) < puts.index(ph.entry_key(case.tenant, case.drawing, 1))
    frozen = json.loads(case.backend.get(freeze_key))
    assert frozen["legacy_last_index"] == 0
    assert result["head"]["parent"] == first["head"]["state"]["artifact_id"]
    for key in ("frames", "piles"):
        assert head(case)[1]["state"][key] == row_cases.NATIVE[key]


def test_physical_version_cap(case, monkeypatch):
    monkeypatch.setattr(ph, "MAX_PHYSICAL_VERSIONS", 2)
    first = publish(case)
    second = publish(case, document(first["head"]["state"]["artifact_id"], 2))
    before = manifest(case)
    refused("PHYSICAL_HEAD_LOG_FULL", publish, case, document(second["head"]["state"]["artifact_id"], 3))
    assert manifest(case) == before


def test_switch_off_has_legacy_shape_and_no_version_or_freeze(case, monkeypatch):
    monkeypatch.delenv(legacy.SWITCH_ENV)
    before = manifest(case)
    result = publish(case, holder=None, fence=None)
    assert set(result) == {"schema", "created", "head"}
    assert result["schema"] == ph.PUBLISH_SCHEMA and result["created"] is True
    assert manifest(case) == before
    assert not any("/physical-legacy/" in k for k in case.backend.drawing_object_keys(case.tenant, case.drawing))


SEAM_CALLS = [
    (lx.import_landxml_terrain, (b"invalid",), {"drawing_units": "m", "crs": "none"}),
    (terrain.render_mesh, (), {}), (terrain.check_tracker_slope, (), {}),
    (terrain.clear_tracker_slope, (), {}),
    (frames.generate_frames, (), {"base": None, "boundary": frame_cases.GEN["boundary"], "preset": frame_cases.PRESET, "drawing_units": "m"}),
    (frames.detect_collisions, (), {"base": None}),
    (frames.generate_piles, (), {"base": None, "preset": frame_cases.PRESET, "pile_template": frame_cases.TEMPLATE}),
    (frames.check_pile_lengths, (), {"base": None, "preset": frame_cases.PRESET}),
    (civil.grade_pad, (), {"base": None, "boundary": frame_cases.SQUARE_M}),
    (civil.operate, ({"operation": frames.COLLISION, "expected_head": None},), {}),
]


@pytest.mark.parametrize("call,args,kwargs", SEAM_CALLS)
@pytest.mark.parametrize("pair", [{"holder": "owner"}, {"fence": 1}])
def test_source_excludes_credentials_before_read(call, args, kwargs, pair):
    class NoReads:
        def get(self, key):
            pytest.fail("source exclusion reached a read")
    with pytest.raises(ValueError, match="^source excludes holder and fence$"):
        call(NoReads(), "tenant", "drawing", *args, source=object(), **pair, **kwargs)


@pytest.mark.parametrize("versioned", [False, True])
def test_legacy_source_forwards_pair_and_normalizes_version(monkeypatch, versioned):
    expected = {"version": 2, "parent_version": 1, "created": True} if versioned else None
    calls = []
    def record(*a, **k):
        calls.append((a, k))
        return {"schema": ph.PUBLISH_SCHEMA, "created": True, "head": None,
                **({"drawing_version": expected} if versioned else {})}
    monkeypatch.setattr(ph, "publish_physical_state", record)
    source = sources.LegacySource("backend", "tenant", "drawing", "project", holder="owner", fence=7)
    source.context = {"project_id": "project"}
    assert source.publish({})["drawing_version"] == expected
    assert calls == [(("backend", "tenant", "drawing", {}), {"project_id": "project", "holder": "owner", "fence": 7})]
    assert source.unchanged_drawing_version() is None


def test_frozen_switch_off_refuses_but_versioned_does_not_call_refuse(case, monkeypatch):
    legacy.freeze_legacy(case.backend, case.tenant, case.drawing, project_id=case.project)
    before = snapshot(case)
    with monkeypatch.context() as patch:
        patch.delenv(legacy.SWITCH_ENV)
        refused("PHYSICAL_LEGACY_FROZEN", publish, case)
    assert snapshot(case) == before
    def fail(*a, **k):
        pytest.fail("versioned publication called refuse_if_frozen")
    monkeypatch.setattr(legacy, "refuse_if_frozen", fail)
    assert publish(case)["created"]


@pytest.mark.parametrize("code,expected", [
    ("WRITES_DRAINED", "WRITES_DRAINED"), ("STORE_UNAVAILABLE", "STORE_UNAVAILABLE"),
    ("STORE_UNSAFE", "STORE_UNSAFE"), ("ID_INVALID", "ID_INVALID"),
    ("PROJECT_MISMATCH", "PROJECT_MISMATCH"), ("CORRUPT", "CORRUPT"), ("FROZEN", "CORRUPT"),
])
def test_freeze_error_mapping(case, monkeypatch, code, expected):
    def fail(*a, **k):
        raise legacy.PhysicalLegacyError("PHYSICAL_LEGACY_" + code)
    monkeypatch.setattr(legacy, "freeze_legacy", fail)
    refused("PHYSICAL_HEAD_" + expected, publish, case)


@pytest.mark.parametrize("error,expected", [
    (store.CheckoutDenied("private"), "CHECKOUT_REQUIRED"),
    (store.PhysicalReferenceScopeMismatch("private"), "PROJECT_MISMATCH"),
    (store.PhysicalReferenceInvalid("private"), "CORRUPT"),
    (ph.PhysicalHeadError("PHYSICAL_HEAD_LOG_FULL"), "LOG_FULL"),
    (ValueError("drawing mutations are disabled: private"), "WRITES_DRAINED"),
    (ValueError("an active checkout is required: private"), "CHECKOUT_REQUIRED"),
    (ValueError("apply id was reused: private"), "CONFLICT"),
    (ValueError("stale parent 1"), "CONFLICT"),
    (ValueError("stale drawing head: 1"), "CONFLICT"),
    (ValueError("physical commit requires a graph version"), "CORRUPT"),
    (OSError("private"), "STORE_UNAVAILABLE"), (RuntimeError("private"), "STORE_UNAVAILABLE"),
])
def test_commit_error_mapping(case, monkeypatch, error, expected):
    def fail(*a, **k):
        raise error
    monkeypatch.setattr(write_loop, "commit_physical_version", fail)
    refused("PHYSICAL_HEAD_" + expected, publish, case)


def test_versioned_project_required_before_read(case, monkeypatch):
    def no_read(*a, **k):
        pytest.fail("missing project reached a read")
    monkeypatch.setattr(case.backend, "get", no_read)
    monkeypatch.setattr(store, "load_manifest", no_read)
    refused("PHYSICAL_HEAD_PROJECT_ID_INVALID", publish, case, project_id=None)


@pytest.fixture
def http(case, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    import deps
    import entitlements
    from envelopes import install_error_handlers
    from routers import drawings, solar_terrain, solar_tracker_rows
    monkeypatch.setattr(drawings, "_backend", lambda *a: case.backend)
    monkeypatch.setattr(solar_terrain, "_terrain_backend", lambda *a: case.backend)
    monkeypatch.setattr(solar_tracker_rows, "_backend", lambda *a: case.backend)
    monkeypatch.setattr(solar_terrain, "_scope_refusal", lambda *a: None)
    monkeypatch.setattr(solar_tracker_rows, "_scope_refusal", lambda *a: None)
    monkeypatch.setattr(entitlements, "resolve_tier", lambda *a: "test")
    monkeypatch.setattr(entitlements, "resolve_roles", lambda *a: ([], False))
    monkeypatch.setattr(entitlements, "entitlements_for", lambda *a: {"upload": True})
    app = FastAPI()
    app.dependency_overrides[deps.require_active_tenant] = lambda: case.tenant
    install_error_handlers(app)
    for module in (drawings, solar_terrain, solar_tracker_rows):
        app.include_router(module.router)
    with TestClient(app) as client:
        yield client


def route_post(client, c, route, base=None):
    url = f"/api/drawings/{c.drawing}"
    if route == "landxml":
        return client.post(url + "/imports/landxml", content=xml_cases.REAL,
            params={"drawing_units": "m", "crs": "none", "project_id": c.project},
            headers={"Content-Type": "application/xml"})
    if route == "terrain":
        return client.post(url + "/terrain/operations", params={"project_id": c.project},
            json={"operation": "frame-generate", "expected_head": base,
                  "boundary": frame_cases.SQUARE_M, "preset": frame_cases.PRESET, "drawing_units": "m"})
    return client.post(url + "/tracker-rows", params={"project_id": c.project},
        json={"operation": "manual-create", "rows": row_cases.RAW,
              "module_power_watts": 450, "expected_head": base})


@pytest.mark.parametrize("route,code", [("landxml", "LANDXML_CHECKOUT_DENIED"),
    ("terrain", "TERRAIN_CHECKOUT_DENIED"), ("rows", "TRACKER_ROWS_CHECKOUT_REQUIRED")])
def test_routes_deny_another_holders_checkout_without_writes(case, http, route, code):
    base = imported(case)["head"]["state"]["artifact_id"] if route == "terrain" else None
    before = snapshot(case)
    response = route_post(http, case, route, base)
    assert response.status_code == 403 and response.json()["error"]["reason_code"] == code
    assert snapshot(case) == before


@pytest.mark.parametrize("route", ["landxml", "terrain", "rows"])
def test_routes_edit_after_undo_accept_their_own_retry(case, http, monkeypatch, route):
    from routers import drawings, solar_terrain, solar_tracker_rows
    for module in (drawings, solar_terrain, solar_tracker_rows):
        monkeypatch.setattr(module, "_lock_authorization", lambda *a: (case.holder, case.fence))
    # Keep a real undone-to terrain state while allowing a fresh LandXML import.
    base_doc = document(state={"grid": {"rows": 2, "cols": 2, "x_min": -100., "x_max": 100.,
        "y_min": -100., "y_max": 100., "elevations": [0., 0., 0., 0.]}})
    first = publish(case, base_doc)
    base = first["head"]["state"]["artifact_id"]
    discarded = publish(case, document(base, 2))
    store.undo(case.backend, case.tenant, case.drawing, holder=case.holder, fence=case.fence)
    response = route_post(http, case, route, base)
    assert response.status_code in (200, 201), response.text
    result = response.json()
    assert result["created"] and result["head"]["parent"] == base
    assert result["head"]["index"] > discarded["head"]["index"]
    before = manifest(case)
    retry = route_post(http, case, route, base)
    assert retry.status_code == 200, retry.text
    assert retry.json()["created"] is False and retry.json()["head"] == result["head"]
    assert manifest(case) == before
