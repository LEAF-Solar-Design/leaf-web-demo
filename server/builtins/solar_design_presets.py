"""LEAFPROFILE Create, Swap and Delete as a registry graph commit over the drawing-owned preset store.

Each subcommand runs the kernel's own command path (solar_design_profiles.leafprofile) with the
answers the plugin's prompts would take, against the store in graph["extra"]["design_profiles"]
(server/solar_preset_store.py), then publishes the new store as the next graph revision. The
drawing's current preset settings take the three always-synced layer names from graph["settings"]
(server/solar_preset_sync.py) unless the request supplies them, and after the kernel runs the
plugin's SyncFromGlobalSettings rule is applied to graph["settings"]: a Swap, the delete of the
active preset, or supplied settings that differ from the graph change the graph's three layer
names (and seed its string length and MPPT topology where they are still 0) through the
solar-settings rule, which clears sizing confirmation. The installation design is read back from
graph["project"] the same way, and a preset of the other design is written to a drawing with no
frame (the project-change rule) or refused on a drawing with frames
(DESIGN_PRESET_INSTALLATION_POPULATED, server/solar_preset_sync.py). The L1/L2 mode
(UseL2Collectors) is read back from graph["settings"] the same way, and a preset of the other mode
is written with every inverter retyped in the same commit, or refused when leaving L2 mode would
drop a combiner box, a central inverter or an L1 to L2 link (DESIGN_PRESET_L2_EQUIPMENT_PRESENT).
Otherwise no entity changes and no design output goes stale. The graph has no layer table and no DrawingStateSnapshot yet, so adoption and
layer freeze or thaw touch nothing and every profile keeps the plugin's default drawing state.
The 12 cable fields restore only when the target preset's CableMaterial is non-empty; the graph
has no cable field, so they stay in the store.

Refusals are named and checked in a fixed order: request shape, settings range, revision, stored
store, graph settings the store cannot hold, then the subcommand's own rules, the kernel, the
installation design, and last the L1/L2 mode. Pure: linear in the preset count, no I/O, no clock.
"""
import re
from datetime import datetime, timezone

import solar_preset_store as preset_store
import solar_preset_sync as preset_sync
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
    current = supplied if supplied is not None else preset_sync.effective_current(result, current)
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
    changed = preset_sync.sync(result, current)
    preset_store.save(result, current, manager)
    return advance(result, changed, TOOL)
