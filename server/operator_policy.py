"""Operator policy catalog parser (contract/OPERATOR.md section 4).

Discipline mirrors the tenant parser: unknown fields are load errors, never
warnings; security booleans refuse coercion; a missing or unreadable file is
fail-safe deny (empty catalog); overlays may only tighten. policy_revision
is the SHA-256 of the canonical JSON, and it binds into every authority and
drift denies redemption.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Dict, Optional

_ALLOWED_TOP = {"_note", "version", "authority_ttl_s",
                "rate_limits_per_hour", "budgets", "spend_limits", "actions"}
_ALLOWED_ACTION = {"class", "rung", "policy", "required", "rate",
                   "timeout_s", "handler", "args_schema", "enabled",
                   "spend", "max_spend_cents"}
_POLICIES = {"auto", "confirm-once", "always-confirm"}
_CLASSES = {"O1", "O2", "O3", "O4", "O5", "O6"}
_SPEND_TYPES = {"none", "cost_tokens", "usd"}


class OperatorPolicyError(RuntimeError):
    pass


def _policy_path() -> Path:
    return Path(os.environ.get(
        "LEAF_OPERATOR_POLICY_FILE",
        str(Path(__file__).resolve().parent / "operator_policy.json")))


def load_catalog() -> Optional[Dict[str, Any]]:
    """Parse and validate. None = file absent/unreadable (deny everything).
    A PRESENT but INVALID file raises: a typo must fail loudly, not grant.
    """
    path = _policy_path()
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    data = json.loads(raw)

    unknown = set(data) - _ALLOWED_TOP
    if unknown:
        raise OperatorPolicyError(f"unknown top-level fields: {sorted(unknown)}")
    actions = data.get("actions")
    if not isinstance(actions, dict) or not actions:
        raise OperatorPolicyError("actions must be a non-empty object")

    for name, entry in actions.items():
        if not name.startswith("operator."):
            raise OperatorPolicyError(f"action {name!r} not operator-namespaced")
        unknown = set(entry) - _ALLOWED_ACTION
        if unknown:
            raise OperatorPolicyError(
                f"action {name}: unknown fields {sorted(unknown)}")
        if entry.get("policy") not in _POLICIES:
            raise OperatorPolicyError(f"action {name}: bad policy")
        if entry.get("class") not in _CLASSES:
            raise OperatorPolicyError(f"action {name}: bad class")
        if not isinstance(entry.get("rung"), int) or not 0 <= entry["rung"] <= 7:
            raise OperatorPolicyError(f"action {name}: bad rung")
        enabled = entry.get("enabled", True)
        if enabled is not True and enabled is not False:
            raise OperatorPolicyError(
                f"action {name}: enabled must be a strict boolean")
        if not isinstance(entry.get("handler"), str) or not entry["handler"]:
            raise OperatorPolicyError(f"action {name}: handler required")
        if not isinstance(entry.get("args_schema"), dict):
            raise OperatorPolicyError(f"action {name}: args_schema required")
        # `spend` is REQUIRED on every action, with no implicit default: an
        # absent field must not let a usd action mint free by silently reading
        # as "none". The value must be one of the known types.
        if entry.get("spend") not in _SPEND_TYPES:
            raise OperatorPolicyError(f"action {name}: spend required, one of "
                                      f"{sorted(_SPEND_TYPES)}")
        if "max_spend_cents" in entry and (
                not isinstance(entry["max_spend_cents"], int)
                or isinstance(entry["max_spend_cents"], bool)
                or entry["max_spend_cents"] < 0):
            raise OperatorPolicyError(
                f"action {name}: max_spend_cents must be a non-negative int")
        for route_word in ("production", "deploy-platform"):
            if route_word in json.dumps(entry).lower() and name != \
                    "operator.stage_release_candidate":
                raise OperatorPolicyError(
                    f"action {name}: production-shaped content refused")
    return data


def policy_revision(catalog: Optional[Dict[str, Any]] = None) -> str:
    data = catalog if catalog is not None else load_catalog()
    if data is None:
        return "absent"
    canon = json.dumps(data, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


def get_action(name: str) -> Optional[Dict[str, Any]]:
    catalog = load_catalog()
    if catalog is None:
        return None
    entry = catalog["actions"].get(name)
    if entry is None or entry.get("enabled", True) is not True:
        return None
    return entry


def canonical_args_hash(args: Dict[str, Any]) -> str:
    canon = json.dumps(args, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()
