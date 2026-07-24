"""Binary acceptance for the APS-to-Leaf callback translation adapter (L3.1).

The strongest check is a ROUND TRIP: an envelope the adapter produces must be
accepted by the REAL callback verifier (callbacks.verify_signature and
callbacks.consume_callback), and a replay of it must be rejected. Every
fail-closed refusal is asserted by its stable reason tag.

Run from ``server/``: ``python -m pytest tests/test_aps_callback_adapter.py -q``.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

SERVER_DIR = Path(__file__).resolve().parent.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))


def _load(mod_file: str, mod_name: str):
    path = SERVER_DIR / "da" / mod_file
    spec = importlib.util.spec_from_file_location(mod_name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


adapter = _load("aps_callback_adapter.py", "leaf_aps_adapter_under_test")
callbacks = _load("callbacks.py", "leaf_callbacks_under_test")

SECRET = b"test-callback-secret"
NOW = 1_700_000_000.0


def _completion(**overrides):
    base = dict(job_id="job-1", workitem_id="wi-1", attempt=2, status="success",
                nonce="nonce-abc", lease_expiry=NOW + 60.0)
    base.update(overrides)
    return adapter.ApsWorkItemCompletion(**base)


@pytest.fixture(autouse=True)
def callback_env(monkeypatch, tmp_path):
    monkeypatch.setenv("LEAF_CALLBACK_SECRET", SECRET.decode())
    monkeypatch.setenv("JOBS_DB", str(tmp_path / "jobs.db"))


def _never(job_id, attempt):
    """Permissive guard for tests whose subject is NOT the duplicate check."""
    return False


def test_translation_without_a_completion_guard_is_refused():
    """An OPTIONAL guard defaulting to None is fail-open for a signing seam: a
    caller who omits it silently regains the duplicate-completion hole. No
    authority, no receipt."""
    with pytest.raises(TypeError):
        adapter.translate(_completion(), b"out", job_attempt=2, secret=SECRET, now=NOW)
    for bad in (None, "nope", 42):
        with pytest.raises(adapter.AdapterError) as excinfo:
            adapter.translate(_completion(), b"out", job_attempt=2, secret=SECRET, now=NOW,
                              seen_completion=bad)
        assert excinfo.value.reason == "no_completion_guard"


def test_a_translated_envelope_verifies_and_consumes_exactly_once():
    output = b'{"strings": 12, "banks": 3}'
    envelope = adapter.translate(_completion(), output, job_attempt=2, secret=SECRET, now=NOW, seen_completion=_never)

    # The real signature verifier accepts it.
    assert callbacks.verify_signature(envelope.body, envelope.timestamp, envelope.nonce,
                                      envelope.signature, SECRET) is True

    # The real consumer accepts it once, binding the job id and the output evidence.
    store = callbacks.CallbackReplayStore()
    result = callbacks.consume_callback(envelope.body, envelope.signature, envelope.timestamp,
                                        envelope.nonce, now=NOW, replay_store=store)
    assert result["ok"] is True
    assert result["job_id"] == "job-1"
    body = json.loads(envelope.body)
    assert body["attempt"] == 2
    assert body["output"]["sha256"] == __import__("hashlib").sha256(output).hexdigest()
    assert body["output"]["size"] == len(output)

    # A replay of the very same envelope is rejected by the durable nonce store.
    replayed = callbacks.consume_callback(envelope.body, envelope.signature, envelope.timestamp,
                                          envelope.nonce, now=NOW, replay_store=store)
    assert replayed["ok"] is False
    assert replayed["reason"] == "replay"


def test_tampering_the_body_breaks_the_signature():
    envelope = adapter.translate(_completion(), b"output", job_attempt=2, secret=SECRET, now=NOW, seen_completion=_never)
    tampered = envelope.body.replace(b'"size":6', b'"size":9')
    assert tampered != envelope.body
    assert callbacks.verify_signature(tampered, envelope.timestamp, envelope.nonce,
                                      envelope.signature, SECRET) is False


def test_headers_carry_the_signed_triple():
    envelope = adapter.translate(_completion(), b"output", job_attempt=2, secret=SECRET, now=NOW, seen_completion=_never)
    headers = envelope.headers()
    assert headers[callbacks.SIGNATURE_HEADER] == envelope.signature
    assert headers[callbacks.TIMESTAMP_HEADER] == envelope.timestamp
    assert headers[callbacks.NONCE_HEADER] == envelope.nonce


@pytest.mark.parametrize("mutate,output,job_attempt,reason", [
    (dict(job_id="  "), b"out", 2, "missing_job"),
    (dict(workitem_id="   "), b"out", 2, "missing_workitem"),
    (dict(status="failed"), b"out", 2, "workitem_not_success"),
    (dict(status="inprogress"), b"out", 2, "workitem_not_success"),
    (dict(), None, 2, "missing_output"),
    (dict(), b"", 2, "missing_output"),
    (dict(attempt=1), b"out", 2, "wrong_attempt"),          # stale retry's late callback
    # `bool` is an int subclass and True == 1, so attempt=True must NOT satisfy
    # job_attempt=1 and sign `"attempt":true`.
    (dict(attempt=True), b"out", 1, "wrong_attempt"),
    (dict(attempt=2.0), b"out", 2, "wrong_attempt"),
    (dict(lease_expiry=NOW - 1.0), b"out", 2, "expired_lease"),
    # NaN defeats comparison (`now > nan` is False), so it must be refused
    # explicitly rather than sailing past the expiry check.
    (dict(lease_expiry=float("nan")), b"out", 2, "expired_lease"),
    (dict(lease_expiry=float("inf")), b"out", 2, "expired_lease"),
    (dict(lease_expiry=None), b"out", 2, "expired_lease"),
    (dict(nonce="   "), b"out", 2, "bad_nonce"),
])
def test_every_fail_closed_mode_refuses_with_its_reason(mutate, output, job_attempt, reason):
    with pytest.raises(adapter.AdapterError) as excinfo:
        adapter.translate(_completion(**mutate), output, job_attempt=job_attempt, secret=SECRET, now=NOW, seen_completion=_never)
    assert excinfo.value.reason == reason


def test_a_non_finite_clock_is_refused():
    with pytest.raises(adapter.AdapterError) as excinfo:
        adapter.translate(_completion(lease_expiry=float("nan")), b"out", job_attempt=2,
                          secret=SECRET, now=float("nan"), seen_completion=_never)
    assert excinfo.value.reason in {"expired_lease", "bad_clock"}


def test_duplicate_completion_guard_is_keyed_on_attempt_not_nonce():
    """The regression this replaces: the guard used to key on (job, nonce), so a
    second delivery of the SAME completion bearing a FRESH nonce produced a
    second envelope the real consumer accepted — completing one attempt twice."""
    completed = {("job-1", 2)}
    for nonce in ("nonce-abc", "nonce-def", "totally-new-nonce"):
        with pytest.raises(adapter.AdapterError) as excinfo:
            adapter.translate(_completion(nonce=nonce), b"out", job_attempt=2, secret=SECRET,
                              now=NOW, seen_completion=lambda job, attempt: (job, attempt) in completed)
        assert excinfo.value.reason == "duplicate_completion", f"nonce {nonce} must not mint a second receipt"
    # A genuinely different ATTEMPT of the same job is a distinct completion.
    envelope = adapter.translate(_completion(attempt=3, nonce="nonce-xyz"), b"out", job_attempt=3,
                                 secret=SECRET, now=NOW,
                                 seen_completion=lambda job, attempt: (job, attempt) in completed)
    assert json.loads(envelope.body)["attempt"] == 3


def test_two_nonces_for_one_attempt_cannot_both_be_consumed():
    """End-to-end proof against the REAL consumer, not just the guard: with the
    adapter as the completion authority, one attempt yields one accepted receipt."""
    completed = set()

    def guard(job, attempt):
        return (job, attempt) in completed

    store = callbacks.CallbackReplayStore()
    accepted = 0
    for nonce in ("nonce-1", "nonce-2"):
        try:
            envelope = adapter.translate(_completion(nonce=nonce), b"out", job_attempt=2,
                                         secret=SECRET, now=NOW, seen_completion=guard)
        except adapter.AdapterError as exc:
            assert exc.reason == "duplicate_completion"
            continue
        result = callbacks.consume_callback(envelope.body, envelope.signature, envelope.timestamp,
                                           envelope.nonce, now=NOW, replay_store=store)
        if result["ok"]:
            accepted += 1
            completed.add(("job-1", 2))
    assert accepted == 1, "one attempt must produce exactly one accepted completion"


def test_a_stale_translated_envelope_is_rejected_by_the_consumer_freshness_window():
    # Adapter stamps produced_at = NOW; consuming far later exceeds max age.
    envelope = adapter.translate(_completion(lease_expiry=NOW + 10_000.0), b"out",
                                 job_attempt=2, secret=SECRET, now=NOW, seen_completion=_never)
    late = callbacks.consume_callback(envelope.body, envelope.signature, envelope.timestamp,
                                      envelope.nonce, now=NOW + 10_000.0,
                                      replay_store=callbacks.CallbackReplayStore())
    assert late["ok"] is False
    assert late["reason"] == "stale_callback"
