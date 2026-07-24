"""
Product-capability availability catalog (release gates 2-8), fail-closed.

WHY THIS MODULE EXISTS
----------------------
`GET /api/platform/capabilities` (server/routers/jobs.py) today emits exactly
ONE product capability, `drawing.solve.strings`, hand-built inline. Every other
release gate — inspect a drawing, run an approved tool, check electrical,
generate evidence, countersign evidence, author a company tool — has no entry at
all, so the console cannot even name them. The next lane to add one would
hand-type its identifier, and a typo there is invisible: the console's validator
simply never matches, and the capability shows locked forever with no error.

So: one catalog, one spelling, one fail-closed default. This module holds the
seven identifiers and the rules for turning a live measurement into a descriptor
the website will actually accept. It deliberately owns NO routing and NO health
probing — `jobs.py` and the platform integrator own those. Live availability is
passed IN. That keeps this file testable with no database, no worker, and no
network, and keeps file ownership disjoint from the integrator lane.

CONTRACT MIRROR (the point of the exercise)
-------------------------------------------
leaf_website `lib/leaf-platform/projection.ts::isVerifiedServerAvailability`
rejects anything malformed and reduces it to `locked_planned`. If this server
emits a payload that validator rejects, the console silently shows a locked
capability and NOTHING reports why. `is_well_formed_availability` below is a
deliberate mirror of that validator, applied before emit, so a malformed
availability is caught HERE — where it can be logged and fixed — instead of
being silently swallowed by a browser.

Mirrored rules, kept in this order to match the TypeScript:
  * `contractVersion` == leaf.platform.v1alpha1, `authority` ==
    leaf-platform-registry;
  * every enum field checked exhaustively (an unknown string is a NO, never a
    fall-through);
  * `expiresAt` in the future, after `observedAt`, and the window no LONGER
    than one TTL — an attacker-supplied 2099 expiry must not extend trust;
  * `observedAt` no further than one TTL into the future (clock-skew bound);
  * every evidence record complete, with a real 64-hex sha256;
  * state/implementation/runtime/evidence consistency, including the rule that
    `locked_planned` carries ZERO evidence — a locked capability dressed in
    receipts is fail-open by implication.

TTL drift is a coordinated contract event: changing LEASE_TTL_SECONDS here
without changing SERVER_AVAILABILITY_TTL_MS on the website breaks every emit.

Run:  cd server && python -m pytest tests/test_product_capability_availability.py -q
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Mapping, Optional, Sequence

CONTRACT_VERSION = "leaf.platform.v1alpha1"
AUTHORITY = "leaf-platform-registry"

# Must equal leaf_website lib/leaf-platform/projection.ts SERVER_AVAILABILITY_TTL_MS.
LEASE_TTL_SECONDS = 15

CAPABILITY_STATES = ("shipping", "connected_degraded", "locked_planned", "failed_retryable")
RUNTIME_STATES = ("available", "degraded", "unavailable")
IMPLEMENTATION_STATES = ("implemented", "planned")
EVIDENCE_KINDS = ("contract_test", "security", "end_to_end", "observability", "recovery")
FALLBACK_MODES = ("local", "cached", "read_only")

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class ProductCapability:
    """One release-gate capability. Plain object, no framework coupling."""

    __slots__ = ("id", "release_gate", "label", "description",
                 "tool_capabilities", "entitlements", "declares_entitlement")

    def __init__(self, id: str, release_gate: int, label: str, description: str,
                 tool_capabilities: Sequence[str], entitlements: Sequence[str],
                 declares_entitlement: bool) -> None:
        self.id = id
        self.release_gate = release_gate
        self.label = label
        self.description = description
        self.tool_capabilities = tuple(tool_capabilities)
        self.entitlements = tuple(entitlements)
        # Whether the descriptor carries an `entitled` field at all. Absent is
        # NOT the same as false: absent means entitlement does not gate this
        # capability, false means it does and the caller lacks it.
        self.declares_entitlement = declares_entitlement


# Gate order. Identifiers MUST match leaf_website
# lib/leaf-platform/capabilityCatalog.ts exactly — the pinned-id test in
# tests/test_product_capability_availability.py is the guard.
PRODUCT_CAPABILITIES: Sequence[ProductCapability] = (
    ProductCapability(
        "drawing.inspect", 2, "Inspect drawing",
        "Read-only drawing context remains locked until a registry response is verified.",
        (), ("run_read",), False),
    ProductCapability(
        "drawing.run.approved", 3, "Run approved tool",
        "Running a registry-approved drawing tool stays locked until the registry reports "
        "that exact tool currently verified.",
        ("run",), ("run",), True),
    # Operator ruling R-A (2026-07-22): ONE capability, two tool names — the
    # heuristic `autofill-string-targets` and the real optimizer
    # `string-autofill-opt`. No shared name, no precedence; each run result
    # discloses which solver ran. This is the one id a live backend emits today
    # (routers/jobs.py).
    ProductCapability(
        "drawing.solve.strings", 4, "Solve strings",
        "Solver dispatch remains locked pending a verified worker connection.",
        ("solve",), ("solve",), True),
    ProductCapability(
        "drawing.check.electrical", 5, "Check electrical",
        "The narrow deterministic rule kernel remains locked until a pinned, reviewed "
        "standards pack and AHJ authority are available.",
        ("check",), ("solve",), True),
    ProductCapability(
        "evidence.generate", 6, "Generate evidence bundle",
        "Bundle generation stays locked until a completed, reviewed solve exists and the "
        "registry reports the action currently verified.",
        ("evidence",), ("review",), True),
    ProductCapability(
        "review.evidence", 7, "Review evidence",
        "Existing frozen bundles can be inspected read-only. Generation and countersign "
        "stay locked until the server capability is promoted.",
        (), ("review",), False),
    ProductCapability(
        "tool.author.company", 8, "Author company tool",
        "Authoring a reusable company tool stays locked without a verified grant, "
        "entitlement, and sandbox.",
        ("author",), ("author",), True),
)

_BY_ID: Dict[str, ProductCapability] = {c.id: c for c in PRODUCT_CAPABILITIES}


def capability(product_capability: str) -> Optional[ProductCapability]:
    """None for an unknown id. Callers fail closed; nobody invents a capability."""
    return _BY_ID.get(product_capability)


def _parse_iso(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _is_evidence(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    digest = value.get("digest")
    return (
        value.get("kind") in EVIDENCE_KINDS
        and isinstance(value.get("uri"), str) and len(value["uri"]) > 0
        and _parse_iso(value.get("verifiedAt")) is not None
        and isinstance(digest, dict)
        and digest.get("algorithm") == "sha256"
        and isinstance(digest.get("value"), str)
        and _SHA256_RE.match(digest["value"]) is not None
    )


def is_well_formed_availability(value: Any, now: Optional[datetime] = None) -> bool:
    """Mirror of the website's `isVerifiedServerAvailability`. False means the
    console WOULD reject this payload, so we must not emit it."""
    if now is None:
        now = datetime.now(timezone.utc)
    if not isinstance(value, dict):
        return False
    if value.get("contractVersion") != CONTRACT_VERSION:
        return False
    if value.get("authority") != AUTHORITY:
        return False
    product_capability = value.get("productCapability")
    if not isinstance(product_capability, str) or not product_capability:
        return False
    if value.get("implementationState") not in IMPLEMENTATION_STATES:
        return False
    if value.get("runtimeState") not in RUNTIME_STATES:
        return False
    state = value.get("state")
    if state not in CAPABILITY_STATES:
        return False
    observed = _parse_iso(value.get("observedAt"))
    expires = _parse_iso(value.get("expiresAt"))
    if observed is None or expires is None:
        return False
    if expires <= now:
        return False
    if expires <= observed:
        return False
    if expires - observed > timedelta(seconds=LEASE_TTL_SECONDS):
        return False
    if observed > now + timedelta(seconds=LEASE_TTL_SECONDS):
        return False
    reason_code = value.get("reasonCode")
    if reason_code is not None and not isinstance(reason_code, str):
        return False
    fallback = value.get("fallback")
    if fallback is not None:
        if not isinstance(fallback, dict):
            return False
        if fallback.get("mode") not in FALLBACK_MODES:
            return False
        if fallback.get("provenanceRequired") is not True:
            return False
    evidence = value.get("evidence")
    if not isinstance(evidence, list) or not all(_is_evidence(item) for item in evidence):
        return False
    implementation_state = value["implementationState"]
    runtime_state = value["runtimeState"]
    if state == "shipping":
        return implementation_state == "implemented" and runtime_state == "available" and len(evidence) > 0
    if state == "connected_degraded":
        return (implementation_state == "implemented" and runtime_state == "degraded"
                and fallback is not None and len(evidence) > 0)
    if state == "locked_planned":
        return implementation_state == "planned" and runtime_state == "unavailable" and len(evidence) == 0
    return implementation_state == "implemented" and runtime_state != "available"


def locked_availability(product_capability: str, now: Optional[datetime] = None,
                        reason_code: str = "no_verified_registry_response") -> Dict[str, Any]:
    """The fail-closed answer: planned, unavailable, locked, zero evidence.

    Unlike the website's static placeholder, this carries a REAL current lease
    window, because a live endpoint answering "locked" is making a statement
    about now and the console must be able to tell a fresh lock from a stale
    one. It is the only state combination the validator accepts with no
    evidence, so a locked capability can never be dressed in proof.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    return {
        "contractVersion": CONTRACT_VERSION,
        "authority": AUTHORITY,
        "productCapability": product_capability,
        "implementationState": "planned",
        "runtimeState": "unavailable",
        "state": "locked_planned",
        "observedAt": now.isoformat(),
        "expiresAt": (now + timedelta(seconds=LEASE_TTL_SECONDS)).isoformat(),
        "reasonCode": reason_code,
        "evidence": [],
    }


def descriptor_for(entry: ProductCapability, availability: Dict[str, Any],
                   entitled: Optional[bool] = None) -> Dict[str, Any]:
    """ServerCapabilityDescriptor shape, field for field with the website type."""
    descriptor: Dict[str, Any] = {
        "id": entry.id,
        "label": entry.label,
        "description": entry.description,
        "availability": availability,
        "toolCapabilities": list(entry.tool_capabilities),
        "entitlements": list(entry.entitlements),
    }
    if entry.declares_entitlement:
        descriptor["entitled"] = bool(entitled)
    return descriptor


def build_descriptors(live: Optional[Mapping[str, Dict[str, Any]]] = None,
                      entitlements: Optional[Mapping[str, bool]] = None,
                      now: Optional[datetime] = None) -> List[Dict[str, Any]]:
    """All seven descriptors in release-gate order.

    `live` maps a capability id to a measured availability (the integrator
    supplies these from real health, never from config presence). Three ways an
    entry is replaced by the locked default, all silent-failure-proof because
    they are the SAME rule the console applies:
      * no live entry for that id;
      * a live entry whose `productCapability` disagrees with its own key;
      * a live entry the website validator would reject.
    Unknown ids in `live` are ignored, never promoted into the emit.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    live = live or {}
    entitlements = entitlements or {}
    descriptors: List[Dict[str, Any]] = []
    for entry in PRODUCT_CAPABILITIES:
        measured = live.get(entry.id)
        usable = (
            isinstance(measured, dict)
            and measured.get("productCapability") == entry.id
            and is_well_formed_availability(measured, now)
        )
        availability = measured if usable else locked_availability(entry.id, now)
        descriptors.append(descriptor_for(entry, availability, entitlements.get(entry.id, False)))
    return descriptors
