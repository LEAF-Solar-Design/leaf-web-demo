"""Offline authoritative APS completion producer (STU3-APSP).

WHAT THIS IS
------------
The one entry that turns a native APS WorkItem completion plus the caller's
PERSISTED output bytes into the signed Leaf callback envelope, using the job
store as the only authority. It delegates to
``aps_callback_adapter.translate_authoritative``, which reads the job's current
attempt, bound WorkItem and lease, reads any stored receipt, and reserves the
receipt under the job lock.

WHAT THIS IS NOT
----------------
It makes no APS call and downloads nothing: ``output`` must be bytes the caller
already persisted. It does not POST the envelope, settle the job, change its
status or output, wire into the broker, or flip ``LEAF_CALLBACK_PRIMARY``
(callback-primary stays reserved in broker.py). The only write it can cause is
the store's receipt reservation, which inserts one row and never replaces one.

FAIL CLOSED, REASONS UNCHANGED
------------------------------
Every refusal is an ``AdapterError`` whose ``reason`` is exactly the adapter's
or the store's tag (``wrong_attempt``, ``wrong_workitem``, ``missing_workitem``,
``bad_completion_guard``, ``expired_lease``, ``not_running`` and the rest). This
module adds one: ``no_completion_guard`` when the store lacks any of the three
authority methods, so a partial store is a tagged refusal, never an
``AttributeError`` and never a receipt minted without a reservation.

A retry returns the stored receipt verbatim (body, timestamp, nonce, signature),
including after the lease has expired, provided the job is still on the same
attempt and WorkItem binding and the stored receipt verifies as ours.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any, Optional

_ADAPTER = None

# The store surface translate_authoritative calls. Checked up front so a store
# missing the reservation cannot reach any signing step.
_AUTHORITY_METHODS = ("aps_completion_context", "read_aps_completion", "reserve_aps_completion")


def _adapter():
    """Load the sibling adapter by file path, the same way the adapter loads
    callbacks.py, so no sys.path ordering or package layout is assumed."""
    global _ADAPTER
    if _ADAPTER is None:
        path = Path(__file__).resolve().parent / "aps_callback_adapter.py"
        spec = importlib.util.spec_from_file_location("leaf_aps_completion_producer_adapter", path)
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)
        _ADAPTER = module
    return _ADAPTER


# Re-exported so callers catch, build and compare against the SAME classes the
# delegate raises and returns; a second copy of the adapter would mint distinct ones.
AdapterError = _adapter().AdapterError
ApsWorkItemCompletion = _adapter().ApsWorkItemCompletion
CallbackEnvelope = _adapter().CallbackEnvelope


def produce_completion(
    completion: "ApsWorkItemCompletion", output: Optional[bytes], *,
    job_id: str, store: Any, secret: bytes, now: float,
) -> "CallbackEnvelope":
    """Produce the authoritative signed envelope for one APS completion.

    ``output`` is caller-supplied persisted bytes, never downloaded here.
    Raises ``AdapterError`` with the delegate's reason unchanged on any refusal.
    """
    for name in _AUTHORITY_METHODS:
        if not callable(getattr(store, name, None)):
            raise AdapterError("no_completion_guard")
    return _adapter().translate_authoritative(
        completion, output, job_id=job_id, store=store, secret=secret, now=now)
