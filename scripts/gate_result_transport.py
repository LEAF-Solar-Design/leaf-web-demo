"""Bounded, same-run gate evidence transport. Gate semantics stay in run-all-gates."""

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile


BUCKET = "leaf-mq-transport-807034087062-us-east-1"
PREFIX = "mq/leaf-web-demo/gate-results"
SHARDS = 8
MAX_BODY = 1024 * 1024
MAX_LOGS = 1024 * 1024
MAX_PAYLOAD = 8 * 1024 * 1024
MAX_OBJECTS = 1000
MAX_LOG_FILES = 256


def require(condition, message):
    if not condition:
        raise ValueError(message)


def identity(source, run, attempt, shard):
    require(isinstance(source, str) and re.fullmatch(r"[0-9a-f]{40}", source),
            "invalid source")
    require(isinstance(run, str) and re.fullmatch(r"[1-9][0-9]{0,19}", run),
            "invalid run")
    require(type(attempt) is int and 1 <= attempt <= 10000, "invalid attempt")
    require(shard == "proof" or (type(shard) is int and 0 <= shard < SHARDS),
            "invalid shard")
    return {"source": source, "run": run, "attempt": attempt, "shard": str(shard)}


def root(source, run):
    identity(source, run, 1, 0)
    return f"{PREFIX}/{source}/{run}/"


def object_key(meta):
    lane = "proof" if meta["shard"] == "proof" else f"shards/{meta['shard']}"
    return f"{root(meta['source'], meta['run'])}{lane}/attempt-{meta['attempt']}.json"


def checksum(payload):
    return base64.b64encode(hashlib.sha256(payload).digest()).decode("ascii")


def read_bounded(path, limit):
    with Path(path).open("rb") as stream:
        data = stream.read(limit + 1)
    require(len(data) <= limit, "payload exceeds bound")
    return data


def collect_logs(directory):
    """Keep UTF-8 text only, never archives or arbitrary result-provided paths."""
    logs, remaining, truncated = {}, MAX_LOGS, False
    if directory is None or not Path(directory).exists():
        return logs, truncated
    # run-all-gates writes flat per-suite *.log files, including retry logs.
    count = 0
    for path in sorted(Path(directory).glob("*.log")):
        count += 1
        if count > MAX_LOG_FILES or remaining == 0:
            truncated = True
            break
        if path.is_symlink() or not path.is_file():
            continue
        with path.open("rb") as stream:
            raw = stream.read(remaining + 1)
        text = raw[:remaining].decode("utf-8", errors="replace")
        encoded = text.encode("utf-8")
        kept = encoded[:remaining].decode("utf-8", errors="ignore")
        truncated |= len(raw) > remaining or len(encoded) > remaining
        logs[path.name] = kept
        remaining -= len(kept.encode("utf-8"))
    return logs, truncated


def pack(body, meta, logs=None, truncated=False):
    require(isinstance(body, bytes) and 0 < len(body) <= MAX_BODY, "invalid body size")
    envelope = {"schema": 1, **meta, "body": body.decode("utf-8"),
                "body_sha256": hashlib.sha256(body).hexdigest(),
                "logs": logs or {}, "logs_truncated": truncated}
    payload = json.dumps(envelope, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    require(len(payload) <= MAX_PAYLOAD, "payload exceeds bound")
    unpack(payload, meta)  # Apply the same shape and size rules before upload.
    return payload


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, "duplicate envelope key")
        result[key] = value
    return result


def unpack(payload, expected):
    require(isinstance(payload, bytes) and len(payload) <= MAX_PAYLOAD,
            "payload exceeds bound")
    data = json.loads(payload.decode("utf-8"), object_pairs_hook=unique_object)
    require(isinstance(data, dict) and set(data) == {
        "schema", "source", "run", "attempt", "shard", "body", "body_sha256",
        "logs", "logs_truncated"}, "unknown or missing envelope keys")
    require(type(data["schema"]) is int and data["schema"] == 1, "invalid schema")
    require(type(data["attempt"]) is int, "invalid attempt")
    require(all(data[k] == v for k, v in expected.items()), "foreign evidence identity")
    require(isinstance(data["body"], str), "invalid body")
    body = data["body"].encode("utf-8")
    require(0 < len(body) <= MAX_BODY, "invalid body size")
    require(data["body_sha256"] == hashlib.sha256(body).hexdigest(), "body checksum mismatch")
    logs = data["logs"]
    require(type(data["logs_truncated"]) is bool and isinstance(logs, dict)
            and len(logs) <= MAX_LOG_FILES, "invalid logs")
    require(all(isinstance(k, str) and re.fullmatch(r"[A-Za-z0-9_.-]+\.log", k)
                and len(k) <= 255 and isinstance(v, str) for k, v in logs.items()),
            "invalid log entry")
    require(sum(len(v.encode("utf-8")) for v in logs.values()) <= MAX_LOGS,
            "logs exceed bound")
    return body


def select_attempts(entries, source, run, attempt):
    identity(source, run, attempt, 0)
    require(isinstance(entries, list) and len(entries) <= MAX_OBJECTS, "listing exceeds bound")
    prefix = root(source, run) + "shards/"
    selected, seen = {}, set()
    for entry in entries:
        require(isinstance(entry, dict), "malformed listing entry")
        key, size = entry.get("Key"), entry.get("Size")
        require(isinstance(key, str), "missing object key")
        match = re.fullmatch(re.escape(prefix) + r"([0-7])/attempt-([1-9][0-9]*)\.json", key)
        require(match is not None and key not in seen, "foreign or duplicate object key")
        seen.add(key)
        shard, stored_attempt = map(int, match.groups())
        require(stored_attempt <= attempt, "future attempt")
        require(type(size) is int and 0 < size <= MAX_PAYLOAD, "payload exceeds bound")
        if shard not in selected or stored_attempt > selected[shard][0]:
            selected[shard] = (stored_attempt, key)
    require(set(selected) == set(range(SHARDS)), "missing shard result")
    return selected


class S3:
    """AWS CLI boundary, injectable for local roundtrips; no shell or SDK."""

    def __init__(self, run_command=subprocess.run):
        self.run_command = run_command

    def call(self, *args):
        command = ["aws", "--region", "us-east-1", "--cli-connect-timeout", "10",
                   "--cli-read-timeout", "20", "--no-cli-pager", "--output", "json",
                   "s3api", *args, "--bucket", BUCKET]
        result = self.run_command(command, capture_output=True, timeout=60,
                                  env={**os.environ, "AWS_MAX_ATTEMPTS": "2"})
        # Never echo provider stderr or the environment (credential custody).
        require(result.returncode == 0, f"S3 {args[0]} failed")
        require(len(result.stdout) <= MAX_PAYLOAD, "CLI response exceeds bound")
        return json.loads(result.stdout or b"{}")

    def put(self, key, payload, meta):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "envelope.json"
            path.write_bytes(payload)
            self.call("put-object", "--key", key, "--body", str(path),
                      "--if-none-match", "*", "--checksum-algorithm", "SHA256",
                      "--checksum-sha256", checksum(payload), "--content-type", "application/json",
                      "--metadata", json.dumps({k: str(v) for k, v in meta.items()}))

    def list(self, prefix):
        listing = self.call("list-objects-v2", "--prefix", prefix,
                            "--max-keys", str(MAX_OBJECTS), "--no-paginate")
        require(not listing.get("IsTruncated", False), "listing exceeds bound")
        return listing.get("Contents", [])

    def get(self, key):
        head = self.call("head-object", "--key", key, "--checksum-mode", "ENABLED")
        size = head.get("ContentLength")
        require(type(size) is int and 0 < size <= MAX_PAYLOAD, "payload exceeds bound")
        require(isinstance(head.get("ETag"), str), "missing object identity")
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "envelope.json"
            # Bound the actual transfer, and pin it to the inspected object.
            self.call("get-object", "--key", key, "--if-match", head["ETag"],
                      "--range", f"bytes=0-{size - 1}", str(path))
            payload = read_bounded(path, MAX_PAYLOAD)
        require(len(payload) == size, "object length mismatch")
        return payload, head.get("ChecksumSHA256"), head.get("Metadata")


def upload(store, source, run, attempt, shard, path, logs=None):
    meta = identity(source, run, attempt, shard)
    texts, truncated = collect_logs(logs)
    payload = pack(read_bounded(path, MAX_BODY), meta, texts, truncated)
    store.put(object_key(meta), payload, meta)


def download(store, source, run, attempt, output):
    selected = select_attempts(store.list(root(source, run) + "shards/"), source, run, attempt)
    results = {}
    for shard, (stored_attempt, key) in sorted(selected.items()):
        meta = identity(source, run, stored_attempt, shard)
        payload, digest, metadata = store.get(key)
        require(len(payload) <= MAX_PAYLOAD, "payload exceeds bound")
        require(metadata == {k: str(v) for k, v in meta.items()}, "foreign S3 metadata")
        require(digest == checksum(payload), "S3 checksum mismatch")
        # Never fall back past a corrupt latest attempt to a greener predecessor.
        results[shard] = unpack(payload, meta)
    output = Path(output)
    require(not output.exists() or not any(output.iterdir()), "result directory is not empty")
    for shard, body in results.items():
        target = output / f"gate-shard-{shard}-{run}" / "gate-results" / "gate-result.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(body)
    return selected


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("upload-shard", "download-shards", "upload-proof"))
    parser.add_argument("--source", required=True)
    parser.add_argument("--run", required=True)
    parser.add_argument("--attempt", required=True, type=int)
    parser.add_argument("--shard", type=int)
    parser.add_argument("--file", type=Path)
    parser.add_argument("--logs", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    store = S3()
    if args.command == "download-shards":
        require(args.output is not None, "--output is required")
        download(store, args.source, args.run, args.attempt, args.output)
    else:
        require(args.file is not None, "--file is required")
        shard = "proof" if args.command == "upload-proof" else args.shard
        upload(store, args.source, args.run, args.attempt, shard, args.file, args.logs)


if __name__ == "__main__":
    main()
