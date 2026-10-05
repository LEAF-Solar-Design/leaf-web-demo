"""Complete stored block panels use the existing import and proof rails."""
import copy
import json
import math

import pytest

from test_solar_tool_solar_panels_from_drawing import (
    DRAWING, ORDINARY_KEYS, P1, P2, P6, TENANT, builtin, expected_panel,
    g1, kernel, local, no_network, preserved, proof, refused, rewrite_intake,
    run, seed_uploaded, store, stored, version_count,
)


R = [[0, 0], [77, 0], [77, 38.5], [0, 38.5]]


def block_intake():
    return {
        "polylines": [],
        "blocks": {"Module": {
            "base": [0, 0, 0], "count": 1, "complete": True,
            "children": [{"kind": "LWPOLYLINE", "layer": "0", "closed": True,
                          "nrm": [0, 0, 1], "elev": 0, "pts": copy.deepcopy(R)}],
        }},
        "inserts": [
            {"handle": handle, "layer": layer, "name": "Module", "x": x,
             "y": 0, "z": 0, "rot": rotation, "scale": [1, 1, 1],
             "nrm": [0, 0, 1]}
            for handle, layer, x, rotation in (
                ("1A", "Panels", 0, 0),
                ("1B", "roof PANEL layout", 100, math.pi / 2),
                ("2F", "Panels", 200, 0),
            )
        ],
    }


def upload_blocks(tmp_path, monkeypatch, source):
    # Exercise real upload and mutation admission without publication stubs.
    monkeypatch.setenv("LEAF_DRAWING_STORE", "legacy")
    monkeypatch.setenv("LEAF_DRAWING_MUTATIONS_ENABLED", "1")
    monkeypatch.delenv("LEAF_DRAWING_MUTATIONS_FENCE_FILE", raising=False)
    monkeypatch.setenv("LEAF_UPLOAD_IMPORT_MUTATIONS_ENABLED", "1")
    backend = store.FilesystemBackend(str(tmp_path / "drawings"))
    intake = {"dwg": {}, "layers": [], "faces3d": [], "blockdefs": [],
              "geodata": None, "custom": {"keep": [1, 2, 3]}}
    intake.update(copy.deepcopy(source))
    path = tmp_path / "drawing.json"
    path.write_text(json.dumps(intake), encoding="utf-8")
    store.ingest_drawing(backend, TENANT, str(path), drawing_id=DRAWING)
    return backend


def assert_geometry(after, before, handles, centres, angles):
    assert len(after["panels"]) == len(handles)
    for panel, handle, centre, angle in zip(after["panels"], handles, centres, angles):
        assert panel["centre"] == pytest.approx(centre, abs=1e-9, rel=0)
        assert panel["angle"] == pytest.approx(angle, abs=1e-9, rel=0)
        expected = expected_panel(before, handle, panel["centre"], panel["angle"])
        assert panel == expected
    assert after["rev"] == before["rev"] + 1
    assert after["parent_rev"] == before["rev"]
    assert {k: v for k, v in after.items() if k not in ("panels", "rev", "parent_rev")} == {
        k: v for k, v in before.items() if k not in ("panels", "rev", "parent_rev")}


@pytest.mark.parametrize("row", range(1, 8), ids=[f"W22C2-{n:02}" for n in range(1, 8)])
def test_W22C2_geometry(builtin, g1, row):
    intake, graph = block_intake(), copy.deepcopy(g1)
    handles = ["1A", "1B", "2F"]
    centres = [[38.5, 19.25], [80.75, 38.5], [238.5, 19.25]]
    angles = [0, 90, 0]
    dimensions = [(77, 38.5)] * 3
    kernel_angles = [0, math.pi / 2, 0]
    if row == 1:
        intake = {"polylines": copy.deepcopy([P1, P2, P6])}
    elif row == 3:
        intake["polylines"] = [copy.deepcopy(P1)]
        intake["inserts"].pop(0)
    elif row in (4, 5, 7):
        intake["inserts"] = intake["inserts"][:1]
        handles = ["1A"]
        centres, angles = centres[:1], angles[:1]
        dimensions, kernel_angles = dimensions[:1], kernel_angles[:1]
        if row == 4:
            intake["blocks"]["Module"]["base"] = [10, 20, 0]
            intake["blocks"]["Module"]["children"][0]["pts"] = [
                [x + 10, y + 20] for x, y in R]
            intake["inserts"][0].update(x=5, y=7, rot=math.pi / 2, scale=[2, 0.5, 1])
            centres, angles = [[-4.625, 84]], [90]
            dimensions, kernel_angles = [(154, 19.25)], [math.pi / 2]
        elif row == 5:
            intake["inserts"][0]["scale"] = [-1, 1, 1]
            centres, angles = [[-38.5, 19.25]], [180]
            kernel_angles = [math.pi]
        else:
            graph["project"]["installation_design"] = "Ground"
            dimensions, kernel_angles = [(38.5, 77)], [math.pi / 2]
    elif row == 6:
        intake["polylines"] = [copy.deepcopy(P1)]
        intake["inserts"] = [dict(intake["inserts"][0], handle="3A", layer="Roof", name="Missing")]
        handles, centres, angles = handles[:1], centres[:1], angles[:1]
        dimensions, kernel_angles = dimensions[:1], kernel_angles[:1]
    variants = [intake]
    if row == 2:
        variants.append({k: copy.deepcopy(v) for k, v in intake.items() if k != "polylines"})
    for source in variants:
        params = {"expected_rev": 1}
        original = copy.deepcopy((graph, params, source))
        panels = kernel.panels_from_intake(
            source, installation_design=graph["project"]["installation_design"])
        assert [p["handle"] for p in panels] == handles
        for panel, centre, angle, dims in zip(panels, centres, kernel_angles, dimensions):
            assert panel["centre"] == pytest.approx(centre, abs=1e-9, rel=0)
            assert panel["angle"] == pytest.approx(angle, abs=1e-9, rel=0)
            assert (panel["column_dim"], panel["row_dim"]) == pytest.approx(dims, abs=1e-9, rel=0)
        assert panels == kernel.panels_from_intake(
            source, installation_design=graph["project"]["installation_design"])
        for insert in source.get("inserts", []):
            if "Panel".casefold() in insert["layer"].casefold():
                outline = kernel.panel_outline_from_insert(insert, source["blocks"])
                assert outline["handle"] == insert["handle"]
                assert outline["layer"] == insert["layer"] and outline["closed"] is True
                assert len(outline["pts"]) == 4
                outline["pts"][0][0] += 1
                assert (graph, params, source) == original
        after = builtin.run(graph, params, source_intake=source)
        assert_geometry(after, graph, handles, centres, angles)
        assert after == builtin.run(graph, params, source_intake=source)
        assert (graph, params, source) == original


@pytest.mark.parametrize("row", range(8, 18), ids=[f"W22C2-{n:02}" for n in range(8, 18)])
def test_W22C2_refusal(builtin, g1, row):
    intake = block_intake()
    definition = intake["blocks"]["Module"]
    child = definition["children"][0]
    insert = intake["inserts"][0]
    if row == 8:
        intake["inserts"] = {}
    elif row == 9:
        insert["name"] = "Missing"
    elif row == 10:
        definition["complete"] = False
    elif row in (11, 12):
        definition["count"] = 2
        if row == 12:
            definition["children"].append(copy.deepcopy(child))
    elif row == 13:
        child["closed"] = False
    elif row == 14:
        insert["nrm"] = [0, 1, 0]
    elif row == 15:
        insert["scale"] = [0, 1, 1]
    elif row == 16:
        insert["rot"] = float("nan")
    else:
        child["pts"] = [[0, 0], [77, 0], [70, 38.5], [0, 38.5]]
    # A valid polyline must never be published ahead of an unsupported block.
    intake["polylines"] = [dict(copy.deepcopy(P1), handle="3A")]
    original_graph = copy.deepcopy(g1)
    with refused("INVALID_SOURCE_INTAKE"):
        builtin.run(g1, {"expected_rev": 1}, source_intake=intake)
    assert g1 == original_graph
    with pytest.raises(kernel.PanelGroupKernelError):
        kernel.panels_from_intake(intake)


@pytest.mark.parametrize("row", range(18, 22), ids=[f"W22C2-{n:02}" for n in range(18, 22)])
def test_W22C2_identity_and_limit(builtin, g1, monkeypatch, row):
    intake = block_intake()
    if row == 18:
        intake["polylines"] = [copy.deepcopy(P1)]
        intake["inserts"] = [dict(intake["inserts"][0], handle="1a")]
    elif row == 19:
        intake["inserts"] = [dict(intake["inserts"][0], handle=h) for h in ("0A", "A")]
    elif row == 20:
        intake["inserts"] = [dict(intake["inserts"][0], handle="ZZ")]
    else:
        monkeypatch.setattr(builtin, "MAX_PANELS", 2)
    original = copy.deepcopy((g1, intake))
    code = {18: "AMBIGUOUS_PANEL_HANDLE", 19: "AMBIGUOUS_PANEL_HANDLE",
            20: "INVALID_PANEL_HANDLE", 21: "PANEL_LIMIT_EXCEEDED"}[row]
    with refused(code):
        builtin.run(g1, {"expected_rev": 1}, source_intake=intake)
    assert (g1, intake) == original


@pytest.mark.parametrize("row", range(22, 25), ids=[f"W22C2-{n:02}" for n in range(22, 25)])
def test_W22C2_commit(tmp_path, monkeypatch, row):
    backend = upload_blocks(tmp_path, monkeypatch, block_intake())
    seed_uploaded(backend)
    parent = stored(backend, 2)
    if row == 23:
        with refused("GRAPH_COMMIT_CANCELLED"):
            run(backend, {"drawing_id": DRAWING, "expected_rev": 1, "cancel": True})
        version_count(backend, 2)
        assert stored(backend, 2) == parent
        return
    result = run(backend)
    assert set(result) == ORDINARY_KEYS
    assert result["before_rev"] == 1 and result["after_rev"] == 2
    assert result["new_version"] == {"drawing_id": DRAWING, "version": 3, "parent": 2}
    assert result["request_sha256"] == local.request_digest(
        "solar-panels-from-drawing", DRAWING, 2, {"expected_rev": 1})
    after = stored(backend, 3)
    assert_geometry(after["solar_design_graph"], parent["solar_design_graph"],
                    ["1A", "1B", "2F"],
                    [[38.5, 19.25], [80.75, 38.5], [238.5, 19.25]], [0, 90, 0])
    assert preserved(after) == preserved(parent)
    version_count(backend, 3)
    assert proof(backend, result) == {
        "execution_mode": "local_graph_commit", "adapter": "local-graph-commit",
        "request_sha256": result["request_sha256"], "graph_sha256": result["graph_sha256"],
        "intake_sha256": result["intake_sha256"], "source_version": 2, "new_version": 3,
    }
    if row == 24:
        for definition in parent["blocks"].values():
            for child in definition["children"]:
                child["pts"] = [[x + 1, y] for x, y in child["pts"]]
        rewrite_intake(backend, 2, parent)
        assert local.resolve_graph_context(backend, TENANT, DRAWING, 2)["graph_sha256"] == result["before_graph_sha256"]
        with pytest.raises(ValueError, match="^graph commit terminal proof rejected$"):
            proof(backend, result)
