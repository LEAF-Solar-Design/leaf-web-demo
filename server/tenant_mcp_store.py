"""server/tenant_mcp_store.py — per-tenant MCP server registry (standardization slice 8b).

Storage discipline mirrors the harness `FileTenantGrantStore`'s on-disk contract
(server/CONTRACT-ADDENDUM.md ~L759: 0600 from the FIRST write, one atomic
rename per write so a concurrent reader never observes a partial file) for a
NEW record kind — registered tenant MCP servers — kept on the Python/server
side because this slice (8b) is server-only. The tenant_id itself is
validated through the ONE shared reject-don't-collapse rule
(server/tenant_id_validator.py) rather than a second regex, same discipline
as server/tenant_paths.py.

One JSON file per tenant: ``<dir>/<tenant_id>.json``, a bounded list of
records. A second small file, ``<dir>/_pending.json``, holds in-flight OAuth
authorization requests keyed by the opaque ``state`` value (the callback has
no tenant auth context of its own — the unguessable state IS the boundary).

This module never redacts on write — every field callers pass is persisted
verbatim (including the OAuth token). Redaction is routers/tenant_mcp.py's
job: its public projections whitelist exactly {id, label, host, state,
linked_at} and the token/secrets never leave this module's callers.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from tenant_id_validator import validate_tenant_id

SERVER_DIR = Path(__file__).resolve().parent

# Bounds (release blockers, not follow-ups — every number below is load-bearing).
MAX_SERVERS_PER_TENANT = 25          # one tenant's registry page; matches the list cap
MAX_TENANT_FILE_BYTES = 262_144      # 256 KiB: ~25 records with generous OAuth fields, capped read
MAX_PENDING = 200                    # in-flight OAuth connects across ALL tenants at once
PENDING_TTL_S = 600.0                # 10 minutes: an authorization code flow the user abandons expires
MAX_PENDING_FILE_BYTES = 131_072     # 128 KiB: MAX_PENDING small records, capped read

# The exact shape `register()` mints below: `secrets.token_hex(12)`, 24 lowercase
# hex characters. Standardization slice 8c's `mcp_source.server_id` (a tool
# record's optional field, validated in server/tool_record_fields.py) calls
# this shape check rather than re-typing the regex a second place.
_SERVER_ID_RE = re.compile(r"^[0-9a-f]{24}$")


def is_valid_server_id(value: Any) -> bool:
    """Shape check only — does an id LOOK like one this registry could have
    minted? Never looks up a tenant's actual records (a caller validating a
    record field has no tenant context to check against), so a well-shaped
    id for a server that was since unlinked, or belongs to a different
    tenant, still reads True here; the catalog assembly step is what scopes
    a projected tool to its own tenant's connected servers."""
    return isinstance(value, str) and bool(_SERVER_ID_RE.fullmatch(value))


class TenantMcpStoreError(Exception):
    """Raised when a PRESENT store file cannot be trusted (corrupt/oversized).

    Never raised for an absent file — absent means "no records yet", which is
    the normal starting state, not a failure."""


def _dir() -> Path:
    override = os.environ.get("LEAF_TENANT_MCP_DIR")
    path = Path(override) if override else (SERVER_DIR / "data" / "tenant_mcp")
    path.mkdir(parents=True, exist_ok=True)
    return path


def _tenant_file(tenant_id: str) -> Path:
    validate_tenant_id(tenant_id, kind="tenant id")  # raises ValueError on a malformed id
    return _dir() / f"{tenant_id}.json"


def _read_bounded(path: Path, cap: int) -> Optional[List[Dict[str, Any]]]:
    """Read + parse a JSON array, capped at `cap` bytes. None iff the file is
    absent (the safe, ordinary case). Raises TenantMcpStoreError for anything
    present but untrustworthy (oversized, unreadable, not a JSON array)."""
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        return None
    if size > cap:
        raise TenantMcpStoreError(f"{path.name} exceeds the {cap}-byte bound ({size} bytes)")
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise TenantMcpStoreError(f"{path.name} unreadable: {exc}") from exc
    try:
        raw = json.loads(text) if text.strip() else []
    except ValueError as exc:
        raise TenantMcpStoreError(f"{path.name} invalid JSON: {exc}") from exc
    if not isinstance(raw, list):
        raise TenantMcpStoreError(f"{path.name} top level must be a JSON array")
    return raw


def _write_atomic(path: Path, records: List[Dict[str, Any]]) -> None:
    """Atomic rename with 0600 from the FIRST write: the temp file is created
    with O_CREAT|O_EXCL|0600 (no window where it is world/group readable),
    written, fsynced, then os.replace() onto the final name — one atomic
    publish, never a partial file observable by a concurrent reader."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f".{path.name}.{secrets.token_hex(8)}.tmp"
    payload = json.dumps(records, separators=(",", ":")).encode("utf-8")
    fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        os.write(fd, payload)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)


# --------------------------------------------------------------------------- #
# server records
# --------------------------------------------------------------------------- #
def list_records(tenant_id: str) -> List[Dict[str, Any]]:
    """Every record for this tenant, oldest-first. Full records (including the
    OAuth token) — callers project down to the safe public shape themselves."""
    raw = _read_bounded(_tenant_file(tenant_id), MAX_TENANT_FILE_BYTES)
    return list(raw) if raw else []


def get_record(tenant_id: str, server_id: str) -> Optional[Dict[str, Any]]:
    for record in list_records(tenant_id):
        if record.get("id") == server_id:
            return record
    return None


def register(tenant_id: str, *, url: str, label: str, host: str) -> Dict[str, Any]:
    """Insert a fresh `registered` record. Raises TenantMcpStoreError iff the
    tenant is already at MAX_SERVERS_PER_TENANT (the per-tenant cap)."""
    records = list_records(tenant_id)
    if len(records) >= MAX_SERVERS_PER_TENANT:
        raise TenantMcpStoreError(
            f"tenant already holds the maximum of {MAX_SERVERS_PER_TENANT} registered servers"
        )
    record = {
        "id": secrets.token_hex(12),
        "label": label,
        "url": url,
        "host": host,
        "state": "registered",
        "linked_at": None,
        "created_at": time.time(),
        "error_detail": None,
        "oauth": None,
    }
    records.append(record)
    _write_atomic(_tenant_file(tenant_id), records)
    return record


def update_record(tenant_id: str, server_id: str, **fields: Any) -> Optional[Dict[str, Any]]:
    """Merge `fields` into the named record and rewrite the whole file
    atomically. Returns the updated record, or None iff server_id is unknown."""
    records = list_records(tenant_id)
    updated = None
    for record in records:
        if record.get("id") == server_id:
            record.update(fields)
            updated = record
            break
    if updated is None:
        return None
    _write_atomic(_tenant_file(tenant_id), records)
    return updated


def delete_record(tenant_id: str, server_id: str) -> bool:
    records = list_records(tenant_id)
    kept = [r for r in records if r.get("id") != server_id]
    if len(kept) == len(records):
        return False
    _write_atomic(_tenant_file(tenant_id), kept)
    return True


# --------------------------------------------------------------------------- #
# pending OAuth connects (state -> in-flight request), single global file
# --------------------------------------------------------------------------- #
def _pending_file() -> Path:
    return _dir() / "_pending.json"


def _prune_pending(entries: List[Dict[str, Any]], *, now: float) -> List[Dict[str, Any]]:
    return [e for e in entries if now - float(e.get("created_at", 0.0)) <= PENDING_TTL_S]


def create_pending(state: str, entry: Dict[str, Any]) -> None:
    """Register one in-flight OAuth request keyed by its unguessable `state`.

    Raises TenantMcpStoreError iff MAX_PENDING (across all tenants) is already
    live — a bounded queue, not an unbounded cache of abandoned connect
    attempts."""
    now = time.time()
    path = _pending_file()
    raw = _read_bounded(path, MAX_PENDING_FILE_BYTES) or []
    entries = _prune_pending(raw, now=now)
    if len(entries) >= MAX_PENDING:
        raise TenantMcpStoreError(f"too many in-flight connect attempts (cap {MAX_PENDING})")
    entries.append({"state": state, "created_at": now, **entry})
    _write_atomic(path, entries)


def pop_pending(state: str) -> Optional[Dict[str, Any]]:
    """Single-use lookup: remove and return the pending entry for `state`, or
    None iff no live (non-expired) entry matches — a state mismatch or replay
    of an already-consumed callback both fail closed on this None."""
    now = time.time()
    path = _pending_file()
    raw = _read_bounded(path, MAX_PENDING_FILE_BYTES) or []
    entries = _prune_pending(raw, now=now)
    found = None
    remaining = []
    for entry in entries:
        if found is None and entry.get("state") == state:
            found = entry
            continue
        remaining.append(entry)
    if found is None:
        return None
    _write_atomic(path, remaining)
    return found
