from __future__ import annotations

import copy
import hashlib
import json
import time
from pathlib import Path

import pytest

import tool_loader
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


# A passing `leaf.tool-execution.v1` microvm receipt as the stub planner
# returns it. The write path never stamps its digest; where the server holds
# a published body for the tool it only CROSS-CHECKS the receipt against the
# digest it measured itself (write_loop `_server_held_source_ref`).
MICROVM_RECEIPT = {
    "contract": "leaf.tool-execution.v1", "provider": "e2b",
    "isolation": "microvm", "passed": True,
    "source_sha256": "a" * 64,
}


def _planner(mutations=None, provenance=None):
    calls = []
    envelope_provenance = (
        copy.deepcopy(MICROVM_RECEIPT) if provenance is None
        else copy.deepcopy(provenance)
    )

    def run(*args, **kwargs):
        calls.append((args, kwargs))
        return {
            "ok": True,
            "result": {"mutations": copy.deepcopy(mutations or _mutations()),
                       "planner_value": "preserved"},
            "overlay": {"kind": "preserved"},
            "execution_provenance": copy.deepcopy(envelope_provenance),
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


def _families_text(intake):
    """Encode the subset emitted by the fixed same-WorkItem inspection."""
    lines = [f"LAYER|{layer}" for layer in intake.get("layers", [])]
    for polyline in intake.get("polylines", []):
        lowered = world_to_ocs(polyline["pts"])
        normal = ",".join(f"{value:.6f}" for value in lowered["normal"])
        lines.append(
            f"PL|{polyline['layer']}|{1 if polyline.get('closed') else 0}|"
            f"{lowered['elevation']:.3f}|{normal}|{polyline.get('handle', '')}"
        )
        lines.extend(
            f"PV|{point[0]:.3f},{point[1]:.3f}"
            for point in lowered["points"]
        )
    return ("\n".join(lines) + "\n").encode("utf-8")


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
        if key.endswith(".txt"):
            return _families_text(self.output_intake)
        return b"AC1032" + b"\x01" * 64

    def extract(self, path):
        raise AssertionError("live writes must not launch a second extraction")

    def delete_scratch_object(self, key):
        pass

    def _engine_seconds(self, status):
        return 2.0

    def _workitem_timing(self, status, submitted_at=None):
        assert submitted_at is not None
        return {
            "contract": "leaf.cad-timing.aps.v1",
            "spans_ms": {
                "submit": 1, "queue": 2, "task_start": 3,
                "engine": 2000, "output_upload": 4,
            },
            "accounted_ms": 2010,
            "unavailable_spans": ["image_pull", "drawing_fetch"],
        }


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


def test_transform_emits_server_lowered_target_geometry_for_existing_handle():
    base = _base()
    canonical = validate_mutations(base, {
        "transforms": [{
            "handle": "A", "dx": 10, "dy": -3, "rotation_deg": 90,
        }],
    })
    plan = emit_plan(
        canonical, base_sha256="3" * 64, base_intake=base)
    assert (
        b"TRANSFORM|A|0,0,1|0|12,-3;12,-1;10,-1;10,-3\n"
        in plan
    )
    assert b"dx" not in plan and b"rotation" not in plan


def test_transform_plan_requires_exact_base_intake():
    canonical = validate_mutations(_base(), {
        "transforms": [{"handle": "A", "dx": 1, "dy": 0}],
    })
    with pytest.raises(ValueError, match="base_intake"):
        emit_plan(canonical, base_sha256="4" * 64)


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
    assert set(arguments) == {"HostDwg", "Plan", "Result", "Intake"}
    assert arguments["HostDwg"]["verb"] == "get"
    assert arguments["Plan"]["verb"] == "get"
    assert arguments["Result"]["verb"] == "put"
    assert arguments["Intake"]["verb"] == "put"
    assert kwargs["dry_run"] is False and kwargs["poll"] is True
    assert env["result"]["planner_value"] == "preserved"
    assert env["overlay"] == {"kind": "preserved"}
    assert env["execution_provenance"]["provider"] == "e2b"
    timing = env["execution_provenance"]["cad_timing"]
    assert timing["contract"] == "leaf.cad-timing.v1"
    assert set(timing["spans_ms"]) == {
        "planner", "submit", "queue", "task_start", "image_pull",
        "drawing_fetch", "engine", "output_upload", "output_inspection",
        "version_write", "publish", "client_delivery",
    }
    assert timing["spans_ms"]["submit"] == 1
    assert timing["spans_ms"]["queue"] == 2
    assert timing["spans_ms"]["task_start"] == 3
    assert timing["spans_ms"]["engine"] == 2000
    assert timing["spans_ms"]["planner"] >= 0
    assert timing["spans_ms"]["output_inspection"] >= 0
    assert timing["spans_ms"]["drawing_fetch"] >= 0
    assert timing["spans_ms"]["version_write"] >= 0
    assert timing["spans_ms"]["publish"] >= 0
    assert timing["spans_ms"]["image_pull"] is None
    assert timing["spans_ms"]["client_delivery"] is None
    assert env["result"]["new_version"]["version"] == 2
    _, intake = write_loop.read_intake(backend, "tenant", "drawing", 2)
    assert intake["dwg"] == "drawing"


def test_live_accepts_measured_three_decimal_extractor_rounding(tmp_path):
    backend = _store(tmp_path)
    expected_x = 17524.4055
    extracted_x = 17524.406
    added = _entity("C", "Leaf Output", z=7.0)
    added["pts"] = [
        [expected_x, 10.0, 7.0], [expected_x + 2.0, 10.0, 7.0],
        [expected_x + 2.0, 12.0, 7.0], [expected_x, 12.0, 7.0],
    ]
    planner, _ = _planner({"removed": ["A"], "added": [added]})
    extracted = copy.deepcopy(added)
    extracted["handle"] = "APS1"
    for point in extracted["pts"]:
        point[0] = extracted_x + (2.0 if point[0] > expected_x else 0.0)
    output = {
        "dwg": "temp-output.dwg",
        "layers": ["Panels", "Leaf Output"],
        "polylines": [_entity("B", z=3.0), extracted],
    }

    env, status = write_loop.run_write_live(
        {"name": "author-tool", "version": "1"},
        {"drawing_id": "drawing"}, "tenant", backend=backend,
        da=FakeDa(output), t0=time.perf_counter(), run_tool_dynamic_fn=planner,
    )

    assert status == 200, env
    assert env["result"]["new_version"]["version"] == 2
    _, intake = write_loop.read_intake(backend, "tenant", "drawing", 2)
    published = next(
        entity for entity in intake["polylines"]
        if entity["handle"] == "APS1"
    )
    assert published["pts"] == extracted["pts"]


def test_live_rejects_full_extractor_quantum_geometry_drift(tmp_path):
    backend = _store(tmp_path)
    added = _entity("C", "Leaf Output", z=7.0)
    added["pts"] = [
        [17524.405, 10.0, 7.0], [17526.405, 10.0, 7.0],
        [17526.405, 12.0, 7.0], [17524.405, 12.0, 7.0],
    ]
    planner, _ = _planner({"removed": ["A"], "added": [added]})
    extracted = copy.deepcopy(added)
    extracted["handle"] = "APS1"
    for point in extracted["pts"]:
        point[0] += 0.001
    output = {
        "dwg": "temp-output.dwg",
        "layers": ["Panels", "Leaf Output"],
        "polylines": [_entity("B", z=3.0), extracted],
    }

    env, status = write_loop.run_write_live(
        {"name": "author-tool", "version": "1"},
        {"drawing_id": "drawing"}, "tenant", backend=backend,
        da=FakeDa(output), t0=time.perf_counter(), run_tool_dynamic_fn=planner,
    )

    assert status == 502
    assert "added polyline" in env["error"]["message"]
    manifest = store.load_manifest(backend, "tenant", "drawing")
    assert manifest["head"] == 1 and manifest["latest"] == 1


def test_live_rejects_malformed_same_workitem_inspection(tmp_path):
    backend = _store(tmp_path)
    planner, _ = _planner()

    class MalformedInspectionDa(FakeDa):
        def download_scratch_object(self, key):
            if key.endswith(".txt"):
                return b"LAYER|Panels\nPL|broken\n"
            return super().download_scratch_object(key)

    env, status = write_loop.run_write_live(
        {"name": "author-tool"}, {"drawing_id": "drawing"}, "tenant",
        backend=backend, da=MalformedInspectionDa(_actual_success()),
        t0=time.perf_counter(), run_tool_dynamic_fn=planner,
    )

    assert status == 502
    assert "malformed records" in env["error"]["message"]
    assert store.load_manifest(backend, "tenant", "drawing")["head"] == 1


def test_live_rejects_oversize_same_workitem_inspection(tmp_path, monkeypatch):
    backend = _store(tmp_path)
    planner, _ = _planner()

    class OversizeInspectionDa(FakeDa):
        def download_scratch_object(self, key):
            if key.endswith(".txt"):
                return b"LAYER|Panels\n"
            return super().download_scratch_object(key)

    monkeypatch.setattr(write_loop, "MAX_OUTPUT_INTAKE_BYTES", 4)
    env, status = write_loop.run_write_live(
        {"name": "author-tool"}, {"drawing_id": "drawing"}, "tenant",
        backend=backend, da=OversizeInspectionDa(_actual_success()),
        t0=time.perf_counter(), run_tool_dynamic_fn=planner,
    )

    assert status == 502
    assert "size limit" in env["error"]["message"]
    assert store.load_manifest(backend, "tenant", "drawing")["head"] == 1


def test_live_rejects_nonempty_corrupt_dwg_with_valid_inspection(tmp_path):
    backend = _store(tmp_path)
    planner, _ = _planner()

    class CorruptDwgDa(FakeDa):
        def download_scratch_object(self, key):
            if key.endswith(".txt"):
                return _families_text(self.output_intake)
            return b"nonempty but not a DWG"

    env, status = write_loop.run_write_live(
        {"name": "author-tool"}, {"drawing_id": "drawing"}, "tenant",
        backend=backend, da=CorruptDwgDa(_actual_success()),
        t0=time.perf_counter(), run_tool_dynamic_fn=planner,
    )

    assert status == 502
    assert "invalid output.dwg header" in env["error"]["message"]
    assert store.load_manifest(backend, "tenant", "drawing")["head"] == 1


def test_live_accepts_legacy_mbcs_inspection_bytes(tmp_path):
    backend = _store(tmp_path)
    planner, _ = _planner()

    class MbcsInspectionDa(FakeDa):
        def download_scratch_object(self, key):
            if key.endswith(".txt"):
                return b"LAYER|Caf\xe9\n" + _families_text(self.output_intake)
            return super().download_scratch_object(key)

    env, status = write_loop.run_write_live(
        {"name": "author-tool"}, {"drawing_id": "drawing"}, "tenant",
        backend=backend, da=MbcsInspectionDa(_actual_success()),
        t0=time.perf_counter(), run_tool_dynamic_fn=planner,
    )

    assert status == 200, env
    _, intake = write_loop.read_intake(backend, "tenant", "drawing", 2)
    assert "Caf\ufffd" in intake["layers"]


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


def test_live_transform_preserves_handle_and_verifies_target_geometry(tmp_path):
    backend = _store(tmp_path)
    mutations = {
        "transforms": [{
            "handle": "A", "dx": 5, "dy": 2, "rotation_deg": 90,
        }],
    }
    planner, _ = _planner(mutations)
    output = _base()
    output["polylines"][0]["pts"] = [
        [7.0, 2.0, 0.0], [7.0, 4.0, 0.0],
        [5.0, 4.0, 0.0], [5.0, 2.0, 0.0],
    ]
    da = FakeDa(output)
    env, status = write_loop.run_write_live(
        {"name": "arrange-panels-as-cat", "version": "1"},
        {"drawing_id": "drawing"}, "tenant", backend=backend, da=da,
        t0=time.perf_counter(), run_tool_dynamic_fn=planner,
    )
    assert status == 200, env
    assert b"TRANSFORM|A|" in next(
        value for key, value in da.staged.items() if key.endswith(".txt"))
    _, intake = write_loop.read_intake(backend, "tenant", "drawing", 2)
    assert intake["polylines"][0]["handle"] == "A"
    assert intake["polylines"][0]["pts"] == output["polylines"][0]["pts"]


def test_live_transform_accepts_three_decimal_extractor_rounding(tmp_path):
    backend = _store(tmp_path)
    mutations = {
        "transforms": [{
            "handle": "A", "dx": 5.1234, "dy": 2.4567,
            "rotation_deg": 37,
        }],
    }
    planner, _ = _planner(mutations)
    output = write_loop.apply_mutations(_base(), mutations)
    for point in output["polylines"][0]["pts"]:
        point[0] = round(point[0], 3)
        point[1] = round(point[1], 3)

    env, status = write_loop.run_write_live(
        {"name": "arrange-panels-as-cat", "version": "1"},
        {"drawing_id": "drawing"}, "tenant", backend=backend,
        da=FakeDa(output), t0=time.perf_counter(),
        run_tool_dynamic_fn=planner,
    )

    assert status == 200, env
    _, intake = write_loop.read_intake(backend, "tenant", "drawing", 2)
    assert intake["polylines"][0]["handle"] == "A"
    assert intake["polylines"][0]["pts"] == output["polylines"][0]["pts"]


def test_live_transform_geometry_mismatch_never_publishes(tmp_path):
    backend = _store(tmp_path)
    planner, _ = _planner({
        "transforms": [{"handle": "A", "dx": 5, "dy": 2}],
    })
    da = FakeDa(_base())
    env, status = write_loop.run_write_live(
        {"name": "arrange-panels-as-cat"}, {"drawing_id": "drawing"},
        "tenant", backend=backend, da=da, t0=time.perf_counter(),
        run_tool_dynamic_fn=planner,
    )
    assert status == 502
    assert "transformed handle" in env["error"]["message"]
    assert store.load_manifest(backend, "tenant", "drawing")["head"] == 1


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


def test_live_effect_verification_accepts_extractor_coordinate_quantization():
    base = {"dwg": "source.dwg", "layers": [], "polylines": []}
    added = _entity("CENTERED", "Leaf Output")
    added["pts"] = [
        [10.0005, 20.0005, 0.0],
        [12.0625, 20.0625, 0.0],
        [12.0625, 22.0625, 0.0],
        [-10.0625, 22.0625, 0.0],
    ]
    actual = {
        "dwg": "output.dwg",
        "layers": ["Leaf Output"],
        "polylines": [{
            **copy.deepcopy(added),
            "handle": "APS1",
            "pts": [
                [10.001, 20.001, 0.0],
                [12.063, 20.063, 0.0],
                [12.063, 22.063, 0.0],
                [-10.063, 22.063, 0.0],
            ],
        }],
    }

    write_loop.verify_live_mutation_effects(
        base, actual, {"added": [added]},
    )


def test_live_effect_verification_rejects_change_beyond_extractor_precision():
    base = {"dwg": "source.dwg", "layers": [], "polylines": []}
    added = _entity("CENTERED", "Leaf Output")
    actual = {
        "dwg": "output.dwg",
        "layers": ["Leaf Output"],
        "polylines": [{
            **copy.deepcopy(added),
            "handle": "APS1",
            "pts": [[0.001, 0.0, 0.0], *added["pts"][1:]],
        }],
    }

    with pytest.raises(ValueError, match="added polyline"):
        write_loop.verify_live_mutation_effects(
            base, actual, {"added": [added]},
        )


def test_live_effect_verification_models_tilted_plane_extraction():
    base = {"dwg": "source.dwg", "layers": [], "polylines": []}
    added = _entity("TILTED", "Leaf Output")
    added["pts"] = [
        [15000.1234, 1000.5678, 10.1234],
        [15010.1234, 1000.5678, 12.1234],
        [15010.1234, 1010.5678, 12.1234],
        [15000.1234, 1010.5678, 10.1234],
    ]
    actual = {
        "dwg": "output.dwg",
        "layers": ["Leaf Output"],
        "polylines": [{
            **copy.deepcopy(added),
            "handle": "APS1",
            "pts": [
                [15000.128, 1000.568, 10.121],
                [15010.128, 1000.568, 12.121],
                [15010.128, 1010.568, 12.121],
                [15000.128, 1010.568, 10.121],
            ],
        }],
    }

    write_loop.verify_live_mutation_effects(
        base, actual, {"added": [added]},
    )


def test_live_effect_verification_preserves_open_unchanged_polylines():
    unchanged = _entity("A")
    unchanged["closed"] = False
    base = {"dwg": "source.dwg", "layers": ["Panels"],
            "polylines": [unchanged]}
    added = _entity("B", "Leaf Output")
    actual_added = copy.deepcopy(added)
    actual_added["handle"] = "APS1"
    actual = {"dwg": "output.dwg", "layers": ["Panels", "Leaf Output"],
              "polylines": [copy.deepcopy(unchanged), actual_added]}

    write_loop.verify_live_mutation_effects(
        base, actual, {"added": [added]},
    )


def test_live_effect_verification_rejects_changed_closed_state():
    unchanged = _entity("A")
    base = {"dwg": "source.dwg", "layers": ["Panels"],
            "polylines": [unchanged]}
    added = _entity("B", "Leaf Output")
    actual_unchanged = copy.deepcopy(unchanged)
    actual_unchanged["closed"] = False
    actual_added = copy.deepcopy(added)
    actual_added["handle"] = "APS1"
    actual = {"dwg": "output.dwg", "layers": ["Panels", "Leaf Output"],
              "polylines": [actual_unchanged, actual_added]}

    with pytest.raises(ValueError, match="unchanged handle"):
        write_loop.verify_live_mutation_effects(
            base, actual, {"added": [added]},
        )


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


# ---------------------------------------------------------------------------
# Provenance is SERVER-HELD or absent (standardization slice 6a, third round)
#
# The digest stamped as `source_ref` is measured by THIS process over the
# published tool body it resolves for the tool id
# (`tool_loader.published_tool_source_sha256`), BEFORE the planner runs. The
# planner envelope is never the source: on the in-process and `subprocess`
# sandbox tiers (broker.py permits both for tenant-authored tools outside
# production) tool_loader adopts a tool's own `{ok, result}` return whole, so
# `execution_provenance` there is whatever the tool body chose to say, and a
# shape check of a claim the attacker controls is not a fence. Two fences now:
# tool_loader drops a tool-supplied `execution_provenance` at the seam it
# enters, and write_loop stamps only what the server measured, cross-checking
# a verified microvm receipt against it where one exists.
#
# The rows below drive the REAL tool_loader -> write_loop seam on both
# non-microvm tiers with a body that forges a passing microvm receipt over a
# 64-hex digest, then the stub-planner rows pin the cross-check.
# ---------------------------------------------------------------------------
FORGED = "f" * 64

# A receipt-SHAPED claim: exactly what `_valid_microvm_provenance` accepts, so
# the only thing standing between it and the version chain is the design.
FORGED_RECEIPT = {
    "contract": "leaf.tool-execution.v1", "provider": "e2b",
    "isolation": "microvm", "passed": True, "source_sha256": FORGED,
}


def _capture_version_meta(monkeypatch):
    """Record the meta stamped on the committed version, still committing it."""
    seen = {}
    real = write_loop._put_bytes_version

    def capturing(*args, **kwargs):
        seen["meta"] = copy.deepcopy(kwargs.get("meta"))
        return real(*args, **kwargs)

    monkeypatch.setattr(write_loop, "_put_bytes_version", capturing)
    return seen


def _tenant_repo(tmp_path, monkeypatch):
    """Point tool_loader's tenant-repo root at a tmp repo (test_hardening_2b's recipe)."""
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    monkeypatch.setattr(tool_loader, "_tenant_repo_root", lambda tid=None: repo)
    return repo


def _published_tool(repo, name, source):
    """Publish `source` as the tenant's tools/<name>/tool.py; return its record.

    The record is what the broker folds from the tenant registry (a name and a
    repo-relative entry). The server resolves that entry against its own copy
    of the tenant repo, which is the ONLY place the stamp may read from.
    """
    body = repo / "tools" / name / "tool.py"
    body.parent.mkdir(parents=True, exist_ok=True)
    body.write_text(source, encoding="utf-8")
    return {"name": name, "version": "1", "entry": f"tools/{name}/tool.py"}


def _server_digest(source):
    """What `published_tool_source_sha256` measures: sha256 over the UTF-8 text."""
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


def _forging_body(mutations, provenance):
    """A body returning a FULL envelope whose `execution_provenance` is `provenance`.

    tool_loader adopts a full `{ok, result}` return whole, so without the seam
    fence this claim would ride the envelope into write_loop untouched.
    """
    return (
        "import json\n"
        f"MUTATIONS = json.loads({json.dumps(json.dumps(mutations))})\n"
        f"PROVENANCE = json.loads({json.dumps(json.dumps(provenance))})\n"
        "\n"
        "def run(intake, params):\n"
        "    return {'ok': True, 'tool': 'forger', 'version': '1',\n"
        "            'result': {'mutations': MUTATIONS, 'planner_value': 'forged'},\n"
        "            'overlay': None, 'timing_ms': 1, 'cost': None,\n"
        "            'degraded_mode': False, 'error': None,\n"
        "            'execution_provenance': PROVENANCE}\n"
    )


def _honest_body(mutations):
    """A body that returns a plain (result, overlay) pair and claims nothing."""
    return (
        "import json\n"
        f"MUTATIONS = json.loads({json.dumps(json.dumps(mutations))})\n"
        "\n"
        "def run(intake, params):\n"
        "    return ({'mutations': MUTATIONS, 'planner_value': 'honest'}, None)\n"
    )


def _select_tier(monkeypatch, tier):
    monkeypatch.delenv("LEAF_TOOL_SANDBOX_PROVIDER", raising=False)
    if tier == "subprocess":
        monkeypatch.setenv("LEAF_SANDBOX", "e2b")
    else:
        monkeypatch.delenv("LEAF_SANDBOX", raising=False)
    assert tool_loader._sandbox_tier() == ("subprocess" if tier == "subprocess" else "off")


# --------------------------------------------------------------------------- #
# The real seam: tool_loader.run_tool_dynamic -> write_loop.run_write_live
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("tier", ["subprocess", "in-process"])
def test_real_seam_a_forged_receipt_is_never_stamped_the_server_digest_is(
        monkeypatch, tmp_path, tier):
    _select_tier(monkeypatch, tier)
    backend = _store(tmp_path)
    repo = _tenant_repo(tmp_path, monkeypatch)
    source = _forging_body(_mutations(), FORGED_RECEIPT)
    tool = _published_tool(repo, f"forger-{tier}", source)
    expected = _server_digest(source)
    assert expected != FORGED
    seen = _capture_version_meta(monkeypatch)

    env, status = write_loop.run_write_live(
        tool, {"drawing_id": "drawing"}, "tenant", backend=backend,
        da=FakeDa(_actual_success()), t0=time.perf_counter(),
        run_tool_dynamic_fn=tool_loader.run_tool_dynamic,
    )

    assert status == 200, env
    # The REAL body ran and its envelope was adopted whole (the seam is live).
    assert env["result"]["planner_value"] == "forged"
    # The stamp is what the server measured over the published body...
    assert seen["meta"]["source_ref"] == expected
    assert env["result"]["mutation_binding"]["tool_source_sha256"] == expected
    rows = store.load_manifest(backend, "tenant", "drawing")["versions"]
    assert [r.get("source_ref") for r in rows] == [None, expected]
    # ...and the forgery reached neither the chain nor the envelope: the loader
    # dropped the tool-supplied claim at the seam it entered.
    assert FORGED not in json.dumps(env)
    assert FORGED not in json.dumps(rows)


@pytest.mark.parametrize("tier", ["subprocess", "in-process"])
def test_real_seam_stamps_the_server_digest_without_any_claim_from_the_body(
        monkeypatch, tmp_path, tier):
    # Positive control for the design: a body that claims NOTHING about
    # itself still gets the server's digest, so the stamp is server-derived,
    # not merely "the sandbox's claim when it happens to be right".
    _select_tier(monkeypatch, tier)
    backend = _store(tmp_path)
    repo = _tenant_repo(tmp_path, monkeypatch)
    source = _honest_body(_mutations())
    tool = _published_tool(repo, f"honest-{tier}", source)
    seen = _capture_version_meta(monkeypatch)

    env, status = write_loop.run_write_live(
        tool, {"drawing_id": "drawing"}, "tenant", backend=backend,
        da=FakeDa(_actual_success()), t0=time.perf_counter(),
        run_tool_dynamic_fn=tool_loader.run_tool_dynamic,
    )

    assert status == 200, env
    assert env["result"]["planner_value"] == "honest"
    assert seen["meta"]["source_ref"] == _server_digest(source)
    assert env["result"]["mutation_binding"]["tool_source_sha256"] == _server_digest(source)


def test_real_seam_stamps_null_when_the_server_holds_no_published_body(
        monkeypatch, tmp_path):
    # A design-time STAGED source runs on the subprocess tier without a
    # published body behind the tool id (tool_loader's `test_source` seam).
    # The body forges a receipt; the server holds nothing for that id, so the
    # honest answer is null, and the forgery still goes nowhere.
    _select_tier(monkeypatch, "subprocess")
    backend = _store(tmp_path)
    _tenant_repo(tmp_path, monkeypatch)  # empty: nothing published
    tool = {"name": "candidate", "version": "1", "entry": "tools/candidate/tool.py"}
    staged = _forging_body(_mutations(), FORGED_RECEIPT)
    assert tool_loader.resolve_local_file(tool, "tenant") is None
    assert tool_loader.published_tool_source_sha256(tool, "tenant") is None
    seen = _capture_version_meta(monkeypatch)

    def staged_planner(*args, **kwargs):
        return tool_loader.run_tool_dynamic(*args, test_source=staged, **kwargs)

    env, status = write_loop.run_write_live(
        tool, {"drawing_id": "drawing"}, "tenant", backend=backend,
        da=FakeDa(_actual_success()), t0=time.perf_counter(),
        run_tool_dynamic_fn=staged_planner,
    )

    assert status == 200, env
    assert env["result"]["planner_value"] == "forged"
    assert seen["meta"]["source_ref"] is None
    assert env["result"]["mutation_binding"]["tool_source_sha256"] is None
    rows = store.load_manifest(backend, "tenant", "drawing")["versions"]
    assert [r.get("source_ref") for r in rows] == [None, None]
    assert FORGED not in json.dumps(env)


def test_the_loader_drops_a_tool_supplied_execution_provenance_at_the_seam(
        monkeypatch, tmp_path):
    # The first fence in isolation: even before write_loop, a full envelope
    # adopted from a tool body carries no `execution_provenance` of the
    # tool's choosing on a tier that produced no verified receipt.
    _select_tier(monkeypatch, "subprocess")
    repo = _tenant_repo(tmp_path, monkeypatch)
    tool = _published_tool(repo, "seam", _forging_body(_mutations(), FORGED_RECEIPT))
    env = tool_loader.run_tool_dynamic(
        tool, _base(), {}, aps_live=False, da=None, tenant_id="tenant")
    assert env["ok"] is True and env["result"]["planner_value"] == "forged"
    assert "execution_provenance" not in env


def test_published_tool_source_sha256_measures_the_body_the_sandbox_is_fed(
        monkeypatch, tmp_path):
    # The value the stamp uses is the value a GENUINE microvm receipt carries:
    # tool_loader's audit hashes `local.read_text("utf-8")` re-encoded, and so
    # does this. A body with CRLF endings on disk hashes the same either way,
    # because both read through the same universal-newline text path.
    repo = _tenant_repo(tmp_path, monkeypatch)
    source = "def run(intake, params):\n    return ({'ok': 1}, None)\n"
    tool = _published_tool(repo, "measured", source)
    local = tool_loader.resolve_local_file(tool, "tenant")
    assert local is not None
    audit_style = hashlib.sha256(
        local.read_text(encoding="utf-8").encode("utf-8")).hexdigest()
    assert tool_loader.published_tool_source_sha256(tool, "tenant") == audit_style
    assert tool_loader.published_tool_source_sha256(tool, "tenant") == _server_digest(source)
    # Absence is honest on every miss the server can have.
    assert tool_loader.published_tool_source_sha256(
        {"name": "nobody", "version": "1"}, "tenant") is None
    assert tool_loader.published_tool_source_sha256(
        {"name": "dangling", "entry": "tools/dangling/tool.py"}, "tenant") is None
    local.write_bytes(b"\xff\xfe not utf-8 \x00")
    assert tool_loader.published_tool_source_sha256(tool, "tenant") is None


# --------------------------------------------------------------------------- #
# The cross-check: a VERIFIED microvm receipt against the server's digest
# --------------------------------------------------------------------------- #
def test_a_verified_receipt_that_agrees_with_the_server_is_stamped(
        monkeypatch, tmp_path):
    backend = _store(tmp_path)
    repo = _tenant_repo(tmp_path, monkeypatch)
    source = _honest_body(_mutations())
    tool = _published_tool(repo, "agreed", source)
    expected = _server_digest(source)
    planner, _ = _planner(provenance={**MICROVM_RECEIPT, "source_sha256": expected})
    seen = _capture_version_meta(monkeypatch)
    env, status = write_loop.run_write_live(
        tool, {"drawing_id": "drawing"}, "tenant", backend=backend,
        da=FakeDa(_actual_success()), t0=time.perf_counter(),
        run_tool_dynamic_fn=planner,
    )
    assert status == 200
    assert seen["meta"]["source_ref"] == expected
    assert env["result"]["mutation_binding"]["tool_source_sha256"] == expected
    rows = store.load_manifest(backend, "tenant", "drawing")["versions"]
    assert [r.get("source_ref") for r in rows] == [None, expected]


def test_a_verified_receipt_that_disagrees_withholds_the_stamp_with_a_warning(
        monkeypatch, tmp_path, caplog):
    backend = _store(tmp_path)
    repo = _tenant_repo(tmp_path, monkeypatch)
    source = _honest_body(_mutations())
    tool = _published_tool(repo, "disagreed", source)
    planner, _ = _planner()  # MICROVM_RECEIPT: "a" * 64, not the body's digest
    assert _server_digest(source) != "a" * 64
    seen = _capture_version_meta(monkeypatch)
    with caplog.at_level("WARNING", logger="write_loop"):
        env, status = write_loop.run_write_live(
            tool, {"drawing_id": "drawing"}, "tenant", backend=backend,
            da=FakeDa(_actual_success()), t0=time.perf_counter(),
            run_tool_dynamic_fn=planner,
        )
    # The write succeeds; what fails closed is the PROVENANCE CLAIM. Neither
    # side's digest is trusted over the other.
    assert status == 200
    assert seen["meta"]["source_ref"] is None
    rows = store.load_manifest(backend, "tenant", "drawing")["versions"]
    assert [r.get("source_ref") for r in rows] == [None, None]
    assert any("source_ref_withheld" in r.getMessage()
               and "reason=receipt_mismatch" in r.getMessage()
               for r in caplog.records)


def test_a_receipt_alone_without_a_server_held_body_is_never_stamped(
        monkeypatch, tmp_path, caplog):
    # Before this round a passing receipt was stamped verbatim. It is not
    # anymore: with nothing server-held behind the tool id there is nothing
    # to attribute, and the receipt's digest is never copied into the chain.
    backend = _store(tmp_path)
    planner, _ = _planner()
    seen = _capture_version_meta(monkeypatch)
    with caplog.at_level("WARNING", logger="write_loop"):
        env, status = write_loop.run_write_live(
            {"name": "author-tool", "version": "1"},
            {"drawing_id": "drawing"}, "tenant", backend=backend,
            da=FakeDa(_actual_success()), t0=time.perf_counter(),
            run_tool_dynamic_fn=planner,
        )
    assert status == 200
    assert seen["meta"]["source_ref"] is None
    assert env["result"]["mutation_binding"]["tool_source_sha256"] is None
    rows = store.load_manifest(backend, "tenant", "drawing")["versions"]
    assert [r.get("source_ref") for r in rows] == [None, None]
    assert any("reason=no_server_held_source" in r.getMessage()
               for r in caplog.records)


@pytest.mark.parametrize("bad", [
    None, 123, "A" * 64, "a" * 63, "sha256:" + "a" * 64, "g" * 64,
])
def test_a_malformed_server_digest_fails_closed(bad):
    assert write_loop._server_held_source_ref(bad, {}, tool="t") is None
    assert write_loop._server_held_source_ref(bad, MICROVM_RECEIPT, tool="t") is None
    # Not vacuous: a well-formed one passes with no receipt and with an
    # agreeing receipt.
    good = "0123456789abcdef" * 4
    assert write_loop._server_held_source_ref(good, {}, tool="t") == good
    assert write_loop._server_held_source_ref(
        good, {**MICROVM_RECEIPT, "source_sha256": good}, tool="t") == good


@pytest.mark.parametrize("provenance", [
    # The subprocess tier: a real posture, no receipt, forged digest.
    {"contract": "leaf.tool-execution.v1", "provider": "subprocess",
     "isolation": "process", "passed": True, "source_sha256": "b" * 64},
    # Right provider, right isolation, but the run did not pass.
    {"contract": "leaf.tool-execution.v1", "provider": "e2b",
     "isolation": "microvm", "passed": False, "source_sha256": "b" * 64},
    # Right provider, wrong isolation.
    {"contract": "leaf.tool-execution.v1", "provider": "e2b",
     "isolation": "process", "passed": True, "source_sha256": "b" * 64},
    # A tool that simply asserts a digest and nothing else.
    {"source_sha256": "b" * 64},
    # A digest that is not a sha256 at all.
    {"contract": "leaf.tool-execution.v1", "provider": "e2b",
     "isolation": "microvm", "passed": True, "source_sha256": "not-a-digest"},
    # No provenance whatsoever.
    {},
], ids=["subprocess-tier", "not-passed", "wrong-isolation",
        "bare-assertion", "not-a-sha256", "absent"])
def test_a_planner_assertion_without_a_server_held_body_is_never_stamped(
        monkeypatch, tmp_path, provenance):
    backend = _store(tmp_path)
    planner, _ = _planner(provenance=provenance)
    seen = _capture_version_meta(monkeypatch)
    env, status = write_loop.run_write_live(
        {"name": "author-tool", "version": "1"},
        {"drawing_id": "drawing"}, "tenant", backend=backend,
        da=FakeDa(_actual_success()), t0=time.perf_counter(),
        run_tool_dynamic_fn=planner,
    )
    # The write itself still succeeds outside the protected production
    # posture; what fails closed is the PROVENANCE CLAIM, not the write.
    assert status == 200
    assert seen["meta"]["source_ref"] is None
    rows = store.load_manifest(backend, "tenant", "drawing")["versions"]
    assert [r.get("source_ref") for r in rows] == [None, None]
    # The planner's own claim is not a binding fact and is recorded nowhere:
    # the binding carries the server's measurement, which is null here.
    assert env["result"]["mutation_binding"]["tool_source_sha256"] is None
    assert "b" * 64 not in json.dumps(env["result"]["mutation_binding"])
