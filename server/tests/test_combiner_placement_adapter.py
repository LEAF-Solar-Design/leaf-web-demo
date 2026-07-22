import os
from pathlib import Path

import pytest

from solver_adapters import combiner_placement

# The adapter's DEFAULT_SOLVER_ROOT assumes leaf-web-demo and
# aws-combiner-placement are sibling checkouts under the same parent folder
# (same convention as autofill.py's DEFAULT_SOLVER_ROOT).  This test worktree
# was created outside that layout, so the real solver checkout is located
# explicitly here rather than relying on the sibling-directory default.
SOLVER_ROOT = Path(os.environ.get(
    "COMBINER_PLACEMENT_SOLVER_ROOT",
    r"C:\Users\ehaug\OneDrive\Documents\GitHub\aws-combiner-placement",
))

SMOKE_INPUT = {"dump": {"inputs": {}}, "options": {}}

pytestmark = pytest.mark.skipif(
    not (SOLVER_ROOT / "sim" / "simulate-row-end-optimizer.js").is_file(),
    reason="aws-combiner-placement checkout is unavailable; adapter needs the real solver source",
)


def test_real_combiner_placement_solver_smoke_is_deterministic():
    first = combiner_placement.run(SMOKE_INPUT, solver_root=SOLVER_ROOT)
    second = combiner_placement.run(SMOKE_INPUT, solver_root=SOLVER_ROOT)
    # generatedUtc is a wall-clock timestamp stamped by the solver on every
    # run, so the full solver_result is not byte-stable; the deterministic
    # business payload is solver_result["solution"].
    assert first["solver_result"]["solution"] == second["solver_result"]["solution"]
    assert first["result_sha256"] == second["result_sha256"]
    assert first["solver_result"]["solution"] == {
        "combiners": [], "l1ToL2Assignments": {}, "warnings": [],
    }
    assert first["result_sha256"] == "9705c47e171485e12f75823cdc575ac53a5942fdaa4e32aa9749fb6968dfd282"
    assert first["request_sha256"]
    assert len(first["source_sha256"]) == 64
    assert first["solver_input"]["dump"] == {"inputs": {}}
    assert first["solver_revision"]
    assert first["runtime"].startswith("node-")


def test_adapter_rejects_invalid_dump_and_options():
    invalid_cases = (
        {},
        {"dump": []},
        {"dump": {}},
        {"dump": {"inputs": []}},
        {"dump": {"inputs": {}}, "options": []},
        {"dump": {"inputs": {}}, "options": {"stringsPerInput": True}},
        {"dump": {"inputs": {}}, "options": {"stringsPerInput": 0}},
        {"dump": {"inputs": {}}, "options": {"dcInputsPerL2": 0}},
        {"dump": {"inputs": {}}, "options": {"placementStrategy": "bogus"}},
        {"dump": {"inputs": {}}, "options": {"locationMode": "bogus"}},
    )
    for invalid in invalid_cases:
        with pytest.raises(ValueError):
            combiner_placement.run(invalid, solver_root=SOLVER_ROOT)


def test_adapter_honors_option_overrides():
    result = combiner_placement.run(
        {"dump": {"inputs": {}},
         "options": {"stringsPerInput": 8, "dcInputsPerL2": 24,
                     "placementStrategy": "row-end", "locationMode": "chunk"}},
        solver_root=SOLVER_ROOT,
    )
    assert result["solver_result"]["commandContext"]["stringsPerDcInput"] == 8
    assert result["solver_result"]["commandContext"]["dcInputsPerL2"] == 24
    assert result["solver_result"]["commandContext"]["placementStrategy"] == "row-end"
    assert result["solver_result"]["commandContext"]["locationMode"] == "chunk"
