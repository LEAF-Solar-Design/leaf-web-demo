"""
Client-facing session routes (sessions wire spec, gap audit §2.1 /
CONTRACT-ADDENDUM sessions addendum). Matches ``console/converse.js``
byte-for-byte:

    POST /api/sessions                  -> get_or_create_session (idempotent)
    POST /api/sessions/{id}/messages    -> turn dispatch (turn_runner.start_turn)
    GET  /api/sessions/{id}/stream      -> SSE, event-per-frame, after_seq replay
    GET  /api/sessions/{id}/transcript  -> poll fallback, most-recent-N ascending

``POST /api/agent/approvals/{confirmation_id}`` is a SIBLING router
(routers/agent.py, S4 lane) — record-only approval decisions live there, not
here; this file never calls ``session_store.decide_approval``.

Ownership guard posture (matches routers/jobs.py's cross-tenant pattern,
deps.py's require_tenant doc): an unknown session_id and a real-but-foreign-
tenant session_id collapse to the IDENTICAL 404 session_not_found response —
no existence leak across the tenant boundary.

CONFIRM-SHAPE SEAM: the client's ``{confirm: {confirmationId, approved}}`` is
NOT the frozen ``ConverseTurnInput.confirm`` shape the harness expects — this
router is the ONE place that bridges them. It looks up the approval row
(created by turn_runner when the harness emitted confirmation_required) and
builds ``{confirmation_id, approved, proposal: {tool, params, dwg?, capability}}``
from it before ever calling ``turn_runner.start_turn`` — the params/tool/
capability the harness resumes with come from the DURABLE row, never from the
client (the client only ever sends confirmationId + approved).

APPROVAL CONSUME (merge-gate finding #1): the client's ``confirm.approved``
boolean is NEVER trusted on its own — this path calls
``session_store.consume_approval(confirmation_id, session_id, tenant_id)``,
ONE atomic locked check-then-set that verifies the row belongs to THIS
session+tenant, has actually been decided (via the separate
``POST /api/agent/approvals/{confirmation_id}`` call, routers/agent.py), is
not expired, and has not already been consumed by a prior confirm — then
returns the DURABLY STORED ``approved`` value, which is what gets wired into
the ``ConverseTurnInput.confirm`` sent to the harness. A client that skips
the approvals call, replays a confirm, reuses another session's
confirmation_id, or sends ``approved: true`` against a row that was actually
decided ``approved: false`` all fail closed. Wire-compat: console/converse.js
(leaf_website/console/converse.js)'s ``approve()`` ALWAYS POSTs
``/api/agent/approvals/{confirmationId}`` (which durably records the decision)
BEFORE its caller sends the confirm message via ``postMessage({confirm})`` —
see that file's ``approve()``/``postMessage()`` comments — so requiring
``decided`` here is compatible with every real client.

APPROVAL GIVE-BACK: the consume above necessarily runs BEFORE the turn's busy
compare-and-swap, because that CAS lives inside ``turn_runner.start_turn``
(``session_store.try_begin_turn``) and start_turn is one call. So a confirm
that races a concurrent turn gets consumed and then answered 409
TURN_IN_PROGRESS — and without a give-back the retry that 409 explicitly
invites could only ever fail ``already_consumed``, destroying the user's one
approval. The ``TurnBusy`` handler therefore calls
``session_store.unconsume_approval``.

The rule is NOT "roll back on TurnBusy". It is: **roll back exactly when the
harness provably never saw the turn, and never otherwise.** Two sites qualify.

``TurnBusy`` means try_begin_turn lost the CAS, which is the first thing
start_turn does after the session guard — no ``turn_started`` event was
appended, no request reached the harness, so nothing anywhere redeemed the
approval and giving it back cannot produce a second redemption.

``TurnRejected`` qualifies when it carries ``approval_unredeemed``. That covers
``pre_harness`` (no POST was attempted, or the connection was never
established) AND the refusals that reach the harness but are answered before
the runner is ever entered: 400/431 request validation, 401 auth gate or
missing grant, 413 body reader, 429 grant-pool exhaustion. In every one of
those the grant is resolved before the session mirror and before ConverseLoop
(``harness/src/agent/spineTurnAdapter.ts``), so nothing can have redeemed the
confirmation. Those legs are marked retryable, so skipping the give-back would
invite a retry against an approval already spent — the defect this note exists
to fix.

It DEFAULTS TO FALSE, so every ambiguous leg still refuses to roll back: on a
read timeout, or a 500 the harness returned after ConverseLoop had already
resolved a confirmation and begun acting on it, the harness may already be
executing the tool call, and un-spending that approval is precisely the
double-execution consume-once exists to prevent.

Single redemption is preserved in both directions: a confirm whose turn really
started still replays into ``already_consumed`` (see
tests/test_sessions_routes.py::test_messages_confirm_busy_gives_the_approval_back).

When a give-back does not succeed the approval stays spent, and the response
says so rather than advertising a retry that could only fail — see
``_busy_response``/``_turn_rejected_response``'s ``approval_recovered``.
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import json
import os
import re
import sys
import time
import uuid
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

import deps
import emf_metrics
import entitlements
import instant_execution
import platform_link
import request_journal
import session_policy
import session_store
import turn_runner
from envelopes import (ErrorCode, err_envelope, error_obj, error_response,
                       with_envelope_fields)

router = APIRouter()


# --------------------------------------------------------------------------- #
# "Mount your LLM" input validation (model allowlist + BYO credential hygiene).
# --------------------------------------------------------------------------- #
def _invalid_model_response(model: Any) -> JSONResponse:
    allowed = ", ".join(sorted(turn_runner.ALLOWED_MODELS))
    return error_response(
        ErrorCode.BAD_PARAMS,
        f"model {model!r} is not allowed; choose one of: {allowed}",
        retryable=False, status_code=400,
    )


# A credential must actually LOOK like one. Accepting any non-empty string made
# downstream redaction unwinnable: the harness scrubs the grant out of the
# transcript by literal match, so a credential of "error", "a" or '"' would
# either corrupt ordinary text and protocol values or have to be left in place
# (sol-critic PR #117 round 4, blockers 2 and 3). Real Agent SDK credentials are
# long, opaque, and whitespace-free — `sk-ant-...` keys and OAuth tokens run to
# ~100 chars — so this floor rejects nothing genuine while removing every
# pathological value at the boundary, where it is cheap and unambiguous.
_MIN_CREDENTIAL_LEN = 24

# PRINTABLE ASCII, no space. Deliberately NOT "not str.isspace()": Python's
# isspace() and JavaScript's \s disagree (U+FEFF is whitespace to \s but not to
# isspace()), so such a credential was ACCEPTED here yet treated as unredactable
# by harness/src/redact.ts — accepted but unstrippable is the worst of both.
# Keep this rule identical to that file's PRINTABLE_ASCII.
# (sol-critic PR #123 rounds 6-8.)
_CREDENTIAL_CHARS = frozenset(chr(c) for c in range(0x21, 0x7F))


def _valid_credential_value(tok: Any) -> bool:
    return (
        isinstance(tok, str)
        and len(tok) >= _MIN_CREDENTIAL_LEN
        and all(ch in _CREDENTIAL_CHARS for ch in tok)
    )


def _validate_credential_grant(raw: Any) -> Optional[Dict[str, Any]]:
    """Return the normalized BYO Agent SDK credential grant, or None if the shape
    is invalid. Accepts EXACTLY {kind:'api_key', api_key:<credential>} or
    {kind:'oauth', oauth_token:<credential>}; extra keys are dropped. A
    <credential> is at least _MIN_CREDENTIAL_LEN characters and entirely
    PRINTABLE ASCII (_CREDENTIAL_CHARS, 0x21-0x7E) — no space, no control
    character, no non-ASCII codepoint. The token VALUE is never logged here
    (nothing in this module prints it)."""
    if not isinstance(raw, dict):
        return None
    kind = raw.get("kind")
    if kind == "api_key":
        tok = raw.get("api_key")
        if _valid_credential_value(tok):
            return {"kind": "api_key", "api_key": tok}
    elif kind == "oauth":
        tok = raw.get("oauth_token")
        if _valid_credential_value(tok):
            return {"kind": "oauth", "oauth_token": tok}
    return None


def _truthy_env(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def _credential_insecure_transport(request: Request) -> bool:
    """True when a BYO credential must be REJECTED because the request did not
    arrive over TLS.

    X-Forwarded-Proto is CALLER-CONTROLLED unless a trusted proxy actually sets
    it. docker-compose publishes this app straight to :8130 with nothing in
    front, so honoring that header unconditionally would let anyone bypass the
    gate by adding one line to a plaintext request (sol-critic review of PR #117,
    blocker 1). The header is therefore consulted ONLY when the deployment
    asserts it sits behind a proxy that overwrites it, via
    LEAF_TRUST_FORWARDED_PROTO=1. Unset (the default, including docker-compose)
    means the real transport decides and the header is ignored.

    LEAF_ALLOW_INSECURE_CREDENTIAL=1 remains the explicit dev/test opt-out.
    Both flags fail CLOSED: absent or unparsable -> enforce TLS on the real
    connection."""
    if _truthy_env("LEAF_ALLOW_INSECURE_CREDENTIAL"):
        return False
    scheme = request.url.scheme.lower()
    if _truthy_env("LEAF_TRUST_FORWARDED_PROTO"):
        forwarded = request.headers.get("x-forwarded-proto", "")
        if forwarded:
            scheme = forwarded.split(",")[0].strip().lower()
    return scheme != "https"

# SSE polling cadence (§2.1.3): cheap poll of the durable event log, a ": ping"
# comment to keep idle connections alive through proxies, and a bounded
# lifetime after which the client's own reconnect-with-after_seq logic takes
# over. Read as bare module globals INSIDE the generator (not captured as
# default args) so tests can monkeypatch them for a fast round-trip.
STREAM_POLL_S = 0.3
STREAM_PING_S = 15.0
STREAM_DEADLINE_S = 300.0

# GET .../transcript?limit= default + hard cap (session_store.recent_events
# also clamps internally; clamping here too keeps this route's own contract
# self-evident and independent of the store's internal choice).
TRANSCRIPT_DEFAULT_LIMIT = 200
TRANSCRIPT_MAX_LIMIT = 10000

# HTTP status a TurnRejected(status_code, ...) is allowed to carry, mapped to
# a sane `retryable` flag for the §10 error object (TurnRejected itself only
# carries status_code/error_code/message/extra — retryable is a presentation
# concern this router owns, mirroring author.py/tenant.py's own per-code
# retryable choices for the same underlying codes).
_RETRYABLE_BY_CODE = {
    ErrorCode.GRANT_REQUIRED: False,
    ErrorCode.LLM_QUOTA_EXHAUSTED: False,
    ErrorCode.LLM_RATE_LIMITED: True,
    ErrorCode.BROKER_UNREACHABLE: True,
    ErrorCode.SESSION_NOT_FOUND: False,
}


# --------------------------------------------------------------------------- #
# Conversation scope (standardization slice 6b)
# --------------------------------------------------------------------------- #
#: The closed kind set is session_store's; the wire, the store and
#: web/src/converse.js SCOPE_KINDS are pinned equal by test_contract_freeze.
SCOPE_KINDS = session_store.SCOPE_KINDS
#: A handle is an opaque identifier (drawing id, project uuid, entity handle):
#: bounded and charset-limited so it can be stored, listed and echoed without
#: ever carrying free text. `:` is allowed inside a handle; the query form
#: `scope=<kind>:<handle>` splits on the FIRST colon, and no kind contains one.
SCOPE_HANDLE_MAX = 128
SCOPE_HANDLE_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:@~+-]{0,127}$"
_SCOPE_HANDLE_RE = re.compile(SCOPE_HANDLE_PATTERN)
#: The drawing key a project-scoped attach carries when it names no drawing:
#: the same sentinel web/src/converse.js uses for its cache key, so the two
#: ends agree on which session a project conversation with no drawing is.
DEFAULT_SCOPE_DRAWING_ID = "default"


class SessionScope(BaseModel):
    """`{kind, handle}`, closed-world and bounded AT THE WIRE: an unknown kind,
    an extra key, an empty, over-long or off-charset handle is a 422 before
    any store call (fail closed, never coerced)."""
    model_config = ConfigDict(extra="forbid")
    kind: str
    handle: str = Field(min_length=1, max_length=SCOPE_HANDLE_MAX)

    @field_validator("kind")
    @classmethod
    def _known_kind(cls, value: str) -> str:
        if value not in SCOPE_KINDS:
            raise ValueError(f"scope.kind must be one of: {', '.join(SCOPE_KINDS)}")
        return value

    @field_validator("handle")
    @classmethod
    def _bounded_handle(cls, value: str) -> str:
        if not _SCOPE_HANDLE_RE.fullmatch(value):
            raise ValueError("scope.handle carries an unsupported character")
        return value


class CreateSessionRequest(BaseModel):
    # The legacy identity field STAYS. Optional only so `scope` can supply it
    # (a drawing scope names the drawing; a project scope defaults it); the
    # validator below refuses a body that names neither, so the old wire is
    # unchanged: a bare `{}` is still a 422.
    drawing_id: Optional[str] = None
    # Optional canonical browser-project binding. The server derives its org
    # and membership from the verified identity; no client org/binding hint is
    # accepted.
    project_id: Optional[str] = Field(default=None, exclude_if=lambda value: value is None)
    # Per-session "mount your LLM" model choice (persisted). Validated against the
    # allowlist; overrides the runner env default for this session's turns.
    model: Optional[str] = None
    # Per-session approval policy (session_policy.POLICIES). Absent (default)
    # leaves the stored policy untouched — a repeat idempotent POST without the
    # field never resets an earlier choice. confirm_all is the implicit default.
    policy: Optional[str] = None
    # Slice 6b: the conversation scope envelope. Absent -> derived from the
    # legacy fields (project when bound, else drawing); present -> must AGREE
    # with them where they overlap, or the body is refused rather than one
    # field silently winning.
    scope: Optional[SessionScope] = None

    @model_validator(mode="after")
    def _scope_agrees_with_identity(self) -> "CreateSessionRequest":
        scope = self.scope
        if scope is None:
            if self.drawing_id is None:
                raise ValueError("drawing_id is required when scope is absent")
            return self
        if scope.kind == "drawing":
            if self.drawing_id is None:
                self.drawing_id = scope.handle
            elif self.drawing_id != scope.handle:
                raise ValueError("scope.handle must equal drawing_id for a drawing scope")
        elif scope.kind == "project":
            if self.project_id is None:
                self.project_id = scope.handle
            elif self.project_id != scope.handle:
                raise ValueError("scope.handle must equal project_id for a project scope")
            if self.drawing_id is None:
                self.drawing_id = DEFAULT_SCOPE_DRAWING_ID
        else:  # entity: the handle names an entity INSIDE a drawing
            if self.drawing_id is None:
                raise ValueError("an entity scope requires drawing_id")
        return self


def _scope_of(sess: Dict[str, Any]) -> Dict[str, str]:
    """The `{kind, handle}` a row answers with: the stored envelope when the
    row has one, else derived from its identity (project when bound, else
    drawing). Rows from before 0053 read NULL and land in the second branch;
    nothing is invented and nothing is backfilled."""
    kind, handle = sess.get("scope_kind"), sess.get("scope_handle")
    if kind in SCOPE_KINDS and isinstance(handle, str) and handle:
        return {"kind": kind, "handle": handle}
    if sess.get("project_id") is not None:
        return {"kind": "project", "handle": str(sess["project_id"])}
    return {"kind": "drawing", "handle": str(sess.get("drawing_id") or "")}


# GET /api/sessions paging. The page cap is the STORE's cap (one number, two
# readers), the default is what ConversationList asks for.
LIST_DEFAULT_LIMIT = 20
LIST_MAX_LIMIT = session_store.LIST_MAX_LIMIT
_CURSOR_MAX_LEN = 256


def _parse_scope_query(raw: Optional[str]):
    """`scope=<kind>:<handle>` -> (kind, handle), or (None, None) when absent.
    Reuses SessionScope's validators so the query form can never accept a
    scope the body form refuses. Raises ValueError on anything malformed."""
    if raw is None or raw == "":
        return None, None
    if len(raw) > len(max(SCOPE_KINDS, key=len)) + 1 + SCOPE_HANDLE_MAX:
        raise ValueError("scope is too long")
    kind, sep, handle = raw.partition(":")
    if not sep:
        raise ValueError("scope must be <kind>:<handle>")
    parsed = SessionScope(kind=kind, handle=handle)  # ValidationError is a ValueError
    return parsed.kind, parsed.handle


def _encode_cursor(cursor) -> Optional[str]:
    if cursor is None:
        return None
    raw = json.dumps([float(cursor[0]), str(cursor[1])], separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii").rstrip("=")


def _decode_cursor(raw: Optional[str]):
    """Opaque keyset cursor -> (updated_at, session_id). Bounded, shape-checked,
    fails closed: anything but our own encoding of a [number, string] pair is
    a ValueError, never a partial page."""
    if raw is None or raw == "":
        return None
    if len(raw) > _CURSOR_MAX_LEN:
        raise ValueError("cursor is too long")
    padded = raw + "=" * (-len(raw) % 4)
    try:
        decoded = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
    except (binascii.Error, ValueError, UnicodeDecodeError) as exc:
        raise ValueError("cursor is not decodable") from exc
    if (not isinstance(decoded, list) or len(decoded) != 2
            or isinstance(decoded[0], bool) or not isinstance(decoded[0], (int, float))
            or not isinstance(decoded[1], str) or not decoded[1]
            or len(decoded[1]) > 128):
        raise ValueError("cursor has the wrong shape")
    return float(decoded[0]), decoded[1]


def _session_row(sess: Dict[str, Any]) -> Dict[str, Any]:
    """One list row. The five fields the slice froze plus the three a resume
    needs: `last_seq` seeds openStream(after_seq), `drawing_id`/`project_id`
    let the client re-attach by scope. Never the model, never the active turn,
    never anything a transcript read would not also show the same tenant."""
    return {
        "id": sess["session_id"],
        "scope": _scope_of(sess),
        "title": sess.get("title"),
        "updated_at": sess.get("updated_at"),
        "turn_count": int(sess.get("turn_count") or 0),
        "last_seq": int(sess.get("last_seq") or 0),
        "drawing_id": sess.get("drawing_id"),
        "project_id": (
            str(sess["project_id"]) if sess.get("project_id") is not None else None
        ),
    }


class MessageRequest(BaseModel):
    # P5a idempotency identity. In PostgreSQL sessions mode, eligible plain-text
    # messages receive a server UUID when absent and may supply that UUID on an
    # ambiguous retry. Confirmations, images, and credential-bearing turns keep
    # their existing consume-once/direct behavior and cannot opt into this key.
    # Any at the wire boundary is intentional. Legacy stores historically
    # ignored unknown request_id shapes; PostgreSQL mode validates the exact
    # canonical UUID before admission without changing that legacy contract.
    request_id: Any = None
    text: Optional[str] = None
    confirm: Optional[Dict[str, Any]] = None
    # Inline image attachments are bounded before the entitlement and approval
    # gates. They are recorded in the transcript and sent to the harness, but
    # never enter the busy-turn queue (which is in-memory).
    images: Optional[List[Dict[str, Any]]] = None
    classifier_hint: Optional[Dict[str, Any]] = None
    # Per-turn model override (validated); falls back to the session's stored model.
    model: Optional[str] = None
    # Optional bring-your-own Agent SDK credential for THIS turn only. Ephemeral:
    # validated for shape, forwarded over TLS, never persisted, never logged.
    credential_grant: Optional[Dict[str, Any]] = None
    # OPT-IN busy-turn queue (cap 1): when the session is mid-turn, park this
    # text prompt and start it at the active turn's terminal event instead of
    # answering 409. Opt-in keeps deployed clients byte-identical: absent (the
    # default), a busy session answers exactly the 409 it always has. Text-only:
    # a confirm or a credential_grant with queue=true is a 400, never queued.
    queue: Optional[bool] = False


_IMAGE_MEDIA_TYPES = {"image/png", "image/jpeg", "image/webp", "image/gif"}
_MAX_IMAGES_PER_MESSAGE = 3
_MAX_IMAGE_BYTES = 1024 * 1024
_MAX_IMAGES_BYTES = 1024 * 1024
# One decoded MiB expands to at most 1,398,104 base64 characters. Leave bounded
# JSON framing room while refusing declared oversize bodies before FastAPI parses.
_MAX_MESSAGE_BODY_BYTES = 1_500_000


def _image_magic_matches(media_type: str, decoded: bytes) -> bool:
    """Check the FULL signature, not a convenient prefix.

    A four-byte PNG test passes anything beginning \\x89PNG, and "GIF8" admits
    versions that are neither GIF87a nor GIF89a. The point of the check is that
    the declared media_type and the actual bytes agree before those bytes become
    a vision content block, so each signature is matched in full.
    """
    if media_type == "image/png":
        return decoded.startswith(b"\x89PNG\r\n\x1a\n")
    if media_type == "image/jpeg":
        # SOI plus the first marker, and that is the whole signature. The byte
        # after \\xff\\xd8\\xff is a marker code and legitimately varies, and
        # JPEG has no fixed tail: real encoders emit files with bytes after EOI,
        # which decoders read happily. Requiring EOI last rejected a genuine
        # ffmpeg-produced photo with 16 bytes of padding. A check that turns
        # away real user images is worse than the shallow one it replaced.
        return decoded.startswith(b"\xff\xd8\xff")
    if media_type == "image/gif":
        return decoded.startswith(b"GIF87a") or decoded.startswith(b"GIF89a")
    # RIFF, a little-endian size, then WEBP — and the declared size must
    # actually describe the payload rather than being arbitrary filler.
    if len(decoded) < 12 or not decoded.startswith(b"RIFF") or decoded[8:12] != b"WEBP":
        return False
    return int.from_bytes(decoded[4:8], "little") == len(decoded) - 8


class _MessageBodyTooLarge(Exception):
    """Raised inside the ASGI receive wrapper to abort an oversized body."""


class MessageBodyLimitMiddleware:
    """Byte-counting ASGI guard on POST /api/sessions/{id}/messages.

    Two things have to be true at once, and only a middleware gets both.

    The cap must be a real MEMORY bound: a chunked request declares no
    Content-Length, so any check that reads the body and then measures it has
    already paid the cost. This wraps `receive` and aborts the moment the
    cumulative body passes the cap, exactly as UploadBodyLimitMiddleware does
    for the upload route.

    And FastAPI must still do the parsing, so there is ONE implementation of the
    wire contract. A dependency cannot achieve both, because FastAPI reads the
    body before it solves dependencies; bounding the stream underneath the
    framework is what lets the framework keep the parsing.
    """

    _PATH = re.compile(r"^/api/sessions/[^/]+/messages$")

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if (scope.get("type") != "http" or scope.get("method") != "POST"
                or not self._PATH.match(scope.get("path") or "")):
            await self.app(scope, receive, send)
            return

        headers = {k.decode("latin-1").lower(): v.decode("latin-1")
                   for k, v in scope.get("headers", [])}
        declared = headers.get("content-length", "")
        if declared.isdigit() and int(declared) > _MAX_MESSAGE_BODY_BYTES:
            await self._send_413(send)
            return

        seen = 0
        response_started = False

        async def counting_receive():
            nonlocal seen
            message = await receive()
            if message["type"] == "http.request":
                seen += len(message.get("body") or b"")
                if seen > _MAX_MESSAGE_BODY_BYTES:
                    raise _MessageBodyTooLarge()
            return message

        async def tracking_send(message):
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, counting_receive, tracking_send)
        except _MessageBodyTooLarge:
            if response_started:  # pragma: no cover - the abort precedes the response
                raise
            await self._send_413(send)

    @staticmethod
    async def _send_413(send):
        payload = json.dumps({
            "error": {"error_code": "BAD_PARAMS",
                      "message": "request exceeds the 1MB image message cap",
                      "retryable": False},
            "degraded_mode": False,
        }).encode("utf-8")
        await send({"type": "http.response.start", "status": 413,
                    "headers": [(b"content-type", b"application/json"),
                                (b"content-length", str(len(payload)).encode())]})
        await send({"type": "http.response.body", "body": payload})



def _validate_images(images: Optional[List[Dict[str, Any]]]) -> Optional[List[Dict[str, str]]]:
    """Validate decoded image bytes and their claimed media type."""
    if images is None:
        return None
    if len(images) > _MAX_IMAGES_PER_MESSAGE:
        raise ValueError(f"at most {_MAX_IMAGES_PER_MESSAGE} images are allowed per message")

    total_size = 0
    validated: List[Dict[str, str]] = []
    for index, image in enumerate(images, start=1):
        media_type = image.get("media_type")
        data = image.get("data")
        # isinstance FIRST: `media_type` comes straight from client JSON, and
        # a list or dict is unhashable — `in` on a set would raise TypeError
        # and turn a bad request into a 500.
        if not isinstance(media_type, str) or media_type not in _IMAGE_MEDIA_TYPES:
            allowed = ", ".join(sorted(_IMAGE_MEDIA_TYPES))
            raise ValueError(f"image {index} media_type must be one of: {allowed}")
        if not isinstance(data, str) or not data:
            raise ValueError(f"image {index} data must be non-empty base64")
        try:
            decoded = base64.b64decode(data, validate=True)
        except (binascii.Error, ValueError, TypeError) as exc:
            raise ValueError(f"image {index} data must be valid base64") from exc
        if len(decoded) > _MAX_IMAGE_BYTES:
            raise ValueError("each image must be at most 1MB decoded")
        total_size += len(decoded)
        if total_size > _MAX_IMAGES_BYTES:
            raise ValueError("images must total at most 1MB decoded")
        if not _image_magic_matches(media_type, decoded):
            raise ValueError(f"image {index} media_type does not match its bytes")
        validated.append({"media_type": media_type, "data": data})
    return validated


def _session_not_found(session_id: str) -> JSONResponse:
    return error_response(
        ErrorCode.SESSION_NOT_FOUND, f"unknown session_id {session_id!r}", retryable=False,
    )


def _require_owned_session(
    session_id: str, tenant: Any, write: bool = False,
) -> Optional[Dict[str, Any]]:
    """Look up one session and re-read project membership when it is bound."""
    sess = session_store.get_session(session_id)
    return platform_link.require_project_session_access(
        sess, tenant, write=write,
    )


def _project_forbidden_response() -> JSONResponse:
    return error_response(
        ErrorCode.BAD_PARAMS,
        "current project role does not permit this conversation action",
        retryable=False, status_code=403,
    )


def _give_back_unredeemed_approval(confirmation_id: str, session_id: str,
                                   tenant_id: str) -> bool:
    """Return a consumed-but-never-redeemed approval to the shelf (see the
    module docstring's APPROVAL GIVE-BACK note for when this is legal).

    Returns True IFF the approval is genuinely back on the shelf. A raise and a
    False both mean it is still spent, and the CALLER MUST NOT then tell the
    client to retry — the retry could only fail `already_consumed`. Callers
    thread this through to `_busy_response`/`_turn_rejected_response` so the
    answer stays honest about what the client can actually do next.

    A store failure is not re-raised: the turn genuinely is busy (or rejected),
    and a 500 would replace an accurate status with a misleading one. It is
    reported instead — loudly to stderr for the operator, and truthfully to the
    client via `approval_recovered: false`. The confirmation_id is a
    server-issued opaque id the client already holds — not a secret."""
    try:
        recovered = session_store.unconsume_approval(
            confirmation_id, session_id, tenant_id)
    except Exception as exc:  # noqa: BLE001
        _report_give_back_failure(
            "raised", confirmation_id, session_id,
            detail=f"{type(exc).__name__}: {exc}")
        return False
    if not recovered:
        # No row moved 1 -> 0. We consumed it moments ago, so this means the
        # row is not in the state we left it in — never silently swallow it.
        _report_give_back_failure("not_released", confirmation_id, session_id)
    return recovered


def _report_give_back_failure(reason: str, confirmation_id: str, session_id: str,
                              detail: str = "") -> None:
    """Report a destroyed approval on BOTH channels, and never raise.

    A failed give-back means the user's single approval is gone, with no
    symptom the user or an operator would otherwise see — the highest-priority
    kind of bug to leave silent. So it goes out twice: a human-readable stderr
    line for log reading, and an ApprovalGiveBackFailed EMF metric that
    CloudWatch extracts so it can actually be ALARMED on (a stderr string
    cannot be). Reporting is best-effort — this runs while we are already
    answering an error, and must not replace that answer with a 500."""
    try:
        print(
            f"[leaf-agent] approval give-back FAILED ({reason}) confirmation_id="
            f"{confirmation_id!r} session={session_id!r}"
            f"{' error=' + detail if detail else ''} — the approval is still "
            f"spent and this client must obtain a NEW approval",
            file=sys.stderr, flush=True,
        )
    except Exception:  # noqa: BLE001  # pragma: no cover
        pass
    try:
        emf_metrics.emit_approval_give_back_failed(
            reason, confirmation_id=confirmation_id, session_id=session_id)
    except Exception:  # noqa: BLE001  # pragma: no cover
        pass


_WALL_KIND_BY_CODE = {
    "GRANT_REQUIRED": "grant",
    "llm_quota_exhausted": "llm_quota",
    "llm_rate_limited": "llm_rate",
    "turn_in_progress": "busy",
    "ENTITLEMENT_REQUIRED": "entitlement",
    "BROKER_UNREACHABLE": "unreachable",
}


def _emit_agent_wall_kind(tenant, session_id, wall_kind: str, http_status: int,
                          error_code: str) -> None:
    """Best-effort `agent.wall_hit` product event (P2): THE chat activation
    blockers, per tenant. Never raises; skipped when identity is absent.
    Covers the TurnRejected choke point PLUS the two walls that answer
    without a TurnRejected: TurnBusy's 409 and the entitlement denial
    (review #426 round-1 warn 5)."""
    try:
        import telemetry_sink

        if tenant is None:
            return
        tid = str(tenant)
        telemetry_sink.emit(
            "agent.wall_hit",
            tenant_id=tid,
            tenant_kind="guest" if tid.startswith("guest-") else "account",
            session_id=str(session_id) if session_id else "none",
            labels={
                "wall_kind": wall_kind,
                "http_status": http_status,
                "error_code": error_code,
            },
        )
    except Exception:  # noqa: BLE001 - telemetry never touches the response
        pass


def _emit_agent_wall(exc: "turn_runner.TurnRejected", tenant, session_id) -> None:
    _emit_agent_wall_kind(
        tenant, session_id,
        _WALL_KIND_BY_CODE.get(exc.error_code, exc.error_code),
        exc.status_code, exc.error_code)


def _turn_rejected_response(exc: "turn_runner.TurnRejected",
                            approval_lost: bool = False,
                            tenant=None, session_id=None) -> JSONResponse:
    """TurnRejected -> HTTP response: exc.extra merged TOP-LEVEL (e.g.
    {'grant_required': True}) alongside the §10-valid `error` object.

    `approval_lost` says a confirm's approval was consumed and could NOT be
    given back. It forces `retryable` false and surfaces
    `approval_recovered: false`, because retrying with the same confirmation_id
    can then only fail `already_consumed` — see `_busy_response`."""
    _emit_agent_wall(exc, tenant, session_id)
    retryable = _RETRYABLE_BY_CODE.get(exc.error_code, False)
    if approval_lost:
        retryable = False
    body = with_envelope_fields({
        **exc.extra,
        **({"approval_recovered": False} if approval_lost else {}),
        "error": error_obj(exc.error_code, exc.message, retryable=retryable),
    })
    return JSONResponse(status_code=exc.status_code, content=body)


def _busy_response(session_id: str, approval_lost: bool = False) -> JSONResponse:
    """The 409 TURN_IN_PROGRESS answer, told honestly.

    Normally this is retryable: the turn is busy, and the confirm's approval
    (if any) went back on the shelf, so the retry can really succeed.

    When `approval_lost` is set the give-back did NOT happen, so the approval
    stays spent — and `turn_in_progress` becomes the WRONG answer, not merely
    an incomplete one. The client classifies that code as `busy`
    (web/src/converse.js classifyAgentError) and tells the user to wait, but
    waiting can never help: the approval is gone, and every retry of this
    confirmation_id can only fail `already_consumed`.

    So the lost case answers `409 BAD_PARAMS`, which that same classifier
    already maps to `approval_stale` -> "That request was already decided — ask
    the assistant to propose it again" (web/src/components/ConversePanel.jsx).
    That is exactly the user's real next step, delivered through an error shape
    the client ALREADY handles — no frontend change, and no new field for
    clients to learn. `approval_recovered: false` rides along for machine
    consumers. (sol-critic round 2, blocker 3.)

    The ordinary busy response is untouched: same code, same `retryable: true`,
    byte-identical body. It routes through ``error_response`` — and the lost
    case through the same ``err_envelope`` builder — because this 409 is a §3
    run envelope carrying `ok`/`tool`/`version`/`result`/`overlay`/`timing_ms`/
    `cost` alongside `error`. Hand-building it from ``with_envelope_fields``
    silently DROPS those seven fields (pinned by
    tests/test_sessions_routes.py::test_messages_busy_response_keeps_the_full_run_envelope)."""
    if not approval_lost:
        return error_response(
            ErrorCode.TURN_IN_PROGRESS,
            f"session {session_id!r} already has an active turn",
            retryable=True,
        )
    body = err_envelope(
        ErrorCode.BAD_PARAMS,
        f"session {session_id!r} already has an active turn, and this "
        f"confirmation could not be returned — request a new approval",
        retryable=False,
    )
    body["approval_recovered"] = False
    return JSONResponse(status_code=409, content=body)


def _journal_response(row: Dict[str, Any], tenant) -> JSONResponse:
    state = row["state"]
    status_code = row.get("response_status") if state in {
        "completed", "failed", "abandoned",
    } else 202
    body = dict(row.get("response_json") or {})
    body.setdefault("request_id", row["request_id"])
    body.setdefault("turn_id", row.get("turn_id"))
    body.setdefault("status", "started" if state == "executing" else state)
    if row.get("org_id") is not None and row.get("project_id") is not None:
        body["active_requests"] = request_journal.active_counts(
            row["tenant_id"], row["drawing_id"],
            org_id=row["org_id"], project_id=row["project_id"],
        )
    else:
        body["active_requests"] = request_journal.active_counts(
            row["tenant_id"], row["drawing_id"],
        )
    return JSONResponse(
        status_code=int(status_code or 202),
        content=deps.tenant_echo(with_envelope_fields(body), tenant),
    )


def _response_content(response: JSONResponse) -> Dict[str, Any]:
    body = json.loads(response.body.decode("utf-8"))
    return body if isinstance(body, dict) else {"status": "failed"}


# --------------------------------------------------------------------------- #
# POST /api/sessions
# --------------------------------------------------------------------------- #
@router.post("/api/sessions")
def create_session(req: CreateSessionRequest, tenant=Depends(deps.require_active_tenant)):
    """Idempotent per (tenant, drawing_id) — a repeat POST with the same
    drawing_id returns the SAME session (session_store's UNIQUE constraint +
    INSERT OR IGNORE, not re-derived here). An optional `model` (validated against
    the allowlist) is persisted as the session's per-session model choice."""
    if req.model is not None and not turn_runner.is_allowed_model(req.model):
        return _invalid_model_response(req.model)
    if req.policy is not None and not session_policy.is_valid_policy(req.policy):
        allowed = ", ".join(sorted(session_policy.POLICIES))
        return error_response(
            ErrorCode.BAD_PARAMS,
            f"policy {req.policy!r} is not allowed; choose one of: {allowed}",
            retryable=False, status_code=400,
        )
    org_id = None
    project_id = None
    if req.project_id is not None:
        if not request_journal.enabled():
            return error_response(
                ErrorCode.BAD_PARAMS,
                "project conversations require PostgreSQL session authority",
                retryable=False, status_code=409,
            )
        try:
            project_id = str(uuid.UUID(req.project_id))
            org_id = platform_link.require_project_access(
                tenant, project_id, write=False,
            )
        except (ValueError, LookupError):
            return _session_not_found("project")
        except platform_link.ProjectSessionForbidden:
            return _project_forbidden_response()
    # Slice 6b: only an EXPLICIT scope is written (last explicit wins); an
    # absent one is derived at read time by _scope_of, so a plain re-attach
    # never overwrites an entity focus a client set on purpose. A project
    # scope stores the CANONICAL uuid form, the same string the row binds.
    scope_kw: Dict[str, Any] = {}
    if req.scope is not None:
        handle = project_id if req.scope.kind == "project" else req.scope.handle
        scope_kw = {"scope_kind": req.scope.kind, "scope_handle": handle}
    if project_id is not None:
        sess = session_store.get_or_create_session(
            str(tenant), req.drawing_id, req.model,
            org_id=org_id, project_id=project_id, **scope_kw,
        )
    else:
        # Preserve the legacy call contract for existing adapters and tests.
        sess = session_store.get_or_create_session(
            str(tenant), req.drawing_id, req.model, **scope_kw,
        )
    if req.policy is not None:
        session_policy.set_policy(sess["session_id"], str(tenant), req.policy)
    instant = instant_execution.prepare_session(
        str(tenant), sess["session_id"], req.drawing_id,
    )
    return deps.tenant_echo(
        with_envelope_fields({
            "session_id": sess["session_id"],
            "status": sess["status"],
            "created_at": sess["created_at"],
            "model": sess.get("model"),
            # Additive (slice 6b): what this session is scoped to, stored or
            # derived, so a client that attached by the legacy fields still
            # learns the envelope the list will answer with.
            "scope": _scope_of(sess),
            **(
                {"project_id": sess["project_id"]}
                if sess.get("project_id") is not None
                else {}
            ),
            "policy": session_policy.get_policy(sess["session_id"], str(tenant)),
            "active_requests": (
                request_journal.active_counts(
                    str(tenant), sess["drawing_id"],
                    org_id=sess["org_id"], project_id=sess["project_id"],
                ) if (
                    request_journal.enabled()
                    and sess.get("org_id") is not None
                    and sess.get("project_id") is not None
                ) else (
                    request_journal.active_counts(str(tenant), sess["drawing_id"])
                    if request_journal.enabled()
                    else {"executing": 0, "queued": 0}
                )
            ),
            # Safe readiness only. The executor endpoint and signed lease stay
            # on the authenticated app-to-harness back-edge.
            "instant_ready": bool(instant["ready"]),
            "instant_reason": instant["reason"],
        }),
        tenant,
    )


# --------------------------------------------------------------------------- #
# GET /api/sessions  (list + resume, standardization slice 6b)
# --------------------------------------------------------------------------- #
@router.get("/api/sessions")
def list_sessions(scope: Optional[str] = None, limit: int = LIST_DEFAULT_LIMIT,
                  cursor: Optional[str] = None,
                  tenant=Depends(deps.require_active_tenant)):
    """The caller's OWN sessions, newest-first, one bounded page.

    `scope=<kind>:<handle>` narrows to one scope (absent: every session of the
    tenant); `limit` is clamped to [1, LIST_MAX_LIMIT] like the transcript
    route clamps its own; `cursor` is the opaque `next_cursor` of the previous
    page. A malformed scope or cursor is 422 BAD_PARAMS, never an empty page
    that reads as "no conversations". Tenant isolation is structural: the
    store's WHERE names this tenant and nothing else, and a project-bound row
    additionally re-reads membership (per listed project, memoized per page)
    exactly as every per-session route does, so another tenant's session
    never lists and a revoked member's project rows drop out.
    """
    try:
        scope_kind, scope_handle = _parse_scope_query(scope)
    except ValueError as exc:
        return error_response(
            ErrorCode.BAD_PARAMS, f"scope is malformed: {exc}",
            retryable=False, status_code=422,
        )
    try:
        after = _decode_cursor(cursor)
    except ValueError as exc:
        return error_response(
            ErrorCode.BAD_PARAMS, f"cursor is malformed: {exc}",
            retryable=False, status_code=422,
        )
    bounded = max(1, min(int(limit), LIST_MAX_LIMIT))
    rows, next_cursor = session_store.list_sessions(
        str(tenant), scope_kind=scope_kind, scope_handle=scope_handle,
        limit=bounded, cursor=after,
    )
    out: List[Dict[str, Any]] = []
    project_access: Dict[str, bool] = {}
    for sess in rows:
        if sess.get("project_id") is not None:
            key = str(sess["project_id"])
            if key not in project_access:
                try:
                    project_access[key] = platform_link.require_project_session_access(
                        sess, tenant, write=False,
                    ) is not None
                except (platform_link.ProjectSessionForbidden, LookupError):
                    # Fail closed: a row whose membership cannot be proven
                    # right now is not listed. It is not deleted, not
                    # reported, just absent from this page.
                    project_access[key] = False
            if not project_access[key]:
                continue
        out.append(_session_row(sess))
    return deps.tenant_echo(
        with_envelope_fields({
            "sessions": out,
            "next_cursor": _encode_cursor(next_cursor),
        }),
        tenant,
    )


# --------------------------------------------------------------------------- #
# POST /api/sessions/{id}/messages
# --------------------------------------------------------------------------- #
@router.post("/api/sessions/{session_id}/messages")
async def post_message_route(session_id: str, request: Request,
                             req: MessageRequest,
                             tenant=Depends(deps.require_active_tenant)):
    return post_message(session_id, req, request, tenant)


def post_message(session_id: str, req: MessageRequest, request: Request,
                 tenant=Depends(deps.require_active_tenant)):
    # 1. ownership guard (404-not-403, no existence leak).
    try:
        session = _require_owned_session(session_id, tenant, True)
    except platform_link.ProjectSessionForbidden:
        return _project_forbidden_response()
    if session is None:
        return _session_not_found(session_id)

    # 2. Validate images before entitlement evaluation or approval consumption.
    # This is deliberately refuse-not-truncate: a caller must choose a bounded
    # attachment set instead of silently losing part of it.
    try:
        images = _validate_images(req.images)
    except ValueError as exc:
        return error_response(ErrorCode.BAD_PARAMS, str(exc), retryable=False, status_code=400)

    # A message is text, images, or a confirmation. Confirmations cannot carry
    # images because an approval resumes only its server-stored proposal.
    has_text = req.text is not None
    has_confirm = req.confirm is not None
    has_images = bool(images)
    if has_confirm and has_images:
        return error_response(
            ErrorCode.BAD_PARAMS, "confirm turns cannot include images",
            retryable=False, status_code=400,
        )
    if has_confirm and has_text:
        return error_response(
            ErrorCode.BAD_PARAMS, "exactly one of `text` or `confirm` is required",
            retryable=False, status_code=409,
        )
    if not has_confirm and not has_text and not has_images:
        # 409, not 400: this is the SAME "neither" refusal deployed clients
        # already classify (web/src/converse.js classifyAgentError keys on the
        # 409+BAD_PARAMS pair). Adding `images` widened WHAT satisfies the rule;
        # it must not change the answer when nothing does.
        return error_response(
            ErrorCode.BAD_PARAMS, "one of `text`, `images`, or `confirm` is required",
            retryable=False, status_code=409,
        )

    # 2a. queue eligibility — decided BEFORE the confirm consume below, so an
    # ineligible queue request never burns an approval. Text-only by design:
    # a queued confirm would hold a consumed approval across an unbounded wait
    # (everything the give-back machinery exists to prevent), and a queued
    # credential_grant would persist a value whose whole contract is
    # "never persisted, never logged".
    if req.queue:
        if has_confirm:
            return error_response(
                ErrorCode.BAD_PARAMS, "confirm turns cannot be queued",
                retryable=False, status_code=400,
            )
        if req.credential_grant is not None:
            return error_response(
                ErrorCode.BAD_PARAMS, "credential_grant turns cannot be queued",
                retryable=False, status_code=400,
            )
        if has_images:
            # Queue entries live in this process. Retaining up to 5MB per
            # session turns a small bounded request into an unbounded memory
            # hazard, so image messages are direct-only.
            return error_response(
                ErrorCode.BAD_PARAMS, "image turns cannot be queued",
                retryable=False, status_code=400,
            )

    # 2b. "Mount your LLM" input hygiene — validated BEFORE the confirm consume,
    # so a bad model or credential never burns an approval. Model:
    # allowlist-checked. Credential: TLS-gated (a BYO token may only ride an
    # encrypted request) and shape-checked; the validated (never the raw) grant
    # is forwarded to the turn.
    #
    # `consume_approval` below still runs BEFORE `start_turn`'s busy CAS — the
    # CAS is the real busy gate and it lives inside start_turn — so a confirm
    # that races a concurrent turn IS consumed and then answered 409. The
    # TurnBusy handler at the bottom of this function gives that consume back
    # (see APPROVAL GIVE-BACK in the module docstring), which is what makes the
    # retryable 409 actually retryable.
    if req.model is not None and not turn_runner.is_allowed_model(req.model):
        return _invalid_model_response(req.model)
    validated_grant: Optional[Dict[str, Any]] = None
    if req.credential_grant is not None:
        if _credential_insecure_transport(request):
            return error_response(
                ErrorCode.BAD_PARAMS,
                "credential_grant requires a TLS (https) request",
                retryable=False, status_code=400,
            )
        validated_grant = _validate_credential_grant(req.credential_grant)
        if validated_grant is None:
            return error_response(
                ErrorCode.BAD_PARAMS,
                "credential_grant must be {kind:'api_key',api_key} or "
                "{kind:'oauth',oauth_token}",
                retryable=False, status_code=400,
            )

    # 3. entitlement gate (§17): mirrors routers/author.py:76-78 — the tenant's
    # tier must grant the `converse` capability. Off-auth/demo grants everything.
    tier = entitlements.resolve_tier(tenant)
    roles, elevated = entitlements.resolve_roles(tenant)
    try:
        allowed = entitlements.entitlements_for(tier, roles, elevated).get("converse", False)
    except entitlements.EntitlementsError:
        return entitlements.policy_unavailable_response("converse", tier)
    if not allowed:
        _emit_agent_wall_kind(tenant, session_id, "entitlement", 403,
                              "ENTITLEMENT_REQUIRED")
        return entitlements.entitlement_denied_response("converse", tier)

    # P5a admission is deliberately narrow: plain-text messages only. Those
    # are the requests that may enter the queue and can be safely recovered
    # without persisting a credential, image bytes, or approval material.
    journal_request_id: Optional[str] = None
    journal_enabled = request_journal.enabled()
    journal_eligible = (
        journal_enabled
        and has_text
        and not has_confirm
        and not has_images
        and validated_grant is None
    )
    if journal_enabled and req.request_id is not None and not journal_eligible:
        return error_response(
            ErrorCode.BAD_PARAMS,
            "request_id is supported only for plain-text messages",
            retryable=False, status_code=400,
        )
    if journal_eligible:
        try:
            journal_request_id = request_journal.canonical_request_id(req.request_id)
        except ValueError as exc:
            return error_response(
                ErrorCode.BAD_PARAMS, str(exc), retryable=False, status_code=400,
            )
        if req.queue is True:
            queue_probe = {
                "text": req.text,
                "classifier_hint": req.classifier_hint,
                "model": req.model,
            }
            try:
                request_journal.validate_recoverable_payload(queue_probe)
            except request_journal.CredentialMaterial:
                return error_response(
                    ErrorCode.BAD_PARAMS,
                    "queue payload contains credential material",
                    retryable=False, status_code=400,
                )
        digest = request_journal.payload_digest({
            "text": req.text,
            "classifier_hint": req.classifier_hint,
            "model": req.model,
            "queue": req.queue is True,
        })
        try:
            journal_row, inserted = request_journal.admit_request(
                request_id=journal_request_id,
                tenant_id=str(tenant),
                drawing_id=str(session["drawing_id"]),
                session_id=session_id,
                principal_key=str(getattr(tenant, "subject", None) or ""),
                digest=digest,
                org_id=session.get("org_id"),
                project_id=session.get("project_id"),
            )
        except request_journal.RequestConflict:
            return error_response(
                ErrorCode.BAD_PARAMS,
                "request_id is already bound to another message",
                retryable=False, status_code=409,
            )
        if not inserted and journal_row["state"] != "admitted":
            return _journal_response(journal_row, tenant)

    # 4. confirm path: atomically verify-and-consume the durable approval row
    # (merge-gate finding #1 — see module docstring's APPROVAL CONSUME note)
    # and build the frozen ConverseTurnInput.confirm shape ourselves, using
    # the STORED approved value — never the client's req.confirm.approved,
    # which is read here only to validate the confirm shape, not trusted.
    confirm_payload: Optional[Dict[str, Any]] = None
    # Hoisted so the dispatch step below can give an unredeemed consume back.
    # Every failure between here and that step returns immediately, so by the
    # time `start_turn` is called this is non-None IFF a consume succeeded.
    confirmation_id: Optional[str] = None
    if has_confirm:
        confirmation_id = (req.confirm or {}).get("confirmationId")
        if not confirmation_id:
            return error_response(
                ErrorCode.BAD_PARAMS, "confirm.confirmationId is required",
                retryable=False, status_code=400,
            )
        try:
            approval = session_store.consume_approval(
                confirmation_id, session_id, str(tenant),
                decided_by=getattr(tenant, "subject", None),
            )
        except session_store.ApprovalConsumeError as exc:
            if exc.reason == "not_found":
                # unknown / cross-session / cross-tenant collapse to the SAME
                # response (agent.py's own /approvals precedent) — no
                # existence leak.
                return error_response(
                    ErrorCode.BAD_PARAMS, f"unknown confirmation_id {confirmation_id!r}",
                    retryable=False, status_code=404,
                )
            if exc.reason == "undecided":
                # client is trying to confirm without ever having called
                # POST /api/agent/approvals first — same (409, BAD_PARAMS)
                # shape converse.js's classifyAgentError() already reads as
                # 'approval_stale' for the already-decided case.
                return error_response(
                    ErrorCode.BAD_PARAMS,
                    f"confirmation_id {confirmation_id!r} has not been decided",
                    retryable=False, status_code=409,
                )
            if exc.reason == "already_consumed":
                return error_response(
                    ErrorCode.BAD_PARAMS,
                    f"confirmation_id {confirmation_id!r} was already consumed",
                    retryable=False, status_code=409,
                )
            # exc.reason == "expired"
            return error_response(
                ErrorCode.CONFIRMATION_EXPIRED,
                f"confirmation_id {confirmation_id!r} has expired",
                retryable=False,
            )
        proposal = {
            "tool": approval.get("tool"),
            "params": approval.get("params"),
            "capability": approval.get("capability"),
        }
        stored_payload = approval.get("payload")
        stored_dwg = (
            stored_payload.get("dwg")
            if isinstance(stored_payload, dict)
            else None
        )
        if approval.get("tool") and not (
            isinstance(stored_dwg, str) and stored_dwg
        ):
            # Pre-binding approvals cannot be upgraded safely. Falling back to
            # the session drawing would authorize a target the stored row never
            # named. The approval has already been consumed, so the only safe
            # recovery is a fresh proposal with an explicit server-stored dwg.
            return error_response(
                ErrorCode.BAD_PARAMS,
                f"confirmation_id {confirmation_id!r} has no stored drawing; "
                "request a new approval",
                retryable=False,
                status_code=409,
            )
        if isinstance(stored_dwg, str):
            # Server-stored proposal truth only. The confirm request carries no
            # drawing field, so a client cannot retarget an approved action.
            proposal["dwg"] = stored_dwg
        confirm_payload = {
            "confirmation_id": confirmation_id,
            "approved": bool(approval.get("approved")),  # STORED value, never the client's
            "proposal": proposal,
        }

    # 5. dispatch the turn.
    try:
        turn_id = turn_runner.start_turn(
            tenant, session_id,
            text=req.text, confirm=confirm_payload, classifier_hint=req.classifier_hint,
            model=req.model, credential_grant=validated_grant, images=images,
            request_id=journal_request_id,
        )
    except turn_runner.TurnBusy:
        # OPT-IN queue: a text prompt with queue=true parks (cap 1) instead of
        # bouncing. Step 2a already refused confirm/grant shapes, so this
        # branch never holds an approval (confirmation_id is None on the text
        # path) and never persists a credential.
        if req.queue and has_text:
            try:
                q_status, queued_id = turn_runner.try_enqueue_turn(
                    tenant, session_id, text=req.text,
                    classifier_hint=req.classifier_hint, model=req.model,
                    request_id=journal_request_id)
            except turn_runner.TurnRejected as exc:
                return _turn_rejected_response(exc, tenant=tenant, session_id=session_id)
            if q_status == "queued":
                if journal_request_id is not None:
                    row = request_journal.get_request(journal_request_id)
                    if row is not None:
                        return _journal_response(row, tenant)
                return JSONResponse(status_code=202, content=deps.tenant_echo(
                    with_envelope_fields(
                        {"status": "queued", "queued_id": queued_id}), tenant))
            # q_status == "full": one prompt is already parked — fall through
            # to the byte-identical busy 409 ("a second is refused").
        # The approval (if any) was consumed at step 4 but NOTHING redeemed it:
        # TurnBusy means try_begin_turn lost the CAS, which happens before
        # start_turn appends `turn_started` or calls the harness. Give it back
        # so the retry this 409 explicitly invites can actually succeed.
        approval_lost = False
        if confirmation_id is not None:
            approval_lost = not _give_back_unredeemed_approval(
                confirmation_id, session_id, str(tenant))
        _emit_agent_wall_kind(tenant, session_id, "busy", 409, "turn_in_progress")
        response = _busy_response(session_id, approval_lost=approval_lost)
        if journal_request_id is not None:
            current = request_journal.get_request(journal_request_id)
            if current is not None and current["state"] != "admitted":
                return _journal_response(current, tenant)
            request_journal.fail_admitted(
                journal_request_id,
                response_status=response.status_code,
                response=_response_content(response),
            )
        return response
    except turn_runner.TurnRejected as exc:
        # Same rule, second site: give the approval back on every rejection
        # the engine can PROVE happened before the harness saw the turn
        # (exc.pre_harness — e.g. no harness URL configured, or the connection
        # was never established). Those legs report BROKER_UNREACHABLE, which
        # _RETRYABLE_BY_CODE marks retryable, so without this the client is
        # told to retry an approval that has already been spent — the very bug
        # the TurnBusy path fixes. The test is `approval_unredeemed`, which is
        # WIDER than `pre_harness`: a request can reach the harness and still be
        # refused before anything touches the confirmation (401 before the body
        # is parsed, 413 in the body reader, 429 during grant acquisition — all
        # before ConverseLoop). Those three used to burn the proposal with no
        # way to retry. It still defaults False, so ambiguous legs (read
        # timeout, a real harness rejection) never roll back; see the module
        # docstring's APPROVAL GIVE-BACK note.
        approval_lost = False
        if exc.approval_unredeemed and confirmation_id is not None:
            approval_lost = not _give_back_unredeemed_approval(
                confirmation_id, session_id, str(tenant))
        # Close the transcript the failed start opened. `turn_started` was
        # appended before the rejection, and nothing downstream will ever
        # terminate that turn — the relay only runs for accepted turns. The
        # queued kicker has closed its own failed starts this way since it
        # existed (turn_runner._kick_queued's TurnRejected leg); the DIRECT
        # path answered its HTTP caller and just left the transcript dangling,
        # for every synchronous rejection alike (401, 413, 429, 502). Same
        # closure, same event shape, so a reloaded client sees a terminated
        # turn instead of one stuck in-flight forever.
        if exc.turn_id is not None:
            try:
                session_store.append_event(
                    session_id, exc.turn_id, "error",
                    {"error": {"error_code": exc.error_code,
                               "message": exc.message},
                     "stop_reason": "error"})
            except Exception:  # noqa: BLE001
                # The caller still gets its rejection response either way, but
                # a silent append failure recreates exactly the dangling
                # transcript this closure exists to prevent — say so.
                print(f"[leaf-agent] transcript closure FAILED for turn "
                      f"{exc.turn_id!r} on session {session_id!r}; the turn "
                      f"dangles until repair", file=sys.stderr, flush=True)
        response = _turn_rejected_response(
            exc, approval_lost=approval_lost, tenant=tenant, session_id=session_id,
        )
        if journal_request_id is not None:
            if exc.turn_id is not None:
                request_journal.finish_request(
                    journal_request_id, exc.turn_id, state="failed",
                    response_status=response.status_code,
                    response=_response_content(response),
                )
            else:
                request_journal.fail_admitted(
                    journal_request_id,
                    response_status=response.status_code,
                    response=_response_content(response),
                )
        return response

    if journal_request_id is not None:
        row = request_journal.get_request(journal_request_id)
        if row is not None:
            return _journal_response(row, tenant)
    return JSONResponse(status_code=202, content=deps.tenant_echo(
        with_envelope_fields({"turn_id": turn_id, "status": "started"}), tenant))


# --------------------------------------------------------------------------- #
# POST /api/sessions/{id}/turns/{turn_id}/cancel
# --------------------------------------------------------------------------- #
@router.post("/api/sessions/{session_id}/turns/{turn_id}/cancel")
def cancel_turn(session_id: str, turn_id: str, tenant=Depends(deps.require_tenant)):
    """Interrupt the session's active turn (the composer's Esc / Stop).

    Terminal state is `turn_complete{stop_reason:"interrupted"}` — the SAME
    event the client already renders, so no new wire type is introduced and a
    stale client simply sees the turn end. Cancelling a turn that is not the
    session's current active turn is a 409 rather than a silent success: a
    client holding a stale turn_id must not be able to end the turn that
    replaced it.
    """
    # ownership guard first (404-not-403, no existence leak) — mirrors post_message.
    try:
        owned = _require_owned_session(session_id, tenant, True)
    except platform_link.ProjectSessionForbidden:
        return _project_forbidden_response()
    if owned is None:
        return _session_not_found(session_id)

    try:
        outcome = turn_runner.request_cancel(tenant, session_id, turn_id)
    except turn_runner.TurnRejected as exc:
        return _turn_rejected_response(exc, tenant=tenant, session_id=session_id)

    if outcome == "not_active":
        return error_response(
            ErrorCode.BAD_PARAMS,
            f"turn {turn_id!r} is not the active turn for session {session_id!r}",
            retryable=False, status_code=409,
        )

    if outcome == "not_cancellable":
        # A checkpoint restore holds the slot. It is not a turn and releases
        # itself; cancelling it would append a false terminal event and let a
        # real turn start while the restore is still writing the drawing.
        return error_response(
            ErrorCode.TURN_IN_PROGRESS,
            f"a checkpoint restore is in progress on session {session_id!r}; "
            f"it cannot be cancelled and will release shortly",
            retryable=True, status_code=409,
        )

    return JSONResponse(
        status_code=202,
        content=deps.tenant_echo(
            with_envelope_fields({"turn_id": turn_id, "status": "cancelled"}), tenant
        ),
    )


# --------------------------------------------------------------------------- #
# GET /api/sessions/{id}/stream (SSE)
# --------------------------------------------------------------------------- #
@router.get("/api/sessions/{session_id}/stream")
async def stream_session(session_id: str, after_seq: int = 0,
                         tenant=Depends(deps.require_active_tenant)):
    """Event-per-frame SSE (the client's EventSource addEventListener's per
    type — see converse.js openStream): `event: {type}\\ndata: {envelope}\\n\\n`.
    404 guard runs BEFORE the generator is constructed (matches
    routers/jobs.py:120-126's precedent) so an unknown/foreign session never
    starts a stream at all.

    Async generator (mirrors routers/jobs.py's stream_job): a sync generator
    here is consumed via AnyIO's threadpool, pinning one of its ~40 worker
    threads for the stream's whole lifetime — every open or abandoned tab holds
    one for up to STREAM_DEADLINE_S, and enough of them starve every sync
    endpoint in the app. The poll cadence now awaits on the event loop; the
    404 pre-check and each per-tick store read hop to a thread so the loop
    never blocks on the session_store lock. Wire format/timing unchanged."""
    try:
        owned = await asyncio.to_thread(_require_owned_session, session_id, tenant)
    except platform_link.ProjectSessionForbidden:
        return _project_forbidden_response()
    if owned is None:
        return _session_not_found(session_id)

    async def event_stream():
        cursor = int(after_seq)
        deadline = time.time() + STREAM_DEADLINE_S
        last_activity = time.time()
        while time.time() < deadline:
            try:
                current = await asyncio.to_thread(
                    _require_owned_session, session_id, tenant,
                )
            except platform_link.ProjectSessionForbidden:
                break
            if current is None:
                break
            events = await asyncio.to_thread(
                session_store.events_after, session_id, cursor, 500
            )
            if events:
                for ev in events:
                    cursor = ev["seq"]
                    yield f"event: {ev['type']}\ndata: {json.dumps(ev)}\n\n"
                last_activity = time.time()
            else:
                now = time.time()
                if now - last_activity >= STREAM_PING_S:
                    yield ": ping\n\n"
                    last_activity = now
            await asyncio.sleep(STREAM_POLL_S)

    return StreamingResponse(event_stream(), media_type="text/event-stream")


# --------------------------------------------------------------------------- #
# GET /api/sessions/{id}/transcript
# --------------------------------------------------------------------------- #
@router.get("/api/sessions/{session_id}/transcript")
def get_transcript(session_id: str, limit: int = TRANSCRIPT_DEFAULT_LIMIT,
                   tenant=Depends(deps.require_active_tenant)):
    """Most-recent-N envelopes, ascending by seq — no after_seq cursor (§2.1.4:
    'most recent N, ascending by seq'). `limit` is clamped to
    [1, TRANSCRIPT_MAX_LIMIT] regardless of what the caller sends."""
    try:
        owned = _require_owned_session(session_id, tenant)
    except platform_link.ProjectSessionForbidden:
        return _project_forbidden_response()
    if owned is None:
        return _session_not_found(session_id)

    clamped = max(1, min(int(limit), TRANSCRIPT_MAX_LIMIT))
    events = session_store.recent_events(session_id, clamped)
    return with_envelope_fields({"events": events})
