"""Offline coverage for the local adapter's first graph and terminal proof."""
import copy
from contextlib import contextmanager
import hashlib
import json
import sys
from pathlib import Path

import pytest

SERVER = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVER))
sys.path.insert(0, str(SERVER / "tests"))

import write_loop
import store
import leaf_cloud_client as cloud
import solar_local_graph as local
from solar_design_graph import GraphValidationError
from solar_graph_seed import new_empty_graph
from solar_sizing_client import digest
from solar_solve_results import publish_version
from test_w1_design_graph import graph  # noqa: F401
from test_w1_solve_commit import seed_graphless, seed


TENANT = "fixture-tenant"
DRAWING = "solar"
ORDINARY_KEYS = {"schema_version", "adapter", "tenant_id", "job_id", "tool",
                 "project_id", "drawing_id", "request_sha256", "new_version",
                 "before_graph_sha256", "graph_sha256", "intake_sha256",
                 "before_rev", "after_rev", "drawing_changed", "replayed"}
SEED_KEYS = ORDINARY_KEYS | {"initialized", "seed_base_rev", "seed_base_graph_sha256",
                             "parent_intake_sha256"}


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*args, **kwargs):
        pytest.fail("offline network forbidden")
    monkeypatch.setattr(cloud.requests.sessions.Session, "request", refuse)


@contextmanager
def held(backend):
    fence = store.acquire_checkout_fence(backend, TENANT, DRAWING, "fixture-owner", 300)
    try:
        yield fence
    finally:
        store.release_checkout(backend, TENANT, DRAWING, "fixture-owner")


@contextmanager
def refused(code):
    with pytest.raises(GraphValidationError) as excinfo:
        yield
    assert excinfo.value.code == code


def request(backend):
    _, key = store.resolve_version(backend, TENANT, DRAWING, 1)
    return {"drawing_id": DRAWING, "expected_rev": 0,
            "changes": {"panels_in_sequence": 3}, "initialize": {
                "schema_version": 1,
                "source_intake_sha256": hashlib.sha256(backend.get(key)).hexdigest(),
                "units": {"drawing_units": "ft",
                          "wcs_to_ucs": [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1],
                          "elevation_datum": "unknown", "crs": None}}}


def run(backend, params, **overrides):
    kwargs = {"drawing_id": DRAWING, "source_version": 1, "holder": "fixture-owner",
              "fence": 1, "job_id": "local-job"}
    tool = overrides.pop("tool", "solar-settings")
    kwargs.update(overrides)
    return local.run_local_graph_commit(backend, TENANT, tool, params, **kwargs)


def proof(backend, result, params, **overrides):
    kwargs = {"tenant_id": TENANT, "job_id": "local-job", "tool": "solar-settings",
              "source_version": 1, "backend": backend}
    kwargs.update(overrides)
    return local.graph_commit_provenance(result, params, **kwargs)


def versions(backend, count):
    assert len(store.load_manifest(backend, TENANT, DRAWING)["versions"]) == count


@pytest.fixture
def graphless(tmp_path, monkeypatch):
    backend, intake = seed_graphless(tmp_path, monkeypatch)
    return backend, intake, request(backend)


@pytest.fixture
def committed(graphless):
    backend, intake, params = graphless
    with held(backend) as fence:
        result = run(backend, params, fence=fence)
    return backend, intake, params, result


def edit(backend):
    params = {"drawing_id": DRAWING, "expected_rev": 1, "changes": {"num_mppt": 2}}
    with held(backend) as fence:
        result = run(backend, params, source_version=2, job_id="edit-job", fence=fence)
    return params, result


def expected_proof(result):
    return {"execution_mode": "local_graph_commit", "adapter": local.ADAPTER_KIND,
            "request_sha256": result["request_sha256"], "graph_sha256": result["graph_sha256"],
            "intake_sha256": result["intake_sha256"], "source_version": 1,
            "new_version": 2, "seeded": True}


def test_seed_receipt_and_preserved_intake(committed):
    backend, intake, params, result = committed
    _, _, parent = store.resolve_version_entry(backend, TENANT, DRAWING, 1)
    base = new_empty_graph(tenant_id=TENANT, drawing_id=DRAWING,
                           source_hash=params["initialize"]["source_intake_sha256"],
                           units=params["initialize"]["units"], created_at=parent["created"])
    assert set(result) == SEED_KEYS and len(result) == 20
    assert result["schema_version"] == local.SEED_RESULT_SCHEMA
    assert result["adapter"] == local.ADAPTER_KIND
    assert result["tenant_id"] == TENANT and result["job_id"] == "local-job"
    assert result["tool"] == "solar-settings" and result["drawing_id"] == DRAWING
    assert result["initialized"] is True
    assert result["before_rev"] is None and result["before_graph_sha256"] is None
    assert result["seed_base_rev"] == 0 and result["seed_base_graph_sha256"] == digest(base)
    assert result["parent_intake_sha256"] == params["initialize"]["source_intake_sha256"]
    assert result["after_rev"] == 1 and result["drawing_changed"] is True
    assert result["new_version"] == {"drawing_id": DRAWING, "version": 2, "parent": 1}
    assert result["replayed"] is False
    assert result["project_id"] == "leaf:project:0c30002c-4ce6-4673-979a-dcb5f2d2c852"
    assert result["request_sha256"] == local.request_digest(
        "solar-settings", DRAWING, 1, {k: v for k, v in params.items() if k != "drawing_id"})
    _, key, entry = store.resolve_version_entry(backend, TENANT, DRAWING, 2)
    data = backend.get(key)
    stored = json.loads(data)
    seeded = stored.pop("solar_design_graph")
    assert stored.pop("solar_design_graph_sha256") == digest(seeded) == result["graph_sha256"]
    assert hashlib.sha256(data).hexdigest() == result["intake_sha256"]
    assert seeded["rev"] == 1 and seeded["parent_rev"] == 0
    assert seeded["settings"]["panels_in_sequence"] == 3
    assert seeded["panels"] == [] and seeded["strings"] == []
    assert seeded["source_hash"] == result["parent_intake_sha256"]
    assert stored == intake and stored["custom"]["keep"] == [1, 2, 3]
    assert entry["note"] == "solar-graph-seed:" + result["request_sha256"]
    versions(backend, 2)


def test_seed_replay_without_checkout(committed):
    backend, _, params, result = committed
    assert run(backend, params, fence=1) == dict(result, replayed=True)
    versions(backend, 2)


def test_seed_reused_job_binds_units(committed):
    backend, _, params, result = committed
    params["initialize"]["units"]["crs"] = "EPSG:4326"
    changed = local.request_digest("solar-settings", DRAWING, 1,
                                   {k: v for k, v in params.items() if k != "drawing_id"})
    assert changed != result["request_sha256"]
    with refused("JOB_BINDING_REUSED"):
        run(backend, params)
    versions(backend, 2)


def test_graphless_ordinary_request_still_refused(graphless):
    backend, _, params = graphless
    params.pop("initialize")
    with held(backend) as fence, refused("GRAPH_NOT_EMBEDDED"):
        run(backend, params, fence=fence)
    versions(backend, 1)


def test_seed_refuses_embedded_graph(graph, tmp_path, monkeypatch):
    backend, _ = seed(tmp_path, monkeypatch, graph)
    with held(backend) as fence, refused("GRAPH_ALREADY_EMBEDDED"):
        run(backend, request(backend), fence=fence)
    versions(backend, 1)


@pytest.mark.parametrize("overrides,code", [
    ({"tool": "solar-correct-string"}, "INVALID_SEED_REQUEST"),
    ({"project_id": "leaf:project:00000000-0000-4000-8000-000000000001"},
     "SEED_PROJECT_SCOPE_UNSUPPORTED"),
])
def test_seed_scope_refusals(graphless, overrides, code):
    backend, _, params = graphless
    with held(backend) as fence, refused(code):
        run(backend, params, fence=fence, **overrides)
    versions(backend, 1)


@pytest.mark.parametrize("mutation", [
    "list", "extra", "missing_units", "version_two", "version_bool", "version_string",
    "bad_hash", "units_extra", "parsec",
])
def test_invalid_initialize(graphless, mutation):
    backend, _, params = graphless
    initialize = params["initialize"]
    if mutation == "list":
        params["initialize"] = []
    elif mutation == "extra":
        initialize["extra"] = 1
    elif mutation == "missing_units":
        initialize.pop("units")
    elif mutation.startswith("version_"):
        initialize["schema_version"] = {"version_two": 2, "version_bool": True,
                                         "version_string": "1"}[mutation]
    elif mutation == "bad_hash":
        initialize["source_intake_sha256"] = "xyz"
    elif mutation == "units_extra":
        initialize["units"]["extra"] = 1
    else:
        initialize["units"]["drawing_units"] = "parsec"
    with held(backend) as fence, refused("INVALID_SEED_REQUEST"):
        run(backend, params, fence=fence)
    versions(backend, 1)


def test_seed_source_mismatch(graphless):
    backend, _, params = graphless
    params["initialize"]["source_intake_sha256"] = "0" * 64
    with held(backend) as fence, refused("SOURCE_HASH_MISMATCH"):
        run(backend, params, fence=fence)
    versions(backend, 1)


def test_seed_refuses_moved_head(graphless):
    backend, intake, params = graphless
    data = cloud.canonical_bytes(dict(intake, competitor=True))
    with held(backend) as fence:
        assert write_loop._put_bytes_version(
            backend, TENANT, DRAWING, data, parent_version=1, meta={"tool": "fixture"},
            holder="fixture-owner", fence=fence, require_parent_is_head=True) == 2
        with refused("STALE_GRAPH_REVISION"):
            run(backend, params, fence=fence)
    version, key = store.resolve_version(backend, TENANT, DRAWING, "head")
    assert version == 2 and backend.get(key) == data
    versions(backend, 2)


def test_seed_cancel_publishes_nothing(graphless):
    backend, _, params = graphless
    params.pop("changes")
    params["cancel"] = True
    with held(backend) as fence, refused("GRAPH_COMMIT_CANCELLED"):
        run(backend, params, fence=fence)
    versions(backend, 1)


@pytest.mark.parametrize("mutation,code", [
    ("rev_one", "STALE_GRAPH_REVISION"), ("rev_missing", "STALE_GRAPH_REVISION"),
    ("changes_missing", "INVALID_SETTINGS_REQUEST"), ("changes_unknown", "INVALID_SETTINGS_REQUEST"),
])
def test_seed_builtin_refusals(graphless, mutation, code):
    backend, _, params = graphless
    if mutation == "rev_one":
        params["expected_rev"] = 1
    elif mutation == "rev_missing":
        params.pop("expected_rev")
    elif mutation == "changes_missing":
        params.pop("changes")
    else:
        params["changes"] = {"nope": 1}
    with held(backend) as fence, refused(code):
        run(backend, params, fence=fence)
    versions(backend, 1)


def test_seed_requires_checkout(graphless):
    backend, _, params = graphless
    with refused("CHECKOUT_REQUIRED"):
        run(backend, params)
    versions(backend, 1)


def test_ordinary_successor_keeps_ordinary_receipt(committed):
    backend, _, _, _ = committed
    _, result = edit(backend)
    assert set(result) == ORDINARY_KEYS
    assert result["schema_version"] == local.RESULT_SCHEMA
    assert result["before_rev"] == 1 and result["after_rev"] == 2
    assert result["new_version"] == {"drawing_id": DRAWING, "version": 3, "parent": 2}
    versions(backend, 3)


def test_seed_terminal_proof(committed):
    backend, _, params, result = committed
    assert proof(backend, result, params) == expected_proof(result)


def test_seed_terminal_proof_survives_head_advance(committed):
    backend, _, params, result = committed
    edit(backend)
    assert proof(backend, result, params) == expected_proof(result)


@pytest.mark.parametrize("target,path,value", [
    ("call", ("tenant_id",), "other-tenant"),
    ("call", ("job_id",), "other-job"),
    ("call", ("tool",), "solar-correct-string"),
    ("params", ("drawing_id",), "other"),
    ("call", ("source_version",), 2),
    ("result", ("request_sha256",), "0" * 64),
    ("result", ("project_id",), "leaf:project:00000000-0000-4000-8000-000000000001"),
    ("result", ("replayed",), "not-a-boolean"),
    ("result", ("initialized",), False),
    ("result", ("before_rev",), 0),
    ("result", ("before_graph_sha256",), "0" * 64),
    ("result", ("seed_base_rev",), 1),
    ("result", ("seed_base_rev",), True),
    ("result", ("seed_base_graph_sha256",), "0" * 64),
    ("result", ("parent_intake_sha256",), "0" * 64),
    ("result", ("graph_sha256",), "0" * 64),
    ("result", ("intake_sha256",), "0" * 64),
    ("result", ("after_rev",), 2),
    ("result", ("drawing_changed",), False),
    ("result", ("new_version", "version"), 3),
    ("result", ("schema_version",), local.RESULT_SCHEMA),
    ("remove", ("initialize",), None),
    ("params", ("initialize", "units", "crs"), "EPSG:4326"),
    ("params", ("initialize", "schema_version"), True),
])
def test_seed_proof_rejects_alterations(committed, target, path, value):
    backend, _, params, result = committed
    overrides = {}
    if target == "remove":
        params.pop(path[0])
    else:
        obj = {"call": overrides, "params": params, "result": result}[target]
        for key in path[:-1]:
            obj = obj[key]
        obj[path[-1]] = value
    with pytest.raises(ValueError, match="^graph commit terminal proof rejected$"):
        proof(backend, result, params, **overrides)


def test_ordinary_proof_refuses_initialize(graph, tmp_path, monkeypatch):
    backend, _ = seed(tmp_path, monkeypatch, graph)
    parent = local.resolve_graph_context(backend, TENANT, DRAWING, 1)
    builtin_params = {"expected_rev": parent["graph"]["rev"], "changes": {"num_mppt": 2}}
    after = local._load_builtin("solar-settings").run(copy.deepcopy(parent["graph"]), builtin_params)
    builtin_params["initialize"] = request(backend)["initialize"]
    params = dict(builtin_params, drawing_id=DRAWING)
    request_sha256 = local.request_digest("solar-settings", DRAWING, 1, builtin_params)
    with held(backend) as fence:
        receipt = publish_version(
            backend, TENANT, DRAWING, parent_version=1, before=parent["graph"], after=after,
            holder="fixture-owner", fence=fence, job_id="local-job", request_sha256=request_sha256)
    context = local.resolve_graph_context(backend, TENANT, DRAWING, receipt["version"])
    result = {
        "schema_version": local.RESULT_SCHEMA, "adapter": local.ADAPTER_KIND,
        "tenant_id": TENANT, "job_id": "local-job", "tool": "solar-settings",
        "project_id": parent["project_id"], "drawing_id": DRAWING,
        "request_sha256": receipt["request_sha256"],
        "new_version": {"drawing_id": DRAWING, "version": receipt["version"],
                        "parent": receipt["parent_version"]},
        "before_graph_sha256": parent["graph_sha256"],
        "graph_sha256": context["graph_sha256"], "intake_sha256": receipt["intake_sha256"],
        "before_rev": parent["graph"]["rev"], "after_rev": context["graph"]["rev"],
        "drawing_changed": True, "replayed": receipt["replayed"]}
    with pytest.raises(ValueError, match="^graph commit terminal proof rejected$"):
        proof(backend, result, params)


def test_seed_proof_rejects_rewritten_manifest_note(committed):
    backend, _, params, result = committed
    manifest = store.load_manifest(backend, TENANT, DRAWING)
    entry = next(row for row in manifest["versions"] if row["v"] == 2)
    entry["note"] = "solar-graph-commit:" + result["request_sha256"]
    store.save_manifest(backend, TENANT, DRAWING, manifest)
    with pytest.raises(ValueError, match="^graph commit terminal proof rejected$"):
        proof(backend, result, params)


def rewrite_intake(backend, version, intake):
    _, key = store.resolve_version(backend, TENANT, DRAWING, version)
    data = cloud.canonical_bytes(intake)
    backend.put(key, data)
    sha256 = hashlib.sha256(data).hexdigest()
    manifest = store.load_manifest(backend, TENANT, DRAWING)
    entry = next(row for row in manifest["versions"] if row["v"] == version)
    entry["sha256"] = sha256
    store.save_manifest(backend, TENANT, DRAWING, manifest)
    return sha256


@pytest.mark.parametrize("companion", ["solar_design_graph", "solar_design_graph_sha256"])
def test_seed_proof_rejects_a_rewritten_parent(committed, graph, companion):
    # Rewriting the parent makes source_intake_sha256 stale; companion rules live in test_w1_graph_seed.py.
    backend, intake, params, result = committed
    intake[companion] = graph if companion == "solar_design_graph" else digest(graph)
    rewrite_intake(backend, 1, intake)
    with pytest.raises(ValueError, match="^graph commit terminal proof rejected$"):
        proof(backend, result, params)


def test_seed_proof_rejects_changed_parent_content(committed):
    backend, _, params, result = committed
    _, key = store.resolve_version(backend, TENANT, DRAWING, 2)
    intake = json.loads(backend.get(key))
    intake["injected"] = 1
    result["intake_sha256"] = rewrite_intake(backend, 2, intake)
    assert local.resolve_graph_context(backend, TENANT, DRAWING, 2)["graph_sha256"] == result["graph_sha256"]
    with pytest.raises(ValueError, match="^graph commit terminal proof rejected$"):
        proof(backend, result, params)


@pytest.mark.parametrize("value", [True, 1.0])
def test_seed_proof_rejects_json_type_change_in_preserved_content(committed, value):
    backend, _, params, result = committed
    _, key = store.resolve_version(backend, TENANT, DRAWING, 2)
    intake = json.loads(backend.get(key))
    intake["custom"]["keep"][0] = value
    result["intake_sha256"] = rewrite_intake(backend, 2, intake)
    with pytest.raises(ValueError, match="^graph commit terminal proof rejected$"):
        proof(backend, result, params)


def test_seed_proof_rederives_the_stored_graph(committed):
    backend, _, params, result = committed
    _, key = store.resolve_version(backend, TENANT, DRAWING, 2)
    intake = json.loads(backend.get(key))
    intake["solar_design_graph"]["settings"]["num_mppt"] = 9
    intake["solar_design_graph_sha256"] = digest(intake["solar_design_graph"])
    result["graph_sha256"] = intake["solar_design_graph_sha256"]
    result["intake_sha256"] = rewrite_intake(backend, 2, intake)
    context = local.resolve_graph_context(backend, TENANT, DRAWING, 2)
    assert context["graph_sha256"] == result["graph_sha256"]
    with pytest.raises(ValueError, match="^graph commit terminal proof rejected$"):
        proof(backend, result, params)


@pytest.mark.parametrize("error", [IndexError("private-store-detail"),
                                 OverflowError("private-store-detail")])
@pytest.mark.parametrize("ordinary", [False, True])
def test_seed_proof_contains_unexpected_exceptions(committed, monkeypatch, error, ordinary):
    backend, _, params, result = committed
    overrides = {}
    resolver = "resolve_seed_context"
    if ordinary:
        params, result = edit(backend)
        overrides = {"source_version": 2, "job_id": "edit-job"}
        resolver = "resolve_graph_context"

    def boom(*args, **kwargs):
        raise error

    monkeypatch.setattr(local, resolver, boom)
    with pytest.raises(ValueError, match="^graph commit terminal proof rejected$") as excinfo:
        proof(backend, result, params, **overrides)
    assert "private-store-detail" not in str(excinfo.value)


def test_seed_proof_hides_context_runtime_failure(committed, monkeypatch):
    backend, _, params, result = committed
    def boom(*args, **kwargs):
        raise RuntimeError("boom")
    monkeypatch.setattr(local, "resolve_seed_context", boom)
    with pytest.raises(ValueError, match="^graph commit terminal proof rejected$") as excinfo:
        proof(backend, result, params)
    assert "boom" not in str(excinfo.value)
