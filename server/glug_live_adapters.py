"""Live, server-owned adapters for the Glug Mushy maintenance rail."""
from __future__ import annotations

import base64
import datetime as dt
import hashlib
import json
import os
import re
import secrets
import sqlite3
import subprocess
import threading
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Mapping

import glug_adoption
from glug_executor import GlugExecutorError


SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,199}$")
SHA40 = re.compile(r"^[0-9a-f]{40}$")
AUTHOR_ENDPOINT_PATH = "/internal/glug/mushy/author"


def _required(env: Mapping[str, str], key: str) -> str:
    value = env.get(key, "").strip()
    if not value:
        raise GlugExecutorError("executor_unavailable", f"{key} is not configured", 503)
    return value


class HarnessAuthorAdapter:
    """Calls the isolated harness that owns the model grant and pinned runtime."""

    MAX_RESULT_BYTES = 64 * 1024

    def __init__(
        self,
        *,
        artifact_root: Path | str,
        entrypoint: glug_adoption.ArtifactFile,
        endpoint: str,
        harness_secret: str,
        workspace_root: Path | str,
        opener=None,
    ):
        root = Path(artifact_root)
        if root.is_symlink() or not root.is_dir():
            raise GlugExecutorError("executor_unavailable", "Glug author artifact is unavailable", 503)
        self.artifact_root = root.resolve()
        self.entrypoint = entrypoint
        self.endpoint = self._validated_endpoint(endpoint)
        if not isinstance(harness_secret, str) or not harness_secret:
            raise GlugExecutorError(
                "executor_unavailable", "Glug author harness authentication is unavailable", 503)
        self.harness_secret = harness_secret
        workspace = Path(workspace_root)
        if workspace.is_symlink() or not workspace.is_dir():
            raise GlugExecutorError(
                "executor_unavailable", "Glug author workspace root is unavailable", 503)
        self.workspace_root = workspace.resolve()
        self._opener = opener or urllib.request.urlopen
        self._verified_entrypoint(root)

    @classmethod
    def configured(
        cls,
        adoption: glug_adoption.GlugAdoption,
        artifact_root: Path | str,
        *,
        env: Mapping[str, str],
    ) -> "HarnessAuthorAdapter":
        entrypoint = next(
            (entry for entry in adoption.artifact_files
             if entry.path == adoption.artifact_entrypoint),
            None,
        )
        if entrypoint is None:
            raise GlugExecutorError(
                "executor_unavailable", "Glug author entrypoint is not pinned", 503)
        return cls(
            artifact_root=artifact_root,
            entrypoint=entrypoint,
            endpoint=_required(env, "GLUG_MUSHY_AUTHOR_URL"),
            harness_secret=_required(env, "LEAF_HARNESS_SECRET"),
            workspace_root=_required(env, "GLUG_MUSHY_WORKSPACE_ROOT"),
        )

    def run(
        self,
        payload: Mapping[str, Any],
        *,
        repository: Path,
        artifact_root: Path,
        author_timeout_seconds: int,
        wrapper_timeout_seconds: int,
        env: Mapping[str, str],
    ) -> Mapping[str, Any]:
        self._verified_entrypoint(Path(artifact_root))
        self._verify_workspace_binding(payload, repository)
        source_commit = env.get("GLUG_MUSHY_SOURCE_COMMIT", "")
        if not SHA40.fullmatch(source_commit):
            raise GlugExecutorError(
                "author_unavailable", "Glug Mushy source identity is unavailable", 503)
        body = json.dumps(
            dict(payload), separators=(",", ":"), ensure_ascii=True,
        ).encode("utf-8")
        request = urllib.request.Request(
            self.endpoint,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "X-Harness-Secret": self.harness_secret,
                "X-Glug-Mushy-Source-Commit": source_commit,
                "X-Glug-Mushy-Author-Timeout-Seconds": str(author_timeout_seconds),
            },
        )
        response = None
        try:
            response = self._opener(request, timeout=wrapper_timeout_seconds)
            raw_status = getattr(response, "status", None)
            status = int(raw_status if raw_status is not None else response.getcode())
            encoded = response.read(self.MAX_RESULT_BYTES + 1)
        except urllib.error.HTTPError as exc:
            code = "author_failed" if 400 <= exc.code < 500 else "author_unavailable"
            status = 502 if code == "author_failed" else 503
            exc.close()
            raise GlugExecutorError(code, "Glug author harness refused the request", status) from exc
        except (OSError, TimeoutError, urllib.error.URLError) as exc:
            raise GlugExecutorError(
                "author_unavailable", "Glug author harness is unavailable", 503,
            ) from exc
        finally:
            if response is not None:
                response.close()
        if status < 200 or status >= 300:
            raise GlugExecutorError(
                "author_unavailable", "Glug author harness returned an invalid status", 503)
        if len(encoded) > self.MAX_RESULT_BYTES:
            raise GlugExecutorError(
                "author_result_invalid", "Glug author result is too large", 502)
        try:
            result = json.loads(encoded.decode("utf-8", errors="strict"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise GlugExecutorError("author_result_invalid", "Glug author result is invalid", 502) from exc
        if not isinstance(result, dict):
            raise GlugExecutorError("author_result_invalid", "Glug author result is invalid", 502)
        return result

    @staticmethod
    def _validated_endpoint(value: str) -> str:
        try:
            parsed = urllib.parse.urlsplit(value)
        except ValueError as exc:
            raise GlugExecutorError(
                "executor_unavailable", "Glug author harness URL is invalid", 503,
            ) from exc
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.path != AUTHOR_ENDPOINT_PATH
        ):
            raise GlugExecutorError(
                "executor_unavailable", "Glug author harness URL is invalid", 503)
        return value

    def _verify_workspace_binding(
        self, payload: Mapping[str, Any], repository: Path,
    ) -> None:
        claim_id = payload.get("claim_id")
        if not isinstance(claim_id, str) or not re.fullmatch(
            r"[a-z0-9][a-z0-9._-]{0,199}", claim_id,
        ):
            raise GlugExecutorError("author_failed", "Glug author claim is invalid", 502)
        expected = (
            self.workspace_root
            / hashlib.sha256(claim_id.encode("utf-8")).hexdigest()
            / "repository"
        )
        try:
            actual = Path(repository).resolve(strict=True)
            expected_resolved = expected.resolve(strict=True)
        except OSError as exc:
            raise GlugExecutorError(
                "author_unavailable", "Glug author workspace is unavailable", 503,
            ) from exc
        if actual != expected_resolved:
            raise GlugExecutorError(
                "author_failed", "Glug author workspace binding is invalid", 502)

    def _verified_entrypoint(self, artifact_root: Path) -> Path:
        if artifact_root.is_symlink() or not artifact_root.is_dir():
            raise GlugExecutorError("author_unavailable", "Glug author artifact is unavailable", 503)
        resolved_root = artifact_root.resolve()
        if resolved_root != self.artifact_root:
            raise GlugExecutorError("author_unavailable", "Glug author artifact changed", 503)
        candidate = artifact_root / self.entrypoint.path
        if candidate.is_symlink() or not candidate.is_file():
            raise GlugExecutorError("author_unavailable", "Glug author entrypoint is unavailable", 503)
        resolved = candidate.resolve()
        try:
            resolved.relative_to(resolved_root)
        except ValueError as exc:
            raise GlugExecutorError("author_unavailable", "Glug author entrypoint escaped its artifact", 503) from exc
        try:
            payload = resolved.read_bytes()
        except OSError as exc:
            raise GlugExecutorError("author_unavailable", "Glug author entrypoint is unavailable", 503) from exc
        if (
            len(payload) != self.entrypoint.bytes
            or hashlib.sha256(payload).hexdigest() != self.entrypoint.sha256
        ):
            raise GlugExecutorError("author_unavailable", "Glug author entrypoint digest drifted", 503)
        return resolved


class SQLiteApprovalStore:
    """Durable exact-subject approvals consumed atomically by publication."""

    def __init__(self, path: Path | str, *, clock=None, id_factory=None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.clock = clock or (lambda: dt.datetime.now(dt.timezone.utc))
        self.id_factory = id_factory or (lambda: "approval-" + secrets.token_hex(16))
        self._lock = threading.Lock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.path), timeout=10, isolation_level=None)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("""
                CREATE TABLE IF NOT EXISTS glug_mushy_approvals (
                    id TEXT PRIMARY KEY, idempotency_key TEXT NOT NULL,
                    actor_digest TEXT NOT NULL, origin_job_id TEXT NOT NULL,
                    repository TEXT NOT NULL, commit_sha TEXT NOT NULL,
                    power TEXT NOT NULL, issued_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL, consumed_at TEXT,
                    UNIQUE(actor_digest, idempotency_key)
                )
            """)

    def issue(
        self, *, actor_digest: str, origin_job_id: str, repository: str,
        commit: str, power: str, idempotency_key: str, expires_at: str,
    ) -> Mapping[str, Any]:
        if not SHA40.fullmatch(commit) or not all(
            SAFE_ID.fullmatch(value) for value in
            (actor_digest, origin_job_id, repository, power, idempotency_key)
        ):
            raise GlugExecutorError("approval_invalid", "Approval subject is invalid", 422)
        approval_id = self.id_factory()
        issued_at = _instant(self.clock()).isoformat().replace("+00:00", "Z")
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            prior = connection.execute(
                "SELECT * FROM glug_mushy_approvals WHERE actor_digest=? AND idempotency_key=?",
                (actor_digest, idempotency_key),
            ).fetchone()
            if prior is None:
                connection.execute(
                    "INSERT INTO glug_mushy_approvals VALUES (?,?,?,?,?,?,?,?,?,NULL)",
                    (approval_id, idempotency_key, actor_digest, origin_job_id,
                     repository, commit, power, issued_at, expires_at),
                )
                prior = connection.execute(
                    "SELECT * FROM glug_mushy_approvals WHERE id=?", (approval_id,)
                ).fetchone()
            elif any(prior[key] != value for key, value in {
                "origin_job_id": origin_job_id, "repository": repository,
                "commit_sha": commit, "power": power,
            }.items()):
                connection.execute("ROLLBACK")
                raise GlugExecutorError("idempotency_conflict", "Approval idempotency key was reused", 409)
            connection.execute("COMMIT")
        return self._public(prior)

    def verify(
        self, *, approval_id: str, actor_id: str, power: str,
        repository_slug: str, commit: str,
    ) -> Mapping[str, Any] | None:
        actor_digest = actor_id if re.fullmatch(r"[0-9a-f]{64}", actor_id) else _digest(actor_id)
        now = _instant(self.clock())
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM glug_mushy_approvals WHERE id=?", (approval_id,)
            ).fetchone()
            if row is None or row["consumed_at"] is not None:
                connection.execute("ROLLBACK")
                return None
            if (
                row["actor_digest"] != actor_digest or row["power"] != power
                or row["repository"] != repository_slug or row["commit_sha"] != commit
                or _parse(row["expires_at"]) <= now
            ):
                connection.execute("ROLLBACK")
                return None
            consumed_at = now.isoformat().replace("+00:00", "Z")
            changed = connection.execute(
                "UPDATE glug_mushy_approvals SET consumed_at=? WHERE id=? AND consumed_at IS NULL",
                (consumed_at, approval_id),
            ).rowcount
            connection.execute("COMMIT")
        return {"approved": changed == 1, "approval_id": approval_id}

    @staticmethod
    def _public(row: sqlite3.Row) -> Mapping[str, Any]:
        return {
            "contract": "glug.mushy-publication-approval.v1", "id": row["id"],
            "origin_job_id": row["origin_job_id"], "repository": row["repository"],
            "commit": row["commit_sha"], "power": row["power"],
            "issued_at": row["issued_at"], "expires_at": row["expires_at"],
            "consumed": row["consumed_at"] is not None,
        }


class GitHubReviewProvider:
    """Pushes one exact commit and optionally creates a PR. It cannot merge."""

    def __init__(self, *, token: str, repository_slug: str, api_root: str = "https://api.github.com"):
        if not token or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository_slug):
            raise GlugExecutorError("executor_unavailable", "GitHub review provider is invalid", 503)
        self.token = token
        self.repository_slug = repository_slug
        self.api_root = api_root.rstrip("/")

    @classmethod
    def configured(cls, env: Mapping[str, str]) -> "GitHubReviewProvider":
        return cls(
            token=_required(env, "GLUG_GITHUB_REVIEW_TOKEN"),
            repository_slug=_required(env, "GLUG_REPOSITORY_SLUG"),
            api_root=env.get("GLUG_GITHUB_API_ROOT", "https://api.github.com"),
        )

    def create_review_branch(self, *, repository_slug: str, repository: Path,
                             commit: str, branch_name: str) -> Mapping[str, Any]:
        self._require_subject(repository_slug, commit, branch_name)
        self._push(repository, commit, branch_name)
        return {"branch": branch_name, "commit": commit}

    def create_pull_request(self, *, repository_slug: str, repository: Path,
                            commit: str, branch_name: str, base_branch: str,
                            title: str) -> Mapping[str, Any]:
        branch = self.create_review_branch(
            repository_slug=repository_slug, repository=repository,
            commit=commit, branch_name=branch_name)
        payload = json.dumps({
            "title": title, "head": branch_name, "base": base_branch,
            "body": "Mushy-authored Glug maintenance proposal. Human review required.",
        }).encode("utf-8")
        request = urllib.request.Request(
            f"{self.api_root}/repos/{self.repository_slug}/pulls", data=payload,
            method="POST", headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/vnd.github+json",
                "Content-Type": "application/json",
                "User-Agent": "glug-mushy-control-plane",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                value = json.loads(response.read().decode("utf-8"))
        except (OSError, urllib.error.HTTPError, json.JSONDecodeError) as exc:
            raise GlugExecutorError("provider_unavailable", "GitHub review publication failed", 503) from exc
        if not isinstance(value, dict) or not isinstance(value.get("number"), int):
            raise GlugExecutorError("provider_invalid", "GitHub review response is invalid", 502)
        return {**branch, "number": value["number"], "url": value.get("html_url", "")}

    def _push(self, repository: Path, commit: str, branch_name: str) -> None:
        credential = base64.b64encode(f"x-access-token:{self.token}".encode()).decode()
        env = {
            "PATH": os.environ.get("PATH", ""), "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),
            "GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
            "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {credential}",
        }
        remote = f"https://github.com/{self.repository_slug}.git"
        completed = subprocess.run(
            ["git", "push", "--porcelain", "--", remote,
             f"{commit}:refs/heads/{branch_name}"],
            cwd=str(repository), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            check=False, env=env, shell=False, timeout=120,
        )
        if completed.returncode != 0:
            raise GlugExecutorError("provider_unavailable", "GitHub review branch push failed", 503)

    def _require_subject(self, repository_slug: str, commit: str, branch_name: str) -> None:
        if repository_slug != self.repository_slug or not SHA40.fullmatch(commit):
            raise GlugExecutorError("provider_subject_invalid", "GitHub publication subject is invalid", 409)
        if not re.fullmatch(r"glug/mushy/[0-9a-f]{12}", branch_name):
            raise GlugExecutorError("provider_subject_invalid", "GitHub branch is invalid", 409)


def configured_live_components(
    env: Mapping[str, str], *, adoption: glug_adoption.GlugAdoption,
    artifact_root: Path | str,
):
    database = Path(_required(env, "GLUG_MUSHY_JOB_DATABASE"))
    return (
        HarnessAuthorAdapter.configured(adoption, artifact_root, env=env),
        SQLiteApprovalStore(database),
        GitHubReviewProvider.configured(env),
    )


def _digest(value: str) -> str:
    import hashlib
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _instant(value: dt.datetime) -> dt.datetime:
    if value.tzinfo is None:
        raise GlugExecutorError("approval_invalid", "Approval time is invalid", 500)
    return value.astimezone(dt.timezone.utc)


def _parse(value: str) -> dt.datetime:
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(dt.timezone.utc)
    except ValueError as exc:
        raise GlugExecutorError("approval_invalid", "Approval time is invalid", 500) from exc
