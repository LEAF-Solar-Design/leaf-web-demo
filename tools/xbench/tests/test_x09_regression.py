"""Synthetic checks of X09 offline logic, not live acceptance evidence."""

import hashlib
import json

import pytest

from benches.x09_regression import compare_pair, plan_pairs


@pytest.mark.parametrize("oracle,output,verdict", [
    ({}, {}, "UNREADABLE"),
    ({"rule": "unknown", "value": 1}, 1, "UNREADABLE"),
    ({"rule": "count", "value": 0}, 0, "PASS"),
    ({"rule": "count", "value": 1}, 0, "FAIL"),
    ({"rule": "count", "value": True}, 1, "UNREADABLE"),
    ({"rule": "count", "value": 1}, True, "UNREADABLE"),
    ({"rule": "count", "value": -1}, -1, "UNREADABLE"),
    ({"rule": "exact-values", "value": []}, [], "PASS"),
    ({"rule": "exact-values", "value": {"a": 1}}, {"a": True}, "FAIL"),
    ({"rule": "exact-values", "value": 1}, 1.0, "FAIL"),
    ({"rule": "exact-values", "value": float("nan")}, 0, "UNREADABLE"),
    ({"rule": "exact-values", "value": {}}, {1: "a"}, "UNREADABLE"),
    ({"rule": "sha256", "value": "invalid"}, {}, "UNREADABLE"),
])
def test_explicit_assertion(oracle, output, verdict):
    assert compare_pair(oracle, output)["verdict"] == verdict


def test_canonical_hash():
    expected = hashlib.sha256(json.dumps({"a": [], "z": "ø"}, sort_keys=True,
                                          separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
    oracle = {"rule": "sha256", "value": expected}
    assert compare_pair(oracle, {"z": "ø", "a": []}) == {
        "verdict": "PASS", "assertion": "sha256", "reason": "golden compare"}
    assert compare_pair(oracle, {})["verdict"] == "FAIL"
    assert compare_pair(oracle, float("inf"))["verdict"] == "UNREADABLE"


def fixtures(n=14, m=10):
    return ([{"name": str(i), "version": "1", "package_sha256": f"{i:064x}"}
             for i in range(n)], [{"sha256": f"{i:064x}"} for i in range(m)])


def test_weekly_capacity_and_order():
    tools, drawings = fixtures()
    week = [plan_pairs(tools, drawings, day) for day in range(7)]
    assert all(len(day) == 20 for day in week)
    assert len({json.dumps(pair, sort_keys=True) for day in week for pair in day}) == 140
    assert plan_pairs(tools[::-1], drawings[::-1], 2) == week[2]
    with pytest.raises(ValueError, match="weekly"):
        plan_pairs(*fixtures(141, 1), 0)


def test_small_set_no_duplicate_per_night():
    tools, drawings = fixtures(1, 3)
    assert len(plan_pairs(tools, drawings, 99)) == 3


@pytest.mark.parametrize("day,cap", [(-1, 20), (True, 20), (0, 21), (0, 0), (0, True)])
def test_bad_schedule(day, cap):
    with pytest.raises(ValueError):
        plan_pairs(*fixtures(1, 1), day, cap)


def test_bad_identities():
    tools, drawings = fixtures(1, 1)
    for t, d in [([], drawings), (tools * 2, drawings), (tools, drawings * 2),
                 ([{"name": "x"}], drawings), (tools, [{"sha256": "bad"}])]:
        with pytest.raises(ValueError):
            plan_pairs(t, d, 0)
