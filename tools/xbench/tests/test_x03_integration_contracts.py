"""Offline response oracles for authoring asks involving external services."""
from collections import Counter
import pytest


@pytest.mark.parametrize("part,unit,version", [("panel", "W", "v1"), ("beam", "mm", "v4"), ("fan", "rpm", "v2")])
def test_catalog_rating_bound_to_requested_identity(part, unit, version):
    expected = (part, unit, version, 125)
    def matches(response):
        return tuple(response.get(k) for k in ("part", "unit", "version", "rating")) == expected
    receipt = dict(zip(("part", "unit", "version", "rating"), expected))
    assert matches(receipt)
    assert not matches(dict(receipt, unit="unknown"))
    assert not matches(dict(receipt, version="retired"))


@pytest.mark.parametrize("revisions", [("r1","r2"), ("draft","issued"), ("007","008")])
def test_revision_deliveries_reconcile_to_receiver_log(revisions):
    keys = [("drawing", rev) for rev in revisions]
    def reconciles(receiver):
        return Counter(receiver) == Counter(keys)
    assert reconciles(keys)
    assert not reconciles(keys + keys[:1])
    assert not reconciles(keys[1:])


@pytest.mark.parametrize("status,weather", [(503, None), (200, {}), (200, {"cold_c":None})])
def test_voltage_report_requires_weather(status, weather):
    def expected_state(status, weather):
        if status == 200 and isinstance(weather, dict) and type(weather.get("cold_c")) in (float, int):
            return "READY"
        return "INCOMPLETE"
    assert expected_state(status, weather) == "INCOMPLETE"
    assert expected_state(200, {"cold_c":0}) == "READY"
