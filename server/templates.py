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
import os
import threading
import uuid
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

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
# (tenant_id, project_id) pair. ``tenant_id``/``project_id`` are plain
# parameters here -- the caller (a router, when this is wired up) must derive
# them from server-side auth/project context, never from an unauthenticated
# request body, so a clone can never be born bound to a foreign tenant.
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


_PROJECT_CLONES: Dict[str, ProjectTemplateClone] = {}
_PROJECT_CLONES_LOCK = threading.Lock()


def clone_template_for_project(
    template_id: str,
    tenant_id: str,
    project_id: str,
    version: Optional[str] = None,
) -> ProjectTemplateClone:
    """Clone one exact pinned template version into a project-owned copy.

    Fails closed (raises, writes nothing) when ``solar_template_beta`` is
    off, when ``tenant_id``/``project_id`` are missing, or when the source
    version's content digest does not verify -- in every case before any
    clone is landed in the store.
    """
    if not solar_template_beta_enabled():
        raise TemplateBetaDisabledError("solar_template_beta is not enabled")
    if not tenant_id or not tenant_id.strip():
        raise ValueError("tenant_id is required to bind a project clone")
    if not project_id or not project_id.strip():
        raise ValueError("project_id is required to bind a project clone")

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
