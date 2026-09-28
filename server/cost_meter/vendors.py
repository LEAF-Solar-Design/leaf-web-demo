"""Vendor, subscription and fleet costs from a declared schedule (TCM-07).

Transparency of Leaf's real cost only, never billing. config/cost-vendors.yaml
declares Leaf's non-AWS recurring costs, one entry each; vendor_observations turns
one month of that schedule into plain JSON-able cost and usage observations for
the publisher. Collectors never write the ledger.

Fails closed on anything malformed: unknown keys, duplicate keys or ids, float or
integer amounts, more than cent precision, shares that do not sum to exactly 1.
Amounts are decimal strings end to end. Payroll is not a resource here.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping, Optional, Tuple

from .ledger import (
    ESTIMATED, PERIOD_RE, RESOURCE_ID_RE, SHARE_ONE, SHARE_QUANTUM, to_decimal, validate_participant,
)

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "cost-vendors.yaml"
CONFIG_VERSION = 1
BASES = frozenset({"invoice", "card-charge", "estimate"})
RESOURCE_PREFIXES = ("vendor:", "subscription:")
DEFAULT_SHARE = {"leaf|development": "1"}
_CENT = Decimal("0.01")
_MAX_USD = Decimal("1000000000")  # bounds quantize so an absurd amount refuses, never overflows
_MAX_ENTRIES = 200
_MAX_TEXT = 256
_MAX_CONFIG_BYTES = 256 * 1024

_TOP_KEYS = frozenset({"version", "entries"})
_ENTRY_KEYS = frozenset({"resource_id", "display_name", "monthly_usd", "annual_usd", "start",
                         "basis", "source", "default_share"})
_REQUIRED_ENTRY_KEYS = frozenset({"resource_id", "display_name", "basis", "source"})


class VendorConfigError(ValueError):
    """The declared vendor schedule is malformed. Nothing is emitted from a bad file."""


@dataclass(frozen=True)
class VendorEntry:
    """One validated recurring cost. Exactly one of monthly_usd and annual_usd is set."""
    resource_id: str
    display_name: str
    monthly_usd: Optional[Decimal]
    annual_usd: Optional[Decimal]
    start: Optional[str]  # YYYY-MM, the first of the 12 months an annual amount covers
    basis: str
    source: str
    default_share: Tuple[Tuple[str, Decimal], ...]  # sorted ("participant|dimension", share)


@dataclass(frozen=True)
class VendorConfig:
    entries: Tuple[VendorEntry, ...]
    digest: str  # sha256 of the canonical validated schedule, names the source batch


def _text(value: Any, what: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > _MAX_TEXT:
        raise VendorConfigError(f"{what} must be a string of 1..{_MAX_TEXT} characters")
    return value


def _money(value: Any, what: str) -> Decimal:
    """A quoted decimal string, >= 0, at most cent precision. Refuses float and int YAML scalars."""
    if not isinstance(value, str):
        raise VendorConfigError(f"{what} must be a quoted decimal string, not {type(value).__name__}")
    try:
        d = to_decimal(value, what)
    except (TypeError, ValueError) as exc:
        raise VendorConfigError(str(exc)) from None
    if d < 0 or d > _MAX_USD:
        raise VendorConfigError(f"{what} must be in 0..{_MAX_USD}, got {d}")
    if d != d.quantize(_CENT):
        raise VendorConfigError(f"{what} carries more than cent precision: {d}")
    return d


def _month_index(period: str, what: str) -> int:
    if not isinstance(period, str) or not PERIOD_RE.fullmatch(period):
        raise VendorConfigError(f"{what} must be YYYY-MM, got {period!r}")
    return int(period[:4]) * 12 + int(period[5:7]) - 1


def _shares(value: Any, what: str) -> Tuple[Tuple[str, Decimal], ...]:
    if not isinstance(value, Mapping) or not value:
        raise VendorConfigError(f"{what} must be a non-empty mapping of participant|dimension to share")
    out = []
    total = Decimal(0)
    for key, raw in value.items():
        if not isinstance(key, str) or key.count("|") != 1:
            raise VendorConfigError(f"{what} key must be 'participant|dimension', got {key!r}")
        try:
            validate_participant(*key.split("|"))
        except (TypeError, ValueError) as exc:
            raise VendorConfigError(f"{what} key {key!r}: {exc}") from None
        if not isinstance(raw, str):
            raise VendorConfigError(f"{what}[{key}] must be a quoted decimal string, not {type(raw).__name__}")
        try:
            share = to_decimal(raw, f"{what}[{key}]")
        except (TypeError, ValueError) as exc:
            raise VendorConfigError(str(exc)) from None
        if share <= 0 or share > 1 or share != share.quantize(SHARE_QUANTUM):
            raise VendorConfigError(f"{what}[{key}] must be in (0, 1] at <= 12 places, got {share}")
        total += share
        out.append((key, share))
    if total != SHARE_ONE:
        raise VendorConfigError(f"{what} must sum to exactly 1, got {total}")
    return tuple(sorted(out))


def _entry(raw: Any, index: int) -> VendorEntry:
    where = f"entries[{index}]"
    if not isinstance(raw, Mapping):
        raise VendorConfigError(f"{where} must be a mapping")
    unknown = set(raw) - _ENTRY_KEYS
    if unknown:
        raise VendorConfigError(f"{where} has unknown keys: {sorted(map(str, unknown))}")
    missing = _REQUIRED_ENTRY_KEYS - set(raw)
    if missing:
        raise VendorConfigError(f"{where} is missing keys: {sorted(missing)}")
    resource_id = raw["resource_id"]
    if not isinstance(resource_id, str) or not RESOURCE_ID_RE.fullmatch(resource_id) \
            or not resource_id.startswith(RESOURCE_PREFIXES):
        raise VendorConfigError(f"{where} resource_id must be vendor:<name> or subscription:<name>, "
                                f"got {resource_id!r}")
    if "payroll" in resource_id:
        raise VendorConfigError(f"{where} payroll is not a resource: {resource_id!r}")
    where = f"{where} ({resource_id})"
    has_monthly, has_annual = "monthly_usd" in raw, "annual_usd" in raw
    if has_monthly == has_annual:
        raise VendorConfigError(f"{where} needs exactly one of monthly_usd and annual_usd")
    monthly = annual = start = None
    if has_monthly:
        if "start" in raw:
            raise VendorConfigError(f"{where} start applies only to annual_usd")
        monthly = _money(raw["monthly_usd"], f"{where} monthly_usd")
    else:
        if "start" not in raw:
            raise VendorConfigError(f"{where} annual_usd needs a start month (YYYY-MM)")
        annual = _money(raw["annual_usd"], f"{where} annual_usd")
        start = raw["start"]
        _month_index(start, f"{where} start")
    basis = raw["basis"]
    if basis not in BASES:
        raise VendorConfigError(f"{where} basis must be one of {sorted(BASES)}, got {basis!r}")
    return VendorEntry(
        resource_id=resource_id,
        display_name=_text(raw["display_name"], f"{where} display_name"),
        monthly_usd=monthly,
        annual_usd=annual,
        start=start,
        basis=basis,
        source=_text(raw["source"], f"{where} source"),
        default_share=_shares(raw.get("default_share", DEFAULT_SHARE), f"{where} default_share"),
    )


def validate_vendor_config(data: Any) -> VendorConfig:
    """The parsed YAML document to a VendorConfig. Fails closed with VendorConfigError."""
    if not isinstance(data, Mapping):
        raise VendorConfigError("the vendor schedule must be a mapping")
    unknown = set(data) - _TOP_KEYS
    if unknown:
        raise VendorConfigError(f"unknown top-level keys: {sorted(map(str, unknown))}")
    if data.get("version") != CONFIG_VERSION or isinstance(data.get("version"), bool):
        raise VendorConfigError(f"version must be {CONFIG_VERSION}")
    raw_entries = data.get("entries")
    if not isinstance(raw_entries, list) or not raw_entries or len(raw_entries) > _MAX_ENTRIES:
        raise VendorConfigError(f"entries must be a list of 1..{_MAX_ENTRIES} entries")
    entries = tuple(_entry(raw, i) for i, raw in enumerate(raw_entries))
    seen = set()
    for entry in entries:
        if entry.resource_id in seen:
            raise VendorConfigError(f"duplicate resource_id: {entry.resource_id}")
        seen.add(entry.resource_id)
    canonical = json.dumps([_canonical(e) for e in entries], sort_keys=True, separators=(",", ":"))
    return VendorConfig(entries=entries, digest=hashlib.sha256(canonical.encode("utf-8")).hexdigest())


def _canonical(entry: VendorEntry) -> dict:
    return {
        "resource_id": entry.resource_id,
        "display_name": entry.display_name,
        "monthly_usd": None if entry.monthly_usd is None else str(entry.monthly_usd),
        "annual_usd": None if entry.annual_usd is None else str(entry.annual_usd),
        "start": entry.start,
        "basis": entry.basis,
        "source": entry.source,
        "default_share": [[k, str(v)] for k, v in entry.default_share],
    }


def _strict_loader():
    import yaml  # PyYAML ships in scripts/requirements-ci.txt; imported only where a file is read

    class _StrictLoader(yaml.SafeLoader):
        pass

    def _mapping(loader, node, deep=False):
        keys = set()
        for key_node, _ in node.value:
            key = loader.construct_object(key_node, deep=deep)
            if key in keys:
                raise VendorConfigError(f"duplicate key {key!r} at line {key_node.start_mark.line + 1}")
            keys.add(key)
        return yaml.SafeLoader.construct_mapping(loader, node, deep=deep)

    _StrictLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)
    return yaml, _StrictLoader


def parse_vendor_config(text: str) -> VendorConfig:
    """YAML text to a VendorConfig. Duplicate mapping keys refuse instead of last-wins."""
    if not isinstance(text, str):
        raise VendorConfigError("the vendor schedule must be text")
    if len(text.encode("utf-8")) > _MAX_CONFIG_BYTES:
        raise VendorConfigError(f"the vendor schedule exceeds {_MAX_CONFIG_BYTES} bytes")
    yaml, loader = _strict_loader()
    try:
        data = yaml.load(text, Loader=loader)  # noqa: S506 - SafeLoader subclass
    except yaml.YAMLError as exc:
        raise VendorConfigError(f"the vendor schedule is not valid YAML: {exc}") from None
    return validate_vendor_config(data)


def load_vendor_config(path: Optional[Path] = None) -> VendorConfig:
    path = Path(path) if path is not None else DEFAULT_CONFIG_PATH
    return parse_vendor_config(path.read_text(encoding="utf-8"))


def month_amount(entry: VendorEntry, period: str) -> Optional[Decimal]:
    """The entry's cost for one month, or None when the entry does not cover it.

    An annual amount covers the 12 months from its start month and is spread evenly
    in whole cents; the leftover cents go one each to the earliest months, so the
    12 months always sum to exactly the annual amount.
    """
    target = _month_index(period, "period")
    if entry.monthly_usd is not None:
        return entry.monthly_usd.quantize(_CENT)
    offset = target - _month_index(entry.start, "start")
    if offset < 0 or offset >= 12:
        return None
    cents = int(entry.annual_usd.scaleb(2))  # exact: _money refused anything finer than a cent
    base, extra = divmod(cents, 12)
    return Decimal(base + (1 if offset < extra else 0)).scaleb(-2).quantize(_CENT)


def vendor_observations(period: str, config: Any) -> list:
    """One cost and one usage observation per entry covering `period`, as JSON-able dicts.

    `config` is a VendorConfig or the parsed YAML mapping (validated here). An entry
    that does not cover the month emits nothing: unknown is absent, never a fabricated 0.
    """
    if not isinstance(config, VendorConfig):
        config = validate_vendor_config(config)
    _month_index(period, "period")
    batch = f"cost-vendors:{period}:{config.digest[:16]}"
    out = []
    for entry in config.entries:
        amount = month_amount(entry, period)
        if amount is None:
            continue
        coverage = "partial" if entry.basis == "estimate" else "complete"
        out.append({
            "kind": "cost",
            "resource_id": entry.resource_id,
            "period": period,
            "gross_cost_usd": str(amount),
            "credits_usd": "0",
            "coverage": coverage,
            "source_batch_id": batch,
            "source": entry.source,
        })
        out.append({
            "kind": "usage",
            "resource_id": entry.resource_id,
            "period": period,
            "unit": "share",
            "total_usage": "1",
            "usages": {key: str(share) for key, share in entry.default_share},
            "status": ESTIMATED,
            "coverage": coverage,
            "source": entry.source,
        })
    return out
