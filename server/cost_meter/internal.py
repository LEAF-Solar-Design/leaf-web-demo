"""Declared Leaf-only usage, for cost transparency only, never billing."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping, Optional, Tuple

from .ledger import ESTIMATED, LEAF, MEASURED, PERIOD_RE, RESOURCE_ID_RE, SHARE_QUANTUM, to_decimal

SOURCE = "cost_meter/data/cost-internal-resources.yaml"
DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent / "data" / "cost-internal-resources.yaml"
_PARTICIPANTS = frozenset(f"{LEAF}|{dimension}" for dimension in ("development", "ci", "fleet"))
_RESOURCE_RE = re.compile(r"(?:aws|llm|vendor|subscription):[a-z0-9]+(?:-[a-z0-9]+)*|aps:engine")
_ENTRY_KEYS = frozenset({"resource_id", "share", "basis", "status"})
_MAX_CONFIG_BYTES = 256 * 1024


class InternalConfigError(ValueError):
    """A malformed declaration refuses the entire configuration."""


@dataclass(frozen=True)
class InternalEntry:
    resource_id: str
    share: Tuple[Tuple[str, Decimal], ...]
    basis: str
    status: str


@dataclass(frozen=True)
class InternalConfig:
    entries: Tuple[InternalEntry, ...]


def validate_internal_config(data: Any) -> InternalConfig:
    if not isinstance(data, Mapping) or set(data) != {"version", "entries"}:
        raise InternalConfigError("configuration requires only version and entries")
    if type(data["version"]) is not int or data["version"] != 1:
        raise InternalConfigError("version must be 1")
    rows = data["entries"]
    if not isinstance(rows, list) or not 1 <= len(rows) <= 200:
        raise InternalConfigError("entries must be a list of 1..200 entries")
    entries = []
    seen = set()
    for row in rows:
        if not isinstance(row, Mapping) or set(row) != _ENTRY_KEYS:
            raise InternalConfigError("entry requires only resource_id, share, basis and status")
        resource_id = row["resource_id"]
        if not isinstance(resource_id, str) or not RESOURCE_ID_RE.fullmatch(resource_id) \
                or not _RESOURCE_RE.fullmatch(resource_id):
            raise InternalConfigError("resource_id must follow the shared resource naming rule")
        if resource_id in seen:
            raise InternalConfigError(f"duplicate resource_id: {resource_id}")
        seen.add(resource_id)
        basis = row["basis"]
        if not isinstance(basis, str) or not basis.strip() or len(basis) > 256:
            raise InternalConfigError("basis must be a non-empty sentence of at most 256 characters")
        status = row["status"]
        if status not in (MEASURED, ESTIMATED):
            raise InternalConfigError("status must be MEASURED or ESTIMATED")
        shares = row["share"]
        if not isinstance(shares, Mapping) or not shares:
            raise InternalConfigError("share must be a non-empty mapping")
        parsed = []
        for key, raw in shares.items():
            if key not in _PARTICIPANTS:
                raise InternalConfigError("share participants must be leaf|development, leaf|ci or leaf|fleet")
            if not isinstance(raw, str):
                raise InternalConfigError("share values must be quoted decimal strings")
            try:
                amount = to_decimal(raw, "share")
            except (TypeError, ValueError) as exc:
                raise InternalConfigError(str(exc)) from None
            if amount <= 0 or amount > 1 or amount != amount.quantize(SHARE_QUANTUM):
                raise InternalConfigError("share values must be in (0, 1] at at most 12 decimal places")
            parsed.append((key, amount))
        if sum((amount for _, amount in parsed), Decimal(0)) != 1:
            raise InternalConfigError("shares must sum to exactly 1")
        entries.append(InternalEntry(resource_id, tuple(sorted(parsed)), basis, status))
    return InternalConfig(tuple(entries))


def parse_internal_config(text: str) -> InternalConfig:
    import yaml

    if not isinstance(text, str) or len(text.encode("utf-8")) > _MAX_CONFIG_BYTES:
        raise InternalConfigError("configuration must be text of at most 256 KiB")

    class StrictLoader(yaml.SafeLoader):
        pass

    def mapping(loader, node, deep=False):
        keys = set()
        for key_node, _ in node.value:
            key = loader.construct_object(key_node, deep=deep)
            try:
                if key in keys:
                    raise InternalConfigError(f"duplicate key: {key!r}")
                keys.add(key)
            except TypeError:
                raise InternalConfigError("mapping keys must be scalar values") from None
        return yaml.SafeLoader.construct_mapping(loader, node, deep=deep)

    StrictLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, mapping)
    try:
        data = yaml.load(text, Loader=StrictLoader)  # noqa: S506 - SafeLoader subclass
    except yaml.YAMLError as exc:
        raise InternalConfigError(f"invalid YAML: {exc}") from None
    return validate_internal_config(data)


def load_internal_config(path: Optional[Path] = None) -> InternalConfig:
    path = Path(path) if path is not None else Path(os.environ.get("LEAF_COST_INTERNAL_CONFIG", DEFAULT_CONFIG_PATH))
    return parse_internal_config(path.read_text(encoding="utf-8"))


def internal_usage_observations(period: str, config: Any) -> list:
    """Return JSON-able usage observations; collectors never write the ledger."""
    if not isinstance(period, str) or not PERIOD_RE.fullmatch(period):
        raise InternalConfigError("period must be YYYY-MM")
    if not isinstance(config, InternalConfig):
        config = validate_internal_config(config)
    return [{
        "kind": "usage", "resource_id": entry.resource_id, "period": period,
        "unit": "share", "total_usage": "1",
        "usages": {key: str(amount) for key, amount in entry.share},
        "status": entry.status, "coverage": "complete", "source": SOURCE,
    } for entry in config.entries]
