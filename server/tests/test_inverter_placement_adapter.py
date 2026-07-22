from pathlib import Path

from solver_adapters import inverter_placement

# The adapter's default solver root follows the autofill.py sibling-repo
# convention (server/solver_adapters/../../../<solver-repo>), which resolves
# correctly in the real leaf-web-demo checkout but not in this rescue
# worktree under C:\tmp.  Point at the real aws-inverter-placement checkout
# explicitly rather than relying on the default or an ambient env var, so
# this test runs solo with no environment setup beyond PYTHONPATH.
SOLVER_ROOT = Path(
    r"C:\Users\ehaug\OneDrive\Documents\GitHub\aws-inverter-placement"
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


def test_real_minimax_solver_reports_infeasible_details_instead_of_raising():
    params = _fixed_centroid_input(capacity=1)
    result = inverter_placement.run(params, solver_root=SOLVER_ROOT)
    assert result["feasible"] is False
    assert result["solver_result"] is None
    assert result["infeasible_details"]["reason"] == "insufficient_capacity"
    assert result["infeasible_details"]["total_capacity"] == 1


def test_adapter_rejects_invalid_input():
    invalid_inputs = (
        {},
        {"groups": [{}], "stringsPerInverter": 2},
        {"groups": [{"strings": []}], "stringsPerInverter": 2},
        {"groups": [{"strings": [{}]}], "stringsPerInverter": True},
        {"groups": [{"strings": [{}]}], "stringsPerInverter": 0},
        {"groups": [{"strings": [{}]}], "stringsPerInverter": 1, "algorithm": "bogus"},
        {"groups": [{"strings": [{}]}], "stringsPerInverter": 1, "fixedCentroids": "nope"},
    )
    for invalid in invalid_inputs:
        try:
            inverter_placement.run(invalid, solver_root=SOLVER_ROOT)
        except ValueError:
            pass
        else:
            raise AssertionError(f"invalid adapter input was accepted: {invalid!r}")
