"""Regression tests for the offline Studio bounds candidate."""
import hashlib
import json
from pathlib import Path

from dxf_intake import parse_dxf_file
from tool_loader import resolve_local_file, run_tool_dynamic


REPO = Path(__file__).resolve().parents[2]
CANDIDATE = REPO / "tools/xbench/candidates/x13/layer-bounding-boxes/1.0.1"
MODEL_LAYER = "Struc_Plan_GB_PL"


def _run(monkeypatch, root, tool, intake, params):
    monkeypatch.setenv("LEAF_TENANT_REPO", str(root))
    monkeypatch.setenv("LEAF_SANDBOX", "off")
    assert resolve_local_file(tool) == root / tool["entry"]
    response = run_tool_dynamic(tool, intake, params, aps_live=False)
    assert response["ok"], response["error"]
    assert response["version"] == tool["version"]
    return response["result"]


def _candidate_tool():
    return json.loads(
        (CANDIDATE / "tools/layer-bounding-boxes/tool.json").read_text(encoding="utf-8")
    )


def test_s04_model_space_keeps_shared_layer_bounds(monkeypatch, tmp_path):
    intake = parse_dxf_file(CANDIDATE / "fixtures/s04-model-paper.dxf")
    raw = (REPO / "data/nl_author_receipt.json").read_bytes()
    assert hashlib.sha256(raw).hexdigest() == (
        "1a931347cfc5cddc2b91d2c656e7503c78be35b2a6a284f32e9b5243ea6160db"
    )
    archive = json.loads(raw)
    original = tmp_path / archive["tool"]["entry"]
    original.parent.mkdir(parents=True)
    original.write_bytes(archive["entry_code"].encode("utf-8"))

    archived = _run(monkeypatch, tmp_path, archive["tool"], intake, {})
    assert [row["layer"] for row in archived["layers"]] == [
        MODEL_LAYER, "PS_ANNOT", "MODEL_AUX"
    ]
    assert archived["layers"][0]["min_x"] == -100
    assert archived["layers"][0]["geometry_count"] == 2

    tool = _candidate_tool()
    assert tool["version"] == "1.0.1"
    result = _run(monkeypatch, CANDIDATE, tool, intake, {})

    assert result["layer_count"] == 2
    assert [row["layer"] for row in result["layers"]] == [MODEL_LAYER, "MODEL_AUX"]
    assert result["layers"][0] == {
        "layer": MODEL_LAYER, "min_x": 0, "max_x": 4, "min_y": 0, "max_y": 3,
        "width": 4, "height": 3, "area": 12, "geometry_count": 1,
    }
    assert result["layers"][1] == archived["layers"][2]


def test_s04_paper_only_filter_returns_empty_success(monkeypatch):
    intake = parse_dxf_file(CANDIDATE / "fixtures/s04-model-paper.dxf")
    result = _run(monkeypatch, CANDIDATE, _candidate_tool(), intake, {"layers": ["PS_ANNOT"]})
    assert result == {"layers": [], "layer_count": 0}


def test_legacy_faces_and_explicit_model_faces_keep_their_bounds(monkeypatch):
    intake = {
        "faces3d": [
            {"layer": "Panels", "pts": [[0, 0, 1], [3, 2, 1]]},
            {"layer": "Panels", "pts": [[-1, -2, 0], [1, 1, 0]], "space": "model"},
            {"layer": "Panels", "pts": [[-100, -100, 0], [100, 100, 0]], "space": "paper"},
        ]
    }
    result = _run(monkeypatch, CANDIDATE, _candidate_tool(), intake, {"layers": ["Panels"]})
    assert result == {
        "layer_count": 1,
        "layers": [{
            "layer": "Panels", "min_x": -1, "max_x": 3, "min_y": -2, "max_y": 2,
            "width": 4, "height": 4, "area": 16, "geometry_count": 2,
        }],
    }
