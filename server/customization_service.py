"""Trusted orchestration for the frozen customization HTTP contract.

Only this module bridges HTTP-facing data to the durable coordination store and
the isolated tenant Git repository. It never treats a mutable checkout or a
request-supplied tenant, role, release, or digest as authority.
"""
from __future__ import annotations

import errno
import hashlib
import hmac
import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import threading
import time
from contextlib import contextmanager
try:  # POSIX advisory locking; the deployed runtime is Linux
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None
try:
    import msvcrt
except ImportError:  # pragma: no cover - POSIX
    msvcrt = None
from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping
from urllib.parse import urlsplit
from uuid import uuid4

from psycopg import Error as PostgresError

import author_quota
import agent_policy
import deps
import entitlements
import platform_link
from customization_authority import (
    AuthorityError, BuilderEntitlement, CustomizationAuthority,
    HmacConfirmationSigner, PublishRequest, StaffAuthority, StagedChange,
    TenantBinding,
)
from customization_flags import RolloutMode, enabled, mode
from customization_models import ChangeSet, ChangeSetNotFoundError, ChangeState
from customization_postgres_store import PostgresCustomizationStore
from customization_store import CustomizationRepository, SQLiteCustomizationStore
from platform_release_policy import PlatformReleasePolicyError, classify_path, load_policy
from tenant_id_validator import is_valid_tenant_id


CONTRACT = "leaf.customization.v1"
DEFAULT_DB = Path(__file__).resolve().parent / "customization.db"
_configured_lock = threading.Lock()
_configured_services: dict[str, "CustomizationService"] = {}
_LOG = logging.getLogger(__name__)
_REPOSITORY_VISIBILITY_DELAYS = (0.0, 1.0, 2.0, 4.0, 8.0, 15.0, 30.0)


class CustomizationServiceError(RuntimeError):
    def __init__(self, code: str, status_code: int = 409, detail: str = "") -> None:
        super().__init__(code)
        self.code, self.status_code = code, status_code
        # `code` is the ONLY client-visible part: routers/author.py copies it
        # into the response body as `reason_code`. `detail` carries operator
        # diagnostics (git stderr, absolute tenant paths) and must never be
        # serialized into a response. It is for logs and test assertions.
        self.detail = str(detail)


def database_path() -> Path:
    raw = os.environ.get("LEAF_CUSTOMIZATION_DB", "").strip()
    return Path(raw) if raw else DEFAULT_DB


def customization_store_mode() -> str:
    selected = os.environ.get("LEAF_CUSTOMIZATION_STORE", "sqlite").strip().lower()
    if selected not in {"sqlite", "postgres"}:
        raise CustomizationServiceError("customization_store_unsupported", 503)
    return selected


def _path_key(path: Path, *, resolve: bool) -> str:
    candidate = path.expanduser()
    if resolve:
        candidate = candidate.resolve(strict=False)
    normalized = os.path.normcase(os.path.abspath(os.path.normpath(str(candidate))))
    if normalized.startswith("\\\\?\\UNC\\"):
        return "\\\\" + normalized[8:]
    if normalized.startswith("\\\\?\\"):
        return normalized[4:]
    return normalized


def _path_within(path: Path, root: Path, *, resolve: bool = True) -> bool:
    candidate_key = _path_key(path, resolve=resolve)
    root_key = _path_key(root, resolve=resolve)
    try:
        return os.path.commonpath((candidate_key, root_key)) == root_key
    except ValueError:
        return False


def _shared_sqlite_path(path: Path) -> bool:
    """Identify the deployed shared EFS authority path.

    SQLite cannot coordinate overlapping ECS tasks on EFS. Customization stays
    unavailable there until the authority moves to PostgreSQL.
    """
    shared_root = Path("/data/state")
    return _path_within(path, shared_root, resolve=False) or _path_within(
        path, shared_root, resolve=True
    )


def _canonical_database_key(path: Path) -> str:
    return os.path.normcase(str(path.expanduser().resolve(strict=False)))


def reset_configured_services() -> None:
    """Clear the process cache. Intended for tests that replace a DB in place."""
    with _configured_lock:
        _configured_services.clear()


def _secret(name: str) -> bytes:
    value = os.environ.get(name, "").strip()
    if not value:
        raise CustomizationServiceError("customization_not_configured", 503)
    return value.encode("utf-8")


def _tenant_id(value: Any) -> str:
    """Return the one canonical tenant identity used by flags, stores and paths."""
    tenant_id = str(value).strip()
    if not is_valid_tenant_id(tenant_id):
        raise CustomizationServiceError("tenant_identity_invalid", 403)
    return tenant_id


def _harness_config() -> tuple[str, str]:
    """The (url, secret) pair exactly as the stage dispatch will use them —
    one normalization, shared by the pre-charge guard and ``_harness_stage``,
    so the two can never diverge on what "configured" means."""
    return (os.environ.get("LEAF_AUTHOR_HARNESS_URL", "").strip().rstrip("/"),
            os.environ.get("LEAF_HARNESS_SECRET", "").strip())


def _harness_stage_timeout_s() -> float:
    """Wait beyond the harness budget so a client retry cannot race its lease."""
    try:
        author_budget = float(os.environ.get("LEAF_AUTHOR_TIMEOUT_S", "300"))
    except ValueError:
        author_budget = 300.0
    if not 5 <= author_budget <= 900:
        author_budget = 300.0
    fallback = author_budget + 15.0
    try:
        configured = float(os.environ.get(
            "LEAF_CUSTOMIZATION_HARNESS_STAGE_TIMEOUT_S", str(fallback)
        ))
    except ValueError:
        return fallback
    if configured <= author_budget or configured > author_budget + 300:
        return fallback
    return configured


def _harness_misconfigured() -> bool:
    """True when no usable authoring harness is configured here.

    Deterministic, so ``stage()`` refuses BEFORE charging the daily authoring
    quota. Total by construction rather than shape-by-shape: it PREPARES the
    same request ``_harness_stage`` will send, so everything the HTTP client
    refuses without any network I/O — a URL that is not an http(s) origin, an
    invalid port, a malformed IPv6 literal, a secret that is not a valid
    header value — refuses here first, as does a blank secret (the harness's
    caller gate 401s every dispatch; same definition of "configured" as the
    repo-preparation fallback). The boundary: this guard owns the app's own
    harness configuration as validated client-side at prepare time. Anything
    past prepare (proxy/DNS/TLS/connect, a secret the harness REJECTS) is
    transport or harness state whose outcome is ambiguous or external, and
    such an attempt is charged like any other refused-in-flight attempt — a
    timed-out dispatch may well have reached the harness and spent.
    """
    url, secret = _harness_config()
    if not secret:
        return True
    try:
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            return True
        # http.client's own pre-I/O gate: header values are sent Latin-1, so a
        # secret outside that repertoire can never be dispatched by this client
        # and is configuration error, not harness state.
        secret.encode("latin-1")
        import requests
        requests.models.PreparedRequest().prepare(
            method="POST", url=f"{url}/author/stage",
            headers={"X-Harness-Secret": secret},
        )
    except Exception:  # noqa: BLE001 - anything the client refuses locally
        return True
    return False


def _git_trust(*paths: Path) -> list[str]:
    """Return command-scope `safe.directory` flags for exactly these paths.

    Tenant repos live on EFS and can be owned by the access-point UID rather than
    the container UID, so git refuses them with "detected dubious ownership".
    Running as root does not bypass that check. The harness already handles this
    (harness/src/ports/impl/tenantRepoProvider.ts trustSharedRepo); this is the
    Python side of the same problem.

    Command scope is protected configuration, which is what git requires for
    safe.directory, and unlike the harness's `git config --global` it needs no
    writable HOME. The app runs as root and the broker as UID 10001, so a
    HOME-dependent approach would work in one and not the other.

    Exact resolved paths only. A wildcard would trust every repository on the
    volume, which is not the same statement at all.
    """
    flags: list[str] = []
    for path in paths:
        try:
            resolved = str(path.resolve(strict=False))
        except OSError:
            resolved = str(path)
        flags.extend(("-c", f"safe.directory={resolved}"))
    return flags


def _git(bare: Path, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", *_git_trust(bare), "--git-dir", str(bare), *args],
            check=True, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        # stderr goes into `detail` (operator-only), never into the response.
        stderr = str(getattr(exc, "stderr", "") or "").strip()
        raise CustomizationServiceError(
            "tenant_repository_unavailable", 503,
            f"git_failed: {' '.join(args)} in {bare} ({type(exc).__name__})"
            + (f": {stderr}" if stderr else ""),
        ) from exc
    return result.stdout.strip()


def _git_blob(bare: Path, object_name: str) -> bytes:
    try:
        result = subprocess.run(
            ["git", *_git_trust(bare), "--git-dir", str(bare), "show", object_name],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        stderr = getattr(exc, "stderr", b"") or b""
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", "replace")
        raise CustomizationServiceError(
            "tenant_repository_unavailable", 503,
            f"git_show_failed: {object_name} in {bare} ({type(exc).__name__})"
            + (f": {stderr.strip()}" if stderr.strip() else ""),
        ) from exc
    return result.stdout


def _bare_repo(tenant_id: str) -> Path:
    tenant_id = _tenant_id(tenant_id)
    base = (
        os.environ.get("LEAF_TENANT_GIT_DIR", "").strip()
        or os.environ.get("LEAF_TENANT_BARE_BASE", "").strip()
    )
    if not base:
        raise CustomizationServiceError(
            "tenant_repository_unavailable", 503,
            "git_dir_unset: neither LEAF_TENANT_GIT_DIR nor LEAF_TENANT_BARE_BASE is set",
        )
    candidate = Path(base) / f"{tenant_id}.git"
    try:
        resolved_base = Path(base).resolve(strict=True)
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(resolved_base)
    except (OSError, ValueError) as exc:
        raise CustomizationServiceError(
            "tenant_repository_unavailable", 503,
            f"path_unresolvable: {candidate} under {base} ({type(exc).__name__})",
        ) from exc
    if not (resolved / "HEAD").is_file():
        raise CustomizationServiceError(
            "tenant_repository_unavailable", 503, f"head_missing: {resolved}/HEAD",
        )
    return resolved


def _ensure_bare_repo(tenant_id: str) -> Path:
    """Ask the harness to provision a first-time tenant repo, then verify it locally."""
    try:
        bare = _bare_repo(tenant_id)
        _git(bare, "rev-parse", "--verify", "refs/heads/main")
        return bare
    except CustomizationServiceError as exc:
        if exc.code != "tenant_repository_unavailable":
            raise
    url = os.environ.get("LEAF_AUTHOR_HARNESS_URL", "").rstrip("/")
    secret = os.environ.get("LEAF_HARNESS_SECRET", "").strip()
    if not url or not secret:
        # Never log the secret itself, only whether the deployment supplied one.
        raise CustomizationServiceError(
            "tenant_repository_unavailable", 503,
            f"harness_unconfigured: url_set={bool(url)} secret_set={bool(secret)}",
        )
    try:
        import requests
        response = requests.post(
            f"{url}/author/repository", timeout=30,
            headers={"X-Harness-Secret": secret},
            json={"tenant_id": tenant_id},
        )
        response.raise_for_status()
        body = response.json()
    except Exception as exc:
        status = getattr(getattr(exc, "response", None), "status_code", None)
        raise CustomizationServiceError(
            "tenant_repository_unavailable", 503,
            f"harness_provision_failed: {url}/author/repository "
            f"status={status} {type(exc).__name__}",
        ) from exc
    if not isinstance(body, Mapping):
        raise CustomizationServiceError(
            "tenant_repository_unavailable", 503,
            f"provision_response_malformed: {type(body).__name__}",
        )
    if body.get("tenant_id") != tenant_id:
        raise CustomizationServiceError(
            "tenant_repository_unavailable", 503,
            "provision_response_tenant_mismatch",
        )
    expected = body.get("base_commit")
    if (
        not isinstance(expected, str)
        or len(expected) != 40
        or any(char not in "0123456789abcdef" for char in expected)
    ):
        raise CustomizationServiceError(
            "tenant_repository_unavailable", 503,
            "provision_response_commit_malformed",
        )

    # App and harness are separate NFS clients. EFS can retain a negative
    # directory entry for refs/heads/main after the harness creates it. Retry
    # only local verification, never the provisioning write, until the bounded
    # visibility window expires.
    last_detail = "main_ref_not_observed"
    for delay in _REPOSITORY_VISIBILITY_DELAYS:
        if delay:
            time.sleep(delay)
        try:
            bare = _bare_repo(tenant_id)
            observed = _git(bare, "rev-parse", "--verify", "refs/heads/main")
        except CustomizationServiceError as exc:
            if exc.code != "tenant_repository_unavailable":
                raise
            last_detail = exc.detail or exc.code
            continue
        if observed == expected:
            return bare
        last_detail = (
            f"provision_base_commit_mismatch: harness={expected} observed={observed}"
        )
    raise CustomizationServiceError(
        "tenant_repository_unavailable", 503,
        "provision_ref_not_visible: "
        f"attempts={len(_REPOSITORY_VISIBILITY_DELAYS)} last={last_detail}",
    )


def _binding(tenant: Any) -> TenantBinding:
    tenant_id = _tenant_id(tenant)
    if not deps.auth_live():
        raise CustomizationServiceError("customization_auth_required", 503)
    subject = getattr(tenant, "subject", None)
    if not subject:
        raise CustomizationServiceError("tenant_identity_binding_unavailable", 403)
    if os.environ.get("DATABASE_URL", "").strip():
        try:
            platform_store = platform_link.platform_store()
            from psycopg import Error as PostgresError
        except (ImportError, OSError, RuntimeError) as exc:
            raise CustomizationServiceError(
                "tenant_identity_binding_unavailable", 503,
                f"platform_store_unavailable: {type(exc).__name__}",
            ) from exc
        try:
            binding = platform_store.resolve_active_identity_binding("auth0", subject)
            if binding is None:
                raise CustomizationServiceError("tenant_identity_binding_unavailable", 403)
            expected_org = str(getattr(tenant, "org_id", "") or tenant_id)
            if str(binding.platform_tenant_id) != expected_org:
                raise CustomizationServiceError("tenant_identity_binding_unavailable", 403)
            role = platform_store.active_identity_role(
                binding.platform_tenant_id, binding.binding_id
            )
            return TenantBinding(tenant_id, subject, role, True)
        except CustomizationServiceError:
            raise
        except (PostgresError, OSError, RuntimeError, ValueError) as exc:
            raise CustomizationServiceError(
                "tenant_identity_binding_unavailable", 503,
                f"binding_resolution_failed: {type(exc).__name__}",
            ) from exc
    if os.environ.get("LEAF_CUSTOMIZATION_ALLOW_STATIC_BINDINGS", "") == "1":
        raw = os.environ.get("LEAF_CUSTOMIZATION_TENANT_BINDINGS", "")
        try:
            record = json.loads(raw).get(tenant_id) if raw else None
        except json.JSONDecodeError:
            record = None
        if isinstance(record, dict) and record.get("subject") == subject:
            return TenantBinding(tenant_id, subject, record.get("role"), True)
    raise CustomizationServiceError("tenant_identity_binding_unavailable", 503)


class _StoreConfirmations:
    """Adapter preserving the authority module's atomic confirmation protocol."""
    def __init__(self, store: CustomizationRepository) -> None:
        self.store = store

    def put(self, confirmation_id: str, payload: dict[str, Any], signature: str) -> None:
        self.store.put_confirmation(confirmation_id=confirmation_id, payload=payload, signature=signature)

    def get(self, confirmation_id: str):
        result = self.store.get_confirmation(confirmation_id=confirmation_id)
        return None if result is None else SimpleNamespace(**result)

    def consume(self, confirmation_id: str, signature: str) -> bool:
        return self.store.consume_confirmation(confirmation_id=confirmation_id, signature=signature)


@dataclass
class CustomizationService:
    store: CustomizationRepository

    @classmethod
    def configured(cls) -> "CustomizationService":
        if customization_store_mode() == "postgres":
            if not os.environ.get("DATABASE_URL", "").strip():
                raise CustomizationServiceError(
                    "customization_database_url_required", 503
                )
            key = "postgres"
            with _configured_lock:
                cached = _configured_services.get(key)
                if cached is not None:
                    return cached
                store = PostgresCustomizationStore()
                store.initialize()
                service = cls(store)
                _configured_services[key] = service
                return service
        path = database_path()
        if _shared_sqlite_path(path):
            raise CustomizationServiceError(
                "customization_shared_sqlite_unsupported", 503
            )
        key = _canonical_database_key(path)
        with _configured_lock:
            cached = _configured_services.get(key)
            if cached is not None:
                return cached
            store = SQLiteCustomizationStore(path)
            store.initialize()
            service = cls(store)
            _configured_services[key] = service
            return service

    def _authority(self) -> CustomizationAuthority:
        secret_name = (
            "LEAF_CUSTOMIZATION_CONFIRMATION_KEY"
            if os.environ.get("LEAF_CUSTOMIZATION_CONFIRMATION_KEY", "").strip()
            else "LEAF_CUSTOMIZATION_CONFIRMATION_SECRET"
        )
        return CustomizationAuthority(
            HmacConfirmationSigner(_secret(secret_name)),
            confirmations=_StoreConfirmations(self.store),
        )

    @staticmethod
    def _release():
        policy = load_policy()
        configured = os.environ.get("LEAF_PLATFORM_RELEASE", "").strip()
        if configured:
            release = policy.releases.get(configured)
            if release is None:
                raise CustomizationServiceError("platform_release_not_declared", 503)
            return release
        if len(policy.releases) != 1:
            raise CustomizationServiceError("platform_release_ambiguous", 503)
        return next(iter(policy.releases.values()))

    def stage(
        self, *, tenant: Any, description: str, mode: str,
        idempotency_key: str, target_tool_name: str | None = None,
    ) -> dict[str, Any]:
        tenant_id = _tenant_id(tenant)
        if not enabled(5, tenant_id):
            raise CustomizationServiceError("customization_stage_disabled", 404)
        if mode != "build" or not isinstance(description, str) or not description.strip():
            raise CustomizationServiceError("invalid_stage_request", 422)
        if target_tool_name is not None:
            if (not isinstance(target_tool_name, str)
                    or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", target_tool_name)
                    or len(target_tool_name) > 64):
                raise CustomizationServiceError("invalid_stage_request", 422)
        binding = _binding(tenant)
        tier = entitlements.resolve_tier(tenant)
        if not entitlements.entitlements_for(tier).get("build", False):
            raise CustomizationServiceError("builder_entitlement_missing", 403)
        self._authority().authorize_stage(
            binding=binding,
            builder_entitlement=BuilderEntitlement(tenant_id, binding.subject, True, True),
        )
        release = self._release()
        bare = _ensure_bare_repo(tenant_id)
        try:
            prior = self.store.get_change_set_by_idempotency(
                tenant_id=tenant_id, idempotency_key=idempotency_key
            )
            base = prior.base_commit
        except ChangeSetNotFoundError:
            base = _git(bare, "rev-parse", "refs/heads/main")
        change = self.store.create_change_set(
            tenant_id=tenant_id, idempotency_key=idempotency_key, base_commit=base,
            desired_platform_release=release.release_id,
            workspace_contract_digest=release.workspace_contract_sha256,
            author_subject=binding.subject or "", change_set_id=str(uuid4()),
            change_kind="revise" if target_tool_name else "create",
            target_tool_name=target_tool_name,
        )
        if change.state is ChangeState.CREATED:
            change = self.store.transition(
                tenant_id=tenant_id, change_set_id=change.change_set_id,
                next_state=ChangeState.STAGING, expected_version=change.version,
                idempotency_key=f"stage:{idempotency_key}", expected_state=ChangeState.CREATED,
            )
        if change.state is ChangeState.STAGED:
            self._verify_stage_policy(change)
            return self._receipt(change)
        if change.state is not ChangeState.STAGING:
            raise CustomizationServiceError("stage_not_available")
        # An unconfigured or misconfigured harness answers every attempt with
        # the same 503 without spending anything, so those deterministic
        # refusals must also come before the charge (_harness_stage re-checks
        # the URL and raises the identical error).
        if _harness_misconfigured():
            raise CustomizationServiceError("customization_harness_unavailable", 503)
        # The daily authoring cap is charged HERE, at the last point before
        # authoring spends money, and never refunded. Everything deterministic
        # has already refused above — a disabled rollout, an invalid mode or
        # blank description, a missing binding, a tier without Build, a role
        # `authorize_stage` denies, an already-STAGED replay that returns its
        # durable receipt without calling the harness, and a harness this
        # deployment never configured — so none of those spends a slot. A retry
        # that DOES reach here re-invokes the harness and so counts again,
        # which is why the unit is an attempt and not a change-set row.
        author_quota.enforce(tenant_id, tier)
        body = self._harness_stage(tenant_id, description, change)
        return self._reconcile_staging(change, body)

    def enqueue_stage(
        self, *, tenant: Any, description: str, mode: str,
        idempotency_key: str, target_tool_name: str | None = None,
    ) -> dict[str, Any]:
        """Reserve and charge one exact stage request without calling the harness."""
        tenant_id = _tenant_id(tenant)
        if not enabled(5, tenant_id):
            raise CustomizationServiceError("customization_stage_disabled", 404)
        if (mode != "build" or not isinstance(description, str)
                or not description.strip() or len(description) > 8000):
            raise CustomizationServiceError("invalid_stage_request", 422)
        if target_tool_name is not None and (
            not isinstance(target_tool_name, str)
            or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", target_tool_name)
            or len(target_tool_name) > 64
        ):
            raise CustomizationServiceError("invalid_stage_request", 422)
        binding = _binding(tenant)
        tier = entitlements.resolve_tier(tenant)
        if not entitlements.entitlements_for(tier).get("build", False):
            raise CustomizationServiceError("builder_entitlement_missing", 403)
        self._authority().authorize_stage(
            binding=binding,
            builder_entitlement=BuilderEntitlement(
                tenant_id, binding.subject, True, True
            ),
        )
        release = self._release()
        try:
            prior = self.store.get_change_set_by_idempotency(
                tenant_id=tenant_id, idempotency_key=idempotency_key
            )
            base = prior.base_commit
        except ChangeSetNotFoundError:
            if _harness_misconfigured():
                raise CustomizationServiceError(
                    "customization_harness_unavailable", 503
                )
            bare = _ensure_bare_repo(tenant_id)
            base = _git(bare, "rev-parse", "refs/heads/main")
        fingerprint = hashlib.sha256(description.encode("utf-8")).hexdigest()
        change, created = self.store.reserve_stage(
            tenant_id=tenant_id, idempotency_key=idempotency_key,
            base_commit=base, desired_platform_release=release.release_id,
            workspace_contract_digest=release.workspace_contract_sha256,
            author_subject=binding.subject or "", change_set_id=str(uuid4()),
            change_kind="revise" if target_tool_name else "create",
            target_tool_name=target_tool_name, request_description=description,
            request_fingerprint=fingerprint,
        )
        if created:
            try:
                author_quota.enforce(tenant_id, tier)
            except Exception:
                self.store.transition(
                    tenant_id=tenant_id, change_set_id=change.change_set_id,
                    next_state=ChangeState.FAILED,
                    expected_version=change.version,
                    idempotency_key=f"admission-failed:{idempotency_key}",
                    expected_state=ChangeState.CREATED,
                    reason_code="stage_admission_failed",
                )
                raise
            change = self.store.transition(
                tenant_id=tenant_id, change_set_id=change.change_set_id,
                next_state=ChangeState.STAGING,
                expected_version=change.version,
                idempotency_key=f"stage:{idempotency_key}",
                expected_state=ChangeState.CREATED,
            )
        return self.stage_status_change(change)

    def execute_stage(self, change: ChangeSet) -> dict[str, Any]:
        if change.state is not ChangeState.STAGING or not change.request_description:
            raise CustomizationServiceError("stage_not_available")
        return self._execute_staging(change, change.request_description)

    def _execute_staging(
        self, change: ChangeSet, description: str
    ) -> dict[str, Any]:
        tenant_id = change.tenant_id
        body = self._harness_stage(tenant_id, description, change)
        return self._reconcile_staging(change, body)

    def _reconcile_staging(
        self, change: ChangeSet, body: Mapping[str, Any]
    ) -> dict[str, Any]:
        tenant_id = change.tenant_id
        idempotency_key = change.idempotency_key
        raw_receipt = body.get("receipt")
        if not isinstance(raw_receipt, Mapping):
            raise CustomizationServiceError("invalid_staged_receipt", 502)
        receipt = self._validate_receipt(raw_receipt, change)
        self._verify_catalog(tenant_id, receipt["staged_commit"], receipt["catalog_digest"])
        proposed = replace(
            change,
            staged_commit=receipt["staged_commit"],
            catalog_digest=receipt["catalog_digest"],
        )
        durable = self.store.get_change_set(
            tenant_id=tenant_id, change_set_id=change.change_set_id
        )
        if durable.state is ChangeState.STAGED:
            if self._raw_receipt(durable) != receipt:
                raise CustomizationServiceError("staged_receipt_mismatch")
            # The authenticated callback validates policy before it records
            # STAGED. The first harness response can still carry the proposed
            # tool, while a retry after a lost response carries only the
            # durable receipt. Revalidate any proposed tool against the
            # committed catalog before returning it to the browser.
            if body.get("tool") is None:
                self._verify_stage_policy(durable)
                return self._receipt(durable)
            self._verify_stage_policy(durable, body)
            return self._receipt(
                durable, tool=body.get("tool"), preview=body.get("preview")
            )
        self._verify_stage_policy(proposed, body)
        change = durable
        if change.state is ChangeState.STAGING:
            change = self.store.record_staged(
                tenant_id=tenant_id, change_set_id=change.change_set_id, expected_version=change.version,
                idempotency_key=f"staged:{idempotency_key}", staged_commit=receipt["staged_commit"],
                catalog_digest=receipt["catalog_digest"], platform_release=receipt["platform_release"],
                workspace_contract_digest=receipt["workspace_contract_digest"],
            )
        elif change.state is not ChangeState.STAGED:
            raise CustomizationServiceError("stage_not_available")
        self._verify_stage_policy(change, body)
        return self._receipt(change, tool=body.get("tool"), preview=body.get("preview"))

    def stage_status(self, *, tenant: Any, change_set_id: str) -> dict[str, Any]:
        change = self.store.get_change_set(
            tenant_id=_tenant_id(tenant), change_set_id=change_set_id
        )
        return self.stage_status_change(change)

    def stage_status_change(self, change: ChangeSet) -> dict[str, Any]:
        if change.staged_commit and change.catalog_digest:
            status = "staged"
        elif change.state is ChangeState.FAILED:
            status = "failed"
        elif (change.state is ChangeState.STAGING and change.stage_lease_owner
              and (change.stage_lease_expires_at or 0) > int(time.time() * 1000)):
            status = "running"
        else:
            status = "queued"
        result: dict[str, Any] = {
            "contract": "leaf.customization-stage-job.v1",
            "change_set_id": change.change_set_id,
            "status": status,
            "change_kind": change.change_kind,
            "attempt": change.stage_attempt,
            "phase": (
                "staged" if status == "staged"
                else "failed" if status == "failed"
                else "authoring" if status == "running"
                else "queued"
            ),
            "updated_at": change.updated_at,
            "poll_url": f"/api/author/stages/{change.change_set_id}",
            "retry_after_ms": 1000,
        }
        if change.target_tool_name:
            result["target_tool_name"] = change.target_tool_name
        if status == "staged":
            receipt = self._raw_receipt(change)
            result["receipt"] = receipt
            result["result"] = {"tool": self._staged_tool(change)}
        elif status == "failed":
            result["error"] = {
                "reason_code": change.stage_error_code or "customization_stage_failed",
                "retryable": bool(change.stage_error_retryable),
            }
        return result

    @staticmethod
    def _staged_tool(change: ChangeSet) -> dict[str, Any]:
        """Read the authored tool from the receipt-bound commit, never main."""
        try:
            bare = _bare_repo(change.tenant_id)
            staged_registry = json.loads(_git_blob(
                bare,
                f"{change.staged_commit}:registry.json",
            ).decode("utf-8"))
            base_registry = json.loads(_git_blob(
                bare,
                f"{change.base_commit}:registry.json",
            ).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CustomizationServiceError("invalid_staged_catalog", 502) from exc
        staged_tools = staged_registry.get("tools") if isinstance(staged_registry, dict) else None
        base_tools = base_registry.get("tools") if isinstance(base_registry, dict) else None
        if not isinstance(staged_tools, list) or not isinstance(base_tools, list):
            raise CustomizationServiceError("invalid_staged_catalog", 502)
        staged_by_name = {
            item.get("name"): item for item in staged_tools
            if isinstance(item, dict) and isinstance(item.get("name"), str)
        }
        if change.change_kind == "revise":
            tool = staged_by_name.get(change.target_tool_name)
        else:
            base_names = {
                item.get("name") for item in base_tools
                if isinstance(item, dict) and isinstance(item.get("name"), str)
            }
            added = [
                item for name, item in staged_by_name.items()
                if name not in base_names
            ]
            tool = added[0] if len(added) == 1 else None
        if not isinstance(tool, dict):
            raise CustomizationServiceError("invalid_staged_catalog", 502)
        return dict(tool)

    def _harness_stage(self, tenant_id: str, description: str, change: ChangeSet) -> Mapping[str, Any]:
        url, secret = _harness_config()
        if not url:
            raise CustomizationServiceError("customization_harness_unavailable", 503)
        try:
            import requests
            response = requests.post(
                f"{url}/author/stage", timeout=_harness_stage_timeout_s(),
                headers={"X-Harness-Secret": secret},
                json={"tenant_id": tenant_id, "description": description, "changeSetId": change.change_set_id,
                      "expectedBaseSha": change.base_commit, "platformRelease": change.desired_platform_release,
                      "workspaceContractDigest": change.workspace_contract_digest,
                      "idempotencyKey": change.idempotency_key,
                      **({"targetToolName": getattr(change, "target_tool_name", None)}
                         if getattr(change, "target_tool_name", None) else {})},
            )
            response.raise_for_status()
            body = response.json()
        except Exception as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            raise CustomizationServiceError(
                "customization_harness_unavailable", 503,
                f"harness_stage_failed: {url}/author/stage status={status} "
                f"{type(exc).__name__}",
            ) from exc
        if not isinstance(body, Mapping):
            raise CustomizationServiceError(
                "invalid_staged_receipt", 502,
                f"staged_receipt_not_a_mapping: {type(body).__name__}",
            )
        return body

    def _validate_receipt(self, body: Mapping[str, Any], change: ChangeSet) -> dict[str, Any]:
        fields = {"contract", "tenant_id", "change_set_id", "state", "base_commit", "staged_commit", "catalog_digest", "platform_release", "workspace_contract_digest", "idempotency_key"}
        if set(body) != fields or any(body.get(k) != v for k, v in {
            "contract": CONTRACT, "tenant_id": change.tenant_id, "change_set_id": change.change_set_id,
            "state": "staged", "base_commit": change.base_commit, "platform_release": change.desired_platform_release,
            "workspace_contract_digest": change.workspace_contract_digest, "idempotency_key": change.idempotency_key,
        }.items()):
            raise CustomizationServiceError("invalid_staged_receipt", 502)
        for key, length in (("staged_commit", 40), ("catalog_digest", 64), ("workspace_contract_digest", 64)):
            value = body.get(key)
            if not isinstance(value, str) or len(value) != length or value.lower() != value:
                raise CustomizationServiceError("invalid_staged_receipt", 502)
            try: int(value, 16)
            except ValueError: raise CustomizationServiceError("invalid_staged_receipt", 502)
        return dict(body)

    @staticmethod
    def _verify_stage_policy(
        change: ChangeSet, body: Mapping[str, Any] | None = None
    ) -> None:
        """Validate every changed path and the trusted derived registry update."""
        bare = _bare_repo(change.tenant_id)
        changed_raw = _git(
            bare, "diff-tree", "--no-commit-id", "--name-only", "-r", "-z",
            change.base_commit, change.staged_commit or "",
        )
        changed = [path for path in changed_raw.split("\0") if path]
        if not changed or "registry.json" not in changed:
            raise CustomizationServiceError("invalid_staged_paths", 422)
        policy = load_policy()
        for path in changed:
            try:
                mutability = classify_path(policy, change.desired_platform_release, path)
            except PlatformReleasePolicyError as exc:
                raise CustomizationServiceError("invalid_staged_paths", 422) from exc
            if mutability == "frozen" and path != "registry.json":
                raise CustomizationServiceError("frozen_path_changed", 403)
            if mutability not in {"slushy", "tenant_owned", "frozen"}:
                raise CustomizationServiceError("unclassified_path_changed", 403)
        tree = _git(bare, "ls-tree", "-r", "-z", change.staged_commit or "")
        for entry in (item for item in tree.split("\0") if item):
            metadata, _, path = entry.partition("\t")
            mode = metadata.split(" ", 1)[0]
            if path in changed and mode not in {"100644", "100755"}:
                code = (
                    "staged_symlink_denied"
                    if mode == "120000"
                    else "staged_file_mode_denied"
                )
                raise CustomizationServiceError(code, 403)
        registry_raw = _git_blob(
            bare, f"{change.staged_commit}:registry.json"
        ).decode("utf-8")
        base_registry_raw = _git_blob(
            bare, f"{change.base_commit}:registry.json"
        ).decode("utf-8")
        try:
            registry = json.loads(registry_raw)
            base_registry = json.loads(base_registry_raw)
        except json.JSONDecodeError as exc:
            raise CustomizationServiceError("invalid_staged_catalog", 422) from exc
        tools = registry.get("tools") if isinstance(registry, dict) else None
        base_tools = base_registry.get("tools") if isinstance(base_registry, dict) else None
        if not isinstance(tools, list) or not isinstance(base_tools, list):
            raise CustomizationServiceError("invalid_staged_catalog", 422)
        base_by_name = {
            item.get("name"): item for item in base_tools
            if isinstance(item, dict) and isinstance(item.get("name"), str)
        }
        staged_by_name = {
            item.get("name"): item for item in tools
            if isinstance(item, dict) and isinstance(item.get("name"), str)
        }
        if len(base_by_name) != len(base_tools) or len(staged_by_name) != len(tools):
            raise CustomizationServiceError("invalid_staged_catalog", 422)
        added = [item for name, item in staged_by_name.items() if name not in base_by_name]
        removed = [name for name in base_by_name if name not in staged_by_name]
        modified = [
            name for name, item in base_by_name.items()
            if name in staged_by_name and staged_by_name[name] != item
        ]
        if change.change_kind == "create":
            if modified:
                raise CustomizationServiceError("existing_catalog_entry_changed", 403)
            if len(added) != 1 or removed:
                raise CustomizationServiceError("invalid_staged_catalog_delta", 422)
            staged_tool = added[0]
        elif change.change_kind == "revise":
            target = change.target_tool_name
            if added or removed or modified != [target]:
                raise CustomizationServiceError("invalid_staged_catalog_delta", 422)
            base_tool = base_by_name.get(target)
            staged_tool = staged_by_name.get(target)
            if not isinstance(base_tool, dict) or not isinstance(staged_tool, dict):
                raise CustomizationServiceError("invalid_staged_catalog_delta", 422)
            for key in ("name", "entry", "kind", "engine_op", "capabilities", "params", "returns"):
                if staged_tool.get(key) != base_tool.get(key):
                    raise CustomizationServiceError("invalid_staged_catalog_revision", 422)
            base_version = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", str(base_tool.get("version", "")))
            staged_version = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", str(staged_tool.get("version", "")))
            if (not base_version or not staged_version
                    or staged_version.groups()[:2] != base_version.groups()[:2]
                    or int(staged_version.group(3)) != int(base_version.group(3)) + 1):
                raise CustomizationServiceError("invalid_staged_catalog_revision", 422)
            entry_path = base_tool.get("entry")
            if not isinstance(entry_path, str):
                raise CustomizationServiceError("invalid_staged_tool_entry", 422)
            allowed = {
                "registry.json", entry_path,
                f"tools/{target}/tool.json",
            }
            if set(changed) != allowed:
                raise CustomizationServiceError("invalid_staged_paths", 422)
        else:
            raise CustomizationServiceError("invalid_staged_catalog_delta", 422)
        entry = staged_tool.get("entry")
        if (not isinstance(entry, str) or not entry.startswith("tools/")
                or entry not in changed):
            raise CustomizationServiceError("invalid_staged_tool_entry", 422)
        if body is not None:
            tool = body.get("tool")
            if not isinstance(tool, Mapping) or dict(tool) != staged_tool:
                raise CustomizationServiceError("invalid_staged_tool", 422)

    def confirm(self, *, tenant_id: str, change_set_id: str) -> dict[str, Any]:
        tenant_id = _tenant_id(tenant_id)
        if not deps.auth_live():
            raise CustomizationServiceError("customization_auth_required", 503)
        if not enabled(6, tenant_id):
            raise CustomizationServiceError("customization_publish_disabled", 404)
        try:
            change = self.store.get_change_set(
                tenant_id=tenant_id, change_set_id=change_set_id
            )
        except ChangeSetNotFoundError as exc:
            raise CustomizationServiceError("confirmation_not_available", 404) from exc
        if change.state is not ChangeState.STAGED or not change.staged_commit or not change.catalog_digest:
            raise CustomizationServiceError("confirmation_not_available")
        staff = os.environ.get("LEAF_CUSTOMIZATION_INTERNAL_APPROVER_SUBJECT", "").strip()
        if not staff:
            raise CustomizationServiceError("staff_authority_unavailable", 503)
        confirmation = self._authority().issue_publish_confirmation(
            staged_change=StagedChange(tenant_id, change.change_set_id, change.staged_commit, change.catalog_digest,
                                        change.desired_platform_release, change.workspace_contract_digest,
                                        change.author_subject, True),
            author_binding=TenantBinding(tenant_id, change.author_subject, "owner", True),
            staff_authority=StaffAuthority(staff, True, True),
        )
        return {"confirmation_id": confirmation.confirmation_id}

    def pending_confirmation(
        self, *, tenant: Any, receipt: Mapping[str, Any]
    ) -> dict[str, Any]:
        tenant_id = _tenant_id(tenant)
        if not enabled(6, tenant_id):
            raise CustomizationServiceError("customization_publish_disabled", 404)
        binding = _binding(tenant)
        change_id = receipt.get("change_set_id")
        if not isinstance(change_id, str):
            raise CustomizationServiceError("invalid_staged_receipt", 422)
        change = self.store.get_change_set(
            tenant_id=tenant_id, change_set_id=change_id
        )
        validated = self._validate_receipt(receipt, change)
        if self._raw_receipt(change) != dict(validated):
            raise CustomizationServiceError("staged_receipt_mismatch")
        if binding.subject != change.author_subject and binding.role != "owner":
            raise CustomizationServiceError("tenant_role_denied", 403)
        record = self.store.find_unconsumed_confirmation(
            tenant_id=tenant_id, change_set_id=change_id
        )
        if record is None:
            raise CustomizationServiceError("independent_approval_pending", 409)
        return {"confirmation_id": record["confirmation_id"]}

    @staticmethod
    def _publication_status(change: ChangeSet, status: str) -> dict[str, Any]:
        """Return the bounded agent-facing view, without approval material."""
        result: dict[str, Any] = {
            "contract": CONTRACT,
            "change_set_id": change.change_set_id,
            "status": status,
        }
        if status == "published":
            result["catalog_digest"] = change.catalog_digest
        return result

    def request_publication(
        self, *, tenant: Any, change_set_id: str
    ) -> dict[str, Any]:
        """Continue publication from durable data under the account policy."""
        tenant_id = _tenant_id(tenant)
        if not deps.auth_live():
            raise CustomizationServiceError("customization_auth_required", 503)
        if not enabled(6, tenant_id):
            raise CustomizationServiceError("customization_publish_disabled", 404)

        # JWT callers still need a current owner/editor binding. A subject-less
        # TenantContext is produced only by require_tenant's exact dispatch
        # back-edge allowlist, whose authority can request continuation but can
        # neither issue nor supply the independent confirmation.
        if getattr(tenant, "subject", None) is not None:
            binding = _binding(tenant)
            if binding.role not in {"owner", "editor"}:
                raise CustomizationServiceError("tenant_role_denied", 403)

        try:
            change = self.store.get_change_set(
                tenant_id=tenant_id, change_set_id=change_set_id
            )
            if change.state is ChangeState.STAGING:
                return self._publication_status(change, "staging")
            continuation = self.store.get_or_create_publication_request(
                tenant_id=tenant_id, change_set_id=change.change_set_id
            )
        except ChangeSetNotFoundError as exc:
            raise CustomizationServiceError(
                "publication_request_not_available", 404
            ) from exc

        if change.state is ChangeState.PUBLISHED:
            try:
                effective = self.store.get_effective_catalog(tenant_id=tenant_id)
            except ChangeSetNotFoundError as exc:
                raise CustomizationServiceError("publish_not_available") from exc
            if effective.change_set_id != change.change_set_id:
                raise CustomizationServiceError("publish_not_available")
            return self._publication_status(change, "published")

        if change.state not in {ChangeState.STAGED, ChangeState.PUBLISHING}:
            raise CustomizationServiceError("publication_request_not_available")

        # Denial is terminal for the durable request regardless of later
        # account-policy changes. It is resolved before policy or receipt
        # lookup so no subsequent authority can revive the denied work.
        if (change.state is ChangeState.STAGED
                and continuation.get("status") == "denied"):
            return self._publication_status(change, "denied")

        publication_enabled, approval_required = self._publication_policy_state(
            tenant_id
        )
        if not publication_enabled:
            raise CustomizationServiceError("tool_publication_disabled", 409)

        confirmation_id = continuation.get("confirmation_id")
        if change.state is ChangeState.STAGED:
            record = self.store.find_unconsumed_confirmation(
                tenant_id=tenant_id, change_set_id=change.change_set_id
            )
            if (approval_required and record is not None
                    and self._is_automatic_publication_confirmation(record)):
                record = None
            if (record is None
                    and not approval_required):
                record = self._issue_automatic_publication_confirmation(change)
            if record is None:
                return self._publication_status(change, "awaiting_approval")
            if confirmation_id != record["confirmation_id"]:
                continuation = self.store.bind_publication_confirmation(
                    tenant_id=tenant_id,
                    change_set_id=change.change_set_id,
                    confirmation_id=record["confirmation_id"],
                )
                confirmation_id = continuation["confirmation_id"]
        elif confirmation_id is None:
            raise CustomizationServiceError(
                "publish_recovery_authority_invalid", 403
            )

        request = PublishRequest(
            change.change_set_id,
            change.staged_commit or "",
            change.catalog_digest or "",
            change.desired_platform_release,
            change.workspace_contract_digest,
        )
        key_material = json.dumps(
            {"tenant_id": tenant_id, "change_set_id": change.change_set_id},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        idempotency_key = (
            "publication-request:"
            + hashlib.sha256(key_material).hexdigest()
        )
        self._publish(
            tenant=tenant,
            request=request,
            confirmation_id=confirmation_id,
            idempotency_key=idempotency_key,
            require_actor_binding=False,
        )
        durable = self.store.get_change_set(
            tenant_id=tenant_id, change_set_id=change.change_set_id
        )
        if durable.state is not ChangeState.PUBLISHED:
            raise CustomizationServiceError("customization_publish_incomplete", 503)
        return self._publication_status(durable, "published")

    @staticmethod
    def _publication_policy_state(tenant_id: str) -> tuple[bool, bool]:
        """Read the account's tighten-only publication policy.

        Missing state means approval is off. An unavailable or invalid policy
        authority requires approval, so an outage can never auto-publish.
        """
        try:
            state = agent_policy.load_tenant_state(tenant_id)
            action = agent_policy.effective_action(
                agent_policy.load_policy(),
                "request_publication",
                tenant_overlay=state["overlay"],
            )
        except Exception:  # noqa: BLE001 - policy authority outages fail closed
            return True, True
        if action is None:
            return True, True
        return action.enabled, action.policy != "auto"

    @staticmethod
    def _is_automatic_publication_confirmation(record: Mapping[str, Any]) -> bool:
        payload = record.get("payload")
        return (
            isinstance(payload, Mapping)
            and payload.get("approver_subject")
            == "leaf:server:auto-publication-policy"
        )

    def _issue_automatic_publication_confirmation(
        self, change: ChangeSet
    ) -> dict[str, Any]:
        """Mint the same exact, signed, one-use receipt as manual approval.

        The fixed subject is server-owned and distinct from every Auth0 author.
        It grants no reusable authority and appears in the durable publication
        audit as the approver subject.
        """
        confirmation = self._authority().issue_publish_confirmation(
            staged_change=StagedChange(
                change.tenant_id,
                change.change_set_id,
                change.staged_commit or "",
                change.catalog_digest or "",
                change.desired_platform_release,
                change.workspace_contract_digest,
                change.author_subject,
                True,
            ),
            author_binding=TenantBinding(
                change.tenant_id, change.author_subject, "owner", True
            ),
            staff_authority=StaffAuthority(
                "leaf:server:auto-publication-policy", True, True
            ),
        )
        return {"confirmation_id": confirmation.confirmation_id}

    def deny_publication(
        self, *, tenant_id: str, change_set_id: str
    ) -> dict[str, Any]:
        """Record an independent trusted denial without issuing authority."""
        tenant_id = _tenant_id(tenant_id)
        if not deps.auth_live():
            raise CustomizationServiceError("customization_auth_required", 503)
        if not enabled(6, tenant_id):
            raise CustomizationServiceError("customization_publish_disabled", 404)
        change = self.store.get_change_set(
            tenant_id=tenant_id, change_set_id=change_set_id
        )
        if change.state is not ChangeState.STAGED:
            raise CustomizationServiceError("publication_denial_not_available")
        staff = os.environ.get(
            "LEAF_CUSTOMIZATION_INTERNAL_APPROVER_SUBJECT", ""
        ).strip()
        if not staff:
            raise CustomizationServiceError("staff_authority_unavailable", 503)
        self.store.get_or_create_publication_request(
            tenant_id=tenant_id,
            change_set_id=change.change_set_id,
        )
        self.store.deny_publication_request(
            tenant_id=tenant_id,
            change_set_id=change.change_set_id,
            reason_code="independent_approver_denied",
        )
        return self._publication_status(change, "denied")

    def publish(self, *, tenant: Any, request: PublishRequest, confirmation_id: str, idempotency_key: str) -> dict[str, Any]:
        return self._publish(
            tenant=tenant,
            request=request,
            confirmation_id=confirmation_id,
            idempotency_key=idempotency_key,
            require_actor_binding=True,
        )

    def _publish(
        self, *, tenant: Any, request: PublishRequest, confirmation_id: str,
        idempotency_key: str, require_actor_binding: bool,
    ) -> dict[str, Any]:
        tenant_id = _tenant_id(tenant)
        if not enabled(6, tenant_id):
            raise CustomizationServiceError("customization_publish_disabled", 404)
        if require_actor_binding:
            binding = _binding(tenant)
            if binding.role not in {"owner", "editor"}:
                raise CustomizationServiceError("tenant_role_denied", 403)
        change = self.store.get_change_set(tenant_id=tenant_id, change_set_id=request.change_set_id)
        if (change.staged_commit, change.catalog_digest, change.desired_platform_release, change.workspace_contract_digest) != (
            request.staged_commit, request.catalog_digest, request.platform_release, request.workspace_contract_digest):
            raise CustomizationServiceError("staged_receipt_mismatch")
        if change.state is ChangeState.PUBLISHED:
            effective = self.store.get_effective_catalog(tenant_id=tenant_id)
            if effective.change_set_id == change.change_set_id:
                return {"contract": CONTRACT, "tenant_id": effective.tenant_id,
                        "change_set_id": effective.change_set_id, "catalog_commit": effective.catalog_commit,
                        "catalog_digest": effective.catalog_digest,
                        "platform_release": effective.effective_platform_release,
                        "workspace_contract_digest": effective.workspace_contract_digest}
            raise CustomizationServiceError("publish_not_available")
        if change.state not in {ChangeState.STAGED, ChangeState.PUBLISHING}:
            raise CustomizationServiceError("publish_not_available")
        self._verify_catalog(tenant_id, request.staged_commit, request.catalog_digest)
        if change.state is ChangeState.PUBLISHING:
            try:
                confirmation, signature = (
                    self._authority().verify_publish_confirmation(
                        tenant_id=tenant_id,
                        request=request,
                        confirmation_id=confirmation_id,
                        allow_consumed=True,
                    )
                )
            except AuthorityError as exc:
                raise CustomizationServiceError(
                    "publish_recovery_authority_invalid", 403
                ) from exc
            if confirmation.approver_subject != change.approver_subject:
                raise CustomizationServiceError("publish_recovery_authority_invalid", 403)
        else:
            confirmation, signature = self._authority().verify_publish_confirmation(
                tenant_id=tenant_id,
                request=request,
                confirmation_id=confirmation_id,
            )
        publishing = self.store.prepare_publish(
            tenant_id=tenant_id,
            change_set_id=change.change_set_id,
            confirmation_id=confirmation_id,
            confirmation_signature=signature,
            approver_subject=confirmation.approver_subject,
            idempotency_key=idempotency_key,
        )
        published_commit = self._harness_publish(change)
        if published_commit != request.staged_commit:
            raise CustomizationServiceError("published_commit_mismatch", 502)
        effective = self.store.publish(tenant_id=tenant_id, change_set_id=change.change_set_id,
            expected_version=publishing.version, idempotency_key=idempotency_key,
            approver_subject=confirmation.approver_subject)
        return {"contract": CONTRACT, "tenant_id": effective.tenant_id, "change_set_id": effective.change_set_id,
                "catalog_commit": effective.catalog_commit, "catalog_digest": effective.catalog_digest,
                "platform_release": effective.effective_platform_release,
                "workspace_contract_digest": effective.workspace_contract_digest}

    def _harness_publish(self, change: ChangeSet) -> str:
        url = os.environ.get("LEAF_AUTHOR_HARNESS_URL", "").rstrip("/")
        if not url:
            raise CustomizationServiceError("customization_harness_unavailable", 503)
        receipt = self._raw_receipt(change)
        try:
            import requests
            response = requests.post(
                f"{url}/author/publish",
                timeout=120,
                headers={"X-Harness-Secret": os.environ.get("LEAF_HARNESS_SECRET", "").strip()},
                json={"tenant_id": change.tenant_id, "receipt": receipt,
                      "expectedMainSha": change.base_commit},
            )
            response.raise_for_status()
            body = response.json()
        except Exception as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            raise CustomizationServiceError(
                "customization_publish_incomplete", 503,
                f"harness_publish_failed: {url}/author/publish status={status} "
                f"{type(exc).__name__}",
            ) from exc
        commit = body.get("commit") if isinstance(body, Mapping) else None
        if not isinstance(commit, str):
            raise CustomizationServiceError(
                "customization_publish_incomplete", 502,
                f"publish_commit_not_a_string: {type(commit).__name__}",
            )
        return commit

    def record_staged_callback(self, *, tenant_id: str, receipt: Mapping[str, Any]) -> dict[str, Any]:
        tenant_id = _tenant_id(tenant_id)
        if not deps.auth_live():
            raise CustomizationServiceError("customization_auth_required", 503)
        change_id = receipt.get("change_set_id")
        if not isinstance(change_id, str):
            raise CustomizationServiceError("invalid_staged_receipt", 422)
        change = self.store.get_change_set(tenant_id=tenant_id, change_set_id=change_id)
        validated = self._validate_receipt(receipt, change)
        self._verify_catalog(tenant_id, validated["staged_commit"], validated["catalog_digest"])
        proposed = replace(
            change,
            staged_commit=validated["staged_commit"],
            catalog_digest=validated["catalog_digest"],
        )
        self._verify_stage_policy(proposed)
        if change.state is ChangeState.STAGING:
            change = self.store.record_staged(
                tenant_id=tenant_id,
                change_set_id=change.change_set_id,
                expected_version=change.version,
                idempotency_key=f"staged:{change.idempotency_key}",
                staged_commit=validated["staged_commit"],
                catalog_digest=validated["catalog_digest"],
                platform_release=validated["platform_release"],
                workspace_contract_digest=validated["workspace_contract_digest"],
            )
        elif change.state is not ChangeState.STAGED or self._raw_receipt(change) != dict(validated):
            raise CustomizationServiceError("staged_receipt_mismatch")
        return {"accepted": True, "change_set_id": change.change_set_id}

    def authorize_publish_callback(
        self,
        *,
        tenant_id: str,
        receipt: Mapping[str, Any],
        expected_main_sha: str,
    ) -> dict[str, Any]:
        tenant_id = _tenant_id(tenant_id)
        if not deps.auth_live():
            raise CustomizationServiceError("customization_auth_required", 503)
        change_id = receipt.get("change_set_id")
        if not isinstance(change_id, str):
            raise CustomizationServiceError("invalid_staged_receipt", 422)
        change = self.store.get_change_set(tenant_id=tenant_id, change_set_id=change_id)
        validated = self._validate_receipt(receipt, change)
        if change.state is not ChangeState.PUBLISHING:
            raise CustomizationServiceError("publish_not_authorized", 403)
        if expected_main_sha != change.base_commit or self._raw_receipt(change) != dict(validated):
            raise CustomizationServiceError("staged_receipt_mismatch")
        self._verify_catalog(tenant_id, change.staged_commit or "", change.catalog_digest or "")
        return {"authorized": True, "change_set_id": change.change_set_id}

    @staticmethod
    def platform_release() -> str:
        return (
            os.environ.get("LEAF_PLATFORM_RELEASE", "").strip()
            or CustomizationService._release().release_id
        )

    def capture_deployment_snapshot(self, *, idempotency_key: str) -> dict[str, Any]:
        snapshot = self.store.capture_deployment_snapshot(
            platform_release=self.platform_release(),
            idempotency_key=idempotency_key,
        )
        return {
            "snapshot_id": snapshot["snapshot_id"],
            "audit_id": snapshot["audit_id"],
            "effective_catalog_release": snapshot["effective_catalog_digest"],
            "platform_release": snapshot["platform_release"],
        }

    def verify_deployment(
        self, *, snapshot_id: str, expected_effective_catalog_release: str,
        expected_platform_release: str, idempotency_key: str,
    ) -> dict[str, Any]:
        if self.platform_release() != expected_platform_release:
            raise CustomizationServiceError("platform_release_mismatch", 409)
        if snapshot_id == "bootstrap-empty":
            current = self.store.effective_catalog_snapshot()
            if current["catalogs"] or expected_effective_catalog_release != "bootstrap-empty":
                raise CustomizationServiceError("effective_catalog_mismatch", 409)
            return {
                "snapshot_id": snapshot_id,
                "effective_catalog_release": "bootstrap-empty",
                "platform_release": expected_platform_release,
                "audit_id": "bootstrap-empty-verify",
            }
        result = self.store.verify_deployment_snapshot(
            snapshot_id=snapshot_id, action="verify",
            idempotency_key=idempotency_key,
        )
        if (not result["verified"]
                or result["effective_catalog_digest"] != expected_effective_catalog_release):
            raise CustomizationServiceError("effective_catalog_mismatch", 409)
        return {
            "snapshot_id": snapshot_id,
            "effective_catalog_release": result["effective_catalog_digest"],
            "platform_release": expected_platform_release,
            "audit_id": result["audit_id"],
        }

    def restore_deployment_snapshot(
        self, *, snapshot_id: str, idempotency_key: str
    ) -> dict[str, Any]:
        result = self.store.restore_deployment_snapshot(
            snapshot_id=snapshot_id, idempotency_key=idempotency_key
        )
        return {
            "snapshot_id": snapshot_id,
            "catalog_restored": True,
            "effective_catalog_release": result["effective_catalog_digest"],
            "platform_release": result["platform_release"],
            "audit_id": result["audit_id"],
        }

    def verify_restored_deployment(
        self, *, snapshot_id: str, idempotency_key: str
    ) -> dict[str, Any]:
        snapshot = self.store.get_deployment_snapshot(snapshot_id=snapshot_id)
        if self.platform_release() != snapshot["platform_release"]:
            raise CustomizationServiceError("platform_release_mismatch", 409)
        result = self.store.verify_deployment_snapshot(
            snapshot_id=snapshot_id, action="restore_verify",
            idempotency_key=idempotency_key,
        )
        if not result["verified"]:
            raise CustomizationServiceError("effective_catalog_mismatch", 409)
        return {
            "snapshot_id": snapshot_id,
            "catalog_verified": True,
            "effective_catalog_release": result["effective_catalog_digest"],
            "platform_release": snapshot["platform_release"],
            "audit_id": result["audit_id"],
        }

    def rollback(self, *, tenant: Any, change_set_id: str, idempotency_key: str) -> dict[str, Any]:
        tenant_id = _tenant_id(tenant)
        if not enabled(6, tenant_id):
            raise CustomizationServiceError("customization_rollback_disabled", 404)
        binding = _binding(tenant)
        if binding.role not in {"owner", "editor"}:
            raise CustomizationServiceError("tenant_role_denied", 403)
        current = self.store.get_effective_catalog(tenant_id=tenant_id)
        target = self.store.get_change_set(tenant_id=tenant_id, change_set_id=change_set_id)
        if target.state is not ChangeState.PUBLISHED:
            raise CustomizationServiceError("rollback_target_invalid")
        result = self.store.restore_effective_catalog(tenant_id=tenant_id, target_change_set_id=target.change_set_id,
            prior_change_set_id=current.change_set_id, idempotency_key=idempotency_key)
        return {"contract": CONTRACT, "tenant_id": result.tenant_id, "change_set_id": result.change_set_id,
                "catalog_commit": result.catalog_commit, "catalog_digest": result.catalog_digest,
                "platform_release": result.effective_platform_release}

    def _receipt(self, change: ChangeSet, **extra: Any) -> dict[str, Any]:
        return {"receipt": self._raw_receipt(change),
                **{k: v for k, v in extra.items() if v is not None}}

    @staticmethod
    def _raw_receipt(change: ChangeSet) -> dict[str, Any]:
        if not change.staged_commit or not change.catalog_digest:
            raise CustomizationServiceError("stage_not_available")
        return {"contract": CONTRACT, "tenant_id": change.tenant_id,
                "change_set_id": change.change_set_id, "state": "staged",
                "base_commit": change.base_commit, "staged_commit": change.staged_commit,
                "catalog_digest": change.catalog_digest,
                "platform_release": change.desired_platform_release,
                "workspace_contract_digest": change.workspace_contract_digest,
                "idempotency_key": change.idempotency_key}

    @staticmethod
    def _verify_catalog(tenant_id: str, commit: str, digest: str) -> None:
        bare = _bare_repo(tenant_id)
        registry = _git_blob(bare, f"{commit}:registry.json")
        if hashlib.sha256(registry).hexdigest() != digest:
            raise CustomizationServiceError("catalog_digest_mismatch")


# --- effective-catalog materialization ------------------------------------
#
# `git worktree add` is NOT safe to run twice at once on one path. git records
# the target as junk and REMOVES it on its own failure path, so the caller that
# loses the race deletes the directory the winner just populated -- measured on
# Linux at ~9% of contended attempts, and in some of those the winner's own
# `add` still exits 0. The deletion then leaves the path registered in the bare
# repo, and every later add fails for good with "is a missing but already
# registered worktree", so one lost race becomes a durable 503 for that tenant
# rather than a transient one.
#
# Hence: one add at a time per tenant repository, prune the leftovers of any
# earlier loss before retrying, and keep git's stderr for the operator.

_MATERIALIZE_ATTEMPTS = 5
_MATERIALIZE_LOCK_TIMEOUT = 60.0
# `git worktree add` writes this into the new worktree's HEAD as a placeholder
# and only replaces it once the checkout finishes, so a reader that arrives
# mid-add sees it from `rev-parse HEAD` with exit status 0. Observing it means
# "still being built", never "the wrong commit".
_NULL_OID = "0" * 40

_worktree_guard = threading.Lock()
_worktree_locks: dict[str, threading.Lock] = {}


def _git_stderr(result: subprocess.CompletedProcess) -> str:
    """Collapse git's stderr to one bounded line fit for a log record."""
    return " ".join((result.stderr or "").split())[:500]


def _unavailable(detail: str) -> CustomizationServiceError:
    """Log the operator-facing cause, return the client-safe 503."""
    _LOG.warning("effective_catalog_unavailable: %s", detail)
    return CustomizationServiceError("effective_catalog_unavailable", 503, detail=detail)


def _tenant_worktree_lock(bare: Path) -> threading.Lock:
    """One in-process lock per tenant repository.

    Keyed on the bare repo rather than the target path so that two different
    pinned commits for one tenant cannot add concurrently -- `worktree prune`
    below would otherwise be free to delete a sibling add still in flight.
    """
    key = str(bare)
    with _worktree_guard:
        lock = _worktree_locks.get(key)
        if lock is None:
            lock = _worktree_locks[key] = threading.Lock()
        return lock


# The errnos that mean "someone else holds it". Everything else is a lock that
# waiting cannot win.
_LOCK_CONTENDED = frozenset(
    code for code in (
        getattr(errno, "EACCES", None),
        getattr(errno, "EAGAIN", None),
        getattr(errno, "EWOULDBLOCK", None),
        getattr(errno, "EDEADLK", None),
        getattr(errno, "EDEADLOCK", None),
    ) if code is not None
)


def _try_os_lock(handle) -> bool:
    """Take an exclusive OS lock on an open file, without blocking.

    Returns False only for genuine contention. An error no amount of waiting
    can clear -- a bad descriptor, exhausted lock resources, a filesystem that
    does not implement locking -- is raised immediately instead, because
    retrying it for the full timeout and then reporting "held by another
    worker" would name a holder that does not exist. Misreporting the cause of
    a failure is the exact fault this change set exists to remove.
    """
    try:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        elif msvcrt is not None:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        # Otherwise there is no OS lock on this platform and the in-process
        # lock is the only guard, which still covers the threaded case this was
        # written for.
        return True
    except OSError as exc:
        if exc.errno in _LOCK_CONTENDED:
            return False
        raise


@contextmanager
def _exclusive_materialize(bare: Path, lock_path: Path):
    """Serialize materialization for one tenant across threads AND processes.

    The cross-process half is an advisory lock on an open file descriptor, not
    a lock directory with a staleness heuristic. That choice is the whole point:
    the kernel drops the lock when the descriptor closes, including when the
    holder is killed, so a crashed worker cannot wedge the tenant AND nothing
    ever has to guess whether a live holder is dead.

    Guessing is unsound at any threshold. Evicting a holder that is merely slow
    puts two `git worktree add` calls on one path, which is precisely the
    failure this lock exists to prevent; no ownership tagging of the eviction
    repairs that, because by then both are already inside. So there is no
    eviction here at all -- the failure mode is designed out rather than
    handled.

    The lock file is created once and never unlinked. Removing it would let a
    later caller open a fresh inode while a holder still owns the old one, and
    two holders would again run at once.
    """
    deadline = time.monotonic() + _MATERIALIZE_LOCK_TIMEOUT
    # Bounded, so a queue of in-process callers cannot wait without a limit
    # before the cross-process deadline below even starts counting.
    lock = _tenant_worktree_lock(bare)
    if not lock.acquire(timeout=_MATERIALIZE_LOCK_TIMEOUT):
        raise _unavailable(
            f"in-process materialization lock busy past "
            f"{_MATERIALIZE_LOCK_TIMEOUT}s: {lock_path}"
        )
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with open(lock_path, "a+b") as handle:
            while not _try_os_lock(handle):
                if time.monotonic() >= deadline:
                    raise _unavailable(
                        f"materialization lock held by another worker past "
                        f"{_MATERIALIZE_LOCK_TIMEOUT}s: {lock_path}"
                    )
                time.sleep(0.05)
            # Closing the handle releases the OS lock on every exit path,
            # including an exception in the body and including process death.
            yield
    finally:
        lock.release()


def _worktree_add(bare: Path, target: Path, commit: str) -> subprocess.CompletedProcess:
    """Add the detached worktree, CAPTURING stderr.

    The previous DEVNULL here is why three CI failures produced nothing but a
    generic 503: git's actual complaint was thrown away at the point of failure.
    """
    return subprocess.run(
        ["git", "-c", "core.autocrlf=false", *_git_trust(bare, target),
         "--git-dir", str(bare),
         "worktree", "add", "--detach", str(target), commit],
        text=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=20,
    )


def _discard_timed_out_add(target: Path) -> SimpleNamespace:
    """Throw away the tree a timed-out `git worktree add` may have left behind.

    Neither accepting nor immediately 503-ing is right here.

    Accepting is unsafe: subprocess.run KILLS git on timeout, and git sets HEAD
    before the checkout has finished writing files, while the digest check only
    covers registry.json. A tree killed after registry.json but before tools/
    would verify perfectly clean and serve an incomplete catalog.

    Raising straight out is also wrong: a slow-but-otherwise-fine box then turns
    a materializable catalog into a false 503.

    So discard and hand back a synthetic failure, letting the SAME prune+retry
    path a hard failure gets try again from a clean slate. A tenant on a slow box
    still gets its catalog; a torn tree is never served. Only when the retry also
    blows the budget does this become a 503, which by then is honest.

    Safe to delete: the caller holds the exclusive materialize lock and checked
    that `target` did not exist on entry, so this tree is ours. The stale
    worktree registration it leaves behind is exactly what the prune step below
    already exists to clear.
    """
    shutil.rmtree(target, ignore_errors=True)
    return SimpleNamespace(returncode=124, stderr="git worktree add timed out")


def _materialize_worktree(bare: Path, target: Path, commit: str) -> None:
    """Ensure `target` holds `commit`, serialized against every other worker."""
    target.parent.mkdir(parents=True, exist_ok=True)
    lock_path = target.parent / ".materialize.lock"
    with _exclusive_materialize(bare, lock_path):
        if target.exists():
            return  # another holder materialized it while we waited
        try:
            first = _worktree_add(bare, target, commit)
        except subprocess.TimeoutExpired:
            first = _discard_timed_out_add(target)
        if first.returncode == 0 or target.exists():
            return
        # A lost race or a crash can leave the path registered but absent, and
        # git then refuses every add on it until the registration is pruned.
        prune = subprocess.run(
            ["git", *_git_trust(bare), "--git-dir", str(bare), "worktree", "prune"],
            text=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=20,
        )
        try:
            retry = _worktree_add(bare, target, commit)
        except subprocess.TimeoutExpired:
            # Twice over budget is a real environment problem, not a blip. Discard
            # so no torn tree is left for the next caller to find, then 503.
            retry = _discard_timed_out_add(target)
        if retry.returncode != 0 and not target.exists():
            raise _unavailable(
                f"git worktree add {target} @ {commit}: rc={first.returncode} "
                f"{_git_stderr(first)!r}; prune rc={prune.returncode} "
                f"{_git_stderr(prune)!r}; retry rc={retry.returncode} "
                f"{_git_stderr(retry)!r}"
            )


def _verified_worktree(bare: Path, target: Path, commit: str, digest: str) -> None:
    """Block until `target` holds exactly `commit` with a matching registry.

    BOTH checks live inside the retry loop on purpose. A caller that reads a
    worktree another worker is still building sees torn state -- HEAD is the
    null OID for part of the build, and registry.json is incomplete for longer
    still, because git sets HEAD to the real commit BEFORE the checkout has
    finished writing files. Checking either one after the loop, as this code
    used to, turns a transient read into a terminal 503 and lets a race
    masquerade as tampering.

    Retrying is safe for the tamper case too: a tampered registry simply fails
    every attempt and still raises `effective_catalog_digest_mismatch`.
    """
    detail = "no attempt completed"
    observed = ""
    for attempt in range(_MATERIALIZE_ATTEMPTS):
        # Attempt 0 fast-paths a settled worktree without taking the lock.
        # Every later attempt goes through it, so a caller that arrived mid-add
        # blocks until the builder finishes instead of re-reading torn state.
        if attempt or not target.exists():
            _materialize_worktree(bare, target, commit)
        try:
            probe = subprocess.run(
                ["git", *_git_trust(target), "-C", str(target), "rev-parse", "HEAD"],
                text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10,
            )
            if probe.returncode != 0:
                detail = f"git rev-parse HEAD rc={probe.returncode}: {_git_stderr(probe)!r}"
            else:
                observed = probe.stdout.strip()
                if observed == _NULL_OID:
                    detail = f"{target} is still being built (HEAD is the null OID)"
                elif observed != commit:
                    detail = f"{target} HEAD {observed!r} != pinned {commit!r}"
                elif hashlib.sha256(
                    (target / "registry.json").read_bytes()
                ).hexdigest() != digest:
                    detail = f"{target}/registry.json does not match the pinned digest"
                else:
                    return
        except (OSError, subprocess.SubprocessError) as exc:
            detail = f"{type(exc).__name__}: {exc}"
        if attempt + 1 < _MATERIALIZE_ATTEMPTS:
            time.sleep(0.05 * (attempt + 1))
    # Settled and still wrong: report which invariant the worktree broke.
    if observed and observed not in (commit, _NULL_OID):
        raise CustomizationServiceError(
            "effective_catalog_commit_mismatch", 503, detail=detail
        )
    if observed == commit:
        raise CustomizationServiceError(
            "effective_catalog_digest_mismatch", 503, detail=detail
        )
    raise _unavailable(f"{target} @ {commit} unverifiable after "
                       f"{_MATERIALIZE_ATTEMPTS} attempts: {detail}")


def effective_catalog_dir(tenant_id: str) -> Path | None:
    """Materialize the durable pin, even after rollout flags are turned off.

    R5/R6 flags control mutation. Once a catalog is published, its durable pin
    remains runtime authority so a flag change cannot expose mutable ``main``.
    """
    tenant_id = _tenant_id(tenant_id)
    try:
        postgres = customization_store_mode() == "postgres"
        path = database_path()
        if not postgres and _shared_sqlite_path(path):
            if not (
                mode("LEAF_CUSTOMIZATION_R5_MODE") is RolloutMode.OFF
                and mode("LEAF_CUSTOMIZATION_R6_MODE") is RolloutMode.OFF
            ):
                raise CustomizationServiceError(
                    "customization_shared_sqlite_unsupported", 503
                )
            # The deployed shared path cannot be a valid customization
            # authority. Keep the base catalog available while rollout is off
            # without opening or migrating SQLite on EFS.
            return None
        if not postgres and not path.exists():
            if enabled(5, tenant_id) or enabled(6, tenant_id):
                raise CustomizationServiceError(
                    "effective_catalog_authority_unavailable", 503
                )
            return None
        if not postgres and _shared_sqlite_path(path):
            if enabled(5, tenant_id) or enabled(6, tenant_id):
                raise CustomizationServiceError(
                    "customization_shared_sqlite_unsupported", 503
                )
            # An unsupported shared SQLite file is never durable authority.
            # Dark rollout must keep the legacy catalog path usable without
            # opening that file during health checks or normal requests.
            return None
        service = CustomizationService.configured()
        pin = service.store.get_effective_catalog(tenant_id=tenant_id)
        bare = _bare_repo(tenant_id)
        registry = _git_blob(bare, f"{pin.catalog_commit}:registry.json")
        if hashlib.sha256(registry).hexdigest() != pin.catalog_digest:
            raise CustomizationServiceError("effective_catalog_digest_mismatch", 503)
        root = Path(
            os.environ.get("LEAF_EFFECTIVE_TENANTS_DIR", "").strip()
            or os.environ.get(
                "LEAF_CUSTOMIZATION_WORKTREES",
                str(database_path().parent / "customization-worktrees"),
            )
        )
        root.mkdir(parents=True, exist_ok=True)
        target = root / tenant_id / pin.catalog_commit
        resolved_root = root.resolve(strict=True)
        if target.is_symlink():
            raise _unavailable(f"refusing symlinked catalog target {target}")
        if not _path_within(target, resolved_root):
            raise _unavailable(f"catalog target {target} escapes {resolved_root}")
        _verified_worktree(bare, target, pin.catalog_commit, pin.catalog_digest)
        return target
    except ChangeSetNotFoundError:
        if enabled(5, tenant_id) or enabled(6, tenant_id):
            raise _unavailable(f"no effective catalog pinned for tenant {tenant_id}")
        return None
    except CustomizationServiceError:
        raise
    except (OSError, sqlite3.DatabaseError, subprocess.SubprocessError) as exc:
        raise _unavailable(f"{type(exc).__name__}: {exc}") from exc


def effective_catalog_pin(tenant_id: str) -> dict[str, str] | None:
    """Return the durable effective catalog generation without materializing it."""
    tenant_id = _tenant_id(tenant_id)
    postgres = customization_store_mode() == "postgres"
    path = database_path()
    if not postgres and _shared_sqlite_path(path):
        if (
            mode("LEAF_CUSTOMIZATION_R5_MODE") is RolloutMode.OFF
            and mode("LEAF_CUSTOMIZATION_R6_MODE") is RolloutMode.OFF
        ):
            # Match effective_catalog_dir: an unsupported shared SQLite file
            # cannot be runtime authority, but dark rollout keeps the base
            # catalog available without opening that file.
            return None
        raise CustomizationServiceError(
            "customization_shared_sqlite_unsupported", 503
        )
    if not postgres and not path.exists():
        if enabled(5, tenant_id) or enabled(6, tenant_id):
            raise CustomizationServiceError(
                "effective_catalog_authority_unavailable", 503
            )
        return None
    try:
        pin = CustomizationService.configured().store.get_effective_catalog(
            tenant_id=tenant_id
        )
    except ChangeSetNotFoundError:
        return None
    except CustomizationServiceError:
        raise
    except (OSError, sqlite3.DatabaseError, PostgresError, RuntimeError) as exc:
        raise _unavailable(f"{type(exc).__name__}: {exc}") from exc
    return {
        "catalog_commit": pin.catalog_commit,
        "effective_catalog_digest": pin.catalog_digest,
    }
