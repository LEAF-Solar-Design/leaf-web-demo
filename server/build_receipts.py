"""
SHA-stamped terminal receipts for jobs (standardization slice 11a).

One ``receipt.json`` per terminal job, written BESIDE the job record: under
``<dir of JOBS_DB>/receipts/<job_id>/receipt.json`` for the SQLite authority,
or under ``LEAF_BUILD_RECEIPTS_DIR`` when the operator names a location (the
postgres authority keeps its rows elsewhere, so that override is how a
container points its receipts at a mounted volume; unset, they land next to
the local jobs.db, which is honest and ephemeral).

The receipt is the broker lane's ``receipts[]`` entry of kind ``terminal``
on GET /api/builds. It carries two SHAs: ``source_sha`` (the deployed
commit, ``LEAF_SOURCE_SHA``, the same value /api/health reports) and
``digest`` (sha256 of the canonical body without the digest), so a reader can
tell WHICH build of the server wrote it and whether the file was edited since.

HARDENING CONTRACT. Fails closed at every seam: a job id that is not a plain
token never reaches the filesystem; a receipt over the size bound, with a bad
digest, a foreign schema, or a job id that does not match its directory reads
as ABSENT (None), never as a partially trusted record; writes are atomic
(temp file + os.replace) and best effort: a receipt that cannot be written
never fails the terminal callback that produced it (``write_terminal_receipt``
returns None and reports once to stderr).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional

SCHEMA = "leaf.build-receipt.v1"
MAX_RECEIPT_BYTES = 16 * 1024
MAX_WRITE_FIELD_CHARS = 2000
MAX_SCAN_DIR_ENTRIES = 5000
_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
_ID_DOT_ONLY = re.compile(r"^\.+$")


def _safe_id(job_id: Any) -> Optional[str]:
    """A job id that may name a directory: a bounded plain token, never a path,
    never ``.`` / ``..``."""
    if not isinstance(job_id, str) or not _ID_RE.match(job_id) or _ID_DOT_ONLY.match(job_id):
        return None
    return job_id


def receipts_dir() -> Path:
    """Where receipts live. Read at call time like every other env seam."""
    override = os.environ.get("LEAF_BUILD_RECEIPTS_DIR", "").strip()
    if override:
        return Path(override)
    import jobs  # noqa: PLC0415 - lazy: jobs binds its DB path at import

    return Path(jobs.DB_PATH).parent / "receipts"


def receipt_ref(job_id: str) -> str:
    """The receipt's reference as the record carries it (relative, portable)."""
    return f"receipts/{job_id}/receipt.json"


def _canonical(body: Dict[str, Any]) -> bytes:
    return json.dumps(body, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")


def _digest(body: Dict[str, Any]) -> str:
    without = {k: v for k, v in body.items() if k != "digest"}
    return hashlib.sha256(_canonical(without)).hexdigest()


def build_receipt(rec: Dict[str, Any], *, source_sha: Optional[str] = None,
                  now: Optional[float] = None) -> Optional[Dict[str, Any]]:
    """The receipt body for a TERMINAL job record, or None when the record is
    not terminal or has no usable id. Pure; the caller writes it."""
    job_id = _safe_id(rec.get("job_id"))
    if job_id is None or rec.get("status") not in ("complete", "failed"):
        return None
    provenance = rec.get("provenance") if isinstance(rec.get("provenance"), dict) else {}
    error = rec.get("error") if isinstance(rec.get("error"), dict) else None
    body: Dict[str, Any] = {
        "schema": SCHEMA,
        "job_id": job_id,
        "tenant_id": str(rec.get("tenant_id") or ""),
        "tool": str(rec.get("tool") or ""),
        "status": rec["status"],
        "attempt": int(rec.get("attempt") or 0),
        "execution_path": provenance.get("execution_path"),
        "fallback": bool(provenance.get("fallback", False)),
        "created_at": rec.get("created_at"),
        "finished_at": rec.get("finished_at"),
        "elapsed_ms": rec.get("elapsed_ms"),
        "error_code": error.get("error_code") if error else None,
        "source_sha": source_sha if source_sha else os.environ.get("LEAF_SOURCE_SHA", "unknown"),
        "written_at": float(now if now is not None else time.time()),
    }
    body["digest"] = _digest(body)
    return body


_reported_write_failure = False


def write_terminal_receipt(rec: Optional[Dict[str, Any]], *, base: Optional[Path] = None) -> Optional[Path]:
    """Write the receipt for a terminal record, atomically. Best effort: returns
    the path on success and None on any failure, never raises into the
    terminal callback. Idempotent: an existing receipt for the job is kept
    (the first terminal outcome is the immutable one, like the job row)."""
    global _reported_write_failure
    if not isinstance(rec, dict):
        return None
    body = build_receipt(rec)
    if body is None:
        return None
    try:
        root = base if base is not None else receipts_dir()
        target_dir = root / body["job_id"]
        target = target_dir / "receipt.json"
        if target.exists():
            return target
        target_dir.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(body, sort_keys=True, indent=2, default=str).encode("utf-8")
        tmp = target_dir / f".receipt.{os.getpid()}.{int(time.time() * 1000)}.tmp"
        with open(tmp, "wb") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, target)
        return target
    except Exception as exc:  # noqa: BLE001 - a receipt must never fail the job
        if not _reported_write_failure:
            _reported_write_failure = True
            print(f"[leaf-builds] terminal receipt not written for {body['job_id']}: "
                  f"{type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        return None


def read_terminal_receipt(job_id: Any, *, base: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    """The receipt for ``job_id`` when it exists AND holds up: bounded size,
    valid JSON object, our schema, the same job id, a matching digest. Anything
    else reads as absent."""
    safe = _safe_id(job_id)
    if safe is None:
        return None
    try:
        root = base if base is not None else receipts_dir()
        path = root / safe / "receipt.json"
        if not path.is_file() or path.stat().st_size > MAX_RECEIPT_BYTES:
            return None
        with open(path, "rb") as fh:
            raw = fh.read(MAX_RECEIPT_BYTES + 1)
        if len(raw) > MAX_RECEIPT_BYTES:
            return None
        body = json.loads(raw.decode("utf-8"))
    except Exception:  # noqa: BLE001 - unreadable == absent
        return None
    if not isinstance(body, dict) or body.get("schema") != SCHEMA or body.get("job_id") != safe:
        return None
    digest = body.get("digest")
    if not isinstance(digest, str) or digest != _digest(body):
        return None
    return body


def list_receipt_job_ids(*, base: Optional[Path] = None) -> "set[str]":
    """The job ids that have a ``receipts/<job_id>/`` subdirectory: ONE
    bounded ``scandir`` pass (MAX_SCAN_DIR_ENTRIES), so a caller checking many
    job ids (GET /api/builds, up to 200 terminal jobs per request) can skip
    the open+parse+digest cost of ``read_terminal_receipt`` for every job that
    has no receipt at all, which is the common case (old jobs, or a job whose
    receipt write failed). Best effort: an unreadable or missing directory
    reads as no ids, never raises."""
    root = base if base is not None else receipts_dir()
    out: "set[str]" = set()
    try:
        with os.scandir(root) as it:
            for i, entry in enumerate(it):
                if i >= MAX_SCAN_DIR_ENTRIES:
                    break
                if entry.is_dir(follow_symlinks=False):
                    out.add(entry.name)
    except OSError:
        return set()
    return out


def terminal_receipt_entry(job_id: Any, *, base: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    """The build record's ``receipts[]`` entry for a job's terminal receipt, or
    None when there is no trustworthy receipt."""
    body = read_terminal_receipt(job_id, base=base)
    if body is None:
        return None
    finished = body.get("finished_at")
    at = finished if isinstance(finished, (int, float)) and not isinstance(finished, bool) and finished > 0 else None
    return {"kind": "terminal", "ref": receipt_ref(body["job_id"]), "at": at}
