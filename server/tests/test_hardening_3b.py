"""
Binary acceptance for lane 3B — the single-writer checkout lock's MUTATING routes
(APS_LIVE=0, no operator, no APS).

Boots the real broker + app subprocesses (same harness style as
tests/test_write_loop.py) against an ISOLATED filesystem store (LEAF_STORE_DIR in
tmp) and drives the new endpoints end to end over HTTP:

  * POST   /api/drawings/{id}/checkout {holder?, ttl_s?}  -> take the lock
  * DELETE /api/drawings/{id}/checkout?holder=<h>          -> release the lock

Covers the acceptance matrix:
  1. acquire on a free drawing -> 200 + checkout record (holder/acquired/expires);
  2. a SECOND holder acquiring the held lock -> 409 not-acquired (locked_by X);
     (the lock is per-tenant+drawing; `holder` is the session id, so a second
     HOLDER on the same tenant/drawing is the true single-writer conflict — two
     distinct tenants have separate manifests and correctly never collide);
  3. the same holder re-acquiring REFRESHES (200, not a conflict);
  4. release by the holder -> 200 + cleared state; GET /versions shows it cleared;
  5. release by a NON-holder -> 403 and the lock is left intact;
  6. a read tool run (count-by-layer) is UNAFFECTED by a held lock;
  7. an EXPIRED lock is re-acquirable by a different holder, and GET /versions
     stops publishing it the moment it elapses (its read-side twin);
  8. the default holder is the tenant id (bodyless POST / no-holder DELETE);
  9. releasing when nothing is held is an idempotent 200.

Each test uses its OWN X-Tenant-Id so its bootstrapped `demo` drawing (and its
manifest lock) is isolated — the tests are order-independent.

Run:  cd server && python -m pytest tests/test_hardening_3b.py tests/test_write_loop.py -q
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest
import requests

from _test_readiness import wait_ready
from _test_run_confirmation import confirmed_requests_payload

SERVER_DIR = Path(__file__).resolve().parent.parent
CAP_HEADER = "X-Checkout-Capability"
FORGED_CAP = "lco1." + "f" * 64        # right shape, never minted by this server
WRITE_TOOL = "delete-marked-panel"     # the seeded drawing.write tool (write_tools.json)


# --------------------------------------------------------------------------- #
# process harness (mirrors tests/test_write_loop.py)
# --------------------------------------------------------------------------- #
def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def start_uvicorn(module_app: str, port: int, env_overrides: dict, log_path: Path) -> subprocess.Popen:
    env = dict(os.environ)
    env.update({k: str(v) for k, v in env_overrides.items()})
    log = open(log_path, "ab")
    return subprocess.Popen(
        [sys.executable, "-m", "uvicorn", module_app, "--port", str(port), "--host", "127.0.0.1"],
        cwd=str(SERVER_DIR), env=env, stdout=log, stderr=log,
    )


def stop(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)


@pytest.fixture(scope="module")
def stack(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("hardening3b")
    store_dir = tmp / "drawings"           # shared by broker (writes) + app (reads/lock)
    broker_port, app_port = free_port(), free_port()
    broker = start_uvicorn("broker:app", broker_port,
                           {"BROKER_LEDGER": tmp / "ledger.jsonl",
                            "BROKER_TENANTS": tmp / "tenants.json",
                            "APS_LIVE": "0",
                            "LEAF_STORE_DIR": store_dir},
                           tmp / "broker.log")
    app = start_uvicorn("app:app", app_port,
                        {"APS_LIVE": "0", "APS_CRED": "/nonexistent",
                         "BROKER_URL": f"http://127.0.0.1:{broker_port}",
                         "JOBS_DB": tmp / "jobs.db",
                         "LEAF_STORE_DIR": store_dir},
                        tmp / "app.log")
    try:
        wait_ready(f"http://127.0.0.1:{broker_port}/broker/health", broker,
                   log_path=tmp / "broker.log")
        wait_ready(f"http://127.0.0.1:{app_port}/api/health", app,
                   log_path=tmp / "app.log")
        yield {"app": f"http://127.0.0.1:{app_port}", "broker": f"http://127.0.0.1:{broker_port}"}
    finally:
        stop(app)
        stop(broker)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _h(tenant: str) -> dict:
    return {"X-Tenant-Id": tenant}


def acquire(stack, tenant, drawing="demo", holder=None, ttl_s=None, capability=None):
    body = {}
    if holder is not None:
        body["holder"] = holder
    if ttl_s is not None:
        body["ttl_s"] = ttl_s
    headers = _h(tenant)
    if capability is not None:
        headers = {**headers, CAP_HEADER: capability}
    return requests.post(f"{stack['app']}/api/drawings/{drawing}/checkout",
                         json=body if body else None, headers=headers, timeout=30)


def cap(response):
    """The opaque capability from a successful acquire — the only proof of
    ownership the caller ever receives."""
    return response.json()["checkout_capability"]


def release(stack, tenant, drawing="demo", capability=None):
    headers = _h(tenant)
    if capability is not None:
        headers = {**headers, CAP_HEADER: capability}
    return requests.delete(f"{stack['app']}/api/drawings/{drawing}/checkout",
                           headers=headers, timeout=30)


def undo(stack, tenant, drawing="demo", capability=None):
    headers = _h(tenant)
    if capability is not None:
        headers = {**headers, CAP_HEADER: capability}
    return requests.post(f"{stack['app']}/api/drawings/{drawing}/undo",
                         headers=headers, timeout=30)


def redo(stack, tenant, drawing="demo", capability=None):
    headers = _h(tenant)
    if capability is not None:
        headers = {**headers, CAP_HEADER: capability}
    return requests.post(f"{stack['app']}/api/drawings/{drawing}/redo",
                         headers=headers, timeout=30)


def versions(stack, tenant, drawing="demo"):
    return requests.get(f"{stack['app']}/api/drawings/{drawing}/versions",
                        headers=_h(tenant), timeout=30)


def run_wait(stack, tool, params=None, tenant="wl"):
    headers = _h(tenant)
    return requests.post(f"{stack['app']}/api/run?wait=1",
                         json=confirmed_requests_payload(
                             stack["app"], tool, params, "rooftop_demo",
                             headers=headers),
                         headers=headers, timeout=120)


def _envelope_ok(body: dict) -> None:
    # §10: every response carries error + degraded_mode
    assert "error" in body and "degraded_mode" in body, body


# --------------------------------------------------------------------------- #
# 1. acquire on a free drawing -> 200 + checkout record
# --------------------------------------------------------------------------- #
def test_acquire_on_free_drawing(stack):
    t = "co-free"
    r = acquire(stack, t, holder="sess-a", ttl_s=3600)
    assert r.status_code == 200, r.text
    body = r.json()
    _envelope_ok(body)
    assert body["error"] is None
    assert body["acquired"] is True
    assert body["holder"] == "sess-a"
    co = body["checkout"]
    assert co["holder"] == "sess-a"
    assert co["acquired"] and co["expires"]            # timestamps present

    # the read-only versions surface now reflects the held lock
    v = versions(stack, t).json()
    assert v["checkout"]["holder"] == "sess-a"


# --------------------------------------------------------------------------- #
# 2. a second HOLDER acquiring the held lock -> 409 not-acquired (locked_by X)
# --------------------------------------------------------------------------- #
def test_second_holder_conflict_409(stack):
    t = "co-conflict"
    assert acquire(stack, t, holder="alice").status_code == 200

    r = acquire(stack, t, holder="bob")
    assert r.status_code == 409, r.text
    body = r.json()
    _envelope_ok(body)
    assert body["acquired"] is False
    assert body["locked_by"] == "alice"
    assert body["checkout"]["holder"] == "alice"       # bob sees who holds it
    assert body["error"]["error_code"] == "BAD_PARAMS"
    assert body["error"]["retryable"] is True

    # the lock was NOT stolen — still alice's
    assert versions(stack, t).json()["checkout"]["holder"] == "alice"


# --------------------------------------------------------------------------- #
# 3. the holder re-acquiring REFRESHES — when it can PROVE the lease is its own
# --------------------------------------------------------------------------- #
def test_holder_reacquire_with_its_capability_refreshes(stack):
    t = "co-refresh"
    took = acquire(stack, t, holder="alice", ttl_s=10)
    first = took.json()
    time.sleep(1.1)
    second = acquire(stack, t, holder="alice", ttl_s=3600, capability=cap(took))
    assert second.status_code == 200, second.text
    body = second.json()
    assert body["acquired"] is True
    # the lease was refreshed -> a strictly later expiry than the first grab
    assert body["checkout"]["expires"] > first["checkout"]["expires"]
    # ...and a NEW capability, so the previous generation stops working
    assert cap(second) != cap(took)


# --------------------------------------------------------------------------- #
# 4. release by the holder -> 200 + cleared state
# --------------------------------------------------------------------------- #
def test_release_by_holder_clears(stack):
    t = "co-release"
    took = acquire(stack, t, holder="alice")
    assert took.status_code == 200

    r = release(stack, t, capability=cap(took))
    assert r.status_code == 200, r.text
    body = r.json()
    _envelope_ok(body)
    assert body["released"] is True
    assert body["checkout"] is None

    # versions confirms the lock is gone
    assert versions(stack, t).json()["checkout"] is None


# --------------------------------------------------------------------------- #
# 5. release by anyone who cannot PROVE they hold it -> 403, lock left intact
# --------------------------------------------------------------------------- #
def test_release_without_a_valid_capability_forbidden(stack):
    """Knowing the holder is not holding the lock. The `?holder=` parameter this
    route used to authorize with came straight off the public /versions read, so
    any tenant member could release anyone's lock by naming them."""
    t = "co-nonholder"
    assert acquire(stack, t, holder="alice").status_code == 200

    for label, kwargs in (
        ("no capability at all", {}),
        ("a forged capability", {"capability": FORGED_CAP}),
    ):
        r = release(stack, t, **kwargs)
        assert r.status_code == 403, f"{label}: {r.text}"
        body = r.json()
        _envelope_ok(body)
        assert body["error"]["error_code"] == "BAD_PARAMS"
        # the active lock is untouched — still alice's
        assert versions(stack, t).json()["checkout"]["holder"] == "alice"

    # naming the holder read off the public surface buys nothing either
    published = versions(stack, t).json()["checkout"]["holder"]
    r = requests.delete(f"{stack['app']}/api/drawings/demo/checkout",
                        params={"holder": published}, headers=_h(t), timeout=30)
    assert r.status_code == 403, r.text
    assert versions(stack, t).json()["checkout"]["holder"] == "alice"


# --------------------------------------------------------------------------- #
# 6. a read tool run is UNAFFECTED by a held lock
# --------------------------------------------------------------------------- #
def test_read_tool_unaffected_by_lock(stack):
    t = "co-read"
    assert acquire(stack, t, holder="alice").status_code == 200      # lock held

    e = run_wait(stack, "count-by-layer", {}, tenant=t).json()
    assert e["ok"] is True
    assert e["result"]["counts"]["Panels"] == 2345                   # golden read, lock ignored
    assert "new_version" not in e["result"]                          # read never versions

    # and the lock is still held afterwards (the read did not touch it)
    assert versions(stack, t).json()["checkout"]["holder"] == "alice"


# --------------------------------------------------------------------------- #
# 7. an EXPIRED lock is re-acquirable by a different holder
# --------------------------------------------------------------------------- #
def test_expired_lock_is_reacquirable(stack):
    t = "co-expire"
    assert acquire(stack, t, holder="alice", ttl_s=1).status_code == 200
    time.sleep(1.3)                                                  # let alice's lease expire

    r = acquire(stack, t, holder="bob")
    assert r.status_code == 200, r.text                             # expired lock is free
    body = r.json()
    assert body["acquired"] is True
    assert body["checkout"]["holder"] == "bob"
    assert versions(stack, t).json()["checkout"]["holder"] == "bob"


# --------------------------------------------------------------------------- #
# 7b. GET /versions publishes a LIVE lease and WITHHOLDS an elapsed one
#
# The read-side twin of 7. That test proves the store treats an elapsed lock as
# free; this one proves the read agrees. It did not: `_checkout_view` returned
# None only for an ABSENT record, so a lapsed lease stayed on this surface until
# somebody took or released the lock. Measured on platform-staging 2026-09-02, a
# lease that ended at 01:56:42Z was still reported at 15:46Z — 827 minutes — and
# the CAD rail read "Editing locked by sess-72d58f4d…" the whole time, on a
# drawing nobody held, with write tools suppressed for every viewer.
#
# BOTH states, one clock apart on the SAME drawing. Asserting only the elapsed
# null is satisfied by a route that answers null for every lock, which would take
# the chip away from a live lease too; asserting only the live record is what the
# suite already did when the defect shipped.
# --------------------------------------------------------------------------- #
def test_versions_withholds_an_elapsed_lease_but_publishes_a_live_one(stack):
    t = "co-elapsed-read"
    assert acquire(stack, t, holder="alice", ttl_s=1).status_code == 200

    # LIVE: the record is published, which is what renders "locked by alice".
    live = versions(stack, t).json()["checkout"]
    assert live is not None, "a live lease must still be published"
    assert live["holder"] == "alice"
    assert live["acquired"] and live["expires"]

    time.sleep(1.3)                                                  # the lease elapses

    # ELAPSED: the store re-grants this lock to anyone, so the read must say free.
    assert versions(stack, t).json()["checkout"] is None

    # … and the record is still IN the manifest: the view withheld it, nothing
    # cleared it. A GET that mutated the store would be a worse fix than the bug.
    # `release_checkout` reports False for an absent lock and True when it clears
    # one, and an elapsed lock is releasable with no capability at all, so this
    # distinguishes "withheld" from "erased" over HTTP alone.
    rel = release(stack, t)
    assert rel.status_code == 200, rel.text
    # Reads as the FAILURE branch, which is the only branch a message is printed
    # on: released=False means release_checkout found nothing to clear, so the
    # record was already gone before this call and the GET above is what removed
    # it. released=True (the pass) means the record survived the read.
    assert rel.json()["released"] is True, (
        "released=False: the elapsed record was already gone from the manifest "
        "before this release, so the GET above cleared it")


# --------------------------------------------------------------------------- #
# 8. the default holder is the tenant id (bodyless POST); releasing it still
#    needs the capability that acquire issued
# --------------------------------------------------------------------------- #
def test_default_holder_is_tenant(stack):
    t = "co-default"
    r = acquire(stack, t)                                           # no body -> holder defaults to tenant
    assert r.status_code == 200, r.text
    assert r.json()["checkout"]["holder"] == t

    rel = release(stack, t, capability=cap(r))
    assert rel.status_code == 200, rel.text
    assert rel.json()["released"] is True
    assert rel.json()["checkout"] is None


# --------------------------------------------------------------------------- #
# 9. releasing when nothing is held is an idempotent 200
# --------------------------------------------------------------------------- #
def test_release_when_free_is_idempotent(stack):
    t = "co-idem"
    # bootstrap the demo drawing (a versions read) with NO lock held
    assert versions(stack, t).json()["checkout"] is None

    r = release(stack, t)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["released"] is False                               # nothing was held
    assert body["checkout"] is None


# --------------------------------------------------------------------------- #
# 10. the capability, and only the capability, is proof of ownership
# --------------------------------------------------------------------------- #
def test_versions_never_publishes_the_capability_or_the_generation(stack):
    """The read every tenant member may make must not carry anything that
    authorizes. `holder` stays — the UI's "locked by X" chip needs a name — and
    that is exactly why it cannot be the proof."""
    t = "co-nopublish"
    took = acquire(stack, t, holder="alice")
    assert took.status_code == 200
    issued = cap(took)

    body = versions(stack, t).json()
    assert body["checkout"]["holder"] == "alice"         # display label, still public
    assert "fence" not in body["checkout"], body         # generation withdrawn
    assert issued not in json.dumps(body)                # capability never echoed
    assert "capability" not in json.dumps(body).lower()


def test_a_capability_from_another_tenant_is_refused(stack):
    """Bound to the tenant: two workspaces have separate manifests, and a token
    minted in one must not act in the other."""
    mine, theirs = "co-x-mine", "co-x-theirs"
    ours = acquire(stack, mine, holder="alice")
    assert ours.status_code == 200
    assert acquire(stack, theirs, holder="bob").status_code == 200

    r = release(stack, theirs, capability=cap(ours))
    assert r.status_code == 403, r.text
    assert versions(stack, theirs).json()["checkout"]["holder"] == "bob"


def test_a_released_capability_cannot_be_replayed_onto_the_next_lease(stack):
    """The generation counter is monotonic ACROSS release, so a token from a
    finished lease never verifies against the lock that follows it — even when
    the same holder takes it again."""
    t = "co-replay-next"
    first = acquire(stack, t, holder="alice")
    assert first.status_code == 200
    stale = cap(first)
    assert release(stack, t, capability=stale).status_code == 200

    second = acquire(stack, t, holder="alice")
    assert second.status_code == 200
    assert cap(second) != stale

    r = release(stack, t, capability=stale)
    assert r.status_code == 403, r.text
    assert versions(stack, t).json()["checkout"]["holder"] == "alice"
    assert release(stack, t, capability=cap(second)).status_code == 200


# --------------------------------------------------------------------------- #
# 11. undo / redo are gated exactly like a version publish
# --------------------------------------------------------------------------- #
def test_undo_redo_need_the_capability_while_a_lock_is_held(stack):
    """They MUTATE head, which every other session reads, so they were the way
    around the write check: a caller refused at the publish gate could still walk
    the same drawing's head backwards under someone else's lease."""
    t = "co-undo-locked"
    # publish a v2 to have something to undo, BEFORE any lock exists
    r = run_wait(stack, WRITE_TOOL, {"drawing_id": "demo"}, tenant=t)
    assert r.status_code == 200, r.text
    assert versions(stack, t).json()["head"] == 2

    took = acquire(stack, t, holder="alice")
    assert took.status_code == 200

    for label, kwargs in (("no capability", {}),
                          ("forged capability", {"capability": FORGED_CAP})):
        got = undo(stack, t, **kwargs)
        assert got.status_code == 403, f"{label}: {got.text}"
        assert got.json()["error"]["error_code"] == "BAD_PARAMS"
        assert versions(stack, t).json()["head"] == 2      # head did not move

    # the real holder still undoes and redoes
    assert undo(stack, t, capability=cap(took)).status_code == 200
    assert versions(stack, t).json()["head"] == 1
    assert redo(stack, t, capability=cap(took)).status_code == 200
    assert versions(stack, t).json()["head"] == 2


def test_undo_redo_need_no_capability_when_no_lock_is_held(stack):
    """The demo's undo/redo buttons never take a checkout. Adding authorization
    must not add a checkout REQUIREMENT — same permitted cases as a publish."""
    t = "co-undo-free"
    r = run_wait(stack, WRITE_TOOL, {"drawing_id": "demo"}, tenant=t)
    assert r.status_code == 200, r.text

    assert undo(stack, t).status_code == 200
    assert versions(stack, t).json()["head"] == 1
    assert redo(stack, t).status_code == 200
    assert versions(stack, t).json()["head"] == 2
