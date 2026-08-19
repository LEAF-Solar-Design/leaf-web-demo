"""Template version pinning + exact-version receipts (``solar_template_beta``).

A template is a versioned, read-only starter config for a solar CAD project.
Every read or clone names the EXACT version served -- "latest" is resolved
ONCE, in :func:`resolve_version`, into a concrete version string, so a
receipt never carries the word "latest" and two callers of the same
unpinned read at different times can tell whether they actually got the
same content.

Every read verifies the served content's digest against the digest frozen
at registration time (:func:`verify_digest`) BEFORE the content or its
receipt reaches a caller. A version whose content and frozen digest have
drifted apart fails closed with :class:`TemplateDigestMismatchError` rather
than silently shipping unverified bytes.

``migration: false`` on this card -- there is no database table here. The
registry is a fixed, in-process catalog, the same shape as
``capability_families.json`` (see ``catalog.py``) but for templates.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import threading
import uuid
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from customization_authority import AuthorityError, TENANT_ROLES, TenantBinding

FLAG_SOLAR_TEMPLATE_BETA = "LEAF_SOLAR_TEMPLATE_BETA_ENABLED"


def solar_template_beta_enabled() -> bool:
    """The ``solar_template_beta`` flag. Off by default; checked before any read."""
    return os.environ.get(FLAG_SOLAR_TEMPLATE_BETA, "").strip().lower() in {
        "1", "true", "yes", "on",
    }


class TemplateNotFoundError(LookupError):
    """Unknown ``template_id``."""


class TemplateVersionNotFoundError(LookupError):
    """A known ``template_id`` has no such version."""


class TemplateDigestMismatchError(RuntimeError):
    """The content actually served does not hash to its frozen registry digest."""


class TemplateBetaDisabledError(RuntimeError):
    """``solar_template_beta`` is off; the fence for any project clone write."""


class ProjectTemplateCloneNotFoundError(LookupError):
    """Unknown ``clone_id`` (or one that does not belong to the caller's scope)."""


class ProjectTemplateCloneWriteValidationError(ValueError):
    """A content-update payload violates the shape/size caps. No write lands."""


class ProjectTemplateCloneConflictError(RuntimeError):
    """``expected_content_version`` is stale: another write already landed. No write lands."""


class ProjectTemplateCloneWriteTimeoutError(RuntimeError):
    """The write could not enter its atomic section within the statement timeout.

    This bounds only THIS call's attempt to acquire the instance's write
    section -- never a process-wide/session-level setting -- so it fires even
    when nothing else is ever slow, and never leaks into unrelated calls."""


def content_digest(content: Dict[str, Any]) -> str:
    """sha256 hex digest of the canonical JSON encoding of template content.

    Same canonicalization ``solar_cad_template_manifest.py`` uses (sorted
    keys, no extra whitespace), so two equal-content dicts always hash
    identically regardless of key insertion order.
    """
    canonical = json.dumps(content, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class TemplateVersion:
    template_id: str
    version: str
    content: Dict[str, Any]
    digest: str  # frozen at registration time; a read recomputes and compares, never trusts this alone


@dataclass(frozen=True)
class Template:
    template_id: str
    title: str
    versions: Dict[str, TemplateVersion]  # version string -> TemplateVersion; insertion order = release order
    latest_version: str


def _register(template_id: str, title: str,
              contents_by_version: "Dict[str, Dict[str, Any]]") -> Template:
    versions: Dict[str, TemplateVersion] = {}
    for version, content in contents_by_version.items():
        versions[version] = TemplateVersion(
            template_id=template_id, version=version, content=content,
            digest=content_digest(content),
        )
    if not versions:
        raise ValueError(f"template {template_id!r} must declare at least one version")
    latest = next(reversed(list(versions)))
    return Template(template_id=template_id, title=title, versions=versions,
                    latest_version=latest)


_REGISTRY: Dict[str, Template] = {
    t.template_id: t for t in (
        _register(
            "rooftop-standard-string", "Rooftop standard string layout",
            {
                "1.0.0": {
                    "array_type": "rooftop",
                    "default_tilt_deg": 20,
                    "default_azimuth_deg": 180,
                    "string_length_max": 12,
                    "setback_ft": 3,
                },
                "1.1.0": {
                    "array_type": "rooftop",
                    "default_tilt_deg": 20,
                    "default_azimuth_deg": 180,
                    "string_length_max": 14,
                    "setback_ft": 3,
                    "fire_setback_note": "3 ft ridge/eave setback per local AHJ",
                },
            },
        ),
        _register(
            "ground-mount-fixed-tilt", "Ground mount, fixed tilt",
            {
                "1.0.0": {
                    "array_type": "ground_mount",
                    "default_tilt_deg": 25,
                    "default_azimuth_deg": 180,
                    "row_spacing_ft": 15,
                    "post_embed_depth_ft": 4,
                },
            },
        ),
    )
}


def list_templates() -> List[Dict[str, Any]]:
    """All registered templates, each naming its exact known version strings."""
    return [
        {
            "template_id": template.template_id,
            "title": template.title,
            "latest_version": template.latest_version,
            "versions": list(template.versions.keys()),
        }
        for template in _REGISTRY.values()
    ]


def _get_template(template_id: str) -> Template:
    template = _REGISTRY.get(template_id)
    if template is None:
        raise TemplateNotFoundError(template_id)
    return template


def resolve_version(template_id: str, version: Optional[str]) -> TemplateVersion:
    """Resolve a caller's request to one EXACT, named :class:`TemplateVersion`.

    ``version=None`` means "the current latest" -- resolved HERE, once, into
    a concrete version string, so every downstream caller (a receipt, a
    clone) works from the same pinned identity rather than re-resolving
    "latest" against a registry that could differ between calls.
    """
    template = _get_template(template_id)
    exact = version or template.latest_version
    resolved = template.versions.get(exact)
    if resolved is None:
        raise TemplateVersionNotFoundError(f"{template_id}@{exact}")
    return resolved


def verify_digest(template_version: TemplateVersion) -> None:
    """Recompute the content digest and compare it to the frozen registry digest.

    Raises :class:`TemplateDigestMismatchError` on any mismatch -- the caller
    never receives content whose digest it cannot independently reproduce.
    """
    recomputed = content_digest(template_version.content)
    if recomputed != template_version.digest:
        raise TemplateDigestMismatchError(
            f"{template_version.template_id}@{template_version.version}: "
            f"recomputed digest {recomputed!r} != registry digest "
            f"{template_version.digest!r}"
        )


def build_receipt(template_version: TemplateVersion, action: str) -> Dict[str, Any]:
    """A receipt names the exact (template_id, version, content_digest) served."""
    return {
        "template_id": template_version.template_id,
        "version": template_version.version,
        "content_digest": template_version.digest,
        "action": action,
    }


@dataclass(frozen=True)
class TemplateReadResult:
    template_id: str
    version: str
    content: Dict[str, Any]
    receipt: Dict[str, Any]


def read_template(template_id: str, version: Optional[str] = None) -> TemplateReadResult:
    """Read one exact version's content, digest-verified before it is returned."""
    resolved = resolve_version(template_id, version)
    verify_digest(resolved)
    receipt = build_receipt(resolved, action="read")
    return TemplateReadResult(
        template_id=resolved.template_id, version=resolved.version,
        content=dict(resolved.content), receipt=receipt,
    )


@dataclass(frozen=True)
class TemplateCloneResult:
    template_id: str
    version: str
    content: Dict[str, Any]
    receipt: Dict[str, Any]


def clone_template(template_id: str, version: Optional[str] = None) -> TemplateCloneResult:
    """Clone one exact pinned version.

    Reuses :func:`read_template`'s digest-verified path -- a clone is never
    built from content whose digest was not just independently verified.
    """
    read = read_template(template_id, version)
    receipt = dict(read.receipt, action="clone")
    return TemplateCloneResult(
        template_id=read.template_id, version=read.version,
        content=read.content, receipt=receipt,
    )


# --------------------------------------------------------------------------- #
# Project-owned template clone (card C2-3)
#
# ``clone_template_for_project`` lands an ISOLATED copy bound to one
# (tenant_id, project_id) pair. The caller must pass a verified
# ``customization_authority.TenantBinding`` naming the caller's role for the
# EXACT ``tenant_id`` being cloned into -- ``_authorize_project_clone`` below
# is a real, enforced check (not a docstring promise) that mirrors
# ``CustomizationAuthority.authorize_stage``'s pattern: a verified binding
# whose role is ``owner``/``editor`` and whose tenant matches the requested
# tenant EXACTLY. A caller can never mint a clone bound to a tenant/project
# other than the one their own binding names.
#
# Isolation is by ``copy.deepcopy`` of the SOURCE template's content, never a
# shallow ``dict()``/``__dict__`` copy and never a shared reference -- nested
# payload structures must never alias the template's own stored content, so a
# later mutation of the clone can never write through to the template.
#
# The receipt's ``content_digest`` is the template's FROZEN registry digest
# (``resolved.digest``), copied verbatim -- never recomputed over the clone's
# own (re-serialized) copy, so it stays byte-identical to every other
# receipt naming that same source version.
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ProjectTemplateClone:
    clone_id: str
    tenant_id: str
    project_id: str
    template_id: str
    version: str
    content: Dict[str, Any]
    receipt: Dict[str, Any]
    # CAS counter for `update_project_clone_content` (card C2-4). Starts at 1
    # on landing; every accepted write bumps it by exactly one. Never the
    # SOURCE template version above -- this counts edits to the CLONE.
    content_version: int = 1


_PROJECT_CLONES: Dict[str, ProjectTemplateClone] = {}
_PROJECT_CLONES_LOCK = threading.Lock()

# Roles allowed to clone a template into a project -- the same owner/editor
# split ``customization_authority._STAGE_ROLES`` uses for staging writes.
_CLONE_AUTHORIZED_ROLES = frozenset({"owner", "editor"})


def _authorize_project_clone(binding: TenantBinding, tenant_id: str) -> None:
    """Require a verified owner/editor binding for the EXACT target tenant.

    Every failure -- an unverified binding, a role outside
    ``customization_authority.TENANT_ROLES``, a role outside owner/editor, or
    a binding whose tenant does not match the requested ``tenant_id`` -- raises
    the SAME :class:`AuthorityError` shape (``"template_clone_denied"``), so a
    denial never tells a caller which check failed or whether the target
    tenant/project even exists.
    """
    authorized = (
        isinstance(binding, TenantBinding)
        and binding.verified is True
        and isinstance(binding.tenant_id, str)
        and binding.tenant_id == tenant_id
        and binding.role in TENANT_ROLES
        and binding.role in _CLONE_AUTHORIZED_ROLES
    )
    if not authorized:
        raise AuthorityError("template_clone_denied")


def clone_template_for_project(
    template_id: str,
    tenant_id: str,
    project_id: str,
    version: Optional[str] = None,
    *,
    binding: TenantBinding,
) -> ProjectTemplateClone:
    """Clone one exact pinned template version into a project-owned copy.

    Fails closed (raises, writes nothing) when ``solar_template_beta`` is
    off, when ``tenant_id``/``project_id`` are missing, when ``binding`` does
    not authorize an owner/editor of the exact target tenant (see
    :func:`_authorize_project_clone`), or when the source version's content
    digest does not verify -- in every case before any clone is landed in
    the store.
    """
    if not solar_template_beta_enabled():
        raise TemplateBetaDisabledError("solar_template_beta is not enabled")
    if not tenant_id or not tenant_id.strip():
        raise ValueError("tenant_id is required to bind a project clone")
    if not project_id or not project_id.strip():
        raise ValueError("project_id is required to bind a project clone")
    _authorize_project_clone(binding, tenant_id)

    resolved = resolve_version(template_id, version)
    verify_digest(resolved)

    isolated_content = copy.deepcopy(resolved.content)
    clone_id = str(uuid.uuid4())
    receipt = {
        "template_id": resolved.template_id,
        "version": resolved.version,
        "content_digest": resolved.digest,
        "action": "clone",
        "tenant_id": tenant_id,
        "project_id": project_id,
        "clone_id": clone_id,
    }
    clone = ProjectTemplateClone(
        clone_id=clone_id, tenant_id=tenant_id, project_id=project_id,
        template_id=resolved.template_id, version=resolved.version,
        content=isolated_content, receipt=receipt,
    )
    with _PROJECT_CLONES_LOCK:
        _PROJECT_CLONES[clone_id] = clone
    return clone


def get_project_clone(clone_id: str) -> ProjectTemplateClone:
    """Fetch a previously landed project clone by its id."""
    clone = _PROJECT_CLONES.get(clone_id)
    if clone is None:
        raise ProjectTemplateCloneNotFoundError(clone_id)
    return clone


def list_project_clones(tenant_id: str, project_id: str) -> List[ProjectTemplateClone]:
    """All clones landed for one (tenant_id, project_id) pair."""
    return [
        clone for clone in _PROJECT_CLONES.values()
        if clone.tenant_id == tenant_id and clone.project_id == project_id
    ]


# --------------------------------------------------------------------------- #
# Single-step, receipted undo (card C2-5)
#
# The full validated, transactional write path onto a project clone is card
# C2-4's deliverable (``server/routers/templates.py``). ``write_project_clone_
# content`` below is the MINIMAL internal primitive undo needs to have a real
# write to act on: it snapshots the clone's content EXACTLY as it stood
# immediately before the write, so undo restores that stored snapshot
# verbatim rather than recomputing an inverse delta from whatever the current
# state happens to be.
#
# ``undo_last_write`` binds to the EXACT write named by ``write_id`` -- it
# never re-resolves "the last write" against the clone's current head at
# request time. A ``write_id`` that is no longer the head (a newer write has
# since landed) is refused as stale, not silently reinterpreted as "undo
# whatever is on top now". A ``write_id`` that has already been undone
# returns the SAME undo receipt again with ``already_undone=True`` -- a
# truthful no-op, never a fresh receipt (no redo, no double revert) -- and a
# single lock serializes the read-check-act-record sequence so two
# concurrent undo calls for the same write_id can never both perform the
# revert.
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ProjectCloneWrite:
    write_id: str
    clone_id: str
    prior_content: Dict[str, Any]  # exact snapshot of clone.content BEFORE this write
    receipt: Dict[str, Any]


@dataclass(frozen=True)
class UndoResult:
    undone: bool
    already_undone: bool
    receipt: Dict[str, Any]


class ProjectCloneWriteNotFoundError(LookupError):
    """``write_id`` was never recorded as a write on this clone."""


class StaleUndoReceiptError(RuntimeError):
    """``write_id`` names a write that is no longer this clone's head write."""


# clone_id -> writes in application order (oldest first, last element = head)
_CLONE_WRITE_LOG: Dict[str, List[ProjectCloneWrite]] = {}
# write_id -> the undo receipt produced the first (and only) time it was undone
_CLONE_UNDO_LOG: Dict[str, Dict[str, Any]] = {}


def write_project_clone_content(
    clone_id: str, content: Dict[str, Any], *, binding: TenantBinding,
) -> ProjectCloneWrite:
    """Overwrite a project clone's content, snapshotting the prior content.

    Fails closed (raises, writes nothing) when ``solar_template_beta`` is
    off, the clone is unknown, or ``binding`` does not authorize an
    owner/editor of the clone's own tenant.
    """
    if not solar_template_beta_enabled():
        raise TemplateBetaDisabledError("solar_template_beta is not enabled")
    # Same caps as the PATCH path (kimi #704 r2 f2): every route that mutates
    # clone content validates through the ONE core, or a 1 MB payload walks in
    # a namespace over.
    validated = _validate_clone_content_patch(content)
    with _PROJECT_CLONES_LOCK:
        clone = _PROJECT_CLONES.get(clone_id)
        if clone is None:
            raise ProjectTemplateCloneNotFoundError(clone_id)
        _authorize_project_clone(binding, clone.tenant_id)

        prior_content = copy.deepcopy(clone.content)
        write_id = str(uuid.uuid4())
        receipt = {
            "clone_id": clone_id,
            "write_id": write_id,
            "action": "write",
            "tenant_id": clone.tenant_id,
            "project_id": clone.project_id,
        }
        write = ProjectCloneWrite(
            write_id=write_id, clone_id=clone_id,
            prior_content=prior_content, receipt=receipt,
        )
        _PROJECT_CLONES[clone_id] = ProjectTemplateClone(
            clone_id=clone.clone_id, tenant_id=clone.tenant_id,
            project_id=clone.project_id, template_id=clone.template_id,
            version=clone.version, content=copy.deepcopy(validated),
            receipt=clone.receipt,
            # Version lineage NEVER resets (kimi #704 r2 f3): a legacy write
            # advances the same CAS counter the PATCH path compares against.
            content_version=clone.content_version + 1,
        )
        _CLONE_WRITE_LOG.setdefault(clone_id, []).append(write)
    return write


def undo_last_write(
    clone_id: str, write_id: str, *, binding: TenantBinding,
) -> UndoResult:
    """Revert ``clone_id`` to exactly the content stored before ``write_id``.

    Fails closed when ``solar_template_beta`` is off, the clone is unknown,
    or ``binding`` does not authorize an owner/editor of the clone's own
    tenant -- checked before any lookup of the write history. A ``write_id``
    that is not this clone's current head write raises
    :class:`StaleUndoReceiptError` and reverts nothing. A ``write_id`` that
    was never recorded for this clone (including a clone with no writes at
    all) raises :class:`ProjectCloneWriteNotFoundError` rather than reverting
    an unrelated write.
    """
    if not solar_template_beta_enabled():
        raise TemplateBetaDisabledError("solar_template_beta is not enabled")
    with _PROJECT_CLONES_LOCK:
        clone = _PROJECT_CLONES.get(clone_id)
        if clone is None:
            raise ProjectTemplateCloneNotFoundError(clone_id)
        _authorize_project_clone(binding, clone.tenant_id)

        already = _CLONE_UNDO_LOG.get(write_id)
        if already is not None:
            return UndoResult(undone=True, already_undone=True, receipt=already)

        history = _CLONE_WRITE_LOG.get(clone_id, [])
        if not history or history[-1].write_id != write_id:
            if any(w.write_id == write_id for w in history):
                raise StaleUndoReceiptError(
                    f"{write_id!r} is not the current head write for clone "
                    f"{clone_id!r}"
                )
            raise ProjectCloneWriteNotFoundError(write_id)

        head = history.pop()
        _PROJECT_CLONES[clone_id] = ProjectTemplateClone(
            clone_id=clone.clone_id, tenant_id=clone.tenant_id,
            project_id=clone.project_id, template_id=clone.template_id,
            version=clone.version, content=copy.deepcopy(head.prior_content),
            receipt=clone.receipt,
            # Undo is itself a mutation: the CAS lineage advances, never resets
            # (kimi #704 r2 f3 - resetting to 1 enabled a silent lost update).
            content_version=clone.content_version + 1,
        )
        undo_receipt = {
            "clone_id": clone_id,
            "undo_id": str(uuid.uuid4()),
            "action": "undo",
            "reverted_write_id": write_id,
            "reverted_write_receipt": head.receipt,
            "tenant_id": clone.tenant_id,
            "project_id": clone.project_id,
        }
        _CLONE_UNDO_LOG[write_id] = undo_receipt
    return UndoResult(undone=True, already_undone=False, receipt=undo_receipt)


# --------------------------------------------------------------------------- #
# The ONE write path: bounded, validated, fails closed (card C2-4)
#
# ``update_project_clone_content`` is the ONLY function that mutates an
# already-landed :class:`ProjectTemplateClone`'s content. It never touches the
# template registry (that store is read-only; see the module docstring and
# card C2-1/C2-3) -- only ``_PROJECT_CLONES``.
#
# Bounded: payload shape/size caps in ``_validate_clone_content_patch`` run
# BEFORE the write's atomic section is ever entered, so a malformed payload
# never blocks on, or is serialized behind, another writer.
#
# Transactional with a statement timeout: the lookup, the CAS check, and the
# replace happen inside ONE critical section guarded by
# ``_PROJECT_CLONES_LOCK`` (the same lock ``clone_template_for_project`` uses
# to land a clone, so creation and update can never interleave a torn read).
# Entering that section is bounded by ``lock_timeout_seconds`` -- a per-call
# bound, not a global setting -- so a stuck writer fails the NEXT caller
# closed (:class:`ProjectTemplateCloneWriteTimeoutError`) instead of hanging
# it indefinitely or letting it silently wait past a real deadline.
#
# No lost update: ``expected_content_version`` is compared to the STORED
# ``content_version`` INSIDE the lock, atomically with the replace. A second
# writer racing the first with the version it read before either of them
# entered the section always loses honestly
# (:class:`ProjectTemplateCloneConflictError`) rather than silently
# overwriting the first writer's content -- this is the CAS the trap sheet
# calls out as "WHERE version = :expected", done in-process because
# ``migration: false`` on this card means there is no table to add it to.
#
# Authority (closes PR #704 finding 7): this is a mutation, so it requires
# the SAME ``binding``/``_authorize_project_clone`` gate ``clone_template_for_
# project``/``write_project_clone_content``/``undo_last_write`` already
# enforce -- an owner/editor binding for the clone's OWN tenant, verified
# INSIDE the critical section, after the tenant-scoped lookup and before the
# CAS check, so a viewer/reviewer/unverified/revoked binding is denied with
# the SAME :class:`AuthorityError` shape those other mutations use, never a
# write. ``binding`` is keyword-only with NO default -- a caller can never
# forget it and silently land an unauthorized write.
# --------------------------------------------------------------------------- #
_CLONE_WRITE_LOCK_TIMEOUT_SECONDS = 2.0

# Caps deliberately TIGHTER than anything a real column would allow, never
# wider -- a schema-valid payload must never be able to blow up a future
# storage layer (trap sheet #7).
_CONTENT_MAX_BYTES = 16_384
_CONTENT_MAX_DEPTH = 6
_CONTENT_MAX_KEYS_PER_OBJECT = 64
_CONTENT_MAX_ITEMS_PER_ARRAY = 256
_CONTENT_MAX_STRING_LEN = 4_000


def _validate_json_shape(value: Any, *, depth: int = 0) -> None:
    if depth > _CONTENT_MAX_DEPTH:
        raise ProjectTemplateCloneWriteValidationError(
            f"content nesting exceeds the allowed depth of {_CONTENT_MAX_DEPTH}")
    if isinstance(value, dict):
        if len(value) > _CONTENT_MAX_KEYS_PER_OBJECT:
            raise ProjectTemplateCloneWriteValidationError(
                f"a content object has more than {_CONTENT_MAX_KEYS_PER_OBJECT} keys")
        for key, item in value.items():
            if not isinstance(key, str) or not key or len(key) > 200:
                raise ProjectTemplateCloneWriteValidationError(
                    f"invalid content key {key!r}")
            _validate_json_shape(item, depth=depth + 1)
    elif isinstance(value, list):
        if len(value) > _CONTENT_MAX_ITEMS_PER_ARRAY:
            raise ProjectTemplateCloneWriteValidationError(
                f"a content array has more than {_CONTENT_MAX_ITEMS_PER_ARRAY} items")
        for item in value:
            _validate_json_shape(item, depth=depth + 1)
    elif isinstance(value, bool):
        pass
    elif isinstance(value, str):
        if len(value) > _CONTENT_MAX_STRING_LEN:
            raise ProjectTemplateCloneWriteValidationError(
                f"a content string exceeds {_CONTENT_MAX_STRING_LEN} characters")
    elif isinstance(value, (int, float)):
        if not math.isfinite(value):
            raise ProjectTemplateCloneWriteValidationError(
                "content numbers must be finite (no NaN/Infinity)")
    elif value is None:
        pass
    else:
        raise ProjectTemplateCloneWriteValidationError(
            f"content contains an unsupported type {type(value).__name__!r}")


def _validate_clone_content_patch(content: Any) -> Dict[str, Any]:
    """Schema-validate a caller-submitted clone content payload, with caps.

    Runs to completion (never partially) before any lock is taken or any
    store state is touched -- a malformed payload always fails closed with
    ZERO side effects, whatever the failure reason."""
    if not isinstance(content, dict):
        raise ProjectTemplateCloneWriteValidationError("content must be a JSON object")
    if not content:
        raise ProjectTemplateCloneWriteValidationError("content must not be empty")
    _validate_json_shape(content)
    try:
        encoded = json.dumps(
            content, ensure_ascii=False, separators=(",", ":"), sort_keys=True,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ProjectTemplateCloneWriteValidationError(
            "content is not JSON-serializable") from exc
    if len(encoded.encode("utf-8")) > _CONTENT_MAX_BYTES:
        raise ProjectTemplateCloneWriteValidationError(
            f"content exceeds {_CONTENT_MAX_BYTES} encoded bytes")
    return content


def _validate_expected_content_version(expected_content_version: Any) -> int:
    if isinstance(expected_content_version, bool) or not isinstance(
        expected_content_version, int
    ):
        raise ProjectTemplateCloneWriteValidationError(
            "expected_content_version must be an integer")
    if expected_content_version < 1:
        raise ProjectTemplateCloneWriteValidationError(
            "expected_content_version must be >= 1")
    return expected_content_version


def update_project_clone_content(
    clone_id: str,
    tenant_id: str,
    content: Dict[str, Any],
    expected_content_version: int,
    *,
    binding: TenantBinding,
    lock_timeout_seconds: float = _CLONE_WRITE_LOCK_TIMEOUT_SECONDS,
) -> ProjectTemplateClone:
    """The ONE mutation path for an already-landed project template clone.

    Fails closed, with NO write landed, on: the beta flag being off, a
    malformed ``content``/``expected_content_version`` payload (validated
    before the lock is ever taken), a ``clone_id`` that does not exist OR
    does not belong to ``tenant_id`` (the SAME
    :class:`ProjectTemplateCloneNotFoundError`, so a foreign tenant's clone
    id is indistinguishable from one that was never issued -- trap sheet
    #9-#11), ``binding`` not authorizing an owner/editor of the clone's own
    tenant (:class:`~customization_authority.AuthorityError`, the SAME gate
    :func:`clone_template_for_project`/:func:`write_project_clone_content`/
    :func:`undo_last_write` enforce -- a viewer/reviewer/unverified/revoked
    binding is denied, checked AFTER the tenant-scoped lookup so it never
    leaks whether a foreign clone_id exists), a stale
    ``expected_content_version`` (:class:`ProjectTemplateCloneConflictError`),
    or a write that could not enter its atomic section within
    ``lock_timeout_seconds`` (:class:`ProjectTemplateCloneWriteTimeoutError`).

    On success, the STORED clone is replaced atomically with a new isolated
    (deep-copied) content and ``content_version`` incremented by exactly one.
    """
    if not solar_template_beta_enabled():
        raise TemplateBetaDisabledError("solar_template_beta is not enabled")
    if not isinstance(tenant_id, str) or not tenant_id.strip():
        raise ValueError("tenant_id is required to write a project clone")
    if not isinstance(clone_id, str) or not clone_id.strip():
        raise ProjectTemplateCloneNotFoundError(clone_id)

    # Validation is COMPLETE before any lock/store access: a malformed
    # payload can never block on, or be serialized behind, another writer.
    validated_content = _validate_clone_content_patch(content)
    validated_expected_version = _validate_expected_content_version(
        expected_content_version)

    acquired = _PROJECT_CLONES_LOCK.acquire(timeout=lock_timeout_seconds)
    if not acquired:
        raise ProjectTemplateCloneWriteTimeoutError(
            f"write did not enter its atomic section within "
            f"{lock_timeout_seconds}s")
    try:
        existing = _PROJECT_CLONES.get(clone_id)
        if existing is None or existing.tenant_id != tenant_id:
            # Same shape whether the id is unknown or belongs to another
            # tenant -- a caller can never use this to learn a foreign
            # clone_id exists.
            raise ProjectTemplateCloneNotFoundError(clone_id)
        # Role gate (closes PR #704 finding 7): a verified owner/editor
        # binding for the clone's OWN tenant only -- same machinery, same
        # AuthorityError shape as clone/write/undo. Checked AFTER the
        # tenant-scoped lookup (so it never turns into a 404-vs-403
        # enumeration signal) and BEFORE the CAS check (a denied caller
        # never learns whether their stale version would otherwise conflict).
        _authorize_project_clone(binding, existing.tenant_id)
        if existing.content_version != validated_expected_version:
            raise ProjectTemplateCloneConflictError(
                f"clone {clone_id!r} is at content_version "
                f"{existing.content_version}, not {validated_expected_version}")

        next_version = existing.content_version + 1
        receipt = {
            **existing.receipt,
            "action": "update",
            "content_version": next_version,
        }
        updated = ProjectTemplateClone(
            clone_id=existing.clone_id,
            tenant_id=existing.tenant_id,
            project_id=existing.project_id,
            template_id=existing.template_id,
            version=existing.version,
            content=copy.deepcopy(validated_content),
            receipt=receipt,
            content_version=next_version,
        )
        _PROJECT_CLONES[clone_id] = updated
        return updated
    finally:
        _PROJECT_CLONES_LOCK.release()
