"""Constant-time text equality that never raises.

``hmac.compare_digest`` raises ``TypeError`` when either argument is a ``str``
holding a non-ASCII character. A JSON field can carry any text and an ASGI header
is decoded as latin-1, so a comparison fed by a request must not be able to throw:
an unhandled ``TypeError`` is an HTTP 500 where the caller is owed a refusal.
"""
from __future__ import annotations

import hmac


def text_matches(candidate, current) -> bool:
    """True only when both values are text and equal; fails closed, never raises.

    Compared as UTF-8 bytes, so the comparison stays constant-time for any text.
    A value that is not a ``str``, or that cannot be encoded (a lone surrogate),
    never matches. The text is read through ``str.encode`` itself, so a ``str``
    subclass is compared by the characters it holds, never by what it overrides.
    """
    if not isinstance(candidate, str) or not isinstance(current, str):
        return False
    try:
        left = str.encode(candidate, "utf-8")
        right = str.encode(current, "utf-8")
    except UnicodeEncodeError:
        return False
    return hmac.compare_digest(left, right)
