import os
from pathlib import Path

import pytest

from solver_adapters import autofill


# The adapter's default assumes leaf-web-demo and autofill-solver are sibling
# checkouts. Support an explicit checkout for isolated worktrees, then skip the
# external smoke when the real solver source is not available.
_CANDIDATES = [
    os.environ.get("AUTOFILL_SOLVER_ROOT"),
    str(Path(__file__).resolve().parents[3] / "autofill-solver"),
    r"C:\Users\ehaug\OneDrive\Documents\GitHub\autofill-solver",
]
SOLVER_ROOT = next((Path(candidate) for candidate in _CANDIDATES
                    if candidate and (Path(candidate) / "solver.py").is_file()), None)


SMOKE_INPUT = {
    "groups": [
        {"handle": "A", "name": "A", "count": 25, "centroidX": 0.0,
         "centroidY": 0.0, "electricalZone": "Z", "elevationZone": ""},
        {"handle": "B", "name": "B", "count": 15, "centroidX": 10.0,
         "centroidY": 0.0, "electricalZone": "Z", "elevationZone": ""},
    ],
    "panelsPerString": 10,
    "options": {"drainThreshold": 23, "drainDiscount": 0.0,
                "activeGroupPenalty": 10, "concentrationBias": 0.15,
                "clusterMarginPitches": 2.0},
}


@pytest.mark.skipif(
    SOLVER_ROOT is None,
    reason="autofill-solver checkout is unavailable; adapter needs the real solver source",
)
def test_real_autofill_solver_smoke_is_deterministic():
    first = autofill.run(SMOKE_INPUT, solver_root=SOLVER_ROOT)
    second = autofill.run(SMOKE_INPUT, solver_root=SOLVER_ROOT)
    assert first == second
    assert first["solver_result"] == {
        "clusters": [["A", "B"]], "diagnostics": [], "feasible": True,
        "groupTargets": {"A": 40, "B": 0}, "totalDisruption": 30,
    }
    assert first["result_sha256"] == "525e2d417d916ab896ab25525352783302c98f6f436631777731a4c08bb1ed59"
    assert first["request_sha256"]
    assert len(first["source_sha256"]) == 64
    assert first["solver_input"]["panelsPerString"] == 10
    assert first["solver_revision"]
    assert first["runtime"].startswith("python-")


def test_adapter_rejects_non_json_and_invalid_panel_count():
    for invalid in ({}, {"groups": [{}], "panelsPerString": True},
                    {"groups": [{}], "panelsPerString": 2},
                    {"groups": [{}], "panelsPerString": 10, "options": []}):
        try:
            autofill.run(invalid)
        except ValueError:
            pass
        else:
            raise AssertionError(f"invalid adapter input was accepted: {invalid!r}")
