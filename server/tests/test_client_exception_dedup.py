"""One browser failure is ONE client.exception row, whatever the timing.

WHY THIS FILE EXISTS. React 18's development build re-throws a render error
through a synthetic DOM event, so one crash reaches BOTH client emitters: the
global `window.onerror` handler and the ErrorBoundary. Three rounds of
browser-side timing de-duplication tried to collapse that pair before it left
the page, and the merge gate rejected all three for the same reason -- a batch
flush landing between the two emits committed the first row before the second
could suppress it, so the row count became a property of how busy the page
happened to be:

  * emit-then-retract: at 19 queued events the global row is the 20th, the
    batch POSTs on the spot, and the boundary a microtask later has nothing
    left to pull back.
  * hold-pending-then-flush-on-exit: pagehide MUST release every held row or a
    torn-down tab loses its crash, so a crash at tab close queued both anyway.
    Reproduced by the gate: 19 buffered events + global error + pagehide + the
    same Error to the boundary => 21 events, 2 rows.

So the client stopped trying. Both emitters now attach the same `dedup_key`
(web/src/telemetry.js `dedupKeyFor`) and emit immediately, and collapsing the
pair is THIS side's job -- where it is a property of the data and no ordering
can break it.

WHAT IS PROVEN HERE. The three orderings the gate used, each asserted to
resolve to EXACTLY ONE row, plus the negative case that keeps the rule honest:

  1. both twins in ONE body (the ordinary case)
  2. the twins split across TWO bodies by the 20-event flush threshold
  3. the twins split by a pagehide beacon, boundary last
  4. two genuinely distinct failures still make TWO rows

Orderings 2 and 3 are the ones the client could not survive. The door does not
survive them by remembering anything either -- it CANNOT, see the topology note
in routers/telemetry.py -- it survives them by stamping every twin with the
same durable identity, which is a pure function of (session, dedup_key). The
tests below assert that purity directly, because it is the whole load-bearing
claim: a stamp that does not depend on process state cannot disagree between
two ECS tasks.

Run:  cd server && python -m pytest tests/test_client_exception_dedup.py -q
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

SERVER_DIR = Path(__file__).resolve().parent.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import telemetry_sink  # noqa: E402
from routers import telemetry as telemetry_router  # noqa: E402

SESSION = "11111111-2222-3333-4444-555555555555"
KEY = "0004815162342108"          # a well-formed 16-digit dedup key
OTHER_KEY = "0009999888877776"
COMPONENT_STACK = "0000000000000042"


@pytest.fixture(autouse=True)
def _reset_sink(monkeypatch):
    with telemetry_sink._wake:
        telemetry_sink._queue.clear()
        for k in telemetry_sink._stats:
            telemetry_sink._stats[k] = 0
    telemetry_sink._created_tables.clear()
    telemetry_sink._noted.clear()
    telemetry_sink._sdk_checked = None
    with telemetry_router._bucket_lock:
        telemetry_router._buckets.clear()
    # The sink is "enabled" without the SDK and without a flusher thread, so
    # rows stay in the queue where a test can count them.
    monkeypatch.setattr(telemetry_sink, "disabled_reason", lambda: None)
    monkeypatch.setattr(telemetry_sink, "_ensure_flusher", lambda: None)
    yield


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(telemetry_router.router)
    return TestClient(app, raise_server_exceptions=True)


def _global_event(key: str = KEY, message_class: str = "TypeError") -> Dict[str, Any]:
    """What web/src/telemetry.js's window.onerror handler posts."""
    return {
        "event_name": "client.exception",
        "event_type": "exception",
        "labels": {
            "source": "window.onerror",
            "message_class": message_class,
            "message_hash": "0002166136261234",
            "stack_hash": "0000884152034421",
            "route": "app",
            "ua_class": "chrome/desktop",
            "dedup_key": key,
        },
    }


def _boundary_event(key: str = KEY, message_class: str = "TypeError") -> Dict[str, Any]:
    """What ErrorBoundary.jsx posts for the SAME crash: no `source`, and the
    one label the global handler cannot know."""
    return {
        "event_name": "client.exception",
        "event_type": "exception",
        "labels": {
            "message_class": message_class,
            "component_stack_hash": COMPONENT_STACK,
            "dedup_key": key,
        },
    }


def _post(client: TestClient, events: List[Dict[str, Any]], session: str = SESSION):
    return client.post(
        "/api/telemetry", json={"schema_version": "1", "session_id": session, "events": events})


def _queued() -> List[Dict[str, Any]]:
    return list(telemetry_sink._queue)


def _exceptions() -> List[Dict[str, Any]]:
    return [r for r in _queued() if r["event_name"] == "client.exception"]


def _labels(row: Dict[str, Any]) -> Dict[str, Any]:
    return json.loads(row["labels"]) if row.get("labels") else {}


def _durable_rows(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """THE DURABLE RULE, applied to rows that are already stored.

    This is the read-time contract recorded in docs/PLATFORM_TELEMETRY.md --
    one row per (session_id, dedup_key), the row WITHOUT `source` winning the
    tie -- expressed over Python dicts so a test can execute it. It is a pure
    function of stored rows, which is exactly why it holds no matter how many
    processes wrote them or how far apart.

    It deliberately calls the ROUTER's `_dedup_rank` rather than re-deciding
    the winner, so this helper cannot drift from the door it is checking."""
    winners: Dict[tuple, int] = {}
    out: List[Dict[str, Any]] = []
    for row in rows:
        labels = _labels(row)
        identity = telemetry_router._dedup_identity(
            row["event_name"], row["session_id"], labels)
        if identity is None:
            out.append(row)
            continue
        prior = winners.get(identity)
        if prior is None:
            winners[identity] = len(out)
            out.append(row)
        elif telemetry_router._dedup_rank(labels) < telemetry_router._dedup_rank(
                _labels(out[prior])):
            out[prior] = row
    return out


# --------------------------------------------------------------------------- #
# the three orderings the merge gate used
# --------------------------------------------------------------------------- #

def test_ordering_1_both_twins_in_one_body_persist_exactly_one_row():
    """The ordinary case: both emits ride the same 20-event batch.

    EXACTLY ONE row is queued -- not "at most one", not "one after a later
    pass". The duplicate never reaches the sink at all."""
    c = _client()
    resp = _post(c, [_global_event(), _boundary_event()])

    assert resp.status_code == 202
    rows = _exceptions()
    assert len(rows) == 1, f"expected exactly 1 stored row, got {len(rows)}"
    labels = _labels(rows[0])
    assert "source" not in labels, "the boundary row must win: it carries the stack"
    assert labels["component_stack_hash"] == COMPONENT_STACK
    assert labels["dedup_key"] == KEY
    # The merged-away event still counts as accepted: the door took it and
    # resolved it onto a row that is already there. A client must never read a
    # drop into a successful merge.
    assert resp.json()["accepted"] == 2


def test_ordering_2_the_twenty_event_flush_boundary_still_yields_one_row():
    """The counterexample that killed emit-then-retract.

    Nineteen buffered events put the global row at the batch threshold, so it
    POSTs alone and the boundary follows in a SEPARATE body. The door sees two
    unrelated requests and cannot compare them -- so it stamps both with one
    durable identity, and the stored pair resolves to one row."""
    c = _client()
    filler = [{"event_name": "site.demo_viewed", "event_type": "custom_event"}
              for _ in range(19)]

    first = _post(c, filler + [_global_event()])     # the flushed batch of 20
    second = _post(c, [_boundary_event()])           # one microtask later

    assert first.status_code == second.status_code == 202
    stored = _exceptions()
    assert len(stored) == 2, "both twins are stored: the door never saw them together"
    assert stored[0]["_insert_id"] == stored[1]["_insert_id"], (
        "twins split across bodies must still carry ONE durable identity")

    surviving = [r for r in _durable_rows(_queued()) if r["event_name"] == "client.exception"]
    assert len(surviving) == 1, f"expected exactly 1 surviving row, got {len(surviving)}"
    assert "source" not in _labels(surviving[0])
    assert _labels(surviving[0])["component_stack_hash"] == COMPONENT_STACK
    # Nothing else was lost paying for it.
    assert len([r for r in _queued() if r["event_name"] == "site.demo_viewed"]) == 19


def test_ordering_3_pagehide_with_nineteen_buffered_still_yields_one_row():
    """The exact sequence the gate reproduced to BLOCK round four.

    Nineteen buffered events, a global error, a pagehide beacon carrying that
    whole buffer, and only THEN the same Error reaching the boundary in a later
    request. Holding died here because pagehide has to release what it holds or
    a torn-down tab loses the crash. The gate counted 21 events and 2 rows;
    this asserts 21 events and ONE surviving exception row."""
    c = _client()
    beaconed = [{"event_name": "site.demo_viewed", "event_type": "custom_event"}
                for _ in range(19)] + [_global_event()]

    _post(c, beaconed)                    # the pagehide beacon
    _post(c, [_boundary_event()])         # the boundary, after the tab went away

    assert len(_queued()) == 21, "every event still lands; nothing is dropped to dedupe"
    surviving = [r for r in _durable_rows(_queued()) if r["event_name"] == "client.exception"]
    assert len(surviving) == 1, f"expected exactly 1 surviving row, got {len(surviving)}"
    assert "source" not in _labels(surviving[0]), "the boundary row is the one kept"


def test_two_distinct_failures_stay_two_rows():
    """The rule has to be able to say NO, or it is just a way of losing crashes.

    Two different failures carry different keys, in one body and across bodies
    alike, and both survive."""
    c = _client()
    _post(c, [_global_event(key=KEY), _boundary_event(key=OTHER_KEY)])
    _post(c, [_global_event(key="0000000000000777", message_class="RangeError")])

    stored = _exceptions()
    assert len(stored) == 3
    surviving = [r for r in _durable_rows(_queued()) if r["event_name"] == "client.exception"]
    assert len(surviving) == 3, f"expected exactly 3 surviving rows, got {len(surviving)}"
    assert len({r["_insert_id"] for r in surviving}) == 3


# --------------------------------------------------------------------------- #
# the property the multi-instance claim rests on
# --------------------------------------------------------------------------- #

def test_the_durable_identity_is_a_pure_function_of_session_and_key():
    """The load-bearing claim, asserted directly.

    This door runs on more than one ECS task -- desired_count is not pinned by
    Terraform, and every rolling deploy runs two tasks at once behind one ALB
    with no session affinity. A per-process memo would therefore de-duplicate
    on a quiet day and silently stop during every deploy.

    The stamp is instead computed from (session_id, dedup_key) and nothing
    else. Here the ENTIRE module state a second process would not share -- the
    token buckets, the sink queue, the created-table set -- is wiped between
    the two requests, and the identity is byte-identical anyway. Two tasks
    cannot disagree about a value neither of them remembers."""
    c = _client()
    _post(c, [_global_event()])
    first = _exceptions()[0]["_insert_id"]

    with telemetry_sink._wake:
        telemetry_sink._queue.clear()
    telemetry_sink._created_tables.clear()
    with telemetry_router._bucket_lock:
        telemetry_router._buckets.clear()

    _post(_client(), [_boundary_event()])
    second = _exceptions()[0]["_insert_id"]

    assert first == second == f"{SESSION}:{KEY}"


def test_the_same_key_in_two_sessions_never_merges():
    """`session_id` is half the identity. Two browsers hitting the identical
    failure in the same time bucket are two crashes."""
    c = _client()
    _post(c, [_global_event()], session=SESSION)
    _post(c, [_global_event()], session="99999999-8888-7777-6666-555555555555")

    surviving = [r for r in _durable_rows(_queued()) if r["event_name"] == "client.exception"]
    assert len(surviving) == 2
    assert len({r["_insert_id"] for r in surviving}) == 2


def test_the_boundary_wins_whichever_order_the_twins_arrive_in():
    """Rank decides the winner, not arrival order, so a body that happened to
    carry the boundary row first keeps the same row as one that did not."""
    for events in ([_global_event(), _boundary_event()],
                   [_boundary_event(), _global_event()]):
        with telemetry_sink._wake:
            telemetry_sink._queue.clear()
        _post(_client(), events)
        stored = _exceptions()
        assert len(stored) == 1
        assert "source" not in _labels(stored[0])
        assert _labels(stored[0])["component_stack_hash"] == COMPONENT_STACK


# --------------------------------------------------------------------------- #
# the rule refuses to merge what it cannot key
# --------------------------------------------------------------------------- #

def test_rows_without_a_key_are_never_merged():
    """A pre-#537 bundle, or a client whose key derivation failed, sends no
    `dedup_key`. Those rows must pass through untouched: merging on a missing
    key would collapse unrelated crashes, which is worse than a duplicate."""
    c = _client()
    keyless = _global_event()
    keyless["labels"].pop("dedup_key")
    _post(c, [keyless, dict(keyless)])

    stored = _exceptions()
    assert len(stored) == 2
    assert all("_insert_id" not in r for r in stored), (
        "no key means no durable identity, so BigQuery must not collapse them either")
    assert len(_durable_rows(_queued())) == 2


def test_a_malformed_key_is_dropped_and_therefore_never_merges():
    """The strict 16-digit rule is what stops this label being a free-text
    hole. `5550142` is a phone number, not a digest: the label is dropped, and
    a dropped key means the row is simply not de-duplicable."""
    c = _client()
    bad_a, bad_b = _global_event(), _boundary_event()
    bad_a["labels"]["dedup_key"] = "5550142"
    bad_b["labels"]["dedup_key"] = "5550142"
    _post(c, [bad_a, bad_b])

    stored = _exceptions()
    assert len(stored) == 2
    for row in stored:
        assert "dedup_key" not in _labels(row)
        assert "_insert_id" not in row


def test_a_client_may_not_smuggle_free_text_through_the_key():
    """The same schema rule, from the direction an attacker would take it: the
    door is the open internet for this event (it is on the pre-auth
    allowlist), and a merge key that accepted arbitrary text would be both a
    data leak and a way to collapse other people's rows."""
    c = _client()
    ev = _global_event()
    ev["labels"]["dedup_key"] = "owner@example.com"
    _post(c, [ev])

    (row,) = _exceptions()
    assert "dedup_key" not in _labels(row)
    assert "owner@example.com" not in json.dumps(row)


# --------------------------------------------------------------------------- #
# the transport key is transport, not data
# --------------------------------------------------------------------------- #

def test_the_insert_id_is_stripped_before_the_row_reaches_bigquery():
    """`_insert_id` is a streaming-API argument, not a column. It must leave the
    row on the way out, and arrive as `row_ids` -- otherwise it is an unknown
    field riding into the table AND the de-duplication never happens."""
    seen: Dict[str, Any] = {}

    class _FakeClient:
        project = "fake-project"   # _table_id reads it

        def insert_rows_json(self, table, rows, row_ids=None, **kwargs):
            seen["rows"] = [dict(r) for r in rows]
            seen["row_ids"] = list(row_ids) if row_ids is not None else None
            return []

    telemetry_sink._client = _FakeClient()
    # Pre-seed the day memo so _ensure_table short-circuits: table creation is
    # not what this asserts.
    telemetry_sink._created_tables.add(time.strftime("%Y%m%d", time.gmtime()))
    try:
        _post(_client(), [_global_event(), {"event_name": "site.demo_viewed",
                                            "event_type": "custom_event"}])
        batch = list(telemetry_sink._queue)
        telemetry_sink._queue.clear()
        telemetry_sink._flush_batch(batch)
    finally:
        telemetry_sink._client = None

    assert seen["row_ids"] == [f"{SESSION}:{KEY}", None], (
        "the exception row carries a stable id; an ordinary event carries none")
    for row in seen["rows"]:
        assert telemetry_sink._INSERT_ID_FIELD not in row


def test_a_batch_that_asks_for_no_ids_keeps_the_original_call_shape():
    """Every other emitter in the platform is untouched by this: a batch with
    no stable ids passes `row_ids=None`, exactly as before."""
    seen: Dict[str, Any] = {}

    class _FakeClient:
        project = "fake-project"   # _table_id reads it

        def insert_rows_json(self, table, rows, row_ids=None, **kwargs):
            seen["row_ids"] = row_ids
            return []

    telemetry_sink._client = _FakeClient()
    # Pre-seed the day memo so _ensure_table short-circuits: table creation is
    # not what this asserts.
    telemetry_sink._created_tables.add(time.strftime("%Y%m%d", time.gmtime()))
    try:
        telemetry_sink.emit("job.terminal", tenant_id="t", tenant_kind="account",
                            session_id="s")
        batch = list(telemetry_sink._queue)
        telemetry_sink._queue.clear()
        telemetry_sink._flush_batch(batch)
    finally:
        telemetry_sink._client = None

    assert seen["row_ids"] is None
