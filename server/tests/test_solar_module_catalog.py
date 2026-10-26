"""Frozen W1C-35 proxy contract, exercised against a streamed fake sizer."""
from copy import deepcopy
import json
import socket
import threading
import time
from types import SimpleNamespace

import pytest
import requests

import envelopes
import solar_module_catalog as catalog
from leaf_cloud_grants import CloudError, CloudGrant
from routers import solar_modules as route
from test_sip_r5_routes import lane  # noqa: F401

BEARER = "fake-private-module-bearer"
BODY = {"schema_version": 1,
        "catalog": {"source": "pvlib.CeCMod", "pvlib_version": "0.13.0",
                    "count": 21500, "sha256": "a" * 64},
        "modules": [{"key": "Acme_400", "stc_w": 400.0, "n_s": 72,
                     "v_oc_ref": 48.1, "length_m": None, "width_m": 1.0}],
        "truncated": False}


class Response:
    def __init__(self, status=200, raw=None):
        self.status_code = status
        self.raw = json.dumps(BODY).encode() if raw is None else raw
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.closed = True

    def iter_content(self, chunk_size):
        for offset in range(0, len(self.raw), chunk_size):
            yield self.raw[offset:offset + chunk_size]


@pytest.fixture
def proxy(lane, monkeypatch, caplog):
    lane.client.app.include_router(route.router)
    with catalog._cache_lock:
        catalog._cache.clear()
    state = SimpleNamespace(lane=lane, grants=[], calls=[], response=Response(), error=None,
                            now=1000.0)
    monkeypatch.setattr(catalog, "_clock", lambda: state.now)

    def resolve(ref, org):
        state.grants.append((ref, org))
        return CloudGrant(org, BEARER)

    def get(url, **kwargs):
        state.calls.append((url, kwargs))
        if state.error is not None:
            raise state.error
        return state.response

    monkeypatch.setattr(catalog.cloud, "resolve_grant", resolve)
    monkeypatch.setattr(catalog.requests, "get", get)
    yield state
    assert BEARER not in caplog.text
    assert BEARER not in repr(catalog._cache)
    with catalog._cache_lock:
        catalog._cache.clear()


def query(proxy, **changes):
    params = {"q": "Acme 400", "grant_ref": "g1", **changes}
    params = {key: value for key, value in params.items() if value is not None}
    response = proxy.lane.client.get("/api/solar/sizing-modules", params=params)
    assert BEARER not in response.text
    return response


def test_valid_query(proxy):
    response = query(proxy, q="  Acme--400  ")
    assert response.status_code == 200
    assert response.json() == BODY
    assert proxy.grants == [("g1", str(proxy.lane.s.org))]
    assert proxy.calls == [(catalog.CATALOG_URL, {
        "params": {"q": "Acme--400", "limit": 20},
        "headers": {"Authorization": "Bearer " + BEARER},
        "timeout": (5, 10), "allow_redirects": False, "stream": True})]
    assert proxy.response.closed


def test_valid_query_with_one_character_token_preserves_wire_query(proxy):
    assert query(proxy, q="a-").status_code == 200
    assert proxy.calls[0][1]["params"]["q"] == "a-"
    assert query(proxy, q="a!").status_code == 200
    assert len(proxy.calls) == 1


@pytest.mark.parametrize("value", [None, "", "a", " " * 3, "x" * 65, "--", "éé",
                                   "a b c d e f g"],
                         ids=["missing", "empty", "short", "space", "long", "punct", "unicode", "terms"])
def test_bad_query(proxy, value):
    response = query(proxy, q=value)
    assert response.status_code == 400
    assert response.json() == envelopes.err_envelope(
        envelopes.ErrorCode.BAD_PARAMS, catalog.QUERY_DETAIL, False)
    assert not proxy.calls and not proxy.grants


@pytest.mark.parametrize("value", ["", "0", "51", "-1", "1.5", "abc", "true", "1e1"],
                         ids=["empty", "zero", "high", "negative", "float", "text", "bool", "exponent"])
def test_bad_limit(proxy, value):
    response = query(proxy, limit=value)
    assert response.status_code == 400
    assert response.json() == envelopes.err_envelope(
        envelopes.ErrorCode.BAD_PARAMS, catalog.LIMIT_DETAIL, False)
    assert not proxy.calls and not proxy.grants


@pytest.mark.parametrize("value", [None, "", "bad.ref", "a" * 65, "a\n"],
                         ids=["missing", "empty", "punct", "long", "newline"])
def test_bad_grant_reference(proxy, value):
    response = query(proxy, grant_ref=value)
    assert response.status_code == 400
    assert response.json()["error"]["error_code"] == "BAD_PARAMS"
    assert not proxy.grants and not proxy.calls


@pytest.mark.parametrize("status,code", [(401, "cloud_auth_missing"),
                                         (403, "cloud_tenant_unauthorized")], ids=["auth", "tenant"])
def test_grant_error(proxy, monkeypatch, status, code):
    def resolve(*args):
        raise CloudError(code, status)
    monkeypatch.setattr(catalog.cloud, "resolve_grant", resolve)
    response = query(proxy)
    assert response.status_code == status
    assert response.json()["error"]["reason_code"] == code.upper()
    assert not proxy.calls and not catalog._cache


@pytest.mark.parametrize("status,expected,code", [
    (401, 401, "cloud_auth_missing"), (403, 403, "cloud_tenant_unauthorized"),
    (500, 502, "cloud_upstream_failure"), (302, 502, "cloud_upstream_failure"),
    (400, 502, "cloud_upstream_failure")], ids=["auth", "tenant", "server", "redirect", "bad-request"])
def test_upstream_status(proxy, status, expected, code):
    proxy.response = Response(status)
    response = query(proxy)
    assert response.status_code == expected
    assert response.json()["error"]["reason_code"] == code.upper()
    assert proxy.response.closed and not catalog._cache


@pytest.mark.parametrize("case", ["oversize", "json", "extra", "catalog-extra", "module-extra",
                                   "many", "sha", "sha-upper", "count", "coercion", "nan",
                                   "missing", "version", "duplicate", "utf8"], ids=str)
def test_invalid_body(proxy, case):
    body = deepcopy(BODY)
    if case == "extra":
        body["extra"] = True
    elif case == "catalog-extra":
        body["catalog"]["extra"] = True
    elif case == "module-extra":
        body["modules"][0]["extra"] = True
    elif case == "many":
        body["modules"] *= 51
    elif case == "sha":
        body["catalog"]["sha256"] = "short"
    elif case == "sha-upper":
        body["catalog"]["sha256"] = "A" * 64
    elif case == "count":
        body["catalog"]["count"] = 1000001
    elif case == "coercion":
        body["modules"][0]["n_s"] = "72"
    elif case == "nan":
        body["modules"][0]["stc_w"] = float("nan")
    elif case == "missing":
        del body["modules"][0]["width_m"]
    elif case == "version":
        body["schema_version"] = True
    raw = {"oversize": b" " * 262145, "json": b"not JSON",
           "duplicate": b'{"schema_version":1,"schema_version":1}', "utf8": b"\xff"}.get(case)
    proxy.response = Response(raw=json.dumps(body).encode() if raw is None else raw)
    response = query(proxy)
    assert response.status_code == 502
    assert response.json()["error"]["reason_code"] == "CLOUD_UPSTREAM_FAILURE"
    assert proxy.response.closed and not catalog._cache


@pytest.mark.parametrize("error,status", [(requests.Timeout(BEARER), 504),
                                         (requests.ConnectionError(BEARER), 502)], ids=["timeout", "connection"])
def test_transport_error(proxy, error, status):
    proxy.error = error
    response = query(proxy)
    assert response.status_code == status
    assert not catalog._cache


def test_transport_exception_text_is_sanitized(proxy):
    proxy.error = requests.ConnectionError(BEARER)
    with pytest.raises(CloudError) as caught:
        catalog.search("org", "Acme 400", "20", "g1")
    assert str(caught.value) == "cloud_upstream_failure"
    assert caught.value.__context__ is None


def test_cache_hit_normalizes_query_and_skips_grant(proxy):
    assert query(proxy).status_code == 200
    assert query(proxy, q=" ACME---400 ", grant_ref="other-ref").json() == BODY
    assert len(proxy.calls) == len(proxy.grants) == 1


def test_cache_is_scoped_by_organization_and_limit(proxy):
    assert query(proxy).status_code == 200
    assert query(proxy, limit="19").status_code == 200
    catalog.search("another-org", "Acme 400", "20", "g1")
    assert len(proxy.calls) == len(proxy.grants) == 3
    assert proxy.grants[-1] == ("g1", "another-org")


def test_cache_expiry(proxy):
    assert query(proxy).status_code == 200
    proxy.now += 599
    assert query(proxy).status_code == 200
    assert len(proxy.calls) == 1
    proxy.now += 1
    assert query(proxy).status_code == 200
    assert len(proxy.calls) == len(proxy.grants) == 2


def test_cache_lru_bound_and_copy(proxy):
    for index in range(256):
        catalog.search("org", f"module {index}", "20", "g1")
    result = catalog.search("org", "module 0", "20", "g1")
    result["modules"].clear()
    catalog.search("org", "module 256", "20", "g1")
    assert len(catalog._cache) == 256
    assert ("org", "module 1", 20) not in catalog._cache
    assert catalog.search("org", "module 0", "20", "g1") == BODY
    assert len(proxy.calls) == 257


def test_body_at_byte_limit(proxy):
    raw = json.dumps(BODY).encode()
    proxy.response = Response(raw=raw + b" " * (262144 - len(raw)))
    assert query(proxy).json() == BODY


def test_overall_deadline(proxy, monkeypatch):
    import threading
    started, release, finished = threading.Event(), threading.Event(), threading.Event()

    def get(*args, **kwargs):
        started.set()
        release.wait(2)
        finished.set()
        return Response()

    monkeypatch.setattr(catalog.requests, "get", get)
    monkeypatch.setattr(catalog, "DEADLINE_SECONDS", 0.05)
    try:
        response = query(proxy)
        assert started.is_set()
        assert response.status_code == 504
        assert not catalog._cache
    finally:
        release.set()
        assert finished.wait(2)


@pytest.mark.parametrize("read_error", [False, True], ids=["eof", "read-error"])
def test_deadline_interrupts_blocked_body_and_releases_slot(proxy, monkeypatch, read_error):
    interrupted = threading.Event()
    released = threading.Event()
    timeouts = []

    class Slots(threading.BoundedSemaphore):
        def release(self):
            super().release()
            released.set()

    class Socket:
        def shutdown(self, how):
            assert how == socket.SHUT_RDWR
            interrupted.set()

        def settimeout(self, value):
            timeouts.append((time.monotonic(), value))

    class DrippingResponse(Response):
        def __init__(self):
            super().__init__()
            self.raw = SimpleNamespace(_fp=SimpleNamespace(
                fp=SimpleNamespace(raw=SimpleNamespace(_sock=Socket()))))

        def close(self):
            self.closed = True

        def iter_content(self, chunk_size):
            yield b"{"
            # Simulate a pending chunk read that only socket shutdown wakes.
            assert interrupted.wait(2)
            if read_error:
                raise requests.ConnectionError(BEARER)

    slots = Slots(1)
    monkeypatch.setattr(catalog, "_transport_slots", slots)
    monkeypatch.setattr(catalog, "DEADLINE_SECONDS", 0.1)
    response = proxy.response = DrippingResponse()
    started = time.monotonic()
    try:
        with pytest.raises(CloudError) as caught:
            catalog._transport("Acme 400", 20, CloudGrant("org", BEARER))
        assert caught.value.status == 504
        assert str(caught.value) == "cloud_upstream_timeout"
        assert released.wait(1)
        assert time.monotonic() - started < 1.1
        assert interrupted.is_set() and response.closed
        assert slots._value == 1
        assert len(timeouts) == 2
        assert all(0 < value <= 0.1 for _, value in timeouts)
        assert timeouts[1][1] < timeouts[0][1]
        assert all(0 < value <= 0.1 for value in proxy.calls[0][1]["timeout"])
        proxy.response = Response()
        assert catalog._transport("Acme 400", 20, CloudGrant("org", BEARER)) == BODY
        assert slots._value == 1
    finally:
        interrupted.set()


def test_demo_organization_resolution(proxy, monkeypatch):
    monkeypatch.setattr(route, "resolve_demo_actor", lambda tenant: SimpleNamespace(org_id="demo-org"))
    assert query(proxy).status_code == 200
    assert proxy.grants == [("g1", "demo-org")]


def test_demo_refusal_precedes_cache(proxy, monkeypatch):
    assert query(proxy).status_code == 200
    def refused(tenant):
        raise route.DemoActorRefused()
    monkeypatch.setattr(route, "resolve_demo_actor", refused)
    assert query(proxy).status_code == 403
    assert len(proxy.calls) == 1
