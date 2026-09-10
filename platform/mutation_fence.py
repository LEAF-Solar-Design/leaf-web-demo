"""Shared EFS fence for canonical drawing authority commits.

The fence FILE is the one live cutover authority every drawing-authority
surface shares: app, broker, and canonical upload import all read the same
path, so draining a storage cutover takes effect on already-running tasks
without waiting for an ECS rollout.

What this module deliberately does NOT read is
``LEAF_DRAWING_MUTATIONS_ENABLED``. That flag is the *authored / checkout /
broker* lane's own deployment default. The canonical upload-import lane is
gated independently by ``LEAF_UPLOAD_IMPORT_MUTATIONS_ENABLED``, which the
caller checks before entering this guard. Folding the authored lane's flag in
here would make an authored-lane drain silently block canonical import too --
the exact lane separation ``test_ready_account_upload_import_and_exact_api_replay``
pins by importing successfully with ``LEAF_DRAWING_MUTATIONS_ENABLED=0``.

A refusal names the fence state that refused it. The platform cannot import
server/write_loop.py, so the reason codes and messages are copied here with
the same strings; server/tests/test_guest_fail_closed.py fails if they drift.
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path
from typing import Dict, Optional

# Stable reason codes, identical to server/write_loop.py. Fence state only:
# never a tenant, drawing, path, or credential. There is no env-disabled code
# because this lane never reads the authored lane's flag.
FENCE_REFUSED_CLOSED = "drawing_mutations_fence_closed"
FENCE_REFUSED_UNREADABLE = "drawing_mutations_fence_unreadable"
FENCE_REFUSED_LOCK_UNAVAILABLE = "drawing_mutations_fence_lock_unavailable"
FENCE_REFUSED_UNATTRIBUTED = "drawing_mutations_refused_unattributed"

FENCE_REFUSAL_MESSAGES: Dict[str, str] = {
    FENCE_REFUSED_CLOSED:
        "drawing mutations are fenced shut for a storage cutover "
        "(LEAF_DRAWING_MUTATIONS_FENCE_FILE does not hold \"1\")",
    FENCE_REFUSED_UNREADABLE:
        "the drawing mutation fence file is unreadable, so mutations fail closed "
        "(LEAF_DRAWING_MUTATIONS_FENCE_FILE)",
    FENCE_REFUSED_LOCK_UNAVAILABLE:
        "the drawing mutation fence lock is unavailable on this host, "
        "so mutations fail closed",
    FENCE_REFUSED_UNATTRIBUTED:
        "drawing mutations were refused by the mutation fence",
}


def fence_refusal_message(reason: Optional[str]) -> str:
    """Public message for a reason code. Total: an unknown code degrades to
    the generic refusal instead of raising, so a 503 never becomes a 500."""
    return FENCE_REFUSAL_MESSAGES.get(
        reason or "", FENCE_REFUSAL_MESSAGES[FENCE_REFUSED_UNATTRIBUTED])


def _fence_refusal() -> Optional[str]:
    """Typed live cutover state: ``None`` when open, else the reason code.

    Unset fence means no cutover is in progress. Missing, unreadable, or
    malformed fence state fails CLOSED.
    """
    fence = os.environ.get("LEAF_DRAWING_MUTATIONS_FENCE_FILE", "").strip()
    if not fence:
        return None
    try:
        state = Path(fence).read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return FENCE_REFUSED_UNREADABLE
    return None if state == "1" else FENCE_REFUSED_CLOSED


def _fence_open() -> bool:
    """Boolean view of ``_fence_refusal``."""
    return _fence_refusal() is None


@contextmanager
def drawing_mutation_refusal_guard():
    """Hold the same shared lock used by app and broker drawing commits.

    Yields ``None`` when the commit is admitted, else the reason code, from
    ONE fence read taken inside the lock. The cutover control takes the
    matching exclusive lock before flipping the fence, so it waits for every
    admitted commit and no in-flight write crosses the drain.
    """
    fence = os.environ.get("LEAF_DRAWING_MUTATIONS_FENCE_FILE", "").strip()
    if not fence:
        yield _fence_refusal()
        return
    Path(fence).parent.mkdir(parents=True, exist_ok=True)
    with open(f"{fence}.lock", "a+b") as lock_file:
        try:
            import fcntl  # Linux deployment; unavailable on Windows unit hosts.
        except ImportError:
            yield FENCE_REFUSED_LOCK_UNAVAILABLE
            return
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_SH)
        try:
            yield _fence_refusal()
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


@contextmanager
def drawing_mutation_commit_guard():
    """Boolean view of ``drawing_mutation_refusal_guard``. A reason code is a
    truthy string, so never read the typed guard's value as a boolean."""
    with drawing_mutation_refusal_guard() as refusal:
        yield refusal is None
