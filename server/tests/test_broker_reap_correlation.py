"""Regressions for the (tenant_id, job_id) -> WorkItem reap correlation.

Every test here pins a fixed review finding: the registry must survive
redeliveries, failed cancels, stub-mode reaps, non-orphan records, and
poll-timeout exits without losing the only copy of a live WorkItem's id —
and the reaper must never report a cancel it did not perform.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

SERVER_DIR = Path(__file__).resolve().parent.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import broker  # noqa: E402
import broker_client  # noqa: E402
import jobs  # noqa: E402
import write_loop  # noqa: E402

DA_DIR = SERVER_DIR.parent / "da"


@pytest.fixture(autouse=True)
def _clean_registry():
    with broker._active_workitems_lock:
        broker._active_workitems.clear()
    yield
    with broker._active_workitems_lock:
        broker._active_workitems.clear()


def _load_reaper():
    import importlib.util
    spec = importlib.util.spec_from_file_location("_test_reaper", DA_DIR / "reaper.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# --------------------------------------------------------------------------- #
# registry semantics
# --------------------------------------------------------------------------- #
def test_registry_is_tenant_scoped():
    broker._record_active_workitem("tenant-a", "job-1", "wi-a", "own-a")
    assert broker.active_workitems_for("tenant-a", "job-1") == ["wi-a"]
    assert broker.active_workitems_for("tenant-b", "job-1") == []
    assert broker._drop_active_workitem("tenant-b", "job-1") == []
    assert broker.active_workitems_for("tenant-a", "job-1") == ["wi-a"]


def test_same_job_second_registration_keeps_both():
    broker._record_active_workitem("t", "job-1", "wi-1", "own-1")
    broker._record_active_workitem("t", "job-1", "wi-2", "own-2")
    assert sorted(broker.active_workitems_for("t", "job-1")) == ["wi-1", "wi-2"]


def test_owner_scoped_drop_spares_other_runs_entry():
    broker._record_active_workitem("t", "job-1", "wi-1", "own-1")
    broker._record_active_workitem("t", "job-1", "wi-2", "own-2")
    assert broker._drop_active_workitem("t", "job-1", owner="own-2") == ["wi-2"]
    assert broker.active_workitems_for("t", "job-1") == ["wi-1"]


def test_drop_with_no_filter_only_hits_named_job():
    broker._record_active_workitem("t", "job-1", "wi-1", "o")
    broker._record_active_workitem("t", "job-2", "wi-2", "o")
    broker._drop_active_workitem("t", "job-1")
    assert broker.active_workitems_for("t", "job-2") == ["wi-2"]


def test_ttl_prunes_expired_entries(monkeypatch):
    broker._record_active_workitem("t", "job-1", "wi-1", "o")
    with broker._active_workitems_lock:
        for entries in broker._active_workitems.values():
            for e in entries:
                e["ts"] -= broker._ACTIVE_WORKITEM_TTL_S + 1
    assert broker.active_workitems_for("t", "job-1") == []
    with broker._active_workitems_lock:
        assert broker._active_workitems == {}


# --------------------------------------------------------------------------- #
# reap resolve: non-destructive, fans out
# --------------------------------------------------------------------------- #
def test_resolve_is_non_destructive_and_fans_out():
    broker._record_active_workitem("t", "job-1", "wi-1", "o1")
    broker._record_active_workitem("t", "job-1", "wi-2", "o2")
    rec = {"job_id": "job-1", "tenant_id": "t", "status": "inprogress",
           "workitem_id": None, "session_closed": True}
    out = broker._resolve_reap_workitems([rec])
    assert sorted(r["workitem_id"] for r in out) == ["wi-1", "wi-2"]
    # the resolve itself must not evict anything: a skipped/failed cancel
    # would otherwise burn the only copy of the correlation
    assert sorted(broker.active_workitems_for("t", "job-1")) == ["wi-1", "wi-2"]


def test_resolve_ignores_records_without_tenant():
    broker._record_active_workitem("t", "job-1", "wi-1", "o")
    out = broker._resolve_reap_workitems([{"job_id": "job-1",
                                           "workitem_id": None}])
    assert out[0].get("workitem_id") is None


# --------------------------------------------------------------------------- #
# reaper.sweep fail-closed semantics
# --------------------------------------------------------------------------- #
def test_sweep_never_marks_unresolved_orphan_reaped():
    reaper = _load_reaper()
    rec = {"status": "inprogress", "workitem_id": None, "session_closed": True}
    reaped = reaper.sweep([rec])
    assert reaped == []
    assert not rec.get("reaped")
    assert rec.get("reap_unresolved") is True


def test_sweep_failed_cancel_keeps_row_unreaped():
    reaper = _load_reaper()

    class FailingCancel:
        def cancel(self, wid):
            return {"workitem_id": wid, "cancelled": False, "status_code": 403}

    rec = {"status": "inprogress", "workitem_id": "wi-9", "session_closed": True}
    reaped = reaper.sweep([rec], cancel_client=FailingCancel())
    assert reaped == []
    assert not rec.get("reaped")
    assert rec["reap_outcome"]["cancelled"] is False


def test_sweep_raising_cancel_does_not_abort_batch():
    reaper = _load_reaper()

    class ExplodingThenOk:
        def __init__(self):
            self.calls = []

        def cancel(self, wid):
            self.calls.append(wid)
            if wid == "wi-bad":
                raise ConnectionError("aps down")
            return {"workitem_id": wid, "cancelled": True}

    bad = {"status": "inprogress", "workitem_id": "wi-bad", "session_closed": True}
    good = {"status": "inprogress", "workitem_id": "wi-good", "session_closed": True}
    client = ExplodingThenOk()
    reaped = reaper.sweep([bad, good], cancel_client=client)
    assert client.calls == ["wi-bad", "wi-good"]
    assert [r["workitem_id"] for r in reaped] == ["wi-good"]
    assert not bad.get("reaped") and bad["reap_outcome"]["cancelled"] is False


def test_sweep_successful_cancel_reaps():
    reaper = _load_reaper()
    rec = {"status": "inprogress", "workitem_id": "wi-1", "session_closed": True}
    reaped = reaper.sweep([rec], cancel_client=reaper.StubCancelClient())
    assert [r["workitem_id"] for r in reaped] == ["wi-1"]
    assert rec["reaped"] is True


# --------------------------------------------------------------------------- #
# on_submitted forwarding
# --------------------------------------------------------------------------- #
def test_run_live_tool_forwards_to_supporting_client():
    seen = {}

    class Da:
        def run_tool(self, local, tool, params, on_submitted=None):
            seen["cb"] = on_submitted
            if on_submitted:
                on_submitted("wi-77")
            return {"ok": True}

    cb_ids = []
    broker._run_live_tool(Da(), "x.dwg", {"name": "t"}, {},
                          on_submitted=cb_ids.append)
    assert cb_ids == ["wi-77"]


def test_run_live_tool_keeps_legacy_double_call_shape():
    calls = []

    class LegacyDa:
        def run_tool(self, local, tool, params):
            calls.append((local, tool, params))
            return {"ok": True}

    broker._run_live_tool(LegacyDa(), "x.dwg", {"name": "t"}, {},
                          on_submitted=lambda wid: None)
    assert calls == [("x.dwg", {"name": "t"}, {})]


def test_production_da_client_declares_on_submitted():
    """The end-to-end dead-code regression: the REAL da/client must accept the
    callback (checked statically -- server code never imports da/ directly)."""
    tree = ast.parse((DA_DIR / "client.py").read_text(encoding="utf-8"))
    fns = {n.name: n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    for name in ("run_tool", "submit_workitem"):
        fn_args = fns[name].args
        names = ([a.arg for a in fn_args.args]
                 + [a.arg for a in fn_args.kwonlyargs])
        assert "on_submitted" in names, f"da/client.{name} lost on_submitted"


def test_write_loop_forwards_on_submitted_when_supported():
    submitted = {}

    class WriteDa:
        def submit_workitem(self, activity_id, args, dry_run=False, poll=True,
                            tenant_id=None, on_submitted=None):
            submitted["cb"] = on_submitted
            return {"status": "failed"}  # stop the write early after submit

        def signed_download_url(self, key):
            return "https://x/get"

        def signed_upload_url(self, key):
            return key, "https://x/put"

        def activity_qualified(self, name):
            return name

    class Backend:
        def exists(self, key):
            # manifest exists; no upload marker
            return "manifest" in key

    import store
    orig = store.resolve_version
    store.resolve_version = lambda *a, **k: (1, "vkey")
    try:
        write_loop.run_write_live(
            {"name": "w", "version": "1.0.0"}, {"drawing_id": "d"}, "t",
            backend=Backend(), da=WriteDa(), t0=0.0,
            on_submitted=lambda wid: None)
    finally:
        store.resolve_version = orig
    assert submitted["cb"] is not None


# --------------------------------------------------------------------------- #
# producer wiring (jobs -> broker)
# --------------------------------------------------------------------------- #
def test_run_via_broker_sends_job_id(monkeypatch):
    captured = {}

    class Resp:
        def json(self):
            return {"ok": True}

    def fake_post(url, json=None, headers=None, timeout=None):
        captured["url"] = url
        captured["body"] = json
        return Resp()

    monkeypatch.setattr(broker_client.requests, "post", fake_post)
    broker_client.run_via_broker("t", {"name": "x"}, {}, "dwg", True,
                                 ledger_event_key="j1:broker-run", job_id="j1")
    assert captured["body"]["job_id"] == "j1"


def test_reap_orphans_once_posts_orphan_records(monkeypatch):
    posted = {}
    monkeypatch.setattr(jobs, "job_store_mode", lambda: "sqlite")
    monkeypatch.setattr(jobs, "_query", lambda *a, **k: [])
    monkeypatch.setattr(jobs, "orphan_lease_records",
                        lambda tenant_id=None: [{"job_id": "j", "tenant_id": "t",
                                                 "status": "inprogress",
                                                 "workitem_id": None,
                                                 "session_closed": True}])
    monkeypatch.setattr(jobs.broker_client, "reap_via_broker",
                        lambda records, **k: posted.update(records=records) or {})
    jobs._reap_orphans_once()
    assert posted["records"][0]["job_id"] == "j"


def test_reap_orphans_once_survives_broker_outage(monkeypatch):
    monkeypatch.setattr(jobs, "job_store_mode", lambda: "sqlite")
    monkeypatch.setattr(jobs, "_query", lambda *a, **k: [])
    monkeypatch.setattr(jobs, "orphan_lease_records",
                        lambda tenant_id=None: [{"job_id": "j"}])

    def boom(records, **k):
        raise broker_client.BrokerUnreachable("down")

    monkeypatch.setattr(jobs.broker_client, "reap_via_broker", boom)
    assert jobs._reap_orphans_once() == 0  # no rows; and no exception escaped
