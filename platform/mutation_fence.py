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
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path


def _fence_open() -> bool:
    """Live cutover state. Unset fence means no cutover is in progress.

    Missing, unreadable, or malformed fence state fails CLOSED.
    """
    fence = os.environ.get("LEAF_DRAWING_MUTATIONS_FENCE_FILE", "").strip()
    if not fence:
        return True
    try:
        return Path(fence).read_text(encoding="utf-8").strip() == "1"
    except (OSError, UnicodeError):
        return False


@contextmanager
def drawing_mutation_commit_guard():
    """Hold the same shared lock used by app and broker drawing commits.

    The cutover control takes the matching exclusive lock before flipping the
    fence, so it waits for every admitted commit and no in-flight write crosses
    the drain.
    """
    fence = os.environ.get("LEAF_DRAWING_MUTATIONS_FENCE_FILE", "").strip()
    if not fence:
        yield _fence_open()
        return
    Path(fence).parent.mkdir(parents=True, exist_ok=True)
    with open(f"{fence}.lock", "a+b") as lock_file:
        try:
            import fcntl  # Linux deployment; unavailable on Windows unit hosts.
        except ImportError:
            yield False
            return
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_SH)
        try:
            yield _fence_open()
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
