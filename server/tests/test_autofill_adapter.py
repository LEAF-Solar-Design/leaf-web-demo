import os
from pathlib import Path

import pytest

from solver_adapters import autofill


# The adapter's default assumes leaf-web-demo and autofill-solver are sibling
# checkouts. AUTOFILL_SOLVER_ROOT overrides for isolated worktrees and must be
# valid when set. The smoke is REQUIRED by default: an unresolvable solver
# source fails the suite so a resolution bug can never skip-launder past the
# gate floor. Hosts that genuinely lack the source opt out explicitly with
# LEAF_AUTOFILL_SOLVER_ABSENT_OK=1, which produces a visible skip.
def _resolve_solver_root():
    env_root = os.environ.get("AUTOFILL_SOLVER_ROOT")
    if env_root:
        root = Path(env_root)
        if not (root / "solver.py").is_file():
            return None, f"AUTOFILL_SOLVER_ROOT is set but invalid: {env_root!r}"
        return root, None
    sibling = Path(__file__).resolve().parents[3] / "autofill-solver"
    if (sibling / "solver.py").is_file():
        return sibling, None
    return None, None


SOLVER_ROOT, _RESOLUTION_ERROR = _resolve_solver_root()
_ABSENT_OK = os.environ.get("LEAF_AUTOFILL_SOLVER_ABSENT_OK") == "1"


def test_solver_source_resolution_policy():
    if _RESOLUTION_ERROR:
        pytest.fail(_RESOLUTION_ERROR)
    if SOLVER_ROOT is None and not _ABSENT_OK:
        pytest.fail(
            "autofill-solver source not found (no sibling checkout, no "
            "AUTOFILL_SOLVER_ROOT). Set AUTOFILL_SOLVER_ROOT, or acknowledge "
            "a source-less host explicitly with LEAF_AUTOFILL_SOLVER_ABSENT_OK=1.")


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
    reason="autofill-solver source absent, acknowledged via LEAF_AUTOFILL_SOLVER_ABSENT_OK=1",
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


def test_adapter_forwards_geometry_to_pinned_solver_contract(tmp_path):
    (tmp_path / "solver.py").write_text(
        "def solve_targets(groups, n, options=None, panel_angle=0.0, "
        "panel_width=0.0, panel_height=0.0):\n"
        "    return {'angle': panel_angle, 'width': panel_width, "
        "'height': panel_height}\n",
        encoding="utf-8",
    )

    body = {**SMOKE_INPUT, "panelAngle": 0.5, "panelWidth": 1.25,
            "panelHeight": 2.5}
    result = autofill.run(body, solver_root=tmp_path)

    assert result["solver_result"] == {
        "angle": 0.5,
        "width": 1.25,
        "height": 2.5,
    }
