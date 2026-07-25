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

WEBSITE_REPO = Path("C:/Users/ehaug/OneDrive/Documents/GitHub/leaf_website")
WEBSITE_VALIDATOR_PATH = "lib/leaf-platform/projection.ts"

_IMPLEMENTATION_STATES = ("implemented", "planned")
_RUNTIME_STATES = ("available", "degraded", "unavailable")
_CAPABILITY_STATES = ("shipping", "connected_degraded", "locked_planned", "failed_retryable")
_EVIDENCE_KINDS = ("contract_test", "security", "end_to_end", "observability", "recovery")
_FALLBACK_MODES = ("local", "cached", "read_only")
_WEBSITE_TTL_MS = 15_000
_SHA256 = re.compile(r"^[0-9a-f]{64}\Z")
# Distinguishes an ABSENT key from an explicit JSON null, because the console does.
_ABSENT = object()
# The extended-ISO subset `Date.parse` actually accepts. Node returns NaN for the
# basic format ("20260724T115959Z"), so a helper that accepted it was claiming the
# console tolerates something it does not.
_JS_PARSEABLE = re.compile(
    r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?\Z")

def _website_source() -> str:
    """The console validator's SOURCE, read from origin/main by content.

    Not from the local working tree. A stale local checkout is what caused a wrong
    "this constant does not exist" conclusion in round 4; squash merges rewrite
    SHAs, so a change can be merged while its commit is not an ancestor of main.
    """
    import os
    import subprocess

    def unavailable(detail: str):
        """Skipping keeps this suite portable, but a skipped CONTRACT check is a
        false green: nothing else in this repo can catch cross-repo drift. So the
        skip is opt-out, not the default posture. Set LEAF_CONTRACT_STRICT=1 in the
        job that has both repos checked out and this becomes a hard failure."""
        message = (f"cannot read the website validator from origin/main: {detail}. "
                   f"Cross-repo contract drift is UNVERIFIED in this run.")
        if os.environ.get("LEAF_CONTRACT_STRICT") == "1":
            pytest.fail(message)
        pytest.skip(message)

    try:
        out = subprocess.run(
            ["git", "show", f"origin/main:{WEBSITE_VALIDATOR_PATH}"],
            cwd=str(WEBSITE_REPO), capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:  # pragma: no cover
        unavailable(str(exc))
    if out.returncode != 0:  # pragma: no cover
        unavailable(out.stderr.strip()[:120])
    return out.stdout

def test_the_lease_ttl_matches_the_websites_constant_read_from_origin_main():
    """A REAL cross-repo assertion, which is what rounds 3, 4 and 5 all asked for.

    Earlier versions compared LEASE_TTL_SECONDS to a number copied into this
    repo's own contract document, which is circular. This reads the website's
    exported constant out of origin/main and fails if the two have drifted. If the
    sibling repo is not reachable the test SKIPS with a reason rather than passing
    vacuously.
    """
    source = _website_source()
    found = re.search(r"SERVER_AVAILABILITY_TTL_MS\s*=\s*([0-9_]+)", source)
    assert found, (
        "origin/main no longer exports SERVER_AVAILABILITY_TTL_MS. If the website "
        "genuinely dropped its TTL rules, this module's window and skew checks "
        "become local policy and this test must be rewritten deliberately")
    website_ms = int(found.group(1).replace("_", ""))
    assert website_ms == _WEBSITE_TTL_MS, (
        f"the transcription in this file assumes {_WEBSITE_TTL_MS}ms but "
        f"origin/main says {website_ms}ms")
    assert LEASE_TTL_SECONDS * 1000 == website_ms, (
        f"TTL DRIFT: this module emits {LEASE_TTL_SECONDS}s leases while the "
        f"console enforces a {website_ms}ms window. A one-sided change makes the "
        f"browser reject every emitted availability and every capability shows "
        f"locked with nothing reporting why")

def test_the_website_still_enforces_the_rules_this_module_mirrors():
    """Pin the PREMISE of the mirror, so a website-side removal is caught here
    rather than silently making this module's strictness arbitrary."""
    source = _website_source()
    # Each needle is a RETURN-BEARING guard, not a definition. Round 6 showed the
    # difference matters: deleting `evidence.every(isCapabilityEvidence)` while
    # leaving the helper function defined kept the old pin green, so it was checking
    # that a name exists rather than that a rule is applied.
    for needle, why in (
        ("if (expiresMs - observedMs > SERVER_AVAILABILITY_TTL_MS) return false",
         "window no longer than one TTL"),
        ("if (observedMs > now.getTime() + SERVER_AVAILABILITY_TTL_MS) return false",
         "observedAt clock-skew bound"),
        ("if (availability.fallback.provenanceRequired !== true) return false",
         "fallback must declare provenanceRequired"),
        ("if (!Array.isArray(evidence) || !evidence.every(isCapabilityEvidence)) return false",
         "evidence is an array AND every member is validated"),
        ("if (expiresMs <= now.getTime()) return false", "expired lease refused"),
        ("if (expiresMs <= observedMs) return false", "expiry must follow observation"),
    ):
        normalized = " ".join(source.split())
        assert " ".join(needle.split()) in normalized, (
            f"origin/main no longer enforces: {why}. If the website deliberately "
            f"dropped this rule, this module's corresponding check becomes local "
            f"policy and must be relabelled, not silently left in place")

def _js_date_parse(value):
    """Model of `Date.parse` for the spellings these tests exercise.

    Two fidelity bugs were fixed here after round 6 measured the real runtime:
      * the basic format `20260724T115959Z` parses in Python but is NaN in Node, so
        accepting it made this helper claim the console tolerates something it does
        not. `_JS_PARSEABLE` restricts the shape.
      * a NAIVE stamp is read by Node as the VIEWER'S LOCAL time, not UTC. Modelling
        it as UTC was simply wrong, and modelling it as local would make these tests
        depend on the machine's timezone. It is treated as UNPARSEABLE instead:
        deterministic, and stricter than the console rather than looser, which is
        the safe direction for a mirror. The module under test refuses naive stamps
        outright, so no case here needs it.
    """
    if not isinstance(value, str):
        return None
    if not _JS_PARSEABLE.match(value):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.replace(microsecond=(parsed.microsecond // 1000) * 1000)

def _is_record(value) -> bool:
    return isinstance(value, dict)

def _website_evidence_ok(value) -> bool:
    """Port of `isCapabilityEvidence`."""
    if not _is_record(value):
        return False
    digest = value.get("digest")
    return (isinstance(value.get("kind"), str) and value["kind"] in _EVIDENCE_KINDS
            and isinstance(value.get("uri"), str) and len(value["uri"]) > 0
            and _js_date_parse(value.get("verifiedAt")) is not None
            and _is_record(digest) and digest.get("algorithm") == "sha256"
            and isinstance(digest.get("value"), str)
            and _SHA256.match(digest["value"]) is not None)

def _website_validator(availability, now: datetime) -> bool:
    """FAITHFUL port of `isVerifiedServerAvailability` as it exists on
    origin/main (verified 2026-07-24), clause for clause and in the same order.

    Round 5 caught the previous version being unfaithful in two ways at once: it
    was transcribed from a STALE local checkout of a much shorter validator, and it
    mapped JavaScript truthiness onto Python truthiness. Both are fixed by porting
    the real thing, which uses explicit `Array.isArray` and `!== undefined` checks
    rather than loose truthiness, so the semantics now line up exactly.

    One documented mapping: JS distinguishes `undefined` (absent) from `null`,
    Python's `.get()` returns None for both. Our payloads never contain null, so
    the distinction cannot change a verdict here.
    """
    if not _is_record(availability):
        return False
    if availability.get("contractVersion") != "leaf.platform.v1alpha1":
        return False
    if availability.get("authority") != "leaf-platform-registry":
        return False
    pc = availability.get("productCapability")
    if not isinstance(pc, str) or len(pc) == 0:
        return False
    for field, allowed in (("implementationState", _IMPLEMENTATION_STATES),
                           ("runtimeState", _RUNTIME_STATES),
                           ("state", _CAPABILITY_STATES)):
        value = availability.get(field)
        if not isinstance(value, str) or value not in allowed:
            return False
    observed = _js_date_parse(availability.get("observedAt"))
    expires = _js_date_parse(availability.get("expiresAt"))
    if observed is None or expires is None:
        return False
    if expires <= now:
        return False
    if expires <= observed:
        return False
    if (expires - observed) > timedelta(milliseconds=_WEBSITE_TTL_MS):
        return False
    if observed > now + timedelta(milliseconds=_WEBSITE_TTL_MS):
        return False
    # `!== undefined`, NOT "is not None". JS treats null as PRESENT, so
    # `reasonCode: null` reaches the typeof test and is rejected, and
    # `fallback: null` reaches isRecord(null) and is rejected. Modelling both as
    # absent made this port accept payloads the console refuses, which is the
    # dangerous direction for a mirror.
    reason = availability.get("reasonCode", _ABSENT)
    if reason is not _ABSENT and not isinstance(reason, str):
        return False
    fallback = availability.get("fallback", _ABSENT)
    if fallback is not _ABSENT:
        if not _is_record(fallback):
            return False
        if fallback.get("mode") not in _FALLBACK_MODES:
            return False
        if fallback.get("provenanceRequired") is not True:
            return False
    evidence = availability.get("evidence")
    if not isinstance(evidence, list) or not all(_website_evidence_ok(e) for e in evidence):
        return False
    impl = availability["implementationState"]
    runtime = availability["runtimeState"]
    state = availability["state"]
    if state == "shipping":
        return impl == "implemented" and runtime == "available" and len(evidence) > 0
    if state == "connected_degraded":
        return (impl == "implemented" and runtime == "degraded"
                and fallback is not _ABSENT and len(evidence) > 0)
    if state == "locked_planned":
        return impl == "planned" and runtime == "unavailable" and len(evidence) == 0
    return impl == "implemented" and runtime != "available"

def test_everything_this_module_emits_satisfies_the_real_console_validator():
    """THE mirror test, against a port of the actual origin/main TypeScript."""
    for entry in PRODUCT_CAPABILITIES:
        locked = locked_availability(entry.id, NOW)
        assert _website_validator(locked, NOW) is True, entry.id
        assert is_well_formed_availability(locked, NOW) is True
    for descriptor in build_descriptors(now=NOW):
        assert _website_validator(descriptor["availability"], NOW) is True, descriptor["id"]
    live = {"drawing.solve.strings": _shipping("drawing.solve.strings")}
    got = {d["id"]: d for d in build_descriptors(live, now=NOW)}
    assert _website_validator(got["drawing.solve.strings"]["availability"], NOW) is True

@pytest.mark.parametrize("mutate,why", [
    (lambda a: a.update({"expiresAt": "2099-01-01T00:00:00.000+00:00"}), "window longer than one TTL"),
    (lambda a: a.update({"state": "available"}), "unknown state string"),
    (lambda a: a.update({"runtimeState": "online"}), "unknown runtime state"),
    (lambda a: a.update({"productCapability": ""}), "empty capability id"),
    (lambda a: a.update({"reasonCode": 7}), "non-string reasonCode"),
    (lambda a: a["evidence"][0]["digest"].update({"value": "nope"}), "digest not sha256"),
    (lambda a: a["evidence"][0].update({"kind": "vibes"}), "unknown evidence kind"),
    (lambda a: a.update({"evidence": "receipts"}), "evidence not an array"),
    (lambda a: a.update({"observedAt": (NOW + timedelta(minutes=5)).isoformat(),
                         "expiresAt": (NOW + timedelta(minutes=5, seconds=10)).isoformat()}),
     "observation beyond the skew bound"),
])
def test_the_console_and_this_module_refuse_the_same_payloads(mutate, why):
    """The mirror runs BOTH ways for these: what the console refuses, we refuse.
    This is what makes the mirror claim testable instead of asserted in prose."""
    availability = _shipping("drawing.solve.strings")
    assert _website_validator(availability, NOW) is True, "baseline valid to console"
    assert is_well_formed_availability(availability, NOW) is True, "baseline valid to us"
    mutate(availability)
    assert _website_validator(availability, NOW) is False, f"console must refuse: {why}"
    assert is_well_formed_availability(availability, NOW) is False, f"we must refuse: {why}"

def test_we_are_never_looser_than_the_console():
    """The only tolerable asymmetry is us being STRICTER. Anything we accept, the
    console must accept."""
    cases = [locked_availability(e.id, NOW) for e in PRODUCT_CAPABILITIES]
    cases.append(_shipping("drawing.solve.strings"))
    degraded = _shipping("drawing.solve.strings")
    degraded.update({"runtimeState": "degraded", "state": "connected_degraded",
                     "fallback": {"mode": "read_only", "provenanceRequired": True}})
    cases.append(degraded)
    for availability in cases:
        if is_well_formed_availability(availability, NOW):
            assert _website_validator(availability, NOW) is True, (
                f"we accept something the console refuses: {availability.get('productCapability')} "
                f"{availability.get('state')}")

def test_descriptor_for_refuses_an_availability_belonging_to_another_capability():
    """Validity alone is not enough: the availability must be FOR this capability.
    A well-formed `drawing.inspect` availability attached to the
    `drawing.solve.strings` descriptor used to pass straight through, producing a
    descriptor whose id and productCapability disagreed."""
    foreign = locked_availability("drawing.inspect", NOW)
    assert is_well_formed_availability(foreign, NOW) is True, "foreign payload is itself valid"
    descriptor = descriptor_for(capability("drawing.solve.strings"), foreign, now=NOW)
    assert descriptor["id"] == "drawing.solve.strings"
    assert descriptor["availability"]["productCapability"] == "drawing.solve.strings", (
        "the descriptor must not carry another capability's availability")
    assert descriptor["availability"]["state"] == "locked_planned"
    # The matching one is kept.
    own = locked_availability("drawing.solve.strings", NOW)
    kept = descriptor_for(capability("drawing.solve.strings"), own, now=NOW)
    assert kept["availability"]["productCapability"] == "drawing.solve.strings"

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

@pytest.mark.parametrize("field", ["reasonCode", "fallback"])
def test_an_explicit_json_null_is_refused_like_the_console_refuses_it(field):
    """JS treats null as PRESENT (`!== undefined`), so the console rejects both of
    these. Reading them with a bare `.get()` made this module ACCEPT them, which is
    the dangerous direction. Live measurements are parsed JSON and can contain null,
    so this is reachable, contrary to an earlier note in the module."""
    availability = _shipping("drawing.solve.strings")
    availability[field] = None
    assert _website_validator(availability, NOW) is False, "console refuses it"
    assert is_well_formed_availability(availability, NOW) is False, "so must we"

def test_a_refused_live_measurement_is_distinguishable_from_no_measurement():
    """Fail-closed is right, but silent fail-closed hides the integrator's bug: a
    rejected payload used to become a lock identical to having no payload at all."""
    from product_capability_availability import (
        REASON_NO_MEASUREMENT, REASON_REJECTED_MEASUREMENT, REASON_KEY_MISMATCH,
        REASON_FOREIGN_CAPABILITY,
    )
    absent = {d["id"]: d for d in build_descriptors(now=NOW)}
    assert absent["drawing.inspect"]["availability"]["reasonCode"] == REASON_NO_MEASUREMENT

    expired = _shipping("drawing.solve.strings", NOW - timedelta(hours=1))
    rejected = {d["id"]: d for d in build_descriptors(
        {"drawing.solve.strings": expired}, now=NOW)}
    assert rejected["drawing.solve.strings"]["availability"]["reasonCode"] == \
        REASON_REJECTED_MEASUREMENT, "a refused payload must say so"

    mismatched = _shipping("drawing.inspect")
    keyed = {d["id"]: d for d in build_descriptors(
        {"drawing.solve.strings": mismatched}, now=NOW)}
    assert keyed["drawing.solve.strings"]["availability"]["reasonCode"] == \
        REASON_KEY_MISMATCH

    foreign = descriptor_for(capability("drawing.solve.strings"),
                             locked_availability("drawing.inspect", NOW), now=NOW)
    assert foreign["availability"]["reasonCode"] == REASON_FOREIGN_CAPABILITY

    # And every one of those substituted locks still satisfies the console.
    for descriptor in list(absent.values()) + list(rejected.values()) + list(keyed.values()):
        assert _website_validator(descriptor["availability"], NOW) is True

def test_the_js_parse_model_rejects_what_node_rejects():
    """Pins the helper's fidelity, measured against the real runtime in round 6."""
    assert _js_date_parse("20260724T115959Z") is None, "Node returns NaN for basic format"
    assert _js_date_parse("2026-07-24T11:59:59") is None, "naive is not modelled as UTC"
    assert _js_date_parse("not-a-date") is None
    assert _js_date_parse(12345) is None
    assert _js_date_parse("2026-07-24T11:59:59Z") is not None
    assert _js_date_parse("2026-07-24T11:59:59+00:00") is not None
    # Millisecond truncation, as Date.parse does.
    got = _js_date_parse("2026-07-24T11:59:59.123999+00:00")
    assert got is not None and got.microsecond == 123000

def test_a_trailing_newline_does_not_slip_past_a_python_anchor():
    r"""Python's `$` also matches just before a trailing newline; JS `$` does not, and
    `Date.parse` does not trim whitespace either (both verified in node). So
    `"a"*64 + "\n"` satisfied the digest pattern here while the console refused it,
    and the same held for timestamps. Both patterns use `\Z` now."""
    for suffix in ("\n", "\r\n", " ", "\t"):
        digest_padded = _shipping("drawing.solve.strings")
        digest_padded["evidence"][0]["digest"]["value"] = "a" * 64 + suffix
        assert is_well_formed_availability(digest_padded, NOW) is False, (
            f"a digest with {suffix!r} appended must be refused, as the console refuses it")
        assert _website_validator(digest_padded, NOW) is False, "the port must agree"

        stamp_padded = _shipping("drawing.solve.strings")
        stamp_padded["observedAt"] = (NOW - timedelta(seconds=1)).isoformat() + suffix
        assert is_well_formed_availability(stamp_padded, NOW) is False, (
            f"a timestamp with {suffix!r} appended must be refused")

    # The clean values still pass, so the anchors were tightened and not broken.
    assert is_well_formed_availability(_shipping("drawing.solve.strings"), NOW) is True

def test_an_explicit_null_measurement_is_reported_as_rejected_not_absent():
    """`live.get(id)` returned None for an absent key AND for a key whose value is
    null, so an integrator handing `{"drawing.solve.strings": None}` got
    REASON_NO_MEASUREMENT and its null payload looked like no payload at all."""
    from product_capability_availability import (
        REASON_NO_MEASUREMENT, REASON_REJECTED_MEASUREMENT,
    )
    explicit_null = {d["id"]: d for d in build_descriptors(
        {"drawing.solve.strings": None}, now=NOW)}
    assert explicit_null["drawing.solve.strings"]["availability"]["reasonCode"] == \
        REASON_REJECTED_MEASUREMENT, "a null measurement was SUPPLIED and refused"
    # A capability with no key at all still reports absence.
    assert explicit_null["drawing.inspect"]["availability"]["reasonCode"] == \
        REASON_NO_MEASUREMENT
