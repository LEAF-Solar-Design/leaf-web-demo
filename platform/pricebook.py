"""Fail-closed, versioned operator price book with an mtime cache."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
from decimal import Decimal
from pathlib import Path

from .tenant_cost_store import ADDONS

SCHEMA = "leaf.pricebook.v1"
SUPPORTED_VERSION = 1
MAX_FEE_PCT = Decimal("100")
DEFAULT_FILE = Path(__file__).resolve().with_name("pricebook.json")
_CACHE = {}


def _keys(value, expected, field):
    if not isinstance(value, dict):
        raise ValueError(f"{field} must be an object")
    extra = set(value) - set(expected)
    missing = set(expected) - set(value)
    if extra or missing:
        names = ", ".join(sorted(extra | missing))
        raise ValueError(f"{field} has invalid fields: {names}")


def _decimal(value, field):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", value):
        raise ValueError(f"{field} must be a non-negative decimal string")
    return Decimal(value)


def _validate(book):
    _keys(book, ("schema", "version", "placeholder", "management_fee_pct", "plans", "addons"),
          "pricebook")
    if book["schema"] != SCHEMA:
        raise ValueError("schema must be leaf.pricebook.v1")
    if type(book["version"]) is not int or book["version"] != SUPPORTED_VERSION:
        raise ValueError("version must be 1")
    if type(book["placeholder"]) is not bool:
        raise ValueError("placeholder must be a boolean")
    if _decimal(book["management_fee_pct"], "management_fee_pct") > MAX_FEE_PCT:
        raise ValueError("management_fee_pct must be between 0 and 100")
    _keys(book["plans"], ("free", "platform"), "plans")
    for plan, fields in (("free", ("runs_per_month",)),
                         ("platform", ("monthly_usd", "runs_per_month"))):
        entry = book["plans"][plan]
        _keys(entry, fields, f"plans.{plan}")
        runs = entry["runs_per_month"]
        if runs is not None and (type(runs) is not int or runs < 0):
            raise ValueError(f"plans.{plan}.runs_per_month must be a non-negative int or null")
        if "monthly_usd" in fields:
            _decimal(entry["monthly_usd"], f"plans.{plan}.monthly_usd")
    _keys(book["addons"], ADDONS, "addons")
    for addon, entry in book["addons"].items():
        _keys(entry, ("billable",), f"addons.{addon}")
        if type(entry["billable"]) is not bool:
            raise ValueError(f"addons.{addon}.billable must be a boolean")


def load_pricebook(path=None) -> dict:
    """Return a defensive copy; unchanged files are never parsed again."""
    source = Path(path if path is not None else os.environ.get("LEAF_PRICEBOOK_FILE", DEFAULT_FILE))
    source = source.resolve()
    try:
        mtime = source.stat().st_mtime_ns
        cached = _CACHE.get(source)
        if cached is not None and cached[0] == mtime:
            return copy.deepcopy(cached[1])
        with source.open(encoding="utf-8") as handle:
            book = json.load(handle)
    except (OSError, ValueError) as exc:
        raise ValueError("pricebook file must contain readable JSON") from exc
    _validate(book)
    canonical = json.dumps(book, sort_keys=True, separators=(",", ":"), allow_nan=False)
    book["digest"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    _CACHE[source] = (mtime, book)
    return copy.deepcopy(book)
