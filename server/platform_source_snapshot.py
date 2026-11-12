"""Offline, exact-source Git objects for platform authoring.

The image producer creates the snapshot from its admitted checkout. Runtime
installation adds objects and one immutable ref to the existing authoring repo.
It never copies Git configuration or contacts a remote.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile


SCHEMA = "leaf.platform-source-snapshot.v1"
_SHA = re.compile(r"[0-9a-f]{40}\Z")
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_FIELDS = {"schema", "source_sha", "source_tree", "bundle_sha256"}
_ZERO = "0" * 40
_TIMEOUT = 120


class SnapshotError(Exception):
    """A fixed error code, without Git output or local configuration."""


def _sha(value: str) -> str:
    if not isinstance(value, str) or not _SHA.fullmatch(value):
        raise SnapshotError("snapshot_source_invalid")
    return value


def _run(directory: Path, *args: str, bare: bool = True,
         code: str = "snapshot_git_failed", allowed: tuple[int, ...] = (0,)):
    # These overrides are command-local. In particular, an import must not run
    # reference hooks or background maintenance from the destination's config.
    command = ["git", "-c", "core.hooksPath=", "-c", "gc.auto=0",
               "-c", "maintenance.auto=false", "-c", "protocol.allow=never"]
    command += ["--git-dir", str(directory)] if bare else ["-C", str(directory)]
    try:
        result = subprocess.run(command + list(args), check=False,
                                capture_output=True, timeout=_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        raise SnapshotError(code) from None
    if result.returncode not in allowed:
        raise SnapshotError(code)
    return result.returncode, result.stdout.decode("utf-8", errors="replace").strip()


def _git(directory: Path, *args: str, bare: bool = True,
         code: str = "snapshot_git_failed") -> str:
    return _run(directory, *args, bare=bare, code=code)[1]


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise SnapshotError("snapshot_manifest_invalid")
        result[key] = value
    return result


def _manifest(directory: Path, expected_source: str) -> dict:
    try:
        raw = (directory / "manifest.json").read_bytes()
        if len(raw) > 16384:
            raise SnapshotError("snapshot_manifest_invalid")
        value = json.loads(raw, object_pairs_hook=_pairs)
    except (OSError, ValueError, UnicodeError):
        raise SnapshotError("snapshot_manifest_invalid") from None
    if (not isinstance(value, dict) or set(value) != _FIELDS
            or value.get("schema") != SCHEMA
            or not isinstance(value.get("source_sha"), str)
            or not _SHA.fullmatch(value["source_sha"])
            or not isinstance(value.get("source_tree"), str)
            or not _SHA.fullmatch(value["source_tree"])
            or not isinstance(value.get("bundle_sha256"), str)
            or not _HASH.fullmatch(value["bundle_sha256"])):
        raise SnapshotError("snapshot_manifest_invalid")
    if value["source_sha"] != expected_source:
        raise SnapshotError("snapshot_source_mismatch")
    return value


def _verified_copy(directory: Path, expected_source: str, scratch: Path):
    manifest = _manifest(directory, expected_source)
    bundle = scratch / "snapshot.bundle"
    try:
        shutil.copyfile(directory / "snapshot.bundle", bundle)
        actual_hash = _digest(bundle)
    except OSError:
        raise SnapshotError("snapshot_bundle_unavailable") from None
    if actual_hash != manifest["bundle_sha256"]:
        raise SnapshotError("snapshot_bundle_hash_mismatch")
    empty = scratch / "verify.git"
    _git(scratch, "init", "--bare", str(empty), bare=False)
    # Verification in an empty repository refuses prerequisite/incremental
    # bundles even if the eventual destination happens to have their parents.
    _git(empty, "bundle", "verify", str(bundle), code="snapshot_bundle_incomplete")
    heads = _git(empty, "bundle", "list-heads", str(bundle)).splitlines()
    if heads != [expected_source + " HEAD"]:
        raise SnapshotError("snapshot_bundle_heads_mismatch")
    _git(empty, "-c", "protocol.file.allow=always", "fetch", "--no-tags",
         "--no-write-fetch-head", str(bundle), "HEAD", code="snapshot_bundle_import_failed")
    if _git(empty, "cat-file", "-t", expected_source) != "commit":
        raise SnapshotError("snapshot_source_not_commit")
    if _git(empty, "rev-parse", expected_source + "^{tree}") != manifest["source_tree"]:
        raise SnapshotError("snapshot_tree_mismatch")
    return manifest, bundle


def create_snapshot(root: Path, output: Path, source_sha: str,
                    source_tree: str) -> dict:
    """Create an exclusive complete HEAD bundle, bound to admitted SHA/tree."""
    source_sha, source_tree = _sha(source_sha), _sha(source_tree)
    root, output = Path(root).resolve(), Path(output).resolve()
    if (_git(root, "rev-parse", "--verify", "HEAD", bare=False) != source_sha
            or _git(root, "rev-parse", "--verify", "HEAD^{tree}", bare=False) != source_tree):
        raise SnapshotError("snapshot_checkout_mismatch")
    try:
        output.mkdir(parents=False, exist_ok=False)
    except OSError:
        raise SnapshotError("snapshot_output_exists_or_unavailable") from None
    bundle = output / "snapshot.bundle"
    _git(root, "bundle", "create", str(bundle), "HEAD", bare=False)
    try:
        manifest = {"schema": SCHEMA, "source_sha": source_sha,
                    "source_tree": source_tree, "bundle_sha256": _digest(bundle)}
        with (output / "manifest.json").open("x", encoding="utf-8") as stream:
            json.dump(manifest, stream, sort_keys=True)
            stream.write("\n")
    except OSError:
        raise SnapshotError("snapshot_output_unavailable") from None
    with tempfile.TemporaryDirectory(prefix="leaf-source-verify-") as temp:
        _verified_copy(output, source_sha, Path(temp))
    return manifest


def _existing_ref(git_dir: Path, ref: str) -> str | None:
    symbolic, _ = _run(git_dir, "symbolic-ref", "--quiet", ref, allowed=(0, 1))
    if symbolic == 0:
        raise SnapshotError("snapshot_ref_conflict")
    status, value = _run(git_dir, "rev-parse", "--verify", "--quiet", ref,
                         allowed=(0, 1))
    return value if status == 0 else None


def install_snapshot(snapshot_dir: Path, git_dir: Path,
                     expected_source: str) -> str:
    """Add an exact versioned source ref, preserving all existing refs/config."""
    expected_source = _sha(expected_source)
    snapshot_dir, git_dir = Path(snapshot_dir).resolve(), Path(git_dir).resolve()
    if not (git_dir / "HEAD").is_file():
        raise SnapshotError("snapshot_target_unavailable")
    _git(git_dir, "rev-parse", "--is-bare-repository", code="snapshot_target_unavailable")
    ref = "refs/leaf/deployed/" + expected_source
    with tempfile.TemporaryDirectory(prefix="leaf-source-install-") as temp:
        manifest, bundle = _verified_copy(snapshot_dir, expected_source, Path(temp))
        existing = _existing_ref(git_dir, ref)
        if existing is not None and existing != expected_source:
            raise SnapshotError("snapshot_ref_conflict")
        if existing is None:
            _git(git_dir, "-c", "protocol.file.allow=always", "fetch", "--no-tags",
                 "--no-write-fetch-head", str(bundle), "HEAD",
                 code="snapshot_target_import_failed")
            status, _ = _run(git_dir, "update-ref", "--no-deref", ref, expected_source, _ZERO,
                             allowed=(0, 1, 128))
            # A concurrent identical installer is idempotent. Any other value,
            # including absence after a permissions failure, remains a refusal.
            if status and _existing_ref(git_dir, ref) != expected_source:
                raise SnapshotError("snapshot_ref_conflict")
        if _git(git_dir, "rev-parse", ref + "^{tree}") != manifest["source_tree"]:
            raise SnapshotError("snapshot_tree_mismatch")
    return ref
