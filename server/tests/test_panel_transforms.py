"""Unit coverage for the additive panel-transform/v1 mutation contract."""
from __future__ import annotations

import copy
import math
import time

import pytest

from write_loop import (
    apply_mutations,
    default_backend,
    read_intake,
    run_write_mock,
    undo_view,
)


def panel(handle="A", points=None, **metadata):
    entity = {
        "handle": handle,
        "layer": "Panels",
        "closed": True,
        "pts": points or [[0.0, 0.0, 7.0], [2.0, 0.0, 7.0],
                          [2.0, 2.0, 7.0], [0.0, 2.0, 7.0]],
        "xdata": {"source": "fixture"},
    }
    entity.update(metadata)
    return entity


def intake(*polylines):
    return {"layers": ["Panels"], "polylines": list(polylines), "drawing": "fixture"}


def mutation(handle="A", dx=0.0, dy=0.0, rotation_deg=None):
    transform = {"handle": handle, "dx": dx, "dy": dy}
    if rotation_deg is not None:
        transform["rotation_deg"] = rotation_deg
    return {"transforms": [transform]}


def xy(entity):
    return [point[:2] for point in entity["pts"]]


def edge_lengths(entity):
    points = entity["pts"]
    return [math.dist(points[index][:2], points[(index + 1) % len(points)][:2])
            for index in range(len(points))]


def test_translation_defaults_rotation_to_zero():
    result = apply_mutations(intake(panel()), mutation(dx=3, dy=-4))
    assert xy(result["polylines"][0]) == [
        [3.0, -4.0], [5.0, -4.0], [5.0, -2.0], [3.0, -2.0]]


def test_rotation_uses_source_vertex_centroid():
    result = apply_mutations(intake(panel()), mutation(rotation_deg=90))
    assert xy(result["polylines"][0]) == [
        [2.0, 0.0], [2.0, 2.0], [0.0, 2.0], [0.0, 0.0]]


def test_rotation_then_translation():
    result = apply_mutations(intake(panel()), mutation(dx=10, dy=20, rotation_deg=-90))
    assert xy(result["polylines"][0]) == [
        [10.0, 22.0], [10.0, 20.0], [12.0, 20.0], [12.0, 22.0]]


def test_preserves_z_metadata_vertex_order_and_edge_lengths():
    source = panel(custom={"nested": [1, 2]})
    before_lengths = edge_lengths(source)
    result = apply_mutations(intake(source), mutation(dx=1.25, dy=8.5, rotation_deg=37))
    changed = result["polylines"][0]
    assert [point[2:] for point in changed["pts"]] == [[7.0]] * 4
    assert {key: changed[key] for key in ("handle", "layer", "closed", "xdata", "custom")} == {
        key: source[key] for key in ("handle", "layer", "closed", "xdata", "custom")}
    assert edge_lengths(changed) == pytest.approx(before_lengths, abs=2e-9)


def test_rounds_xy_and_normalizes_negative_zero():
    source = panel(points=[[-0.0000000001, 0.0, 1], [0.0000000001, 0.0, 1],
                           [0.0000000001, 0.0000000001, 1]])
    result = apply_mutations(intake(source), mutation())
    for point in result["polylines"][0]["pts"]:
        assert point[0] == 0.0 and math.copysign(1, point[0]) == 1
        assert point[1] == 0.0 and math.copysign(1, point[1]) == 1


def test_coexists_with_add_and_remove_without_changing_old_behavior():
    added = panel("C", layer="NewLayer")
    result = apply_mutations(
        intake(panel("A"), panel("B")),
        {"transforms": [{"handle": "A", "dx": 4, "dy": 5}],
         "removed": ["B"], "added": [added]},
    )
    assert [entity["handle"] for entity in result["polylines"]] == ["A", "C"]
    assert xy(result["polylines"][0])[0] == [4.0, 5.0]
    assert "NewLayer" in result["layers"]


def test_transform_declaration_order_does_not_affect_result():
    source = intake(panel("A"), panel("B"))
    first = {"transforms": [
        {"handle": "A", "dx": 1, "dy": 2, "rotation_deg": 45},
        {"handle": "B", "dx": -3, "dy": 4},
    ]}
    second = {"transforms": list(reversed(first["transforms"]))}
    assert apply_mutations(source, first) == apply_mutations(source, second)


@pytest.mark.parametrize("transforms", [
    None,
    {},
    "not-a-list",
    [],
    [None],
    [{"handle": "", "dx": 0, "dy": 0}],
    [{"handle": 1, "dx": 0, "dy": 0}],
    [{"handle": "UNKNOWN", "dx": 0, "dy": 0}],
    [{"handle": "A", "dx": 0, "dy": 0}, {"handle": "A", "dx": 1, "dy": 1}],
    [{"handle": "A", "dy": 0}],
    [{"handle": "A", "dx": 0}],
    [{"handle": "A", "dx": True, "dy": 0}],
    [{"handle": "A", "dx": 10000.0001, "dy": 0}],
    [{"handle": "A", "dx": 0, "dy": -10000.0001}],
    [{"handle": "A", "dx": 0, "dy": 0, "rotation_deg": 360.1}],
])
def test_rejects_invalid_transform_batches_atomically(transforms):
    source = intake(panel("A"))
    before = copy.deepcopy(source)
    with pytest.raises(ValueError):
        apply_mutations(source, {"transforms": transforms})
    assert source == before


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
@pytest.mark.parametrize("field", ["dx", "dy", "rotation_deg"])
def test_rejects_nonfinite_numbers_atomically(field, value):
    source = intake(panel())
    transform = {"handle": "A", "dx": 0, "dy": 0, "rotation_deg": 0}
    transform[field] = value
    with pytest.raises(ValueError):
        apply_mutations(source, {"transforms": [transform]})
    assert source == intake(panel())


@pytest.mark.parametrize("points", [
    None,
    [],
    [[0, 0], [1, 0]],
    [[0, 0], [1, 0], [1]],
    [[0, 0], [1, 0], [1, "bad"]],
    [[0, 0], [1, 0], [1, float("nan")]],
])
def test_rejects_malformed_target_points_without_mutating_original(points):
    entity = panel()
    entity["pts"] = points
    source = intake(entity)
    before = copy.deepcopy(source)
    with pytest.raises(ValueError):
        apply_mutations(source, mutation())
    assert source == before


def test_returns_new_intake_and_leaves_original_unchanged_on_success():
    source = intake(panel())
    before = copy.deepcopy(source)
    result = apply_mutations(source, mutation(dx=2, dy=3, rotation_deg=15))
    assert source == before
    assert result is not source and result["polylines"][0] is not source["polylines"][0]


def test_transform_persists_as_new_undoable_version(tmp_path, monkeypatch):
    monkeypatch.setenv("LEAF_STORE_DIR", str(tmp_path / "drawings"))
    backend = default_backend()
    captured = {}

    def transform_tool(_tool, source, _params, **_kwargs):
        target = source["polylines"][0]
        captured["handle"] = target["handle"]
        captured["before"] = copy.deepcopy(target["pts"])
        return {
            "ok": True,
            "result": {
                "mutations": {
                    "transforms": [{
                        "handle": target["handle"],
                        "dx": 12.5,
                        "dy": -3.25,
                        "rotation_deg": 0,
                    }]
                }
            },
        }

    envelope, status = run_write_mock(
        {"name": "arrange-panels", "version": "1.0.0"},
        {"drawing_id": "demo"},
        "cat-litmus",
        backend=backend,
        t0=time.perf_counter(),
        run_tool_dynamic_fn=transform_tool,
    )

    assert status == 200
    assert envelope["result"]["new_version"] == {
        "drawing_id": "demo", "version": 2, "parent": 1}
    version, after = read_intake(backend, "cat-litmus", "demo", "head")
    assert version == 2
    changed = next(p for p in after["polylines"] if p["handle"] == captured["handle"])
    assert changed["pts"][0][0] == pytest.approx(captured["before"][0][0] + 12.5)
    assert changed["pts"][0][1] == pytest.approx(captured["before"][0][1] - 3.25)

    undone = undo_view("cat-litmus", "demo", backend=backend)
    assert undone["head"] == 1
    restored = next(p for p in undone["intake"]["polylines"]
                    if p["handle"] == captured["handle"])
    assert restored["pts"] == captured["before"]


def test_dry_run_returns_proposal_without_creating_a_version(tmp_path, monkeypatch):
    monkeypatch.setenv("LEAF_STORE_DIR", str(tmp_path / "drawings"))
    backend = default_backend()

    def transform_tool(_tool, source, _params, **_kwargs):
        target = source["polylines"][0]
        return {
            "ok": True,
            "result": {
                "mutations": {
                    "transforms": [{
                        "handle": target["handle"],
                        "dx": 12.5,
                        "dy": -3.25,
                        "rotation_deg": 0,
                    }]
                }
            },
        }

    envelope, status = run_write_mock(
        {"name": "arrange-panels", "version": "1.0.0"},
        {"drawing_id": "demo", "dry_run": True},
        "cat-litmus",
        backend=backend,
        t0=time.perf_counter(),
        run_tool_dynamic_fn=transform_tool,
    )

    assert status == 200
    assert envelope["result"]["dry_run"] is True
    assert "new_version" not in envelope["result"]
    version, _ = read_intake(backend, "cat-litmus", "demo", "head")
    assert version == 1


def test_write_runner_propagates_tenant_identity_to_execution_boundary(tmp_path, monkeypatch):
    monkeypatch.setenv("LEAF_STORE_DIR", str(tmp_path / "drawings"))
    backend = default_backend()
    captured = {}

    def transform_tool(_tool, _source, _params, **kwargs):
        captured.update(kwargs)
        return {
            "ok": True,
            "result": {"mutations": {}},
        }

    envelope, status = run_write_mock(
        {"name": "tenant-bound-write", "version": "1.0.0"},
        {"drawing_id": "demo", "dry_run": True},
        "acceptance-tenant-a",
        backend=backend,
        t0=time.perf_counter(),
        run_tool_dynamic_fn=transform_tool,
    )

    assert status == 200
    assert envelope["ok"] is True
    assert captured["tenant_id"] == "acceptance-tenant-a"
    assert captured["tenant_id"] != "demo-tenant"
