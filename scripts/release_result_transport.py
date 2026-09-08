"""Immutable ordinary release transport, not completed-producer acceptance.

Descriptors prove content and transport identity only. Consumers must separately
accept the producer and retain the strict v3 manifest semantic verification.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile
import zipfile

BUCKET = "leaf-mq-transport-807034087062-us-east-1"
PREFIX = "mq/leaf-web-demo/release-results/"
KINDS = frozenset({"service-app", "service-broker", "service-canonical-worker",
                   "service-harness", "service-web", "surface-web-dist",
                   "staging-supply-set", "web-dist"})
ARCHIVE_BYTES = 128 * 1024 * 1024
EXPANDED_BYTES = 256 * 1024 * 1024
FILES = 10000
JSON_BYTES = 2 * 1024 * 1024


class TransportError(ValueError):
    pass


def identity(source, run, attempt, kind):
    if not isinstance(source, str) or not re.fullmatch(r"[0-9a-f]{40}", source):
        raise TransportError("source must be lowercase40")
    if any(not re.fullmatch(r"[1-9][0-9]*", str(v)) for v in (run, attempt)):
        raise TransportError("run and attempt must be positive integers")
    if kind not in KINDS:
        raise TransportError("unknown artifact kind")
    return {"source": source, "run": str(run), "attempt": str(attempt), "kind": kind}


def is_archive(kind):
    return kind in {"surface-web-dist", "web-dist"}


def key_for(meta):
    suffix = "zip" if is_archive(meta["kind"]) else "json"
    return f'{PREFIX}{meta["source"]}/{meta["run"]}/{meta["kind"]}/{meta["attempt"]}.{suffix}'


def checksum(data):
    return base64.b64encode(hashlib.sha256(data).digest()).decode("ascii")


def safe_name(name):
    if (not name or "\\" in name or ":" in name
            or any(ord(c) < 32 for c in name)
            or any(p in {"", ".", ".."} or p.endswith((" ", "."))
                   or p.split(".")[0].upper() in
                   {"CON", "PRN", "AUX", "NUL", *[f"COM{i}" for i in range(10)],
                    *[f"LPT{i}" for i in range(10)]} for p in name.split("/"))):
        raise TransportError("unsafe member path")


def safe_destination(path):
    path = Path(os.path.abspath(path))
    for part in [*reversed(path.parents), path]:
        if part.is_symlink() or (hasattr(part, "is_junction") and part.is_junction()):
            raise TransportError("link destination")
    return path


def archive_members(data):
    if len(data) > ARCHIVE_BYTES:
        raise TransportError("compressed bound exceeded")
    result = {}
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            entries = archive.infolist()
            if not entries or len(entries) > FILES:
                raise TransportError("file count bound exceeded")
            names = set()
            total = 0
            for entry in entries:
                name = entry.filename
                safe_name(name)
                folded = name.casefold()
                if folded in names:
                    raise TransportError("duplicate member")
                names.add(folded)
                if (entry.orig_filename != name or entry.is_dir() or entry.flag_bits & 1
                        or stat.S_IFMT(entry.external_attr >> 16) not in {0, stat.S_IFREG}):
                    raise TransportError("nonregular archive member")
                total += entry.file_size
                if total > EXPANDED_BYTES:
                    raise TransportError("expanded bound exceeded")
            for name in names:
                if any('/'.join(name.split('/')[:i]) in names
                       for i in range(1, len(name.split('/')))):
                    raise TransportError("member parent is a file")
            for entry in entries:
                with archive.open(entry) as member:
                    payload = member.read(entry.file_size + 1)
                if len(payload) != entry.file_size:
                    raise TransportError("expanded size mismatch")
                result[entry.filename] = payload
    except (zipfile.BadZipFile, RuntimeError, NotImplementedError, EOFError) as exc:
        raise TransportError("invalid archive") from exc
    return result


def validate_payload(data, kind):
    if is_archive(kind):
        return archive_members(data)
    if len(data) > JSON_BYTES:
        raise TransportError("JSON bound exceeded")
    try:
        json.loads(data)
    except (ValueError, UnicodeError) as exc:
        raise TransportError("invalid JSON") from exc
    return None


def package(path, kind):
    path = safe_destination(path)
    if not is_archive(kind):
        if not path.is_file():
            raise TransportError("expected regular JSON file")
        with path.open("rb") as source:
            data = source.read(JSON_BYTES + 1)
    else:
        if not path.is_dir():
            raise TransportError("expected directory")
        files = []
        for root, dirs, names in os.walk(path, followlinks=False):
            for name in dirs + names:
                candidate = safe_destination(Path(root) / name)
                mode = candidate.stat().st_mode
                if not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
                    raise TransportError("nonregular input")
            files.extend(Path(root) / name for name in names)
            if len(files) > FILES:
                raise TransportError("file count bound exceeded")
        total = 0
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for candidate in sorted(files):
                name = candidate.relative_to(path).as_posix()
                safe_name(name)
                with candidate.open("rb") as source:
                    payload = source.read(EXPANDED_BYTES - total + 1)
                total += len(payload)
                if total > EXPANDED_BYTES:
                    raise TransportError("expanded bound exceeded")
                entry = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
                entry.create_system = 3
                entry.external_attr = (stat.S_IFREG | 0o644) << 16
                entry.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(entry, payload)
                if buffer.tell() > ARCHIVE_BYTES:
                    raise TransportError("compressed bound exceeded")
        data = buffer.getvalue()
    validate_payload(data, kind)
    return data


def aws(operation, *args):
    completed = subprocess.run(
        ["aws", "s3api", operation, *map(str, args), "--region", "us-east-1",
         "--output", "json", "--no-cli-pager"], capture_output=True, text=True,
        check=True, timeout=180)
    return json.loads(completed.stdout)


def read_object(meta, call=aws):
    key = key_for(meta)
    head = call("head-object", "--bucket", BUCKET, "--key", key,
                "--checksum-mode", "ENABLED")
    bound = ARCHIVE_BYTES if is_archive(meta["kind"]) else JSON_BYTES
    if (type(head.get("ContentLength")) is not int
            or not 0 <= head["ContentLength"] <= bound
            or head.get("Metadata") != meta or not head.get("ChecksumSHA256")):
        raise TransportError("head identity, checksum or size mismatch")
    binding = (["--version-id", head["VersionId"]] if head.get("VersionId")
               else ["--if-match", head.get("ETag", "")])
    if not binding[1]:
        raise TransportError("missing immutable read binding")
    with tempfile.TemporaryDirectory() as temp:
        output = Path(temp) / "object"
        response = call("get-object", "--bucket", BUCKET, "--key", key,
                        "--checksum-mode", "ENABLED", *binding, str(output))
        with output.open("rb") as source:
            data = source.read(bound + 1)
    if (len(data) != head["ContentLength"] or len(data) > bound
            or response.get("Metadata") != meta
            or response.get("ChecksumSHA256") != checksum(data)
            or head["ChecksumSHA256"] != checksum(data)
            or response.get("ContentLength") != len(data)
            or (head.get("VersionId") and response.get("VersionId") != head["VersionId"])
            or (not head.get("VersionId") and response.get("ETag") != head.get("ETag"))):
        raise TransportError("object identity or checksum mismatch")
    validate_payload(data, meta["kind"])
    descriptor = {"scope": "content-and-transport-identity-only", "bucket": BUCKET,
                  "key": key, **meta, "sha256": hashlib.sha256(data).hexdigest(),
                  "checksum_sha256": checksum(data)}
    if response.get("VersionId"):
        descriptor["version_id"] = response["VersionId"]
    return data, descriptor


def put(path, source, run, attempt, kind, call=aws):
    meta = identity(source, run, attempt, kind)
    data = package(path, kind)
    with tempfile.TemporaryDirectory() as temp:
        body = Path(temp) / "body"
        body.write_bytes(data)
        try:
            call("put-object", "--bucket", BUCKET, "--key", key_for(meta),
                 "--body", str(body), "--if-none-match", "*",
                 "--checksum-sha256", checksum(data), "--metadata", json.dumps(meta))
        except subprocess.CalledProcessError as exc:
            if "PreconditionFailed" not in (exc.stderr or "") and "ConditionalRequestConflict" not in (exc.stderr or ""):
                raise
    stored, descriptor = read_object(meta, call)
    if stored != data:
        raise TransportError("immutable object differs")
    return descriptor


def get(path, source, run, attempt, kind, call=aws):
    meta = identity(source, run, attempt, kind)
    prefix = key_for(meta).rsplit("/", 1)[0] + "/"
    listing = call("list-objects-v2", "--bucket", BUCKET, "--prefix", prefix,
                   "--max-keys", "1000", "--no-paginate")
    entries = listing.get("Contents", [])
    if listing.get("IsTruncated") or len(entries) > 1000:
        raise TransportError("listing exceeds bound")
    candidates = []
    for entry in entries:
        key = entry.get("Key", "")
        match = re.fullmatch(re.escape(prefix) + r"([1-9][0-9]*)\." +
                             ("zip" if is_archive(kind) else "json"), key)
        if match and int(match[1]) <= int(attempt):
            candidates.append(int(match[1]))
    if not candidates:
        raise TransportError("no matching artifact")
    meta["attempt"] = str(max(candidates))
    data, descriptor = read_object(meta, call)
    destination = safe_destination(path)
    if is_archive(kind):
        members = archive_members(data)
        # Complete archive and destination validation precedes every write.
        for name in members:
            target = safe_destination(destination / name)
            if target.exists() and not target.is_file():
                raise TransportError("destination is not a regular file")
            for parent in target.parents:
                if parent.exists() and not parent.is_dir():
                    raise TransportError("destination parent is not a directory")
        for name, payload in members.items():
            target = destination / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(payload)
    else:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
    return descriptor


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("put", "get"))
    for name in ("path", "source", "run", "attempt", "kind"):
        parser.add_argument("--" + name, required=True)
    args = vars(parser.parse_args())
    operation = args.pop("operation")
    print(json.dumps((put if operation == "put" else get)(**args), sort_keys=True))


if __name__ == "__main__":
    main()
