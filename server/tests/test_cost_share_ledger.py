"""TCM-01 resource share ledger core: exact shares, append-only revisions, frozen publications.

Transparency of Leaf's real cost, never billing. Hermetic: every store lives in
tmp_path, and LEAF_COST_LEDGER_DIR is set or cleared per test.
"""
from __future__ import annotations

import json
import sys
from decimal import Decimal
from pathlib import Path

import pytest

SERVER_DIR = Path(__file__).resolve().parent.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

from cost_meter import ledger, store  # noqa: E402
from cost_meter.ledger import (  # noqa: E402
    ESTIMATED, MEASURED, ResourcePeriod, ShareEntry, compute_shares, implied_cost,
)
from cost_meter.store import CostLedgerStore  # noqa: E402

PERIOD = "2026-09"


def _rp(resource_id="aws:codebuild", total="100", gross="50.00", credits="0",
        unit="build-minutes", coverage="complete", batches=("batch-1",)):
    return ResourcePeriod(resource_id=resource_id, period=PERIOD, unit=unit, total_usage=total,
                          gross_cost_usd=gross, credits_usd=credits,
                          source_batch_ids=batches, coverage=coverage)


def _by_key(shares):
    return {s.key: s for s in shares}


def _sum(shares):
    return sum((s.usage_share for s in shares), Decimal(0))


def test_two_tenants_and_leaf_on_a_measured_resource_match_usage_and_sum_to_one():
    rp = _rp(resource_id="aps:engine", unit="engine-seconds", total="1000")
    shares = compute_shares(rp, {("tenant-a", ""): Decimal("500"), ("tenant-b", ""): "300",
                                 ("leaf", "development"): 200}, MEASURED)
    got = _by_key(shares)
    assert set(got) == {("tenant-a", ""), ("tenant-b", ""), ("leaf", "development")}
    assert got[("tenant-a", "")].usage_share == Decimal("0.500000000000")
    assert got[("tenant-b", "")].usage_share == Decimal("0.300000000000")
    assert got[("leaf", "development")].usage_share == Decimal("0.200000000000")
    assert got[("tenant-b", "")].participant_usage == Decimal("300")
    assert all(s.status == MEASURED for s in shares)
    assert _sum(shares) == Decimal("1.000000000000")
    assert all(s.usage_share.as_tuple().exponent == -12 for s in shares)


def test_ci_resource_used_only_by_leaf_gives_leaf_ci_the_whole_share():
    shares = compute_shares(_rp(total="42"), {("leaf", "ci"): Decimal("42")}, MEASURED)
    assert len(shares) == 1
    assert shares[0].key == ("leaf", "ci")
    assert shares[0].usage_share == Decimal("1")
    assert shares[0].status == MEASURED


def test_unattributed_remainder_lands_on_leaf_unattributed():
    shares = compute_shares(_rp(total="10"), {("tenant-a", ""): "4", ("leaf", "fleet"): "1"}, MEASURED)
    got = _by_key(shares)
    assert got[("leaf", "unattributed")].participant_usage == Decimal("5")
    assert got[("leaf", "unattributed")].usage_share == Decimal("0.5")
    assert got[("tenant-a", "")].usage_share == Decimal("0.4")
    assert _sum(shares) == Decimal(1)


@pytest.mark.parametrize("total", [None, "0"])
def test_zero_or_unknown_total_is_all_leaf_unattributed_estimated(total):
    shares = compute_shares(_rp(total=total), {("tenant-a", ""): "0"}, MEASURED)
    assert [(s.key, s.usage_share, s.status) for s in shares] == [
        (("leaf", "unattributed"), Decimal("1.000000000000"), ESTIMATED)]
    none_supplied = compute_shares(_rp(total="7"), {}, MEASURED)
    assert [(s.key, s.usage_share, s.status) for s in none_supplied] == [
        (("leaf", "unattributed"), Decimal("1"), ESTIMATED)]


def test_largest_remainder_three_equal_users_sum_to_exactly_one():
    shares = compute_shares(_rp(total="3"), {("tenant-c", ""): 1, ("tenant-a", ""): 1,
                                             ("tenant-b", ""): 1}, MEASURED)
    assert _sum(shares) == Decimal("1.000000000000")
    values = sorted(s.usage_share for s in shares)
    assert values == [Decimal("0.333333333333"), Decimal("0.333333333333"), Decimal("0.333333333334")]
    # Deterministic: the tie goes to the first participant key, every time.
    assert _by_key(shares)[("tenant-a", "")].usage_share == Decimal("0.333333333334")
    again = compute_shares(_rp(total="3"), [(("tenant-b", ""), 1), (("tenant-a", ""), 1),
                                            (("tenant-c", ""), 1)], MEASURED)
    assert [s.to_dict() for s in again] == [s.to_dict() for s in shares]


def test_usage_above_total_is_refused():
    with pytest.raises(ValueError, match="exceeds"):
        compute_shares(_rp(total="5"), {("tenant-a", ""): "4", ("tenant-b", ""): "2"}, MEASURED)


def test_late_correction_keeps_history_and_an_earlier_publication_reads_the_old_revision(
        tmp_path, monkeypatch):
    monkeypatch.setenv(store.ENV_DIR, str(tmp_path))
    s = CostLedgerStore()
    assert s.enabled
    rp1 = _rp(total="100")
    r1 = s.append_revision(rp1, compute_shares(rp1, {("tenant-a", ""): "60"}, MEASURED), "first close")
    other = _rp(resource_id="vendor:vercel", unit="active-days", total="30", gross="20")
    o1 = s.append_revision(other, compute_shares(other, {("leaf", "development"): "30"}, MEASURED),
                           "first close")
    pub = s.publish(PERIOD)

    rp2 = _rp(total="100", gross="55.00", batches=("batch-1", "batch-2"))
    r2 = s.append_revision(rp2, compute_shares(rp2, {("tenant-a", ""): "70"}, MEASURED),
                           "late invoice line", supersedes=r1)
    assert r2 != r1
    hist = s.history("aws:codebuild", PERIOD)
    assert [h.revision_id for h in hist] == [r1, r2]
    assert [h.revision for h in hist] == [1, 2]
    assert hist[1].supersedes == r1
    assert s.current("aws:codebuild", PERIOD).revision_id == r2

    frozen = {r.resource_id: r for r in s.read_publication(pub)}
    assert frozen["aws:codebuild"].revision_id == r1
    assert frozen["aws:codebuild"].resource_period.gross_cost_usd == Decimal("50.00")
    assert frozen["vendor:vercel"].revision_id == o1

    pub2 = s.publish(PERIOD)
    assert pub2 != pub
    assert {r.revision_id for r in s.read_publication(pub2)} == {r2, o1}
    assert {r.revision_id for r in s.read_publication(pub)} == {r1, o1}

    with pytest.raises(ValueError, match="not the current revision"):
        s.append_revision(rp1, compute_shares(rp1, {("tenant-a", ""): "10"}, MEASURED),
                          "stale correction", supersedes=r1)


def test_reappending_identical_content_adds_nothing(tmp_path):
    s = CostLedgerStore(tmp_path)
    rp = _rp(total="10")
    shares = compute_shares(rp, {("tenant-a", ""): "10"}, MEASURED)
    first = s.append_revision(rp, shares, "close")
    path = tmp_path / "revisions" / f"{PERIOD}.jsonl"
    before = path.read_bytes()
    same = _rp(total="10.0", gross="50.0")  # same values, different spelling
    again = s.append_revision(same, compute_shares(same, {("tenant-a", ""): "10.00"}, MEASURED),
                              "re-run of the same batch")
    assert again == first
    assert path.read_bytes() == before
    assert len(s.history("aws:codebuild", PERIOD)) == 1


@pytest.mark.parametrize("where", ["usage", "total", "gross"])
def test_float_inputs_are_rejected(where):
    with pytest.raises(TypeError, match="float"):
        if where == "usage":
            compute_shares(_rp(), {("tenant-a", ""): 1.5}, MEASURED)
        elif where == "total":
            _rp(total=100.0)
        else:
            _rp(gross=9.99)


def test_negative_usage_is_rejected():
    with pytest.raises(ValueError, match=">= 0"):
        compute_shares(_rp(), {("tenant-a", ""): "-1"}, MEASURED)
    with pytest.raises(ValueError, match=">= 0"):
        _rp(total="-5")


def test_duplicate_participants_are_rejected(tmp_path):
    with pytest.raises(ValueError, match="duplicate"):
        compute_shares(_rp(), [(("tenant-a", ""), "1"), (("tenant-a", ""), "2")], MEASURED)
    dup = [ShareEntry("tenant-a", "", Decimal(1), Decimal("0.5"), MEASURED),
           ShareEntry("tenant-a", "", Decimal(1), Decimal("0.5"), MEASURED)]
    with pytest.raises(ValueError, match="duplicate"):
        CostLedgerStore(tmp_path).append_revision(_rp(), dup, "bad")
    with pytest.raises(ValueError):
        compute_shares(_rp(), {("tenant-a", "ci"): "1"}, MEASURED)  # a tenant has no dimension
    with pytest.raises(ValueError):
        compute_shares(_rp(), {("leaf", ""): "1"}, MEASURED)  # leaf always has one


def test_append_refuses_shares_that_do_not_sum_to_one(tmp_path):
    s = CostLedgerStore(tmp_path)
    short = [ShareEntry("tenant-a", "", None, Decimal("0.5"), MEASURED),
             ShareEntry("leaf", "ci", None, Decimal("0.499999999999"), MEASURED)]
    with pytest.raises(ValueError, match="exactly 1"):
        s.append_revision(_rp(), short, "bad")
    with pytest.raises(ValueError):
        ShareEntry("tenant-a", "", None, Decimal("1.5"), MEASURED)
    assert s.history("aws:codebuild", PERIOD) == []


def test_implied_cost_rounds_only_at_display():
    rp = _rp(total="3", gross="100.00", credits="10.005")
    shares = compute_shares(rp, {("tenant-a", ""): 1, ("tenant-b", ""): 1, ("tenant-c", ""): 1}, MEASURED)
    assert rp.gross_cost_usd == Decimal("100.00") and rp.credits_usd == Decimal("10.005")
    costs = {s.key: implied_cost(s, rp) for s in shares}
    assert costs[("tenant-a", "")].gross_usd == Decimal("33.33")
    assert costs[("tenant-b", "")].gross_usd == Decimal("33.33")
    # 10.005 x 0.333333333334 = 3.33500..., 10.005 x 0.333333333333 = 3.33499...: cents come from
    # the exact product, not from a pre-rounded share or a pre-rounded credit.
    assert costs[("tenant-a", "")].credits_usd == Decimal("3.34")
    assert costs[("tenant-b", "")].credits_usd == Decimal("3.33")
    # The stored share keeps all 12 places; only the display figure is in cents.
    assert _by_key(shares)[("tenant-a", "")].usage_share == Decimal("0.333333333334")
    assert costs[("tenant-a", "")].gross_usd.as_tuple().exponent == -2


def test_disabled_store_readers_return_empty_and_writers_refuse(monkeypatch):
    monkeypatch.delenv(store.ENV_DIR, raising=False)
    s = CostLedgerStore()
    assert not s.enabled
    assert s.history("aws:codebuild", PERIOD) == []
    assert s.current("aws:codebuild", PERIOD) is None
    assert s.read_publication(f"pub-{PERIOD}-{'0' * 20}") == []
    rp = _rp()
    with pytest.raises(store.LedgerStoreDisabled):
        s.append_revision(rp, compute_shares(rp, {}, ESTIMATED), "x")


def test_a_torn_or_malformed_line_is_skipped_and_counted_never_guessed(tmp_path):
    s = CostLedgerStore(tmp_path)
    rp = _rp(total="10")
    r1 = s.append_revision(rp, compute_shares(rp, {("tenant-a", ""): "10"}, MEASURED), "close")
    path = tmp_path / "revisions" / f"{PERIOD}.jsonl"
    with open(path, "ab") as fh:
        fh.write(b'{"schema": "leaf.cost-share-revision.v1", "revision_id": "aws:codebu')
    assert [r.revision_id for r in s.history("aws:codebuild", PERIOD)] == [r1]
    assert s.malformed_lines == 1

    rp2 = _rp(total="20")
    r2 = s.append_revision(rp2, compute_shares(rp2, {("tenant-a", ""): "10"}, MEASURED), "fix")
    assert [r.revision_id for r in s.history("aws:codebuild", PERIOD)] == [r1, r2]
    assert s.malformed_lines == 1

    # A line whose content no longer matches its digest is refused, not believed.
    lines = path.read_bytes().split(b"\n")
    tampered = json.loads(lines[0])
    tampered["shares"][0]["participant_usage"] = "9"
    with open(path, "ab") as fh:
        fh.write(json.dumps(tampered).encode() + b"\n")
    assert len(s.history("aws:codebuild", PERIOD)) == 2
    assert s.malformed_lines == 2


def test_decimals_serialize_as_strings(tmp_path):
    s = CostLedgerStore(tmp_path)
    rp = _rp(total="100", gross="12.345678", credits="1.5")
    s.append_revision(rp, compute_shares(rp, {("tenant-a", ""): "25"}, MEASURED), "close")
    line = json.loads((tmp_path / "revisions" / f"{PERIOD}.jsonl").read_bytes().splitlines()[0])
    assert line["resource_period"]["gross_cost_usd"] == "12.345678"
    assert line["resource_period"]["total_usage"] == "100"
    assert line["resource_period"]["credits_usd"] == "1.5"
    shares = {(e["participant_id"], e["dimension"]): e for e in line["shares"]}
    assert shares[("tenant-a", "")]["usage_share"] == "0.250000000000"
    assert shares[("leaf", "unattributed")]["participant_usage"] == "75"
    assert all(isinstance(v, str) for e in line["shares"] for v in
               (e["usage_share"], e["participant_usage"]))
    assert ledger.ResourcePeriod.from_dict(line["resource_period"]) == rp
