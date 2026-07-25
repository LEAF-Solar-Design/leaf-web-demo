"""Trusted orchestration for the frozen customization HTTP contract.

Only this module bridges HTTP-facing data to the durable coordination store and
the isolated tenant Git repository. It never treats a mutable checkout or a
request-supplied tenant, role, release, or digest as authority.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import sqlite3
import subprocess
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping
from uuid import uuid4

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
from customization_store import SQLiteCustomizationStore
from platform_release_policy import PlatformReleasePolicyError, classify_path, load_policy
from tenant_id_validator import is_valid_tenant_id


CONTRACT = "leaf.customization.v1"
DEFAULT_DB = Path(__file__).resolve().parent / "customization.db"
_configured_lock = threading.Lock()
_configured_services: dict[str, "CustomizationService"] = {}
_LOG = logging.getLogger(__name__)


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


def _git(bare: Path, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", "--git-dir", str(bare), *args], check=True, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=15,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise CustomizationServiceError("tenant_repository_unavailable", 503) from exc
    return result.stdout.strip()


def _git_blob(bare: Path, object_name: str) -> bytes:
    try:
        result = subprocess.run(
            ["git", "--git-dir", str(bare), "show", object_name],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise CustomizationServiceError("tenant_repository_unavailable", 503) from exc
    return result.stdout


def _bare_repo(tenant_id: str) -> Path:
    tenant_id = _tenant_id(tenant_id)
    base = (
        os.environ.get("LEAF_TENANT_GIT_DIR", "").strip()
        or os.environ.get("LEAF_TENANT_BARE_BASE", "").strip()
    )
    if not base:
        raise CustomizationServiceError("tenant_repository_unavailable", 503)
    candidate = Path(base) / f"{tenant_id}.git"
    try:
        resolved_base = Path(base).resolve(strict=True)
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(resolved_base)
    except (OSError, ValueError) as exc:
        raise CustomizationServiceError("tenant_repository_unavailable", 503) from exc
    if not (resolved / "HEAD").is_file():
        raise CustomizationServiceError("tenant_repository_unavailable", 503)
    return resolved


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
            raise CustomizationServiceError("tenant_identity_binding_unavailable", 503) from exc
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
                "tenant_identity_binding_unavailable", 503
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
    def __init__(self, store: SQLiteCustomizationStore) -> None:
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
    store: SQLiteCustomizationStore

    @classmethod
    def configured(cls) -> "CustomizationService":
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

    def stage(self, *, tenant: Any, description: str, mode: str, idempotency_key: str) -> dict[str, Any]:
        tenant_id = _tenant_id(tenant)
        if not enabled(5, tenant_id):
            raise CustomizationServiceError("customization_stage_disabled", 404)
        if mode != "build" or not isinstance(description, str) or not description.strip():
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
        bare = _bare_repo(tenant_id)
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
        body = self._harness_stage(tenant_id, description, change)
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
            # STAGED. A retry may therefore receive only the durable receipt
            # after the first response was lost. Revalidate the committed tree
            # and return that durable result in the same retry.
            self._verify_stage_policy(durable)
            return self._receipt(durable)
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

    def _harness_stage(self, tenant_id: str, description: str, change: ChangeSet) -> Mapping[str, Any]:
        url = os.environ.get("LEAF_AUTHOR_HARNESS_URL", "").rstrip("/")
        if not url:
            raise CustomizationServiceError("customization_harness_unavailable", 503)
        try:
            import requests
            response = requests.post(
                f"{url}/author/stage", timeout=120,
                headers={"X-Harness-Secret": os.environ.get("LEAF_HARNESS_SECRET", "").strip()},
                json={"tenant_id": tenant_id, "description": description, "changeSetId": change.change_set_id,
                      "expectedBaseSha": change.base_commit, "platformRelease": change.desired_platform_release,
                      "workspaceContractDigest": change.workspace_contract_digest,
                      "idempotencyKey": change.idempotency_key},
            )
            response.raise_for_status()
            body = response.json()
        except Exception as exc:
            raise CustomizationServiceError("customization_harness_unavailable", 503) from exc
        if not isinstance(body, Mapping):
            raise CustomizationServiceError("invalid_staged_receipt", 502)
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
        if any(staged_by_name.get(name) != item for name, item in base_by_name.items()):
            raise CustomizationServiceError("existing_catalog_entry_changed", 403)
        added = [item for name, item in staged_by_name.items() if name not in base_by_name]
        if len(added) != 1:
            raise CustomizationServiceError("invalid_staged_catalog_delta", 422)
        added_tool = added[0]
        entry = added_tool.get("entry")
        if (not isinstance(entry, str) or not entry.startswith("tools/")
                or entry not in changed):
            raise CustomizationServiceError("invalid_staged_tool_entry", 422)
        if body is not None:
            tool = body.get("tool")
            if not isinstance(tool, Mapping) or dict(tool) != added_tool:
                raise CustomizationServiceError("invalid_staged_tool", 422)

    def confirm(self, *, tenant_id: str, change_set_id: str) -> dict[str, Any]:
        tenant_id = _tenant_id(tenant_id)
        if not deps.auth_live():
            raise CustomizationServiceError("customization_auth_required", 503)
        if not enabled(6, tenant_id):
            raise CustomizationServiceError("customization_publish_disabled", 404)
        change = self.store.get_change_set(tenant_id=tenant_id, change_set_id=change_set_id)
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

    def publish(self, *, tenant: Any, request: PublishRequest, confirmation_id: str, idempotency_key: str) -> dict[str, Any]:
        tenant_id = _tenant_id(tenant)
        if not enabled(6, tenant_id):
            raise CustomizationServiceError("customization_publish_disabled", 404)
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
            raise CustomizationServiceError("customization_publish_incomplete", 503) from exc
        commit = body.get("commit") if isinstance(body, Mapping) else None
        if not isinstance(commit, str):
            raise CustomizationServiceError("customization_publish_incomplete", 502)
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
# Only a crashed holder should ever be evicted, so this sits far above the 20s
# `worktree add` timeout rather than near it.
_MATERIALIZE_LOCK_STALE = 300.0

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


@contextmanager
def _exclusive_materialize(bare: Path, lock_dir: Path):
    """Serialize materialization for one tenant across threads AND processes.

    `os.mkdir` is atomic on POSIX and on Windows, so exactly one holder wins
    without a platform-specific file-locking call. A lock older than
    ``_MATERIALIZE_LOCK_STALE`` is taken over, so a worker killed mid-add
    cannot wedge the tenant permanently.
    """
    with _tenant_worktree_lock(bare):
        deadline = time.monotonic() + _MATERIALIZE_LOCK_TIMEOUT
        while True:
            try:
                lock_dir.mkdir()
                break
            except FileExistsError:
                try:
                    held = time.time() - lock_dir.stat().st_mtime
                except OSError:
                    continue  # released underneath us; try to claim it now
                if held > _MATERIALIZE_LOCK_STALE:
                    try:
                        lock_dir.rmdir()
                    except OSError:
                        pass
                    continue
                if time.monotonic() >= deadline:
                    raise _unavailable(
                        f"materialization lock held past {_MATERIALIZE_LOCK_TIMEOUT}s: {lock_dir}"
                    )
                time.sleep(0.05)
        try:
            yield
        finally:
            try:
                lock_dir.rmdir()
            except OSError:
                pass


def _worktree_add(bare: Path, target: Path, commit: str) -> subprocess.CompletedProcess:
    """Add the detached worktree, CAPTURING stderr.

    The previous DEVNULL here is why three CI failures produced nothing but a
    generic 503: git's actual complaint was thrown away at the point of failure.
    """
    return subprocess.run(
        ["git", "-c", "core.autocrlf=false", "--git-dir", str(bare),
         "worktree", "add", "--detach", str(target), commit],
        text=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=20,
    )


def _materialize_worktree(bare: Path, target: Path, commit: str) -> None:
    """Ensure `target` holds `commit`, serialized against every other worker."""
    target.parent.mkdir(parents=True, exist_ok=True)
    lock_dir = target.parent / ".materialize.lock"
    with _exclusive_materialize(bare, lock_dir):
        if target.exists():
            return  # another holder materialized it while we waited
        first = _worktree_add(bare, target, commit)
        if first.returncode == 0 or target.exists():
            return
        # A lost race or a crash can leave the path registered but absent, and
        # git then refuses every add on it until the registration is pruned.
        prune = subprocess.run(
            ["git", "--git-dir", str(bare), "worktree", "prune"],
            text=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=20,
        )
        retry = _worktree_add(bare, target, commit)
        if retry.returncode != 0 and not target.exists():
            raise _unavailable(
                f"git worktree add {target} @ {commit}: rc={first.returncode} "
                f"{_git_stderr(first)!r}; prune rc={prune.returncode} "
                f"{_git_stderr(prune)!r}; retry rc={retry.returncode} "
                f"{_git_stderr(retry)!r}"
            )


def _verified_worktree(bare: Path, target: Path, commit: str) -> tuple[str, bytes]:
    """Return the worktree's observed HEAD and its registry.json bytes.

    Each attempt re-materializes when the directory is missing. The loop this
    replaces only ever re-ran `rev-parse`, which can never recover the one
    failure that actually happens: the directory being deleted.
    """
    detail = "no attempt completed"
    for attempt in range(_MATERIALIZE_ATTEMPTS):
        # Attempt 0 fast-paths an already-good worktree without taking the
        # lock. Every later attempt goes through it, so a caller that arrived
        # mid-add blocks until the holder finishes instead of spinning on a
        # half-written directory and exhausting its retries.
        if attempt or not target.exists():
            _materialize_worktree(bare, target, commit)
        try:
            probe = subprocess.run(
                ["git", "-C", str(target), "rev-parse", "HEAD"],
                text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10,
            )
            if probe.returncode == 0:
                # A tampered registry still reads and still rev-parses; the
                # caller's digest check is what must reject it, not a retry.
                return probe.stdout.strip(), (target / "registry.json").read_bytes()
            detail = f"git rev-parse HEAD rc={probe.returncode}: {_git_stderr(probe)!r}"
        except (OSError, subprocess.SubprocessError) as exc:
            detail = f"{type(exc).__name__}: {exc}"
        if attempt + 1 < _MATERIALIZE_ATTEMPTS:
            time.sleep(0.05 * (attempt + 1))
    raise _unavailable(f"{target} @ {commit} unverifiable after "
                       f"{_MATERIALIZE_ATTEMPTS} attempts: {detail}")


def effective_catalog_dir(tenant_id: str) -> Path | None:
    """Materialize the durable pin, even after rollout flags are turned off.

    R5/R6 flags control mutation. Once a catalog is published, its durable pin
    remains runtime authority so a flag change cannot expose mutable ``main``.
    """
    tenant_id = _tenant_id(tenant_id)
    try:
        path = database_path()
        if _shared_sqlite_path(path):
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
        if not path.exists():
            if enabled(5, tenant_id) or enabled(6, tenant_id):
                raise CustomizationServiceError(
                    "effective_catalog_authority_unavailable", 503
                )
            return None
        if _shared_sqlite_path(path):
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
        observed, materialized_registry = _verified_worktree(
            bare, target, pin.catalog_commit
        )
        if observed != pin.catalog_commit:
            raise CustomizationServiceError(
                "effective_catalog_commit_mismatch", 503,
                detail=f"{target} HEAD {observed!r} != pinned {pin.catalog_commit!r}",
            )
        if hashlib.sha256(materialized_registry).hexdigest() != pin.catalog_digest:
            raise CustomizationServiceError(
                "effective_catalog_digest_mismatch", 503,
                detail=f"{target}/registry.json does not match the pinned digest",
            )
        return target
    except ChangeSetNotFoundError:
        if enabled(5, tenant_id) or enabled(6, tenant_id):
            raise _unavailable(f"no effective catalog pinned for tenant {tenant_id}")
        return None
    except CustomizationServiceError:
        raise
    except (OSError, sqlite3.DatabaseError, subprocess.SubprocessError) as exc:
        raise _unavailable(f"{type(exc).__name__}: {exc}") from exc
