"""W14 admin self-edit lane (R7): BRANCH-ONLY proposals against the platform repo.

This module is the trusted spine for `customize_platform`. An admin session
proposes a change to the PLATFORM repository itself (the repo this server is
built from — not a tenant tool repo), and the proposal materializes as exactly
one commit on exactly one branch under ``refs/heads/admin-customize/``. Nothing
in this module can advance ``main``/``master`` or any ref outside that prefix:
every ref write and every push goes through ``_assert_branch_only``, the one
chokepoint, and pushes never use ``--force``.

Landing is a HANDOFF, not a deploy: ``land()`` (optionally) pushes the branch
and returns a receipt naming the standing pipeline the branch must ride —
branch -> PR -> sol-critic review gate -> merge -> ECS staging canary -> prod,
rollback = previous ECS task-definition revision (docs/ADMIN-SELF-EDIT-LANE.md).
The lane never merges, never touches ECS, and never bypasses the PR gate.

FUNDAMENTAL PATHS (auth, billing, agent spine — see
``platform_fundamental_paths.json``) require the out-of-band co-sign before the
branch may land: an approver presents ``LEAF_CUSTOMIZATION_APPROVAL_SECRET``
(the same independent-approval credential the R6 lane uses; the authoring
harness never holds it) and the co-sign binds to the exact commit sha. A
missing manifest fails closed by treating EVERY path as fundamental; a present
but unreadable manifest refuses service (503) rather than guessing.

Change records are one-JSON-file-per-change under the state dir (the approvals
-dir idiom: O_EXCL create, atomic replace) so the lane needs no database and
survives restarts.
"""
from __future__ import annotations

import hmac
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
import unicodedata
from pathlib import Path
from typing import Any, Mapping, Optional
from uuid import uuid4

_LOG = logging.getLogger(__name__)

SERVER_DIR = Path(__file__).resolve().parent
DEFAULT_FUNDAMENTAL_FILE = SERVER_DIR / "platform_fundamental_paths.json"

BRANCH_PREFIX = "refs/heads/admin-customize/"
_CHANGE_ID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")

MAX_EDITS = 200
MAX_EDIT_BYTES = 1_000_000  # per file
MAX_TITLE = 200

# States a change record moves through. Terminal-ish: landed / denied.
AWAITING_COSIGN = "awaiting_cosign"
APPROVED = "approved"
DENIED = "denied"
LANDED = "landed"


class PlatformCustomizeError(RuntimeError):
    """Opaque, client-safe reason code + status; detail is operator-only."""

    def __init__(self, code: str, status_code: int = 409, detail: str = "") -> None:
        super().__init__(code)
        self.code, self.status_code = code, status_code
        self.detail = str(detail)


# --------------------------------------------------------------------------- #
# configuration (read at call time, like every other policy seam here)
# --------------------------------------------------------------------------- #
def repo_dir() -> Path:
    raw = os.environ.get("LEAF_PLATFORM_REPO_DIR", "").strip()
    if not raw:
        raise PlatformCustomizeError(
            "platform_repo_unavailable", 503, "LEAF_PLATFORM_REPO_DIR unset")
    path = Path(raw)
    git_dir = path / ".git"
    if git_dir.is_dir():
        return git_dir
    if (path / "HEAD").is_file():  # bare repo / git dir
        return path
    raise PlatformCustomizeError(
        "platform_repo_unavailable", 503, f"not_a_git_dir: {path}")


def base_ref() -> str:
    return os.environ.get("LEAF_PLATFORM_REPO_BASE_REF", "").strip() or "refs/heads/main"


def state_dir() -> Path:
    raw = os.environ.get("LEAF_PLATFORM_CUSTOMIZE_STATE_DIR", "").strip()
    root = Path(raw) if raw else SERVER_DIR / "platform_customize_state"
    root.mkdir(parents=True, exist_ok=True)
    return root


def push_enabled() -> bool:
    return os.environ.get("LEAF_PLATFORM_REPO_PUSH", "").strip().lower() in {
        "1", "true", "yes", "on"}


_USERINFO_RE = re.compile(r"://[^/@\s]+@")


def push_remote() -> str:
    """The push remote NAME or URL — refusing credential-bearing URLs.

    A ``https://user:token@host/...`` remote would put the token into git's
    argv and stderr, both of which feed operator-log detail on failure. Push
    credentials belong in a credential helper or the remote's stored config,
    never in this env var.
    """
    remote = os.environ.get("LEAF_PLATFORM_REPO_REMOTE", "").strip() or "origin"
    if _USERINFO_RE.search(remote):
        raise PlatformCustomizeError(
            "push_remote_invalid", 503,
            "LEAF_PLATFORM_REPO_REMOTE carries URL userinfo; use a credential "
            "helper or a named remote instead")
    return remote


def _redact(text: str) -> str:
    """Scrub URL userinfo out of anything destined for operator detail/logs."""
    return _USERINFO_RE.sub("://[redacted]@", text)


def fundamental_file() -> Path:
    raw = os.environ.get("LEAF_PLATFORM_FUNDAMENTAL_PATHS_FILE", "").strip()
    return Path(raw) if raw else DEFAULT_FUNDAMENTAL_FILE


# --------------------------------------------------------------------------- #
# the branch-only chokepoint
# --------------------------------------------------------------------------- #
def branch_ref_for(change_id: str) -> str:
    if not _CHANGE_ID_RE.fullmatch(str(change_id)):
        raise PlatformCustomizeError("change_id_invalid", 422)
    return BRANCH_PREFIX + str(change_id)


def _assert_branch_only(ref: str) -> str:
    """The ONE gate every ref write and push refspec passes through.

    Full-ref form only (``refs/heads/admin-customize/<uuid>``): a short name
    could alias a tag or be rewritten by push refspec defaults. Anything else —
    main, master, tags, notes, other branches — is refused with the same code,
    and there is deliberately no override.
    """
    if not isinstance(ref, str) or not ref.startswith(BRANCH_PREFIX):
        raise PlatformCustomizeError(
            "protected_ref_refused", 403, f"ref_outside_lane: {ref!r}")
    tail = ref[len(BRANCH_PREFIX):]
    if not _CHANGE_ID_RE.fullmatch(tail):
        raise PlatformCustomizeError(
            "protected_ref_refused", 403, f"ref_tail_invalid: {ref!r}")
    return ref


# --------------------------------------------------------------------------- #
# git helpers (customization_service idioms: safe.directory, timeouts, stderr
# into operator-only detail)
# --------------------------------------------------------------------------- #
def _git_trust(*paths: Path) -> list[str]:
    flags: list[str] = []
    for path in paths:
        try:
            resolved = str(path.resolve(strict=False))
        except OSError:
            resolved = str(path)
        flags.extend(("-c", f"safe.directory={resolved}"))
    return flags


def _run_git(cmd: list[str], *, cwd: Optional[Path], where: str,
             timeout: int, env_extra: Optional[Mapping[str, str]]) -> str:
    env = dict(os.environ)
    if env_extra:
        env.update(env_extra)
    try:
        result = subprocess.run(
            cmd, check=True, text=True, env=env,
            cwd=str(cwd) if cwd is not None else None,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        stderr = str(getattr(exc, "stderr", "") or "").strip()
        raise PlatformCustomizeError(
            "platform_repo_unavailable", 503,
            _redact(f"git_failed: {' '.join(cmd[-3:])} in {where} "
                    f"({type(exc).__name__})"
                    + (f": {stderr}" if stderr else "")),
        ) from exc
    return result.stdout.strip()


def _git(git_dir: Path, *args: str, timeout: int = 30,
         env_extra: Optional[Mapping[str, str]] = None) -> str:
    """Run against the platform repo's git dir (ref reads/writes, worktree
    admin, push). Never used for add/commit — those must run INSIDE the linked
    worktree (below) so only its detached HEAD can move."""
    cmd = ["git", *_git_trust(git_dir), "--git-dir", str(git_dir), *args]
    return _run_git(cmd, cwd=None, where=str(git_dir), timeout=timeout,
                    env_extra=env_extra)


def _git_wt(git_dir: Path, worktree: Path, *args: str, timeout: int = 60,
            env_extra: Optional[Mapping[str, str]] = None) -> str:
    """Run INSIDE a linked worktree (cwd resolution, no --git-dir override).

    A linked worktree has its own private git dir and its own detached HEAD;
    forcing ``--git-dir <main>`` + ``--work-tree`` here would make ``commit``
    move the MAIN repo's HEAD — for this lane that could be ``main`` itself.
    """
    cmd = ["git", *_git_trust(git_dir, worktree), *args]
    return _run_git(cmd, cwd=worktree, where=str(worktree), timeout=timeout,
                    env_extra=env_extra)


# --------------------------------------------------------------------------- #
# path validation (this repo has uppercase filenames, so the tenant-workspace
# lowercase rule does not apply; everything else is the same discipline)
# --------------------------------------------------------------------------- #
def _validated_repo_path(raw: Any) -> str:
    if not isinstance(raw, str) or not raw:
        raise PlatformCustomizeError("edit_path_invalid", 422, f"empty: {raw!r}")
    if "\\" in raw or unicodedata.normalize("NFC", raw) != raw:
        raise PlatformCustomizeError("edit_path_invalid", 422, f"encoding: {raw!r}")
    if raw.startswith("/") or re.match(r"^[A-Za-z]:", raw):
        raise PlatformCustomizeError("edit_path_invalid", 422, f"absolute: {raw!r}")
    segments = raw.split("/")
    if any(not seg for seg in segments):
        raise PlatformCustomizeError("edit_path_invalid", 422, f"empty_segment: {raw!r}")
    if any(seg in {".", ".."} for seg in segments):
        raise PlatformCustomizeError("edit_path_invalid", 422, f"traversal: {raw!r}")
    if any(seg == ".git" or seg.lower() == ".git" for seg in segments):
        raise PlatformCustomizeError("edit_path_invalid", 422, f"git_dir: {raw!r}")
    return raw


def _reject_symlink_escape(root: Path, relative: str) -> None:
    """No write may traverse a symlink inside the fresh worktree."""
    current = root
    for segment in relative.split("/")[:-1]:
        current = current / segment
        if current.is_symlink():
            raise PlatformCustomizeError(
                "edit_path_invalid", 422, f"symlink_traversal: {relative}")
    final = root / relative
    if final.is_symlink():
        raise PlatformCustomizeError(
            "edit_path_invalid", 422, f"symlink_target: {relative}")


# --------------------------------------------------------------------------- #
# fundamental-path classification (co-sign scope)
# --------------------------------------------------------------------------- #
def _load_fundamental_patterns() -> Optional[list[str]]:
    """The co-sign pattern list. ``None`` means the manifest is ABSENT, which
    callers must treat as "everything is fundamental" (fail closed without
    bricking the lane). Present-but-unreadable refuses service instead — a
    corrupt manifest must never silently widen the no-co-sign set."""
    path = fundamental_file()
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise PlatformCustomizeError(
            "fundamental_manifest_unavailable", 503, f"unreadable: {path}: {exc}")
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise PlatformCustomizeError(
            "fundamental_manifest_unavailable", 503, f"invalid_json: {path}: {exc}")
    if not isinstance(raw, dict) or raw.get("version") != 1:
        raise PlatformCustomizeError(
            "fundamental_manifest_unavailable", 503, f"bad_shape: {path}")
    areas = raw.get("areas")
    if not isinstance(areas, dict) or not areas:
        raise PlatformCustomizeError(
            "fundamental_manifest_unavailable", 503, f"areas_missing: {path}")
    patterns: list[str] = []
    for area, entries in areas.items():
        if not isinstance(entries, list) or not all(
                isinstance(p, str) and p for p in entries):
            raise PlatformCustomizeError(
                "fundamental_manifest_unavailable", 503, f"area_invalid: {area}")
        patterns.extend(entries)
    return patterns


def _fold(path: str) -> str:
    """Case-fold + NFC for CLASSIFICATION ONLY (never for writing).

    Windows and macOS checkouts resolve ``Server/entitlements.py`` to the same
    file as ``server/entitlements.py``, so a case-variant spelling must
    classify as the protected path, not slip past it. Folding can only WIDEN
    the fundamental set — the fail-closed direction.
    """
    return unicodedata.normalize("NFC", path).casefold()


def _matches(pattern: str, path: str) -> bool:
    pattern, path = _fold(pattern), _fold(path)
    return path.startswith(pattern[:-2]) if pattern.endswith("/**") else path == pattern


def classify_fundamental(paths: list[str]) -> list[str]:
    """Return the subset of ``paths`` requiring co-sign, in input order."""
    patterns = _load_fundamental_patterns()
    if patterns is None:
        return list(paths)  # absent manifest: everything is fundamental
    return [p for p in paths if any(_matches(pat, p) for pat in patterns)]


# --------------------------------------------------------------------------- #
# durable change records (approvals-dir idiom: file-per-record, atomic writes)
# --------------------------------------------------------------------------- #
def _record_path(change_id: str) -> Path:
    if not _CHANGE_ID_RE.fullmatch(str(change_id)):
        raise PlatformCustomizeError("change_id_invalid", 422)
    return state_dir() / f"{change_id}.json"


def _write_record(record: dict[str, Any], *, create: bool = False) -> None:
    path = _record_path(record["change_id"])
    payload = json.dumps(record, sort_keys=True, indent=1).encode("utf-8")
    if create:
        try:
            fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL)
        except FileExistsError:
            raise PlatformCustomizeError("change_conflict", 409)
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
        return
    tmp = path.with_suffix(f".tmp-{uuid4().hex[:8]}")
    tmp.write_bytes(payload)
    os.replace(tmp, path)


def load_record(change_id: str) -> dict[str, Any]:
    path = _record_path(change_id)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise PlatformCustomizeError("change_not_found", 404)
    except (OSError, json.JSONDecodeError) as exc:
        raise PlatformCustomizeError(
            "change_record_unavailable", 503, f"{path}: {exc}")
    if not isinstance(raw, dict):
        raise PlatformCustomizeError("change_record_unavailable", 503, f"{path}: not a dict")
    return raw


# --------------------------------------------------------------------------- #
# the lane
# --------------------------------------------------------------------------- #
def propose(*, tenant_id: str, subject: str, title: str,
            edits: list[Mapping[str, Any]]) -> dict[str, Any]:
    """Create ONE commit on ONE new lane branch from the base ref tip.

    Never touches HEAD, never advances any existing ref, never pushes. The
    returned record is the durable authority for everything after (co-sign,
    land); the branch ref in the local repo is derived state.
    """
    title = str(title or "").strip()
    if not title or len(title) > MAX_TITLE:
        raise PlatformCustomizeError("title_invalid", 422)
    if not isinstance(edits, list) or not edits or len(edits) > MAX_EDITS:
        raise PlatformCustomizeError("edits_invalid", 422)

    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for edit in edits:
        if not isinstance(edit, Mapping):
            raise PlatformCustomizeError("edits_invalid", 422)
        unknown = set(edit) - {"path", "content", "delete"}
        if unknown:
            raise PlatformCustomizeError("edits_invalid", 422, f"unknown: {unknown}")
        path = _validated_repo_path(edit.get("path"))
        if path in seen:
            raise PlatformCustomizeError("edits_invalid", 422, f"duplicate: {path}")
        seen.add(path)
        delete = edit.get("delete", False)
        if delete is not False and delete is not True:
            raise PlatformCustomizeError("edits_invalid", 422, f"delete_not_bool: {path}")
        content = edit.get("content")
        if delete:
            if content is not None:
                raise PlatformCustomizeError("edits_invalid", 422, f"delete_with_content: {path}")
        else:
            if not isinstance(content, str):
                raise PlatformCustomizeError("edits_invalid", 422, f"content_missing: {path}")
            if len(content.encode("utf-8")) > MAX_EDIT_BYTES:
                raise PlatformCustomizeError("edits_invalid", 422, f"content_too_large: {path}")
        normalized.append({"path": path, "delete": bool(delete),
                           "content": None if delete else content})

    fundamental = classify_fundamental([e["path"] for e in normalized])

    git_dir = repo_dir()
    base_sha = _git(git_dir, "rev-parse", "--verify", base_ref() + "^{commit}")
    if not _SHA_RE.fullmatch(base_sha):
        raise PlatformCustomizeError(
            "platform_repo_unavailable", 503, f"base_unresolvable: {base_sha!r}")

    change_id = str(uuid4())
    ref = _assert_branch_only(branch_ref_for(change_id))

    worktree = Path(tempfile.mkdtemp(prefix=f"pc-{change_id[:8]}-"))
    try:
        _git(git_dir, "worktree", "add", "--detach", str(worktree), base_sha,
             timeout=120)
        for edit in normalized:
            _reject_symlink_escape(worktree, edit["path"])
            target = worktree / edit["path"]
            if edit["delete"]:
                if not target.is_file():
                    raise PlatformCustomizeError(
                        "edits_invalid", 422, f"delete_missing: {edit['path']}")
                target.unlink()
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(edit["content"], encoding="utf-8", newline="\n")
        _git_wt(git_dir, worktree, "add", "-A")
        status = _git_wt(git_dir, worktree, "status", "--porcelain")
        if not status.strip():
            raise PlatformCustomizeError("edits_noop", 422, "no diff against base")
        author_env = {
            "GIT_AUTHOR_NAME": "Leaf admin self-edit lane",
            "GIT_AUTHOR_EMAIL": "platform-customize@leafdesign.ai",
            "GIT_COMMITTER_NAME": "Leaf admin self-edit lane",
            "GIT_COMMITTER_EMAIL": "platform-customize@leafdesign.ai",
        }
        message = (
            f"admin-customize: {title}\n\n"
            f"Change-Id: {change_id}\n"
            f"Proposed-By-Tenant: {tenant_id}\n"
            f"Proposed-By-Subject: {subject}\n"
            f"Fundamental-Paths: {len(fundamental)}\n"
        )
        # --no-verify: a machine commit inside the app container must not
        # execute repo-configured hooks in this process; review happens at the
        # PR gate the branch is handed to, not here.
        _git_wt(git_dir, worktree, "commit", "--no-verify", "-m", message,
                env_extra=author_env)
        commit_sha = _git_wt(git_dir, worktree, "rev-parse", "HEAD")
        if not _SHA_RE.fullmatch(commit_sha):
            raise PlatformCustomizeError(
                "platform_repo_unavailable", 503, f"commit_unresolvable: {commit_sha!r}")
        # The worktree's HEAD is detached; publish the commit ONLY at the lane
        # ref, created empty (the old-value guard makes an overwrite a failure).
        _assert_branch_only(ref)
        _git(git_dir, "update-ref", ref, commit_sha,
             "0000000000000000000000000000000000000000")
    finally:
        try:
            _git(git_dir, "worktree", "remove", "--force", str(worktree), timeout=60)
        except PlatformCustomizeError:
            shutil.rmtree(worktree, ignore_errors=True)
            try:
                _git(git_dir, "worktree", "prune", timeout=30)
            except PlatformCustomizeError:
                pass

    record = {
        "contract": "leaf.platform-customize.v1",
        "change_id": change_id,
        "tenant_id": str(tenant_id),
        "author_subject": str(subject),
        "title": title,
        "branch_ref": ref,
        "base_sha": base_sha,
        "commit_sha": commit_sha,
        "paths": [e["path"] for e in normalized],
        "fundamental_paths": fundamental,
        "state": AWAITING_COSIGN if fundamental else APPROVED,
        "cosign": None,
        "push": None,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    _write_record(record, create=True)
    return public_view(record)


def _approval_secret() -> str:
    return os.environ.get("LEAF_CUSTOMIZATION_APPROVAL_SECRET", "").strip()


def verify_approval_secret(presented: Optional[str]) -> bool:
    secret = _approval_secret()
    if not secret or not presented:
        return False
    try:
        return hmac.compare_digest(presented.encode("ascii"), secret.encode("ascii"))
    except UnicodeEncodeError:
        return False


def _marker_path(change_id: str, kind: str) -> Path:
    if not _CHANGE_ID_RE.fullmatch(str(change_id)):
        raise PlatformCustomizeError("change_id_invalid", 422)
    return state_dir() / f"{change_id}.{kind}.json"


def _claim_marker(change_id: str, kind: str, payload: dict[str, Any]) -> bool:
    """Atomically claim a one-shot transition (O_EXCL). False = already taken.

    The marker, not the record, is the transition authority: two racing
    writers both read the same record state, but only ONE can create the
    marker file, so approve-vs-deny and double-land races collapse to a
    single winner regardless of record rewrite ordering.
    """
    try:
        fd = os.open(str(_marker_path(change_id, kind)),
                     os.O_WRONLY | os.O_CREAT | os.O_EXCL)
    except FileExistsError:
        return False
    with os.fdopen(fd, "wb") as handle:
        handle.write(json.dumps(payload, sort_keys=True).encode("utf-8"))
    return True


def _read_marker(change_id: str, kind: str) -> Optional[dict[str, Any]]:
    try:
        raw = json.loads(_marker_path(change_id, kind).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as exc:
        raise PlatformCustomizeError(
            "change_record_unavailable", 503, f"marker {kind}: {exc}")
    return raw if isinstance(raw, dict) else None


def cosign(*, change_id: str, approver_subject: str, commit_sha: str,
           approve: bool) -> dict[str, Any]:
    """Record the out-of-band co-sign verdict, bound to the exact commit.

    The transport (router) has ALREADY verified the approval secret; this
    function enforces the bindings: the record must be awaiting co-sign, the
    presented commit sha must equal the staged one (approving "the change"
    without naming its bytes is not an approval), and the approver must not be
    the proposing subject. The verdict itself is claimed via a one-shot O_EXCL
    marker, so concurrent approve/deny requests resolve to exactly one winner.

    TRUST NOTE: possession of the approval secret IS the co-sign authority
    (the operator-held credential the harness and admin sessions never see —
    same boundary as the R6 lane). ``approver_subject`` is the secret-holder's
    ATTESTED audit label, not an authenticated identity; the self-approval
    check is hygiene against honest mistakes, not a boundary against a
    secret-holder who lies.
    """
    record = load_record(change_id)
    if record.get("state") != AWAITING_COSIGN:
        raise PlatformCustomizeError("cosign_not_pending", 409)
    if not isinstance(commit_sha, str) or commit_sha != record.get("commit_sha"):
        raise PlatformCustomizeError("cosign_commit_mismatch", 409)
    approver = str(approver_subject or "").strip()
    if not approver:
        raise PlatformCustomizeError("cosign_approver_missing", 422)
    if approver == record.get("author_subject"):
        raise PlatformCustomizeError("cosign_self_approval", 403)
    verdict = {
        "approver_subject": approver,
        "approver_attestation": "approval-secret-holder",
        "commit_sha": commit_sha,
        "verdict": "approved" if approve else "denied",
        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    if not _claim_marker(change_id, "cosign", verdict):
        raise PlatformCustomizeError("cosign_not_pending", 409)
    record["cosign"] = verdict
    record["state"] = APPROVED if approve else DENIED
    _write_record(record)
    return public_view(record)


def land(*, change_id: str, tenant_id: str, ack_commit_sha: str) -> dict[str, Any]:
    """Hand the approved change to the standing pipeline (optionally pushing).

    ``ack_commit_sha`` is the fresh per-invocation approval on this API lane:
    the caller must name the EXACT commit they are landing (mirroring the
    catalog's always-confirm posture — a bare "land it" that names no bytes is
    not an approval). The push sources the RECORDED COMMIT SHA, never the
    mutable branch ref, so a ref moved between verification and push cannot
    smuggle different bytes (the branch-diverged check stays as an early
    honest error). Push never forces; the landed transition is a one-shot
    O_EXCL marker, so concurrent lands collapse to one push; landing is
    idempotent per record afterward.
    """
    record = load_record(change_id)
    if record.get("tenant_id") != str(tenant_id):
        raise PlatformCustomizeError("change_not_found", 404)
    if record.get("state") == LANDED:
        return public_view(record)
    if record.get("state") != APPROVED:
        code = ("cosign_required" if record.get("state") == AWAITING_COSIGN
                else "change_not_landable")
        raise PlatformCustomizeError(code, 409)
    commit_sha = str(record.get("commit_sha", ""))
    if not isinstance(ack_commit_sha, str) or ack_commit_sha != commit_sha:
        raise PlatformCustomizeError("land_ack_mismatch", 409)
    # Fundamental changes must ALSO show the durable approved verdict marker —
    # the record alone could have been rewritten by a stale writer.
    if record.get("fundamental_paths"):
        marker = _read_marker(change_id, "cosign")
        if not marker or marker.get("verdict") != "approved" \
                or marker.get("commit_sha") != commit_sha:
            raise PlatformCustomizeError("cosign_required", 409)

    ref = _assert_branch_only(str(record.get("branch_ref", "")))
    git_dir = repo_dir()
    observed = _git(git_dir, "rev-parse", "--verify", ref + "^{commit}")
    if observed != commit_sha:
        raise PlatformCustomizeError(
            "branch_diverged", 409,
            f"ref={ref} observed={observed} recorded={commit_sha}")

    # DELIVER, then mark. A pre-push marker would wedge the change if the push
    # failed (marker consumed, nothing delivered). SHA-pinned pushes of the
    # same commit are idempotent ("everything up-to-date"), so a rare double
    # push is harmless; the marker exists to serialize the RECORD transition.
    pushed = False
    if push_enabled():
        # SHA-pinned refspec: the pushed bytes are exactly the recorded
        # commit, regardless of where the local ref points by now.
        _git(git_dir, "push", push_remote(), f"{commit_sha}:{ref}", timeout=120)
        pushed = True

    if not _claim_marker(change_id, "landed", {
            "commit_sha": commit_sha,
            "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}):
        return public_view(load_record(change_id))

    record["push"] = {"pushed": pushed, "remote": push_remote() if pushed else None,
                      "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    record["state"] = LANDED
    _write_record(record)
    return public_view(record)


def public_view(record: Mapping[str, Any]) -> dict[str, Any]:
    """The client-visible projection + the landing-path contract."""
    branch = str(record.get("branch_ref", ""))
    short = branch[len("refs/heads/"):] if branch.startswith("refs/heads/") else branch
    return {
        "contract": "leaf.platform-customize.v1",
        "change_id": record.get("change_id"),
        "title": record.get("title"),
        "state": record.get("state"),
        "branch": short,
        "base_sha": record.get("base_sha"),
        "commit_sha": record.get("commit_sha"),
        "paths": list(record.get("paths") or []),
        "fundamental_paths": list(record.get("fundamental_paths") or []),
        "cosign": record.get("cosign"),
        "push": record.get("push"),
        "landing_path": {
            "pipeline": ["branch", "pull-request", "sol-critic review gate",
                         "merge", "ECS staging canary", "production"],
            "rollback": "previous ECS task-definition revision",
            "writes": "branch-only; this lane never merges or deploys",
        },
    }
