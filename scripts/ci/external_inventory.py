# Ported from terraform at 9fca040c18a6fd338445c7199865c0f0552fcbec with the terraform roots removed.
"""Per-root content digests for admissible external inputs (stdlib only).

The digest binds every installed file under an admissible root by relative
path, size and SHA-256, so a changed installed byte changes the capture epoch.
Walks are bounded (files and bytes per root) and never follow a symlink out of
the admissible roots. Bytecode is skipped: it is derived from sources the walk covers and
carries install-time mtimes; a traced bytecode read binds to its source's identity instead.
"""

import hashlib
import json
import os
import stat
import subprocess
import sys


ADMISSIBLE_CLASSES = ("python-installation", "os-image")
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
    # tools-29 rule D: image files the suites read (TLS, git, locale, terminfo, shell rc).
    "/etc/ssl/certs": {"class": "os-image", "origin_category": "os-image"},
    "/etc/ssl/openssl.cnf": {"class": "os-image", "origin_category": "os-image"},
    "/usr/lib/ssl": {"class": "os-image", "origin_category": "os-image"},
    "/usr/share/git-core": {"class": "os-image", "origin_category": "os-image"},
    # tools-31: only the three git executables named by the v21/v22 certificates.
    "/usr/libexec/git-core/git": {"class": "os-image", "origin_category": "os-image"},
    "/usr/libexec/git-core/git-receive-pack": {"class": "os-image", "origin_category": "os-image"},
    "/usr/libexec/git-core/git-upload-pack": {"class": "os-image", "origin_category": "os-image"},
    "/etc/gitconfig": {"class": "os-image", "origin_category": "os-image"},
    "/usr/lib/locale": {"class": "os-image", "origin_category": "os-image"},
    "/usr/share/terminfo": {"class": "os-image", "origin_category": "os-image"},
    "/lib/terminfo": {"class": "os-image", "origin_category": "os-image"},
    "/etc/terminfo": {"class": "os-image", "origin_category": "os-image"},
    "/etc/inputrc": {"class": "os-image", "origin_category": "os-image"},
    "/root/.profile": {"class": "os-image", "origin_category": "os-image"},
    "/root/.bashrc": {"class": "os-image", "origin_category": "os-image"},
    "/bin/bash": {"class": "os-image", "origin_category": "os-image"},
    "/bin/sh": {"class": "os-image", "origin_category": "os-image"},
    "/usr/local/bin/node": {"class": "os-image", "origin_category": "os-image"},
    "/etc/profile": {"class": "os-image", "origin_category": "os-image"},
    "/etc/profile.d": {"class": "os-image", "origin_category": "os-image"},
    "/usr/share/locale/locale.alias": {"class": "os-image", "origin_category": "os-image"},
    "/root/.gitconfig": {"class": "os-image", "origin_category": "os-image"},
    # p3f-image-roots: web-demo capture-2 evidence of installed image reads.
    "/usr/share/zoneinfo": {"class": "os-image", "origin_category": "os-image"},
    "/etc/timezone": {"class": "os-image", "origin_category": "os-image"},
    "/usr/local/bin": {"class": "os-image", "origin_category": "os-image"},
    "/usr/lib/git-core/git": {"class": "os-image", "origin_category": "os-image"},
    "/usr/lib/git-core/git-upload-pack": {"class": "os-image", "origin_category": "os-image"},
    "/usr/lib/git-core/git-receive-pack": {"class": "os-image", "origin_category": "os-image"},
    "/etc/mime.types": {"class": "os-image", "origin_category": "os-image"},
    "/etc/gai.conf": {"class": "os-image", "origin_category": "os-image"},
    "/etc/os-release": {"class": "os-image", "origin_category": "os-image"},
    "/usr/lib/os-release": {"class": "os-image", "origin_category": "os-image"},
    "/etc/python3.12": {"class": "os-image", "origin_category": "os-image"},
    "/etc/fonts": {"class": "os-image", "origin_category": "os-image"},
    "/usr/share/fonts": {"class": "os-image", "origin_category": "os-image"},
    "/usr/local/share/fonts": {"class": "os-image", "origin_category": "os-image"},
    "/var/cache/fontconfig": {"class": "os-image", "origin_category": "os-image"},
    "/usr/lib/node_modules": {"class": "os-image", "origin_category": "os-image"},
    "/usr/lib/postgresql": {"class": "os-image", "origin_category": "os-image"},
    "/root/.npm": {"class": "generated", "origin_category": "generated"},
    "/tmp": {"class": "generated", "origin_category": "generated"},
    # Runtime-written per container: bind by class, never by installed bytes.
    "/etc/hosts": {"class": "container-runtime", "origin_category": "container-runtime"},
    "/etc/resolv.conf": {"class": "container-runtime", "origin_category": "container-runtime"},
    "/etc/host.conf": {"class": "container-runtime", "origin_category": "container-runtime"},
    "/etc/hostname": {"class": "container-runtime", "origin_category": "container-runtime"},
    # Never digested: the decoder binds these by class (device, kernel), not by content.
    "/dev": {"class": "device", "origin_category": "device"},
    "/proc": {"class": "kernel", "origin_category": "kernel"},
    "/sys": {"class": "kernel", "origin_category": "kernel"},
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


PYTHON_ROOTS_TIMEOUT = 20
_PYTHON_ROOTS_PROBE = """import json, os, site, sys, sysconfig
paths = sysconfig.get_paths()
roots = [paths[key] for key in ('stdlib', 'platstdlib', 'purelib', 'platlib')]
roots.extend(site.getsitepackages())
user = site.getusersitepackages()
if user and os.path.exists(user):
    roots.append(user)
roots.append(os.path.dirname(os.path.realpath(sys.executable)))
library = sysconfig.get_config_var('INSTSONAME') or sysconfig.get_config_var('LDLIBRARY')
if library:
    library = os.path.join(sys.base_prefix, 'lib', library)
    if os.path.isfile(library):
        roots.append(library)
print(json.dumps([os.path.realpath(root) for root in roots]))
"""


def resolve_default_roots(environ=None, interpreter=None, table=None):
    """Resolve Python roots with the suite interpreter and expose any fallback."""
    environ = os.environ if environ is None else environ
    interpreter = interpreter if interpreter is not None else environ.get("LEAF_TRACE_INTERPRETER", sys.executable)
    report = {"roots_source": "fallback", "roots_interpreter": interpreter, "error_class": None}
    python_roots = {root: value for root, value in DEFAULT_ROOTS.items()
                    if root_class(value) == "python-installation"}
    try:
        if not isinstance(interpreter, str) or not os.path.isabs(interpreter):
            raise ValueError("interpreter_must_be_absolute")
        probe = subprocess.run([interpreter, "-I", "-c", _PYTHON_ROOTS_PROBE],
                               stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               timeout=PYTHON_ROOTS_TIMEOUT, check=True)
        paths = json.loads(probe.stdout)
        if (not isinstance(paths, list) or not paths
                or any(not isinstance(path, str) or not os.path.isabs(path) or "\x00" in path for path in paths)):
            raise ValueError("invalid_python_roots")
        roots = []
        for path in sorted({os.path.realpath(path) for path in paths}, key=lambda path: (len(path), path)):
            if not any(_within(os.path.normcase(path), os.path.normcase(root)) for root in roots):
                roots.append(path)
        python_roots = {root: {"class": "python-installation", "origin_category": "python-installation"}
                        for root in roots}
        report["roots_source"] = "interpreter"
    except (OSError, ValueError, UnicodeError, subprocess.SubprocessError) as exc:
        report["error_class"] = type(exc).__name__
    resolve_default_roots.last_report = report
    result = {}
    for root, value in (DEFAULT_ROOTS if table is None else table).items():
        if root_class(value) == "python-installation":
            continue
        if root.startswith("${") and root.endswith("}"):
            resolved = environ.get(root[2:-1])
            if not resolved or not resolved.startswith("/"):
                continue
            root = resolved.rstrip("/") or "/"
        result[root] = value
    for root, value in python_roots.items():
        # Preserve explicit non-Python policy for a shared directory such as /usr/bin.
        result.setdefault(root, value)
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


def _within(real, root_real):
    return real == root_real or real.startswith(root_real.rstrip(os.sep) + os.sep)


def digest_root(root, limits=None, admissible=()):
    """Return {files, bytes, sha256, complete, reason, symlinks_outside} for one root; never raises on I/O.

    A root that is itself a symlink (merged-usr farms, /etc/localtime) is walked at its
    realpath and records it as target. A symlink inside the root whose target lies in
    this root or another admissible root (realpaths in admissible) is followed and hashed
    as content; one whose target lies outside every admissible root is bound by its link
    text and counted in symlinks_outside (a read through it is its own external input).
    A missing root is complete and empty.
    """
    caps = dict(LIMITS)
    caps.update(limits or {})
    row = {"files": 0, "bytes": 0, "sha256": None, "complete": False, "reason": None, "symlinks_outside": 0}
    if not isinstance(root, str) or not root or "${" in root:
        row["reason"] = "root_unresolved"
        return row
    try:
        os.lstat(root)
        real = os.path.realpath(root)
        info = os.stat(real)
    except (OSError, ValueError):
        row.update(reason="root_missing", complete=True, sha256=hashlib.sha256(_canonical([])).hexdigest())
        return row
    if os.path.normcase(real) != os.path.normcase(os.path.abspath(root)):
        row["target"] = real.replace(os.sep, "/")
    targets = [real] + [r for r in admissible if r != real]
    entries = []
    try:
        if stat.S_ISREG(info.st_mode):
            size, sha = _hash_file(real, caps["max_bytes"])
            entries.append([os.path.basename(root), size, sha])
            row.update(files=1, bytes=size)
        elif stat.S_ISDIR(info.st_mode):
            for directory, dirnames, filenames in os.walk(real, followlinks=False):
                dirnames[:] = sorted(d for d in dirnames if d != "__pycache__")
                names = [(d, True) for d in dirnames] + [(f, False) for f in sorted(filenames)]
                keep = []
                for name, is_dir in names:
                    full = os.path.join(directory, name)
                    relative = os.path.relpath(full, real).replace(os.sep, "/")
                    if os.path.islink(full):
                        target = os.path.realpath(full)
                        if not any(_within(target, r) for r in targets):
                            # Bound by its text only; never followed out of the admissible set.
                            row["symlinks_outside"] += 1
                            entries.append([relative, "symlink", os.readlink(full)])
                        elif not is_dir and os.path.isfile(target):
                            size, sha = _hash_file(target, caps["max_bytes"] - row["bytes"])
                            entries.append([relative, size, sha])
                            row["bytes"] += size
                        else:
                            # A directory or dangling target: its content is walked where it lives.
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
    roots = [root for root in sorted(table) if root_class(table[root]) in ADMISSIBLE_CLASSES]
    admissible = []
    for root in roots:
        try:
            admissible.append(os.path.realpath(root))
        except (OSError, ValueError, TypeError):
            continue
    result = {}
    for root in roots:
        row = digest_root(root, limits, admissible)
        result[root] = dict(row, **{"class": root_class(table[root])})
    for root in sorted(table):
        if root_class(table[root]) == "container-runtime":
            result[root] = {"class": "container-runtime", "sha256": None, "complete": True,
                            "reason": None, "files": 0, "bytes": 0, "symlinks_outside": 0}
    return result
