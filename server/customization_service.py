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
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping
from urllib.parse import urlsplit
from uuid import uuid4

from psycopg import Error as PostgresError

import author_quota
import agent_gate
import agent_policy
import build_plan
import change_classifier
import customization_flags
import deps
import entitlements
import platform_link
from branch_tools.package_check import check_branch_package
from branch_tools import online_signer, tenant_catalog, tenant_publish
from branch_tools.dev_publish import (
    DevPublishError, DevPublishResult, publish_branch_package, read_branch_publication,
)
from customization_authority import (
    AuthorityError, BuilderEntitlement, CustomizationAuthority,
    HmacConfirmationSigner, PublishRequest, StaffAuthority, StagedChange,
    TenantBinding,
)
from customization_flags import RolloutMode, enabled, mode
from customization_models import (
    ChangeSet, ChangeSetNotFoundError, ChangeState, ChangeSetConflictError,
    IdempotencyReplayError, InvalidTransitionError, RetryTargetInvalidError, require_uuid,
)
from customization_postgres_store import PostgresCustomizationStore
from customization_store import CustomizationRepository, SQLiteCustomizationStore
from platform_release_policy import PlatformReleasePolicyError, classify_path, load_policy
from tool_record_fields import (
    ToolRecordFieldError, validate_graph_input,
    parse_catalog_record_fields_json, canonicalize_catalog_record_fields,
)
from solar_authored_graph import reads_graph


CONTRACT = "leaf.customization.v1"
MAX_STAGED_SOURCE_BYTES = 256 * 1024
DEFAULT_DB = Path(__file__).resolve().parent / "customization.db"
_configured_lock = threading.Lock()
_configured_services: dict[str, "CustomizationService"] = {}
_LOG = logging.getLogger(__name__)


def finish_stage_authority_for_change(change: ChangeSet) -> None:
    """Terminal stage cleanup cannot undo a validated durable stage result."""
    if change.state not in {ChangeState.STAGED, ChangeState.FAILED}:
        return
    try:
        import turn_runner
        turn_runner.finish_stage_authority(
            change.tenant_id, change.authority_session_id,
            change.authority_turn_id, change.author_subject,
            stop_reason="stage_complete" if change.state is ChangeState.STAGED else "stage_failed",
        )
    except Exception as exc:
        _LOG.warning("stage_authority_cleanup_failed: exception=%s", type(exc).__name__)


_REPOSITORY_VISIBILITY_DELAYS = (0.0, 1.0, 2.0, 4.0, 8.0, 15.0, 30.0)
# Bound the extra wait to the rare dangling-worktree recovery path.
_DANGLING_RECHECK_S = 0.25
# The harness's closed 409 codes for a publish that lost for good (a lost
# expected-head CAS, a remote that did not accept). Only these end a change
# SUPERSEDED; anything else, including an unreadable 409, stays recoverable.
_HARNESS_PUBLISH_LOST = frozenset({"publish_conflict", "publish_not_accepted"})
_BRANCH_VERSION = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")


_DIAGNOSTIC_CODES = frozenset({
    "customization_refused",
    "git_failed",
    "git_show_failed",
    "git_dir_unset",
    "path_unresolvable",
    "head_missing",
    "harness_unconfigured",
    "harness_provision_failed",
    "provision_response_malformed",
    "provision_response_tenant_mismatch",
    "provision_response_commit_malformed",
    "provision_ref_not_visible",
    "platform_store_unavailable",
    "binding_resolution_failed",
    "harness_remove_failed",
    "harness_stage_job_failed",
    "harness_stage_failed",
    "staged_receipt_not_a_mapping",
    "stale_base",
    "harness_publish_lost",
    "harness_publish_failed",
    "publish_commit_not_a_string",
    "catalog_process_lock_timeout",
    "catalog_worker_lock_timeout",
    "catalog_worktree_add_failed",
    "catalog_dangling_move_failed",
    "catalog_head_probe_failed",
    "catalog_head_pending",
    "catalog_commit_mismatch",
    "catalog_digest_mismatch",
    "catalog_worktree_verification_failed",
    "catalog_symlink_rejected",
    "catalog_containment_rejected",
    "catalog_pin_missing",
    "catalog_authority_disappeared",
    "catalog_authority_changed",
    "catalog_authority_unavailable",
    "catalog_directory_failed",
    "catalog_pin_lookup_failed",
    "catalog_authority_appeared",
    "author_quota_store_failed",
    "missing_authority_tuple",
    "authority_tuple_rejected",
    "customization_unexpected_failure",
    "change_set_not_found",
})


class CustomizationServiceError(RuntimeError):
    def __init__(
        self, code: str, status_code: int = 409, detail: str = "", *,
        diagnostic_code: str | None = None, cause: BaseException | None = None,
        failures: list[dict[str, str]] | None = None,
    ) -> None:
        super().__init__(code)
        self.code, self.status_code = code, status_code
        selected = detail if diagnostic_code is None else diagnostic_code
        self.diagnostic_code = (
            selected if type(selected) is str and selected in _DIAGNOSTIC_CODES
            else "customization_refused"
        )
        self.exception_type = type(cause).__name__ if isinstance(cause, BaseException) else None
        # Legacy readers receive the validated branch code only.
        self.detail = self.diagnostic_code
        self.failures = [
            {"rule": str(item.get("rule", ""))[:100],
             "path": str(item.get("path", ""))[:300],
             "message": str(item.get("message", ""))[:300]}
            for item in (failures or [])[:50]
        ]


def _stage_request_fingerprint(description: str, target: str | None = None) -> str:
    payload = description if target != "branch" else json.dumps(
        {"description": description, "target": "branch"}, sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _valid_compile_attempts(attempts: Any) -> bool:
    def integer(value: Any, low: int, high: int) -> bool:
        return type(value) is int and low <= value <= high

    if not isinstance(attempts, list) or len(attempts) > 8:
        return False
    previous = 0
    for attempt in attempts:
        if not isinstance(attempt, dict) or attempt.keys() != {"n", "decision", "errors", "diagnostics"}:
            return False
        if (not integer(attempt["n"], previous + 1, 8)
                or attempt["decision"] not in ("accept", "compile_failed", "malformed", "unavailable")
                or not integer(attempt["errors"], 0, 10000)):
            return False
        previous = attempt["n"]
        diagnostics = attempt["diagnostics"]
        if not isinstance(diagnostics, list) or len(diagnostics) > 10:
            return False
        for diagnostic in diagnostics:
            if not isinstance(diagnostic, dict) or diagnostic.keys() != {"code", "file", "line"}:
                return False
            if (not isinstance(diagnostic["code"], str)
                    or not re.fullmatch(r"CS[0-9]{4}|scan|unparsed", diagnostic["code"])
                    or not isinstance(diagnostic["file"], str)
                    or not re.fullmatch(r"[A-Za-z0-9_./-]{1,120}", diagnostic["file"])
                    or not integer(diagnostic["line"], 0, 1000000)):
                return False
    return True


_SCHEMA_IDENTITY_MAX_DEPTH = 128
_UNCLASSIFIABLE = object()


def _schema_identity(value: Any, depth: int = 0) -> Any:
    """A comparison form in which every scalar keeps its exact JSON type.

    Python equality says True == 1 == 1.0, so it cannot tell a schema change
    from no change. Fails closed: a non-string key, a non-JSON value or a
    value nested deeper than the bound is unclassifiable and equals nothing.
    """
    if depth > _SCHEMA_IDENTITY_MAX_DEPTH:
        return _UNCLASSIFIABLE
    kind = type(value)
    if value is None or kind is bool or kind is int or kind is str:
        return (kind.__name__, value)
    if kind is float:
        return ("float", repr(value))
    if kind is list:
        items = [_schema_identity(item, depth + 1) for item in value]
        if any(item is _UNCLASSIFIABLE for item in items):
            return _UNCLASSIFIABLE
        return ("list", tuple(items))
    if kind is dict:
        if any(type(key) is not str for key in value):
            return _UNCLASSIFIABLE
        items = [(key, _schema_identity(item, depth + 1)) for key, item in value.items()]
        if any(item is _UNCLASSIFIABLE for _, item in items):
            return _UNCLASSIFIABLE
        return ("dict", tuple(sorted(items, key=lambda pair: pair[0])))
    return _UNCLASSIFIABLE


def _schema_equal(left: Any, right: Any) -> bool:
    left_form = _schema_identity(left)
    right_form = _schema_identity(right)
    if left_form is _UNCLASSIFIABLE or right_form is _UNCLASSIFIABLE:
        return False
    return left_form == right_form


def _revision_schema_change(base: Any, staged: Any) -> str:
    """Classify only unchanged schemas and top-level property additions.

    Every comparison is by exact JSON type (_schema_equal), never Python
    equality, so a true in a schema never matches a 1.
    """
    if _schema_equal(base, staged):
        return "same"
    if (not isinstance(base, dict) or not isinstance(staged, dict)
            or base.get("type") != "object" or staged.get("type") != "object"):
        return "breaking"
    if not _schema_equal({key: value for key, value in base.items() if key != "properties"},
                         {key: value for key, value in staged.items() if key != "properties"}):
        return "breaking"
    base_properties = base.get("properties", {})
    staged_properties = staged.get("properties", {})
    if (not isinstance(base_properties, dict) or not isinstance(staged_properties, dict)
            or any(not isinstance(key, str) for key in base_properties)
            or any(not isinstance(key, str) for key in staged_properties)):
        return "breaking"
    if "required" in base:
        required = base["required"]
        if (not isinstance(required, list)
                or any(not isinstance(key, str) for key in required)
                or len(set(required)) != len(required)
                or any(key not in base_properties for key in required)):
            return "breaking"
    if any(key not in staged_properties or not _schema_equal(value, staged_properties[key])
           for key, value in base_properties.items()):
        return "breaking"
    added = set(staged_properties) - set(base_properties)
    if not added or any(not isinstance(staged_properties[key], (dict, bool)) for key in added):
        return "breaking"
    return "additive"


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
    """Return the one canonical tenant identity used by flags, stores and paths.

    Inline LITERAL restatement of tenant_id_validator.TENANT_ID_PATTERN — a
    literal fullmatch is a taint barrier static analysis proves, and every
    tenant-derived path in this module flows through here. Pinned equal to the
    canonical rule by server/tests/test_codeql_barrier_literals.py.
    """
    m = re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,62}", str(value).strip())
    if m is None:
        raise CustomizationServiceError("tenant_identity_invalid", 403)
    return str(m.group(0))


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


def _git(bare: Path, *args: str, strip: bool = True) -> str:
    try:
        result = subprocess.run(
            ["git", *_git_trust(bare), "--git-dir", str(bare), *args],
            check=True, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise CustomizationServiceError(
            "tenant_repository_unavailable", 503,
            diagnostic_code="git_failed", cause=exc,
        ) from exc
    return result.stdout.strip() if strip else result.stdout


def _git_blob(bare: Path, object_name: str) -> bytes:
    try:
        result = subprocess.run(
            ["git", *_git_trust(bare), "--git-dir", str(bare), "show", object_name],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise CustomizationServiceError(
            "tenant_repository_unavailable", 503,
            diagnostic_code="git_show_failed", cause=exc,
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
            diagnostic_code="git_dir_unset",
        )
    candidate = Path(base) / f"{tenant_id}.git"
    try:
        resolved_base = Path(base).resolve(strict=True)
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(resolved_base)
    except (OSError, ValueError) as exc:
        raise CustomizationServiceError(
            "tenant_repository_unavailable", 503,
            diagnostic_code="path_unresolvable", cause=exc,
        ) from exc
    if not (resolved / "HEAD").is_file():
        raise CustomizationServiceError(
            "tenant_repository_unavailable", 503, diagnostic_code="head_missing",
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
        # Configuration values never enter operator diagnostics.
        raise CustomizationServiceError(
            "tenant_repository_unavailable", 503,
            diagnostic_code="harness_unconfigured",
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
        raise CustomizationServiceError(
            "tenant_repository_unavailable", 503,
            diagnostic_code="harness_provision_failed", cause=exc,
        ) from exc
    if not isinstance(body, Mapping):
        raise CustomizationServiceError(
            "tenant_repository_unavailable", 503,
            diagnostic_code="provision_response_malformed",
        )
    if body.get("tenant_id") != tenant_id:
        raise CustomizationServiceError(
            "tenant_repository_unavailable", 503,
            diagnostic_code="provision_response_tenant_mismatch",
        )
    expected = body.get("base_commit")
    if (
        not isinstance(expected, str)
        or len(expected) != 40
        or any(char not in "0123456789abcdef" for char in expected)
    ):
        raise CustomizationServiceError(
            "tenant_repository_unavailable", 503,
            diagnostic_code="provision_response_commit_malformed",
        )

    # App and harness are separate NFS clients. EFS can retain a negative
    # directory entry for refs/heads/main after the harness creates it. Retry
    # only local verification, never the provisioning write, until the bounded
    # visibility window expires.
    last_cause = None
    for delay in _REPOSITORY_VISIBILITY_DELAYS:
        if delay:
            time.sleep(delay)
        try:
            bare = _bare_repo(tenant_id)
            observed = _git(bare, "rev-parse", "--verify", "refs/heads/main")
        except CustomizationServiceError as exc:
            if exc.code != "tenant_repository_unavailable":
                raise
            last_cause = exc.__cause__
            continue
        if observed == expected:
            return bare
        last_cause = None
    raise CustomizationServiceError(
        "tenant_repository_unavailable", 503,
        diagnostic_code="provision_ref_not_visible", cause=last_cause,
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
                diagnostic_code="platform_store_unavailable", cause=exc,
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
                diagnostic_code="binding_resolution_failed", cause=exc,
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

    def removal_authority(self, *, tenant: Any, tool_name: str) -> dict[str, Any]:
        """Project one immutable tenant-row removal authority tuple."""
        tenant_id = _tenant_id(tenant)
        if not isinstance(tool_name, str) or not re.fullmatch(
            r"[a-z0-9]+(?:-[a-z0-9]+)*", tool_name
        ):
            raise CustomizationServiceError("invalid_removal_request", 422)
        binding = _binding(tenant)
        if binding.role not in {"owner", "editor"}:
            raise CustomizationServiceError("tenant_role_denied", 403)
        current = self.store.get_effective_catalog(tenant_id=tenant_id)
        raw = _git_blob(_bare_repo(tenant_id), f"{current.catalog_commit}:registry.json")
        if not hmac.compare_digest(hashlib.sha256(raw).hexdigest(), current.catalog_digest):
            raise CustomizationServiceError("effective_catalog_digest_mismatch", 409)
        try:
            registry = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CustomizationServiceError("effective_catalog_malformed", 503) from exc
        if not isinstance(registry, dict) or not isinstance(registry.get("tools"), list):
            raise CustomizationServiceError("effective_catalog_malformed", 503)
        names = []
        for row in registry["tools"]:
            if not isinstance(row, dict) or not isinstance(row.get("name"), str):
                raise CustomizationServiceError("effective_catalog_malformed", 503)
            names.append(row["name"])
        row_count = names.count(tool_name)
        if row_count > 1:
            raise CustomizationServiceError("removal_target_ambiguous", 409)
        return {
            "contract": "leaf.customization-removal-authority.v1",
            "tool_name": tool_name,
            "effective_catalog_digest": current.catalog_digest,
            "target_row_count": row_count,
            "target_provenance": (
                "tenant_repo" if row_count == 1 else "operator_owned_engine_expected"
            ),
            "removal_authorized": row_count == 1,
        }

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

    def _author_stage_key(self, tenant_id: str, key: str, subject: str | None) -> str:
        """Prefer the author's reservation; reuse a legacy key only for its author."""
        if not isinstance(subject, str) or not subject.strip():
            raise CustomizationServiceError("tenant_identity_binding_unavailable", 403)
        scoped = "author-stage:" + hashlib.sha256(
            json.dumps([key, subject], separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        try:
            for candidate in (scoped, key):
                try:
                    prior = self.store.get_change_set_by_idempotency(
                        tenant_id=tenant_id, idempotency_key=candidate
                    )
                except ChangeSetNotFoundError:
                    continue
                if prior.author_subject == subject:
                    return candidate
                if candidate == scoped:
                    raise CustomizationServiceError("idempotency_replay", 409)
        except ToolRecordFieldError as exc:
            raise CustomizationServiceError("record_fields_invalid", 503) from exc
        except (sqlite3.DatabaseError, PostgresError, OSError, RuntimeError) as exc:
            if isinstance(exc, CustomizationServiceError):
                raise
            raise CustomizationServiceError("record_fields_read_failed", 503) from exc
        return scoped

    def _reserve_stage(self, **kwargs: Any) -> ChangeSet:
        try:
            change, _created = self.store.reserve_stage(**kwargs)
            return change
        except RetryTargetInvalidError as exc:
            raise CustomizationServiceError("retry_target_invalid", 409) from exc
        except IdempotencyReplayError as exc:
            raise CustomizationServiceError("idempotency_replay", 409) from exc
        except (ChangeSetConflictError, ChangeSetNotFoundError) as exc:
            raise CustomizationServiceError("record_fields_predecessor_mismatch", 409) from exc
        except (sqlite3.DatabaseError, PostgresError, OSError, RuntimeError) as exc:
            raise CustomizationServiceError("record_fields_write_failed", 503) from exc

    def _stage_admission_base(
        self, tenant_id: str, key: str, graph_input: Any, release: Any,
        *, check_harness: bool = False,
    ) -> tuple[str, str | None]:
        try:
            prior = self.store.get_change_set_by_idempotency(
                tenant_id=tenant_id, idempotency_key=key
            )
        except ChangeSetNotFoundError:
            prior = None
        except ToolRecordFieldError as exc:
            raise CustomizationServiceError("record_fields_invalid", 503) from exc
        except (sqlite3.DatabaseError, PostgresError, OSError, RuntimeError) as exc:
            raise CustomizationServiceError("record_fields_read_failed", 503) from exc
        if prior is not None:
            if (type(graph_input) is not type(prior.request_graph_input)
                    or graph_input != prior.request_graph_input):
                raise CustomizationServiceError("idempotency_replay", 409)
            return prior.base_commit, prior.base_catalog_change_set_id
        if graph_input is not None:
            validate_graph_input(graph_input)
        if check_harness and _harness_misconfigured():
            raise CustomizationServiceError("customization_harness_unavailable", 503)
        bare = _ensure_bare_repo(tenant_id)
        base = _git(bare, "rev-parse", "refs/heads/main")
        try:
            pin = self.store.get_effective_catalog(tenant_id=tenant_id)
        except ChangeSetNotFoundError:
            return base, None
        except (sqlite3.DatabaseError, PostgresError, OSError, RuntimeError) as exc:
            raise CustomizationServiceError("record_fields_read_failed", 503) from exc
        predecessor = self._record_fields_change(tenant_id, pin.change_set_id)
        if (predecessor.request_graph_input is not None
                or predecessor.base_catalog_change_set_id is not None
                or predecessor.catalog_record_fields_json is not None):
            if (pin.tenant_id != tenant_id or predecessor.tenant_id != tenant_id
                    or predecessor.staged_commit != base
                    or pin.catalog_commit != base
                    or predecessor.state not in {ChangeState.PUBLISHED, ChangeState.ROLLED_BACK}
                    or predecessor.catalog_digest != pin.catalog_digest
                    or predecessor.desired_platform_release != pin.effective_platform_release
                    or predecessor.workspace_contract_digest != pin.workspace_contract_digest
                    or pin.effective_platform_release != release.release_id
                    or pin.workspace_contract_digest != release.workspace_contract_sha256):
                raise CustomizationServiceError("record_fields_predecessor_mismatch", 409)
            self._verified_record_fields(predecessor, set())
            return base, predecessor.change_set_id
        return base, None

    def _record_fields_change(self, tenant_id: str, change_id: str) -> ChangeSet:
        try:
            return self.store.get_change_set(tenant_id=tenant_id, change_set_id=change_id)
        except (ChangeSetNotFoundError, ToolRecordFieldError) as exc:
            # A stored snapshot the store cannot parse is invalid metadata, never an unmapped error.
            raise CustomizationServiceError("record_fields_invalid", 503) from exc
        except (sqlite3.DatabaseError, PostgresError, OSError, RuntimeError) as exc:
            raise CustomizationServiceError("record_fields_read_failed", 503) from exc

    def _record_fields_pin(self, tenant_id: str):
        try:
            return self.store.get_effective_catalog(tenant_id=tenant_id)
        except ChangeSetNotFoundError:
            return None
        except (sqlite3.DatabaseError, PostgresError, OSError, RuntimeError) as exc:
            raise CustomizationServiceError("record_fields_read_failed", 503) from exc

    @staticmethod
    def _pin_identity(pin):
        if pin is None:
            return None
        return (
            pin.tenant_id, pin.change_set_id, pin.catalog_commit, pin.catalog_digest,
            pin.effective_platform_release, pin.workspace_contract_digest,
        )

    @staticmethod
    def _pin_matches_change(pin, change: ChangeSet) -> bool:
        return pin is not None and CustomizationService._pin_identity(pin) == (
            change.tenant_id, change.change_set_id, change.staged_commit,
            change.catalog_digest, change.desired_platform_release,
            change.workspace_contract_digest,
        )

    def _verify_record_fields_predecessor(self, change: ChangeSet) -> None:
        if (change.request_graph_input is None
                and change.base_catalog_change_set_id is None
                and change.catalog_record_fields_json is None):
            return
        pin = self._record_fields_pin(change.tenant_id)
        predecessor_id = change.base_catalog_change_set_id
        if predecessor_id is not None:
            predecessor = self._record_fields_change(change.tenant_id, predecessor_id)
            if not self._pin_matches_change(pin, predecessor):
                raise CustomizationServiceError("record_fields_predecessor_mismatch", 409)
        elif pin is not None:
            predecessor = self._record_fields_change(change.tenant_id, pin.change_set_id)
            if (predecessor.request_graph_input is not None
                    or predecessor.base_catalog_change_set_id is not None
                    or predecessor.catalog_record_fields_json is not None):
                raise CustomizationServiceError("record_fields_predecessor_mismatch", 409)

    @staticmethod
    def _record_fields_rows(change: ChangeSet, commit: str, digest: str | None = None) -> dict[str, Any]:
        try:
            raw = _git_blob(_bare_repo(change.tenant_id), f"{commit}:registry.json")
        except (CustomizationServiceError, OSError) as exc:
            raise CustomizationServiceError("record_fields_read_failed", 503) from exc
        try:
            if digest is not None and hashlib.sha256(raw).hexdigest() != digest:
                raise ValueError("metadata catalog digest mismatch")
            def pairs(items):
                result = {}
                for key, value in items:
                    if key in result:
                        raise ValueError("duplicate registry key")
                    result[key] = value
                return result
            def constant(_value):
                raise ValueError("non-finite registry value")
            registry = json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)
            tools = registry["tools"]
            if not isinstance(tools, list):
                raise ValueError("invalid registry tools")
            rows = {row["name"]: row for row in tools}
            if len(rows) != len(tools) or any(
                not isinstance(name, str) or not isinstance(row, dict)
                for name, row in rows.items()
            ):
                raise ValueError("invalid registry rows")
            return rows
        except (ValueError, TypeError, KeyError, UnicodeDecodeError, RecursionError) as exc:
            raise CustomizationServiceError("record_fields_invalid", 503) from exc

    @staticmethod
    def _record_fields_binding(row: Mapping[str, Any], graph: str) -> dict[str, Any]:
        try:
            return {
                "version": row["version"], "graph_input": graph,
                "record_sha256": hashlib.sha256(json.dumps(
                    row, sort_keys=True, separators=(",", ":"),
                    ensure_ascii=True, allow_nan=False,
                ).encode("utf-8")).hexdigest(),
            }
        except (ValueError, TypeError, KeyError, RecursionError) as exc:
            raise CustomizationServiceError("record_fields_invalid", 503) from exc

    def _inherited_record_fields(self, change: ChangeSet, seen: set[str]) -> dict[str, Any]:
        predecessor_id = change.base_catalog_change_set_id
        if predecessor_id is None:
            return {}
        if predecessor_id in seen:
            raise CustomizationServiceError("record_fields_invalid", 503)
        predecessor = self._record_fields_change(change.tenant_id, predecessor_id)
        if (predecessor.tenant_id != change.tenant_id
                or predecessor.staged_commit != change.base_commit
                or predecessor.state not in {ChangeState.PUBLISHED, ChangeState.ROLLED_BACK}
                or predecessor.desired_platform_release != change.desired_platform_release
                or predecessor.workspace_contract_digest != change.workspace_contract_digest):
            raise CustomizationServiceError("record_fields_predecessor_mismatch", 409)
        snapshot = self._verified_record_fields(predecessor, seen)
        if snapshot is None:
            raise CustomizationServiceError("record_fields_invalid", 503)
        return dict(parse_catalog_record_fields_json(snapshot)["tools"])

    def _verified_record_fields(self, change: ChangeSet, seen: set[str]) -> str | None:
        try:
            stored = change.catalog_record_fields_json
            if stored is not None:
                stored = canonicalize_catalog_record_fields(parse_catalog_record_fields_json(stored))
            derived = self._derive_catalog_record_fields(change, _seen=seen)
            if stored != derived:
                raise CustomizationServiceError("record_fields_invalid", 503)
            return derived
        except ToolRecordFieldError as exc:
            raise CustomizationServiceError("record_fields_invalid", 503) from exc
        except RecursionError as exc:
            raise CustomizationServiceError("record_fields_invalid", 503) from exc
        except CustomizationServiceError as exc:
            if exc.code in {
                "graph_input_consumer_unsupported", "graph_input_unreserved",
                "record_fields_predecessor_mismatch",
            }:
                raise CustomizationServiceError("record_fields_invalid", 503) from exc
            raise

    def _derive_catalog_record_fields(
        self, change: ChangeSet, *, removal_target: str | None = None,
        _seen: set[str] | None = None,
    ) -> str | None:
        """Derive complete metadata from the frozen reservation and raw Git rows."""
        if (change.request_graph_input is None
                and change.base_catalog_change_set_id is None
                and change.catalog_record_fields_json is None):
            return None
        seen = set() if _seen is None else set(_seen)
        if change.change_set_id in seen:
            raise CustomizationServiceError("record_fields_invalid", 503)
        seen.add(change.change_set_id)
        try:
            if change.request_graph_input is not None:
                validate_graph_input(change.request_graph_input)
            inherited = self._inherited_record_fields(change, seen)
            rows = self._record_fields_rows(change, change.staged_commit or "", change.catalog_digest)
            base = self._record_fields_rows(change, change.base_commit)
            if change.tool_target == "branch":
                if change.request_graph_input is not None:
                    raise CustomizationServiceError("graph_input_consumer_unsupported", 422)
                if rows != base:
                    raise CustomizationServiceError("record_fields_invalid", 503)
                return canonicalize_catalog_record_fields({
                    "schema": "leaf.customization-record-fields.v1", "tools": inherited,
                })
            self._verify_raw_graph_declarations(change, base, rows, inherited)
            if removal_target is None:
                marker = self.store.get_removal_request(
                    tenant_id=change.tenant_id, change_set_id=change.change_set_id
                )
                removal_target = marker["target_tool_name"] if marker else None
            bindings = dict(inherited)
            if removal_target is not None:
                if (set(base) - set(rows) != {removal_target}
                        or set(rows) - set(base)
                        or any(rows[name] != base[name] for name in rows)):
                    raise CustomizationServiceError("record_fields_invalid", 503)
                bindings.pop(removal_target, None)
            else:
                if change.change_kind == "create":
                    added = set(rows) - set(base)
                    if len(added) != 1:
                        raise CustomizationServiceError("record_fields_invalid", 503)
                    target = next(iter(added))
                else:
                    target = change.target_tool_name
                graph = change.request_graph_input
                if graph is None and target in inherited:
                    graph = inherited[target]["graph_input"]
                if graph is not None:
                    if target not in rows:
                        raise CustomizationServiceError("record_fields_invalid", 503)
                    bindings[target] = self._record_fields_binding(rows[target], graph)
            for name, binding in bindings.items():
                if (name not in rows
                        or binding != self._record_fields_binding(rows[name], binding["graph_input"])):
                    raise CustomizationServiceError("record_fields_invalid", 503)
                if not reads_graph({**rows[name], "graph_input": binding["graph_input"]}):
                    raise CustomizationServiceError("graph_input_consumer_unsupported", 422)
            return canonicalize_catalog_record_fields({
                "schema": "leaf.customization-record-fields.v1", "tools": bindings,
            })
        except ToolRecordFieldError as exc:
            raise CustomizationServiceError("record_fields_invalid", 503) from exc
        except (sqlite3.DatabaseError, PostgresError, OSError, RuntimeError) as exc:
            if isinstance(exc, CustomizationServiceError):
                raise
            raise CustomizationServiceError("record_fields_read_failed", 503) from exc

    def stage(
        self, *, tenant: Any, description: str, mode: str,
        idempotency_key: str, target_tool_name: str | None = None,
        target: str | None = None,
        authority_session_id: str | None = None,
        authority_turn_id: str | None = None,
        graph_input: Any = None,
    ) -> dict[str, Any]:
        if target not in (None, "studio", "branch"):
            raise CustomizationServiceError("invalid_target", 422)
        tenant_id = _tenant_id(tenant)
        if customization_flags.plan_mode_enabled(tenant_id):
            return self.enqueue_stage(
                tenant=tenant, description=description, mode=mode, idempotency_key=idempotency_key,
                target=target, target_tool_name=target_tool_name, graph_input=graph_input,
                authority_session_id=authority_session_id, authority_turn_id=authority_turn_id,
            )
        if not enabled(5, tenant_id):
            raise CustomizationServiceError("customization_stage_disabled", 404)
        if mode != "build" or not isinstance(description, str) or not description.strip():
            raise CustomizationServiceError("invalid_stage_request", 422)
        if target_tool_name is not None:
            if (not isinstance(target_tool_name, str)
                    or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", target_tool_name)
                    or len(target_tool_name) > 64):
                raise CustomizationServiceError("invalid_stage_request", 422)
        authority_required = bool(
            os.environ.get("LEAF_TENANT_MCP_BROKER_URL", "").strip()
        )
        authority_values = (authority_session_id, authority_turn_id)
        if any(authority_values) != all(authority_values):
            raise CustomizationServiceError("invalid_stage_authority", 422)
        if authority_required and not all(authority_values):
            raise CustomizationServiceError("active_stage_authority_required", 409)
        for value in authority_values:
            if value is not None and not re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}", value
            ):
                raise CustomizationServiceError("invalid_stage_authority", 422)
        binding = _binding(tenant)
        tier = entitlements.resolve_tier(tenant)
        roles, elevated = entitlements.resolve_roles(tenant)
        if not entitlements.entitlements_for(tier, roles, elevated).get("build", False):
            raise CustomizationServiceError("builder_entitlement_missing", 403)
        self._authority().authorize_stage(
            binding=binding,
            builder_entitlement=BuilderEntitlement(tenant_id, binding.subject, True, True),
        )
        release = self._release()
        idempotency_key = self._author_stage_key(
            tenant_id, idempotency_key, binding.subject
        )
        base, predecessor = self._stage_admission_base(
            tenant_id, idempotency_key, graph_input, release
        )
        if target == "branch" and target_tool_name:
            self._branch_revision_base(tenant_id, base, target_tool_name)
        change = self._reserve_stage(
            tenant_id=tenant_id, idempotency_key=idempotency_key, base_commit=base,
            desired_platform_release=release.release_id,
            workspace_contract_digest=release.workspace_contract_sha256,
            author_subject=binding.subject or "", change_set_id=str(uuid4()),
            change_kind="revise" if target_tool_name else "create",
            target_tool_name=target_tool_name,
            **({"tool_target": "branch"} if target == "branch" else {}),
            authority_session_id=authority_session_id,
            authority_turn_id=authority_turn_id,
            request_description=description,
            request_fingerprint=_stage_request_fingerprint(description, target),
            request_graph_input=graph_input,
            base_catalog_change_set_id=predecessor,
        )
        if change.state is ChangeState.CREATED:
            change = self.store.transition(
                tenant_id=tenant_id, change_set_id=change.change_set_id,
                next_state=ChangeState.STAGING, expected_version=change.version,
                idempotency_key=f"stage:{idempotency_key}", expected_state=ChangeState.CREATED,
            )
        if change.state is ChangeState.STAGED:
            changed = self._verify_bound_stage_policy(change)
            return self._receipt(change, changed=changed)
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
        target: str | None = None,
        authority_session_id: str | None = None,
        authority_turn_id: str | None = None,
        graph_input: Any = None,
        plan_confirmation_id: str | None = None,
        retry_of_change_set_id: str | None = None,
    ) -> dict[str, Any]:
        """Reserve and charge one exact stage request without calling the harness."""
        if target not in (None, "studio", "branch"):
            raise CustomizationServiceError("invalid_target", 422)
        tenant_id = _tenant_id(tenant)
        if not enabled(5, tenant_id):
            raise CustomizationServiceError("customization_stage_disabled", 404)
        plan_fields = {}
        change_kind = "revise" if target_tool_name else "create"
        if customization_flags.plan_mode_enabled(tenant_id):
            record = agent_gate.consumed_build_plan(
                tenant_id, authority_session_id, authority_turn_id,
                getattr(tenant, "subject", None), plan_confirmation_id,
            )
            if record is None:
                raise CustomizationServiceError("plan_required", 409)
            plan = record["args"]["plan"]
            if ((target is not None and target != plan["target"])
                    or (target_tool_name is not None and target_tool_name != plan.get("target_tool_name"))
                    or graph_input is not None):
                raise CustomizationServiceError("plan_binding_mismatch", 422)
            target = plan["target"]
            change_kind = plan["change_kind"]
            target_tool_name = plan.get("target_tool_name")
            description = build_plan.render_description(plan)
            plan_fields = {
                "plan_confirmation_id": record["confirmation_id"],
                "plan_digest": build_plan.plan_digest(plan),
                "plan_json": build_plan.canonical_bytes(plan).decode("utf-8"),
            }
            gated_retry = record.get("retry_of_change_set_id")
            if gated_retry is not None and retry_of_change_set_id not in (None, gated_retry):
                raise CustomizationServiceError("retry_target_invalid", 409)
            retry = gated_retry if gated_retry is not None else retry_of_change_set_id
            if retry is not None:
                if not isinstance(retry, str):
                    raise CustomizationServiceError("retry_target_invalid", 409)
                try:
                    plan_fields["retry_of_change_set_id"] = require_uuid(retry)
                except ValueError as exc:
                    raise CustomizationServiceError("retry_target_invalid", 409) from exc
        if (mode != "build" or not isinstance(description, str)
                or not description.strip() or (not plan_fields and len(description) > 8000)):
            raise CustomizationServiceError("invalid_stage_request", 422)
        if target_tool_name is not None and (
            not isinstance(target_tool_name, str)
            or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", target_tool_name)
            or len(target_tool_name) > 64
        ):
            raise CustomizationServiceError("invalid_stage_request", 422)
        authority_required = bool(
            os.environ.get("LEAF_TENANT_MCP_BROKER_URL", "").strip()
        )
        authority_values = (authority_session_id, authority_turn_id)
        if any(authority_values) != all(authority_values):
            raise CustomizationServiceError("invalid_stage_authority", 422)
        if authority_required and not all(authority_values):
            raise CustomizationServiceError("active_stage_authority_required", 409)
        for value in authority_values:
            if value is not None and not re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}", value
            ):
                raise CustomizationServiceError("invalid_stage_authority", 422)
        binding = _binding(tenant)
        tier = entitlements.resolve_tier(tenant)
        roles, elevated = entitlements.resolve_roles(tenant)
        if not entitlements.entitlements_for(tier, roles, elevated).get("build", False):
            raise CustomizationServiceError("builder_entitlement_missing", 403)
        self._authority().authorize_stage(
            binding=binding,
            builder_entitlement=BuilderEntitlement(
                tenant_id, binding.subject, True, True
            ),
        )
        release = self._release()
        idempotency_key = self._author_stage_key(
            tenant_id, idempotency_key, binding.subject
        )
        base, predecessor = self._stage_admission_base(
            tenant_id, idempotency_key, graph_input, release,
            check_harness=True,
        )
        if target == "branch" and target_tool_name:
            self._branch_revision_base(tenant_id, base, target_tool_name)
        fingerprint = _stage_request_fingerprint(description, target)
        change = self._reserve_stage(
            tenant_id=tenant_id, idempotency_key=idempotency_key,
            base_commit=base, desired_platform_release=release.release_id,
            workspace_contract_digest=release.workspace_contract_sha256,
            author_subject=binding.subject or "", change_set_id=str(uuid4()),
            change_kind=change_kind,
            target_tool_name=target_tool_name, request_description=description,
            **plan_fields,
            **({"tool_target": "branch"} if target == "branch" else {}),
            request_fingerprint=fingerprint,
            request_graph_input=graph_input,
            base_catalog_change_set_id=predecessor,
            authority_session_id=authority_session_id,
            authority_turn_id=authority_turn_id,
        )
        if change.state is ChangeState.CREATED:
            try:
                author_quota.enforce(
                    tenant_id, tier, idempotency_key=change.change_set_id
                )
            except (author_quota.AuthorQuotaExceeded,
                    author_quota.AuthorQuotaStoreError):
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

    def stage_removal(
        self, *, tenant: Any, tool_name: str,
        expected_catalog_digest: str, idempotency_key: str,
    ) -> dict[str, Any]:
        """Stage deletion of one exact tenant row without widening catalog edits."""
        tenant_id = _tenant_id(tenant)
        if not enabled(6, tenant_id):
            raise CustomizationServiceError("customization_publish_disabled", 404)
        if (
            not isinstance(tool_name, str)
            or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", tool_name)
            or len(tool_name) > 64
            or not isinstance(expected_catalog_digest, str)
            or not re.fullmatch(r"[0-9a-f]{64}", expected_catalog_digest)
        ):
            raise CustomizationServiceError("invalid_removal_request", 422)
        binding = _binding(tenant)
        if binding.role not in {"owner", "editor"}:
            raise CustomizationServiceError("tenant_role_denied", 403)
        current = self._record_fields_pin(tenant_id)
        if current is None:
            raise ChangeSetNotFoundError("no effective catalog")
        if not hmac.compare_digest(current.catalog_digest, expected_catalog_digest):
            raise CustomizationServiceError("effective_catalog_digest_mismatch", 409)
        idempotency_key = self._author_stage_key(
            tenant_id, idempotency_key, binding.subject
        )
        try:
            change = self.store.get_change_set_by_idempotency(
                tenant_id=tenant_id, idempotency_key=idempotency_key
            )
        except ChangeSetNotFoundError:
            predecessor = self._record_fields_change(tenant_id, current.change_set_id)
            if (predecessor.request_graph_input is not None
                    or predecessor.base_catalog_change_set_id is not None
                    or predecessor.catalog_record_fields_json is not None):
                self._verify_catalog(tenant_id, current.catalog_commit, current.catalog_digest)
                self._verified_record_fields(predecessor, set())
            if not self._pin_matches_change(current, predecessor):
                raise CustomizationServiceError("record_fields_predecessor_mismatch", 409)
            change = self.store.create_change_set(
                tenant_id=tenant_id, idempotency_key=idempotency_key,
                base_commit=current.catalog_commit,
                desired_platform_release=current.effective_platform_release,
                workspace_contract_digest=current.workspace_contract_digest,
                author_subject=binding.subject or "", change_set_id=str(uuid4()),
                change_kind="revise", target_tool_name=tool_name,
                base_catalog_change_set_id=(
                    predecessor.change_set_id
                    if predecessor.catalog_record_fields_json is not None else None
                ),
            )
        except ToolRecordFieldError as exc:
            raise CustomizationServiceError("record_fields_invalid", 503) from exc
        except (sqlite3.DatabaseError, PostgresError, OSError, RuntimeError) as exc:
            raise CustomizationServiceError("record_fields_read_failed", 503) from exc
        self._verify_record_fields_predecessor(change)
        self.store.bind_removal_request(
            tenant_id=tenant_id, change_set_id=change.change_set_id,
            target_tool_name=tool_name,
            expected_catalog_digest=expected_catalog_digest,
        )
        if change.state is ChangeState.CREATED:
            change = self.store.transition(
                tenant_id=tenant_id, change_set_id=change.change_set_id,
                next_state=ChangeState.STAGING, expected_version=change.version,
                idempotency_key=f"remove:{idempotency_key}",
                expected_state=ChangeState.CREATED,
            )
        if change.state is ChangeState.STAGED:
            changed = self._verify_bound_stage_policy(change)
            removal = self.store.get_removal_request(
                tenant_id=tenant_id, change_set_id=change.change_set_id
            )
            return self._receipt(
                change,
                changed=changed,
                predecessor_change_set_id=removal["predecessor_change_set_id"],
                predecessor_catalog_commit=removal["predecessor_catalog_commit"],
                predecessor_catalog_digest=removal["predecessor_catalog_digest"],
            )
        if change.state is not ChangeState.STAGING:
            raise CustomizationServiceError("stage_not_available")
        body = self._harness_remove(change, expected_catalog_digest)
        receipt = self._validate_receipt(body.get("receipt", {}), change)
        proposed = replace(
            change, staged_commit=receipt["staged_commit"],
            catalog_digest=receipt["catalog_digest"],
        )
        self._verify_bound_stage_policy(proposed, body)
        snapshot = self._derive_catalog_record_fields(proposed, removal_target=tool_name)
        durable = self._record_fields_change(tenant_id, change.change_set_id)
        if durable.state is ChangeState.STAGING:
            try:
                durable = self.store.record_staged(
                    tenant_id=tenant_id, change_set_id=durable.change_set_id,
                    expected_version=durable.version,
                    idempotency_key=f"removed:{idempotency_key}",
                    staged_commit=receipt["staged_commit"],
                    catalog_digest=receipt["catalog_digest"],
                    platform_release=receipt["platform_release"],
                    workspace_contract_digest=receipt["workspace_contract_digest"],
                    catalog_record_fields_json=snapshot,
                )
            except ToolRecordFieldError as exc:
                raise CustomizationServiceError("record_fields_invalid", 503) from exc
            except (sqlite3.DatabaseError, PostgresError, OSError, RuntimeError) as exc:
                raise CustomizationServiceError("record_fields_write_failed", 503) from exc
        changed = self._verify_bound_stage_policy(durable)
        removal = self.store.get_removal_request(
            tenant_id=tenant_id, change_set_id=durable.change_set_id
        )
        return self._receipt(
            durable,
            changed=changed,
            predecessor_change_set_id=removal["predecessor_change_set_id"],
            predecessor_catalog_commit=removal["predecessor_catalog_commit"],
            predecessor_catalog_digest=removal["predecessor_catalog_digest"],
        )

    def _harness_remove(
        self, change: ChangeSet, expected_catalog_digest: str
    ) -> Mapping[str, Any]:
        url, secret = _harness_config()
        if not url:
            raise CustomizationServiceError("customization_harness_unavailable", 503)
        try:
            import requests
            response = requests.post(
                f"{url}/author/remove", timeout=_harness_stage_timeout_s(),
                headers={"X-Harness-Secret": secret},
                json={
                    "tenant_id": change.tenant_id,
                    "toolName": change.target_tool_name,
                    "expectedCatalogDigest": expected_catalog_digest,
                    "changeSetId": change.change_set_id,
                    "expectedBaseSha": change.base_commit,
                    "platformRelease": change.desired_platform_release,
                    "workspaceContractDigest": change.workspace_contract_digest,
                    "idempotencyKey": change.idempotency_key,
                },
            )
            response.raise_for_status()
            body = response.json()
        except Exception as exc:
            raise CustomizationServiceError(
                "customization_harness_unavailable", 503,
                diagnostic_code="harness_remove_failed", cause=exc,
            ) from exc
        if not isinstance(body, Mapping):
            raise CustomizationServiceError("invalid_staged_receipt", 502)
        return body

    def execute_stage(self, change: ChangeSet) -> dict[str, Any]:
        if change.state is not ChangeState.STAGING or not change.request_description:
            raise CustomizationServiceError("stage_not_available")
        return self._execute_staging(change, change.request_description)

    def dispatch_stage(self, change: ChangeSet) -> Mapping[str, Any]:
        if change.state is not ChangeState.STAGING or not change.request_description:
            raise CustomizationServiceError("stage_not_available")
        return self._harness_stage(
            change.tenant_id, change.request_description, change
        )

    def reconcile_stage_worker(
        self, change: ChangeSet, body: Mapping[str, Any], *,
        lease_owner: str, lease_attempt: int,
    ) -> dict[str, Any]:
        return self._reconcile_staging(
            change, body, lease_owner=lease_owner,
            lease_attempt=lease_attempt,
        )

    def _execute_staging(
        self, change: ChangeSet, description: str
    ) -> dict[str, Any]:
        tenant_id = change.tenant_id
        body = self._harness_stage(tenant_id, description, change)
        return self._reconcile_staging(change, body)

    def _reconcile_staging(
        self, change: ChangeSet, body: Mapping[str, Any], *,
        lease_owner: str | None = None,
        lease_attempt: int | None = None,
    ) -> dict[str, Any]:
        tenant_id = change.tenant_id
        raw_receipt = body.get("receipt")
        if not isinstance(raw_receipt, Mapping):
            raise CustomizationServiceError("invalid_staged_receipt", 502)
        receipt = self._validate_receipt(raw_receipt, change)
        try:
            durable = self.store.get_change_set(
                tenant_id=tenant_id, change_set_id=change.change_set_id
            )
        except (sqlite3.DatabaseError, PostgresError, OSError, RuntimeError) as exc:
            raise CustomizationServiceError("record_fields_read_failed", 503) from exc
        change, changed = self._complete_stage(
            durable, receipt, body=body,
            lease_owner=lease_owner, lease_attempt=lease_attempt,
        )
        return self._receipt(
            change, changed=changed,
            tool=body.get("tool"), preview=body.get("preview"),
        )

    def _complete_stage(
        self, durable: ChangeSet, receipt: Mapping[str, Any], *,
        body: Mapping[str, Any] | None = None,
        compile_summary_json: str | None = None,
        lease_owner: str | None = None, lease_attempt: int | None = None,
    ) -> tuple[ChangeSet, tuple[str, ...]]:
        def agreement(winner: ChangeSet, snapshot: str | None) -> None:
            if lease_owner is not None and winner.stage_attempt != lease_attempt:
                raise CustomizationServiceError("record_fields_stage_conflict", 409)
            if self._raw_receipt(winner) != dict(receipt):
                raise CustomizationServiceError("staged_receipt_mismatch", 409)
            if self._verified_record_fields(winner, set()) != snapshot:
                raise CustomizationServiceError("idempotency_replay", 409)

        if lease_owner is not None and durable.stage_attempt != lease_attempt:
            raise CustomizationServiceError("record_fields_stage_conflict", 409)
        if durable.state is ChangeState.STAGED and self._raw_receipt(durable) != dict(receipt):
            raise CustomizationServiceError("staged_receipt_mismatch", 409)
        proposed = replace(
            durable, staged_commit=receipt["staged_commit"],
            catalog_digest=receipt["catalog_digest"],
        )
        policy_body = body
        if durable.state is ChangeState.STAGED and body is not None and body.get("tool") is None:
            policy_body = None
        try:
            self._verify_catalog(durable.tenant_id, receipt["staged_commit"], receipt["catalog_digest"])
            changed = self._verify_bound_stage_policy(
                proposed, {"receipt": receipt} if proposed.tool_target == "branch" else policy_body
            )
        except CustomizationServiceError as exc:
            if (durable.tool_target == "branch" and exc.code == "branch_package_invalid"
                    and durable.state is ChangeState.STAGING):
                # Both stores bound reason_message to 500 characters, below the
                # branch contract's 2000-character ceiling. Fail here so sync
                # calls and harness callbacks retain the same worker diagnostic.
                message = "; ".join(
                    f"{item['rule']} {item['path']}: {item['message']}"
                    for item in exc.failures
                )[:500]
                failed = self.store.fail_stage_claim(
                    tenant_id=durable.tenant_id, change_set_id=durable.change_set_id,
                    owner=lease_owner if lease_owner is not None else durable.stage_lease_owner,
                    reason_code=exc.code, retryable=False, reason_message=message or None,
                )
                if failed is not None:
                    finish_stage_authority_for_change(failed)
            if exc.code == "tenant_repository_unavailable" and (
                durable.request_graph_input is not None
                or durable.base_catalog_change_set_id is not None
                or durable.catalog_record_fields_json is not None
            ):
                raise CustomizationServiceError("record_fields_read_failed", 503) from exc
            raise
        snapshot = self._derive_catalog_record_fields(proposed)
        if durable.state is ChangeState.STAGED:
            agreement(durable, snapshot)
            finish_stage_authority_for_change(durable)
            return durable, changed
        if durable.state is not ChangeState.STAGING:
            raise CustomizationServiceError("record_fields_stage_conflict", 409)
        try:
            completed = self.store.record_staged(
                tenant_id=durable.tenant_id, change_set_id=durable.change_set_id,
                expected_version=durable.version,
                idempotency_key=f"staged:{durable.idempotency_key}",
                staged_commit=receipt["staged_commit"], catalog_digest=receipt["catalog_digest"],
                platform_release=receipt["platform_release"],
                workspace_contract_digest=receipt["workspace_contract_digest"],
                stage_lease_owner=lease_owner, stage_attempt=lease_attempt,
                catalog_record_fields_json=snapshot,
                compile_summary_json=compile_summary_json,
            )
        except (ChangeSetConflictError, InvalidTransitionError, IdempotencyReplayError) as exc:
            winner = self._record_fields_change(durable.tenant_id, durable.change_set_id)
            if winner.state is ChangeState.STAGED:
                agreement(winner, snapshot)
                finish_stage_authority_for_change(winner)
                return winner, changed
            code = "idempotency_replay" if isinstance(exc, IdempotencyReplayError) else "record_fields_stage_conflict"
            raise CustomizationServiceError(code, 409) from exc
        except ToolRecordFieldError as exc:
            raise CustomizationServiceError("record_fields_invalid", 503) from exc
        except (sqlite3.DatabaseError, PostgresError, OSError, RuntimeError) as exc:
            raise CustomizationServiceError("record_fields_write_failed", 503) from exc
        finish_stage_authority_for_change(completed)
        return completed, changed

    # The job status each stored stage state can project to. A row whose state and
    # projection disagree (a staged state with no staged commit, a failed state that
    # carries one) is a corrupt stage row: recovery answers it as a failure, never as
    # a found job. A state with no entry here has no product writer for a stage row
    # and is refused the same way.
    _RECOVERY_JOB_STATUSES = {
        ChangeState.STAGING: frozenset({"running", "queued"}),
        ChangeState.FAILED: frozenset({"failed"}),
        ChangeState.STAGED: frozenset({"staged"}),
        ChangeState.AWAITING_APPROVAL: frozenset({"staged"}),
        ChangeState.APPROVED: frozenset({"staged"}),
        ChangeState.PUBLISHING: frozenset({"staged"}),
        ChangeState.PUBLISHED: frozenset({"staged"}),
        ChangeState.SUPERSEDED: frozenset({"staged"}),
        ChangeState.ROLLED_BACK: frozenset({"staged"}),
    }

    def recover_stage(
        self, *, tenant: Any, idempotency_key: str | None = None,
        change_set_id: str | None = None,
    ) -> dict[str, Any]:
        """Read an author's reserved stage request without current turn authority."""
        if (bool(idempotency_key) == bool(change_set_id)
                or (idempotency_key is not None and (
                    not isinstance(idempotency_key, str) or not idempotency_key.strip()
                    or len(idempotency_key) > 200))
                or (change_set_id is not None and (
                    not isinstance(change_set_id, str) or not re.fullmatch(
                        r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", change_set_id)))):
            raise CustomizationServiceError("invalid_recovery_request", 422)
        tenant_id = _tenant_id(tenant)
        if not enabled(5, tenant_id):
            raise CustomizationServiceError("customization_stage_disabled", 404)
        binding = _binding(tenant)
        if binding.role not in {"owner", "editor"}:
            raise CustomizationServiceError("tenant_role_denied", 403)
        tier = entitlements.resolve_tier(tenant)
        roles, elevated = entitlements.resolve_roles(tenant)
        if not entitlements.entitlements_for(tier, roles, elevated).get("build", False):
            raise CustomizationServiceError("builder_entitlement_missing", 403)
        not_found = {
            "contract": "leaf.customization-stage-recovery.v1", "status": "not_found",
        }
        try:
            if change_set_id is not None:
                change = self.store.get_change_set(
                    tenant_id=tenant_id, change_set_id=change_set_id
                )
            else:
                idempotency_key = self._author_stage_key(
                    tenant_id, idempotency_key, binding.subject
                )
                change = self.store.get_change_set_by_idempotency(
                    tenant_id=tenant_id, idempotency_key=idempotency_key
                )
        except ChangeSetNotFoundError:
            return not_found
        except CustomizationServiceError as exc:
            if exc.code == "record_fields_read_failed":
                raise CustomizationServiceError("customization_stage_failed", 503) from exc.__cause__
            raise
        except ToolRecordFieldError as exc:
            raise CustomizationServiceError("record_fields_invalid", 503) from exc
        except (sqlite3.DatabaseError, PostgresError, OSError, RuntimeError) as exc:
            raise CustomizationServiceError("customization_stage_failed", 503) from exc
        if change.author_subject != binding.subject:
            return not_found
        if change.request_description is None and change.request_fingerprint is None:
            return not_found
        if (not isinstance(change.request_description, str)
                or not isinstance(change.request_fingerprint, str)
                or _stage_request_fingerprint(change.request_description, change.tool_target)
                != change.request_fingerprint):
            raise CustomizationServiceError("customization_stage_failed", 503)
        if change.state is ChangeState.CREATED:
            return {
                "contract": "leaf.customization-stage-recovery.v1",
                "status": "admission_pending", "retry_after_ms": 1000,
            }
        try:
            job = self.stage_status_change(change)
        except CustomizationServiceError:
            raise
        except Exception as exc:
            raise CustomizationServiceError("customization_stage_failed", 503) from exc
        if job.get("status") not in self._RECOVERY_JOB_STATUSES.get(change.state, frozenset()):
            raise CustomizationServiceError("customization_stage_failed", 503)
        if change_set_id is not None:
            job = {**job, "version": change.version}
        return {
            "contract": "leaf.customization-stage-recovery.v1",
            "status": "found", "job": job,
        }

    def retry_author_stage(
        self, *, tenant: Any, change_set_id: str, expected_version: int,
        expected_attempt: int, authority_session_id: str | None,
        authority_turn_id: str | None,
    ) -> dict[str, Any]:
        # Lookup checks rollout, role, entitlement, original author and stored
        # request fingerprint before any admission charge or mutation.
        recovered = self.recover_stage(tenant=tenant, change_set_id=change_set_id)
        if recovered["status"] != "found":
            return recovered
        if (type(expected_version) is not int or expected_version < 0
                or type(expected_attempt) is not int or expected_attempt < 0):
            raise CustomizationServiceError("invalid_recovery_request", 422)
        if not all(isinstance(value, str) and re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}", value)
                for value in (authority_session_id, authority_turn_id)):
            raise CustomizationServiceError("stage_authority_invalid", 409)
        if deps.stage_author_identity(tenant, authority_session_id, authority_turn_id) is None:
            raise CustomizationServiceError("stage_authority_invalid", 409)
        binding = _binding(tenant)
        tenant_id = _tenant_id(tenant)
        keep_authority = False
        try:
            if customization_flags.plan_mode_enabled(tenant_id):
                # Plan retries are new, separately approved enqueue requests.
                raise CustomizationServiceError("plan_required", 409)
            self._authority().authorize_stage(
                binding=binding,
                builder_entitlement=BuilderEntitlement(tenant_id, binding.subject, True, True),
            )
            if _harness_misconfigured():
                raise CustomizationServiceError("customization_harness_unavailable", 503)
            change = self.store.retry_stage(
                tenant_id=tenant_id, change_set_id=change_set_id,
                author_subject=binding.subject, expected_version=expected_version,
                expected_attempt=expected_attempt, authority_session_id=authority_session_id,
                authority_turn_id=authority_turn_id,
                admit=lambda: author_quota.enforce(
                    tenant_id, entitlements.resolve_tier(tenant),
                    idempotency_key=f"stage-retry:{change_set_id}:{expected_version}:{expected_attempt}",
                ),
            )
            keep_authority = (change.authority_session_id, change.authority_turn_id) == (
                authority_session_id, authority_turn_id)
            return {"contract": "leaf.customization-stage-recovery.v1", "status": "found",
                    "job": {**self.stage_status_change(change), "version": change.version}}
        except ChangeSetNotFoundError:
            return {"contract": "leaf.customization-stage-recovery.v1", "status": "not_found"}
        except ChangeSetConflictError as exc:
            raise CustomizationServiceError("stage_retry_conflict", 409) from exc
        finally:
            if not keep_authority:
                # A concurrent retry can join another turn's winning job. Only
                # this unused, verified dedicated turn may then be released.
                try:
                    import turn_runner
                    turn_runner.finish_stage_authority(
                        tenant_id, authority_session_id, authority_turn_id,
                        binding.subject, stop_reason="stage_retry_not_admitted",
                    )
                except Exception as exc:
                    _LOG.warning("stage_retry_authority_cleanup_failed: exception=%s", type(exc).__name__)

    def stage_status(self, *, tenant: Any, change_set_id: str) -> dict[str, Any]:
        try:
            change = self.store.get_change_set(
                tenant_id=_tenant_id(tenant), change_set_id=change_set_id
            )
        except ToolRecordFieldError as exc:
            raise CustomizationServiceError("record_fields_invalid", 503) from exc
        return self.stage_status_change(change)

    def stage_source(self, *, tenant: Any, change_set_id: str) -> tuple[bytes, dict[str, str]]:
        """Read only the original author's active, receipt-bound tool source."""
        def eligible() -> ChangeSet:
            tenant_id = _tenant_id(tenant)
            if not enabled(5, tenant_id):
                raise CustomizationServiceError("customization_stage_disabled", 404)
            binding = _binding(tenant)
            if not isinstance(binding.subject, str) or not binding.subject.strip():
                raise CustomizationServiceError("tenant_identity_binding_unavailable", 403)
            if binding.role not in {"owner", "editor"}:
                raise CustomizationServiceError("tenant_role_denied", 403)
            tier = entitlements.resolve_tier(tenant)
            roles, elevated = entitlements.resolve_roles(tenant)
            if not entitlements.entitlements_for(tier, roles, elevated).get("build", False):
                raise CustomizationServiceError("builder_entitlement_missing", 403)
            change = self.store.get_change_set(tenant_id=tenant_id, change_set_id=change_set_id)
            if (change.tenant_id != tenant_id or change.change_set_id != change_set_id
                    or not change.author_subject or change.author_subject != binding.subject
                    or change.state is not ChangeState.STAGED
                    or change.change_kind not in {"create", "revise"}
                    or not isinstance(change.staged_commit, str)
                    or not re.fullmatch(r"[a-f0-9]{40}", change.staged_commit)
                    or not isinstance(change.catalog_digest, str)
                    or not re.fullmatch(r"[a-f0-9]{64}", change.catalog_digest)):
                raise CustomizationServiceError("stage_source_not_available", 404)
            return change

        def declaration(change: ChangeSet) -> tuple[str, str]:
            if change.tool_target == "branch":
                # A Branch package's source is its one main.csx in the receipt-bound tree.
                fields = self._branch_receipt_fields(change)
                package = fields["package_path"]
                entry = f"{package}/main.csx"
                if entry not in fields["package_files"]:
                    raise CustomizationServiceError("stage_source_not_available", 404)
                return package.split("/", 1)[1], entry
            tool = self._staged_tool(change)
            slug = tool.get("name")
            is_script = tool.get("kind") == "script"
            if (not isinstance(slug, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", slug)
                    or not is_script or tool.get("entry") != f"tools/{slug}/tool.py"):
                raise CustomizationServiceError("stage_source_not_available", 404)
            return slug, tool["entry"]

        change = eligible()
        slug, path = declaration(change)
        # Metadata reads can block too. Recheck before crossing the private hop.
        if eligible() != change:
            raise CustomizationServiceError("stage_source_not_available", 404)
        if change.tool_target == "branch":
            data, headers = self._branch_staged_source(change, path)
        else:
            data, headers = self._harness_staged_source(change, slug)
        if eligible() != change or declaration(change) != (slug, path):
            raise CustomizationServiceError("stage_source_not_available", 404)
        if eligible() != change:
            raise CustomizationServiceError("stage_source_not_available", 404)
        return data, {**headers, "X-Leaf-Source-Path": path}

    @staticmethod
    def _branch_staged_source(change: ChangeSet, path: str) -> tuple[bytes, dict[str, str]]:
        """Read the Branch source blob from the staged commit; bounded, digest-bound."""
        try:
            data = _git_blob(_bare_repo(change.tenant_id), f"{change.staged_commit}:{path}")
        except CustomizationServiceError as exc:
            raise CustomizationServiceError("stage_source_failed", 503) from exc
        if type(data) is not bytes or len(data) > MAX_STAGED_SOURCE_BYTES:
            raise CustomizationServiceError("stage_source_failed", 503)
        blob = hashlib.sha1(f"blob {len(data)}\0".encode("ascii") + data).hexdigest()
        return data, {"Content-Length": str(len(data)), "X-Leaf-Source-Commit": change.staged_commit or "",
                      "X-Leaf-Source-Blob": blob, "X-Leaf-Source-Sha256": hashlib.sha256(data).hexdigest()}

    @staticmethod
    def _harness_staged_source(change: ChangeSet, slug: str) -> tuple[bytes, dict[str, str]]:
        """Bound the response before buffering; never follow a credential redirect."""
        if _harness_misconfigured():
            raise CustomizationServiceError("stage_source_failed", 503)
        url, secret = _harness_config()
        response = None
        try:
            import requests
            response = requests.post(
                f"{url}/internal/author/staged-source", timeout=(5, 30),
                headers={"X-Harness-Secret": secret, "X-Tenant-Id": change.tenant_id,
                         "Accept-Encoding": "identity"},
                json={"tenant_id": change.tenant_id, "changeSetId": change.change_set_id,
                      "stagedCommit": change.staged_commit, "slug": slug},
                stream=True, allow_redirects=False,
            )
            if response.status_code != 200:
                raise ValueError("source unavailable")
            length = response.headers.get("Content-Length", "")
            if not re.fullmatch(r"0|[1-9][0-9]{0,5}", length):
                raise ValueError("invalid source length")
            size = int(length)
            commit = response.headers.get("X-Leaf-Source-Commit", "")
            blob = response.headers.get("X-Leaf-Source-Blob", "")
            sha256 = response.headers.get("X-Leaf-Source-Sha256", "")
            if (size > MAX_STAGED_SOURCE_BYTES or commit != change.staged_commit
                    or not re.fullmatch(r"[a-f0-9]{40}", blob)
                    or not re.fullmatch(r"[a-f0-9]{64}", sha256)
                    or response.headers.get("X-Leaf-Tool-Slug") != slug
                    or response.headers.get("Content-Encoding", "identity") != "identity"
                    or response.headers.get("Content-Type") != "application/octet-stream"):
                raise ValueError("invalid source binding")
            chunks: list[bytes] = []
            received = 0
            while received <= size:
                chunk = response.raw.read(min(8192, size + 1 - received), decode_content=False)
                if not chunk:
                    break
                if type(chunk) is not bytes or len(chunk) > min(8192, size + 1 - received):
                    raise ValueError("invalid source chunk")
                received += len(chunk)
                if received > size:
                    raise ValueError("source exceeds declared length")
                chunks.append(chunk)
            data = b"".join(chunks)
            if (received != size or hashlib.sha256(data).hexdigest() != sha256
                    or hashlib.sha1(f"blob {size}\0".encode("ascii") + data).hexdigest() != blob):
                raise ValueError("source digest mismatch")
            return data, {"Content-Length": str(size), "X-Leaf-Source-Commit": commit,
                          "X-Leaf-Source-Blob": blob, "X-Leaf-Source-Sha256": sha256}
        except Exception as exc:
            raise CustomizationServiceError("stage_source_failed", 503) from exc
        finally:
            if response is not None:
                response.close()

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
        if change.plan_json and change.plan_digest:
            plan = json.loads(change.plan_json)
            result["plan"] = {key: plan[key] for key in ("title", "tool_name", "target", "change_kind")}
            result["plan"]["digest"] = change.plan_digest
        if status in ("staged", "failed") and change.compile_summary_json is not None:
            result["compile"] = {"attempts": json.loads(change.compile_summary_json)}
        if change.tool_target == "branch":
            result["target"] = "branch"
            if change.target_tool_name:
                result["package_path"] = f"branch-tools/{change.target_tool_name}"
        if status == "staged":
            key = {"tenant_id": change.tenant_id, "change_set_id": change.change_set_id}
            review = self.store.get_review_decision(**key, staged_commit=change.staged_commit)
            if review is not None:
                result["review"] = {field: review[field] for field in (
                    "decision", "approval_kind", "approver_role", "decided_at",
                )}
            if change.state is ChangeState.PUBLISHED:
                result["publication"] = {"state": "published", "published_at": change.updated_at}
            elif change.state in (ChangeState.PUBLISHING, ChangeState.SUPERSEDED, ChangeState.ROLLED_BACK):
                result["publication"] = {"state": change.state.value}
            else:
                publication = self.store.get_publication_request(**key)
                if publication is not None:
                    result["publication"] = {"state": publication["status"]}
            receipt = self._raw_receipt(change)
            result["receipt"] = receipt
            if change.tool_target == "branch":
                self._verified_record_fields(change, set())
                result["package_path"] = receipt["package_path"]
                if change.state is ChangeState.PUBLISHED:
                    from branch_tools import sync_receipts
                    from branch_tools.vendor.cad_tool_package.runner import package_digest

                    files, name = self._branch_package_files(change)
                    manifest = json.loads(files["tool.json"])
                    branch = sync_receipts.latest(
                        self.store, change.tenant_id, name, manifest["version"], package_digest(files),
                    )
                    if branch is not None:
                        result["branch"] = branch
                else:
                    manifest = self._staged_tool(change)
                result["result"] = {"branch_package": manifest}
            else:
                result["result"] = {"tool": self._decorate_stage_tool(change, self._staged_tool(change))}
        elif status == "failed":
            result["error"] = {
                "reason_code": change.stage_error_code or "customization_stage_failed",
                "retryable": bool(change.stage_error_retryable),
                # The terminal reason a tenant can act on ("Agent SDK auth
                # failure: ..."), persisted from the harness's own error body.
                # Additive: absent when no reason was captured.
                **({"message": change.stage_error_message}
                   if change.stage_error_message else {}),
            }
        return result

    @staticmethod
    def _staged_tool(change: ChangeSet) -> dict[str, Any]:
        """Read the authored tool from the receipt-bound commit, never main."""
        if change.tool_target == "branch":
            package = CustomizationService._branch_receipt_fields(change)["package_path"]
            return json.loads(_git_blob(
                _bare_repo(change.tenant_id), f"{change.staged_commit}:{package}/tool.json"
            ))
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

    # The only harness failure messages that may cross the boundary VERBATIM.
    # The harness's catch-all 500 serializes raw exception messages (only its
    # stderr copy is token-redacted), so an arbitrary message can carry a
    # credential fragment, internal URL, or path — length and printable-char
    # filtering are not secret redaction (sol-critic, PR #553). These full-match
    # patterns pin the deliberate, authored Agent SDK terminal failures
    # (agentSdkRunner.ts); anything else surfaces as reason_code only, and the
    # full text stays in the harness's own log for operators.
    _SAFE_HARNESS_REASONS = (
        re.compile(
            r"Agent SDK auth failure: "
            r"(?:authentication_failed|oauth_org_not_allowed|billing_error)"
        ),
        re.compile(
            r"Agent SDK rate limited"
            r"(?: \(retry after ~?\d+s\)| \(retry horizon unknown\))?"
        ),
        re.compile(
            r"Agent SDK spend cap exceeded "
            r"\(turns=\d+ > \d+ or cost-tokens=\d+ > \d+\)"
        ),
    )

    @classmethod
    def _harness_job_failure(cls, response: Any) -> tuple[bool, str | None]:
        """Classify an error response: (harness answered, tenant-safe reason).

        DETECTION and SURFACING are deliberately separate (sol-critic round 2,
        PR #553). A parseable JSON error body carrying a string message means
        the harness was reachable and ANSWERED: the authoring job itself
        failed, terminally — regardless of whether its message is safe to
        show. That is distinct from a transport failure, where nothing about
        the job is known and retrying is honest.

        The reason is non-None only when it may cross the tenant boundary:
        either the body's shape marks a deliberate harness refusal
        (grant_required / llm_quota_exhausted), or the message full-matches a
        pinned safe pattern. An answered failure with an unpinned message is
        (True, None): terminal, surfaced as reason_code only.
        """
        try:
            body = response.json()
        except Exception:  # noqa: BLE001 - unparseable body: not a harness answer
            return False, None
        if not isinstance(body, dict):
            return False, None
        error = body.get("error")
        if isinstance(error, dict) and "reason" in error:
            return True, cls._branch_failure_message(error["reason"])
        message = None
        if isinstance(error, dict) and isinstance(error.get("message"), str):
            message = error["message"]
        elif isinstance(body.get("message"), str):
            # The harness quota refusal carries its message at the top level.
            message = body["message"]
        if not isinstance(message, str) or not message:
            return False, None
        cleaned = " ".join(
            "".join(ch if ch.isprintable() else " " for ch in message).split()
        )[:300]
        if not cleaned:
            return True, None
        # Shape-marked deliberate refusals: the §16 legacy lane already
        # forwards the grant message verbatim, and the quota message is a
        # fixed authored constant.
        if body.get("grant_required") is True:
            return True, cleaned
        if body.get("errorCode") == "llm_quota_exhausted":
            return True, cleaned
        if any(p.fullmatch(cleaned) for p in cls._SAFE_HARNESS_REASONS):
            return True, cleaned
        return True, None

    @staticmethod
    def _branch_failure_message(reason: Any) -> str | None:
        if not isinstance(reason, dict):
            return None
        code = reason.get("code")
        if code == "branch_revision_target_missing" and reason.keys() == {"code"}:
            return "The Branch tool to revise has no published version to start from."
        turns = reason.get("turns")
        if type(turns) is not int or not 0 <= turns <= 10000:
            return None
        if code == "branch_author_unsuccessful" and reason.keys() == {"code", "turns"}:
            return f"The Branch author session did not finish successfully (turns={turns})."
        attempts = reason.get("submit_calls")
        if type(attempts) is not int or not 0 <= attempts <= 10000:
            return None
        counts = f"(turns={turns}, submit attempts={attempts})."
        if code == "branch_author_no_submission" and reason.keys() == {"code", "turns", "submit_calls"}:
            return f"The Branch author session ended without submitting a package {counts}"
        rule = reason.get("rule")
        if (code == "branch_author_package_refused" and reason.keys() == {"code", "turns", "submit_calls", "rule"}
                and isinstance(rule, str) and re.fullmatch(r"V[1-9][0-9]?", rule)):
            return f"The Branch author's last package submission was refused by rule {rule} {counts}"
        return None

    def _harness_stage(self, tenant_id: str, description: str, change: ChangeSet) -> Mapping[str, Any]:
        active_subject = deps.active_stage_author_subject(
            tenant_id,
            change.authority_session_id,
            change.authority_turn_id,
        )
        if (
            tenant_id != change.tenant_id
            or not active_subject
            or active_subject != change.author_subject
        ):
            raise CustomizationServiceError("stage_authority_invalid", 409)
        url, secret = _harness_config()
        if not url:
            raise CustomizationServiceError("customization_harness_unavailable", 503)
        revision_base = None
        if getattr(change, "tool_target", None) == "branch" and change.target_tool_name:
            revision_base = self._branch_revision_base(change.tenant_id, change.base_commit, change.target_tool_name)
        try:
            import requests
            response = requests.post(
                f"{url}/author/stage", timeout=_harness_stage_timeout_s(),
                headers={
                    "X-Harness-Secret": secret,
                    **({
                        "X-Authority-Session-Id": change.authority_session_id,
                        "X-Authority-Turn-Id": change.authority_turn_id,
                    } if change.authority_session_id and change.authority_turn_id else {}),
                },
                json={"tenant_id": tenant_id, "description": description, "changeSetId": change.change_set_id,
                      "expectedBaseSha": change.base_commit, "platformRelease": change.desired_platform_release,
                      "workspaceContractDigest": change.workspace_contract_digest,
                      "idempotencyKey": change.idempotency_key,
                      **({"toolName": json.loads(change.plan_json)["tool_name"]}
                         if getattr(change, "plan_json", None) else {}),
                      **({"target": "branch"} if getattr(change, "tool_target", None) == "branch" else {}),
                      **({"revisionBaseCommit": revision_base[1]} if revision_base else {}),
                      **({"targetToolName": getattr(change, "target_tool_name", None)}
                         if getattr(change, "target_tool_name", None) else {})},
            )
            response.raise_for_status()
            body = response.json()
        except Exception as exc:
            response = getattr(exc, "response", None)
            # Class-qualified: _harness_stage is also exercised unbound
            # (self=None), a convention test_customization_refusal_observability
            # pins for studio requests.
            answered, reason = (
                CustomizationService._harness_job_failure(response)
                if response is not None else (False, None)
            )
            if answered:
                # The harness answered with its own failure reason: the
                # authoring JOB failed terminally. Filing it as "unavailable"
                # would defer and re-run authoring for minutes while the
                # workspace shows nothing, so this raises a distinct code the
                # worker fails immediately, carrying the harness's reason for
                # stage status to surface.
                error = CustomizationServiceError(
                    "customization_author_job_failed", 502,
                    diagnostic_code="harness_stage_job_failed", cause=exc,
                )
                error.harness_reason = reason
                if getattr(change, "tool_target", None) == "branch" and response.status_code == 422:
                    refusal = response.json().get("error")
                    if isinstance(refusal, dict):
                        self._record_compile_attempts(change, refusal.get("compile"), staged_commit=None)
                raise error from exc
            raise CustomizationServiceError(
                "customization_harness_unavailable", 503,
                diagnostic_code="harness_stage_failed", cause=exc,
            ) from exc
        if not isinstance(body, Mapping):
            raise CustomizationServiceError(
                "invalid_staged_receipt", 502,
                diagnostic_code="staged_receipt_not_a_mapping",
            )
        if getattr(change, "tool_target", None) == "branch" and "compile_attempts" in body:
            receipt = body.get("receipt")
            if isinstance(receipt, Mapping):
                staged_commit = self._validate_receipt(receipt, change)["staged_commit"]
                self._record_compile_attempts(change, body, staged_commit=staged_commit)
        return body

    def _record_compile_attempts(
        self, change: ChangeSet, body: Any, *, staged_commit: str | None,
    ) -> None:
        summary = self._compile_summary_json(body)
        if summary is not None:
            self.store.record_compile_summary(change, summary, staged_commit=staged_commit)

    @staticmethod
    def _compile_summary_json(body: Any) -> str | None:
        if not isinstance(body, Mapping) or "compile_attempts" not in body:
            return None
        attempts = body["compile_attempts"]
        if not _valid_compile_attempts(attempts):
            _LOG.warning("invalid_compile_attempts")
            return None
        return json.dumps(attempts)

    def _validate_receipt(self, body: Mapping[str, Any], change: ChangeSet) -> dict[str, Any]:
        fields = {"contract", "tenant_id", "change_set_id", "state", "base_commit", "staged_commit", "catalog_digest", "platform_release", "workspace_contract_digest", "idempotency_key"}
        if change.tool_target == "branch":
            fields |= {"target", "package_path", "package_files"}
            package_path, package_files = body.get("package_path"), body.get("package_files")
            if (body.get("target") != "branch" or not isinstance(package_path, str)
                    or not re.fullmatch(r"branch-tools/[a-z][a-z0-9]*(?:-[a-z0-9]+)*", package_path)
                    or not isinstance(package_files, list) or not 1 <= len(package_files) <= 64
                    or any(not isinstance(path, str) or not path.startswith(package_path + "/")
                           for path in package_files)
                    or len(set(package_files)) != len(package_files)):
                raise CustomizationServiceError("invalid_staged_receipt", 502)
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

    def _verify_bound_stage_policy(
        self, change: ChangeSet, body: Mapping[str, Any] | None = None
    ) -> tuple[str, ...]:
        """Validate, and hand back the changed paths the validation already read.

        Returning them costs nothing (the ``diff-tree`` has already run) and
        saves ``_receipt`` a second subprocess per staged change just to stamp
        the change class.
        """
        if change.tool_target == "branch":
            receipt = body.get("receipt") if body is not None else self._raw_receipt(change)
            return self._verify_branch_stage_policy(change, self._validate_receipt(receipt, change))
        removal = self.store.get_removal_request(
            tenant_id=change.tenant_id, change_set_id=change.change_set_id
        )
        bindings = self._inherited_record_fields(change, {change.change_set_id})
        if not bindings and change.request_graph_input is None:
            if removal is None:
                return self._verify_stage_policy(change, body)
            return self._verify_stage_policy(
                change, body, removal_target=removal["target_tool_name"]
            )
        if removal is None:
            return self._verify_stage_policy(change, body, graph_bindings=bindings)
        return self._verify_stage_policy(
            change, body, removal_target=removal["target_tool_name"],
            graph_bindings=bindings,
        )

    @staticmethod
    def _branch_receipt_fields(change: ChangeSet) -> dict[str, Any]:
        """Reconstruct branch receipt fields from the immutable staged Git tree."""
        bare = _bare_repo(change.tenant_id)
        changed = [path for path in _git(
            bare, "diff-tree", "--no-commit-id", "--name-only", "--no-renames", "-r", "-z",
            change.base_commit, change.staged_commit or "",
        ).split("\0") if path]
        roots = {"/".join(path.split("/")[:2]) for path in changed}
        if len(roots) != 1:
            raise CustomizationServiceError("invalid_staged_paths", 422)
        package_path = next(iter(roots))
        if (not re.fullmatch(r"branch-tools/[a-z][a-z0-9]*(?:-[a-z0-9]+)*", package_path)
                or any(not path.startswith(package_path + "/") for path in changed)):
            raise CustomizationServiceError("invalid_staged_paths", 422)
        files = [path for path in _git(
            bare, "ls-tree", "-r", "--name-only", "-z", change.staged_commit or "",
            "--", package_path + "/",
        ).split("\0") if path]
        return {"target": "branch", "package_path": package_path, "package_files": sorted(files)}

    def _branch_revision_base(self, tenant_id: str, base_commit: str, name: str) -> tuple[str, str] | None:
        bare = _bare_repo(tenant_id)
        package = f"branch-tools/{name}"
        path = f"{package}/tool.json"
        missing = CustomizationServiceError("revision_target_missing", 422)
        try:
            entry = _git(bare, "ls-tree", base_commit, "--", path)
        except CustomizationServiceError as exc:
            raise missing from exc
        if re.fullmatch(r"[0-7]+ blob [0-9a-f]+\t" + re.escape(path), entry):
            return None
        try:
            published = self.store.list_published_branch_change_sets(tenant_id=tenant_id, tool_name=name)
        except (sqlite3.DatabaseError, PostgresError, OSError, RuntimeError) as exc:
            raise CustomizationServiceError("record_fields_read_failed", 503) from exc
        except ValueError as exc:
            raise missing from exc
        for row in published:
            if row.tenant_id != tenant_id:
                continue
            try:
                fields = self._branch_receipt_fields(row)
            except CustomizationServiceError:
                continue
            if fields["package_path"] != package:
                continue
            try:
                manifest = json.loads(_git_blob(bare, f"{row.staged_commit}:{path}"))
            except (CustomizationServiceError, ValueError, RecursionError) as exc:
                raise missing from exc
            if (row.staged_commit and isinstance(manifest, dict) and manifest.get("name") == name
                    and manifest.get("entry") == "main.csx" and isinstance(manifest.get("provenance"), dict)
                    and manifest["provenance"].get("author") == "agent" and isinstance(manifest.get("version"), str)
                    and _BRANCH_VERSION.fullmatch(manifest["version"])):
                return row.change_set_id, row.staged_commit
            raise missing
        raise missing

    def _published_branch_seed(self, change: ChangeSet, bare: Path, package_path: str) -> str:
        missing = CustomizationServiceError("branch_revise_target_missing", 404)
        try:
            parents = _git(bare, "rev-list", "--parents", "-n", "1", change.staged_commit or "").split()
            if len(parents) != 2:
                raise ValueError("expected one seed parent")
            seed = parents[1]
            if _git(bare, "rev-list", "--parents", "-n", "1", seed).split() != [seed, change.base_commit]:
                raise ValueError("seed must descend directly from the base")
            # Whitespace is significant in NUL-delimited Git paths.
            paths = [path for path in _git(
                bare, "diff-tree", "--no-commit-id", "--name-only", "--no-renames", "-r", "-z",
                change.base_commit, seed, strip=False,
            ).split("\0") if path]
            if not paths or any(not p.startswith(package_path + "/") for p in paths):
                raise ValueError("seed must introduce only the revision package")
            tree = _git(bare, "rev-parse", f"{seed}:{package_path}")
            published = self.store.list_published_branch_change_sets(
                tenant_id=change.tenant_id, tool_name=change.target_tool_name,
            )
        except (sqlite3.DatabaseError, PostgresError, OSError, RuntimeError, ValueError) as exc:
            raise missing from exc
        for row in published:
            if row.tenant_id != change.tenant_id or row.change_set_id == change.change_set_id or not row.staged_commit:
                continue
            try:
                if tree == _git(bare, "rev-parse", f"{row.staged_commit}:{package_path}"):
                    return seed
            except CustomizationServiceError:
                continue
        raise missing

    def _verify_branch_stage_policy(self, change: ChangeSet, receipt: Mapping[str, Any]) -> tuple[str, ...]:
        bare = _bare_repo(change.tenant_id)
        package_path = receipt["package_path"]
        changed = tuple(path for path in _git(
            bare, "diff-tree", "--no-commit-id", "--name-only", "--no-renames", "-r", "-z",
            change.base_commit, change.staged_commit or "",
        ).split("\0") if path)
        if not changed or any(not path.startswith(package_path + "/") for path in changed):
            raise CustomizationServiceError("invalid_staged_paths", 422)
        entries = {}
        for entry in _git(
            bare, "ls-tree", "-r", "-z", change.staged_commit or "", "--", package_path + "/",
        ).split("\0"):
            if not entry:
                continue
            metadata, _, path = entry.partition("\t")
            mode, kind, oid = metadata.split(" ")
            if mode not in {"100644", "100755"} or kind != "blob":
                raise CustomizationServiceError("invalid_staged_paths", 422)
            entries[path] = oid
        if sorted(entries) != receipt["package_files"]:
            raise CustomizationServiceError("invalid_staged_paths", 422)
        base_entries = {}
        for entry in _git(
            bare, "ls-tree", "-r", "-z", change.base_commit, "--", package_path,
        ).split("\0"):
            if entry:
                metadata, _, path = entry.partition("\t")
                base_entries[path] = metadata.split(" ")[0]
        if any(path not in entries and base_entries.get(path) not in {"100644", "100755"}
               for path in changed):
            raise CustomizationServiceError("invalid_staged_paths", 422)
        if hashlib.sha256(_git_blob(bare, f"{change.base_commit}:registry.json")).hexdigest() != receipt["catalog_digest"]:
            raise CustomizationServiceError("catalog_digest_mismatch", 409)
        name = package_path.split("/")[1]
        if getattr(change, "plan_json", None) and name != json.loads(change.plan_json)["tool_name"]:
            raise CustomizationServiceError("plan_binding_mismatch", 422)
        if change.target_tool_name and name != change.target_tool_name:
            raise CustomizationServiceError("branch_target_mismatch", 422)
        manifest_path = package_path + "/tool.json"
        revision_base = change.base_commit
        if change.target_tool_name:
            if manifest_path not in base_entries:
                revision_base = self._published_branch_seed(change, bare, package_path)
        elif base_entries:
            raise CustomizationServiceError("branch_tool_exists", 409)
        files = {path[len(package_path) + 1:]: _git_blob(bare, oid) for path, oid in entries.items()}
        checked = check_branch_package(files, name)
        if (change.target_tool_name and checked.manifest is not None
                and isinstance(checked.manifest.get("name"), str)
                and checked.manifest["name"] != change.target_tool_name):
            raise CustomizationServiceError("branch_target_mismatch", 422)
        if not checked.ok:
            raise CustomizationServiceError("branch_package_invalid", 422, failures=checked.failures)
        if change.target_tool_name:
            try:
                try:
                    base_manifest = json.loads(_git_blob(bare, f"{revision_base}:{manifest_path}"))
                except CustomizationServiceError as exc:
                    if revision_base == change.base_commit:
                        raise
                    raise CustomizationServiceError("branch_revise_target_missing", 404) from exc
                if revision_base != change.base_commit and (
                    not isinstance(base_manifest, dict) or base_manifest.get("entry") != "main.csx"
                    or not isinstance(base_manifest.get("provenance"), dict)
                    or base_manifest["provenance"].get("author") != "agent"
                ):
                    raise CustomizationServiceError("branch_revise_target_missing", 404)
                base_version = _BRANCH_VERSION.fullmatch(base_manifest["version"])
                staged_version = _BRANCH_VERSION.fullmatch(checked.manifest["version"])
                if (not base_version or not staged_version
                        or tuple(map(int, staged_version.groups())) <= tuple(map(int, base_version.groups()))):
                    raise ValueError("version must increase")
            except (ValueError, KeyError, TypeError, UnicodeDecodeError) as exc:
                raise CustomizationServiceError("branch_version_not_greater", 409) from exc
        return changed

    @staticmethod
    def _verify_raw_graph_declarations(
        change: ChangeSet, base: Mapping[str, Any], rows: Mapping[str, Any],
        bindings: Mapping[str, Any],
    ) -> None:
        for name, row in rows.items():
            old = base.get(name, {})
            if "graph_input" in row and (
                "graph_input" not in old or row["graph_input"] != old["graph_input"]
            ):
                inherited = bindings.get(name, {}).get("graph_input")
                is_target = (name not in base if change.change_kind == "create"
                             else name == change.target_tool_name)
                requested = change.request_graph_input if is_target else None
                authorized = requested if requested is not None else inherited
                if (authorized is None or type(row["graph_input"]) is not str
                        or row["graph_input"] != authorized):
                    raise CustomizationServiceError("graph_input_unreserved", 422)

    @staticmethod
    def _verify_stage_policy(
        change: ChangeSet, body: Mapping[str, Any] | None = None,
        *, removal_target: str | None = None,
        graph_bindings: Mapping[str, Any] | None = None,
    ) -> tuple[str, ...]:
        """Validate every changed path and the trusted derived registry update.

        Returns the validated changed paths. Validation semantics are unchanged:
        every refusal below still raises exactly as before.
        """
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
        CustomizationService._verify_raw_graph_declarations(
            change, base_by_name, staged_by_name, graph_bindings or {}
        )
        added = [item for name, item in staged_by_name.items() if name not in base_by_name]
        removed = [name for name in base_by_name if name not in staged_by_name]
        modified = [
            name for name, item in base_by_name.items()
            if name in staged_by_name and staged_by_name[name] != item
        ]
        if removal_target is not None:
            if change.target_tool_name != removal_target:
                raise CustomizationServiceError("invalid_staged_catalog_delta", 422)
            if set(changed) != {"registry.json"}:
                raise CustomizationServiceError("invalid_staged_paths", 422)
            if (
                added or modified or removed != [removal_target]
                or [item for item in base_tools if item.get("name") != removal_target]
                != tools
            ):
                raise CustomizationServiceError("invalid_staged_catalog_delta", 422)
            if sum(item.get("name") == removal_target for item in base_tools) != 1:
                raise CustomizationServiceError("invalid_staged_catalog_delta", 422)
            if body is not None and set(body) - {"receipt"}:
                raise CustomizationServiceError("invalid_staged_tool", 422)
            return tuple(changed)
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
            for key in ("name", "entry", "kind", "engine_op", "capabilities"):
                if staged_tool.get(key) != base_tool.get(key):
                    raise CustomizationServiceError("invalid_staged_catalog_revision", 422)
            schema_changes = [
                _revision_schema_change(base_tool.get(key), staged_tool.get(key))
                for key in ("params", "returns")
            ]
            if "breaking" in schema_changes:
                raise CustomizationServiceError("invalid_staged_catalog_revision", 422)
            base_version = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", str(base_tool.get("version", "")))
            staged_version = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", str(staged_tool.get("version", "")))
            if not base_version or not staged_version:
                raise CustomizationServiceError("invalid_staged_catalog_revision", 422)
            if "additive" in schema_changes:
                if (staged_version.group(1) != base_version.group(1)
                        or staged_version.group(2) != str(int(base_version.group(2)) + 1)
                        or staged_version.group(3) != "0"):
                    raise CustomizationServiceError("invalid_staged_catalog_revision", 422)
            elif (staged_version.groups()[:2] != base_version.groups()[:2]
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
        if getattr(change, "plan_json", None) and staged_tool.get("name") != json.loads(change.plan_json)["tool_name"]:
            raise CustomizationServiceError("plan_binding_mismatch", 422)
        entry = staged_tool.get("entry")
        if (not isinstance(entry, str) or not entry.startswith("tools/")
                or entry not in changed):
            raise CustomizationServiceError("invalid_staged_tool_entry", 422)
        if body is not None:
            tool = body.get("tool")
            if not isinstance(tool, Mapping) or dict(tool) != staged_tool:
                raise CustomizationServiceError("invalid_staged_tool", 422)
        return tuple(changed)

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
        if change.tool_target == "branch":
            result["target"] = "branch"
            if change.staged_commit:
                manifest = CustomizationService._staged_tool(change)
                result.update(name=manifest["name"], version=manifest["version"])
            elif change.target_tool_name:
                result["name"] = change.target_tool_name
        elif status == "published":
            result["catalog_digest"] = change.catalog_digest
        return result

    def _review_target(
        self, viewer: TenantBinding, change_set_id: str, *, allow_author_view: bool = False,
    ) -> tuple[ChangeSet, dict | None, dict | None]:
        reviewer = viewer.role in {"owner", "reviewer"}
        if not reviewer and not allow_author_view:
            raise CustomizationServiceError("approver_role_denied", 403)
        key = {"tenant_id": viewer.tenant_id, "change_set_id": change_set_id}
        try:
            change = self.store.get_change_set(**key)
        except ChangeSetNotFoundError as exc:
            if not reviewer:
                raise CustomizationServiceError("approver_role_denied", 403) from exc
            raise CustomizationServiceError("review_target_stale", 409) from exc
        if not reviewer and viewer.subject != change.author_subject:
            raise CustomizationServiceError("approver_role_denied", 403)
        review = None
        if change.staged_commit:
            review = self.store.get_review_decision(**key, staged_commit=change.staged_commit)
        return change, review, self.store.get_publication_request(**key)

    def _review_eligibility(
        self, viewer: TenantBinding, change: ChangeSet, review: dict | None, request: dict | None, *,
        gate_open: bool, staged_commit: str | None, catalog_digest: str | None, decision: str | None = None,
    ) -> str:
        """Return the viewer's approval kind, or the reason this review is unavailable."""
        if not gate_open or not customization_flags.inline_review_enabled(viewer.tenant_id):
            raise CustomizationServiceError("inline_review_disabled", 404)
        if viewer.role not in {"owner", "reviewer"}:
            raise CustomizationServiceError("approver_role_denied", 403)
        if not staged_commit or not catalog_digest or (staged_commit, catalog_digest) != (
            change.staged_commit, change.catalog_digest
        ):
            raise CustomizationServiceError("review_target_stale", 409)
        if review is not None:
            raise CustomizationServiceError("review_already_decided", 409)
        if change.state is not ChangeState.STAGED and not (
            decision == "approve" and change.state is ChangeState.PUBLISHED
        ):
            raise CustomizationServiceError("review_target_stale", 409)
        if request and request["status"] == "denied" and not (
            decision == "request_changes" and request["reason_code"] == "reviewer_requested_changes"
        ):
            raise CustomizationServiceError("review_target_stale", 409)
        is_self = viewer.subject == change.author_subject
        if is_self and not customization_flags.self_approval_allowed(viewer.tenant_id):
            raise CustomizationServiceError("self_approval_not_allowed", 403)
        if change.state is ChangeState.STAGED and decision != "request_changes":
            if not self._publication_policy_state(viewer.tenant_id)[0]:
                raise CustomizationServiceError("tool_publication_disabled", 409)
        return "self" if is_self else "independent"

    def review_context(self, *, tenant: Any, change_set_id: str, gate_open: bool) -> dict[str, Any]:
        viewer = _binding(tenant)
        change, review, request = self._review_target(viewer, change_set_id, allow_author_view=True)
        try:
            kind = self._review_eligibility(
                viewer, change, review, request, gate_open=gate_open,
                staged_commit=change.staged_commit, catalog_digest=change.catalog_digest,
            )
        except CustomizationServiceError:
            kind = None

        # Refresh can finish bookkeeping for a committed publication. It has
        # no authority to start or resume the external publish.
        if gate_open and change.state is ChangeState.PUBLISHED and review is None:
            fields = self._published_review_fields(change, request)
            if fields is not None:
                try:
                    review = self._record_review(change, "approve", fields)
                except IdempotencyReplayError:
                    review = self.store.get_review_decision(
                        tenant_id=viewer.tenant_id, change_set_id=change_set_id, staged_commit=change.staged_commit,
                    )
        return {
            "viewer_may_approve": kind is not None,
            "approval_kind_if_approved": kind,
            "review": None if review is None else {
                key: review[key] for key in ("decision", "approval_kind", "approver_role", "decided_at")
            },
            "flags": {
                "plan_mode": customization_flags.plan_mode_enabled(viewer.tenant_id),
                "inline_review": customization_flags.inline_review_enabled(viewer.tenant_id),
                "self_approval": customization_flags.self_approval_allowed(viewer.tenant_id),
            },
        }

    def _published_review_fields(self, change: ChangeSet, request: dict | None) -> dict | None:
        if not request or not request["confirmation_id"]:
            return None
        confirmation_id = request["confirmation_id"]
        record = self.store.get_confirmation(confirmation_id=confirmation_id)
        if not record or not record["consumed"] or "approval_kind" not in record["payload"]:
            return None
        confirmation, _ = self._authority().verify_publish_confirmation(
            tenant_id=change.tenant_id, confirmation_id=confirmation_id, allow_consumed=True,
            request=PublishRequest(change.change_set_id, change.staged_commit, change.catalog_digest,
                                   change.desired_platform_release, change.workspace_contract_digest),
        )
        if (confirmation.approver_subject != change.approver_subject
                or confirmation.author_subject != change.author_subject
                or confirmation.approver_role not in {"owner", "reviewer"}):
            raise CustomizationServiceError("review_target_stale", 409)
        return {
            "confirmation_id": confirmation_id, "approval_kind": confirmation.approval_kind,
            "approver_subject": confirmation.approver_subject, "approver_role": confirmation.approver_role,
            "comment": json.loads(request["review_details_json"] or "{}").get("comment"),
        }

    def _record_review(self, change: ChangeSet, decision: str, fields: dict) -> dict:
        decided_at = self.store.record_review_decision(
            tenant_id=change.tenant_id, change_set_id=change.change_set_id, staged_commit=change.staged_commit,
            catalog_digest=change.catalog_digest, decision=decision, **fields,
        )
        return {"decision": decision, "approval_kind": fields["approval_kind"],
                "approver_role": fields["approver_role"], "decided_at": decided_at}

    def decide_review(self, *, tenant: Any, change_set_id: str, staged_commit: str, catalog_digest: str,
                      decision: str, comment: str | None) -> dict[str, Any]:
        reviewer = _binding(tenant)
        tenant_id = reviewer.tenant_id
        key = {"tenant_id": tenant_id, "change_set_id": change_set_id}
        change, review, request = self._review_target(reviewer, change_set_id)
        kind = self._review_eligibility(reviewer, change, review, request, gate_open=True,
                                        staged_commit=staged_commit, catalog_digest=catalog_digest, decision=decision)
        fields = {"approver_subject": reviewer.subject, "approver_role": reviewer.role,
                  "approval_kind": kind, "comment": comment, "confirmation_id": None}
        if change.state is ChangeState.PUBLISHED:
            fields = self._published_review_fields(change, request)
            if fields is None:
                raise CustomizationServiceError("review_target_stale", 409)
        elif decision == "approve":
            confirmation = self._authority().issue_publish_confirmation(
                staged_change=StagedChange(
                    tenant_id, change_set_id, staged_commit, catalog_digest, change.desired_platform_release,
                    change.workspace_contract_digest, change.author_subject, True,
                ),
                author_binding=TenantBinding(tenant_id, change.author_subject, "owner", True),
                approver_binding=reviewer, allow_self_approval=kind == "self", approval_kind=kind,
            )
            fields["confirmation_id"] = confirmation.confirmation_id
            self.store.get_or_create_publication_request(**key)
            request = self.store.bind_publication_confirmation(
                **key, confirmation_id=confirmation.confirmation_id, review_comment=comment,
            )
            if request["status"] == "denied":
                raise CustomizationServiceError("review_target_stale", 409)
            author = deps.TenantContext(tenant_id, subject=change.author_subject)
            try:
                self._continue_publication(author, change, confirmation.confirmation_id)
            except AuthorityError as exc:
                if exc.reason_code == "confirmation_replayed":
                    raise CustomizationServiceError("review_target_stale", 409) from exc
                raise
            except CustomizationServiceError as exc:
                if exc.code == "publish_recovery_authority_invalid":
                    raise CustomizationServiceError("review_target_stale", 409) from exc
                raise
        elif decision == "request_changes":
            self.store.get_or_create_publication_request(**key)
            request = self.store.deny_publication_request(**key, reason_code="reviewer_requested_changes",
                                                          review_details=fields)
            if request["reason_code"] != "reviewer_requested_changes":
                raise CustomizationServiceError("review_target_stale", 409)
            if request["review_details_json"]:
                fields = json.loads(request["review_details_json"])
        else:
            raise CustomizationServiceError("invalid_review_request", 422)

        status = "published" if decision == "approve" else "denied"
        publication = self._publication_status(change, status)
        try:
            recorded = self._record_review(change, decision, fields)
        except IdempotencyReplayError as exc:
            # A card refresh can complete the audit after this POST published.
            # Acknowledge only the receipt issued by this call in that race.
            existing = self.store.get_review_decision(**key, staged_commit=staged_commit)
            if (decision != "approve" or change.state is not ChangeState.STAGED or not existing
                    or existing["confirmation_id"] != fields["confirmation_id"]):
                raise CustomizationServiceError("review_already_decided", 409) from exc
            recorded = {name: existing[name] for name in ("decision", "approval_kind", "approver_role", "decided_at")}
        return {
            **{key: publication[key] for key in ("contract", "change_set_id", "status", "name", "version")
               if key in publication},
            **recorded,
        }

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
            if change.tool_target == "branch":
                self._published_branch_result(change)
                return self._publication_status(change, "published")
            try:
                effective = self.store.get_effective_catalog(tenant_id=tenant_id)
            except ChangeSetNotFoundError as exc:
                raise CustomizationServiceError("publish_not_available") from exc
            if effective.change_set_id != change.change_set_id:
                raise CustomizationServiceError("publish_not_available")
            return self._publication_status(change, "published")

        if change.state is ChangeState.SUPERSEDED:
            raise CustomizationServiceError("publish_superseded", 409)
        if change.state not in {ChangeState.STAGED, ChangeState.PUBLISHING}:
            raise CustomizationServiceError("publication_request_not_available")
        if change.tool_target == "branch" and not customization_flags.plugin_builder_enabled(tenant_id):
            self._branch_dev_root(tenant_id)

        # Denial is terminal for the durable request regardless of later
        # account-policy changes. It is resolved before policy or receipt
        # lookup so no subsequent authority can revive the denied work.
        if (change.state is ChangeState.STAGED
                and continuation.get("status") == "denied"):
            return self._publication_status(change, "denied")

        publication_enabled, approval_required = self._publication_policy_state(
            tenant_id
        )
        if change.tool_target == "branch":
            # Branch entries always need a human review.
            approval_required = True
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
                if continuation["status"] == "denied":
                    return self._publication_status(change, "denied")
                confirmation_id = continuation["confirmation_id"]
        elif confirmation_id is None:
            raise CustomizationServiceError(
                "publish_recovery_authority_invalid", 403
            )
        else:
            bound = self.store.get_confirmation(confirmation_id=confirmation_id)
            if bound and bound["payload"].get("approval_kind") is not None:
                tenant = deps.TenantContext(tenant_id, subject=change.author_subject)

        return self._continue_publication(tenant, change, confirmation_id)

    def _continue_publication(self, tenant: Any, change: ChangeSet, confirmation_id: str) -> dict[str, Any]:
        tenant_id = change.tenant_id
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
        try:
            self.store.deny_publication_request(
                tenant_id=tenant_id,
                change_set_id=change.change_set_id,
                reason_code="independent_approver_denied",
            )
        except ChangeSetConflictError as exc:
            raise CustomizationServiceError("publication_denial_not_available") from exc
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
        change = self._record_fields_change(tenant_id, request.change_set_id)
        if (change.staged_commit, change.catalog_digest, change.desired_platform_release, change.workspace_contract_digest) != (
            request.staged_commit, request.catalog_digest, request.platform_release, request.workspace_contract_digest):
            raise CustomizationServiceError("staged_receipt_mismatch")
        if change.state not in {ChangeState.STAGED, ChangeState.PUBLISHING, ChangeState.PUBLISHED}:
            raise CustomizationServiceError("publish_not_available")
        if change.tool_target == "branch" and not customization_flags.plugin_builder_enabled(tenant_id):
            self._branch_dev_root(tenant_id)
        self._verify_catalog(tenant_id, request.staged_commit, request.catalog_digest)
        self._verified_record_fields(change, set())
        if change.state is ChangeState.PUBLISHED:
            if change.tool_target == "branch":
                return self._published_branch_result(change)
            effective = self._record_fields_pin(tenant_id)
            if self._pin_matches_change(effective, change):
                return {"contract": CONTRACT, "tenant_id": effective.tenant_id,
                        "change_set_id": effective.change_set_id, "catalog_commit": effective.catalog_commit,
                        "catalog_digest": effective.catalog_digest,
                        "platform_release": effective.effective_platform_release,
                        "workspace_contract_digest": effective.workspace_contract_digest}
            raise CustomizationServiceError("publish_not_available")
        if change.tool_target != "branch":
            self._verify_record_fields_predecessor(change)
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
            # Before prepare_publish: a stale addition must leave the change
            # STAGED and its confirmation unconsumed.
            if change.tool_target != "branch":
                self._refuse_stale_addition_base(change)
        if (change.tool_target == "branch"
                and confirmation.approver_subject == "leaf:server:auto-publication-policy"):
            raise CustomizationServiceError("independent_approval_pending", 409)
        if confirmation.approval_kind is not None and require_actor_binding:
            raise CustomizationServiceError("inline_review_required", 403)
        publishing = self.store.prepare_publish(
            tenant_id=tenant_id,
            change_set_id=change.change_set_id,
            confirmation_id=confirmation_id,
            confirmation_signature=signature,
            approver_subject=confirmation.approver_subject,
            idempotency_key=idempotency_key,
            require_bound_confirmation=not require_actor_binding,
        )
        if change.tool_target == "branch":
            try:
                files, name = self._branch_package_files(change)
                review = None
                if confirmation.approval_kind and confirmation.approver_role:
                    review = {"approval": confirmation.approval_kind, "approver_role": confirmation.approver_role}
                if customization_flags.plugin_builder_enabled(tenant_id):
                    try:
                        result = tenant_publish.publish_tenant_tool(
                            files, name, tenant_id, online_signer.configured_signer(),
                            os.environ["LEAF_TENANT_TOOLS_DIR"], datetime.now(timezone.utc))
                    except online_signer.SignerUnavailable as exc:
                        raise tenant_publish.TenantPublishError("signer_unavailable") from exc
                    except tenant_catalog.TenantCatalogError as exc:
                        raise tenant_publish.TenantPublishError("tenant_catalog_unavailable") from exc
                else:
                    result = publish_branch_package(files, name, self._branch_dev_root(tenant_id), review=review)
            except (DevPublishError, tenant_publish.TenantPublishError) as exc:
                if confirmation.approval_kind is None or exc.code != "branch_dev_catalog_unavailable":
                    self.store.supersede_publish(
                        tenant_id=tenant_id, change_set_id=change.change_set_id,
                        expected_version=publishing.version,
                        idempotency_key=idempotency_key, reason_code="branch_" + exc.code,
                    )
                    _LOG.warning(
                        "customization_publish_superseded: change_set=%s code=%s",
                        change.change_set_id, exc.code,
                    )
                raise CustomizationServiceError(
                    exc.code, exc.status_code if isinstance(exc, tenant_publish.TenantPublishError) else (
                        409 if exc.code == "branch_version_conflict" else (
                        422 if exc.code == "branch_package_invalid" else 503
                    )), failures=exc.failures,
                ) from exc
            except OSError as exc:
                code = ("tenant_catalog_unavailable" if customization_flags.plugin_builder_enabled(tenant_id)
                        else "branch_dev_catalog_unavailable")
                raise CustomizationServiceError(code, 503) from exc
            try:
                self.store.publish_branch(
                    tenant_id=tenant_id, change_set_id=change.change_set_id,
                    expected_version=publishing.version, idempotency_key=idempotency_key,
                    approver_subject=confirmation.approver_subject,
                )
            except (sqlite3.DatabaseError, PostgresError, OSError, RuntimeError) as exc:
                raise CustomizationServiceError("record_fields_write_failed", 503) from exc
            return self._branch_publish_result(change, result)
        self.store.verify_removal_predecessor(
            tenant_id=tenant_id, change_set_id=change.change_set_id
        )
        try:
            published_commit = self._harness_publish(change)
        except CustomizationServiceError as exc:
            if exc.status_code == 409 and exc.code in _HARNESS_PUBLISH_LOST:
                # A lost publish never wins on retry, so leaving it PUBLISHING
                # would hold the tenant's only publish slot forever. End it here.
                self.store.supersede_publish(
                    tenant_id=tenant_id, change_set_id=change.change_set_id,
                    expected_version=publishing.version,
                    idempotency_key=idempotency_key,
                    reason_code=f"harness_{exc.code}",
                )
                _LOG.warning(
                    "customization_publish_superseded: change_set=%s code=%s",
                    change.change_set_id, exc.code,
                )
            raise
        if published_commit != request.staged_commit:
            raise CustomizationServiceError("published_commit_mismatch", 502)
        try:
            effective = self.store.publish(tenant_id=tenant_id, change_set_id=change.change_set_id,
                expected_version=publishing.version, idempotency_key=idempotency_key,
                approver_subject=confirmation.approver_subject)
        except (sqlite3.DatabaseError, PostgresError, OSError, RuntimeError) as exc:
            raise CustomizationServiceError("record_fields_write_failed", 503) from exc
        return {"contract": CONTRACT, "tenant_id": effective.tenant_id, "change_set_id": effective.change_set_id,
                "catalog_commit": effective.catalog_commit, "catalog_digest": effective.catalog_digest,
                "platform_release": effective.effective_platform_release,
                "workspace_contract_digest": effective.workspace_contract_digest}

    @staticmethod
    def _branch_dev_root(tenant_id: str) -> Path:
        if tenant_id not in customization_flags.internal_tenants():
            raise CustomizationServiceError("branch_publish_internal_only", 403)
        configured = os.environ.get("LEAF_BRANCH_DEV_TOOLS_DIR", "")
        if os.environ.get("LEAF_BRANCH_DEV_TOOLS") != "1" or not configured.strip():
            raise CustomizationServiceError("branch_dev_catalog_unavailable", 503)
        return Path(configured)

    @staticmethod
    def _branch_package_files(change: ChangeSet) -> tuple[dict[str, bytes], str]:
        receipt = CustomizationService._branch_receipt_fields(change)
        package = receipt["package_path"]
        bare = _bare_repo(change.tenant_id)
        files = {
            path[len(package) + 1:]: _git_blob(bare, f"{change.staged_commit}:{path}")
            for path in receipt["package_files"]
        }
        return files, package.split("/")[1]

    @staticmethod
    def _branch_publish_result(
        change: ChangeSet, result: DevPublishResult | tenant_publish.TenantPublishResult,
    ) -> dict[str, Any]:
        return {
            "contract": CONTRACT, "tenant_id": change.tenant_id,
            "change_set_id": change.change_set_id, "target": "branch",
            "name": result.name, "version": result.version,
            "package_digest": result.package_digest,
            **({"catalog": "/api/tool-envelopes/tenant"}
               if customization_flags.plugin_builder_enabled(change.tenant_id)
               else {"dev_catalog": "/api/tool-envelopes/dev"}),
        }

    def _published_branch_result(self, change: ChangeSet) -> dict[str, Any]:
        if customization_flags.plugin_builder_enabled(change.tenant_id):
            files, name = self._branch_package_files(change)
            try:
                _, checked = tenant_publish.reviewed_package(files, name)
                result = tenant_publish.read_publication(
                    os.environ["LEAF_TENANT_TOOLS_DIR"], change.tenant_id,
                    name, checked.manifest["version"])
                if result.package_digest != checked.package_digest:
                    raise tenant_publish.TenantPublishError("branch_version_conflict", 409)
            except (tenant_publish.TenantPublishError, OSError) as exc:
                raise CustomizationServiceError("publish_not_available", 409) from exc
            return self._branch_publish_result(change, result)
        root = self._branch_dev_root(change.tenant_id)
        files, name = self._branch_package_files(change)
        checked = check_branch_package(files, name)
        if not checked.ok:
            raise CustomizationServiceError("branch_package_invalid", 422, failures=checked.failures)
        try:
            result = read_branch_publication(name, checked.manifest["version"], root)
            if result.package_digest != checked.package_digest:
                raise DevPublishError("branch_version_conflict")
        except (DevPublishError, OSError) as exc:
            raise CustomizationServiceError("publish_not_available", 409) from exc
        return self._branch_publish_result(change, result)

    def _refuse_stale_addition_base(self, change: ChangeSet) -> None:
        """Refuse an addition staged on a catalog that is no longer effective.

        Reads only, so the change stays STAGED and its confirmation unconsumed.
        A removal binds and rechecks its own predecessor pin, and a tenant with
        no effective catalog has nothing to be stale against (the harness CAS on
        main still guards that first publish).
        """
        if change.change_kind != "create":
            return
        try:
            effective = self.store.get_effective_catalog(tenant_id=change.tenant_id)
        except ChangeSetNotFoundError:
            return
        if not hmac.compare_digest(change.base_commit, effective.catalog_commit):
            raise CustomizationServiceError(
                "stale_base", 409,
                diagnostic_code="stale_base",
            )

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
            response = getattr(exc, "response", None)
            status = getattr(response, "status_code", None)
            lost = _harness_publish_lost_code(response)
            if lost is not None:
                raise CustomizationServiceError(
                    lost, 409,
                    diagnostic_code="harness_publish_lost", cause=exc,
                ) from exc
            raise CustomizationServiceError(
                "customization_publish_incomplete", 503,
                diagnostic_code="harness_publish_failed", cause=exc,
            ) from exc
        commit = body.get("commit") if isinstance(body, Mapping) else None
        if not isinstance(commit, str):
            raise CustomizationServiceError(
                "customization_publish_incomplete", 502,
                diagnostic_code="publish_commit_not_a_string",
            )
        return commit

    def record_staged_callback(self, *, tenant_id: str, receipt: Mapping[str, Any]) -> dict[str, Any]:
        tenant_id = _tenant_id(tenant_id)
        if not deps.auth_live():
            raise CustomizationServiceError("customization_auth_required", 503)
        change_id = receipt.get("change_set_id")
        if not isinstance(change_id, str):
            raise CustomizationServiceError("invalid_staged_receipt", 422)
        try:
            change = self.store.get_change_set(tenant_id=tenant_id, change_set_id=change_id)
        except ToolRecordFieldError as exc:
            raise CustomizationServiceError("record_fields_invalid", 503) from exc
        except (sqlite3.DatabaseError, PostgresError, OSError, RuntimeError) as exc:
            raise CustomizationServiceError("record_fields_read_failed", 503) from exc
        summary = None
        if change.tool_target == "branch" and "compile_attempts" in receipt:
            summary = self._compile_summary_json(receipt)
            receipt = {key: value for key, value in receipt.items() if key != "compile_attempts"}
        validated = self._validate_receipt(receipt, change)
        change, _changed = self._complete_stage(change, validated, compile_summary_json=summary)
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
        change = self._record_fields_change(tenant_id, change_id)
        validated = self._validate_receipt(receipt, change)
        if change.state is not ChangeState.PUBLISHING:
            raise CustomizationServiceError("publish_not_authorized", 403)
        if expected_main_sha != change.base_commit or self._raw_receipt(change) != dict(validated):
            raise CustomizationServiceError("staged_receipt_mismatch")
        self._verify_catalog(tenant_id, change.staged_commit or "", change.catalog_digest or "")
        self._verified_record_fields(change, set())
        self._verify_record_fields_predecessor(change)
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
        current = self._record_fields_pin(tenant_id)
        if current is None:
            raise ChangeSetNotFoundError("no effective catalog")
        target = self._record_fields_change(tenant_id, change_set_id)
        if target.state is not ChangeState.PUBLISHED:
            raise CustomizationServiceError("rollback_target_invalid")
        self._verify_catalog(tenant_id, target.staged_commit or "", target.catalog_digest or "")
        self._verified_record_fields(target, set())
        try:
            result = self.store.restore_effective_catalog(tenant_id=tenant_id, target_change_set_id=target.change_set_id,
                prior_change_set_id=current.change_set_id, idempotency_key=idempotency_key)
        except (sqlite3.DatabaseError, PostgresError, OSError, RuntimeError) as exc:
            raise CustomizationServiceError("record_fields_write_failed", 503) from exc
        return {"contract": CONTRACT, "tenant_id": result.tenant_id, "change_set_id": result.change_set_id,
                "catalog_commit": result.catalog_commit, "catalog_digest": result.catalog_digest,
                "platform_release": result.effective_platform_release}

    def _receipt(
        self, change: ChangeSet, *, changed: tuple[str, ...] | None = None, **extra: Any
    ) -> dict[str, Any]:
        """The durable stage receipt, plus the change class when the paths are known.

        ADDITIVE and DECORATIVE ONLY. ``change_class`` is honest data for the
        change capsule (slice 9b) and the build card (slice 11): it says which
        delivery ladder a change of this shape rides, and it is NEVER a promise
        that the change lands, or lands in that time. It gates nothing, and the
        stage/confirm/publish state machine cannot read it -- adding it changes
        no transition, no refusal, and no persisted row. When the caller has not
        already validated the paths the key is simply absent; nothing recomputes
        a diff to manufacture it.
        """
        if change.tool_target != "branch" and (change.request_graph_input is not None
                or change.base_catalog_change_set_id is not None
                or change.catalog_record_fields_json is not None):
            self._verified_record_fields(change, set())
            try:
                removal = self.store.get_removal_request(
                    tenant_id=change.tenant_id, change_set_id=change.change_set_id
                )
            except (sqlite3.DatabaseError, PostgresError, OSError, RuntimeError) as exc:
                raise CustomizationServiceError("record_fields_read_failed", 503) from exc
            if removal is None:
                tool = extra.get("tool")
                if tool is None:
                    tool = self._staged_tool(change)
                extra["tool"] = self._decorate_stage_tool(change, tool)
        body = {"receipt": self._raw_receipt(change),
                **{k: v for k, v in extra.items() if v is not None}}
        if change.tool_target == "branch":
            self._verified_record_fields(change, set())
            body.update(target="branch", package_path=body["receipt"]["package_path"])
        klass = self._change_class(change, changed)
        if klass is not None:
            body["change_class"] = klass
        return body

    def _decorate_stage_tool(self, change: ChangeSet, tool: Mapping[str, Any]) -> dict[str, Any]:
        snapshot = self._verified_record_fields(change, set())
        decorated = dict(tool)
        if snapshot is not None:
            binding = parse_catalog_record_fields_json(snapshot)["tools"].get(tool.get("name"))
            if binding is not None:
                decorated["graph_input"] = binding["graph_input"]
        return decorated

    @staticmethod
    def _change_class(
        change: ChangeSet, changed: tuple[str, ...] | None
    ) -> dict[str, str] | None:
        """Classify the already-validated paths. Never raises, never gates.

        COST, measured on this host 2026-09-03: 0.63 ms per staged receipt
        (0.48 ms of it the deliberate policy re-read, 0.04 ms the classification
        of a three-path change set; 7.4 ms at the 500-path ceiling). That sits
        beside the several `git diff-tree` / `git ls-tree` subprocesses the
        validation above already spends on the same request, so it is noise on
        this path. It is NOT on any poll path: `stage_status_change` does not
        call it, because stamping a class there would buy a `diff-tree`
        subprocess per one-second poll to re-derive a value the stage receipt
        already carries.
        """
        if not changed:
            return None
        try:
            # repo=TENANT, always: `changed` is the staged TENANT change set,
            # validated against the tenant repository's own mutability policy a
            # few lines up. Handing these paths to the platform ladder would
            # classify a tenant tools/** edit as an image build.
            return change_classifier.classify_change(
                list(changed), None,
                repo=change_classifier.REPO_TENANT,
                release_id=change.desired_platform_release,
            )
        except (change_classifier.ChangeClassifierError, PlatformReleasePolicyError):
            # Decoration must never turn a valid stage receipt into an error.
            # The two callers above have already refused anything the policy
            # rejects, so reaching here means the classifier and the policy
            # disagree; the receipt is still correct without the class.
            _LOG.warning("change_class_unavailable: change_set=%s", change.change_set_id)
            return None

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
                "idempotency_key": change.idempotency_key,
                **(CustomizationService._branch_receipt_fields(change) if change.tool_target == "branch" else {})}

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


def _harness_publish_lost_code(response: Any) -> str | None:
    """The harness's closed code for a publish that lost for good, else None.

    Never raises: a 409 whose body is unreadable or carries any other code is not
    proof of a loss, so the caller keeps it recoverable.
    """
    if getattr(response, "status_code", None) != 409:
        return None
    try:
        body = response.json()
    except Exception:  # noqa: BLE001 - an unreadable body proves nothing
        return None
    code = body.get("error") if isinstance(body, Mapping) else None
    return code if isinstance(code, str) and code in _HARNESS_PUBLISH_LOST else None


def _unavailable(
    diagnostic_code: str, *, cause: BaseException | None = None,
) -> CustomizationServiceError:
    """Construct safe diagnostics before logging the client-safe 503."""
    error = CustomizationServiceError(
        "effective_catalog_unavailable", 503,
        diagnostic_code=diagnostic_code, cause=cause,
    )
    _LOG.warning(
        "effective_catalog_unavailable: detail=%s exception_type=%s",
        error.diagnostic_code, error.exception_type,
    )
    return error


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
            "catalog_process_lock_timeout"
        )
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with open(lock_path, "a+b") as handle:
            while not _try_os_lock(handle):
                if time.monotonic() >= deadline:
                    raise _unavailable(
                        "catalog_worker_lock_timeout"
                    )
                time.sleep(0.05)
            # Closing the handle releases the OS lock on every exit path,
            # including an exception in the body and including process death.
            yield
    finally:
        lock.release()


def _worktree_add(bare: Path, target: Path, commit: str) -> subprocess.CompletedProcess:
    """Add the detached worktree, capturing output without logging it."""
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


def _dangling_worktree(target: Path, bare: Path) -> bool:
    """Return true only for a valid pointer into bare/worktrees, a readable bare,
    an absent listing entry, and two stat misses separated by a recheck delay.

    Any doubt (OSError other than the required missing-path evidence,
    unexpected layout, or unreadable listing) returns False so nothing is moved.
    """
    try:
        dotgit = target / ".git"
        if not dotgit.is_file():
            return False
        lines = dotgit.read_text(encoding="utf-8").splitlines()
        if len(lines) != 1 or not lines[0].startswith("gitdir: "):
            return False
        raw = lines[0][8:].strip()
        if not raw:
            return False
        gitdir = Path(raw)
        if not gitdir.is_absolute():
            gitdir = target / gitdir
        if os.path.normcase(os.path.realpath(gitdir.parent)) != os.path.normcase(
            os.path.realpath(bare / "worktrees")
        ):
            return False
        if not (bare / "HEAD").is_file():
            return False
        try:
            if gitdir.name in os.listdir(bare / "worktrees"):
                return False
        except FileNotFoundError:
            pass  # The readable bare above makes a missing worktrees directory safe.
        try:
            gitdir.stat()
        except FileNotFoundError:
            pass
        else:
            return False
        time.sleep(_DANGLING_RECHECK_S)
        try:
            gitdir.stat()
        except FileNotFoundError:
            return True
    except (OSError, UnicodeError, ValueError):
        return False
    return False


def _materialize_worktree(bare: Path, target: Path, commit: str) -> None:
    """Ensure `target` holds `commit`, serialized against every other worker."""
    target.parent.mkdir(parents=True, exist_ok=True)
    lock_path = target.parent / ".materialize.lock"
    with _exclusive_materialize(bare, lock_path):
        if target.exists():
            if not _dangling_worktree(target, bare):
                return  # another holder materialized it while we waited
            try:
                os.replace(
                    target, target.with_name(f"{target.name}.dangling-{time.time_ns()}"),
                )
            except OSError as exc:
                raise _unavailable("catalog_dangling_move_failed", cause=exc)
            _LOG.warning("effective_catalog_worktree_dangling: moved_aside")
        add_cause = None
        try:
            first = _worktree_add(bare, target, commit)
        except subprocess.TimeoutExpired as exc:
            add_cause = exc
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
        except subprocess.TimeoutExpired as exc:
            add_cause = exc
            # Twice over budget is a real environment problem, not a blip. Discard
            # so no torn tree is left for the next caller to find, then 503.
            retry = _discard_timed_out_add(target)
        if retry.returncode != 0 and not target.exists():
            raise _unavailable(
                "catalog_worktree_add_failed", cause=add_cause,
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
    diagnostic_code = "catalog_worktree_verification_failed"
    diagnostic_cause = None
    observed = ""
    for attempt in range(_MATERIALIZE_ATTEMPTS):
        # Attempt 0 fast-paths a settled worktree without taking the lock.
        # Every later attempt goes through it, so a caller that arrived mid-add
        # blocks until the builder finishes instead of re-reading torn state.
        if attempt or not target.exists():
            _materialize_worktree(bare, target, commit)
        diagnostic_cause = None
        try:
            probe = subprocess.run(
                ["git", *_git_trust(target), "-C", str(target), "rev-parse", "HEAD"],
                text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10,
            )
            if probe.returncode != 0:
                diagnostic_code = "catalog_head_probe_failed"
            else:
                observed = probe.stdout.strip()
                if observed == _NULL_OID:
                    diagnostic_code = "catalog_head_pending"
                elif observed != commit:
                    diagnostic_code = "catalog_commit_mismatch"
                elif hashlib.sha256(
                    (target / "registry.json").read_bytes()
                ).hexdigest() != digest:
                    diagnostic_code = "catalog_digest_mismatch"
                else:
                    return
        except (OSError, subprocess.SubprocessError) as exc:
            diagnostic_code = "catalog_worktree_verification_failed"
            diagnostic_cause = exc
        if attempt + 1 < _MATERIALIZE_ATTEMPTS:
            time.sleep(0.05 * (attempt + 1))
    # Settled and still wrong: report which invariant the worktree broke.
    if observed and observed not in (commit, _NULL_OID):
        raise CustomizationServiceError(
            "effective_catalog_commit_mismatch", 503, diagnostic_code=diagnostic_code, cause=diagnostic_cause
        )
    if observed == commit:
        raise CustomizationServiceError(
            "effective_catalog_digest_mismatch", 503, diagnostic_code=diagnostic_code, cause=diagnostic_cause
        )
    raise _unavailable(diagnostic_code, cause=diagnostic_cause)


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
            raise _unavailable("catalog_symlink_rejected")
        if not _path_within(target, resolved_root):
            raise _unavailable("catalog_containment_rejected")
        _verified_worktree(bare, target, pin.catalog_commit, pin.catalog_digest)
        return target
    except ChangeSetNotFoundError:
        if enabled(5, tenant_id) or enabled(6, tenant_id):
            raise _unavailable("catalog_pin_missing")
        return None
    except CustomizationServiceError:
        raise
    except (OSError, sqlite3.DatabaseError, subprocess.SubprocessError) as exc:
        raise _unavailable("catalog_directory_failed", cause=exc) from exc


def load_authoritative_tenant_tools(tenant_id: str) -> list[dict[str, Any]] | None:
    """Join one complete durable generation; None means legacy resolution.

    Materialization may itself observe a newer pin. Never combine its rows with
    the snapshot captured before it: reread full authority even on a failed attempt.
    """
    tenant_id = _tenant_id(tenant_id)
    try:
        projection = effective_catalog_pin(tenant_id)
    except CustomizationServiceError as exc:
        if exc.code == "effective_catalog_unavailable":
            raise CustomizationServiceError("record_fields_read_failed", 503) from exc
        raise
    if projection is None:
        return None
    service = CustomizationService.configured()
    for attempt in range(2):
        pin = service._record_fields_pin(tenant_id)
        if pin is None:
            if attempt == 0:
                # The durable row is the authority; the projection above only applied
                # the rollout and path guards. No row means no authoritative pin, so
                # resolution stays legacy (which still materializes through
                # effective_catalog_dir and never falls back to mutable main).
                return None
            raise _unavailable("catalog_authority_disappeared")
        failure = None
        joined = None
        try:
            root = effective_catalog_dir(tenant_id)
            change = service._record_fields_change(tenant_id, pin.change_set_id)
            if (not service._pin_matches_change(pin, change)
                    or change.state not in {ChangeState.PUBLISHED, ChangeState.ROLLED_BACK}):
                raise CustomizationServiceError("record_fields_invalid", 503)
            raw = (root / "registry.json").read_bytes() if root is not None else b""
            if hashlib.sha256(raw).hexdigest() != pin.catalog_digest:
                raise CustomizationServiceError("effective_catalog_digest_mismatch", 503)
            rows = service._record_fields_rows(change, pin.catalog_commit, pin.catalog_digest)
            if json.loads(raw).get("tools") != list(rows.values()):
                raise CustomizationServiceError("record_fields_invalid", 503)
            snapshot = service._verified_record_fields(change, set())
            bindings = parse_catalog_record_fields_json(snapshot)["tools"] if snapshot is not None else {}
            joined = [dict(row) for row in rows.values()]
            for row in joined:
                binding = bindings.get(row["name"])
                if binding is not None:
                    row["graph_input"] = binding["graph_input"]
        except CustomizationServiceError as exc:
            if isinstance(exc.__cause__, (sqlite3.DatabaseError, PostgresError)):
                failure = CustomizationServiceError("record_fields_read_failed", 503)
            else:
                failure = exc
        except ToolRecordFieldError as exc:
            failure = CustomizationServiceError("record_fields_invalid", 503)
        except (sqlite3.DatabaseError, PostgresError, OSError, RuntimeError) as exc:
            failure = CustomizationServiceError("record_fields_read_failed", 503)
        except (ValueError, TypeError, KeyError, RecursionError) as exc:
            failure = CustomizationServiceError("record_fields_invalid", 503)
        if service._pin_identity(service._record_fields_pin(tenant_id)) != service._pin_identity(pin):
            if attempt == 0:
                continue
            raise _unavailable("catalog_authority_changed")
        if failure is not None:
            raise failure
        return joined
    raise _unavailable("catalog_authority_unavailable")


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
        raise _unavailable("catalog_pin_lookup_failed", cause=exc) from exc
    return {
        "catalog_commit": pin.catalog_commit,
        "effective_catalog_digest": pin.catalog_digest,
    }
