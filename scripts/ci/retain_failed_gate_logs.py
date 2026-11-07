"""Retain bounded failed-suite evidence; never follow result-provided paths."""
import argparse
from contextlib import contextmanager, nullcontext
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
from urllib.parse import parse_qsl, unquote_plus, urlencode, urlsplit, urlunsplit

RESULT_LIMIT = 8 * 1024 * 1024
TOTAL_LIMIT = 2 * 1024 * 1024
FILE_LIMIT = 256 * 1024
FILE_COUNT_LIMIT = 128
SUITE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")
STDOUT_LIMIT = TOTAL_LIMIT
CHUNK_CHARS = 4096
_PRIVATE_CONTAINER = re.compile(
    r'''(?i)\b(?:\w*context|env|environ|environment|headers|cookies|params|payload|body|storage_state)["']?\s*[:=]\s*([\[{])''')
_PRIVATE_ASSIGNMENT = re.compile(
    r'''(?im)(\b(?:\w*(?:token|secret|password|passwd|credential|dsn|api[_-]?key|access[_-]?key|private[_-]?key)\w*|database[_-]?url|authorization|proxy-authorization|cookie|set-cookie|tenant_id|org_id|project_id|actor_id|subject|\w*context|env|environ|environment)["']?\s*[:=])[^\r\n]*''')
_SIGNED_QUERY = re.compile(r"(?i)^(?:x-amz-.+|x-goog-.+|signature|sig|token|access_token|id_token|refresh_token|api[_-]?key|key|credential|password|code|se|sp|sv|sr)$")
_URL = re.compile(r'''\b[a-zA-Z][a-zA-Z0-9+.-]*://[^\s<>"']+''')


def _redact_containers(text):
    """Drop complete private dumps, including nested or unfinished containers."""
    parts, position = [], 0
    while match := _PRIVATE_CONTAINER.search(text, position):
        parts.append(text[position:match.start(1)])
        start, stack, quote, escaped = match.start(1), [], None, False
        end = len(text)
        for index in range(start, len(text)):
            char = text[index]
            if quote:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == quote:
                    quote = None
            elif char in "\"'":
                quote = char
            elif char in "[{":
                stack.append(char)
            elif char in "]}":
                if not stack or (stack[-1], char) not in (("[", "]"), ("{", "}")):
                    break
                stack.pop()
                if not stack:
                    end = index + 1
                    break
        parts.append("[REDACTED_PRIVATE_CONTEXT]")
        position = end
    parts.append(text[position:])
    return "".join(parts)


def sanitize_log(text):
    """Frozen stdout policy: diagnostic prose only, never private dump values."""
    text = re.sub(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))", "", text)
    text = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", text.replace("\r\n", "\n"))
    text = re.sub(r"-----BEGIN[^\n]*PRIVATE KEY-----[\s\S]*?(?:-----END[^\n]*PRIVATE KEY-----|\Z)",
                  "[REDACTED_PRIVATE_KEY]", text)
    text = _redact_containers(text)

    # Request diagnostics also print relative URLs, not only scheme:// URLs.
    def query(match):
        separator, key, value = match.groups()
        return separator + key + '=' + ('[REDACTED]' if _SIGNED_QUERY.fullmatch(unquote_plus(key)) else value)

    text = re.sub(r'''([?&])([^=&#\s"']+)=([^&#\s"']*)''', query, text)

    def url(match):
        try:
            value = urlsplit(match.group())
            if value.username is not None or value.scheme.lower() in {
                    "postgres", "postgresql", "mysql", "mongodb", "mongodb+srv", "redis", "rediss", "amqp", "amqps"}:
                return "[REDACTED_URI_CREDENTIALS]"
            query = parse_qsl(value.query, keep_blank_values=True)
            if any(_SIGNED_QUERY.fullmatch(key) for key, _ in query):
                return urlunsplit((value.scheme, value.netloc, value.path,
                    urlencode([(key, "[REDACTED]" if _SIGNED_QUERY.fullmatch(key) else data)
                               for key, data in query]), ""))
            return match.group()
        except ValueError:
            return "[REDACTED_URL]"

    text = _URL.sub(url, text)
    text = _PRIVATE_ASSIGNMENT.sub(r"\1 [REDACTED]", text)
    # An environment listing need not contain a recognisable credential key.
    text = re.sub(r"(?m)(\b[A-Z][A-Z0-9_]*\s*=)[^\r\n]*", r"\1[REDACTED_ENV]", text)
    text = re.sub(r"(?i)\b(?:bearer|basic)\s+[A-Za-z0-9._~+/=-]+", "[REDACTED_AUTH]", text)
    text = re.sub(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b", "[REDACTED_JWT]", text)
    text = re.sub(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b|\bgh[pousr]_[A-Za-z0-9_]{10,}\b", "[REDACTED_TOKEN]", text)
    return text


def emit_failed_logs(manifest, output_dir, source, build_id, stream=None):
    """Publish only verified retained copies, with a separate redacted identity."""
    if not manifest["failed_suites"]:
        return None
    if (not re.fullmatch(r"[0-9a-f]{40}", source)
            or not re.fullmatch(r"(?:[A-Za-z0-9][A-Za-z0-9_.-]{0,127}:[0-9a-fA-F-]{16,64}|device:dv-[0-9a-f]{16})", build_id)):
        raise ValueError("invalid_publication_identity")
    public = {"schema": "leaf.ci.failed-logs.stdout.v1", "source_sha": source,
              "build_id": build_id, "failed_suites": manifest["failed_suites"],
              "retained_bytes": manifest["copied_bytes"], "files": [],
              "omitted_candidates": manifest["omitted_candidates"]}
    lines, wire_bytes = [], 0
    # Reserve enough for every bounded manifest entry, even when content fills up.
    reserve = FILE_COUNT_LIMIT * 1536 + 4096
    with _directory(output_dir) as directory:
        for entry in manifest["files"][:FILE_COUNT_LIMIT]:
            suite, name = entry["suite_id"], entry["copy"]
            item = {key: entry[key] for key in ("suite_id", "source", "status", "source_sha256",
                                                "copy_sha256", "original_size", "copied_size", "truncated")}
            public["files"].append(item)
            if entry["status"] != "copied":
                continue
            if (not SUITE_ID.fullmatch(suite) or not isinstance(name, str)
                    or not re.fullmatch(re.escape(suite) + r"(?:\.retry[1-9][0-9]{0,5})?\.log", name)):
                raise ValueError("unsafe_publication_copy")
            try:
                with _open(directory, name) as opened:
                    before = os.fstat(opened.fileno())
                    if before.st_size > FILE_LIMIT or before.st_size != entry["copied_size"]:
                        raise ValueError("publication_size_changed")
                    raw = opened.read(FILE_LIMIT + 1)
                    after = os.fstat(opened.fileno())
                if (len(raw) != before.st_size or (before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                        != (after.st_size, after.st_mtime_ns, after.st_ctime_ns)
                        or hashlib.sha256(raw).hexdigest() != entry["copy_sha256"]):
                    raise ValueError("publication_copy_changed")
            except (OSError, ValueError):
                item["publication_status"] = "copy_refused"
                continue
            # The start of a retained tail can be the middle of a credential.
            if entry["truncated"]:
                raw = raw.partition(b"\n")[2]
            text = sanitize_log(raw.decode("utf-8", errors="replace"))
            item.update(redacted_sha256=hashlib.sha256(text.encode()).hexdigest(),
                        redacted_bytes=len(text.encode()), partial_first_line_dropped=entry["truncated"])
            published = []
            for index, start in enumerate(range(0, len(text), CHUNK_CHARS)):
                chunk = text[start:start + CHUNK_CHARS]
                line = "LEAF_FAILED_LOG_CHUNK " + json.dumps({
                    "source_sha": source, "build_id": build_id, "suite_id": suite,
                    "log_name": name, "chunk_index": index, "text": chunk}, sort_keys=True)
                size = len((line + "\n").encode())
                if wire_bytes + size > STDOUT_LIMIT - reserve:
                    break
                lines.append(line)
                wire_bytes += size
                published.append(chunk)
            emitted = "".join(published).encode()
            item.update(published_sha256=hashlib.sha256(emitted).hexdigest(), published_bytes=len(emitted),
                        publication_status="published", publication_truncated=len(emitted) < len(text.encode()))
    lines.append("LEAF_FAILED_LOG_MANIFEST " + json.dumps(public, sort_keys=True))
    encoded = "\n".join(lines) + "\n"
    if len(encoded.encode()) > STDOUT_LIMIT:
        raise ValueError("stdout_budget_exceeded")
    (stream or sys.stdout).write(encoded)
    return public


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
    parser.add_argument("--emit-stdout", action="store_true")
    parser.add_argument("--source", default="")
    parser.add_argument("--build-id", default="")
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
    if args.emit_stdout:
        try:
            emit_failed_logs(manifest, args.output, args.source, args.build_id)
        except (OSError, ValueError):
            print("failed_log_publication error=retained_copy_or_identity_refused")
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
