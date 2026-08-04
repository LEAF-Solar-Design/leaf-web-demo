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
The lane never touches ECS and never bypasses the review gate.
When configured (``LEAF_PLATFORM_PR_OPEN`` + a PR-scoped token), landing also
opens the pull request automatically and status reads observe the review
verdict (leaf-web-demo#422 Phases 1-2) — best-effort and recorded, never
load-bearing. Merging exists ONLY as ``merge()`` (#422 Phase 3): its own kill
switch and credential, a fresh operator approval naming the exact commit, and
a twice-executed same-tree guard; it is not on the harness back-edge, so the
drawer is the only door.

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

# The complete character set an edit path may use. Verified against every one
# of the repo's tracked files (984 at 2026-08-03): zero fall outside it.
_PATH_ALLOWED_RE = re.compile(r"[A-Za-z0-9._/+@-]+")

MAX_EDITS = 200
MAX_EDIT_BYTES = 1_000_000  # per file
MAX_TITLE = 200

# States a change record moves through. Terminal-ish: landed / denied.
AWAITING_COSIGN = "awaiting_cosign"
APPROVED = "approved"
DENIED = "denied"
LANDED = "landed"
MERGED = "merged"


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


def pr_open_enabled() -> bool:
    """Phase 1 of the operator-gated follow-through (leaf-web-demo#422):
    after a successful push, open the pull request automatically. OFF by
    default; opening a PR merges nothing and the review gate is untouched
    (merging is Phase 3's separately-gated, separately-credentialed step)."""
    return os.environ.get("LEAF_PLATFORM_PR_OPEN", "").strip().lower() in {
        "1", "true", "yes", "on"}


_REPO_SLUG_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")

# Printable ASCII, no spaces: anything wider (whitespace, control bytes) is not
# a GitHub token and would make urllib quote the full header value — token
# included — into a ValueError.
_PR_TOKEN_RE = re.compile(r"^[\x21-\x7E]+$")


def _github_api_root() -> str:
    return (os.environ.get("LEAF_PLATFORM_GITHUB_API", "").strip()
            or "https://api.github.com")


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
    # --no-replace-objects on EVERY lane invocation. refs/replace/* are applied
    # by default to almost all commands including diff and ls-tree, so a
    # replacement for the base commit could present a tree that hides an
    # unapproved side effect: the binding diff would report only approved
    # paths while the PUBLISHED commit differs from the real base — and git
    # excludes replacement refs from pack transfer, so the remote sees the
    # unapproved change. The oracle must read real objects (sol-critic #423 r6).
    flags: list[str] = ["--no-replace-objects"]
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
    # Win32 STRIPS trailing dots and spaces from every path component, so
    # `server./agent_gate.py` and `server /agent_gate.py` name the same file as
    # `server/agent_gate.py` on a Windows checkout while spelling differently
    # here — and a different spelling is a different classification. That is a
    # co-sign bypass: the alias reports fundamental_paths=[] and the change
    # goes APPROVED instead of AWAITING_COSIGN (sol-critic PR #417 round 3).
    # No legitimate path in this repo ends a component in a dot or a space
    # (git cannot even check such a name out on Windows), so REJECT rather
    # than normalize: rejecting cannot be replayed into an accepted alias.
    if any(seg != seg.rstrip(". ") for seg in segments):
        raise PlatformCustomizeError("edit_path_invalid", 422, f"win32_alias: {raw!r}")
    # A path is only a control if a human can READ it on the approval chip, and
    # can tell two different paths apart. Denylisting the ways to break that
    # loses: controls collapse a row, bidi marks reorder what the eye sees,
    # zero-width and filler characters (U+200B, U+FEFF, U+00AD, U+034F Mn,
    # U+3164 Lo) are invisible, and homoglyphs read as the wrong file — and
    # they span half the Unicode category table, so each denied class leaves
    # the next (sol-critic PR #417 rounds 3-4). ALLOWLIST instead: every one
    # of this repo's 984 tracked paths uses only these characters, so nothing
    # legitimate is refused and every confusable spelling is.
    if _PATH_ALLOWED_RE.fullmatch(raw) is None:
        raise PlatformCustomizeError("edit_path_invalid", 422, "charset")
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
    """Case-fold + NFC + Win32 component canonicalization, for CLASSIFICATION
    ONLY (never for writing).

    Windows and macOS checkouts resolve ``Server/entitlements.py`` to the same
    file as ``server/entitlements.py``, so a case-variant spelling must
    classify as the protected path, not slip past it. Win32 additionally
    strips trailing dots and spaces from each component, making
    ``server./entitlements.py`` another alias of the same file — so those are
    stripped here too. Folding can only WIDEN the fundamental set — the
    fail-closed direction — which is why this stays a second barrier behind
    ``_validated_repo_path``'s outright rejection of such aliases: a caller
    that ever reaches classification without validation still classifies the
    protected path correctly.
    """
    folded = unicodedata.normalize("NFC", path).casefold()
    return "/".join(seg.rstrip(". ") or seg for seg in folded.split("/"))


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
        expected_blobs: dict[str, str] = {}
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
                # Capture the approved bytes' object id NOW, before `git add`
                # can run a clean filter over them. Hashing after the add is
                # CIRCULAR: a filter (possibly introduced by a .gitattributes
                # edit in this very request) rewrites the file, git stages the
                # rewritten bytes, and a later read hashes those same rewritten
                # bytes — want == got while the landed content is not what was
                # approved (sol-critic PR #423 round 3). git's own hasher, so
                # the repo's object format is respected.
                expected_blobs[edit["path"]] = _git_wt(
                    git_dir, worktree, "hash-object", "--no-filters",
                    "--", edit["path"]).strip()
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

        # THE COMMIT MUST BE EXACTLY WHAT WAS APPROVED — PATHS AND BYTES.
        #
        # The approval binds the EDIT SET, so anything the commit holds that
        # the operator did not approve, or any approved byte the commit does
        # not hold, means the landed change is not the reviewed one. Two
        # concrete ways that happened (sol-critic PR #423 rounds 1-2):
        #
        #   * `git add -A` SILENTLY skips .gitignore'd paths while the record
        #     still listed them, so a mixed request committed only part of an
        #     atomic set and reported APPROVED.
        #   * a content filter rewrites bytes on the way in. With
        #     core.autocrlf=true a requested CRLF body commits as LF
        #     (measured: requested 4e349b59..., committed 814f4a42...), so a
        #     path-only check passes while the BYTES differ. A clean filter —
        #     including one introduced by a .gitattributes edit in the SAME
        #     request — does the same, and its side effects can dirty extra
        #     paths that `add -A` then stages.
        #
        # So: compare both directions on paths, and compare the committed blob
        # against the exact requested bytes. Fail closed, before any durable
        # record, marker, or lane ref exists.
        committed = {
            # --no-renames is LOAD-BEARING: rename detection collapses a
            # delete+add into the destination alone, so a hidden deletion of a
            # fundamental path could ride along beside an approved addition
            # with identical bytes and never appear here — landing a
            # fundamental deletion with no co-sign (sol-critic PR #423 r4).
            line for line in _git_wt(
                # --ignore-submodules=none likewise: diff.ignoreSubmodules=all
                # (repo config or .gitmodules) HIDES gitlink changes from the
                # diff family, so an unapproved staged gitlink would be absent
                # from `committed` and the set equality would pass. The
                # command-line option overrides both (sol-critic PR #423 r5).
                git_dir, worktree, "diff", "--no-renames",
                "--ignore-submodules=none", "--name-only",
                base_sha, commit_sha,
            ).split("\n") if line.strip()
        }
        requested = {e["path"] for e in normalized}
        dropped = sorted(requested - committed)
        if dropped:
            raise PlatformCustomizeError(
                "edits_not_committed", 422,
                f"git refused to stage (ignored or excluded): {dropped}")
        unexpected = sorted(committed - requested)
        if unexpected:
            raise PlatformCustomizeError(
                "edits_not_committed", 422,
                f"commit carries unapproved paths: {unexpected}")
        for edit in normalized:
            path = edit["path"]
            entry = _git_wt(
                git_dir, worktree, "ls-tree", commit_sha, "--", path).strip()
            if edit.get("delete"):
                # A delete must actually be absent from the commit's tree.
                if entry:
                    raise PlatformCustomizeError(
                        "edits_not_committed", 422, f"delete_not_applied: {path}")
                continue
            if not entry:
                raise PlatformCustomizeError(
                    "edits_not_committed", 422, f"missing_from_tree: {path}")
            # "<mode> <type> <oid>\t<path>". MODE and TYPE are recorded
            # separately from content, so bytes alone do not pin the entry:
            # 100755 (executable), 120000 (symlink) and 160000 (gitlink) are
            # distinct entries a side effect could produce while the blob id
            # still matches. A proposal writes a plain file; anything else is
            # not what was approved (sol-critic PR #423 round 3).
            meta = entry.partition("\t")[0].split()
            if len(meta) < 3:
                raise PlatformCustomizeError(
                    "edits_not_committed", 422, f"unreadable_tree_entry: {path}")
            mode, obj_type, oid = meta[0], meta[1], meta[2]
            # A plain file, and the SAME kind of plain file it already was.
            # Symlink (120000) and gitlink (160000) are never a proposal's
            # output. An existing executable must keep 100755 — requiring a
            # flat 100644 would refuse a legitimate edit to it (sol-critic
            # PR #423 r4); a NEW file must be 100644.
            base_entry = _git_wt(
                git_dir, worktree, "ls-tree", base_sha, "--", path).strip()
            base_mode = (base_entry.partition("\t")[0].split() or [""])[0]
            want_mode = base_mode if base_mode in {"100644", "100755"} else "100644"
            if mode != want_mode or obj_type != "blob":
                raise PlatformCustomizeError(
                    "edits_not_committed", 422,
                    f"unexpected tree entry {mode} {obj_type} "
                    f"(expected {want_mode} blob): {path}")
            # expected_blobs was captured BEFORE `git add`, so a clean filter
            # cannot make the oracle agree with its own rewrite.
            want = expected_blobs.get(path, "")
            if not want or oid != want:
                raise PlatformCustomizeError(
                    "edits_not_committed", 422,
                    f"committed bytes differ from the approved edit "
                    f"(filter or normalization): {path}")

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
        "base_ref": base_ref(),
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
    """Atomically claim a one-shot transition. False = already taken.

    The marker, not the record, is the transition authority: two racing
    writers both read the same record state, but only ONE can claim the
    marker, so approve-vs-deny and double-land races collapse to a single
    winner regardless of record rewrite ordering.

    CRASH-SAFE: the payload is written and fsynced to a PRIVATE temp file
    first, then published under the final name via ``os.link`` — an atomic
    claim that either exposes the complete payload or nothing. A bare O_EXCL
    open of the final path would expose an empty marker between open and
    write, and a crash there would wedge the transition forever.
    """
    final = _marker_path(change_id, kind)
    tmp = final.with_suffix(f".tmp-{uuid4().hex[:8]}")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(json.dumps(payload, sort_keys=True).encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(str(tmp), str(final))
        except FileExistsError:
            return False
        return True
    finally:
        try:
            os.unlink(str(tmp))
        except OSError:
            pass


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
    record = _reconcile(change_id, record)
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
        # Lost the race (or a prior crash already claimed it): reconcile the
        # record from the durable marker so nothing stays wedged, then report
        # the transition as already settled.
        _reconcile(change_id, load_record(change_id))
        raise PlatformCustomizeError("cosign_not_pending", 409)
    record["cosign"] = verdict
    record["state"] = APPROVED if approve else DENIED
    _write_record(record)
    return public_view(record)


def _reconcile(change_id: str, record: dict[str, Any]) -> dict[str, Any]:
    """Re-derive the record's state from the durable markers (self-healing).

    Markers are the transition authority; the record file is a projection a
    crash can leave stale (verdict claimed but record still awaiting, or land
    marker claimed but record still approved). Every read path funnels through
    here so a wedged projection heals on the next touch instead of refusing
    forever. Marker bindings are checked against the record's commit before
    they are believed.
    """
    commit_sha = record.get("commit_sha")
    changed = False
    cosign_marker = _read_marker(change_id, "cosign")
    if (cosign_marker and cosign_marker.get("commit_sha") == commit_sha
            and record.get("state") == AWAITING_COSIGN):
        record["cosign"] = cosign_marker
        record["state"] = (APPROVED if cosign_marker.get("verdict") == "approved"
                           else DENIED)
        changed = True
    landed_marker = _read_marker(change_id, "landed")
    if (landed_marker and landed_marker.get("commit_sha") == commit_sha
            and record.get("state") == APPROVED
            and _cosign_satisfied(change_id, record)):
        # Heal ONLY the state land() can legitimately crash out of: the
        # record was APPROVED when the marker was claimed (land() requires
        # approved before claiming, and for fundamental changes the durable
        # cosign marker must say approved). A landed marker seen against
        # awaiting/denied can only be forged or misplaced — reconciling it
        # would let a marker write skip co-sign, branch verification, and
        # push, so it is deliberately IGNORED (the record stays authoritative
        # and the anomaly is logged for the operator).
        # push-then-mark ordering: within the legitimate window the marker
        # PROVES delivery completed (or push was disabled), so completing the
        # projection is honest, not optimistic.
        record.setdefault("push", None)
        if record["push"] is None:
            record["push"] = {"pushed": push_enabled(),
                              "remote": push_remote() if push_enabled() else None,
                              "at": landed_marker.get("at"), "healed": True}
        record["state"] = LANDED
        changed = True
    elif landed_marker and record.get("state") not in (LANDED, APPROVED):
        _LOG.warning(
            "platform_customize: landed marker present against state=%s for "
            "change %s — ignored (illegitimate heal window)",
            record.get("state"), change_id)
    merged_marker = _read_marker(change_id, "merged")
    if (merged_marker and merged_marker.get("commit_sha") == commit_sha
            and record.get("state") == LANDED):
        # The only state merge() can legitimately crash out of: the sha-pinned
        # PUT succeeded (marker proves delivery) but the record write was
        # lost. A merged marker against any other state is forged or
        # misplaced and is deliberately IGNORED — it must never stand in for
        # the land/co-sign/review gates it skipped.
        record["merge"] = {
            "merged": True, "commit_sha": commit_sha,
            "merge_commit_sha": merged_marker.get("merge_commit_sha"),
            "approved_by_subject": None, "branch_deleted": False,
            "at": merged_marker.get("at"), "healed": True,
        }
        record["state"] = MERGED
        changed = True
    elif merged_marker and record.get("state") not in (MERGED, LANDED):
        _LOG.warning(
            "platform_customize: merged marker present against state=%s for "
            "change %s — ignored (illegitimate heal window)",
            record.get("state"), change_id)
    if changed:
        _write_record(record)
    return record


def _cosign_satisfied(change_id: str, record: dict[str, Any]) -> bool:
    """True when the change needs no co-sign, or the DURABLE marker approves
    this exact commit. Used by the heal path so a forged landed marker cannot
    stand in for the co-sign it never had."""
    if not record.get("fundamental_paths"):
        return True
    marker = _read_marker(change_id, "cosign")
    return bool(marker and marker.get("verdict") == "approved"
                and marker.get("commit_sha") == record.get("commit_sha"))


def propose_and_land(*, tenant_id: str, subject: str, title: str,
                     edits: list[Mapping[str, Any]]) -> dict[str, Any]:
    """Propose and, when the change needs no co-sign, land it in ONE approval.

    WHY THIS EXISTS: the two-approval shape cost two LLM turn boundaries and two
    human decisions around ~4s of real work (measured on staging: propose
    2014ms, land 2242ms, a git rev-parse on the EFS clone 11ms). The second
    approval bought nothing the first did not already authorise.

    WHAT THE APPROVAL BINDS. ``land()`` normally demands ``ack_commit_sha`` so a
    bare "land it" that names no bytes is never an approval. Here the operator
    approved the EXACT EDIT SET instead, and the commit is a pure function of
    those edits applied to the base tip, so the sha is derived rather than
    round-tripped through a second human answer. This is not weaker: the
    two-step shape leaves a WINDOW between propose and land in which the branch
    could move, which is precisely why ``land()`` carries a branch-diverged
    check; this path closes the window instead of policing it. Every one of
    land()'s server-side guards still runs, unchanged and in order — state must
    be APPROVED, the durable co-sign marker must exist for fundamental paths,
    the observed ref must equal the recorded commit, and the push is SHA-pinned.

    WHAT IT MUST NEVER DO: land a change that touches a fundamental path. Those
    return AWAITING_COSIGN from propose() and this function then RETURNS that
    view untouched, so the independent co-signer is exactly as required as
    before. One approval replaces the operator's second click, never the second
    PERSON.
    """
    view = propose(tenant_id=tenant_id, subject=subject, title=title, edits=edits)
    # Anything other than a clean self-approval stops here and is reported as
    # proposed. AWAITING_COSIGN is the fundamental-path case; any other state is
    # equally not ours to land.
    if view.get("state") != APPROVED:
        return view
    return land(change_id=view["change_id"], tenant_id=tenant_id,
                ack_commit_sha=str(view["commit_sha"]))


# --------------------------------------------------------------------------- #
# PR auto-open on land (issue #422 Phase 1) — best-effort, NEVER load-bearing.
# The lane's write authority is unchanged: branch-only pushes with the
# Contents-scoped token. Opening the PR uses a SEPARATE, PR-scoped token so
# each capability revokes independently. Every failure here is recorded on the
# change record and the land still succeeds; the idempotent land replay (same
# exact-commit ack) is the retry path.
# --------------------------------------------------------------------------- #
def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _github_request(method: str, path: str, *, token: str,
                    payload: Optional[dict[str, Any]] = None,
                    timeout: int = 15) -> tuple[int, Any]:
    """One GitHub API call. Returns (status, parsed-json-or-None).

    The token travels ONLY in the Authorization header — never in the URL, so
    it can never reach ``_redact``-scrubbed detail via argv/stderr the way a
    userinfo URL could. HTTP errors return their status rather than raising so
    the caller can decide; transport errors raise and the caller records a
    generic code.
    """
    import urllib.error
    import urllib.request

    body = None
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        _github_api_root() + path, data=body, method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "leaf-platform-customize",
            **({"Content-Type": "application/json"} if body else {}),
        })
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
            status = response.status
    except urllib.error.HTTPError as exc:  # non-2xx still carries a body
        raw = exc.read()
        status = exc.code
    try:
        data = json.loads(raw.decode("utf-8")) if raw else None
    except (UnicodeDecodeError, json.JSONDecodeError):
        data = None
    return status, data


def _pr_body(record: Mapping[str, Any]) -> str:
    fundamental = list(record.get("fundamental_paths") or [])
    cosign = record.get("cosign") or {}
    lines = [
        f"Opened automatically by the R7 self-edit lane on Land "
        f"(leaf-web-demo#422 Phase 1).",
        "",
        f"- Change-Id: `{record.get('change_id')}`",
        f"- Commit: `{record.get('commit_sha')}`",
        f"- Proposed-By-Subject: `{record.get('author_subject')}`",
        f"- Paths: " + ", ".join(f"`{p}`" for p in (record.get("paths") or [])),
    ]
    if fundamental:
        lines.append(
            f"- Fundamental paths ({len(fundamental)}): co-signed by "
            f"`{cosign.get('approver_subject', 'UNKNOWN')}` at "
            f"{cosign.get('at', 'UNKNOWN')}")
    lines += [
        "",
        "The lane pushes branches only — this PR rides the standing review "
        "pipeline (sol-critic gate) like every other change; merging stays "
        "gated there.",
    ]
    return "\n".join(lines)


def _pr_view(data: Mapping[str, Any], *, slug: str,
             reused: bool) -> Optional[dict[str, Any]]:
    """Validate the fields we persist — a misconfigured API root must not be
    able to plant junk in the record the drawer renders as a link.

    Strict by construction (sol-critic PR #424 round 1, finding 2):
    ``type(number) is int`` because ``bool`` subclasses ``int`` and ``True``
    must not read as PR #1; and the URL is not merely https — it must be
    EXACTLY the canonical GitHub address this repo+number implies, so a
    "successful" response cannot plant a phishing link.
    """
    number, url = data.get("number"), data.get("html_url")
    if type(number) is not int or number <= 0:
        return None
    if url != f"https://github.com/{slug}/pull/{number}":
        return None
    return {"number": number, "url": url, "reused": reused, "at": _now()}


def _pr_settled(pr: Any) -> bool:
    # type() not isinstance(): bool subclasses int, and True must never read
    # as PR #1 (the same trap _pr_view closed in PR #424 round 1).
    return isinstance(pr, dict) and type(pr.get("number")) is int \
        and pr.get("number") > 0


def _pr_credentials() -> tuple[str, str] | str:
    """(slug, token) when the follow-through config is usable, else the error
    code. Shared by PR-open and review observation so the charset gate that
    keeps the token out of exception text guards every HTTP path."""
    slug = os.environ.get("LEAF_PLATFORM_PR_REPO", "").strip()
    token = os.environ.get("LEAF_PLATFORM_PR_TOKEN", "").strip()
    if not slug or not token:
        return "pr_config_missing"
    if not _REPO_SLUG_RE.fullmatch(slug):
        return "pr_repo_invalid"
    if _PR_TOKEN_RE.fullmatch(token) is None:
        return "pr_token_invalid"
    return slug, token


# ---------------------------------------------------------------------------- #
# Review observation (issue #422 Phase 2) — READ-ONLY, best-effort, cached.
# The platform OBSERVES the standing review gate's verdict (the
# `sol-critic-review` commit status at the PR head, posted by the fleet
# reviewer on every round since 2026-08-04); it never runs, simulates, or
# gates anything on the review itself. Absence of a verdict at a NEW head is
# correct: pushed code is unreviewed until its own round posts.
# ---------------------------------------------------------------------------- #
REVIEW_CONTEXT = "sol-critic-review"
_REVIEW_CACHE_S = 60


def _fetch_review(record: Mapping[str, Any]) -> dict[str, Any]:
    """One observation of the PR's review state. NEVER raises.

    Two reads: the PR itself (current head + open/merged/closed — the head can
    legitimately move past the lane's commit if a fix round is pushed), then
    the commit status at that head filtered to ``REVIEW_CONTEXT``. Every
    failure is an honest ``state: unknown`` + error code; the token needs
    Commit-statuses read (documented in the runbook) and its absence shows up
    here as ``unknown``, never as a crash.
    """
    at = _now()
    creds = _pr_credentials()
    if isinstance(creds, str):
        return {"state": "unknown", "error": creds, "checked_at": at}
    slug, token = creds
    try:
        # Inside the guarded path: a malformed record (pr: null, junk number)
        # must degrade to unknown, never raise out of a status read.
        pr = record.get("pr")
        number = pr.get("number") if isinstance(pr, Mapping) else None
        if type(number) is not int or number <= 0:
            return {"state": "unknown", "error": "review_pr_number_invalid",
                    "checked_at": at}
        status, data = _github_request(
            "GET", f"/repos/{slug}/pulls/{number}", token=token)
        if status != 200 or not isinstance(data, Mapping):
            return {"state": "unknown", "error": f"review_http_{int(status)}",
                    "checked_at": at}
        head = str((data.get("head") or {}).get("sha", ""))
        if not _SHA_RE.fullmatch(head):
            return {"state": "unknown", "error": "review_head_unresolvable",
                    "checked_at": at}
        # Closed set only: junk from a misconfigured API must not become
        # persisted record content (same invariant as _pr_view).
        pr_state = ("merged" if data.get("merged") is True
                    else data.get("state")
                    if data.get("state") in ("open", "closed") else "unknown")
        status2, agg = _github_request(
            "GET", f"/repos/{slug}/commits/{head}/status", token=token)
        if status2 != 200 or not isinstance(agg, Mapping):
            return {"state": "unknown", "error": f"review_http_{int(status2)}",
                    "pr_state": pr_state, "head_sha": head, "checked_at": at}
        verdict, description = "none", None
        for item in agg.get("statuses") or []:
            if isinstance(item, Mapping) and item.get("context") == REVIEW_CONTEXT:
                raw = str(item.get("state") or "")
                verdict = {"success": "passed", "failure": "failed",
                           "error": "error_verdict",
                           "pending": "pending"}.get(raw, "unknown")
                description = str(item.get("description") or "")[:140]
                break
        review = {"state": verdict, "pr_state": pr_state, "head_sha": head,
                  "checked_at": at}
        if description:
            review["description"] = description
        return review
    except Exception as exc:  # noqa: BLE001 — observation is never load-bearing
        detail = f"{type(exc).__name__}: {exc}".replace(token, "[redacted-token]")
        _LOG.warning("platform_customize: review fetch failed for change %s: %s",
                     record.get("change_id"), _redact(detail)[:200])
        return {"state": "unknown", "error": "review_fetch_failed", "checked_at": at}


# ---------------------------------------------------------------------------- #
# Merge on operator approval (issue #422 Phase 3). This is the first stage
# whose blast radius is the SHARED MAIN BRANCH, so it takes the strictest
# posture in the lane:
#   - its own kill switch (LEAF_PLATFORM_MERGE_ENABLED), independent of
#     PR-open/observation, defaulting OFF;
#   - its own credential (LEAF_PLATFORM_MERGE_TOKEN: Contents write + Pull
#     requests write — necessarily the most powerful of the three tokens, so
#     it revokes independently; emergency containment names THREE tokens);
#   - a fresh operator approval naming the exact commit, one-shot marker;
#   - a same-tree guard executed TWICE: a fresh (never cached) observation
#     that the review PASSED at a head equal to the recorded commit with the
#     PR still open, and then GitHub's own sha-pinned merge (the API refuses
#     if the head moved between our check and the PUT).
# The review gate is untouched: merge is only ever offered AFTER the standing
# reviewer passed the exact bytes.
# ---------------------------------------------------------------------------- #
def merge_enabled() -> bool:
    return os.environ.get("LEAF_PLATFORM_MERGE_ENABLED", "").strip().lower() in {
        "1", "true", "yes", "on"}


def merge(*, change_id: str, tenant_id: str, ack_commit_sha: str,
          approver_subject: str) -> dict[str, Any]:
    """Merge the landed change's PR — only on a fresh, exact-commit approval.

    ``ack_commit_sha`` is the operator's fresh per-invocation approval (the
    land-ack idiom): a bare "merge it" that names no bytes is not an approval,
    and the idempotent replay of an already-merged record demands the same
    exact ack. Delivery-then-mark like land(): GitHub's sha-pinned merge is
    the delivery, the one-shot marker serializes the record transition, and a
    crash between the two heals honestly on the next touch.
    """
    record = load_record(change_id)
    if record.get("tenant_id") != str(tenant_id):
        raise PlatformCustomizeError("change_not_found", 404)
    record = _reconcile(change_id, record)
    commit_sha = str(record.get("commit_sha", ""))
    if not isinstance(ack_commit_sha, str) or ack_commit_sha != commit_sha:
        raise PlatformCustomizeError("merge_ack_mismatch", 409)
    if record.get("state") == MERGED:
        return public_view(record)
    if not merge_enabled():
        raise PlatformCustomizeError("merge_disabled", 404)
    if record.get("state") != LANDED:
        raise PlatformCustomizeError("merge_not_ready", 409)
    pr = record.get("pr")
    if not _pr_settled(pr):
        raise PlatformCustomizeError("merge_no_pr", 409)
    # Defense in depth: a fundamental-path change must still show the durable
    # approved co-sign marker — merging cannot become the path around it.
    if record.get("fundamental_paths") and not _cosign_satisfied(change_id, record):
        raise PlatformCustomizeError("cosign_required", 409)

    # FRESH observation, deliberately bypassing the cache: a merge decision
    # must never ride a 59-second-old projection.
    review = _fetch_review(record)
    if review.get("state") != "passed":
        raise PlatformCustomizeError(
            "merge_review_not_passed", 409,
            f"review_state={review.get('state')} error={review.get('error')}")
    if review.get("pr_state") != "open":
        raise PlatformCustomizeError(
            "merge_pr_not_open", 409, f"pr_state={review.get('pr_state')}")
    if review.get("head_sha") != commit_sha:
        raise PlatformCustomizeError(
            "merge_head_moved", 409,
            f"head={review.get('head_sha')} recorded={commit_sha}")

    creds = _pr_credentials()
    if isinstance(creds, str):  # slug validation rides the same helper
        raise PlatformCustomizeError("merge_config_missing", 503, creds)
    slug, _observe_token = creds
    merge_token = os.environ.get("LEAF_PLATFORM_MERGE_TOKEN", "").strip()
    if not merge_token:
        raise PlatformCustomizeError("merge_config_missing", 503,
                                     "LEAF_PLATFORM_MERGE_TOKEN unset")
    if _PR_TOKEN_RE.fullmatch(merge_token) is None:
        raise PlatformCustomizeError("merge_config_missing", 503,
                                     "merge token charset")

    number = int(pr["number"])
    try:
        # GitHub's own same-tree guard: `sha` pins the exact head this
        # approval covers; a head moved between our check and this PUT
        # answers 409 instead of merging different bytes.
        status, data = _github_request(
            "PUT", f"/repos/{slug}/pulls/{number}/merge", token=merge_token,
            payload={"sha": commit_sha, "merge_method": "squash"})
    except Exception as exc:  # noqa: BLE001
        detail = f"{type(exc).__name__}: {exc}".replace(
            merge_token, "[redacted-token]")
        raise PlatformCustomizeError(
            "merge_failed", 503, _redact(detail)[:200])
    if status == 409:
        raise PlatformCustomizeError("merge_head_moved", 409, "github_409")
    if status == 405:
        raise PlatformCustomizeError("merge_not_mergeable", 409, "github_405")
    if status != 200 or not isinstance(data, Mapping) \
            or data.get("merged") is not True:
        raise PlatformCustomizeError(
            "merge_failed", 503, f"github_{int(status)}")
    merge_commit = str(data.get("sha") or "")

    if not _claim_marker(change_id, "merged", {
            "commit_sha": commit_sha, "merge_commit_sha": merge_commit,
            "at": _now()}):
        return public_view(_reconcile(change_id, load_record(change_id)))

    # Best-effort branch delete (Contents write travels with the merge
    # token); refusing to fail the merge over cleanup.
    branch_deleted = False
    try:
        ref = _assert_branch_only(str(record.get("branch_ref", "")))
        status2, _ = _github_request(
            "DELETE", f"/repos/{slug}/git/refs/{ref[len('refs/'):]}",
            token=merge_token)
        branch_deleted = status2 in (204, 422)  # 422: already gone
    except Exception:  # noqa: BLE001
        pass

    record["merge"] = {
        "merged": True, "commit_sha": commit_sha,
        "merge_commit_sha": merge_commit or None,
        "approved_by_subject": str(approver_subject or ""),
        "branch_deleted": branch_deleted, "at": _now(),
    }
    record["state"] = MERGED
    _write_record(record)
    return public_view(record)


def _review_fresh(review: Any) -> bool:
    if not isinstance(review, dict):
        return False
    try:
        import calendar
        checked = calendar.timegm(
            time.strptime(str(review.get("checked_at")), "%Y-%m-%dT%H:%M:%SZ"))
    except (ValueError, TypeError, OverflowError):
        return False
    return (time.time() - checked) < _REVIEW_CACHE_S


def _refresh_review(record: dict[str, Any]) -> dict[str, Any]:
    """Refresh the cached review projection on a landed, PR-carrying record.
    Skips entirely when the follow-through is off, terminal (merged/closed),
    or the cache is fresh — a drawer poll never turns into an API hammer."""
    if not pr_open_enabled() or record.get("state") != LANDED \
            or not _pr_settled(record.get("pr")):
        return record
    existing = record.get("review")
    if _review_fresh(existing):
        return record
    if isinstance(existing, dict) and existing.get("pr_state") in ("merged", "closed"):
        return record  # terminal; nothing left to observe
    record["review"] = _fetch_review(record)
    _write_record(record)
    return record


def _open_pull_request(record: Mapping[str, Any]) -> dict[str, Any]:
    """Open (or find) the PR for the landed branch. NEVER raises.

    Reuse-first, then create, then one re-read on 422 (GitHub answers 422 for
    "A pull request already exists" when two landers race). Config gaps and
    API failures come back as recorded error codes; operator detail goes to
    the log, redacted and bounded.
    """
    at = _now()
    # _pr_credentials carries the charset gate: a token with whitespace or
    # control characters would make urllib refuse the Authorization header
    # with a ValueError QUOTING THE FULL HEADER — i.e. the token itself —
    # which the except-arm below would log (sol-critic PR #424 round 1,
    # finding 1). Refused before any HTTP attempt.
    creds = _pr_credentials()
    if isinstance(creds, str):
        return {"error": creds, "at": at}
    slug, token = creds
    branch_ref = str(record.get("branch_ref", ""))
    try:
        _assert_branch_only(branch_ref)
    except PlatformCustomizeError:
        return {"error": "pr_branch_invalid", "at": at}
    branch = branch_ref[len("refs/heads/"):]
    owner = slug.split("/", 1)[0]
    # The base is BOUND AT PROPOSAL TIME (recorded next to base_sha): the env
    # read happens once, in propose(), so flipping LEAF_PLATFORM_REPO_BASE_REF
    # between proposal and landing cannot retarget the PR (round 1, finding 3).
    # Records written before this field existed fall back to the current env —
    # the exact pre-fix behavior, for replay compatibility only.
    base = str(record.get("base_ref") or base_ref())
    base = base[len("refs/heads/"):] if base.startswith("refs/heads/") else base
    from urllib.parse import quote

    list_path = (f"/repos/{slug}/pulls?state=open"
                 f"&head={quote(f'{owner}:{branch}', safe='')}"
                 f"&base={quote(base, safe='')}")
    try:
        status, data = _github_request("GET", list_path, token=token)
        if status == 200 and isinstance(data, list):
            for item in data:
                if isinstance(item, Mapping):
                    view = _pr_view(item, slug=slug, reused=True)
                    if view:
                        return view
        status, data = _github_request("POST", f"/repos/{slug}/pulls", token=token, payload={
            "title": f"admin-customize: {record.get('title')}",
            "head": branch,
            "base": base,
            "body": _pr_body(record),
        })
        if status == 201 and isinstance(data, Mapping):
            view = _pr_view(data, slug=slug, reused=False)
            if view:
                return view
        if status == 422:
            # a racing lander (or a previous half-failure) already opened it
            status2, data2 = _github_request("GET", list_path, token=token)
            if status2 == 200 and isinstance(data2, list):
                for item in data2:
                    if isinstance(item, Mapping):
                        view = _pr_view(item, slug=slug, reused=True)
                        if view:
                            return view
        _LOG.warning("platform_customize: PR open answered %s for change %s",
                     status, record.get("change_id"))
        return {"error": f"pr_open_http_{int(status)}", "at": at}
    except Exception as exc:  # noqa: BLE001 — best-effort by contract
        # Defense in depth behind the charset gate: whatever the exception
        # quotes, the literal token never reaches the log.
        detail = f"{type(exc).__name__}: {exc}".replace(token, "[redacted-token]")
        _LOG.warning("platform_customize: PR open failed for change %s: %s",
                     record.get("change_id"), _redact(detail)[:200])
        return {"error": "pr_open_failed", "at": at}


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
    record = _reconcile(change_id, record)
    # The ack binds EVERY land invocation to the exact bytes — including the
    # idempotent replay of an already-landed record. Checked before any state
    # answer so a wrong sha never earns a landed receipt.
    commit_sha = str(record.get("commit_sha", ""))
    if not isinstance(ack_commit_sha, str) or ack_commit_sha != commit_sha:
        raise PlatformCustomizeError("land_ack_mismatch", 409)
    if record.get("state") == LANDED:
        # Idempotent replay doubles as the PR-open RETRY path: the push
        # happened but the PR is missing or errored (crash between marker and
        # record write, transient API failure, config landed later). The ack
        # equality above already re-bound this invocation to the exact bytes.
        if (pr_open_enabled() and isinstance(record.get("push"), dict)
                and record["push"].get("pushed")
                and not _pr_settled(record.get("pr"))):
            record["pr"] = _open_pull_request(record)
            _write_record(record)
        return public_view(record)
    if record.get("state") != APPROVED:
        code = ("cosign_required" if record.get("state") == AWAITING_COSIGN
                else "change_not_landable")
        raise PlatformCustomizeError(code, 409)
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
        # Another lander won (or a prior crash left the marker): reconcile so
        # the projection reflects the marker instead of wedging on "approved".
        return public_view(_reconcile(change_id, load_record(change_id)))

    record["push"] = {"pushed": pushed, "remote": push_remote() if pushed else None,
                      "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    record["state"] = LANDED
    # Best-effort, after the marker: a PR-open failure is recorded, never
    # load-bearing (issue #422 Phase 1). Requires a real push — with push off
    # there is nothing on the remote to open a PR from.
    record["pr"] = (_open_pull_request(record)
                    if pushed and pr_open_enabled() else None)
    _write_record(record)
    return public_view(record)


def status_view(*, change_id: str, tenant_id: str) -> dict[str, Any]:
    """Tenant-scoped read that self-heals a stale projection from markers."""
    record = load_record(change_id)
    if record.get("tenant_id") != str(tenant_id):
        raise PlatformCustomizeError("change_not_found", 404)
    return public_view(_refresh_review(_reconcile(change_id, record)))


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
        "pr": record.get("pr"),
        "review": record.get("review"),
        "merge": record.get("merge"),
        "landing_path": {
            "pipeline": ["branch", "pull-request", "sol-critic review gate",
                         "merge", "ECS staging canary", "production"],
            "rollback": "previous ECS task-definition revision",
            "writes": ("branch-only; merge happens ONLY on a fresh operator "
                       "approval naming the exact commit, after the review "
                       "gate passed those bytes; this lane never deploys"),
        },
    }
