"""The browser and server execute the same operator vectors."""

import json
from pathlib import Path
import re

import pytest

from lab_ops import (
    MAX_ROWS, OPERATOR_IDS, STREAM_ADAPTERS, OperatorRefused,
    OperatorUnavailable, has_operator, run_operator,
)

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = json.loads((Path(__file__).parent / "fixtures/lab-operator-cases.v1.json").read_text(encoding="utf-8"))
CASES = FIXTURE["cases"]


def assert_json_equal(actual, expected):
    # Python's True == 1 would otherwise hide a cross-language regression.
    if isinstance(expected, bool) or expected is None:
        assert actual is expected
    elif isinstance(expected, (int, float)):
        assert isinstance(actual, (int, float)) and not isinstance(actual, bool)
        assert actual == expected
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
    if "error" in case:
        error_class = {"refused": OperatorRefused, "unavailable": OperatorUnavailable}[case["error"]]
        with pytest.raises(error_class):
            run_operator(case["op"], case["input"], case["settings"])
    else:
        assert_json_equal(run_operator(case["op"], case["input"], case["settings"]), case["expect"])


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
        assert ("expect" in case) != ("error" in case)
        assert case.get("error", "refused") in ("refused", "unavailable")
