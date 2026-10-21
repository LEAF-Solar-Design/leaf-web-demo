"""Optional synchronous host custody for a freshly accepted direct Studio turn.

No startup I/O and no credential consumers. S10 KMS uses two calls, each with
1s connect/read timeouts and one attempt. S10 SQL uses 1s statement, 500ms lock
and 2s idle-transaction limits; its shared pool defaults to a 30s acquisition
timeout. These are per-operation limits, not a three-second total or a hard
end-to-end wall-clock bound (authority reads and network teardown also matter).
There are no retries, timers or background credential workers here. Fixed
expiry and S12's mandatory current-authority checks remain authoritative after
a crash, cross-replica termination, or failed best-effort revocation.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
import os
import re
import threading
import time
from uuid import UUID, uuid4

if __package__:
    from .integration_config import IntegrationConfigError, load_integration_config
else:
    from integration_config import IntegrationConfigError, load_integration_config


@dataclass(frozen=True)
class CaptureOutcome:
    code: str
    grant_id: str | None = None


@dataclass
class _Reference:
    authority: object
    grant_id: str
    expires_at: float
    terminal: bool = False


_lock = threading.Lock()
_references: dict[tuple[str, str], _Reference] = {}
_CAPACITY = 1024


def _finite(value):
    try:
        return type(value) in (int, float) and math.isfinite(value) and value >= 0
    except (ValueError, OverflowError):
        return False


def _uuid(value):
    text = str(value)
    parsed = UUID(text)
    if str(parsed) != text or parsed.int == 0:
        raise ValueError()
    return text


def _configuration():
    try:
        settings = load_integration_config()
    except IntegrationConfigError:
        return None
    if not settings.enabled:
        return None
    origin = settings.api_origin
    # Authority also validates the origin before any privileged I/O.
    if (not origin or not os.environ.get("DATABASE_URL")
            or not re.fullmatch(r"[A-Za-z0-9:/_-]{1,2048}", settings.kms_key_id)
            or not re.fullmatch(r"[a-z]{2}(?:-gov)?-[a-z]+-[0-9]+",
                                os.environ.get("AWS_REGION", ""))):
        return None
    return origin


def _adapters():
    import platform_link
    platform_link._ensure_platform_package()
    from leaf_platform.integration_turn_grant_store import Authority, Binding, IntegrationTurnGrantStore
    from integration_turn_grant_crypto import seal
    return Authority, Binding, IntegrationTurnGrantStore(), seal


def _prune(now):
    # Caller holds the lock. In-flight capture still owns its reference locally.
    for key, ref in list(_references.items()):
        if ref.expires_at <= now:
            del _references[key]


def _reserve(binding, now):
    key = (binding.authority.session_id, binding.authority.turn_id)
    with _lock:
        _prune(now)
        # Even identical authority cannot renew or recapture an occupied turn.
        if key in _references or len(_references) >= _CAPACITY:
            return None
        ref = _Reference(binding.authority, binding.grant_id, binding.expires_at)
        _references[key] = ref
        return ref


def _terminal(ref):
    with _lock:
        return ref.terminal


def _revoke(ref, store=None):
    try:
        if store is None:
            store = _adapters()[2]
        store.revoke_turn(ref.authority, time.time())
    except Exception:
        pass


def cleanup_turn(session_id, turn_id):
    """Nonthrowing exact-key cleanup, including when the store release failed.

    Mark terminal before I/O. Keep the tombstone until fixed expiry so repeated
    cleanup is inert and put-in-flight can detect revocation-before-insert.
    No local reference means no privileged database lookup.
    """
    try:
        with _lock:
            _prune(time.time())
            ref = _references.get((session_id, turn_id))
            if ref is None or ref.terminal:
                return
            ref.terminal = True
        _revoke(ref)
    except Exception:
        pass


def _authority(tenant, snapshot, session_id, turn_id, max_s, origin, authority_type):
    import platform_link
    import session_store
    from leaf_platform.integration_turn_grant_store import CURRENT_OPERATIONS
    tenant_id = _uuid(tenant)
    if (not getattr(tenant, "authority_resolved", False)
            or getattr(tenant, "backedge", False)
            or snapshot.subject != getattr(tenant, "subject", None)
            or not _finite(max_s) or max_s <= 0):
        raise ValueError()
    session = session_store.get_session(session_id)
    if (not session or session.get("session_id") != session_id
            or _uuid(session.get("org_id")) != tenant_id
            or session.get("active_turn_id") != turn_id):
        raise ValueError()
    project_id = _uuid(session.get("project_id"))
    started = session.get("turn_started_at")
    now = time.time()
    if (not _finite(now) or not _finite(started) or started > now
            or not _finite(snapshot.expires_at) or snapshot.expires_at <= now
            or started + max_s <= now
            or session_store.active_turn_subject(
                session_id, turn_id, tenant_id, max_age_s=max_s) != snapshot.subject):
        raise ValueError()
    binding = platform_link.resolve_caller_binding(tenant)
    if (binding is None or binding.status != "active"
            or binding.external_authority != "auth0"
            or binding.external_subject != snapshot.subject
            or _uuid(binding.platform_tenant_id) != tenant_id
            or platform_link.require_project_session_access(
                session, tenant, write=True, binding=binding) is None):
        raise ValueError()
    authority = authority_type(
        tenant_id=tenant_id, project_id=project_id,
        platform_user_id=_uuid(binding.platform_user_id), binding_id=_uuid(binding.binding_id),
        external_authority="auth0", subject=snapshot.subject,
        session_id=session_id, turn_id=turn_id, origin=origin, operations=CURRENT_OPERATIONS,
    )
    return authority, started + max_s


def _capture(bearer, snapshot, tenant, session_id, turn_id, max_s):
    ref = None
    store = None
    try:
        origin = _configuration()
        if origin is None:
            return CaptureOutcome("unavailable")
        authority_type, binding_type, store, seal = _adapters()
        # Validate configured origin before authority reads, KMS or storage.
        authority_type(tenant_id=_uuid(tenant), project_id=_uuid(tenant),
                       platform_user_id=_uuid(tenant), binding_id=_uuid(tenant),
                       external_authority="auth0", subject=snapshot.subject,
                       session_id=session_id, turn_id=turn_id, origin=origin)
        authority, deadline = _authority(
            tenant, snapshot, session_id, turn_id, max_s, origin, authority_type)
        issued = time.time()
        binding = binding_type(authority, str(uuid4()), issued, snapshot.expires_at,
                               deadline, min(snapshot.expires_at, deadline, issued + 120))
        ref = _reserve(binding, issued)
        if ref is None:
            return CaptureOutcome("unavailable")
        envelope = seal(bearer.encode("utf-8"), binding)
        bearer = None
        if _terminal(ref) or time.time() >= binding.expires_at:
            raise ValueError()
        result = store.put_once(binding, envelope)
        envelope = None
        # Always re-read after put: terminal cleanup might have run before INSERT.
        current, current_deadline = _authority(
            tenant, snapshot, session_id, turn_id, max_s, origin, authority_type)
        if (result not in {"created", "already_present"} or current != authority
                or current_deadline != deadline or _terminal(ref)
                or not issued <= time.time() < binding.expires_at):
            raise ValueError()
        return CaptureOutcome("captured", binding.grant_id)
    except Exception:
        if ref is not None:
            with _lock:
                ref.terminal = True
            # Also covers ambiguous put failures and revoke-before-insert.
            _revoke(ref, store)
        return CaptureOutcome("unavailable")
    finally:
        bearer = None


def for_request(request, tenant):
    """Build a single-use ephemeral callback, only from the private live snapshot."""
    try:
        if _configuration() is None or request is None:
            return None
        import deps
        snapshot = getattr(request.state, "_integration_verified_expiry", None)
        if (type(snapshot) is not deps._VerifiedPlatformExpiry
                or not isinstance(tenant, deps.TenantContext)
                or snapshot.subject != getattr(tenant, "subject", None)
                or not _finite(snapshot.expires_at) or snapshot.expires_at <= time.time()):
            return None
        header = request.headers.get("authorization", "")
        scheme, separator, bearer = header.partition(" ")
        if scheme.lower() != "bearer" or not separator or not bearer or len(bearer) > 16384:
            return None

        def capture(session_id, turn_id, max_s):
            nonlocal bearer
            token, bearer = bearer, None
            if token is None:
                return CaptureOutcome("unavailable")
            try:
                return _capture(token, snapshot, tenant, session_id, turn_id, max_s)
            finally:
                token = None

        def discard():
            nonlocal bearer
            bearer = None

        capture.discard = discard
        return capture
    except Exception:
        return None
