"""
Append-only audit JSONL for the conversational agent (who asked for what, and
what the gate decided). Separate from the metering ledger (agent_ledger.py):
audit = what happened & who decided, ledger = cost.

File: `LEAF_AGENT_AUDIT` (default `data/agent_audit.jsonl` under the repo root).

Disciplines (ported from the platform's broker-ledger + gate exemplars):
  * append-only — nothing here rewrites or truncates;
  * NEVER raises on write failure — audit must not block the request path
    (post-hoc accountability; if it's down the operator finds out via ops
    reads, not via a tenant-facing 500);
  * args reach a record ONLY through the action's `audit_extra` allowlist
    (`project_args`) — params are never logged wholesale;
  * free text is hashed, never raw (`hash_text` / the projection's long-string
    hashing) — a prompt or tool description must not leak into the audit file.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

SERVER_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SERVER_DIR.parent

# Any allowlisted string value longer than this is recorded as its hash — an
# identifier (tool name, dwg, sha256) fits well under it; free text does not.
_MAX_VERBATIM_STR = 120

_write_lock = threading.Lock()


def audit_path() -> Path:
    """Resolved at call time so subprocess/test env overrides apply."""
    override = os.environ.get("LEAF_AGENT_AUDIT")
    return Path(override) if override else (PROJECT_ROOT / "data" / "agent_audit.jsonl")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def hash_text(text: Any) -> str:
    """Stable content fingerprint for free text (joinable, never reversible)."""
    return "sha256:" + hashlib.sha256(str(text).encode("utf-8")).hexdigest()


def project_args(args: Optional[Dict[str, Any]], audit_extra: Iterable[str]) -> Dict[str, Any]:
    """The ONLY path by which call args reach an audit record: copy exactly the
    allowlisted keys. Non-scalar values and long strings (free text) are hashed
    rather than recorded verbatim."""
    args = args or {}
    out: Dict[str, Any] = {}
    for key in audit_extra:
        if key not in args:
            continue
        val = args[key]
        if isinstance(val, str) and len(val) > _MAX_VERBATIM_STR:
            out[key] = hash_text(val)
        elif isinstance(val, (str, int, float, bool)) or val is None:
            out[key] = val
        else:
            out[key] = hash_text(json.dumps(val, sort_keys=True, default=str))
    return out


def append(event: Dict[str, Any], *, path: Optional[Path] = None) -> None:
    """Append one audit event. Adds `ts` if missing. Never raises — logs the
    failure to stderr and returns (audit is best-effort by contract)."""
    target = path or audit_path()
    record = dict(event)
    record.setdefault("ts", _now_iso())
    try:
        line = json.dumps(record, separators=(",", ":"), sort_keys=True, default=str)
        target.parent.mkdir(parents=True, exist_ok=True)
        with _write_lock:
            with target.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
    except Exception as exc:  # noqa: BLE001 - never block the request path on audit IO
        print(f"[leaf-agent] audit append failed: {exc}", file=sys.stderr, flush=True)


def tail(n: int = 100, *, path: Optional[Path] = None) -> List[Dict[str, Any]]:
    """Last N events, oldest first. Tolerates missing file + malformed lines."""
    target = path or audit_path()
    if not target.exists():
        return []
    try:
        lines = target.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out: List[Dict[str, Any]] = []
    for line in lines[-n:] if n else lines:
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _filtered(match: Dict[str, Any], limit: int, path: Optional[Path]) -> List[Dict[str, Any]]:
    target = path or audit_path()
    if not target.exists():
        return []
    try:
        lines = target.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out: List[Dict[str, Any]] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if all(event.get(k) == v for k, v in match.items()):
            out.append(event)
    return out[-limit:] if limit else out


def for_tenant(tenant_id: str, limit: int = 100, *, path: Optional[Path] = None) -> List[Dict[str, Any]]:
    """A tenant's own records (already audit_extra-projected at write time)."""
    return _filtered({"tenant_id": str(tenant_id)}, limit, path)


def for_session(session_id: str, limit: int = 100, *, path: Optional[Path] = None) -> List[Dict[str, Any]]:
    return _filtered({"session_id": str(session_id)}, limit, path)
