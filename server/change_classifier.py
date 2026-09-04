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

WHICH REPOSITORY (REQUIRED, NOT INFERRED)
-----------------------------------------
``repo`` is a REQUIRED keyword, and the reason is a real collision rather than
ceremony. Two different repositories are addressed by the same-looking path
strings: the TENANT repository, whose mutability vocabulary lives in
``platform_release_policy.json`` (``tools/**`` tenant_owned, ``config/**``
slushy, ``.github/workflows/**`` and the lockfiles frozen), and THIS platform
repository, whose delivery ladder is ``web/`` / ``server/`` / ``platform/`` /
migrations. They genuinely overlap: ``leaf-web-demo`` has a real tracked
top-level ``tools/`` directory, so ``tools/skills-bundle/build.mjs`` is a
platform build input AND matches the tenant ``tools/**`` fold rule. A single
flat path space answering both would have called that change ``fold`` /
``seconds`` -- a change that needs the image build and the relay, advertised as
landing with no deploy. That is the one OPTIMISTIC error the worst-rank-wins
design exists to prevent, so the discriminator is required and unguessable:

``repo="tenant"``    the tenant repository. ONLY the mutability vocabulary
                     applies. A tenant artifact folds; a frozen path and a path
                     no tenant rule claims are both ``denied``, because a tenant
                     cannot land either.
``repo="platform"``  this repository. ONLY the delivery ladder applies, and the
                     tenant vocabulary is never consulted. ``web/``-only rides
                     the prewarm relay; EVERY other structurally-sound path
                     rides the full relay, because the image build carries the
                     whole tree. That is pessimistic on purpose (a docs-only
                     change does not really need a relay), and pessimism is the
                     side this module already commits to. In the platform repo
                     ``denied`` therefore means one thing only: the path is
                     malformed.

An absent or unknown ``repo`` raises rather than defaulting. A default would be
a guess about which namespace the caller meant, and the collision above is
exactly where a guess goes optimistic.

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

THE FIRST GATE (``repo="tenant"``)
----------------------------------
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
malformed and is ``denied`` for the structural reason. A path that passes it is
simply not tenant-proposable, which in the TENANT repository is still
``denied``: a tenant cannot land a change to a file no tenant rule claims. The
``web/src/App.jsx`` and ``server/app.py`` cases the paragraph above describes
are not tenant changes at all -- they are ``repo="platform"`` changes, and the
delivery ladder, not the tenant vocabulary, is what classifies them.

``repo="platform"`` runs ``_safe_path`` alone and then the ladder. The tenant
policy is not consulted, which is what keeps ``tools/skills-bundle/build.mjs``
out of the fold class it has no business in.

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

# The two path namespaces, named so a caller cannot pass one by accident. There
# is deliberately no default: see WHICH REPOSITORY in the module docstring.
REPO_TENANT = "tenant"
REPO_PLATFORM = "platform"
REPOS = frozenset({REPO_TENANT, REPO_PLATFORM})


class ChangeClassifierError(ValueError):
    """A change-class request is malformed. Nothing malformed gets a class."""


def classify_change(
    paths: Sequence[str],
    kind: Optional[str] = None,
    *,
    repo: Optional[str] = None,
    policy: Optional[PlatformReleasePolicy] = None,
    release_id: Optional[str] = None,
) -> dict[str, str]:
    """Return ``{klass, reason, lands_in, repo}`` for a change of this shape.

    The answer is honest data for a capsule or a build card and NEVER a promise
    that the change will land, or land in that time -- see the module docstring.

    ``repo`` is REQUIRED (``"tenant"`` or ``"platform"``): the two namespaces
    collide on real paths, and defaulting would guess. ``policy`` and
    ``release_id`` are injectable so a caller that already holds a loaded policy
    (and every test) can run this with no I/O at all.
    """
    repo_token = _validated_repo(repo)
    checked = _validated_paths(paths)
    kind_token = _validated_kind(kind)

    if not checked and kind_token != KIND_MARATHON:
        return _result(
            KLASS_DENIED,
            "no paths were supplied, so there is nothing to classify",
            repo_token,
        )

    # The tenant vocabulary is the only thing that needs the policy, so a
    # platform classification stays completely I/O free.
    if repo_token == REPO_TENANT:
        resolved_policy, resolved_release = _resolve_policy(policy, release_id)
    else:
        resolved_policy, resolved_release = None, ""

    # No seed class: every reason a caller reads is a REAL path's sentence, so
    # a set can never be described by a class no path in it actually produced.
    worst: Optional[str] = None
    reason = ""
    for path in checked:
        klass, why = _classify_one(path, repo_token, resolved_policy, resolved_release)
        if worst is None or _RANK[klass] > _RANK[worst]:
            worst, reason = klass, why
            if klass == KLASS_DENIED:
                # Nothing outranks denied; stop walking the rest.
                break

    if worst is None or (kind_token == KIND_MARATHON
                         and _RANK[KLASS_FLEET] > _RANK[worst]):
        # `worst is None` means no paths at all, which the guard above already
        # narrowed to the marathon case.
        worst = KLASS_FLEET
        reason = "a marathon is many rounds of autonomous work, not one delivery"

    return _result(worst, reason, repo_token)


def _classify_one(
    path: str, repo: str, policy: Optional[PlatformReleasePolicy], release_id: str
) -> tuple[str, str]:
    """One path's class and the sentence that says why, in ONE namespace."""
    if repo == REPO_PLATFORM:
        return _classify_platform_path(path)
    assert policy is not None  # _resolve_policy ran for REPO_TENANT
    return _classify_tenant_path(path, policy, release_id)


def _classify_platform_path(path: str) -> tuple[str, str]:
    """This repository's delivery ladder. No tenant vocabulary, ever.

    Pessimistic by construction: the image build carries the whole tree, so the
    only path that reads faster than the full relay is a ``web/``-only one the
    prewarm relay can stage (``STAGE_SERVICES: "web"``). Everything else --
    ``server/``, ``platform/``, a migration, and equally ``engine/``,
    ``scripts/``, ``tools/``, ``docs/``, a lockfile, a workflow -- rides the
    relay. Nothing here is ``denied`` except a malformed path: a real file in
    this repository always lands somehow, and calling it "not allowed" would be
    a lie about the changes that ship every day.
    """
    unsafe = _safe_path(path)
    if unsafe is not None:
        return (
            KLASS_DENIED,
            f"{path!r} is not an acceptable repository path: {unsafe}",
        )
    if path.startswith(_WEB_PREFIXES) and not _is_relay_path(path):
        return (
            KLASS_PREWARM,
            f"{path!r} is web-only, so a reviewed PR is eligible for the "
            f"prewarm relay's web leg",
        )
    return (
        KLASS_FULL_RELAY,
        f"{path!r} is carried by the platform image build, so it rides the "
        f"build, dispatch, reconcile and finalize relay",
    )


def _classify_tenant_path(
    path: str, policy: PlatformReleasePolicy, release_id: str
) -> tuple[str, str]:
    """The tenant repository's mutability vocabulary, and nothing else.

    THE FIRST GATE, in order: the platform mutability policy, then the safety
    half of its own normalizer. A structurally-sound path that no tenant rule
    claims is ``denied``, because a tenant cannot land a change to it -- that is
    a statement about the TENANT repository and says nothing about the platform
    surface of the same name.
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

    # Refused or denied by a TENANT-relative rule, but structurally sound: the
    # tenant simply may not change this file. The delivery ladder is NOT
    # consulted here -- a same-looking path in the platform repository is a
    # different file, and answering for it would be the namespace merge this
    # module's `repo` discriminator exists to prevent.
    return (
        KLASS_DENIED,
        f"no tenant-repository mutability rule claims {path!r}, so no tenant "
        f"change to it can land",
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


def _validated_repo(repo: Any) -> str:
    """The namespace, REQUIRED. Absent is a refusal, never a default."""
    if repo is None:
        raise ChangeClassifierError(
            f"repo is required and must be one of {sorted(REPOS)}: the tenant "
            f"and platform repositories share path shapes, so it cannot be guessed"
        )
    if not isinstance(repo, str) or repo not in REPOS:
        raise ChangeClassifierError(f"repo must be one of {sorted(REPOS)}")
    return repo


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


def _result(klass: str, reason: str, repo: str) -> dict[str, str]:
    # `repo` rides the answer so a rendered class can never be read against the
    # wrong namespace: the same path string means different things in each.
    return {"klass": klass, "reason": reason, "lands_in": LANDS_IN[klass], "repo": repo}


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
    "REPOS",
    "REPO_PLATFORM",
    "REPO_TENANT",
    "classify_change",
]
