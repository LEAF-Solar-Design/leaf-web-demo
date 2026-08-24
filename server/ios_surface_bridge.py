"""Interim in-repo contract source for ``routers/ios_surface.py``.

Projects the LIVE ship lane's own store (``platform/ios_ship.py``) into the
``leaf.ios-ship-surface.v1`` shape the consume-only surface validates. It is
read-only: it opens no dispatch, writes no row, and never touches Apple
credential material — the payload it returns carries exactly the seven
published contract keys and nothing else, so ``ios_surface.validate_contract``
has no unknown field to drop and no secret-shaped key to reject.

THE SEMANTIC TRAP THIS MODULE EXISTS TO AVOID
---------------------------------------------
``ios_ship`` readiness uses ``launchable`` to mean "the Launch TestFlight
button may be pressed" — i.e. the app is ready to BE built. ``ios_surface``
renders ``readiness.launchable is True`` to the user as "iOS app Ready" — i.e.
a built app EXISTS. Copying the field straight through would tell the user the
app is ready before any build had ever run, which is a lie.

So the two booleans are derived from different facts:

  healthy    — LANE health only. True when the ship lane's own health gates
               (readiness row present, healthy, fresh, grant healthy, dispatch
               available) all pass. A spent or absent revision approval is NOT
               a lane fault, so ``unapproved_revision`` / ``approval_consumed``
               still read healthy.
  launchable — a TERMINAL BUILD EXISTS: the newest execution for this exact
               revision succeeded AND its TestFlight receipt reads back from
               the receipt store. Nothing else sets it True.

The honest projection for a working lane that has never built is therefore
``healthy=True, launchable=False`` plus whatever stage the lane last reported,
which ``ios_surface`` renders as in-progress, never as Ready.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any, Dict, Optional

CONTRACT_SCHEMA = "leaf.ios-ship-surface.v1"

# Same closed stage vocabulary the ship-lane controller emits
# (platform/ios_ship.py _CONTROLLER_STAGES) and the surface validates
# (routers/ios_surface.py _BUILD_STAGES). Duplicated per this repo's
# frozen-vocabulary convention: each consumer owns its own copy. Anything
# outside it projects as null rather than as an unvalidatable string.
_BUILD_STAGES = frozenset({
    "SOURCE_APPROVED", "GRANT_READY", "BUNDLE_READY", "MAC_ALLOCATED", "APP_RECORD",
    "XCODE_READY", "SIGNING_READY", "BUILT", "UPLOADED", "COMPLIANCE", "BETA_ASSIGNED",
    "CREDENTIALS_SCRUBBED", "MAC_RELEASED", "RECEIPT",
})

# ``get_readiness`` raises these two ONLY after every lane-health gate has
# already passed, so they prove the lane works and only the revision approval
# is absent or already spent. Every other reason code is a real lane fault.
_LANE_HEALTHY_REASONS = frozenset({"unapproved_revision", "approval_consumed"})

_TERMINAL_BUILD_STAGE = "RECEIPT"


class SurfaceScopeInvalid(ValueError):
    """The caller's scope was not three non-empty strings. Fails closed."""


def _store() -> Any:
    """Load the ship-lane store under the non-colliding ``leaf_platform``
    alias (the ``platform/`` directory shadows the stdlib module). Mechanism
    mirrors ``routers/ios_ship.py._store``."""
    if "leaf_platform" not in sys.modules:
        pkg_dir = Path(__file__).resolve().parent.parent / "platform"
        spec = importlib.util.spec_from_file_location(
            "leaf_platform", pkg_dir / "__init__.py",
            submodule_search_locations=[str(pkg_dir)])
        mod = importlib.util.module_from_spec(spec)
        sys.modules["leaf_platform"] = mod
        spec.loader.exec_module(mod)
    from leaf_platform import ios_ship
    return ios_ship


def _required(scope: Any, key: str) -> str:
    value = scope.get(key) if isinstance(scope, dict) else None
    if not isinstance(value, str) or not value.strip():
        raise SurfaceScopeInvalid(f"{key} is required")
    return value


def _lane_healthy(readiness: Any) -> bool:
    """Lane health, NOT launch eligibility. See the module docstring."""
    if not isinstance(readiness, dict):
        return False
    if readiness.get("healthy") is True:
        return True
    return readiness.get("reason") in _LANE_HEALTHY_REASONS


def _reported_at(readiness: Any) -> Optional[str]:
    value = readiness.get("reported_at") if isinstance(readiness, dict) else None
    return value if isinstance(value, str) and value else None


def _reported_stage(execution: Any) -> Optional[str]:
    """The lane's last reported stage for this execution, or None.

    Prefers the provider's live progress stage over the recorded failed stage,
    and admits ONLY the frozen vocabulary — an unrecognised string projects as
    null rather than as a stage the surface cannot validate.
    """
    if not isinstance(execution, dict):
        return None
    dispatch = execution.get("dispatch_result")
    stage = dispatch.get("stage") if isinstance(dispatch, dict) else None
    if not isinstance(stage, str) or stage not in _BUILD_STAGES:
        stage = execution.get("failed_stage")
    return stage if isinstance(stage, str) and stage in _BUILD_STAGES else None


def _terminal_receipt_id(store: Any, org_id: Any, project_id: str,
                         execution: Any) -> Optional[str]:
    """The receipt id ONLY when a terminal build genuinely exists.

    Requires the newest execution to be ``succeeded`` AND its receipt to read
    back from the receipt store. A succeeded row whose receipt is missing or
    fails canonical verification yields None, so a build that cannot be proven
    can never present as available.
    """
    if not isinstance(execution, dict) or execution.get("status") != "succeeded":
        return None
    receipt_id = execution.get("receipt_id")
    if not isinstance(receipt_id, str) or not receipt_id:
        return None
    try:
        receipt = store.read_receipt(org_id, project_id, receipt_id)
    except Exception:  # noqa: BLE001 - an unverifiable receipt is not a build
        return None
    return receipt_id if isinstance(receipt, dict) and receipt else None


def contract_source(scope: Dict[str, str]) -> Dict[str, Any]:
    """``ios_surface`` source seam: scope -> leaf.ios-ship-surface.v1 payload.

    Raises on an invalid scope or an unreachable/unknown store, which the
    surface renders as a truthful "unavailable" — never as a served contract.
    """
    tenant_id = _required(scope, "tenant_id")
    project_id = _required(scope, "project_id")
    revision = _required(scope, "revision")

    store = _store()
    org_id = store.project_org(project_id)
    if org_id is None:
        raise SurfaceScopeInvalid("project is unavailable")

    readiness = store.readiness_projection(org_id, tenant_id, project_id, revision)
    execution = store.latest_execution_for_revision(
        org_id, tenant_id, project_id, revision)
    receipt_id = _terminal_receipt_id(store, org_id, project_id, execution)
    launchable = receipt_id is not None

    return {
        "schema": CONTRACT_SCHEMA,
        "project_id": project_id,
        "revision": revision,
        "reported_at": _reported_at(readiness),
        # launchable is the BUILD-EXISTS fact, never ios_ship's launch
        # eligibility. See the module docstring.
        "readiness": {"healthy": _lane_healthy(readiness), "launchable": launchable},
        "build_stage": _TERMINAL_BUILD_STAGE if launchable else _reported_stage(execution),
        "receipt_id": receipt_id,
    }
