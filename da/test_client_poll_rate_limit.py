"""da/test_client_poll_rate_limit.py: WorkItem polling survives 429 and stays under APS 150/min.

APS limits GET workitems/:id to 150 requests/min per app and answers HTTP 429
with Retry-After. `_poll_workitem` used to poll every 2 s per WorkItem and raise
on a 429, so five concurrent pollers failed tenant jobs while the WorkItems kept
running and billing. These tests pin the shared pacer, the 429 handling and the
overall budget.

Pure python: `requests` is stubbed, the clock and sleep are fake, nothing really sleeps.

  cd da && python -m pytest test_client_poll_rate_limit.py -q
"""
import os
import sys
import threading
import time

import pytest
import requests

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

import client  # noqa: E402


class _NoNetwork:
    """Any HTTP verb this suite does not explicitly stub is a loud failure."""

    def _boom(self, *a, **k):
        raise AssertionError("pure test attempted a real network/APS call")

    get = post = put = delete = request = _boom


class _Resp:
    def __init__(self, status_code=200, payload=None, headers=None):
        self.status_code = status_code
        self._payload = payload or {}
        self.headers = headers or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} Error", response=self)

    def json(self):
        return dict(self._payload)


class _FakeClock:
    """Single-threaded fake time: sleep advances `now` and records the request."""

    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += max(0.0, seconds)


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    monkeypatch.setattr(client, "_auth_headers", lambda: {})
    monkeypatch.setattr(client, "requests", _NoNetwork())
    monkeypatch.delenv("APS_POLL_BUDGET_PER_MIN", raising=False)
    client._reset_poll_pacer()
    yield
    client._reset_poll_pacer()


@pytest.fixture
def fake_clock(monkeypatch):
    fc = _FakeClock()
    monkeypatch.setattr(client, "_poll_clock", fc.clock)
    monkeypatch.setattr(client, "_poll_sleep", fc.sleep)
    # Worst case jitter: the top of every range.
    monkeypatch.setattr(client, "_poll_jitter", lambda lo, hi: hi)
    return fc


def _scripted_get(monkeypatch, fc, responses):
    """GET returns `responses` in order (the last one repeats); records GET timestamps."""
    stamps = []
    it = iter(responses)
    state = {"last": None}

    def _get(url, **_kw):
        stamps.append(fc.now)
        try:
            state["last"] = next(it)
        except StopIteration:
            pass
        return state["last"]

    monkeypatch.setattr(client.requests, "get", _get)
    return stamps


def _gaps(stamps):
    return [b - a for a, b in zip(stamps, stamps[1:])]


def test_429_with_retry_after_is_honored_then_success(monkeypatch, fake_clock):
    stamps = _scripted_get(monkeypatch, fake_clock, [
        _Resp(429, headers={"Retry-After": "7"}),
        _Resp(200, {"id": "wi-1", "status": "success"}),
    ])
    out = client._poll_workitem("wi-1")
    assert out["status"] == "success"
    assert len(stamps) == 2
    assert any(7.0 <= s <= 7.7 + 1e-9 for s in fake_clock.sleeps)
    assert 7.0 <= stamps[1] - stamps[0] <= 7.7 + 1e-9


def test_429_without_header_backs_off_and_never_raises(monkeypatch, fake_clock):
    stamps = _scripted_get(monkeypatch, fake_clock, [
        _Resp(429), _Resp(429), _Resp(429),
        _Resp(200, {"id": "wi-2", "status": "success"}),
    ])
    out = client._poll_workitem("wi-2")
    assert out["status"] == "success"
    gaps = _gaps(stamps)
    assert len(gaps) == 3
    for gap, base in zip(gaps, (2.0, 4.0, 8.0)):
        assert base <= gap <= base * 1.1 + 1e-9
    assert gaps[0] < gaps[1] < gaps[2]


def test_http_date_retry_after_falls_back_to_backoff(monkeypatch, fake_clock):
    stamps = _scripted_get(monkeypatch, fake_clock, [
        _Resp(429, headers={"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}),
        _Resp(429, headers={"Retry-After": "-5"}),
        _Resp(200, {"id": "wi-3", "status": "success"}),
    ])
    out = client._poll_workitem("wi-3")
    assert out["status"] == "success"
    gaps = _gaps(stamps)
    assert 2.0 <= gaps[0] <= 2.2 + 1e-9
    assert 4.0 <= gaps[1] <= 4.4 + 1e-9


def test_retry_after_is_capped_at_60s(monkeypatch, fake_clock):
    stamps = _scripted_get(monkeypatch, fake_clock, [
        _Resp(429, headers={"Retry-After": "3600"}),
        _Resp(200, {"id": "wi-4", "status": "success"}),
    ])
    out = client._poll_workitem("wi-4")
    assert out["status"] == "success"
    assert 60.0 <= stamps[1] - stamps[0] <= 66.0 + 1e-9
    assert max(fake_clock.sleeps) <= 66.0 + 1e-9


def test_429_until_budget_returns_timeout_not_exception(monkeypatch, fake_clock):
    stamps = _scripted_get(monkeypatch, fake_clock, [_Resp(429)])
    out = client._poll_workitem("wi-5", timeout_s=30)
    assert isinstance(out, dict)
    assert out["_timeout"] is True
    assert out["_throttled"] is True
    assert out["id"] == "wi-5"
    assert fake_clock.now <= 30 + 66.0
    assert stamps[-1] <= 30 + 66.0


def test_normal_timeout_keeps_the_old_shape(monkeypatch, fake_clock):
    _scripted_get(monkeypatch, fake_clock, [_Resp(200, {"id": "wi-6", "status": "inprogress"})])
    out = client._poll_workitem("wi-6", timeout_s=10)
    assert out["status"] == "inprogress"
    assert out["_timeout"] is True
    assert "_throttled" not in out
    assert fake_clock.now <= 10 + 2.0 * 1.15 + 1.0


def test_non_429_http_error_still_raises(monkeypatch, fake_clock):
    _scripted_get(monkeypatch, fake_clock, [_Resp(500)])
    with pytest.raises(requests.HTTPError):
        client._poll_workitem("wi-7")


def test_429_pushes_the_shared_pacer_for_siblings(monkeypatch, fake_clock):
    """A throttled poller holds the whole process, not just itself."""
    _scripted_get(monkeypatch, fake_clock, [
        _Resp(429, headers={"Retry-After": "10"}),
        _Resp(200, {"id": "wi-8", "status": "success"}),
    ])
    sibling_wait = []
    base_sleep = fake_clock.sleep

    def _sleep(seconds):
        if not sibling_wait:
            # A sibling poller arrives while the first one waits out its 429.
            sibling_wait.append(None)
            before = fake_clock.now
            assert client._POLL_PACER.acquire()
            sibling_wait[0] = fake_clock.now - before
        base_sleep(seconds)

    monkeypatch.setattr(client, "_poll_sleep", _sleep)
    out = client._poll_workitem("wi-8")
    assert out["status"] == "success"
    assert sibling_wait[0] >= 10.0


class _Scheduler:
    """Thread-safe fake time for real threads.

    sleep(s) blocks the caller until fake now reaches its wake time. Fake now
    advances to the earliest pending wake only when every live thread sleeps.
    """

    def __init__(self, live):
        self.cond = threading.Condition()
        self.now = 0.0
        self.live = live
        self.sleeping = {}  # thread ident -> wake time

    def clock(self):
        with self.cond:
            return self.now

    def _advance_if_idle(self):
        if self.sleeping and len(self.sleeping) == self.live:
            earliest = min(self.sleeping.values())
            if earliest > self.now:
                self.now = earliest
            for ident in [i for i, w in self.sleeping.items() if w <= self.now]:
                del self.sleeping[ident]
            self.cond.notify_all()

    def sleep(self, seconds):
        me = threading.get_ident()
        real_deadline = time.monotonic() + 20.0
        with self.cond:
            wake = self.now + max(0.0, seconds)
            if wake <= self.now:
                return
            self.sleeping[me] = wake
            self._advance_if_idle()
            while me in self.sleeping:
                if time.monotonic() > real_deadline:
                    del self.sleeping[me]
                    raise AssertionError("fake scheduler stalled")
                self.cond.wait(timeout=1.0)

    def finished(self):
        with self.cond:
            self.live -= 1
            self._advance_if_idle()


@pytest.mark.parametrize("n", [10, 25])
def test_concurrent_pollers_stay_under_150_per_min(monkeypatch, n):
    sched = _Scheduler(n)
    monkeypatch.setattr(client, "_poll_clock", sched.clock)
    monkeypatch.setattr(client, "_poll_sleep", sched.sleep)
    monkeypatch.setattr(client, "_poll_jitter", lambda lo, hi: (lo + hi) / 2.0)

    stamps = []
    stamps_lock = threading.Lock()

    def _get(url, **_kw):
        now = sched.clock()
        with stamps_lock:
            stamps.append(now)
        wid = url.rsplit("/", 1)[-1]
        return _Resp(200, {"id": wid, "status": "success" if now > 180.0 else "inprogress"})

    monkeypatch.setattr(client.requests, "get", _get)

    results = [None] * n
    errors = []

    def _run(i):
        try:
            results[i] = client._poll_workitem(f"wi-{i}", timeout_s=200)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            sched.finished()

    threads = [threading.Thread(target=_run, args=(i,), daemon=True) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert not any(t.is_alive() for t in threads), "pollers did not finish"
    assert not errors, errors
    assert all(r is not None and r["status"] == "success" for r in results), results

    ts = sorted(stamps)
    worst = 0
    j = 0
    for i, start in enumerate(ts):
        if j < i:
            j = i
        while j + 1 < len(ts) and ts[j + 1] <= start + 60.0:
            j += 1
        worst = max(worst, j - i + 1)
    assert worst <= 150
    assert worst <= 121


@pytest.mark.parametrize("raw, expected", [
    ("9999", 150),
    ("0", 1),
    ("junk", 120),
    ("-3", 1),
    ("90", 90),
])
def test_poll_budget_env_is_clamped(monkeypatch, raw, expected):
    monkeypatch.setenv("APS_POLL_BUDGET_PER_MIN", raw)
    assert client._poll_budget_per_min() == expected


def test_poll_budget_default_is_120(monkeypatch):
    monkeypatch.delenv("APS_POLL_BUDGET_PER_MIN", raising=False)
    assert client._poll_budget_per_min() == 120
