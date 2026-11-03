"""Retain bounded failed-suite evidence; never follow result-provided paths."""
import argparse
from contextlib import contextmanager, nullcontext
import hashlib
import json
import os
from pathlib import Path
import re
import stat

RESULT_LIMIT = 8 * 1024 * 1024
TOTAL_LIMIT = 2 * 1024 * 1024
FILE_LIMIT = 256 * 1024
FILE_COUNT_LIMIT = 128
SUITE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")


def _checked_path(path):
    path = Path(os.path.abspath(path))
    for part in [*reversed(path.parents), path]:
        info = part.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(part, "is_junction", lambda: False)():
            raise ValueError("unsafe_path")
    return path


@contextmanager
def _directory(path):
    """Pin every directory component where descriptor-relative opens exist."""
    path = _checked_path(path)
    if not path.is_dir():
        raise ValueError("not_directory")
    fd = None
    try:
        if os.open in os.supports_dir_fd:
            flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            fd = os.open(path.anchor, flags)
            for part in path.parts[1:]:
                child = os.open(part, flags, dir_fd=fd)
                os.close(fd)
                fd = child
        yield path, fd
    finally:
        if fd is not None:
            os.close(fd)


def _open(directory, name, write=False):
    path, fd = directory
    flags = getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    if write:
        flags |= os.O_WRONLY | os.O_CREAT | os.O_EXCL
    else:
        flags |= os.O_RDONLY | getattr(os, "O_NONBLOCK", 0)
    if fd is None:
        _checked_path(path)
        if not write:
            info = _checked_path(path / name).lstat()
            if not stat.S_ISREG(info.st_mode):
                raise ValueError("not_regular")
        opened = os.open(path / name, flags, 0o600)
    else:
        opened = os.open(name, flags, 0o600, dir_fd=fd)
    try:
        opened_info = os.fstat(opened)
        if not stat.S_ISREG(opened_info.st_mode):
            raise ValueError("not_regular")
        if fd is None and not write and (info.st_dev, info.st_ino) != (
                opened_info.st_dev, opened_info.st_ino):
            raise ValueError("source_changed")
        return os.fdopen(opened, "wb" if write else "rb")
    except BaseException:
        os.close(opened)
        raise


def _result(path):
    path = Path(os.path.abspath(path))
    with _directory(path.parent) as directory:
        with _open(directory, path.name) as stream:
            if os.fstat(stream.fileno()).st_size > RESULT_LIMIT:
                raise ValueError("result_too_large")
            data = stream.read(RESULT_LIMIT + 1)
    if len(data) > RESULT_LIMIT:
        raise ValueError("result_too_large")
    try:
        payload = json.loads(data)
    except (ValueError, UnicodeError, RecursionError):
        raise ValueError("malformed_result") from None
    if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
        raise ValueError("malformed_result")
    failed = []
    seen = set()
    for row in payload["results"]:
        if not isinstance(row, dict):
            raise ValueError("malformed_result")
        if row.get("status") != "FAIL":
            continue
        suite = row.get("id")
        attempts = row.get("attempts", 1)
        if not isinstance(suite, str) or not SUITE_ID.fullmatch(suite):
            raise ValueError("unsafe_suite_id")
        if type(attempts) is not int or not 1 <= attempts <= 1000000 or suite in seen:
            raise ValueError("invalid_attempts_or_duplicate")
        seen.add(suite)
        failed.append((suite, attempts))
    return failed


def _snapshot(directory, name, limit):
    with _open(directory, name) as stream:
        before = os.fstat(stream.fileno())
        digest = hashlib.sha256()
        remaining = before.st_size
        while remaining:
            chunk = stream.read(min(65536, remaining))
            if not chunk:
                raise ValueError("source_changed")
            digest.update(chunk)
            remaining -= len(chunk)
        stream.seek(max(0, before.st_size - limit))
        tail = stream.read(min(limit, before.st_size))
        after = os.fstat(stream.fileno())
        if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise ValueError("source_changed")
    return before.st_size, digest.hexdigest(), tail


def retain_failed_logs(result_path, log_dir, output_dir):
    """Create a fresh job output directory or refuse collisions without cleanup.

    Unsafe/malformed inputs raise ValueError/OSError. Missing individual logs and
    rejected special files are manifest entries, never reasons to change a gate.
    """
    failed = _result(result_path)
    output = Path(os.path.abspath(output_dir))
    latest = [(suite, f"{suite}.retry{attempts - 1}.log" if attempts > 1 else f"{suite}.log")
              for suite, attempts in failed]
    originals = [(suite, f"{suite}.log") for suite, attempts in failed if attempts > 1]
    candidates = latest + originals
    manifest = {"schema": 1, "failed_suites": len(failed), "files": [],
                "copied_files": 0, "copied_bytes": 0,
                "omitted_candidates": max(0, len(candidates) - FILE_COUNT_LIMIT)}
    with (_directory(log_dir) if candidates else nullcontext(None)) as logs, _directory(output.parent) as parent:
        try:
            if parent[1] is None:
                _checked_path(parent[0])
                output.mkdir(mode=0o700)
            else:
                os.mkdir(output.name, mode=0o700, dir_fd=parent[1])
        except FileExistsError:
            raise ValueError("output_collision") from None
        with _directory(output) as destination:
            identities = {}
            for suite, name in candidates[:FILE_COUNT_LIMIT]:
                entry = {"suite_id": suite, "source": name, "copy": None,
                         "source_sha256": None, "copy_sha256": None,
                         "original_size": None, "copied_size": 0, "truncated": False}
                manifest["files"].append(entry)
                budget = min(FILE_LIMIT, TOTAL_LIMIT - manifest["copied_bytes"])
                try:
                    size, digest, tail = _snapshot(logs, name, budget)
                except FileNotFoundError:
                    entry["status"] = "missing"
                    continue
                except (OSError, ValueError):
                    entry["status"] = "rejected"
                    continue
                entry.update(original_size=size, source_sha256=digest)
                if suite in identities and identities[suite] == digest:
                    entry["status"] = "identical_to_latest"
                    continue
                identities[suite] = digest
                entry["truncated"] = len(tail) < size
                if not budget:
                    entry["status"] = "budget_exhausted"
                    continue
                try:
                    with _open(destination, name, write=True) as stream:
                        stream.write(tail)
                except FileExistsError:
                    entry["status"] = "copy_collision"
                    continue
                entry.update(status="copied", copy=name, copied_size=len(tail),
                             copy_sha256=hashlib.sha256(tail).hexdigest())
                manifest["copied_files"] += 1
                manifest["copied_bytes"] += len(tail)
            with _open(destination, "manifest.json", write=True) as stream:
                stream.write((json.dumps(manifest, sort_keys=True) + "\n").encode("utf-8"))
    return manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--result", required=True)
    parser.add_argument("--logs", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    try:
        manifest = retain_failed_logs(args.result, args.logs, args.output)
    except (OSError, ValueError):
        # Never echo paths, malformed JSON, exception messages, or log contents.
        print("failed_log_retention error=input_or_output_refused")
        return 1
    print("failed_log_retention " + json.dumps({
        "failed_suites": manifest["failed_suites"], "copied_files": manifest["copied_files"],
        "copied_bytes": manifest["copied_bytes"],
        "missing": sum(e["status"] == "missing" for e in manifest["files"]),
        "rejected": sum(e["status"] == "rejected" for e in manifest["files"]),
        "truncated": sum(e["truncated"] for e in manifest["files"]),
        "omitted_candidates": manifest["omitted_candidates"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
