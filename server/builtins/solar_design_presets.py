"""LEAFPROFILE Create, Swap and Delete as a registry graph commit over the drawing-owned preset store.

Each subcommand runs the kernel's own command path (solar_design_profiles.leafprofile) with the
answers the plugin's prompts would take, against the store in graph["extra"]["design_profiles"]
(server/solar_preset_store.py), then publishes the new store as the next graph revision. No entity
changes, so no design output goes stale. The graph has no layer table and no DrawingStateSnapshot
yet, so adoption and layer freeze or thaw touch nothing and every profile keeps the plugin's
default drawing state; Swap changes the drawing's preset settings, not the graph's own settings.
The 12 cable fields restore only when the target preset's CableMaterial is non-empty.

Refusals are named and checked in a fixed order: request shape, settings range, revision, stored
store, then the subcommand's own rules. Pure: linear in the preset count, no I/O, no clock.
"""
import re
from datetime import datetime, timezone

import solar_preset_store as preset_store
from solar_design_graph import GraphValidationError, _bounded_json, require_revision
from solar_sizing_client import advance

TOOL = "solar-design-presets"
INVALID = "INVALID_DESIGN_PRESET_REQUEST"
SUBCOMMANDS = ("Create", "Swap", "Delete")
_KEYS = frozenset(("expected_rev", "subcommand", "name", "prefix", "current_settings"))
_REQUEST_PREFIX = re.compile(r"[A-Za-z]|[Pp][0-9]{2}")
# The kernel requires a creation moment; the store never keeps it (solar_preset_store.save).
_UNRECORDED = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _request(params):
    """(subcommand, trimmed name or requested prefix, normalized settings or None); fails closed."""
    _bounded_json(params)
    if (type(params) is not dict or set(params) - _KEYS
            or "expected_rev" not in params or "subcommand" not in params):
        raise GraphValidationError(INVALID)
    subcommand = params["subcommand"]
    if type(subcommand) is not str or subcommand not in SUBCOMMANDS:
        raise GraphValidationError(INVALID)
    if subcommand == "Create":
        if "prefix" in params or "name" not in params or type(params["name"]) is not str \
                or len(params["name"]) > preset_store.MAX_NAME_CHARS:
            raise GraphValidationError(INVALID)
        target = params["name"].strip()
        if not preset_store.valid_name(target):
            raise GraphValidationError(INVALID)
    else:
        if "name" in params or "prefix" not in params or type(params["prefix"]) is not str \
                or not _REQUEST_PREFIX.fullmatch(params["prefix"]):
            raise GraphValidationError(INVALID)
        target = params["prefix"]
    supplied = None
    if "current_settings" in params:
        code = preset_store.settings_error(params["current_settings"])
        if code is not None:
            raise GraphValidationError(code)
        supplied = preset_store.normalized_settings(params["current_settings"])
    return subcommand, target, supplied


def _answers(subcommand, target, manager):
    """The plugin prompts' answers for a request the store can honour, or the named refusal."""
    if subcommand == "Create":
        if any(p.name.upper() == target.upper() for p in manager.profiles):
            raise GraphValidationError("DESIGN_PRESET_NAME_EXISTS")
        if len(manager.profiles) >= preset_store.MAX_PRESETS:
            raise GraphValidationError("DESIGN_PRESET_LIMIT_REACHED")
        if manager.find(manager.next_prefix()) is not None:
            # NextPrefix's "P" + (count + 1) repeats a live prefix after a P-prefix delete.
            raise GraphValidationError("DESIGN_PRESET_PREFIX_COLLISION")
        return ["Create", target] + (["No"] if not manager.profiles else [])
    if subcommand == "Swap" and len(manager.profiles) < 2:
        raise GraphValidationError("DESIGN_PRESET_SWAP_NEEDS_TWO")
    found = manager.find(target)
    if found is None:
        raise GraphValidationError("DESIGN_PRESET_NOT_FOUND")
    if subcommand == "Swap":
        if found.prefix.upper() == (manager.active_prefix or "").upper():
            raise GraphValidationError("DESIGN_PRESET_ALREADY_ACTIVE")
        return ["Swap", found.prefix]
    return ["Delete", found.prefix, "Yes"]


def run(graph, params):
    subcommand, target, supplied = _request(params)
    result = require_revision(graph, params["expected_rev"])
    current, manager = preset_store.load(result)
    if supplied is not None:
        current = supplied
    if current is None:
        raise GraphValidationError("DESIGN_PRESET_SETTINGS_REQUIRED")
    answers = _answers(subcommand, target, manager)
    kernel = preset_store.profiles  # the kernel module the store's manager was built from
    try:
        out = kernel.leafprofile(manager, current, answers, created_utc=_UNRECORDED)
    except kernel.ProfileInputError:
        raise GraphValidationError("DESIGN_PRESET_KERNEL_REFUSED") from None
    if out["saved"] is not True or out["consumed"] != len(answers):
        raise GraphValidationError("DESIGN_PRESET_KERNEL_REFUSED")
    preset_store.save(result, current, manager)
    return advance(result, [], TOOL)
