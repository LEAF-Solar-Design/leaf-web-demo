"""Admission checks for the X11 foreground browser run."""

import hashlib
import re

from benches.x08_runtime import permitted_path
from benches.x11_evidence import UnreadableEvidence


FIXTURE_PINS = {
    "lena": (1_445_687, "b8cd6fa2b0d4f39e29ee34b1f1a618358f9b773892861e043b94d1b900aef9e7"),
    "bensenville-dwg": (14_575_864, "a5e77ffe80d63825c69e0408ab416a1cf2ee7ac120903f51a6d0fecb05187574"),
}


def preflight(ctx):
    """Return the bounded, verified bytes to use for each private drawing upload."""
    if set(ctx.options) != {"expected_source_sha"}:
        raise UnreadableEvidence("X11 requires an exact expected_source_sha option")
    expected = ctx.options["expected_source_sha"]
    if not re.fullmatch(r"[0-9a-f]{40}", expected) or ctx.source_sha != expected:
        raise UnreadableEvidence("X11 build SHA does not match the expected source")

    fixtures = ctx.manifest["fixtures"]
    by_id = {row["id"]: row for row in fixtures}
    if len(fixtures) != len(FIXTURE_PINS) or by_id.keys() != FIXTURE_PINS.keys():
        raise UnreadableEvidence("X11 requires exactly the two frozen drawing fixtures")

    inputs = {}
    for fixture_id, pin in FIXTURE_PINS.items():
        fixture = by_id[fixture_id]
        size, digest = pin
        if (fixture["bytes"], fixture["sha256"]) != pin:
            raise UnreadableEvidence(f"{fixture_id} manifest does not match its frozen pin")
        try:
            path = permitted_path(fixture["path"])
            if not path.is_file():
                raise FileNotFoundError
            with path.open("rb") as stream:
                data = stream.read(size + 1)
        except (OSError, ValueError) as exc:
            raise UnreadableEvidence(f"cannot read {fixture_id} private drawing") from exc
        if len(data) != size or hashlib.sha256(data).hexdigest() != digest:
            raise UnreadableEvidence(f"{fixture_id} bytes do not match its frozen pin")
        inputs[fixture_id] = data
    return inputs
