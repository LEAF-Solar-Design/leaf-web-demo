"""Durable, narrowly bound permission for a queued Branch build.

This is not turn authority: dispatches admitted here cannot attach services.
All live checks are repeated before the lease-bound, final consume operation.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time

import build_plan
import customization_flags
import deps
import entitlements
import platform_link
import session_store
from customization_authority import BuilderEntitlement
from customization_models import ChangeState

_LOG = logging.getLogger(__name__)
_BINDING_FIELDS = (
    "tenant_id", "change_set_id", "author_subject", "idempotency_key", "version",
    "base_commit", "desired_platform_release", "workspace_contract_digest",
    "change_kind", "target_tool_name", "tool_target", "request_fingerprint",
    "plan_digest", "plan_confirmation_id", "request_graph_input",
    "base_catalog_change_set_id", "authority_session_id", "authority_turn_id",
)


def ttl_s() -> int | None:
    raw = os.environ.get("LEAF_STAGE_GRANT_TTL_S", "").strip()
    if not re.fullmatch(r"[0-9]+", raw):
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if 300 <= value <= 14400 else None


def revoke_for_turn(tenant_id: str, session_id: str, turn_id: str, *, reason: str) -> None:
    try:
        from customization_service import CustomizationService

        if ttl_s() is None:
            return
        service = CustomizationService.configured()
        service.store.revoke_stage_grants_for_turn(
            tenant_id=tenant_id, authority_session_id=session_id,
            authority_turn_id=turn_id, reason=reason, now_ms=int(time.time() * 1000),
        )
    except Exception as exc:
        _LOG.error("stage_grant_revoke_failed exception_type=%s", type(exc).__name__)


def binding_digest(change) -> str:
    from customization_service import _stage_request_fingerprint

    if (not isinstance(change.request_description, str)
            or _stage_request_fingerprint(change.request_description, change.tool_target) != change.request_fingerprint):
        raise ValueError("request fingerprint mismatch")
    if change.plan_json is not None:
        if build_plan.plan_digest(json.loads(change.plan_json)) != change.plan_digest:
            raise ValueError("plan digest mismatch")
    payload = {field: getattr(change, field) for field in _BINDING_FIELDS}
    return hashlib.sha256(json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode("utf-8")).hexdigest()


def live_check(service, change) -> str | None:
    try:
        from customization_service import _binding, enabled

        tenant_id, tier = deps.resolve_active_platform_tenant_authority(change.author_subject)
        if tenant_id != change.tenant_id:
            return None
        identity = deps.TenantContext(
            change.tenant_id, subject=change.author_subject, tier=tier,
            backedge=True, authority_resolved=True,
        )
        if not enabled(5, change.tenant_id):
            return None
        if customization_flags.plan_mode_enabled(change.tenant_id) and change.plan_digest is None:
            return None
        binding = _binding(identity)
        build = entitlements.entitlements_for(tier, [], False).get("build", False)
        service._authority().authorize_stage(
            binding=binding,
            builder_entitlement=BuilderEntitlement(
                change.tenant_id, binding.subject, bool(build), True,
            ),
        )
        if platform_link.require_project_session_access(
            session_store.get_session(change.authority_session_id), identity, write=True,
        ) is None:
            return None
        return tier if build else None
    except Exception:
        return None


def issue(service, change) -> bool:
    try:
        ttl = ttl_s()
        if (ttl is None or change.state is not ChangeState.STAGING
                or change.tool_target != "branch" or not change.author_subject
                or not change.authority_session_id or not change.authority_turn_id):
            return False
        tier = live_check(service, change)
        if tier is None:
            return False
        digest = binding_digest(change)
        now_ms = int(time.time() * 1000)
        return service.store.insert_stage_grant(
            tenant_id=change.tenant_id, change_set_id=change.change_set_id,
            author_subject=change.author_subject, tier=tier, binding_digest=digest,
            authority_session_id=change.authority_session_id,
            authority_turn_id=change.authority_turn_id,
            issued_ms=now_ms, expires_ms=now_ms + ttl * 1000,
        )
    except Exception as exc:
        _LOG.warning("stage_grant_issue exception_type=%s", type(exc).__name__)
        return False


def turn_ended_cleanly(session_id: str, turn_id: str, tenant_id: str) -> bool:
    try:
        events = session_store.turn_terminal_events(session_id, turn_id, tenant_id)
        if len(events) != 1 or events[0]["type"] != "turn_complete":
            return False
        reason = events[0]["data"].get("stop_reason")
        if reason == "end_turn":
            return True
        if reason == "timeout":
            started = session_store.turn_started_data(session_id, turn_id, tenant_id)
            return isinstance(started, dict) and started.get("stage_authority") is True
        return False
    except Exception:
        return False


def admit(service, change, owner) -> None:
    from customization_service import CustomizationServiceError

    code = "stage_grant_refused"
    try:
        try:
            row = service.store.get_stage_grant(change.tenant_id, change.change_set_id)
        except Exception as exc:
            raise CustomizationServiceError("stage_grant_unavailable", 409) from exc
        if row is None:
            code = "stage_authority_invalid"
        elif any(row[field] != getattr(change, field) for field in (
            "authority_session_id", "authority_turn_id",
        )):
            code = "stage_authority_invalid"
        elif any(row[field] != getattr(change, field) for field in (
            "change_set_id", "tenant_id", "author_subject",
        )):
            pass
        elif row["revoked_ms"] is not None:
            code = "stage_grant_cancelled" if row["revoked_reason"] == "cancelled" else "stage_grant_withdrawn"
        elif row["expires_ms"] <= int(time.time() * 1000):
            code = "stage_grant_expired"
        elif row["dispatches"] >= 2:
            code = "stage_grant_exhausted"
        elif change.tool_target != "branch" or row["binding_digest"] != binding_digest(change):
            pass
        elif not turn_ended_cleanly(change.authority_session_id, change.authority_turn_id, change.tenant_id):
            code = "stage_grant_turn_open"
        elif live_check(service, change) == row["tier"]:
            try:
                consumed = service.store.consume_stage_grant(
                    tenant_id=change.tenant_id, change_set_id=change.change_set_id,
                    owner=owner, attempt=change.stage_attempt,
                    binding_digest=row["binding_digest"], now_ms=int(time.time() * 1000),
                )
            except Exception as exc:
                raise CustomizationServiceError("stage_grant_unavailable", 409) from exc
            if consumed:
                return
    except CustomizationServiceError:
        raise
    except Exception:
        code = "stage_grant_refused"
    _LOG.warning("stage_grant_refusal change_set=%s code=%s", change.change_set_id, code)
    raise CustomizationServiceError(code, 409)
