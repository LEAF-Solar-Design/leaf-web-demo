"""
Binary acceptance for the product-capability catalog
(server/product_capability_availability.py), release gates 2-8.

Covers:
  * the seven identifiers and their order are PINNED, because they must match
    leaf_website lib/leaf-platform/capabilityCatalog.ts character for character.
    A rename on one side and not the other is invisible at runtime — the console
    shows a permanently locked capability and nothing reports why — so the only
    place it can be caught is a pinned list on each side.
  * is_well_formed_availability mirrors the website validator, refusing every
    malformed shape the browser would refuse: wrong contract/authority, unknown
    enum, expired lease, a window longer than one TTL (an attacker-supplied
    2099 expiry must not extend trust), an observation stamped far in the
    future, a bad digest, and each state/evidence consistency rule.
  * the locked default is the ONLY zero-evidence state the validator accepts,
    so a locked capability can never be dressed in receipts.
  * build_descriptors fails CLOSED three ways: no live entry, a live entry
    whose productCapability disagrees with its key, and a live entry the
    website would reject. An unknown id in the live map is ignored, never
    promoted.
  * ROUND TRIP: every availability this module emits passes its own validator,
    which is the mirror of the console's. If this test is green, the server
    cannot emit a payload the console silently swallows.

Run:  cd server && python -m pytest tests/test_product_capability_availability.py -q
"""
from __future__ import annotations

import copy
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

SERVER_DIR = Path(__file__).resolve().parent.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import pytest

from product_capability_availability import (
    LEASE_TTL_SECONDS,
    PRODUCT_CAPABILITIES,
    build_descriptors,
    capability,
    descriptor_for,
    is_well_formed_availability,
    locked_availability,
)

NOW = datetime(2026, 7, 24, 12, 0, 0, tzinfo=timezone.utc)

# The exact ids, in the exact order, that leaf_website's catalog declares.
PINNED_IDS = [
    "drawing.inspect",
    "drawing.run.approved",
    "drawing.solve.strings",
    "drawing.check.electrical",
    "evidence.generate",
    "review.evidence",
    "tool.author.company",
]


def _shipping(product_capability: str, now: datetime = NOW) -> dict:
    return {
        "contractVersion": "leaf.platform.v1alpha1",
        "authority": "leaf-platform-registry",
        "productCapability": product_capability,
        "implementationState": "implemented",
        "runtimeState": "available",
        "state": "shipping",
        "observedAt": (now - timedelta(seconds=1)).isoformat(),
        "expiresAt": (now + timedelta(seconds=13)).isoformat(),
        "evidence": [{
            "kind": "observability",
            "uri": "urn:leaf:runtime:autofill-worker",
            "verifiedAt": now.isoformat(),
            "digest": {"algorithm": "sha256", "value": "a" * 64},
        }],
    }


def test_identifiers_and_gates_are_pinned():
    assert [c.id for c in PRODUCT_CAPABILITIES] == PINNED_IDS
    gates = [c.release_gate for c in PRODUCT_CAPABILITIES]
    assert gates == [2, 3, 4, 5, 6, 7, 8], "catalog order must be release-gate order"
    assert len(set(gates)) == len(gates)


def test_ttl_is_asserted_against_the_contract_document_not_a_local_literal():
    """TTL drift is a coordinated contract event with the website's
    SERVER_AVAILABILITY_TTL_MS. Asserting `LEASE_TTL_SECONDS == 15` against a
    literal duplicated in this file proves nothing — the literal drifts with the
    code. So read the NORMATIVE value out of the shared contract document, which
    is what both repos cite, and fail if the code disagrees with it."""
    contract = (SERVER_DIR.parent / "contract" / "CAPABILITY-AVAILABILITY-EMIT.md")
    text = contract.read_text(encoding="utf-8")
    declared = re.search(r"LEASE_TTL_SECONDS\s*=\s*(\d+)", text)
    assert declared, "the contract document must declare the normative LEASE_TTL_SECONDS"
    assert LEASE_TTL_SECONDS == int(declared.group(1)), (
        f"code says {LEASE_TTL_SECONDS}s but {contract.name} declares "
        f"{declared.group(1)}s — TTL drift must be a coordinated change")
    # The website counterpart is stated in milliseconds in the same document.
    declared_ms = re.search(r"SERVER_AVAILABILITY_TTL_MS\s*=\s*(\d+)", text)
    assert declared_ms, "the contract document must state the website TTL too"
    assert LEASE_TTL_SECONDS * 1000 == int(declared_ms.group(1))


def test_unknown_capability_is_not_invented():
    assert capability("drawing.solve.string") is None  # deliberate typo
    assert capability("") is None
    assert capability("drawing.solve.strings").release_gate == 4


def test_locked_default_is_accepted_and_carries_no_proof():
    locked = locked_availability("drawing.inspect", NOW)
    assert locked["state"] == "locked_planned"
    assert locked["implementationState"] == "planned"
    assert locked["runtimeState"] == "unavailable"
    assert locked["evidence"] == []
    assert is_well_formed_availability(locked, NOW) is True
    # A lock dressed in receipts is fail-open by implication — refuse it.
    dressed = copy.deepcopy(locked)
    dressed["evidence"] = _shipping("drawing.inspect")["evidence"]
    assert is_well_formed_availability(dressed, NOW) is False


def test_locked_lease_expires_like_any_other():
    locked = locked_availability("drawing.inspect", NOW)
    stale = NOW + timedelta(seconds=LEASE_TTL_SECONDS + 1)
    assert is_well_formed_availability(locked, stale) is False


@pytest.mark.parametrize("mutate,reason", [
    (lambda a: a.update({"contractVersion": "leaf.platform.v2"}), "wrong contract version"),
    (lambda a: a.update({"authority": "leaf-server-catalog"}), "wrong authority"),
    (lambda a: a.update({"productCapability": ""}), "empty capability id"),
    (lambda a: a.update({"state": "available"}), "unknown state string"),
    (lambda a: a.update({"runtimeState": "online"}), "unknown runtime state"),
    (lambda a: a.update({"implementationState": "shipped"}), "unknown implementation state"),
    (lambda a: a.update({"observedAt": "not-a-date"}), "unparseable observedAt"),
    (lambda a: a.update({"expiresAt": (NOW - timedelta(seconds=1)).isoformat()}), "already expired"),
    (lambda a: a.update({"expiresAt": "2099-01-01T00:00:00+00:00"}), "window longer than one TTL"),
    (lambda a: a.update({"observedAt": (NOW + timedelta(minutes=5)).isoformat(),
                         "expiresAt": (NOW + timedelta(minutes=5, seconds=10)).isoformat()}),
     "observation beyond the clock-skew bound"),
    (lambda a: a.update({"reasonCode": 7}), "non-string reasonCode"),
    (lambda a: a.update({"evidence": []}), "shipping without evidence"),
    (lambda a: a["evidence"][0]["digest"].update({"value": "nope"}), "digest is not sha256 hex"),
    (lambda a: a["evidence"][0].update({"kind": "vibes"}), "unknown evidence kind"),
    (lambda a: a.update({"evidence": "receipts"}), "evidence is not a list"),
    (lambda a: a.update({"runtimeState": "degraded", "state": "connected_degraded"}),
     "degraded without a declared fallback"),
])
def test_validator_refuses_what_the_console_would_refuse(mutate, reason):
    availability = _shipping("drawing.solve.strings")
    assert is_well_formed_availability(availability, NOW) is True, "baseline must be valid"
    mutate(availability)
    assert is_well_formed_availability(availability, NOW) is False, reason


def test_degraded_needs_fallback_and_proof():
    availability = _shipping("drawing.solve.strings")
    availability.update({"runtimeState": "degraded", "state": "connected_degraded",
                         "fallback": {"mode": "read_only", "provenanceRequired": True}})
    assert is_well_formed_availability(availability, NOW) is True
    availability["fallback"] = {"mode": "read_only", "provenanceRequired": False}
    assert is_well_formed_availability(availability, NOW) is False
    # NEGATIVE COVERAGE (sol-critic gap): a weakened validator that accepted
    # connected_degraded with NO evidence previously passed the whole suite. A
    # degraded capability still has to carry a receipt.
    degraded_no_proof = _shipping("drawing.solve.strings")
    degraded_no_proof.update({"runtimeState": "degraded", "state": "connected_degraded",
                              "fallback": {"mode": "read_only", "provenanceRequired": True},
                              "evidence": []})
    assert is_well_formed_availability(degraded_no_proof, NOW) is False
    # ...and the same rule for failed_retryable: implemented but not available.
    failed = _shipping("drawing.solve.strings")
    failed.update({"runtimeState": "available", "state": "failed_retryable"})
    assert is_well_formed_availability(failed, NOW) is False


def test_a_naive_now_fails_closed_instead_of_crashing():
    """A validator whose whole job is to fail CLOSED must never fail by
    exception. Passing datetime.now() rather than datetime.now(timezone.utc)
    used to raise TypeError and take the emit path down with it."""
    naive = NOW.replace(tzinfo=None)
    assert is_well_formed_availability(_shipping("drawing.solve.strings"), naive) is True
    stale = _shipping("drawing.solve.strings", NOW - timedelta(minutes=5))
    assert is_well_formed_availability(stale, naive) is False
    # The whole descriptor path must survive a naive clock too.
    descriptors = build_descriptors(now=naive)
    assert len(descriptors) == len(PINNED_IDS)
    assert all(d["availability"]["state"] == "locked_planned" for d in descriptors)


def test_a_validated_availability_is_snapshotted_not_aliased():
    """Validate-then-mutate: the caller owns the `live` mapping, so keeping a
    reference let a mutation AFTER validation change the emitted descriptor's
    state without revalidation."""
    live = {"drawing.solve.strings": _shipping("drawing.solve.strings")}
    descriptors = {d["id"]: d for d in build_descriptors(live, now=NOW)}
    assert descriptors["drawing.solve.strings"]["availability"]["state"] == "shipping"
    live["drawing.solve.strings"]["state"] = "unknown"
    live["drawing.solve.strings"]["evidence"].clear()
    assert descriptors["drawing.solve.strings"]["availability"]["state"] == "shipping"
    assert descriptors["drawing.solve.strings"]["availability"]["evidence"], "evidence must not be aliased either"


def test_timestamps_javascript_cannot_parse_are_rejected():
    """The mirror only holds if this validator is no more permissive than JS
    `Date.parse`. Python's fromisoformat accepts spellings JS does not; lock the
    rejections so a future loosening of _parse_iso is caught here."""
    for odd in ("2026-07-24T11:59:59+00:00:30",   # sub-minute offset: NaN in JS
                # NAIVE stamps are the subtler hazard: Python would read this as
                # UTC while the console's Date.parse reads it as the VIEWER'S
                # LOCAL time, so both accept and then disagree on the instant.
                "2026-07-24 11:59:59",
                "2026-07-24T11:59:59",
                "20260724T115959Z",                # basic format
                "not-a-date", "", None, 12345):
        availability = _shipping("drawing.solve.strings")
        availability["observedAt"] = odd
        assert is_well_formed_availability(availability, NOW) is False, f"{odd!r} must be rejected"


def test_no_live_measurement_locks_every_gate():
    descriptors = build_descriptors(now=NOW)
    assert [d["id"] for d in descriptors] == PINNED_IDS
    for descriptor in descriptors:
        assert descriptor["availability"]["state"] == "locked_planned"
        assert descriptor["availability"]["productCapability"] == descriptor["id"]
        # Round trip: what we emit, the console accepts.
        assert is_well_formed_availability(descriptor["availability"], NOW) is True


def test_a_valid_live_measurement_is_passed_through():
    live = {"drawing.solve.strings": _shipping("drawing.solve.strings")}
    descriptors = {d["id"]: d for d in build_descriptors(live, {"drawing.solve.strings": True}, NOW)}
    assert descriptors["drawing.solve.strings"]["availability"]["state"] == "shipping"
    assert descriptors["drawing.solve.strings"]["entitled"] is True
    # Everything else stays locked; one live capability does not lift the rest.
    assert descriptors["drawing.inspect"]["availability"]["state"] == "locked_planned"


@pytest.mark.parametrize("live,reason", [
    ({"drawing.solve.strings": _shipping("drawing.inspect")}, "key disagrees with payload"),
    ({"drawing.solve.strings": {"state": "shipping"}}, "payload is not a full availability"),
    ({"drawing.solve.strings": None}, "payload is not a mapping"),
])
def test_a_bad_live_measurement_falls_back_to_locked(live, reason):
    descriptors = {d["id"]: d for d in build_descriptors(live, now=NOW)}
    assert descriptors["drawing.solve.strings"]["availability"]["state"] == "locked_planned", reason
    assert is_well_formed_availability(descriptors["drawing.solve.strings"]["availability"], NOW)


def test_an_expired_live_measurement_falls_back_to_locked():
    stale = _shipping("drawing.solve.strings", NOW - timedelta(minutes=1))
    descriptors = {d["id"]: d for d in build_descriptors({"drawing.solve.strings": stale}, now=NOW)}
    assert descriptors["drawing.solve.strings"]["availability"]["state"] == "locked_planned"


def test_an_unknown_live_id_is_never_promoted():
    live = {"drawing.solve.everything": _shipping("drawing.solve.everything")}
    descriptors = build_descriptors(live, now=NOW)
    assert [d["id"] for d in descriptors] == PINNED_IDS
    assert all(d["availability"]["state"] == "locked_planned" for d in descriptors)


def test_entitled_is_absent_where_entitlement_does_not_gate():
    descriptors = {d["id"]: d for d in build_descriptors(now=NOW)}
    # Absent is not the same as false: absent means entitlement does not gate
    # this capability at all.
    assert "entitled" not in descriptors["drawing.inspect"]
    assert "entitled" not in descriptors["review.evidence"]
    assert descriptors["drawing.solve.strings"]["entitled"] is False
    assert descriptors["tool.author.company"]["entitled"] is False


def test_descriptor_shape_matches_the_website_type():
    entry = capability("drawing.solve.strings")
    descriptor = descriptor_for(entry, locked_availability(entry.id, NOW), entitled=True)
    assert set(descriptor) == {"id", "label", "description", "availability",
                               "toolCapabilities", "entitlements", "entitled"}
    assert descriptor["toolCapabilities"] == ["solve"]
    assert descriptor["entitlements"] == ["solve"]
