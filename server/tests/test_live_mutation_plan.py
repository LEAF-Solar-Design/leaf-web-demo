from __future__ import annotations

import copy
import json
import time
from pathlib import Path

import pytest

import write_loop
import store
from mutation_plan import emit_plan, validate_mutations, world_to_ocs


def _entity(handle, layer="Panels", z=0.0):
    return {
        "handle": handle, "layer": layer, "closed": True, "xdata": None,
        "pts": [[0.0, 0.0, z], [2.0, 0.0, z], [2.0, 2.0, z], [0.0, 2.0, z]],
    }


def _base():
    return {"dwg": "source.dwg", "layers": ["Panels"],
            "polylines": [_entity("A"), _entity("B", z=3.0)]}


def _mutations():
    added = _entity("C", "Leaf Output", z=7.0)
    added["pts"] = [[10.0, 10.0, 7.0], [12.0, 10.0, 7.0],
                    [12.0, 12.0, 7.0], [10.0, 12.0, 7.0]]
    return {"removed": ["A"], "added": [added]}


def _planner(mutations=None):
    calls = []

    def run(*args, **kwargs):
        calls.append((args, kwargs))
        return {
            "ok": True,
            "result": {"mutations": copy.deepcopy(mutations or _mutations()),
                       "planner_value": "preserved"},
            "overlay": {"kind": "preserved"},
            "execution_provenance": {
                "contract": "leaf.tool-execution.v1", "provider": "e2b",
                "isolation": "microvm", "passed": True,
                "source_sha256": "a" * 64,
            },
            "timing_ms": 1, "cost": None, "degraded_mode": False,
        }

    return run, calls


def _store(tmp_path):
    backend = store.FilesystemBackend(str(tmp_path / "drawings"))
    source = tmp_path / "base.dwg"
    source.write_bytes(b"AC1032" + b"\x00" * 64)
    store.ingest_drawing(backend, "tenant", str(source), drawing_id="drawing")
    write_loop.publish_intake_cache(
        backend, "tenant", "drawing", 1, source.read_bytes(), _base())
    return backend


class FakeDa:
    def __init__(self, output_intake):
        self.output_intake = output_intake
        self.staged = {}
        self.submissions = []

    def ephemeral_input_key(self, name, tenant_id, ts):
        return f"t/{tenant_id}/in/{ts}_{name}"

    def ephemeral_output_key(self, name, tenant_id, ts, suffix):
        return f"t/{tenant_id}/out/{ts}_{name}{suffix}"

    def upload_scratch_object(self, path, key):
        self.staged[key] = Path(path).read_bytes()

    def scratch_signed_download_url(self, key):
        return f"https://scratch.test/get/{key}"

    def scratch_signed_upload_url(self, key):
        return "upload-token", f"https://scratch.test/put/{key}"

    def activity_qualified(self, name):
        return f"owner.{name}+prod"

    def submit_workitem(self, activity, arguments, **kwargs):
        self.submissions.append((activity, arguments, kwargs))
        return {"id": "wi-1", "status": "success"}

    def finalize_scratch_upload(self, key, token):
        assert token == "upload-token"

    def download_scratch_object(self, key):
        return b"AC1032" + b"\x01" * 64

    def extract(self, path):
        return copy.deepcopy(self.output_intake)

    def delete_scratch_object(self, key):
        pass

    def _engine_seconds(self, status):
        return 2.0


class FailedDa(FakeDa):
    def submit_workitem(self, activity, arguments, **kwargs):
        self.submissions.append((activity, arguments, kwargs))
        return {
            "id": "wi-secret",
            "status": "failed",
            "reportUrl": "https://reports.test/output?token=do-not-return",
        }


class RaisingDa(FakeDa):
    def submit_workitem(self, activity, arguments, **kwargs):
        raise RuntimeError(
            "request failed at https://objects.test/input?signature=do-not-return")


def _actual_success():
    added = copy.deepcopy(_mutations()["added"][0])
    added["handle"] = "APS1"
    return {"dwg": "temp-output.dwg", "layers": ["Panels", "Leaf Output"],
            "polylines": [_entity("B", z=3.0), added]}


def test_world_to_ocs_round_trips_tilted_planar_points():
    points = [[0.0, 0.0, 0.0], [2.0, 0.0, 2.0],
              [2.0, 3.0, 2.0], [0.0, 3.0, 0.0]]
    lowered = world_to_ocs(points)
    for source, ocs in zip(points, lowered["points"]):
        rebuilt = [
            lowered["axis_x"][axis] * ocs[0]
            + lowered["axis_y"][axis] * ocs[1]
            + lowered["normal"][axis] * lowered["elevation"]
            for axis in range(3)
        ]
        assert rebuilt == pytest.approx(source, abs=1e-8)


def test_distinct_mutations_emit_distinct_canonical_data_plans():
    base = _base()
    first = validate_mutations(base, _mutations(), allow_transforms=False)
    second_raw = _mutations()
    second_raw["added"][0]["pts"][0][0] = 11.0
    second = validate_mutations(base, second_raw, allow_transforms=False)
    first_plan = emit_plan(first, base_sha256="1" * 64)
    second_plan = emit_plan(second, base_sha256="1" * 64)
    assert first_plan != second_plan
    assert b"REMOVE|A\n" in first_plan
    assert b"ADD|Leaf Output|" in first_plan
    assert b"LEAFMARK" not in first_plan


@pytest.mark.parametrize("bad", [
    {"script": "(command \"erase\")"},
    {"removed": ["UNKNOWN"]},
    {"removed": ["A", "A"]},
    {"transforms": [{"handle": "A", "dx": 1, "dy": 0}]},
    {"added": [{**_entity("C"), "command": "erase"}]},
])
def test_validator_rejects_raw_unknown_conflicting_and_unsupported_data(bad):
    with pytest.raises(ValueError):
        validate_mutations(_base(), bad, allow_transforms=False)


def test_nonplanar_add_fails_before_a_plan_is_emitted():
    mutations = _mutations()
    mutations["added"][0]["pts"][3][2] += 1
    canonical = validate_mutations(_base(), mutations, allow_transforms=False)
    with pytest.raises(ValueError, match="not planar"):
        emit_plan(canonical, base_sha256="2" * 64)


def test_live_dry_run_returns_validated_proposal_without_aps(tmp_path):
    backend = _store(tmp_path)
    planner, calls = _planner()
    da = FakeDa(_actual_success())
    env, status = write_loop.run_write_live(
        {"name": "author-tool", "version": "1"},
        {"drawing_id": "drawing", "dry_run": True}, "tenant",
        backend=backend, da=da, t0=time.perf_counter(),
        run_tool_dynamic_fn=planner,
    )
    assert status == 200 and env["result"]["dry_run"] is True
    assert len(calls) == 1
    assert da.submissions == [] and da.staged == {}
    assert store.load_manifest(backend, "tenant", "drawing")["head"] == 1


def test_live_auth_failure_runs_neither_planner_nor_aps(tmp_path):
    backend = _store(tmp_path)
    store.acquire_checkout(backend, "tenant", "drawing", "owner", 300)
    planner, calls = _planner()
    da = FakeDa(_actual_success())
    env, status = write_loop.run_write_live(
        {"name": "author-tool"}, {"drawing_id": "drawing"}, "tenant",
        backend=backend, da=da, t0=time.perf_counter(), holder="intruder",
        run_tool_dynamic_fn=planner,
    )
    assert status == 403 and env["error"]["error_code"] == "FORBIDDEN"
    assert calls == [] and da.submissions == [] and da.staged == {}


def test_live_validation_failure_spends_no_aps(tmp_path):
    backend = _store(tmp_path)
    planner, calls = _planner({"removed": ["UNKNOWN"]})
    da = FakeDa(_actual_success())
    env, status = write_loop.run_write_live(
        {"name": "author-tool"}, {"drawing_id": "drawing"}, "tenant",
        backend=backend, da=da, t0=time.perf_counter(),
        run_tool_dynamic_fn=planner,
    )
    assert status == 400 and len(calls) == 1
    assert da.submissions == [] and da.staged == {}


def test_live_submits_exact_activity_args_and_preserves_planner_result(tmp_path):
    backend = _store(tmp_path)
    planner, calls = _planner()
    da = FakeDa(_actual_success())
    env, status = write_loop.run_write_live(
        {"name": "author-tool", "version": "1"},
        {"drawing_id": "drawing"}, "tenant", backend=backend, da=da,
        t0=time.perf_counter(), run_tool_dynamic_fn=planner,
    )
    assert status == 200 and len(calls) == 1
    activity, arguments, kwargs = da.submissions[0]
    assert activity == "owner.LeafApplyMutations+prod"
    assert set(arguments) == {"HostDwg", "Plan", "Result"}
    assert arguments["HostDwg"]["verb"] == "get"
    assert arguments["Plan"]["verb"] == "get"
    assert arguments["Result"]["verb"] == "put"
    assert kwargs["dry_run"] is False and kwargs["poll"] is True
    assert env["result"]["planner_value"] == "preserved"
    assert env["overlay"] == {"kind": "preserved"}
    assert env["execution_provenance"]["provider"] == "e2b"
    assert env["result"]["new_version"]["version"] == 2
    _, intake = write_loop.read_intake(backend, "tenant", "drawing", 2)
    assert intake["dwg"] == "drawing"


def test_live_workitem_failure_never_returns_report_url(tmp_path):
    backend = _store(tmp_path)
    planner, _ = _planner()
    da = FailedDa(_actual_success())
    env, status = write_loop.run_write_live(
        {"name": "author-tool"}, {"drawing_id": "drawing"}, "tenant",
        backend=backend, da=da, t0=time.perf_counter(),
        run_tool_dynamic_fn=planner,
    )
    assert status == 502
    assert env["error"]["message"] == "APS write WorkItem did not succeed"
    assert "report" not in json.dumps(env).lower()
    assert "do-not-return" not in json.dumps(env)


def test_live_transport_exception_never_returns_exception_text(tmp_path):
    backend = _store(tmp_path)
    planner, _ = _planner()
    da = RaisingDa(_actual_success())
    env, status = write_loop.run_write_live(
        {"name": "author-tool"}, {"drawing_id": "drawing"}, "tenant",
        backend=backend, da=da, t0=time.perf_counter(),
        run_tool_dynamic_fn=planner,
    )
    assert status == 502
    assert env["error"]["message"] == "live drawing mutation failed"
    assert "https://" not in json.dumps(env)
    assert "do-not-return" not in json.dumps(env)


def test_reextract_mismatch_never_publishes(tmp_path):
    backend = _store(tmp_path)
    planner, _ = _planner()
    da = FakeDa(_base())
    env, status = write_loop.run_write_live(
        {"name": "author-tool"}, {"drawing_id": "drawing"}, "tenant",
        backend=backend, da=da, t0=time.perf_counter(),
        run_tool_dynamic_fn=planner,
    )
    assert status == 502 and env["error"]["error_code"] == "WORKITEM_FAILED"
    manifest = store.load_manifest(backend, "tenant", "drawing")
    assert manifest["head"] == 1 and manifest["latest"] == 1


def test_added_effect_cannot_consume_identical_unchanged_entity(tmp_path):
    backend = _store(tmp_path)
    added = copy.deepcopy(_base()["polylines"][1])
    added["handle"] = "C"
    planner, _ = _planner({"removed": ["A"], "added": [added]})
    malicious = {
        "dwg": "output.dwg", "layers": ["Panels"],
        "polylines": [
            _entity("B", z=3.0),
            {**_entity("D", z=9.0), "pts": [[20, 20, 9], [22, 20, 9],
                                               [22, 22, 9], [20, 22, 9]]},
        ],
    }
    da = FakeDa(malicious)
    env, status = write_loop.run_write_live(
        {"name": "author-tool"}, {"drawing_id": "drawing"}, "tenant",
        backend=backend, da=da, t0=time.perf_counter(),
        run_tool_dynamic_fn=planner,
    )
    assert status == 502
    assert "added polyline" in env["error"]["message"]
    assert store.load_manifest(backend, "tenant", "drawing")["head"] == 1


def test_matching_new_entity_without_a_handle_is_refused(tmp_path):
    backend = _store(tmp_path)
    planner, _ = _planner()
    output = _actual_success()
    output["polylines"][1].pop("handle")
    da = FakeDa(output)
    env, status = write_loop.run_write_live(
        {"name": "author-tool"}, {"drawing_id": "drawing"}, "tenant",
        backend=backend, da=da, t0=time.perf_counter(),
        run_tool_dynamic_fn=planner,
    )
    assert status == 502
    assert "nonempty handle" in env["error"]["message"]
    assert store.load_manifest(backend, "tenant", "drawing")["head"] == 1


def test_live_publish_uses_parent_head_cas(monkeypatch, tmp_path):
    backend = _store(tmp_path)
    planner, _ = _planner()
    da = FakeDa(_actual_success())
    observed = {}

    def stale(*args, **kwargs):
        observed.update(kwargs)
        raise ValueError("stale parent 1: head is now 2")

    monkeypatch.setattr(write_loop, "_put_bytes_version", stale)
    env, status = write_loop.run_write_live(
        {"name": "author-tool"}, {"drawing_id": "drawing"}, "tenant",
        backend=backend, da=da, t0=time.perf_counter(),
        run_tool_dynamic_fn=planner,
    )
    assert status == 400 and "stale parent" in env["error"]["message"]
    assert observed["require_parent_is_head"] is True
    assert store.load_manifest(backend, "tenant", "drawing")["head"] == 1
