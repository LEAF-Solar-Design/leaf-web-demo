"""Fail-open, job-local dependency cache copies. Only trusted main jobs publish."""

import argparse
from concurrent.futures import ThreadPoolExecutor
import datetime
import errno
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shutil
import stat
import sys
import sysconfig
import time


MAX_BYTES = 6 * 1024**3
MAX_FILES = 250000
BUFFER_SIZE = 1024**2
DEPENDENCY_FILES = (
    "web/package-lock.json", "harness/package-lock.json", "server/requirements.txt",
    "server/requirements-auth.txt", "da/requirements.txt", "platform/requirements.txt",
    "scripts/requirements-ci.txt", "executor/control_plane/requirements.txt",
    "executor/runtime/requirements.txt",
)
ENTRY_RE = re.compile(r"(dl|pw|as|nm|py)-v1-[0-9a-f]{64}\Z")


class Corrupt(Exception):
    pass


class Refused(Exception):
    pass


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def safe_path(value):
    if (not isinstance(value, str) or not value or len(value.encode()) > 4096
            or "\x00" in value or "\\" in value or ":" in value
            or value.startswith("/") or any(p in ("", ".", "..") for p in value.split("/"))):
        raise Corrupt("unsafe path")
    return value


def safe_link(path, target):
    if (not isinstance(target, str) or not target or "\x00" in target
            or "\\" in target or ":" in target or target.startswith("/")
            or len(target.encode()) > 4096):
        raise Corrupt("unsafe link")
    stack = list(PurePosixPath(path).parent.parts)
    for part in target.split("/"):
        if part == "..":
            if len(stack) <= 1:
                raise Corrupt("escaping link")
            stack.pop()
        elif part not in ("", "."):
            stack.append(part)


def validate_links(links):
    """Resolve manifest link chains too: lexical '..' checks alone are insufficient."""
    mapping = dict(links)
    for name, target in links:
        pending = list(PurePosixPath(name).parent.parts) + target.split("/")
        resolved, followed = [], 0
        part = name.split("/", 1)[0]
        while pending:
            item = pending.pop(0)
            if item in ("", "."):
                continue
            if item == "..":
                if len(resolved) <= 1:
                    raise Corrupt("escaping link chain")
                resolved.pop()
                continue
            resolved.append(item)
            link = "/".join(resolved)
            if link in mapping:
                followed += 1
                if followed > 40:
                    raise Corrupt("link cycle")
                resolved.pop()
                pending = mapping[link].split("/") + pending
            if resolved and resolved[0] != part:
                raise Corrupt("cross-part link")


def key_inputs(args):
    repo = Path(args.repo)
    if args.kind == "dl":
        files = {}
        for rel in DEPENDENCY_FILES:
            path = repo / rel
            files[rel] = hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else "absent"
        major = re.match(r"v?(\d+)", args.node_version)
        return {"schema": "leaf.dep-cache.dl.v1", "files": files,
                "python": f"{sys.version_info.major}.{sys.version_info.minor}",
                "platform": sysconfig.get_platform(), "node": major[1] if major else "absent",
                "day": args.day}
    if args.kind == "as":
        if not re.fullmatch(r"[0-9a-f]{40}", args.revision):
            raise ValueError("invalid revision")
        return {"schema": "leaf.dep-cache.as.v1", "revision": args.revision}
    release = {}
    for line in Path(args.os_release).read_text().splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            release[key] = value.strip().strip("\"'")
    os_version = release.get("ID", "") + release.get("VERSION_ID", "")
    if args.kind == "nm":
        if (args.tree not in ("web", "harness")
                or not re.fullmatch(r"v\d+\.\d+\.\d+", args.node_version)
                or not re.fullmatch(r"\d+\.\d+\.\d+", args.npm_version)):
            raise ValueError("invalid npm tree or version")
        lock, package = repo / args.tree / "package-lock.json", repo / args.tree / "package.json"
        if not lock.is_file() or not package.is_file():
            raise ValueError("missing npm inputs")
        return {"schema": "leaf.dep-cache.nm.v1", "tree": args.tree,
                "lock": hashlib.sha256(lock.read_bytes()).hexdigest(),
                "package": hashlib.sha256(package.read_bytes()).hexdigest(),
                "node": args.node_version, "npm": args.npm_version,
                "platform": sysconfig.get_platform(), "arch": platform.machine(), "os": os_version}
    if args.kind == "py":
        prefix = args.src if args.action == "publish" else args.dest
        if not prefix or not Path(prefix).is_absolute() or not re.fullmatch(r"\d{8}", args.day):
            raise ValueError("invalid Python prefix or day")
        files = {}
        for rel in DEPENDENCY_FILES[2:]:
            path = repo / rel
            files[rel] = hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else "absent"
        paths = sysconfig.get_paths()
        base = sorted({entry.name for key in ("purelib", "platlib")
                       for pattern in ("*.dist-info", "*.egg-info")
                       for entry in Path(paths[key]).glob(pattern)})
        return {"schema": "leaf.dep-cache.py.v1", "files": files,
                "python": platform.python_version(), "implementation": sys.implementation.name,
                "executable": os.path.realpath(sys.executable), "prefix": prefix,
                "platform": sysconfig.get_platform(), "os": os_version, "day": args.day, "base": base}
    lock = json.loads((repo / "web/package-lock.json").read_text())
    core = lock["packages"]["node_modules/playwright-core"]
    requirement = "absent"
    requirements = repo / "server/requirements.txt"
    for line in requirements.read_text().splitlines() if requirements.exists() else ():
        if re.fullmatch(r"playwright==[^\s;#]+", line.strip()):
            requirement = line.strip()
    return {"schema": "leaf.dep-cache.pw.v1",
            "node_playwright_core": core["version"] + core["integrity"],
            "python_playwright": requirement,
            "os": os_version,
            "arch": platform.machine()}


def parts(args):
    if args.kind == "dl":
        return {"pip": args.pip_cache, "npm": args.npm_cache}
    if args.kind == "as":
        return {"snapshot": args.src if args.action == "publish" else args.dest}
    if args.kind == "nm":
        return {"node_modules": str(Path(args.repo) / args.tree / "node_modules")}
    if args.kind == "py":
        return {"venv": args.src if args.action == "publish" else args.dest}
    location = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if not location:
        location = str(Path(os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache"))) / "ms-playwright")
    return {"ms-playwright": location}


def excluded(part, rel):
    names = rel.split("/")
    if part == "pip":
        return names[0] == "selfcheck"
    if part == "npm":
        return (names[0] in ("_logs", "_npx", "_update-notifier-last-checked")
                or names[:2] == ["_cacache", "tmp"])
    if part == "ms-playwright":
        return names[0] in (".links", "__dirlock") or names[-1] == "DEPENDENCIES_VALIDATED"
    if part == "node_modules":
        return names[0] in (".cache", ".vite", ".vitest")
    if part == "venv":
        return (rel == "pyvenv.cfg" or "__pycache__" in names
                or re.fullmatch(r"bin/python(?:3(?:\.\d+)?)?", rel) is not None)
    return names[0] not in ("manifest.json", "snapshot.bundle")


def bound(files, symlinks):
    if len(files) + len(symlinks) > MAX_FILES or sum(row[1] for row in files) > MAX_BYTES:
        raise OSError("entry bound exceeded")


def scan(sources, kind, revision):
    files, links = [], []
    total_bytes = 0
    def walk(root, directory, part):
        nonlocal total_bytes
        for path in sorted(directory.iterdir()):
            rel = path.relative_to(root).as_posix()
            if excluded(part, rel):
                continue
            name = safe_path(part + "/" + rel)
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode):
                target = os.readlink(path)
                safe_link(name, target)
                links.append([name, target])
            elif stat.S_ISDIR(info.st_mode):
                if kind == "pw" and directory == root and not (path / "INSTALLATION_COMPLETE").is_file():
                    raise Refused("incomplete browser")
                walk(root, path, part)
            elif stat.S_ISREG(info.st_mode):
                files.append([name, info.st_size, "", bool(info.st_mode & 0o111)])
                total_bytes += info.st_size
            else:
                raise Refused("special file")
            if len(files) + len(links) > MAX_FILES or total_bytes > MAX_BYTES:
                raise OSError("entry bound exceeded")
    for part, source in sources.items():
        if not source:
            continue
        root = Path(source)
        if root.is_symlink():
            raise Refused("symlink source")
        if root.exists():
            walk(root, root, part)
    validate_links(links)
    if kind == "as":
        try:
            manifest = json.loads((Path(sources["snapshot"]) / "manifest.json").read_text())
        except (OSError, ValueError) as exc:
            raise Refused("snapshot manifest") from exc
        if (not isinstance(manifest, dict) or manifest.get("head_sha") != revision
                or manifest.get("repository") != "LEAF-Solar-Design/autofill-solver"
                or not (Path(sources["snapshot"]) / "snapshot.bundle").is_file()):
            raise Refused("snapshot identity")
    return sorted(files), sorted(links)


def regular_source(root, rel):
    """Reject symlink ancestors and open the payload without following its leaf."""
    path = root
    for name in rel.split("/")[:-1]:
        path = path / name
        if not stat.S_ISDIR(path.lstat().st_mode):
            raise Corrupt("non-directory ancestor")
    if not stat.S_ISREG((root / rel).lstat().st_mode):
        raise Corrupt("non-regular payload")
    descriptor = os.open(root / rel, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                         | getattr(os, "O_NONBLOCK", 0))
    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        raise Corrupt("non-regular payload")
    return os.fdopen(descriptor, "rb")


def copy_file(source_root, rel, destination, row, verify):
    try:
        source = regular_source(source_root, rel)
    except (OSError, Corrupt) as exc:
        raise Corrupt("unreadable payload") from exc
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
    hasher, size = hashlib.sha256(), 0
    with source, open(destination, "xb") as output:
        while True:
            try:
                chunk = source.read(BUFFER_SIZE)
            except OSError as exc:
                raise Corrupt("unreadable payload") from exc
            if not chunk:
                break
            size += len(chunk)
            if size > row[1] or size > MAX_BYTES:
                raise Corrupt("size mismatch")
            output.write(chunk)
            hasher.update(chunk)
    if size != row[1] or (verify and hasher.hexdigest() != row[2]):
        raise Corrupt("payload mismatch")
    row[2] = hasher.hexdigest()
    destination.chmod(0o755 if row[3] else 0o644)


def load_meta(entry, kind, key=None):
    try:
        try:
            if not stat.S_ISDIR(entry.lstat().st_mode):
                raise Corrupt("non-directory entry")
            with regular_source(entry, "meta.json") as stream:
                # A malformed metadata file must not bypass the entry bounds.
                raw = stream.read(min(MAX_BYTES, 128 * 1024**2) + 1)
        except OSError as exc:
            raise Corrupt("unreadable metadata") from exc
        if len(raw) > min(MAX_BYTES, 128 * 1024**2):
            raise OSError("metadata bound")
        meta = json.loads(raw)
        if (meta["schema"] != "leaf.dep-cache.entry.v1" or meta["kind"] != kind
                or meta["inputs"]["schema"] != f"leaf.dep-cache.{kind}.v1"
                or meta["key"] != digest(meta["inputs"])
                or entry.name != f"{kind}-v1-{meta['key']}"
                or (key is not None and meta["key"] != key)):
            raise Corrupt("identity")
        files, links = meta["files"], meta["symlinks"]
        if not isinstance(files, list) or not isinstance(links, list):
            raise Corrupt("manifest lists")
        names = set()
        allowed = {"dl": {"pip", "npm"}, "pw": {"ms-playwright"}, "as": {"snapshot"},
                   "nm": {"node_modules"}, "py": {"venv"}}[kind]
        for row in files:
            if (not isinstance(row, list) or len(row) != 4 or type(row[1]) is not int or row[1] < 0
                    or not isinstance(row[2], str) or not re.fullmatch(r"[0-9a-f]{64}", row[2])
                    or type(row[3]) is not bool):
                raise Corrupt("file row")
        for row in links:
            if not isinstance(row, list) or len(row) != 2:
                raise Corrupt("link row")
            safe_path(row[0])
            safe_link(row[0], row[1])
        for row in files + links:
            name = safe_path(row[0])
            if name.split("/")[0] not in allowed or "/" not in name or name in names:
                raise Corrupt("manifest path")
            names.add(name)
        validate_links(links)
        for name in names:
            if any(str(parent) in names for parent in PurePosixPath(name).parents):
                raise Corrupt("manifest ancestor collision")
        bound(files, links)
        if (files != sorted(files) or links != sorted(links)
                or meta["list_sha256"] != digest({"files": files, "symlinks": links})
                or meta["total_bytes"] != sum(row[1] for row in files)
                or meta["file_count"] != len(files) + len(links)):
            raise Corrupt("manifest checksum")
        return meta
    except (KeyError, TypeError, ValueError, UnicodeError) as exc:
        raise Corrupt("invalid metadata") from exc
    except FileNotFoundError as exc:
        raise Corrupt("missing metadata") from exc


def seed(args, cache, entry, inputs, report):
    meta = None
    exact_error = None
    if entry.exists() or entry.is_symlink():
        try:
            meta = load_meta(entry, args.kind, digest(inputs))
            report["result"] = "exact"
        except Corrupt as exc:
            if args.kind != "dl" or args.writable != "0":
                raise
            exact_error = exc
    if meta is None and args.kind == "dl" and args.writable == "0" and cache.is_dir():
        for candidate in sorted(cache.iterdir(), key=lambda p: p.lstat().st_mtime, reverse=True):
            if not ENTRY_RE.fullmatch(candidate.name) or not candidate.name.startswith("dl-"):
                continue
            try:
                candidate_meta = load_meta(candidate, "dl")
                if all(candidate_meta["inputs"].get(k) == inputs[k] for k in ("python", "platform", "node")):
                    entry, meta = candidate, candidate_meta
                    report["result"] = "nearest"
                    break
            except (Corrupt, OSError):
                continue
    if meta is None:
        if exact_error is not None:
            raise exact_error
        return
    report["entry"] = entry.name
    staged, committed = [], []
    try:
        for part, location in parts(args).items():
            if not location:
                report[part] = "skipped"
                continue
            destination = Path(location)
            if destination.resolve().is_relative_to(cache.resolve()):
                raise OSError("destination overlaps persistent cache")
            if destination.exists() or destination.is_symlink():
                report[part] = "present"
                continue
            destination.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
            temporary = destination.with_name(destination.name + f".leaf-seed-{os.getpid()}")
            temporary.mkdir(mode=0o755)
            staged.append((temporary, destination, part))
            rows = [row[:] for row in meta["files"] if row[0].startswith(part + "/")]
            with ThreadPoolExecutor(max_workers=min(4, os.cpu_count() or 1)) as pool:
                futures = [pool.submit(copy_file, entry, row[0], temporary / row[0].split("/", 1)[1], row, True)
                           for row in rows]
                for future in futures:
                    future.result()
            for name, target in meta["symlinks"]:
                if name.startswith(part + "/"):
                    link = temporary / name.split("/", 1)[1]
                    link.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
                    link.symlink_to(target)
        # Verify every part before exposing any of them.
        for temporary, destination, part in staged:
            os.rename(temporary, destination)
            committed.append(destination)
            report[part] = "seeded"
        if args.writable == "1" and report["result"] == "exact":
            os.utime(entry, None)
        report.update(files=meta["file_count"], bytes=meta["total_bytes"])
    except Exception:
        for destination in committed:
            shutil.rmtree(destination)
        raise
    finally:
        for temporary, _, _ in staged:
            if temporary.exists():
                shutil.rmtree(temporary)


def publish(args, cache, entry, inputs, report):
    if args.writable != "1":
        report["result"] = "refused"
        return
    if entry.exists() or entry.is_symlink():
        report["result"] = "exists"
        return
    sources = parts(args)
    try:
        files, links = scan(sources, args.kind, args.revision)
    except (Corrupt, Refused):
        report["result"] = "refused"
        return
    temporary = cache / (entry.name + ".tmp-" + run_id(args.run))
    cache.mkdir(parents=True, exist_ok=True, mode=0o755)
    if temporary.is_symlink():
        raise OSError("unsafe temporary entry")
    if temporary.exists():
        shutil.rmtree(temporary)
    temporary.mkdir(mode=0o755)
    try:
        for part, source in sources.items():
            (temporary / part).mkdir(mode=0o755)
        with ThreadPoolExecutor(max_workers=min(4, os.cpu_count() or 1)) as pool:
            futures = []
            for row in files:
                part, rel = row[0].split("/", 1)
                futures.append(pool.submit(copy_file, Path(sources[part]), rel, temporary / row[0], row, False))
            for future in futures:
                future.result()
        for name, target in links:
            path = temporary / name
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
            path.symlink_to(target)
        meta = {"schema": "leaf.dep-cache.entry.v1", "kind": args.kind, "key": digest(inputs),
                "inputs": inputs, "run": args.run, "source_sha": os.environ.get("HEAD_SHA", ""),
                "created": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "files": files, "symlinks": links,
                "total_bytes": sum(row[1] for row in files), "file_count": len(files) + len(links),
                "list_sha256": digest({"files": files, "symlinks": links})}
        (temporary / "meta.json").write_bytes(canonical(meta))
        try:
            os.rename(temporary, entry)
        except OSError as exc:
            if exc.errno not in (errno.EEXIST, errno.ENOTEMPTY):
                raise
            report["result"] = "lost"
            return
        report.update(result="published", files=meta["file_count"], bytes=meta["total_bytes"])
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)


def run_id(value):
    return re.sub(r"[^A-Za-z0-9._-]", "_", value)[:64] or "local"


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("action", choices=("seed", "publish"))
    result.add_argument("--kind", required=True, choices=("dl", "pw", "as", "nm", "py"))
    result.add_argument("--tree", choices=("web", "harness"))
    result.add_argument("--repo", default=".")
    result.add_argument("--cache-dir", default="")
    result.add_argument("--writable", required=True, choices=("0", "1"))
    result.add_argument("--run", required=True)
    result.add_argument("--day", default="")
    result.add_argument("--pip-cache", default="")
    result.add_argument("--npm-cache", default="")
    result.add_argument("--node-version", default="")
    result.add_argument("--npm-version", default="")
    result.add_argument("--os-release", default="/etc/os-release")
    result.add_argument("--revision", default="")
    result.add_argument("--dest", default="")
    result.add_argument("--src", default="")
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    started = time.monotonic()
    report = dict(kind=args.kind, action=args.action, result="miss", entry="-", files=0, bytes=0)
    entry = None
    try:
        cache = Path(args.cache_dir)
        if (not args.cache_dir or os.environ.get("LEAF_DEP_CACHE_OFF") == "1"
                or (cache / ".leaf-dep-cache-off").exists()
                or (cache / f".leaf-dep-cache-off-{args.kind}").exists()
                or (args.kind == "pw" and os.environ.get("PLAYWRIGHT_BROWSERS_PATH") == "0")):
            report["result"] = "off"
        else:
            inputs = key_inputs(args)
            entry = cache / f"{args.kind}-v1-{digest(inputs)}"
            report["entry"] = entry.name
            if args.action == "seed":
                seed(args, cache, entry, inputs, report)
            else:
                publish(args, cache, entry, inputs, report)
    except Corrupt:
        report["result"] = "corrupt" if args.action == "seed" else "refused"
        if args.action == "seed" and args.writable == "1" and entry is not None:
            try:
                os.rename(entry, entry.with_name(entry.name + ".tmp-corrupt-" + run_id(args.run)))
            except OSError:
                pass
    except Exception:
        report["result"] = "error"
    if report["result"] in ("corrupt", "error"):
        for part in ("pip", "npm", "ms-playwright", "snapshot", "node_modules", "venv"):
            if report.get(part) == "seeded":
                report.pop(part)
    report["ms"] = int((time.monotonic() - started) * 1000)
    print("LEAF_DEP_CACHE " + " ".join(f"{key}={value}" for key, value in report.items()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
