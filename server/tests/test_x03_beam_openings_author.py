"""Net installed steel through the ordinary DXF intake, author and sandbox."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import dxf_intake
import intake_dxf
import tool_loader
import tools_fallback


ASKS = [
    "Give me the installed steel length by level after deducting the openings marked on each beam.",
    "What net beam length remains on each floor once the marked openings are removed?",
    "Total the steel beam lengths per storey, subtracting the cutouts along each member.",
]


def drawing(scale, marks, extra=()):
    # The first centerline bends 5 + 7 units; the third rises along a 5-12-13 slope.
    lines = [
        ("A1", "BEAM-L1", [(0, 0, 0), (3, 4, 0), (10, 4, 0)]),
        ("A2", "BEAM-L1", [(0, 8, 0), (9, 8, 0)]),
        ("B1", "BEAM-Roof", [(0, 0, 4), (3, 4, 16)]),
        ("B2", "BEAM-Roof", [(3, 1, 4), (3, 7, 4)]),
        ("C1", "Walls", [(0, 0, 0), (90, 0, 0)]),
    ]
    paths = [dict(handle=h, layer=layer, closed=False,
                  pts=[[scale*v for v in p] for p in pts]) for h, layer, pts in lines]
    paths.append(dict(paths[0], handle="F1", space="paper"))
    texts = [dict(layer="Openings", pt=[0, 0], kind="MTEXT" if i % 2 else "TEXT",
                  text=f"OPENING {handle} {low*scale} {high*scale}")
             for i, (handle, low, high) in enumerate(marks)]
    texts.extend(dict(layer="Openings", pt=[0, 0], text=text) for text in extra)
    texts.append(dict(layer="Openings", pt=[0, 0], text="OPENING F1 1 999", space="paper"))
    data = dict(layers=sorted({p["layer"] for p in paths} | {"Openings"}),
                polylines=paths, texts=texts)
    return dxf_intake.parse_dxf_bytes(intake_dxf.intake_to_dxf(data))


def invoke(ask, intake):
    _, source, _ = tools_fallback.author_tool(ask)
    return tool_loader._run_source_in_sandbox(source, "installed_steel.py", intake, {})


@pytest.mark.parametrize("ask,scale,marks", [
    (ASKS[0], 1, [("A1", 5, 9), ("A2", 2, 5), ("A1", 2, 7), ("B1", 0, 3)]),
    (ASKS[1], 2.5, [("A1", 0, 5), ("A1", 4, 5), ("A1", 9, 12), ("B1", 0, 13)]),
    (ASKS[2], 0.5, [("B1", 1, 4), ("B1", 1, 4), ("B1", 4, 6), ("A2", 0, 9)]),
])
def test_installed_steel_by_level(ask, scale, marks):
    status, payload = invoke(ask, drawing(scale, marks))
    assert status == "ok", payload
    result, _ = payload
    totals = {"L1": 0, "Roof": 0}
    for handle, level, length in [("A1", "L1", 12), ("A2", "L1", 9),
                                  ("B1", "Roof", 13), ("B2", "Roof", 6)]:
        # Count covered unit stations independently of the production interval union.
        covered = {i for h, lo, hi in marks if h == handle for i in range(lo, hi)}
        net = (length - len(covered)) * scale
        totals[level] += net
        assert result.get("beams", {}).get(handle) == {
            "level": level, "gross_length": length * scale,
            "opening_length": len(covered) * scale, "installed_length": net,
        }, result
    assert result["installed_length_by_level"] == totals
    assert result["units"] == "drawing_units"


@pytest.mark.parametrize("mark", ["OPENING A1 -1 2", "OPENING A1 5 12.1", "OPENING A1 7 3",
                                  "OPENING A1 4 4", "OPENING A1 0 nan", "OPENING A1 3",
                                  "OPENING DC 0 1"])
def test_invalid_opening_fails_the_takeoff(mark):
    status, payload = invoke(ASKS[0], drawing(1, [], [mark]))
    assert status == "tool_error", payload
    assert "opening" in str(payload).lower()
