"""TCM-06 Cost Explorer import: gross cost by service, credits apart, never floats.

Transparency of Leaf's real cost, never billing. Hermetic: Cost Explorer is a
fake client, and the collector script writes only to a StringIO or tmp_path.
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

from cost_meter import aws_import  # noqa: E402
from cost_meter.ledger import ResourcePeriod  # noqa: E402

SCRIPT = SERVER_DIR.parent / "scripts" / "collect-cost-aws.py"
CLOSED = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)  # fetched after August closed


def _group(service, record_type, amount, unit="USD"):
    return {"Keys": [service, record_type],
            "Metrics": {"UnblendedCost": {"Amount": amount, "Unit": unit}}}


def _page(groups, start="2026-08-01", end="2026-09-01", token=None, request_id="req-1"):
    page = {
        "GroupDefinitions": [{"Type": "DIMENSION", "Key": "SERVICE"},
                             {"Type": "DIMENSION", "Key": "RECORD_TYPE"}],
        "ResultsByTime": [{"TimePeriod": {"Start": start, "End": end}, "Total": {},
                           "Groups": groups, "Estimated": False}],
        "DimensionValueAttributes": [],
        "ResponseMetadata": {"RequestId": request_id, "HTTPStatusCode": 200},
    }
    if token:
        page["NextPageToken"] = token
    return page


class FakeCostExplorer:
    """Returns the queued pages in order and records every request."""

    def __init__(self, pages):
        self.pages = list(pages)
        self.calls = []

    def get_cost_and_usage(self, **kwargs):
        self.calls.append(copy.deepcopy(kwargs))
        return copy.deepcopy(self.pages[len(self.calls) - 1])


class EndlessCostExplorer:
    def __init__(self):
        self.calls = 0

    def get_cost_and_usage(self, **kwargs):
        self.calls += 1
        return _page([], token="again")


def _by_id(observations):
    return {o["resource_id"]: o for o in observations}


def test_fully_credited_month_keeps_nonzero_gross():
    pages = [_page([
        _group("Amazon Elastic Compute Cloud - Compute", "Usage", "41.2500000000"),
        _group("Amazon Elastic Compute Cloud - Compute", "Credit", "-41.2500000000"),
    ])]
    obs = _by_id(aws_import.to_cost_observations("2026-08", pages, CLOSED))
    ec2 = obs["aws:amazon-elastic-compute-cloud-compute"]
    assert ec2["gross_cost_usd"] == "41.2500000000"
    assert ec2["credits_usd"] == "41.2500000000"
    assert Decimal(ec2["gross_cost_usd"]) > 0
    assert ec2["coverage"] == "complete"
    rp = ResourcePeriod(resource_id=ec2["resource_id"], period=ec2["period"], unit="usd",
                        total_usage=None, gross_cost_usd=ec2["gross_cost_usd"],
                        credits_usd=ec2["credits_usd"], source_batch_ids=(ec2["source_batch_id"],),
                        coverage=ec2["coverage"])
    assert rp.gross_cost_usd == Decimal("41.25")


def test_fetch_follows_next_page_token_with_the_closed_month_request():
    client = FakeCostExplorer([
        _page([_group("Amazon Simple Storage Service", "Usage", "1.10")], token="tok-2"),
        _page([_group("AWS Lambda", "Usage", "0.20")], token="tok-3"),
        _page([_group("Amazon Simple Storage Service", "Tax", "0.05")]),
    ])
    responses = aws_import.fetch(client, "2026-08", today=date(2026, 9, 3))
    assert len(responses) == 3
    assert [c.get("NextPageToken") for c in client.calls] == [None, "tok-2", "tok-3"]
    first = client.calls[0]
    assert first["TimePeriod"] == {"Start": "2026-08-01", "End": "2026-09-01"}
    assert first["Granularity"] == "MONTHLY"
    assert first["Metrics"] == ["UnblendedCost"]
    assert first["GroupBy"] == [{"Type": "DIMENSION", "Key": "SERVICE"},
                                {"Type": "DIMENSION", "Key": "RECORD_TYPE"}]
    obs = _by_id(aws_import.to_cost_observations("2026-08", responses, CLOSED))
    assert obs["aws:amazon-simple-storage-service"]["gross_cost_usd"] == "1.15"
    assert obs["aws:aws-lambda"]["gross_cost_usd"] == "0.20"


def test_pagination_is_bounded_and_fails_closed():
    client = EndlessCostExplorer()
    with pytest.raises(RuntimeError, match="pagination exceeded 5 pages"):
        aws_import.fetch(client, "2026-08", today=date(2026, 9, 3), max_pages=5)
    assert client.calls == 5


def test_current_month_is_month_to_date_and_partial():
    client = FakeCostExplorer([_page([_group("AWS CodeBuild", "Usage", "3.00")],
                                     start="2026-09-01", end="2026-09-29")])
    responses = aws_import.fetch(client, "2026-09", today=date(2026, 9, 28))
    assert client.calls[0]["TimePeriod"] == {"Start": "2026-09-01", "End": "2026-09-29"}
    fetched = datetime(2026, 9, 28, 18, 0, tzinfo=timezone.utc)
    obs = aws_import.to_cost_observations("2026-09", responses, fetched)
    assert [o["coverage"] for o in obs] == ["partial"]
    with pytest.raises(ValueError, match="future"):
        aws_import.fetch(client, "2026-10", today=date(2026, 9, 28))
    with pytest.raises(ValueError, match="after fetched_at"):
        aws_import.to_cost_observations("2026-10", responses, fetched)


def test_codebuild_and_small_services_are_kept():
    pages = [_page([
        _group("AWS CodeBuild", "Usage", "12.3400000000"),
        _group("AWS Key Management Service", "Usage", "0.0000001"),
        _group("Amazon Route 53", "Usage", "0"),
    ])]
    obs = _by_id(aws_import.to_cost_observations("2026-08", pages, CLOSED))
    assert obs["aws:aws-codebuild"]["gross_cost_usd"] == "12.3400000000"
    assert obs["aws:aws-key-management-service"]["gross_cost_usd"] == "0.0000001"
    assert obs["aws:amazon-route-53"]["gross_cost_usd"] == "0"
    assert list(obs) == sorted(obs)


def test_tax_and_fees_count_as_gross_and_refunds_as_credits():
    pages = [_page([
        _group("Amazon Elastic File System", "Usage", "10.00"),
        _group("Amazon Elastic File System", "Tax", "0.80"),
        _group("Amazon Elastic File System", "Fee", "1.20"),
        _group("Amazon Elastic File System", "Credit", "-5.00"),
        _group("Amazon Elastic File System", "Refund", "-2.50"),
        _group("Tax", "Tax", "3.00"),
    ])]
    obs = _by_id(aws_import.to_cost_observations("2026-08", pages, CLOSED))
    efs = obs["aws:amazon-elastic-file-system"]
    assert efs["gross_cost_usd"] == "12.00"
    assert efs["credits_usd"] == "7.50"
    assert obs["aws:tax"]["gross_cost_usd"] == "3.00"
    assert set(efs) == {"kind", "resource_id", "period", "gross_cost_usd", "credits_usd",
                        "coverage", "source_batch_id", "source"}
    assert efs["kind"] == "cost" and efs["source"] == "aws-cost-explorer"


def test_resource_id_naming_rule():
    assert aws_import.resource_id_for_service("Amazon Elastic File System") == \
        "aws:amazon-elastic-file-system"
    assert aws_import.resource_id_for_service("  EC2 - Other  ") == "aws:ec2-other"
    assert aws_import.resource_id_for_service("AmazonCloudWatch") == "aws:amazoncloudwatch"
    with pytest.raises(ValueError):
        aws_import.resource_id_for_service(" - ")


@pytest.mark.parametrize("estimated,coverage", [(True, "partial"), (False, "complete")])
def test_closed_month_respects_cost_explorer_estimated(estimated, coverage):
    page = _page([_group("AWS Lambda", "Usage", "1.25")])
    page["ResultsByTime"][0]["Estimated"] = estimated
    observations = aws_import.to_cost_observations("2026-08", [page], CLOSED)
    assert observations[0]["coverage"] == coverage
    assert observations[0]["gross_cost_usd"] == "1.25"


def test_estimated_page_cannot_be_overridden_by_a_final_page():
    first = _page([_group("AWS Lambda", "Usage", "1.25")], token="next")
    first["ResultsByTime"][0]["Estimated"] = True
    last = _page([_group("AWS Lambda", "Tax", "0.05")])
    observations = aws_import.to_cost_observations("2026-08", [first, last], CLOSED)
    assert observations[0]["coverage"] == "partial"
    assert observations[0]["gross_cost_usd"] == "1.30"


@pytest.mark.parametrize("period,today,end", [
    ("2026-12", date(2027, 1, 3), "2027-01-01"),
    ("2026-12", date(2026, 12, 31), "2027-01-01"),
    ("2028-02", date(2028, 3, 3), "2028-03-01"),
    ("2028-02", date(2028, 2, 29), "2028-03-01"),
])
def test_fetch_preserves_exclusive_calendar_month_ends(period, today, end):
    client = FakeCostExplorer([_page([], start=period + "-01", end=end)])
    aws_import.fetch(client, period, today=today)
    assert client.calls[0]["TimePeriod"] == {"Start": period + "-01", "End": end}


def _publication_cost(period, now):
    return aws_import.to_cost_observations(
        period, [_page([_group("AWS Lambda", "Usage", "2.00")], start=period + "-01")], now)


@pytest.mark.parametrize("now,argv,expected", [
    (datetime(2026, 10, 3, tzinfo=timezone.utc), [], ["2026-09", "2026-10"]),
    (datetime(2026, 10, 5, tzinfo=timezone.utc), [], ["2026-09", "2026-10"]),
    (datetime(2026, 10, 6, tzinfo=timezone.utc), [], ["2026-10"]),
    (datetime(2026, 10, 3, tzinfo=timezone.utc), ["--period", "2026-08"], ["2026-08"]),
    (datetime(2026, 10, 3, tzinfo=timezone.utc), ["--reconcile-days", "0"], ["2026-10"]),
    (datetime(2026, 10, 6, tzinfo=timezone.utc), ["--reconcile-days", "6"],
     ["2026-09", "2026-10"]),
    (datetime(2027, 1, 3, tzinfo=timezone.utc), [], ["2026-12", "2027-01"]),
    (datetime(2028, 3, 3, tzinfo=timezone.utc), [], ["2028-02", "2028-03"]),
    (datetime.fromisoformat("2026-10-05T23:30:00-02:00"), [], ["2026-10"]),
])
def test_publish_reconciles_previous_month_first(tmp_path, now, argv, expected):
    from cost_meter import publish_main
    from cost_meter.store import CostLedgerStore, ENV_DIR

    called = []

    def collect(period, fetched_at):
        called.append(period)
        return _publication_cost(period, fetched_at)

    out = io.StringIO()
    assert publish_main.main(argv, collectors={"aws-cost-explorer": collect},
                             environ={ENV_DIR: str(tmp_path)}, stdout=out, now=now) == 0
    summaries = [json.loads(line) for line in out.getvalue().splitlines()]
    assert called == expected
    assert [summary["period"] for summary in summaries] == expected
    store = CostLedgerStore(tmp_path)
    for summary in summaries:
        revisions = store.read_publication(summary["publication_id"])
        assert [revision.period for revision in revisions] == [summary["period"]]


@pytest.mark.parametrize("missing_source,required,expected_code", [
    ("aws-cost-explorer", ["--required-sources", "aws-cost-explorer"], 3),
    ("aws-cost-explorer", [], 0),
    ("cost-vendors", ["--required-sources", "aws-cost-explorer"], 0),
])
def test_required_source_exit_keeps_publication(tmp_path, capsys, missing_source,
                                               required, expected_code):
    from cost_meter import publish_main, publisher
    from cost_meter.store import CostLedgerStore, ENV_DIR

    def fail(period, now):
        raise RuntimeError("collector unavailable")

    collectors = {"aws-cost-explorer": _publication_cost, "cost-vendors": _publication_cost}
    collectors[missing_source] = fail
    out = io.StringIO()
    code = publish_main.main(["--period", "2026-08", *required], collectors=collectors,
                             environ={ENV_DIR: str(tmp_path)}, stdout=out, now=CLOSED)
    assert code == expected_code
    summary = json.loads(out.getvalue())
    assert summary["missing_sources"] == [missing_source]
    assert CostLedgerStore(tmp_path).read_publication(summary["publication_id"])
    manifest = json.loads((tmp_path / "publications" /
                           (summary["publication_id"] + ".json")).read_text(encoding="utf-8"))
    assert "metadata" not in manifest
    assert missing_source in publisher.publication_info(
        CostLedgerStore(tmp_path), summary["publication_id"])["missing_sources"]
    if expected_code == 3:
        assert "required sources missing: aws-cost-explorer" in capsys.readouterr().err


def test_reconciliation_continues_after_required_source_failure(tmp_path):
    from cost_meter import publish_main
    from cost_meter.store import ENV_DIR

    def aws(period, now):
        if period == "2026-09":
            raise RuntimeError("September unavailable")
        return _publication_cost(period, now)

    out = io.StringIO()
    code = publish_main.main(
        ["--required-sources", "aws-cost-explorer"],
        collectors={"aws-cost-explorer": aws, "cost-vendors": _publication_cost},
        environ={ENV_DIR: str(tmp_path)}, stdout=out,
        now=datetime(2026, 10, 3, tzinfo=timezone.utc))
    assert code == 3
    summaries = [json.loads(line) for line in out.getvalue().splitlines()]
    assert [summary["period"] for summary in summaries] == ["2026-09", "2026-10"]
    assert [summary["missing_sources"] for summary in summaries] == [["aws-cost-explorer"], []]


def test_the_same_responses_give_the_same_batch_id():
    groups = [_group("AWS CodeBuild", "Usage", "1.00"), _group("AWS Lambda", "Usage", "2.00")]
    first = aws_import.to_cost_observations("2026-08", [_page(groups, request_id="a")], CLOSED)
    again = aws_import.to_cost_observations("2026-08", [_page(copy.deepcopy(groups), request_id="b")],
                                            CLOSED)
    assert first == again
    batch = first[0]["source_batch_id"]
    assert len(batch) == 64 and all(o["source_batch_id"] == batch for o in first)
    changed = aws_import.to_cost_observations(
        "2026-08", [_page([_group("AWS CodeBuild", "Usage", "1.01")])], CLOSED)
    assert changed[0]["source_batch_id"] != batch


def test_no_floats_in_and_none_out():
    pages = [_page([_group("AWS CodeBuild", "Usage", "1.10"),
                    _group("AWS CodeBuild", "Credit", "-0.10")])]
    obs = aws_import.to_cost_observations("2026-08", pages, CLOSED)

    def walk(value):
        assert not isinstance(value, float)
        if isinstance(value, dict):
            for v in value.values():
                walk(v)
        elif isinstance(value, list):
            for v in value:
                walk(v)

    walk(obs)
    assert json.loads(json.dumps(obs), parse_float=lambda s: pytest.fail(f"float {s}")) == obs
    with pytest.raises(TypeError):
        aws_import.to_cost_observations("2026-08", [_page([_group("AWS CodeBuild", "Usage", 1.1)])],
                                        CLOSED)


def test_malformed_input_fails_closed():
    with pytest.raises(ValueError, match="not USD"):
        aws_import.to_cost_observations("2026-08", [_page([_group("AWS CodeBuild", "Usage", "1", "EUR")])],
                                        CLOSED)
    with pytest.raises(ValueError, match="outside"):
        aws_import.to_cost_observations("2026-08", [_page([], start="2026-07-01")], CLOSED)
    with pytest.raises(ValueError, match="negative"):
        aws_import.to_cost_observations(
            "2026-08", [_page([_group("AWS CodeBuild", "SavingsPlanNegation", "-4.00")])], CLOSED)
    with pytest.raises(ValueError, match="timezone"):
        aws_import.to_cost_observations("2026-08", [], datetime(2026, 9, 3))
    with pytest.raises(ValueError, match="YYYY-MM"):
        aws_import.to_cost_observations("2026-8", [], CLOSED)


# --------------------------------------------------------------------------- #
# physical usage (TCM-21): build-minutes for CodeBuild, instance-hours for EC2
# --------------------------------------------------------------------------- #
EC2 = "Amazon Elastic Compute Cloud - Compute"
CODEBUILD_ID = "aws:aws-codebuild"
EC2_ID = "aws:amazon-elastic-compute-cloud-compute"


def _quantity(service, usage_type, amount, unit):
    return {"Keys": [service, usage_type],
            "Metrics": {"UsageQuantity": {"Amount": amount, "Unit": unit}}}


def _quantity_page(start="2026-08-01", token=None):
    return _page([
        _quantity("AWS CodeBuild", "USE1-Build-Min:Linux:g1.small", "120.5", "Minutes"),
        _quantity("AWS CodeBuild", "USE1-Build-Min:Linux:g1.large", "30", "Minutes"),
        _quantity("AWS CodeBuild", "USE1-Storage", "4", "GB"),
        _quantity(EC2, "USE1-BoxUsage:t3.medium", "720", "Hrs"),
        _quantity(EC2, "BoxUsage:t3.micro", "10.25", "Hrs"),
        _quantity(EC2, "USE1-EBS:VolumeUsage.gp3", "300", "GB-Mo"),
        _quantity(EC2, "USE1-DataTransfer-Out-Bytes", "12.5", "GB"),
        _quantity("AWS Lambda", "USE1-Request", "1000000", "Requests"),
    ], start=start, token=token)


class RoutingCostExplorer:
    """Answers cost and quantity requests from separate queues; quantities may raise."""

    def __init__(self, cost_pages, quantity_pages=(), quantity_error=None):
        self.cost_pages = list(cost_pages)
        self.quantity_pages = list(quantity_pages)
        self.quantity_error = quantity_error
        self.metrics = []

    def get_cost_and_usage(self, **kwargs):
        self.metrics.append(kwargs["Metrics"])
        if kwargs["Metrics"] == ["UsageQuantity"]:
            if self.quantity_error is not None:
                raise self.quantity_error
            return copy.deepcopy(self.quantity_pages.pop(0))
        return copy.deepcopy(self.cost_pages.pop(0))


def test_fetch_quantities_asks_for_usage_quantity_of_codebuild_and_ec2_only():
    client = FakeCostExplorer([_quantity_page(token="tok-2"), _page([])])
    responses = aws_import.fetch_quantities(client, "2026-08", today=date(2026, 9, 3))
    assert len(responses) == 2
    assert [c.get("NextPageToken") for c in client.calls] == [None, "tok-2"]
    first = client.calls[0]
    assert first["TimePeriod"] == {"Start": "2026-08-01", "End": "2026-09-01"}
    assert first["Granularity"] == "MONTHLY"
    assert first["Metrics"] == ["UsageQuantity"]
    assert first["GroupBy"] == [{"Type": "DIMENSION", "Key": "SERVICE"},
                                {"Type": "DIMENSION", "Key": "USAGE_TYPE"}]
    assert first["Filter"] == {"Dimensions": {"Key": "SERVICE",
                                               "Values": ["AWS CodeBuild", EC2, "CodeBuild"]}}
    cost_client = FakeCostExplorer([_page([])])
    aws_import.fetch(cost_client, "2026-08", today=date(2026, 9, 3))
    assert "Filter" not in cost_client.calls[0]
    with pytest.raises(RuntimeError, match="pagination exceeded 3 pages"):
        aws_import.fetch_quantities(EndlessCostExplorer(), "2026-08", today=date(2026, 9, 3), max_pages=3)


def test_real_codebuild_usage_reports_minutes_without_lambda_seconds():
    usage_types = [
        ("USE1-Build-Min:Linux:g1.large", "243514", "Minutes"),
        ("USE1-Build-Min:Linux:g1.medium", "25776", "Minutes"),
        ("USE1-Build-Min:Windows:g1.medium", "21121", "Minutes"),
        ("USE1-Build-Min:Linux:g1.xlarge", "19710", "Minutes"),
        ("USE1-Build-Min:Linux:g1.small", "9583", "Minutes"),
        ("USE1-Build-Sec:Linux:Lambda:arm.4GB", "21399", "Second"),
        ("USE1-Build-Sec:Linux:Lambda:arm.2GB", "21209", "Second"),
        ("USE1-Build-Sec:Linux:Lambda:x86-64.2GB", "405", "Second"),
    ]
    page = {
        "GroupDefinitions": [{"Type": "DIMENSION", "Key": "SERVICE"},
                             {"Type": "DIMENSION", "Key": "USAGE_TYPE"}],
        "ResultsByTime": [{
            "TimePeriod": {"Start": "2026-09-01", "End": "2026-10-01"},
            "Total": {},
            "Groups": [_quantity("CodeBuild", usage_type, amount, unit)
                       for usage_type, amount, unit in usage_types],
            "Estimated": False,
        }],
    }
    assert aws_import.resource_id_for_service("CodeBuild") == "aws:codebuild"
    client = FakeCostExplorer([page])
    responses = aws_import.fetch_quantities(client, "2026-09", today=date(2026, 10, 3))
    assert aws_import.to_physical_usage(
        responses, period="2026-09", fetched_at=datetime(2026, 10, 3, tzinfo=timezone.utc)) == {
        "aws:codebuild": {"quantity": "319704", "unit": "build-minutes", "coverage": "complete"},
    }


def test_physical_usage_sums_like_units_and_ignores_every_other_usage_type():
    usage = aws_import.to_physical_usage([_quantity_page()], period="2026-08", fetched_at=CLOSED)
    assert usage == {
        CODEBUILD_ID: {"quantity": "150.5", "unit": "build-minutes", "coverage": "complete"},
        EC2_ID: {"quantity": "730.25", "unit": "instance-hours", "coverage": "complete"},
    }
    assert list(usage) == sorted(usage)
    only_ebs = _page([_quantity(EC2, "USE1-EBS:VolumeUsage.gp3", "300", "GB-Mo"),
                      _quantity(EC2, "USE1-DataTransfer-Out-Bytes", "12.5", "GB")])
    assert aws_import.to_physical_usage([only_ebs]) == {}


def test_physical_usage_sums_across_pages():
    first = _page([_quantity("AWS CodeBuild", "USE1-Build-Min:Linux:g1.small", "10", "Minutes")],
                  token="next")
    last = _page([_quantity("AWS CodeBuild", "USE1-Build-Min:ARM:g1.small", "2.25", "Minutes")])
    usage = aws_import.to_physical_usage([first, last], period="2026-08", fetched_at=CLOSED)
    assert usage == {CODEBUILD_ID: {"quantity": "12.25", "unit": "build-minutes", "coverage": "complete"}}


@pytest.mark.parametrize("estimated,coverage", [(True, "partial"), (False, "complete")])
def test_an_estimated_period_gives_partial_physical_usage(estimated, coverage):
    page = _quantity_page()
    page["ResultsByTime"][0]["Estimated"] = estimated
    usage = aws_import.to_physical_usage([page], period="2026-08", fetched_at=CLOSED)
    assert {entry["coverage"] for entry in usage.values()} == {coverage}
    assert {entry["coverage"] for entry in aws_import.to_physical_usage([page]).values()} == {coverage}


def test_the_current_month_physical_usage_is_partial():
    fetched = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)
    usage = aws_import.to_physical_usage([_quantity_page()], period="2026-08", fetched_at=fetched)
    assert {entry["coverage"] for entry in usage.values()} == {"partial"}


def test_physical_usage_fails_closed_and_never_holds_a_float():
    usage = aws_import.to_physical_usage([_quantity_page()], period="2026-08", fetched_at=CLOSED)
    text = json.dumps(usage)
    assert json.loads(text, parse_float=lambda s: pytest.fail(f"float {s}")) == usage
    with pytest.raises(TypeError):
        aws_import.to_physical_usage(
            [_page([_quantity("AWS CodeBuild", "USE1-Build-Min:Linux:g1.small", 1.5, "Minutes")])])
    with pytest.raises(ValueError, match="negative"):
        aws_import.to_physical_usage(
            [_page([_quantity("AWS CodeBuild", "USE1-Build-Min:Linux:g1.small", "-1", "Minutes")])])
    with pytest.raises(ValueError, match="units differ"):
        aws_import.to_physical_usage([_page([
            _quantity("AWS CodeBuild", "USE1-Build-Min:Linux:g1.small", "1", "Minutes"),
            _quantity("AWS CodeBuild", "USE1-Build-Min:Linux:g1.large", "1", "Hrs"),
        ])])
    with pytest.raises(ValueError, match="outside"):
        aws_import.to_physical_usage([_quantity_page(start="2026-07-01")], period="2026-08",
                                     fetched_at=CLOSED)


def test_publish_carries_physical_usage_into_the_publication(tmp_path):
    from cost_meter import publish_main, publisher
    from cost_meter.store import CostLedgerStore, ENV_DIR

    cost_page = _page([_group("AWS CodeBuild", "Usage", "12.34"), _group(EC2, "Usage", "5.00")])

    def collect(period, now):
        client = RoutingCostExplorer([cost_page], [_quantity_page()])
        return publish_main._aws_observations(client, period, now)

    out = io.StringIO()
    code = publish_main.main(["--period", "2026-08"], collectors={"aws-cost-explorer": collect},
                             environ={ENV_DIR: str(tmp_path)}, stdout=out, now=CLOSED)
    assert code == 0
    summary = json.loads(out.getvalue())
    assert summary["missing_sources"] == []
    store = CostLedgerStore(tmp_path)
    info = publisher.publication_info(store, summary["publication_id"])
    assert info["physical_usage"] == {
        CODEBUILD_ID: {"quantity": "150.5", "unit": "build-minutes", "coverage": "complete"},
        EC2_ID: {"quantity": "730.25", "unit": "instance-hours", "coverage": "complete"},
    }
    rows = {r.resource_id: r.resource_period for r in store.read_publication(summary["publication_id"])}
    assert rows[CODEBUILD_ID].gross_cost_usd == Decimal("12.34")
    assert rows[EC2_ID].gross_cost_usd == Decimal("5.00")


def test_a_failed_quantities_fetch_is_missing_and_costs_still_publish(tmp_path, capsys):
    from cost_meter import publish_main, publisher
    from cost_meter.store import CostLedgerStore, ENV_DIR

    cost_page = _page([_group("AWS CodeBuild", "Usage", "12.34"), _group(EC2, "Usage", "5.00")])
    clients = []

    def collect(period, now):
        client = RoutingCostExplorer([cost_page], quantity_error=RuntimeError("AccessDenied"))
        clients.append(client)
        return publish_main._aws_observations(client, period, now)

    out = io.StringIO()
    code = publish_main.main(["--period", "2026-08"], collectors={"aws-cost-explorer": collect},
                             environ={ENV_DIR: str(tmp_path)}, stdout=out, now=CLOSED)
    assert code == 0
    assert clients[0].metrics == [["UnblendedCost"], ["UsageQuantity"]]
    summary = json.loads(out.getvalue())
    assert summary["missing_sources"] == ["aws-usage-quantities"]
    assert summary["resources"] == 2
    store = CostLedgerStore(tmp_path)
    rows = {r.resource_id: r.resource_period for r in store.read_publication(summary["publication_id"])}
    assert rows[CODEBUILD_ID].gross_cost_usd == Decimal("12.34")
    assert rows[EC2_ID].gross_cost_usd == Decimal("5.00")
    info = publisher.publication_info(store, summary["publication_id"])
    assert info["missing_sources"] == ["aws-usage-quantities"]
    assert info["sources"] == ["aws-cost-explorer"]
    assert info["physical_usage"] == {}
    assert "aws-usage-quantities failed: RuntimeError: AccessDenied" in capsys.readouterr().err


def _load_script():
    spec = importlib.util.spec_from_file_location("collect_cost_aws", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_collector_prints_json_lines_or_appends_to_the_env_file(tmp_path):
    script = _load_script()
    pages = [_page([_group("AWS CodeBuild", "Usage", "2.00"), _group("AWS Lambda", "Usage", "0.40")])]
    out = io.StringIO()
    code = script.main(["--period", "2026-08"], client=FakeCostExplorer(pages), environ={},
                       stdout=out, now=CLOSED)
    assert code == 0
    lines = [json.loads(line) for line in out.getvalue().splitlines()]
    assert [o["resource_id"] for o in lines] == ["aws:aws-codebuild", "aws:aws-lambda"]

    target = tmp_path / "observations.jsonl"
    target.write_text('{"kind":"usage"}\n', encoding="utf-8")
    silent = io.StringIO()
    code = script.main(["--period", "2026-08"], client=FakeCostExplorer(pages),
                       environ={script.ENV_OUTPUT: str(target)}, stdout=silent, now=CLOSED)
    assert code == 0 and silent.getvalue() == ""
    written = target.read_text(encoding="utf-8").splitlines()
    assert written[0] == '{"kind":"usage"}'
    assert [json.loads(line) for line in written[1:]] == lines

    assert script.main(["--period", "2026-13"], client=FakeCostExplorer(pages), environ={},
                       stdout=io.StringIO(), now=CLOSED) == 2
    assert script.main(["--period", "2026-08"], client=EndlessCostExplorer(), environ={},
                       stdout=io.StringIO(), now=CLOSED) == 1
