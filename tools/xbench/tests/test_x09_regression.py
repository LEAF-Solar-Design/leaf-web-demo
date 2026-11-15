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


def fixtures(n=63, m=10):
    return ([{"name": f"tool-{i:03d}", "version": "1", "catalog_digest": f"sha256:{i:064x}"}
             for i in range(n)], [{"sha256": f"{i:064x}"} for i in range(m)])


def test_weekly_capacity_and_order():
    tools, drawings = fixtures()
    week = [plan_pairs(tools, drawings, day) for day in range(7)]
    assert all(len(day) == 9 for day in week)
    assert len({pair["name"] for day in week for pair in day}) == 63
    assert sum(map(len, week)) == 63
    assert plan_pairs(tools[::-1], drawings[::-1], 2) == week[2]
    with pytest.raises(ValueError, match="weekly"):
        plan_pairs(*fixtures(141, 1), 0)


def test_every_tool_visits_every_drawing_over_ten_weeks():
    tools, drawings = fixtures()
    visits = {tool["name"]: set() for tool in tools}
    for day in range(70):
        for pair in plan_pairs(tools, drawings, day):
            visits[pair["name"]].add(pair["drawing_sha256"])
    assert all(seen == {drawing["sha256"] for drawing in drawings} for seen in visits.values())
    # The full 140-tool weekly capacity still fits 20 pairs on each night.
    assert all(len(plan_pairs(*fixtures(140, 10), day)) == 20 for day in range(7))


def test_catalog_digest_required_and_optional_package_preserved():
    tools, drawings = fixtures(1, 1)
    tools[0]["package_digest"] = "b" * 64
    pair, = plan_pairs(tools, drawings, 0)
    assert pair["catalog_digest"] == tools[0]["catalog_digest"]
    assert pair["package_digest"] == "b" * 64
    tools[0]["catalog_digest"] = "invalid"
    with pytest.raises(ValueError, match="catalog"):
        plan_pairs(tools, drawings, 0)


def test_small_set_no_duplicate_per_night():
    tools, drawings = fixtures(1, 3)
    assert sum(len(plan_pairs(tools, drawings, day)) for day in range(7)) == 1
    assert plan_pairs(tools, drawings, 0)[0]["drawing_sha256"] != plan_pairs(tools, drawings, 7)[0]["drawing_sha256"]


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
