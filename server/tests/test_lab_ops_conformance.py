"""The browser and server execute the same operator vectors."""

import json
import math
from copy import deepcopy
from pathlib import Path
import re

import pytest

from lab_ops import (
    MAX_ROWS, OPERATOR_IDS, STREAM_ADAPTERS, UNDEFINED, OperatorRefused,
    OperatorUnavailable, has_operator, run_operator,
)

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = json.loads((Path(__file__).parent / "fixtures/lab-operator-cases.v1.json").read_text(encoding="utf-8"))
CASES = FIXTURE["cases"]


def case_arguments(case):
    input, settings = deepcopy((case["input"], case["settings"]))
    if "non_json" in case:
        # JSON cannot carry these values. Map denotes non-string keys in JS.
        cycle = []
        cycle.append(cycle)
        values = {
            "undefined": UNDEFINED, "nan": float("nan"),
            "infinity": float("inf"), "negative-infinity": -float("inf"),
            "non-string-key": {1: None}, "unsupported": object(), "cycle": cycle,
        }
        value = values[case["non_json"]["kind"]]
        at = case["non_json"]["at"]
        if at == "selected":
            input["value"][0]["x"] = value
        elif at == "target":
            settings["value"] = value
        elif at in ("input-value", "auxiliary"):
            input["value" if at == "input-value" else at] = value
        else:
            assert at in ("count", "direction", "field", "comparison")
            settings[at] = value
    return input, settings


def assert_json_equal(actual, expected):
    # Python's True == 1 would otherwise hide a cross-language regression.
    if isinstance(expected, bool) or expected is None:
        assert actual is expected
    elif isinstance(expected, (int, float)):
        assert isinstance(actual, (int, float)) and not isinstance(actual, bool)
        assert actual == expected
        if expected == 0:
            assert math.copysign(1.0, actual) == math.copysign(1.0, expected)
    elif isinstance(expected, list):
        assert isinstance(actual, list) and len(actual) == len(expected)
        for left, right in zip(actual, expected):
            assert_json_equal(left, right)
    elif isinstance(expected, dict):
        assert isinstance(actual, dict) and actual.keys() == expected.keys()
        for key in expected:
            assert_json_equal(actual[key], expected[key])
    else:
        assert type(actual) is type(expected) and actual == expected


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["name"])
def test_shared_operator_case(case):
    input, settings = case_arguments(case)
    if "error" in case:
        error_class = {"refused": OperatorRefused, "unavailable": OperatorUnavailable}[case["error"]]
        with pytest.raises(error_class):
            run_operator(case["op"], input, settings)
    elif case.get("expect_undefined"):
        assert run_operator(case["op"], input, settings) is UNDEFINED
    else:
        assert_json_equal(run_operator(case["op"], input, settings), case["expect"])


@pytest.mark.parametrize("comparison", ["eq", "ne"])
@pytest.mark.parametrize("value", [b"bytes", (1,), {1}, {None: 1}, {"nested": [UNDEFINED]}])
@pytest.mark.parametrize("at", ["selected", "target"])
def test_python_non_json_values_are_refused(comparison, value, at):
    selected, target = (value, None) if at == "selected" else (None, value)
    with pytest.raises(OperatorRefused):
        run_operator("filter", {"value": [{"x": selected}]},
                     {"field": "x", "comparison": comparison, "value": target})


@pytest.mark.parametrize("op", ["take", "collect", "window"])
@pytest.mark.parametrize("settings", [{}, {"count": UNDEFINED}])
def test_undefined_count_uses_max_rows(op, settings):
    values = list(range(MAX_ROWS + 1))
    expected = values[-MAX_ROWS:] if op == "window" else values[:MAX_ROWS]
    assert run_operator(op, {"value": values}, settings) == expected


def test_undefined_results_and_null_are_distinct():
    assert repr(UNDEFINED) == "UNDEFINED"
    assert run_operator("field", {"value": {"x": UNDEFINED}}, {"field": "x"}) is UNDEFINED
    assert run_operator("select", {"value": [{"x": UNDEFINED}]}, {"field": "x"})[0] is UNDEFINED
    assert run_operator("field", {}, {"field": "x"}) is None
    assert run_operator("latest", {"value": [UNDEFINED]}) is None


def test_browser_operator_exports_and_fixture_contract():
    source = (ROOT / "web/src/lab/runtime/ops.js").read_text(encoding="utf-8")
    operators = source.split("const operators = Object.freeze({", 1)[1].split("\n});", 1)[0]
    assert list(OPERATOR_IDS) == re.findall(r"^  (\w+):", operators, re.MULTILINE)
    adapters = re.search(r"export const STREAM_ADAPTERS = Object.freeze\(\[(.*?)\]\)", source).group(1)
    assert list(STREAM_ADAPTERS) == re.findall(r"'([^']+)'", adapters)
    assert MAX_ROWS == int(re.search(r"export const MAX_ROWS = (\d+);", source).group(1))
    assert all(has_operator(op) for op in OPERATOR_IDS)
    assert not has_operator("constructor")
    assert not has_operator("missing")
    assert FIXTURE["schema"] == "leaf.lab-operator-cases.v1"
    assert len(CASES) >= 60
    assert len({case["name"] for case in CASES}) == len(CASES)
    assert set(OPERATOR_IDS) <= {case["op"] for case in CASES}
    for case in CASES:
        assert sum(key in case for key in ("expect", "expect_undefined", "error")) == 1
        if "expect_undefined" in case:
            assert case["expect_undefined"] is True
        assert case.get("error", "refused") in ("refused", "unavailable")
