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

import pytest

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


_QUOTES = "'\"`"


def _set_literal_names(body: str) -> set[str]:
    """Every string literal in a JS array body, with a hard requirement that
    NOTHING ELSE is in there.

    This was `re.findall(r"'([A-Za-z0-9]+)'")`, which is not a parser: it saw
    single-quoted alphanumerics and was BLIND to everything else. A
    double-quoted name, a backtick name, a spread of another list, or a
    computed entry could join the runtime Set while this freeze stayed green
    -- the one failure a freeze exists to prevent. Both quote styles are legal
    JS and this repo's lint rules are the only thing discouraging them, which
    is a style gate, not a data-safety one.

    So the body is SCANNED. Comments and separators are skipped (a
    commented-out name is not in the Set at runtime), string literals in all
    three quote styles are collected, and ANYTHING the scanner cannot account
    for raises rather than being silently ignored -- an escape, a template
    substitution, an identifier, an unterminated literal.
    """
    names: set[str] = set()
    i, n = 0, len(body)
    while i < n:
        ch = body[i]
        if ch in " \t\r\n,":
            i += 1
        elif body.startswith("//", i):
            nl = body.find("\n", i)
            i = n if nl == -1 else nl + 1
        elif body.startswith("/*", i):
            end = body.find("*/", i + 2)
            if end == -1:
                raise ValueError("unterminated block comment")
            i = end + 2
        elif ch in _QUOTES:
            j, buf = i + 1, []
            while j < n and body[j] != ch:
                if body[j] == "\\":
                    raise ValueError(f"escape at offset {j}: not a bare class name")
                if ch == "`" and body.startswith("${", j):
                    raise ValueError(f"template substitution at offset {j}")
                buf.append(body[j])
                j += 1
            if j >= n:
                raise ValueError(f"unterminated string literal at offset {i}")
            names.add("".join(buf))
            i = j + 1
        else:
            raise ValueError(f"unparsed content at offset {i}: {body[i:i + 40]!r}")
    return names


def _js_known_classes() -> set[str]:
    """The literal names in web/src/telemetry.js's KNOWN_CLASSES."""
    src = CLIENT_JS.read_text(encoding="utf-8")
    block = re.search(r"const KNOWN_CLASSES = new Set\(\[(.*?)\n\]\)", src, re.S)
    assert block, "KNOWN_CLASSES not found in web/src/telemetry.js"
    return _set_literal_names(block.group(1))


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


def test_the_parser_reads_every_quote_style_and_refuses_the_rest():
    """Prove the checker can fail. The parser this replaced matched
    single-quoted alphanumerics only, so a double-quoted or backtick class --
    both legal JS -- could change the runtime vocabulary while every
    assertion above still passed against a stale reading of the file."""
    assert _set_literal_names(
        "'A', \"B\", `C`,\n  // 'D' retired\n  /* 'E' never shipped */\n"
    ) == {"A", "B", "C"}
    for hostile in (
        "'A', ...OTHER_CLASSES",     # a spread
        "'A', SOME_CONSTANT",        # an identifier
        "'A', `pre${suffix}`",       # a template substitution
        "'A', 'B",                   # unterminated
        "'A', 'B\\u0041'",           # escaped: not a bare name
        "'A', /* unterminated",
    ):
        with pytest.raises(ValueError):
            _set_literal_names(hostile)


def test_component_stack_hash_carries_the_documented_compat_pair():
    """`component_stack_hash` is the ONE label a client older than this
    release already emits: the pre-#537 ErrorBoundary sent
    `String(hash >>> 0)` from its own 32-bit shift-hash, 1 to 10 digits. Its
    rule therefore accepts BOTH widths for the rollout window and only those
    two, while the digests that are new in this release keep the strict rule
    -- no stale client can emit them."""
    schema = telemetry_router.PREAUTH_LABEL_SCHEMAS["client.exception"]
    rule = schema["component_stack_hash"]
    assert rule is telemetry_router._COMPONENT_STACK_HASH_RE
    assert rule.match("0" * DIGEST_WIDTH)      # this release
    assert rule.match("4294967295")            # the old 32-bit maximum
    assert rule.match("0")                     # and its minimum
    assert not rule.match("1" * 11)            # a width no client ever emitted
    assert not rule.match("1" * 15)
    assert not rule.match("abcdefgh")
    assert not rule.match("0" * DIGEST_WIDTH + "\n")
    for key in ("message_hash", "stack_hash"):
        assert schema[key] is telemetry_router._HASH_RE


def test_tour_step_is_deliberately_absent():
    """Closing it would need a mirror of two product-owned step tables that
    change with ordinary feature work, and a shape rule would accept
    `customer_secret`. Recorded so a future reader does not read the gap as an
    oversight and 'fix' it with a regex."""
    assert "tour_step" not in telemetry_router.PREAUTH_LABEL_SCHEMAS["client.exception"]
