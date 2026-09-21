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

WHAT THE WEBSITE ACTUALLY CHECKS (verified against origin/main, 2026-07-24)
--------------------------------------------------------------------------
Ground truth is `git show origin/main:lib/leaf-platform/projection.ts`, function
`isVerifiedServerAvailability`. It exports
`SERVER_AVAILABILITY_TTL_MS = 15_000` and validates, in order:

  * the payload is a record; `contractVersion`; `authority`;
  * `productCapability` a non-empty string;
  * `implementationState`, `runtimeState`, `state` each checked EXHAUSTIVELY
    against an allow-list (an unknown string is a NO, never a fall-through);
  * `observedAt` and `expiresAt` both parseable (`isIsoDate`);
  * `expiresAt` in the future, `expiresAt` after `observedAt`,
    `expiresAt - observedAt` NO LONGER than one TTL, and `observedAt` no further
    than one TTL into the future (the clock-skew bound);
  * `reasonCode`, if present, a string;
  * `fallback`, if present, a record with `mode` in the allow-list and
    `provenanceRequired === true`;
  * `evidence` an array whose EVERY member is a complete record: `kind` in the
    allow-list, non-empty `uri`, parseable `verifiedAt`, and a `digest` with
    `algorithm === 'sha256'` and a 64-hex value;
  * then the per-state consistency rules, including `locked_planned` carrying
    ZERO evidence.

So the mirror below is a real mirror, rule for rule.

CORRECTION OF A CORRECTION (2026-07-24)
---------------------------------------
An earlier revision of this docstring, and of
contract/CAPABILITY-AVAILABILITY-EMIT.md, announced that
`SERVER_AVAILABILITY_TTL_MS` "does not exist anywhere in the website repo" and
relabelled the TTL, window and skew rules as local invention. THAT RETRACTION WAS
WRONG. It came from reading the stale local working tree of leaf_website instead
of `origin/main`, where the constant and both rules are present. The original
claims were accurate and are restored above.

The lesson, which this repo's own notes already recorded: squash merges rewrite
SHAs, so a branch can be merged while its commit is not an ancestor of main.
Verify cross-repo claims by CONTENT against `origin/<branch>`
(`git show origin/main:<path>`), never against a local checkout and never by SHA
ancestry.

Mirrored rules (this module applies all of the above). Two deliberate places
where this module is STRICTER, both fail-closed:
  * `_parse_iso` demands `T` plus an explicit UTC offset and compares at
    millisecond resolution, while the console's `isIsoDate` accepts anything
    `Date.parse` accepts. A naive or oddly-spelled stamp is read differently by
    the two runtimes, so refusing it here removes the ambiguity.
  * a naive `now` is refused outright rather than assumed to be UTC.

LEASE_TTL_SECONDS IS a cross-repo contract: it must equal the website's
`SERVER_AVAILABILITY_TTL_MS / 1000`. Drift is a coordinated contract event, and a
one-sided change makes the console reject every emitted availability while every
capability silently shows locked. The test asserts this against
`git show origin/main:lib/leaf-platform/projection.ts` when the sibling repo is
reachable, and skips with a stated reason when it is not; asserting it against a
number copied into this repo would be circular.

Run:  cd server && python -m pytest tests/test_product_capability_availability.py -q
"""
from __future__ import annotations

import copy
import math
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Mapping, Optional, Sequence

CONTRACT_VERSION = "leaf.platform.v1alpha1"

# W1 uses the ordinary tool catalog, independently of the platform lease catalog.
W1_CAPABILITIES = {
    "solar-settings": {"requires_persisted_graph": True, "adapter": None},
    "solar-size-strings": {"requires_persisted_graph": True, "adapter": None},
    "solar-panel-groups": {"requires_persisted_graph": True, "adapter": None},
    "solar-solve-proposal": {"requires_persisted_graph": False, "adapter": "cloud-proposal"},
    "solar-commit-solve": {"requires_persisted_graph": True, "adapter": None},
    "solar-correct-string": {"requires_persisted_graph": True, "adapter": None},
    "solar-assign-equipment": {"requires_persisted_graph": True, "adapter": None},
    "solar-homeruns": {"requires_persisted_graph": True, "adapter": None},
    "solar-schedule": {"requires_persisted_graph": True, "adapter": None},
}

CLOUD_PROPOSAL_ADAPTER = "cloud-proposal"


def capability_adapter(name):
    """The adapter kind a W1 capability executes through, or None when it has none yet.

    This is the ONE place engine readiness and cloud-proposal routing are decided from.
    An unknown or non-string name answers None (never raises): routing sites hand it
    arbitrary tool records.
    """
    row = W1_CAPABILITIES.get(name) if isinstance(name, str) else None
    return row.get("adapter") if isinstance(row, dict) else None


def is_cloud_proposal(tool_or_name):
    """True only for the capability whose adapter is the cloud-proposal kind.

    Accepts a tool record (reads its "name") or a bare name. Compares adapter KINDS,
    never the tool name, so a second adapter of another kind can never route here.
    """
    name = tool_or_name.get("name") if isinstance(tool_or_name, dict) else tool_or_name
    return capability_adapter(name) == CLOUD_PROPOSAL_ADAPTER


def annotate_w1_availability(families, tenant, drawing_id=None, *,
                             project_id=None, version="head"):
    """Annotate W1 rows, resolving shared drawing readiness only when needed."""
    import entitlements

    inputs = None
    resolved = False
    for family in families:
        for row in family.get("capabilities") or []:
            if row.get("name") not in W1_CAPABILITIES:
                continue
            if not resolved:
                inputs = w1_input_readiness(
                    tenant, drawing_id, project_id=project_id, version=version)
                resolved = True
            row["availability"] = entitlements.w1_tool_availability(
                row, tenant, drawing_id, project_id=project_id,
                version=version, inputs=inputs)
    return families


def w1_input_readiness(tenant, drawing_id=None, *, project_id=None, version="head"):
    """Read only the selected tenant's persisted drawing bundle, never UI steps."""
    def unavailable(reason):
        return {name: {"input_ready": False, "input_reason": reason}
                for name in W1_CAPABILITIES}

    if drawing_id is None:
        return unavailable("drawing_context_required")
    from tenant_id_validator import validate_tenant_id
    try:
        validate_tenant_id(str(tenant))
        validate_tenant_id(drawing_id, kind="drawing id")
        if (project_id is not None and
                (type(project_id) is not str or not 1 <= len(project_id) <= 100)):
            return unavailable("invalid_drawing_context")
        if not ((type(version) is int and version >= 1) or
                (type(version) is str and (version == "head" or
                 (version.isascii() and version.isdigit() and 1 <= len(version) <= 10
                  and int(version) >= 1)))):
            return unavailable("invalid_drawing_context")
        import write_loop
        import store
        backend = write_loop.backend_for_tenant(str(tenant), aps_live=False, da=None)
        bundle = store.read_graph_bundle(backend, str(tenant), drawing_id,
                                         version, project_id=project_id)
        return w1_graph_readiness(bundle["graph"])
    except (KeyError, ValueError, TypeError, OSError):
        return unavailable("persisted_graph_unavailable")


def w1_graph_readiness(graph):
    """Project persisted producer contracts without making a mutation or a call."""
    from solar_design_graph import validate_graph
    from solar_sizing_client import require_sizing
    from solar_equipment import equipment_ready
    from solar_solve_results import coverage, upstream_basis
    from solar_wiring_client import local_routes

    graph = validate_graph(graph)
    result = {}

    def mark(names, ready, reason):
        for name in names:
            result[name] = {"input_ready": bool(ready),
                            "input_reason": None if ready else reason}

    mark(["solar-settings"], True, None)
    settings_ready = all(item["validity"]["state"] == "valid"
                         for item in (graph["project"], graph["settings"]))
    mark(["solar-size-strings"], settings_ready, "valid_settings_required")
    sized = False
    if settings_ready:
        try:
            require_sizing(graph)
            sized = True
        except (KeyError, ValueError, TypeError):
            pass
    mark(["solar-panel-groups"], sized, "sizing_confirmation_required")
    grouped = sized and bool(graph["frames"]) and bool(graph["panels"]) and all(
        item["validity"]["state"] == "valid"
        for item in graph["frames"] + graph["panels"] + graph["electrical_zones"]
    ) and all(panel["frame_ref"] is not None for panel in graph["panels"])
    mark(["solar-solve-proposal", "solar-commit-solve"], grouped,
         "sized_panel_groups_required")
    # Corrections must remain possible when an existing string is stale.
    mark(["solar-correct-string"], bool(graph["strings"]), "strings_required")
    basis = upstream_basis(graph)
    strings_valid = grouped and bool(graph["strings"]) and all(
        item["validity"]["state"] == "valid" and item["module_count"] > 0
        for item in graph["strings"]
    ) and not any(coverage(graph).values()) and all(
        isinstance(frame["extra"].get("solve", {}), dict) and
        frame["extra"].get("solve", {}).get("upstream_sha256", basis) == basis
        for frame in graph["frames"])
    mark(["solar-assign-equipment"], strings_valid, "valid_strings_required")
    assigned = strings_valid and equipment_ready(graph)
    mark(["solar-homeruns"], assigned, "equipment_assignment_required")
    routed = False
    if assigned:
        try:
            expected = local_routes(graph)
            actual = {(r["from_ref"], r["route_kind"]): r for r in graph["routes"]}
            routed = len(actual) == len(expected) == len(graph["routes"])
            for route in expected:
                found = actual.get((route["from_ref"], route["route_kind"]))
                routed = routed and found is not None and found["validity"]["state"] == "valid" and all(
                    found[key] == route[key] for key in
                    ("to_ref", "points", "wire_gauge", "point_units", "length_units")
                ) and math.isclose(found["length_ft"], route["length_ft"], rel_tol=1e-9) and all(
                    found["extra"].get(key) == route["extra"][key]
                    for key in ("terminal_panel_ref", "mppt_letter", "input_number"))
        except (KeyError, ValueError, TypeError):
            routed = False
    mark(["solar-schedule"], routed, "complete_routing_required")
    return result


def w1_availability(name, *, entitled, inputs):
    """Implementation presence does not imply a reachable broker engine."""
    if not W1_CAPABILITIES[name]["requires_persisted_graph"]:
        # Params are validated on submission by the capability's own validator.
        inputs = {"input_ready": True, "input_reason": None}
    engine_ready = capability_adapter(name) is not None
    state = {
        "entitled": entitled is True,
        "engine_ready": engine_ready,
        "implemented": True,
        **inputs,
        "entitlement_reason": None if entitled else "entitlement_required",
        "engine_reason": None if engine_ready else "broker_adapter_unavailable",
        "implementation_reason": None,
    }
    state["refusal_reasons"] = [reason for reason in (
        state["entitlement_reason"], state["implementation_reason"],
        state["engine_reason"], state["input_reason"],
    ) if reason]
    state["runnable"] = all(state[key] is True for key in
                            ("entitled", "implemented", "engine_ready", "input_ready"))
    return state
AUTHORITY = "leaf-platform-registry"

# Must equal leaf_website lib/leaf-platform/projection.ts SERVER_AVAILABILITY_TTL_MS.
LEASE_TTL_SECONDS = 15

# Why a capability is locked. These ride out as `reasonCode`, so a rejected live
# measurement is DISTINGUISHABLE from having no measurement at all. Without that
# distinction an integrator defect is invisible: the console shows an ordinary
# locked capability and nothing reports that a payload was refused.
REASON_NO_MEASUREMENT = "no_verified_registry_response"
REASON_REJECTED_MEASUREMENT = "live_availability_rejected_by_contract_validator"
REASON_FOREIGN_CAPABILITY = "live_availability_named_a_different_capability"
REASON_KEY_MISMATCH = "live_availability_key_disagreed_with_its_payload"

CAPABILITY_STATES = ("shipping", "connected_degraded", "locked_planned", "failed_retryable")
RUNTIME_STATES = ("available", "degraded", "unavailable")
IMPLEMENTATION_STATES = ("implemented", "planned")
EVIDENCE_KINDS = ("contract_test", "security", "end_to_end", "observability", "recovery")
FALLBACK_MODES = ("local", "cached", "read_only")

# `\Z`, NOT `$`. Python's `$` also matches just before a trailing newline, so
# `"a"*64 + "\n"` satisfied this pattern while JS `/^[0-9a-f]{64}$/` refuses it: we
# were ACCEPTING a digest the console rejects, the dangerous direction. Verified in
# node, which also does not trim whitespace in `Date.parse`, so the same reasoning
# applies to the timestamp pattern below.
_SHA256_RE = re.compile(r"^[0-9a-f]{64}\Z")

# Absent is NOT the same as JSON `null`, and conflating them was a hole in the
# DANGEROUS direction (accepting what the console refuses). The console tests
# `!== undefined`, so `reasonCode: null` still reaches its `typeof !== 'string'`
# check and is REJECTED, and `fallback: null` still reaches `isRecord(null)` and is
# REJECTED. Python's `.get()` returns None for absent AND for null, so both were
# accepted here. A sentinel keeps them apart.
#
# An earlier note in this module dismissed this as unreachable "because our
# payloads never contain null". That was wrong: this validator also judges LIVE
# measurements handed in by the integrator, which are parsed JSON and can.
_MISSING = object()
# The one timestamp spelling this validator and the console's Date.parse both
# accept AND resolve to the same instant: extended date, literal T, seconds,
# optional fractional seconds, explicit Z or +/-HH:MM offset.
_ISO_UTC_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?(Z|[+-]\d{2}:\d{2})\Z")


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
    """Parse a CONTRACT timestamp, requiring an explicit UTC offset.

    A naive stamp is not merely sloppy here, it is a cross-runtime hazard: this
    module would read `"2026-07-24 11:59:59"` as UTC, while the console's
    `Date.parse` reads the very same string as the VIEWER'S LOCAL TIME. Both
    sides "accept" it and then disagree about the instant, so a lease could look
    fresh on one side and expired on the other. Demanding an offset makes the two
    validators agree by construction, and it is stricter than either alone.
    """
    if not isinstance(value, str) or not value:
        return None
    # Restrict to the EXACT spelling both runtimes agree on. Verified against
    # node's Date.parse (2026-07-24):
    #   "2026-07-24T11:59:59+00:00:30"  Python ok  / JS NaN      -> divergence
    #   "20260724T115959Z"              Python ok  / JS NaN      -> divergence
    #   "2026-07-24 11:59:59"           both ok, but JS resolves it as the
    #                                   viewer's LOCAL time (16:59:59Z on a
    #                                   UTC-5 host) while Python reads UTC
    #                                   -> silent 5-hour disagreement
    # Extended date + `T` + time + explicit Z/±HH:MM is the safe intersection.
    if not _ISO_UTC_RE.match(value):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    # TRUNCATE to milliseconds, because `Date.parse` does. Accepting 6 digits and
    # then comparing at microsecond precision was a real divergence, not a
    # harmless one: with now=12:00:00.123Z and expiresAt=12:00:00.123999Z, Python
    # saw 999us of life left and said valid while the browser, holding both at
    # .123, judged the lease expired. Truncating here makes the comparison happen
    # on exactly the instants the console will use, and still accepts 1 to 6
    # incoming digits rather than rejecting otherwise-valid ISO.
    return parsed.replace(microsecond=(parsed.microsecond // 1000) * 1000)


def _is_aware(moment: Any) -> bool:
    return (isinstance(moment, datetime) and moment.tzinfo is not None
            and moment.utcoffset() is not None)


def _to_utc(moment: datetime) -> datetime:
    """Normalize an AWARE datetime to UTC. Raises on a naive one.

    Two lessons are baked in here, both of them mistakes this file already made.

    First, a naive `now` must NOT be assumed to be UTC. That looks like a safe
    default and is actually fail-OPEN: at a true 17:00Z a UTC-5 caller passes
    naive 12:00, and a lease that expired at 12:00:13Z is then judged still
    valid, five hours late. Guessing turns a loud failure into a silent wrong
    answer, which is strictly worse. Callers of the EMIT path get a ValueError;
    the validator, which must return a bool rather than raise, fails closed.

    Second, an aware non-UTC `now` must be CONVERTED, not used as-is. Emitting
    `isoformat()` straight from a `timezone(timedelta(seconds=30))` clock
    produced `+00:00:30`, a spelling this module's own validator rejects, so the
    module emitted a payload it would itself refuse.
    """
    if not _is_aware(moment):
        raise ValueError(
            "availability timestamps require a timezone-aware datetime; "
            "got a naive one, which cannot be placed on the timeline. "
            "Pass datetime.now(timezone.utc), not datetime.now()."
        )
    return moment.astimezone(timezone.utc)


def _iso(moment: datetime) -> str:
    """Emit UTC with MILLISECOND precision.

    `Date.parse` keeps only milliseconds, so a 6-digit fraction means Python and
    the console hold instants up to 0.999 ms apart. The window is tiny against a
    15 s lease, but it costs nothing to remove for payloads we generate, so we
    emit exactly 3 fractional digits. Incoming timestamps are still accepted with
    1 to 6 digits: truncation is not an accept/reject divergence, and rejecting
    otherwise-valid ISO would be a worse trade.
    """
    return _to_utc(moment).isoformat(timespec="milliseconds")


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
    # Fail CLOSED, do not guess. A naive clock cannot be placed on the timeline,
    # and assuming UTC would accept long-expired leases from a non-UTC caller.
    if not _is_aware(now):
        return False
    now = _to_utc(now)
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
    reason_code = value.get("reasonCode", _MISSING)
    if reason_code is not _MISSING and not isinstance(reason_code, str):
        return False
    fallback = value.get("fallback", _MISSING)
    if fallback is not _MISSING:
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
                and fallback is not _MISSING and len(evidence) > 0)
    if state == "locked_planned":
        return implementation_state == "planned" and runtime_state == "unavailable" and len(evidence) == 0
    return implementation_state == "implemented" and runtime_state != "available"


def locked_availability(product_capability: str, now: Optional[datetime] = None,
                        reason_code: str = REASON_NO_MEASUREMENT) -> Dict[str, Any]:
    """The fail-closed answer: planned, unavailable, locked, zero evidence.

    Unlike the website's static placeholder, this carries a REAL current lease
    window, because a live endpoint answering "locked" is making a statement
    about now and the console must be able to tell a fresh lock from a stale
    one. It is the only state combination the validator accepts with no
    evidence, so a locked capability can never be dressed in proof.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    now = _to_utc(now)
    return {
        "contractVersion": CONTRACT_VERSION,
        "authority": AUTHORITY,
        "productCapability": product_capability,
        "implementationState": "planned",
        "runtimeState": "unavailable",
        "state": "locked_planned",
        "observedAt": _iso(now),
        "expiresAt": _iso(now + timedelta(seconds=LEASE_TTL_SECONDS)),
        "reasonCode": reason_code,
        "evidence": [],
    }


def descriptor_for(entry: ProductCapability, availability: Dict[str, Any],
                   entitled: Optional[bool] = None,
                   now: Optional[datetime] = None) -> Dict[str, Any]:
    """ServerCapabilityDescriptor shape, field for field with the website type.

    The availability is VALIDATED and SNAPSHOTTED here, not trusted. This was the
    one hole left: `descriptor_for` used to pass caller-owned availability
    straight through, so a payload carrying a naive datetime (or any shape this
    module refuses elsewhere) produced a descriptor containing it, contradicting
    the module's own promise. Anything that does not validate is replaced by the
    locked default, exactly as `build_descriptors` does.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    now = _to_utc(now)
    # The availability must be FOR THIS CAPABILITY. Validity alone is not enough:
    # a well-formed availability for `drawing.inspect` attached to the
    # `drawing.solve.strings` descriptor was accepted, producing a descriptor whose
    # id and whose availability.productCapability disagreed. `build_descriptors`
    # checked the mapping key; this path did not.
    if not isinstance(availability, dict):
        availability = locked_availability(entry.id, now, REASON_NO_MEASUREMENT)
    elif availability.get("productCapability") != entry.id:
        # Names another capability. Distinct code: this is an integrator wiring
        # bug, not an absent measurement, and the two must not look alike.
        availability = locked_availability(entry.id, now, REASON_FOREIGN_CAPABILITY)
    elif not is_well_formed_availability(availability, now):
        availability = locked_availability(entry.id, now, REASON_REJECTED_MEASUREMENT)
    else:
        availability = copy.deepcopy(availability)
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
    now = _to_utc(now)
    live = live or {}
    entitlements = entitlements or {}
    descriptors: List[Dict[str, Any]] = []
    for entry in PRODUCT_CAPABILITIES:
        # A key present with value None is a REJECTED measurement, not an absent one.
        # `live.get(id)` returned None for both, so `{"drawing.solve.strings": None}`
        # reported REASON_NO_MEASUREMENT and the integrator's null payload looked
        # like no payload at all. Same absent-vs-null distinction as the validator.
        measured = live.get(entry.id, _MISSING)
        # Distinguish WHY a live entry is unusable, so a refused measurement never
        # looks like an absent one. The reason rides out as `reasonCode`.
        if measured is _MISSING:
            usable, reason = False, REASON_NO_MEASUREMENT
        elif not isinstance(measured, dict):
            usable, reason = False, REASON_REJECTED_MEASUREMENT
        elif measured.get("productCapability") != entry.id:
            usable, reason = False, REASON_KEY_MISMATCH
        elif not is_well_formed_availability(measured, now):
            usable, reason = False, REASON_REJECTED_MEASUREMENT
        else:
            usable, reason = True, None
        # DEEP COPY, not an alias. `measured` is caller-owned; keeping a
        # reference means a mutation AFTER validation silently changes the emitted
        # descriptor's state without revalidation (validate-then-mutate).
        availability = (copy.deepcopy(measured) if usable
                        else locked_availability(entry.id, now, reason))
        descriptors.append(descriptor_for(entry, availability, entitlements.get(entry.id, False), now))
    return descriptors
