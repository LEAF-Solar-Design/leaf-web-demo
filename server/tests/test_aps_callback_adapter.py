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


def _win(job_id, attempt):
    """Reservation that always succeeds, for tests whose subject is NOT the
    duplicate check. Returns True = "you won the claim"."""
    return True


def test_translation_without_a_completion_guard_is_refused():
    """An OPTIONAL guard defaulting to None is fail-open for a signing seam: a
    caller who omits it silently regains the duplicate-completion hole. No
    authority, no receipt."""
    with pytest.raises(TypeError):
        adapter.translate(_completion(), b"out", job_attempt=2, job_workitem_id='wi-1', secret=SECRET, now=NOW)
    for bad in (None, "nope", 42):
        with pytest.raises(adapter.AdapterError) as excinfo:
            adapter.translate(_completion(), b"out", job_attempt=2, job_workitem_id='wi-1', secret=SECRET, now=NOW,
                              reserve_completion=bad)
        assert excinfo.value.reason == "no_completion_guard"


def test_a_translated_envelope_verifies_and_consumes_exactly_once():
    output = b'{"strings": 12, "banks": 3}'
    envelope = adapter.translate(_completion(), output, job_attempt=2, job_workitem_id='wi-1', secret=SECRET, now=NOW, reserve_completion=_win)

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
    envelope = adapter.translate(_completion(), b"output", job_attempt=2, job_workitem_id='wi-1', secret=SECRET, now=NOW, reserve_completion=_win)
    tampered = envelope.body.replace(b'"size":6', b'"size":9')
    assert tampered != envelope.body
    assert callbacks.verify_signature(tampered, envelope.timestamp, envelope.nonce,
                                      envelope.signature, SECRET) is False


def test_headers_carry_the_signed_triple():
    envelope = adapter.translate(_completion(), b"output", job_attempt=2, job_workitem_id='wi-1', secret=SECRET, now=NOW, reserve_completion=_win)
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
        adapter.translate(_completion(**mutate), output, job_attempt=job_attempt, job_workitem_id='wi-1', secret=SECRET, now=NOW, reserve_completion=_win)
    assert excinfo.value.reason == reason


@pytest.mark.parametrize("bad_now", [float("nan"), float("inf"), float("-inf"),
                                    "1700000000", None, True])
def test_a_bad_clock_is_refused_as_bad_clock_specifically(bad_now):
    """The clock is validated BEFORE anything compares against it. A string or
    None clock used to escape as an untagged TypeError out of the lease
    comparison, and inf was mislabelled `expired_lease`. The previous test
    accepted EITHER reason, so deleting the bad_clock check left it green."""
    with pytest.raises(adapter.AdapterError) as excinfo:
        adapter.translate(_completion(), b"out", job_attempt=2, job_workitem_id="wi-1",
                          secret=SECRET, now=bad_now, reserve_completion=_win)
    assert excinfo.value.reason == "bad_clock"


def test_the_receipt_must_name_the_workitem_the_job_dispatched():
    """Nonblank is not enough: a completion for some OTHER WorkItem must not
    close this job."""
    with pytest.raises(adapter.AdapterError) as excinfo:
        adapter.translate(_completion(workitem_id="wi-other"), b"out", job_attempt=2,
                          job_workitem_id="wi-real", secret=SECRET, now=NOW,
                          reserve_completion=_win)
    assert excinfo.value.reason == "wrong_workitem"
    for absent in ("", "   ", None):
        with pytest.raises(adapter.AdapterError) as excinfo:
            adapter.translate(_completion(), b"out", job_attempt=2, job_workitem_id=absent,
                              secret=SECRET, now=NOW, reserve_completion=_win)
        assert excinfo.value.reason == "missing_workitem"


@pytest.mark.parametrize("attempt", [0, -1, -99])
def test_nonpositive_attempts_are_not_real_attempts(attempt):
    """Attempts are 1-based, so 0 and negatives never ran, even when the job
    store agrees with them."""
    with pytest.raises(adapter.AdapterError) as excinfo:
        adapter.translate(_completion(attempt=attempt), b"out", job_attempt=attempt,
                          job_workitem_id="wi-1", secret=SECRET, now=NOW,
                          reserve_completion=_win)
    assert excinfo.value.reason == "wrong_attempt"


def test_an_int_subclass_cannot_lie_about_equality():
    """An int subclass may override __eq__/__ne__ to claim equality it does not
    have. That let attempt=3 satisfy job_attempt=2 and then serialize as 3, so
    the compared value and the signed value disagreed. Both sides are coerced
    with int() now, making them the same number by construction."""

    class TrickInt(int):
        def __eq__(self, other):
            return True

        def __ne__(self, other):
            return False

        def __hash__(self):
            return hash(int(self))

    with pytest.raises(adapter.AdapterError) as excinfo:
        adapter.translate(_completion(attempt=TrickInt(3)), b"out", job_attempt=2,
                          job_workitem_id="wi-1", secret=SECRET, now=NOW,
                          reserve_completion=_win)
    assert excinfo.value.reason == "wrong_attempt"
    # A plain subclass whose value AND serialization are both 2 is legitimate.
    class PlainInt(int):
        pass

    envelope = adapter.translate(_completion(attempt=PlainInt(2)), b"out", job_attempt=2,
                                 job_workitem_id="wi-1", secret=SECRET, now=NOW,
                                 reserve_completion=_win)
    assert json.loads(envelope.body)["attempt"] == 2


def test_two_translations_before_either_receipt_is_recorded_cannot_both_be_accepted():
    """THE RACE a read-only guard cannot close. With `seen_completion` merely
    asking "have you seen this?", both deliveries were translated BEFORE either
    receipt was recorded, and the real consumer accepted BOTH, completing one
    attempt twice. An atomic reservation makes the check and the record one step,
    so the second translation loses the claim."""
    claimed = set()

    def reserve(job_id, attempt):
        key = (job_id, attempt)
        if key in claimed:
            return False
        claimed.add(key)          # claim and record are ONE step
        return True

    envelopes = []
    refusals = []
    for nonce in ("n-1", "n-2"):          # translate BOTH first, record nothing
        try:
            envelopes.append(adapter.translate(
                _completion(nonce=nonce), b"out", job_attempt=2, job_workitem_id="wi-1",
                secret=SECRET, now=NOW, reserve_completion=reserve))
        except adapter.AdapterError as exc:
            refusals.append(exc.reason)

    assert len(envelopes) == 1, "only one translation may win the reservation"
    assert refusals == ["duplicate_completion"]

    store = callbacks.CallbackReplayStore()
    accepted = sum(
        1 for e in envelopes
        if callbacks.consume_callback(e.body, e.signature, e.timestamp, e.nonce,
                                      now=NOW, replay_store=store)["ok"]
    )
    assert accepted == 1, "one attempt must produce exactly one accepted completion"


def test_duplicate_completion_guard_is_keyed_on_attempt_not_nonce():
    """The regression this replaces: the guard used to key on (job, nonce), so a
    second delivery of the SAME completion bearing a FRESH nonce produced a
    second envelope the real consumer accepted — completing one attempt twice."""
    completed = {("job-1", 2)}
    for nonce in ("nonce-abc", "nonce-def", "totally-new-nonce"):
        with pytest.raises(adapter.AdapterError) as excinfo:
            adapter.translate(_completion(nonce=nonce), b"out", job_attempt=2, job_workitem_id='wi-1', secret=SECRET,
                              now=NOW, reserve_completion=lambda job, attempt: (job, attempt) not in completed)
        assert excinfo.value.reason == "duplicate_completion", f"nonce {nonce} must not mint a second receipt"
    # A genuinely different ATTEMPT of the same job is a distinct completion.
    envelope = adapter.translate(_completion(attempt=3, nonce="nonce-xyz"), b"out", job_attempt=3,
                                 job_workitem_id="wi-1", secret=SECRET, now=NOW,
                                 reserve_completion=lambda job, attempt: (job, attempt) not in completed)
    assert json.loads(envelope.body)["attempt"] == 3


def test_two_nonces_for_one_attempt_cannot_both_be_consumed():
    """End-to-end against the REAL consumer, recording each receipt as it is
    accepted. This is the sequential case; the harder pre-record race is covered
    by test_two_translations_before_either_receipt_is_recorded_cannot_both_be_accepted."""
    claimed = set()

    def reserve(job, attempt):
        key = (job, attempt)
        if key in claimed:
            return False
        claimed.add(key)
        return True

    store = callbacks.CallbackReplayStore()
    accepted = 0
    for nonce in ("nonce-1", "nonce-2"):
        try:
            envelope = adapter.translate(_completion(nonce=nonce), b"out", job_attempt=2,
                                         job_workitem_id="wi-1", secret=SECRET, now=NOW,
                                         reserve_completion=reserve)
        except adapter.AdapterError as exc:
            assert exc.reason == "duplicate_completion"
            continue
        result = callbacks.consume_callback(envelope.body, envelope.signature, envelope.timestamp,
                                           envelope.nonce, now=NOW, replay_store=store)
        if result["ok"]:
            accepted += 1
    assert accepted == 1, "one attempt must produce exactly one accepted completion"


def test_a_stale_translated_envelope_is_rejected_by_the_consumer_freshness_window():
    # Adapter stamps produced_at = NOW; consuming far later exceeds max age.
    envelope = adapter.translate(_completion(lease_expiry=NOW + 10_000.0), b"out",
                                 job_attempt=2, job_workitem_id='wi-1', secret=SECRET, now=NOW, reserve_completion=_win)
    late = callbacks.consume_callback(envelope.body, envelope.signature, envelope.timestamp,
                                      envelope.nonce, now=NOW + 10_000.0,
                                      replay_store=callbacks.CallbackReplayStore())
    assert late["ok"] is False
    assert late["reason"] == "stale_callback"
