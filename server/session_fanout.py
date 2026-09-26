"""Shared durable-log fan-out, scoped to one process and running event loop.

An ECS service with K tasks still polls once per task per session with at least
one subscriber on that task. No asyncio primitives cross event-loop boundaries.
A client cursor beyond the log's head makes a later subscriber replay directly
from the log until the log passes that cursor.
"""
from __future__ import annotations

import asyncio
from collections import deque
from contextlib import asynccontextmanager, suppress
from typing import Callable, Optional


SUBSCRIBER_QUEUE_MAX = 1000
_hubs: dict[tuple[asyncio.AbstractEventLoop, str], _Hub] = {}


class _Subscription:
    def __init__(self, hub: _Hub, after_seq: int):
        self.hub = hub
        self.last_delivered = after_seq
        # Replay only the prefix already read by the hub at attachment time.
        # The registered queue holds everything beyond this fixed boundary.
        self.replay_until = hub.cursor
        self.queue: deque[dict] = deque()
        self.ready = asyncio.Event()
        self.closed = False

    def close(self) -> None:
        self.closed = True
        self.queue.clear()
        self.ready.set()

    def put(self, envelope: dict) -> None:
        if self.closed or envelope["seq"] <= max(
            self.last_delivered, self.replay_until,
        ):
            return
        if len(self.queue) >= SUBSCRIBER_QUEUE_MAX:
            self.hub.detach(self)
            return
        self.queue.append(envelope)
        self.ready.set()

    async def next_batch(self, timeout: float) -> Optional[list[dict]]:
        if self.closed:
            return None
        if self.last_delivered < self.replay_until:
            try:
                page = await asyncio.to_thread(
                    self.hub.read, self.hub.session_id, self.last_delivered, 500,
                )
            except Exception:
                self.hub.detach(self)
                return None
            if self.closed:
                return None
            batch = [ev for ev in page
                     if self.last_delivered < ev["seq"] <= self.replay_until]
            if batch:
                self.last_delivered = batch[-1]["seq"]
            if any(ev["seq"] > self.replay_until for ev in page):
                self.last_delivered = self.replay_until
            if self.last_delivered < self.replay_until:
                # A live event cannot skip an unfinished replay prefix, even
                # when this read saw an empty page before the log advanced.
                if not batch:
                    await asyncio.sleep(min(timeout, self.hub.poll_s))
                    if self.closed:
                        return None
                return batch
            if batch:
                return batch
        if not self.queue and timeout > 0:
            try:
                await asyncio.wait_for(self.ready.wait(), timeout)
            except asyncio.TimeoutError:
                return None if self.closed else []
        if self.closed:
            return None
        batch = []
        while self.queue:
            envelope = self.queue.popleft()
            if envelope["seq"] > self.last_delivered:
                batch.append(envelope)
                self.last_delivered = envelope["seq"]
        self.ready.clear()
        return batch


class _Hub:
    def __init__(self, key, after_seq: int, read: Callable, poll_s: float):
        self.key = key
        self.session_id = key[1]
        self.cursor = after_seq
        self.read = read
        self.poll_s = poll_s
        self.subscribers: set[_Subscription] = set()
        self.task: Optional[asyncio.Task] = None

    def remove(self) -> None:
        if _hubs.get(self.key) is self:
            del _hubs[self.key]

    def detach(self, subscription: _Subscription) -> None:
        subscription.close()
        self.subscribers.discard(subscription)
        if not self.subscribers:
            self.remove()
            if self.task is not None and self.task is not asyncio.current_task():
                self.task.cancel()

    async def poll(self) -> None:
        try:
            while self.subscribers:
                events = await asyncio.to_thread(
                    self.read, self.session_id, self.cursor, 500,
                )
                if events:
                    self.cursor = events[-1]["seq"]
                    for subscription in tuple(self.subscribers):
                        for envelope in events:
                            subscription.put(envelope)
                            if subscription.closed:
                                break
                if self.subscribers:
                    await asyncio.sleep(self.poll_s)
        except Exception:
            # Reconnect resumes from the client's durable cursor. The stream
            # vocabulary has no transport-error event.
            pass
        finally:
            for subscription in self.subscribers:
                subscription.close()
            self.subscribers.clear()
            self.remove()


@asynccontextmanager
async def subscribe(session_id: str, after_seq: int, *, read, poll_s: float):
    key = (asyncio.get_running_loop(), session_id)
    hub = _hubs.get(key)
    if hub is None:
        hub = _Hub(key, after_seq, read, poll_s)
        _hubs[key] = hub
    subscription = _Subscription(hub, after_seq)
    # No await before registration: even an in-flight poll cannot pass us by.
    hub.subscribers.add(subscription)
    if hub.task is None:
        hub.task = asyncio.create_task(hub.poll())
    try:
        yield subscription
    finally:
        hub.detach(subscription)
        if not hub.subscribers and hub.task is not None:
            with suppress(asyncio.CancelledError):
                await hub.task


def poller_count(session_id: str) -> int:
    hub = _hubs.get((asyncio.get_running_loop(), session_id))
    return int(hub is not None and hub.task is not None and not hub.task.done())
