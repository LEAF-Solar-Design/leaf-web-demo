"""Offline application proof for the cat-shape panel transform litmus.

This is deliberately not a staging or APS proof. It exercises the registered
write contract, immutable drawing versions, undo, and the frozen cat oracle for
a non-demo tenant without invoking an authoring model during execution.
"""
from __future__ import annotations

import json
import time

from cat_oracle import FIXTURE_DIR, _read_pbm, evaluate_cat
from write_loop import default_backend, read_intake, run_write_mock, undo_view


TENANT = "cat-litmus-tenant"
DRAWING = "cat-panels"


def _panel(handle: str, x: float, y: float) -> dict:
    return {
        "handle": handle,
        "layer": "PANELS",
        "closed": True,
        "pts": [
            [x, y, 7.0],
            [x + 1.0, y, 7.0],
            [x + 1.0, y + 1.0, 7.0],
            [x, y + 1.0, 7.0],
        ],
        "xdata": {"panel": handle},
        "metadata": {"kind": "roof-panel"},
    }


def test_non_demo_tenant_cat_write_persists_passes_oracle_and_undoes(
    tmp_path, monkeypatch
):
    import store

    monkeypatch.setenv("LEAF_STORE_DIR", str(tmp_path / "drawings"))
    monkeypatch.setenv("LEAF_DRAWING_AUTHORITY", "filesystem")
    pixels = sorted(
        _read_pbm(FIXTURE_DIR / "sitting-v1.pbm"),
        key=lambda point: (point[1], point[0]),
    )
    # Match real AutoCAD handles. The live validator intentionally accepts
    # hexadecimal handles only, so the cat proof must use the same identity
    # contract instead of fixture-only labels.
    handles = [f"{index + 1:X}" for index in range(len(pixels))]
    before = {
        "dwg": "cat.dwg",
        "layers": ["PANELS"],
        "polylines": [
            _panel(handle, index % 64, index // 64)
            for index, handle in enumerate(handles)
        ],
    }
    transforms = []
    for panel, (target_x, target_y) in zip(before["polylines"], pixels):
        source_x, source_y = panel["pts"][0][:2]
        transforms.append({
            "handle": panel["handle"],
            "dx": target_x - source_x,
            "dy": target_y - source_y,
            "rotation_deg": 0.0,
        })

    source_path = tmp_path / "cat-panels.json"
    source_path.write_text(
        json.dumps(before, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    backend = default_backend()
    assert store.ingest_drawing(
        backend, TENANT, str(source_path), drawing_id=DRAWING
    ) == {"drawing_id": DRAWING, "version": 1}

    calls = 0

    def registered_cat_tool(_tool, source, _params, **_kwargs):
        nonlocal calls
        calls += 1
        assert source == before
        return {
            "ok": True,
            "result": {"mutations": {"transforms": transforms}},
        }

    envelope, status = run_write_mock(
        {
            "name": "arrange-panels-as-cat",
            "version": "1.0.0",
            "capabilities": ["drawing.write"],
        },
        {"drawing_id": DRAWING},
        TENANT,
        backend=backend,
        t0=time.perf_counter(),
        version=1,
        run_tool_dynamic_fn=registered_cat_tool,
    )

    assert status == 200
    assert calls == 1
    assert envelope["result"]["new_version"] == {
        "drawing_id": DRAWING,
        "version": 2,
        "parent": 1,
    }
    version, after = read_intake(backend, TENANT, DRAWING, "head")
    assert version == 2
    report = evaluate_cat(before, after, handles)
    assert report["verdict"] == "pass", report
    assert report["template"] == "sitting-v1"
    assert report["metrics"]["iou"] >= 0.985
    assert report["metrics"]["outline_chamfer_px"] <= 0.15
    assert min(report["metrics"]["region_recall"].values()) >= 0.98
    assert report["metrics"]["overlap_pixels"] == 0

    restored = undo_view(TENANT, DRAWING, backend=backend)
    assert restored["head"] == 1
    assert restored["intake"] == before
    assert read_intake(backend, TENANT, DRAWING, "head") == (1, before)
