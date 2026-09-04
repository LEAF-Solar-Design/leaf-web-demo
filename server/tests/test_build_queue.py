"""
The build queue record, server side (slice 11a): the SHARED mapping cases the
JS mirror is pinned to, the two-stage terminal rules, and the fail-closed
validator. Every expected record also validates against
contract/build-queue.v1.schema.json.

Run:  cd server && python -m pytest tests/test_build_queue.py -q
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import jsonschema
import pytest

SERVER_DIR = Path(__file__).resolve().parent.parent
REPO = SERVER_DIR.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import build_queue as bq  # noqa: E402

CASES = json.loads((REPO / "contract" / "build-queue.v1.cases.json").read_text(encoding="utf-8"))["cases"]
SCHEMA = json.loads((REPO / "contract" / "build-queue.v1.schema.json").read_text(encoding="utf-8"))


def _map(case):
    source, given = case["source"], case["input"]
    if source == "broker":
        return bq.from_broker_job(given)
    if source == "fold":
        return bq.from_fold_state(given["state"], given["meta"])
    return bq.from_fleet_task(given)


def test_the_cases_cover_every_lane_with_a_refusal():
    for lane in bq.LANES:
        mine = [c for c in CASES if c["source"] == lane]
        assert len(mine) >= 3, lane
        assert any(c["expected"] is None for c in mine), f"{lane} needs a refusal case"


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_shared_case(case):
    got = _map(case)
    assert got == case["expected"], json.dumps(got, sort_keys=True)
    if case["expected"] is not None:
        jsonschema.validate(got, SCHEMA)
        assert bq.validate_record(got) == case["expected"]


# --------------------------------------------------------------------------- #
# two-stage terminal, never inferred
# --------------------------------------------------------------------------- #
def test_complete_job_is_verified_but_never_promoted_on_its_own():
    rec = bq.from_broker_job({"job_id": "j", "tool": "t", "status": "complete"})
    assert rec["terminal"] == {"verified": True, "promoted": False}


def test_failed_job_is_neither():
    rec = bq.from_broker_job({"job_id": "j", "tool": "t", "status": "failed", "error": {"message": "x"}})
    assert rec["terminal"] == {"verified": False, "promoted": False}


def test_wire_refuses_promoted_without_a_promotion_receipt():
    rec = bq.from_broker_job({"job_id": "j", "tool": "t", "status": "complete"})
    rec["terminal"]["promoted"] = True
    with pytest.raises(bq.BuildQueueError, match="promoted: true without a promotion receipt"):
        bq.validate_record(rec)


def test_fold_verified_only_by_verified_at_under_a_real_oracle():
    state = {"run_id": "r", "rounds": 2, "mission_complete": True, "milestones": {"a": {"status": "done"}}}
    assert bq.from_fold_state(state, {})["terminal"]["verified"] is False
    verified = dict(state, milestones={"a": {"status": "done", "verified_at": "2026-09-04T00:00:00Z"}})
    assert bq.from_fold_state(verified, {})["terminal"]["verified"] is True
    vacuous = dict(verified, mission_complete_vacuous=True)
    assert bq.from_fold_state(vacuous, {})["terminal"]["verified"] is False


def test_fleet_completion_is_evidence_not_a_verdict():
    row = {"task_id": "t", "title": "x", "state": "complete"}
    assert bq.from_fleet_task(row)["terminal"]["verified"] is False
    assert bq.from_fleet_task(dict(row, receipts=[{"kind": "artifact", "ref": "a"}]))["terminal"]["verified"] is False
    assert bq.from_fleet_task(dict(row, receipts=[{"kind": "verification", "ref": "v"}]))["terminal"]["verified"] is True
    assert bq.from_fleet_task(dict(row, receipts=[{"kind": "gate-proof", "ref": "g"}]))["terminal"]["verified"] is True


def test_promotion_artifacts_by_schema_and_dispatch_not_by_key():
    ok = {"prewarm_relay": {"schema": bq.PREWARM_RELAY_SCHEMA, "relay_run_id": 1, "dispatched": [{}]}}
    assert bq.promotion_receipt(ok)["ref"] == f"{bq.PREWARM_RELAY_SCHEMA}#1"
    assert bq.promotion_receipt({"prewarm_relay": {"schema": "leaf.staging-prewarm-relay.v0", "relay_run_id": 1, "dispatched": [{}]}}) is None
    assert bq.promotion_receipt({"prewarm_relay": {"schema": bq.PREWARM_RELAY_SCHEMA, "relay_run_id": 1, "dispatched": []}}) is None
    assert bq.promotion_receipt({"app_store_connect_result": {"status": "failed", "build_id": "1"}}) is None
    assert bq.promotion_receipt({"app_store_connect_result": {"status": "succeeded"}}) is None
    assert bq.promotion_receipt({"app_store_connect_result": {"status": "succeeded", "build_id": "7"}})["ref"] == "app_store_connect#7"
    assert bq.promotion_receipt({"promotion_stage": {"status": "pending", "ref": "x"}}) is None
    assert bq.promotion_receipt({"promotion_stage": {"status": "promoted", "ref": "x"}})["ref"] == "x"
    assert bq.promotion_receipt({"promotion": True}) is None
    assert bq.promotion_receipt(None) is None


# --------------------------------------------------------------------------- #
# the validator fails closed
# --------------------------------------------------------------------------- #
def _good():
    return bq.from_broker_job({"job_id": "j-1", "tool": "count-by-layer", "status": "complete",
                               "created_at": 1725400000, "elapsed_ms": 10, "cost": {"usd_est": 0.5}})


@pytest.mark.parametrize("mutate, pattern", [
    (lambda r: r.pop("id"), r"^id:"),
    (lambda r: r.update(id=""), r"^id:"),
    (lambda r: r.update(id="x" * 129), r"^id:"),
    (lambda r: r.update(id=12), r"^id:"),
    (lambda r: r.update(lane="ci"), r"^lane:"),
    (lambda r: r.update(state="complete"), r"^state:"),
    (lambda r: r.update(title=""), r"^title:"),
    (lambda r: r.update(title="x" * 201), r"^title:"),
    (lambda r: r.update(requested_by=""), r"^requested_by:"),
    (lambda r: r.update(requested_by=7), r"^requested_by:"),
    (lambda r: r.update(started="yesterday"), r"^started:"),
    (lambda r: r.update(started=-1), r"^started:"),
    (lambda r: r.update(elapsed_ms=-1), r"^elapsed_ms:"),
    (lambda r: r.update(elapsed_ms="fast"), r"^elapsed_ms:"),
    (lambda r: r.update(elapsed_ms=float("nan")), r"^elapsed_ms:"),
    (lambda r: r.update(estimate_ms=0), r"^estimate_ms:"),
    (lambda r: r.update(cost_usd=-0.5), r"^cost_usd:"),
    (lambda r: r.update(cost_usd=float("inf")), r"^cost_usd:"),
    (lambda r: r.update(cost_usd=True), r"^cost_usd:"),
    (lambda r: r.update(receipts=None), r"^receipts:"),
    (lambda r: r.update(receipts=[{"kind": "terminal", "ref": ""}]), r"^receipts\[0\]"),
    (lambda r: r.update(receipts=[{"kind": "nope", "ref": "x"}]), r"^receipts\[0\]"),
    (lambda r: r.update(receipts=[{"kind": "terminal", "ref": "x", "at": "never"}]), r"^receipts\[0\]"),
    (lambda r: r.update(receipts=[{"kind": "artifact", "ref": "x"}] * 33), r"^receipts: more than"),
    (lambda r: r.update(terminal=None), r"^terminal:"),
    (lambda r: r.update(terminal={"verified": "yes", "promoted": False}), r"^terminal\.verified:"),
    (lambda r: r.update(terminal={"verified": True, "promoted": 0}), r"^terminal\.promoted:"),
    (lambda r: r.update(actions="cancel"), r"^actions:"),
    (lambda r: r.update(actions=["restart"]), r"^actions:"),
    (lambda r: r.update(actions=["retry", "retry"]), r"^actions:"),
    (lambda r: r.update(actions=["cancel", "retry", "promote", "cancel"]), r"^actions:"),
    (lambda r: r.update(status=None), r"^status:"),
    (lambda r: r.update(status={"word": "", "tint": "ok", "detail": None}), r"^status\.word:"),
    (lambda r: r.update(status={"word": "ok", "tint": "green", "detail": None}), r"^status\.tint:"),
    (lambda r: r.update(status={"word": "ok", "tint": "ok", "detail": ""}), r"^status\.detail:"),
])
def test_validate_record_refuses(mutate, pattern):
    rec = _good()
    mutate(rec)
    with pytest.raises(bq.BuildQueueError, match=pattern):
        bq.validate_record(rec)


@pytest.mark.parametrize("bad", [None, 1, "x", [], True])
def test_validate_record_refuses_non_objects(bad):
    with pytest.raises(bq.BuildQueueError, match="record: not an object"):
        bq.validate_record(bad)


def test_validate_record_normalises_timestamps_and_keeps_good_receipts():
    rec = _good()
    rec["started"] = "2026-09-04T00:00:00Z"
    rec["receipts"] = [{"kind": "artifact", "ref": "x"}]
    out = bq.validate_record(rec)
    assert out["started"] == 1788480000000
    assert out["receipts"] == [{"kind": "artifact", "ref": "x", "at": None}]
    assert bq.validate_record(dict(_good(), started=1725400000))["started"] == 1725400000000


# --------------------------------------------------------------------------- #
# the mappers bound what they carry
# --------------------------------------------------------------------------- #
def test_mappers_clip_prose_and_refuse_over_bound_ids():
    long = "x" * 250
    rec = bq.from_fleet_task({"task_id": "t", "title": long, "state": "active", "detail": long, "owner": long})
    assert len(rec["title"]) == 200 and rec["title"].endswith("…")
    assert len(rec["status"]["detail"]) == 200
    assert len(rec["requested_by"]) == 200
    bq.validate_record(rec)
    assert bq.from_fleet_task({"task_id": "x" * 129, "title": "t", "state": "active"}) is None
    assert bq.from_broker_job({"job_id": "x" * 129, "tool": "t", "status": "running"}) is None


def test_mappers_cap_receipts_and_refuse_non_objects():
    many = [{"kind": "artifact", "ref": f"a{i}"} for i in range(42)]
    assert len(bq.from_fleet_task({"task_id": "t", "title": "t", "state": "active", "receipts": many})["receipts"]) == 32
    assert bq.from_fold_state(None, {}) is None
    assert bq.from_fold_state({"run_id": "r"}, "meta") is None
    assert bq.from_fleet_task("x") is None
    assert bq.from_broker_job(None) is None
    assert bq.from_fold_state({"run_id": "r", "rounds": 1, "milestones": ["a"]}, {})["status"]["detail"] is None


def test_helpers():
    assert bq.to_epoch_ms(1725400000) == 1725400000000
    assert bq.to_epoch_ms(1725400000000) == 1725400000000
    assert bq.to_epoch_ms("2026-09-04T00:00:00Z") == 1788480000000
    assert bq.to_epoch_ms(0) is None and bq.to_epoch_ms(-5) is None
    assert bq.to_epoch_ms("soon") is None and bq.to_epoch_ms("x" * 65) is None
    assert bq.to_epoch_ms({}) is None and bq.to_epoch_ms(True) is None
    assert bq.format_cost_usd(0.0042) == "$0.0042"
    assert bq.format_cost_usd(0.0123) == "$0.01"
    assert bq.format_cost_usd(1.5) == "$1.50"
    assert bq.format_cost_usd(0) is None and bq.format_cost_usd(-1) is None and bq.format_cost_usd("abc") is None
    assert bq.parse_receipt({"kind": "terminal", "ref": "r"}) == {"kind": "terminal", "ref": "r", "at": None}
    assert bq.parse_receipt({"kind": "terminal", "ref": "r", "at": "bad"}) is None
    rows = [{"state": s} for s in ("queued", "running", "verifying", "done", "failed")]
    assert bq.running_count(rows) == 3
    assert bq.running_count([None, {"state": "running"}]) == 1
