"""Resource share ledger core (TCM-01): who used how much of each thing Leaf pays for.

Transparency of Leaf's real cost only, never billing: nothing here feeds Stripe,
quotas or caps. Every amount is a Decimal and floats are refused at every entry
point, because a binary float cannot carry an exact share. The shares of one
resource-period revision sum to exactly 1 at 12 decimal places (deterministic
largest-remainder rounding). Money is rounded to cents only in implied_cost,
the display helper; stored figures are never rounded.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, Inexact, InvalidOperation, ROUND_HALF_UP, localcontext
from fractions import Fraction
from typing import Any, Iterable, Mapping, Optional, Tuple

SHARE_PLACES = 12
SHARE_QUANTUM = Decimal("0.000000000001")
SHARE_ONE = Decimal("1.000000000000")
CENT = Decimal("0.01")

LEAF = "leaf"
LEAF_DIMENSIONS = frozenset({"development", "ci", "fleet", "unattributed"})
UNATTRIBUTED = (LEAF, "unattributed")

MEASURED = "MEASURED"
ESTIMATED = "ESTIMATED"
STATUSES = frozenset({MEASURED, ESTIMATED})
COVERAGES = frozenset({"complete", "partial", "unknown"})

PERIOD_RE = re.compile(r"\d{4}-(0[1-9]|1[0-2])")
RESOURCE_ID_RE = re.compile(r"[a-z0-9][a-z0-9:_.\-]{0,127}")
PARTICIPANT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:\-]{0,127}")
_MAX_TEXT = 256


def to_decimal(value: Any, what: str, *, allow_none: bool = False) -> Optional[Decimal]:
    """Decimal, int or numeric string to a finite Decimal. Fails closed on float, bool and NaN."""
    if value is None:
        if allow_none:
            return None
        raise ValueError(f"{what} is required")
    if isinstance(value, (bool, float)):
        raise TypeError(f"{what} must be a Decimal, int or numeric string, not {type(value).__name__}")
    if isinstance(value, Decimal):
        d = value
    elif isinstance(value, int):
        d = Decimal(value)
    elif isinstance(value, str):
        try:
            d = Decimal(value.strip())
        except InvalidOperation:
            raise ValueError(f"{what} is not a number: {value[:40]!r}") from None
    else:
        raise TypeError(f"{what} must be a Decimal, int or numeric string, not {type(value).__name__}")
    if not d.is_finite():
        raise ValueError(f"{what} must be finite")
    return d


def _non_negative(value: Any, what: str, *, allow_none: bool = False) -> Optional[Decimal]:
    d = to_decimal(value, what, allow_none=allow_none)
    if d is not None and d < 0:
        raise ValueError(f"{what} must be >= 0, got {d}")
    return d


def _text(value: Any, what: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{what} must be a string")
    if (not allow_empty and not value.strip()) or len(value) > _MAX_TEXT:
        raise ValueError(f"{what} must be 1..{_MAX_TEXT} characters")
    return value


def validate_participant(participant_id: Any, dimension: Any) -> Tuple[str, str]:
    """A tenant carries dimension ''; the reserved `leaf` carries one of LEAF_DIMENSIONS."""
    if not isinstance(participant_id, str) or not PARTICIPANT_RE.fullmatch(participant_id):
        raise ValueError(f"invalid participant id: {participant_id!r}")
    if not isinstance(dimension, str):
        raise TypeError("dimension must be a string")
    if participant_id == LEAF:
        if dimension not in LEAF_DIMENSIONS:
            raise ValueError(f"leaf dimension must be one of {sorted(LEAF_DIMENSIONS)}, got {dimension!r}")
    elif dimension != "":
        raise ValueError(f"tenant {participant_id!r} must have dimension '', got {dimension!r}")
    return participant_id, dimension


@dataclass(frozen=True)
class ResourcePeriod:
    """One resource over one UTC calendar month. Validates and coerces on construction."""
    resource_id: str
    period: str
    unit: str
    total_usage: Optional[Decimal]
    gross_cost_usd: Decimal
    credits_usd: Decimal
    source_batch_ids: Tuple[str, ...] = ()
    coverage: str = "unknown"

    def __post_init__(self) -> None:
        if not isinstance(self.resource_id, str) or not RESOURCE_ID_RE.fullmatch(self.resource_id):
            raise ValueError(f"invalid resource id: {self.resource_id!r}")
        if not isinstance(self.period, str) or not PERIOD_RE.fullmatch(self.period):
            raise ValueError(f"period must be YYYY-MM, got {self.period!r}")
        _text(self.unit, "unit")
        object.__setattr__(self, "total_usage",
                           _non_negative(self.total_usage, "total_usage", allow_none=True))
        object.__setattr__(self, "gross_cost_usd", _non_negative(self.gross_cost_usd, "gross_cost_usd"))
        object.__setattr__(self, "credits_usd", _non_negative(self.credits_usd, "credits_usd"))
        if isinstance(self.source_batch_ids, (str, bytes)):
            raise TypeError("source_batch_ids must be a list of strings")
        batches = tuple(_text(b, "source batch id") for b in self.source_batch_ids)
        object.__setattr__(self, "source_batch_ids", batches)
        if self.coverage not in COVERAGES:
            raise ValueError(f"coverage must be one of {sorted(COVERAGES)}, got {self.coverage!r}")

    def to_dict(self) -> dict:
        """Decimals serialize as strings, never as JSON numbers."""
        return {
            "resource_id": self.resource_id,
            "period": self.period,
            "unit": self.unit,
            "total_usage": None if self.total_usage is None else str(self.total_usage),
            "gross_cost_usd": str(self.gross_cost_usd),
            "credits_usd": str(self.credits_usd),
            "source_batch_ids": list(self.source_batch_ids),
            "coverage": self.coverage,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ResourcePeriod":
        if not isinstance(data, Mapping):
            raise TypeError("resource period must be an object")
        return cls(
            resource_id=data["resource_id"],
            period=data["period"],
            unit=data["unit"],
            total_usage=data["total_usage"],
            gross_cost_usd=data["gross_cost_usd"],
            credits_usd=data["credits_usd"],
            source_batch_ids=tuple(data["source_batch_ids"]),
            coverage=data["coverage"],
        )


@dataclass(frozen=True)
class ShareEntry:
    """One participant's share of one resource-period, a Decimal in [0, 1] at 12 places."""
    participant_id: str
    dimension: str
    participant_usage: Optional[Decimal]
    usage_share: Decimal
    status: str

    def __post_init__(self) -> None:
        validate_participant(self.participant_id, self.dimension)
        object.__setattr__(self, "participant_usage",
                           _non_negative(self.participant_usage, "participant_usage", allow_none=True))
        share = to_decimal(self.usage_share, "usage_share")
        if share < 0 or share > 1:
            raise ValueError(f"usage_share must be in [0, 1], got {share}")
        quantized = share.quantize(SHARE_QUANTUM)
        if quantized != share:
            raise ValueError(f"usage_share carries more than {SHARE_PLACES} places: {share}")
        object.__setattr__(self, "usage_share", quantized)
        if self.status not in STATUSES:
            raise ValueError(f"status must be one of {sorted(STATUSES)}, got {self.status!r}")

    @property
    def key(self) -> Tuple[str, str]:
        return (self.participant_id, self.dimension)

    def to_dict(self) -> dict:
        return {
            "participant_id": self.participant_id,
            "dimension": self.dimension,
            "participant_usage": None if self.participant_usage is None else str(self.participant_usage),
            "usage_share": str(self.usage_share),
            "status": self.status,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ShareEntry":
        if not isinstance(data, Mapping):
            raise TypeError("share entry must be an object")
        return cls(
            participant_id=data["participant_id"],
            dimension=data["dimension"],
            participant_usage=data["participant_usage"],
            usage_share=data["usage_share"],
            status=data["status"],
        )


def validate_shares(shares: Iterable[ShareEntry]) -> Tuple[ShareEntry, ...]:
    """The revision invariants: non-empty, unique participants, exact sum 1. Fails closed."""
    if isinstance(shares, (str, bytes, Mapping)):
        raise TypeError("shares must be a sequence of ShareEntry")
    out = tuple(shares)
    if not out:
        raise ValueError("a revision needs at least one share")
    seen = set()
    total = Decimal(0)
    for entry in out:
        if not isinstance(entry, ShareEntry):
            raise TypeError(f"share must be a ShareEntry, not {type(entry).__name__}")
        if entry.key in seen:
            raise ValueError(f"duplicate participant: {entry.key}")
        seen.add(entry.key)
        total += entry.usage_share
    if total != SHARE_ONE:
        raise ValueError(f"shares must sum to exactly 1, got {total}")
    return out


def _usage_items(usages: Any) -> Iterable[Tuple[Any, Any]]:
    if isinstance(usages, Mapping):
        return list(usages.items())
    if isinstance(usages, (str, bytes)):
        raise TypeError("usages must map (participant_id, dimension) to a usage")
    return list(usages)


def compute_shares(resource_period: ResourcePeriod, usages: Any,
                   status: str = MEASURED) -> list:
    """Shares of one resource-period from per-participant usage, summing to exactly 1.

    `usages` maps (participant_id, dimension) to a Decimal, int or numeric string
    (a mapping or an iterable of pairs; a repeated participant is refused).
    Usage the total does not account for goes to (leaf, unattributed); usage above
    the total raises. An unknown or zero total, or no usage at all, puts the whole
    resource on (leaf, unattributed) as ESTIMATED. Exact rational arithmetic, then
    largest-remainder rounding at 12 places with ties broken by participant key.
    """
    if not isinstance(resource_period, ResourcePeriod):
        raise TypeError("resource_period must be a ResourcePeriod")
    if status not in STATUSES:
        raise ValueError(f"status must be one of {sorted(STATUSES)}, got {status!r}")
    measured: dict = {}
    for item in _usage_items(usages):
        if not isinstance(item, tuple) or len(item) != 2:
            raise TypeError("each usage must be ((participant_id, dimension), usage)")
        key, value = item
        if not isinstance(key, tuple) or len(key) != 2:
            raise TypeError("a usage key must be (participant_id, dimension)")
        key = validate_participant(*key)
        if key in measured:
            raise ValueError(f"duplicate participant: {key}")
        measured[key] = _non_negative(value, f"usage for {key}")

    total = resource_period.total_usage
    with localcontext() as ctx:
        ctx.prec = 200
        ctx.traps[Inexact] = True  # usage sums are exact or refused, never silently rounded
        attributed = sum(measured.values(), Decimal(0))
        if total is not None and attributed > total:
            raise ValueError(f"participant usage {attributed} exceeds total usage {total}")
        if total is None or total == 0 or not measured:
            return [ShareEntry(LEAF, "unattributed", total, SHARE_ONE, ESTIMATED)]
        remainder = total - attributed
        if remainder > 0:
            measured[UNATTRIBUTED] = measured.get(UNATTRIBUTED, Decimal(0)) + remainder

    keys = sorted(measured)
    scale = 10 ** SHARE_PLACES
    total_f = Fraction(total)
    units: dict = {}
    fractions: dict = {}
    for key in keys:
        exact = Fraction(measured[key]) * scale / total_f
        whole = exact.numerator // exact.denominator
        units[key] = whole
        fractions[key] = exact - whole
    deficit = scale - sum(units.values())  # 0 <= deficit < len(keys): the exact parts sum to scale
    for key in sorted(keys, key=lambda k: (-fractions[k], k))[:deficit]:
        units[key] += 1
    return [
        ShareEntry(key[0], key[1], measured[key],
                   Decimal(units[key]).scaleb(-SHARE_PLACES).quantize(SHARE_QUANTUM), status)
        for key in keys
    ]


@dataclass(frozen=True)
class ImpliedCost:
    gross_usd: Decimal
    credits_usd: Decimal


def implied_cost(share: ShareEntry, resource_period: ResourcePeriod) -> ImpliedCost:
    """Display helper: share times gross cost, and times credits, rounded to cents only here."""
    if not isinstance(share, ShareEntry) or not isinstance(resource_period, ResourcePeriod):
        raise TypeError("implied_cost takes a ShareEntry and a ResourcePeriod")
    with localcontext() as ctx:
        ctx.prec = 60
        gross = (share.usage_share * resource_period.gross_cost_usd).quantize(CENT, rounding=ROUND_HALF_UP)
        credits = (share.usage_share * resource_period.credits_usd).quantize(CENT, rounding=ROUND_HALF_UP)
    return ImpliedCost(gross_usd=gross, credits_usd=credits)
