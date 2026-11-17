"""Exercise the arithmetic seam without allocating millions of parents."""
from copy import deepcopy
import json
from pathlib import Path
import pytest
import tool_loader
from test_x04_minsert_arrays import TOOL, selected


def module():
    return tool_loader._load_module(Path(__file__).resolve().parents[1] / "builtins" / "count_modules.py")


@pytest.mark.parametrize("delta", [-1, 0, 1, 2])
def test_weighted_total_boundary_is_exact_or_refused(delta):
    tool = module()
    maximum = 2**53 - 1
    # Valid maximum array weights plus a residual pin the real arithmetic limit.
    weight = 65535**2
    parents, residual = divmod(maximum + delta, weight)
    weights = (weight for _ in range(parents))
    from itertools import chain
    if delta > 0:
        with pytest.raises(ValueError, match="safe integer"):
            tool._module_total(chain(weights, [residual]))
    else:
        total = tool._module_total(chain(weights, [residual]))
        metadata = {"count": total, "sets": [{"set_id": "synthetic", "count": total}]}
        restored = json.loads(json.dumps(metadata, allow_nan=False))
        assert restored == metadata and int(float(restored["sets"][0]["count"])) == total


@pytest.mark.parametrize("same_set", [False, True])
def test_loader_refuses_group_or_union_overflow_without_partial_metadata(monkeypatch, same_set):
    # Lower only the arithmetic ceiling for a small normal-loader integration case.
    monkeypatch.setattr(module(), "MAX_EXACT_COUNT", 10)
    intake = {"inserts": [{"handle": "A", "name": "Panel", "layer": "East", "rows": 2, "columns": 3},
                           {"handle": "B", "name": "Panel", "layer": "East" if same_set else "West", "rows": 2, "columns": 3}]}
    original = deepcopy(intake)
    outcome = selected(intake)
    assert not outcome["ok"] and "safe integer" in outcome["error"]["message"]
    assert not outcome.get("result") and not outcome.get("overlay")
    assert intake == original
