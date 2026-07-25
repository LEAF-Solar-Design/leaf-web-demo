import os
from pathlib import Path

import pytest

from solver_adapters import inverter_placement

# Portable root: env override, then the autofill.py sibling-repo convention.
# Only real-solver integrations skip when the source or dependencies are absent;
# offline input-validation coverage remains mandatory.
_CANDIDATES = [
    os.environ.get("INVERTER_SOLVER_ROOT"),
    str(Path(__file__).resolve().parents[3] / "aws-inverter-placement"),
]
SOLVER_ROOT = next((Path(c) for c in _CANDIDATES
                    if c and (Path(c) / "minimax_optimizer.py").is_file()), None)


def _solver_deps_importable():
    try:
        import numpy  # noqa: F401
        return True
    except Exception:
        return False


requires_real_solver = pytest.mark.skipif(
    SOLVER_ROOT is None or not _solver_deps_importable(),
    reason="aws-inverter-placement source or its runtime deps (numpy) unavailable",
)

# Known-good fixture, matches aws-inverter-placement/tests/test_inverter_placement.py
# ``fixed_centroid_input`` (capacity=2 -> feasible, capacity=1 -> infeasible).
FIXED_CENTROID_GROUPS = [
    {
        "name": "Group A",
        "handle": "group-a",
        "strings": [
            {"handle": "string-1", "startPoint": {"coordinate": "0,0"},
             "endPoint": {"coordinate": "1,0"}},
            {"handle": "string-2", "startPoint": {"coordinate": "10,0"},
             "endPoint": {"coordinate": "11,0"}},
        ],
    }
]


def _fixed_centroid_input(capacity):
    return {
        "algorithm": "minimax",
        "groups": FIXED_CENTROID_GROUPS,
        "stringsPerInverter": capacity,
        "iterations": 5,
        "fixedCentroids": [{"position": {"x": 5, "y": 0}, "capacity": capacity}],
    }


@requires_real_solver
def test_real_minimax_solver_smoke_is_deterministic():
    params = _fixed_centroid_input(capacity=2)
    first = inverter_placement.run(params, solver_root=SOLVER_ROOT)
    second = inverter_placement.run(params, solver_root=SOLVER_ROOT)
    assert first == second
    assert first["feasible"] is True
    assert first["algorithm"] == "minimax"
    assert first["solver_result"]["num_inverters"] == 1
    assert first["solver_result"]["total_distance"] == 20.0
    assert first["solver_result"]["max_distance"] == 11.0
    assert first["solver_result"]["points"][0]["assigned_pairs"] == 2
    assert first["infeasible_details"] is None
    assert first["request_sha256"]
    assert len(first["source_sha256"]) == 64
    assert first["solver_input"]["stringsPerInverter"] == 2
    assert first["solver_revision"]
    assert first["runtime"].startswith("python-")


@requires_real_solver
def test_real_minimax_solver_reports_infeasible_details_instead_of_raising():
    params = _fixed_centroid_input(capacity=1)
    result = inverter_placement.run(params, solver_root=SOLVER_ROOT)
    assert result["feasible"] is False
    assert result["solver_result"] is None
    assert result["infeasible_details"]["reason"] == "insufficient_capacity"
    assert result["infeasible_details"]["total_capacity"] == 1


def test_adapter_rejects_invalid_input():
    good_string = {"handle": "s", "startPoint": {"coordinate": "0,0"},
                   "endPoint": {"coordinate": "1,0"}}
    invalid_inputs = (
        {},
        {"groups": [{}], "stringsPerInverter": 2},
        {"groups": [{"strings": []}], "stringsPerInverter": 2},
        {"groups": [{"strings": [good_string]}], "stringsPerInverter": True},
        {"groups": [{"strings": [good_string]}], "stringsPerInverter": 0},
        {"groups": [{"strings": [good_string]}], "stringsPerInverter": 1, "algorithm": "bogus"},
        {"groups": [{"strings": [good_string]}], "stringsPerInverter": 1, "fixedCentroids": "nope"},
        # legacy is rejected (it reports infeasible-as-success; not exposed)
        {"groups": [{"strings": [good_string]}], "stringsPerInverter": 1, "algorithm": "legacy"},
        # unknown top-level field is rejected (fail closed)
        {"groups": [{"strings": [good_string]}], "stringsPerInverter": 1, "spurious": 1},
        # a string missing its endpoints is rejected before the solver crashes
        {"groups": [{"strings": [{"handle": "x"}]}], "stringsPerInverter": 1},
        {"groups": [{"strings": [{"startPoint": {"coordinate": ""},
                                  "endPoint": {"coordinate": "1,0"}}]}], "stringsPerInverter": 1},
    )
    for invalid in invalid_inputs:
        try:
            inverter_placement.run(invalid, solver_root=SOLVER_ROOT)
        except ValueError:
            pass
        else:
            raise AssertionError(f"invalid adapter input was accepted: {invalid!r}")
