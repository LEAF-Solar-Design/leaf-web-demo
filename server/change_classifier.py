"""Classify a change set by SHAPE into the ladder that carries it to live.

WHAT THIS IS
------------
Honest data, never a promise. ``classify_change`` answers one question --
"which delivery ladder does a change of this shape ride?" -- from the paths
alone. It does not schedule anything, reserve anything, or observe anything
that has already run. A caller may show the answer beside a change capsule
(slice 9b) or a build card (slice 11) so a person reads "this lands in
seconds / minutes / after the relay" instead of guessing; the answer is a
CLASSIFICATION of the request, not a commitment that the relay will run, that
a runner is free, that the gate will pass, or that the change will land at
all. A relay that queues behind the shared single-slot ECS mutation lock is
still ``full-relay``; a fold whose commit is refused downstream was still
``fold``. Read every ``lands_in`` value as "the ladder for this shape", and
never as an ETA the platform owes anybody.

THE LADDER
----------
``fold``                every changed path is a tenant-repository artifact the
                        tenant may actually change: ``registry.json``, a
                        ``tools/**`` entry, or the ``config/**`` surface-config
                        overlay. The tenant repo is read at call time by
                        ``deps.load_tenant_repo_tools`` (CONTRACT-ADDENDUM
                        15.B/16), so a commit is live to the next request with
                        no deploy at all -> ``seconds``. A FROZEN path other
                        than ``registry.json`` is not a fold: the stage gate
                        refuses it, so this refuses it too.
``speculative-prewarm`` the change reaches ``web/`` and nothing heavier. The
                        prewarm relay stages the web leg of a reviewed PR ahead
                        of merge (``prewarm-staging-cutover.yml``, currently
                        ``STAGE_SERVICES: "web"``) -> ``minutes``.
``full-relay``          the change reaches ``server/``, ``platform/`` or a
                        migration. Those ride the build -> dispatch ->
                        reconcile -> finalize chain behind one shared lock ->
                        ``after the relay``.
``fleet``               the caller declared ``kind == "marathon"``: many-round
                        autonomous work, not a single delivery -> ``a marathon``.
``denied``              the change cannot honestly be said to land: a malformed
                        path, a platform-frozen path inside the tenant
                        repository, or a path no delivery surface claims ->
                        ``not allowed``.

HOW THE CLASSES COMBINE
-----------------------
Every path gets a rank and the WORST rank wins, so a mixed set never reads
faster than its slowest member: ``fold`` < ``speculative-prewarm`` <
``full-relay`` < ``fleet`` < ``denied``. That reproduces every clause of the
ladder above -- all-tenant-artifact sets fold, web-only sets prewarm, anything
touching server/platform/migrations takes the relay -- and it also answers the
mixed sets the clauses leave open, always on the pessimistic side.

THE FIRST GATE
--------------
``platform_release_policy.classify_path`` runs FIRST, over every path, before
any ladder rule is consulted. It rejects a path that is not repository-relative
and canonical (traversal, absolute, backslash, uppercase, non-NFC, empty
segment); such a path is ``denied`` on the spot. For a path it accepts, its
return value IS the tenant-artifact test: ``tenant_owned`` (``tools/**``) and
``slushy`` (``config/**``) fold, and ``frozen`` folds for ``registry.json``
alone -- the one frozen file every staged change must touch. Every OTHER frozen
path (``.github/workflows/**``, ``.aps/**``, ``credentials/**``, the lockfiles)
is ``denied``, because ``customization_service._verify_stage_policy`` refuses
exactly that set with ``frozen_path_changed``: advertising a landing the stage
gate will not allow is the dishonesty this class exists to avoid.

A tenant-relative REFUSAL is not the same as a malformed path, and the
difference is load bearing. ``classify_path`` normalizes with the tenant
repository's conventions, which include "every segment is lowercase" -- a
naming convention for authored tools, not a safety property. Under it
``web/src/App.jsx`` and ``server/app.py`` BOTH refuse: the first for the
uppercase component, the second because no tenant rule claims it. Denying
every such path would call every web and server change "not allowed", which is
a lie about the two classes of change that ship every day.

So the gate runs first and its verdict is read in two steps. A path it accepts
folds. A path it refuses or denies is re-checked against ``_safe_path``, which
carries the SAFETY half of ``normalize_path`` verbatim -- repository-relative,
forward slashes, NFC, no empty segment, no ``.``/``..``, no control bytes --
and drops only the lowercase convention. A path that fails THAT is genuinely
malformed and is ``denied`` on the spot. A path that passes it is simply not
tenant-proposable, so the delivery ladder classifies it; if no delivery surface
claims it either, ``denied`` is the real answer and its reason names the path.

PURITY AND I/O
--------------
This module holds no state, opens no socket, touches no database, and spawns
no process. The one read it delegates is the platform release policy, loaded
through ``platform_release_policy.load_policy``, which deliberately re-reads
its file on every call (stale authorization policy is a security defect). Pass
``policy=`` to keep a call completely I/O free; the tests do.

BOUNDS AND FAILURE MODE
-----------------------
Fails closed on every malformed input: a non-sequence, a non-string member, an
empty member, more than ``MAX_PATHS`` paths, a path over ``MAX_PATH_LENGTH``
bytes, or a ``kind`` that is not a short lowercase token all raise
``ChangeClassifierError`` (a ``ValueError``) rather than returning a class.
Nothing malformed can produce a permissive answer. No allocation is
proportional to anything unbounded: the caller's list is size-checked before it
is walked.
"""
from __future__ import annotations

import os
import re
import unicodedata
from typing import Any, Mapping, Optional, Sequence

import platform_release_policy
from platform_release_policy import (
    DENIED,
    PlatformReleasePolicyError,
    PlatformReleasePolicy,
)


CONTRACT = "leaf.change-class.v1"

# Bounds. A change set wider than this is not a change set a person reads a
# class for; refusing is cheaper and more honest than classifying 100k paths.
MAX_PATHS = 500
MAX_PATH_LENGTH = 400
MAX_KIND_LENGTH = 64

KLASS_FOLD = "fold"
KLASS_PREWARM = "speculative-prewarm"
KLASS_FULL_RELAY = "full-relay"
KLASS_FLEET = "fleet"
KLASS_DENIED = "denied"

# Worst rank wins. Order is the honest severity order, not an enum accident.
_RANK: dict[str, int] = {
    KLASS_FOLD: 1,
    KLASS_PREWARM: 2,
    KLASS_FULL_RELAY: 3,
    KLASS_FLEET: 4,
    KLASS_DENIED: 5,
}

LANDS_IN: Mapping[str, str] = {
    KLASS_FOLD: "seconds",
    KLASS_PREWARM: "minutes",
    KLASS_FULL_RELAY: "after the relay",
    KLASS_FLEET: "a marathon",
    KLASS_DENIED: "not allowed",
}

# The tenant repository's mutability vocabulary, named rather than repeated as
# literals so a policy vocabulary change is one edit here and a failing test,
# not a silent misclassification.
FROZEN = "frozen"
TENANT_REGISTRY = "registry.json"
_FOLDING_MUTABILITY = frozenset({"tenant_owned", "slushy"})

# The delivery surfaces of THIS repository, in the order the ladder consults
# them. `server/` and `platform/` ride the staging relay; so does any path with
# a `migrations/` segment, wherever it lives, because a schema change cannot
# ride a prewarm. `web/` is the only prewarm-eligible surface today.
_RELAY_PREFIXES = ("server/", "platform/")
_MIGRATION_SEGMENT = "migrations"
_WEB_PREFIXES = ("web/",)

_KIND_RE = re.compile(r"^[a-z][a-z0-9-]{0,62}[a-z0-9]$|^[a-z]$")
KIND_MARATHON = "marathon"


class ChangeClassifierError(ValueError):
    """A change-class request is malformed. Nothing malformed gets a class."""


def classify_change(
    paths: Sequence[str],
    kind: Optional[str] = None,
    *,
    policy: Optional[PlatformReleasePolicy] = None,
    release_id: Optional[str] = None,
) -> dict[str, str]:
    """Return ``{klass, reason, lands_in}`` for a change of this shape.

    The answer is honest data for a capsule or a build card and NEVER a promise
    that the change will land, or land in that time -- see the module docstring.

    ``policy`` and ``release_id`` are injectable so a caller that already holds
    a loaded policy (and every test) can run this with no I/O at all.
    """
    checked = _validated_paths(paths)
    kind_token = _validated_kind(kind)

    if not checked and kind_token != KIND_MARATHON:
        return _result(
            KLASS_DENIED,
            "no paths were supplied, so there is nothing to classify",
        )

    resolved_policy, resolved_release = _resolve_policy(policy, release_id)

    worst = KLASS_FOLD if checked else KLASS_FLEET
    reason = (
        "every changed path is a tenant-repository artifact, so the next "
        "request reads it straight from the tenant repository"
        if checked
        else "a marathon is many rounds of autonomous work, not one delivery"
    )
    for path in checked:
        klass, why = _classify_one(path, resolved_policy, resolved_release)
        if _RANK[klass] > _RANK[worst]:
            worst, reason = klass, why
            if klass == KLASS_DENIED:
                # Nothing outranks denied; stop walking the rest.
                break

    if kind_token == KIND_MARATHON and _RANK[KLASS_FLEET] > _RANK[worst]:
        worst = KLASS_FLEET
        reason = "a marathon is many rounds of autonomous work, not one delivery"

    return _result(worst, reason)


def _classify_one(
    path: str, policy: PlatformReleasePolicy, release_id: str
) -> tuple[str, str]:
    """One path's class and the sentence that says why.

    THE FIRST GATE, in order: the platform mutability policy, then the safety
    half of its own normalizer, then the delivery ladder. Nothing reaches the
    ladder that ``_safe_path`` has not accepted.
    """
    try:
        mutability = platform_release_policy.classify_path(policy, release_id, path)
    except PlatformReleasePolicyError:
        # A refusal here may be a tenant NAMING refusal (an uppercase segment)
        # rather than a malformed path, and the policy's own message cannot
        # tell the two apart. `_safe_path` below is what does, and its sentence
        # is the one a reader can act on, so the policy's is deliberately
        # dropped rather than surfaced as if it were the defect.
        mutability = DENIED

    if mutability != DENIED and _folds(path, mutability):
        return (
            KLASS_FOLD,
            f"{path!r} is a tenant-repository artifact ({mutability}), which "
            f"folds into the catalog at read time with no deploy",
        )
    if mutability == FROZEN:
        # A frozen path OTHER than the registry is platform-owned inside the
        # tenant repository (`.github/workflows/**`, `.aps/**`, `credentials/**`,
        # the lockfiles). `customization_service._verify_stage_policy` refuses
        # exactly this set with `frozen_path_changed`, so classifying it as a
        # fold would advertise a landing the stage gate will not allow.
        return (
            KLASS_DENIED,
            f"{path!r} is frozen by the platform release policy, so no tenant "
            f"change to it can land",
        )

    unsafe = _safe_path(path)
    if unsafe is not None:
        # The STRUCTURAL reason wins over the policy's message. A path like
        # `Web/../x` refuses at the gate for its uppercase segment, but the
        # defect a reader must fix is the traversal.
        return (
            KLASS_DENIED,
            f"{path!r} is not an acceptable repository path: {unsafe}",
        )

    # Refused or denied by a TENANT-relative rule, but structurally sound.
    # That is the expected answer for platform-repository code, so hand it to
    # the delivery ladder.
    if _is_relay_path(path):
        return (
            KLASS_FULL_RELAY,
            f"{path!r} is platform code or a migration, so it rides the "
            f"build, dispatch, reconcile and finalize relay",
        )
    if path.startswith(_WEB_PREFIXES):
        return (
            KLASS_PREWARM,
            f"{path!r} is web-only, so a reviewed PR is eligible for the "
            f"prewarm relay's web leg",
        )
    return (
        KLASS_DENIED,
        f"no platform mutability rule and no known delivery surface claims "
        f"{path!r}",
    )


# The safety half of `platform_release_policy.normalize_path`, deliberately
# WITHOUT its tenant-only "every segment is lowercase" convention. Kept in
# lockstep with that function by `test_change_classifier.py`, which asserts
# that every structural refusal there refuses here too.
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:")


def _safe_path(path: str) -> Optional[str]:
    """None when the path is structurally sound, else the refusal sentence."""
    if "\\" in path:
        return "path must use forward slashes"
    if _CONTROL_RE.search(path):
        return "path must not contain control characters"
    if unicodedata.normalize("NFC", path) != path:
        return "path must use NFC Unicode normalization"
    if path.startswith("/") or _WINDOWS_DRIVE_RE.match(path):
        return "path must be repository-relative"
    segments = path.split("/")
    if any(not segment for segment in segments):
        return "path must not contain empty segments"
    if any(segment in {".", ".."} for segment in segments):
        return "path traversal is not allowed"
    return None


def _folds(path: str, mutability: str) -> bool:
    """True when a mutability class means "the tenant may change this and the
    change is live at the next read".

    Mirrors ``customization_service._verify_stage_policy`` exactly:
    ``tenant_owned`` and ``slushy`` paths fold, and of the FROZEN paths only
    ``registry.json`` does -- it is the one frozen file every staged change is
    required to touch. Every other frozen path is refused there with
    ``frozen_path_changed``, so it must not read as a fold here.
    """
    if mutability == FROZEN:
        return path == TENANT_REGISTRY
    return mutability in _FOLDING_MUTABILITY


def _is_relay_path(path: str) -> bool:
    """True for platform code and for a migration anywhere in the tree."""
    if path.startswith(_RELAY_PREFIXES):
        return True
    return _MIGRATION_SEGMENT in path.split("/")[:-1]


def _resolve_policy(
    policy: Optional[PlatformReleasePolicy], release_id: Optional[str]
) -> tuple[PlatformReleasePolicy, str]:
    """The active policy and release, mirroring the customization service.

    Same selection rule as ``customization_service._release``: an explicit
    ``LEAF_PLATFORM_RELEASE`` when set, otherwise the single declared release.
    An ambiguous or unknown release is a refusal, never a guess.
    """
    resolved = policy if policy is not None else platform_release_policy.load_policy()
    if not isinstance(resolved, PlatformReleasePolicy):
        raise ChangeClassifierError("a platform release policy is required")

    if release_id is not None:
        if release_id not in resolved.releases:
            raise ChangeClassifierError("unknown platform release")
        return resolved, release_id

    configured = os.environ.get("LEAF_PLATFORM_RELEASE", "").strip()
    if configured:
        if configured not in resolved.releases:
            raise ChangeClassifierError("unknown platform release")
        return resolved, configured
    if len(resolved.releases) != 1:
        raise ChangeClassifierError("the active platform release is ambiguous")
    return resolved, next(iter(resolved.releases))


def _validated_paths(paths: Any) -> tuple[str, ...]:
    """Bounded, fail-closed validation. Size is checked before anything walks."""
    if isinstance(paths, (str, bytes)) or not isinstance(paths, (list, tuple)):
        raise ChangeClassifierError("paths must be a list of strings")
    if len(paths) > MAX_PATHS:
        raise ChangeClassifierError(f"at most {MAX_PATHS} paths may be classified")
    out: list[str] = []
    for item in paths:
        if not isinstance(item, str) or not item:
            raise ChangeClassifierError("every path must be a non-empty string")
        if len(item) > MAX_PATH_LENGTH:
            raise ChangeClassifierError(
                f"a path may be at most {MAX_PATH_LENGTH} characters"
            )
        out.append(item)
    return tuple(out)


def _validated_kind(kind: Any) -> Optional[str]:
    if kind is None:
        return None
    if not isinstance(kind, str):
        raise ChangeClassifierError("kind must be a string or absent")
    token = kind.strip()
    if not token:
        return None
    if len(token) > MAX_KIND_LENGTH or _KIND_RE.fullmatch(token) is None:
        raise ChangeClassifierError("kind must be a short lowercase token")
    return token


def _result(klass: str, reason: str) -> dict[str, str]:
    return {"klass": klass, "reason": reason, "lands_in": LANDS_IN[klass]}


__all__ = [
    "CONTRACT",
    "ChangeClassifierError",
    "KLASS_DENIED",
    "KLASS_FLEET",
    "KLASS_FOLD",
    "KLASS_FULL_RELAY",
    "KLASS_PREWARM",
    "LANDS_IN",
    "MAX_PATHS",
    "MAX_PATH_LENGTH",
    "classify_change",
]
