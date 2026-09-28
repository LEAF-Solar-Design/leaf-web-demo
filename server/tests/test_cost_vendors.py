"""TCM-07 vendor, subscription and fleet costs from the declared schedule.

Transparency of Leaf's real cost, never billing. Hermetic: reads only the repo's
config/cost-vendors.yaml and in-memory documents; nothing touches the ledger.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from decimal import Decimal
from pathlib import Path

import pytest

SERVER_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = SERVER_DIR.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

from cost_meter.vendors import (  # noqa: E402
    DEFAULT_CONFIG_PATH, VendorConfig, VendorConfigError, load_vendor_config, month_amount,
    parse_vendor_config, validate_vendor_config, vendor_observations,
)

SEED_IDS = {
    "subscription:ai-models", "vendor:github", "subscription:autodesk-adn",
    "vendor:software-and-fees-unitemized", "vendor:other-bills-unitemized",
}


def _entry(**overrides):
    entry = {
        "resource_id": "vendor:example",
        "display_name": "Example",
        "monthly_usd": "10",
        "basis": "invoice",
        "source": "Example invoice 2026-09",
    }
    entry.update(overrides)
    return {k: v for k, v in entry.items() if v is not None}


def _doc(*entries):
    return {"version": 1, "entries": list(entries) or [_entry()]}


def _refuses(doc, match=None):
    with pytest.raises(VendorConfigError, match=match):
        validate_vendor_config(doc)


def _walk(value):
    yield value
    if isinstance(value, dict):
        for v in value.values():
            yield from _walk(v)
    elif isinstance(value, list):
        for v in value:
            yield from _walk(v)


def test_seed_file_loads_and_emits_one_cost_and_one_usage_per_entry():
    assert DEFAULT_CONFIG_PATH == REPO_ROOT / "config" / "cost-vendors.yaml"
    config = load_vendor_config()
    assert isinstance(config, VendorConfig)
    assert {e.resource_id for e in config.entries} == SEED_IDS
    assert not any("payroll" in e.resource_id for e in config.entries)
    obs = vendor_observations("2026-09", config)
    costs = {o["resource_id"]: o for o in obs if o["kind"] == "cost"}
    usages = {o["resource_id"]: o for o in obs if o["kind"] == "usage"}
    assert set(costs) == set(usages) == SEED_IDS and len(obs) == 10
    assert costs["subscription:ai-models"]["gross_cost_usd"] == "3000.00"
    assert costs["vendor:github"]["gross_cost_usd"] == "590.00"
    assert costs["subscription:autodesk-adn"]["gross_cost_usd"] == "135.00"
    assert costs["vendor:software-and-fees-unitemized"]["gross_cost_usd"] == "970.00"
    assert costs["vendor:other-bills-unitemized"]["gross_cost_usd"] == "720.00"
    assert usages["subscription:ai-models"]["usages"] == {"leaf|fleet": "1"}
    assert usages["vendor:github"]["usages"] == {"leaf|development": "1"}


def test_observation_shapes_follow_the_wave_contract():
    config = validate_vendor_config(_doc(
        _entry(resource_id="vendor:est", basis="estimate",
               default_share={"leaf|development": "0.25", "tenant_42|": "0.75"}),
        _entry(resource_id="vendor:inv", basis="invoice"),
    ))
    obs = vendor_observations("2026-09", config)
    cost_est, usage_est, cost_inv, _usage_inv = obs
    assert cost_est == {
        "kind": "cost", "resource_id": "vendor:est", "period": "2026-09",
        "gross_cost_usd": "10.00", "credits_usd": "0", "coverage": "partial",
        "source_batch_id": cost_est["source_batch_id"], "source": "Example invoice 2026-09",
    }
    assert cost_est["source_batch_id"].startswith("cost-vendors:2026-09:")
    assert usage_est == {
        "kind": "usage", "resource_id": "vendor:est", "period": "2026-09", "unit": "share",
        "total_usage": "1", "usages": {"leaf|development": "0.25", "tenant_42|": "0.75"},
        "status": "ESTIMATED", "coverage": "partial", "source": "Example invoice 2026-09",
    }
    assert cost_inv["coverage"] == "complete"
    # The same validated schedule names the same batch; a raw mapping is validated in place.
    assert vendor_observations("2026-09", _doc(
        _entry(resource_id="vendor:est", basis="estimate",
               default_share={"leaf|development": "0.25", "tenant_42|": "0.75"}),
        _entry(resource_id="vendor:inv", basis="invoice"),
    )) == obs


def test_annual_amount_spreads_evenly_over_its_twelve_months():
    config = validate_vendor_config(_doc(
        _entry(monthly_usd=None, annual_usd="1620", start="2026-03")))
    (entry,) = config.entries
    months = [f"2026-{m:02d}" for m in range(3, 13)] + ["2027-01", "2027-02"]
    assert [month_amount(entry, p) for p in months] == [Decimal("135.00")] * 12
    # Outside its coverage the amount is unknown: absent, never a fabricated 0.
    assert month_amount(entry, "2026-02") is None
    assert month_amount(entry, "2027-03") is None
    assert vendor_observations("2026-02", config) == []
    assert [o["gross_cost_usd"] for o in vendor_observations("2027-02", config)
            if o["kind"] == "cost"] == ["135.00"]


def test_annual_spread_with_leftover_cents_sums_exactly():
    config = validate_vendor_config(_doc(
        _entry(monthly_usd=None, annual_usd="1000.03", start="2026-11")))
    (entry,) = config.entries
    months = ["2026-11", "2026-12"] + [f"2027-{m:02d}" for m in range(1, 11)]
    amounts = [month_amount(entry, p) for p in months]
    assert sum(amounts, Decimal(0)) == Decimal("1000.03")
    assert amounts[:7] == [Decimal("83.34")] * 7 and amounts[7:] == [Decimal("83.33")] * 5


def test_no_floats_anywhere_in_the_observations():
    obs = vendor_observations("2026-06", load_vendor_config())
    assert obs
    for value in _walk(obs):
        assert not isinstance(value, float)
    for o in obs:
        if o["kind"] == "cost":
            assert isinstance(o["gross_cost_usd"], str) and isinstance(o["credits_usd"], str)
        else:
            assert isinstance(o["total_usage"], str)
            assert all(isinstance(v, str) for v in o["usages"].values())
    json.dumps(obs)


def test_float_and_int_amounts_are_refused():
    _refuses(_doc(_entry(monthly_usd=3000.0)), "quoted decimal string")
    _refuses(_doc(_entry(monthly_usd=3000)), "quoted decimal string")
    _refuses(_doc(_entry(default_share={"leaf|development": 1})), "quoted decimal string")
    with pytest.raises(VendorConfigError, match="quoted decimal string"):
        parse_vendor_config(
            "version: 1\nentries:\n  - resource_id: vendor:x\n    display_name: X\n"
            "    monthly_usd: 12.5\n    basis: invoice\n    source: Invoice 1\n")


def test_bad_decimals_are_refused():
    for bad in ("", "abc", "NaN", "Infinity", "-1", "1.005", "1e400x"):
        _refuses(_doc(_entry(monthly_usd=bad)))
    _refuses(_doc(_entry(monthly_usd=None, annual_usd="12.345", start="2026-01")), "cent precision")


def test_unknown_keys_are_refused():
    _refuses(_doc(_entry(currency="USD")), "unknown keys")
    doc = _doc()
    doc["notes"] = "x"
    _refuses(doc, "unknown top-level keys")


def test_shares_must_sum_to_exactly_one():
    _refuses(_doc(_entry(default_share={"leaf|development": "0.5", "leaf|ci": "0.4"})),
             "sum to exactly 1")
    _refuses(_doc(_entry(default_share={"leaf|development": "0.6", "leaf|ci": "0.6"})))
    _refuses(_doc(_entry(default_share={"leaf|payroll": "1"})), "leaf dimension")
    _refuses(_doc(_entry(default_share={"tenant_1|development": "1"})), "dimension ''")
    _refuses(_doc(_entry(default_share={"leaf-development": "1"})), "participant|dimension")
    _refuses(_doc(_entry(default_share={})))
    ok = validate_vendor_config(_doc(_entry(default_share={"leaf|development": "0.5", "leaf|ci": "0.5"})))
    assert ok.entries[0].default_share == (("leaf|ci", Decimal("0.5")), ("leaf|development", Decimal("0.5")))


def test_duplicate_resource_ids_are_refused():
    _refuses(_doc(_entry(), _entry(display_name="Again")), "duplicate resource_id")


def test_duplicate_yaml_keys_are_refused():
    with pytest.raises(VendorConfigError, match="duplicate key"):
        parse_vendor_config(
            "version: 1\nentries:\n  - resource_id: vendor:x\n    display_name: X\n"
            "    monthly_usd: \"1\"\n    monthly_usd: \"2\"\n    basis: invoice\n    source: Invoice 1\n")
    with pytest.raises(VendorConfigError, match="not valid YAML"):
        parse_vendor_config("version: [1\n")


def test_amount_shape_and_start_rules():
    _refuses(_doc(_entry(annual_usd="100", start="2026-01")), "exactly one of")
    _refuses(_doc(_entry(monthly_usd=None)), "exactly one of")
    _refuses(_doc(_entry(monthly_usd=None, annual_usd="100")), "start month")
    _refuses(_doc(_entry(monthly_usd=None, annual_usd="100", start="2026-13")), "YYYY-MM")
    _refuses(_doc(_entry(start="2026-01")), "only to annual_usd")
    _refuses(_doc(_entry(basis="guess")), "basis")
    _refuses(_doc(_entry(source="")), "source")
    _refuses({"version": 2, "entries": [_entry()]}, "version")
    _refuses({"version": 1, "entries": []}, "entries")


def test_resource_ids_are_vendors_or_subscriptions_and_never_payroll():
    _refuses(_doc(_entry(resource_id="aws:amazon-s3")), "vendor:<name> or subscription:<name>")
    _refuses(_doc(_entry(resource_id="Vendor:GitHub")), "vendor:<name> or subscription:<name>")
    _refuses(_doc(_entry(resource_id="vendor:payroll")), "payroll")


def test_bad_period_is_refused():
    config = load_vendor_config()
    for bad in ("2026-9", "2026-00", "2026-13", "202609", None):
        with pytest.raises(VendorConfigError, match="YYYY-MM"):
            vendor_observations(bad, config)


def test_script_prints_json_lines_and_refuses_a_bad_schedule(tmp_path, capsys):
    spec = importlib.util.spec_from_file_location(
        "collect_cost_vendors", REPO_ROOT / "scripts" / "collect-cost-vendors.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.main(["--period", "2026-09"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert [json.loads(line) for line in lines] == vendor_observations("2026-09", load_vendor_config())
    bad = tmp_path / "bad.yaml"
    bad.write_text("version: 1\nentries: []\n", encoding="utf-8")
    assert module.main(["--period", "2026-09", "--config", str(bad)]) == 2
    captured = capsys.readouterr()
    assert captured.out == "" and "entries" in captured.err
