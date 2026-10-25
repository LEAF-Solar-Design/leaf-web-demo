"""Staging machine grants: tenant boundary, bounded minting, and process cache."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import threading
import time
import traceback
from unittest.mock import Mock

import pytest

import leaf_cloud_grants as grants


CLIENT_ID = "private-client-id"
SECRET = "private-client-secret"
TOKEN = "private-access-token"


@pytest.fixture(autouse=True)
def staging(monkeypatch):
    for name, value in {
        "LEAF_CLOUD_GRANT_SOURCE": "m2m-staging",
        "LEAF_RUNTIME_ENV": "staging",
        "LEAF_CLOUD_M2M_GRANTS": "g1=tenant-a,g2=tenant-b",
        "LEAF_CLOUD_M2M_CLIENT_ID": CLIENT_ID,
        "LEAF_CLOUD_M2M_CLIENT_SECRET": SECRET,
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("LEAF_CLOUD_GRANTS_FILE", raising=False)
    grants.reset_m2m_grant_cache()
    yield
    if grants._m2m_worker is not None:
        grants._m2m_worker.join(timeout=1)
        assert not grants._m2m_worker.is_alive()
    grants.reset_m2m_grant_cache()


@pytest.fixture
def transport(monkeypatch):
    response = Mock(status=200)
    response.read1.side_effect = lambda size: body_read(size)
    body = bytearray(json.dumps({"access_token": TOKEN, "token_type": "Bearer",
                                "expires_in": 3600}).encode())

    def body_read(size):
        chunk = bytes(body[:size])
        del body[:size]
        return chunk

    connection = Mock()
    connection.getresponse.return_value = response
    factory = Mock(return_value=connection)
    monkeypatch.setattr(grants.http.client, "HTTPSConnection", factory)

    def set_body(value):
        body[:] = value if isinstance(value, bytes) else json.dumps(value).encode()

    return factory, connection, response, set_body


def refused(call=None, classification="cloud_auth_missing", status=401):
    with pytest.raises(grants.CloudError) as caught:
        (call or (lambda: grants.resolve_grant("g1", "tenant-a")))()
    assert caught.value.classification == classification
    assert caught.value.status == status
    assert caught.value.__context__ is None
    assert caught.value.__cause__ is None
    formatted = "".join(traceback.format_exception(caught.value))
    for private in (CLIENT_ID, SECRET, TOKEN):
        assert private not in str(caught.value)
        assert private not in repr(caught.value)
        assert private not in formatted
    frame = caught.value.__traceback__
    while frame is not None:
        if frame.tb_frame.f_globals.get("__name__") == grants.__name__:
            for value in frame.tb_frame.f_locals.values():
                if isinstance(value, str):
                    assert value not in (CLIENT_ID, SECRET, TOKEN)
        frame = frame.tb_next
    return caught.value


@pytest.mark.parametrize("source", [None, ""], ids=["unset", "empty"])
def test_default_source_keeps_file_behavior(monkeypatch, tmp_path, source):
    if source is None:
        monkeypatch.delenv("LEAF_CLOUD_GRANT_SOURCE")
    else:
        monkeypatch.setenv("LEAF_CLOUD_GRANT_SOURCE", source)
    path = tmp_path / "grants.json"
    path.write_text(json.dumps({"g1": {"tenant_id": "tenant-a", "access_token": TOKEN,
        "audience": "https://api.leafdesign.ai", "expires_at": 2000000000}}))
    monkeypatch.setattr(grants.time, "time", lambda: 1900000000)
    monkeypatch.setenv("LEAF_CLOUD_GRANTS_FILE", str(path))
    assert grants.resolve_grant("g1", "tenant-a").access_token == TOKEN


@pytest.mark.parametrize("name,value", [
    ("LEAF_CLOUD_GRANT_SOURCE", "unknown"),
    ("LEAF_CLOUD_GRANT_SOURCE", "M2M-STAGING"),
    ("LEAF_RUNTIME_ENV", "production"), ("LEAF_RUNTIME_ENV", "Staging"),
    ("LEAF_RUNTIME_ENV", ""),
], ids=["unknown", "uppercase", "production", "mixed-case", "empty"])
def test_source_and_environment_fail_closed(monkeypatch, transport, name, value):
    monkeypatch.setenv(name, value)
    refused()
    transport[0].assert_not_called()


@pytest.mark.parametrize("reference", [None, "", "../g1", "g 1", "a" * 65, 1],
                         ids=["none", "empty", "path", "space", "long", "integer"])
def test_reference_is_checked_first(monkeypatch, transport, reference):
    monkeypatch.setenv("LEAF_CLOUD_GRANT_SOURCE", "unknown")
    refused(lambda: grants.resolve_grant(reference, "tenant-a"))
    transport[0].assert_not_called()


def test_absent_reference_and_tenant_mismatch(transport):
    refused(lambda: grants.resolve_grant("absent", "tenant-a"))
    refused(lambda: grants.resolve_grant("g1", "tenant-b"),
            "cloud_tenant_unauthorized", 403)
    transport[0].assert_not_called()


@pytest.mark.parametrize("raw,reference,tenant", [
    ("g1=tenant-a", "g1", "tenant-a"),
    ("g1=tenant-a,g2=tenant-b", "g2", "tenant-b"),
    ("staging-solver=4a0928c9-f687-50b4-b116-891a36f044b3", "staging-solver",
     "4a0928c9-f687-50b4-b116-891a36f044b3"),
    (",".join(f"g{i}=tenant-a" for i in range(16)), "g15", "tenant-a"),
    ("A_0-" * 16 + "=" + "Z_9-" * 32, "A_0-" * 16, "Z_9-" * 32),
    ("a=Z", "a", "Z"),
], ids=["single", "multiple", "uuid", "max-pairs", "max-lengths", "min-lengths"])
def test_valid_allowlist(monkeypatch, transport, raw, reference, tenant):
    monkeypatch.setenv("LEAF_CLOUD_M2M_GRANTS", raw)
    assert grants.resolve_grant(reference, tenant) == grants.CloudGrant(tenant, TOKEN)
    transport[0].assert_called_once()


@pytest.mark.parametrize("raw", [
    "", "{", '{"' + SECRET, "[]", "null", '{"g1":null}', '{"g1":1}', '{"g1":""}',
    "g1=" + "a" * 129, "bad/key=tenant-a", "a" * 65 + "=tenant-a",
    ",".join(f"g{i}=tenant-a" for i in range(17)),
    " " * 4097, "g1=tenant-a" + "a" * 4096,
    "g1=tenant-a" + " " * 4085 + "é",
    '{"g1":"tenant-a"}', "g1=", "=tenant-a", "g1", "g1=tenant-a=extra",
    "g1==tenant-a", ",g1=tenant-a", "g1=tenant-a,", "g1=tenant-a,,g2=tenant-b",
    ",", "g1=tenant-a,g1=tenant-a", "g1=tenant-a,g1=tenant-b",
    " g1=tenant-a", "g1=tenant-a ", "g1 =tenant-a", "g1= tenant-a",
    "g1=tenant a", "g1=tenant-a, g2=tenant-b", "g1=tenant-a\t", "g1=tenant-a\n",
    "g1=tenant\ra", "g1=tenant\va", "g1=tenant\fa", "g1=tenant\u00a0a",
    "g1=tenant/a", "g1=tenant.a", "g1=ténant", "gé=tenant-a",
    "g1=tenant-a,g2=bad/tenant",
], ids=["empty", "json", "secret", "array", "null", "null-tenant", "int-tenant",
        "empty-tenant", "long-tenant", "bad-key", "long-key", "many-grants",
        "oversize-space", "oversize-pair", "oversize-utf8", "json-object",
        "missing-tenant", "missing-ref", "missing-equals", "extra-equals", "double-equals",
        "leading-comma", "trailing-comma", "double-comma", "empty-items",
        "duplicate-ref", "duplicate-conflict", "leading-space", "trailing-space",
        "ref-space", "tenant-space", "inner-space", "item-space", "tab", "newline",
        "return", "vertical-tab", "form-feed", "unicode-space", "tenant-slash",
        "tenant-dot", "unicode-tenant", "unicode-ref", "bad-later-pair"])
def test_malformed_allowlist(monkeypatch, transport, raw):
    monkeypatch.setenv("LEAF_CLOUD_M2M_GRANTS", raw)
    refused()
    transport[0].assert_not_called()


@pytest.mark.parametrize("name", ["LEAF_CLOUD_M2M_CLIENT_ID", "LEAF_CLOUD_M2M_CLIENT_SECRET"],
                         ids=["id", "secret"])
@pytest.mark.parametrize("value", [None, "", "has space", "tab\t", "newline\n", "a" * 513],
                         ids=["unset", "empty", "space", "tab", "newline", "long"])
def test_invalid_credentials(monkeypatch, transport, name, value):
    if value is None:
        monkeypatch.delenv(name)
    else:
        monkeypatch.setenv(name, value)
    refused()
    transport[0].assert_not_called()


@pytest.mark.parametrize("name", ["LEAF_CLOUD_M2M_CLIENT_ID", "LEAF_CLOUD_M2M_CLIENT_SECRET"],
                         ids=["id", "secret"])
@pytest.mark.parametrize("character", ["\ud800", "\udcff", "\u00e9", "\t", "\x7f", "\x01", " "],
                         ids=["high-surrogate", "surrogateescape", "non-ascii", "tab", "del",
                              "control", "space"])
def test_non_printable_ascii_credentials_are_private(monkeypatch, transport, name, character):
    planted = "planted-private-credential" + character + "suffix"
    # A mapping also models surrogates that the host environment API cannot store.
    monkeypatch.setattr(grants.os, "environ", {**grants.os.environ, name: planted})
    digest = Mock()
    monkeypatch.setattr(grants.hashlib, "sha256", digest)
    error = refused()
    formatted = "".join(traceback.format_exception(error))
    for rendered in (str(error), repr(error), formatted):
        assert planted not in rendered
        assert repr(planted) not in rendered
        assert "planted-private-credential" not in rendered
    digest.assert_not_called()
    transport[0].assert_not_called()
    assert grants._m2m_cache is None


@pytest.mark.parametrize("value", ["!", "~", "!" * 512,
                                  "".join(chr(code) for code in range(0x21, 0x7f))],
                         ids=["min-character", "max-character", "max-length", "all-printable"])
def test_printable_ascii_credentials_are_accepted(monkeypatch, transport, value):
    monkeypatch.setenv("LEAF_CLOUD_M2M_CLIENT_ID", value)
    monkeypatch.setenv("LEAF_CLOUD_M2M_CLIENT_SECRET", value)
    assert grants.resolve_grant("g1", "tenant-a").access_token == TOKEN
    payload = json.loads(transport[1].request.call_args.kwargs["body"])
    assert payload["client_id"] == value
    assert payload["client_secret"] == value


def test_mint_request_and_private_return(monkeypatch, transport):
    monkeypatch.setenv("LEAF_CLOUD_GRANTS_FILE", "/ignored/file")
    result = grants.resolve_grant("g1", "tenant-a")
    assert result == grants.CloudGrant("tenant-a", TOKEN)
    transport[0].assert_called_once()
    assert transport[0].call_args.args == ("leafautomation.us.auth0.com",)
    assert 0 < transport[0].call_args.kwargs["timeout"] <= 10
    args, kwargs = transport[1].request.call_args
    assert args == ("POST", "/oauth/token")
    assert json.loads(kwargs["body"]) == {
        "grant_type": "client_credentials", "client_id": CLIENT_ID,
        "client_secret": SECRET, "audience": "https://api.leafdesign.ai"}
    assert kwargs["headers"] == {"Content-Type": "application/json"}
    for private in (CLIENT_ID, SECRET, TOKEN):
        assert private not in repr(result)
        assert private not in repr(grants._m2m_cache)
    transport[1].close.assert_called_once()
    assert transport[1].debuglevel == 0


@pytest.mark.parametrize("patch", [
    {"access_token": None}, {"access_token": ""}, {"access_token": "bad token"},
    {"access_token": "bad\n"}, {"access_token": "x" * 16385},
    {"token_type": "Basic"}, {"token_type": None},
    {"expires_in": 59}, {"expires_in": 2592001}, {"expires_in": 3600.0},
    {"expires_in": "3600"}, {"expires_in": True}, {"expires_in": None},
], ids=["null-token", "empty-token", "space-token", "newline-token", "long-token",
        "basic-type", "null-type", "short-ttl", "long-ttl", "float-ttl",
        "string-ttl", "bool-ttl", "null-ttl"])
def test_invalid_mint_fields(transport, patch):
    payload = {"access_token": TOKEN, "token_type": "Bearer", "expires_in": 3600}
    payload.update(patch)
    transport[3](payload)
    refused()


@pytest.mark.parametrize("body", [b"{", b"[]", b"null", b"{}", b"\xff", b"x" * 65537],
                         ids=["json", "array", "null", "empty", "utf8", "oversize"])
def test_bad_response_body(transport, body):
    transport[3](body)
    refused()
    assert grants._m2m_cache is None


@pytest.mark.parametrize("status", [201, 301, 400, 401, 500],
                         ids=["201", "301", "400", "401", "500"])
def test_non_200(transport, status):
    transport[2].status = status
    refused()
    transport[2].read1.assert_not_called()


@pytest.mark.parametrize("stage", ["connect", "request", "getresponse", "read1"],
                         ids=["connect", "request", "response", "read"])
@pytest.mark.parametrize("error", [TimeoutError, OSError], ids=["timeout", "os"])
def test_transport_failures_are_private(transport, stage, error):
    target = transport[2] if stage == "read1" else transport[1]
    getattr(target, stage).side_effect = error(SECRET + TOKEN + CLIENT_ID)
    refused()
    assert grants._m2m_cache is None
    transport[1].close.assert_called_once()


@pytest.mark.parametrize("stage", ["constructor", "settimeout", "response_close", "close"],
                         ids=["init", "timeout", "response", "close"])
def test_other_transport_failures_are_private(transport, stage):
    target = {
        "constructor": transport[0],
        "settimeout": transport[1].sock.settimeout,
        "response_close": transport[2].close,
        "close": transport[1].close,
    }[stage]
    target.side_effect = OSError(SECRET + TOKEN + CLIENT_ID)
    refused()
    assert grants._m2m_cache is None


@pytest.mark.parametrize("expires_in", [60, 2592000], ids=["min", "max"])
def test_valid_mint_limits(transport, expires_in):
    transport[3]({"access_token": "x" * 16384, "token_type": "bEaReR",
                  "expires_in": expires_in})
    assert len(grants.resolve_grant("g1", "tenant-a").access_token) == 16384


def test_cache_reuse_and_refresh_boundary(monkeypatch, transport):
    now = [1000.0]
    monkeypatch.setattr(grants.time, "monotonic", lambda: now[0])
    assert grants.resolve_grant("g1", "tenant-a").access_token == TOKEN
    now[0] = 4299.0
    assert grants.resolve_grant("g2", "tenant-b").access_token == TOKEN
    assert transport[0].call_count == 1
    now[0] = 4300.0
    transport[3]({"access_token": "refreshed", "token_type": "Bearer", "expires_in": 3600})
    assert grants.resolve_grant("g1", "tenant-a").access_token == "refreshed"
    assert transport[0].call_count == 2


def test_failed_mint_is_not_cached(transport):
    transport[2].status = 500
    refused()
    transport[2].status = 200
    assert grants.resolve_grant("g1", "tenant-a").access_token == TOKEN
    assert transport[0].call_count == 2


def test_reset_and_client_change_invalidate(monkeypatch, transport):
    payload = {"access_token": TOKEN, "token_type": "Bearer", "expires_in": 3600}
    grants.resolve_grant("g1", "tenant-a")
    monkeypatch.setenv("LEAF_CLOUD_M2M_CLIENT_ID", "another-client")
    transport[3](payload)
    grants.resolve_grant("g1", "tenant-a")
    grants.reset_m2m_grant_cache()
    transport[3](payload)
    grants.resolve_grant("g1", "tenant-a")
    assert transport[0].call_count == 3


def test_secret_rotation_invalidates_cache(monkeypatch, transport):
    grants.resolve_grant("g1", "tenant-a")
    assert grants._m2m_cache.credential_digest == hashlib.sha256(
        (CLIENT_ID + "\0" + SECRET).encode()).hexdigest()
    assert set(vars(grants._m2m_cache)) == {
        "credential_digest", "audience", "token", "expires_at", "refresh_at"}
    grants.resolve_grant("g1", "tenant-a")
    assert transport[0].call_count == 1
    monkeypatch.setenv("LEAF_CLOUD_M2M_CLIENT_SECRET", "rotated-secret")
    transport[3]({"access_token": "rotated-token", "token_type": "Bearer", "expires_in": 3600})
    assert grants.resolve_grant("g1", "tenant-a").access_token == "rotated-token"
    assert grants._m2m_cache.credential_digest == hashlib.sha256(
        (CLIENT_ID + "\0rotated-secret").encode()).hexdigest()
    assert json.loads(transport[1].request.call_args.kwargs["body"])["client_secret"] == "rotated-secret"
    assert grants.resolve_grant("g1", "tenant-a").access_token == "rotated-token"
    assert transport[0].call_count == 2


@pytest.mark.parametrize("success", [True, False], ids=["success", "failure"])
def test_concurrent_callers_mint_once(monkeypatch, transport, success):
    start = threading.Barrier(10)
    all_waiting = threading.Event()
    release = threading.Event()
    count_lock = threading.Lock()
    waiting = [0]
    operation_type = grants._M2MOperation

    class CountedEvent(threading.Event):
        def wait(self, timeout=None):
            with count_lock:
                waiting[0] += 1
                if waiting[0] == 10:
                    all_waiting.set()
            return super().wait(timeout)

    def operation(deadline):
        return operation_type(deadline, done=CountedEvent())

    monkeypatch.setattr(grants, "_M2MOperation", operation)
    transport[1].connect.side_effect = lambda: release.wait(1)
    transport[2].status = 200 if success else 500

    def resolve(_):
        start.wait(timeout=5)
        if success:
            return grants.resolve_grant("g1", "tenant-a")
        return refused()

    with ThreadPoolExecutor(max_workers=10) as pool:
        futures = [pool.submit(resolve, i) for i in range(10)]
        try:
            assert all_waiting.wait(1)
        finally:
            release.set()
        results = [future.result(timeout=1) for future in futures]
    if success:
        assert all(result.access_token == TOKEN for result in results)
    else:
        assert all(result.status == 401 for result in results)
    assert transport[0].call_count == 1
    if not success:
        transport[2].status = 200
        assert grants.resolve_grant("g1", "tenant-a").access_token == TOKEN
        assert transport[0].call_count == 2


def test_total_timeout_budget(monkeypatch, transport):
    now = [1000.0]
    monkeypatch.setattr(grants.time, "monotonic", lambda: now[0])
    transport[1].connect.side_effect = lambda: now.__setitem__(0, 1004.0)
    transport[1].request.side_effect = lambda *a, **kw: now.__setitem__(0, 1008.0)
    transport[1].getresponse.side_effect = lambda: now.__setitem__(0, 1011.0) or transport[2]
    refused()
    assert [call.args[0] for call in transport[1].sock.settimeout.call_args_list] == [6.0, 2.0]
    transport[2].read1.assert_not_called()


def test_timeout_values_share_one_deadline(monkeypatch, transport):
    now = [1000.0]
    monkeypatch.setattr(grants.time, "monotonic", lambda: now[0])

    def advance():
        now[0] += 1

    def create_connection(*args, **kwargs):
        advance()
        return transport[1]

    transport[0].side_effect = create_connection
    transport[1].connect.side_effect = advance
    transport[1].request.side_effect = lambda *args, **kwargs: advance()
    transport[1].getresponse.side_effect = lambda: advance() or transport[2]
    read_body = transport[2].read1.side_effect

    def read(size):
        advance()
        return read_body(size)

    transport[2].read1.side_effect = read
    assert grants.resolve_grant("g1", "tenant-a").access_token == TOKEN
    values = [transport[0].call_args.kwargs["timeout"]]
    values.extend(call.args[0] for call in transport[1].sock.settimeout.call_args_list)
    assert values == [10.0, 8.0, 7.0, 6.0, 5.0]
    assert all(0 < value <= 10 for value in values)
    assert values == sorted(values, reverse=True)


def test_deadline_before_request_fails_closed(monkeypatch, transport):
    now = [1000.0]
    monkeypatch.setattr(grants.time, "monotonic", lambda: now[0])
    transport[1].connect.side_effect = lambda: now.__setitem__(0, 1011.0)
    refused()
    transport[1].request.assert_not_called()
    assert grants._m2m_cache is None


@pytest.mark.parametrize("stage", ["connect", "request", "getresponse", "read1", "close"])
def test_blocked_worker_bounds_callers(monkeypatch, transport, stage):
    monkeypatch.setattr(grants, "_M2M_CALL_TIMEOUT", 0.04)
    entered = threading.Event()
    release = threading.Event()
    target = transport[2] if stage == "read1" else transport[1]
    original = getattr(target, stage).side_effect

    def block(*args, **kwargs):
        entered.set()
        release.wait()
        if original is not None:
            return original(*args, **kwargs)
        if stage == "getresponse":
            return transport[2]

    getattr(target, stage).side_effect = block
    started = time.monotonic()
    try:
        refused()
        assert entered.is_set()
        assert time.monotonic() - started < 0.54
        wait = Mock(wraps=grants._m2m_inflight.done.wait)
        monkeypatch.setattr(grants._m2m_inflight.done, "wait", wait)
        started = time.monotonic()
        refused()
        assert time.monotonic() - started < 0.5
        wait.assert_not_called()
        assert transport[0].call_count == 1
        assert grants._m2m_worker.is_alive()
        assert grants._m2m_worker.daemon
    finally:
        release.set()
        grants._m2m_worker.join(timeout=1)
    assert not grants._m2m_worker.is_alive()


def test_caller_deadline_includes_state_lock(monkeypatch, transport):
    monkeypatch.setattr(grants, "_M2M_CALL_TIMEOUT", 0.03)
    grants._m2m_lock.acquire()
    started = time.monotonic()
    try:
        refused()
        assert time.monotonic() - started < 0.53
    finally:
        grants._m2m_lock.release()
    transport[0].assert_not_called()


def test_short_lived_token_reuses_half_its_life(monkeypatch, transport):
    now = [1000.0]
    monkeypatch.setattr(grants.time, "monotonic", lambda: now[0])
    payload = {"access_token": TOKEN, "token_type": "Bearer", "expires_in": 120}
    transport[3](payload)
    assert grants.resolve_grant("g1", "tenant-a").access_token == TOKEN
    assert grants._m2m_cache.refresh_at == 1060.0
    now[0] = 1059.0
    assert grants.resolve_grant("g1", "tenant-a").access_token == TOKEN
    assert transport[0].call_count == 1
    now[0] = 1061.0
    transport[3](payload)
    assert grants.resolve_grant("g1", "tenant-a").access_token == TOKEN
    assert transport[0].call_count == 2


@pytest.mark.parametrize("rotate", [False, True], ids=["unchanged", "rotated"])
def test_late_worker_publishes_only_current_credentials(monkeypatch, transport, rotate):
    monkeypatch.setattr(grants, "_M2M_CALL_TIMEOUT", 0.04)
    release = threading.Event()
    transport[1].connect.side_effect = lambda: release.wait()
    try:
        refused()
        if rotate:
            monkeypatch.setenv("LEAF_CLOUD_M2M_CLIENT_SECRET", "new-secret")
    finally:
        release.set()
        grants._m2m_worker.join(timeout=1)
    assert not grants._m2m_worker.is_alive()
    assert grants._m2m_inflight is None
    if rotate:
        assert grants._m2m_cache is None
        transport[3]({"access_token": "new-token", "token_type": "Bearer", "expires_in": 3600})
        assert grants.resolve_grant("g1", "tenant-a").access_token == "new-token"
        assert transport[0].call_count == 2
    else:
        assert grants._m2m_cache.token == TOKEN
        assert grants.resolve_grant("g1", "tenant-a").access_token == TOKEN
        assert transport[0].call_count == 1


@pytest.mark.parametrize("body", [
    ('{"access_token":"' + TOKEN + '","secret":"' + SECRET + '"').encode(),
    (SECRET + TOKEN).encode() + b"\xff",
], ids=["json-doc", "unicode-object"])
def test_sensitive_parser_errors_stay_on_worker(transport, body):
    transport[3](body)
    refused()
    assert grants._m2m_cache is None


def test_credentials_are_read_only_on_worker_including_cache_hits(monkeypatch, transport):
    caller = threading.get_ident()
    readers = []

    class Environment(dict):
        def get(self, name, default=None):
            if name in ("LEAF_CLOUD_M2M_CLIENT_ID", "LEAF_CLOUD_M2M_CLIENT_SECRET"):
                readers.append(threading.get_ident())
            return super().get(name, default)

    monkeypatch.setattr(grants.os, "environ", Environment(grants.os.environ))
    assert grants.resolve_grant("g1", "tenant-a").access_token == TOKEN
    assert grants.resolve_grant("g1", "tenant-a").access_token == TOKEN
    assert readers and all(reader != caller for reader in readers)
    assert transport[0].call_count == 1
