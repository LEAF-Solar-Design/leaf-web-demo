"""Database-free price book, monthly arithmetic, and org boundary contracts."""
import copy
import hashlib
import json
import os
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal, localcontext

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from leaf_platform import api, billing_statement as statements, pricebook
from leaf_platform.tenant_cost_store import ADDONS

ORG = uuid.UUID("7b51687d-2d39-47ac-b598-54f3d1fd9e22")
PERIOD = "2026-10"


@pytest.fixture(scope="session", autouse=True)
def _migrate():
    """These tests never require a database, including on configured hosts."""
    yield


@pytest.fixture
def book():
    return pricebook.load_pricebook(pricebook.DEFAULT_FILE)


def event(**changes):
    return {"addon": "aps_solve", "mode": "managed", "unit": "seconds",
            "quantity": Decimal("2"), "cost_usd": Decimal("0.125"), **changes}


def canonical_digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode("utf-8")).hexdigest()


def test_shipped_pricebook_and_digest(book):
    raw = json.loads(pricebook.DEFAULT_FILE.read_text(encoding="utf-8"))
    assert book == {**raw, "digest": canonical_digest(raw)}
    assert book["placeholder"] is True
    assert book["plans"]["free"]["runs_per_month"] == 1
    assert book["plans"]["platform"] == {"monthly_usd": "0", "runs_per_month": None}
    assert set(book["addons"]) == set(ADDONS)
    assert pricebook.load_pricebook(pricebook.DEFAULT_FILE) == book


@pytest.mark.parametrize("path,value,field", [
    (("schema",), "other", "schema"),
    (("version",), 2, "version"),
    (("version",), True, "version"),
    (("placeholder",), "true", "placeholder"),
    (("management_fee_pct",), 15.0, "management_fee_pct"),
    (("management_fee_pct",), "-1", "management_fee_pct"),
    (("management_fee_pct",), "101", "management_fee_pct"),
    (("management_fee_pct",), "NaN", "management_fee_pct"),
    (("management_fee_pct",), "Infinity", "management_fee_pct"),
    (("management_fee_pct",), "1e1", "management_fee_pct"),
    (("plans", "platform", "monthly_usd"), -1.0, "monthly_usd"),
    (("plans", "platform", "monthly_usd"), "-1", "monthly_usd"),
    (("plans", "platform", "monthly_usd"), "NaN", "monthly_usd"),
    (("plans", "free", "runs_per_month"), -1, "runs_per_month"),
    (("plans", "platform", "runs_per_month"), 1.5, "runs_per_month"),
    (("plans", "free", "runs_per_month"), True, "runs_per_month"),
    (("addons",), {}, "addons"),
    (("addons", "aps_solve", "billable"), 1, "billable"),
    (("extra",), 1, "extra"),
    (("plans", "other"), {}, "other"),
    (("plans", "free", "extra"), 1, "extra"),
    (("plans", "platform", "extra"), 1, "extra"),
    (("addons", "other"), {"billable": True}, "other"),
    (("addons", "aps_solve", "extra"), 1, "extra"),
])
def test_invalid_pricebook_names_field(tmp_path, book, path, value, field):
    raw = copy.deepcopy(book)
    raw.pop("digest")
    target = raw
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    source = tmp_path / "invalid.json"
    source.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValueError, match=field):
        pricebook.load_pricebook(source)


@pytest.mark.parametrize("path", [
    ("management_fee_pct",), ("plans", "free", "runs_per_month"),
    ("plans", "platform", "monthly_usd"), ("addons", "aps_solve", "billable"),
])
def test_missing_pricebook_fields(tmp_path, book, path):
    raw = copy.deepcopy(book)
    raw.pop("digest")
    target = raw
    for key in path[:-1]:
        target = target[key]
    del target[path[-1]]
    source = tmp_path / "missing.json"
    source.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValueError, match=path[-1]):
        pricebook.load_pricebook(source)


def test_pricebook_cache_override_and_invalidation(tmp_path, monkeypatch, book):
    raw = {key: value for key, value in book.items() if key != "digest"}
    source = tmp_path / "prices.json"
    source.write_text(json.dumps(raw), encoding="utf-8")
    monkeypatch.setenv("LEAF_PRICEBOOK_FILE", str(source))
    first = pricebook.load_pricebook()
    original_load = pricebook.json.load
    calls = []

    def counted_load(handle):
        calls.append(handle.name)
        return original_load(handle)

    monkeypatch.setattr(pricebook.json, "load", counted_load)
    first["addons"]["aps_solve"]["billable"] = False
    assert pricebook.load_pricebook() == book
    assert calls == []
    old_mtime = source.stat().st_mtime_ns
    raw["management_fee_pct"] = "20"
    source.write_text(json.dumps(raw), encoding="utf-8")
    os.utime(source, ns=(old_mtime, old_mtime + 1_000_000_000))
    changed = pricebook.load_pricebook()
    assert changed["digest"] != book["digest"]
    assert changed["management_fee_pct"] == "20"
    assert len(calls) == 1


@pytest.mark.parametrize("fee", ["0", "100"])
def test_fee_boundaries_and_unlimited_free(tmp_path, book, fee):
    raw = {key: value for key, value in book.items() if key != "digest"}
    raw["management_fee_pct"] = fee
    raw["plans"]["free"]["runs_per_month"] = None
    source = tmp_path / "boundary.json"
    source.write_text(json.dumps(raw), encoding="utf-8")
    assert pricebook.load_pricebook(source)["management_fee_pct"] == fee


def test_decimal_fee_rounding_and_line_totals(book):
    result = statements.compute_statement([event(), event(addon="storage")], ORG, PERIOD, book)
    for line in result["lines"]:
        assert line["cost_usd"] == "0.13"
        assert line["fee_usd"] == "0.02"
        assert line["total_usd"] == "0.15"
    assert result["totals"] == {"cost_usd": "0.26", "fee_usd": "0.04", "total_usd": "0.30"}
    half = statements.compute_statement([event(cost_usd=Decimal("0.10"))], ORG, PERIOD, book)
    assert half["lines"][0]["fee_usd"] == "0.02"


def test_byo_usage_has_zero_money(book):
    line = statements.compute_statement([event(mode="byo")], ORG, PERIOD, book)["lines"][0]
    assert line["quantity_by_unit"] == {"seconds": "2"}
    assert {line[field] for field in ("cost_usd", "fee_usd", "total_usd")} == {"0.00"}


def test_nonbillable_has_cost_without_fee(book):
    book["addons"]["aps_solve"]["billable"] = False
    line = statements.compute_statement([event()], ORG, PERIOD, book)["lines"][0]
    assert (line["cost_usd"], line["fee_usd"], line["total_usd"]) == ("0.13", "0.00", "0.13")


def test_units_order_and_digest(book):
    events = [event(addon="storage"), event(mode="byo"), event(),
              event(unit="requests", quantity=Decimal("3.25")), event(quantity=Decimal("0.5"))]
    result = statements.compute_statement(events, ORG, PERIOD, book)
    assert [(line["addon"], line["mode"]) for line in result["lines"]] == [
        ("aps_solve", "byo"), ("aps_solve", "managed"), ("storage", "managed")]
    assert result["lines"][1]["quantity_by_unit"] == {"requests": "3.25", "seconds": "2.5"}
    assert result["lines"][1]["events"] == 3
    assert result == statements.compute_statement(reversed(events), ORG, PERIOD, book)
    assert result["digest"] == canonical_digest({k: v for k, v in result.items() if k != "digest"})
    for index in range(len(events)):
        changed = copy.deepcopy(events)
        changed[index]["quantity"] += Decimal("1")
        assert statements.compute_statement(changed, ORG, PERIOD, book)["digest"] != result["digest"]


def test_empty_month(book):
    result = statements.compute_statement([], ORG, PERIOD, book)
    assert result["lines"] == []
    assert set(result["totals"].values()) == {"0.00"}
    assert result["pricebook_version"] == book["version"]
    assert result["pricebook_digest"] == book["digest"]
    assert result["placeholder"] is True


def test_decimal_context_does_not_change_statement(book):
    events = [event(quantity=Decimal("123456789012345678901234567890.123456"))]
    expected = statements.compute_statement(events, ORG, PERIOD, book)
    with localcontext() as context:
        context.prec = 6
        assert statements.compute_statement(events, ORG, PERIOD, book) == expected


@pytest.mark.parametrize("period", ["2026-00", "2026-13", "2026-1", "26-01", "2026-10\n", "0000-01"])
def test_invalid_period(period, book):
    with pytest.raises(ValueError, match="period"):
        statements.compute_statement([], ORG, period, book)


def test_build_refuses_full_limit(monkeypatch, book):
    monkeypatch.setattr(statements, "list_cost_events", lambda *args, **kwargs: [event()] * statements.MAX_LIST_LIMIT)
    with pytest.raises(ValueError, match="^statement window exceeds the event limit$"):
        statements.build_statement(object(), ORG, PERIOD, book)


@pytest.mark.parametrize("period,start,end", [
    ("2024-02", (2024, 2, 1), (2024, 3, 1)),
    ("2026-12", (2026, 12, 1), (2027, 1, 1)),
])
def test_build_utc_month_window(monkeypatch, book, period, start, end):
    conn = object()
    rows = [event()]

    def read(actual_conn, org, lower, upper, *, limit):
        assert actual_conn is conn and org == ORG
        assert lower == datetime(*start, tzinfo=timezone.utc)
        assert upper == datetime(*end, tzinfo=timezone.utc)
        assert limit == statements.MAX_LIST_LIMIT
        return rows

    monkeypatch.setattr(statements, "list_cost_events", read)
    assert statements.build_statement(conn, ORG, period, book) == statements.compute_statement(rows, ORG, period, book)


@pytest.fixture
def route(monkeypatch, book):
    state = {"known": True, "lookup": [], "build": [], "connections": []}
    conn = object()

    def lookup(org):
        state["lookup"].append(org)
        return object() if state["known"] else None

    @contextmanager
    def connection():
        state["connections"].append(conn)
        yield conn

    def build(actual_conn, org, period, prices):
        assert actual_conn is conn and org == ORG and prices == book
        state["build"].append(period)
        if "error" in state:
            raise ValueError(state["error"])
        return statements.compute_statement([event()], org, period, prices)

    monkeypatch.setattr(api.store, "get_org", lookup)
    monkeypatch.setattr(api.db, "connection", connection)
    monkeypatch.setattr(api, "load_pricebook", lambda: book)
    monkeypatch.setattr(statements, "build_statement", build)
    app = FastAPI()
    app.include_router(api.router)
    app.dependency_overrides[api.get_org_id] = lambda: ORG
    with TestClient(app) as client:
        yield client, state


def read(client, org=ORG, period=PERIOD):
    return client.get(f"/api/orgs/{org}/billing/statement", params={"period": period})


def test_route_cross_org_is_404_before_lookup(route):
    client, state = route
    response = read(client, uuid.uuid4())
    assert response.status_code == 404
    assert response.json()["detail"] == "org not found"
    assert state["lookup"] == state["build"] == state["connections"] == []


def test_route_unknown_org_is_404(route):
    client, state = route
    state["known"] = False
    response = read(client)
    assert response.status_code == 404
    assert response.json()["detail"] == "org not found"
    assert state["build"] == state["connections"] == []


def test_route_bad_period_is_400(route):
    client, state = route
    response = read(client, period="2026-13")
    assert response.status_code == 400
    assert response.json()["detail"] == statements.PERIOD_MESSAGE
    assert state["build"] == state["connections"] == []


def test_route_full_window_is_409(route):
    client, state = route
    state["error"] = statements.EVENT_LIMIT_MESSAGE
    response = read(client)
    assert response.status_code == 409
    assert response.json()["detail"] == "statement window exceeds the event limit"


def test_route_statement_shape(route, book):
    client, state = route
    response = read(client)
    assert response.status_code == 200
    assert response.json() == {"statement": statements.compute_statement([event()], ORG, PERIOD, book)}
    assert state["build"] == [PERIOD]


def test_route_unrelated_valueerror_is_not_window_conflict(route):
    client, state = route
    state["error"] = "internal problem"
    with pytest.raises(ValueError, match="internal problem"):
        read(client)
