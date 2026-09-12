"""Offline Git materialization for the Studio producer's three admitted sources.

Consumes forge/source_snapshot.py schema 1, not commands or a buildspec. The
caller must load ``admitted`` from trusted producer configuration/admission,
independently of the untrusted request. This library grants no build authority.
Store.get(bucket, key, version_id, limit) returns (bytes, actual_version_id).
"""
from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import zipfile

ROLES = ("primary", "provider_contract", "autofill_solver")
LIMIT = 512 * 1024 * 1024


def require(value, message):
    if not value:
        raise ValueError(message)


def digest(value):
    return hashlib.sha256(value).hexdigest()


def hex_id(value, size):
    require(isinstance(value, str) and re.fullmatch(r"[0-9a-f]{%d}" % size, value), "invalid digest")


def fetch(store, ref, scope, limit=LIMIT):
    # Same immutable reference contract as forge/source_snapshot.py.
    require(isinstance(ref, dict) and set(ref) == {"bucket", "key", "version_id", "sha256"}, "reference fields")
    require(set(scope) == {"bucket", "prefix"} and scope["prefix"].endswith("/"), "scope fields")
    require(ref["bucket"] == scope["bucket"] and isinstance(ref["key"], str)
            and ref["key"].startswith(scope["prefix"]), "source scope")
    require(all(p not in ("", ".", "..") for p in ref["key"].split("/"))
            and not any(c in ref["key"] for c in ("\\", ":", "\x00")), "source key")
    require(isinstance(ref["version_id"], str) and ref["version_id"] not in ("", "null"), "version required")
    hex_id(ref["sha256"], 64)
    data, version = store.get(ref["bucket"], ref["key"], ref["version_id"], limit)
    require(isinstance(data, bytes) and len(data) <= limit and version == ref["version_id"]
            and digest(data) == ref["sha256"], "object integrity")
    return data


def git(root, *args):
    # No inherited Git config, hooks, filters, remote helpers or prompts. Output
    # uses private files rather than pipes held open by child processes.
    env = {k: os.environ[k] for k in ("PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP") if k in os.environ}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
               GIT_TERMINAL_PROMPT="0", GIT_ALLOW_PROTOCOL="file", GIT_NO_REPLACE_OBJECTS="1")
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        result = subprocess.run(["git", "-c", "core.hooksPath=" + os.devnull,
                                 "-c", "core.autocrlf=false", "-c", "core.filemode=true",
                                 *args], cwd=root, env=env, stdin=subprocess.DEVNULL,
                                stdout=out, stderr=err, timeout=60, close_fds=True)
        require(result.returncode == 0, "local Git verification failed")
        require(out.tell() <= 8 * 1024 * 1024, "Git output limit")
        out.seek(0)
        return out.read()


def archive_members(data):
    """Compare Git archive semantics, not ZIP compression implementation bytes."""
    result, total = {}, 0
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        require(len(archive.infolist()) <= 20000, "archive member limit")
        for item in archive.infolist():
            name = item.orig_filename
            require(name == item.filename and not name.startswith("/") and "\\" not in name
                    and ":" not in name and all(p not in ("", ".", "..") for p in name.rstrip("/").split("/")), "archive path")
            require(name.casefold() not in result and not item.flag_bits & 1, "duplicate/encrypted archive member")
            total += item.file_size
            require(total <= LIMIT, "archive expansion limit")
            with archive.open(item) as stream:
                h, size = hashlib.sha256(), 0
                while chunk := stream.read(1024 * 1024):
                    size += len(chunk)
                    require(size <= item.file_size, "archive size")
                    h.update(chunk)
            require(size == item.file_size, "archive truncated")
            result[name.casefold()] = (name, item.external_attr >> 16, size, h.hexdigest())
    return result


def materialize(store, requested, admitted, destination, *, producer_request):
    """Return the three CodeBuild directory values plus exact source custody.

    Both mappings have exactly three roles. Each admitted entry contains only
    commit, tree, manifest (immutable ref) and scope (bucket/prefix). No AWS pin
    is defaulted. ``requested`` must equal those independently admitted refs.
    Failure leaves a private incomplete directory for diagnosis, never a result.
    """
    require(isinstance(admitted, dict) and set(admitted) == set(ROLES)
            and isinstance(requested, dict) and set(requested) == set(ROLES), "source roles")
    # Validate every admission before fetching or creating any checkout.
    for role in ROLES:
        entry = admitted[role]
        require(set(entry) == {"commit", "tree", "manifest", "scope"}, "admission fields")
        hex_id(entry["commit"], 40)
        hex_id(entry["tree"], 40)
        require(requested[role] == entry["manifest"], "request differs from admission")
    require(producer_request.get("source_revision") == admitted["primary"]["commit"]
            and producer_request.get("source_tree") == admitted["primary"]["tree"], "producer source binding")
    if "contract_revision" in producer_request:
        require(producer_request["contract_revision"] == admitted["provider_contract"]["commit"], "producer contract binding")
    destination = Path(destination).resolve()
    destination.mkdir(mode=0o700, parents=True, exist_ok=False)
    custody = {}
    for role in ROLES:
        entry = admitted[role]
        raw = fetch(store, entry["manifest"], entry["scope"], 32768)
        manifest = json.loads(raw)
        require(set(manifest) == {"schema_version", "commit", "archive", "bundle"}
                and type(manifest["schema_version"]) is int and manifest["schema_version"] == 1
                and manifest["commit"] == entry["commit"], "snapshot manifest")
        archive = fetch(store, manifest["archive"], entry["scope"])
        bundle = fetch(store, manifest["bundle"], entry["scope"])
        bundle_path = destination / (role + ".bundle")
        bundle_path.write_bytes(bundle)
        root = destination / role
        root.mkdir(mode=0o700)
        git(root, "init", "--template=", ".")
        git(root, "bundle", "verify", str(bundle_path))
        heads = git(root, "bundle", "list-heads", str(bundle_path)).decode().splitlines()
        require(heads == [entry["commit"] + " refs/heads/snapshot"], "bundle snapshot head")
        git(root, "fetch", "--no-tags", str(bundle_path), "refs/heads/snapshot")
        require(git(root, "rev-parse", "FETCH_HEAD^{commit}").decode().strip() == entry["commit"]
                and git(root, "rev-parse", "FETCH_HEAD^{tree}").decode().strip() == entry["tree"], "commit/tree substitution")
        git(root, "fsck", "--full", "--no-reflogs")
        # Canonical snapshot already refuses links and repository metadata.
        # Match that profile before checkout; never fetch a submodule.
        rows = git(root, "ls-tree", "-rzl", "FETCH_HEAD").split(b"\0")
        total = 0
        for row in filter(None, rows):
            meta, name = row.split(b"\t", 1)
            mode, kind, oid, size = meta.split()
            require(mode in (b"100644", b"100755") and kind == b"blob", "unsupported snapshot file type")
            require(not any(p.lower() in (b".git", b".terraform") for p in name.split(b"/")), "snapshot metadata")
            total += int(size)
        require(total <= LIMIT and len(rows) <= 20001, "checkout bound")
        check_archive = destination / (role + ".zip")
        git(root, "archive", "--format=zip", "--output=" + str(check_archive), "FETCH_HEAD")
        require(check_archive.stat().st_size <= LIMIT, "archive limit")
        require(archive_members(archive) == archive_members(check_archive.read_bytes()), "archive/bundle disagreement")
        git(root, "checkout", "--detach", entry["commit"])
        require(not git(root, "status", "--porcelain", "--untracked-files=normal"), "dirty materialized source")
        custody[role] = {"commit": entry["commit"], "tree": entry["tree"],
                         "manifest": dict(entry["manifest"]), "archive": manifest["archive"], "bundle": manifest["bundle"]}
    return {"directories": {"CODEBUILD_SRC_DIR": str(destination / "primary"),
                            "CODEBUILD_SRC_DIR_provider_contract": str(destination / "provider_contract"),
                            "CODEBUILD_SRC_DIR_autofill_solver": str(destination / "autofill_solver")},
            "sources": custody}
