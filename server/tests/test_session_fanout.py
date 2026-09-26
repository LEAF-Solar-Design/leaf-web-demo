"""Shared polling for the durable session SSE transport."""
from __future__ import annotations

import asyncio
import json
import sys
import threading
from contextlib import AsyncExitStack, asynccontextmanager, suppress
from pathlib import Path

SERVER_DIR = Path(__file__).resolve().parent.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import session_fanout
from routers import sessions as sessions_router


def _event(seq):
    return {"seq": seq, "type": "text_delta", "data": {"text": str(seq)}}


class _Log:
    def __init__(self, count=0):
        self.events = [_event(seq) for seq in range(1, count + 1)]
        self.calls = []
        self.lock = threading.Lock()

    def read(self, session_id, after_seq, limit):
        with self.lock:
            self.calls.append((session_id, after_seq, limit, threading.get_ident()))
            return [ev for ev in self.events if ev["seq"] > after_seq][:limit]

    def append_through(self, seq):
        with self.lock:
            self.events.extend(_event(n) for n in range(len(self.events) + 1, seq + 1))


async def _collect(subscription, count):
    async def consume():
        events = []
        while len(events) < count:
            batch = await subscription.next_batch(0.1)
            assert batch is not None
            events.extend(batch)
        return events

    return await asyncio.wait_for(consume(), 1)


def test_n_subscribers_share_exactly_one_poll_loop():
    async def run():
        log = _Log(1)
        loop_thread = threading.get_ident()
        async with AsyncExitStack() as stack:
            subscriptions = [await stack.enter_async_context(session_fanout.subscribe(
                "shared", 0, read=log.read, poll_s=0.01,
            )) for _ in range(8)]
            assert session_fanout.poller_count("shared") == 1
            batches = await asyncio.gather(*[_collect(sub, 1) for sub in subscriptions])
            assert batches == [[_event(1)]] * 8
            # Every client was attached before the initial read completed.
            assert len(log.calls) == 1
            assert log.calls[0][:3] == ("shared", 0, 500)
            assert log.calls[0][3] != loop_thread
            assert await subscriptions[0].next_batch(0) == []
        assert session_fanout.poller_count("shared") == 0

    asyncio.run(run())


def test_every_subscriber_receives_ordered_events_after_its_own_cursor():
    async def run():
        log = _Log(10)
        async with AsyncExitStack() as stack:
            subscriptions = [await stack.enter_async_context(session_fanout.subscribe(
                "cursors", cursor, read=log.read, poll_s=0.01,
            )) for cursor in (3, 0, 7)]
            batches = await asyncio.gather(*[
                _collect(sub, 10 - cursor)
                for sub, cursor in zip(subscriptions, (3, 0, 7))
            ])
            for batch, cursor in zip(batches, (3, 0, 7)):
                assert [ev["seq"] for ev in batch] == list(range(cursor + 1, 11))
            other_log = _Log(1)
            async with session_fanout.subscribe(
                "other", 0, read=other_log.read, poll_s=0.01,
            ) as other:
                assert await _collect(other, 1) == [_event(1)]
                assert session_fanout.poller_count("cursors") == 1
                assert session_fanout.poller_count("other") == 1
            assert all(call[0] == "cursors" for call in log.calls)

    # Reusing session names on a new running loop must build fresh primitives.
    asyncio.run(run())
    asyncio.run(run())


def test_late_subscriber_replays_backlog_without_gap_or_duplicate():
    async def run():
        log = _Log(1200)
        replay_started = threading.Event()
        release_replay = threading.Event()

        def read(session_id, after_seq, limit):
            page = log.read(session_id, after_seq, limit)
            if after_seq == 100:
                replay_started.set()
                assert release_replay.wait(1)
            return page

        async with session_fanout.subscribe(
            "late", 0, read=read, poll_s=0.01,
        ) as first:
            assert len(await _collect(first, 1200)) == 1200
            async with session_fanout.subscribe(
                "late", 100, read=read, poll_s=0.01,
            ) as late:
                pending = asyncio.create_task(late.next_batch(0.1))
                try:
                    assert await asyncio.to_thread(replay_started.wait, 1)
                    log.append_through(1205)
                    assert await _collect(first, 5) == [_event(n) for n in range(1201, 1206)]
                    release_replay.set()
                    events = await pending
                    assert events is not None
                    events += await _collect(late, 1105 - len(events))
                    assert [ev["seq"] for ev in events] == list(range(101, 1206))
                    assert await late.next_batch(0) == []
                    assert all(call[2] == 500 for call in log.calls)
                    assert {100, 600, 1100}.issubset({call[1] for call in log.calls})
                finally:
                    release_replay.set()
                    pending.cancel()
                    with suppress(asyncio.CancelledError):
                        await pending

    asyncio.run(run())


def test_subscriber_behind_a_cursor_beyond_the_log_still_gets_new_events():
    async def run():
        log = _Log(10)
        async with session_fanout.subscribe(
            "beyond-head", 100, read=log.read, poll_s=0.01,
        ) as ahead:
            async with session_fanout.subscribe(
                "beyond-head", 0, read=log.read, poll_s=0.01,
            ) as behind:
                assert session_fanout.poller_count("beyond-head") == 1
                events = await _collect(behind, 10)
                assert events == [_event(n) for n in range(1, 11)]
                # An empty replay page must not skip future events below 100.
                assert await behind.next_batch(0.01) == []
                log.append_through(11)
                new_events = await _collect(behind, 1)
                assert new_events == [_event(11)]
                events += new_events
                assert await ahead.next_batch(0.01) == []
                log.append_through(101)
                ahead_events, new_events = await asyncio.gather(
                    _collect(ahead, 1), _collect(behind, 90),
                )
                assert ahead_events == [_event(101)]
                events += new_events
                assert events == [_event(n) for n in range(1, 102)]
                assert await ahead.next_batch(0.02) == []
                assert await behind.next_batch(0.02) == []
                assert session_fanout.poller_count("beyond-head") == 1
        assert session_fanout.poller_count("beyond-head") == 0

    asyncio.run(run())


def test_queue_never_runs_ahead_of_an_open_replay_window():
    async def run():
        log = _Log(10)
        empty_replay_started = threading.Event()
        release_replay = threading.Event()

        def read(session_id, after_seq, limit):
            page = log.read(session_id, after_seq, limit)
            if after_seq == 10 and not page:
                empty_replay_started.set()
                assert release_replay.wait(1)
            return page

        async with session_fanout.subscribe(
            "open-replay", 100, read=read, poll_s=0.01,
        ) as ahead:
            async with session_fanout.subscribe(
                "open-replay", 0, read=read, poll_s=0.01,
            ) as behind:
                events = await _collect(behind, 10)
                assert events == [_event(n) for n in range(1, 11)]
                pending = asyncio.create_task(behind.next_batch(0.1))
                try:
                    assert await asyncio.to_thread(empty_replay_started.wait, 1)
                    # The replay read has captured [], but has not returned.
                    log.append_through(101)
                    assert await _collect(ahead, 1) == [_event(101)]
                    assert list(behind.queue) == [_event(101)]
                    release_replay.set()
                    assert await pending == []
                    assert behind.last_delivered == 10
                    assert list(behind.queue) == [_event(101)]
                    events += await _collect(behind, 91)
                    assert events == [_event(n) for n in range(1, 102)]
                    assert await behind.next_batch(0.02) == []
                finally:
                    release_replay.set()
                    pending.cancel()
                    with suppress(asyncio.CancelledError):
                        await pending
        assert session_fanout.poller_count("open-replay") == 0

    asyncio.run(run())


def test_concurrent_event_loops_each_get_their_own_hub():
    log = _Log()
    attached = threading.Barrier(2, timeout=1)
    publish = threading.Barrier(2, action=lambda: log.append_through(3), timeout=1)
    results = [None, None]
    errors = [None, None]

    def client(index):
        async def run():
            async with session_fanout.subscribe(
                "concurrent-loops", 0, read=log.read, poll_s=0.01,
            ) as subscription:
                await asyncio.to_thread(attached.wait)
                assert subscription.hub.task.get_loop() is asyncio.get_running_loop()
                assert session_fanout.poller_count("concurrent-loops") == 1
                # Publish only after both loops have attached and checked affinity.
                await asyncio.to_thread(publish.wait)
                results[index] = await _collect(subscription, 3)
                assert results[index] == [_event(n) for n in range(1, 4)]
                assert await subscription.next_batch(0.02) == []
                await asyncio.to_thread(attached.wait)
            assert session_fanout.poller_count("concurrent-loops") == 0

        try:
            asyncio.run(run())
        except BaseException as error:
            errors[index] = error
            attached.abort()
            publish.abort()

    threads = [threading.Thread(target=client, args=(index,), daemon=True)
               for index in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)
    assert all(not thread.is_alive() for thread in threads)
    for error in errors:
        if error is not None:
            raise error
    assert results == [[_event(n) for n in range(1, 4)]] * 2


def test_poll_loop_stops_when_last_subscriber_leaves():
    async def run():
        log = _Log(1)
        async with session_fanout.subscribe("stop", 0, read=log.read, poll_s=0.01) as first:
            async with session_fanout.subscribe("stop", 0, read=log.read, poll_s=0.01):
                assert await _collect(first, 1) == [_event(1)]
            assert session_fanout.poller_count("stop") == 1
        assert session_fanout.poller_count("stop") == 0
        call_count = len(log.calls)
        await asyncio.sleep(0.02)
        assert len(log.calls) == call_count
        attached = asyncio.Event()

        async def client():
            async with session_fanout.subscribe("cancel", 0, read=log.read, poll_s=0.01):
                attached.set()
                await asyncio.Event().wait()

        task = asyncio.create_task(client())
        await attached.wait()
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        assert session_fanout.poller_count("cancel") == 0

    asyncio.run(run())


def test_slow_subscriber_is_dropped_without_stalling_the_others():
    async def run():
        assert session_fanout.SUBSCRIBER_QUEUE_MAX == 1000
        log = _Log(500)
        async with session_fanout.subscribe("slow", 0, read=log.read, poll_s=0.01) as slow:
            async with session_fanout.subscribe("slow", 0, read=log.read, poll_s=0.01) as fast:
                events = await _collect(fast, 500)
                log.append_through(1000)
                events += await _collect(fast, 500)
                log.append_through(1001)
                events += await _collect(fast, 1)
                assert await slow.next_batch(0.1) is None
                assert session_fanout.poller_count("slow") == 1
                log.append_through(1002)
                events += await _collect(fast, 1)
                assert [ev["seq"] for ev in events] == list(range(1, 1003))
        assert session_fanout.poller_count("slow") == 0

    asyncio.run(run())


def test_read_error_ends_every_subscriber_and_the_poll_loop():
    async def run():
        def broken(*args):
            raise RuntimeError("durable log unavailable")

        async with AsyncExitStack() as stack:
            subscriptions = [await stack.enter_async_context(session_fanout.subscribe(
                "error", 0, read=broken, poll_s=0.01,
            )) for _ in range(3)]
            assert await asyncio.gather(*[sub.next_batch(1) for sub in subscriptions]) == [None] * 3
            assert session_fanout.poller_count("error") == 0
            log = _Log(1)
            async with session_fanout.subscribe("error", 0, read=log.read, poll_s=0.01) as fresh:
                assert await _collect(fresh, 1) == [_event(1)]
                assert session_fanout.poller_count("error") == 1
        assert session_fanout.poller_count("error") == 0

    asyncio.run(run())


def test_stream_route_uses_one_poll_loop_for_concurrent_clients(monkeypatch):
    async def run():
        both_attached = threading.Event()
        attached = 0
        calls = []
        ownership_calls = []
        original_subscribe = session_fanout.subscribe

        @asynccontextmanager
        async def subscribe(*args, **kwargs):
            nonlocal attached
            async with original_subscribe(*args, **kwargs) as subscription:
                attached += 1
                assert session_fanout.poller_count("route") == 1
                if attached == 2:
                    both_attached.set()
                yield subscription

        def read(session_id, after_seq, limit):
            assert both_attached.wait(1)
            calls.append((session_id, after_seq, limit))
            return [_event(after_seq + 1)]

        def require(session_id, tenant):
            ownership_calls.append(tenant)
            return {"session_id": session_id}

        monkeypatch.setattr(session_fanout, "subscribe", subscribe)
        monkeypatch.setattr(sessions_router, "_require_owned_session", require)
        monkeypatch.setattr(sessions_router.session_store, "events_after", read)
        monkeypatch.setattr(sessions_router, "STREAM_POLL_S", 0.01)
        monkeypatch.setattr(sessions_router, "STREAM_PING_S", 10)
        monkeypatch.setattr(sessions_router, "STREAM_DEADLINE_S", 1)

        async def consume(tenant):
            response = await sessions_router.stream_session("route", tenant=tenant)
            frames = []
            try:
                async for frame in response.body_iterator:
                    frames.append(frame)
                    if len(frames) == 3:
                        break
            finally:
                await response.body_iterator.aclose()
            return frames

        frames = await asyncio.wait_for(asyncio.gather(consume("a"), consume("b")), 1.5)
        expected = [f"event: text_delta\ndata: {json.dumps(_event(seq))}\n\n" for seq in range(1, 4)]
        assert frames == [expected, expected]
        assert calls == [("route", cursor, 500) for cursor in range(3)]
        assert ownership_calls.count("a") >= 5
        assert ownership_calls.count("b") >= 5
        assert session_fanout.poller_count("route") == 0

    asyncio.run(run())
