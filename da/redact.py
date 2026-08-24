#!/usr/bin/env python3
r"""da/redact.py — strip credentials out of URLs BEFORE they reach a receipt.

Why this module exists
----------------------
An APS Design Automation WorkItem status carries a `reportUrl`: a presigned S3
URL whose query string embeds a temporary AWS credential
(`X-Amz-Credential`, `X-Amz-Security-Token`, `X-Amz-Signature`) valid for one
hour. Two committed receipts (`data/arx_probe_receipt.json`,
`data/write_spike_receipt.json`) carried three of those each, which GitHub
secret scanning flagged the moment the repo went public. Those specific
tokens had expired 37 days earlier, but the PATTERN is the defect: any process
that writes a receipt and commits it inside the hour publishes a LIVE
credential.

The fix is redaction at WRITE time, not at review time. Every path that puts a
report URL into a receipt, a log line, an exception message, or an error
envelope routes through here first, so no live presigned URL can reach git.

Contract
--------
- FAILS CLOSED. Anything that is not a parseable, in-bounds http(s) URL
  collapses to the marker. This function never returns its input unchanged when
  it could not prove the input safe.
- DROPS THE WHOLE QUERY AND FRAGMENT, unconditionally. There is no allowlist of
  "safe" parameters: presigned schemes differ per provider and per version, and
  an allowlist silently admits the next one. A report URL's identity is its
  path; the query is never load-bearing for a receipt.
- No allocation beyond the bounded input; the regex is linear (no nested
  quantifiers) and every scan is capped.
"""
from __future__ import annotations

import re
from urllib.parse import urlsplit, urlunsplit

__all__ = ["REDACTED", "redact_url", "redact_text", "report_url_fields",
           "MAX_URL_CHARS", "MAX_TEXT_CHARS"]

REDACTED = "[redacted]"

# Bounds. A presigned APS report URL runs ~1.7 KB; 8 KB leaves generous headroom
# while keeping the work per call constant-bounded. Free text (a log line, an
# exception message) is capped separately.
MAX_URL_CHARS = 8192
MAX_TEXT_CHARS = 65536
MAX_URLS_PER_TEXT = 64

# Linear, no backtracking: one bounded character class, no nested quantifiers.
# Stops at whitespace and at the delimiters a URL is normally embedded behind.
_URL_RE = re.compile(r"https?://[^\s\"'<>\|)\]}]{1,%d}" % MAX_URL_CHARS, re.IGNORECASE)

_SAFE_SCHEMES = frozenset(("http", "https"))


def redact_url(value: object, *, marker: str = REDACTED) -> str:
    """Return `value` with every credential-bearing component removed.

    Keeps scheme, host, and path (the parts that identify WHICH artifact the
    receipt is about). Drops the query string, the fragment, and any
    `user:password@` userinfo. Fails closed to `marker` on anything it cannot
    prove safe: a non-string, an over-long string, a non-http(s) scheme, or an
    unparseable URL.
    """
    if not isinstance(value, str):
        return marker
    candidate = value.strip()
    if not candidate or len(candidate) > MAX_URL_CHARS:
        return marker
    try:
        parts = urlsplit(candidate)
    except ValueError:
        return marker
    if parts.scheme.lower() not in _SAFE_SCHEMES or not parts.hostname:
        return marker
    # hostname/port only — urlsplit.netloc still carries userinfo.
    host = parts.hostname
    if parts.port is not None:
        host = f"{host}:{parts.port}"
    return urlunsplit((parts.scheme.lower(), host, parts.path, "", ""))


def redact_text(value: object, *, marker: str = REDACTED) -> str:
    """Redact every http(s) URL embedded in free text (log lines, exception
    messages, error envelopes). Fails closed: a non-string becomes `marker`,
    and text past the bound is truncated rather than passed through whole."""
    if not isinstance(value, str):
        return marker
    text = value if len(value) <= MAX_TEXT_CHARS else value[:MAX_TEXT_CHARS] + "...[truncated]"
    return _URL_RE.sub(lambda m: redact_url(m.group(0), marker=marker),
                       text, count=MAX_URLS_PER_TEXT)


def report_url_fields(status: object, *, key: str = "reportUrl",
                      prefix: str = "reportUrl") -> dict:
    """Receipt fields for a WorkItem report URL, credentials already stripped.

    Returns `{"<prefix>_path": <scheme://host/path>, "<prefix>_redacted": True}`
    so the receipt still names the exact WorkItem report it refers to, and says
    plainly that it was redacted. Returns `{}` when the status carries no URL,
    so an absent report stays absent rather than becoming a fake field.

    Never emits the raw key: a caller that merges this into a receipt block
    cannot accidentally re-introduce the credential-bearing value.
    """
    raw = status.get(key) if isinstance(status, dict) else None
    if raw is None:
        return {}
    return {f"{prefix}_path": redact_url(raw), f"{prefix}_redacted": True}
