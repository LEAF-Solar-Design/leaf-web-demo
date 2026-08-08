"""client.exception label-vocabulary freeze (docs/PLATFORM_TELEMETRY.md).

Two layers speak this event's vocabulary and they are maintained BY HAND in
different languages:

  * `KNOWN_CLASSES` in `web/src/telemetry.js` -- what the browser will emit
  * `_CLIENT_EXCEPTION_CLASSES` in `server/routers/telemetry.py` -- what the
    ingest door will store

Drift is safe-by-default (an unlisted class degrades to "Other" rather than
travelling as free text), but it is silent: the label simply loses precision
and nobody finds out. This test is the enforcement the review asked for.

It also freezes the two CLOSED sets that carry the event's structural
guarantee, because a shape rule is exactly what let `alicesmith/desktop`
through review round five. Growing any of these is a deliberate act: change
both files and this test in the same PR.

Run:  cd server && python -m pytest tests/test_client_exception_vocab_freeze.py -q
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

SERVER_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = SERVER_DIR.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

from routers import telemetry as telemetry_router  # noqa: E402

CLIENT_JS = REPO_ROOT / "web" / "src" / "telemetry.js"

# ------------------------------ THE FROZEN SETS ---------------------------- #
FROZEN_SOURCES = {"window.onerror", "unhandledrejection"}
FROZEN_ROUTES = {"site", "tool", "sheets", "app", "unknown"}
# `sceneForPath` returns four values; "unknown" is telemetry's own fallback
# when `location` is unreadable.
FROZEN_SCENES = {"site", "tool", "sheets", "app"}
FROZEN_UA_FAMILIES = {"edge", "opera", "firefox", "chrome", "safari", "other"}
DIGEST_WIDTH = 16


def _js_known_classes() -> set[str]:
    """The literal names in web/src/telemetry.js's KNOWN_CLASSES, comments
    stripped (a commented-out name is not in the Set at runtime)."""
    src = CLIENT_JS.read_text(encoding="utf-8")
    block = re.search(r"const KNOWN_CLASSES = new Set\(\[(.*?)\n\]\)", src, re.S)
    assert block, "KNOWN_CLASSES not found in web/src/telemetry.js"
    body = "\n".join(
        line for line in block.group(1).split("\n")
        if not line.strip().startswith("//")
    )
    # [A-Za-z0-9]+, not [A-Za-z]+: a class name carrying a digit (a real
    # possibility -- `ChunkLoad2Error`) was invisible to the parser, so a
    # JS-only addition passed this test while the runtime lists disagreed.
    return set(re.findall(r"'([A-Za-z0-9]+)'", body))


def test_client_and_server_class_lists_agree_exactly():
    """The mirror the review asked to be enforced. A name on one side only is
    a silent precision loss, not a crash, which is exactly why a human never
    notices it."""
    js = _js_known_classes()
    py = set(telemetry_router._CLIENT_EXCEPTION_CLASSES)
    assert js, "parsed no class names from the client -- the parser is stale"
    assert js == py, (
        f"client-only: {sorted(js - py)}  server-only: {sorted(py - js)}"
    )


def test_self_assigned_fallbacks_are_listed():
    """`UnhandledRejection` and `Other` are values the CLIENT assigns when the
    platform supplies no name. Omitting them once made the choke-point filter
    degrade the very fallbacks the handlers had just computed."""
    for name in ("UnhandledRejection", "Other"):
        assert name in telemetry_router._CLIENT_EXCEPTION_CLASSES
        assert name in _js_known_classes()


def test_the_apps_own_error_class_is_listed():
    """FetchTimeoutError is declared in web/src/fetchBudget.js. Its provenance
    is ours, so it is a real class rather than caller text."""
    declared = (REPO_ROOT / "web" / "src" / "fetchBudget.js").read_text(encoding="utf-8")
    assert "class FetchTimeoutError extends Error" in declared
    assert "FetchTimeoutError" in telemetry_router._CLIENT_EXCEPTION_CLASSES


def test_enumerated_labels_are_closed_sets_not_shapes():
    """A shape rule is what let `alicesmith/desktop` through: these labels
    carry the event's structural guarantee, so each must be a CLOSED SET."""
    schema = telemetry_router.PREAUTH_LABEL_SCHEMAS["client.exception"]
    for key in ("source", "route", "ua_class", "message_class"):
        assert isinstance(schema[key], frozenset), f"{key} must be a closed set"

    assert set(schema["source"]) == FROZEN_SOURCES
    assert set(schema["route"]) == FROZEN_ROUTES
    assert set(schema["ua_class"]) == (
        {f"{f}/{form}" for f in FROZEN_UA_FAMILIES for form in ("mobile", "desktop")}
        | {"unknown"}
    )


def test_route_vocabulary_matches_the_apps_own_router():
    """`route` is the app's scene name. If routeScene.js grows a scene, this
    fails until the door learns it -- otherwise the new scene's exceptions
    would silently lose their route label."""
    scenes = (REPO_ROOT / "web" / "src" / "site" / "routeScene.js").read_text(encoding="utf-8")
    returned = set(re.findall(r"return '([a-z]+)'", scenes))
    assert returned == FROZEN_SCENES, f"routeScene.js now returns {sorted(returned)}"
    assert FROZEN_SCENES <= set(PREAUTH_ROUTE_SET())


def PREAUTH_ROUTE_SET():
    return telemetry_router.PREAUTH_LABEL_SCHEMAS["client.exception"]["route"]


def test_digest_width_is_fixed_on_both_sides():
    """A variable-width decimal accepted `5550142`, a phone number. The door
    requires exactly one digest's width; the client zero-pads to it."""
    src = CLIENT_JS.read_text(encoding="utf-8")
    assert f"const DIGEST_WIDTH = {DIGEST_WIDTH}" in src
    assert telemetry_router._HASH_RE.pattern == r"\A[0-9]{%d}\Z" % DIGEST_WIDTH
    assert telemetry_router._HASH_RE.match("0" * DIGEST_WIDTH)
    assert not telemetry_router._HASH_RE.match("5550142")
    # Python's `$` matches before a trailing newline; `\Z` does not.
    assert not telemetry_router._HASH_RE.match("0" * DIGEST_WIDTH + "\n")


def test_tour_step_is_deliberately_absent():
    """Closing it would need a mirror of two product-owned step tables that
    change with ordinary feature work, and a shape rule would accept
    `customer_secret`. Recorded so a future reader does not read the gap as an
    oversight and 'fix' it with a regex."""
    assert "tour_step" not in telemetry_router.PREAUTH_LABEL_SCHEMAS["client.exception"]
