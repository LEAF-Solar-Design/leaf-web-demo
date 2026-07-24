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


def test_a_translated_envelope_verifies_and_consumes_exactly_once():
    output = b'{"strings": 12, "banks": 3}'
    envelope = adapter.translate(_completion(), output, job_attempt=2, secret=SECRET, now=NOW)

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
    envelope = adapter.translate(_completion(), b"output", job_attempt=2, secret=SECRET, now=NOW)
    tampered = envelope.body.replace(b'"size":6', b'"size":9')
    assert tampered != envelope.body
    assert callbacks.verify_signature(tampered, envelope.timestamp, envelope.nonce,
                                      envelope.signature, SECRET) is False


def test_headers_carry_the_signed_triple():
    envelope = adapter.translate(_completion(), b"output", job_attempt=2, secret=SECRET, now=NOW)
    headers = envelope.headers()
    assert headers[callbacks.SIGNATURE_HEADER] == envelope.signature
    assert headers[callbacks.TIMESTAMP_HEADER] == envelope.timestamp
    assert headers[callbacks.NONCE_HEADER] == envelope.nonce


@pytest.mark.parametrize("mutate,output,job_attempt,reason", [
    (dict(job_id="  "), b"out", 2, "missing_job"),
    (dict(status="failed"), b"out", 2, "workitem_not_success"),
    (dict(status="inprogress"), b"out", 2, "workitem_not_success"),
    (dict(), None, 2, "missing_output"),
    (dict(), b"", 2, "missing_output"),
    (dict(attempt=1), b"out", 2, "wrong_attempt"),          # stale retry's late callback
    (dict(lease_expiry=NOW - 1.0), b"out", 2, "expired_lease"),
    (dict(nonce="   "), b"out", 2, "bad_nonce"),
])
def test_every_fail_closed_mode_refuses_with_its_reason(mutate, output, job_attempt, reason):
    with pytest.raises(adapter.AdapterError) as excinfo:
        adapter.translate(_completion(**mutate), output, job_attempt=job_attempt, secret=SECRET, now=NOW)
    assert excinfo.value.reason == reason


def test_producer_side_replay_guard_refuses_a_seen_nonce():
    seen = {("job-1", "nonce-abc")}
    with pytest.raises(adapter.AdapterError) as excinfo:
        adapter.translate(_completion(), b"out", job_attempt=2, secret=SECRET, now=NOW,
                          seen_nonce=lambda job, nonce: (job, nonce) in seen)
    assert excinfo.value.reason == "replay"
    # A fresh nonce for the same job is allowed.
    envelope = adapter.translate(_completion(nonce="nonce-def"), b"out", job_attempt=2, secret=SECRET,
                                 now=NOW, seen_nonce=lambda job, nonce: (job, nonce) in seen)
    assert envelope.nonce == "nonce-def"


def test_a_stale_translated_envelope_is_rejected_by_the_consumer_freshness_window():
    # Adapter stamps produced_at = NOW; consuming far later exceeds max age.
    envelope = adapter.translate(_completion(lease_expiry=NOW + 10_000.0), b"out",
                                 job_attempt=2, secret=SECRET, now=NOW)
    late = callbacks.consume_callback(envelope.body, envelope.signature, envelope.timestamp,
                                      envelope.nonce, now=NOW + 10_000.0,
                                      replay_store=callbacks.CallbackReplayStore())
    assert late["ok"] is False
    assert late["reason"] == "stale_callback"
