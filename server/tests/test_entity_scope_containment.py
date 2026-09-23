"""SSD1 C1: the frozen entity binding contains every reachable drawing writer."""
from copy import deepcopy
import hashlib
import json
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import broker
import broker_client
import jobs
import mutation_plan
import write_loop
import store
import entity_scope_containment as scope


PARENT = "This change was prepared for a different version of the drawing. Nothing was published."
UNCHECKABLE = "This drawing cannot be checked completely. Nothing was published."
TOOL = {"name": "scope-write", "version": "1.0.0", "capabilities": ["drawing.write"]}
TENANT = "scope-tenant"
B = {"drawing_id": "demo", "base_version": 2,
     "base_source_sha256": hashlib.sha256(b"source").hexdigest(),
     "allowed_handles": ["2A"]}


def panel(handle):
    return {"handle": handle, "layer": "Panels", "closed": True,
            "pts": [[0.0, 0.0, 7.0], [2.0, 0.0, 7.0],
                    [2.0, 2.0, 7.0], [0.0, 2.0, 7.0]]}


def intake():
    return {"layers": ["Panels"], "polylines": [panel("2A"), panel("2B")]}


def transform(handle="2A"):
    return {"transforms": [{"handle": handle, "dx": 3, "dy": 4}]}


def out_message(name):
    return f"This change also affects {name}. Nothing was published."


def wide_message(label):
    return f"This change also affects the drawing's {label}. Nothing was published."


def assert_exception(fn, reason, message):
    with pytest.raises(scope.ContainmentRefusal) as caught:
        fn()
    assert caught.value.reason_code == reason
    assert caught.value.message == message == str(caught.value)
    assert not isinstance(caught.value, (ValueError, KeyError))


def assert_refusal(response, reason, message):
    env, status = response
    assert status == 409
    assert env["ok"] is False
    assert env["error"]["error_code"] == "BAD_PARAMS"
    assert env["error"]["reason_code"] == reason
    assert env["error"]["message"] == message
    assert env["error"]["retryable"] is False


def test_c1_known_plan_keys_match_the_contract():
    assert scope.KNOWN_PLAN_KEYS == (
        mutation_plan._MUTATION_FIELDS | set(mutation_plan.V3_SET_OPS) | {"removed_kinds"})


@pytest.mark.parametrize("op", ["transforms", "removed", "set_layer", "set_points",
                                  "set_circle", "set_arc", "set_color", "set_linetype",
                                  "set_lineweight"])
def test_c1_plan_allows_every_op_on_a(op):
    canonical = {op: ["2A"] if op == "removed" else [{"handle": "2A"}]}
    if op == "removed":
        canonical["removed_kinds"] = {"2A": "CIRCLE"}
    scope.check_plan(B, canonical)


def test_c1_plan_refuses_another_target():
    assert_exception(lambda: scope.check_plan(B, transform("2B")),
                     scope.REASON_OUT_OF_SCOPE, out_message("2B"))


def test_c1_plan_refuses_an_addition():
    assert_exception(lambda: scope.check_plan(B, {"added": [panel("C")]}),
                     scope.REASON_OUT_OF_SCOPE, out_message("C"))


def test_c1_plan_refuses_a_replacement():
    assert_exception(lambda: scope.check_plan(B, {"removed": ["2A"], "added": [panel("C")]}),
                     scope.REASON_OUT_OF_SCOPE, out_message("C"))


@pytest.mark.parametrize("key,label", [("added_groups", "groups"),
                                        ("removed_groups", "groups"),
                                        ("block_defs", "block definitions")])
def test_c1_plan_refuses_drawing_wide_ops(key, label):
    assert_exception(lambda: scope.check_plan(B, {key: [{}]}),
                     scope.REASON_DRAWING_WIDE, wide_message(label))


@pytest.mark.parametrize("canonical", [[], {"future": []}, {"removed": "2A"},
                                         {"transforms": [{}]}, {"removed": [1]},
                                         {"set_color": [None]}])
def test_c1_plan_uncheckable_shapes(canonical):
    assert_exception(lambda: scope.check_plan(B, canonical), scope.REASON_UNCHECKABLE, UNCHECKABLE)


def test_c1_plan_names_the_smallest_offender():
    assert_exception(lambda: scope.check_plan(B, {"transforms": [
        {"handle": "2C"}, {"handle": "2B"}]}), scope.REASON_OUT_OF_SCOPE, out_message("2B"))


@pytest.mark.parametrize("change", [{"drawing_id": "other"}, {"version": 1},
                                     {"head_version": 3}, {"stored_source": b"wrong"},
                                     {"version": "2"}, {"stored_source": "source"},
                                     {"head_version": "2"}, {"version": True}])
def test_c1_parent_mismatch(change):
    args = dict(drawing_id="demo", version=2, head_version=2, stored_source=b"source")
    scope.check_parent(B, **args)
    args.update(change)
    assert_exception(lambda: scope.check_parent(B, **args), scope.REASON_PARENT, PARENT)


@pytest.mark.parametrize("kind", ["equal", "dwg", "bundle", "utf8", "list"])
def test_c1_payload_intake(kind):
    source = {"equal": json.dumps(intake()).encode(), "dwg": b"AC1032...",
              "bundle": json.dumps({"intake": intake()}).encode(),
              "utf8": b"\xff", "list": b"[]"}[kind]
    if kind == "equal":
        scope.require_payload_intake(source, intake())
    else:
        assert_exception(lambda: scope.require_payload_intake(source, intake()),
                         scope.REASON_UNCHECKABLE, UNCHECKABLE)


@pytest.mark.parametrize("change", ["points", "collection", "delete", "properties"])
def test_c1_output_allows_changes_to_a_only(change):
    before, after = intake(), intake()
    if change == "points":
        after["polylines"][0]["pts"][0][0] += 1
    elif change == "collection":
        after["polylines"].pop(0)
        after["circles"] = [{"handle": "2A", "c": [0, 0, 0], "r": 2}]
    elif change == "delete":
        after["polylines"].pop(0)
    else:
        after["properties"] = {"2A": {"aci": 1}}
    scope.check_output_exact(B, before, after)


def test_c1_output_refuses_another_record():
    after = intake()
    after["polylines"][1]["pts"][0][0] += 1
    assert_exception(lambda: scope.check_output_exact(B, intake(), after),
                     scope.REASON_OUT_OF_SCOPE, out_message("2B"))


def test_c1b_output_refuses_a_collection_move():
    after = intake()
    moved = after["polylines"].pop()
    after["circles"] = [moved]
    assert_exception(lambda: scope.check_output_exact(B, intake(), after),
                     scope.REASON_OUT_OF_SCOPE, out_message("2B"))


def test_c1b_output_absent_collection_equals_empty():
    before = intake()
    after = deepcopy(before)
    after["texts"] = []
    scope.check_output_exact(B, before, after)
    scope.check_output_exact(B, after, before)


def test_c1_output_refuses_a_deleted_neighbour():
    after = intake()
    after["polylines"].pop()
    assert_exception(lambda: scope.check_output_exact(B, intake(), after),
                     scope.REASON_OUT_OF_SCOPE, out_message("2B"))


def test_c1_output_refuses_an_addition():
    after = {**intake(), "circles": [{"handle": "C9", "c": [0, 0, 0], "r": 2}]}
    assert_exception(lambda: scope.check_output_exact(B, intake(), after),
                     scope.REASON_OUT_OF_SCOPE, out_message("C9"))


def test_c1_output_refuses_another_property():
    after = {**intake(), "properties": {"2B": {"aci": 1}}}
    assert_exception(lambda: scope.check_output_exact(B, intake(), after),
                     scope.REASON_OUT_OF_SCOPE, out_message("2B"))


@pytest.mark.parametrize("key", ["layers", "groups", "extra"])
def test_c1_output_refuses_a_section_change(key):
    before = intake()
    before["groups"] = [{"name": "G1", "members": ["2A", "2B"]}]
    after = deepcopy(before)
    if key == "layers":
        after[key].append("Fresh")
    elif key == "groups":
        after[key][0]["members"].pop()
    else:
        after[key] = []
    assert_exception(lambda: scope.check_output_exact(B, before, after),
                     scope.REASON_DRAWING_WIDE, wide_message(key))


def test_c1_output_absent_equals_empty():
    scope.check_output_exact(B, {}, {"layers": [], "properties": {}})
    scope.check_output_exact(B, {"layers": [], "properties": {}}, {})


@pytest.mark.parametrize("after", [{"polylines": [{}]},
    {"polylines": [panel("2A")], "circles": [{"handle": "2A"}]},
    {"texts": {}}, {"properties": []}, []])
def test_c1_output_uncheckable_shapes(after):
    assert_exception(lambda: scope.check_output_exact(B, {}, after), scope.REASON_UNCHECKABLE, UNCHECKABLE)
    assert_exception(lambda: scope.check_output_exact(B, after, {}), scope.REASON_UNCHECKABLE, UNCHECKABLE)


@pytest.mark.parametrize("name", ["x" * 200, "x\ny", None])
def test_c1_message_bounds(name):
    assert_exception(lambda: scope.check_plan(B, {"added": [{"handle": name}]}),
                     scope.REASON_OUT_OF_SCOPE, out_message("another entity"))
    assert_exception(lambda: scope.check_output_exact(B, {}, {"x-y": []}),
                     scope.REASON_DRAWING_WIDE, wide_message("other data"))


@pytest.mark.parametrize("error,expected", [({"reason_code": "ENTITY_SCOPE_UNCHECKABLE"}, True),
    ({"reason_code": "GRAPH_COMMIT_REFUSED"}, False), ({}, False),
    ({"reason_code": 4}, False), (None, False)])
def test_c1_is_scope_refusal(error, expected):
    assert scope.is_scope_refusal(error) is expected


@pytest.fixture
def drawing(tmp_path, monkeypatch):
    monkeypatch.setenv("LEAF_STORE_DIR", str(tmp_path / "drawings"))
    monkeypatch.setenv("LEAF_BLOB_STORE", "filesystem")
    monkeypatch.setenv("LEAF_DRAWING_MUTATIONS_ENABLED", "1")
    monkeypatch.delenv("LEAF_DRAWING_MUTATIONS_FENCE_FILE", raising=False)
    monkeypatch.setenv("LEAF_RUNTIME_ENV", "test")
    backend = write_loop.default_backend()

    def build(*, groups=False, live=False):
        before = intake()
        if groups:
            before["groups"] = [{"name": "G1", "members": ["2A", "2B"],
                                  "flags": 0, "selectable": 1}]
        write_loop.ensure_demo_drawing(backend, TENANT, "demo")
        payload = b"AC1032\x00source" if live else json.dumps(before).encode()
        assert write_loop._put_bytes_version(backend, TENANT, "demo", payload,
            parent_version=1, meta={"note": "fixture"}) == 2
        if live:
            write_loop.publish_intake_cache(backend, TENANT, "demo", 2, payload, before)
        _, key = store.resolve_version(backend, TENANT, "demo", 2)
        source = backend.get(key)
        binding = {**B, "base_source_sha256": hashlib.sha256(source).hexdigest()}
        return SimpleNamespace(backend=backend, before=before, binding=binding,
                               source=source, key=key)
    return build


def snapshot(d):
    manifest = store.load_manifest(d.backend, TENANT, "demo")
    return deepcopy(manifest), d.backend.get(d.key)


def unchanged(d, saved):
    # Whole-manifest equality covers head, latest, and the complete version list.
    assert snapshot(d) == saved


def planner(mutations):
    return Mock(return_value={"ok": True, "result": {"mutations": deepcopy(mutations)}})


def mock_run(d, mutations=None, *, scoped=True, dry_run=False, run=None):
    return write_loop.run_write_mock(
        TOOL, {"drawing_id": "demo", "dry_run": dry_run}, TENANT,
        backend=d.backend, t0=time.perf_counter(), version=2,
        run_tool_dynamic_fn=run if run is not None else planner(mutations),
        **({"entity_scope": d.binding} if scoped else {}))


def test_c1_mock_scoped_edit_of_a_publishes(drawing):
    d = drawing()
    env, status = mock_run(d, transform())
    assert status == 200
    assert env["result"]["new_version"] == {"drawing_id": "demo", "version": 3, "parent": 2}
    version, after = write_loop.read_intake(d.backend, TENANT, "demo", 3)
    assert version == 3
    assert after["polylines"][0]["pts"][0] == [3.0, 4.0, 7.0]
    assert after["polylines"][1] == d.before["polylines"][1]
    assert d.backend.get(d.key) == d.source


@pytest.mark.parametrize("mutations,reason,message,groups", [
    (transform("2B"), scope.REASON_OUT_OF_SCOPE, out_message("2B"), False),
    ({"added": [panel("C")]}, scope.REASON_OUT_OF_SCOPE, out_message("C"), False),
    ({"removed": ["2A"], "added": [panel("C")]}, scope.REASON_OUT_OF_SCOPE, out_message("C"), False),
    ({"added_groups": [{"name": "G2", "members": ["2A", "2B"]}]},
     scope.REASON_DRAWING_WIDE, wide_message("groups"), False),
    ({"set_layer": [{"handle": "2A", "layer": "Fresh"}]},
     scope.REASON_DRAWING_WIDE, wide_message("layers"), False),
    ({"removed": ["2A"]}, scope.REASON_DRAWING_WIDE, wide_message("groups"), True),
])
def test_c1_mock_scoped_refusals_publish_nothing(drawing, mutations, reason, message, groups):
    d = drawing(groups=groups)
    saved = snapshot(d)
    assert_refusal(mock_run(d, mutations), reason, message)
    unchanged(d, saved)


def test_c1_mock_scoped_parent_checked_before_the_tool_runs(drawing):
    d = drawing()
    d.binding["base_source_sha256"] = "0" * 64
    saved, run = snapshot(d), planner(transform())
    assert_refusal(mock_run(d, run=run), scope.REASON_PARENT, PARENT)
    run.assert_not_called()
    unchanged(d, saved)


def test_c1_mock_scoped_head_moved_refuses(drawing):
    d = drawing()
    write_loop._put_bytes_version(d.backend, TENANT, "demo", d.source,
                                  parent_version=2, meta={"note": "head moved"})
    saved, run = snapshot(d), planner(transform())
    assert_refusal(mock_run(d, run=run), scope.REASON_PARENT, PARENT)
    run.assert_not_called()
    unchanged(d, saved)


def test_c1_mock_scoped_non_payload_intake_refuses(drawing, monkeypatch):
    d = drawing()
    monkeypatch.setattr(write_loop, "read_intake", lambda *a: (2, {**d.before, "extra": 1}))
    saved, run = snapshot(d), planner(transform())
    assert_refusal(mock_run(d, run=run), scope.REASON_UNCHECKABLE, UNCHECKABLE)
    run.assert_not_called()
    unchanged(d, saved)


def _side_effecting_tool(tool, received, params, **kwargs):
    received["polylines"][1]["pts"][0][0] = 999
    received["layers"].append("Injected")
    return {"ok": True, "result": {"mutations": transform()}}


def test_c1b_mock_tool_side_effects_are_not_published(drawing):
    d = drawing()
    env, status = mock_run(d, run=_side_effecting_tool)
    assert status == 200
    assert env["result"]["new_version"] == {
        "drawing_id": "demo", "version": 3, "parent": 2}
    version, after = write_loop.read_intake(d.backend, TENANT, "demo", 3)
    assert version == 3
    assert after["polylines"][0]["pts"][0] == [3.0, 4.0, 7.0]
    assert after["polylines"][1] == d.before["polylines"][1]
    assert after["layers"] == d.before["layers"]


def test_c1b_mock_tool_side_effects_unscoped_unchanged(drawing):
    d = drawing()
    env, status = mock_run(d, scoped=False, run=_side_effecting_tool)
    assert status == 200
    assert env["result"]["new_version"] == {
        "drawing_id": "demo", "version": 3, "parent": 2}
    version, after = write_loop.read_intake(d.backend, TENANT, "demo", 3)
    assert version == 3
    assert after["polylines"][1]["pts"][0][0] == 999
    assert "Injected" in after["layers"]


def test_c1b_plan_check_refusal_is_never_swallowed(drawing, monkeypatch):
    d = drawing()

    def refuse(*args, **kwargs):
        raise scope.ContainmentRefusal(scope.REASON_UNCHECKABLE, UNCHECKABLE)

    monkeypatch.setattr(write_loop, "validate_mutations", refuse)
    assert_refusal(mock_run(d, transform(), dry_run=True),
                   scope.REASON_UNCHECKABLE, UNCHECKABLE)


@pytest.mark.parametrize("handle", ["2A", "2B"])
def test_c1_mock_scoped_dry_run(drawing, handle):
    d = drawing()
    saved = snapshot(d)
    response = mock_run(d, transform(handle), dry_run=True)
    if handle == "2B":
        assert_refusal(response, scope.REASON_OUT_OF_SCOPE, out_message("2B"))
    else:
        env, status = response
        assert status == 200 and env["result"]["dry_run"] is True
        assert "new_version" not in env["result"]
    unchanged(d, saved)


@pytest.mark.parametrize("scoped", [True, False])
def test_c1_mock_scoped_publish_is_compare_and_set(drawing, monkeypatch, scoped):
    d = drawing()
    publish = Mock(wraps=write_loop._put_bytes_version)
    monkeypatch.setattr(write_loop, "_put_bytes_version", publish)
    assert mock_run(d, transform(), scoped=scoped)[1] == 200
    publish.assert_called_once()
    assert publish.call_args.kwargs.get("require_parent_is_head", False) is scoped


def test_c1_mock_scoped_stale_parent_at_publish(drawing, monkeypatch):
    d = drawing()
    saved = snapshot(d)
    monkeypatch.setattr(write_loop, "_put_bytes_version",
                        Mock(side_effect=ValueError("stale parent 2: head is now 3")))
    assert_refusal(mock_run(d, transform()), scope.REASON_PARENT, PARENT)
    unchanged(d, saved)


def test_c1b_mock_scoped_stale_head_postgres_message(drawing, monkeypatch):
    d = drawing()
    saved = snapshot(d)
    monkeypatch.setattr(write_loop, "_put_bytes_version", Mock(
        side_effect=ValueError("stale drawing head: expected 2, current 3")))
    assert_refusal(mock_run(d, transform()), scope.REASON_PARENT, PARENT)
    unchanged(d, saved)


@pytest.mark.parametrize("mutations", [transform("2B"), {"added": [panel("C")]}])
def test_c1_mock_unscoped_is_unchanged(drawing, mutations):
    d = drawing()
    env, status = mock_run(d, mutations, scoped=False)
    assert status == 200
    assert env["result"]["new_version"] == {"drawing_id": "demo", "version": 3, "parent": 2}


class NoAPS:
    def __getattr__(self, name):
        def forbidden(*args, **kwargs):
            pytest.fail(f"unexpected APS call: {name}")
        return forbidden


def live_run(d, run):
    return write_loop.run_write_live(
        TOOL, {"drawing_id": "demo"}, TENANT, backend=d.backend,
        da=NoAPS(), t0=time.perf_counter(), version=2,
        run_tool_dynamic_fn=run, entity_scope=d.binding)


@pytest.mark.parametrize("mutations,reason,message", [
    (transform(), scope.REASON_UNCHECKABLE, UNCHECKABLE),
    (transform("2B"), scope.REASON_OUT_OF_SCOPE, out_message("2B")),
    ({"added": [panel("C")]}, scope.REASON_OUT_OF_SCOPE, out_message("C")),
    ({"added_groups": [{"name": "G2", "members": ["2A", "2B"]}]},
     scope.REASON_DRAWING_WIDE, wide_message("groups")),
])
def test_c1_live_scoped_refuses_before_any_aps_call(drawing, mutations, reason, message):
    d = drawing(live=True)
    saved = snapshot(d)
    assert_refusal(live_run(d, planner(mutations)), reason, message)
    unchanged(d, saved)


def test_c1_live_parent_checked_before_the_planner(drawing):
    d = drawing(live=True)
    d.binding["base_source_sha256"] = "0" * 64
    saved, run = snapshot(d), planner(transform())
    assert_refusal(live_run(d, run), scope.REASON_PARENT, PARENT)
    run.assert_not_called()
    unchanged(d, saved)


def test_c1_live_parent_uses_stored_bytes(drawing, monkeypatch):
    d = drawing(live=True)
    saved, run = snapshot(d), planner(transform())
    bridge = Mock(return_value=(b"different execution bytes", True))
    monkeypatch.setattr(write_loop, "_live_execution_source_bytes", bridge)
    assert_refusal(live_run(d, run), scope.REASON_UNCHECKABLE, UNCHECKABLE)
    bridge.assert_called_once_with(d.source)
    run.assert_called_once()
    unchanged(d, saved)


@pytest.fixture
def broker_lane(monkeypatch):
    monkeypatch.setattr(broker, "tenant_disabled", lambda *a: False)
    monkeypatch.setattr(broker, "_deployed_runtime", lambda: False)
    monkeypatch.setattr(broker, "_tenant_tier", lambda *a: "demo")
    monkeypatch.setattr(broker, "_cap_preflight", lambda *a: None)
    monkeypatch.setattr(broker, "_run_quota_preflight", lambda *a: None)
    monkeypatch.setattr(broker, "_require_supported_live_completion_mode", lambda: None)
    monkeypatch.setattr(broker.entitlements, "entitlements_for",
                        lambda *a: {"run_read": True, "run_write": True})
    monkeypatch.setattr(broker, "_submission_recorder", lambda *a: None)
    monkeypatch.setattr(broker, "_get_da", lambda: NoAPS())


@pytest.mark.parametrize("handle,reason,message", [
    ("2A", scope.REASON_UNCHECKABLE, UNCHECKABLE),
    ("2B", scope.REASON_OUT_OF_SCOPE, out_message("2B")),
])
def test_c1_data_plan_scoped_refuses_before_execution(drawing, broker_lane, monkeypatch,
                                                     handle, reason, message):
    d = drawing(live=True)
    saved = snapshot(d)
    monkeypatch.setattr(write_loop, "default_backend", lambda **k: d.backend)
    start = Mock(side_effect=AssertionError("execution started"))
    writer = Mock(side_effect=AssertionError("data plan writer called"))
    ready = Mock(side_effect=AssertionError("readiness checked"))
    monkeypatch.setattr(broker, "_start_admitted_execution", start)
    monkeypatch.setattr(broker, "_plan_activity_ready", ready)
    monkeypatch.setattr(write_loop, "run_data_plan_live", writer)
    req = broker.BrokerPlanRunRequest(
        tenant_id=TENANT, dwg="demo", dwg_version=2, entity_scope=d.binding,
        plan={"drawing_id": "demo", "parent_version": 2, "mutations": transform(handle),
              "plan_sha256": "a" * 64, "source_sha256": d.binding["base_source_sha256"]})
    assert_refusal(broker._execute_plan(req, broker.PLAN_TOOL, "", time.perf_counter(), {}),
                   reason, message)
    start.assert_not_called()
    writer.assert_not_called()
    ready.assert_not_called()
    unchanged(d, saved)


@pytest.mark.parametrize("mismatch", ["digest", "base_version"])
def test_c1b_data_plan_parent_mismatch(drawing, broker_lane, monkeypatch, mismatch):
    d = drawing(live=True)
    saved = snapshot(d)
    binding = deepcopy(d.binding)
    if mismatch == "digest":
        binding["base_source_sha256"] = "0" * 64
    else:
        binding["base_version"] = 1
    monkeypatch.setattr(write_loop, "default_backend", lambda **k: d.backend)
    start = Mock(side_effect=AssertionError("execution started"))
    writer = Mock(side_effect=AssertionError("data plan writer called"))
    monkeypatch.setattr(broker, "_start_admitted_execution", start)
    monkeypatch.setattr(write_loop, "run_data_plan_live", writer)
    req = broker.BrokerPlanRunRequest(
        tenant_id=TENANT, dwg="demo", dwg_version=2, entity_scope=binding,
        plan={"drawing_id": "demo", "parent_version": 2, "mutations": transform(),
              "plan_sha256": "a" * 64,
              "source_sha256": d.binding["base_source_sha256"]})
    assert_refusal(broker._execute_plan(
        req, broker.PLAN_TOOL, "", time.perf_counter(), {}),
        scope.REASON_PARENT, PARENT)
    start.assert_not_called()
    writer.assert_not_called()
    unchanged(d, saved)


@pytest.mark.parametrize("path", ["live", "degraded", "mock"])
def test_c1_broker_forwards_scope_only_when_present(drawing, broker_lane, monkeypatch, path):
    d = drawing()
    monkeypatch.setattr(write_loop, "default_backend", lambda **k: d.backend)
    monkeypatch.setattr(broker, "_start_admitted_execution", lambda *a, **k: None)
    if path == "degraded":
        monkeypatch.setattr(broker, "_get_da", lambda: None)
    live, mock = Mock(return_value=({"ok": True}, 200)), Mock(return_value=({"ok": True}, 200))
    monkeypatch.setattr(write_loop, "run_write_live", live)
    monkeypatch.setattr(write_loop, "run_write_mock", mock)
    for scoped in (True, False):
        req = broker.BrokerRunRequest(
            tenant_id=TENANT, tool=TOOL, params={"drawing_id": "demo"},
            dwg="demo", dwg_version=2, aps_live=path != "mock",
            **({"entity_scope": d.binding} if scoped else {}))
        assert broker._execute(req, TOOL, "", time.perf_counter(), {})[1] == 200
        kwargs = (live if path == "live" else mock).call_args.kwargs
        if scoped:
            assert kwargs["entity_scope"] == d.binding
        else:
            assert "entity_scope" not in kwargs
        if path == "degraded":
            assert kwargs["degraded"] is True
    assert (live if path == "live" else mock).call_count == 2
    (mock if path == "live" else live).assert_not_called()


class Queue:
    def __init__(self):
        self.pending = []

    def submit(self, fn, *args, **kwargs):
        self.pending.append((fn, args, kwargs))

    def run(self):
        fn, args, kwargs = self.pending.pop(0)
        fn(*args, **kwargs)


@pytest.fixture
def job_lane(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, "DB_PATH", tmp_path / "jobs.db")
    monkeypatch.setattr(jobs, "_conn", None)
    monkeypatch.setattr(jobs, "job_store_mode", lambda: "legacy")
    monkeypatch.setattr(jobs, "ensure_started", lambda: None)
    monkeypatch.setattr(jobs.platform_link, "on_submit", lambda *a, **k: None)
    monkeypatch.setattr(jobs.platform_link, "on_running", lambda *a, **k: None)
    monkeypatch.setattr(jobs, "max_attempts", lambda: 3)
    queue = Queue()
    monkeypatch.setattr(jobs, "_executors", {jobs.LANE_FAST: queue, jobs.LANE_SLOW: queue})
    yield queue
    jobs.reset_connection()


def test_c1_job_scope_refusal_never_falls_back(job_lane, monkeypatch):
    response = scope.refusal_envelope(scope.uncheckable(), tool=TOOL["name"], version="1.0.0")
    assert_refusal(response, scope.REASON_UNCHECKABLE, UNCHECKABLE)
    monkeypatch.setattr(broker_client, "run_via_broker", lambda *a, **k: deepcopy(response[0]))
    fallback = Mock(side_effect=AssertionError("scope refusal fell back"))
    monkeypatch.setattr(jobs, "_run_local_fallback", fallback)
    jid = jobs.submit_job(TENANT, {**TOOL, "allow_local_fallback": True}, {}, "demo", True,
                          dwg_version=2, entity_scope=B)
    job_lane.run()
    fallback.assert_not_called()
    rec = jobs.get_job(jid)
    assert rec["status"] == "failed"
    assert rec["error"] == response[0]["error"]


def test_c1b_unscoped_job_keeps_fallback(job_lane, monkeypatch):
    response = scope.refusal_envelope(
        scope.uncheckable(), tool=TOOL["name"], version="1.0.0")

    def run(*args, **kwargs):
        if args[4]:
            return deepcopy(response[0])
        return {"ok": True, "result": {}}

    monkeypatch.setattr(broker_client, "run_via_broker", run)
    fallback = Mock(wraps=jobs._run_local_fallback)
    monkeypatch.setattr(jobs, "_run_local_fallback", fallback)
    jid = jobs.submit_job(
        TENANT, {**TOOL, "allow_local_fallback": True}, {}, "demo", True,
        dwg_version=2)
    job_lane.run()
    fallback.assert_called_once()
    assert jobs.get_job(jid)["status"] == "complete"


def test_c1_job_other_failures_still_fall_back(job_lane, monkeypatch):
    calls = []

    def run(*args, **kwargs):
        calls.append(args[4])
        if args[4]:
            return {"ok": False, "error": {"error_code": "WORKITEM_FAILED",
                    "message": "cloud failed", "retryable": False}}
        return {"ok": True, "result": {}}

    monkeypatch.setattr(broker_client, "run_via_broker", run)
    fallback = Mock(wraps=jobs._run_local_fallback)
    monkeypatch.setattr(jobs, "_run_local_fallback", fallback)
    jid = jobs.submit_job(TENANT, {**TOOL, "allow_local_fallback": True}, {}, "demo", True,
                          dwg_version=2, entity_scope=B)
    job_lane.run()
    fallback.assert_called_once()
    assert calls == [True, False]
    assert jobs.get_job(jid)["status"] == "complete"
