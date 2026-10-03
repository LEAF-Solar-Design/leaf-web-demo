"""Binary acceptance for the offline authoritative APS completion producer.

A fake store mirrors ``job_pg_store.PgJobStore``'s three APS completion methods
(context read, receipt read, locked reservation that never replaces a receipt),
so every assertion runs offline: no APS call, no POST, no database. Each refusal
is asserted by its exact reason tag AND by the absence of a receipt insertion.

Run: ``python -P -B -m pytest -q -p no:cacheprovider server/tests/test_aps_completion_producer.py``.
"""
from __future__ import annotations

import copy
import importlib.util
import json
import math
from pathlib import Path

import pytest

SERVER_DIR = Path(__file__).resolve().parent.parent


def _load(mod_file: str, mod_name: str):
    path = SERVER_DIR / "da" / mod_file
    spec = importlib.util.spec_from_file_location(mod_name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


producer = _load("aps_completion_producer.py", "leaf_aps_completion_producer_under_test")
callbacks = _load("callbacks.py", "leaf_aps_producer_callbacks_under_test")

SECRET = b"test-producer-secret"
NOW = 1_700_000_000.0
OUTPUT = b'{"strings": 12, "banks": 3}'


class FakeStore:
    """In-memory twin of the PgJobStore APS completion surface.

    ``clock`` stands in for the database's clock_timestamp(). ``inserts`` counts
    receipt insertions, the only write a reservation may make.
    """

    def __init__(self, *, attempt=2, workitem_attempt=2, workitem_id="wi-1",
                 lease_expires_at=NOW + 60.0, status="running", progress="running",
                 lease_owner="worker-1"):
        self.job = {
            "job_id": "job-1", "attempt": attempt, "aps_workitem_id": workitem_id,
            "aps_workitem_attempt": workitem_attempt, "lease_expires_at": lease_expires_at,
            "status": status, "progress": progress, "lease_owner": lease_owner,
            "output": None,
        }
        self.receipts = {}
        self.inserts = 0
        self.reserve_calls = 0
        self.clock = NOW

    def aps_completion_context(self, job_id):
        if job_id != self.job["job_id"]:
            return None
        return {
            "job_id": self.job["job_id"], "attempt": self.job["attempt"],
            "workitem_id": self.job["aps_workitem_id"],
            "workitem_attempt": self.job["aps_workitem_attempt"],
            "lease_expires_at": self.job["lease_expires_at"],
        }

    def read_aps_completion(self, job_id, attempt):
        row = self.receipts.get((job_id, attempt))
        return None if row is None else {**row, "body": bytes(row["body"])}

    def reserve_aps_completion(self, job_id, attempt, workitem_id, envelope):
        self.reserve_calls += 1
        row = self.job if job_id == self.job["job_id"] else None
        if row is None:
            raise ValueError("missing_job")
        if type(attempt) is not int or attempt < 1 or row["attempt"] != attempt:
            raise ValueError("wrong_attempt")
        if row["aps_workitem_attempt"] != attempt or row["aps_workitem_id"] != workitem_id:
            raise ValueError("wrong_workitem")
        existing = self.receipts.get((job_id, attempt))
        if existing is not None:
            return {**existing, "body": bytes(existing["body"])}
        if row["status"] != "running" or row["progress"] == "closed":
            raise ValueError("not_running")
        expiry = row["lease_expires_at"]
        if (not row["lease_owner"] or expiry is None or not math.isfinite(expiry)
                or expiry <= self.clock):
            raise ValueError("expired_lease")
        payload = json.loads(envelope.body)
        if (payload.get("job_id") != job_id or type(payload.get("attempt")) is not int
                or payload["attempt"] != attempt or payload.get("workitem_id") != workitem_id):
            raise ValueError("bad_completion_guard")
        saved = {
            "job_id": job_id, "attempt": attempt, "workitem_id": workitem_id,
            "body": bytes(envelope.body), "timestamp": envelope.timestamp,
            "nonce": envelope.nonce, "signature": envelope.signature,
        }
        self.receipts[(job_id, attempt)] = saved
        self.inserts += 1
        return dict(saved)


def _completion(**overrides):
    base = dict(job_id="job-1", workitem_id="wi-1", attempt=2, status="success",
                nonce="nonce-abc", lease_expiry=NOW + 60.0)
    base.update(overrides)
    return producer.ApsWorkItemCompletion(**base)


def _produce(store, completion=None, output=OUTPUT, *, now=NOW, job_id="job-1"):
    return producer.produce_completion(
        completion if completion is not None else _completion(), output,
        job_id=job_id, store=store, secret=SECRET, now=now)


def _fields(envelope):
    return (envelope.body, envelope.timestamp, envelope.nonce, envelope.signature)


def _refused(store, reason, *args, **kwargs):
    with pytest.raises(producer.AdapterError) as excinfo:
        _produce(store, *args, **kwargs)
    assert excinfo.value.reason == reason
    return excinfo.value


def test_retry_returns_the_original_signed_envelope_verbatim_after_lease_expiry():
    store = FakeStore()
    job_before = copy.deepcopy(store.job)
    first = _produce(store)
    assert callbacks.verify_signature(
        first.body, first.timestamp, first.nonce, first.signature, SECRET) is True
    body = json.loads(first.body)
    assert (body["job_id"], body["workitem_id"], body["attempt"]) == ("job-1", "wi-1", 2)
    assert body["output"]["size"] == len(OUTPUT)
    assert store.inserts == 1
    stored = store.receipts[("job-1", 2)]
    assert (stored["body"], stored["timestamp"], stored["nonce"], stored["signature"]) == _fields(first)

    # Redelivery with a fresh delivery nonce, after the lease has expired: the
    # stored receipt comes back byte for byte and nothing new is inserted.
    store.clock = NOW + 600
    second = _produce(store, _completion(nonce="redelivery"), now=NOW + 600)
    assert _fields(second) == _fields(first)
    assert store.inserts == 1
    # The producer wrote only the receipt: job status, output and lease untouched.
    assert store.job == job_before


@pytest.mark.parametrize("attempt", [1, 3])
def test_a_wrong_attempt_is_refused_without_a_receipt(attempt):
    store = FakeStore()
    _refused(store, "wrong_attempt", _completion(attempt=attempt))
    assert store.inserts == 0 and store.receipts == {}


def test_a_reclaim_between_context_read_and_reservation_is_wrong_attempt():
    class Reclaimed(FakeStore):
        def reserve_aps_completion(self, job_id, attempt, workitem_id, envelope):
            self.job["attempt"] = 3  # reclaimed under the lock the real store takes
            return super().reserve_aps_completion(job_id, attempt, workitem_id, envelope)

    store = Reclaimed()
    _refused(store, "wrong_attempt")
    assert store.inserts == 0 and store.receipts == {}


@pytest.mark.parametrize("rebound_workitem, reason", [
    ("wi-1", "wrong_attempt"),     # attempt 3 rebound to the same WorkItem id
    ("wi-3", "wrong_workitem"),    # attempt 3 bound to a new WorkItem (checked first)
])
def test_a_stored_receipt_cannot_be_recovered_once_the_attempt_advances(rebound_workitem, reason):
    store = FakeStore()
    _produce(store)
    store.job.update(attempt=3, aps_workitem_attempt=3, aps_workitem_id=rebound_workitem)
    _refused(store, reason, now=NOW + 5)
    assert store.inserts == 1 and list(store.receipts) == [("job-1", 2)]


def _tamper_body(row):
    payload = json.loads(row["body"])
    payload["output"]["size"] += 1
    row["body"] = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()


def _resign_with_foreign_secret(row):
    # Body untouched, so only the signature check can refuse it.
    row["signature"] = callbacks.sign_payload(
        row["body"], row["timestamp"], row["nonce"], b"attacker-secret")


@pytest.mark.parametrize("tamper", [
    _tamper_body,
    _resign_with_foreign_secret,
    lambda row: row.update(timestamp=repr(NOW + 1.0)),
    lambda row: row.update(nonce="2:forged"),
    lambda row: row.update(signature="sha256=" + "0" * 64),
    lambda row: row.update(body=b"not json"),
], ids=["body", "foreign-secret", "timestamp", "nonce", "signature", "garbage-body"])
def test_a_tampered_recovered_envelope_is_refused(tamper):
    store = FakeStore()
    _produce(store)
    row = store.receipts[("job-1", 2)]
    tamper(row)
    tampered = dict(row)
    _refused(store, "bad_completion_guard", _completion(nonce="retry"), now=NOW + 5)
    # Refused, never replaced and never re-inserted.
    assert store.inserts == 1 and store.receipts[("job-1", 2)] == tampered


@pytest.mark.parametrize("workitem_attempt, workitem_id, reason", [
    (None, None, "wrong_workitem"),        # attempt never bound
    (1, "wi-1", "wrong_workitem"),         # binding left over from an older attempt
    (2, "", "missing_workitem"),           # bound attempt, empty WorkItem id
    (2, None, "missing_workitem"),         # bound attempt, no WorkItem id
    (2, "wi-other", "wrong_workitem"),     # bound to a different WorkItem
])
def test_a_missing_or_foreign_binding_is_refused_without_a_receipt(
        workitem_attempt, workitem_id, reason):
    store = FakeStore(workitem_attempt=workitem_attempt, workitem_id=workitem_id)
    _refused(store, reason)
    assert store.inserts == 0 and store.receipts == {} and store.reserve_calls == 0


def test_job_identity_refusals():
    store = FakeStore()
    _refused(store, "missing_job", job_id="job-unknown")
    _refused(store, "wrong_job", _completion(job_id="job-2"))
    assert store.inserts == 0 and store.reserve_calls == 0


@pytest.mark.parametrize("setup, kwargs, reason", [
    (dict(status="complete"), {}, "not_running"),
    (dict(progress="closed"), {}, "not_running"),
    (dict(lease_owner=None), {}, "expired_lease"),
    (dict(lease_expires_at=NOW), {}, "expired_lease"),
    ({}, dict(now=float("nan")), "bad_clock"),
    ({}, dict(output=b""), "missing_output"),
    ({}, dict(output=None), "missing_output"),
    ({}, dict(completion=_completion(status="failedInstructions")), "workitem_not_success"),
    ({}, dict(completion=_completion(nonce="bad\r\nX-Evil: 1")), "bad_nonce"),
    ({}, dict(completion=_completion(lease_expiry="soon")), "malformed_lease"),
    ({}, dict(completion=_completion(workitem_id="w" * 513)), "field_too_long"),
])
def test_refusals_propagate_their_reason_unchanged_without_a_receipt(setup, kwargs, reason):
    store = FakeStore(**setup)
    _refused(store, reason, **kwargs)
    assert store.inserts == 0 and store.receipts == {}


def test_the_store_lease_check_under_the_lock_is_propagated():
    store = FakeStore()
    store.clock = NOW + 61  # the context read looked live; the locked row is not
    _refused(store, "expired_lease")
    assert store.reserve_calls == 1 and store.inserts == 0


def test_a_store_without_the_reservation_authority_is_refused():
    class NoReserve:
        def aps_completion_context(self, job_id):
            raise AssertionError("must refuse before reading authority")

        def read_aps_completion(self, job_id, attempt):
            raise AssertionError("must refuse before reading authority")

    _refused(NoReserve(), "no_completion_guard")
    _refused(object(), "no_completion_guard")


def test_reexported_types_are_the_ones_the_delegate_uses():
    store = FakeStore()
    envelope = _produce(store)
    assert type(envelope) is producer.CallbackEnvelope
    assert issubclass(producer.AdapterError, Exception)
