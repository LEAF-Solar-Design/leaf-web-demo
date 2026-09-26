"""
ContextPacket assembly (agent spine, CONTRACT-ADDENDUM §18 / wire contract §4).

The app builds one compact, PURE-READ snapshot per conversational turn and ships
it to the harness inside the message body. It is advisory grounding for the
model — never an execution surface — so every field is a projection:

    catalog        cap 60 entries of {name, description, capabilities} only
                   (params schemas deliberately excluded), then {"more": N}
    catalog_hash   sha256 over the FULL visible catalog (name@version), so the
                   harness can detect catalog drift even when the list is capped
    drawing        {id, head_version, layers:[{name,count}], entity_total}
    versions       manifest tail 3 as {version, ts, tool}
    checkout       {held_by, expires_at} (nulls when free)
    entitlements   the tier's capability booleans (entitlements_for)
    active_jobs    non-terminal jobs, cap 5, {job_id, tool, status}
    grant          {kind: oauth|api_key|missing, degraded} — projected from the
                   harness grant-status read; token values NEVER enter this module
    classifier_hint  §12 classifier output passthrough (or null)

SIZE DISCIPLINE: the serialized packet must stay under MAX_PACKET_CHARS
(~1.2K tokens). The catalog is truncated FIRST (it is the only unbounded
field); the final size is asserted, so an oversized packet fails loudly at
build time instead of silently blowing the harness prompt budget.

PURE READS: this module never writes. It deliberately does NOT bootstrap the
demo drawing (write_loop.ensure_demo_drawing) — an unknown drawing yields an
empty digest, not a store mutation.
"""
from __future__ import annotations

import hashlib
import json
import os
import socket
import threading
import time
from typing import Any, Dict, List, Optional

import catalog as catalog_mod
import deps
import entitlements
import jobs
import write_loop

# Wire-contract §4 size disciplines.
CATALOG_CAP = 60
VERSIONS_TAIL = 3
ACTIVE_JOBS_CAP = 5
LAYERS_CAP = 20  # top-N layers by entity count, then {"more": N} (catalog pattern)
# NOTE: 8000 chars is ~2K tokens — LOOSER than the wire contract's ~1.2K-token
# cap. Tightening is a coordinated cross-lane change (the harness prompt budget
# is sized against this constant); tracked as a contract-vs-code discrepancy.
MAX_PACKET_CHARS = 8000
# One-liner descriptions only — a long docstring must not eat the packet budget.
DESCRIPTION_CAP = 140


# --------------------------------------------------------------------------- #
# catalog projection
# --------------------------------------------------------------------------- #
def _one_liner(text: Any) -> str:
    line = str(text or "").strip().splitlines()[0] if str(text or "").strip() else ""
    return line[:DESCRIPTION_CAP]


def _visible_tools(tenant_id: str) -> List[Dict[str, Any]]:
    """The tenant's catalog minus internal/QA tools (same server-side filter the
    §9 catalog applies — the agent must not see internal tools either)."""
    try:
        rules = catalog_mod._load_config().get("filter_rules", {})
    except Exception:  # noqa: BLE001 - missing families file -> no filtering
        rules = {}
    return [t for t in deps.all_tools(str(tenant_id))
            if not catalog_mod.is_internal(t, rules)]


def _catalog_entries(tools: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [{
        "name": t.get("name"),
        "description": _one_liner(t.get("description")),
        "capabilities": list(t.get("capabilities") or []),
    } for t in tools]


def _catalog_hash(tools: List[Dict[str, Any]]) -> str:
    """Hash of the FULL visible catalog identity (pre-cap), stable across ordering."""
    ident = sorted(f"{t.get('name')}@{t.get('version', '1.0.0')}" for t in tools)
    return "sha256:" + hashlib.sha256(json.dumps(ident).encode("utf-8")).hexdigest()


def _capped_catalog(entries: List[Dict[str, Any]], keep: int) -> List[Dict[str, Any]]:
    """First `keep` entries; a trailing {"more": N} marker when any were dropped."""
    keep = max(0, keep)
    out: List[Dict[str, Any]] = list(entries[:keep])
    dropped = len(entries) - keep
    if dropped > 0:
        out.append({"more": dropped})
    return out


# --------------------------------------------------------------------------- #
# drawing digest (pure read — no demo bootstrap)
# --------------------------------------------------------------------------- #
def _empty_drawing(drawing_id: str) -> Dict[str, Any]:
    return {"id": drawing_id, "head_version": None, "layers": [], "entity_total": 0}


def _drawing_sections(tenant_id: str, drawing_id: str) -> Dict[str, Any]:
    """{drawing, versions, checkout} straight from the store manifest + head
    intake. An unknown/unreadable drawing degrades to the empty digest."""
    import store  # da/store.py; importable via write_loop's sys.path setup

    out: Dict[str, Any] = {
        "drawing": _empty_drawing(drawing_id),
        "versions": [],
        "checkout": {"held_by": None, "expires_at": None},
    }
    try:
        backend = write_loop.backend_for_tenant(
            str(tenant_id), aps_live=False, da=None,
        )
        if not backend.exists(store.manifest_key(str(tenant_id), drawing_id)):
            return out  # pure read: never bootstrap the demo drawing here
        m = store.load_manifest(backend, str(tenant_id), drawing_id)
    except Exception:  # noqa: BLE001 - a broken store must not kill the turn
        return out

    out["versions"] = [{
        "version": int(e["v"]),
        "ts": e.get("created"),
        "tool": e.get("tool"),
    } for e in (m.get("versions") or [])[-VERSIONS_TAIL:]]

    # NULLS WHEN FREE, and "free" is `store.checkout_active`'s answer, not the
    # presence of a record. Neither storage authority erases a lapsed lease
    # eagerly, so a manifest keeps its `checkout` dict long after the lease ended
    # and `co.get("holder")` stays truthy the whole time. Publishing that named a
    # holder for a lock the store re-grants to anyone — the same defect
    # GET /versions carried (`routers/drawings.py` `_checkout_view`), where a
    # lease measured 827 minutes dead was still reported as held. Both this live
    # packet and the versions read use the same expiry rule.
    # The shape stays frozen at {held_by, expires_at}: an elapsed lease nulls
    # both fields, it never drops or renames them.
    co = m.get("checkout")
    if not store.checkout_active(co):
        co = None
    out["checkout"] = {"held_by": (co or {}).get("holder"),
                       "expires_at": (co or {}).get("expires")}

    layers: Dict[str, int] = {}
    total = 0
    try:
        _v, intake = write_loop.read_intake(backend, str(tenant_id), drawing_id, "head")
        for key in ("polylines", "inserts", "faces3d"):
            for ent in intake.get(key) or []:
                layers[str(ent.get("layer", "0"))] = layers.get(str(ent.get("layer", "0")), 0) + 1
                total += 1
    except Exception:  # noqa: BLE001 - digest is best-effort; manifest facts still ship
        pass
    # layers is per-DISTINCT-layer and therefore unbounded — cap it like the
    # catalog (top-N by count, deterministic tiebreak, trailing {"more": N}).
    ranked = sorted(layers.items(), key=lambda kv: (-kv[1], kv[0]))
    layer_rows: List[Dict[str, Any]] = [{"name": n, "count": c}
                                        for n, c in ranked[:LAYERS_CAP]]
    if len(ranked) > LAYERS_CAP:
        layer_rows.append({"more": len(ranked) - LAYERS_CAP})
    out["drawing"] = {
        "id": drawing_id,
        "head_version": int(m.get("head", 0)) or None,
        "layers": layer_rows,
        "entity_total": total,
    }
    return out


# --------------------------------------------------------------------------- #
# active jobs + grant status
# --------------------------------------------------------------------------- #
def _active_jobs(tenant_id: str) -> List[Dict[str, Any]]:
    try:
        recent = jobs.list_jobs(str(tenant_id), limit=25)
    except Exception:  # noqa: BLE001
        return []
    live = [r for r in recent if r.get("status") not in jobs.TERMINAL]
    return [{"job_id": r.get("job_id"), "tool": r.get("tool"), "status": r.get("status")}
            for r in live[:ACTIVE_JOBS_CAP]]


def _grant_status(tenant_id: str, grant_timeout_s: float = 10.0) -> Dict[str, Any]:
    """{kind, degraded} projected from the harness grant store — the SAME read
    routers/tenant.py performs, minus everything but the credential KIND. Token
    values never reach this process (the harness status body carries none, and
    only `linked`/`kind` are consulted here regardless).

    kind: "oauth" | "api_key" | "missing"; degraded=True means the grant store
    could not be consulted (harness down/unconfigured), not that the grant is bad.
    """
    deadline = time.monotonic() + grant_timeout_s
    base = (os.environ.get("LEAF_AUTHOR_HARNESS_URL")
            or os.environ.get("LEAF_CONVERSE_HARNESS_URL") or "").rstrip("/")
    if not base:
        return {"kind": "missing", "degraded": True}
    done = threading.Event()
    result: List[Any] = []
    responses: List[Any] = []

    def read_status() -> None:
        r = None
        try:
            import urllib.parse

            import broker_client
            import requests
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            timeout = max(0.001, remaining)
            r = requests.get(f"{base}/grants/{urllib.parse.quote(str(tenant_id), safe='')}",
                             headers=broker_client.harness_headers(), stream=True,
                             timeout=(timeout, timeout))
            responses.append(r)
            if time.monotonic() >= deadline:
                return
            body = bytearray()
            for chunk in r.iter_content(chunk_size=8192):
                if time.monotonic() >= deadline or len(body) + len(chunk) > 64 * 1024:
                    return
                body.extend(chunk)
            if time.monotonic() < deadline:
                result.append(json.loads(body))
        except Exception:  # noqa: BLE001 - unreachable/non-JSON -> degraded
            pass
        finally:
            try:
                if r is not None:
                    r.close()
            except Exception:  # noqa: BLE001 - cleanup failure also degrades
                result.clear()
            finally:
                done.set()

    # iter_content can wait for a whole chunk while a peer dribbles bytes.
    # Bound the caller as well as each socket operation by the same deadline.
    worker = threading.Thread(target=read_status, daemon=True)
    try:
        worker.start()
    except Exception:  # noqa: BLE001 - unavailable worker must not fail the turn
        return {"kind": "missing", "degraded": True}
    done.wait(max(0.0, deadline - time.monotonic()))
    if not done.is_set() or time.monotonic() >= deadline or not result:
        if responses:
            try:
                # Interrupt a blocked body read; the worker owns response.close().
                responses[0].raw._fp.fp.raw._sock.shutdown(socket.SHUT_RDWR)
            except Exception:  # noqa: BLE001 - already closed or no socket
                pass
        return {"kind": "missing", "degraded": True}
    hj = result[0]
    if not isinstance(hj, dict) or not hj.get("linked"):
        return {"kind": "missing", "degraded": False}
    kind = hj.get("kind")
    return {"kind": kind if kind in ("oauth", "api_key") else "oauth", "degraded": False}


# --------------------------------------------------------------------------- #
# packet assembly
# --------------------------------------------------------------------------- #
def _serialize(packet: Dict[str, Any]) -> str:
    return json.dumps(packet, separators=(",", ":"), default=str)


def build_packet(tenant_id: Any, drawing_id: str,
                 classifier_hint: Optional[Dict[str, Any]] = None, *,
                 entitlements_override: Optional[Dict[str, bool]] = None,
                 grant_timeout_s: float = 10.0) -> Dict[str, Any]:
    """Assemble the §4 ContextPacket for one turn. Pure reads; no writes.

    `tenant_id` may be the plain str or the TenantContext (it IS its tenant_id
    string) — the tier resolution needs the object when auth is live, unless
    `entitlements_override` carries the turn's already-resolved snapshot.
    """
    tools = _visible_tools(str(tenant_id))
    entries = _catalog_entries(tools)

    if entitlements_override is not None:
        capabilities = dict(entitlements_override)
    else:
        tier = entitlements.resolve_tier(tenant_id)
        roles, elevated = entitlements.resolve_roles(tenant_id)
        capabilities = entitlements.entitlements_for(tier, roles, elevated)
    packet: Dict[str, Any] = {
        "catalog": _capped_catalog(entries, CATALOG_CAP),
        "catalog_hash": _catalog_hash(tools),
        "entitlements": capabilities,
        "active_jobs": _active_jobs(str(tenant_id)),
        "grant": _grant_status(str(tenant_id), grant_timeout_s=grant_timeout_s),
        "classifier_hint": classifier_hint if classifier_hint else None,
    }
    packet.update(_drawing_sections(str(tenant_id), drawing_id))

    # HARD CAP: truncate the catalog FIRST (the only unbounded field), then assert.
    keep = min(len(entries), CATALOG_CAP)
    while len(_serialize(packet)) >= MAX_PACKET_CHARS and keep > 0:
        keep -= 1
        packet["catalog"] = _capped_catalog(entries, keep)
    raw = _serialize(packet)
    if len(raw) >= MAX_PACKET_CHARS:
        # ValueError (not assert): the route maps it to BAD_PARAMS instead of a
        # 500, and `python -O` cannot strip it into silently shipping oversized.
        raise ValueError(
            f"ContextPacket is {len(raw)} chars even with an empty catalog "
            f"(cap {MAX_PACKET_CHARS}) — a non-catalog field is oversized"
        )
    return packet
