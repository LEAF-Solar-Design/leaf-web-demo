"""Per-root content digests for admissible external inputs (stdlib only).

The digest binds every installed file under an admissible root by relative
path, size and SHA-256, so a changed installed byte changes the capture epoch.
Walks are bounded (files and bytes per root) and never follow a symlink out of
the root. Bytecode is skipped: it is derived from sources the walk covers and
carries install-time mtimes.
"""

import hashlib
import json
import os
import stat


ADMISSIBLE_CLASSES = ("python-installation", "os-image", "terraform-provider-cache")
LIMITS = {"max_files": 300000, "max_bytes": 2 * 1024 ** 3}  # per root [guessed]
DEFAULT_ROOTS = {
    "/usr/lib/python3.11": {"class": "python-installation", "origin_category": "python-installation"},
    "/usr/local/lib/python3.11": {"class": "python-installation", "origin_category": "python-installation"},
    "/usr/bin": {"class": "os-image", "origin_category": "os-image"},
    "/usr/lib/x86_64-linux-gnu": {"class": "os-image", "origin_category": "os-image"},
    "/usr/lib64": {"class": "os-image", "origin_category": "os-image"},
    "/lib/x86_64-linux-gnu": {"class": "os-image", "origin_category": "os-image"},
    "/lib64": {"class": "os-image", "origin_category": "os-image"},
    # /etc is not a root: only these single files are admissible os-image inputs.
    "/etc/ld.so.cache": {"class": "os-image", "origin_category": "os-image"},
    "/etc/localtime": {"class": "os-image", "origin_category": "os-image"},
    "/etc/nsswitch.conf": {"class": "os-image", "origin_category": "os-image"},
    "/etc/passwd": {"class": "os-image", "origin_category": "os-image"},
    "/etc/group": {"class": "os-image", "origin_category": "os-image"},
    "/tmp": {"class": "generated", "origin_category": "generated"},
    "/dev": {"class": "device", "origin_category": "device"},
    "/proc": {"class": "device", "origin_category": "device"},
    "/sys": {"class": "device", "origin_category": "device"},
    # The producer substitutes its resolved cache directory (resolve_default_roots).
    "${TF_PLUGIN_CACHE_DIR}": {"class": "terraform-provider-cache", "origin_category": "terraform-provider-cache"},
}


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False).encode("utf-8")


def root_class(value):
    if isinstance(value, str):
        return value
    if isinstance(value, dict) and isinstance(value.get("class"), str):
        return value["class"]
    return "unresolved_external"


def resolve_default_roots(environ=None, table=None):
    """Replace ${NAME} root keys from the producer environment; drop unset names."""
    environ = os.environ if environ is None else environ
    result = {}
    for root, value in (DEFAULT_ROOTS if table is None else table).items():
        if root.startswith("${") and root.endswith("}"):
            resolved = environ.get(root[2:-1])
            if not resolved or not resolved.startswith("/"):
                continue
            root = resolved.rstrip("/") or "/"
        result[root] = value
    return result


def _hash_file(path, budget):
    """Hash one regular file without following a final symlink; bounded by budget."""
    hasher = hashlib.sha256()
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    with os.fdopen(fd, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise OSError("not_regular")
        size = 0
        for chunk in iter(lambda: stream.read(1048576), b""):
            size += len(chunk)
            if size > budget:
                raise OverflowError("inventory_too_large")
            hasher.update(chunk)
    return size, hasher.hexdigest()


def _inside(path, root_real):
    real = os.path.realpath(path)
    return real == root_real or real.startswith(root_real.rstrip(os.sep) + os.sep)


def digest_root(root, limits=None):
    """Return {files, bytes, sha256, complete, reason} for one root; never raises on I/O."""
    caps = dict(LIMITS)
    caps.update(limits or {})
    row = {"files": 0, "bytes": 0, "sha256": None, "complete": False, "reason": None}
    if not isinstance(root, str) or not root or "${" in root:
        row["reason"] = "root_unresolved"
        return row
    try:
        info = os.lstat(root)
    except OSError:
        row["reason"] = "root_missing"
        return row
    entries = []
    try:
        if stat.S_ISREG(info.st_mode):
            size, sha = _hash_file(root, caps["max_bytes"])
            entries.append([os.path.basename(root), size, sha])
            row.update(files=1, bytes=size)
        elif stat.S_ISDIR(info.st_mode):
            root_real = os.path.realpath(root)
            for directory, dirnames, filenames in os.walk(root, followlinks=False):
                dirnames[:] = sorted(d for d in dirnames if d != "__pycache__")
                names = [(d, True) for d in dirnames] + [(f, False) for f in sorted(filenames)]
                keep = []
                for name, is_dir in names:
                    full = os.path.join(directory, name)
                    relative = os.path.relpath(full, root).replace(os.sep, "/")
                    if os.path.islink(full):
                        # A link is bound by its target text; never follow it out.
                        if not _inside(full, root_real):
                            row["reason"] = "symlink_escape"
                            return row
                        entries.append([relative, "symlink", os.readlink(full)])
                        row["files"] += 1
                    elif is_dir:
                        keep.append(name)
                        continue
                    elif name.endswith(".pyc"):
                        continue
                    else:
                        size, sha = _hash_file(full, caps["max_bytes"] - row["bytes"])
                        entries.append([relative, size, sha])
                        row["files"] += 1
                        row["bytes"] += size
                    if row["files"] > caps["max_files"] or row["bytes"] > caps["max_bytes"]:
                        raise OverflowError("inventory_too_large")
                dirnames[:] = keep
        else:
            row["reason"] = "not_regular"
            return row
    except OverflowError:
        row.update(reason="inventory_too_large", sha256=None)
        return row
    except (OSError, ValueError):
        row.update(reason="unreadable", sha256=None)
        return row
    row.update(sha256=hashlib.sha256(_canonical(sorted(entries))).hexdigest(), complete=True)
    return row


def digest_roots(table, limits=None):
    """Digest every admissible root of an external-roots table, keyed by the table's root."""
    if not isinstance(table, dict):
        raise ValueError("invalid_external_roots")
    result = {}
    for root in sorted(table):
        kind = root_class(table[root])
        if kind not in ADMISSIBLE_CLASSES:
            continue
        row = digest_root(root, limits)
        result[root] = dict(row, **{"class": kind})
    return result
