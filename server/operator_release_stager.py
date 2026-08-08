"""Operator staging release stager (contract/OPERATOR.md section 4.1,
operator.stage_release_candidate; O6, Wave 3; section 7 production
unreachability).

A DB-free, DARK-by-default side-effect primitive that performs the actual
staging deploy: it registers a new ECS task-definition revision for the
candidate's source SHA and points the STAGING service at it, returning the
PREVIOUS and NEW task-definition revisions so the runbook can record the
documented rollback target. No stager ships in v1, so stage_release_candidate
fails closed with `no_stager` until a deployment registers one out of band.

Production is unreachable here BY CONSTRUCTION: `stage()` refuses any target
that is not non-production, and the shipped stager (there is none in v1) is a
deployment concern that must only ever touch staging. This module never names a
production route or credential, and the runbook additionally pins target to the
catalog's staging-only enum.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, Optional

_NON_PRODUCTION = {"staging", "development"}


class StageError(RuntimeError):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


# Deployment-registered stager: (source_sha, target) -> {"previous_revision",
# "new_revision"}. Left None so staging is dark until a real stager is
# registered out of band.
_STAGER: Optional[Callable[[str, str], Dict[str, Any]]] = None


def register_stager(stager: Optional[Callable[[str, str], Dict[str, Any]]]) -> None:
    """Register (or clear) the deployment staging stager. Out-of-band."""
    global _STAGER
    _STAGER = stager


def has_stager() -> bool:
    """Whether a stager is registered. Lets the runbook fail closed with
    `no_stager` BEFORE consuming the authority, so a dark deployment does not
    spend it."""
    return _STAGER is not None


def stage(source_sha: str, target: str) -> Dict[str, str]:
    """Deploy `source_sha` to the NON-PRODUCTION `target` and return the
    previous and new ECS task-definition revisions. Fail-closed: production
    target, no stager, a stager error, or a malformed stager result all raise a
    fixed value-free StageError. The raise on a stager error happens OUTSIDE the
    except block so no error is chained."""
    if target not in _NON_PRODUCTION:
        raise StageError("production_target_refused")
    if _STAGER is None:
        raise StageError("no_stager")

    result = None
    stager_failed = False
    try:
        result = _STAGER(source_sha, target)
    except BaseException:  # noqa: BLE001 - mask EVERY failure, value-free
        stager_failed = True
    if stager_failed:
        raise StageError("stager_failed")

    # Validate and extract the result shape DEFENSIVELY. A hostile return value
    # (e.g. a dict subclass whose .get/__getitem__ raises a SHA-bearing error)
    # must yield a fixed value-free StageError, never leak it. Any exception
    # during inspection is masked; the raise happens OUTSIDE the except so
    # nothing is chained in __context__/__cause__.
    invalid = False
    previous = new = None
    try:
        if (isinstance(result, dict)
                and isinstance(result.get("previous_revision"), str)
                and isinstance(result.get("new_revision"), str)):
            previous = result["previous_revision"]
            new = result["new_revision"]
        else:
            invalid = True
    except BaseException:  # noqa: BLE001 - mask ANY inspection failure, value-free
        invalid = True
    if invalid:
        raise StageError("stager_result_invalid")
    return {"previous_revision": previous, "new_revision": new}
