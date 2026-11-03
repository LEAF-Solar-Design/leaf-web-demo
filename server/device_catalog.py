"""Validated server catalog; changing the file requires a server release."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path
import re

from jsonschema import Draft202012Validator, FormatChecker

_ROOT = Path(__file__).resolve().parent
SCHEMA = json.loads((_ROOT.parent / "contract/device-bridge.v1.schema.json").read_text(encoding="utf-8"))
_FORMATS = FormatChecker()


@_FORMATS.checks("date-time", raises=ValueError)
def _datetime(value):
    if not isinstance(value, str):
        return True
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}[Tt]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[Zz]|[+-]\d{2}:\d{2})", value) is None:
        return False
    datetime.fromisoformat(value.upper().replace("Z", "+00:00"))
    return True


VALIDATORS = {name: Draft202012Validator({"$ref": "#/$defs/" + name, "$defs": SCHEMA["$defs"]},
                                       format_checker=_FORMATS) for name in SCHEMA["$defs"]}


def canonical_path(path: str) -> bool:
    return (isinstance(path, str) and path.startswith("/") and not path.startswith("//")
            and len(path) <= 2048 and not any(c in path for c in "%\\?#")
            and not any(ord(c) <= 32 or 127 <= ord(c) <= 159 or c.isspace() for c in path)
            and not any(part in (".", "..", "") for part in path.split("/")[1:-1])
            and path.rsplit("/", 1)[-1] not in (".", ".."))


def load_catalog(path: Path) -> list[dict]:
    entries = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(entries, list) or not entries:
        raise ValueError("invalid device catalog")
    names = set()
    for entry in entries:
        if not VALIDATORS["PackageCatalogEntry"].is_valid(entry):
            raise ValueError("invalid device catalog entry")
        if (entry["package"] in names or not canonical_path(entry["health_path"])
                or any(not canonical_path(rule["path_prefix"]) for rule in entry["relay_allow"])):
            raise ValueError("invalid device catalog paths or duplicate package")
        names.add(entry["package"])
    return entries


_PACKAGES = load_catalog(_ROOT / "device_packages.json")


def packages() -> list[dict]:
    return deepcopy(_PACKAGES)


def get_package(name: str) -> dict | None:
    return next((entry for entry in packages() if entry["package"] == name), None)


def allows_proxy(package: dict, method: str, path: str) -> bool:
    if not canonical_path(path):
        return False
    return any(rule["method"] == method and (
        path == rule["path_prefix"] or path.startswith(rule["path_prefix"].rstrip("/") + "/"))
        for rule in package["relay_allow"])
