"""TCM-08 pooled AWS lines split by the Environment tag: exact, never floats.

Transparency of Leaf's real cost, never billing. Hermetic: Cost Explorer is a
fake client, ledger rows are fixtures, and the collector script writes only to
a StringIO.
"""
from __future__ import annotations

import copy
import importlib.util
import io
import json
import sys
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

SERVER_DIR = Path(__file__).resolve().parent.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

from cost_meter import pooled  # noqa: E402
from cost_meter.ledger import ESTIMATED, ResourcePeriod, compute_shares  # noqa: E402

SCRIPT = SERVER_DIR.parent / "scripts" / "collect-cost-pooled.py"
PERIOD = "2026-08"
EC2 = "Amazon Elastic Compute Cloud - Compute"
EC2_ID = "aws:amazon-elastic-compute-cloud-compute"


def _group(service, environment, amount, unit="USD"):
    return {"Keys": [service, f"Environment${environment}"],
            "Metrics": {"UnblendedCost": {"Amount": amount, "Unit": unit}}}


def _page(groups, start="2026-08-01", end="2026-09-01", token=None):
    page = {
        "GroupDefinitions": [{"Type": "DIMENSION", "Key": "SERVICE"},
                             {"Type": "TAG", "Key": "Environment"}],
        "ResultsByTime": [{"TimePeriod": {"Start": start, "End": end}, "Total": {},
                           "Groups": groups, "Estimated": False}],
        "DimensionValueAttributes": [],
        "ResponseMetadata": {"RequestId": "req-1", "HTTPStatusCode": 200},
    }
    if token:
        page["NextPageToken"] = token
    return page


class FakeCostExplorer:
    """Returns the queued pages in order (the last one forever) and records every request."""

    def __init__(self, pages):
        self.pages = list(pages)
        self.calls = []

    def get_cost_and_usage(self, **kwargs):
        self.calls.append(copy.deepcopy(kwargs))
        return copy.deepcopy(self.pages[min(len(self.calls), len(self.pages)) - 1])


def _one(observations, resource_id=EC2_ID):
    matches = [o for o in observations if o["resource_id"] == resource_id]
    assert len(matches) == 1
    return matches[0]


def _assert_exact(obs):
    assert sum((Decimal(v) for v in obs["usages"].values()), Decimal(0)) == Decimal(obs["total_usage"])


def test_staging_only_goes_to_leaf_development():
    obs = _one(pooled.pooled_usage_observations(PERIOD, [_page([_group(EC2, "staging", "12.50")])],
                                                {"tenant_a": Decimal(4)}))
    assert obs == {
        "kind": "usage", "resource_id": EC2_ID, "period": PERIOD, "unit": "usd-by-environment",
        "total_usage": "12.5", "usages": {"leaf|development": "12.5"}, "status": ESTIMATED,
        "coverage": "complete", "source": pooled.SOURCE,
    }


def test_production_split_by_activity():
    responses = [_page([_group(EC2, "production", "10"), _group(EC2, "staging", "2")])]
    obs = _one(pooled.pooled_usage_observations(
        PERIOD, responses, {"tenant_a": Decimal(3), "tenant_b": Decimal(1), "tenant_idle": Decimal(0)}))
    assert obs["usages"] == {"leaf|development": "2", "tenant_a|": "7.5", "tenant_b|": "2.5"}
    assert obs["total_usage"] == "12"
    assert obs["coverage"] == "complete"
    _assert_exact(obs)


def test_production_without_activity_goes_to_unattributed():
    obs = _one(pooled.pooled_usage_observations(PERIOD, [_page([_group(EC2, "production", "9.99")])], {}))
    assert obs["usages"] == {"leaf|unattributed": "9.99"}
    assert obs["total_usage"] == "9.99"
    assert obs["coverage"] == "complete"


def test_untagged_goes_to_unattributed_with_partial_coverage():
    responses = [_page([_group(EC2, "", "4.20"), _group(EC2, "staging", "1")])]
    obs = _one(pooled.pooled_usage_observations(PERIOD, responses, {"tenant_a": Decimal(1)}))
    assert obs["usages"] == {"leaf|development": "1", "leaf|unattributed": "4.2"}
    assert obs["total_usage"] == "5.2"
    assert obs["coverage"] == "partial"
    assert obs["status"] == ESTIMATED


def test_other_environment_value_goes_to_unattributed():
    responses = [_page([_group(EC2, "qa", "3"), _group(EC2, "Production", "1")])]
    obs = _one(pooled.pooled_usage_observations(PERIOD, responses, {"tenant_a": Decimal(2)}))
    assert obs["usages"] == {"leaf|unattributed": "3", "tenant_a|": "1"}
    assert obs["coverage"] == "partial"
    _assert_exact(obs)


def test_excluded_resource_ids_are_skipped():
    responses = [_page([_group("Amazon Elastic File System", "production", "5"),
                        _group(EC2, "production", "5")])]
    assert "aws:amazon-elastic-file-system" in pooled.DIRECT_COLLECTOR_RESOURCE_IDS
    observations = pooled.pooled_usage_observations(
        PERIOD, responses, {"tenant_a": Decimal(1)},
        exclude_resource_ids=pooled.DIRECT_COLLECTOR_RESOURCE_IDS)
    assert [o["resource_id"] for o in observations] == [EC2_ID]
    both = pooled.pooled_usage_observations(PERIOD, responses, {"tenant_a": Decimal(1)})
    assert [o["resource_id"] for o in both] == [EC2_ID, "aws:amazon-elastic-file-system"]  # sorted
    with pytest.raises(TypeError):
        pooled.pooled_usage_observations(PERIOD, responses, {}, exclude_resource_ids="aws:x")


def test_usages_sum_exactly_to_total_with_rounding_remainder():
    responses = [_page([_group(EC2, "production", "1.00"), _group(EC2, "staging", "0.10")],
                       token="p2"),
                 _page([_group(EC2, "production", "0.00")])]
    activity = {"tenant_a": Decimal(1), "tenant_b": Decimal(1), "tenant_c": Decimal(1)}
    obs = _one(pooled.pooled_usage_observations(PERIOD, responses, activity))
    assert obs["usages"] == {
        "leaf|development": "0.1", "leaf|unattributed": "0.000000000001",
        "tenant_a|": "0.333333333333", "tenant_b|": "0.333333333333", "tenant_c|": "0.333333333333",
    }
    assert obs["total_usage"] == "1.1"
    assert obs["coverage"] == "complete"
    _assert_exact(obs)
    # The observation is ledger-ready: its shares sum to exactly 1.
    rp = ResourcePeriod(obs["resource_id"], PERIOD, obs["unit"], obs["total_usage"], "0", "0")
    usages = {tuple(k.split("|")): v for k, v in obs["usages"].items()}
    shares = compute_shares(rp, usages, ESTIMATED)
    assert sum(s.usage_share for s in shares) == Decimal(1)


def test_no_floats_in_output_and_float_input_refused():
    responses = [_page([_group(EC2, "production", "3"), _group(EC2, "", "1")])]
    observations = pooled.pooled_usage_observations(PERIOD, responses, {"tenant_a": Decimal(1)})

    def _no_float(text):
        raise AssertionError(f"float in output: {text}")

    for obs in observations:
        parsed = json.loads(json.dumps(obs), parse_float=_no_float)
        assert isinstance(parsed["total_usage"], str)
        assert all(isinstance(v, str) for v in parsed["usages"].values())
    with pytest.raises(TypeError):
        pooled.pooled_usage_observations(PERIOD, responses, {"tenant_a": 1.5})
    with pytest.raises(TypeError):
        pooled.pooled_usage_observations(PERIOD, [_page([_group(EC2, "production", 3.0)])], {})


def test_malformed_input_fails_closed():
    with pytest.raises(ValueError):
        pooled.pooled_usage_observations(PERIOD, [_page([{"Keys": [EC2, "Project$x"], "Metrics": {
            "UnblendedCost": {"Amount": "1", "Unit": "USD"}}}])], {})
    with pytest.raises(ValueError):
        pooled.pooled_usage_observations(PERIOD, [_page([_group(EC2, "staging", "1", unit="EUR")])], {})
    with pytest.raises(ValueError):
        pooled.pooled_usage_observations(PERIOD, [_page([_group(EC2, "staging", "-1")])], {})
    with pytest.raises(ValueError):
        pooled.pooled_usage_observations(PERIOD, [_page([], start="2026-07-01")], {})
    with pytest.raises(ValueError):
        pooled.pooled_usage_observations(PERIOD, [], {"leaf": Decimal(1)})


def test_fetch_filters_usage_groups_by_tag_and_follows_pages():
    client = FakeCostExplorer([_page([_group(EC2, "staging", "1")], token="next-1"),
                               _page([_group(EC2, "production", "2")])])
    responses = pooled.fetch_environment_split(client, PERIOD, today=date(2026, 9, 3))
    assert len(responses) == 2 and len(client.calls) == 2
    first = client.calls[0]
    assert first["TimePeriod"] == {"Start": "2026-08-01", "End": "2026-09-01"}
    assert first["Metrics"] == ["UnblendedCost"]
    assert first["Filter"] == {"Dimensions": {"Key": "RECORD_TYPE", "Values": ["Usage"]}}
    assert first["GroupBy"] == [{"Type": "DIMENSION", "Key": "SERVICE"},
                                {"Type": "TAG", "Key": "Environment"}]
    assert "NextPageToken" not in first
    assert client.calls[1]["NextPageToken"] == "next-1"


def test_fetch_refuses_runaway_pagination():
    client = FakeCostExplorer([_page([], token="again")])
    with pytest.raises(RuntimeError):
        pooled.fetch_environment_split(client, PERIOD, today=date(2026, 9, 3), max_pages=3)
    assert len(client.calls) == 3


def test_tenant_activity_counts_distinct_utc_days():
    aug5 = datetime(2026, 8, 5, 12, 0, tzinfo=timezone.utc).timestamp()
    aug2 = datetime(2026, 8, 2, 8, 0, tzinfo=timezone.utc).timestamp()
    agent_rows = [
        {"kind": "turn", "tenant_id": "tenant_a", "ts": "2026-08-01T10:00:00Z"},
        {"kind": "turn", "tenant_id": "tenant_a", "ts": "2026-08-01T23:00:00Z"},
        {"kind": "turn", "tenant_id": "tenant_a", "ts": "2026-08-02T01:00:00Z"},
        {"kind": "turn", "tenant_id": "tenant_f", "ts": "2026-07-31T22:00:00-05:00"},  # Aug 1 UTC
        {"kind": "turn", "tenant_id": "tenant_c", "ts": "2026-07-20T10:00:00Z"},
        {"kind": "session", "tenant_id": "tenant_d", "ts": "2026-08-03T10:00:00Z"},
        "not a row",
    ]
    broker_rows = [
        {"tenant_id": "tenant_a", "ts": aug2, "status": "ok"},
        {"tenant_id": "tenant_a", "ts": aug5, "status": "ok"},
        {"tenant_id": "tenant_b", "ts": aug5, "status": "quota_exceeded"},
        {"tenant_id": "tenant_e", "ts": aug5, "status": "ok", "aps_live": False},
        None,
    ]
    activity = pooled.tenant_activity_from_rows(PERIOD, agent_rows, broker_rows)
    assert activity == {"tenant_a": Decimal(3), "tenant_e": Decimal(1), "tenant_f": Decimal(1)}
    assert all(isinstance(v, Decimal) for v in activity.values())
    assert pooled.tenant_activity_from_rows(PERIOD, None, None) == {}


def _load_script():
    spec = importlib.util.spec_from_file_location("collect_cost_pooled", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_script_prints_json_lines():
    script = _load_script()
    client = FakeCostExplorer([_page([_group(EC2, "production", "6"),
                                      _group("Amazon Elastic File System", "production", "5")])])
    out = io.StringIO()
    code = script.main(["--period", PERIOD], client=client, stdout=out,
                       now=datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc),
                       agent_rows=[{"kind": "turn", "tenant_id": "tenant_a", "ts": "2026-08-01T10:00:00Z"},
                                   {"kind": "turn", "tenant_id": "tenant_b", "ts": "2026-08-01T10:00:00Z"},
                                   {"kind": "turn", "tenant_id": "tenant_b", "ts": "2026-08-09T10:00:00Z"}],
                       broker_rows=[])
    assert code == 0
    lines = [json.loads(line) for line in out.getvalue().splitlines()]
    assert [o["resource_id"] for o in lines] == [EC2_ID]  # EFS has its own collector
    assert lines[0]["usages"] == {"tenant_a|": "2", "tenant_b|": "4"}
    assert script.main(["--period", "2026-13"], client=client, stdout=io.StringIO()) == 2
