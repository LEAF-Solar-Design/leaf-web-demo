"""da/test_multitenant.py — binary acceptance for the multi-tenant APS hardening.

Four sub-suites, ALL pure-python (NO live APS, NO network):
  (a) per-tenant object-key ISOLATION — same dwg under tenant a vs b => disjoint
      OSS key prefixes, zero collision.
  (b) submit QUEUE — ceiling N=1 across 3 tenants x 2 jobs never exceeds 1
      in-flight AND dispatches round-robin fair.
  (c) usage CAP — a $0.02 cap + $0.008/run rejects the 3rd submission pre-flight
      with {error_code:'quota_exceeded', retryable:false}; another tenant is ok.
  (d) orphan REAPER — sweep() cancels EXACTLY the expired-lease/closed WorkItems
      and marks them 'reaped', leaving live jobs untouched.

Runnable two ways:
  cd C:/tmp/leaf-web-demo && python da/test_multitenant.py      # exit 0 on success
  cd C:/tmp/leaf-web-demo && python -m pytest da/test_multitenant.py -q

A module-level no-network guard makes any accidental live APS call fail loudly,
satisfying the acceptance clause 'no live APS call by importing/exercising this
suite'.
"""
import importlib.util
import json
import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

import client  # noqa: E402
import tenant  # noqa: E402
import usage  # noqa: E402
import reaper  # noqa: E402


def _load_queue():
    """Load da/queue.py by path (its FairQueue), independent of `import queue`
    resolving to stdlib-or-shadow."""
    spec = importlib.util.spec_from_file_location("leaf_aps_queue_test", os.path.join(_HERE, "queue.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


leaf_queue = _load_queue()


# --------------------------------------------------------------------------- #
# no-network guard: any live HTTP verb through client.requests -> hard failure.
# (dry_run / key helpers never call the network; this proves it.)
# --------------------------------------------------------------------------- #
class _NoNetwork:
    def _boom(self, *a, **k):
        raise AssertionError("pure test attempted a real network/APS call")

    get = post = put = delete = request = _boom


client.nickname = lambda: "TESTOWNER"          # avoid the live GET /forgeapps/me
client.auth_token = lambda: "offline-faketoken"
os.environ.setdefault("APS_BUCKET", "leaf-web-store-test")  # bucket_key needs no creds
client.requests = _NoNetwork()


# --------------------------------------------------------------------------- #
# (a) per-tenant object-key isolation
# --------------------------------------------------------------------------- #
def test_a_tenant_key_isolation_disjoint_prefixes():
    dwg = "rooftop_demo.dwg"
    ts = 1730000000  # fixed so the ONLY dimension that varies is the tenant

    a_in = client.ephemeral_input_key(dwg, "a", ts)
    b_in = client.ephemeral_input_key(dwg, "b", ts)
    a_out = client.ephemeral_output_key("count_by_layer", "a", ts)
    b_out = client.ephemeral_output_key("count_by_layer", "b", ts)

    # each key lives under its own tenant root
    assert a_in.startswith("t/a/") and b_in.startswith("t/b/")
    assert a_out.startswith("t/a/") and b_out.startswith("t/b/")
    # ZERO collision: the two tenants' keys are never equal and neither prefix
    # contains the other
    assert a_in != b_in and a_out != b_out
    assert not a_in.startswith("t/b/") and not b_in.startswith("t/a/")
    # the differing segment is exactly the tenant segment; everything else matches
    assert a_in.split("/", 2)[1] == "a" and b_in.split("/", 2)[1] == "b"
    assert a_in.split("/", 2)[2] == b_in.split("/", 2)[2]  # identical tail (in/<ts>_<dwg>)

    # default (tenant_id=None) reproduces the legacy single-tenant key byte-for-byte
    assert client.ephemeral_input_key(dwg, None, ts) == f"in/{ts}_{dwg}"
    assert client.ephemeral_output_key("count_by_layer", None, ts) == f"out/{ts}_count_by_layer.result.json"

    # and the REAL client run path (dry-run, no network) carries the disjoint keys
    tool = {"name": "count-by-layer", "engine_op": "count_by_layer", "version": "1.0.0"}
    ra = client.run_tool(client.__file__, tool, {}, dry_run=True, tenant_id="a")
    rb = client.run_tool(client.__file__, tool, {}, dry_run=True, tenant_id="b")
    assert ra["input_object"].startswith("t/a/in/") and rb["input_object"].startswith("t/b/in/")
    assert ra["output_object"].startswith("t/a/out/") and rb["output_object"].startswith("t/b/out/")
    # no shared key between the two tenants
    assert {ra["input_object"], ra["output_object"]}.isdisjoint({rb["input_object"], rb["output_object"]})


# --------------------------------------------------------------------------- #
# (b) submit queue: ceiling N=1, round-robin fair over 3 tenants x 2 jobs
# --------------------------------------------------------------------------- #
def test_b_queue_ceiling_and_round_robin_fairness():
    q = leaf_queue.FairQueue(ceiling=1)
    tenants = ["t0", "t1", "t2"]
    job_ids = {}
    for j in range(2):        # enqueue interleaved so a naive FIFO would NOT be fair
        for t in tenants:
            jid = q.enqueue(t, f"act_{t}", {"n": j})
            job_ids[jid] = (t, j)

    # a runner that takes real (tiny) wall-clock time so overlapping would show up
    def runner(job):
        time.sleep(0.01)
        return {"ran": job["job_id"]}

    completed = q.run_all(runner=runner)
    assert len(completed) == 6

    # (a) ceiling: never more than 1 job in the in-flight set at once
    assert q.peak_in_flight() == 1, f"peak in-flight {q.peak_in_flight()} > ceiling 1"
    assert q.max_inflight == 1

    # (b) round-robin fairness: the first 3 dispatched are each tenant's 1st job,
    #     i.e. no tenant's 2nd job runs before every other tenant's 1st job.
    order = q.admission_order()
    assert len(order) == 6
    first_round_tenants = [job_ids[j][0] for j in order[:3]]
    first_round_rounds = [job_ids[j][1] for j in order[:3]]
    assert sorted(first_round_tenants) == tenants, f"first 3 not one-per-tenant: {first_round_tenants}"
    assert first_round_rounds == [0, 0, 0], f"a 2nd job jumped the queue: {first_round_rounds}"
    second_round_rounds = [job_ids[j][1] for j in order[3:]]
    assert second_round_rounds == [1, 1, 1]


# --------------------------------------------------------------------------- #
# (c) usage cap: 3rd run over a $0.02 cap rejected pre-flight; other tenant ok
# --------------------------------------------------------------------------- #
def test_c_usage_cap_rejects_third_run_preflight():
    cap = 0.02
    per_run = 0.008
    ledger = usage.UsageLedger()
    tid = "cap-tenant"

    # runs 1 and 2 are admitted, and their cost is attributed
    for i in range(2):
        d = usage.check_cap(tid, est_cost=per_run, cap=cap, spent=ledger.spent(tid))
        assert d["ok"] is True, f"run {i+1} should be admitted: {d}"
        ledger.add(tid, per_run)
    assert abs(ledger.spent(tid) - 0.016) < 1e-9

    # run 3 is REJECTED before any APS call (0.016 + 0.008 = 0.024 > 0.02)
    d3 = usage.check_cap(tid, est_cost=per_run, cap=cap, spent=ledger.spent(tid), tool="count-by-layer")
    assert d3["ok"] is False
    assert d3["error"]["error_code"] == "quota_exceeded"
    assert d3["error"]["retryable"] is False
    assert d3["error_code"] == "quota_exceeded"          # plan §3 top-level mirror
    assert d3["retryable"] is False
    assert d3["degraded_mode"] is None                   # plan §3 shape

    # a DIFFERENT tenant, under cap, still succeeds
    dother = usage.check_cap("other-tenant", est_cost=per_run, cap=cap, spent=0.0)
    assert dother["ok"] is True

    # uncapped tenant (no cap configured) is never gated
    assert usage.check_cap("uncapped", est_cost=per_run, cap=None, spent=999.0)["ok"] is True


def test_c_broker_ledger_is_authoritative_attribution(tmp_path=None):
    # write a broker-shaped JSONL ledger and confirm spend is summed correctly,
    # excluding denied (quota_exceeded / TENANT_DISABLED) lines
    import tempfile
    fd, path = tempfile.mkstemp(suffix=".jsonl")
    os.close(fd)
    try:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"tenant_id": "x", "usd_est": 0.008, "status": "ok"}) + "\n")
            fh.write(json.dumps({"tenant_id": "x", "usd_est": 0.008, "status": "ok"}) + "\n")
            fh.write(json.dumps({"tenant_id": "x", "usd_est": None, "status": "quota_exceeded"}) + "\n")
            fh.write(json.dumps({"tenant_id": "y", "usd_est": 0.005, "status": "ok"}) + "\n")
        assert abs(usage.spent_from_broker_ledger("x", path) - 0.016) < 1e-9
        assert abs(usage.spent_from_broker_ledger("y", path) - 0.005) < 1e-9
        assert usage.spent_from_broker_ledger("z", path) == 0.0
    finally:
        os.remove(path)


# --------------------------------------------------------------------------- #
# (d) reaper: cancel exactly the orphaned WorkItems, leave live ones untouched
# --------------------------------------------------------------------------- #
def test_d_reaper_cancels_only_orphans():
    now = 1_000_000.0
    records = [
        {"job_id": "j-live", "status": "inprogress", "workitem_id": "wi-live",
         "lease_expires": now + 300},                       # live: lease valid
        {"job_id": "j-expired", "status": "inprogress", "workitem_id": "wi-expired",
         "lease_expires": now - 1},                          # orphan: lease expired
        {"job_id": "j-closed", "status": "inprogress", "workitem_id": "wi-closed",
         "session_closed": True},                            # orphan: tab closed
        {"job_id": "j-done", "status": "complete", "workitem_id": "wi-done",
         "session_closed": True},                            # not in-flight: ignore
    ]
    stub = reaper.StubCancelClient()
    reaped = reaper.sweep(records, cancel_client=stub, now=now)

    # cancelled EXACTLY the two orphans, in encounter order
    assert stub.cancelled == ["wi-expired", "wi-closed"], stub.cancelled
    assert {r["job_id"] for r in reaped} == {"j-expired", "j-closed"}
    # those rows are marked reaped
    by_id = {r["job_id"]: r for r in records}
    assert by_id["j-expired"]["status"] == "reaped" and by_id["j-expired"]["reaped"] is True
    assert by_id["j-closed"]["status"] == "reaped"
    # live + already-complete rows are untouched (never cancelled, not reaped)
    assert by_id["j-live"]["status"] == "inprogress" and "reaped" not in by_id["j-live"]
    assert by_id["j-done"]["status"] == "complete"
    assert "wi-live" not in stub.cancelled and "wi-done" not in stub.cancelled

    # default cancel_client is inert (no APS): sweeping again cancels nothing new
    stub2 = reaper.StubCancelClient()
    assert reaper.sweep(records, cancel_client=stub2, now=now) == []
    assert stub2.cancelled == []

    # the live-cancel guard is OFF unless BOTH APS_LIVE=1 and BROKER_REAP_LIVE=1
    assert reaper.reap_live_enabled() is False


# --------------------------------------------------------------------------- #
# script entry point: run every test, exit non-zero on any failure
# --------------------------------------------------------------------------- #
def _run_all() -> int:
    tests = [
        ("a_key_isolation", test_a_tenant_key_isolation_disjoint_prefixes),
        ("b_queue_fair_ceiling", test_b_queue_ceiling_and_round_robin_fairness),
        ("c_usage_cap", test_c_usage_cap_rejects_third_run_preflight),
        ("c_broker_ledger_attribution", test_c_broker_ledger_is_authoritative_attribution),
        ("d_reaper", test_d_reaper_cancels_only_orphans),
    ]
    failures = 0
    for name, fn in tests:
        try:
            fn()
            print(f"PASS  {name}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            import traceback
            print(f"FAIL  {name}: {type(exc).__name__}: {exc}")
            traceback.print_exc()
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(_run_all())
