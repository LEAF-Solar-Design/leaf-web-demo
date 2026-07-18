"""da/queue.py — APS submit QUEUE: fair per-tenant scheduling under a Flex ceiling.

WHY THIS FILE ALSO RE-EXPORTS THE STDLIB `queue`
------------------------------------------------
The regression oracles run `python -m pytest` with cwd=`da/`, which puts `da/` on
`sys.path[0]`. A bare module named `queue.py` there SHADOWS the standard-library
`queue` module that pytest/pluggy (`queue.LifoQueue`) and `concurrent.futures`
import — breaking collection and the async job spine's thread pool. The brief
mandates this filename, so this module is a TRANSPARENT SUPERSET: it loads the
real stdlib `queue` by path and re-exports its public API, THEN adds the Leaf
scheduler on top. `queue.LifoQueue` still works; `queue.FairQueue` / `queue.admit`
are the additions. (The broker process also caches the stdlib `queue` before any
`da/` path insert — belt and suspenders.)

WHAT THE SCHEDULER DOES
-----------------------
Today every /api/run submits + polls a WorkItem synchronously with no concurrency
model — the head-of-line-blocking point named in the plan. This module adds:

  * a hard CEILING `APS_MAX_CONCURRENCY` (env, default 1 — the account Flex cap;
    it moves in lockstep with the Autodesk raise requested in
    docs/aps-concurrency-raise-request.md) on WorkItems in flight ACROSS ALL
    tenants, and
  * ROUND-ROBIN FAIR dispatch per tenant: a tenant's 2nd job never precedes any
    other tenant's 1st job. Implemented with a per-tenant FIFO + a rotation ring +
    a global semaphore + a recorded dispatch log. Pure python; no external broker
    needed for the unit test.

INTEGRATION: da/client.submit_workitem routes its LIVE submit through `admit()`
(the process-global ceiling gate), so the synchronous /api/run path stays correct
while honoring the ceiling. APS_LIVE=0 / dry_run never reach the gate.
"""
from __future__ import annotations

# --------------------------------------------------------------------------- #
# transparent stdlib-queue superset (see module docstring)
# --------------------------------------------------------------------------- #
import importlib.util as _ilu
import os as _os

_std_queue_path = _os.path.join(_os.path.dirname(_os.__file__), "queue.py")
_spec = _ilu.spec_from_file_location("_leaf_stdlib_queue", _std_queue_path)
_stdlib_queue = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(_stdlib_queue)  # type: ignore[union-attr]
for _n, _v in _stdlib_queue.__dict__.items():
    if not _n.startswith("_"):
        globals().setdefault(_n, _v)  # Queue, LifoQueue, PriorityQueue, SimpleQueue, Empty, Full, ...

# --------------------------------------------------------------------------- #
# Leaf fair scheduler
# --------------------------------------------------------------------------- #
import threading
import time
import uuid
from collections import OrderedDict, deque
from contextlib import contextmanager


def max_concurrency() -> int:
    """Account Flex ceiling (WorkItems in flight across ALL tenants).

    Read at call time so a subprocess env override applies. Default 1 (very
    conservative; raise in lockstep with the Autodesk Flex-limit raise). See
    docs/aps-concurrency-raise-request.md.
    """
    try:
        return max(1, int(_os.environ.get("APS_MAX_CONCURRENCY", "1")))
    except ValueError:
        return 1


# module-level constant snapshot (also exported for callers that want the number)
APS_MAX_CONCURRENCY = max_concurrency()


class QueueFull(RuntimeError):
    """Raised when the ceiling gate could not be acquired within a timeout."""


# --------------------------------------------------------------------------- #
# process-global ceiling gate (the inline integration point in submit_workitem)
# --------------------------------------------------------------------------- #
_gate_lock = threading.Lock()
_gate = {"sem": threading.BoundedSemaphore(APS_MAX_CONCURRENCY), "n": APS_MAX_CONCURRENCY}


def _ensure_gate() -> "threading.BoundedSemaphore":
    """Return the shared ceiling semaphore, rebuilding it if APS_MAX_CONCURRENCY
    changed (tests/subprocesses may set a different ceiling)."""
    n = max_concurrency()
    with _gate_lock:
        if n != _gate["n"]:
            _gate["sem"] = threading.BoundedSemaphore(n)
            _gate["n"] = n
        return _gate["sem"]


@contextmanager
def admit(tenant_id: str | None = None, timeout: float | None = None):
    """Ceiling gate for ONE live WorkItem submit (process-global).

    Blocks until a slot is free, so at most APS_MAX_CONCURRENCY live submits run
    concurrently in this process. `tenant_id` is accepted for future fair
    accounting + logging; the ceiling itself is tenant-agnostic (fairness ACROSS
    tenants is FairQueue's job). Raises QueueFull if `timeout` elapses.
    """
    sem = _ensure_gate()
    acquired = sem.acquire(timeout=timeout) if timeout is not None else sem.acquire()
    if not acquired:
        raise QueueFull(f"APS ceiling ({_gate['n']}) busy; no slot within {timeout}s")
    try:
        yield
    finally:
        try:
            sem.release()
        except ValueError:
            pass  # ceiling was rebuilt underneath us; ignore stale release


# --------------------------------------------------------------------------- #
# FairQueue — per-tenant FIFO + round-robin ring + ceiling, with a dispatch log
# --------------------------------------------------------------------------- #
class FairQueue:
    """Round-robin-fair, ceiling-bounded submit queue.

    enqueue(tenant_id, activity_id, arguments) -> job_id, then drain via
    run_all(runner). At most `ceiling` jobs are ever in the in-flight
    (submitted/inprogress) set. Dispatch order is round-robin fair across
    tenants: a tenant's k-th job is never dispatched before every other tenant's
    (k-1)-th job. Both properties are provable from `dispatch_log`.

    The `runner(job)` callable performs the actual submit (in tests, a stub; in
    production, the thing that POSTs the WorkItem). Exceptions from the runner
    mark the job failed but never break fairness/ceiling bookkeeping.
    """

    IN_FLIGHT = ("submitted", "inprogress")

    def __init__(self, ceiling: int | None = None, runner=None):
        self.ceiling = int(ceiling if ceiling is not None else max_concurrency())
        if self.ceiling < 1:
            raise ValueError("ceiling must be >= 1")
        self._runner = runner
        self._fifos: "OrderedDict[str, deque]" = OrderedDict()  # tenant_id -> deque[job_id]
        self._ring: deque = deque()  # tenants with pending work, in rotation order
        self._jobs: dict = {}  # job_id -> job record
        self._lock = threading.Lock()
        self._sem = threading.BoundedSemaphore(self.ceiling)
        self._inflight = 0
        self.max_inflight = 0
        self.dispatch_log: list = []  # [{seq, event, tenant_id, job_id, inflight, ts}]
        self._seq = 0

    # -- enqueue ----------------------------------------------------------- #
    def enqueue(self, tenant_id: str, activity_id: str, arguments: dict,
                job_id: str | None = None, meta: dict | None = None) -> str:
        """Append a job to the tenant's FIFO. Returns its job_id."""
        import tenant as _tenant  # sibling; validates identity (isolation guard)

        tid = _tenant.validate_tenant_id(tenant_id)
        jid = job_id or str(uuid.uuid4())
        rec = {
            "job_id": jid, "tenant_id": tid, "activity_id": activity_id,
            "arguments": arguments, "status": "queued", "result": None,
            "error": None, "meta": meta or {}, "enqueued_at": time.time(),
        }
        with self._lock:
            self._jobs[jid] = rec
            if tid not in self._fifos:
                self._fifos[tid] = deque()
                self._ring.append(tid)
            self._fifos[tid].append(jid)
        return jid

    def get(self, job_id: str) -> dict | None:
        with self._lock:
            rec = self._jobs.get(job_id)
            return dict(rec) if rec else None

    poll = get  # alias

    def pending(self) -> int:
        with self._lock:
            return sum(len(d) for d in self._fifos.values())

    # -- round-robin picker (call under _lock) ----------------------------- #
    def _next_job_id(self) -> str | None:
        """Pop the next job_id round-robin across tenants, or None if drained.

        The chosen tenant is rotated to the BACK of the ring, so every other
        tenant with pending work is served before that tenant again — the
        fairness invariant.
        """
        checked = 0
        n = len(self._ring)
        while self._ring and checked <= n:
            tid = self._ring.popleft()
            fifo = self._fifos.get(tid)
            if fifo:
                jid = fifo.popleft()
                if fifo:  # still has work -> back of the rotation
                    self._ring.append(tid)
                return jid
            checked += 1
        return None

    def _log(self, event: str, rec: dict) -> None:
        self._seq += 1
        self.dispatch_log.append({
            "seq": self._seq, "event": event, "tenant_id": rec["tenant_id"],
            "job_id": rec["job_id"], "inflight": self._inflight, "ts": time.time(),
        })

    # -- drain ------------------------------------------------------------- #
    def run_all(self, runner=None, workers: int | None = None, block: bool = True) -> list:
        """Drain the queue honoring ceiling + fairness. Returns job records in
        completion order. `runner` overrides the constructor runner.

        Uses a small worker pool that all contend on the ceiling semaphore, so
        the ceiling (not the pool size) is the true concurrency bound — a job is
        only picked while holding a semaphore slot, which is why at most
        `ceiling` jobs are ever in flight and the picks serialize into a
        deterministic round-robin order.
        """
        run = runner or self._runner
        if run is None:
            raise ValueError("no runner provided (set one in __init__ or run_all)")

        total = self.pending()
        if total == 0:
            return []
        nworkers = workers if workers is not None else min(max(total, self.ceiling), 32)
        completed: list = []
        completed_lock = threading.Lock()

        def worker():
            while True:
                self._sem.acquire()
                with self._lock:
                    jid = self._next_job_id()
                    if jid is None:
                        self._sem.release()
                        return
                    rec = self._jobs[jid]
                    rec["status"] = "submitted"
                    self._inflight += 1
                    self.max_inflight = max(self.max_inflight, self._inflight)
                    self._log("dispatch", rec)
                try:
                    rec["status"] = "inprogress"
                    rec["result"] = run(dict(rec))
                    rec["status"] = "complete"
                except Exception as exc:  # noqa: BLE001
                    rec["status"] = "failed"
                    rec["error"] = f"{type(exc).__name__}: {exc}"
                finally:
                    with self._lock:
                        self._inflight -= 1
                        self._log("complete", rec)
                    with completed_lock:
                        completed.append(dict(rec))
                    self._sem.release()

        threads = [threading.Thread(target=worker, name=f"fairq-{i}", daemon=True)
                   for i in range(nworkers)]
        for t in threads:
            t.start()
        if block:
            for t in threads:
                t.join()
        return completed

    # -- assertions helpers (used by tests) -------------------------------- #
    def admission_order(self) -> list:
        """job_ids in the order they were dispatched (admitted to in-flight)."""
        return [e["job_id"] for e in self.dispatch_log if e["event"] == "dispatch"]

    def peak_in_flight(self) -> int:
        """Max size the in-flight set ever reached (recomputed from the log)."""
        cur = peak = 0
        for e in self.dispatch_log:
            if e["event"] == "dispatch":
                cur += 1
                peak = max(peak, cur)
            elif e["event"] == "complete":
                cur -= 1
        return peak
