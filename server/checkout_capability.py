"""
server/checkout_capability.py — the opaque, server-issued proof of a drawing
checkout.

WHY THIS EXISTS. The single-writer lock identifies its holder with a string the
CALLER supplies (`holder`), and GET /api/drawings/{id}/versions publishes that
string so the UI can render a "locked by X" chip. Every mutating route took the
same field from the caller. So the whole ownership test was "name the holder",
and the holder was public: session B, authenticated as any member of the same
tenant, could read session A's holder off the versions read and present it to
publish, release, undo or redo under A's lease. Adding a fence did not close it
either, because the fence was published on that same read.

A secret the attacker cannot read is the only thing that separates "I am the
holder" from "I know who the holder is". So acquiring a checkout now MINTS a
capability, and it is the capability — never the holder — that authorizes a
mutation.

WHAT IT BINDS. The token is `HMAC-SHA256(secret, tenant | subject | drawing |
fence)`, hex, behind a scheme prefix. Nothing about it is readable: it carries no
payload, only a tag, so there is nothing in it to decode, edit and re-sign.

  * tenant   — a capability issued in one workspace is void in another.
  * subject  — the AUTHENTICATED identity (`TenantContext.subject`, the Auth0
               `sub`), not the caller-supplied holder label. Two members of one
               tenant get different capabilities, so a leaked one is useless to a
               colleague. With LEAF_AUTH_LIVE=0 there is no authenticated subject
               and this component is empty; the token is still unforgeable (the
               secret is what makes it so), it just cannot separate two anonymous
               callers on the open demo — which is the same identity the demo
               gives every other route.
  * drawing  — a capability for one drawing does not move to another.
  * fence    — the lock GENERATION, bumped by every acquire and never reused
               (da/store.py `_next_fence`). This is what makes the token
               single-use per lease: the moment a lease lapses and the lock is
               re-acquired — even by the same person — every capability from the
               previous generation stops verifying.

The lease EXPIRY is not in the token because it does not need to be: verification
reads the live lock and refuses one that is not active, so a capability cannot
outlive the checkout it names.

WHERE IT TRAVELS. In the `X-Checkout-Capability` request header, never a body
field and never a query parameter — it is a credential, so it stays out of URLs,
out of the persisted job record, and out of anything echoed back to a client. The
app process is the only one that ever sees it: routes exchange it for the
(holder, fence) pair the store already understands, and it is that pair, not the
capability, that travels down the broker/write-loop chain.
"""
from __future__ import annotations

import hmac
import os
import secrets
import sys
from hashlib import sha256
from typing import Any, Dict, Optional, Tuple

CAPABILITY_HEADER = "X-Checkout-Capability"

# Scheme tag, so a token is recognizable in a log or a bug report without being
# guessable, and so the format can be rotated later without ambiguity.
_SCHEME = "lco1"
_BINDING_VERSION = "leaf.checkout.capability.v1"
_FIELD_SEP = "\x1f"  # ASCII unit separator: cannot occur in any bound component

_SECRET_ENV = "LEAF_CHECKOUT_CAP_SECRET"
_RUNTIME_ENV = "LEAF_RUNTIME_ENV"
_AUTH_LIVE_ENV = "LEAF_AUTH_LIVE"
_MIN_PRODUCTION_SECRET_BYTES = 32

_EPHEMERAL_SECRET: Optional[str] = None


class CapabilityRejected(Exception):
    """The presented capability does not prove ownership of the active lock.

    Carries the operator-facing reason. Routes render this as HTTP 403, matching
    the answer DELETE .../checkout has always given a non-holder — the request is
    well formed and the drawing exists, the caller simply lacks authority.
    """


class CapabilityUnavailable(Exception):
    """No signing secret is configured in a posture that requires one.

    Distinct from `CapabilityRejected` because it is an OPERATOR fault, not a
    caller fault: the deployment cannot mint or verify anything, so every write
    would be refused. Routes render this as 503, never 403 — a misconfigured
    server must not look like an unauthorized user.
    """


def _production_posture() -> bool:
    return os.environ.get(_RUNTIME_ENV, "").strip().lower() == "production"


def _auth_live_posture() -> bool:
    return os.environ.get(_AUTH_LIVE_ENV, "0") == "1"


def _secret() -> str:
    """The HMAC signing secret.

    `LEAF_CHECKOUT_CAP_SECRET` when set. In production posture that is the ONLY
    answer: a fail-closed error beats silently minting capabilities that a second
    replica cannot verify, which would present as sporadic 403s on legitimate
    writes.

    Off production the module falls back to a per-process random secret so a
    clean checkout, the test harness and `docker compose up` all work with no
    configuration. The consequence is stated rather than hidden: capabilities do
    not survive an app restart and do not span app processes. A holder whose
    capability was invalidated that way cannot release or take over its own live
    lease and must wait out the lease TTL, so any deployment running more than
    one app process — or that cares about restart continuity — must set the env
    var. (The documented deployment is a single app replica; see
    docker-compose.yml, which pins one for the SQLite write path.)
    """
    global _EPHEMERAL_SECRET
    configured = os.environ.get(_SECRET_ENV, "").strip()
    if configured:
        if (_production_posture()
                and len(configured.encode("utf-8")) < _MIN_PRODUCTION_SECRET_BYTES):
            raise CapabilityUnavailable(
                f"{_SECRET_ENV} must contain at least "
                f"{_MIN_PRODUCTION_SECRET_BYTES} bytes when "
                f"{_RUNTIME_ENV}=production")
        return configured
    if _production_posture():
        raise CapabilityUnavailable(
            f"{_SECRET_ENV} is required when {_RUNTIME_ENV}=production: without "
            f"it, checkout capabilities cannot be verified across app processes "
            f"or restarts")
    if _EPHEMERAL_SECRET is None:
        _EPHEMERAL_SECRET = secrets.token_hex(32)
        print(f"[leaf-demo] {_SECRET_ENV} unset; using a per-process checkout "
              f"capability secret. Checkouts taken before an app restart cannot "
              f"be released or written to afterwards until their lease expires.",
              file=sys.stderr)
    return _EPHEMERAL_SECRET


def _subject_of(tenant: Any) -> str:
    """The AUTHENTICATED subject to bind, or "" when auth is off.

    Read off `deps.TenantContext`, which only carries a subject when
    LEAF_AUTH_LIVE=1 verified a token. Never falls back to a caller-supplied
    value: a subject a caller can choose binds the capability to nothing.
    """
    subject = getattr(tenant, "subject", None)
    if isinstance(subject, str) and subject:
        return subject
    if _auth_live_posture():
        raise CapabilityUnavailable(
            "an authenticated subject is required to mint or verify a checkout "
            "capability when LEAF_AUTH_LIVE=1")
    return ""


def mint(tenant: Any, drawing_id: str, fence: int) -> str:
    """Issue the capability for one lease. Called ONLY by the acquire route, and
    the returned value is handed to that caller alone."""
    binding = _FIELD_SEP.join((
        _BINDING_VERSION, str(tenant), _subject_of(tenant), str(drawing_id),
        str(int(fence)),
    ))
    tag = hmac.new(_secret().encode("utf-8"), binding.encode("utf-8"),
                   sha256).hexdigest()
    return f"{_SCHEME}.{tag}"


def verify(presented: Optional[str], tenant: Any, drawing_id: str,
           checkout: Optional[Dict[str, Any]]) -> Tuple[str, int]:
    """Prove ownership of the drawing's ACTIVE lock. Returns `(holder, fence)`.

    The returned pair is what the caller passes to the store primitives — the
    holder as recorded in the manifest, NOT anything the caller sent, so a
    request cannot influence which lease it acts on.

    Raises `CapabilityRejected` for every failure, with no detail that would help
    an attacker distinguish "wrong generation" from "wrong subject". Callers must
    only reach this when a lock is actually active; an absent or expired lock
    needs no proof (`da/store.py` grants those to anyone) and has no fence to
    bind against.
    """
    co = checkout or {}
    fence = co.get("fence")
    if fence is None:
        # A lock taken before the legacy authority stamped generations. It cannot
        # be verified against anything, and guessing a generation for it would
        # make every capability for this drawing interchangeable. Refused until
        # the lease expires, at which point the lock is free again.
        raise CapabilityRejected(
            "this checkout was taken before capability-based ownership and "
            "cannot be proven; wait for its lease to expire, then re-acquire it")
    if not isinstance(presented, str) or not presented:
        raise CapabilityRejected(
            f"this drawing is checked out; a {CAPABILITY_HEADER} header from "
            f"POST /api/drawings/{{id}}/checkout is required to change it")
    expected = mint(tenant, drawing_id, int(fence))
    if not hmac.compare_digest(presented.strip(), expected):
        raise CapabilityRejected(
            "the presented checkout capability does not match this drawing's "
            "active checkout; re-acquire it to get a current one")
    return str(co.get("holder")), int(fence)
