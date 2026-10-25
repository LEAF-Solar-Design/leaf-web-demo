"""Bounded, resumable multiplexed Lab events. Credentials stay in headers."""
from __future__ import annotations

import asyncio
from collections import deque
import json
import logging
import math
import time
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import StreamingResponse

import deps
import lab_run_store as store
from routers.lab_artifacts import _admit
from routers.lab_runs import _call, _public_event

router = APIRouter(prefix="/api/lab/stream", tags=["lab_stream"])
logger = logging.getLogger(__name__)
LEASE_TTL = 45
VALUE_BYTES = 64 * 1024
STREAM_BYTES = 2 * 1024 * 1024
_streams = set()


class _StreamLease:
    def __init__(self, tenant, principal, stream_id, signal):
        self.tenant = str(tenant)
        self.principal = principal
        self.stream_id = stream_id
        self.signal = signal
        self.release_task = None

    async def _release(self):
        _streams.discard(self.signal)
        try:
            await asyncio.to_thread(store.release_stream, self.tenant, self.principal, self.stream_id)
        except Exception:
            # A store outage cannot prevent close; the admission TTL is the fallback.
            pass

    async def release(self):
        if self.release_task is None:
            self.release_task = asyncio.create_task(self._release())
        await asyncio.shield(self.release_task)


class _LeasedStreamingResponse(StreamingResponse):
    def __init__(self, content, lease, **kwargs):
        super().__init__(content, **kwargs)
        self.lease = lease

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            await self.lease.release()


@router.on_event("shutdown")
async def drain():
    for signal in tuple(_streams):
        signal.set()


def _frame(kind, data, event_id=None):
    return (("id: " + event_id + "\n") if event_id is not None else "") + (
        "event: " + kind + "\ndata: " + json.dumps(jsonable_encoder(data), separators=(",", ":")) + "\n\n")


def _cursors(runs, after_seq, last_id):
    ids = runs.split(",")
    if not 1 <= len(ids) <= 8 or len(set(ids)) != len(ids):
        raise HTTPException(422, detail="invalid_lab_stream_runs")
    try:
        for run_id in ids:
            store._pin(run_id)
        cursors = dict.fromkeys(ids, after_seq)
        if last_id:
            pairs = last_id.split(",")
            if len(pairs) > 8:
                raise ValueError
            seen = set()
            for pair in pairs:
                run_id, seq = pair.rsplit(":", 1)
                if run_id not in cursors or run_id in seen or int(seq) < -1:
                    raise ValueError
                seen.add(run_id)
                cursors[run_id] = int(seq)
        return cursors
    except (ValueError, store.InvalidGraph):
        raise HTTPException(422, detail="invalid_lab_stream_cursor") from None


async def event_stream(tenant, cursors, principal, stream_id, signal, *,
                       clock=time.monotonic, sleep=asyncio.sleep,
                       wall_clock=time.time, request=None, lease=None):
    if lease is None:
        lease = _StreamLease(tenant, principal, stream_id, signal)
    started = clock()
    binding_at = started
    heartbeat_at = started + 15
    window = started
    byte_count = 0
    rates = dict.fromkeys(cursors, 0)
    pending = {run_id: {} for run_id in cursors}
    # Bounded rolling samples; counts cover the complete stream.
    latencies = {run_id: deque(maxlen=15000) for run_id in cursors}
    counts = dict.fromkeys(cursors, 0)
    newest = None
    reason = "disconnect"

    def encode(run_id, event, coalesced=0):
        public = _public_event(event)
        if public["kind"] == "value":
            size = len(json.dumps(public["payload"], separators=(",", ":")).encode())
            if size > VALUE_BYTES:
                public["payload"] = {"truncated": True, "size": size}
        if coalesced:
            public["coalesced"] = coalesced
        return _frame("run", {"run_id": run_id, **public}, f'{run_id}:{event["seq"]}')

    def record(run_id, event):
        nonlocal newest
        stamp = store._date(event["created_at"]).timestamp()
        newest = stamp if newest is None else max(newest, stamp)
        latencies[run_id].append(max(0, (wall_clock() - stamp) * 1000))
        counts[run_id] += 1

    async def closing_reason():
        nonlocal binding_at
        now = clock()
        if signal.is_set():
            return "drain"
        if now - started >= 300:
            return "max_age"
        if now >= binding_at:
            binding_at = now + 1
            if (deps.auth_live() and isinstance(tenant, deps.TenantContext)
                    and not getattr(tenant, "verified_demo", False) and tenant.subject):
                try:
                    active, _ = await asyncio.to_thread(
                        deps.resolve_active_platform_tenant_authority, tenant.subject)
                except HTTPException:
                    return "binding_unavailable"
                if active != str(tenant):
                    return "binding_changed"
        return None

    try:
        while True:
            now = clock()
            closed = await closing_reason()
            if closed:
                reason = closed
                break
            if request is not None and await request.is_disconnected():
                return
            if now >= heartbeat_at:
                if not await asyncio.to_thread(store.renew_stream, str(tenant), principal, stream_id, LEASE_TTL):
                    reason = "lease_expired"
                    break
                heartbeat_at = now + 15
                yield ": hb\n\n"
                yield _frame("health", {"lag_ms": max(0, (wall_clock() - newest) * 1000)
                                       if newest is not None else None})
            if now - window >= 1:
                window, byte_count = now, 0
                rates = dict.fromkeys(cursors, 0)
            for run_id in cursors:
                # Deliver the newest deferred value per block before reading more.
                for block, (event, removed) in sorted(pending[run_id].items(), key=lambda item: item[1][0]["seq"]):
                    closed = await closing_reason()
                    if closed:
                        reason = closed
                        yield _frame("close", {"reason": reason})
                        return
                    frame = encode(run_id, event, removed)
                    if rates[run_id] >= 50 or byte_count + len(frame.encode()) > STREAM_BYTES - 4096:
                        break
                    del pending[run_id][block]
                    rates[run_id] += 1
                    byte_count += len(frame.encode())
                    record(run_id, event)
                    yield frame
                if pending[run_id]:
                    continue
                try:
                    events = await asyncio.to_thread(store.list_events, str(tenant), run_id,
                                                     after_seq=cursors[run_id], limit=200)
                except store.NotFound:
                    reason = "run_missing"
                    yield _frame("close", {"reason": reason, "run_id": run_id})
                    return
                for event in events:
                    closed = await closing_reason()
                    if closed:
                        reason = closed
                        yield _frame("close", {"reason": reason})
                        return
                    # Keep control events behind every deferred value, including
                    # when the allowance was used exactly without a pending value.
                    if event["kind"] != "value" and (pending[run_id] or rates[run_id] >= 50):
                        break
                    stamp = store._date(event["created_at"]).timestamp()
                    newest = stamp if newest is None else max(newest, stamp)
                    frame = encode(run_id, event)
                    if byte_count + len(frame.encode()) > STREAM_BYTES - 4096:
                        break
                    if event["seq"] > cursors[run_id] + 1:
                        gap = _frame("gap", {"run_id": run_id, "from": cursors[run_id] + 1,
                                             "to": event["seq"] - 1})
                        byte_count += len(gap.encode())
                        yield gap
                    cursors[run_id] = event["seq"]
                    if event["kind"] == "value" and rates[run_id] >= 50:
                        block = event["block_id"]
                        previous = pending[run_id].get(block)
                        pending[run_id][block] = (event, previous[1] + 1 if previous else 1)
                        continue
                    if event["kind"] == "value":
                        rates[run_id] += 1
                    byte_count += len(frame.encode())
                    record(run_id, event)
                    yield frame
            await sleep(0.25)
        yield _frame("close", {"reason": reason})
    except Exception:
        reason = "store_unavailable"
        yield _frame("close", {"reason": reason})
    finally:
        try:
            await lease.release()
        finally:
            summary = {}
            for run_id, samples in latencies.items():
                ordered = sorted(samples)
                summary[run_id] = {"count": counts[run_id], **{
                    key: ordered[max(0, math.ceil(len(ordered) * percentile) - 1)] if ordered else None
                    for key, percentile in (("p50_ms", .5), ("p95_ms", .95))}}
            logger.info("lab_stream_closed %s", json.dumps({"reason": reason, "runs": summary}))


@router.get("/runs")
async def runs(request: Request, runs: str = Query(...), after_seq: int = Query(-1, ge=-1),
               tenant=Depends(deps.require_tenant)):
    _admit(tenant, "build")
    if any(key in request.query_params for key in ("token", "jwt", "ticket", "access_token")):
        raise HTTPException(400, detail="lab_stream_query_credential")
    cursors = _cursors(runs, after_seq, request.headers.get("last-event-id"))
    for run_id in cursors:
        await asyncio.to_thread(_call, store.get, str(tenant), run_id)
    principal = getattr(tenant, "subject", None) or "tenant:" + str(tenant)
    stream_id = uuid.uuid4().hex
    signal = asyncio.Event()
    lease = _StreamLease(tenant, principal, stream_id, signal)
    try:
        await asyncio.to_thread(store.acquire_stream, str(tenant), principal, stream_id, LEASE_TTL)
    except store.TooLarge:
        raise HTTPException(409, detail="lab_stream_limit") from None
    except Exception:
        raise HTTPException(503, detail="lab_store_unavailable") from None
    _streams.add(signal)
    return _LeasedStreamingResponse(
        event_stream(tenant, cursors, principal, stream_id, signal, request=request, lease=lease), lease,
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
