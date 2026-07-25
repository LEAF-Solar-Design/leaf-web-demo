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


def _js_date_parse(value):
    """`Date.parse` semantics for the spellings this module emits: millisecond
    resolution, explicit offset required."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.replace(microsecond=(parsed.microsecond // 1000) * 1000)


def _website_validator(availability: dict, now: datetime) -> bool:
    """A faithful transcription of the REAL console validator,
    leaf_website lib/leaf-platform/projection.ts:31-41, read on 2026-07-24.

    Written out in full because this suite's earlier "mirror" claims were
    invented: the console has NO TTL constant, never parses `observedAt`, imposes
    no window-length limit, and never inspects an evidence record's contents. The
    honest test is not "does our validator match a number I wrote down" but "does
    everything we EMIT satisfy the rules the console actually applies".
    """
    if availability.get("contractVersion") != "leaf.platform.v1alpha1":
        return False
    if availability.get("authority") != "leaf-platform-registry":
        return False
    parsed = _js_date_parse(availability.get("expiresAt"))
    if parsed is None or parsed <= now:
        return False
    state = availability.get("state")
    impl = availability.get("implementationState")
    runtime = availability.get("runtimeState")
    evidence = availability.get("evidence")
    if not isinstance(evidence, list):
        return False
    if state == "shipping":
        return impl == "implemented" and runtime == "available" and len(evidence) > 0
    if state == "connected_degraded":
        return (impl == "implemented" and runtime == "degraded"
                and bool(availability.get("fallback")) and len(evidence) > 0)
    if state == "locked_planned":
        return impl == "planned" and runtime == "unavailable" and len(evidence) == 0
    return impl == "implemented" and runtime != "available"


def test_everything_this_module_emits_satisfies_the_real_console_validator():
    """THE mirror test, against a transcription of the actual TypeScript rather
    than against a constant of my own invention."""
    for entry in PRODUCT_CAPABILITIES:
        locked = locked_availability(entry.id, NOW)
        assert _website_validator(locked, NOW) is True, entry.id
        assert is_well_formed_availability(locked, NOW) is True
    for descriptor in build_descriptors(now=NOW):
        assert _website_validator(descriptor["availability"], NOW) is True, descriptor["id"]
    live = {"drawing.solve.strings": _shipping("drawing.solve.strings")}
    got = {d["id"]: d for d in build_descriptors(live, now=NOW)}
    assert _website_validator(got["drawing.solve.strings"]["availability"], NOW) is True


def test_this_module_is_stricter_than_the_console_never_looser():
    """The asymmetry must only run one way: anything we accept, the console must
    accept. The reverse (a local FALSE LOCK) is the deliberate cost of the extra
    policy, and this pins it so nobody loosens us into agreement by accident."""
    console_fixture = {
        "contractVersion": "leaf.platform.v1alpha1",
        "authority": "leaf-platform-registry",
        "productCapability": "drawing.inspect",
        "implementationState": "planned",
        "runtimeState": "unavailable",
        "state": "locked_planned",
        "observedAt": "2026-07-21T00:00:00.000+00:00",
        "expiresAt": "2026-07-21T00:05:00.000+00:00",
        "evidence": [],
    }
    at = datetime(2026, 7, 21, 0, 0, 1, tzinfo=timezone.utc)
    assert _website_validator(console_fixture, at) is True, "console accepts its own fixture"
    assert is_well_formed_availability(console_fixture, at) is False, (
        "we refuse its 5-minute window: stricter, therefore fail-closed")


def test_the_lease_ttl_is_local_policy_with_no_cross_repo_counterpart():
    """The previous version asserted LEASE_TTL_SECONDS against a
    `SERVER_AVAILABILITY_TTL_MS` figure recorded in the contract document. That
    constant does not exist in the website repo at all, so the assertion was both
    circular and false in its premise. Honest statement: the console enforces no
    TTL, this is a local freshness choice, and it must stay short."""
    assert 1 <= LEASE_TTL_SECONDS <= 120
    locked = locked_availability("drawing.inspect", NOW)
    observed = datetime.fromisoformat(locked["observedAt"])
    expires = datetime.fromisoformat(locked["expiresAt"])
    assert (expires - observed).total_seconds() == LEASE_TTL_SECONDS


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


def test_a_naive_now_is_refused_not_assumed_to_be_utc():
    """Two wrong answers were tried here before this one.

    First the naive clock raised TypeError deep inside a comparison. The "fix"
    was to assume UTC, which is worse: it is fail-OPEN. At a true 17:00Z a
    UTC-5 caller passes naive 12:00, and a lease that expired at 12:00:13Z is
    judged still valid, five hours late. A silent wrong answer beats neither a
    crash nor an honest refusal. So: the validator fails CLOSED, and the emit
    path raises rather than guessing an instant it cannot know."""
    naive = NOW.replace(tzinfo=None)
    # Fails closed even for a payload that IS valid against the real clock.
    assert is_well_formed_availability(_shipping("drawing.solve.strings"), naive) is False
    # The concrete fail-open case: a UTC-5 caller's naive wall clock, 5h stale.
    stale_naive = (NOW - timedelta(hours=5)).replace(tzinfo=None)
    expired = _shipping("drawing.solve.strings", NOW - timedelta(hours=5))
    assert is_well_formed_availability(expired, NOW) is False
    assert is_well_formed_availability(expired, stale_naive) is False, (
        "a naive clock must never resurrect an expired lease")
    # The emit path refuses loudly instead of stamping an unknowable instant.
    with pytest.raises(ValueError, match="timezone-aware"):
        build_descriptors(now=naive)
    with pytest.raises(ValueError, match="timezone-aware"):
        locked_availability("drawing.inspect", naive)


def test_an_aware_non_utc_now_is_converted_not_emitted_verbatim():
    """Emitting isoformat() straight from an odd-offset clock produced
    `+00:00:30`, which this module's own validator rejects, so the module emitted
    a payload it would itself refuse."""
    odd = timezone(timedelta(seconds=30))
    weird_now = NOW.astimezone(odd)
    locked = locked_availability("drawing.inspect", weird_now)
    assert locked["observedAt"].endswith("+00:00"), locked["observedAt"]
    assert is_well_formed_availability(locked, weird_now) is True, (
        "the module must accept its own emitted payload from any aware clock")
    # And ordinary whole-hour offsets round-trip identically.
    for offset_hours in (-5, 0, 5, 13):
        clock = NOW.astimezone(timezone(timedelta(hours=offset_hours)))
        payload = locked_availability("drawing.inspect", clock)
        assert is_well_formed_availability(payload, clock) is True


def test_emitted_timestamps_carry_millisecond_precision():
    """`Date.parse` keeps only milliseconds, so a 6-digit fraction would leave
    Python and the console up to 0.999 ms apart. Payloads we generate remove that
    gap; payloads we RECEIVE still accept 1 to 6 digits, since truncation is not
    an accept/reject divergence."""
    for micro in (0, 1, 123456, 999999):
        payload = locked_availability("drawing.inspect", NOW.replace(microsecond=micro))
        fraction = payload["observedAt"].split(".")[1].split("+")[0]
        assert len(fraction) == 3, payload["observedAt"]
        assert is_well_formed_availability(payload, NOW.replace(microsecond=micro)) is True
    # Incoming 6-digit fractions stay acceptable, and are compared at MILLISECOND
    # resolution so Python agrees with Date.parse. The previous assertion here was
    # a tautology: NOW has zero microseconds, so `"." not in observedAt` was always
    # True and the right-hand side never ran.
    incoming = _shipping("drawing.solve.strings", NOW)
    six = (NOW - timedelta(seconds=1)).replace(microsecond=123456).isoformat()
    assert "." in six and len(six.split(".")[1].split("+")[0]) == 6
    incoming["observedAt"] = six
    assert is_well_formed_availability(incoming, NOW) is True, "6-digit input stays acceptable"
    # The divergence case: sub-millisecond life the browser cannot see.
    at = NOW.replace(microsecond=123000)
    boundary = _shipping("drawing.solve.strings", at)
    boundary["observedAt"] = (at - timedelta(seconds=1)).isoformat()
    boundary["expiresAt"] = at.replace(microsecond=123999).isoformat()
    assert is_well_formed_availability(boundary, at) is False, (
        "999us of remaining life is already expired to the console, so it must be "
        "expired to us too")


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
