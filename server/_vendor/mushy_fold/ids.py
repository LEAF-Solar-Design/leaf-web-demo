"""The ONE canonical consumer-id rule: REJECT, don't collapse.

Carried over from leaf-web-demo's security-audit F13 (server/tenant_id_validator.py,
mirrored byte-for-byte in the TS safeBase): three validators once disagreed, and the
one that COLLAPSED ids let two distinct consumers fold onto one namespace — a
cross-consumer data-mingling hazard. So an id is ACCEPTED unchanged iff it already
matches, else REJECTED. Never normalise (lower-casing, stripping, char-folding is
exactly what lets two inputs collide).

CHARSET — ``^[a-z0-9][a-z0-9_-]{0,62}$``: lowercase alphanumeric + hyphen +
underscore, must start alphanumeric, 1..63 chars. Rejects uppercase, spaces, dots,
``/`` and ``\\`` (path traversal), and unicode.

Consumers with a different id scheme inject their own validator into
``mushy_fold.paths`` — this module is only the default.
"""
from __future__ import annotations

import re

CONSUMER_ID_PATTERN = r"^[a-z0-9][a-z0-9_-]{0,62}$"
_CONSUMER_ID_RE = re.compile(CONSUMER_ID_PATTERN)


def is_valid_consumer_id(value: object) -> bool:
    """True iff ``value`` is a ``str`` that already matches the canonical rule."""
    return isinstance(value, str) and _CONSUMER_ID_RE.match(value) is not None


def validate_consumer_id(value: object, *, kind: str = "consumer id") -> str:
    """Return ``value`` UNCHANGED iff it matches, else raise ``ValueError``."""
    if not is_valid_consumer_id(value):
        raise ValueError(
            f"invalid {kind} {value!r}: must match {CONSUMER_ID_PATTERN} "
            "(lowercase a-z / 0-9 / '_' / '-', start alphanumeric, 1..63 chars)"
        )
    return value  # type: ignore[return-value]
