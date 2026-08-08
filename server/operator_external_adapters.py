"""Operator external-write allowlist and adapter registry
(contract/OPERATOR.md section 4.1, operator.external_write; O5, Wave 3).

Two fail-closed pieces the external_write runbook depends on:

- An ALLOWLIST of destinations (server/operator_external_destinations.json, env
  override LEAF_OPERATOR_DESTINATIONS_FILE) holding ONLY metadata: the
  destination key, its NON-PRODUCTION environment, and the single adapter that
  is permitted to service it. It holds no secret and names no production host. A
  missing/unreadable file yields an empty allowlist, so nothing is allowed.

- An ADAPTER registry that is DARK by default: no adapter ships in v1 (contract
  reversal: "per-adapter documented reversal, or the adapter does not ship"), so
  external_write fails closed until a deployment registers one out of band. An
  adapter is (write_fn, reversal) where write_fn(destination, payload, token)
  performs EXACTLY ONE outbound call using the injected short-lived token, and
  reversal is a short documented string the runbook records.

Neither piece ever handles a production route or a secret value: the token is
minted and injected by the secret broker, and the destination is a logical
allowlisted name, not a raw URL for a live production surface.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Callable, Dict, NamedTuple, Optional

_NON_PRODUCTION = {"staging", "development"}
_ALLOWED_META = {"environment", "adapter"}
# Destinations and adapter names are bounded identifiers, never free text or a
# raw URL: this keeps a production host or a smuggled value out of the allowlist
# and out of everything the runbook echoes.
_IDENT_RE = re.compile(r"^[a-z0-9_.:-]{1,64}$")


class ExternalWriteError(RuntimeError):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class Adapter(NamedTuple):
    write: Callable[[str, Dict[str, Any], str], None]
    reversal: str


# Deployment-registered adapters, keyed by name. Left empty so external_write is
# dark until a real adapter is registered.
_ADAPTERS: Dict[str, Adapter] = {}


def register_adapter(name: str, write: Optional[Callable[[str, Dict[str, Any], str], None]],
                     reversal: str = "") -> None:
    """Register (or, with write=None, clear) a named external-write adapter.
    Out-of-band; no adapter ships in v1."""
    if write is None:
        _ADAPTERS.pop(name, None)
        return
    if not _IDENT_RE.match(name):
        raise ExternalWriteError("adapter_name_invalid")
    if not isinstance(reversal, str) or not reversal.strip():
        raise ExternalWriteError("adapter_reversal_required")
    _ADAPTERS[name] = Adapter(write=write, reversal=reversal)


def get_adapter(name: str) -> Optional[Adapter]:
    return _ADAPTERS.get(name)


def _destinations_path() -> Path:
    return Path(os.environ.get(
        "LEAF_OPERATOR_DESTINATIONS_FILE",
        str(Path(__file__).resolve().parent / "operator_external_destinations.json")))


def _load_allowlist() -> Dict[str, Dict[str, Any]]:
    try:
        raw = _destinations_path().read_text(encoding="utf-8")
    except OSError:
        return {}
    data = json.loads(raw)
    dests = data.get("destinations", {})
    if not isinstance(dests, dict):
        raise ExternalWriteError("destinations allowlist: destinations must be a mapping")
    for name, meta in dests.items():
        if not isinstance(name, str) or not _IDENT_RE.match(name):
            raise ExternalWriteError(
                "destinations allowlist: destination keys must match "
                "[a-z0-9_.:-]{1,64} (a logical name, never a raw URL)")
        if not isinstance(meta, dict) or set(meta) != _ALLOWED_META:
            raise ExternalWriteError(
                f"destinations allowlist: {name} fields must be exactly "
                f"{sorted(_ALLOWED_META)}")
        for key in ("environment", "adapter"):
            if not isinstance(meta[key], str) or not _IDENT_RE.match(meta[key]):
                raise ExternalWriteError(
                    f"destinations allowlist: {name}.{key} must be a bounded identifier")
        if meta["environment"] not in _NON_PRODUCTION:
            raise ExternalWriteError(
                f"destinations allowlist: {name} environment must be non-production")
    return dests


def describe_destination(destination: str) -> Optional[Dict[str, Any]]:
    """Allowlist metadata (environment, adapter) for a destination, or None."""
    meta = _load_allowlist().get(destination)
    if meta is None:
        return None
    return {"destination": destination, "environment": meta.get("environment"),
            "adapter": meta.get("adapter")}


def list_destinations() -> Dict[str, Dict[str, Any]]:
    return {d: describe_destination(d) for d in _load_allowlist()}


def verify_allowed(destination: str, adapter: str, environment: str) -> Dict[str, Any]:
    """Server-owned precondition: the destination is allowlisted, its declared
    adapter is the one requested, and its NON-PRODUCTION environment matches the
    operator's. Raises ExternalWriteError otherwise. Does NOT require the adapter
    to be registered (that is re-checked at execute so a dark deployment fails
    closed with a distinct reason)."""
    if not _IDENT_RE.match(destination or ""):
        raise ExternalWriteError("destination_invalid")
    if not _IDENT_RE.match(adapter or ""):
        raise ExternalWriteError("adapter_invalid")
    meta = _load_allowlist().get(destination)
    if meta is None:
        raise ExternalWriteError("destination_not_allowlisted")
    if meta["environment"] not in _NON_PRODUCTION or environment not in _NON_PRODUCTION:
        raise ExternalWriteError("production_destination_refused")
    if meta["environment"] != environment:
        raise ExternalWriteError("environment_mismatch")
    if meta["adapter"] != adapter:
        raise ExternalWriteError("adapter_not_permitted_for_destination")
    return {"destination": destination, "environment": meta["environment"],
            "adapter": adapter}
