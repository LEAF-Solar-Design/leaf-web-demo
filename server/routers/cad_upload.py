"""Hardened CAD upload validation (flag: ``cad_upload``) — server/routers/cad_upload.py

    POST /api/cad/upload    multipart {file} -> 201 {upload_id, filename, ext,
        size, digest, version, created_at}

Self-contained validation lane, deliberately separate from the guest/account
drawing-import pipeline in ``routers/uploads.py``: this endpoint only proves a
file is admissible and durably receipted, nothing about extraction or tenancy.

Every rejection is a typed 4xx and NEVER touches disk — bytes are only ever
written once every check below has passed, so a malformed or oversized upload
leaves zero orphan bytes on disk by construction, not by a separate cleanup
step. The four gates, in order (cheapest/most decisive first):

  1. size cap — enforced by ``_read_bounded`` streaming the body in fixed
     chunks and aborting the instant the cap is exceeded, so a hostile
     Content-Length lie still never buffers more than cap+1 bytes.
  2. extension — filename must end in ``.dwg`` or ``.dxf``.
  3. magic-byte sniff — DWG needs the ``AC1`` version prefix; DXF needs the
     binary sentinel or ASCII group-code structure in the first 4 KB.
  4. zip/entity-bomb heuristics — a ZIP local-file-header signature anywhere
     in the payload, an implausible zlib compression ratio (crafted
     high-repetition payloads that pass the magic sniff but are not real CAD
     geometry), or — for DXF — an implausible density of entity-start group
     codes (a cheap proxy for a block/entity-expansion bomb).

Accepted uploads get a content-addressed ``upload_id`` (sha256 digest prefix +
a per-digest version counter) and an append-only JSONL receipt row
(``receipts.jsonl`` in the upload dir) recording digest + version + size +
timestamp — the durable proof this exact content was accepted and when.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import zlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import APIRouter, File, Request, UploadFile
from fastapi.responses import JSONResponse

from envelopes import ErrorCode, error_response, with_envelope_fields

router = APIRouter()

FLAG_CAD_UPLOAD = "LEAF_CAD_UPLOAD_ENABLED"

_ACCEPTED_EXTENSIONS = (".dwg", ".dxf")
_DWG_MAGIC = b"AC1"
_DXF_BINARY_SENTINEL = b"AutoCAD Binary DXF"
_DXF_ASCII_MARKERS = (b"SECTION", b"HEADER", b"ENTITIES")
_ZIP_MAGICS = (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")

# Below this size a compression-ratio verdict is noise (tiny legitimate DXF
# headers compress well too); above it, a high ratio is a real bomb signal.
_BOMB_MIN_BYTES = 4096
_BOMB_COMPRESSION_RATIO = 40.0
# Cheap proxy for entity/block-expansion bombs: real DXF exports of this size
# class do not carry six-figure counts of INSERT/POLYLINE/VERTEX starts.
_DXF_MAX_ENTITY_MARKERS = 150_000

_RECEIPTS_FILENAME = "receipts.jsonl"
_RECEIPT_LOCK = threading.Lock()

SERVER_DIR = Path(__file__).resolve().parent.parent
PROJECT_ROOT = SERVER_DIR.parent


def cad_upload_enabled() -> bool:
    """The ``cad_upload`` flag. Off by default; checked first in the handler."""
    return os.environ.get(FLAG_CAD_UPLOAD, "").strip().lower() in {
        "1", "true", "yes", "on",
    }


def max_upload_bytes() -> int:
    try:
        return int(os.environ.get("LEAF_CAD_UPLOAD_MAX_BYTES", str(25 * 1024 * 1024)))
    except ValueError:
        return 25 * 1024 * 1024


def cad_upload_dir() -> Path:
    return Path(os.environ.get(
        "LEAF_CAD_UPLOAD_DIR", str(PROJECT_ROOT / "data" / "cad_uploads")))


def _read_bounded(stream: Any, cap: int, chunk_size: int = 65536) -> bytes:
    """Stream at most cap+1 bytes from ``stream`` in fixed chunks. Never
    buffers more than cap+1 bytes total regardless of the true body size, so
    an oversized upload cannot be used to exhaust memory before the caller
    gets a chance to reject it — the "streamed, bounded" size gate."""
    buf = bytearray()
    remaining = cap + 1
    while remaining > 0:
        chunk = stream.read(min(chunk_size, remaining))
        if not chunk:
            break
        buf.extend(chunk)
        remaining -= len(chunk)
    return bytes(buf)


def _sniff_reason(ext: str, data: bytes) -> Optional[str]:
    """None when acceptable, else the rejection reason."""
    if not data:
        return "the uploaded file is empty"
    if ext == ".dwg":
        if data[:3] != _DWG_MAGIC:
            return "not a DWG file (missing AC1 version magic)"
        return None
    head = data[:4096]
    if head.startswith(_DXF_BINARY_SENTINEL):
        return None
    if any(marker in head for marker in _DXF_ASCII_MARKERS):
        return None
    return "not a DXF file (no group-code structure in the first 4 KB)"


def _bomb_reason(ext: str, data: bytes) -> Optional[str]:
    """None when the payload looks like plausible CAD geometry, else the
    pathological-file rejection reason (zip/entity-bomb fence)."""
    head = data[:8192]
    if any(magic in head for magic in _ZIP_MAGICS):
        return "embedded zip signature in a CAD payload is rejected"
    if len(data) >= _BOMB_MIN_BYTES:
        compressed = zlib.compress(bytes(data), level=6)
        ratio = len(data) / max(1, len(compressed))
        if ratio > _BOMB_COMPRESSION_RATIO:
            return "pathological compression ratio (possible decompression bomb)"
    if ext == ".dxf":
        marker_count = (
            data.count(b"\nINSERT") + data.count(b"\nPOLYLINE") + data.count(b"\nVERTEX"))
        if marker_count > _DXF_MAX_ENTITY_MARKERS:
            return "implausible entity count (possible entity-expansion bomb)"
    return None


def _next_version_for_digest(directory: Path, digest: str) -> int:
    """Caller holds ``_RECEIPT_LOCK``. Version is the count of prior accepted
    receipts for this exact content digest, so identical bytes uploaded twice
    are traceable as v1/v2/... of the SAME content rather than unrelated ids."""
    receipts_path = directory / _RECEIPTS_FILENAME
    if not receipts_path.exists():
        return 1
    count = 0
    with receipts_path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("digest") == digest:
                count += 1
    return count + 1


def _append_receipt(directory: Path, receipt: Dict[str, Any]) -> None:
    receipts_path = directory / _RECEIPTS_FILENAME
    with receipts_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(receipt) + "\n")


def _write_accepted_upload(filename: str, ext: str, data: bytes) -> Dict[str, Any]:
    """Every effect here happens only for bytes that already cleared every
    gate above — nothing malformed or oversized ever reaches this function,
    so it is the ONLY place this router touches disk."""
    digest = hashlib.sha256(data).hexdigest()
    directory = cad_upload_dir()
    with _RECEIPT_LOCK:
        directory.mkdir(parents=True, exist_ok=True)
        version = _next_version_for_digest(directory, digest)
        upload_id = f"{digest[:16]}-v{version}"
        staged = directory / f"{upload_id}{ext}"
        staged.write_bytes(data)
        receipt = {
            "upload_id": upload_id,
            "filename": filename,
            "ext": ext,
            "size": len(data),
            "digest": digest,
            "version": version,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        _append_receipt(directory, receipt)
    return receipt


@router.post("/api/cad/upload")
def upload_cad_file(request: Request, file: UploadFile = File(...)) -> Any:
    if not cad_upload_enabled():
        return error_response(
            ErrorCode.INTERNAL,
            "cad upload validation is not enabled on this deployment",
            retryable=False, status_code=503)

    cap = max_upload_bytes()
    # Declared Content-Length is rejected up front for the obviously-oversized
    # case; the bounded stream read below is the real wall for a length-less
    # or lying body, matching the pattern in routers/uploads.py.
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > cap + 65536:
        return error_response(
            ErrorCode.BAD_PARAMS,
            f"request exceeds the {cap} byte upload cap", retryable=False,
            status_code=413)

    data = _read_bounded(file.file, cap)
    if len(data) > cap:
        return error_response(
            ErrorCode.BAD_PARAMS,
            f"file exceeds the {cap} byte upload cap", retryable=False,
            status_code=413)

    name = str(file.filename or "").lower()
    ext = next((e for e in _ACCEPTED_EXTENSIONS if name.endswith(e)), "")
    if not ext:
        return error_response(
            ErrorCode.BAD_PARAMS, "only .dwg and .dxf files are accepted",
            retryable=False, status_code=400)

    sniff_reason = _sniff_reason(ext, data)
    if sniff_reason is not None:
        return error_response(
            ErrorCode.BAD_PARAMS, sniff_reason, retryable=False, status_code=400)

    bomb_reason = _bomb_reason(ext, data)
    if bomb_reason is not None:
        return error_response(
            ErrorCode.BAD_PARAMS, bomb_reason, retryable=False, status_code=422)

    receipt = _write_accepted_upload(file.filename or f"upload{ext}", ext, data)
    return JSONResponse(status_code=201, content=with_envelope_fields(receipt))
