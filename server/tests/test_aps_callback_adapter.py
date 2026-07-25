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

    # DELIBERATE TIGHTENING vs round 3, which accepted a plain int subclass. There
    # is no way to tell a plain subclass from a lying one without trusting a method
    # the subclass controls, and these values come from parsed JSON where exact
    # ints are what actually occur. So EXACT type only.
    class PlainInt(int):
        pass

    with pytest.raises(adapter.AdapterError) as excinfo:
        adapter.translate(_completion(attempt=PlainInt(2)), b"out", job_attempt=2,
                          job_workitem_id="wi-1", secret=SECRET, now=NOW,
                          reserve_completion=_win)
    assert excinfo.value.reason == "wrong_attempt"

    # And the specific bypass round 3 introduced: coercing with int() trusted
    # __int__, so a real value of 7 was signed as attempt 2.
    class LyingInt(int):
        def __int__(self):
            return 2

    with pytest.raises(adapter.AdapterError) as excinfo:
        adapter.translate(_completion(attempt=LyingInt(7)), b"out", job_attempt=2,
                          job_workitem_id="wi-1", secret=SECRET, now=NOW,
                          reserve_completion=_win)
    assert excinfo.value.reason == "wrong_attempt"


def test_a_str_subclass_cannot_turn_a_failure_into_a_success():
    """The sharpest case found so far: a `str` subclass whose strip() returns
    "success" made a FAILED WorkItem emit a success receipt. Every `.strip()` in
    the validator was only as trustworthy as the type it was called on."""

    class LyingStatus(str):
        def strip(self, *args):
            return "success"

    with pytest.raises(adapter.AdapterError) as excinfo:
        adapter.translate(_completion(status=LyingStatus("failed")), b"out", job_attempt=2,
                          job_workitem_id="wi-1", secret=SECRET, now=NOW,
                          reserve_completion=_win)
    assert excinfo.value.reason == "workitem_not_success"


def test_workitem_ids_are_compared_exactly_not_normalized():
    """A WorkItem id is opaque, so normalizing it is a hole rather than leniency:
    strip() removes unicode whitespace, so " wi-1 " and " wi-1" both matched
    "wi-1" while the envelope carried the untrimmed string."""
    for claimed in (" wi-1 ", " wi-1", "wi-1	", "WI-1"):
        with pytest.raises(adapter.AdapterError) as excinfo:
            adapter.translate(_completion(workitem_id=claimed), b"out", job_attempt=2,
                              job_workitem_id="wi-1", secret=SECRET, now=NOW,
                              reserve_completion=_win)
        assert excinfo.value.reason == "wrong_workitem", f"{claimed!r} must not match 'wi-1'"


def test_a_failure_after_validation_does_not_burn_the_attempt():
    """The claim is the LAST gate. It used to be taken before hashing and signing,
    so a str `output` blew up inside sha256 AFTER consuming the identity, and the
    legitimate retry was then refused as duplicate_completion — the job could
    never be completed at all."""
    claimed = set()

    def reserve(job_id, attempt):
        key = (job_id, attempt)
        if key in claimed:
            return False
        claimed.add(key)
        return True

    # A non-bytes output is refused cleanly, and claims nothing.
    with pytest.raises(adapter.AdapterError) as excinfo:
        adapter.translate(_completion(), "not-bytes", job_attempt=2, job_workitem_id="wi-1",
                          secret=SECRET, now=NOW, reserve_completion=reserve)
    assert excinfo.value.reason == "missing_output"
    assert claimed == set(), "a rejected translation must not consume the identity"

    # So the genuine retry still succeeds.
    envelope = adapter.translate(_completion(), b"real-output", job_attempt=2,
                                 job_workitem_id="wi-1", secret=SECRET, now=NOW,
                                 reserve_completion=reserve)
    assert json.loads(envelope.body)["attempt"] == 2
    assert claimed == {("job-1", 2)}


def test_two_translations_before_either_receipt_is_recorded_cannot_both_be_accepted():
    """THE RACE a read-only guard cannot close. With `seen_completion` merely
    asking "have you seen this?", both deliveries were translated BEFORE either
    receipt was recorded, and the real consumer accepted BOTH, completing one
    attempt twice. An atomic reservation makes the check and the record one step,
    so the second translation loses the claim."""
    claimed = set()
    calls = []

    def reserve(job_id, attempt):
        key = (job_id, attempt)
        calls.append(key)
        if key in claimed:
            return False
        claimed.add(key)          # claim and record are ONE step
        return True

    # Record the outcome of each delivery IN ORDER. Order is the discriminator that
    # counts alone could never be: a previous version asserted only "one envelope,
    # one refusal", which is equally true of the old query semantics AND of an
    # inverted gate (`if reserve(...)` instead of `if not reserve(...)`), where the
    # FIRST delivery is refused and the SECOND succeeds. Only correct polarity
    # gives envelope-then-refusal.
    outcomes = []
    envelopes = []
    for nonce in ("n-1", "n-2"):          # translate BOTH first, record nothing
        try:
            env = adapter.translate(
                _completion(nonce=nonce), b"out", job_attempt=2, job_workitem_id="wi-1",
                secret=SECRET, now=NOW, reserve_completion=reserve)
            envelopes.append(env)
            outcomes.append(("envelope", json.loads(env.body)["nonce"]))
        except adapter.AdapterError as exc:
            outcomes.append(("refused", exc.reason))

    assert outcomes == [("envelope", "n-1"), ("refused", "duplicate_completion")], (
        f"the FIRST delivery must win and the SECOND must lose; got {outcomes}. "
        "Reversed order means the reservation gate's polarity is inverted")
    assert len(envelopes) == 1
    # And only a RESERVATION records on the winning call. Under a read-only
    # `seen_completion` the check wrote nothing, so `claimed` would still be empty.
    assert claimed == {("job-1", 2)}, (
        "the winning call must itself have recorded the claim; an empty set means "
        "the guard merely queried and the race is still open")
    assert calls == [("job-1", 2), ("job-1", 2)], "the guard is consulted once per delivery"

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


def test_a_stateful_completion_cannot_swap_identities_after_validation():
    """Validate-then-REREAD, the deepest form of this module's recurring bug. An
    object whose attributes are properties returned `job-1`/`wi-1` to the validator
    and then `victim-job`/`wi-victim` to the payload builder, so a signed receipt
    was emitted AND reserved for a job that was never validated. Every field is
    snapshotted once now, and the payload is built only from those locals."""

    class ShiftingCompletion:
        """First read of each identity field is the honest one; later reads lie."""

        def __init__(self):
            self._job_reads = 0
            self._wi_reads = 0
            self._nonce_reads = 0
            self.attempt = 2
            self.status = "success"
            self.lease_expiry = NOW + 60.0

        @property
        def job_id(self):
            self._job_reads += 1
            return "job-1" if self._job_reads == 1 else "victim-job"

        @property
        def workitem_id(self):
            self._wi_reads += 1
            return "wi-1" if self._wi_reads == 1 else "wi-victim"

        @property
        def nonce(self):
            # Raising on the SECOND read pins the single-read guarantee: any
            # re-read at all, including one after the claim, fails the test.
            self._nonce_reads += 1
            if self._nonce_reads > 1:
                raise RuntimeError("nonce must be read exactly once")
            return "nonce-abc"

    claimed = set()

    def reserve(job_id, attempt):
        key = (job_id, attempt)
        if key in claimed:
            return False
        claimed.add(key)
        return True

    envelope = adapter.translate(ShiftingCompletion(), b"out", job_attempt=2,
                                 job_workitem_id="wi-1", secret=SECRET, now=NOW,
                                 reserve_completion=reserve)
    body = json.loads(envelope.body)
    assert body["job_id"] == "job-1", "the receipt must name the job that was VALIDATED"
    assert body["workitem_id"] == "wi-1"
    assert claimed == {("job-1", 2)}, "the claim key must match the receipt's job"
    assert envelope.nonce == "nonce-abc"
    assert body["nonce"] == "nonce-abc"
