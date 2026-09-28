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
