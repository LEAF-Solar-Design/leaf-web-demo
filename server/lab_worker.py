"""In-process Lab executor: 2 runs per process, 4 active per tenant, queue 16
per tenant, 600 s per run, per-block budgets from lab_evaluate; host graphs are
refused, and tool nodes require explicit admission; claims only on the adopted revision unless idle
claims are explicitly enabled and labelled.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
import logging
import os
import threading
import time
import uuid

import emf_metrics
import lab_artifacts
import lab_activation
import lab_run_store
import lab_tool_nodes
from dependency_health import _source_revision
from lab_evaluate import (Budget, GraphRefused, RunBudgetExceeded, RunCancelled,
                          evaluate_graph, export_value, trusted_registry)
from routers.lab_surface import lab_surface_enabled

OPS_REVISION = "lab-ops.v1"
_LOG = logging.getLogger(__name__)


@dataclass
class _Run:
    row: dict
    lease: dict
    lock: object = field(default_factory=threading.RLock)
    done: object = field(default_factory=threading.Event)
    lost: bool = False
    suspended: bool = False
    terminal: bool = False
    cancel: bool = False
    checked_at: float = float("-inf")
    future: object = None


class LabWorker:
    def __init__(self, store=lab_run_store, artifacts=lab_artifacts, *, slots=2,
                 poll_s=0.25, lease_s=30, heartbeat_s=10, clock=time.monotonic,
                 wall=time.time, emit=emf_metrics, env=os.environ, worker_id=None):
        if type(slots) is not int or not 1 <= slots <= 2:
            raise ValueError("invalid_lab_worker_slots")
        if any(not 0 < value < float("inf") for value in (poll_s, lease_s, heartbeat_s)):
            raise ValueError("invalid_lab_worker_interval")
        self.store, self.artifacts, self.slots = store, artifacts, slots
        self.poll_s, self.lease_s, self.heartbeat_s = poll_s, lease_s, heartbeat_s
        self.clock, self.wall, self.emit, self.env = clock, wall, emit, env
        self.worker_id = worker_id or "lab-" + uuid.uuid4().hex
        self.revision = _source_revision(env) or "unknown"
        self._stop = threading.Event()
        self._lock = threading.RLock()
        self._runs = {}
        self._thread = None
        self._pool = ThreadPoolExecutor(max_workers=slots, thread_name_prefix="lab-run")
        self._status_at = float("-inf")
        self._activation_at = float("-inf")

    def start(self):
        with self._lock:
            if self._thread is None and not self._stop.is_set():
                self._thread = threading.Thread(target=self._loop, name="lab-worker", daemon=True)
                self._thread.start()
        return self

    def stop(self, timeout_s=2.0):
        started = time.monotonic()
        self._stop.set()
        # Database calls can outlive the shutdown deadline. Suspension remains
        # best-effort in a daemon while the caller's wait stays bounded.
        def suspend_all():
            with self._lock:
                for run in self._runs.values():
                    try:
                        self._suspend(run, "worker_disabled")
                    except Exception as exc:
                        _LOG.warning("lab_worker_suspend_failed class=%s", type(exc).__name__)

        suspender = threading.Thread(target=suspend_all, name="lab-suspend", daemon=True)
        suspender.start()
        suspender.join(max(0, timeout_s - (time.monotonic() - started)))
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(max(0, timeout_s - (time.monotonic() - started)))
        self._pool.shutdown(wait=False, cancel_futures=True)

    def _loop(self):
        failures = 0
        while not self._stop.is_set():
            try:
                self.tick()
                failures = 0
            except Exception as exc:
                _LOG.warning("lab_worker_tick_failed class=%s", type(exc).__name__)
                failures = min(failures + 1, 5)
            self._stop.wait(min(5, self.poll_s * 2 ** failures))

    def _off_reason(self):
        if self._stop.is_set() or self.env.get("LEAF_LAB_WORKER_DISABLED") == "1":
            return "worker_disabled"
        if not lab_surface_enabled():
            return "rail_off"
        if self.store.paused():
            return "paused"
        return None

    def _args(self, run):
        return (run.row["tenant_id"], run.row["run_id"], self.worker_id, run.lease["fence"])

    def _append(self, run, events):
        if not self.store.append_events(*self._args(run), events):
            run.lost = True
            return False
        return True

    def _suspend(self, run, reason):
        with run.lock:
            if not (run.lost or run.suspended or run.terminal):
                self._append(run, [{"kind": "suspended", "payload": {"reason": reason}}])
                run.suspended = True

    def tick(self):
        with self._lock:
            self._runs = {key: run for key, run in self._runs.items()
                          if run.future is None or not run.future.done()}
            reason = self._off_reason()
            latest = self.store.latest_build_revision()
            color = "active" if latest is None or latest == self.revision else "idle"
            if not reason and color == "active" and self.clock() - self._activation_at >= 5:
                self._activation_at = self.clock()
                lab_activation.tick(self.wall())
            if reason:
                for run in self._runs.values():
                    self._suspend(run, reason)
            elif color == "active" or self.env.get("LEAF_LAB_WORKER_IDLE_CLAIMS") == "1":
                free = self.slots - len(self._runs)
                for row in self.store.claimable(free, ops_revisions={OPS_REVISION}):
                    key = (row["tenant_id"], row["run_id"])
                    if key in self._runs:
                        continue
                    lease = self.store.claim(*key, self.worker_id, self.lease_s,
                                             build_revision=self.revision, color=color)
                    if not lease:
                        continue
                    run = _Run(row, lease)
                    self._runs[key] = run
                    if lease.get("takeover"):
                        self.emit.emit_lab_lease_takeover()
                    run.future = self._pool.submit(self._execute, run)
            now = self.clock()
            if now - self._status_at >= self.heartbeat_s:
                self.store.record_worker_status(self.worker_id, self.revision, color,
                                                sum(not (r.suspended or r.lost or r.terminal)
                                                    for r in self._runs.values()),
                                                self.store.queued_run_count())
                age = self.store.oldest_queued_age_s(now=self.wall())
                self.emit.emit_lab_queue_age_max(0 if age is None else age)
                self._status_at = now

    def _cancelled(self, run):
        with run.lock:
            reason = self._off_reason()
            if reason:
                self._suspend(run, reason)
            if run.lost or run.suspended or self._stop.is_set():
                return True
            now = time.monotonic()
            if now - run.checked_at >= 0.25:
                run.cancel = self.store.cancel_requested(run.row["tenant_id"], run.row["run_id"])
                run.checked_at = now
            return run.cancel

    def _renew(self, run):
        while not run.done.wait(self.lease_s / 3):
            try:
                with run.lock:
                    if run.lost or run.suspended or run.terminal:
                        return
                    reason = self._off_reason()
                    if reason:
                        self._suspend(run, reason)
                        return
                    if not self.store.heartbeat(*self._args(run), self.lease_s):
                        run.lost = True
                        return
            except Exception as exc:
                with run.lock:
                    run.lost = True
                _LOG.warning("lab_worker_heartbeat_failed class=%s", type(exc).__name__)
                return

    def _terminal(self, run, state, reason=None):
        with run.lock:
            off = self._off_reason()
            if off:
                self._suspend(run, off)
            if run.lost or run.suspended or run.terminal:
                return
            if state != "finished" and not self._append(run, [{
                    "kind": state, "payload": {"reason": reason} if reason else {}}]):
                return
            if self.store.finish(*self._args(run), state):
                run.terminal = True
                self.emit.emit_lab_run_terminal(state)
            else:
                run.lost = True

    def _execute(self, run):
        started = self.clock()
        renewer = threading.Thread(target=self._renew, args=(run,), daemon=True, name="lab-lease")
        try:
            renewer.start()
            row = run.row
            if row["adapter_revision"] == "lab-activation.v1" and not lab_activation.holds_run(row["tenant_id"], row["run_id"]):
                self._terminal(run, "cancelled", "activation_inactive")
                return
            artifact = self.artifacts.get(row["tenant_id"], row["doc_id"], row["graph_revision"])
            if self.clock() - started >= 600:
                raise RunBudgetExceeded()
            if artifact["sha256"] != row["graph_sha256"]:
                self._terminal(run, "failed", "graph_pin_mismatch")
                return
            if self.store.registry_digest() != row["registry_digest"]:
                self._terminal(run, "failed", "registry_changed")
                return
            committed = set()
            if run.lease.get("takeover"):
                cursor, end = -1, run.lease["committed_seq"]
                while cursor < end:
                    batch = self.store.list_events(row["tenant_id"], row["run_id"], after_seq=cursor, limit=1000)
                    if not batch:
                        break
                    committed.update(event["block_id"] for event in batch
                                     if event["seq"] <= end and event["kind"] == "value")
                    cursor = batch[-1]["seq"]

            elapsed = max(0, self.clock() - started)
            if elapsed >= 600:
                raise RunBudgetExceeded()
            if row["adapter_revision"] == "lab-activation.v1" and not lab_activation.holds_run(row["tenant_id"], row["run_id"]):
                self._terminal(run, "cancelled", "activation_inactive")
                return

            def on_block(block_id, values):
                with run.lock:
                    if self._cancelled(run):
                        raise RunCancelled()
                    if self.clock() - started > 600:
                        raise RunBudgetExceeded()
                    if block_id not in committed:
                        if not self._append(run, [{"kind": "value", "block_id": block_id,
                                                  "payload": {port: export_value(value)
                                                              for port, value in values.items()}}]):
                            raise RunCancelled()

            tool_options = {}
            if lab_tool_nodes.enabled():
                effects = ({"effects": ("read", "simulate")}
                           if row["adapter_revision"] == "lab-activation.v1" else {})
                tool_options["tool_adapter"] = lab_tool_nodes.ToolNodeAdapter(
                    row["tenant_id"], row["run_id"], cancelled=lambda: self._cancelled(run), **effects)
            evaluate_graph(artifact["graph"], trusted_registry(), budget=Budget(run_wall_s=600 - elapsed),
                           clock=self.clock, cancelled=lambda: self._cancelled(run), on_block=on_block,
                           **tool_options)
            # Refresh cancellation at the terminal boundary, even for a one-block graph.
            with run.lock:
                run.checked_at = float("-inf")
                if self._cancelled(run):
                    raise RunCancelled()
            if self.clock() - started > 600:
                raise RunBudgetExceeded()
            self._terminal(run, "finished")
        except RunCancelled:
            if run.cancel:
                self._terminal(run, "cancelled")
            elif self._stop.is_set():
                self._suspend(run, "worker_disabled")
        except RunBudgetExceeded:
            self._terminal(run, "failed", "run_wall_cap")
        except GraphRefused as exc:
            self._terminal(run, "failed", exc.code)
        except Exception as exc:
            _LOG.warning("lab_worker_run_failed class=%s", type(exc).__name__)
            try:
                self._terminal(run, "failed", "worker_error")
            except Exception as terminal_exc:
                _LOG.warning("lab_worker_terminal_failed class=%s", type(terminal_exc).__name__)
        finally:
            run.done.set()
