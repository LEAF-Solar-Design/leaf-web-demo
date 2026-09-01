"""Trusted Glug executor for bounded Mushy maintenance proposals.

Client input selects an allowed power and supplies a short-lived claim. The
server selects the adoption manifest, repository, artifact, environment, and
provider adapters. Stage receipts come from Git after the author returns, never
from author output.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
import math
import os
import re
import secrets
import shutil
import stat
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence

import glug_adoption


SHA40 = re.compile(r"^[0-9a-f]{40}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
SAFE_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,199}$")
SECRET_KEY = re.compile(
    r"(secret|token|password|credential|private.?key|grant|stripe|payment|"
    r"apple|resend|discord|github|aws|deploy)", re.I)
SECRET_VALUE = re.compile(
    r"(?:gh[opsu]_[A-Za-z0-9]{20,}|sk_(?:live|test)_[A-Za-z0-9]{12,}|"
    r"whsec_[A-Za-z0-9]{12,}|-----BEGIN [A-Z ]*PRIVATE KEY-----)"
)
LOCAL_PATH = re.compile(r"(?:[A-Za-z]:\\|/(?:Users|home|tmp)/)")

CLAIM_REQUEST_FIELDS = frozenset({"workspace_id", "requested_power"})
EXECUTE_FIELDS = frozenset({
    "workspace_id", "requested_power", "instruction", "claim",
})
CLAIM_FIELDS = frozenset({
    "contract", "id", "workspace", "actor_digest", "power", "base_commit",
    "issued_at", "expires_at", "signature",
})
PUBLISH_FIELDS = frozenset({
    "workspace_id", "requested_power", "approval_id", "stage_receipt",
})
STAGE_RECEIPT_FIELDS = frozenset({
    "contract", "workspace", "repository", "requested_power", "base_commit",
    "commit", "tree", "changed_files", "diff_bytes", "diff_sha256",
    "claim_id", "mushy_pin", "limits", "signature",
})
LIMIT_FIELDS = frozenset({
    "max_changed_files", "max_diff_bytes", "author_timeout_seconds",
    "wrapper_timeout_seconds", "reclaim_timeout_seconds",
})
AUTHOR_POWERS = frozenset({
    "code_question", "announcement_draft", "schedule_draft", "stage_change",
})
PUBLICATION_POWERS = frozenset({"create_review_branch", "create_pull_request"})
READ_ONLY_POWERS = AUTHOR_POWERS - {"stage_change"}
SAFE_AUTHOR_ENV_KEYS = frozenset({"PATH", "SYSTEMROOT", "TMP", "TEMP", "LANG", "LC_ALL"})


class GlugExecutorError(RuntimeError):
    """A stable fail-closed refusal from the trusted Glug executor."""

    def __init__(self, code: str, message: str, status: int = 422):
        super().__init__(message)
        self.code = code
        self.status = status


class CommandRunner(Protocol):
    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        timeout_seconds: int,
        env: Mapping[str, str] | None = None,
    ) -> bytes: ...


class AuthorRunner(Protocol):
    def run(
        self,
        payload: Mapping[str, Any],
        *,
        repository: Path,
        artifact_root: Path,
        author_timeout_seconds: int,
        wrapper_timeout_seconds: int,
        env: Mapping[str, str],
    ) -> Mapping[str, Any]: ...


class ApprovalVerifier(Protocol):
    def verify(
        self,
        *,
        approval_id: str,
        actor_id: str,
        power: str,
        repository_slug: str,
        commit: str,
    ) -> Mapping[str, Any] | None: ...


class ReviewProvider(Protocol):
    def create_review_branch(
        self, *, repository_slug: str, repository: Path, commit: str, branch_name: str
    ) -> Mapping[str, Any]: ...

    def create_pull_request(
        self,
        *,
        repository_slug: str,
        repository: Path,
        commit: str,
        branch_name: str,
        base_branch: str,
        title: str,
    ) -> Mapping[str, Any]: ...


@dataclass(frozen=True)
class CreatedWorkspace:
    claim_id: str
    repository: Path
    expected_head: str | None


@dataclass(frozen=True)
class WorkspaceLease:
    claim_id: str
    lease_id: str
    repository: Path
    base_commit: str
    head_commit: str | None = None


class ClaimWorkspaceManager(Protocol):
    def create_claim(
        self, claim_id: str, *, now: dt.datetime, reclaim_seconds: int
    ) -> CreatedWorkspace: ...

    def activate_claim(
        self, created: CreatedWorkspace, *, base_commit: str, now: dt.datetime
    ) -> None: ...

    def discard_unissued(self, claim_id: str) -> None: ...

    def acquire_execution(
        self,
        claim_id: str,
        *,
        expected_base: str,
        now: dt.datetime,
        reclaim_seconds: int,
    ) -> WorkspaceLease: ...

    def preserve_stage(
        self, lease: WorkspaceLease, *, head_commit: str, now: dt.datetime
    ) -> None: ...

    def finish_read_only(self, lease: WorkspaceLease) -> None: ...

    def fail_execution(self, lease: WorkspaceLease) -> None: ...

    def reclaim_claim_if_stale(
        self, claim_id: str, *, now: dt.datetime, reclaim_seconds: int
    ) -> bool: ...

    def acquire_publication(
        self,
        claim_id: str,
        *,
        expected_base: str,
        expected_head: str,
        now: dt.datetime,
    ) -> WorkspaceLease: ...

    def release_publication(self, lease: WorkspaceLease, *, now: dt.datetime) -> None: ...

    def finish_publication(self, lease: WorkspaceLease) -> None: ...


class _InjectedWorkspaceManager:
    """In-memory lifecycle adapter for explicitly injected test repositories."""

    def __init__(self, repository: Path | str):
        self.repository = Path(repository)
        self._records: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def create_claim(
        self, claim_id: str, *, now: dt.datetime, reclaim_seconds: int
    ) -> CreatedWorkspace:
        with self._lock:
            self._reclaim(now, reclaim_seconds)
            if claim_id in self._records:
                raise GlugExecutorError("workspace_exists", "Claim workspace already exists", 409)
            self._records[claim_id] = {
                "status": "creating", "base": None, "head": None,
                "lease": None, "updated": _epoch(now),
            }
        return CreatedWorkspace(claim_id, self.repository, None)

    def activate_claim(
        self, created: CreatedWorkspace, *, base_commit: str, now: dt.datetime
    ) -> None:
        with self._lock:
            record = self._require(created.claim_id, "creating")
            record.update(status="claimed", base=base_commit, updated=_epoch(now))

    def discard_unissued(self, claim_id: str) -> None:
        with self._lock:
            self._records.pop(claim_id, None)

    def acquire_execution(
        self, claim_id: str, *, expected_base: str, now: dt.datetime,
        reclaim_seconds: int,
    ) -> WorkspaceLease:
        with self._lock:
            self._reclaim(now, reclaim_seconds)
            record = self._records.get(claim_id)
            if record is None:
                raise GlugExecutorError("workspace_unavailable", "Claim workspace is unavailable", 409)
            if record["status"] == "running":
                raise GlugExecutorError("claim_busy", "Claim execution is already running", 409)
            if record["status"] != "claimed":
                raise GlugExecutorError("claim_reused", "Claim has already been used", 409)
            if record["base"] != expected_base:
                raise GlugExecutorError("base_drift", "Claim workspace base drifted", 409)
            lease_id = secrets.token_hex(16)
            record.update(status="running", lease=lease_id, updated=_epoch(now))
            return WorkspaceLease(claim_id, lease_id, self.repository, expected_base)

    def preserve_stage(
        self, lease: WorkspaceLease, *, head_commit: str, now: dt.datetime
    ) -> None:
        with self._lock:
            record = self._require_lease(lease, "running")
            record.update(
                status="staged", head=head_commit, lease=None, updated=_epoch(now))

    def finish_read_only(self, lease: WorkspaceLease) -> None:
        self._finish_lease(lease, "running")

    def fail_execution(self, lease: WorkspaceLease) -> None:
        self._finish_lease(lease, "running")

    def reclaim_claim_if_stale(
        self, claim_id: str, *, now: dt.datetime, reclaim_seconds: int
    ) -> bool:
        with self._lock:
            record = self._records.get(claim_id)
            if record is None or record["status"] not in {"creating", "claimed", "running"}:
                return False
            if record["updated"] + reclaim_seconds > _epoch(now):
                return False
            del self._records[claim_id]
            return True

    def acquire_publication(
        self, claim_id: str, *, expected_base: str, expected_head: str,
        now: dt.datetime,
    ) -> WorkspaceLease:
        with self._lock:
            record = self._records.get(claim_id)
            if record is None:
                raise GlugExecutorError("workspace_unavailable", "Staged workspace is unavailable", 409)
            if record["status"] == "publishing":
                raise GlugExecutorError("claim_busy", "Claim publication is already running", 409)
            if record["status"] != "staged":
                raise GlugExecutorError("workspace_unavailable", "Staged workspace is unavailable", 409)
            if record["base"] != expected_base or record["head"] != expected_head:
                raise GlugExecutorError("receipt_invalid", "Staged workspace identity drifted", 409)
            lease_id = secrets.token_hex(16)
            record.update(status="publishing", lease=lease_id, updated=_epoch(now))
            return WorkspaceLease(
                claim_id, lease_id, self.repository, expected_base, expected_head)

    def release_publication(self, lease: WorkspaceLease, *, now: dt.datetime) -> None:
        with self._lock:
            record = self._require_lease(lease, "publishing")
            record.update(status="staged", lease=None, updated=_epoch(now))

    def finish_publication(self, lease: WorkspaceLease) -> None:
        self._finish_lease(lease, "publishing")

    def _finish_lease(self, lease: WorkspaceLease, status: str) -> None:
        with self._lock:
            self._require_lease(lease, status)
            del self._records[lease.claim_id]

    def _require(self, claim_id: str, status: str) -> dict[str, Any]:
        record = self._records.get(claim_id)
        if record is None or record["status"] != status:
            raise GlugExecutorError("workspace_invalid", "Claim workspace state is invalid", 409)
        return record

    def _require_lease(self, lease: WorkspaceLease, status: str) -> dict[str, Any]:
        record = self._require(lease.claim_id, status)
        if record["lease"] != lease.lease_id:
            raise GlugExecutorError("workspace_invalid", "Claim workspace lease is invalid", 409)
        return record

    def _reclaim(self, now: dt.datetime, reclaim_seconds: int) -> None:
        cutoff = _epoch(now) - reclaim_seconds
        stale = [
            claim_id for claim_id, record in self._records.items()
            if record["status"] in {"creating", "claimed", "running"}
            and record["updated"] <= cutoff
        ]
        for claim_id in stale:
            del self._records[claim_id]


class LocalClaimWorkspaceManager:
    """Per-claim standalone clones with atomic marker-state transitions."""

    _STATE_FIELDS = frozenset({
        "version", "claim_id", "created_at", "updated_at", "base_commit",
        "head_commit", "signature",
    })
    _ACTIVE = frozenset({"creating", "claimed", "running"})
    _LEASE = re.compile(r"^[0-9a-f]{32}$")

    def __init__(
        self,
        *,
        canonical_source: Path | str,
        workspace_root: Path | str,
        state_key: bytes,
        commands: CommandRunner,
        env: Mapping[str, str],
    ):
        if not isinstance(state_key, bytes) or len(state_key) < 32:
            raise GlugExecutorError(
                "workspace_configuration_invalid", "Workspace authentication is unavailable", 503)
        self.state_key = state_key
        self.commands = commands
        self.env = dict(env)
        self.canonical_source = self._trusted_directory(
            Path(canonical_source), "canonical Git source")
        self.workspace_root = self._trusted_directory(
            Path(workspace_root), "workspace root")
        if (
            self.canonical_source == self.workspace_root
            or _is_relative_to(self.canonical_source, self.workspace_root)
            or _is_relative_to(self.workspace_root, self.canonical_source)
        ):
            raise GlugExecutorError(
                "workspace_configuration_invalid",
                "Canonical source and workspace root must not overlap",
                503,
            )

    def create_claim(
        self, claim_id: str, *, now: dt.datetime, reclaim_seconds: int
    ) -> CreatedWorkspace:
        self._validate_roots()
        _safe_id(claim_id, "claim id")
        self._reclaim_all_stale(now=now, reclaim_seconds=reclaim_seconds)
        path = self._container(claim_id)
        try:
            path.mkdir(mode=0o700)
            self._marker(path, "creating").mkdir(mode=0o700)
        except FileExistsError as exc:
            raise GlugExecutorError(
                "workspace_exists", "Claim workspace already exists", 409) from exc
        except OSError as exc:
            raise GlugExecutorError(
                "workspace_unavailable", "Claim workspace could not be created", 503) from exc
        state = {
            "version": 1,
            "claim_id": claim_id,
            "created_at": _epoch(now),
            "updated_at": _epoch(now),
            "base_commit": None,
            "head_commit": None,
        }
        try:
            self._write_state(path, state)
            expected_head = self._git_text(self.canonical_source, "rev-parse", "HEAD")
            if not SHA40.fullmatch(expected_head):
                raise GlugExecutorError(
                    "workspace_source_invalid", "Canonical Git source HEAD is invalid", 409)
            repository = path / "repository"
            self._run_git(
                self.workspace_root,
                "clone", "--quiet", "--no-local", "--no-hardlinks",
                "--no-recurse-submodules", "--",
                str(self.canonical_source), str(repository),
                timeout_seconds=reclaim_seconds,
            )
            self._run_git(repository, "remote", "remove", "origin")
            self._run_git(repository, "config", "--local", "core.autocrlf", "false")
            self._run_git(repository, "config", "--local", "core.longpaths", "true")
            return CreatedWorkspace(claim_id, repository, expected_head)
        except Exception:
            self._remove_workspace(path)
            raise

    def activate_claim(
        self, created: CreatedWorkspace, *, base_commit: str, now: dt.datetime
    ) -> None:
        source = self._exact_marker(created.claim_id, "creating")
        workspace = source.parent
        state = self._read_state(workspace, created.claim_id)
        if state["base_commit"] is not None or state["head_commit"] is not None:
            raise GlugExecutorError("workspace_invalid", "Claim workspace state is invalid", 409)
        state.update(base_commit=base_commit, updated_at=_epoch(now))
        self._write_state(workspace, state)
        self._transition(source, self._marker(workspace, "claimed"))

    def discard_unissued(self, claim_id: str) -> None:
        workspace = self._container(claim_id)
        if not workspace.exists():
            return
        locations = self._locations(claim_id)
        if len(locations) != 1 or locations[0][0] not in {"creating", "claimed"}:
            raise GlugExecutorError("workspace_invalid", "Claim workspace state is invalid", 409)
        self._remove_workspace(workspace)

    def acquire_execution(
        self, claim_id: str, *, expected_base: str, now: dt.datetime,
        reclaim_seconds: int,
    ) -> WorkspaceLease:
        self._validate_roots()
        self.reclaim_claim_if_stale(
            claim_id, now=now, reclaim_seconds=reclaim_seconds)
        locations = self._locations(claim_id)
        if not locations:
            raise GlugExecutorError("workspace_unavailable", "Claim workspace is unavailable", 409)
        if len(locations) != 1:
            raise GlugExecutorError("workspace_invalid", "Claim workspace is ambiguous", 409)
        status, _prior_lease, source = locations[0]
        if status == "running":
            raise GlugExecutorError("claim_busy", "Claim execution is already running", 409)
        if status in {"staged", "publishing"}:
            raise GlugExecutorError("claim_reused", "Claim has already been used", 409)
        if status != "claimed":
            raise GlugExecutorError("workspace_unavailable", "Claim workspace is unavailable", 409)
        workspace = source.parent
        state = self._read_state(workspace, claim_id)
        if state["base_commit"] != expected_base or state["head_commit"] is not None:
            raise GlugExecutorError("base_drift", "Claim workspace base drifted", 409)
        lease_id = secrets.token_hex(16)
        destination = self._marker(workspace, "running", lease_id)
        self._transition(source, destination)
        state.update(updated_at=_epoch(now))
        self._write_state(workspace, state)
        return WorkspaceLease(
            claim_id, lease_id, workspace / "repository", expected_base)

    def preserve_stage(
        self, lease: WorkspaceLease, *, head_commit: str, now: dt.datetime
    ) -> None:
        source = self._lease_marker(lease, "running")
        workspace = source.parent
        state = self._read_state(workspace, lease.claim_id)
        if state["base_commit"] != lease.base_commit:
            raise GlugExecutorError("base_drift", "Claim workspace base drifted", 409)
        state.update(head_commit=head_commit, updated_at=_epoch(now))
        self._write_state(workspace, state)
        self._transition(source, self._marker(workspace, "staged"))

    def finish_read_only(self, lease: WorkspaceLease) -> None:
        self._remove_workspace(self._lease_marker(lease, "running").parent)

    def fail_execution(self, lease: WorkspaceLease) -> None:
        self._remove_workspace(self._lease_marker(lease, "running").parent)

    def reclaim_claim_if_stale(
        self, claim_id: str, *, now: dt.datetime, reclaim_seconds: int
    ) -> bool:
        locations = self._locations(claim_id)
        if not locations:
            return False
        if len(locations) != 1:
            raise GlugExecutorError("workspace_invalid", "Claim workspace is ambiguous", 409)
        status, _lease_id, marker = locations[0]
        if status not in self._ACTIVE:
            return False
        workspace = marker.parent
        updated = self._workspace_updated(workspace, claim_id)
        if updated + reclaim_seconds > _epoch(now):
            return False
        self._remove_workspace(workspace)
        return True

    def acquire_publication(
        self, claim_id: str, *, expected_base: str, expected_head: str,
        now: dt.datetime,
    ) -> WorkspaceLease:
        self._validate_roots()
        locations = self._locations(claim_id)
        if not locations:
            raise GlugExecutorError("workspace_unavailable", "Staged workspace is unavailable", 409)
        if len(locations) != 1:
            raise GlugExecutorError("workspace_invalid", "Claim workspace is ambiguous", 409)
        status, _prior_lease, source = locations[0]
        if status == "publishing":
            raise GlugExecutorError("claim_busy", "Claim publication is already running", 409)
        if status != "staged":
            raise GlugExecutorError("workspace_unavailable", "Staged workspace is unavailable", 409)
        workspace = source.parent
        state = self._read_state(workspace, claim_id)
        if state["base_commit"] != expected_base or state["head_commit"] != expected_head:
            raise GlugExecutorError("receipt_invalid", "Staged workspace identity drifted", 409)
        lease_id = secrets.token_hex(16)
        destination = self._marker(workspace, "publishing", lease_id)
        self._transition(source, destination)
        state.update(updated_at=_epoch(now))
        self._write_state(workspace, state)
        return WorkspaceLease(
            claim_id, lease_id, workspace / "repository", expected_base, expected_head)

    def release_publication(self, lease: WorkspaceLease, *, now: dt.datetime) -> None:
        source = self._lease_marker(lease, "publishing")
        workspace = source.parent
        state = self._read_state(workspace, lease.claim_id)
        state.update(updated_at=_epoch(now))
        self._write_state(workspace, state)
        self._transition(source, self._marker(workspace, "staged"))

    def finish_publication(self, lease: WorkspaceLease) -> None:
        self._remove_workspace(self._lease_marker(lease, "publishing").parent)

    def _validate_roots(self) -> None:
        if self._trusted_directory(
            self.canonical_source, "canonical Git source") != self.canonical_source:
            raise GlugExecutorError(
                "workspace_configuration_invalid", "Canonical Git source changed", 503)
        if self._trusted_directory(
            self.workspace_root, "workspace root") != self.workspace_root:
            raise GlugExecutorError(
                "workspace_configuration_invalid", "Workspace root changed", 503)

    @staticmethod
    def _trusted_directory(path: Path, label: str) -> Path:
        if not path.is_absolute():
            raise GlugExecutorError(
                "workspace_configuration_invalid", label + " must be an absolute path", 503)
        try:
            absolute = Path(os.path.abspath(path))
            resolved = path.resolve(strict=True)
        except OSError as exc:
            raise GlugExecutorError(
                "workspace_configuration_invalid", label + " is unavailable", 503) from exc
        if not resolved.is_dir() or _path_has_link_component(absolute):
            raise GlugExecutorError(
                "workspace_configuration_invalid", label + " must be a real directory", 503)
        if os.path.normcase(str(absolute)) != os.path.normcase(str(resolved)):
            raise GlugExecutorError(
                "workspace_configuration_invalid", label + " must not traverse a link", 503)
        return resolved

    def _container(self, claim_id: str) -> Path:
        digest = hashlib.sha256(_safe_id(claim_id, "claim id").encode("utf-8")).hexdigest()
        return self.workspace_root / digest

    @staticmethod
    def _marker(
        workspace: Path, status: str, lease_id: str | None = None
    ) -> Path:
        suffix = status if lease_id is None else status + "." + lease_id
        return workspace / ("." + suffix)

    def _locations(self, claim_id: str) -> list[tuple[str, str | None, Path]]:
        self._validate_roots()
        workspace = self._container(claim_id)
        if not workspace.exists():
            return []
        workspace = self._validate_workspace(workspace, claim_id)
        result: list[tuple[str, str | None, Path]] = []
        for path in workspace.iterdir():
            if path.name in {"repository", "state.json"}:
                continue
            if re.fullmatch(r"\.state-[0-9a-f]{32}\.tmp", path.name):
                continue
            if not path.name.startswith("."):
                raise GlugExecutorError(
                    "workspace_invalid", "Claim workspace contains an unexpected entry", 409)
            suffix = path.name[1:]
            if suffix in {"creating", "claimed", "staged"}:
                self._validate_marker(path)
                result.append((suffix, None, path))
                continue
            parts = suffix.split(".")
            if len(parts) == 2 and parts[0] in {"running", "publishing"} and self._LEASE.fullmatch(parts[1]):
                self._validate_marker(path)
                result.append((parts[0], parts[1], path))
                continue
            raise GlugExecutorError(
                "workspace_invalid", "Claim workspace contains an unexpected entry", 409)
        return sorted(result, key=lambda item: item[2].name)

    def _exact_marker(self, claim_id: str, status: str) -> Path:
        locations = self._locations(claim_id)
        expected = [item for item in locations if item[0] == status]
        if len(locations) != 1 or len(expected) != 1:
            raise GlugExecutorError("workspace_invalid", "Claim workspace state is invalid", 409)
        return expected[0][2]

    def _lease_marker(self, lease: WorkspaceLease, status: str) -> Path:
        locations = self._locations(lease.claim_id)
        expected = [
            item for item in locations
            if item[0] == status and item[1] == lease.lease_id
        ]
        if len(locations) != 1 or len(expected) != 1:
            raise GlugExecutorError("workspace_invalid", "Claim workspace lease is invalid", 409)
        return expected[0][2]

    def _validate_workspace(self, path: Path, claim_id: str) -> Path:
        if path != self._container(claim_id) or path.parent != self.workspace_root or not path.exists():
            raise GlugExecutorError("workspace_unavailable", "Claim workspace is unavailable", 409)
        if _is_link(path) or not path.is_dir():
            raise GlugExecutorError("workspace_invalid", "Claim workspace must not be linked", 409)
        try:
            resolved = path.resolve(strict=True)
        except OSError as exc:
            raise GlugExecutorError("workspace_invalid", "Claim workspace is invalid", 409) from exc
        if resolved.parent != self.workspace_root:
            raise GlugExecutorError("workspace_invalid", "Claim workspace escaped its root", 409)
        self._read_state(resolved, claim_id)
        repository = resolved / "repository"
        if repository.exists() and (_is_link(repository) or not repository.is_dir()):
            raise GlugExecutorError("workspace_invalid", "Claim repository must not be linked", 409)
        if repository.exists():
            try:
                resolved_repository = repository.resolve(strict=True)
            except OSError as exc:
                raise GlugExecutorError(
                    "workspace_invalid", "Claim repository is invalid", 409) from exc
            if resolved_repository.parent != resolved:
                raise GlugExecutorError(
                    "workspace_invalid", "Claim repository escaped its workspace", 409)
        return resolved

    @staticmethod
    def _validate_marker(path: Path) -> None:
        if _is_link(path) or not path.is_dir():
            raise GlugExecutorError(
                "workspace_invalid", "Claim workspace marker is invalid", 409)

    def _transition(self, source: Path, destination: Path) -> None:
        if (
            source.parent != destination.parent
            or source.parent.parent != self.workspace_root
            or destination.exists()
        ):
            raise GlugExecutorError("workspace_invalid", "Claim workspace transition was refused", 409)
        self._validate_marker(source)
        try:
            source.rename(destination)
        except OSError as exc:
            raise GlugExecutorError("claim_busy", "Claim workspace transition was lost", 409) from exc

    def _state_signature(self, value: Mapping[str, Any]) -> str:
        canonical = json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        ).encode("utf-8")
        return hmac.new(
            self.state_key, b"glug-workspace-state-v1\0" + canonical,
            hashlib.sha256,
        ).hexdigest()

    def _write_state(self, workspace: Path, value: Mapping[str, Any]) -> None:
        unsigned = {key: value[key] for key in self._STATE_FIELDS if key != "signature"}
        payload = {**unsigned, "signature": self._state_signature(unsigned)}
        target = workspace / "state.json"
        temporary = workspace / (".state-" + secrets.token_hex(16) + ".tmp")
        try:
            with temporary.open("x", encoding="utf-8", newline="\n") as handle:
                json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
        except OSError as exc:
            raise GlugExecutorError("workspace_invalid", "Claim workspace state could not be written", 409) from exc
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    def _read_state(self, workspace: Path, claim_id: str) -> dict[str, Any]:
        target = workspace / "state.json"
        if _is_link(target) or not target.is_file():
            raise GlugExecutorError("workspace_invalid", "Claim workspace state is invalid", 409)
        try:
            value = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeError) as exc:
            raise GlugExecutorError("workspace_invalid", "Claim workspace state is invalid", 409) from exc
        if not isinstance(value, Mapping) or set(value) != set(self._STATE_FIELDS):
            raise GlugExecutorError("workspace_invalid", "Claim workspace state is invalid", 409)
        unsigned = {key: value[key] for key in self._STATE_FIELDS if key != "signature"}
        signature = value["signature"]
        if (
            not isinstance(signature, str)
            or not SHA256.fullmatch(signature)
            or not hmac.compare_digest(signature, self._state_signature(unsigned))
        ):
            raise GlugExecutorError("workspace_invalid", "Claim workspace state authentication failed", 409)
        if value["version"] != 1 or value["claim_id"] != claim_id:
            raise GlugExecutorError("workspace_invalid", "Claim workspace state is invalid", 409)
        for field in ("created_at", "updated_at"):
            number = value[field]
            if isinstance(number, bool) or not isinstance(number, (int, float)) or not math.isfinite(number):
                raise GlugExecutorError("workspace_invalid", "Claim workspace time is invalid", 409)
        if value["updated_at"] < value["created_at"]:
            raise GlugExecutorError("workspace_invalid", "Claim workspace time is invalid", 409)
        for field in ("base_commit", "head_commit"):
            identity = value[field]
            if identity is not None and (not isinstance(identity, str) or not SHA40.fullmatch(identity)):
                raise GlugExecutorError("workspace_invalid", "Claim workspace identity is invalid", 409)
        return dict(value)

    def _workspace_updated(self, path: Path, claim_id: str) -> float:
        try:
            return float(self._read_state(path, claim_id)["updated_at"])
        except GlugExecutorError:
            try:
                return path.stat().st_mtime
            except OSError as exc:
                raise GlugExecutorError("workspace_invalid", "Claim workspace is invalid", 409) from exc

    def _reclaim_all_stale(self, *, now: dt.datetime, reclaim_seconds: int) -> None:
        cutoff = _epoch(now) - reclaim_seconds
        for path in list(self.workspace_root.iterdir()):
            if not SHA256.fullmatch(path.name) or _is_link(path) or not path.is_dir():
                continue
            try:
                updated = path.stat().st_mtime
                active = any(
                    re.fullmatch(r"\.(creating|claimed|running\.[0-9a-f]{32})", child.name)
                    and not _is_link(child) and child.is_dir()
                    for child in path.iterdir()
                )
                if not active:
                    continue
                state_path = path / "state.json"
                if state_path.is_file() and not _is_link(state_path):
                    raw = json.loads(state_path.read_text(encoding="utf-8"))
                    if isinstance(raw, Mapping) and isinstance(raw.get("updated_at"), (int, float)):
                        updated = float(raw["updated_at"])
                if math.isfinite(updated) and updated <= cutoff:
                    self._remove_workspace(path)
            except (OSError, json.JSONDecodeError, ValueError):
                continue

    def _remove_workspace(self, path: Path) -> None:
        self._validate_roots()
        if not path.exists():
            return
        if (
            path.parent != self.workspace_root
            or not SHA256.fullmatch(path.name)
            or _is_link(path)
            or not path.is_dir()
        ):
            raise GlugExecutorError("workspace_invalid", "Workspace cleanup target is unsafe", 409)
        trash = self.workspace_root / (".trash-" + secrets.token_hex(16))
        try:
            path.rename(trash)
        except OSError as exc:
            raise GlugExecutorError("workspace_cleanup_failed", "Workspace cleanup could not start", 500) from exc
        last_error: OSError | None = None
        for delay in (0.0, 0.02, 0.05):
            if not trash.exists():
                return
            if delay:
                time.sleep(delay)
            try:
                shutil.rmtree(trash, onerror=_rmtree_remove_readonly)
                return
            except OSError as exc:
                last_error = exc
        raise GlugExecutorError(
            "workspace_cleanup_failed", "Workspace cleanup did not finish", 500
        ) from last_error

    def _run_git(
        self, cwd: Path, *args: str, timeout_seconds: int = 30
    ) -> bytes:
        return self.commands.run(
            ("git", *args), cwd=cwd, timeout_seconds=timeout_seconds,
            env=self._git_environment())

    def _git_text(self, cwd: Path, *args: str) -> str:
        try:
            value = self._run_git(cwd, *args).decode("utf-8", errors="strict").strip()
        except UnicodeDecodeError as exc:
            raise GlugExecutorError("git_output_invalid", "Git output is invalid", 409) from exc
        if not value:
            raise GlugExecutorError("git_output_invalid", "Git output is empty", 409)
        return value

    def _git_environment(self) -> Mapping[str, str]:
        result = {
            key: value for key, value in self.env.items()
            if key.upper() in SAFE_AUTHOR_ENV_KEYS and isinstance(value, str)
        }
        result.update({
            "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never",
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "core.longpaths",
            "GIT_CONFIG_VALUE_0": "true",
        })
        return result


class SubprocessCommandRunner:
    """Shell-free Git command adapter. Network commands are never issued here."""

    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        timeout_seconds: int,
        env: Mapping[str, str] | None = None,
    ) -> bytes:
        try:
            completed = subprocess.run(
                list(argv), cwd=str(cwd), env=dict(env) if env is not None else None,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
                timeout=timeout_seconds, shell=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise GlugExecutorError("git_unavailable", "Git inspection failed", 503) from exc
        if completed.returncode != 0:
            raise GlugExecutorError("git_refused", "Git inspection failed", 409)
        return completed.stdout


class GlugExecutor:
    def __init__(
        self,
        *,
        repository: Path | str | None,
        artifact_root: Path | str,
        env: Mapping[str, str],
        adoption_path: Path | str | None = None,
        commands: CommandRunner | None = None,
        author: AuthorRunner | None = None,
        approvals: ApprovalVerifier | None = None,
        provider: ReviewProvider | None = None,
        clock: Callable[[], dt.datetime] | None = None,
        monotonic: Callable[[], float] | None = None,
        claim_id_factory: Callable[[], str] | None = None,
        workspace_manager: ClaimWorkspaceManager | None = None,
    ):
        self.repository = Path(repository) if repository is not None else None
        self.artifact_root = Path(artifact_root)
        self.env = dict(env)
        self.adoption_path = adoption_path
        self.commands = commands or SubprocessCommandRunner()
        if workspace_manager is not None:
            self.workspaces = workspace_manager
        elif self.repository is not None:
            self.workspaces = _InjectedWorkspaceManager(self.repository)
        else:
            raise GlugExecutorError(
                "workspace_configuration_invalid", "Claim workspace manager is required", 503)
        self.author = author
        self.approvals = approvals
        self.provider = provider
        self.clock = clock or (lambda: dt.datetime.now(dt.timezone.utc))
        self.monotonic = monotonic or time.monotonic
        self.claim_id_factory = claim_id_factory or (lambda: secrets.token_hex(16))

    @classmethod
    def configured(cls) -> "GlugExecutor":
        canonical_source = os.environ.get("GLUG_MUSHY_CANONICAL_GIT_SOURCE", "").strip()
        workspace_root = os.environ.get("GLUG_MUSHY_WORKSPACE_ROOT", "").strip()
        artifact_root = os.environ.get("LEAF_GLUG_MUSHY_ARTIFACT_ROOT", "").strip()
        if not canonical_source or not workspace_root or not artifact_root:
            raise GlugExecutorError(
                "executor_unavailable", "Glug trusted executor is not configured", 503)
        commands = SubprocessCommandRunner()
        manager = LocalClaimWorkspaceManager(
            canonical_source=canonical_source,
            workspace_root=workspace_root,
            state_key=_signing_key_from_env(os.environ),
            commands=commands,
            env=os.environ,
        )
        return cls(
            repository=None, artifact_root=artifact_root, env=os.environ,
            commands=commands, workspace_manager=manager)

    def pin_receipt(self) -> Mapping[str, Any]:
        adoption = glug_adoption.load_adoption(self.adoption_path)
        glug_adoption.verify_artifact_tree(adoption, self.artifact_root)
        return glug_adoption.client_pin_receipt(adoption)

    def issue_claim(
        self, request: Mapping[str, Any], *, actor_id: str
    ) -> Mapping[str, Any]:
        _exact(request, CLAIM_REQUEST_FIELDS, "claim request")
        adoption = glug_adoption.adoption_for_workspace(
            _text(request["workspace_id"], "workspace_id"), self.adoption_path)
        power = _text(request["requested_power"], "requested_power")
        if power not in AUTHOR_POWERS or power not in adoption.allowed_powers:
            raise GlugExecutorError("power_unavailable", "Requested power is unavailable", 403)
        issued = _utc(self.clock())
        claim_id = _safe_id(self.claim_id_factory(), "claim id")
        actor_digest = self._actor_digest(actor_id)
        created: CreatedWorkspace | None = None
        try:
            created = self.workspaces.create_claim(
                claim_id, now=issued,
                reclaim_seconds=_limits(adoption)["reclaim_timeout_seconds"],
            )
            state = self._repository_state(
                adoption, created.repository, expected_head=created.expected_head)
            self._require_safe_state(state)
            unsigned = {
                "contract": "glug.mushy-claim.v1",
                "id": claim_id,
                "workspace": adoption.workspace_id,
                "actor_digest": actor_digest,
                "power": power,
                "base_commit": state["head_commit"],
                "issued_at": _format_instant(issued),
                "expires_at": _format_instant(issued + dt.timedelta(seconds=300)),
            }
            claim = {**unsigned, "signature": self._sign("claim", unsigned)}
            _reject_sensitive(claim)
            self.workspaces.activate_claim(
                created, base_commit=state["head_commit"], now=issued)
            return claim
        except Exception:
            if created is not None:
                self.workspaces.discard_unissued(claim_id)
            raise

    def execute(self, request: Mapping[str, Any], *, actor_id: str) -> Mapping[str, Any]:
        _exact(request, EXECUTE_FIELDS, "execution request")
        _exact(_mapping(request["claim"], "claim"), CLAIM_FIELDS, "claim")
        adoption = glug_adoption.adoption_for_workspace(
            _text(request["workspace_id"], "workspace_id"), self.adoption_path)
        power = _text(request["requested_power"], "requested_power")
        if power not in AUTHOR_POWERS or power not in adoption.allowed_powers:
            raise GlugExecutorError("power_unavailable", "Requested power is unavailable", 403)
        instruction = _text(request["instruction"], "instruction").strip()
        if not instruction or len(instruction) > 20_000:
            raise GlugExecutorError("instruction_invalid", "Instruction is invalid")
        if self.author is None:
            raise GlugExecutorError("author_unavailable", "Glug author is not mounted", 503)

        glug_adoption.verify_artifact_tree(adoption, self.artifact_root)
        claim = _mapping(request["claim"], "claim")
        now = _utc(self.clock())
        try:
            self._verify_claim(
                claim, adoption=adoption, actor_id=actor_id,
                requested_power=power, now=now)
        except GlugExecutorError as exc:
            if exc.code == "claim_stale":
                self.workspaces.reclaim_claim_if_stale(
                    _safe_id(claim["id"], "claim id"), now=now,
                    reclaim_seconds=_limits(adoption)["reclaim_timeout_seconds"],
                )
            raise
        limits = _limits(adoption)
        lease = self.workspaces.acquire_execution(
            _safe_id(claim["id"], "claim id"),
            expected_base=_text(claim["base_commit"], "claim base_commit"),
            now=now, reclaim_seconds=limits["reclaim_timeout_seconds"],
        )
        try:
            before = self._repository_state(
                adoption, lease.repository,
                expected_head=_text(claim["base_commit"], "claim base_commit"))
            stage_request = {
                "workspace_id": adoption.workspace_id,
                "repository_slug": self._repository_slug(adoption),
                "requested_power": power,
                "claim": dict(claim),
                "repository_state": before,
            }
            try:
                glug_adoption.validate_stage_request(
                    adoption, stage_request, env=self.env, now=now)
            except glug_adoption.GlugAdoptionError as exc:
                raise GlugExecutorError("stage_refused", str(exc), 409) from exc

            author_env = self._author_environment(adoption)
            author_payload = {
                "contract": "glug.mushy-author-request.v1",
                "workspace": adoption.workspace_id,
                "power": power,
                "instruction": instruction,
                "base_commit": before["head_commit"],
                "claim_id": claim["id"],
            }
            started = self.monotonic()
            result = self.author.run(
                author_payload,
                repository=lease.repository,
                artifact_root=self.artifact_root,
                author_timeout_seconds=limits["author_timeout_seconds"],
                wrapper_timeout_seconds=limits["wrapper_timeout_seconds"],
                env=author_env,
            )
            if self.monotonic() - started > limits["wrapper_timeout_seconds"]:
                raise GlugExecutorError(
                    "wrapper_timeout", "Author wrapper exceeded its limit", 504)

            after = self._repository_state(
                adoption, lease.repository, expected_head=None)
            receipt = self._git_receipt(
                adoption=adoption,
                repository=lease.repository,
                power=power,
                claim_id=claim["id"],
                before=before,
                after=after,
            )
            response: dict[str, Any] = {"receipt": receipt}
            if power in READ_ONLY_POWERS:
                _exact(
                    _mapping(result, "author result"), frozenset({"text"}),
                    "author result")
                text = _text(result["text"], "author result").strip()
                if not text or len(text) > 50_000:
                    raise GlugExecutorError(
                        "author_result_invalid", "Author result is invalid", 502)
                _reject_sensitive({"text": text})
                response["text"] = text
                self.workspaces.finish_read_only(lease)
            else:
                _exact(_mapping(result, "author result"), frozenset(), "author result")
                self.workspaces.preserve_stage(
                    lease, head_commit=receipt["commit"], now=_utc(self.clock()))
            return response
        except Exception as exc:
            try:
                self.workspaces.fail_execution(lease)
            except GlugExecutorError as cleanup_error:
                raise cleanup_error from exc
            raise

    def publish(self, request: Mapping[str, Any], *, actor_id: str) -> Mapping[str, Any]:
        _exact(request, PUBLISH_FIELDS, "publication request")
        adoption = glug_adoption.adoption_for_workspace(
            _text(request["workspace_id"], "workspace_id"), self.adoption_path)
        power = _text(request["requested_power"], "requested_power")
        if power not in PUBLICATION_POWERS or power not in adoption.allowed_powers:
            raise GlugExecutorError("power_unavailable", "Requested power is unavailable", 403)
        if self.approvals is None or self.provider is None:
            raise GlugExecutorError(
                "publication_unavailable", "Review publication is not mounted", 503)
        approval_id = _safe_id(request["approval_id"], "approval_id")
        receipt = self._validated_stage_receipt(adoption, request["stage_receipt"])
        repository_slug = self._repository_slug(adoption)
        lease = self.workspaces.acquire_publication(
            _safe_id(receipt["claim_id"], "claim id"),
            expected_base=receipt["base_commit"],
            expected_head=receipt["commit"],
            now=_utc(self.clock()),
        )
        provider_succeeded = False
        try:
            after = self._repository_state(
                adoption, lease.repository, expected_head=receipt["commit"])
            rederived = self._git_receipt(
                adoption=adoption,
                repository=lease.repository,
                power="stage_change",
                claim_id=receipt["claim_id"],
                before={"head_commit": receipt["base_commit"]},
                after=after,
            )
            if dict(rederived) != dict(receipt):
                raise GlugExecutorError(
                    "receipt_invalid", "Stage receipt no longer matches Git", 409)
            approval = self.approvals.verify(
                approval_id=approval_id,
                actor_id=actor_id,
                power=power,
                repository_slug=repository_slug,
                commit=receipt["commit"],
            )
            if not approval or approval.get("approved") is not True:
                raise GlugExecutorError(
                    "approval_required",
                    "Explicit review publication approval is required",
                    403,
                )
            _reject_sensitive(approval)
            branch_name = f"glug/mushy/{receipt['commit'][:12]}"
            if power == "create_review_branch":
                provider_result = self.provider.create_review_branch(
                    repository_slug=repository_slug,
                    repository=lease.repository,
                    commit=receipt["commit"],
                    branch_name=branch_name,
                )
            else:
                provider_result = self.provider.create_pull_request(
                    repository_slug=repository_slug,
                    repository=lease.repository,
                    commit=receipt["commit"],
                    branch_name=branch_name,
                    base_branch="main",
                    title="Glug maintenance proposal",
                )
            provider_succeeded = True
            _reject_sensitive(_mapping(provider_result, "provider result"))
            publication = {
                "contract": "glug.mushy-review-publication.v1",
                "workspace": adoption.workspace_id,
                "power": power,
                "repository": repository_slug,
                "commit": receipt["commit"],
                "branch": branch_name,
                "approval_id": approval_id,
                "provider_result": dict(provider_result),
            }
            _reject_sensitive(publication)
        except Exception:
            if provider_succeeded:
                self.workspaces.finish_publication(lease)
            else:
                self.workspaces.release_publication(
                    lease, now=_utc(self.clock()))
            raise
        self.workspaces.finish_publication(lease)
        return publication

    def _repository_slug(self, adoption: glug_adoption.GlugAdoption) -> str:
        slug = self.env.get(adoption.repository_slug_env, "").strip()
        if not glug_adoption.REPO_SLUG.fullmatch(slug):
            raise GlugExecutorError(
                "repository_unavailable", "Glug repository is not server-bound", 503)
        return slug

    def _verify_claim(
        self,
        claim: Mapping[str, Any],
        *,
        adoption: glug_adoption.GlugAdoption,
        actor_id: str,
        requested_power: str,
        now: dt.datetime,
    ) -> None:
        _exact(claim, CLAIM_FIELDS, "claim")
        signature = _text(claim["signature"], "claim signature")
        if not SHA256.fullmatch(signature):
            raise GlugExecutorError("claim_invalid", "Claim authentication is invalid", 403)
        unsigned = {key: claim[key] for key in CLAIM_FIELDS if key != "signature"}
        if not hmac.compare_digest(signature, self._sign("claim", unsigned)):
            raise GlugExecutorError("claim_invalid", "Claim authentication is invalid", 403)
        if claim["workspace"] != adoption.workspace_id:
            raise GlugExecutorError("claim_invalid", "Claim workspace is invalid", 403)
        if claim["power"] != requested_power:
            raise GlugExecutorError("claim_invalid", "Claim power is invalid", 403)
        if not hmac.compare_digest(
            _text(claim["actor_digest"], "claim actor digest"),
            self._actor_digest(actor_id),
        ):
            raise GlugExecutorError("claim_invalid", "Claim actor is invalid", 403)
        _safe_id(claim["id"], "claim id")
        base_commit = _text(claim["base_commit"], "claim base_commit")
        if not SHA40.fullmatch(base_commit):
            raise GlugExecutorError("claim_invalid", "Claim base is invalid", 403)
        issued = _instant(claim["issued_at"], "claim issued_at")
        expires = _instant(claim["expires_at"], "claim expires_at")
        current = _utc(now)
        if expires <= issued or expires - issued > dt.timedelta(seconds=300):
            raise GlugExecutorError("claim_invalid", "Claim lifetime is invalid", 403)
        if current < issued or current >= expires:
            raise GlugExecutorError("claim_stale", "stale claim", 409)

    def _actor_digest(self, actor_id: str) -> str:
        if not isinstance(actor_id, str) or not actor_id.strip() or len(actor_id) > 512:
            raise GlugExecutorError("actor_invalid", "Authenticated actor is invalid", 403)
        return hmac.new(
            self._signing_key(), b"glug-actor-v1\0" + actor_id.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def _sign(self, purpose: str, value: Mapping[str, Any]) -> str:
        canonical = json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        ).encode("utf-8")
        return hmac.new(
            self._signing_key(),
            purpose.encode("ascii") + b"\0" + canonical,
            hashlib.sha256,
        ).hexdigest()

    def _signing_key(self) -> bytes:
        return _signing_key_from_env(self.env)

    @staticmethod
    def _require_safe_state(state: Mapping[str, Any]) -> None:
        if state["clean"] is not True:
            raise GlugExecutorError("unsafe_repository", "dirty clone", 409)
        for key in ("linked_refs", "submodules", "symlinks"):
            if state[key]:
                raise GlugExecutorError("unsafe_repository", key + " are not allowed", 409)

    def _repository_state(
        self,
        adoption: glug_adoption.GlugAdoption,
        repository: Path,
        *,
        expected_head: str | None = None,
    ) -> Mapping[str, Any]:
        root = Path(repository)
        git_dir = root / ".git"
        if _is_link(root) or not root.is_dir() or _is_link(git_dir) or not git_dir.is_dir():
            raise GlugExecutorError(
                "standalone_clone_required", "A standalone clone is required", 409)
        linked_refs: list[str] = []
        for relative in ("commondir", "objects/info/alternates"):
            if (git_dir / relative).exists():
                linked_refs.append(relative)
        shown_root = self._git_text(root, "rev-parse", "--show-toplevel")
        try:
            same_root = Path(shown_root).resolve() == root.resolve()
        except OSError:
            same_root = False
        if not same_root:
            raise GlugExecutorError("standalone_clone_required", "Git root does not match", 409)
        head = self._git_text(root, "rev-parse", "HEAD")
        if not SHA40.fullmatch(head):
            raise GlugExecutorError("git_identity_invalid", "Git HEAD is invalid", 409)
        if expected_head is not None and head != expected_head:
            raise GlugExecutorError("base_drift", "Repository HEAD drifted", 409)
        self._require_ancestor(
            root,
            adoption.ranglr_base_commit, head,
            code="lineage_invalid", message="Repository HEAD is not a Ranglr descendant")
        status = self._git_bytes(
            root, "status", "--porcelain=v1", "--untracked-files=all")
        replacements = self._git_text(
            root, "replace", "-l", allow_empty=True).splitlines()
        linked_refs.extend(item for item in replacements if item)
        index_lines = self._git_text(
            root, "ls-files", "--stage", allow_empty=True).splitlines()
        submodules = _index_paths(index_lines, "160000")
        symlinks = _index_paths(index_lines, "120000")
        return {
            "head_commit": head,
            "clean": status == b"",
            "linked_refs": sorted(set(linked_refs)),
            "submodules": sorted(set(submodules)),
            "symlinks": sorted(set(symlinks)),
        }

    def _require_ancestor(
        self, repository: Path, ancestor: str, descendant: str, *, code: str, message: str
    ) -> None:
        try:
            merge_base = self._git_text(repository, "merge-base", ancestor, descendant)
        except GlugExecutorError as exc:
            raise GlugExecutorError(code, message, 409) from exc
        if merge_base != ancestor:
            raise GlugExecutorError(code, message, 409)

    def _git_receipt(
        self,
        *,
        adoption: glug_adoption.GlugAdoption,
        repository: Path,
        power: str,
        claim_id: Any,
        before: Mapping[str, Any],
        after: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        if after["clean"] is not True:
            raise GlugExecutorError("dirty_result", "Author left a dirty clone", 409)
        if after["linked_refs"] or after["submodules"] or after["symlinks"]:
            raise GlugExecutorError("unsafe_result", "Author produced an unsafe repository", 409)
        commit = after["head_commit"]
        self._require_ancestor(
            repository,
            before["head_commit"], commit,
            code="base_drift", message="Author result does not descend from the claimed base")
        tree = self._git_text(repository, "rev-parse", f"{commit}^{{tree}}")
        if not SHA40.fullmatch(tree):
            raise GlugExecutorError("git_identity_invalid", "Git tree is invalid", 409)
        range_value = f"{before['head_commit']}..{commit}"
        names_raw = self._git_bytes(
            repository,
            "diff", "--name-only", "-z", "--diff-filter=ACMRTUXB", range_value)
        changed_files = [
            value.decode("utf-8", errors="strict")
            for value in names_raw.split(b"\0") if value
        ]
        if changed_files != sorted(changed_files) or len(set(changed_files)) != len(changed_files):
            raise GlugExecutorError("git_diff_invalid", "Changed files are not canonical", 409)
        if any(path.startswith("/") or ".." in Path(path).parts or "\\" in path
               for path in changed_files):
            raise GlugExecutorError("git_diff_invalid", "Changed path is unsafe", 409)
        diff = self._git_bytes(
            repository,
            "diff", "--binary", "--full-index", "--no-ext-diff", range_value)
        limits = _limits(adoption)
        if len(changed_files) > limits["max_changed_files"]:
            raise GlugExecutorError("file_limit", "Changed-file limit exceeded", 409)
        if len(diff) > limits["max_diff_bytes"]:
            raise GlugExecutorError("diff_limit", "Diff-byte limit exceeded", 409)
        if power == "stage_change":
            if commit == before["head_commit"] or not changed_files or not diff:
                raise GlugExecutorError("empty_change", "Stage change produced no Git change", 409)
        elif commit != before["head_commit"] or changed_files or diff:
            raise GlugExecutorError("read_only_mutation", "Read-only power changed Git", 409)
        unsigned = {
            "contract": "glug.mushy-stage-receipt.v1",
            "workspace": adoption.workspace_id,
            "repository": self._repository_slug(adoption),
            "requested_power": power,
            "base_commit": before["head_commit"],
            "commit": commit,
            "tree": tree,
            "changed_files": changed_files,
            "diff_bytes": len(diff),
            "diff_sha256": hashlib.sha256(diff).hexdigest(),
            "claim_id": _safe_id(claim_id, "claim id"),
            "mushy_pin": glug_adoption.client_pin_receipt(adoption),
            "limits": limits,
        }
        receipt = {**unsigned, "signature": self._sign("stage-receipt", unsigned)}
        _reject_sensitive(receipt)
        return receipt

    def _validated_stage_receipt(
        self, adoption: glug_adoption.GlugAdoption, value: Any
    ) -> Mapping[str, Any]:
        receipt = _mapping(value, "stage receipt")
        _exact(receipt, STAGE_RECEIPT_FIELDS, "stage receipt")
        signature = _text(receipt["signature"], "stage receipt signature")
        if not SHA256.fullmatch(signature):
            raise GlugExecutorError("receipt_invalid", "Stage receipt authentication is invalid")
        unsigned = {key: receipt[key] for key in STAGE_RECEIPT_FIELDS if key != "signature"}
        if not hmac.compare_digest(signature, self._sign("stage-receipt", unsigned)):
            raise GlugExecutorError("receipt_invalid", "Stage receipt authentication is invalid")
        if receipt["contract"] != "glug.mushy-stage-receipt.v1":
            raise GlugExecutorError("receipt_invalid", "Stage receipt contract is invalid")
        if receipt["workspace"] != adoption.workspace_id:
            raise GlugExecutorError("receipt_invalid", "Stage receipt workspace is invalid")
        if receipt["repository"] != self._repository_slug(adoption):
            raise GlugExecutorError("receipt_invalid", "Stage receipt repository is invalid")
        if receipt["requested_power"] != "stage_change":
            raise GlugExecutorError("receipt_invalid", "Only a staged change can publish")
        for field in ("base_commit", "commit", "tree"):
            if not isinstance(receipt[field], str) or not SHA40.fullmatch(receipt[field]):
                raise GlugExecutorError("receipt_invalid", "Stage receipt Git identity is invalid")
        if not isinstance(receipt["changed_files"], list) or not receipt["changed_files"]:
            raise GlugExecutorError("receipt_invalid", "Stage receipt has no changed files")
        limits = _mapping(receipt["limits"], "stage receipt limits")
        _exact(limits, LIMIT_FIELDS, "stage receipt limits")
        if dict(limits) != _limits(adoption):
            raise GlugExecutorError("receipt_invalid", "Stage receipt limits drifted")
        if not isinstance(receipt["diff_bytes"], int) or receipt["diff_bytes"] <= 0:
            raise GlugExecutorError("receipt_invalid", "Stage receipt diff size is invalid")
        if not isinstance(receipt["diff_sha256"], str) or not SHA256.fullmatch(receipt["diff_sha256"]):
            raise GlugExecutorError("receipt_invalid", "Stage receipt diff digest is invalid")
        if receipt["mushy_pin"] != glug_adoption.client_pin_receipt(adoption):
            raise GlugExecutorError("receipt_invalid", "Stage receipt Mushy pin drifted")
        _reject_sensitive(receipt)
        return dict(receipt)

    def _author_environment(self, adoption: glug_adoption.GlugAdoption) -> Mapping[str, str]:
        result = {
            key: value for key, value in self.env.items()
            if key.upper() in SAFE_AUTHOR_ENV_KEYS and isinstance(value, str)
        }
        result.update({
            "CI": "true",
            "GIT_TERMINAL_PROMPT": "0",
            "GCM_INTERACTIVE": "never",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GLUG_MUSHY_SOURCE_COMMIT": adoption.mushy_source_commit,
        })
        for key in result:
            if SECRET_KEY.search(key):
                raise GlugExecutorError("author_environment_invalid", "Author environment is unsafe", 500)
        return result

    def _git_bytes(self, repository: Path, *args: str) -> bytes:
        return self.commands.run(
            ("git", *args), cwd=repository, timeout_seconds=30,
            env=self._git_environment())

    def _git_text(
        self, repository: Path, *args: str, allow_empty: bool = False
    ) -> str:
        try:
            value = self._git_bytes(
                repository, *args).decode("utf-8", errors="strict").strip()
        except UnicodeDecodeError as exc:
            raise GlugExecutorError("git_output_invalid", "Git output is invalid", 409) from exc
        if not value and not allow_empty:
            raise GlugExecutorError("git_output_invalid", "Git output is empty", 409)
        return value

    def _git_environment(self) -> Mapping[str, str]:
        result = {
            key: value for key, value in self.env.items()
            if key.upper() in SAFE_AUTHOR_ENV_KEYS and isinstance(value, str)
        }
        result.update({
            "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never",
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "core.longpaths",
            "GIT_CONFIG_VALUE_0": "true",
        })
        return result


def _index_paths(lines: Sequence[str], mode: str) -> list[str]:
    result: list[str] = []
    for line in lines:
        if not line.startswith(mode + " ") or "\t" not in line:
            continue
        result.append(line.split("\t", 1)[1])
    return result


def _limits(adoption: glug_adoption.GlugAdoption) -> dict[str, int]:
    limits = adoption.raw["limits"]
    return {key: int(limits[key]) for key in LIMIT_FIELDS}


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise GlugExecutorError("input_invalid", label + " must be an object")
    return value


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise GlugExecutorError("input_invalid", label + " must be text")
    return value


def _safe_id(value: Any, label: str) -> str:
    text = _text(value, label)
    if not SAFE_ID.fullmatch(text):
        raise GlugExecutorError("input_invalid", label + " is invalid")
    return text


def _signing_key_from_env(env: Mapping[str, str]) -> bytes:
    value = env.get("GLUG_MUSHY_CLAIM_SIGNING_SECRET")
    if not isinstance(value, str) or value != value.strip() or len(value) < 32:
        raise GlugExecutorError(
            "claim_signing_unavailable", "Glug claim signing is not configured", 503)
    return value.encode("utf-8")


def _instant(value: Any, label: str) -> dt.datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise GlugExecutorError("claim_invalid", label + " is invalid", 403)
    try:
        parsed = dt.datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise GlugExecutorError("claim_invalid", label + " is invalid", 403) from exc
    return parsed


def _epoch(value: dt.datetime) -> float:
    return _utc(value).timestamp()


def _is_link(path: Path) -> bool:
    try:
        if path.is_symlink():
            return True
        is_junction = getattr(path, "is_junction", None)
        return bool(is_junction()) if callable(is_junction) else False
    except OSError:
        return True


def _rmtree_remove_readonly(function: Callable[..., Any], path: str, _exc: Any) -> None:
    os.chmod(path, os.stat(path, follow_symlinks=False).st_mode | stat.S_IWRITE)
    function(path)


def _path_has_link_component(path: Path) -> bool:
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        if _is_link(current):
            return True
    return False


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _utc(value: dt.datetime) -> dt.datetime:
    if not isinstance(value, dt.datetime) or value.tzinfo is None:
        raise GlugExecutorError("clock_invalid", "Executor clock is invalid", 500)
    return value.astimezone(dt.timezone.utc)


def _format_instant(value: dt.datetime) -> str:
    return _utc(value).isoformat(timespec="seconds").replace("+00:00", "Z")


def _exact(value: Mapping[str, Any], fields: frozenset[str], label: str) -> None:
    if set(value) != set(fields):
        raise GlugExecutorError("input_invalid", label + " fields are invalid")


def _reject_sensitive(value: Mapping[str, Any]) -> None:
    encoded = json.dumps(value, sort_keys=True)
    for key in _keys(value):
        if SECRET_KEY.search(key):
            raise GlugExecutorError("receipt_unsafe", "Output contains a secret-shaped key", 500)
    if SECRET_VALUE.search(encoded) or LOCAL_PATH.search(encoded):
        raise GlugExecutorError("receipt_unsafe", "Output contains sensitive content", 500)


def _keys(value: Any):
    if isinstance(value, Mapping):
        for key, child in value.items():
            yield str(key)
            yield from _keys(child)
    elif isinstance(value, list):
        for child in value:
            yield from _keys(child)
